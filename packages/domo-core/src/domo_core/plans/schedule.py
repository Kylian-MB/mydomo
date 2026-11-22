"""When a repeating plan is due again (spec 6.9): an iCalendar rule (RFC 5545 ``RRULE``), computed
in the household's time zone by ``dateutil.rrule``.

A rule is the ``RRULE`` part alone, such as ``FREQ=WEEKLY;BYDAY=MO;BYHOUR=9;BYMINUTE=0``, at most
``MAX_REPEAT`` characters (an ``RRULE:`` in front is accepted). It has no ``DTSTART``: the series
starts at the time it is computed from (``after``), whose hour, minute and weekday fill in what
the rule does not say, and always at the second 0; when the rule gives its minutes (``BYMINUTE``)
it starts at the minute 0, and when it gives its hours (``BYHOUR``) at the hour 0, so that "every
15 minutes at 0, 15, 30, 45" or "every two hours at 8, 10..." count from there, whatever the
minute they are set at. A rule that gives an hour and no minute is at the minute 0 of that hour
("every day at 8"), not at the minute it was computed at.
``UNTIL`` is read in the household's time zone, whatever offset it is written with.

The rule is computed on the household's clock: "every day at 08:00" stays 08:00 there over a
change of the clock, and an hour that does not exist that day (skipped by the change) is the same
time an hour later. What is returned is in UTC, and always after the time it is computed from: the
same hour twice in a night (the clock goes back) never gives a time already past.

``COUNT`` is how many times the plan is still due, the next one included (``plans.service.fired``
counts it down): ``next_after`` leaves it out.

A rule is refused (``valid_repeat``) when it is not one ``dateutil`` reads, when it is due more
often than once a minute (``FREQ=SECONDLY``, or several seconds of a minute), when a number is
out of its bounds (``INTERVAL=0``, which would never end, a 32nd day of a month...), and when a
rule of weeks, days, hours or minutes is limited by the day of a month or of a year, a week
number or a position (``BYMONTHDAY``, ``BYYEARDAY``, ``BYWEEKNO``, ``BYSETPOS``): such a rule may
give no time ever, and finding it out would take ``dateutil`` seconds; the same said with
``FREQ=MONTHLY`` or ``FREQ=YEARLY`` is answered in less than a second.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from zoneinfo import ZoneInfo

from dateutil.rrule import rrule, rrulestr

MAX_REPEAT: Final = 200
# The parts of a rule (RFC 5545 3.3.10), and those that limit the days by a date or a position.
_PARTS: Final = frozenset(
    {
        "FREQ",
        "INTERVAL",
        "COUNT",
        "UNTIL",
        "BYSECOND",
        "BYMINUTE",
        "BYHOUR",
        "BYDAY",
        "BYMONTHDAY",
        "BYYEARDAY",
        "BYWEEKNO",
        "BYMONTH",
        "BYSETPOS",
        "WKST",
    }
)
_BY_DATE: Final = frozenset({"BYMONTHDAY", "BYYEARDAY", "BYWEEKNO", "BYSETPOS"})
_SHORT_PERIODS: Final = frozenset({"WEEKLY", "DAILY", "HOURLY", "MINUTELY"})
_PART: Final = re.compile(r"[A-Z]+=[A-Z0-9,+\-]+")
_COUNT: Final = re.compile(r"COUNT=\d+", re.IGNORECASE)
# A day of ``BYDAY``: a weekday, with its position in the month or the year in front of it or not.
_WEEKDAY: Final = re.compile(r"([+-]?)(\d{1,2})?(MO|TU|WE|TH|FR|SA|SU)")
# Where the series is read from to check a rule: any time does.
_CHECKED_FROM: Final = datetime(2000, 1, 1)
# The numbers a part may hold (RFC 5545 3.3.10), and whether they may count from the end: what
# ``dateutil`` would take otherwise may never give a time (``INTERVAL=0`` never ends).
_RANGES: Final[dict[str, tuple[int, int, bool]]] = {
    "INTERVAL": (1, 10_000, False),
    "BYSECOND": (0, 59, False),
    "BYMINUTE": (0, 59, False),
    "BYHOUR": (0, 23, False),
    "BYMONTH": (1, 12, False),
    "BYMONTHDAY": (1, 31, True),
    "BYYEARDAY": (1, 366, True),
    "BYWEEKNO": (1, 53, True),
    "BYSETPOS": (1, 366, True),
}


def _in_range(value: str, least: int, most: int, from_the_end: bool) -> bool:
    """Whether each of the numbers of ``value`` (apart by commas) is within ``least`` and
    ``most``, or within ``-most`` and ``-least`` when it may count from the end."""
    for number in value.split(","):
        negative = from_the_end and number.startswith("-")
        digits = number.removeprefix("-") if negative else number.removeprefix("+")
        if not digits.isdigit() or not least <= int(digits) <= most:
            return False
    return True


def _days_in_range(value: str, freq: str, by_month: bool) -> bool:
    """Whether each day of ``BYDAY`` is a weekday, and its position, when it has one, is one a
    rule of ``freq`` can give (RFC 5545 3.3.10): of the month (1 to 5, from the start or the end)
    in a rule of months, or in a rule of years limited to months (``by_month``); of the year (1
    to 53) in a rule of years; none in any other rule. ``dateutil`` fails on what it cannot give
    (``+60MO``), instead of saying it gives nothing."""
    for day in value.split(","):
        found = _WEEKDAY.fullmatch(day)
        if found is None:
            return False
        sign, position, _ = found.groups()
        if position is None:
            if sign:
                return False
            continue
        if freq == "MONTHLY" or (freq == "YEARLY" and by_month):
            most = 5
        elif freq == "YEARLY":
            most = 53
        else:
            return False
        if not 1 <= int(position) <= most:
            return False
    return True


@dataclass(frozen=True)
class _Rule:
    """A rule, read: its parts without ``COUNT`` (``text``, what ``dateutil`` is given),
    ``COUNT`` (``None`` without one), and whether it gives its minutes (``by_minute``) and its
    hours (``by_hour``): its series then starts at the minute 0, or the hour 0, of its first time,
    so that an interval counts from there whatever minute it is set at."""

    text: str
    count: int | None
    by_minute: bool
    by_hour: bool

    def start(self, since: datetime, zone: ZoneInfo) -> datetime:
        """Where the series from ``since`` starts, on the household's clock without a zone."""
        local = since.astimezone(zone).replace(tzinfo=None, second=0, microsecond=0)
        if self.by_minute:
            local = local.replace(minute=0)
        if self.by_hour:
            local = local.replace(hour=0)
        return local


def _read(repeat: str) -> _Rule | None:
    """``repeat`` read and checked (see the module docstring), or ``None``."""
    if not 1 <= len(repeat) <= MAX_REPEAT:
        return None
    given = repeat.strip().upper()
    given = given.removeprefix("RRULE:")
    parts = given.split(";")
    if not all(_PART.fullmatch(part) for part in parts):
        return None
    named = dict(part.split("=", 1) for part in parts)
    if len(named) != len(parts) or not set(named) <= _PARTS or "FREQ" not in named:
        return None
    count: int | None = None
    if "COUNT" in named:
        if not named["COUNT"].isdigit() or int(named["COUNT"]) < 1 or "UNTIL" in named:
            return None
        count = int(named.pop("COUNT"))
    if "BYHOUR" in named and "BYMINUTE" not in named:
        named["BYMINUTE"] = "0"
    text = ";".join(f"{name}={value}" for name, value in named.items())
    if named["FREQ"] == "SECONDLY" or len(named.get("BYSECOND", "0").split(",")) > 1:
        return None
    if not all(
        _in_range(named[name], *bounds) for name, bounds in _RANGES.items() if name in named
    ):
        return None
    if named["FREQ"] in _SHORT_PERIODS and _BY_DATE & set(named):
        return None
    if "BYDAY" in named and not _days_in_range(named["BYDAY"], named["FREQ"], "BYMONTH" in named):
        return None
    try:
        _rule(text, _CHECKED_FROM)
    except (ValueError, TypeError, OverflowError, KeyError, IndexError):
        return None
    return _Rule(text=text, count=count, by_minute="BYMINUTE" in named, by_hour="BYHOUR" in named)


def _rule(text: str, start: datetime) -> rrule:
    """The rule ``text`` from ``start`` (a time of the household's clock, without a zone)."""
    read = rrulestr(text, dtstart=start, ignoretz=True)
    if not isinstance(read, rrule):
        raise ValueError("A plan repeats by one rule.")
    return read


def valid_repeat(repeat: str) -> bool:
    """Whether ``repeat`` is a rule a plan may repeat by (see the module docstring)."""
    return _read(repeat) is not None


def remaining(repeat: str) -> int | None:
    """How many times a plan that repeats by ``repeat`` is still due, the next one included
    (``COUNT``); ``None`` when the rule does not count, or is no rule."""
    read = _read(repeat)
    return None if read is None else read.count


def with_count(repeat: str, count: int) -> str:
    """``repeat`` as it is written, with its ``COUNT`` lowered to ``count`` (1 or more, never
    more than it was: the rule stays within ``MAX_REPEAT``); ``ValueError`` for a rule
    ``valid_repeat`` refuses or one that does not count."""
    read = _read(repeat)
    if read is None or read.count is None or not 1 <= count <= read.count:
        raise ValueError("Not a rule a plan counts down.")
    return _COUNT.sub(f"COUNT={count}", repeat, count=1)


def catch_up(
    repeat: str, *, since: datetime, now: datetime, zone: ZoneInfo
) -> tuple[datetime | None, int]:
    """For a plan that repeats by ``repeat`` and was due at ``since`` (an aware time, where its
    series starts): the first time of its series after ``now``, in UTC (``None`` when it gives
    nothing more, or for a rule ``valid_repeat`` refuses), and how many times it gave after
    ``since`` and up to ``now`` (missed while Domo was away). ``COUNT`` is left out; the series
    is read once: a series of every minute missed for a year is about half a million times, two
    seconds or so (``dateutil`` has no way to start it later without changing it), which is why
    ``plans.service`` runs it in a worker thread."""
    read = _read(repeat)
    if read is None:
        return None, 0
    start = read.start(since, zone)
    missed = 0
    try:
        for found in _rule(read.text, start):
            moment = found.replace(tzinfo=zone).astimezone(UTC)
            if moment <= since:
                continue
            if moment > now:
                return moment, missed
            missed += 1
    except (ValueError, OverflowError, IndexError):
        return None, missed
    return None, missed


def next_after(repeat: str, after: datetime, zone: ZoneInfo) -> datetime | None:
    """The next time a plan that repeats by ``repeat`` is due after ``after`` (an aware time), in
    UTC, its series computed from ``after`` in the household's time zone ``zone``; ``None`` when
    the rule gives nothing more. Never a time at or before ``after``."""
    upcoming, _ = catch_up(repeat, since=after, now=after, zone=zone)
    return upcoming


def as_local(moment: datetime | None, zone: ZoneInfo | None) -> str | None:
    """``moment`` in ISO 8601 with its offset, on the household's clock (UTC when it has none):
    how Domo is told when a plan is due."""
    return None if moment is None else moment.astimezone(zone or UTC).isoformat()
