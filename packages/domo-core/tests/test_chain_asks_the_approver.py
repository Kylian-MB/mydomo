"""A chain that nobody said yes to (the judge let it through, or it ran at once) and that meets a
step needing someone's agreement (an adult for a child, the owner by a right or a limit) does not
stop dead: the rest of it (that step and those after it) waits as a request for that approver, the
barrier's for that step, in the conversation the chain was made in. Their yes runs the rest, each
step again under the barrier; their no ends it. What was done stays done (spec 4.3, JDG-9).

The chat pilots talk with Domo on a live server (see ``chat_support.py``).
"""

import uuid
from typing import Any

import domo_core.tools.chain as chain_tool
import pytest
from chat_support import ChatPilot, status_of
from domo_core.chat.models import Conversation, Message, MessageAction
from domo_core.confirmations.chain import WAITING_FOR_ANOTHER_YES
from domo_core.confirmations.service import (
    DecideOutcome,
    decide_confirmation,
    may_decide,
    requester_context,
)
from domo_core.confirmations.store import awaiting_approval, conversation_made_in
from domo_core.policy.catalog import DEFAULT_POLICY
from domo_core.policy.roles import Actor, as_role
from domo_core.policy.settings import load_policy_settings
from domo_core.tools.context import ToolContext, judged_actor_of
from domo_core.tools.registry import TOOLS
from sqlalchemy import select
from test_chain_arrival import invitation_named, request_row
from test_chain_plan import chain

TOOL = "do_in_order"
# Alex leaves one note a day freely; past it, the owner agrees first (a limit sends to the owner).
ONE_THEN_THE_OWNER = 'history.day < 1 ? "allow" : "ask_owner"'
# The same, and past two the notes are refused.
ONE_THE_OWNER_THEN_NO = 'history.day < 1 ? "allow" : (history.day < 2 ? "ask_owner" : "refuse")'


def note(text: str, recipient: str = "Owner", after: str | None = None) -> tuple[Any, ...]:
    arguments = {"recipient": recipient, "text": text}
    return ("note_leave", arguments) if after is None else ("note_leave", arguments, after)


def rows_of(rows: list[Any]) -> list[tuple[int, str]]:
    return [(row.position, row.status) for row in rows]


async def decide_as(
    pilot: ChatPilot, confirmation_id: uuid.UUID, *, via: Any, approve: bool = True
) -> DecideOutcome:
    """``pilot``'s answer to the request, through the service itself, given ``via``."""
    person = pilot.person
    base = ToolContext(
        session_factory=pilot.server.session_factory,
        actor=Actor(person.id, as_role(person.role)),
        person=person,
        conversation_id=confirmation_id,
        origin=pilot.server.base_url,
        gatekeeper=None,  # type: ignore[arg-type]
        clock=pilot.clock,
        registry=TOOLS,
        policy=DEFAULT_POLICY,
        reply_message_id=None,
    )
    outcome, _ = await decide_confirmation(
        session_factory=pilot.server.session_factory,
        confirmation_id=confirmation_id,
        decider=person,
        approve=approve,
        via=via,
        reply_message_id=None,
        ctx_factory=requester_context(base),
        registry=TOOLS,
        policy=DEFAULT_POLICY,
        now=pilot.clock(),
    )
    return outcome


async def test_a_let_through_chain_that_meets_an_adult_s_agreement_waits_for_that_adult(
    child_chat,
    adult_member,
    owner_member,
    grant,
    invite_for_test,
    join_with_invitation,
    upkeep_pass,
    steps_rows,
    stored_notes,
    confirmation_status,
    session_factory,
):
    sam = child_chat.person
    await invite_for_test("Tom", "guest")
    reply = await child_chat.say_with_tool(
        TOOL, chain(note("a"), note("b", after="Tom"), note("c")), judge="do"
    )
    cid = reply.pending_confirmation_id
    assert await confirmation_status(cid) == "waiting_arrival"
    assert (await request_row(session_factory, cid)).decided_via == "judge"
    # While it waits, the owner wants an adult to agree to Sam's notes.
    await grant("note.leave", person=sam, approver="adult")
    tom = await invitation_named(session_factory, "Tom")
    await join_with_invitation(tom.id, "tom")
    await upkeep_pass()
    # The rest waits for an adult; step 1 is done, steps 2 and 3 wait with it.
    c = await request_row(session_factory, cid)
    assert (c.status, c.approver_kind, c.concerned_id) == ("pending", "adult", None)
    # Nobody's rule asks more of the step: a plain yes of the adult.
    assert (c.ask, c.asked_by, c.ask_why) == ("yes", "approver", None)
    assert rows_of(await steps_rows(cid)) == [(0, "done"), (1, "planned"), (2, "planned")]
    assert sorted(n.text for n in await stored_notes()) == ["a"]
    now = child_chat.clock()
    assert may_decide(c, adult_member.person, now) and not may_decide(c, sam, now)
    async with session_factory() as reader:
        settings = await load_policy_settings(reader)
        listed, _ = await awaiting_approval(reader, adult_member.person, settings, now, limit=20)
    assert cid in [request.id for request, _ in listed]


async def test_a_chain_run_at_once_gets_a_request_for_its_rest(
    adult_chat, owner_member, grant, count_confirmations, stored_notes, steps_rows, session_factory
):
    alex = adult_chat.person
    await grant("note.leave", person=alex, limit=ONE_THEN_THE_OWNER, summary="One note a day.")
    assert await count_confirmations() == 0
    reply = await adult_chat.say_with_tool(TOOL, chain(note("a"), note("b"), note("c")), judge="do")
    assert sorted(n.text for n in await stored_notes()) == ["a"]
    assert reply.actions == [{"tool": TOOL, "status": "waiting"}]
    result = reply.model_results[-1]
    assert (result["chain"], result["by"], result["done"], result["left"]) == (
        WAITING_FOR_ANOTHER_YES,
        "the owner",
        1,
        2,
    )
    cid = uuid.UUID(result["request_id"])
    assert await count_confirmations() == 1
    c = await request_row(session_factory, cid)
    assert (c.status, c.approver_kind, c.concerned_id, c.requester_id, c.by_domo) == (
        "pending",
        "owner",
        None,
        alex.id,
        False,
    )
    assert (c.message_id, c.tool_name) == (reply.message_id, TOOL)
    assert c.chain_basis is not None
    async with session_factory() as reader:
        made_in = await conversation_made_in(reader, c)
        whose = await reader.scalar(
            select(Conversation.person_id).where(Conversation.id == c.conversation_id)
        )
    assert made_in == c.conversation_id and whose == alex.id
    assert rows_of(await steps_rows(cid)) == [(0, "done"), (1, "planned"), (2, "planned")]
    assert reply.pending_confirmation_id == cid


@pytest.mark.parametrize("answer", ["approve", "refuse"])
async def test_the_approver_s_yes_runs_the_rest_and_their_no_ends_it(
    adult_chat, owner_chat, grant, stored_notes, steps_rows, confirmation_status, answer
):
    alex = adult_chat.person
    await grant("note.leave", person=alex, limit=ONE_THEN_THE_OWNER, summary="One note a day.")
    reply = await adult_chat.say_with_tool(TOOL, chain(note("a"), note("b"), note("c")), judge="do")
    cid = uuid.UUID(reply.model_results[-1]["request_id"])
    # Alex cannot give it; the owner can.
    assert status_of(await adult_chat.post(f"/confirmations/{cid}/approve")) == "not_yours"
    answered = await owner_chat.post(f"/confirmations/{cid}/{answer}")
    if answer == "approve":
        assert status_of(answered) == "executed"
        assert sorted(n.text for n in await stored_notes()) == ["a", "b", "c"]
        assert rows_of(await steps_rows(cid)) == [(0, "done"), (1, "done"), (2, "done")]
    else:
        assert status_of(answered) == "refused"
        assert await confirmation_status(cid) == "refused"
        assert sorted(n.text for n in await stored_notes()) == ["a"]


async def test_an_owner_s_explicit_yes_rule_on_the_step_is_carried(
    owner_chat, adult_chat, grant, stored_notes, confirmation_status, confirmation_ask, person_row
):
    # Once the owner hands the role over, the adults' notes wait for the new owner's firm yes.
    await grant("note.leave", role="adult", approver="owner", ask="explicit_yes")
    reply = await owner_chat.say_with_tool(
        TOOL,
        chain(("household_transfer_ownership", {"person": "Alex"}), note("bye", "Alex")),
        judge="do",
    )
    assert (await person_row(adult_chat.person)).role == "owner"
    result = reply.model_results[-1]
    assert (result["chain"], result["by"]) == (WAITING_FOR_ANOTHER_YES, "the owner")
    cid = uuid.UUID(result["request_id"])
    assert await confirmation_ask(cid) == ("explicit_yes", "rule", None)
    # Words alone (a plain yes) do not give it; the button does.
    assert await decide_as(adult_chat, cid, via="phrase") != "executed"
    assert await confirmation_status(cid) == "pending" and await stored_notes() == []
    assert status_of(await adult_chat.post(f"/confirmations/{cid}/approve")) == "executed"
    assert sorted(n.text for n in await stored_notes()) == ["bye"]


async def test_a_refusal_at_a_step_still_stops_the_chain(
    adult_chat, owner_chat, grant, stored_notes, steps_rows, confirmation_status
):
    alex = adult_chat.person
    await grant("note.leave", person=alex, limit=ONE_THE_OWNER_THEN_NO, summary="Two at most.")
    reply = await adult_chat.say_with_tool(TOOL, chain(note("a"), note("b"), note("c")), judge="do")
    cid = uuid.UUID(reply.model_results[-1]["request_id"])
    # The owner agrees to the rest: the second note is left, the third is refused by the limit.
    assert status_of(await owner_chat.post(f"/confirmations/{cid}/approve")) == "failed"
    assert await confirmation_status(cid) == "failed"
    assert rows_of(await steps_rows(cid)) == [(0, "done"), (1, "done"), (2, "failed")]
    assert sorted(n.text for n in await stored_notes()) == ["a", "b"]


async def test_a_chain_that_had_a_yes_is_rerouted_as_before(
    adult_chat, owner_chat, grant, steps_rows, stored_notes, session_factory
):  # a guard: the yes path keeps what it asked (Ruling R27), only its approver changes
    alex = adult_chat.person
    reply = await adult_chat.say_with_tool(TOOL, chain(note("a"), note("b")))
    cid = reply.pending_confirmation_id
    before = await request_row(session_factory, cid)
    # Past the first note, the owner agrees first.
    await grant("note.leave", person=alex, limit=ONE_THEN_THE_OWNER, summary="One note a day.")
    assert (await adult_chat.confirm_last(reply)).status == "approver_changed"
    c = await request_row(session_factory, cid)
    assert (c.status, c.approver_kind) == ("pending", "owner")
    assert (c.ask, c.asked_by) == (before.ask, before.asked_by)
    assert rows_of(await steps_rows(cid)) == [(0, "done"), (1, "planned")]
    assert status_of(await owner_chat.post(f"/confirmations/{cid}/approve")) == "executed"
    assert sorted(n.text for n in await stored_notes()) == ["a", "b"]


async def test_a_later_step_that_needs_the_owner_after_an_adult_s_yes_waits_for_the_owner(
    child_chat,
    adult_chat,
    owner_chat,
    grant,
    invite_for_test,
    join_with_invitation,
    upkeep_pass,
    steps_rows,
    stored_notes,
    session_factory,
):  # the adult's yes does not cover a step that needs the owner's: it is rerouted again
    sam = child_chat.person
    await invite_for_test("Tom", "guest")
    reply = await child_chat.say_with_tool(
        TOOL, chain(note("a"), note("b", after="Tom"), note("c")), judge="do"
    )
    cid = reply.pending_confirmation_id
    # An adult agrees to Sam's notes, and past two a day the owner does.
    await grant(
        "note.leave", person=sam, approver="adult", limit='history.day < 2 ? "allow" : "ask_owner"'
    )
    tom = await invitation_named(session_factory, "Tom")
    await join_with_invitation(tom.id, "tom")
    await upkeep_pass()
    assert (await request_row(session_factory, cid)).approver_kind == "adult"
    assert status_of(await adult_chat.post(f"/confirmations/{cid}/approve")) == "approver_changed"
    c = await request_row(session_factory, cid)
    assert (c.status, c.approver_kind) == ("pending", "owner")
    assert rows_of(await steps_rows(cid)) == [(0, "done"), (1, "done"), (2, "planned")]
    assert sorted(n.text for n in await stored_notes()) == ["a", "b"]
    assert status_of(await owner_chat.post(f"/confirmations/{cid}/approve")) == "executed"
    assert sorted(n.text for n in await stored_notes()) == ["a", "b", "c"]


async def test_the_steps_after_the_one_that_waits_are_described_now(
    adult_chat, owner_member, grant, steps_rows, session_factory
):  # the approver sees their targets, and a target that changed stops them in their turn
    alex = adult_chat.person
    await grant("note.leave", person=alex, limit=ONE_THEN_THE_OWNER, summary="One note a day.")
    reply = await adult_chat.say_with_tool(TOOL, chain(note("a"), note("b"), note("c")), judge="do")
    cid = uuid.UUID(reply.model_results[-1]["request_id"])
    rows = await steps_rows(cid)
    assert [row.action is not None for row in rows] == [True, True, True]
    assert rows[2].action is not None and rows[2].action["action_type"] == "note.leave"
    c = await request_row(session_factory, cid)
    assert c.action["facts"]["checked_in_turn"] == 0


async def test_domo_reads_the_results_of_the_steps_done_before_the_rest_waits(
    adult_chat, owner_member, grant
):
    alex = adult_chat.person
    await grant("note.leave", person=alex, limit=ONE_THEN_THE_OWNER, summary="One note a day.")
    reply = await adult_chat.say_with_tool(TOOL, chain(note("a"), note("b"), note("c")), judge="do")
    result = reply.model_results[-1]
    assert result["chain"] == WAITING_FOR_ANOTHER_YES
    [done] = result["steps"]
    assert (done["step"], done["action_type"], done["status"]) == (1, "note.leave", "done")
    assert done["result"]["status"] == "done"


async def test_a_rest_that_cannot_be_stored_tells_the_steps_done_as_a_stop(
    adult_chat, owner_member, grant, stored_notes, count_confirmations, session_factory, monkeypatch
):  # nothing is lost: what ran is told and counted, as before the rest could wait
    alex = adult_chat.person
    await grant("note.leave", person=alex, limit=ONE_THEN_THE_OWNER, summary="One note a day.")

    async def cannot_store(*_: Any, **__: Any) -> Any:
        raise RuntimeError("the database is away")

    monkeypatch.setattr(chain_tool, "create_confirmation", cannot_store)
    reply = await adult_chat.say_with_tool(TOOL, chain(note("a"), note("b"), note("c")), judge="do")
    assert reply.actions == [{"tool": TOOL, "status": "failed", "reason": "needs_confirmation"}]
    assert await count_confirmations() == 0
    assert sorted(n.text for n in await stored_notes()) == ["a"]
    result = reply.model_results[-1]
    assert (result["stopped_at"], result["reason"], result["left"]) == (2, "needs_confirmation", 1)
    assert [(step["step"], step["status"]) for step in result["steps"]] == [
        (1, "done"),
        (2, "failed"),
    ]
    async with session_factory() as reader:
        summary = await reader.scalar(
            select(MessageAction.summary).where(MessageAction.message_id == reply.message_id)
        )
    assert summary is not None and summary["did"] == ["note.leave"]


async def test_a_chain_run_at_once_in_a_turn_of_his_own_leaves_a_request_of_his(
    owner_chat, adult_member, grant, session_factory
):  # the request is his exactly when the action that shows it is (``_waits``: judged as Domo)
    owner = owner_chat.person
    await grant("note.leave", person=owner, approver="adult")
    said = await owner_chat.say("Bonjour")
    async with session_factory() as reader:
        conversation_id = await reader.scalar(
            select(Message.conversation_id).where(Message.id == said.message_id)
        )
    assert conversation_id is not None
    tool = TOOLS[TOOL]
    for on_his_own in (True, False):
        ctx = ToolContext(
            session_factory=session_factory,
            actor=Actor(owner.id, as_role(owner.role)),
            person=owner,
            conversation_id=conversation_id,
            origin=owner_chat.server.base_url,
            gatekeeper=None,  # type: ignore[arg-type]
            clock=owner_chat.clock,
            registry=TOOLS,
            policy=DEFAULT_POLICY,
            reply_message_id=None,
            domo_message_id=said.message_id,
            on_his_own=on_his_own,
        )
        outcome = await tool.execute(chain(note("a", "Alex")), ctx)
        assert outcome.confirmation_id is not None, outcome
        c = await request_row(session_factory, outcome.confirmation_id)
        assert c.approver_kind == "adult"
        assert c.by_domo is on_his_own
        assert c.by_domo == (judged_actor_of(tool, ctx).subject == "domo")
