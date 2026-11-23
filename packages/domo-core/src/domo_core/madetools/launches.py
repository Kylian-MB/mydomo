"""What Domo sees of how his tools behave (spec 12.2, 2026-11-21).

Each launch of a tool leaves its trace (``record``, a ``MadeToolLaunch``), written where the audit
keeps that it was launched (``madetools.launch``): when, its cause, the version that ran, whether
it was for a person or the house, whether a turn's call made it, how it ended, how long it ran,
how many requests it made, what its answer weighed, the error its program said, and, for a launch
that did not end ``done``, why the core or the sandbox ended or refused it (``reason``: their own
words, with the setting that did it and its figure, ``setting`` and ``value``), so that a launch no
turn read still tells Domo why (final review I3). That error is the program's words: Domo
reads it as data (under ``error``, as a failed launch gives it), and it never goes into the audit
log nor into anything a person sees; the reason is the core's, never the program's, and stays out
of the audit log too (its lines are metadata).

``figures`` gives Domo, by version (numbered from the first one made, ``versions_of``), how many
launches, how many failed, how long they usually take and the longest, how many ran without a turn
of a model, the last errors and the last reasons, and the launches of each day; and what the tool
cost to make: the tokens and the cost of the model calls of its builds (``model_calls.tool_id``,
``build_id``); and the requests it sent out (``out_figures``), read from their audit lines, which
hold metadata alone (no table of their own: a request out is audited already).
``latest`` gives its latest launches. Nothing here writes but ``record``; the upkeep forgets the
traces older than ``launches_kept_days`` (``madetools.upkeep.forget_launches``)."""

import json
import math
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final, cast
from zoneinfo import ZoneInfo

from domo_common.sandbox_api import JsonObject
from pydantic import JsonValue
from sqlalchemy import ColumnElement, Numeric, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.audit.log import details_as_text
from domo_core.audit.models import AuditEntry
from domo_core.llm.spending_models import ModelCall
from domo_core.madetools.models import (
    ERROR_KEPT,
    LaunchCause,
    LaunchEnd,
    MadeTool,
    MadeToolLaunch,
    MadeToolVersion,
    ToolBuild,
)
from domo_core.madetools.settings import held
from domo_core.outward.literal import is_a_host
from domo_core.plans.schedule import as_local
from domo_core.policy.settings import load_policy_settings

# How many of the last errors of a version are told.
ERRORS_TOLD: Final = 5
# The ends of a launch that are failures of the tool: the failure that set it aside too.
_FAILED: Final = ("failed", "set_aside")
# The second audit line of a request out, as ``madetools.means`` writes it (``means.OUT_DONE``:
# named here again, as ``means`` reaches this module through ``launch``).
_OUT_DONE: Final = "madetool.out_done"


def _kept_error(error: str | None) -> str | None:
    """``error`` (or a reason) as a trace keeps it: its first ``ERROR_KEPT`` characters, with what
    the database cannot hold in a text (a NUL, a lone surrogate) replaced."""
    if error is None:
        return None
    cut = error[:ERROR_KEPT].replace("\x00", "�")
    return cut.encode("utf-8", "replace").decode("utf-8")


async def record(
    session: AsyncSession,
    *,
    tool_id: uuid.UUID,
    version_id: uuid.UUID | None,
    at: datetime,
    cause: LaunchCause,
    for_house: bool,
    by_turn: bool,
    status: LaunchEnd,
    seconds: float,
    requests: int,
    reads: int,
    answer_bytes: int,
    error: str | None,
    reason: str | None = None,
    setting: tuple[str, int] | None = None,
) -> None:
    """Keep the trace of one launch of the tool ``tool_id`` (see the module docstring), in the
    caller's transaction: flushed, never committed. ``setting`` is the key and the figure of the
    setting that ended or refused it, kept as the database holds a count (``settings.held``)."""
    session.add(
        MadeToolLaunch(
            tool_id=tool_id,
            version_id=version_id,
            at=at,
            cause=cause,
            for_kind="house" if for_house else "person",
            by_turn=by_turn,
            status=status,
            seconds=seconds if math.isfinite(seconds) else None,
            requests=requests,
            reads=reads,
            answer_bytes=answer_bytes,
            error=_kept_error(error),
            reason=_kept_error(reason),
            setting=None if setting is None else setting[0],
            setting_value=None if setting is None else held(setting[1]),
        )
    )
    await session.flush()


async def versions_of(session: AsyncSession, tool: MadeTool) -> list[MadeToolVersion]:
    """The versions of ``tool``, the first one made first: the first is number 1."""
    found = await session.scalars(
        select(MadeToolVersion)
        .where(MadeToolVersion.tool_id == tool.id)
        .order_by(MadeToolVersion.created_at, MadeToolVersion.id)
    )
    return list(found.all())


def _seconds(value: object) -> float | None:
    return None if not isinstance(value, int | float) else round(float(value), 2)


async def figures(
    session: AsyncSession, tool: MadeTool, days: int = 30, *, now: datetime | None = None
) -> JsonObject:
    """How ``tool`` behaves, from the traces kept of its launches, by version
    (``per_version``, the versions that were launched, the first one first: its ``version``
    number, ``launches``, ``failed``, ``usual_seconds``, the median, ``longest_seconds``,
    ``without_a_turn``, ``errors``, the last ``ERRORS_TOLD`` distinct ones with their ``count`` and
    their ``last`` time, ``reasons``, the same of why the core or the sandbox ended them (each with
    its ``setting`` and ``value`` when one did), and ``per_day``, the launches of each of the last
    ``days`` days before ``now`` on the household's clock); and ``build``, what its builds cost:
    the tokens in and out and the cost in dollars of the model calls made for it; and ``out``, the
    requests it sent out (``out_figures``)."""
    now = datetime.now(UTC) if now is None else now
    zone = (await load_policy_settings(session)).time_zone
    numbers = {version.id: n for n, version in enumerate(await versions_of(session, tool), 1)}
    launch = MadeToolLaunch
    mine = launch.tool_id == tool.id
    counted = await session.execute(
        select(
            launch.version_id,
            func.count(),
            func.count().filter(launch.status.in_(_FAILED)),
            func.percentile_cont(0.5).within_group(launch.seconds),
            func.max(launch.seconds),
            func.count().filter(launch.by_turn.is_(False)),
        )
        .where(mine)
        .group_by(launch.version_id)
    )
    per_version: dict[uuid.UUID | None, JsonObject] = {}
    for version_id, launches, failed, usual, longest, without_a_turn in counted.all():
        per_version[version_id] = {
            "version": numbers.get(version_id) if version_id is not None else None,
            "launches": launches,
            "failed": failed,
            "usual_seconds": _seconds(usual),
            "longest_seconds": _seconds(longest),
            "without_a_turn": without_a_turn,
            "errors": [],
            "reasons": [],
            "per_day": [],
        }
    await _add_errors(session, tool, per_version, zone)
    await _add_reasons(session, tool, per_version, zone)
    await _add_days(session, tool, per_version, now - timedelta(days=days), zone)
    ordered = sorted(
        per_version.values(),
        key=lambda told: (told["version"] is None, told["version"] or 0),
    )
    return {
        "per_version": cast(list[JsonValue], ordered),
        "build": await _build_cost(session, tool),
        "out": await out_figures(session, tool, zone),
    }


def _number(details: Any, key: str) -> ColumnElement[Any]:
    """The number ``key`` of an audit line's details, ``NULL`` when it is no number (a line kept by
    hand is read without failing the whole reading)."""
    field = details[key]
    return case((func.jsonb_typeof(field) == "number", field.astext.cast(Numeric)), else_=None)


async def out_figures(session: AsyncSession, tool: MadeTool, zone: ZoneInfo | None) -> JsonObject:
    """The requests ``tool`` sent out since it was made (``requests``), read from the second audit
    line of each (``madetool.out_done``, ``madetools.means``: no table of their own): how many were
    refused, by code (``refused``), how many were answered with an error status, by status
    (``error_statuses``, 400 and over), the bytes sent and read (``bytes_out``, ``bytes_in``), and
    the slowest (``slowest``: its host, port, method, status or refusal, milliseconds and time;
    ``None`` when none was sent). Metadata alone: never a path, a header or a body."""
    details = AuditEntry.details
    # Its lines, their figures read once: what is grouped below is a column, never an expression
    # whose parameters the database would see twice.
    lines = (
        select(
            AuditEntry.id.label("id"),
            AuditEntry.ts.label("ts"),
            details_as_text(),
            details["refused"].astext.label("refused"),
            _number(details, "status").label("status"),
            _number(details, "bytes_out").label("bytes_out"),
            _number(details, "bytes_in").label("bytes_in"),
            _number(details, "ms").label("ms"),
        )
        .where(
            AuditEntry.action == _OUT_DONE,
            details["tool"].astext == tool.name,
            AuditEntry.ts >= tool.created_at,
        )
        .subquery()
    )
    totals = (
        await session.execute(
            select(
                func.count(),
                func.coalesce(func.sum(lines.c.bytes_out), 0),
                func.coalesce(func.sum(lines.c.bytes_in), 0),
            )
        )
    ).one()
    by_code = await session.execute(
        select(lines.c.refused, func.count())
        .where(lines.c.refused.is_not(None))
        .group_by(lines.c.refused)
    )
    by_status = await session.execute(
        select(lines.c.status, func.count())
        .where(lines.c.refused.is_(None), lines.c.status >= 400)
        .group_by(lines.c.status)
    )
    slowest = (
        await session.execute(
            select(lines.c.details, lines.c.ts)
            .where(lines.c.ms.is_not(None))
            .order_by(lines.c.ms.desc(), lines.c.id.desc())
            .limit(1)
        )
    ).first()
    return {
        "requests": int(totals[0]),
        "refused": {str(code): count for code, count in sorted(by_code.all())},
        "error_statuses": {str(int(code)): count for code, count in sorted(by_status.all())},
        "bytes_out": int(totals[1]),
        "bytes_in": int(totals[2]),
        "slowest": None if slowest is None else _slowest(slowest[0], slowest[1], zone),
    }


def _slowest(details_text: str, at: datetime, zone: ZoneInfo | None) -> JsonObject:
    """The slowest request out, as ``out_figures`` tells it, from its audit line."""
    try:
        read: object = json.loads(details_text)
    except ValueError:
        read = None
    line = cast(dict[str, JsonValue], read) if isinstance(read, dict) else {}
    host = line.get("host")
    return {
        **{key: line.get(key) for key in ("port", "method", "status", "refused", "ms")},
        # Only ever a valid host name or address, never text a program wrote (a line kept so).
        "host": host if isinstance(host, str) and is_a_host(host) else None,
        "at": as_local(at, zone),
    }


async def _add_errors(
    session: AsyncSession,
    tool: MadeTool,
    per_version: dict[uuid.UUID | None, JsonObject],
    zone: ZoneInfo | None,
) -> None:
    """The last distinct errors of each version, ``ERRORS_TOLD`` at most, the last first."""
    launch = MadeToolLaunch
    last = func.max(launch.at)
    grouped = (
        select(
            launch.version_id.label("version_id"),
            launch.error.label("error"),
            func.count().label("count"),
            last.label("last"),
            func.row_number()
            .over(partition_by=launch.version_id, order_by=(last.desc(), launch.error))
            .label("rank"),
        )
        .where(launch.tool_id == tool.id, launch.error.is_not(None))
        .group_by(launch.version_id, launch.error)
        .subquery()
    )
    found = await session.execute(
        select(grouped.c.version_id, grouped.c.error, grouped.c.count, grouped.c.last)
        .where(grouped.c.rank <= ERRORS_TOLD)
        .order_by(grouped.c.version_id, grouped.c.rank)
    )
    for version_id, error, count, at in found.all():
        told = per_version.get(version_id)
        if told is not None:
            errors = told["errors"]
            assert isinstance(errors, list)
            errors.append({"error": error, "count": count, "last": as_local(at, zone)})


async def _add_reasons(
    session: AsyncSession,
    tool: MadeTool,
    per_version: dict[uuid.UUID | None, JsonObject],
    zone: ZoneInfo | None,
) -> None:
    """The last distinct reasons the core or the sandbox ended a launch of each version for,
    ``ERRORS_TOLD`` at most, the last first, each with the setting that did it."""
    launch = MadeToolLaunch
    last = func.max(launch.at)
    grouped = (
        select(
            launch.version_id.label("version_id"),
            launch.reason.label("reason"),
            launch.setting.label("setting"),
            launch.setting_value.label("value"),
            func.count().label("count"),
            last.label("last"),
            func.row_number()
            .over(partition_by=launch.version_id, order_by=(last.desc(), launch.reason))
            .label("rank"),
        )
        .where(launch.tool_id == tool.id, launch.reason.is_not(None))
        .group_by(launch.version_id, launch.reason, launch.setting, launch.setting_value)
        .subquery()
    )
    found = await session.execute(
        select(
            grouped.c.version_id,
            grouped.c.reason,
            grouped.c.setting,
            grouped.c.value,
            grouped.c.count,
            grouped.c.last,
        )
        .where(grouped.c.rank <= ERRORS_TOLD)
        .order_by(grouped.c.version_id, grouped.c.rank)
    )
    for version_id, reason, setting, figure, count, at in found.all():
        told = per_version.get(version_id)
        if told is not None:
            reasons = told["reasons"]
            assert isinstance(reasons, list)
            shown: JsonObject = {"reason": reason, "count": count, "last": as_local(at, zone)}
            if setting is not None:
                shown.update({"setting": setting, "value": figure})
            reasons.append(shown)


async def _add_days(
    session: AsyncSession,
    tool: MadeTool,
    per_version: dict[uuid.UUID | None, JsonObject],
    since: datetime,
    zone: ZoneInfo | None,
) -> None:
    """How many launches each version made each day since ``since``, on the household's clock."""
    launch = MadeToolLaunch
    day = func.date(func.timezone("UTC" if zone is None else zone.key, launch.at))
    found = await session.execute(
        select(launch.version_id, day, func.count())
        .where(launch.tool_id == tool.id, launch.at >= since)
        .group_by(launch.version_id, day)
        .order_by(day)
    )
    for version_id, on, launches in found.all():
        told = per_version.get(version_id)
        if told is not None:
            days = told["per_day"]
            assert isinstance(days, list)
            days.append({"day": on.isoformat(), "launches": launches})


async def _build_cost(session: AsyncSession, tool: MadeTool) -> JsonObject:
    """The tokens in and out, and the cost in dollars, of the model calls made for ``tool``: those
    that name it, and those of its builds (a build of a new tool names no tool yet). A call not
    settled yet counts at what it reserved."""
    builds = select(ToolBuild.id).where(ToolBuild.tool_id == tool.id)
    summed = await session.execute(
        select(
            func.coalesce(func.sum(ModelCall.input_tokens), 0),
            func.coalesce(func.sum(ModelCall.output_tokens), 0),
            func.coalesce(func.sum(func.coalesce(ModelCall.cost_usd, ModelCall.reserved_usd)), 0),
        ).where(or_(ModelCall.tool_id == tool.id, ModelCall.build_id.in_(builds)))
    )
    tokens_in, tokens_out, usd = summed.one()
    return {
        "input_tokens": int(tokens_in),
        "output_tokens": int(tokens_out),
        "cost_usd": float(round(Decimal(usd), 6)),
    }


async def latest(
    session: AsyncSession, tool: MadeTool, most: int, zone: ZoneInfo | None
) -> Sequence[JsonObject]:
    """The ``most`` latest launches of ``tool``, the last first: how each ended, its cause, when,
    how long it ran, the error its program said (``None`` when it said none) and why the core or
    the sandbox ended it (``reason``, ``None`` for one done), with the setting that did it
    (``setting``, ``value``) when one did."""
    found = await session.scalars(
        select(MadeToolLaunch)
        .where(MadeToolLaunch.tool_id == tool.id)
        .order_by(MadeToolLaunch.at.desc(), MadeToolLaunch.id.desc())
        .limit(most)
    )
    return [_shown_line(line, zone) for line in found.all()]


def _shown_line(line: MadeToolLaunch, zone: ZoneInfo | None) -> JsonObject:
    """One launch as ``latest`` tells it."""
    shown: JsonObject = {
        "status": line.status,
        "cause": line.cause,
        "at": as_local(line.at, zone),
        "seconds": _seconds(line.seconds),
        "error": line.error,
        "reason": line.reason,
    }
    if line.setting is not None:
        shown.update({"setting": line.setting, "value": line.setting_value})
    return shown
