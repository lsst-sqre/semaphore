"""Storage for user notifications."""

from datetime import UTC, datetime

from safir.database import (
    CountedPaginatedList,
    CountedPaginatedQueryRunner,
    datetime_to_db,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.notification import (
    CreateUserNotification,
    UserNotification,
    UserNotificationCursor,
)
from ..schema import UserNotification as SQLUserNotification

__all__ = ["UserNotificationStore"]


class UserNotificationStore:
    """Storages and manipulates notifications in the database.

    Parameters
    ----------
    session
        Database session.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._paginated_runner = CountedPaginatedQueryRunner(
            UserNotification, UserNotificationCursor
        )

    async def add(
        self, notification: CreateUserNotification, sender: str
    ) -> UserNotification:
        """Store a new user notification.

        Parameters
        ----------
        notification
            Input model for a new notification.
        sender
            Username of the sender.

        Returns
        -------
        UserNotification
            Notification as stored, which includes the ID and sender.
        """
        created = datetime.now(tz=UTC).replace(microsecond=0)
        new = SQLUserNotification(
            recipient=notification.recipient,
            sender=sender,
            summary=notification.summary,
            body=notification.body,
            created=datetime_to_db(created),
            read=None,
        )
        self._session.add(new)
        await self._session.flush()
        return UserNotification.model_validate(new, from_attributes=True)

    async def get(self, id: str) -> UserNotification | None:
        """Retrieve a specific notification by ID.

        Parameters
        ----------
        id
            Identifier of the notification.

        Returns
        -------
        UserNotification or None
            The corresponding user notification, or `None` if none exists with
            that identifier.
        """
        stmt = select(SQLUserNotification).where(
            SQLUserNotification.id == int(id)
        )
        result = (await self._session.execute(stmt)).scalar_one_or_none()
        if not result:
            return None
        return UserNotification.model_validate(result, from_attributes=True)

    async def list(
        self,
        *,
        cursor: UserNotificationCursor | None = None,
        limit: int | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        recipient: str | None = None,
        sender: str | None = None,
        unread: bool = False,
    ) -> CountedPaginatedList[UserNotification, UserNotificationCursor]:
        """List or search for notifications.

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
        unread
            If `True`, limit the results to unread notifications.

        Returns
        -------
        CountedPaginatedList of UserNotification
            List of user notifications.
        """
        stmt = select(SQLUserNotification)
        if since:
            since = datetime_to_db(since)
            stmt = stmt.where(SQLUserNotification.created >= since)
        if until:
            until = datetime_to_db(until)
            stmt = stmt.where(SQLUserNotification.created <= until)
        if recipient:
            stmt = stmt.where(SQLUserNotification.recipient == recipient)
        if sender:
            stmt = stmt.where(SQLUserNotification.sender == sender)
        if unread:
            stmt = stmt.where(SQLUserNotification.read.is_(None))

        # Perform the paginated query.
        return await self._paginated_runner.query_object(
            self._session, stmt, cursor=cursor, limit=limit
        )

    async def mark_read(self, recipient: str, ids: set[str]) -> None:
        """Mark a set of notification IDs as read.

        Parameters
        ----------
        recipient
            Only act on notifications sent to this recipient.
        ids
            Set of IDs to mark as read.
        """
        ids_int = [int(v) for v in ids]
        now = datetime_to_db(datetime.now(tz=UTC).replace(microsecond=0))
        stmt = select(SQLUserNotification).where(
            SQLUserNotification.recipient == recipient,
            SQLUserNotification.id.in_(ids_int),
            SQLUserNotification.read.is_(None),
        )
        notifications = await self._session.scalars(stmt)
        for notification in notifications:
            notification.read = now
