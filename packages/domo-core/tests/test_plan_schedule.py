"""When a repeating plan is due again: its rule, on the household's clock (spec 6.9, task 8).

``next_after`` computes the rule in the household's time zone and gives a time in UTC, always
after the time it is computed from; ``valid_repeat`` refuses what is due more often than once a
minute and what could give no time at all without saying so at once.
"""

import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from domo_core.plans.schedule import (
    as_local,
    catch_up,
    next_after,
    remaining,
    valid_repeat,
    with_count,
)

BRUSSELS = ZoneInfo("Europe/Brussels")
EVERY_DAY_AT_8 = "FREQ=DAILY;BYHOUR=8;BYMINUTE=0"


def local(moment: datetime | None) -> datetime | None:
    return None if moment is None else moment.astimezone(BRUSSELS)


def test_next_after_follows_the_households_zone_over_a_clock_change():
    # Brussels goes from +01:00 to +02:00 in the night of the 2027-03-28.
    before = datetime(2027, 3, 26, 9, 0, tzinfo=UTC)
    first = next_after(EVERY_DAY_AT_8, before, BRUSSELS)
    second = next_after(EVERY_DAY_AT_8, first, BRUSSELS)  # type: ignore[arg-type]
    third = next_after(EVERY_DAY_AT_8, second, BRUSSELS)  # type: ignore[arg-type]
    assert [local(m).hour for m in (first, second, third)] == [8, 8, 8]  # type: ignore[union-attr]
    assert [m.isoformat() for m in (first, second, third)] == [  # type: ignore[union-attr]
        "2027-03-27T07:00:00+00:00",
        "2027-03-28T06:00:00+00:00",
        "2027-03-29T06:00:00+00:00",
    ]
    # And back in October: still 08:00 there.
    autumn = next_after(EVERY_DAY_AT_8, datetime(2027, 10, 30, 6, 30, tzinfo=UTC), BRUSSELS)
    assert autumn is not None and autumn.isoformat() == "2027-10-31T07:00:00+00:00"


def test_an_hour_that_comes_twice_never_gives_a_time_already_past():
    # 02:30 comes twice in Brussels on the 2027-10-31: once at 00:30 UTC, once at 01:30 UTC.
    rule = "FREQ=DAILY;BYHOUR=2;BYMINUTE=30"
    after_the_first = datetime(2027, 10, 31, 0, 45, tzinfo=UTC)
    upcoming = next_after(rule, after_the_first, BRUSSELS)
    assert upcoming is not None and upcoming > after_the_first
    # An hour the change skips (02:30 on the 2027-03-28) is the same time an hour later.
    skipped = next_after(rule, datetime(2027, 3, 27, 12, 0, tzinfo=UTC), BRUSSELS)
    assert skipped is not None and skipped.isoformat() == "2027-03-28T01:30:00+00:00"


def test_an_hour_without_a_minute_is_at_its_minute_zero():
    upcoming = next_after("FREQ=DAILY;BYHOUR=8", datetime(2027, 1, 1, 10, 37, tzinfo=UTC), BRUSSELS)
    assert local(upcoming) == datetime(2027, 1, 2, 8, 0, tzinfo=BRUSSELS)


def test_the_31st_skips_the_short_months():
    rule = "FREQ=MONTHLY;BYMONTHDAY=31;BYHOUR=9;BYMINUTE=0"
    moment = datetime(2027, 1, 1, tzinfo=UTC)
    days = []
    for _ in range(4):
        moment = next_after(rule, moment, BRUSSELS)  # type: ignore[assignment]
        days.append(local(moment).date().isoformat())  # type: ignore[union-attr]
    assert days == ["2027-01-31", "2027-03-31", "2027-05-31", "2027-07-31"]


def test_a_rule_that_gives_nothing_more_ends_the_plan():
    # COUNT is how many times it is still due, the next one included: next_after leaves it out,
    # the plan counts it down (``plans.service.fired``).
    assert remaining("FREQ=DAILY;COUNT=1") == 1
    assert remaining(EVERY_DAY_AT_8) is None
    assert with_count("FREQ=DAILY;COUNT=3", 2) == "FREQ=DAILY;COUNT=2"
    # UNTIL is read on the household's clock: past it, nothing more.
    until = "FREQ=DAILY;BYHOUR=8;BYMINUTE=0;UNTIL=20270105T235959"
    assert next_after(until, datetime(2027, 1, 5, 12, 0, tzinfo=UTC), BRUSSELS) is None
    last = next_after(until, datetime(2027, 1, 4, 12, 0, tzinfo=UTC), BRUSSELS)
    assert local(last) == datetime(2027, 1, 5, 8, 0, tzinfo=BRUSSELS)
    # A rule whose dates exist no more (a 30 February, said yearly) gives nothing, at once.
    started = time.monotonic()
    assert next_after("FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=30", datetime.now(UTC), BRUSSELS) is None
    assert time.monotonic() - started < 2


def test_a_rule_more_often_than_every_minute_is_refused():
    for rule in ("FREQ=SECONDLY", "FREQ=MINUTELY;BYSECOND=0,30"):
        assert not valid_repeat(rule), rule
    # A rule of seconds is refused whatever its interval, even one longer than a minute.
    assert not valid_repeat("RRULE:FREQ=SECONDLY;INTERVAL=90")
    for rule in ("FREQ=MINUTELY", "RRULE:FREQ=HOURLY;BYMINUTE=0,30", "freq=weekly;byday=mo,fr"):
        assert valid_repeat(rule), rule


@pytest.mark.parametrize(
    "rule",
    [
        "",
        "FREQ=DAILY;" + "BYHOUR=8;" * 30,
        "BYHOUR=8",
        "FREQ=DAILY;FREQ=WEEKLY",
        "FREQ=FORTNIGHTLY",
        "FREQ=DAILY;BYHOUR=25",
        # Numbers out of their bounds: INTERVAL=0 would never end.
        "FREQ=MINUTELY;INTERVAL=0",
        "FREQ=DAILY;INTERVAL=-1",
        "FREQ=MONTHLY;BYMONTHDAY=0",
        "FREQ=HOURLY;BYSECOND=61",
        "FREQ=YEARLY;BYWEEKNO=60",
        "FREQ=DAILY;COUNT=0",
        "FREQ=DAILY;COUNT=2;UNTIL=20270101T000000",
        "DTSTART:20270101T000000\nRRULE:FREQ=DAILY",
        "FREQ=DAILY;EXDATE=20270101",
        # A rule of days, hours or weeks limited by a date could take seconds to give nothing.
        "FREQ=DAILY;BYMONTH=2;BYMONTHDAY=30",
        "FREQ=WEEKLY;BYMONTHDAY=31",
        "FREQ=HOURLY;BYSETPOS=3",
        "FREQ=MINUTELY;BYYEARDAY=366",
        # A position of a weekday a rule cannot give (dateutil fails on it), or none it can have.
        "FREQ=MONTHLY;BYDAY=+60MO",
        "FREQ=YEARLY;BYDAY=+60MO",
        "FREQ=YEARLY;BYMONTH=1;BYDAY=+6MO",
        "FREQ=MONTHLY;BYDAY=0MO",
        "FREQ=WEEKLY;BYDAY=+1MO",
        "FREQ=DAILY;BYDAY=-1FR",
        "FREQ=MONTHLY;BYDAY=+MO",
        "FREQ=MONTHLY;BYDAY=XX",
    ],
)
def test_what_is_no_rule_of_a_plan_is_refused(rule: str):
    assert not valid_repeat(rule)
    assert next_after(rule, datetime(2027, 1, 1, tzinfo=UTC), BRUSSELS) is None


def test_a_position_of_a_weekday_is_one_of_the_month_or_of_the_year():
    last_friday = next_after(
        "FREQ=MONTHLY;BYDAY=-1FR;BYHOUR=18", datetime(2027, 1, 1, tzinfo=UTC), BRUSSELS
    )
    assert local(last_friday) == datetime(2027, 1, 29, 18, 0, tzinfo=BRUSSELS)
    twentieth_monday = next_after(
        "FREQ=YEARLY;BYDAY=+20MO;BYHOUR=9", datetime(2027, 1, 1, tzinfo=UTC), BRUSSELS
    )
    assert local(twentieth_monday) == datetime(2027, 5, 17, 9, 0, tzinfo=BRUSSELS)


def test_a_rule_that_gives_its_minutes_or_hours_counts_from_their_zero():
    # Set at 10:07: every quarter of an hour on the quarter, and every two hours on the even hours.
    at_ten_past = datetime(2027, 1, 4, 10, 7, tzinfo=BRUSSELS)
    quarter = next_after("FREQ=MINUTELY;INTERVAL=15;BYMINUTE=0,15,30,45", at_ten_past, BRUSSELS)
    assert local(quarter) == datetime(2027, 1, 4, 10, 15, tzinfo=BRUSSELS)
    at_eleven = datetime(2027, 1, 4, 11, 7, tzinfo=BRUSSELS)
    even = next_after("FREQ=HOURLY;INTERVAL=2;BYHOUR=8,10,12,14", at_eleven, BRUSSELS)
    assert local(even) == datetime(2027, 1, 4, 12, 0, tzinfo=BRUSSELS)
    # A rule that gives neither keeps the minute it is set at.
    every_quarter = next_after("FREQ=MINUTELY;INTERVAL=15", at_ten_past, BRUSSELS)
    assert local(every_quarter) == datetime(2027, 1, 4, 10, 22, tzinfo=BRUSSELS)


def test_the_times_missed_are_counted_and_the_next_is_after_now():
    since = datetime(2027, 1, 1, 7, 0, tzinfo=UTC)  # 08:00 in Brussels
    now = since + timedelta(days=3, hours=2)
    upcoming, missed = catch_up(EVERY_DAY_AT_8, since=since, now=now, zone=BRUSSELS)
    assert (local(upcoming), missed) == (datetime(2027, 1, 5, 8, 0, tzinfo=BRUSSELS), 3)
    # A rule without an hour keeps the time of its first: the series goes on from it.
    upcoming, missed = catch_up("FREQ=DAILY", since=since, now=now, zone=BRUSSELS)
    assert (local(upcoming), missed) == (datetime(2027, 1, 5, 8, 0, tzinfo=BRUSSELS), 3)


def test_a_time_is_told_on_the_household_clock():
    moment = datetime(2027, 1, 1, 17, 0, tzinfo=UTC)
    assert as_local(moment, BRUSSELS) == "2027-01-01T18:00:00+01:00"
    assert as_local(moment, None) == "2027-01-01T17:00:00+00:00"
    assert as_local(None, BRUSSELS) is None
