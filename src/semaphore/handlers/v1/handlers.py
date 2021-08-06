"""Handlers for the app's v1 REST API."""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends

from semaphore.broadcast.repository import BroadcastMessageRepository
from semaphore.config import config
from semaphore.dependencies.broadcastrepo import broadcast_repo_dependency

from .models import BroadcastMessageModel

router = APIRouter(prefix=f"/{config.name}/v1")
"""FastAPI router for all v1 REST API endpoints."""


@router.get(
    "/broadcasts",
    description="List broadcast messages.",
    response_model=List[BroadcastMessageModel],
)
def get_broadcasts(
    broadcast_repo: BroadcastMessageRepository = Depends(
        broadcast_repo_dependency
    ),
) -> List[BroadcastMessageModel]:
    return [
        BroadcastMessageModel.from_broadcast_message(m)
        for m in broadcast_repo.iter_active()
    ]
