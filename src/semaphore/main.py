"""Semaphore FastAPI application."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from importlib.metadata import version

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from safir.dependencies.http_client import http_client_dependency
from safir.logging import configure_logging, configure_uvicorn_logging
from safir.middleware.x_forwarded import XForwardedMiddleware
from safir.slack.webhook import SlackRouteErrorHandler

from .config import config
from .dependencies.broadcastrepo import broadcast_repo_dependency
from .github.broadcastservices import bootstrap_broadcast_repo
from .handlers.external import external_router
from .handlers.internal import internal_router
from .handlers.v1 import v1_router

__all__ = ["app"]

configure_logging(
    profile=config.log_profile, log_level=config.log_level, name="semaphore"
)
configure_uvicorn_logging(config.log_level)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    logger = structlog.get_logger("semaphore")
    logger.info("Running startup")

    broadcast_repo = await broadcast_repo_dependency()
    if config.enable_github_app:
        await bootstrap_broadcast_repo(
            http_client=await http_client_dependency(),
            broadcast_repo=broadcast_repo,
            logger=logger,
        )

    yield

    await http_client_dependency.aclose()


app = FastAPI(
    title="Semaphore",
    description=(
        "Semaphore is the user message and notification system for the "
        "Rubin Science Platform.\n\n"
        "You can find Semaphore's user and developer documentation at "
        "[https://semaphore.lsst.io](https://semaphore.lsst.io). "
        "Semaphore is developed at [https://github.com/lsst-sqre/semaphore]"
        "(https://github.com/lsst-sqre/semaphore)"
    ),
    version=version("semaphore"),
    docs_url=f"/{config.path_prefix}/docs",
    redoc_url=f"/{config.path_prefix}/redoc",
    openapi_url=f"/{config.path_prefix}/openapi.json",
    lifespan=lifespan,
)
app.include_router(internal_router)
app.include_router(external_router, prefix=config.path_prefix)
app.include_router(v1_router, prefix=f"{config.path_prefix}/v1")
app.add_middleware(XForwardedMiddleware)

# This CORS policy is quite liberal. When the API becomes writeable we'll
# need to revisit this.
app.add_middleware(
    CORSMiddleware,
    allow_origins="*",
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)

# Configure Slack alerts.
if webhook := config.slack_webhook:
    logger = structlog.get_logger("semaphore")
    SlackRouteErrorHandler.initialize(webhook, "semaphore", logger)
    logger.debug("Initialized Slack webhook")


def create_openapi() -> str:
    """Create the OpenAPI spec for static documentation."""
    spec = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    return json.dumps(spec)
