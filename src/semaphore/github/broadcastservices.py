"""Orchestration around GitHub repository sources for broadcast messages."""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncGenerator, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

from gidgethub import GitHubException, RateLimitExceeded
from gidgethub.sansio import accept_format

from ..broadcast.markdown import BroadcastMarkdown
from ..config import config
from .client import (
    create_github_client,
    create_github_installation_client,
    get_app_jwt,
)

if TYPE_CHECKING:
    import httpx
    from gidgethub.httpx import GitHubAPI
    from gidgethub.sansio import Event
    from sempaphore.broadcast.repository import BroadcastMessageRepository
    from structlog.stdlib import BoundLogger


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
) -> None:
    """Add/update a broadcast message in the repository from a file in a GitHub
    repository.

    Parameters
    ----------
    file_path
        Posix path of the message's file in GitHub.
    message_ref
        Reference for the message.
    contents_url
        The URI template for the repository's contents (the template
        includs a ``path`` variable).
    broadcast_repo
        The broadcast message repository.
    github_client
        The GitHub API client, pre-authorized as an app installation.
    logger
        The logger instance

    Raises
    ------
    GitHubException
        Raised if there is an issue with GitHub. The sub-class
        RateLimitExceeded indicates if the client has exceeded its rate limit.
    """
    logger = logger.bind(message_id=message_ref.as_id())

    markdown_text = await github_client.getitem(
        contents_url,
        url_vars={"path": file_path},
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
        identifier=message_ref.as_id(),
    )
    if not broadcast_markdown.is_relevant_to_env(config.phalanx_env):
        logger.debug(
            "Skipping broadcast message from GitHub because it is "
            "irrelevant to this Phalanx environment."
        )
        return

    broadcast_message = broadcast_markdown.to_broadcast()
    broadcast_repo.add(broadcast_message)
    logger.debug(
        "Added broadcast to the repository",
    )


async def bootstrap_broadcast_repo(
    *,
    http_client: httpx.AsyncClient,
    broadcast_repo: BroadcastMessageRepository,
    logger: BoundLogger,
) -> None:
    """Bootstrap data in the broadcast repo from GitHub repositories that the
    GitHub app has access to.

    Semaphore is a GitHub App that can be installed in specific repositories.
    Semaphore's heuristic is that any repo it is installed in is eligible for
    contributing repos.

    Parameters
    ----------
    http_client
        The httpx client.
    broadcast_repo
        The broadcast message repository.
    logger
        The logger instance.
    """
    github_client = create_github_client(http_client=http_client)
    jwt = get_app_jwt()
    async for installation in iter_installations(
        github_client=github_client, jwt=jwt
    ):
        installation_client = await create_github_installation_client(
            http_client=http_client, installation_id=installation["id"]
        )
        async for github_repo in iter_installation_repositories(
            github_client=installation_client
        ):
            logger.debug("interating on github_repo", github_repo=github_repo)
            async for file_obj in iter_repo_dir_contents(
                github_client=installation_client,
                contents_url=github_repo["contents_url"],
                directory_path=BROADCASTS_DIR,
            ):
                if is_broadcast_message(file_obj["path"]):
                    message_ref = GitHubMessageRef(
                        path=file_obj["path"],
                        repo_name=github_repo["name"],
                        repo_owner=github_repo["owner"]["login"],
                        ref=f"refs/heads/{github_repo['default_branch']}",
                    )
                    await add_file_to_repository(
                        file_path=file_obj["path"],
                        message_ref=message_ref,
                        contents_url=github_repo["contents_url"],
                        broadcast_repo=broadcast_repo,
                        github_client=installation_client,
                        logger=logger,
                    )


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
    to.

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
    # TODO(jsick): we're temporarily getitem instead of getiter because
    # of https://github.com/brettcannon/gidgethub/issues/164
    # this means we'll miss any extra pages, but that should be fine for now
    # given the small number of installations expected (generally 1 repo!).
    data = await github_client.getitem("/installation/repositories")
    for repo in data["repositories"]:
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
