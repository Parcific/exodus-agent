from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from exodus_agent.archive import Archive
from exodus_agent.job import JobStore
from exodus_agent.model import Attachment, Conversation, ConversationKind, Message, Participant, Workspace
from exodus_agent.workflow import (
    run_teams_dry_run_workflow,
    run_telegram_dry_run_workflow,
    run_webex_to_teams_dry_run_workflow,
    run_webex_to_telegram_dry_run_workflow,
)
from exodus_agent.targets.teams_mapping import CompletedTeamsConversationMapping, TeamsTargetKind


class FakeWebexSource:
    def get_workspace(self) -> Workspace:
        return Workspace(source_id="org-1", source_kind="webex")

    def list_conversations(self) -> tuple[Conversation, ...]:
        return (Conversation(source_id="room/1", kind=ConversationKind.SPACE, title="General"),)

    def list_participants(self) -> tuple[Participant, ...]:
        return (Participant(source_id="user-1", display_name="Ada"),)

    def list_messages(self, conversation: Conversation) -> tuple[Message, ...]:
        return (
            Message(
                source_id="msg-1",
                conversation_id=conversation.source_id,
                author_id="user-1",
                created_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
                text="hello",
                attachments=(Attachment(source_id="file-1", filename="notes.txt"),),
            ),
        )

    def download_attachment(self, attachment: Attachment) -> bytes:
        return b"notes"


class WorkflowTests(unittest.TestCase):
    def test_webex_to_telegram_dry_run_workflow_runs_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = Archive(root / "archive")
            export_store = JobStore(root / "jobs" / "export")
            telegram_store = JobStore(root / "jobs" / "telegram")

            result = run_webex_to_telegram_dry_run_workflow(
                source=FakeWebexSource(),
                archive=archive,
                package_root=root / "telegram-package",
                destination_map={"room/1": "@general_archive"},
                export_job_store=export_store,
                telegram_job_store=telegram_store,
                export_job_id="export",
                telegram_job_id="telegram",
                name="demo",
            )

            self.assertTrue(result.ok)
            self.assertEqual(result.export.attachments, 1)
            self.assertEqual(result.telegram.import_plan.media, 1)
            self.assertTrue(export_store.read_events())
            self.assertTrue(telegram_store.read_events())

    def test_webex_to_telegram_dry_run_workflow_resets_stale_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = Archive(root / "archive")
            archive.initialize(source_kind="webex", target_kind="telegram", name="stale")
            archive.write_conversations(
                [Conversation(source_id="stale-room", kind=ConversationKind.SPACE)]
            )

            run_webex_to_telegram_dry_run_workflow(
                source=FakeWebexSource(),
                archive=archive,
                package_root=root / "telegram-package",
                destination_map={"room/1": "@general_archive"},
                export_job_store=JobStore(root / "jobs" / "export"),
                telegram_job_store=JobStore(root / "jobs" / "telegram"),
                export_job_id="export",
                telegram_job_id="telegram",
                name="demo",
            )

            self.assertEqual([item.source_id for item in archive.read_conversations()], ["room/1"])

    def test_webex_to_teams_dry_run_workflow_runs_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = Archive(root / "archive")

            result = run_webex_to_teams_dry_run_workflow(
                source=FakeWebexSource(),
                archive=archive,
                conversation_map=(
                    CompletedTeamsConversationMapping(
                        source_conversation_id="room/1",
                        target_kind=TeamsTargetKind.GROUP_CHAT,
                        target={"chat_id": "chat-1"},
                    ),
                ),
                identity_map={"user-1": "entra-user-1"},
                import_plan_path=root / "archive" / "plans" / "teams-import-plan.json",
                message_map_path=root / "archive" / "mappings" / "teams-message-map.json",
                verification_report_path=root / "archive" / "reports" / "teams-import-verification.json",
                export_job_store=JobStore(root / "jobs" / "export"),
                teams_job_store=JobStore(root / "jobs" / "teams"),
                export_job_id="export",
                teams_job_id="teams",
                name="demo",
            )

            self.assertTrue(result.ok)
            self.assertEqual(result.export.conversations, 1)
            self.assertEqual(result.export.messages, 1)
            self.assertEqual(result.export.attachments, 1)
            self.assertEqual(result.teams.import_plan.messages, 1)
            self.assertIsNotNone(result.teams.execution)
            self.assertEqual(result.teams.execution.messages_imported, 1)
            self.assertIsNotNone(result.teams.verification)
            self.assertEqual(result.teams.verification.messages_mapped, 1)

    def test_webex_to_teams_dry_run_workflow_resets_stale_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = Archive(root / "archive")
            archive.initialize(source_kind="webex", target_kind="teams", name="stale")
            archive.write_conversations(
                [Conversation(source_id="stale-room", kind=ConversationKind.SPACE)]
            )

            run_webex_to_teams_dry_run_workflow(
                source=FakeWebexSource(),
                archive=archive,
                conversation_map=(
                    CompletedTeamsConversationMapping(
                        source_conversation_id="room/1",
                        target_kind=TeamsTargetKind.GROUP_CHAT,
                        target={"chat_id": "chat-1"},
                    ),
                ),
                identity_map={"user-1": "entra-user-1"},
                import_plan_path=root / "archive" / "plans" / "teams-import-plan.json",
                message_map_path=root / "archive" / "mappings" / "teams-message-map.json",
                verification_report_path=root / "archive" / "reports" / "teams-import-verification.json",
                export_job_store=JobStore(root / "jobs" / "export"),
                teams_job_store=JobStore(root / "jobs" / "teams"),
                export_job_id="export",
                teams_job_id="teams",
                name="demo",
            )

            self.assertEqual([item.source_id for item in archive.read_conversations()], ["room/1"])

    def test_webex_to_teams_dry_run_refuses_external_stale_message_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            message_map_path = root / "stale-teams-message-map.json"
            message_map_path.write_text(
                '{"format":"exodus.teams.message_map.v1","messages":[]}\n',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(FileExistsError, "stale mappings"):
                run_webex_to_teams_dry_run_workflow(
                    source=FakeWebexSource(),
                    archive=Archive(root / "archive"),
                    conversation_map=(
                        CompletedTeamsConversationMapping(
                            source_conversation_id="room/1",
                            target_kind=TeamsTargetKind.GROUP_CHAT,
                            target={"chat_id": "chat-1"},
                        ),
                    ),
                    identity_map={"user-1": "entra-user-1"},
                    import_plan_path=root / "archive" / "plans" / "teams-import-plan.json",
                    message_map_path=message_map_path,
                    verification_report_path=root / "archive" / "reports" / "teams-import-verification.json",
                    export_job_store=JobStore(root / "jobs" / "export"),
                    teams_job_store=JobStore(root / "jobs" / "teams"),
                    export_job_id="export",
                    teams_job_id="teams",
                    name="demo",
                )

    def test_webex_to_teams_dry_run_preflights_stale_message_map_before_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = Archive(root / "archive")
            archive.initialize(source_kind="webex", target_kind="teams", name="stale")
            archive.write_conversations(
                [Conversation(source_id="stale-room", kind=ConversationKind.SPACE)]
            )
            message_map_path = root / "stale-teams-message-map.json"
            message_map_path.write_text(
                '{"format":"exodus.teams.message_map.v1","messages":[]}\n',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(FileExistsError, "before fresh Webex export"):
                run_webex_to_teams_dry_run_workflow(
                    source=FakeWebexSource(),
                    archive=archive,
                    conversation_map=(
                        CompletedTeamsConversationMapping(
                            source_conversation_id="room/1",
                            target_kind=TeamsTargetKind.GROUP_CHAT,
                            target={"chat_id": "chat-1"},
                        ),
                    ),
                    identity_map={"user-1": "entra-user-1"},
                    import_plan_path=root / "archive" / "plans" / "teams-import-plan.json",
                    message_map_path=message_map_path,
                    verification_report_path=root / "archive" / "reports" / "teams-import-verification.json",
                    export_job_store=JobStore(root / "jobs" / "export"),
                    teams_job_store=JobStore(root / "jobs" / "teams"),
                    export_job_id="export",
                    teams_job_id="teams",
                    name="demo",
                )

            self.assertEqual([item.source_id for item in archive.read_conversations()], ["stale-room"])

    def test_webex_to_teams_dry_run_preflights_bad_report_path_before_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = Archive(root / "archive")
            archive.initialize(source_kind="webex", target_kind="teams", name="stale")
            archive.write_conversations(
                [Conversation(source_id="stale-room", kind=ConversationKind.SPACE)]
            )
            report_path = root / "archive" / "reports" / "teams-import-verification.json"
            report_path.mkdir(parents=True)

            with self.assertRaisesRegex(ValueError, "report path must be a file"):
                run_webex_to_teams_dry_run_workflow(
                    source=FakeWebexSource(),
                    archive=archive,
                    conversation_map=(
                        CompletedTeamsConversationMapping(
                            source_conversation_id="room/1",
                            target_kind=TeamsTargetKind.GROUP_CHAT,
                            target={"chat_id": "chat-1"},
                        ),
                    ),
                    identity_map={"user-1": "entra-user-1"},
                    import_plan_path=root / "archive" / "plans" / "teams-import-plan.json",
                    message_map_path=root / "archive" / "mappings" / "teams-message-map.json",
                    verification_report_path=report_path,
                    export_job_store=JobStore(root / "jobs" / "export"),
                    teams_job_store=JobStore(root / "jobs" / "teams"),
                    export_job_id="export",
                    teams_job_id="teams",
                    name="demo",
                )

            self.assertEqual([item.source_id for item in archive.read_conversations()], ["stale-room"])

    def test_telegram_dry_run_workflow_executes_ready_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = _sample_archive(root, with_local_media=True)
            job_store = JobStore(root / "jobs" / "job-1")

            result = run_telegram_dry_run_workflow(
                archive=archive,
                package_root=root / "telegram-package",
                destination_map={"room/1": "@general_archive"},
                job_store=job_store,
                job_id="job-1",
            )

            self.assertTrue(result.ok)
            self.assertIsNotNone(result.execution)
            self.assertEqual(result.execution.operations_completed, 5)
            self.assertTrue((root / "telegram-package" / "import-plan.json").exists())

    def test_telegram_dry_run_workflow_does_not_execute_unready_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = _sample_archive(root, with_local_media=False)
            job_store = JobStore(root / "jobs" / "job-1")

            result = run_telegram_dry_run_workflow(
                archive=archive,
                package_root=root / "telegram-package",
                destination_map={"room/1": "@general_archive"},
                job_store=job_store,
                job_id="job-1",
            )

            self.assertFalse(result.ok)
            self.assertIsNone(result.execution)
            self.assertIn("missing local_path", "\n".join(result.import_plan.issues))
            self.assertEqual(job_store.read_events(), [])

    def test_telegram_dry_run_workflow_refuses_completed_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = _sample_archive(root, with_local_media=True)
            job_store = JobStore(root / "jobs" / "job-1")
            run_telegram_dry_run_workflow(
                archive=archive,
                package_root=root / "telegram-package",
                destination_map={"room/1": "@general_archive"},
                job_store=job_store,
                job_id="job-1",
            )

            with self.assertRaises(FileExistsError):
                run_telegram_dry_run_workflow(
                    archive=archive,
                    package_root=root / "telegram-package",
                    destination_map={"room/1": "@general_archive"},
                    job_store=job_store,
                    job_id="job-1",
                )

    def test_teams_dry_run_workflow_runs_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = _teams_archive(root)
            job_store = JobStore(root / "jobs" / "teams")

            result = run_teams_dry_run_workflow(
                archive=archive,
                conversation_map=_teams_conversation_map(),
                identity_map={"user-1": "entra-user-1"},
                import_plan_path=root / "archive" / "plans" / "teams-import-plan.json",
                message_map_path=root / "archive" / "mappings" / "teams-message-map.json",
                verification_report_path=root / "archive" / "reports" / "teams-import-verification.json",
                job_store=job_store,
                job_id="teams",
            )

            self.assertTrue(result.ok)
            self.assertEqual(result.import_plan.messages, 1)
            self.assertIsNotNone(result.execution)
            self.assertEqual(result.execution.messages_imported, 1)
            self.assertIsNotNone(result.verification)
            self.assertEqual(result.verification.messages_mapped, 1)

    def test_teams_dry_run_workflow_refuses_completed_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = _teams_archive(root)
            job_store = JobStore(root / "jobs" / "teams")
            kwargs = {
                "archive": archive,
                "conversation_map": _teams_conversation_map(),
                "identity_map": {"user-1": "entra-user-1"},
                "import_plan_path": root / "archive" / "plans" / "teams-import-plan.json",
                "message_map_path": root / "archive" / "mappings" / "teams-message-map.json",
                "verification_report_path": root / "archive" / "reports" / "teams-import-verification.json",
                "job_store": job_store,
                "job_id": "teams",
                "overwrite_import_plan": True,
            }
            run_teams_dry_run_workflow(**kwargs)

            with self.assertRaises(FileExistsError):
                run_teams_dry_run_workflow(**kwargs)

    def test_teams_dry_run_workflow_does_not_verify_failed_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = _teams_archive(root)
            message_map_path = root / "archive" / "mappings" / "teams-message-map.json"
            message_map_path.parent.mkdir(parents=True, exist_ok=True)
            message_map_path.write_text(
                '{"format":"wrong","messages":[]}\n',
                encoding="utf-8",
            )

            result = run_teams_dry_run_workflow(
                archive=archive,
                conversation_map=_teams_conversation_map(),
                identity_map={"user-1": "entra-user-1"},
                import_plan_path=root / "archive" / "plans" / "teams-import-plan.json",
                message_map_path=message_map_path,
                verification_report_path=root / "archive" / "reports" / "teams-import-verification.json",
                job_store=JobStore(root / "jobs" / "teams"),
                job_id="teams",
            )

            self.assertFalse(result.ok)
            self.assertIsNotNone(result.execution)
            self.assertFalse(result.execution.ok)
            self.assertIsNone(result.verification)

    def test_teams_dry_run_workflow_resumes_partial_message_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = _teams_archive(root)
            archive.write_messages(
                "space-1",
                [
                    Message(
                        source_id="msg-1",
                        conversation_id="space-1",
                        author_id="user-1",
                        created_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
                        text="hello",
                    ),
                    Message(
                        source_id="msg-2",
                        conversation_id="space-1",
                        author_id="user-1",
                        created_at=datetime(2026, 1, 1, 12, 1, tzinfo=timezone.utc),
                        text="again",
                    )
                ],
            )
            message_map_path = root / "archive" / "mappings" / "teams-message-map.json"
            message_map_path.parent.mkdir(parents=True, exist_ok=True)
            message_map_path.write_text(
                """
{
  "format": "exodus.teams.message_map.v1",
  "messages": [
    {
      "source_message_id": "msg-1",
      "teams_message_id": "teams-existing"
    }
  ]
}
""".strip(),
                encoding="utf-8",
            )

            result = run_teams_dry_run_workflow(
                archive=archive,
                conversation_map=_teams_conversation_map(),
                identity_map={"user-1": "entra-user-1"},
                import_plan_path=root / "archive" / "plans" / "teams-import-plan.json",
                message_map_path=message_map_path,
                verification_report_path=root / "archive" / "reports" / "teams-import-verification.json",
                job_store=JobStore(root / "jobs" / "teams"),
                job_id="teams",
            )

            self.assertTrue(result.ok)
            self.assertIsNotNone(result.execution)
            self.assertEqual(result.execution.messages_skipped, 1)
            self.assertEqual(result.execution.messages_imported, 1)


def _sample_archive(root: Path, *, with_local_media: bool) -> Archive:
    archive = Archive(root / "archive")
    archive.initialize(source_kind="webex", target_kind="telegram", name="demo")
    if with_local_media:
        attachment_path = archive.root / "attachments" / "notes.txt"
        attachment_path.parent.mkdir(parents=True, exist_ok=True)
        attachment_path.write_text("attachment body", encoding="utf-8")
    archive.write_workspace(Workspace(source_id="org-1", source_kind="webex"))
    archive.write_conversations(
        [Conversation(source_id="room/1", kind=ConversationKind.SPACE, title="General")]
    )
    archive.write_messages(
        "room/1",
        [
            Message(
                source_id="msg-1",
                conversation_id="room/1",
                author_id="user-1",
                created_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
                text="hello",
                attachments=(
                    Attachment(
                        source_id="file-1",
                        filename="notes.txt",
                        local_path="attachments/notes.txt" if with_local_media else None,
                    ),
                ),
            )
        ],
    )
    return archive


def _teams_archive(root: Path) -> Archive:
    archive = Archive(root / "archive")
    archive.initialize(source_kind="webex", target_kind="teams", name="demo")
    archive.write_workspace(Workspace(source_id="org-1", source_kind="webex"))
    archive.write_conversations(
        [Conversation(source_id="space-1", kind=ConversationKind.SPACE, title="General")]
    )
    archive.write_participants([Participant(source_id="user-1", display_name="Ada")])
    archive.write_messages(
        "space-1",
        [
            Message(
                source_id="msg-1",
                conversation_id="space-1",
                author_id="user-1",
                created_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
                text="hello",
            )
        ],
    )
    return archive


def _teams_conversation_map() -> tuple[CompletedTeamsConversationMapping, ...]:
    return (
        CompletedTeamsConversationMapping(
            source_conversation_id="space-1",
            target_kind=TeamsTargetKind.GROUP_CHAT,
            target={"chat_id": "chat-1"},
        ),
    )


if __name__ == "__main__":
    unittest.main()
