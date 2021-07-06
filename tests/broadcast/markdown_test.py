"""Tests for the semaphore.broadcast.markdown module."""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

import arrow
import dateutil

from semaphore.broadcast.data import OneTimeScheduler
from semaphore.broadcast.markdown import BroadcastMarkdown

if TYPE_CHECKING:
    from pathlib import Path


def test_evergreen(broadcasts_dir: Path) -> None:
    source_path = "evergreen.md"
    text = broadcasts_dir.joinpath(source_path).read_text()

    expected_summary = "The markdown-formatted broadcast message."
    expected_body = (
        "The extended message body, shown *only* when the user interacts "
        "with the message, and formatted as markdown.\n"
    )

    md = BroadcastMarkdown(text, source_path)
    assert md.text == text
    assert md.metadata.summary == expected_summary
    assert md.metadata.env is None
    assert md.body == expected_body

    broadcast = md.to_broadcast()
    assert broadcast.summary_md == expected_summary
    assert broadcast.body_md == expected_body
    assert broadcast.source_path == source_path
    assert broadcast.active is True
    assert broadcast.stale is False


def test_evergreen_no_body(broadcasts_dir: Path) -> None:
    source_path = "evergreen-no-body.md"
    text = broadcasts_dir.joinpath(source_path).read_text()

    expected_summary = "This message doesn't have body content."
    expected_body = None

    md = BroadcastMarkdown(text, source_path)
    assert md.text == text
    assert md.metadata.summary == expected_summary
    assert md.metadata.env is None
    assert md.body is expected_body

    broadcast = md.to_broadcast()
    assert broadcast.summary_md == expected_summary
    assert broadcast.body_md == expected_body
    assert broadcast.source_path == source_path
    assert broadcast.active is True
    assert broadcast.stale is False


def test_env_list(broadcasts_dir: Path) -> None:
    source_path = "env-list.md"
    text = broadcasts_dir.joinpath(source_path).read_text()

    expected_summary = "The markdown-formatted broadcast message."
    expected_body = (
        "The extended message body, shown *only* when the user interacts "
        "with the message, and formatted as markdown.\n"
    )

    md = BroadcastMarkdown(text, source_path)
    assert md.metadata.summary == expected_summary
    assert md.metadata.env == ["idfprod", "stable"]
    assert md.body == expected_body

    broadcast = md.to_broadcast()
    assert broadcast.summary_md == expected_summary
    assert broadcast.body_md == expected_body
    assert broadcast.source_path == source_path
    assert broadcast.active is True
    assert broadcast.stale is False


def test_env_string(broadcasts_dir: Path) -> None:
    source_path = "env-string.md"
    text = broadcasts_dir.joinpath(source_path).read_text()

    expected_summary = "The markdown-formatted broadcast message."
    expected_body = (
        "The extended message body, shown *only* when the user interacts "
        "with the message, and formatted as markdown.\n"
    )

    md = BroadcastMarkdown(text, source_path)
    assert md.metadata.summary == expected_summary
    assert md.metadata.env == ["idfprod"]
    assert md.body == expected_body

    broadcast = md.to_broadcast()
    assert broadcast.summary_md == expected_summary
    assert broadcast.body_md == expected_body
    assert broadcast.source_path == source_path
    assert broadcast.active is True
    assert broadcast.stale is False


def test_defer_expire(broadcasts_dir: Path) -> None:
    source_path = "defer-expire.md"
    text = broadcasts_dir.joinpath(source_path).read_text()

    expected_summary = "The markdown-formatted broadcast message."
    expected_body = (
        "The extended message body, shown *only* when the user interacts "
        "with the message, and formatted as markdown.\n"
    )

    md = BroadcastMarkdown(text, source_path)
    assert md.metadata.summary == expected_summary
    assert md.metadata.env is None
    assert md.body == expected_body

    broadcast = md.to_broadcast()
    assert broadcast.summary_md == expected_summary
    assert broadcast.body_md == expected_body
    assert broadcast.source_path == source_path
    assert broadcast.active is False
    assert broadcast.stale is True
    scheduler = broadcast.scheduler
    assert isinstance(scheduler, OneTimeScheduler)
    assert scheduler.start == arrow.get(
        datetime.datetime(2021, 1, 1), dateutil.tz.UTC
    )
    assert scheduler.end == arrow.get(
        datetime.datetime(2021, 1, 2), dateutil.tz.UTC
    )


def test_defer_expire_fuzzy(broadcasts_dir: Path) -> None:
    source_path = "defer-expire-fuzzy.md"
    text = broadcasts_dir.joinpath(source_path).read_text()

    expected_summary = "The markdown-formatted broadcast message."
    expected_body = None

    md = BroadcastMarkdown(text, source_path)
    assert md.metadata.summary == expected_summary
    assert md.metadata.env is None
    assert md.body == expected_body

    broadcast = md.to_broadcast()
    assert broadcast.summary_md == expected_summary
    assert broadcast.body_md == expected_body
    assert broadcast.source_path == source_path
    assert broadcast.active is False
    assert broadcast.stale is True
    scheduler = broadcast.scheduler
    assert isinstance(scheduler, OneTimeScheduler)
    assert scheduler.start == arrow.get(
        datetime.datetime(2021, 1, 1, 12), dateutil.tz.UTC
    )
    assert scheduler.end == arrow.get(
        datetime.datetime(2021, 1, 2, 4), dateutil.tz.UTC
    )
