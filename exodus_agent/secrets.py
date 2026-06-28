from __future__ import annotations

import logging
import os
import re
from pathlib import Path


ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_log = logging.getLogger("exodus.secrets")


class SecretResolutionError(RuntimeError):
    pass


class SecretValue:
    """Opaque container for a resolved secret.

    The value is never revealed through ``__repr__`` or ``__str__``; callers
    must explicitly call :meth:`reveal` to obtain the underlying string.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value: str = value

    def __repr__(self) -> str:
        return "SecretValue('<masked>')"

    def __str__(self) -> str:
        return "**********"

    def reveal(self) -> str:
        """Return the raw secret value."""
        return self._value

    def __eq__(self, other: object) -> bool:
        if isinstance(other, SecretValue):
            return self._value == other._value
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._value)


def resolve_secret(value: object, *, field_name: str) -> SecretValue:
    """Resolve a secret reference to a :class:`SecretValue`.

    Supported reference schemes:

    * ``env:VAR_NAME`` — read from an environment variable.
    * ``file:/absolute/or/relative/path`` — read from a file on disk; content
      is stripped of surrounding whitespace.
    * ``<literal>`` — used as-is, but emits a WARNING log because hardcoded
      secrets are discouraged.

    Raises :class:`SecretResolutionError` on any resolution failure.
    """
    if not isinstance(value, str) or not value.strip():
        _log.warning("Missing secret reference for %s", field_name)
        raise SecretResolutionError(f"Missing secret reference for {field_name}")

    value = value.strip()

    if value.startswith("env:"):
        return _resolve_env(value, field_name=field_name)

    if value.startswith("file:"):
        return _resolve_file(value, field_name=field_name)

    # Literal fallthrough — return the value but warn the caller.
    _log.warning(
        "Resolved %s via literal backend — hardcoded secrets are discouraged",
        field_name,
    )
    return SecretValue(value)


def _resolve_env(raw: str, *, field_name: str) -> SecretValue:
    env_name = raw.removeprefix("env:").strip()
    if not ENV_NAME_PATTERN.fullmatch(env_name):
        _log.warning("Invalid env secret reference for %s: %r", field_name, env_name)
        raise SecretResolutionError(f"Invalid env secret reference for {field_name}")
    secret = os.environ.get(env_name)
    if not secret:
        _log.warning(
            "Environment variable is not set for %s: %s", field_name, env_name
        )
        raise SecretResolutionError(
            f"Environment variable is not set for {field_name}: {env_name}"
        )
    _log.info("Resolved %s via env backend", field_name)
    return SecretValue(secret)


def _resolve_file(raw: str, *, field_name: str) -> SecretValue:
    path = raw.removeprefix("file:").strip()
    try:
        content = Path(path).read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        _log.warning("Secret file not found for %s: %s", field_name, path)
        raise SecretResolutionError(
            f"Secret file not found for {field_name}: {path}"
        ) from exc
    except OSError as exc:
        _log.warning("Failed to read secret file for %s: %s", field_name, path)
        raise SecretResolutionError(
            f"Failed to read secret file for {field_name}: {path}"
        ) from exc
    if not content:
        _log.warning("Secret file is empty for %s: %s", field_name, path)
        raise SecretResolutionError(f"Secret file is empty for {field_name}: {path}")
    _log.info("Resolved %s via file backend", field_name)
    return SecretValue(content)
