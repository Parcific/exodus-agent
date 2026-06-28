from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from exodus_agent.archive import Archive, _safe_filename
from exodus_agent.job import JobStore
from exodus_agent.model import Attachment, Conversation, ConversationKind, ConversationMembership, Message, Participant, Workspace
from exodus_agent.runner import export_dry_run


class FakeSource:
    def get_workspace(self) -> Workspace:
        return Workspace(source_id="workspace-1", source_kind="fake", display_name="Fake")

    def list_conversations(self) -> tuple[Conversation, ...]:
        return (Conversation(source_id="room/1", kind=ConversationKind.SPACE, title="Room"),)

    def list_participants(self) -> tuple[Participant, ...]:
        return (Participant(source_id="user-1", display_name="Ada"),)

    def list_memberships(self) -> tuple[ConversationMembership, ...]:
        return (
            ConversationMembership(
                source_id="membership-1",
                conversation_id="room/1",
                participant_id="user-1",
            ),
        )

    def list_messages(self, conversation: Conversation) -> tuple[Message, ...]:
        return (
            Message(
                source_id="msg-1",
                conversation_id=conversation.source_id,
                author_id="user-1",
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                text="hello",
                attachments=(Attachment(source_id="file-1", filename="notes.txt"),),
            ),
        )

    def download_attachment(self, attachment: Attachment) -> bytes:
        return f"content:{attachment.filename}".encode()


class FakeSourceWithoutMedia:
    def get_workspace(self) -> Workspace:
        return Workspace(source_id="workspace-1", source_kind="fake", display_name="Fake")

    def list_conversations(self) -> tuple[Conversation, ...]:
        return (Conversation(source_id="room/1", kind=ConversationKind.SPACE, title="Room"),)

    def list_participants(self) -> tuple[Participant, ...]:
        return (Participant(source_id="user-1", display_name="Ada"),)

    def list_messages(self, conversation: Conversation) -> tuple[Message, ...]:
        return (
            Message(
                source_id="msg-1",
                conversation_id=conversation.source_id,
                author_id="user-1",
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                text="hello",
                attachments=(Attachment(source_id="file-1", filename="notes.txt"),),
            ),
        )


class FakeSourceWithBadMembershipConversation(FakeSource):
    def list_memberships(self) -> tuple[ConversationMembership, ...]:
        return (
            ConversationMembership(
                source_id="membership-1",
                conversation_id="missing-room",
                participant_id="user-1",
            ),
        )


class FakeSourceWithBadMembershipParticipant(FakeSource):
    def list_memberships(self) -> tuple[ConversationMembership, ...]:
        return (
            ConversationMembership(
                source_id="membership-1",
                conversation_id="room/1",
                participant_id="missing-user",
            ),
        )


class FakeSourceWithDuplicateConversations(FakeSource):
    def list_conversations(self) -> tuple[Conversation, ...]:
        return (
            Conversation(source_id="room/1", kind=ConversationKind.SPACE, title="Room"),
            Conversation(source_id="room/1", kind=ConversationKind.SPACE, title="Duplicate"),
        )


class FakeSourceWithPaddedConversationSourceId(FakeSource):
    def list_conversations(self) -> tuple[Conversation, ...]:
        return (Conversation(source_id=" room/1 ", kind=ConversationKind.SPACE, title="Room"),)


class FakeSourceWithPaddedMembershipConversationId(FakeSource):
    def list_memberships(self) -> tuple[ConversationMembership, ...]:
        return (
            ConversationMembership(
                source_id="membership-1",
                conversation_id=" room/1 ",
                participant_id="user-1",
            ),
        )


class FakeSourceWithDuplicateMessages(FakeSource):
    def list_conversations(self) -> tuple[Conversation, ...]:
        return (
            Conversation(source_id="room/1", kind=ConversationKind.SPACE, title="Room"),
            Conversation(source_id="room/2", kind=ConversationKind.SPACE, title="Other"),
        )

    def list_messages(self, conversation: Conversation) -> tuple[Message, ...]:
        return (
            Message(
                source_id="msg-1",
                conversation_id=conversation.source_id,
                author_id="user-1",
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                text="duplicate",
            ),
        )


class FakeSourceWithWrongMessageConversation(FakeSource):
    def list_messages(self, conversation: Conversation) -> tuple[Message, ...]:
        return (
            Message(
                source_id="msg-1",
                conversation_id="wrong-room",
                author_id="user-1",
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                text="wrong room",
            ),
        )


class FakeSourceWithMissingMessageParent(FakeSource):
    def list_messages(self, conversation: Conversation) -> tuple[Message, ...]:
        return (
            Message(
                source_id="reply-1",
                conversation_id=conversation.source_id,
                author_id="user-1",
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                parent_id="missing-parent",
                text="reply",
            ),
        )


class FakeSourceWithClippedThreadRoot:
    """Simulates a source where message_since clips the thread root but not the reply.

    The root message (root-msg) predates message_since and is excluded from the
    returned list. The reply still references root-msg as its parent_id.  This
    mirrors exactly what WebexSource does when message_since is set and the thread
    root falls before the window.
    """

    _EXCLUDED_ROOT_ID = "root-msg"

    def get_workspace(self) -> Workspace:
        return Workspace(source_id="workspace-1", source_kind="fake", display_name="Fake")

    def list_conversations(self) -> tuple[Conversation, ...]:
        return (Conversation(source_id="room/1", kind=ConversationKind.SPACE, title="Room"),)

    def list_participants(self) -> tuple[Participant, ...]:
        return (Participant(source_id="user-1", display_name="Ada"),)

    def list_messages(self, conversation: Conversation) -> tuple[Message, ...]:
        # Only the reply is returned; the root was clipped by message_since.
        return (
            Message(
                source_id="reply-1",
                conversation_id=conversation.source_id,
                author_id="user-1",
                created_at=datetime(2026, 6, 2, tzinfo=timezone.utc),
                parent_id=self._EXCLUDED_ROOT_ID,
                text="reply to clipped root",
            ),
        )

    def get_excluded_root_ids(self, conversation: Conversation) -> frozenset[str]:
        return frozenset({self._EXCLUDED_ROOT_ID})


class FakeSourceWithSelfParentMessage(FakeSource):
    def list_messages(self, conversation: Conversation) -> tuple[Message, ...]:
        return (
            Message(
                source_id="msg-1",
                conversation_id=conversation.source_id,
                author_id="user-1",
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                parent_id="msg-1",
                text="self parent",
            ),
        )


class FakeSourceWithBlankMessageSourceId(FakeSource):
    def list_messages(self, conversation: Conversation) -> tuple[Message, ...]:
        return (
            Message(
                source_id="",
                conversation_id=conversation.source_id,
                author_id="user-1",
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                text="blank id",
            ),
        )


class FakeSourceWithWhitespaceMessageSourceId(FakeSource):
    def list_messages(self, conversation: Conversation) -> tuple[Message, ...]:
        return (
            Message(
                source_id="   ",
                conversation_id=conversation.source_id,
                author_id="user-1",
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                text="whitespace id",
            ),
        )


class FakeSourceWithBlankAttachmentSourceId(FakeSource):
    def list_messages(self, conversation: Conversation) -> tuple[Message, ...]:
        return (
            Message(
                source_id="msg-1",
                conversation_id=conversation.source_id,
                author_id="user-1",
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                text="blank attachment id",
                attachments=(Attachment(source_id="", filename="notes.txt", local_path="attachments/notes.txt"),),
            ),
        )


class FakeSourceWithBlankDownloadableAttachment(FakeSource):
    def __init__(self) -> None:
        self.downloads = 0

    def list_messages(self, conversation: Conversation) -> tuple[Message, ...]:
        return (
            Message(
                source_id="msg-1",
                conversation_id=conversation.source_id,
                author_id="user-1",
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                text="blank downloadable attachment id",
                attachments=(Attachment(source_id="", filename="notes.txt"),),
            ),
        )

    def download_attachment(self, attachment: Attachment) -> bytes:
        self.downloads += 1
        return b"should-not-download"


class RunnerTests(unittest.TestCase):
    def test_export_dry_run_writes_archive_and_job_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = Archive(root / "archive")
            job_store = JobStore(root / "jobs" / "job-1")

            result = export_dry_run(
                job_id="job-1",
                archive=archive,
                job_store=job_store,
                source=FakeSource(),
                source_kind="fake",
                target_kind="telegram",
                name="demo",
            )

            self.assertEqual(result.messages, 1)
            self.assertEqual(result.attachments, 1)
            self.assertEqual(result.memberships, 1)
            message = archive.read_messages("room/1")[0]
            self.assertEqual(archive.read_memberships()[0].source_id, "membership-1")
            self.assertEqual(message.attachments[0].size_bytes, len(b"content:notes.txt"))
            self.assertTrue(message.attachments[0].local_path)
            self.assertEqual(
                archive.read_jsonl(f"messages/{_safe_filename('room/1')}.jsonl")[0]["source_id"],
                "msg-1",
            )
            self.assertEqual(
                [event["kind"] for event in job_store.read_events()],
                ["created", "phase_started", "phase_completed"],
            )

    def test_export_preserves_attachments_when_source_cannot_download_media(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = Archive(root / "archive")
            job_store = JobStore(root / "jobs" / "job-1")

            result = export_dry_run(
                job_id="job-1",
                archive=archive,
                job_store=job_store,
                source=FakeSourceWithoutMedia(),
                source_kind="fake",
                target_kind="telegram",
                name="demo",
            )

            message = archive.read_messages("room/1")[0]
            self.assertEqual(result.attachments, 1)
            self.assertIsNone(message.attachments[0].local_path)

    def test_repeated_export_replaces_canonical_archive_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = Archive(root / "archive")

            first = export_dry_run(
                job_id="job-1",
                archive=archive,
                job_store=JobStore(root / "jobs" / "job-1"),
                source=FakeSource(),
                source_kind="fake",
                target_kind="telegram",
                name="demo",
            )
            second = export_dry_run(
                job_id="job-2",
                archive=archive,
                job_store=JobStore(root / "jobs" / "job-2"),
                source=FakeSource(),
                source_kind="fake",
                target_kind="telegram",
                name="demo",
            )

            self.assertEqual(first.messages, 1)
            self.assertEqual(second.messages, 1)
            self.assertEqual(len(archive.read_conversations()), 1)
            self.assertEqual(len(archive.read_participants()), 1)
            self.assertEqual(len(archive.read_memberships()), 1)
            self.assertEqual(len(archive.read_messages("room/1")), 1)

    def test_export_rejects_membership_for_unknown_conversation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            with self.assertRaisesRegex(ValueError, "unknown conversation_id"):
                export_dry_run(
                    job_id="job-1",
                    archive=Archive(root / "archive"),
                    job_store=JobStore(root / "jobs" / "job-1"),
                    source=FakeSourceWithBadMembershipConversation(),
                    source_kind="fake",
                    target_kind="telegram",
                    name="demo",
                )

    def test_export_rejects_membership_for_unknown_participant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            with self.assertRaisesRegex(ValueError, "unknown participant_id"):
                export_dry_run(
                    job_id="job-1",
                    archive=Archive(root / "archive"),
                    job_store=JobStore(root / "jobs" / "job-1"),
                    source=FakeSourceWithBadMembershipParticipant(),
                    source_kind="fake",
                    target_kind="telegram",
                    name="demo",
                )

    def test_export_rejects_duplicate_conversation_source_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            with self.assertRaisesRegex(ValueError, "duplicate conversation source_id"):
                export_dry_run(
                    job_id="job-1",
                    archive=Archive(root / "archive"),
                    job_store=JobStore(root / "jobs" / "job-1"),
                    source=FakeSourceWithDuplicateConversations(),
                    source_kind="fake",
                    target_kind="telegram",
                    name="demo",
                )

    def test_export_rejects_padded_conversation_source_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            with self.assertRaisesRegex(ValueError, "conversation has invalid source_id"):
                export_dry_run(
                    job_id="job-1",
                    archive=Archive(root / "archive"),
                    job_store=JobStore(root / "jobs" / "job-1"),
                    source=FakeSourceWithPaddedConversationSourceId(),
                    source_kind="fake",
                    target_kind="telegram",
                    name="demo",
                )

    def test_export_rejects_padded_membership_conversation_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            with self.assertRaisesRegex(ValueError, "membership has invalid conversation_id"):
                export_dry_run(
                    job_id="job-1",
                    archive=Archive(root / "archive"),
                    job_store=JobStore(root / "jobs" / "job-1"),
                    source=FakeSourceWithPaddedMembershipConversationId(),
                    source_kind="fake",
                    target_kind="telegram",
                    name="demo",
                )

    def test_export_rejects_duplicate_message_source_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            with self.assertRaisesRegex(ValueError, "duplicate message source_id"):
                export_dry_run(
                    job_id="job-1",
                    archive=Archive(root / "archive"),
                    job_store=JobStore(root / "jobs" / "job-1"),
                    source=FakeSourceWithDuplicateMessages(),
                    source_kind="fake",
                    target_kind="telegram",
                    name="demo",
                )

    def test_export_rejects_message_for_unexpected_conversation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            with self.assertRaisesRegex(ValueError, "unexpected conversation_id"):
                export_dry_run(
                    job_id="job-1",
                    archive=Archive(root / "archive"),
                    job_store=JobStore(root / "jobs" / "job-1"),
                    source=FakeSourceWithWrongMessageConversation(),
                    source_kind="fake",
                    target_kind="telegram",
                    name="demo",
                )

    def test_export_succeeds_when_message_since_clips_thread_root(self) -> None:
        """Export must complete without ValueError when the parent of a reply was excluded
        by message_since (i.e. the thread root predates the export window).
        The reply itself must still appear in the written archive.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = Archive(root / "archive")
            job_store = JobStore(root / "jobs" / "job-1")

            result = export_dry_run(
                job_id="job-1",
                archive=archive,
                job_store=job_store,
                source=FakeSourceWithClippedThreadRoot(),
                source_kind="fake",
                target_kind="telegram",
                name="demo",
            )

            self.assertEqual(result.messages, 1)
            messages = archive.read_messages("room/1")
            self.assertEqual(len(messages), 1)
            self.assertEqual(messages[0].source_id, "reply-1")
            self.assertEqual(messages[0].parent_id, FakeSourceWithClippedThreadRoot._EXCLUDED_ROOT_ID)

    def test_export_rejects_message_with_unknown_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            with self.assertRaisesRegex(ValueError, "unknown parent_id"):
                export_dry_run(
                    job_id="job-1",
                    archive=Archive(root / "archive"),
                    job_store=JobStore(root / "jobs" / "job-1"),
                    source=FakeSourceWithMissingMessageParent(),
                    source_kind="fake",
                    target_kind="telegram",
                    name="demo",
                )

    def test_export_rejects_self_parent_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            with self.assertRaisesRegex(ValueError, "itself as parent_id"):
                export_dry_run(
                    job_id="job-1",
                    archive=Archive(root / "archive"),
                    job_store=JobStore(root / "jobs" / "job-1"),
                    source=FakeSourceWithSelfParentMessage(),
                    source_kind="fake",
                    target_kind="telegram",
                    name="demo",
                )

    def test_export_rejects_blank_message_source_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            with self.assertRaisesRegex(ValueError, "message has invalid source_id"):
                export_dry_run(
                    job_id="job-1",
                    archive=Archive(root / "archive"),
                    job_store=JobStore(root / "jobs" / "job-1"),
                    source=FakeSourceWithBlankMessageSourceId(),
                    source_kind="fake",
                    target_kind="telegram",
                    name="demo",
                )

    def test_export_rejects_whitespace_message_source_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            with self.assertRaisesRegex(ValueError, "message has invalid source_id"):
                export_dry_run(
                    job_id="job-1",
                    archive=Archive(root / "archive"),
                    job_store=JobStore(root / "jobs" / "job-1"),
                    source=FakeSourceWithWhitespaceMessageSourceId(),
                    source_kind="fake",
                    target_kind="telegram",
                    name="demo",
                )

    def test_export_rejects_blank_attachment_source_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            with self.assertRaisesRegex(ValueError, "attachment has invalid source_id"):
                export_dry_run(
                    job_id="job-1",
                    archive=Archive(root / "archive"),
                    job_store=JobStore(root / "jobs" / "job-1"),
                    source=FakeSourceWithBlankAttachmentSourceId(),
                    source_kind="fake",
                    target_kind="telegram",
                    name="demo",
                )

    def test_export_rejects_blank_downloadable_attachment_before_download(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = FakeSourceWithBlankDownloadableAttachment()

            with self.assertRaisesRegex(ValueError, "attachment has invalid source_id"):
                export_dry_run(
                    job_id="job-1",
                    archive=Archive(root / "archive"),
                    job_store=JobStore(root / "jobs" / "job-1"),
                    source=source,
                    source_kind="fake",
                    target_kind="telegram",
                    name="demo",
                )

            self.assertEqual(source.downloads, 0)
            self.assertEqual(list((root / "archive" / "attachments").rglob("*")), [])


if __name__ == "__main__":
    unittest.main()
