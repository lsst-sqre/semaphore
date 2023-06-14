"""A proof-of-concept worker function."""

from __future__ import annotations

from typing import Any, Dict

import structlog
from safir.slack.blockkit import SlackMessage
from safir.slack.webhook import SlackWebhookClient


async def send_message(
    ctx: Dict[Any, Any], webhook: str, message: str
) -> SlackMessage:
    logger = ctx["logger"].bind(task="send_message")
    logger.info("Running send_message")

    logger = structlog.get_logger(__name__)
    client = SlackWebhookClient(webhook, "Semaphore", logger)

    slack_message = SlackMessage(message=message)
    await client.post(slack_message)
    return slack_message
