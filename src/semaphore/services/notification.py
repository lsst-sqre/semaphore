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

        # This produces a CountedPaginatedList of UserNotifications, which
        # must now be formatted.
        formatted_entries = []
        for result in unformatted.entries:
            html_summary = self._markdown.renderInline(result.summary)
            summary = FormattedText(gfm=result.summary, html=html_summary)
            url = HttpUrl(f"{base_url!s}/{result.id}")
            formatted = UserNotificationSummary(
                id=result.id,
                summary=summary,
                created=result.created,
                read=result.read,
                url=url,
            )
            formatted_entries.append(formatted)

        # This requires creating a new CountedPaginatedList, which in turn
        # requires knowing what fields to copy. This is not ideal and should
        # have better support in Safir.
        return CountedPaginatedList[
            UserNotificationSummary, UserNotificationCursor
        ](
            entries=formatted_entries,
            next_cursor=unformatted.next_cursor,
            prev_cursor=unformatted.prev_cursor,
            count=unformatted.count,
        )

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
        CountedPaginatedList of UserNotification
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

        # Add the URLs to the entries.
        entries = [
            UserNotificationWithUrl.from_notification(e, base_url)
            for e in results.entries
        ]

        # This requires creating a new CountedPaginatedList, which in turn
        # requires knowing what fields to copy. This is not ideal and should
        # have better support in Safir.
        return CountedPaginatedList[
            UserNotificationWithUrl, UserNotificationCursor
        ](
            entries=entries,
            next_cursor=results.next_cursor,
            prev_cursor=results.prev_cursor,
            count=results.count,
        )
