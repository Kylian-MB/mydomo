"""The run of a chain of actions (spec 4.3 and 4.4): after the yes to its request, or at once when
the judge asks none (``tools.chain.DoInOrder``), and again when Domo continues the rest of
a chain that stopped (``tools.chain.ChainContinue``).

``run_steps`` does for each step, in order, what a yes does for a request:

0. the one who asked is read again, as the database holds them now
   (``conversation.execution.read_the_person_again``): someone who is no longer in the household
   stops the chain (``no_longer_possible``), and nothing more is done for them; someone who cannot
   be read stops it before the step runs (``CHECK_FAILED``). Then a step that waits for someone's
   arrival (``PlannedStep.after_arrival_of``, the name of their invitation, spec 4.3b) looks for
   that invitation (``_Runner._arrival``): the one the request waited for last
   (``Confirmation.waiting_for_invitation_id``) if it has that name (the invitation whose
   acceptance resumed it), else the last one a step done of this chain made under that name (the
   invitation on its card), else the one invitation of that name that waits in the household, if
   the one who asked made it or it already waited when the yes was given (``_open_invitation``).
   Two waits of a chain never mix their invitations: each has its name. One that ended
   (cancelled, or past its date) gives way only to an invitation of that name that waits and that
   the one who asked made (a new link of theirs): never to one someone else made since, whose
   link the one who asked does not hold. Accepted: the person arrived, and the step goes on.
   Still open: the chain pauses there (``_pause``: the request is ``waiting_arrival``, waiting
   for that invitation, ``chain.paused`` is audited), and the upkeep resumes it once the
   invitation is accepted (``confirmations.arrivals``). Ended, or none: the chain stops there
   (``NOT_ARRIVED``). A chain that runs at once never waits: nobody said yes to it
   (``needs_confirmation``);
1. the step is described again by its tool, with the facts of now (Ruling R6), within
   ``_DESCRIBE_SECONDS``, for the person as the chain judges them
   (``policy.decision.judged_actor``): a tool that always refuses some situations (handing the
   owner's role over by anyone but the owner of now, changing the owner's role) refuses them on
   the facts of now, whatever role the chain remembers. A call its tool no longer accepts, that
   fails or takes too long to describe, or that now describes another type of action, or another
   target than the action stored when the chain was asked for, stops the chain:
   ``no_longer_possible``. A step checked in its turn has no stored action: its target is first
   known now, and the action it is described as now is stored on its row once it is done;
2. it goes through the barrier: for a request, under the rule of the chain
   (``policy.decision.decide_in_chain``, with what the request remembers of the one who asked,
   ``ChainBasis``, and the actions of its steps done so far), for a step as it was asked for
   (``PlannedStep.with_basis``); a step changed or added when the chain was changed is judged,
   and described, for the person as they are now (``decide_now``: spec 4.1, last point); for a
   chain that runs at once, as any call of the person's (``decide_now``), the steps it did so far
   counting in the history a limit reads. Rights or facts that cannot be read stop the chain
   before the step runs (``CHECK_FAILED``). A ``Refuse`` stops the chain, with its reason. A
   ``NeedsApproval`` stops a chain that runs at once, and a chain the judge let through under a
   request (``decided_via`` ``judge``): nobody said yes to it (``needs_confirmation``, spec 4.7,
   F28). In a request somebody said yes to, the yes of the chain covers the step
   (``confirmations.store.covers``), unless the barrier now asks for a more demanding approver
   than the one who said yes (a right changed meanwhile): the request then waits for that yes
   (Ruling R27), as ``confirmations.service`` reroutes a request, with its steps done so far kept
   (``waiting``); the yes of the right person runs the rest;
3. it runs, as a call the barrier allowed (``conversation.execution.execute_allowed``, which
   limits its time), in the context of the one who asked, told the action it was described as now
   (``ToolContext.described_action``) and that it is a step of a chain (``ToolContext.in_a_chain``:
   a link or a form it makes for the one who asked is never shown at once, but left behind a
   button on the chain's card, spec 4.5); then, in a transaction of its own, the request's row of
   the step is marked with what came of it (``mark_step``, with its card) and the barrier's
   decision is audited for the person as judged (``barrier.record_decision``), committed. A step
   that does not end ``done`` stops the chain.

When the chain stops, the step that stopped it is ``failed`` (with a card whose ``reason`` says
why), the steps after it are ``stopped``, and ``chain.stopped`` is audited (``{"confirmation_id",
"step", "reason"}``, the step counted from 1), with the barrier's decision on the step when it gave
one, in one transaction. What was done stays done. The step that stopped it is known not to have
acted when it never ran, or its tool rejected it (``KNOWN_NOT_DONE``), or when it is a step that
never acts in a chain (``KNOWN_NOT_DONE_TYPES``: a key given again, a new key or an admin key,
which only leaves a button);
otherwise (its tool failed, or took too long) it may have acted (``failed_step_may_have_acted``):
the judge is told so, as a fact, before the chain is taken up again (spec 4.4, 4.7).

The rows of a request's steps (``confirmations.steps``) are the source of truth: which steps are
done, failed, stopped or still to run (``stored_steps``), and the actions of the steps done, which
the rule of the chain reads in a later run (a chain continued, or one that waited again for
someone else's yes). A run does the steps that are ``planned`` or ``stopped``, never one that is
``done`` or ``failed`` (a failed step known not to have acted is made ``stopped`` again before a
chain is continued). Every record of a step has its own time limit (``_STORE_SECONDS``); a
record that fails ends the run with that error (the caller logs it): the step it was about stays
as it was, and a chain with a step whose record is not known is never continued
(``ChainContinue``), so that no step runs twice.

``chain_outcome`` is what the tool of the chain tells the model and the people of a run: the
steps, and for a chain that stopped where it stopped, why, how many are left, and what Domo may
do (``STOPPED_DETAIL``, or ``STOPPED_AT_ONCE_DETAIL`` for a chain that ran at once: it has no
request to take up again); for a chain that paused, whom it waits for (``WAITING_FOR_ARRIVAL``).
Its card lists the types of the steps it did (``"did"``): the history a limit reads counts the
steps of a chain that ran at once by it (``policy.facts``); and the buttons its steps done left
for the one who asked (``"buttons"``, ``steps.StepButton``: an invitation whose link they show,
the provider whose key's form they open), those of an earlier run of the same request included.
Identifiers only: no link, token or form is ever in a card, nor in what the model reads, and a
chain's run has no part shown once (``ToolOutcome.card_once``).

``just_decided`` and ``chain_report`` are what Domo is told once a request was decided, for the
turn that follows (``prompts.JustDecided`` and ``prompts.ChainReport``): the request in English
sentences, who decided and how, what the model would have read of the action, and, for a chain,
its steps as its rows say them now (done, the one that stopped it, why, and whether it may have
acted, those left, and whether ``chain_continue`` may take it up again: ``can_be_continued``).
``request_told`` is what the yes-judge is told of a request changed in words or of a chain taken
up again (spec 4.7): its yes, by whom (``who_decided``) and how, what it asked, its steps done, the
one that failed and whether it may have acted, and what changed.
"""

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import TYPE_CHECKING, Final, Literal

import anyio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.audit.log import append_audit
from domo_core.chat.agent import ActionRecord
from domo_core.confirmations.describe import (
    description_for_a_model,
    request_description,
    step_sentence,
    step_sentence_for_a_model,
)
from domo_core.confirmations.judge import RequestTold, YesTold
from domo_core.confirmations.models import Confirmation, ConfirmationStep
from domo_core.confirmations.steps import (
    MAYBE_DONE,
    PlannedStep,
    StepButton,
    button_of,
    mark_step,
    steps_of,
)
from domo_core.confirmations.store import action_type_of, covers, requester_of
from domo_core.conversation.execution import (
    LIMIT_ERROR_DETAIL,
    WHO_SAYS_YES,
    execute_allowed,
    read_the_person_again,
)
from domo_core.household.models import Invitation
from domo_core.household.service import is_open, resolve_invitation
from domo_core.llm.types import JsonObject
from domo_core.policy.actions import DescribedAction
from domo_core.policy.barrier import (
    Allow,
    Decision,
    NeedsApproval,
    Refuse,
    record_decision,
)
from domo_core.policy.catalog import is_made
from domo_core.policy.decision import ChainBasis, decide_in_chain, decide_now, judged_actor
from domo_core.policy.roles import Actor
from domo_core.prompts import ChainReport, JustDecided
from domo_core.text import name_key
from domo_core.texts.view import REFERENCE
from domo_core.tools.context import InternalTool, InvalidArguments, ToolContext, ToolOutcome

if TYPE_CHECKING:
    from domo_core.confirmations.service import AmendOutcome, DecideOutcome

logger = logging.getLogger(__name__)

# How long a step's tool may take to describe it again (as a stored call at a yes:
# ``confirmations.service``); reading the rights and the facts for its decision; and recording
# what came of it.
_DESCRIBE_SECONDS: Final = 60.0
_CHECK_SECONDS: Final = 15.0
_STORE_SECONDS: Final = 15.0

# The steps a run does: those still to do, and those a stop left.
_TO_RUN: Final = frozenset({"planned", "stopped"})

# What the model is told when a chain stopped (spec 4.4): the choice is Domo's.
STOPPED_DETAIL: Final = (
    "the chain stopped: take it up again from the step that failed with chain_continue, drop it, "
    "or undo what was done with new actions; what was done stays done"
)
# What the model is told when a chain that ran at once stopped: it has no request, so
# chain_continue has nothing to take up again.
STOPPED_AT_ONCE_DETAIL: Final = (
    "the chain stopped, and it ran at once, with no request to take up again: if the person "
    "still wants the rest, do it with new calls; or undo what was done with new actions; what was "
    "done stays done"
)

# Why a step stopped before it ran, when a check before it could not be made: the one who asked
# could not be read again, or the rights and the facts for its decision, or the invitation it waits
# for, could not be read. Nothing ran (a tool that failed while it ran is ``failed``).
CHECK_FAILED: Final = "check_failed"
# Why a step that waits for someone's arrival stopped the chain: their invitation ended (cancelled,
# or past its date) without them, or none has that name. Nothing ran.
NOT_ARRIVED: Final = "not_arrived"
# The reasons of a step that stopped the chain and is known not to have acted (spec 4.4): it never
# ran, refused by the barrier (``not_allowed``, ``untrusted_identity``, ``limit``,
# ``limit_error``), no longer what was asked for (``no_longer_possible``), with nobody's yes
# (``needs_confirmation``), waiting for an arrival that did not come (``not_arrived``), or stopped
# by a check before it (``CHECK_FAILED``); or its tool rejected its arguments (the codes of
# ``InvalidArguments``). Any other reason (``failed``: its tool failed; ``timeout``: it took too
# long), or none, may have acted.
KNOWN_NOT_DONE: Final = frozenset(
    {
        "not_allowed",
        "untrusted_identity",
        "limit",
        "limit_error",
        "no_longer_possible",
        "needs_confirmation",
        NOT_ARRIVED,
        CHECK_FAILED,
        "invalid",
        "ambiguous",
        "not_found",
        "too_long",
        "not_possible",
    }
)
# The types of step that never act in a chain, whatever stopped them: a key given again, a new
# key or an admin key, which a chain only leaves behind a button on the card of the one who asked
# (``ToolContext.in_a_chain``, ``tools.model_key``, ``tools.key_tools``, ``tools.spending_tools``);
# the vault is asked for a form only when they press it.
KNOWN_NOT_DONE_TYPES: Final = frozenset({"model.rekey", "key.add", "key.admin"})
# What the model reads of a chain that waits for someone else's yes again, half done.
WAITING_FOR_ANOTHER_YES: Final = "waiting_for_another_yes"
# What the model reads of a chain that paused at a step that waits for someone's arrival.
WAITING_FOR_ARRIVAL: Final = "waiting_for_arrival"
# What the model is told of a chain that paused (spec 4.3b).
PAUSED_DETAIL: Final = (
    "the rest runs by itself once they have joined; if their link ends first, the chain stops and "
    "you are told"
)
# The steps left of a chain that stopped: the one that failed, and those it stopped.
STEPS_LEFT: Final = frozenset({"failed", "stopped"})


@dataclass(frozen=True)
class StepDone:
    """A step that ran, or that stopped the chain: its ``position`` (from 0) and ``action_type``,
    ``done`` or ``failed``; ``reason`` is why it failed (the barrier's refusal,
    ``no_longer_possible``, ``needs_confirmation``, ``CHECK_FAILED``, or the reason of its record:
    ``invalid``, ``timeout``...; ``failed`` when nothing more is known), and ``model_result`` what
    the model reads of it."""

    position: int
    action_type: str
    status: Literal["done", "failed"]
    reason: str | None
    model_result: JsonObject

    def told(self) -> JsonObject:
        """The step as the model reads it, numbered from 1."""
        told: JsonObject = {
            "step": self.position + 1,
            "action_type": self.action_type,
            "status": self.status,
        }
        if self.reason is not None:
            told["reason"] = self.reason
        told["result"] = dict(self.model_result)
        return told


@dataclass(frozen=True)
class ChainRun:
    """What came of a run of a chain: ``done`` when every step it did ended ``done``; ``failed``
    when a step stopped it (``failed``, with the positions of the steps it did not run in
    ``left``); ``waiting`` when its request waits for someone else's yes again (``need``: whose;
    ``left``: the steps still to run, from the one that asked for it);
    ``paused`` when its request waits for someone's arrival (``waiting_for``: the name of their
    invitation; ``left``: the steps still to run, from the one that waits).
    ``done`` are the steps this run did, in order; ``earlier`` how many an earlier run of the same
    request did (a chain continued after it stopped). ``buttons`` are what the steps done left for
    the one who asked, each behind its own button on the chain's card, in the order of the steps:
    this run's, and an earlier run's of the same request (their rows keep them)."""

    status: Literal["done", "failed", "waiting", "paused"]
    done: tuple[StepDone, ...]
    failed: StepDone | None
    left: tuple[int, ...]
    need: NeedsApproval | None
    earlier: int = 0
    buttons: tuple[StepButton, ...] = ()
    waiting_for: str | None = None


def waits_again(record: ActionRecord) -> bool:
    """Whether ``record`` is the run of a chain whose request went back to waiting on the way, for
    someone else's yes or for someone's arrival (``paused``): it is not over, and what came of it
    so far is not settled."""
    return record.model_result.get("chain") in (WAITING_FOR_ANOTHER_YES, WAITING_FOR_ARRIVAL)


def paused(record: ActionRecord) -> bool:
    """Whether ``record`` is the run of a chain that paused at a step that waits for someone's
    arrival (``WAITING_FOR_ARRIVAL``)."""
    return record.model_result.get("chain") == WAITING_FOR_ARRIVAL


def not_arrived_card(name: str) -> JsonObject:
    """The card of a step that waited for the arrival of ``name`` (their invitation's name), whose
    invitation ended without them: the chain stopped there (``NOT_ARRIVED``)."""
    return {
        "key": "action.card.failed.chain.not_arrived",
        "params": {"name": name},
        "reason": NOT_ARRIVED,
    }


def failed_step_may_have_acted(row: ConfirmationStep) -> bool:
    """Whether ``row`` is a step that stopped its chain (``failed``) and may have acted: the
    reason on its card is not one of those known not to have acted (``KNOWN_NOT_DONE``), or it
    has none. A step that enters a key again, adds one or gives an admin key
    (``KNOWN_NOT_DONE_TYPES``) never acted, whatever its reason: in a chain it only leaves a
    button on the card of the one who asked, and never touches the vault (spec 5.5.2, a fact of
    the code)."""
    if row.status != "failed":
        return False
    if row.action_type in KNOWN_NOT_DONE_TYPES:
        return False
    reason = None if row.result is None else row.result.get("reason")
    return not isinstance(reason, str) or reason not in KNOWN_NOT_DONE


def can_be_continued(c: Confirmation, rows: Sequence[ConfirmationStep]) -> bool:
    """Whether the chain ``c``, whose steps are ``rows``, may be taken up again
    (``tools.chain.ChainContinue``), as far as its rows tell: it ended ``failed``, no step's
    record is unknown (none is ``planned``: no step runs twice), and it has steps left
    (``STEPS_LEFT``: the one that failed, and those it stopped). A chain whose last step failed is
    taken up again too: that step runs again under the same yes only when it is known not to have
    acted, and with a new yes otherwise (``failed_step_may_have_acted``)."""
    states = {row.status for row in rows}
    return c.status == "failed" and "planned" not in states and bool(states & STEPS_LEFT)


async def mark_maybe_done(
    session: AsyncSession, request_id: uuid.UUID, position: int, *, now: datetime
) -> None:
    """Mark the step at ``position`` of the request ``request_id``, still to run, as one that may
    already have been done (``MAYBE_DONE``): its card says so. Flushed, not committed."""
    await mark_step(
        session, request_id, position, status="planned", result={MAYBE_DONE: True}, now=now
    )


def planned_of(row: ConfirmationStep) -> PlannedStep:
    """A stored step as it was planned, with whether the rule of the chain holds for it
    (``with_basis``) and the arrival it waits for. ``ValueError`` for a stored action that does
    not read back: it is never guessed."""
    action = None if row.action is None else DescribedAction.from_json(row.action)
    return PlannedStep(
        row.tool_name,
        row.action_type,
        dict(row.arguments),
        action,
        with_basis=row.with_basis,
        after_arrival_of=row.after_arrival_of,
    )


async def stored_steps(session: AsyncSession, request_id: uuid.UUID) -> list[PlannedStep]:
    """The steps of the chain ``request_id``, from their rows (the source of truth), in order."""
    return [planned_of(row) for row in await steps_of(session, request_id)]


def _refused_result(reason: str, limit: str | None) -> JsonObject:
    """What the model reads of a step that did not run, and why; with the summary of the limit
    that refused it, if one did (spec 5.8). A check before the step that could not be made is a
    failure, not a refusal (``CHECK_FAILED``), and so is an arrival that did not come
    (``NOT_ARRIVED``)."""
    if reason == "failed":
        return {"status": "failed"}
    if reason in (CHECK_FAILED, NOT_ARRIVED):
        return {"status": "failed", "reason": reason}
    result: JsonObject = {"status": "refused", "reason": reason}
    if limit is not None:
        result["limit"] = limit
    if reason == "limit_error":
        result["detail"] = LIMIT_ERROR_DETAIL
    return result


def _card_of_stop(reason: str, record: ActionRecord | None) -> JsonObject:
    """The card of the step that stopped the chain, which always says why (``reason``): the card
    of its record when it ran, else the sentence of a step that did not run."""
    if record is not None:
        return {**record.card, "reason": reason}
    if reason in ("failed", CHECK_FAILED):
        return {"key": "action.card.failed", "params": {}, "reason": reason}
    return {"key": f"action.card.refused.{reason}", "params": {}, "reason": reason}


def _reason_of(record: ActionRecord) -> str:
    """Why a step that ran did not end ``done``: the reason its record gives, else its status."""
    reason = record.model_result.get("reason")
    return reason if isinstance(reason, str) else record.status


def _same(action: DescribedAction, step: PlannedStep) -> bool:
    """Whether ``action``, the step described now, is still what was planned: the same type and,
    for a step described when the chain was asked for, the same target."""
    if action.action_type != step.action_type:
        return False
    return step.action is None or (
        (action.action_type, action.target) == (step.action.action_type, step.action.target)
    )


async def _described(
    tool: InternalTool, step: PlannedStep, actor: Actor, ctx: ToolContext
) -> DescribedAction | None:
    """The step described again by its tool for ``actor``; ``None`` when the tool no longer
    accepts it, fails, or takes more than ``_DESCRIBE_SECONDS`` (describing changes nothing)."""
    with anyio.move_on_after(_DESCRIBE_SECONDS):
        try:
            return await tool.describe(step.arguments, actor, ctx)
        except InvalidArguments:
            return None
        except Exception as error:
            logger.error(
                "A step %s of a chain could not be described again (%s)",
                step.tool_name,
                type(error).__name__,
            )
            return None
    logger.error("Describing a step %s of a chain again took too long", step.tool_name)
    return None


@dataclass
class _Runner:
    """One run of a chain (see ``run_steps``): what it was given, what the rows say, and what it
    did so far."""

    ctx: ToolContext
    steps: Sequence[PlannedStep]
    basis: ChainBasis | None
    request_id: uuid.UUID | None
    decider: Person | None
    states: list[str]
    given: str = ""
    # Whether a person said yes to the request (``False`` for a chain the judge let through:
    # ``decided_via`` ``judge``).
    said_yes: bool = True
    earlier: int = 0
    # The actions of the steps done, earlier runs included, in order (the rule of the chain).
    done_actions: list[DescribedAction] = field(default_factory=lambda: [])
    # The types of the steps this run did that no table holds (a chain that runs at once).
    unrecorded: list[str] = field(default_factory=lambda: [])
    done: list[StepDone] = field(default_factory=lambda: [])
    # What the steps done left for the one who asked, earlier runs included, in order.
    buttons: list[StepButton] = field(default_factory=lambda: [])

    def _ran(
        self,
        status: Literal["done", "failed", "waiting", "paused"],
        *,
        failed: StepDone | None = None,
        left: tuple[int, ...] = (),
        need: NeedsApproval | None = None,
        waiting_for: str | None = None,
    ) -> ChainRun:
        """What came of the run, with what the steps done so far carried."""
        return ChainRun(
            status=status,
            done=tuple(self.done),
            failed=failed,
            left=left,
            need=need,
            earlier=self.earlier,
            buttons=tuple(self.buttons),
            waiting_for=waiting_for,
        )

    def _left_from(self, position: int) -> tuple[int, ...]:
        return tuple(p for p in range(position, len(self.steps)) if self.states[p] in _TO_RUN)

    async def run(self) -> ChainRun:
        for position, step in enumerate(self.steps):
            if self.states[position] not in _TO_RUN:
                continue
            ended = await self._step(position, step)
            if ended is not None:
                return ended
        return self._ran("done")

    async def _step(self, position: int, step: PlannedStep) -> ChainRun | None:
        """Run the step at ``position``; ``None`` when it was done and the chain goes on, else
        what came of the run."""
        # A copy: reading the person again changes the context it is given.
        current = replace(self.ctx)
        match await read_the_person_again(current):
            case "gone":
                return await self._stop(position, step, "no_longer_possible")
            case "unknown":
                return await self._stop(position, step, CHECK_FAILED)
            case "active":
                pass
        if step.after_arrival_of is not None:
            waited = await self._waited_for(position, step, step.after_arrival_of)
            if waited is not None:
                return waited
        # The rule of the chain holds for a step as it was asked for, by the person the chain
        # remembers (``with_basis``); a step changed or added later is judged with the role of
        # now (spec 4.1, last point).
        basis = self.basis if step.with_basis else None
        actor = (
            current.actor
            if basis is None
            else judged_actor(current.actor, basis, self.done_actions)
        )
        tool = self.ctx.registry.get(step.tool_name)
        if tool is None or is_made(tool.action_type) or tool.action_type != step.action_type:
            # A tool Domo made is never a step (``tools.chain`` refuses it when it plans).
            return await self._stop(position, step, "no_longer_possible")
        action = await _described(tool, step, actor, replace(current, actor=actor))
        if action is None or not _same(action, step):
            return await self._stop(position, step, "no_longer_possible")
        decided = await self._decided(current.actor, action, basis)
        if decided is None:
            return await self._stop(position, step, CHECK_FAILED)
        judged, decision = decided
        audited = (judged, action, decision)
        match decision:
            case Refuse(reason=reason, limit=limit):
                return await self._stop(position, step, reason, audited=audited, limit=limit)
            case NeedsApproval():
                if self.request_id is None or self.decider is None or not self.said_yes:
                    # Nobody said yes to a chain that runs at once, nor to one the judge let
                    # through (F28): it stops, it is never sent to someone for their yes.
                    return await self._stop(position, step, "needs_confirmation", audited=audited)
                if not covers(self.given, decision.approver, self.decider, self.ctx.person.id):
                    return await self._reroute(position, step, decision, judged, action)
            case Allow():
                pass
        record = await execute_allowed(
            tool,
            step.tool_name,
            step.action_type,
            step.arguments,
            # A step of a chain: what it makes for the one who asked alone waits behind its own
            # button on the chain's card (``ToolContext.leave_on_their_card``).
            replace(current, actor=judged, described_action=action, in_a_chain=True),
        )
        if record.status != "done":
            return await self._stop(
                position, step, _reason_of(record), audited=audited, record=record
            )
        await self._record_done(position, step, record, audited)
        self.done_actions.append(action)
        if self.request_id is None:
            self.unrecorded.append(action.action_type)
        self.done.append(StepDone(position, step.action_type, "done", None, record.model_result))
        button = button_of(position, record.card)
        if button is not None:
            self.buttons.append(button)
        return None

    async def _waited_for(self, position: int, step: PlannedStep, name: str) -> ChainRun | None:
        """The step at ``position`` waits for the arrival of ``name``: ``None`` when they arrived
        (the step goes on); otherwise what came of the run: paused while their invitation waits,
        stopped when it ended or none has that name (``NOT_ARRIVED``), or when it could not be
        read (``CHECK_FAILED``). A chain that runs at once never waits: nobody said yes to it."""
        if self.request_id is None:
            return await self._stop(position, step, "needs_confirmation")
        found = await self._arrival(name)
        if found is None:
            return await self._stop(position, step, CHECK_FAILED)
        state, invitation_id = found
        match state:
            case "arrived":
                return None
            case "waiting" if invitation_id is not None:
                return await self._pause(position, invitation_id, name)
            case _:
                return await self._stop(position, step, NOT_ARRIVED, card=not_arrived_card(name))

    async def _arrival(
        self, name: str
    ) -> tuple[Literal["arrived", "waiting", "ended"], uuid.UUID | None] | None:
        """Where the arrival of ``name`` stands (see the module docstring, 0): ``arrived`` (the
        invitation was accepted), ``waiting`` (it can still be accepted: its id comes with it) or
        ``ended`` (cancelled, past its date, or none); ``None`` when the invitation could not be
        read, or not within ``_CHECK_SECONDS``."""
        assert self.request_id is not None
        now = self.ctx.clock()
        with anyio.move_on_after(_CHECK_SECONDS):
            try:
                async with self.ctx.session_factory() as session:
                    found = await session.execute(
                        select(Confirmation)
                        .where(Confirmation.id == self.request_id)
                        .execution_options(populate_existing=True)
                    )
                    c = found.scalar_one()
                    invitation = await self._invitation_of(session, c, name, now)
                    if invitation is not None and invitation.accepted_at is not None:
                        return "arrived", invitation.id
                    if invitation is None or not is_open(invitation, now):
                        # It ended: a new link of the one who asked for that name, if one waits;
                        # never one someone else made, whose link they do not hold.
                        invitation = await _open_invitation(
                            session, c, name, now, made_by_the_requester_only=True
                        )
                        if invitation is None:
                            return "ended", None
                    return "waiting", invitation.id
            except Exception as error:
                logger.error(
                    "The invitation a step of a chain waits for could not be read (%s)",
                    type(error).__name__,
                )
                return None
        logger.error("Reading the invitation a step of a chain waits for took too long")
        return None

    async def _invitation_of(
        self, session: AsyncSession, c: Confirmation, name: str, now: datetime
    ) -> Invitation | None:
        """The invitation a step of the chain ``c`` waits for, by its ``name`` (see the module
        docstring, 0): the one ``c`` waited for last if it has that name (the invitation whose
        acceptance resumed it), else the last one a step done of this chain made under that name,
        else the one that waits in the household under that name, if the one who asked made it or
        it already waited when the yes was given (``_open_invitation``); ``None`` when there is
        none."""
        key = name_key(name)
        if c.waiting_for_invitation_id is not None:
            found = await session.execute(
                select(Invitation)
                .where(Invitation.id == c.waiting_for_invitation_id)
                .execution_options(populate_existing=True)
            )
            waited = found.scalar_one_or_none()
            if waited is not None and name_key(waited.display_name) == key:
                return waited
        made = [uuid.UUID(b.invitation) for b in self.buttons if b.invitation is not None]
        if made:
            found = await session.execute(
                select(Invitation)
                .where(Invitation.id.in_(made))
                .execution_options(populate_existing=True)
            )
            by_id = {invitation.id: invitation for invitation in found.scalars()}
            for invitation_id in reversed(made):
                invitation = by_id.get(invitation_id)
                if invitation is not None and name_key(invitation.display_name) == key:
                    return invitation
        return await _open_invitation(session, c, name, now, made_by_the_requester_only=False)

    async def _decided(
        self, actor_now: Actor, action: DescribedAction, basis: ChainBasis | None
    ) -> tuple[Actor, Decision] | None:
        """The barrier's decision on the step, and the person as judged: under the rule of the
        chain with ``basis``, else as any call of theirs now; ``None`` when the rights or the facts
        could not be read, or not within ``_CHECK_SECONDS`` (nothing may run then)."""
        ctx = self.ctx
        with anyio.move_on_after(_CHECK_SECONDS):
            try:
                async with ctx.session_factory() as session:
                    if basis is None:
                        decision = await decide_now(
                            session,
                            actor_now,
                            action,
                            ctx.policy,
                            ctx.clock(),
                            unrecorded=tuple(self.unrecorded),
                        )
                        return actor_now, decision
                    return await decide_in_chain(
                        session,
                        actor_now,
                        action,
                        ctx.policy,
                        ctx.clock(),
                        basis=basis,
                        done=tuple(self.done_actions),
                    )
            except Exception as error:
                logger.error(
                    "The rights could not be read for a step of a chain (%s)",
                    type(error).__name__,
                )
                return None
        logger.error("Reading the rights for a step of a chain took too long")
        return None

    async def _record_done(
        self,
        position: int,
        step: PlannedStep,
        record: ActionRecord,
        audited: tuple[Actor, DescribedAction, Decision],
    ) -> None:
        """Record that the step was done: its row, with its card (and, for a step checked in its
        turn, the action it was described as in its turn), and the barrier's decision, last. A
        failure is logged and raised: the step ran, and its record is not known."""
        judged, action, decision = audited
        try:
            with anyio.fail_after(_STORE_SECONDS):
                async with self.ctx.session_factory() as session:
                    if self.request_id is not None:
                        await mark_step(
                            session,
                            self.request_id,
                            position,
                            status="done",
                            result=record.card,
                            now=self.ctx.clock(),
                            action=action if step.action is None else None,
                        )
                    await record_decision(
                        session, judged, action, decision, tool_name=step.tool_name
                    )
                    await session.commit()
        except Exception as error:
            logger.error(
                "Step %d of a chain (%s) was done, but not recorded (%s): check what it did",
                position + 1,
                step.action_type,
                type(error).__name__,
            )
            raise

    async def _stop(
        self,
        position: int,
        step: PlannedStep,
        reason: str,
        *,
        audited: tuple[Actor, DescribedAction, Decision] | None = None,
        limit: str | None = None,
        record: ActionRecord | None = None,
        card: JsonObject | None = None,
    ) -> ChainRun:
        """Stop the chain at ``position`` (see the module docstring), in one transaction; the
        step's card is ``card`` when it is given (an arrival that did not come names whom)."""
        failed = StepDone(
            position,
            step.action_type,
            "failed",
            reason,
            _refused_result(reason, limit) if record is None else record.model_result,
        )
        left = self._left_from(position + 1)
        now = self.ctx.clock()
        with anyio.fail_after(_STORE_SECONDS):
            async with self.ctx.session_factory() as session:
                if self.request_id is not None:
                    await mark_step(
                        session,
                        self.request_id,
                        position,
                        status="failed",
                        result=_card_of_stop(reason, record) if card is None else card,
                        now=now,
                    )
                    for rest in left:
                        await mark_step(
                            session, self.request_id, rest, status="stopped", result=None, now=now
                        )
                if audited is not None:
                    judged, action, decision = audited
                    await record_decision(
                        session, judged, action, decision, tool_name=step.tool_name
                    )
                await append_audit(
                    session,
                    actor=f"person:{self.ctx.person.id}",
                    action="chain.stopped",
                    details={
                        "confirmation_id": None
                        if self.request_id is None
                        else str(self.request_id),
                        "step": position + 1,
                        "reason": reason,
                    },
                )
                await session.commit()
        return self._ran("failed", failed=failed, left=left)

    async def _pause(self, position: int, invitation_id: uuid.UUID, name: str) -> ChainRun:
        """The step at ``position`` waits for the arrival of ``name``, whose invitation
        (``invitation_id``) can still be accepted: the request (``executing``) pauses there
        (``waiting_arrival``), waiting for that invitation (in place of one it waited for before),
        and ``chain.paused`` is audited (``{"confirmation_id", "step", "invitation_id"}``, the step
        counted from 1), committed. The upkeep resumes it once the invitation is accepted, or stops
        it when it ends (``confirmations.arrivals``)."""
        assert self.request_id is not None
        with anyio.fail_after(_STORE_SECONDS):
            async with self.ctx.session_factory() as session:
                found = await session.execute(
                    select(Confirmation)
                    .where(Confirmation.id == self.request_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                c = found.scalar_one_or_none()
                if c is None or c.status != "executing":
                    raise LookupError("the request of the chain is not being carried out")
                c.status = "waiting_arrival"
                c.waiting_for_invitation_id = invitation_id
                await session.flush()
                # Last, just before the commit.
                await append_audit(
                    session,
                    actor=f"person:{self.ctx.person.id}",
                    action="chain.paused",
                    details={
                        "confirmation_id": str(c.id),
                        "step": position + 1,
                        "invitation_id": str(invitation_id),
                    },
                )
                await session.commit()
        return self._ran("paused", left=self._left_from(position), waiting_for=name)

    async def _reroute(
        self,
        position: int,
        step: PlannedStep,
        need: NeedsApproval,
        judged: Actor,
        action: DescribedAction,
    ) -> ChainRun:
        """The yes given to the chain does not cover the step (a more demanding approver is asked
        for now): its request waits for that yes again (Ruling R27), asking what it asked, with
        what Domo could not tell when a limit sends it to the owner, and its age; nothing of the
        yes is kept on it. Audited as ``confirmations.service`` reroutes a request (the barrier's
        decision on the step, then ``confirmation.rerouted``), committed."""
        assert self.request_id is not None and self.decider is not None
        with anyio.fail_after(_STORE_SECONDS):
            async with self.ctx.session_factory() as session:
                found = await session.execute(
                    select(Confirmation)
                    .where(Confirmation.id == self.request_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                c = found.scalar_one_or_none()
                if c is None or c.status != "executing":
                    raise LookupError("the request of the chain is not being carried out")
                before = c.approver_kind
                c.status = "pending"
                c.approver_kind = need.approver
                c.ask_reason = need.question
                c.decided_by = None
                c.decided_at = None
                c.decided_via = None
                c.decided_message_id = None
                await session.flush()
                await record_decision(session, judged, action, need, tool_name=step.tool_name)
                await append_audit(
                    session,
                    actor=f"person:{self.decider.id}",
                    action="confirmation.rerouted",
                    details={
                        "confirmation_id": str(c.id),
                        "action_type": action_type_of(c),
                        "approver_kind": c.approver_kind,
                        "before": before,
                    },
                )
                await session.commit()
        return self._ran("waiting", left=self._left_from(position), need=need)


async def _open_invitation(
    session: AsyncSession,
    c: Confirmation,
    name: str,
    now: datetime,
    *,
    made_by_the_requester_only: bool,
) -> Invitation | None:
    """The one invitation of ``name`` that waits in the household at ``now``
    (``resolve_invitation``), if the yes given to the chain ``c`` covers it: the one who asked made
    it (they hold its link), or, unless ``made_by_the_requester_only``, it already waited when the
    yes was given (the yes was given to a chain that waits for it). One someone else made since is
    never taken: its link is theirs, and whoever comes with it would have the rest of the chain run
    under a yes that never covered it. ``None`` otherwise."""
    try:
        invitation = await resolve_invitation(session, name, now=now)
    except InvalidArguments:
        return None
    if invitation.created_by == c.requester_id:
        return invitation
    if made_by_the_requester_only or c.decided_at is None or invitation.created_at > c.decided_at:
        return None
    return invitation


async def run_steps(
    ctx: ToolContext,
    steps: Sequence[PlannedStep],
    *,
    basis: ChainBasis | None,
    request_id: uuid.UUID | None,
    decider: Person | None,
) -> ChainRun:
    """Run the ``steps`` of a chain, in order, for the person of ``ctx`` (the one who asked; see
    the module docstring).

    Without a request (``request_id`` is ``None``: a chain whose every step was free, that runs
    at once), every step runs, as any call of the person's (``basis`` is then ``None``, and nobody
    said yes: ``decider`` is ``None``). With a request, ``steps`` are its steps
    (``stored_steps``), ``basis`` what it remembers of the one who asked, and ``decider`` the
    person whose yes runs it; its rows say which steps are still to run, and are marked as they
    run. ``ValueError`` when a request comes without these, or with steps that are not its own."""
    states = ["planned"] * len(steps)
    runner = _Runner(ctx, steps, basis, request_id, decider, states)
    if request_id is not None:
        if basis is None or decider is None:
            raise ValueError("a request's chain runs with its basis and the one who said yes")
        async with ctx.session_factory() as session:
            found = await session.execute(
                select(Confirmation.approver_kind, Confirmation.decided_via).where(
                    Confirmation.id == request_id
                )
            )
            runner.given, decided_via = found.one()
            runner.said_yes = decided_via != "judge"
            rows = await steps_of(session, request_id)
        if [row.tool_name for row in rows] != [step.tool_name for step in steps]:
            raise ValueError("these are not the steps of this request")
        for row in rows:
            states[row.position] = row.status
            if row.status == "done":
                runner.earlier += 1
                # A step checked in its turn that was done holds the action it was described as
                # in its turn (``_record_done``): a hand-over it made counts. (A row without one,
                # which no run writes, would leave it unseen: the role of now would count, which
                # closes rather than opens.)
                if row.action is not None:
                    runner.done_actions.append(DescribedAction.from_json(row.action))
                # What an earlier run left behind a button on the card of the one who asked: the
                # chain's card after this run carries it too (the rows are in order, and every
                # step done comes before those this run does).
                button = None if row.result is None else button_of(row.position, row.result)
                if button is not None:
                    runner.buttons.append(button)
    return await runner.run()


def chain_outcome(run: ChainRun, *, total: int, at_once: bool) -> ToolOutcome:
    """What the tool of a chain tells of a run (``total`` is how many steps the chain has): to
    the model, each step it did (and the one that stopped it), with what it gave back; for a
    chain that stopped, where (``stopped_at``, from 1), why, how many steps are left, and what
    Domo may do (``STOPPED_DETAIL``; for a chain that ran ``at_once``, with no request that
    ``chain_continue`` could take up again, ``STOPPED_AT_ONCE_DETAIL``); for a chain that waits
    for someone else's yes again, whose and how many steps are done and left; for a chain that
    paused at a step that waits for someone's arrival, whom (``"of"``, the name of their
    invitation), how many steps are done and left, and what comes next (``PAUSED_DETAIL``). To
    the people, a card that says how many were done (or, for a chain that paused, whom it waits
    for); it lists the types of the steps the run did (``"did"``, for the history a limit reads),
    and, whatever came of the run, the buttons its steps done left for the one who asked
    (``"buttons"``, ``ChainRun.buttons``). Never a part shown once: each step left it behind its
    button."""
    steps: list[JsonObject] = [step.told() for step in run.done]
    did = [step.action_type for step in run.done]
    counted = {"done": str(run.earlier + len(run.done)), "total": str(total)}
    buttons: JsonObject = {"buttons": [button.as_json() for button in run.buttons]}
    match run.status:
        case "done":
            return ToolOutcome(
                "done",
                {"steps": list(steps)},
                {"key": "action.card.done.chain", "params": counted, "did": list(did), **buttons},
            )
        case "failed":
            if run.failed is None:
                raise ValueError("a chain that stopped says where")
            stopped = run.failed
            steps.append(stopped.told())
            reason = stopped.reason or "failed"
            return ToolOutcome(
                "failed",
                {
                    "steps": list(steps),
                    "stopped_at": stopped.position + 1,
                    "reason": reason,
                    "left": len(run.left),
                    "detail": STOPPED_AT_ONCE_DETAIL if at_once else STOPPED_DETAIL,
                },
                {
                    "key": "action.card.failed.chain",
                    "params": counted,
                    "reason": reason,
                    "did": list(did),
                    **buttons,
                },
            )
        case "waiting":
            if run.need is None:
                raise ValueError("a chain that waits says for whom")
            approver = run.need.approver
            return ToolOutcome(
                "done",
                {
                    "chain": WAITING_FOR_ANOTHER_YES,
                    "by": WHO_SAYS_YES[approver],
                    "done": run.earlier + len(run.done),
                    "left": len(run.left),
                },
                {"key": f"action.card.waiting.{approver}", "params": {}, **buttons},
            )
        case "paused":
            if run.waiting_for is None:
                raise ValueError("a chain that pauses says for whom")
            name = run.waiting_for
            return ToolOutcome(
                "done",
                {
                    "chain": WAITING_FOR_ARRIVAL,
                    "of": name,
                    "done": run.earlier + len(run.done),
                    "left": len(run.left),
                    "detail": PAUSED_DETAIL,
                },
                {"key": "action.card.waiting_arrival", "params": {"name": name}, **buttons},
            )


async def chain_report(
    session: AsyncSession, c: Confirmation, *, someone_else: bool = False
) -> ChainReport | None:
    """Where the chain of the request ``c`` stands, from its rows as the database holds them now
    (``None`` for a request that is no chain): its steps in English sentences, the done ones, the
    one that failed and why (the reason on its card), and those left (a step the chain stopped
    before, or that still waits its turn). With ``someone_else``, the report is for the model of
    someone other than the one who asked (an approver's): a note is told without its words
    (``describe.step_sentence_for_a_model``, D4).

    ``can_continue`` is whether ``chain_continue`` may take it up again (``can_be_continued``, as
    that tool asks; its lifetime is not read here: the report is made right after a decision), and
    ``failed_may_have_acted`` whether the step that failed may have acted
    (``failed_step_may_have_acted``): Domo then checks what happened before he continues it."""
    if c.chain_basis is None:
        return None
    rows = await steps_of(session, c.id)
    failed = next((row for row in rows if row.status == "failed"), None)
    reason = None if failed is None or failed.result is None else failed.result.get("reason")

    def told(row: ConfirmationStep) -> str:
        return step_sentence_for_a_model(row) if someone_else else step_sentence(row, REFERENCE)

    return ChainReport(
        done=tuple(told(row) for row in rows if row.status == "done"),
        failed=None if failed is None else told(failed),
        failed_reason=reason if isinstance(reason, str) else None,
        left=tuple(told(row) for row in rows if row.status in _TO_RUN),
        can_continue=can_be_continued(c, rows),
        failed_may_have_acted=failed is not None and failed_step_may_have_acted(failed),
    )


def who_decided(decider: Person, requester_id: uuid.UUID) -> str:
    """Who said yes or no, as Domo and the judge are told: the person who asked, else the owner,
    else an adult."""
    if decider.id == requester_id:
        return "the person"
    return "the owner" if decider.role == "owner" else "an adult"


def ago(moment: datetime, now: datetime) -> str:
    """How long before ``now`` ``moment`` was, in plain English words: "3 days ago", "1 hour ago",
    "5 minutes ago", or "just now" under a minute (the largest whole unit)."""
    seconds = max(0, int((now - moment).total_seconds()))
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds >= size:
            count = seconds // size
            return f"{count} {unit}{'' if count == 1 else 's'} ago"
    return "just now"


# How a yes was given (``Confirmation.decided_via``), as the judge reads it: the button, words
# (a yes, plain or firm, in words), a change in words, a proof, or no yes at all (the judge let a
# chain through).
_HOW: Final[dict[str, str]] = {
    "button": "button",
    "phrase": "words",
    "explicit_phrase": "words",
    "classifier": "words",
    "change": "change",
    "proof": "proof",
    "judge": "none",
}


async def request_told(
    session: AsyncSession,
    c: Confirmation,
    *,
    before: Sequence[str],
    after: Sequence[str],
    now: datetime,
) -> RequestTold:
    """The request ``c``, changed or taken up again, as the judge reads it (spec 4.2, its last
    block), from the database on ``session`` at ``now``: the yes it had (``YesTold``: how long
    ago, by whom, ``who_decided``, and how; ``None`` when nobody decided it, as a request that
    still waits: build it before the change marks it decided, F23), what it asked
    (``Confirmation.ask``), the steps of a chain done (``step_sentence``, in English), the step
    that failed, why (the reason on its card) and whether it may have acted
    (``failed_step_may_have_acted``: a fact of the code), and what it did ``before`` and does
    ``after`` the change, as given (both empty when nothing changes)."""
    yes: YesTold | None = None
    if c.decided_by is not None:
        decider = (
            await session.execute(select(Person).where(Person.id == c.decided_by))
        ).scalar_one_or_none()
        if decider is not None:
            by = who_decided(decider, requester_of(c))
        else:
            by = "the person" if c.decided_by == c.requester_id else "someone"
        yes = YesTold(
            when="" if c.decided_at is None else ago(c.decided_at, now),
            by=by,
            how=_HOW.get(c.decided_via or "", "none"),
        )
    rows = await steps_of(session, c.id)
    failed = next((row for row in rows if row.status == "failed"), None)
    reason = None if failed is None or failed.result is None else failed.result.get("reason")
    return RequestTold(
        yes=yes,
        done=tuple(step_sentence(row, REFERENCE) for row in rows if row.status == "done"),
        failed=None if failed is None else step_sentence(failed, REFERENCE),
        failed_reason=reason if isinstance(reason, str) else None,
        may_have_acted=failed is not None and failed_step_may_have_acted(failed),
        before=tuple(before),
        after=tuple(after),
        asked=c.ask,
    )


async def just_decided(
    session: AsyncSession,
    c: Confirmation,
    *,
    outcome: "DecideOutcome | AmendOutcome",
    record: ActionRecord | None,
    decider: Person,
    via: Literal["words", "button", "arrival", "proof"],
    woken: bool,
) -> JustDecided:
    """What Domo is told of the request ``c`` once ``decider`` decided it (see
    ``prompts.JustDecided``): the request in English, what the decision came to (``outcome``), who
    decided and how (``via``: ``proof`` for a proof given on the card), what the model would have
    read of the action (the ``record`` of the call, if one ran: never its ``card_once``) and, for
    a chain, where it stands (``chain_report``). ``c`` is the request as the database holds it
    after the decision; ``woken`` says that a button wakes Domo, the person being perhaps away.

    Someone else may decide it in words, in their own conversation (an approver: spec 5.1): Domo
    is then told in the turn of ``decider``, who did not ask, so the request and its steps are told
    without the words of a note (``describe.description_for_a_model``, D4), and without what the
    action gave back (``result``: what the steps of a chain read is the one who asked's, a note
    they left included; the outcome and ``chain`` say where it stands). A wake (``woken``)
    always speaks to the one who asked. A request decided by someone else than the one who asked
    comes with what that approver said with their change (``approver_note``), if anything.

    A chain paused until someone's arrival is decided again by the upkeep (``via`` is
    ``arrival``, ``confirmations.arrivals``): its rest ran, or it stopped because the invitation
    ended, or it waits again for the explicit yes (``arrived``); ``decider`` is then the one whose
    yes it had (who decided is told as for that yes)."""
    by_someone_else = decider.id != c.requester_id
    # Who reads it: the one who asked when Domo is woken in their conversation, else the decider.
    told_to_someone_else = by_someone_else and not woken
    return JustDecided(
        request_id=str(c.id),
        description=(
            description_for_a_model(c)
            if told_to_someone_else
            else request_description(c, REFERENCE)
        ),
        outcome=outcome,
        decided_by=who_decided(decider, requester_of(c)),
        via=via,
        result=None if record is None or told_to_someone_else else dict(record.model_result),
        woken=woken,
        chain=await chain_report(session, c, someone_else=told_to_someone_else),
        approver_note=c.approver_note if by_someone_else else None,
    )
