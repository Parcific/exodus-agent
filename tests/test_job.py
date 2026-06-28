from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from exodus_agent.job import JobEvent, JobEventKind, JobStore, validate_job_id


class JobStoreTests(unittest.TestCase):
    def test_job_store_appends_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp) / "jobs" / "job-1")
            store.create(job_id="job-1")
            store.append(
                JobEvent(
                    kind=JobEventKind.PHASE_STARTED,
                    job_id="job-1",
                    phase="extract",
                    message="started extraction",
                )
            )

            events = store.read_events()
            self.assertEqual([event["kind"] for event in events], ["created", "phase_started"])
            self.assertEqual(events[1]["phase"], "extract")

    def test_create_rejects_existing_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp) / "jobs" / "job-1")
            store.create(job_id="job-1")

            with self.assertRaises(FileExistsError):
                store.create(job_id="job-1")

    def test_create_rejects_job_id_that_does_not_match_store_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp) / "jobs" / "job-1")

            with self.assertRaisesRegex(ValueError, "does not match job store path"):
                store.create(job_id="job-2")

    def test_create_rejects_directory_events_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp) / "jobs" / "job-1")
            store.root.mkdir(parents=True)
            store.events_path.mkdir()

            with self.assertRaisesRegex(ValueError, "events JSONL path must be a file"):
                store.create(job_id="job-1")

    def test_rejects_path_traversal_job_ids(self) -> None:
        with self.assertRaisesRegex(ValueError, "Job ID"):
            validate_job_id("../escape")

    def test_append_rejects_invalid_event_job_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp) / "jobs" / "job-1")

            with self.assertRaisesRegex(ValueError, "Job ID"):
                store.append(JobEvent(kind=JobEventKind.ERROR, job_id="../escape"))

    def test_append_rejects_event_job_id_that_does_not_match_store_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp) / "jobs" / "job-1")

            with self.assertRaisesRegex(ValueError, "does not match job store path"):
                store.append(JobEvent(kind=JobEventKind.ERROR, job_id="job-2"))

    def test_append_rejects_directory_events_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp) / "jobs" / "job-1")
            store.root.mkdir(parents=True)
            store.events_path.mkdir()

            with self.assertRaisesRegex(ValueError, "events JSONL path must be a file"):
                store.append(JobEvent(kind=JobEventKind.ERROR, job_id="job-1"))

    def test_read_events_rejects_directory_events_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp) / "jobs" / "job-1")
            store.root.mkdir(parents=True)
            store.events_path.mkdir()

            with self.assertRaisesRegex(ValueError, "events JSONL path must be a file"):
                store.read_events()

    def test_read_events_rejects_invalid_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp) / "jobs" / "job-1")
            store.root.mkdir(parents=True)
            store.events_path.write_text("{not-json}\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "events.jsonl:1"):
                store.read_events()

    def test_read_events_rejects_non_utf8_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp) / "jobs" / "job-1")
            store.root.mkdir(parents=True)
            store.events_path.write_bytes(b"\xff")

            with self.assertRaisesRegex(ValueError, "not valid UTF-8"):
                store.read_events()

    def test_read_events_rejects_non_object_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp) / "jobs" / "job-1")
            store.root.mkdir(parents=True)
            store.events_path.write_text('["not", "object"]\n', encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "row must be an object"):
                store.read_events()

    def test_read_events_rejects_invalid_event_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp) / "jobs" / "job-1")
            store.root.mkdir(parents=True)
            store.events_path.write_text(
                '{"created_at":"2026-01-01T00:00:00+00:00","data":{},"id":"event-1",'
                '"job_id":"job-1","kind":"unknown","message":null,"phase":null}\n',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "invalid kind"):
                store.read_events()

    def test_read_events_rejects_row_job_id_that_does_not_match_store_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp) / "jobs" / "job-1")
            store.root.mkdir(parents=True)
            store.events_path.write_text(
                '{"created_at":"2026-01-01T00:00:00+00:00","data":{},"id":"event-1",'
                '"job_id":"job-2","kind":"created","message":null,"phase":null}\n',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "does not match job store path"):
                store.read_events()

    def test_read_events_rejects_invalid_created_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp) / "jobs" / "job-1")
            store.root.mkdir(parents=True)
            store.events_path.write_text(
                '{"created_at":"not-a-date","data":{},"id":"event-1",'
                '"job_id":"job-1","kind":"created","message":null,"phase":null}\n',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "invalid created_at"):
                store.read_events()

    def test_read_events_rejects_naive_created_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp) / "jobs" / "job-1")
            store.root.mkdir(parents=True)
            store.events_path.write_text(
                '{"created_at":"2026-01-01T00:00:00","data":{},"id":"event-1",'
                '"job_id":"job-1","kind":"created","message":null,"phase":null}\n',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "invalid created_at"):
                store.read_events()


if __name__ == "__main__":
    unittest.main()
