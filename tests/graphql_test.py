"""Tests for the /graphql API."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from httpx import AsyncClient


@pytest.mark.asyncio
async def test_graphql(client: AsyncClient) -> None:
    """Demo test of the graphql endpoint."""
    r = await client.post(
        "/semaphore/graphql", json={"query": "{ books { title, author} }"}
    )
    assert r.status_code == 200
    data = r.json()
    assert data["data"]["books"][0] == {
        "title": "The Great Gatsby",
        "author": "F. Scott Fitzgerald",
    }
