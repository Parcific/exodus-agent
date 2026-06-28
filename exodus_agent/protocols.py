from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol, runtime_checkable

from .model import Attachment, Conversation, ConversationMembership, Message, Participant, Workspace


class DiscoverySource(Protocol):
    def get_workspace(self) -> Workspace:
        """Return the source workspace or tenant represented by this run."""

    def list_conversations(self) -> Iterable[Conversation]:
        """Return conversations in scope for the migration."""

    def list_participants(self) -> Iterable[Participant]:
        """Return participants in scope for the migration."""


class MessageSource(Protocol):
    def list_messages(self, conversation: Conversation) -> Iterable[Message]:
        """Return messages for a source conversation in chronological order when possible."""


@runtime_checkable
class MediaSource(Protocol):
    def download_attachment(self, attachment: Attachment) -> bytes:
        """Download attachment bytes for local archive storage."""


@runtime_checkable
class MembershipSource(Protocol):
    def list_memberships(self) -> Iterable[ConversationMembership]:
        """Return per-conversation memberships in scope for the migration."""


class HistoricalImportTarget(Protocol):
    def prepare(self, conversations: Iterable[Conversation], participants: Iterable[Participant]) -> None:
        """Create or bind destination containers and validate import eligibility."""

    def import_messages(self, conversation: Conversation, messages: Iterable[Message]) -> None:
        """Import messages into the destination preserving history where supported."""

    def verify(self) -> None:
        """Verify destination state against imported source records."""
