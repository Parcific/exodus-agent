from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timezone
from hashlib import sha256
from pathlib import Path

from exodus_agent.archive import Archive, _safe_filename
from exodus_agent.model import Attachment, Conversation, Message, Participant


@dataclass(frozen=True)
class TelegramPackageResult:
    package_root: Path
    conversations: int
    messages: int


@dataclass(frozen=True)
class TelegramVerificationResult:
    ok: bool
    report_path: Path
    conversations_expected: int
    conversations_found: int
    messages_expected: int
    messages_found: int
    issues: tuple[str, ...]


@dataclass(frozen=True)
class TelegramImportPlanResult:
    plan_path: Path
    ready: bool
    conversations: int
    messages: int
    media: int
    issues: tuple[str, ...]


@dataclass(frozen=True)
class TelegramDestinationMapTemplateResult:
    path: Path
    conversations: int


def write_telegram_destination_map_template(
    *,
    archive: Archive,
    output_path: Path,
    overwrite: bool = False,
) -> TelegramDestinationMapTemplateResult:
    archive_manifest = archive.read_manifest()
    if archive_manifest.get("schema_version") != 1:
        raise ValueError("Unsupported archive schema version")
    conversations = archive.read_conversations()
    if not conversations:
        raise ValueError("Archive contains no conversations to map")
    if output_path.exists() and not output_path.is_file():
        raise ValueError(f"Telegram destination map output path must be a file: {output_path}")
    if output_path.exists() and not overwrite:
        raise FileExistsError(output_path)

    template = {}
    for conversation in sorted(conversations, key=lambda item: (item.title or "", item.source_id)):
        template[conversation.source_id] = {
            "peer": "",
            "title": conversation.title,
            "kind": conversation.kind.value,
            "message_count": len(archive.read_messages(conversation.source_id)),
        }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(template, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return TelegramDestinationMapTemplateResult(path=output_path, conversations=len(conversations))


def write_telegram_staging_package(*, archive: Archive, output_root: Path) -> TelegramPackageResult:
    archive_manifest = archive.read_manifest()
    if archive_manifest.get("schema_version") != 1:
        raise ValueError("Unsupported archive schema version")
    if output_root.exists() and not output_root.is_dir():
        raise ValueError(f"Telegram package output root must be a directory: {output_root}")
    output_root.mkdir(parents=True, exist_ok=True)
    conversations = archive.read_conversations()
    if not conversations:
        raise ValueError("Archive contains no conversations to package")
    participants = {participant.source_id: participant for participant in archive.read_participants()}
    total_messages = 0
    conversation_entries: list[dict[str, object]] = []

    for conversation in conversations:
        messages = archive.read_messages(conversation.source_id)
        total_messages += len(messages)
        conversation_dir = output_root / _safe_filename(conversation.source_id)
        conversation_dir.mkdir(parents=True, exist_ok=True)
        transcript_path = conversation_dir / "messages.txt"
        transcript_path.write_text(
            _render_transcript(conversation, messages, participants),
            encoding="utf-8",
        )
        conversation_manifest = {
            "source_conversation_id": conversation.source_id,
            "title": conversation.title,
            "kind": conversation.kind.value,
            "message_count": len(messages),
            "transcript": transcript_path.name,
            "format": "exodus.telegram.staging.v1",
            "note": "Staging transcript for Telegram MTProto history-import preparation.",
        }
        (conversation_dir / "manifest.json").write_text(
            json.dumps(conversation_manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        conversation_entries.append(
            {
                "source_conversation_id": conversation.source_id,
                "title": conversation.title,
                "message_count": len(messages),
                "path": conversation_dir.name,
            }
        )

    root_manifest = {
        "format": "exodus.telegram.package.v1",
        "source_archive": {
            "name": archive_manifest.get("name"),
            "source_kind": archive_manifest.get("source_kind"),
            "target_kind": archive_manifest.get("target_kind"),
            "schema_version": archive_manifest.get("schema_version"),
        },
        "conversation_count": len(conversations),
        "message_count": total_messages,
        "conversations": conversation_entries,
    }
    (output_root / "manifest.json").write_text(
        json.dumps(root_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return TelegramPackageResult(
        package_root=output_root,
        conversations=len(conversations),
        messages=total_messages,
    )


def verify_telegram_staging_package(
    *,
    archive: Archive,
    package_root: Path,
) -> TelegramVerificationResult:
    archive_manifest = archive.read_manifest()
    conversations = archive.read_conversations()
    expected_messages_by_conversation = {
        conversation.source_id: len(archive.read_messages(conversation.source_id))
        for conversation in conversations
    }
    expected_messages = sum(expected_messages_by_conversation.values())
    issues: list[str] = []
    package_manifest = _read_json(package_root / "manifest.json", issues)

    package_conversations = package_manifest.get("conversations", [])
    if not isinstance(package_conversations, list):
        issues.append("Package manifest conversations must be a list")
        package_conversations = []

    if package_manifest.get("format") != "exodus.telegram.package.v1":
        issues.append("Package manifest has unsupported format")
    if package_manifest.get("conversation_count") != len(conversations):
        issues.append(
            f"Conversation count mismatch: expected {len(conversations)}, "
            f"found {package_manifest.get('conversation_count')}"
        )
    if package_manifest.get("message_count") != expected_messages:
        issues.append(
            f"Message count mismatch: expected {expected_messages}, "
            f"found {package_manifest.get('message_count')}"
        )

    found_conversation_ids: set[str] = set()
    found_messages = 0
    for entry in package_conversations:
        if not isinstance(entry, dict):
            issues.append("Package manifest contains non-object conversation entry")
            continue
        source_id = entry.get("source_conversation_id")
        path = entry.get("path")
        if not isinstance(source_id, str) or not isinstance(path, str):
            issues.append("Conversation entry missing source_conversation_id or path")
            continue
        if source_id in found_conversation_ids:
            issues.append(f"Package manifest duplicates conversation: {source_id}")
        found_conversation_ids.add(source_id)
        expected_count = expected_messages_by_conversation.get(source_id)
        if expected_count is None:
            issues.append(f"Package contains unknown conversation: {source_id}")
            expected_count = 0
        conversation_dir = _resolve_package_path(package_root, path, issues)
        if conversation_dir is None:
            continue
        conversation_manifest = _read_json(conversation_dir / "manifest.json", issues)
        if conversation_manifest.get("format") != "exodus.telegram.staging.v1":
            issues.append(f"Conversation {source_id} manifest has unsupported format")
        manifest_source_id = conversation_manifest.get("source_conversation_id")
        if manifest_source_id != source_id:
            issues.append(
                f"Conversation {source_id} manifest source_conversation_id mismatch: "
                f"found {manifest_source_id!r}"
            )
        transcript_value = conversation_manifest.get("transcript", "messages.txt")
        if not isinstance(transcript_value, str):
            issues.append(f"Conversation {source_id} manifest transcript must be a string")
            transcript_path = conversation_dir / "messages.txt"
        else:
            transcript_path = _resolve_package_path(conversation_dir, transcript_value, issues)
            if transcript_path is None:
                continue
        transcript_count = _transcript_message_count(transcript_path, issues)
        found_messages += transcript_count
        manifest_count = conversation_manifest.get("message_count")
        if manifest_count != expected_count:
            issues.append(
                f"Conversation {source_id} manifest message_count mismatch: "
                f"expected {expected_count}, found {manifest_count}"
            )
        if transcript_count != expected_count:
            issues.append(
                f"Conversation {source_id} transcript line count mismatch: "
                f"expected {expected_count}, found {transcript_count}"
            )

    expected_conversation_ids = {conversation.source_id for conversation in conversations}
    missing = expected_conversation_ids - found_conversation_ids
    for source_id in sorted(missing):
        issues.append(f"Package missing conversation: {source_id}")
    if found_messages != expected_messages:
        issues.append(
            f"Transcript message total mismatch: expected {expected_messages}, "
            f"found {found_messages}"
        )

    report = {
        "format": "exodus.telegram.verification.v1",
        "ok": not issues,
        "source_archive": {
            "name": archive_manifest.get("name"),
            "source_kind": archive_manifest.get("source_kind"),
            "target_kind": archive_manifest.get("target_kind"),
            "schema_version": archive_manifest.get("schema_version"),
        },
        "package_root": str(package_root),
        "conversations_expected": len(conversations),
        "conversations_found": len(found_conversation_ids),
        "messages_expected": expected_messages,
        "messages_found": found_messages,
        "issues": issues,
    }
    report_path = archive.root / "reports" / "telegram-package-verification.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return TelegramVerificationResult(
        ok=not issues,
        report_path=report_path,
        conversations_expected=len(conversations),
        conversations_found=len(found_conversation_ids),
        messages_expected=expected_messages,
        messages_found=found_messages,
        issues=tuple(issues),
    )


def write_telegram_import_plan(
    *,
    archive: Archive,
    package_root: Path,
    destination_map: object | None = None,
) -> TelegramImportPlanResult:
    verification = verify_telegram_staging_package(archive=archive, package_root=package_root)
    conversations = archive.read_conversations()
    issues = list(verification.issues)
    destination_map = _validated_destination_map(destination_map or {}, issues)
    operations: list[dict[str, object]] = []
    total_messages = 0
    total_media = 0
    known_conversation_ids = {conversation.source_id for conversation in conversations}

    for source_id in sorted(set(destination_map) - known_conversation_ids):
        issues.append(f"Destination map contains unknown conversation: {source_id}")
    _append_duplicate_peer_issues(destination_map, known_conversation_ids, issues)

    for conversation in conversations:
        messages = archive.read_messages(conversation.source_id)
        media_count = sum(len(message.attachments) for message in messages)
        total_messages += len(messages)
        total_media += media_count
        peer = destination_map.get(conversation.source_id)
        if not peer:
            issues.append(f"Missing Telegram destination peer for conversation: {conversation.source_id}")
        package_dir = _safe_filename(conversation.source_id)
        transcript = f"{package_dir}/messages.txt"
        transcript_path = (package_root / transcript).resolve()
        operations.extend(
            [
                {
                    "method": "messages.checkHistoryImport",
                    "conversation_id": conversation.source_id,
                    "import_head_file": transcript,
                    "import_head_path": str(transcript_path),
                    "import_head_lines": 100,
                },
                {
                    "method": "messages.checkHistoryImportPeer",
                    "conversation_id": conversation.source_id,
                    "peer": peer,
                },
                {
                    "method": "messages.initHistoryImport",
                    "conversation_id": conversation.source_id,
                    "peer": peer,
                    "file": transcript,
                    "file_path": str(transcript_path),
                    "media_count": media_count,
                    "captures": ("import_id",),
                },
            ]
        )
        for message in messages:
            for attachment in message.attachments:
                if not attachment.local_path:
                    issues.append(
                        "Attachment is missing local_path for Telegram media upload: "
                        f"conversation={conversation.source_id} message={message.source_id} "
                        f"attachment={attachment.source_id}"
                    )
                    attachment_path = None
                else:
                    try:
                        attachment_path = archive.resolve_path(attachment.local_path)
                    except ValueError as exc:
                        issues.append(
                            "Attachment local_path is unsafe for Telegram media upload: "
                            f"conversation={conversation.source_id} message={message.source_id} "
                            f"attachment={attachment.source_id} path={attachment.local_path}: {exc}"
                        )
                        attachment_path = None
                    if attachment_path is not None and not attachment_path.exists():
                        issues.append(
                            "Attachment local_path does not exist for Telegram media upload: "
                            f"conversation={conversation.source_id} message={message.source_id} "
                            f"attachment={attachment.source_id} path={attachment.local_path}"
                        )
                    elif attachment_path is not None:
                        _append_attachment_integrity_issues(
                            attachment_path=attachment_path,
                            conversation_id=conversation.source_id,
                            message_id=message.source_id,
                            attachment=attachment,
                            issues=issues,
                        )
                operations.append(
                    {
                        "method": "messages.uploadImportedMedia",
                        "conversation_id": conversation.source_id,
                        "message_id": message.source_id,
                        "peer": peer,
                        "requires_import_id": True,
                        "file_name": attachment.filename,
                        "mime_type": attachment.mime_type,
                        "local_path": attachment.local_path,
                        "file_path": str(attachment_path) if attachment_path else None,
                        "source_attachment_id": attachment.source_id,
                    }
                )
        operations.append(
            {
                "method": "messages.startHistoryImport",
                "conversation_id": conversation.source_id,
                "peer": peer,
                "requires_import_id": True,
            }
        )

    ready = not issues
    plan = {
        "format": "exodus.telegram.mtproto.import_plan.v1",
        "ready": ready,
        "package_root": str(package_root),
        "conversations": len(conversations),
        "messages": total_messages,
        "media": total_media,
        "issues": issues,
        "operations": operations,
    }
    plan_path = package_root / "import-plan.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    if plan_path.exists() and not plan_path.is_file():
        raise ValueError(f"Telegram import plan output path must be a file: {plan_path}")
    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return TelegramImportPlanResult(
        plan_path=plan_path,
        ready=ready,
        conversations=len(conversations),
        messages=total_messages,
        media=total_media,
        issues=tuple(issues),
    )


def _append_duplicate_peer_issues(
    destination_map: dict[str, str],
    known_conversation_ids: set[str],
    issues: list[str],
) -> None:
    by_peer: dict[str, list[str]] = {}
    for source_id, peer in destination_map.items():
        if source_id not in known_conversation_ids or not peer:
            continue
        by_peer.setdefault(peer.casefold(), []).append(source_id)
    for peer_key, source_ids in sorted(by_peer.items()):
        if len(source_ids) > 1:
            issues.append(
                "Telegram destination peer is mapped from multiple conversations: "
                f"peer={peer_key} conversations={', '.join(sorted(source_ids))}"
            )


def _validated_destination_map(value: object, issues: list[str]) -> dict[str, str]:
    if not isinstance(value, dict):
        issues.append("Destination map must be an object")
        return {}
    destination_map: dict[str, str] = {}
    for key, peer in value.items():
        if not isinstance(key, str) or not key.strip():
            issues.append("Destination map contains a non-string or empty conversation id")
            continue
        key = key.strip()
        if key in destination_map:
            issues.append(f"Destination map duplicates conversation id: {key}")
            continue
        if not isinstance(peer, str) or not peer.strip():
            issues.append(f"Destination map entry must include a non-empty Telegram peer: {key}")
            continue
        destination_map[key] = peer.strip()
    return destination_map


def _append_attachment_integrity_issues(
    *,
    attachment_path: Path,
    conversation_id: str,
    message_id: str,
    attachment: Attachment,
    issues: list[str],
) -> None:
    if not attachment_path.is_file():
        issues.append(
            "Attachment local_path is not a file for Telegram media upload: "
            f"conversation={conversation_id} message={message_id} "
            f"attachment={attachment.source_id} path={attachment.local_path}"
        )
        return
    if attachment.size_bytes is not None:
        actual_size = attachment_path.stat().st_size
        if actual_size != attachment.size_bytes:
            issues.append(
                "Attachment size mismatch for Telegram media upload: "
                f"conversation={conversation_id} message={message_id} attachment={attachment.source_id} "
                f"expected={attachment.size_bytes} found={actual_size}"
            )
    if attachment.sha256:
        digest = sha256(attachment_path.read_bytes()).hexdigest()
        if digest.casefold() != attachment.sha256.casefold():
            issues.append(
                "Attachment sha256 mismatch for Telegram media upload: "
                f"conversation={conversation_id} message={message_id} attachment={attachment.source_id}"
            )


def _render_transcript(
    conversation: Conversation,
    messages: list[Message],
    participants: dict[str, Participant],
) -> str:
    lines = [
        f"Chat: {conversation.title or conversation.source_id}",
        f"Source conversation: {conversation.source_id}",
        "",
    ]
    for message in sorted(messages, key=lambda item: item.created_at):
        author = _author_label(message, participants)
        timestamp = message.created_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        body = message.text or message.markdown or message.html or ""
        lines.append(f"MSG [{timestamp}] {author}: {body}")
        for attachment in message.attachments:
            lines.append(f"ATTACH [{timestamp}] {author}: {attachment.filename}")
    lines.append("")
    return "\n".join(lines)


def _author_label(message: Message, participants: dict[str, Participant]) -> str:
    if message.author_id is None:
        return "Unknown"
    participant = participants.get(message.author_id)
    if participant is None:
        return message.author_id
    return participant.display_name


def _read_json(path: Path, issues: list[str]) -> dict[str, object]:
    if not path.exists():
        issues.append(f"Missing JSON file: {path}")
        return {}
    if not path.is_file():
        issues.append(f"JSON file must be a file: {path}")
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError:
        issues.append(f"JSON file is not valid UTF-8: {path}")
        return {}
    except json.JSONDecodeError as exc:
        issues.append(f"Invalid JSON file: {path}: {exc.msg}")
        return {}
    if not isinstance(payload, dict):
        issues.append(f"JSON file must contain an object: {path}")
        return {}
    return payload


def _resolve_package_path(root: Path, relative_path: str, issues: list[str]) -> Path | None:
    path = Path(relative_path)
    if path.is_absolute() or ".." in path.parts:
        issues.append(f"Package path must stay within package root: {relative_path}")
        return None
    resolved_root = root.resolve()
    resolved_path = (root / path).resolve()
    if not resolved_path.is_relative_to(resolved_root):
        issues.append(f"Package path must stay within package root: {relative_path}")
        return None
    return resolved_path


def _transcript_message_count(path: Path, issues: list[str]) -> int:
    if not path.exists():
        issues.append(f"Missing transcript file: {path}")
        return 0
    if not path.is_file():
        issues.append(f"Transcript file must be a file: {path}")
        return 0
    count = 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        issues.append(f"Transcript file is not valid UTF-8: {path}")
        return 0
    for line in lines:
        if line.startswith("MSG [") and "] " in line:
            count += 1
    return count
