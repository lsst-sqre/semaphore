"""Data containers for broadcast messages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import arrow

if TYPE_CHECKING:
    from typing import Optional

__all__ = ["BroadcastMessage", "OneTimeBroadcastMessage"]


@dataclass
class BroadcastMessage:
    """A basic broadcast message that does not expire or repeat."""

    source_path: str
    """The path of the source in the GitHub repository, which serves as a
    unique identifier.
    """

    summary_md: str
    """The message message, as markdown."""

    body_md: Optional[str]
    """The body content, as markdown."""

    @property
    def active(self) -> bool:
        """Whether the message should be served to clients for display."""
        return True


@dataclass
class OneTimeBroadcastMessage(BroadcastMessage):
    """A broadcast that is scheduled to display for a single time window."""

    defer: arrow.Arrow
    """Time when the message begins to be displayed."""

    expire: arrow.Arrow
    """Time when the message begins to be considered expired."""

    @property
    def active(self) -> bool:
        """Whether the message should be served to clients for display."""
        return arrow.utcnow().is_between(self.defer, self.expire, bounds="[)")
