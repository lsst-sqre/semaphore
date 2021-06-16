"""Handlers for the app's root endpoint, ``/``."""

from fastapi import APIRouter
from safir.metadata import Metadata, get_metadata

from semaphore.config import config

__all__ = ["internal_router", "get_index"]

internal_router = APIRouter()


@internal_router.get(
    "/",
    description=(
        "Returns metadata about the running application. Can also be used as "
        "a health check. This route is not exposed outside the cluster and "
        "therefore cannot be used by external clients."
    ),
    response_model=Metadata,
    response_model_exclude_none=True,
    summary="Application metadata",
)
async def get_index() -> Metadata:
    """GET ``/`` (the app's internal root).

    By convention, this endpoint returns only the application's metadata.
    """
    metadata = get_metadata(
        package_name="semaphore",
        application_name=config.name,
    )
    return metadata
