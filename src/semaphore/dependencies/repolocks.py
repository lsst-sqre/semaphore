"""Repository locks dependency.

This FastAPI dependency provides the per-repository locks of broadcast
sources to request handlers. Code outside a request, such as the startup
lifespan, can await the dependency to share the same locks.
"""

from ..github.repolocks import RepoLocks

__all__ = ["RepoLocksDependency", "repo_locks_dependency"]


class RepoLocksDependency:
    """Provides the per-repository locks of broadcast sources."""

    def __init__(self) -> None:
        self.locks = RepoLocks()

    async def __call__(self) -> RepoLocks:
        """Return the per-repository locks."""
        return self.locks


repo_locks_dependency = RepoLocksDependency()
"""A FastAPI dependency that returns the per-repository locks."""
