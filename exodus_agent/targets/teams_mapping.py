from __future__ import annotations

import csv
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path

from exodus_agent.archive import Archive
from exodus_agent.model import Attachment, Conversation, ConversationKind, ConversationMembership, Message, Participant

from .teams_shared import _as_utc, _truncate_to_millisecond, _timestamp_adjustment_reason


class TeamsTargetKind(StrEnum):
    ONE_ON_ONE_CHAT = "one_on_one_chat"
    GROUP_CHAT = "group_chat"
    TEAM_CHANNEL = "team_channel"
    REVIEW_REQUIRED = "review_required"


@dataclass(frozen=True)
class TeamsConversationMapping:
    source_conversation_id: str
    target_kind: TeamsTargetKind
    confidence: str
    reason: str
    title: str | None
    participant_count: int
    message_count: int
    missing_identity_count: int
    participant_source_ids: tuple[str, ...]
    target_user_ids: tuple[str, ...]

    def to_json(self) -> dict[str, object]:
        return {
            "source_conversation_id": self.source_conversation_id,
            "target_kind": self.target_kind.value,
            "confidence": self.confidence,
            "reason": self.reason,
            "title": self.title,
            "participant_count": self.participant_count,
            "message_count": self.message_count,
            "missing_identity_count": self.missing_identity_count,
            "participant_source_ids": list(self.participant_source_ids),
            "target_user_ids": list(self.target_user_ids),
            "target": _target_placeholder(self.target_kind),
        }


@dataclass(frozen=True)
class TeamsMappingTemplateResult:
    path: Path
    conversations: int
    review_required: int


@dataclass(frozen=True)
class TeamsImportPlanResult:
    path: Path
    conversations: int
    messages: int
    attachments: int
    unsupported_attachments: int
    timestamp_adjustments: int


@dataclass(frozen=True)
class TeamsIdentityMappingTemplateResult:
    path: Path
    identities: int


@dataclass(frozen=True)
class TeamsIdentityMappingEntry:
    source_user_id: str
    entra_user_id: str
    display_name: str
    email: str | None
    status: str
    reason: str

    def to_json(self) -> dict[str, object]:
        return {
            "source_user_id": self.source_user_id,
            "entra_user_id": self.entra_user_id,
            "display_name": self.display_name,
            "email": self.email,
            "status": self.status,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class EntraUser:
    id: str
    mail: str | None = None
    user_principal_name: str | None = None
    proxy_addresses: tuple[str, ...] = ()
    other_mails: tuple[str, ...] = ()


@dataclass(frozen=True)
class CompletedTeamsConversationMapping:
    source_conversation_id: str
    target_kind: TeamsTargetKind
    target: Mapping[str, str]


@dataclass(frozen=True)
class TeamsPreparedMessage:
    import_order: int
    source_message_id: str
    source_conversation_id: str
    target_kind: TeamsTargetKind
    target: Mapping[str, str]
    author_user_id: str
    created_date_time: str
    original_created_at: str
    timestamp_adjusted: bool
    timestamp_adjustment_ms: int
    timestamp_adjustment_reason: str | None
    parent_source_message_id: str | None
    content: str
    attachments: tuple[dict[str, object], ...]

    def to_json(self) -> dict[str, object]:
        return {
            "import_order": self.import_order,
            "source_message_id": self.source_message_id,
            "source_conversation_id": self.source_conversation_id,
            "target_kind": self.target_kind.value,
            "target": dict(self.target),
            "author_user_id": self.author_user_id,
            "createdDateTime": self.created_date_time,
            "original_created_at": self.original_created_at,
            "timestamp_adjusted": self.timestamp_adjusted,
            "timestamp_adjustment_ms": self.timestamp_adjustment_ms,
            "timestamp_adjustment_reason": self.timestamp_adjustment_reason,
            "parent_source_message_id": self.parent_source_message_id,
            "content": self.content,
            "attachments": list(self.attachments),
        }


def write_teams_identity_map_template(
    *,
    archive: Archive,
    output_path: Path,
    existing_identity_map: Mapping[str, str] | None = None,
    identity_map_reasons: Mapping[str, str] | None = None,
    overwrite: bool = False,
) -> TeamsIdentityMappingTemplateResult:
    _ensure_output_file_path(output_path, "Teams identity map template")
    if output_path.exists() and not overwrite:
        raise FileExistsError(output_path)
    entries = _identity_template_entries(
        archive.read_participants(),
        existing_identity_map or {},
        identity_map_reasons or {},
    )
    if not entries:
        raise ValueError("Archive contains no identities to map")
    payload = {
        "format": "exodus.teams.identity_map.v1",
        "identities": [entry.to_json() for entry in entries],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return TeamsIdentityMappingTemplateResult(path=output_path, identities=len(entries))


def load_entra_users(path: Path) -> tuple[EntraUser, ...]:
    if path.suffix.casefold() == ".csv":
        return _load_entra_users_csv(path)
    return _load_entra_users_json(path)


def build_teams_identity_prefill_from_entra(
    *,
    archive: Archive,
    entra_users: tuple[EntraUser, ...],
) -> tuple[dict[str, str], dict[str, str]]:
    indexes = _entra_email_indexes(entra_users)
    identity_map: dict[str, str] = {}
    reasons: dict[str, str] = {}
    for participant in archive.read_participants():
        if not participant.email:
            continue
        email_key = _email_key(participant.email)
        if not email_key:
            continue
        for field_name, index in indexes:
            matched_ids = index.get(email_key, ())
            if len(matched_ids) == 1:
                identity_map[participant.source_id] = matched_ids[0]
                reasons[participant.source_id] = f"Exact Webex email matched Entra {field_name}."
                break
            if len(matched_ids) > 1:
                reasons[participant.source_id] = (
                    f"Needs review: Webex email matched multiple Entra {field_name} values."
                )
                break
    return identity_map, reasons


def load_teams_identity_map(path: Path) -> dict[str, str]:
    payload = _read_json_file(path, "Teams identity map")
    if not isinstance(payload, dict) or payload.get("format") != "exodus.teams.identity_map.v1":
        raise ValueError("Teams identity map has unsupported format")
    identities = payload.get("identities")
    if not isinstance(identities, list):
        raise ValueError("Teams identity map identities must be a list")
    identity_map: dict[str, str] = {}
    source_by_entra_id: dict[str, str] = {}
    for index, item in enumerate(identities):
        if not isinstance(item, dict):
            raise ValueError(f"Teams identity map row {index} must be an object")
        source_user_id = item.get("source_user_id")
        entra_user_id = item.get("entra_user_id")
        if not isinstance(source_user_id, str) or not source_user_id.strip():
            raise ValueError(f"Teams identity map row {index} missing source_user_id")
        if not isinstance(entra_user_id, str) or not entra_user_id.strip():
            raise ValueError(f"Teams identity map row {index} missing entra_user_id")
        source_user_id = source_user_id.strip()
        if source_user_id in identity_map:
            raise ValueError(f"Teams identity map duplicates source_user_id: {source_user_id}")
        normalized_entra_user_id = entra_user_id.strip()
        normalized_entra_user_key = normalized_entra_user_id.casefold()
        existing_source_user_id = source_by_entra_id.get(normalized_entra_user_key)
        if existing_source_user_id is not None:
            raise ValueError(
                "Teams identity map assigns multiple source_user_id values to "
                f"entra_user_id {normalized_entra_user_id}: {existing_source_user_id}, {source_user_id}"
            )
        identity_map[source_user_id] = normalized_entra_user_id
        source_by_entra_id[normalized_entra_user_key] = source_user_id
    return identity_map


def load_teams_conversation_map(
    path: Path,
    *,
    allow_review_required: bool = False,
) -> tuple[CompletedTeamsConversationMapping, ...]:
    payload = _read_json_file(path, "Teams conversation map")
    if not isinstance(payload, dict) or payload.get("format") != "exodus.teams.mapping_template.v1":
        raise ValueError("Teams conversation map has unsupported format")
    conversations = payload.get("conversations")
    if not isinstance(conversations, list):
        raise ValueError("Teams conversation map conversations must be a list")

    mappings: list[CompletedTeamsConversationMapping] = []
    source_ids: set[str] = set()
    target_assignments: dict[str, str] = {}
    for index, item in enumerate(conversations):
        if not isinstance(item, dict):
            raise ValueError(f"Teams conversation map row {index} must be an object")
        source_conversation_id = item.get("source_conversation_id")
        if not isinstance(source_conversation_id, str) or not source_conversation_id.strip():
            raise ValueError(f"Teams conversation map row {index} missing source_conversation_id")
        source_conversation_id = source_conversation_id.strip()
        if source_conversation_id in source_ids:
            raise ValueError(
                "Teams conversation map duplicates source_conversation_id: "
                f"{source_conversation_id}"
            )
        source_ids.add(source_conversation_id)

        raw_target_kind = item.get("target_kind")
        try:
            target_kind = TeamsTargetKind(raw_target_kind)
        except ValueError as exc:
            raise ValueError(
                f"Teams conversation map row {index} has unsupported target_kind: "
                f"{raw_target_kind!r}"
            ) from exc
        if target_kind == TeamsTargetKind.REVIEW_REQUIRED and not allow_review_required:
            raise ValueError(
                "Teams conversation map contains unresolved review_required row: "
                f"{source_conversation_id}"
            )

        target = item.get("target")
        if not isinstance(target, dict):
            raise ValueError(f"Teams conversation map row {index} target must be an object")
        normalized_target = _completed_target_for_row(
            target_kind=target_kind,
            target=target,
            row_index=index,
        )
        target_key = _target_assignment_key(target_kind, normalized_target)
        if target_key:
            existing_source_id = target_assignments.get(target_key)
            if existing_source_id is not None:
                raise ValueError(
                    "Teams conversation map assigns multiple source conversations "
                    f"to target {target_key}: {existing_source_id}, {source_conversation_id}"
                )
            target_assignments[target_key] = source_conversation_id
        mappings.append(
            CompletedTeamsConversationMapping(
                source_conversation_id=source_conversation_id,
                target_kind=target_kind,
                target=normalized_target,
            )
        )
    return tuple(mappings)


def prepare_teams_import_messages(
    *,
    archive: Archive,
    conversation_map: tuple[CompletedTeamsConversationMapping, ...],
    identity_map: Mapping[str, str],
    import_time: datetime | None = None,
) -> tuple[TeamsPreparedMessage, ...]:
    import_cutoff = _as_utc(import_time or datetime.now(timezone.utc))
    mappings_by_conversation: dict[str, CompletedTeamsConversationMapping] = {}
    target_assignments: dict[str, str] = {}
    for mapping in conversation_map:
        if mapping.source_conversation_id in mappings_by_conversation:
            raise ValueError(
                "Teams conversation map duplicates source_conversation_id: "
                f"{mapping.source_conversation_id}"
            )
        target_key = _target_assignment_key(mapping.target_kind, mapping.target)
        if target_key is None:
            raise ValueError(
                "Cannot prepare Teams import messages for unresolved conversation: "
                f"{mapping.source_conversation_id}"
            )
        existing_source_id = target_assignments.get(target_key)
        if existing_source_id is not None:
            raise ValueError(
                "Teams conversation map assigns multiple source conversations "
                f"to target {target_key}: {existing_source_id}, {mapping.source_conversation_id}"
            )
        target_assignments[target_key] = mapping.source_conversation_id
        mappings_by_conversation[mapping.source_conversation_id] = mapping
    archive_conversation_ids = {conversation.source_id for conversation in archive.read_conversations()}
    mapped_conversation_ids = set(mappings_by_conversation)
    missing_mappings = sorted(archive_conversation_ids - mapped_conversation_ids)
    unknown_mappings = sorted(mapped_conversation_ids - archive_conversation_ids)
    if missing_mappings:
        raise ValueError(
            "Teams conversation map is missing archived source_conversation_id values: "
            + ", ".join(missing_mappings)
        )
    if unknown_mappings:
        raise ValueError(
            "Teams conversation map contains unknown source_conversation_id values: "
            + ", ".join(unknown_mappings)
        )
    prepared: list[TeamsPreparedMessage] = []
    prepared_source_message_ids: set[str] = set()
    used_timestamps_by_target: dict[str, set[datetime]] = {}
    prepared_timestamps_by_source: dict[str, datetime] = {}
    for source_conversation_id in sorted(mappings_by_conversation):
        mapping = mappings_by_conversation[source_conversation_id]
        target_key = _target_assignment_key(mapping.target_kind, mapping.target)
        used_timestamps = used_timestamps_by_target.setdefault(target_key, set())
        messages = _ordered_messages_for_import(
            archive.read_messages(source_conversation_id),
            conversation_id=source_conversation_id,
        )
        for message in messages:
            if message.source_id in prepared_source_message_ids:
                raise ValueError(f"Teams import plan duplicates source_message_id: {message.source_id}")
            prepared_source_message_ids.add(message.source_id)
            if not message.author_id:
                raise ValueError(f"Message {message.source_id} is missing an author")
            author_user_id = identity_map.get(message.author_id, "").strip()
            if not author_user_id:
                raise ValueError(
                    f"Message {message.source_id} author {message.author_id} is not mapped to Entra ID"
                )
            original_created_at = _as_utc(message.created_at)
            if original_created_at > import_cutoff:
                raise ValueError(
                    f"Message {message.source_id} created_at is in the future for Teams import"
                )
            created_at = _truncate_to_millisecond(original_created_at)
            precision_adjusted = created_at != original_created_at
            if message.parent_id is not None:
                parent_created_at = prepared_timestamps_by_source[message.parent_id]
                if created_at <= parent_created_at:
                    created_at = parent_created_at + timedelta(milliseconds=1)
            while created_at in used_timestamps:
                created_at += timedelta(milliseconds=1)
            timestamp_capped = created_at > import_cutoff
            if timestamp_capped:
                created_at = import_cutoff
            used_timestamps.add(created_at)
            prepared_timestamps_by_source[message.source_id] = created_at
            adjustment_ms = int((created_at - _truncate_to_millisecond(original_created_at)).total_seconds() * 1000)
            reason = _timestamp_adjustment_reason(
                precision_adjusted=precision_adjusted,
                collision_adjustment_ms=adjustment_ms,
                cutoff_capped=timestamp_capped,
            )
            prepared.append(
                TeamsPreparedMessage(
                    import_order=len(prepared),
                    source_message_id=message.source_id,
                    source_conversation_id=source_conversation_id,
                    target_kind=mapping.target_kind,
                    target=mapping.target,
                    author_user_id=author_user_id,
                    created_date_time=_graph_datetime(created_at),
                    original_created_at=_audit_datetime(original_created_at),
                    timestamp_adjusted=reason is not None,
                    timestamp_adjustment_ms=adjustment_ms,
                    timestamp_adjustment_reason=reason,
                    parent_source_message_id=message.parent_id,
                    content=message.html or message.markdown or message.text or "",
                    attachments=tuple(_attachment_to_plan_json(attachment) for attachment in message.attachments),
                )
            )
    return tuple(prepared)


def write_teams_import_plan(
    *,
    archive: Archive,
    conversation_map: tuple[CompletedTeamsConversationMapping, ...],
    identity_map: Mapping[str, str],
    output_path: Path,
    overwrite: bool = False,
    import_time: datetime | None = None,
) -> TeamsImportPlanResult:
    _ensure_output_file_path(output_path, "Teams import plan")
    if output_path.exists() and not overwrite:
        raise FileExistsError(output_path)
    prepared_messages = prepare_teams_import_messages(
        archive=archive,
        conversation_map=conversation_map,
        identity_map=identity_map,
        import_time=import_time,
    )
    payload = {
        "format": "exodus.teams.import_plan.v1",
        "messages": [message.to_json() for message in prepared_messages],
        "unsupported_attachments": _unsupported_attachment_rows(prepared_messages),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return TeamsImportPlanResult(
        path=output_path,
        conversations=len(conversation_map),
        messages=len(prepared_messages),
        attachments=sum(len(message.attachments) for message in prepared_messages),
        unsupported_attachments=sum(
            1 for message in prepared_messages for a in message.attachments if a.get("supported") is False
        ),
        timestamp_adjustments=sum(1 for message in prepared_messages if message.timestamp_adjusted),
    )


def build_teams_conversation_mappings(
    *,
    archive: Archive,
    identity_map: Mapping[str, str],
    group_chat_member_limit: int = 8,
) -> tuple[TeamsConversationMapping, ...]:
    if group_chat_member_limit < 2:
        raise ValueError("group_chat_member_limit must be at least 2")
    conversations = archive.read_conversations()
    memberships = archive.read_memberships()
    return tuple(
        _classify_conversation(
            archive=archive,
            conversation=conversation,
            memberships=memberships,
            identity_map=identity_map,
            group_chat_member_limit=group_chat_member_limit,
        )
        for conversation in conversations
    )


def write_teams_mapping_template(
    *,
    archive: Archive,
    identity_map: Mapping[str, str],
    output_path: Path,
    overwrite: bool = False,
    group_chat_member_limit: int = 8,
) -> TeamsMappingTemplateResult:
    _ensure_output_file_path(output_path, "Teams conversation map template")
    if output_path.exists() and not overwrite:
        raise FileExistsError(output_path)
    mappings = build_teams_conversation_mappings(
        archive=archive,
        identity_map=identity_map,
        group_chat_member_limit=group_chat_member_limit,
    )
    if not mappings:
        raise ValueError("Archive contains no conversations to map")
    payload = {
        "format": "exodus.teams.mapping_template.v1",
        "conversations": [mapping.to_json() for mapping in mappings],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return TeamsMappingTemplateResult(
        path=output_path,
        conversations=len(mappings),
        review_required=sum(1 for mapping in mappings if mapping.target_kind == TeamsTargetKind.REVIEW_REQUIRED),
    )


def _ensure_output_file_path(path: Path, label: str) -> None:
    if path.exists() and not path.is_file():
        raise ValueError(f"{label} output path must be a file: {path}")


def _classify_conversation(
    *,
    archive: Archive,
    conversation: Conversation,
    memberships: list[ConversationMembership],
    identity_map: Mapping[str, str],
    group_chat_member_limit: int,
) -> TeamsConversationMapping:
    messages = archive.read_messages(conversation.source_id)
    participant_ids = _participant_ids_for_conversation(conversation.source_id, messages, memberships)
    target_user_ids = tuple(
        identity_map[source_id]
        for source_id in participant_ids
        if source_id in identity_map and identity_map[source_id]
    )
    missing_identity_count = len(participant_ids) - len(target_user_ids)
    if missing_identity_count:
        return TeamsConversationMapping(
            source_conversation_id=conversation.source_id,
            target_kind=TeamsTargetKind.REVIEW_REQUIRED,
            confidence="blocked",
            reason="One or more Webex participants are missing Entra user mappings.",
            title=conversation.title,
            participant_count=len(participant_ids),
            message_count=len(messages),
            missing_identity_count=missing_identity_count,
            participant_source_ids=participant_ids,
            target_user_ids=target_user_ids,
        )

    if conversation.kind == ConversationKind.DIRECT and len(target_user_ids) == 2:
        return _mapping(
            conversation=conversation,
            target_kind=TeamsTargetKind.ONE_ON_ONE_CHAT,
            confidence="high",
            reason="Webex direct conversation with exactly two mapped participants.",
            participant_ids=participant_ids,
            target_user_ids=target_user_ids,
            message_count=len(messages),
        )

    if conversation.kind == ConversationKind.DIRECT:
        return TeamsConversationMapping(
            source_conversation_id=conversation.source_id,
            target_kind=TeamsTargetKind.REVIEW_REQUIRED,
            confidence="blocked",
            reason="Webex direct conversation does not have exactly two mapped participants.",
            title=conversation.title,
            participant_count=len(participant_ids),
            message_count=len(messages),
            missing_identity_count=missing_identity_count,
            participant_source_ids=participant_ids,
            target_user_ids=target_user_ids,
        )

    if 2 <= len(target_user_ids) <= group_chat_member_limit and conversation.kind in {
        ConversationKind.GROUP,
        ConversationKind.SPACE,
    }:
        return _mapping(
            conversation=conversation,
            target_kind=TeamsTargetKind.GROUP_CHAT,
            confidence="medium",
            reason="Small mapped Webex space/group; defaulting to Teams group chat.",
            participant_ids=participant_ids,
            target_user_ids=target_user_ids,
            message_count=len(messages),
        )

    if len(target_user_ids) >= 2:
        return _mapping(
            conversation=conversation,
            target_kind=TeamsTargetKind.TEAM_CHANNEL,
            confidence="medium",
            reason="Large or persistent Webex space; defaulting to Teams channel.",
            participant_ids=participant_ids,
            target_user_ids=target_user_ids,
            message_count=len(messages),
        )

    return TeamsConversationMapping(
        source_conversation_id=conversation.source_id,
        target_kind=TeamsTargetKind.REVIEW_REQUIRED,
        confidence="blocked",
        reason="Conversation does not have enough mapped participants for a Teams chat or channel.",
        title=conversation.title,
        participant_count=len(participant_ids),
        message_count=len(messages),
        missing_identity_count=missing_identity_count,
        participant_source_ids=participant_ids,
        target_user_ids=target_user_ids,
    )


def _mapping(
    *,
    conversation: Conversation,
    target_kind: TeamsTargetKind,
    confidence: str,
    reason: str,
    participant_ids: tuple[str, ...],
    target_user_ids: tuple[str, ...],
    message_count: int,
) -> TeamsConversationMapping:
    return TeamsConversationMapping(
        source_conversation_id=conversation.source_id,
        target_kind=target_kind,
        confidence=confidence,
        reason=reason,
        title=conversation.title,
        participant_count=len(participant_ids),
        message_count=message_count,
        missing_identity_count=0,
        participant_source_ids=participant_ids,
        target_user_ids=target_user_ids,
    )


def _participant_ids_for_conversation(
    conversation_id: str,
    messages: list[Message],
    memberships: list[ConversationMembership],
) -> tuple[str, ...]:
    membership_participant_ids = {
        membership.participant_id
        for membership in memberships
        if membership.conversation_id == conversation_id and not membership.is_deleted
    }
    if membership_participant_ids:
        return tuple(sorted(membership_participant_ids))
    return tuple(sorted({message.author_id for message in messages if message.author_id}))


def _identity_template_entries(
    participants: list[Participant],
    existing_identity_map: Mapping[str, str],
    identity_map_reasons: Mapping[str, str],
) -> tuple[TeamsIdentityMappingEntry, ...]:
    by_source_id: dict[str, Participant] = {}
    for participant in participants:
        by_source_id.setdefault(participant.source_id, participant)
    entries: list[TeamsIdentityMappingEntry] = []
    for source_user_id in sorted(by_source_id):
        participant = by_source_id[source_user_id]
        entra_user_id = existing_identity_map.get(source_user_id, "").strip()
        entries.append(
            TeamsIdentityMappingEntry(
                source_user_id=source_user_id,
                entra_user_id=entra_user_id,
                display_name=participant.display_name,
                email=participant.email,
                status="mapped" if entra_user_id else "needs_review",
                reason=identity_map_reasons.get(
                    source_user_id,
                    "Pre-filled from existing identity map." if entra_user_id else "Provide Microsoft Entra user ID.",
                ),
            )
        )
    return tuple(entries)


def _load_entra_users_json(path: Path) -> tuple[EntraUser, ...]:
    payload = _read_json_file(path, "Entra users JSON")
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        raw_rows = payload.get("value", payload.get("users"))
        if not isinstance(raw_rows, list):
            raise ValueError("Entra users JSON must be a list or object with value/users list")
        rows = raw_rows
    else:
        raise ValueError("Entra users JSON must be an object or list")
    users: list[EntraUser] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"Entra users row {index} must be an object")
        users.append(_entra_user_from_mapping(row, index))
    return tuple(users)


def _read_json_file(path: Path, label: str) -> object:
    if path.exists() and not path.is_file():
        raise ValueError(f"{label} must be a file: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"{label} does not exist: {path}") from exc
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label} is not valid UTF-8: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is not valid JSON: {path}: {exc.msg}") from exc


def _load_entra_users_csv(path: Path) -> tuple[EntraUser, ...]:
    if path.exists() and not path.is_file():
        raise ValueError(f"Entra users CSV must be a file: {path}")
    users: list[EntraUser] = []
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            fieldnames = reader.fieldnames or ()
            if "id" not in fieldnames and "user_id" not in fieldnames:
                raise ValueError("Entra users CSV must include an id or user_id column")
            for index, row in enumerate(reader):
                users.append(_entra_user_from_mapping(row, index))
    except UnicodeDecodeError as exc:
        raise ValueError(f"Entra users CSV is not valid UTF-8: {path}") from exc
    return tuple(users)


def _entra_user_from_mapping(row: Mapping[str, object], index: int) -> EntraUser:
    user_id = _string_field(row, "id") or _string_field(row, "user_id")
    if not user_id:
        raise ValueError(f"Entra users row {index} missing id")
    return EntraUser(
        id=user_id,
        mail=_string_field(row, "mail"),
        user_principal_name=_string_field(row, "userPrincipalName") or _string_field(row, "user_principal_name"),
        proxy_addresses=_string_tuple_field(row, "proxyAddresses") or _string_tuple_field(row, "proxy_addresses"),
        other_mails=_string_tuple_field(row, "otherMails") or _string_tuple_field(row, "other_mails"),
    )


def _string_field(row: Mapping[str, object], key: str) -> str | None:
    value = row.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _string_tuple_field(row: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = row.get(key)
    if isinstance(value, list):
        return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())
    if isinstance(value, str) and value.strip():
        return tuple(item.strip() for item in value.replace(",", ";").split(";") if item.strip())
    return ()


def _entra_email_indexes(
    users: tuple[EntraUser, ...],
) -> tuple[tuple[str, dict[str, tuple[str, ...]]], ...]:
    mail_index: dict[str, list[str]] = {}
    upn_index: dict[str, list[str]] = {}
    alias_index: dict[str, list[str]] = {}
    for user in users:
        _add_email_index(mail_index, user.mail, user.id)
        _add_email_index(upn_index, user.user_principal_name, user.id)
        for value in user.proxy_addresses + user.other_mails:
            _add_email_index(alias_index, _normalize_proxy_address(value), user.id)
    return (
        ("mail", _freeze_index(mail_index)),
        ("userPrincipalName", _freeze_index(upn_index)),
        ("proxyAddress/otherMail", _freeze_index(alias_index)),
    )


def _add_email_index(index: dict[str, list[str]], value: str | None, user_id: str) -> None:
    key = _email_key(value)
    if not key:
        return
    bucket = index.setdefault(key, [])
    if user_id not in bucket:
        bucket.append(user_id)


def _freeze_index(index: dict[str, list[str]]) -> dict[str, tuple[str, ...]]:
    return {key: tuple(values) for key, values in index.items()}


def _normalize_proxy_address(value: str) -> str:
    if ":" in value:
        _, address = value.split(":", 1)
        return address
    return value


def _email_key(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip().casefold()
    if "@" not in normalized:
        return None
    return normalized


def _target_placeholder(target_kind: TeamsTargetKind) -> dict[str, str]:
    if target_kind in {TeamsTargetKind.ONE_ON_ONE_CHAT, TeamsTargetKind.GROUP_CHAT}:
        return {
            "chat_id": "",
        }
    if target_kind == TeamsTargetKind.TEAM_CHANNEL:
        return {
            "team_id": "",
            "channel_id": "",
        }
    return {
        "resolution": "",
    }


def _completed_target_for_row(
    *,
    target_kind: TeamsTargetKind,
    target: Mapping[object, object],
    row_index: int,
) -> dict[str, str]:
    if target_kind in {TeamsTargetKind.ONE_ON_ONE_CHAT, TeamsTargetKind.GROUP_CHAT}:
        _reject_unknown_target_fields(target, {"chat_id"}, row_index)
        return {
            "chat_id": _required_target_field(target, "chat_id", row_index),
        }
    if target_kind == TeamsTargetKind.TEAM_CHANNEL:
        _reject_unknown_target_fields(target, {"team_id", "channel_id"}, row_index)
        return {
            "team_id": _required_target_field(target, "team_id", row_index),
            "channel_id": _required_target_field(target, "channel_id", row_index),
        }
    _reject_unknown_target_fields(target, {"resolution"}, row_index)
    resolution = target.get("resolution")
    return {"resolution": resolution.strip() if isinstance(resolution, str) else ""}


def _reject_unknown_target_fields(
    target: Mapping[object, object],
    allowed_fields: set[str],
    row_index: int,
) -> None:
    unknown_fields = sorted(
        str(field)
        for field in target
        if not isinstance(field, str) or field not in allowed_fields
    )
    if unknown_fields:
        raise ValueError(
            f"Teams conversation map row {row_index} target has unsupported fields: "
            + ", ".join(unknown_fields)
        )


def _required_target_field(
    target: Mapping[object, object],
    field: str,
    row_index: int,
) -> str:
    value = target.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Teams conversation map row {row_index} missing target.{field}")
    return value.strip()


def _target_assignment_key(
    target_kind: TeamsTargetKind,
    target: Mapping[str, str],
) -> str | None:
    if target_kind in {TeamsTargetKind.ONE_ON_ONE_CHAT, TeamsTargetKind.GROUP_CHAT}:
        chat_id = target.get("chat_id", "").strip()
        if not chat_id:
            raise ValueError("Teams conversation target missing chat_id")
        return f"chat:{chat_id.casefold()}"
    if target_kind == TeamsTargetKind.TEAM_CHANNEL:
        team_id = target.get("team_id", "").strip()
        channel_id = target.get("channel_id", "").strip()
        if not team_id:
            raise ValueError("Teams conversation target missing team_id")
        if not channel_id:
            raise ValueError("Teams conversation target missing channel_id")
        return f"channel:{team_id.casefold()}:{channel_id.casefold()}"
    return None


def _attachment_to_plan_json(attachment: Attachment) -> dict[str, object]:
    return {
        "source_attachment_id": attachment.source_id,
        "filename": attachment.filename,
        "mime_type": attachment.mime_type,
        "size_bytes": attachment.size_bytes,
        "sha256": attachment.sha256,
        "local_path": attachment.local_path,
        "supported": False,
        "reason": "Teams historical import attachment import is not implemented yet.",
    }


def _unsupported_attachment_rows(
    messages: tuple[TeamsPreparedMessage, ...],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for message in messages:
        for attachment in message.attachments:
            rows.append(
                {
                    "source_message_id": message.source_message_id,
                    "source_conversation_id": message.source_conversation_id,
                    "source_attachment_id": attachment.get("source_attachment_id"),
                    "filename": attachment.get("filename"),
                    "local_path": attachment.get("local_path"),
                    "reason": attachment.get("reason"),
                }
            )
    return rows


def _ordered_messages_for_import(
    messages: list[Message],
    *,
    conversation_id: str,
) -> tuple[Message, ...]:
    messages_by_id: dict[str, Message] = {}
    for message in messages:
        if message.source_id in messages_by_id:
            raise ValueError(f"Conversation {conversation_id} duplicates message source_id: {message.source_id}")
        messages_by_id[message.source_id] = message

    children_by_parent: dict[str, list[Message]] = {}
    roots: list[Message] = []
    for message in messages:
        if message.parent_id is None:
            roots.append(message)
            continue
        if message.parent_id not in messages_by_id:
            raise ValueError(
                f"Message {message.source_id} references missing parent {message.parent_id}"
            )
        children_by_parent.setdefault(message.parent_id, []).append(message)

    for children in children_by_parent.values():
        children.sort(key=_message_sort_key)
    ordered: list[Message] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    # Iterative DFS — avoids Python recursion-limit problems on deep threads.
    # Stack entries: (message, returning).  returning=True is the post-order pop
    # used only to move the node from visiting → visited for cycle detection.
    stack: list[tuple[Message, bool]] = [
        (root, False)
        for root in reversed(sorted(roots, key=_message_sort_key))
    ]
    while stack:
        message, returning = stack.pop()
        if returning:
            visiting.discard(message.source_id)
            visited.add(message.source_id)
            continue
        if message.source_id in visited:
            continue
        if message.source_id in visiting:
            raise ValueError(f"Conversation {conversation_id} contains a message parent cycle")
        visiting.add(message.source_id)
        ordered.append(message)
        stack.append((message, True))
        for child in reversed(children_by_parent.get(message.source_id, [])):
            stack.append((child, False))

    if len(ordered) != len(messages):
        raise ValueError(f"Conversation {conversation_id} contains a message parent cycle")
    return tuple(ordered)


def _message_sort_key(message: Message) -> tuple[datetime, str]:
    return (_as_utc(message.created_at), message.source_id)



def _graph_datetime(value: datetime) -> str:
    utc_value = _as_utc(value)
    milliseconds = utc_value.microsecond // 1000
    return utc_value.strftime("%Y-%m-%dT%H:%M:%S") + f".{milliseconds:03d}Z"


def _audit_datetime(value: datetime) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")


