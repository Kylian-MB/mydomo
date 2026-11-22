"""When a tool Domo made launches by itself (spec 5.6, 5.7, 5.8): its triggers
(``made_tool_triggers``, set by ``tool_launch_when`` or by a tool that asks to run again), fired
with no model.

* a ``time`` trigger is fired by the upkeep (``triggers_pass``) once its ``due_at`` has come: it is
  claimed first, in one transaction with its row locked, and moved to its next time by its
  ``repeat`` (as a repeating plan goes on, ``plans.service.after_firing``: the times missed while
  the core was stopped are not fired one by one, they count down its ``COUNT``) or marked done;
  only then is the tool launched. Two passes, or a restart, never fire one time twice;
* an ``arrival`` trigger is fired by the same pass when someone joined the household since it last
  fired (or since it was set: an invitation accepted, ``household.service``), once whatever how
  many joined since;
* a ``return`` trigger is fired when its returner (``returner_id``) comes back after an absence
  (``conversation.starts.came_back``, where their plans due at their return are given:
  ``returns_seen``).

Each trigger launches its tool for someone (``for_person_id``): a person, or the house. An event
trigger stays: it fires at every arrival, or every return, until it is cancelled. Only the
triggers of tools that are ready, and for someone who is in the household (or for the house), are
fired, and count toward ``TRIGGERS_PER_PASS`` (the time ones, the arrival ones and the ones tried
again, each up to it): a tool set aside keeps its triggers, paused, neither fired nor moved, and
once a new version makes it ready they go on (a time that came meanwhile fires once, then its
repeat); a trigger for someone who left waits the same way until they come back, and goes with
them when they are deleted (``ON DELETE CASCADE``). A trigger is cancelled only with its tool
(``tool_remove``), or by ``tool_launch_when``.

The launch (``launch.launch`` with ``cause="trigger"``) runs in a task of the application, after the
launches turns wait on, counted in the tool's launches of the day; it is the first of its chain
(depth 0), and its program's requests are answered while it runs (``madetools.means``). It is
judged by the barrier for whom the trigger is: a person with their rights of now, the house with
Domo's own, whoever set the trigger and whatever became of them.

A launch that ended because the sandbox did not answer (``launch.SANDBOX_DOWN``: a host that
restarts, a sandbox that stops) did not happen: its trigger is put back as it was before it was
claimed (its time, its repeat, its last firing; a ``return`` is taken by the pass), its try
counted (``missed_tries``, since ``missed_since``), and the next pass fires it again. After
``MISSED_MOST`` of such tries it is settled as it would have been (done, or at its next time) and
Domo is woken for whom the trigger is (the owner for the house) with ``MISSED``, as data
(``handover.wake_for``). A trigger never breaks what fired it: what goes
wrong is logged, by its kind."""

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, cast
from zoneinfo import ZoneInfo

from anyio import to_thread
from domo_common.sandbox_api import JsonObject
from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.accounts.sessions import active_at, is_active
from domo_core.conversation.prepare import TurnDeps, made_tool_deps
from domo_core.household.models import Invitation
from domo_core.madetools.handover import AskingTool, wake_for
from domo_core.madetools.launch import HOUSE, SANDBOX_DOWN, ForWhom, LaunchDeps, Launched, launch
from domo_core.madetools.models import MadeTool, MadeToolTrigger, TriggerKind
from domo_core.plans.service import after_firing
from domo_core.policy.settings import load_policy_settings
from domo_core.upkeep import UpkeepPass

logger = logging.getLogger(__name__)

# How many time triggers, how many arrival triggers and how many triggers tried again one pass
# fires at most, the soonest first: the others wait for the next pass.
TRIGGERS_PER_PASS: Final = 20
# How long a trigger is tried again while the sandbox does not answer before it is settled and
# Domo woken.
MISSED_MOST: Final = timedelta(hours=1)
# What Domo is woken with then (data, a fixed sentence of the code).
MISSED: Final = (
    "it could not run at its time: the place where your tools run did not answer for an hour. "
    "Do what it was for another way if it matters"
)


@dataclass(frozen=True)
class Firing:
    """A launch to make: the trigger, as it was before it was claimed (to put it back if the
    sandbox does not answer), the tool, for whom (``None``: the house) and the arguments it gives
    it, and the moment it was claimed."""

    trigger_id: uuid.UUID
    tool_id: uuid.UUID
    for_person_id: uuid.UUID | None
    arguments: JsonObject
    kind: TriggerKind
    claimed_at: datetime
    due_at: datetime | None
    repeat: str | None
    last_fired_at: datetime | None
    missed_tries: int
    missed_since: datetime | None


async def _claimed(
    session: AsyncSession, trigger: MadeToolTrigger, now: datetime, zone: ZoneInfo
) -> Firing | None:
    """``trigger`` (its row locked) claimed for ``now``: a time trigger moved to its next time or
    done, an event trigger marked as fired. The launch to make; ``None`` when its tool is not
    ready (set aside) or the person it is for is not in the household: the trigger is left as it
    is, paused. Flushed."""
    tool = await session.get(MadeTool, trigger.tool_id)
    if tool is None or tool.state != "ready":
        return None
    if trigger.for_person_id is not None:
        person = await session.get(Person, trigger.for_person_id)
        if person is None or not is_active(person, now):
            return None
    firing = Firing(
        trigger_id=trigger.id,
        tool_id=trigger.tool_id,
        for_person_id=trigger.for_person_id,
        arguments=cast(JsonObject, dict(trigger.arguments)),
        kind=trigger.kind,
        claimed_at=now,
        due_at=trigger.due_at,
        repeat=trigger.repeat,
        last_fired_at=trigger.last_fired_at,
        missed_tries=trigger.missed_tries,
        missed_since=trigger.missed_since,
    )
    trigger.last_fired_at = now
    if trigger.kind == "time":
        since = trigger.due_at or now
        upcoming, repeat = await to_thread.run_sync(after_firing, trigger.repeat, since, now, zone)
        if upcoming is None:
            trigger.state = "done"
        else:
            trigger.due_at = upcoming
            trigger.repeat = repeat
    await session.flush()
    return firing


async def fire(deps: LaunchDeps, firings: Sequence[Firing]) -> None:
    """Launch each tool of ``firings`` with its arguments (``cause="trigger"``), one after the
    other, for whom its trigger is (a person, or the house), the first of its chain. A launch the
    sandbox did not answer puts its trigger back (``_missed``). Never raises but for its
    cancellation: what goes wrong is logged, by its kind."""
    for firing in firings:
        try:
            async with deps.session_factory() as session:
                tool = await session.get(MadeTool, firing.tool_id)
                if tool is None:
                    continue
                asking = AskingTool.of(tool)
                person = (
                    None
                    if firing.for_person_id is None
                    else await session.get(Person, firing.for_person_id)
                )
            if firing.for_person_id is not None and person is None:
                continue
            for_whom = HOUSE if person is None else ForWhom(person)
            # Its program's requests are answered while it runs; the wakes they ask are given
            # once it ends.
            launched = await launch(
                deps, firing.tool_id, arguments=firing.arguments, for_whom=for_whom, cause="trigger"
            )
            if _sandbox_down(launched):
                await _missed(deps, firing, asking)
                continue
            if firing.missed_tries:
                await _caught_up(deps, firing)
        except Exception as error:
            logger.error("A tool's trigger could not launch it (%s)", type(error).__name__)


def _sandbox_down(launched: Launched) -> bool:
    """Whether ``launched`` ended because the sandbox did not answer: nothing ran."""
    return launched.status == "failed" and launched.result == {"reason": SANDBOX_DOWN}


async def _locked_as_claimed(session: AsyncSession, firing: Firing) -> MadeToolTrigger | None:
    """The trigger of ``firing``, locked, while it is as its claim left it (not cancelled, nor
    claimed again since)."""
    trigger = await session.get(
        MadeToolTrigger, firing.trigger_id, with_for_update=True, populate_existing=True
    )
    if (
        trigger is None
        or trigger.state == "cancelled"
        or trigger.last_fired_at != firing.claimed_at
    ):
        return None
    return trigger


async def _missed(deps: LaunchDeps, firing: Firing, tool: AskingTool) -> None:
    """The sandbox did not answer the launch of ``firing``: its trigger is put back as it was
    before its claim, its try counted, for the next pass; or, once it was tried ``MISSED_MOST``,
    it stays settled and Domo is woken for its person with ``MISSED``."""
    now = deps.clock()
    since = firing.missed_since or firing.claimed_at
    async with deps.session_factory() as session:
        trigger = await _locked_as_claimed(session, firing)
        if trigger is None:
            return
        if now - since < MISSED_MOST:
            trigger.state = "waiting"
            trigger.due_at = firing.due_at
            trigger.repeat = firing.repeat
            trigger.last_fired_at = firing.last_fired_at
            trigger.missed_tries = firing.missed_tries + 1
            trigger.missed_since = since
            await session.commit()
            logger.info(
                "The sandbox did not answer a trigger of the tool %s: tried again", tool.name
            )
            return
        trigger.missed_tries = 0
        trigger.missed_since = None
        await session.commit()
    logger.warning("A trigger of the tool %s could not run for an hour", tool.name)
    # For whom the trigger is; the owner for the house.
    await wake_for(deps, firing.for_person_id, tool, MISSED)


async def _caught_up(deps: LaunchDeps, firing: Firing) -> None:
    """A trigger tried again ran: its tries start again from nothing."""
    async with deps.session_factory() as session:
        trigger = await _locked_as_claimed(session, firing)
        if trigger is not None:
            trigger.missed_tries = 0
            trigger.missed_since = None
            await session.commit()


async def _zone(session: AsyncSession) -> ZoneInfo:
    return (await load_policy_settings(session)).time_zone or ZoneInfo("UTC")


async def _claimed_each(
    session: AsyncSession, triggers: Sequence[MadeToolTrigger], now: datetime, zone: ZoneInfo
) -> list[Firing]:
    """``triggers`` claimed for ``now``, each apart: one that cannot be claimed never holds back
    the others."""
    firings: list[Firing] = []
    for trigger in triggers:
        try:
            async with session.begin_nested():
                firing = await _claimed(session, trigger, now, zone)
        except Exception as error:
            logger.error("A tool's trigger could not be claimed (%s)", type(error).__name__)
            continue
        if firing is not None:
            firings.append(firing)
    return firings


async def _claim_due(session: AsyncSession, now: datetime) -> list[Firing]:
    """Claim the time triggers due at ``now``, the arrival triggers someone's joining fires and
    the return triggers tried again, of tools that are ready and for someone who is in the
    household (or for the house), ``TRIGGERS_PER_PASS`` of each at most. Committed."""
    zone = await _zone(session)
    of_a_ready_tool = MadeToolTrigger.tool_id.in_(
        select(MadeTool.id).where(MadeTool.state == "ready")
    ) & _for_someone_here(now)
    found = await session.scalars(
        select(MadeToolTrigger)
        .where(
            MadeToolTrigger.state == "waiting",
            MadeToolTrigger.kind == "time",
            MadeToolTrigger.due_at <= now,
            of_a_ready_tool,
        )
        .order_by(MadeToolTrigger.due_at, MadeToolTrigger.id)
        .limit(TRIGGERS_PER_PASS)
        .with_for_update(skip_locked=True)
    )
    due: list[MadeToolTrigger] = list(found.all())
    # Since when an arrival trigger looks: its last firing, else the moment it was set.
    since = func.coalesce(MadeToolTrigger.last_fired_at, MadeToolTrigger.created_at)
    joined = (
        select(Invitation.id)
        .where(
            Invitation.accepted_person_id.is_not(None),
            Invitation.accepted_at > since,
            Invitation.accepted_at <= now,
        )
        .exists()
    )
    arrived = await session.scalars(
        select(MadeToolTrigger)
        .where(
            MadeToolTrigger.state == "waiting",
            MadeToolTrigger.kind == "arrival",
            of_a_ready_tool,
            joined,
        )
        .order_by(MadeToolTrigger.id)
        .limit(TRIGGERS_PER_PASS)
        .with_for_update(skip_locked=True)
    )
    due.extend(arrived.all())
    # A return whose launch the sandbox did not answer is the pass's to try again.
    returned = await session.scalars(
        select(MadeToolTrigger)
        .where(
            MadeToolTrigger.state == "waiting",
            MadeToolTrigger.kind == "return",
            MadeToolTrigger.missed_tries > 0,
            of_a_ready_tool,
        )
        .order_by(MadeToolTrigger.missed_since, MadeToolTrigger.id)
        .limit(TRIGGERS_PER_PASS)
        .with_for_update(skip_locked=True)
    )
    due.extend(returned.all())
    firings = await _claimed_each(session, due, now, zone)
    await session.commit()
    return firings


def _for_someone_here(now: datetime) -> ColumnElement[bool]:
    """A trigger for the house, or for someone who is in the household at ``now``: one for
    someone who left waits, neither fired nor moved, until they come back."""
    return or_(
        MadeToolTrigger.for_person_id.is_(None),
        MadeToolTrigger.for_person_id.in_(select(Person.id).where(active_at(now))),
    )


def triggers_pass(deps: TurnDeps) -> UpkeepPass:
    """The pass of the upkeep that fires the time triggers due, the arrival triggers someone's
    joining fires and the return triggers tried again (see the module docstring): claimed and
    committed, then launched in a task of ``deps.wakes``. It returns once they are started."""
    made = made_tool_deps(deps)

    async def fire_the_triggers_due(now: datetime) -> None:
        try:
            async with deps.session_factory() as session:
                firings = await _claim_due(session, now)
        except Exception as error:
            logger.error("The triggers due could not be read (%s)", type(error).__name__)
            return
        if firings:
            deps.wakes.spawn(fire(made, firings))

    return fire_the_triggers_due


async def returns_seen(deps: LaunchDeps, person_id: uuid.UUID, now: datetime) -> None:
    """``person_id`` came back after an absence at ``now`` (``conversation.starts.came_back``):
    the triggers of their return are claimed (once for this moment) and their tools launched, for
    whom each is (one for someone who is away waits). Never raises but for its cancellation: a
    trigger's failure never touches the return."""
    try:
        async with deps.session_factory() as session:
            zone = await _zone(session)
            found = await session.scalars(
                select(MadeToolTrigger)
                .where(
                    MadeToolTrigger.state == "waiting",
                    MadeToolTrigger.kind == "return",
                    MadeToolTrigger.returner_id == person_id,
                    _for_someone_here(now),
                    or_(
                        MadeToolTrigger.last_fired_at.is_(None),
                        MadeToolTrigger.last_fired_at < now,
                    ),
                )
                .order_by(MadeToolTrigger.id)
                .limit(TRIGGERS_PER_PASS)
                .with_for_update(skip_locked=True)
            )
            firings = await _claimed_each(session, list(found.all()), now, zone)
            await session.commit()
    except Exception as error:
        logger.error("A return's triggers could not be read (%s)", type(error).__name__)
        return
    await fire(deps, firings)
