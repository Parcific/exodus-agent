from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from exodus_agent.archive import Archive, _safe_filename
from exodus_agent.model import Conversation, ConversationKind, ConversationMembership, Message, Participant, Workspace


class ArchiveTests(unittest.TestCase):
    def test_archive_writes_portable_jsonl_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Archive(Path(tmp) / "archive")
            archive.initialize(source_kind="webex", target_kind="telegram", name="demo")
            archive.write_workspace(Workspace(source_id="org-1", source_kind="webex"))
            archive.write_conversations(
                [Conversation(source_id="room/1", kind=ConversationKind.SPACE, title="Room")]
            )
            archive.write_participants([Participant(source_id="user-1", display_name="Ada")])
            archive.write_memberships(
                [
                    ConversationMembership(
                        source_id="membership-1",
                        conversation_id="room/1",
                        participant_id="user-1",
                        display_name="Ada",
                    )
                ]
            )
            archive.write_messages(
                "room/1",
                [
                    Message(
                        source_id="msg-1",
                        conversation_id="room/1",
                        author_id="user-1",
                        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                        text="hello",
                    )
                ],
            )

            manifest = json.loads((archive.root / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(archive.read_manifest()["name"], "demo")
            self.assertEqual(manifest["schema_version"], 1)
            self.assertEqual(manifest["source_kind"], "webex")
            self.assertEqual(manifest["target_kind"], "telegram")
            self.assertEqual(
                archive.read_jsonl(f"messages/{_safe_filename('room/1')}.jsonl")[0]["text"],
                "hello",
            )
            self.assertEqual(archive.read_conversations()[0].source_id, "room/1")
            self.assertEqual(archive.read_participants()[0].display_name, "Ada")
            self.assertEqual(archive.read_memberships()[0].participant_id, "user-1")
            self.assertEqual(archive.read_messages("room/1")[0].source_id, "msg-1")

    def test_safe_filename_preserves_identity_for_similar_ids(self) -> None:
        self.assertNotEqual(_safe_filename("room/1"), _safe_filename("room_1"))

    def test_write_attachment_blob_deduplicates_by_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Archive(Path(tmp) / "archive")

            first = archive.write_attachment_blob(
                source_id="file-1",
                filename="notes.txt",
                content=b"hello",
            )
            second = archive.write_attachment_blob(
                source_id="file-2",
                filename="notes.txt",
                content=b"hello",
            )

            self.assertEqual(first.local_path, second.local_path)
            self.assertEqual(first.sha256, second.sha256)
            self.assertEqual((archive.root / first.local_path).read_bytes(), b"hello")

    def test_write_attachment_blob_rejects_existing_directory_blob_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Archive(Path(tmp) / "archive")
            existing = archive.write_attachment_blob(
                source_id="file-1",
                filename="notes.txt",
                content=b"hello",
            )
            blob_path = archive.root / existing.local_path
            blob_path.unlink()
            blob_path.mkdir()

            with self.assertRaisesRegex(ValueError, "attachment blob path must be a file"):
                archive.write_attachment_blob(
                    source_id="file-2",
                    filename="notes.txt",
                    content=b"hello",
                )

    def test_jsonl_writes_replace_previous_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Archive(Path(tmp) / "archive")
            archive.initialize(source_kind="webex", target_kind="telegram", name="demo")

            archive.write_conversations(
                [Conversation(source_id="room-1", kind=ConversationKind.SPACE, title="First")]
            )
            archive.write_conversations(
                [Conversation(source_id="room-2", kind=ConversationKind.SPACE, title="Second")]
            )
            archive.write_messages(
                "room-1",
                [
                    Message(
                        source_id="old-msg",
                        conversation_id="room-1",
                        author_id="user-1",
                        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                        text="old",
                    )
                ],
            )
            archive.write_messages(
                "room-1",
                [
                    Message(
                        source_id="new-msg",
                        conversation_id="room-1",
                        author_id="user-1",
                        created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
                        text="new",
                    )
                ],
            )

            self.assertEqual([conversation.source_id for conversation in archive.read_conversations()], ["room-2"])
            self.assertEqual([message.source_id for message in archive.read_messages("room-1")], ["new-msg"])

    def test_jsonl_write_rejects_existing_directory_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Archive(Path(tmp) / "archive")
            archive.initialize(source_kind="webex", target_kind="telegram", name="demo")
            path = archive.root / "conversations" / "conversations.jsonl"
            path.mkdir()

            with self.assertRaisesRegex(ValueError, "JSONL path must be a file"):
                archive.write_conversations(
                    [Conversation(source_id="room-1", kind=ConversationKind.SPACE)]
                )

    def test_json_write_rejects_existing_directory_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Archive(Path(tmp) / "archive")
            archive.root.mkdir(parents=True)
            manifest_path = archive.root / "manifest.json"
            manifest_path.mkdir()

            with self.assertRaisesRegex(ValueError, "JSON path must be a file"):
                archive.initialize(source_kind="webex", target_kind="telegram", name="demo")

    def test_write_messages_rejects_mismatched_conversation_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Archive(Path(tmp) / "archive")
            archive.initialize(source_kind="webex", target_kind="telegram", name="demo")

            with self.assertRaisesRegex(ValueError, "does not match archive conversation"):
                archive.write_messages(
                    "room-1",
                    [
                        Message(
                            source_id="msg-1",
                            conversation_id="room-2",
                            author_id="user-1",
                            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                            text="wrong room",
                        )
                    ],
                )

            self.assertEqual(archive.read_jsonl(f"messages/{_safe_filename('room-1')}.jsonl"), [])

    def test_read_messages_rejects_mismatched_conversation_id_from_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Archive(Path(tmp) / "archive")
            archive.initialize(source_kind="webex", target_kind="telegram", name="demo")
            path = archive.root / "messages" / f"{_safe_filename('room-1')}.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "source_id": "msg-1",
                        "conversation_id": "room-2",
                        "author_id": "user-1",
                        "created_at": "2026-01-01T00:00:00+00:00",
                        "text": "wrong room",
                        "attachments": [],
                        "metadata": {},
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "does not match requested conversation"):
                archive.read_messages("room-1")

    def test_resolve_path_rejects_archive_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Archive(Path(tmp) / "archive")

            with self.assertRaisesRegex(ValueError, "within archive root"):
                archive.resolve_path("../escape.txt")

    def test_read_jsonl_rejects_non_object_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Archive(Path(tmp) / "archive")
            archive.initialize(source_kind="webex", target_kind="telegram", name="demo")
            path = archive.root / "conversations" / "conversations.jsonl"
            path.write_text('["not", "object"]\n', encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "JSONL row must be an object"):
                archive.read_conversations()

    def test_read_jsonl_rejects_invalid_json_with_row_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Archive(Path(tmp) / "archive")
            archive.initialize(source_kind="webex", target_kind="telegram", name="demo")
            path = archive.root / "conversations" / "conversations.jsonl"
            path.write_text(
                json.dumps({"source_id": "room-1", "kind": "space"}) + "\n{not-json}\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, r"conversations/conversations.jsonl:2"):
                archive.read_conversations()

    def test_read_jsonl_rejects_non_utf8_with_file_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Archive(Path(tmp) / "archive")
            archive.initialize(source_kind="webex", target_kind="telegram", name="demo")
            path = archive.root / "conversations" / "conversations.jsonl"
            path.write_bytes(b"\xff")

            with self.assertRaisesRegex(ValueError, r"not valid UTF-8: conversations/conversations.jsonl"):
                archive.read_conversations()

    def test_read_jsonl_rejects_directory_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Archive(Path(tmp) / "archive")
            archive.initialize(source_kind="webex", target_kind="telegram", name="demo")
            path = archive.root / "conversations" / "conversations.jsonl"
            path.mkdir()

            with self.assertRaisesRegex(ValueError, r"JSONL path must be a file: conversations/conversations.jsonl"):
                archive.read_conversations()

    def test_read_manifest_rejects_invalid_json_with_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Archive(Path(tmp) / "archive")
            archive.initialize(source_kind="webex", target_kind="telegram", name="demo")
            (archive.root / "manifest.json").write_text("{not-json}", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, r"Archive manifest is not valid JSON: manifest.json"):
                archive.read_manifest()

    def test_read_manifest_rejects_non_utf8_with_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Archive(Path(tmp) / "archive")
            archive.initialize(source_kind="webex", target_kind="telegram", name="demo")
            (archive.root / "manifest.json").write_bytes(b"\xff")

            with self.assertRaisesRegex(ValueError, r"Archive manifest is not valid UTF-8: manifest.json"):
                archive.read_manifest()

    def test_initialize_reset_clears_existing_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Archive(Path(tmp) / "archive")
            archive.initialize(source_kind="webex", target_kind="telegram", name="first")
            archive.write_workspace(Workspace(source_id="org-1", source_kind="webex"))

            archive.initialize(
                source_kind="webex",
                target_kind="telegram",
                name="second",
                reset=True,
            )

            self.assertEqual(archive.read_jsonl("workspaces.jsonl"), [])
            self.assertEqual(archive.read_manifest()["name"], "second")

    def test_initialize_reset_refuses_non_archive_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Archive(Path(tmp) / "not-an-archive")
            archive.root.mkdir()
            (archive.root / "important.txt").write_text("keep", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "not an Exodus archive"):
                archive.initialize(
                    source_kind="webex",
                    target_kind="telegram",
                    name="demo",
                    reset=True,
                )

            self.assertEqual((archive.root / "important.txt").read_text(encoding="utf-8"), "keep")

    def test_initialize_reset_refuses_invalid_archive_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Archive(Path(tmp) / "not-an-archive")
            archive.root.mkdir()
            (archive.root / ".exodus-archive").write_text("not json", encoding="utf-8")
            (archive.root / "important.txt").write_text("keep", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "not an Exodus archive"):
                archive.initialize(
                    source_kind="webex",
                    target_kind="telegram",
                    name="demo",
                    reset=True,
                )

            self.assertEqual((archive.root / "important.txt").read_text(encoding="utf-8"), "keep")

    def test_initialize_reset_allows_empty_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Archive(Path(tmp) / "empty")
            archive.root.mkdir()

            archive.initialize(
                source_kind="webex",
                target_kind="telegram",
                name="demo",
                reset=True,
            )

            self.assertEqual(archive.read_manifest()["name"], "demo")


if __name__ == "__main__":
    unittest.main()
