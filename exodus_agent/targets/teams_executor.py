from __future__ import annotations

import json
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from exodus_agent.job import JobEvent, JobEventKind, JobStore
from exodus_agent.redaction import redact_sensitive, redact_text

from .teams_shared import _truncate_to_millisecond, _timestamp_adjustment_reason


class TeamsMessageAdapter(Protocol):
    def import_message(self, message: Mapping[str, object]) -> dict[str, object]:
        """Import one prepared Teams message and return adapter metadata."""


@dataclass(frozen=True)
class DryRunTeamsAdapter:
    def import_message(self, message: Mapping[str, object]) -> dict[str, object]:
        source_message_id = message.get("source_message_id")
        if not isinstance(source_message_id, str) or not source_message_id:
            raise ValueError("Prepared Teams message missing source_message_id")
        return {
            "dry_run": True,
            "teams_message_id": f"dry-run:{source_message_id}",
        }


@dataclass(frozen=True)
class TeamsPlanExecutionResult:
    ok: bool
    messages_total: int
    messages_imported: int
    messages_skipped: int
    message_map_path: Path
    issues: tuple[str, ...]


@dataclass(frozen=True)
class TeamsImportVerificationResult:
    ok: bool
    report_path: Path
    messages_expected: int
    messages_mapped: int
    extra_mappings: int
    unsupported_attachments: int
    issues: tuple[str, ...]


def execute_teams_import_plan(
    *,
    plan_path: Path,
    message_map_path: Path,
    job_store: JobStore,
    job_id: str,
    adapter: TeamsMessageAdapter | None = None,
) -> TeamsPlanExecutionResult:
    adapter = adapter or DryRunTeamsAdapter()
    issues: list[str] = []
    prior_events = job_store.read_events()
    if _already_completed(prior_events):
        issues.append("Teams import job already completed")

    plan = _read_json_object_or_issue(plan_path, "Teams import plan", issues)
    messages = _validated_plan_messages(plan, issues)
    message_map = _load_message_map(message_map_path, issues)
    _validate_message_map_matches_plan(message_map, messages, issues)

    if issues:
        _append_event(job_store, job_id, JobEventKind.ERROR, "teams_import", issues=issues)
        return TeamsPlanExecutionResult(
            ok=False,
            messages_total=len(messages),
            messages_imported=0,
            messages_skipped=0,
            message_map_path=message_map_path,
            issues=tuple(issues),
        )

    imported = 0
    skipped = 0
    _append_event(
        job_store,
        job_id,
        JobEventKind.PHASE_STARTED,
        "teams_import",
        messages_total=len(messages),
        plan_path=str(plan_path),
        message_map_path=str(message_map_path),
    )
    for message in messages:
        source_message_id = message["source_message_id"]
        existing = message_map.get(source_message_id)
        if existing:
            skipped += 1
            _append_event(
                job_store,
                job_id,
                JobEventKind.PHASE_COMPLETED,
                "teams_import_message_skipped",
                source_message_id=source_message_id,
                teams_message_id=existing,
            )
            continue
        try:
            result = adapter.import_message(message)
        except Exception as exc:  # noqa: BLE001 - adapter boundary must capture failures.
            issues.append(f"Message {source_message_id} failed: {redact_text(exc)}")
            break
        teams_message_id = result.get("teams_message_id")
        if not isinstance(teams_message_id, str) or not teams_message_id.strip():
            issues.append(f"Message {source_message_id} adapter did not return teams_message_id")
            break
        message_map[source_message_id] = teams_message_id.strip()
        _write_message_map(message_map_path, message_map)
        imported += 1
        _append_event(
            job_store,
            job_id,
            JobEventKind.PHASE_COMPLETED,
            "teams_import_message",
            source_message_id=source_message_id,
            teams_message_id=teams_message_id.strip(),
            adapter_result=redact_sensitive(result),
        )

    if issues:
        _append_event(job_store, job_id, JobEventKind.ERROR, "teams_import", issues=issues)
    else:
        _append_event(
            job_store,
            job_id,
            JobEventKind.PHASE_COMPLETED,
            "teams_import",
            messages_imported=imported,
            messages_skipped=skipped,
        )
    return TeamsPlanExecutionResult(
        ok=not issues,
        messages_total=len(messages),
        messages_imported=imported,
        messages_skipped=skipped,
        message_map_path=message_map_path,
        issues=tuple(issues),
    )


def verify_teams_import(
    *,
    plan_path: Path,
    message_map_path: Path,
    report_path: Path,
) -> TeamsImportVerificationResult:
    if report_path.exists() and not report_path.is_file():
        raise ValueError(f"Teams import verification report path must be a file: {report_path}")
    issues: list[str] = []
    plan = _read_json_object_or_issue(plan_path, "Teams import plan", issues)
    messages = _validated_plan_messages(plan, issues)
    message_map = _load_message_map(message_map_path, issues, require_exists=True)
    planned_source_ids = {
        message["source_message_id"]
        for message in messages
        if isinstance(message.get("source_message_id"), str)
    }
    unsupported_attachments = _unsupported_attachments_from_plan(
        plan,
        messages,
        issues,
        planned_source_ids=planned_source_ids,
    )
    mapped_source_ids = set(message_map)
    missing_source_ids = sorted(planned_source_ids - mapped_source_ids)
    extra_source_ids = sorted(mapped_source_ids - planned_source_ids)

    for source_message_id in missing_source_ids:
        issues.append(f"Teams message map missing source_message_id: {source_message_id}")
    for source_message_id in extra_source_ids:
        issues.append(f"Teams message map contains unplanned source_message_id: {source_message_id}")

    report = {
        "format": "exodus.teams.import_verification.v1",
        "ok": not issues,
        "plan_path": str(plan_path),
        "message_map_path": str(message_map_path),
        "messages_expected": len(planned_source_ids),
        "messages_mapped": len(planned_source_ids & mapped_source_ids),
        "extra_mappings": len(extra_source_ids),
        "unsupported_attachments": len(unsupported_attachments),
        "unsupported_attachment_rows": unsupported_attachments,
        "missing_source_message_ids": missing_source_ids,
        "extra_source_message_ids": extra_source_ids,
        "issues": issues,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return TeamsImportVerificationResult(
        ok=not issues,
        report_path=report_path,
        messages_expected=len(planned_source_ids),
        messages_mapped=len(planned_source_ids & mapped_source_ids),
        extra_mappings=len(extra_source_ids),
        unsupported_attachments=len(unsupported_attachments),
        issues=tuple(issues),
    )


def _validated_plan_messages(
    plan: dict[str, object],
    issues: list[str],
) -> list[dict[str, object]]:
    if plan.get("format") != "exodus.teams.import_plan.v1":
        issues.append("Teams import plan has unsupported format")
    raw_messages = plan.get("messages", [])
    if not isinstance(raw_messages, list):
        issues.append("Teams import plan messages must be a list")
        return []

    messages: list[dict[str, object]] = []
    source_ids: set[str] = set()
    import_orders: set[int] = set()
    seen_by_source_id: set[str] = set()
    for index, raw_message in enumerate(raw_messages):
        if not isinstance(raw_message, dict):
            issues.append(f"Teams import plan message {index} must be an object")
            continue
        message = dict(raw_message)
        source_message_id = message.get("source_message_id")
        if not isinstance(source_message_id, str) or not source_message_id.strip():
            issues.append(f"Teams import plan message {index} missing source_message_id")
            continue
        source_message_id = source_message_id.strip()
        message["source_message_id"] = source_message_id
        if source_message_id in source_ids:
            issues.append(f"Teams import plan duplicates source_message_id: {source_message_id}")
            continue
        source_ids.add(source_message_id)

        import_order = message.get("import_order")
        if not _is_nonnegative_int(import_order):
            issues.append(f"Teams import plan message {source_message_id} missing import_order")
            continue
        _validate_prepared_message_contract(message, source_message_id, issues)
        if import_order in import_orders:
            issues.append(f"Teams import plan duplicates import_order: {import_order}")
            continue
        import_orders.add(import_order)
        messages.append(message)

    messages.sort(key=lambda message: message["import_order"])
    seen_created_at_by_source_id: dict[str, datetime] = {}
    for expected_order, message in enumerate(messages):
        if message["import_order"] != expected_order:
            issues.append("Teams import plan import_order values must be contiguous from 0")
            break
        parent_id = message.get("parent_source_message_id")
        if isinstance(parent_id, str) and parent_id and parent_id not in seen_by_source_id:
            issues.append(
                "Teams import plan emits reply before parent: "
                f"{message['source_message_id']} -> {parent_id}"
            )
            break
        raw_created_at = message.get("createdDateTime")
        created_at = _parse_graph_created_datetime(raw_created_at) if isinstance(raw_created_at, str) else None
        if isinstance(parent_id, str) and parent_id:
            parent_created_at = seen_created_at_by_source_id.get(parent_id)
            if created_at is not None and parent_created_at is not None and created_at <= parent_created_at:
                issues.append(
                    "Teams import plan emits reply createdDateTime before or equal to parent: "
                    f"{message['source_message_id']} -> {parent_id}"
                )
                break
        seen_by_source_id.add(str(message["source_message_id"]))
        if created_at is not None:
            seen_created_at_by_source_id[str(message["source_message_id"])] = created_at
    _validate_unique_timestamps_by_target(messages, issues)
    return messages


def _validate_prepared_message_contract(
    message: MutableMapping[str, object],
    source_message_id: str,
    issues: list[str],
) -> None:
    for field in (
        "source_conversation_id",
        "target_kind",
        "author_user_id",
        "createdDateTime",
        "original_created_at",
    ):
        value = message.get(field)
        if not isinstance(value, str) or not value.strip():
            issues.append(f"Teams import plan message {source_message_id} missing {field}")
        elif field in {"source_conversation_id", "target_kind", "author_user_id"}:
            message[field] = value.strip()
    target = message.get("target")
    if not isinstance(target, dict):
        issues.append(f"Teams import plan message {source_message_id} target must be an object")
    else:
        _validate_plan_target(message, target, source_message_id, issues)
    content = message.get("content")
    if not isinstance(content, str):
        issues.append(f"Teams import plan message {source_message_id} content must be a string")
    attachments = message.get("attachments", [])
    if not isinstance(attachments, list):
        issues.append(f"Teams import plan message {source_message_id} attachments must be a list")
    else:
        for index, attachment in enumerate(attachments):
            if not isinstance(attachment, dict):
                issues.append(
                    f"Teams import plan message {source_message_id} attachment row {index} must be an object"
                )
                continue
            _validate_plan_attachment(attachment, source_message_id, index, issues)
    timestamp_adjusted = message.get("timestamp_adjusted")
    if not isinstance(timestamp_adjusted, bool):
        issues.append(f"Teams import plan message {source_message_id} timestamp_adjusted must be a boolean")
    timestamp_adjustment_ms = message.get("timestamp_adjustment_ms")
    if not _is_nonnegative_int(timestamp_adjustment_ms):
        issues.append(
            f"Teams import plan message {source_message_id} timestamp_adjustment_ms must be a non-negative integer"
        )
    reason = message.get("timestamp_adjustment_reason")
    if reason is not None and (not isinstance(reason, str) or not reason):
        issues.append(
            f"Teams import plan message {source_message_id} timestamp_adjustment_reason must be a string or null"
        )
    _validate_timestamp_audit_fields(message, source_message_id, issues)
    parent_id = message.get("parent_source_message_id")
    if parent_id is not None and not isinstance(parent_id, str):
        issues.append(f"Teams import plan message {source_message_id} parent_source_message_id must be a string or null")
    elif isinstance(parent_id, str):
        if not parent_id.strip():
            issues.append(
                f"Teams import plan message {source_message_id} parent_source_message_id must be a string or null"
            )
        else:
            message["parent_source_message_id"] = parent_id.strip()


def _validate_plan_attachment(
    attachment: Mapping[str, object],
    source_message_id: str,
    row_index: int,
    issues: list[str],
) -> None:
    for field in ("source_attachment_id", "filename"):
        value = attachment.get(field)
        if not isinstance(value, str) or not value.strip():
            issues.append(
                f"Teams import plan message {source_message_id} attachment row {row_index} missing {field}"
            )
    for field in ("mime_type", "sha256", "local_path", "reason"):
        value = attachment.get(field)
        if value is not None and not isinstance(value, str):
            issues.append(
                f"Teams import plan message {source_message_id} attachment row {row_index} field {field} "
                "must be a string or null"
            )
    size_bytes = attachment.get("size_bytes")
    if size_bytes is not None and not _is_nonnegative_int(size_bytes):
        issues.append(
            f"Teams import plan message {source_message_id} attachment row {row_index} field size_bytes "
            "must be a non-negative integer or null"
        )
    supported = attachment.get("supported")
    if not isinstance(supported, bool):
        issues.append(
            f"Teams import plan message {source_message_id} attachment row {row_index} supported must be a boolean"
        )
    elif supported is False:
        reason = attachment.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            issues.append(
                f"Teams import plan message {source_message_id} attachment row {row_index} missing reason"
            )


def _is_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _validate_message_map_matches_plan(
    message_map: Mapping[str, str],
    messages: list[dict[str, object]],
    issues: list[str],
) -> None:
    planned_source_ids = {
        message["source_message_id"]
        for message in messages
        if isinstance(message.get("source_message_id"), str)
    }
    for source_message_id in sorted(set(message_map) - planned_source_ids):
        issues.append(f"Teams message map contains unplanned source_message_id: {source_message_id}")


def _validate_timestamp_audit_fields(
    message: Mapping[str, object],
    source_message_id: str,
    issues: list[str],
) -> None:
    raw_created_at = message.get("createdDateTime")
    raw_original_created_at = message.get("original_created_at")
    if not isinstance(raw_created_at, str) or not isinstance(raw_original_created_at, str):
        return
    created_at = _parse_graph_created_datetime(raw_created_at)
    original_created_at = _parse_graph_datetime(raw_original_created_at)
    if created_at is None or original_created_at is None:
        if created_at is None:
            issues.append(
                f"Teams import plan message {source_message_id} has invalid createdDateTime: "
                f"{raw_created_at}"
            )
        if original_created_at is None:
            issues.append(
                f"Teams import plan message {source_message_id} has invalid original_created_at: "
                f"{raw_original_created_at}"
            )
        return

    timestamp_adjusted = message.get("timestamp_adjusted")
    timestamp_adjustment_ms = message.get("timestamp_adjustment_ms")
    reason = message.get("timestamp_adjustment_reason")
    if not isinstance(timestamp_adjusted, bool) or not _is_nonnegative_int(timestamp_adjustment_ms):
        return
    if reason is not None and (not isinstance(reason, str) or not reason):
        return

    original_truncated = _truncate_to_millisecond(original_created_at)
    computed_adjustment_ms = int((created_at - original_truncated).total_seconds() * 1000)
    if computed_adjustment_ms < 0:
        issues.append(
            f"Teams import plan message {source_message_id} createdDateTime is before original_created_at"
        )
        return
    if timestamp_adjustment_ms != computed_adjustment_ms:
        issues.append(
            f"Teams import plan message {source_message_id} timestamp_adjustment_ms "
            f"{timestamp_adjustment_ms} does not match createdDateTime/original_created_at delta "
            f"{computed_adjustment_ms}"
        )
    expected_reason = _timestamp_adjustment_reason(
        precision_adjusted=original_created_at != original_truncated,
        collision_adjustment_ms=computed_adjustment_ms,
    )
    if timestamp_adjusted != (expected_reason is not None):
        issues.append(
            f"Teams import plan message {source_message_id} timestamp_adjusted does not match timestamp audit"
        )
    if reason != expected_reason:
        issues.append(
            f"Teams import plan message {source_message_id} timestamp_adjustment_reason "
            f"does not match timestamp audit"
        )


def _validate_unique_timestamps_by_target(
    messages: list[dict[str, object]],
    issues: list[str],
) -> None:
    timestamps_by_target: dict[str, dict[datetime, str]] = {}
    for message in messages:
        source_message_id = str(message["source_message_id"])
        raw_created_at = message.get("createdDateTime")
        if not isinstance(raw_created_at, str):
            continue
        created_at = _parse_graph_created_datetime(raw_created_at)
        if created_at is None:
            issues.append(
                f"Teams import plan message {source_message_id} has invalid createdDateTime: "
                f"{raw_created_at}"
            )
            continue
        target_key = _message_target_key(message)
        seen_for_target = timestamps_by_target.setdefault(target_key, {})
        existing_source_id = seen_for_target.get(created_at)
        if existing_source_id is not None:
            issues.append(
                "Teams import plan duplicates createdDateTime in the same target: "
                f"{raw_created_at} for {existing_source_id}, {source_message_id}"
            )
            continue
        seen_for_target[created_at] = source_message_id


def _validate_plan_target(
    message: Mapping[str, object],
    target: Mapping[object, object],
    source_message_id: str,
    issues: list[str],
) -> None:
    target_kind = message.get("target_kind")
    if target_kind not in {"one_on_one_chat", "group_chat", "team_channel"}:
        issues.append(f"Teams import plan message {source_message_id} has unsupported target_kind")
        return
    if target_kind in {"one_on_one_chat", "group_chat"}:
        _validate_exact_target_fields(
            target,
            {"chat_id"},
            source_message_id,
            issues,
        )
        _validate_target_string(target, "chat_id", source_message_id, issues)
        return
    _validate_exact_target_fields(
        target,
        {"team_id", "channel_id"},
        source_message_id,
        issues,
    )
    _validate_target_string(target, "team_id", source_message_id, issues)
    _validate_target_string(target, "channel_id", source_message_id, issues)


def _validate_exact_target_fields(
    target: Mapping[object, object],
    allowed_fields: set[str],
    source_message_id: str,
    issues: list[str],
) -> None:
    unknown_fields = sorted(
        str(field)
        for field in target
        if not isinstance(field, str) or field not in allowed_fields
    )
    if unknown_fields:
        issues.append(
            f"Teams import plan message {source_message_id} target has unsupported fields: "
            + ", ".join(unknown_fields)
        )


def _validate_target_string(
    target: Mapping[object, object],
    field: str,
    source_message_id: str,
    issues: list[str],
) -> None:
    value = target.get(field)
    if not isinstance(value, str) or not value.strip():
        issues.append(f"Teams import plan message {source_message_id} missing target.{field}")


def _parse_graph_datetime(value: str) -> datetime | None:
    if not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _parse_graph_created_datetime(value: str) -> datetime | None:
    parsed = _parse_graph_datetime(value)
    if parsed is None or not _has_exact_millisecond_precision(value):
        return None
    return parsed


def _has_exact_millisecond_precision(value: str) -> bool:
    if not value.endswith("Z"):
        return False
    timestamp = value[:-1]
    if "." not in timestamp:
        return False
    fraction = timestamp.rsplit(".", 1)[1]
    return len(fraction) == 3 and fraction.isdigit()



def _message_target_key(message: Mapping[str, object]) -> str:
    target_kind = message.get("target_kind")
    target = message.get("target")
    if not isinstance(target_kind, str):
        target_kind = ""
    if not isinstance(target, dict):
        target = {}
    if target_kind in {"one_on_one_chat", "group_chat"}:
        chat_id = target.get("chat_id")
        if isinstance(chat_id, str) and chat_id.strip():
            return f"chat:{chat_id.strip().casefold()}"
    if target_kind == "team_channel":
        team_id = target.get("team_id")
        channel_id = target.get("channel_id")
        if (
            isinstance(team_id, str)
            and team_id.strip()
            and isinstance(channel_id, str)
            and channel_id.strip()
        ):
            return f"channel:{team_id.strip().casefold()}:{channel_id.strip().casefold()}"
    return f"{target_kind}:{json.dumps(target, sort_keys=True, separators=(',', ':'))}"


def _load_message_map(path: Path, issues: list[str], *, require_exists: bool = False) -> dict[str, str]:
    if not path.exists():
        if require_exists:
            issues.append(f"Teams message map does not exist: {path}")
        return {}
    payload = _read_json_object_or_issue(path, "Teams message map", issues)
    if payload.get("format") != "exodus.teams.message_map.v1":
        issues.append("Teams message map has unsupported format")
        return {}
    raw_messages = payload.get("messages")
    if not isinstance(raw_messages, list):
        issues.append("Teams message map messages must be a list")
        return {}
    message_map: dict[str, str] = {}
    target_ids: dict[str, str] = {}
    for index, row in enumerate(raw_messages):
        if not isinstance(row, dict):
            issues.append(f"Teams message map row {index} must be an object")
            continue
        source_message_id = row.get("source_message_id")
        teams_message_id = row.get("teams_message_id")
        if not isinstance(source_message_id, str) or not source_message_id.strip():
            issues.append(f"Teams message map row {index} missing source_message_id")
            continue
        source_message_id = source_message_id.strip()
        if not isinstance(teams_message_id, str) or not teams_message_id.strip():
            issues.append(f"Teams message map row {index} missing teams_message_id")
            continue
        teams_message_id = teams_message_id.strip()
        if source_message_id in message_map:
            issues.append(f"Teams message map duplicates source_message_id: {source_message_id}")
            continue
        existing_source = target_ids.get(teams_message_id.casefold())
        if existing_source is not None:
            issues.append(
                "Teams message map assigns multiple source messages to Teams message "
                f"{teams_message_id}: {existing_source}, {source_message_id}"
            )
            continue
        message_map[source_message_id] = teams_message_id
        target_ids[teams_message_id.casefold()] = source_message_id
    return message_map


def _unsupported_attachments_from_plan(
    plan: Mapping[str, object],
    messages: list[dict[str, object]],
    issues: list[str],
    *,
    planned_source_ids: set[str] | None = None,
) -> list[dict[str, object]]:
    raw_rows = plan.get("unsupported_attachments")
    if raw_rows is None:
        return _unsupported_attachments_from_messages(messages)
    if not isinstance(raw_rows, list):
        issues.append("Teams import plan unsupported_attachments must be a list")
        return []
    rows: list[dict[str, object]] = []
    for index, row in enumerate(raw_rows):
        if not isinstance(row, dict):
            issues.append(f"Teams import plan unsupported_attachments row {index} must be an object")
            continue
        source_message_id = row.get("source_message_id")
        if (
            planned_source_ids is not None
            and isinstance(source_message_id, str)
            and source_message_id not in planned_source_ids
        ):
            issues.append(
                "Teams import plan unsupported_attachments row references unplanned "
                f"source_message_id: {source_message_id}"
            )
            continue
        rows.append(dict(row))
    return rows


def _unsupported_attachments_from_messages(
    messages: list[dict[str, object]],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for message in messages:
        raw_attachments = message.get("attachments", [])
        if not isinstance(raw_attachments, list):
            continue
        for attachment in raw_attachments:
            if not isinstance(attachment, dict):
                continue
            if attachment.get("supported") is True:
                continue
            rows.append(
                {
                    "source_message_id": message.get("source_message_id"),
                    "source_conversation_id": message.get("source_conversation_id"),
                    "source_attachment_id": attachment.get("source_attachment_id"),
                    "filename": attachment.get("filename"),
                    "local_path": attachment.get("local_path"),
                    "reason": attachment.get("reason"),
                }
            )
    return rows


def _write_message_map(path: Path, message_map: Mapping[str, str]) -> None:
    payload = {
        "format": "exodus.teams.message_map.v1",
        "messages": [
            {
                "source_message_id": source_message_id,
                "teams_message_id": teams_message_id,
            }
            for source_message_id, teams_message_id in sorted(message_map.items())
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _read_json_object_or_issue(path: Path, label: str, issues: list[str]) -> dict[str, object]:
    if path.exists() and not path.is_file():
        issues.append(f"{label} must be a file: {path}")
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        issues.append(f"{label} does not exist: {path}")
        return {}
    except UnicodeDecodeError:
        issues.append(f"{label} is not valid UTF-8: {path}")
        return {}
    except json.JSONDecodeError as exc:
        issues.append(f"{label} is not valid JSON: {path}: {exc.msg}")
        return {}
    if not isinstance(payload, dict):
        issues.append(f"{label} must be a JSON object")
        return {}
    return payload


def _append_event(
    job_store: JobStore,
    job_id: str,
    kind: JobEventKind,
    phase: str,
    **data: object,
) -> None:
    job_store.append(JobEvent(kind=kind, job_id=job_id, phase=phase, data=data))


def _already_completed(events: list[dict[str, object]]) -> bool:
    for event in events:
        if event.get("kind") == JobEventKind.PHASE_COMPLETED.value and event.get("phase") == "teams_import":
            return True
    return False
