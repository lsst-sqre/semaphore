"""Exceptions for Semaphore."""

from fastapi import status
from safir.fastapi import ClientRequestError

__all__ = ["NotificationNotFoundError", "PermissionDeniedError"]


class NotificationNotFoundError(ClientRequestError):
    """Requested notification was not found."""

    error = "notification_not_found"
    status_code = status.HTTP_404_NOT_FOUND

    def __init__(self, id: str) -> None:
        super().__init__(f"Unknown notification: {id}")


class PermissionDeniedError(ClientRequestError):
    """Permission denied accessing a resource."""

    error = "permission_denied"
    status_code = status.HTTP_403_FORBIDDEN
