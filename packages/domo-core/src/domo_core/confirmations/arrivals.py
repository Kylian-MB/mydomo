"""The chains paused until someone's arrival (spec 4.3b): a pass of the core's upkeep resumes them
once the person joined, or stops them when their invitation ended without them.

A chain whose step waits for someone's arrival does its steps up to that one and pauses
(``confirmations.chain``: the request is ``waiting_arrival``, and ``waiting_for_invitation_id``
names the invitation it waits for). ``arrivals_pass`` is the pass of the upkeep
(``upkeep.run_upkeep``) that looks at them, when the core starts and every minute after: so a chain
resumes at most a minute after the arrival, even when the application restarted meanwhile (its
first pass is made at once). At each pass:

* each chain whose invitation was accepted (``chains_to_resume``) is claimed (``claim_resume``: it
  is ``executing`` again, and its lifetime starts again at the arrival: ``created_at`` is the time
  of the pass, ``chain.resumed`` is audited with who arrived), committed, then resumed in a task of
  the application (``conversation.starts.Wakes``: ``resume_in_background``). A chain whose steps
  left the owner's rules ask more of now than the yes given gave (a rule added while it waited;
  for a chain the judge let through, anything they ask) runs nothing: it waits again for the yes
  of the one who gave its yes (``_ask_again``, ``arrived``; its card says who arrived): the rules
  are read again at the arrival, as at a yes. What it asked before is never lowered (a proof stays
  a proof). No step asks to be seen again by default (2026-11-15: the chain was asked for and
  understood; each step left is still decided by the barrier in its turn, and no yes-judge is
  consulted at an arrival). Otherwise the rest runs as after the
  yes (``resume_chain``), under the yes already given
  and in the context of the one who asked, each step described again and judged in its turn with the
  facts of now (the wait finds the person arrived; a step after it may name them); and what came of
  it is recorded as for a yes (``tools.chain.run_under_the_same_yes``). Someone who asked and left
  the household meanwhile gets nothing done: the chain stops at once (``no_longer_possible``);
* each chain whose invitation ended without the person (cancelled, or past its date:
  ``chains_to_stop``) stops at the wait (``stop_not_arrived``): that step is ``failed``
  (``NOT_ARRIVED``, the card names whom it waited for), the steps after it ``stopped``, the
  request ``failed`` with the card of a chain that stopped (its steps done keep their buttons), and
  ``chain.stopped`` is audited. Its lifetime starts again then, as at an arrival: the link ended
  at least a week after the chain was asked for, and Domo may take it up again
  (``chain_continue``) once the one who asked made a new link of that name (the runner never takes
  an invitation someone else made: ``confirmations.chain``).

Whatever came of it, Domo is then woken in the conversation of the one who asked, as after a
button (``conversation.starts.spawn_wake``, ``via`` ``arrival``), told what came of it and who
had said yes, and judges whether to say something; a chain that waits for a yes
again is presented again by his reply, so that the next words of the one who asked may give it.

A resume begun is run to its record, whatever stops meanwhile: the run and its record are one unit
shielded from cancellation (as a yes's, ``confirmations.service._run_and_settle``), and the task
that the application cancels when it stops (``Wakes.stop``) waits for it, within the time the
application gives its wakes: a stop never leaves a chain ``executing`` with its rest never run.
A resume that cannot be made (what the chain stored of its yes does not read back) or that fails
on the way (a database error, say) is logged, by its kind, and leaves the chain ``failed``, not
``executing`` (no pass would look at it again): Domo is woken as for any chain that failed.
A pass that fails for one chain is logged, by its kind, and goes on with the others; the next pass
tries it again.

What a pass needs of the application (its database, its tools, its catalog, its clock, the model's
factory for the wake that follows a resume) is read again at each pass (``deps_factory``): a test
may replace them while the application runs.
"""

import asyncio
import logging
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime

import anyio
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.accounts.sessions import is_active
from domo_core.audit.log import append_audit
from domo_core.chat.agent import ActionRecord
from domo_core.confirmations.chain import (
    NOT_ARRIVED,
    ChainRun,
    StepDone,
    chain_outcome,
    not_arrived_card,
    paused,
    planned_of,
    waits_again,
)
from domo_core.confirmations.models import Confirmation, ConfirmationStep
from domo_core.confirmations.service import (
    DecidedVia,
    DecideOutcome,
    rerouted_yes,
    what_the_yes_gave,
)
from domo_core.confirmations.steps import button_of, mark_step, steps_of
from domo_core.confirmations.store import asked_of, confirmations_by_id
from domo_core.conversation.prepare import TurnDeps
from domo_core.conversation.starts import Wakes, notifier, spawn_wake
from domo_core.household.models import Invitation
from domo_core.policy.asks import Ask, Asked, most_demanding, most_demanding_asked
from domo_core.policy.catalog import PolicyTables
from domo_core.policy.decision import ChainBasis
from domo_core.policy.rights import load_rights
from domo_core.policy.roles import Actor, as_role
from domo_core.tools.chain import (
    UnderTheSameYes,
    run_under_the_same_yes,
    the_one_who_said_yes,
    via_of,
)
from domo_core.tools.context import ToolContext
from domo_core.upkeep import UpkeepPass

logger = logging.getLogger(__name__)

# The state of a chain paused until someone's arrival.
_PAUSED = "waiting_arrival"
# The states of the steps of a chain still to run.
_LEFT = frozenset({"planned", "stopped"})
# Whom a chain waited for, when its step does not say (it always does).
_SOMEONE = "…"


async def chains_to_resume(session: AsyncSession, now: datetime) -> list[uuid.UUID]:
    """The chains paused until someone's arrival whose invitation was accepted (the person
    arrived), oldest first. Reads only."""
    found = await session.execute(
        select(Confirmation.id)
        .join(Invitation, Invitation.id == Confirmation.waiting_for_invitation_id)
        .where(Confirmation.status == _PAUSED, Invitation.accepted_at.is_not(None))
        .order_by(Confirmation.created_at, Confirmation.id)
    )
    return list(found.scalars())


async def chains_to_stop(session: AsyncSession, now: datetime) -> list[uuid.UUID]:
    """The chains paused until someone's arrival whose invitation ended without them at ``now``
    (cancelled, or past its date, and never accepted), oldest first. Reads only."""
    found = await session.execute(
        select(Confirmation.id)
        .join(Invitation, Invitation.id == Confirmation.waiting_for_invitation_id)
        .where(
            Confirmation.status == _PAUSED,
            Invitation.accepted_at.is_(None),
            or_(Invitation.cancelled_at.is_not(None), Invitation.expires_at <= now),
        )
        .order_by(Confirmation.created_at, Confirmation.id)
    )
    return list(found.scalars())


async def _locked(session: AsyncSession, confirmation_id: uuid.UUID) -> Confirmation | None:
    found = await session.execute(
        select(Confirmation)
        .where(Confirmation.id == confirmation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return found.scalar_one_or_none()


async def _waited_for(session: AsyncSession, c: Confirmation) -> Invitation | None:
    """The invitation the paused chain ``c`` waits for, as the database holds it now."""
    if c.waiting_for_invitation_id is None:
        return None
    found = await session.execute(
        select(Invitation)
        .where(Invitation.id == c.waiting_for_invitation_id)
        .execution_options(populate_existing=True)
    )
    return found.scalar_one_or_none()


async def claim_resume(
    session: AsyncSession, confirmation_id: uuid.UUID, now: datetime
) -> Confirmation | None:
    """Claim the chain ``confirmation_id`` for its resume, its row locked: if it is still paused
    and its invitation was accepted, it is ``executing`` again, its lifetime starts again
    (``created_at`` is ``now``), and ``chain.resumed`` is audited (``{"confirmation_id",
    "invitation_id", "arrived": "person:<id>"}``, by the system). ``None`` when it is no longer
    paused (dropped, or claimed by another pass) or nobody arrived. Flushed, not committed."""
    c = await _locked(session, confirmation_id)
    if c is None or c.status != _PAUSED:
        return None
    invitation = await _waited_for(session, c)
    if invitation is None or invitation.accepted_at is None:
        return None
    c.status = "executing"
    c.created_at = now
    await session.flush()
    # Last, before the caller commits.
    await append_audit(
        session,
        actor="system",
        action="chain.resumed",
        details={
            "confirmation_id": str(c.id),
            "invitation_id": str(invitation.id),
            "arrived": f"person:{invitation.accepted_person_id}",
        },
    )
    return c


async def stop_not_arrived(
    session: AsyncSession, confirmation_id: uuid.UUID, now: datetime
) -> Person | None:
    """Stop the chain ``confirmation_id`` at its wait, its row locked, if it is still paused and its
    invitation ended without the person: the step that waits is ``failed`` (its card names whom it
    waited for, ``NOT_ARRIVED``), the steps after it ``stopped``, the request ``failed`` with the
    card of a chain that stopped (with the buttons of its steps done) and its lifetime started
    again (``created_at`` is ``now``: it may be taken up again with a new link), and
    ``chain.stopped`` is audited (``{"confirmation_id", "step", "reason"}``, by the system).
    When it was stopped, the one whose yes it had (``tools.chain.the_one_who_said_yes``: the one
    who gave it, else the one who asked), whom Domo is told decided it; ``None`` otherwise.
    Flushed, not committed."""
    c = await _locked(session, confirmation_id)
    if c is None or c.status != _PAUSED:
        return None
    invitation = await _waited_for(session, c)
    if invitation is not None and invitation.accepted_at is not None:
        # They arrived meanwhile: the next pass resumes it.
        return None
    requester = await session.get(Person, c.requester_id)
    if requester is None:
        raise LookupError("the one who asked for a chain is not in the database")
    decider = await the_one_who_said_yes(session, c, requester, now)
    rows = await steps_of(session, c.id)
    done = [row for row in rows if row.status == "done"]
    wait = next((row for row in rows if row.status != "done"), None)
    if wait is None:
        raise LookupError("a paused chain has a step that waits")
    name = wait.after_arrival_of or (_SOMEONE if invitation is None else invitation.display_name)
    await mark_step(
        session, c.id, wait.position, status="failed", result=not_arrived_card(name), now=now
    )
    left = tuple(row.position for row in rows if row.position > wait.position)
    for position in left:
        await mark_step(session, c.id, position, status="stopped", result=None, now=now)
    run = ChainRun(
        status="failed",
        done=(),
        failed=StepDone(
            wait.position,
            wait.action_type,
            "failed",
            NOT_ARRIVED,
            {"status": "failed", "reason": NOT_ARRIVED},
        ),
        left=left,
        need=None,
        earlier=len(done),
        buttons=tuple(
            button
            for row in done
            if row.result is not None and (button := button_of(row.position, row.result))
        ),
    )
    c.status = "failed"
    c.result = dict(chain_outcome(run, total=len(rows), at_once=False).card)
    c.created_at = now
    await session.flush()
    # Last, before the caller commits.
    await append_audit(
        session,
        actor="system",
        action="chain.stopped",
        details={"confirmation_id": str(c.id), "step": wait.position + 1, "reason": NOT_ARRIVED},
    )
    return decider


async def resume_chain(
    deps: TurnDeps, confirmation_id: uuid.UUID
) -> tuple[DecideOutcome, ActionRecord | None, Person] | None:
    """Resume the chain ``confirmation_id`` that ``claim_resume`` made ``executing``: its rest runs
    as after the yes, in the context of the one who asked (with no reader: a link or a form a step
    makes waits behind its button), under the yes of the one who gave it
    (``tools.chain.the_one_who_said_yes``), and what came of it is recorded as for a yes, unless it
    waits again (``tools.chain.run_under_the_same_yes``). One unit shielded from cancellation, as
    a yes's: a resume begun is run and recorded. A chain whose stored yes cannot be read back is
    recorded ``failed`` (it is never guessed).

    A chain whose steps left the owner's rules ask more of now than the yes given
    gave (``_asked_at_the_arrival``) runs nothing: it waits again for a yes (``_ask_again``),
    unless the one who asked left the household meanwhile (their chain then stops at once, as any
    of theirs). There is no judge at an arrival: otherwise the rest goes on under the yes already
    given, or the judge's leave.

    What came of it (``executed``, ``failed``, ``paused`` at a later wait, ``approver_changed``
    when a step asked for someone else's yes, or ``arrived`` when it waits for a yes
    again), the record of the run (for a chain that waits for someone else's yes, the record that
    says whose, ``service.rerouted_yes``; none for ``arrived``, nor for a chain that could not be
    resumed, ``failed``), and the one whose yes it ran under, or had (read before it is cleared);
    ``None`` when the request is not a chain being resumed."""
    with anyio.CancelScope(shield=True):
        return await _resume(deps, confirmation_id)
    # A shield without a deadline that nobody cancels never stops what it holds.
    raise AssertionError("unreachable")


async def _resume(
    deps: TurnDeps, confirmation_id: uuid.UUID
) -> tuple[DecideOutcome, ActionRecord | None, Person] | None:
    async with deps.session_factory() as session:
        c = (await confirmations_by_id(session, [confirmation_id])).get(confirmation_id)
        if c is None or c.status != "executing" or c.chain_basis is None:
            return None
        requester = await session.get(Person, c.requester_id)
        if requester is None:
            raise LookupError("the one who asked for a chain is not in the database")
        rows = await steps_of(session, c.id)
        try:
            taken = UnderTheSameYes(
                steps=[planned_of(row) for row in rows],
                basis=ChainBasis.from_json(c.chain_basis),
                decider=await the_one_who_said_yes(session, c, requester, deps.clock()),
                via=via_of(c),
                tool_name=c.tool_name,
            )
        except ValueError:
            logger.error(
                "A chain paused until an arrival could not be resumed: its yes does not read back"
            )
            decider = await _fail_resume(session, c.id, deps.clock())
            return None if decider is None else ("failed", None, decider)
        asked = (
            await _asked_at_the_arrival(session, rows, requester, deps.policy, taken.via)
            if is_active(requester, deps.clock())
            else None
        )
        if asked is not None:
            if not await _ask_again(session, c.id, rows, asked):
                return None
            return "arrived", None, taken.decider
    ctx = ToolContext(
        session_factory=deps.session_factory,
        actor=Actor(person_id=requester.id, role=as_role(requester.role)),
        person=requester,
        conversation_id=c.conversation_id,
        # No link is made in a resumed chain: each step leaves what it makes behind its button.
        origin="",
        gatekeeper=deps.gatekeeper,
        clock=deps.clock,
        registry=deps.registry,
        policy=deps.policy,
        reply_message_id=None,
        # No page reads a resume: what a step makes for the one who asked waits on their card.
        no_reader=True,
        text_writing=deps.text_writing,
        # A note a step leaves is news for its recipient.
        notify=notifier(deps),
    )
    _, record = await run_under_the_same_yes(ctx, c.id, taken)
    if paused(record):
        return "paused", record, taken.decider
    if waits_again(record):
        async with deps.session_factory() as reader:
            waiting = (await confirmations_by_id(reader, [c.id]))[c.id]
        return "approver_changed", rerouted_yes(waiting), taken.decider
    return ("executed" if record.status == "done" else "failed"), record, taken.decider


async def _asked_at_the_arrival(
    session: AsyncSession,
    rows: Sequence[ConfirmationStep],
    requester: Person,
    policy: PolicyTables,
    via: DecidedVia,
) -> Asked | None:
    """What the chain asks again at the arrival, read from the rights in force now for the one
    who asked, over every step still to run: the owner's rules on their types; ``None`` when the
    rest goes on under the yes already given, or the judge's leave.

    It asks again when what the rules ask now is more than the yes given ``via`` gave
    (``service.what_the_yes_gave``; the judge's leave gives nothing): a rule the owner added
    while the chain waited holds for it, too. Who asked it is ``rule``."""
    left = [row for row in rows if row.status in _LEFT]
    if not left:
        return None
    rights = await load_rights(session)
    actor = Actor(person_id=requester.id, role=as_role(requester.role))
    rules: list[Ask | None] = [rights.resolve(actor, row.action_type, policy).ask for row in left]
    rule = most_demanding(*rules)
    if rule is None:
        return None
    gave = what_the_yes_gave(via)
    if gave is None or most_demanding(rule, gave) != gave:
        return Asked(rule, "rule")
    return None


async def _ask_again(
    session: AsyncSession,
    confirmation_id: uuid.UUID,
    rows: Sequence[ConfirmationStep],
    asked: Asked,
) -> bool:
    """The chain ``confirmation_id``, resumed (``executing``), waits again for the yes of the one
    who gave its yes (spec 4.3b, ``arrived``): its row locked, it is ``pending`` and asks the most
    demanding of what it asked before and what is ``asked`` now (``_asked_at_the_arrival``), with
    who asked it and why: never less than before (a proof stays a proof), and as before when
    nothing more is asked. It is for the same approver, with nothing of the yes of before kept on
    it (``decided_by``, ``decided_at``, ``decided_via``, ``decided_message_id``); the invitation it
    waited for stays named (``waiting_for_invitation_id``: its card says who arrived), and its
    lifetime is the one that started at the arrival. ``chain.arrived`` is audited
    (``{"confirmation_id", "invitation_id", "step"}``, the step it waited at, counted from 1, by
    the system), committed. Whether it was done: ``False`` when the request is no longer being
    resumed."""
    c = await _locked(session, confirmation_id)
    if c is None or c.status != "executing":
        return False
    # On a tie, what was asked before stays, with who asked it and why.
    now_asked = most_demanding_asked(asked_of(c), asked) or asked
    c.status = "pending"
    c.ask = now_asked.ask
    c.asked_by = now_asked.by
    c.ask_why = now_asked.why
    c.decided_by = None
    c.decided_at = None
    c.decided_via = None
    c.decided_message_id = None
    await session.flush()
    wait = next(row.position for row in rows if row.status != "done")
    # Last, just before the commit.
    await append_audit(
        session,
        actor="system",
        action="chain.arrived",
        details={
            "confirmation_id": str(c.id),
            "invitation_id": str(c.waiting_for_invitation_id),
            "step": wait + 1,
        },
    )
    await session.commit()
    return True


async def _fail_resume(
    session: AsyncSession, confirmation_id: uuid.UUID, now: datetime
) -> Person | None:
    """The chain ``confirmation_id``, claimed for its resume (``executing``), cannot be resumed
    (what it stored of its yes does not read back), or its resume failed on the way: its row
    locked, it is ``failed``, with the card of an action that failed, its steps left as they are
    (a step whose record is not known stays as it was: it can never be continued), committed. The
    one whose yes it had (``tools.chain.the_one_who_said_yes``), whom Domo is told decided it
    when he is woken; ``None`` when it was no longer being resumed (nothing changes then)."""
    locked = await _locked(session, confirmation_id)
    if locked is None or locked.status != "executing":
        return None
    requester = await session.get(Person, locked.requester_id)
    if requester is None:
        raise LookupError("the one who asked for a chain is not in the database")
    decider = await the_one_who_said_yes(session, locked, requester, now)
    locked.status = "failed"
    locked.result = {"key": "action.card.failed", "params": {}}
    await session.commit()
    return decider


async def _resume_logged(
    deps: TurnDeps, confirmation_id: uuid.UUID
) -> tuple[DecideOutcome, ActionRecord | None, Person] | None:
    """``resume_chain``, its failure logged by its kind alone (its message may hold what the
    database said). A chain whose resume failed is not left ``executing``, which no pass would
    look at again and whose card would say "running" for ever: it is ``failed``
    (``_fail_resume``), and Domo is woken as for any chain that failed (spec 4.3b: he and the
    one who asked always have a next move). ``None`` when even that cannot be done (logged), or
    when the chain was no longer being resumed."""
    try:
        return await resume_chain(deps, confirmation_id)
    except Exception as error:
        logger.error("Resuming a chain after an arrival failed (%s)", type(error).__name__)
    try:
        # In the task ``resume_in_background`` shields, as the resume was.
        async with deps.session_factory() as session:
            decider = await _fail_resume(session, confirmation_id, deps.clock())
    except Exception as error:
        logger.error("A chain whose resume failed could not be settled (%s)", type(error).__name__)
        return None
    return None if decider is None else ("failed", None, decider)


async def resume_in_background(deps: TurnDeps, wakes: Wakes, confirmation_id: uuid.UUID) -> None:
    """``resume_chain``, as a task of the application (``wakes``), logged by its kind on failure;
    then Domo is woken in the conversation of the one who asked, told what came of it
    (``conversation.starts.spawn_wake``, ``via`` ``arrival``), in a task of the application's
    wakes of its own (``TurnDeps.wakes``). The application cancels its wakes when it stops
    (``Wakes.stop``): the resume runs in a task of its own that this one waits for, cancelled or
    not, so that a resume begun is run and recorded within the time the application gives its
    wakes (nobody is woken then)."""
    resumed = asyncio.ensure_future(_resume_logged(deps, confirmation_id))
    try:
        came = await asyncio.shield(resumed)
    except asyncio.CancelledError:
        if not resumed.done():
            await asyncio.shield(resumed)
        raise
    if came is None:
        return
    outcome, record, decider = came
    spawn_wake(
        deps,
        confirmation_id=confirmation_id,
        outcome=outcome,
        record=record,
        decider=decider,
        via="arrival",
    )


def arrivals_pass(deps_factory: Callable[[], TurnDeps], wakes: Wakes) -> UpkeepPass:
    """The pass of the upkeep that resumes the chains whose awaited person arrived, and stops
    those whose invitation ended without them (see the module docstring), with what the
    application gives now (``deps_factory``, read at each pass). A chain claimed is committed,
    then its resume starts at once in a task of ``wakes`` (which wakes Domo once it is over); a
    chain stopped is committed, then Domo is woken in a task of ``wakes`` (``failed``, ``via``
    ``arrival``); a chain that fails is logged, by its kind, and the others go on."""

    async def arrivals(now: datetime) -> None:
        deps = deps_factory()
        async with deps.session_factory() as session:
            to_resume = await chains_to_resume(session, now)
            to_stop = await chains_to_stop(session, now)
        for confirmation_id in to_resume:
            try:
                async with deps.session_factory() as session:
                    claimed = await claim_resume(session, confirmation_id, now)
                    await session.commit()
                    if claimed is not None:
                        # At once, with nothing awaited in between: a chain claimed is resumed.
                        wakes.spawn(resume_in_background(deps, wakes, confirmation_id))
            except Exception as error:
                logger.error("A chain could not be resumed (%s)", type(error).__name__)
        for confirmation_id in to_stop:
            try:
                async with deps.session_factory() as session:
                    decider = await stop_not_arrived(session, confirmation_id, now)
                    await session.commit()
            except Exception as error:
                logger.error("A paused chain could not be stopped (%s)", type(error).__name__)
                continue
            if decider is not None:
                spawn_wake(
                    deps,
                    confirmation_id=confirmation_id,
                    outcome="failed",
                    record=None,
                    decider=decider,
                    via="arrival",
                )

    return arrivals
