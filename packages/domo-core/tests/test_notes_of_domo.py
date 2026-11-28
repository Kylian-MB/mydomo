"""A note of Domo's own (the owner, 2026-11-21: "Domo can leave a note in his own name").

In a turn he acts in on his own, a note Domo leaves is his: no person is its author
(``Note.by_domo``, ``author_id`` empty). In a person's turn, a note is that person's, unless the
call says ``{"from": "me"}``: Domo's own word to someone, judged with his own rights (a
``for_domo`` deny stops it), counted in his history and never in theirs. Whoever reads a note of
his reads that it is his (``DOMO_NAME``, or the name the reader calls him by), never someone
else's; and a person's note never becomes his (Review Focus 5). On his own, he finds, takes back
and keeps his own notes, and nobody else's; a person never reaches his.
"""

import json
from dataclasses import replace
from datetime import timedelta

import pytest
from chat_support import ChatPilot, ChatServer
from domo_core.accounts.models import Person
from domo_core.channels.turns import app_turn_deps
from domo_core.confirmations.models import Confirmation
from domo_core.confirmations.service import decide_confirmation
from domo_core.conversation.prepare import made_tool_deps
from domo_core.household.models import Invitation
from domo_core.llm.types import ToolCall
from domo_core.notes.models import Note
from domo_core.policy.actions import DescribedAction
from domo_core.policy.catalog import (
    DEFAULT_POLICY,
    NOT_ON_HIS_OWN,
    on_his_own_tables,
)
from domo_core.policy.facts import DOMO_NAME, person_history
from domo_core.policy.rights import NO_RIGHTS
from domo_core.policy.roles import domo_actor
from domo_core.prompts import NoteToTell
from domo_core.tools.context import InvalidArguments, OutsideText, judged_actor_of
from domo_core.tools.notes import notes_to_tell
from domo_core.tools.registry import TOOLS
from fakes import FakeChatModel, FakeSandbox, is_judge_call
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from test_madetools_in_turn import (
    add_tool,
    audit_of,
    decisions_on,
    wakes_once,
    woken_for_the_house,
)
from test_madetools_yes import deciders, policy_of
from test_notes import call_context, main_calls, notes_block

FROM_ME = {"from": "me"}


def alone(ctx):
    """``ctx`` for a turn Domo acts in on his own, as the core makes one."""
    return replace(ctx, on_his_own=True, policy=on_his_own_tables(ctx.policy))


async def leave(ctx, recipient: str, text: str = "Hi", **more):
    return await TOOLS["note_leave"].execute({"recipient": recipient, "text": text, **more}, ctx)


# --- who a note is from ----------------------------------------------------------------------


async def test_on_his_own_a_note_is_domo_s_and_audited_as_his(
    add_member, session_factory, live_clock, stored_notes, last_audit_entry
):
    owner = await add_member("Alex", "owner")
    dana = await add_member("Dana", "adult")
    outcome = await leave(alone(call_context(owner, session_factory, live_clock)), "Dana")
    assert outcome.status == "done"
    assert outcome.card == {"key": "action.card.done.note.leave.his", "params": {"name": "Dana"}}
    [note] = await stored_notes()
    # Nobody's: neither the owner's, whose conversation it is, nor anyone else's.
    assert (note.by_domo, note.author_id, note.recipient_id) == (True, None, dana.id)
    entry = await last_audit_entry("note.left")
    assert entry.actor == "domo"
    assert entry.details == {"author": "domo", "recipient": str(dana.id)}


async def test_in_a_person_s_turn_a_note_is_theirs(
    add_member, session_factory, live_clock, stored_notes, last_audit_entry
):
    dana = await add_member("Dana", "adult")
    sam = await add_member("Sam", "child")
    outcome = await leave(call_context(dana, session_factory, live_clock), "Sam")
    assert outcome.card == {"key": "action.card.done.note.leave", "params": {"name": "Sam"}}
    [note] = await stored_notes()
    assert (note.by_domo, note.author_id, note.recipient_id) == (False, dana.id, sam.id)
    assert (await last_audit_entry("note.left")).actor == f"person:{dana.id}"


async def test_from_me_makes_it_domo_s_and_never_keeps_the_person_s_words(
    add_member, session_factory, live_clock, stored_notes, last_audit_entry
):
    dana = await add_member("Dana", "adult")
    sam = await add_member("Sam", "child")
    said = "Tell Sam the bus is late"
    ctx = call_context(dana, session_factory, live_clock, said=said)
    outcome = await leave(ctx, "Sam", "The bus is late.", exact=said, **FROM_ME)
    assert outcome.card == {"key": "action.card.done.note.leave.his", "params": {"name": "Sam"}}
    [note] = await stored_notes()
    assert (note.by_domo, note.author_id, note.recipient_id) == (True, None, sam.id)
    # His words are what he wrote: a passage of Dana's message is never shown as his.
    assert note.exact_text is None
    assert "yours" in outcome.model_result["exact"]
    assert (await last_audit_entry("note.left")).actor == "domo"


@pytest.mark.parametrize("given", ["Dana", "you", "", "ME", None, True])
async def test_from_says_me_or_nothing(add_member, session_factory, live_clock, given, count_notes):
    dana = await add_member("Dana", "adult")
    await add_member("Sam", "child")
    with pytest.raises(InvalidArguments) as refused:
        await leave(call_context(dana, session_factory, live_clock), "Sam", **{"from": given})
    assert refused.value.code == "invalid"
    assert await count_notes() == 0


async def test_from_me_is_judged_as_the_person_and_also_as_domo(
    add_member, session_factory, live_clock
):
    # Ruling R29: the person's own judgement stays whole; Domo's is added to it.
    dana = await add_member("Dana", "adult")
    await add_member("Sam", "child")
    ctx = call_context(dana, session_factory, live_clock)
    tool = TOOLS["note_leave"]
    arguments = {"recipient": "Sam", "text": "Hi"}
    assert judged_actor_of(tool, ctx) == ctx.actor
    theirs = await tool.describe(arguments, ctx.actor, ctx)
    his_too = await tool.describe({**arguments, **FROM_ME}, ctx.actor, ctx)
    assert (theirs.also_domo, his_too.also_domo) == (False, True)
    # Stored and read back with a request that waits.
    assert DescribedAction.from_json(his_too.as_json()) == his_too
    assert "also_domo" not in theirs.as_json()
    # On his own, only his judgement: nothing is added.
    alone_ctx = alone(ctx)
    on_his_own = await tool.describe({**arguments, **FROM_ME}, domo_actor(), alone_ctx)
    assert on_his_own.also_domo is False


def test_the_stricter_of_two_decisions():
    from domo_core.policy.barrier import Allow, NeedsApproval, Refuse, stricter

    owner_rule = Allow(rule=None)
    assert stricter(Allow(), Allow()) == Allow()
    # Fix round 2 (N1): when both sides carry a rule of the owner's, the more demanding is asked,
    # whichever side it is on.
    assert stricter(Allow(rule="yes"), Allow(rule="proof")).rule == "proof"
    assert stricter(Allow(rule="proof"), Allow(rule="yes")).rule == "proof"
    assert stricter(Allow(rule="explicit_yes"), NeedsApproval("adult", rule="proof")).rule == (
        "proof"
    )
    assert stricter(NeedsApproval("owner", rule="proof"), Allow(rule="yes")).rule == "proof"
    assert stricter(Refuse("not_allowed"), Allow()) == Refuse("not_allowed", side="person")
    assert stricter(Allow(), Refuse("limit", limit="x")) == Refuse("limit", limit="x", side="domo")
    assert stricter(NeedsApproval("adult"), owner_rule) == NeedsApproval("adult")
    assert stricter(Allow(), NeedsApproval("owner")) == NeedsApproval("owner")
    assert stricter(NeedsApproval("adult"), NeedsApproval("owner")) == NeedsApproval("owner")
    assert stricter(NeedsApproval("owner"), NeedsApproval("adult")) == NeedsApproval("owner")
    # Left to the judge only when every side that asks leaves the same agreement to it.
    first_adult = NeedsApproval("adult", judge_first=True)
    first_owner = NeedsApproval("owner", "Q?", judge_first=True)
    assert stricter(first_adult, Allow()).judge_first
    assert stricter(first_owner, first_owner).judge_first
    assert not stricter(first_adult, NeedsApproval("adult")).judge_first
    assert not stricter(first_adult, first_owner).judge_first


def _chain_with_his_note(recipient: str) -> dict[str, object]:
    """``do_in_order``: the person's own note first, then one of his, both to ``recipient``."""
    return {
        "steps": [
            {"tool": "note_leave", "arguments": {"recipient": recipient, "text": "mine"}},
            {"tool": "note_leave", "arguments": {"recipient": recipient, "text": "his", **FROM_ME}},
        ]
    }


async def test_from_me_as_a_step_of_a_person_s_chain_is_judged_both_ways_and_left(
    owner_chat: ChatPilot, adult_chat: ChatPilot, session_factory, stored_notes
):
    # Final review I2, ruling R34: judged when the chain is planned, as at a direct call.
    owner = owner_chat.person.display_name
    reply = await adult_chat.say_with_tool("do_in_order", _chain_with_his_note(owner), judge="do")
    assert reply.actions == [{"tool": "do_in_order", "status": "done"}]
    notes = await stored_notes()
    assert sorted((n.text, n.by_domo, n.author_id) for n in notes) == [
        ("his", True, None),
        ("mine", False, adult_chat.person.id),
    ]
    decided = await decisions_on(session_factory, "note.leave")
    assert sorted((entry.actor, entry.details["decision"]) for entry in decided) == [
        ("domo", "allow"),
        (f"person:{adult_chat.person.id}", "allow"),
        (f"person:{adult_chat.person.id}", "allow"),
    ]


async def test_from_me_in_a_chain_refused_on_his_side_stops_the_chain_before_any_step(
    owner_chat: ChatPilot, adult_chat: ChatPilot, session_factory, grant, count_notes
):
    # Never accepted at plan time then refused at run, and no allow left for a step not run.
    await grant("note.leave", for_domo=True, access="deny")
    reply = await adult_chat.say_with_tool(
        "do_in_order", _chain_with_his_note(owner_chat.person.display_name), judge="do"
    )
    [action] = reply.actions
    assert (action["tool"], action["status"]) == ("do_in_order", "refused")
    assert await count_notes() == 0
    decided = await decisions_on(session_factory, "note.leave")
    assert [entry for entry in decided if entry.details["decision"] == "allow"] == []


async def test_from_me_after_a_wait_is_judged_on_his_side_when_the_chain_is_planned(
    owner_chat: ChatPilot, session_factory, grant, count_notes
):
    # Re-review of the final fix, point 1: a step checked in its turn (it names the newcomer)
    # is judged for Domo too when it is his note: refused at plan, the invitation never made.
    await grant("note.leave", for_domo=True, access="deny")
    steps = {
        "steps": [
            {"tool": "household_invite", "arguments": {"display_name": "Tom", "role": "adult"}},
            {
                "tool": "note_leave",
                "arguments": {"recipient": "Tom", "text": "Welcome.", **FROM_ME},
                "after_arrival_of": "Tom",
            },
        ]
    }
    reply = await owner_chat.say_with_tool("do_in_order", steps, judge="do")
    [action] = reply.actions
    assert (action["tool"], action["status"]) == ("do_in_order", "refused")
    async with session_factory() as reader:
        invited = (await reader.scalars(select(Invitation))).all()
    assert invited == [] and await count_notes() == 0


async def test_a_note_names_exactly_one_author(add_member, session_factory):
    dana = await add_member("Dana", "adult")
    for author_id, by_domo in ((dana.id, True), (None, False)):
        async with session_factory() as writer:
            writer.add(Note(author_id=author_id, by_domo=by_domo, recipient_id=dana.id, text="x"))
            with pytest.raises(IntegrityError):
                await writer.commit()


# --- whoever reads it reads that it is his (Review Focus 5) ----------------------------------


async def test_a_note_of_his_is_told_to_its_reader_as_his(add_member, session_factory, live_clock):
    owner = await add_member("Alex", "owner")
    dana = await add_member("Dana", "adult")
    await leave(alone(call_context(owner, session_factory, live_clock)), "Dana", "Bins tonight.")
    await leave(call_context(owner, session_factory, live_clock), "Dana", "From Alex.")
    async with session_factory() as session:
        his, alexs = sorted(await notes_to_tell(session, dana.id), key=lambda n: n.yours is None)
    assert (his.author, his.yours, his.words) == (DOMO_NAME, True, "Bins tonight.")
    assert (alexs.author, alexs.yours) == ("Alex", None)
    # The name Dana calls him by, where she gave him one.
    async with session_factory() as session:
        await session.execute(update(type(dana)).values(calls_domo="Brenda"))
        await session.commit()
    async with session_factory() as session:
        his, alexs = sorted(await notes_to_tell(session, dana.id), key=lambda n: n.yours is None)
    assert (his.author, alexs.author) == ("Brenda", "Alex")


def test_the_context_says_a_note_marked_yours_is_his_own():
    assert NoteToTell("n", "Domo", "2026-11-21T08:00:00+00:00", "Hi", yours=True).yours
    from domo_core.prompts import _notes_told_line

    line = " ".join(_notes_told_line(("note_told", "note_show")).split())
    assert "yours" in line and "you left yourself" in line


async def test_told_and_shown_a_note_of_his_is_his(add_member, session_factory, live_clock):
    owner = await add_member("Alex", "owner")
    dana = await add_member("Dana", "adult")
    await leave(alone(call_context(owner, session_factory, live_clock)), "Dana", "Bins tonight.")
    async with session_factory() as session:
        [note] = await notes_to_tell(session, dana.id)
    ctx = call_context(dana, session_factory, live_clock)
    shown = await TOOLS["note_show"].execute({"note": note.id}, ctx)
    assert shown.card["key"] == "action.card.done.note.show.his"
    assert shown.card["params"] == {"words": "Bins tonight."}
    assert (shown.model_result["from"], shown.model_result["yours"]) == (DOMO_NAME, True)
    told = await TOOLS["note_told"].execute({"note": note.id}, ctx)
    assert told.card["key"] == "action.card.done.note.told.his"
    assert told.card["params"] == {}
    assert (told.model_result["from"], told.model_result["yours"]) == (DOMO_NAME, True)


async def test_a_person_s_note_is_never_shown_as_his(add_member, session_factory, live_clock):
    alex = await add_member("Alex", "owner")
    dana = await add_member("Dana", "adult")
    await leave(call_context(alex, session_factory, live_clock), "Dana", "From Alex.")
    async with session_factory() as session:
        [note] = await notes_to_tell(session, dana.id)
    ctx = call_context(dana, session_factory, live_clock)
    told = await TOOLS["note_told"].execute({"note": note.id}, ctx)
    assert told.card["key"] == "action.card.done.note.told"
    assert told.model_result == {"told": True, "from": "Alex"}


# --- his own notes, found, taken back and kept on his own; never a person's -------------------


async def test_on_his_own_he_finds_takes_back_and_keeps_his_notes_alone(
    add_member, session_factory, live_clock, last_audit_entry
):
    owner = await add_member("Alex", "owner")
    guest = await add_member("Robin", "guest")
    mine = alone(call_context(owner, session_factory, live_clock))
    await leave(mine, "Robin", "His.")
    await leave(call_context(owner, session_factory, live_clock), "Robin", "Alex's.")
    listed = await TOOLS["note_list_sent"].execute({}, mine)
    assert listed.card["key"] == "action.card.done.note.list_sent.his"
    [his] = listed.model_result["notes"]
    assert his["text"] == "His."
    # The owner's list holds only the owner's note.
    [owners] = (
        await TOOLS["note_list_sent"].execute({}, call_context(owner, session_factory, live_clock))
    ).model_result["notes"]
    assert owners["text"] == "Alex's."
    until = (live_clock() + timedelta(days=40)).date()
    kept = await TOOLS["note_keep"].execute({"note": his["id"], "until": until.isoformat()}, mine)
    assert kept.status == "done"
    assert (await last_audit_entry("note.kept")).actor == "domo"
    # Described for him, his own note; the owner's is none of his.
    described = await TOOLS["note_withdraw"].describe({"note": his["id"]}, domo_actor(), mine)
    assert described.facts["recipient"] == f"person:{guest.id}"
    with pytest.raises(InvalidArguments) as refused:
        await TOOLS["note_withdraw"].execute({"note": owners["id"]}, mine)
    assert refused.value.code == "not_found"
    withdrawn = await TOOLS["note_withdraw"].execute({"note": his["id"]}, mine)
    assert withdrawn.model_result["withdrawn"] is True
    assert (await last_audit_entry("note.withdrawn")).actor == "domo"


async def test_a_person_never_reaches_a_note_of_his(add_member, session_factory, live_clock):
    dana = await add_member("Dana", "adult")
    await add_member("Robin", "guest")
    theirs = call_context(dana, session_factory, live_clock)
    # Left in Dana's turn, in his own name.
    await leave(theirs, "Robin", "His.", **FROM_ME)
    async with session_factory() as session:
        [note_id] = (await session.scalars(select(Note.id))).all()
    assert (await TOOLS["note_list_sent"].execute({}, theirs)).model_result["notes"] == []
    until = (live_clock() + timedelta(days=40)).date().isoformat()
    for name, arguments in (
        ("note_withdraw", {"note": str(note_id)}),
        ("note_keep", {"note": str(note_id), "until": until}),
    ):
        with pytest.raises(InvalidArguments) as refused:
            await TOOLS[name].execute(arguments, theirs)
        assert refused.value.code == "not_found", name


def test_his_own_notes_are_his_on_his_own_and_the_person_s_received_ones_are_not():
    for action_type in ("note.leave", "note.list_sent", "note.withdraw", "note.keep"):
        assert action_type not in NOT_ON_HIS_OWN, action_type
        policy = on_his_own_tables(DEFAULT_POLICY)
        assert NO_RIGHTS.resolve(domo_actor(), action_type, policy).allowed, action_type
    # What was left for the person of the conversation stays theirs.
    assert {"note.told", "note.show"} <= NOT_ON_HIS_OWN


def test_note_leave_says_when_a_note_is_his_and_when_it_is_the_person_s():
    leave_tool = TOOLS["note_leave"]
    said = " ".join(leave_tool.definition.description.split())
    assert "you leave no note" not in said
    assert '"from": "me"' in said
    assert leave_tool.definition.parameters["properties"]["from"]["enum"] == ["me"]


# --- in turns ----------------------------------------------------------------------------------


async def test_on_his_own_he_leaves_a_note_told_to_its_reader_as_his(
    chat_server: ChatServer, owner_chat: ChatPilot, adult_chat: ChatPilot, session_factory
):
    tool_id = await add_tool(session_factory, "split")
    chat_server.sandbox = FakeSandbox(program=wakes_once())
    call = ToolCall(
        "n1", "note_leave", json.dumps({"recipient": adult_chat.person.display_name, "text": "Hi"})
    )
    owner_chat.use_model(FakeChatModel(tool_script=[call, "Done."], judge="do"))
    await woken_for_the_house(chat_server, tool_id)
    [decided] = await decisions_on(session_factory, "note.leave")
    assert (decided.actor, decided.details["decision"]) == ("domo", "allow")
    async with session_factory() as reader:
        [note] = (await reader.scalars(select(Note))).all()
    assert (note.by_domo, note.author_id) == (True, None)
    # The adult's Domo reads it as his own.
    adult_chat.use_model(FakeChatModel(["Hello."]))
    await adult_chat.say("hi")
    [told] = notes_block(main_calls(chat_server.calls)[-1].system)
    assert (told["author"], told["yours"], told["words"]) == (DOMO_NAME, True, "Hi")


async def from_me(pilot: ChatPilot, recipient: str, *, judge: str = "do"):
    return await pilot.say_with_tool(
        "note_leave", {"recipient": recipient, "text": "Hi", **FROM_ME}, judge=judge
    )


async def test_a_deny_of_his_stops_both_of_his_notes_and_no_person_s(
    chat_server: ChatServer,
    owner_chat: ChatPilot,
    adult_chat: ChatPilot,
    session_factory,
    grant,
    count_notes,
):
    await grant("note.leave", for_domo=True, access="deny")
    tool_id = await add_tool(session_factory, "split")
    chat_server.sandbox = FakeSandbox(program=wakes_once())
    call = ToolCall(
        "n1", "note_leave", json.dumps({"recipient": adult_chat.person.display_name, "text": "Hi"})
    )
    owner_chat.use_model(FakeChatModel(tool_script=[call, "Done."], judge="do"))
    await woken_for_the_house(chat_server, tool_id)
    [on_his_own] = await decisions_on(session_factory, "note.leave")
    assert (on_his_own.actor, on_his_own.details["decision"]) == ("domo", "refuse")
    # In the adult's turn, his own word to the owner is stopped too, though the adult may: the
    # refusal says it is his rights that refused.
    owner = owner_chat.person.display_name
    reply = await from_me(adult_chat, owner)
    assert reply.actions == [{"tool": "note_leave", "status": "refused", "reason": "not_allowed"}]
    assert reply.model_results[-1]["whose_rights"] == "yours"
    assert await count_notes() == 0
    # The adult's own note is theirs to leave: his right is not theirs.
    reply = await adult_chat.say_with_tool(
        "note_leave", {"recipient": owner, "text": "Hi"}, judge="do"
    )
    assert reply.actions == [{"tool": "note_leave", "status": "done"}]
    assert await count_notes() == 1


async def test_from_me_never_widens_what_the_person_may_do(
    owner_chat: ChatPilot, adult_chat: ChatPilot, grant, count_notes
):
    # Ruling R29: a person denied notes gets none out through Domo's name.
    await grant("note.leave", person=adult_chat.person, access="deny")
    reply = await from_me(adult_chat, owner_chat.person.display_name)
    assert reply.actions == [{"tool": "note_leave", "status": "refused", "reason": "not_allowed"}]
    assert reply.model_results[-1]["whose_rights"] == "the person's"
    assert await count_notes() == 0


async def test_from_me_keeps_the_person_s_limit_on_whom_they_write_to(
    owner_chat: ChatPilot, adult_chat: ChatPilot, child_member, grant, count_notes
):
    await grant(
        "note.leave",
        person=adult_chat.person,
        limit='action.facts.recipient_role == "owner" ? "allow" : "refuse"',
    )
    refused = await from_me(adult_chat, child_member.person.display_name)
    assert refused.actions[0]["status"] == "refused"
    assert refused.model_results[-1]["whose_rights"] == "the person's"
    assert await count_notes() == 0
    allowed = await from_me(adult_chat, owner_chat.person.display_name)
    assert allowed.actions == [{"tool": "note_leave", "status": "done"}]
    assert await count_notes() == 1


async def test_from_me_waits_for_the_yes_the_person_s_notes_need(
    owner_chat: ChatPilot, adult_chat: ChatPilot, child_member, grant, count_notes
):
    await grant("note.leave", person=adult_chat.person, approver="owner")
    reply = await from_me(adult_chat, child_member.person.display_name)
    assert reply.actions[0]["status"] == "waiting"
    assert await count_notes() == 0


async def test_from_me_counts_in_the_person_s_daily_limit(
    owner_chat: ChatPilot, adult_chat: ChatPilot, grant, count_notes
):
    await grant(
        "note.leave", person=adult_chat.person, limit='history.day < 1 ? "allow" : "refuse"'
    )
    owner = owner_chat.person.display_name
    first = await from_me(adult_chat, owner)
    assert first.actions == [{"tool": "note_leave", "status": "done"}]
    second = await from_me(adult_chat, owner)
    assert second.actions[0]["status"] == "refused"
    assert second.model_results[-1]["whose_rights"] == "the person's"
    assert await count_notes() == 1


async def test_a_guest_s_turn_leaves_no_note_in_his_name(
    owner_chat: ChatPilot, guest_chat: ChatPilot, count_notes
):
    reply = await from_me(guest_chat, owner_chat.person.display_name)
    assert reply.actions[0]["status"] == "refused"
    assert await count_notes() == 0


async def test_a_right_of_his_on_notes_now_does_what_it_says(
    chat_server: ChatServer, owner_chat: ChatPilot, adult_chat: ChatPilot, session_factory, grant
):
    # Ledger line n4: the owner's allow of note.leave for Domo, which the rights writer shows
    # as letting him judge, now lets him leave one.
    await grant("note.leave", for_domo=True, access="allow")
    tool_id = await add_tool(session_factory, "split")
    chat_server.sandbox = FakeSandbox(program=wakes_once())
    call = ToolCall(
        "n1", "note_leave", json.dumps({"recipient": adult_chat.person.display_name, "text": "Hi"})
    )
    owner_chat.use_model(FakeChatModel(tool_script=[call, "Done."], judge="do"))
    await woken_for_the_house(chat_server, tool_id)
    async with session_factory() as reader:
        [note] = (await reader.scalars(select(Note))).all()
    assert note.by_domo
    [left] = await audit_of(session_factory, "note.left")
    assert left.actor == "domo"


async def test_allowed_on_both_sides_the_note_is_his_and_names_whose_turn_it_was(
    owner_chat: ChatPilot, adult_chat: ChatPilot, session_factory, last_audit_entry
):
    owner = owner_chat.person.display_name
    reply = await from_me(adult_chat, owner)
    assert reply.actions == [{"tool": "note_leave", "status": "done"}]
    async with session_factory() as reader:
        [note] = (await reader.scalars(select(Note))).all()
    assert (note.by_domo, note.author_id) == (True, None)
    # Judged for both: the person's decision, and his.
    decided = await decisions_on(session_factory, "note.leave")
    assert sorted((entry.actor, entry.details["decision"]) for entry in decided) == [
        ("domo", "allow"),
        (f"person:{adult_chat.person.id}", "allow"),
    ]
    left = await last_audit_entry("note.left")
    assert left.actor == "domo"
    assert left.details == {
        "author": "domo",
        "recipient": str(owner_chat.person.id),
        "turn_of": f"person:{adult_chat.person.id}",
    }
    # It counts in the person's history (their limits read it), but it is not one they left.
    async with session_factory() as reader:
        theirs = await person_history(
            reader, adult_chat.person.id, "note.leave", owner_chat.server.clock()
        )
    assert theirs.ever == 1
    listed = await adult_chat.say_with_tool("note_list_sent", {})
    assert listed.model_results[-1]["notes"] == []


class ChangesWhileJudging(FakeChatModel):
    """A model that, as the yes-judge is asked, first runs ``change`` once."""

    def __init__(self, change, **kwargs):
        super().__init__(**kwargs)
        self._change = change

    async def stream(self, *, system, **kwargs):
        if is_judge_call(system) and self._change is not None:
            change, self._change = self._change, None
            await change()
        async for item in super().stream(system=system, **kwargs):
            yield item


async def test_a_role_changed_while_the_judge_answers_is_the_one_judged(
    owner_chat: ChatPilot, adult_chat: ChatPilot, session_factory, grant, count_notes
):
    # Fix round 1 (I1): the barrier's second look reads the person's role of now.
    await grant("note.leave", role="child", access="deny")
    person = adult_chat.person

    async def made_a_child():
        async with session_factory() as writer:
            await writer.execute(update(Person).where(Person.id == person.id).values(role="child"))
            await writer.commit()

    call = ToolCall(
        "n1", "note_leave", json.dumps({"recipient": owner_chat.person.display_name, "text": "Hi"})
    )
    adult_chat.use_model(ChangesWhileJudging(made_a_child, tool_script=[call, "Done."], judge="do"))
    reply = await adult_chat.say("Tell the owner hi.")
    assert reply.actions == [{"tool": "note_leave", "status": "refused", "reason": "not_allowed"}]
    assert await count_notes() == 0


async def decide(chat_server, session_factory, tool_context, c, who, *, approve: bool):
    """``who`` says yes (or no, ``approve`` false) to the request ``c``, by the button."""
    deps = made_tool_deps(app_turn_deps(chat_server.app))
    async with session_factory() as reader:
        decider = await reader.get(Person, who.id)
    outcome, _ = await decide_confirmation(
        session_factory=session_factory,
        confirmation_id=c.id,
        decider=decider,
        approve=approve,
        via="button",
        reply_message_id=None,
        ctx_factory=deciders(tool_context, deps, session_factory),
        registry=TOOLS,
        policy=await policy_of(session_factory),
        now=chat_server.clock(),
    )
    return outcome


async def waiting_requests(session_factory) -> list[Confirmation]:
    async with session_factory() as reader:
        found = await reader.scalars(
            select(Confirmation)
            .where(Confirmation.status == "pending")
            .order_by(Confirmation.created_at)
        )
        return list(found.all())


@pytest.mark.parametrize("approve", [True, False], ids=["approved", "refused"])
async def test_his_note_that_went_through_a_yes_counts_in_his_daily_limit_once_left(
    chat_server: ChatServer,
    owner_chat: ChatPilot,
    adult_chat: ChatPilot,
    child_member,
    session_factory,
    grant,
    tool_context,
    count_notes,
    approve,
):
    # Fix round 2 (N2): one note of his a day; the adult's notes need the owner's yes.
    await grant("note.leave", for_domo=True, limit='history.day < 1 ? "allow" : "refuse"')
    await grant("note.leave", person=adult_chat.person, approver="owner")
    sam = child_member.person.display_name
    first = await from_me(adult_chat, sam)
    assert first.actions[0]["status"] == "waiting"
    [asked] = await waiting_requests(session_factory)
    await decide(
        chat_server, session_factory, tool_context, asked, owner_chat.person, approve=approve
    )
    assert await count_notes() == (1 if approve else 0)
    second = await from_me(adult_chat, sam)
    if approve:
        # The note he left after the yes is his one of the day.
        assert second.actions[0]["status"] == "refused"
        assert second.model_results[-1]["whose_rights"] == "yours"
    else:
        # A note refused at the yes was never his: the next one may still be.
        assert second.actions[0]["status"] == "waiting"


# --- a note of his keeps where its words came from (final fix wave B2) -------------------------


async def test_his_note_after_a_programs_text_is_from_an_unknown_source(
    add_member, session_factory, live_clock, stored_notes
):
    owner = await add_member("Alex", "owner")
    await add_member("Dana", "adult")
    plain = alone(call_context(owner, session_factory, live_clock))
    await leave(plain, "Dana", "First")
    await leave(replace(plain, tool_woke=True), "Dana", "Second")
    await leave(replace(plain, outside=OutsideText(raised=True)), "Dana", "Third")
    marks = {note.text: note.from_unknown for note in await stored_notes()}
    assert marks == {"First": False, "Second": True, "Third": True}


async def test_a_persons_note_never_is_from_an_unknown_source(
    add_member, session_factory, live_clock, stored_notes
):
    dana = await add_member("Dana", "adult")
    await add_member("Sam", "child")
    await leave(replace(call_context(dana, session_factory, live_clock), tool_woke=True), "Sam")
    [note] = await stored_notes()
    assert (note.by_domo, note.from_unknown) == (False, False)


async def test_a_person_cannot_be_written_as_author_of_an_unknown_source(
    add_member, session_factory
):
    dana = await add_member("Dana", "adult")
    sam = await add_member("Sam", "child")
    async with session_factory() as writer:
        writer.add(Note(author_id=dana.id, recipient_id=sam.id, text="x", from_unknown=True))
        with pytest.raises(IntegrityError):
            await writer.commit()
