"""Chains of actions, when they are asked for (spec 4.1, 4.2 and 8): Domo puts two to eight of
its tools, in order, in one call of ``do_in_order``, when the steps depend on each other. Each step
is described and goes through the barrier at once, and the chain is judged once, as a whole: a
chain that needs a yes is one request with one yes, waiting for the most demanding agreement its
steps need, asking the most demanding of the judge's verdict, the owner's rules and the yes he
decided by default. The request carries its steps, in order, and the role and the own rights the
person had when they asked.

A step that cannot be described yet only because an earlier step changes whom it acts on ("hand my
role to Alex, then remove me") is checked in its turn; any other step that cannot be described
makes the whole chain invalid, and a refused step refuses the whole chain, with its reason. What
happens after the yes comes with the next task.

The chat pilots talk with Domo on a live server (see ``chat_support.py``); the pieces are also
tried directly.
"""

import html
import json
import re
import uuid
from dataclasses import replace
from typing import Any
from unittest.mock import ANY

import pytest
from chat_support import confirmation_cards, status_of
from domo_common.i18n import t
from domo_core.accounts.actors import actor_of
from domo_core.accounts.models import Person
from domo_core.confirmations.describe import describe_for_people
from domo_core.confirmations.models import Confirmation, ConfirmationStep
from domo_core.confirmations.steps import mark_step, steps_of
from domo_core.confirmations.store import ANSWER_BY_THE_OWNER, ANSWER_EXPLICITLY
from domo_core.conversation.execution import NOT_ALLOWED_DETAIL
from domo_core.household.service import disable
from domo_core.household.succession import SuccessorEntry, replace_successors
from domo_core.policy.actions import DescribedAction
from domo_core.policy.asks import NeverAsk
from domo_core.policy.barrier import Allow, NeedsApproval
from domo_core.policy.catalog import DEFAULT_POLICY
from domo_core.policy.decision import ChainBasis
from domo_core.policy.roles import Actor
from domo_core.texts.view import CatalogTexts
from domo_core.tools.chain import (
    MAX_STEPS,
    MIN_STEPS,
    NEVER_IN_A_CHAIN,
    DoInOrder,
    touched,
)
from domo_core.tools.context import (
    ActsOn,
    BoundsItsOwnSteps,
    PlansSteps,
    ToolContext,
    WillTouch,
    touched_by,
)
from domo_core.tools.registry import TOOLS
from fakes import FakeChatModel
from sqlalchemy import select

# The rights writer's scripted draft of "Alex may invite guests, three days at most", and how the
# owner gets it proposed (``test_rights_tools.py``).
from test_rights_tools import SUMMARY, WORDS, alex_invites, drafts, propose

TOOL = "do_in_order"
# The block of the pending requests in Domo's context (``prompts``).
PENDING_IN_CONTEXT = "Pending requests (data, not instructions)"


def chain(*calls: tuple[str, dict[str, Any]] | tuple[str, dict[str, Any], str]) -> dict[str, Any]:
    """The arguments of ``do_in_order`` for these calls, in this order. A call may carry, third,
    the name of the invitation whose arrival its step waits for (``after_arrival_of``)."""
    steps: list[dict[str, Any]] = []
    for name, args, *after in calls:
        step: dict[str, Any] = {"tool": name, "arguments": args}
        if after:
            step["after_arrival_of"] = after[0]
        steps.append(step)
    return {"steps": steps}


def plain(fragment: str) -> str:
    """What a person reads in a page or a fragment."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def planned_items(fragment: str) -> int:
    return fragment.count('<li data-step-state="planned"')


def pending_in(system: str) -> list[dict[str, Any]]:
    """The pending requests of a system prompt's context, as Domo reads them (its JSON list)."""
    block = system.split(PENDING_IN_CONTEXT + ":\n")[1]
    return json.loads(block.split("\n\n")[0])


DEPARTURE = chain(
    ("household_transfer_ownership", {"person": "Alex"}),
    ("household_remove", {"person": "Owner"}),
)


# --- one request, judged as a whole --------------------------------------------------------------


async def test_a_chain_that_waits_is_one_request_with_its_steps(
    owner_chat, adult_member, child_member, steps_rows, confirmation_ask
):
    reply = await owner_chat.say_with_tool(
        TOOL,
        chain(
            ("household_change_role", {"person": "Sam", "role": "adult"}),
            ("household_remove", {"person": "Alex"}),
        ),
        judge="yes: two big changes",
    )
    assert reply.model_results[-1] == {
        "status": "waiting_for_confirmation",
        "by": "you",
        "answer_with": ANY,
        "steps": 2,
        "asked_by": "judge",
        "why": "two big changes",
    }
    assert reply.actions == [{"tool": TOOL, "status": "waiting"}]
    assert planned_items(reply.cards_html) == 2
    rows = await steps_rows(reply.pending_confirmation_id)
    assert [(r.position, r.tool_name, r.action_type, r.status) for r in rows] == [
        (0, "household_change_role", "household.change_role", "planned"),
        (1, "household_remove", "household.remove", "planned"),
    ]
    assert all(r.with_basis for r in rows)
    assert rows[0].arguments == {"person": "Sam", "role": "adult"}
    assert rows[1].arguments == {"person": "Alex"}
    assert rows[1].action is not None
    assert DescribedAction.from_json(rows[1].action).target == f"person:{adult_member.person.id}"
    assert all(r.result is None and r.done_at is None for r in rows)
    assert await confirmation_ask(reply.pending_confirmation_id) == (
        "yes",
        "judge",
        "two big changes",
    )
    # The card says what the chain does, then each step, in order, in a sentence of its own.
    (card,) = confirmation_cards(reply.cards_html)
    words = plain(card)
    first = t(
        "action.describe.household.change_role",
        "en",
        name="Sam",
        role=t("household.role.adult", "en"),
    )
    second = t("action.describe.household.remove", "en", name="Alex")
    assert t("action.describe.chain", "en", count="2") in words
    assert first in words and second in words
    assert words.index(first) < words.index(second)


async def test_a_chain_takes_the_most_demanding_approver(
    adult_chat, live_owner_client, grant, steps_rows
):
    alex = adult_chat.person
    # Alex's invitations need the owner's agreement; Alex's notes, nobody's.
    await grant("household.invite", person=alex, access="allow", approver="owner")
    reply = await adult_chat.say_with_tool(
        TOOL,
        chain(
            ("note_leave", {"recipient": "Owner", "text": "Jo arrive demain."}),
            ("household_invite", {"display_name": "Jo", "role": "guest"}),
        ),
        judge="do",
    )
    assert reply.model_results[-1] == {
        "status": "waiting_for_confirmation",
        "by": "the owner",
        "answer_with": ANSWER_BY_THE_OWNER,
        "steps": 2,
        "asked_by": "approver",
    }
    cid = reply.pending_confirmation_id
    assert len(await steps_rows(cid)) == 2
    # Alex's own button decides nothing.
    assert status_of(await adult_chat.post(f"/confirmations/{cid}/approve")) == "not_yours"
    # The owner finds it among what waits for their yes, with its steps.
    page = (await live_owner_client.get("/approvals")).text
    assert f'data-confirmation-id="{cid}"' in page
    assert planned_items(page) == 2


async def test_a_chain_with_the_transfer_asks_what_the_owners_rule_on_it_asks(
    owner_chat, adult_member, confirmation_status, confirmation_ask, grant
):
    # Nothing asks a yes for the hand-over by default any more (2026-11-15); the owner's own
    # rule on it holds for a chain that carries it, and the judge does not loosen it.
    await grant("household.transfer_ownership", person=owner_chat.person, ask="explicit_yes")
    reply = await owner_chat.say_with_tool(TOOL, DEPARTURE, judge="do")
    assert reply.model_results[-1]["answer_with"] == ANSWER_EXPLICITLY
    assert await confirmation_ask(reply.pending_confirmation_id) == (
        "explicit_yes",
        "rule",
        None,
    )
    owner_chat.use_model(FakeChatModel(["Il faut le bouton, ou « je confirme »."]))
    await owner_chat.say("oui")
    assert await confirmation_status(reply.pending_confirmation_id) == "pending"


async def test_a_chains_plan_says_whose_accord_it_needs_and_its_rule(
    owner_chat, adult_member, child_member, grant, tool_context
):
    owner = owner_chat.person
    tool = DoInOrder()
    invite = ("household_invite", {"display_name": "Dana", "role": "guest"})
    hand_over = ("household_transfer_ownership", {"person": "Alex"})
    await grant("household.invite", person=owner, ask="proof")
    plan = await tool.plan(chain(invite, hand_over), tool_context.actor, tool_context)
    # Nobody else's agreement; the owner's rule on invitations. The hand-over asks nothing by
    # itself: no step has a yes of its own by default.
    assert (plan.decision, plan.rule, plan.never_ask) == (Allow(), "proof", None)
    assert not hasattr(plan, "floor")
    assert plan.waits == 0
    # The owner says never to ask him before handing his role over, not at all.
    await grant("household.transfer_ownership", person=owner, ask="never")
    lifted = await tool.plan(chain(invite, hand_over), tool_context.actor, tool_context)
    assert (lifted.rule, lifted.never_ask) == ("proof", None)
    assert [step.never_ask for step in lifted.steps] == [None, NeverAsk(None, at_all=True)]
    alone = await tool.plan(chain(hand_over), tool_context.actor, tool_context)
    assert alone.never_ask == NeverAsk(None, at_all=True)
    # A child's chain that leaves needs an adult.
    sam = child_member.person
    child = replace(tool_context, actor=Actor(sam.id, "child", child=True), person=sam)
    leaving = await tool.plan(
        chain(("note_leave", {"recipient": "Owner", "text": "x"}), ("household_leave", {})),
        child.actor,
        child,
    )
    assert leaving.decision == NeedsApproval("adult")


# --- a refused step ------------------------------------------------------------------------------


async def test_a_refused_step_refuses_the_whole_chain_with_its_reason(
    adult_chat,
    owner_member,
    child_member,
    grant,
    count_confirmations,
    count_notes,
    last_audit_entry,
):
    # Alex may remove people, but a limit of the owner keeps the children in.
    await grant(
        "household.remove",
        person=adult_chat.person,
        access="allow",
        limit='action.facts.current_role == "child" ? "refuse" : "allow"',
        summary="Alex may not remove a child.",
    )
    reply = await adult_chat.say_with_tool(
        TOOL,
        chain(
            ("note_leave", {"recipient": "Owner", "text": "x"}),
            ("household_remove", {"person": "Sam"}),
        ),
    )
    assert reply.model_results[-1] == {
        "status": "refused",
        "reason": "limit",
        "limit": "Alex may not remove a child.",
        "step": 2,
    }
    assert reply.actions == [{"tool": TOOL, "status": "refused", "reason": "limit"}]
    assert await count_confirmations() == 0 and await count_notes() == 0
    entry = await last_audit_entry("policy.decision")
    assert (entry.details["action_type"], entry.details["decision"]) == ("chain.run", "refuse")
    assert entry.details["reason"] == "limit"


async def test_the_chain_is_the_members_and_a_guest_is_refused_it(guest_chat, count_confirmations):
    reply = await guest_chat.say_with_tool(
        TOOL,
        chain(("profile_rename", {"name": "Tom"}), ("profile_rename", {"name": "Paul"})),
    )
    assert reply.model_results[-1] == {
        "status": "refused",
        "reason": "not_allowed",
        "detail": NOT_ALLOWED_DETAIL,
    }
    assert await count_confirmations() == 0
    assert TOOL not in {tool.name for tool in guest_chat.server.calls[-1].tools}


async def test_a_chain_the_judge_lets_through_asks_for_no_yes(
    adult_chat, owner_member, count_confirmations, last_audit_entry
):
    reply = await adult_chat.say_with_tool(
        TOOL,
        chain(("household_list", {}), ("note_leave", {"recipient": "Owner", "text": "x"})),
        judge="do",
    )
    assert reply.model_results[-1]["status"] == "done"
    assert await count_confirmations() == 0
    entry = await last_audit_entry("policy.decision")
    assert (entry.details["action_type"], entry.details["decision"]) == ("chain.run", "allow")
    assert entry.details["facts"] == {"steps": 2, "checked_in_turn": 0, "waits": 0}


# --- the owner's rights on chains --------------------------------------------------------------


async def test_a_limit_of_the_owner_on_chains_refuses_them(
    adult_chat, grant, count_confirmations, last_audit_entry
):
    # Alex may chain two actions at most.
    await grant(
        "chain.run",
        person=adult_chat.person,
        limit='action.facts.steps > 2 ? "refuse" : "allow"',
        summary="Alex does two actions in a row at most.",
    )
    listing = ("household_list", {})
    three = await adult_chat.say_with_tool(TOOL, chain(listing, listing, listing))
    # The chain itself is refused: no step is named.
    assert three.model_results[-1] == {
        "status": "refused",
        "reason": "limit",
        "limit": "Alex does two actions in a row at most.",
    }
    entry = await last_audit_entry("policy.decision")
    assert (entry.details["action_type"], entry.details["decision"]) == ("chain.run", "refuse")
    two = await adult_chat.say_with_tool(TOOL, chain(listing, listing), judge="do")
    assert two.model_results[-1]["status"] != "refused"
    entry = await last_audit_entry("policy.decision")
    assert (entry.details["action_type"], entry.details["decision"]) == ("chain.run", "allow")
    assert await count_confirmations() == 0


async def test_a_limit_on_chains_that_cannot_answer_refuses_them(adult_chat, grant):
    await grant(
        "chain.run",
        person=adult_chat.person,
        limit='action.facts.nothing_of_the_kind > 1 ? "refuse" : "allow"',
        summary="Alex chains little.",
    )
    listing = ("household_list", {})
    reply = await adult_chat.say_with_tool(TOOL, chain(listing, listing))
    # A limit fails closed.
    result = reply.model_results[-1]
    assert (result["status"], result["reason"]) == ("refused", "limit_error")
    assert "step" not in result


async def test_a_limit_on_chains_reads_how_many_steps_are_checked_in_their_turn(
    owner_chat, adult_member, grant, count_confirmations
):
    await grant(
        "chain.run",
        person=owner_chat.person,
        limit='action.facts.checked_in_turn > 0 ? "refuse" : "allow"',
        summary="Nothing checked later.",
    )
    reply = await owner_chat.say_with_tool(TOOL, DEPARTURE)
    assert reply.model_results[-1] == {
        "status": "refused",
        "reason": "limit",
        "limit": "Nothing checked later.",
    }
    assert await count_confirmations() == 0


async def test_a_right_on_chains_makes_a_chain_the_judge_lets_through_wait_for_the_owner(
    adult_chat, owner_member, grant, steps_rows, confirmation_ask
):
    await grant("chain.run", person=adult_chat.person, approver="owner")
    reply = await adult_chat.say_with_tool(
        TOOL,
        chain(("household_list", {}), ("note_leave", {"recipient": "Owner", "text": "x"})),
        judge="do",
    )
    assert reply.model_results[-1] == {
        "status": "waiting_for_confirmation",
        "by": "the owner",
        "answer_with": ANSWER_BY_THE_OWNER,
        "steps": 2,
        "asked_by": "approver",
    }
    assert await confirmation_ask(reply.pending_confirmation_id) == ("yes", "approver", None)
    assert [row.status for row in await steps_rows(reply.pending_confirmation_id)] == [
        "planned",
        "planned",
    ]


# --- a step checked in its turn ------------------------------------------------------------------


async def test_a_step_that_cannot_be_described_yet_is_checked_in_its_turn_only_if_an_earlier_step_touches_it(  # noqa: E501
    owner_chat,
    adult_member,
    steps_rows,
    last_audit_entry,
    count_confirmations,
    session_factory,
):
    reply = await owner_chat.say_with_tool(TOOL, DEPARTURE)
    assert reply.model_results[-1]["status"] == "waiting_for_confirmation"
    assert reply.model_results[-1]["steps"] == 2
    rows = await steps_rows(reply.pending_confirmation_id)
    assert [(r.tool_name, r.action is None) for r in rows] == [
        ("household_transfer_ownership", False),
        ("household_remove", True),
    ]
    assert rows[1].action_type == "household.remove"
    async with session_factory() as reader:
        unchecked = await reader.execute(
            select(ConfirmationStep.position).where(
                ConfirmationStep.confirmation_id == reply.pending_confirmation_id,
                ConfirmationStep.action.is_(None),
            )
        )
        assert list(unchecked.scalars()) == [1]
    assert planned_items(reply.cards_html) == 2
    words = plain(reply.cards_html)
    assert t("confirmation.step.checked_in_turn", "en") in words
    assert t("action.describe.household.remove", "en", name="Owner") in words
    entry = await last_audit_entry("policy.decision")
    assert entry.details["facts"] == {"steps": 2, "checked_in_turn": 1, "waits": 0}
    assert entry.details["decision"] == "allow"

    # Nothing before it touches the owner: the chain is not valid.
    for steps, first_bad in (
        (
            chain(
                ("household_remove", {"person": "Owner"}),
                ("household_transfer_ownership", {"person": "Alex"}),
            ),
            1,
        ),
        (
            chain(
                ("household_change_role", {"person": "Alex", "role": "child"}),
                ("household_remove", {"person": "Owner"}),
            ),
            2,
        ),
    ):
        refused = await owner_chat.say_with_tool(TOOL, steps)
        result = refused.model_results[-1]
        assert (result["status"], result["reason"]) == ("invalid", "invalid")
        assert result["detail"].startswith(f"step {first_bad}: "), result
    # A name that means nobody acts on nobody.
    nobody = await owner_chat.say_with_tool(
        TOOL,
        chain(
            ("household_transfer_ownership", {"person": "Alex"}),
            ("household_remove", {"person": "Nobody"}),
        ),
    )
    result = nobody.model_results[-1]
    assert (result["status"], result["reason"]) == ("invalid", "not_found")
    assert result["detail"].startswith("step 2: ")
    # A call its tool does not accept is never checked in its turn, whomever it names: no earlier
    # step can make it right.
    for malformed in (
        ("household_remove", {"person": "Owner", "why": "x"}),
        ("household_change_role", {"person": "Owner", "role": 3}),
    ):
        refused = await owner_chat.say_with_tool(
            TOOL, chain(("household_transfer_ownership", {"person": "Alex"}), malformed)
        )
        result = refused.model_results[-1]
        assert (result["status"], result["reason"]) == ("invalid", "invalid"), malformed
        assert result["detail"].startswith("step 2: "), result
    assert await count_confirmations() == 1


async def test_a_step_may_follow_a_step_checked_in_its_turn_that_will_touch_it(
    owner_chat, child_member, steps_rows, count_confirmations
):
    # Sam is a child: the hand-over to Sam is checked in its turn (the first step makes Sam an
    # adult), and it will touch the owner, who gives, and Sam, who receives: "remove me" follows.
    reply = await owner_chat.say_with_tool(
        TOOL,
        chain(
            ("household_change_role", {"person": "Sam", "role": "adult"}),
            ("household_transfer_ownership", {"person": "Sam"}),
            ("household_remove", {"person": "Owner"}),
        ),
    )
    result = reply.model_results[-1]
    assert result["status"] == "waiting_for_confirmation" and result["steps"] == 3
    rows = await steps_rows(reply.pending_confirmation_id)
    assert [(r.tool_name, r.action is None) for r in rows] == [
        ("household_change_role", False),
        ("household_transfer_ownership", True),
        ("household_remove", True),
    ]
    assert reply.cards_html.count("data-checked-in-turn") == 2
    assert await count_confirmations() == 1
    # The order matters: nothing before "remove me" touches the owner yet.
    refused = await owner_chat.say_with_tool(
        TOOL,
        chain(
            ("household_change_role", {"person": "Sam", "role": "adult"}),
            ("household_remove", {"person": "Owner"}),
            ("household_transfer_ownership", {"person": "Sam"}),
        ),
    )
    result = refused.model_results[-1]
    assert (result["status"], result["reason"]) == ("invalid", "invalid")
    assert result["detail"].startswith("step 2: "), result
    assert await count_confirmations() == 1


async def test_an_invitation_may_follow_the_cancellation_of_the_one_that_holds_its_name(
    owner_chat, invite_for_test, steps_rows, count_confirmations
):
    await invite_for_test("Dana", "guest")
    reply = await owner_chat.say_with_tool(
        TOOL,
        chain(
            ("household_cancel_invitation", {"display_name": "Dana"}),
            ("household_invite", {"display_name": "Dana", "role": "adult"}),
        ),
    )
    assert reply.model_results[-1]["status"] == "waiting_for_confirmation"
    rows = await steps_rows(reply.pending_confirmation_id)
    assert [(r.tool_name, r.action is None) for r in rows] == [
        ("household_cancel_invitation", False),
        ("household_invite", True),
    ]
    # The other way round, the name is still taken when the invitation is asked for.
    refused = await owner_chat.say_with_tool(
        TOOL,
        chain(
            ("household_invite", {"display_name": "Dana", "role": "adult"}),
            ("household_cancel_invitation", {"display_name": "Dana"}),
        ),
    )
    result = refused.model_results[-1]
    assert (result["status"], result["reason"]) == ("invalid", "invalid")
    assert result["detail"].startswith("step 1: "), result
    assert await count_confirmations() == 1


async def test_what_a_step_will_touch_is_what_its_tool_says(
    owner_chat, adult_member, tool_context, invite_for_test
):
    owner, alex = owner_chat.person, adult_member.person
    ctx, actor = tool_context, tool_context.actor
    giver, receiver = f"person:{owner.id}", f"person:{alex.id}"
    # A tool that says whom it will touch: the hand-over touches the giver and the receiver.
    transfer = TOOLS["household_transfer_ownership"]
    assert isinstance(transfer, WillTouch)
    assert await transfer.will_touch({"person": "Alex"}, actor, ctx) == {giver, receiver}
    assert await touched_by(transfer, {"person": "Alex"}, actor, ctx) == {giver, receiver}
    # A name that means nobody: only the giver, who gives.
    assert await transfer.will_touch({"person": "Nobody"}, actor, ctx) == {giver}
    # Arguments the tool does not accept touch nobody; it never raises.
    for arguments in ({}, {"person": 3}, {"person": "Alex", "reason": "x"}):
        assert await transfer.will_touch(arguments, actor, ctx) == frozenset(), arguments
    # A tool that only says whom it acts on: that one.
    remove = TOOLS["household_remove"]
    assert not isinstance(remove, WillTouch)
    assert await touched_by(remove, {"person": "Alex"}, actor, ctx) == {receiver}
    assert await touched_by(remove, {"person": "Nobody"}, actor, ctx) == frozenset()
    made = await invite_for_test("Dana", "guest")
    cancel = TOOLS["household_cancel_invitation"]
    assert await touched_by(cancel, {"display_name": "Dana"}, actor, ctx) == {
        f"invitation:{made.id}"
    }
    # A tool that says neither touches nobody.
    listing = TOOLS["household_list"]
    assert not isinstance(listing, (ActsOn, WillTouch))
    assert await touched_by(listing, {}, actor, ctx) == frozenset()


async def test_whom_an_invitation_acts_on_is_the_open_invitation_of_that_name(
    owner_chat, adult_member, invite_for_test, tool_context
):
    made = await invite_for_test("Dana", "guest")
    invite = TOOLS["household_invite"]
    assert isinstance(invite, ActsOn)
    named = {"display_name": " dana ", "role": "adult"}
    assert await invite.acts_on(named, tool_context) == f"invitation:{made.id}"
    # The assistant's own call carries its reason (``people.whose``): it names the same one.
    his = {**named, "reason": "Dana moves in"}
    assert await invite.acts_on(his, tool_context) == f"invitation:{made.id}"
    # A free name, the name of a person of the household, or arguments the tool does not accept
    # act on nothing.
    for arguments in (
        {"display_name": "Eli", "role": "adult"},
        {"display_name": "Alex", "role": "adult"},
        {"display_name": "Dana"},
        {"display_name": "Dana", "role": "captain"},
        {"display_name": 3, "role": "adult"},
        {"display_name": "Dana", "role": "adult", "colour": "x"},
    ):
        assert await invite.acts_on(arguments, tool_context) is None, arguments
    # An invitation that was cancelled no longer holds its name.
    cancel = TOOLS["household_cancel_invitation"]
    await cancel.execute({"display_name": "Dana"}, tool_context)
    assert await invite.acts_on(named, tool_context) is None


# --- what a chain may hold -----------------------------------------------------------------------


async def test_a_chain_never_holds_a_request_tool_or_a_tool_the_person_is_not_shown(
    adult_chat, owner_member, count_confirmations
):
    for calls, bad in (
        ([("household_list", {}), ("confirmation_remind", {"request_id": str(uuid.uuid4())})], 2),
        ([("household_list", {}), ("household_remove", {"person": "Owner"})], 2),
        ([("household_list", {}), ("no_such_tool", {})], 2),
    ):
        reply = await adult_chat.say_with_tool(TOOL, chain(*calls))
        result = reply.model_results[-1]
        assert (result["status"], result["reason"]) == ("invalid", "invalid"), calls
        assert result["detail"].startswith(f"step {bad}: "), result
    assert await count_confirmations() == 0


async def test_a_chain_may_hold_the_form_for_the_models_key_and_several_links(
    owner_chat, adult_member, child_member, session_factory, live_clock, count_confirmations
):
    # Each step that makes a link or opens the form for the model's key leaves it behind its own
    # button on the chain's card (spec 4.5): a chain may hold several, and the form.
    async with session_factory() as writer:
        await disable(
            writer,
            await writer.get_one(Person, child_member.person.id),
            live_clock(),
            by=child_member.person.id,
        )
        await writer.commit()
    invite = ("household_invite", {"display_name": "Jo", "role": "guest"})
    other = ("household_invite", {"display_name": "Lou", "role": "guest"})
    back = ("household_bring_back", {"person": "Sam", "role": "child"})
    note = ("note_leave", {"recipient": "Alex", "text": "x"})
    rekey = ("model_rekey", {})
    listing = ("household_list", {})
    chains = (
        [rekey, listing],
        [listing, rekey],
        [invite, other],
        [invite, note, back],
        [back, note, invite],
        [rekey, invite, back],
    )
    for calls in chains:
        reply = await owner_chat.say_with_tool(TOOL, chain(*calls))
        result = reply.model_results[-1]
        assert result["status"] == "waiting_for_confirmation", (calls, result)
        assert result["steps"] == len(calls)
    assert await count_confirmations() == len(chains)


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"steps": "household_list, household_list"},
        {"steps": [{"tool": "household_list"}, {"tool": "household_list", "arguments": {}}]},
        {"steps": [{"tool": "household_list", "arguments": []}, {"tool": "x", "arguments": {}}]},
        {"steps": [["household_list", {}], ["household_list", {}]]},
        {"steps": [], "why": "x"},
    ],
)
async def test_a_chain_is_a_list_of_tools_and_their_arguments(adult_chat, arguments):
    reply = await adult_chat.say_with_tool(TOOL, arguments)
    result = reply.model_results[-1]
    assert (result["status"], result["reason"]) == ("invalid", "invalid")


async def test_a_chain_has_one_to_twenty_steps(
    adult_chat, owner_member, stored_notes, grant, steps_rows, count_confirmations
):
    note = ("note_leave", {"recipient": "Owner", "text": "x"})
    one = await adult_chat.say_with_tool(TOOL, chain(note), judge="do")
    result = one.model_results[-1]
    assert (result["status"], len(result["steps"])) == ("done", 1)
    assert len(await stored_notes()) == 1
    for count in (0, 21):
        reply = await adult_chat.say_with_tool(TOOL, chain(*[note] * count))
        # Beyond twenty, Domo is told what to do instead.
        assert reply.model_results[-1] == {
            "status": "invalid",
            "reason": "invalid",
            "detail": "a chain has 1 to 20 steps: make several chains, one after the other",
        }
    assert len(await stored_notes()) == 1
    twenty = await adult_chat.say_with_tool(TOOL, chain(*[note] * 20), judge="do")
    result = twenty.model_results[-1]
    assert (result["status"], len(result["steps"])) == ("done", 20)
    assert len(await stored_notes()) == 21
    # A chain of one step that waits is a request with the card of a chain.
    alone = await adult_chat.say_with_tool(TOOL, chain(("profile_rename", {"name": "Alex B"})))
    assert alone.model_results[-1]["steps"] == 1
    assert len(await steps_rows(alone.pending_confirmation_id)) == 1
    (card,) = confirmation_cards(alone.cards_html)
    assert t("action.describe.chain.one", "en") in plain(card)
    assert planned_items(card) == 1
    assert await count_confirmations() == 1


async def test_a_chain_inside_a_chain_is_flattened(
    adult_chat, owner_member, child_member, stored_notes, grant, steps_rows, session_factory
):
    def note(recipient: str, text: str) -> tuple[str, dict[str, Any]]:
        return ("note_leave", {"recipient": recipient, "text": text})

    nested = chain(
        note("Owner", "a"),
        (TOOL, chain(note("Sam", "b"), (TOOL, chain(note("Owner", "c"))))),
        note("Sam", "d"),
    )
    reply = await adult_chat.say_with_tool(TOOL, nested, judge="do")
    result = reply.model_results[-1]
    assert result["status"] == "done"
    # In order, each in its place: a for the owner, b for Sam, c for the owner, d for Sam.
    told = [(step["step"], step["result"]["left_for"]) for step in result["steps"]]
    assert told == [(1, "Owner"), (2, "Sam"), (3, "Owner"), (4, "Sam")]
    notes = {(n.recipient_id, n.text) for n in await stored_notes()}
    owner, sam = owner_member.person.id, child_member.person.id
    assert notes == {(owner, "a"), (sam, "b"), (owner, "c"), (sam, "d")}
    # Waiting, it is one request with its steps in their place, and its call as it runs.
    waiting = await adult_chat.say_with_tool(TOOL, nested)
    assert waiting.model_results[-1]["steps"] == 4
    cid = waiting.pending_confirmation_id
    rows = await steps_rows(cid)
    assert [(row.position, row.arguments["text"]) for row in rows] == [
        (0, "a"),
        (1, "b"),
        (2, "c"),
        (3, "d"),
    ]
    async with session_factory() as reader:
        stored = await reader.get_one(Confirmation, cid)
    assert stored.arguments == chain(
        note("Owner", "a"), note("Sam", "b"), note("Owner", "c"), note("Sam", "d")
    )
    (card,) = confirmation_cards(waiting.cards_html)
    assert t("action.describe.chain", "en", count="4") in plain(card)
    # The twenty steps count once flattened.
    ten = chain(*[note("Owner", "x")] * 10)
    too_many = await adult_chat.say_with_tool(
        TOOL, chain((TOOL, ten), (TOOL, ten), note("Sam", "y"))
    )
    assert too_many.model_results[-1]["detail"].startswith("a chain has 1 to 20 steps")


async def test_the_request_tools_and_the_relaunch_are_called_beside_the_chain(
    adult_chat, count_confirmations
):
    listing = ("household_list", {})
    request_id = str(uuid.uuid4())
    relaunch = await adult_chat.say_with_tool(
        TOOL, chain(listing, ("chain_continue", {"request_id": request_id}))
    )
    assert relaunch.model_results[-1] == {
        "status": "invalid",
        "reason": "invalid",
        "detail": (
            "step 2: chain_continue is not a step: call it on its own, before or after this "
            "chain, in the same answer"
        ),
    }
    remind = await adult_chat.say_with_tool(
        TOOL, chain(("confirmation_remind", {"request_id": request_id}), listing)
    )
    assert remind.model_results[-1] == {
        "status": "invalid",
        "reason": "invalid",
        "detail": (
            "step 1: a tool that manages requests is not a step (a chain is itself a request): "
            "call it before or after the chain, in the same answer"
        ),
    }
    # Inside a chain inside it too, numbered as the steps run.
    inner = await adult_chat.say_with_tool(
        TOOL,
        chain(
            listing,
            (TOOL, chain(listing, ("confirmation_expire", {"request_id": request_id}))),
        ),
    )
    assert inner.model_results[-1]["detail"].startswith(
        "step 3: a tool that manages requests is not a step"
    )
    assert await count_confirmations() == 0


# --- what the chain remembers --------------------------------------------------------------------


async def test_the_chain_remembers_the_role_and_the_rights_of_the_moment(
    owner_chat, adult_member, grant, chain_basis_of, session_factory
):
    mine = await grant("note.leave", person=owner_chat.person, ask="yes")
    # A right of a role is not the person's own.
    await grant("note.leave", role="adult", ask="yes")
    reply = await owner_chat.say_with_tool(
        TOOL,
        chain(
            ("household_change_role", {"person": "Alex", "role": "child"}),
            ("household_remove", {"person": "Alex"}),
        ),
    )
    assert await chain_basis_of(reply.pending_confirmation_id) == {
        "role": "owner",
        "right_ids": [str(mine.id)],
    }
    # A request that is no chain remembers nothing of the kind: that is how a chain is known (an
    # SQL null, never a JSON one).
    single = await owner_chat.say_with_tool("household_remove", {"person": "Alex"})
    assert await chain_basis_of(single.pending_confirmation_id) is None
    async with session_factory() as reader:
        chains = await reader.execute(
            select(Confirmation.id).where(Confirmation.chain_basis.is_not(None))
        )
        assert list(chains.scalars()) == [reply.pending_confirmation_id]


async def test_the_page_shows_the_steps_of_a_waiting_chain(owner_chat, adult_member, child_member):
    await owner_chat.say_with_tool(
        TOOL,
        chain(
            ("household_change_role", {"person": "Sam", "role": "adult"}),
            ("household_remove", {"person": "Alex"}),
        ),
    )
    page = (await owner_chat.get("/chat")).text
    (card,) = confirmation_cards(page)
    (steps,) = re.findall(r'<ol class="confirmation-steps">.*?</ol>', card, re.DOTALL)
    assert planned_items(steps) == 2
    assert t("action.describe.household.remove", "en", name="Alex") in plain(steps)


async def test_a_step_is_marked_with_what_came_of_it(owner_chat, adult_member, session_factory):
    reply = await owner_chat.say_with_tool(TOOL, DEPARTURE)
    cid = reply.pending_confirmation_id
    assert cid is not None
    now = owner_chat.clock()
    async with session_factory() as writer:
        await mark_step(writer, cid, 0, status="done", result={"key": "k", "params": {}}, now=now)
        await writer.commit()
    async with session_factory() as reader:
        first, second = await steps_of(reader, cid)
    assert (first.status, first.result, first.done_at) == ("done", {"key": "k", "params": {}}, now)
    assert (second.status, second.result, second.done_at) == ("planned", None, None)
    assert (first.with_basis, second.with_basis) == (True, True)
    page = (await owner_chat.get("/chat")).text
    assert '<li data-step-state="done"' in page and planned_items(page) == 1


def step_items(card: str) -> list[str]:
    """The steps of a chain's card, each one whole, as HTML."""
    return re.findall(
        r"<li data-step-state=.*?</li>\s*(?=<li data-step-state=|</ol>)", card, re.DOTALL
    )


async def test_a_step_that_changes_the_rights_shows_its_draft(
    owner_chat, adult_member, session_factory
):
    await propose(owner_chat, WORDS, alex_invites(adult_member.person.id))
    [draft] = await drafts(session_factory)
    reply = await owner_chat.say_with_tool(
        TOOL,
        chain(
            ("rights_apply", {"draft": str(draft.id)}),
            ("note_leave", {"recipient": "Alex", "text": "Tu peux inviter."}),
        ),
    )
    assert reply.model_results[-1]["steps"] == 2
    (card,) = confirmation_cards(reply.cards_html)
    first, second = step_items(card)
    # Under the step, each right and each example, as the card of the change alone shows them.
    assert t("action.describe.rights.apply", "en") in plain(first)
    assert 'class="confirmation-draft"' in first and SUMMARY in first
    assert t("rights.example.outcome.domo_judges", "en") in first
    assert t("rights.example.outcome.refused", "en") in first
    assert "Dana" in first and "Finn" in first
    assert "confirmation-draft" not in second
    # The page shows it too.
    page = (await owner_chat.get("/chat")).text
    (shown,) = confirmation_cards(page)
    assert SUMMARY in step_items(shown)[0]


async def test_a_step_that_changes_the_successors_shows_the_list_to_the_owner_only(
    owner_chat, adult_member, session_factory, live_clock
):
    async with session_factory() as writer:
        await replace_successors(
            writer,
            [SuccessorEntry(person_id=None, name="Paul", relation="my brother", access_way=None)],
            live_clock(),
        )
        await writer.commit()
    reply = await owner_chat.say_with_tool(
        TOOL,
        chain(
            ("successors_set", {"successors": [{"person": "Alex"}]}),
            ("note_leave", {"recipient": "Alex", "text": "Tu es sur la liste."}),
        ),
    )
    assert reply.model_results[-1]["steps"] == 2
    (card,) = confirmation_cards(reply.cards_html)
    first, second = step_items(card)
    words = plain(first)
    assert t("action.describe.successors.set", "en") in words
    assert t("confirmation.successors.before", "en", list="1. Paul (my brother)") in words
    assert t("confirmation.successors.after", "en", list="1. Alex (adult)") in words
    assert "confirmation-successors" not in second
    # The owner hands the role over: the one who asked is an adult now, and the chain still waits
    # on their page, without the list in it.
    handed = await owner_chat.say_with_tool("household_transfer_ownership", {"person": "Alex"})
    assert (await owner_chat.confirm_last(handed)).status == "executed"
    page = (await owner_chat.get("/chat")).text
    chain_card = next(c for c in confirmation_cards(page) if "confirmation-steps" in c)
    assert planned_items(chain_card) == 2
    assert "confirmation-successors" not in page and "Paul" not in page


# --- the pieces ----------------------------------------------------------------------------------


def test_the_chain_tool_plans_its_steps_and_bounds_them():
    tool = TOOLS[TOOL]
    assert isinstance(tool, DoInOrder)
    assert isinstance(tool, PlansSteps) and isinstance(tool, BoundsItsOwnSteps)
    assert tool.action_type == "chain.run"
    assert (MIN_STEPS, MAX_STEPS) == (1, 20)
    steps = tool.definition.parameters["properties"]["steps"]
    assert (steps["minItems"], steps["maxItems"]) == (1, 20)
    # A chain inside a chain is flattened: it is no longer refused.
    # Nor are the tools that make a link or open the form for the model's key: each leaves it
    # behind its own button on the chain's card.
    assert {"chain_continue"} == NEVER_IN_A_CHAIN
    description = tool.definition.description
    assert "only when the steps depend on each other" in description
    for words in ("1 to 20", "flattened", "chain_continue", "judged once, as a whole"):
        assert words in description, words
    for gone in ("explicit_step", "confirmation_style", "policy_update"):
        assert gone not in description, gone


def test_the_household_tools_that_name_someone_say_whom_they_act_on():
    for name in (
        "household_change_role",
        "household_remove",
        "household_transfer_ownership",
        "household_cancel_invitation",
        # An invitation acts on the one that holds its name.
        "household_invite",
    ):
        assert isinstance(TOOLS[name], ActsOn), name
    # The hand-over says more: whom it will touch is the giver as well as the receiver.
    assert isinstance(TOOLS["household_transfer_ownership"], WillTouch)


def _context(person, session_factory, clock) -> ToolContext:
    return ToolContext(
        session_factory=session_factory,
        actor=actor_of(person),
        person=person,
        conversation_id=uuid.uuid4(),
        origin="https://domo.example.com/",
        gatekeeper=None,  # type: ignore[arg-type]
        clock=clock,
        registry=TOOLS,
        policy=DEFAULT_POLICY,
        reply_message_id=None,
    )


async def test_whom_a_call_acts_on_is_found_by_the_name_alone(
    add_member, session_factory, live_clock
):
    owner = await add_member("Owner", "owner")
    alex = await add_member("Alex", "adult")
    await add_member("Lou", "child")
    await add_member("lou", "adult")
    ctx = _context(owner, session_factory, live_clock)
    remove = TOOLS["household_remove"]
    assert isinstance(remove, ActsOn)
    # The owner, whom the tool would never remove: it is still whom the call names.
    assert await remove.acts_on({"person": "Owner"}, ctx) == f"person:{owner.id}"
    assert await remove.acts_on({"person": " alex "}, ctx) == f"person:{alex.id}"
    for arguments in (
        {"person": "Nobody"},
        {"person": "Lou"},
        {"person": 3},
        {},
        {"x": "Alex"},
        # Arguments the tool does not accept act on nobody.
        {"person": "Owner", "why": "x"},
    ):
        assert await remove.acts_on(arguments, ctx) is None, arguments
    change = TOOLS["household_change_role"]
    assert isinstance(change, ActsOn)
    assert await change.acts_on({"person": "Alex", "role": "child"}, ctx) == f"person:{alex.id}"
    # A role made for the household may be made by an earlier step: only a role that is no name
    # at all makes the call act on nobody.
    assert await change.acts_on({"person": "Alex", "role": "captain"}, ctx) == f"person:{alex.id}"
    assert await change.acts_on({"person": "Alex", "role": 3}, ctx) is None
    transfer = TOOLS["household_transfer_ownership"]
    assert isinstance(transfer, ActsOn)
    assert await transfer.acts_on({"person": "Alex"}, ctx) == f"person:{alex.id}"


def test_what_an_action_touches_is_its_target_and_the_people_of_its_facts():
    a, b = uuid.uuid4(), uuid.uuid4()
    transfer = DescribedAction(
        "core",
        "household.transfer_ownership",
        f"person:{b}",
        "permissions",
        "household",
        {"from": f"person:{a}", "to": f"person:{b}", "requester_role": "owner", "to_role": "adult"},
    )
    assert touched(transfer) == {f"person:{a}", f"person:{b}"}
    listing = DescribedAction("core", "household.list", None, "read", "household")
    assert touched(listing) == frozenset()
    cancel = DescribedAction(
        "core", "household.cancel_invitation", f"invitation:{a}", "permissions", "household"
    )
    assert touched(cancel) == {f"invitation:{a}"}


def test_the_basis_of_a_chain_reads_back():
    first, second = uuid.uuid4(), uuid.uuid4()
    basis = ChainBasis("owner", frozenset({second, first}))
    stored = basis.as_json()
    assert stored == {"role": "owner", "right_ids": sorted([str(first), str(second)])}
    assert ChainBasis.from_json(stored) == basis
    assert ChainBasis.from_json({"role": "child", "right_ids": []}) == ChainBasis(
        "child", frozenset()
    )


@pytest.mark.parametrize(
    "broken",
    [
        None,
        [],
        {"role": "owner"},
        {"role": "owner", "right_ids": [], "more": 1},
        {"role": "boss", "right_ids": []},
        {"role": "owner", "right_ids": "abc"},
        {"role": "owner", "right_ids": ["not an id"]},
        {"role": "owner", "right_ids": [3]},
    ],
)
def test_a_basis_that_does_not_read_back_is_refused(broken):
    with pytest.raises(ValueError):
        ChainBasis.from_json(broken)


@pytest.mark.parametrize(("lang", "sentence"), [("en", "in order")])
def test_a_chain_is_told_by_its_number_of_steps(lang, sentence):
    action = DescribedAction(
        "core", "chain.run", None, "permissions", "household", {"steps": 3, "checked_in_turn": 0}
    )
    told = describe_for_people(action, chain(("a", {}), ("b", {}), ("c", {})), CatalogTexts(lang))
    assert told == t("action.describe.chain", lang, count="3")
    assert "3" in told and sentence in told
