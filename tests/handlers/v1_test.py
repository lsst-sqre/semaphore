"""Tests for the semaphore v1 API."""

from typing import Any

import pytest
from httpx import AsyncClient

from semaphore.dependencies.broadcastrepo import broadcast_repo_dependency

from ..support.broadcasts import create_active_message, create_disabled_message


async def get_broadcasts(client: AsyncClient) -> dict[str, Any]:
    response = await client.get("/semaphore/v1/broadcasts")
    assert response.status_code == 200
    return response.json()


@pytest.mark.asyncio
async def test_get_broadcasts(client: AsyncClient) -> None:
    """Test ``GET /semaphore/v1/broadcasts."""
    broadcast_repo = await broadcast_repo_dependency()

    data = await get_broadcasts(client)
    assert len(data) == 0

    broadcast_repo.add(create_active_message("1"))

    data = await get_broadcasts(client)
    assert len(data) == 1

    message_2 = create_disabled_message("2")
    assert message_2.active is False
    broadcast_repo.add(message_2)

    data = await get_broadcasts(client)
    assert len(data) == 1
