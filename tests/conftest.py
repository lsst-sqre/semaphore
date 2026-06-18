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
from sqlalchemy.ext.asyncio import AsyncEngine

from semaphore import main
from semaphore.config import config
from semaphore.schema import SchemaBase

from .support.constants import TEST_BASE_URL


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--update-test-data",
        action="store_true",
        default=False,
        help="Overwrite expected test output with current results",
    )


@pytest_asyncio.fixture
async def admin_client(app: FastAPI) -> AsyncGenerator[AsyncClient]:
    """Client authenticated as an admin user."""
    async with AsyncClient(
        base_url=TEST_BASE_URL,
        headers={"X-Auth-Request-User": "admin"},
        transport=ASGITransport(app=app),
    ) as client:
        yield client


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
        transport=ASGITransport(app), base_url=TEST_BASE_URL
    ) as client:
        yield client


@pytest.fixture
def data(request: pytest.FixtureRequest) -> Data:
    update = request.config.getoption("--update-test-data")
    return Data(Path(__file__).parent / "data", update_test_data=update)


@pytest_asyncio.fixture
async def empty_database(engine: AsyncEngine) -> None:
    """Empty the database before a test."""
    logger = structlog.get_logger("semaphore")
    base = SchemaBase.metadata
    await initialize_database(engine, logger, schema=base, reset=True)
    await stamp_database_async(engine)


@pytest_asyncio.fixture
async def engine() -> AsyncGenerator[AsyncEngine]:
    engine = create_database_engine(
        config.database_url, config.database_password
    )
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def service_client(app: FastAPI) -> AsyncGenerator[AsyncClient]:
    """Client authenticated as a service user."""
    async with AsyncClient(
        base_url=TEST_BASE_URL,
        headers={"X-Auth-Request-User": "bot-service"},
        transport=ASGITransport(app=app),
    ) as client:
        yield client


@pytest_asyncio.fixture
async def user_client(app: FastAPI) -> AsyncGenerator[AsyncClient]:
    """Client authenticated as a regular user."""
    async with AsyncClient(
        base_url=TEST_BASE_URL,
        headers={"X-Auth-Request-User": "some-user"},
        transport=ASGITransport(app=app),
    ) as client:
        yield client
