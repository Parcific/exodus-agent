from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .archive import Archive
from .job import JobEvent, JobEventKind, JobStore
from .model import Attachment, Message
from .protocols import ClipAwareMessageSource, DiscoverySource, MediaSource, MembershipSource, MessageSource


@dataclass(frozen=True)
class DryRunResult:
    conversations: int
    participants: int
    memberships: int
    messages: int
    attachments: int


def export_dry_run(
    *,
    job_id: str,
    archive: Archive,
    job_store: JobStore,
    source: DiscoverySource & MessageSource,
    source_kind: str,
    target_kind: str,
    name: str,
    reset_archive: bool = False,
) -> DryRunResult:
    job_store.create(job_id=job_id)
    archive.initialize(
        source_kind=source_kind,
        target_kind=target_kind,
        name=name,
        reset=reset_archive,
    )

    _phase(job_store, job_id, "extract", JobEventKind.PHASE_STARTED)
    workspace = source.get_workspace()
    conversations = tuple(source.list_conversations())
    participants = tuple(source.list_participants())
    memberships = tuple(source.list_memberships()) if isinstance(source, MembershipSource) else ()
    _validate_export_snapshot(conversations=conversations, participants=participants, memberships=memberships)

    archive.write_workspace(workspace)
    archive.write_conversations(conversations)
    archive.write_participants(participants)
    archive.write_memberships(memberships)

    message_count = 0
    attachment_count = 0
    message_source_ids: set[str] = set()
    for conversation in conversations:
        messages = tuple(_materialize_attachments(archive, source, source.list_messages(conversation)))
        excluded_root_ids = (
            source.get_excluded_root_ids(conversation)
            if isinstance(source, ClipAwareMessageSource)
            else frozenset()
        )
        _validate_export_messages(
            conversation_id=conversation.source_id,
            messages=messages,
            message_source_ids=message_source_ids,
            excluded_root_ids=excluded_root_ids,
        )
        message_count += len(messages)
        attachment_count += sum(len(message.attachments) for message in messages)
        archive.write_messages(conversation.source_id, messages)

    _phase(
        job_store,
        job_id,
        "extract",
        JobEventKind.PHASE_COMPLETED,
        data={
            "conversations": len(conversations),
            "participants": len(participants),
            "memberships": len(memberships),
            "messages": message_count,
            "attachments": attachment_count,
        },
    )
    return DryRunResult(
        conversations=len(conversations),
        participants=len(participants),
        memberships=len(memberships),
        messages=message_count,
        attachments=attachment_count,
    )


def _materialize_attachments(
    archive: Archive,
    source: DiscoverySource & MessageSource,
    messages: Iterable[Message],
) -> Iterable[Message]:
    if not isinstance(source, MediaSource):
        yield from messages
        return

    for message in messages:
        attachments: list[Attachment] = []
        for attachment in message.attachments:
            _required_record_id(attachment, "source_id", label="attachment")
            _required_record_string(attachment, "filename", label="attachment")
            if attachment.local_path:
                attachments.append(attachment)
                continue
            content = source.download_attachment(attachment)
            stored = archive.write_attachment_blob(
                source_id=attachment.source_id,
                filename=attachment.filename,
                content=content,
            )
            attachments.append(
                Attachment(
                    source_id=attachment.source_id,
                    filename=attachment.filename,
                    mime_type=attachment.mime_type,
                    size_bytes=stored.size_bytes,
                    sha256=stored.sha256,
                    local_path=stored.local_path,
                    metadata=attachment.metadata,
                )
            )
        yield Message(
            source_id=message.source_id,
            conversation_id=message.conversation_id,
            author_id=message.author_id,
            created_at=message.created_at,
            text=message.text,
            markdown=message.markdown,
            html=message.html,
            parent_id=message.parent_id,
            edited_at=message.edited_at,
            deleted_at=message.deleted_at,
            attachments=tuple(attachments),
            metadata=message.metadata,
        )


def _validate_export_snapshot(
    *,
    conversations: tuple[object, ...],
    participants: tuple[object, ...],
    memberships: tuple[object, ...],
) -> None:
    conversation_ids = _unique_source_ids(conversations, label="conversation")
    participant_ids = _unique_source_ids(participants, label="participant")
    membership_ids = _unique_source_ids(memberships, label="membership")
    del membership_ids
    for membership in memberships:
        _required_record_id(membership, "conversation_id", label="membership")
        _required_record_id(membership, "participant_id", label="membership")
        if membership.conversation_id not in conversation_ids:
            raise ValueError(
                "Export membership references unknown conversation_id: "
                f"{membership.source_id} -> {membership.conversation_id}"
            )
        if membership.participant_id not in participant_ids:
            raise ValueError(
                "Export membership references unknown participant_id: "
                f"{membership.source_id} -> {membership.participant_id}"
            )


def _validate_export_messages(
    *,
    conversation_id: str,
    messages: tuple[Message, ...],
    message_source_ids: set[str],
    excluded_root_ids: frozenset[str] = frozenset(),
) -> None:
    conversation_message_ids = {message.source_id for message in messages}
    for message in messages:
        _required_record_id(message, "source_id", label="message")
        _required_record_id(message, "conversation_id", label="message")
        if message.author_id is not None:
            _required_record_id(message, "author_id", label="message")
        if message.parent_id is not None:
            _required_record_id(message, "parent_id", label="message")
        for attachment in message.attachments:
            _required_record_id(attachment, "source_id", label="attachment")
            _required_record_string(attachment, "filename", label="attachment")
        if message.conversation_id != conversation_id:
            raise ValueError(
                "Export message references unexpected conversation_id: "
                f"{message.source_id} -> {message.conversation_id}"
            )
        if message.source_id in message_source_ids:
            raise ValueError(f"Export contains duplicate message source_id: {message.source_id}")
        if message.parent_id == message.source_id:
            raise ValueError(f"Export message references itself as parent_id: {message.source_id}")
        if message.parent_id is not None and message.parent_id not in conversation_message_ids:
            if message.parent_id not in excluded_root_ids:
                raise ValueError(
                    "Export message references unknown parent_id in conversation: "
                    f"{message.source_id} -> {message.parent_id}"
                )
            # parent_id predates the message_since window: thread root was clipped.
            # The reply is still valid and included in the archive; the dangling
            # parent_id is expected and not an error.
        message_source_ids.add(message.source_id)


def _unique_source_ids(records: tuple[object, ...], *, label: str) -> set[str]:
    source_ids: set[str] = set()
    for record in records:
        source_id = _required_record_id(record, "source_id", label=label)
        if source_id in source_ids:
            raise ValueError(f"Export contains duplicate {label} source_id: {source_id}")
        source_ids.add(source_id)
    return source_ids


def _required_record_string(record: object, field_name: str, *, label: str) -> str:
    value = getattr(record, field_name, None)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Export {label} has invalid {field_name}")
    return value


def _required_record_id(record: object, field_name: str, *, label: str) -> str:
    value = _required_record_string(record, field_name, label=label)
    if value != value.strip():
        raise ValueError(f"Export {label} has invalid {field_name}")
    return value


def _phase(
    job_store: JobStore,
    job_id: str,
    phase: str,
    kind: JobEventKind,
    data: dict[str, object] | None = None,
) -> None:
    job_store.append(JobEvent(kind=kind, job_id=job_id, phase=phase, data=data or {}))
