from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch

from exodus_agent.targets.graph_teams_adapter import (
    GraphApiError,
    GraphTeamsAdapter,
    _TokenCache,
    _build_graph_url,
    _build_message_body,
    _graph_error_message,
    _parse_retry_after,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _oauth_ok(token: str = "tok") -> Any:
    """Returns an OAuth transport that always succeeds."""
    def transport(url: str, form_data: dict[str, str]) -> tuple[int, dict[str, Any]]:
        return 200, {"access_token": token, "expires_in": 3600}
    return transport


def _graph_ok(teams_message_id: str = "msg-1") -> Any:
    """Returns a Graph transport that returns success on first call."""
    def transport(method: str, url: str, token: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        return 201, {"id": teams_message_id, "createdDateTime": "2024-01-15T10:00:00.000Z"}
    return transport


def _make_adapter(**kwargs: Any) -> GraphTeamsAdapter:
    defaults: dict[str, Any] = {
        "tenant_id": "tenant-1",
        "client_id": "client-1",
        "client_secret": "secret-1",
        "oauth_transport": _oauth_ok(),
        "graph_transport": _graph_ok(),
        "sleeper": lambda _: None,
    }
    defaults.update(kwargs)
    return GraphTeamsAdapter(**defaults)


def _chat_message(source_id: str = "src-1", chat_id: str = "chat-abc") -> dict[str, Any]:
    return {
        "source_message_id": source_id,
        "source_conversation_id": "conv-1",
        "target_kind": "group_chat",
        "target": {"chat_id": chat_id},
        "author_user_id": "entra-user-1",
        "createdDateTime": "2024-01-15T10:00:00.001Z",
        "original_created_at": "2024-01-15T10:00:00Z",
        "content": "<p>Hello</p>",
        "attachments": [],
        "timestamp_adjusted": False,
        "timestamp_adjustment_ms": 0,
        "timestamp_adjustment_reason": None,
        "import_order": 0,
    }


def _channel_message(source_id: str = "src-1") -> dict[str, Any]:
    msg = _chat_message(source_id=source_id)
    msg["target_kind"] = "team_channel"
    msg["target"] = {"team_id": "team-xyz", "channel_id": "channel-abc"}
    return msg


# ---------------------------------------------------------------------------
# _TokenCache tests
# ---------------------------------------------------------------------------

class TestTokenCache(unittest.TestCase):

    def test_fetches_token_on_first_get(self) -> None:
        cache = _TokenCache("t", "c", "s", oauth_transport=_oauth_ok("my-token"))
        self.assertEqual(cache.get(), "my-token")

    def test_reuses_cached_token_without_re_fetching(self) -> None:
        calls: list[int] = []

        def transport(url: str, form_data: dict[str, str]) -> tuple[int, dict[str, Any]]:
            calls.append(1)
            return 200, {"access_token": "tok", "expires_in": 3600}

        cache = _TokenCache("t", "c", "s", oauth_transport=transport)
        cache.get()
        cache.get()
        self.assertEqual(len(calls), 1)

    def test_invalidate_forces_refresh_on_next_get(self) -> None:
        calls: list[int] = []

        def transport(url: str, form_data: dict[str, str]) -> tuple[int, dict[str, Any]]:
            calls.append(1)
            return 200, {"access_token": f"tok-{len(calls)}", "expires_in": 3600}

        cache = _TokenCache("t", "c", "s", oauth_transport=transport)
        first = cache.get()
        cache.invalidate()
        second = cache.get()
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(first, second)

    def test_raises_on_oauth_error_status(self) -> None:
        def transport(url: str, form_data: dict[str, str]) -> tuple[int, dict[str, Any]]:
            return 401, {"error": "unauthorized_client", "error_description": "bad creds"}

        cache = _TokenCache("t", "c", "s", oauth_transport=transport)
        with self.assertRaises(GraphApiError) as ctx:
            cache.get()
        self.assertIn("bad creds", str(ctx.exception))

    def test_raises_when_response_missing_access_token(self) -> None:
        def transport(url: str, form_data: dict[str, str]) -> tuple[int, dict[str, Any]]:
            return 200, {"token_type": "Bearer"}  # missing access_token

        cache = _TokenCache("t", "c", "s", oauth_transport=transport)
        with self.assertRaises(GraphApiError) as ctx:
            cache.get()
        self.assertIn("access_token", str(ctx.exception))

    def test_oauth_url_contains_tenant_id(self) -> None:
        urls: list[str] = []

        def transport(url: str, form_data: dict[str, str]) -> tuple[int, dict[str, Any]]:
            urls.append(url)
            return 200, {"access_token": "tok", "expires_in": 3600}

        cache = _TokenCache("my-tenant", "c", "s", oauth_transport=transport)
        cache.get()
        self.assertIn("my-tenant", urls[0])

    def test_scope_is_graph_default(self) -> None:
        form_datas: list[dict[str, str]] = []

        def transport(url: str, form_data: dict[str, str]) -> tuple[int, dict[str, Any]]:
            form_datas.append(form_data)
            return 200, {"access_token": "tok", "expires_in": 3600}

        cache = _TokenCache("t", "c", "s", oauth_transport=transport)
        cache.get()
        self.assertEqual(form_datas[0]["scope"], "https://graph.microsoft.com/.default")
        self.assertEqual(form_datas[0]["grant_type"], "client_credentials")


# ---------------------------------------------------------------------------
# _build_graph_url tests
# ---------------------------------------------------------------------------

class TestBuildGraphUrl(unittest.TestCase):

    def test_group_chat(self) -> None:
        url = _build_graph_url("group_chat", {"chat_id": "chat-123"}, "msg-1")
        self.assertEqual(url, "https://graph.microsoft.com/v1.0/chats/chat-123/messages")

    def test_one_on_one_chat(self) -> None:
        url = _build_graph_url("one_on_one_chat", {"chat_id": "  chat-456  "}, "msg-1")
        self.assertIn("chat-456", url)
        self.assertNotIn("  ", url)

    def test_team_channel(self) -> None:
        url = _build_graph_url(
            "team_channel",
            {"team_id": "team-abc", "channel_id": "chan-xyz"},
            "msg-1",
        )
        self.assertEqual(
            url,
            "https://graph.microsoft.com/v1.0/teams/team-abc/channels/chan-xyz/messages",
        )

    def test_raises_for_unsupported_kind(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            _build_graph_url("unknown_kind", {}, "msg-1")
        self.assertIn("unsupported target_kind", str(ctx.exception))

    def test_raises_when_chat_id_missing(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            _build_graph_url("group_chat", {"chat_id": ""}, "msg-1")
        self.assertIn("chat_id", str(ctx.exception))

    def test_raises_when_team_id_missing(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            _build_graph_url("team_channel", {"team_id": "", "channel_id": "c"}, "msg-1")
        self.assertIn("team_id", str(ctx.exception))

    def test_raises_when_channel_id_missing(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            _build_graph_url("team_channel", {"team_id": "t", "channel_id": ""}, "msg-1")
        self.assertIn("channel_id", str(ctx.exception))

    def test_real_teams_chat_id_with_colon_and_at(self) -> None:
        # Realistic Teams thread ID: "19:abc123@thread.v2" — colon and @ must be preserved.
        url = _build_graph_url("group_chat", {"chat_id": "19:abc123@thread.v2"}, "msg-1")
        self.assertIn("19:abc123@thread.v2", url)
        self.assertTrue(url.startswith("https://graph.microsoft.com/v1.0/chats/"))

    def test_chat_id_with_slash_is_url_encoded(self) -> None:
        # A slash in a chat_id would split the path — must be encoded.
        url = _build_graph_url("group_chat", {"chat_id": "chat/evil"}, "msg-1")
        self.assertNotIn("chat/evil", url)
        self.assertIn("%2F", url)

    def test_real_team_channel_guids_unchanged(self) -> None:
        team_id = "00000000-1111-2222-3333-444444444444"
        channel_id = "19:abc@thread.skype"
        url = _build_graph_url("team_channel", {"team_id": team_id, "channel_id": channel_id}, "msg-1")
        self.assertIn(team_id, url)
        self.assertIn(channel_id, url)


# ---------------------------------------------------------------------------
# _build_message_body tests
# ---------------------------------------------------------------------------

class TestBuildMessageBody(unittest.TestCase):

    def test_body_contains_migration_header(self) -> None:
        body = _build_message_body({
            "original_created_at": "2024-01-15T10:00:00Z",
            "content": "<p>Hello</p>",
        })
        html = body["body"]["content"]
        self.assertIn("Migrated from Webex", html)
        self.assertIn("2024-01-15T10:00:00Z", html)

    def test_body_contains_original_content(self) -> None:
        body = _build_message_body({
            "original_created_at": "2024-01-15T10:00:00Z",
            "content": "<p>My message</p>",
        })
        self.assertIn("<p>My message</p>", body["body"]["content"])

    def test_content_type_is_html(self) -> None:
        body = _build_message_body({"content": "hi"})
        self.assertEqual(body["body"]["contentType"], "html")

    def test_missing_original_created_at_still_migrates(self) -> None:
        body = _build_message_body({"content": "<p>hi</p>"})
        html = body["body"]["content"]
        self.assertIn("Migrated from Webex", html)

    def test_empty_content_no_extra_paragraph(self) -> None:
        body = _build_message_body({"original_created_at": "2024-01-15T10:00:00Z", "content": ""})
        html = body["body"]["content"]
        self.assertNotIn("<p></p>", html)
        self.assertIn("Migrated from Webex", html)

    def test_non_string_content_treated_as_empty(self) -> None:
        body = _build_message_body({"content": 42})
        html = body["body"]["content"]
        self.assertIn("Migrated from Webex", html)


# ---------------------------------------------------------------------------
# GraphTeamsAdapter.import_message tests
# ---------------------------------------------------------------------------

class TestGraphTeamsAdapterImportMessage(unittest.TestCase):

    def test_success_chat_message(self) -> None:
        adapter = _make_adapter()
        result = adapter.import_message(_chat_message())
        self.assertEqual(result["teams_message_id"], "msg-1")

    def test_success_channel_message(self) -> None:
        adapter = _make_adapter()
        result = adapter.import_message(_channel_message())
        self.assertEqual(result["teams_message_id"], "msg-1")

    def test_graph_url_for_chat_uses_chat_id(self) -> None:
        posted_urls: list[str] = []

        def transport(method: str, url: str, token: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            posted_urls.append(url)
            return 201, {"id": "msg-1"}

        adapter = _make_adapter(graph_transport=transport)
        adapter.import_message(_chat_message(chat_id="my-chat"))
        self.assertIn("my-chat", posted_urls[0])

    def test_graph_url_for_channel_uses_team_and_channel_ids(self) -> None:
        posted_urls: list[str] = []

        def transport(method: str, url: str, token: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            posted_urls.append(url)
            return 201, {"id": "msg-1"}

        adapter = _make_adapter(graph_transport=transport)
        adapter.import_message(_channel_message())
        self.assertIn("team-xyz", posted_urls[0])
        self.assertIn("channel-abc", posted_urls[0])

    def test_bearer_token_sent_in_header(self) -> None:
        seen_tokens: list[str] = []

        def transport(method: str, url: str, token: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            seen_tokens.append(token)
            return 201, {"id": "msg-1"}

        adapter = _make_adapter(graph_transport=transport, oauth_transport=_oauth_ok("my-bearer"))
        adapter.import_message(_chat_message())
        self.assertEqual(seen_tokens[0], "my-bearer")

    def test_retries_on_429_then_succeeds(self) -> None:
        calls: list[int] = []

        def transport(method: str, url: str, token: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            calls.append(1)
            if len(calls) < 2:
                return 429, {"_retry_after": 0}
            return 201, {"id": "msg-ok"}

        adapter = _make_adapter(graph_transport=transport)
        result = adapter.import_message(_chat_message())
        self.assertEqual(result["teams_message_id"], "msg-ok")
        self.assertEqual(len(calls), 2)

    def test_raises_after_all_retries_exhausted_on_429(self) -> None:
        def transport(method: str, url: str, token: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            return 429, {"_retry_after": 0}

        adapter = _make_adapter(graph_transport=transport, max_retries=2)
        with self.assertRaises(GraphApiError) as ctx:
            adapter.import_message(_chat_message())
        self.assertIn("rate-limit exhausted", str(ctx.exception))
        self.assertIn("3 attempts", str(ctx.exception))

    def test_retries_on_401_with_token_refresh(self) -> None:
        token_calls: list[int] = []
        graph_calls: list[int] = []

        def oauth(url: str, form_data: dict[str, str]) -> tuple[int, dict[str, Any]]:
            token_calls.append(1)
            return 200, {"access_token": f"tok-{len(token_calls)}", "expires_in": 3600}

        def transport(method: str, url: str, token: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            graph_calls.append(1)
            if len(graph_calls) == 1:
                return 401, {}
            return 201, {"id": "msg-after-refresh"}

        adapter = _make_adapter(graph_transport=transport, oauth_transport=oauth)
        result = adapter.import_message(_chat_message())
        self.assertEqual(result["teams_message_id"], "msg-after-refresh")
        self.assertEqual(len(token_calls), 2)

    def test_raises_after_repeated_401(self) -> None:
        def transport(method: str, url: str, token: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            return 401, {}

        adapter = _make_adapter(graph_transport=transport, max_retries=1)
        with self.assertRaises(GraphApiError) as ctx:
            adapter.import_message(_chat_message())
        self.assertIn("authentication failed", str(ctx.exception))

    def test_raises_on_4xx_error(self) -> None:
        def transport(method: str, url: str, token: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            return 403, {"error": {"code": "Forbidden", "message": "Missing permission"}}

        adapter = _make_adapter(graph_transport=transport)
        with self.assertRaises(GraphApiError) as ctx:
            adapter.import_message(_chat_message())
        self.assertIn("403", str(ctx.exception))

    def test_raises_when_response_missing_id(self) -> None:
        def transport(method: str, url: str, token: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            return 201, {"other_field": "value"}  # missing "id"

        adapter = _make_adapter(graph_transport=transport)
        with self.assertRaises(GraphApiError) as ctx:
            adapter.import_message(_chat_message())
        self.assertIn("missing id field", str(ctx.exception))

    def test_raises_when_source_message_id_missing(self) -> None:
        adapter = _make_adapter()
        with self.assertRaises(ValueError) as ctx:
            adapter.import_message({"target_kind": "group_chat", "target": {"chat_id": "c"}})
        self.assertIn("source_message_id", str(ctx.exception))

    def test_raises_when_target_missing(self) -> None:
        adapter = _make_adapter()
        msg = _chat_message()
        del msg["target"]
        with self.assertRaises(ValueError) as ctx:
            adapter.import_message(msg)
        self.assertIn("missing target", str(ctx.exception))

    def test_result_includes_graph_created_date_time(self) -> None:
        adapter = _make_adapter()
        result = adapter.import_message(_chat_message())
        self.assertEqual(result["graph_created_date_time"], "2024-01-15T10:00:00.000Z")


# ---------------------------------------------------------------------------
# _parse_retry_after + _graph_error_message helpers
# ---------------------------------------------------------------------------

class TestHelpers(unittest.TestCase):

    def test_parse_retry_after_from_underscore_key(self) -> None:
        self.assertEqual(_parse_retry_after({"_retry_after": 5.0}), 5.0)

    def test_parse_retry_after_defaults_to_one(self) -> None:
        self.assertEqual(_parse_retry_after({}), 1.0)

    def test_parse_retry_after_negative_clamped_to_zero(self) -> None:
        self.assertEqual(_parse_retry_after({"_retry_after": -3}), 0.0)

    def test_graph_error_message_extracts_code_and_message(self) -> None:
        msg = _graph_error_message({"error": {"code": "Forbidden", "message": "No access"}})
        self.assertIn("Forbidden", msg)
        self.assertIn("No access", msg)

    def test_graph_error_message_empty_for_no_error(self) -> None:
        self.assertEqual(_graph_error_message({}), "")


# ---------------------------------------------------------------------------
# Integration: adapter satisfies TeamsMessageAdapter protocol
# ---------------------------------------------------------------------------

class TestAdapterProtocolCompatibility(unittest.TestCase):

    def test_adapter_is_runtime_usable_as_teams_message_adapter(self) -> None:
        from exodus_agent.targets.teams_executor import TeamsMessageAdapter
        from typing import runtime_checkable, Protocol
        # Protocol is structural — just confirm the method exists with right name
        adapter = _make_adapter()
        self.assertTrue(hasattr(adapter, "import_message"))
        result = adapter.import_message(_chat_message())
        self.assertIn("teams_message_id", result)


if __name__ == "__main__":
    unittest.main()
