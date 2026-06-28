from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .capabilities import Capability
from .config import MigrationConfig
from .connectors import get_connector


@dataclass(frozen=True)
class Plan:
    phases: tuple[str, ...]
    warnings: tuple[str, ...]


def build_plan(config: MigrationConfig) -> Plan:
    source = get_connector(config.source.kind)
    target = get_connector(config.target.kind)
    warnings: list[str] = []

    _require(source.capabilities, Capability.MESSAGE_SOURCE, source.kind)

    if Capability.HISTORICAL_IMPORT_TARGET in target.capabilities:
        load_phase = "historical_import"
    elif Capability.REPLAY_TARGET in target.capabilities:
        load_phase = "replay"
        warnings.append("Target does not support historical import; replay fidelity is lower.")
    else:
        raise ValueError(f"Target {target.kind!r} cannot import or replay messages")

    if config.runtime == "managed_cloud_worker":
        warnings.append(
            "Managed cloud workers require mature tenant isolation and secret custody controls."
        )

    if config.mode == "organization":
        _validate_organization_policy(config)
        if config.source.kind == "webex":
            warnings.append("Webex organization mode requires compliance/admin authorization.")
            warnings.append("Webex organization extraction is not implemented in this build.")

    phases = (
        "preflight",
        "extract",
        "normalize",
        "map_identities",
        "map_conversations",
        "prepare_destination",
        load_phase,
        "verify",
        "report",
    )
    return Plan(phases=phases, warnings=tuple(warnings))


def _require(capabilities: frozenset[Capability], capability: Capability, connector: str) -> None:
    if capability not in capabilities:
        raise ValueError(f"Connector {connector!r} does not support {capability.value}")


def _validate_organization_policy(config: MigrationConfig) -> None:
    if config.source.kind == "webex" and config.source.settings.get("scope") != "organization":
        raise ValueError('organization mode requires [source].scope = "organization" for Webex')

    required = ("legal_basis", "approved_by", "retention_start", "retention_end")
    missing = [
        key
        for key in required
        if not isinstance(config.policy.get(key), str) or not config.policy.get(key).strip()
    ]
    if missing:
        raise ValueError(
            "organization mode requires [policy] fields: " + ", ".join(sorted(missing))
        )

    retention_start = _parse_policy_datetime(config.policy["retention_start"].strip(), "retention_start")
    retention_end = _parse_policy_datetime(config.policy["retention_end"].strip(), "retention_end")
    if retention_start >= retention_end:
        raise ValueError("organization policy retention_start must be before retention_end")

    if "include_direct_messages" not in config.policy:
        raise ValueError("organization mode requires [policy] fields: include_direct_messages")
    include_direct = config.policy["include_direct_messages"]
    if not isinstance(include_direct, bool):
        raise ValueError("organization policy include_direct_messages must be a boolean")


def _parse_policy_datetime(value: str, field: str) -> datetime:
    try:
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"organization policy {field} must be an ISO-8601 datetime") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"organization policy {field} must include a timezone")
    return parsed.astimezone(timezone.utc)
