from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from exodus_agent.config import load_config
from exodus_agent.planner import build_plan


class ConfigAndPlannerTests(unittest.TestCase):
    def test_builds_historical_import_plan_for_webex_to_telegram(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "migration.toml"
            path.write_text(
                """
name = "demo"
mode = "organization"
runtime = "local"
workspace = ".exodus/demo"

[source]
kind = "webex"
scope = "organization"

[target]
kind = "telegram"

[policy]
legal_basis = "customer-approved migration"
approved_by = "security@example.com"
retention_start = "2020-01-01T00:00:00Z"
retention_end = "2026-01-01T00:00:00Z"
include_direct_messages = false
""".strip(),
                encoding="utf-8",
            )

            config = load_config(path)
            plan = build_plan(config)

            self.assertIn("historical_import", plan.phases)
            self.assertIn("Webex organization mode requires compliance/admin authorization.", plan.warnings)
            self.assertIn("Webex organization extraction is not implemented in this build.", plan.warnings)

    def test_organization_mode_requires_source_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "migration.toml"
            path.write_text(
                """
mode = "organization"

[source]
kind = "webex"
scope = "user_rooms"

[target]
kind = "telegram"

[policy]
legal_basis = "customer-approved migration"
approved_by = "security@example.com"
retention_start = "2020-01-01T00:00:00Z"
retention_end = "2026-01-01T00:00:00Z"
include_direct_messages = false
""".strip(),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "scope"):
                build_plan(load_config(path))

    def test_organization_mode_requires_policy_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "migration.toml"
            path.write_text(
                """
mode = "organization"

[source]
kind = "webex"
scope = "organization"

[target]
kind = "telegram"
""".strip(),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "policy"):
                build_plan(load_config(path))

    def test_organization_mode_rejects_whitespace_policy_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "migration.toml"
            path.write_text(
                """
mode = "organization"

[source]
kind = "webex"
scope = "organization"

[target]
kind = "telegram"

[policy]
legal_basis = "   "
approved_by = "security@example.com"
retention_start = "2020-01-01T00:00:00Z"
retention_end = "2026-01-01T00:00:00Z"
include_direct_messages = false
""".strip(),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "legal_basis"):
                build_plan(load_config(path))

    def test_organization_mode_requires_explicit_direct_message_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "migration.toml"
            path.write_text(
                """
mode = "organization"

[source]
kind = "webex"
scope = "organization"

[target]
kind = "telegram"

[policy]
legal_basis = "customer-approved migration"
approved_by = "security@example.com"
retention_start = "2020-01-01T00:00:00Z"
retention_end = "2026-01-01T00:00:00Z"
""".strip(),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "include_direct_messages"):
                build_plan(load_config(path))

    def test_organization_mode_rejects_invalid_retention_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "migration.toml"
            path.write_text(
                """
mode = "organization"

[source]
kind = "webex"
scope = "organization"

[target]
kind = "telegram"

[policy]
legal_basis = "customer-approved migration"
approved_by = "security@example.com"
retention_start = "2026-01-01T00:00:00Z"
retention_end = "2020-01-01T00:00:00Z"
include_direct_messages = false
""".strip(),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "retention_start"):
                build_plan(load_config(path))

    def test_rejects_unknown_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "migration.toml"
            path.write_text(
                """
runtime = "somewhere"

[source]
kind = "webex"

[target]
kind = "telegram"
""".strip(),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "Invalid runtime"):
                load_config(path)

    def test_rejects_invalid_toml_with_config_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "migration.toml"
            path.write_text("[source\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, r"Migration config is not valid TOML: .*migration.toml"):
                load_config(path)

    def test_rejects_non_utf8_config_with_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "migration.toml"
            path.write_bytes(b"\xff")

            with self.assertRaisesRegex(ValueError, r"Migration config is not valid UTF-8: .*migration.toml"):
                load_config(path)

    def test_rejects_config_directory_with_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "migration.toml"
            path.mkdir()

            with self.assertRaisesRegex(ValueError, r"Migration config must be a file: .*migration.toml"):
                load_config(path)

    def test_rejects_non_string_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "migration.toml"
            path.write_text(
                """
workspace = true

[source]
kind = "webex"

[target]
kind = "telegram"
""".strip(),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "workspace"):
                load_config(path)

    def test_trims_basic_string_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "migration.toml"
            path.write_text(
                """
name = " demo "
mode = " individual "
runtime = " local "
workspace = " .exodus/demo "

[source]
kind = " webex "

[target]
kind = " telegram "
""".strip(),
                encoding="utf-8",
            )

            config = load_config(path)

            self.assertEqual(config.name, "demo")
            self.assertEqual(config.mode, "individual")
            self.assertEqual(config.runtime, "local")
            self.assertEqual(config.workspace, Path(".exodus/demo"))
            self.assertEqual(config.source.kind, "webex")
            self.assertEqual(config.target.kind, "telegram")

    def test_allows_absolute_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "migration.toml"
            workspace = Path(tmp) / "workspace"
            path.write_text(
                f"""
workspace = "{workspace}"

[source]
kind = "webex"

[target]
kind = "telegram"
""".strip(),
                encoding="utf-8",
            )

            self.assertEqual(load_config(path).workspace, workspace)

    def test_rejects_traversing_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "migration.toml"
            path.write_text(
                """
workspace = "../outside"

[source]
kind = "webex"

[target]
kind = "telegram"
""".strip(),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "workspace"):
                load_config(path)

    def test_rejects_empty_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "migration.toml"
            path.write_text(
                """
name = ""

[source]
kind = "webex"

[target]
kind = "telegram"
""".strip(),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "name"):
                load_config(path)


if __name__ == "__main__":
    unittest.main()
