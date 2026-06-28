from __future__ import annotations

import tempfile
import unittest
from builtins import __import__ as real_import
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from exodus_agent.mtproto_runner import (
    MtprotoRunnerError,
    RunnerConfig,
    TelethonHistoryImporter,
    execute_operation,
    _read_operation,
)


class MtprotoRunnerTests(unittest.TestCase):
    def test_rejects_unsupported_method(self) -> None:
        with self.assertRaisesRegex(MtprotoRunnerError, "Unsupported"):
            execute_operation(
                {"method": "messages.deleteHistory"},
                _config(live_enabled=False),
            )

    def test_validates_required_fields_before_live_check(self) -> None:
        with self.assertRaisesRegex(MtprotoRunnerError, "missing required fields"):
            execute_operation(
                {"method": "messages.checkHistoryImportPeer", "conversation_id": "room-1"},
                _config(live_enabled=False),
            )

    def test_fails_closed_when_live_disabled(self) -> None:
        with self.assertRaisesRegex(MtprotoRunnerError, "disabled"):
            execute_operation(
                {
                    "method": "messages.startHistoryImport",
                    "conversation_id": "room-1",
                    "peer": "@archive",
                    "import_id": 12345,
                },
                _config(live_enabled=False),
            )

    def test_fails_closed_when_live_enabled_without_telethon(self) -> None:
        def import_without_telethon(name: str, *args: object, **kwargs: object) -> object:
            if name == "telethon":
                raise ImportError("missing telethon")
            return real_import(name, *args, **kwargs)

        with self.assertRaisesRegex(MtprotoRunnerError, "Telethon"):
            with patch("builtins.__import__", side_effect=import_without_telethon):
                TelethonHistoryImporter.from_installed()

    def test_start_history_import_requires_import_id(self) -> None:
        with self.assertRaisesRegex(MtprotoRunnerError, "import_id"):
            execute_operation(
                {
                    "method": "messages.startHistoryImport",
                    "conversation_id": "room-1",
                    "peer": "@archive",
                },
                _config(live_enabled=False),
            )

    def test_rejects_invalid_media_count_before_live_check(self) -> None:
        with self.assertRaisesRegex(MtprotoRunnerError, "media_count"):
            execute_operation(
                {
                    "method": "messages.initHistoryImport",
                    "conversation_id": "room-1",
                    "peer": "@archive",
                    "file_path": "/tmp/messages.txt",
                    "media_count": "many",
                },
                _config(live_enabled=False),
            )

    def test_rejects_string_numeric_fields_before_live_check(self) -> None:
        with self.assertRaisesRegex(MtprotoRunnerError, "import_id"):
            execute_operation(
                {
                    "method": "messages.startHistoryImport",
                    "conversation_id": "room-1",
                    "peer": "@archive",
                    "import_id": "12345",
                },
                _config(live_enabled=False),
            )

    def test_rejects_non_string_required_fields_before_live_check(self) -> None:
        with self.assertRaisesRegex(MtprotoRunnerError, "peer"):
            execute_operation(
                {
                    "method": "messages.checkHistoryImportPeer",
                    "conversation_id": "room-1",
                    "peer": 123,
                },
                _config(live_enabled=False),
            )

    def test_live_importer_rejects_invalid_media_count_before_upload(self) -> None:
        client = FakeClient()
        importer = TelethonHistoryImporter(
            client_factory=lambda *args: client,
            functions=FakeFunctions,
            types=FakeTypes,
        )

        with self.assertRaisesRegex(MtprotoRunnerError, "media_count"):
            execute_operation(
                {
                    "method": "messages.initHistoryImport",
                    "conversation_id": "room-1",
                    "peer": "@archive",
                    "file_path": "/tmp/messages.txt",
                    "media_count": "many",
                },
                _config(live_enabled=True),
                importer=importer,
            )

        self.assertEqual(client.uploads, [])
        self.assertEqual(client.calls, [])

    def test_live_importer_rejects_invalid_import_id_before_upload(self) -> None:
        client = FakeClient()
        importer = TelethonHistoryImporter(
            client_factory=lambda *args: client,
            functions=FakeFunctions,
            types=FakeTypes,
        )

        with self.assertRaisesRegex(MtprotoRunnerError, "import_id"):
            execute_operation(
                {
                    "method": "messages.uploadImportedMedia",
                    "conversation_id": "room-1",
                    "peer": "@archive",
                    "import_id": "abc",
                    "file_name": "notes.txt",
                    "file_path": "/tmp/notes.txt",
                    "source_attachment_id": "file-1",
                },
                _config(live_enabled=True),
                importer=importer,
            )

        self.assertEqual(client.uploads, [])
        self.assertEqual(client.calls, [])

    def test_live_execution_rejects_missing_upload_file_before_client_side_effects(self) -> None:
        client = FakeClient()
        importer = TelethonHistoryImporter(
            client_factory=lambda *args: client,
            functions=FakeFunctions,
            types=FakeTypes,
        )

        with self.assertRaisesRegex(MtprotoRunnerError, "file_path file does not exist"):
            execute_operation(
                {
                    "method": "messages.uploadImportedMedia",
                    "conversation_id": "room-1",
                    "peer": "@archive",
                    "import_id": 12345,
                    "file_name": "notes.txt",
                    "file_path": "/tmp/exodus-missing-notes.txt",
                    "source_attachment_id": "file-1",
                },
                _config(live_enabled=True),
                importer=importer,
            )

        self.assertEqual(client.uploads, [])
        self.assertEqual(client.calls, [])

    def test_live_execution_rejects_directory_upload_file_before_client_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient()
            importer = TelethonHistoryImporter(
                client_factory=lambda *args: client,
                functions=FakeFunctions,
                types=FakeTypes,
            )

            with self.assertRaisesRegex(MtprotoRunnerError, "file_path must reference a file"):
                execute_operation(
                    {
                        "method": "messages.initHistoryImport",
                        "conversation_id": "room-1",
                        "peer": "@archive",
                        "file_path": tmp,
                        "media_count": 0,
                    },
                    _config(live_enabled=True),
                    importer=importer,
                )

            self.assertEqual(client.uploads, [])
            self.assertEqual(client.calls, [])

    def test_empty_stdin_payload_has_clear_error(self) -> None:
        with self.assertRaisesRegex(MtprotoRunnerError, "required on stdin"):
            _read_operation("")

    def test_live_importer_checks_history_import_head(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "messages.txt"
            transcript.write_text("line 1\nline 2\nline 3\n", encoding="utf-8")
            client = FakeClient()
            importer = TelethonHistoryImporter(
                client_factory=lambda *args: client,
                functions=FakeFunctions,
                types=FakeTypes,
            )

            result = importer.execute(
                {
                    "method": "messages.checkHistoryImport",
                    "conversation_id": "room-1",
                    "import_head_path": str(transcript),
                    "import_head_lines": 2,
                },
                _config(live_enabled=True),
            )

            self.assertTrue(result["live"])
            self.assertEqual(client.calls[0].import_head, "line 1\nline 2")

    def test_live_importer_captures_init_history_import_id(self) -> None:
        client = FakeClient(response=SimpleNamespace(id=9876))
        importer = TelethonHistoryImporter(
            client_factory=lambda *args: client,
            functions=FakeFunctions,
            types=FakeTypes,
        )

        result = importer.execute(
            {
                "method": "messages.initHistoryImport",
                "conversation_id": "room-1",
                "peer": "@archive",
                "file_path": "/tmp/messages.txt",
                "media_count": 3,
            },
            _config(live_enabled=True),
        )

        self.assertEqual(result["import_id"], 9876)
        self.assertEqual(client.uploads, ["/tmp/messages.txt"])
        self.assertEqual(client.calls[0].media_count, 3)

    def test_live_importer_uploads_imported_media(self) -> None:
        client = FakeClient()
        importer = TelethonHistoryImporter(
            client_factory=lambda *args: client,
            functions=FakeFunctions,
            types=FakeTypes,
        )

        importer.execute(
            {
                "method": "messages.uploadImportedMedia",
                "conversation_id": "room-1",
                "peer": "@archive",
                "import_id": 9876,
                "file_name": "notes.txt",
                "file_path": "/tmp/notes.txt",
                "source_attachment_id": "file-1",
            },
            _config(live_enabled=True),
        )

        call = client.calls[0]
        self.assertEqual(call.import_id, 9876)
        self.assertEqual(call.file_name, "notes.txt")
        self.assertEqual(call.media.attributes[0].file_name, "notes.txt")

    def test_live_importer_starts_history_import(self) -> None:
        client = FakeClient(response=True)
        importer = TelethonHistoryImporter(
            client_factory=lambda *args: client,
            functions=FakeFunctions,
            types=FakeTypes,
        )

        result = importer.execute(
            {
                "method": "messages.startHistoryImport",
                "conversation_id": "room-1",
                "peer": "@archive",
                "import_id": 9876,
            },
            _config(live_enabled=True),
        )

        self.assertTrue(result["started"])
        self.assertEqual(client.calls[0].import_id, 9876)


def _config(*, live_enabled: bool) -> RunnerConfig:
    return RunnerConfig(
        api_id="1",
        api_hash="hash",
        session="session",
        live_enabled=live_enabled,
    )


@dataclass(frozen=True)
class Request:
    name: str
    kwargs: dict[str, object]

    def __getattr__(self, name: str) -> object:
        return self.kwargs[name]


class FakeClient:
    def __init__(self, response: object | None = None) -> None:
        self.response = response or SimpleNamespace(stringify=lambda: "ok")
        self.calls: list[Request] = []
        self.uploads: list[str] = []

    def __enter__(self) -> FakeClient:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def __call__(self, request: Request) -> object:
        self.calls.append(request)
        return self.response

    def upload_file(self, path: str) -> str:
        self.uploads.append(path)
        return f"uploaded:{path}"


class FakeFunctions:
    class messages:
        @staticmethod
        def CheckHistoryImportRequest(**kwargs: object) -> Request:
            return Request("check", kwargs)

        @staticmethod
        def CheckHistoryImportPeerRequest(**kwargs: object) -> Request:
            return Request("check_peer", kwargs)

        @staticmethod
        def InitHistoryImportRequest(**kwargs: object) -> Request:
            return Request("init", kwargs)

        @staticmethod
        def UploadImportedMediaRequest(**kwargs: object) -> Request:
            return Request("upload_media", kwargs)

        @staticmethod
        def StartHistoryImportRequest(**kwargs: object) -> Request:
            return Request("start", kwargs)


class FakeTypes:
    @dataclass(frozen=True)
    class DocumentAttributeFilename:
        file_name: str

    @dataclass(frozen=True)
    class InputMediaUploadedDocument:
        file: str
        mime_type: str
        attributes: list[object]


if __name__ == "__main__":
    unittest.main()
