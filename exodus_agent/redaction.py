from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


SENSITIVE_KEYS = {
    "access_token",
    "api_key",
    "api_hash",
    "authorization",
    "client_secret",
    "password",
    "refresh_token",
    "secret",
    "secret_key",
    "session",
    "token",
}


_SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)\b("
    + "|".join(re.escape(key) for key in sorted(SENSITIVE_KEYS, key=len, reverse=True))
    + r")\s*([=:])\s*([^&\s]+)"
)
_SENSITIVE_QUOTED_ASSIGNMENT_RE = re.compile(
    r"(?i)([\"'])("
    + "|".join(re.escape(key) for key in sorted(SENSITIVE_KEYS, key=len, reverse=True))
    + r")\1\s*:\s*([\"'])(.*?)\3"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+([A-Za-z0-9._~+/=-]+)")


def redact_sensitive(value: Any) -> Any:
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and key.casefold() in SENSITIVE_KEYS:
                redacted[str(key)] = "[redacted]"
            else:
                redacted[str(key)] = redact_sensitive(item)
        return redacted
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return [redact_sensitive(item) for item in value]
    return value


def redact_text(value: object) -> str:
    text = str(value)
    text = _SENSITIVE_QUOTED_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{match.group(1)}:{match.group(3)}[redacted]{match.group(3)}",
        text,
    )
    text = _SENSITIVE_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}[redacted]", text)
    return _BEARER_RE.sub("Bearer [redacted]", text)
