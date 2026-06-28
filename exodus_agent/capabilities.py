from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Capability(StrEnum):
    DISCOVERY_SOURCE = "discovery_source"
    MESSAGE_SOURCE = "message_source"
    MEDIA_SOURCE = "media_source"
    IDENTITY_DIRECTORY = "identity_directory"
    DESTINATION_PROVISIONER = "destination_provisioner"
    MIGRATION_SESSION_TARGET = "migration_session_target"
    HISTORICAL_IMPORT_TARGET = "historical_import_target"
    REPLAY_TARGET = "replay_target"
    VERIFIER = "verifier"


@dataclass(frozen=True)
class ConnectorDescriptor:
    kind: str
    display_name: str
    capabilities: frozenset[Capability]
    notes: tuple[str, ...] = ()

