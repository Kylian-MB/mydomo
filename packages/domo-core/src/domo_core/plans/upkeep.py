"""When a plan is due: the pass of the upkeep that wakes Domo for it (spec 6.9).

``plans_pass`` finds the plans whose time came, or whose build of a tool ended
(``plans.service.due``: both at once give one turn, and the plan is settled once), and starts, for
each person
they are of, ONE turn nobody asked for (``conversation.starts.plan_turn``), in a task of
``TurnDeps.wakes``, with all of that person's plans due under Domo's eyes: ``PLANS_AT_ONCE`` at
most, the soonest first, each with how late it is (a plan missed while Domo was stopped comes
with its lateness; he judges whether it still makes sense). He decides there: speak, act, set it
again for later, or do nothing. The plans due at a person's return are not this pass's: their
coming back gives them (``conversation.starts``, where the absence is weighed).

No turn while one of that person's runs: their plans stay due for a later pass. A turn that could
not be written (no key Domo may use for them, every key at its limit) changes nothing: the plans
stay due and come back at a later pass, later still; when every key of theirs is at its limit
(``no_room``: a reply was opened, then taken back), that person's plans are not tried again before
``upkeep.RETRY_AFTER``, rather than a reply written and taken back every minute. A turn that
failed leaves them due too
(``failed``, what came of them last); after ``FAILURES_BEFORE_WAITING`` such turns in a row, a plan
waits ``FAILED_WAIT`` before it is tried again, so that a turn that keeps failing never loops. The
waits are the pass's own (a restart forgets them). The code puts no other bound on how often
Domo wakes himself: the person's spending limit is the brake, and his context says how many times
he woke for them today.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timedelta
from functools import partial
from typing import Final

from domo_core.conversation.prepare import TurnDeps
from domo_core.conversation.starts import PLANS_AT_ONCE, PlanTurnEnd, plan_turn
from domo_core.plans.service import due
from domo_core.upkeep import RETRY_AFTER, UpkeepPass

logger = logging.getLogger(__name__)

# How many turns in a row a plan may fail before it waits, and how long it waits then.
FAILURES_BEFORE_WAITING: Final = 3
FAILED_WAIT: Final = timedelta(hours=1)


def plans_pass(deps: TurnDeps) -> UpkeepPass:
    """The pass of the upkeep that starts the turns of the plans due (see the module docstring):
    one per person, in a task of ``deps.wakes``, and none for someone whose turn still runs. It
    returns once they are started."""
    running: dict[uuid.UUID, asyncio.Task[None]] = {}
    # How many turns in a row each plan failed in, and until when one that failed too often waits.
    failures: dict[uuid.UUID, int] = {}
    waits: dict[uuid.UUID, datetime] = {}
    # Until when the plans of a person whose keys are all at their limit wait.
    no_room: dict[uuid.UUID, datetime] = {}

    def note(
        person_id: uuid.UUID, plan_ids: list[uuid.UUID], outcome: PlanTurnEnd | None, now: datetime
    ) -> None:
        if outcome is None:
            return  # No turn: nothing changed, they are tried again at the next pass.
        if outcome == "no_room":
            no_room[person_id] = now + RETRY_AFTER
            logger.info("A plan's turn waits for room under a limit: tried again in an hour")
            return
        no_room.pop(person_id, None)
        for plan_id in plan_ids:
            if outcome != "failed":
                failures.pop(plan_id, None)
                waits.pop(plan_id, None)
                continue
            failures[plan_id] = failures.get(plan_id, 0) + 1
            if failures[plan_id] >= FAILURES_BEFORE_WAITING:
                waits[plan_id] = now + FAILED_WAIT
                logger.info("A plan's turn failed %d times in a row: it waits", failures[plan_id])

    async def one(person_id: uuid.UUID, plan_ids: list[uuid.UUID], now: datetime) -> None:
        note(person_id, plan_ids, await plan_turn(deps, person_id, plan_ids), now)

    def forget(person_id: uuid.UUID, task: asyncio.Task[None]) -> None:
        if running.get(person_id) is task:
            del running[person_id]

    async def wake_for_the_plans_due(now: datetime) -> None:
        async with deps.session_factory() as session:
            found = await due(session, now)
        by_person: dict[uuid.UUID, list[uuid.UUID]] = {}
        for plan in found:
            if plan.id in waits and now < waits[plan.id]:
                continue
            if plan.person_id in no_room and now < no_room[plan.person_id]:
                continue
            given = by_person.setdefault(plan.person_id, [])
            if len(given) < PLANS_AT_ONCE:
                given.append(plan.id)
        for person_id, plan_ids in by_person.items():
            if person_id in running:
                continue
            task = deps.wakes.spawn(one(person_id, plan_ids, now))
            running[person_id] = task
            task.add_done_callback(partial(forget, person_id))

    return wake_for_the_plans_due
