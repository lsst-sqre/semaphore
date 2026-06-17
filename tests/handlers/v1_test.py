"""Tests for the semaphore v1 API."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from safir.testing.data import Data

from semaphore.dependencies.broadcastrepo import broadcast_repo_dependency

from ..support.broadcasts import create_active_message, create_disabled_message
from ..support.constants import TEST_BASE_URL


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


@pytest.mark.asyncio
async def test_user_notification(
    data: Data, admin_client: AsyncClient, user_client: AsyncClient
) -> None:
    start = datetime.now(tz=UTC).replace(microsecond=0)

    # Send the message. This should succeed and redirect the user to the
    # specific message within the list of admin notifications. The returned
    # notification should fill out some additional details, such as the
    # message ID, sender, and creation date, but the message should still be
    # unformatted.
    r = await admin_client.post(
        "/semaphore/v1/admin/notifications",
        json=data.read_json("notifications/example"),
    )
    assert r.status_code == 200
    notification_url = r.headers["Location"]
    sent = r.json()
    data.assert_json_matches(sent, "api/sent")
    created = datetime.fromisoformat(sent["created"])
    assert start <= created <= start + timedelta(seconds=2)
    message_id = sent["id"]
    expected_url = "semaphore/v1/admin/notifications"
    assert notification_url == f"{TEST_BASE_URL}{expected_url}/{message_id}"
    assert sent["url"] == notification_url

    # Retrieve the sent notification. This should be the same but without the
    # url key.
    r = await admin_client.get(notification_url)
    assert r.status_code == 200
    assert r.json() == {k: v for k, v in sent.items() if k != "url"}

    # Retrieving the list of all admin notifications should return a list
    # containing just that notification.
    r = await admin_client.get("/semaphore/v1/admin/notifications")
    assert r.status_code == 200
    assert r.json() == [sent]

    # Retrieving messages for the recipient user should also retrieve a list
    # of message summaries containing that message, with the summary
    # formatted and a URL to the full message.
    r = await user_client.get("/semaphore/v1/notifications")
    assert r.status_code == 200
    notifications = r.json()
    data.assert_json_matches(notifications, "api/notifications")
    assert datetime.fromisoformat(notifications[0]["created"]) == created

    # The individual message should also be retrievable by ID. This should
    # include the full formatted body.
    notification_url = notifications[0]["url"]
    r = await user_client.get(notification_url)
    assert r.status_code == 200
    notification = r.json()
    data.assert_json_matches(notification, "api/notification-one")
    assert datetime.fromisoformat(notification["created"]) == created

    # The admin user should not see the notification for the regular user.
    r = await admin_client.get("/semaphore/v1/notifications")
    assert r.status_code == 200
    assert r.json() == []
    r = await admin_client.get(notification_url)
    assert r.status_code == 404
