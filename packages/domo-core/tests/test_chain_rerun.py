"""Continuing a chain that stopped from the step that failed (spec 4.4, 4.7, 8.5).

``chain_continue`` takes the rest of a chain up again: the step that failed, then the steps after
it, as they are or as Domo gives them (changed). The rest is planned again (the accord it needs
is the barrier's now, never copied from the request), then the yes-judge judges it with the
request's history: when and how its yes was given and by whom, the steps done, the step that
failed, why, and whether it may have acted (a fact the code tells), and what changed.

* The judge's ``do_not`` leaves it as it is, with the judge's reason.
* When nothing more is asked (the yes given covers what it was given to: the owner's rules and his
  default yeses count for the steps changed or added, and for a step kept only above what that
  yes gave) and that yes still covers the accord the rest needs, the rest runs under that yes, as
  after it, each step judged again in its turn.
* Otherwise it waits for a new yes, on one card that replaces the chain, saying that the step
  that failed may already have been done when it may have acted.

A step kept as it was asked for keeps the role the chain remembers; a step changed or added is
judged with the role of now (spec 4.1).

The chat pilots talk with Domo on a live server (see ``chat_support.py``).
"""

import json
import uuid
from datetime import timedelta
from typing import Any

import anyio
import domo_core.confirmations.chain as chain_run
import domo_core.conversation.execution as execution
import pytest
from chat_support import ChatPilot, status_of
from domo_core.accounts.models import Person
from domo_core.confirmations.chain import (
    CHECK_FAILED,
    KNOWN_NOT_DONE,
    MAYBE_DONE,
    chain_report,
    failed_step_may_have_acted,
)
from domo_core.confirmations.models import Confirmation, ConfirmationStep
from domo_core.confirmations.store import confirmations_by_id
from domo_core.conversation.execution import JUDGE_REFUSED_DETAIL
from domo_core.household.models import Invitation
from domo_core.llm.types import JsonObject, ToolCall, ToolDefinition
from domo_core.policy.actions import DescribedAction
from domo_core.policy.facts import decision_facts
from domo_core.policy.roles import Actor
from domo_core.tools.context import ToolContext, ToolOutcome
from domo_core.tools.registry import TOOLS
from domo_core.web.deps import get_tool_registry
from fakes import FakeChatModel
from sqlalchemy import select
from test_approver_in_words import changed_by, plain
from test_chain_run import CONTINUE, TOOL, chain, remove, steps_of_rows, stopped_chain
from test_judge_decides import judge_cases


def call(tool: str, **arguments: Any) -> dict[str, Any]:
    """A step left of a chain, as Domo gives it to ``chain_continue``."""
    return {"tool": tool, "arguments": arguments}


def not_found(reply: Any) -> bool:
    """Whether ``chain_continue`` found no chain it may continue under that id."""
    return reply.actions == [{"tool": CONTINUE, "status": "invalid", "reason": "not_found"}]


class SlowStep:
    """A step of the type ``profile.rename`` that takes ``delay`` seconds (2 at first): Domo
    gives up on it before it ends when ``conversation.execution._TOOL_SECONDS`` is shorter, and
    whether it was done is then not known. ``started`` counts how many times it started."""

    definition = ToolDefinition(
        name="slow_step",
        description="A slow step.",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
    )
    action_type = "profile.rename"

    def __init__(self) -> None:
        self.started = 0
        self.delay = 2.0

    async def describe(self, arguments: JsonObject, actor: Actor, ctx: Any) -> DescribedAction:
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=f"person:{actor.person_id}",
            effect="self_profile",
            destination="household",
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        self.started += 1
        await anyio.sleep(self.delay)
        return ToolOutcome("done", {}, {"key": "action.card.done", "params": {}})


async def a_step_that_timed_out(
    owner_chat: ChatPilot, grant: Any, monkeypatch: pytest.MonkeyPatch, *, last: bool = False
) -> tuple[uuid.UUID, SlowStep, Any]:
    """The owner's chain [a slow step, a note for Alex] (or the other way round, the slow step
    ``last``), waiting for the owner's yes (a right of the owner makes the owner's renames wait for
    one); at the yes, Domo gives up on the slow step: the chain stops there, and whether the step
    was done is not known. The request, the slow step and that right."""
    slow = SlowStep()
    registry = {**TOOLS, "slow_step": slow}
    owner_chat.app.dependency_overrides[get_tool_registry] = lambda: registry
    right = await grant("profile.rename", person=owner_chat.person)
    steps = [("slow_step", {}), ("note_leave", {"recipient": "Alex", "text": "x"})]
    reply = await owner_chat.say_with_tool(TOOL, chain(*(reversed(steps) if last else steps)))
    cid = reply.pending_confirmation_id
    assert cid is not None
    with monkeypatch.context() as patched:
        patched.setattr(execution, "_TOOL_SECONDS", 0.2)
        assert (await owner_chat.confirm_last(reply)).status == "failed"
    return cid, slow, right


def unknown_at(monkeypatch: pytest.MonkeyPatch, read: int) -> None:
    """The runner of a chain cannot read the one who asked at its ``read``-th reading (from 1):
    the step it was about to run stops the chain before it runs."""
    real = chain_run.read_the_person_again
    readings = 0

    async def reading(ctx: ToolContext) -> Any:
        nonlocal readings
        readings += 1
        return "unknown" if readings == read else await real(ctx)

    monkeypatch.setattr(chain_run, "read_the_person_again", reading)


# --- without a change, under the same yes ---------------------------------------------------------


async def test_chain_continue_reruns_a_step_refused_by_the_barrier_under_the_same_yes(
    adult_chat,
    owner_member,
    grant,
    revoke_right,
    stored_notes,
    count_invitations,
    confirmation_status,
    steps_rows,
):
    right = await grant("household.invite", person=adult_chat.person, access="allow")
    reply = await adult_chat.say_with_tool(
        TOOL,
        chain(
            ("household_invite", {"display_name": "Jo", "role": "guest"}),
            ("note_leave", {"recipient": "Owner", "text": "x"}),
        ),
    )
    cid = reply.pending_confirmation_id
    await revoke_right(right)
    # An adult invites by default: taking it away is saying no.
    no = await grant("household.invite", person=adult_chat.person, access="deny")
    assert (await adult_chat.confirm_last(reply)).status == "failed"
    rows = await steps_rows(cid)
    assert steps_of_rows(rows) == [(0, "failed"), (1, "stopped")]
    assert rows[0].result is not None and rows[0].result["reason"] == "not_allowed"
    # The owner gives the right again: the step that was refused runs first, under Alex's yes.
    await revoke_right(no)
    again = await adult_chat.say_with_tool(CONTINUE, {"request_id": str(cid)}, judge="do")
    assert again.actions == [{"tool": CONTINUE, "status": "done"}]
    assert await confirmation_status(cid) == "executed"
    assert steps_of_rows(await steps_rows(cid)) == [(0, "done"), (1, "done")]
    assert await count_invitations() == 1
    assert [note.text for note in await stored_notes()] == ["x"]
    result = again.model_results[-1]
    assert [(step["step"], step["status"]) for step in result["steps"]] == [
        (1, "done"),
        (2, "done"),
    ]


@pytest.mark.parametrize("failing", [1, 2], ids=["first step", "last step"])
async def test_a_step_that_failed_before_running_is_rerun_under_the_same_yes(
    failing,
    owner_chat,
    adult_member,
    child_member,
    monkeypatch,
    stored_notes,
    confirmation_status,
    steps_rows,
    person_row,
):
    reply = await owner_chat.say_with_tool(
        TOOL,
        chain(
            ("household_change_role", {"person": "Sam", "role": "adult"}),
            ("note_leave", {"recipient": "Alex", "text": "x"}),
        ),
    )
    cid = reply.pending_confirmation_id
    unknown_at(monkeypatch, failing)
    assert (await owner_chat.confirm_last(reply)).status == "failed"
    rows = await steps_rows(cid)
    stopped_at = rows[failing - 1]
    assert stopped_at.status == "failed"
    assert stopped_at.result is not None and stopped_at.result["reason"] == CHECK_FAILED
    assert not failed_step_may_have_acted(stopped_at)
    if failing == 2:
        # The last step failed: no step is left after it, and it is taken up again all the same.
        assert steps_of_rows(rows) == [(0, "done"), (1, "failed")]
    again = await owner_chat.say_with_tool(CONTINUE, {"request_id": str(cid)}, judge="do")
    assert again.actions == [{"tool": CONTINUE, "status": "done"}]
    assert await confirmation_status(cid) == "executed"
    assert steps_of_rows(await steps_rows(cid)) == [(0, "done"), (1, "done")]
    assert (await person_row(child_member.person)).role == "adult"
    assert [note.text for note in await stored_notes()] == ["x"]


# --- a step that may have acted -------------------------------------------------------------------


async def test_a_step_that_may_have_acted_is_not_rerun_without_a_new_yes(
    owner_chat, adult_member, grant, monkeypatch, steps_rows, confirmation_status, stored_notes
):
    cid, slow, _ = await a_step_that_timed_out(owner_chat, grant, monkeypatch)
    rows = await steps_rows(cid)
    assert steps_of_rows(rows) == [(0, "failed"), (1, "stopped")]
    assert rows[0].result is not None and rows[0].result["reason"] == "timeout"
    assert failed_step_may_have_acted(rows[0])

    continued = await owner_chat.say_with_tool(CONTINUE, {"request_id": str(cid)})
    result = continued.model_results[-1]
    assert (result["status"], result["chain"], result["maybe_done"], result["steps"]) == (
        "done",
        "waiting_for_confirmation",
        True,
        2,
    )
    assert result["by"] == "you"
    new = uuid.UUID(result["request_id"])
    assert continued.pending_confirmation_id == new
    assert await confirmation_status(cid) == "replaced"
    assert await confirmation_status(new) == "pending"
    # Nothing ran again: the new card says the step may already have been done.
    assert slow.started == 1 and await stored_notes() == []
    assert "data-maybe-done" in continued.cards_html
    assert "May already have been done" in continued.cards_html
    new_rows = await steps_rows(new)
    assert steps_of_rows(new_rows) == [(0, "planned"), (1, "planned")]
    assert new_rows[0].result == {MAYBE_DONE: True} and new_rows[1].result is None

    # Only the person's new yes runs it again.
    slow.delay = 0
    assert (await owner_chat.confirm_last(continued)).status == "executed"
    assert slow.started == 2
    assert [note.text for note in await stored_notes()] == ["x"]


async def test_what_domo_reads_says_whether_the_failed_step_may_have_acted(
    owner_chat, adult_member, child_member, grant, monkeypatch, session_factory
):
    timed_out, _, _ = await a_step_that_timed_out(owner_chat, grant, monkeypatch)
    refused, _ = await stopped_chain(owner_chat, adult_member.person)
    async with session_factory() as reader:
        found = await confirmations_by_id(reader, [timed_out, refused])
        after_a_timeout = await chain_report(reader, found[timed_out])
        after_a_refusal = await chain_report(reader, found[refused])
    assert after_a_timeout is not None and after_a_refusal is not None
    assert (after_a_timeout.failed_reason, after_a_timeout.failed_may_have_acted) == (
        "timeout",
        True,
    )
    assert (after_a_refusal.failed_reason, after_a_refusal.failed_may_have_acted) == (
        "no_longer_possible",
        False,
    )
    assert after_a_timeout.can_continue and after_a_refusal.can_continue


async def test_a_last_step_that_may_have_acted_is_taken_up_again_with_a_new_yes(
    owner_chat, adult_member, grant, monkeypatch, steps_rows, stored_notes, session_factory
):
    cid, slow, _ = await a_step_that_timed_out(owner_chat, grant, monkeypatch, last=True)
    rows = await steps_rows(cid)
    # The last step timed out: no step is left after it.
    assert steps_of_rows(rows) == [(0, "done"), (1, "failed")]
    async with session_factory() as reader:
        report = await chain_report(reader, (await confirmations_by_id(reader, [cid]))[cid])
    assert report is not None and report.can_continue and report.failed_may_have_acted

    continued = await owner_chat.say_with_tool(CONTINUE, {"request_id": str(cid)})
    result = continued.model_results[-1]
    assert (result["chain"], result["maybe_done"], result["steps"]) == (
        "waiting_for_confirmation",
        True,
        1,
    )
    new_rows = await steps_rows(uuid.UUID(result["request_id"]))
    assert steps_of_rows(new_rows) == [(0, "done"), (1, "planned")]
    assert new_rows[1].result == {MAYBE_DONE: True}
    assert slow.started == 1
    slow.delay = 0
    assert (await owner_chat.confirm_last(continued)).status == "executed"
    assert slow.started == 2 and len(await stored_notes()) == 1


async def test_a_step_that_may_have_acted_waits_for_a_new_yes_even_when_all_the_rest_is_free(
    owner_chat, adult_member, grant, revoke_right, monkeypatch, confirmation_status, stored_notes
):
    cid, slow, right = await a_step_that_timed_out(owner_chat, grant, monkeypatch)
    # The owner's right on renames is taken away: nobody else's agreement is needed now.
    await revoke_right(right)
    continued = await owner_chat.say_with_tool(
        CONTINUE,
        {
            "request_id": str(cid),
            "steps": [call("slow_step"), call("note_leave", recipient="Alex", text="x")],
        },
    )
    result = continued.model_results[-1]
    assert (result["chain"], result["maybe_done"]) == ("waiting_for_confirmation", True)
    assert await confirmation_status(uuid.UUID(result["request_id"])) == "pending"
    assert slow.started == 1 and await stored_notes() == []


def a_failed_row(result: JsonObject | None) -> ConfirmationStep:
    return ConfirmationStep(
        confirmation_id=uuid.uuid4(),
        position=0,
        tool_name="note_leave",
        action_type="note.leave",
        arguments={},
        status="failed",
        result=result,
    )


def test_a_failed_step_may_have_acted_unless_it_is_known_not_to():
    for reason in KNOWN_NOT_DONE:
        assert not failed_step_may_have_acted(a_failed_row({"key": "k", "reason": reason}))
    # A tool that failed or took too long, or a step whose reason is not known.
    for result in ({"reason": "failed"}, {"reason": "timeout"}, {"key": "k"}, None):
        assert failed_step_may_have_acted(a_failed_row(result))
    assert failed_step_may_have_acted(a_failed_row({"reason": ["not_allowed"]}))
    done = a_failed_row({"reason": "timeout"})
    done.status = "done"
    assert not failed_step_may_have_acted(done)
    assert {"check_failed", "no_longer_possible", "not_allowed", "invalid"} <= KNOWN_NOT_DONE
    assert "failed" not in KNOWN_NOT_DONE and "timeout" not in KNOWN_NOT_DONE


# --- with a change --------------------------------------------------------------------------------


async def test_a_childs_chain_taken_up_again_is_changed_by_the_child_not_by_the_adult(
    child_chat, adult_chat, owner_member, grant, monkeypatch, confirmation_status
):  # final review M3
    await grant("household.invite", person=child_chat.person, access="allow", approver="adult")
    tom = {"display_name": "Tom", "role": "guest", "guest_days": 7}
    reply = await child_chat.say_with_tool(
        TOOL, chain(("household_invite", tom), ("note_leave", {"recipient": "Owner", "text": "x"}))
    )
    cid = reply.pending_confirmation_id
    # Alex says yes with the button; the chain stops before its first step runs.
    with monkeypatch.context() as patched:
        unknown_at(patched, 1)
        answer = await adult_chat.post(f"/confirmations/{cid}/approve")
    assert answer.status_code == 200 and await confirmation_status(cid) == "failed"
    # Sam's Domo takes it up again, changed: a new request, which waits for an adult again.
    continued = await child_chat.say_with_tool(
        CONTINUE,
        {
            "request_id": str(cid),
            "steps": [
                call("household_invite", **{**tom, "guest_days": 3}),
                call("note_leave", recipient="Owner", text="x"),
            ],
        },
    )
    new = uuid.UUID(continued.model_results[-1]["request_id"])
    assert await confirmation_status(new) == "pending"
    # Sam changed it, not Alex, whose yes was to the chain before.
    sams_page = plain((await child_chat.get("/chat")).text)
    assert changed_by("confirmation.changed") in sams_page
    assert changed_by("confirmation.changed.by_adult") not in sams_page
    approvals = plain((await adult_chat.get("/approvals")).text)
    assert changed_by("confirmation.changed.by_requester") in approvals
    assert changed_by("confirmation.changed.by_adult") not in approvals


async def test_chain_continue_with_changed_steps_asks_the_person_again(
    owner_chat,
    adult_member,
    invite_for_test,
    confirmation_status,
    count_invitations,
    stored_notes,
    steps_rows,
    session_factory,
):  # spec 8.5
    reply = await owner_chat.say_with_tool(
        TOOL,
        chain(
            ("household_invite", {"display_name": "Léa", "role": "adult"}),
            ("note_leave", {"recipient": "Alex", "text": "x"}),
        ),
    )
    cid = reply.pending_confirmation_id
    await invite_for_test("Léa", "adult")  # the name is taken meanwhile
    assert (await owner_chat.confirm_last(reply)).status == "failed"
    rows = await steps_rows(cid)
    assert rows[0].result is not None and rows[0].result["reason"] == "no_longer_possible"

    continued = await owner_chat.say_with_tool(
        CONTINUE,
        {
            "request_id": str(cid),
            "steps": [
                call("household_invite", display_name="Léa B", role="adult"),
                call("note_leave", recipient="Alex", text="x"),
            ],
        },
    )
    result = continued.model_results[-1]
    assert (result["chain"], result["maybe_done"], result["steps"]) == (
        "waiting_for_confirmation",
        False,
        2,
    )
    new = uuid.UUID(result["request_id"])
    assert continued.pending_confirmation_id == new
    assert await confirmation_status(cid) == "replaced"
    # The changed step is judged with the role of now, the step kept with the request's.
    assert [(row.status, row.with_basis) for row in await steps_rows(new)] == [
        ("planned", False),
        ("planned", True),
    ]
    assert await count_invitations() == 1 and await stored_notes() == []
    async with session_factory() as reader:
        replaced_by = (
            await reader.execute(select(Confirmation.replaced_by).where(Confirmation.id == cid))
        ).scalar_one()
    assert replaced_by == new

    assert (await owner_chat.confirm_last(continued)).status == "executed"
    async with session_factory() as reader:
        found = await reader.execute(
            select(Invitation.display_name).where(Invitation.cancelled_at.is_(None))
        )
        names = sorted(found.scalars())
    assert names == ["Léa", "Léa B"]
    assert [note.text for note in await stored_notes()] == ["x"]


async def changed_to_a_free_rest(
    owner_chat: ChatPilot, sam: Person, judge: str = "yes"
) -> tuple[uuid.UUID, Any]:
    """The owner's chain [Sam an adult, a note for Sam]; Sam is removed before the yes, so it
    stops at once; Domo continues it changed to a note for Alex, which needs nobody else's
    agreement, and the judge says ``judge``. The request, and the reply of the continue."""
    reply = await owner_chat.say_with_tool(
        TOOL,
        chain(
            ("household_change_role", {"person": "Sam", "role": "adult"}),
            ("note_leave", {"recipient": "Sam", "text": "x"}),
        ),
    )
    cid = reply.pending_confirmation_id
    assert cid is not None
    await remove(owner_chat.server.session_factory, sam, owner_chat.clock)
    assert (await owner_chat.confirm_last(reply)).status == "failed"
    continued = await owner_chat.say_with_tool(
        CONTINUE,
        {"request_id": str(cid), "steps": [call("note_leave", recipient="Alex", text="x")]},
        judge=judge,
    )
    return cid, continued


async def test_a_changed_rest_the_judge_lets_through_runs_under_the_same_yes(
    owner_chat, child_member, adult_member, stored_notes, confirmation_status, confirmation_ask
):
    cid, continued = await changed_to_a_free_rest(owner_chat, child_member.person, judge="do")
    assert continued.pending_confirmation_id == cid
    assert await confirmation_status(cid) == "executed"
    assert [note.text for note in await stored_notes()] == ["x"]


async def test_a_changed_rest_the_judge_asks_a_yes_for_is_a_new_request(
    owner_chat, child_member, adult_member, stored_notes, confirmation_status, confirmation_ask
):
    cid, continued = await changed_to_a_free_rest(
        owner_chat, child_member.person, judge="yes: a new note"
    )
    result = continued.model_results[-1]
    assert (result["chain"], result["by"]) == ("waiting_for_confirmation", "you")
    assert (result["asked_by"], result["why"]) == ("judge", "a new note")
    assert await stored_notes() == []
    assert await confirmation_status(cid) == "replaced"
    new = result["request_id"]
    assert await confirmation_status(new) == "pending"
    assert await confirmation_ask(new) == ("yes", "judge", "a new note")


# --- judged again, with the request's history (spec 4.7) ------------------------------------------


async def test_a_step_that_may_have_acted_is_told_to_the_judge_and_resumes_under_the_same_yes(
    owner_chat, adult_member, grant, monkeypatch, steps_rows, confirmation_status, stored_notes
):  # scenario 12
    cid, slow, _ = await a_step_that_timed_out(owner_chat, grant, monkeypatch)
    slow.delay = 0
    judged = len(judge_cases(owner_chat.server))
    continued = await owner_chat.say_with_tool(
        CONTINUE, {"request_id": str(cid)}, judge="do: nothing was done"
    )
    [case] = judge_cases(owner_chat.server)[judged:]
    request = case["request"]
    assert (request["failed_reason"], request["may_have_acted"]) == ("timeout", True)
    assert request["yes"]["by"] == "the person" and request["yes"]["how"] == "button"
    assert request["yes"]["when"] == "just now"
    assert (request["done"], request["before"], request["after"]) == ([], [], [])
    assert request["failed"] is not None and request["asked"] == "yes"
    assert [step["type"] for step in case["steps"]] == ["profile.rename", "note.leave"]
    # The rest runs under the same yes: no new request.
    assert continued.pending_confirmation_id == cid
    assert await confirmation_status(cid) == "executed"
    assert steps_of_rows(await steps_rows(cid)) == [(0, "done"), (1, "done")]
    assert slow.started == 2 and [note.text for note in await stored_notes()] == ["x"]


async def test_the_judge_may_ask_a_new_yes_to_continue_and_the_card_says_maybe_done(
    owner_chat, adult_member, grant, monkeypatch, steps_rows, confirmation_ask, stored_notes
):
    cid, slow, _ = await a_step_that_timed_out(owner_chat, grant, monkeypatch)
    continued = await owner_chat.say_with_tool(
        CONTINUE, {"request_id": str(cid)}, judge="yes: check first"
    )
    result = continued.model_results[-1]
    assert (result["chain"], result["maybe_done"]) == ("waiting_for_confirmation", True)
    assert (result["asked_by"], result["why"]) == ("judge", "check first")
    new = uuid.UUID(result["request_id"])
    assert await confirmation_ask(new) == ("yes", "judge", "check first")
    new_rows = await steps_rows(new)
    assert new_rows[0].result == {MAYBE_DONE: True}
    assert "May already have been done" in continued.cards_html
    assert slow.started == 1 and await stored_notes() == []


async def test_a_continue_the_judge_refuses_leaves_the_chain_as_it_is(
    owner_chat, adult_member, grant, monkeypatch, confirmation_status, last_audit_entry
):  # spec 4.6
    cid, slow, _ = await a_step_that_timed_out(owner_chat, grant, monkeypatch)
    refused = await owner_chat.say_with_tool(
        CONTINUE, {"request_id": str(cid)}, judge="do_not: it may have been done"
    )
    assert refused.model_results[-1] == {
        "status": "done",
        "chain": "not_continued",
        "reason": "judge",
        "why": "it may have been done",
        "detail": JUDGE_REFUSED_DETAIL,
    }
    assert await confirmation_status(cid) == "failed"
    assert slow.started == 1
    entry = await last_audit_entry("judge.verdict")
    assert (entry.details["verdict"], entry.details["action_type"]) == ("do_not", "chain.run")


async def stopped_at_its_note(
    pilot: ChatPilot, monkeypatch: pytest.MonkeyPatch, names: tuple[str, str]
) -> uuid.UUID:
    """The pilot's chain [invite the first name, a note for Alex, invite the second], said yes to
    with the button; the note stops it before it runs (the person cannot be read then): the first
    invitation is done, the note failed (known not to have acted), the second stopped."""
    first, second = names
    reply = await pilot.say_with_tool(
        TOOL,
        chain(
            ("household_invite", {"display_name": first, "role": "guest"}),
            ("note_leave", {"recipient": "Alex", "text": "x"}),
            ("household_invite", {"display_name": second, "role": "guest"}),
        ),
    )
    cid = reply.pending_confirmation_id
    assert cid is not None
    with monkeypatch.context() as patched:
        unknown_at(patched, 2)
        assert (await pilot.confirm_last(reply)).status == "failed"
    return cid


async def test_a_continued_chain_asks_again_only_for_what_the_yes_never_covered(
    owner_chat,
    adult_member,
    grant,
    monkeypatch,
    steps_rows,
    confirmation_status,
    confirmation_ask,
    count_invitations,
):  # F2 (4): a yes given covers what it was given to
    await grant("household.invite", person=owner_chat.person, ask="explicit_yes")
    first = await stopped_at_its_note(owner_chat, monkeypatch, ("Jo", "Lou"))
    assert await confirmation_ask(first) == ("explicit_yes", "rule", None)
    assert steps_of_rows(await steps_rows(first)) == [(0, "done"), (1, "failed"), (2, "stopped")]
    # As it is: the invitations were covered by the explicit yes given; it runs under it.
    again = await owner_chat.say_with_tool(CONTINUE, {"request_id": str(first)}, judge="do")
    assert again.pending_confirmation_id == first
    assert await confirmation_status(first) == "executed"
    assert await count_invitations() == 2
    # With an invitation added, the rule asks its explicit yes for it.
    second = await stopped_at_its_note(owner_chat, monkeypatch, ("Max", "Noa"))
    more = await owner_chat.say_with_tool(
        CONTINUE,
        {
            "request_id": str(second),
            "steps": [
                call("note_leave", recipient="Alex", text="x"),
                call("household_invite", display_name="Noa", role="guest"),
                call("household_invite", display_name="Zoé", role="guest"),
            ],
        },
        judge="do",
    )
    result = more.model_results[-1]
    assert result["chain"] == "waiting_for_confirmation"
    assert await confirmation_status(second) == "replaced"
    assert await confirmation_ask(result["request_id"]) == ("explicit_yes", "rule", None)
    assert await count_invitations() == 3


async def test_a_continued_chain_counts_a_rule_added_after_its_yes_above_what_that_yes_gave(
    owner_chat,
    adult_member,
    grant,
    revoke_right,
    monkeypatch,
    confirmation_status,
    confirmation_ask,
    count_invitations,
):  # final review M14: the yes given covers what it gave, not a rule asking more since
    first = await stopped_at_its_note(owner_chat, monkeypatch, ("Jo", "Lou"))
    second = await stopped_at_its_note(owner_chat, monkeypatch, ("Max", "Noa"))
    assert await count_invitations() == 2
    # After the button, the owner asks for the button before any invitation: the yes gave it.
    rule = await grant("household.invite", ask="explicit_yes", summary="Confirm invitations.")
    again = await owner_chat.say_with_tool(CONTINUE, {"request_id": str(first)}, judge="do")
    assert again.pending_confirmation_id == first
    assert await confirmation_status(first) == "executed"
    assert await count_invitations() == 3
    # Then for a proof: the button never gave one, so the rest asks it, for the steps kept too.
    await revoke_right(rule)
    await grant("household.invite", ask="proof", summary="Prove it before inviting.")
    more = await owner_chat.say_with_tool(CONTINUE, {"request_id": str(second)}, judge="do")
    result = more.model_results[-1]
    assert result["chain"] == "waiting_for_confirmation"
    assert await confirmation_status(second) == "replaced"
    assert await confirmation_ask(result["request_id"]) == ("proof", "rule", None)
    assert await count_invitations() == 3


async def test_domo_may_ask_more_to_take_a_chain_up_again(
    owner_chat, adult_member, grant, monkeypatch, confirmation_ask, count_invitations
):  # F15: what Domo asks more reaches what chain_continue carries
    first = await stopped_at_its_note(owner_chat, monkeypatch, ("Jo", "Lou"))
    owner_chat.use_model(
        FakeChatModel(
            tool_script=[
                ToolCall(
                    "a1", "ask_before_acting", json.dumps({"ask": "yes", "reason": "a while ago"})
                ),
                ToolCall("c1", CONTINUE, json.dumps({"request_id": str(first)})),
                "D'accord ?",
            ],
            judge="do",
        )
    )
    reply = await owner_chat.say("reprends-la")
    result = reply.model_results[-1]
    assert result["chain"] == "waiting_for_confirmation"
    assert await confirmation_ask(result["request_id"]) == ("yes", "domo", "a while ago")
    assert await count_invitations() == 1


async def test_the_rest_of_a_childs_chain_asks_the_barrier_whose_accord_it_needs(
    child_chat,
    adult_chat,
    owner_member,
    grant,
    monkeypatch,
    session_factory,
    confirmation_status,
    count_invitations,
    stored_notes,
):  # spec 5.5.1
    await grant("household.invite", person=child_chat.person, access="allow", approver="adult")

    async def stopped(text: str, name: str) -> uuid.UUID:
        """Sam's chain [a note for the owner, invite ``name``], to which Alex said yes; the note
        stops it before it runs."""
        reply = await child_chat.say_with_tool(
            TOOL,
            chain(
                ("note_leave", {"recipient": "Owner", "text": text}),
                ("household_invite", {"display_name": name, "role": "guest"}),
            ),
        )
        cid = reply.pending_confirmation_id
        assert cid is not None
        with monkeypatch.context() as patched:
            unknown_at(patched, 1)
            assert status_of(await adult_chat.post(f"/confirmations/{cid}/approve")) == "failed"
        return cid

    # Alex said yes and is still here: the rest, which needs an adult, runs under that yes.
    first = await stopped("a", "Tom")
    again = await child_chat.say_with_tool(CONTINUE, {"request_id": str(first)}, judge="do")
    assert again.pending_confirmation_id == first
    assert await confirmation_status(first) == "executed"
    assert await count_invitations() == 1
    # Alex left: what is left, a note, needs nobody else's accord, and runs under Sam's own yes.
    second = await stopped("b", "Lou")
    await remove(session_factory, adult_chat.person, child_chat.clock)
    free = await child_chat.say_with_tool(
        CONTINUE,
        {"request_id": str(second), "steps": [call("note_leave", recipient="Owner", text="b")]},
        judge="do",
    )
    assert free.pending_confirmation_id == second
    assert await confirmation_status(second) == "executed"
    assert sorted(note.text for note in await stored_notes()) == ["a", "b"]
    assert await count_invitations() == 1


async def test_a_changed_rest_is_judged_with_the_role_of_now_and_a_kept_one_with_the_requests(
    owner_chat,
    adult_member,
    invite_for_test,
    steps_rows,
    person_row,
    current_settings,
    confirmation_status,
):
    reply = await owner_chat.say_with_tool(
        TOOL,
        chain(
            ("household_transfer_ownership", {"person": "Alex"}),
            ("household_invite", {"display_name": "Jo", "role": "adult"}),
            ("policy_update", {"key": "confirmation_max_lifetime_hours", "value": 48}),
        ),
    )
    cid = reply.pending_confirmation_id
    await invite_for_test("Jo", "adult")  # the name is taken meanwhile
    assert (await owner_chat.confirm_last(reply)).status == "failed"
    assert steps_of_rows(await steps_rows(cid)) == [(0, "done"), (1, "failed"), (2, "stopped")]
    assert (await person_row(owner_chat.person)).role == "adult"
    rule = call("policy_update", key="confirmation_max_lifetime_hours", value=48)

    # Changed, the step is judged with the role of now: an adult does not read the rules.
    refused = await owner_chat.say_with_tool(
        CONTINUE,
        {
            "request_id": str(cid),
            "steps": [call("policy_show"), rule],
        },
    )
    assert refused.actions == [{"tool": CONTINUE, "status": "invalid", "reason": "invalid"}]
    assert refused.model_results[-1]["detail"] == "step 2: refused (not_allowed)"
    assert await confirmation_status(cid) == "failed"

    # Kept as it was asked for, the change of the rules keeps the role of the request; an added
    # note is judged with the role of now (free for an adult).
    continued = await owner_chat.say_with_tool(
        CONTINUE,
        {"request_id": str(cid), "steps": [call("note_leave", recipient="Alex", text="x"), rule]},
    )
    new = continued.pending_confirmation_id
    assert new is not None and new != cid
    assert [(row.status, row.with_basis) for row in await steps_rows(new)] == [
        ("done", True),
        ("planned", False),
        ("planned", True),
    ]
    assert (await owner_chat.confirm_last(continued)).status == "executed"
    assert (await current_settings()).max_lifetime == timedelta(hours=48)


async def test_a_kept_step_free_only_under_the_role_of_the_request_waits_for_a_new_yes(
    owner_chat, adult_member, invite_for_test, steps_rows, confirmation_status, person_row
):
    reply = await owner_chat.say_with_tool(
        TOOL,
        chain(
            ("household_transfer_ownership", {"person": "Alex"}),
            ("household_invite", {"display_name": "Jo", "role": "adult"}),
            ("policy_show", {}),
        ),
    )
    cid = reply.pending_confirmation_id
    await invite_for_test("Jo", "adult")  # the name is taken meanwhile
    assert (await owner_chat.confirm_last(reply)).status == "failed"
    assert (await person_row(owner_chat.person)).role == "adult"
    # Reading the rules is free for the owner the chain remembers, and not something an adult
    # does: the rest does not run at once (it would be judged with the role of now), it waits.
    continued = await owner_chat.say_with_tool(
        CONTINUE, {"request_id": str(cid), "steps": [call("policy_show")]}
    )
    result = continued.model_results[-1]
    assert (result["chain"], result["maybe_done"], result["steps"]) == (
        "waiting_for_confirmation",
        False,
        1,
    )
    new = uuid.UUID(result["request_id"])
    assert [(row.status, row.with_basis) for row in await steps_rows(new)] == [
        ("done", True),
        ("planned", True),
    ]
    # Its yes runs it under the role of the request.
    assert (await owner_chat.confirm_last(continued)).status == "executed"
    assert await confirmation_status(cid) == "replaced"


async def test_the_steps_done_count_once_in_the_history_when_a_new_request_replaces_the_chain(
    owner_chat, adult_member, invite_for_test, session_factory
):
    reply = await owner_chat.say_with_tool(
        TOOL,
        chain(
            ("note_leave", {"recipient": "Alex", "text": "a"}),
            ("household_invite", {"display_name": "Jo", "role": "adult"}),
        ),
    )
    await invite_for_test("Jo", "adult")  # the name is taken meanwhile
    assert (await owner_chat.confirm_last(reply)).status == "failed"
    continued = await owner_chat.say_with_tool(
        CONTINUE,
        {
            "request_id": str(reply.pending_confirmation_id),
            "steps": [call("household_invite", display_name="Jo B", role="adult")],
        },
    )
    # The new request holds the note done, copied: it was left once.
    assert continued.model_results[-1]["chain"] == "waiting_for_confirmation"
    note = DescribedAction(
        connector="core",
        action_type="note.leave",
        target=f"person:{adult_member.person.id}",
        effect="message",
        destination="household",
    )
    owner = owner_chat.person
    async with session_factory() as reader:
        facts = await decision_facts(
            reader, Actor(owner.id, "owner"), note, owner_chat.clock(), None
        )
    assert facts["history"] == {"day": 1, "week": 1, "month": 1}


async def test_a_replaced_chain_cannot_be_continued_twice(
    owner_chat, child_member, adult_member, stored_notes
):
    cid, _ = await changed_to_a_free_rest(owner_chat, child_member.person)
    for arguments in (
        {"request_id": str(cid)},
        {"request_id": str(cid), "steps": [call("note_leave", recipient="Alex", text="x")]},
    ):
        assert not_found(await owner_chat.say_with_tool(CONTINUE, arguments))
    assert await stored_notes() == []


@pytest.mark.parametrize(
    "arguments",
    [
        {"steps": []},
        {"request_id": "x", "other": 1},
        {"request_id": str(uuid.uuid4()), "steps": "all"},
        {"request_id": str(uuid.uuid4()), "steps": [{"tool": "note_leave"}]},
        {"request_id": str(uuid.uuid4()), "steps": [call(TOOL, steps=[])]},
    ],
)
async def test_chain_continue_reads_its_arguments(arguments, owner_chat):
    reply = await owner_chat.say_with_tool(CONTINUE, arguments)
    assert reply.actions == [{"tool": CONTINUE, "status": "invalid", "reason": "invalid"}]
