"""A cost for a person is judged within their limit (Task 24, spec part 12).

When a tool launched for a person stops because an operation costs, and no agreement that lasts
covers it, the person's yes is no longer asked by default: within their spending limit (applied by
the code before the stop exists, never by the judge) the assistant's judge weighs it, with the
amount, the tool, the person's words, their agreements and their own earlier spendings of the
same kind. Usual: it runs, and the assistant tells them afterwards what was spent. Unusual: they
are asked. A silent judge asks them. An owner's "ask me first", an approver or a proof is never
the judge's; a launch for the house is unchanged."""

import json
import uuid
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from chat_support import ChatPilot, ChatServer
from conftest import FakeClock
from domo_core.accounts.actors import actor_of
from domo_core.accounts.models import Person
from domo_core.chat.models import Conversation, Message
from domo_core.confirmations.judge import Judgement
from domo_core.confirmations.models import Confirmation
from domo_core.conversation import execution as execution_module
from domo_core.conversation.execution import handle_tool_call
from domo_core.llm.types import ToolCall
from domo_core.madetools.models import DeclaredSpending, LaunchStop, StandingAgreement
from domo_core.madetools.offered import offer_named
from domo_core.madetools.yes import spent_like
from domo_core.tools.context import ToolContext
from domo_core.tools.registry import TOOLS
from fakes import FakeRefused, FakeSandbox, is_judge_call
from sqlalchemy import func, select
from test_madetools_handover import add_tool, by_program
from test_madetools_yes import ARGUMENTS, ORDER, Shop, deps_at, launched, policy_of, stops
from test_own_idea import Judged
from test_program_run import by_hand, writes_after

# What the operation that costs says, in the tool's own words.
SAYS = ORDER["order"]["says"]


def judge_cases(chat_server: ChatServer) -> list[dict[str, Any]]:
    """The cases the yes-judge was given in the turns of the pilots, in order."""
    return [
        json.loads(str(call.messages[0].content))
        for call in chat_server.calls
        if is_judge_call(call.system)
    ]


async def spent_before(
    session_factory, person: Person, amounts: list[int], *, clock: FakeClock, tool_id=None
) -> None:
    """Earlier spendings of ``person`` for the operation ``order`` (of the tool ``tool_id``; a
    program run by hand when ``None``), one a day, the last one yesterday."""
    async with session_factory() as writer:
        for days, amount in enumerate(reversed(amounts), start=1):
            writer.add(
                DeclaredSpending(
                    for_person_id=person.id,
                    tool_id=tool_id,
                    operation="order",
                    amount_cents=amount,
                    at=clock() - timedelta(days=days),
                )
            )
        await writer.commit()


async def requests(session_factory) -> int:
    async with session_factory() as reader:
        return await reader.scalar(select(func.count()).select_from(Confirmation)) or 0


async def a_stop_for(chat: ChatPilot, chat_server: ChatServer, shop: Shop) -> LaunchStop:
    """The tool ``order`` launched for the person of ``chat`` outside a turn, stopped for its
    cost; the server's sandbox runs ``shop`` from now on."""
    tool = await add_tool(chat_server.session_factory, "order", ORDER)
    chat_server.sandbox = by_program(order=shop)
    deps = deps_at(chat_server.session_factory, chat_server.clock, chat_server.sandbox, [])
    await launched(deps, tool.id, chat.person)
    [stop] = await stops(chat_server.session_factory)
    assert (stop.why, stop.approver, stop.ask) == ("costs", "self", "yes")
    return stop


async def test_a_usual_cost_they_just_asked_for_runs_and_is_told_after(
    adult_chat: ChatPilot, chat_server: ChatServer, add_member
):
    dana = adult_chat.person
    sam = await add_member("Sam", "adult")
    shop = Shop(price=2000)
    stop = await a_stop_for(adult_chat, chat_server, shop)
    assert stop.tool_id is not None
    clock = chat_server.clock
    await spent_before(
        chat_server.session_factory, dana, [1900, 2100, 2000], clock=clock, tool_id=stop.tool_id
    )
    # Someone else's spending is never in her case.
    await spent_before(chat_server.session_factory, sam, [9900], clock=clock, tool_id=stop.tool_id)
    reply = await adult_chat.say_with_tool(
        "t_order",
        {"yes_for": str(stop.id)},
        text="Order the pizzas, as every Friday.",
        judge="do: as usual",
    )
    # Run at once: no request, the yes is hers, taken once.
    assert reply.actions == [{"tool": "t_order", "status": "done"}]
    assert reply.pending_confirmation_id is None
    assert await requests(chat_server.session_factory) == 0
    assert shop.ordered == 1
    [used] = await stops(chat_server.session_factory)
    assert (used.yes_used_at is not None, used.yes_approver) == (True, "self")
    # The assistant reads that he tells her what was spent.
    [result] = reply.model_results
    assert result["launch"] == "done"
    assert (result["told_after"], result["amount_cents"]) == (True, 2000)
    # The judge weighed it with the amount, her words and her own earlier spendings.
    [case] = judge_cases(chat_server)
    cost = case["cost"]
    assert (cost["amount_cents"], cost["tool"], cost["operation"]) == (2000, "order", "order")
    assert [line["amount_cents"] for line in cost["spent_before"]] == [2000, 2100, 1900]
    assert cost["spent_before"][0]["day"] == (clock() - timedelta(days=1)).date().isoformat()
    assert cost["agreements"] == []
    assert "Order the pizzas, as every Friday." in case["words"]
    assert SAYS in json.dumps(case)


async def test_a_first_cost_waits_for_their_yes(adult_chat: ChatPilot, chat_server: ChatServer):
    dana = adult_chat.person
    shop = Shop(price=2000)
    stop = await a_stop_for(adult_chat, chat_server, shop)
    # An agreement that does not cover it (too low) is told to the judge, as data.
    async with chat_server.session_factory() as writer:
        assert stop.tool_id is not None
        writer.add(
            StandingAgreement(
                tool_id=stop.tool_id,
                operation="order",
                for_person_id=dana.id,
                amount_cents_at_most=1000,
                times=1,
                per="week",
                given_by=dana.id,
            )
        )
        await writer.commit()
    reply = await adult_chat.say_with_tool(
        "t_order", {"yes_for": str(stop.id)}, judge="yes: first time"
    )
    # Asked of her, as today: her own yes, asked by the assistant for what it costs.
    assert reply.actions == [{"tool": "t_order", "status": "waiting"}]
    [result] = reply.model_results
    assert (result["status"], result["by"], result["asked_by"], result["why"]) == (
        "waiting_for_confirmation",
        "you",
        "domo",
        SAYS,
    )
    assert "told_after" not in result
    assert reply.pending_confirmation_id is not None
    assert shop.ordered == 0
    [case] = judge_cases(chat_server)
    assert case["cost"]["spent_before"] == []
    [agreement] = case["cost"]["agreements"]
    assert (agreement["operation"], agreement["amount_cents_at_most"]) == ("order", 1000)


async def test_a_silent_judge_asks_them_and_a_refusal_runs_nothing(
    adult_chat: ChatPilot, chat_server: ChatServer
):
    shop = Shop(price=2000)
    stop = await a_stop_for(adult_chat, chat_server, shop)
    # A judge that gives no verdict: for a cost, the safe way, never a run.
    silent = await adult_chat.say_with_tool(
        "t_order", {"yes_for": str(stop.id)}, judge="hmm, hard to say"
    )
    assert silent.actions == [{"tool": "t_order", "status": "waiting"}]
    assert shop.ordered == 0
    refused = await adult_chat.say_with_tool(
        "t_order", {"yes_for": str(stop.id)}, judge="do_not: far more than usual"
    )
    assert refused.actions == [{"tool": "t_order", "status": "refused", "reason": "judge"}]
    assert refused.model_results[0]["why"] == "far more than usual"
    assert shop.ordered == 0
    [kept] = await stops(chat_server.session_factory)
    assert kept.yes_used_at is None


async def test_over_their_limit_it_never_reaches_the_judge(
    adult_chat: ChatPilot, chat_server: ChatServer, grant
):
    dana = adult_chat.person
    stop = await a_stop_for(adult_chat, chat_server, Shop(price=3250))
    # The owner's limit on the tool reads the amount of the yes: refused before any judge.
    await grant(
        "made.order",
        person=dana,
        limit=(
            "action.facts.amount_cents != null && action.facts.amount_cents > 3000"
            ' ? "refuse" : "allow"'
        ),
    )
    reply = await adult_chat.say_with_tool("t_order", {"yes_for": str(stop.id)}, judge="do")
    assert reply.actions == [{"tool": "t_order", "status": "refused", "reason": "limit"}]
    # Her spending limit refuses the begin itself: no stop, nothing to judge.
    await grant(
        "program.begin",
        person=dana,
        limit=(
            "action.facts.amount_cents != null && "
            'action.facts.amount_cents + spent.month > 1000 ? "refuse" : "allow"'
        ),
    )
    await adult_chat.say_with_tool("t_order", dict(ARGUMENTS), judge="do")
    assert len(await stops(chat_server.session_factory)) == 1
    assert judge_cases(chat_server) == []


async def test_an_owner_s_ask_me_first_is_not_judged(
    adult_chat: ChatPilot, chat_server: ChatServer, grant
):
    dana = adult_chat.person
    shop = Shop(price=2000)
    stop = await a_stop_for(adult_chat, chat_server, shop)
    # The owner's "ask me first" on the tool: it waits as his rule says, never judged.
    await grant("made.order", person=dana, ask="yes")
    reply = await adult_chat.say_with_tool("t_order", {"yes_for": str(stop.id)}, judge="do")
    assert reply.actions == [{"tool": "t_order", "status": "waiting"}]
    assert shop.ordered == 0
    assert judge_cases(chat_server) == []


async def test_an_owner_s_rule_on_spending_is_not_judged(
    adult_chat: ChatPilot, chat_server: ChatServer, grant
):
    dana = adult_chat.person
    tool = await add_tool(chat_server.session_factory, "order", ORDER)
    shop = Shop(price=2000)
    chat_server.sandbox = by_program(order=shop)
    deps = deps_at(chat_server.session_factory, chat_server.clock, chat_server.sandbox, [])
    await grant("program.begin", person=dana, ask="yes")
    await launched(deps, tool.id, dana)
    [stop] = await stops(chat_server.session_factory)
    assert stop.why == "owner_rule"
    reply = await adult_chat.say_with_tool("t_order", {"yes_for": str(stop.id)}, judge="do")
    assert reply.actions == [{"tool": "t_order", "status": "waiting"}]
    assert shop.ordered == 0
    assert judge_cases(chat_server) == []


async def test_a_hand_run_s_cost_is_judged_the_same(adult_chat: ChatPilot, chat_server: ChatServer):
    dana = adult_chat.person
    clock = chat_server.clock
    await spent_before(chat_server.session_factory, dana, [2000, 2000], clock=clock)
    chat_server.sandbox = FakeSandbox(program=writes_after("order", 2000))
    await adult_chat.say_with_tool("program_run", by_hand(operations={"order": ORDER["order"]}))
    [stop] = await stops(chat_server.session_factory)
    assert (stop.hand_run_id is not None, stop.why, stop.approver) == (True, "costs", "self")
    reply = await adult_chat.say_with_tool(
        "program_run", {"yes_for": str(stop.id)}, judge="do: as usual"
    )
    assert reply.actions == [{"tool": "program_run", "status": "done"}]
    [result] = reply.model_results
    assert (result["told_after"], result["amount_cents"]) == (True, 2000)
    assert len(chat_server.sandbox.runs) == 2
    [case] = judge_cases(chat_server)
    cost = case["cost"]
    # A program run by hand: no tool, and no agreement is invented for it. Nor any habit (ruling
    # R63): earlier programs that named an operation the same are other programs, and a new one
    # could borrow their name; its cost passes only on the person's own words.
    assert (cost["tool"], cost["operation"], cost["agreements"]) == (None, "order", [])
    assert cost["spent_before"] == []


async def test_a_run_that_failed_is_never_told_as_spent(
    adult_chat: ChatPilot, chat_server: ChatServer
):
    stop = await a_stop_for(adult_chat, chat_server, Shop(price=2000))

    async def broken(request: Any, ask: Any) -> Any:
        # Fails before it begins anything: nothing is declared.
        raise FakeRefused("broken")

    chat_server.sandbox = by_program(order=broken)
    reply = await adult_chat.say_with_tool(
        "t_order", {"yes_for": str(stop.id)}, judge="do: as usual"
    )
    [result] = reply.model_results
    assert result["launch"] == "failed"
    assert "told_after" not in result and "amount_cents" not in result
    async with chat_server.session_factory() as reader:
        assert (await reader.scalar(select(func.count()).select_from(DeclaredSpending))) == 0


async def test_for_the_house_nothing_changed(chat_server: ChatServer, tool_context: ToolContext):
    tool = await add_tool(chat_server.session_factory, "order", ORDER)
    deps = deps_at(chat_server.session_factory, chat_server.clock, by_program(order=Shop()), [])
    await launched(deps, tool.id, None)
    [stop] = await stops(chat_server.session_factory)
    assert (stop.for_person_id, stop.why, stop.approver) == (None, "costs", "self")
    offer = await offer_named(chat_server.session_factory, deps, "t_order")
    assert offer is not None
    # On his own, he answers it himself: it waits for nobody, and nothing is judged.
    alone = replace(tool_context, on_his_own=True)
    assert await offer.waits_for({"yes_for": str(stop.id)}, alone) is None  # type: ignore[attr-defined]


async def test_spent_like_reads_only_their_own_lines_of_this_kind_the_latest_first(
    session_factory, add_member, clock
):
    dana = await add_member("Dana", "adult")
    sam = await add_member("Sam", "adult")
    tool = await add_tool(session_factory, "order", ORDER)
    await spent_before(
        session_factory, dana, [100, 200, 300, 400, 500, 600], clock=clock, tool_id=tool.id
    )
    await spent_before(session_factory, sam, [999], clock=clock, tool_id=tool.id)
    await spent_before(session_factory, dana, [777], clock=clock)
    async with session_factory() as session:
        found = await spent_like(session, person_id=dana.id, tool_id=tool.id, operation="order")
        by_hand_run = await spent_like(session, person_id=dana.id, tool_id=None, operation="order")
        other = await spent_like(session, person_id=dana.id, tool_id=tool.id, operation="follow")
    assert [amount for _, amount in found] == [600, 500, 400, 300, 200]
    assert found[0][0] == (clock() - timedelta(days=1)).date()
    assert [amount for _, amount in by_hand_run] == [777]
    assert other == []


async def _reply_in(session_factory, person: Person) -> tuple[uuid.UUID, uuid.UUID]:
    """``person``'s conversation and a reply of the assistant in it, committed."""
    async with session_factory() as writer:
        conversation = (
            await writer.scalars(select(Conversation).where(Conversation.person_id == person.id))
        ).first()
        if conversation is None:
            conversation = Conversation(person_id=person.id)
            writer.add(conversation)
            await writer.flush()
        reply = Message(
            conversation_id=conversation.id,
            role="assistant",
            content="",
            status="complete",
            provenance="model",
        )
        writer.add(reply)
        await writer.commit()
        return conversation.id, reply.id


async def test_in_a_turn_he_started_a_silent_judge_asks_them(
    adult_chat: ChatPilot,
    chat_server: ChatServer,
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
):
    dana = adult_chat.person
    shop = Shop(price=2000)
    stop = await a_stop_for(adult_chat, chat_server, shop)
    deps = deps_at(chat_server.session_factory, chat_server.clock, chat_server.sandbox, [])
    offer = await offer_named(chat_server.session_factory, deps, "t_order")
    assert offer is not None
    # A judge that did not answer, whose fallback would let a request of theirs run.
    judged = Judged(Judgement("do", "", "error"))
    monkeypatch.setattr(execution_module, "judge", judged)
    conversation_id, reply_id = await _reply_in(chat_server.session_factory, dana)
    ctx = replace(
        tool_context,
        person=dana,
        actor=actor_of(dana),
        conversation_id=conversation_id,
        reply_message_id=None,
        turn_reason="the pizzas of the week are due",
        registry={**TOOLS, "t_order": offer},
        policy=await policy_of(chat_server.session_factory),
    )
    call = ToolCall("c1", "t_order", json.dumps({"yes_for": str(stop.id)}))
    record = await handle_tool_call(call, ctx=ctx, reply_id=reply_id)
    assert record.status == "waiting"
    assert shop.ordered == 0
    # Nobody just asked: the case says it is his idea, and weighs her habit.
    [case] = judged.cases
    assert case.idea is not None and case.idea.of == "domo"
    assert case.cost is not None and case.cost["amount_cents"] == 2000
