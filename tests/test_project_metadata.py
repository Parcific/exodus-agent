from __future__ import annotations

import tomllib
import unittest
from pathlib import Path


class ProjectMetadataTests(unittest.TestCase):
    def test_project_metadata_advertises_implemented_cli_surfaces(self) -> None:
        payload = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
        project = payload["project"]
        scripts = project["scripts"]

        self.assertIn("Telegram", project["description"])
        self.assertIn("Teams", project["description"])
        self.assertEqual(scripts["exodus"], "exodus_agent.cli:main")
        self.assertEqual(
            scripts["exodus-telegram-mtproto-runner"],
            "exodus_agent.mtproto_runner:main",
        )
        self.assertIn("telegram", project["optional-dependencies"])


if __name__ == "__main__":
    unittest.main()
