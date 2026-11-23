"""GitHub webhook event handlers."""

import asyncio
from typing import Any

import httpx
from gidgethub.routing import Router
from gidgethub.sansio import Event
from structlog.stdlib import BoundLogger

from ..broadcast.repository import BroadcastMessageRepository
from .broadcastservices import update_broadcast_repo_from_push_event
from .client import create_github_installation_client
from .sourcepolicy import BroadcastSourcePolicy

__all__ = ["GITHUB_CONSISTENCY_DELAY", "router"]


GITHUB_CONSISTENCY_DELAY = 1.0
"""Seconds to wait after a push before reading content from GitHub, giving
GitHub time to reach internal consistency.
"""

router = Router()
"""GitHub webhook router."""


@router.register("push")
async def handle_push_event(
    event: Event,
    *,
    broadcast_repo: BroadcastMessageRepository,
    http_client: httpx.AsyncClient,
    installation_id: str,
    source_policy: BroadcastSourcePolicy,
    logger: BoundLogger,
    **kwargs: Any,
) -> None:
    """Process ``push`` webhook events from GitHub.

    Pushes from repositories that ``source_policy`` rejects are ignored with
    a warning, before any request is made to GitHub.

    Parameters
    ----------
    event
        The parsed event payload.
    broadcast_repo
        The broadcast message repository.
    http_client
        The httpx client, used to create a GitHub API client authorized as
        the app installation.
    installation_id
        The ID of the GitHub App installation that sent the event.
    source_policy
        The policy for which repositories may publish broadcast messages.
    logger
        The logger instance
    """
    repository = event.data["repository"]
    decision = source_policy.evaluate(repository)
    if not decision.allowed:
        logger.warning(
            "Ignoring push from a repository that may not publish broadcasts",
            github_repo=repository.get("full_name"),
            reason=decision.reason,
        )
        return

    git_ref = event.data["ref"]

    # TODO(jsick): process only broadcasts on the HEAD branch. The name of
    # this branch should be detected from the repository settings.
    if git_ref != "refs/heads/main":
        return

    github_client = await create_github_installation_client(
        http_client=http_client, installation_id=installation_id
    )
    await asyncio.sleep(GITHUB_CONSISTENCY_DELAY)
    await update_broadcast_repo_from_push_event(
        event=event,
        broadcast_repo=broadcast_repo,
        github_client=github_client,
        logger=logger,
    )

    logger.debug(
        "GH requests remaining",
        remaining=(
            github_client.rate_limit.remaining
            if github_client.rate_limit is not None
            else "unknown"
        ),
    )
