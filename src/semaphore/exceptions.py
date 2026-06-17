"""Exceptions for Semaphore."""

from fastapi import status
from safir.fastapi import ClientRequestError

__all__ = ["NotificationNotFoundError"]


class NotificationNotFoundError(ClientRequestError):
    """Requested notification was not found."""

    error = "notification_not_found"
    status_code = status.HTTP_404_NOT_FOUND
