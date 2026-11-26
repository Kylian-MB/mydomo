"""A program's operation that needs a yes, the yes that lets one relaunch through, and the
agreements that last (Task 8, spec 5.3, 5.8; the tools are Domo's, 2026-11-18).

A tool declares its operations and their risks; each request of its program belongs to the
operation it began last. When one needs a yes nothing is done: the run stops, the stop is kept
(``launch_stops``) and Domo is woken for whom the launch is (the owner, for the house) with what
was to be done. The yes runs it again once (``take_yes``, ``launch(..., yes=...)``). A standing
agreement lets a costly operation through without a yes, within its bounds, counted under a row
lock. The programs are coroutines given to a ``FakeSandbox``; the tools' base is the test's own."""

import asyncio
import contextlib
import json
import re
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

import pytest
from chat_support import ChatPilot, ChatServer
from conftest import FakeClock
from domo_common.i18n import t
from domo_common.sandbox_api import JsonObject, RunRequest
from domo_core.accounts.models import Person
from domo_core.channels.turns import app_turn_deps
from domo_core.confirmations.describe import describe_type
from domo_core.confirmations.models import Confirmation
from domo_core.confirmations.service import decide_confirmation, requester_context
from domo_core.conversation.prepare import made_tool_deps
from domo_core.conversation.starts import Wakes
from domo_core.llm.types import ToolCall
from domo_core.madetools import handover
from domo_core.madetools import means as means_module
from domo_core.madetools.handover import AskingTool, launch_in_a_turn
from domo_core.madetools.launch import HOUSE, ForWhom, LaunchDeps, Launched, launch
from domo_core.madetools.means import STOPPED, Chain, LaunchScope, Means
from domo_core.madetools.models import DeclaredSpending, LaunchStop, StandingAgreement
from domo_core.madetools.offered import STOPPED_NEXT, offer_named, outcome_of
from domo_core.madetools.risks import OperationsNotRight, read_operations
from domo_core.madetools.settings import set_house
from domo_core.madetools.yes import take_yes
from domo_core.policy.actions import DescribedAction
from domo_core.policy.catalog import DEFAULT_POLICY, Risks
from domo_core.policy.facts import STOPPED_FOR_A_YES, decision_facts
from domo_core.policy.open_catalog import load_policy
from domo_core.policy.roles import Actor, as_role, domo_actor
from domo_core.prompts import ToolWake
from domo_core.texts.view import CatalogTexts
from domo_core.tools.context import InvalidArguments, ToolContext
from domo_core.tools.registry import TOOLS
from fakes import FakeAsk, FakeChatModel, FakeRefused, FakeSandbox, is_judge_call
from pydantic import JsonValue
from sqlalchemy import select
from test_madetools_handover import Woken, add_tool, all_audit, audit_of, by_program, deps_of

ORDER: dict[str, Any] = {
    "order": {"says": "Order the pizzas of the week.", "risks": ["costs", "outward"]},
    "follow": {"says": "Follow the order.", "risks": []},
}
ARGUMENTS: JsonObject = {"n": 2}
# A Friday, and the household's zone is not set: UTC.
FRIDAY = datetime(2030, 1, 4, 18, 0, tzinfo=UTC)


@pytest.fixture
async def alex(add_member) -> Person:
    return await add_member("Alex", "owner")


@pytest.fixture
async def dana(add_member, alex) -> Person:
    return await add_member("Dana", "adult")


class Shop:
    """The program of ``order``: it begins ``order`` at the price of the moment, then writes the
    order in its corner of the base and gives back how many orders it holds."""

    def __init__(self, price: int = 3250) -> None:
        self.price = price
        self.ordered = 0

    async def __call__(self, request: RunRequest, ask: FakeAsk) -> JsonValue:
        await ask({"do": "begin", "operation": "order", "amount_cents": self.price})
        self.ordered += 1
        return self.ordered


def deps_at(session_factory, clock, sandbox: FakeSandbox, woken: Woken) -> LaunchDeps:
    return deps_of(session_factory, clock, sandbox, woken)


async def launched(
    deps: LaunchDeps, tool_id: uuid.UUID, person: Person | None, yes: LaunchStop | None = None
) -> Launched:
    for_whom = HOUSE if person is None else ForWhom(person)
    return await launch(
        deps, tool_id, arguments=ARGUMENTS, for_whom=for_whom, cause="trigger", yes=yes
    )


async def stops(session_factory) -> list[LaunchStop]:
    async with session_factory() as reader:
        return list(
            (await reader.scalars(select(LaunchStop).order_by(LaunchStop.created_at))).all()
        )


async def spending(session_factory) -> list[DeclaredSpending]:
    async with session_factory() as reader:
        found = await reader.scalars(select(DeclaredSpending).order_by(DeclaredSpending.at))
        return list(found.all())


async def taken(
    session_factory,
    stop_id: uuid.UUID,
    tool_id: uuid.UUID,
    person: Person | None,
    arguments: JsonObject = ARGUMENTS,
) -> LaunchStop | None:
    """The yes of the stop ``stop_id`` taken as its rightful approver would give it (the
    confirmation path, tested through ``decide_confirmation``, decides who that is)."""
    async with session_factory() as writer:
        asked = await writer.get(LaunchStop, stop_id)
        row = await take_yes(
            writer,
            stop_id,
            tool_id=tool_id,
            for_person_id=None if person is None else person.id,
            arguments=arguments,
            given_by="self" if asked is None else asked.approver,
        )
        await writer.commit()
        return row


async def keep_agreement(session_factory, tool_id: uuid.UUID, by: Person, **given: Any) -> None:
    async with session_factory() as writer:
        writer.add(
            StandingAgreement(
                tool_id=tool_id,
                operation="order",
                for_person_id=given.pop("for_person_id", by.id),
                amount_cents_at_most=given.pop("amount", 4000),
                times=given.pop("times", 1),
                per=given.pop("per", "week"),
                weekdays=given.pop("weekdays", None),
                given_by=by.id,
            )
        )
        await writer.commit()


def needs_yes(stop: LaunchStop, says: str = ORDER["order"]["says"]) -> JsonValue:
    return {
        "needs_yes": {
            "stop": str(stop.id),
            "says": says,
            "amount_cents": stop.amount_cents,
            "why": stop.why,
            "by": stop.approver,
        }
    }


# --- the operations ------------------------------------------------------------------------------


def test_operations_are_read_with_their_risks():
    read = read_operations(ORDER)
    assert read["order"].risks == Risks(costs=True, outward=True)
    assert read["follow"].says == "Follow the order."
    for wrong in (
        {},
        {"Order": {"says": "x", "risks": []}},
        {"order": {"says": "two\nlines", "risks": []}},
        {"order": {"says": "x", "risks": ["dangerous"]}},
        {"order": {"says": "x" * 201, "risks": []}},
        {f"op{n}": {"says": "x", "risks": []} for n in range(11)},
    ):
        with pytest.raises(OperationsNotRight):
            read_operations(wrong)


# --- the stop and the yes ------------------------------------------------------------------------


async def test_order_stops_and_domo_is_woken_for_dana_with_what_was_to_be_done(
    session_factory, live_clock, dana
):
    tool = await add_tool(session_factory, "order", ORDER)
    shop = Shop()
    woken: Woken = []
    sandbox = by_program(order=shop)
    deps = deps_at(session_factory, live_clock, sandbox, woken)
    came = await launched(deps, tool.id, dana)
    # Nothing was done past the begin: the run stopped there.
    assert came.status == "stopped" and shop.ordered == 0
    [stop] = await stops(session_factory)
    assert came.result == {
        "reason": "needs_yes",
        "stop": str(stop.id),
        "says": ORDER["order"]["says"],
        "amount_cents": 3250,
        "why": "costs",
        "by": "self",
    }
    assert (stop.tool_id, stop.for_person_id, stop.operation, stop.amount_cents) == (
        tool.id,
        dana.id,
        "order",
        3250,
    )
    assert (stop.why, stop.approver, stop.ask) == ("costs", "self", "yes")
    assert stop.arguments == ARGUMENTS
    assert stop.yes_used_at is None
    # Domo is woken for Dana with what was to be done, as data.
    assert woken == [(dana.id, [ToolWake("order", needs_yes(stop))])]
    assert await spending(session_factory) == []
    # The audit says a launch stopped, never its arguments nor what the operation says.
    [entry] = await audit_of(session_factory, STOPPED)
    assert entry.actor == f"person:{dana.id}"
    assert entry.details == {
        "tool": "order",
        "operation": "order",
        "amount_cents": 3250,
        "why": "costs",
        "approver": "self",
        "ask": "yes",
    }
    audited = json.dumps([e.details for e in await all_audit(session_factory)])
    assert "pizzas" not in audited


async def test_the_yes_serves_once_for_these_arguments_and_amount(
    session_factory, live_clock, dana, alex
):
    tool = await add_tool(session_factory, "order", ORDER)
    other = await add_tool(session_factory, "other", ORDER)
    shop = Shop()
    woken: Woken = []
    deps = deps_at(session_factory, live_clock, by_program(order=shop), woken)
    await launched(deps, tool.id, dana)
    [first] = await stops(session_factory)
    # Not for another tool, nor another person, nor other arguments.
    assert await taken(session_factory, first.id, other.id, dana) is None
    assert await taken(session_factory, first.id, tool.id, alex) is None
    assert await taken(session_factory, first.id, tool.id, dana, {"n": 3}) is None
    yes = await taken(session_factory, first.id, tool.id, dana)
    assert yes is not None and yes.yes_used_at is not None
    woken.clear()
    came = await launched(deps, tool.id, dana, yes)
    # Let through once: the order was made, declared at its amount, and nobody was woken.
    assert came == Launched("done", 1)
    [spent] = await spending(session_factory)
    assert (spent.for_person_id, spent.operation, spent.amount_cents) == (dana.id, "order", 3250)
    assert spent.agreement_id is None
    assert woken == []
    # The yes was used: it is not taken again, and a launch without it stops again.
    assert await taken(session_factory, first.id, tool.id, dana) is None
    assert (await launched(deps, tool.id, dana)).status == "stopped"
    assert shop.ordered == 1
    # A yes for 32.50 does not let 33.00 through: it stops again, and the yes stays used.
    second = (await stops(session_factory))[-1]
    yes = await taken(session_factory, second.id, tool.id, dana)
    assert yes is not None
    shop.price = 3300
    assert (await launched(deps, tool.id, dana, yes)).status == "stopped"
    assert shop.ordered == 1
    assert len(await stops(session_factory)) == 3
    assert await taken(session_factory, second.id, tool.id, dana) is None
    assert len(await spending(session_factory)) == 1


async def test_a_request_before_begin_is_refused(session_factory, live_clock, tools_base, dana):
    said: list[str] = []

    async def program(request: RunRequest, ask: FakeAsk) -> JsonValue:
        for asked in (
            {"do": "write", "sql": "CREATE TABLE t (n int)", "params": [], "space": "mine"},
            {"do": "wake", "content": "hello"},
            {"do": "tool", "name": "order", "arguments": {}},
            {"do": "begin", "operation": "fly"},
            {"do": "begin", "operation": "order"},
        ):
            try:
                await ask(asked)
            except FakeRefused as refused:
                said.append(refused.reason)
        # A read needs no operation.
        return await ask({"do": "read", "sql": "SELECT 1 AS one", "params": [], "space": "common"})

    tool = await add_tool(session_factory, "order", ORDER)
    deps = deps_of(
        session_factory, live_clock, by_program(order=program), [], tools_base=tools_base
    )
    came = await launched(deps, tool.id, dana)
    assert came == Launched("done", [{"one": 1}])
    assert said == [
        "no_operation",
        "no_operation",
        "no_operation",
        "unknown_operation",
        "amount_missing",
    ]


# --- the agreements that last --------------------------------------------------------------------


async def test_friday_up_to_40_euros_once_a_week(session_factory, dana):
    clock = FakeClock(FRIDAY)
    tool = await add_tool(session_factory, "order", ORDER)
    await keep_agreement(session_factory, tool.id, dana, weekdays=[5])
    shop = Shop()
    woken: Woken = []
    deps = deps_at(session_factory, clock, by_program(order=shop), woken)
    # A Friday, 32.50: through, nobody woken, declared under the agreement.
    assert (await launched(deps, tool.id, dana)).status == "done"
    assert woken == []
    [spent] = await spending(session_factory)
    assert spent.agreement_id is not None and spent.amount_cents == 3250
    # Again the same week: once a week only.
    clock.advance(timedelta(hours=2))
    assert (await launched(deps, tool.id, dana)).status == "stopped"
    # The next Thursday (a new week): not one of its days.
    clock.advance(timedelta(days=6) - timedelta(hours=2))
    assert clock().isoweekday() == 4
    assert (await launched(deps, tool.id, dana)).status == "stopped"
    # The next Friday, 40.01: more than it lets through.
    clock.advance(timedelta(days=1))
    shop.price = 4001
    assert (await launched(deps, tool.id, dana)).status == "stopped"
    # The same Friday at 32.50: a new ISO week, through.
    shop.price = 3250
    assert (await launched(deps, tool.id, dana)).status == "done"
    assert [s.amount_cents for s in await spending(session_factory)] == [3250, 3250]
    assert shop.ordered == 2


async def test_two_launches_at_once_under_once_a_week_pass_once(session_factory, live_clock, dana):
    tool = await add_tool(session_factory, "order", ORDER)
    await keep_agreement(session_factory, tool.id, dana)
    deps = deps_at(session_factory, live_clock, by_program(order=Shop()), [])
    both = await asyncio.gather(launched(deps, tool.id, dana), launched(deps, tool.id, dana))
    assert sorted(came.status for came in both) == ["done", "stopped"]
    assert len(await spending(session_factory)) == 1


async def test_a_house_operation_that_costs_wakes_the_owner_and_an_owners_house_agreement_lets_it_through(  # noqa: E501
    session_factory, live_clock, alex
):
    tool = await add_tool(session_factory, "order", ORDER)
    shop = Shop()
    woken: Woken = []
    deps = deps_at(session_factory, live_clock, by_program(order=shop), woken)
    came = await launched(deps, tool.id, None)
    # Stopped, never refused: for the house, Domo judges, and the owner of now is woken.
    assert came.status == "stopped"
    [stop] = await stops(session_factory)
    assert (stop.for_person_id, stop.why, stop.approver) == (None, "costs", "self")
    assert woken == [(alex.id, [ToolWake("order", needs_yes(stop))])]
    [entry] = await audit_of(session_factory, STOPPED)
    assert entry.actor == "domo"
    # A house agreement the owner kept lets it through: no stop, nobody woken.
    await keep_agreement(session_factory, tool.id, alex, for_person_id=None)
    woken.clear()
    assert (await launched(deps, tool.id, None)).status == "done"
    assert woken == [] and len(await stops(session_factory)) == 1
    [spent] = await spending(session_factory)
    assert (spent.for_person_id, spent.amount_cents) == (None, 3250)
    assert spent.agreement_id is not None


async def test_an_owners_limit_reads_the_amount_and_what_was_spent(
    session_factory, live_clock, dana, grant
):
    tool = await add_tool(session_factory, "order", ORDER)
    await keep_agreement(session_factory, tool.id, dana, times=10, per="month")
    await grant(
        "program.begin",
        person=dana,
        limit=(
            "action.facts.amount_cents != null && "
            'action.facts.amount_cents + spent.month > 10000 ? "refuse" : "allow"'
        ),
    )
    shop = Shop(price=4000)

    async def program(request: RunRequest, ask: FakeAsk) -> JsonValue:
        try:
            return await shop(request, ask)
        except FakeRefused as refused:
            return refused.reason

    deps = deps_at(session_factory, live_clock, by_program(order=program), [])
    # 40 then 80 euros this month: through; 120 would be past the owner's 100.
    assert (await launched(deps, tool.id, dana)).result == 1
    assert (await launched(deps, tool.id, dana)).result == 2
    assert (await launched(deps, tool.id, dana)).result == "limit"
    assert [s.amount_cents for s in await spending(session_factory)] == [4000, 4000]


async def test_for_the_house_an_owners_rule_waits_for_the_owner_and_a_limit_reads_its_spending(
    session_factory, live_clock, alex, grant, revoke_right
):
    # Carried questions (1) and (2): the owner's "always ask" on a right of Domo's waits for the
    # owner's yes, a person's; a limit on a right of Domo's reads what was spent for the house.
    tool = await add_tool(session_factory, "order", ORDER)
    await keep_agreement(session_factory, tool.id, alex, for_person_id=None, times=10)
    rule = await grant("program.begin", for_domo=True, ask="yes")
    shop = Shop()
    woken: Woken = []
    deps = deps_at(session_factory, live_clock, by_program(order=shop), woken)
    assert (await launched(deps, tool.id, None)).status == "stopped"
    [stop] = await stops(session_factory)
    assert (stop.for_person_id, stop.why, stop.approver, stop.ask) == (
        None,
        "owner_rule",
        "owner",
        "yes",
    )
    assert woken == [(alex.id, [ToolWake("order", needs_yes(stop))])]
    await revoke_right(rule)
    await grant(
        "program.begin",
        for_domo=True,
        limit=(
            "action.facts.amount_cents != null && "
            'action.facts.amount_cents + spent.week > 5000 ? "refuse" : "allow"'
        ),
    )

    async def program(request: RunRequest, ask: FakeAsk) -> JsonValue:
        try:
            return await shop(request, ask)
        except FakeRefused as refused:
            return refused.reason

    deps = deps_at(session_factory, live_clock, by_program(order=program), woken)
    assert (await launched(deps, tool.id, None)).result == 1
    # 32.50 more for the house this week would be past its 50 euros.
    assert (await launched(deps, tool.id, None)).result == "limit"
    [spent] = await spending(session_factory)
    assert (spent.for_person_id, spent.amount_cents) == (None, 3250)


async def test_a_dry_run_never_stops_declares_or_wakes(session_factory, live_clock, dana):
    tool = await add_tool(session_factory, "order", ORDER)
    woken: Woken = []
    scope = LaunchScope(
        tool_name="order",
        tool_id=tool.id,
        for_whom=ForWhom(dana),
        actor=Actor(dana.id, as_role(dana.role)),
        depth=0,
        chain=Chain(),
        waited=False,
        path=("order",),
        dry_run=True,
        operations=read_operations(ORDER),
    )
    means = Means(deps_at(session_factory, live_clock, by_program(), woken), scope)
    assert await means.answer({"do": "begin", "operation": "order", "amount_cents": 3250}) == {
        "ok": None
    }
    assert await stops(session_factory) == [] and await spending(session_factory) == []
    assert scope.chain.wakes == {}
    # Not judged with Dana's rights, and nothing written in the audit as an act.
    assert await audit_of(session_factory, "policy.decision", STOPPED) == []


# --- a yes in a turn -----------------------------------------------------------------------------


async def test_a_yes_call_waits_and_is_shown_and_the_judge_never_reads_the_output(
    adult_chat: ChatPilot, chat_server: ChatServer, session_factory, tool_context: ToolContext
):
    secret_result = "the shop said: 4 pizzas on their way"
    dana = adult_chat.person
    tool = await add_tool(session_factory, "order", ORDER)
    shop = Shop()

    async def program(request: RunRequest, ask: FakeAsk) -> JsonValue:
        await shop(request, ask)
        return secret_result

    woken: Woken = []
    deps = deps_at(session_factory, chat_server.clock, by_program(order=program), woken)
    await launched(deps, tool.id, dana)
    [stop] = await stops(session_factory)
    reply = await adult_chat.say_with_tool("t_order", {"yes_for": str(stop.id)})
    # Waiting for Dana's own yes, asked by Domo for what costs, why being what it does.
    assert reply.actions == [{"tool": "t_order", "status": "waiting"}]
    [result] = reply.model_results
    assert (result["status"], result["by"], result["asked_by"]) == (
        "waiting_for_confirmation",
        "you",
        "domo",
    )
    assert result["why"] == ORDER["order"]["says"]
    # Shown, as any request that waits; the judge weighed her cost first (Task 24) and asked
    # her yes (the pilots' judge answers a yes), with the amount and what it does, never the
    # program's output.
    assert reply.pending_confirmation_id is not None
    [weighed] = [call for call in chat_server.calls if is_judge_call(call.system)]
    case = json.loads(str(weighed.messages[0].content))
    assert (case["cost"]["amount_cents"], case["cost"]["operation"]) == (3250, "order")
    # Any other argument with it is refused: the stop's own arguments are used.
    mixed = await adult_chat.say_with_tool("t_order", {"yes_for": str(stop.id), "n": 9})
    assert mixed.actions == [{"tool": "t_order", "status": "invalid", "reason": "invalid"}]
    # The stored call, once said yes to, takes the yes and runs it once.
    offer = await offer_named(session_factory, deps, "t_order")
    assert offer is not None
    ctx = replace(tool_context, person=dana, actor=Actor(dana.id, as_role(dana.role)))
    ran = await offer.execute({"yes_for": str(stop.id)}, ctx)
    assert ran.model_result == {"launch": "done", "result": secret_result}
    assert shop.ordered == 1
    with pytest.raises(InvalidArguments):
        # The yes was used.
        await offer.execute({"yes_for": str(stop.id)}, ctx)
    # Nothing the program gave back, nor what it would wake Domo with, reached a judge.
    judges = [call for call in chat_server.calls if is_judge_call(call.system)]
    judged = json.dumps([[str(m) for m in call.messages] for call in judges])
    assert secret_result not in judged and "needs_yes" not in judged


# --- fix round 1 ---------------------------------------------------------------------------------

WRITE: dict[str, Any] = {"do": "write", "sql": "SELECT 1", "params": [], "space": "common"}


def shop_then(*asked: dict[str, Any], operation: str = "order", price: int = 3250) -> Any:
    """A program that begins ``operation`` (at ``price`` when it is ``order``), then asks each
    of ``asked``, noting what each answer was (``None`` once let through, else the refusal)."""
    said: list[Any] = []

    async def program(request: RunRequest, ask: FakeAsk) -> JsonValue:
        begin: dict[str, Any] = {"do": "begin", "operation": operation}
        if operation == "order":
            begin["amount_cents"] = price
        await ask(begin)
        for one in asked:
            try:
                said.append(await ask(one))
            except FakeRefused as refused:
                said.append(refused.reason)
        return said

    program.said = said  # type: ignore[attr-defined]
    return program


async def test_a_self_yes_on_a_cost_does_not_lift_an_adults_approval(
    session_factory, live_clock, dana, grant
):
    tool = await add_tool(session_factory, "order", ORDER)
    deps = deps_at(session_factory, live_clock, by_program(order=shop_then(WRITE)), [])
    await launched(deps, tool.id, dana)
    [cost] = await stops(session_factory)
    assert (cost.why, cost.approver) == ("costs", "self")
    await grant("program.base.write", person=dana, approver="adult")
    yes = await taken(session_factory, cost.id, tool.id, dana)
    # Her own yes answers the cost, never the adult's approval her write needs: it stops again.
    assert (await launched(deps, tool.id, dana, yes)).status == "stopped"
    [_, again] = await stops(session_factory)
    assert (again.why, again.approver, again.ask) == ("approval", "adult", "yes")


async def test_a_self_yes_on_a_cost_does_not_lift_an_owners_proof(
    session_factory, live_clock, dana, grant
):
    tool = await add_tool(session_factory, "order", ORDER)
    deps = deps_at(session_factory, live_clock, by_program(order=shop_then(WRITE)), [])
    await launched(deps, tool.id, dana)
    [cost] = await stops(session_factory)
    await grant("program.base.write", person=dana, ask="proof")
    yes = await taken(session_factory, cost.id, tool.id, dana)
    assert (await launched(deps, tool.id, dana, yes)).status == "stopped"
    [_, again] = await stops(session_factory)
    assert (again.why, again.approver, again.ask) == ("owner_rule", "self", "proof")


async def test_a_yes_that_answered_the_stronger_ask_lets_the_weaker_through(
    session_factory, live_clock, dana, grant
):
    tool = await add_tool(session_factory, "order", ORDER)
    program = shop_then(WRITE, {"do": "wake", "content": "ok"}, operation="follow")
    woken: Woken = []
    deps = deps_at(session_factory, live_clock, by_program(order=program), woken)
    await grant("program.base.write", person=dana, ask="proof")
    await grant("program.wake", person=dana, ask="yes")
    await launched(deps, tool.id, dana)
    [proof] = await stops(session_factory)
    assert (proof.why, proof.ask, proof.operation) == ("owner_rule", "proof", "follow")
    yes = await taken(session_factory, proof.id, tool.id, dana)
    woken.clear()
    came = await launched(deps, tool.id, dana, yes)
    # The write passes the barrier (the base is not there) and the wake asks no more than a proof.
    assert came == Launched("done", ["no_base", None])
    assert len(await stops(session_factory)) == 1
    assert woken == [(dana.id, [ToolWake("order", "ok")])]


async def test_a_costing_begin_of_nothing_is_refused(session_factory, live_clock, dana):
    tool = await add_tool(session_factory, "order", ORDER)

    async def program(request: RunRequest, ask: FakeAsk) -> JsonValue:
        try:
            await ask({"do": "begin", "operation": "order", "amount_cents": 0})
        except FakeRefused as refused:
            return refused.reason
        return None

    deps = deps_at(session_factory, live_clock, by_program(order=program), [])
    assert (await launched(deps, tool.id, dana)).result == "amount_missing"


def begin_then_write(amount: int) -> Any:
    """A program that begins ``order`` at ``amount``, carries on past a refusal, then writes:
    what each answer was (``None`` once let through, else the refusal)."""

    async def program(request: RunRequest, ask: FakeAsk) -> JsonValue:
        said: list[Any] = []
        for one in ({"do": "begin", "operation": "order", "amount_cents": amount}, WRITE):
            try:
                said.append(await ask(one))
            except FakeRefused as refused:
                said.append(refused.reason)
        return said

    return program


async def test_a_begin_that_fails_leaves_no_operation_begun(
    session_factory, live_clock, dana, monkeypatch
):
    """Whatever a begin's fate, a request that follows it is judged under an operation the
    barrier let begin: a begin that could not be answered covers nothing after it."""
    tool = await add_tool(session_factory, "order", ORDER)
    real = means_module.decide_now

    async def failing(*args: Any, **kwargs: Any) -> Any:
        if args[2].action_type == "program.begin":
            raise RuntimeError("the base went away")
        return await real(*args, **kwargs)

    monkeypatch.setattr(means_module, "decide_now", failing)
    deps = deps_at(session_factory, live_clock, by_program(order=begin_then_write(3250)), [])
    came = await launched(deps, tool.id, dana)
    assert came == Launched("done", ["failed", "no_operation"])
    assert await stops(session_factory) == []
    assert await spending(session_factory) == []


async def test_a_begin_that_takes_too_long_leaves_no_operation_begun(
    session_factory, live_clock, dana, monkeypatch
):
    tool = await add_tool(session_factory, "order", ORDER)
    real = means_module.decide_now

    async def hanging(*args: Any, **kwargs: Any) -> Any:
        if args[2].action_type == "program.begin":
            await asyncio.sleep(3600)
        return await real(*args, **kwargs)

    monkeypatch.setattr(means_module, "decide_now", hanging)
    async with session_factory() as writer:
        # The shortest a setting gives: one second.
        await set_house(writer, {"answer_seconds": 1})
        await writer.commit()
    deps = deps_at(session_factory, live_clock, by_program(order=begin_then_write(3250)), [])
    came = await launched(deps, tool.id, dana)
    assert came == Launched("done", ["too_slow", "no_operation"])
    assert await stops(session_factory) == []


async def test_a_begin_of_more_than_the_columns_hold_is_refused_cleanly(
    session_factory, live_clock, dana
):
    tool = await add_tool(session_factory, "order", ORDER)
    deps = deps_at(
        session_factory, live_clock, by_program(order=begin_then_write(5_000_000_000)), []
    )
    came = await launched(deps, tool.id, dana)
    assert came == Launched("done", ["not_valid", "no_operation"])
    assert await stops(session_factory) == []
    assert await spending(session_factory) == []


async def test_a_relaunch_never_declares_the_same_money_twice(
    session_factory, live_clock, dana, grant
):
    tool = await add_tool(session_factory, "order", ORDER)
    await keep_agreement(session_factory, tool.id, dana, times=10)
    await grant("program.base.write", person=dana, approver="adult")
    deps = deps_at(session_factory, live_clock, by_program(order=shop_then(WRITE)), [])
    # Declared under the agreement, then stopped at the write for an adult's agreement.
    assert (await launched(deps, tool.id, dana)).status == "stopped"
    assert len(await spending(session_factory)) == 1
    [stop] = await stops(session_factory)
    yes = await taken(session_factory, stop.id, tool.id, dana)
    assert (await launched(deps, tool.id, dana, yes)).status == "done"
    [spent] = await spending(session_factory)
    assert spent.amount_cents == 3250


def slowed_decisions(monkeypatch) -> None:
    """Every decision of a program's request takes a moment once read: two begins at once both
    read what was spent before either declares, unless something holds them apart."""
    real = means_module.decide_now

    async def slow(*args: Any, **kwargs: Any) -> Any:
        decided = await real(*args, **kwargs)
        await asyncio.sleep(0.3)
        return decided

    monkeypatch.setattr(means_module, "decide_now", slow)


LIMIT_50 = (
    "action.facts.amount_cents != null && "
    'action.facts.amount_cents + spent.week > 5000 ? "refuse" : "allow"'
)


def noting(program: Any) -> Any:
    async def noted(request: RunRequest, ask: FakeAsk) -> JsonValue:
        try:
            return await program(request, ask)
        except FakeRefused as refused:
            return refused.reason

    return noted


async def test_two_begins_at_once_never_pass_the_owners_limit(
    session_factory, live_clock, dana, grant, monkeypatch
):
    tool = await add_tool(session_factory, "order", ORDER)
    await keep_agreement(session_factory, tool.id, dana, times=10)
    await grant("program.begin", person=dana, limit=LIMIT_50)
    slowed_decisions(monkeypatch)
    deps = deps_at(session_factory, live_clock, by_program(order=noting(Shop())), [])
    both = await asyncio.gather(launched(deps, tool.id, dana), launched(deps, tool.id, dana))
    assert sorted(str(came.result) for came in both) == ["1", "limit"]
    assert len(await spending(session_factory)) == 1


async def test_a_yes_relaunch_and_a_begin_at_once_never_pass_the_owners_limit(
    session_factory, live_clock, dana, grant, monkeypatch
):
    tool = await add_tool(session_factory, "order", ORDER)
    deps = deps_at(session_factory, live_clock, by_program(order=noting(Shop())), [])
    await launched(deps, tool.id, dana)
    [stop] = await stops(session_factory)
    yes = await taken(session_factory, stop.id, tool.id, dana)
    await keep_agreement(session_factory, tool.id, dana, times=10)
    await grant("program.begin", person=dana, limit=LIMIT_50)
    slowed_decisions(monkeypatch)
    both = await asyncio.gather(launched(deps, tool.id, dana, yes), launched(deps, tool.id, dana))
    assert sorted(str(came.result) for came in both) == ["1", "limit"]
    assert len(await spending(session_factory)) == 1


async def test_a_turn_that_waits_reads_the_stop_and_nobody_is_woken(
    session_factory, live_clock, dana
):
    tool = await add_tool(session_factory, "order", ORDER)
    woken: Woken = []
    deps = deps_at(session_factory, live_clock, by_program(order=Shop()), woken)
    came = await launch_in_a_turn(deps, AskingTool.of(tool), arguments=ARGUMENTS, for_person=dana)
    [stop] = await stops(session_factory)
    assert came == Launched(
        "stopped",
        {
            "reason": "needs_yes",
            "stop": str(stop.id),
            "says": ORDER["order"]["says"],
            "amount_cents": 3250,
            "why": "costs",
            "by": "self",
        },
    )
    # The turn tells it: no wake asks the person a second time.
    assert woken == []


async def test_an_owners_limit_on_the_tool_reads_the_amount_of_a_yes(
    adult_chat: ChatPilot, chat_server: ChatServer, session_factory, grant
):
    dana = adult_chat.person
    tool = await add_tool(session_factory, "order", ORDER)
    deps = deps_at(session_factory, chat_server.clock, by_program(order=Shop()), [])
    await launched(deps, tool.id, dana)
    [stop] = await stops(session_factory)
    await grant(
        "made.order",
        person=dana,
        limit=(
            "action.facts.amount_cents != null && action.facts.amount_cents > 3000"
            ' ? "refuse" : "allow"'
        ),
    )
    reply = await adult_chat.say_with_tool("t_order", {"yes_for": str(stop.id)})
    assert reply.actions == [{"tool": "t_order", "status": "refused", "reason": "limit"}]
    # A launch that carries no yes has no amount: the limit lets it through.
    assert (await launched(deps, tool.id, dana)).status == "stopped"


async def waiting_request(chat: ChatPilot, stop: LaunchStop) -> uuid.UUID:
    reply = await chat.say_with_tool("t_order", {"yes_for": str(stop.id)})
    assert reply.actions == [{"tool": "t_order", "status": "waiting"}]
    confirmation_id = reply.pending_confirmation_id
    assert confirmation_id is not None
    return confirmation_id


async def policy_of(session_factory) -> Any:
    """The catalog with the kinds of Domo's tools, as a decision outside a turn reads it."""
    async with session_factory() as session:
        return await load_policy(session, DEFAULT_POLICY)


def deciders(tool_context: ToolContext, deps: LaunchDeps, session_factory) -> Any:
    """The requester's context of a decision outside a turn, finding Domo's tools as the
    routes do."""
    return requester_context(
        replace(tool_context, made_tool_named=partial(offer_named, session_factory, deps))
    )


async def test_a_yes_by_the_button_runs_the_stored_call_and_takes_the_yes(
    adult_chat: ChatPilot, chat_server: ChatServer, session_factory
):
    dana = adult_chat.person
    tool = await add_tool(session_factory, "order", ORDER)
    shop = Shop()
    chat_server.sandbox = by_program(order=shop)
    deps = deps_at(session_factory, chat_server.clock, chat_server.sandbox, [])
    await launched(deps, tool.id, dana)
    [stop] = await stops(session_factory)
    reply = await adult_chat.say_with_tool("t_order", {"yes_for": str(stop.id)})
    # The card says what it costs, and what it does in the operation's own words, as data.
    assert "32.50 €" in reply.cards_html
    assert ORDER["order"]["says"] in reply.cards_html
    card = await adult_chat.confirm_last(reply)
    assert card.status == "executed"
    assert shop.ordered == 1
    [used] = await stops(session_factory)
    assert used.yes_used_at is not None


async def test_a_proof_stop_is_answered_by_the_proof_and_a_no_leaves_the_yes(
    adult_chat: ChatPilot,
    chat_server: ChatServer,
    session_factory,
    grant,
    tool_context: ToolContext,
):
    dana = adult_chat.person
    tool = await add_tool(session_factory, "order", ORDER)
    await keep_agreement(session_factory, tool.id, dana, times=10)
    await grant("program.begin", person=dana, ask="proof")
    shop = Shop()
    deps = deps_at(session_factory, chat_server.clock, by_program(order=shop), [])
    await launched(deps, tool.id, dana)
    [stop] = await stops(session_factory)
    assert (stop.why, stop.ask) == ("owner_rule", "proof")
    # A no: nothing ran, and the yes is still to give.
    refused = await waiting_request(adult_chat, stop)
    outcome, _ = await decide_confirmation(
        session_factory=session_factory,
        confirmation_id=refused,
        decider=dana,
        approve=False,
        via="button",
        reply_message_id=None,
        ctx_factory=deciders(tool_context, deps, session_factory),
        registry=TOOLS,
        policy=await policy_of(session_factory),
        now=chat_server.clock(),
    )
    assert outcome == "refused" and shop.ordered == 0
    assert (await stops(session_factory))[0].yes_used_at is None
    # The proof runs it, through the stored call, once.
    proved = await waiting_request(adult_chat, stop)
    outcome, record = await decide_confirmation(
        session_factory=session_factory,
        confirmation_id=proved,
        decider=dana,
        approve=True,
        via="proof",
        reply_message_id=None,
        ctx_factory=deciders(tool_context, deps, session_factory),
        registry=TOOLS,
        policy=await policy_of(session_factory),
        now=chat_server.clock(),
        means="password",
    )
    assert outcome == "executed" and shop.ordered == 1, record
    [used] = await stops(session_factory)
    assert used.yes_used_at is not None


# --- fix round 2 ---------------------------------------------------------------------------------


async def relaunched_until_done(
    session_factory, deps: LaunchDeps, tool_id: uuid.UUID, person: Person, *, most: int
) -> list[LaunchStop]:
    """Launch, then run it again with the yes of each stop it makes (each yes given, as a person
    would give it), ``most`` yeses at most; the stops, in order, once it ended ``done``."""
    came = await launched(deps, tool_id, person)
    for _ in range(most):
        if came.status == "done":
            break
        assert came.status == "stopped", came
        last = (await stops(session_factory))[-1]
        yes = await taken(session_factory, last.id, tool_id, person)
        came = await launched(deps, tool_id, person, yes)
    assert came.status == "done", came
    return await stops(session_factory)


async def test_an_approval_and_a_rule_on_one_request_are_asked_together(
    session_factory, live_clock, dana, grant
):
    tool = await add_tool(session_factory, "order", ORDER)
    await grant("program.base.write", person=dana, approver="adult", ask="explicit_yes")
    program = shop_then(WRITE, operation="follow")
    deps = deps_at(session_factory, live_clock, by_program(order=program), [])
    [stop] = await relaunched_until_done(session_factory, deps, tool.id, dana, most=2)
    # One stop asks the strongest of both: an adult's explicit yes.
    assert (stop.approver, stop.ask) == ("adult", "explicit_yes")


async def test_a_proof_on_the_begin_and_an_approval_on_a_write_finish_in_two_yeses(
    session_factory, live_clock, dana, grant
):
    tool = await add_tool(session_factory, "order", ORDER)
    await grant("program.begin", person=dana, ask="proof")
    await grant("program.base.write", person=dana, approver="adult")
    program = shop_then(WRITE, operation="follow")
    deps = deps_at(session_factory, live_clock, by_program(order=program), [])
    first, second = await relaunched_until_done(session_factory, deps, tool.id, dana, most=2)
    assert (first.approver, first.ask) == ("self", "proof")
    # The second asks what the write needs and no more: the proof stays answered at its place.
    assert (second.approver, second.ask) == ("adult", "yes")


async def test_a_settled_relaunch_counts_its_money_once_in_the_owners_limit(
    session_factory, live_clock, dana, grant
):
    tool = await add_tool(session_factory, "order", ORDER)
    await keep_agreement(session_factory, tool.id, dana, times=10)
    await grant("program.begin", person=dana, limit=LIMIT_50)
    await grant("program.base.write", person=dana, approver="adult")
    deps = deps_at(session_factory, live_clock, by_program(order=shop_then(WRITE)), [])
    assert (await launched(deps, tool.id, dana)).status == "stopped"
    [stop] = await stops(session_factory)
    yes = await taken(session_factory, stop.id, tool.id, dana)
    # 32.50 spent once: the relaunch is within the 50 euros of the week.
    assert (await launched(deps, tool.id, dana, yes)).status == "done"
    assert [s.amount_cents for s in await spending(session_factory)] == [3250]


TWO_COSTS: dict[str, Any] = {
    **ORDER,
    "tip": {"says": "Tip the driver.", "risks": ["costs"]},
}


async def test_a_relaunch_declares_each_operation_of_the_run_once(
    session_factory, live_clock, dana
):
    tool = await add_tool(session_factory, "order", TWO_COSTS)
    async with session_factory() as writer:
        writer.add(
            StandingAgreement(
                tool_id=tool.id,
                operation="tip",
                for_person_id=dana.id,
                amount_cents_at_most=1000,
                times=1,
                per="week",
                given_by=dana.id,
            )
        )
        await writer.commit()

    async def program(request: RunRequest, ask: FakeAsk) -> JsonValue:
        await ask({"do": "begin", "operation": "tip", "amount_cents": 500})
        await ask({"do": "begin", "operation": "order", "amount_cents": 3250})
        return "ok"

    deps = deps_at(session_factory, live_clock, by_program(order=program), [])
    assert (await launched(deps, tool.id, dana)).status == "stopped"
    [stop] = await stops(session_factory)
    yes = await taken(session_factory, stop.id, tool.id, dana)
    # The tip, declared before the stop under its once-a-week agreement, is neither declared
    # again nor stopped on the relaunch.
    assert (await launched(deps, tool.id, dana, yes)) == Launched("done", "ok")
    spent = sorted((s.operation, s.amount_cents) for s in await spending(session_factory))
    assert spent == [("order", 3250), ("tip", 500)]


class EndsLate(FakeSandbox):
    """A sandbox whose runs end only once ``after`` is set (the program itself is done)."""

    def __init__(self, **given: Any) -> None:
        super().__init__(**given)
        self.after = asyncio.Event()

    async def run(self, request: RunRequest, **given: Any) -> Any:
        ran = await super().run(request, **given)
        await self.after.wait()
        return ran


async def test_a_stop_a_turn_handed_over_reaches_domo_once_as_a_stop(
    session_factory, live_clock, dana, monkeypatch
):
    monkeypatch.setattr(handover, "HAND_OVER_SECONDS", 0.2)
    tool = await add_tool(session_factory, "order", ORDER)
    sandbox = EndsLate(program=by_program(order=Shop())._program)  # type: ignore[attr-defined]
    wakes = Wakes()
    woken: Woken = []
    deps = deps_of(session_factory, live_clock, sandbox, woken, wakes)
    # The stop is made while the turn waits; the turn hands over before the run ends.
    came = await launch_in_a_turn(deps, AskingTool.of(tool), arguments=ARGUMENTS, for_person=dana)
    assert came.status == "running"
    sandbox.after.set()
    async with asyncio.timeout(5):
        while wakes.pending or not woken:
            await asyncio.sleep(0.01)
    [stop] = await stops(session_factory)
    assert woken == [(dana.id, [ToolWake("order", needs_yes(stop))])]


async def test_a_stop_handed_over_before_it_is_made_is_told_once(
    session_factory, live_clock, dana, monkeypatch
):
    monkeypatch.setattr(handover, "HAND_OVER_SECONDS", 0.1)
    tool = await add_tool(session_factory, "order", ORDER)
    sandbox = by_program(order=Shop())
    sandbox.gate = asyncio.Event()
    wakes = Wakes()
    woken: Woken = []
    deps = deps_of(session_factory, live_clock, sandbox, woken, wakes)
    came = await launch_in_a_turn(deps, AskingTool.of(tool), arguments=ARGUMENTS, for_person=dana)
    assert came.status == "running"
    sandbox.gate.set()
    async with asyncio.timeout(5):
        while wakes.pending or not woken:
            await asyncio.sleep(0.01)
    await asyncio.sleep(0.2)
    [stop] = await stops(session_factory)
    # Woken once with the stop: the run's own end does not tell it a second time.
    assert woken == [(dana.id, [ToolWake("order", needs_yes(stop))])]


async def test_a_stop_whose_turn_was_cancelled_wakes_domo(
    session_factory, live_clock, dana, monkeypatch
):
    tool = await add_tool(session_factory, "order", ORDER)
    wakes = Wakes()
    woken: Woken = []
    deps = deps_of(session_factory, live_clock, by_program(order=Shop()), woken, wakes)
    real = handover._launched  # pyright: ignore[reportPrivateUsage]
    turn: list[asyncio.Task[Any]] = []

    async def then_cancelled(*args: Any) -> Launched:
        came = await real(*args)
        # The person stops the turn just as the run ends.
        turn[0].cancel()
        return came

    monkeypatch.setattr(handover, "_launched", then_cancelled)
    turn.append(
        asyncio.create_task(
            launch_in_a_turn(deps, AskingTool.of(tool), arguments=ARGUMENTS, for_person=dana)
        )
    )
    with pytest.raises(asyncio.CancelledError):
        await turn[0]
    async with asyncio.timeout(5):
        while wakes.pending or not woken:
            await asyncio.sleep(0.01)
    [stop] = await stops(session_factory)
    assert woken == [(dana.id, [ToolWake("order", needs_yes(stop))])]


def test_the_amount_of_a_yes_is_written_in_the_readers_language():
    facts = {"operation": "order", "amount_cents": 3250, "yes": True, "approver": "self"}
    for language, amount in (("en", "32.50 €"), ("fr", "32,50 €")):
        said = describe_type("made.order", {}, CatalogTexts(language), facts)
        assert said == t("action.describe.made.yes.amount", language, amount=amount)


# --- fix round 3 ---------------------------------------------------------------------------------


def program_of(*asked: dict[str, Any]) -> Any:
    """A program that asks each of ``asked`` in turn and gives back ``"done"``."""

    async def program(request: RunRequest, ask: FakeAsk) -> JsonValue:
        for one in asked:
            try:
                await ask(one)
            except FakeRefused:
                # The base is not there: what passed the barrier is refused there.
                continue
        return "done"

    return program


def begin(operation: str, amount: int | None = None) -> dict[str, Any]:
    asked: dict[str, Any] = {"do": "begin", "operation": operation}
    if amount is not None:
        asked["amount_cents"] = amount
    return asked


async def test_one_operation_begun_twice_ends_after_two_yeses_declared_once_each(
    session_factory, live_clock, dana
):
    tool = await add_tool(session_factory, "order", ORDER)
    program = program_of(begin("order", 100), begin("order", 100))
    deps = deps_at(session_factory, live_clock, by_program(order=program), [])
    assert len(await relaunched_until_done(session_factory, deps, tool.id, dana, most=2)) == 2
    assert [s.amount_cents for s in await spending(session_factory)] == [100, 100]


async def test_a_loop_of_three_ends_after_three_yeses(session_factory, live_clock, dana):
    tool = await add_tool(session_factory, "order", ORDER)
    program = program_of(begin("order", 100), begin("order", 200), begin("order", 300))
    deps = deps_at(session_factory, live_clock, by_program(order=program), [])
    assert len(await relaunched_until_done(session_factory, deps, tool.id, dana, most=3)) == 3
    assert sorted(s.amount_cents for s in await spending(session_factory)) == [100, 200, 300]


async def test_a_write_rule_inside_a_repeated_operation_ends(
    session_factory, live_clock, dana, grant
):
    tool = await add_tool(session_factory, "order", ORDER)
    await grant("program.base.write", person=dana, ask="explicit_yes")
    program = program_of(begin("follow"), WRITE, begin("follow"), WRITE)
    deps = deps_at(session_factory, live_clock, by_program(order=noting(program)), [])
    assert len(await relaunched_until_done(session_factory, deps, tool.id, dana, most=2)) == 2


async def test_each_begin_of_a_repeated_operation_is_declared_once_for_its_own_amount(
    session_factory, live_clock, dana, grant
):
    tool = await add_tool(session_factory, "order", ORDER)
    await keep_agreement(session_factory, tool.id, dana, times=10)
    await grant("program.base.write", person=dana, approver="adult")
    program = program_of(begin("order", 100), begin("order", 50), WRITE)
    deps = deps_at(session_factory, live_clock, by_program(order=noting(program)), [])
    await relaunched_until_done(session_factory, deps, tool.id, dana, most=1)
    assert sorted(s.amount_cents for s in await spending(session_factory)) == [50, 100]


async def test_a_settled_begin_shows_its_real_amount_to_the_limits_and_the_audit(
    session_factory, live_clock, dana, grant
):
    tool = await add_tool(session_factory, "order", ORDER)
    await keep_agreement(session_factory, tool.id, dana, times=10)
    await grant("program.base.write", person=dana, approver="adult")
    deps = deps_at(session_factory, live_clock, by_program(order=noting(shop_then(WRITE))), [])
    assert (await launched(deps, tool.id, dana)).status == "stopped"
    [stop] = await stops(session_factory)
    # A cap on one order, set between the runs: the relaunch sees the order's own amount.
    await grant(
        "program.begin",
        person=dana,
        limit="action.facts.amount_cents != null && action.facts.amount_cents > 3000"
        ' ? "refuse" : "allow"',
    )
    yes = await taken(session_factory, stop.id, tool.id, dana)
    assert (await launched(deps, tool.id, dana, yes)).result == "limit"
    begins = [
        e.details["facts"]["amount_cents"]
        for e in await audit_of(session_factory, "policy.decision")
        if e.details["action_type"] == "program.begin"
    ]
    assert begins == [3250, 3250]


async def test_a_settled_begin_counts_in_the_week_it_is_let_through(session_factory, dana, grant):
    clock = FakeClock(FRIDAY)
    tool = await add_tool(session_factory, "order", ORDER)
    await keep_agreement(session_factory, tool.id, dana, times=10)
    await grant("program.base.write", person=dana, approver="adult")
    await grant("program.begin", person=dana, limit=LIMIT_50)
    deps = deps_at(session_factory, clock, by_program(order=noting(shop_then(WRITE))), [])
    assert (await launched(deps, tool.id, dana)).status == "stopped"
    [stop] = await stops(session_factory)
    yes = await taken(session_factory, stop.id, tool.id, dana)
    # The yes comes the next week: the money is spent now, and counts in this week, once.
    clock.advance(timedelta(days=7))
    assert (await launched(deps, tool.id, dana, yes)).result == ["no_base"]
    [spent] = await spending(session_factory)
    assert (spent.amount_cents, spent.at) == (3250, clock())


async def decided_by(
    decider: Person,
    confirmation_id: uuid.UUID,
    *,
    chat_server: ChatServer,
    session_factory,
    tool_context: ToolContext,
    deps: LaunchDeps,
) -> str:
    """What comes of ``decider`` pressing yes on the request ``confirmation_id``."""
    outcome, record = await decide_confirmation(
        session_factory=session_factory,
        confirmation_id=confirmation_id,
        decider=decider,
        approve=True,
        via="button",
        reply_message_id=None,
        ctx_factory=deciders(tool_context, deps, session_factory),
        registry=TOOLS,
        policy=await policy_of(session_factory),
        now=chat_server.clock(),
    )
    assert record is None or record.status in ("done", "refused", "waiting"), record
    return outcome


async def test_a_joined_stop_is_an_adults_and_the_childs_own_yes_never_runs_it(
    adult_chat: ChatPilot,
    child_chat: ChatPilot,
    chat_server: ChatServer,
    session_factory,
    grant,
    tool_context: ToolContext,
):
    dana, paul = adult_chat.person, child_chat.person
    tool = await add_tool(session_factory, "order", ORDER)
    # One request that needs both: an adult's approval, and the yes of what costs.
    await grant("program.begin", person=paul, approver="adult")
    shop = Shop()
    deps = deps_at(session_factory, chat_server.clock, by_program(order=shop), [])
    decided = partial(
        decided_by,
        chat_server=chat_server,
        session_factory=session_factory,
        tool_context=tool_context,
        deps=deps,
    )
    await launched(deps, tool.id, paul)
    [both] = await stops(session_factory)
    # Joined: asked of an adult, as an approval.
    assert (both.why, both.approver) == ("approval", "adult")
    request = await waiting_request(child_chat, both)
    # Paul's own yes does not run it; Dana's does.
    assert await decided(paul, request) != "executed"
    assert shop.ordered == 0
    assert await decided(dana, request) == "executed"
    assert shop.ordered == 1
    assert len(await stops(session_factory)) == 1


async def test_after_an_adults_approval_a_childs_cost_is_his_own_and_an_adults_stop_never_is(
    adult_chat: ChatPilot,
    child_chat: ChatPilot,
    chat_server: ChatServer,
    session_factory,
    grant,
    tool_context: ToolContext,
):
    dana, paul = adult_chat.person, child_chat.person
    tool = await add_tool(session_factory, "order", ORDER)
    await grant("program.base.write", person=paul, approver="adult")
    # Each write passes the barrier, then the base, which is not there, refuses it.
    program = program_of(begin("follow"), WRITE, begin("order", 3250), WRITE)
    deps = deps_at(session_factory, chat_server.clock, by_program(order=program), [])
    decided = partial(
        decided_by,
        chat_server=chat_server,
        session_factory=session_factory,
        tool_context=tool_context,
        deps=deps,
    )
    await launched(deps, tool.id, paul)
    [first] = await stops(session_factory)
    assert (first.why, first.approver) == ("approval", "adult")
    # Dana agrees: the run gets to the order, which costs.
    assert await decided(dana, await waiting_request(child_chat, first)) == "executed"
    [_, cost] = await stops(session_factory)
    # The cost is Paul's own to answer: the approval given before it is not asked again.
    assert (cost.why, cost.approver) == ("costs", "self")
    assert await decided(paul, await waiting_request(child_chat, cost)) == "executed"
    [_, _, approval] = await stops(session_factory)
    # The write after the order is another request of an adult: Paul's own yes never runs it.
    assert (approval.why, approval.approver) == ("approval", "adult")
    request = await waiting_request(child_chat, approval)
    assert await decided(paul, request) != "executed"
    assert (await stops(session_factory))[-1].yes_used_at is None
    assert await decided(dana, request) == "executed"
    assert [stop.yes_approver for stop in await stops(session_factory)] == [
        "adult",
        "self",
        "adult",
    ]


async def test_a_childs_own_yes_on_its_cost_never_lifts_an_adults_approval_after_it(
    child_chat: ChatPilot,
    chat_server: ChatServer,
    session_factory,
    grant,
    tool_context: ToolContext,
    add_member,
):
    paul = child_chat.person
    tool = await add_tool(session_factory, "order", ORDER)
    await grant("program.base.write", person=paul, approver="adult")
    deps = deps_at(session_factory, chat_server.clock, by_program(order=shop_then(WRITE)), [])
    policy = await policy_of(session_factory)
    await launched(deps, tool.id, paul)
    [cost] = await stops(session_factory)
    assert (cost.why, cost.approver) == ("costs", "self")
    # Paul says yes to his own order: the write after it still needs an adult.
    outcome, _ = await decide_confirmation(
        session_factory=session_factory,
        confirmation_id=await waiting_request(child_chat, cost),
        decider=paul,
        approve=True,
        via="button",
        reply_message_id=None,
        ctx_factory=deciders(tool_context, deps, session_factory),
        registry=TOOLS,
        policy=policy,
        now=chat_server.clock(),
    )
    assert outcome == "executed"
    [_, approval] = await stops(session_factory)
    assert (approval.why, approval.approver) == ("approval", "adult")


# --- fix round 4 ---------------------------------------------------------------------------------


async def test_one_request_counts_once_in_the_tools_history_however_many_stops_it_went_through(
    adult_chat: ChatPilot, chat_server: ChatServer, session_factory, grant
):
    dana = adult_chat.person
    await add_tool(session_factory, "order", ORDER)
    chat_server.sandbox = by_program(order=program_of(begin("order", 100), begin("order", 200)))
    await grant(
        "made.order",
        person=dana,
        limit='history.day < 2 ? "allow" : "refuse"',
        summary="Two uses a day.",
    )

    async def request() -> None:
        """One request of Dana's, through its two stops, to its end."""
        known = len(await stops(session_factory))
        reply = await adult_chat.say_with_tool("t_order", dict(ARGUMENTS))
        assert reply.actions == [{"tool": "t_order", "status": "done"}]
        # The turn is told plainly that the launch stopped and what is asked, not that it failed.
        [told] = reply.model_results
        assert (told["status"], told["launch"]) == ("done", "stopped")
        assert told["result"]["reason"] == "needs_yes"
        for _ in range(2):
            stop = (await stops(session_factory))[-1]
            asked = await adult_chat.say_with_tool("t_order", {"yes_for": str(stop.id)})
            assert asked.actions == [{"tool": "t_order", "status": "waiting"}]
            # A relaunch that stops again is no failure, and no use either.
            assert (await adult_chat.confirm_last(asked)).status == "executed"
        assert len(await stops(session_factory)) == known + 2

    # Two requests, three launches each: two uses, and the limit lets both through whole.
    await request()
    await request()
    third = await adult_chat.say_with_tool("t_order", dict(ARGUMENTS))
    assert third.actions == [{"tool": "t_order", "status": "refused", "reason": "limit"}]
    assert len(await stops(session_factory)) == 4


def test_a_launch_stopped_for_a_yes_is_told_as_such_and_one_stopped_for_nothing_is_no_success():
    asked = outcome_of(
        Launched("stopped", {"reason": "needs_yes", "stop": str(uuid.uuid4()), "why": "costs"})
    )
    assert asked.status == "done" and asked.model_result["launch"] == "stopped"
    # What Domo is told to do with it: call again with yes_for at once, the call is the ask (R20).
    assert asked.model_result["next"] == STOPPED_NEXT
    # Its card says it did not do the tool's work: the history leaves it out.
    assert asked.card["launch"] == STOPPED_FOR_A_YES
    assert outcome_of(Launched("done", 1)).card.get("launch") is None
    lost = outcome_of(Launched("stopped", None))
    assert lost.status == "failed"
    assert lost.model_result == {"launch": "stopped", "result": None}


class Priced:
    """A program that begins ``order`` at the price of the moment, then writes."""

    def __init__(self, price: int) -> None:
        self.price = price

    async def __call__(self, request: RunRequest, ask: FakeAsk) -> JsonValue:
        await ask(begin("order", self.price))
        # The write passes the barrier, then the base, which is not there, refuses it.
        with contextlib.suppress(FakeRefused):
            await ask(WRITE)
        return "ordered"


async def test_a_settled_begin_carried_into_a_new_week_counts_in_its_agreements_times_there(
    session_factory, dana, grant
):
    clock = FakeClock(FRIDAY)
    tool = await add_tool(session_factory, "order", ORDER)
    await keep_agreement(session_factory, tool.id, dana, times=1)
    await grant("program.base.write", person=dana, approver="adult")
    deps = deps_at(session_factory, clock, by_program(order=Priced(3250)), [])
    # Week 1: the order passes under the agreement, and the run stops at its write.
    assert (await launched(deps, tool.id, dana)).status == "stopped"
    [waiting] = await stops(session_factory)
    # Week 2: another order uses the week's one time.
    clock.advance(timedelta(days=7))
    assert (await launched(deps, tool.id, dana)).status == "stopped"
    assert [s.agreement_id is not None for s in await spending(session_factory)] == [True, True]
    # The first one, answered now, would be a second order this week: it asks the cost's yes.
    yes = await taken(session_factory, waiting.id, tool.id, dana)
    assert (await launched(deps, tool.id, dana, yes)).status == "stopped"
    cost = (await stops(session_factory))[-1]
    assert (cost.why, cost.approver, cost.amount_cents) == ("costs", "self", 3250)
    assert [s.at for s in await spending(session_factory)] == [FRIDAY, clock()]
    # That yes lets it through: spent now, under a yes and no longer under the agreement.
    clock.advance(timedelta(hours=1))
    yes = await taken(session_factory, cost.id, tool.id, dana)
    assert (await launched(deps, tool.id, dana, yes)).status == "done"
    under_it, under_a_yes = await spending(session_factory)
    assert under_it.agreement_id is not None and under_a_yes.agreement_id is None
    assert (under_a_yes.amount_cents, under_a_yes.at) == (3250, clock())


async def test_a_settled_begin_carried_into_a_free_week_passes_under_its_agreement(
    session_factory, dana, grant
):
    clock = FakeClock(FRIDAY)
    tool = await add_tool(session_factory, "order", ORDER)
    await keep_agreement(session_factory, tool.id, dana, times=1)
    await grant("program.base.write", person=dana, approver="adult")
    deps = deps_at(session_factory, clock, by_program(order=Priced(3250)), [])
    assert (await launched(deps, tool.id, dana)).status == "stopped"
    [waiting] = await stops(session_factory)
    clock.advance(timedelta(days=7))
    yes = await taken(session_factory, waiting.id, tool.id, dana)
    # Nothing else was ordered this week: it is this week's one time, and nothing is asked.
    assert (await launched(deps, tool.id, dana, yes)).status == "done"
    [spent] = await spending(session_factory)
    assert spent.agreement_id is not None and spent.at == clock()
    # The week's time is used: the next order asks.
    assert (await launched(deps, tool.id, dana)).status == "stopped"
    assert (await stops(session_factory))[-1].why == "costs"


async def test_a_settled_begin_at_a_lower_amount_lowers_what_is_declared(
    session_factory, live_clock, dana, grant
):
    tool = await add_tool(session_factory, "order", ORDER)
    await keep_agreement(session_factory, tool.id, dana, times=10)
    await grant("program.base.write", person=dana, approver="adult")
    shop = Priced(3250)
    deps = deps_at(session_factory, live_clock, by_program(order=shop), [])
    assert (await launched(deps, tool.id, dana)).status == "stopped"
    [stop] = await stops(session_factory)
    assert [s.amount_cents for s in await spending(session_factory)] == [3250]
    shop.price = 2000
    yes = await taken(session_factory, stop.id, tool.id, dana)
    assert (await launched(deps, tool.id, dana, yes)).status == "done"
    # What was really let through, once.
    assert [s.amount_cents for s in await spending(session_factory)] == [2000]


async def test_a_begin_whose_amount_grew_asks_its_own_yes_and_replaces_what_was_declared(
    session_factory, live_clock, dana, grant
):
    tool = await add_tool(session_factory, "order", ORDER)
    await grant("program.base.write", person=dana, approver="adult")
    shop = Priced(100)
    deps = deps_at(session_factory, live_clock, by_program(order=shop), [])

    async def answered() -> Launched:
        """The launch run again with the yes of its last stop."""
        last = (await stops(session_factory))[-1]
        return await launched(
            deps, tool.id, dana, await taken(session_factory, last.id, tool.id, dana)
        )

    assert (await launched(deps, tool.id, dana)).status == "stopped"
    # Her yes for 1 euro, then the run stops at its write.
    assert (await answered()).status == "stopped"
    assert [s.amount_cents for s in await spending(session_factory)] == [100]
    shop.price = 150
    # The order grew: the yes given for 1 euro does not cover it, and it is asked for 1.50.
    assert (await answered()).status == "stopped"
    grown = (await stops(session_factory))[-1]
    assert (grown.why, grown.amount_cents) == ("costs", 150)
    assert [s.amount_cents for s in await spending(session_factory)] == [100]
    # Her yes for 1.50 lets the order through: the ledger holds what was let through last for
    # that begin, and nothing of before. The adult's approval was for a write of an order of
    # 1 euro: it is asked again for this one.
    assert (await answered()).status == "stopped"
    assert [s.amount_cents for s in await spending(session_factory)] == [150]
    again = (await stops(session_factory))[-1]
    assert (again.why, again.approver, again.amount_cents) == ("approval", "adult", 150)
    assert (await answered()).status == "done"
    assert [s.amount_cents for s in await spending(session_factory)] == [150]


# --- Domo on his own (Task 9, spec 5.8) --------------------------------------------------------


class AnswersItsStop(FakeChatModel):
    """The owner's model in a turn a launch woke Domo for: its first main call carries the yes
    of the stop the wake tells (read from the context, where the stop's id is data), then it
    says ``then``."""

    def __init__(self, tool: str, then: str = "Done.") -> None:
        super().__init__(tool_script=[then], judge="do")
        self._tool = tool
        self._answered = False

    async def stream(self, **kwargs: Any) -> Any:
        system: str = kwargs["system"]
        found = re.search(r'"stop":\s*"([0-9a-f-]{36})"', system)
        if not self._answered and found is not None and kwargs.get("tools"):
            self._answered = True
            call = ToolCall("y1", f"t_{self._tool}", json.dumps({"yes_for": found.group(1)}))
            self._script = [call, *(self._script or [])]
            self._next_entry = 0
        async for item in super().stream(**kwargs):
            yield item


async def launched_for_the_house(chat_server: ChatServer, tool_id: uuid.UUID) -> Launched:
    """``tool_id`` launched by a trigger for the house with the application's deps (a stop wakes
    Domo in the owner's conversation, on his own), waited for until that turn ended."""
    deps = made_tool_deps(app_turn_deps(chat_server.app))
    came = await launch(deps, tool_id, arguments=ARGUMENTS, for_whom=HOUSE, cause="trigger")
    await chat_server.wait_for_wakes()
    return came


async def requests_of(session_factory) -> list[Confirmation]:
    async with session_factory() as reader:
        return list((await reader.scalars(select(Confirmation))).all())


async def test_a_house_operation_that_costs_wakes_domo_alone_and_his_yes_runs_it(
    chat_server: ChatServer, owner_chat: ChatPilot, session_factory
):
    tool = await add_tool(session_factory, "order", ORDER)
    shop = Shop()
    chat_server.sandbox = by_program(order=shop)
    owner_chat.use_model(AnswersItsStop("order"))
    came = await launched_for_the_house(chat_server, tool.id)
    assert came.status == "stopped"
    # Woken on his own, Domo gave the yes himself: it ran at once, nobody was asked.
    assert shop.ordered == 1
    [stop] = await stops(session_factory)
    assert (stop.for_person_id, stop.why, stop.approver) == (None, "costs", "self")
    assert stop.yes_used_at is not None and stop.yes_approver == "self"
    assert await requests_of(session_factory) == []
    # The money is declared for the house, never for the owner.
    [spent] = await spending(session_factory)
    assert (spent.for_person_id, spent.amount_cents, spent.agreement_id) == (None, 3250, None)
    decided = [
        entry
        for entry in await audit_of(session_factory, "policy.decision")
        if entry.details["action_type"] == "made.order"
    ]
    assert {entry.actor for entry in decided} == {"domo"}


async def test_domo_never_answers_a_house_stop_that_asks_a_person(
    chat_server: ChatServer,
    owner_chat: ChatPilot,
    adult_chat: ChatPilot,
    session_factory,
    grant,
    tool_context: ToolContext,
):
    # Task 9 (c): the owner's rule on a right of Domo's asks the owner. Domo never counts as
    # the adult or the owner who agrees, nor gives a proof: his yes waits for the owner.
    tool = await add_tool(session_factory, "order", ORDER)
    await grant("program.begin", for_domo=True, ask="explicit_yes")
    shop = Shop()
    chat_server.sandbox = by_program(order=shop)
    owner_chat.use_model(AnswersItsStop("order"))
    await launched_for_the_house(chat_server, tool.id)
    [stop] = await stops(session_factory)
    assert (stop.for_person_id, stop.approver, stop.ask) == (None, "owner", "explicit_yes")
    assert shop.ordered == 0 and stop.yes_used_at is None
    # It waits, in the owner's conversation, as Domo's request, for the owner's yes (R16).
    [waiting] = await requests_of(session_factory)
    assert (
        waiting.status,
        waiting.by_domo,
        waiting.requester_id,
        waiting.approver_kind,
        waiting.ask,
    ) == ("pending", True, owner_chat.person.id, "owner", "explicit_yes")
    deps = made_tool_deps(app_turn_deps(chat_server.app))
    decided = partial(
        decide_confirmation,
        session_factory=session_factory,
        confirmation_id=waiting.id,
        approve=True,
        via="button",
        reply_message_id=None,
        ctx_factory=deciders(tool_context, deps, session_factory),
        registry=TOOLS,
        policy=await policy_of(session_factory),
        now=chat_server.clock(),
    )
    # An adult is not the one it waits for.
    outcome, _ = await decided(decider=adult_chat.person)
    assert outcome != "executed" and shop.ordered == 0
    # The owner's yes runs it, for the house, once.
    outcome, record = await decided(decider=owner_chat.person)
    assert outcome == "executed" and shop.ordered == 1, record
    [used] = await stops(session_factory)
    assert used.yes_used_at is not None and used.yes_approver == "owner"
    [spent] = await spending(session_factory)
    assert (spent.for_person_id, spent.amount_cents) == (None, 3250)
    # One request, counted once, in the house's history alone (M2): never in the owner's.
    decided = [
        entry
        for entry in await audit_of(session_factory, "policy.decision")
        if entry.details["action_type"] == "made.order"
    ]
    assert {entry.actor for entry in decided} == {"domo"}
    async with session_factory() as reader:
        order = DescribedAction("core", "made.order", None, "self_profile", "household")
        now = chat_server.clock()
        his = await decision_facts(reader, domo_actor(), order, now, None)
        theirs = await decision_facts(
            reader, Actor(owner_chat.person.id, "owner"), order, now, None
        )
    assert his["history"] == {"day": 1, "week": 1, "month": 1}
    assert theirs["history"] == {"day": 0, "week": 0, "month": 0}


async def test_a_house_stop_for_an_adult_is_answered_by_another_adult(
    chat_server: ChatServer,
    owner_chat: ChatPilot,
    adult_chat: ChatPilot,
    session_factory,
    grant,
    tool_context: ToolContext,
):
    # Fix round 1 (R16): an adult's agreement asked for the house is any adult's to give, the
    # owner's included; Domo is the one who asks, never the one who agrees.
    tool = await add_tool(session_factory, "order", ORDER)
    await grant("program.begin", for_domo=True, approver="adult")
    shop = Shop()
    chat_server.sandbox = by_program(order=shop)
    owner_chat.use_model(AnswersItsStop("order"))
    await launched_for_the_house(chat_server, tool.id)
    [stop] = await stops(session_factory)
    assert (stop.for_person_id, stop.approver) == (None, "adult")
    [waiting] = await requests_of(session_factory)
    assert (waiting.by_domo, waiting.approver_kind) == (True, "adult")
    deps = made_tool_deps(app_turn_deps(chat_server.app))
    outcome, record = await decide_confirmation(
        session_factory=session_factory,
        confirmation_id=waiting.id,
        decider=adult_chat.person,
        approve=True,
        via="button",
        reply_message_id=None,
        ctx_factory=deciders(tool_context, deps, session_factory),
        registry=TOOLS,
        policy=await policy_of(session_factory),
        now=chat_server.clock(),
    )
    assert outcome == "executed" and shop.ordered == 1, record
    [used] = await stops(session_factory)
    assert used.yes_approver == "adult"
    [spent] = await spending(session_factory)
    assert spent.for_person_id is None
