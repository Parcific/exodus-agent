from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from exodus_agent.job import JobStore
from exodus_agent.targets.telegram_executor import SubprocessTelegramAdapter, execute_import_plan


class FailingAdapter:
    def execute(self, operation: dict[str, object]) -> dict[str, object]:
        raise RuntimeError(f"boom:{operation.get('method')}")


class SecretFailingAdapter:
    def execute(self, operation: dict[str, object]) -> dict[str, object]:
        raise RuntimeError("token=super-secret")


class SecretReturningAdapter:
    def execute(self, operation: dict[str, object]) -> dict[str, object]:
        return {
            "token": "abc",
            "nested": {"session": "secret"},
            "items": [{"authorization": "secret"}],
            "ok": True,
        }


class RecordingImportAdapter:
    def __init__(self) -> None:
        self.operations: list[dict[str, object]] = []

    def execute(self, operation: dict[str, object]) -> dict[str, object]:
        self.operations.append(dict(operation))
        if operation.get("method") == "messages.initHistoryImport":
            return {"import_id": 12345}
        return {"ok": True}


class MissingCaptureAdapter:
    def __init__(self) -> None:
        self.operations: list[dict[str, object]] = []

    def execute(self, operation: dict[str, object]) -> dict[str, object]:
        self.operations.append(dict(operation))
        return {"ok": True}


class TelegramExecutorTests(unittest.TestCase):
    def test_executes_ready_plan_with_dry_run_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = _write_plan(root, ready=True)
            store = JobStore(root / "jobs" / "job-1")

            result = execute_import_plan(plan_path=plan_path, job_store=store, job_id="job-1")
            events = store.read_events()

            self.assertTrue(result.ok)
            self.assertEqual(result.operations_completed, 2)
            self.assertEqual(
                [event["phase"] for event in events],
                [
                    "telegram_import",
                    "telegram_import_operation",
                    "telegram_import_operation",
                    "telegram_import",
                ],
            )
            self.assertEqual(events[1]["data"]["adapter_result"]["dry_run"], True)

    def test_refuses_not_ready_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = _write_plan(root, ready=False)
            store = JobStore(root / "jobs" / "job-1")

            result = execute_import_plan(plan_path=plan_path, job_store=store, job_id="job-1")

            self.assertFalse(result.ok)
            self.assertEqual(result.operations_completed, 0)
            self.assertIn("not ready", "\n".join(result.issues))
            self.assertEqual(store.read_events()[0]["kind"], "error")

    def test_invalid_plan_json_records_job_error_without_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / "import-plan.json"
            plan_path.write_text("{not-json}", encoding="utf-8")
            store = JobStore(root / "jobs" / "job-1")
            adapter = RecordingImportAdapter()

            result = execute_import_plan(
                plan_path=plan_path,
                job_store=store,
                job_id="job-1",
                adapter=adapter,
            )

            self.assertFalse(result.ok)
            self.assertEqual(adapter.operations, [])
            self.assertIn("not valid JSON", "\n".join(result.issues))
            self.assertEqual(store.read_events()[0]["kind"], "error")

    def test_missing_plan_records_job_error_without_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = JobStore(root / "jobs" / "job-1")
            adapter = RecordingImportAdapter()

            result = execute_import_plan(
                plan_path=root / "missing-plan.json",
                job_store=store,
                job_id="job-1",
                adapter=adapter,
            )

            self.assertFalse(result.ok)
            self.assertEqual(adapter.operations, [])
            self.assertIn("does not exist", "\n".join(result.issues))
            self.assertEqual(store.read_events()[0]["kind"], "error")

    def test_plan_directory_records_job_error_without_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / "import-plan.json"
            plan_path.mkdir()
            store = JobStore(root / "jobs" / "job-1")
            adapter = RecordingImportAdapter()

            result = execute_import_plan(
                plan_path=plan_path,
                job_store=store,
                job_id="job-1",
                adapter=adapter,
            )

            self.assertFalse(result.ok)
            self.assertEqual(adapter.operations, [])
            self.assertIn("must be a file", "\n".join(result.issues))
            self.assertEqual(store.read_events()[0]["kind"], "error")

    def test_non_object_plan_records_job_error_without_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / "import-plan.json"
            plan_path.write_text('["not", "object"]', encoding="utf-8")
            store = JobStore(root / "jobs" / "job-1")
            adapter = RecordingImportAdapter()

            result = execute_import_plan(
                plan_path=plan_path,
                job_store=store,
                job_id="job-1",
                adapter=adapter,
            )

            self.assertFalse(result.ok)
            self.assertEqual(adapter.operations, [])
            self.assertIn("must be a JSON object", "\n".join(result.issues))
            self.assertEqual(store.read_events()[0]["kind"], "error")

    def test_records_adapter_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = _write_plan(root, ready=True)
            store = JobStore(root / "jobs" / "job-1")

            result = execute_import_plan(
                plan_path=plan_path,
                job_store=store,
                job_id="job-1",
                adapter=FailingAdapter(),
            )

            self.assertFalse(result.ok)
            self.assertEqual(result.operations_completed, 0)
            self.assertIn("boom", "\n".join(result.issues))
            self.assertEqual(store.read_events()[-1]["kind"], "error")

    def test_redacts_adapter_failure_issues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = _write_plan(root, ready=True)
            store = JobStore(root / "jobs" / "job-1")

            result = execute_import_plan(
                plan_path=plan_path,
                job_store=store,
                job_id="job-1",
                adapter=SecretFailingAdapter(),
            )

            issues = "\n".join(result.issues)
            self.assertFalse(result.ok)
            self.assertIn("token=[redacted]", issues)
            self.assertNotIn("super-secret", issues)

    def test_refuses_duplicate_completed_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = _write_plan(root, ready=True)
            store = JobStore(root / "jobs" / "job-1")

            first = execute_import_plan(plan_path=plan_path, job_store=store, job_id="job-1")
            second = execute_import_plan(plan_path=plan_path, job_store=store, job_id="job-1")

            self.assertTrue(first.ok)
            self.assertFalse(second.ok)
            self.assertIn("already completed", "\n".join(second.issues))

    def test_redacts_adapter_result_in_job_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = _write_plan(root, ready=True)
            store = JobStore(root / "jobs" / "job-1")

            result = execute_import_plan(
                plan_path=plan_path,
                job_store=store,
                job_id="job-1",
                adapter=SecretReturningAdapter(),
            )
            event_data = store.read_events()[1]["data"]["adapter_result"]

            self.assertTrue(result.ok)
            self.assertEqual(event_data["token"], "[redacted]")
            self.assertEqual(event_data["nested"]["session"], "[redacted]")
            self.assertEqual(event_data["items"][0]["authorization"], "[redacted]")
            self.assertEqual(event_data["ok"], True)

    def test_injects_captured_import_id_into_dependent_operations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = _write_import_id_plan(root)
            store = JobStore(root / "jobs" / "job-1")
            adapter = RecordingImportAdapter()

            result = execute_import_plan(
                plan_path=plan_path,
                job_store=store,
                job_id="job-1",
                adapter=adapter,
            )

            self.assertTrue(result.ok)
            self.assertEqual(adapter.operations[1]["import_id"], 12345)

    def test_import_id_state_uses_trimmed_conversation_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / "import-plan.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "format": "exodus.telegram.mtproto.import_plan.v1",
                        "ready": True,
                        "operations": [
                            {
                                "method": "messages.initHistoryImport",
                                "conversation_id": " room-1 ",
                                "peer": "@archive",
                                "file_path": "/tmp/messages.txt",
                                "media_count": 0,
                                "captures": ["import_id"],
                            },
                            {
                                "method": "messages.startHistoryImport",
                                "conversation_id": "room-1",
                                "peer": "@archive",
                                "requires_import_id": True,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            store = JobStore(root / "jobs" / "job-1")
            adapter = RecordingImportAdapter()

            result = execute_import_plan(
                plan_path=plan_path,
                job_store=store,
                job_id="job-1",
                adapter=adapter,
            )

            self.assertTrue(result.ok)
            self.assertEqual(adapter.operations[0]["conversation_id"], "room-1")
            self.assertEqual(adapter.operations[1]["import_id"], 12345)

    def test_fails_immediately_when_required_capture_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = _write_import_id_plan(root)
            store = JobStore(root / "jobs" / "job-1")
            adapter = MissingCaptureAdapter()

            result = execute_import_plan(
                plan_path=plan_path,
                job_store=store,
                job_id="job-1",
                adapter=adapter,
            )

            self.assertFalse(result.ok)
            self.assertEqual(result.operations_completed, 0)
            self.assertEqual([operation["method"] for operation in adapter.operations], ["messages.initHistoryImport"])
            self.assertIn("did not return required captures: import_id", "\n".join(result.issues))

    def test_fails_when_import_id_is_required_before_capture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / "import-plan.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "format": "exodus.telegram.mtproto.import_plan.v1",
                        "ready": True,
                        "operations": [
                            {
                                "method": "messages.startHistoryImport",
                                "conversation_id": "room-1",
                                "requires_import_id": True,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            store = JobStore(root / "jobs" / "job-1")

            result = execute_import_plan(plan_path=plan_path, job_store=store, job_id="job-1")

            self.assertFalse(result.ok)
            self.assertIn("requires import_id", "\n".join(result.issues))

    def test_preflights_all_operations_before_adapter_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / "import-plan.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "format": "exodus.telegram.mtproto.import_plan.v1",
                        "ready": True,
                        "operations": [
                            {
                                "method": "messages.checkHistoryImport",
                                "conversation_id": "room-1",
                                "import_head_path": "/tmp/messages.txt",
                            },
                            {
                                "method": "messages.startHistoryImport",
                                "conversation_id": "room-1",
                                "peer": "@archive",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            store = JobStore(root / "jobs" / "job-1")
            adapter = RecordingImportAdapter()

            result = execute_import_plan(
                plan_path=plan_path,
                job_store=store,
                job_id="job-1",
                adapter=adapter,
            )

            self.assertFalse(result.ok)
            self.assertEqual(adapter.operations, [])
            self.assertIn("requires import_id before it has been captured", "\n".join(result.issues))
            self.assertEqual(store.read_events()[0]["kind"], "error")

    def test_rejects_unsupported_operation_method_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / "import-plan.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "format": "exodus.telegram.mtproto.import_plan.v1",
                        "ready": True,
                        "operations": [
                            {
                                "method": "messages.deleteHistory",
                                "conversation_id": "room-1",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            store = JobStore(root / "jobs" / "job-1")

            result = execute_import_plan(plan_path=plan_path, job_store=store, job_id="job-1")

            self.assertFalse(result.ok)
            self.assertIn("unsupported method", "\n".join(result.issues))

    def test_rejects_missing_required_operation_fields_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / "import-plan.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "format": "exodus.telegram.mtproto.import_plan.v1",
                        "ready": True,
                        "operations": [
                            {
                                "method": "messages.checkHistoryImportPeer",
                                "conversation_id": "room-1",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            store = JobStore(root / "jobs" / "job-1")

            result = execute_import_plan(plan_path=plan_path, job_store=store, job_id="job-1")

            self.assertFalse(result.ok)
            self.assertIn("missing required fields: peer", "\n".join(result.issues))

    def test_trims_required_operation_string_fields_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / "import-plan.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "format": "exodus.telegram.mtproto.import_plan.v1",
                        "ready": True,
                        "operations": [
                            {
                                "method": "messages.checkHistoryImportPeer",
                                "conversation_id": " room-1 ",
                                "peer": " @archive ",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            store = JobStore(root / "jobs" / "job-1")
            adapter = RecordingImportAdapter()

            result = execute_import_plan(
                plan_path=plan_path,
                job_store=store,
                job_id="job-1",
                adapter=adapter,
            )

            self.assertTrue(result.ok)
            self.assertEqual(adapter.operations[0]["conversation_id"], "room-1")
            self.assertEqual(adapter.operations[0]["peer"], "@archive")

    def test_rejects_malformed_operation_field_types_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / "import-plan.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "format": "exodus.telegram.mtproto.import_plan.v1",
                        "ready": True,
                        "operations": [
                            {
                                "method": "messages.initHistoryImport",
                                "conversation_id": "room-1",
                                "peer": 123,
                                "file_path": "/tmp/messages.txt",
                                "media_count": "0",
                                "requires_import_id": "false",
                                "captures": ["import_id"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            store = JobStore(root / "jobs" / "job-1")
            adapter = RecordingImportAdapter()

            result = execute_import_plan(
                plan_path=plan_path,
                job_store=store,
                job_id="job-1",
                adapter=adapter,
            )

            self.assertFalse(result.ok)
            self.assertEqual(adapter.operations, [])
            issues = "\n".join(result.issues)
            self.assertIn("field peer must be a non-empty string", issues)
            self.assertIn("media_count must be a non-negative integer", issues)
            self.assertIn("requires_import_id must be a boolean", issues)

    def test_rejects_non_string_capture_entries_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / "import-plan.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "format": "exodus.telegram.mtproto.import_plan.v1",
                        "ready": True,
                        "operations": [
                            {
                                "method": "messages.initHistoryImport",
                                "conversation_id": "room-1",
                                "peer": "@archive",
                                "file_path": "/tmp/messages.txt",
                                "media_count": 0,
                                "captures": ["import_id", 123, ""],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            store = JobStore(root / "jobs" / "job-1")
            adapter = RecordingImportAdapter()

            result = execute_import_plan(
                plan_path=plan_path,
                job_store=store,
                job_id="job-1",
                adapter=adapter,
            )

            issues = "\n".join(result.issues)
            self.assertFalse(result.ok)
            self.assertEqual(adapter.operations, [])
            self.assertIn("capture 1 must be a non-empty string", issues)
            self.assertIn("capture 2 must be a non-empty string", issues)

    def test_subprocess_adapter_executes_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "adapter.py"
            script.write_text(
                "import json, sys\n"
                "operation = json.loads(sys.stdin.read())\n"
                "print(json.dumps({'seen': operation['method']}))\n",
                encoding="utf-8",
            )

            adapter = SubprocessTelegramAdapter(command=["python3", str(script)])
            result = adapter.execute({"method": "messages.checkHistoryImport"})

            self.assertEqual(result["seen"], "messages.checkHistoryImport")
            self.assertEqual(result["subprocess"], True)

    def test_subprocess_adapter_reports_nonzero_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "adapter.py"
            script.write_text("import sys\nsys.stderr.write('bad')\nsys.exit(3)\n", encoding="utf-8")

            adapter = SubprocessTelegramAdapter(command=["python3", str(script)])

            with self.assertRaisesRegex(RuntimeError, "exited 3"):
                adapter.execute({"method": "messages.checkHistoryImport"})

    def test_subprocess_adapter_rejects_nonpositive_timeout(self) -> None:
        adapter = SubprocessTelegramAdapter(command=["python3"], timeout_seconds=0)

        with self.assertRaisesRegex(ValueError, "timeout_seconds"):
            adapter.execute({"method": "messages.checkHistoryImport"})

    def test_subprocess_adapter_reports_timeout_without_command_details(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "adapter.py"
            script.write_text("import time\ntime.sleep(2)\n", encoding="utf-8")

            adapter = SubprocessTelegramAdapter(
                command=["python3", str(script), "token=super-secret"],
                timeout_seconds=1,
            )

            with self.assertRaisesRegex(TimeoutError, "timed out") as context:
                adapter.execute({"method": "messages.checkHistoryImport"})

            self.assertNotIn("super-secret", str(context.exception))
            self.assertNotIn(str(script), str(context.exception))

    def test_subprocess_adapter_requires_json_object_response(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "adapter.py"
            script.write_text("print('[]')\n", encoding="utf-8")

            adapter = SubprocessTelegramAdapter(command=["python3", str(script)])

            with self.assertRaisesRegex(ValueError, "JSON object"):
                adapter.execute({"method": "messages.checkHistoryImport"})

    def test_subprocess_adapter_redacts_invalid_json_response(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "adapter.py"
            script.write_text("print('token=super-secret {not json')\n", encoding="utf-8")

            adapter = SubprocessTelegramAdapter(command=["python3", str(script)])

            with self.assertRaisesRegex(ValueError, r"token=\[redacted\]") as context:
                adapter.execute({"method": "messages.checkHistoryImport"})
            self.assertNotIn("super-secret", str(context.exception))


def _write_plan(root: Path, *, ready: bool) -> Path:
    plan_path = root / "import-plan.json"
    plan = {
        "format": "exodus.telegram.mtproto.import_plan.v1",
        "ready": ready,
        "operations": [
            {
                "method": "messages.checkHistoryImport",
                "conversation_id": "room-1",
                "import_head_path": "/tmp/messages.txt",
            },
            {
                "method": "messages.checkHistoryImportPeer",
                "conversation_id": "room-1",
                "peer": "@archive",
            },
        ],
    }
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    return plan_path


def _write_import_id_plan(root: Path) -> Path:
    plan_path = root / "import-plan.json"
    plan = {
        "format": "exodus.telegram.mtproto.import_plan.v1",
        "ready": True,
        "operations": [
            {
                "method": "messages.initHistoryImport",
                "conversation_id": "room-1",
                "peer": "@archive",
                "file_path": "/tmp/messages.txt",
                "media_count": 0,
                "captures": ["import_id"],
            },
            {
                "method": "messages.startHistoryImport",
                "conversation_id": "room-1",
                "peer": "@archive",
                "requires_import_id": True,
            },
        ],
    }
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    return plan_path


if __name__ == "__main__":
    unittest.main()
