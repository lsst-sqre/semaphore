"""Handlers for the app's external root, ``/semaphore/``."""

from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, Request, Response, status
from gidgethub import BadRequest, ValidationFailure
from gidgethub.sansio import Event
from safir.dependencies.http_client import http_client_dependency
from safir.dependencies.logger import logger_dependency
from safir.metadata import get_metadata
from safir.slack.webhook import SlackRouteErrorHandler
from structlog.stdlib import BoundLogger

from ...broadcast.repository import BroadcastMessageRepository
from ...config import config
from ...dependencies.broadcastrepo import broadcast_repo_dependency
from ...dependencies.reloadqueue import reload_queue_dependency
from ...dependencies.repolocks import repo_locks_dependency
from ...github.reloadqueue import ReloadQueue
from ...github.repolocks import RepoLocks
from ...github.sourcepolicy import BroadcastSourcePolicy
from ...github.webhooks import router as webhook_router
from .models import Index

__all__ = ["router"]

router = APIRouter(route_class=SlackRouteErrorHandler)
"""FastAPI router for all external handlers.

These routes have paths prefixed by the application name.
"""


@router.get(
    "/",
    summary="Application metadata",
    description=(
        "Semaphore's API entrypoint provides metadata about the service "
        "version and configuration information."
    ),
    response_model=Index,
    response_model_exclude_none=True,
    tags=["internal"],
)
async def get_index() -> Index:
    """GET ``/semaphore/`` (the app's external root).

    This handler provides metadata and other top-level URLs, such as
    key API URLs.

    By convention, the root of the external API includes a field called
    ``metadata`` that provides the same Safir-generated metadata as the
    internal root endpoint.
    """
    metadata = get_metadata(
        package_name="semaphore", application_name=config.name
    )
    return Index(
        metadata=metadata,
        github_app_id=config.github_app_id,
        github_app_enabled=config.enable_github_app,
        api_docs_path=f"/{config.path_prefix}/docs",
        openapi_path=f"/{config.path_prefix}/openapi.json",
    )


@router.post(
    "/github/webhook",
    summary="GitHub App webhook",
    description=("This endpoint receives webhook events from GitHub"),
    status_code=status.HTTP_200_OK,
    tags=["internal"],
)
async def post_github_webhook(
    request: Request,
    *,
    logger: Annotated[BoundLogger, Depends(logger_dependency)],
    http_client: Annotated[httpx.AsyncClient, Depends(http_client_dependency)],
    broadcast_repo: Annotated[
        BroadcastMessageRepository, Depends(broadcast_repo_dependency)
    ],
    repo_locks: Annotated[RepoLocks, Depends(repo_locks_dependency)],
    reload_queue: Annotated[ReloadQueue, Depends(reload_queue_dependency)],
) -> Response:
    """Process GitHub webhook events."""
    if not config.enable_github_app:
        return Response(
            "GitHub App is not enabled",
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
        )

    body = await request.body()

    if config.github_webhook_secret is None:
        return Response(
            "The webhook secret is not configured",
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
        )

    # Event.from_http compares the signature with hmac.compare_digest, which
    # raises TypeError for a string with a character that isn't ASCII.
    # Starlette decodes headers as latin-1, so a request can send one, but a
    # signature that GitHub sends is always ASCII.
    if not _are_signatures_ascii(request):
        return _reject_invalid_signature(logger, "signature is not ASCII")

    webhook_secret = config.github_webhook_secret.get_secret_value()
    try:
        event = Event.from_http(request.headers, body, secret=webhook_secret)
    except ValidationFailure as e:
        return _reject_invalid_signature(logger, str(e))
    except BadRequest as e:
        # Event.from_http raises this if it can't decode the body, such as
        # for an unsupported content type.
        logger.warning(
            "Rejected a GitHub webhook whose body could not be decoded",
            reason=str(e),
        )
        return Response(
            "The webhook body must be JSON or form-encoded JSON",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    if event.event == "ping":
        return Response(status_code=status.HTTP_200_OK)

    # Bind the X-GitHub-Delivery header to the logger context; this identifies
    # the webhook request in GitHub's API and UI for diagnostics
    logger = logger.bind(github_delivery=event.delivery_id)

    logger.debug("Received GitHub webhook", payload=event.data)
    try:
        # GitHub sends the ID as a number. It identifies the installation as
        # a string elsewhere, as in the reload queue.
        installation_id = str(event.data["installation"]["id"])
    except KeyError, TypeError:
        # TypeError is raised if installation is null or the body is not a
        # JSON object.
        logger.warning("Rejected a GitHub webhook without an installation ID")
        return Response(
            "Did not find installation.id in the webhook event payload",
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    # Handlers create their own GitHub client, so that they can check the
    # event's repository against the source policy before calling GitHub.
    await webhook_router.dispatch(
        event,
        broadcast_repo=broadcast_repo,
        http_client=http_client,
        installation_id=installation_id,
        source_policy=BroadcastSourcePolicy.from_config(config),
        repo_locks=repo_locks,
        reload_queue=reload_queue,
        logger=logger,
    )
    return Response(status_code=status.HTTP_202_ACCEPTED)


def _are_signatures_ascii(request: Request) -> bool:
    """Check whether every signature header of a GitHub webhook request is
    ASCII.

    ``Event.from_http`` validates the ``X-Hub-Signature-256`` header, or the
    deprecated ``X-Hub-Signature`` header if the request has no
    ``X-Hub-Signature-256`` header, so both are checked.
    """
    return all(
        value.isascii()
        for name in ("X-Hub-Signature-256", "X-Hub-Signature")
        for value in request.headers.getlist(name)
    )


def _reject_invalid_signature(logger: BoundLogger, reason: str) -> Response:
    """Log and return the response to a GitHub webhook request whose
    signature is missing or invalid.
    """
    logger.warning(
        "Rejected a GitHub webhook without a valid signature", reason=reason
    )
    return Response(
        "The webhook signature is missing or invalid",
        status_code=status.HTTP_401_UNAUTHORIZED,
    )
