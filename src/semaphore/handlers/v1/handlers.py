"""Handlers for the app's v1 REST API."""

from typing import Annotated

from fastapi import APIRouter, Depends
from safir.slack.webhook import SlackRouteErrorHandler

from ...broadcast.repository import BroadcastMessageRepository
from ...dependencies.broadcastrepo import broadcast_repo_dependency
from .models import BroadcastMessageModel

router = APIRouter(route_class=SlackRouteErrorHandler)
"""FastAPI router for all v1 REST API endpoints."""


@router.get(
    "/broadcasts",
    summary="Get broadcasts",
    description="List broadcast messages.",
    response_model=list[BroadcastMessageModel],
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
