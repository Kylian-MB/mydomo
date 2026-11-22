"""The plans Domo sets for himself, as the service keeps them (spec 6.9, task 8).

A plan needs a time, a rule or a return; a time already past is refused, so is a ``why`` with the
shape of a secret. A repeating plan that fired is due at the next time of its rule after the
moment it fired, the times it missed while Domo was away not due one by one, and is done when
the rule gives nothing more. Each person's plans are theirs.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from domo_core.memory.service import SecretRefused
from domo_core.plans.models import Plan
from domo_core.plans.service import (
    cancel,
    change,
    due,
    due_on_return,
    fired,
    make,
    waiting_of,
    waiting_plan_of,
)
from sqlalchemy.exc import IntegrityError

NOW = datetime(2027, 1, 4, 9, 0, tzinfo=UTC)  # a Monday, 10:00 in Brussels
BRUSSELS = ZoneInfo("Europe/Brussels")
EVERY_DAY_AT_8 = "FREQ=DAILY;BYHOUR=8;BYMINUTE=0"


def at(day: int, hour: int, minute: int = 0) -> datetime:
    """A time of January 2027 on the household's clock, in UTC."""
    return datetime(2027, 1, day, hour, minute, tzinfo=BRUSSELS).astimezone(UTC)


async def test_a_plan_needs_a_time_a_rule_or_a_return(session, add_person):
    alex = await add_person("Alex")
    with pytest.raises(ValueError):
        await make(session, person_id=alex.id, why="Remind Alex.", now=NOW, zone=BRUSSELS)
    timed = await make(
        session, person_id=alex.id, why="Remind Alex.", now=NOW, zone=BRUSSELS, due_at=at(4, 18)
    )
    back = await make(
        session, person_id=alex.id, why="Ask how it went.", now=NOW, zone=BRUSSELS, on_return=True
    )
    # A rule alone is first due at its first time after now.
    daily = await make(
        session,
        person_id=alex.id,
        why="Morning news.",
        now=NOW,
        zone=BRUSSELS,
        repeat=EVERY_DAY_AT_8,
    )
    assert (timed.state, timed.made_by_domo, back.due_at) == ("waiting", False, None)
    assert daily.due_at == at(5, 8)
    # The database holds it too: a waiting plan with nothing to wait for is refused.
    session.add(Plan(person_id=alex.id, why="Nothing.", state="waiting"))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_a_past_time_is_refused(session, add_person):
    alex = await add_person("Alex")
    for due_at in (NOW, NOW - timedelta(minutes=1)):
        with pytest.raises(ValueError):
            await make(
                session, person_id=alex.id, why="Too late.", now=NOW, zone=BRUSSELS, due_at=due_at
            )
    plan = await make(
        session, person_id=alex.id, why="Later.", now=NOW, zone=BRUSSELS, due_at=at(4, 18)
    )
    with pytest.raises(ValueError):
        await change(session, plan, now=NOW, zone=BRUSSELS, due_at=NOW - timedelta(hours=1))
    assert plan.due_at == at(4, 18)


async def test_why_and_the_exact_words_never_hold_a_secret(session, add_person):
    alex = await add_person("Alex")
    secret = "Give the alarm key sk-abc123def456ghi789jkl0 to Sam."
    with pytest.raises(SecretRefused):
        await make(session, person_id=alex.id, why=secret, now=NOW, zone=BRUSSELS, on_return=True)
    with pytest.raises(SecretRefused):
        await make(
            session,
            person_id=alex.id,
            why="Remind Alex of the alarm.",
            now=NOW,
            zone=BRUSSELS,
            on_return=True,
            their_words=secret,
        )
    assert await waiting_of(session, alex.id) == []


async def test_the_plans_of_a_person_are_theirs_and_the_soonest_come_first(session, add_person):
    alex = await add_person("Alex")
    sam = await add_person("Sam", "child")
    back = await make(
        session, person_id=alex.id, why="On return.", now=NOW, zone=BRUSSELS, on_return=True
    )
    later = await make(
        session, person_id=alex.id, why="Later.", now=NOW, zone=BRUSSELS, due_at=at(6, 9)
    )
    sooner = await make(
        session, person_id=alex.id, why="Sooner.", now=NOW, zone=BRUSSELS, due_at=at(5, 9)
    )
    theirs = await make(
        session, person_id=sam.id, why="Sam's.", now=NOW, zone=BRUSSELS, due_at=at(5, 8)
    )
    assert [p.id for p in await waiting_of(session, alex.id)] == [sooner.id, later.id, back.id]
    assert [p.id for p in await waiting_of(session, alex.id, limit=2)] == [sooner.id, later.id]
    assert await waiting_plan_of(session, alex.id, theirs.id) is None
    assert await waiting_plan_of(session, sam.id, theirs.id) is theirs
    await cancel(session, later, NOW)
    assert await waiting_plan_of(session, alex.id, later.id) is None
    assert [p.id for p in await waiting_of(session, alex.id)] == [sooner.id, back.id]
    assert [p.id for p in await due(session, at(5, 9))] == [theirs.id, sooner.id]
    assert [p.id for p in await due_on_return(session, alex.id, NOW)] == [back.id]


async def test_a_plan_that_does_not_repeat_is_done_once_it_fired(session, add_person):
    alex = await add_person("Alex")
    plan = await make(
        session, person_id=alex.id, why="Once.", now=NOW, zone=BRUSSELS, due_at=at(4, 18)
    )
    await fired(session, plan, now=at(4, 18), zone=BRUSSELS, outcome="Reminded Alex.")
    assert (plan.state, plan.due_at, plan.last_fired_at, plan.last_outcome) == (
        "done",
        None,
        at(4, 18),
        "Reminded Alex.",
    )
    assert await due(session, at(9, 0)) == []


async def test_a_repeating_plan_goes_to_its_next_real_time(session, add_person):
    alex = await add_person("Alex")
    plan = await make(
        session,
        person_id=alex.id,
        why="Morning news.",
        now=NOW,
        zone=BRUSSELS,
        repeat=EVERY_DAY_AT_8,
    )
    await fired(session, plan, now=at(5, 8), zone=BRUSSELS, outcome="Told the news.")
    assert (plan.state, plan.due_at) == ("waiting", at(6, 8))
    # Domo was away four days: it is due at its next time to come, not once for each missed.
    await fired(session, plan, now=at(10, 11), zone=BRUSSELS, outcome="Late, told the news.")
    assert (plan.state, plan.due_at) == ("waiting", at(11, 8))


async def test_a_repeating_plan_fired_by_a_return_keeps_its_time(session, add_person):
    alex = await add_person("Alex")
    plan = await make(
        session,
        person_id=alex.id,
        why="Mondays, or when Alex is back.",
        now=NOW,
        zone=BRUSSELS,
        repeat="FREQ=WEEKLY;BYDAY=MO;BYHOUR=9;COUNT=3",
        on_return=True,
    )
    assert plan.due_at == at(11, 9)
    # Alex came back on Thursday: the return was served, the Monday is still to come.
    await fired(session, plan, now=at(7, 15), zone=BRUSSELS, outcome="Welcomed Alex back.")
    assert (plan.state, plan.due_at, plan.repeat) == (
        "waiting",
        at(11, 9),
        "FREQ=WEEKLY;BYDAY=MO;BYHOUR=9;COUNT=3",
    )
    assert (plan.last_fired_at, plan.last_outcome) == (at(7, 15), "Welcomed Alex back.")
    # On the Monday it goes on as ever.
    await fired(session, plan, now=at(11, 9), zone=BRUSSELS, outcome="Monday.")
    assert (plan.due_at, plan.repeat) == (at(18, 9), "FREQ=WEEKLY;BYDAY=MO;BYHOUR=9;COUNT=2")


async def test_a_rule_that_gives_nothing_more_ends_the_plan(session, add_person):
    alex = await add_person("Alex")
    once = await make(
        session,
        person_id=alex.id,
        why="Once.",
        now=NOW,
        zone=BRUSSELS,
        due_at=at(4, 18),
        repeat="FREQ=DAILY;COUNT=1",
    )
    await fired(session, once, now=at(4, 18), zone=BRUSSELS, outcome="Done.")
    assert (once.state, once.due_at) == ("done", None)
    # Twice: due once more, then done.
    twice = await make(
        session,
        person_id=alex.id,
        why="Twice.",
        now=NOW,
        zone=BRUSSELS,
        repeat=f"{EVERY_DAY_AT_8};COUNT=2",
    )
    await fired(session, twice, now=at(5, 8), zone=BRUSSELS, outcome="First.")
    assert (twice.state, twice.due_at, twice.repeat) == (
        "waiting",
        at(6, 8),
        f"{EVERY_DAY_AT_8};COUNT=1",
    )
    await fired(session, twice, now=at(6, 8), zone=BRUSSELS, outcome="Second.")
    assert (twice.state, twice.due_at) == ("done", None)
    # Three times, two of them missed while Domo was away: done, never in a loop.
    thrice = await make(
        session,
        person_id=alex.id,
        why="Thrice.",
        now=NOW,
        zone=BRUSSELS,
        repeat=f"{EVERY_DAY_AT_8};COUNT=3",
    )
    await fired(session, thrice, now=at(8, 9), zone=BRUSSELS, outcome="Late.")
    assert (thrice.state, thrice.due_at) == ("done", None)
    # An end day (UNTIL) past: done too.
    until = await make(
        session,
        person_id=alex.id,
        why="Until Wednesday.",
        now=NOW,
        zone=BRUSSELS,
        repeat=f"{EVERY_DAY_AT_8};UNTIL=20270106T120000",
    )
    await fired(session, until, now=at(6, 8), zone=BRUSSELS, outcome="Last.")
    assert (until.state, until.due_at) == ("done", None)


async def test_a_change_keeps_what_it_is_not_given(session, add_person):
    alex = await add_person("Alex")
    plan = await make(
        session,
        person_id=alex.id,
        why="Morning news.",
        now=NOW,
        zone=BRUSSELS,
        repeat=EVERY_DAY_AT_8,
    )
    await change(session, plan, now=NOW, zone=BRUSSELS, why="The news, at breakfast.")
    assert (plan.why, plan.due_at, plan.repeat) == (
        "The news, at breakfast.",
        at(5, 8),
        EVERY_DAY_AT_8,
    )
    # A new rule is first due at its first time.
    await change(session, plan, now=NOW, zone=BRUSSELS, repeat="FREQ=WEEKLY;BYDAY=FR;BYHOUR=7")
    assert plan.due_at == at(8, 7)
    # It stops repeating: due at its next time only.
    await change(session, plan, now=NOW, zone=BRUSSELS, repeat=None)
    assert (plan.repeat, plan.due_at) == (None, at(8, 7))
    await cancel(session, plan, NOW)
    with pytest.raises(ValueError):
        await change(session, plan, now=NOW, zone=BRUSSELS, why="Too late.")


async def test_a_plan_goes_with_its_person(session, add_person):
    alex = await add_person("Alex", "adult")
    await make(session, person_id=alex.id, why="Remind.", now=NOW, zone=BRUSSELS, on_return=True)
    await session.delete(alex)
    await session.flush()
    assert await due_on_return(session, alex.id, NOW) == []


async def test_the_plans_of_someone_who_left_are_never_due_and_stay(session, add_person):
    # Final review M1: tried every minute for ever otherwise.
    alex = await add_person("Alex", "adult")
    dana = await add_person("Dana", "guest")
    plans = {
        person.id: [
            await make(
                session,
                person_id=person.id,
                why="Remind.",
                now=NOW,
                zone=BRUSSELS,
                due_at=at(4, 18),
            ),
            await make(
                session, person_id=person.id, why="Back.", now=NOW, zone=BRUSSELS, on_return=True
            ),
        ]
        for person in (alex, dana)
    }
    later = at(5, 9)
    assert {p.person_id for p in await due(session, later)} == {alex.id, dana.id}
    # Alex is removed, Dana's stay ends.
    alex.disabled_at = NOW
    dana.expires_at = at(5, 8)
    await session.flush()
    assert await due(session, later) == []
    assert await due_on_return(session, alex.id, later) == []
    assert await due_on_return(session, dana.id, later) == []
    # Before Dana's stay ended, hers were still due.
    assert [p.person_id for p in await due(session, at(5, 7))] == [dana.id]
    # Nothing is deleted: they still wait.
    for kept in plans.values():
        assert all(plan.state == "waiting" for plan in kept)
