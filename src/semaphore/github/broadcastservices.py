"""Orchestration around GitHub repository sources for broadcast messages."""

import asyncio
import dataclasses
from collections.abc import AsyncGenerator, Iterable, Iterator, Sequence
from contextlib import AbstractAsyncContextManager, nullcontext
from http import HTTPStatus
from pathlib import Path
from typing import Any, Literal, Self

import httpx
from gidgethub import BadRequest, GitHubException, RateLimitExceeded
from gidgethub.httpx import GitHubAPI
from gidgethub.sansio import Event, accept_format
from structlog.stdlib import BoundLogger

from ..broadcast.markdown import BroadcastMarkdown
from ..broadcast.models import BroadcastMessage
from ..broadcast.repository import BroadcastMessageRepository
from ..config import config
from .client import (
    create_github_client,
    create_github_installation_client,
    get_app_jwt,
)
from .repolocks import RepoLocks
from .sourcepolicy import BroadcastSourcePolicy

BROADCASTS_DIR = "broadcasts"
"""Directory relative to a GitHub repository's root that contains broadcast
message files.
"""

BOOTSTRAP_RETRY_INITIAL_DELAY = 10.0
"""Seconds to wait before the first attempt to load again the installations
and repositories that failed at startup (see `retry_bootstrap_failures`).
The wait doubles after each attempt in which something fails, up to
`BOOTSTRAP_RETRY_MAX_DELAY`.
"""

BOOTSTRAP_RETRY_MAX_DELAY = 300.0
"""Most seconds to wait between attempts to load again the installations and
repositories that failed at startup.
"""

_ID_PREFIX = "github.com/"
"""Prefix of the identifier of a message sourced from GitHub."""

_ID_REF_SEPARATOR = "/?ref="
"""Separator between the file path and the git ref in the identifier of a
message sourced from GitHub.
"""


@dataclasses.dataclass(frozen=True)
class GitHubMessageRef:
    """An identifier for referencing broadcast messages sourced from GitHub in
    the BroadcastMessageRepostory.
    """

    path: str
    """Posix file path in the repository of the message
    (``broadcasts/hello.md``).
    """

    repo_name: str
    """Repository name (e.g. ``phalanx``."""

    repo_owner: str
    """Repository owner (e.g. ``lsst-sqre``)."""

    ref: str
    """Git ref (e.g. ``refs/heads/main``)"""

    @classmethod
    def from_push_event(cls, *, path: str, event: Event) -> Self:
        return cls(
            path=path,
            repo_name=event.data["repository"]["name"],
            repo_owner=event.data["repository"]["owner"]["name"],
            ref=event.data["ref"],
        )

    @classmethod
    def from_id(cls, identifier: str) -> Self | None:
        """Parse a message identifier made by `as_id`.

        Parameters
        ----------
        identifier
            A BroadcastMessage.identifier attribute.

        Returns
        -------
        GitHubMessageRef or None
            The reference whose `as_id` is ``identifier``, or `None` if
            ``identifier`` does not identify a message sourced from GitHub.
        """
        if not identifier.startswith(_ID_PREFIX):
            return None
        parts = identifier.removeprefix(_ID_PREFIX).split("/", maxsplit=2)
        if len(parts) != 3:
            return None
        repo_owner, repo_name, rest = parts
        # A git ref cannot contain "?", so the last separator is the one
        # before the ref.
        path, separator, ref = rest.rpartition(_ID_REF_SEPARATOR)
        if not (repo_owner and repo_name and path and separator and ref):
            return None
        return cls(
            path=path, repo_name=repo_name, repo_owner=repo_owner, ref=ref
        )

    def as_id(self) -> str:
        """Convert to a string format for use as a BroadcastMessage.identifier
        attribute.
        """
        return (
            f"{_ID_PREFIX}{self.repo_owner}/{self.repo_name}/{self.path}"
            f"{_ID_REF_SEPARATOR}{self.ref}"
        )

    def as_dict(self) -> dict[str, str]:
        """Express the message as a dict, which is useful for establishing
        logging context.
        """
        return dataclasses.asdict(self)

    def is_in_repo(self, repo_full_name: str) -> bool:
        """Determine if the message is sourced from a GitHub repository.

        Parameters
        ----------
        repo_full_name
            Full name (``owner/repo``) of the repository, matched
            case-insensitively as GitHub matches repository names.
        """
        full_name = f"{self.repo_owner}/{self.repo_name}"
        return full_name.lower() == repo_full_name.lower()


def get_repo_message_refs(
    *, broadcast_repo: BroadcastMessageRepository, repo_full_name: str
) -> list[GitHubMessageRef]:
    """Get the references of the stored messages that are sourced from one
    GitHub repository, under any git ref.

    Parameters
    ----------
    broadcast_repo
        The broadcast message repository.
    repo_full_name
        Full name (``owner/repo``) of the repository, matched
        case-insensitively.

    Returns
    -------
    list of GitHubMessageRef
        References of the repository's messages. The `~GitHubMessageRef.as_id`
        of each is the identifier of a stored message.
    """
    message_refs = []
    for message in broadcast_repo.iter():
        message_ref = GitHubMessageRef.from_id(message.identifier)
        if message_ref is not None and message_ref.is_in_repo(repo_full_name):
            message_refs.append(message_ref)
    return message_refs


async def update_broadcast_repo_from_push_event(
    *,
    event: Event,
    broadcast_repo: BroadcastMessageRepository,
    github_client: GitHubAPI,
    repo_locks: RepoLocks,
    logger: BoundLogger,
) -> None:
    """Update messages in the repository based on a GitHub webhook for the
    push event to a repository's default branch.

    The pushed repository's messages are reconciled with the default branch
    as it is when the push is processed, rather than as the push left it:
    the branch's head commit is looked up once, and every file is read at
    that commit. So a push that GitHub redelivers, or that arrives after a
    later push, cannot revert a message or bring back a removed one. For a
    push that is still the branch's head, the head is the push's ``after``
    commit.

    Usually, only the broadcast files that the push's commits touched
    (added, modified or removed) are reconciled. A file that exists at the
    head is added or updated, and a file that does not is removed. A push
    that touches no broadcast file makes no request to GitHub.

    The commits cannot show every change in two cases, so the repository is
    reloaded in full instead (see `_reload_repo_broadcasts`):

    - A force push, whose commits do not list the files of the history it
      discarded.
    - A push to a ref other than the one that some of the repository's
      messages were loaded from, as after the default branch is changed or
      renamed. Message identifiers include the ref, so the messages from the
      old ref would otherwise never be updated or removed.

    The repository's lock is held throughout, so pushes for one repository
    are processed one at a time. If the head cannot be looked up, the push
    is ignored with an error log. Other GitHub errors on a file, unlike the
    404 of a missing file, are logged and leave the file's message as it is.

    Parameters
    ----------
    event
        The parsed event payload.
    broadcast_repo
        The broadcast message repository.
    github_client
        The GitHub API client, pre-authorized as an app installation.
    repo_locks
        The per-repository locks of broadcast sources.
    logger
        The logger instance
    """
    files_written, files_removed = _integrate_file_changes_in_commits(
        event.data["commits"]
    )

    logger.debug(
        "GitHub push event, got commits and computed files changed",
        commits=event.data["commits"],
        files_written=list(files_written),
        files_removed=list(files_removed),
    )

    paths = sorted(filter(is_broadcast_message, files_written | files_removed))
    repository = event.data["repository"]
    async with repo_locks.get(repository["full_name"]):
        # Decided under the lock, since another push for the repository may
        # change its stored messages.
        reload_reason = _get_reload_reason(
            event=event, broadcast_repo=broadcast_repo
        )
        if reload_reason is None and not paths:
            return

        try:
            head_sha = await get_branch_head_sha(
                github_client=github_client,
                repo_full_name=repository["full_name"],
                branch=repository["default_branch"],
            )
        except Exception as e:
            logger.exception(
                "Could not look up the default branch's head, ignoring the "
                "push",
                github_repo=repository["full_name"],
                branch=repository["default_branch"],
                error=str(e),
            )
            return
        logger.debug(
            "Reading broadcast files at the default branch's head",
            head_sha=head_sha,
            after_sha=event.data["after"],
        )

        if reload_reason is not None:
            logger.info(
                "Reloading all broadcasts of the repository",
                github_repo=repository["full_name"],
                reason=reload_reason,
                head_sha=head_sha,
            )
            await _reload_repo_broadcasts(
                event=event,
                head_sha=head_sha,
                broadcast_repo=broadcast_repo,
                github_client=github_client,
                logger=logger,
            )
            return

        for path in paths:
            await _reconcile_file_at_commit(
                file_path=path,
                message_ref=GitHubMessageRef.from_push_event(
                    path=path, event=event
                ),
                contents_url=repository["contents_url"],
                git_ref=head_sha,
                broadcast_repo=broadcast_repo,
                github_client=github_client,
                logger=logger,
            )


def _get_reload_reason(
    *, event: Event, broadcast_repo: BroadcastMessageRepository
) -> Literal["forced_push", "ref_changed"] | None:
    """Determine whether a push to a repository's default branch needs the
    repository's broadcasts to be reloaded in full, rather than only the
    files that its commits touched.

    Parameters
    ----------
    event
        The parsed event payload.
    broadcast_repo
        The broadcast message repository.

    Returns
    -------
    str or None
        Why the repository needs reloading (``forced_push`` or
        ``ref_changed``), or `None` if it does not.
    """
    if event.data.get("forced"):
        return "forced_push"
    message_refs = get_repo_message_refs(
        broadcast_repo=broadcast_repo,
        repo_full_name=event.data["repository"]["full_name"],
    )
    if any(m.ref != event.data["ref"] for m in message_refs):
        return "ref_changed"
    return None


async def _reload_repo_broadcasts(
    *,
    event: Event,
    head_sha: str,
    broadcast_repo: BroadcastMessageRepository,
    github_client: GitHubAPI,
    logger: BoundLogger,
) -> None:
    """Make the stored messages of a pushed repository match the broadcast
    files of its default branch at a commit.

    The repository's broadcasts directory is listed at the commit, and each
    broadcast file in it is reconciled with the commit (see
    `_reconcile_file_at_commit`). Then every other stored message of the
    repository is removed, which includes every message stored under a ref
    other than the pushed one. A file that GitHub fails to return (other
    than with a 404) keeps its message under the pushed ref, if it has one.

    If the directory does not exist at the commit, the repository has no
    broadcasts and all of its messages are removed. If GitHub fails to list
    the directory for another reason, the push is ignored with an error log
    and the stored messages are kept.

    Parameters
    ----------
    event
        The parsed event payload.
    head_sha
        Commit SHA of the default branch's head, to read the files at.
    broadcast_repo
        The broadcast message repository.
    github_client
        The GitHub API client, pre-authorized as an app installation.
    logger
        The logger instance
    """
    repository = event.data["repository"]
    try:
        paths = await _list_broadcast_paths(
            github_client=github_client,
            contents_url=repository["contents_url"],
            git_ref=head_sha,
        )
    except Exception as e:
        if _is_not_found(e):
            # The broadcasts directory does not exist at the commit.
            paths = []
        else:
            logger.exception(
                "Could not list the repository's broadcasts, ignoring the "
                "push",
                github_repo=repository["full_name"],
                error=str(e),
            )
            return

    listed_ids = set()
    for path in paths:
        message_ref = GitHubMessageRef.from_push_event(path=path, event=event)
        listed_ids.add(message_ref.as_id())
        await _reconcile_file_at_commit(
            file_path=path,
            message_ref=message_ref,
            contents_url=repository["contents_url"],
            git_ref=head_sha,
            broadcast_repo=broadcast_repo,
            github_client=github_client,
            logger=logger,
        )

    for message_ref in get_repo_message_refs(
        broadcast_repo=broadcast_repo, repo_full_name=repository["full_name"]
    ):
        message_id = message_ref.as_id()
        if message_id not in listed_ids:
            broadcast_repo.remove(message_id)
            logger.debug(
                "Removed message that is not in the default branch",
                message_id=message_id,
                git_ref=head_sha,
            )


async def _reconcile_file_at_commit(
    *,
    file_path: str,
    message_ref: GitHubMessageRef,
    contents_url: str,
    git_ref: str,
    broadcast_repo: BroadcastMessageRepository,
    github_client: GitHubAPI,
    logger: BoundLogger,
) -> None:
    """Make a broadcast message match its file as it is at a commit.

    If the file exists at the commit, its message is added or updated (see
    `add_file_to_repository`). If GitHub reports that the file does not
    exist (404), its message is removed. Any other error is logged and the
    stored message, if any, is left unchanged.

    Parameters
    ----------
    file_path
        Posix path of the message's file in GitHub.
    message_ref
        Reference for the message, which identifies it.
    contents_url
        The URI template for the repository's contents.
    git_ref
        The commit SHA to read the file at.
    broadcast_repo
        The broadcast message repository.
    github_client
        The GitHub API client, pre-authorized as an app installation.
    logger
        The logger instance
    """
    try:
        await add_file_to_repository(
            file_path=file_path,
            message_ref=message_ref,
            contents_url=contents_url,
            broadcast_repo=broadcast_repo,
            github_client=github_client,
            logger=logger,
            git_ref=git_ref,
        )
    except RateLimitExceeded as e:
        logger.exception(
            "GitHub rate limit exception",
            limit=e.rate_limit.limit,
            remaining=e.rate_limit.remaining,
            resets_at=e.rate_limit.reset_datetime,
        )
    except BadRequest as e:
        if e.status_code == HTTPStatus.NOT_FOUND:
            message_id = message_ref.as_id()
            broadcast_repo.remove(message_id)
            logger.debug(
                "Removed message whose file does not exist",
                message_id=message_id,
                git_ref=git_ref,
            )
        else:
            logger.exception("GitHub error", error=str(e))
    except GitHubException as e:
        logger.exception("GitHub error", error=str(e))
    except Exception as e:
        logger.exception("Unknown error", error=str(e))


async def get_branch_head_sha(
    *, github_client: GitHubAPI, repo_full_name: str, branch: str
) -> str:
    """Get the commit SHA that a branch of a GitHub repository points to.

    Parameters
    ----------
    github_client
        The GitHub API client, pre-authorized as an app installation that
        can see the repository.
    repo_full_name
        Full name (``owner/repo``) of the repository.
    branch
        Name of the branch, such as ``main``.

    Returns
    -------
    str
        The commit SHA of the branch's head.

    Raises
    ------
    GitHubException
        Raised if there is an issue with GitHub, including the branch not
        existing.
    """
    owner, repo = repo_full_name.split("/", maxsplit=1)
    git_ref = await github_client.getitem(
        # Branch names may contain slashes, which stay path separators.
        "/repos/{owner}/{repo}/git/ref/heads/{+branch}",
        url_vars={"owner": owner, "repo": repo, "branch": branch},
    )
    return git_ref["object"]["sha"]


def _integrate_file_changes_in_commits(
    commits: Sequence[dict[str, Any]],
) -> tuple[set[str], set[str]]:
    """Integrate the file changes from the sequence of individual commits
    to determine the overall set of added/modified and removed files.
    """
    files_written = set()  # new or modified content
    files_removed = set()  # deleted content
    for commit in commits:
        commit_added = set(commit["added"])
        commit_modified = set(commit["modified"])
        commit_removed = set(commit["removed"])

        # Update tally of modified files
        files_written |= commit_added | commit_modified

        # Update tally of removed files
        files_removed |= commit_removed

        # Added files are not longer removed
        files_removed -= commit_added

        # Deleted files are no longer written
        files_written -= commit_removed
    return files_written, files_removed


def is_broadcast_message(github_path: str) -> bool:
    """Determine if a path corresponds to a broadcast message file.

    Parameters
    ----------
    github_path
        Posix file path in a GitHub repository.

    Returns
    -------
    bool
        See implementation code for the current heuristics.
    """
    path = Path(github_path)

    # TODO(jsick): this function could be refactored into a class containing
    # configuration about the GitHub repo, such as where messages are hosted
    # if they aren't hosted in broadcasts/
    if str(path.parent) != BROADCASTS_DIR:
        return False

    if path.suffix != ".md":
        return False

    name = path.name.lower()
    return not (name.startswith(".") or name == "readme.md")


async def add_file_to_repository(
    *,
    file_path: str,
    message_ref: GitHubMessageRef,
    contents_url: str,
    broadcast_repo: BroadcastMessageRepository,
    github_client: GitHubAPI,
    logger: BoundLogger,
    git_ref: str | None = None,
) -> BroadcastMessage | None:
    """Add/update a broadcast message in the repository from a file in a GitHub
    repository.

    If the file is not relevant to this Phalanx environment, it is not added,
    and a message already stored under the same identifier is removed (the
    file may have been retargeted away from this environment).

    Parameters
    ----------
    file_path
        Posix path of the message's file in GitHub.
    message_ref
        Reference for the message. Its git ref identifies the message; it
        does not select the version of the file that is read (see
        ``git_ref``).
    contents_url
        The URI template for the repository's contents (the template
        includs a ``path`` variable).
    broadcast_repo
        The broadcast message repository.
    github_client
        The GitHub API client, pre-authorized as an app installation.
    logger
        The logger instance
    git_ref
        The git ref (commit SHA, branch, or tag) to read the file at. If
        `None`, the file is read from the repository's default branch as it
        is at the time of the request.

    Returns
    -------
    BroadcastMessage or None
        The message added to the repository, or `None` if the message is not
        relevant to this Phalanx environment and was skipped (or removed).

    Raises
    ------
    GitHubException
        Raised if there is an issue with GitHub. The sub-class
        RateLimitExceeded indicates if the client has exceeded its rate limit.
    """
    message_id = message_ref.as_id()
    logger = logger.bind(message_id=message_id, git_ref=git_ref)

    markdown_text = await github_client.getitem(
        # The contents URL template has no variable for the ref, which the
        # contents API takes as a query parameter. A ref of None is omitted
        # from the expanded URL.
        contents_url + "{?ref}",
        url_vars={"path": file_path, "ref": git_ref},
        accept=accept_format(media="raw", json=False),
    )
    logger.debug(
        "Downloaded broadcast from GitHub",
        github_ratelimit_remaining=(
            github_client.rate_limit.remaining
            if github_client.rate_limit is not None
            else "Unknown"
        ),
    )
    broadcast_markdown = BroadcastMarkdown(
        text=markdown_text,
        identifier=message_id,
    )
    if not broadcast_markdown.is_relevant_to_env(config.phalanx_env):
        logger.debug(
            "Skipping broadcast message from GitHub because it is "
            "irrelevant to this Phalanx environment."
        )
        # The file may have been retargeted away from this environment, so
        # a version stored from before the change must stop being served.
        if message_id in broadcast_repo:
            broadcast_repo.remove(message_id)
            logger.info(
                "Removed broadcast message that is no longer relevant to "
                "this Phalanx environment"
            )
        return None

    broadcast_message = broadcast_markdown.to_broadcast()
    broadcast_repo.add(broadcast_message)
    logger.debug(
        "Added broadcast to the repository",
    )
    return broadcast_message


@dataclasses.dataclass(frozen=True)
class FailedRepo:
    """A repository whose broadcasts GitHub failed to return, which must be
    loaded again.
    """

    installation_id: str
    """ID of the GitHub App installation that can see the repository."""

    github_repo: dict[str, Any]
    """The repository resource from the GitHub v3 API."""

    @property
    def full_name(self) -> str:
        """Full name (``owner/repo``) of the repository."""
        return self.github_repo["full_name"]


@dataclasses.dataclass(frozen=True)
class FailedInstallation:
    """A GitHub App installation that GitHub failed to authorize, or whose
    repositories GitHub failed to list, which must be loaded again.
    """

    installation_id: str
    """ID of the GitHub App installation."""

    account: str
    """Login of the user or organization that the installation is on."""


type BootstrapFailure = FailedRepo | FailedInstallation
"""What GitHub failed to return when broadcasts were loaded at startup, which
`retry_bootstrap_failures` loads again.
"""


async def bootstrap_broadcast_repo(
    *,
    http_client: httpx.AsyncClient,
    broadcast_repo: BroadcastMessageRepository,
    source_policy: BroadcastSourcePolicy,
    logger: BoundLogger,
) -> list[BootstrapFailure]:
    """Bootstrap data in the broadcast repo from GitHub repositories that the
    GitHub app has access to.

    Semaphore is a GitHub App that can be installed in specific repositories.
    An installation that is suspended, or that is on an account that owns no
    repository that ``source_policy`` allows, is skipped with a warning,
    before a token is requested for it. Of the repositories that the other
    installations can see, only those that ``source_policy`` allows are
    loaded; the others are skipped with a warning, before any of their
    contents are requested (see `_load_installation`).

    Neither content nor GitHub errors stop startup:

    - If GitHub fails to authorize an installation or to list its
      repositories, the installation is logged with a warning and returned,
      so that it can be loaded again with `retry_bootstrap_failures`. The
      other installations are still loaded.
    - In an allowed repository (see `_load_repo_broadcasts`), a broadcast
      file that is malformed is skipped with a warning, and a repository
      without a broadcasts directory has no broadcasts. A repository that
      GitHub fails to list, or one of whose files GitHub fails to return, is
      logged with a warning and returned, to be loaded again; the messages
      of its other files are kept.

    At the end, a summary of what was loaded is logged. Then a warning lists
    the allowed repositories that no installation can see, as when a name is
    misspelled or the app is not installed on the repository. The
    repositories of the accounts whose installations are returned are left
    out of it, since what those installations can see is not known yet.

    Parameters
    ----------
    http_client
        The httpx client.
    broadcast_repo
        The broadcast message repository.
    source_policy
        The policy for which repositories may publish broadcast messages.
    logger
        The logger instance.

    Returns
    -------
    list of BootstrapFailure
        The installations, and then the repositories, that GitHub failed to
        return, each in the order they were loaded. Empty if everything was
        loaded.
    """
    github_client = create_github_client(http_client=http_client)
    jwt = get_app_jwt()
    seen_repos: list[str] = []
    repo_count = 0
    message_count = 0
    skipped_file_count = 0
    failed_installations: list[FailedInstallation] = []
    failed_repos: list[FailedRepo] = []
    async for installation in iter_installations(
        github_client=github_client, jwt=jwt
    ):
        installation_id = str(installation["id"])
        account = _get_installation_account(installation)
        installation_logger = logger.bind(
            installation_id=installation_id, github_account=account
        )
        if installation.get("suspended_at"):
            installation_logger.warning(
                "Skipping a suspended GitHub App installation",
                suspended_at=installation["suspended_at"],
            )
            continue
        if account is None or not source_policy.allows_owner(account):
            installation_logger.warning(
                "Skipping a GitHub App installation on an account that owns "
                "no allowed repository"
            )
            continue
        try:
            result = await _load_installation(
                installation_id=installation_id,
                http_client=http_client,
                broadcast_repo=broadcast_repo,
                source_policy=source_policy,
                repo_locks=None,
                logger=installation_logger,
            )
        except Exception:
            installation_logger.warning(
                "Could not authorize a GitHub App installation or list its "
                "repositories, will retry",
                exc_info=True,
            )
            failed_installations.append(
                FailedInstallation(
                    installation_id=installation_id, account=account
                )
            )
            continue
        seen_repos.extend(result.seen_repos)
        repo_count += result.repo_count
        message_count += result.message_count
        skipped_file_count += result.skipped_file_count
        failed_repos.extend(result.failed_repos)

    logger.info(
        "Loaded broadcasts from GitHub",
        github_repos=source_policy.allowed_repos,
        repo_count=repo_count,
        failed_installation_count=len(failed_installations),
        failed_repo_count=len(failed_repos),
        message_count=message_count,
        skipped_file_count=skipped_file_count,
    )
    unseen_repos = _get_unseen_repos(
        source_policy=source_policy,
        seen_repos=seen_repos,
        unknown_accounts=[f.account for f in failed_installations],
    )
    if unseen_repos:
        logger.warning(
            "No GitHub App installation can see these allowed repositories",
            github_repos=unseen_repos,
        )
    return [*failed_installations, *failed_repos]


def _get_installation_account(installation: dict[str, Any]) -> str | None:
    """Get the login of the account that a GitHub App installation is on.

    Parameters
    ----------
    installation
        The installation resource from the GitHub v3 API.

    Returns
    -------
    str or None
        The ``account.login`` of the installation, or `None` if the
        installation has no account with a login.
    """
    account = installation.get("account")
    if not isinstance(account, dict):
        return None
    login = account.get("login")
    return login if isinstance(login, str) else None


def _get_unseen_repos(
    *,
    source_policy: BroadcastSourcePolicy,
    seen_repos: Iterable[str],
    unknown_accounts: Iterable[str],
) -> list[str]:
    """Get the allowed repositories that no GitHub App installation can see.

    Parameters
    ----------
    source_policy
        The policy for which repositories may publish broadcast messages.
    seen_repos
        Full names (``owner/repo``) of the repositories that the
        installations can see, allowed or not.
    unknown_accounts
        Logins of the accounts whose installations' repositories are not
        known, because GitHub failed to list them. Their allowed repositories
        are left out.

    Returns
    -------
    list of str
        Full names of the allowed repositories, as configured, that are not
        in ``seen_repos`` and are not owned by an account in
        ``unknown_accounts``. Names are compared case-insensitively.
    """
    seen = {name.lower() for name in seen_repos}
    unknown = {account.lower() for account in unknown_accounts}
    return [
        name
        for name in source_policy.allowed_repos
        if name.lower() not in seen
        and name.split("/", maxsplit=1)[0].lower() not in unknown
    ]


@dataclasses.dataclass
class _InstallationLoadResult:
    """The outcome of loading the broadcast messages in the repositories of
    one GitHub App installation.
    """

    seen_repos: list[str] = dataclasses.field(default_factory=list)
    """Full names of the repositories that the installation can see, allowed
    or not.
    """

    repo_count: int = 0
    """Number of allowed repositories that loaded, including those without
    broadcasts.
    """

    message_count: int = 0
    """Number of messages added to the broadcast message repository."""

    skipped_file_count: int = 0
    """Number of broadcast message files skipped because they could not be
    loaded.
    """

    failed_repos: list[FailedRepo] = dataclasses.field(default_factory=list)
    """The allowed repositories that GitHub failed to return, which must be
    loaded again.
    """


async def _load_installation(
    *,
    installation_id: str,
    http_client: httpx.AsyncClient,
    broadcast_repo: BroadcastMessageRepository,
    source_policy: BroadcastSourcePolicy,
    repo_locks: RepoLocks | None,
    logger: BoundLogger,
) -> _InstallationLoadResult:
    """Load the broadcast messages in the allowed repositories that a GitHub
    App installation can see.

    The installation's repositories are listed in full with a new client
    authorized as the installation. A repository that ``source_policy``
    rejects is skipped with a warning. Each allowed repository is then
    loaded (see `_load_repo_broadcasts`).

    Parameters
    ----------
    installation_id
        ID of the GitHub App installation.
    http_client
        The httpx client.
    broadcast_repo
        The broadcast message repository.
    source_policy
        The policy for which repositories may publish broadcast messages.
    repo_locks
        The per-repository locks of broadcast sources, to load each
        repository under its lock so that loading it does not interleave
        with a push to it. `None` to load without locks, as at startup,
        before any push is processed.
    logger
        The logger instance.

    Returns
    -------
    _InstallationLoadResult
        What was loaded from the installation's repositories, and which of
        them failed.

    Raises
    ------
    Exception
        Raised if the installation cannot be authorized or its repositories
        cannot be listed, as when GitHub fails or cannot be reached. None of
        the installation's repositories has been loaded then.
    """
    # A new client each time, since an installation's token expires after an
    # hour.
    github_client = await create_github_installation_client(
        http_client=http_client, installation_id=installation_id
    )
    # Listed in full before any repository is loaded, so that a failure on a
    # later page of the listing fails the installation before any of its
    # repositories is loaded.
    github_repos = [
        github_repo
        async for github_repo in iter_installation_repositories(
            github_client=github_client
        )
    ]
    result = _InstallationLoadResult()
    for github_repo in github_repos:
        full_name = github_repo.get("full_name")
        if isinstance(full_name, str):
            result.seen_repos.append(full_name)
        decision = source_policy.evaluate(github_repo)
        if not decision.allowed:
            logger.warning(
                "Skipping a repository that may not publish broadcasts",
                github_repo=full_name,
                reason=decision.reason,
            )
            continue
        repo_lock: AbstractAsyncContextManager[object] = (
            nullcontext()
            if repo_locks is None
            else repo_locks.get(github_repo["full_name"])
        )
        async with repo_lock:
            repo_result = await _load_repo_broadcasts(
                github_repo=github_repo,
                broadcast_repo=broadcast_repo,
                github_client=github_client,
                logger=logger,
            )
        result.message_count += repo_result.message_count
        result.skipped_file_count += repo_result.skipped_file_count
        if repo_result.failed:
            result.failed_repos.append(
                FailedRepo(
                    installation_id=installation_id, github_repo=github_repo
                )
            )
        else:
            result.repo_count += 1
    return result


async def retry_bootstrap_failures(
    *,
    failures: Sequence[BootstrapFailure],
    http_client: httpx.AsyncClient,
    broadcast_repo: BroadcastMessageRepository,
    source_policy: BroadcastSourcePolicy,
    repo_locks: RepoLocks,
    logger: BoundLogger,
) -> None:
    """Load again the installations and repositories that GitHub failed to
    return at startup, until each one loads.

    This is meant to run as a background task, so that startup does not
    wait for GitHub to recover. Each attempt waits first, for
    `BOOTSTRAP_RETRY_INITIAL_DELAY` seconds and then twice as long after
    each attempt, up to `BOOTSTRAP_RETRY_MAX_DELAY`. Then it loads each
    failure that is still pending, with a new client authorized as its
    installation, and each repository under the repository's lock, so that
    it does not interleave with a push to the repository:

    - A failed installation is loaded as at startup (see
      `_load_installation`): its repositories are listed and those that
      ``source_policy`` allows are loaded. If GitHub fails again to
      authorize it or to list its repositories, it is tried again in the
      next attempt. A repository of it that GitHub fails to return is tried
      again in the next attempt, as a failed repository.
    - A failed repository is loaded in full, as at startup (see
      `_load_repo_broadcasts`). If it fails again, including because its
      installation cannot be authorized, it is tried again in the next
      attempt.

    The function returns once everything has loaded.

    Parameters
    ----------
    failures
        What to load, as returned by `bootstrap_broadcast_repo`.
    http_client
        The httpx client.
    broadcast_repo
        The broadcast message repository.
    source_policy
        The policy for which repositories may publish broadcast messages.
    repo_locks
        The per-repository locks of broadcast sources.
    logger
        The logger instance.
    """
    pending = list(failures)
    delays = _iter_retry_delays()
    attempt = 0
    while pending:
        await asyncio.sleep(next(delays))
        attempt += 1
        still_failed: list[BootstrapFailure] = []
        for failure in pending:
            if isinstance(failure, FailedInstallation):
                still_failed.extend(
                    await _retry_installation(
                        failed_installation=failure,
                        attempt=attempt,
                        http_client=http_client,
                        broadcast_repo=broadcast_repo,
                        source_policy=source_policy,
                        repo_locks=repo_locks,
                        logger=logger,
                    )
                )
                continue
            loaded = await _retry_repo(
                failed_repo=failure,
                attempt=attempt,
                http_client=http_client,
                broadcast_repo=broadcast_repo,
                repo_locks=repo_locks,
                logger=logger,
            )
            if not loaded:
                still_failed.append(failure)
        pending = still_failed


def _iter_retry_delays() -> Iterator[float]:
    """Iterate over the seconds to wait before each attempt of
    `retry_bootstrap_failures`: an exponential backoff that is capped.
    """
    delay = BOOTSTRAP_RETRY_INITIAL_DELAY
    while True:
        yield delay
        delay = min(delay * 2, BOOTSTRAP_RETRY_MAX_DELAY)


async def _retry_installation(
    *,
    failed_installation: FailedInstallation,
    attempt: int,
    http_client: httpx.AsyncClient,
    broadcast_repo: BroadcastMessageRepository,
    source_policy: BroadcastSourcePolicy,
    repo_locks: RepoLocks,
    logger: BoundLogger,
) -> list[BootstrapFailure]:
    """Try once to load a GitHub App installation that GitHub failed to
    authorize, or whose repositories GitHub failed to list.

    Parameters
    ----------
    failed_installation
        The installation to load.
    attempt
        Number of the attempt of `retry_bootstrap_failures`, starting at 1.
    http_client
        The httpx client.
    broadcast_repo
        The broadcast message repository.
    source_policy
        The policy for which repositories may publish broadcast messages.
    repo_locks
        The per-repository locks of broadcast sources.
    logger
        The logger instance.

    Returns
    -------
    list of BootstrapFailure
        What is still to be loaded: the installation, if it failed again, or
        else its repositories that GitHub failed to return.
    """
    logger = logger.bind(
        installation_id=failed_installation.installation_id,
        github_account=failed_installation.account,
        attempt=attempt,
    )
    logger.info("Retrying loading broadcasts from a GitHub App installation")
    try:
        result = await _load_installation(
            installation_id=failed_installation.installation_id,
            http_client=http_client,
            broadcast_repo=broadcast_repo,
            source_policy=source_policy,
            repo_locks=repo_locks,
            logger=logger,
        )
    except Exception:
        logger.warning(
            "Could not authorize a GitHub App installation or list its "
            "repositories, will retry",
            exc_info=True,
        )
        return [failed_installation]
    logger.info(
        "Loaded broadcasts from a GitHub App installation after retrying",
        repo_count=result.repo_count,
        failed_repo_count=len(result.failed_repos),
        message_count=result.message_count,
        skipped_file_count=result.skipped_file_count,
    )
    return list(result.failed_repos)


async def _retry_repo(
    *,
    failed_repo: FailedRepo,
    attempt: int,
    http_client: httpx.AsyncClient,
    broadcast_repo: BroadcastMessageRepository,
    repo_locks: RepoLocks,
    logger: BoundLogger,
) -> bool:
    """Try once to load a repository that GitHub failed to return.

    Parameters
    ----------
    failed_repo
        The repository to load.
    attempt
        Number of the attempt of `retry_bootstrap_failures`, starting at 1.
    http_client
        The httpx client.
    broadcast_repo
        The broadcast message repository.
    repo_locks
        The per-repository locks of broadcast sources.
    logger
        The logger instance.

    Returns
    -------
    bool
        Whether the repository loaded, rather than failing again.
    """
    logger = logger.bind(github_repo=failed_repo.full_name, attempt=attempt)
    logger.info("Retrying loading broadcasts from a repository")
    try:
        # A new client for each attempt, since an installation's token
        # expires after an hour.
        github_client = await create_github_installation_client(
            http_client=http_client,
            installation_id=failed_repo.installation_id,
        )
        async with repo_locks.get(failed_repo.full_name):
            result = await _load_repo_broadcasts(
                github_repo=failed_repo.github_repo,
                broadcast_repo=broadcast_repo,
                github_client=github_client,
                logger=logger,
            )
    except Exception:
        logger.warning(
            "Could not load the repository's broadcasts, will retry",
            exc_info=True,
        )
        return False
    if result.failed:
        return False
    logger.info(
        "Loaded broadcasts from a repository after retrying",
        message_count=result.message_count,
        skipped_file_count=result.skipped_file_count,
    )
    return True


@dataclasses.dataclass
class _RepoLoadResult:
    """The outcome of loading the broadcast messages in one repository."""

    message_count: int = 0
    """Number of messages added to the broadcast message repository."""

    skipped_file_count: int = 0
    """Number of broadcast message files skipped because they could not be
    loaded.
    """

    failed: bool = False
    """Whether GitHub failed to list the repository's broadcasts or to return
    any of its broadcast files, so that the repository must be loaded again.
    """


async def _load_repo_broadcasts(
    *,
    github_repo: dict[str, Any],
    broadcast_repo: BroadcastMessageRepository,
    github_client: GitHubAPI,
    logger: BoundLogger,
) -> _RepoLoadResult:
    """Load the broadcast messages in a GitHub repository's default branch.

    Errors are handled by whether loading the repository again could fix
    them (see `_is_github_failure`):

    - A file whose content cannot be loaded, because it is malformed or
      because GitHub reports that it does not exist (404), is skipped with
      a warning. Loading the repository again would skip it again.
    - If the repository has no broadcasts directory (GitHub returns a 404
      for it), the repository has no broadcasts. Git does not store empty
      directories, so this is how a repository without broadcasts looks.
    - If GitHub fails to list the directory, or to return a file, for any
      other reason, the repository is marked as failed with a warning. The
      other files are still loaded.

    Parameters
    ----------
    github_repo
        The repository resource from the GitHub v3 API.
    broadcast_repo
        The broadcast message repository.
    github_client
        The GitHub API client, pre-authorized as an app installation that
        can see the repository.
    logger
        The logger instance.

    Returns
    -------
    _RepoLoadResult
        What was loaded from the repository, and whether it failed.
    """
    logger = logger.bind(github_repo=github_repo["full_name"])
    logger.debug("Loading broadcasts from repository")
    try:
        paths = await _list_broadcast_paths(
            github_client=github_client,
            contents_url=github_repo["contents_url"],
        )
    except Exception as e:
        if _is_not_found(e):
            logger.debug("Repository has no broadcasts directory")
            return _RepoLoadResult()
        logger.warning(
            "Could not list the repository's broadcasts, will retry",
            exc_info=True,
        )
        return _RepoLoadResult(failed=True)

    result = _RepoLoadResult()
    for path in paths:
        message_ref = GitHubMessageRef(
            path=path,
            repo_name=github_repo["name"],
            repo_owner=github_repo["owner"]["login"],
            ref=f"refs/heads/{github_repo['default_branch']}",
        )
        try:
            message = await add_file_to_repository(
                file_path=path,
                message_ref=message_ref,
                contents_url=github_repo["contents_url"],
                broadcast_repo=broadcast_repo,
                github_client=github_client,
                logger=logger,
            )
        except Exception as e:
            if _is_github_failure(e):
                logger.warning(
                    "Could not read a broadcast file from GitHub, will retry "
                    "the repository",
                    path=path,
                    exc_info=True,
                )
                result.failed = True
            else:
                logger.warning(
                    "Skipping a broadcast file that could not be loaded",
                    path=path,
                    exc_info=True,
                )
                result.skipped_file_count += 1
            continue
        if message is not None:
            result.message_count += 1
    return result


def _is_github_failure(error: Exception) -> bool:
    """Determine if an error reading from GitHub is a failure of GitHub or
    of the connection to it, so that reading again later may succeed, rather
    than a problem with the content that was read.

    Failures are the GitHub errors, such as a 5xx response
    (`~gidgethub.GitHubBroken`), an exceeded rate limit, or a 401 or 403
    response, and the errors connecting to GitHub, including timeouts. A
    404 response is not a failure: it means that the file or directory does
    not exist.
    """
    if isinstance(error, httpx.TransportError):
        return True
    return isinstance(error, GitHubException) and not _is_not_found(error)


def _is_not_found(error: Exception) -> bool:
    """Determine if an error is GitHub's 404 response for a file or
    directory that does not exist.
    """
    return (
        isinstance(error, BadRequest)
        and error.status_code == HTTPStatus.NOT_FOUND
    )


async def _list_broadcast_paths(
    *,
    github_client: GitHubAPI,
    contents_url: str,
    git_ref: str | None = None,
) -> list[str]:
    """List the paths of the broadcast message files in a GitHub
    repository.

    Parameters
    ----------
    github_client
        The GitHub API client, pre-authorized as an app installation that
        can see the repository.
    contents_url
        The URI template for the repository's contents.
    git_ref
        The git ref (commit SHA, branch, or tag) to list the files at. If
        `None`, the files are listed from the repository's default branch as
        it is at the time of the request.

    Returns
    -------
    list of str
        Posix paths of the broadcast message files, in the order GitHub lists
        them.

    Raises
    ------
    GitHubException
        Raised if there is an issue with GitHub, including a 404 (as
        `~gidgethub.BadRequest`) if the broadcasts directory does not exist.
    """
    return [
        file_obj["path"]
        async for file_obj in iter_repo_dir_contents(
            github_client=github_client,
            contents_url=contents_url,
            directory_path=BROADCASTS_DIR,
            git_ref=git_ref,
        )
        if is_broadcast_message(file_obj["path"])
    ]


async def iter_installations(
    *, github_client: GitHubAPI, jwt: str
) -> AsyncGenerator[dict[str, Any]]:
    """Iterate over the GitHub app installations.

    Parameters
    ----------
    github_client
        The GitHub client.
    jwt
        The JWT for the GitHub application. See `get_app_jwt`.

    Yields
    ------
    dict
        The installation resource from the GitHub v3 API. See
        https://docs.github.com/en/rest/reference/apps#list-installations-for-the-authenticated-app

        The app's installation id in the ``id`` key at the root.
    """
    url = "/app/installations"
    async for inst in github_client.getiter(url, jwt=jwt):
        yield inst


async def iter_installation_repositories(
    *,
    github_client: GitHubAPI,
) -> AsyncGenerator[dict[str, Any]]:
    """Iterate over repositories that the GitHub app installation has access
    to, across every page of the listing.

    Parameters
    ----------
    github_client
        The GitHub client that is pre-authorized with an installation ID,
        see `create_github_installation_client`.

    Yields
    ------
    dict
        The repository resource from the GitHub v3 API. See
        https://docs.github.com/en/rest/reference/apps#list-repositories-accessible-to-the-app-installation
    """
    # Each page is an object that lists its repositories under the
    # "repositories" key rather than a bare list.
    async for repo in github_client.getiter(
        "/installation/repositories", iterable_key="repositories"
    ):
        yield repo


async def iter_repo_dir_contents(
    github_client: GitHubAPI,
    contents_url: str,
    directory_path: str,
    git_ref: str | None = None,
) -> AsyncGenerator[dict[str, Any]]:
    """Iterate over content objects in a repository in a GitHub repository.

    Parameters
    ----------
    github_client
        The GitHub client that is pre-authorized with an installation ID,
        see `create_github_installation_client`.
    contents_url
        The contents URL for the repository, which is a templated URI,
        Example:
        ``https://api.github.com/repos/lsst-sqre/semaphore-demo/contents/{+path}``
    directory_path
        The directory path to iterate in.
    git_ref
        The git ref (commit SHA, branch, or tag) to list the directory at. If
        `None`, the directory is listed from the repository's default branch
        as it is at the time of the request.

    Yields
    ------
    dict
        The repository content resource. See
        https://docs.github.com/en/rest/reference/repos#contents
    """
    if directory_path.endswith("/"):
        directory_path = directory_path.rstrip("/")

    async for file_obj in github_client.getiter(
        # As in add_file_to_repository, the ref is a query parameter, which
        # is omitted from the expanded URL if it is None.
        contents_url + "{?ref}",
        url_vars={"path": directory_path, "ref": git_ref},
    ):
        yield file_obj
