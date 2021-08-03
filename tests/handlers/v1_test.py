"""Tests for the semaphore v1 API."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from semaphore.dependencies.broadcastrepo import broadcast_repo_dependency

from ..support.broadcasts import create_active_message

if TYPE_CHECKING:
    from httpx import AsyncClient


@pytest.mark.asyncio
async def test_get_broadcasts(client: AsyncClient) -> None:
    """Test ``GET /semaphore/v1/broadcasts."""
    broadcast_repo = broadcast_repo_dependency()

    response = await client.get("/semaphore/v1/broadcasts")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 0

    broadcast_repo.add(create_active_message("1"))

    response = await client.get("/semaphore/v1/broadcasts")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
