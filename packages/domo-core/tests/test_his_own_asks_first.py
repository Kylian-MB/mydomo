"""The owner's "ask me first" for the assistant is read when the action runs (ruling R41).

On a type that needs no judge (a plan, a memory kept: never shown, never judged), the barrier lets
the call through with the owner's rule; the handler then makes it wait for that yes, in a request
that is his (``by_domo``) and the owner's to answer, whichever conversation he acts in. Without
such a rule the call runs at once, as before. The tests drive the real handler: a launch for the
house wakes him on his own, in the owner's conversation, and he calls the tool."""

import json

import pytest
from chat_support import ChatPilot, ChatServer
from domo_core.confirmations.service import may_decide
from domo_core.llm.types import ToolCall
from domo_core.memory.models import Memory
from domo_core.plans.models import Plan
from sqlalchemy import func, select
from test_his_own_requests import his_turn, requests_of
from test_notes_of_domo import decide, waiting_requests
from test_own_idea import LIVE

PLAN = {"why": "Ask about the bins.", "on_return": True}
BOILER = {
    "content": "The boiler is noisy in the morning.",
    "subject": "the boiler",
    "kind": "fact",
    "private": False,
    "core": False,
}
# (action type, tool, its arguments, the model its call writes)
CASES = [
    pytest.param("plan.set", "plan_set", PLAN, Plan, id="plan"),
    pytest.param("memory.keep", "memory_keep", BOILER, Memory, id="memory"),
]


def call(tool: str, arguments: dict) -> ToolCall:
    return ToolCall("c1", tool, json.dumps(arguments))


async def written(session_factory, model) -> int:
    async with session_factory() as reader:
        return (await reader.scalar(select(func.count()).select_from(model))) or 0


@pytest.mark.parametrize(("action_type", "tool", "arguments", "model"), CASES)
async def test_on_his_own_he_waits_for_the_owner_s_yes_before_he_acts(
    action_type,
    tool,
    arguments,
    model,
    chat_server: ChatServer,
    owner_chat: ChatPilot,
    adult_chat: ChatPilot,
    session_factory,
    grant,
    tool_context,
):
    await grant(action_type, for_domo=True, ask="yes")
    await his_turn(chat_server, owner_chat, session_factory, call(tool, arguments))
    # Nothing happened yet: a request that is his waits for the owner, who alone may answer it.
    assert await written(session_factory, model) == 0
    [asked] = await waiting_requests(session_factory)
    assert (asked.by_domo, asked.approver_kind, asked.tool_name) == (True, "owner", tool)
    assert (asked.ask, asked.asked_by) == ("yes", "rule")
    assert may_decide(asked, owner_chat.person, LIVE)
    assert not may_decide(asked, adult_chat.person, LIVE)
    # On the owner's yes it is done, as his.
    outcome = await decide(
        chat_server, session_factory, tool_context, asked, owner_chat.person, approve=True
    )
    assert outcome == "executed"
    assert await written(session_factory, model) == 1


@pytest.mark.parametrize(("action_type", "tool", "arguments", "model"), CASES)
async def test_on_the_owner_s_no_nothing_is_done_and_nothing_else_happens(
    action_type,
    tool,
    arguments,
    model,
    chat_server: ChatServer,
    owner_chat: ChatPilot,
    session_factory,
    grant,
    tool_context,
):
    await grant(action_type, for_domo=True, ask="yes")
    await his_turn(chat_server, owner_chat, session_factory, call(tool, arguments))
    [asked] = await waiting_requests(session_factory)
    await decide(
        chat_server, session_factory, tool_context, asked, owner_chat.person, approve=False
    )
    assert await written(session_factory, model) == 0
    assert [c.status for c in await requests_of(session_factory)] == ["refused"]
    assert await waiting_requests(session_factory) == []


@pytest.mark.parametrize(("action_type", "tool", "arguments", "model"), CASES)
async def test_without_his_rule_the_call_runs_at_once(
    action_type,
    tool,
    arguments,
    model,
    chat_server: ChatServer,
    owner_chat: ChatPilot,
    session_factory,
):
    await his_turn(chat_server, owner_chat, session_factory, call(tool, arguments))
    assert await written(session_factory, model) == 1
    assert await requests_of(session_factory) == []


async def test_an_explicit_yes_is_asked_as_such_and_plain_words_do_not_give_it(
    chat_server: ChatServer,
    owner_chat: ChatPilot,
    session_factory,
    grant,
):
    await grant("plan.set", for_domo=True, ask="explicit_yes")
    await his_turn(chat_server, owner_chat, session_factory, call("plan_set", PLAN))
    [asked] = await waiting_requests(session_factory)
    assert (asked.ask, asked.asked_by, asked.by_domo) == ("explicit_yes", "rule", True)
    assert await written(session_factory, Plan) == 0
