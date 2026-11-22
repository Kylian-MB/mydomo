"""The pass of the upkeep that builds Domo's tools (spec 5.5): outside anyone's turn.

``toolmaking_pass`` takes each build ``waiting``, or ``building`` untouched for long enough
(``build.may_take``: ``build.STALE_AFTER`` for a worker a restart stopped, later and later for one
that waits for a model, the sandbox or the reviewer), the oldest first, claims it in the database
(``build.claim``: two passes, or a pass and a restart, never build it twice at once) and runs its
rounds in a task of ``TurnDeps.wakes``
(``build.run_build``): one task per build, ``builds_at_once`` at most for the household (a setting
of Domo's, read at each pass); the others wait for a later pass. It returns once they are
started.

Each pass first forgets the programs Domo ran by hand more than ``hand_runs_kept_days`` ago (a
setting of Domo's)
(``forget_hand_runs``): their code and arguments go, with the stops that still waited on them (a
yes for one then runs nothing) and what of them waited to be told; a build that started from one
keeps going without it. A failure there is logged and the pass goes on with its builds: forgetting
old programs never keeps a tool from being made. It forgets too the traces of the launches of
his tools older than ``launches_kept_days`` (``forget_launches``), the same way.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timedelta
from functools import partial

from sqlalchemy import delete, select

from domo_core.conversation.prepare import TurnDeps
from domo_core.db import SessionFactory
from domo_core.madetools.build import claim, may_take, run_build
from domo_core.madetools.models import HandRun, MadeToolLaunch, ToolBuild
from domo_core.madetools.settings import HELD, span, value
from domo_core.upkeep import UpkeepPass

logger = logging.getLogger(__name__)


async def forget_hand_runs(session_factory: SessionFactory, now: datetime) -> int:
    """Delete the programs run by hand made more than ``hand_runs_kept_days`` before ``now`` (spec
    5.4: until then a build may start from one, a stop of it be run again by its yes; their stops
    and their rows to tell go with them; a build forgets where it started from); how many
    (committed). Kept longer than a date holds (``settings.HELD``): kept for good, nothing
    forgotten."""
    async with session_factory() as session:
        days = await value(session, "hand_runs_kept_days")
        if days * 86_400 >= HELD.total_seconds():
            return 0
        kept = timedelta(days=days)
        gone = await session.execute(
            delete(HandRun)
            .where(HandRun.created_at < now - kept)
            .returning(HandRun.id)
            .execution_options(synchronize_session=False)
        )
        count = len(gone.all())
        await session.commit()
    return count


async def forget_launches(session_factory: SessionFactory, now: datetime) -> int:
    """Delete the traces of the launches of Domo's tools that ended more than
    ``launches_kept_days`` before ``now`` (spec 12.2: what he reads of how his tools behave); how
    many (committed). Kept longer than a date holds (``settings.HELD``): kept for good, nothing
    forgotten."""
    async with session_factory() as session:
        kept = span(await value(session, "launches_kept_days") * 86_400)
        if kept >= HELD:
            return 0
        gone = await session.execute(
            delete(MadeToolLaunch)
            .where(MadeToolLaunch.at < now - kept)
            .returning(MadeToolLaunch.id)
            .execution_options(synchronize_session=False)
        )
        count = len(gone.all())
        await session.commit()
    return count


def toolmaking_pass(deps: TurnDeps) -> UpkeepPass:
    """The pass of the upkeep that starts the builds to make (see the module docstring)."""
    running: dict[uuid.UUID, asyncio.Task[None]] = {}

    def forget(build_id: uuid.UUID, task: asyncio.Task[None]) -> None:
        if running.get(build_id) is task:
            del running[build_id]

    async def make_tools(now: datetime) -> None:
        try:
            await forget_hand_runs(deps.session_factory, now)
        except Exception as error:
            # Its own failure: the builds of this pass are taken all the same. Its kind alone.
            logger.error(
                "the programs run by hand could not be forgotten (%s)", type(error).__name__
            )
        try:
            await forget_launches(deps.session_factory, now)
        except Exception as error:
            # As above: the builds of this pass are taken all the same. Its kind alone.
            logger.error(
                "the traces of old launches could not be forgotten (%s)", type(error).__name__
            )
        async with deps.session_factory() as session:
            found = await session.scalars(
                select(ToolBuild)
                .where(ToolBuild.state.in_(("waiting", "building")))
                .order_by(ToolBuild.created_at, ToolBuild.id)
            )
            to_take = [build.id for build in found.all() if may_take(build, now)]
            at_once = await value(session, "builds_at_once")
        for build_id in to_take:
            if len(running) >= at_once:
                break
            if build_id in running or not await claim(deps, build_id, now):
                continue
            task = deps.wakes.spawn(run_build(deps, build_id))
            running[build_id] = task
            task.add_done_callback(partial(forget, build_id))

    return make_tools
