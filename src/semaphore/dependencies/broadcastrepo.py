"""Broadcast repository dependency.

This FastAPI dependency provides a repository to request handlers.
"""

from semaphore.broadcast.repository import BroadcastMessageRepository

__all__ = ["BroadcastRepoDependency", "broadcast_repo_dependency"]


class BroadcastRepoDependency:
    """Provides the broadcast repository."""

    def __init__(self) -> None:
        self.repo = BroadcastMessageRepository()

    def __call__(self) -> BroadcastMessageRepository:
        """Return the broadcast message repository."""
        return self.repo


broadcast_repo_dependency = BroadcastRepoDependency()
"""A FastAPI dependency that returns the broadcast repository."""
