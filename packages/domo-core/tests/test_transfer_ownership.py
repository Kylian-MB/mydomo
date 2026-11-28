"""Handing the owner's role over (spec 6 and 8): the owner, through Domo, gives the role to an
adult of the household, with an explicit yes every time, and from then on the roles are the other
way round.

The role is the owner's alone and no right moves that. The new owner does not have to agree. The
old owner becomes an adult and keeps their account. In the same transaction the requests of the
new owner that waited for the owner's yes wait for their own, and their personal rights are
paused. The pages follow the role at the next request. There is exactly one owner at every
moment.

The chat pilots talk with Domo on a live server (see ``chat_support.py``); the service and the
tool are also tried directly.
"""

import asyncio
import html
import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from chat_support import Card, status_of
from domo_common.i18n import t
from domo_core.accounts.models import Account, Person
from domo_core.accounts.sessions import SESSION_COOKIE, create_session
from domo_core.confirmations.describe import describe_for_people
from domo_core.confirmations.models import Confirmation
from domo_core.confirmations.store import ANSWER_EXPLICITLY
from domo_core.conversation.execution import NOT_ALLOWED_DETAIL
from domo_core.household.service import (
    OWNERSHIP_NOTE_DAYS,
    Transferred,
    disable,
    ownership_note,
    transfer_ownership,
)
from domo_core.llm.types import ToolCall
from domo_core.people.models import MadeRole
from domo_core.policy.actions import DescribedAction
from domo_core.policy.catalog import DEFAULT_POLICY
from domo_core.policy.models import PolicyRight
from domo_core.policy.roles import Actor
from domo_core.policy.settings import InvalidSetting, validate_setting
from domo_core.texts.view import CatalogTexts
from domo_core.tools.context import InvalidArguments, ToolContext
from domo_core.tools.registry import TOOLS
from fakes import FakeChatModel
from sqlalchemy import func, select, text

TOOL = "household_transfer_ownership"
ACTION_TYPE = "household.transfer_ownership"


async def hand_over(owner_chat, name: str) -> Card:
    """The owner asks Domo to hand the role to ``name`` and presses the confirm button."""
    return await owner_chat.confirm_last(await owner_chat.say_with_tool(TOOL, {"person": name}))


def plain(fragment: str) -> str:
    """What a person reads in a page or a fragment."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def context(person: Person, session_factory, clock, *, role="owner", **overrides) -> ToolContext:
    """The context of a call made by ``person``, whom the caller takes for ``role``."""
    return ToolContext(
        session_factory=session_factory,
        actor=Actor(person.id, role),
        person=person,
        conversation_id=uuid.uuid4(),
        origin="https://domo.example.com/",
        gatekeeper=None,  # type: ignore[arg-type]
        clock=clock,
        registry=TOOLS,
        policy=DEFAULT_POLICY,
        reply_message_id=None,
        **overrides,
    )


@pytest.fixture
def role_of(session_factory):
    """``await role_of(person)``: the role the database holds for the person now."""

    async def _role_of(person: Person) -> str:
        async with session_factory() as reader:
            return (
                await reader.execute(select(Person.role).where(Person.id == person.id))
            ).scalar_one()

    return _role_of


@pytest.fixture
def count_owners(session_factory):
    """``await count_owners()``: how many people have the owner's role now (committed)."""

    async def _count_owners() -> int:
        async with session_factory() as reader:
            found = await reader.execute(
                select(func.count()).select_from(Person).where(Person.role == "owner")
            )
            return found.scalar_one()

    return _count_owners


@pytest.fixture
def approver_of(session_factory):
    """``await approver_of(confirmation_id)``: who says yes to the request, as stored."""

    async def _approver_of(confirmation_id) -> str:
        async with session_factory() as reader:
            found = await reader.execute(
                select(Confirmation.approver_kind).where(Confirmation.id == confirmation_id)
            )
            return found.scalar_one()

    return _approver_of


# --- the hand-over -------------------------------------------------------------------------------


async def test_the_owner_hands_the_role_to_an_adult_who_does_not_have_to_agree(
    owner_chat, adult_member, person_row, last_audit_entry
):
    reply = await owner_chat.say_with_tool(TOOL, {"person": "Alex"})
    assert reply.model_results[-1]["status"] == "waiting_for_confirmation"
    assert reply.actions == [{"tool": TOOL, "status": "waiting"}]
    # Nothing has changed before the yes.
    assert (await person_row(adult_member.person)).role == "adult"
    card = await owner_chat.confirm_last(reply)
    assert card.status == "executed"
    words = plain(card.html)
    assert t("action.card.done.household.transfer_ownership", "en", name="Alex") in words
    assert t("action.describe.household.transfer_ownership", "en", name="Alex") in words
    assert (await person_row(adult_member.person)).role == "owner"
    assert (await person_row(owner_chat.person)).role == "adult"
    assert (await person_row(adult_member.person)).expires_at is None
    # The audit holds identifiers, names, roles and numbers, written by the one who handed it over.
    entry = await last_audit_entry("household.ownership_transferred")
    assert entry.actor == f"person:{owner_chat.person.id}"
    assert entry.details == {
        "from": f"person:{owner_chat.person.id}",
        "to": f"person:{adult_member.person.id}",
        "from_name": "Owner",
        "to_name": "Alex",
        "rerouted": 0,
        "paused": 0,
        "left_the_successors": 0,
    }


async def test_the_model_is_told_what_the_hand_over_did(
    owner_chat, adult_member, session_factory, live_clock
):
    outcome = await TOOLS[TOOL].execute(
        {"person": "Alex"}, context(owner_chat.person, session_factory, live_clock)
    )
    assert outcome.status == "done"
    assert outcome.model_result == {"new_owner": "Alex", "rerouted": 0, "paused_rights": 0}
    assert outcome.card == {
        "key": "action.card.done.household.transfer_ownership",
        "params": {"name": "Alex"},
    }
    assert outcome.card_once is None


async def test_the_old_owner_keeps_their_account_and_the_conversation(
    owner_chat, adult_member, chat_server
):
    await hand_over(owner_chat, "Alex")
    reply = await owner_chat.say("hello")
    assert reply.status == "complete" and reply.text == "ok"
    # Domo now knows them as an adult.
    assert "Role: adult" in chat_server.calls[-1].system
    assert "Tools: available" in chat_server.calls[-1].system


async def test_the_new_owners_requests_now_wait_for_their_own_yes(
    owner_chat, adult_chat, adult_member, grant, approver_of, last_audit_entry
):
    await grant("household.invite", person=adult_member.person, access="allow", approver="owner")
    asked = await adult_chat.say_with_tool(
        "household_invite", {"display_name": "Lou", "role": "child"}
    )
    assert asked.model_results[-1]["by"] == "the owner"
    assert await approver_of(asked.pending_confirmation_id) == "owner"

    await hand_over(owner_chat, "Alex")

    # The owner does not say yes to their own request on someone's behalf, and there is no other
    # owner: it waits for Alex's own yes now.
    assert await approver_of(asked.pending_confirmation_id) == "self"
    assert (await last_audit_entry("household.ownership_transferred")).details["rerouted"] == 1
    assert (await adult_chat.confirm_last(asked)).status == "executed"


async def test_requests_that_waited_for_the_owner_now_wait_for_the_new_one(
    child_chat,
    owner_chat,
    child_member,
    adult_member,
    live_adult_client,
    live_owner_client,
    grant,
    count_notes,
):
    await grant("note.leave", person=child_member.person, approver="owner")
    reply = await child_chat.say_with_tool("note_leave", {"recipient": "Owner", "text": "Hello"})
    assert reply.model_results[-1]["by"] == "the owner"
    waiting = f'data-confirmation-id="{reply.pending_confirmation_id}"'
    assert waiting in (await live_owner_client.get("/approvals")).text
    assert waiting not in (await live_adult_client.get("/approvals")).text

    await hand_over(owner_chat, "Alex")

    assert waiting in (await live_adult_client.get("/approvals")).text
    assert waiting not in (await live_owner_client.get("/approvals")).text
    path = f"/confirmations/{reply.pending_confirmation_id}/approve"
    # The old owner is an adult now: the yes of the owner is not theirs to give.
    assert status_of(await live_owner_client.post(path)) == "not_yours"
    assert await count_notes() == 0
    assert status_of(await live_adult_client.post(path)) == "executed"
    assert await count_notes() == 1


async def test_the_new_owners_personal_rights_are_paused_and_the_old_owner_keeps_their_account(
    owner_chat, adult_member, grant, session_factory, last_audit_entry
):
    right = await grant("note.leave", person=adult_member.person, ask="yes")
    await hand_over(owner_chat, "Alex")
    async with session_factory() as reader:
        row = (
            await reader.execute(select(PolicyRight).where(PolicyRight.id == right.id))
        ).scalar_one()
    # Paused, not lost: the new owner is not held back by limits that were set for an adult.
    assert row.paused_at is not None and row.revoked_at is None
    assert (await last_audit_entry("household.ownership_transferred")).details["paused"] == 1
    assert (await owner_chat.say("hello")).status == "complete"


async def test_the_model_is_told_how_many_requests_and_rights_the_hand_over_moved(
    owner_member, adult_chat, adult_member, grant, session_factory, live_clock
):
    await grant("household.invite", person=adult_member.person, access="allow", approver="owner")
    await adult_chat.say_with_tool("household_invite", {"display_name": "Lou", "role": "child"})
    outcome = await TOOLS[TOOL].execute(
        {"person": "Alex"}, context(owner_member.person, session_factory, live_clock)
    )
    assert outcome.model_result == {"new_owner": "Alex", "rerouted": 1, "paused_rights": 1}


# --- asked for and understood, it is done: nothing in the code asks again -------------------------

# A judge that asks a firm yes of this hand-over: its own judgement, never the code's default.
FIRM = "explicit_yes: a big step"


async def test_a_hand_over_asked_for_and_understood_is_done_without_a_request(
    owner_chat, adult_member, person_row, session_factory
):
    """The owner's decision of 2026-11-15: "when a request is made and understood by Domo,
    Domo acts, and does not ask the question again". No type asks a yes by default, the
    hand-over of the owner's role neither: the judge let it go, so it is done."""
    reply = await owner_chat.say_with_tool(TOOL, {"person": "Alex"}, judge="do")
    assert reply.actions == [{"tool": TOOL, "status": "done"}]
    assert reply.pending_confirmation_id is None
    assert (await person_row(adult_member.person)).role == "owner"
    assert (await person_row(owner_chat.person)).role == "adult"
    async with session_factory() as reader:
        requests = await reader.execute(select(func.count()).select_from(Confirmation))
        assert requests.scalar_one() == 0


async def test_a_judge_that_does_not_answer_does_not_hold_the_hand_over_back(
    owner_chat, adult_member, person_row, last_audit_entry
):
    # An answer that is no verdict: the fallback is "do", never a yes to ask.
    reply = await owner_chat.say_with_tool(TOOL, {"person": "Alex"}, judge="well, perhaps")
    assert reply.actions == [{"tool": TOOL, "status": "done"}]
    assert (await person_row(adult_member.person)).role == "owner"
    verdict = (await last_audit_entry("judge.verdict")).details
    assert (verdict["verdict"], verdict["source"]) == ("do", "fallback")


async def test_the_owners_own_rule_on_the_hand_over_still_asks_what_it_asks(
    owner_chat, adult_member, grant, person_row, confirmation_ask
):
    # "To hand my role over, always ask me a proof": his rule, whatever the judge says.
    await grant(ACTION_TYPE, person=owner_chat.person, ask="proof")
    reply = await owner_chat.say_with_tool(TOOL, {"person": "Alex"}, judge="do")
    assert reply.actions == [{"tool": TOOL, "status": "waiting"}]
    assert await confirmation_ask(reply.pending_confirmation_id) == ("proof", "rule", None)
    assert (await person_row(adult_member.person)).role == "adult"


async def test_the_judge_may_ask_a_firm_yes_of_the_hand_over(
    owner_chat, adult_member, confirmation_status, confirmation_ask
):
    reply = await owner_chat.say_with_tool(TOOL, {"person": "Alex"}, judge=FIRM)
    # Domo is told how the yes can be given.
    assert reply.model_results[-1]["answer_with"] == ANSWER_EXPLICITLY
    assert await confirmation_ask(reply.pending_confirmation_id) == (
        "explicit_yes",
        "judge",
        "a big step",
    )
    owner_chat.use_model(FakeChatModel(["Il faut un oui plus ferme."]))
    await owner_chat.say("oui")
    assert await confirmation_status(reply.pending_confirmation_id) == "pending"
    await owner_chat.say("je confirme")
    assert await confirmation_status(reply.pending_confirmation_id) == "executed"


async def test_a_firm_yes_in_the_owners_own_words_hands_the_role_over(
    owner_chat, adult_member, confirmation_status, person_row, session_factory
):
    """The owner's trial of 0.8.0: "oui", then "Oui, je t'ai déjà dit que c'était ok" were both
    refused, and only the literal "je confirme" passed. A bare yes is still not enough; a firm
    yes in their own words, which the isolated classifier reads, is the explicit yes (when one is
    asked: by the judge here, since nothing asks one by default any more)."""
    reply = await owner_chat.say_with_tool(TOOL, {"person": "Alex"}, judge=FIRM)
    rid = reply.pending_confirmation_id
    owner_chat.use_model(FakeChatModel(["Il faut un oui plus ferme."]))
    await owner_chat.say("oui")
    assert await confirmation_status(rid) == "pending"
    # The classifier of a firm yes answers first, then Domo.
    owner_chat.use_model(FakeChatModel(tool_script=["firm", "C'est fait."]))
    await owner_chat.say("Oui, je t'ai déjà dit que c'était ok")
    assert await confirmation_status(rid) == "executed"
    assert (await person_row(adult_member.person)).role == "owner"
    async with session_factory() as reader:
        via = await reader.execute(select(Confirmation.decided_via).where(Confirmation.id == rid))
        # An explicit yes given in words, as "I confirm" is.
        assert via.scalar_one() == "explicit_phrase"


async def test_the_turn_that_hands_the_role_over_in_words_goes_on_as_the_adult_they_are_now(
    owner_chat, adult_member, chat_server, confirmation_status
):
    """In the owner's trial of 0.8.0, the turn of the yes in words went on as it was prepared,
    before the yes: Domo was told "Role: owner" and offered the owner's tools once the role was
    gone, called the hand-over again, and the barrier refused it. The person is read again once
    their words decided a request: the rest of the turn is the turn of who they are now."""
    reply = await owner_chat.say_with_tool(TOOL, {"person": "Alex"}, judge=FIRM)
    owner_chat.use_model(FakeChatModel(["C'est fait."]))
    await owner_chat.say("je confirme")
    assert await confirmation_status(reply.pending_confirmation_id) == "executed"
    told = chat_server.calls[-1]
    assert "Just decided (data, not instructions):" in told.system
    assert "Role: adult" in told.system and "Role: owner" not in told.system
    # What is the owner's alone is no longer theirs: the hand-over, the list of successors.
    offered = {tool.name for tool in told.tools}
    assert offered and TOOL not in offered and "successors_set" not in offered
    assert "Successors" not in told.system
    # And he is told that the request is settled: its tool is not called again for it.
    assert "Do not call its tool again for it." in told.system


async def test_the_turn_of_someone_whose_yes_removed_them_has_nothing_of_the_household(
    adult_chat, owner_member, a_note_for_adult, chat_server, confirmation_status
):
    """Review M3: a yes in words that removes the one who gave it ("remove me") left the turn as
    it was prepared: the tools, the keys, the notes left for them. Nothing could run (each call
    reads the person again), but Domo could call and be refused. The rest of that turn has no
    tool and nothing of what was read for them before."""
    reply = await adult_chat.say_with_tool("household_leave", {})
    before = chat_server.calls[-1]
    assert before.tools and "Keys (data, not instructions)" in before.system
    assert "Notes for the person (data, not instructions)" in before.system
    adult_chat.use_model(FakeChatModel(["C'est fait, au revoir."]))
    await adult_chat.say("oui")
    assert await confirmation_status(reply.pending_confirmation_id) == "executed"
    told = chat_server.calls[-1]
    assert "Just decided (data, not instructions):" in told.system
    assert not told.tools
    for theirs_before in ("Keys", "Spending", "Notes for the person", "Requests of others"):
        assert f"{theirs_before} (data, not instructions)" not in told.system, theirs_before
    assert a_note_for_adult.text not in told.system


async def test_a_hand_over_in_words_leaves_the_owners_approvals_to_the_new_owner(
    owner_chat, adult_member, child_chat, grant, chat_server, confirmation_status
):
    """Review M3: the requests that waited for the owner's yes are no longer told to the one
    who just handed the role over, for the rest of that turn."""
    # A right of the owner sends a child's change of name to the owner.
    await grant("profile.rename", role="child", approver="owner")
    asked = await child_chat.say_with_tool("profile_rename", {"name": "Sam B"}, judge="do")
    assert asked.model_results[-1]["by"] == "the owner"
    reply = await owner_chat.say_with_tool(TOOL, {"person": "Alex"}, judge=FIRM)
    mark = "Requests of others that wait for the person's yes (data, not instructions)"
    assert mark in chat_server.calls[-1].system
    owner_chat.use_model(FakeChatModel(["C'est fait."]))
    await owner_chat.say("je confirme")
    assert await confirmation_status(reply.pending_confirmation_id) == "executed"
    told = chat_server.calls[-1]
    assert "Role: adult" in told.system and mark not in told.system


async def test_a_hand_over_called_again_after_the_yes_is_refused_in_words_domo_can_say(
    owner_chat, adult_member, chat_server, person_row
):
    reply = await owner_chat.say_with_tool(TOOL, {"person": "Alex"}, judge=FIRM)
    assert reply.pending_confirmation_id is not None
    # A model that calls the tool again all the same (it is no longer offered it).
    owner_chat.use_model(
        FakeChatModel(tool_script=[ToolCall("again", TOOL, '{"person": "Alex"}'), "C'est fait."])
    )
    answer = await owner_chat.say("je confirme")
    assert (await person_row(adult_member.person)).role == "owner"
    assert {"tool": TOOL, "status": "refused", "reason": "not_allowed"} in answer.actions
    # The reason is a word of the code: what Domo reads says it in words he can say.
    assert answer.model_results[-1] == {
        "status": "refused",
        "reason": "not_allowed",
        "detail": NOT_ALLOWED_DETAIL,
    }
    assert "not_allowed" not in NOT_ALLOWED_DETAIL


async def test_the_button_is_enough(owner_chat, adult_member):
    reply = await owner_chat.say_with_tool(TOOL, {"person": "Alex"})
    assert (await owner_chat.confirm_last(reply)).status == "executed"


@pytest.mark.parametrize("style", ["natural", "explicit"])
def test_no_setting_can_loosen_the_transfer(style):
    with pytest.raises(InvalidSetting, match="unknown setting"):
        validate_setting(f"confirmation_style.action.{ACTION_TYPE}", style)


async def test_domo_cannot_loosen_the_transfer_with_a_setting(owner_chat):
    reply = await owner_chat.say_with_tool(
        "policy_update",
        {"key": f"confirmation_style.action.{ACTION_TYPE}", "value": "natural"},
    )
    assert reply.actions == [{"tool": "policy_update", "status": "invalid", "reason": "invalid"}]


# --- who may receive it, who may hand it over ----------------------------------------------------


@pytest.mark.parametrize(
    ("name", "why"),
    [
        ("Sam", "the role goes to an adult who is in the household"),  # a child
        ("Robin", "the role goes to an adult who is in the household"),  # a guest
        ("Owner", "the role is not handed to oneself"),  # oneself
    ],
)
async def test_the_role_goes_only_to_an_adult_of_the_household(
    owner_chat, adult_member, child_member, guest_member, name, why, count_owners
):
    reply = await owner_chat.say_with_tool(TOOL, {"person": name})
    assert reply.actions == [{"tool": TOOL, "status": "invalid", "reason": "invalid"}]
    assert reply.model_results[-1]["detail"] == why
    assert await count_owners() == 1


async def test_someone_who_left_cannot_receive_the_role(
    owner_chat, adult_member, session_factory, live_clock, role_of
):
    async with session_factory() as writer:
        await disable(
            writer,
            await writer.get_one(Person, adult_member.person.id),
            live_clock(),
            by=adult_member.person.id,
        )
        await writer.commit()
    reply = await owner_chat.say_with_tool(TOOL, {"person": "Alex"})
    assert reply.actions == [{"tool": TOOL, "status": "invalid", "reason": "not_found"}]
    assert await role_of(owner_chat.person) == "owner"


async def test_a_name_that_means_nobody_is_not_guessed(owner_chat, adult_member):
    reply = await owner_chat.say_with_tool(TOOL, {"person": "Zed"})
    assert reply.actions == [{"tool": TOOL, "status": "invalid", "reason": "not_found"}]


@pytest.mark.parametrize("arguments", [{}, {"person": ""}, {"person": 3}, {"person": "A", "x": 1}])
async def test_the_arguments_are_a_name_and_nothing_else(owner_chat, adult_member, arguments):
    reply = await owner_chat.say_with_tool(TOOL, arguments)
    assert reply.actions == [{"tool": TOOL, "status": "invalid", "reason": "invalid"}]


async def test_only_the_current_owner_hands_the_role_over(
    adult_chat, add_member, adult_member, owner_member, grant, role_of
):
    kim = await add_member("Kim", "adult")
    reply = await adult_chat.say_with_tool(TOOL, {"person": "Kim"})
    assert reply.actions == [{"tool": TOOL, "status": "refused", "reason": "not_allowed"}]
    # No right gives it: the foundation is the owner's alone.
    await grant(ACTION_TYPE, person=adult_member.person, access="allow")
    await grant(ACTION_TYPE, role="adult", access="allow")
    again = await adult_chat.say_with_tool(TOOL, {"person": "Kim"})
    assert again.actions == [{"tool": TOOL, "status": "refused", "reason": "not_allowed"}]
    assert [await role_of(p) for p in (owner_member.person, adult_member.person, kim)] == [
        "owner",
        "adult",
        "adult",
    ]


async def test_a_transfer_that_waited_is_refused_if_the_requester_is_no_longer_the_owner(
    owner_chat, adult_member, owner_member, set_role, role_of, confirmation_status
):
    reply = await owner_chat.say_with_tool(TOOL, {"person": "Alex"})
    await set_role(owner_member.person, "adult")
    card = await owner_chat.confirm_last(reply)
    assert card.status == "refused"
    assert await confirmation_status(reply.pending_confirmation_id) == "refused"
    assert [await role_of(owner_member.person), await role_of(adult_member.person)] == [
        "adult",
        "adult",
    ]


async def test_a_transfer_that_waited_is_refused_if_the_new_owner_is_no_longer_an_adult(
    owner_chat, adult_member, set_role, role_of, confirmation_status
):
    reply = await owner_chat.say_with_tool(TOOL, {"person": "Alex"})
    await set_role(adult_member.person, "child")
    card = await owner_chat.confirm_last(reply)
    assert card.status == "refused"
    assert await confirmation_status(reply.pending_confirmation_id) == "refused"
    assert await role_of(owner_chat.person) == "owner"


async def test_a_transfer_that_waited_is_refused_if_the_new_owner_left_meanwhile(
    owner_chat, adult_member, session_factory, live_clock, role_of, confirmation_status
):
    reply = await owner_chat.say_with_tool(TOOL, {"person": "Alex"})
    async with session_factory() as writer:
        await disable(
            writer,
            await writer.get_one(Person, adult_member.person.id),
            live_clock(),
            by=adult_member.person.id,
        )
        await writer.commit()
    card = await owner_chat.confirm_last(reply)
    assert card.status == "refused"
    assert await confirmation_status(reply.pending_confirmation_id) == "refused"
    assert await role_of(owner_chat.person) == "owner"


async def test_the_tool_itself_refuses_a_requester_who_is_no_longer_the_owner(
    owner_member, adult_member, session_factory, live_clock, set_role, role_of
):
    """A caller that still believes the requester is the owner (the role of an earlier moment)
    does not get the hand-over: the tool reads the role the database holds now."""
    await set_role(owner_member.person, "adult")
    ctx = context(owner_member.person, session_factory, live_clock, role="owner")
    with pytest.raises(InvalidArguments) as refused:
        await TOOLS[TOOL].describe({"person": "Alex"}, ctx.actor, ctx)
    assert refused.value.code == "invalid"
    assert refused.value.detail == "only the current owner hands the role over"
    with pytest.raises(InvalidArguments) as refused:
        await TOOLS[TOOL].execute({"person": "Alex"}, ctx)
    assert refused.value.detail == "only the current owner hands the role over"
    assert await role_of(adult_member.person) == "adult"


async def test_a_name_that_no_longer_means_the_same_person_hands_nothing_over(
    owner_member, adult_member, add_member, session_factory, live_clock, role_of
):
    kim = await add_member("Kim", "adult")
    asked = DescribedAction("core", ACTION_TYPE, f"person:{kim.id}", "permissions", "household", {})
    ctx = context(owner_member.person, session_factory, live_clock, described_action=asked)
    with pytest.raises(InvalidArguments) as refused:
        await TOOLS[TOOL].execute({"person": "Alex"}, ctx)
    assert refused.value.code == "not_found"
    assert await role_of(adult_member.person) == "adult"
    assert await role_of(owner_member.person) == "owner"


async def test_the_tool_describes_the_hand_over_for_the_barrier(
    owner_member, adult_member, session_factory, live_clock
):
    ctx = context(owner_member.person, session_factory, live_clock)
    action = await TOOLS[TOOL].describe({"person": "alex"}, ctx.actor, ctx)
    assert action == DescribedAction(
        "core",
        ACTION_TYPE,
        f"person:{adult_member.person.id}",
        "permissions",
        "household",
        {
            "from": f"person:{owner_member.person.id}",
            "to": f"person:{adult_member.person.id}",
            "requester_role": "owner",
            "to_role": "adult",
        },
    )


# --- exactly one owner ---------------------------------------------------------------------------


async def _a_writer_waits_for_a_lock(session_factory) -> bool:
    async with session_factory() as reader:
        found = await reader.execute(
            text("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'")
        )
        return found.scalar_one() > 0


async def test_there_is_always_exactly_one_owner(
    session_factory, add_member, live_clock, count_owners, role_of
):
    owner = await add_member("Owner", "owner")
    alex = await add_member("Alex", "adult", username="alex")
    dana = await add_member("Dana", "adult", username="dana")
    assert await count_owners() == 1  # before

    async with session_factory() as one, session_factory() as two:
        done = await transfer_ownership(one, old_owner=owner, new_owner=alex, now=live_clock())
        assert isinstance(done, Transferred)
        assert await count_owners() == 1  # during: the first has not committed yet
        racing = asyncio.create_task(
            transfer_ownership(two, old_owner=owner, new_owner=dana, now=live_clock())
        )
        async with asyncio.timeout(10):
            while not await _a_writer_waits_for_a_lock(session_factory):
                await asyncio.sleep(0.02)
        assert not racing.done()
        await one.commit()
        # The second finds the owner is not the owner any more: nothing is handed over twice.
        with pytest.raises(ValueError):
            await racing
        await two.rollback()

    assert await count_owners() == 1  # after
    assert [await role_of(p) for p in (owner, alex, dana)] == ["adult", "owner", "adult"]


async def test_the_service_gives_the_role_in_one_step_and_says_what_it_moved(
    session, add_person, add_account, clock
):
    owner = await add_person("Owner", "owner")
    alex = await add_person("Alex", "adult")
    await add_account(alex, "alex")
    done = await transfer_ownership(session, old_owner=owner, new_owner=alex, now=clock())
    assert done == Transferred(
        old_owner_id=owner.id,
        new_owner_id=alex.id,
        rerouted=0,
        paused=0,
        left_the_successors=0,
    )
    roles = await session.execute(select(Person.display_name, Person.role))
    assert dict(roles.all()) == {"Owner": "adult", "Alex": "owner"}


async def _everyones_role(session) -> list[tuple[str, str]]:
    found = await session.execute(select(Person.display_name, Person.role))
    return sorted((name, role) for name, role in found.all())


@pytest.mark.parametrize(
    ("old_role", "new_role", "oneself", "left"),
    [
        ("adult", "adult", False, False),  # the one who hands it over is not the owner
        ("child", "adult", False, False),
        ("owner", "child", False, False),
        ("owner", "guest", False, False),
        ("owner", "adult", True, False),  # to oneself
        ("owner", "adult", False, True),  # someone who left
    ],
)
async def test_the_service_checks_who_gives_and_who_receives_again(
    session, add_person, clock, old_role, new_role, oneself, left
):
    old = await add_person("Old", old_role)
    new = old if oneself else await add_person("New", new_role)
    if left:
        new.disabled_at = clock()
        new.state = "gone"
        await session.flush()
    before = await _everyones_role(session)
    with pytest.raises(ValueError):
        await transfer_ownership(session, old_owner=old, new_owner=new, now=clock())
    assert await _everyones_role(session) == before


async def test_the_role_goes_only_to_an_adult_with_an_account_and_no_child_mark(
    session, add_person, add_account, clock
):
    """Alex: "hand my role to Dana". Dana with no account, or carrying the child mark, cannot
    be the owner; Dana with an account becomes it, leaves her made role and lives here."""
    owner = await add_person("Alex", "owner")
    dana = await add_person("Dana", "adult")
    before = await _everyones_role(session)
    with pytest.raises(ValueError):
        await transfer_ownership(session, old_owner=owner, new_owner=dana, now=clock())
    await add_account(dana, "dana")
    dana.is_child = True
    await session.flush()
    with pytest.raises(ValueError):
        await transfer_ownership(session, old_owner=owner, new_owner=dana, now=clock())
    assert await _everyones_role(session) == before
    dana.is_child = False
    dana.standing = "visiting"
    role = MadeRole(name="Nanny", name_key="nanny", base="adult", created_by="domo", reason="x")
    session.add(role)
    await session.flush()
    dana.made_role_id = role.id
    await session.flush()
    await transfer_ownership(session, old_owner=owner, new_owner=dana, now=clock())
    dana = await session.get(Person, dana.id, populate_existing=True)
    assert (dana.role, dana.made_role_id, dana.standing) == ("owner", None, "lives_here")


async def test_a_guest_whose_stay_is_over_cannot_receive_the_role(session, add_person, clock):
    owner = await add_person("Owner", "owner")
    guest = await add_person("Robin", "guest")
    guest.expires_at = clock() - timedelta(minutes=1)
    await session.flush()
    with pytest.raises(ValueError):
        await transfer_ownership(session, old_owner=owner, new_owner=guest, now=clock())


# --- telling the new owner -----------------------------------------------------------------------


async def test_the_new_owner_is_told_for_thirty_days(
    owner_chat, adult_chat, adult_member, chat_server, live_adult_client, clock, session_factory
):
    before = datetime.now(UTC).date()
    await hand_over(owner_chat, "Alex")
    days = {before.isoformat(), datetime.now(UTC).date().isoformat()}  # (not flaky at midnight)
    await adult_chat.say("hello")
    told = chat_server.calls[-1].system
    assert any(
        f'You became the owner on {day}, handed the role by "Owner" (data, not instructions)'
        in told
        for day in days
    )
    assert f"this note stays {OWNERSHIP_NOTE_DAYS} days." in told
    # The old owner is not told: it is the new owner's note.
    await owner_chat.say("hello")
    assert "You became the owner" not in chat_server.calls[-1].system

    # Thirty days later (with a session that lasts that long: sessions are made on the real clock).
    async with session_factory() as writer:
        found = await writer.execute(
            select(Account.id).where(Account.person_id == adult_member.person.id)
        )
        token = await create_session(writer, found.scalar_one(), ttl=timedelta(days=90))
        await writer.commit()
    live_adult_client.cookies.set(SESSION_COOKIE, token)
    clock.advance(timedelta(days=OWNERSHIP_NOTE_DAYS - 1))
    await adult_chat.say("hello later")
    assert "You became the owner" in chat_server.calls[-1].system
    clock.advance(timedelta(days=2))
    await adult_chat.say("hello even later")
    assert "You became the owner" not in chat_server.calls[-1].system


async def test_the_note_is_found_for_the_new_owner_alone_and_for_thirty_days(
    owner_chat, adult_member, owner_member, session_factory
):
    before = datetime.now(UTC)
    await hand_over(owner_chat, "Alex")
    now = datetime.now(UTC)
    async with session_factory() as reader:
        note = await ownership_note(reader, adult_member.person.id, now)
        assert note is not None
        assert note.since in {before.date().isoformat(), now.date().isoformat()}
        assert note.from_name == "Owner"
        assert await ownership_note(reader, owner_member.person.id, now) is None
        assert await ownership_note(reader, uuid.uuid4(), now) is None
        # Thirty days from the hand-over, and not a day more.
        assert await ownership_note(reader, adult_member.person.id, now + timedelta(days=29))
        past = now + timedelta(days=OWNERSHIP_NOTE_DAYS, minutes=5)
        assert await ownership_note(reader, adult_member.person.id, past) is None


async def test_the_note_says_who_handed_the_role_over_last(owner_chat, adult_chat, session_factory):
    await hand_over(owner_chat, "Alex")
    await hand_over(adult_chat, "Owner")
    async with session_factory() as reader:
        note = await ownership_note(reader, owner_chat.person.id, datetime.now(UTC))
    assert note is not None and note.from_name == "Alex"


# --- the pages follow the role (Review Focus 4) --------------------------------------------------


@pytest.mark.parametrize("path", ["/audit", "/household"])
async def test_the_pages_follow_the_role_after_the_transfer(
    owner_chat, live_owner_client, live_adult_client, adult_member, path
):
    assert (await live_owner_client.get(path)).status_code == 200
    assert (await live_adult_client.get(path)).status_code == 403
    await hand_over(owner_chat, "Alex")
    # Their pages are still open: at the next request the roles are the other way round.
    assert (await live_owner_client.get(path)).status_code == 403
    assert (await live_adult_client.get(path)).status_code == 200


# --- what people read ----------------------------------------------------------------------------


@pytest.mark.parametrize("lang", ["en", "fr"])
def test_the_request_reads_as_a_plain_sentence(lang):
    action = DescribedAction("core", ACTION_TYPE, "person:x", "permissions", "household")
    sentence = describe_for_people(action, {"person": "Alex"}, CatalogTexts(lang))
    assert sentence == t("action.describe.household.transfer_ownership", lang, name="Alex")
    assert "Alex" in sentence and "household." not in sentence
    assert sentence != t("action.describe", lang)


def test_the_sentences_say_what_the_brief_says():
    # Their meanings: the writer says them in each reader's language.
    assert "Alex" in t("action.describe.household.transfer_ownership", "en", name="Alex")
    assert "adult" in t("action.describe.household.transfer_ownership", "en", name="Alex")
    assert "Alex" in t("action.card.done.household.transfer_ownership", "en", name="Alex")


def test_the_tool_says_what_it_does_and_announces_no_yes():
    description = TOOLS[TOOL].definition.description
    # The owner reads Domo talk about it: the role is named, never "the owner" (spec 5.7.3).
    for word in ("the role of owner", "adult", "keep their account", "does not have to agree"):
        assert word in description, word
    assert "no way back" in description
    # It announced a firm yes by default: nothing is asked by default any more (2026-11-15), so
    # nothing here leads Domo to ask again what he understood.
    for gone in ("firm yes", "I confirm", "lifted", "their yes"):
        assert gone not in description, gone
