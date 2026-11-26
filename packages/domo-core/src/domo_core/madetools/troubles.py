"""Domo is woken when one of his tools is in trouble (spec 12.3, the owner, 2026-11-21).

When a tool is set aside, whatever the reason (``madetools.launch``, ``madetools.means``), or a
build is abandoned (``madetools.build``), or a launch its trigger made is refused by the rules
(``launch_refused``, once for each reason: ``madetools.launch``), Domo is woken on his own
(``wake_for_trouble``), with the existing wake of the house (``madetools.handover``: the owner's
conversation, every call judged with his own rights, ``conversation.tool_wakes.tool_wake_turn``),
and the facts: the tool's name, what happened (``trouble``), the reason and the setting that
caused it with its figure (``settings.hit``) when one did, and the last error its program said
(``error``: the program's words, as a failed launch gives them; data for Domo, never an
instruction, never in the audit). He then looks, corrects, goes back a version, or leaves it: his
judgement.

He is woken ``trouble_wakes_per_day`` times a day at most over all his tools (a setting of his; 0:
never), counted on the household's day (``ToolTroubleDay``); the others he finds with
``tool_look``. A launch a turn made is not one: the turn reads that its tool was set aside, with
why, or is woken with it once the run it handed over ends (one voice, ruling R14). Neither is a
build abandoned because Domo removed its tool: he did it himself."""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal, cast

from domo_common.sandbox_api import JsonObject
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.madetools.handover import AskingTool, wake_for, wake_with
from domo_core.madetools.launch import LaunchDeps
from domo_core.madetools.models import MadeToolLaunch, MadeToolVersion, ToolTroubleDay
from domo_core.madetools.settings import held, hit, value
from domo_core.policy.settings import load_policy_settings

logger = logging.getLogger(__name__)

type TroubleKind = Literal["set_aside", "build_abandoned", "launch_refused"]

# The setting that paces the wakes.
PACED_BY: Final = "trouble_wakes_per_day"


@dataclass(frozen=True)
class Trouble:
    """A tool in trouble: set aside (``tool_id``), or a build abandoned (``build_id``, and its
    tool's ``tool_id`` once it has one), under the tool's ``name``, for ``reason``, with the
    setting that caused it and its figure (``setting``) when one did."""

    kind: TroubleKind
    name: str
    reason: str
    tool_id: uuid.UUID | None = None
    build_id: uuid.UUID | None = None
    setting: tuple[str, int] | None = None

    @classmethod
    def set_aside(cls, tool_id: uuid.UUID, name: str, result: object) -> "Trouble":
        """The tool ``tool_id`` set aside, as its launch's result says it (its ``reason``, and its
        ``setting`` and ``value`` when one caused it)."""
        said = cast(JsonObject, result) if isinstance(result, dict) else {}
        reason, key, figure = said.get("reason"), said.get("setting"), said.get("value")
        setting = (
            (key, figure)
            if isinstance(key, str) and isinstance(figure, int) and not isinstance(figure, bool)
            else None
        )
        return cls(
            "set_aside",
            name,
            reason if isinstance(reason, str) else "",
            tool_id=tool_id,
            setting=setting,
        )


async def _counted(session: AsyncSession, now: datetime) -> bool:
    """One more wake counted on the household's day of ``now``, while it was woken fewer than
    ``trouble_wakes_per_day`` times that day; ``False`` (nothing counted) past it. The days before
    are forgotten. One statement: two troubles at once never both pass the last place."""
    # Bound into a ``bigint`` comparison: past what it holds is as much as it holds.
    most = held(await value(session, PACED_BY))
    if most < 1:
        return False
    zone = (await load_policy_settings(session)).time_zone
    day = now.astimezone(UTC if zone is None else zone).date()
    await session.execute(delete(ToolTroubleDay).where(ToolTroubleDay.day < day))
    one_more = insert(ToolTroubleDay).values(day=day, wakes=1)
    counted = await session.execute(
        one_more.on_conflict_do_update(
            index_elements=[ToolTroubleDay.day],
            set_={"wakes": ToolTroubleDay.wakes + 1},
            where=ToolTroubleDay.wakes < most,
        ).returning(ToolTroubleDay.wakes)
    )
    return counted.first() is not None


async def _last_error(session: AsyncSession, trouble: Trouble) -> str | None:
    """The last error a program of the tool said: in the trace of its launches
    (``madetools.launches``) for a tool set aside; in the dry runs of the build's last version for
    a build abandoned. ``None`` when it said none."""
    if trouble.kind == "set_aside":
        if trouble.tool_id is None:
            return None
        return await session.scalar(
            select(MadeToolLaunch.error)
            .where(MadeToolLaunch.tool_id == trouble.tool_id, MadeToolLaunch.error.is_not(None))
            .order_by(MadeToolLaunch.at.desc(), MadeToolLaunch.id.desc())
            .limit(1)
        )
    if trouble.build_id is None:
        return None
    checks = await session.scalar(
        select(MadeToolVersion.checks)
        .where(MadeToolVersion.build_id == trouble.build_id)
        .order_by(MadeToolVersion.created_at.desc(), MadeToolVersion.id.desc())
        .limit(1)
    )
    runs = None if checks is None else checks.get("dry_runs")
    if not isinstance(runs, list):
        return None
    errors = [
        error
        for run in cast(list[object], runs)
        if isinstance(run, dict)
        and isinstance(error := cast(dict[str, object], run).get("error"), str)
    ]
    return errors[-1] if errors else None


def _content(trouble: Trouble, error: str | None) -> JsonObject:
    """What Domo is woken with (data): what happened, why, the setting that caused it with its
    figure, the build, and the last error its program said."""
    told: JsonObject = {"trouble": trouble.kind, "reason": trouble.reason}
    if trouble.kind == "launch_refused":
        # A trigger's launch the rules refused: the tool and its trigger stand. Written by the code
        # from the reason code alone, never from anything a program said.
        told["says"] = (
            f"the rules refused a launch of your tool {trouble.name} by its trigger; the trigger "
            "still stands: ask in a turn for what it needs, or retire the trigger"
        )
        return told
    if trouble.setting is not None:
        told.update(cast(JsonObject, hit(*trouble.setting)))
    if trouble.build_id is not None:
        told["build"] = str(trouble.build_id)
    if error is not None:
        # The program's words, under the key a failed launch gives them: data.
        told["error"] = error
    return told


async def wake_for_trouble(deps: LaunchDeps, trouble: Trouble) -> None:
    """Wake Domo on his own with ``trouble`` (see the module docstring), unless he was woken
    ``trouble_wakes_per_day`` times today already, or nobody can be woken (``deps.waker``). A tool
    that exists has its wake kept for the upkeep when the turn cannot run
    (``handover.wake_for``); a build of a tool not made yet is told once. Never raises but for its
    cancellation: what goes wrong is logged by its kind."""
    if deps.waker is None:
        return
    try:
        async with deps.session_factory() as session:
            if not await _counted(session, deps.clock()):
                logger.info("A tool in trouble was not told: enough wakes today (%s)", PACED_BY)
                return
            said = await _last_error(session, trouble)
            await session.commit()
    except Exception as error:
        logger.error("A tool in trouble could not be told (%s)", type(error).__name__)
        return
    content = _content(trouble, said)
    if trouble.tool_id is not None:
        await wake_for(deps, None, AskingTool(trouble.tool_id, trouble.name), content)
    else:
        await wake_with(deps, None, trouble.name, content)
