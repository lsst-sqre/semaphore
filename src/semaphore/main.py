"""Semaphore FastAPI application."""

from __future__ import annotations

from importlib.metadata import metadata

from fastapi import FastAPI
from safir.dependencies.http_client import http_client_dependency
from safir.logging import configure_logging
from safir.middleware.x_forwarded import XForwardedMiddleware

from .config import config
from .handlers.external import external_router
from .handlers.internal import internal_router

__all__ = ["app"]

configure_logging(
    profile=config.profile,
    log_level=config.log_level,
    name=config.logger_name,
)

app = FastAPI(
    title="Semaphore",
    description=metadata("semaphore").get("Summary", ""),
    version=metadata("semaphore").get("Version", "0.0.0"),
)
app.include_router(internal_router)
app.include_router(external_router)


@app.on_event("startup")
async def startup_event() -> None:
    app.add_middleware(XForwardedMiddleware)


@app.on_event("shutdown")
async def shutdown_event() -> None:
    await http_client_dependency.aclose()
