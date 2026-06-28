from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Sequence
from typing import Protocol

from exodus_agent.job import JobEvent, JobEventKind, JobStore
from exodus_agent.redaction import redact_sensitive, redact_text


SUPPORTED_METHODS = frozenset(
    {
        "messages.checkHistoryImport",
        "messages.checkHistoryImportPeer",
        "messages.initHistoryImport",
        "messages.uploadImportedMedia",
        "messages.startHistoryImport",
    }
)


REQUIRED_FIELDS_BY_METHOD = {
    "messages.checkHistoryImport": ("conversation_id", "import_head_path"),
    "messages.checkHistoryImportPeer": ("conversation_id", "peer"),
    "messages.initHistoryImport": ("conversation_id", "peer", "file_path", "media_count"),
    "messages.uploadImportedMedia": (
        "conversation_id",
        "peer",
        "file_name",
        "file_path",
        "source_attachment_id",
    ),
    "messages.startHistoryImport": ("conversation_id", "peer"),
}


class TelegramOperationAdapter(Protocol):
    def execute(self, operation: dict[str, object]) -> dict[str, object]:
        """Execute one import-plan operation and return adapter-specific metadata."""


@dataclass(frozen=True)
class DryRunTelegramAdapter:
    def execute(self, operation: dict[str, object]) -> dict[str, object]:
        result = {
            "dry_run": True,
            "method": operation.get("method"),
            "conversation_id": operation.get("conversation_id"),
        }
        if operation.get("method") == "messages.initHistoryImport":
            result["import_id"] = f"dry-run:{operation.get('conversation_id')}"
        return result


@dataclass(frozen=True)
class SubprocessTelegramAdapter:
    command: Sequence[str]
    timeout_seconds: int = 300

    def execute(self, operation: dict[str, object]) -> dict[str, object]:
        if not self.command:
            raise ValueError("Subprocess adapter command cannot be empty")
        if self.timeout_seconds <= 0:
            raise ValueError("Subprocess adapter timeout_seconds must be greater than zero")
        try:
            completed = subprocess.run(
                list(self.command),
                input=json.dumps(operation, sort_keys=True),
                text=True,
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(
                f"Subprocess adapter timed out after {self.timeout_seconds} seconds"
            ) from exc
        if completed.returncode != 0:
            stderr = completed.stderr.strip()
            raise RuntimeError(f"adapter exited {completed.returncode}: {redact_text(stderr)}")
        stdout = completed.stdout.strip()
        if not stdout:
            return {"subprocess": True}
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            snippet = redact_text(stdout[:200])
            raise ValueError(f"Subprocess adapter returned invalid JSON: {exc.msg}: {snippet}") from exc
        if not isinstance(payload, dict):
            raise ValueError("Subprocess adapter must return a JSON object")
        payload.setdefault("subprocess", True)
        return payload


@dataclass(frozen=True)
class TelegramPlanExecutionResult:
    ok: bool
    operations_total: int
    operations_completed: int
    issues: tuple[str, ...]


def execute_import_plan(
    *,
    plan_path: Path,
    job_store: JobStore,
    job_id: str,
    adapter: TelegramOperationAdapter | None = None,
) -> TelegramPlanExecutionResult:
    adapter = adapter or DryRunTelegramAdapter()
    prior_events = job_store.read_events()
    issues: list[str] = []
    plan = _read_plan_or_issue(plan_path, issues)
    if _already_completed(prior_events):
        issues.append("Telegram import job already completed")
    if plan.get("format") != "exodus.telegram.mtproto.import_plan.v1":
        issues.append("Import plan has unsupported format")
    if plan.get("ready") is not True:
        issues.append("Import plan is not ready")

    operations = plan.get("operations", [])
    if not isinstance(operations, list):
        issues.append("Import plan operations must be a list")
        operations = []
    else:
        _validate_operations(operations, issues)

    if issues:
        _append_event(job_store, job_id, JobEventKind.ERROR, "telegram_import", issues=issues)
        return TelegramPlanExecutionResult(
            ok=False,
            operations_total=len(operations),
            operations_completed=0,
            issues=tuple(issues),
        )

    completed = 0
    _append_event(
        job_store,
        job_id,
        JobEventKind.PHASE_STARTED,
        "telegram_import",
        operations_total=len(operations),
        plan_path=str(plan_path),
    )
    conversation_state: dict[str, dict[str, object]] = {}
    for index, operation in enumerate(operations):
        operation = dict(operation)
        method = str(operation["method"])
        operation_to_execute = _normalized_operation(operation, method)
        conversation_id = operation_to_execute.get("conversation_id")
        if _operation_requires_import_id(operation):
            if not isinstance(conversation_id, str) or not conversation_id:
                issues.append(f"Operation {index} requires import_id but has no conversation_id")
                break
            import_id = conversation_state.get(conversation_id, {}).get("import_id")
            if import_id is None:
                issues.append(f"Operation {index} requires import_id before it has been captured")
                break
            operation_to_execute["import_id"] = import_id
        try:
            result = adapter.execute(operation_to_execute)
        except Exception as exc:  # noqa: BLE001 - adapter boundary must capture failures.
            issues.append(f"Operation {index} failed: {redact_text(exc)}")
            break
        missing_captures = _missing_required_captures(operation, result)
        if missing_captures:
            issues.append(
                f"Operation {index} did not return required captures: {', '.join(missing_captures)}"
            )
            break
        _capture_operation_result(
            operation=operation_to_execute,
            result=result,
            conversation_state=conversation_state,
        )
        completed += 1
        _append_event(
            job_store,
            job_id,
            JobEventKind.PHASE_COMPLETED,
            "telegram_import_operation",
            operation_index=index,
            method=method,
            adapter_result=redact_sensitive(result),
        )

    if issues:
        _append_event(job_store, job_id, JobEventKind.ERROR, "telegram_import", issues=issues)
    else:
        _append_event(
            job_store,
            job_id,
            JobEventKind.PHASE_COMPLETED,
            "telegram_import",
            operations_completed=completed,
        )
    return TelegramPlanExecutionResult(
        ok=not issues,
        operations_total=len(operations),
        operations_completed=completed,
        issues=tuple(issues),
    )


def _read_plan_or_issue(path: Path, issues: list[str]) -> dict[str, object]:
    if path.exists() and not path.is_file():
        issues.append(f"Telegram import plan must be a file: {path}")
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        issues.append(f"Telegram import plan does not exist: {path}")
        return {}
    except UnicodeDecodeError:
        issues.append(f"Telegram import plan is not valid UTF-8: {path}")
        return {}
    except json.JSONDecodeError as exc:
        issues.append(f"Telegram import plan is not valid JSON: {path}: {exc.msg}")
        return {}
    if not isinstance(payload, dict):
        issues.append("Telegram import plan must be a JSON object")
        return {}
    return payload


def _validate_operations(operations: list[object], issues: list[str]) -> None:
    captured_import_ids: set[str] = set()
    for index, operation in enumerate(operations):
        if not isinstance(operation, dict):
            issues.append(f"Operation {index} is not an object")
            continue
        method = operation.get("method")
        if not isinstance(method, str) or not method:
            issues.append(f"Operation {index} missing method")
            continue
        if method not in SUPPORTED_METHODS:
            issues.append(f"Operation {index} has unsupported method: {method}")
            continue
        missing = [
            field
            for field in REQUIRED_FIELDS_BY_METHOD[method]
            if operation.get(field) in (None, "")
        ]
        if missing:
            issues.append(f"Operation {index} missing required fields: {', '.join(missing)}")
        _validate_operation_field_types(operation, method, index, issues)
        normalized_operation = _normalized_operation(operation, method)
        conversation_id = normalized_operation.get("conversation_id")
        if _operation_requires_import_id(operation):
            if not isinstance(conversation_id, str) or not conversation_id:
                issues.append(f"Operation {index} requires import_id but has no conversation_id")
            elif conversation_id not in captured_import_ids:
                issues.append(f"Operation {index} requires import_id before it has been captured")
        captures = operation.get("captures", ())
        if captures is not None and not isinstance(captures, (list, tuple)):
            issues.append(f"Operation {index} captures must be a list")
        elif isinstance(captures, (list, tuple)):
            for capture_index, capture in enumerate(captures):
                if not isinstance(capture, str) or not capture.strip():
                    issues.append(f"Operation {index} capture {capture_index} must be a non-empty string")
        if (
            method == "messages.initHistoryImport"
            and isinstance(conversation_id, str)
            and isinstance(captures, (list, tuple))
            and "import_id" in captures
        ):
            captured_import_ids.add(conversation_id)


def _validate_operation_field_types(
    operation: dict[object, object],
    method: str,
    index: int,
    issues: list[str],
) -> None:
    for field in REQUIRED_FIELDS_BY_METHOD[method]:
        if field == "media_count":
            value = operation.get(field)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                issues.append(f"Operation {index} field media_count must be a non-negative integer")
            continue
        value = operation.get(field)
        if not isinstance(value, str) or not value.strip():
            issues.append(f"Operation {index} field {field} must be a non-empty string")
    requires_import_id = operation.get("requires_import_id")
    if requires_import_id is not None and not isinstance(requires_import_id, bool):
        issues.append(f"Operation {index} requires_import_id must be a boolean")


def _normalized_operation(operation: dict[object, object], method: str) -> dict[str, object]:
    normalized = dict(operation)
    for field in REQUIRED_FIELDS_BY_METHOD[method]:
        value = normalized.get(field)
        if isinstance(value, str):
            normalized[field] = value.strip()
    return {str(key): value for key, value in normalized.items()}


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
        if event.get("kind") == JobEventKind.PHASE_COMPLETED.value and event.get("phase") == "telegram_import":
            return True
    return False


def _capture_operation_result(
    *,
    operation: dict[str, object],
    result: dict[str, object],
    conversation_state: dict[str, dict[str, object]],
) -> None:
    conversation_id = operation.get("conversation_id")
    if not isinstance(conversation_id, str) or not conversation_id:
        return
    captures = operation.get("captures", ())
    if not isinstance(captures, (list, tuple)):
        return
    state = conversation_state.setdefault(conversation_id, {})
    for key in captures:
        if isinstance(key, str) and key in result:
            state[key] = result[key]


def _missing_required_captures(
    operation: dict[str, object],
    result: dict[str, object],
) -> list[str]:
    captures = operation.get("captures", ())
    if not isinstance(captures, (list, tuple)):
        return []
    missing: list[str] = []
    for key in captures:
        if isinstance(key, str) and key not in result:
            missing.append(key)
    return missing


def _operation_requires_import_id(operation: dict[str, object]) -> bool:
    return operation.get("requires_import_id") is True or operation.get("method") in {
        "messages.uploadImportedMedia",
        "messages.startHistoryImport",
    }
