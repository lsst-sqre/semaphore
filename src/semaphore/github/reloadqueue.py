"""The queue of GitHub repositories whose broadcasts must be reloaded."""

import asyncio
from dataclasses import dataclass

__all__ = [
    "QueuedInstallation",
    "QueuedInstallationListing",
    "QueuedReload",
    "QueuedRepo",
    "ReloadQueue",
]


@dataclass(frozen=True, slots=True)
class QueuedRepo:
    """A repository whose broadcasts must be reloaded, because GitHub failed
    while they were loaded or while a push to the repository was processed.
    """

    installation_id: str
    """ID of the GitHub App installation that can see the repository."""

    full_name: str
    """Full name (``owner/repo``) of the repository."""


@dataclass(frozen=True, slots=True)
class QueuedInstallation:
    """A GitHub App installation whose repositories' broadcasts must be
    loaded.

    It is queued when GitHub fails to authorize it, or to list its
    repositories, as broadcasts are loaded at startup, and when the reload
    worker lists it after GitHub failed to list the installations at startup
    (see `QueuedInstallationListing`).
    """

    installation_id: str
    """ID of the GitHub App installation."""

    account: str
    """Login of the user or organization that the installation is on."""


@dataclass(frozen=True, slots=True)
class QueuedInstallationListing:
    """The listing of the GitHub App's installations, which GitHub failed to
    return as broadcasts were loaded at startup.

    The installations must be listed again, and the repositories of each
    one loaded. There is at most one such entry, and every instance is
    equal.
    """


type QueuedReload = QueuedRepo | QueuedInstallation | QueuedInstallationListing
"""An entry of the `ReloadQueue`."""


class ReloadQueue:
    """The repositories and installations whose broadcasts must be reloaded
    from GitHub, which one background worker reloads (see
    `~semaphore.github.broadcastservices.run_reload_worker`).

    Each entry is pending once: adding an entry that is already pending does
    nothing, so several failures for one repository cause one reload. A
    repository is identified by its installation ID and its full name
    (``owner/repo``), compared case-insensitively, as `RepoLocks` compares
    them; an installation by its ID. The listing of the installations is a
    single entry. Entries are kept in the order they were first added.

    The queue is held in memory by each process, so it is lost when the
    process restarts. Loading broadcasts at startup then covers it.
    """

    def __init__(self) -> None:
        self._pending: dict[tuple[str, ...], QueuedReload] = {}
        self._has_pending = asyncio.Event()
        self._is_empty = asyncio.Event()
        self._is_empty.set()

    def __contains__(self, entry: QueuedReload) -> bool:
        return self._get_key(entry) in self._pending

    def __len__(self) -> int:
        return len(self._pending)

    def add_repo(self, *, installation_id: str, repo_full_name: str) -> None:
        """Add a repository whose broadcasts must be reloaded, unless it is
        already pending.

        Parameters
        ----------
        installation_id
            ID of the GitHub App installation that can see the repository.
        repo_full_name
            Full name (``owner/repo``) of the repository.
        """
        self._add(
            QueuedRepo(
                installation_id=installation_id, full_name=repo_full_name
            )
        )

    def add_installation(self, *, installation_id: str, account: str) -> None:
        """Add a GitHub App installation whose repositories' broadcasts must
        be loaded, unless it is already pending.

        Parameters
        ----------
        installation_id
            ID of the GitHub App installation.
        account
            Login of the user or organization that the installation is on.
        """
        self._add(
            QueuedInstallation(
                installation_id=installation_id, account=account
            )
        )

    def add_installation_listing(self) -> None:
        """Add the listing of the GitHub App's installations, which must be
        listed again, unless it is already pending.
        """
        self._add(QueuedInstallationListing())

    def discard(self, entry: QueuedReload) -> None:
        """Remove an entry, as once it has reloaded, if it is pending.

        Parameters
        ----------
        entry
            The entry to remove. A repository is matched by its installation
            ID and its full name, case-insensitively.
        """
        self._pending.pop(self._get_key(entry), None)
        if not self._pending:
            self._has_pending.clear()
            self._is_empty.set()

    def pending(self) -> list[QueuedReload]:
        """Return the pending entries, in the order they were first added.

        The list is a copy, so the queue may change while it is iterated
        over.
        """
        return list(self._pending.values())

    async def wait_for_pending(self) -> None:
        """Wait until an entry is pending."""
        await self._has_pending.wait()

    async def wait_until_empty(self) -> None:
        """Wait until no entry is pending, as after the last pending entry
        has reloaded.
        """
        await self._is_empty.wait()

    def _add(self, entry: QueuedReload) -> None:
        self._pending.setdefault(self._get_key(entry), entry)
        self._is_empty.clear()
        self._has_pending.set()

    @staticmethod
    def _get_key(entry: QueuedReload) -> tuple[str, ...]:
        match entry:
            case QueuedRepo():
                return ("repo", entry.installation_id, entry.full_name.lower())
            case QueuedInstallation():
                return ("installation", entry.installation_id)
            case QueuedInstallationListing():
                return ("installation_listing",)
