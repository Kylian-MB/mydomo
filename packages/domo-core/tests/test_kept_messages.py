"""A message the person sends while Domo answers (spec 11.3): it is kept, never refused. He
reads it between two actions of the turn that runs, as their own words, and judges whether to stop,
change or go on; what was done before it stays done. When no action is left to the turn, it is
answered right after, by a reply of its own.

The chat pilots talk with Domo on a live server (see ``chat_support.py``). The model holds its
answer back (``Gated``) until the test let the person write meanwhile.
"""

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from typing import Any

import httpx
import pytest
from api_support import API, ApiPilot, plain_client
from chat_support import (
    WAIT_SECONDS,
    ChatPilot,
    ChatServer,
    Member,
    actions_of,
    consume_sse,
    is_text_call,
    post_message,
    read_stream,
)
from domo_core.channels.page.speaker import page_speaker
from domo_core.channels.turns import app_turn_deps
from domo_core.chat.agent import KEPT_MARKER
from domo_core.chat.models import Conversation as Stored
from domo_core.chat.models import Message
from domo_core.confirmations.models import Confirmation
from domo_core.conversation import turn as conversation_turn
from domo_core.conversation.service import Conversation
from domo_core.llm.types import (
    ChatChunk,
    ChatMessage,
    ChatUsage,
    HistoryItem,
    ModelCallError,
    ToolCall,
    ToolChoice,
    ToolDefinition,
    ToolResult,
)
from domo_core.tools.notes import _EXACT_KEPT as EXACT_KEPT  # pyright: ignore[reportPrivateUsage]
from fakes import FakeCall, FakeChatModel, is_judge_call, is_reader_call
from sqlalchemy import select


class Gated:
    """The model of a turn the person writes during: its main calls (neither the judge's, nor the
    reader's, nor the texts') take the next entry of ``script`` (``FakeChatModel``), and the main
    call number ``gate_at`` waits, once it began (``started``), until the test lets it go
    (``go``). With ``fail_gated``, that call then fails instead. The judge answers ``judge``."""

    def __init__(
        self,
        *script: ToolCall | str,
        gate_at: int = 1,
        fail_gated: bool = False,
        judge: str | Sequence[str] = "do",
    ) -> None:
        self.inner = FakeChatModel(tool_script=list(script), judge=judge)
        self.started = asyncio.Event()
        self.go = asyncio.Event()
        self._gate_at = gate_at
        self._fail_gated = fail_gated
        self._main = 0

    @property
    def main_calls(self) -> list[FakeCall]:
        return [call for call in self.inner.calls if not is_reader_call(call.system)]

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
        main = not (is_judge_call(system) or is_text_call(system) or is_reader_call(system))
        if main:
            self._main += 1
            if self._main == self._gate_at:
                self.started.set()
                await self.go.wait()
                if self._fail_gated:
                    raise ModelCallError("upstream")
        async for item in self.inner.stream(
            system=system,
            messages=messages,
            max_output_tokens=max_output_tokens,
            tools=tools,
            tool_choice=tool_choice,
        ):
            yield item


async def started(model: Gated) -> None:
    async with asyncio.timeout(WAIT_SECONDS):
        await model.started.wait()


async def conversation_of(server: ChatServer, person_id: uuid.UUID) -> list[Any]:
    """The person's messages, oldest first: ``id``, ``role``, ``content``, ``status``,
    ``own_answer``."""
    async with server.session_factory() as reader:
        found = await reader.execute(
            select(Message.id, Message.role, Message.content, Message.status, Message.own_answer)
            .join(Stored, Stored.id == Message.conversation_id)
            .where(Stored.person_id == person_id)
            .order_by(Message.created_at, Message.id)
        )
        return list(found.all())


@pytest.fixture
async def other_page(
    chat_server: ChatServer, owner_member: Member
) -> AsyncIterator[httpx.AsyncClient]:
    """Another page of the owner, which never waits for the turns of Domo (one runs while the
    test writes)."""
    async with chat_server.browser(owner_member, waits_for_wakes=False) as client:
        yield client


async def during_the_reply(
    pilot: ChatPilot, model: Gated, first: str, *meanwhile: tuple[httpx.AsyncClient, str]
) -> tuple[str, list[httpx.Response]]:
    """``pilot`` says ``first`` and reads the reply, and, once the model holds its answer back,
    each of ``meanwhile`` is sent (a page and its text); then the model goes on. The id of the
    reply, and the answers to what was sent meanwhile."""
    reply_id = await post_message(pilot.client, first)
    reading = asyncio.create_task(consume_sse(pilot.client, f"/chat/stream/{reply_id}"))
    await started(model)
    answers = [await page.post("/chat/messages", data={"text": text}) for page, text in meanwhile]
    model.go.set()
    await reading
    await pilot.server.wait_for_wakes()
    return reply_id, answers


RENAME = ToolCall("c1", "profile_rename", json.dumps({"name": "Owner B"}))


async def test_a_message_during_a_reply_is_stored_not_refused(owner_chat, chat_server):
    await post_message(owner_chat.client, "hello")  # its reply is being written
    before = await conversation_of(chat_server, owner_chat.person.id)
    said, reply = await Conversation(app_turn_deps(chat_server.app)).say(
        owner_chat.person, "no, stop", page_speaker(owner_chat.person.id, chat_server.clock())
    )
    assert reply is None
    after = await conversation_of(chat_server, owner_chat.person.id)
    assert len(after) == len(before) + 1
    assert (after[-1].id, after[-1].role, after[-1].content) == (said.id, "user", "no, stop")
    # No other reply was opened: the one being written is still the only one.
    assert [m.status for m in after if m.role == "assistant"] == ["streaming"]


async def test_between_two_actions_he_reads_it(owner_chat, chat_server, other_page):
    model = owner_chat.use_model(Gated(RENAME, "I stopped."))
    reply_id, [kept] = await during_the_reply(
        owner_chat, model, "rename me", (other_page, "no, stop")
    )
    assert kept.status_code == 202
    first, second = model.main_calls
    last = second.messages[-1]
    assert isinstance(last, ChatMessage) and last.role == "user"
    assert last.content.startswith(KEPT_MARKER) and "no, stop" in last.content
    assert "no, stop" not in json.dumps([str(m) for m in first.messages])
    # Read in this turn: no reply of its own.
    stored = await conversation_of(chat_server, owner_chat.person.id)
    assert [(m.role, m.content) for m in stored] == [
        ("user", "rename me"),
        ("assistant", "I stopped."),
        ("user", "no, stop"),
    ]
    assert str(stored[1].id) == reply_id


async def test_what_was_done_before_it_stays_done(
    owner_chat, chat_server, other_page, display_name_of
):
    model = owner_chat.use_model(Gated(RENAME, "Done before you wrote."))
    reply_id, [kept] = await during_the_reply(
        owner_chat, model, "rename me", (other_page, "no, stop")
    )
    assert kept.status_code == 202
    assert await display_name_of(owner_chat.person) == "Owner B"
    async with chat_server.session_factory() as reader:
        assert await actions_of(reader, uuid.UUID(reply_id)) == [
            {"tool": "profile_rename", "status": "done"}
        ]


async def test_the_judge_reads_it_as_their_words(owner_chat, chat_server, other_page):
    again = ToolCall("c2", "profile_rename", json.dumps({"name": "Owner C"}))
    # They write once the first call was judged, while Domo thinks of the second.
    model = owner_chat.use_model(Gated(RENAME, again, "ok", gate_at=2))
    await during_the_reply(owner_chat, model, "rename me", (other_page, "no, stop"))
    first, second = (json.loads(call.messages[0].content) for call in model.inner.judge_calls())
    assert "no, stop" not in first["words"]
    assert "no, stop" in second["words"]


async def test_with_no_action_left_it_is_answered_right_after(owner_chat, chat_server, other_page):
    model = owner_chat.use_model(Gated("First answer.", "Second answer."))
    reply_id, [kept] = await during_the_reply(
        owner_chat, model, "hello", (other_page, "and also this")
    )
    assert kept.status_code == 202
    stored = await conversation_of(chat_server, owner_chat.person.id)
    assert [(m.role, m.content, m.status) for m in stored] == [
        ("user", "hello", "complete"),
        ("assistant", "First answer.", "complete"),
        ("user", "and also this", "complete"),
        ("assistant", "Second answer.", "complete"),
    ]
    # The second reply answers that message, as any reply answers the person's.
    follow_up = model.main_calls[-1].messages[-1]
    assert follow_up == ChatMessage("user", "and also this")
    assert stored[3].own_answer is True
    # The page finds it as it finds any reply Domo starts.
    page = (await other_page.get(f"/chat/since/{stored[2].id}")).text
    assert "Second answer." in page


async def test_two_kept_messages_are_read_together_in_order(owner_chat, chat_server, other_page):
    model = owner_chat.use_model(Gated(RENAME, "Read both."))
    _, answers = await during_the_reply(
        owner_chat, model, "rename me", (other_page, "one"), (other_page, "two")
    )
    assert [a.status_code for a in answers] == [202, 202]
    last = model.main_calls[1].messages[-1]
    assert isinstance(last, ChatMessage) and last.content.startswith(KEPT_MARKER)
    assert last.content.index("one") < last.content.index("two")
    stored = await conversation_of(chat_server, owner_chat.person.id)
    assert [m.role for m in stored] == ["user", "assistant", "user", "user"]


async def test_the_page_answers_202_and_the_api_says_kept(
    owner_chat, chat_server, other_page, owner_member
):
    model = owner_chat.use_model(Gated("First.", "Second.", "Third."))
    async with (
        ApiPilot.paired(chat_server, owner_member) as device,
        plain_client(chat_server) as raw,
    ):
        # A device that never waits for the turns of Domo (one runs while it writes).
        raw.headers["authorization"] = f"Bearer {device.secret}"
        reply_id = await post_message(owner_chat.client, "hello")
        reading = asyncio.create_task(consume_sse(owner_chat.client, f"/chat/stream/{reply_id}"))
        await started(model)
        page = await other_page.post("/chat/messages", data={"text": "from the page"})
        api = await raw.post(f"{API}/messages", json={"text": "from the device"})
        model.go.set()
        events = await reading
    await chat_server.wait_for_wakes()
    assert page.status_code == 202
    # The person's message is shown at once; no stream is opened for it.
    assert 'class="message user"' in page.text and "from the page" in page.text
    assert "sse-connect" not in page.text
    assert (api.status_code, api.json()) == (202, {"reply_id": None, "kept": True})
    # The reply that was being written went on to its end.
    assert events[-1].event == "done"
    stored = await conversation_of(chat_server, owner_chat.person.id)
    assert [(m.role, m.content) for m in stored][-3:] == [
        ("user", "from the page"),
        ("user", "from the device"),
        ("assistant", "Second."),
    ]


async def test_a_kept_message_survives_a_turn_that_fails(owner_chat, chat_server, other_page):
    model = owner_chat.use_model(Gated("Answered after.", fail_gated=True))
    _, [kept] = await during_the_reply(owner_chat, model, "hello", (other_page, "still there?"))
    assert kept.status_code == 202
    stored = await conversation_of(chat_server, owner_chat.person.id)
    assert [(m.role, m.content, m.status) for m in stored] == [
        ("user", "hello", "complete"),
        ("assistant", "", "error"),
        ("user", "still there?", "complete"),
        ("assistant", "Answered after.", "complete"),
    ]


async def test_a_yes_sent_while_he_writes_is_read_in_the_turn_after(
    adult_chat, chat_server, adult_member, display_name_of, confirmation_status
):
    # The request is shown by the reply being written; the yes comes during its last round, so
    # no round is left to read it: the turn after answers it, and reads it as any yes in words.
    rename = ToolCall("c1", "profile_rename", json.dumps({"name": "Alex B"}))
    model = adult_chat.use_model(Gated(rename, "Shall I?", "Done.", gate_at=2, judge="yes"))
    async with chat_server.browser(adult_member, waits_for_wakes=False) as page:
        _, [kept] = await during_the_reply(adult_chat, model, "rename me", (page, "oui"))
    assert kept.status_code == 202
    async with chat_server.session_factory() as reader:
        request_id = await reader.scalar(select(Confirmation.id))
    assert await confirmation_status(request_id) == "executed"
    assert await display_name_of(adult_chat.person) == "Alex B"
    stored = await conversation_of(chat_server, adult_chat.person.id)
    assert [(m.role, m.content) for m in stored][-2:] == [("user", "oui"), ("assistant", "Done.")]


# --- fix round 1 ---------------------------------------------------------------------------------


async def test_in_a_turn_he_started_a_kept_message_changes_nobodys_acts(
    adult_chat, chat_server, adult_member
):
    # Ruling R61: their words are handed to him, but what he does there stays his own.
    asked = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    again = ToolCall("w1", "profile_rename", json.dumps({"name": "Alex C"}))
    model = adult_chat.use_model(Gated(again, "Ton nom est changé.", judge="do"))
    async with chat_server.browser(adult_member, waits_for_wakes=False) as page:
        approved = await page.post(
            f"/confirmations/{asked.pending_confirmation_id}/approve",
            headers={"hx-request": "true"},
        )
        assert approved.status_code == 200
        await started(model)  # Domo writes the wake's reply
        kept = await page.post("/chat/messages", data={"text": "ok"})
        model.go.set()
        await chat_server.wait_for_wakes()
    assert kept.status_code == 202
    last = model.main_calls[-1].messages[-1]
    assert isinstance(last, ChatMessage) and last.content.startswith(KEPT_MARKER)
    stored = await conversation_of(chat_server, adult_chat.person.id)
    wake = stored[-2]
    assert (wake.role, wake.content) == ("assistant", "Ton nom est changé.")
    assert wake.own_answer is False  # still a reply he wrote unasked
    assert (stored[-1].role, stored[-1].content) == ("user", "ok")


async def test_a_kept_message_adds_to_what_they_said(owner_chat, chat_server, other_page):
    # An exact passage of either message is theirs: the first one's is never lost.
    def note(call_id: str, exact: str) -> ToolCall:
        arguments = {"recipient": "Owner", "text": "A note.", "exact": exact}
        return ToolCall(call_id, "note_leave", json.dumps(arguments))

    first = note("k1", "the boiler is noisy in the morning")
    second = note("k2", "I am in a hurry")
    model = owner_chat.use_model(Gated(RENAME, first, second, "Kept.", judge="do"))
    await during_the_reply(
        owner_chat,
        model,
        "Remember: the boiler is noisy in the morning.",
        (other_page, "I am in a hurry"),
    )
    results = [
        json.loads(item.content)
        for item in model.main_calls[-1].messages
        if isinstance(item, ToolResult) and item.name == "note_leave"
    ]
    assert [result["exact"] for result in results] == [EXACT_KEPT, EXACT_KEPT]


async def test_a_passage_never_spans_two_of_their_messages(owner_chat, chat_server, other_page):
    # Ruling R62: each message is checked on its own.
    def note(call_id: str, exact: str) -> ToolCall:
        arguments = {"recipient": "Owner", "text": "A note.", "exact": exact}
        return ToolCall(call_id, "note_leave", json.dumps(arguments))

    across = note("k1", "did not lie")
    first = note("k2", "I did not")
    second = note("k3", "lie to you")
    model = owner_chat.use_model(Gated(RENAME, across, first, second, "Noted.", judge="do"))
    await during_the_reply(owner_chat, model, "I did not", (other_page, "lie to you"))
    results = [
        json.loads(item.content)
        for item in model.main_calls[-1].messages
        if isinstance(item, ToolResult) and item.name == "note_leave"
    ]
    assert [result["exact"] == EXACT_KEPT for result in results] == [False, True, True]


async def test_a_kept_message_survives_a_turn_that_raises(
    owner_chat, chat_server, other_page, monkeypatch
):
    # Whatever ends the turn but the core stopping, what was kept is answered after it.
    original = conversation_turn.Turn._after_words
    raised: list[bool] = []

    async def fails_once(self: Any, **kwargs: Any) -> None:
        if not raised:
            raised.append(True)
            raise RuntimeError("a bug")
        await original(self, **kwargs)

    monkeypatch.setattr(conversation_turn.Turn, "_after_words", fails_once)
    model = owner_chat.use_model(Gated("First.", "Answered after."))
    reply_id = await post_message(owner_chat.client, "hello")
    reading = asyncio.create_task(read_stream(owner_chat.client, f"/chat/stream/{reply_id}"))
    await started(model)
    kept = await other_page.post("/chat/messages", data={"text": "still there?"})
    model.go.set()
    await reading
    await chat_server.wait_for_wakes()
    assert kept.status_code == 202 and raised == [True]
    stored = await conversation_of(chat_server, owner_chat.person.id)
    assert [(m.role, m.content) for m in stored][-2:] == [
        ("user", "still there?"),
        ("assistant", "Answered after."),
    ]


async def test_a_turn_stopped_with_the_core_opens_no_turn_after_it(
    owner_chat, chat_server, other_page
):
    # Stopping, nothing new is started: the message stays stored, as after a restart.
    model = owner_chat.use_model(Gated("Never said."))
    reply_id = await post_message(owner_chat.client, "hello")
    reading = asyncio.create_task(read_stream(owner_chat.client, f"/chat/stream/{reply_id}"))
    await started(model)
    kept = await other_page.post("/chat/messages", data={"text": "still there?"})
    await chat_server.app.state.wakes.stop(WAIT_SECONDS)
    await asyncio.gather(reading, return_exceptions=True)
    assert kept.status_code == 202
    assert chat_server.app.state.wakes.pending == 0
    stored = await conversation_of(chat_server, owner_chat.person.id)
    assert [(m.role, m.content) for m in stored][-1] == ("user", "still there?")
