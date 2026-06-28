from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from exodus_agent.archive import Archive
from exodus_agent.model import Attachment, Conversation, ConversationKind, ConversationMembership, Message, Participant
from exodus_agent.targets.teams_mapping import (
    CompletedTeamsConversationMapping,
    EntraUser,
    TeamsTargetKind,
    build_teams_identity_prefill_from_entra,
    build_teams_conversation_mappings,
    load_entra_users,
    load_teams_conversation_map,
    load_teams_identity_map,
    prepare_teams_import_messages,
    write_teams_identity_map_template,
    write_teams_import_plan,
    write_teams_mapping_template,
    _ordered_messages_for_import,
)


class TeamsMappingTests(unittest.TestCase):
    def test_writes_identity_map_template(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = _archive_with_conversation(
                root,
                conversation=Conversation(
                    source_id="space-1",
                    kind=ConversationKind.SPACE,
                    title="Project",
                ),
                participant_ids=("u2", "u1"),
            )
            output = root / "identity-map.json"

            result = write_teams_identity_map_template(
                archive=archive,
                output_path=output,
                existing_identity_map={"u1": " entra-u1 "},
            )
            payload = json.loads(output.read_text(encoding="utf-8"))

            self.assertEqual(result.identities, 2)
            self.assertEqual(payload["format"], "exodus.teams.identity_map.v1")
            self.assertEqual(payload["identities"][0]["source_user_id"], "u1")
            self.assertEqual(payload["identities"][0]["entra_user_id"], "entra-u1")
            self.assertEqual(payload["identities"][0]["status"], "mapped")
            self.assertEqual(payload["identities"][1]["status"], "needs_review")

    def test_writes_identity_map_template_with_entra_prefill_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = _archive_with_conversation(
                root,
                conversation=Conversation(
                    source_id="space-1",
                    kind=ConversationKind.SPACE,
                    title="Project",
                ),
                participant_ids=("u1",),
            )
            output = root / "identity-map.json"

            write_teams_identity_map_template(
                archive=archive,
                output_path=output,
                existing_identity_map={"u1": "entra-u1"},
                identity_map_reasons={"u1": "Exact Webex email matched Entra mail."},
            )
            payload = json.loads(output.read_text(encoding="utf-8"))

            self.assertEqual(payload["identities"][0]["entra_user_id"], "entra-u1")
            self.assertEqual(payload["identities"][0]["reason"], "Exact Webex email matched Entra mail.")

    def test_identity_map_template_rejects_directory_output_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = _archive_with_conversation(
                root,
                conversation=Conversation(
                    source_id="space-1",
                    kind=ConversationKind.SPACE,
                    title="Project",
                ),
                participant_ids=("u1",),
            )
            output = root / "identity-map.json"
            output.mkdir()

            with self.assertRaisesRegex(ValueError, "output path must be a file"):
                write_teams_identity_map_template(
                    archive=archive,
                    output_path=output,
                    overwrite=True,
                )

    def test_loads_entra_users_from_graph_json_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "entra-users.json"
            path.write_text(
                json.dumps(
                    {
                        "value": [
                            {
                                "id": "entra-u1",
                                "mail": "Ada@Example.com",
                                "userPrincipalName": "ada@tenant.example",
                                "proxyAddresses": ["SMTP:ada.alias@example.com"],
                                "otherMails": ["ada.other@example.com"],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            users = load_entra_users(path)

            self.assertEqual(users[0].id, "entra-u1")
            self.assertEqual(users[0].mail, "Ada@Example.com")
            self.assertEqual(users[0].proxy_addresses, ("SMTP:ada.alias@example.com",))

    def test_entra_users_loader_rejects_invalid_json_with_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "entra-users.json"
            path.write_text("{not-json}", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, r"Entra users JSON is not valid JSON: .*entra-users.json"):
                load_entra_users(path)

    def test_entra_users_loader_rejects_json_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "entra-users.json"
            path.mkdir()

            with self.assertRaisesRegex(ValueError, r"Entra users JSON must be a file: .*entra-users.json"):
                load_entra_users(path)

    def test_loads_entra_users_from_csv_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "entra-users.csv"
            path.write_text(
                "id,mail,userPrincipalName,proxyAddresses\n"
                "entra-u1,ada@example.com,ada@tenant.example,SMTP:ada.alias@example.com\n",
                encoding="utf-8",
            )

            users = load_entra_users(path)

            self.assertEqual(users[0].id, "entra-u1")
            self.assertEqual(users[0].user_principal_name, "ada@tenant.example")
            self.assertEqual(users[0].proxy_addresses, ("SMTP:ada.alias@example.com",))

    def test_entra_users_csv_rejects_missing_id_column(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "entra-users.csv"
            path.write_text("mail,userPrincipalName\nada@example.com,ada@tenant.example\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "id or user_id column"):
                load_entra_users(path)

    def test_entra_users_csv_rejects_empty_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "entra-users.csv"
            path.write_text("", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "id or user_id column"):
                load_entra_users(path)

    def test_entra_users_csv_rejects_non_utf8_with_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "entra-users.csv"
            path.write_bytes(b"\xff")

            with self.assertRaisesRegex(ValueError, r"Entra users CSV is not valid UTF-8: .*entra-users.csv"):
                load_entra_users(path)

    def test_entra_users_csv_rejects_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "entra-users.csv"
            path.mkdir()

            with self.assertRaisesRegex(ValueError, r"Entra users CSV must be a file: .*entra-users.csv"):
                load_entra_users(path)

    def test_entra_prefill_matches_exact_email_with_priority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = _archive_with_participants(
                Path(tmp),
                [
                    Participant(source_id="u1", display_name="Ada", email="ada@example.com"),
                    Participant(source_id="u2", display_name="Grace", email="grace@tenant.example"),
                    Participant(source_id="u3", display_name="Katherine", email="kat.alias@example.com"),
                ],
            )

            identity_map, reasons = build_teams_identity_prefill_from_entra(
                archive=archive,
                entra_users=(
                    EntraUser(
                        id="entra-u1",
                        mail="ADA@example.com",
                        user_principal_name="other@example.com",
                    ),
                    EntraUser(
                        id="entra-u2",
                        user_principal_name="grace@tenant.example",
                    ),
                    EntraUser(
                        id="entra-u3",
                        proxy_addresses=("smtp:kat.alias@example.com",),
                    ),
                ),
            )

            self.assertEqual(identity_map, {"u1": "entra-u1", "u2": "entra-u2", "u3": "entra-u3"})
            self.assertEqual(reasons["u1"], "Exact Webex email matched Entra mail.")
            self.assertEqual(reasons["u2"], "Exact Webex email matched Entra userPrincipalName.")
            self.assertEqual(reasons["u3"], "Exact Webex email matched Entra proxyAddress/otherMail.")

    def test_entra_prefill_rejects_ambiguous_exact_email_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = _archive_with_participants(
                Path(tmp),
                [Participant(source_id="u1", display_name="Ada", email="ada@example.com")],
            )

            identity_map, reasons = build_teams_identity_prefill_from_entra(
                archive=archive,
                entra_users=(
                    EntraUser(id="entra-u1", mail="ada@example.com"),
                    EntraUser(id="entra-u2", mail="ADA@example.com"),
                ),
            )

            self.assertEqual(identity_map, {})
            self.assertIn("multiple Entra mail", reasons["u1"])

    def test_entra_prefill_never_matches_display_name_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = _archive_with_participants(
                Path(tmp),
                [Participant(source_id="u1", display_name="Ada Lovelace", email=None)],
            )

            identity_map, reasons = build_teams_identity_prefill_from_entra(
                archive=archive,
                entra_users=(EntraUser(id="entra-u1", mail="ada@example.com"),),
            )

            self.assertEqual(identity_map, {})
            self.assertEqual(reasons, {})

    def test_identity_map_template_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = _archive_with_conversation(
                root,
                conversation=Conversation(
                    source_id="space-1",
                    kind=ConversationKind.SPACE,
                    title="Project",
                ),
                participant_ids=("u1", "u2"),
            )
            output = root / "identity-map.json"
            output.write_text("{}\n", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                write_teams_identity_map_template(archive=archive, output_path=output)

    def test_loads_completed_identity_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "identity-map.json"
            path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.identity_map.v1",
                        "identities": [
                            {"source_user_id": "u1", "entra_user_id": " entra-u1 "},
                            {"source_user_id": "u2", "entra_user_id": "entra-u2"},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(load_teams_identity_map(path), {"u1": "entra-u1", "u2": "entra-u2"})

    def test_identity_map_loader_trims_source_user_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "identity-map.json"
            path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.identity_map.v1",
                        "identities": [
                            {"source_user_id": " u1 ", "entra_user_id": "entra-u1"},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(load_teams_identity_map(path), {"u1": "entra-u1"})

    def test_identity_map_loader_rejects_invalid_json_with_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "identity-map.json"
            path.write_text("{not-json}", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, r"Teams identity map is not valid JSON: .*identity-map.json"):
                load_teams_identity_map(path)

    def test_identity_map_loader_rejects_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "identity-map.json"
            path.mkdir()

            with self.assertRaisesRegex(ValueError, r"Teams identity map must be a file: .*identity-map.json"):
                load_teams_identity_map(path)

    def test_identity_map_loader_rejects_incomplete_rows(self) -> None:
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

            with self.assertRaisesRegex(ValueError, "entra_user_id"):
                load_teams_identity_map(path)

    def test_identity_map_loader_rejects_duplicate_source_users(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "identity-map.json"
            path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.identity_map.v1",
                        "identities": [
                            {"source_user_id": "u1", "entra_user_id": "entra-u1"},
                            {"source_user_id": "u1", "entra_user_id": "entra-u1b"},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "duplicates"):
                load_teams_identity_map(path)

    def test_identity_map_loader_rejects_duplicate_source_users_after_trim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "identity-map.json"
            path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.identity_map.v1",
                        "identities": [
                            {"source_user_id": " u1 ", "entra_user_id": "entra-u1"},
                            {"source_user_id": "u1", "entra_user_id": "entra-u1b"},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "duplicates"):
                load_teams_identity_map(path)

    def test_identity_map_loader_rejects_duplicate_entra_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "identity-map.json"
            path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.identity_map.v1",
                        "identities": [
                            {"source_user_id": "u1", "entra_user_id": " entra-u1 "},
                            {"source_user_id": "u2", "entra_user_id": "entra-u1"},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "multiple source_user_id"):
                load_teams_identity_map(path)

    def test_identity_map_loader_rejects_duplicate_entra_targets_case_insensitively(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "identity-map.json"
            path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.identity_map.v1",
                        "identities": [
                            {"source_user_id": "u1", "entra_user_id": "ENTRA-U1"},
                            {"source_user_id": "u2", "entra_user_id": "entra-u1"},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "multiple source_user_id"):
                load_teams_identity_map(path)

    def test_maps_direct_webex_conversation_to_one_on_one_chat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = _archive_with_conversation(
                Path(tmp),
                conversation=Conversation(
                    source_id="direct-1",
                    kind=ConversationKind.DIRECT,
                    title="Ada / Grace",
                ),
                participant_ids=("ada", "grace"),
            )

            mappings = build_teams_conversation_mappings(
                archive=archive,
                identity_map={"ada": "entra-ada", "grace": "entra-grace"},
            )

            self.assertEqual(mappings[0].target_kind, TeamsTargetKind.ONE_ON_ONE_CHAT)
            self.assertEqual(mappings[0].confidence, "high")
            self.assertEqual(mappings[0].target_user_ids, ("entra-ada", "entra-grace"))

    def test_maps_small_webex_space_to_group_chat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = _archive_with_conversation(
                Path(tmp),
                conversation=Conversation(
                    source_id="space-1",
                    kind=ConversationKind.SPACE,
                    title="Project",
                ),
                participant_ids=("u1", "u2", "u3"),
            )

            mappings = build_teams_conversation_mappings(
                archive=archive,
                identity_map={"u1": "e1", "u2": "e2", "u3": "e3"},
            )

            self.assertEqual(mappings[0].target_kind, TeamsTargetKind.GROUP_CHAT)
            self.assertEqual(mappings[0].confidence, "medium")

    def test_uses_memberships_instead_of_only_message_authors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = Archive(root / "archive")
            archive.initialize(source_kind="webex", target_kind="teams", name="demo")
            archive.write_conversations(
                [Conversation(source_id="direct-1", kind=ConversationKind.DIRECT, title="Ada / Grace")]
            )
            archive.write_participants(
                [
                    Participant(source_id="ada", display_name="Ada"),
                    Participant(source_id="grace", display_name="Grace"),
                ]
            )
            archive.write_memberships(
                [
                    ConversationMembership(
                        source_id="membership-1",
                        conversation_id="direct-1",
                        participant_id="ada",
                    ),
                    ConversationMembership(
                        source_id="membership-2",
                        conversation_id="direct-1",
                        participant_id="grace",
                    ),
                ]
            )
            archive.write_messages(
                "direct-1",
                [
                    Message(
                        source_id="msg-1",
                        conversation_id="direct-1",
                        author_id="ada",
                        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                        text="hello",
                    )
                ],
            )

            mappings = build_teams_conversation_mappings(
                archive=archive,
                identity_map={"ada": "entra-ada", "grace": "entra-grace"},
            )

            self.assertEqual(mappings[0].target_kind, TeamsTargetKind.ONE_ON_ONE_CHAT)
            self.assertEqual(mappings[0].participant_source_ids, ("ada", "grace"))

    def test_requires_review_for_direct_conversation_with_more_than_two_authors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = _archive_with_conversation(
                Path(tmp),
                conversation=Conversation(
                    source_id="direct-1",
                    kind=ConversationKind.DIRECT,
                    title="Unexpected direct",
                ),
                participant_ids=("u1", "u2", "u3"),
            )

            mappings = build_teams_conversation_mappings(
                archive=archive,
                identity_map={"u1": "e1", "u2": "e2", "u3": "e3"},
            )

            self.assertEqual(mappings[0].target_kind, TeamsTargetKind.REVIEW_REQUIRED)

    def test_maps_large_webex_space_to_team_channel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            participant_ids = tuple(f"u{index}" for index in range(10))
            archive = _archive_with_conversation(
                Path(tmp),
                conversation=Conversation(
                    source_id="space-1",
                    kind=ConversationKind.SPACE,
                    title="Department",
                ),
                participant_ids=participant_ids,
            )

            mappings = build_teams_conversation_mappings(
                archive=archive,
                identity_map={source_id: f"e-{source_id}" for source_id in participant_ids},
            )

            self.assertEqual(mappings[0].target_kind, TeamsTargetKind.TEAM_CHANNEL)

    def test_requires_review_when_identity_mapping_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = _archive_with_conversation(
                Path(tmp),
                conversation=Conversation(
                    source_id="direct-1",
                    kind=ConversationKind.DIRECT,
                    title="Ada / Grace",
                ),
                participant_ids=("ada", "grace"),
            )

            mappings = build_teams_conversation_mappings(
                archive=archive,
                identity_map={"ada": "entra-ada"},
            )

            self.assertEqual(mappings[0].target_kind, TeamsTargetKind.REVIEW_REQUIRED)
            self.assertEqual(mappings[0].missing_identity_count, 1)

    def test_writes_mapping_template_and_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = _archive_with_conversation(
                root,
                conversation=Conversation(
                    source_id="space-1",
                    kind=ConversationKind.SPACE,
                    title="Project",
                ),
                participant_ids=("u1", "u2"),
            )
            output = root / "teams-map.json"

            result = write_teams_mapping_template(
                archive=archive,
                identity_map={"u1": "e1", "u2": "e2"},
                output_path=output,
            )
            payload = json.loads(output.read_text(encoding="utf-8"))

            self.assertEqual(result.conversations, 1)
            self.assertEqual(result.review_required, 0)
            self.assertEqual(payload["format"], "exodus.teams.mapping_template.v1")
            self.assertEqual(payload["conversations"][0]["target_kind"], "group_chat")
            self.assertEqual(payload["conversations"][0]["target"], {"chat_id": ""})
            with self.assertRaises(FileExistsError):
                write_teams_mapping_template(
                    archive=archive,
                    identity_map={"u1": "e1", "u2": "e2"},
                    output_path=output,
                )

    def test_mapping_template_rejects_directory_output_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = _archive_with_conversation(
                root,
                conversation=Conversation(
                    source_id="space-1",
                    kind=ConversationKind.SPACE,
                    title="Project",
                ),
                participant_ids=("u1", "u2"),
            )
            output = root / "teams-map.json"
            output.mkdir()

            with self.assertRaisesRegex(ValueError, "output path must be a file"):
                write_teams_mapping_template(
                    archive=archive,
                    identity_map={"u1": "e1", "u2": "e2"},
                    output_path=output,
                    overwrite=True,
                )

    def test_loads_completed_conversation_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "teams-map.json"
            path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.mapping_template.v1",
                        "conversations": [
                            {
                                "source_conversation_id": "direct-1",
                                "target_kind": "one_on_one_chat",
                                "target": {"chat_id": " chat-1 "},
                            },
                            {
                                "source_conversation_id": "space-1",
                                "target_kind": "team_channel",
                                "target": {"team_id": "team-1", "channel_id": "channel-1"},
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            mappings = load_teams_conversation_map(path)

            self.assertEqual(len(mappings), 2)
            self.assertEqual(mappings[0].source_conversation_id, "direct-1")
            self.assertEqual(mappings[0].target_kind, TeamsTargetKind.ONE_ON_ONE_CHAT)
            self.assertEqual(mappings[0].target["chat_id"], "chat-1")
            self.assertEqual(mappings[1].target["team_id"], "team-1")

    def test_conversation_map_loader_rejects_invalid_json_with_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "teams-map.json"
            path.write_text("{not-json}", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, r"Teams conversation map is not valid JSON: .*teams-map.json"):
                load_teams_conversation_map(path)

    def test_conversation_map_loader_rejects_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "teams-map.json"
            path.mkdir()

            with self.assertRaisesRegex(ValueError, r"Teams conversation map must be a file: .*teams-map.json"):
                load_teams_conversation_map(path)

    def test_conversation_map_loader_rejects_missing_chat_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "teams-map.json"
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

            with self.assertRaisesRegex(ValueError, "target.chat_id"):
                load_teams_conversation_map(path)

    def test_conversation_map_loader_rejects_missing_channel_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "teams-map.json"
            path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.mapping_template.v1",
                        "conversations": [
                            {
                                "source_conversation_id": "space-1",
                                "target_kind": "team_channel",
                                "target": {"team_id": "team-1", "channel_id": ""},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "target.channel_id"):
                load_teams_conversation_map(path)

    def test_conversation_map_loader_rejects_extra_chat_target_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "teams-map.json"
            path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.mapping_template.v1",
                        "conversations": [
                            {
                                "source_conversation_id": "space-1",
                                "target_kind": "group_chat",
                                "target": {"chat_id": "chat-1", "channel_id": "channel-1"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "unsupported fields"):
                load_teams_conversation_map(path)

    def test_conversation_map_loader_rejects_extra_channel_target_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "teams-map.json"
            path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.mapping_template.v1",
                        "conversations": [
                            {
                                "source_conversation_id": "space-1",
                                "target_kind": "team_channel",
                                "target": {
                                    "team_id": "team-1",
                                    "channel_id": "channel-1",
                                    "channelId": "channel-typo",
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "channelId"):
                load_teams_conversation_map(path)

    def test_conversation_map_loader_rejects_review_required_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "teams-map.json"
            path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.mapping_template.v1",
                        "conversations": [
                            {
                                "source_conversation_id": "space-1",
                                "target_kind": "review_required",
                                "target": {"resolution": ""},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "review_required"):
                load_teams_conversation_map(path)

            mappings = load_teams_conversation_map(path, allow_review_required=True)
            self.assertEqual(mappings[0].target_kind, TeamsTargetKind.REVIEW_REQUIRED)

    def test_conversation_map_loader_rejects_duplicate_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "teams-map.json"
            path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.mapping_template.v1",
                        "conversations": [
                            {
                                "source_conversation_id": "space-1",
                                "target_kind": "group_chat",
                                "target": {"chat_id": "chat-1"},
                            },
                            {
                                "source_conversation_id": "space-1",
                                "target_kind": "group_chat",
                                "target": {"chat_id": "chat-2"},
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "duplicates source_conversation_id"):
                load_teams_conversation_map(path)

    def test_conversation_map_loader_rejects_duplicate_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "teams-map.json"
            path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.mapping_template.v1",
                        "conversations": [
                            {
                                "source_conversation_id": "space-1",
                                "target_kind": "group_chat",
                                "target": {"chat_id": "Chat-1"},
                            },
                            {
                                "source_conversation_id": "space-2",
                                "target_kind": "group_chat",
                                "target": {"chat_id": "chat-1"},
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "multiple source conversations"):
                load_teams_conversation_map(path)

    def test_prepare_teams_import_messages_preserves_unique_millisecond_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = Archive(root / "archive")
            archive.initialize(source_kind="webex", target_kind="teams", name="demo")
            archive.write_conversations(
                [Conversation(source_id="space-1", kind=ConversationKind.SPACE, title="Project")]
            )
            archive.write_messages(
                "space-1",
                [
                    Message(
                        source_id="msg-1",
                        conversation_id="space-1",
                        author_id="u1",
                        created_at=datetime(2026, 1, 1, 12, 0, 0, 123000, tzinfo=timezone.utc),
                        text="first",
                    ),
                    Message(
                        source_id="msg-2",
                        conversation_id="space-1",
                        author_id="u2",
                        created_at=datetime(2026, 1, 1, 12, 0, 1, 124000, tzinfo=timezone.utc),
                        text="second",
                    ),
                ],
            )
            path = root / "teams-map.json"
            path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.mapping_template.v1",
                        "conversations": [
                            {
                                "source_conversation_id": "space-1",
                                "target_kind": "group_chat",
                                "target": {"chat_id": "chat-1"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            messages = prepare_teams_import_messages(
                archive=archive,
                conversation_map=load_teams_conversation_map(path),
                identity_map={"u1": "entra-u1", "u2": "entra-u2"},
            )

            self.assertEqual(messages[0].created_date_time, "2026-01-01T12:00:00.123Z")
            self.assertEqual(messages[0].original_created_at, "2026-01-01T12:00:00.123000Z")
            self.assertFalse(messages[0].timestamp_adjusted)
            self.assertEqual(messages[1].created_date_time, "2026-01-01T12:00:01.124Z")

    def test_prepare_teams_import_messages_offsets_colliding_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = Archive(root / "archive")
            archive.initialize(source_kind="webex", target_kind="teams", name="demo")
            archive.write_conversations(
                [Conversation(source_id="space-1", kind=ConversationKind.SPACE, title="Project")]
            )
            archive.write_messages(
                "space-1",
                [
                    Message(
                        source_id="msg-1",
                        conversation_id="space-1",
                        author_id="u1",
                        created_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
                        text="first",
                    ),
                    Message(
                        source_id="msg-2",
                        conversation_id="space-1",
                        author_id="u2",
                        created_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
                        text="second",
                    ),
                ],
            )
            path = root / "teams-map.json"
            path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.mapping_template.v1",
                        "conversations": [
                            {
                                "source_conversation_id": "space-1",
                                "target_kind": "team_channel",
                                "target": {"team_id": "team-1", "channel_id": "channel-1"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            messages = prepare_teams_import_messages(
                archive=archive,
                conversation_map=load_teams_conversation_map(path),
                identity_map={"u1": "entra-u1", "u2": "entra-u2"},
            )

            self.assertEqual(messages[0].created_date_time, "2026-01-01T12:00:00.000Z")
            self.assertFalse(messages[0].timestamp_adjusted)
            self.assertEqual(messages[1].created_date_time, "2026-01-01T12:00:00.001Z")
            self.assertEqual(messages[1].original_created_at, "2026-01-01T12:00:00Z")
            self.assertTrue(messages[1].timestamp_adjusted)
            self.assertEqual(messages[1].timestamp_adjustment_ms, 1)
            self.assertEqual(messages[1].timestamp_adjustment_reason, "timestamp_collision")

    def test_prepare_teams_import_messages_records_millisecond_precision_adjustment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = Archive(root / "archive")
            archive.initialize(source_kind="webex", target_kind="teams", name="demo")
            archive.write_conversations(
                [Conversation(source_id="space-1", kind=ConversationKind.SPACE, title="Project")]
            )
            archive.write_messages(
                "space-1",
                [
                    Message(
                        source_id="msg-1",
                        conversation_id="space-1",
                        author_id="u1",
                        created_at=datetime(2026, 1, 1, 12, 0, 0, 123456, tzinfo=timezone.utc),
                        text="first",
                    )
                ],
            )
            path = root / "teams-map.json"
            path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.mapping_template.v1",
                        "conversations": [
                            {
                                "source_conversation_id": "space-1",
                                "target_kind": "group_chat",
                                "target": {"chat_id": "chat-1"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            messages = prepare_teams_import_messages(
                archive=archive,
                conversation_map=load_teams_conversation_map(path),
                identity_map={"u1": "entra-u1"},
            )

            self.assertEqual(messages[0].created_date_time, "2026-01-01T12:00:00.123Z")
            self.assertEqual(messages[0].original_created_at, "2026-01-01T12:00:00.123456Z")
            self.assertTrue(messages[0].timestamp_adjusted)
            self.assertEqual(messages[0].timestamp_adjustment_ms, 0)
            self.assertEqual(messages[0].timestamp_adjustment_reason, "millisecond_precision")

    def test_prepare_teams_import_messages_rejects_future_source_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = Archive(root / "archive")
            archive.initialize(source_kind="webex", target_kind="teams", name="demo")
            archive.write_conversations(
                [Conversation(source_id="space-1", kind=ConversationKind.SPACE, title="Project")]
            )
            archive.write_messages(
                "space-1",
                [
                    Message(
                        source_id="msg-1",
                        conversation_id="space-1",
                        author_id="u1",
                        created_at=datetime(2026, 1, 1, 12, 0, 1, tzinfo=timezone.utc),
                        text="future",
                    )
                ],
            )
            conversation_map = (
                CompletedTeamsConversationMapping(
                    source_conversation_id="space-1",
                    target_kind=TeamsTargetKind.GROUP_CHAT,
                    target={"chat_id": "chat-1"},
                ),
            )

            with self.assertRaisesRegex(ValueError, "future"):
                prepare_teams_import_messages(
                    archive=archive,
                    conversation_map=conversation_map,
                    identity_map={"u1": "entra-u1"},
                    import_time=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
                )

    def test_prepare_teams_import_messages_caps_collision_adjustment_at_cutoff(self) -> None:
        # Both messages share a timestamp exactly equal to import_cutoff.  The second
        # message's collision bump would overshoot the cutoff by 1 ms; the fix caps it
        # back to import_cutoff and records "cutoff_capped" in the audit reason instead
        # of aborting with a ValueError.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = Archive(root / "archive")
            archive.initialize(source_kind="webex", target_kind="teams", name="demo")
            archive.write_conversations(
                [Conversation(source_id="space-1", kind=ConversationKind.SPACE, title="Project")]
            )
            archive.write_messages(
                "space-1",
                [
                    Message(
                        source_id="msg-1",
                        conversation_id="space-1",
                        author_id="u1",
                        created_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
                        text="first",
                    ),
                    Message(
                        source_id="msg-2",
                        conversation_id="space-1",
                        author_id="u2",
                        created_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
                        text="second",
                    ),
                ],
            )
            conversation_map = (
                CompletedTeamsConversationMapping(
                    source_conversation_id="space-1",
                    target_kind=TeamsTargetKind.GROUP_CHAT,
                    target={"chat_id": "chat-1"},
                ),
            )

            messages = prepare_teams_import_messages(
                archive=archive,
                conversation_map=conversation_map,
                identity_map={"u1": "entra-u1", "u2": "entra-u2"},
                import_time=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
            )

            self.assertEqual(len(messages), 2)
            self.assertTrue(messages[1].timestamp_adjusted)
            self.assertIn("cutoff_capped", messages[1].timestamp_adjustment_reason or "")

    def test_prepare_teams_import_messages_orders_parent_before_earlier_reply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = Archive(root / "archive")
            archive.initialize(source_kind="webex", target_kind="teams", name="demo")
            archive.write_conversations(
                [Conversation(source_id="space-1", kind=ConversationKind.SPACE, title="Project")]
            )
            archive.write_messages(
                "space-1",
                [
                    Message(
                        source_id="reply-1",
                        conversation_id="space-1",
                        author_id="u2",
                        created_at=datetime(2026, 1, 1, 11, 59, tzinfo=timezone.utc),
                        parent_id="parent-1",
                        text="reply",
                    ),
                    Message(
                        source_id="parent-1",
                        conversation_id="space-1",
                        author_id="u1",
                        created_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
                        text="parent",
                    ),
                ],
            )
            conversation_map = (
                CompletedTeamsConversationMapping(
                    source_conversation_id="space-1",
                    target_kind=TeamsTargetKind.GROUP_CHAT,
                    target={"chat_id": "chat-1"},
                ),
            )

            messages = prepare_teams_import_messages(
                archive=archive,
                conversation_map=conversation_map,
                identity_map={"u1": "entra-u1", "u2": "entra-u2"},
            )

            self.assertEqual([message.source_message_id for message in messages], ["parent-1", "reply-1"])
            self.assertEqual([message.import_order for message in messages], [0, 1])
            self.assertEqual(messages[1].parent_source_message_id, "parent-1")
            self.assertEqual(messages[0].created_date_time, "2026-01-01T12:00:00.000Z")
            self.assertEqual(messages[1].created_date_time, "2026-01-01T12:00:00.001Z")
            self.assertEqual(messages[1].original_created_at, "2026-01-01T11:59:00Z")
            self.assertTrue(messages[1].timestamp_adjusted)
            self.assertEqual(messages[1].timestamp_adjustment_ms, 60001)
            self.assertEqual(messages[1].timestamp_adjustment_reason, "timestamp_collision")

    def test_prepare_teams_import_messages_rejects_missing_reply_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = Archive(root / "archive")
            archive.initialize(source_kind="webex", target_kind="teams", name="demo")
            archive.write_conversations(
                [Conversation(source_id="space-1", kind=ConversationKind.SPACE, title="Project")]
            )
            archive.write_messages(
                "space-1",
                [
                    Message(
                        source_id="reply-1",
                        conversation_id="space-1",
                        author_id="u1",
                        created_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
                        parent_id="missing-parent",
                        text="reply",
                    )
                ],
            )
            conversation_map = (
                CompletedTeamsConversationMapping(
                    source_conversation_id="space-1",
                    target_kind=TeamsTargetKind.GROUP_CHAT,
                    target={"chat_id": "chat-1"},
                ),
            )

            with self.assertRaisesRegex(ValueError, "missing parent"):
                prepare_teams_import_messages(
                    archive=archive,
                    conversation_map=conversation_map,
                    identity_map={"u1": "entra-u1"},
                )

    def test_prepare_teams_import_messages_rejects_duplicate_message_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = Archive(root / "archive")
            archive.initialize(source_kind="webex", target_kind="teams", name="demo")
            archive.write_conversations(
                [Conversation(source_id="space-1", kind=ConversationKind.SPACE, title="Project")]
            )
            archive.write_messages(
                "space-1",
                [
                    Message(
                        source_id="msg-1",
                        conversation_id="space-1",
                        author_id="u1",
                        created_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
                        text="first",
                    ),
                    Message(
                        source_id="msg-1",
                        conversation_id="space-1",
                        author_id="u1",
                        created_at=datetime(2026, 1, 1, 12, 1, tzinfo=timezone.utc),
                        text="duplicate",
                    ),
                ],
            )
            conversation_map = (
                CompletedTeamsConversationMapping(
                    source_conversation_id="space-1",
                    target_kind=TeamsTargetKind.GROUP_CHAT,
                    target={"chat_id": "chat-1"},
                ),
            )

            with self.assertRaisesRegex(ValueError, "duplicates message source_id"):
                prepare_teams_import_messages(
                    archive=archive,
                    conversation_map=conversation_map,
                    identity_map={"u1": "entra-u1"},
                )

    def test_prepare_teams_import_messages_rejects_duplicate_message_ids_across_conversations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = Archive(root / "archive")
            archive.initialize(source_kind="webex", target_kind="teams", name="demo")
            archive.write_conversations(
                [
                    Conversation(source_id="space-1", kind=ConversationKind.SPACE, title="One"),
                    Conversation(source_id="space-2", kind=ConversationKind.SPACE, title="Two"),
                ]
            )
            for conversation_id in ("space-1", "space-2"):
                archive.write_messages(
                    conversation_id,
                    [
                        Message(
                            source_id="msg-1",
                            conversation_id=conversation_id,
                            author_id="u1",
                            created_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
                            text=f"message in {conversation_id}",
                        )
                    ],
                )
            conversation_map = (
                CompletedTeamsConversationMapping(
                    source_conversation_id="space-1",
                    target_kind=TeamsTargetKind.GROUP_CHAT,
                    target={"chat_id": "chat-1"},
                ),
                CompletedTeamsConversationMapping(
                    source_conversation_id="space-2",
                    target_kind=TeamsTargetKind.GROUP_CHAT,
                    target={"chat_id": "chat-2"},
                ),
            )

            with self.assertRaisesRegex(ValueError, "duplicates source_message_id"):
                prepare_teams_import_messages(
                    archive=archive,
                    conversation_map=conversation_map,
                    identity_map={"u1": "entra-u1"},
                )

    def test_prepare_teams_import_messages_rejects_parent_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = Archive(root / "archive")
            archive.initialize(source_kind="webex", target_kind="teams", name="demo")
            archive.write_conversations(
                [Conversation(source_id="space-1", kind=ConversationKind.SPACE, title="Project")]
            )
            archive.write_messages(
                "space-1",
                [
                    Message(
                        source_id="msg-1",
                        conversation_id="space-1",
                        author_id="u1",
                        created_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
                        parent_id="msg-2",
                        text="first",
                    ),
                    Message(
                        source_id="msg-2",
                        conversation_id="space-1",
                        author_id="u1",
                        created_at=datetime(2026, 1, 1, 12, 1, tzinfo=timezone.utc),
                        parent_id="msg-1",
                        text="second",
                    ),
                ],
            )
            conversation_map = (
                CompletedTeamsConversationMapping(
                    source_conversation_id="space-1",
                    target_kind=TeamsTargetKind.GROUP_CHAT,
                    target={"chat_id": "chat-1"},
                ),
            )

            with self.assertRaisesRegex(ValueError, "parent cycle"):
                prepare_teams_import_messages(
                    archive=archive,
                    conversation_map=conversation_map,
                    identity_map={"u1": "entra-u1"},
                )

    def test_prepare_teams_import_messages_rejects_unmapped_authors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = _archive_with_conversation(
                Path(tmp),
                conversation=Conversation(
                    source_id="space-1",
                    kind=ConversationKind.SPACE,
                    title="Project",
                ),
                participant_ids=("u1", "u2"),
            )
            path = Path(tmp) / "teams-map.json"
            path.write_text(
                json.dumps(
                    {
                        "format": "exodus.teams.mapping_template.v1",
                        "conversations": [
                            {
                                "source_conversation_id": "space-1",
                                "target_kind": "group_chat",
                                "target": {"chat_id": "chat-1"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "not mapped to Entra ID"):
                prepare_teams_import_messages(
                    archive=archive,
                    conversation_map=load_teams_conversation_map(path),
                    identity_map={"u1": "entra-u1"},
                )

    def test_prepare_teams_import_messages_reports_attachments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = Archive(root / "archive")
            archive.initialize(source_kind="webex", target_kind="teams", name="demo")
            archive.write_conversations(
                [Conversation(source_id="space-1", kind=ConversationKind.SPACE, title="Project")]
            )
            archive.write_messages(
                "space-1",
                [
                    Message(
                        source_id="msg-1",
                        conversation_id="space-1",
                        author_id="u1",
                        created_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
                        text="with file",
                        attachments=(
                            Attachment(
                                source_id="file-1",
                                filename="notes.txt",
                                mime_type="text/plain",
                                size_bytes=12,
                                sha256="abc",
                                local_path="attachments/notes.txt",
                            ),
                        ),
                    )
                ],
            )
            conversation_map = (
                CompletedTeamsConversationMapping(
                    source_conversation_id="space-1",
                    target_kind=TeamsTargetKind.GROUP_CHAT,
                    target={"chat_id": "chat-1"},
                ),
            )

            messages = prepare_teams_import_messages(
                archive=archive,
                conversation_map=conversation_map,
                identity_map={"u1": "entra-u1"},
            )

            self.assertEqual(len(messages[0].attachments), 1)
            self.assertEqual(messages[0].attachments[0]["source_attachment_id"], "file-1")
            self.assertEqual(messages[0].attachments[0]["filename"], "notes.txt")
            self.assertEqual(messages[0].attachments[0]["supported"], False)

    def test_prepare_teams_import_messages_rejects_duplicate_direct_mappings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = _archive_with_conversation(
                Path(tmp),
                conversation=Conversation(
                    source_id="space-1",
                    kind=ConversationKind.SPACE,
                    title="Project",
                ),
                participant_ids=("u1", "u2"),
            )
            duplicate_mappings = (
                CompletedTeamsConversationMapping(
                    source_conversation_id="space-1",
                    target_kind=TeamsTargetKind.GROUP_CHAT,
                    target={"chat_id": "chat-1"},
                ),
                CompletedTeamsConversationMapping(
                    source_conversation_id="space-1",
                    target_kind=TeamsTargetKind.GROUP_CHAT,
                    target={"chat_id": "chat-2"},
                ),
            )

            with self.assertRaisesRegex(ValueError, "duplicates source_conversation_id"):
                prepare_teams_import_messages(
                    archive=archive,
                    conversation_map=duplicate_mappings,
                    identity_map={"u1": "entra-u1", "u2": "entra-u2"},
                )

    def test_prepare_teams_import_messages_rejects_duplicate_direct_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = Archive(root / "archive")
            archive.initialize(source_kind="webex", target_kind="teams", name="demo")
            archive.write_conversations(
                [
                    Conversation(source_id="space-1", kind=ConversationKind.SPACE, title="One"),
                    Conversation(source_id="space-2", kind=ConversationKind.SPACE, title="Two"),
                ]
            )
            archive.write_messages(
                "space-1",
                [
                    Message(
                        source_id="msg-1",
                        conversation_id="space-1",
                        author_id="u1",
                        created_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
                        text="first",
                    )
                ],
            )
            archive.write_messages(
                "space-2",
                [
                    Message(
                        source_id="msg-2",
                        conversation_id="space-2",
                        author_id="u1",
                        created_at=datetime(2026, 1, 1, 12, 1, tzinfo=timezone.utc),
                        text="second",
                    )
                ],
            )
            duplicate_targets = (
                CompletedTeamsConversationMapping(
                    source_conversation_id="space-1",
                    target_kind=TeamsTargetKind.GROUP_CHAT,
                    target={"chat_id": "chat-1"},
                ),
                CompletedTeamsConversationMapping(
                    source_conversation_id="space-2",
                    target_kind=TeamsTargetKind.GROUP_CHAT,
                    target={"chat_id": "CHAT-1"},
                ),
            )

            with self.assertRaisesRegex(ValueError, "multiple source conversations"):
                prepare_teams_import_messages(
                    archive=archive,
                    conversation_map=duplicate_targets,
                    identity_map={"u1": "entra-u1"},
                )

    def test_prepare_teams_import_messages_rejects_missing_archived_conversation_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = Archive(root / "archive")
            archive.initialize(source_kind="webex", target_kind="teams", name="demo")
            archive.write_conversations(
                [
                    Conversation(source_id="space-1", kind=ConversationKind.SPACE, title="One"),
                    Conversation(source_id="space-2", kind=ConversationKind.SPACE, title="Two"),
                ]
            )
            archive.write_messages(
                "space-1",
                [
                    Message(
                        source_id="msg-1",
                        conversation_id="space-1",
                        author_id="u1",
                        created_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
                        text="first",
                    )
                ],
            )
            archive.write_messages(
                "space-2",
                [
                    Message(
                        source_id="msg-2",
                        conversation_id="space-2",
                        author_id="u1",
                        created_at=datetime(2026, 1, 1, 12, 1, tzinfo=timezone.utc),
                        text="second",
                    )
                ],
            )
            partial_map = (
                CompletedTeamsConversationMapping(
                    source_conversation_id="space-1",
                    target_kind=TeamsTargetKind.GROUP_CHAT,
                    target={"chat_id": "chat-1"},
                ),
            )

            with self.assertRaisesRegex(ValueError, "missing archived source_conversation_id"):
                prepare_teams_import_messages(
                    archive=archive,
                    conversation_map=partial_map,
                    identity_map={"u1": "entra-u1"},
                )

    def test_prepare_teams_import_messages_rejects_unknown_conversation_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = _archive_with_conversation(
                Path(tmp),
                conversation=Conversation(
                    source_id="space-1",
                    kind=ConversationKind.SPACE,
                    title="Project",
                ),
                participant_ids=("u1", "u2"),
            )
            stale_map = (
                CompletedTeamsConversationMapping(
                    source_conversation_id="space-1",
                    target_kind=TeamsTargetKind.GROUP_CHAT,
                    target={"chat_id": "chat-1"},
                ),
                CompletedTeamsConversationMapping(
                    source_conversation_id="stale-space",
                    target_kind=TeamsTargetKind.GROUP_CHAT,
                    target={"chat_id": "chat-2"},
                ),
            )

            with self.assertRaisesRegex(ValueError, "unknown source_conversation_id"):
                prepare_teams_import_messages(
                    archive=archive,
                    conversation_map=stale_map,
                    identity_map={"u1": "entra-u1", "u2": "entra-u2"},
                )

    def test_prepare_teams_import_messages_rejects_incomplete_direct_targets_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = _archive_with_conversation(
                Path(tmp),
                conversation=Conversation(
                    source_id="space-1",
                    kind=ConversationKind.SPACE,
                    title="Project",
                ),
                participant_ids=("u1", "u2"),
            )
            incomplete_target = (
                CompletedTeamsConversationMapping(
                    source_conversation_id="space-1",
                    target_kind=TeamsTargetKind.TEAM_CHANNEL,
                    target={"team_id": "team-1"},
                ),
            )

            with self.assertRaisesRegex(ValueError, "missing channel_id"):
                prepare_teams_import_messages(
                    archive=archive,
                    conversation_map=incomplete_target,
                    identity_map={"u1": "entra-u1", "u2": "entra-u2"},
                )

    def test_writes_teams_import_plan_with_timestamp_audit_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = Archive(root / "archive")
            archive.initialize(source_kind="webex", target_kind="teams", name="demo")
            archive.write_conversations(
                [Conversation(source_id="space-1", kind=ConversationKind.SPACE, title="Project")]
            )
            archive.write_messages(
                "space-1",
                [
                    Message(
                        source_id="msg-1",
                        conversation_id="space-1",
                        author_id="u1",
                        created_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
                        text="first",
                    ),
                    Message(
                        source_id="msg-2",
                        conversation_id="space-1",
                        author_id="u2",
                        created_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
                        text="second",
                    ),
                ],
            )
            conversation_map = (
                CompletedTeamsConversationMapping(
                    source_conversation_id="space-1",
                    target_kind=TeamsTargetKind.GROUP_CHAT,
                    target={"chat_id": "chat-1"},
                ),
            )
            output = root / "teams-import-plan.json"

            result = write_teams_import_plan(
                archive=archive,
                conversation_map=conversation_map,
                identity_map={"u1": "entra-u1", "u2": "entra-u2"},
                output_path=output,
            )
            payload = json.loads(output.read_text(encoding="utf-8"))

            self.assertEqual(result.conversations, 1)
            self.assertEqual(result.messages, 2)
            self.assertEqual(result.attachments, 0)
            self.assertEqual(result.unsupported_attachments, 0)
            self.assertEqual(result.timestamp_adjustments, 1)
            self.assertEqual(payload["format"], "exodus.teams.import_plan.v1")
            self.assertEqual(payload["messages"][1]["createdDateTime"], "2026-01-01T12:00:00.001Z")
            self.assertEqual(payload["messages"][1]["timestamp_adjustment_reason"], "timestamp_collision")
            self.assertEqual(payload["unsupported_attachments"], [])

            with self.assertRaises(FileExistsError):
                write_teams_import_plan(
                    archive=archive,
                    conversation_map=conversation_map,
                    identity_map={"u1": "entra-u1", "u2": "entra-u2"},
                    output_path=output,
                )

    def test_unsupported_attachments_counts_only_unsupported(self) -> None:
        # Bug fix regression: unsupported_attachments used to count ALL attachments.
        # When _attachment_to_plan_json returns supported=True the count must be 0.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = Archive(root / "archive")
            archive.initialize(source_kind="webex", target_kind="teams", name="demo")
            archive.write_conversations(
                [Conversation(source_id="space-1", kind=ConversationKind.SPACE, title="Project")]
            )
            archive.write_messages(
                "space-1",
                [
                    Message(
                        source_id="msg-1",
                        conversation_id="space-1",
                        author_id="u1",
                        created_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
                        text="with file",
                        attachments=(
                            Attachment(
                                source_id="file-1",
                                filename="notes.txt",
                                mime_type="text/plain",
                                size_bytes=12,
                                sha256="abc",
                                local_path="attachments/notes.txt",
                            ),
                        ),
                    )
                ],
            )
            conversation_map = (
                CompletedTeamsConversationMapping(
                    source_conversation_id="space-1",
                    target_kind=TeamsTargetKind.GROUP_CHAT,
                    target={"chat_id": "chat-1"},
                ),
            )
            output = root / "teams-import-plan.json"

            supported_attachment = {
                "source_attachment_id": "file-1",
                "filename": "notes.txt",
                "mime_type": "text/plain",
                "size_bytes": 12,
                "sha256": "abc",
                "local_path": "attachments/notes.txt",
                "supported": True,
                "reason": None,
            }
            with patch(
                "exodus_agent.targets.teams_mapping._attachment_to_plan_json",
                return_value=supported_attachment,
            ):
                result = write_teams_import_plan(
                    archive=archive,
                    conversation_map=conversation_map,
                    identity_map={"u1": "entra-u1"},
                    output_path=output,
                )

            self.assertEqual(result.attachments, 1)
            self.assertEqual(result.unsupported_attachments, 0)

    def test_many_same_timestamp_messages_near_cutoff_do_not_abort(self) -> None:
        # Bug fix regression: 10 messages sharing a timestamp 5 ms before now() caused
        # the collision-bump loop to overshoot the cutoff and abort with ValueError.
        # The fix caps overshot timestamps at import_cutoff instead of aborting.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = Archive(root / "archive")
            archive.initialize(source_kind="webex", target_kind="teams", name="demo")
            archive.write_conversations(
                [Conversation(source_id="space-1", kind=ConversationKind.SPACE, title="Project")]
            )
            import_time = datetime.now(tz=timezone.utc)
            shared_ts = import_time - timedelta(milliseconds=5)
            archive.write_messages(
                "space-1",
                [
                    Message(
                        source_id=f"msg-{i}",
                        conversation_id="space-1",
                        author_id="u1",
                        created_at=shared_ts,
                        text=f"message {i}",
                    )
                    for i in range(10)
                ],
            )
            conversation_map = (
                CompletedTeamsConversationMapping(
                    source_conversation_id="space-1",
                    target_kind=TeamsTargetKind.GROUP_CHAT,
                    target={"chat_id": "chat-1"},
                ),
            )

            # Must complete without ValueError even though bumped timestamps exceed cutoff
            messages = prepare_teams_import_messages(
                archive=archive,
                conversation_map=conversation_map,
                identity_map={"u1": "entra-u1"},
                import_time=import_time,
            )

            self.assertEqual(len(messages), 10)
            # Messages that required capping must flag it in the audit reason
            capped = [m for m in messages if m.timestamp_adjustment_reason and "cutoff_capped" in m.timestamp_adjustment_reason]
            self.assertGreater(len(capped), 0)

    def test_teams_import_plan_rejects_directory_output_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = _archive_with_conversation(
                root,
                conversation=Conversation(
                    source_id="space-1",
                    kind=ConversationKind.SPACE,
                    title="Project",
                ),
                participant_ids=("u1", "u2"),
            )
            conversation_map = (
                CompletedTeamsConversationMapping(
                    source_conversation_id="space-1",
                    target_kind=TeamsTargetKind.GROUP_CHAT,
                    target={"chat_id": "chat-1"},
                ),
            )
            output = root / "teams-import-plan.json"
            output.mkdir()

            with self.assertRaisesRegex(ValueError, "output path must be a file"):
                write_teams_import_plan(
                    archive=archive,
                    conversation_map=conversation_map,
                    identity_map={"u1": "entra-u1", "u2": "entra-u2"},
                    output_path=output,
                    overwrite=True,
                )

    def test_teams_import_plan_counts_mapped_conversations_without_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = Archive(root / "archive")
            archive.initialize(source_kind="webex", target_kind="teams", name="demo")
            archive.write_conversations(
                [Conversation(source_id="space-1", kind=ConversationKind.SPACE, title="Project")]
            )
            conversation_map = (
                CompletedTeamsConversationMapping(
                    source_conversation_id="space-1",
                    target_kind=TeamsTargetKind.GROUP_CHAT,
                    target={"chat_id": "chat-1"},
                ),
            )

            result = write_teams_import_plan(
                archive=archive,
                conversation_map=conversation_map,
                identity_map={},
                output_path=root / "teams-import-plan.json",
            )

            self.assertEqual(result.conversations, 1)
            self.assertEqual(result.messages, 0)
            self.assertEqual(result.attachments, 0)
            self.assertEqual(result.unsupported_attachments, 0)
            self.assertEqual(result.timestamp_adjustments, 0)

    def test_writes_teams_import_plan_with_unsupported_attachment_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = Archive(root / "archive")
            archive.initialize(source_kind="webex", target_kind="teams", name="demo")
            archive.write_conversations(
                [Conversation(source_id="space-1", kind=ConversationKind.SPACE, title="Project")]
            )
            archive.write_messages(
                "space-1",
                [
                    Message(
                        source_id="msg-1",
                        conversation_id="space-1",
                        author_id="u1",
                        created_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
                        text="with file",
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
            conversation_map = (
                CompletedTeamsConversationMapping(
                    source_conversation_id="space-1",
                    target_kind=TeamsTargetKind.GROUP_CHAT,
                    target={"chat_id": "chat-1"},
                ),
            )

            result = write_teams_import_plan(
                archive=archive,
                conversation_map=conversation_map,
                identity_map={"u1": "entra-u1"},
                output_path=root / "teams-import-plan.json",
            )
            payload = json.loads((root / "teams-import-plan.json").read_text(encoding="utf-8"))

            self.assertEqual(result.attachments, 1)
            self.assertEqual(result.unsupported_attachments, 1)
            self.assertEqual(payload["messages"][0]["attachments"][0]["source_attachment_id"], "file-1")
            self.assertEqual(payload["unsupported_attachments"][0]["source_message_id"], "msg-1")
            self.assertEqual(payload["unsupported_attachments"][0]["source_attachment_id"], "file-1")

    def test_ordered_messages_for_import_deep_chain_does_not_recurse(self) -> None:
        # Regression test: a chain deeper than Python's default recursion limit (~1000)
        # must not raise RecursionError.  The iterative DFS implementation handles this.
        depth = 1200
        base = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
        messages = [
            Message(
                source_id=f"msg-{i}",
                conversation_id="conv-1",
                author_id="u1",
                created_at=base + timedelta(seconds=i),
                parent_id=f"msg-{i - 1}" if i > 1 else None,
                text=f"message {i}",
            )
            for i in range(1, depth + 1)
        ]
        result = _ordered_messages_for_import(messages, conversation_id="conv-1")
        self.assertEqual(len(result), depth)
        for position, msg in enumerate(result):
            self.assertEqual(msg.source_id, f"msg-{position + 1}")


def _archive_with_conversation(
    root: Path,
    *,
    conversation: Conversation,
    participant_ids: tuple[str, ...],
) -> Archive:
    archive = Archive(root / "archive")
    archive.initialize(source_kind="webex", target_kind="teams", name="demo")
    archive.write_conversations([conversation])
    archive.write_participants(
        [
            Participant(
                source_id=source_id,
                display_name=source_id,
                email=f"{source_id}@example.com",
                metadata={"room_id": conversation.source_id},
            )
            for source_id in participant_ids
        ]
    )
    archive.write_memberships(
        [
            ConversationMembership(
                source_id=f"membership-{index}",
                conversation_id=conversation.source_id,
                participant_id=source_id,
            )
            for index, source_id in enumerate(participant_ids)
        ]
    )
    archive.write_messages(
        conversation.source_id,
        [
            Message(
                source_id=f"msg-{index}",
                conversation_id=conversation.source_id,
                author_id=source_id,
                created_at=datetime(2026, 1, 1, 0, index, tzinfo=timezone.utc),
                text="hello",
            )
            for index, source_id in enumerate(participant_ids)
        ],
    )
    return archive


def _archive_with_participants(root: Path, participants: list[Participant]) -> Archive:
    archive = Archive(root / "archive")
    archive.initialize(source_kind="webex", target_kind="teams", name="demo")
    archive.write_participants(participants)
    return archive


if __name__ == "__main__":
    unittest.main()
