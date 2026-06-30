"""Tests for the semaphore v1 API."""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import ANY

import pytest
from httpx import AsyncClient, Response
from safir.database import create_async_session, datetime_to_db
from safir.http import PaginationLinkData
from safir.testing.data import Data
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine

from semaphore.dependencies.broadcastrepo import broadcast_repo_dependency
from semaphore.schema import UserNotification as SQLUserNotification

from ..support.broadcasts import create_active_message, create_disabled_message
from ..support.constants import TEST_BASE_URL


async def get_broadcasts(client: AsyncClient) -> dict[str, Any]:
    response = await client.get("/semaphore/v1/broadcasts")
    assert response.status_code == 200
    return response.json()


def assert_http_response(r: Response, expected_status: int) -> None:
    """Check that an HTTP response matches expectations.

    Parameters
    ----------
    r
        Response to check.
    expected_status
        Expected status code.
    """
    assert r.status_code == expected_status, f"Failed response body: {r.text}"


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
async def test_notification(
    *,
    data: Data,
    client: AsyncClient,
    admin_client: AsyncClient,
    user_client: AsyncClient,
) -> None:
    start = datetime.now(tz=UTC).replace(microsecond=0)

    # Send the message. This should succeed and direct the admin to the
    # specific message within the list of admin notifications. The returned
    # notification should fill out some additional details, such as the
    # message ID, sender, and creation date, but the message should still be
    # unformatted.
    r = await admin_client.post(
        "/semaphore/v1/admin/notifications",
        json=data.read_json("notifications/create-example"),
    )
    assert_http_response(r, 200)
    sent = r.json()
    data.assert_json_matches(sent, "notifications/admin-example")
    created = datetime.fromisoformat(sent["created"])
    assert start <= created <= start + timedelta(seconds=2)
    message_id = sent["id"]
    expected_url = "semaphore/v1/admin/notifications"
    notification_url = r.headers["Location"]
    assert notification_url == f"{TEST_BASE_URL}{expected_url}/{message_id}"
    assert sent["url"] == notification_url

    # Retrieve the sent notification. This should be the same but without the
    # url key.
    r = await admin_client.get(notification_url)
    assert_http_response(r, 200)
    assert r.json() == {k: v for k, v in sent.items() if k != "url"}

    # Retrieving the list of all admin notifications should return a list
    # containing just that notification.
    r = await admin_client.get("/semaphore/v1/admin/notifications")
    assert_http_response(r, 200)
    assert r.json() == [sent]

    # Retrieving messages for the recipient user should also retrieve a list
    # of message summaries containing that message, with the summary
    # formatted and a URL to the full message.
    r = await user_client.get("/semaphore/v1/notifications")
    assert_http_response(r, 200)
    notifications = r.json()
    data.assert_json_matches(notifications, "notifications/user-one")
    assert datetime.fromisoformat(notifications[0]["created"]) == created

    # The individual message should also be retrievable by ID. This should
    # include the full formatted body.
    notification_url = notifications[0]["url"]
    r = await user_client.get(notification_url)
    assert_http_response(r, 200)
    notification = r.json()
    data.assert_json_matches(notification, "notifications/user-example")
    assert datetime.fromisoformat(notification["created"]) == created

    # The admin user should not see the notification for the regular user.
    r = await admin_client.get("/semaphore/v1/notifications")
    assert_http_response(r, 200)
    assert r.json() == []
    r = await admin_client.get(notification_url)
    assert_http_response(r, 404)

    # Services should not be able to see any of these notifications.
    r = await client.get(
        "/semaphore/v1/services/bot-service/notifications",
        headers={"X-Auth-Request-User": "bot-service"},
    )
    assert_http_response(r, 200)
    assert r.json() == []


@pytest.mark.asyncio
async def test_notification_read(
    data: Data, admin_client: AsyncClient, user_client: AsyncClient
) -> None:
    to_send = data.read_json("notifications/create-multiple")
    for notification in to_send:
        r = await admin_client.post(
            "/semaphore/v1/admin/notifications",
            json=notification,
        )
        assert_http_response(r, 200)

    # Listing all unread notifications should return all three.
    r = await user_client.get(
        "/semaphore/v1/notifications", params={"unread": "true"}
    )
    assert_http_response(r, 200)
    notifications = r.json()
    data.assert_json_matches(notifications, "notifications/user-multiple")

    # Mark the first two as read.
    ids = {"ids": [notifications[0]["id"], notifications[1]["id"]]}
    start = datetime.now(tz=UTC).replace(microsecond=0)
    r = await user_client.post("/semaphore/v1/notifications/read", json=ids)
    assert_http_response(r, 204)

    # Now, listing all unread notifications returns only the third.
    r = await user_client.get(
        "/semaphore/v1/notifications", params={"unread": "true"}
    )
    assert_http_response(r, 200)
    assert r.json() == notifications[2:]

    # Listing all notifications still returns all three, but now with read
    # timestamps.
    r = await user_client.get("/semaphore/v1/notifications")
    assert_http_response(r, 200)
    updated_notifications = r.json()
    notifications[0]["read"] = ANY
    notifications[1]["read"] = ANY
    assert updated_notifications == notifications
    for index in (0, 1):
        read = datetime.fromisoformat(updated_notifications[index]["read"])
        assert start <= read <= start + timedelta(seconds=2)


@pytest.mark.asyncio
async def test_notification_service(
    *,
    data: Data,
    client: AsyncClient,
    service_client: AsyncClient,
    user_client: AsyncClient,
) -> None:
    start = datetime.now(tz=UTC).replace(microsecond=0)
    example = data.read_json("notifications/service-example")
    sender = example["sender"]
    base_url = f"semaphore/v1/services/{sender}/notifications"

    # Send the message. This should succeed and redirect the user to the
    # specific message within the list of admin notifications. The returned
    # notification should fill out some additional details, such as the
    # message ID, sender, and creation date, but the message should still be
    # unformatted.
    r = await service_client.post(
        base_url,
        json=data.read_json("notifications/create-example"),
    )
    assert_http_response(r, 200)
    sent = r.json()
    data.assert_json_matches(sent, "notifications/service-example")
    created = datetime.fromisoformat(sent["created"])
    assert start <= created <= start + timedelta(seconds=2)
    message_id = sent["id"]
    sender = sent["sender"]
    recipient = sent["recipient"]
    notification_url = r.headers["Location"]
    assert notification_url == f"{TEST_BASE_URL}{base_url}/{message_id}"
    assert sent["url"] == notification_url

    # Retrieve the sent notification. This should be the same but without the
    # url key.
    r = await service_client.get(notification_url)
    assert_http_response(r, 200)
    assert r.json() == {k: v for k, v in sent.items() if k != "url"}

    # Retrieving the list of all notifications sent by that service should
    # return a list containing just that notification.
    r = await service_client.get(base_url)
    assert_http_response(r, 200)
    assert r.json() == [sent]

    # The message should then appear for the user.
    r = await user_client.get("/semaphore/v1/notifications")
    assert_http_response(r, 200)
    notifications = r.json()
    data.assert_json_matches(notifications, "notifications/user-one")
    assert datetime.fromisoformat(notifications[0]["created"]) == created

    # Retrieving messages for the wrong service should return a 403 error.
    other_url = "/semaphore/v1/services/bot-other/notifications"
    r = await service_client.get(other_url)
    assert_http_response(r, 403)
    r = await service_client.get(other_url + "/1")
    assert_http_response(r, 403)

    # Posting messages for the wrong service should return a 403 error.
    r = await service_client.post(
        other_url,
        json=data.read_json("notifications/create-example"),
    )
    assert_http_response(r, 403)

    # Authenticating as a user to a service URL should fail with a 403 error.
    user_url = f"/semaphore/v1/services/{recipient}/notifications"
    r = await client.get(user_url, headers={"X-Auth-Request-User": recipient})
    assert_http_response(r, 403)


async def setup_paginate_test(
    data: Data,
    client: AsyncClient,
    engine: AsyncEngine,
    *,
    as_service: bool = False,
) -> None:
    """Create some notifications to test pagination.

    Parameters
    ----------
    data
        Test data management object.
    admin_client
        Client that authenticates as an admin.
    engine
        Database engine.
    as_service
        If `True`, post messages as a service instead of as an admin.
    """
    to_send = data.read_json("notifications/create-pagination")
    if as_service:
        expected = data.read_json("notifications/service-pagination")
        sender = expected[0]["sender"]
        url = f"/semaphore/v1/services/{sender}/notifications"
    else:
        url = "/semaphore/v1/admin/notifications"
    for notification in to_send:
        r = await client.post(url, json=notification)
        assert_http_response(r, 200)

    # Adjust the creation timestamps to match the expected data.
    if as_service:
        expected = data.read_json("notifications/service-pagination")
    else:
        expected = data.read_json("notifications/admin-pagination")
    session = await create_async_session(engine)
    async with session.begin():
        for notification in expected:
            id = int(notification["id"])
            created = datetime.fromisoformat(notification["created"])
            await session.execute(
                update(SQLUserNotification)
                .where(SQLUserNotification.id == id)
                .values(created=datetime_to_db(created))
            )


async def check_pagination(
    client: AsyncClient,
    url: str,
    notifications: list[dict[str, Any]],
    *,
    params: dict[str, str] | None = None,
) -> None:
    """Test pagination of an API.

    Parameters
    ----------
    client
        Client to use to make the request.
    url
        URL to which to make the request.
    notifications
        Expected complete results in JSON-serialized form.
    params
        Additional parameters to pass to the request.
    """
    params = params.copy() if params else {}

    r = await client.get(url, params={**params, "limit": "1"})
    assert_http_response(r, 200)

    # Check the first reply, which should contain only the first element.
    if len(notifications) > 0:
        assert r.json() == [notifications[0]]
    else:
        assert r.json() == []
    assert r.headers["X-Total-Count"] == str(len(notifications))

    # Parse the Link header if expecting more than one result. Otherwise,
    # there should be no Link header.
    links = PaginationLinkData.from_header(r.headers["Link"])
    if len(notifications) <= 1:
        assert not links.next_url
        return
    assert links.next_url

    # Get the next batch of one.
    r = await client.get(links.next_url)
    assert_http_response(r, 200)
    assert r.json() == [notifications[1]]
    assert r.headers["X-Total-Count"] == str(len(notifications))

    # Get the previous page, which should just be the first notification.
    links = PaginationLinkData.from_header(r.headers["Link"])
    assert links.prev_url
    r = await client.get(links.prev_url)
    assert_http_response(r, 200)
    assert r.json() == [notifications[0]]

    # If there are more notifications, get the next page.
    if len(notifications) > 2:
        assert links.next_url
        r = await client.get(links.next_url)
        assert_http_response(r, 200)
        assert r.json() == [notifications[2]]
    else:
        assert not links.next_url


@pytest.mark.asyncio
async def test_notification_admin_paginate(
    data: Data, admin_client: AsyncClient, engine: AsyncEngine
) -> None:
    await setup_paginate_test(data, admin_client, engine)

    # Retrieve all of the notifications.
    r = await admin_client.get("/semaphore/v1/admin/notifications")
    assert_http_response(r, 200)
    notifications = r.json()
    data.assert_json_matches(notifications, "notifications/admin-pagination")

    # Retrieve the notifications one at a time to test pagination. Limit to
    # the sender, which should produce the same results (all the notifications
    # were sent by the same user), but helps check that the per-notification
    # URLs don't include the search string.
    await check_pagination(
        admin_client,
        "/semaphore/v1/admin/notifications",
        notifications,
        params={"sender": notifications[0]["sender"]},
    )

    # Restrict to a specific user, which should return only one result.
    await check_pagination(
        admin_client,
        "/semaphore/v1/admin/notifications",
        [notifications[0]],
        params={"recipient": notifications[0]["recipient"]},
    )

    # Request the first two notifications by created date.
    r = await admin_client.get(
        "/semaphore/v1/admin/notifications",
        params={"since": notifications[1]["created"]},
    )
    assert_http_response(r, 200)
    assert r.json() == notifications[:2]

    # Request the last two notifications by created date.
    r = await admin_client.get(
        "/semaphore/v1/admin/notifications",
        params={"until": notifications[1]["created"]},
    )
    assert_http_response(r, 200)
    assert r.json() == notifications[1:]


@pytest.mark.asyncio
async def test_notification_user_paginate(
    *,
    data: Data,
    admin_client: AsyncClient,
    user_client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    await setup_paginate_test(data, admin_client, engine)

    # Retrieve all of the notifications.
    r = await user_client.get("/semaphore/v1/notifications")
    assert_http_response(r, 200)
    notifications = r.json()
    data.assert_json_matches(notifications, "notifications/user-pagination")

    # Retrieve the notifications one at a time to test pagination.
    await check_pagination(
        user_client, "/semaphore/v1/notifications", notifications
    )


@pytest.mark.asyncio
async def test_notification_service_paginate(
    data: Data,
    service_client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    await setup_paginate_test(data, service_client, engine, as_service=True)

    # Retrieve all of the notifications.
    expected = data.read_json("notifications/service-pagination")
    sender = expected[0]["sender"]
    url = f"/semaphore/v1/services/{sender}/notifications"
    r = await service_client.get(url)
    assert_http_response(r, 200)
    notifications = r.json()
    data.assert_json_matches(notifications, "notifications/service-pagination")

    # Retrieve the notifications one at a time to test pagination.
    await check_pagination(service_client, url, notifications)

    # Restrict to a specific user and request pagination.
    await check_pagination(
        service_client,
        url,
        [notifications[0]],
        params={"recipient": notifications[0]["recipient"]},
    )

    # Request the first two notifications by created date.
    r = await service_client.get(
        url, params={"since": notifications[1]["created"]}
    )
    assert_http_response(r, 200)
    assert r.json() == notifications[:2]

    # Request the last two notifications by created date.
    r = await service_client.get(
        url, params={"until": notifications[1]["created"]}
    )
    assert_http_response(r, 200)
    assert r.json() == notifications[1:]
