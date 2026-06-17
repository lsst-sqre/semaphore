"""Models for per-user notifications."""

from typing import Annotated, Self, override

from pydantic import BaseModel, BeforeValidator, Field, HttpUrl
from safir.database import DatetimeIdCursor
from safir.pydantic import UtcDatetime
from sqlalchemy.orm import InstrumentedAttribute
from starlette.datastructures import URL

from ..schema import UserNotification as SQLUserNotification

__all__ = [
    "CURSOR_REGEX",
    "CreateUserNotification",
    "UserNotification",
    "UserNotificationBase",
    "UserNotificationCursor",
    "UserNotificationFormatted",
    "UserNotificationSummary",
    "UserNotificationWithUrl",
]

CURSOR_REGEX = "^p?[0-9]+_[0-9]+$"
"""Regex matching a valid cursor."""


class CreateUserNotification(BaseModel):
    """Input for creating a new user notification."""

    recipient: Annotated[
        str,
        Field(
            title="Recipient",
            description="Username to whom to send the notification.",
            examples=["some-user"],
        ),
    ]

    summary: Annotated[
        str,
        Field(
            title="Summary",
            description=(
                "Short summary in Markdown format. This will be shown in the"
                " message list. This may only use inline formatting."
            ),
            examples=["You are approaching your disk space quota limit"],
        ),
    ]

    body: Annotated[
        str | None,
        Field(
            title="Message body",
            description=(
                "Optional message body. This can use full Markdown formatting"
                " including lists and headings."
            ),
            examples=[
                "You are using 448GiB of disk out of a quota of 500GiB."
            ],
        ),
    ]


class UserNotificationBase(BaseModel):
    """Metadata common to formatted and unformatted user notifications."""

    id: Annotated[
        str,
        Field(
            title="Message ID",
            description=(
                "Unique message ID for this message. Clients should not make"
                " any assumptions about the structure of this ID and should"
                " treat it as an opaque string."
            ),
            examples=["4561-a7513"],
        ),
        BeforeValidator(lambda v: str(v) if isinstance(v, int) else v),
    ]

    created: Annotated[
        UtcDatetime,
        Field(
            title="Created date",
            description="When the message was sent.",
            examples=["2026-06-12T17:10:32-00:00"],
        ),
    ]

    read: Annotated[
        UtcDatetime | None,
        Field(
            title="Read date",
            description=(
                "When the notification was read by its recipient, or null if"
                " the notification has not been read."
            ),
            examples=["2026-06-13T14:45:12-00:00"],
        ),
    ]


class UserNotificationCursor(DatetimeIdCursor[UserNotificationBase]):
    """Pagination cursor for user notifications."""

    @override
    @staticmethod
    def id_column() -> InstrumentedAttribute:
        return SQLUserNotification.id

    @override
    @staticmethod
    def time_column() -> InstrumentedAttribute:
        return SQLUserNotification.created

    @override
    @classmethod
    def from_entry(
        cls, entry: UserNotificationBase, *, reverse: bool = False
    ) -> Self:
        return cls(id=int(entry.id), time=entry.created, previous=reverse)


class UserNotification(UserNotificationBase):
    """Model for a created but not formatted user notification."""

    sender: Annotated[
        str,
        Field(
            title="Sender",
            description="Username of the agent that sent the notification.",
            examples=["bot-quota-notifier"],
        ),
    ]

    recipient: Annotated[
        str,
        Field(
            title="Recipient",
            description="Username to whom the notiication was sent.",
            examples=["some-user"],
        ),
    ]

    summary: Annotated[
        str,
        Field(
            title="Summary",
            description="Short summary in Markdown format.",
            examples=["You are approaching your disk space quota limit"],
        ),
    ]

    body: Annotated[
        str | None,
        Field(
            title="Message body",
            description="Optional body, or null if there is no body.",
            examples=[
                "You are using 448GiB of disk out of a quota of 500GiB."
            ],
        ),
    ]


class UserNotificationWithUrl(UserNotification):
    """User notification with URL.

    Returned in lists of user notifications so that each notification in the
    list has a URL to retrieve or manipulate the notification.
    """

    url: Annotated[
        HttpUrl,
        Field(
            title="URL",
            description="URL to the sent notification.",
            examples=[
                "https://data.example.com/semaphore/v1/admin/notifications/"
                "4561-a7513"
            ],
        ),
    ]

    @classmethod
    def from_notification(
        cls, notification: UserNotification, base_url: str | URL
    ) -> Self:
        """Construct from a user notification without a URL.

        Parameters
        ----------
        notification
            Base notification.
        base_url
            Base URL for notification URLs.

        Returns
        -------
        UserNotificationWithUrl
            User notification with a URL added.
        """
        url = HttpUrl(f"{base_url!s}/{notification.id}")
        return cls(**notification.model_dump(), url=url)


class FormattedText(BaseModel):
    """Text that is formatted in both Markdown and HTML."""

    gfm: Annotated[
        str,
        Field(
            title="Markdown text",
            description="The GitHub Flavored Markdown-formatted text.",
            examples=["Some *Markdown* text."],
        ),
    ]

    html: Annotated[
        str,
        Field(
            title="HTML text",
            description="The HTML-formatted text.",
            examples=["Some <em>HTML</em> text."],
        ),
    ]


class UserNotificationSummary(UserNotificationBase):
    """User notification with a formatted summary but no body.

    Returned in lists of notifications to display to a user. Includes a URL
    from which the full notification including the formatted body can be
    returned.
    """

    summary: Annotated[
        FormattedText,
        Field(
            title="Summary",
            description="Will be formatted for inline context.",
        ),
    ]

    url: Annotated[
        HttpUrl,
        Field(
            title="URL",
            description="URL to the sent notification.",
            examples=[
                "https://data.example.com/semaphore/v1/admin/notifications/"
                "4561-a7513"
            ],
        ),
    ]


class UserNotificationFormatted(UserNotificationBase):
    """The full formatted user notification for user-facing UIs."""

    summary: Annotated[
        FormattedText,
        Field(
            title="Summary",
            description="Will be formatted for inline context.",
        ),
    ]

    body: Annotated[
        FormattedText | None,
        Field(
            title="Message body",
            description="Will be formatted for block context.",
        ),
    ]
