from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

VALID_MODES = frozenset({"individual", "organization"})
VALID_RUNTIMES = frozenset({"local", "customer_cloud_worker", "managed_cloud_worker"})


@dataclass(frozen=True)
class EndpointConfig:
    kind: str
    settings: dict[str, Any]


@dataclass(frozen=True)
class MigrationConfig:
    name: str
    mode: str
    runtime: str
    workspace: Path
    source: EndpointConfig
    target: EndpointConfig
    policy: dict[str, Any]


def load_config(path: Path) -> MigrationConfig:
    if not path.exists():
        raise FileNotFoundError(path)
    if not path.is_file():
        raise ValueError(f"Migration config must be a file: {path}")

    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        raise ValueError(f"Migration config is not valid UTF-8: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Migration config is not valid TOML: {path}: {exc}") from exc
    source = _endpoint(data, "source")
    target = _endpoint(data, "target")

    name = _optional_string(data, "name", default=path.stem)
    mode = _optional_string(data, "mode", default="individual")
    runtime = _optional_string(data, "runtime", default="local")
    workspace = _workspace_path(_optional_string(data, "workspace", default=".exodus"))
    if mode not in VALID_MODES:
        raise ValueError(f"Invalid mode {mode!r}. Expected one of: {', '.join(sorted(VALID_MODES))}")
    if runtime not in VALID_RUNTIMES:
        raise ValueError(
            f"Invalid runtime {runtime!r}. Expected one of: {', '.join(sorted(VALID_RUNTIMES))}"
        )

    return MigrationConfig(
        name=name,
        mode=mode,
        runtime=runtime,
        workspace=workspace,
        source=source,
        target=target,
        policy=_policy(data),
    )


def _endpoint(data: dict[str, Any], key: str) -> EndpointConfig:
    raw = data.get(key)
    if not isinstance(raw, dict):
        raise ValueError(f"Missing [{key}] section")
    kind = raw.get("kind")
    if not isinstance(kind, str) or not kind.strip():
        raise ValueError(f"Missing [{key}].kind")
    return EndpointConfig(kind=kind.strip(), settings={k: v for k, v in raw.items() if k != "kind"})


def _policy(data: dict[str, Any]) -> dict[str, Any]:
    raw = data.get("policy", {})
    if not isinstance(raw, dict):
        raise ValueError("Missing or invalid [policy] section")
    return dict(raw)


def _optional_string(data: dict[str, Any], key: str, *, default: str) -> str:
    value = data.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _workspace_path(value: str) -> Path:
    path = Path(value)
    if ".." in path.parts:
        raise ValueError("workspace must not contain parent directory traversal")
    return path
