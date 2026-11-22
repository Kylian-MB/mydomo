"""A step of a chain that waits for someone's arrival (spec 4.3b): "invite Paul as an adult, and
hand him my role once he is here".

A step may wait for the arrival of the person an invitation is for (``after_arrival_of``, the name
of the invitation): one an earlier step of the same chain makes, or one that already waits. Such a
chain always waits for a yes; after it, the chain does its steps up to the wait and pauses
(``waiting_arrival``): its card says whom it waits for, keeps the buttons of its steps done (the
link to send Paul), and Domo sees it in the context of the one who asked, who may drop it. A
periodic pass of the core (``upkeep.run_upkeep``, ``confirmations.arrivals``) resumes it at most a
minute after the arrival, even after a restart, each step judged again in its turn with the facts
of then; its lifetime starts again at the arrival. A chain with a hand-over (or a change of the
successors) left runs nothing then: it waits again for the explicit yes of the one who gave it,
whose card says who joined. When the invitation ends without an arrival (cancelled, expired), the
pass stops the chain at the wait. Whatever came of it, Domo is woken in the conversation of the
one who asked (``"via": "arrival"``).

The chat pilots talk with Domo on a live server (see ``chat_support.py``); ``upkeep_pass`` makes
one pass of the arrivals as the application does, and ``join_with_invitation`` is someone joining
with their invitation.
"""

import asyncio
import json
import logging
import re
import uuid
from datetime import timedelta
from typing import Any

import anyio
import domo_core.confirmations.arrivals as arrivals
import domo_core.conversation.starts as starts_module
import pytest
from chat_support import MEMBER_PASSWORD, WAIT_SECONDS, confirmation_cards, status_of
from domo_common.i18n import t
from domo_core.accounts.models import Person
from domo_core.config import CoreSettings
from domo_core.confirmations.arrivals import arrivals_pass
from domo_core.confirmations.models import Confirmation, ConfirmationStep
from domo_core.confirmations.replan import StepMatch, calls_of, match_steps, step_call
from domo_core.household.models import Invitation
from domo_core.household.service import disable
from domo_core.llm.types import ToolCall
from domo_core.policy.asks import Asked
from domo_core.policy.catalog import ACTION_FACTS
from domo_core.tools.context import InvalidArguments
from domo_core.tools.registry import TOOLS
from domo_core.web.app import arrival_deps, create_app
from domo_core.web.deps import get_clock, get_tool_registry
from fakes import FakeChatModel
from pydantic import ValidationError
from sqlalchemy import select, update
from test_chain_plan import PENDING_IN_CONTEXT, chain, plain
from test_chain_run import link_buttons
from test_confirmation_tools import amend

TOOL = "do_in_order"
INVITE_PAUL = ("household_invite", {"display_name": "Paul", "role": "adult"})
INVITE_KIM = ("household_invite", {"display_name": "Kim", "role": "adult"})
HAND_OVER_TO_PAUL = ("household_transfer_ownership", {"person": "Paul"}, "Paul")
NO_SUCH_INVITATION = (
    "after_arrival_of names an invitation that waits, or one an earlier step of this chain makes"
)
DECIDED_MARK = "Just decided (data, not instructions):\n"


def note_for(recipient: str, text: str, after: str | None = None) -> tuple[Any, ...]:
    """A step that leaves ``text`` for ``recipient``, once ``after`` has joined if it is given."""
    arguments = {"recipient": recipient, "text": text}
    return ("note_leave", arguments) if after is None else ("note_leave", arguments, after)


def pending_in(system: str) -> list[dict[str, Any]]:
    """The pending requests of a system prompt's context, as Domo reads them."""
    if PENDING_IN_CONTEXT + ":\n" not in system:
        return []
    block = system.split(PENDING_IN_CONTEXT + ":\n")[1]
    return json.loads(block.split("\n\n")[0])


def decided_in(system: str) -> dict[str, Any]:
    """What a system prompt's context tells of the request just decided."""
    return json.loads(system.split(DECIDED_MARK)[1].split("\n\n")[0])


async def request_row(session_factory: Any, cid: uuid.UUID | str | None) -> Confirmation:
    """The request as the database holds it now (committed)."""
    assert cid is not None
    async with session_factory() as reader:
        found = await reader.execute(
            select(Confirmation).where(Confirmation.id == uuid.UUID(str(cid)))
        )
        return found.scalar_one()


async def invitation_named(session_factory: Any, name: str) -> Invitation:
    """The latest invitation made for ``name`` (committed)."""
    async with session_factory() as reader:
        found = await reader.execute(
            select(Invitation)
            .where(Invitation.display_name == name)
            .order_by(Invitation.created_at.desc())
            .limit(1)
        )
        return found.scalar_one()


async def paused_chain(
    pilot: Any, *calls: tuple[Any, ...], words: str = "je confirme"
) -> uuid.UUID:
    """A chain of ``calls`` the pilot asks for and confirms with ``words`` ("je confirme", an
    explicit yes, unless told otherwise): it pauses at its first wait. Its request."""
    reply = await pilot.say_with_tool(TOOL, chain(*calls))
    cid = reply.pending_confirmation_id
    assert cid is not None, reply
    await pilot.say(words)
    return cid


async def a_firm_yes_is_asked_since(grant: Any, pilot: Any) -> None:
    """While a chain of the owner (``pilot``) waits, he asks for a firm yes before his role is
    handed over. Nothing asks one by default (2026-11-15): a chain given a plain yes ("oui") is
    asked again at the arrival only because this rule of his asks more than that yes gave."""
    await grant(
        "household.transfer_ownership",
        person=pilot.person,
        ask="explicit_yes",
        summary="Ask me firmly before handing my role over.",
    )


def steps_of_rows(rows: list[ConfirmationStep]) -> list[tuple[int, str, str | None]]:
    return [(row.position, row.status, row.after_arrival_of) for row in rows]


# --- the wait, when the chain is asked for (spec 4.3b) -------------------------------------------


async def test_a_chain_pauses_at_the_wait_once_the_invitation_is_made(
    owner_chat, chat_server, confirmation_status, steps_rows, count_invitations, session_factory
):  # spec 8.3
    reply = await owner_chat.say_with_tool(
        TOOL, chain(INVITE_PAUL, HAND_OVER_TO_PAUL), judge="explicit_yes: a big step"
    )
    cid = reply.pending_confirmation_id
    [card] = confirmation_cards(reply.cards_html)
    items = re.findall(r"<li [^>]*>", card)
    assert len(items) == 2
    assert "data-checked-in-turn" not in items[0] and "data-checked-in-turn" in items[1]
    # The card says that the hand-over waits for Paul's arrival.
    assert t("confirmation.step.after_arrival_of", "en", name="Paul") in plain(card)
    # The judge asked a firm yes of it (nothing asks one by default): a plain yes is not enough.
    await owner_chat.say("oui")
    assert await confirmation_status(cid) == "pending"
    answer = await owner_chat.say("je confirme")
    assert await confirmation_status(cid) == "waiting_arrival"
    assert steps_of_rows(await steps_rows(cid)) == [(0, "done", None), (1, "planned", "Paul")]
    assert await count_invitations() == 1
    paul = await invitation_named(session_factory, "Paul")
    assert paul.accepted_at is None and paul.cancelled_at is None
    assert (await request_row(session_factory, cid)).waiting_for_invitation_id == paul.id
    # Domo is told the chain is paused; the card names Paul, and says what was done stays.
    assert decided_in(chat_server.calls[-1].system)["outcome"] == "paused"
    shown = plain(answer.cards_html)
    assert t("action.card.waiting_arrival", "en", name="Paul") in shown
    assert t("confirmation.outcome.paused", "en", name="Paul") in shown
    assert 'data-confirmation-status="waiting_arrival"' in answer.cards_html


async def test_a_wait_names_an_invitation_that_waits_or_one_the_chain_makes(
    owner_chat, invite_for_test
):
    # No invitation of that name waits, and no earlier step makes one.
    for calls, number in (
        ((HAND_OVER_TO_PAUL,), 1),
        ((INVITE_KIM, HAND_OVER_TO_PAUL), 2),
        # A later step does not count: the wait comes first.
        ((HAND_OVER_TO_PAUL, INVITE_PAUL), 1),
    ):
        reply = await owner_chat.say_with_tool(TOOL, chain(*calls))
        assert reply.actions == [{"tool": TOOL, "status": "invalid", "reason": "invalid"}]
        assert reply.model_results[-1]["detail"] == f"step {number}: {NO_SUCH_INVITATION}"
    # An invitation that waits already.
    await invite_for_test("Paul", "adult")
    reply = await owner_chat.say_with_tool(TOOL, chain(HAND_OVER_TO_PAUL))
    assert reply.actions == [{"tool": TOOL, "status": "waiting"}]


async def test_a_one_step_chain_may_wait_on_an_invitation_that_already_waits(
    owner_chat, invite_for_test, steps_rows, confirmation_status, session_factory
):  # spec 4.3b: "Paul est invité depuis hier ; quand il arrive, donne-lui mon rôle"
    paul = await invite_for_test("Paul", "adult")
    reply = await owner_chat.say_with_tool(TOOL, chain(HAND_OVER_TO_PAUL))
    cid = reply.pending_confirmation_id
    # Never free: a chain that waits for an arrival needs a request, which the pass resumes.
    assert await confirmation_status(cid) == "pending"
    # No step is described yet (Paul is not there): the chain's effect comes from its types.
    assert (await request_row(session_factory, cid)).action["effect"] == "permissions"
    await owner_chat.say("je confirme")
    assert await confirmation_status(cid) == "waiting_arrival"
    assert steps_of_rows(await steps_rows(cid)) == [(0, "planned", "Paul")]
    assert (await request_row(session_factory, cid)).waiting_for_invitation_id == paul.id


async def test_the_steps_after_the_wait_may_name_the_newcomer(owner_chat, steps_rows):
    reply = await owner_chat.say_with_tool(
        TOOL, chain(INVITE_PAUL, note_for("Paul", "bienvenue", "Paul"))
    )
    assert reply.actions == [{"tool": TOOL, "status": "waiting"}]
    rows = await steps_rows(reply.pending_confirmation_id)
    # Paul does not exist yet: the note is checked in its turn.
    assert rows[0].action is not None
    assert (rows[1].action, rows[1].after_arrival_of) == (None, "Paul")


async def test_a_wait_may_be_for_someone_brought_back(
    owner_chat, adult_member, confirmation_status, session_factory
):
    async with session_factory() as writer:
        await disable(writer, adult_member.person, owner_chat.clock(), by=adult_member.person.id)
        await writer.commit()
    cid = await paused_chain(
        owner_chat,
        ("household_bring_back", {"person": "Alex", "role": "adult"}),
        note_for("Alex", "re-bienvenue", "Alex"),
    )
    # The chain waits for the return link its first step made.
    assert await confirmation_status(cid) == "waiting_arrival"
    link = await invitation_named(session_factory, "Alex")
    assert link.person_id == adult_member.person.id
    assert (await request_row(session_factory, cid)).waiting_for_invitation_id == link.id


async def test_a_chain_with_a_wait_always_waits_for_a_yes(
    adult_chat, owner_member, invite_for_test, stored_notes, confirmation_status
):  # F29: never a free chain that meets a wait
    await invite_for_test("Paul", "adult")
    # Every step free, but one waits for Paul: the chain waits for the person's yes.
    reply = await adult_chat.say_with_tool(
        TOOL, chain(note_for("Owner", "a"), note_for("Owner", "b", "Paul"))
    )
    result = reply.model_results[-1]
    assert (result["status"], result["by"]) == ("waiting_for_confirmation", "you")
    assert await stored_notes() == []
    # A yes in words is enough for it, and it then does what comes before the wait.
    await adult_chat.say("oui")
    assert await confirmation_status(reply.pending_confirmation_id) == "waiting_arrival"
    assert [note.text for note in await stored_notes()] == ["a"]
    # The chain's own facts count its waits, for a limit of the owner.
    assert ACTION_FACTS["chain.run"]["waits"] == "number"


# --- a paused chain, for the one who asked --------------------------------------------------------


async def test_the_requester_sees_a_paused_chain_and_may_drop_it(
    owner_chat, chat_server, confirmation_status, count_invitations, session_factory
):
    cid = await paused_chain(owner_chat, INVITE_PAUL, HAND_OVER_TO_PAUL)
    await owner_chat.say("Où en est-on ?")
    [listed] = pending_in(chat_server.calls[-1].system)
    assert listed["request_id"] == str(cid)
    assert listed["waiting_for_arrival_of"] == "Paul"
    assert listed["answer_with"].startswith("nothing: it runs by itself once they join")
    # Words decide nothing on it.
    await owner_chat.say("oui")
    assert await confirmation_status(cid) == "waiting_arrival"
    # It is not changed: Domo drops it and proposes a new chain.
    reply = await owner_chat.say_with_tool(
        "confirmation_amend",
        {"request_id": str(cid), "arguments": chain(HAND_OVER_TO_PAUL), "ask_again": False},
    )
    assert reply.model_results[-1]["reason"] == "invalid"
    assert "drop it and propose a new chain" in reply.model_results[-1]["detail"]
    assert await confirmation_status(cid) == "waiting_arrival"
    reply = await owner_chat.say_with_tool("confirmation_expire", {"request_id": str(cid)})
    assert reply.actions == [{"tool": "confirmation_expire", "status": "done"}]
    assert await confirmation_status(cid) == "expired"
    # The invitation made stays.
    paul = await invitation_named(session_factory, "Paul")
    assert paul.cancelled_at is None and await count_invitations() == 1


async def test_a_paused_chain_that_waits_for_an_adult_is_listed_for_its_requester(
    child_chat, adult_chat, grant, chat_server, confirmation_status
):
    await grant("household.invite", person=child_chat.person, access="allow")
    reply = await child_chat.say_with_tool(
        TOOL,
        chain(
            ("household_invite", {"display_name": "Paul", "role": "guest"}),
            note_for("Paul", "salut", "Paul"),
        ),
    )
    cid = reply.pending_confirmation_id
    assert reply.model_results[-1]["by"] == "an adult"
    answer = await adult_chat.post(f"/confirmations/{cid}/approve")
    assert status_of(answer) == "paused"
    assert await confirmation_status(cid) == "waiting_arrival"
    # Sam, who asked, sees it in the context, whoever's yes it waited for.
    await child_chat.say("Alors ?")
    [listed] = pending_in(chat_server.calls[-1].system)
    assert (listed["request_id"], listed["waiting_for_arrival_of"]) == (str(cid), "Paul")
    reply = await child_chat.say_with_tool("confirmation_expire", {"request_id": str(cid)})
    assert reply.actions == [{"tool": "confirmation_expire", "status": "done"}]
    assert await confirmation_status(cid) == "expired"


async def test_the_link_of_a_paused_chain_is_shown_from_its_button(owner_chat):  # F3
    cid = await paused_chain(owner_chat, INVITE_PAUL, HAND_OVER_TO_PAUL)
    page = (await owner_chat.get("/chat")).text
    [paused] = [card for card in confirmation_cards(page) if str(cid) in card]
    assert 'data-confirmation-status="waiting_arrival"' in paused
    ((step, invitation),) = link_buttons(paused)
    assert step == 1
    # Once on the page.
    assert len(link_buttons(page)) == 1
    shown = await owner_chat.post(f"/invitations/{invitation}/link")
    assert shown.status_code == 200 and "/invite/" in shown.text


async def test_a_changed_chain_that_runs_at_once_pauses_at_its_wait(
    owner_chat, adult_member, invite_for_test, confirmation_status, steps_rows, stored_notes
):  # F10
    await invite_for_test("Paul", "adult")
    reply = await owner_chat.say_with_tool(
        TOOL, chain(note_for("Alex", "a"), note_for("Alex", "b", "Paul"))
    )
    rid = str(reply.pending_confirmation_id)
    steps = [
        {"tool": "note_leave", "arguments": {"recipient": "Alex", "text": "a2"}},
        {
            "tool": "note_leave",
            "arguments": {"recipient": "Alex", "text": "b"},
            "after_arrival_of": "Paul",
        },
    ]
    amend = ToolCall(
        "c1",
        "confirmation_amend",
        json.dumps({"request_id": rid, "arguments": {"steps": steps}, "ask_again": False}),
    )
    owner_chat.use_model(
        FakeChatModel(tool_script=["change", amend, "C'est commencé."], judge="do")
    )
    answer = await owner_chat.say("oui, mais écris a2")
    assert answer.model_results[0]["outcome"] == "paused"
    assert await confirmation_status(rid) == "waiting_arrival"
    assert [note.text for note in await stored_notes()] == ["a2"]
    # The wait is carried through the change.
    assert steps_of_rows(await steps_rows(rid)) == [(0, "done", None), (1, "planned", "Paul")]


# --- the arrival, and the pass that resumes or stops the chain ------------------------------------


async def test_a_paused_chain_resumes_after_the_arrival_and_its_lifetime_restarts(
    owner_chat,
    join_with_invitation,
    upkeep_pass,
    clock,
    stored_notes,
    confirmation_status,
    session_factory,
    last_audit_entry,
):  # spec 4.3b, 8.3
    cid = await paused_chain(owner_chat, INVITE_PAUL, note_for("Paul", "bienvenue", "Paul"))
    clock.advance(timedelta(days=2))
    invitation = await invitation_named(session_factory, "Paul")
    paul = await join_with_invitation(invitation.id, "paul")
    await upkeep_pass()
    assert await confirmation_status(cid) == "executed"
    [note] = await stored_notes()
    assert (note.recipient_id, note.text) == (paul.id, "bienvenue")
    # Its lifetime starts again at the arrival.
    assert (await request_row(session_factory, cid)).created_at == clock()
    resumed = await last_audit_entry("chain.resumed")
    assert resumed.details == {
        "confirmation_id": str(cid),
        "invitation_id": str(invitation.id),
        "arrived": f"person:{paul.id}",
    }


async def test_two_waits_in_one_chain_each_wait_for_their_own_invitation(
    owner_chat, join_with_invitation, upkeep_pass, steps_rows, confirmation_status, session_factory
):  # F22
    cid = await paused_chain(
        owner_chat,
        INVITE_PAUL,
        note_for("Paul", "a", "Paul"),
        INVITE_KIM,
        note_for("Kim", "b", "Kim"),
    )
    paul = await invitation_named(session_factory, "Paul")
    assert (await request_row(session_factory, cid)).waiting_for_invitation_id == paul.id
    await join_with_invitation(paul.id, "paul")
    await upkeep_pass()
    # Paul's arrival does not count for Kim: the chain waits for Kim now.
    assert await confirmation_status(cid) == "waiting_arrival"
    kim = await invitation_named(session_factory, "Kim")
    assert (await request_row(session_factory, cid)).waiting_for_invitation_id == kim.id
    assert [row.status for row in await steps_rows(cid)] == ["done", "done", "done", "planned"]
    await join_with_invitation(kim.id, "kim")
    await upkeep_pass()
    assert await confirmation_status(cid) == "executed"


async def test_a_paused_chain_stops_when_its_invitation_ends(
    owner_chat, adult_member, upkeep_pass, clock, steps_rows, confirmation_status, last_audit_entry
):  # spec 4.3b
    cid = await paused_chain(
        owner_chat, INVITE_PAUL, note_for("Paul", "a", "Paul"), note_for("Alex", "fin")
    )
    # Paul's invitation is cancelled, in words.
    await owner_chat.say_with_tool("household_cancel_invitation", {"display_name": "Paul"})
    await owner_chat.say("oui")
    await upkeep_pass()
    assert await confirmation_status(cid) == "failed"
    rows = await steps_rows(cid)
    assert [row.status for row in rows] == ["done", "failed", "stopped"]
    assert rows[1].result == {
        "key": "action.card.failed.chain.not_arrived",
        "params": {"name": "Paul"},
        "reason": "not_arrived",
    }
    stopped = await last_audit_entry("chain.stopped")
    assert stopped.details == {"confirmation_id": str(cid), "step": 2, "reason": "not_arrived"}
    # The same when the link expires.
    cid = await paused_chain(owner_chat, INVITE_KIM, note_for("Kim", "b", "Kim"))
    clock.advance(timedelta(days=8))
    await upkeep_pass()
    assert await confirmation_status(cid) == "failed"
    assert [row.status for row in await steps_rows(cid)] == ["done", "failed"]


async def test_a_paused_chain_lives_while_its_invitation_can_be_accepted(
    owner_chat, set_setting, clock, confirmation_status, upkeep_pass
):  # spec 4.3b
    await set_setting("confirmation_max_lifetime_hours", 1)
    cid = await paused_chain(owner_chat, INVITE_PAUL, HAND_OVER_TO_PAUL)
    clock.advance(timedelta(hours=2))
    await upkeep_pass()
    assert await confirmation_status(cid) == "waiting_arrival"
    page = (await owner_chat.get("/chat")).text
    [paused] = [card for card in confirmation_cards(page) if str(cid) in card]
    assert 'data-confirmation-status="waiting_arrival"' in paused


async def test_a_paused_chain_of_someone_who_left_never_resumes(
    adult_chat,
    owner_member,
    grant,
    join_with_invitation,
    upkeep_pass,
    stored_notes,
    steps_rows,
    confirmation_status,
    session_factory,
):  # Review Focus 2
    alex = adult_chat.person
    await grant("household.invite", person=alex, access="allow")
    cid = await paused_chain(
        adult_chat,
        ("household_invite", {"display_name": "Paul", "role": "guest"}),
        note_for("Paul", "x", "Paul"),
    )
    assert await confirmation_status(cid) == "waiting_arrival"
    async with session_factory() as writer:
        await disable(writer, alex, adult_chat.clock(), by=alex.id)
        await writer.commit()
    invitation = await invitation_named(session_factory, "Paul")
    await join_with_invitation(invitation.id, "paul")
    await upkeep_pass()
    assert await confirmation_status(cid) == "failed"
    rows = await steps_rows(cid)
    assert [row.status for row in rows] == ["done", "failed"]
    assert rows[1].result is not None and rows[1].result["reason"] == "no_longer_possible"
    assert await stored_notes() == []


async def test_the_steps_after_the_arrival_are_judged_with_the_rights_of_then(
    adult_chat,
    owner_member,
    grant,
    revoke_right,
    join_with_invitation,
    upkeep_pass,
    steps_rows,
    confirmation_status,
    count_invitations,
    session_factory,
    last_audit_entry,
):  # spec 6.2: a chain woken by an arrival goes through the barrier in its turn too
    right = await grant("household.invite", person=adult_chat.person, access="allow")
    cid = await paused_chain(
        adult_chat,
        ("household_invite", {"display_name": "Paul", "role": "guest"}),
        ("household_invite", {"display_name": "Kim", "role": "guest"}, "Paul"),
    )
    assert await confirmation_status(cid) == "waiting_arrival"
    # While the chain waits, the owner takes Alex's right to invite away (an adult invites by
    # default: taking it away is saying no).
    await revoke_right(right)
    await grant("household.invite", person=adult_chat.person, access="deny")
    paul = await invitation_named(session_factory, "Paul")
    await join_with_invitation(paul.id, "paul")
    await upkeep_pass()
    assert await confirmation_status(cid) == "failed"
    rows = await steps_rows(cid)
    assert [row.status for row in rows] == ["done", "failed"]
    assert rows[1].result is not None and rows[1].result["reason"] == "not_allowed"
    assert await count_invitations() == 1
    stopped = await last_audit_entry("chain.stopped")
    assert stopped.details == {"confirmation_id": str(cid), "step": 2, "reason": "not_allowed"}


async def test_a_new_link_of_the_requester_takes_a_chain_up_again_after_its_link_expired(
    owner_chat,
    join_with_invitation,
    upkeep_pass,
    clock,
    confirmation_status,
    stored_notes,
    session_factory,
):  # spec 4.3b: "nouveau lien"
    cid = await paused_chain(owner_chat, INVITE_PAUL, note_for("Paul", "bienvenue", "Paul"))
    expired = await invitation_named(session_factory, "Paul")
    clock.advance(timedelta(days=8))
    await upkeep_pass()
    assert await confirmation_status(cid) == "failed"
    # The owner makes a new link for Paul; Domo takes the chain up again under the same yes
    # (its lifetime started again when it stopped).
    await owner_chat.say_with_tool("household_invite", {"display_name": "Paul", "role": "adult"})
    await owner_chat.say("je confirme")
    new = await invitation_named(session_factory, "Paul")
    assert new.id != expired.id and new.created_by == owner_chat.person.id
    reply = await owner_chat.say_with_tool("chain_continue", {"request_id": str(cid)}, judge="do")
    assert reply.model_results[-1]["chain"] == "waiting_for_arrival"
    assert await confirmation_status(cid) == "waiting_arrival"
    assert (await request_row(session_factory, cid)).waiting_for_invitation_id == new.id
    # Paul joins with the new link: the pass resumes the chain, which waited for that one.
    paul = await join_with_invitation(new.id, "paul")
    await upkeep_pass()
    assert await confirmation_status(cid) == "executed"
    [note] = await stored_notes()
    assert (note.recipient_id, note.text) == (paul.id, "bienvenue")


async def test_a_link_someone_else_made_never_takes_a_chain_up_again(
    owner_chat,
    adult_chat,
    grant,
    upkeep_pass,
    clock,
    steps_rows,
    confirmation_status,
    session_factory,
):  # the yes given to a chain never covers a link its requester does not hold
    cid = await paused_chain(owner_chat, INVITE_PAUL, note_for("Paul", "bienvenue", "Paul"))
    await owner_chat.say_with_tool("household_cancel_invitation", {"display_name": "Paul"})
    await owner_chat.say("oui")
    await upkeep_pass()
    assert await confirmation_status(cid) == "failed"
    # A moment later, Alex invites someone under the same name: the link is Alex's.
    clock.advance(timedelta(minutes=1))
    await grant("household.invite", person=adult_chat.person, access="allow")
    await adult_chat.say_with_tool("household_invite", {"display_name": "Paul", "role": "guest"})
    await adult_chat.say("je confirme")
    assert (await invitation_named(session_factory, "Paul")).created_by == adult_chat.person.id
    # Taken up again under the owner's yes, the wait does not take Alex's link: it stops again.
    await owner_chat.say_with_tool("chain_continue", {"request_id": str(cid)}, judge="do")
    assert await confirmation_status(cid) == "failed"
    rows = await steps_rows(cid)
    assert [row.status for row in rows] == ["done", "failed"]
    assert rows[1].result is not None and rows[1].result["reason"] == "not_arrived"


async def test_a_wait_never_takes_a_link_someone_else_made_after_the_yes(
    owner_chat, adult_chat, grant, invite_for_test, clock, confirmation_status, steps_rows
):
    await invite_for_test("Paul", "adult")
    reply = await owner_chat.say_with_tool(TOOL, chain(note_for("Alex", "a", "Paul")))
    cid = reply.pending_confirmation_id
    # Paul's invitation is cancelled before the yes: the chain stops at its wait at once.
    await owner_chat.say_with_tool("household_cancel_invitation", {"display_name": "Paul"})
    await owner_chat.say("oui")
    assert status_of(await owner_chat.post(f"/confirmations/{cid}/approve")) == "failed"
    # Alex then invites someone under that name: that link was never covered by the owner's yes.
    clock.advance(timedelta(minutes=1))
    await grant("household.invite", person=adult_chat.person, access="allow")
    await adult_chat.say_with_tool("household_invite", {"display_name": "Paul", "role": "guest"})
    await adult_chat.say("je confirme")
    await owner_chat.say_with_tool("chain_continue", {"request_id": str(cid)}, judge="do")
    assert await confirmation_status(cid) == "failed"
    [row] = await steps_rows(cid)
    assert row.result is not None and row.result["reason"] == "not_arrived"


async def test_a_paused_chain_is_neither_shown_again_nor_asked_an_explicit_yes(
    owner_chat, confirmation_status, session_factory
):  # nothing answers it: it goes on under the yes already given
    cid = await paused_chain(owner_chat, INVITE_PAUL, HAND_OVER_TO_PAUL)
    reply = await owner_chat.say_with_tool("confirmation_remind", {"request_id": str(cid)})
    refused = reply.model_results[-1]
    assert (refused["status"], refused["reason"]) == ("invalid", "invalid")
    assert "not shown again" in refused["detail"]
    reply = await owner_chat.say_with_tool(
        "confirmation_require_explicit", {"request_id": str(cid), "reason": "Paul, vraiment ?"}
    )
    refused = reply.model_results[-1]
    assert (refused["status"], refused["reason"]) == ("invalid", "invalid")
    assert "asks for no yes" in refused["detail"]
    assert await confirmation_status(cid) == "waiting_arrival"
    c = await request_row(session_factory, cid)
    # What it asked stays what the judge asked (a plain yes: nothing asks more by default).
    assert (c.reminder_count, c.ask, c.asked_by) == (0, "yes", "judge")


async def test_the_pass_resumes_a_chain_after_a_restart(
    owner_chat,
    settings,
    join_with_invitation,
    clock,
    confirmation_status,
    stored_notes,
    session_factory,
):  # spec 4.3b: "même si l'application a redémarré"
    cid = await paused_chain(owner_chat, INVITE_PAUL, note_for("Paul", "bienvenue", "Paul"))
    invitation = await invitation_named(session_factory, "Paul")
    # Paul joins while no pass runs (the application is stopped, say).
    await join_with_invitation(invitation.id, "paul")
    assert await confirmation_status(cid) == "waiting_arrival"
    app = create_app(settings)
    app.dependency_overrides[get_clock] = lambda: clock
    async with app.router.lifespan_context(app):
        # Its first pass, at once.
        with anyio.fail_after(30):
            while await confirmation_status(cid) != "executed":
                await anyio.sleep(0.02)
    assert [note.text for note in await stored_notes()] == ["bienvenue"]


class SlowNote:
    """``note_leave``, slowed down: it says when it starts, then takes a moment."""

    def __init__(self) -> None:
        self._tool = TOOLS["note_leave"]
        self.definition = self._tool.definition
        self.action_type = self._tool.action_type
        self.started = asyncio.Event()

    async def describe(self, arguments: Any, actor: Any, ctx: Any) -> Any:
        return await self._tool.describe(arguments, actor, ctx)

    async def execute(self, arguments: Any, ctx: Any) -> Any:
        self.started.set()
        await asyncio.sleep(0.3)
        return await self._tool.execute(arguments, ctx)


async def test_a_resume_cut_by_a_shutdown_still_settles(
    owner_chat, chat_server, join_with_invitation, confirmation_status, session_factory
):  # F11
    slow = SlowNote()
    registry = {**TOOLS, "note_leave": slow}
    chat_server.app.dependency_overrides[get_tool_registry] = lambda: registry
    cid = await paused_chain(owner_chat, INVITE_PAUL, note_for("Paul", "bienvenue", "Paul"))
    invitation = await invitation_named(session_factory, "Paul")
    await join_with_invitation(invitation.id, "paul")
    app = chat_server.app
    await arrivals_pass(arrival_deps(app), app.state.wakes)(owner_chat.clock())
    with anyio.fail_after(WAIT_SECONDS):
        await slow.started.wait()
    # The application stops while the step runs: the chain is run and recorded all the same.
    await app.state.wakes.stop(WAIT_SECONDS)
    assert await confirmation_status(cid) == "executed"


# --- after the arrival: the rest goes on, or a rule added since asks again; Domo woken ---------


async def test_after_the_arrival_a_hand_over_goes_on_under_the_yes_given(
    owner_chat,
    join_with_invitation,
    upkeep_pass,
    person_row,
    count_owners,
    confirmation_status,
    session_factory,
    audit_actions,
):
    """The owner's decision of 2026-11-15: what was asked for and understood is done. A chain
    that hands the owner's role over once someone has joined was given its yes: at the arrival it
    is not asked again by default (it was, "to see who arrived first")."""
    cid = await paused_chain(owner_chat, INVITE_PAUL, HAND_OVER_TO_PAUL, words="oui")
    invitation = await invitation_named(session_factory, "Paul")
    paul = await join_with_invitation(invitation.id, "paul")
    await upkeep_pass()
    assert await confirmation_status(cid) == "executed"
    assert (await person_row(paul)).role == "owner"
    assert (await person_row(owner_chat.person)).role == "adult"
    assert await count_owners() == 1
    assert "chain.arrived" not in await audit_actions()


async def test_after_the_arrival_a_rule_added_since_asks_for_the_explicit_yes_again(
    owner_chat,
    grant,
    join_with_invitation,
    upkeep_pass,
    chat_server,
    recent_replies,
    person_row,
    count_owners,
    confirmation_status,
    session_factory,
    last_audit_entry,
):  # spec 4.3b, 8.3
    reply = await owner_chat.say_with_tool(TOOL, chain(INVITE_PAUL, HAND_OVER_TO_PAUL))
    cid = reply.pending_confirmation_id
    answer = await owner_chat.say("oui")
    assert await confirmation_status(cid) == "waiting_arrival"
    await a_firm_yes_is_asked_since(grant, owner_chat)
    invitation = await invitation_named(session_factory, "Paul")
    paul = await join_with_invitation(invitation.id, "paul")
    owner_chat.use_model(FakeChatModel(["Paul est là : je lui donne ton rôle ?"]))
    await upkeep_pass()
    # The hand-over is not made under the yes of before: the owner's rule asks more than it gave.
    assert await confirmation_status(cid) == "pending"
    assert (await person_row(owner_chat.person)).role == "owner"
    c = await request_row(session_factory, cid)
    assert (c.ask, c.asked_by, c.approver_kind) == ("explicit_yes", "rule", "self")
    assert (c.decided_by, c.decided_at, c.decided_via, c.decided_message_id) == (None,) * 4
    assert c.waiting_for_invitation_id == invitation.id
    arrived = await last_audit_entry("chain.arrived")
    assert arrived.details == {
        "confirmation_id": str(cid),
        "invitation_id": str(invitation.id),
        "step": 2,
    }
    # Domo is woken in the owner's conversation, and told it was asked again at the arrival.
    [woken] = await recent_replies(owner_chat.person, after=answer.message_id)
    assert woken.content == "Paul est là : je lui donne ton rôle ?"
    system = chat_server.calls[-1].system
    assert '"outcome": "arrived"' in system and '"via": "arrival"' in system
    decided = decided_in(system)
    assert (decided["request_id"], decided["decided_by"], decided["result"]) == (
        str(cid),
        "the person",
        None,
    )
    # The card comes back under the wake's reply: who arrived, and the explicit yes it needs.
    page = (await owner_chat.get("/chat")).text
    shown = [card for card in confirmation_cards(page) if str(cid) in card][-1]
    assert 'data-confirmation-status="pending"' in shown
    assert t("confirmation.outcome.arrived", "en", name="Paul") in plain(shown)
    assert t("confirmation.hint.explicit", "en") in plain(shown)
    owner_chat.use_model(FakeChatModel(["ok"]))
    # The owner's words answer it: a plain yes is not enough, "I confirm" is.
    await owner_chat.say("oui")
    assert await confirmation_status(cid) == "pending"
    await owner_chat.say("je confirme")
    assert await confirmation_status(cid) == "executed"
    assert (await person_row(paul)).role == "owner"
    assert await count_owners() == 1


async def test_the_button_gives_the_yes_asked_again_after_the_arrival(
    owner_chat, grant, join_with_invitation, upkeep_pass, person_row, count_owners, session_factory
):  # spec 8.3
    cid = await paused_chain(owner_chat, INVITE_PAUL, HAND_OVER_TO_PAUL, words="oui")
    await a_firm_yes_is_asked_since(grant, owner_chat)
    invitation = await invitation_named(session_factory, "Paul")
    paul = await join_with_invitation(invitation.id, "paul")
    await upkeep_pass()
    assert status_of(await owner_chat.post(f"/confirmations/{cid}/approve")) == "executed"
    assert (await person_row(paul)).role == "owner"
    assert await count_owners() == 1


async def test_a_rule_added_while_a_let_through_chain_waits_is_asked_at_the_arrival(
    owner_chat,
    grant,
    join_with_invitation,
    upkeep_pass,
    confirmation_ask,
    confirmation_status,
    person_row,
    audit_actions,
    session_factory,
):  # final review I2: a rule "always ask me" holds for what waits, too
    reply = await owner_chat.say_with_tool(
        TOOL,
        chain(
            ("household_invite", {"display_name": "Gil", "role": "adult"}),
            ("household_change_role", {"person": "Gil", "role": "child"}, "Gil"),
        ),
        judge="do",
    )
    cid = reply.pending_confirmation_id
    assert await confirmation_status(cid) == "waiting_arrival"
    assert (await request_row(session_factory, cid)).decided_via == "judge"
    # While it waits, the owner asks to be asked before any change of role.
    await grant("household.change_role", ask="yes", summary="Ask me before a change of role.")
    invitation = await invitation_named(session_factory, "Gil")
    gil = await join_with_invitation(invitation.id, "gil")
    owner_chat.use_model(FakeChatModel(["Gil est là : je le passe en enfant ?"]))
    await upkeep_pass()
    # Nobody said yes to it: the rule is asked now, and nothing ran under the judge's leave.
    assert await confirmation_status(cid) == "pending"
    assert await confirmation_ask(cid) == ("yes", "rule", None)
    assert (await person_row(gil)).role == "adult"
    assert "chain.arrived" in await audit_actions()
    owner_chat.use_model(FakeChatModel(["ok"]))
    await owner_chat.say("oui")
    assert await confirmation_status(cid) == "executed"
    assert (await person_row(gil)).role == "child"


async def test_a_rule_added_while_a_chain_waits_counts_above_the_yes_given(
    owner_chat,
    grant,
    join_with_invitation,
    upkeep_pass,
    confirmation_ask,
    confirmation_status,
    stored_notes,
    session_factory,
):  # final review I2: the yes given covers what it gave, not more
    in_words = await owner_chat.say_with_tool(
        TOOL, chain(INVITE_PAUL, note_for("Paul", "bienvenue", "Paul"))
    )
    await owner_chat.say("oui")
    with_the_explicit_yes = await owner_chat.say_with_tool(
        TOOL, chain(INVITE_KIM, note_for("Kim", "salut", "Kim"))
    )
    await owner_chat.say("je confirme")
    first, second = in_words.pending_confirmation_id, with_the_explicit_yes.pending_confirmation_id
    for cid in (first, second):
        assert await confirmation_status(cid) == "waiting_arrival"
    # While they wait, the owner asks for the button, or "I confirm", before any note.
    await grant("note.leave", ask="explicit_yes", summary="Ask me to confirm a note.")
    for name in ("Paul", "Kim"):
        invitation = await invitation_named(session_factory, name)
        await join_with_invitation(invitation.id, name.lower())
    owner_chat.use_model(FakeChatModel(["Paul et Kim sont là."]))
    await upkeep_pass()
    # A yes in words never gave an explicit yes: it is asked now; "I confirm" gave it.
    assert await confirmation_status(first) == "pending"
    assert await confirmation_ask(first) == ("explicit_yes", "rule", None)
    assert await confirmation_status(second) == "executed"
    assert [note.text for note in await stored_notes()] == ["salut"]
    assert status_of(await owner_chat.post(f"/confirmations/{first}/approve")) == "executed"
    assert sorted(note.text for note in await stored_notes()) == ["bienvenue", "salut"]


async def test_a_chain_whose_proof_was_given_goes_on_after_the_arrival(
    owner_chat,
    join_with_invitation,
    upkeep_pass,
    confirmation_ask,
    confirmation_status,
    person_row,
    session_factory,
):  # it pinned the proof asked again at the arrival (a hand-over was always seen again)
    reply = await owner_chat.say_with_tool(
        TOOL,
        chain(
            ("household_invite", {"display_name": "Gil", "role": "adult"}),
            ("household_transfer_ownership", {"person": "Gil"}, "Gil"),
        ),
        judge="proof: an unusual hand-over",
    )
    cid = reply.pending_confirmation_id
    assert await confirmation_ask(cid) == ("proof", "judge", "an unusual hand-over")
    proved = await owner_chat.client.post(
        f"/confirmations/{cid}/prove",
        data={"means": "password", "password": MEMBER_PASSWORD},
        headers={"hx-request": "true"},
    )
    assert status_of(proved) == "paused"
    assert await confirmation_status(cid) == "waiting_arrival"
    invitation = await invitation_named(session_factory, "Gil")
    gil = await join_with_invitation(invitation.id, "gil")
    owner_chat.use_model(FakeChatModel(["Gil est là."]))
    await upkeep_pass()
    # The proof was given: nothing asks it again by default, and the rest goes on under it. (It
    # keeps what it asked, with who asked it and why.)
    assert await confirmation_status(cid) == "executed"
    assert await confirmation_ask(cid) == ("proof", "judge", "an unusual hand-over")
    assert (await person_row(gil)).role == "owner"


async def test_the_owners_proof_on_the_hand_over_is_asked_at_the_arrival_and_words_never_give_it(
    owner_chat,
    grant,
    join_with_invitation,
    upkeep_pass,
    confirmation_ask,
    confirmation_status,
    person_row,
    session_factory,
):
    """Review M5: nothing asks again by default at an arrival, but a rule of the owner's own
    still does. He asks for a proof before his role is handed over, while a chain given a firm
    yes waits: at the arrival the proof is asked, a firm yes in words does not give it, the
    proof does."""
    cid = await paused_chain(owner_chat, INVITE_PAUL, HAND_OVER_TO_PAUL, words="je confirme")
    await grant(
        "household.transfer_ownership",
        person=owner_chat.person,
        ask="proof",
        summary="A proof before my role is handed over.",
    )
    invitation = await invitation_named(session_factory, "Paul")
    paul = await join_with_invitation(invitation.id, "paul")
    owner_chat.use_model(FakeChatModel(["Paul est là."]))
    await upkeep_pass()
    assert await confirmation_status(cid) == "pending"
    assert await confirmation_ask(cid) == ("proof", "rule", None)
    assert (await person_row(owner_chat.person)).role == "owner"
    await owner_chat.say("je confirme")
    assert await confirmation_status(cid) == "pending"
    assert (await person_row(paul)).role == "adult"
    proved = await owner_chat.client.post(
        f"/confirmations/{cid}/prove",
        data={"means": "password", "password": MEMBER_PASSWORD},
        headers={"hx-request": "true"},
    )
    assert status_of(proved) == "executed"
    assert (await person_row(paul)).role == "owner"


async def test_a_chain_asked_again_at_an_arrival_never_asks_less_than_before(
    owner_chat, confirmation_ask, confirmation_status, steps_rows, session_factory
):
    """Review M5 (final review I3, "ask more, never less"): when a chain is asked again at an
    arrival for less than what it asked before, what it asked before stays, with who asked it
    and why: a proof stays a proof."""
    reply = await owner_chat.say_with_tool(
        TOOL,
        chain(
            ("household_invite", {"display_name": "Gil", "role": "adult"}),
            ("household_transfer_ownership", {"person": "Gil"}, "Gil"),
        ),
        judge="proof: an unusual hand-over",
    )
    cid = reply.pending_confirmation_id
    proved = await owner_chat.client.post(
        f"/confirmations/{cid}/prove",
        data={"means": "password", "password": MEMBER_PASSWORD},
        headers={"hx-request": "true"},
    )
    assert status_of(proved) == "paused"
    rows = await steps_rows(cid)
    async with session_factory() as writer:
        # As the pass claims it for its resume.
        await writer.execute(
            update(Confirmation).where(Confirmation.id == cid).values(status="executing")
        )
        await writer.commit()
        assert await arrivals._ask_again(writer, cid, rows, Asked("explicit_yes", "rule"))
    assert await confirmation_status(cid) == "pending"
    assert await confirmation_ask(cid) == ("proof", "judge", "an unusual hand-over")


async def test_the_hand_over_after_the_arrival_is_judged_when_the_yes_is_given_again(
    owner_chat,
    grant,
    join_with_invitation,
    upkeep_pass,
    person_row,
    steps_rows,
    confirmation_status,
    session_factory,
):  # spec 6.3: an adult present when it is made
    cid = await paused_chain(owner_chat, INVITE_PAUL, HAND_OVER_TO_PAUL, words="oui")
    await a_firm_yes_is_asked_since(grant, owner_chat)
    invitation = await invitation_named(session_factory, "Paul")
    paul = await join_with_invitation(invitation.id, "paul")
    await upkeep_pass()
    assert await confirmation_status(cid) == "pending"
    # Paul leaves before the owner gives the yes again: nobody is handed the role.
    async with session_factory() as writer:
        await disable(writer, await person_row(paul), owner_chat.clock(), by=owner_chat.person.id)
        await writer.commit()
    await owner_chat.say("je confirme")
    assert await confirmation_status(cid) == "failed"
    rows = await steps_rows(cid)
    assert [row.status for row in rows] == ["done", "failed"]
    assert (await person_row(owner_chat.person)).role == "owner"


async def test_a_resumed_chain_that_ends_wakes_domo(
    owner_chat,
    join_with_invitation,
    upkeep_pass,
    chat_server,
    recent_replies,
    confirmation_status,
    session_factory,
):  # spec 4.3b: "Domo est réveillé ... comme après un bouton"
    reply = await owner_chat.say_with_tool(
        TOOL, chain(INVITE_PAUL, note_for("Paul", "bienvenue", "Paul"))
    )
    cid = reply.pending_confirmation_id
    answer = await owner_chat.say("je confirme")
    invitation = await invitation_named(session_factory, "Paul")
    await join_with_invitation(invitation.id, "paul")
    owner_chat.use_model(FakeChatModel(["Paul est arrivé, et ton mot l'attend."]))
    await upkeep_pass()
    # A free rest runs at once, under the yes already given.
    assert await confirmation_status(cid) == "executed"
    [woken] = await recent_replies(owner_chat.person, after=answer.message_id)
    assert woken.content == "Paul est arrivé, et ton mot l'attend."
    system = chat_server.calls[-1].system
    for told in ('"outcome": "executed"', '"via": "arrival"', '"woken": true'):
        assert told in system
    decided = decided_in(system)
    assert (decided["request_id"], decided["decided_by"]) == (str(cid), "the person")
    assert decided["chain"]["left"] == []


async def test_a_chain_whose_invitation_ended_wakes_domo_with_the_failed_step(
    owner_chat, upkeep_pass, chat_server, recent_replies, clock, confirmation_status
):  # spec 4.3b: "Domo est réveillé dans la conversation de la personne et décide"
    reply = await owner_chat.say_with_tool(
        TOOL, chain(INVITE_PAUL, note_for("Paul", "bienvenue", "Paul"))
    )
    cid = reply.pending_confirmation_id
    answer = await owner_chat.say("je confirme")
    clock.advance(timedelta(days=8))
    owner_chat.use_model(FakeChatModel(["Le lien de Paul a expiré : on en refait un ?"]))
    await upkeep_pass()
    assert await confirmation_status(cid) == "failed"
    [woken] = await recent_replies(owner_chat.person, after=answer.message_id)
    assert woken.content == "Le lien de Paul a expiré : on en refait un ?"
    system = chat_server.calls[-1].system
    for told in ('"failed_reason": "not_arrived"', '"can_continue": true', '"via": "arrival"'):
        assert told in system
    decided = decided_in(system)
    assert (decided["outcome"], decided["decided_by"], decided["result"], decided["woken"]) == (
        "failed",
        "the person",
        None,
        True,
    )


async def test_the_steps_after_the_arrival_are_judged_with_the_facts_of_then(
    adult_chat,
    owner_member,
    grant,
    join_with_invitation,
    upkeep_pass,
    chat_server,
    stored_notes,
    steps_rows,
    confirmation_status,
    session_factory,
):  # spec 6.2, and Domo woken with the step that failed
    await grant("household.invite", person=adult_chat.person, access="allow")
    cid = await paused_chain(
        adult_chat,
        ("household_invite", {"display_name": "Paul", "role": "guest"}),
        note_for("Paul", "bienvenue", "Paul"),
    )
    assert await confirmation_status(cid) == "waiting_arrival"
    # While the chain waits, the owner takes Alex's right to leave notes away.
    await grant("note.leave", person=adult_chat.person, access="deny")
    paul = await invitation_named(session_factory, "Paul")
    await join_with_invitation(paul.id, "paul")
    await upkeep_pass()
    assert await confirmation_status(cid) == "failed"
    rows = await steps_rows(cid)
    assert [row.status for row in rows] == ["done", "failed"]
    assert rows[1].result is not None and rows[1].result["reason"] == "not_allowed"
    assert await stored_notes() == []
    decided = decided_in(chat_server.calls[-1].system)
    assert (decided["outcome"], decided["via"], decided["decided_by"]) == (
        "failed",
        "arrival",
        "the person",
    )
    assert decided["chain"]["failed_reason"] == "not_allowed"


async def test_a_button_that_pauses_a_chain_wakes_domo_with_paused(
    owner_chat, chat_server, recent_replies, confirmation_status
):
    reply = await owner_chat.say_with_tool(
        TOOL, chain(INVITE_PAUL, note_for("Paul", "bienvenue", "Paul"))
    )
    cid = reply.pending_confirmation_id
    owner_chat.use_model(FakeChatModel(["La suite attend Paul."]))
    assert status_of(await owner_chat.post(f"/confirmations/{cid}/approve")) == "paused"
    assert await confirmation_status(cid) == "waiting_arrival"
    [woken] = await recent_replies(owner_chat.person, after=reply.message_id)
    assert woken.content == "La suite attend Paul."
    system = chat_server.calls[-1].system
    assert '"outcome": "paused"' in system
    assert (decided_in(system)["via"], decided_in(system)["woken"]) == ("button", True)


async def a_chain_whose_awaited_person_joined(
    pilot: Any,
    join_with_invitation: Any,
    session_factory: Any,
    *calls: tuple[Any, ...],
    words: str = "je confirme",
) -> tuple[uuid.UUID, uuid.UUID, Invitation]:
    """A chain of ``calls`` the pilot asks for and confirms with ``words`` ("je confirme" unless
    told otherwise), paused at its first wait for Paul, who has joined since (no pass has run
    yet): its request, the reply to those words, and Paul's invitation (as read before he
    joined)."""
    reply = await pilot.say_with_tool(TOOL, chain(*calls))
    cid = reply.pending_confirmation_id
    assert cid is not None, reply
    answer = await pilot.say(words)
    invitation = await invitation_named(session_factory, "Paul")
    await join_with_invitation(invitation.id, "paul")
    return cid, answer.message_id, invitation


async def test_a_resume_that_fails_settles_the_chain_and_wakes_domo(
    owner_chat,
    join_with_invitation,
    upkeep_pass,
    chat_server,
    recent_replies,
    confirmation_status,
    session_factory,
    monkeypatch,
):  # spec 4.3b, final review M2: no chain left running that nobody is told of
    cid, answer, _ = await a_chain_whose_awaited_person_joined(
        owner_chat,
        join_with_invitation,
        session_factory,
        INVITE_PAUL,
        note_for("Paul", "bienvenue", "Paul"),
    )

    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("the database went away")

    monkeypatch.setattr(arrivals, "run_under_the_same_yes", broken)
    owner_chat.use_model(FakeChatModel(["Je n'ai pas pu finir : on reprend ?"]))
    await upkeep_pass()
    assert await confirmation_status(cid) == "failed"
    [woken] = await recent_replies(owner_chat.person, after=answer)
    assert woken.content == "Je n'ai pas pu finir : on reprend ?"
    decided = decided_in(chat_server.calls[-1].system)
    assert (decided["outcome"], decided["via"], decided["decided_by"], decided["woken"]) == (
        "failed",
        "arrival",
        "the person",
        True,
    )


async def test_a_chain_that_cannot_be_resumed_is_failed_and_wakes_domo(
    owner_chat,
    join_with_invitation,
    upkeep_pass,
    chat_server,
    recent_replies,
    confirmation_status,
    session_factory,
):  # spec 4.3b, final review M2
    cid, answer, _ = await a_chain_whose_awaited_person_joined(
        owner_chat,
        join_with_invitation,
        session_factory,
        INVITE_PAUL,
        note_for("Paul", "bienvenue", "Paul"),
    )
    # What it remembers of the one who asked no longer reads back: it is never guessed.
    async with session_factory() as writer:
        await writer.execute(
            update(Confirmation).where(Confirmation.id == cid).values(chain_basis={"role": "owner"})
        )
        await writer.commit()
    owner_chat.use_model(FakeChatModel(["Ta suite n'a pas pu reprendre."]))
    await upkeep_pass()
    assert await confirmation_status(cid) == "failed"
    [woken] = await recent_replies(owner_chat.person, after=answer)
    assert woken.content == "Ta suite n'a pas pu reprendre."
    decided = decided_in(chat_server.calls[-1].system)
    assert (decided["outcome"], decided["via"]) == ("failed", "arrival")


async def test_a_change_after_the_arrival_still_says_who_joined(
    owner_chat, grant, join_with_invitation, upkeep_pass, session_factory, confirmation_status
):  # spec 4.3b, final review M5: the one who gives the yes sees who actually arrived
    cid, _, invitation = await a_chain_whose_awaited_person_joined(
        owner_chat,
        join_with_invitation,
        session_factory,
        INVITE_PAUL,
        HAND_OVER_TO_PAUL,
        words="oui",
    )
    await a_firm_yes_is_asked_since(grant, owner_chat)
    owner_chat.use_model(FakeChatModel(["Paul est là : je lui donne ton rôle ?"]))
    await upkeep_pass()
    assert await confirmation_status(cid) == "pending"
    # "Yes, but leave him a word first": a new request, which still says who joined.
    steps = [
        {"tool": INVITE_PAUL[0], "arguments": INVITE_PAUL[1]},
        {"tool": "note_leave", "arguments": {"recipient": "Paul", "text": "bienvenue"}},
        {
            "tool": HAND_OVER_TO_PAUL[0],
            "arguments": HAND_OVER_TO_PAUL[1],
            "after_arrival_of": "Paul",
        },
    ]
    owner_chat.use_model(
        FakeChatModel(tool_script=["change", amend(str(cid), {"steps": steps}), "Je redemande."])
    )
    answer = await owner_chat.say("oui, mais laisse-lui d'abord un mot")
    result = answer.model_results[0]
    assert result["outcome"] == "asked_again"
    new = result["request_id"]
    assert (await request_row(session_factory, new)).waiting_for_invitation_id == invitation.id
    [card] = [card for card in confirmation_cards(answer.cards_html) if new in card]
    assert t("confirmation.outcome.arrived", "en", name="Paul") in plain(card)


async def test_a_wake_that_presents_a_request_and_ends_in_error_is_logged(
    owner_chat,
    grant,
    join_with_invitation,
    upkeep_pass,
    session_factory,
    confirmation_status,
    caplog,
):  # final review M8: no page reads a wake, so its failure is logged
    cid, _, _ = await a_chain_whose_awaited_person_joined(
        owner_chat,
        join_with_invitation,
        session_factory,
        INVITE_PAUL,
        HAND_OVER_TO_PAUL,
        words="oui",
    )
    await a_firm_yes_is_asked_since(grant, owner_chat)
    owner_chat.use_model(FakeChatModel(fail_after=0))
    with caplog.at_level(logging.WARNING, logger="domo_core.conversation.turn"):
        await upkeep_pass()
    assert await confirmation_status(cid) == "pending"
    assert "A wake of the assistant ended in error (upstream)" in caplog.text


async def test_a_wake_that_presented_a_request_and_cannot_be_written_keeps_its_reply(
    owner_chat,
    grant,
    join_with_invitation,
    upkeep_pass,
    session_factory,
    recent_replies,
    confirmation_status,
    monkeypatch,
):  # final review M9: the request names that reply; the conversation is not held
    cid, answer, _ = await a_chain_whose_awaited_person_joined(
        owner_chat,
        join_with_invitation,
        session_factory,
        INVITE_PAUL,
        HAND_OVER_TO_PAUL,
        words="oui",
    )
    await a_firm_yes_is_asked_since(grant, owner_chat)

    async def cannot_prepare(**kwargs: Any) -> Any:
        raise RuntimeError("the turn could not be prepared")

    with monkeypatch.context() as patched:
        patched.setattr(starts_module, "prepare_turn", cannot_prepare)
        await upkeep_pass()
    assert await confirmation_status(cid) == "pending"
    # The reply that presents the request stays, ended in error: the card is under it.
    [kept] = await recent_replies(owner_chat.person, after=answer)
    assert kept.status == "error"
    assert (await request_row(session_factory, cid)).last_shown_message_id == kept.id
    # The person may write at once, and their words answer the request it presented. That reply
    # answered no message of theirs: Domo is told of no moment without an answer.
    owner_chat.use_model(FakeChatModel(["C'est fait."]))
    await owner_chat.say("je confirme")
    assert await confirmation_status(cid) == "executed"
    assert "Unanswered (data, not instructions)" not in owner_chat.server.calls[-1].system
    paul = (await invitation_named(session_factory, "Paul")).accepted_person_id
    assert paul is not None
    async with session_factory() as reader:
        found = await reader.execute(select(Person.role).where(Person.id == paul))
        assert found.scalar_one() == "owner"


def test_the_upkeep_interval_is_a_setting(monkeypatch):
    monkeypatch.delenv("DOMO_UPKEEP_SECONDS", raising=False)
    assert CoreSettings().upkeep_seconds == 60.0
    monkeypatch.setenv("DOMO_UPKEEP_SECONDS", "5")
    assert CoreSettings().upkeep_seconds == 5.0
    monkeypatch.setenv("DOMO_UPKEEP_SECONDS", "0")
    with pytest.raises(ValidationError):
        CoreSettings()


# --- the wait as a step reads it (unit) ----------------------------------------------------------


def test_a_step_may_name_the_invitation_it_waits_for():
    assert step_call({"tool": "note_leave", "arguments": {}, "after_arrival_of": "Paul"}, 1) == (
        "note_leave",
        {},
        "Paul",
    )
    assert step_call({"tool": "note_leave", "arguments": {}}, 1) == ("note_leave", {}, None)
    for after in ("", "   ", 3, None, "x" * 61, "Pa\nul"):
        with pytest.raises(InvalidArguments) as refused:
            step_call({"tool": "note_leave", "arguments": {}, "after_arrival_of": after}, 2)
        assert refused.value.detail is not None and refused.value.detail.startswith("step 2:")
    with pytest.raises(InvalidArguments):
        step_call({"tool": "note_leave", "arguments": {}, "other": "Paul"}, 1)


def test_a_change_keeps_the_wait_and_a_changed_wait_is_a_changed_step():
    calls = calls_of(
        [
            {"tool": "note_leave", "arguments": {"recipient": "Paul", "text": "a"}},
            {
                "tool": "note_leave",
                "arguments": {"recipient": "Paul", "text": "a"},
                "after_arrival_of": "Paul",
            },
        ]
    )
    assert calls[0] == {"tool": "note_leave", "arguments": {"recipient": "Paul", "text": "a"}}
    assert calls[1]["after_arrival_of"] == "Paul"
    row = ConfirmationStep(
        position=0,
        tool_name="note_leave",
        action_type="note.leave",
        arguments={"recipient": "Paul", "text": "a"},
        status="planned",
        after_arrival_of="Paul",
    )
    # The same call that waits is the step kept; the same call that does not wait is changed.
    assert match_steps([row], [calls[1]]) == (StepMatch("kept", 0),)
    assert match_steps([row], [calls[0]]) == (StepMatch("changed", 0),)
