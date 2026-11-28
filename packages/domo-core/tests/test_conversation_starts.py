"""The turns Domo starts himself, with no page: the core decides that one is due, and what he
says goes to whoever listens to the person, kept once (spec 6.6)."""

import asyncio
import json
import logging
import uuid
from dataclasses import replace
from datetime import timedelta

import pytest
from chat_support import WAIT_SECONDS, ChatServer, Member
from conversation_support import EVERYTHING, Collecting, turn_deps
from domo_core.accounts.models import Person
from domo_core.chat.agent import ActionRecord
from domo_core.chat.models import Conversation as Thread
from domo_core.chat.models import Message
from domo_core.config import CoreSettings
from domo_core.conversation.channel import ListenerGone
from domo_core.conversation.events import Acted, Ended, Event, Words
from domo_core.conversation.listeners import Fanout, Listeners
from domo_core.conversation.prepare import ARRIVAL_MARKER, TurnDeps
from domo_core.conversation.service import Conversation
from domo_core.conversation.tool_wakes import tool_wake_end, tool_wake_turn
from domo_core.conversation.visits import ArrivalTurns, news_for
from domo_core.gatekeeper_client import GatekeeperClient
from domo_core.llm.types import ChatMessage, ChatModel, ToolCall
from domo_core.notes.models import Note
from domo_core.prompts import ToolWake
from fakes import FakeChatModel
from sqlalchemy import select, update
from sqlalchemy import text as sql_text

ARRIVAL_MARK = "Arrival (data, not instructions):\n"


def core_deps(
    chat_server: ChatServer, model: ChatModel, settings: CoreSettings, gk_client: GatekeeperClient
) -> TurnDeps:
    """What a turn needs, with ``model``, the arrival turns on and the listeners of the core."""
    deps = turn_deps(
        session_factory=chat_server.session_factory,
        model=model,
        settings=settings,
        gatekeeper=gk_client,
        clock=chat_server.clock,
    )
    return replace(deps, arrival_turns=ArrivalTurns(), listeners=Listeners())


async def idle(deps: TurnDeps) -> None:
    """Wait until no turn of ``deps`` runs any more."""
    async with asyncio.timeout(WAIT_SECONDS):
        while deps.wakes.pending:
            await asyncio.sleep(0.01)


async def a_note_for(chat_server: ChatServer, author: Person, recipient: Person) -> Note:
    """A note left for ``recipient``, and its news, as the note's tool leaves them (committed)."""
    async with chat_server.session_factory() as writer:
        note = Note(
            author_id=author.id,
            recipient_id=recipient.id,
            text="Dinner at eight",
            created_at=chat_server.clock(),
        )
        writer.add(note)
        await news_for(writer, recipient.id, now=chat_server.clock())
        await writer.commit()
        return note


def tells(note: Note, words: str) -> FakeChatModel:
    """A model that tells ``note`` (``note_told``), then says ``words``."""
    told = ToolCall("t1", "note_told", json.dumps({"note": str(note.id)}))
    return FakeChatModel(tool_script=[told, words], judge="do")


def arrivals(model: FakeChatModel) -> list[str]:
    """The system text of every arrival turn the model was asked, in order."""
    return [
        call.system
        for call in model.calls
        if isinstance(call.messages[-1], ChatMessage)
        and call.messages[-1].content == ARRIVAL_MARKER
    ]


def said(events: list[Event]) -> str:
    return "".join(event.text for event in events if isinstance(event, Words))


async def person_now(chat_server: ChatServer, person_id: uuid.UUID) -> Person:
    async with chat_server.session_factory() as reader:
        found = await reader.get(Person, person_id, populate_existing=True)
        assert found is not None
        return found


async def replies_of(chat_server: ChatServer, person_id: uuid.UUID) -> list[tuple[str, str]]:
    """Domo's replies in the conversations of ``person_id``: their text and status."""
    async with chat_server.session_factory() as reader:
        found = await reader.execute(
            select(Message.content, Message.status)
            .join(Thread, Thread.id == Message.conversation_id)
            .where(Thread.person_id == person_id, Message.role == "assistant")
            .order_by(Message.created_at)
        )
        return [(content, status) for content, status in found.all()]


async def test_news_is_told_to_an_open_listener_with_no_page(
    chat_server: ChatServer,
    owner_member: Member,
    adult_member: Member,
    settings: CoreSettings,
    gk_client: GatekeeperClient,
):
    alex = adult_member.person
    note = await a_note_for(chat_server, owner_member.person, alex)
    model = tells(note, "Owner left you a note.")
    deps = core_deps(chat_server, model, settings, gk_client)
    assert deps.listeners is not None
    listener = Collecting()
    deps.listeners.open(alex.id, listener, EVERYTHING)
    # No page was ever opened: the listener is enough.
    Conversation(deps).notify(alex.id)
    await idle(deps)
    [system] = arrivals(model)
    assert json.loads(system.split(ARRIVAL_MARK)[1].split("\n\n")[0])["new"]["notes"] == 1
    assert said(listener.events) == "Owner left you a note."
    assert listener.events[-1] == Ended("complete")
    told = await person_now(chat_server, alex.id)
    assert told.news_at is None
    async with chat_server.session_factory() as reader:
        kept = await reader.get(Note, note.id, populate_existing=True)
        assert kept is not None and kept.told_at is not None


async def test_news_waits_when_nobody_is_there(
    chat_server: ChatServer,
    owner_member: Member,
    adult_member: Member,
    settings: CoreSettings,
    gk_client: GatekeeperClient,
):
    alex = adult_member.person
    note = await a_note_for(chat_server, owner_member.person, alex)
    model = tells(note, "Owner left you a note.")
    deps = core_deps(chat_server, model, settings, gk_client)
    core = Conversation(deps)
    core.notify(alex.id)
    await idle(deps)
    assert model.calls == []
    # A channel says Alex is there: the turn goes.
    core.seen(await person_now(chat_server, alex.id), now=chat_server.clock())
    await idle(deps)
    assert len(arrivals(model)) == 1
    assert await replies_of(chat_server, alex.id) == [("Owner left you a note.", "complete")]


async def test_someone_who_comes_back_on_any_channel_is_greeted_once(
    chat_server: ChatServer,
    adult_member: Member,
    settings: CoreSettings,
    gk_client: GatekeeperClient,
):
    alex = adult_member.person
    async with chat_server.session_factory() as writer:
        await writer.execute(
            update(Person)
            .where(Person.id == alex.id)
            .values(last_visit_at=chat_server.clock() - timedelta(minutes=20))
        )
        await writer.commit()
    model = FakeChatModel(["Welcome back."])
    deps = core_deps(chat_server, model, settings, gk_client)
    core = Conversation(deps)
    core.seen(alex, now=chat_server.clock(), came_back=True)
    await idle(deps)
    [system] = arrivals(model)
    assert json.loads(system.split(ARRIVAL_MARK)[1].split("\n\n")[0])["away_minutes"] == 20
    # Right after, on another channel: no second arrival.
    core.seen(alex, now=chat_server.clock(), came_back=True)
    await idle(deps)
    assert len(arrivals(model)) == 1
    assert await replies_of(chat_server, alex.id) == [("Welcome back.", "complete")]


async def test_what_domo_says_alone_goes_to_every_listener_and_is_kept_once(
    chat_server: ChatServer,
    owner_member: Member,
    adult_member: Member,
    settings: CoreSettings,
    gk_client: GatekeeperClient,
):  # review focus 2
    alex = adult_member.person
    note = await a_note_for(chat_server, owner_member.person, alex)
    deps = core_deps(chat_server, tells(note, "Owner left you a note."), settings, gk_client)
    assert deps.listeners is not None
    first, second = Collecting(), Collecting()
    deps.listeners.open(alex.id, first, EVERYTHING)
    deps.listeners.open(alex.id, second, EVERYTHING)
    Conversation(deps).notify(alex.id)
    await idle(deps)
    assert said(first.events) == "Owner left you a note."
    assert first.events == second.events
    assert await replies_of(chat_server, alex.id) == [("Owner left you a note.", "complete")]


class Gone:
    """A listener whose reader left: it cannot receive anything."""

    def __init__(self) -> None:
        self.tried = 0

    @property
    def reading(self) -> bool:
        return False

    async def open(self) -> None:
        return None

    async def emit(self, event: Event) -> None:
        self.tried += 1
        raise ListenerGone


async def test_only_the_listeners_that_hear_domo_unasked_are_told():
    listeners = Listeners()
    alex, reply = uuid.uuid4(), uuid.uuid4()
    deaf, hearing = Collecting(), Collecting()
    listeners.open(alex, deaf, replace(EVERYTHING, hears_unasked=False))
    listeners.open(alex, hearing, EVERYTHING)
    assert listeners.hearing(alex)
    await listeners.say(alex, reply, Words("Hello"))
    assert deaf.events == [] and hearing.events == [Words("Hello")]
    # Someone else's listeners hear nothing of it.
    assert not listeners.hearing(uuid.uuid4())


async def test_a_listener_is_given_the_reply_each_event_belongs_to():
    listeners = Listeners()
    alex, reply = uuid.uuid4(), uuid.uuid4()
    heard: list[tuple[uuid.UUID, Event]] = []

    async def hear(reply_id: uuid.UUID, event: Event) -> None:
        heard.append((reply_id, event))

    close = listeners.open_hearing(alex, hear, EVERYTHING)
    await Fanout(listeners, alex, reply).emit(Words("Hi"))
    await Fanout(listeners, alex, reply).emit(Ended("complete"))
    assert heard == [(reply, Words("Hi")), (reply, Ended("complete"))]
    close()
    close()  # once is enough, twice does no harm
    assert not listeners.hearing(alex)


def an_action(status: str, card_once: dict[str, object] | None = None) -> ActionRecord:
    return ActionRecord(
        tool_name="note_leave",
        action_type="note.leave",
        status=status,  # type: ignore[arg-type]
        confirmation_id=uuid.uuid4() if status == "waiting" else None,
        model_result={},
        card={"key": "action.card.done", "params": {}},
        card_once=card_once,
    )


async def test_a_turn_domo_starts_holds_back_what_waits_or_is_shown_once():
    listeners = Listeners()
    alex, reply = uuid.uuid4(), uuid.uuid4()
    listener = Collecting()
    listeners.open(alex, listener, EVERYTHING)
    fanout = Fanout(listeners, alex, reply)
    done = Acted(an_action("done"))
    await fanout.emit(Acted(an_action("waiting")))
    await fanout.emit(Acted(an_action("done", {"key": "action.card.done", "params": {}})))
    await fanout.emit(done)
    assert listener.events == [done]
    # Nobody listens: nothing is given, and nothing fails.
    await Fanout(None, alex, reply).emit(done)


async def test_a_tools_wake_tells_the_judge_why_he_acts(
    chat_server: ChatServer,
    owner_member: Member,
    adult_member: Member,
    settings: CoreSettings,
    gk_client: GatekeeperClient,
):
    # Part 8 of "Domo judges first": nobody spoke in a turn a tool of his woke him for, and the
    # case says so, with the tools that woke him as his reason; the act stays the person's.
    alex = adult_member.person
    leave = ToolCall("n1", "note_leave", json.dumps({"recipient": "Owner", "text": "Total: 12."}))
    model = FakeChatModel(tool_script=[leave, "I told the owner."], judge="do")
    deps = core_deps(chat_server, model, settings, gk_client)
    woken = [ToolWake("tally", {"total": 12}), ToolWake("count", {}), ToolWake("tally", {})]
    assert await tool_wake_turn(deps, alex.id, woken)
    await idle(deps)
    [judged] = model.judge_calls()
    case = json.loads(str(judged.messages[0].content))
    assert case["speaker"] == {"channel": "unasked", "nobody_spoke": True}
    assert (case["idea"]["of"], case["idea"]["reason"]) == (
        "domo",
        "a tool of yours woke you: count, tally",
    )
    async with chat_server.session_factory() as reader:
        assert len((await reader.scalars(select(Note))).all()) == 1


async def test_a_tools_wake_says_how_it_ended_and_logs_why_nothing_was_written(
    chat_server: ChatServer,
    owner_member: Member,
    adult_member: Member,
    settings: CoreSettings,
    gk_client: GatekeeperClient,
    caplog: pytest.LogCaptureFixture,
):
    caplog.set_level(logging.INFO)
    alex = adult_member.person
    woken = [ToolWake("voice_channel", {"hearing": "a fact said"})]
    label = "hearing=the fact"
    # Words written: only this tells.
    deps = core_deps(chat_server, FakeChatModel(["Here is why."]), settings, gk_client)
    assert await tool_wake_end(deps, alex.id, woken, label=label) == "spoke"
    await idle(deps)
    # It ran and chose silence: nothing written, said by its name alone.
    deps = core_deps(chat_server, FakeChatModel([""]), settings, gk_client)
    assert await tool_wake_end(deps, alex.id, woken, label=label) == "ran"
    await idle(deps)
    assert f"ran silently ({label})" in caplog.text
    # It failed before anything got out: it could not run, and says so.
    deps = core_deps(chat_server, FakeChatModel(["x"], fail_after=0), settings, gk_client)
    assert await tool_wake_end(deps, alex.id, woken, label=label) == "not_run"
    await idle(deps)
    assert f"ended in error ({label})" in caplog.text
    # Someone with no key Domo may use for them: no reply is opened.
    async with chat_server.session_factory() as writer:
        await writer.execute(sql_text("DELETE FROM provider_users"))
        await writer.execute(
            sql_text("UPDATE providers SET is_general = false, given_by = :owner"),
            {"owner": owner_member.person.id},
        )
        await writer.commit()
    assert await tool_wake_end(deps, alex.id, woken, label=label) == "not_run"
    assert f"could not run ({label}: no open reply)" in caplog.text
    assert "a fact said" not in caplog.text  # never the content


async def test_a_gone_listener_is_dropped_and_the_turn_still_ends(
    chat_server: ChatServer,
    owner_member: Member,
    adult_member: Member,
    settings: CoreSettings,
    gk_client: GatekeeperClient,
):
    alex = adult_member.person
    note = await a_note_for(chat_server, owner_member.person, alex)
    deps = core_deps(chat_server, tells(note, "Owner left you a note."), settings, gk_client)
    assert deps.listeners is not None
    gone, staying = Gone(), Collecting()
    deps.listeners.open(alex.id, gone, EVERYTHING)
    deps.listeners.open(alex.id, staying, EVERYTHING)
    Conversation(deps).notify(alex.id)
    await idle(deps)
    # Dropped at the first event it could not take; the other one heard it all.
    assert gone.tried == 1
    assert said(staying.events) == "Owner left you a note."
    assert staying.events[-1] == Ended("complete")
    assert await replies_of(chat_server, alex.id) == [("Owner left you a note.", "complete")]
