"""Handlers for the app's v1 REST API."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from safir.pydantic import UtcDatetime
from safir.slack.webhook import SlackRouteErrorHandler

from ...broadcast.repository import BroadcastMessageRepository
from ...dependencies.broadcastrepo import broadcast_repo_dependency
from ...dependencies.context import RequestContext, context_dependency
from ...models.notification import (
    CURSOR_REGEX,
    CreateUserNotification,
    UserNotification,
    UserNotificationCursor,
    UserNotificationFormatted,
    UserNotificationSummary,
    UserNotificationWithUrl,
)
from .models import BroadcastMessageModel, UserNotificationRead

router = APIRouter(route_class=SlackRouteErrorHandler)
"""FastAPI router for all v1 REST API endpoints."""


@router.get(
    "/broadcasts",
    summary="Get broadcasts",
    description="List broadcast messages.",
    tags=["broadcasts"],
)
def get_broadcasts(
    broadcast_repo: Annotated[
        BroadcastMessageRepository, Depends(broadcast_repo_dependency)
    ],
) -> list[BroadcastMessageModel]:
    return [
        BroadcastMessageModel.from_broadcast_message(m)
        for m in broadcast_repo.iter_active()
    ]


@router.post(
    "/admin/notifications",
    summary="Create admin notification",
    description="Send an admin notification to a user.",
    tags=["admin", "notifications"],
)
async def admin_create_notification(
    new: CreateUserNotification,
    *,
    context: Annotated[RequestContext, Depends(context_dependency)],
    response: Response,
) -> UserNotificationWithUrl:
    service = context.factory.create_notification_service()
    base_url = context.request.url_for("admin_list_notifications")
    notification = await service.create(context.username, new, base_url)
    response.headers["Location"] = str(notification.url)
    return notification


@router.get(
    "/admin/notifications",
    summary="List admin notifications",
    description="List all current admin notifications.",
    tags=["admin", "notifications"],
)
async def admin_list_notifications(
    *,
    cursor: Annotated[
        str | None,
        Query(
            title="Cursor",
            description="Pagination cursor",
            examples=["1614985055_4234"],
            pattern=CURSOR_REGEX,
        ),
    ] = None,
    limit: Annotated[
        int | None,
        Query(
            title="Row limit",
            description="Maximum number of notifications to return.",
            examples=[500],
            ge=1,
        ),
    ] = None,
    since: Annotated[
        UtcDatetime | None,
        Query(
            title="Not before",
            description="Only show entries at or after this time.",
            examples=["2021-03-05T14:59:52Z"],
        ),
    ] = None,
    until: Annotated[
        UtcDatetime | None,
        Query(
            title="Not after",
            description="Only show entries before or at this time.",
            examples=["2021-03-05T14:59:52Z"],
        ),
    ] = None,
    recipient: Annotated[
        str | None,
        Query(
            title="Recipient",
            description="Limit notifications to this recipient.",
            examples=["username"],
        ),
    ] = None,
    sender: Annotated[
        str | None,
        Query(
            title="Sender",
            description="Limit notifications to this sender.",
            examples=["bot-quota"],
        ),
    ] = None,
    context: Annotated[RequestContext, Depends(context_dependency)],
    response: Response,
) -> list[UserNotificationWithUrl]:
    service = context.factory.create_notification_service()
    parsed_cursor = None
    if cursor:
        parsed_cursor = UserNotificationCursor.from_str(cursor)
    base_url = context.request.url_for("admin_list_notifications")
    results = await service.list_unformatted(
        cursor=parsed_cursor,
        limit=limit,
        since=since,
        until=until,
        recipient=recipient,
        sender=sender,
        base_url=base_url,
    )
    if limit:
        response.headers["Link"] = results.link_header(context.request.url)
        response.headers["X-Total-Count"] = str(results.count)
    return results.entries


@router.get(
    "/admin/notifications/{id}",
    summary="Get admin notification",
    description="Retrieve a specific admin notification.",
    tags=["admin", "notifications"],
)
async def admin_get_notification(
    id: str,
    *,
    context: Annotated[RequestContext, Depends(context_dependency)],
    response: Response,
) -> UserNotification:
    service = context.factory.create_notification_service()
    return await service.get_unformatted(id)


@router.get(
    "/notifications",
    summary="List notifications",
    description="List all current notifications for the authenticated user.",
    tags=["notifications"],
)
async def list_notifications(
    *,
    cursor: Annotated[
        str | None,
        Query(
            title="Cursor",
            description="Pagination cursor",
            examples=["1614985055_4234"],
            pattern=CURSOR_REGEX,
        ),
    ] = None,
    limit: Annotated[
        int | None,
        Query(
            title="Row limit",
            description="Maximum number of notifications to return.",
            examples=[500],
            ge=1,
        ),
    ] = None,
    unread: Annotated[
        bool,
        Query(
            title="Only unread",
            description="Only show unread notifications.",
        ),
    ] = False,
    context: Annotated[RequestContext, Depends(context_dependency)],
    response: Response,
) -> list[UserNotificationSummary]:
    service = context.factory.create_notification_service()
    parsed_cursor = None
    if cursor:
        parsed_cursor = UserNotificationCursor.from_str(cursor)
    base_url = context.request.url_for("list_notifications")
    results = await service.list_formatted(
        cursor=parsed_cursor,
        limit=limit,
        unread=unread,
        required_recipient=context.username,
        base_url=base_url,
    )
    if limit:
        response.headers["Link"] = results.link_header(context.request.url)
        response.headers["X-Total-Count"] = str(results.count)
    return results.entries


@router.get(
    "/notifications/{id}",
    summary="Get admin notification",
    description="Retrieve a specific admin notification.",
    tags=["notifications"],
)
async def get_notification(
    id: str,
    *,
    context: Annotated[RequestContext, Depends(context_dependency)],
) -> UserNotificationFormatted:
    service = context.factory.create_notification_service()
    return await service.get_formatted(id, context.username)


@router.post(
    "/notifications/read",
    summary="Mark notifications read",
    description=(
        "Mark a list of notifications read. Notifications that do not exist"
        " or that are already marked as read are silently ignored. Returning"
        " errors for nonexistent notifications is not useful since there may"
        " be race conditions with services revoking notifications."
    ),
    status_code=204,
    tags=["notifications"],
)
async def post_notification_read(
    read: UserNotificationRead,
    *,
    context: Annotated[RequestContext, Depends(context_dependency)],
) -> None:
    service = context.factory.create_notification_service()
    await service.mark_read(read.ids, context.username)
