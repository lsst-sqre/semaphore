"""Tests for the semaphore.broadcast.markdown module."""

from __future__ import annotations

from typing import TYPE_CHECKING

from semaphore.broadcast.markdown import BroadcastMarkdown

if TYPE_CHECKING:
    from pathlib import Path


def test_evergreen(broadcasts_dir: Path) -> None:
    text = broadcasts_dir.joinpath("evergreen.md").read_text()
    md = BroadcastMarkdown(text)
    assert md.text == text
    assert md.metadata.summary == "The markdown-formatted broadcast message."
    assert md.metadata.env is None


def test_evergreen_no_body(broadcasts_dir: Path) -> None:
    text = broadcasts_dir.joinpath("evergreen-no-body.md").read_text()
    md = BroadcastMarkdown(text)
    assert md.text == text
    assert md.metadata.summary == "This message doesn't have body content."
    assert md.metadata.env is None


def test_env_list(broadcasts_dir: Path) -> None:
    text = broadcasts_dir.joinpath("env-list.md").read_text()
    md = BroadcastMarkdown(text)
    assert md.metadata.summary == "The markdown-formatted broadcast message."
    assert md.metadata.env == ["idfprod", "stable"]


def test_env_string(broadcasts_dir: Path) -> None:
    text = broadcasts_dir.joinpath("env-string.md").read_text()
    md = BroadcastMarkdown(text)
    assert md.metadata.summary == "The markdown-formatted broadcast message."
    assert md.metadata.env == ["idfprod"]
