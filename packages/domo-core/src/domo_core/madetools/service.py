"""Looking up and keeping the tools Domo makes: builds, tools and their launches.

Nothing here runs a tool or judges who may launch one: a tool is Domo's, every ready one is
returned (``ready_tools``) and the barrier decides, later, who it may be launched for. None of the
functions commits: the caller owns the transaction. A tool's code is never put in an audit entry
or a log; nothing here knows the web.
"""

import hashlib
import uuid
from datetime import UTC, datetime
from typing import Final
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.madetools.models import (
    MadeTool,
    MadeToolTrigger,
    MadeToolVersion,
    ToolBuild,
)

MAX_REASON: Final = 300
IN_PROGRESS: Final = ("waiting", "building")


def fingerprint(code: str) -> str:
    """The sha256 of a tool's code, in hex."""
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


async def build_in_progress(session: AsyncSession, name: str) -> ToolBuild | None:
    """The build of the tool ``name`` that is waiting or building, if there is one."""
    found = await session.execute(
        select(ToolBuild).where(ToolBuild.name == name, ToolBuild.state.in_(IN_PROGRESS))
    )
    return found.scalar_one_or_none()


async def open_build(
    session: AsyncSession,
    *,
    name: str,
    purpose: str,
    opened_by: uuid.UUID | None,
    examples: list[object],
    now: datetime,
    tool_id: uuid.UUID | None = None,
    from_run_id: uuid.UUID | None = None,
) -> ToolBuild:
    """Opens the build of the tool ``name`` (``opened_by``: in whose turn, whose key pays for
    it; ``None``: Domo on his own, the house's, the owner's key pays), starting from the program
    run by hand ``from_run_id`` when one is given, or gives back the one already waiting or
    building for that name (the database holds one per name, so two turns at once open one)."""
    existing = await build_in_progress(session, name)
    if existing is not None:
        return existing
    build = ToolBuild(
        name=name,
        purpose=purpose,
        opened_by=opened_by,
        examples=examples,
        tool_id=tool_id,
        from_run_id=from_run_id,
        last_progress_at=now,
        created_at=now,
    )
    try:
        async with session.begin_nested():
            session.add(build)
            await session.flush()
    except IntegrityError:
        # Another turn opened this name between the look and the insert.
        winner = await build_in_progress(session, name)
        if winner is None:
            raise
        return winner
    return build


async def builds_in_progress(session: AsyncSession) -> list[ToolBuild]:
    """The builds waiting or building, the oldest first."""
    found = await session.execute(
        select(ToolBuild)
        .where(ToolBuild.state.in_(IN_PROGRESS))
        .order_by(ToolBuild.created_at, ToolBuild.id)
    )
    return list(found.scalars())


async def ready_tools(session: AsyncSession) -> list[MadeTool]:
    """Every ready tool, by name: they are all Domo's, offered in anyone's turn (the barrier
    says who may use one). A tool is ready when its version in force is too (a tool without one is
    not offered)."""
    found = await session.execute(
        select(MadeTool)
        .join(MadeToolVersion, MadeToolVersion.id == MadeTool.current_version_id)
        .where(MadeTool.state == "ready", MadeToolVersion.state == "ready")
        .order_by(MadeTool.name)
    )
    return list(found.scalars())


async def tool_named(session: AsyncSession, name: str) -> MadeTool | None:
    """The tool called ``name`` in whatever state but removed (a removed tool frees its name)."""
    found = await session.execute(
        select(MadeTool).where(MadeTool.name == name, MadeTool.state != "removed")
    )
    return found.scalar_one_or_none()


async def set_aside(session: AsyncSession, tool: MadeTool, now: datetime, *, reason: str) -> None:
    """Sets the tool aside: it is no longer offered nor launched until it is made ready again (a
    new version that passed its checks). Its triggers are kept, paused: the upkeep neither fires
    nor moves them while it is aside, and once it is ready again a time that came meanwhile fires
    once, then its repeat goes on (``madetools.triggers``). ``reason`` (kept on the tool, at most
    300 characters) says nothing of the code."""
    tool.aside_reason = reason[:MAX_REASON] or None
    tool.state = "set_aside"
    tool.changed_at = now
    await session.flush()


async def count_launch(
    session: AsyncSession, tool: MadeTool, now: datetime, zone: ZoneInfo | None, most: int
) -> bool:
    """Counts a launch of the tool on the household's day (in ``zone``, else UTC): False, and
    nothing counted, once it was launched ``most`` times that day (its ``launches_per_day``, a
    setting of Domo's)."""
    await session.refresh(tool, with_for_update=True)
    _on_the_day(tool, now, zone)
    if tool.launches_today >= most:
        return False
    tool.launches_today += 1
    await session.flush()
    return True


def _on_the_day(tool: MadeTool, now: datetime, zone: ZoneInfo | None) -> None:
    """The counts of the day of ``tool`` start again on a new day of the household (in ``zone``,
    else UTC)."""
    day = now.astimezone(UTC if zone is None else zone).date()
    if tool.launches_day != day:
        tool.launches_day = day
        tool.launches_today = 0
        tool.wakes_today = 0


async def count_wake(
    session: AsyncSession, tool: MadeTool, now: datetime, zone: ZoneInfo | None, most: int
) -> bool:
    """Counts a time the tool asks to wake Domo, on the household's day (in ``zone``, else UTC):
    False, and nothing counted, once it asked ``most`` times that day (its ``wakes_per_day``, a
    setting of Domo's: each wake is a turn of a model, paid)."""
    await session.refresh(tool, with_for_update=True)
    _on_the_day(tool, now, zone)
    if tool.wakes_today >= most:
        return False
    tool.wakes_today += 1
    await session.flush()
    return True


async def cancel_triggers_of(session: AsyncSession, tool_ids: list[uuid.UUID]) -> int:
    """Cancels the triggers that wait of the tools ``tool_ids`` (they never fire again). How many
    were cancelled. Flushed, not committed."""
    if not tool_ids:
        return 0
    cancelled = await session.execute(
        update(MadeToolTrigger)
        .where(MadeToolTrigger.tool_id.in_(tool_ids), MadeToolTrigger.state == "waiting")
        .values(state="cancelled")
        .returning(MadeToolTrigger.id)
        .execution_options(synchronize_session=False)
    )
    return len(cancelled.all())


NOT_CHECKED: Final = "its build ended before its checks could run"


async def refuse_unchecked(session: AsyncSession, build_ids: list[uuid.UUID]) -> None:
    """The versions of the builds ``build_ids`` that still wait for their checks (``checking``)
    are refused: their build ended (abandoned, or its tool removed). Flushed, not committed."""
    if not build_ids:
        return
    await session.execute(
        update(MadeToolVersion)
        .where(MadeToolVersion.build_id.in_(build_ids), MadeToolVersion.state == "checking")
        .values(state="refused", checks={"passed": False, "not_checked": NOT_CHECKED})
        .execution_options(synchronize_session=False)
    )
