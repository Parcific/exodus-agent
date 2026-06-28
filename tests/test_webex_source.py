from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from exodus_agent.config import EndpointConfig
from exodus_agent.model import ConversationKind
from exodus_agent.secrets import SecretResolutionError
from exodus_agent.sources.webex import (
    WebexApiError,
    WebexClient,
    WebexSource,
    _filename_from_url,
    _next_link,
    source_from_config,
)


class FakeWebexTransport:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.responses: dict[str, list[tuple[int, dict[str, str], dict[str, object]]]] = {}

    def add(
        self,
        path: str,
        payload: dict[str, object],
        headers: dict[str, str] | None = None,
        status: int = 200,
    ) -> None:
        self.responses.setdefault(path, []).append((status, headers or {}, payload))

    def __call__(self, url: str, headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
        self.calls.append(url)
        self.assert_auth(headers)
        if url == "https://files.example.test/a.txt":
            return 200, {}, b"file-content"
        parsed = urlparse(url)
        path = parsed.path.removeprefix("/v1")
        query = parse_qs(parsed.query)
        key = path
        if "page" in query:
            key = f"{path}?page={query['page'][0]}"
        responses = self.responses.get(key)
        if not responses:
            raise AssertionError(f"No fake response for {key}. URL: {url}")
        status, response_headers, payload = responses.pop(0)
        return status, {k.lower(): v for k, v in response_headers.items()}, json.dumps(payload).encode()

    def assert_auth(self, headers: dict[str, str]) -> None:
        if headers.get("Authorization") != "Bearer token":
            raise AssertionError("missing bearer token")


class WebexSourceTests(unittest.TestCase):
    def test_lists_workspace_conversations_participants_and_messages(self) -> None:
        transport = FakeWebexTransport()
        transport.add(
            "/people/me",
            {"id": "person-1", "orgId": "org-1", "displayName": "Ada", "emails": ["ada@example.com"]},
        )
        transport.add(
            "/rooms",
            {
                "items": [
                    {
                        "id": "room-1",
                        "title": "General",
                        "type": "group",
                        "created": "2026-01-01T00:00:00.000Z",
                    }
                ]
            },
            headers={"Link": '<https://webexapis.com/v1/rooms?page=2>; rel="next"'},
        )
        transport.add(
            "/rooms?page=2",
            {
                "items": [
                    {
                        "id": "room-2",
                        "title": "DM",
                        "type": "direct",
                        "created": "2026-01-02T00:00:00Z",
                    }
                ]
            },
        )
        for room_id in ["room-1", "room-2"]:
            transport.add(
                "/memberships",
                {
                    "items": [
                        {
                            "id": f"membership-{room_id}",
                            "roomId": room_id,
                            "personId": "person-1",
                            "personDisplayName": "Ada",
                            "personEmail": "ada@example.com",
                        }
                    ]
                },
            )
        transport.add(
            "/messages",
            {
                "items": [
                    {
                        "id": "msg-2",
                        "roomId": "room-1",
                        "personId": "person-1",
                        "created": "2026-01-01T00:00:02Z",
                        "text": "second",
                    },
                    {
                        "id": "msg-1",
                        "roomId": "room-1",
                        "personId": "person-1",
                        "created": "2026-01-01T00:00:01Z",
                        "markdown": "**first**",
                        "files": ["https://files.example.test/a.txt"],
                    },
                ]
            },
        )

        source = WebexSource(WebexClient("token", transport=transport, sleeper=lambda _: None))

        workspace = source.get_workspace()
        conversations = tuple(source.list_conversations())
        participants = tuple(source.list_participants())
        memberships = tuple(source.list_memberships())
        messages = tuple(source.list_messages(conversations[0]))

        self.assertEqual(workspace.source_id, "org-1")
        self.assertEqual([conversation.source_id for conversation in conversations], ["room-1", "room-2"])
        self.assertEqual(conversations[0].kind, ConversationKind.SPACE)
        self.assertEqual(conversations[1].kind, ConversationKind.DIRECT)
        self.assertEqual(len(participants), 1)
        self.assertEqual(len(memberships), 2)
        self.assertEqual(memberships[0].conversation_id, "room-1")
        self.assertEqual(memberships[0].participant_id, "person-1")
        self.assertEqual([message.source_id for message in messages], ["msg-1", "msg-2"])
        self.assertEqual(messages[0].attachments[0].filename, "a.txt")
        self.assertEqual(source.download_attachment(messages[0].attachments[0]), b"file-content")

    def test_retries_429_with_retry_after(self) -> None:
        sleeps: list[float] = []
        transport = FakeWebexTransport()
        transport.add("/people/me", {"message": "slow down"}, {"Retry-After": "2"}, status=429)
        transport.add("/people/me", {"id": "person-1", "displayName": "Ada"})

        client = WebexClient("token", transport=transport, sleeper=sleeps.append)

        self.assertEqual(client.get("/people/me")["id"], "person-1")
        self.assertEqual(sleeps, [2.0])

    def test_get_raises_retries_exhausted_after_all_429s(self) -> None:
        calls: list[int] = []

        def transport(url: str, headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
            calls.append(1)
            return 429, {"retry-after": "0"}, b'{"message":"slow down"}'

        client = WebexClient("token", transport=transport, sleeper=lambda _: None, max_retries=3)

        with self.assertRaisesRegex(WebexApiError, "retries exhausted after 4 attempts"):
            client.get("https://webexapis.com/v1/people/me")
        self.assertEqual(len(calls), 4)  # initial + 3 retries

    def test_get_bytes_raises_retries_exhausted_after_all_429s(self) -> None:
        calls: list[int] = []

        def transport(url: str, headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
            calls.append(1)
            return 429, {"retry-after": "0"}, b""

        client = WebexClient("token", transport=transport, sleeper=lambda _: None, max_retries=3)

        with self.assertRaisesRegex(WebexApiError, "retries exhausted after 4 attempts"):
            client.get_bytes("https://webexapis.com/v1/files/1")
        self.assertEqual(len(calls), 4)  # initial + 3 retries

    def test_redacts_secret_query_params_in_webex_errors(self) -> None:
        transport = FakeWebexTransport()
        transport.add("/messages", {"message": "bad"}, status=400)
        client = WebexClient("token", transport=transport, sleeper=lambda _: None)

        with self.assertRaisesRegex(WebexApiError, r"token=\[redacted\]") as context:
            client.get("https://webexapis.com/v1/messages?token=super-secret", {"roomId": "room-1"})

        self.assertNotIn("super-secret", str(context.exception))

    def test_invalid_success_json_becomes_redacted_webex_error(self) -> None:
        def transport(url: str, headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
            self.assertEqual(headers["Authorization"], "Bearer token")
            return 200, {}, b"{not-json}"

        client = WebexClient("token", transport=transport, sleeper=lambda _: None)

        with self.assertRaisesRegex(WebexApiError, r"invalid JSON.*token=\[redacted\]") as context:
            client.get("https://webexapis.com/v1/people/me?token=super-secret")

        self.assertNotIn("super-secret", str(context.exception))

    def test_non_utf8_success_json_becomes_redacted_webex_error(self) -> None:
        def transport(url: str, headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
            self.assertEqual(headers["Authorization"], "Bearer token")
            return 200, {}, b"\xff"

        client = WebexClient("token", transport=transport, sleeper=lambda _: None)

        with self.assertRaisesRegex(WebexApiError, r"non-UTF-8 JSON.*token=\[redacted\]") as context:
            client.get("https://webexapis.com/v1/people/me?token=super-secret")

        self.assertNotIn("super-secret", str(context.exception))

    def test_paged_items_rejects_non_object_items_with_redacted_url(self) -> None:
        def transport(url: str, headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
            self.assertEqual(headers["Authorization"], "Bearer token")
            return 200, {}, json.dumps({"items": [{"id": "ok"}, "bad"]}).encode()

        client = WebexClient("token", transport=transport, sleeper=lambda _: None)

        with self.assertRaisesRegex(WebexApiError, r"non-object item at index 1.*token=\[redacted\]") as context:
            tuple(client.paged_items("https://webexapis.com/v1/messages?token=super-secret"))

        self.assertNotIn("super-secret", str(context.exception))

    def test_treats_naive_webex_datetimes_as_utc(self) -> None:
        transport = FakeWebexTransport()
        transport.add(
            "/messages",
            {
                "items": [
                    {
                        "id": "msg-1",
                        "roomId": "room-1",
                        "personId": "person-1",
                        "created": "2026-01-01T12:00:00",
                        "text": "hello",
                    }
                ]
            },
        )
        source = WebexSource(WebexClient("token", transport=transport, sleeper=lambda _: None))

        messages = tuple(source.list_messages(type("Conversation", (), {"source_id": "room-1"})()))

        self.assertEqual(messages[0].created_at, datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc))

    def test_rejects_invalid_required_message_datetime_as_webex_error(self) -> None:
        transport = FakeWebexTransport()
        transport.add(
            "/messages",
            {
                "items": [
                    {
                        "id": "msg-1",
                        "roomId": "room-1",
                        "personId": "person-1",
                        "created": "not-a-date",
                        "text": "hello",
                    }
                ]
            },
        )
        source = WebexSource(WebexClient("token", transport=transport, sleeper=lambda _: None))

        with self.assertRaisesRegex(WebexApiError, "created"):
            tuple(source.list_messages(type("Conversation", (), {"source_id": "room-1"})()))

    def test_trims_webex_payload_identity_strings(self) -> None:
        transport = FakeWebexTransport()
        transport.add(
            "/rooms",
            {
                "items": [
                    {
                        "id": " room-1 ",
                        "title": " General ",
                        "type": " group ",
                    }
                ]
            },
        )
        transport.add(
            "/messages",
            {
                "items": [
                    {
                        "id": " msg-1 ",
                        "roomId": " room-1 ",
                        "personId": " user-1 ",
                        "personEmail": " ada@example.com ",
                        "created": "2026-01-01T00:00:00Z",
                        "text": " hello ",
                        "files": [" https://files.example.test/a.txt "],
                        "mentionedPeople": [" user-1 "],
                    }
                ]
            },
        )
        source = WebexSource(WebexClient("token", transport=transport, sleeper=lambda _: None))

        conversation = tuple(source.list_conversations())[0]
        message = tuple(source.list_messages(type("Conversation", (), {"source_id": "room-1"})()))[0]

        self.assertEqual(conversation.source_id, "room-1")
        self.assertEqual(conversation.title, "General")
        self.assertEqual(message.source_id, "msg-1")
        self.assertEqual(message.author_id, "user-1")
        self.assertEqual(message.text, "hello")
        self.assertEqual(message.metadata["room_id"], "room-1")
        self.assertEqual(message.metadata["person_email"], "ada@example.com")
        self.assertEqual(message.metadata["mentioned_people"], ("user-1",))
        self.assertEqual(message.attachments[0].source_id, "https://files.example.test/a.txt")

    def test_rejects_whitespace_required_webex_id(self) -> None:
        transport = FakeWebexTransport()
        transport.add(
            "/rooms",
            {
                "items": [
                    {
                        "id": "   ",
                        "type": "group",
                    }
                ]
            },
        )
        source = WebexSource(WebexClient("token", transport=transport, sleeper=lambda _: None))

        with self.assertRaisesRegex(WebexApiError, "id"):
            tuple(source.list_conversations())

    def test_rejects_non_list_message_files_payload(self) -> None:
        transport = FakeWebexTransport()
        transport.add(
            "/messages",
            {
                "items": [
                    {
                        "id": "msg-1",
                        "roomId": "room-1",
                        "personId": "person-1",
                        "created": "2026-01-01T00:00:00Z",
                        "files": "https://files.example.test/a.txt",
                    }
                ]
            },
        )
        source = WebexSource(WebexClient("token", transport=transport, sleeper=lambda _: None))

        with self.assertRaisesRegex(WebexApiError, "files"):
            tuple(source.list_messages(type("Conversation", (), {"source_id": "room-1"})()))

    def test_rejects_non_string_message_file_entries(self) -> None:
        transport = FakeWebexTransport()
        transport.add(
            "/messages",
            {
                "items": [
                    {
                        "id": "msg-1",
                        "roomId": "room-1",
                        "personId": "person-1",
                        "created": "2026-01-01T00:00:00Z",
                        "files": ["https://files.example.test/a.txt", 123],
                    }
                ]
            },
        )
        source = WebexSource(WebexClient("token", transport=transport, sleeper=lambda _: None))

        with self.assertRaisesRegex(WebexApiError, "files"):
            tuple(source.list_messages(type("Conversation", (), {"source_id": "room-1"})()))

    def test_rejects_non_http_message_file_urls(self) -> None:
        transport = FakeWebexTransport()
        transport.add(
            "/messages",
            {
                "items": [
                    {
                        "id": "msg-1",
                        "roomId": "room-1",
                        "personId": "person-1",
                        "created": "2026-01-01T00:00:00Z",
                        "files": ["file:///private/tmp/a.txt"],
                    }
                ]
            },
        )
        source = WebexSource(WebexClient("token", transport=transport, sleeper=lambda _: None))

        with self.assertRaisesRegex(WebexApiError, "HTTP\\(S\\) URLs"):
            tuple(source.list_messages(type("Conversation", (), {"source_id": "room-1"})()))

    def test_rejects_relative_message_file_urls(self) -> None:
        transport = FakeWebexTransport()
        transport.add(
            "/messages",
            {
                "items": [
                    {
                        "id": "msg-1",
                        "roomId": "room-1",
                        "personId": "person-1",
                        "created": "2026-01-01T00:00:00Z",
                        "files": ["/files/a.txt"],
                    }
                ]
            },
        )
        source = WebexSource(WebexClient("token", transport=transport, sleeper=lambda _: None))

        with self.assertRaisesRegex(WebexApiError, "HTTP\\(S\\) URLs"):
            tuple(source.list_messages(type("Conversation", (), {"source_id": "room-1"})()))

    def test_rejects_invalid_mentioned_people_payload(self) -> None:
        transport = FakeWebexTransport()
        transport.add(
            "/messages",
            {
                "items": [
                    {
                        "id": "msg-1",
                        "roomId": "room-1",
                        "personId": "person-1",
                        "created": "2026-01-01T00:00:00Z",
                        "mentionedPeople": ["person-1", ""],
                    }
                ]
            },
        )
        source = WebexSource(WebexClient("token", transport=transport, sleeper=lambda _: None))

        with self.assertRaisesRegex(WebexApiError, "mentionedPeople"):
            tuple(source.list_messages(type("Conversation", (), {"source_id": "room-1"})()))

    def test_rejects_non_string_message_body_fields(self) -> None:
        transport = FakeWebexTransport()
        transport.add(
            "/messages",
            {
                "items": [
                    {
                        "id": "msg-1",
                        "roomId": "room-1",
                        "personId": "person-1",
                        "created": "2026-01-01T00:00:00Z",
                        "text": {"unexpected": "object"},
                    }
                ]
            },
        )
        source = WebexSource(WebexClient("token", transport=transport, sleeper=lambda _: None))

        with self.assertRaisesRegex(WebexApiError, "text"):
            tuple(source.list_messages(type("Conversation", (), {"source_id": "room-1"})()))

    def test_rejects_non_string_message_identity_fields(self) -> None:
        transport = FakeWebexTransport()
        transport.add(
            "/messages",
            {
                "items": [
                    {
                        "id": "msg-1",
                        "roomId": 123,
                        "personId": "person-1",
                        "created": "2026-01-01T00:00:00Z",
                        "text": "hello",
                    }
                ]
            },
        )
        source = WebexSource(WebexClient("token", transport=transport, sleeper=lambda _: None))

        with self.assertRaisesRegex(WebexApiError, "roomId"):
            tuple(source.list_messages(type("Conversation", (), {"source_id": "room-1"})()))

    def test_rejects_invalid_optional_room_datetime_as_webex_error(self) -> None:
        transport = FakeWebexTransport()
        transport.add(
            "/rooms",
            {
                "items": [
                    {
                        "id": "room-1",
                        "title": "General",
                        "type": "group",
                        "created": "not-a-date",
                    }
                ]
            },
        )
        source = WebexSource(WebexClient("token", transport=transport, sleeper=lambda _: None))

        with self.assertRaisesRegex(WebexApiError, "datetime"):
            tuple(source.list_conversations())

    def test_rejects_unknown_room_type_instead_of_treating_as_space(self) -> None:
        transport = FakeWebexTransport()
        transport.add(
            "/rooms",
            {
                "items": [
                    {
                        "id": "room-1",
                        "title": "General",
                        "type": "unknown",
                    }
                ]
            },
        )
        source = WebexSource(WebexClient("token", transport=transport, sleeper=lambda _: None))

        with self.assertRaisesRegex(WebexApiError, "type"):
            tuple(source.list_conversations())

    def test_rejects_non_string_room_title(self) -> None:
        transport = FakeWebexTransport()
        transport.add(
            "/rooms",
            {
                "items": [
                    {
                        "id": "room-1",
                        "title": {"unexpected": "object"},
                        "type": "group",
                    }
                ]
            },
        )
        source = WebexSource(WebexClient("token", transport=transport, sleeper=lambda _: None))

        with self.assertRaisesRegex(WebexApiError, "title"):
            tuple(source.list_conversations())

    def test_rejects_non_boolean_room_lock_flag(self) -> None:
        transport = FakeWebexTransport()
        transport.add(
            "/rooms",
            {
                "items": [
                    {
                        "id": "room-1",
                        "title": "General",
                        "type": "group",
                        "isLocked": "false",
                    }
                ]
            },
        )
        source = WebexSource(WebexClient("token", transport=transport, sleeper=lambda _: None))

        with self.assertRaisesRegex(WebexApiError, "isLocked"):
            tuple(source.list_conversations())

    def test_normalizes_room_last_activity_metadata_to_utc(self) -> None:
        transport = FakeWebexTransport()
        transport.add(
            "/rooms",
            {
                "items": [
                    {
                        "id": "room-1",
                        "title": "General",
                        "type": "group",
                        "lastActivity": "2026-01-01T01:00:00+01:00",
                    }
                ]
            },
        )
        source = WebexSource(WebexClient("token", transport=transport, sleeper=lambda _: None))

        conversations = tuple(source.list_conversations())

        self.assertEqual(conversations[0].metadata["last_activity"], "2026-01-01T00:00:00Z")

    def test_rejects_non_boolean_membership_flags(self) -> None:
        transport = FakeWebexTransport()
        transport.add(
            "/rooms",
            {
                "items": [
                    {
                        "id": "room-1",
                        "title": "General",
                        "type": "group",
                    }
                ]
            },
        )
        transport.add(
            "/memberships",
            {
                "items": [
                    {
                        "id": "membership-1",
                        "roomId": "room-1",
                        "personId": "person-1",
                        "personDisplayName": "Ada",
                        "isDeleted": "false",
                    }
                ]
            },
        )
        source = WebexSource(WebexClient("token", transport=transport, sleeper=lambda _: None))

        with self.assertRaisesRegex(WebexApiError, "isDeleted"):
            tuple(source.list_memberships())

    def test_rejects_membership_rows_missing_required_identity(self) -> None:
        transport = FakeWebexTransport()
        transport.add(
            "/rooms",
            {
                "items": [
                    {
                        "id": "room-1",
                        "title": "General",
                        "type": "group",
                    }
                ]
            },
        )
        transport.add(
            "/memberships",
            {
                "items": [
                    {
                        "id": "membership-1",
                        "roomId": "room-1",
                        "personDisplayName": "Ada",
                    }
                ]
            },
        )
        source = WebexSource(WebexClient("token", transport=transport, sleeper=lambda _: None))

        with self.assertRaisesRegex(WebexApiError, "personId"):
            tuple(source.list_memberships())

    def test_rejects_non_string_membership_optional_identity_fields(self) -> None:
        transport = FakeWebexTransport()
        transport.add(
            "/rooms",
            {
                "items": [
                    {
                        "id": "room-1",
                        "title": "General",
                        "type": "group",
                    }
                ]
            },
        )
        transport.add(
            "/memberships",
            {
                "items": [
                    {
                        "id": "membership-1",
                        "roomId": "room-1",
                        "personId": "person-1",
                        "personEmail": ["ada@example.com"],
                    }
                ]
            },
        )
        source = WebexSource(WebexClient("token", transport=transport, sleeper=lambda _: None))

        with self.assertRaisesRegex(WebexApiError, "personEmail"):
            tuple(source.list_participants())

    def test_message_window_passes_before_and_filters_locally(self) -> None:
        transport = FakeWebexTransport()
        transport.add(
            "/messages",
            {
                "items": [
                    {
                        "id": "too-old",
                        "roomId": "room-1",
                        "personId": "person-1",
                        "created": "2025-12-31T23:59:59Z",
                        "text": "old",
                    },
                    {
                        "id": "inside",
                        "roomId": "room-1",
                        "personId": "person-1",
                        "created": "2026-01-01T00:00:00Z",
                        "text": "inside",
                    },
                    {
                        "id": "at-before",
                        "roomId": "room-1",
                        "personId": "person-1",
                        "created": "2026-02-01T00:00:00Z",
                        "text": "boundary",
                    },
                ]
            },
        )
        source = WebexSource(
            WebexClient("token", transport=transport, sleeper=lambda _: None),
            message_since=datetime(2026, 1, 1, tzinfo=timezone.utc),
            message_before=datetime(2026, 2, 1, tzinfo=timezone.utc),
        )

        messages = tuple(source.list_messages(type("Conversation", (), {"source_id": "room-1"})()))
        query = parse_qs(urlparse(transport.calls[0]).query)

        self.assertEqual([message.source_id for message in messages], ["inside"])
        self.assertEqual(query["afterDate"], ["2026-01-01T00:00:00Z"])
        self.assertEqual(query["before"], ["2026-02-01T00:00:00Z"])

    def test_message_since_sends_after_date_api_param(self) -> None:
        transport = FakeWebexTransport()
        transport.add("/messages", {"items": []})
        source = WebexSource(
            WebexClient("token", transport=transport, sleeper=lambda _: None),
            message_since=datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc),
        )

        tuple(source.list_messages(type("Conversation", (), {"source_id": "room-1"})()))
        query = parse_qs(urlparse(transport.calls[0]).query)

        self.assertEqual(query["afterDate"], ["2026-03-15T12:00:00Z"])
        self.assertNotIn("before", query)

    def test_next_link_parser_handles_missing_or_next_links(self) -> None:
        self.assertIsNone(_next_link({}))
        self.assertEqual(
            _next_link({"link": '<https://webexapis.com/v1/messages?page=2>; rel="next"'}),
            "https://webexapis.com/v1/messages?page=2",
        )

    def test_filename_from_url_ignores_query_and_decodes_path(self) -> None:
        self.assertEqual(
            _filename_from_url("https://files.example.test/folder/hello%20world.txt?token=secret"),
            "hello world.txt",
        )
        self.assertEqual(_filename_from_url("https://files.example.test/"), "attachment")

    def test_source_factory_requires_auth_secret(self) -> None:
        with self.assertRaises(SecretResolutionError):
            source_from_config(EndpointConfig(kind="webex", settings={}))

    def test_source_factory_reads_room_ids(self) -> None:
        config = EndpointConfig(
            kind="webex",
            settings={
                "auth": "token",
                "scope": "selected_rooms",
                "room_ids": [" room-1 ", "room-2"],
                "max_page_size": 50,
                "message_since": "2026-01-01T00:00:00Z",
                "message_before": "2026-02-01T00:00:00Z",
            },
        )

        source = source_from_config(config)

        self.assertEqual(source.room_ids, ("room-1", "room-2"))
        self.assertEqual(source.max_page_size, 50)
        self.assertEqual(source.message_since, datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(source.message_before, datetime(2026, 2, 1, tzinfo=timezone.utc))

    def test_source_factory_rejects_blank_room_ids(self) -> None:
        config = EndpointConfig(
            kind="webex",
            settings={"auth": "token", "scope": "selected_rooms", "room_ids": ["room-1", "  "]},
        )

        with self.assertRaisesRegex(ValueError, "room_ids"):
            source_from_config(config)

    def test_source_factory_rejects_non_string_tuple_room_ids(self) -> None:
        config = EndpointConfig(
            kind="webex",
            settings={"auth": "token", "scope": "selected_rooms", "room_ids": ("room-1", 123)},
        )

        with self.assertRaisesRegex(ValueError, "room_ids"):
            source_from_config(config)

    def test_source_factory_rejects_boolean_max_page_size(self) -> None:
        config = EndpointConfig(kind="webex", settings={"auth": "token", "max_page_size": True})

        with self.assertRaisesRegex(ValueError, "max_page_size"):
            source_from_config(config)

    def test_source_factory_rejects_invalid_message_window(self) -> None:
        config = EndpointConfig(
            kind="webex",
            settings={
                "auth": "token",
                "message_since": "2026-02-01T00:00:00Z",
                "message_before": "2026-01-01T00:00:00Z",
            },
        )

        with self.assertRaisesRegex(ValueError, "message_since"):
            source_from_config(config)

    def test_source_factory_requires_timezone_for_message_window(self) -> None:
        config = EndpointConfig(
            kind="webex",
            settings={
                "auth": "token",
                "message_since": "2026-01-01T00:00:00",
            },
        )

        with self.assertRaisesRegex(ValueError, "timezone"):
            source_from_config(config)

    def test_selected_rooms_scope_requires_room_ids(self) -> None:
        config = EndpointConfig(
            kind="webex",
            settings={"auth": "token", "scope": "selected_rooms", "room_ids": []},
        )

        with self.assertRaisesRegex(ValueError, "selected_rooms"):
            source_from_config(config)

    def test_organization_scope_is_not_silently_treated_as_user_scope(self) -> None:
        config = EndpointConfig(kind="webex", settings={"auth": "token", "scope": "organization"})

        with self.assertRaisesRegex(ValueError, "organization scope"):
            source_from_config(config)


if __name__ == "__main__":
    unittest.main()
