"""A chain that waits for its yes is changed as a whole (spec 4.1): the person answers it with a
change ("yes, but..."), and Domo gives the whole chain as he understood it, with steps changed,
added, taken away or put in another order. The code plans it again, as when it was asked for:

* a step kept as it was asked for is judged with the role the chain remembers, a step changed or
  added with the role of now (spec 4.1, last point; the foundation, spec 6.10);
* the steps already done cannot change, and the chain given back as it is changes nothing
  (``invalid``);
* a step the barrier refuses refuses the changed chain, with its number;
* otherwise the yes-judge judges the new version (spec 4.7), told what the request asked, what it
  did before and does after the change, and that nobody said yes yet: ``do_not`` refuses it (an
  adult whose accord it would need is never bothered); a step that needs an accord the person
  cannot give sends the changed chain to whoever gives it, whatever the judge says, asking what
  the judge, Domo, the old request and the owner's rules ask; and it runs at once under the
  words of the change only when nothing more than a plain yes of a rule or of the owner's default
  is asked (words never give an explicit yes or a proof, F2); anything the judge or Domo asks,
  and any explicit yes or proof, makes it a new request that replaces the old one.

The chat pilots talk with Domo on a live server (see ``chat_support.py``); some states are made
by hand, as the chain would have left them.
"""

import json
import uuid
from datetime import timedelta
from typing import Any

from domo_common.i18n import t
from domo_core.chat.store import ConversationStore
from domo_core.confirmations.models import ConfirmationStep
from domo_core.confirmations.replan import (
    StepMatch,
    calls_of,
    done_prefix_holds,
    match_steps,
    unchanged,
)
from domo_core.confirmations.steps import PlannedStep, add_steps, mark_step
from domo_core.confirmations.store import (
    ANSWER_BY_AN_ADULT,
    ANSWER_BY_THE_OWNER,
    ANSWER_EXPLICITLY,
    create_confirmation,
)
from domo_core.conversation.execution import JUDGE_REFUSED_DETAIL
from domo_core.llm.types import ToolCall
from domo_core.policy.actions import DescribedAction
from domo_core.policy.asks import Asked
from domo_core.policy.decision import ChainBasis
from domo_core.policy.roles import Actor
from domo_core.tools.chain import _chain_action
from domo_core.tools.context import InvalidArguments
from fakes import FakeChatModel
from test_chain_plan import chain, plain
from test_confirmation_tools import amend, ask_before
from test_judge_decides import judge_cases

TOOL = "do_in_order"


def call(tool: str, **arguments: Any) -> dict[str, Any]:
    """A step of a chain, as Domo gives it."""
    return {"tool": tool, "arguments": arguments}


def rechain(
    rid: object, steps: list[dict[str, Any]], ask_again: bool = False, call_id: str = "c1"
) -> ToolCall:
    """Domo's change of the chain ``rid``: the whole chain, as the person wants it now."""
    return ToolCall(
        call_id,
        "confirmation_amend",
        json.dumps({"request_id": str(rid), "arguments": {"steps": steps}, "ask_again": ask_again}),
    )


# Sam made a guest for 2 days, then a note to Alex; and the same with 5 days.
SAM_AND_A_NOTE = chain(
    ("household_change_role", {"person": "Sam", "role": "guest", "guest_days": 2}),
    ("note_leave", {"recipient": "Alex", "text": "a"}),
)
FIVE_DAYS = [
    call("household_change_role", person="Sam", role="guest", guest_days=5),
    call("note_leave", recipient="Alex", text="a"),
]


def tom_for(days: int) -> dict[str, Any]:
    """An invitation of Tom, a guest for ``days`` days."""
    return {"display_name": "Tom", "role": "guest", "guest_days": days}


# --- the change is judged again -------------------------------------------------------------------


async def test_a_yes_with_a_change_reruns_the_chain_with_the_changed_step(
    owner_chat,
    child_member,
    adult_member,
    person_row,
    confirmation_status,
    stored_notes,
    steps_rows,
):  # spec 8.1
    reply = await owner_chat.say_with_tool(TOOL, SAM_AND_A_NOTE)
    rid = str(reply.pending_confirmation_id)
    owner_chat.use_model(
        FakeChatModel(tool_script=["change", rechain(rid, FIVE_DAYS), "ok"], judge="do")
    )
    answer = await owner_chat.say("oui, mais 5 jours")
    assert answer.model_results[0]["outcome"] == "executed"
    assert await confirmation_status(rid) == "executed"
    sam = await person_row(child_member.person)
    assert sam.expires_at == owner_chat.clock() + timedelta(days=5)
    assert [n.text for n in await stored_notes()] == ["a"]
    # The rows are the chain that ran: the changed step is judged with the role of now.
    rows = await steps_rows(rid)
    assert [(r.arguments.get("guest_days"), r.status, r.with_basis) for r in rows] == [
        (5, "done", False),
        (None, "done", True),
    ]
    # The card says what the chain was before.
    before = t("action.describe.chain", "en", count="2")
    assert t("confirmation.changed", "en", before=before) in plain(answer.cards_html)


async def test_a_changed_chain_is_judged_again_with_what_changed(
    owner_chat, child_member, adult_member, person_row, confirmation_ask
):  # spec 4.7
    reply = await owner_chat.say_with_tool(TOOL, SAM_AND_A_NOTE)
    rid = reply.pending_confirmation_id
    assert await confirmation_ask(rid) == ("yes", "judge", None)
    owner_chat.use_model(
        FakeChatModel(
            tool_script=["change", rechain(rid, FIVE_DAYS), "ok"], judge="do: a small change"
        )
    )
    answer = await owner_chat.say("oui, mais 5 jours")
    assert answer.model_results[0]["outcome"] == "executed"
    sam = await person_row(child_member.person)
    assert sam.expires_at == owner_chat.clock() + timedelta(days=5)
    first, second = judge_cases(owner_chat.server)
    # The new version, judged for the one who asked, with the request's history: what it asked,
    # what it did before and does after the change; nobody had said yes yet (F23).
    assert second["person"] == first["person"] == {"name": "Owner", "role": "owner"}
    assert second["action"]["type"] == "chain.run"
    assert [step["type"] for step in second["steps"]] == [
        "household.change_role",
        "note.leave",
    ]
    request = second["request"]
    assert request["yes"] is None and request["asked"] == "yes"
    assert (request["done"], request["failed"], request["may_have_acted"]) == ([], None, False)
    before, after = request["before"], request["after"]
    assert len(before) == len(after) == 2
    assert before[1] == after[1] and before[0] != after[0]
    assert after == [step["says"] for step in second["steps"]]
    assert "request" not in first


async def test_a_changed_chain_the_judge_wants_asked_again_makes_a_new_card(
    owner_chat, child_member, adult_member, person_row, confirmation_status, confirmation_ask
):
    rid = (await owner_chat.say_with_tool(TOOL, SAM_AND_A_NOTE)).pending_confirmation_id
    owner_chat.use_model(
        FakeChatModel(
            tool_script=["change", rechain(rid, FIVE_DAYS), "Appuie sur le bouton."],
            judge="explicit_yes: bigger",
        )
    )
    answer = await owner_chat.say("oui, mais 5 jours")
    result = answer.model_results[0]
    assert result["outcome"] == "asked_again" and result["answer_with"] == ANSWER_EXPLICITLY
    assert (result["asked_by"], result["why"]) == ("judge", "bigger")
    new = result["request_id"]
    assert await confirmation_status(rid) == "replaced"
    assert await confirmation_status(new) == "pending"
    assert await confirmation_ask(new) == ("explicit_yes", "judge", "bigger")
    assert answer.pending_confirmation_id == uuid.UUID(new)
    assert (await person_row(child_member.person)).role == "child"


async def test_a_change_in_words_never_runs_what_the_old_ask_needs(
    owner_chat, confirmation_status, confirmation_ask, count_invitations
):  # F2
    gil = chain(
        ("household_invite", {"display_name": "Gil", "role": "adult"}),
        ("household_transfer_ownership", {"person": "Gil"}, "Gil"),
    )
    # The judge asks a firm yes of this chain (nothing asks one by default, 2026-11-15).
    asked = await owner_chat.say_with_tool(TOOL, gil, judge="explicit_yes: a big step")
    rid = asked.pending_confirmation_id
    assert await confirmation_ask(rid) == ("explicit_yes", "judge", "a big step")
    as_a_guest = [
        call("household_invite", display_name="Gil", role="guest"),
        {
            "tool": "household_transfer_ownership",
            "arguments": {"person": "Gil"},
            "after_arrival_of": "Gil",
        },
    ]
    owner_chat.use_model(
        FakeChatModel(tool_script=["change", rechain(rid, as_a_guest), "Confirme ?"], judge="do")
    )
    result = (await owner_chat.say("oui, mais en invité")).model_results[0]
    # Words never give the explicit yes it asked, whatever the judge says now: it is asked again.
    assert result["outcome"] == "asked_again" and result["answer_with"] == ANSWER_EXPLICITLY
    assert await confirmation_status(rid) == "replaced"
    assert await confirmation_ask(result["request_id"]) == (
        "explicit_yes",
        "judge",
        "a big step",
    )
    assert await count_invitations() == 0
    # A proof the judge asked stays the least a change asks.
    robin = await owner_chat.say_with_tool(
        "household_invite",
        {"display_name": "Robin", "role": "guest", "guest_days": 7},
        judge="proof: unusual",
    )
    asked_for = str(robin.pending_confirmation_id)
    assert await confirmation_ask(asked_for) == ("proof", "judge", "unusual")
    owner_chat.use_model(
        FakeChatModel(
            tool_script=["change", amend(asked_for, {"guest_days": 3}), "Prouve-le."], judge="do"
        )
    )
    proof = (await owner_chat.say("oui, mais 3 jours")).model_results[0]
    assert proof["outcome"] == "asked_again"
    assert await confirmation_status(asked_for) == "replaced"
    assert await confirmation_ask(proof["request_id"]) == ("proof", "judge", "unusual")
    assert await count_invitations() == 0


async def test_a_change_runs_at_once_on_the_plain_yes_a_rule_asks(
    owner_chat, grant, confirmation_status, confirmation_ask, count_invitations
):  # F2: the words of the change give a plain yes
    await grant("household.invite", person=owner_chat.person, ask="yes")
    first = await owner_chat.say_with_tool("household_invite", tom_for(7), judge="do")
    rid = str(first.pending_confirmation_id)
    assert await confirmation_ask(rid) == ("yes", "rule", None)
    owner_chat.use_model(
        FakeChatModel(tool_script=["change", amend(rid, {"guest_days": 3}), "ok"], judge="do")
    )
    assert (await owner_chat.say("oui, mais 3 jours")).model_results[0]["outcome"] == "executed"
    assert await confirmation_status(rid) == "executed"
    assert await count_invitations() == 1
    # The judge asks a yes again: a new request, whatever the rule asks.
    jo = {"display_name": "Jo", "role": "guest", "guest_days": 7}
    second = await owner_chat.say_with_tool("household_invite", jo, judge="do")
    owner_chat.use_model(
        FakeChatModel(
            tool_script=[
                "change",
                amend(str(second.pending_confirmation_id), {"guest_days": 3}),
                "D'accord ?",
            ],
            judge="yes: hmm",
        )
    )
    result = (await owner_chat.say("oui, mais 3 jours")).model_results[0]
    assert result["outcome"] == "asked_again"
    assert await confirmation_ask(result["request_id"]) == ("yes", "judge", "hmm")
    assert await count_invitations() == 1


async def test_an_approvers_change_is_judged_on_the_one_who_asked(
    child_chat, adult_chat, owner_member, grant, chat_server, confirmation_status
):  # F22
    await grant("household.invite", person=child_chat.person, access="allow", approver="adult")
    reply = await child_chat.say_with_tool(
        "household_invite", tom_for(7), text="Invite Tom for a week, please."
    )
    rid = str(reply.pending_confirmation_id)
    await adult_chat.say_with_tool("confirmation_remind", {"request_id": rid})
    adult_chat.use_model(
        FakeChatModel(tool_script=["change", amend(rid, {"guest_days": 3}), "ok"], judge="do")
    )
    answer = await adult_chat.say("oui pour Sam, mais seulement 3 jours")
    assert answer.model_results[0]["outcome"] == "executed"
    assert await confirmation_status(rid) == "executed"
    case = judge_cases(chat_server)[-1]
    # The case is Sam's: their name, their words, their request; never Alex's words.
    assert case["person"] == {"name": "Sam", "role": "child"}
    assert case["words"] == ["Invite Tom for a week, please."]
    assert "seulement 3 jours" not in json.dumps(case, ensure_ascii=False)
    assert case["rights"]["needs"] == "adult"
    assert case["request"]["yes"] is None


async def test_a_chain_given_back_unchanged_is_invalid(
    owner_chat, adult_member, confirmation_status, stored_notes
):  # as for any request
    rid = (
        await owner_chat.say_with_tool(
            TOOL, chain(("note_leave", {"recipient": "Alex", "text": "a"}))
        )
    ).pending_confirmation_id
    same = [call("note_leave", recipient="Alex", text="a")]
    owner_chat.use_model(
        FakeChatModel(tool_script=["change", rechain(rid, same), "Hmm."], judge="do")
    )
    answer = await owner_chat.say("oui, mais vérifie")
    assert answer.actions == [
        {"tool": "confirmation_amend", "status": "invalid", "reason": "invalid"}
    ]
    assert answer.model_results[0]["detail"] == (
        "the change changes nothing: give the steps that change"
    )
    assert await confirmation_status(rid) == "pending"
    assert await stored_notes() == []


async def test_an_added_step_is_told_to_the_judge(
    owner_chat,
    adult_member,
    person_row,
    confirmation_status,
    steps_rows,
    stored_notes,
):  # Review Focus 4: a step the person never saw on a card is judged with the rest
    reply = await owner_chat.say_with_tool(
        TOOL, chain(("note_leave", {"recipient": "Alex", "text": "a"}))
    )
    rid = reply.pending_confirmation_id
    more = [call("note_leave", recipient="Alex", text="a"), call("household_remove", person="Alex")]
    owner_chat.use_model(
        FakeChatModel(tool_script=["change", rechain(rid, more), "C'est fait."], judge="do")
    )
    answer = await owner_chat.say("oui, et retire Alex aussi")
    assert answer.model_results[0]["outcome"] == "executed"
    assert await confirmation_status(rid) == "executed"
    rows = await steps_rows(rid)
    assert [(r.tool_name, r.status, r.with_basis) for r in rows] == [
        ("note_leave", "done", True),
        ("household_remove", "done", False),
    ]
    assert (await person_row(adult_member.person)).disabled_at is not None
    assert [n.text for n in await stored_notes()] == ["a"]
    case = judge_cases(owner_chat.server)[-1]
    assert [step["type"] for step in case["steps"]] == ["note.leave", "household.remove"]
    assert len(case["request"]["before"]) == 1 and len(case["request"]["after"]) == 2


async def _a_childs_chain_with_a_departure_added(
    child_chat: Any, judge: str, *, before: tuple[ToolCall, ...] = ()
) -> dict[str, Any]:
    """Sam's chain [a note for the owner], waiting for Sam's yes; Sam says "yes, and I leave too",
    and Domo gives it back with ``household_leave`` added (which needs an adult), after
    ``before`` (his own calls first, say ``ask_before_acting``); the judge says ``judge`` of the
    change. What the change came to."""
    rid = (
        await child_chat.say_with_tool(
            TOOL, chain(("note_leave", {"recipient": "Owner", "text": "a"}))
        )
    ).pending_confirmation_id
    more = [call("note_leave", recipient="Owner", text="a"), call("household_leave")]
    child_chat.use_model(
        FakeChatModel(
            tool_script=["change", *before, rechain(rid, more), "Un adulte doit dire oui."],
            judge=judge,
        )
    )
    answer = await child_chat.say("oui, et je pars aussi")
    result = answer.model_results[-1]
    result["old"] = str(rid)
    return result


async def test_an_added_step_that_needs_an_adult_sends_the_changed_chain_to_them(
    child_chat, owner_member, adult_member, chat_server, steps_rows, stored_notes, person_row
):  # the accord the rights need, whatever the judge says
    result = await _a_childs_chain_with_a_departure_added(child_chat, "do")
    assert result["outcome"] == "approver_changed"
    assert result["answer_with"] == ANSWER_BY_AN_ADULT
    assert result["asked_by"] == "approver"
    rows = await steps_rows(result["request_id"])
    assert [(r.tool_name, r.status) for r in rows] == [
        ("note_leave", "planned"),
        ("household_leave", "planned"),
    ]
    assert await stored_notes() == []
    assert (await person_row(child_chat.person)).disabled_at is None
    # The judge was told the new version, and that it goes to an adult (spec 4.1, 4.4).
    case = judge_cases(chat_server)[-1]
    assert case["rights"]["needs"] == "adult"
    assert [step["type"] for step in case["steps"]] == ["note.leave", "household.leave"]
    assert case["request"]["yes"] is None and len(case["request"]["after"]) == 2


async def test_the_judge_and_domo_ask_the_adult_more_for_a_changed_chain(
    child_chat, owner_member, adult_member, confirmation_ask, confirmation_status
):  # spec 4.4: the verdict says how the adult says yes; Domo asks more, never less (I1)
    judged = await _a_childs_chain_with_a_departure_added(child_chat, "explicit_yes: leaving")
    assert judged["outcome"] == "approver_changed"
    assert (judged["asked_by"], judged["why"]) == ("judge", "leaving")
    assert await confirmation_ask(judged["request_id"]) == ("explicit_yes", "judge", "leaving")
    raised = await _a_childs_chain_with_a_departure_added(
        child_chat, "do", before=(ask_before("proof", "a child leaving"),)
    )
    assert raised["outcome"] == "approver_changed"
    assert await confirmation_ask(raised["request_id"]) == ("proof", "domo", "a child leaving")
    assert await confirmation_status(raised["old"]) == "replaced"


async def test_a_changed_chain_the_judge_refuses_never_reaches_the_adult(
    child_chat,
    owner_member,
    adult_member,
    live_adult_client,
    confirmation_status,
    count_confirmations,
    person_row,
):  # spec 4.4, as for a new request (F13): the adult is not bothered
    result = await _a_childs_chain_with_a_departure_added(child_chat, "do_not: not alone")
    assert (result["outcome"], result["reason"], result["why"]) == (
        "refused",
        "judge",
        "not alone",
    )
    assert await confirmation_status(result["old"]) == "refused"
    assert await count_confirmations() == 1
    assert "data-confirmation-id" not in (await live_adult_client.get("/approvals")).text
    assert (await person_row(child_chat.person)).disabled_at is None


async def test_a_changed_chain_the_judge_refuses_is_refused_with_its_reason(
    owner_chat, child_member, adult_member, person_row, confirmation_status, last_audit_entry
):  # spec 4.6
    rid = (await owner_chat.say_with_tool(TOOL, SAM_AND_A_NOTE)).pending_confirmation_id
    owner_chat.use_model(
        FakeChatModel(
            tool_script=["change", rechain(rid, FIVE_DAYS), "Non."], judge="do_not: not that long"
        )
    )
    answer = await owner_chat.say("oui, mais 5 jours")
    assert answer.model_results[0] == {
        "status": "done",
        "outcome": "refused",
        "reason": "judge",
        "why": "not that long",
        "detail": JUDGE_REFUSED_DETAIL,
    }
    assert await confirmation_status(rid) == "refused"
    assert (await person_row(child_member.person)).role == "child"
    entry = await last_audit_entry("judge.verdict")
    assert (entry.details["verdict"], entry.details["reason"]) == ("do_not", "not that long")


async def test_a_changed_chain_that_asked_an_explicit_yes_makes_a_new_card_that_replaces_the_old(
    owner_chat, child_member, adult_member, person_row, confirmation_status, steps_rows
):  # F2: words never give an explicit yes
    reply = await owner_chat.say_with_tool(TOOL, SAM_AND_A_NOTE, judge="explicit_yes: big")
    rid = reply.pending_confirmation_id
    owner_chat.use_model(
        FakeChatModel(
            tool_script=["change", rechain(rid, FIVE_DAYS), "Dis « je confirme »."], judge="do"
        )
    )
    answer = await owner_chat.say("oui, mais 5 jours")
    result = answer.model_results[0]
    assert result["outcome"] == "asked_again" and result["answer_with"] == ANSWER_EXPLICITLY
    new = uuid.UUID(result["request_id"])
    assert await confirmation_status(rid) == "replaced"
    assert await confirmation_status(new) == "pending"
    assert answer.pending_confirmation_id == new
    rows = await steps_rows(new)
    assert [(r.position, r.tool_name, r.arguments, r.status, r.with_basis) for r in rows] == [
        (0, "household_change_role", FIVE_DAYS[0]["arguments"], "planned", False),
        (1, "note_leave", FIVE_DAYS[1]["arguments"], "planned", True),
    ]
    before = t("action.describe.chain", "en", count="2")
    assert t("confirmation.changed", "en", before=before) in plain(answer.cards_html)
    # Nothing ran yet; "I confirm" runs the new request.
    assert (await person_row(child_member.person)).role == "child"
    owner_chat.use_model(FakeChatModel(["C'est fait."]))
    await owner_chat.say("je confirme")
    assert await confirmation_status(new) == "executed"
    sam = await person_row(child_member.person)
    assert sam.expires_at == owner_chat.clock() + timedelta(days=5)


async def test_a_changed_chain_that_needs_nobody_elses_accord_runs_at_once(
    owner_chat, adult_member, child_member, person_row, confirmation_status, stored_notes
):
    to_alex = chain(("note_leave", {"recipient": "Alex", "text": "a"}))
    rid = (await owner_chat.say_with_tool(TOOL, to_alex)).pending_confirmation_id
    changed = [call("note_leave", recipient="Alex", text="b")]
    owner_chat.use_model(
        FakeChatModel(tool_script=["change", rechain(rid, changed), "ok"], judge="do")
    )
    answer = await owner_chat.say("oui, mais écris plutôt b")
    assert answer.model_results[0]["outcome"] == "executed"
    assert await confirmation_status(rid) == "executed"
    assert [n.text for n in await stored_notes()] == ["b"]
    # Unless Domo asks again (more than the judge: his own yes): the chain then waits.
    again = (await owner_chat.say_with_tool(TOOL, to_alex)).pending_confirmation_id
    owner_chat.use_model(
        FakeChatModel(
            tool_script=["change", rechain(again, changed, ask_again=True), "D'accord ?"],
            judge="do",
        )
    )
    asked = (await owner_chat.say("oui, mais écris plutôt b")).model_results[0]
    assert asked["outcome"] == "asked_again"
    assert (asked["asked_by"], asked["why"]) == ("domo", "The assistant is not sure")
    assert await confirmation_status(again) == "replaced"
    assert await confirmation_status(asked["request_id"]) == "pending"
    assert len(await stored_notes()) == 1


async def test_a_removed_or_reordered_step_runs_under_the_yes_given(
    owner_chat, adult_member, child_member, stored_notes, confirmation_status, steps_rows
):
    two = chain(
        ("note_leave", {"recipient": "Alex", "text": "a"}),
        ("note_leave", {"recipient": "Sam", "text": "b"}),
    )
    removed = (await owner_chat.say_with_tool(TOOL, two)).pending_confirmation_id
    only_sam = [call("note_leave", recipient="Sam", text="b")]
    owner_chat.use_model(
        FakeChatModel(tool_script=["change", rechain(removed, only_sam), "ok"], judge="do")
    )
    answer = await owner_chat.say("oui, mais pas le mot à Alex")
    assert answer.model_results[0]["outcome"] == "executed"
    assert await confirmation_status(removed) == "executed"
    assert [n.text for n in await stored_notes()] == ["b"]
    # The note left as it was asked for keeps the role the chain remembers.
    rows = await steps_rows(removed)
    assert [(r.arguments["recipient"], r.status, r.with_basis) for r in rows] == [
        ("Sam", "done", True)
    ]
    reordered = (await owner_chat.say_with_tool(TOOL, two)).pending_confirmation_id
    sam_first = [
        call("note_leave", recipient="Sam", text="b"),
        call("note_leave", recipient="Alex", text="a"),
    ]
    owner_chat.use_model(
        FakeChatModel(tool_script=["change", rechain(reordered, sam_first), "ok"], judge="do")
    )
    await owner_chat.say("oui, mais d'abord Sam")
    assert await confirmation_status(reordered) == "executed"
    # The steps ran in their new order (the rows are the chain that ran, in order).
    rows = await steps_rows(reordered)
    assert [(r.arguments["recipient"], r.status, r.with_basis) for r in rows] == [
        ("Sam", "done", True),
        ("Alex", "done", True),
    ]
    assert sorted(n.text for n in await stored_notes()) == ["a", "b", "b"]


async def test_a_step_changed_once_does_not_get_the_remembered_role_back(
    owner_chat, child_member, adult_member, confirmation_status, steps_rows
):  # the foundation, spec 6.10
    first = (
        await owner_chat.say_with_tool(TOOL, SAM_AND_A_NOTE, judge="explicit_yes: big")
    ).pending_confirmation_id
    owner_chat.use_model(
        FakeChatModel(tool_script=["change", rechain(first, FIVE_DAYS), "Dis « je confirme »."])
    )
    second = (await owner_chat.say("oui, mais 5 jours")).model_results[0]["request_id"]
    # Kept as the first change made it, with another note: it stays judged with the role of now.
    other_note = [FIVE_DAYS[0], call("note_leave", recipient="Alex", text="b")]
    owner_chat.use_model(
        FakeChatModel(tool_script=["change", rechain(second, other_note), "Dis « je confirme »."])
    )
    third = (await owner_chat.say("oui, mais un autre mot")).model_results[0]["request_id"]
    assert await confirmation_status(second) == "replaced"
    rows = await steps_rows(third)
    assert [(r.tool_name, r.with_basis) for r in rows] == [
        ("household_change_role", False),
        ("note_leave", False),
    ]


# --- what a change may not do ---------------------------------------------------------------------


def note_step(recipient: Any, text: str) -> PlannedStep:
    """A note to ``recipient`` (a ``Person``), described, free."""
    action = DescribedAction("core", "note.leave", f"person:{recipient.id}", "message", "household")
    return PlannedStep(
        "note_leave",
        "note.leave",
        {"recipient": recipient.display_name, "text": text},
        action,
    )


async def chain_by_hand(
    pilot: Any,
    session_factory: Any,
    steps: list[PlannedStep],
    *,
    done: int,
    explicit: bool = False,
) -> uuid.UUID:
    """A chain of the pilot's that waits for their yes in their conversation, shown by the reply to
    a first message of theirs, with its first ``done`` steps done: as a chain leaves it when it
    waits again for a yes, half done."""
    shown = await pilot.say("Bonjour.")
    owner = pilot.person
    async with session_factory() as writer:
        conversation = await ConversationStore(writer).active_conversation(owner.id)
        c = await create_confirmation(
            writer,
            requester=Actor(owner.id, "owner"),
            tool_name=TOOL,
            arguments=chain(*((step.tool_name, step.arguments) for step in steps)),
            action=DescribedAction(
                "core",
                "chain.run",
                None,
                "permissions",
                "household",
                {"steps": len(steps), "checked_in_turn": 0},
            ),
            approver="self",
            asked=Asked("explicit_yes", "rule") if explicit else Asked("yes", "judge"),
            conversation_id=conversation.id,
            message_id=shown.message_id,
            now=pilot.clock(),
            chain_basis=ChainBasis("owner", frozenset()),
        )
        await add_steps(writer, c.id, steps)
        for position in range(done):
            await mark_step(
                writer,
                c.id,
                position,
                status="done",
                result={"key": "action.card.done", "params": {}},
                now=pilot.clock(),
            )
        await writer.commit()
        return c.id


async def test_the_steps_already_done_cannot_change(
    owner_chat, adult_member, session_factory, steps_rows, confirmation_status
):  # Review Focus 3
    alex = adult_member.person
    rid = await chain_by_hand(
        owner_chat, session_factory, [note_step(alex, "a"), note_step(alex, "b")], done=1
    )
    done = call("note_leave", recipient="Alex", text="a")
    owner_chat.use_model(
        FakeChatModel(
            tool_script=[
                "change",
                # The step done left out, then given otherwise.
                rechain(rid, [call("note_leave", recipient="Alex", text="b")]),
                rechain(
                    rid,
                    [
                        call("note_leave", recipient="Alex", text="z"),
                        call("note_leave", recipient="Alex", text="b"),
                    ],
                    call_id="c2",
                ),
                # Twenty steps after the one done: one too many.
                rechain(rid, [done, *[call("household_list")] * 20], call_id="c3"),
                "Hmm.",
            ]
        )
    )
    answer = await owner_chat.say("oui, mais seulement le second")
    assert (
        answer.actions
        == [{"tool": "confirmation_amend", "status": "invalid", "reason": "invalid"}] * 3
    )
    first, second, third = (result["detail"] for result in answer.model_results)
    assert first == second == "the steps already done cannot change: give them first, as they were"
    assert third.startswith("a chain has 1 to 20 steps")
    assert await confirmation_status(rid) == "pending"
    rows = await steps_rows(rid)
    assert [(r.status, r.arguments["text"]) for r in rows] == [("done", "a"), ("planned", "b")]


async def handed_over_chain(
    pilot: Any, alex: Any, session_factory: Any, set_role: Any
) -> uuid.UUID:
    """A chain of the owner (``pilot``) that waits again for their yes after its first step handed
    their role to Alex (done), with the list of successors still to set ([Sam]), as asked for when
    they were the owner; they are an adult now, and Alex the owner."""
    owner = pilot.person
    handed_over = PlannedStep(
        "household_transfer_ownership",
        "household.transfer_ownership",
        {"person": "Alex"},
        DescribedAction(
            "core",
            "household.transfer_ownership",
            f"person:{alex.id}",
            "permissions",
            "household",
            {
                "from": f"person:{owner.id}",
                "to": f"person:{alex.id}",
                "requester_role": "owner",
                "to_role": "adult",
            },
        ),
    )
    successors = PlannedStep(
        "successors_set",
        "successors.set",
        {"successors": [{"person": "Sam"}]},
        DescribedAction(
            "core", "successors.set", "successors", "permissions", "household", {"count": 1}
        ),
    )
    rid = await chain_by_hand(
        pilot,
        session_factory,
        [handed_over, successors],
        done=1,
        explicit=True,
    )
    # What the hand-over did.
    await set_role(owner, "adult")
    await set_role(alex, "owner")
    return rid


# The chain as the former owner asked for it.
AS_ASKED = [
    call("household_transfer_ownership", person="Alex"),
    call("successors_set", successors=[{"person": "Sam"}]),
]


async def test_a_kept_step_keeps_the_requests_role_and_a_changed_one_takes_the_role_of_now(
    owner_chat,
    adult_member,
    child_member,
    set_role,
    session_factory,
    steps_rows,
    confirmation_status,
):  # spec 4.1, last point
    owner = owner_chat.person
    rid = await handed_over_chain(owner_chat, adult_member.person, session_factory, set_role)
    with_a_note = [*AS_ASKED, call("note_leave", recipient="Sam", text="x")]
    owner_chat.use_model(
        FakeChatModel(tool_script=["change", rechain(rid, with_a_note), "Dis « je confirme »."])
    )
    answer = await owner_chat.say("oui, et laisse un mot à Sam")
    result = answer.model_results[0]
    # The list as it was asked for keeps the role of the request: it is asked again (explicit).
    assert result["outcome"] == "asked_again" and result["answer_with"] == ANSWER_EXPLICITLY
    assert await confirmation_status(rid) == "replaced"
    new = result["request_id"]
    rows = await steps_rows(new)
    assert [(r.tool_name, r.status, r.with_basis) for r in rows] == [
        ("household_transfer_ownership", "done", True),
        ("successors_set", "planned", True),
        ("note_leave", "planned", False),
    ]
    assert rows[0].action is not None and rows[0].action["facts"]["from"] == f"person:{owner.id}"
    # A list changed now is judged with the role of now (an adult): refused, step 2.
    other_list = [AS_ASKED[0], call("successors_set", successors=[])]
    owner_chat.use_model(FakeChatModel(tool_script=["change", rechain(new, other_list), "Non."]))
    refused = await owner_chat.say("oui, mais vide la liste")
    assert refused.model_results[0] == {
        "status": "done",
        "outcome": "refused",
        "reason": "not_allowed",
        "step": 2,
    }
    assert await confirmation_status(new) == "refused"


async def test_an_owner_only_step_added_after_the_hand_over_is_refused(
    owner_chat, adult_member, child_member, set_role, session_factory, confirmation_status
):  # the foundation, spec 6.10
    rid = await handed_over_chain(owner_chat, adult_member.person, session_factory, set_role)
    # The list as it was asked for, and a second change of the list, added now.
    one_more = [*AS_ASKED, call("successors_set", successors=[])]
    owner_chat.use_model(FakeChatModel(tool_script=["change", rechain(rid, one_more), "Non."]))
    answer = await owner_chat.say("oui, puis vide la liste")
    assert answer.model_results[0] == {
        "status": "done",
        "outcome": "refused",
        "reason": "not_allowed",
        "step": 3,
    }
    assert await confirmation_status(rid) == "refused"


async def test_a_change_the_barrier_refuses_or_sends_to_the_owner(
    adult_chat, owner_member, child_member, grant, person_row, confirmation_status
):
    alex = adult_chat.person
    # Alex may remove people, but not a child.
    await grant(
        "household.remove",
        person=alex,
        access="allow",
        limit='action.facts.current_role == "child" ? "refuse" : "allow"',
        summary="Alex may not remove a child.",
    )
    to_owner = chain(("note_leave", {"recipient": "Owner", "text": "a"}))
    refused = (await adult_chat.say_with_tool(TOOL, to_owner)).pending_confirmation_id
    removal = [
        call("note_leave", recipient="Owner", text="a"),
        call("household_remove", person="Sam"),
    ]
    adult_chat.use_model(FakeChatModel(tool_script=["change", rechain(refused, removal), "Non."]))
    answer = await adult_chat.say("oui, et retire Sam")
    assert answer.model_results[0] == {
        "status": "done",
        "outcome": "refused",
        "reason": "limit",
        "step": 2,
    }
    assert await confirmation_status(refused) == "refused"
    assert (await person_row(child_member.person)).disabled_at is None
    # Alex's invitations wait for the owner's yes: the changed chain waits for it.
    await grant("household.invite", person=alex, access="allow", approver="owner")
    rid = (await adult_chat.say_with_tool(TOOL, to_owner)).pending_confirmation_id
    invite = [
        call("note_leave", recipient="Owner", text="a"),
        call("household_invite", display_name="Jo", role="guest"),
    ]
    adult_chat.use_model(FakeChatModel(tool_script=["change", rechain(rid, invite), "Ok."]))
    answer = await adult_chat.say("oui, et invite Jo")
    result = answer.model_results[0]
    assert result["outcome"] == "approver_changed"
    assert result["answer_with"] == ANSWER_BY_THE_OWNER
    assert await confirmation_status(rid) == "replaced"
    assert await confirmation_status(result["request_id"]) == "pending"


async def test_the_change_is_the_whole_chain(
    owner_chat, adult_member, child_member, confirmation_status, steps_rows
):
    rid = (await owner_chat.say_with_tool(TOOL, SAM_AND_A_NOTE)).pending_confirmation_id
    nested = [call(TOOL, steps=[call("household_list")])]
    owner_chat.use_model(
        FakeChatModel(
            tool_script=[
                "change",
                amend(str(rid), {"guest_days": 5}),
                rechain(rid, nested, call_id="c2"),
                "Hmm.",
            ]
        )
    )
    answer = await owner_chat.say("oui, mais 5 jours")
    assert [action["status"] for action in answer.actions] == ["invalid", "invalid"]
    whole, flat = (result["detail"] for result in answer.model_results)
    assert (
        whole
        == "a chain is changed as a whole: give steps, the whole chain as the person wants it now"
    )
    assert "give the steps one by one" in flat
    assert await confirmation_status(rid) == "pending"
    assert [r.status for r in await steps_rows(rid)] == ["planned", "planned"]


# --- the pieces -----------------------------------------------------------------------------------


def row(position: int, tool: str, status: str = "planned", **arguments: Any) -> ConfirmationStep:
    """A stored step, as the database would hold it."""
    return ConfirmationStep(
        confirmation_id=uuid.uuid4(),
        position=position,
        tool_name=tool,
        action_type=tool,
        arguments=arguments,
        status=status,
        with_basis=True,
    )


def test_steps_are_matched_by_tool_in_order():
    old = [
        row(1, "note_leave", recipient="Alex", text="a"),
        row(2, "household_remove", person="Sam"),
        row(3, "note_leave", recipient="Sam", text="b"),
    ]
    new = [
        call("note_leave", recipient="Alex", text="a"),
        call("note_leave", recipient="Sam", text="c"),
        call("household_list"),
        call("household_remove", person="Sam"),
    ]
    assert match_steps(old, new) == (
        StepMatch("kept", 1),
        StepMatch("changed", 3),
        StepMatch("added", None),
        StepMatch("kept", 2),
    )
    done = [row(0, "note_leave", "done", recipient="Alex", text="z")]
    assert done_prefix_holds(done, [call("note_leave", recipient="Alex", text="z"), *new])
    assert not done_prefix_holds(done, [*new, call("note_leave", recipient="Alex", text="z")])
    assert not done_prefix_holds(done, [call("note_leave", recipient="Alex", text="y")])
    assert not done_prefix_holds(done, [])
    assert done_prefix_holds([], [])


def test_a_step_as_it_was_asked_for_is_kept_wherever_it_stands():
    alex, sam = (
        row(0, "note_leave", recipient="Alex", text="a"),
        row(1, "note_leave", recipient="Sam", text="b"),
    )
    to_alex, to_sam = (
        call("note_leave", recipient="Alex", text="a"),
        call("note_leave", recipient="Sam", text="b"),
    )
    # One taken away, or both in another order: the others are kept.
    assert match_steps([alex, sam], [to_sam]) == (StepMatch("kept", 1),)
    assert match_steps([alex, sam], [to_sam, to_alex]) == (
        StepMatch("kept", 1),
        StepMatch("kept", 0),
    )
    # A changed call takes the first step of its tool left once the kept ones are matched.
    other = call("note_leave", recipient="Sam", text="z")
    assert match_steps([alex, sam], [other, to_sam]) == (
        StepMatch("changed", 0),
        StepMatch("kept", 1),
    )
    # As many added as the calls of a tool beyond its steps.
    assert match_steps([alex], [other, to_alex, other]) == (
        StepMatch("added", None),
        StepMatch("kept", 0),
        StepMatch("added", None),
    )
    # The chain given back as it is (in the same order) is no change.
    assert unchanged([alex, sam], [to_alex, to_sam])
    assert not unchanged([alex, sam], [to_sam, to_alex])
    assert not unchanged([alex, sam], [to_alex])


def test_a_changed_chain_is_a_list_of_calls():
    assert calls_of([call("household_list")]) == [{"tool": "household_list", "arguments": {}}]
    for wrong in (
        None,
        {"tool": "household_list", "arguments": {}},
        [{"tool": "household_list"}],
        [{"tool": "household_list", "arguments": {}, "more": 1}],
        [{"tool": 1, "arguments": {}}],
        [call(TOOL, steps=[])],
    ):
        try:
            calls_of(wrong)
        except InvalidArguments as error:
            assert error.code == "invalid"
        else:
            raise AssertionError(f"accepted: {wrong!r}")


def test_the_chains_action_without_a_described_step():
    assert _chain_action(1, 1, [], ["household.transfer_ownership"]).effect == "permissions"
    action = _chain_action(1, 1, [], ["note.leave"])
    assert (action.effect, action.destination) == ("message", "household")
