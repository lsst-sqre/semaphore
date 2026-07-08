"""User notification business logic."""

from datetime import datetime

from markdown_it import MarkdownIt
from pydantic import HttpUrl
from safir.database import CountedPaginatedList
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import URL
from structlog.stdlib import BoundLogger

from ..exceptions import NotificationNotFoundError
from ..models.notification import (
    CreateUserNotification,
    FormattedText,
    UserNotification,
    UserNotificationCursor,
    UserNotificationFormatted,
    UserNotificationSummary,
    UserNotificationWithUrl,
)
from ..storage.notification import UserNotificationStore

__all__ = ["UserNotificationService"]


class UserNotificationService:
    """Business logic for user notifications.

    Parameters
    ----------
    markdown_parser
        Parser to convert Markdown into HTML.
    storage
        Underlying database storage.
    session
        Database session.
    logger
        Logger to use.
    """

    def __init__(
        self,
        *,
        markdown_parser: MarkdownIt,
        storage: UserNotificationStore,
        session: AsyncSession,
        logger: BoundLogger,
    ) -> None:
        self._markdown = markdown_parser
        self._storage = storage
        self._session = session
        self._logger = logger

    async def create(
        self, sender: str, request: CreateUserNotification, base_url: str | URL
    ) -> UserNotificationWithUrl:
        """Create a new user notification.

        Parameters
        ----------
        request
            User notification creation request.
        sender
            Username of the sender.
        base_url
            Base URL for notifications, used to construct the canonical URL
            of the notification.

        Returns
        -------
        UserNotification
            Newly-created notification with create-time fields filled in.
        """
        async with self._session.begin():
            result = await self._storage.add(request, sender)
        return UserNotificationWithUrl.from_notification(result, base_url)

    async def get_formatted(
        self, id: str, required_recipient: str
    ) -> UserNotificationFormatted:
        """Retrieve a formatted user notification.

        Parameters
        ----------
        id
            Identifier of the notification.
        required_recipient
            Raise `NotificationNotFoundError` if the recipient of the
            notification does not match the provided username. This access
            control measure ensures users cannot see each other's
            notifications.
        base_url
            Base URL for notifications.

        Returns
        -------
        FormattedUserNotification
            Corresponding formatted user notification.

        Raises
        ------
        NotificationNotFoundError
            Raised if the notification could not be found, or if ``username``
            was provided and the notification does not have a matching sender.
        """
        async with self._session.begin():
            notification = await self._storage.get(id)
        if not notification:
            raise NotificationNotFoundError(id)
        if required_recipient and notification.recipient != required_recipient:
            raise NotificationNotFoundError(id)

        # Format the result.
        html_summary = self._markdown.renderInline(notification.summary)
        summary = FormattedText(gfm=notification.summary, html=html_summary)
        body = None
        if notification.body:
            html_body = self._markdown.render(notification.body)
            body = FormattedText(gfm=notification.body, html=html_body)
        return UserNotificationFormatted(
            id=notification.id,
            summary=summary,
            body=body,
            created=notification.created,
            read=notification.read,
        )

    async def get_unformatted(
        self, id: str, required_sender: str | None = None
    ) -> UserNotification:
        """Retrieve an unformatted user notification.

        Parameters
        ----------
        id
            Identifier of the notification.
        required_sender
            If not `None`, raise `NotificationNotFoundError` if the sender of
            the notification does not match the provided username. This access
            control measure is used to limit the view of applications to only
            notifications they have sent.

        Returns
        -------
        UserNotification
            Corresponding user notification.

        Raises
        ------
        NotificationNotFoundError
            Raised if the notification could not be found, or if ``username``
            was provided and the notification does not have a matching sender.
        """
        async with self._session.begin():
            notification = await self._storage.get(id)
        if not notification:
            raise NotificationNotFoundError(id)
        if required_sender and notification.sender != required_sender:
            raise NotificationNotFoundError(id)
        return notification

    async def list_formatted(
        self,
        *,
        cursor: UserNotificationCursor | None = None,
        limit: int | None = None,
        unread: bool = False,
        required_recipient: str,
        base_url: str | URL,
    ) -> CountedPaginatedList[UserNotificationSummary, UserNotificationCursor]:
        """List unformatted user notifications.

        Parameters
        ----------
        cursor
            A pagination cursor specifying where to start in the results.
        limit
            Limit the number of returned results.
        unread
            Limit the results to unread messages.
        required_recipient
            Limit the results to messages intended for this recipient.
        base_url
            Base URL for notifications.

        Returns
        -------
        CountedPaginatedList of UserNotificationSummary
            Matching formatted user notification summaries, possibly paginated
            depending on the request parameters.
        """
        async with self._session.begin():
            unformatted = await self._storage.list(
                cursor=cursor,
                limit=limit,
                recipient=required_recipient,
                unread=unread,
            )

        # The above produces a CountedPaginatedList of UserNotifications. Each
        # of those notifications must now be formatted. This method formats a
        # since notification.
        def format_entry(entry: UserNotification) -> UserNotificationSummary:
            html_summary = self._markdown.renderInline(entry.summary)
            summary = FormattedText(gfm=entry.summary, html=html_summary)
            url = HttpUrl(f"{base_url!s}/{entry.id}")
            return UserNotificationSummary(
                id=entry.id,
                summary=summary,
                created=entry.created,
                read=entry.read,
                url=url,
            )

        # Return the transformed list.
        return CountedPaginatedList.from_transform(unformatted, format_entry)

    async def list_unformatted(
        self,
        *,
        cursor: UserNotificationCursor | None = None,
        limit: int | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        recipient: str | None = None,
        sender: str | None = None,
        base_url: str | URL,
    ) -> CountedPaginatedList[UserNotificationWithUrl, UserNotificationCursor]:
        """List unformatted user notifications.

        Parameters
        ----------
        cursor
            A pagination cursor specifying where to start in the results.
        limit
            Limit the number of returned results.
        since
            Limit the results to notifications at or after this time.
        until
            Limit the results to notifications before or at this time.
        recipient
            Limit the results to notifications sent to this username.
        sender
            Limit the results to notifications sent by this username.
        base_url
            Base URL for notifications.

        Returns
        -------
        CountedPaginatedList of UserNotificationWithUrl
            Matching user notifications, possibly paginated depending on the
            request parameters.
        """
        async with self._session.begin():
            results = await self._storage.list(
                cursor=cursor,
                limit=limit,
                since=since,
                until=until,
                recipient=recipient,
                sender=sender,
            )

        # Define a transform to add a URL to an entry.
        def add_url(entry: UserNotification) -> UserNotificationWithUrl:
            return UserNotificationWithUrl.from_notification(entry, base_url)

        # Return the transformed paginated list.
        return CountedPaginatedList.from_transform(results, add_url)

    async def mark_read(self, ids: set[str], required_recipient: str) -> None:
        """Mark a set of notifications as read.

        Notifications that are already marked as read will be silently left
        unchanged, as will notifications that don't exist. Error reporting is
        not useful here since there may be a race condition with revoking
        notifications.

        Parameters
        ----------
        ids
            Notification IDs to mark as read.
        required_recipient
            Only operate on notifications sent to this username.
        """
        async with self._session.begin():
            await self._storage.mark_read(required_recipient, ids)
