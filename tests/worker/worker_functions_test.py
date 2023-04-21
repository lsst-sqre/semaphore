"""Test the ping worker function."""

from __future__ import annotations

from typing import Any

import pytest

from semaphore.worker.functions.ping import ping
from semaphore.worker.functions.send_message import send_message


@pytest.mark.asyncio
async def test_ping(worker_context: dict[Any, Any]) -> None:
    result = await ping(worker_context)
    assert result == "pong"


@pytest.mark.asyncio
async def test_message(worker_context: dict[Any, Any]) -> None:
    result = await send_message(worker_context, "test 1 2 3")
    assert result is None
