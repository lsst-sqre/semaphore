"""Reload queue dependency.

This FastAPI dependency provides the queue of GitHub repositories whose
broadcasts must be reloaded to request handlers. Code outside a request, such
as the startup lifespan and the reload worker it starts, can await the
dependency to share the same queue.
"""

from ..github.reloadqueue import ReloadQueue

__all__ = ["ReloadQueueDependency", "reload_queue_dependency"]


class ReloadQueueDependency:
    """Provides the queue of repositories whose broadcasts must be
    reloaded.
    """

    def __init__(self) -> None:
        self.queue = ReloadQueue()

    async def __call__(self) -> ReloadQueue:
        """Return the reload queue."""
        return self.queue


reload_queue_dependency = ReloadQueueDependency()
"""A FastAPI dependency that returns the reload queue."""
