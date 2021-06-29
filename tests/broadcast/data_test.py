"""Tests for the semaphore.broadcast.data module."""

from __future__ import annotations

import arrow

from semaphore.broadcast.data import BroadcastMessage, OneTimeBroadcastMessage


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
