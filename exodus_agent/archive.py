from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Iterable

from .model import Attachment, Conversation, ConversationMembership, Message, Participant, Workspace, utc_now


SCHEMA_VERSION = 1
ARCHIVE_MARKER = ".exodus-archive"


@dataclass(frozen=True)
class Archive:
    root: Path

    def initialize(self, *, source_kind: str, target_kind: str, name: str, reset: bool = False) -> None:
        if reset and self.root.exists():
            self._validate_reset_target()
            shutil.rmtree(self.root)
        self._mkdirs()
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "name": name,
            "source_kind": source_kind,
            "target_kind": target_kind,
            "created_at": utc_now().isoformat(),
        }
        self._write_json(ARCHIVE_MARKER, {"schema_version": SCHEMA_VERSION})
        self._write_json("manifest.json", manifest)

    def write_workspace(self, workspace: Workspace) -> None:
        self._mkdirs()
        self._write_jsonl("workspaces.jsonl", [workspace.to_json()])

    def write_conversations(self, conversations: Iterable[Conversation]) -> None:
        self._mkdirs()
        self._write_jsonl("conversations/conversations.jsonl", [c.to_json() for c in conversations])

    def write_participants(self, participants: Iterable[Participant]) -> None:
        self._mkdirs()
        self._write_jsonl("participants/participants.jsonl", [p.to_json() for p in participants])

    def write_memberships(self, memberships: Iterable[ConversationMembership]) -> None:
        self._mkdirs()
        self._write_jsonl("memberships/memberships.jsonl", [m.to_json() for m in memberships])

    def write_messages(self, conversation_id: str, messages: Iterable[Message]) -> None:
        self._mkdirs()
        rows = list(messages)
        for message in rows:
            if message.conversation_id != conversation_id:
                raise ValueError(
                    f"Message {message.source_id} conversation_id {message.conversation_id!r} "
                    f"does not match archive conversation {conversation_id!r}"
                )
        safe_id = _safe_filename(conversation_id)
        self._write_jsonl(f"messages/{safe_id}.jsonl", [m.to_json() for m in rows])

    def write_attachment_blob(self, *, source_id: str, filename: str, content: bytes) -> Attachment:
        self._mkdirs()
        digest = sha256(content).hexdigest()
        safe_name = _safe_filename(filename)
        relative_path = f"attachments/{digest[:2]}/{digest}-{safe_name}"
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and not path.is_file():
            raise ValueError(f"Archive attachment blob path must be a file: {relative_path}")
        if not path.exists():
            path.write_bytes(content)
        return Attachment(
            source_id=source_id,
            filename=filename,
            size_bytes=len(content),
            sha256=digest,
            local_path=relative_path,
        )

    def read_jsonl(self, relative_path: str) -> list[dict[str, object]]:
        path = self.resolve_path(relative_path)
        if not path.exists():
            return []
        if not path.is_file():
            raise ValueError(f"Archive JSONL path must be a file: {relative_path}")
        rows: list[dict[str, object]] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError as exc:
            raise ValueError(f"Archive JSONL file is not valid UTF-8: {relative_path}") from exc
        for line_number, line in enumerate(lines, start=1):
            if line.strip():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"Archive JSONL row is not valid JSON: {relative_path}:{line_number}: {exc.msg}"
                    ) from exc
                if not isinstance(row, dict):
                    raise ValueError(f"Archive JSONL row must be an object: {relative_path}:{line_number}")
                rows.append(row)
        return rows

    def resolve_path(self, relative_path: str) -> Path:
        path = Path(relative_path)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"Archive path must stay within archive root: {relative_path}")
        resolved_root = self.root.resolve()
        resolved_path = (self.root / path).resolve()
        if not resolved_path.is_relative_to(resolved_root):
            raise ValueError(f"Archive path must stay within archive root: {relative_path}")
        return resolved_path

    def read_manifest(self) -> dict[str, object]:
        path = self.resolve_path("manifest.json")
        if not path.exists():
            raise FileNotFoundError(path)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except UnicodeDecodeError as exc:
            raise ValueError("Archive manifest is not valid UTF-8: manifest.json") from exc
        except json.JSONDecodeError as exc:
            raise ValueError(f"Archive manifest is not valid JSON: manifest.json: {exc.msg}") from exc
        if not isinstance(payload, dict):
            raise ValueError("Archive manifest must be a JSON object")
        return payload

    def read_conversations(self) -> list[Conversation]:
        return [
            Conversation.from_json(row)
            for row in self.read_jsonl("conversations/conversations.jsonl")
        ]

    def read_participants(self) -> list[Participant]:
        return [
            Participant.from_json(row)
            for row in self.read_jsonl("participants/participants.jsonl")
        ]

    def read_memberships(self) -> list[ConversationMembership]:
        return [
            ConversationMembership.from_json(row)
            for row in self.read_jsonl("memberships/memberships.jsonl")
        ]

    def read_messages(self, conversation_id: str) -> list[Message]:
        safe_id = _safe_filename(conversation_id)
        messages = [
            Message.from_json(row)
            for row in self.read_jsonl(f"messages/{safe_id}.jsonl")
        ]
        for message in messages:
            if message.conversation_id != conversation_id:
                raise ValueError(
                    f"Archive message {message.source_id} conversation_id {message.conversation_id!r} "
                    f"does not match requested conversation {conversation_id!r}"
                )
        return messages

    def _mkdirs(self) -> None:
        for relative in [
            ".",
            "conversations",
            "participants",
            "memberships",
            "messages",
            "attachments",
            "mappings",
            "plans",
            "reports",
        ]:
            (self.root / relative).mkdir(parents=True, exist_ok=True)

    def _write_json(self, relative_path: str, value: dict[str, object]) -> None:
        path = self.resolve_path(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and not path.is_file():
            raise ValueError(f"Archive JSON path must be a file: {relative_path}")
        path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def _write_jsonl(self, relative_path: str, rows: list[dict[str, object]]) -> None:
        path = self.resolve_path(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and not path.is_file():
            raise ValueError(f"Archive JSONL path must be a file: {relative_path}")
        with path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")

    def _validate_reset_target(self) -> None:
        resolved_root = self.root.resolve()
        if not self.root.is_dir():
            raise ValueError(f"Archive reset target must be a directory: {self.root}")
        if _is_dangerous_reset_root(resolved_root):
            raise ValueError(f"Refusing to reset unsafe archive root: {self.root}")
        if not any(self.root.iterdir()):
            return
        marker_path = self.root / ARCHIVE_MARKER
        if marker_path.is_file():
            try:
                payload = json.loads(marker_path.read_text(encoding="utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, dict) and payload.get("schema_version") == SCHEMA_VERSION:
                return
        manifest_path = self.root / "manifest.json"
        if manifest_path.is_file():
            try:
                payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict) and payload.get("schema_version") == SCHEMA_VERSION:
                return
        raise ValueError(
            "Refusing to reset a non-empty directory that is not an Exodus archive: "
            f"{self.root}"
        )


def _safe_filename(value: str) -> str:
    safe = "".join(char if char.isalnum() or char in ("-", "_", ".") else "_" for char in value)
    prefix = (safe or "conversation")[:80]
    digest = sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}-{digest}"


def _is_dangerous_reset_root(path: Path) -> bool:
    home = Path.home().resolve()
    cwd = Path.cwd().resolve()
    return path == Path(path.anchor).resolve() or path == home or path == cwd
