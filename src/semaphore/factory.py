"""Component factory for Semaphore."""

from dataclasses import dataclass
from typing import Self

from markdown_it import MarkdownIt
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.stdlib import BoundLogger

from .config import Config
from .services.notification import UserNotificationService
from .storage.notification import UserNotificationStore

__all__ = ["Factory", "ProcessContext"]


@dataclass(frozen=True, slots=True)
class ProcessContext:
    """Per-process global application state."""

    markdown_parser: MarkdownIt
    """Global Markdown parser tuned for GitHub Flavored Markdown syntax."""

    @classmethod
    def from_config(cls, config: Config) -> Self:
        """Create a new process context from the configuration.

        Parameters
        ----------
        config
            Semaphore configuration.

        Returns
        -------
        ProcessContext
            Global Semaphore context.
        """
        return cls(markdown_parser=MarkdownIt("gfm-like"))


class Factory:
    """Component factory for Semaphore.

    Parameters
    ----------
    session
        Database session.
    logger
        Logger to use.
    """

    def __init__(
        self,
        context: ProcessContext,
        session: AsyncSession,
        logger: BoundLogger,
    ) -> None:
        self._context = context
        self._session = session
        self._logger = logger

    def create_notification_service(self) -> UserNotificationService:
        """Create a user notification service.

        Returns
        -------
        UserNotificationService
            Newly-created user notification service.
        """
        store = UserNotificationStore(self._session)
        return UserNotificationService(
            markdown_parser=self._context.markdown_parser,
            storage=store,
            session=self._session,
            logger=self._logger,
        )

    def set_logger(self, logger: BoundLogger) -> None:
        """Replace the internal logger.

        Used by the context dependency to update the logger for all
        newly-created components when it's rebound with additional context.

        Parameters
        ----------
        logger
            New logger.
        """
        self._logger = logger
