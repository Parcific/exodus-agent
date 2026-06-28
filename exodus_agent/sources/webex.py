from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError
from urllib.parse import parse_qs, unquote, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

from exodus_agent.model import (
    Attachment,
    Conversation,
    ConversationMembership,
    ConversationKind,
    Message,
    Participant,
    Workspace,
)
from exodus_agent.config import EndpointConfig
from exodus_agent.redaction import redact_text
from exodus_agent.secrets import resolve_secret

WEBEX_API_BASE_URL = "https://webexapis.com/v1"

Transport = Callable[[str, dict[str, str]], tuple[int, dict[str, str], bytes]]
Sleeper = Callable[[float], None]


class WebexApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class WebexClient:
    access_token: str
    base_url: str = WEBEX_API_BASE_URL
    transport: Transport | None = None
    sleeper: Sleeper = time.sleep
    max_retries: int = 3

    def get(self, path_or_url: str, params: dict[str, object] | None = None) -> dict[str, Any]:
        url = self._url(path_or_url, params)
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.access_token}",
        }
        for attempt in range(self.max_retries + 1):
            status, response_headers, body = self._request(url, headers)
            if status == 429:
                if attempt < self.max_retries:
                    self.sleeper(_retry_after(response_headers))
                    continue
                raise WebexApiError(f"Webex API request exceeded retry limit: url={redact_text(url)}")
            if status >= 400:
                raise WebexApiError(f"Webex API request failed: status={status} url={redact_text(url)}")
            payload = _decode_json_object(body, url)
            if not isinstance(payload, dict):
                raise WebexApiError(f"Webex API returned a non-object response: url={redact_text(url)}")
            payload["_headers"] = response_headers
            return payload

    def get_bytes(self, path_or_url: str, params: dict[str, object] | None = None) -> bytes:
        url = self._url(path_or_url, params)
        headers = {
            "Accept": "*/*",
            "Authorization": f"Bearer {self.access_token}",
        }
        for attempt in range(self.max_retries + 1):
            status, response_headers, body = self._request(url, headers)
            if status == 429:
                if attempt < self.max_retries:
                    self.sleeper(_retry_after(response_headers))
                    continue
                raise WebexApiError(f"Webex file request exceeded retry limit: url={redact_text(url)}")
            if status >= 400:
                raise WebexApiError(f"Webex file request failed: status={status} url={redact_text(url)}")
            return body

    def paged_items(self, path: str, params: dict[str, object] | None = None) -> Iterable[dict[str, Any]]:
        next_url: str | None = self._url(path, params)
        while next_url:
            payload = self.get(next_url)
            items = payload.get("items", [])
            if not isinstance(items, list):
                raise WebexApiError(f"Webex API returned invalid items payload: url={redact_text(next_url)}")
            for index, item in enumerate(items):
                if not isinstance(item, dict):
                    raise WebexApiError(
                        f"Webex API returned non-object item at index {index}: "
                        f"url={redact_text(next_url)}"
                    )
                yield item
            next_url = _next_link(payload.get("_headers", {}))

    def _request(self, url: str, headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
        if self.transport is not None:
            return self.transport(url, headers)

        request = Request(url, headers=headers, method="GET")
        try:
            with urlopen(request) as response:  # noqa: S310 - CLI uses user-provided API URL.
                return (
                    response.status,
                    {key.lower(): value for key, value in response.headers.items()},
                    response.read(),
                )
        except HTTPError as exc:
            return (
                exc.code,
                {key.lower(): value for key, value in exc.headers.items()},
                exc.read(),
            )

    def _url(self, path_or_url: str, params: dict[str, object] | None = None) -> str:
        if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
            return _merge_query(path_or_url, params or {})
        path = path_or_url if path_or_url.startswith("/") else f"/{path_or_url}"
        return _merge_query(f"{self.base_url.rstrip('/')}{path}", params or {})


@dataclass
class WebexSource:
    client: WebexClient
    room_ids: tuple[str, ...] = ()
    max_page_size: int = 100
    message_since: datetime | None = None
    message_before: datetime | None = None
    _conversation_cache: tuple[Conversation, ...] | None = field(default=None, init=False)
    _membership_payload_cache: tuple[dict[str, Any], ...] | None = field(default=None, init=False)
    _excluded_root_ids_cache: dict[str, frozenset[str]] = field(default_factory=dict, init=False)

    def get_workspace(self) -> Workspace:
        me = self.client.get("/people/me")
        org_id = _str(me.get("orgId")) or _str(me.get("id")) or "webex"
        return Workspace(
            source_id=org_id,
            source_kind="webex",
            display_name=_str(me.get("displayName")),
            metadata={
                "person_id": me.get("id"),
                "emails": me.get("emails", []),
            },
        )

    def list_conversations(self) -> Iterable[Conversation]:
        if self._conversation_cache is None:
            if self.room_ids:
                conversations = tuple(self._get_room(room_id) for room_id in self.room_ids)
            else:
                conversations = tuple(
                    _conversation_from_room(room)
                    for room in self.client.paged_items("/rooms", {"max": self.max_page_size})
                )
            self._conversation_cache = conversations
        return self._conversation_cache

    def list_participants(self) -> Iterable[Participant]:
        by_id: dict[str, Participant] = {}
        for membership in self._iter_membership_payloads():
            participant = _participant_from_membership(membership)
            by_id.setdefault(participant.source_id, participant)
        return tuple(by_id.values())

    def list_memberships(self) -> Iterable[ConversationMembership]:
        return tuple(
            _conversation_membership_from_webex(membership)
            for membership in self._iter_membership_payloads()
        )

    def list_messages(self, conversation: Conversation) -> Iterable[Message]:
        params: dict[str, object] = {"roomId": conversation.source_id, "max": self.max_page_size}
        if self.message_since is not None:
            params["afterDate"] = _format_webex_datetime(self.message_since)
        if self.message_before is not None:
            params["before"] = _format_webex_datetime(self.message_before)
        included: list[Message] = []
        excluded_ids: set[str] = set()
        for item in self.client.paged_items("/messages", params):
            message = _message_from_webex(item, conversation.source_id)
            if _message_in_window(message, self.message_since, self.message_before):
                included.append(message)
            else:
                excluded_ids.add(message.source_id)
        self._excluded_root_ids_cache[conversation.source_id] = frozenset(excluded_ids)
        return tuple(sorted(included, key=lambda message: message.created_at))

    def get_excluded_root_ids(self, conversation: Conversation) -> frozenset[str]:
        """Return IDs of messages excluded by the time-window filter for this conversation.

        Populated during list_messages; returns empty frozenset if list_messages has not
        yet been called for this conversation.
        """
        return self._excluded_root_ids_cache.get(conversation.source_id, frozenset())

    def download_attachment(self, attachment: Attachment) -> bytes:
        url = attachment.metadata.get("url")
        if not isinstance(url, str) or not url:
            raise WebexApiError(f"Attachment missing Webex file URL: {attachment.source_id}")
        return self.client.get_bytes(url)

    def _get_room(self, room_id: str) -> Conversation:
        return _conversation_from_room(self.client.get(f"/rooms/{room_id}"))

    def _iter_membership_payloads(self) -> Iterable[dict[str, Any]]:
        if self._membership_payload_cache is None:
            self._membership_payload_cache = tuple(
                membership
                for conversation in self.list_conversations()
                for membership in self.client.paged_items(
                    "/memberships",
                    {"roomId": conversation.source_id, "max": self.max_page_size},
                )
            )
        return self._membership_payload_cache


def source_from_config(config: EndpointConfig) -> WebexSource:
    if config.kind != "webex":
        raise ValueError(f"Expected webex source config, got {config.kind!r}")
    token = resolve_secret(config.settings.get("auth"), field_name="source.auth")
    scope = config.settings.get("scope", "user_rooms")
    room_ids = _room_ids(config.settings.get("room_ids", ()))
    if scope == "selected_rooms" and not room_ids:
        raise ValueError("source.scope selected_rooms requires non-empty source.room_ids")
    if scope == "organization":
        raise ValueError("Webex organization scope requires the future compliance extractor")
    if scope != "user_rooms" and scope != "selected_rooms":
        raise ValueError("source.scope must be one of: user_rooms, selected_rooms, organization")
    max_page_size = _max_page_size(config.settings.get("max_page_size", 100))
    message_since = _optional_config_datetime(config.settings.get("message_since"), "source.message_since")
    message_before = _optional_config_datetime(config.settings.get("message_before"), "source.message_before")
    if message_since is not None and message_before is not None and message_since >= message_before:
        raise ValueError("source.message_since must be before source.message_before")
    return WebexSource(
        client=WebexClient(access_token=token.reveal()),
        room_ids=room_ids,
        max_page_size=max_page_size,
        message_since=message_since,
        message_before=message_before,
    )


def _conversation_from_room(room: dict[str, Any]) -> Conversation:
    room_type = _required_str(room, "type")
    if room_type not in {"direct", "group"}:
        raise WebexApiError(f"Webex payload field must be one of direct, group: type")
    kind = ConversationKind.DIRECT if room_type == "direct" else ConversationKind.SPACE
    last_activity = _parse_dt(room.get("lastActivity"), field_name="lastActivity")
    return Conversation(
        source_id=_required_str(room, "id"),
        kind=kind,
        title=_optional_payload_str(room.get("title"), field_name="title"),
        created_at=_parse_dt(room.get("created")),
        metadata={
            "webex_type": room_type,
            "is_locked": _optional_bool_or_none(room.get("isLocked"), field_name="isLocked"),
            "last_activity": _format_webex_datetime(last_activity) if last_activity is not None else None,
        },
    )


def _participant_from_membership(membership: dict[str, Any]) -> Participant:
    person_id = _required_str(membership, "personId")
    return Participant(
        source_id=person_id,
        display_name=_optional_payload_str(membership.get("personDisplayName"), field_name="personDisplayName")
        or person_id,
        email=_optional_payload_str(membership.get("personEmail"), field_name="personEmail"),
        is_deleted=_optional_bool(membership.get("isDeleted"), field_name="isDeleted"),
        metadata={
            "membership_id": _required_str(membership, "id"),
            "room_id": _required_str(membership, "roomId"),
            "is_moderator": membership.get("isModerator"),
        },
    )


def _conversation_membership_from_webex(membership: dict[str, Any]) -> ConversationMembership:
    membership_id = _required_str(membership, "id")
    room_id = _required_str(membership, "roomId")
    person_id = _required_str(membership, "personId")
    return ConversationMembership(
        source_id=membership_id,
        conversation_id=room_id,
        participant_id=person_id,
        display_name=_optional_payload_str(membership.get("personDisplayName"), field_name="personDisplayName"),
        email=_optional_payload_str(membership.get("personEmail"), field_name="personEmail"),
        is_deleted=_optional_bool(membership.get("isDeleted"), field_name="isDeleted"),
        is_moderator=_optional_bool(membership.get("isModerator"), field_name="isModerator"),
        metadata={"raw_person_org_id": _optional_payload_str(membership.get("personOrgId"), field_name="personOrgId")},
    )


def _message_from_webex(item: dict[str, Any], conversation_id: str) -> Message:
    file_urls = _optional_file_url_list(item.get("files"), field_name="files")
    attachments = tuple(
        Attachment(
            source_id=url,
            filename=_filename_from_url(url),
            metadata={"url": url},
        )
        for url in file_urls
    )
    return Message(
        source_id=_required_str(item, "id"),
        conversation_id=conversation_id,
        author_id=_optional_payload_str(item.get("personId"), field_name="personId"),
        created_at=_parse_required_dt(item, "created"),
        text=_optional_payload_str(item.get("text"), field_name="text"),
        markdown=_optional_payload_str(item.get("markdown"), field_name="markdown"),
        html=_optional_payload_str(item.get("html"), field_name="html"),
        parent_id=_optional_payload_str(item.get("parentId"), field_name="parentId"),
        attachments=attachments,
        metadata={
            "room_id": _optional_payload_str(item.get("roomId"), field_name="roomId"),
            "person_email": _optional_payload_str(item.get("personEmail"), field_name="personEmail"),
            "mentioned_people": _optional_str_list(item.get("mentionedPeople"), field_name="mentionedPeople"),
        },
    )


def _next_link(headers: dict[str, str]) -> str | None:
    link = headers.get("link")
    if not link:
        return None
    for entry in link.split(","):
        parts = [part.strip() for part in entry.split(";")]
        if not parts or not parts[0].startswith("<") or not parts[0].endswith(">"):
            continue
        attrs = {}
        for attr in parts[1:]:
            key, separator, value = attr.partition("=")
            if separator:
                attrs[key.strip().lower()] = value.strip().strip('"')
        if attrs.get("rel") == "next":
            return parts[0][1:-1]
    return None


def _merge_query(url: str, params: dict[str, object]) -> str:
    if not params:
        return url
    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    for key, value in params.items():
        if value is None:
            continue
        query[key] = [str(value)]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def _retry_after(headers: dict[str, str]) -> float:
    value = headers.get("retry-after")
    if value is None:
        return 1.0
    try:
        return max(float(value), 0.0)
    except ValueError:
        return 1.0


def _decode_json_object(body: bytes, url: str) -> object:
    if not body:
        return {}
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WebexApiError(f"Webex API returned non-UTF-8 JSON: url={redact_text(url)}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise WebexApiError(f"Webex API returned invalid JSON: url={redact_text(url)}") from exc


def _parse_required_dt(value: dict[str, Any], key: str) -> datetime:
    parsed = _parse_dt(value.get(key), field_name=key)
    if parsed is None:
        raise WebexApiError(f"Webex payload missing required datetime field: {key}")
    return parsed


def _parse_dt(value: object, *, field_name: str = "datetime") -> datetime | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise WebexApiError(f"Webex payload field must be an ISO-8601 datetime: {field_name}")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise WebexApiError(f"Webex payload field must be an ISO-8601 datetime: {field_name}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _optional_config_datetime(value: object, field_name: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be an ISO-8601 datetime string")
    try:
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 datetime string") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _format_webex_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _message_in_window(
    message: Message,
    message_since: datetime | None,
    message_before: datetime | None,
) -> bool:
    if message_since is not None and message.created_at < message_since:
        return False
    if message_before is not None and message.created_at >= message_before:
        return False
    return True


def _required_str(value: dict[str, Any], key: str) -> str:
    item = _str(value.get(key))
    if item is None:
        raise WebexApiError(f"Webex payload missing required field: {key}")
    return item


def _str(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _optional_bool(value: object, *, field_name: str) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    raise WebexApiError(f"Webex payload field must be a boolean: {field_name}")


def _optional_bool_or_none(value: object, *, field_name: str) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    raise WebexApiError(f"Webex payload field must be a boolean: {field_name}")


def _optional_payload_str(value: object, *, field_name: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        return value or None
    raise WebexApiError(f"Webex payload field must be a string: {field_name}")


def _optional_str_list(value: object, *, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise WebexApiError(f"Webex payload field must be a list of non-empty strings: {field_name}")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise WebexApiError(f"Webex payload field must be a list of non-empty strings: {field_name}")
        items.append(item.strip())
    return tuple(items)


def _optional_file_url_list(value: object, *, field_name: str) -> tuple[str, ...]:
    urls = _optional_str_list(value, field_name=field_name)
    for url in urls:
        parsed = urlparse(url)
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
            raise WebexApiError(f"Webex payload field must be a list of HTTP(S) URLs: {field_name}")
    return urls


def _filename_from_url(url: str) -> str:
    path = urlparse(url).path
    name = unquote(path.rsplit("/", 1)[-1])
    return name or "attachment"


def _room_ids(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError("source.room_ids must be a list of non-empty strings")
    room_ids: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("source.room_ids must be a list of non-empty strings")
        room_ids.append(item.strip())
    return tuple(room_ids)


def _max_page_size(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("source.max_page_size must be an integer")
    try:
        max_page_size = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("source.max_page_size must be an integer") from exc
    if max_page_size < 1 or max_page_size > 1000:
        raise ValueError("source.max_page_size must be between 1 and 1000")
    return max_page_size
