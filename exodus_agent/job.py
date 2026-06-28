from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

from .model import utc_now


class JobEventKind(StrEnum):
    CREATED = "created"
    PHASE_STARTED = "phase_started"
    PHASE_COMPLETED = "phase_completed"
    WARNING = "warning"
    ERROR = "error"


JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def validate_job_id(job_id: str) -> str:
    if not JOB_ID_PATTERN.fullmatch(job_id):
        raise ValueError(
            "Job ID must be 1-128 characters and contain only letters, numbers, dots, "
            "underscores, or hyphens; it must start with a letter or number"
        )
    return job_id


@dataclass(frozen=True)
class JobEvent:
    kind: JobEventKind
    job_id: str
    id: str = field(default_factory=lambda: str(uuid4()))
    phase: str | None = None
    message: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "job_id": self.job_id,
            "kind": self.kind.value,
            "phase": self.phase,
            "message": self.message,
            "data": self.data,
            "created_at": utc_now().isoformat(),
        }


@dataclass(frozen=True)
class JobStore:
    root: Path

    @property
    def job_id(self) -> str:
        return validate_job_id(self.root.name)

    @property
    def events_path(self) -> Path:
        return self.root / "events.jsonl"

    def create(self, *, job_id: str) -> None:
        job_id = validate_job_id(job_id)
        if job_id != self.job_id:
            raise ValueError(f"Job ID does not match job store path: {job_id}")
        self.root.mkdir(parents=True, exist_ok=True)
        self._ensure_events_file_path()
        if self.events_path.exists() and self.events_path.stat().st_size > 0:
            raise FileExistsError(f"Job already exists: {job_id}")
        self.append(JobEvent(kind=JobEventKind.CREATED, job_id=job_id))

    def append(self, event: JobEvent) -> None:
        validate_job_id(event.job_id)
        if event.job_id != self.job_id:
            raise ValueError(f"Job event job_id does not match job store path: {event.job_id}")
        self.root.mkdir(parents=True, exist_ok=True)
        self._ensure_events_file_path()
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.to_json(), sort_keys=True, separators=(",", ":")) + "\n")

    def read_events(self) -> list[dict[str, Any]]:
        if not self.events_path.exists():
            return []
        self._ensure_events_file_path()
        events: list[dict[str, Any]] = []
        try:
            lines = self.events_path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError as exc:
            raise ValueError(f"Job events JSONL is not valid UTF-8: {self.events_path}") from exc
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Job events JSONL row is invalid: {self.events_path}:{line_number}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"Job events JSONL row must be an object: {self.events_path}:{line_number}")
            _validate_event_row(row, self.events_path, line_number)
            if row["job_id"] != self.job_id:
                raise ValueError(f"Job event row job_id does not match job store path: {self.events_path}:{line_number}")
            events.append(row)
        return events

    def _ensure_events_file_path(self) -> None:
        if self.events_path.exists() and not self.events_path.is_file():
            raise ValueError(f"Job events JSONL path must be a file: {self.events_path}")


def _validate_event_row(row: dict[str, Any], path: Path, line_number: int) -> None:
    job_id = row.get("job_id")
    if not isinstance(job_id, str):
        raise ValueError(f"Job event row has invalid job_id: {path}:{line_number}")
    try:
        validate_job_id(job_id)
    except ValueError as exc:
        raise ValueError(f"Job event row has invalid job_id: {path}:{line_number}") from exc

    kind = row.get("kind")
    if kind not in {event_kind.value for event_kind in JobEventKind}:
        raise ValueError(f"Job event row has invalid kind: {path}:{line_number}")

    event_id = row.get("id")
    if not isinstance(event_id, str) or not event_id:
        raise ValueError(f"Job event row has invalid id: {path}:{line_number}")

    created_at = row.get("created_at")
    if not isinstance(created_at, str) or not created_at:
        raise ValueError(f"Job event row has invalid created_at: {path}:{line_number}")
    _parse_event_datetime(created_at, path, line_number)

    phase = row.get("phase")
    if phase is not None and not isinstance(phase, str):
        raise ValueError(f"Job event row has invalid phase: {path}:{line_number}")

    message = row.get("message")
    if message is not None and not isinstance(message, str):
        raise ValueError(f"Job event row has invalid message: {path}:{line_number}")

    data = row.get("data")
    if not isinstance(data, dict):
        raise ValueError(f"Job event row has invalid data: {path}:{line_number}")


def _parse_event_datetime(value: str, path: Path, line_number: int) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Job event row has invalid created_at: {path}:{line_number}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"Job event row has invalid created_at: {path}:{line_number}")
    return parsed
