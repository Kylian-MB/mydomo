"""What a person's word has been worth, kept as facts (spec 6.1, task 1).

The service is tried directly on the shared rolled-back ``session``."""

from datetime import UTC, datetime, timedelta

import pytest
from domo_core.accounts.models import Person
from domo_core.memory.service import SecretRefused, keep
from domo_core.memory.service import forget as forget_memories
from domo_core.trust.models import TrustFact
from domo_core.trust.score import score
from domo_core.trust.service import (
    MAX_ABOUT,
    Trust,
    change,
    facts_of,
    forget,
    get,
    note,
    trust_of,
    trust_of_many,
)
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

NOW = datetime(2026, 11, 16, 10, 0, tzinfo=UTC)
START = 70


async def test_a_fact_is_kept_with_who_and_how(session, add_person):
    sam = await add_person("Sam", "adult")
    alex = await add_person("Alex", "owner")
    dana = await add_person("Dana", "adult")
    memory = await keep(
        session,
        said_by=sam.id,
        about_id=alex.id,
        subject="Alex",
        content="does not like pine nuts",
        kind="preference",
        origin="others_words",
        kept_private=False,
        core=False,
        now=NOW,
    )
    fact = await note(
        session,
        person_id=sam.id,
        outcome="mistaken",
        established="confirmed",
        about="said Alex does not like pine nuts; Alex loves them",
        now=NOW,
        memory_id=memory.id,
        noted_for_id=alex.id,
    )
    assert (fact.person_id, fact.outcome, fact.established) == (sam.id, "mistaken", "confirmed")
    assert (fact.memory_id, fact.noted_for_id, fact.reported_by_id) == (memory.id, alex.id, None)
    assert fact.created_at == NOW
    assert await get(session, fact.id) is fact

    reported = await note(
        session,
        person_id=sam.id,
        outcome="lied",
        established="reported",
        about="told Paul the car was fixed",
        now=NOW,
        noted_for_id=dana.id,
        reported_by_id=dana.id,
    )
    assert reported.reported_by_id == dana.id
    # Whoever reported it is kept with a reported fact, and only with one.
    with pytest.raises(ValueError):
        await note(
            session, person_id=sam.id, outcome="lied", established="reported", about="x", now=NOW
        )
    with pytest.raises(ValueError):
        await note(
            session,
            person_id=sam.id,
            outcome="lied",
            established="seen",
            about="x",
            now=NOW,
            reported_by_id=dana.id,
        )
    for about in ("", "   ", "x" * (MAX_ABOUT + 1)):
        with pytest.raises(ValueError):
            await note(
                session,
                person_id=sam.id,
                outcome="held",
                established="seen",
                about=about,
                now=NOW,
            )
    assert len(await facts_of(session, sam.id)) == 2


async def test_the_table_refuses_what_the_service_would(session, add_person):
    sam = await add_person("Sam", "adult")
    for values in (
        {"outcome": "lying", "established": "seen", "about": "x"},
        {"outcome": "lied", "established": "heard", "about": "x"},
        {"outcome": "lied", "established": "seen", "about": ""},
        {"outcome": "lied", "established": "seen", "about": "x" * 201},
        {"outcome": "lied", "established": "seen", "about": "x", "reported_by_id": sam.id},
    ):
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                session.add(TrustFact(person_id=sam.id, created_at=NOW, **values))
    assert await session.scalar(select(func.count()).select_from(TrustFact)) == 0


async def test_a_secret_shaped_text_is_refused(session, add_person):
    sam = await add_person("Sam", "adult")
    for about in (
        "gave the key abcdef0123456789abcdef0123",
        "the link https://example.com/?token=abc",
    ):
        with pytest.raises(SecretRefused):
            await note(
                session, person_id=sam.id, outcome="lied", established="seen", about=about, now=NOW
            )
    assert await session.scalar(select(func.count()).select_from(TrustFact)) == 0


async def test_facts_follow_a_deleted_person(session, add_person):
    sam = await add_person("Sam", "adult")
    dana = await add_person("Dana", "adult")
    alex = await add_person("Alex", "owner")
    memory = await keep(
        session,
        said_by=sam.id,
        subject="Dana",
        content="is away on Monday",
        kind="fact",
        origin="own_words",
        kept_private=False,
        core=False,
        now=NOW,
    )
    of_sam = await note(
        session,
        person_id=sam.id,
        outcome="held",
        established="seen",
        about="Dana was away on Monday",
        now=NOW,
        memory_id=memory.id,
        noted_for_id=alex.id,
    )
    of_dana = await note(
        session,
        person_id=dana.id,
        outcome="lied",
        established="reported",
        about="told Alex the bill was paid",
        now=NOW,
        noted_for_id=sam.id,
        reported_by_id=sam.id,
    )
    of_dana_id, of_sam_id, memory_id = of_dana.id, of_sam.id, memory.id

    await session.execute(delete(Person).where(Person.id == sam.id))
    session.expunge_all()
    # Sam's facts go with Sam; what Sam reported about Dana stays, and no longer names Sam.
    assert await get(session, of_sam_id) is None
    left = await get(session, of_dana_id)
    assert left is not None
    assert (left.reported_by_id, left.noted_for_id, left.established) == (None, None, "reported")
    assert await trust_of(session, sam.id, start=START) == Trust(percent=START, facts=0)

    # A memory forgotten leaves the fact, without it.
    fact = await note(
        session,
        person_id=dana.id,
        outcome="held",
        established="seen",
        about="the bill was paid after all",
        now=NOW,
        memory_id=memory_id,
    )
    await forget_memories(session, [memory_id])
    session.expunge_all()
    kept = await get(session, fact.id)
    assert kept is not None and kept.memory_id is None


async def test_trust_of_many_gives_the_start_to_who_has_no_fact(session, add_person):
    sam = await add_person("Sam", "adult")
    dana = await add_person("Dana", "adult")
    alex = await add_person("Alex", "owner")
    for _ in range(3):
        await note(
            session, person_id=sam.id, outcome="lied", established="seen", about="a lie", now=NOW
        )
    await note(
        session,
        person_id=dana.id,
        outcome="held",
        established="confirmed",
        about="the truth",
        now=NOW,
    )
    many = await trust_of_many(session, [sam.id, dana.id, alex.id], start=START)
    assert many == {
        sam.id: Trust(percent=score([("lied", "seen")] * 3, start=START), facts=3),
        dana.id: Trust(percent=score([("held", "confirmed")], start=START), facts=1),
        alex.id: Trust(percent=START, facts=0),
    }
    assert many[sam.id].percent < 50
    assert await trust_of(session, sam.id, start=START) == many[sam.id]
    assert await trust_of(session, alex.id, start=55) == Trust(percent=55, facts=0)
    assert await trust_of_many(session, [], start=START) == {}


async def test_facts_of_gives_the_ten_newest(session, add_person):
    sam = await add_person("Sam", "adult")
    dana = await add_person("Dana", "adult")
    for hour in range(12):
        await note(
            session,
            person_id=sam.id,
            outcome="held",
            established="seen",
            about=f"fact {hour}",
            now=NOW + timedelta(hours=hour),
        )
    await note(
        session, person_id=dana.id, outcome="lied", established="seen", about="other", now=NOW
    )
    newest = await facts_of(session, sam.id)
    assert [fact.about for fact in newest] == [f"fact {hour}" for hour in range(11, 1, -1)]
    assert [fact.about for fact in await facts_of(session, sam.id, limit=2)] == [
        "fact 11",
        "fact 10",
    ]
    # The score rests on all of them, not on the ten shown.
    assert (await trust_of(session, sam.id, start=START)).facts == 12


async def test_a_fact_can_be_changed_and_forgotten(session, add_person):
    sam = await add_person("Sam", "adult")
    fact = await note(
        session,
        person_id=sam.id,
        outcome="lied",
        established="confirmed",
        about="said the dog was fed",
        now=NOW,
    )
    before = await trust_of(session, sam.id, start=START)
    changed = await change(session, fact, outcome="mistaken")
    assert changed is fact and fact.outcome == "mistaken"
    assert (fact.established, fact.about) == ("confirmed", "said the dog was fed")
    assert fact.created_at == NOW
    after = await trust_of(session, sam.id, start=START)
    assert after.facts == 1 and after.percent > before.percent

    assert await forget(session, fact.id) is True
    assert await forget(session, fact.id) is False
    session.expunge_all()
    assert await get(session, fact.id) is None
    assert await trust_of(session, sam.id, start=START) == Trust(percent=START, facts=0)


async def _reported_lies(session, about: Person, reporters: list[Person], *, each: int):
    """``each`` reported lies about ``about`` from each of ``reporters``, each a minute after the
    last."""
    minute = 0
    for reporter in reporters:
        for _ in range(each):
            minute += 1
            await note(
                session,
                person_id=about.id,
                outcome="lied",
                established="reported",
                about="reported",
                now=NOW + timedelta(minutes=minute),
                reported_by_id=reporter.id,
            )


async def test_one_reporter_counts_three_times_at_most(session, add_person):
    sam = await add_person("Sam", "adult")
    dana = await add_person("Dana", "adult")
    await _reported_lies(session, sam, [dana], each=10)
    many = await trust_of(session, sam.id, start=START)
    assert many == Trust(percent=score([("lied", "reported")] * 3, start=START), facts=3)
    # And the facts of reporters who are gone count together, as one reporter.
    paul = await add_person("Paul", "adult")
    gone = [await add_person(f"Gone {i}", "adult") for i in range(2)]
    await _reported_lies(session, paul, gone, each=2)
    assert (await trust_of(session, paul.id, start=START)).facts == 4
    await session.execute(delete(Person).where(Person.id.in_([g.id for g in gone])))
    assert (await trust_of(session, paul.id, start=START)).facts == 3


async def test_ten_reports_from_five_people_weigh_more_than_from_one(session, add_person):
    sam = await add_person("Sam", "adult")
    alex = await add_person("Alex", "owner")
    reporters = [await add_person(f"R{i}", "adult") for i in range(5)]
    await _reported_lies(session, sam, [reporters[0]], each=10)
    await _reported_lies(session, alex, reporters, each=2)
    one = await trust_of(session, sam.id, start=START)
    five = await trust_of(session, alex.id, start=START)
    assert five.facts == 10 and one.facts == 3
    assert five.percent < one.percent


async def test_the_newest_reports_of_a_reporter_are_the_ones_that_count(session, add_person):
    sam = await add_person("Sam", "adult")
    dana = await add_person("Dana", "adult")
    for minute, outcome in enumerate(["lied"] * 5 + ["held"] * 3):
        await note(
            session,
            person_id=sam.id,
            outcome=outcome,  # type: ignore[arg-type]
            established="reported",
            about="reported",
            now=NOW + timedelta(minutes=minute),
            reported_by_id=dana.id,
        )
    # Established facts are never capped.
    for minute in range(5):
        await note(
            session,
            person_id=sam.id,
            outcome="held",
            established="seen",
            about="seen",
            now=NOW + timedelta(minutes=minute),
        )
    expected = score([("held", "reported")] * 3 + [("held", "seen")] * 5, start=START)
    assert await trust_of(session, sam.id, start=START) == Trust(percent=expected, facts=8)
