"""Models for the v1 REST API."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from markdown_it import MarkdownIt
from pydantic import BaseModel

if TYPE_CHECKING:
    from semaphore.broadcast.models import BroadcastMessage


class FormattedText(BaseModel):
    """Text that is formatted in both markdown and HTML."""

    gfm: str
    """The GitHub-flavored Markdown version of the text."""

    html: str
    """The HTML-formatted version of the text."""

    @classmethod
    def from_gfm(cls, gfm_text: str, inline: bool = False) -> FormattedText:
        """Create formatted text from GitHub-flavored markdown.

        Parameters
        ----------
        gfm_text : `str`
            GitHub flavored markdown.
        inline : `bool`
            If `True`, no paragraph tags are added to the HTML content.

        Returns
        -------
        `FormattedText`
            The formatted text, containing both markdown and HTML renderings.
        """
        md_parser = MarkdownIt("gfm-like")
        if inline:
            html_text = md_parser.renderInline(gfm_text)
        else:
            html_text = md_parser.render(gfm_text)
        return cls(gfm=gfm_text, html=html_text)


class BroadcastMessageModel(BaseModel):
    """A broadcast message."""

    id: str
    """The message's identifier."""

    summary: FormattedText
    """The message summary."""

    body: Optional[FormattedText]
    """The body content (optional)."""

    active: bool
    """True if the message should be broadcast based on its schedule and
    being enabled.
    """

    enabled: bool
    """A toggle that, when false, disables a message even if scheduled."""

    stale: bool
    """True if the message has not future scheduled broadcast events."""

    @classmethod
    def from_broadcast_message(
        cls, message: BroadcastMessage
    ) -> BroadcastMessageModel:
        """Create a v1 API BroadcastMessageModel from a broadcast message
        domain model.

        Parameters
        ----------
        message : `semaphore.broadcast.models.BroadcastMessage`
            The message domain model, usually obtained from a repository.

        Returns
        -------
        `BroadcastMessageModel`
            The message entity for the v1 API.
        """
        formatted_summary = FormattedText.from_gfm(message.summary_md)
        formatted_body = None
        if message.body_md is not None:
            formatted_body = FormattedText.from_gfm(message.body_md)
        return cls(
            id=message.identifier,
            summary=formatted_summary,
            body=formatted_body,
            active=message.active,
            enabled=message.enabled,
            stale=message.stale,
        )
