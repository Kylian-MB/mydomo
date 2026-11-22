"""A request Domo made on his own that waits for someone's yes is his (spec 12.5).

In a turn he acts in on his own, he drops it or changes it himself: a change of his is judged
again from scratch, with his own rights and the house's limits, and always waits again for the yes
of whoever it waited for (his own yes is nobody's). A person never drops nor changes one of his:
they answer it yes or no. Nor does he, on his own, drop or change a person's request under cover
of "his own", a note asked in their turn in his name (``also_domo``) included; and a person's
request changed in a turn of his stays judged as theirs (ledger line n3).

His history (what his limits read) counts once a request of his that waited for someone's yes and
was let through, however it was sent from one approver to another on the way
(``Confirmation.counted_for_domo``, the N2 remainder of the ledger).
"""

import json
import uuid
from dataclasses import replace

import pytest
from chat_support import ChatPilot, ChatServer
from domo_core.confirmations.models import Confirmation
from domo_core.confirmations.service import amend_confirmation
from domo_core.confirmations.store import requester_of
from domo_core.llm.types import ToolCall
from domo_core.policy.actions import DescribedAction
from domo_core.policy.catalog import (
    DEFAULT_POLICY,
    NOT_ON_HIS_OWN,
    ON_THEIR_REQUESTS,
    on_his_own_requests,
    on_his_own_tables,
)
from domo_core.policy.facts import decision_facts
from domo_core.policy.rights import NO_RIGHTS
from domo_core.policy.roles import Actor, as_role, domo_actor
from domo_core.tools.context import InvalidArguments
from fakes import FakeChatModel, FakeSandbox
from sqlalchemy import select
from test_madetools_in_turn import (
    add_tool,
    audit_of,
    decisions_on,
    wakes_once,
    woken_for_the_house,
)
from test_notes_of_domo import FROM_ME, decide, waiting_requests


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
    # Showing a request again or asking more of it is about the person's own requests.
    for action_type in ("confirmation.remind", "confirmation.require_explicit"):
        assert action_type in NOT_ON_HIS_OWN, action_type


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


async def test_on_his_own_he_changes_a_request_of_his_and_it_waits_again_for_its_approver(
    chat_server: ChatServer,
    owner_chat: ChatPilot,
    adult_chat: ChatPilot,
    session_factory,
    grant,
    tool_context,
):
    asked = await his_waiting_note(chat_server, owner_chat, adult_chat, session_factory, grant)
    await his_turn(chat_server, owner_chat, session_factory, amend(asked.id, text="Bye"))
    made = {c.id: c for c in await requests_of(session_factory)}
    before = made.pop(asked.id)
    [after] = made.values()
    assert (before.status, before.replaced_by) == ("replaced", after.id)
    # Still his, still in the owner's conversation, waiting for the owner's yes: his own change is
    # no yes.
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


async def test_a_person_never_drops_nor_changes_a_request_of_his(
    chat_server: ChatServer,
    owner_chat: ChatPilot,
    adult_chat: ChatPilot,
    session_factory,
    grant,
):
    asked = await his_waiting_note(chat_server, owner_chat, adult_chat, session_factory, grant)
    await owner_chat.say_with_tool("confirmation_expire", {"request_id": str(asked.id)})
    await owner_chat.say_with_tool(
        "confirmation_amend",
        {"request_id": str(asked.id), "arguments": {"text": "Bye"}, "ask_again": False},
    )
    [still] = await requests_of(session_factory)
    assert (still.status, still.arguments["text"]) == ("pending", "Hi")


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
