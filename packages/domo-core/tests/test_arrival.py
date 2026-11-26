"""Domo may write first when someone opens their page (spec 4.9, 5.9), and knows what went
unanswered (spec 4.10, 5.10).

Opening the chat page after ``ARRIVAL_AFTER`` without it (or for the first time) offers Domo an
arrival turn: a turn of his, in the background, with no message of the person, on the small model of
the kind ``arrival``, offered the person's tools (each act judged as any act of a turn he started),
and told what is new. He may answer nothing at all: nothing is then shown. His words come through
``/chat/since``, as a wake's do. Someone without a key Domo may use for them gets an upkeep turn
instead, when the general key serves for that. A message the person sends while it runs is never
refused: the arrival turn steps aside. A reply that ends in error keeps why, and the person's next
turn is told the moments that went unanswered.

The chat pilots talk with Domo on a live server (see ``chat_support.py``). Arrival turns are off
on the servers of the other test files; they are on here (``arrival_turns_on``). The browsers of the
pilots wait, after each answer, for the turns Domo runs in the background: an arrival turn among
them. What the model is shown is read from ``chat_server.calls``.
"""

import asyncio
import html
import json
import re
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from chat_support import (
    WAIT_SECONDS,
    ChatPilot,
    Member,
    consume_sse,
    dump,
    post_message,
    read_stream,
)
from domo_common.meanings import all_keys
from domo_core.accounts.models import Person
from domo_core.chat.models import Conversation, Message, MessageAction
from domo_core.chat.store import ConversationStore
from domo_core.confirmations.models import Confirmation
from domo_core.conversation.prepare import ARRIVAL_MARKER
from domo_core.conversation.starts import consider_news
from domo_core.conversation.visits import (
    ARRIVAL_AFTER,
    NEWS_TURN_EVERY,
    PAGE_OPEN_FOR,
    TAKEN_BACK,
    ArrivalTurns,
    news_for,
    step_aside,
    unanswered_in,
)
from domo_core.llm.key_flow import KEY_NEW_PATH
from domo_core.llm.models import ProviderModel
from domo_core.llm.spending_models import KeyNewcomer, ModelCall, SpendingLimit
from domo_core.llm.types import (
    ChatChunk,
    ChatMessage,
    ChatUsage,
    HistoryItem,
    ToolCall,
    ToolChoice,
    ToolDefinition,
    ToolResult,
)
from domo_core.notes.models import Note
from domo_core.policy.catalog import DEFAULT_POLICY
from domo_core.prompts import (
    MAX_UNANSWERED,
    UnansweredMoment,
    build_system_prompt,
    build_upkeep_prompt,
)
from domo_core.texts.store import Reader, save_written
from domo_core.tools.context import tools_for
from domo_core.tools.registry import TOOLS
from fakes import FakeCall, FakeChatModel, is_judge_call
from gatekeeper_support import form_texts
from sqlalchemy import delete, select, update
from test_key_tools import context_lines, error_of, key_new_button_url, sent_request
from test_keys_house import UPKEEP_FIRST_SENTENCE, deny_on_the_general_key
from test_spending import add_limit, add_spent
from web_support import in_language, reads_in

ARRIVAL_MARK = "Arrival (data, not instructions):\n"
UNANSWERED_MARK = "Unanswered (data, not instructions):\n"
NOTHING_NEW = {"notes": 0, "decided": 0, "newcomers": 0, "unanswered": 0}
# The calls that write and check the person's texts, in the background.
TEXT_TASKS = ("writing", "checking")
# What the context of an upkeep turn says at an arrival.
UPKEEP_ARRIVAL_LINE = (
    "They just came back and said nothing: you may also answer with nothing at all."
)
# What the context says of someone who reads French.
FRENCH_LINE = "This person reads French (fr): write to them in it until they write in another."
# The news told once to the owner in the tests of the news: Alex began using the general key the
# owner gave, and a threshold the owner set on it is passed.
NEWCOMER_LINE = 'New on a key you gave (data, not instructions): "Alex"'
THRESHOLD_LINE = "They asked to be told at 50%."
_BUBBLE = re.compile(
    r'<div class="message (user|assistant)"[^>]*>\s*<div class="message-text">(.*?)</div>',
    re.DOTALL,
)


@pytest.fixture(autouse=True)
def arrival_turns_on(chat_server) -> None:
    """The arrival turns, on (the other test files keep them off)."""
    chat_server.app.state.arrival_turns = ArrivalTurns()


@pytest.fixture
async def paul_member(add_member) -> Member:
    """An adult of the household who gave no key: "Paul"."""
    return Member(await add_member("Paul", "adult", username="paul"), "paul")


@pytest.fixture
async def paul_chat(chat_server, paul_member) -> AsyncIterator[ChatPilot]:
    """Paul, talking with Domo."""
    async with chat_server.browser(paul_member) as client:
        yield ChatPilot(chat_server, paul_member, client)


def opens_an_arrival(call: FakeCall) -> bool:
    """Whether a call is the first of an arrival turn: the person's turn of the conversation it
    is sent ends with the line of the code that says they just came back (after their
    message that got no answer, if one did not)."""
    last = call.messages[-1]
    return (
        isinstance(last, ChatMessage)
        and last.role == "user"
        and last.content.endswith(ARRIVAL_MARKER)
        and not is_judge_call(call.system)
    )


def arrival_calls(chat: ChatPilot) -> list[FakeCall]:
    """The first call of every arrival turn so far, in order."""
    return [call for call in chat.server.calls if opens_an_arrival(call)]


def block_of(system: str, mark: str) -> Any:
    """The JSON of the context block that starts with ``mark``."""
    return json.loads(system.split(mark)[1].split("\n\n")[0])


def bubbles(fragment: str) -> list[tuple[str, str]]:
    return [(side, html.unescape(text)) for side, text in _BUBBLE.findall(fragment)]


async def open_page(chat: ChatPilot) -> str:
    """The person opens their page; their browser waits for the arrival turn it starts."""
    page = await chat.get("/chat")
    assert page.status_code == 200, page.text
    return page.text


async def messages_of(chat: ChatPilot) -> list[tuple[str, str, str]]:
    """The messages of the person's conversations, oldest first: role, text and status."""
    async with chat.server.session_factory() as reader:
        found = await reader.execute(
            select(Message.role, Message.content, Message.status)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(Conversation.person_id == chat.person.id)
            .order_by(Message.created_at)
        )
        return [tuple(row) for row in found.all()]


async def tasks_of(chat: ChatPilot) -> list[tuple[str, str]]:
    """The calls counted for the person: their kind of task and their model, in order."""
    async with chat.server.session_factory() as reader:
        found = await reader.execute(
            select(ModelCall.task, ModelCall.model_id)
            .where(ModelCall.person_id == chat.person.id)
            .order_by(ModelCall.started_at)
        )
        return [tuple(row) for row in found.all()]


async def failure_of(chat: ChatPilot, message_id: Any) -> str | None:
    async with chat.server.session_factory() as reader:
        return (
            await reader.execute(select(Message.failure).where(Message.id == message_id))
        ).scalar_one()


async def choose_arrival(chat: ChatPilot, arrival: str) -> None:
    """Domo chooses, with his tool, how he takes the moments someone opens their page."""
    reply = await chat.say_with_tool(
        "writing_choose", {"arrival": arrival, "why": "to spend less on what says nothing"}
    )
    assert reply.actions == [{"tool": "writing_choose", "status": "done"}]


# --- the arrival turn ----------------------------------------------------------------------------


async def test_opening_the_page_after_a_while_offers_an_arrival_turn(adult_chat):
    server = adult_chat.server
    adult_chat.use_model(FakeChatModel(["Hello Alex."]))
    # The first visit is an arrival like any other.
    page = await open_page(adult_chat)
    assert bubbles(page) == []
    [first] = arrival_calls(adult_chat)
    assert block_of(first.system, ARRIVAL_MARK) == {"away_minutes": None, "new": NOTHING_NEW}
    # No message of the person: their turn is a line of the code, never shown.
    assert [dump(item) for item in first.messages] == [{"role": "user", "content": ARRIVAL_MARKER}]
    # On the small model, as the kind ``arrival`` starts, counted for the person as such.
    assert server.requested[-1] == (server.provider_id, "example-haiku-1")
    turns = [call for call in await tasks_of(adult_chat) if call[0] not in TEXT_TASKS]
    assert turns == [("arrival", "example-haiku-1")]
    # What he said comes through ``/chat/since``: from the start, for a page without a bubble.
    since = await adult_chat.get("/chat/since")
    assert since.status_code == 200
    assert bubbles(since.text) == [("assistant", "Hello Alex.")]
    assert ARRIVAL_MARKER not in html.unescape(since.text)

    # An hour later, another one, told how long the person was away.
    adult_chat.clock.advance(timedelta(hours=1))
    page = await open_page(adult_chat)
    assert bubbles(page) == [("assistant", "Hello Alex.")]
    [_, second] = arrival_calls(adult_chat)
    assert block_of(second.system, ARRIVAL_MARK) == {"away_minutes": 60, "new": NOTHING_NEW}
    assert ARRIVAL_MARKER not in html.unescape(await open_page(adult_chat))
    # They are there: the turn is never told that nobody listens.
    for call in (first, second):
        assert "Nobody is listening" not in call.system


async def test_what_domo_says_at_an_arrival_is_not_his_answer_to_them(adult_chat):
    # Re-review m4: an arrival answers no message of the person. Its reply is not his own answer:
    # the yes-judge reads it as "relayed" (trial fix 2); his answer to what they then write is.
    adult_chat.use_model(FakeChatModel(["Hello Alex. Shall I rename you?"]))
    await open_page(adult_chat)
    await adult_chat.say("Hello")
    async with adult_chat.server.session_factory() as reader:
        found = await reader.execute(
            select(Message.own_answer)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(Conversation.person_id == adult_chat.person.id, Message.role == "assistant")
            .order_by(Message.created_at)
        )
        assert found.scalars().all() == [False, True]


def test_the_line_of_an_arrival_names_no_channel():  # spec 6.8
    """The person's turn of an arrival is a line of the code, the same whatever they came back
    through (their page, a program on a device): it names none."""
    assert ARRIVAL_MARKER == "[they just came back: see the context]"


async def test_an_arrival_turn_is_told_the_language(adult_chat, session_factory):
    """Scenario 3 of the trial of 0.7.0: in a French house, Domo's first words at an arrival were
    in English. Nobody wrote anything yet: the context says which language the person reads."""
    async with session_factory() as writer:
        await reads_in(writer, adult_chat.person, "fr")
        await writer.commit()
    adult_chat.use_model(FakeChatModel())
    await open_page(adult_chat)
    [call] = arrival_calls(adult_chat)
    assert FRENCH_LINE in context_lines(call.system)


async def test_an_upkeep_turn_is_told_the_language(owner_chat, paul_chat, session_factory):
    await deny_on_the_general_key(owner_chat, "Paul")
    async with session_factory() as writer:
        await reads_in(writer, paul_chat.person, "fr")
        await writer.commit()
    paul_chat.use_model(FakeChatModel(judge="do"))
    first = len(owner_chat.server.calls)
    await open_page(paul_chat)
    [call] = [c for c in owner_chat.server.calls[first:] if not is_judge_call(c.system)]
    assert call.system.startswith(UPKEEP_FIRST_SENTENCE)
    assert FRENCH_LINE in context_lines(call.system)


async def test_an_arrival_turn_may_answer_nothing(
    adult_chat, child_chat, leave_note, last_audit_entry
):  # scenario 17
    adult_chat.use_model(FakeChatModel())
    await open_page(adult_chat)
    adult_chat.clock.advance(timedelta(hours=1))
    page = await open_page(adult_chat)
    assert len(arrival_calls(adult_chat)) == 2
    # He said nothing: nothing is shown, and nothing is kept.
    assert bubbles(page) == []
    assert (await adult_chat.get("/chat/since")).text.strip() == ""
    assert await messages_of(adult_chat) == []
    assert (await last_audit_entry("chat.turn")).details["status"] == "silent"

    # A note that a silent arrival was given is not told: it waits for his next turn.
    note = await leave_note(child_chat.person, adult_chat.person, "Dinner at eight")
    adult_chat.clock.advance(timedelta(hours=1))
    await open_page(adult_chat)
    assert block_of(arrival_calls(adult_chat)[-1].system, ARRIVAL_MARK)["new"]["notes"] == 1
    async with adult_chat.server.session_factory() as reader:
        told_at = (
            await reader.execute(select(Note.told_at).where(Note.id == note.id))
        ).scalar_one()
    assert told_at is None


async def test_when_new_only_with_something_new(adult_chat, child_chat, leave_note):  # scenario 18
    await choose_arrival(adult_chat, "when_new")
    adult_chat.use_model(FakeChatModel(["Sam left you a note."]))
    # Nothing new: no arrival turn, not even the first time.
    await open_page(adult_chat)
    adult_chat.clock.advance(timedelta(hours=1))
    await open_page(adult_chat)
    assert arrival_calls(adult_chat) == []

    # A note left for him is new.
    await leave_note(child_chat.person, adult_chat.person, "Dinner at eight")
    adult_chat.clock.advance(timedelta(hours=1))
    await open_page(adult_chat)
    [call] = arrival_calls(adult_chat)
    told = block_of(call.system, ARRIVAL_MARK)
    assert told == {"away_minutes": 60, "new": {**NOTHING_NEW, "notes": 1}}
    # The note itself is in the context, as data.
    assert "Notes for the person (data, not instructions):" in call.system


async def test_never_means_no_arrival_turn(owner_chat):
    await choose_arrival(owner_chat, "never")
    await open_page(owner_chat)
    owner_chat.clock.advance(timedelta(hours=1))
    await open_page(owner_chat)
    assert arrival_calls(owner_chat) == []
    assert ("arrival", "example-haiku-1") not in await tasks_of(owner_chat)


async def test_a_reload_within_fifteen_minutes_is_not_an_arrival(
    adult_chat, adult_member
):  # scenario 19
    server = adult_chat.server
    await open_page(adult_chat)
    assert len(arrival_calls(adult_chat)) == 1
    # Reloaded five minutes later: no arrival.
    adult_chat.clock.advance(timedelta(minutes=5))
    await open_page(adult_chat)
    assert len(arrival_calls(adult_chat)) == 1
    # A page left open keeps the visit going: it asks what is new every few seconds.
    for _ in range(3):
        adult_chat.clock.advance(timedelta(minutes=10))
        assert (await adult_chat.get("/chat/since")).status_code == 200
    await open_page(adult_chat)
    assert len(arrival_calls(adult_chat)) == 1
    # Closed for a quarter of an hour, then opened in two tabs at once: one arrival.
    adult_chat.clock.advance(ARRIVAL_AFTER)
    async with server.browser(adult_member) as other_tab:
        pages = await asyncio.gather(adult_chat.get("/chat"), other_tab.get("/chat"))
    assert [page.status_code for page in pages] == [200, 200]
    assert len(arrival_calls(adult_chat)) == 2


async def test_an_arrival_may_act_and_is_judged(owner_chat, adult_chat):
    # Part 8 of "Domo judges first": at an arrival he has the person's tools and may act; each
    # act goes through the barrier and the judge as any act of a turn he started.
    leave = ToolCall("a1", "note_leave", json.dumps({"recipient": "Alex", "text": "hi"}))
    model = owner_chat.use_model(FakeChatModel(tool_script=[leave, "Welcome back."], judge="do"))
    await open_page(owner_chat)
    [call] = arrival_calls(owner_chat)
    offered = {tool.name for tool in call.tools}
    # Every tool the person may use, as in any turn of theirs.
    assert offered == {
        tool.name
        for tool in tools_for("owner", TOOLS, DEFAULT_POLICY, person_id=owner_chat.person.id)
    }
    assert "note_leave" in offered
    # Its call reached the judge, told that nobody spoke and why he acts.
    [judged] = model.judge_calls()
    case = json.loads(str(judged.messages[0].content))
    assert case["speaker"] == {"channel": "unasked", "nobody_spoke": True}
    assert (case["idea"]["of"], case["idea"]["reason"]) == ("domo", "they just came back")
    async with owner_chat.server.session_factory() as reader:
        [note] = (await reader.scalars(select(Note))).all()
        assert note.text == "hi"
        assert (await reader.scalars(select(Confirmation))).all() == []


async def test_an_arrival_act_its_judge_does_not_weigh_is_not_done(owner_chat, adult_chat):
    # A judge silent twice on what he does at an arrival: nothing is done, nobody is asked.
    leave = ToolCall("a1", "note_leave", json.dumps({"recipient": "Alex", "text": "hi"}))
    model = owner_chat.use_model(
        FakeChatModel(tool_script=[leave, "Welcome back."], judge="mumble")
    )
    await open_page(owner_chat)
    assert len(model.judge_calls()) == 2
    async with owner_chat.server.session_factory() as reader:
        assert (await reader.execute(select(Note))).all() == []
        assert (await reader.scalars(select(Confirmation))).all() == []
    page = await open_page(owner_chat)
    assert 'data-action-status="refused"' in page


class HeldArrival:
    """A model whose arrival calls wait for ``go`` (``waiting`` is set as soon as one does), and
    which answers any other call "Hi" at once."""

    def __init__(self) -> None:
        self.go = asyncio.Event()
        self.waiting = asyncio.Event()
        self.stopped = False

    async def stream(
        self,
        *,
        system: str,
        messages: Sequence[HistoryItem],
        max_output_tokens: int,
        tools: Sequence[ToolDefinition] = (),
        tool_choice: ToolChoice = "auto",
        think: bool = True,
    ) -> AsyncGenerator[ChatChunk | ToolCall | ChatUsage]:
        last = messages[-1]
        if isinstance(last, ChatMessage) and last.content == ARRIVAL_MARKER:
            self.waiting.set()
            try:
                await self.go.wait()
            except asyncio.CancelledError:
                self.stopped = True
                raise
            yield ChatChunk("Welcome back.")
        else:
            yield ChatChunk("Hi")
        yield ChatUsage(3, 2)


async def test_a_message_sent_during_an_arrival_turn_is_not_refused(chat_server, adult_member):
    held = HeldArrival()
    chat_server.model = held
    async with chat_server.browser(adult_member, waits_for_wakes=False) as browser:
        assert (await browser.get("/chat")).status_code == 200
        async with asyncio.timeout(WAIT_SECONDS):
            await held.waiting.wait()
        # Domo writes his arrival, and the person types at once: never "busy".
        reply_id = await post_message(browser, "hello")
        events = await consume_sse(browser, f"/chat/stream/{reply_id}")
        assert "".join(event.data for event in events if event.event == "delta") == "Hi"
        await chat_server.wait_for_wakes()
    # The arrival turn stopped, and its reply went: the message has its answer.
    assert held.stopped
    pilot = ChatPilot(chat_server, adult_member, browser)
    assert await messages_of(pilot) == [
        ("user", "hello", "complete"),
        ("assistant", "Hi", "complete"),
    ]


class ActsThenHolds:
    """An arrival that first tells a note (``note_told``), then waits for ``go`` (``waiting`` is
    set as soon as it does); any other call is answered "Hi" at once."""

    def __init__(self, note_id: str) -> None:
        self.go = asyncio.Event()
        self.waiting = asyncio.Event()
        self.stopped = False
        self._told = ToolCall("t1", "note_told", json.dumps({"note": note_id}))

    async def stream(
        self,
        *,
        system: str,
        messages: Sequence[HistoryItem],
        max_output_tokens: int,
        tools: Sequence[ToolDefinition] = (),
        tool_choice: ToolChoice = "auto",
        think: bool = True,
    ) -> AsyncGenerator[ChatChunk | ToolCall | ChatUsage]:
        last = messages[-1]
        if isinstance(last, ChatMessage) and last.content == ARRIVAL_MARKER:
            yield self._told
        elif isinstance(last, ToolResult):
            # The arrival's next round, once the note is told.
            self.waiting.set()
            try:
                await self.go.wait()
            except asyncio.CancelledError:
                self.stopped = True
                raise
            yield ChatChunk("Sam left you a note.")
        else:
            yield ChatChunk("Hi")
        yield ChatUsage(3, 2)


async def test_a_message_after_the_arrival_acted_keeps_what_it_did(
    chat_server, adult_member, child_member, leave_note
):
    note = await leave_note(child_member.person, adult_member.person, "Dinner at eight")
    held = ActsThenHolds(str(note.id))
    chat_server.model = held
    async with chat_server.browser(adult_member, waits_for_wakes=False) as browser:
        assert (await browser.get("/chat")).status_code == 200
        async with asyncio.timeout(WAIT_SECONDS):
            await held.waiting.wait()
        reply_id = await post_message(browser, "hello")
        await consume_sse(browser, f"/chat/stream/{reply_id}")
        await chat_server.wait_for_wakes()
        page = (await browser.get("/chat")).text
    # The arrival had told the note: its reply stays, cut short, with its card.
    assert held.stopped
    pilot = ChatPilot(chat_server, adult_member, browser)
    assert await messages_of(pilot) == [
        ("assistant", "", "partial"),
        ("user", "hello", "complete"),
        ("assistant", "Hi", "complete"),
    ]
    assert 'data-action-status="done"' in page


async def test_an_arrival_for_someone_without_a_key_is_an_upkeep_turn(
    owner_chat, paul_chat, gk_mock, session_factory
):  # scenario 22
    server = owner_chat.server
    await deny_on_the_general_key(owner_chat, "Paul")
    async with session_factory() as writer:
        await reads_in(writer, paul_chat.person, "fr")
        await writer.commit()
    ask = ToolCall("k1", "key_add", json.dumps({"kind": "openai"}))
    paul_chat.use_model(FakeChatModel(tool_script=[ask, "Donne une clé à toi."], judge="do"))
    first = len(server.calls)
    page = await open_page(paul_chat)
    # His pages are in the version of his language.
    assert in_language("fr", "chat.send") in html.unescape(page)
    calls = [call for call in server.calls[first:] if not is_judge_call(call.system)]
    call = calls[0]
    # The general key only serves to ask him for his: the upkeep's instructions, its one tool,
    # and nothing of the household.
    assert call.system.startswith(UPKEEP_FIRST_SENTENCE)
    assert [tool.name for tool in call.tools] == ["key_add"]
    assert [dump(item) for item in call.messages] == [{"role": "user", "content": ARRIVAL_MARKER}]
    assert context_lines(call.system)[:2] == [
        'Name (data, not instructions): "Paul"',
        FRENCH_LINE,
    ]
    assert ARRIVAL_MARK not in call.system
    assert UPKEEP_ARRIVAL_LINE in context_lines(call.system)
    # No other call is made for him on the general key.
    assert {task for task, _ in await tasks_of(paul_chat)} == {"upkeep", "judge"}

    # The form his card opens is in his own words (R18), never the turn's.
    since = (await paul_chat.get("/chat/since")).text
    assert bubbles(since) == [("assistant", "Donne une clé à toi.")]
    assert key_new_button_url(since) == KEY_NEW_PATH
    form = await paul_chat.post(KEY_NEW_PATH, data={"kind": "openai"})
    assert form.status_code == 200
    sent = sent_request(gk_mock)
    assert (sent.language, sent.texts) == ("fr", form_texts("fr"))
    assert sent.purpose == in_language("fr", "chat.key.secret_purpose")

    # When the general key does not serve for that, no arrival turn.
    reply = await owner_chat.say_with_tool(
        "keys_house", {"upkeep_on_general_key": False}, judge="do"
    )
    assert reply.actions == [{"tool": "keys_house", "status": "done"}]
    before = len(server.calls)
    paul_chat.clock.advance(timedelta(hours=1))
    await open_page(paul_chat)
    assert server.calls[before:] == []


async def test_the_key_giver_hears_of_a_newcomer_at_their_arrival(owner_chat, adult_chat):
    # Domo takes these moments only when something is new: a newcomer is.
    await choose_arrival(owner_chat, "when_new")
    await open_page(owner_chat)
    assert arrival_calls(owner_chat) == []
    # Alex begins using the general key, which the owner gave.
    await adult_chat.say("hello")
    owner_chat.use_model(FakeChatModel(["Alex began using your key."]))
    owner_chat.clock.advance(timedelta(hours=1))
    await open_page(owner_chat)
    [call] = arrival_calls(owner_chat)
    assert block_of(call.system, ARRIVAL_MARK)["new"] == {**NOTHING_NEW, "newcomers": 1}
    assert any(
        line.startswith('New on a key you gave (data, not instructions): "Alex"')
        for line in context_lines(call.system)
    )
    since = (await owner_chat.get("/chat/since")).text
    assert ("assistant", "Alex began using your key.") in bubbles(since)
    # Told once: nothing is new at the next visit, and no arrival turn is offered.
    owner_chat.clock.advance(timedelta(hours=1))
    await open_page(owner_chat)
    assert len(arrival_calls(owner_chat)) == 1


async def test_the_welcome_block_is_gone(adult_chat):
    adult_chat.use_model(FakeChatModel())
    page = await open_page(adult_chat)
    assert 'class="message welcome"' not in page
    assert "chat.welcome" not in all_keys()
    # The first visit is an arrival like any other.
    assert len(arrival_calls(adult_chat)) == 1


async def test_the_upkeep_prompt_names_only_the_tools_given(owner_chat, paul_chat, grant):
    instructions = build_upkeep_prompt(display_name="Paul", language="en")
    assert "key_add" not in instructions
    assert "the tool you are given" in instructions
    # A rule takes key_add away from Paul: his upkeep turn is given no tool, and its instructions
    # name none.
    server = owner_chat.server
    await deny_on_the_general_key(owner_chat, "Paul")
    await grant("key.add", person=paul_chat.person, access="deny")
    paul_chat.use_model(FakeChatModel(["Give a key of your own."]))
    first = len(server.calls)
    answer = await paul_chat.say("hello")
    [call] = server.calls[first:]
    assert call.system.startswith(UPKEEP_FIRST_SENTENCE)
    assert call.tools == ()
    assert "key_add" not in call.system
    assert answer.status == "complete"


# --- the moments without an answer ---------------------------------------------------------------


@pytest.mark.parametrize("kind", ["unreachable", "rate_limited", "auth"])
async def test_a_failed_reply_keeps_why(adult_chat, kind):
    adult_chat.use_model(FakeChatModel(["x"], fail_after=0, error_kind=kind))
    failed = await adult_chat.say("are you there?")
    assert failed.status == "error"
    assert await failure_of(adult_chat, failed.message_id) == kind
    adult_chat.use_model(FakeChatModel(["Here."]))
    answered = await adult_chat.say("hello?")
    assert answered.status == "complete"
    assert await failure_of(adult_chat, answered.message_id) is None


async def test_the_next_turn_is_told_what_went_unanswered(adult_chat):  # scenario 20
    server = adult_chat.server
    adult_chat.use_model(FakeChatModel(["x"], fail_after=0, error_kind="unreachable"))
    failed = await adult_chat.say("are you there?")
    async with server.session_factory() as reader:
        at = (
            await reader.execute(select(Message.created_at).where(Message.id == failed.message_id))
        ).scalar_one()

    adult_chat.use_model(FakeChatModel(["Back now."]))
    await adult_chat.say("hello?")
    call = server.calls[-1]
    assert block_of(call.system, UNANSWERED_MARK) == [
        {"at": at.astimezone(UTC).isoformat(), "why": "unreachable"}
    ]
    # The message that got no answer is in the conversation, before the new one.
    assert [dump(item) for item in call.messages] == [
        {"role": "user", "content": "are you there?\n\nhello?"}
    ]
    # Once he answered, the next turn is told nothing of it.
    await adult_chat.say("thanks")
    assert UNANSWERED_MARK not in server.calls[-1].system

    # An arrival is told too, and counts it as new.
    adult_chat.use_model(FakeChatModel(["x"], fail_after=0, error_kind="upstream"))
    await adult_chat.say("one more")
    adult_chat.use_model(FakeChatModel())
    await open_page(adult_chat)
    [arrival] = arrival_calls(adult_chat)
    assert block_of(arrival.system, ARRIVAL_MARK)["new"] == {**NOTHING_NEW, "unanswered": 1}
    assert [moment["why"] for moment in block_of(arrival.system, UNANSWERED_MARK)] == ["upstream"]


async def test_a_limit_reached_shows_the_written_text_without_any_call(
    owner_chat, adult_chat, session_factory
):  # scenario 21
    server = owner_chat.server
    reply = await owner_chat.say_with_tool(
        "keys_house",
        {"person": "Alex", "limit_amount": 0.01, "limit_currency": "USD"},
        judge="do",
    )
    assert reply.actions == [{"tool": "keys_house", "status": "done"}]
    # Domo wrote Alex's own text for it, ahead.
    async with session_factory() as writer:
        await save_written(
            writer,
            Reader(adult_chat.person.id, "en"),
            {"chat.error.limit_reached": "Alex, this month's money is spent."},
            model_id="m",
            now=adult_chat.clock(),
        )
        await writer.commit()
    await adult_chat.texts_idle()
    calls, written = len(server.calls), len(server.text_model.writer_calls())

    answer = await adult_chat.say("hello")
    assert answer.status == "error"
    assert await failure_of(adult_chat, answer.message_id) == "limit_reached"
    assert "Alex, this month's money is spent." in html.unescape(error_of(answer))
    # No call: neither of the conversation, nor of the writer on that key.
    assert len(server.calls) == calls
    await adult_chat.texts_idle()
    assert len(server.text_model.writer_calls()) == written


# --- an arrival stopped after its first words (fix round 1) --------------------------------------


class SpeaksThenHolds:
    """A model whose arrival calls say their first words, then wait for ``go`` (``waiting`` is
    set once the words are out); any other call is answered "Hi" at once."""

    def __init__(self) -> None:
        self.go = asyncio.Event()
        self.waiting = asyncio.Event()
        self.stopped = False

    async def stream(
        self,
        *,
        system: str,
        messages: Sequence[HistoryItem],
        max_output_tokens: int,
        tools: Sequence[ToolDefinition] = (),
        tool_choice: ToolChoice = "auto",
        think: bool = True,
    ) -> AsyncGenerator[ChatChunk | ToolCall | ChatUsage]:
        last = messages[-1]
        if isinstance(last, ChatMessage) and last.content.endswith(ARRIVAL_MARKER):
            yield ChatChunk("Welcome back, ")
            self.waiting.set()
            try:
                await self.go.wait()
            except asyncio.CancelledError:
                self.stopped = True
                raise
            yield ChatChunk("Alex.")
        else:
            yield ChatChunk("Hi")
        yield ChatUsage(3, 2)


async def streaming_reply_of(chat: ChatPilot) -> Any:
    """The id of the reply being written in the person's conversation."""
    async with chat.server.session_factory() as reader:
        found = await reader.execute(
            select(Message.id)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(Conversation.person_id == chat.person.id, Message.status == "streaming")
        )
        return found.scalar_one()


async def test_an_arrival_stopped_after_its_first_words_leaves_nothing(chat_server, adult_chat):
    held = SpeaksThenHolds()
    chat_server.model = held
    async with chat_server.browser(adult_chat.member, waits_for_wakes=False) as browser:
        assert (await browser.get("/chat")).status_code == 200
        async with asyncio.timeout(WAIT_SECONDS):
            await held.waiting.wait()
        # Stopped as a message of the person stops it: its first words go with it.
        assert chat_server.app.state.arrival_turns.stop(await streaming_reply_of(adult_chat))
        await chat_server.wait_for_wakes()
    assert held.stopped
    assert await messages_of(adult_chat) == []


async def test_a_yes_typed_while_the_arrival_speaks_answers_the_card_above(
    chat_server, adult_chat, confirmation_status
):  # R74 holds at an arrival
    asked = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    request = asked.pending_confirmation_id
    assert await confirmation_status(request) == "pending"
    held = SpeaksThenHolds()
    chat_server.model = held
    adult_chat.clock.advance(timedelta(hours=1))
    async with chat_server.browser(adult_chat.member, waits_for_wakes=False) as browser:
        assert (await browser.get("/chat")).status_code == 200
        async with asyncio.timeout(WAIT_SECONDS):
            await held.waiting.wait()
        # Domo has begun to write first; the person answers the card above at once.
        reply_id = await post_message(browser, "oui")
        await consume_sse(browser, f"/chat/stream/{reply_id}")
        await chat_server.wait_for_wakes()
    assert held.stopped
    assert await confirmation_status(request) == "executed"
    # No arrival bubble is left between the card and the yes.
    shown = [(role, status) for role, _, status in await messages_of(adult_chat)]
    assert shown == [
        ("user", "complete"),
        ("assistant", "complete"),
        ("user", "complete"),
        ("assistant", "complete"),
    ]


async def test_an_arrival_stopped_after_showing_a_request_again_ends_and_leaves_the_chat_free(
    chat_server, adult_chat, confirmation_status
):  # final review F3
    asked = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    request = asked.pending_confirmation_id
    held = SpeaksThenHolds()
    chat_server.model = held
    adult_chat.clock.advance(timedelta(hours=1))
    async with chat_server.browser(adult_chat.member, waits_for_wakes=False) as browser:
        assert (await browser.get("/chat")).status_code == 200
        async with asyncio.timeout(WAIT_SECONDS):
            await held.waiting.wait()
        arrival = await streaming_reply_of(adult_chat)
        # The arrival showed the request again, and its card is not stored yet: the request
        # names its reply.
        async with chat_server.session_factory() as writer:
            await writer.execute(
                update(Confirmation)
                .where(Confirmation.id == request)
                .values(last_shown_message_id=arrival)
            )
            await writer.commit()
        # Stopped (as the core stops it): what it said stays, a request names it, and it ends as
        # it stands, never left "streaming".
        assert chat_server.app.state.arrival_turns.stop(arrival)
        await chat_server.wait_for_wakes()
        assert held.stopped
        assert (await messages_of(adult_chat))[-1] == ("assistant", "Welcome back, ", "partial")
        # The person's next message is answered: the chat is not left busy.
        reply_id = await post_message(browser, "hello")
        events = await consume_sse(browser, f"/chat/stream/{reply_id}")
        assert "".join(event.data for event in events if event.event == "delta") == "Hi"
        await chat_server.wait_for_wakes()
    assert (await messages_of(adult_chat))[-2:] == [
        ("user", "hello", "complete"),
        ("assistant", "Hi", "complete"),
    ]
    assert await confirmation_status(request) == "pending"


# --- the news told once wait for an arrival that says them (fix round 1) ------------------------


async def news_for_the_owner(owner_chat: ChatPilot, adult_member: Member) -> None:
    """Alex began using the general key, which the owner gave; and the owner's limit on it has
    passed the threshold they asked to be told at."""
    server = owner_chat.server
    await add_limit(
        server.session_factory,
        server.provider_id,
        amount="10",
        set_by=owner_chat.person.id,
        warn_at=50,
    )
    await add_spent(
        server.session_factory,
        server.provider_id,
        "6",
        person_id=adult_member.person.id,
        at=owner_chat.clock(),
    )


@pytest.mark.parametrize("arrival", ["silent", "failed", "stopped"])
async def test_news_told_once_wait_for_an_arrival_that_says_them(
    chat_server, owner_chat, adult_member, arrival
):
    await news_for_the_owner(owner_chat, adult_member)
    if arrival == "stopped":
        held = HeldArrival()
        chat_server.model = held
        async with chat_server.browser(owner_chat.member, waits_for_wakes=False) as browser:
            assert (await browser.get("/chat")).status_code == 200
            async with asyncio.timeout(WAIT_SECONDS):
                await held.waiting.wait()
            [call] = arrival_calls(owner_chat)
            reply_id = await post_message(browser, "hello")
            await consume_sse(browser, f"/chat/stream/{reply_id}")
            await chat_server.wait_for_wakes()
        told = chat_server.calls[-1].system
    else:
        silent = FakeChatModel()
        failing = FakeChatModel(["x"], fail_after=0, error_kind="upstream")
        owner_chat.use_model(silent if arrival == "silent" else failing)
        await open_page(owner_chat)
        [call] = arrival_calls(owner_chat)
        owner_chat.use_model(FakeChatModel(["ok"]))
        await owner_chat.say("hello")
        told = chat_server.calls[-1].system
    # The arrival was told the news, but said nothing of them: the next turn is told them.
    assert NEWCOMER_LINE in call.system and THRESHOLD_LINE in call.system
    assert NEWCOMER_LINE in told and THRESHOLD_LINE in told


async def test_an_arrival_that_speaks_tells_the_news_once(owner_chat, adult_member):
    await news_for_the_owner(owner_chat, adult_member)
    owner_chat.use_model(FakeChatModel(["Alex began using your key; half the money is spent."]))
    await open_page(owner_chat)
    [call] = arrival_calls(owner_chat)
    assert NEWCOMER_LINE in call.system and THRESHOLD_LINE in call.system
    owner_chat.use_model(FakeChatModel(["ok"]))
    await owner_chat.say("hello")
    told = owner_chat.server.calls[-1].system
    assert NEWCOMER_LINE not in told and THRESHOLD_LINE not in told
    async with owner_chat.server.session_factory() as reader:
        newcomers = (
            await reader.execute(
                select(KeyNewcomer.person_id).where(
                    KeyNewcomer.provider_id == owner_chat.server.provider_id
                )
            )
        ).scalars()
        marked = list(newcomers)
        warned = (await reader.execute(select(SpendingLimit.warned_at))).scalar_one()
    assert marked == [adult_member.person.id]
    assert warned is not None


# --- someone without a key when the general key does not serve for that (R46) -------------------


def notice_of(page: str) -> str:
    """The notice area of the chat page, as it comes."""
    return page.split('id="chat-notice"', 1)[1].split("</div>", 1)[0]


async def test_without_a_key_nor_the_upkeep_the_page_says_so_at_once(
    owner_chat, paul_chat, session_factory, grant
):  # spec 4.11
    server = owner_chat.server
    reply = await owner_chat.say_with_tool(
        "keys_house",
        {"person": "Paul", "on_general_key": "deny", "upkeep_on_general_key": False},
        judge="do",
    )
    assert reply.actions == [{"tool": "keys_house", "status": "done"}]
    async with session_factory() as writer:
        await reads_in(writer, paul_chat.person, "fr")
        await writer.commit()
    before = len(server.calls)
    page = await open_page(paul_chat)
    # No arrival turn; the notice in the version of his language, with the button of the form of
    # a key of his own, which he may open.
    assert server.calls[before:] == []
    assert in_language("fr", "chat.error.no_key") in html.unescape(notice_of(page))
    assert key_new_button_url(notice_of(page)) == KEY_NEW_PATH
    # A rule takes the form away from him: the notice alone.
    await grant("key.add", person=paul_chat.person, access="deny")
    paul_chat.clock.advance(timedelta(hours=1))
    page = await open_page(paul_chat)
    assert in_language("fr", "chat.error.no_key") in html.unescape(notice_of(page))
    assert key_new_button_url(notice_of(page)) is None
    assert server.calls[before:] == []


async def test_someone_with_a_key_has_no_notice_at_load(adult_chat):
    adult_chat.use_model(FakeChatModel())
    page = await open_page(adult_chat)
    assert notice_of(page).split(">", 1)[1].strip() == ""


# --- what an arrival counts, and what went unanswered (fix round 1) -----------------------------


async def test_the_arrival_counts_the_requests_decided_while_away(
    child_chat, live_adult_client, an_adult_agrees_to_a_childs_rename, chat_server
):
    child_chat.use_model(FakeChatModel())
    await open_page(child_chat)
    child_chat.clock.advance(timedelta(minutes=1))
    asked = await child_chat.say_with_tool("profile_rename", {"name": "Sam B"})
    # An adult says yes while Sam is away.
    answer = await live_adult_client.post(
        f"/confirmations/{asked.pending_confirmation_id}/approve", headers={"hx-request": "true"}
    )
    assert answer.status_code == 200
    await chat_server.wait_for_wakes()
    child_chat.use_model(FakeChatModel())
    child_chat.clock.advance(timedelta(hours=1))
    await open_page(child_chat)
    [_, call] = arrival_calls(child_chat)
    assert block_of(call.system, ARRIVAL_MARK)["new"]["decided"] == 1


async def test_a_reply_a_request_names_stays_when_the_arrival_steps_aside(
    adult_member, session_factory
):
    async with session_factory() as writer:
        store = ConversationStore(writer)
        conversation = await store.active_conversation(adult_member.person.id)
        reply = await store.add_message(
            conversation.id, role="assistant", content="", status="streaming", provenance="model"
        )
        # The arrival showed a request again: the request names its reply.
        writer.add(
            Confirmation(
                requester_id=adult_member.person.id,
                approver_kind="self",
                conversation_id=conversation.id,
                last_shown_message_id=reply.id,
                tool_name="profile_rename",
                arguments={},
                action={},
                status="pending",
            )
        )
        await writer.commit()
    async with session_factory() as session:
        await step_aside(session, reply.id)
        await session.commit()
    async with session_factory() as reader:
        kept = (
            await reader.execute(select(Message.status).where(Message.id == reply.id))
        ).scalar_one()
    assert kept == "partial"


async def test_no_key_and_no_model_are_kept_as_why(owner_chat, adult_chat, session_factory):
    key = owner_chat.server.provider_id
    # No model on any key Alex may use: the fixed message, kept as why.
    async with session_factory() as writer:
        rows = await writer.execute(
            select(ProviderModel.model_id).where(ProviderModel.provider_id == key)
        )
        models = list(rows.scalars())
        await writer.execute(delete(ProviderModel).where(ProviderModel.provider_id == key))
        await writer.commit()
    answer = await adult_chat.say("hello")
    assert answer.status == "error"
    assert await failure_of(adult_chat, answer.message_id) == "no_model"
    async with session_factory() as writer:
        writer.add_all(ProviderModel(provider_id=key, model_id=model) for model in models)
        await writer.commit()
    # A message posted while Alex had a key, whose turn finds none any more: no_key.
    reply_id = await post_message(adult_chat.client, "are you there?")
    ruled = await owner_chat.say_with_tool(
        "keys_house",
        {"person": "Alex", "on_general_key": "deny", "upkeep_on_general_key": False},
        judge="do",
    )
    assert ruled.actions == [{"tool": "keys_house", "status": "done"}]
    status, _ = await read_stream(adult_chat.client, f"/chat/stream/{reply_id}")
    assert status == 409
    assert await failure_of(adult_chat, reply_id) == "no_key"


def a_reply(status: str, failure: str | None = None, *, minute: int) -> Message:
    return Message(
        role="assistant",
        content="",
        status=status,
        failure=failure,
        created_at=datetime(2030, 1, 1, 12, minute, tzinfo=UTC),
    )


def test_at_most_the_latest_moments_are_told():
    first = [a_reply("error", "upstream", minute=0), a_reply("complete", minute=1)]
    failed = [a_reply("error", "unreachable", minute=2 + n) for n in range(MAX_UNANSWERED + 2)]
    moments = unanswered_in([*first, *failed])
    assert len(moments) == MAX_UNANSWERED == 10
    assert moments[0].at == datetime(2030, 1, 1, 12, 4, tzinfo=UTC).isoformat()
    assert moments[-1].at == datetime(2030, 1, 1, 12, 13, tzinfo=UTC).isoformat()
    many = [UnansweredMoment(at=str(n), why="upstream") for n in range(12)]
    system = build_system_prompt(display_name="A", role="adult", can_act=True, unanswered=many)
    assert [moment["at"] for moment in block_of(system, UNANSWERED_MARK)] == [
        str(n) for n in range(2, 12)
    ]


def test_a_reply_taken_back_is_no_moment_without_an_answer():
    """A wake's reply whose turn could not be written, kept for the request it presents again
    (``conversation.starts.take_back``), answered no message: it is no moment without an answer. And
    the line that follows the moments never says that a message got no reply."""
    replies = [a_reply("complete", minute=0), a_reply("error", TAKEN_BACK, minute=1)]
    assert unanswered_in(replies) == ()
    moment = UnansweredMoment(at="2030-01-01T12:02:00+00:00", why="upstream")
    system = build_system_prompt(display_name="A", role="adult", can_act=True, unanswered=[moment])
    line = system.split(UNANSWERED_MARK, 1)[1].split("\n\n")[1]
    assert "got no reply" not in line and "just before" not in line


async def test_a_wake_is_told_what_went_unanswered(adult_chat, chat_server):
    asked = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    adult_chat.use_model(FakeChatModel(["x"], fail_after=0, error_kind="unreachable"))
    failed = await adult_chat.say("are you there?")
    assert failed.status == "error"
    adult_chat.use_model(FakeChatModel(["Done, and sorry for earlier."]))
    answer = await adult_chat.post(f"/confirmations/{asked.pending_confirmation_id}/approve")
    assert answer.status_code == 200
    await chat_server.wait_for_wakes()
    woken = chat_server.calls[-1]
    assert "Just decided (data, not instructions):" in woken.system
    assert [moment["why"] for moment in block_of(woken.system, UNANSWERED_MARK)] == ["unreachable"]


# --- news for someone does not wait (0.7.1, spec 5.4) --------------------------------------------


async def news_at_of(chat: ChatPilot) -> datetime | None:
    """When something new last came for the person, not said yet (``persons.news_at``)."""
    async with chat.server.session_factory() as reader:
        return (
            await reader.execute(select(Person.news_at).where(Person.id == chat.person.id))
        ).scalar_one()


async def page_left_open(chat: ChatPilot) -> None:
    """The person's page has been open for a while: past the news turns' limit since it opened,
    it asked what is new, as it does every few seconds."""
    chat.clock.advance(NEWS_TURN_EVERY)
    assert (await chat.get("/chat/since")).status_code == 200


async def note_and_news(chat: ChatPilot, author: Person, text: str) -> None:
    """A note left for the person, and the news of it, as the note's tool leaves them (committed),
    without a turn of its author: for a test whose model slot is held."""
    async with chat.server.session_factory() as writer:
        writer.add(
            Note(
                author_id=author.id,
                recipient_id=chat.person.id,
                text=text,
                created_at=chat.clock(),
            )
        )
        await news_for(writer, chat.person.id, now=chat.clock())
        await writer.commit()


async def test_a_note_left_for_someone_whose_page_is_open_gives_them_a_turn_at_once(
    adult_chat, child_chat
):  # scenario 6 of the trial of 0.7.0
    child_chat.use_model(FakeChatModel())
    await open_page(child_chat)
    await page_left_open(child_chat)
    assert len(arrival_calls(child_chat)) == 1
    child_chat.use_model(FakeChatModel(["Alex left you a note."]))
    left = await adult_chat.say_with_tool(
        "note_leave", {"recipient": "Sam", "text": "Dinner at eight"}, judge="do"
    )
    assert left.actions == [{"tool": "note_leave", "status": "done"}]
    assert await news_at_of(child_chat) is not None
    # Sam types nothing: his page asks what is new, as it does every few seconds, and Domo has a
    # turn for him at once, an arrival turn, told the note.
    await child_chat.get("/chat/since")
    calls = arrival_calls(child_chat)
    assert len(calls) == 2
    assert block_of(calls[-1].system, ARRIVAL_MARK) == {
        "away_minutes": 0,
        "new": {**NOTHING_NEW, "notes": 1},
    }
    assert "Notes for the person (data, not instructions):" in calls[-1].system
    # What he said comes to the page at its next ask, and the news are said.
    since = await child_chat.get("/chat/since")
    assert bubbles(since.text) == [("assistant", "Alex left you a note.")]
    assert await news_at_of(child_chat) is None
    child_chat.clock.advance(NEWS_TURN_EVERY)
    await child_chat.get("/chat/since")
    assert len(arrival_calls(child_chat)) == 2


async def test_a_note_left_for_someone_away_is_told_at_their_next_page_even_within_fifteen_minutes(
    adult_chat, child_chat
):  # scenario 7 of the trial of 0.7.0
    child_chat.use_model(FakeChatModel())
    await open_page(child_chat)
    # Sam closed his page; two minutes later, Alex leaves him a note: no page of his asks.
    child_chat.clock.advance(timedelta(minutes=2))
    await adult_chat.say_with_tool(
        "note_leave", {"recipient": "Sam", "text": "Dinner at eight"}, judge="do"
    )
    assert len(arrival_calls(child_chat)) == 1
    # Opening his page gives the turn, well within ``ARRIVAL_AFTER``.
    assert PAGE_OPEN_FOR < timedelta(minutes=2) < ARRIVAL_AFTER
    child_chat.use_model(FakeChatModel(["Alex left you a note."]))
    page = await open_page(child_chat)
    calls = arrival_calls(child_chat)
    assert len(calls) == 2
    assert block_of(calls[-1].system, ARRIVAL_MARK) == {
        "away_minutes": 2,
        "new": {**NOTHING_NEW, "notes": 1},
    }
    assert bubbles(page) == []
    since = await child_chat.get("/chat/since")
    assert bubbles(since.text) == [("assistant", "Alex left you a note.")]
    assert await news_at_of(child_chat) is None
    # Said: a reload is no arrival again.
    child_chat.clock.advance(timedelta(minutes=2))
    await open_page(child_chat)
    assert len(arrival_calls(child_chat)) == 2


class HeldReply:
    """A model whose calls for a message of the person wait for ``go`` (``waiting`` is set as soon
    as one does), and which answers any arrival turn "You have a note." at once."""

    def __init__(self) -> None:
        self.go = asyncio.Event()
        self.waiting = asyncio.Event()
        self.stopped = False

    async def stream(
        self,
        *,
        system: str,
        messages: Sequence[HistoryItem],
        max_output_tokens: int,
        tools: Sequence[ToolDefinition] = (),
        tool_choice: ToolChoice = "auto",
        think: bool = True,
    ) -> AsyncGenerator[ChatChunk | ToolCall | ChatUsage]:
        last = messages[-1]
        if isinstance(last, ChatMessage) and last.content.endswith(ARRIVAL_MARKER):
            yield ChatChunk("You have a note.")
        else:
            self.waiting.set()
            try:
                await self.go.wait()
            except asyncio.CancelledError:
                self.stopped = True
                raise
            yield ChatChunk("Hi")
        yield ChatUsage(3, 2)


async def test_news_never_stops_a_reply_under_way(
    chat_server, child_member, adult_member
):  # review focus 3
    held = HeldReply()
    chat_server.model = held
    async with chat_server.browser(child_member, waits_for_wakes=False) as browser:
        sam = ChatPilot(chat_server, child_member, browser)
        assert (await browser.get("/chat")).status_code == 200
        await chat_server.wait_for_wakes()
        await page_left_open(sam)
        assert len(arrival_calls(sam)) == 1
        # Sam writes; while Domo answers him, a note comes for him.
        reply_id = await post_message(browser, "hello")
        reading = asyncio.create_task(consume_sse(browser, f"/chat/stream/{reply_id}"))
        async with asyncio.timeout(WAIT_SECONDS):
            await held.waiting.wait()
        await note_and_news(sam, adult_member.person, "Dinner at eight")
        assert (await browser.get("/chat/since")).status_code == 200
        # The reply under way goes on: no turn is offered meanwhile, and the news wait.
        assert len(arrival_calls(sam)) == 1
        assert not held.stopped
        assert await news_at_of(sam) is not None
        held.go.set()
        events = await reading
        assert "".join(event.data for event in events if event.event == "delta") == "Hi"
        await chat_server.wait_for_wakes()
        # Once it is over, the page's next ask gives the news turn.
        assert (await browser.get("/chat/since")).status_code == 200
        await chat_server.wait_for_wakes()
    assert not held.stopped
    calls = arrival_calls(sam)
    assert len(calls) == 2
    assert block_of(calls[-1].system, ARRIVAL_MARK)["new"]["notes"] == 1
    assert [(role, text) for role, text, _ in await messages_of(sam)] == [
        ("assistant", "You have a note."),
        ("user", "hello"),
        ("assistant", "Hi"),
        ("assistant", "You have a note."),
    ]
    assert await news_at_of(sam) is None


async def test_at_most_one_news_turn_a_minute(child_chat, adult_member):
    child_chat.use_model(FakeChatModel())
    await open_page(child_chat)
    await page_left_open(child_chat)
    await note_and_news(child_chat, adult_member.person, "Dinner at eight")
    await child_chat.get("/chat/since")
    assert len(arrival_calls(child_chat)) == 2
    # Another note, right after: its turn waits for the minute to pass, whatever the page asks.
    child_chat.clock.advance(timedelta(seconds=1))
    await note_and_news(child_chat, adult_member.person, "And bread")
    for _ in range(3):
        child_chat.clock.advance(timedelta(seconds=19))
        await child_chat.get("/chat/since")
    assert len(arrival_calls(child_chat)) == 2
    child_chat.clock.advance(timedelta(seconds=2))
    await child_chat.get("/chat/since")
    calls = arrival_calls(child_chat)
    assert len(calls) == 3
    assert block_of(calls[-1].system, ARRIVAL_MARK)["new"]["notes"] == 2


async def test_never_means_no_news_turn(owner_chat, adult_chat):
    await choose_arrival(owner_chat, "never")
    await open_page(owner_chat)
    await page_left_open(owner_chat)
    await adult_chat.say_with_tool(
        "note_leave", {"recipient": "Owner", "text": "Bread bought"}, judge="do"
    )
    await owner_chat.get("/chat/since")
    owner_chat.clock.advance(timedelta(minutes=2))
    await open_page(owner_chat)
    assert arrival_calls(owner_chat) == []
    assert ("arrival", "example-haiku-1") not in await tasks_of(owner_chat)


async def test_a_decided_request_is_news_for_the_one_who_asked(
    child_chat, adult_chat, live_adult_client, an_adult_agrees_to_a_childs_rename, chat_server
):
    child_chat.use_model(FakeChatModel())
    await open_page(child_chat)
    asked = await child_chat.say_with_tool("profile_rename", {"name": "Sam B"})
    # Sam closes his page; an adult says yes. The wake that follows in his conversation says
    # nothing: the news wait for him.
    child_chat.clock.advance(timedelta(minutes=2))
    answer = await live_adult_client.post(
        f"/confirmations/{asked.pending_confirmation_id}/approve", headers={"hx-request": "true"}
    )
    assert answer.status_code == 200
    await chat_server.wait_for_wakes()
    assert await news_at_of(child_chat) is not None
    child_chat.clock.advance(timedelta(minutes=2))
    await open_page(child_chat)
    calls = arrival_calls(child_chat)
    assert len(calls) == 2
    assert block_of(calls[-1].system, ARRIVAL_MARK)["new"]["decided"] == 1
    # One's own yes is no news.
    mine = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    answer = await adult_chat.post(f"/confirmations/{mine.pending_confirmation_id}/approve")
    assert answer.status_code == 200
    await chat_server.wait_for_wakes()
    assert await news_at_of(adult_chat) is None


async def test_a_silent_news_turn_keeps_the_news(child_chat, adult_member):
    child_chat.use_model(FakeChatModel())
    await open_page(child_chat)
    await page_left_open(child_chat)
    await note_and_news(child_chat, adult_member.person, "Dinner at eight")
    await child_chat.get("/chat/since")
    assert len(arrival_calls(child_chat)) == 2
    # He said nothing: the news stay, for the next turn that speaks; but this news is not offered
    # again while the page stays open.
    assert await news_at_of(child_chat) is not None
    for _ in range(4):
        child_chat.clock.advance(timedelta(seconds=30))
        await child_chat.get("/chat/since")
    assert len(arrival_calls(child_chat)) == 2
    # The next opening of his page, within a quarter of an hour, gives one turn for them...
    child_chat.clock.advance(timedelta(minutes=2))
    await open_page(child_chat)
    assert len(arrival_calls(child_chat)) == 3
    assert block_of(arrival_calls(child_chat)[-1].system, ARRIVAL_MARK)["new"]["notes"] == 1
    # ...and once Domo chose silence there too, a reload does nothing.
    child_chat.clock.advance(timedelta(minutes=2))
    await open_page(child_chat)
    assert len(arrival_calls(child_chat)) == 3
    child_chat.use_model(FakeChatModel(["Alex left you a note."]))
    await child_chat.say("hello")
    assert await news_at_of(child_chat) is None


def failing_model() -> FakeChatModel:
    """A model that cannot be reached."""
    return FakeChatModel(["x"], fail_after=0, error_kind="upstream")


async def test_a_news_turn_that_failed_is_offered_again_after_a_minute(child_chat, adult_member):
    child_chat.use_model(FakeChatModel())
    await open_page(child_chat)
    await page_left_open(child_chat)
    await note_and_news(child_chat, adult_member.person, "Dinner at eight")
    child_chat.use_model(failing_model())
    await child_chat.get("/chat/since")
    assert len(arrival_calls(child_chat)) == 2
    assert await news_at_of(child_chat) is not None
    # The model answers again: the page's asks try again, once the minute passed.
    child_chat.use_model(FakeChatModel(["Alex left you a note."]))
    child_chat.clock.advance(timedelta(seconds=30))
    await child_chat.get("/chat/since")
    assert len(arrival_calls(child_chat)) == 2
    child_chat.clock.advance(timedelta(seconds=31))
    await child_chat.get("/chat/since")
    assert len(arrival_calls(child_chat)) == 3
    since = await child_chat.get("/chat/since")
    assert bubbles(since.text) == [("assistant", "Alex left you a note.")]
    assert await news_at_of(child_chat) is None


async def test_a_news_turn_that_failed_leaves_the_news_to_the_next_page(child_chat, adult_member):
    child_chat.use_model(FakeChatModel())
    await open_page(child_chat)
    await page_left_open(child_chat)
    await note_and_news(child_chat, adult_member.person, "Dinner at eight")
    child_chat.use_model(failing_model())
    await child_chat.get("/chat/since")
    assert len(arrival_calls(child_chat)) == 2
    # Sam closes his page and comes back two minutes later: the news are told then.
    child_chat.clock.advance(timedelta(minutes=2))
    child_chat.use_model(FakeChatModel(["Alex left you a note."]))
    await open_page(child_chat)
    calls = arrival_calls(child_chat)
    assert len(calls) == 3
    assert block_of(calls[-1].system, ARRIVAL_MARK)["new"]["notes"] == 1
    assert await news_at_of(child_chat) is None


async def test_a_news_turn_the_person_s_message_erased_leaves_the_news_to_their_turn(
    chat_server, child_member, adult_member
):
    chat_server.model = FakeChatModel()
    async with chat_server.browser(child_member, waits_for_wakes=False) as browser:
        sam = ChatPilot(chat_server, child_member, browser)
        assert (await browser.get("/chat")).status_code == 200
        await chat_server.wait_for_wakes()
        await page_left_open(sam)
        await chat_server.wait_for_wakes()
        held = HeldArrival()
        chat_server.model = held
        await note_and_news(sam, adult_member.person, "Dinner at eight")
        assert (await browser.get("/chat/since")).status_code == 200
        async with asyncio.timeout(WAIT_SECONDS):
            await held.waiting.wait()
        # Sam types as Domo begins: the news turn steps aside, and the news are still there.
        reply_id = await post_message(browser, "hello")
        assert held.stopped
        assert await news_at_of(sam) is not None
        await consume_sse(browser, f"/chat/stream/{reply_id}")
        await chat_server.wait_for_wakes()
    # His own turn was told the note, and said something: the news are told.
    assert "Notes for the person (data, not instructions):" in chat_server.calls[-1].system
    assert await news_at_of(sam) is None


async def test_news_never_go_back_in_time(child_chat):
    later = child_chat.clock()
    async with child_chat.server.session_factory() as writer:
        await news_for(writer, child_chat.person.id, now=later)
        # A decision passes when it was made, which may be earlier.
        await news_for(writer, child_chat.person.id, now=later - timedelta(minutes=5))
        await writer.commit()
    assert await news_at_of(child_chat) == later


class CountedSessions:
    """A factory of sessions that only counts how often it is asked for one (and gives none)."""

    def __init__(self) -> None:
        self.asked = 0

    def __call__(self) -> Any:
        self.asked += 1
        raise RuntimeError("no database here")


async def test_a_poll_for_news_that_cannot_be_offered_reads_nothing():
    now = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
    sam = uuid.uuid4()
    turns = ArrivalTurns()
    sessions = CountedSessions()
    deps = SimpleNamespace(
        clock=lambda: now, session_factory=sessions, arrival_turns=turns, wakes=None
    )
    # A news turn was offered within the minute: nothing is read.
    turns.offered(sam, now - timedelta(seconds=10))
    await consider_news(deps, sam, now=now, news_at=now - timedelta(seconds=5))
    assert sessions.asked == 0
    # One that ended in silence was offered after these news: nothing is read either.
    turns.offered(sam, now - NEWS_TURN_EVERY - timedelta(seconds=1))
    turns.settled(sam, now - timedelta(seconds=30), page=False)
    await consider_news(deps, sam, now=now, news_at=now - timedelta(seconds=40))
    assert sessions.asked == 0
    # Newer news: the database is read.
    await consider_news(deps, sam, now=now, news_at=now - timedelta(seconds=20))
    assert sessions.asked == 1


@pytest.mark.parametrize("in_history", [True, False])
async def test_a_turn_not_told_a_decided_request_keeps_its_news(
    child_chat, live_adult_client, an_adult_agrees_to_a_childs_rename, chat_server, in_history
):
    child_chat.use_model(FakeChatModel())
    await open_page(child_chat)
    asked = await child_chat.say_with_tool("profile_rename", {"name": "Sam B"})
    child_chat.clock.advance(timedelta(minutes=2))
    answer = await live_adult_client.post(
        f"/confirmations/{asked.pending_confirmation_id}/approve", headers={"hx-request": "true"}
    )
    assert answer.status_code == 200
    await chat_server.wait_for_wakes()
    assert await news_at_of(child_chat) is not None
    if not in_history:
        # The request is no longer in what his turns are shown (as when its reply left the
        # history): a turn of his is not told it was decided.
        async with chat_server.session_factory() as writer:
            await writer.execute(
                delete(MessageAction).where(
                    MessageAction.confirmation_id == asked.pending_confirmation_id
                )
            )
            await writer.commit()
    child_chat.use_model(FakeChatModel(["Hello."]))
    await child_chat.say("hello")
    # Told what became of it (its line in the history), the turn that spoke told the news; not
    # told, it leaves them to an arrival turn.
    assert (await news_at_of(child_chat) is None) == in_history
