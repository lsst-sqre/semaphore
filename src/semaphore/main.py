"""Semaphore FastAPI application."""

from __future__ import annotations

from importlib.metadata import version

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from safir.dependencies.http_client import http_client_dependency
from safir.logging import configure_logging
from safir.middleware.x_forwarded import XForwardedMiddleware

from .config import config
from .dependencies.broadcastrepo import broadcast_repo_dependency
from .github.broadcastservices import bootstrap_broadcast_repo
from .handlers.external import external_router
from .handlers.internal import internal_router
from .handlers.v1 import v1_router

__all__ = ["app"]

configure_logging(
    profile=config.profile,
    log_level=config.log_level,
    name=config.logger_name,
)

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
    docs_url=f"/{config.name}/docs",
    redoc_url=f"/{config.name}/redoc",
    openapi_url=f"/{config.name}/openapi.json",
)
app.include_router(internal_router)
app.include_router(external_router)
app.include_router(v1_router)
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


@app.on_event("startup")
async def startup_event() -> None:
    logger = structlog.get_logger(config.logger_name)
    logger.info("Running startup")

    broadcast_repo = await broadcast_repo_dependency()
    if config.enable_github_app:
        await bootstrap_broadcast_repo(
            http_client=await http_client_dependency(),
            broadcast_repo=broadcast_repo,
            logger=logger,
        )


@app.on_event("shutdown")
async def shutdown_event() -> None:
    await http_client_dependency.aclose()
