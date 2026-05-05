"""Tests for the semaphore.broadcast.markdown module."""

import datetime
from datetime import UTC

import arrow
import dateutil
import pytest
from pydantic import ValidationError
from safir.testing.data import Data

from semaphore.broadcast.markdown import (
    BroadcastMarkdown,
    BroadcastMarkdownFrontMatter,
)
from semaphore.broadcast.models import (
    FixedExpirationScheduler,
    OneTimeScheduler,
    OpenEndedScheduler,
    PermaScheduler,
    RecurringScheduler,
)


def test_evergreen(data: Data) -> None:
    source_path = "evergreen.md"
    text = data.read_text(f"broadcasts/{source_path}")

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
    assert broadcast.identifier == source_path
    assert broadcast.active is True
    assert broadcast.stale is False
    assert broadcast.category == "notice"


def test_evergreen_no_body(data: Data) -> None:
    source_path = "evergreen-no-body.md"
    text = data.read_text(f"broadcasts/{source_path}")

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
    assert broadcast.identifier == source_path
    assert broadcast.active is True
    assert broadcast.stale is False


def test_evergreen_disabled(data: Data) -> None:
    source_path = "evergreen-disabled.md"
    text = data.read_text(f"broadcasts/{source_path}")

    md = BroadcastMarkdown(text, source_path)
    broadcast = md.to_broadcast()
    assert broadcast.active is False
    assert broadcast.scheduler.is_active() is True


def test_evergreen_info(data: Data) -> None:
    source_path = "evergreen-info.md"
    text = data.read_text(f"broadcasts/{source_path}")

    expected_summary = "Informational markdown-formatted broadcast message."
    expected_body_pre = (
        "Informational markdown-formatted broadcast message.\n\n"
        "The extended message body, shown *only* when the user interacts "
        "with the message, and formatted as markdown.\n"
    )
    expected_body_post = (
        "The extended message body, shown *only* when the user interacts "
        "with the message, and formatted as markdown.\n"
    )

    md = BroadcastMarkdown(text, source_path)
    assert md.text == text
    assert md.metadata.summary is None
    assert md.metadata.env is None
    assert md.body == expected_body_pre

    broadcast = md.to_broadcast()
    assert isinstance(broadcast.scheduler, PermaScheduler)
    assert broadcast.summary_md == expected_summary
    assert broadcast.body_md == expected_body_post
    assert broadcast.identifier == source_path
    assert broadcast.active is True
    assert broadcast.stale is False
    assert broadcast.category == "info"


def test_env_list(data: Data) -> None:
    source_path = "env-list.md"
    text = data.read_text(f"broadcasts/{source_path}")

    expected_summary = "The markdown-formatted broadcast message."
    expected_body_pre = (
        "The markdown-formatted broadcast message.\n\n"
        "The extended message body, shown *only* when the user interacts "
        "with the message, and formatted as markdown.\n"
    )
    expected_body_post = (
        "The extended message body, shown *only* when the user interacts "
        "with the message, and formatted as markdown.\n"
    )

    md = BroadcastMarkdown(text, source_path)
    assert md.metadata.summary is None
    assert md.metadata.env == ["idfprod", "stable"]
    assert md.body == expected_body_pre

    broadcast = md.to_broadcast()
    assert broadcast.summary_md == expected_summary
    assert broadcast.body_md == expected_body_post
    assert broadcast.identifier == source_path
    assert broadcast.active is True
    assert broadcast.stale is False


def test_env_string(data: Data) -> None:
    source_path = "env-string.md"
    text = data.read_text(f"broadcasts/{source_path}")

    expected_summary = "The markdown-formatted broadcast message."
    expected_body_pre = (
        "The markdown-formatted broadcast message.\n\n"
        "The extended message body, shown *only* when the user interacts "
        "with the message, and formatted as markdown.\n"
    )
    expected_body_post = (
        "The extended message body, shown *only* when the user interacts "
        "with the message, and formatted as markdown.\n"
    )

    md = BroadcastMarkdown(text, source_path)
    assert md.metadata.summary is None
    assert md.metadata.env == ["idfprod"]
    assert md.body == expected_body_pre

    broadcast = md.to_broadcast()
    assert broadcast.summary_md == expected_summary
    assert broadcast.body_md == expected_body_post
    assert broadcast.identifier == source_path
    assert broadcast.active is True
    assert broadcast.stale is False


def test_summary(data: Data) -> None:
    source_path = "summary.md"
    text = data.read_text(f"broadcasts/{source_path}")

    expected_summary = (
        "There is no summary here,\njust a couple of title lines"
    )
    expected_body_pre = (
        "There is no summary here,\njust a couple of title "
        "lines\n\nHere's some body text!\n\nMore body text\n"
    )
    expected_body_post = "Here's some body text!\n\nMore body text\n"

    md = BroadcastMarkdown(text, source_path)
    assert md.text == text
    assert md.metadata.summary is None
    assert md.body == expected_body_pre

    broadcast = md.to_broadcast()

    assert broadcast.summary_md == expected_summary
    assert broadcast.body_md == expected_body_post
    assert broadcast.identifier == source_path


def test_defer_expire(data: Data) -> None:
    source_path = "defer-expire.md"
    text = data.read_text(f"broadcasts/{source_path}")

    expected_summary = "The markdown-formatted broadcast message."
    expected_body_pre = (
        "The markdown-formatted broadcast message.\n\n"
        "The extended message body, shown *only* when the user interacts "
        "with the message, and formatted as markdown.\n"
    )
    expected_body_post = (
        "The extended message body, shown *only* when the user interacts "
        "with the message, and formatted as markdown.\n"
    )

    md = BroadcastMarkdown(text, source_path)
    assert md.metadata.summary is None
    assert md.metadata.env is None
    assert md.body == expected_body_pre

    broadcast = md.to_broadcast()
    assert broadcast.summary_md == expected_summary
    assert broadcast.body_md == expected_body_post
    assert broadcast.identifier == source_path
    assert broadcast.active is False
    assert broadcast.stale is True
    scheduler = broadcast.scheduler
    assert isinstance(scheduler, OneTimeScheduler)
    assert scheduler.start == arrow.get(
        datetime.datetime(2021, 1, 1, tzinfo=UTC),
    )
    assert scheduler.end == arrow.get(
        datetime.datetime(2021, 1, 2, tzinfo=UTC),
    )


def test_defer_expire_fuzzy(data: Data) -> None:
    source_path = "defer-expire-fuzzy.md"
    text = data.read_text(f"broadcasts/{source_path}")

    expected_summary = "The markdown-formatted broadcast message.\n"
    expected_body_pre = "The markdown-formatted broadcast message.\n"
    expected_body_post = None

    md = BroadcastMarkdown(text, source_path)
    assert md.metadata.summary is None
    assert md.metadata.env is None
    assert md.body == expected_body_pre

    broadcast = md.to_broadcast()
    assert broadcast.summary_md == expected_summary
    assert broadcast.body_md == expected_body_post
    assert broadcast.identifier == source_path
    assert broadcast.active is False
    assert broadcast.stale is True
    scheduler = broadcast.scheduler
    assert isinstance(scheduler, OneTimeScheduler)
    assert scheduler.start == arrow.get(
        datetime.datetime(2021, 1, 1, 12, tzinfo=UTC),
    )
    assert scheduler.end == arrow.get(
        datetime.datetime(2021, 1, 2, 4, tzinfo=UTC),
    )


def test_defer_expire_fuzzy_default_tz(data: Data) -> None:
    source_path = "defer-expire-fuzzy-default-tz.md"
    text = data.read_text(f"broadcasts/{source_path}")

    md = BroadcastMarkdown(text, source_path)
    assert md.metadata.timezone == dateutil.tz.gettz("America/Los Angeles")

    broadcast = md.to_broadcast()
    scheduler = broadcast.scheduler
    assert isinstance(scheduler, OneTimeScheduler)
    assert scheduler.start == arrow.get(
        datetime.datetime(2021, 1, 1, 12, tzinfo=UTC),
    )
    assert scheduler.end == arrow.get(
        "2021-01-02 04:00 America/Los_Angeles", "YYYY-MM-DD HH:mm ZZZ"
    )


def test_defer_ttl(data: Data) -> None:
    source_path = "defer-ttl.md"
    text = data.read_text(f"broadcasts/{source_path}")

    md = BroadcastMarkdown(text, source_path)
    broadcast = md.to_broadcast()
    scheduler = broadcast.scheduler
    assert isinstance(scheduler, OneTimeScheduler)
    assert scheduler.start == arrow.get(
        datetime.datetime(2021, 1, 1, tzinfo=UTC)
    )
    assert scheduler.end == arrow.get(
        datetime.datetime(2021, 1, 1, 1, tzinfo=UTC)
    )


def test_defer_noexpire(data: Data) -> None:
    source_path = "defer-noexpire.md"
    text = data.read_text(f"broadcasts/{source_path}")

    md = BroadcastMarkdown(text, source_path)
    broadcast = md.to_broadcast()
    scheduler = broadcast.scheduler
    assert isinstance(scheduler, OpenEndedScheduler)
    assert scheduler.start == arrow.get(
        datetime.datetime(2021, 1, 1, tzinfo=UTC)
    )


def test_expire(data: Data) -> None:
    source_path = "expire.md"
    text = data.read_text(f"broadcasts/{source_path}")

    md = BroadcastMarkdown(text, source_path)
    broadcast = md.to_broadcast()
    scheduler = broadcast.scheduler
    assert isinstance(scheduler, FixedExpirationScheduler)
    assert scheduler.end == arrow.get(
        datetime.datetime(2021, 1, 2, tzinfo=UTC)
    )


def test_patch_thursday(data: Data) -> None:
    source_path = "patch-thursday.md"
    text = data.read_text(f"broadcasts/{source_path}")
    md = BroadcastMarkdown(text, source_path)
    broadcast = md.to_broadcast()
    scheduler = broadcast.scheduler
    assert isinstance(scheduler, RecurringScheduler)


def test_frontmatter_expire_ttl_conflict() -> None:
    """If frontmatter has both ttl and expire, validation should fail."""
    with pytest.raises(ValidationError):
        BroadcastMarkdownFrontMatter.model_validate(
            {
                "summary": "The summary",
                "start": "2021-01-01 12pm",
                "expire": "2021-02-01 1pm",
                "ttl": "2h",
            }
        )


def test_frontmatter_expire_before_defer() -> None:
    """If frontmatter defer is before start, validation should fail."""
    with pytest.raises(ValidationError):
        BroadcastMarkdownFrontMatter.model_validate(
            {
                "summary": "The summary",
                "defer": "2021-01-02 12pm",
                "expire": "2021-01-01 1pm",
            }
        )
