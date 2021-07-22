"""GitHub webhook event handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from gidgethub.routing import Router

from .broadcastservices import update_broadcast_repo_from_push_event

if TYPE_CHECKING:
    from gidgethub.httpx import GitHubAPI
    from gidgethub.sansio import Event
    from sempaphore.broadcast.repository import BroadcastMessageRepository
    from structlog.stdlib import BoundLogger

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
    event : `gidgethub.sansio.Event`
        The parsed event payload.
    broadcast_repo : ``BroadcastMessageRepository``
        The broadcast message repository.
    github_client : `gidgethub.httpx.GitHubAPI`
        The GitHub API client, pre-authorized as an app installation.
    logger
        The logger instance
    """
    git_ref = event.data["ref"]

    # TODO process only broadcasts on the HEAD branch. The name of this
    # branch should be detected from the repository settings.
    if git_ref == "refs/heads/main":
        await update_broadcast_repo_from_push_event(
            event=event,
            broadcast_repo=broadcast_repo,
            github_client=github_client,
            logger=logger,
        )
