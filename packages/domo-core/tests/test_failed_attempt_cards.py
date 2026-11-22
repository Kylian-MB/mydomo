"""A failed attempt that the same answer makes good leaves no card (spec 6.7, task 10).

When a call is refused for its arguments (``invalid``) and a later call of the same tool, in the
same answer, is done or waits for a yes, the person sees only what was done: no card for the
attempt, live, on the page read again, or through the API. The attempt stays stored with the reply
(``message_actions``), and in the history the model reads (it learns from its own mistakes). A
failed attempt alone is still shown, and so is one that only a call of another tool follows.

Live, what follows a failed attempt is held only while the round after it may still call the same
tool (``conversation.turn.HeldBack``): the answer streams again after that round.
"""

import json
import re
from dataclasses import dataclass
from typing import Any

from api_support import ApiPilot
from chat_support import ChatPilot
from domo_core.chat.agent import ActionRecord, ActionTaken, TextDelta, run_turn
from domo_core.conversation.cards import shown_actions
from domo_core.conversation.events import Words
from domo_core.conversation.turn import HeldBack
from domo_core.llm.types import ChatMessage, ToolCall, ToolDefinition
from fakes import FakeChatModel, FixedModels
from test_agent_tools import RoundsModel

_CARD = re.compile(r'<div class="action-card" data-action-status="([a-z]+)" data-tool="([a-z_]+)"')


def call(name: str, arguments: dict[str, Any], call_id: str) -> ToolCall:
    return ToolCall(call_id, name, json.dumps(arguments))


def cards(html: str) -> list[tuple[str, str]]:
    """The cards of a stream or a page, as ``(tool, status)``, in order."""
    return [(tool, status) for status, tool in _CARD.findall(html)]


async def stored_cards(chat_server, member, message_id) -> list[str]:
    """The cards the API gives of the reply ``message_id``, in words."""
    async with ApiPilot.paired(chat_server, member) as device:
        [kept] = [m for m in await device.since() if m["id"] == str(message_id)]
    return kept["cards"]


async def test_a_failed_then_successful_attempt_shows_one_card(
    adult_chat: ChatPilot, chat_server, adult_member
):
    model = adult_chat.use_model(
        FakeChatModel(
            tool_script=[
                call("profile_rename", {"name": "Al\nex"}, "c1"),
                call("profile_rename", {"name": "Al"}, "c2"),
                "Done, Al.",
            ],
            judge="do",
        )
    )
    reply = await adult_chat.say("Call me Al")
    # Both stay stored with the reply...
    assert reply.actions == [
        {"tool": "profile_rename", "status": "invalid", "reason": "invalid"},
        {"tool": "profile_rename", "status": "done"},
    ]
    # ... and the model read both results in the turn.
    assert [r["status"] for r in reply.model_results] == ["invalid", "done"]
    # Live: one card, the one of what was done, before the words that followed it.
    assert cards(reply.cards_html) == [("profile_rename", "done")]
    assert [e.event for e in reply.events] == ["action", "delta", "done"]
    # The page read again: the same.
    page = (await adult_chat.get("/chat")).text
    assert cards(page) == [("profile_rename", "done")]
    # The API: one card in words.
    assert len(await stored_cards(chat_server, adult_member, reply.message_id)) == 1
    # The history of the next turn keeps the attempt: Domo learns from his mistakes.
    adult_chat.use_model(model := FakeChatModel(["ok"]))
    await adult_chat.say("Thanks")
    history = json.dumps([str(m) for m in model.calls[0].messages])
    assert "[action profile.rename: invalid]" in history
    assert "[action profile.rename: done]" in history


async def test_a_failed_attempt_alone_still_shows(adult_chat: ChatPilot, chat_server, adult_member):
    adult_chat.use_model(
        FakeChatModel(
            tool_script=[call("profile_rename", {"name": "Al\nex"}, "c1"), "I could not."],
            judge="do",
        )
    )
    reply = await adult_chat.say("Call me Al")
    assert reply.actions == [{"tool": "profile_rename", "status": "invalid", "reason": "invalid"}]
    # Live, in the order it came: the card, then the words.
    assert cards(reply.cards_html) == [("profile_rename", "invalid")]
    assert [e.event for e in reply.events] == ["action", "delta", "done"]
    assert cards((await adult_chat.get("/chat")).text) == [("profile_rename", "invalid")]
    assert len(await stored_cards(chat_server, adult_member, reply.message_id)) == 1


async def test_another_tool_does_not_make_a_failed_attempt_good(adult_chat: ChatPilot):
    adult_chat.use_model(
        FakeChatModel(
            tool_script=[
                call("profile_rename", {"name": "Al\nex"}, "c1"),
                call("household_list", {}, "c2"),
                "Here.",
            ],
            judge="do",
        )
    )
    reply = await adult_chat.say("Call me Al and list the household")
    expected = [("profile_rename", "invalid"), ("household_list", "done")]
    assert cards(reply.cards_html) == expected
    assert cards((await adult_chat.get("/chat")).text) == expected


async def test_an_attempt_made_good_after_another_tool_is_still_not_shown(
    adult_chat: ChatPilot,
):
    """A retry two rounds later: live, the attempt was already given once the round after it
    ended without one (the answer is never held longer, ``HeldBack``); the page read again, which
    knows the whole answer, shows only what was done (``shown_actions``)."""
    adult_chat.use_model(
        FakeChatModel(
            tool_script=[
                call("profile_rename", {"name": "Al\nex"}, "c1"),
                call("household_list", {}, "c2"),
                call("profile_rename", {"name": "Al"}, "c3"),
                "Done.",
            ],
            judge="do",
        )
    )
    reply = await adult_chat.say("Call me Al")
    assert cards(reply.cards_html) == [
        ("profile_rename", "invalid"),
        ("household_list", "done"),
        ("profile_rename", "done"),
    ]
    expected = [("household_list", "done"), ("profile_rename", "done")]
    assert cards((await adult_chat.get("/chat")).text) == expected


@dataclass(frozen=True)
class Taken:
    tool_name: str
    action_type: str | None
    status: str


def test_which_actions_of_an_answer_are_shown():
    rename = "profile.rename"
    bad, good = Taken("profile_rename", rename, "invalid"), Taken("profile_rename", rename, "done")
    waits = Taken("profile_rename", rename, "waiting")
    refused = Taken("profile_rename", rename, "refused")
    other = Taken("household_list", "household.list", "done")
    memory = Taken("memory_keep", "memory.keep", "invalid")
    assert shown_actions([bad, good]) == [good]
    assert shown_actions([bad, waits]) == [waits]
    assert shown_actions([bad]) == [bad]
    # Only an action done or waiting makes good, only a call refused for its arguments is made
    # good, and only by a later call of the same tool.
    assert shown_actions([bad, refused]) == [bad, refused]
    assert shown_actions([refused, good]) == [refused, good]
    assert shown_actions([good, bad]) == [good, bad]
    assert shown_actions([bad, other]) == [bad, other]
    assert shown_actions([bad, bad, other, good]) == [other, good]
    # What is never shown stays unshown.
    assert shown_actions([memory, other]) == [other]


async def test_a_retry_that_waits_for_a_yes_hides_the_failed_card_live(adult_chat: ChatPilot):
    adult_chat.use_model(
        FakeChatModel(
            tool_script=[
                call("profile_rename", {"name": "Al\nex"}, "c1"),
                call("profile_rename", {"name": "Al"}, "c2"),
                "Shall I?",
            ],
            judge="yes",
        )
    )
    reply = await adult_chat.say("Call me Al")
    assert [a["status"] for a in reply.actions] == ["invalid", "waiting"]
    # One card, the request's: none for the attempt.
    assert cards(reply.cards_html) == [("profile_rename", "waiting")]
    assert reply.pending_confirmation_id is not None
    assert 'data-action-status="invalid"' not in (await adult_chat.get("/chat")).text


def record(tool: str, status: str) -> ActionRecord:
    return ActionRecord(tool, "profile.rename", status, None, {}, {})  # type: ignore[arg-type]


def test_a_round_of_other_calls_alone_lets_the_answer_flow_again():
    """The usual way: a failed call (round 1), then a round that only calls another tool (round
    2), then the final words. Once round 2 is over, the attempt and the other card go out, and
    the words come as they come."""
    held = HeldBack()
    failed, other = record("profile_rename", "invalid"), record("household_list", "done")
    assert held.action(failed, round=1, closes_round=True) == []
    assert held.action(other, round=2, closes_round=True) == [failed, other]
    words = Words("Here is the household.")
    assert held.words(words) == [words]
    assert held.end() == []


def test_words_and_other_calls_of_the_next_round_are_held_until_it_is_over():
    held = HeldBack()
    failed = record("profile_rename", "invalid")
    first, second = record("x", "done"), record("y", "done")
    assert held.action(failed, round=1, closes_round=True) == []
    w1 = Words("Let me look first.")
    assert held.words(w1) == []
    # Round 2 is not over until its last call: the retry may still come.
    assert held.action(first, round=2, closes_round=False) == []
    assert held.action(second, round=2, closes_round=True) == [failed, w1, first, second]
    w2 = Words("Found it.")
    assert held.words(w2) == [w2]


def test_a_failed_attempt_made_good_in_the_next_round_is_never_given():
    held = HeldBack()
    failed, good = record("profile_rename", "invalid"), record("profile_rename", "done")
    assert held.action(failed, round=1, closes_round=True) == []
    w1 = Words("Again:")
    assert held.words(w1) == []
    assert held.action(good, round=2, closes_round=True) == [w1, good]
    assert held.end() == []


def test_a_failed_attempt_waits_for_the_last_call_of_its_own_round():
    """A call of the same round after the attempt does not end its wait: the next round may
    still make it good."""
    held = HeldBack()
    failed, other = record("profile_rename", "invalid"), record("x", "done")
    assert held.action(failed, round=1, closes_round=False) == []
    assert held.action(other, round=1, closes_round=True) == []
    words = Words("Voilà.")
    assert held.words(words) == []
    # The round after it said words only, and the answer ended: the card, then the words.
    assert held.end() == [failed, other, words]


def test_a_second_failed_attempt_is_held_for_its_own_next_round():
    held = HeldBack()
    first, second = record("profile_rename", "invalid"), record("note_leave", "invalid")
    assert held.action(first, round=1, closes_round=True) == []
    w1 = Words("Hm.")
    assert held.words(w1) == []
    # Round 2 ends: the first attempt goes out with what came before the second, still held.
    assert held.action(second, round=2, closes_round=True) == [first, w1]
    w2 = Words("Again.")
    assert held.words(w2) == []
    assert held.end() == [second, w2]


class _Calls:
    """Each call of the engine is handled at once: ``profile_rename`` refused for its arguments,
    anything else done."""

    async def __call__(self, call: ToolCall, *, over_limit: bool = False) -> ActionRecord:
        status = "invalid" if call.name == "profile_rename" else "done"
        return record(call.name, status)


async def test_the_engine_rounds_let_the_final_words_flow_after_a_silent_round():
    """With the engine itself: a failed call, a round that only calls another tool, then the
    final words. What may go out is given as each event comes, never all at the end."""
    model = RoundsModel(
        [call("profile_rename", {"name": "Al\nex"}, "c1")],
        [call("household_list", {}, "c2")],
        ["Here", " it is."],
    )
    held = HeldBack()
    given: list[list[str]] = []
    async for event in run_turn(
        models=FixedModels(model),
        system_prompt="S",
        history=[ChatMessage("user", "hi")],
        max_output_tokens=32,
        tools=(
            ToolDefinition("profile_rename", "R.", {}),
            ToolDefinition("household_list", "L.", {}),
        ),
        handle_tool_call=_Calls(),
    ):
        match event:
            case ActionTaken(record=taken, round=round_, closes_round=closes):
                out = held.action(taken, round=round_, closes_round=closes)
            case TextDelta(text=text):
                out = held.words(Words(text))
            case _:
                out = held.end()
        given.append([i.text if isinstance(i, Words) else i.tool_name for i in out])
    assert given == [[], ["profile_rename", "household_list"], ["Here"], [" it is."], []]
