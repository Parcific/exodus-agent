from __future__ import annotations

import os
import unittest

from exodus_agent.secrets import SecretResolutionError, resolve_secret


class SecretTests(unittest.TestCase):
    def test_resolves_env_secret(self) -> None:
        os.environ["EXODUS_TEST_SECRET"] = "secret-value"
        self.addCleanup(os.environ.pop, "EXODUS_TEST_SECRET", None)

        self.assertEqual(
            resolve_secret("env:EXODUS_TEST_SECRET", field_name="source.auth"),
            "secret-value",
        )

    def test_resolves_env_secret_with_surrounding_reference_whitespace(self) -> None:
        os.environ["EXODUS_TEST_SECRET"] = "secret-value"
        self.addCleanup(os.environ.pop, "EXODUS_TEST_SECRET", None)

        self.assertEqual(
            resolve_secret(" env:EXODUS_TEST_SECRET ", field_name="source.auth"),
            "secret-value",
        )

    def test_missing_env_secret_fails_without_leaking_value(self) -> None:
        os.environ.pop("EXODUS_MISSING_SECRET", None)

        with self.assertRaisesRegex(SecretResolutionError, "EXODUS_MISSING_SECRET"):
            resolve_secret("env:EXODUS_MISSING_SECRET", field_name="source.auth")

    def test_rejects_blank_secret_reference(self) -> None:
        with self.assertRaisesRegex(SecretResolutionError, "Missing secret reference"):
            resolve_secret("   ", field_name="source.auth")

    def test_rejects_malformed_env_secret_reference(self) -> None:
        with self.assertRaisesRegex(SecretResolutionError, "Invalid env secret reference"):
            resolve_secret("env:BAD-NAME", field_name="source.auth")

    def test_trims_literal_secret_reference(self) -> None:
        self.assertEqual(resolve_secret(" token ", field_name="source.auth"), "token")


if __name__ == "__main__":
    unittest.main()
