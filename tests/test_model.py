from __future__ import annotations

import unittest
from datetime import datetime, timezone

from exodus_agent.model import Attachment, ConversationMembership, Message, Participant


class ModelTests(unittest.TestCase):
    def test_message_from_json_treats_naive_datetime_as_utc(self) -> None:
        message = Message.from_json(
            {
                "source_id": "msg-1",
                "conversation_id": "space-1",
                "author_id": "user-1",
                "created_at": "2026-01-01T12:00:00",
            }
        )

        self.assertEqual(message.created_at, datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc))

    def test_message_from_json_rejects_invalid_required_datetime_with_field_context(self) -> None:
        with self.assertRaisesRegex(ValueError, "Invalid datetime field: created_at"):
            Message.from_json(
                {
                    "source_id": "msg-1",
                    "conversation_id": "space-1",
                    "author_id": "user-1",
                    "created_at": "not-a-date",
                }
            )

    def test_message_from_json_rejects_invalid_optional_datetime_with_field_context(self) -> None:
        with self.assertRaisesRegex(ValueError, "Invalid datetime field: edited_at"):
            Message.from_json(
                {
                    "source_id": "msg-1",
                    "conversation_id": "space-1",
                    "author_id": "user-1",
                    "created_at": "2026-01-01T12:00:00Z",
                    "edited_at": "bad-edited-at",
                }
            )

    def test_message_from_json_rejects_non_string_datetime_with_field_context(self) -> None:
        with self.assertRaisesRegex(ValueError, "Expected datetime string field: created_at"):
            Message.from_json(
                {
                    "source_id": "msg-1",
                    "conversation_id": "space-1",
                    "author_id": "user-1",
                    "created_at": 123,
                }
            )

    def test_participant_from_json_rejects_string_boolean(self) -> None:
        with self.assertRaisesRegex(ValueError, "is_deleted"):
            Participant.from_json(
                {
                    "source_id": "user-1",
                    "display_name": "Ada",
                    "is_deleted": "false",
                }
            )

    def test_membership_from_json_rejects_string_boolean(self) -> None:
        with self.assertRaisesRegex(ValueError, "is_moderator"):
            ConversationMembership.from_json(
                {
                    "source_id": "membership-1",
                    "conversation_id": "space-1",
                    "participant_id": "user-1",
                    "is_moderator": "false",
                }
            )

    def test_attachment_from_json_rejects_boolean_size_bytes(self) -> None:
        with self.assertRaisesRegex(ValueError, "size_bytes"):
            Attachment.from_json(
                {
                    "source_id": "file-1",
                    "filename": "notes.txt",
                    "size_bytes": True,
                }
            )

    def test_attachment_from_json_rejects_negative_size_bytes(self) -> None:
        with self.assertRaisesRegex(ValueError, "non-negative.*size_bytes"):
            Attachment.from_json(
                {
                    "source_id": "file-1",
                    "filename": "notes.txt",
                    "size_bytes": -1,
                }
            )

    def test_message_from_json_rejects_non_list_attachments(self) -> None:
        with self.assertRaisesRegex(ValueError, "attachments must be a list"):
            Message.from_json(
                {
                    "source_id": "msg-1",
                    "conversation_id": "space-1",
                    "author_id": "user-1",
                    "created_at": "2026-01-01T12:00:00Z",
                    "attachments": {"source_id": "file-1"},
                }
            )

    def test_message_from_json_rejects_non_object_attachment_rows(self) -> None:
        with self.assertRaisesRegex(ValueError, "attachment row 1"):
            Message.from_json(
                {
                    "source_id": "msg-1",
                    "conversation_id": "space-1",
                    "author_id": "user-1",
                    "created_at": "2026-01-01T12:00:00Z",
                    "attachments": [
                        {"source_id": "file-1", "filename": "notes.txt"},
                        "bad",
                    ],
                }
            )

    def test_missing_metadata_defaults_to_empty_object(self) -> None:
        message = Message.from_json(
            {
                "source_id": "msg-1",
                "conversation_id": "space-1",
                "author_id": "user-1",
                "created_at": "2026-01-01T12:00:00Z",
            }
        )

        self.assertEqual(message.metadata, {})

    def test_rejects_non_object_metadata(self) -> None:
        with self.assertRaisesRegex(ValueError, "metadata"):
            Message.from_json(
                {
                    "source_id": "msg-1",
                    "conversation_id": "space-1",
                    "author_id": "user-1",
                    "created_at": "2026-01-01T12:00:00Z",
                    "metadata": ["bad"],
                }
            )

    def test_optional_empty_string_fields_remain_none(self) -> None:
        message = Message.from_json(
            {
                "source_id": "msg-1",
                "conversation_id": "space-1",
                "author_id": "",
                "created_at": "2026-01-01T12:00:00Z",
                "text": "",
            }
        )

        self.assertIsNone(message.author_id)
        self.assertIsNone(message.text)

    def test_required_whitespace_string_fields_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "source_id"):
            Message.from_json(
                {
                    "source_id": "   ",
                    "conversation_id": "space-1",
                    "author_id": "user-1",
                    "created_at": "2026-01-01T12:00:00Z",
                }
            )

    def test_optional_whitespace_string_fields_become_none(self) -> None:
        message = Message.from_json(
            {
                "source_id": "msg-1",
                "conversation_id": "space-1",
                "author_id": "   ",
                "created_at": "2026-01-01T12:00:00Z",
                "text": "   ",
            }
        )

        self.assertIsNone(message.author_id)
        self.assertIsNone(message.text)

    def test_message_from_json_rejects_non_string_optional_fields(self) -> None:
        with self.assertRaisesRegex(ValueError, "parent_id"):
            Message.from_json(
                {
                    "source_id": "msg-1",
                    "conversation_id": "space-1",
                    "author_id": "user-1",
                    "created_at": "2026-01-01T12:00:00Z",
                    "parent_id": 123,
                }
            )

    def test_attachment_from_json_rejects_non_string_optional_fields(self) -> None:
        with self.assertRaisesRegex(ValueError, "local_path"):
            Attachment.from_json(
                {
                    "source_id": "file-1",
                    "filename": "notes.txt",
                    "local_path": 123,
                }
            )


if __name__ == "__main__":
    unittest.main()
