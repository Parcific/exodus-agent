from __future__ import annotations

import unittest

from exodus_agent.redaction import redact_sensitive, redact_text


class RedactionTests(unittest.TestCase):
    def test_redacts_nested_dicts_and_lists(self) -> None:
        payload = {
            "token": "top",
            "nested": {
                "Authorization": "bearer",
                "client_secret": "client",
                "access_token": "access",
                "refresh_token": "refresh",
                "items": [
                    {"session": "s1", "api_key": "key", "value": 1},
                    {"ok": True, "children": ({"api_hash": "hash", "secret_key": "sk"},)},
                ],
            },
        }

        redacted = redact_sensitive(payload)

        self.assertEqual(redacted["token"], "[redacted]")
        self.assertEqual(redacted["nested"]["Authorization"], "[redacted]")
        self.assertEqual(redacted["nested"]["client_secret"], "[redacted]")
        self.assertEqual(redacted["nested"]["access_token"], "[redacted]")
        self.assertEqual(redacted["nested"]["refresh_token"], "[redacted]")
        self.assertEqual(redacted["nested"]["items"][0]["session"], "[redacted]")
        self.assertEqual(redacted["nested"]["items"][0]["api_key"], "[redacted]")
        self.assertEqual(redacted["nested"]["items"][1]["children"][0]["api_hash"], "[redacted]")
        self.assertEqual(redacted["nested"]["items"][1]["children"][0]["secret_key"], "[redacted]")
        self.assertEqual(redacted["nested"]["items"][1]["ok"], True)

    def test_redacts_sensitive_text_assignments_and_bearer_tokens(self) -> None:
        redacted = redact_text(
            "request failed token=abc123&ok=1 api_key:secret Bearer abc.def"
        )

        self.assertIn("token=[redacted]", redacted)
        self.assertIn("api_key:[redacted]", redacted)
        self.assertIn("Bearer [redacted]", redacted)
        self.assertNotIn("abc123", redacted)
        self.assertNotIn("abc.def", redacted)

    def test_redacts_sensitive_json_style_text_fields(self) -> None:
        redacted = redact_text(
            '{"access_token":"abc 123","ok":true} '
            "{'client_secret':'secret with spaces'}"
        )

        self.assertIn('"access_token":"[redacted]"', redacted)
        self.assertIn("'client_secret':'[redacted]'", redacted)
        self.assertNotIn("abc 123", redacted)
        self.assertNotIn("secret with spaces", redacted)


if __name__ == "__main__":
    unittest.main()
