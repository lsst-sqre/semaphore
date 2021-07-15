"""Data containers for broadcast messages."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

import arrow

if TYPE_CHECKING:
    import datetime
    from typing import Optional, Tuple

    import dateutil.rrule


__all__ = [
    "BroadcastMessage",
    "Scheduler",
    "PermaScheduler",
    "RecurringScheduler",
    "OneTimeScheduler",
    "OpenEndedScheduler",
    "FixedExpirationScheduler",
]


class Scheduler(ABC):
    """A scheduler for messages."""

    @abstractmethod
    def is_active(self) -> bool:
        """Tests if the scheduled event is active."""
        raise NotImplementedError()

    @abstractmethod
    def has_future_events(self) -> bool:
        """Tests if the schedule includes a future event (not including the
        current event).
        """
        raise NotImplementedError()

    def is_stale(self) -> bool:
        """True, if the event is neither active or in the future."""
        return not (self.is_active() | self.has_future_events())


class PermaScheduler(Scheduler):
    """A scheduler that is always active."""

    def is_active(self) -> bool:
        return True

    def has_future_events(self) -> bool:
        return False


class FixedExpirationScheduler(Scheduler):
    """A scheduler that is active from now until a fixed date."""

    def __init__(self, end: arrow.Arrow) -> None:
        self._end = end

    @property
    def end(self) -> arrow.Arrow:
        """The end date."""
        return self._end

    def is_active(self) -> bool:
        return arrow.utcnow() < self.end

    def has_future_events(self) -> bool:
        return False


class OpenEndedScheduler(Scheduler):
    """A scheduler that has a fixed start time, but no end time."""

    def __init__(self, start: arrow.Arrow) -> None:
        self._start = start

    @property
    def start(self) -> arrow.Arrow:
        return self._start

    def is_active(self) -> bool:
        return arrow.utcnow() >= self.start

    def has_future_events(self) -> bool:
        if arrow.utcnow() < self.start:
            return True
        else:
            return False


class OneTimeScheduler(Scheduler):
    """A scheduler for a single, fixed, time window.

    Parameters
    ----------
    start : `arrow.Arrow`
        A start date as an arrow object.
    end : `arrow.Arrow`
        An end date as an arrow object.
    """

    def __init__(self, start: arrow.Arrow, end: arrow.Arrow) -> None:
        self._start = start
        self._end = end

    @classmethod
    def from_ttl(
        cls, start: arrow.Arrow, ttl: datetime.timedelta
    ) -> OneTimeScheduler:
        """Create a OneTimeScheduler given a known start date and a TTL.

        Parameters
        ----------
        start : `arrow.Arrow`
            A start date as an arrow object.
        ttl : `datetime.timedela`
            The duration of the event.

        Returns
        -------
        `OneTimeScheduler`
            The scheduler.
        """
        end = start.shift(seconds=ttl.total_seconds())
        return cls(start, end)

    @property
    def start(self) -> arrow.Arrow:
        """The start date."""
        return self._start

    @property
    def end(self) -> arrow.Arrow:
        return self._end

    def is_active(self) -> bool:
        return arrow.utcnow().is_between(self.start, self.end, bounds="[)")

    def has_future_events(self) -> bool:
        if self.start > arrow.utcnow():
            return True
        else:
            return False


class RecurringScheduler(Scheduler):
    """A schedule based on recurrence rule sets (RFC 5546).

    Parameters
    ----------
    rruleset : `dateutil.rrule.rruleset`
        A recurrence ruleset from the dateutil package. Rulesets can contain
        both repeats, one-time dates, one-time exclusions, and so on. The
        rruleset **must** have a UTC timezone. Time zones are not validated
        by the constructor.
    ttl : `datetime.timedela`
        The duration of the event.
    """

    def __init__(
        self, rruleset: dateutil.rrule.rruleset, ttl: datetime.timedelta
    ) -> None:
        self.rruleset = rruleset
        self.ttl = ttl

        # Get the next start date from now (but rewinding by the ttl in case
        # the message is active **right now**.
        start_datetime = rruleset.after(
            arrow.utcnow().shift(seconds=-ttl.total_seconds()).datetime,
            inc=True,
        )
        if start_datetime is None:
            # For consistency with the constructors of schedulers, like
            # OneTimeScheduler, try to build the scheduler with an already-old
            # start.
            start_datetime = rruleset.before(
                arrow.utcnow().shift(seconds=-ttl.total_seconds()).datetime,
                inc=True,
            )
            if start_datetime is None:
                # Could this rruleset ever be scheduled?
                raise ValueError(f"Cannot schedule rruleset: {rruleset!r}")
        self._start = arrow.get(start_datetime)

    @property
    def ttl_seconds(self) -> float:
        """The TTL, in seconds."""
        return self.ttl.total_seconds()

    @property
    def _end(self) -> arrow.Arrow:
        """The end time of the current event."""
        return self._start.shift(seconds=self.ttl_seconds)

    def is_active(self) -> bool:
        self._refresh()
        return arrow.utcnow().is_between(self._start, self._end, bounds="[)")

    def has_future_events(self) -> bool:
        now = arrow.utcnow()
        if now < self._start:
            # Next event is already scheduled
            return True
        elif self.is_active():
            # Currently active, determine if there will be a future event
            try:
                self._propose(self._start.shift(seconds=1))
            except ValueError:
                return False
            return True
        else:
            if self._end < now:
                # Running is_active() above already refreshed the start/end
                # dates, so if _end is in the past, there will never be a
                # future event
                return False
            else:
                return True

    def _refresh(self) -> None:
        if self._end < arrow.utcnow():
            # The last message occurence expired, so let's calculate the next.
            try:
                candidate_start, candidate_end = self._propose(self._start)
            except ValueError:
                return None  # no future event
            while candidate_end < arrow.utcnow():
                # Keep iterating in case windows overlap
                try:
                    candidate_start, candidate_end = self._propose(
                        candidate_start
                    )
                except ValueError:
                    return None  # no future event
            # Set next start date
            self._start = candidate_start

    def _propose(self, after: arrow.Arrow) -> Tuple[arrow.Arrow, arrow.Arrow]:
        """Propose a new start/end time after the given time."""
        candidate_start = self.rruleset.after(after, inc=False)
        if candidate_start is None:
            raise ValueError
        else:
            candidate_start = arrow.get(candidate_start)
        candidate_end = candidate_start.shift(seconds=self.ttl_seconds)
        return (candidate_start, candidate_end)


@dataclass
class BroadcastMessage:
    """A broadcast message, including its content and schedule."""

    source_path: str
    """The path of the source in the GitHub repository, which serves as a
    unique identifier.
    """

    summary_md: str
    """The message message, as markdown."""

    body_md: Optional[str]
    """The body content, as markdown."""

    scheduler: Scheduler
    """The broadcast scheduler.

    Can be one of:

    - `PermaScheduler`
    - `OneTimeScheduler`
    - `RecurringingScheduler`
    - `OneTimeScheduler`
    - `OpenEndedScheduler`
    - `FixedExpirationScheduler`
    """

    enabled: bool = True
    """A toggle for disabling a message, overriding the scheduler."""

    @property
    def active(self) -> bool:
        """Whether the message should be served to clients for display.

        The active state is determined by the scheduler, by can be overridden
        by the "enabled" toggle attribute.
        """
        return self.enabled and self.scheduler.is_active()

    @property
    def stale(self) -> bool:
        """Wether the message is neither being currently displayable now
        or ever in the future.
        """
        return self.scheduler.is_stale()
