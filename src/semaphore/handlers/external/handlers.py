"""Handlers for the app's external root, ``/semaphore/``."""

from __future__ import annotations

import asyncio

import httpx
from fastapi import APIRouter, Depends, Request, Response, status
from gidgethub.sansio import Event
from safir.dependencies.http_client import http_client_dependency
from safir.dependencies.logger import logger_dependency
from safir.metadata import get_metadata
from structlog.stdlib import BoundLogger

from semaphore.broadcast.repository import BroadcastMessageRepository
from semaphore.config import config
from semaphore.dependencies.broadcastrepo import broadcast_repo_dependency
from semaphore.github.client import create_github_installation_client
from semaphore.github.webhooks import router as webhook_router

from .models import Index

__all__ = ["get_index", "post_github_webhook"]

router = APIRouter(prefix=f"/{config.name}")
"""FastAPI router for all external handlers.

These routes have paths prefixed by the application name.
"""


@router.get(
    "/",
    description=(
        "Document the top-level API here. By default it only returns metadata "
        "about the application."
    ),
    response_model=Index,
    response_model_exclude_none=True,
    summary="Application metadata",
)
async def get_index(
    logger: BoundLogger = Depends(logger_dependency),
) -> Index:
    """GET ``/semaphore/`` (the app's external root).

    This handler provides metadata and other top-level URLs, such as
    key API URLs.

    By convention, the root of the external API includes a field called
    ``metadata`` that provides the same Safir-generated metadata as the
    internal root endpoint.
    """
    metadata = get_metadata(
        package_name="semaphore",
        application_name=config.name,
    )
    return Index(metadata=metadata)


@router.post(
    "/github/webhook",
    description=("This endpoint receives webhook events from GitHub"),
    status_code=status.HTTP_200_OK,
)
async def post_github_webhook(
    request: Request,
    logger: BoundLogger = Depends(logger_dependency),
    http_client: httpx.AsyncClient = Depends(http_client_dependency),
    broadcast_repo: BroadcastMessageRepository = Depends(
        broadcast_repo_dependency
    ),
) -> Response:
    """Process GitHub webhook events."""
    if not config.enable_github_app:
        return Response(
            "GitHub App is not enabled",
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
        )

    body = await request.body()

    try:
        # FIXME workaround for typing
        assert config.github_webhook_secret is not None
        webhook_secret = config.github_webhook_secret.get_secret_value()
    except AttributeError:
        return Response(
            "The webhook secret is not configured",
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
        )
    event = Event.from_http(request.headers, body, secret=webhook_secret)

    if event.event == "ping":
        return Response(status_code=status.HTTP_200_OK)

    # Bind the X-GitHub-Delivery header to the logger context; this identifies
    # the webhook request in GitHub's API and UI for diagnostics
    logger = logger.bind(github_delivery=event.delivery_id)

    logger.debug("Received GitHub webhook", payload=event.data)
    try:
        installation_id = event.data["installation"]["id"]
    except AttributeError:
        return Response(
            "Did not find installation.id in the webhook event payload",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    github_client = await create_github_installation_client(
        http_client=http_client, installation_id=installation_id
    )
    # Give GitHub some time to reach internal consistency.
    await asyncio.sleep(1)
    await webhook_router.dispatch(event, broadcast_repo, github_client, logger)

    logger.debug(
        "GH requests remaining",
        remaining=(
            github_client.rate_limit.remaining
            if github_client.rate_limit is not None
            else "unknown"
        ),
    )
    return Response(status_code=status.HTTP_202_ACCEPTED)
