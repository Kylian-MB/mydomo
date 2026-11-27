"""A turn, prepared and run with no page: its events are data, and nothing in them is HTML."""

from collections.abc import Callable
from datetime import datetime

import pytest
from chat_support import ChatServer, Member, tool_results
from conversation_support import EVERYTHING, Collecting, request_of, said, stored, turn_deps
from domo_common.i18n import t
from domo_core.config import CoreSettings
from domo_core.conversation.channel import Abilities
from domo_core.conversation.events import Acted, Ended, Failed, Words
from domo_core.conversation.prepare import NoKey, prepare_turn
from domo_core.conversation.service import Conversation
from domo_core.gatekeeper_client import GatekeeperClient
from domo_core.llm.types import ChatUsage, ToolCall
from domo_core.memory.models import Memory
from domo_core.prompts import JustDecided
from domo_core.trust.models import TrustFact
from fakes import FakeChatModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def test_a_turn_gives_words_then_ends(
    chat_server: ChatServer,
    owner_member: Member,
    settings: CoreSettings,
    gk_client: GatekeeperClient,
):
    owner = owner_member.person
    deps = turn_deps(
        session_factory=chat_server.session_factory,
        model=FakeChatModel(["Hel", "lo"]),
        settings=settings,
        gatekeeper=gk_client,
        clock=chat_server.clock,
    )
    reply = await said(chat_server.session_factory, owner, "Hi")
    deps.streaming_replies.add(reply.id)
    turn = await prepare_turn(
        deps, person=owner, reply=reply, request=request_of(owner, chat_server.clock())
    )
    listener = Collecting()
    assert await turn.run(listener) == "spoke"
    assert listener.events == [Words("Hel"), Words("lo"), Ended("complete")]
    kept = await stored(chat_server.session_factory, reply.id)
    assert (kept.content, kept.status) == ("Hello", "complete")
    assert reply.id not in deps.streaming_replies  # the turn let go of its claim


async def test_a_tool_call_gives_its_card_as_data(
    chat_server: ChatServer,
    owner_member: Member,
    settings: CoreSettings,
    gk_client: GatekeeperClient,
):
    owner = owner_member.person
    model = FakeChatModel(tool_script=[ToolCall("c1", "household_list", "{}"), "Voilà."])
    deps = turn_deps(
        session_factory=chat_server.session_factory,
        model=model,
        settings=settings,
        gatekeeper=gk_client,
        clock=chat_server.clock,
    )
    reply = await said(chat_server.session_factory, owner, "Who is here?")
    turn = await prepare_turn(
        deps, person=owner, reply=reply, request=request_of(owner, chat_server.clock())
    )
    listener = Collecting()
    await turn.run(listener)
    [acted] = [event for event in listener.events if isinstance(event, Acted)]
    assert acted.record.card["key"] == "action.card.done.household.list"
    assert all("<" not in repr(event) for event in listener.events)
    assert listener.events[-1] == Ended("complete")


async def test_a_turn_with_no_key_fails_with_its_reason(
    session_factory: async_sessionmaker[AsyncSession],
    owner_member: Member,
    settings: CoreSettings,
    gk_client: GatekeeperClient,
    live_clock: Callable[[], datetime],
):
    # No key at all in the household: nobody is answered, not even by an upkeep turn.
    owner = owner_member.person
    deps = turn_deps(
        session_factory=session_factory,
        model=FakeChatModel(["never asked"]),
        settings=settings,
        gatekeeper=gk_client,
        clock=live_clock,
    )
    reply = await said(session_factory, owner, "Hi")
    conversation = Conversation(deps)
    listener = Collecting()
    with pytest.raises(NoKey):
        await conversation.answer(owner, reply.id, request_of(owner, live_clock()), listener)
    assert listener.events == [Failed(reason="no_key"), Ended("error")]
    kept = await stored(session_factory, reply.id)
    assert (kept.status, kept.failure) == ("error", "no_key")
    assert reply.id not in deps.streaming_replies


async def test_what_shows_once_is_not_made_when_the_channel_cannot_show_it(
    chat_server: ChatServer,
    owner_member: Member,
    settings: CoreSettings,
    gk_client: GatekeeperClient,
):
    owner = owner_member.person
    invite = ToolCall("c1", "household_invite", '{"display_name":"Dana","role":"guest"}')
    model = FakeChatModel(tool_script=[invite, "Dana est invitée."], judge="do")
    deps = turn_deps(
        session_factory=chat_server.session_factory,
        model=model,
        settings=settings,
        gatekeeper=gk_client,
        clock=chat_server.clock,
    )
    reply = await said(chat_server.session_factory, owner, "Invite Dana for two days.")
    nowhere_to_show_it_once = Abilities(
        buttons=EVERYTHING.buttons,
        shows_once=False,
        safe_screen=EVERYTHING.safe_screen,
        hears_unasked=EVERYTHING.hears_unasked,
        proof_kinds=EVERYTHING.proof_kinds,
    )
    turn = await prepare_turn(
        deps,
        person=owner,
        reply=reply,
        request=request_of(owner, chat_server.clock(), nowhere_to_show_it_once),
    )
    listener = Collecting()
    await turn.run(listener)
    [acted] = [event for event in listener.events if isinstance(event, Acted)]
    assert acted.record.status == "done"
    assert "invitation" in acted.record.card
    assert acted.record.card_once is None
    [result] = tool_results(model.calls)
    assert result["link"] == "waits_for_them"


async def test_an_answer_cut_short_with_nothing_said_gives_the_sentence_of_the_catalog(
    chat_server: ChatServer,
    owner_member: Member,
    settings: CoreSettings,
    gk_client: GatekeeperClient,
):
    owner = owner_member.person
    model = FakeChatModel([], usage=ChatUsage(10, 2048, cut_short=True))
    deps = turn_deps(
        session_factory=chat_server.session_factory,
        model=model,
        settings=settings,
        gatekeeper=gk_client,
        clock=chat_server.clock,
    )
    reply = await said(chat_server.session_factory, owner, "Hi")
    turn = await prepare_turn(
        deps, person=owner, reply=reply, request=request_of(owner, chat_server.clock())
    )
    listener = Collecting()
    # A cut reply does not count as Domo having spoken.
    assert await turn.run(listener) == "silent"
    sentence = t("chat.cut_short", "en")
    assert listener.events == [Words(sentence), Ended("complete")]
    kept = await stored(chat_server.session_factory, reply.id)
    assert (kept.content, kept.status) == (sentence, "complete")
    limits = [call.max_output_tokens for call in model.calls]
    assert len(limits) == 3 and limits[0] < limits[1] < limits[2]


async def test_an_answer_cut_short_in_a_turn_that_may_stay_silent_says_nothing(
    chat_server: ChatServer,
    owner_member: Member,
    settings: CoreSettings,
    gk_client: GatekeeperClient,
):
    owner = owner_member.person
    deps = turn_deps(
        session_factory=chat_server.session_factory,
        model=FakeChatModel([], usage=ChatUsage(10, 2048, cut_short=True)),
        settings=settings,
        gatekeeper=gk_client,
        clock=chat_server.clock,
    )
    reply = await said(chat_server.session_factory, owner, "Hi")
    turn = await prepare_turn(
        deps,
        person=owner,
        reply=reply,
        request=request_of(owner, chat_server.clock()),
        silent_ok=True,
    )
    listener = Collecting()
    await turn.run(listener)
    assert listener.events == [Ended("complete")]


@pytest.mark.parametrize(("from_outside", "origin"), [(True, "external"), (False, "deduced")])
async def test_a_decision_wake_of_a_made_tool_marks_the_turn_and_closes_no_trust(
    chat_server: ChatServer,
    owner_member: Member,
    add_member,
    settings: CoreSettings,
    gk_client: GatekeeperClient,
    from_outside: bool,
    origin: str,
):
    # The turn a decision wakes him for carries what the decided request gave: from a made tool
    # or a program it is text nobody of the house said (``JustDecided.from_outside``). The mark
    # still says where a memory kept there comes from (nobody wrote in this turn: from outside,
    # no person's words); it no longer closes a fact of trust: he
    # weighs the source.
    owner = owner_member.person
    await add_member("Sam Martin", "adult")
    call = ToolCall(
        "c1",
        "trust_note",
        '{"person": "Sam", "outcome": "held", "how": "seen", "about": "The boiler was off."}',
    )
    keep = ToolCall(
        "c2",
        "memory_keep",
        '{"content": "The boiler was off.", "subject": "the boiler", "kind": "fact", '
        '"private": false, "core": false}',
    )
    deps = turn_deps(
        session_factory=chat_server.session_factory,
        model=FakeChatModel(tool_script=[call, keep, "ok"]),
        settings=settings,
        gatekeeper=gk_client,
        clock=chat_server.clock,
    )
    reply = await said(chat_server.session_factory, owner, "Hi")
    decided = JustDecided(
        request_id="r",
        description="A made tool ran",
        outcome="executed",
        decided_by="the person",
        via="button",
        result={"status": "done"},
        woken=True,
        from_outside=from_outside,
    )
    turn = await prepare_turn(
        deps,
        person=owner,
        reply=reply,
        request=request_of(owner, chat_server.clock()),
        decided=decided,
    )
    await turn.run(Collecting())
    async with chat_server.session_factory() as session:
        count = await session.scalar(select(func.count()).select_from(TrustFact))
        origins = list(await session.scalars(select(Memory.origin)))
    assert count == 1
    assert origins == [origin]
