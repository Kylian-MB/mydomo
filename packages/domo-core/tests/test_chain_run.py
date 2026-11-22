"""Chains of actions after the yes (spec 4.3, 4.4 and 8): the steps run one by one, in order, each
described again and put through the barrier in its turn, with the rule of the chain for the rights
(the role and the own rights the person had when they asked still count, as long as only the chain
changed them); the facts, and so the fixed refusals of the tools, are always those of the moment.
The first step that fails stops the chain, and what was done stays done: Domo may continue the
rest under the same yes (``chain_continue``) while the request lives.

The chat pilots talk with Domo on a live server (see ``chat_support.py``); a yes is also given
through the service itself, when what the model would read of it matters.
"""

import html
import json
import logging
import re
import uuid
from datetime import timedelta
from typing import Any

import anyio
import domo_core.confirmations.chain as chain_run
import pytest
from chat_support import ChatPilot, confirmation_cards, dump, status_of
from domo_common.i18n import t
from domo_core.accounts.models import Person
from domo_core.chat.agent import ActionRecord
from domo_core.confirmations.chain import STOPPED_AT_ONCE_DETAIL, STOPPED_DETAIL
from domo_core.confirmations.service import DecideOutcome, decide_confirmation, requester_context
from domo_core.confirmations.steps import mark_step
from domo_core.household.models import Invitation
from domo_core.household.service import disable
from domo_core.household.succession import list_successors
from domo_core.policy.actions import DescribedAction
from domo_core.policy.catalog import DEFAULT_POLICY
from domo_core.policy.roles import Actor, as_role
from domo_core.tools.context import ToolContext, ToolOutcome
from domo_core.tools.registry import TOOLS
from domo_core.web.deps import get_tool_registry
from fakes import FakeChatModel
from sqlalchemy import select, text, update
from test_spending import add_key
from web_support import audit_entries

TOOL = "do_in_order"
CONTINUE = "chain_continue"


def chain(*calls: tuple[str, dict[str, Any]]) -> dict[str, Any]:
    """The arguments of ``do_in_order`` for these calls, in this order."""
    return {"steps": [{"tool": name, "arguments": args} for name, args in calls]}


DEPARTURE = chain(
    ("household_transfer_ownership", {"person": "Alex"}),
    ("household_remove", {"person": "Owner"}),
)


def steps_of_rows(rows: list[Any]) -> list[tuple[int, str]]:
    return [(row.position, row.status) for row in rows]


# A "Show the link" button a chain left for the one who asked: the step it is for (from 1), and the
# invitation whose link it shows.
_LINK_BUTTON = re.compile(
    r'<div class="invitation-link" data-step="(\d+)"><button[^>]*data-action="show-link"[^>]*'
    r'hx-post="/invitations/([0-9a-f-]{36})/link"'
)
_REKEY_BUTTON = re.compile(r'<button[^>]*data-action="rekey"[^>]*\bhx-post="([^"]+)"')
INVITE_TOKEN = re.compile(r"/invite/([A-Za-z0-9_-]+)")


def link_buttons(fragment: str) -> list[tuple[int, str]]:
    """The "Show the link" buttons of the steps of a chain in a page or a card, in order: the step
    each one is for, and its invitation."""
    return [(int(step), invitation) for step, invitation in _LINK_BUTTON.findall(fragment)]


def rekey_buttons(fragment: str) -> list[str]:
    """Where the buttons that open a form for the model's key post, in a page or a card."""
    return [html.unescape(url) for url in _REKEY_BUTTON.findall(fragment)]


async def yes_through_the_service(
    pilot: ChatPilot, confirmation_id: uuid.UUID | None
) -> tuple[DecideOutcome, ActionRecord | None]:
    """The pilot's yes, as the button gives it, through the service itself: the outcome, and the
    record of what ran (what the model would read of it, had the yes been said in words)."""
    assert confirmation_id is not None
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
    return await decide_confirmation(
        session_factory=pilot.server.session_factory,
        confirmation_id=confirmation_id,
        decider=person,
        approve=True,
        via="button",
        reply_message_id=None,
        ctx_factory=requester_context(base),
        registry=TOOLS,
        policy=DEFAULT_POLICY,
        now=pilot.clock(),
    )


async def remove(session_factory: Any, person: Person, clock: Any) -> None:
    """Someone else removes ``person`` from the household (committed)."""
    async with session_factory() as writer:
        await disable(writer, person, clock(), by=person.id)
        await writer.commit()


async def bring_back(session_factory: Any, person: Person) -> None:
    """``person``, removed, is in the household again (committed)."""
    async with session_factory() as writer:
        await writer.execute(update(Person).where(Person.id == person.id).values(disabled_at=None))
        await writer.commit()


# --- the departure in one sentence (spec 10.1) ---------------------------------------------------


async def test_the_owner_leaves_in_one_sentence(
    owner_chat, adult_member, person_row, audit_actions, count_owners
):  # spec 10.1
    reply = await owner_chat.say_with_tool(TOOL, DEPARTURE)
    assert (await owner_chat.confirm_last(reply)).status == "executed"
    assert (await person_row(adult_member.person)).role == "owner"
    old = await person_row(owner_chat.person)
    assert (old.role, old.disabled_at is not None) == ("adult", True)
    actions = await audit_actions()
    assert actions.index("household.ownership_transferred") < actions.index("household.removed")
    assert await count_owners() == 1


async def test_the_owner_hands_the_role_over_then_leaves_with_household_leave(
    owner_chat, adult_member, person_row, audit_actions, count_owners, last_audit_entry
):  # spec 5.9, 8.4: the owner never leaves, but the first step makes them an adult
    reply = await owner_chat.say_with_tool(
        TOOL,
        chain(("household_transfer_ownership", {"person": "Alex"}), ("household_leave", {})),
    )
    assert reply.actions == [{"tool": TOOL, "status": "waiting"}]
    assert (await owner_chat.confirm_last(reply)).status == "executed"
    assert (await person_row(adult_member.person)).role == "owner"
    old = await person_row(owner_chat.person)
    assert (old.role, old.disabled_at is not None) == ("adult", True)
    actions = await audit_actions()
    assert actions.index("household.ownership_transferred") < actions.index("household.left")
    left = await last_audit_entry("household.left")
    assert left.actor == f"person:{owner_chat.person.id}" and left.details["role"] == "adult"
    assert await count_owners() == 1


async def test_the_owner_makes_someone_an_adult_hands_the_role_over_and_leaves_in_one_chain(
    owner_chat, child_member, person_row, count_owners, audit_actions, confirmation_status
):  # spec 8.4
    reply = await owner_chat.say_with_tool(
        TOOL,
        chain(
            ("household_change_role", {"person": "Sam", "role": "adult"}),
            ("household_transfer_ownership", {"person": "Sam"}),
            ("household_remove", {"person": "Owner"}),
        ),
        judge="explicit_yes: a big step",
    )
    cid = reply.pending_confirmation_id
    # The judge asks a firm yes of this chain (nothing asks one by default, 2026-11-15): the
    # button or "I confirm"; a plain yes is not enough.
    owner_chat.use_model(FakeChatModel(["Il faut le bouton, ou « je confirme »."]))
    await owner_chat.say("oui")
    assert await confirmation_status(cid) == "pending"
    assert (await person_row(child_member.person)).role == "child"
    owner_chat.use_model(FakeChatModel(["C'est fait."]))
    await owner_chat.say("je confirme")
    assert await confirmation_status(cid) == "executed"
    assert (await person_row(child_member.person)).role == "owner"
    old = await person_row(owner_chat.person)
    assert (old.role, old.disabled_at is not None) == ("adult", True)
    actions = await audit_actions()
    assert (
        actions.index("household.role_changed")
        < actions.index("household.ownership_transferred")
        < actions.index("household.removed")
    )
    assert await count_owners() == 1


async def test_cancelling_and_inviting_again_in_one_chain_makes_the_new_invitation(
    owner_chat, invite_for_test, count_invitations, session_factory, confirmation_status
):
    old = await invite_for_test("Dana", "guest")
    reply = await owner_chat.say_with_tool(
        TOOL,
        chain(
            ("household_cancel_invitation", {"display_name": "Dana"}),
            ("household_invite", {"display_name": "Dana", "role": "adult"}),
        ),
    )
    assert (await owner_chat.confirm_last(reply)).status == "executed"
    assert await confirmation_status(reply.pending_confirmation_id) == "executed"
    assert await count_invitations() == 2
    async with session_factory() as reader:
        found = await reader.execute(
            select(Invitation).where(Invitation.display_name == "Dana").order_by(Invitation.id)
        )
        invitations = {i.id: i for i in found.scalars()}
    assert invitations[old.id].cancelled_at is not None
    (new,) = [i for i in invitations.values() if i.cancelled_at is None]
    assert new.id != old.id and new.role == "adult" and new.accepted_at is None


async def test_a_chain_of_free_steps_runs_at_once_in_order(
    adult_chat, owner_member, child_member, stored_notes, count_confirmations, session_factory
):
    reply = await adult_chat.say_with_tool(
        TOOL,
        chain(
            ("note_leave", {"recipient": "Owner", "text": "a"}),
            ("note_leave", {"recipient": "Sam", "text": "b"}),
        ),
        judge="do",
    )
    assert reply.actions == [{"tool": TOOL, "status": "done"}]
    assert sorted(n.text for n in await stored_notes()) == ["a", "b"]
    # In order: the audit log keeps the order in which they were left.
    async with session_factory() as reader:
        left_for = [
            entry.details["recipient"]
            for entry in await audit_entries(reader)
            if entry.action == "note.left"
        ]
    assert left_for == [str(owner_member.person.id), str(child_member.person.id)]
    assert await count_confirmations() == 0
    result = reply.model_results[-1]
    assert result["status"] == "done"
    assert [(step["step"], step["status"]) for step in result["steps"]] == [
        (1, "done"),
        (2, "done"),
    ]
    assert t("action.card.done.chain", "en", done="2", total="2") in reply.cards_html


async def test_someone_elses_yes_leaves_the_link_of_the_invitation_to_the_one_who_asked(
    adult_chat, owner_chat, grant
):
    await grant("household.invite", person=adult_chat.person, access="allow", approver="owner")
    reply = await adult_chat.say_with_tool(
        TOOL,
        chain(
            ("household_invite", {"display_name": "Jo", "role": "guest"}),
            ("note_leave", {"recipient": "Owner", "text": "Jo arrive."}),
        ),
    )
    cid = reply.pending_confirmation_id
    answer = await owner_chat.post(f"/confirmations/{cid}/approve")
    assert status_of(answer) == "executed" and "/invite/" not in answer.text
    # The button that shows it is on the card of the one who asked, and theirs only, under the
    # number of the step that made it.
    assert 'data-action="show-link"' not in answer.text
    assert [step for step, _ in link_buttons((await adult_chat.get("/chat")).text)] == [1]


# --- the links and the forms a chain makes (spec 4.5) --------------------------------------------


async def test_every_link_a_chain_makes_waits_behind_its_own_button_for_the_one_who_asked(
    owner_chat, adult_member, chat_server, session_factory, stored_text, count_invitations
):  # spec 8.6
    await remove(session_factory, adult_member.person, owner_chat.clock)
    reply = await owner_chat.say_with_tool(
        TOOL,
        chain(
            ("household_bring_back", {"person": "Alex", "role": "adult"}),
            ("household_invite", {"display_name": "Léa", "role": "adult"}),
        ),
    )
    card = await owner_chat.confirm_last(reply)
    # No link is shown at once in a chain, even to the one who asked: each step leaves its own
    # behind a button, numbered.
    assert card.status == "executed" and card.invitation_token is None
    assert await count_invitations() == 2
    buttons = link_buttons(card.html)
    assert [step for step, _ in buttons] == [1, 2]
    tokens: list[str] = []
    for _, invitation in buttons:
        shown = await owner_chat.post(f"/invitations/{invitation}/link")
        token = INVITE_TOKEN.search(shown.text)
        assert shown.status_code == 200 and token is not None
        tokens.append(token.group(1))
    assert len(set(tokens)) == 2
    # A later turn: the model is shown the history, with what came of the chain.
    await owner_chat.say("merci")
    told = json.dumps(
        [[call.system, [dump(m) for m in call.messages]] for call in chat_server.calls],
        ensure_ascii=False,
    )
    kept = await stored_text()
    for token in tokens:
        assert token not in told and token not in kept


async def test_the_buttons_of_a_chain_decided_in_words_are_on_one_card_of_the_page(
    owner_chat, adult_member, session_factory, confirmation_status
):  # final review M1: a button pressed twice answers 410 the second time
    await remove(session_factory, adult_member.person, owner_chat.clock)
    reply = await owner_chat.say_with_tool(
        TOOL,
        chain(
            ("household_bring_back", {"person": "Alex", "role": "adult"}),
            ("household_invite", {"display_name": "Léa", "role": "adult"}),
        ),
    )
    cid = reply.pending_confirmation_id
    owner_chat.use_model(FakeChatModel(["C'est fait : chaque lien est derrière son bouton."]))
    answer = await owner_chat.say("je confirme")
    assert await confirmation_status(cid) == "executed"
    # The card that answers the words shows them, once.
    assert [step for step, _ in link_buttons(answer.cards_html)] == [1, 2]
    # The page shows the chain's own card (with them) and the card of the reply that answered
    # the words (without them): each button once.
    page = (await owner_chat.get("/chat")).text
    assert [step for step, _ in link_buttons(page)] == [1, 2]


async def test_the_buttons_of_a_chain_are_the_requesters_only(
    adult_chat, owner_chat, grant, steps_rows
):
    alex = adult_chat.person
    await grant("household.invite", person=alex, access="allow")
    reply = await adult_chat.say_with_tool(
        TOOL,
        chain(
            ("household_invite", {"display_name": "Jo", "role": "guest"}),
            ("household_invite", {"display_name": "Kim", "role": "guest"}),
            ("note_leave", {"recipient": "Owner", "text": "Jo et Kim arrivent."}),
        ),
    )
    cid = reply.pending_confirmation_id
    # The owner now wants to say yes to Alex's notes: the chain waits for the owner once both
    # invitations are made.
    await grant("note.leave", person=alex, approver="owner")
    assert (await adult_chat.confirm_last(reply)).status == "approver_changed"
    assert steps_of_rows(await steps_rows(cid)) == [(0, "done"), (1, "done"), (2, "planned")]
    # The owner sees the chain in the approvals, with its steps done, but not their buttons.
    approvals = (await owner_chat.get("/approvals")).text
    assert str(cid) in approvals and 'data-action="show-link"' not in approvals
    answer = await owner_chat.post(f"/confirmations/{cid}/approve")
    assert status_of(answer) == "executed" and 'data-action="show-link"' not in answer.text
    assert [step for step, _ in link_buttons((await adult_chat.get("/chat")).text)] == [1, 2]


async def test_the_key_form_of_a_chain_opens_from_its_button_for_the_one_who_asked(
    owner_chat, adult_chat, gk_mock, confirmation_status
):
    reply = await owner_chat.say_with_tool(
        TOOL,
        chain(
            ("policy_update", {"key": "confirmation_max_lifetime_hours", "value": 48}),
            ("model_rekey", {}),
        ),
    )
    cid = reply.pending_confirmation_id
    owner_chat.use_model(FakeChatModel(["C'est fait : le formulaire est derrière son bouton."]))
    answer = await owner_chat.say("je confirme")
    assert await confirmation_status(cid) == "executed"
    # No form is opened at once in a chain: its button, which names the chain, opens it.
    url = f"/chat/key/{owner_chat.server.provider_id}/form?confirmation={cid}"
    assert rekey_buttons(answer.cards_html) == [url]
    assert "<iframe" not in answer.cards_html and not gk_mock.secret_requests.called
    # On the page, the button is on one card only: the chain's own (final review M1).
    assert rekey_buttons((await owner_chat.get("/chat")).text) == [url]
    form = await owner_chat.post(url)
    assert form.status_code == 200 and 'src="/vault/r/token-req1"' in form.text
    assert (await adult_chat.post(url)).status_code == 403


async def test_a_free_chain_leaves_its_links_behind_buttons_too(
    owner_chat, grant, count_invitations, count_confirmations
):
    reply = await owner_chat.say_with_tool(
        TOOL,
        chain(
            ("household_invite", {"display_name": "Jo", "role": "guest"}),
            ("household_invite", {"display_name": "Kim", "role": "guest"}),
        ),
        judge="do",
    )
    # It ran at once, with no request: its card, stored with the reply, holds the buttons.
    assert reply.actions == [{"tool": TOOL, "status": "done"}]
    assert await count_confirmations() == 0 and await count_invitations() == 2
    assert "/invite/" not in reply.cards_html
    assert [step for step, _ in link_buttons(reply.cards_html)] == [1, 2]
    assert [step["result"]["link"] for step in reply.model_results[-1]["steps"]] == [
        "waits_for_them",
        "waits_for_them",
    ]
    buttons = link_buttons((await owner_chat.get("/chat")).text)
    assert [step for step, _ in buttons] == [1, 2]
    shown = await owner_chat.post(f"/invitations/{buttons[1][1]}/link")
    assert shown.status_code == 200 and "/invite/" in shown.text


# --- the rule of the chain, and where it stops (spec 4.3, 8.3) -----------------------------------


async def test_a_chain_cannot_hand_the_role_over_twice(
    owner_chat, adult_member, add_member, steps_rows, person_row
):  # spec 10.1, variant
    bea = await add_member("Bea", "adult")
    reply = await owner_chat.say_with_tool(
        TOOL,
        chain(
            ("household_transfer_ownership", {"person": "Alex"}),
            ("household_transfer_ownership", {"person": "Bea"}),
        ),
    )
    assert (await owner_chat.confirm_last(reply)).status == "failed"
    assert (await person_row(adult_member.person)).role == "owner"
    assert (await person_row(bea)).role == "adult"
    rows = await steps_rows(reply.pending_confirmation_id)
    assert steps_of_rows(rows) == [(0, "done"), (1, "failed")]
    assert rows[1].result is not None and rows[1].result["reason"] == "no_longer_possible"


async def test_a_right_someone_else_took_away_before_the_yes_applies(
    adult_chat, owner_member, grant, revoke_right, steps_rows, count_invitations, count_notes
):  # spec 10.3
    right = await grant("household.invite", person=adult_chat.person, access="allow")
    reply = await adult_chat.say_with_tool(
        TOOL,
        chain(
            ("household_invite", {"display_name": "Jo", "role": "adult"}),
            ("note_leave", {"recipient": "Owner", "text": "Jo arrive."}),
        ),
    )
    assert reply.model_results[-1]["status"] == "waiting_for_confirmation"
    await revoke_right(right)
    # An adult invites by default: taking it away is saying no.
    await grant("household.invite", person=adult_chat.person, access="deny")
    assert (await adult_chat.confirm_last(reply)).status == "failed"
    rows = await steps_rows(reply.pending_confirmation_id)
    assert steps_of_rows(rows) == [(0, "failed"), (1, "stopped")]
    assert rows[0].result is not None and rows[0].result["reason"] == "not_allowed"
    assert await count_invitations() == 0 and await count_notes() == 0


async def test_the_chains_rule_does_not_reach_past_the_fixed_refusals(
    owner_chat, adult_member, person_row, steps_rows
):  # spec 10.4
    reply = await owner_chat.say_with_tool(
        TOOL,
        chain(
            ("household_transfer_ownership", {"person": "Alex"}),
            ("household_change_role", {"person": "Alex", "role": "adult"}),
        ),
    )
    rows = await steps_rows(reply.pending_confirmation_id)
    # Alex is an adult when the chain is asked for: the second step is checked in its turn.
    assert rows[1].action is None
    assert (await owner_chat.confirm_last(reply)).status == "failed"
    rows = await steps_rows(reply.pending_confirmation_id)
    assert steps_of_rows(rows) == [(0, "done"), (1, "failed")]
    assert rows[1].result is not None and rows[1].result["reason"] == "no_longer_possible"
    # The role of the owner never changes, whatever the chain remembers.
    assert (await person_row(adult_member.person)).role == "owner"


async def test_what_the_owner_asked_as_owner_still_passes_after_the_transfer(
    owner_chat, adult_member, current_settings, session_factory, live_clock
):  # the owner's choice (a), 1 November
    reply = await owner_chat.say_with_tool(
        TOOL,
        chain(
            ("household_transfer_ownership", {"person": "Alex"}),
            ("policy_update", {"key": "confirmation_max_lifetime_hours", "value": 48}),
            ("successors_set", {"successors": [{"name": "Paul", "relation": "my brother"}]}),
        ),
    )
    assert (await owner_chat.confirm_last(reply)).status == "executed"
    assert (await current_settings()).max_lifetime == timedelta(hours=48)
    async with session_factory() as reader:
        listed = await list_successors(reader, live_clock())
    assert [place.name for place in listed] == ["Paul"]


async def test_the_decision_of_each_step_is_audited_for_the_role_it_was_judged_with(
    owner_chat, adult_member, session_factory
):
    reply = await owner_chat.say_with_tool(TOOL, DEPARTURE)
    assert (await owner_chat.confirm_last(reply)).status == "executed"
    async with session_factory() as reader:
        entries = [
            entry
            for entry in await audit_entries(reader)
            if entry.action == "policy.decision" and entry.details["tool"] != TOOL
        ]
    assert [entry.details["tool"] for entry in entries] == [
        "household_transfer_ownership",
        "household_remove",
    ]
    # Removed as an adult, the old owner would have been refused: the step was judged for the
    # owner they were when they asked.
    for entry in entries:
        assert entry.actor == f"person:{owner_chat.person.id}"
        assert (entry.details["decision"], entry.details["reason"]) == ("allow", None)


# --- a step that fails, and the rest (spec 4.4) --------------------------------------------------


async def stopped_chain(owner_chat: ChatPilot, adult: Person) -> tuple[uuid.UUID, Any]:
    """The owner's chain [Sam an adult, remove Alex, a note for Sam], Alex removed by someone
    else between the request and the yes, then the owner's yes: the request, and the record of
    what ran."""
    reply = await owner_chat.say_with_tool(
        TOOL,
        chain(
            ("household_change_role", {"person": "Sam", "role": "adult"}),
            ("household_remove", {"person": "Alex"}),
            ("note_leave", {"recipient": "Sam", "text": "x"}),
        ),
    )
    cid = reply.pending_confirmation_id
    assert cid is not None
    await remove(owner_chat.server.session_factory, adult, owner_chat.clock)
    outcome, record = await yes_through_the_service(owner_chat, cid)
    assert outcome == "failed"
    return cid, record


async def test_a_step_that_fails_stops_the_chain_and_what_was_done_stays(
    owner_chat, child_member, adult_member, person_row, steps_rows, stored_notes, last_audit_entry
):
    cid, record = await stopped_chain(owner_chat, adult_member.person)
    assert (await person_row(child_member.person)).role == "adult"
    assert steps_of_rows(await steps_rows(cid)) == [(0, "done"), (1, "failed"), (2, "stopped")]
    assert await stored_notes() == []
    assert record is not None and record.status == "failed"
    result = record.model_result
    assert (result["stopped_at"], result["reason"], result["left"]) == (2, "no_longer_possible", 1)
    assert result["detail"] == STOPPED_DETAIL
    assert [(step["step"], step["status"]) for step in result["steps"]] == [
        (1, "done"),
        (2, "failed"),
    ]
    assert record.card["key"] == "action.card.failed.chain"
    assert record.card["params"] == {"done": "1", "total": "3"}
    entry = await last_audit_entry("chain.stopped")
    assert entry.details == {"confirmation_id": str(cid), "step": 2, "reason": "no_longer_possible"}


async def test_domo_continues_a_stopped_chain_under_the_same_yes(
    owner_chat,
    child_chat,
    adult_member,
    steps_rows,
    stored_notes,
    confirmation_status,
    last_audit_entry,
    person_row,
):
    cid, _ = await stopped_chain(owner_chat, adult_member.person)
    # Nobody else continues it.
    other = await child_chat.say_with_tool(CONTINUE, {"request_id": str(cid)})
    assert other.actions == [{"tool": CONTINUE, "status": "invalid", "reason": "not_found"}]

    # Alex is back: the step that failed (it could no longer be done, so it did nothing) runs
    # again first, under the same yes, then the step after it.
    await bring_back(owner_chat.server.session_factory, adult_member.person)
    reply = await owner_chat.say_with_tool(CONTINUE, {"request_id": str(cid)}, judge="do")
    # Audited before the rest runs: who continued which request, and how many steps were left.
    entry = await last_audit_entry("chain.continued")
    assert entry.actor == f"person:{owner_chat.person.id}"
    assert entry.details == {"confirmation_id": str(cid), "steps": 2}
    assert await confirmation_status(cid) == "executed"
    assert [note.text for note in await stored_notes()] == ["x"]
    assert steps_of_rows(await steps_rows(cid)) == [(0, "done"), (1, "done"), (2, "done")]
    assert (await person_row(adult_member.person)).disabled_at is not None
    # The card shown is the request's.
    assert reply.pending_confirmation_id == cid
    result = reply.model_results[-1]
    assert result["status"] == "done"
    assert [(step["step"], step["status"]) for step in result["steps"]] == [
        (2, "done"),
        (3, "done"),
    ]

    again = await owner_chat.say_with_tool(CONTINUE, {"request_id": str(cid)})
    assert again.actions == [{"tool": CONTINUE, "status": "invalid", "reason": "not_found"}]
    assert len(await stored_notes()) == 1


async def test_a_chain_with_a_step_whose_record_is_not_known_is_never_continued(
    owner_chat, child_member, adult_member, session_factory, steps_rows, stored_notes
):
    cid, _ = await stopped_chain(owner_chat, adult_member.person)
    # The first step ran, but say its record was lost: it is still marked as it was before.
    async with session_factory() as writer:
        await mark_step(writer, cid, 0, status="planned", result=None, now=owner_chat.clock())
        await writer.commit()
    reply = await owner_chat.say_with_tool(CONTINUE, {"request_id": str(cid)})
    assert reply.actions == [{"tool": CONTINUE, "status": "invalid", "reason": "not_found"}]
    # Nothing ran: no step runs twice.
    assert await stored_notes() == []
    assert steps_of_rows(await steps_rows(cid)) == [(0, "planned"), (1, "failed"), (2, "stopped")]


async def test_a_step_whose_record_fails_ends_the_run_and_never_runs_again(
    owner_chat,
    child_member,
    adult_member,
    monkeypatch,
    caplog,
    person_row,
    steps_rows,
    stored_notes,
    confirmation_status,
):
    reply = await owner_chat.say_with_tool(
        TOOL,
        chain(
            ("household_change_role", {"person": "Sam", "role": "adult"}),
            ("note_leave", {"recipient": "Sam", "text": "x"}),
        ),
    )
    cid = reply.pending_confirmation_id
    assert cid is not None

    async def lost(*args: Any, **kwargs: Any) -> None:
        """``mark_step``, whose record of a step done fails (with words that are never logged)."""
        if kwargs.get("status") == "done":
            raise RuntimeError("words-that-are-never-logged")
        await mark_step(*args, **kwargs)

    monkeypatch.setattr(chain_run, "mark_step", lost)
    with caplog.at_level(logging.ERROR):
        assert (await owner_chat.confirm_last(reply)).status == "failed"
    monkeypatch.undo()
    # The step ran; its record is not known, so it stays as it was, and the run ended there.
    assert (await person_row(child_member.person)).role == "adult"
    assert steps_of_rows(await steps_rows(cid)) == [(0, "planned"), (1, "planned")]
    assert await stored_notes() == []
    assert await confirmation_status(cid) == "failed"
    assert (
        "Step 1 of a chain (household.change_role) was done, but not recorded (RuntimeError)"
        in caplog.text
    )
    assert "words-that-are-never-logged" not in caplog.text
    # Never continued: what ran is not run twice.
    again = await owner_chat.say_with_tool(CONTINUE, {"request_id": str(cid)})
    assert again.actions == [{"tool": CONTINUE, "status": "invalid", "reason": "not_found"}]
    assert await stored_notes() == []


async def test_a_hand_over_checked_in_its_turn_still_counts_when_the_chain_is_continued(
    owner_chat,
    adult_member,
    child_member,
    steps_rows,
    person_row,
    current_settings,
    confirmation_status,
):
    # Sam is a child when the chain is asked for: handing Sam the role is checked in its turn.
    # The owner asked for the last step as owner, and only the chain took that role.
    reply = await owner_chat.say_with_tool(
        TOOL,
        chain(
            ("household_change_role", {"person": "Sam", "role": "adult"}),
            ("household_transfer_ownership", {"person": "Sam"}),
            ("household_remove", {"person": "Alex"}),
            ("policy_update", {"key": "confirmation_max_lifetime_hours", "value": 48}),
        ),
    )
    cid = reply.pending_confirmation_id
    assert cid is not None
    rows = await steps_rows(cid)
    assert [row.action is None for row in rows] == [False, True, False, False]
    # Alex is removed by someone else before the yes: the third step stops the chain.
    await remove(owner_chat.server.session_factory, adult_member.person, owner_chat.clock)
    outcome, _ = await yes_through_the_service(owner_chat, cid)
    assert outcome == "failed"
    assert (await person_row(child_member.person)).role == "owner"
    rows = await steps_rows(cid)
    assert steps_of_rows(rows) == [(0, "done"), (1, "done"), (2, "failed"), (3, "stopped")]
    # The row of the hand-over keeps the action it was described as in its turn.
    assert rows[1].action is not None
    handed = DescribedAction.from_json(rows[1].action)
    assert (handed.action_type, handed.target) == (
        "household.transfer_ownership",
        f"person:{child_member.person.id}",
    )
    assert handed.facts["from"] == f"person:{owner_chat.person.id}"
    # Alex is back, and the chain is continued from the step that failed: the last two steps are
    # judged with the role the old owner had when they asked.
    await bring_back(owner_chat.server.session_factory, adult_member.person)
    await owner_chat.say_with_tool(CONTINUE, {"request_id": str(cid)}, judge="do")
    assert await confirmation_status(cid) == "executed"
    assert steps_of_rows(await steps_rows(cid))[2:] == [(2, "done"), (3, "done")]
    assert (await current_settings()).max_lifetime == timedelta(hours=48)


async def test_a_stopped_chain_expires_with_its_request(
    owner_chat, child_member, adult_member, set_setting, clock, stored_notes
):
    cid, _ = await stopped_chain(owner_chat, adult_member.person)
    await set_setting("confirmation_max_lifetime_hours", 1)
    clock.advance(timedelta(hours=2))
    reply = await owner_chat.say_with_tool(CONTINUE, {"request_id": str(cid)})
    assert reply.actions == [{"tool": CONTINUE, "status": "invalid", "reason": "not_found"}]
    assert await stored_notes() == []


# --- whose yes (spec 4.3.4, Ruling R27) ----------------------------------------------------------


async def test_a_chain_waits_for_a_stricter_yes_when_a_right_changed_in_between(
    adult_chat,
    owner_chat,
    grant,
    revoke_right,
    steps_rows,
    count_notes,
    count_invitations,
    session_factory,
):
    alex = adult_chat.person
    right = await grant("household.invite", person=alex, access="allow")
    reply = await adult_chat.say_with_tool(
        TOOL,
        chain(
            ("note_leave", {"recipient": "Owner", "text": "Jo arrive."}),
            ("household_invite", {"display_name": "Jo", "role": "guest"}),
        ),
    )
    assert reply.model_results[-1]["by"] == "you"
    cid = reply.pending_confirmation_id
    # The owner now wants to say yes to Alex's invitations.
    await revoke_right(right)
    await grant("household.invite", person=alex, access="allow", approver="owner")
    assert (await adult_chat.confirm_last(reply)).status == "approver_changed"
    assert steps_of_rows(await steps_rows(cid)) == [(0, "done"), (1, "planned")]
    async with session_factory() as reader:
        found = await reader.execute(
            text("SELECT status, approver_kind FROM confirmations WHERE id = :id"), {"id": cid}
        )
        assert tuple(found.one()) == ("pending", "owner")
    assert await count_notes() == 1 and await count_invitations() == 0
    # Alex's button no longer decides it; the owner's does, and the rest runs.
    assert status_of(await adult_chat.post(f"/confirmations/{cid}/approve")) == "not_yours"
    assert status_of(await owner_chat.post(f"/confirmations/{cid}/approve")) == "executed"
    assert steps_of_rows(await steps_rows(cid)) == [(0, "done"), (1, "done")]
    assert await count_notes() == 1 and await count_invitations() == 1


async def test_a_chain_that_waits_again_after_its_invitation_leaves_the_link_behind_its_button(
    adult_chat, owner_chat, grant, steps_rows, count_invitations, count_notes
):
    alex = adult_chat.person
    await grant("household.invite", person=alex, access="allow")
    reply = await adult_chat.say_with_tool(
        TOOL,
        chain(
            ("household_invite", {"display_name": "Jo", "role": "guest"}),
            ("note_leave", {"recipient": "Owner", "text": "Jo arrive."}),
        ),
    )
    cid = reply.pending_confirmation_id
    # The owner now wants to say yes to Alex's notes: the chain waits for the owner after the
    # invitation, which Alex's yes made.
    await grant("note.leave", person=alex, approver="owner")
    card = await adult_chat.confirm_last(reply)
    assert card.status == "approver_changed"
    assert steps_of_rows(await steps_rows(cid)) == [(0, "done"), (1, "planned")]
    assert await count_invitations() == 1 and await count_notes() == 0
    # The link waits behind the button of its step, on Alex's card: it is never shown at once.
    assert card.invitation_token is None
    assert [step for step, _ in link_buttons(card.html)] == [1]
    # The owner's yes runs the rest; the owner is shown no link and no button.
    answer = await owner_chat.post(f"/confirmations/{cid}/approve")
    assert status_of(answer) == "executed" and "/invite/" not in answer.text
    assert 'data-action="show-link"' not in answer.text
    assert await count_invitations() == 1 and await count_notes() == 1


async def test_a_chain_that_waits_again_or_is_refused_keeps_the_buttons_of_the_steps_done(
    child_chat, adult_chat, owner_chat, grant, steps_rows, count_invitations, confirmation_status
):
    sam = child_chat.person
    await grant("household.invite", person=sam, access="allow")
    reply = await child_chat.say_with_tool(
        TOOL,
        chain(
            ("household_invite", {"display_name": "Jo", "role": "guest"}),
            ("note_leave", {"recipient": "Owner", "text": "Jo arrive."}),
        ),
    )
    cid = reply.pending_confirmation_id
    assert reply.model_results[-1]["by"] == "an adult"
    # The owner now wants to say yes to Sam's notes: Alex's yes makes the invitation (its link
    # is Sam's, behind a button on Sam's card), then the chain waits for the owner.
    await grant("note.leave", person=sam, approver="owner")
    answer = await adult_chat.post(f"/confirmations/{cid}/approve")
    assert status_of(answer) == "approver_changed"
    assert "/invite/" not in answer.text and 'data-action="show-link"' not in answer.text
    assert steps_of_rows(await steps_rows(cid)) == [(0, "done"), (1, "planned")]
    assert await count_invitations() == 1
    # While it waits for the owner, the button of the step done is on Sam's card, and Sam's only.
    assert [step for step, _ in link_buttons((await child_chat.get("/chat")).text)] == [1]
    # The owner says no: what was done stays done, and so does its button, which still works. The
    # card says that the rest was refused, not that nothing was done.
    answer = await owner_chat.post(f"/confirmations/{cid}/refuse")
    assert status_of(answer) == "refused" and 'data-action="show-link"' not in answer.text
    assert await confirmation_status(cid) == "refused"
    page = (await child_chat.get("/chat")).text
    assert t("confirmation.card.refused.rest", "en") in page
    assert t("confirmation.card.refused", "en") not in page
    ((step, invitation),) = link_buttons(page)
    assert step == 1
    shown = await child_chat.post(f"/invitations/{invitation}/link")
    assert shown.status_code == 200 and "/invite/" in shown.text


async def test_the_key_button_of_a_chain_that_waits_again_still_opens_the_form(
    adult_chat, owner_chat, grant, gk_mock, steps_rows, session_factory
):
    alex = adult_chat.person
    await grant("model.rekey", person=alex, access="allow")
    # Only the one who gave a key gives it again: Alex's own key (0.6.0).
    alex_key = (await add_key(session_factory, alex.id)).id
    reply = await adult_chat.say_with_tool(
        TOOL,
        chain(
            ("model_rekey", {}),
            ("note_leave", {"recipient": "Owner", "text": "La clé est changée."}),
        ),
    )
    cid = reply.pending_confirmation_id
    # The owner now wants to say yes to Alex's notes: the chain waits for the owner after the form
    # is left on Alex's card, and the yes Alex gave is no longer kept on the request.
    await grant("note.leave", person=alex, approver="owner")
    card = await adult_chat.confirm_last(reply)
    assert card.status == "approver_changed"
    assert steps_of_rows(await steps_rows(cid)) == [(0, "done"), (1, "planned")]
    async with session_factory() as reader:
        found = await reader.execute(
            text("SELECT decided_by, decided_at FROM confirmations WHERE id = :id"), {"id": cid}
        )
        assert tuple(found.one()) == (None, None)
    url = f"/chat/key/{alex_key}/form?confirmation={cid}"
    assert rekey_buttons(card.html) == [url] and not gk_mock.secret_requests.called
    form = await adult_chat.post(url)
    assert form.status_code == 200 and 'src="/vault/r/token-req1"' in form.text
    # It is Alex's form: the owner cannot open it under Alex's chain.
    assert (await owner_chat.post(url)).status_code == 403


@pytest.mark.parametrize("saved_under", ["old", "new"])
async def test_a_key_saved_under_a_chain_spends_the_requests_a_replacement_links_it_to(
    adult_chat,
    owner_member,
    child_member,
    grant,
    gk_mock,
    session_factory,
    confirmation_status,
    saved_under,
):  # Ruling R22: one form per yes
    alex = adult_chat.person
    await grant("model.rekey", person=alex, access="allow")
    alex_key = (await add_key(session_factory, alex.id)).id
    reply = await adult_chat.say_with_tool(
        TOOL,
        chain(
            ("model_rekey", {}),
            ("note_leave", {"recipient": "Sam", "text": "La clé est changée."}),
        ),
    )
    old = reply.pending_confirmation_id
    assert old is not None
    # Sam leaves before the yes: the form is left on Alex's card, then the note stops the chain.
    await remove(session_factory, child_member.person, adult_chat.clock)
    assert (await adult_chat.confirm_last(reply)).status == "failed"
    # Taken up again with a change: a new request replaces the chain, its step done copied.
    note = {"recipient": "Owner", "text": "La clé est changée."}
    taken = await adult_chat.say_with_tool(
        CONTINUE,
        {"request_id": str(old), "steps": [{"tool": "note_leave", "arguments": note}]},
    )
    new = uuid.UUID(taken.model_results[-1]["request_id"])
    assert await confirmation_status(old) == "replaced"
    # Each card keeps the button of the step done, under its own request; the old one says that a
    # new request replaced the rest.
    urls = {
        "old": f"/chat/key/{alex_key}/form?confirmation={old}",
        "new": f"/chat/key/{alex_key}/form?confirmation={new}",
    }
    page = (await adult_chat.get("/chat")).text
    assert sorted(set(rekey_buttons(page))) == sorted(urls.values())
    (replaced,) = [c for c in confirmation_cards(page) if f'data-confirmation-id="{old}"' in c]
    assert t("confirmation.card.replaced.rest", "en") in replaced
    assert t("confirmation.card.replaced", "en") not in replaced
    # A key saved under one of them spends the yes of both: one form per yes.
    other = "new" if saved_under == "old" else "old"
    assert (await adult_chat.post(urls[saved_under])).status_code == 200
    gk_mock.set_status("req1", "fulfilled")
    assert (await adult_chat.post(urls[other])).status_code == 403
    assert (await adult_chat.post(urls[saved_under])).status_code == 403
    assert len(gk_mock.refs) == 1  # no other form was asked of the vault


async def test_a_chain_that_ends_with_steps_done_says_only_the_rest_was_not_done(
    adult_chat, owner_member, grant, set_setting, clock
):
    alex = adult_chat.person
    await grant("household.invite", person=alex, access="allow")
    # Nothing done yet: refused, it says that nothing was done, as any request does.
    untouched = await adult_chat.say_with_tool(
        TOOL,
        chain(
            ("household_invite", {"display_name": "Kim", "role": "guest"}),
            ("note_leave", {"recipient": "Owner", "text": "Kim arrive."}),
        ),
    )
    answer = await adult_chat.post(f"/confirmations/{untouched.pending_confirmation_id}/refuse")
    assert status_of(answer) == "refused"
    assert t("confirmation.card.refused", "en") in answer.text
    assert t("confirmation.card.refused.rest", "en") not in answer.text
    # Its first step done, the chain waits for the owner, then expires: only its rest was not done.
    reply = await adult_chat.say_with_tool(
        TOOL,
        chain(
            ("household_invite", {"display_name": "Jo", "role": "guest"}),
            ("note_leave", {"recipient": "Owner", "text": "Jo arrive."}),
        ),
    )
    cid = reply.pending_confirmation_id
    await grant("note.leave", person=alex, approver="owner")
    assert (await adult_chat.confirm_last(reply)).status == "approver_changed"
    await set_setting("confirmation_max_lifetime_hours", 1)
    clock.advance(timedelta(hours=2))
    page = (await adult_chat.get("/chat")).text
    (card,) = [c for c in confirmation_cards(page) if f'data-confirmation-id="{cid}"' in c]
    assert 'data-confirmation-status="expired"' in card
    assert t("confirmation.card.expired.rest", "en") in card
    assert t("confirmation.card.expired", "en") not in card
    assert [step for step, _ in link_buttons(card)] == [1]


async def test_the_yes_of_the_most_demanding_approver_covers_every_step(
    adult_chat, owner_chat, grant, count_notes, count_invitations
):
    alex = adult_chat.person
    # Alex's notes wait for Alex's own yes, Alex's invitations for the owner's: the chain waits
    # for the owner, whose one yes covers both steps.
    await grant("household.invite", person=alex, access="allow", approver="owner")
    reply = await adult_chat.say_with_tool(
        TOOL,
        chain(
            ("note_leave", {"recipient": "Owner", "text": "Jo arrive."}),
            ("household_invite", {"display_name": "Jo", "role": "guest"}),
        ),
    )
    cid = reply.pending_confirmation_id
    assert status_of(await owner_chat.post(f"/confirmations/{cid}/approve")) == "executed"
    assert await count_notes() == 1 and await count_invitations() == 1


async def test_a_right_on_chains_given_after_the_request_applies_at_the_yes(
    adult_chat, owner_member, grant, steps_rows, count_notes
):
    alex = adult_chat.person
    reply = await adult_chat.say_with_tool(
        TOOL,
        chain(
            ("note_leave", {"recipient": "Owner", "text": "a"}),
            ("note_leave", {"recipient": "Owner", "text": "b"}),
        ),
    )
    cid = reply.pending_confirmation_id
    await grant("chain.run", person=alex, access="deny")
    assert (await adult_chat.confirm_last(reply)).status == "refused"
    assert steps_of_rows(await steps_rows(cid)) == [(0, "planned"), (1, "planned")]
    assert await count_notes() == 0


async def test_two_yes_on_a_chain_run_it_once(
    owner_chat, adult_member, person_row, steps_rows, count_notes
):  # Review Focus 1
    reply = await owner_chat.say_with_tool(
        TOOL,
        chain(
            ("household_change_role", {"person": "Alex", "role": "child"}),
            ("note_leave", {"recipient": "Alex", "text": "x"}),
        ),
    )
    cid = reply.pending_confirmation_id
    outcomes: list[str] = []

    async def press() -> None:
        outcomes.append(status_of(await owner_chat.post(f"/confirmations/{cid}/approve")))

    async with anyio.create_task_group() as group:
        group.start_soon(press)
        group.start_soon(press)
    assert sorted(outcomes) == ["already_decided", "executed"]
    assert (await person_row(adult_member.person)).role == "child"
    assert steps_of_rows(await steps_rows(cid)) == [(0, "done"), (1, "done")]
    assert await count_notes() == 1


class RemovesTheRequester:
    """``note_leave``, which also removes ``person`` from the household once its note is left:
    the new owner removing the old one between two steps of the old one's chain."""

    def __init__(self, person: Person) -> None:
        self._tool = TOOLS["note_leave"]
        self._person = person
        self.definition = self._tool.definition
        self.action_type = self._tool.action_type

    async def describe(self, arguments: Any, actor: Any, ctx: Any) -> Any:
        return await self._tool.describe(arguments, actor, ctx)

    async def execute(self, arguments: Any, ctx: ToolContext) -> ToolOutcome:
        outcome = await self._tool.execute(arguments, ctx)
        await remove(ctx.session_factory, self._person, ctx.clock)
        return outcome


async def test_a_chain_stops_when_the_requester_is_removed_meanwhile(
    owner_chat, adult_member, chat_server, steps_rows, person_row, audit_actions
):  # Review Focus 2
    registry = {**TOOLS, "note_leave": RemovesTheRequester(owner_chat.person)}
    chat_server.app.dependency_overrides[get_tool_registry] = lambda: registry
    reply = await owner_chat.say_with_tool(
        TOOL,
        chain(
            ("household_transfer_ownership", {"person": "Alex"}),
            ("note_leave", {"recipient": "Alex", "text": "x"}),
            ("household_remove", {"person": "Owner"}),
        ),
    )
    assert (await owner_chat.confirm_last(reply)).status == "failed"
    rows = await steps_rows(reply.pending_confirmation_id)
    assert steps_of_rows(rows) == [(0, "done"), (1, "done"), (2, "failed")]
    assert rows[2].result is not None and rows[2].result["reason"] == "no_longer_possible"
    # Nothing else: the third step never ran.
    assert "household.removed" not in await audit_actions()
    assert (await person_row(adult_member.person)).role == "owner"


async def test_nothing_more_is_done_for_someone_removed_while_their_chain_runs(
    owner_chat, adult_member, child_member, chat_server, steps_rows, stored_notes
):  # Review Focus 2: a step that would work otherwise
    registry = {**TOOLS, "note_leave": RemovesTheRequester(owner_chat.person)}
    chat_server.app.dependency_overrides[get_tool_registry] = lambda: registry
    reply = await owner_chat.say_with_tool(
        TOOL,
        chain(
            ("household_change_role", {"person": "Sam", "role": "adult"}),
            ("note_leave", {"recipient": "Alex", "text": "x"}),
            ("note_leave", {"recipient": "Sam", "text": "y"}),
        ),
    )
    assert (await owner_chat.confirm_last(reply)).status == "failed"
    rows = await steps_rows(reply.pending_confirmation_id)
    assert steps_of_rows(rows) == [(0, "done"), (1, "done"), (2, "failed")]
    assert rows[2].result is not None and rows[2].result["reason"] == "no_longer_possible"
    assert [note.text for note in await stored_notes()] == ["x"]


async def test_a_step_whose_person_an_earlier_step_removed_stops_the_chain(
    owner_chat, adult_member, steps_rows, count_notes
):  # Review Focus 5
    reply = await owner_chat.say_with_tool(
        TOOL,
        chain(
            ("household_remove", {"person": "Alex"}),
            ("note_leave", {"recipient": "Alex", "text": "x"}),
        ),
    )
    assert (await owner_chat.confirm_last(reply)).status == "failed"
    rows = await steps_rows(reply.pending_confirmation_id)
    assert steps_of_rows(rows) == [(0, "done"), (1, "failed")]
    assert rows[1].result is not None and rows[1].result["reason"] == "no_longer_possible"
    assert await count_notes() == 0


# --- the history a limit reads (spec 4.3) --------------------------------------------------------


async def test_each_step_counts_in_the_history_a_limit_reads(
    adult_chat, owner_member, grant, stored_notes
):
    await grant(
        "note.leave",
        person=adult_chat.person,
        limit='history.day < 2 ? "allow" : "refuse"',
        summary="Alex leaves two notes a day at most.",
    )
    note = ("note_leave", {"recipient": "Owner", "text": "x"})
    reply = await adult_chat.say_with_tool(TOOL, chain(note, note, note), judge="do")
    assert reply.actions == [{"tool": TOOL, "status": "failed", "reason": "limit"}]
    result = reply.model_results[-1]
    assert (result["stopped_at"], result["reason"], result["left"]) == (3, "limit", 0)
    assert result["steps"][-1]["result"]["limit"] == "Alex leaves two notes a day at most."
    assert len(await stored_notes()) == 2
    # The notes the chain left count after it too.
    single = await adult_chat.say_with_tool(*note)
    assert single.model_results[-1]["reason"] == "limit"
    assert len(await stored_notes()) == 2


async def test_a_chain_that_ran_at_once_and_stopped_is_done_again_with_new_calls(
    adult_chat, owner_member, grant, stored_notes, count_confirmations
):  # final review M6: it has no request that chain_continue could take up again
    await grant(
        "note.leave",
        person=adult_chat.person,
        limit='history.day < 1 ? "allow" : "refuse"',
        summary="Alex leaves one note a day at most.",
    )
    note = ("note_leave", {"recipient": "Owner", "text": "x"})
    reply = await adult_chat.say_with_tool(TOOL, chain(note, note), judge="do")
    assert reply.actions == [{"tool": TOOL, "status": "failed", "reason": "limit"}]
    assert await count_confirmations() == 0 and len(await stored_notes()) == 1
    result = reply.model_results[-1]
    assert result["stopped_at"] == 2
    assert result["detail"] == STOPPED_AT_ONCE_DETAIL
    assert "chain_continue" not in result["detail"] and "chain_continue" in STOPPED_DETAIL


async def test_the_steps_of_a_chain_that_waited_count_in_the_history(
    adult_chat, owner_member, grant, stored_notes
):
    await grant(
        "note.leave",
        person=adult_chat.person,
        limit='history.day < 2 ? "allow" : "refuse"',
        summary="Alex leaves two notes a day at most.",
    )
    note = ("note_leave", {"recipient": "Owner", "text": "x"})
    reply = await adult_chat.say_with_tool(TOOL, chain(note, note))
    assert (await adult_chat.confirm_last(reply)).status == "executed"
    assert len(await stored_notes()) == 2
    single = await adult_chat.say_with_tool(*note)
    assert single.model_results[-1]["reason"] == "limit"


# --- the pieces ----------------------------------------------------------------------------------


def test_chain_continue_is_a_free_tool_of_the_members_that_bounds_its_own_steps():
    tool = TOOLS[CONTINUE]
    assert tool.action_type == "chain.continue"
    assert getattr(tool, "bounds_its_own_steps", False) is True
    assert tool.definition.parameters["required"] == ["request_id"]


async def test_marking_a_step_that_does_not_exist_fails_loudly(
    owner_chat, adult_member, session_factory
):
    reply = await owner_chat.say_with_tool(TOOL, DEPARTURE)
    cid = reply.pending_confirmation_id
    assert cid is not None
    async with session_factory() as writer:
        with pytest.raises(LookupError):
            await mark_step(writer, cid, 5, status="done", result=None, now=owner_chat.clock())
        with pytest.raises(LookupError):
            await mark_step(
                writer, uuid.uuid4(), 0, status="done", result=None, now=owner_chat.clock()
            )


async def test_what_domo_reads_of_a_chain_tells_each_step(owner_chat, adult_member):
    reply = await owner_chat.say_with_tool(TOOL, DEPARTURE)
    outcome, record = await yes_through_the_service(owner_chat, reply.pending_confirmation_id)
    assert outcome == "executed" and record is not None
    transfer, removal = record.model_result["steps"]
    assert (transfer["step"], transfer["action_type"], transfer["status"]) == (
        1,
        "household.transfer_ownership",
        "done",
    )
    assert transfer["result"]["new_owner"] == "Alex"
    assert (removal["step"], removal["status"], removal["result"]) == (
        2,
        "done",
        {"status": "done", "removed": "Owner", "paused_rights": 0},
    )
    assert record.card == {
        "key": "action.card.done.chain",
        "params": {"done": "2", "total": "2"},
        "did": ["household.transfer_ownership", "household.remove"],
        "buttons": [],
    }
