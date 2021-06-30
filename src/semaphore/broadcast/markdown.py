"""Support for parsing broadcast messages from Markdown data with YAML
front matter.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, List, Optional, Union

import yaml
from markdown_it import MarkdownIt
from mdit_py_plugins.front_matter import front_matter_plugin
from pydantic import BaseModel, validator

if TYPE_CHECKING:

    from markdown_it.token import Token

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
    """

    def __init__(self, text: str) -> None:
        self._text = text
        self._md_tokens = md.parse(text)
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
