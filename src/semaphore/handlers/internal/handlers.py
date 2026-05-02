"""Handlers for the app's root endpoint, ``/``.

These endpoints are internal because they are served from the root url
rather than the application's path prefix. Therefore these endpoints don't
receive web traffic from an ingress.
"""

from fastapi import APIRouter
from safir.metadata import Metadata, get_metadata
from safir.slack.webhook import SlackRouteErrorHandler

from semaphore.config import config

__all__ = ["router"]

router = APIRouter(route_class=SlackRouteErrorHandler)
"""FastAPI router for all internal handlers."""


@router.get(
    "/",
    summary="Health check",
    description=(
        "Returns metadata about the running application. Can also be used as "
        "a health check. This route is not exposed outside the cluster and "
        "therefore cannot be used by external clients."
    ),
    response_model=Metadata,
    response_model_exclude_none=True,
    tags=["internal"],
)
async def get_index() -> Metadata:
    """GET ``/`` (the app's internal root).

    By convention, this endpoint returns only the application's metadata.
    """
    return get_metadata(package_name="semaphore", application_name=config.name)
