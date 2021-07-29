"""Orchestration around GitHub repository sources for broadcast messages."""

from __future__ import annotations

import dataclasses
import os.path
from typing import TYPE_CHECKING, Any, Dict, Sequence, Set, Tuple

from gidgethub import GitHubException, RateLimitExceeded
from gidgethub.sansio import accept_format

from semaphore.broadcast.markdown import BroadcastMarkdown

if TYPE_CHECKING:
    from gidgethub.httpx import GitHubAPI
    from gidgethub.sansio import Event
    from sempaphore.broadcast.repository import BroadcastMessageRepository
    from structlog.stdlib import BoundLogger


@dataclasses.dataclass(frozen=True)
class GitHubMessageId:
    """An identifier for referencing broadcast messages sourced from GitHub in
    the BroadcastMessageRepostory, compatible with the MessageId protocol.
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
    def from_push_event(cls, *, path: str, event: Event) -> GitHubMessageId:
        return cls(
            path=path,
            repo_name=event.data["repository"]["name"],
            repo_owner=event.data["repository"]["owner"]["name"],
            ref=event.data["ref"],
        )


async def update_broadcast_repo_from_push_event(
    *,
    event: Event,
    broadcast_repo: BroadcastMessageRepository,
    github_client: GitHubAPI,
    logger: BoundLogger,
) -> None:
    """Updates messages in the repository based on a GitHub webhook for the
    push event, either adding, modifying, or removing messages based on
    the pushed commits.

    Parameters
    ----------
    event : `gidgethub.sansio.Event`
        The parsed event payload.
    broadcast_repo : ``BroadcastMessageRepository``
        The broadcast message repository.
    github_client : `gidgethub.httpx.GitHubAPI`
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
        message_id = GitHubMessageId.from_push_event(path=path, event=event)
        try:
            await add_file_to_repository(
                file_path=path,
                identifier=message_id,
                contents_url=event.data["repository"]["contents_url"],
                broadcast_repo=broadcast_repo,
                github_client=github_client,
                logger=logger,
            )
        except RateLimitExceeded as e:
            logger.error(
                "GitHub rate limit exception",
                limit=e.rate_limit.limit,
                remaining=e.rate_limit.remaining,
                resets_at=e.rate_limit.reset_datetime,
            )
        except GitHubException as e:
            logger.error("GitHub error", exception=str(e))
        except Exception as e:
            logger.error("Unknown error", exception=str(e))
        finally:
            continue

    for path in filter(is_broadcast_message, files_removed):
        message_id = GitHubMessageId.from_push_event(path=path, event=event)
        broadcast_repo.remove(message_id)
        logger.debug(
            "Removed message from repo",
            message_id=dataclasses.asdict(message_id),
        )


def _integrate_file_changes_in_commits(
    commits: Sequence[Dict[str, Any]]
) -> Tuple[Set[str], Set[str]]:
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


def is_broadcast_message(path: str) -> bool:
    """Determine if a path corresponds to a broadcast message file.

    Parameters
    ----------
    path : `str`
        Posix file path in a GitHub repository.

    Returns
    -------
    bool
        See implementation code for the current heuristics.
    """
    # TODO this function could be refactored into a class containing
    # configuration about the GitHub repo, such as where messages are hosted
    # if they aren't hosted in broadcasts/
    if os.path.dirname(path) != "broadcasts":
        return False

    if os.path.splitext(path)[-1].lower() not in set([".md"]):
        return False

    name = os.path.basename(path).lower()
    if name.startswith("."):
        return False
    if name == "readme.md":
        return False

    return True


async def add_file_to_repository(
    *,
    file_path: str,
    identifier: GitHubMessageId,
    contents_url: str,
    broadcast_repo: BroadcastMessageRepository,
    github_client: GitHubAPI,
    logger: BoundLogger,
) -> None:
    """Add/update a broadcast message in the repository from a file in a GitHub
    repository.

    Parameters
    ----------
    file_path : str
        Posix path of the message's file in GitHub.
    identifier : `GitHubMessageId`
        The file's GitHub message ID.
    broadcast_repo : ``BroadcastMessageRepository``
        The broadcast message repository.
    github_client : `gidgethub.httpx.GitHubAPI`
        The GitHub API client, pre-authorized as an app installation.
    logger
        The logger instance

    Raises
    ------
    GitHubException
        Raised if there is an issue with GitHub. The sub-class
        RateLimitExceeded indicates if the client has exceeded its rate limit.
    """
    markdown_text = await github_client.getitem(
        contents_url,
        url_vars={"path": file_path},
        accept=accept_format(media="raw", json=False),
    )
    logger.debug(
        "Downloaded broadcast from GitHub",
        message_id=dataclasses.asdict(identifier),
    )
    broadcast_message = BroadcastMarkdown(
        text=markdown_text,
        identifier=identifier,
    ).to_broadcast()
    broadcast_repo.add(broadcast_message)
    logger.debug(
        "Added broadcast to the repository",
        message_id=dataclasses.asdict(identifier),
    )
