"""Handlers for the app's external root, ``/semaphore/``."""

from fastapi import APIRouter, Depends
from safir.dependencies.logger import logger_dependency
from safir.metadata import get_metadata
from strawberry.asgi import GraphQL
from structlog.stdlib import BoundLogger

from semaphore.config import config
from semaphore.graphql.schema import graphql_schema
from semaphore.models import Index

__all__ = ["get_index", "external_router"]

external_router = APIRouter()
"""FastAPI router for all external handlers."""


# Add the Strawberry graphql app
external_router.add_route("/graphql", GraphQL(graphql_schema))


@external_router.get(
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

    Customize this handler to return whatever the top-level resource of your
    application should return. For example, consider listing key API URLs.
    When doing so, also change or customize the response model in
    `semaphore.models.metadata`.

    By convention, the root of the external API includes a field called
    ``metadata`` that provides the same Safir-generated metadata as the
    internal root endpoint.
    """
    metadata = get_metadata(
        package_name="semaphore",
        application_name=config.name,
    )
    return Index(metadata=metadata)
