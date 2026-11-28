"""Who may agree, who is asked (final review of the judge-first branch): the owner's "ask me
first" on an act of the assistant waits for a yes someone can give, in the owner's own turn too
(B1); the person concerned is read again at the yes (B2); a chain of his own sent back for a plain
yes waits for the owner (B5); one rule says when an adult agrees for a child (A5)."""

import json
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

import domo_core
from domo_core.confirmations.chain import RestAsked
from domo_core.confirmations.service import decide_confirmation, may_decide, requester_context
from domo_core.confirmations.steps import PlannedStep
from domo_core.conversation.execution import handle_tool_call
from domo_core.llm.types import JsonObject, ToolCall, ToolDefinition
from domo_core.policy.actions import DescribedAction
from domo_core.policy.barrier import Allow
from domo_core.policy.catalog import DEFAULT_POLICY, an_adult_agrees_for
from domo_core.policy.models import PolicyRight
from domo_core.policy.rights import NO_RIGHTS
from domo_core.policy.roles import Actor, domo_actor
from domo_core.tools.chain import _the_rest_waits
from domo_core.tools.context import ToolContext, ToolOutcome
from test_concerned_person import DOUBT, _judge, _people, _requests, _turn
from test_own_idea import LIVE

SOURCE = Path(domo_core.__file__).parent
BY_HAND = {"code": "def run(entry):\n    return 1\n", "arguments": {}, "operations": {}}


async def _ask_me_first_on_his_runs(session_factory: Any, by: Any) -> None:
    async with session_factory() as writer:
        writer.add(
            PolicyRight(
                for_domo=True,
                action_type="program.run",
                ask="yes",
                summary="Ask me before the assistant runs a program.",
                owner_words="ask me first",
                created_by=by.id,
            )
        )
        await writer.commit()


def run_by_hand() -> ToolCall:
    return ToolCall("p", "program_run", json.dumps(BY_HAND))


async def test_the_owner_s_rule_on_his_act_in_the_owner_s_own_turn_is_the_owner_s_to_answer(
    session_factory,
):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"), grants=False)
    alex = people["Alex"]
    await _ask_me_first_on_his_runs(session_factory, alex)
    ctx, reply_id, _ = await _turn(session_factory, alex)
    record = await handle_tool_call(run_by_hand(), ctx=ctx, reply_id=reply_id)
    assert record.status == "waiting", record.model_result
    [request] = await _requests(session_factory)
    # His request, the owner's to answer: never one nobody may say yes to.
    assert (request.by_domo, request.approver_kind) == (True, "owner")
    assert may_decide(request, alex, LIVE, rights=NO_RIGHTS, policy=DEFAULT_POLICY)


async def test_the_owner_s_rule_on_his_act_in_dana_s_turn_takes_the_owner_s_yes_alone(
    session_factory,
):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"), grants=False)
    alex, dana = people["Alex"], people["Dana"]
    await _ask_me_first_on_his_runs(session_factory, alex)
    ctx, reply_id, _ = await _turn(session_factory, dana)
    await handle_tool_call(run_by_hand(), ctx=ctx, reply_id=reply_id)
    [request] = await _requests(session_factory)
    assert (request.by_domo, request.approver_kind) == (True, "owner")
    assert may_decide(
        request, alex, LIVE, rights=NO_RIGHTS, policy=DEFAULT_POLICY
    ) and not may_decide(request, dana, LIVE, rights=NO_RIGHTS, policy=DEFAULT_POLICY)


class TakeBackMoving:
    """A test-only tool: take back a device, whose owner is read when it is described
    (``belongs_to``, which a test changes: the device was entrusted to someone else)."""

    belongs_to: dict[str, uuid.UUID] = {}
    definition = ToolDefinition(
        name="take_back_moving",
        description="Take back a device.",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
    )
    action_type = "device.remove"

    async def describe(self, arguments: JsonObject, actor: Actor, ctx: Any) -> DescribedAction:
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target="device:tablet",
            effect="self_profile",
            destination="household",
            facts={"person": f"person:{self.belongs_to['now']}", "is_self": False},
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        return ToolOutcome("done", {}, {"key": "action.card.done", "params": {}})


async def test_the_person_concerned_is_read_again_at_the_yes(session_factory, monkeypatch):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"), ("Tom", "adult"))
    alex, dana, tom = people["Alex"], people["Dana"], people["Tom"]
    tool = TakeBackMoving()
    tool.belongs_to = {"now": dana.id}
    _judge(monkeypatch, DOUBT)
    ctx, reply_id, _ = await _turn(session_factory, alex, on_his_own=True, reply_message_id=None)
    ctx = replace(ctx, registry={**ctx.registry, "take_back_moving": tool})
    record = await handle_tool_call(
        ToolCall("t", "take_back_moving", "{}"), ctx=ctx, reply_id=reply_id
    )
    assert record.status == "waiting"
    [request] = await _requests(session_factory)
    assert request.concerned_id == dana.id
    # Meanwhile the tablet is entrusted to Tom: Dana's yes no longer answers for it.
    tool.belongs_to["now"] = tom.id
    outcome, _ = await decide_confirmation(
        session_factory=session_factory,
        confirmation_id=request.id,
        decider=dana,
        approve=True,
        via="button",
        reply_message_id=None,
        ctx_factory=requester_context(ctx),
        registry=ctx.registry,
        policy=DEFAULT_POLICY,
        now=LIVE,
    )
    assert outcome == "refused"


LAST_KEY = DescribedAction(
    "core",
    "key.remove",
    "provider:p-x",
    "secrets",
    "household",
    {"last_key": True, "general_key_without_heir": True},
)


async def test_his_chain_sent_back_for_a_plain_yes_waits_for_the_owner(session_factory):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"), grants=False)
    alex, dana = people["Alex"], people["Dana"]
    ctx, reply_id, _ = await _turn(session_factory, alex, on_his_own=True, reply_message_id=None)
    ctx.domo_message_id = reply_id
    steps = [PlannedStep("key_remove", "key.remove", {"key": "p-x"}, LAST_KEY)]
    # A step of his chain on his own became the last key: sent back for a plain yes.
    asked = RestAsked(0, domo_actor(), LAST_KEY, Allow(), (), plain_yes=True)
    request_id = await _the_rest_waits(ctx, "do_in_order", steps, asked)
    [request] = await _requests(session_factory)
    assert request.id == request_id
    assert (request.by_domo, request.approver_kind) == (True, "owner")
    assert may_decide(
        request, alex, LIVE, rights=NO_RIGHTS, policy=DEFAULT_POLICY
    ) and not may_decide(request, dana, LIVE, rights=NO_RIGHTS, policy=DEFAULT_POLICY)


def test_one_rule_says_when_an_adult_agrees_for_a_child():
    assert an_adult_agrees_for(True, "household.invite")
    assert not an_adult_agrees_for(True, "device.remove")
    assert not an_adult_agrees_for(False, "household.invite")
    # Neither the barrier nor who answers for a thing branch on a role's name for it.
    for module in ("policy/barrier.py", "policy/subjects.py"):
        assert 'role == "child"' not in (SOURCE / module).read_text(encoding="utf-8"), module
