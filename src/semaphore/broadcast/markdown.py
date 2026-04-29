"""Support for parsing broadcast messages from Markdown data with YAML
front matter.
"""

from __future__ import annotations

import datetime
import enum
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Annotated, Any, Literal, Self

import arrow
import dateutil
import dateutil.parser
import dateutil.rrule
import yaml
from markdown_it import MarkdownIt
from mdformat.renderer import MDRenderer
from mdit_py_plugins.front_matter import front_matter_plugin
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainValidator,
    ValidationInfo,
    field_validator,
    model_validator,
)

from .models import (
    BroadcastCategory,
    BroadcastMessage,
    FixedExpirationScheduler,
    OneTimeScheduler,
    OpenEndedScheduler,
    PermaScheduler,
    RecurringScheduler,
)

if TYPE_CHECKING:
    from markdown_it.token import Token

    from .models import Scheduler

__all__ = ["BroadcastMarkdown", "BroadcastMarkdownFrontMatter"]

md = MarkdownIt("gfm-like").use(front_matter_plugin)
"""Markdown parser tuned for GitHub Flavored Markdown syntax and supporting
front matter.

See https://markdown-it-py.readthedocs.io/en/latest/using.html#the-parser
"""
timespan_pattern = re.compile(
    r"((?P<weeks>\d+?)\s*(weeks|week|w))?\s*"
    r"((?P<days>\d+?)\s*(days|day|d))?\s*"
    r"((?P<hours>\d+?)\s*(hours|hour|hr|h))?\s*"
    r"((?P<minutes>\d+?)\s*(minutes|minute|mins|min|m))?\s*"
    r"((?P<seconds>\d+?)\s*(seconds|second|secs|sec|s))?$"
)
"""Regular expression pattern for a time duration."""


def parse_timedelta(text: str) -> datetime.timedelta:
    """Parse a `datetime.timedelta` from a string containing integer numbers
    of weeks, days, hours, minutes, and seconds.
    """
    m = timespan_pattern.match(text.strip())
    if m is None:
        raise ValueError(f"Could not parse a timespan from {text!r}.")
    td_args = {k: int(v) for k, v in m.groupdict().items() if v is not None}
    return datetime.timedelta(**td_args)


class BroadcastMarkdown:
    """A representation of a markdown file containing broadcast message
    content and metadata.

    Properties
    ----------
    text : `str`
        The content of the markdown message (including YAML-formatted
        front-matter).
    identifier : `str`
        A unique identifier that is associated with the markdown content.
    """

    def __init__(self, text: str, identifier: str) -> None:
        self._text = text
        self.identifier = identifier
        self._md_env: dict[Any, Any] = {}
        self._md_tokens = md.parse(text, self._md_env)
        self._metadata = self._parse_metadata()

    def _parse_metadata(self) -> BroadcastMarkdownFrontMatter:
        frontmatter_token = self._get_front_matter_token()
        yaml_data = yaml.safe_load(frontmatter_token.content)
        return BroadcastMarkdownFrontMatter.model_validate(yaml_data)

    def _get_front_matter_token(self) -> Token:
        for token in self._md_tokens:
            if token.type == "front_matter":
                return token
        raise ValueError(
            "A front_matter token is not present in the markdown content."
        )

    @property
    def metadata(self) -> BroadcastMarkdownFrontMatter:
        """The broadcast's metadata."""
        return self._metadata

    @property
    def text(self) -> str:
        """The full text of the markdown message (including front-matter)."""
        return self._text

    @property
    def body(self) -> str | None:
        """The text of the markdown body or `None` if the message doesn't have
        body content.
        """
        body_tokens = [t for t in self._md_tokens if t.type != "front_matter"]
        if len(body_tokens) == 0:
            return None
        else:
            return MDRenderer().render(body_tokens, md.options, self._md_env)

    def is_relevant_to_env(self, env_name: str) -> bool:
        """Determine if this broadcast message is relevant to the given
        Phalanx environment name given the ``env`` key in the front matter.

        A message is considered "relevant" if the message's ``env`` key isn't
        set (`None`) or if the given environment is in the list of
        environment names.

        Parameters
        ----------
        env_name : `str`
            Name of the Phalanx environment (e.g., `idf-prod`).

        Returns
        -------
        `bool`
            `True`, if the message should be included in that environment's
            broadcast message. `False` otherwise.
        """
        return (self._metadata.env is None) or (env_name in self._metadata.env)

    def extract_content(self, *, get_summary: bool) -> str | None:
        if self.body is None:
            raise RuntimeError("No body provided")

        paragraphs = self.body.split("\n\n")
        new_summary = paragraphs[0]

        del paragraphs[0]
        new_body = "\n\n".join(paragraphs)

        if get_summary:
            return new_summary
        else:
            return new_body

    @property
    def extracted_summary(self) -> str:
        content = self.extract_content(get_summary=True)

        if content is None:
            raise RuntimeError("No summary found")

        return content

    @property
    def extracted_body(self) -> str | None:
        content = self.extract_content(get_summary=False)

        if content == "":
            return None
        else:
            return content

    def to_broadcast(self) -> BroadcastMessage:
        """Export a BroadcastMessage from the markdown content.

        Returns
        -------
        `semaphore.broadcast.data.BroadcastMessage`
            The broadcast message.
        """
        if self.body is not None and self.metadata.summary is None:
            new_summary = self.extracted_summary

            new_body = self.extracted_body
        else:
            if self.metadata.summary is None:
                raise RuntimeError(
                    "Summary metadata must be set if body is empty"
                )

            new_summary = self.metadata.summary

            new_body = self.body

        return BroadcastMessage(
            identifier=self.identifier,
            summary_md=new_summary,
            body_md=new_body,
            scheduler=self._make_scheduler(),
            enabled=self.metadata.enabled,
            category=self.metadata.category,
        )

    def _make_scheduler(self) -> Scheduler:
        if self.metadata.defer is not None:
            if self.metadata.expire is not None:
                return OneTimeScheduler(
                    self.metadata.defer, self.metadata.expire
                )
            elif self.metadata.ttl is not None:
                return OneTimeScheduler.from_ttl(
                    self.metadata.defer, self.metadata.ttl
                )
            else:
                return OpenEndedScheduler(self.metadata.defer)
        elif self.metadata.expire is not None:
            # In this case, there is an expiration, but the defer must be
            # none, so it is a fixed-expiration scheduler
            return FixedExpirationScheduler(self.metadata.expire)
        elif self.metadata.rules is not None and self.metadata.ttl is not None:
            # Create a rruleset
            rset = dateutil.rrule.rruleset(cache=True)
            for rule in self.metadata.rules:
                if rule.date is not None:
                    if rule.exclude:
                        rset.exdate(rule.to_datetime())
                    else:
                        rset.rdate(rule.to_datetime())
                elif isinstance(rule, RecurringRule):
                    if rule.exclude:
                        rset.exrule(rule.to_rrule())
                    else:
                        rset.rrule(rule.to_rrule())
            return RecurringScheduler(rruleset=rset, ttl=self.metadata.ttl)
        else:
            return PermaScheduler()


class FreqEnum(enum.StrEnum):
    """An enumeration of frequency labels for RecurringRule."""

    # These are lower-cased versions of dateutil.rrule frequency attribute
    # constants. The to_rrule_freq method transforms these labels
    # to dateutil values (integers) for use with dateutil.
    yearly = "yearly"
    monthly = "monthly"
    weekly = "weekly"
    hourly = "hourly"
    minutely = "minutely"

    def to_rrule_freq(self) -> Literal[0, 1, 2, 3, 4, 5, 6]:
        """Convert the frequency to an integer for use as the ``freq``
        parameter in `dateutil.rrule.rrule`.
        """
        return getattr(dateutil.rrule, self.name.upper())


class WeekdayEnum(enum.StrEnum):
    """A enumeration of weekday names."""

    sunday = "sunday"
    monday = "monday"
    tuesday = "tuesday"
    wednesday = "wednesday"
    thursday = "thursday"
    friday = "friday"
    saturday = "saturday"

    def to_rrule_weekday(self) -> dateutil.rrule.weekday:
        """Convert the weekday to an `dateutil.rrule.weekday` for use with the
        ``byweekday`` and ``wkst`` parameter of `dateutil.rrule.rrule`.
        """
        if self.name == "sunday":
            return dateutil.rrule.SU
        elif self.name == "monday":
            return dateutil.rrule.MO
        elif self.name == "tuesday":
            return dateutil.rrule.TU
        elif self.name == "wednesday":
            return dateutil.rrule.WE
        elif self.name == "thursday":
            return dateutil.rrule.TH
        elif self.name == "friday":
            return dateutil.rrule.FR
        else:
            return dateutil.rrule.SA


class ByWeekday(BaseModel):
    """A Pydantic model for the ``by_weekday`` field in the `RecurringRule`
    model.
    """

    day: WeekdayEnum
    """The day of the week."""

    index: int | None = None
    """The index of the weekday. For example, with a monthly recurrency
    frequency, an index of ``1`` means the first of that weekday of the
    month.
    """

    def to_rrule_weekday(self) -> dateutil.rrule.weekday:
        """Convert to a `dateutil.rrule.weekday`, accounting for the index."""
        weekday = self.day.to_rrule_weekday()
        if self.index is not None:
            return weekday(self.index)
        else:
            return weekday


class RecurringRule(BaseModel):
    """A recurring rule (rrule) to include or exclude from a recurring
    schedule.

    Notes
    -----
    This model is intended to be an inteferace for defining
    `dateutil.rrule.rrule` instances. In turn, rrule is an implementation
    of :rfc:`5545` (iCalendar). While the fields in this model generally match
    up to `~dateutil.rrule.rrule` parameters and :rfc:`5545` syntax, the field
    names here are slightly modified for consistency within the Semaphore app.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    timezone: Annotated[
        datetime.tzinfo | None, PlainValidator(convert_to_tzinfo)
    ] = None
    """Default timezone for any datetime fields that don't contain explicit
    datetimes.
    """

    date: arrow.Arrow | None = None
    """A fixed datetime to include (or exclude) from the recurrence."""

    freq: FreqEnum | None = None
    """Frequency of recurrence."""

    interval: int = 1
    """The interval between each iteration.

    For example, if ``freq`` is monthly, an interval of ``2`` means that the
    rule triggers every two months.
    """

    start: arrow.Arrow | None = None
    """The date when the repeating rule starts. If not set, the rule is
    assumed to start now.
    """

    end: arrow.Arrow | None = None
    """Then date when this rule ends. The last recurrence is the datetime
    that is less than or equal to this date. If not set, the rule can recur
    infinitely.
    """

    count: int | None = None
    """The number of occurrences of this recurring rule. The ``count`` must
    be used exclusively of the ``end`` date field.
    """

    week_start: WeekdayEnum | None = None
    """The week start day for weekly frequencies."""

    by_set_position: list[int] | None = None
    """Each integer specifies the occurence number within the recurrence
    frequency (freq).

    For example, with a monthly frequency, and a by_weekday of Friday, a
    value of ``1`` specifies the first Friday of the month. Likewise, ``-1``
    specifies the last Friday of the month.
    """

    by_month: list[int] | None = None
    """The months (1-12) when the recurrence happens. Use negative integers
    to specify an index from the end of the year.
    """

    by_month_day: list[int] | None = None
    """The days of the month (1-31) when the recurrence happens. Use negative
    integers to specify an index from the end of the month.
    """

    by_year_day: list[int] | None = None
    """The days of the year (1-366; allowing for leap years) when the
    recurrence happens. Use negative integers to specify a day relative to the
    end of the year.
    """

    by_week: list[int] | None = None
    """The weeks of the year (1-52) when the recurrence happens. Use negative
    integers to specify a week relative to the end of the year. The definition
    of week matches ISO 8601: the first week of the year is the one with at
    least 4 days.
    """

    by_weekday: list[ByWeekday] | None = None
    """The days of the week when the recurrence happens."""

    by_hour: list[Annotated[int, Field(ge=0, le=23)]] | None = None
    """The hours of the day (0-23) when the recurrence happens."""

    by_minute: list[Annotated[int, Field(ge=0, le=59)]] | None = None
    """The minutes of the hour (0-23) when the recurrence happens."""

    by_second: list[Annotated[int, Field(ge=0, le=59)]] | None = None
    """The seconds of the minute (0-59) when the recurrence happens."""

    exclude: bool = False
    """Set to True to exclude these events from the schedule."""

    @field_validator("date", "start", "end", mode="before")
    @classmethod
    def preprocess_optional_arrow(
        cls, v: Any, info: ValidationInfo
    ) -> arrow.Arrow | None:
        """Convert a datetime into a arrow.Arrow, or None."""
        default_tz = info.data.get("timezone", dateutil.tz.UTC)
        return convert_to_arrow(v, default_tz=default_tz)

    @field_validator("by_set_position", "by_year_day")
    @classmethod
    def check_year_day_index(cls, v: list[int]) -> list[int]:
        for elem in v:
            if (elem >= 1 and elem <= 366) or (elem <= -1 and elem >= 366):
                continue
            raise ValueError(
                "value must be in the range [1, 366] or [-366, -1]"
            )
        return v

    @field_validator("by_month")
    @classmethod
    def check_month_index(cls, v: list[int]) -> list[int]:
        for elem in v:
            if (elem >= 1 and elem <= 12) or (elem <= -1 and elem >= -12):
                continue
            raise ValueError("value must be in the range [1, 12] or [-12, -1]")
        return v

    @field_validator("by_month_day")
    @classmethod
    def check_month_day(cls, v: list[int]) -> list[int]:
        for elem in v:
            if (elem >= 1 and elem <= 31) or (elem <= -1 and elem >= -31):
                continue
            raise ValueError("value must be in the range [1, 31] or [-31, -1]")
        return v

    @field_validator("by_week")
    @classmethod
    def check_week(cls, v: list[int]) -> list[int]:
        for elem in v:
            if (elem >= 1 and elem <= 52) or (elem <= -1 and elem >= -52):
                continue
            raise ValueError("value must be in the range [1, 52] or [-52, -1]")
        return v

    @model_validator(mode="after")
    def check_combinations(self) -> Self:
        """Validate that fields are used together correctly.

        Notes
        -----
        Rules:

        - ``end`` and ``count`` cannot be used together.
        - ``date`` cannot be used with the recurrence settings
        - If ``date`` is none, ``freq`` must be set
        """
        if self.date is None and self.freq is None:
            raise ValueError('"freq" must be set for a recurring rule.')
        if self.end is not None and self.count is not None:
            raise ValueError('"end" and "count" cannot be set simultaneously.')
        if self.date is not None:
            # all recurring settings must not be set
            recurring_attributes = [
                "freq",
                "start",
                "end",
                "count",
                "week_start",
                "by_set_position",
                "by_month",
                "by_month_day",
                "by_year_day",
                "by_hour",
                "by_minute",
                "by_second",
            ]
            for attr in recurring_attributes:
                if getattr(self, attr, None) is not None:
                    raise ValueError(
                        '"date" cannot be used with fields for a recurring '
                        "rule."
                    )
        return self

    def to_rrule(self) -> dateutil.rrule.rrule:
        """Export to a `dateutil.rrule.rrule`."""
        if self.date is not None:
            raise RuntimeError(
                "Cannot export a fixed-date based rule as an rrule. "
                "Use to_datetime() in this case."
            )
        if self.freq is None:
            raise RuntimeError(
                'Cannot export an rrule without a "freq" field.'
            )
        return dateutil.rrule.rrule(
            freq=self.freq.to_rrule_freq(),
            dtstart=self.start.datetime if self.start else None,
            interval=self.interval,
            wkst=(
                self.week_start.to_rrule_weekday() if self.week_start else None
            ),
            until=self.end.datetime if self.end else None,
            bysetpos=self.by_set_position,
            bymonth=self.by_month,
            bymonthday=self.by_month_day,
            byyearday=self.by_year_day,
            byweekno=self.by_week,
            byweekday=(
                [w.to_rrule_weekday() for w in self.by_weekday]
                if self.by_weekday
                else None
            ),
            byhour=self.by_hour,
            byminute=self.by_minute,
            bysecond=self.by_second,
        )

    def to_datetime(self) -> datetime.datetime:
        """Export to a `datetime.datetime`."""
        if self.date is None:
            raise RuntimeError(
                "Cannot export a recurrence-based rule as a single date. "
                "Use to_rrule() in this case."
            )
        return self.date.datetime


class BroadcastMarkdownFrontMatter(BaseModel):
    """A pydantic model describing the front-matter from a markdown broadcast
    message.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    summary: str | None = None
    """Broadcast summary message.

    If not set, summary will be set to None then default to the first
    paragraph of the body.
    """

    env: list[str] | None = None
    """The list of applicable environments. None implies that the broadcast
    is applicable to all environments.
    """

    timezone: Annotated[
        datetime.tzinfo | None, PlainValidator(convert_to_tzinfo)
    ] = dateutil.tz.UTC
    """Default timezone for any datetime fields that don't contain explicit
    datetimes.

    If not set, the default timezone is UTC.
    """

    defer: arrow.Arrow | None = None
    """Date when the message is deferred to start."""

    expire: arrow.Arrow | None = None
    """Date when the message expires."""

    ttl: Annotated[
        datetime.timedelta | None, PlainValidator(convert_to_timedelta)
    ] = None
    """Time duration if `expire` is not set with `defer`."""

    rules: list[RecurringRule] | None = None
    """For creating a repeating schedule, a list of rrule or dates to
    include or exclude.
    """

    enabled: bool = True
    """Toggle to disable a message, overriding the scheduling."""

    category: BroadcastCategory = BroadcastCategory.notice
    """Broadcast category."""

    @model_validator(mode="before")
    @classmethod
    def propagate_timezone(
        cls, values: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Pre-validate by propagating timezone info from the top-level,
        if present, into individual rules items, if present, and if those
        items do not already have a non-None timezone.
        """
        if "timezone" in values:
            default_tz = values["timezone"]
            if "rules" in values:
                for r in values["rules"]:
                    if r.get("timezone") is None:
                        r["timezone"] = default_tz

        return values

    @field_validator("env", mode="before")
    def preprocess_env(cls, v: str | list[str]) -> list[str] | None:
        """Convert the string form of the env keyword to a list, supporting
        comma-separated lists as well.
        """
        if isinstance(v, str):
            return [s.strip() for s in v.split(",")]
        else:
            return v

    @field_validator("defer", "expire", mode="before")
    @classmethod
    def preprocess_optional_arrow(
        cls, v: Any, info: ValidationInfo
    ) -> arrow.Arrow | None:
        """Convert a datetime into a arrow.Arrow, or None."""
        default_tz = info.data.get("timezone", dateutil.tz.UTC)
        return convert_to_arrow(v, default_tz=default_tz)

    @model_validator(mode="after")
    def check_schedule_combinations(self) -> Self:
        # expire and ttl cannot coexist
        if self.expire is not None and self.ttl is not None:
            raise ValueError(
                '"expire" and "ttl" fields cannot be used together.'
            )

        # defer must be before expire
        if self.defer is not None and self.expire is not None:
            if self.expire < self.defer:
                raise ValueError('"expire" cannot happen before "defer"')

        # rules does not coexist with defer or expire
        if self.rules is not None and self.defer is not None:
            raise ValueError(
                '"rules" and "defer" fields cannot be used together.'
            )
        if self.rules is not None and self.expire is not None:
            raise ValueError(
                '"rules" and "expire" fields cannot be used together.'
            )

        # rules must be used with ttl
        if self.rules is not None and self.ttl is None:
            raise ValueError('"ttl" must be specified with rules.')

        return self


def convert_to_tzinfo(v: Any) -> datetime.tzinfo:
    """Convert a value to a datetime.tzinfo.

    This function is intended to be used in a validator for Pydantic models
    and will raise ValueError or TypeError if ``v`` is not an appropriate
    value.

    Parameters
    ----------
    v : datetime.tzinfo, str
        A value to convert into a timezone.
    """
    if isinstance(v, datetime.tzinfo):
        return v
    elif isinstance(v, str):
        tz = dateutil.tz.gettz(v)
        if not isinstance(tz, datetime.tzinfo):
            raise TypeError(f"Could not parse timezone from {v!s}")
        return tz
    else:
        raise TypeError(f"Incorrect type for timezone, got {v!r}.")


def convert_to_arrow(
    v: Any, default_tz: Any | None = None
) -> arrow.Arrow | None:
    """Convert a value to an arrow.Arrow datetime.

    This function is intended to be used in a validator for Pydantic models,
    and will raise ValueErrors or TypeErrors if ``v`` is not an appropriate
    value.

    Parameters
    ----------
    v : datetime.date, datetime.datetime, str
        A value to convert into a datetime.
    default_tz : datetime.tzinfo
        A default timezone. If neither ``v`` has a timezone or ``default_tz``
        is set, the default timezone is UTC.
    """
    if v is None:
        return None
    if isinstance(v, datetime.date):
        # Pydantic pre-parses YYYY-MM-DD into a datetime.date even if
        # we didn't declare the field as a datetime.date type
        dt = datetime.datetime.combine(v, datetime.time())
    elif isinstance(v, datetime.datetime):
        # Pydantic pre-parses timestamps into datetime.datetime even if
        # we didn't declare the field as a datetime.datetime type
        # Pydantic pre-parses into a datetime
        dt = v
    elif isinstance(v, str):
        try:
            dt = dateutil.parser.parse(v, fuzzy=True, yearfirst=True)
        except (ValueError, OverflowError) as e:
            raise ValueError("Could not parse date") from e
    else:
        raise TypeError(f"Not a string (got {v!r})")

    if dt.tzinfo:
        # Parsed date includes a timezone.
        return arrow.get(dt)
    # naive datetime, so default to given timezone
    elif default_tz:
        return arrow.get(dt, default_tz)
    else:
        return arrow.get(dt, dateutil.tz.UTC)


def convert_to_timedelta(v: Any) -> datetime.timedelta | None:
    """Convert a value to a datetime.timedelta.

    This function is intended to be used in a validator for Pydantic models,
    and will raise ValueErrors or TypeErrors if ``v`` is not an appropriate
    value.

    Parameters
    ----------
    v : datetime.timedelta, str
        A value to convert into a timedelta.
    """
    if v is None:
        return None
    elif isinstance(v, str):
        return parse_timedelta(v)
    elif isinstance(v, datetime.timedelta):
        return v
    else:
        raise TypeError(f"Cannot parse timedelta from {v!r}")
