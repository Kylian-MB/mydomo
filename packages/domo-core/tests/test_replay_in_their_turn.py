"""The owner's yes replays what it was asked for (re-review of the fix wave): a request born of
the owner's "ask me first" on an act of the assistant in a person's turn runs in that turn, for
that person (R1); on his own, a right of his on ``house.set`` that is no "at once" is set by the
owner's yes (R2). Through the real tools and the real ``decide_confirmation``."""

import json
from typing import Any

import pytest
from chat_support import ChatPilot, ChatServer, Member
from domo_core.config import CoreSettings
from domo_core.confirmations.judge import Judgement
from domo_core.confirmations.service import amend_confirmation
from domo_core.conversation import starts as starts_module
from domo_core.conversation.execution import handle_tool_call
from domo_core.conversation.prepare import NoKey
from domo_core.conversation.starts import wake_after_decision, wake_before_expiry
from domo_core.gatekeeper_client import GatekeeperClient
from domo_core.llm.types import ToolCall
from domo_core.madetools.models import StandingAgreement
from domo_core.policy.models import PolicyRight
from fakes import FakeChatModel, FakeSandbox
from sqlalchemy import select
from test_agreements import ORDER
from test_concerned_person import _judge, _requests, _turn
from test_conversation_starts import core_deps
from test_expiring import HOUR, LIFETIME, put_in_his_conversation
from test_made_tools_tools import _the_house, _timed, _yes, trigger_rows
from test_madetools_handover import add_tool as add_order_tool
from test_own_idea import LIVE
from test_program_run import by_hand, doubles, hand_runs

WHEN = "2030-01-08T08:00:00+00:00"


async def _right(session_factory: Any, by: Any, action_type: str, **right: Any) -> None:
    async with session_factory() as writer:
        writer.add(
            PolicyRight(
                for_domo=True,
                action_type=action_type,
                summary="The owner's rule on the assistant.",
                owner_words="ask me first",
                created_by=by.id,
                **right,
            )
        )
        await writer.commit()


@pytest.mark.parametrize("arguments", [{}, {"for": "me"}], ids=["default", "for_me"])
async def test_dana_s_trigger_under_the_owner_s_rule_stays_hers_after_his_yes(
    session_factory, arguments
):
    people = await _the_house(session_factory)
    alex, dana = people["Alex"], people["Dana"]
    await _right(session_factory, alex, "tool.launch_when", ask="yes")
    ctx, reply_id, _ = await _turn(session_factory, dana)
    call = _timed({"name": "chores", "when": WHEN, **arguments})
    record = await handle_tool_call(call, ctx=ctx, reply_id=reply_id)
    assert record.status == "waiting", record.model_result
    [request] = await _requests(session_factory)
    outcome, ran = await _yes(ctx, request.id, alex)
    assert outcome == "executed", ran
    [trigger] = await trigger_rows(session_factory)
    # Dana's, as she asked it: never the house's.
    assert trigger.for_person_id == dana.id


async def test_alex_s_own_trigger_under_his_rule_is_his_after_his_yes(session_factory):
    people = await _the_house(session_factory)
    alex = people["Alex"]
    await _right(session_factory, alex, "tool.launch_when", ask="yes")
    ctx, reply_id, _ = await _turn(session_factory, alex)
    record = await handle_tool_call(
        _timed({"name": "chores", "when": WHEN}), ctx=ctx, reply_id=reply_id
    )
    assert record.status == "waiting", record.model_result
    [request] = await _requests(session_factory)
    outcome, ran = await _yes(ctx, request.id, alex)
    assert outcome == "executed", ran
    [trigger] = await trigger_rows(session_factory)
    assert trigger.for_person_id == alex.id


async def test_a_hand_run_under_the_owner_s_rule_stays_the_person_s(
    adult_chat: ChatPilot, owner_chat: ChatPilot, chat_server: ChatServer, grant
):
    await grant("program.run", for_domo=True, ask="yes")
    chat_server.sandbox = FakeSandbox(program=doubles)
    reply = await adult_chat.say_with_tool("program_run", by_hand())
    assert reply.actions == [{"tool": "program_run", "status": "waiting"}], reply.model_results
    response = await owner_chat.post(f"/confirmations/{reply.pending_confirmation_id}/approve")
    assert response.status_code == 200 and 'data-outcome="executed"' in response.text
    [run] = await hand_runs(chat_server.session_factory)
    assert run.for_person_id == adult_chat.person.id


async def _his_house_set_asks_first(session_factory: Any) -> dict:
    people = await _the_house(session_factory)
    await _right(session_factory, people["Alex"], "house.set", access="allow", ask="yes")
    return people


async def _his_own_turn(session_factory: Any, people: dict) -> Any:
    ctx, reply_id, _ = await _turn(
        session_factory, people["Alex"], on_his_own=True, reply_message_id=None
    )
    return ctx, reply_id


async def test_on_his_own_a_trigger_for_the_house_is_set_by_the_owner_s_yes(session_factory):
    people = await _his_house_set_asks_first(session_factory)
    ctx, reply_id = await _his_own_turn(session_factory, people)
    record = await handle_tool_call(
        _timed({"name": "chores", "when": WHEN}), ctx=ctx, reply_id=reply_id
    )
    assert record.status == "waiting", record.model_result
    [request] = await _requests(session_factory)
    outcome, ran = await _yes(ctx, request.id, people["Alex"])
    assert outcome == "executed", ran
    [trigger] = await trigger_rows(session_factory)
    assert trigger.for_person_id is None


async def test_on_his_own_cancelling_a_house_trigger_is_done_by_the_owner_s_yes(
    session_factory,
):
    people = await _the_house(session_factory)
    alex = people["Alex"]
    ctx, reply_id, _ = await _turn(session_factory, alex)
    await handle_tool_call(_timed({**_HOUSE}), ctx=ctx, reply_id=reply_id)
    [trigger] = await trigger_rows(session_factory)
    await _right(session_factory, alex, "house.set", access="allow", ask="yes")
    his, his_reply = await _his_own_turn(session_factory, people)
    cancel = _timed({"name": "chores", "cancel": str(trigger.id)})
    record = await handle_tool_call(cancel, ctx=his, reply_id=his_reply)
    assert record.status == "waiting", record.model_result
    [request] = await _requests(session_factory)
    outcome, ran = await _yes(his, request.id, alex)
    assert outcome == "executed", ran
    [after] = await trigger_rows(session_factory)
    assert after.state == "cancelled"


_HOUSE = {"name": "chores", "when": WHEN, "for": "house"}
_AGREEMENT = {
    "tool": "t_order",
    "operation": "order",
    "amount_cents_at_most": 4000,
    "times": 1,
    "per": "week",
    "for": "house",
}


async def _agreements(session_factory: Any) -> list[StandingAgreement]:
    async with session_factory() as reader:
        return list(await reader.scalars(select(StandingAgreement)))


async def test_on_his_own_an_agreement_for_the_house_is_kept_then_dropped_by_the_owner_s_yes(
    session_factory, monkeypatch
):
    people = await _his_house_set_asks_first(session_factory)
    alex = people["Alex"]
    await add_order_tool(session_factory, "order", ORDER)
    _judge(monkeypatch, Judgement("do"))
    ctx, reply_id = await _his_own_turn(session_factory, people)
    record = await handle_tool_call(
        ToolCall("k", "agreement_keep", json.dumps(_AGREEMENT)), ctx=ctx, reply_id=reply_id
    )
    assert record.status == "waiting", record.model_result
    [request] = await _requests(session_factory)
    outcome, ran = await _yes(ctx, request.id, alex)
    assert outcome == "executed", ran
    [kept] = await _agreements(session_factory)
    assert kept.for_person_id is None and kept.withdrawn_at is None
    drop = ToolCall("d", "agreement_drop", json.dumps({"id": str(kept.id)}))
    record = await handle_tool_call(drop, ctx=ctx, reply_id=reply_id)
    assert record.status == "waiting", record.model_result
    dropping = next(c for c in await _requests(session_factory) if c.id != request.id)
    outcome, ran = await _yes(ctx, dropping.id, alex)
    assert outcome == "executed", ran
    [gone] = await _agreements(session_factory)
    assert gone.withdrawn_at is not None


async def test_a_deny_of_house_set_written_for_him_still_refuses(session_factory):
    people = await _the_house(session_factory)
    await _right(session_factory, people["Alex"], "house.set", access="deny")
    ctx, reply_id = await _his_own_turn(session_factory, people)
    record = await handle_tool_call(
        _timed({"name": "chores", "when": WHEN}), ctx=ctx, reply_id=reply_id
    )
    assert record.status != "done" and await trigger_rows(session_factory) == []


# --- RC1: the mark holds through a change and a wake -------------------------------------------


async def _danas_trigger_under_the_rule(session_factory: Any) -> tuple[dict, Any, Any]:
    people = await _the_house(session_factory)
    await _right(session_factory, people["Alex"], "tool.launch_when", ask="yes")
    ctx, reply_id, _ = await _turn(session_factory, people["Dana"])
    record = await handle_tool_call(
        _timed({"name": "chores", "when": WHEN}), ctx=ctx, reply_id=reply_id
    )
    assert record.status == "waiting", record.model_result
    [request] = await _requests(session_factory)
    assert request.in_their_turn
    return people, ctx, request


async def _alex_says_yes_but(session_factory: Any, alex: Any, request: Any) -> tuple[Any, Any]:
    ctx, reply_id, _ = await _turn(session_factory, alex, change_grant=request.id)
    ctx.domo_message_id = reply_id
    ctx.reply_message_id = None
    outcome, _, new = await amend_confirmation(
        session_factory=session_factory,
        confirmation_id=request.id,
        new_arguments={"when": "2030-01-09T08:00:00+00:00"},
        ask_again=False,
        ctx=ctx,
        now=LIVE,
    )
    return outcome, new


async def test_a_change_asked_again_stays_dana_s_at_the_owner_s_yes(session_factory, monkeypatch):
    people, ctx, request = await _danas_trigger_under_the_rule(session_factory)
    _judge(monkeypatch, Judgement("yes", reason="sure?"))
    outcome, new = await _alex_says_yes_but(session_factory, people["Alex"], request)
    assert outcome == "asked_again", outcome
    assert new is not None and new.in_their_turn and new.by_domo
    outcome, ran = await _yes(ctx, new.id, people["Alex"])
    assert outcome == "executed", ran
    [trigger] = await trigger_rows(session_factory)
    assert trigger.for_person_id == people["Dana"].id


async def test_a_change_the_owner_agrees_to_runs_as_dana_s(session_factory, monkeypatch):
    people, _, request = await _danas_trigger_under_the_rule(session_factory)
    _judge(monkeypatch, Judgement("do"))
    outcome, _ = await _alex_says_yes_but(session_factory, people["Alex"], request)
    assert outcome == "executed", outcome
    [trigger] = await trigger_rows(session_factory)
    assert trigger.for_person_id == people["Dana"].id


def _seen_wakes(monkeypatch: Any) -> list[bool]:
    seen: list[bool] = []

    async def prepare(**kwargs: Any) -> Any:
        seen.append(kwargs["on_his_own"])
        raise NoKey()

    monkeypatch.setattr(starts_module, "prepare_turn", prepare)
    return seen


async def test_her_request_s_wakes_start_in_her_turn(
    chat_server: ChatServer,
    adult_member: Member,
    owner_member: Member,
    settings: CoreSettings,
    gk_client: GatekeeperClient,
    monkeypatch,
):
    request_id, _ = await put_in_his_conversation(
        chat_server,
        adult_member.person,
        waiting=LIFETIME - 30 * HOUR,
        approver_kind="owner",
        by_domo=True,
        in_their_turn=True,
    )
    seen = _seen_wakes(monkeypatch)
    deps = core_deps(chat_server, FakeChatModel(), settings, gk_client)
    await wake_before_expiry(deps, request_id)
    await wake_after_decision(
        deps,
        confirmation_id=request_id,
        outcome="executed",
        record=None,
        decider=owner_member.person,
    )
    assert seen == [False, False]
