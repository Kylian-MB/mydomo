"""The account of Domo and the firm limits: every call is counted, its maximum is reserved before
it is made, a firm limit is never passed (two calls at once included), the person who set a
limit is told where it stands (and once, at a threshold), and a key at its limit hands the next
request over to the next key the person may use."""

import asyncio
import html
import uuid
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from domo_common.i18n import t
from domo_core.accounts.actors import actor_of
from domo_core.accounts.models import Person
from domo_core.confirmations.judge import RightsSay
from domo_core.conversation.execution import consult_the_judge
from domo_core.llm.catalog import UNKNOWN_PRICES, Catalog, ModelInfo, shipped_catalog
from domo_core.llm.costs import CHARS_PER_TOKEN, speech_cost_usd
from domo_core.llm.metered import MeteredModel
from domo_core.llm.models import ModelChoices, Provider, ProviderModel, new_provider_id
from domo_core.llm.picker import ModelPicker
from domo_core.llm.provider_keys import check_key
from domo_core.llm.router import Route, choose_route
from domo_core.llm.speech.catalog import FREE_SPEECH, SpeechPrices
from domo_core.llm.spending import (
    RESERVATION_TTL,
    LimitReached,
    _measured,
    at_limit,
    limit_notes,
    period_start,
    reserve,
    set_spending_limit,
    settle_speech,
    settle_stale,
    spent_usd,
    usd_per,
)
from domo_core.llm.spending_models import ExchangeRate, ModelCall, SpendingLimit
from domo_core.llm.types import ChatMessage, ModelCallError
from domo_core.policy.actions import DescribedAction
from domo_core.policy.catalog import DEFAULT_POLICY
from domo_core.tools.context import ToolContext
from domo_core.tools.registry import TOOLS
from fakes import FakeChatModel, FakeModelLister, fake_factory
from llm_support import run
from sqlalchemy import delete, select

PILOT_MODELS = ("example-haiku-1", "example-sonnet-1", "example-opus-1")
CONTEXT_MARK = "\n\nContext\n"


async def add_key(
    session_factory,
    giver: uuid.UUID,
    *,
    general: bool = False,
    models: Sequence[str] = PILOT_MODELS,
    kind: str = "openai_compatible",
    base_url: str = "https://llm.example.com/v1",
) -> Provider:
    """A key of ``giver`` with a key in the vault, listing ``models`` (committed)."""
    provider_id = new_provider_id()
    row = Provider(
        id=provider_id,
        kind=kind,
        base_url=base_url,
        secret_ref=f"provider:{provider_id}:api_key",
        given_by=giver,
        is_general=general,
    )
    async with session_factory() as writer:
        writer.add(row)
        await writer.flush()
        writer.add_all(ProviderModel(provider_id=provider_id, model_id=m) for m in models)
        await writer.commit()
    return row


async def add_limit(
    session_factory,
    provider_id: str,
    *,
    amount: str,
    set_by: uuid.UUID,
    currency: str = "USD",
    period: str = "month",
    person_id: uuid.UUID | None = None,
    warn_at: int | None = None,
    role: str | None = None,
    split: str | None = None,
) -> uuid.UUID:
    """A firm limit on the key ``provider_id`` (for one person, a role, or the whole key),
    committed."""
    async with session_factory() as writer:
        limit = SpendingLimit(
            provider_id=provider_id,
            person_id=person_id,
            role=role,
            split=split,
            amount=Decimal(amount),
            currency=currency,
            period=period,
            warn_at_percent=warn_at,
            set_by=set_by,
        )
        writer.add(limit)
        await writer.commit()
        return limit.id


async def add_spent(
    session_factory, provider_id: str, usd: str, *, person_id: uuid.UUID, at: datetime
) -> None:
    """A call of ``person_id`` on the key, settled at ``usd``, made at ``at`` (committed)."""
    async with session_factory() as writer:
        writer.add(
            ModelCall(
                provider_id=provider_id,
                person_id=person_id,
                task="conversation",
                model_id="example-sonnet-1",
                status="settled",
                reserved_usd=Decimal(usd),
                cost_usd=Decimal(usd),
                started_at=at,
                settled_at=at,
            )
        )
        await writer.commit()


async def calls_of(session_factory, provider_id: str) -> list[ModelCall]:
    """The calls counted on the key, oldest first, but those that wrote someone's texts in the
    background (``writing``, ``checking``): a page shown has them made at any time."""
    async with session_factory() as reader:
        rows = await reader.execute(
            select(ModelCall)
            .where(
                ModelCall.provider_id == provider_id,
                ModelCall.task.not_in(("writing", "checking")),
            )
            .order_by(ModelCall.started_at, ModelCall.id)
        )
        return list(rows.scalars().all())


def route_on(key: Provider, *, model_id: str = "example-sonnet-1") -> Route:
    """The conversation's route on ``key`` at the prices of a model the catalog does not know."""
    return Route(
        provider=key,
        model_id=model_id,
        level="medium",
        prices=UNKNOWN_PRICES,
        task="conversation",
        price_known=False,
    )


def metered(inner, key: Provider, person: Person, session_factory, clock, **options):
    return MeteredModel(
        inner,
        route=options.pop("route", None) or route_on(key),
        person_id=person.id,
        session_factory=session_factory,
        clock=clock,
        catalog=options.pop("catalog", shipped_catalog()),
    )


def context_lines(system: str) -> list[str]:
    return system.split(CONTEXT_MARK)[1].splitlines()


def error_text(reply) -> str:
    (error,) = [event.data for event in reply.events if event.event == "error"]
    return html.unescape(error)


@pytest.fixture
async def owner(add_member):
    return await add_member("Owner", "owner")


@pytest.fixture
async def alex(add_member):
    return await add_member("Alex", "adult")


# --- a firm limit is never passed ----------------------------------------------------------------


async def test_a_call_that_would_pass_a_firm_limit_is_not_made(owner_chat, session_factory):
    server = owner_chat.server
    await add_limit(session_factory, server.provider_id, amount="0.01", set_by=owner_chat.person.id)
    model = owner_chat.use_model(FakeChatModel(["never said"]))

    reply = await owner_chat.say("hello")

    # The most the call could cost does not fit under the limit: it is not made at all.
    assert model.calls == [] and server.calls == []
    assert reply.status == "error"
    assert error_text(reply) == t("chat.error.limit_reached", "en")
    # Nothing was reserved, so nothing counts against the limit.
    assert await calls_of(session_factory, server.provider_id) == []


async def test_two_calls_at_once_never_pass_a_firm_limit(owner, session_factory, clock):
    key = await add_key(session_factory, owner.id, general=True)
    limit_id = await add_limit(session_factory, key.id, amount="1", set_by=owner.id)

    async def one_call() -> uuid.UUID | LimitReached:
        async with session_factory() as session:
            reserved = await reserve(
                session,
                route=route_on(key),
                person_id=owner.id,
                max_usd=Decimal("0.6"),
                now=clock(),
                catalog=shipped_catalog(),
                zone=None,
            )
            # Both have looked before either commits, unless the first holds the second back.
            await asyncio.sleep(0.3)
            await session.commit()
            return reserved

    results = await asyncio.gather(one_call(), one_call())

    assert [isinstance(result, uuid.UUID) for result in results].count(True) == 1
    (refused,) = [result for result in results if isinstance(result, LimitReached)]
    assert refused == LimitReached(limit_id)
    (row,) = await calls_of(session_factory, key.id)
    assert (row.status, row.reserved_usd, row.cost_usd) == ("reserved", Decimal("0.6"), None)


async def test_a_call_without_usage_is_counted_from_its_length(owner, session_factory, clock):
    key = await add_key(session_factory, owner.id, general=True)
    model = metered(FakeChatModel(["abcdef"], usage=None), key, owner, session_factory, clock)

    await run(model, system="S" * 30, messages=[ChatMessage("user", "x" * 30)])

    (row,) = await calls_of(session_factory, key.id)
    input_tokens, output_tokens = 60 // CHARS_PER_TOKEN, 6 // CHARS_PER_TOKEN
    assert (row.status, row.estimated) == ("settled", True)
    assert (row.input_tokens, row.output_tokens) == (input_tokens, output_tokens)
    cost = (UNKNOWN_PRICES.input * input_tokens + UNKNOWN_PRICES.output * output_tokens) / 10**6
    assert row.cost_usd == cost
    assert (model.cost_usd, model.estimated) == (cost, True)


async def test_a_euro_limit_without_a_fresh_rate_uses_the_shipped_one(
    owner, session_factory, clock
):
    key = await add_key(session_factory, owner.id, general=True)
    catalog = replace(shipped_catalog(), usd_per_eur=Decimal("1.2"))
    await add_limit(session_factory, key.id, amount="1", currency="EUR", set_by=owner.id)
    await add_spent(session_factory, key.id, "1.10", person_id=owner.id, at=clock())

    async def a_call(max_usd: str) -> uuid.UUID | LimitReached:
        async with session_factory() as session:
            reserved = await reserve(
                session,
                route=route_on(key),
                person_id=owner.id,
                max_usd=Decimal(max_usd),
                now=clock(),
                catalog=catalog,
                zone=None,
            )
            await session.commit()
            return reserved

    async with session_factory() as session:
        assert await usd_per(session, "USD", catalog) == 1
        assert await usd_per(session, "EUR", catalog) == Decimal("1.2")
    # 1.10 + 0.05 dollars is more than a dollar, but less than a euro at the shipped rate.
    assert isinstance(await a_call("0.05"), uuid.UUID)

    # A rate read since: each check takes the latest, so that the limit keeps its sense in euros.
    async with session_factory() as writer:
        writer.add(ExchangeRate(currency="EUR", usd_per_unit=Decimal("1.0")))
        await writer.commit()
    async with session_factory() as session:
        assert await usd_per(session, "EUR", catalog) == Decimal("1.0")
    assert isinstance(await a_call("0.01"), LimitReached)


async def test_a_limit_for_one_person_counts_only_their_calls(owner, alex, session_factory, clock):
    key = await add_key(session_factory, owner.id, general=True)
    await add_limit(session_factory, key.id, amount="1", person_id=alex.id, set_by=owner.id)
    await add_spent(session_factory, key.id, "0.9", person_id=owner.id, at=clock())
    await add_spent(session_factory, key.id, "0.5", person_id=alex.id, at=clock())
    catalog = shipped_catalog()
    since = period_start(clock(), None, "month")

    async with session_factory() as session:
        assert await spent_usd(session, provider_id=key.id, person_id=alex.id, since=since) == (
            Decimal("0.5")
        )
        assert await spent_usd(session, provider_id=key.id, person_id=None, since=since) == (
            Decimal("1.4")
        )
        for person, fits in ((alex, False), (owner, True)):
            reserved = await reserve(
                session,
                route=route_on(key),
                person_id=person.id,
                max_usd=Decimal("0.6"),
                now=clock(),
                catalog=catalog,
                zone=None,
            )
            assert isinstance(reserved, uuid.UUID) is fits, person.display_name
        await session.commit()

    async with session_factory() as session:
        options = {"now": clock(), "catalog": catalog, "zone": None}
        # Alex has room for a call of 0.4 dollars, not for one of 0.6: as ``reserve`` compares.
        for person, need, full in (
            (alex, "0.4", False),
            (alex, "0.6", True),
            (owner, "0.6", False),
        ):
            assert (
                await at_limit(
                    session, provider=key, person_id=person.id, need=Decimal(need), **options
                )
                is full
            ), (person.display_name, need)


def test_the_period_starts_at_local_midnight_monday_and_the_first():
    paris = ZoneInfo("Europe/Paris")
    # Wednesday 9 January 2030, 23:30 in UTC: already Thursday the 10th in Paris.
    now = datetime(2030, 1, 9, 23, 30, tzinfo=UTC)
    assert period_start(now, paris, "day") == datetime(2030, 1, 10, tzinfo=paris)
    assert period_start(now, paris, "week") == datetime(2030, 1, 7, tzinfo=paris)
    assert period_start(now, paris, "month") == datetime(2030, 1, 1, tzinfo=paris)
    # Without a time zone, the days of UTC.
    assert period_start(now, None, "day") == datetime(2030, 1, 9, tzinfo=UTC)
    assert period_start(now, None, "week") == datetime(2030, 1, 7, tzinfo=UTC)
    assert period_start(now, None, "month") == datetime(2030, 1, 1, tzinfo=UTC)
    # The last evening of January in UTC is the first of February in Paris.
    end_of_january = datetime(2030, 1, 31, 23, 30, tzinfo=UTC)
    assert period_start(end_of_january, paris, "month") == datetime(2030, 2, 1, tzinfo=paris)
    assert period_start(end_of_january, None, "month") == datetime(2030, 1, 1, tzinfo=UTC)
    # In summer time too: midnight in Paris is 22:00 in UTC.
    summer = datetime(2030, 7, 1, 22, 30, tzinfo=UTC)
    assert period_start(summer, paris, "day") == datetime(2030, 7, 1, 22, 0, tzinfo=UTC)


async def test_a_stale_reservation_is_settled_at_its_maximum(owner, session_factory, clock):
    key = await add_key(session_factory, owner.id, general=True)

    async def reserved(max_usd: str) -> uuid.UUID:
        async with session_factory() as session:
            call_id = await reserve(
                session,
                route=route_on(key),
                person_id=owner.id,
                max_usd=Decimal(max_usd),
                now=clock(),
                catalog=shipped_catalog(),
                zone=None,
            )
            await session.commit()
            assert isinstance(call_id, uuid.UUID)
            return call_id

    stale = await reserved("0.25")
    clock.advance(timedelta(minutes=5))
    fresh = await reserved("0.10")
    clock.advance(RESERVATION_TTL - timedelta(minutes=4))

    async with session_factory() as session:
        assert await settle_stale(session, clock()) == 1
        await session.commit()

    rows = {row.id: row for row in await calls_of(session_factory, key.id)}
    assert (rows[stale].status, rows[stale].cost_usd, rows[stale].estimated) == (
        "settled",
        Decimal("0.25"),
        True,
    )
    assert (rows[fresh].status, rows[fresh].cost_usd) == ("reserved", None)


async def test_the_judge_on_a_key_at_its_limit_is_not_called_and_asks_nothing(
    owner, session_factory, clock
):
    key = await add_key(session_factory, owner.id, general=True)
    await add_limit(session_factory, key.id, amount="1", set_by=owner.id)
    await add_spent(session_factory, key.id, "1", person_id=owner.id, at=clock())
    model = FakeChatModel(["ok"], judge="do")
    picker = ModelPicker(
        session_factory=session_factory,
        model_factory=fake_factory(model),
        provider=key,
        person_id=owner.id,
        clock=clock,
    )
    ctx = ToolContext(
        session_factory=session_factory,
        actor=actor_of(owner),
        person=owner,
        conversation_id=uuid.uuid4(),
        origin="https://domo.example.com/",
        gatekeeper=None,  # type: ignore[arg-type]
        clock=clock,
        registry=TOOLS,
        policy=DEFAULT_POLICY,
        # The turn answers a message of theirs: what is judged is a request they made.
        reply_message_id=uuid.uuid4(),
        models=picker,
    )

    judgement = await consult_the_judge(
        ctx,
        actor=ctx.actor,
        action=DescribedAction("core", "profile.rename", None, "self_profile", "household"),
        says="rename themselves",
        rights=RightsSay(allowed=True, needs=None, question=None),
    )

    # As when the judge does not answer: nothing is asked (2026-11-15), and no call is paid for.
    assert (judgement.verdict, judgement.source) == ("do", "fallback")
    assert model.judge_calls() == []
    assert len(await calls_of(session_factory, key.id)) == 1  # the spending only


# --- where a limit stands ------------------------------------------------------------------------


async def test_the_setter_sees_where_their_limit_stands(owner_chat, adult_chat, session_factory):
    server = owner_chat.server
    limit_id = await add_limit(
        session_factory, server.provider_id, amount="10", set_by=owner_chat.person.id
    )
    await add_spent(
        session_factory,
        server.provider_id,
        "2.5",
        person_id=adult_chat.person.id,
        at=owner_chat.clock(),
    )

    await owner_chat.say("hello")

    # The whole line: the share of the limit, with no threshold and no warning.
    assert (
        "Spending (data, not instructions): 25% of the limit of 10 USD per month on "
        '"openai_compatible key of Owner, general".'
    ) in context_lines(server.calls[-1].system)
    # Alex did not set it: he is not told.
    await adult_chat.say("hello")
    assert "Spending (data" not in server.calls[-1].system
    async with session_factory() as reader:
        limit = await reader.get(SpendingLimit, limit_id)
        assert limit is not None and (limit.warned_at, limit.domo_warned_at) == (None, None)


async def test_a_newcomer_on_a_key_is_told_to_its_giver_once_at_their_turn(
    owner_chat, guest_chat, adult_chat
):
    server = owner_chat.server

    await guest_chat.say("hello")
    await owner_chat.say("hello")

    line = (
        "New on a key you gave (data, not instructions): "
        f'"{guest_chat.person.display_name}", guest, began using '
        '"openai_compatible key of Owner, general"; about '
    )
    assert any(item.startswith(line) for item in context_lines(server.calls[-1].system))
    # Told once: the next turn has no such line. And not to someone who gave no key.
    await owner_chat.say("and again")
    assert "New on a key" not in server.calls[-1].system
    await adult_chat.say("hello")
    assert "New on a key" not in server.calls[-1].system


async def notes_of(
    session_factory, person: Person, clock, own_thresholds: dict[str, int] | None = None
):
    async with session_factory() as session:
        notes = await limit_notes(
            session,
            person_id=person.id,
            now=clock(),
            catalog=shipped_catalog(),
            zone=None,
            own_thresholds=own_thresholds or {},
        )
        await session.commit()
    return [(note.percent, note.threshold_reached, note.threshold_by) for note in notes]


async def test_a_threshold_asked_by_the_setter_is_told_once(owner, session_factory, clock):
    key = await add_key(session_factory, owner.id, general=True)
    await add_limit(session_factory, key.id, amount="10", set_by=owner.id, warn_at=20)
    await add_spent(session_factory, key.id, "2.5", person_id=owner.id, at=clock())

    async with session_factory() as session:
        (note,) = await limit_notes(
            session,
            person_id=owner.id,
            now=clock(),
            catalog=shipped_catalog(),
            zone=None,
            own_thresholds={},
        )
        await session.commit()
    assert (note.percent, note.amount, note.currency, note.period) == (
        25,
        Decimal(10),
        "USD",
        "month",
    )
    assert note.key_label == "openai_compatible key of Owner, general"
    assert (note.threshold_reached, note.threshold_by) == (20, "setter")
    # Told once in the period.
    assert await notes_of(session_factory, owner, clock) == [(25, None, None)]
    assert await notes_of(session_factory, owner, clock) == [(25, None, None)]

    # In the next period, once it is passed again.
    clock.advance(timedelta(days=32))
    assert await notes_of(session_factory, owner, clock) == [(0, None, None)]
    await add_spent(session_factory, key.id, "3", person_id=owner.id, at=clock())
    assert await notes_of(session_factory, owner, clock) == [(30, 20, "setter")]
    assert await notes_of(session_factory, owner, clock) == [(30, None, None)]


async def test_domo_own_threshold_comes_after_the_setters(owner, session_factory, clock):
    key = await add_key(session_factory, owner.id, general=True)
    await add_limit(session_factory, key.id, amount="10", set_by=owner.id, warn_at=80)

    await add_spent(session_factory, key.id, "5.5", person_id=owner.id, at=clock())
    assert await notes_of(session_factory, owner, clock, {key.id: 50}) == [(55, 50, "domo")]
    assert await notes_of(session_factory, owner, clock, {key.id: 50}) == [(55, None, None)]
    await add_spent(session_factory, key.id, "3", person_id=owner.id, at=clock())
    # The setter's, even though Domo's was told already.
    assert await notes_of(session_factory, owner, clock, {key.id: 50}) == [(85, 80, "setter")]
    assert await notes_of(session_factory, owner, clock, {key.id: 50}) == [(85, None, None)]

    # Both passed at once: the setter's first, Domo's the time after.
    clock.advance(timedelta(days=32))
    await add_spent(session_factory, key.id, "9", person_id=owner.id, at=clock())
    assert await notes_of(session_factory, owner, clock, {key.id: 50}) == [(90, 80, "setter")]
    assert await notes_of(session_factory, owner, clock, {key.id: 50}) == [(90, 50, "domo")]
    assert await notes_of(session_factory, owner, clock, {key.id: 50}) == [(90, None, None)]
    # A key Domo gave himself no threshold on is told none.
    clock.advance(timedelta(days=32))
    await add_spent(session_factory, key.id, "6", person_id=owner.id, at=clock())
    assert await notes_of(session_factory, owner, clock, {"p-00000000": 50}) == [(60, None, None)]


async def test_domo_own_threshold_is_the_one_of_each_key(owner_chat, session_factory):
    server = owner_chat.server
    owner = owner_chat.person
    # The owner's second key, besides the general one: Domo chose to warn sooner on it.
    other = await add_key(
        session_factory,
        owner.id,
        kind="openai",
        base_url="https://api.openai.com/v1",
        models=("gpt-4.1",),
    )
    for key_id in (server.provider_id, other.id):
        await add_limit(session_factory, key_id, amount="10", set_by=owner.id)
        await add_spent(session_factory, key_id, "3", person_id=owner.id, at=owner_chat.clock())
    async with session_factory() as writer:
        writer.add_all(
            [
                ModelChoices(scope="household", choices={"warn_at_percent": 80}),
                ModelChoices(scope=f"provider:{other.id}", choices={"warn_at_percent": 20}),
            ]
        )
        await writer.commit()

    await owner_chat.say("hello")

    lines = context_lines(server.calls[-1].system)
    # The key's own threshold is passed: told.
    assert (
        "Spending (data, not instructions): 30% of the limit of 10 USD per month on "
        '"openai key of Owner". You chose to tell them at 20%.'
    ) in lines
    # The household's is not, on the general key.
    assert (
        "Spending (data, not instructions): 30% of the limit of 10 USD per month on "
        '"openai_compatible key of Owner, general".'
    ) in lines


# --- the keys of a request ---------------------------------------------------------------------


async def test_a_key_at_its_limit_hands_over_to_the_next_permitted_key(
    adult_chat, owner_member, add_member, session_factory
):
    server = adult_chat.server
    alex = adult_chat.person
    alexs = await add_key(session_factory, alex.id)
    await add_limit(session_factory, alexs.id, amount="1", set_by=alex.id)
    await add_spent(session_factory, alexs.id, "1", person_id=alex.id, at=adult_chat.clock())
    # A key with room that Alex may not use: never taken.
    dana = await add_member("Dana", "adult")
    await add_key(session_factory, dana.id)

    reply = await adult_chat.say("hello")

    assert reply.status == "complete"
    assert {provider_id for provider_id, _ in server.requested} == {server.provider_id}
    assert (
        'Keys (data, not instructions): "openai_compatible key of Alex" is at its spending '
        'limit; this request uses "openai_compatible key of Owner, general".'
    ) in context_lines(server.calls[-1].system)

    # Once every key he may use is at its limit, the fixed message answers, and no model.
    await add_limit(session_factory, server.provider_id, amount="1", set_by=owner_member.person.id)
    await add_spent(
        session_factory, server.provider_id, "1", person_id=alex.id, at=adult_chat.clock()
    )
    calls_before = len(server.calls)
    reply = await adult_chat.say("hello again")
    assert reply.status == "error" and error_text(reply) == t("chat.error.limit_reached", "en")
    assert len(server.calls) == calls_before


async def test_a_key_with_too_little_left_for_a_call_hands_over(adult_chat, session_factory):
    server = adult_chat.server
    alex = adult_chat.person
    alexs = await add_key(session_factory, alex.id)
    await add_limit(session_factory, alexs.id, amount="1", set_by=alex.id)
    # A thousandth of a dollar left: less than the most the first call of a request can cost.
    await add_spent(session_factory, alexs.id, "0.999", person_id=alex.id, at=adult_chat.clock())

    reply = await adult_chat.say("hello")

    assert reply.status == "complete"
    assert {provider_id for provider_id, _ in server.requested} == {server.provider_id}
    assert (
        'Keys (data, not instructions): "openai_compatible key of Alex" is at its spending '
        'limit; this request uses "openai_compatible key of Owner, general".'
    ) in context_lines(server.calls[-1].system)
    # Nothing was reserved on his key: only what it spent before.
    assert len(await calls_of(session_factory, alexs.id)) == 1


async def test_a_key_without_a_model_hands_over_to_the_next_permitted_key(
    adult_chat, session_factory
):
    server = adult_chat.server
    await add_key(session_factory, adult_chat.person.id, models=())

    reply = await adult_chat.say("hello")

    assert reply.status == "complete"
    assert {provider_id for provider_id, _ in server.requested} == {server.provider_id}
    # Skipped for having no model: nothing to tell of a limit (the keys Alex may use are told).
    assert "spending limit" not in server.calls[-1].system

    # No key left with a model: the turn ends with no model, and no model is asked.
    async with session_factory() as writer:
        await writer.execute(
            delete(ProviderModel).where(ProviderModel.provider_id == server.provider_id)
        )
        await writer.commit()
    calls_before = len(server.calls)
    reply = await adult_chat.say("hello again")
    assert reply.status == "error" and error_text(reply) == t("chat.error.no_model", "en")
    assert len(server.calls) == calls_before


async def test_the_owner_s_two_keys_are_chosen_between(owner_chat, session_factory):
    server = owner_chat.server
    # A key of OpenAI the owner added in words, besides the general one: both are his own.
    openai = await add_key(
        session_factory,
        owner_chat.person.id,
        kind="openai",
        base_url="https://api.openai.com/v1",
        models=("gpt-4.1", "gpt-4.1-mini"),
    )

    await owner_chat.say("hello")
    # The cheapest conversation at the level wanted (medium): gpt-4.1 at 2 + 8 dollars per
    # million tokens, against the unknown prices of the general key.
    assert server.requested[-1] == (openai.id, "gpt-4.1")

    # Domo pinned a model of the conversation that only the general key lists: it is taken.
    async with session_factory() as writer:
        writer.add(
            ModelChoices(
                scope="household",
                choices={"tasks": {"conversation": {"model": "example-opus-1"}}},
            )
        )
        await writer.commit()
    await owner_chat.say("hello again")
    assert server.requested[-1] == (server.provider_id, "example-opus-1")


# --- what is counted -----------------------------------------------------------------------------


async def test_a_call_without_a_known_price_is_marked(owner, session_factory, clock):
    unknown = await add_key(session_factory, owner.id, general=True)
    known = await add_key(
        session_factory,
        owner.id,
        kind="openai",
        base_url="https://api.openai.com/v1",
        models=("gpt-4.1",),
    )
    for key in (unknown, known):
        async with session_factory() as session:
            route = await choose_route(
                session, provider=key, task="conversation", catalog=shipped_catalog()
            )
        assert route is not None
        await run(metered(FakeChatModel(["ok"]), key, owner, session_factory, clock, route=route))

    (unknown_call,) = await calls_of(session_factory, unknown.id)
    assert (unknown_call.model_id, unknown_call.price_unknown) == ("example-sonnet-1", True)
    (known_call,) = await calls_of(session_factory, known.id)
    assert (known_call.model_id, known_call.price_unknown) == ("gpt-4.1", False)


async def test_a_call_asks_no_more_output_than_its_model_gives(owner, session_factory, clock):
    key = await add_key(
        session_factory,
        owner.id,
        general=True,
        kind="anthropic",
        base_url="https://api.anthropic.com",
        models=("claude-small", "claude-other"),
    )
    small = ModelInfo("claude-small", UNKNOWN_PRICES, 200_000, 4096, True, False)
    catalog = Catalog(date(2026, 11, 1), Decimal("1.1"), {"anthropic": {"claude-small": small}})
    known, unknown = FakeChatModel(["ok"]), FakeChatModel(["ok"])

    def on(inner: FakeChatModel, model_id: str) -> MeteredModel:
        route = route_on(key, model_id=model_id)
        return metered(inner, key, owner, session_factory, clock, route=route, catalog=catalog)

    for asked in (8192, 4096, 1024):
        await run(on(known, "claude-small"), max_output_tokens=asked)
    await run(on(unknown, "claude-other"), max_output_tokens=8192)

    # The model gives 4096 tokens at most: more is never asked of it, nor reserved.
    assert [call.max_output_tokens for call in known.calls] == [4096, 4096, 1024]
    # (The rows start at the same moment of the test's clock: their order is not the calls'.)
    reserved = sorted(
        call.reserved_usd
        for call in await calls_of(session_factory, key.id)
        if call.model_id == "claude-small"
    )
    # Asked 8192 or 4096, the same reservation; asked 1024, a smaller one.
    assert reserved[0] < reserved[1] == reserved[2]
    # A model whose limit the catalog does not know is asked what the caller asks.
    assert [call.max_output_tokens for call in unknown.calls] == [8192]


async def test_a_counted_call_passes_on_whether_the_model_may_think(owner, session_factory, clock):
    key = await add_key(session_factory, owner.id, general=True)
    inner = FakeChatModel(["ok"])
    model = metered(inner, key, owner, session_factory, clock)
    await run(model, think=False)
    await run(model)
    assert [call.think for call in inner.calls] == [False, True]


async def test_the_key_check_probe_is_counted(owner, session_factory, gk_client, gk_mock):
    key_id = (
        await add_key(
            session_factory,
            owner.id,
            general=True,
            kind="anthropic",
            base_url="https://api.anthropic.com",
        )
    ).id
    model = FakeChatModel(["pong"])
    async with session_factory() as session:
        key = await session.get(Provider, key_id)
        assert key is not None

        async def checked():
            return await check_key(
                session,
                gk_client,
                key,
                fake_factory(model),
                FakeModelLister(),
                catalog=shipped_catalog(),
                now=datetime.now(UTC),
                session_factory=session_factory,
            )

        assert (await checked()).error is None
        rows = await calls_of(session_factory, key_id)
        assert [(r.person_id, r.task, r.model_id, r.status) for r in rows] == [
            (owner.id, "key_check", "example-haiku-1", "settled")
        ]

        # At the end of a limit, the check is refused before the model is asked.
        await add_limit(session_factory, key_id, amount="0.0001", set_by=owner.id)
        assert (await checked()).error == "limit_reached"
        assert len(model.calls) == 1
        # What the caller holds is untouched (its page goes on with the key), and its session
        # is still usable.
        assert (key.id, key.secret_ref) == (key_id, f"provider:{key_id}:api_key")
        await session.commit()


async def test_a_call_on_a_removed_key_stops_with_no_model(owner, session_factory, clock):
    key = await add_key(session_factory, owner.id, general=True)
    async with session_factory() as writer:
        await writer.execute(delete(Provider).where(Provider.id == key.id))
        await writer.commit()
    inner = FakeChatModel(["never said"])

    with pytest.raises(ModelCallError) as raised:
        await run(metered(inner, key, owner, session_factory, clock))

    assert raised.value.kind == "no_model"
    assert inner.calls == []
    async with session_factory() as reader:
        assert (await reader.execute(select(ModelCall))).scalars().all() == []


async def test_a_turn_audits_its_cost(owner_chat, last_audit_entry, session_factory):
    server = owner_chat.server

    await owner_chat.say("hello")

    # The model says "ok" with 3 tokens in and 2 out, at the unknown prices (15 and 75 dollars
    # per million tokens): 195 millionths of a dollar.
    entry = await last_audit_entry("chat.turn")
    assert (entry.details["cost_usd"], entry.details["estimated"]) == ("0.000195", False)
    (call,) = await calls_of(session_factory, server.provider_id)
    assert (call.task, call.person_id, call.status, call.cost_usd) == (
        "conversation",
        owner_chat.person.id,
        "settled",
        Decimal("0.000195"),
    )
    assert (call.input_tokens, call.output_tokens, call.estimated) == (3, 2, False)


async def test_a_known_yes_asks_no_model_to_read_it(adult_chat):
    server = adult_chat.server
    await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    asked_before = len(server.requested)
    adult_chat.use_model(FakeChatModel(["Done."]))

    answer = await adult_chat.say("oui")

    assert 'data-confirmation-status="executed"' in answer.cards_html
    # The code knows the phrase: the only model asked for is the conversation's, so a plain yes
    # costs no reading.
    assert len(server.requested) == asked_before + 1


# --- a limit for a role ---------------------------------------------------------------------------


@pytest.fixture
async def robin(add_member):
    return await add_member("Robin", "guest")


@pytest.fixture
async def dana(add_member):
    return await add_member("Dana", "guest")


async def try_reserve(session_factory, key: Provider, person: Person, clock, usd: str):
    """``reserve`` for ``person`` on ``key``, committed: an id if the call fits."""
    async with session_factory() as session:
        reserved = await reserve(
            session,
            route=route_on(key),
            person_id=person.id,
            max_usd=Decimal(usd),
            now=clock(),
            catalog=shipped_catalog(),
            zone=None,
        )
        await session.commit()
    return reserved


async def become(session_factory, person: Person, role: str) -> None:
    async with session_factory() as writer:
        row = await writer.get(Person, person.id)
        assert row is not None
        row.role = role
        await writer.commit()


async def test_a_limit_for_each_guest_stops_one_guest_not_another(
    owner, robin, dana, session_factory, clock
):
    key = await add_key(session_factory, owner.id, general=True)
    await add_limit(
        session_factory, key.id, amount="1", set_by=owner.id, role="guest", split="each"
    )
    await add_spent(session_factory, key.id, "0.95", person_id=robin.id, at=clock())

    assert isinstance(await try_reserve(session_factory, key, robin, clock, "0.1"), LimitReached)
    assert isinstance(await try_reserve(session_factory, key, dana, clock, "0.1"), uuid.UUID)


async def test_a_limit_for_the_guests_together_counts_them_all_and_not_an_adult(
    owner, alex, robin, dana, add_member, session_factory, clock
):
    key = await add_key(session_factory, owner.id, general=True)
    await add_limit(
        session_factory, key.id, amount="1", set_by=owner.id, role="guest", split="together"
    )
    await add_spent(session_factory, key.id, "0.5", person_id=robin.id, at=clock())
    await add_spent(session_factory, key.id, "0.5", person_id=dana.id, at=clock())
    third = await add_member("Sam", "guest")

    assert isinstance(await try_reserve(session_factory, key, third, clock, "0.1"), LimitReached)
    assert isinstance(await try_reserve(session_factory, key, alex, clock, "0.1"), uuid.UUID)


async def test_a_role_limit_holds_for_a_guest_who_joins_later(
    owner, add_member, session_factory, clock
):
    key = await add_key(session_factory, owner.id, general=True)
    await add_limit(
        session_factory, key.id, amount="1", set_by=owner.id, role="guest", split="each"
    )
    later = await add_member("Sam", "guest")
    await add_spent(session_factory, key.id, "0.95", person_id=later.id, at=clock())

    assert isinstance(await try_reserve(session_factory, key, later, clock, "0.1"), LimitReached)


async def test_a_role_limit_counts_the_role_at_the_time_of_the_call(
    owner, robin, dana, session_factory, clock
):
    key = await add_key(session_factory, owner.id, general=True)
    together = await add_limit(
        session_factory, key.id, amount="1", set_by=owner.id, role="guest", split="together"
    )
    adults_each = await add_limit(
        session_factory, key.id, amount="1", set_by=owner.id, role="adult", split="each"
    )
    await add_spent(session_factory, key.id, "0.9", person_id=robin.id, at=clock())
    assert await try_reserve(session_factory, key, dana, clock, "0.2") == LimitReached(together)

    await become(session_factory, robin, "adult")

    # Robin no longer counts with the guests, so a guest passes...
    assert isinstance(await try_reserve(session_factory, key, dana, clock, "0.2"), uuid.UUID)
    # ... and the limit of each adult applies to Robin, who spent 0.9 of 1.
    assert await try_reserve(session_factory, key, robin, clock, "0.2") == LimitReached(adults_each)


async def test_a_guest_who_left_still_counts_with_the_guests(
    owner, robin, dana, session_factory, clock
):
    key = await add_key(session_factory, owner.id, general=True)
    together = await add_limit(
        session_factory, key.id, amount="1", set_by=owner.id, role="guest", split="together"
    )
    await add_spent(session_factory, key.id, "0.9", person_id=robin.id, at=clock())
    async with session_factory() as writer:
        row = await writer.get(Person, robin.id)
        assert row is not None
        row.disabled_at = clock()
        row.state = "gone"
        await writer.commit()

    # Robin left the household, a guest still: what he spent this month was spent.
    assert await try_reserve(session_factory, key, dana, clock, "0.2") == LimitReached(together)


async def test_each_and_together_of_one_role_on_one_key_are_both_enforced(
    owner, robin, dana, session_factory, clock
):
    key = await add_key(session_factory, owner.id, general=True)
    each = await add_limit(
        session_factory, key.id, amount="1", set_by=owner.id, role="guest", split="each"
    )
    together = await add_limit(
        session_factory, key.id, amount="1.5", set_by=owner.id, role="guest", split="together"
    )
    await add_spent(session_factory, key.id, "0.4", person_id=robin.id, at=clock())
    await add_spent(session_factory, key.id, "0.9", person_id=dana.id, at=clock())

    # Robin's 0.3 fits under his own 1 (0.7), but not under the guests' 1.5 (1.6).
    assert await try_reserve(session_factory, key, robin, clock, "0.3") == LimitReached(together)
    # Dana's 0.15 fits under the guests' 1.5 (1.45), but not under her own 1 (1.05).
    assert await try_reserve(session_factory, key, dana, clock, "0.15") == LimitReached(each)
    # And a call that fits both is made.
    assert isinstance(await try_reserve(session_factory, key, robin, clock, "0.1"), uuid.UUID)


async def test_a_limit_for_each_of_a_role_is_measured_for_one_of_them(
    owner, session_factory, clock
):
    key = await add_key(session_factory, owner.id, general=True)
    await add_limit(
        session_factory, key.id, amount="1", set_by=owner.id, role="guest", split="each"
    )
    async with session_factory() as session:
        limit = (await session.execute(select(SpendingLimit))).scalar_one()
        with pytest.raises(ValueError):
            await _measured(
                session, limit, for_person=None, now=clock(), zone=None, catalog=shipped_catalog()
            )


async def test_each_and_together_for_one_role_hold_side_by_side(owner, session_factory, clock):
    key = await add_key(session_factory, owner.id, general=True)
    options = {"provider_id": key.id, "person_id": None, "currency": "USD", "period": "month"}
    options |= {"warn_at_percent": None, "by": owner.id, "now": clock(), "role": "guest"}
    async with session_factory() as session:
        for split, amount in (("each", "2"), ("together", "5"), ("each", "3")):
            assert await set_spending_limit(session, amount=Decimal(amount), split=split, **options)
        await session.commit()
    async with session_factory() as reader:
        rows = await reader.execute(
            select(SpendingLimit.split, SpendingLimit.amount).order_by(SpendingLimit.split)
        )
        assert [tuple(row) for row in rows] == [
            ("each", Decimal("3.0000")),
            ("together", Decimal("5.0000")),
        ]


async def test_every_limit_that_applies_holds_at_once(owner, robin, session_factory, clock):
    key = await add_key(session_factory, owner.id, general=True)
    whole = await add_limit(session_factory, key.id, amount="5", set_by=owner.id)
    await add_limit(
        session_factory, key.id, amount="2", set_by=owner.id, role="guest", split="each"
    )
    his = await add_limit(session_factory, key.id, amount="1", person_id=robin.id, set_by=owner.id)
    await add_spent(session_factory, key.id, "0.95", person_id=robin.id, at=clock())

    refused = await try_reserve(session_factory, key, robin, clock, "0.1")
    assert isinstance(refused, LimitReached) and refused.limit_id == his != whole


async def test_set_spending_limit_for_a_role_audits_role_and_split(
    owner, alex, session_factory, clock, last_audit_entry
):
    key = await add_key(session_factory, owner.id, general=True)
    options = {"provider_id": key.id, "currency": "EUR", "period": "week", "warn_at_percent": 50}
    options |= {"by": owner.id, "now": clock()}
    async with session_factory() as session:
        await set_spending_limit(
            session, person_id=None, amount=Decimal(2), role="guest", split="each", **options
        )
        await session.commit()
    assert (await last_audit_entry("spending.limit_set")).details == {
        "provider_id": key.id,
        "person": None,
        "role": "guest",
        "split": "each",
        "amount": "2",
        "currency": "EUR",
        "period": "week",
    }
    async with session_factory() as session:
        await set_spending_limit(session, person_id=alex.id, amount=Decimal(2), **options)
        await session.commit()
    details = (await last_audit_entry("spending.limit_set")).details
    assert "role" not in details and "split" not in details
    # Taken away: an entry of its own, never one that reads as a limit set.
    async with session_factory() as session:
        assert await set_spending_limit(session, person_id=alex.id, amount=None, **options)
        await session.commit()
    assert (await last_audit_entry("spending.limit_removed")).details == {
        "provider_id": key.id,
        "person": str(alex.id),
    }
    async with session_factory() as session:
        with pytest.raises(ValueError):
            await set_spending_limit(
                session, person_id=alex.id, amount=Decimal(2), role="guest", split="each", **options
            )
        with pytest.raises(ValueError):
            await set_spending_limit(
                session, person_id=None, amount=Decimal(2), role="guest", **options
            )
        with pytest.raises(ValueError):
            await set_spending_limit(
                session, person_id=None, amount=Decimal(2), split="each", **options
            )


async def test_limit_notes_for_each_tell_the_largest_share_and_whose(
    owner, robin, dana, session_factory, clock
):
    key = await add_key(session_factory, owner.id, general=True)
    await add_limit(
        session_factory, key.id, amount="1", set_by=owner.id, role="guest", split="each"
    )

    async def the_note():
        async with session_factory() as session:
            (note,) = await limit_notes(
                session,
                person_id=owner.id,
                now=clock(),
                catalog=shipped_catalog(),
                zone=None,
                own_thresholds={},
            )
        return note

    empty = await the_note()
    assert (empty.percent, empty.top_person_name) == (0, None)
    assert (empty.role, empty.split) == ("guest", "each")
    await add_spent(session_factory, key.id, "0.2", person_id=robin.id, at=clock())
    await add_spent(session_factory, key.id, "0.6", person_id=dana.id, at=clock())
    note = await the_note()
    assert (note.percent, note.top_person_name) == (60, "Dana")


async def test_a_removed_role_limit_is_gone(owner, session_factory, clock):
    key = await add_key(session_factory, owner.id, general=True)
    await add_limit(
        session_factory, key.id, amount="1", set_by=owner.id, role="guest", split="each"
    )
    options = {"provider_id": key.id, "person_id": None, "currency": "USD", "period": "month"}
    options |= {"warn_at_percent": None, "by": owner.id, "now": clock(), "amount": None}
    async with session_factory() as session:
        # Another split is another limit: nothing to remove there.
        assert not await set_spending_limit(session, role="guest", split="together", **options)
        assert await set_spending_limit(session, role="guest", split="each", **options)
        assert not await set_spending_limit(session, role="guest", split="each", **options)
        await session.commit()
    async with session_factory() as reader:
        assert (await reader.execute(select(SpendingLimit))).first() is None


# --- the calls of sound ---------------------------------------------------------------------------


def test_a_call_of_sound_costs_its_minutes_and_its_characters():
    prices = SpeechPrices(Decimal("0.006"), Decimal(15))
    assert speech_cost_usd(prices, heard_seconds=90.0, said_chars=0) == Decimal("0.009")
    assert speech_cost_usd(prices, heard_seconds=0.0, said_chars=2000) == Decimal("0.03")
    assert speech_cost_usd(FREE_SPEECH, heard_seconds=60.0, said_chars=10**6) == 0


async def test_a_sound_reservation_not_settled_is_settled_at_its_maximum(
    owner, session_factory, clock
):
    key = await add_key(session_factory, owner.id, general=True)
    async with session_factory() as session:
        first = await reserve(
            session,
            route=route_on(key),
            person_id=owner.id,
            max_usd=Decimal("0.5"),
            now=clock(),
            catalog=shipped_catalog(),
            zone=None,
        )
        second = await reserve(
            session,
            route=route_on(key),
            person_id=owner.id,
            max_usd=Decimal("0.25"),
            now=clock(),
            catalog=shipped_catalog(),
            zone=None,
        )
        assert isinstance(first, uuid.UUID) and isinstance(second, uuid.UUID)
        cost = await settle_speech(
            session,
            first,
            heard_seconds=1.5,
            said_chars=0,
            prices=FREE_SPEECH,
            failed_before_any_answer=False,
            now=clock(),
        )
        await session.commit()
    assert cost == 0
    clock.advance(timedelta(hours=1))
    async with session_factory() as session:
        assert await settle_stale(session, clock()) == 1  # only the one never settled
        await session.commit()
    done = {c.id: c for c in await calls_of(session_factory, key.id)}
    assert done[first].cost_usd == 0 and done[first].heard_seconds == Decimal("1.50")
    assert done[second].cost_usd == Decimal("0.25")
