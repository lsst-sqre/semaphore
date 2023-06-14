"""Test the ping worker function."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from safir.testing.slack import MockSlackWebhook

from semaphore.config import config
from semaphore.worker.functions.ping import ping
from semaphore.worker.functions.send_message import send_message


@pytest.mark.asyncio
async def test_ping(worker_context: dict[Any, Any]) -> None:
    result = await ping(worker_context)
    assert result == "pong"


# Functional test, requires defined webhook URL in tox.ini
@pytest.mark.asyncio
async def test_message(worker_context: dict[Any, Any]) -> None:
    message = "test 1 2 3"
    result = await send_message(
        worker_context, config.slack_webhook.get_secret_value(), message
    )
    assert result is True


# Non-functional mock webhook test
@pytest.mark.asyncio
async def test_something(
    worker_context: dict[Any, Any],
    client: AsyncClient,
    mock_slack: MockSlackWebhook,
) -> None:
    # Do something with client that generates Slack messages.
    await send_message(worker_context, mock_slack, "test 1 2 3")
    mock_slack.post_webhook()

    assert mock_slack.messages == "hi"
