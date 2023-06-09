"""Pytest fixtures."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
import structlog
from asgi_lifespan import LifespanManager
from httpx import AsyncClient

from semaphore import main

if TYPE_CHECKING:
    from typing import AsyncIterator

    from fastapi import FastAPI


@pytest_asyncio.fixture
async def app() -> AsyncIterator[FastAPI]:
    """Return a configured test application.

    Wraps the application in a lifespan manager so that startup and shutdown
    events are sent during test execution.
    """
    async with LifespanManager(main.app):
        yield main.app


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Return an ``httpx.AsyncClient`` configured to talk to the test app."""
    async with AsyncClient(app=app, base_url="https://example.com/") as client:
        yield client


@pytest.fixture
def broadcasts_dir() -> Path:
    """Directory containing test broadcast markdown messages."""
    return Path(__file__).parent.joinpath("data/broadcasts")


@pytest.fixture
def worker_context() -> dict[Any, Any]:
    """A mock ctx (context) fixture for arq workers."""
    ctx: dict[Any, Any] = {}

    # Prep logger
    logger = structlog.get_logger("semaphore")
    ctx["logger"] = logger

    return ctx
