from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


JsonObject = dict[str, Any]


class ConversationKind(StrEnum):
    DIRECT = "direct"
    GROUP = "group"
    CHANNEL = "channel"
    SPACE = "space"


@dataclass(frozen=True)
class Workspace:
    source_id: str
    source_kind: str
    display_name: str | None = None
    metadata: JsonObject = field(default_factory=dict)

    def to_json(self) -> JsonObject:
        return _drop_none(
            {
                "source_id": self.source_id,
                "source_kind": self.source_kind,
                "display_name": self.display_name,
                "metadata": self.metadata,
            }
        )

    @classmethod
    def from_json(cls, value: JsonObject) -> Workspace:
        return cls(
            source_id=_required_str(value, "source_id"),
            source_kind=_required_str(value, "source_kind"),
            display_name=_optional_str(value.get("display_name"), field_name="display_name"),
            metadata=_object(value.get("metadata"), field_name="metadata"),
        )


@dataclass(frozen=True)
class Conversation:
    source_id: str
    kind: ConversationKind
    title: str | None = None
    created_at: datetime | None = None
    metadata: JsonObject = field(default_factory=dict)

    def to_json(self) -> JsonObject:
        return _drop_none(
            {
                "source_id": self.source_id,
                "kind": self.kind.value,
                "title": self.title,
                "created_at": _dt(self.created_at),
                "metadata": self.metadata,
            }
        )

    @classmethod
    def from_json(cls, value: JsonObject) -> Conversation:
        return cls(
            source_id=_required_str(value, "source_id"),
            kind=ConversationKind(_required_str(value, "kind")),
            title=_optional_str(value.get("title"), field_name="title"),
            created_at=_parse_dt(value.get("created_at"), field_name="created_at"),
            metadata=_object(value.get("metadata"), field_name="metadata"),
        )


@dataclass(frozen=True)
class Participant:
    source_id: str
    display_name: str
    email: str | None = None
    is_deleted: bool = False
    metadata: JsonObject = field(default_factory=dict)

    def to_json(self) -> JsonObject:
        return _drop_none(
            {
                "source_id": self.source_id,
                "display_name": self.display_name,
                "email": self.email,
                "is_deleted": self.is_deleted,
                "metadata": self.metadata,
            }
        )

    @classmethod
    def from_json(cls, value: JsonObject) -> Participant:
        return cls(
            source_id=_required_str(value, "source_id"),
            display_name=_required_str(value, "display_name"),
            email=_optional_str(value.get("email"), field_name="email"),
            is_deleted=_optional_bool(value.get("is_deleted"), field_name="is_deleted"),
            metadata=_object(value.get("metadata"), field_name="metadata"),
        )


@dataclass(frozen=True)
class ConversationMembership:
    source_id: str
    conversation_id: str
    participant_id: str
    display_name: str | None = None
    email: str | None = None
    is_deleted: bool = False
    is_moderator: bool = False
    metadata: JsonObject = field(default_factory=dict)

    def to_json(self) -> JsonObject:
        return _drop_none(
            {
                "source_id": self.source_id,
                "conversation_id": self.conversation_id,
                "participant_id": self.participant_id,
                "display_name": self.display_name,
                "email": self.email,
                "is_deleted": self.is_deleted,
                "is_moderator": self.is_moderator,
                "metadata": self.metadata,
            }
        )

    @classmethod
    def from_json(cls, value: JsonObject) -> ConversationMembership:
        return cls(
            source_id=_required_str(value, "source_id"),
            conversation_id=_required_str(value, "conversation_id"),
            participant_id=_required_str(value, "participant_id"),
            display_name=_optional_str(value.get("display_name"), field_name="display_name"),
            email=_optional_str(value.get("email"), field_name="email"),
            is_deleted=_optional_bool(value.get("is_deleted"), field_name="is_deleted"),
            is_moderator=_optional_bool(value.get("is_moderator"), field_name="is_moderator"),
            metadata=_object(value.get("metadata"), field_name="metadata"),
        )


@dataclass(frozen=True)
class Attachment:
    source_id: str
    filename: str
    mime_type: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    local_path: str | None = None
    metadata: JsonObject = field(default_factory=dict)

    def to_json(self) -> JsonObject:
        return _drop_none(
            {
                "source_id": self.source_id,
                "filename": self.filename,
                "mime_type": self.mime_type,
                "size_bytes": self.size_bytes,
                "sha256": self.sha256,
                "local_path": self.local_path,
                "metadata": self.metadata,
            }
        )

    @classmethod
    def from_json(cls, value: JsonObject) -> Attachment:
        return cls(
            source_id=_required_str(value, "source_id"),
            filename=_required_str(value, "filename"),
            mime_type=_optional_str(value.get("mime_type"), field_name="mime_type"),
            size_bytes=_optional_nonnegative_int(value.get("size_bytes"), field_name="size_bytes"),
            sha256=_optional_str(value.get("sha256"), field_name="sha256"),
            local_path=_optional_str(value.get("local_path"), field_name="local_path"),
            metadata=_object(value.get("metadata"), field_name="metadata"),
        )


@dataclass(frozen=True)
class Message:
    source_id: str
    conversation_id: str
    author_id: str | None
    created_at: datetime
    text: str | None = None
    markdown: str | None = None
    html: str | None = None
    parent_id: str | None = None
    edited_at: datetime | None = None
    deleted_at: datetime | None = None
    attachments: tuple[Attachment, ...] = ()
    metadata: JsonObject = field(default_factory=dict)

    def to_json(self) -> JsonObject:
        return _drop_none(
            {
                "source_id": self.source_id,
                "conversation_id": self.conversation_id,
                "author_id": self.author_id,
                "created_at": _dt(self.created_at),
                "text": self.text,
                "markdown": self.markdown,
                "html": self.html,
                "parent_id": self.parent_id,
                "edited_at": _dt(self.edited_at),
                "deleted_at": _dt(self.deleted_at),
                "attachments": [item.to_json() for item in self.attachments],
                "metadata": self.metadata,
            }
        )

    @classmethod
    def from_json(cls, value: JsonObject) -> Message:
        attachments = value.get("attachments", [])
        if not isinstance(attachments, list):
            raise ValueError("Message attachments must be a list")
        parsed_attachments: list[Attachment] = []
        for index, item in enumerate(attachments):
            if not isinstance(item, dict):
                raise ValueError(f"Message attachment row {index} must be an object")
            parsed_attachments.append(Attachment.from_json(item))
        return cls(
            source_id=_required_str(value, "source_id"),
            conversation_id=_required_str(value, "conversation_id"),
            author_id=_optional_str(value.get("author_id"), field_name="author_id"),
            created_at=_required_dt(value, "created_at"),
            text=_optional_str(value.get("text"), field_name="text"),
            markdown=_optional_str(value.get("markdown"), field_name="markdown"),
            html=_optional_str(value.get("html"), field_name="html"),
            parent_id=_optional_str(value.get("parent_id"), field_name="parent_id"),
            edited_at=_parse_dt(value.get("edited_at"), field_name="edited_at"),
            deleted_at=_parse_dt(value.get("deleted_at"), field_name="deleted_at"),
            attachments=tuple(parsed_attachments),
            metadata=_object(value.get("metadata"), field_name="metadata"),
        )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _dt(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _drop_none(value: JsonObject) -> JsonObject:
    return {key: item for key, item in value.items() if item is not None}


def _parse_dt(value: object, *, field_name: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"Expected datetime string field: {field_name}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Invalid datetime field: {field_name}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _required_dt(value: JsonObject, key: str) -> datetime:
    parsed = _parse_dt(value.get(key), field_name=key)
    if parsed is None:
        raise ValueError(f"Missing required datetime field: {key}")
    return parsed


def _required_str(value: JsonObject, key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise ValueError(f"Missing required string field: {key}")
    return item


def _optional_str(value: object, *, field_name: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value if value.strip() else None
    raise ValueError(f"Expected string field: {field_name}")


def _optional_int(value: object, *, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise ValueError(f"Expected integer field: {field_name}")


def _optional_nonnegative_int(value: object, *, field_name: str) -> int | None:
    parsed = _optional_int(value, field_name=field_name)
    if parsed is not None and parsed < 0:
        raise ValueError(f"Expected non-negative integer field: {field_name}")
    return parsed


def _optional_bool(value: object, *, field_name: str) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    raise ValueError(f"Expected boolean field: {field_name}")


def _object(value: object, *, field_name: str) -> JsonObject:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    raise ValueError(f"Expected object field: {field_name}")
