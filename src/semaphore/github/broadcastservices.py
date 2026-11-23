"""Orchestration around GitHub repository sources for broadcast messages."""

import dataclasses
from collections.abc import AsyncGenerator, Sequence
from pathlib import Path
from typing import Any, Self

import httpx
from gidgethub import GitHubException, RateLimitExceeded
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
from .sourcepolicy import BroadcastSourcePolicy

BROADCASTS_DIR = "broadcasts"
"""Directory relative to a GitHub repository's root that contains broadcast
message files.
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

    def as_id(self) -> str:
        """Convert to a string format for use as a BroadcastMessage.identifier
        attribute.
        """
        return (
            f"github.com/{self.repo_owner}/{self.repo_name}/{self.path}/"
            f"?ref={self.ref}"
        )

    def as_dict(self) -> dict[str, str]:
        """Express the message as a dict, which is useful for establishing
        logging context.
        """
        return dataclasses.asdict(self)


async def update_broadcast_repo_from_push_event(
    *,
    event: Event,
    broadcast_repo: BroadcastMessageRepository,
    github_client: GitHubAPI,
    logger: BoundLogger,
) -> None:
    """Update messages in the repository based on a GitHub webhook for the
    push event, either adding, modifying, or removing messages based on
    the pushed commits.

    Added and modified files are read at the commit the push moved its ref
    to (the payload's ``after`` SHA), so later pushes cannot change what is
    read for this one.

    Parameters
    ----------
    event
        The parsed event payload.
    broadcast_repo
        The broadcast message repository.
    github_client
        The GitHub API client, pre-authorized as an app installation.
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

    for path in filter(is_broadcast_message, files_written):
        message_ref = GitHubMessageRef.from_push_event(path=path, event=event)
        try:
            await add_file_to_repository(
                file_path=path,
                message_ref=message_ref,
                contents_url=event.data["repository"]["contents_url"],
                broadcast_repo=broadcast_repo,
                github_client=github_client,
                logger=logger,
                git_ref=event.data["after"],
            )
        except RateLimitExceeded as e:
            logger.exception(
                "GitHub rate limit exception",
                limit=e.rate_limit.limit,
                remaining=e.rate_limit.remaining,
                resets_at=e.rate_limit.reset_datetime,
            )
        except GitHubException as e:
            logger.exception("GitHub error", error=str(e))
        except Exception as e:
            logger.exception("Unknown error", error=str(e))

    for path in filter(is_broadcast_message, files_removed):
        message_id = GitHubMessageRef.from_push_event(
            path=path, event=event
        ).as_id()
        broadcast_repo.remove(message_id)
        logger.debug(
            "Removed message from repo",
            message_id=message_id,
        )


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


async def bootstrap_broadcast_repo(
    *,
    http_client: httpx.AsyncClient,
    broadcast_repo: BroadcastMessageRepository,
    source_policy: BroadcastSourcePolicy,
    logger: BoundLogger,
) -> None:
    """Bootstrap data in the broadcast repo from GitHub repositories that the
    GitHub app has access to.

    Semaphore is a GitHub App that can be installed in specific repositories.
    Of the repositories that its installations can see, only those that
    ``source_policy`` allows are loaded; the others are skipped with a
    warning, before any of their contents are requested. Content in an
    allowed repository cannot stop startup: a broadcast file that cannot be
    read or parsed, or a repository whose broadcasts cannot be listed, is
    skipped with a warning and loading continues. A summary of what was
    loaded is logged at the end.

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
    """
    github_client = create_github_client(http_client=http_client)
    jwt = get_app_jwt()
    repo_count = 0
    message_count = 0
    skipped_file_count = 0
    async for installation in iter_installations(
        github_client=github_client, jwt=jwt
    ):
        installation_client = await create_github_installation_client(
            http_client=http_client, installation_id=installation["id"]
        )
        async for github_repo in iter_installation_repositories(
            github_client=installation_client
        ):
            decision = source_policy.evaluate(github_repo)
            if not decision.allowed:
                logger.warning(
                    "Skipping a repository that may not publish broadcasts",
                    github_repo=github_repo.get("full_name"),
                    reason=decision.reason,
                )
                continue
            result = await _load_repo_broadcasts(
                github_repo=github_repo,
                broadcast_repo=broadcast_repo,
                github_client=installation_client,
                logger=logger,
            )
            if result is None:
                continue
            repo_count += 1
            message_count += result.message_count
            skipped_file_count += result.skipped_file_count

    logger.info(
        "Loaded broadcasts from GitHub",
        github_repos=source_policy.allowed_repos,
        repo_count=repo_count,
        message_count=message_count,
        skipped_file_count=skipped_file_count,
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


async def _load_repo_broadcasts(
    *,
    github_repo: dict[str, Any],
    broadcast_repo: BroadcastMessageRepository,
    github_client: GitHubAPI,
    logger: BoundLogger,
) -> _RepoLoadResult | None:
    """Load the broadcast messages in a GitHub repository's default branch.

    A file that cannot be loaded, because of a GitHub error or because its
    content is malformed, is skipped with a warning. If the repository's
    broadcasts directory cannot be listed, the whole repository is skipped
    with a warning.

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
    _RepoLoadResult or None
        What was loaded from the repository, or `None` if the repository was
        skipped.
    """
    logger = logger.bind(github_repo=github_repo["full_name"])
    logger.debug("Loading broadcasts from repository")
    try:
        paths = [
            file_obj["path"]
            async for file_obj in iter_repo_dir_contents(
                github_client=github_client,
                contents_url=github_repo["contents_url"],
                directory_path=BROADCASTS_DIR,
            )
            if is_broadcast_message(file_obj["path"])
        ]
    except Exception:
        logger.warning(
            "Skipping a repository whose broadcasts could not be listed",
            exc_info=True,
        )
        return None

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
        except Exception:
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
    github_client: GitHubAPI, contents_url: str, directory_path: str
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

    Yields
    ------
    dict
        The repository content resource. See
        https://docs.github.com/en/rest/reference/repos#contents
    """
    if directory_path.endswith("/"):
        directory_path = directory_path.rstrip("/")

    async for file_obj in github_client.getiter(
        contents_url, url_vars={"path": directory_path}
    ):
        yield file_obj
