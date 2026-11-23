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
from .repolocks import RepoLocks
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
    repo_locks: RepoLocks,
    logger: BoundLogger,
    **kwargs: Any,
) -> None:
    """Process ``push`` webhook events from GitHub.

    Pushes from repositories that ``source_policy`` rejects are ignored with
    a warning, before any request is made to GitHub. Pushes that delete a
    ref, or that are not to the repository's default branch, are also
    ignored. The broadcast files that a push touches are reconciled with the
    default branch's current head, one push per repository at a time (see
    `update_broadcast_repo_from_push_event`).

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
    repo_locks
        The per-repository locks that serialize pushes for one repository.
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

    # A push that deletes its ref has no commit to read. GitHub marks it
    # with "deleted" and an all-zero "after" SHA; either is enough.
    if event.data.get("deleted") or _is_null_sha(event.data["after"]):
        logger.debug("Ignoring push that deletes a ref", ref=event.data["ref"])
        return

    # Broadcasts are published only from the repository's default branch.
    if event.data["ref"] != f"refs/heads/{repository['default_branch']}":
        return

    github_client = await create_github_installation_client(
        http_client=http_client, installation_id=installation_id
    )
    await asyncio.sleep(GITHUB_CONSISTENCY_DELAY)
    await update_broadcast_repo_from_push_event(
        event=event,
        broadcast_repo=broadcast_repo,
        github_client=github_client,
        repo_locks=repo_locks,
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


def _is_null_sha(sha: str) -> bool:
    """Determine if a commit SHA is the null SHA (all zeros) that GitHub
    reports for the missing side of a push that creates or deletes a ref.
    """
    return not sha.strip("0")
