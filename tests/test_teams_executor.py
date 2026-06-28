from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from exodus_agent.job import JobStore
from exodus_agent.targets.teams_executor import execute_teams_import_plan, verify_teams_import


class FailingTeamsAdapter:
    def import_message(self, message: dict[str, object]) -> dict[str, object]:
        raise RuntimeError(f"boom:{message.get('source_message_id')}")


class SecretFailingTeamsAdapter:
    def import_message(self, message: dict[str, object]) -> dict[str, object]:
        raise RuntimeError("client_secret=super-secret")


class SecretTeamsAdapter:
    def import_message(self, message: dict[str, object]) -> dict[str, object]:
        return {
            "teams_message_id": f"teams:{message['source_message_id']}",
            "authorization": "secret",
            "nested": {"token": "secret"},
            "items": [{"session": "secret"}],
        }


class RecordingTeamsAdapter:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []

    def import_message(self, message: dict[str, object]) -> dict[str, object]:
        self.messages.append(dict(message))
        return {"teams_message_id": f"teams:{message['source_message_id']}"}


class TeamsExecutorTests(unittest.TestCase):
    def test_executes_plan_with_dry_run_adapter_and_writes_message_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = _write_plan(root)
            message_map_path = root / "teams-message-map.json"
            store = JobStore(root / "jobs" / "job-1")

            result = execute_teams_import_plan(
                plan_path=plan_path,
                message_map_path=message_map_path,
                job_store=store,
                job_id="job-1",
            )
            payload = json.loads(message_map_path.read_text(encoding="utf-8"))
            events = store.read_events()

            self.assertTrue(result.ok)
            self.assertEqual(result.messages_total, 2)
            self.assertEqual(result.messages_imported, 2)
            self.assertEqual(result.messages_skipped, 0)
            self.assertEqual(payload["format"], "exodus.teams.message_map.v1")
            self.assertEqual(payload["messages"][0]["teams_message_id"], "dry-run:msg-1")
            self.assertEqual(events[-1]["phase"], "teams_import")

    def test_skips_messages_already_in_message_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = _write_plan(root)
            message_map_path = root / "teams-message-map.json"
            message_map_path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.message_map.v1",
                        "messages": [
                            {"source_message_id": "msg-1", "teams_message_id": "teams-existing"}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            store = JobStore(root / "jobs" / "job-1")
            adapter = RecordingTeamsAdapter()

            result = execute_teams_import_plan(
                plan_path=plan_path,
                message_map_path=message_map_path,
                job_store=store,
                job_id="job-1",
                adapter=adapter,
            )
            payload = json.loads(message_map_path.read_text(encoding="utf-8"))

            self.assertTrue(result.ok)
            self.assertEqual(result.messages_imported, 1)
            self.assertEqual(result.messages_skipped, 1)
            self.assertEqual([message["source_message_id"] for message in adapter.messages], ["msg-2"])
            self.assertEqual(
                {row["source_message_id"]: row["teams_message_id"] for row in payload["messages"]},
                {"msg-1": "teams-existing", "msg-2": "teams:msg-2"},
            )

    def test_skips_messages_already_in_message_map_with_trimmed_source_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = _write_plan(root)
            message_map_path = root / "teams-message-map.json"
            message_map_path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.message_map.v1",
                        "messages": [
                            {"source_message_id": " msg-1 ", "teams_message_id": " teams-existing "}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            store = JobStore(root / "jobs" / "job-1")
            adapter = RecordingTeamsAdapter()

            result = execute_teams_import_plan(
                plan_path=plan_path,
                message_map_path=message_map_path,
                job_store=store,
                job_id="job-1",
                adapter=adapter,
            )
            payload = json.loads(message_map_path.read_text(encoding="utf-8"))

            self.assertTrue(result.ok)
            self.assertEqual(result.messages_imported, 1)
            self.assertEqual(result.messages_skipped, 1)
            self.assertEqual([message["source_message_id"] for message in adapter.messages], ["msg-2"])
            self.assertEqual(
                {row["source_message_id"]: row["teams_message_id"] for row in payload["messages"]},
                {"msg-1": "teams-existing", "msg-2": "teams:msg-2"},
            )

    def test_refuses_duplicate_completed_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = _write_plan(root)
            message_map_path = root / "teams-message-map.json"
            store = JobStore(root / "jobs" / "job-1")

            first = execute_teams_import_plan(
                plan_path=plan_path,
                message_map_path=message_map_path,
                job_store=store,
                job_id="job-1",
            )
            second = execute_teams_import_plan(
                plan_path=plan_path,
                message_map_path=message_map_path,
                job_store=store,
                job_id="job-1",
            )

            self.assertTrue(first.ok)
            self.assertFalse(second.ok)
            self.assertIn("already completed", "\n".join(second.issues))

    def test_rejects_reply_before_parent_in_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / "teams-import-plan.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.import_plan.v1",
                        "messages": [
                            _message("reply-1", 0, parent_source_message_id="parent-1"),
                            _message("parent-1", 1),
                        ],
                    }
                ),
                encoding="utf-8",
            )
            store = JobStore(root / "jobs" / "job-1")

            result = execute_teams_import_plan(
                plan_path=plan_path,
                message_map_path=root / "teams-message-map.json",
                job_store=store,
                job_id="job-1",
            )

            self.assertFalse(result.ok)
            self.assertIn("reply before parent", "\n".join(result.issues))

    def test_rejects_reply_created_datetime_not_after_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / "teams-import-plan.json"
            parent = _message("parent-1", 0)
            reply = _message("reply-1", 1, parent_source_message_id="parent-1")
            parent["createdDateTime"] = "2026-01-01T12:00:00.000Z"
            parent["original_created_at"] = "2026-01-01T12:00:00.000Z"
            reply["createdDateTime"] = "2026-01-01T11:59:00.000Z"
            reply["original_created_at"] = "2026-01-01T11:59:00.000Z"
            plan_path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.import_plan.v1",
                        "messages": [parent, reply],
                    }
                ),
                encoding="utf-8",
            )
            store = JobStore(root / "jobs" / "job-1")
            adapter = RecordingTeamsAdapter()

            result = execute_teams_import_plan(
                plan_path=plan_path,
                message_map_path=root / "teams-message-map.json",
                job_store=store,
                job_id="job-1",
                adapter=adapter,
            )

            self.assertFalse(result.ok)
            self.assertEqual(adapter.messages, [])
            self.assertIn("reply createdDateTime before or equal to parent", "\n".join(result.issues))

    def test_rejects_duplicate_message_map_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = _write_plan(root)
            message_map_path = root / "teams-message-map.json"
            message_map_path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.message_map.v1",
                        "messages": [
                            {"source_message_id": "msg-a", "teams_message_id": "Teams-1"},
                            {"source_message_id": "msg-b", "teams_message_id": "teams-1"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            store = JobStore(root / "jobs" / "job-1")

            result = execute_teams_import_plan(
                plan_path=plan_path,
                message_map_path=message_map_path,
                job_store=store,
                job_id="job-1",
            )

            self.assertFalse(result.ok)
            self.assertIn("multiple source messages", "\n".join(result.issues))

    def test_rejects_duplicate_message_map_source_ids_after_trim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = _write_plan(root)
            message_map_path = root / "teams-message-map.json"
            message_map_path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.message_map.v1",
                        "messages": [
                            {"source_message_id": "msg-1", "teams_message_id": "teams-1"},
                            {"source_message_id": " msg-1 ", "teams_message_id": "teams-2"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            store = JobStore(root / "jobs" / "job-1")
            adapter = RecordingTeamsAdapter()

            result = execute_teams_import_plan(
                plan_path=plan_path,
                message_map_path=message_map_path,
                job_store=store,
                job_id="job-1",
                adapter=adapter,
            )

            self.assertFalse(result.ok)
            self.assertEqual(adapter.messages, [])
            self.assertIn("duplicates source_message_id: msg-1", "\n".join(result.issues))

    def test_rejects_stale_message_map_entries_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = _write_plan(root)
            message_map_path = root / "teams-message-map.json"
            message_map_path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.message_map.v1",
                        "messages": [
                            {"source_message_id": "msg-1", "teams_message_id": "teams-1"},
                            {"source_message_id": "stale-msg", "teams_message_id": "teams-stale"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            store = JobStore(root / "jobs" / "job-1")
            adapter = RecordingTeamsAdapter()

            result = execute_teams_import_plan(
                plan_path=plan_path,
                message_map_path=message_map_path,
                job_store=store,
                job_id="job-1",
                adapter=adapter,
            )

            self.assertFalse(result.ok)
            self.assertEqual(adapter.messages, [])
            self.assertIn("unplanned source_message_id: stale-msg", "\n".join(result.issues))

    def test_rejects_duplicate_created_datetime_in_same_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / "teams-import-plan.json"
            first = _message("msg-1", 0)
            second = _message("msg-2", 1)
            second["createdDateTime"] = first["createdDateTime"]
            plan_path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.import_plan.v1",
                        "messages": [first, second],
                    }
                ),
                encoding="utf-8",
            )
            store = JobStore(root / "jobs" / "job-1")

            result = execute_teams_import_plan(
                plan_path=plan_path,
                message_map_path=root / "teams-message-map.json",
                job_store=store,
                job_id="job-1",
            )

            self.assertFalse(result.ok)
            self.assertIn("duplicates createdDateTime in the same target", "\n".join(result.issues))

    def test_rejects_duplicate_created_datetime_in_same_trimmed_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / "teams-import-plan.json"
            first = _message("msg-1", 0)
            second = _message("msg-2", 1)
            second["createdDateTime"] = first["createdDateTime"]
            second["target"] = {"chat_id": " chat-1 "}
            plan_path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.import_plan.v1",
                        "messages": [first, second],
                    }
                ),
                encoding="utf-8",
            )
            store = JobStore(root / "jobs" / "job-1")

            result = execute_teams_import_plan(
                plan_path=plan_path,
                message_map_path=root / "teams-message-map.json",
                job_store=store,
                job_id="job-1",
            )

            self.assertFalse(result.ok)
            self.assertIn("duplicates createdDateTime in the same target", "\n".join(result.issues))

    def test_allows_same_created_datetime_in_different_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / "teams-import-plan.json"
            first = _message("msg-1", 0)
            second = _message("msg-2", 1)
            second["target"] = {"chat_id": "chat-2"}
            plan_path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.import_plan.v1",
                        "messages": [first, second],
                    }
                ),
                encoding="utf-8",
            )
            store = JobStore(root / "jobs" / "job-1")

            result = execute_teams_import_plan(
                plan_path=plan_path,
                message_map_path=root / "teams-message-map.json",
                job_store=store,
                job_id="job-1",
            )

            self.assertTrue(result.ok)
            self.assertEqual(result.messages_imported, 2)

    def test_rejects_invalid_created_datetime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / "teams-import-plan.json"
            message = _message("msg-1", 0)
            message["createdDateTime"] = "2026-01-01T00:00:00"
            plan_path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.import_plan.v1",
                        "messages": [message],
                    }
                ),
                encoding="utf-8",
            )
            store = JobStore(root / "jobs" / "job-1")

            result = execute_teams_import_plan(
                plan_path=plan_path,
                message_map_path=root / "teams-message-map.json",
                job_store=store,
                job_id="job-1",
            )

            self.assertFalse(result.ok)
            self.assertIn("invalid createdDateTime", "\n".join(result.issues))

    def test_rejects_sub_millisecond_created_datetime_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / "teams-import-plan.json"
            message = _message("msg-1", 0)
            message["createdDateTime"] = "2026-01-01T00:00:00.000100Z"
            plan_path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.import_plan.v1",
                        "messages": [message],
                    }
                ),
                encoding="utf-8",
            )
            store = JobStore(root / "jobs" / "job-1")
            adapter = RecordingTeamsAdapter()

            result = execute_teams_import_plan(
                plan_path=plan_path,
                message_map_path=root / "teams-message-map.json",
                job_store=store,
                job_id="job-1",
                adapter=adapter,
            )

            self.assertFalse(result.ok)
            self.assertEqual(adapter.messages, [])
            self.assertIn("invalid createdDateTime", "\n".join(result.issues))

    def test_rejects_invalid_original_created_at_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / "teams-import-plan.json"
            message = _message("msg-1", 0)
            message["original_created_at"] = "2026-01-01T00:00:00"
            plan_path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.import_plan.v1",
                        "messages": [message],
                    }
                ),
                encoding="utf-8",
            )
            store = JobStore(root / "jobs" / "job-1")
            adapter = RecordingTeamsAdapter()

            result = execute_teams_import_plan(
                plan_path=plan_path,
                message_map_path=root / "teams-message-map.json",
                job_store=store,
                job_id="job-1",
                adapter=adapter,
            )

            self.assertFalse(result.ok)
            self.assertEqual(adapter.messages, [])
            self.assertIn("invalid original_created_at", "\n".join(result.issues))

    def test_accepts_high_precision_original_created_at_with_audit_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / "teams-import-plan.json"
            message = _message("msg-1", 0)
            message["original_created_at"] = "2026-01-01T00:00:00.000100Z"
            message["timestamp_adjusted"] = True
            message["timestamp_adjustment_ms"] = 0
            message["timestamp_adjustment_reason"] = "millisecond_precision"
            plan_path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.import_plan.v1",
                        "messages": [message],
                    }
                ),
                encoding="utf-8",
            )
            store = JobStore(root / "jobs" / "job-1")
            adapter = RecordingTeamsAdapter()

            result = execute_teams_import_plan(
                plan_path=plan_path,
                message_map_path=root / "teams-message-map.json",
                job_store=store,
                job_id="job-1",
                adapter=adapter,
            )

            self.assertTrue(result.ok)
            self.assertEqual([message["source_message_id"] for message in adapter.messages], ["msg-1"])

    def test_rejects_inconsistent_timestamp_audit_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / "teams-import-plan.json"
            message = _message("msg-1", 0)
            message["createdDateTime"] = "2026-01-01T00:00:00.001Z"
            message["timestamp_adjusted"] = False
            message["timestamp_adjustment_ms"] = 0
            message["timestamp_adjustment_reason"] = None
            plan_path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.import_plan.v1",
                        "messages": [message],
                    }
                ),
                encoding="utf-8",
            )
            store = JobStore(root / "jobs" / "job-1")
            adapter = RecordingTeamsAdapter()

            result = execute_teams_import_plan(
                plan_path=plan_path,
                message_map_path=root / "teams-message-map.json",
                job_store=store,
                job_id="job-1",
                adapter=adapter,
            )

            self.assertFalse(result.ok)
            self.assertEqual(adapter.messages, [])
            self.assertIn("timestamp_adjustment_ms 0 does not match", "\n".join(result.issues))
            self.assertIn("timestamp_adjusted does not match", "\n".join(result.issues))
            self.assertIn("timestamp_adjustment_reason does not match", "\n".join(result.issues))

    def test_rejects_extra_target_fields_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / "teams-import-plan.json"
            malformed = _message("msg-1", 0)
            malformed["target"] = {"chat_id": "chat-1", "channel_id": "channel-1"}
            plan_path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.import_plan.v1",
                        "messages": [malformed],
                    }
                ),
                encoding="utf-8",
            )
            store = JobStore(root / "jobs" / "job-1")
            adapter = RecordingTeamsAdapter()

            result = execute_teams_import_plan(
                plan_path=plan_path,
                message_map_path=root / "teams-message-map.json",
                job_store=store,
                job_id="job-1",
                adapter=adapter,
            )

            self.assertFalse(result.ok)
            self.assertEqual(adapter.messages, [])
            self.assertIn("unsupported fields", "\n".join(result.issues))

    def test_rejects_malformed_prepared_message_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / "teams-import-plan.json"
            malformed = _message("msg-1", 0)
            del malformed["author_user_id"]
            malformed["content"] = None
            malformed["attachments"] = None
            plan_path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.import_plan.v1",
                        "messages": [malformed],
                    }
                ),
                encoding="utf-8",
            )
            store = JobStore(root / "jobs" / "job-1")

            result = execute_teams_import_plan(
                plan_path=plan_path,
                message_map_path=root / "teams-message-map.json",
                job_store=store,
                job_id="job-1",
            )

            self.assertFalse(result.ok)
            self.assertIn("missing author_user_id", "\n".join(result.issues))
            self.assertIn("content must be a string", "\n".join(result.issues))
            self.assertIn("attachments must be a list", "\n".join(result.issues))

    def test_rejects_whitespace_only_plan_source_message_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / "teams-import-plan.json"
            malformed = _message("msg-1", 0)
            malformed["source_message_id"] = "   "
            plan_path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.import_plan.v1",
                        "messages": [malformed],
                    }
                ),
                encoding="utf-8",
            )
            store = JobStore(root / "jobs" / "job-1")

            result = execute_teams_import_plan(
                plan_path=plan_path,
                message_map_path=root / "teams-message-map.json",
                job_store=store,
                job_id="job-1",
            )

            self.assertFalse(result.ok)
            self.assertIn("missing source_message_id", "\n".join(result.issues))

    def test_rejects_duplicate_plan_source_message_ids_after_trim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / "teams-import-plan.json"
            first = _message("msg-1", 0)
            second = _message(" msg-1 ", 1)
            plan_path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.import_plan.v1",
                        "messages": [first, second],
                    }
                ),
                encoding="utf-8",
            )
            store = JobStore(root / "jobs" / "job-1")

            result = execute_teams_import_plan(
                plan_path=plan_path,
                message_map_path=root / "teams-message-map.json",
                job_store=store,
                job_id="job-1",
            )

            self.assertFalse(result.ok)
            self.assertIn("duplicates source_message_id: msg-1", "\n".join(result.issues))

    def test_rejects_non_object_prepared_message_attachments_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / "teams-import-plan.json"
            malformed = _message("msg-1", 0)
            malformed["attachments"] = [{"source_attachment_id": "file-1"}, "bad"]
            plan_path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.import_plan.v1",
                        "messages": [malformed],
                    }
                ),
                encoding="utf-8",
            )
            store = JobStore(root / "jobs" / "job-1")
            adapter = RecordingTeamsAdapter()

            result = execute_teams_import_plan(
                plan_path=plan_path,
                message_map_path=root / "teams-message-map.json",
                job_store=store,
                job_id="job-1",
                adapter=adapter,
            )

            self.assertFalse(result.ok)
            self.assertEqual(adapter.messages, [])
            self.assertIn("attachment row 1 must be an object", "\n".join(result.issues))

    def test_rejects_malformed_prepared_message_attachment_fields_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / "teams-import-plan.json"
            malformed = _message("msg-1", 0)
            malformed["attachments"] = [
                {
                    "source_attachment_id": "file-1",
                    "filename": "",
                    "size_bytes": True,
                    "supported": False,
                }
            ]
            plan_path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.import_plan.v1",
                        "messages": [malformed],
                    }
                ),
                encoding="utf-8",
            )
            store = JobStore(root / "jobs" / "job-1")
            adapter = RecordingTeamsAdapter()

            result = execute_teams_import_plan(
                plan_path=plan_path,
                message_map_path=root / "teams-message-map.json",
                job_store=store,
                job_id="job-1",
                adapter=adapter,
            )

            issues = "\n".join(result.issues)
            self.assertFalse(result.ok)
            self.assertEqual(adapter.messages, [])
            self.assertIn("attachment row 0 missing filename", issues)
            self.assertIn("field size_bytes must be a non-negative integer or null", issues)
            self.assertIn("attachment row 0 missing reason", issues)

    def test_accepts_legacy_plan_message_without_attachments_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / "teams-import-plan.json"
            legacy_message = _message("msg-1", 0)
            del legacy_message["attachments"]
            plan_path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.import_plan.v1",
                        "messages": [legacy_message],
                    }
                ),
                encoding="utf-8",
            )
            store = JobStore(root / "jobs" / "job-1")

            result = execute_teams_import_plan(
                plan_path=plan_path,
                message_map_path=root / "teams-message-map.json",
                job_store=store,
                job_id="job-1",
            )

            self.assertTrue(result.ok)
            self.assertEqual(result.messages_imported, 1)

    def test_rejects_boolean_import_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / "teams-import-plan.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.import_plan.v1",
                        "messages": [_message("msg-1", True)],
                    }
                ),
                encoding="utf-8",
            )
            store = JobStore(root / "jobs" / "job-1")

            result = execute_teams_import_plan(
                plan_path=plan_path,
                message_map_path=root / "teams-message-map.json",
                job_store=store,
                job_id="job-1",
            )

            self.assertFalse(result.ok)
            self.assertIn("missing import_order", "\n".join(result.issues))

    def test_execution_reports_invalid_plan_json_as_job_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / "teams-import-plan.json"
            plan_path.write_text("{not json", encoding="utf-8")
            store = JobStore(root / "jobs" / "job-1")

            result = execute_teams_import_plan(
                plan_path=plan_path,
                message_map_path=root / "teams-message-map.json",
                job_store=store,
                job_id="job-1",
            )

            self.assertFalse(result.ok)
            self.assertIn("not valid JSON", "\n".join(result.issues))
            self.assertEqual(store.read_events()[0]["kind"], "error")

    def test_execution_reports_non_utf8_plan_as_job_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / "teams-import-plan.json"
            plan_path.write_bytes(b"\xff")
            store = JobStore(root / "jobs" / "job-1")
            adapter = RecordingTeamsAdapter()

            result = execute_teams_import_plan(
                plan_path=plan_path,
                message_map_path=root / "teams-message-map.json",
                job_store=store,
                job_id="job-1",
                adapter=adapter,
            )

            self.assertFalse(result.ok)
            self.assertEqual(adapter.messages, [])
            self.assertIn("not valid UTF-8", "\n".join(result.issues))
            self.assertEqual(store.read_events()[0]["kind"], "error")

    def test_execution_reports_plan_directory_as_job_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / "teams-import-plan.json"
            plan_path.mkdir()
            store = JobStore(root / "jobs" / "job-1")
            adapter = RecordingTeamsAdapter()

            result = execute_teams_import_plan(
                plan_path=plan_path,
                message_map_path=root / "teams-message-map.json",
                job_store=store,
                job_id="job-1",
                adapter=adapter,
            )

            self.assertFalse(result.ok)
            self.assertEqual(adapter.messages, [])
            self.assertIn("must be a file", "\n".join(result.issues))
            self.assertEqual(store.read_events()[0]["kind"], "error")

    def test_records_adapter_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = _write_plan(root)
            store = JobStore(root / "jobs" / "job-1")

            result = execute_teams_import_plan(
                plan_path=plan_path,
                message_map_path=root / "teams-message-map.json",
                job_store=store,
                job_id="job-1",
                adapter=FailingTeamsAdapter(),
            )

            self.assertFalse(result.ok)
            self.assertEqual(result.messages_imported, 0)
            self.assertIn("boom:msg-1", "\n".join(result.issues))
            self.assertEqual(store.read_events()[-1]["kind"], "error")

    def test_redacts_adapter_failure_issues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = _write_plan(root)
            store = JobStore(root / "jobs" / "job-1")

            result = execute_teams_import_plan(
                plan_path=plan_path,
                message_map_path=root / "teams-message-map.json",
                job_store=store,
                job_id="job-1",
                adapter=SecretFailingTeamsAdapter(),
            )

            issues = "\n".join(result.issues)
            self.assertFalse(result.ok)
            self.assertIn("client_secret=[redacted]", issues)
            self.assertNotIn("super-secret", issues)

    def test_redacts_adapter_result_in_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = _write_plan(root)
            store = JobStore(root / "jobs" / "job-1")

            result = execute_teams_import_plan(
                plan_path=plan_path,
                message_map_path=root / "teams-message-map.json",
                job_store=store,
                job_id="job-1",
                adapter=SecretTeamsAdapter(),
            )
            event_data = store.read_events()[1]["data"]["adapter_result"]

            self.assertTrue(result.ok)
            self.assertEqual(event_data["authorization"], "[redacted]")
            self.assertEqual(event_data["nested"]["token"], "[redacted]")
            self.assertEqual(event_data["items"][0]["session"], "[redacted]")

    def test_verifies_completed_teams_import_message_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = _write_plan(root)
            message_map_path = root / "teams-message-map.json"
            execute_teams_import_plan(
                plan_path=plan_path,
                message_map_path=message_map_path,
                job_store=JobStore(root / "jobs" / "job-1"),
                job_id="job-1",
            )

            result = verify_teams_import(
                plan_path=plan_path,
                message_map_path=message_map_path,
                report_path=root / "teams-import-verification.json",
            )
            report = json.loads(result.report_path.read_text(encoding="utf-8"))

            self.assertTrue(result.ok)
            self.assertEqual(result.messages_expected, 2)
            self.assertEqual(result.messages_mapped, 2)
            self.assertEqual(result.extra_mappings, 0)
            self.assertEqual(result.unsupported_attachments, 0)
            self.assertEqual(report["format"], "exodus.teams.import_verification.v1")
            self.assertEqual(report["ok"], True)
            self.assertEqual(report["unsupported_attachments"], 0)

    def test_verification_reports_missing_and_extra_message_mappings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = _write_plan(root)
            message_map_path = root / "teams-message-map.json"
            message_map_path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.message_map.v1",
                        "messages": [
                            {"source_message_id": "msg-1", "teams_message_id": "teams-1"},
                            {"source_message_id": "extra-1", "teams_message_id": "teams-extra"},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = verify_teams_import(
                plan_path=plan_path,
                message_map_path=message_map_path,
                report_path=root / "teams-import-verification.json",
            )
            report = json.loads(result.report_path.read_text(encoding="utf-8"))

            self.assertFalse(result.ok)
            self.assertEqual(result.messages_expected, 2)
            self.assertEqual(result.messages_mapped, 1)
            self.assertEqual(result.extra_mappings, 1)
            self.assertEqual(report["missing_source_message_ids"], ["msg-2"])
            self.assertEqual(report["extra_source_message_ids"], ["extra-1"])
            self.assertIn("missing source_message_id: msg-2", "\n".join(result.issues))
            self.assertIn("unplanned source_message_id: extra-1", "\n".join(result.issues))

    def test_verification_reports_unsupported_attachments_from_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / "teams-import-plan.json"
            message = _message("msg-1", 0)
            message["attachments"] = [
                {
                    "source_attachment_id": "file-1",
                    "filename": "notes.txt",
                    "local_path": "attachments/notes.txt",
                    "supported": False,
                    "reason": "Teams historical import attachment import is not implemented yet.",
                }
            ]
            plan_path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.import_plan.v1",
                        "messages": [message],
                        "unsupported_attachments": [
                            {
                                "source_message_id": "msg-1",
                                "source_conversation_id": "space-1",
                                "source_attachment_id": "file-1",
                                "filename": "notes.txt",
                                "local_path": "attachments/notes.txt",
                                "reason": "Teams historical import attachment import is not implemented yet.",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            message_map_path = root / "teams-message-map.json"
            message_map_path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.message_map.v1",
                        "messages": [{"source_message_id": "msg-1", "teams_message_id": "teams-1"}],
                    }
                ),
                encoding="utf-8",
            )

            result = verify_teams_import(
                plan_path=plan_path,
                message_map_path=message_map_path,
                report_path=root / "teams-import-verification.json",
            )
            report = json.loads(result.report_path.read_text(encoding="utf-8"))

            self.assertTrue(result.ok)
            self.assertEqual(result.unsupported_attachments, 1)
            self.assertEqual(report["unsupported_attachments"], 1)
            self.assertEqual(report["unsupported_attachment_rows"][0]["source_attachment_id"], "file-1")

    def test_verification_rejects_unsupported_attachment_for_unplanned_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / "teams-import-plan.json"
            message = _message("msg-1", 0)
            plan_path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.import_plan.v1",
                        "messages": [message],
                        "unsupported_attachments": [
                            {
                                "source_message_id": "stale-msg",
                                "source_conversation_id": "space-1",
                                "source_attachment_id": "file-1",
                                "filename": "notes.txt",
                                "local_path": "attachments/notes.txt",
                                "reason": "not implemented",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            message_map_path = root / "teams-message-map.json"
            message_map_path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.message_map.v1",
                        "messages": [{"source_message_id": "msg-1", "teams_message_id": "teams-1"}],
                    }
                ),
                encoding="utf-8",
            )

            result = verify_teams_import(
                plan_path=plan_path,
                message_map_path=message_map_path,
                report_path=root / "teams-import-verification.json",
            )

            self.assertFalse(result.ok)
            self.assertIn("unplanned source_message_id: stale-msg", "\n".join(result.issues))

    def test_verification_derives_unsupported_attachments_from_legacy_plan_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / "teams-import-plan.json"
            message = _message("msg-1", 0)
            message["attachments"] = [
                {
                    "source_attachment_id": "file-1",
                    "filename": "notes.txt",
                    "local_path": "attachments/notes.txt",
                    "supported": False,
                    "reason": "not implemented",
                }
            ]
            plan_path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.import_plan.v1",
                        "messages": [message],
                    }
                ),
                encoding="utf-8",
            )
            message_map_path = root / "teams-message-map.json"
            message_map_path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.message_map.v1",
                        "messages": [{"source_message_id": "msg-1", "teams_message_id": "teams-1"}],
                    }
                ),
                encoding="utf-8",
            )

            result = verify_teams_import(
                plan_path=plan_path,
                message_map_path=message_map_path,
                report_path=root / "teams-import-verification.json",
            )

            self.assertTrue(result.ok)
            self.assertEqual(result.unsupported_attachments, 1)

    def test_verification_reports_malformed_message_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = _write_plan(root)
            message_map_path = root / "teams-message-map.json"
            message_map_path.write_text(
                json.dumps({"format": "wrong", "messages": []}),
                encoding="utf-8",
            )

            result = verify_teams_import(
                plan_path=plan_path,
                message_map_path=message_map_path,
                report_path=root / "teams-import-verification.json",
            )

            self.assertFalse(result.ok)
            self.assertIn("unsupported format", "\n".join(result.issues))

    def test_verification_writes_report_when_message_map_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = _write_plan(root)
            report_path = root / "teams-import-verification.json"

            result = verify_teams_import(
                plan_path=plan_path,
                message_map_path=root / "missing-message-map.json",
                report_path=report_path,
            )
            report = json.loads(report_path.read_text(encoding="utf-8"))

            self.assertFalse(result.ok)
            self.assertIn("does not exist", "\n".join(result.issues))
            self.assertEqual(report["messages_expected"], 2)

    def test_verification_writes_report_when_message_map_is_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = _write_plan(root)
            message_map_path = root / "teams-message-map.json"
            message_map_path.mkdir()
            report_path = root / "teams-import-verification.json"

            result = verify_teams_import(
                plan_path=plan_path,
                message_map_path=message_map_path,
                report_path=report_path,
            )
            report = json.loads(report_path.read_text(encoding="utf-8"))

            self.assertFalse(result.ok)
            self.assertIn("must be a file", "\n".join(result.issues))
            self.assertEqual(report["ok"], False)

    def test_verification_rejects_directory_report_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = _write_plan(root)
            message_map_path = root / "teams-message-map.json"
            message_map_path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.message_map.v1",
                        "messages": [
                            {"source_message_id": "m1", "teams_message_id": "tm1"},
                            {"source_message_id": "m2", "teams_message_id": "tm2"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            report_path = root / "teams-import-verification.json"
            report_path.mkdir()

            with self.assertRaisesRegex(ValueError, "report path must be a file"):
                verify_teams_import(
                    plan_path=plan_path,
                    message_map_path=message_map_path,
                    report_path=report_path,
                )

    def test_verification_writes_report_for_non_utf8_message_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = _write_plan(root)
            message_map_path = root / "teams-message-map.json"
            message_map_path.write_bytes(b"\xff")
            report_path = root / "teams-import-verification.json"

            result = verify_teams_import(
                plan_path=plan_path,
                message_map_path=message_map_path,
                report_path=report_path,
            )
            report = json.loads(report_path.read_text(encoding="utf-8"))

            self.assertFalse(result.ok)
            self.assertIn("not valid UTF-8", "\n".join(result.issues))
            self.assertEqual(report["ok"], False)


def _write_plan(root: Path) -> Path:
    plan_path = root / "teams-import-plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "format": "exodus.teams.import_plan.v1",
                "messages": [
                    _message("msg-1", 0),
                    _message("msg-2", 1, parent_source_message_id="msg-1"),
                ],
            }
        ),
        encoding="utf-8",
    )
    return plan_path


def _message(
    source_message_id: str,
    import_order: int,
    *,
    parent_source_message_id: str | None = None,
) -> dict[str, object]:
    created_date_time = f"2026-01-01T00:00:00.{import_order:03d}Z"
    return {
        "import_order": import_order,
        "source_message_id": source_message_id,
        "source_conversation_id": "space-1",
        "target_kind": "group_chat",
        "target": {"chat_id": "chat-1"},
        "author_user_id": "entra-u1",
        "createdDateTime": created_date_time,
        "original_created_at": created_date_time,
        "timestamp_adjusted": False,
        "timestamp_adjustment_ms": 0,
        "timestamp_adjustment_reason": None,
        "parent_source_message_id": parent_source_message_id,
        "content": "hello",
        "attachments": [],
    }


if __name__ == "__main__":
    unittest.main()
