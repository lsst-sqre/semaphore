"""Pytest fixtures."""

from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
import pytest_asyncio
import structlog
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from safir.database import (
    create_database_engine,
    initialize_database,
    stamp_database_async,
)
from safir.testing.data import Data

from semaphore import main
from semaphore.config import config
from semaphore.schema import SchemaBase


@pytest_asyncio.fixture
async def app(empty_database: None) -> AsyncGenerator[FastAPI]:
    """Return a configured test application.

    Wraps the application in a lifespan manager so that startup and shutdown
    events are sent during test execution.
    """
    async with LifespanManager(main.app):
        yield main.app


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncGenerator[AsyncClient]:
    """Return an ``httpx.AsyncClient`` configured to talk to the test app."""
    async with AsyncClient(
        transport=ASGITransport(app), base_url="https://example.com/"
    ) as client:
        yield client


@pytest.fixture
def data() -> Data:
    return Data(Path(__file__).parent / "data")


@pytest_asyncio.fixture
async def empty_database() -> None:
    """Empty the database before a test."""
    logger = structlog.get_logger("semaphore")
    engine = create_database_engine(
        config.database_url, config.database_password
    )
    base = SchemaBase.metadata
    await initialize_database(engine, logger, schema=base, reset=True)
    await stamp_database_async(engine)
    await engine.dispose()
