"""GitHub webhook event handlers."""

from typing import Any

from gidgethub.httpx import GitHubAPI
from gidgethub.routing import Router
from gidgethub.sansio import Event
from structlog.stdlib import BoundLogger

from ..broadcast.repository import BroadcastMessageRepository
from .broadcastservices import update_broadcast_repo_from_push_event

__all__ = ["router"]


router = Router()
"""GitHub webhook router."""


@router.register("push")
async def handle_push_event(
    event: Event,
    broadcast_repo: BroadcastMessageRepository,
    github_client: GitHubAPI,
    logger: BoundLogger,
    *args: Any,
    **kwargs: Any,
) -> None:
    """Process ``push`` webhook events from GitHub.

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
    git_ref = event.data["ref"]

    # TODO(jsick): process only broadcasts on the HEAD branch. The name of
    # this branch should be detected from the repository settings.
    if git_ref == "refs/heads/main":
        await update_broadcast_repo_from_push_event(
            event=event,
            broadcast_repo=broadcast_repo,
            github_client=github_client,
            logger=logger,
        )
