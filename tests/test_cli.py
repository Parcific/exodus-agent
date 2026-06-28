from __future__ import annotations

import tempfile
import unittest
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from exodus_agent.archive import Archive
from exodus_agent.cli import (
    _load_destination_map,
    _load_entra_identity_prefill_for_cli,
    _load_teams_conversation_map_for_cli,
    _load_teams_identity_map_for_cli,
    main,
)
from exodus_agent.model import Attachment, Conversation, ConversationKind, Message, Participant, Workspace


class FakeCliWebexSource:
    def get_workspace(self) -> Workspace:
        return Workspace(source_id="org-1", source_kind="webex")

    def list_conversations(self) -> tuple[Conversation, ...]:
        return (Conversation(source_id="room-1", kind=ConversationKind.SPACE, title="General"),)

    def list_participants(self) -> tuple[Participant, ...]:
        return (Participant(source_id="u1", display_name="Ada", email="ada@example.com"),)

    def list_messages(self, conversation: Conversation) -> tuple[Message, ...]:
        return (
            Message(
                source_id="msg-1",
                conversation_id=conversation.source_id,
                author_id="u1",
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                text="hello",
                attachments=(Attachment(source_id="file-1", filename="notes.txt"),),
            ),
        )

    def download_attachment(self, attachment: Attachment) -> bytes:
        return b"notes"


class CliTests(unittest.TestCase):
    def test_teams_identity_and_conversation_map_template_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "migration.toml"
            workspace = root / "workspace"
            config_path.write_text(
                f"""
name = "demo"
workspace = "{workspace}"

[source]
kind = "webex"

[target]
kind = "teams"
""".strip(),
                encoding="utf-8",
            )
            archive = Archive(workspace / "archive")
            archive.initialize(source_kind="webex", target_kind="teams", name="demo")
            archive.write_conversations(
                [Conversation(source_id="space-1", kind=ConversationKind.SPACE, title="General")]
            )
            archive.write_participants(
                [
                    Participant(source_id="u1", display_name="Ada", email="ada@example.com"),
                    Participant(source_id="u2", display_name="Grace", email="grace@example.com"),
                ]
            )
            archive.write_messages(
                "space-1",
                [
                    Message(
                        source_id="msg-1",
                        conversation_id="space-1",
                        author_id="u1",
                        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                        text="hello",
                    ),
                    Message(
                        source_id="msg-2",
                        conversation_id="space-1",
                        author_id="u2",
                        created_at=datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc),
                        text="hi",
                    ),
                ],
            )

            with patch("builtins.print"):
                main(["teams-identity-map-template", "--config", str(config_path)])
            identity_path = workspace / "archive" / "mappings" / "teams-identity-map.json"
            identity_payload = json.loads(identity_path.read_text(encoding="utf-8"))
            for row in identity_payload["identities"]:
                row["entra_user_id"] = f"entra-{row['source_user_id']}"
            identity_path.write_text(json.dumps(identity_payload), encoding="utf-8")

            with patch("builtins.print"):
                main(
                    [
                        "teams-conversation-map-template",
                        "--config",
                        str(config_path),
                        "--identity-map",
                        str(identity_path),
                    ]
                )

            conversation_path = workspace / "archive" / "mappings" / "teams-conversation-map.json"
            conversation_payload = json.loads(conversation_path.read_text(encoding="utf-8"))
            self.assertEqual(conversation_payload["conversations"][0]["target_kind"], "group_chat")
            conversation_payload["conversations"][0]["target"]["chat_id"] = "chat-1"
            conversation_path.write_text(json.dumps(conversation_payload), encoding="utf-8")

            with patch("builtins.print"):
                main(
                    [
                        "teams-import-plan",
                        "--config",
                        str(config_path),
                        "--identity-map",
                        str(identity_path),
                        "--conversation-map",
                        str(conversation_path),
                    ]
                )
            plan_path = workspace / "archive" / "plans" / "teams-import-plan.json"
            plan_payload = json.loads(plan_path.read_text(encoding="utf-8"))
            self.assertEqual(plan_payload["format"], "exodus.teams.import_plan.v1")
            self.assertEqual(len(plan_payload["messages"]), 2)
            self.assertEqual(plan_payload["messages"][0]["createdDateTime"], "2026-01-01T00:00:00.000Z")

            with patch("builtins.print"):
                main(["teams-execute-plan", "--config", str(config_path)])
            message_map_path = workspace / "archive" / "mappings" / "teams-message-map.json"
            message_map_payload = json.loads(message_map_path.read_text(encoding="utf-8"))
            self.assertEqual(message_map_payload["format"], "exodus.teams.message_map.v1")
            self.assertEqual(
                {row["source_message_id"] for row in message_map_payload["messages"]},
                {"msg-1", "msg-2"},
            )

            with patch("builtins.print"):
                main(["teams-verify-import", "--config", str(config_path)])
            report_path = workspace / "archive" / "reports" / "teams-import-verification.json"
            report_payload = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report_payload["format"], "exodus.teams.import_verification.v1")
            self.assertEqual(report_payload["ok"], True)
            self.assertEqual(report_payload["messages_mapped"], 2)

            with patch("builtins.print"):
                main(
                    [
                        "teams-dry-run-workflow",
                        "--config",
                        str(config_path),
                        "--identity-map",
                        str(identity_path),
                        "--conversation-map",
                        str(conversation_path),
                        "--plan",
                        str(workspace / "archive" / "plans" / "teams-workflow-plan.json"),
                        "--message-map",
                        str(workspace / "archive" / "mappings" / "teams-workflow-message-map.json"),
                        "--report",
                        str(workspace / "archive" / "reports" / "teams-workflow-verification.json"),
                    ]
                )
            workflow_report = json.loads(
                (workspace / "archive" / "reports" / "teams-workflow-verification.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(workflow_report["ok"], True)
            self.assertEqual(workflow_report["messages_mapped"], 2)

    def test_teams_identity_map_template_prefills_from_entra_users_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "migration.toml"
            workspace = root / "workspace"
            config_path.write_text(
                f"""
name = "demo"
workspace = "{workspace}"

[source]
kind = "webex"

[target]
kind = "teams"
""".strip(),
                encoding="utf-8",
            )
            archive = Archive(workspace / "archive")
            archive.initialize(source_kind="webex", target_kind="teams", name="demo")
            archive.write_participants(
                [
                    Participant(source_id="u1", display_name="Ada", email="ada@example.com"),
                    Participant(source_id="u2", display_name="Grace", email="grace@example.com"),
                ]
            )
            entra_path = root / "entra-users.json"
            entra_path.write_text(
                json.dumps(
                    {
                        "value": [
                            {"id": "entra-u1", "mail": "ADA@example.com"},
                            {"id": "entra-u2-generated", "mail": "grace@example.com"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            prefill_path = root / "prefill.json"
            prefill_path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.identity_map.v1",
                        "identities": [
                            {"source_user_id": "u2", "entra_user_id": "entra-u2-approved"}
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with patch("builtins.print"):
                main(
                    [
                        "teams-identity-map-template",
                        "--config",
                        str(config_path),
                        "--entra-users",
                        str(entra_path),
                        "--prefill",
                        str(prefill_path),
                    ]
                )
            identity_path = workspace / "archive" / "mappings" / "teams-identity-map.json"
            payload = json.loads(identity_path.read_text(encoding="utf-8"))
            rows = {row["source_user_id"]: row for row in payload["identities"]}

            self.assertEqual(rows["u1"]["entra_user_id"], "entra-u1")
            self.assertEqual(rows["u1"]["reason"], "Exact Webex email matched Entra mail.")
            self.assertEqual(rows["u2"]["entra_user_id"], "entra-u2-approved")
            self.assertEqual(rows["u2"]["reason"], "Pre-filled from existing identity map.")

    def test_webex_teams_dry_run_command_runs_with_mocked_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "migration.toml"
            workspace = root / "workspace"
            config_path.write_text(
                f"""
name = "demo"
workspace = "{workspace}"

[source]
kind = "webex"

[target]
kind = "teams"
""".strip(),
                encoding="utf-8",
            )
            identity_path = root / "teams-identity-map.json"
            identity_path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.identity_map.v1",
                        "identities": [{"source_user_id": "u1", "entra_user_id": "entra-u1"}],
                    }
                ),
                encoding="utf-8",
            )
            conversation_path = root / "teams-conversation-map.json"
            conversation_path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.mapping_template.v1",
                        "conversations": [
                            {
                                "source_conversation_id": "room-1",
                                "target_kind": "group_chat",
                                "target": {"chat_id": "chat-1"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with patch("exodus_agent.cli.webex_source_from_config", return_value=FakeCliWebexSource()):
                with patch("builtins.print"):
                    main(
                        [
                            "webex-teams-dry-run",
                            "--config",
                            str(config_path),
                            "--identity-map",
                            str(identity_path),
                            "--conversation-map",
                            str(conversation_path),
                        ]
                    )

            report_path = workspace / "archive" / "reports" / "teams-import-verification.json"
            report_payload = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report_payload["ok"], True)
            self.assertEqual(report_payload["messages_mapped"], 1)
            self.assertTrue((workspace / "archive" / "attachments").exists())

    def test_webex_teams_dry_run_command_reports_stale_message_map_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "migration.toml"
            workspace = root / "workspace"
            config_path.write_text(
                f"""
name = "demo"
workspace = "{workspace}"

[source]
kind = "webex"

[target]
kind = "teams"
""".strip(),
                encoding="utf-8",
            )
            identity_path = root / "teams-identity-map.json"
            identity_path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.identity_map.v1",
                        "identities": [{"source_user_id": "u1", "entra_user_id": "entra-u1"}],
                    }
                ),
                encoding="utf-8",
            )
            conversation_path = root / "teams-conversation-map.json"
            conversation_path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.mapping_template.v1",
                        "conversations": [
                            {
                                "source_conversation_id": "room-1",
                                "target_kind": "group_chat",
                                "target": {"chat_id": "chat-1"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            message_map_path = root / "stale-teams-message-map.json"
            message_map_path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.message_map.v1",
                        "messages": [{"source_message_id": "old", "teams_message_id": "teams-old"}],
                    }
                ),
                encoding="utf-8",
            )

            with patch("exodus_agent.cli.webex_source_from_config", return_value=FakeCliWebexSource()):
                with patch("builtins.print"):
                    with self.assertRaisesRegex(SystemExit, "stale mappings"):
                        main(
                            [
                                "webex-teams-dry-run",
                                "--config",
                                str(config_path),
                                "--identity-map",
                                str(identity_path),
                                "--conversation-map",
                                str(conversation_path),
                                "--message-map",
                                str(message_map_path),
                            ]
                        )

    def test_rejects_path_traversal_job_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "migration.toml"
            workspace = root / "workspace"
            escaped = root / "escape"
            config_path.write_text(
                f"""
name = "demo"
workspace = "{workspace}"

[source]
kind = "webex"

[target]
kind = "teams"
""".strip(),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(SystemExit, "Job ID"):
                with patch("builtins.print"):
                    main(["init-job", "--config", str(config_path), "--job-id", "../escape"])

            self.assertFalse(escaped.exists())

    def test_telegram_destination_map_template_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "migration.toml"
            workspace = root / "workspace"
            config_path.write_text(
                f"""
name = "demo"
workspace = "{workspace}"

[source]
kind = "webex"

[target]
kind = "telegram"
""".strip(),
                encoding="utf-8",
            )
            archive = Archive(workspace / "archive")
            archive.initialize(source_kind="webex", target_kind="telegram", name="demo")
            archive.write_conversations(
                [Conversation(source_id="room-1", kind=ConversationKind.SPACE, title="General")]
            )

            with patch("builtins.print"):
                main(["telegram-destination-map-template", "--config", str(config_path)])

            output = workspace / "archive" / "mappings" / "telegram-destination-map.json"
            self.assertTrue(output.exists())
            self.assertIn('"title": "General"', output.read_text(encoding="utf-8"))

    def test_loads_compact_destination_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "destination-map.json"
            path.write_text('{" room-1 ":" @archive "}\n', encoding="utf-8")

            self.assertEqual(_load_destination_map(path), {"room-1": "@archive"})

    def test_rejects_duplicate_destination_map_conversation_ids_after_trim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "destination-map.json"
            path.write_text('{"room-1":"@archive"," room-1 ":"@other"}\n', encoding="utf-8")

            with self.assertRaisesRegex(SystemExit, "duplicates conversation id: room-1"):
                _load_destination_map(path)

    def test_loads_annotated_destination_map_template(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "destination-map.json"
            path.write_text(
                """
{
  "room-1": {
    "peer": "@archive",
    "title": "General",
    "kind": "space",
    "message_count": 12
  }
}
""".strip(),
                encoding="utf-8",
            )

            self.assertEqual(_load_destination_map(path), {"room-1": "@archive"})

    def test_rejects_empty_destination_map_peer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "destination-map.json"
            path.write_text('{"room-1":{"peer":"   "}}\n', encoding="utf-8")

            with self.assertRaises(SystemExit):
                _load_destination_map(path)

    def test_rejects_empty_destination_map_conversation_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "destination-map.json"
            path.write_text('{"   ":"@archive"}\n', encoding="utf-8")

            with self.assertRaisesRegex(SystemExit, "non-empty strings"):
                _load_destination_map(path)

    def test_rejects_missing_destination_map_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing.json"

            with self.assertRaisesRegex(SystemExit, "does not exist"):
                _load_destination_map(path)

    def test_rejects_destination_map_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "destination-map.json"
            path.mkdir()

            with self.assertRaisesRegex(SystemExit, "must be a file"):
                _load_destination_map(path)

    def test_rejects_invalid_destination_map_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "destination-map.json"
            path.write_text("{not json", encoding="utf-8")

            with self.assertRaisesRegex(SystemExit, "not valid JSON"):
                _load_destination_map(path)

    def test_rejects_non_utf8_destination_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "destination-map.json"
            path.write_bytes(b"\xff")

            with self.assertRaisesRegex(SystemExit, "not valid UTF-8"):
                _load_destination_map(path)

    def test_rejects_missing_teams_identity_map_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing.json"

            with self.assertRaisesRegex(SystemExit, "does not exist"):
                _load_teams_identity_map_for_cli(path)

    def test_rejects_teams_identity_map_directory_for_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "identity-map.json"
            path.mkdir()

            with self.assertRaisesRegex(SystemExit, "must be a file"):
                _load_teams_identity_map_for_cli(path)

    def test_rejects_incomplete_teams_identity_map_for_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "identity-map.json"
            path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.identity_map.v1",
                        "identities": [{"source_user_id": "u1", "entra_user_id": ""}],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(SystemExit, "entra_user_id"):
                _load_teams_identity_map_for_cli(path)

    def test_rejects_missing_teams_conversation_map_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing.json"

            with self.assertRaisesRegex(SystemExit, "does not exist"):
                _load_teams_conversation_map_for_cli(path)

    def test_rejects_teams_conversation_map_directory_for_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "conversation-map.json"
            path.mkdir()

            with self.assertRaisesRegex(SystemExit, "must be a file"):
                _load_teams_conversation_map_for_cli(path)

    def test_rejects_incomplete_teams_conversation_map_for_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "conversation-map.json"
            path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.mapping_template.v1",
                        "conversations": [
                            {
                                "source_conversation_id": "space-1",
                                "target_kind": "group_chat",
                                "target": {"chat_id": ""},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(SystemExit, "target.chat_id"):
                _load_teams_conversation_map_for_cli(path)

    def test_telegram_import_plan_cli_raises_system_exit_for_directory_output(self) -> None:
        """CLI boundary: ValueError from lower layer must surface as clean SystemExit."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "migration.toml"
            workspace = root / "workspace"
            config_path.write_text(
                f"""
name = "demo"
workspace = "{workspace}"

[source]
kind = "webex"

[target]
kind = "telegram"
""".strip(),
                encoding="utf-8",
            )
            archive = Archive(workspace / "archive")
            archive.initialize(source_kind="webex", target_kind="telegram", name="demo")
            archive.write_conversations(
                [Conversation(source_id="room-1", kind=ConversationKind.SPACE, title="General")]
            )
            archive.write_participants(
                [Participant(source_id="u1", display_name="Ada", email="ada@example.com")]
            )
            archive.write_messages(
                "room-1",
                [
                    Message(
                        source_id="msg-1",
                        conversation_id="room-1",
                        author_id="u1",
                        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                        text="hello",
                    )
                ],
            )

            package_root = workspace / "telegram-package"
            with patch("builtins.print"):
                main(["telegram-package", "--config", str(config_path)])

            destination_map = root / "destination-map.json"
            destination_map.write_text(json.dumps({"room-1": "@archive_group"}), encoding="utf-8")

            # Create the import-plan output path as a directory so lower layer raises ValueError.
            plan_as_dir = package_root / "import-plan.json"
            plan_as_dir.mkdir(parents=True, exist_ok=True)

            with self.assertRaisesRegex(SystemExit, "import plan output path must be a file"):
                main(
                    [
                        "telegram-import-plan",
                        "--config", str(config_path),
                        "--package", str(package_root),
                        "--destination-map", str(destination_map),
                    ]
                )

    def test_rejects_missing_entra_prefill_file_for_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = Archive(root / "archive")
            path = root / "missing.json"

            with self.assertRaisesRegex(SystemExit, "does not exist"):
                _load_entra_identity_prefill_for_cli(archive=archive, path=path)

    def test_rejects_entra_prefill_directory_for_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = Archive(root / "archive")
            path = root / "entra-users.json"
            path.mkdir()

            with self.assertRaisesRegex(SystemExit, "must be a file"):
                _load_entra_identity_prefill_for_cli(archive=archive, path=path)

    def test_webex_teams_dry_run_secret_resolution_error_raises_system_exit(self) -> None:
        """BUG 1: SecretResolutionError (RuntimeError subclass) must surface as SystemExit, not raw traceback."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "migration.toml"
            workspace = root / "workspace"
            # No 'auth' key under [source] — resolve_secret(None, ...) raises SecretResolutionError.
            config_path.write_text(
                f"""
name = "demo"
workspace = "{workspace}"

[source]
kind = "webex"

[target]
kind = "teams"
""".strip(),
                encoding="utf-8",
            )
            identity_path = root / "teams-identity-map.json"
            identity_path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.identity_map.v1",
                        "identities": [{"source_user_id": "u1", "entra_user_id": "entra-u1"}],
                    }
                ),
                encoding="utf-8",
            )
            conversation_path = root / "teams-conversation-map.json"
            conversation_path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.mapping_template.v1",
                        "conversations": [
                            {
                                "source_conversation_id": "room-1",
                                "target_kind": "group_chat",
                                "target": {"chat_id": "chat-1"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            # webex_source_from_config is called INSIDE the lambda passed to _run_cli_action,
            # so SecretResolutionError must be caught there and converted to SystemExit.
            with patch("builtins.print"):
                with self.assertRaises(SystemExit):
                    main(
                        [
                            "webex-teams-dry-run",
                            "--config",
                            str(config_path),
                            "--identity-map",
                            str(identity_path),
                            "--conversation-map",
                            str(conversation_path),
                        ]
                    )

    def test_export_dry_run_duplicate_job_id_raises_system_exit(self) -> None:
        """BUG 2: export-dry-run re-run with the same job-id raises SystemExit, not raw FileExistsError."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "migration.toml"
            workspace = root / "workspace"
            config_path.write_text(
                f"""
name = "demo"
workspace = "{workspace}"

[source]
kind = "webex"

[target]
kind = "teams"
""".strip(),
                encoding="utf-8",
            )

            with patch("exodus_agent.cli.webex_source_from_config", return_value=FakeCliWebexSource()):
                with patch("builtins.print"):
                    main(["export-dry-run", "--config", str(config_path), "--job-id", "run-1"])

            # Second call with the same job-id must raise SystemExit("Job already exists: run-1"),
            # not a raw FileExistsError traceback.
            with patch("exodus_agent.cli.webex_source_from_config", return_value=FakeCliWebexSource()):
                with self.assertRaisesRegex(SystemExit, "already exists"):
                    main(["export-dry-run", "--config", str(config_path), "--job-id", "run-1"])


if __name__ == "__main__":
    unittest.main()
