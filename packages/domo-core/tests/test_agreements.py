"""The agreements that last, kept and withdrawn in a turn (Task 8, spec 5.3; the tools are
Domo's, 2026-11-18): ``agreement_keep`` for the person who keeps it (a guest too) or, by the
owner, for the house; ``agreement_drop`` withdraws one's own. Both are judged and shown like the
other actions, and the context of a turn lists the person's agreements, as data."""

import json
from typing import Any

from chat_support import ChatPilot, ChatServer
from domo_core.audit.models import AuditEntry
from domo_core.channels.turns import app_turn_deps
from domo_core.conversation.prepare import made_tool_deps
from domo_core.llm.types import ToolCall
from domo_core.madetools.launch import HOUSE, launch
from domo_core.madetools.models import StandingAgreement
from fakes import FakeChatModel
from sqlalchemy import select
from test_madetools_handover import add_tool, by_program, waking
from test_madetools_in_turn import offered_in_a_turn

ORDER: dict[str, Any] = {
    "order": {"says": "Order the pizzas of the week.", "risks": ["costs"]},
    "follow": {"says": "Follow the order.", "risks": []},
}
FRIDAYS: dict[str, Any] = {
    "tool": "t_order",
    "operation": "order",
    "amount_cents_at_most": 4000,
    "times": 1,
    "per": "week",
    "weekdays": [5],
}


async def agreements(session_factory) -> list[StandingAgreement]:
    async with session_factory() as reader:
        found = await reader.scalars(
            select(StandingAgreement).order_by(StandingAgreement.created_at)
        )
        return list(found.all())


async def test_keep_and_drop(adult_chat: ChatPilot, session_factory):
    tool = await add_tool(session_factory, "order", ORDER)
    dana = adult_chat.person
    reply = await adult_chat.say_with_tool("agreement_keep", FRIDAYS, judge="do")
    assert reply.actions == [{"tool": "agreement_keep", "status": "done"}]
    # Shown like any action.
    assert reply.cards_html != ""
    [kept] = await agreements(session_factory)
    assert (kept.tool_id, kept.operation, kept.for_person_id, kept.given_by) == (
        tool.id,
        "order",
        dana.id,
        dana.id,
    )
    assert (kept.amount_cents_at_most, kept.times, kept.per, kept.weekdays) == (
        4000,
        1,
        "week",
        [5],
    )
    reply = await adult_chat.say_with_tool("agreement_drop", {"id": str(kept.id)}, judge="do")
    assert reply.actions == [{"tool": "agreement_drop", "status": "done"}]
    [dropped] = await agreements(session_factory)
    assert dropped.withdrawn_at is not None
    # Withdrawn once: there is nothing left to withdraw.
    again = await adult_chat.say_with_tool("agreement_drop", {"id": str(kept.id)}, judge="do")
    assert again.actions == [{"tool": "agreement_drop", "status": "invalid", "reason": "not_found"}]


async def test_an_operation_that_costs_nothing_needs_no_agreement(
    adult_chat: ChatPilot, session_factory
):
    await add_tool(session_factory, "order", ORDER)
    reply = await adult_chat.say_with_tool(
        "agreement_keep", {**FRIDAYS, "operation": "follow"}, judge="do"
    )
    assert reply.actions == [{"tool": "agreement_keep", "status": "invalid", "reason": "invalid"}]
    assert await agreements(session_factory) == []


async def test_a_guest_keeps_one_for_themselves(guest_chat: ChatPilot, session_factory):
    await add_tool(session_factory, "order", ORDER)
    # The test tools are open to the owner, the adults and the children: the owner opens it to a
    # guest as a build would.
    async with session_factory() as writer:
        from domo_core.madetools.models import ActionKind

        kind = await writer.get(ActionKind, "made.order")
        assert kind is not None
        kind.default_roles = ["owner", "adult", "child", "guest"]
        await writer.commit()
    reply = await guest_chat.say_with_tool("agreement_keep", FRIDAYS, judge="do")
    assert reply.actions == [{"tool": "agreement_keep", "status": "done"}]
    [kept] = await agreements(session_factory)
    assert kept.for_person_id == guest_chat.person.id


async def test_who_may_not_launch_a_tool_keeps_no_agreement_on_it(
    adult_chat: ChatPilot, session_factory, grant
):
    await add_tool(session_factory, "order", ORDER)
    await grant("made.order", person=adult_chat.person, access="deny")
    reply = await adult_chat.say_with_tool("agreement_keep", FRIDAYS, judge="do")
    assert reply.actions == [
        {"tool": "agreement_keep", "status": "invalid", "reason": "not_possible"}
    ]
    assert await agreements(session_factory) == []


async def test_house_by_an_adult_is_refused_and_by_the_owner_kept(
    adult_chat: ChatPilot, owner_chat: ChatPilot, session_factory
):
    await add_tool(session_factory, "order", ORDER)
    house = {**FRIDAYS, "for": "house"}
    reply = await adult_chat.say_with_tool("agreement_keep", house, judge="do")
    assert reply.actions == [
        {"tool": "agreement_keep", "status": "invalid", "reason": "not_possible"}
    ]
    assert await agreements(session_factory) == []
    reply = await owner_chat.say_with_tool("agreement_keep", house, judge="do")
    assert reply.actions == [{"tool": "agreement_keep", "status": "done"}]
    [kept] = await agreements(session_factory)
    assert (kept.for_person_id, kept.given_by) == (None, owner_chat.person.id)
    # An adult does not withdraw the house's either.
    reply = await adult_chat.say_with_tool("agreement_drop", {"id": str(kept.id)}, judge="do")
    assert reply.actions == [
        {"tool": "agreement_drop", "status": "invalid", "reason": "not_possible"}
    ]


async def test_the_context_lists_them(
    adult_chat: ChatPilot, owner_chat: ChatPilot, session_factory
):
    await add_tool(session_factory, "order", ORDER)
    await adult_chat.say_with_tool("agreement_keep", FRIDAYS, judge="do")
    await owner_chat.say_with_tool("agreement_keep", {**FRIDAYS, "for": "house"}, judge="do")
    _, system = await offered_in_a_turn(adult_chat)
    [line] = [line for line in system.splitlines() if line.startswith("Standing agreements")]
    listed = json.loads(line.split(": ", 1)[1])
    # Dana's own, never the house's.
    assert [(a["tool"], a["operation"], a["weekdays"], a["for"]) for a in listed] == [
        ("t_order", "order", [5], "them")
    ]
    _, system = await offered_in_a_turn(owner_chat)
    [line] = [line for line in system.splitlines() if line.startswith("Standing agreements")]
    # The owner reads the house's.
    assert [a["for"] for a in json.loads(line.split(": ", 1)[1])] == ["house"]


async def test_domo_keeps_one_for_the_house_in_a_turn_of_his_own(
    chat_server: ChatServer, owner_chat: ChatPilot, adult_chat: ChatPilot, session_factory
):
    # Task 9 (b): in a turn a launch for the house woke him for, the agreement Domo keeps for
    # the house is his own: kept, given by nobody, judged as his act.
    await add_tool(session_factory, "order", ORDER)
    bell = await add_tool(session_factory, "bell")
    chat_server.sandbox = by_program(bell=waking({"rang": True}))
    keep = ToolCall("k1", "agreement_keep", json.dumps({**FRIDAYS, "for": "house"}))
    model = owner_chat.use_model(FakeChatModel(tool_script=[keep, "Kept."], judge="do"))
    deps = made_tool_deps(app_turn_deps(chat_server.app))
    await launch(deps, bell.id, arguments={}, for_whom=HOUSE, cause="trigger")
    await chat_server.wait_for_wakes()
    [kept] = await agreements(session_factory)
    assert (kept.for_person_id, kept.given_by) == (None, None)
    # The judge's case is his own: his name, nobody's words, never the owner's.
    [judged] = model.judge_calls()
    case = json.loads(str(judged.messages[0].content))  # type: ignore[union-attr]
    assert case["person"] == {"name": "Domo", "role": "adult"}
    assert case["words"] == []
    async with session_factory() as reader:
        decided = [
            entry
            for entry in (
                await reader.scalars(
                    select(AuditEntry).where(AuditEntry.action == "policy.decision")
                )
            ).all()
            if entry.details["action_type"] == "agreement.keep"
        ]
    assert [(entry.actor, entry.details["decision"]) for entry in decided] == [("domo", "allow")]
    # An adult's, in a turn of theirs, is still refused.
    reply = await adult_chat.say_with_tool(
        "agreement_keep", {**FRIDAYS, "for": "house"}, judge="do"
    )
    assert reply.actions == [
        {"tool": "agreement_keep", "status": "invalid", "reason": "not_possible"}
    ]
    assert len(await agreements(session_factory)) == 1
