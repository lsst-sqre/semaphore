"""Broadcast message repository."""

from __future__ import annotations

from typing import TYPE_CHECKING, Dict, Iterator, Optional, Sequence

if TYPE_CHECKING:
    from .models import BroadcastMessage


class NotFoundError(Exception):
    """An exception indicating that the reference cannot be found in the
    repository.
    """


class BroadcastMessageRepository:
    """A repository of broadcast messages.

    The repository supports adding, updating (replacing), and deleting
    messages. The repository also supports queries for messages based on
    scheduling.

    Parameters
    ----------
    messages : sequence of `BroadcastMessage`, optional
        Bootstrap the repository with these messages, optionally. Messages
        can always be added later using the `add` method.
    """

    def __init__(
        self, messages: Optional[Sequence[BroadcastMessage]] = None
    ) -> None:
        self._messages: Dict[str, BroadcastMessage] = {}
        if messages is not None:
            for message in messages:
                self.add(message)

    def add(self, message: BroadcastMessage) -> None:
        """Add a message into the repository, or replace an existing message
        of same identifier.

        Parameters
        ----------
        message : `semaphore.broadcast.models.BroadcastMessage`
            Description
        """
        self._messages[message.identifier] = message

    def __contains__(self, identifier: str) -> bool:
        return identifier in self._messages.keys()

    def __getitem__(self, identifier: str) -> BroadcastMessage:
        """Get an item based on a message's identifier.

        Parameters
        ----------
        identifier : hashable
            The message's identifier.

        Returns
        -------
        `semaphore.broadcast.models.BroadcastMessage`
            The broadcast message.

        Raises
        ------
        NotFoundError
            Raised if the message is not available.
        """
        return self.get(identifier)

    def get(self, identifier: str) -> BroadcastMessage:
        """Get an item based on a message's identifier.

        Parameters
        ----------
        identifier : hashable
            The message's identifier.

        Returns
        -------
        `semaphore.broadcast.models.BroadcastMessage`
            The broadcast message.

        Raises
        ------
        NotFoundError
            Raised if the message is not available.
        """
        try:
            return self._messages[identifier]
        except KeyError:
            raise NotFoundError(
                f"{identifier} is not in the broadcast message repository"
            )

    def iter(self) -> Iterator[BroadcastMessage]:
        """Iterate over all messages."""
        for message in self._messages.values():
            yield message

    def iter_active(self) -> Iterator[BroadcastMessage]:
        """Iterate over messages that are currently active (based on their
        scheduler.
        """
        for message in self.iter():
            if message.scheduler.is_active():
                yield message

    def iter_stale(self) -> Iterator[BroadcastMessage]:
        """Iterate over messages that are considered stale (they are not
        active and will not be scheduled in the future.
        """
        for message in self.iter():
            if message.scheduler.is_stale():
                yield message

    def iter_pending(self) -> Iterator[BroadcastMessage]:
        """Iterate over messages that have pending future events, but
        is not currently active.
        """
        for message in self.iter():
            if message.scheduler.has_future_events():
                yield message

    def remove(self, identifier: str, raise_if_missing: bool = False) -> None:
        """Remove the message."""
        try:
            del self._messages[identifier]
        except KeyError:
            if raise_if_missing:
                raise NotFoundError(
                    f"{identifier} is not in the broadcast message repository"
                )
