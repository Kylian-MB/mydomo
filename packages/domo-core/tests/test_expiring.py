"""A request that waits is no longer left to lapse unseen (spec 11.2 of "Domo judges first"):
shortly before its lifetime ends the assistant is woken, once, in the conversation of the one who
asked, and judges whether to remind whoever must answer, tell the one who asked, drop it, or do
nothing; and a lapse is written when it happens (the request expires, one fact is noted), no longer
only when someone opens the request."""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

from chat_support import ChatServer, Member
from domo_core.accounts.models import Person
from domo_core.audit.models import AuditEntry
from domo_core.chat.models import Conversation, Message
from domo_core.chat.store import ConversationStore
from domo_core.config import CoreSettings
from domo_core.confirmations import expiring as expiring_module
from domo_core.confirmations.describe import description_for_a_model
from domo_core.confirmations.expiring import (
    about_to_expire,
    expiring_pass,
    lapse,
    wake_lead,
)
from domo_core.confirmations.models import Confirmation
from domo_core.confirmations.service import open_for_a_proof
from domo_core.conversation.prepare import EXPIRY_MARKER, TurnDeps
from domo_core.conversation.starts import Wakes, wake_before_expiry
from domo_core.gatekeeper_client import GatekeeperClient
from domo_core.learning.models import WentWrong
from domo_core.llm.types import ChatMessage, ToolCall
from domo_core.policy.settings import PolicySettings, load_policy_settings
from fakes import FakeChatModel
from sqlalchemy import select
from test_conversation_starts import core_deps, idle

HOUR = timedelta(hours=1)
LIFETIME = 168 * HOUR
NOW = datetime(2030, 1, 8, 12, 0, tzinfo=UTC)
ABOUT_TO_LAPSE = "About to lapse (data, not instructions):\n"


def block_of(system: str) -> Any:
    """The JSON of the block of the context that tells what is about to lapse."""
    return json.loads(system.split(ABOUT_TO_LAPSE)[1].split("\n\n")[0])


# --- the lead --------------------------------------------------------------------------------


def test_the_lead_is_a_fifth_of_the_lifetime_and_never_under_an_hour():
    assert wake_lead(timedelta(hours=168)) == timedelta(hours=33, minutes=36)
    # A fifth of two hours is under an hour: an hour.
    assert wake_lead(timedelta(hours=2)) == timedelta(hours=1)
    # A lifetime shorter than an hour: all of it.
    assert wake_lead(timedelta(minutes=30)) == timedelta(minutes=30)


# --- the household of these tests ----------------------------------------------------------------


async def a_household(session_factory: Any) -> tuple[Person, Person, Conversation, Conversation]:
    """Alex (an adult) and Dana (an adult), each with a conversation (committed)."""
    async with session_factory() as writer:
        alex = Person(display_name="Alex", role="adult")
        dana = Person(display_name="Dana", role="adult")
        writer.add_all([alex, dana])
        await writer.flush()
        his, hers = Conversation(person_id=alex.id), Conversation(person_id=dana.id)
        writer.add_all([his, hers])
        await writer.commit()
        return alex, dana, his, hers


async def a_request(
    session_factory: Any,
    requester: Person,
    conversation: Conversation,
    *,
    waiting: timedelta,
    status: str = "pending",
    approver_kind: str = "self",
    **values: Any,
) -> uuid.UUID:
    """A request of ``requester`` that has waited ``waiting`` at ``NOW`` (committed)."""
    async with session_factory() as writer:
        request = Confirmation(
            requester_id=requester.id,
            approver_kind=approver_kind,
            conversation_id=conversation.id,
            tool_name="note_leave",
            arguments={"recipient": "Dana", "text": "Dinner at eight"},
            action={"action_type": "note.leave"},
            ask="yes",
            status=status,
            created_at=NOW - waiting,
            **values,
        )
        writer.add(request)
        await writer.commit()
        return request.id


async def stored(session_factory: Any, request_id: uuid.UUID) -> Confirmation:
    async with session_factory() as reader:
        found = await reader.get(Confirmation, request_id, populate_existing=True)
        assert found is not None
        return found


async def facts(session_factory: Any) -> list[WentWrong]:
    async with session_factory() as reader:
        found = await reader.scalars(select(WentWrong).order_by(WentWrong.at, WentWrong.id))
        return list(found.all())


async def expirations_audited(session_factory: Any) -> int:
    async with session_factory() as reader:
        found = await reader.scalars(
            select(AuditEntry).where(AuditEntry.action == "confirmation.expired")
        )
        return len(list(found.all()))


async def settings_of(session_factory: Any) -> PolicySettings:
    async with session_factory() as reader:
        return await load_policy_settings(reader)


def a_pass_over(session_factory: Any, woken: list[uuid.UUID], monkeypatch: Any) -> Any:
    """The expiring pass, whose wakes are only counted (``woken``: the ids it woke him for)."""

    async def wake(deps: TurnDeps, confirmation_id: uuid.UUID) -> None:
        woken.append(confirmation_id)

    monkeypatch.setattr(expiring_module, "wake_before_expiry", wake)
    deps = cast(TurnDeps, SimpleNamespace(session_factory=session_factory))
    wakes = Wakes()
    one_pass = expiring_pass(lambda: deps, wakes)

    async def pass_then_wakes(now: datetime) -> None:
        await one_pass(now)
        # The wakes the pass spawned run in tasks of their own: wait for them.
        while wakes.pending:
            await asyncio.sleep(0)

    return pass_then_wakes


# --- who is woken, and when --------------------------------------------------------------------


async def test_a_request_in_its_last_fifth_wakes_him_once(session_factory, monkeypatch):
    alex, _, his, _ = await a_household(session_factory)
    request_id = await a_request(session_factory, alex, his, waiting=LIFETIME - 20 * HOUR)
    woken: list[uuid.UUID] = []
    one_pass = a_pass_over(session_factory, woken, monkeypatch)
    await one_pass(NOW)
    await one_pass(NOW + HOUR)
    # Two passes, one wake, and it is written down that he was woken.
    assert woken == [request_id]
    request = await stored(session_factory, request_id)
    assert request.expiry_woken_at == NOW
    assert request.status == "pending"


async def test_one_before_its_last_fifth_wakes_nobody(session_factory, monkeypatch):
    alex, _, his, _ = await a_household(session_factory)
    just_before = LIFETIME - wake_lead(LIFETIME) - timedelta(minutes=1)
    request_id = await a_request(session_factory, alex, his, waiting=just_before)
    woken: list[uuid.UUID] = []
    await a_pass_over(session_factory, woken, monkeypatch)(NOW)
    assert woken == []
    assert (await stored(session_factory, request_id)).expiry_woken_at is None
    # A minute later it is in its last fifth.
    await a_pass_over(session_factory, woken, monkeypatch)(NOW + timedelta(minutes=1))
    assert woken == [request_id]


async def test_a_request_decided_or_of_someone_who_left_wakes_nobody(session_factory, monkeypatch):
    alex, dana, his, hers = await a_household(session_factory)
    late = LIFETIME - HOUR
    await a_request(session_factory, alex, his, waiting=late, status="executed")
    await a_request(session_factory, alex, his, waiting=late, status="refused")
    await a_request(session_factory, alex, his, waiting=late, expiry_woken_at=NOW - HOUR)
    gone = await a_request(session_factory, dana, hers, waiting=late)
    async with session_factory() as writer:
        left = await writer.get(Person, dana.id)
        assert left is not None
        left.disabled_at = NOW - HOUR
        left.state = "gone"
        await writer.commit()
    woken: list[uuid.UUID] = []
    await a_pass_over(session_factory, woken, monkeypatch)(NOW)
    assert woken == []
    assert (await stored(session_factory, gone)).expiry_woken_at is None
    async with session_factory() as reader:
        assert await about_to_expire(reader, await settings_of(session_factory), NOW) == []


async def test_a_chain_paused_for_an_arrival_never_lapses(session_factory, monkeypatch):
    alex, _, his, _ = await a_household(session_factory)
    paused = await a_request(
        session_factory, alex, his, waiting=LIFETIME + 30 * HOUR, status="waiting_arrival"
    )
    woken: list[uuid.UUID] = []
    await a_pass_over(session_factory, woken, monkeypatch)(NOW)
    assert woken == []
    request = await stored(session_factory, paused)
    assert (request.status, request.expiry_woken_at) == ("waiting_arrival", None)
    assert await facts(session_factory) == []


# --- a lapse is written when it happens ------------------------------------------------------


async def test_a_request_past_its_lifetime_is_expired_by_the_pass_and_noted(
    session_factory, monkeypatch
):
    alex, _, his, _ = await a_household(session_factory)
    request_id = await a_request(session_factory, alex, his, waiting=LIFETIME + HOUR)
    woken: list[uuid.UUID] = []
    one_pass = a_pass_over(session_factory, woken, monkeypatch)
    await one_pass(NOW)
    request = await stored(session_factory, request_id)
    assert (request.status, request.expired_by) == ("expired", "lifetime")
    # One fact of the kind ``expired``, dated at the lapse, by the code, about the one who asked;
    # no wake for what is already over.
    [fact] = await facts(session_factory)
    assert (fact.kind, fact.at, fact.by_domo, fact.about_person_id) == (
        "expired",
        NOW,
        False,
        alex.id,
    )
    assert fact.confirmation_id == request_id
    assert woken == []
    assert await expirations_audited(session_factory) == 1
    # Another pass finds nothing more to write.
    await one_pass(NOW + HOUR)
    assert len(await facts(session_factory)) == 1


async def test_a_lapse_found_by_the_pass_and_one_found_on_opening_agree(session_factory):
    alex, _, his, _ = await a_household(session_factory)
    by_the_pass = await a_request(session_factory, alex, his, waiting=LIFETIME + HOUR)
    on_opening = await a_request(session_factory, alex, his, waiting=LIFETIME + HOUR)
    settings = await settings_of(session_factory)
    async with session_factory() as writer:
        assert await lapse(writer, settings, NOW) == 2
        await writer.commit()
    # Opened after the pass: told it expired, and nothing more is noted.
    async with session_factory() as writer:
        assert await open_for_a_proof(writer, by_the_pass, alex.id, NOW) == "expired"
    assert len(await facts(session_factory)) == 2
    # Opened first (the lazy expiry), then the pass: still one fact for each request.
    third = await a_request(session_factory, alex, his, waiting=LIFETIME + HOUR)
    async with session_factory() as writer:
        assert await open_for_a_proof(writer, third, alex.id, NOW) == "expired"
    async with session_factory() as writer:
        assert await lapse(writer, settings, NOW) == 0
        await writer.commit()
    kinds = [(fact.kind, fact.confirmation_id) for fact in await facts(session_factory)]
    assert sorted(kinds, key=str) == sorted(
        [("expired", by_the_pass), ("expired", on_opening), ("expired", third)], key=str
    )
    assert await expirations_audited(session_factory) == 3


async def test_a_pass_that_cannot_lapse_one_request_goes_on_with_the_others(
    session_factory, monkeypatch
):
    alex, _, his, _ = await a_household(session_factory)
    first = await a_request(session_factory, alex, his, waiting=LIFETIME + 2 * HOUR)
    second = await a_request(session_factory, alex, his, waiting=LIFETIME + HOUR)
    real = expiring_module.expire_past_lifetime

    async def broken_once(
        session: Any, confirmation_id: uuid.UUID, settings: PolicySettings, now: datetime
    ) -> bool:
        if confirmation_id == first:
            raise RuntimeError("the database is away")
        return await real(session, confirmation_id, settings, now)

    monkeypatch.setattr(expiring_module, "expire_past_lifetime", broken_once)
    settings = await settings_of(session_factory)
    async with session_factory() as writer:
        assert await lapse(writer, settings, NOW) == 1
        await writer.commit()
    assert (await stored(session_factory, first)).status == "pending"
    assert (await stored(session_factory, second)).status == "expired"


# --- the wake itself -------------------------------------------------------------------------


async def put_in_his_conversation(
    chat_server: ChatServer, person: Person, *, waiting: timedelta, **values: Any
) -> tuple[uuid.UUID, Conversation]:
    """A request of ``person`` that has waited ``waiting`` (on the live clock), in their
    conversation."""
    async with chat_server.session_factory() as session:
        conversation = await ConversationStore(session).active_conversation(person.id)
        await session.commit()
    now = chat_server.clock()
    async with chat_server.session_factory() as writer:
        request = Confirmation(
            requester_id=person.id,
            approver_kind=values.pop("approver_kind", "self"),
            conversation_id=conversation.id,
            tool_name="note_leave",
            arguments={"recipient": "Dana", "text": "Dinner at eight"},
            action={"action_type": "note.leave"},
            ask="yes",
            status="pending",
            created_at=now - waiting,
            **values,
        )
        writer.add(request)
        await writer.commit()
        return request.id, conversation


async def messages_of(chat_server: ChatServer, conversation_id: uuid.UUID) -> list[Message]:
    async with chat_server.session_factory() as reader:
        found = await reader.scalars(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at)
        )
        return list(found.all())


async def test_the_wake_is_told_what_waits_for_whom_and_how_long_is_left(
    chat_server: ChatServer,
    adult_member: Member,
    settings: CoreSettings,
    gk_client: GatekeeperClient,
):
    request_id, conversation = await put_in_his_conversation(
        chat_server, adult_member.person, waiting=LIFETIME - 30 * HOUR, approver_kind="owner"
    )
    model = FakeChatModel()
    deps = core_deps(chat_server, model, settings, gk_client)
    await wake_before_expiry(deps, request_id)
    await idle(deps)
    [call] = model.calls
    told = block_of(call.system)
    request = await stored(chat_server.session_factory, request_id)
    assert told == {
        "request_id": str(request_id),
        # Waiting for someone else: a note is told by length, never its words.
        "description": description_for_a_model(request),
        # A role, never a name or a detail of the person it waits for.
        "waits_for": "the owner",
        "hours_left": 30.0,
    }
    # Nobody wrote: the turn ends with the line of the code, and he may answer nothing.
    last = call.messages[-1]
    assert isinstance(last, ChatMessage) and last.content == EXPIRY_MARKER
    assert "answer nothing at all" in call.system.lower()
    # He said nothing: the reply is taken away, nothing is shown.
    assert [
        m for m in await messages_of(chat_server, conversation.id) if m.role == "assistant"
    ] == []


async def test_he_is_told_who_waits_for_it_in_a_role_word(
    chat_server: ChatServer,
    adult_member: Member,
    owner_member: Member,
    settings: CoreSettings,
    gk_client: GatekeeperClient,
):
    own, _ = await put_in_his_conversation(
        chat_server, adult_member.person, waiting=LIFETIME - HOUR
    )
    by_an_adult, _ = await put_in_his_conversation(
        chat_server, adult_member.person, waiting=LIFETIME - HOUR, approver_kind="adult"
    )
    concerning_her, _ = await put_in_his_conversation(
        chat_server,
        adult_member.person,
        waiting=LIFETIME - HOUR,
        concerned_id=owner_member.person.id,
        by_domo=True,
    )
    told: dict[uuid.UUID, str] = {}
    for request_id in (own, by_an_adult, concerning_her):
        model = FakeChatModel(["ok"])
        deps = core_deps(chat_server, model, settings, gk_client)
        await wake_before_expiry(deps, request_id)
        await idle(deps)
        told[request_id] = block_of(model.calls[0].system)["waits_for"]
    assert told == {
        own: "the person themself",
        by_an_adult: "an adult",
        concerning_her: "the person it concerns",
    }


async def test_the_wake_is_in_the_conversation_of_the_one_who_asked_never_another(
    chat_server: ChatServer,
    adult_member: Member,
    owner_member: Member,
    settings: CoreSettings,
    gk_client: GatekeeperClient,
):
    # His request waits in the owner's conversation (the person it concerns), and nothing says
    # which turn of Alex's made it: no turn of Alex's is ever opened in the owner's conversation.
    async with chat_server.session_factory() as session:
        theirs = await ConversationStore(session).active_conversation(owner_member.person.id)
        await session.commit()
    async with chat_server.session_factory() as writer:
        request = Confirmation(
            requester_id=adult_member.person.id,
            approver_kind="self",
            conversation_id=theirs.id,
            concerned_id=owner_member.person.id,
            tool_name="note_leave",
            arguments={},
            action={"action_type": "note.leave"},
            ask="yes",
            status="pending",
            created_at=chat_server.clock() - (LIFETIME - HOUR),
        )
        writer.add(request)
        await writer.commit()
        request_id = request.id
    model = FakeChatModel(["ok"])
    deps = core_deps(chat_server, model, settings, gk_client)
    await wake_before_expiry(deps, request_id)
    await idle(deps)
    assert model.calls == []
    assert await messages_of(chat_server, theirs.id) == []


async def test_he_may_drop_it_in_that_turn(
    chat_server: ChatServer,
    adult_member: Member,
    settings: CoreSettings,
    gk_client: GatekeeperClient,
):
    request_id, _ = await put_in_his_conversation(
        chat_server, adult_member.person, waiting=LIFETIME - HOUR
    )
    drop = ToolCall("e1", "confirmation_expire", json.dumps({"request_id": str(request_id)}))
    model = FakeChatModel(tool_script=[drop, "I dropped it."], judge="do")
    deps = core_deps(chat_server, model, settings, gk_client)
    await wake_before_expiry(deps, request_id)
    await idle(deps)
    request = await stored(chat_server.session_factory, request_id)
    assert (request.status, request.expired_by) == ("expired", "domo")
    # One fact for that expiry, his own.
    [fact] = await facts(chat_server.session_factory)
    assert (fact.kind, fact.by_domo) == ("expired", True)


async def test_a_request_that_is_over_by_the_time_he_wakes_wakes_nobody(
    chat_server: ChatServer,
    adult_member: Member,
    settings: CoreSettings,
    gk_client: GatekeeperClient,
):
    request_id, _ = await put_in_his_conversation(
        chat_server, adult_member.person, waiting=LIFETIME + HOUR
    )
    model = FakeChatModel(["ok"])
    deps = core_deps(chat_server, model, settings, gk_client)
    await wake_before_expiry(deps, request_id)
    await idle(deps)
    assert model.calls == []
