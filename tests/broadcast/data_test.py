"""Tests for the semaphore.broadcast.data module."""

from __future__ import annotations

import datetime

import arrow
import pytest
from dateutil.rrule import DAILY, HOURLY, rrule, rruleset

from semaphore.broadcast.data import (
    BroadcastMessage,
    OneTimeBroadcastMessage,
    RepeatingBroadcastMessage,
)


def test_broadcastmessage_with_body() -> None:
    source_path = "broadcasts/demo.md"
    summary = "This is the **summary** content."
    body = (
        "This is the body of the message.\n"
        "\n"
        "The body is also markdown-formatted.\n"
    )
    m = BroadcastMessage(
        source_path=source_path, summary_md=summary, body_md=body
    )
    assert m.source_path == source_path
    assert m.summary_md == summary
    assert m.body_md == body
    assert m.active is True


def test_broadcastmessage_without_body() -> None:
    source_path = "broadcasts/demo.md"
    summary = "This is the **summary** content."
    m = BroadcastMessage(
        source_path=source_path, summary_md=summary, body_md=None
    )
    assert m.source_path == source_path
    assert m.summary_md == summary
    assert m.body_md is None
    assert m.active is True


def test_onetimebroadcastmessage_active() -> None:
    """Test a OneTimeBroadcast message that is currently active."""
    source_path = "broadcasts/demo.md"
    summary = "This is the **summary** content."
    start, end = arrow.utcnow().shift(minutes=-1).span("hour")
    m = OneTimeBroadcastMessage(
        source_path=source_path,
        summary_md=summary,
        body_md=None,
        defer=start,
        expire=end,
    )
    assert m.source_path == source_path
    assert m.summary_md == summary
    assert m.body_md is None
    assert m.defer == start
    assert m.expire == end
    assert m.active is True


def test_onetimebroadcastmessage_past() -> None:
    """Test a OneTimeBroadcast message that was in the past."""
    source_path = "broadcasts/demo.md"
    summary = "This is the **summary** content."
    start, end = arrow.utcnow().shift(hours=-1).span("minute")
    m = OneTimeBroadcastMessage(
        source_path=source_path,
        summary_md=summary,
        body_md=None,
        defer=start,
        expire=end,
    )
    assert m.source_path == source_path
    assert m.summary_md == summary
    assert m.body_md is None
    assert m.defer == start
    assert m.expire == end
    assert m.active is False


def test_onetimebroadcastmessage_future() -> None:
    """Test a OneTimeBroadcast message that is in the future."""
    source_path = "broadcasts/demo.md"
    summary = "This is the **summary** content."
    start, end = arrow.utcnow().shift(hours=1).span("minute")
    m = OneTimeBroadcastMessage(
        source_path=source_path,
        summary_md=summary,
        body_md=None,
        defer=start,
        expire=end,
    )
    assert m.source_path == source_path
    assert m.summary_md == summary
    assert m.body_md is None
    assert m.defer == start
    assert m.expire == end
    assert m.active is False


def test_repeating_active() -> None:
    """Test a RepeatingBroadcastMessage that should be currently active."""
    source_path = "broadcasts/demo.md"
    summary = "This is the **summary** content."

    start = arrow.utcnow().floor("second").shift(minutes=-10)
    ttl = datetime.timedelta(hours=1)
    rset = rruleset(cache=True)
    rset.rrule(rrule(freq=DAILY, dtstart=start.datetime))
    m = RepeatingBroadcastMessage.from_rruleset(
        rruleset=rset,
        ttl=ttl,
        source_path=source_path,
        summary_md=summary,
        body_md=None,
    )
    assert m.source_path == source_path
    assert m.summary_md == summary
    assert m.body_md is None
    assert m.defer == start
    assert m.expire == start.shift(hours=1)
    assert m.active is True


def test_repeating_noevents_on_init() -> None:
    """Test a RepeatingBroadcastMessage that can never be scheduled."""
    source_path = "broadcasts/demo.md"
    summary = "This is the **summary** content."

    # No future events because of the repeat count being limited
    start = arrow.utcnow().floor("second").shift(hours=-10)
    ttl = datetime.timedelta(hours=1)
    rset = rruleset(cache=True)
    rset.rrule(rrule(freq=HOURLY, dtstart=start.datetime, count=2))
    with pytest.raises(ValueError):
        RepeatingBroadcastMessage.from_rruleset(
            rruleset=rset,
            ttl=ttl,
            source_path=source_path,
            summary_md=summary,
            body_md=None,
        )


def test_repeating_future() -> None:
    """Test a RepeatingBroadcastMessage that will display in the future."""
    source_path = "broadcasts/demo.md"
    summary = "This is the **summary** content."

    # Set the start date to 10 hours ago, repeating daily with a TTL of 1 hr
    # The next event is 1 day from now.
    start = arrow.utcnow().floor("second").shift(hours=-10)
    ttl = datetime.timedelta(hours=1)
    rset = rruleset(cache=True)
    rset.rrule(rrule(freq=DAILY, dtstart=start.datetime))
    m = RepeatingBroadcastMessage.from_rruleset(
        rruleset=rset,
        ttl=ttl,
        source_path=source_path,
        summary_md=summary,
        body_md=None,
    )
    assert m.source_path == source_path
    assert m.summary_md == summary
    assert m.body_md is None
    assert m.defer == start.shift(hours=24)
    assert m.expire == start.shift(hours=25)
    assert m.active is False


def test_repeating_propose_next() -> None:
    """Test a RepeatingBroadcastMessage that needs to propose a next defer
    time.
    """
    source_path = "broadcasts/demo.md"
    summary = "This is the **summary** content."

    # Build this RepeatingBroadcastMessage manually, without
    # from_rruleset, in order to trigger a state where the last
    # "defer" and "expire" window was in the past.
    now = arrow.utcnow().floor("second")
    ttl = datetime.timedelta(hours=1)

    rset = rruleset(cache=True)
    rset.rrule(rrule(freq=DAILY, dtstart=now.shift(days=-2).datetime))

    orig_defer = now.shift(days=-1)
    orig_expire = orig_defer.shift(seconds=ttl.total_seconds())

    m = RepeatingBroadcastMessage(
        rruleset=rset,
        ttl=ttl,
        source_path=source_path,
        summary_md=summary,
        body_md=None,
        defer=orig_defer,
        expire=orig_expire,
    )
    assert m.source_path == source_path
    assert m.summary_md == summary
    assert m.body_md is None
    assert m.active is True
    assert m.defer == now
    assert m.expire == now.shift(seconds=ttl.total_seconds())
