"""Arq-based queue worker lifecycle configuration."""

from __future__ import annotations

import uuid
from typing import Any, Dict

import httpx
import structlog
from safir.logging import configure_logging

from semaphore.config import config
from semaphore.dependencies.redis import redis_dependency

from .functions import ping


async def startup(ctx: Dict[Any, Any]) -> None:
    """Runs during working start-up to set up the worker context."""
    configure_logging(
        profile=config.profile,
        log_level=config.log_level,
        name="semaphore",
    )
    logger = structlog.get_logger("semaphore")
    # The instance key uniquely identifies this worker in logs
    instance_key = uuid.uuid4().hex
    logger = logger.bind(worker_instance=instance_key)

    logger.info("Starting up worker")

    http_client = httpx.AsyncClient()
    ctx["http_client"] = http_client

    ctx["logger"] = logger
    logger.info("Start up complete")
    await redis_dependency.initialize(config.redis_url)


async def shutdown(ctx: Dict[Any, Any]) -> None:
    """Runs during worker shut-down to resources."""
    if "logger" in ctx.keys():
        logger = ctx["logger"]
    else:
        logger = structlog.get_logger(__name__)
    logger.info("Running worker shutdown.")

    await redis_dependency.close()

    try:
        await ctx["http_client"].aclose()
    except Exception as e:
        logger.warning("Issue closing the http_client: %s", str(e))

    logger.info("Worker shutdown complete.")


class WorkerSettings:
    """Configuration for a Times Square arq worker.
    See `arq.worker.Worker` for details on these attributes.
    """

    functions = [
        ping,
    ]

    redis_settings = config.arq_redis_settings

    on_startup = startup

    on_shutdown = shutdown
