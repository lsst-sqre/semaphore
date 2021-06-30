"""Data containers for broadcast messages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import arrow

if TYPE_CHECKING:
    import datetime
    from typing import Optional, Tuple

    import dateutil.rrule


__all__ = [
    "BroadcastMessage",
    "OneTimeBroadcastMessage",
    "RepeatingBroadcastMessage",
]


@dataclass
class BroadcastMessage:
    """A basic broadcast message that does not expire or repeat."""

    source_path: str
    """The path of the source in the GitHub repository, which serves as a
    unique identifier.
    """

    summary_md: str
    """The message message, as markdown."""

    body_md: Optional[str]
    """The body content, as markdown."""

    @property
    def active(self) -> bool:
        """Whether the message should be served to clients for display."""
        return True


@dataclass
class OneTimeBroadcastMessage(BroadcastMessage):
    """A broadcast that is scheduled to display for a single time window."""

    defer: arrow.Arrow
    """Time when the message begins to be displayed."""

    expire: arrow.Arrow
    """Time when the message begins to be considered expired."""

    @property
    def active(self) -> bool:
        """Whether the message should be served to clients for display."""
        return arrow.utcnow().is_between(self.defer, self.expire, bounds="[)")


@dataclass
class RepeatingBroadcastMessage(OneTimeBroadcastMessage):
    """A broadcast message that repeats according to an RFC 5546 recurence
    rule schedule.

    Note
    ----
    The `defer` and `expire` attributes reflect either the current or the next
    periods when the message can be displayed. These dates are refreshed
    automatically when polling the `active` property.
    """

    rruleset: dateutil.rrule.rruleset
    """A recurring rule set."""

    ttl: datetime.timedelta
    """The length of time the message is displayed for after each cron
    event.
    """

    @property
    def ttl_seconds(self) -> float:
        return self.ttl.total_seconds()

    @property
    def active(self) -> bool:
        """Whether the message should be served to clients for display."""
        if self.expire < arrow.utcnow():
            # The last message occurence expired, so let's calculate the next.
            try:
                candidate_defer, candidate_expire = self._propose(self.defer)
            except ValueError:
                return False  # no future event
            while candidate_expire < arrow.utcnow():
                # Keep iterating in case windows overlap
                try:
                    candidate_defer, candidate_expire = self._propose(
                        candidate_defer
                    )
                except ValueError:
                    return False  # no future event
            # Set next defer and expire dates
            self.defer = candidate_defer
            self.expire = candidate_expire
        return super().active

    def _propose(self, after: arrow.Arrow) -> Tuple[arrow.Arrow, arrow.Arrow]:
        """Propose a new defer/expire time after the given time"""
        candidate_defer = self.rruleset.after(after, inc=False)
        if candidate_defer is None:
            raise ValueError
        else:
            candidate_defer = arrow.get(candidate_defer)
        candidate_expire = candidate_defer.shift(seconds=self.ttl_seconds)
        return (candidate_defer, candidate_expire)

    @classmethod
    def from_rruleset(
        cls,
        *,
        rruleset: dateutil.rrule.rruleset,
        ttl: datetime.timedelta,
        summary_md: str,
        body_md: Optional[str],
        source_path: str,
    ) -> RepeatingBroadcastMessage:
        """Create a RepeatingBroadcastMessage from a recurring rule set
        and a TTL duration for each message event.
        """
        # Get the next defer date from now (but rewinding by the ttl in case
        # the message is active **right now**.
        defer = rruleset.after(
            arrow.utcnow().shift(seconds=-ttl.total_seconds()).datetime,
            inc=True,
        )
        if defer is None:
            raise ValueError(
                "No future events can be schedule with this rruleset"
            )
        else:
            defer = arrow.get(defer)
        expire = defer.shift(seconds=ttl.total_seconds())
        return cls(
            rruleset=rruleset,
            ttl=ttl,
            defer=defer,
            expire=expire,
            summary_md=summary_md,
            body_md=body_md,
            source_path=source_path,
        )
