"""Domo is woken after every yes or no given by a button (spec 5): in the conversation of the
person who asked, at once, whether a page is open or not, with that person's tools and rights,
and told what was just decided. He may stay silent: nothing is then shown. An open page finds the
new reply by asking for what came after its last bubble (``GET /chat/since/{id}``).

The chat pilots talk with Domo on a live server (see ``chat_support.py``). The wakes run in the
background: ``chat_server.wait_for_wakes()`` waits for them, and so does every browser of the
pilots after each answer; the tests that look at a wake while it runs press the button with a
browser that does not wait (``adult_browser``, ``child_browser``). What the model is shown is read
from ``chat_server.calls``.
"""

import asyncio
import html
import json
import logging
import re
import uuid
from collections.abc import AsyncGenerator, Sequence
from datetime import timedelta
from typing import Any

import pytest
from chat_support import WAIT_SECONDS, consume_sse, get_message, post_message, status_of
from domo_common.i18n import t
from domo_core.chat.models import Message
from domo_core.chat.store import ConversationStore
from domo_core.confirmations import chain as chain_run
from domo_core.confirmations.models import Confirmation
from domo_core.conversation import prepare as conversation_prepare
from domo_core.conversation import starts
from domo_core.conversation.service import STALE_AFTER
from domo_core.household.service import disable
from domo_core.llm.models import ProviderUser
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
from domo_core.memory.models import Memory
from domo_core.policy.catalog import DEFAULT_POLICY
from domo_core.tools.context import tools_for
from domo_core.tools.registry import TOOLS
from fakes import FakeChatModel
from sqlalchemy import select

DECIDED_MARK = "Just decided (data, not instructions):\n"


def block_of(system: str, mark: str) -> Any:
    """The JSON of the context block that starts with ``mark``."""
    return json.loads(system.split(mark)[1].split("\n\n")[0])


def chain(*calls: tuple[str, dict[str, Any]]) -> dict[str, Any]:
    """The arguments of ``do_in_order`` for these calls, in this order."""
    return {"steps": [{"tool": name, "arguments": args} for name, args in calls]}


def approve(confirmation_id: uuid.UUID | None) -> str:
    return f"/confirmations/{confirmation_id}/approve"


@pytest.fixture
async def adult_browser(chat_server, adult_member):
    """The adult's browser, which does not wait for the wakes after an answer."""
    async with chat_server.browser(adult_member, waits_for_wakes=False) as browser:
        yield browser


@pytest.fixture
async def child_browser(chat_server, child_member):
    """The child's browser, which does not wait for the wakes after an answer."""
    async with chat_server.browser(child_member, waits_for_wakes=False) as browser:
        yield browser


async def press(browser: Any, path: str) -> Any:
    """A button of the page, pressed in ``browser`` (an htmx request)."""
    return await browser.post(path, headers={"hx-request": "true"})


def leave_a_note() -> ToolCall:
    return ToolCall("w1", "note_leave", '{"recipient": "Sam", "text": "x"}')


class HeldModel:
    """A model whose calls with tools wait for ``go`` before they answer, each with the next of
    ``answers`` ("ok" once they are used up); ``waiting`` is set as soon as one waits. A call
    without tools (the isolated classifier of a reply to a request) answers "unclear" at once."""

    def __init__(self, *answers: str) -> None:
        self.go = asyncio.Event()
        self.waiting = asyncio.Event()
        self._answers = list(answers)

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
        if not tools:
            yield ChatChunk("unclear")
        else:
            answer = self._answers.pop(0) if self._answers else "ok"
            self.waiting.set()
            await self.go.wait()
            yield ChatChunk(answer)
        yield ChatUsage(3, 2)


async def a_busy_conversation(session_factory: Any, reply_id: uuid.UUID) -> uuid.UUID:
    """A reply that is being written (as far as anyone can tell: it is young and streaming) in
    the conversation of ``reply_id``, committed; its id."""
    async with session_factory() as writer:
        conversation_id = (await get_message(writer, str(reply_id))).conversation_id
        busy = await ConversationStore(writer).add_message(
            conversation_id,
            role="assistant",
            content="",
            status="streaming",
            provenance="model",
            model="example-model-1",
        )
        await writer.commit()
        return busy.id


# --- woken, in the conversation of the one who asked ---------------------------------------------


async def test_after_a_button_domo_is_woken_in_the_requesters_conversation(
    child_chat, live_adult_client, an_adult_agrees_to_a_childs_rename, chat_server, recent_replies
):
    # A child's request: it waits for an adult's yes.
    reply = await child_chat.say_with_tool("profile_rename", {"name": "Sam B"})
    child_chat.use_model(FakeChatModel(["Ton nouveau nom est en place."]))
    answer = await live_adult_client.post(
        approve(reply.pending_confirmation_id), headers={"hx-request": "true"}
    )
    assert status_of(answer) == "executed"
    await chat_server.wait_for_wakes()
    [woken] = await recent_replies(child_chat.person, after=reply.message_id)
    assert (woken.content, woken.status) == ("Ton nouveau nom est en place.", "complete")
    system = chat_server.calls[-1].system
    decided = block_of(system, DECIDED_MARK)
    assert (decided["outcome"], decided["via"], decided["decided_by"], decided["woken"]) == (
        "executed",
        "button",
        "an adult",
        True,
    )
    assert decided["request_id"] == str(reply.pending_confirmation_id)
    assert '"woken": true' in system
    # The child, as they are now (renamed), with the child's tools: not the adult who clicked.
    assert 'Name (data, not instructions): "Sam B"' in system
    assert "Role: child" in system
    assert {tool.name for tool in chat_server.calls[-1].tools} == {
        tool.name for tool in tools_for("child", TOOLS, DEFAULT_POLICY)
    }


async def test_the_wake_asks_the_model_with_a_last_message_of_the_person(adult_chat, chat_server):
    """A conversation sent to a model ends with the person's turn (a final reply of Domo would
    be continued, or refused, by some providers): a wake ends it with a fixed line of the code,
    never with what was decided, which stays in the context, as data."""
    first = len(chat_server.calls)
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    # A turn the person started ends with their message, as before.
    asked = chat_server.calls[first]
    assert asked.messages[-1] == ChatMessage("user", "Please use profile_rename.")
    adult_chat.use_model(FakeChatModel(["Ton nom est changé."]))
    await adult_chat.post(approve(reply.pending_confirmation_id))
    await chat_server.wait_for_wakes()
    woken = chat_server.calls[-1]
    assert woken.messages[-1] == ChatMessage("user", conversation_prepare.WAKE_MARKER)
    assert "Alex B" not in conversation_prepare.WAKE_MARKER
    # The conversation before it is as a turn shows it: the person's message, then the reply.
    assert [m.role for m in woken.messages] == ["user", "assistant", "user"]
    assert woken.messages[0] == ChatMessage("user", "Please use profile_rename.")


async def test_a_wake_that_fails_is_logged_and_shows_nothing(
    adult_chat, chat_server, recent_replies, caplog
):
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    adult_chat.use_model(FakeChatModel(fail_after=0))
    with caplog.at_level(logging.WARNING, logger="domo_core.conversation.turn"):
        await adult_chat.post(approve(reply.pending_confirmation_id))
        await chat_server.wait_for_wakes()
    assert "A wake of the assistant ended in error (upstream)" in caplog.text
    assert await recent_replies(adult_chat.person, after=reply.message_id) == []


async def test_a_wake_for_someone_who_has_no_key_any_more_is_dropped(
    adult_chat, owner_member, chat_server, session_factory, recent_replies
):
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    # A rule of the house takes Alex off the general key before the button is pressed: a wake goes
    # on the key of the one whose conversation it is, and there is none.
    async with session_factory() as writer:
        writer.add(
            ProviderUser(
                provider_id=chat_server.provider_id,
                scope="person",
                person_id=adult_chat.person.id,
                allowed=False,
                set_by=owner_member.person.id,
            )
        )
        await writer.commit()
    calls = len(chat_server.calls)
    await adult_chat.post(approve(reply.pending_confirmation_id))
    await chat_server.wait_for_wakes()
    assert len(chat_server.calls) == calls
    assert await recent_replies(adult_chat.person, after=reply.message_id) == []


async def test_a_no_by_the_button_wakes_domo_too(
    child_chat,
    live_adult_client,
    an_adult_agrees_to_a_childs_rename,
    chat_server,
    recent_replies,
    display_name_of,
):
    reply = await child_chat.say_with_tool("profile_rename", {"name": "Sam B"})
    child_chat.use_model(FakeChatModel(["Un adulte a dit non pour ton nom."]))
    answer = await live_adult_client.post(
        f"/confirmations/{reply.pending_confirmation_id}/refuse", headers={"hx-request": "true"}
    )
    assert status_of(answer) == "refused"
    await chat_server.wait_for_wakes()
    [woken] = await recent_replies(child_chat.person, after=reply.message_id)
    assert woken.content == "Un adulte a dit non pour ton nom."
    decided = block_of(chat_server.calls[-1].system, DECIDED_MARK)
    assert (decided["outcome"], decided["via"], decided["result"]) == ("refused", "button", None)
    assert await display_name_of(child_chat.person) == "Sam"


async def test_domo_may_stay_silent_and_nothing_is_shown(
    adult_chat, chat_server, recent_replies, last_audit_entry
):
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    adult_chat.use_model(FakeChatModel([""]))
    before = len(chat_server.calls)
    assert status_of(await adult_chat.post(approve(reply.pending_confirmation_id))) == "executed"
    await chat_server.wait_for_wakes()
    assert len(chat_server.calls) == before + 1  # he was asked, once
    assert await recent_replies(adult_chat.person, after=reply.message_id) == []
    page = (await adult_chat.get("/chat")).text
    assert page.count('class="message assistant"') == 1  # the reply that asked for the yes
    details = (await last_audit_entry("chat.turn")).details
    assert details["status"] == "silent"
    assert "fallback" not in details


async def test_a_reply_that_only_acts_is_kept(
    adult_chat, child_member, chat_server, recent_replies, count_notes
):
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    adult_chat.use_model(FakeChatModel(tool_script=[leave_a_note()], judge="do"))
    await adult_chat.post(approve(reply.pending_confirmation_id))
    await chat_server.wait_for_wakes()
    [woken] = await recent_replies(adult_chat.person, after=reply.message_id)
    assert (woken.content, woken.status) == ("", "complete")
    assert await count_notes() == 1


async def test_a_wake_that_only_keeps_a_memory_and_says_nothing_leaves_no_reply(
    adult_chat, chat_server, recent_replies, last_audit_entry, session_factory
):
    """What Domo keeps is never shown (spec 6.2): it is no action that keeps a silent reply.
    The memory stays, and the audit log is its record."""
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    kept = {
        "content": "Alex now goes by Alex B.",
        "subject": "Alex",
        "kind": "fact",
        "private": False,
        "core": True,
    }
    chat_server.app.state.embedder = OneAxisEmbedder()
    adult_chat.use_model(
        FakeChatModel(tool_script=[ToolCall("w1", "memory_keep", json.dumps(kept))])
    )
    await adult_chat.post(approve(reply.pending_confirmation_id))
    await chat_server.wait_for_wakes()
    assert await recent_replies(adult_chat.person, after=reply.message_id) == []
    page = (await adult_chat.get("/chat")).text
    assert page.count('class="message assistant"') == 1  # the reply that asked for the yes
    assert (await last_audit_entry("chat.turn")).details["status"] == "silent"
    async with session_factory() as session:
        memories = (await session.scalars(select(Memory))).all()
    assert [(m.content, m.said_by_id) for m in memories] == [
        (kept["content"], adult_chat.person.id)
    ]
    entry = await last_audit_entry("memory.kept")
    assert entry.details["memory"] == str(memories[0].id)
    # And its meaning was computed after the turn all the same.
    async with session_factory() as session:
        model = await session.scalar(
            select(Memory.embedding_model).where(Memory.id == memories[0].id)
        )
    assert model == OneAxisEmbedder.name


class OneAxisEmbedder:
    """An embedder that gives every text the same vector."""

    name = "one-axis"
    dimensions = 384

    async def passages(self, texts: Sequence[str]) -> list[list[float]] | None:
        return [[1.0] + [0.0] * (self.dimensions - 1) for _ in texts]

    async def query(self, text: str) -> list[float] | None:
        return None


# --- one reply at a time --------------------------------------------------------------------------


async def test_a_wake_waits_for_the_reply_that_streams(
    adult_chat, adult_browser, chat_server, recent_replies, monkeypatch
):
    monkeypatch.setattr(starts, "WAKE_RETRY_SECONDS", 0.05)
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    model = adult_chat.use_model(HeldModel("Il fait beau.", "Ton nom est changé."))
    talking = asyncio.create_task(adult_chat.say("Et la météo ?"))
    await asyncio.wait_for(model.waiting.wait(), WAIT_SECONDS)
    answer = await press(adult_browser, approve(reply.pending_confirmation_id))
    assert status_of(answer) == "executed"
    await asyncio.sleep(0.3)  # the wake tries again and again meanwhile
    # The turn of the reply the page streams, and the wake that waits for it.
    assert chat_server.wakes_of_domo == 2
    replies = await recent_replies(adult_chat.person, after=reply.message_id)
    assert [r.status for r in replies] == ["streaming"]  # the one that streams, and no other
    model.go.set()
    assert (await talking).text == "Il fait beau."
    await chat_server.wait_for_wakes()
    replies = await recent_replies(adult_chat.person, after=reply.message_id)
    assert [(r.content, r.status) for r in replies] == [
        ("Il fait beau.", "complete"),
        ("Ton nom est changé.", "complete"),
    ]


async def test_a_message_sent_during_a_wake_is_refused_as_busy(
    adult_chat, adult_browser, chat_server, recent_replies
):
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    model = adult_chat.use_model(HeldModel("Ton nom est changé."))
    answer = await press(adult_browser, approve(reply.pending_confirmation_id))
    assert status_of(answer) == "executed"
    await asyncio.wait_for(model.waiting.wait(), WAIT_SECONDS)  # Domo writes the wake's reply
    refused = await adult_browser.post("/chat/messages", data={"text": "Coucou"})
    assert refused.status_code == 409
    assert t("chat.error.busy", "en") in html.unescape(refused.text)
    model.go.set()
    await chat_server.wait_for_wakes()
    [woken] = await recent_replies(adult_chat.person, after=reply.message_id)
    assert (woken.content, woken.status) == ("Ton nom est changé.", "complete")
    # The conversation is free again.
    adult_chat.use_model(FakeChatModel(["Coucou !"]))
    assert (await adult_chat.say("Coucou")).text == "Coucou !"


async def test_nothing_domo_does_in_a_wake_wakes_him_again(
    adult_chat, child_member, chat_server, recent_replies, count_notes
):
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    adult_chat.use_model(FakeChatModel(tool_script=[leave_a_note(), "ok"], judge="do"))
    before = len(chat_server.calls)
    await adult_chat.post(approve(reply.pending_confirmation_id))
    await chat_server.wait_for_wakes()
    await asyncio.sleep(0.2)  # time for a second wake, were there one
    assert chat_server.wakes_of_domo == 0
    # One turn of two rounds: the note (and the judge's call on it), then "ok".
    assert len(chat_server.calls) - before == 3
    [woken] = await recent_replies(adult_chat.person, after=reply.message_id)
    assert (woken.content, woken.status) == ("ok", "complete")
    assert await count_notes() == 1


async def test_what_domo_does_on_his_own_in_a_wake_waits_for_a_yes_when_the_judge_is_silent(
    adult_chat, child_member, chat_server, count_notes, session_factory
):
    # Review I2: a wake answers no message of the person. Nobody asked for what Domo does in
    # it, so a judge that does not answer means a yes; and its reply is not his answer to them
    # (review I1: the judge never reads it).
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    adult_chat.use_model(FakeChatModel(tool_script=[leave_a_note(), "ok"], judge="maybe"))
    await adult_chat.post(approve(reply.pending_confirmation_id))
    await chat_server.wait_for_wakes()
    assert await count_notes() == 0
    async with session_factory() as reader:
        [waiting] = (
            await reader.execute(
                select(Confirmation.tool_name, Confirmation.ask, Confirmation.asked_by).where(
                    Confirmation.status == "pending"
                )
            )
        ).all()
        assert tuple(waiting) == ("note_leave", "yes", "judge")
        replies = (
            await reader.execute(
                select(Message.content, Message.own_answer)
                .where(Message.role == "assistant")
                .order_by(Message.created_at)
            )
        ).all()
    # The answer to their message is his own; the wake's is not.
    assert [tuple(row) for row in replies][-1] == ("ok", False)
    assert replies[0].own_answer is True


async def test_a_wake_is_dropped_when_the_conversation_stays_busy_too_long(
    adult_chat,
    chat_server,
    recent_replies,
    session_factory,
    monkeypatch,
    caplog,
):
    monkeypatch.setattr(starts, "WAKE_WAIT_SECONDS", 0.3)
    monkeypatch.setattr(starts, "WAKE_RETRY_SECONDS", 0.05)
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    busy = await a_busy_conversation(session_factory, reply.message_id)
    before = len(chat_server.calls)
    with caplog.at_level(logging.WARNING, logger="domo_core.conversation.starts"):
        assert status_of(await adult_chat.post(approve(reply.pending_confirmation_id))) == (
            "executed"
        )
        await chat_server.wait_for_wakes()
    assert "A wake was dropped: the conversation stayed busy" in caplog.text
    assert len(chat_server.calls) == before
    replies = await recent_replies(adult_chat.person, after=reply.message_id)
    assert [(r.id, r.status) for r in replies] == [(busy, "streaming")]


async def test_a_reply_nobody_writes_any_more_does_not_hold_the_wake(
    adult_chat, chat_server, recent_replies, session_factory
):
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    dead = await a_busy_conversation(session_factory, reply.message_id)
    chat_server.clock.advance(STALE_AFTER + timedelta(seconds=5))  # nobody writes it now
    adult_chat.use_model(FakeChatModel(["Ton nom est changé."]))
    await adult_chat.post(approve(reply.pending_confirmation_id))
    await chat_server.wait_for_wakes()
    replies = await recent_replies(adult_chat.person, after=reply.message_id)
    assert [(r.id, r.status) for r in replies][0] == (dead, "error")  # as a new message would
    assert [(r.content, r.status) for r in replies[1:]] == [("Ton nom est changé.", "complete")]


async def test_the_wakes_stop_with_the_application(
    adult_chat,
    adult_browser,
    chat_server,
    recent_replies,
    session_factory,
):
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    await a_busy_conversation(session_factory, reply.message_id)
    await press(adult_browser, approve(reply.pending_confirmation_id))
    wakes = chat_server.app.state.wakes
    assert chat_server.wakes_of_domo == 1  # it waits for the conversation
    await wakes.stop(WAIT_SECONDS)
    assert wakes.pending == 0
    assert [r.status for r in await recent_replies(adult_chat.person, after=reply.message_id)] == [
        "streaming"  # the reply that held the conversation, and nothing of the wake
    ]


async def test_the_reply_of_a_page_runs_with_the_wakes_and_stops_with_the_application(
    adult_chat, chat_server, session_factory
):
    """The turn of a reply a page streams is a task of ``app.state.wakes``, like a wake (spec
    5.7): the page that leaves does not stop it, the application that stops does, and the reply
    keeps what was written."""
    adult_chat.use_model(FakeChatModel(["Hel", "lo"], delay_s=5.0))
    reply_id = await post_message(adult_chat.client, "hi")
    assert await consume_sse(adult_chat.client, f"/chat/stream/{reply_id}", stop_after_events=1)
    wakes = chat_server.app.state.wakes
    assert chat_server.wakes_of_domo == 1  # the page left; the turn still runs
    await wakes.stop(WAIT_SECONDS)
    assert wakes.pending == 0
    async with session_factory() as session:
        message = await get_message(session, reply_id)
    assert (message.content, message.status) == ("Hel", "partial")
    assert uuid.UUID(reply_id) not in chat_server.app.state.streaming_replies


# --- what wakes him, and what does not ------------------------------------------------------------


async def test_a_stopped_chain_wakes_domo_with_the_steps(
    owner_chat, adult_member, child_member, chat_server, session_factory, recent_replies
):
    reply = await owner_chat.say_with_tool(
        "do_in_order",
        chain(
            ("household_change_role", {"person": "Sam", "role": "adult"}),
            ("household_remove", {"person": "Alex"}),
            ("note_leave", {"recipient": "Sam", "text": "x"}),
        ),
    )
    async with session_factory() as writer:
        await disable(
            writer, adult_member.person, owner_chat.clock(), by=owner_chat.person.id
        )  # gone before the yes
        await writer.commit()
    owner_chat.use_model(FakeChatModel(["La suite s'est arrêtée à la deuxième étape."]))
    await owner_chat.post(approve(reply.pending_confirmation_id))
    await chat_server.wait_for_wakes()
    decided = block_of(chat_server.calls[-1].system, DECIDED_MARK)
    assert (decided["outcome"], decided["via"], decided["woken"]) == ("failed", "button", True)
    report = decided["chain"]
    assert report["failed"] == t("action.describe.household.remove", "en", name="Alex")
    assert report["left"] == [t("action.describe.note.leave", "en", recipient="Sam", text="x")]
    assert report["can_continue"] is True
    [woken] = await recent_replies(owner_chat.person, after=reply.message_id)
    assert woken.content == "La suite s'est arrêtée à la deuxième étape."


async def test_a_link_made_in_a_wake_waits_on_the_card_of_the_one_who_asked(
    owner_chat, adult_member, chat_server, count_invitations, monkeypatch
):
    """Domo, woken by a button, continues a chain whose next step invites someone: no page reads
    the wake, so the link is not shown once to nobody. It waits behind the button of the owner's
    card, and the model is told so, never that it was shown."""
    reply = await owner_chat.say_with_tool(
        "do_in_order",
        chain(
            ("household_remove", {"person": "Alex"}),
            ("household_invite", {"display_name": "Dana", "role": "adult"}),
        ),
    )
    cid = reply.pending_confirmation_id
    # At the yes, the owner cannot be read again before the invitation: it stops the chain before
    # it runs (it did nothing), and Domo takes it up again in the wake.
    readings = 0
    read_again = chain_run.read_the_person_again

    async def unreadable_once(ctx: Any) -> Any:
        nonlocal readings
        readings += 1
        return "unknown" if readings == 2 else await read_again(ctx)

    monkeypatch.setattr(chain_run, "read_the_person_again", unreadable_once)
    before = len(chat_server.calls)
    owner_chat.use_model(
        FakeChatModel(
            tool_script=[
                ToolCall("w1", "chain_continue", json.dumps({"request_id": str(cid)})),
                "Dana est invitée : son lien est sur la carte.",
            ],
            judge="do",
        )
    )
    await owner_chat.post(approve(cid))
    await chat_server.wait_for_wakes()
    assert await count_invitations() == 1
    told = [
        content
        for call in chat_server.calls[before:]
        for item in call.messages
        if isinstance(item, ToolResult)
        for content in [item.content]
    ]
    assert len(told) == 1
    [invited] = [step["result"] for step in json.loads(told[0])["steps"]]
    assert invited["link"] == "waits_for_them" and "link_shown_to_owner" not in invited
    for call in chat_server.calls:
        assert "/invite/" not in call.system
        assert all("/invite/" not in str(item) for item in call.messages)
    # The button that makes the link is on the owner's card, and it works.
    page = (await owner_chat.get("/chat")).text
    assert 'data-action="show-link"' in page
    button = re.search(r'hx-post="(/[^"]*invitations/[^"]*)"', page)
    assert button is not None
    shown = await owner_chat.post(button.group(1))
    assert shown.status_code == 200 and "/invite/" in shown.text


async def test_a_click_that_decides_nothing_wakes_nobody(
    adult_chat, adult_browser, child_browser, chat_server
):
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    path = approve(reply.pending_confirmation_id)
    before = len(chat_server.calls)
    # A wake starts before the button's answer: none was, for a click that decided nothing.
    assert status_of(await press(child_browser, path)) == "not_yours"
    assert chat_server.wakes_of_domo == 0
    assert status_of(await press(adult_browser, path)) == "executed"
    assert chat_server.wakes_of_domo == 1
    await chat_server.wait_for_wakes()
    assert len(chat_server.calls) == before + 1  # the wake of that yes
    assert status_of(await press(adult_browser, path)) == "already_decided"
    assert chat_server.wakes_of_domo == 0
    assert len(chat_server.calls) == before + 1


async def test_the_link_a_button_made_never_reaches_the_model(owner_chat, chat_server):
    reply = await owner_chat.say_with_tool(
        "household_invite", {"display_name": "Dana", "role": "adult"}
    )
    owner_chat.use_model(FakeChatModel(["Le lien est sur la carte."]))
    before = len(chat_server.calls)
    card = await owner_chat.post(approve(reply.pending_confirmation_id))
    await chat_server.wait_for_wakes()
    token = re.search(r"/invite/([A-Za-z0-9_-]+)", card.text)
    assert token is not None  # the person sees the link, once, on the card
    assert len(chat_server.calls) == before + 1
    for call in chat_server.calls:
        assert token.group(1) not in call.system
        assert all(token.group(1) not in str(message) for message in call.messages)


# --- the open page --------------------------------------------------------------------------------


async def test_the_bubbles_from_since_have_their_cards(adult_chat, session_factory):
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    async with session_factory() as reader:
        conversation_id = (await get_message(reader, str(reply.message_id))).conversation_id
        asked = (
            await reader.execute(
                select(Message.id)
                .where(Message.conversation_id == conversation_id, Message.role == "user")
                .order_by(Message.created_at.desc())
                .limit(1)
            )
        ).scalar_one()
    page = (await adult_chat.get(f"/chat/since/{asked}")).text
    assert f'data-message-id="{reply.message_id}"' in page
    assert f'data-confirmation-id="{reply.pending_confirmation_id}"' in page
    assert f'hx-post="/confirmations/{reply.pending_confirmation_id}/approve"' in page


async def test_the_open_page_shows_domo_writing_a_wake_then_its_reply(
    adult_chat, adult_browser, chat_server, recent_replies
):  # spec 5.7.2
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    model = adult_chat.use_model(HeldModel("Ton nom est changé."))
    assert status_of(await press(adult_browser, approve(reply.pending_confirmation_id))) == (
        "executed"
    )
    await asyncio.wait_for(model.waiting.wait(), WAIT_SECONDS)  # Domo writes the wake's reply
    [woken] = await recent_replies(adult_chat.person, after=reply.message_id)
    page = (await adult_browser.get("/chat")).text
    since = (await adult_browser.get(f"/chat/since/{reply.message_id}")).text
    for shown in (page, since):
        assert re.search(
            rf'<div class="message assistant"[^>]*aria-busy="true" data-writing="true" '
            rf'data-message-id="{woken.id}"',
            shown,
        ), shown
    assert (
        t("chat.status.writing", "en", domo="Domo") in since and "Ton nom est changé." not in since
    )
    model.go.set()
    await chat_server.wait_for_wakes()
    since = (await adult_browser.get(f"/chat/since/{reply.message_id}")).text
    assert "data-writing" not in since and "Ton nom est changé." in since
    assert f'data-message-id="{woken.id}"' in since


async def test_the_open_page_gets_the_reply_through_since(
    child_chat, live_adult_client, an_adult_agrees_to_a_childs_rename, chat_server, recent_replies
):
    reply = await child_chat.say_with_tool("profile_rename", {"name": "Sam B"})
    nothing_yet = await child_chat.get(f"/chat/since/{reply.message_id}")
    assert (nothing_yet.status_code, nothing_yet.text.strip()) == (200, "")
    child_chat.use_model(FakeChatModel(["Ton nouveau nom est en place."]))
    await live_adult_client.post(
        approve(reply.pending_confirmation_id), headers={"hx-request": "true"}
    )
    await chat_server.wait_for_wakes()
    [woken] = await recent_replies(child_chat.person, after=reply.message_id)
    page = await child_chat.get(f"/chat/since/{reply.message_id}")
    assert page.status_code == 200
    assert f'data-message-id="{woken.id}"' in page.text
    assert "Ton nouveau nom est en place." in page.text
    assert "sse-connect" not in page.text  # a reply that is over, not a stream to open
    assert (await child_chat.get(f"/chat/since/{woken.id}")).text.strip() == ""
    # The child's messages are not the adult's to follow.
    assert (await live_adult_client.get(f"/chat/since/{woken.id}")).status_code == 404
