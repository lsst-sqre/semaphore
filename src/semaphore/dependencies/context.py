"""Request context dependency for FastAPI.

This dependency gathers a variety of information into a single object for the
convenience of writing request handlers. It also provides a place to store a
`structlog.BoundLogger` that can gather additional context during processing,
including from dependencies.
"""

from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Header, Request
from safir.dependencies.db_session import db_session_dependency
from safir.dependencies.gafaelfawr import auth_logger_dependency
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.stdlib import BoundLogger

from ..config import config
from ..factory import Factory, ProcessContext

__all__ = [
    "ContextDependency",
    "RequestContext",
    "context_dependency",
]


@dataclass(slots=True)
class RequestContext:
    """Holds the incoming request and its surrounding context.

    The primary reason for the existence of this class is to allow the
    functions involved in request processing to repeated rebind the request
    logger to include more information, without having to pass both the
    request and the logger separately to every function.
    """

    request: Request
    """The incoming request."""

    username: str
    """Authenticated username of the incoming request."""

    logger: BoundLogger
    """The request logger, rebound with discovered context."""

    session: AsyncSession
    """The database session."""

    factory: Factory
    """The component factory."""

    def rebind_logger(self, **values: Any) -> None:
        """Add the given values to the logging context.

        Parameters
        ----------
        **values
            Additional values that should be added to the logging context.
        """
        self.logger = self.logger.bind(**values)
        self.factory.set_logger(self.logger)


class ContextDependency:
    """Provide a per-request context as a FastAPI dependency.

    This dependency should only be used by routes that require a database
    session and an authenticated request. Broadcast message retrieval should
    not use this dependency since it is anonymous.
    """

    def __init__(self) -> None:
        self._process_context = ProcessContext.from_config(config)

    async def __call__(
        self,
        *,
        request: Request,
        x_auth_request_user: Annotated[str, Header()],
        session: Annotated[AsyncSession, Depends(db_session_dependency)],
        logger: Annotated[BoundLogger, Depends(auth_logger_dependency)],
    ) -> RequestContext:
        """Create a per-request context and return it."""
        return RequestContext(
            request=request,
            username=x_auth_request_user,
            logger=logger,
            session=session,
            factory=Factory(self._process_context, session, logger),
        )


context_dependency = ContextDependency()
"""The dependency that will return the per-request context.

This dependency should only be used by routes that require a database session
and an authenticated request. Broadcast message retrieval should not use this
dependency since it is anonymous.
"""
