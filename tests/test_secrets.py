from __future__ import annotations

import os
import tempfile
import unittest

from exodus_agent.secrets import SecretResolutionError, SecretValue, resolve_secret


class SecretValueTests(unittest.TestCase):
    def test_secret_value_repr_does_not_leak_value(self) -> None:
        sv = SecretValue("secret")
        self.assertNotIn("secret", repr(sv))

    def test_secret_value_str_does_not_leak_value(self) -> None:
        sv = SecretValue("secret")
        self.assertNotIn("secret", str(sv))

    def test_secret_value_reveal_returns_value(self) -> None:
        self.assertEqual(SecretValue("secret").reveal(), "secret")

    def test_secret_value_equality(self) -> None:
        self.assertEqual(SecretValue("a"), SecretValue("a"))
        self.assertNotEqual(SecretValue("a"), SecretValue("b"))

    def test_secret_value_inequality_with_non_secret_value(self) -> None:
        self.assertIs(SecretValue("a").__eq__("a"), NotImplemented)


class SecretTests(unittest.TestCase):
    def test_resolve_returns_secret_value_type(self) -> None:
        os.environ["EXODUS_TEST_SECRET"] = "secret-value"
        self.addCleanup(os.environ.pop, "EXODUS_TEST_SECRET", None)

        result = resolve_secret("env:EXODUS_TEST_SECRET", field_name="f")
        self.assertIsInstance(result, SecretValue)

    def test_resolves_env_secret(self) -> None:
        os.environ["EXODUS_TEST_SECRET"] = "secret-value"
        self.addCleanup(os.environ.pop, "EXODUS_TEST_SECRET", None)

        self.assertEqual(
            resolve_secret("env:EXODUS_TEST_SECRET", field_name="source.auth").reveal(),
            "secret-value",
        )

    def test_resolves_env_secret_with_surrounding_reference_whitespace(self) -> None:
        os.environ["EXODUS_TEST_SECRET"] = "secret-value"
        self.addCleanup(os.environ.pop, "EXODUS_TEST_SECRET", None)

        self.assertEqual(
            resolve_secret(" env:EXODUS_TEST_SECRET ", field_name="source.auth").reveal(),
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
        with self.assertLogs("exodus.secrets", level="WARNING"):
            result = resolve_secret(" token ", field_name="source.auth")
        self.assertEqual(result.reveal(), "token")

    def test_resolves_file_secret(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as fh:
            fh.write("secret-value\n")
            path = fh.name
        self.addCleanup(os.unlink, path)

        self.assertEqual(
            resolve_secret(f"file:{path}", field_name="source.auth").reveal(),
            "secret-value",
        )

    def test_file_secret_missing_file_fails(self) -> None:
        with self.assertRaisesRegex(SecretResolutionError, "Secret file not found"):
            resolve_secret("file:/nonexistent/path/to/secret.txt", field_name="source.auth")

    def test_file_secret_empty_file_fails(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as fh:
            fh.write("")
            path = fh.name
        self.addCleanup(os.unlink, path)

        with self.assertRaisesRegex(SecretResolutionError, "Secret file is empty"):
            resolve_secret(f"file:{path}", field_name="source.auth")

    def test_literal_secret_logs_warning(self) -> None:
        with self.assertLogs("exodus.secrets", level="WARNING") as cm:
            resolve_secret("my-literal-token", field_name="source.auth")
        self.assertTrue(any("literal" in msg for msg in cm.output))


if __name__ == "__main__":
    unittest.main()
