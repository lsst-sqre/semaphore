"""Test the semaphore.broadcast.repository module."""

from __future__ import annotations

import arrow
import pytest

from semaphore.broadcast.models import (
    BroadcastMessage,
    FixedExpirationScheduler,
    OpenEndedScheduler,
    PermaScheduler,
)
from semaphore.broadcast.repository import (
    BroadcastMessageRepository,
    NotFoundError,
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


def test_repository() -> None:
    repo = BroadcastMessageRepository([create_active_message("1")])

    assert "1" in repo

    active_message = repo.get("1")
    assert active_message.identifier == "1"

    assert "2" not in repo
    with pytest.raises(NotFoundError):
        repo.get("2")

    repo.add(create_future_message("2"))

    assert "2" in repo

    active_message_ids = set([m.identifier for m in repo.iter_active()])
    assert active_message_ids == set(["1"])

    pending_message_ids = set([m.identifier for m in repo.iter_pending()])
    assert pending_message_ids == set(["2"])

    stale_message_ids = set([m.identifier for m in repo.iter_stale()])
    assert len(stale_message_ids) == 0

    repo.add(create_stale_message("3"))

    stale_message_ids = set([m.identifier for m in repo.iter_stale()])
    assert stale_message_ids == set(["3"])

    repo.remove("3")
    assert len([m for m in repo.iter_stale()]) == 0

    repo.remove("3")  # does nothing, but doesn't raise by default either

    with pytest.raises(NotFoundError):
        repo.remove("3", raise_if_missing=True)
