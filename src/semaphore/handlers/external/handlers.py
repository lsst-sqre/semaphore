"""Handlers for the app's external root, ``/semaphore/``."""

from fastapi import APIRouter, Depends
from safir.dependencies.logger import logger_dependency
from safir.metadata import get_metadata
from structlog.stdlib import BoundLogger

from semaphore.config import config

from .models import Index

__all__ = ["get_index"]

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
