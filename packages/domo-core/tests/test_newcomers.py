"""Who began using a key of someone: told to its giver once, with what they spent this month."""

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal

from domo_core.accounts.models import Person
from domo_core.llm.catalog import shipped_catalog
from domo_core.llm.models import Provider
from domo_core.llm.newcomers import (
    MAX_NEWCOMERS_TOLD,
    NEWCOMER_TASKS,
    Newcomer,
    newcomers_told,
)
from domo_core.llm.spending_models import ExchangeRate, KeyNewcomer, ModelCall
from sqlalchemy import delete, func, select, text
from test_spending import add_key


async def add_person(
    session_factory, name: str, role: str, *, invited_by: Person | None = None
) -> Person:
    async with session_factory() as writer:
        person = Person(
            display_name=name, role=role, invited_by=None if invited_by is None else invited_by.id
        )
        writer.add(person)
        await writer.commit()
        return person


async def add_call(
    session_factory,
    key_id: str,
    person: Person,
    at: datetime,
    *,
    task: str = "conversation",
    usd: str = "0.1",
) -> None:
    async with session_factory() as writer:
        writer.add(
            ModelCall(
                provider_id=key_id,
                person_id=person.id,
                task=task,
                model_id="example-sonnet-1",
                status="settled",
                reserved_usd=Decimal(usd),
                cost_usd=Decimal(usd),
                started_at=at,
                settled_at=at,
            )
        )
        await writer.commit()


async def told(session_factory, giver: Person, now: datetime) -> list[Newcomer]:
    async with session_factory() as session:
        found = await newcomers_told(
            session, giver_id=giver.id, now=now, zone=None, catalog=shipped_catalog()
        )
        await session.commit()
    return found


async def test_a_newcomer_is_told_once_to_the_giver(session_factory, clock):
    owner = await add_person(session_factory, "Owner", "owner")
    robin = await add_person(session_factory, "Robin", "guest", invited_by=owner)
    key = await add_key(session_factory, owner.id, general=True)
    await add_call(session_factory, key.id, robin, clock(), usd="0.4")

    first = await told(session_factory, owner, clock())

    assert [(n.key_label, n.person_name, n.role, n.host_name, n.spent_usd) for n in first] == [
        (
            "openai_compatible key of Owner, general",
            "Robin",
            "guest",
            "Owner",
            Decimal("0.40"),
        )
    ]
    assert await told(session_factory, owner, clock()) == []


async def test_only_a_conversation_makes_a_newcomer(session_factory, clock):
    owner = await add_person(session_factory, "Owner", "owner")
    robin = await add_person(session_factory, "Robin", "guest", invited_by=owner)
    key = await add_key(session_factory, owner.id, general=True)
    # The upkeep, a check of the key, and the yes-judge an upkeep turn asks: none of them.
    for task in ("upkeep", "key_check", "judge"):
        await add_call(session_factory, key.id, robin, clock(), task=task)

    assert NEWCOMER_TASKS == ("conversation",)
    assert await told(session_factory, owner, clock()) == []
    # Then a request of theirs: now they are.
    await add_call(session_factory, key.id, robin, clock(), task="conversation")
    assert [n.person_name for n in await told(session_factory, owner, clock())] == ["Robin"]


async def until_someone_waits_for_a_row(session_factory) -> None:
    """Wait until a connection of the test database waits for a lock (a row that another
    transaction holds)."""
    async with asyncio.timeout(10):
        while True:
            async with session_factory() as watcher:
                waiting = (
                    await watcher.execute(
                        text(
                            "SELECT count(*) FROM pg_stat_activity "
                            "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                        )
                    )
                ).scalar_one()
            if waiting:
                return
            await asyncio.sleep(0.02)


async def test_two_turns_at_once_tell_a_newcomer_once_and_without_an_error(session_factory, clock):
    owner = await add_person(session_factory, "Owner", "owner")
    robin = await add_person(session_factory, "Robin", "guest", invited_by=owner)
    key = await add_key(session_factory, owner.id, general=True)
    await add_call(session_factory, key.id, robin, clock())

    async with session_factory() as first:
        found = await newcomers_told(
            first, giver_id=owner.id, now=clock(), zone=None, catalog=shipped_catalog()
        )
        assert [n.person_name for n in found] == ["Robin"]
        # The other turn looks before the first commits: it finds Robin too, and waits on the row
        # the first wrote.
        later = asyncio.create_task(told(session_factory, owner, clock()))
        await until_someone_waits_for_a_row(session_factory)
        assert not later.done()
        await first.commit()

    # The row the first wrote stands: the other leaves Robin out, and does not fail.
    assert await later == []
    async with session_factory() as reader:
        rows = (await reader.execute(select(KeyNewcomer.person_id))).scalars().all()
    assert rows == [robin.id]


async def test_the_giver_is_never_a_newcomer_on_their_own_key(session_factory, clock):
    owner = await add_person(session_factory, "Owner", "owner")
    key = await add_key(session_factory, owner.id, general=True)
    await add_call(session_factory, key.id, owner, clock())

    assert await told(session_factory, owner, clock()) == []


async def test_newcomers_are_told_per_key(session_factory, clock):
    owner = await add_person(session_factory, "Owner", "owner")
    alex = await add_person(session_factory, "Alex", "adult", invited_by=owner)
    robin = await add_person(session_factory, "Robin", "guest", invited_by=alex)
    general = await add_key(session_factory, owner.id, general=True)
    alexs = await add_key(session_factory, alex.id, kind="openai", base_url="https://o.example/v1")
    await add_call(session_factory, general.id, robin, clock())
    await add_call(session_factory, alexs.id, robin, clock())

    for_alex = await told(session_factory, alex, clock())
    for_owner = await told(session_factory, owner, clock())

    assert [(n.key_label, n.person_name, n.host_name) for n in for_alex] == [
        ("openai key of Alex", "Robin", "Alex")
    ]
    assert [(n.key_label, n.person_name) for n in for_owner] == [
        ("openai_compatible key of Owner, general", "Robin")
    ]
    assert await told(session_factory, alex, clock()) == []
    assert await told(session_factory, owner, clock()) == []


async def test_at_most_ten_newcomers_a_turn_the_oldest_first(session_factory, clock):
    owner = await add_person(session_factory, "Owner", "owner")
    key = await add_key(session_factory, owner.id, general=True)
    assert MAX_NEWCOMERS_TOLD == 10
    start = clock()
    for index in range(12):
        # The later the person, the older the first call: the order is the calls', not the rows'.
        person = await add_person(session_factory, f"Guest {index:02}", "guest", invited_by=owner)
        await add_call(session_factory, key.id, person, start - timedelta(minutes=index))

    first = await told(session_factory, owner, start)
    second = await told(session_factory, owner, start)

    assert [n.person_name for n in first] == [f"Guest {i:02}" for i in range(11, 1, -1)]
    assert [n.person_name for n in second] == ["Guest 01", "Guest 00"]
    assert await told(session_factory, owner, start) == []


async def test_a_newcomer_without_a_host_or_whose_host_left_is_still_told(session_factory, clock):
    owner = await add_person(session_factory, "Owner", "owner")
    dana = await add_person(session_factory, "Dana", "adult", invited_by=owner)
    alone = await add_person(session_factory, "Paul", "adult")
    robin = await add_person(session_factory, "Robin", "guest", invited_by=dana)
    key = await add_key(session_factory, owner.id, general=True)
    async with session_factory() as writer:
        found = await writer.get(Person, dana.id)
        assert found is not None
        found.disabled_at = clock()
        await writer.commit()
    await add_call(session_factory, key.id, alone, clock() - timedelta(minutes=2))
    await add_call(session_factory, key.id, robin, clock() - timedelta(minutes=1))

    found_ones = await told(session_factory, owner, clock())

    assert [(n.person_name, n.host_name) for n in found_ones] == [("Paul", None), ("Robin", "Dana")]


async def test_a_removed_key_takes_its_newcomers_with_it(session_factory, clock):
    owner = await add_person(session_factory, "Owner", "owner")
    robin = await add_person(session_factory, "Robin", "guest", invited_by=owner)
    key = await add_key(session_factory, owner.id, general=True)
    await add_call(session_factory, key.id, robin, clock())
    assert len(await told(session_factory, owner, clock())) == 1

    async with session_factory() as writer:
        await writer.execute(delete(Provider).where(Provider.id == key.id))
        await writer.commit()
    async with session_factory() as reader:
        count = (await reader.execute(select(func.count()).select_from(KeyNewcomer))).scalar_one()
    assert count == 0
    assert await told(session_factory, owner, clock()) == []


async def test_a_newcomers_spending_is_this_months_on_that_key(session_factory, clock):
    owner = await add_person(session_factory, "Owner", "owner")
    robin = await add_person(session_factory, "Robin", "guest", invited_by=owner)
    key = await add_key(session_factory, owner.id, general=True)
    other = await add_key(session_factory, owner.id, kind="openai", base_url="https://o.example/v1")
    now = clock()
    async with session_factory() as writer:
        writer.add(ExchangeRate(currency="EUR", usd_per_unit=Decimal("1.25")))
        await writer.commit()
    # A call of last month makes the newcomer but does not count, nor does one on another key.
    await add_call(session_factory, key.id, robin, now - timedelta(days=40), usd="5")
    await add_call(session_factory, other.id, robin, now, usd="7")
    await add_call(session_factory, key.id, robin, now, usd="0.123")
    await add_call(session_factory, key.id, robin, now, usd="0.1225")

    both = await told(session_factory, owner, now)

    (found,) = [n for n in both if n.key_label.endswith("general")]
    (elsewhere,) = [n for n in both if not n.key_label.endswith("general")]
    assert elsewhere.spent_usd == Decimal("7.00")
    # 0.2455 USD, rounded half up to the cent; in euros at 1.25 USD per EUR: 0.1964 -> 0.20.
    assert found.spent_usd == Decimal("0.25")
    assert found.spent_eur == Decimal("0.20")
