"""The trust rule for GitHub repositories that publish broadcast messages."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Self

from ..config import Config

__all__ = [
    "BroadcastSourcePolicy",
    "SourceDecision",
    "SourceRejectionReason",
]


class SourceRejectionReason(StrEnum):
    """Reasons a GitHub repository is not allowed to publish broadcasts."""

    not_in_allowlist = "not_in_allowlist"
    """The repository's full name is not in the configured allowlist."""

    fork = "fork"
    """The repository is a fork, which is never a broadcast source."""


@dataclass(frozen=True, slots=True)
class SourceDecision:
    """The outcome of evaluating a repository with `BroadcastSourcePolicy`."""

    reason: SourceRejectionReason | None = None
    """Why the repository is rejected, or `None` if it is allowed."""

    @property
    def allowed(self) -> bool:
        """Whether the repository may publish broadcast messages."""
        return self.reason is None


class BroadcastSourcePolicy:
    """Decides which GitHub repositories may publish broadcast messages.

    This is the only place the trust rule for broadcast sources is
    implemented. A repository is allowed if it is not a fork and its full
    name (``owner/repo``) is in the allowlist. Names are compared
    case-insensitively.

    Parameters
    ----------
    allowed_repos
        Full names (``owner/repo``) of the repositories that may publish
        broadcast messages.
    """

    def __init__(self, allowed_repos: Iterable[str]) -> None:
        self._configured_repos = list(allowed_repos)
        self._allowed_repos = frozenset(
            name.lower() for name in self._configured_repos
        )
        self._allowed_owners = frozenset(
            name.split("/", maxsplit=1)[0] for name in self._allowed_repos
        )

    @classmethod
    def from_config(cls, config: Config) -> Self:
        """Create the policy from the application configuration.

        Parameters
        ----------
        config
            The application configuration, whose ``github_repos`` setting is
            the allowlist.

        Returns
        -------
        BroadcastSourcePolicy
            The policy.
        """
        return cls(config.github_repos)

    @property
    def allowed_repos(self) -> list[str]:
        """Full names of the allowed repositories, as configured."""
        return list(self._configured_repos)

    def allows_owner(self, owner: str) -> bool:
        """Determine whether an account owns any of the allowed repositories.

        Only an account that owns an allowed repository can be the source of
        broadcast messages, so the GitHub App's installations on other
        accounts need not be read.

        Parameters
        ----------
        owner
            Login of a GitHub user or organization, such as the
            ``account.login`` of a GitHub App installation. It is compared
            case-insensitively.

        Returns
        -------
        bool
            Whether ``owner`` is the owner of any allowed repository.
        """
        return owner.lower() in self._allowed_owners

    def evaluate(self, repository: Mapping[str, Any]) -> SourceDecision:
        """Decide whether a repository may publish broadcast messages.

        Parameters
        ----------
        repository
            A GitHub repository resource, such as the ``repository`` object
            of a push webhook payload or an item of the
            ``/installation/repositories`` listing. The ``full_name`` and
            ``fork`` fields are used.

        Returns
        -------
        SourceDecision
            The decision, with the rejection reason if the repository is not
            allowed.
        """
        if repository.get("fork"):
            return SourceDecision(SourceRejectionReason.fork)
        full_name = repository.get("full_name")
        if (
            not isinstance(full_name, str)
            or full_name.lower() not in self._allowed_repos
        ):
            return SourceDecision(SourceRejectionReason.not_in_allowlist)
        return SourceDecision()
