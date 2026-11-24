"""Orchestration around GitHub repository sources for broadcast messages."""

import asyncio
import dataclasses
from collections.abc import AsyncGenerator, Iterable, Iterator, Mapping
from enum import Enum, auto
from http import HTTPStatus
from pathlib import Path
from typing import Any, Literal, Self

import httpx
from gidgethub import BadRequest, GitHubException
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
from .reloadqueue import (
    QueuedInstallation,
    QueuedInstallationListing,
    QueuedRepo,
    ReloadQueue,
)
from .repolocks import RepoLocks
from .sourcepolicy import BroadcastSourcePolicy

BROADCASTS_DIR = "broadcasts"
"""Directory relative to a GitHub repository's root that contains broadcast
message files.
"""

BOOTSTRAP_RETRY_INITIAL_DELAY = 10.0
"""Seconds that the reload worker waits before its first attempt to reload
the repositories and installations in the reload queue (see
`run_reload_worker`). The wait doubles after each attempt that leaves
something in the queue, up to `BOOTSTRAP_RETRY_MAX_DELAY`.
"""

BOOTSTRAP_RETRY_MAX_DELAY = 300.0
"""Most seconds that the reload worker waits between attempts to reload the
repositories and installations in the reload queue.
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
    def from_repository(
        cls, *, path: str, repository: Mapping[str, Any], ref: str
    ) -> Self:
        """Make the reference of a message in a GitHub repository.

        Parameters
        ----------
        path
            Posix file path of the message in the repository.
        repository
            The repository resource, such as the ``repository`` object of a
            push webhook payload or a repository from the GitHub v3 API. Its
            ``full_name`` (``owner/repo``) is used.
        ref
            Git ref that the message is loaded from (e.g.
            ``refs/heads/main``).
        """
        owner, name = repository["full_name"].split("/", maxsplit=1)
        return cls(path=path, repo_name=name, repo_owner=owner, ref=ref)

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


class _FileOutcome(Enum):
    """The outcome of reconciling a broadcast message with its file (see
    `_reconcile_file_at_commit`).
    """

    loaded = auto()
    """The file's message was added or updated."""

    absent = auto()
    """The file does not exist, or is not relevant to this Phalanx
    environment, so it has no message.
    """

    skipped = auto()
    """The file's content could not be loaded, as when it is malformed. Its
    stored message, if any, was left unchanged.
    """

    failed = auto()
    """GitHub failed to return the file (see `_is_github_failure`), so its
    repository must be reloaded. Its stored message, if any, was left
    unchanged.
    """


async def update_broadcast_repo_from_push_event(
    *,
    event: Event,
    installation_id: str,
    broadcast_repo: BroadcastMessageRepository,
    github_client: GitHubAPI,
    repo_locks: RepoLocks,
    reload_queue: ReloadQueue,
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
    are processed one at a time.

    GitHub does not redeliver a webhook on its own, so a push that GitHub
    fails is not dropped. If GitHub fails to return the head, to list the
    broadcasts or to return a file (other than with the 404 of a missing
    file), the failure is logged with a warning, the messages that could not
    be read are left as they are, and the repository is added to
    ``reload_queue``. The reload worker then reloads the repository in full
    once GitHub recovers (see `run_reload_worker`).

    Parameters
    ----------
    event
        The parsed event payload.
    installation_id
        ID of the GitHub App installation that sent the event, which can see
        the repository.
    broadcast_repo
        The broadcast message repository.
    github_client
        The GitHub API client, pre-authorized as an app installation.
    repo_locks
        The per-repository locks of broadcast sources.
    reload_queue
        The queue of repositories whose broadcasts must be reloaded.
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
    ref = event.data["ref"]
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
        except Exception:
            logger.warning(
                "Could not look up the default branch's head, will reload the "
                "repository",
                github_repo=repository["full_name"],
                branch=repository["default_branch"],
                exc_info=True,
            )
            failed = True
        else:
            logger.debug(
                "Reading broadcast files at the default branch's head",
                head_sha=head_sha,
                after_sha=event.data["after"],
            )
            failed = await _reconcile_push_at_commit(
                repository=repository,
                ref=ref,
                head_sha=head_sha,
                paths=paths,
                reload_reason=reload_reason,
                broadcast_repo=broadcast_repo,
                github_client=github_client,
                logger=logger,
            )

        if failed:
            # Queued under the lock, as the reload worker removes the
            # repository from the queue under the lock once it reloads it.
            reload_queue.add_repo(
                installation_id=installation_id,
                repo_full_name=repository["full_name"],
            )


async def _reconcile_push_at_commit(
    *,
    repository: Mapping[str, Any],
    ref: str,
    head_sha: str,
    paths: Iterable[str],
    reload_reason: str | None,
    broadcast_repo: BroadcastMessageRepository,
    github_client: GitHubAPI,
    logger: BoundLogger,
) -> bool:
    """Reconcile the messages of a pushed repository with its default
    branch's head, as `update_broadcast_repo_from_push_event` describes.

    Parameters
    ----------
    repository
        The ``repository`` object of the push webhook payload.
    ref
        The pushed ref, which is the default branch's.
    head_sha
        Commit SHA of the default branch's head, to read the files at.
    paths
        Paths of the broadcast files that the push's commits touched.
    reload_reason
        Why the repository is reloaded in full, or `None` to reconcile only
        ``paths``.
    broadcast_repo
        The broadcast message repository.
    github_client
        The GitHub API client, pre-authorized as an app installation.
    logger
        The logger instance

    Returns
    -------
    bool
        Whether GitHub failed, so that the repository must be reloaded.
    """
    if reload_reason is not None:
        logger.info(
            "Reloading all broadcasts of the repository",
            github_repo=repository["full_name"],
            reason=reload_reason,
            head_sha=head_sha,
        )
        result = await _reload_repo_broadcasts(
            repository=repository,
            ref=ref,
            head_sha=head_sha,
            broadcast_repo=broadcast_repo,
            github_client=github_client,
            logger=logger,
        )
        return result.failed

    failed = False
    for path in paths:
        outcome = await _reconcile_file_at_commit(
            file_path=path,
            message_ref=GitHubMessageRef.from_repository(
                path=path, repository=repository, ref=ref
            ),
            contents_url=repository["contents_url"],
            git_ref=head_sha,
            broadcast_repo=broadcast_repo,
            github_client=github_client,
            logger=logger,
        )
        failed = failed or outcome is _FileOutcome.failed
    return failed


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
    repository: Mapping[str, Any],
    ref: str,
    head_sha: str,
    broadcast_repo: BroadcastMessageRepository,
    github_client: GitHubAPI,
    logger: BoundLogger,
) -> _RepoLoadResult:
    """Make the stored messages of a repository match the broadcast files of
    its default branch at a commit.

    This is shared by a push that needs a full reload and by the reload
    worker. The caller holds the repository's lock.

    The repository's broadcasts directory is listed at the commit, and each
    broadcast file in it is reconciled with the commit, as a message under
    ``ref`` (see `_reconcile_file_at_commit`). Then every other stored
    message of the repository is removed, which includes every message
    stored under a ref other than ``ref``, except one whose file is listed
    but GitHub fails to return (other than with a 404). Such a file keeps
    whatever is served for it, under any ref, until the repository is
    reloaded, and the reload is marked as failed.

    If the directory does not exist at the commit, the repository has no
    broadcasts and all of its messages are removed. If GitHub fails to list
    the directory for another reason, the failure is logged with a warning,
    the stored messages are kept and the reload is marked as failed.

    Parameters
    ----------
    repository
        The repository resource, such as the ``repository`` object of a push
        webhook payload or a repository from the GitHub v3 API. Its
        ``full_name`` and ``contents_url`` are used.
    ref
        Git ref of the default branch (``refs/heads/<branch>``), which
        identifies the messages.
    head_sha
        Commit SHA of the default branch's head, to read the files at.
    broadcast_repo
        The broadcast message repository.
    github_client
        The GitHub API client, pre-authorized as an app installation.
    logger
        The logger instance

    Returns
    -------
    _RepoLoadResult
        What was loaded from the repository, and whether GitHub failed, so
        that the repository must be reloaded again.
    """
    full_name = repository["full_name"]
    logger = logger.bind(github_repo=full_name)
    try:
        paths = await _list_broadcast_paths(
            github_client=github_client,
            contents_url=repository["contents_url"],
            git_ref=head_sha,
        )
    except Exception as e:
        if not _is_not_found(e):
            logger.warning(
                "Could not list the repository's broadcasts, will retry",
                exc_info=True,
            )
            return _RepoLoadResult(failed=True)
        # The broadcasts directory does not exist at the commit.
        paths = []

    result = _RepoLoadResult()
    listed_ids = set()
    failed_paths = set()
    for path in paths:
        message_ref = GitHubMessageRef.from_repository(
            path=path, repository=repository, ref=ref
        )
        listed_ids.add(message_ref.as_id())
        outcome = await _reconcile_file_at_commit(
            file_path=path,
            message_ref=message_ref,
            contents_url=repository["contents_url"],
            git_ref=head_sha,
            broadcast_repo=broadcast_repo,
            github_client=github_client,
            logger=logger,
        )
        match outcome:
            case _FileOutcome.loaded:
                result.message_count += 1
            case _FileOutcome.absent:
                pass
            case _FileOutcome.skipped:
                result.skipped_file_count += 1
            case _FileOutcome.failed:
                result.failed = True
                failed_paths.add(path)

    for message_ref in get_repo_message_refs(
        broadcast_repo=broadcast_repo, repo_full_name=full_name
    ):
        message_id = message_ref.as_id()
        if message_id in listed_ids or message_ref.path in failed_paths:
            continue
        broadcast_repo.remove(message_id)
        logger.debug(
            "Removed message that is not in the default branch",
            message_id=message_id,
            git_ref=head_sha,
        )
    return result


async def _reconcile_file_at_commit(
    *,
    file_path: str,
    message_ref: GitHubMessageRef,
    contents_url: str,
    git_ref: str,
    broadcast_repo: BroadcastMessageRepository,
    github_client: GitHubAPI,
    logger: BoundLogger,
) -> _FileOutcome:
    """Make a broadcast message match its file as it is at a commit.

    If the file exists at the commit, its message is added or updated (see
    `add_file_to_repository`). If GitHub reports that the file does not
    exist (404), its message is removed. Otherwise, the stored message, if
    any, is left unchanged and a warning is logged, for a GitHub failure
    (see `_is_github_failure`) as for any other error, such as malformed
    content.

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

    Returns
    -------
    _FileOutcome
        What became of the file's message.
    """
    try:
        message = await add_file_to_repository(
            file_path=file_path,
            message_ref=message_ref,
            contents_url=contents_url,
            broadcast_repo=broadcast_repo,
            github_client=github_client,
            logger=logger,
            git_ref=git_ref,
        )
    except Exception as e:
        if _is_not_found(e):
            message_id = message_ref.as_id()
            broadcast_repo.remove(message_id)
            logger.debug(
                "Removed message whose file does not exist",
                message_id=message_id,
                git_ref=git_ref,
            )
            return _FileOutcome.absent
        if _is_github_failure(e):
            logger.warning(
                "Could not read a broadcast file from GitHub, will retry the "
                "repository",
                path=file_path,
                git_ref=git_ref,
                exc_info=True,
            )
            return _FileOutcome.failed
        logger.warning(
            "Skipping a broadcast file that could not be loaded",
            path=file_path,
            git_ref=git_ref,
            exc_info=True,
        )
        return _FileOutcome.skipped
    return _FileOutcome.absent if message is None else _FileOutcome.loaded


async def get_repository(
    *, github_client: GitHubAPI, repo_full_name: str
) -> dict[str, Any]:
    """Get a GitHub repository's resource, as it is now.

    Parameters
    ----------
    github_client
        The GitHub API client, pre-authorized as an app installation that
        can see the repository.
    repo_full_name
        Full name (``owner/repo``) of the repository.

    Returns
    -------
    dict
        The repository resource from the GitHub v3 API, with the
        repository's current ``default_branch``.

    Raises
    ------
    GitHubException
        Raised if there is an issue with GitHub, including a 404 (as
        `~gidgethub.BadRequest`) if the repository does not exist or the
        installation cannot see it.
    """
    owner, repo = repo_full_name.split("/", maxsplit=1)
    return await github_client.getitem(
        "/repos/{owner}/{repo}", url_vars={"owner": owner, "repo": repo}
    )


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
    commits: Iterable[dict[str, Any]],
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


async def bootstrap_broadcast_repo(
    *,
    http_client: httpx.AsyncClient,
    broadcast_repo: BroadcastMessageRepository,
    source_policy: BroadcastSourcePolicy,
    reload_queue: ReloadQueue,
    logger: BoundLogger,
) -> None:
    """Bootstrap data in the broadcast repo from GitHub repositories that the
    GitHub app has access to.

    Semaphore is a GitHub App that can be installed in specific repositories.
    The app's installations are listed page by page, and each page's
    installations are loaded before the next page is requested. An
    installation that is suspended, or that is on an account that owns no
    repository that ``source_policy`` allows, is skipped with a warning,
    before a token is requested for it (see `_get_account_to_load`). Of the
    repositories that the other installations can see, only those that
    ``source_policy`` allows are loaded; the others are skipped with a
    warning, before any of their contents are requested (see
    `_load_installation`).

    Neither content nor GitHub errors stop startup:

    - If GitHub fails to list the installations, on the first page or a
      later one, the failure is logged with a warning and the listing is
      added to ``reload_queue``, so that the reload worker lists the
      installations again and loads each one once GitHub recovers (see
      `run_reload_worker`). The installations listed before the failure
      keep what they loaded.
    - If GitHub fails to authorize an installation or to list its
      repositories, the installation is logged with a warning and added to
      ``reload_queue``, so that the reload worker loads its repositories
      once GitHub recovers. The other installations are still loaded.
    - In an allowed repository (see `_load_repo_broadcasts`), a broadcast
      file that is malformed is skipped with a warning, and a repository
      without a broadcasts directory has no broadcasts. A repository that
      GitHub fails to list, or one of whose files GitHub fails to return, is
      logged with a warning and added to ``reload_queue``, to be reloaded in
      full; the messages of its other files are kept.

    At the end, a summary of what was loaded is logged. Then a warning lists
    the allowed repositories that no installation can see, as when a name is
    misspelled or the app is not installed on the repository (see
    `_warn_about_unseen_repos`). The repositories of the accounts whose
    installations were queued are left out of it, since what those
    installations can see is not known yet. If GitHub failed to list the
    installations, the warning is not logged at all; the reload worker logs
    it once it lists them.

    Parameters
    ----------
    http_client
        The httpx client.
    broadcast_repo
        The broadcast message repository.
    source_policy
        The policy for which repositories may publish broadcast messages.
    reload_queue
        The queue of repositories and installations whose broadcasts must be
        reloaded, to add what GitHub fails to return to.
    logger
        The logger instance.
    """
    github_client = create_github_client(http_client=http_client)
    jwt = get_app_jwt()
    totals = _InstallationLoadResult()
    listed = True
    try:
        # Each page of installations is loaded before the next is requested.
        async for installation in iter_installations(
            github_client=github_client, jwt=jwt
        ):
            # This handles the installation's own errors, so what is caught
            # here is a failure to list the installations.
            result = await _bootstrap_installation(
                installation,
                http_client=http_client,
                broadcast_repo=broadcast_repo,
                source_policy=source_policy,
                reload_queue=reload_queue,
                logger=logger,
            )
            totals.add_installation_result(result)
    except Exception:
        logger.warning(
            "Could not list the GitHub App's installations, will retry",
            exc_info=True,
        )
        reload_queue.add_installation_listing()
        listed = False

    logger.info(
        "Loaded broadcasts from GitHub",
        github_repos=source_policy.allowed_repos,
        repo_count=totals.repo_count,
        failed_installation_count=len(totals.failed_accounts),
        failed_repo_count=totals.failed_repo_count,
        message_count=totals.message_count,
        skipped_file_count=totals.skipped_file_count,
    )
    # What the installations can see is not known until they are all listed,
    # which the reload worker then does.
    if listed:
        _warn_about_unseen_repos(
            totals, source_policy=source_policy, logger=logger
        )


async def _bootstrap_installation(
    installation: dict[str, Any],
    *,
    http_client: httpx.AsyncClient,
    broadcast_repo: BroadcastMessageRepository,
    source_policy: BroadcastSourcePolicy,
    reload_queue: ReloadQueue,
    logger: BoundLogger,
) -> _InstallationLoadResult:
    """Load, at startup, the broadcasts of a GitHub App installation that
    GitHub listed, unless it must be skipped.

    An installation that must be skipped (see `_get_account_to_load`) is
    skipped with a warning. Otherwise, its repositories are loaded (see
    `_load_installation`). If GitHub fails to authorize the installation or
    to list its repositories, the installation is logged with a warning and
    added to ``reload_queue``.

    Parameters
    ----------
    installation
        The installation resource from the GitHub v3 API.
    http_client
        The httpx client.
    broadcast_repo
        The broadcast message repository.
    source_policy
        The policy for which repositories may publish broadcast messages.
    reload_queue
        The queue of repositories and installations whose broadcasts must be
        reloaded.
    logger
        The logger instance.

    Returns
    -------
    _InstallationLoadResult
        What was loaded from the installation's repositories. It is empty
        for a skipped installation, and has the installation's account as a
        failed account for a queued one.
    """
    account = _get_account_to_load(
        installation, source_policy=source_policy, logger=logger
    )
    if account is None:
        return _InstallationLoadResult()
    installation_id = str(installation["id"])
    logger = logger.bind(
        installation_id=installation_id, github_account=account
    )
    try:
        return await _load_installation(
            installation_id=installation_id,
            http_client=http_client,
            broadcast_repo=broadcast_repo,
            source_policy=source_policy,
            reload_queue=reload_queue,
            logger=logger,
        )
    except Exception:
        logger.warning(
            "Could not authorize a GitHub App installation or list its "
            "repositories, will retry",
            exc_info=True,
        )
        reload_queue.add_installation(
            installation_id=installation_id, account=account
        )
        return _InstallationLoadResult(failed_accounts=[account])


def _get_account_to_load(
    installation: dict[str, Any],
    *,
    source_policy: BroadcastSourcePolicy,
    logger: BoundLogger,
) -> str | None:
    """Get the account of a GitHub App installation whose repositories must
    be loaded, or skip the installation with a warning.

    An installation that is suspended, or that is on an account that owns no
    repository that ``source_policy`` allows, is skipped, before a token is
    requested for it. So is an installation without an account.

    Parameters
    ----------
    installation
        The installation resource from the GitHub v3 API.
    source_policy
        The policy for which repositories may publish broadcast messages.
    logger
        The logger instance.

    Returns
    -------
    str or None
        The login of the installation's account, or `None` if the
        installation is skipped.
    """
    account = _get_installation_account(installation)
    logger = logger.bind(
        installation_id=str(installation["id"]), github_account=account
    )
    if installation.get("suspended_at"):
        logger.warning(
            "Skipping a suspended GitHub App installation",
            suspended_at=installation["suspended_at"],
        )
        return None
    if account is None or not source_policy.allows_owner(account):
        logger.warning(
            "Skipping a GitHub App installation on an account that owns no "
            "allowed repository"
        )
        return None
    return account


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


def _warn_about_unseen_repos(
    result: _InstallationLoadResult,
    *,
    source_policy: BroadcastSourcePolicy,
    logger: BoundLogger,
) -> None:
    """Log a warning that lists the allowed repositories that no GitHub App
    installation can see, if there are any.

    Parameters
    ----------
    result
        What was loaded from every installation that must be loaded. The
        repositories of its failed accounts are left out of the warning,
        since what their installations can see is not known.
    source_policy
        The policy for which repositories may publish broadcast messages.
    logger
        The logger instance.
    """
    unseen_repos = _get_unseen_repos(
        source_policy=source_policy,
        seen_repos=result.seen_repos,
        unknown_accounts=result.failed_accounts,
    )
    if unseen_repos:
        logger.warning(
            "No GitHub App installation can see these allowed repositories",
            github_repos=unseen_repos,
        )


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
    one or more GitHub App installations.
    """

    seen_repos: list[str] = dataclasses.field(default_factory=list)
    """Full names of the repositories that the installations can see,
    allowed or not.
    """

    failed_accounts: list[str] = dataclasses.field(default_factory=list)
    """Logins of the accounts of the installations that GitHub failed to
    authorize or to list the repositories of, which are in the reload queue.
    """

    repo_count: int = 0
    """Number of allowed repositories that loaded, including those without
    broadcasts.
    """

    failed_repo_count: int = 0
    """Number of allowed repositories that GitHub failed to return, which
    were added to the reload queue.
    """

    message_count: int = 0
    """Number of messages added to the broadcast message repository."""

    skipped_file_count: int = 0
    """Number of broadcast message files skipped because they could not be
    loaded.
    """

    def add_repo_result(self, repo_result: _RepoLoadResult) -> None:
        """Count what was loaded from one allowed repository."""
        self.message_count += repo_result.message_count
        self.skipped_file_count += repo_result.skipped_file_count
        if repo_result.failed:
            self.failed_repo_count += 1
        else:
            self.repo_count += 1

    def add_installation_result(self, result: Self) -> None:
        """Count what was loaded from the repositories of another
        installation.
        """
        self.seen_repos.extend(result.seen_repos)
        self.failed_accounts.extend(result.failed_accounts)
        self.repo_count += result.repo_count
        self.failed_repo_count += result.failed_repo_count
        self.message_count += result.message_count
        self.skipped_file_count += result.skipped_file_count


async def _list_installation_repos(
    *, installation_id: str, http_client: httpx.AsyncClient
) -> tuple[GitHubAPI, list[dict[str, Any]]]:
    """Authorize a GitHub App installation and list, in full, the
    repositories that it can see.

    Parameters
    ----------
    installation_id
        ID of the GitHub App installation.
    http_client
        The httpx client.

    Returns
    -------
    tuple
        A new client authorized as the installation, and the repository
        resources from the GitHub v3 API.

    Raises
    ------
    Exception
        Raised if the installation cannot be authorized or its repositories
        cannot be listed, as when GitHub fails or cannot be reached.
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
    return github_client, github_repos


def _get_seen_repos(github_repos: Iterable[dict[str, Any]]) -> list[str]:
    """Get the full names of the repositories that an installation lists."""
    return [
        full_name
        for github_repo in github_repos
        if isinstance(full_name := github_repo.get("full_name"), str)
    ]


def _filter_allowed_repos(
    github_repos: Iterable[dict[str, Any]],
    *,
    source_policy: BroadcastSourcePolicy,
    logger: BoundLogger,
) -> list[dict[str, Any]]:
    """Keep the repositories that may publish broadcasts, skipping each
    other one with a warning.

    Parameters
    ----------
    github_repos
        Repository resources from the GitHub v3 API.
    source_policy
        The policy for which repositories may publish broadcast messages.
    logger
        The logger instance.

    Returns
    -------
    list of dict
        The repositories that ``source_policy`` allows, in order.
    """
    allowed = []
    for github_repo in github_repos:
        decision = source_policy.evaluate(github_repo)
        if not decision.allowed:
            logger.warning(
                "Skipping a repository that may not publish broadcasts",
                github_repo=github_repo.get("full_name"),
                reason=decision.reason,
            )
            continue
        allowed.append(github_repo)
    return allowed


async def _load_installation(
    *,
    installation_id: str,
    http_client: httpx.AsyncClient,
    broadcast_repo: BroadcastMessageRepository,
    source_policy: BroadcastSourcePolicy,
    reload_queue: ReloadQueue,
    logger: BoundLogger,
) -> _InstallationLoadResult:
    """Load, at startup, the broadcast messages in the allowed repositories
    that a GitHub App installation can see.

    The installation's repositories are listed in full with a new client
    authorized as the installation. A repository that ``source_policy``
    rejects is skipped with a warning. Each allowed repository is then
    loaded (see `_load_repo_broadcasts`), without its lock, since no push is
    processed before startup completes. A repository that GitHub fails to
    return is added to ``reload_queue``.

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
    reload_queue
        The queue of repositories whose broadcasts must be reloaded.
    logger
        The logger instance.

    Returns
    -------
    _InstallationLoadResult
        What was loaded from the installation's repositories.

    Raises
    ------
    Exception
        Raised if the installation cannot be authorized or its repositories
        cannot be listed, as when GitHub fails or cannot be reached. None of
        the installation's repositories has been loaded then.
    """
    github_client, github_repos = await _list_installation_repos(
        installation_id=installation_id, http_client=http_client
    )
    result = _InstallationLoadResult(seen_repos=_get_seen_repos(github_repos))
    for github_repo in _filter_allowed_repos(
        github_repos, source_policy=source_policy, logger=logger
    ):
        repo_result = await _load_repo_broadcasts(
            github_repo=github_repo,
            broadcast_repo=broadcast_repo,
            github_client=github_client,
            logger=logger,
        )
        result.add_repo_result(repo_result)
        if repo_result.failed:
            reload_queue.add_repo(
                installation_id=installation_id,
                repo_full_name=github_repo["full_name"],
            )
    return result


async def run_reload_worker(
    *,
    reload_queue: ReloadQueue,
    http_client: httpx.AsyncClient,
    broadcast_repo: BroadcastMessageRepository,
    source_policy: BroadcastSourcePolicy,
    repo_locks: RepoLocks,
    logger: BoundLogger,
) -> None:
    """Reload the broadcasts of the repositories and installations in the
    reload queue, for as long as the app runs.

    This is meant to run as one background task for the life of the app,
    which both the startup bootstrap and the push webhook feed through
    ``reload_queue``. Each process reloads for its own broadcast repository.

    The worker waits until the queue has an entry. Then it makes attempts
    until the queue is empty, each one after a wait, for
    `BOOTSTRAP_RETRY_INITIAL_DELAY` seconds and then twice as long after
    each attempt, up to `BOOTSTRAP_RETRY_MAX_DELAY`. Once the queue is
    empty, the next entry starts the waits again from the initial delay.

    Each attempt (see `_run_reload_attempt`) tries once each entry that was
    pending when it started, and each installation that the listing of the
    installations queues during it, with a new client authorized as the
    entry's installation. Any exception, including an authentication error,
    leaves the entry in the queue to be tried in the next attempt.

    - The listing of the installations, which GitHub failed to return at
      startup, is tried first. Every page of the installations is listed
      with a new app JWT, the installations that startup skips are skipped
      again, and each other one is added to the queue as an installation,
      before the listing leaves the queue (see
      `_retry_installation_listing`). Those installations are tried in the
      same attempt. Once they have been tried, the allowed repositories that
      none of them can see are logged with a warning, leaving out the
      repositories of the accounts whose installations are still pending.
    - A repository is reloaded under its lock (see `_reload_queued_repo`).
      It leaves the queue once it reloads, or if GitHub no longer has it or
      ``source_policy`` no longer allows it. Otherwise it is tried again in
      the next attempt.
    - An installation, which GitHub failed to authorize or to list the
      repositories of at startup, or which the listing queued, is authorized
      and its repositories are listed. If that fails, it is tried again in
      the next attempt. Otherwise, each repository that ``source_policy``
      allows is reloaded in full under its lock, so that a repository that
      already loaded at startup stores each broadcast once, and one that
      fails is added to the queue as a repository, before the installation
      leaves the queue.

    The function returns only when the task is cancelled.

    Parameters
    ----------
    reload_queue
        The queue of repositories and installations whose broadcasts must be
        reloaded.
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
    while True:
        await reload_queue.wait_for_pending()
        delays = _iter_retry_delays()
        attempt = 0
        while len(reload_queue) > 0:
            await asyncio.sleep(next(delays))
            attempt += 1
            await _run_reload_attempt(
                attempt=attempt,
                http_client=http_client,
                broadcast_repo=broadcast_repo,
                source_policy=source_policy,
                repo_locks=repo_locks,
                reload_queue=reload_queue,
                logger=logger,
            )


async def _run_reload_attempt(
    *,
    attempt: int,
    http_client: httpx.AsyncClient,
    broadcast_repo: BroadcastMessageRepository,
    source_policy: BroadcastSourcePolicy,
    repo_locks: RepoLocks,
    reload_queue: ReloadQueue,
    logger: BoundLogger,
) -> None:
    """Make one attempt of `run_reload_worker`.

    If the listing of the installations is pending, it is tried first, so
    that the installations it queues are tried in the same attempt. Then
    each repository and installation that is pending is tried once. If the
    installations were listed, the allowed repositories that none of them
    can see are logged with a warning once they have all been tried (see
    `_warn_about_unseen_repos`).

    Parameters
    ----------
    attempt
        Number of the attempt since the queue was last empty, starting at 1.
    http_client
        The httpx client.
    broadcast_repo
        The broadcast message repository.
    source_policy
        The policy for which repositories may publish broadcast messages.
    repo_locks
        The per-repository locks of broadcast sources.
    reload_queue
        The queue of repositories and installations whose broadcasts must be
        reloaded.
    logger
        The logger instance.
    """
    listed = False
    if QueuedInstallationListing() in reload_queue:
        listed = await _retry_installation_listing(
            attempt=attempt,
            http_client=http_client,
            source_policy=source_policy,
            reload_queue=reload_queue,
            logger=logger,
        )

    totals = _InstallationLoadResult()
    for entry in reload_queue.pending():
        match entry:
            case QueuedInstallationListing():
                # Tried above, so it is pending only if GitHub failed again.
                pass
            case QueuedInstallation():
                result = await _retry_installation(
                    entry=entry,
                    attempt=attempt,
                    http_client=http_client,
                    broadcast_repo=broadcast_repo,
                    source_policy=source_policy,
                    repo_locks=repo_locks,
                    reload_queue=reload_queue,
                    logger=logger,
                )
                totals.add_installation_result(result)
            case QueuedRepo():
                await _retry_repo(
                    entry=entry,
                    attempt=attempt,
                    http_client=http_client,
                    broadcast_repo=broadcast_repo,
                    source_policy=source_policy,
                    repo_locks=repo_locks,
                    reload_queue=reload_queue,
                    logger=logger,
                )

    if listed:
        _warn_about_unseen_repos(
            totals, source_policy=source_policy, logger=logger
        )


def _iter_retry_delays() -> Iterator[float]:
    """Iterate over the seconds to wait before each attempt of
    `run_reload_worker`: an exponential backoff that is capped.
    """
    delay = BOOTSTRAP_RETRY_INITIAL_DELAY
    while True:
        yield delay
        delay = min(delay * 2, BOOTSTRAP_RETRY_MAX_DELAY)


async def _retry_installation_listing(
    *,
    attempt: int,
    http_client: httpx.AsyncClient,
    source_policy: BroadcastSourcePolicy,
    reload_queue: ReloadQueue,
    logger: BoundLogger,
) -> bool:
    """Try once to list the GitHub App's installations, which GitHub failed
    to list at startup, and queue each installation whose repositories must
    be loaded.

    Every page of the installations is listed with a new app JWT, since the
    one from startup expires. An installation that startup would skip (see
    `_get_account_to_load`) is skipped with a warning, and each other one is
    added to ``reload_queue`` as an installation. Then the listing leaves the
    queue. If GitHub fails, on any page, nothing is queued and the listing
    stays in the queue, to be tried again in the next attempt.

    Parameters
    ----------
    attempt
        Number of the attempt of `run_reload_worker` since the queue was
        last empty, starting at 1.
    http_client
        The httpx client.
    source_policy
        The policy for which repositories may publish broadcast messages.
    reload_queue
        The queue of repositories and installations whose broadcasts must be
        reloaded.
    logger
        The logger instance.

    Returns
    -------
    bool
        Whether the installations were listed.
    """
    logger = logger.bind(attempt=attempt)
    logger.info("Retrying listing the GitHub App's installations")
    try:
        installations = await _list_installations(http_client=http_client)
    except Exception:
        logger.warning(
            "Could not list the GitHub App's installations, will retry",
            exc_info=True,
        )
        return False

    installation_count = 0
    for installation in installations:
        account = _get_account_to_load(
            installation, source_policy=source_policy, logger=logger
        )
        if account is None:
            continue
        reload_queue.add_installation(
            installation_id=str(installation["id"]), account=account
        )
        installation_count += 1
    # Removed after the installations are queued, so that the queue does not
    # look empty in between.
    reload_queue.discard(QueuedInstallationListing())
    logger.info(
        "Listed the GitHub App's installations after retrying",
        installation_count=installation_count,
    )
    return True


async def _list_installations(
    *, http_client: httpx.AsyncClient
) -> list[dict[str, Any]]:
    """List, in full, the installations of the GitHub App.

    Parameters
    ----------
    http_client
        The httpx client.

    Returns
    -------
    list of dict
        The installation resources from the GitHub v3 API.

    Raises
    ------
    Exception
        Raised if the installations cannot be listed, as when GitHub fails or
        cannot be reached, on any page of the listing.
    """
    github_client = create_github_client(http_client=http_client)
    # A new JWT each time, since the app's JWT expires after ten minutes.
    jwt = get_app_jwt()
    return [
        installation
        async for installation in iter_installations(
            github_client=github_client, jwt=jwt
        )
    ]


async def _retry_installation(
    *,
    entry: QueuedInstallation,
    attempt: int,
    http_client: httpx.AsyncClient,
    broadcast_repo: BroadcastMessageRepository,
    source_policy: BroadcastSourcePolicy,
    repo_locks: RepoLocks,
    reload_queue: ReloadQueue,
    logger: BoundLogger,
) -> _InstallationLoadResult:
    """Try once to load the repositories of a GitHub App installation in the
    reload queue.

    Parameters
    ----------
    entry
        The installation's entry in the reload queue.
    attempt
        Number of the attempt of `run_reload_worker` since the queue was
        last empty, starting at 1.
    http_client
        The httpx client.
    broadcast_repo
        The broadcast message repository.
    source_policy
        The policy for which repositories may publish broadcast messages.
    repo_locks
        The per-repository locks of broadcast sources.
    reload_queue
        The queue of repositories and installations whose broadcasts must be
        reloaded.
    logger
        The logger instance.

    Returns
    -------
    _InstallationLoadResult
        What was loaded from the installation's repositories, including the
        repositories it can see. If GitHub failed to authorize the
        installation or to list its repositories, so that it stays in the
        queue, it has the installation's account as a failed account.
    """
    logger = logger.bind(
        installation_id=entry.installation_id,
        github_account=entry.account,
        attempt=attempt,
    )
    logger.info("Retrying loading broadcasts from a GitHub App installation")
    try:
        github_client, github_repos = await _list_installation_repos(
            installation_id=entry.installation_id, http_client=http_client
        )
    except Exception:
        logger.warning(
            "Could not authorize a GitHub App installation or list its "
            "repositories, will retry",
            exc_info=True,
        )
        return _InstallationLoadResult(failed_accounts=[entry.account])

    result = _InstallationLoadResult(seen_repos=_get_seen_repos(github_repos))
    for github_repo in _filter_allowed_repos(
        github_repos, source_policy=source_policy, logger=logger
    ):
        full_name = github_repo["full_name"]
        async with repo_locks.get(full_name):
            try:
                repo_result = await _reload_repo_at_default_branch(
                    github_repo=github_repo,
                    broadcast_repo=broadcast_repo,
                    github_client=github_client,
                    logger=logger,
                )
            except Exception:
                logger.warning(
                    "Could not load the repository's broadcasts, will retry",
                    github_repo=full_name,
                    exc_info=True,
                )
                repo_result = _RepoLoadResult(failed=True)
            if repo_result.failed:
                reload_queue.add_repo(
                    installation_id=entry.installation_id,
                    repo_full_name=full_name,
                )
        result.add_repo_result(repo_result)
    reload_queue.discard(entry)
    logger.info(
        "Loaded broadcasts from a GitHub App installation after retrying",
        repo_count=result.repo_count,
        failed_repo_count=result.failed_repo_count,
        message_count=result.message_count,
        skipped_file_count=result.skipped_file_count,
    )
    return result


async def _retry_repo(
    *,
    entry: QueuedRepo,
    attempt: int,
    http_client: httpx.AsyncClient,
    broadcast_repo: BroadcastMessageRepository,
    source_policy: BroadcastSourcePolicy,
    repo_locks: RepoLocks,
    reload_queue: ReloadQueue,
    logger: BoundLogger,
) -> None:
    """Try once to reload a repository in the reload queue, and remove it
    from the queue if it reloads or must no longer be reloaded.

    Parameters
    ----------
    entry
        The repository's entry in the reload queue.
    attempt
        Number of the attempt of `run_reload_worker` since the queue was
        last empty, starting at 1.
    http_client
        The httpx client.
    broadcast_repo
        The broadcast message repository.
    source_policy
        The policy for which repositories may publish broadcast messages.
    repo_locks
        The per-repository locks of broadcast sources.
    reload_queue
        The queue of repositories and installations whose broadcasts must be
        reloaded.
    logger
        The logger instance.
    """
    logger = logger.bind(github_repo=entry.full_name, attempt=attempt)
    logger.info("Retrying loading broadcasts from a repository")
    try:
        # A new client for each attempt, since an installation's token
        # expires after an hour.
        github_client = await create_github_installation_client(
            http_client=http_client, installation_id=entry.installation_id
        )
        async with repo_locks.get(entry.full_name):
            result = await _reload_queued_repo(
                entry=entry,
                broadcast_repo=broadcast_repo,
                github_client=github_client,
                source_policy=source_policy,
                logger=logger,
            )
            # Removed under the lock, so that a push that fails after the
            # reload queues the repository again.
            if result is None or not result.failed:
                reload_queue.discard(entry)
    except Exception:
        logger.warning(
            "Could not load the repository's broadcasts, will retry",
            exc_info=True,
        )
        return
    if result is not None and not result.failed:
        logger.info(
            "Loaded broadcasts from a repository after retrying",
            message_count=result.message_count,
            skipped_file_count=result.skipped_file_count,
        )


async def _reload_queued_repo(
    *,
    entry: QueuedRepo,
    broadcast_repo: BroadcastMessageRepository,
    github_client: GitHubAPI,
    source_policy: BroadcastSourcePolicy,
    logger: BoundLogger,
) -> _RepoLoadResult | None:
    """Reload a repository in the reload queue from its current default
    branch.

    The caller holds the repository's lock. The repository is fetched again,
    for its current default branch and to evaluate it with
    ``source_policy`` again. If GitHub does not find it (404), or the policy
    no longer allows it, it is not reloaded and a warning is logged.
    Otherwise it is reloaded in full from the head of its default branch
    (see `_reload_repo_at_default_branch`).

    Parameters
    ----------
    entry
        The repository's entry in the reload queue.
    broadcast_repo
        The broadcast message repository.
    github_client
        The GitHub API client, pre-authorized as the entry's installation.
    source_policy
        The policy for which repositories may publish broadcast messages.
    logger
        The logger instance.

    Returns
    -------
    _RepoLoadResult or None
        What was loaded from the repository, and whether GitHub failed, or
        `None` if the repository must no longer be reloaded.

    Raises
    ------
    Exception
        Raised if GitHub fails to return the repository or its default
        branch's head.
    """
    try:
        github_repo = await get_repository(
            github_client=github_client, repo_full_name=entry.full_name
        )
    except Exception as e:
        if not _is_not_found(e):
            raise
        logger.warning(
            "Dropping a repository that GitHub cannot find from the reload "
            "queue"
        )
        return None
    decision = source_policy.evaluate(github_repo)
    if not decision.allowed:
        logger.warning(
            "Dropping a repository that may not publish broadcasts from the "
            "reload queue",
            reason=decision.reason,
        )
        return None
    return await _reload_repo_at_default_branch(
        github_repo=github_repo,
        broadcast_repo=broadcast_repo,
        github_client=github_client,
        logger=logger,
    )


async def _reload_repo_at_default_branch(
    *,
    github_repo: Mapping[str, Any],
    broadcast_repo: BroadcastMessageRepository,
    github_client: GitHubAPI,
    logger: BoundLogger,
) -> _RepoLoadResult:
    """Reload a repository's broadcasts in full from the head of its default
    branch.

    The caller holds the repository's lock. The head of the repository's
    ``default_branch`` is looked up, and the repository's messages are made
    to match its broadcast files at that commit, under the branch's ref (see
    `_reload_repo_broadcasts`). So a file that was removed, or a message
    stored under a former default branch, stops being served.

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
        What was loaded from the repository, and whether GitHub failed.

    Raises
    ------
    Exception
        Raised if GitHub fails to return the default branch's head.
    """
    branch = github_repo["default_branch"]
    head_sha = await get_branch_head_sha(
        github_client=github_client,
        repo_full_name=github_repo["full_name"],
        branch=branch,
    )
    return await _reload_repo_broadcasts(
        repository=github_repo,
        ref=f"refs/heads/{branch}",
        head_sha=head_sha,
        broadcast_repo=broadcast_repo,
        github_client=github_client,
        logger=logger,
    )


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
    """Load the broadcast messages in a GitHub repository's default branch,
    at startup.

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
