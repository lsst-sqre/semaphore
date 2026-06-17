"""The user notification database table."""

from datetime import datetime

from sqlalchemy import Index
from sqlalchemy.orm import Mapped, mapped_column

from ._base import SchemaBase

__all__ = ["UserNotification"]


class UserNotification(SchemaBase):
    """A user notification."""

    __tablename__ = "user_notification"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    recipient: Mapped[str]
    sender: Mapped[str]
    summary: Mapped[str]
    body: Mapped[str | None]
    created: Mapped[datetime]
    read: Mapped[datetime | None]

    __table_args__ = (
        Index("by_created", "created", "id"),
        Index("by_sender", "sender", "created", "id"),
        Index("by_recipient", "recipient", "created", "id"),
        Index("by_recipient_id", "recipient", "id"),
    )
