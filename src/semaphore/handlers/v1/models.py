"""Models for the v1 REST API."""

from __future__ import annotations

from typing import Optional

from markdown_it import MarkdownIt
from pydantic import BaseModel, Field

from semaphore.broadcast.models import BroadcastCategory, BroadcastMessage


class FormattedText(BaseModel):
    """Text that is formatted in both markdown and HTML."""

    gfm: str = Field(title="The GitHub-flavored Markdown-formatted text.")

    html: str = Field(title="The HTML-formatted text.")

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

    id: str = Field(title="The message's identifier")

    summary: FormattedText = Field(title="The message summary")

    body: Optional[FormattedText] = Field(title="The body content (optional).")

    active: bool = Field(
        title="Whether the message should be displayed",
        description=(
            "True if the message should be broadcast based on its schedule "
            "and being enabled."
        ),
    )

    enabled: bool = Field(
        title="Toggle for whether the message is enabled",
        description=(
            "When false, the message isn't shown even if it is scheduled"
        ),
    )

    stale: bool = Field(
        title="Flag indicated the message is stable",
        description=(
            "True if the message has no future scheduled broadcast events."
        ),
    )

    category: BroadcastCategory = Field(title="Category of the message.")

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
            category=message.category,
        )
