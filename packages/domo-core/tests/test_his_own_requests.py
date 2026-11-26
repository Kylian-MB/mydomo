"""A request Domo made on his own that waits for someone's yes is his (spec 12.5).

In a turn he acts in on his own, he drops it or changes it himself: a change of his is judged
again from scratch, with his own rights and the house's limits, and follows the verdict: it runs
when nothing more is asked and the barrier lets it through, else it waits for the yes the code
says (his own yes is nobody's). A person never drops one of his: they answer it yes, no, or "yes,
but" (the owner's decision of 2026-11-24): the change is made again in his name, and their yes
covers it when his judge lets it go and it needs no more than their words give. Nor does he, on
his own, drop or change a person's request under cover
of "his own", a note asked in their turn in his name (``also_domo``) included; and a person's
request changed in a turn of his stays judged as theirs (ledger line n3).

His history (what his limits read) counts once a request of his that waited for someone's yes and
was let through, however it was sent from one approver to another on the way
(``Confirmation.counted_for_domo``, the N2 remainder of the ledger).
"""

import json
import uuid
from dataclasses import replace
from typing import Any

import pytest
from chat_support import ChatPilot, ChatServer
from domo_core.accounts.models import Person
from domo_core.chat.agent import ActionRecord
from domo_core.confirmations import service as service_module
from domo_core.confirmations.judge import Judgement
from domo_core.confirmations.models import Confirmation
from domo_core.confirmations.service import amend_confirmation, may_decide
from domo_core.confirmations.store import conversation_made_in, pending_for, requester_of
from domo_core.conversation.execution import handle_tool_call
from domo_core.household.models import Invitation
from domo_core.llm.types import JsonObject, ToolCall, ToolDefinition
from domo_core.policy.actions import DescribedAction
from domo_core.policy.catalog import (
    DEFAULT_POLICY,
    NOT_ON_HIS_OWN,
    ON_THEIR_REQUESTS,
    on_his_own_requests,
    on_his_own_tables,
)
from domo_core.policy.facts import decision_facts
from domo_core.policy.models import PolicyRight
from domo_core.policy.rights import NO_RIGHTS
from domo_core.policy.roles import Actor, as_role, domo_actor
from domo_core.policy.settings import load_policy_settings
from domo_core.tools.context import InvalidArguments, ToolContext, ToolOutcome
from domo_core.tools.registry import TOOLS
from fakes import FakeChatModel, FakeSandbox
from sqlalchemy import func, select, update
from test_concerned_person import DOUBT, _judge, _people, _requests, _turn
from test_madetools_in_turn import (
    add_tool,
    audit_of,
    decisions_on,
    wakes_once,
    woken_for_the_house,
)
from test_notes_of_domo import FROM_ME, decide, waiting_requests
from test_own_idea import LIVE, invite, mark


async def requests_of(session_factory) -> list[Confirmation]:
    async with session_factory() as reader:
        found = await reader.scalars(
            select(Confirmation).order_by(Confirmation.created_at, Confirmation.id)
        )
        return list(found.all())


async def his_turn(chat_server: ChatServer, owner_chat: ChatPilot, session_factory, *calls):
    """A launch for the house wakes Domo on his own, in the owner's conversation; he makes
    ``calls`` (``ToolCall``), then says he is done."""
    tool_id = await add_tool(session_factory, f"bell_{uuid.uuid4().hex[:8]}")
    chat_server.sandbox = FakeSandbox(program=wakes_once())
    owner_chat.use_model(FakeChatModel(tool_script=[*calls, "Done."], judge="do"))
    await woken_for_the_house(chat_server, tool_id)


def note_to(name: str, text: str = "Hi") -> ToolCall:
    return ToolCall("n1", "note_leave", json.dumps({"recipient": name, "text": text}))


def expire(request_id) -> ToolCall:
    return ToolCall("e1", "confirmation_expire", json.dumps({"request_id": str(request_id)}))


def amend(request_id, **changes) -> ToolCall:
    arguments = {"request_id": str(request_id), "arguments": changes, "ask_again": False}
    return ToolCall("a1", "confirmation_amend", json.dumps(arguments))


async def his_waiting_note(
    chat_server: ChatServer, owner_chat: ChatPilot, adult_chat: ChatPilot, session_factory, grant
) -> Confirmation:
    """On his own, Domo leaves Dana a note; the owner wants to say yes to his notes first."""
    await grant("note.leave", for_domo=True, approver="owner")
    await his_turn(
        chat_server, owner_chat, session_factory, note_to(adult_chat.person.display_name)
    )
    [asked] = await waiting_requests(session_factory)
    assert (asked.by_domo, asked.approver_kind) == (True, "owner")
    return asked


async def his_history(session_factory, chat_server: ChatServer, action_type: str) -> dict:
    async with session_factory() as reader:
        action = DescribedAction("core", action_type, None, "message", "household")
        facts = await decision_facts(reader, domo_actor(), action, chat_server.clock(), None)
    return facts["history"]


# --- his own, on his own -----------------------------------------------------------------------


def test_dropping_and_changing_his_requests_are_his_on_his_own():
    policy = on_his_own_tables(DEFAULT_POLICY)
    for action_type in ("confirmation.expire", "confirmation.amend"):
        assert action_type not in NOT_ON_HIS_OWN, action_type
        assert NO_RIGHTS.resolve(domo_actor(), action_type, policy).allowed, action_type
        # The person's requests, only by a right the owner gives him (ruling R30).
        assert action_type in ON_THEIR_REQUESTS
        theirs = on_his_own_requests(policy)
        assert not NO_RIGHTS.resolve(domo_actor(), action_type, theirs).allowed, action_type
    # Showing a request again or asking more of it is his own on his own too (the owner,
    # 2026-11-24): a rule of the owner takes it back.
    for action_type in ("confirmation.remind", "confirmation.require_explicit"):
        assert action_type not in NOT_ON_HIS_OWN, action_type
        assert NO_RIGHTS.resolve(domo_actor(), action_type, policy).allowed, action_type


async def test_on_his_own_he_drops_a_request_of_his(
    chat_server: ChatServer,
    owner_chat: ChatPilot,
    adult_chat: ChatPilot,
    session_factory,
    grant,
):
    asked = await his_waiting_note(chat_server, owner_chat, adult_chat, session_factory, grant)
    await his_turn(chat_server, owner_chat, session_factory, expire(asked.id))
    [dropped] = await requests_of(session_factory)
    assert (dropped.status, dropped.expired_by) == ("expired", "domo")
    [entry] = await audit_of(session_factory, "confirmation.expired")
    assert entry.actor == "domo"
    [decided] = await decisions_on(session_factory, "confirmation.expire")
    assert (decided.actor, decided.details["decision"]) == ("domo", "allow")


async def test_his_changed_request_waits_for_whom_the_barrier_asks(
    chat_server: ChatServer,
    owner_chat: ChatPilot,
    adult_chat: ChatPilot,
    session_factory,
    grant,
    tool_context,
):
    asked = await his_waiting_note(chat_server, owner_chat, adult_chat, session_factory, grant)
    # His judge lets the change go (``his_turn``), but the barrier asks the owner's agreement.
    await his_turn(chat_server, owner_chat, session_factory, amend(asked.id, text="Bye"))
    made = {c.id: c for c in await requests_of(session_factory)}
    before = made.pop(asked.id)
    [after] = made.values()
    assert (before.status, before.replaced_by) == ("replaced", after.id)
    # Still his, still in the owner's conversation, waiting for the owner's yes: his own change is
    # no agreement, and nothing ran.
    assert (after.status, after.by_domo, after.approver_kind) == ("pending", True, "owner")
    assert after.conversation_id == before.conversation_id
    assert after.arguments["text"] == "Bye"
    # Judged as his, every time.
    decided = await decisions_on(session_factory, "note.leave")
    assert {entry.actor for entry in decided} == {"domo"}
    # The owner's yes runs it as his.
    outcome = await decide(
        chat_server, session_factory, tool_context, after, owner_chat.person, approve=True
    )
    assert outcome == "executed"


async def his_note_the_judge_doubts(
    chat_server: ChatServer, owner_chat: ChatPilot, adult_chat: ChatPilot, session_factory
) -> Confirmation:
    """On his own, Domo leaves Dana a note his judge doubts: it waits for the owner, the person of
    the turn it was made in (a note is about them), in the owner's conversation."""
    tool_id = await add_tool(session_factory, f"bell_{uuid.uuid4().hex[:8]}")
    chat_server.sandbox = FakeSandbox(program=wakes_once())
    call = note_to(adult_chat.person.display_name)
    owner_chat.use_model(FakeChatModel(tool_script=[call, "Done."], judge="yes"))
    await woken_for_the_house(chat_server, tool_id)
    [asked] = await waiting_requests(session_factory)
    assert (asked.by_domo, asked.approver_kind, asked.asked_by) == (True, "self", "judge")
    assert asked.concerned_id == owner_chat.person.id
    return asked


async def test_his_changed_request_runs_when_the_judge_says_do(
    chat_server: ChatServer,
    owner_chat: ChatPilot,
    adult_chat: ChatPilot,
    session_factory,
    stored_notes,
):
    asked = await his_note_the_judge_doubts(chat_server, owner_chat, adult_chat, session_factory)
    # On his own, he changes it; his judge lets the change go, and the barrier lets it through.
    await his_turn(chat_server, owner_chat, session_factory, amend(asked.id, text="Bye"))
    # It ran now, as his: no new request waits.
    [done] = await requests_of(session_factory)
    assert done.id == asked.id
    assert (done.status, done.by_domo, done.decided_via) == ("executed", True, "change")
    assert done.amended_arguments is not None and done.amended_arguments["text"] == "Bye"
    assert await waiting_requests(session_factory) == []
    [note] = await stored_notes()
    assert (note.by_domo, note.author_id, note.recipient_id) == (True, None, adult_chat.person.id)
    # Decided in his name, as a change.
    *_, decided = await audit_of(session_factory, "confirmation.decided")
    assert decided.actor == "domo"
    assert (decided.details["outcome"], decided.details["via"], decided.details["changed"]) == (
        "executed",
        "change",
        True,
    )


async def test_a_change_of_his_is_judged_again_with_his_rights_and_the_house_s_limits(
    chat_server: ChatServer,
    owner_chat: ChatPilot,
    adult_chat: ChatPilot,
    child_member,
    session_factory,
    grant,
):
    # His notes wait for the owner's yes; never one of his to a child.
    await grant(
        "note.leave",
        for_domo=True,
        limit='action.facts.recipient_role == "child" ? "refuse" : "ask_owner"',
    )
    await his_turn(
        chat_server, owner_chat, session_factory, note_to(adult_chat.person.display_name)
    )
    [asked] = await waiting_requests(session_factory)
    assert (asked.by_domo, asked.approver_kind) == (True, "owner")
    child = child_member.person.display_name
    await his_turn(chat_server, owner_chat, session_factory, amend(asked.id, recipient=child))
    # Refused by his limit, though the owner, whose conversation it is, may write to a child: the
    # request stays as it was, waiting.
    [still] = await requests_of(session_factory)
    assert (still.status, still.arguments["recipient"]) == ("pending", asked.arguments["recipient"])
    *_, refused = await decisions_on(session_factory, "note.leave")
    assert (refused.actor, refused.details["decision"], refused.details["reason"]) == (
        "domo",
        "refuse",
        "limit",
    )


# --- a person's request is never his ----------------------------------------------------------


async def test_on_his_own_he_never_drops_nor_changes_a_person_s_request(
    chat_server: ChatServer,
    owner_chat: ChatPilot,
    adult_chat: ChatPilot,
    session_factory,
):
    dana = adult_chat.person.display_name
    # The owner's own notes wait for the owner's own yes, one of them in his name.
    plain = await owner_chat.say_with_tool(
        "note_leave", {"recipient": dana, "text": "Hi"}, judge="yes"
    )
    in_his_name = await owner_chat.say_with_tool(
        "note_leave", {"recipient": dana, "text": "Hi", **FROM_ME}, judge="yes"
    )
    assert [plain.actions[0]["status"], in_his_name.actions[0]["status"]] == ["waiting"] * 2
    first, second = await waiting_requests(session_factory)
    assert (first.by_domo, second.by_domo) == (False, False)
    # One of them is a note in his name (the two were made in the same instant of the clock).
    assert {DescribedAction.from_json(c.action).also_domo for c in (first, second)} == {
        True,
        False,
    }
    await his_turn(
        chat_server,
        owner_chat,
        session_factory,
        expire(first.id),
        expire(second.id),
        amend(first.id, text="Bye"),
        amend(second.id, text="Bye"),
    )
    after = await requests_of(session_factory)
    assert {(c.id, c.status, c.arguments["text"]) for c in after} == {
        (first.id, "pending", "Hi"),
        (second.id, "pending", "Hi"),
    }
    assert await audit_of(session_factory, "confirmation.expired") == []


async def test_a_person_never_drops_a_request_of_his_nor_changes_it_unanswered(
    chat_server: ChatServer,
    owner_chat: ChatPilot,
    adult_chat: ChatPilot,
    session_factory,
    grant,
):
    # They answer it: yes, no, or "yes, but" (below); nothing else of theirs reaches it.
    asked = await his_waiting_note(chat_server, owner_chat, adult_chat, session_factory, grant)
    await owner_chat.say_with_tool("confirmation_expire", {"request_id": str(asked.id)})
    await owner_chat.say_with_tool(
        "confirmation_amend",
        {"request_id": str(asked.id), "arguments": {"text": "Bye"}, "ask_again": False},
    )
    [still] = await requests_of(session_factory)
    assert (still.status, still.arguments["text"]) == ("pending", "Hi")


# --- "yes, but": a person who answers a request of his changes it ---------------------------------


async def owner_says_yes_but(
    chat_server: ChatServer,
    owner_chat: ChatPilot,
    adult_chat: ChatPilot,
    session_factory,
    grant,
    *,
    judge: str,
) -> Confirmation:
    """His note to Dana ("Dinner at 20:00") waits for the owner's yes; the owner, shown it,
    answers "yes, but at 22:30", which Domo applies (his judge answering ``judge``)."""
    await grant("note.leave", for_domo=True, approver="owner")
    await his_turn(
        chat_server,
        owner_chat,
        session_factory,
        note_to(adult_chat.person.display_name, "Dinner at 20:00"),
    )
    [asked] = await waiting_requests(session_factory)
    assert (asked.by_domo, asked.approver_kind) == (True, "owner")
    shown = await owner_chat.say_with_tool("confirmation_remind", {"request_id": str(asked.id)})
    assert shown.pending_confirmation_id == asked.id
    owner_chat.use_model(
        FakeChatModel(
            tool_script=["change", amend(asked.id, text="Dinner at 22:30"), "Done."],
            judge=judge,
        )
    )
    await owner_chat.say("oui, mais à 22:30")
    return asked


async def test_the_owner_s_yes_but_at_22_30_is_done_as_his(
    chat_server: ChatServer,
    owner_chat: ChatPilot,
    adult_chat: ChatPilot,
    session_factory,
    grant,
    stored_notes,
):
    asked = await owner_says_yes_but(
        chat_server, owner_chat, adult_chat, session_factory, grant, judge="do"
    )
    # Their yes covers the change his judge lets go: the stored call ran with the new time.
    [done] = await requests_of(session_factory)
    assert done.id == asked.id
    assert (done.status, done.by_domo, done.decided_via, done.decided_by) == (
        "executed",
        True,
        "change",
        owner_chat.person.id,
    )
    assert done.amended_arguments is not None
    assert done.amended_arguments["text"] == "Dinner at 22:30"
    # Still his act: his note, judged as his.
    [note] = await stored_notes()
    assert (note.by_domo, note.author_id, note.text) == (True, None, "Dinner at 22:30")
    decided = await decisions_on(session_factory, "note.leave")
    assert {entry.actor for entry in decided} == {"domo"}


async def test_a_change_the_judge_doubts_is_asked_again_of_them(
    chat_server: ChatServer,
    owner_chat: ChatPilot,
    adult_chat: ChatPilot,
    session_factory,
    grant,
    stored_notes,
):
    asked = await owner_says_yes_but(
        chat_server, owner_chat, adult_chat, session_factory, grant, judge="yes"
    )
    # Nothing ran: a new request of his, with the change, waits for the owner again.
    assert await stored_notes() == []
    made = {c.id: c for c in await requests_of(session_factory)}
    before = made.pop(asked.id)
    [again] = made.values()
    assert (before.status, before.replaced_by) == ("replaced", again.id)
    assert (again.status, again.by_domo, again.approver_kind) == ("pending", True, "owner")
    assert (again.arguments["text"], again.asked_by) == ("Dinner at 22:30", "judge")
    # Shown to the owner by this reply: their next words answer it.
    assert again.shown_to_approver_message_id is not None
    owner_chat.use_model(FakeChatModel(["Fine."]))
    await owner_chat.say("oui")
    [note] = await stored_notes()
    assert (note.by_domo, note.text) == (True, "Dinner at 22:30")


async def test_a_person_s_request_changed_in_a_turn_of_his_is_judged_as_theirs(
    owner_chat: ChatPilot, adult_chat: ChatPilot, session_factory, tool_context, grant
):
    # Ledger line n3: whatever the turn, the change of someone's request is theirs (here opened
    # to him by the owner's right: ruling R30).
    await grant("confirmation.amend", for_domo=True, access="allow")
    dana = adult_chat.person.display_name
    reply = await owner_chat.say_with_tool(
        "note_leave", {"recipient": dana, "text": "Hi"}, judge="yes"
    )
    assert reply.actions[0]["status"] == "waiting"
    [asked] = await waiting_requests(session_factory)
    alone = replace(
        tool_context,
        conversation_id=asked.conversation_id,
        on_his_own=True,
        policy=on_his_own_tables(tool_context.policy),
        change_grant=asked.id,
    )
    outcome, _, new = await amend_confirmation(
        session_factory=session_factory,
        confirmation_id=asked.id,
        new_arguments={"text": "Bye"},
        ask_again=True,
        ctx=alone,
        now=alone.clock(),
    )
    assert outcome == "asked_again"
    assert new is not None and not new.by_domo
    *_, changed = await decisions_on(session_factory, "note.leave")
    assert changed.actor == f"person:{owner_chat.person.id}"


async def test_a_note_in_his_name_changed_by_the_person_is_judged_both_ways_again(
    owner_chat: ChatPilot, adult_chat: ChatPilot, session_factory, tool_context
):
    # Task 6's note in his name, waiting: the person's request, changed by them as theirs, and
    # judged again for them and for him.
    reply = await adult_chat.say_with_tool(
        "note_leave",
        {"recipient": owner_chat.person.display_name, "text": "Hi", **FROM_ME},
        judge="yes",
    )
    assert reply.actions[0]["status"] == "waiting"
    [asked] = await waiting_requests(session_factory)
    assert requester_of(asked) == adult_chat.person.id
    dana = adult_chat.person
    theirs = replace(
        tool_context,
        actor=Actor(dana.id, as_role(dana.role)),
        person=dana,
        conversation_id=asked.conversation_id,
        change_grant=asked.id,
    )
    outcome, _, new = await amend_confirmation(
        session_factory=session_factory,
        confirmation_id=asked.id,
        new_arguments={"text": "Bye"},
        ask_again=True,
        ctx=theirs,
        now=theirs.clock(),
    )
    assert outcome == "asked_again"
    assert new is not None and not new.by_domo
    assert DescribedAction.from_json(new.action).also_domo
    *_, for_dana, for_him = await decisions_on(session_factory, "note.leave")
    assert (for_dana.actor, for_him.actor) == (f"person:{dana.id}", "domo")


@pytest.mark.parametrize("opened", [True, False], ids=["owner_s_right", "no_right"])
async def test_on_his_own_a_person_s_request_is_his_to_drop_only_by_the_owner_s_right(
    chat_server: ChatServer,
    owner_chat: ChatPilot,
    adult_chat: ChatPilot,
    session_factory,
    grant,
    opened,
):
    # Ruling R30: the owner's right opens the person's requests to him on his own, as before.
    if opened:
        await grant("confirmation.expire", for_domo=True, access="allow")
    dana = adult_chat.person.display_name
    reply = await owner_chat.say_with_tool(
        "note_leave", {"recipient": dana, "text": "Hi"}, judge="yes"
    )
    assert reply.actions[0]["status"] == "waiting"
    [asked] = await waiting_requests(session_factory)
    await his_turn(chat_server, owner_chat, session_factory, expire(asked.id))
    [after] = await requests_of(session_factory)
    assert after.status == ("expired" if opened else "pending")


@pytest.mark.parametrize("opened", [True, False], ids=["owner_s_right", "no_right"])
async def test_on_his_own_a_person_s_request_is_his_to_change_only_by_the_owner_s_right(
    owner_chat: ChatPilot, adult_chat: ChatPilot, session_factory, tool_context, grant, opened
):
    # Ruling R30: Dana's note waits for the owner's yes; a turn of his in her conversation.
    await grant("note.leave", person=adult_chat.person, approver="owner")
    if opened:
        await grant("confirmation.amend", for_domo=True, access="allow")
    reply = await adult_chat.say_with_tool(
        "note_leave",
        {"recipient": owner_chat.person.display_name, "text": "Hi", **FROM_ME},
        judge="yes",
    )
    assert reply.actions[0]["status"] == "waiting"
    [asked] = await waiting_requests(session_factory)
    dana = adult_chat.person
    alone = replace(
        tool_context,
        actor=Actor(dana.id, as_role(dana.role)),
        person=dana,
        conversation_id=asked.conversation_id,
        on_his_own=True,
        policy=on_his_own_tables(tool_context.policy),
    )
    change = amend_confirmation(
        session_factory=session_factory,
        confirmation_id=asked.id,
        new_arguments={"text": "Bye"},
        ask_again=False,
        ctx=alone,
        now=alone.clock(),
    )
    if not opened:
        with pytest.raises(InvalidArguments):
            await change
        [still] = await requests_of(session_factory)
        assert (still.status, still.arguments["text"]) == ("pending", "Hi")
        return
    outcome, _, new = await change
    assert outcome == "asked_again"
    # Still Dana's, judged as hers and as his (her note in his name), never as his alone.
    assert new is not None and (new.by_domo, new.requester_id) == (False, dana.id)
    *_, for_dana, for_him = await decisions_on(session_factory, "note.leave")
    assert (for_dana.actor, for_him.actor) == (f"person:{dana.id}", "domo")


async def test_a_rerouted_note_in_his_name_let_through_counts_once(
    chat_server: ChatServer,
    owner_chat: ChatPilot,
    adult_chat: ChatPilot,
    child_member,
    session_factory,
    grant,
    revoke_right,
    tool_context,
):
    # Fix round 1 (I1): Dana's note in his name waits for the owner (and her judge's yes); the
    # owner lifts the right, so the owner's yes sends it back to Dana, letting it through.
    needs_owner = await grant("note.leave", person=adult_chat.person, approver="owner")
    reply = await adult_chat.say_with_tool(
        "note_leave",
        {"recipient": child_member.person.display_name, "text": "Hi", **FROM_ME},
        judge="yes",
    )
    assert reply.actions[0]["status"] == "waiting"
    [asked] = await waiting_requests(session_factory)
    assert (asked.approver_kind, asked.asked_by) == ("owner", "judge")
    assert (await his_history(session_factory, chat_server, "note.leave"))["day"] == 0
    await revoke_right(needs_owner)
    sent_back = await decide(
        chat_server, session_factory, tool_context, asked, owner_chat.person, approve=True
    )
    assert sent_back == "approver_changed"
    assert (await his_history(session_factory, chat_server, "note.leave"))["day"] == 1
    ran = await decide(
        chat_server, session_factory, tool_context, asked, adult_chat.person, approve=True
    )
    assert ran == "executed"
    assert (await his_history(session_factory, chat_server, "note.leave"))["day"] == 1


# --- his history counts a request of his once ------------------------------------------------


async def test_his_request_that_waited_with_the_judge_s_yes_counts_once_let_through(
    chat_server: ChatServer,
    owner_chat: ChatPilot,
    adult_chat: ChatPilot,
    session_factory,
    grant,
    tool_context,
):
    # The owner's yes for his notes, and his judge asks a yes too: it waited for an agreement.
    await grant("note.leave", for_domo=True, approver="owner")
    tool_id = await add_tool(session_factory, "bell")
    chat_server.sandbox = FakeSandbox(program=wakes_once())
    call = note_to(adult_chat.person.display_name)
    owner_chat.use_model(FakeChatModel(tool_script=[call, "Done."], judge="yes"))
    await woken_for_the_house(chat_server, tool_id)
    [asked] = await waiting_requests(session_factory)
    assert (asked.by_domo, asked.approver_kind, asked.asked_by) == (True, "owner", "judge")
    assert (await his_history(session_factory, chat_server, "note.leave"))["day"] == 0
    outcome = await decide(
        chat_server, session_factory, tool_context, asked, owner_chat.person, approve=True
    )
    assert outcome == "executed"
    assert (await his_history(session_factory, chat_server, "note.leave"))["day"] == 1


async def test_a_note_in_his_name_sent_on_to_the_owner_counts_once_in_his_history(
    chat_server: ChatServer,
    owner_chat: ChatPilot,
    adult_chat: ChatPilot,
    child_member,
    session_factory,
    grant,
    tool_context,
):
    # Dana's note in his name waits for her own yes (her judge asks it): he lets it through then,
    # so it is counted once already.
    reply = await adult_chat.say_with_tool(
        "note_leave",
        {"recipient": child_member.person.display_name, "text": "Hi", **FROM_ME},
        judge="yes",
    )
    assert reply.actions[0]["status"] == "waiting"
    [asked] = await waiting_requests(session_factory)
    assert asked.approver_kind == "self"
    assert (await his_history(session_factory, chat_server, "note.leave"))["day"] == 1
    # Then the owner wants to say yes to Dana's notes first: her yes sends it on to the owner.
    await grant("note.leave", person=adult_chat.person, approver="owner")
    sent_on = await decide(
        chat_server, session_factory, tool_context, asked, adult_chat.person, approve=True
    )
    assert sent_on == "approver_changed"
    ran = await decide(
        chat_server, session_factory, tool_context, asked, owner_chat.person, approve=True
    )
    assert ran == "executed"
    assert (await his_history(session_factory, chat_server, "note.leave"))["day"] == 1


# --- the invariants hold on the two new paths -----------------------------------------------------


class TakeBackAt:
    """A test-only tool: take back, at a time (``at``, which changes nothing of whose thing it is),
    a device of the person its argument names (``device.remove`` with the fact ``person``)."""

    definition = ToolDefinition(
        name="take_back",
        description="Take back a device.",
        parameters={
            "type": "object",
            "properties": {"person": {"type": "string"}, "at": {"type": "string"}},
            "additionalProperties": False,
        },
    )
    action_type = "device.remove"

    async def describe(self, arguments: JsonObject, actor: Actor, ctx: Any) -> DescribedAction:
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target="device:phone",
            effect="self_profile",
            destination="household",
            facts={"person": str(arguments["person"]), "is_self": False},
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        return ToolOutcome("done", {}, {"key": "action.card.done", "params": {}})


TAKE_BACK = {**TOOLS, "take_back": TakeBackAt()}


def take_back_at(person: Person, at: str = "20:00") -> ToolCall:
    return ToolCall("t", "take_back", json.dumps({"person": str(person.id), "at": at}))


async def his_take_back_waiting_for_dana(
    session_factory: Any, monkeypatch: pytest.MonkeyPatch, doubt: Judgement = DOUBT
) -> tuple[dict[str, Person], ToolContext, Confirmation]:
    """On his own, in Alex's conversation, he takes back Dana's device; his judge doubts it: it
    waits for Dana, in her conversation."""
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"))
    _judge(monkeypatch, doubt)
    ctx, reply_id, _ = await _turn(
        session_factory, people["Alex"], on_his_own=True, reply_message_id=None, registry=TAKE_BACK
    )
    await handle_tool_call(take_back_at(people["Dana"]), ctx=ctx, reply_id=reply_id)
    [request] = await _requests(session_factory)
    assert (request.by_domo, request.concerned_id) == (True, people["Dana"].id)
    return people, ctx, request


async def dana_says_yes_but(
    session_factory: Any, dana: Person, request: Confirmation, **changes: Any
) -> tuple[Any, Any, Any, ToolContext]:
    """Dana answers his request, in her conversation, with "yes, but" ``changes``."""
    ctx, reply_id, _ = await _turn(
        session_factory, dana, change_grant=request.id, registry=TAKE_BACK
    )
    ctx.domo_message_id = reply_id
    ctx.reply_message_id = None
    outcome, record, new = await amend_confirmation(
        session_factory=session_factory,
        confirmation_id=request.id,
        new_arguments=changes,
        ask_again=False,
        ctx=ctx,
        now=LIVE,
    )
    return outcome, record, new, ctx


def runs_seen(monkeypatch: pytest.MonkeyPatch) -> list[ToolContext]:
    """The contexts the requests' calls run in, from now on."""
    seen: list[ToolContext] = []
    run = service_module._run_and_settle

    async def running(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs["ctx"])
        return await run(*args, **kwargs)

    monkeypatch.setattr(service_module, "_run_and_settle", running)
    return seen


async def test_the_concerned_person_s_yes_but_runs_his_request_in_the_turn_it_was_made_in(
    session_factory, monkeypatch
):
    people, alexs, request = await his_take_back_waiting_for_dana(session_factory, monkeypatch)
    alex, dana = people["Alex"], people["Dana"]
    judged = _judge(monkeypatch, Judgement("do"))
    seen = runs_seen(monkeypatch)
    outcome, record, done, _ = await dana_says_yes_but(session_factory, dana, request, at="22:30")
    assert outcome == "executed", record
    # Run where it was made, as Alex's turn of his own: never a turn of Alex's in Dana's.
    [ran] = seen
    assert (ran.conversation_id, ran.person.id, ran.on_his_own) == (
        alexs.conversation_id,
        alex.id,
        True,
    )
    assert done is not None and done.id == request.id
    [done] = await _requests(session_factory)
    assert (done.status, done.decided_by, done.decided_via) == ("executed", dana.id, "change")
    # Judged as his idea.
    assert judged.cases[-1].as_json()["idea"]["of"] == "domo"


async def test_the_concerned_person_s_yes_but_he_doubts_waits_for_them_again(
    session_factory, monkeypatch
):
    people, alexs, request = await his_take_back_waiting_for_dana(session_factory, monkeypatch)
    alex, dana = people["Alex"], people["Dana"]
    seen = runs_seen(monkeypatch)
    outcome, _, new, ctx = await dana_says_yes_but(session_factory, dana, request, at="22:30")
    assert outcome == "asked_again"
    assert seen == []
    assert new is not None and new.id != request.id
    # His again; who answers is the code's: Dana, in her conversation, shown by her reply there,
    # without the judge's reason, written from Alex's turn.
    assert (new.by_domo, new.requester_id, new.concerned_id) == (True, alex.id, dana.id)
    assert (new.conversation_id, new.last_shown_message_id) == (
        ctx.conversation_id,
        ctx.domo_message_id,
    )
    assert new.ask_why is None and new.arguments["at"] == "22:30"
    assert may_decide(new, dana, LIVE) and not may_decide(new, alex, LIVE)
    async with session_factory() as reader:
        settings = await load_policy_settings(reader)
        listed = await pending_for(reader, dana.id, ctx.conversation_id, settings, LIVE)
        # Still made in Alex's turn: run and woken there.
        assert await conversation_made_in(reader, new) == alexs.conversation_id
    assert [c.id for c in listed] == [new.id]


async def test_a_yes_but_never_gives_the_explicit_yes_his_request_asked(
    session_factory, monkeypatch
):
    people, _, request = await his_take_back_waiting_for_dana(
        session_factory, monkeypatch, Judgement("explicit_yes", reason="it is her phone")
    )
    _judge(monkeypatch, Judgement("do"))
    seen = runs_seen(monkeypatch)
    outcome, _, new, _ = await dana_says_yes_but(
        session_factory, people["Dana"], request, at="22:30"
    )
    # Words never give an explicit yes: it waits for hers, explicitly, again.
    assert seen == []
    assert outcome == "asked_again"
    assert new is not None and new.ask == "explicit_yes"


async def test_a_yes_but_of_a_request_whose_turn_cannot_be_known_runs_nothing(
    session_factory, monkeypatch
):
    people, _, request = await his_take_back_waiting_for_dana(session_factory, monkeypatch)
    async with session_factory() as writer:
        await writer.execute(
            update(Confirmation).where(Confirmation.id == request.id).values(message_id=None)
        )
        await writer.commit()
    _judge(monkeypatch, Judgement("do"))
    seen = runs_seen(monkeypatch)
    with pytest.raises(InvalidArguments):
        await dana_says_yes_but(session_factory, people["Dana"], request, at="22:30")
    assert seen == []
    [still] = await _requests(session_factory)
    assert still.status == "pending"


async def his_idea_in_sam_s_turn(
    session_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, Person], ToolContext, Confirmation]:
    """Sam's turn: the assistant marks inviting Tom; his judge doubts it; it waits for Alex."""
    people = await _people(session_factory, ("Alex", "owner"), ("Sam", "guest"))
    _judge(monkeypatch, DOUBT)
    ctx, reply_id, _ = await _turn(session_factory, people["Sam"])
    await handle_tool_call(mark(), ctx=ctx, reply_id=reply_id)
    await handle_tool_call(invite(), ctx=ctx, reply_id=reply_id)
    [request] = await _requests(session_factory)
    assert (request.by_domo, request.concerned_id) == (True, people["Alex"].id)
    return people, ctx, request


async def his_change_on_his_own(
    session_factory: Any, alex: Person, request: Confirmation, **changes: Any
) -> ActionRecord:
    his, his_reply, _ = await _turn(
        session_factory,
        alex,
        on_his_own=True,
        reply_message_id=None,
        policy=on_his_own_tables(DEFAULT_POLICY),
    )
    his.domo_message_id = his_reply
    change = {"request_id": str(request.id), "arguments": changes, "ask_again": False}
    return await handle_tool_call(
        ToolCall("a", "confirmation_amend", json.dumps(change)), ctx=his, reply_id=his_reply
    )


async def test_his_change_of_an_idea_that_runs_runs_in_the_turn_it_was_born_in(
    session_factory, monkeypatch
):
    people, sams, request = await his_idea_in_sam_s_turn(session_factory, monkeypatch)
    _judge(monkeypatch, Judgement("do"))
    seen = runs_seen(monkeypatch)
    record = await his_change_on_his_own(session_factory, people["Alex"], request, guest_days=2)
    assert record.status == "done", record.model_result
    # Run in Sam's turn, as Sam's, never as a turn of his own in Alex's conversation.
    [ran] = seen
    assert (ran.conversation_id, ran.person.id, ran.on_his_own) == (
        sams.conversation_id,
        people["Sam"].id,
        False,
    )
    async with session_factory() as reader:
        [made] = await reader.scalars(select(Invitation))
        done = await reader.get(Confirmation, request.id)
    assert made.created_by == people["Sam"].id
    assert done is not None and done.status == "executed"


async def test_his_change_of_an_idea_never_runs_past_the_speaker_s_socle(
    session_factory, monkeypatch
):
    people, _, request = await his_idea_in_sam_s_turn(session_factory, monkeypatch)
    # Meanwhile the owner forbids Sam to invite, on purpose.
    async with session_factory() as writer:
        writer.add(
            PolicyRight(
                person_id=people["Sam"].id,
                action_type="household.invite",
                access="deny",
                summary="Sam may not invite.",
                owner_words="never let Sam invite",
                created_by=people["Alex"].id,
            )
        )
        await writer.commit()
    _judge(monkeypatch, Judgement("do"))
    seen = runs_seen(monkeypatch)
    await his_change_on_his_own(session_factory, people["Alex"], request, guest_days=2)
    assert seen == []
    [still] = await _requests(session_factory)
    assert (still.status, still.arguments["guest_days"]) == ("pending", 1)
    async with session_factory() as reader:
        assert await reader.scalar(select(func.count()).select_from(Invitation)) == 0
