"""Test utilities for creating sample broadcasts."""

from __future__ import annotations

import arrow

from semaphore.broadcast.models import (
    BroadcastMessage,
    FixedExpirationScheduler,
    OpenEndedScheduler,
    PermaScheduler,
)


def create_active_message(identifier: str) -> BroadcastMessage:
    return BroadcastMessage(
        identifier=identifier,
        summary_md="I'm an active message.",
        body_md=None,
        scheduler=PermaScheduler(),
    )


def create_future_message(identifier: str) -> BroadcastMessage:
    start = arrow.utcnow().shift(hours=1)
    return BroadcastMessage(
        identifier=identifier,
        summary_md="I'm an future message.",
        body_md=None,
        scheduler=OpenEndedScheduler(start),
    )


def create_stale_message(identifier: str) -> BroadcastMessage:
    end = arrow.utcnow().shift(hours=-1)
    return BroadcastMessage(
        identifier=identifier,
        summary_md="I'm an stale message.",
        body_md=None,
        scheduler=FixedExpirationScheduler(end),
    )
