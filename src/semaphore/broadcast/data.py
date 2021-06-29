"""Data containers for broadcast messages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Optional

__all__ = ["BroadcastMessage"]


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
