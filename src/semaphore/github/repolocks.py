"""Per-repository locks for GitHub repositories that publish broadcasts."""

import asyncio

__all__ = ["RepoLocks"]


class RepoLocks:
    """A registry of asyncio locks, one for each GitHub repository.

    Holding a repository's lock serializes the work that reads the
    repository and updates the broadcast messages it publishes, such as
    processing a push, so that two such tasks for one repository do not
    interleave. Tasks for different repositories do not block each other.

    Repositories are identified by full name (``owner/repo``), compared
    case-insensitively, as `BroadcastSourcePolicy` compares them. A lock is
    created the first time its repository is seen and is kept for the life
    of the registry. Only repositories that the source policy allows are
    locked, so the registry stays small.
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    def get(self, repo_full_name: str) -> asyncio.Lock:
        """Return the lock for a repository.

        Parameters
        ----------
        repo_full_name
            Full name (``owner/repo``) of the repository.

        Returns
        -------
        asyncio.Lock
            The repository's lock, which is the same object for every call
            with the same repository.
        """
        key = repo_full_name.lower()
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock
