"""Tests for the semaphore.broadcast.data module."""

from __future__ import annotations

import datetime

import arrow
from dateutil.rrule import DAILY, HOURLY, rrule, rruleset

from semaphore.broadcast.data import (
    BroadcastMessage,
    FixedExpirationScheduler,
    OneTimeScheduler,
    OpenEndedScheduler,
    PermaScheduler,
    RecurringScheduler,
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
        source_path=source_path,
        summary_md=summary,
        body_md=body,
        scheduler=PermaScheduler(),
    )
    assert m.source_path == source_path
    assert m.summary_md == summary
    assert m.body_md == body
    assert m.active is True
    assert m.stale is False


def test_broadcastmessage_without_body() -> None:
    source_path = "broadcasts/demo.md"
    summary = "This is the **summary** content."
    m = BroadcastMessage(
        source_path=source_path,
        summary_md=summary,
        body_md=None,
        scheduler=PermaScheduler(),
    )
    assert m.source_path == source_path
    assert m.summary_md == summary
    assert m.body_md is None
    assert m.active is True
    assert m.stale is False


def test_broadcastmessage_disabled() -> None:
    m = BroadcastMessage(
        source_path="demo.md",
        summary_md="Summary",
        body_md=None,
        scheduler=PermaScheduler(),
        enabled=False,
    )
    assert m.active is False
    assert m.scheduler.is_active() is True


def test_onetimescheduler_active() -> None:
    """Test a OneTimeScheduler that is currently active."""
    start = arrow.utcnow().shift(minutes=-1)
    s = OneTimeScheduler.from_ttl(start, datetime.timedelta(hours=1))
    assert s.is_active() is True
    assert s.has_future_events() is False
    assert s.is_stale() is False


def test_fixedexpirationsscheduler_active() -> None:
    """Test a FixedExpirationSchduler that is currently active."""
    end = arrow.utcnow().shift(minutes=5)
    s = FixedExpirationScheduler(end)
    assert s.is_active() is True
    assert s.has_future_events() is False
    assert s.is_stale() is False


def test_fixedexpirationsscheduler_expired() -> None:
    """Test a FixedExpirationSchduler that has expired."""
    end = arrow.utcnow().shift(minutes=-5)
    s = FixedExpirationScheduler(end)
    assert s.is_active() is False
    assert s.has_future_events() is False
    assert s.is_stale() is True


def test_openendedscheduler_future() -> None:
    """Test an OpenEndedScheduled that is set for the future."""
    start = arrow.utcnow().shift(minutes=5)
    s = OpenEndedScheduler(start)
    assert s.is_active() is False
    assert s.has_future_events() is True
    assert s.is_stale() is False


def test_openendedcheduler_active() -> None:
    """Test an OpenEndedScheduler that is active."""
    start = arrow.utcnow().shift(minutes=-5)
    s = OpenEndedScheduler(start)
    assert s.is_active() is True
    assert s.has_future_events() is False
    assert s.is_stale() is False


def test_onetimescheduler_past() -> None:
    """Test a OneTimeScheduler that was in the past."""
    start = arrow.utcnow().shift(hours=-1)
    s = OneTimeScheduler.from_ttl(start, datetime.timedelta(minutes=1))
    assert s.is_active() is False
    assert s.has_future_events() is False
    assert s.is_stale() is True


def test_onetimescheduler_future() -> None:
    """Test a OneTimeScheduler that is in the future."""
    start = arrow.utcnow().shift(hours=1)
    s = OneTimeScheduler.from_ttl(start, datetime.timedelta(minutes=1))
    assert s.is_active() is False
    assert s.has_future_events() is True
    assert s.is_stale() is False


def test_recurring_active() -> None:
    """Test a RecurringScheduler that should be currently active."""
    start = arrow.utcnow().floor("second").shift(minutes=-10)
    ttl = datetime.timedelta(hours=1)
    rset = rruleset(cache=True)
    rset.rrule(rrule(freq=DAILY, dtstart=start.datetime))
    s = RecurringScheduler(rset, ttl)
    assert s._start == start
    assert s._end == start.shift(hours=1)
    assert s.is_active() is True
    assert s.has_future_events() is True
    assert s.is_stale() is False


def test_recurring_noevents() -> None:
    """Test a RecurringScheduler that has no future events."""
    # No future events because of the recurrence count being limited
    start = arrow.utcnow().floor("second").shift(hours=-10)
    ttl = datetime.timedelta(hours=1)
    rset = rruleset(cache=True)
    rset.rrule(rrule(freq=HOURLY, dtstart=start.datetime, count=2))
    s = RecurringScheduler(rset, ttl)
    assert s.is_active() is False
    assert s.has_future_events() is False
    assert s.is_stale() is True


def test_recurring_future() -> None:
    """Test a RecurringingScheduler that will display in the future."""
    # Set the start date to 10 hours ago, recurring daily with a TTL of 1 hr
    # The next event is 1 day from now.
    start = arrow.utcnow().floor("second").shift(hours=-10)
    ttl = datetime.timedelta(hours=1)
    rset = rruleset(cache=True)
    rset.rrule(rrule(freq=DAILY, dtstart=start.datetime))
    s = RecurringScheduler(rset, ttl)
    assert s.is_active() is False
    assert s.has_future_events() is True
    assert s.is_stale() is False


def test_recurringing_propose_next() -> None:
    """Test a RecurringingScheduler that needs to propose a next start time."""
    now = arrow.utcnow().floor("second")
    ttl = datetime.timedelta(hours=1)
    rset = rruleset(cache=True)
    rset.rrule(rrule(freq=DAILY, dtstart=now.shift(days=-2).datetime))
    s = RecurringScheduler(rset, ttl)
    # Monkey around in the internal state so the scheduler has a window
    # indicating yesterday's event
    s._start = now.shift(days=-1)

    assert s.is_active() is True
    assert s.has_future_events() is True
    assert s.is_stale() is False
    assert s._start == now
    assert s._end == now.shift(seconds=ttl.total_seconds())
