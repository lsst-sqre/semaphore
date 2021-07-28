"""Tests for the semaphore.broadcast.markdown module."""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

import arrow
import dateutil
import pytest
from pydantic import ValidationError

from semaphore.broadcast.markdown import (
    BroadcastMarkdown,
    BroadcastMarkdownFrontMatter,
    parse_timedelta,
)
from semaphore.broadcast.models import (
    FixedExpirationScheduler,
    OneTimeScheduler,
    OpenEndedScheduler,
    PermaScheduler,
    RecurringScheduler,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    "value,expected",
    [
        ("1w", datetime.timedelta(weeks=1)),
        ("1week", datetime.timedelta(weeks=1)),
        ("2weeks", datetime.timedelta(weeks=2)),
        ("1d", datetime.timedelta(days=1)),
        ("1day", datetime.timedelta(days=1)),
        ("2days", datetime.timedelta(days=2)),
        ("1h", datetime.timedelta(hours=1)),
        ("1hr", datetime.timedelta(hours=1)),
        ("1hour", datetime.timedelta(hours=1)),
        ("1hours", datetime.timedelta(hours=1)),
        ("1m", datetime.timedelta(minutes=1)),
        ("1min", datetime.timedelta(minutes=1)),
        ("1mins", datetime.timedelta(minutes=1)),
        ("1minute", datetime.timedelta(minutes=1)),
        ("1minutes", datetime.timedelta(minutes=1)),
        ("1s", datetime.timedelta(seconds=1)),
        ("1sec", datetime.timedelta(seconds=1)),
        ("1secs", datetime.timedelta(seconds=1)),
        ("1second", datetime.timedelta(seconds=1)),
        ("1seconds", datetime.timedelta(seconds=1)),
        ("1w1d1h1m", datetime.timedelta(weeks=1, days=1, hours=1, minutes=1)),
        (
            "1w 1d 1h 1m",
            datetime.timedelta(weeks=1, days=1, hours=1, minutes=1),
        ),
        ("2days 6hr", datetime.timedelta(days=2, hours=6)),
        ("1w 2d 6hours", datetime.timedelta(weeks=1, days=2, hours=6)),
        ("1 week 2 days 6hours", datetime.timedelta(weeks=1, days=2, hours=6)),
    ],
)
def test_parse_timedelta(value: str, expected: datetime.timedelta) -> None:
    td = parse_timedelta(value)
    assert td == expected


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
    assert isinstance(broadcast.scheduler, PermaScheduler)
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


def test_evergreen_disabled(broadcasts_dir: Path) -> None:
    source_path = "evergreen-disabled.md"
    text = broadcasts_dir.joinpath(source_path).read_text()
    md = BroadcastMarkdown(text, source_path)
    broadcast = md.to_broadcast()
    assert broadcast.active is False
    assert broadcast.scheduler.is_active() is True


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


def test_defer_expire_fuzzy_default_tz(broadcasts_dir: Path) -> None:
    source_path = "defer-expire-fuzzy-default-tz.md"
    text = broadcasts_dir.joinpath(source_path).read_text()

    md = BroadcastMarkdown(text, source_path)
    assert md.metadata.timezone == dateutil.tz.gettz("America/Los Angeles")

    broadcast = md.to_broadcast()
    scheduler = broadcast.scheduler
    assert isinstance(scheduler, OneTimeScheduler)
    assert scheduler.start == arrow.get(
        datetime.datetime(2021, 1, 1, 12), dateutil.tz.UTC
    )
    assert scheduler.end == arrow.get(
        datetime.datetime(2021, 1, 2, 4),
        "America/Los Angeles",
    )


def test_defer_ttl(broadcasts_dir: Path) -> None:
    source_path = "defer-ttl.md"
    text = broadcasts_dir.joinpath(source_path).read_text()

    md = BroadcastMarkdown(text, source_path)
    broadcast = md.to_broadcast()
    scheduler = broadcast.scheduler
    assert isinstance(scheduler, OneTimeScheduler)
    assert scheduler.start == arrow.get(
        datetime.datetime(2021, 1, 1), dateutil.tz.UTC
    )
    assert scheduler.end == arrow.get(
        datetime.datetime(2021, 1, 1, 1), dateutil.tz.UTC
    )


def test_defer_noexpire(broadcasts_dir: Path) -> None:
    source_path = "defer-noexpire.md"
    text = broadcasts_dir.joinpath(source_path).read_text()

    md = BroadcastMarkdown(text, source_path)
    broadcast = md.to_broadcast()
    scheduler = broadcast.scheduler
    assert isinstance(scheduler, OpenEndedScheduler)
    assert scheduler.start == arrow.get(
        datetime.datetime(2021, 1, 1), dateutil.tz.UTC
    )


def test_expire(broadcasts_dir: Path) -> None:
    source_path = "expire.md"
    text = broadcasts_dir.joinpath(source_path).read_text()

    md = BroadcastMarkdown(text, source_path)
    broadcast = md.to_broadcast()
    scheduler = broadcast.scheduler
    assert isinstance(scheduler, FixedExpirationScheduler)
    assert scheduler.end == arrow.get(
        datetime.datetime(2021, 1, 2), dateutil.tz.UTC
    )


def test_patch_thursday(broadcasts_dir: Path) -> None:
    source_path = "patch-thursday.md"
    text = broadcasts_dir.joinpath(source_path).read_text()
    md = BroadcastMarkdown(text, source_path)
    broadcast = md.to_broadcast()
    scheduler = broadcast.scheduler
    assert isinstance(scheduler, RecurringScheduler)


def test_frontmatter_expire_ttl_conflict() -> None:
    """If frontmatter has both ttl and expire, validation should fail."""
    with pytest.raises(ValidationError):
        BroadcastMarkdownFrontMatter(
            summary="The summary",
            start="2021-01-01 12pm",
            expire="2021-02-01 1pm",
            ttl="2h",
        )


def test_frontmatter_expire_before_defer() -> None:
    """If frontmatter defer is before start, validation should fail."""
    with pytest.raises(ValidationError):
        BroadcastMarkdownFrontMatter(
            summary="The summary",
            defer="2021-01-02 12pm",
            expire="2021-01-01 1pm",
        )
