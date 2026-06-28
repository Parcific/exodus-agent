from __future__ import annotations

import os
import re


ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SecretResolutionError(RuntimeError):
    pass


def resolve_secret(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SecretResolutionError(f"Missing secret reference for {field_name}")
    value = value.strip()
    if value.startswith("env:"):
        env_name = value.removeprefix("env:").strip()
        if not ENV_NAME_PATTERN.fullmatch(env_name):
            raise SecretResolutionError(f"Invalid env secret reference for {field_name}")
        secret = os.environ.get(env_name)
        if not secret:
            raise SecretResolutionError(f"Environment variable is not set for {field_name}: {env_name}")
        return secret
    return value
