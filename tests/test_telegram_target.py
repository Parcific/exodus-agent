from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from exodus_agent.archive import Archive, _safe_filename
from exodus_agent.model import (
    Attachment,
    Conversation,
    ConversationKind,
    Message,
    Participant,
    Workspace,
)
from exodus_agent.targets.telegram import (
    verify_telegram_staging_package,
    write_telegram_destination_map_template,
    write_telegram_import_plan,
    write_telegram_staging_package,
)


def _sample_archive(root: Path) -> Archive:
    archive = Archive(root / "archive")
    archive.initialize(source_kind="webex", target_kind="telegram", name="demo")
    attachment_path = archive.root / "attachments" / "notes.txt"
    attachment_path.parent.mkdir(parents=True, exist_ok=True)
    attachment_path.write_text("attachment body", encoding="utf-8")
    archive.write_workspace(Workspace(source_id="org-1", source_kind="webex"))
    archive.write_conversations(
        [Conversation(source_id="room/1", kind=ConversationKind.SPACE, title="General")]
    )
    archive.write_participants([Participant(source_id="user-1", display_name="Ada")])
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
                        local_path="attachments/notes.txt",
                    ),
                ),
            )
        ],
    )
    return archive


class TelegramTargetTests(unittest.TestCase):
    def test_writes_destination_map_template(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = Archive(root / "archive")
            archive.initialize(source_kind="webex", target_kind="telegram", name="demo")
            archive.write_conversations(
                [
                    Conversation(source_id="room/2", kind=ConversationKind.SPACE, title="Beta"),
                    Conversation(source_id="room/1", kind=ConversationKind.SPACE, title="Alpha"),
                ]
            )

            result = write_telegram_destination_map_template(
                archive=archive,
                output_path=root / "destination-map.json",
            )
            template = json.loads(result.path.read_text(encoding="utf-8"))

            self.assertEqual(result.conversations, 2)
            self.assertEqual(sorted(template), ["room/1", "room/2"])
            self.assertEqual(template["room/1"]["peer"], "")
            self.assertEqual(template["room/1"]["title"], "Alpha")
            self.assertEqual(template["room/1"]["kind"], "space")
            self.assertEqual(template["room/1"]["message_count"], 0)

    def test_destination_map_template_refuses_overwrite_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = _sample_archive(root)
            output = root / "destination-map.json"
            output.write_text('{"existing":"@peer"}\n', encoding="utf-8")

            with self.assertRaises(FileExistsError):
                write_telegram_destination_map_template(archive=archive, output_path=output)

            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), {"existing": "@peer"})

    def test_destination_map_template_overwrites_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = _sample_archive(root)
            output = root / "destination-map.json"
            output.write_text('{"existing":"@peer"}\n', encoding="utf-8")

            write_telegram_destination_map_template(
                archive=archive,
                output_path=output,
                overwrite=True,
            )

            template = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(sorted(template), ["room/1"])
            self.assertEqual(template["room/1"]["peer"], "")

    def test_destination_map_template_rejects_directory_output_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = _sample_archive(root)
            output = root / "destination-map.json"
            output.mkdir()

            with self.assertRaisesRegex(ValueError, "output path must be a file"):
                write_telegram_destination_map_template(
                    archive=archive,
                    output_path=output,
                    overwrite=True,
                )

    def test_destination_map_template_refuses_empty_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = Archive(root / "archive")
            archive.initialize(source_kind="webex", target_kind="telegram", name="demo")

            with self.assertRaisesRegex(ValueError, "no conversations"):
                write_telegram_destination_map_template(
                    archive=archive,
                    output_path=root / "destination-map.json",
                )

    def test_writes_staging_package_from_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = _sample_archive(root)

            result = write_telegram_staging_package(
                archive=archive,
                output_root=root / "telegram-package",
            )

            package_manifest = json.loads(
                (result.package_root / "manifest.json").read_text(encoding="utf-8")
            )
            conversation_dir = result.package_root / _safe_filename("room/1")
            conversation_manifest = json.loads(
                (conversation_dir / "manifest.json").read_text(encoding="utf-8")
            )
            transcript = (conversation_dir / "messages.txt").read_text(encoding="utf-8")

            self.assertEqual(result.conversations, 1)
            self.assertEqual(result.messages, 1)
            self.assertEqual(package_manifest["format"], "exodus.telegram.package.v1")
            self.assertEqual(package_manifest["source_archive"]["source_kind"], "webex")
            self.assertEqual(conversation_manifest["message_count"], 1)
            self.assertIn("MSG [2026-01-01 12:00:00 UTC] Ada: hello", transcript)
            self.assertIn("ATTACH [2026-01-01 12:00:00 UTC] Ada: notes.txt", transcript)

    def test_staging_package_rejects_output_root_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = _sample_archive(root)
            output_root = root / "telegram-package"
            output_root.write_text("keep", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "output root must be a directory"):
                write_telegram_staging_package(archive=archive, output_root=output_root)

            self.assertEqual(output_root.read_text(encoding="utf-8"), "keep")

    def test_verifies_staging_package(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = _sample_archive(root)
            package_root = root / "telegram-package"
            write_telegram_staging_package(archive=archive, output_root=package_root)

            result = verify_telegram_staging_package(archive=archive, package_root=package_root)
            report = json.loads(result.report_path.read_text(encoding="utf-8"))

            self.assertTrue(result.ok)
            self.assertEqual(result.messages_found, 1)
            self.assertEqual(report["ok"], True)

    def test_verification_reports_tampered_package(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = _sample_archive(root)
            package_root = root / "telegram-package"
            write_telegram_staging_package(archive=archive, output_root=package_root)
            conversation_dir = package_root / _safe_filename("room/1")
            (conversation_dir / "messages.txt").write_text("Chat: General\n", encoding="utf-8")

            result = verify_telegram_staging_package(archive=archive, package_root=package_root)

            self.assertFalse(result.ok)
            self.assertIn("transcript line count mismatch", "\n".join(result.issues))

    def test_verification_rejects_duplicate_package_conversation_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = _sample_archive(root)
            package_root = root / "telegram-package"
            write_telegram_staging_package(archive=archive, output_root=package_root)
            manifest_path = package_root / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["conversations"].append(dict(manifest["conversations"][0]))
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            result = verify_telegram_staging_package(archive=archive, package_root=package_root)

            self.assertFalse(result.ok)
            self.assertIn("duplicates conversation", "\n".join(result.issues))
            self.assertIn("Transcript message total mismatch", "\n".join(result.issues))

    def test_verification_reports_invalid_conversation_manifest_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = _sample_archive(root)
            package_root = root / "telegram-package"
            write_telegram_staging_package(archive=archive, output_root=package_root)
            conversation_dir = package_root / _safe_filename("room/1")
            (conversation_dir / "manifest.json").write_text("{not json", encoding="utf-8")

            result = verify_telegram_staging_package(archive=archive, package_root=package_root)

            self.assertFalse(result.ok)
            self.assertIn("Invalid JSON file", "\n".join(result.issues))

    def test_verification_reports_non_utf8_conversation_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = _sample_archive(root)
            package_root = root / "telegram-package"
            write_telegram_staging_package(archive=archive, output_root=package_root)
            conversation_dir = package_root / _safe_filename("room/1")
            (conversation_dir / "manifest.json").write_bytes(b"\xff")

            result = verify_telegram_staging_package(archive=archive, package_root=package_root)

            self.assertFalse(result.ok)
            self.assertIn("not valid UTF-8", "\n".join(result.issues))

    def test_verification_reports_conversation_manifest_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = _sample_archive(root)
            package_root = root / "telegram-package"
            write_telegram_staging_package(archive=archive, output_root=package_root)
            conversation_dir = package_root / _safe_filename("room/1")
            (conversation_dir / "manifest.json").unlink()
            (conversation_dir / "manifest.json").mkdir()

            result = verify_telegram_staging_package(archive=archive, package_root=package_root)

            self.assertFalse(result.ok)
            self.assertIn("JSON file must be a file", "\n".join(result.issues))

    def test_verification_reports_non_utf8_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = _sample_archive(root)
            package_root = root / "telegram-package"
            write_telegram_staging_package(archive=archive, output_root=package_root)
            conversation_dir = package_root / _safe_filename("room/1")
            (conversation_dir / "messages.txt").write_bytes(b"\xff")

            result = verify_telegram_staging_package(archive=archive, package_root=package_root)

            self.assertFalse(result.ok)
            self.assertIn("Transcript file is not valid UTF-8", "\n".join(result.issues))

    def test_verification_reports_transcript_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = _sample_archive(root)
            package_root = root / "telegram-package"
            write_telegram_staging_package(archive=archive, output_root=package_root)
            conversation_dir = package_root / _safe_filename("room/1")
            (conversation_dir / "messages.txt").unlink()
            (conversation_dir / "messages.txt").mkdir()

            result = verify_telegram_staging_package(archive=archive, package_root=package_root)

            self.assertFalse(result.ok)
            self.assertIn("Transcript file must be a file", "\n".join(result.issues))

    def test_verification_rejects_conversation_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = _sample_archive(root)
            package_root = root / "telegram-package"
            write_telegram_staging_package(archive=archive, output_root=package_root)
            manifest_path = package_root / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["conversations"][0]["path"] = "../outside"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            result = verify_telegram_staging_package(archive=archive, package_root=package_root)

            self.assertFalse(result.ok)
            self.assertIn("within package root", "\n".join(result.issues))

    def test_verification_rejects_transcript_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = _sample_archive(root)
            package_root = root / "telegram-package"
            write_telegram_staging_package(archive=archive, output_root=package_root)
            conversation_dir = package_root / _safe_filename("room/1")
            manifest_path = conversation_dir / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["transcript"] = "../outside.txt"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            result = verify_telegram_staging_package(archive=archive, package_root=package_root)

            self.assertFalse(result.ok)
            self.assertIn("within package root", "\n".join(result.issues))

    def test_import_plan_is_ready_with_destination_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = _sample_archive(root)
            package_root = root / "telegram-package"
            write_telegram_staging_package(archive=archive, output_root=package_root)

            result = write_telegram_import_plan(
                archive=archive,
                package_root=package_root,
                destination_map={"room/1": "@general_archive"},
            )
            plan = json.loads(result.plan_path.read_text(encoding="utf-8"))

            self.assertTrue(result.ready)
            self.assertEqual(result.media, 1)
            self.assertEqual(plan["format"], "exodus.telegram.mtproto.import_plan.v1")
            self.assertEqual(
                [operation["method"] for operation in plan["operations"]],
                [
                    "messages.checkHistoryImport",
                    "messages.checkHistoryImportPeer",
                    "messages.initHistoryImport",
                    "messages.uploadImportedMedia",
                    "messages.startHistoryImport",
                ],
            )
            self.assertEqual(plan["operations"][1]["peer"], "@general_archive")
            self.assertEqual(plan["operations"][2]["captures"], ["import_id"])
            self.assertEqual(plan["operations"][3]["requires_import_id"], True)
            self.assertEqual(plan["operations"][3]["local_path"], "attachments/notes.txt")
            self.assertTrue(plan["operations"][3]["file_path"].endswith("archive/attachments/notes.txt"))
            self.assertEqual(plan["operations"][4]["requires_import_id"], True)

    def test_import_plan_trims_destination_peer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = _sample_archive(root)
            package_root = root / "telegram-package"
            write_telegram_staging_package(archive=archive, output_root=package_root)

            result = write_telegram_import_plan(
                archive=archive,
                package_root=package_root,
                destination_map={"room/1": " @general_archive "},
            )
            plan = json.loads(result.plan_path.read_text(encoding="utf-8"))

            self.assertTrue(result.ready)
            self.assertEqual(plan["operations"][1]["peer"], "@general_archive")

    def test_import_plan_trims_destination_conversation_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = _sample_archive(root)
            package_root = root / "telegram-package"
            write_telegram_staging_package(archive=archive, output_root=package_root)

            result = write_telegram_import_plan(
                archive=archive,
                package_root=package_root,
                destination_map={" room/1 ": "@general_archive"},
            )
            plan = json.loads(result.plan_path.read_text(encoding="utf-8"))

            self.assertTrue(result.ready)
            self.assertEqual(plan["operations"][1]["peer"], "@general_archive")

    def test_import_plan_rejects_directory_output_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = _sample_archive(root)
            package_root = root / "telegram-package"
            write_telegram_staging_package(archive=archive, output_root=package_root)
            (package_root / "import-plan.json").mkdir()

            with self.assertRaisesRegex(ValueError, "import plan output path must be a file"):
                write_telegram_import_plan(
                    archive=archive,
                    package_root=package_root,
                    destination_map={"room/1": "@general_archive"},
                )

    def test_import_plan_requires_destination_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = _sample_archive(root)
            package_root = root / "telegram-package"
            write_telegram_staging_package(archive=archive, output_root=package_root)

            result = write_telegram_import_plan(archive=archive, package_root=package_root)

            self.assertFalse(result.ready)
            self.assertIn("Missing Telegram destination peer", "\n".join(result.issues))

    def test_import_plan_requires_local_media_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = Archive(root / "archive")
            archive.initialize(source_kind="webex", target_kind="telegram", name="demo")
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
                        attachments=(Attachment(source_id="file-1", filename="notes.txt"),),
                    )
                ],
            )
            package_root = root / "telegram-package"
            write_telegram_staging_package(archive=archive, output_root=package_root)

            result = write_telegram_import_plan(
                archive=archive,
                package_root=package_root,
                destination_map={"room/1": "@general_archive"},
            )

            self.assertFalse(result.ready)
            self.assertIn("missing local_path", "\n".join(result.issues))

    def test_import_plan_requires_existing_local_media_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = Archive(root / "archive")
            archive.initialize(source_kind="webex", target_kind="telegram", name="demo")
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
                                local_path="attachments/missing.txt",
                            ),
                        ),
                    )
                ],
            )
            package_root = root / "telegram-package"
            write_telegram_staging_package(archive=archive, output_root=package_root)

            result = write_telegram_import_plan(
                archive=archive,
                package_root=package_root,
                destination_map={"room/1": "@general_archive"},
            )

            self.assertFalse(result.ready)
            self.assertIn("does not exist", "\n".join(result.issues))

    def test_import_plan_rejects_local_media_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = Archive(root / "archive")
            archive.initialize(source_kind="webex", target_kind="telegram", name="demo")
            (archive.root / "attachments" / "dir").mkdir(parents=True)
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
                                local_path="attachments/dir",
                            ),
                        ),
                    )
                ],
            )
            package_root = root / "telegram-package"
            write_telegram_staging_package(archive=archive, output_root=package_root)

            result = write_telegram_import_plan(
                archive=archive,
                package_root=package_root,
                destination_map={"room/1": "@general_archive"},
            )

            self.assertFalse(result.ready)
            self.assertIn("local_path is not a file", "\n".join(result.issues))

    def test_import_plan_rejects_local_media_size_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = Archive(root / "archive")
            archive.initialize(source_kind="webex", target_kind="telegram", name="demo")
            attachment_path = archive.root / "attachments" / "notes.txt"
            attachment_path.parent.mkdir(parents=True, exist_ok=True)
            attachment_path.write_text("attachment body", encoding="utf-8")
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
                                size_bytes=999,
                                local_path="attachments/notes.txt",
                            ),
                        ),
                    )
                ],
            )
            package_root = root / "telegram-package"
            write_telegram_staging_package(archive=archive, output_root=package_root)

            result = write_telegram_import_plan(
                archive=archive,
                package_root=package_root,
                destination_map={"room/1": "@general_archive"},
            )

            self.assertFalse(result.ready)
            self.assertIn("Attachment size mismatch", "\n".join(result.issues))

    def test_import_plan_rejects_local_media_sha256_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = Archive(root / "archive")
            archive.initialize(source_kind="webex", target_kind="telegram", name="demo")
            attachment_path = archive.root / "attachments" / "notes.txt"
            attachment_path.parent.mkdir(parents=True, exist_ok=True)
            attachment_path.write_text("attachment body", encoding="utf-8")
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
                                size_bytes=len("attachment body".encode()),
                                sha256=sha256(b"different body").hexdigest(),
                                local_path="attachments/notes.txt",
                            ),
                        ),
                    )
                ],
            )
            package_root = root / "telegram-package"
            write_telegram_staging_package(archive=archive, output_root=package_root)

            result = write_telegram_import_plan(
                archive=archive,
                package_root=package_root,
                destination_map={"room/1": "@general_archive"},
            )

            self.assertFalse(result.ready)
            self.assertIn("Attachment sha256 mismatch", "\n".join(result.issues))

    def test_import_plan_rejects_unsafe_local_media_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = Archive(root / "archive")
            archive.initialize(source_kind="webex", target_kind="telegram", name="demo")
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
                                local_path="../outside.txt",
                            ),
                        ),
                    )
                ],
            )
            package_root = root / "telegram-package"
            write_telegram_staging_package(archive=archive, output_root=package_root)

            result = write_telegram_import_plan(
                archive=archive,
                package_root=package_root,
                destination_map={"room/1": "@general_archive"},
            )

            self.assertFalse(result.ready)
            self.assertIn("local_path is unsafe", "\n".join(result.issues))

    def test_import_plan_rejects_unknown_destination_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = _sample_archive(root)
            package_root = root / "telegram-package"
            write_telegram_staging_package(archive=archive, output_root=package_root)

            result = write_telegram_import_plan(
                archive=archive,
                package_root=package_root,
                destination_map={"room/1": "@general_archive", "unknown": "@other"},
            )

            self.assertFalse(result.ready)
            self.assertIn("unknown conversation", "\n".join(result.issues))

    def test_import_plan_rejects_malformed_destination_mapping_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = _sample_archive(root)
            package_root = root / "telegram-package"
            write_telegram_staging_package(archive=archive, output_root=package_root)

            result = write_telegram_import_plan(
                archive=archive,
                package_root=package_root,
                destination_map={"room/1": 123, "": "@other"},  # type: ignore[arg-type]
            )
            plan = json.loads(result.plan_path.read_text(encoding="utf-8"))

            self.assertFalse(result.ready)
            self.assertIn("non-empty Telegram peer", "\n".join(result.issues))
            self.assertIn("non-string or empty conversation id", "\n".join(result.issues))
            self.assertIsNone(plan["operations"][1]["peer"])

    def test_import_plan_rejects_duplicate_destination_conversation_ids_after_trim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = _sample_archive(root)
            package_root = root / "telegram-package"
            write_telegram_staging_package(archive=archive, output_root=package_root)

            result = write_telegram_import_plan(
                archive=archive,
                package_root=package_root,
                destination_map={"room/1": "@general_archive", " room/1 ": "@other_archive"},
            )
            plan = json.loads(result.plan_path.read_text(encoding="utf-8"))

            self.assertFalse(result.ready)
            self.assertIn("duplicates conversation id: room/1", "\n".join(result.issues))
            self.assertEqual(plan["operations"][1]["peer"], "@general_archive")

    def test_import_plan_rejects_non_object_destination_map_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = _sample_archive(root)
            package_root = root / "telegram-package"
            write_telegram_staging_package(archive=archive, output_root=package_root)

            result = write_telegram_import_plan(
                archive=archive,
                package_root=package_root,
                destination_map=["not", "a", "map"],
            )
            plan = json.loads(result.plan_path.read_text(encoding="utf-8"))

            self.assertFalse(result.ready)
            self.assertIn("Destination map must be an object", "\n".join(result.issues))
            self.assertIsNone(plan["operations"][1]["peer"])

    def test_import_plan_rejects_duplicate_destination_peers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = Archive(root / "archive")
            archive.initialize(source_kind="webex", target_kind="telegram", name="demo")
            archive.write_conversations(
                [
                    Conversation(source_id="room/1", kind=ConversationKind.SPACE, title="General"),
                    Conversation(source_id="room/2", kind=ConversationKind.SPACE, title="Random"),
                ]
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
                    )
                ],
            )
            archive.write_messages(
                "room/2",
                [
                    Message(
                        source_id="msg-2",
                        conversation_id="room/2",
                        author_id="user-1",
                        created_at=datetime(2026, 1, 1, 12, 1, tzinfo=timezone.utc),
                        text="world",
                    )
                ],
            )
            package_root = root / "telegram-package"
            write_telegram_staging_package(archive=archive, output_root=package_root)

            result = write_telegram_import_plan(
                archive=archive,
                package_root=package_root,
                destination_map={"room/1": "@Archive", "room/2": "@archive"},
            )

            self.assertFalse(result.ready)
            self.assertIn("multiple conversations", "\n".join(result.issues))

    def test_import_plan_includes_verification_issues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = _sample_archive(root)
            package_root = root / "telegram-package"
            write_telegram_staging_package(archive=archive, output_root=package_root)
            (package_root / "manifest.json").write_text("{}", encoding="utf-8")

            result = write_telegram_import_plan(
                archive=archive,
                package_root=package_root,
                destination_map={"room/1": "@general_archive"},
            )

            self.assertFalse(result.ready)
            self.assertIn("unsupported format", "\n".join(result.issues))

    def test_refuses_missing_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            with self.assertRaises(FileNotFoundError):
                write_telegram_staging_package(
                    archive=Archive(root / "missing"),
                    output_root=root / "telegram-package",
                )

    def test_refuses_empty_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = Archive(root / "archive")
            archive.initialize(source_kind="webex", target_kind="telegram", name="demo")

            with self.assertRaisesRegex(ValueError, "no conversations"):
                write_telegram_staging_package(
                    archive=archive,
                    output_root=root / "telegram-package",
                )


if __name__ == "__main__":
    unittest.main()
