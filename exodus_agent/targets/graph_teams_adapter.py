from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError


# Injectable transport seams — callers can supply fakes for testing.
OAuthTransport = Callable[[str, dict[str, str]], tuple[int, dict[str, Any]]]
GraphTransport = Callable[[str, str, str, dict[str, Any]], tuple[int, dict[str, Any]]]

_TOKEN_REFRESH_MARGIN = 60.0  # re-fetch 60 s before expiry


class GraphApiError(RuntimeError):
    pass


@dataclass
class _TokenCache:
    """Client-credentials OAuth2 token, refreshed transparently before expiry."""

    tenant_id: str
    client_id: str
    client_secret: str = field(repr=False)
    oauth_transport: OAuthTransport | None = None
    _token: str = field(default="", init=False, repr=False)
    _expires_at: float = field(default=0.0, init=False)

    def get(self) -> str:
        if self._token and time.monotonic() < self._expires_at - _TOKEN_REFRESH_MARGIN:
            return self._token
        self._refresh()
        return self._token

    def invalidate(self) -> None:
        """Force the next get() to re-fetch (e.g. after a 401 response)."""
        self._expires_at = 0.0

    def _refresh(self) -> None:
        url = (
            f"https://login.microsoftonline.com"
            f"/{urllib.parse.quote(self.tenant_id, safe='')}"
            f"/oauth2/v2.0/token"
        )
        form_data = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "scope": "https://graph.microsoft.com/.default",
        }
        status, body = self._do_oauth(url, form_data)
        if status != 200:
            error = body.get("error_description") or body.get("error") or f"status={status}"
            raise GraphApiError(f"Microsoft OAuth2 token request failed: {error}")
        token = body.get("access_token")
        if not isinstance(token, str) or not token:
            raise GraphApiError("Microsoft OAuth2 token response missing access_token")
        expires_in = body.get("expires_in", 3600)
        self._token = token
        self._expires_at = time.monotonic() + (
            float(expires_in) if isinstance(expires_in, (int, float)) else 3600.0
        )

    def _do_oauth(self, url: str, form_data: dict[str, str]) -> tuple[int, dict[str, Any]]:
        if self.oauth_transport is not None:
            return self.oauth_transport(url, form_data)
        encoded = urllib.parse.urlencode(form_data).encode()
        req = urllib.request.Request(
            url,
            data=encoded,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req) as resp:  # noqa: S310
                return resp.status, json.loads(resp.read())
        except HTTPError as exc:
            try:
                body: dict[str, Any] = json.loads(exc.read())
            except Exception:
                body = {}
            return exc.code, body


@dataclass
class GraphTeamsAdapter:
    """Live Microsoft Teams adapter via Graph API and client-credentials OAuth2.

    Required app permissions: Chat.ReadWrite.All (chats) or
    ChannelMessage.Send (channels). Messages are posted as the application
    identity; original author and timestamp are embedded in the message body
    as migration provenance because createdDateTime is server-controlled
    without Teamwork.Migrate.All.
    """

    tenant_id: str
    client_id: str
    client_secret: str = field(repr=False)  # pre-revealed; caller owns lifecycle
    graph_transport: GraphTransport | None = None
    oauth_transport: OAuthTransport | None = None
    sleeper: Callable[[float], None] = field(default=time.sleep)
    max_retries: int = 3
    _tokens: _TokenCache = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._tokens = _TokenCache(
            tenant_id=self.tenant_id,
            client_id=self.client_id,
            client_secret=self.client_secret,
            oauth_transport=self.oauth_transport,
        )

    def import_message(self, message: Mapping[str, object]) -> dict[str, object]:
        source_message_id = message.get("source_message_id")
        if not isinstance(source_message_id, str) or not source_message_id:
            raise ValueError("Prepared Teams message missing source_message_id")
        target_kind = message.get("target_kind")
        target = message.get("target")
        if not isinstance(target, dict):
            raise ValueError(f"Message {source_message_id} missing target")
        url = _build_graph_url(target_kind, target, source_message_id)
        body = _build_message_body(message)
        response = self._post_with_retry(url, body, source_message_id)
        teams_message_id = response.get("id")
        if not isinstance(teams_message_id, str) or not teams_message_id:
            raise GraphApiError(
                f"Graph API response for message {source_message_id} missing id field"
            )
        return {
            "teams_message_id": teams_message_id,
            "graph_created_date_time": response.get("createdDateTime"),
        }

    def _post_with_retry(
        self, url: str, body: dict[str, Any], source_message_id: str
    ) -> dict[str, Any]:
        for attempt in range(self.max_retries + 1):
            token = self._tokens.get()
            status, response = self._do_graph("POST", url, token, body)
            if status == 429:
                if attempt < self.max_retries:
                    self.sleeper(_parse_retry_after(response))
                    continue
                total = self.max_retries + 1
                raise GraphApiError(
                    f"Graph API rate-limit exhausted after"
                    f" {total} {'attempt' if total == 1 else 'attempts'}"
                    f" for message {source_message_id}"
                )
            if status == 401:
                if attempt < self.max_retries:
                    self._tokens.invalidate()
                    continue
                raise GraphApiError(
                    f"Graph API authentication failed for message {source_message_id}: status=401"
                )
            if status >= 400:
                raise GraphApiError(
                    f"Graph API request failed for message {source_message_id}:"
                    f" status={status} {_graph_error_message(response)}"
                )
            return response
        raise AssertionError("unreachable")  # pragma: no cover

    def _do_graph(
        self, method: str, url: str, token: str, body: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        if self.graph_transport is not None:
            return self.graph_transport(method, url, token, body)
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(req) as resp:  # noqa: S310
                return resp.status, json.loads(resp.read())
        except HTTPError as exc:
            try:
                resp_body: dict[str, Any] = json.loads(exc.read())
            except Exception:
                resp_body = {}
            retry_after = exc.headers.get("Retry-After")
            if retry_after is not None:
                try:
                    resp_body["_retry_after"] = float(retry_after)
                except ValueError:
                    pass
            return exc.code, resp_body


def _build_graph_url(
    target_kind: object, target: dict[str, object], source_message_id: str
) -> str:
    if target_kind in {"one_on_one_chat", "group_chat"}:
        chat_id = target.get("chat_id")
        if not isinstance(chat_id, str) or not chat_id.strip():
            raise ValueError(f"Message {source_message_id} missing target.chat_id")
        return f"https://graph.microsoft.com/v1.0/chats/{chat_id.strip()}/messages"
    if target_kind == "team_channel":
        team_id = target.get("team_id")
        channel_id = target.get("channel_id")
        if not isinstance(team_id, str) or not team_id.strip():
            raise ValueError(f"Message {source_message_id} missing target.team_id")
        if not isinstance(channel_id, str) or not channel_id.strip():
            raise ValueError(f"Message {source_message_id} missing target.channel_id")
        return (
            f"https://graph.microsoft.com/v1.0"
            f"/teams/{team_id.strip()}/channels/{channel_id.strip()}/messages"
        )
    raise ValueError(
        f"Message {source_message_id} has unsupported target_kind: {target_kind!r}"
    )


def _build_message_body(message: Mapping[str, object]) -> dict[str, Any]:
    original_created_at = message.get("original_created_at")
    content = message.get("content")
    if not isinstance(content, str):
        content = ""
    if isinstance(original_created_at, str) and original_created_at:
        header = (
            f"<p><em>Migrated from Webex"
            f" | Originally sent: {original_created_at}</em></p>"
        )
    else:
        header = "<p><em>Migrated from Webex</em></p>"
    body_html = f"{header}<p>{content}</p>" if content else header
    return {"body": {"contentType": "html", "content": body_html}}


def _parse_retry_after(response: dict[str, Any]) -> float:
    value = response.get("_retry_after") or response.get("retry_after")
    try:
        return max(0.0, float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 1.0


def _graph_error_message(response: dict[str, Any]) -> str:
    error = response.get("error")
    if isinstance(error, dict):
        code = error.get("code") or ""
        msg = error.get("message") or ""
        parts = [p for p in (str(code), str(msg)) if p]
        return f"[{'] ['.join(parts)}]" if parts else ""
    return ""
