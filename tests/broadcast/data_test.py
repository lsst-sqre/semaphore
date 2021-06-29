"""Tests for the semaphore.broadcast.data module."""

from __future__ import annotations

from semaphore.broadcast.data import BroadcastMessage


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
