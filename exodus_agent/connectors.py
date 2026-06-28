from __future__ import annotations

from .capabilities import Capability, ConnectorDescriptor


CONNECTORS: dict[str, ConnectorDescriptor] = {
    "webex": ConnectorDescriptor(
        kind="webex",
        display_name="Cisco Webex",
        capabilities=frozenset(
            {
                Capability.DISCOVERY_SOURCE,
                Capability.MESSAGE_SOURCE,
                Capability.MEDIA_SOURCE,
                Capability.IDENTITY_DIRECTORY,
            }
        ),
        notes=(
            "Organization scope requires an approved compliance/admin identity.",
            "Normal user tokens only cover rooms visible to that user.",
        ),
    ),
    "telegram": ConnectorDescriptor(
        kind="telegram",
        display_name="Telegram",
        capabilities=frozenset(
            {
                Capability.DESTINATION_PROVISIONER,
                Capability.MIGRATION_SESSION_TARGET,
                Capability.HISTORICAL_IMPORT_TARGET,
                Capability.REPLAY_TARGET,
                Capability.VERIFIER,
            }
        ),
        notes=(
            "Prefer MTProto history import for faithful historical migration.",
            "Bot/API replay is a fallback with weaker timestamp/authorship fidelity.",
        ),
    ),
    "teams": ConnectorDescriptor(
        kind="teams",
        display_name="Microsoft Teams",
        capabilities=frozenset(
            {
                Capability.DESTINATION_PROVISIONER,
                Capability.MIGRATION_SESSION_TARGET,
                Capability.HISTORICAL_IMPORT_TARGET,
                Capability.VERIFIER,
            }
        ),
        notes=(
            "Graph external message import requires migration mode and app-only permissions.",
            "Import throughput must respect Graph throttling and per-channel limits.",
        ),
    ),
}


def get_connector(kind: str) -> ConnectorDescriptor:
    try:
        return CONNECTORS[kind]
    except KeyError as exc:
        known = ", ".join(sorted(CONNECTORS))
        raise ValueError(f"Unknown connector {kind!r}. Known connectors: {known}") from exc

