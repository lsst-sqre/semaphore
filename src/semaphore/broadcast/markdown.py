"""Support for parsing broadcast messages from Markdown data with YAML
front matter.
"""

from __future__ import annotations

import datetime
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    Optional,
    Union,
)

import arrow
import dateutil
import dateutil.parser
import yaml
from markdown_it import MarkdownIt
from mdformat.renderer import MDRenderer
from mdit_py_plugins.front_matter import front_matter_plugin
from pydantic import BaseModel, validator

from .data import BroadcastMessage, OneTimeScheduler, PermaScheduler

if TYPE_CHECKING:
    from markdown_it.token import Token

    from .data import Scheduler

__all__ = ["BroadcastMarkdown", "BroadcastMarkdownFrontMatter"]

md = MarkdownIt("gfm-like").use(front_matter_plugin)
"""Markdown parser tuned for GitHub Flavored Markdown syntax and supporting
front matter.

See https://markdown-it-py.readthedocs.io/en/latest/using.html#the-parser
"""


class BroadcastMarkdown:
    """A representation of a markdown file containing broadcast message
    content and metadata.

    Properties
    ----------
    text : `str`
        The content of the markdown message (including YAML-formatted
        front-matter).
    source_path : `str`
        A string that identifies the message, which is typically the POSIX path
        of the markdown within the host GitHub repository.
    """

    def __init__(self, text: str, source_path: str) -> None:
        self._text = text
        self.source_path = source_path
        self._md_env: Dict[Any, Any] = {}
        self._md_tokens = md.parse(text, self._md_env)
        self._metadata = self._parse_metadata()

    def _parse_metadata(self) -> BroadcastMarkdownFrontMatter:
        frontmatter_token = self._get_front_matter_token()
        yaml_data = yaml.safe_load(frontmatter_token.content)
        return BroadcastMarkdownFrontMatter.parse_obj(yaml_data)

    def _get_front_matter_token(self) -> Token:
        for token in self._md_tokens:
            if token.type == "front_matter":
                return token
        raise ValueError(
            "A front_matter token is not present in the markdown content."
        )

    @property
    def metadata(self) -> BroadcastMarkdownFrontMatter:
        """The broadcast's metadata."""
        return self._metadata

    @property
    def text(self) -> str:
        """The full text of the markdown message (including front-matter)."""
        return self._text

    @property
    def body(self) -> Optional[str]:
        """The text of the markdown body or `None` if the message doesn't have
        body content.
        """
        body_tokens = [t for t in self._md_tokens if t.type != "front_matter"]
        if len(body_tokens) == 0:
            return None
        else:
            return MDRenderer().render(body_tokens, md.options, self._md_env)

    def to_broadcast(self) -> BroadcastMessage:
        """Export a BroadcastMessage from the markdown content.

        Returns
        -------
        `semaphore.broadcast.data.BroadcastMessage`
            The broadcast message.
        """
        return BroadcastMessage(
            source_path=self.source_path,
            summary_md=self.metadata.summary,
            body_md=self.body,
            scheduler=self._make_scheduler(),
        )

    def _make_scheduler(self) -> Scheduler:
        if self.metadata.defer is not None:
            if self.metadata.expire is not None:
                return OneTimeScheduler(
                    self.metadata.defer, self.metadata.expire
                )
            else:
                raise NotImplementedError("Implement TTL")
        else:
            return PermaScheduler()


class FuzzyArrow(arrow.Arrow):
    """A Pydantic custom type that has flexible parsing of datetime fields
    and generates arrow datetimes.
    """

    @classmethod
    def __get_validators__(cls) -> Iterator[Callable[[Any], arrow.Arrow]]:
        # one or more validators may be yielded which will be called in the
        # order to validate the input, each validator will receive as an input
        # the value returned from the previous validator
        yield cls.validate

    @classmethod
    def validate(cls, v: Any) -> arrow.Arrow:
        if isinstance(v, datetime.date):
            # Pydantic pre-parses YYYY-MM-DD into a datetime.date
            dt = datetime.datetime.combine(v, datetime.time())
        elif isinstance(v, datetime.datetime):
            # Pydantic pre-parses into a datetime
            dt = v
        elif isinstance(v, str):
            try:
                dt = dateutil.parser.parse(v, fuzzy=True, yearfirst=True)
            except (ValueError, OverflowError):
                raise ValueError("Could not parse date")
        else:
            raise TypeError("Not a string")

        if dt.tzinfo:
            # Parsed date includes a timezone.
            return arrow.get(dt)
        else:
            # naive datetime, so default to UTC
            return arrow.get(dt, dateutil.tz.UTC)


class BroadcastMarkdownFrontMatter(BaseModel):
    """A pydantic model describing the front-matter from a markdown broadcast
    message.
    """

    summary: str
    """Broadcast summary message."""

    env: Optional[List[str]] = None
    """The list of applicable environments. None implies that the broadcast
    is applicable to all environments.
    """

    defer: Optional[FuzzyArrow] = None
    """Date when the message is deferred to start."""

    expire: Optional[FuzzyArrow] = None
    """Date when the message expires."""

    @validator("env", pre=True)
    def preprocess_env(
        cls, v: Union[str, List[str]], **kwargs: Any
    ) -> Optional[List[str]]:
        """Convert the string form of the env keyword to a list, supporting
        comma-separated lists as well.
        """
        if isinstance(v, str):
            return [s.strip() for s in v.split(",")]
        else:
            return v
