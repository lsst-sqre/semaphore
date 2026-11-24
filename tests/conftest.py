"""Pytest fixtures."""

from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
import pytest_asyncio
import structlog
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pytest_httpx import HTTPXMock
from safir.database import (
    create_database_engine,
    initialize_database,
    stamp_database_async,
)
from safir.testing.data import Data
from sqlalchemy.ext.asyncio import AsyncEngine

from semaphore import main
from semaphore.broadcast.repository import BroadcastMessageRepository
from semaphore.config import config
from semaphore.dependencies.broadcastrepo import broadcast_repo_dependency
from semaphore.dependencies.reloadqueue import reload_queue_dependency
from semaphore.dependencies.repolocks import repo_locks_dependency
from semaphore.github import webhooks
from semaphore.github.reloadqueue import ReloadQueue
from semaphore.github.repolocks import RepoLocks
from semaphore.schema import SchemaBase

from .support.constants import TEST_BASE_URL
from .support.github import GitHubMock, configure_github_app


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
async def app(
    empty_database: None,
    broadcast_repo: BroadcastMessageRepository,
    repo_locks: RepoLocks,
    reload_queue: ReloadQueue,
) -> AsyncGenerator[FastAPI]:
    """Return a configured test application.

    Wraps the application in a lifespan manager so that startup and shutdown
    events are sent during test execution. Each test starts with an empty
    broadcast repository, its own per-repository locks and an empty reload
    queue.
    """
    async with LifespanManager(main.app):
        yield main.app


@pytest.fixture
def broadcast_repo(
    monkeypatch: pytest.MonkeyPatch,
) -> BroadcastMessageRepository:
    """Give the app an empty broadcast repository for the test.

    Returns the repository, so a test can inspect what the app loaded.
    """
    repo = BroadcastMessageRepository()
    monkeypatch.setattr(broadcast_repo_dependency, "repo", repo)
    return repo


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
async def github_app_client(
    app: FastAPI, github_mock: GitHubMock, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[AsyncClient]:
    """Client for the app with the GitHub App enabled.

    The allowlist is `tests.support.github.GITHUB_REPO`. The GitHub App is
    enabled after the app starts, so the startup bootstrap from GitHub does
    not run. Webhooks are processed without the delay that lets GitHub reach
    consistency.
    """
    configure_github_app(monkeypatch)
    monkeypatch.setattr(webhooks, "GITHUB_CONSISTENCY_DELAY", 0)
    async with AsyncClient(
        transport=ASGITransport(app), base_url=TEST_BASE_URL
    ) as client:
        yield client


@pytest.fixture
def github_mock(httpx_mock: HTTPXMock) -> GitHubMock:
    """Mock the GitHub API.

    Any request to GitHub that the test does not mock fails the test.
    """
    return GitHubMock(httpx_mock)


@pytest.fixture
def reload_queue(monkeypatch: pytest.MonkeyPatch) -> ReloadQueue:
    """Give the app a new, empty reload queue for the test.

    Returns the queue, so a test can inspect what the app queued. As with
    `repo_locks`, the queue's asyncio events are bound to the event loop
    that first waits on them, so queues are not shared between tests.
    """
    queue = ReloadQueue()
    monkeypatch.setattr(reload_queue_dependency, "queue", queue)
    return queue


@pytest.fixture
def repo_locks(monkeypatch: pytest.MonkeyPatch) -> RepoLocks:
    """Give the app new per-repository locks for the test.

    An asyncio lock is bound to the event loop that first waits on it, and
    each test runs in its own event loop, so locks are not shared between
    tests.
    """
    locks = RepoLocks()
    monkeypatch.setattr(repo_locks_dependency, "locks", locks)
    return locks


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
