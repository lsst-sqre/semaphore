"""Semaphore FastAPI application."""

import asyncio
import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from importlib.metadata import version

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from safir.database import create_database_engine, is_database_current
from safir.dependencies.db_session import db_session_dependency
from safir.dependencies.http_client import http_client_dependency
from safir.fastapi import ClientRequestError, client_request_error_handler
from safir.logging import configure_logging, configure_uvicorn_logging
from safir.middleware.x_forwarded import XForwardedMiddleware
from safir.sentry import initialize_sentry
from safir.slack.webhook import SlackRouteErrorHandler

from . import __version__
from .config import config
from .dependencies.broadcastrepo import broadcast_repo_dependency
from .dependencies.reloadqueue import reload_queue_dependency
from .dependencies.repolocks import repo_locks_dependency
from .github.broadcastservices import (
    bootstrap_broadcast_repo,
    run_reload_worker,
)
from .github.sourcepolicy import BroadcastSourcePolicy
from .handlers.external import external_router
from .handlers.internal import internal_router
from .handlers.v1 import v1_router

__all__ = ["app"]

initialize_sentry(release=__version__)

configure_logging(
    profile=config.log_profile, log_level=config.log_level, name="semaphore"
)
configure_uvicorn_logging(config.log_level)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    logger = structlog.get_logger("semaphore")
    logger.info("Running startup")

    # Check that the database schema is current.
    engine = create_database_engine(
        config.database_url, config.database_password
    )
    if not await is_database_current(engine, logger):
        raise RuntimeError("Database schema out of date")
    await engine.dispose()

    # Initialize the database.
    await db_session_dependency.initialize(
        config.database_url, config.database_password, pool_pre_ping=True
    )

    # Initialize the broadcast GitHub repository.
    broadcast_repo = await broadcast_repo_dependency()
    reload_task: asyncio.Task[None] | None = None
    if config.github_app_disabled_reason:
        logger.warning(
            "GitHub App is disabled", reason=config.github_app_disabled_reason
        )
    if config.enable_github_app:
        http_client = await http_client_dependency()
        source_policy = BroadcastSourcePolicy.from_config(config)
        reload_queue = await reload_queue_dependency()
        await bootstrap_broadcast_repo(
            http_client=http_client,
            broadcast_repo=broadcast_repo,
            source_policy=source_policy,
            reload_queue=reload_queue,
            logger=logger,
        )
        # Startup does not wait for GitHub to recover. The worker reloads
        # what failed at startup, and what fails while a push is processed,
        # for this process's own broadcast repository.
        reload_task = asyncio.create_task(
            run_reload_worker(
                reload_queue=reload_queue,
                http_client=http_client,
                broadcast_repo=broadcast_repo,
                source_policy=source_policy,
                repo_locks=await repo_locks_dependency(),
                logger=logger,
            ),
            name="github-reload-worker",
        )
    # The reload worker's task, if the GitHub App is enabled, is kept on the
    # app's state so that it can be inspected (as by tests).
    app.state.github_reload_task = reload_task

    yield

    if reload_task is not None:
        # Stopped before the HTTP client it uses is closed.
        reload_task.cancel()
        with suppress(asyncio.CancelledError):
            await reload_task
    await db_session_dependency.aclose()
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
    docs_url=f"{config.path_prefix}/docs",
    redoc_url=f"{config.path_prefix}/redoc",
    openapi_url=f"{config.path_prefix}/openapi.json",
    lifespan=lifespan,
)
"""The main FastAPI web application for Semaphore."""

# Attach the routers.
app.include_router(internal_router)
app.include_router(external_router, prefix=config.path_prefix)
app.include_router(v1_router, prefix=f"{config.path_prefix}/v1")

# Add middleware.
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

# Add exception handlers.
app.exception_handler(ClientRequestError)(client_request_error_handler)

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
