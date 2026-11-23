"""What a round of a tool's build writes (``madetools.build``): the round's own data (what it read,
``Reading``, and the submission, ``Submission``), how it ends (``RoundOutcome``), and its writes,
each in a short transaction of its own, made only while the build is still this worker's
(``_claimed``): a submission kept before it is checked (``keep``), a round counted (``count``),
a wait (``wait``), a round that could not be settled (``unsettled``), a version settled with its
report and the tool it makes (``settle``, ``_made``), and a build abandoned (``abandoned``,
``_abandon_in``: the round's outcome carries what Domo is woken with on his own,
``madetools.troubles``). See ``madetools.build`` for what each one means.

A counted round that made no tool (nothing usable submitted, the same code again, a version
refused, a round not settled) adds to ``ToolBuild.refused_in_a_row``; from the ``free_rounds``-th
in a row on (the first, when it is 0), the build pauses before its next round (``pause_after``:
``first_pause_seconds``, twice as long at each one more, ``longest_pause_seconds`` at most): it is
``waiting`` again, not taken before ``ToolBuild.not_before`` (the upkeep's pass respects it;
nothing sleeps, and a restart keeps it). A version that passes makes the tool, and the count is
reset. A wait (a model, the sandbox or the reviewer out of reach) neither counts nor resets it.
Those three, and ``give_up_hours`` (how long a build may wait in all before it is abandoned), are
settings of Domo's (``madetools.settings``), read by each write that needs them.

What a submission says of its tool's own bounds (``max_seconds``, ``launches_per_day``) is written
in the tool's settings (``run_seconds``, ``launches_per_day``:
``settings.write_for_tool_from_build``) when Domo's right on ``tool.set`` allows it (ruling R31:
the owner's deny holds through a build); what it does not say is left as it is. Refused, the tool
is built with its settings as they were, and the build's outcome says so (``SETTINGS_KEPT``).

A version that passes keeps what it made the tool with (its description, parameters and
operations), and becomes the one in force (``made_current``), as an earlier version Domo goes
back to does (``tools.made_tools``'s ``tool_back``). A version in force whose operations changed
takes back what was agreed for the old ones: an operation it no longer has, or whose risks
changed, has its standing agreements withdrawn and its stops that wait for a yes deleted (their
yes would run what nobody agreed to).
It is audited (``YES_WITHDRAWN``: the tool, the operations, how many of each; never an argument),
written in the ``madetool.built`` entry, and kept as the build's outcome (``withdrawn_outcome``),
which Domo reads with the tool (``tool_look``) to tell whom it concerns.

A version that passes and whose operations name addresses not open for its tool opens none of them
(spec 13.10: only an act of Domo's in a turn, or the owner's, opens an address): the build's
outcome says which (``missing_outcome``), for him to open those he judges right, and the
``madetool.built`` entry how many (``missing_addresses``), never which.
"""

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Final, Literal

from domo_common.sandbox_api import JsonObject
from pydantic import JsonValue
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.audit.log import append_audit
from domo_core.conversation.prepare import TurnDeps
from domo_core.llm.picker import ModelPicker
from domo_core.madetools.checks import CheckReport
from domo_core.madetools.launch import action_type_of
from domo_core.madetools.models import (
    ActionKind,
    LaunchStop,
    MadeTool,
    MadeToolVersion,
    StandingAgreement,
    ToolBuild,
)
from domo_core.madetools.risks import (
    RISK_WORDS,
    Operation,
    addresses_named,
    kept_operations,
    operations_of,
    read_operations,
)
from domo_core.madetools.service import fingerprint, refuse_unchecked
from domo_core.madetools.settings import (
    all_values,
    span,
    told,
    value,
    write_for_tool_from_build,
)
from domo_core.madetools.troubles import Trouble
from domo_core.outward.service import closed_by_the_owner, open_for
from domo_core.policy.catalog import NO_RISK, OpenKind, PolicyTables, Risks
from domo_core.policy.open_catalog import add_kind, replace_kind
from domo_core.policy.rights import load_rights
from domo_core.policy.roles import ROLES, domo_actor

# How many rounds in a row that could not be settled abandon a build.
UNSETTLED_MOST: Final = 3
MAX_OUTCOME: Final = 300

# The audit entries of a build.
CHECKED: Final = "madetool.checked"
BUILT: Final = "madetool.built"
ABANDONED: Final = "madetool.build_abandoned"
# What a new version took back of the yeses given for its old operations.
YES_WITHDRAWN: Final = "madetool.yes_withdrawn"


# Where a version's report keeps the digest of the submission it was.
SUBMITTED: Final = "submission"
UNSETTLED: Final = (
    "your last submission could not be settled (an error on our side): submit it again, or "
    "another way"
)
# Where a version that waits for its checks keeps the rest of its submission.
PENDING: Final = "pending"
# Why a build is abandoned without the model saying so.
UNSETTLED_TOO_OFTEN: Final = f"{UNSETTLED_MOST} rounds in a row could not be settled"


def waited_too_long(hours: int) -> str:
    """Why a build that waited ``hours`` in all (its ``give_up_hours``), for its checks or for an
    answer of the model that writes, is abandoned."""
    return f"it could not be written or checked for {hours} hours; {told('give_up_hours', hours)}"


# The roles the kind of a new tool is open to: every one, guests included (a tool is Domo's;
# the owner may close it to someone).
OPEN_TO: Final = frozenset(ROLES)

type RoundEnd = Literal["again", "ready", "abandoned", "waits", "paced", "lost"]


@dataclass(frozen=True)
class RoundOutcome:
    """How a round ended: another round follows (``again``, with what it tells the next one of
    itself, ``note``), the build is over (``ready``, ``abandoned``), it waits for a model, the
    sandbox or the reviewer (``waits``: tried again later), it pauses after too many rounds in a
    row that made no tool (``paced``: taken again from ``ToolBuild.not_before``), or it is no
    longer this worker's (``lost``)."""

    end: RoundEnd
    note: str | None = None
    # What Domo is woken with on his own once the round ended, when it abandoned the build
    # (``madetools.troubles``; none when it was abandoned because he removed its tool).
    trouble: Trouble | None = None


def digest(given: "Submission") -> str:
    """The digest of a whole submission: the same code submitted the same way has the same."""
    whole = json.dumps(
        {
            "name": given.name,
            "description": given.description,
            "parameters": given.parameters,
            "code": given.code,
            "max_seconds": given.max_seconds,
            "operations": given.operations,
            "launches_per_day": given.launches_per_day,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return fingerprint(whole)


@dataclass(frozen=True)
class Submission:
    """What the model submitted: the tool's name, description, parameters, code and time, its
    operations as given (``None``: left out; ``risks.read_operations`` reads them when it is
    checked) and how many launches a day it may have (the time and the launches ``None`` when it
    does not say: the tool's settings are left as they are)."""

    name: str
    description: str
    parameters: dict[str, JsonValue]
    code: str
    max_seconds: int | None
    operations: JsonValue
    launches_per_day: int | None


@dataclass(frozen=True)
class Reading:
    """What a round read of its build before the model is asked: its claim (``progress``), what
    the model is told, the picker of the round, and what the checks and the making need."""

    build_id: uuid.UUID
    progress: datetime
    name: str
    purpose: str
    examples: list[object]
    tool_id: uuid.UUID | None
    rounds: int
    name_taken: bool
    message: str
    picker: ModelPicker
    refused: frozenset[str]
    now_text: str
    for_name: str | None
    pending: "tuple[uuid.UUID, Submission] | None"
    # The bounds of the build's tool (``madetools.settings``: its own, else the house's, else the
    # starts), read with the rest: its checks run within them.
    settings: Mapping[str, int]


def pause_after(refused_in_a_row: int, settings: Mapping[str, int]) -> timedelta | None:
    """The pause before the next round of a build whose last ``refused_in_a_row`` counted rounds
    made no tool, by the pace ``settings`` say (see the module docstring); ``None``: none."""
    free = settings["free_rounds"]
    if refused_in_a_row < free:
        return None
    # The first pause comes after the round that made ``free`` of them, or after the first one.
    doubled = refused_in_a_row - max(free, 1)
    seconds = settings["first_pause_seconds"] * 2 ** min(doubled, 64)
    # Whole numbers until the end: the longest a date holds (``settings.HELD``) at most.
    return span(min(seconds, settings["longest_pause_seconds"]))


async def _made_no_tool(session: AsyncSession, build: ToolBuild, now: datetime) -> bool:
    """Count a round of ``build`` that made no tool; whether it now pauses (``waiting`` until
    ``not_before``), by the pace of the settings of now."""
    build.refused_in_a_row += 1
    pause = pause_after(build.refused_in_a_row, await all_values(session))
    if pause is None:
        return False
    build.state = "waiting"
    build.not_before = now + pause
    return True


async def _claimed(
    session: AsyncSession, build_id: uuid.UUID, progress: datetime
) -> ToolBuild | None:
    """The build, locked, while it is still this worker's: ``building`` with the progress the
    round read."""
    build = await session.get(ToolBuild, build_id, with_for_update=True, populate_existing=True)
    if build is None or build.state != "building" or build.last_progress_at != progress:
        return None
    return build


async def _abandon_in(
    session: AsyncSession,
    build: ToolBuild,
    reason: str,
    now: datetime,
    setting: tuple[str, int] | None = None,
) -> Trouble:
    """The build is abandoned for ``reason`` (the setting that caused it, with its figure:
    ``setting``), its submission waiting for its checks refused; what Domo is woken with."""
    await refuse_unchecked(session, [build.id])
    build.state = "abandoned"
    build.outcome = reason[:MAX_OUTCOME]
    build.last_progress_at = now
    await session.flush()
    await append_audit(
        session,
        actor="system",
        action=ABANDONED,
        details={"tool": build.name, "rounds": build.rounds},
    )
    return Trouble(
        "build_abandoned",
        build.name,
        reason[:MAX_OUTCOME],
        tool_id=build.tool_id,
        build_id=build.id,
        setting=setting,
    )


async def abandoned(
    deps: TurnDeps,
    build_id: uuid.UUID,
    progress: datetime,
    reason: str,
    *,
    counted: bool,
    in_trouble: bool = True,
) -> RoundOutcome:
    """The build is abandoned for ``reason`` (``counted``: the round that ends it is one;
    ``in_trouble``: Domo is woken with it, unless he caused it himself)."""
    async with deps.session_factory() as session:
        build = await _claimed(session, build_id, progress)
        if build is None:
            return RoundOutcome("lost")
        if counted:
            build.rounds += 1
        trouble = await _abandon_in(session, build, reason, deps.clock())
        await session.commit()
    return RoundOutcome("abandoned", trouble=trouble if in_trouble else None)


async def count(
    deps: TurnDeps, reading: Reading, note: str | None, *, waits: bool = False
) -> RoundOutcome:
    """A round that kept no version: counted, and told to the next one (``note``); or, when its
    answer did not arrive (``waits``), not counted (``rounds`` counts only the rounds whose answer
    arrived) and ended as a wait, which ``waits`` counts (``wait``: tried again later and later,
    abandoned once it waited ``give_up_hours`` in all)."""
    now = deps.clock()
    async with deps.session_factory() as session:
        build = await _claimed(session, reading.build_id, reading.progress)
        if build is None:
            return RoundOutcome("lost")
        build.last_progress_at = now
        if waits:
            build.waits += 1
            trouble = await _waited_too_long(session, build, now)
            if trouble is not None:
                await session.commit()
                return RoundOutcome("abandoned", trouble=trouble)
            await session.commit()
            return RoundOutcome("waits")
        build.rounds += 1
        build.waits = 0
        paced = await _made_no_tool(session, build, now)
        await session.commit()
    return RoundOutcome("paced" if paced else "again", note)


async def keep(
    deps: TurnDeps, reading: Reading, given: Submission
) -> tuple[uuid.UUID, datetime] | None:
    """Keep a submission before it is checked: a version ``checking`` with the rest of the
    submission, and the round counted (the model was asked). Its id and the build's new progress;
    ``None`` when the build is no longer this worker's."""
    now = deps.clock()
    async with deps.session_factory() as session:
        build = await _claimed(session, reading.build_id, reading.progress)
        if build is None:
            return None
        build.rounds += 1
        build.last_progress_at = now
        build.waits = 0
        version = MadeToolVersion(
            tool_id=build.tool_id,
            build_id=build.id,
            fingerprint=fingerprint(given.code),
            code=given.code,
            state="checking",
            checks={
                PENDING: {
                    "name": given.name,
                    "description": given.description,
                    "parameters": given.parameters,
                    "max_seconds": given.max_seconds,
                    "operations": given.operations,
                    "launches_per_day": given.launches_per_day,
                }
            },
            # Stamped by the database's clock, which always moves on: the last one is the
            # last report (``_last_report``).
        )
        session.add(version)
        await session.flush()
        version_id = version.id
        await session.commit()
    return version_id, now


async def wait(deps: TurnDeps, build_id: uuid.UUID, progress: datetime) -> RoundOutcome:
    """The round waits for a model, the sandbox or the reviewer: nothing more is counted, and the
    build is tried again later and later (``retried_after``), with the submission it may keep. The
    time it waited adds up (``claim``); once it is ``give_up_hours``, the build is abandoned
    (``waited_too_long``)."""
    now = deps.clock()
    async with deps.session_factory() as session:
        build = await _claimed(session, build_id, progress)
        if build is None:
            return RoundOutcome("lost")
        build.waits += 1
        build.last_progress_at = now
        trouble = await _waited_too_long(session, build, now)
        if trouble is not None:
            await session.commit()
            return RoundOutcome("abandoned", trouble=trouble)
        await session.commit()
    return RoundOutcome("waits")


async def _waited_too_long(
    session: AsyncSession, build: ToolBuild, now: datetime
) -> Trouble | None:
    """Whether ``build`` waited its ``give_up_hours`` in all: then it is abandoned for it (what
    Domo is woken with)."""
    hours = await value(session, "give_up_hours")
    if build.waited_seconds < hours * 3600:
        return None
    return await _abandon_in(session, build, waited_too_long(hours), now, ("give_up_hours", hours))


async def unsettled(deps: TurnDeps, reading: Reading, version_id: uuid.UUID | None) -> RoundOutcome:
    """Something raised once the model answered: the submission kept (``version_id``) is refused
    as not settled (it may be submitted again: it keeps no digest); without one (it could not be
    kept), the round is counted here and an empty version marks it. ``UNSETTLED_MOST`` such
    rounds in a row abandon the build."""
    now = deps.clock()
    async with deps.session_factory() as session:
        build = await _claimed(session, reading.build_id, reading.progress)
        if build is None:
            return RoundOutcome("lost")
        unsettled: dict[str, object] = {"passed": False, "unsettled": UNSETTLED}
        if version_id is None:
            build.rounds += 1
            session.add(
                MadeToolVersion(
                    tool_id=build.tool_id,
                    build_id=build.id,
                    fingerprint=fingerprint(""),
                    code="",
                    state="refused",
                    checks=unsettled,
                )
            )
        else:
            version = await session.get(MadeToolVersion, version_id, with_for_update=True)
            if version is not None and version.state == "checking":
                version.state = "refused"
                version.checks = unsettled
        build.last_progress_at = now
        build.waits = 0
        await session.flush()
        latest = await session.scalars(
            select(MadeToolVersion.checks)
            .where(MadeToolVersion.build_id == build.id)
            .order_by(MadeToolVersion.created_at.desc(), MadeToolVersion.id.desc())
            .limit(UNSETTLED_MOST)
        )
        last = list(latest.all())
        end: RoundEnd = "again"
        trouble: Trouble | None = None
        if len(last) == UNSETTLED_MOST and all("unsettled" in checks for checks in last):
            trouble = await _abandon_in(session, build, UNSETTLED_TOO_OFTEN, now)
            end = "abandoned"
        elif await _made_no_tool(session, build, now):
            end = "paced"
        await session.commit()
    return RoundOutcome(end, None if end == "abandoned" else UNSETTLED, trouble)


async def settle(
    deps: TurnDeps,
    reading: Reading,
    version_id: uuid.UUID,
    given: Submission,
    report: CheckReport,
) -> RoundOutcome:
    """Settle the version checked with its report, and make the tool when it passed. One
    transaction."""
    now = deps.clock()
    async with deps.session_factory() as session:
        build = await _claimed(session, reading.build_id, reading.progress)
        if build is None:
            return RoundOutcome("lost")
        version = await session.get(MadeToolVersion, version_id, with_for_update=True)
        if version is None or version.state != "checking":
            return RoundOutcome("lost")
        version.state = "ready" if report.passed else "refused"
        version.checks = {**report.for_model(), SUBMITTED: digest(given)}
        build.waits = 0
        await session.flush()
        checked: JsonObject = {"tool": given.name, "round": build.rounds, **report.audited()}
        end: RoundEnd = "again"
        if report.passed:
            # The tool's row before the audit's lock, as at the end of a run (``launch``): the
            # two never wait on each other.
            tool, made = await _made(session, build, reading, given, version, now, deps.policy)
            kept = made.pop("kept", None)
            missing = await missing_addresses(session, tool.name, tool.operations)
            if missing:
                # Their number alone: the addresses are in the outcome, Domo's to read.
                made["missing_addresses"] = len(missing)
            await append_audit(session, actor="system", action=CHECKED, details=checked)
            build.state = "ready"
            build.name = given.name
            build.tool_id = tool.id
            build.refused_in_a_row = 0
            build.not_before = None
            withdrawn = made.get("withdrawn")
            # What was withdrawn comes first and whole: only the outcome tells Domo to tell
            # whom it concerns. Then the settings the owner keeps from him (what the tool runs
            # by); the addresses it lacks take the room left, said short: tool_look lists them.
            closed = {
                address for address in missing if await closed_by_the_owner(session, *address)
            }
            said = outcome_of(
                withdrawn_outcome(withdrawn) if isinstance(withdrawn, dict) else None,
                kept if isinstance(kept, str) else None,
                tool.name,
                missing,
                closed,
            )
            if said:
                build.outcome = said
            await append_audit(
                session,
                actor="system",
                action=BUILT,
                details={
                    "tool": given.name,
                    "rounds": build.rounds,
                    "change": reading.tool_id is not None,
                    **made,
                },
            )
            end = "ready"
        else:
            await append_audit(session, actor="system", action=CHECKED, details=checked)
            if await _made_no_tool(session, build, now):
                end = "paced"
        build.last_progress_at = now
        await session.commit()
    return RoundOutcome(end)


def _risks_shown(risks: Risks | ActionKind | OpenKind) -> JsonObject:
    """The four risks, as the audit keeps them."""
    return {word: bool(getattr(risks, word)) for word in RISK_WORDS}


def withdrawn_outcome(withdrawn: JsonObject) -> str:
    """What a build that withdrew yeses keeps as its outcome, for Domo (one plain line)."""
    names = withdrawn.get("operations")
    listed = ", ".join(str(name) for name in names) if isinstance(names, list) else ""
    said = (
        f"built; what was agreed for {listed} (standing agreements, stops waiting for a yes) was "
        "withdrawn, as those operations were removed or their risks changed: tell whom it "
        "concerns, and ask again if needed"
    )
    return said[:MAX_OUTCOME]


async def missing_addresses(
    session: AsyncSession, name: str, operations: object
) -> list[tuple[str, int]]:
    """The addresses the ``operations`` kept on the tool ``name`` name (``risks.addresses_named``)
    that are not open for it now (``outward.service.open_for``), in order. Nothing is opened: an
    address opens by an act of Domo's, in a turn (spec 13.10)."""
    open_now = {(address.host, address.port) for address in await open_for(session, name)}
    named = addresses_named(operations_of(operations))
    return [address for address in named if address not in open_now]


def _address_text(address: tuple[str, int]) -> str:
    host, port = address
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def _fitted(head: str, groups: list[tuple[str, list[tuple[str, int]]]], room: int) -> str:
    """``head``, then each group's prefix and as many of its addresses as ``room`` holds (``...``
    when some are left out): the whole of it ``room`` characters at most (nothing when there is
    no room)."""
    if room <= 0:
        return ""
    text = head[:room]
    for prefix, addresses in groups:
        named = 0
        for address in addresses:
            piece = f"{prefix if named == 0 else ','} {_address_text(address)}"
            if len(text) + len(piece) > room:
                break
            text += piece
            named += 1
        if 0 < named < len(addresses) and len(text) + len(", ...") <= room:
            text += ", ..."
    return text


def outcome_of(
    withdrawn: str | None,
    kept: str | None,
    name: str,
    missing: list[tuple[str, int]],
    closed_by_owner: set[tuple[str, int]],
) -> str:
    """What a build that made its tool keeps as its outcome (``""``: nothing to say), never more
    than ``MAX_OUTCOME``: what was withdrawn first and whole (only the outcome tells Domo to tell
    whom it concerns), then the settings the owner keeps from him (what the tool runs by), then the
    addresses the tool lacks in the room left, said short (``missing_outcome``), or not at all
    when none is left (``tool_look`` lists them all)."""
    said = "; ".join(part for part in (withdrawn, kept) if part)[:MAX_OUTCOME]
    if not missing:
        return said
    room = MAX_OUTCOME - (len(said) + len("; ") if said else 0)
    lacking = missing_outcome(name, missing, closed_by_owner, room) if room > 0 else ""
    if not lacking:
        return said
    return f"{said}; {lacking}" if said else lacking


def missing_outcome(
    name: str,
    missing: list[tuple[str, int]],
    closed_by_owner: set[tuple[str, int]],
    room: int = MAX_OUTCOME,
) -> str:
    """What a build keeps as its outcome, for Domo, when its tool names addresses not open for
    it: how many, which (those the owner closed first: reopening one is Domo's judgement,
    weighing that closing, ruling R24), and that opening the others is his act; ``room``
    characters at most, said shorter when the room is short (``tool_look`` lists them all). One
    plain line."""
    count = len(missing)
    owners = [address for address in missing if address in closed_by_owner]
    others = [address for address in missing if address not in closed_by_owner]
    plural = "es" if count > 1 else ""
    whole = _fitted(
        f"built; {count} address{plural} its operations name {'are' if count > 1 else 'is'} "
        f"not open for it, and nothing opened them (tool_look lists them)",
        [
            ("; the owner closed (reopen only if things changed, your judgement):", owners),
            (f"; open those you judge right with address_open (tool: {name}):", others),
        ],
        MAX_OUTCOME,
    )
    complete = all(_address_text(address) in whole for address in missing)
    if complete and len(whole) <= room:
        return whole
    return _fitted(
        f"{count} address{plural} not open (tool_look)",
        [("; owner closed:", owners), (";", others)],
        room,
    )


async def _withdrawn(
    session: AsyncSession,
    tool: MadeTool,
    before: object,
    after: object,
    now: datetime,
) -> JsonObject | None:
    """What a new version of ``tool`` takes back (see the module docstring): for each operation of
    ``before`` it no longer has, or whose risks changed in ``after``, its standing agreements are
    withdrawn and its stops that wait for a yes deleted (a yes to one then finds none), audited.
    ``None`` when nothing was withdrawn."""
    old, new = operations_of(before), operations_of(after)
    changed = sorted(
        name
        for name, operation in old.items()
        if name not in new or new[name].risks != operation.risks
    )
    if not changed:
        return None
    agreements = await session.execute(
        update(StandingAgreement)
        .where(
            StandingAgreement.tool_id == tool.id,
            StandingAgreement.operation.in_(changed),
            StandingAgreement.withdrawn_at.is_(None),
        )
        .values(withdrawn_at=now)
        .returning(StandingAgreement.id)
        .execution_options(synchronize_session=False)
    )
    # A stop's yes would run what nobody agreed to: the stop goes (a yes to it finds none).
    stops = await session.execute(
        delete(LaunchStop)
        .where(
            LaunchStop.tool_id == tool.id,
            LaunchStop.operation.in_(changed),
            LaunchStop.yes_used_at.is_(None),
        )
        .returning(LaunchStop.id)
        .execution_options(synchronize_session=False)
    )
    withdrawn: JsonObject = {
        "operations": list[JsonValue](changed),
        "agreements": len(agreements.all()),
        "stops": len(stops.all()),
    }
    if withdrawn["agreements"] or withdrawn["stops"]:
        await append_audit(
            session,
            actor="system",
            action=YES_WITHDRAWN,
            details={"tool": tool.name, **withdrawn},
        )
        return withdrawn
    return None


def _kind_of(name: str, description: str, operations: Mapping[str, Operation]) -> OpenKind:
    """The kind of the tool ``name``: its four risks are those of its ``operations`` together, open
    to every role (``OPEN_TO``)."""
    risks = NO_RISK
    for operation in operations.values():
        risks |= operation.risks
    return OpenKind(
        action_type=action_type_of(name),
        costs=risks.costs,
        irreversible=risks.irreversible,
        physical=risks.physical,
        outward=risks.outward,
        facts=MappingProxyType({}),
        default_roles=OPEN_TO,
        description=description,
    )


async def made_current(
    session: AsyncSession, tool: MadeTool, version: MadeToolVersion, now: datetime
) -> JsonObject:
    """Make ``version``, one that passed its checks and keeps what it made the tool with, the one
    in force of ``tool`` (whose row the caller holds): a new version a build made, or an earlier one
    Domo goes back to (``tools.made_tools``'s ``tool_back``). The tool takes its description,
    parameters and operations, and is ready again, its failures in a row forgotten; its kind keeps
    its roles (and the owner's rights on it, rows of their own) and takes the risks of those
    operations; what was agreed for an operation it no longer has, or whose risks changed, is
    withdrawn (``_withdrawn``). Flushed. What the ``madetool.built`` entry says of it: its kind's
    risks, those it had before, and what was withdrawn."""
    assert version.description is not None and version.parameters is not None
    operations = operations_of(version.operations)
    kind = _kind_of(tool.name, version.description, operations)
    known = await session.get(ActionKind, kind.action_type)
    told: JsonObject = {"risks": _risks_shown(kind)}
    if known is not None:
        told["risks_before"] = _risks_shown(known)
    withdrawn = await _withdrawn(session, tool, tool.operations, version.operations, now)
    if withdrawn is not None:
        told["withdrawn"] = withdrawn
    tool.description = version.description
    tool.parameters = dict(version.parameters)
    tool.operations = kept_operations(operations)
    tool.current_version_id = version.id
    tool.failures_in_a_row = 0
    if tool.state == "set_aside":
        tool.state = "ready"
        tool.aside_reason = None
    tool.changed_at = now
    if known is None:
        await add_kind(session, kind, now=now)
    else:
        known.costs, known.irreversible = kind.costs, kind.irreversible
        known.physical, known.outward = kind.physical, kind.outward
    await session.flush()
    return told


def _own_bounds(given: Submission) -> dict[str, int | None]:
    """What a submission says of its tool's own bounds, as the tool's settings name them."""
    return {"run_seconds": given.max_seconds, "launches_per_day": given.launches_per_day}


# Domo's act of setting one tool's own settings (``tools.made_tools``'s ``tool_set``), whose
# right a build's write of them needs too (ruling R31).
SET_FOR_A_TOOL: Final = "tool.set"
# The audit entry of a change of settings (as ``tools.made_tools``'s ``tools_settings`` and
# ``tool_set`` write it): the keys and their figures before and after, metadata alone.
SETTINGS_CHANGED: Final = "madetool.settings_changed"


def settings_kept(keys: list[str]) -> str:
    """What a build keeps as its outcome when the owner does not let Domo set a tool's own
    settings (``tool.set``): the tool was built, its ``keys`` left as they were."""
    said = (
        f"built; {', '.join(keys)} left as they were: the owner does not let you set a tool's own "
        "settings (tool_set)"
    )
    return said[:MAX_OUTCOME]


async def _bounds_written(
    session: AsyncSession, tool: MadeTool, given: Submission, policy: PolicyTables
) -> str | None:
    """What ``given`` says of its tool's own bounds written on ``tool``
    (``settings.write_for_tool_from_build``), and audited as a change of settings by Domo (spec
    12.1: every change is in the audit log): the keys and their figures before and after, never
    through ``settings.hit``. Nothing said, nothing written nor audited. Only when Domo's right
    on ``tool.set`` allows it (ruling R31, with ``policy``, the barrier's catalog): else nothing is
    written and what the build keeps as its outcome is returned (``settings_kept``)."""
    said = {key: number for key, number in _own_bounds(given).items() if number is not None}
    if not said:
        return None
    keys = sorted(said)
    rights = await load_rights(session)
    if not rights.resolve(domo_actor(), SET_FOR_A_TOOL, policy).allowed:
        return settings_kept(keys)
    before = await all_values(session, tool)
    write_for_tool_from_build(tool, said)
    after = await all_values(session, tool)
    details: JsonObject = {
        "tool": given.name,
        "keys": list[JsonValue](keys),
        "before": {key: before[key] for key in keys},
        "after": {key: after[key] for key in keys},
    }
    await append_audit(session, actor="domo", action=SETTINGS_CHANGED, details=details)
    return None


async def _made(
    session: AsyncSession,
    build: ToolBuild,
    reading: Reading,
    given: Submission,
    version: MadeToolVersion,
    now: datetime,
    policy: PolicyTables,
) -> tuple[MadeTool, JsonObject]:
    """The tool of a version that passed: a new tool and its kind, or the new version of the
    build's tool (see ``madetools.build``, ``made_current``), with its operations, and its time and
    launches a day in its settings when the submission says them and Domo's right allows it
    (``policy``; else ``kept`` in what is returned: the build's outcome, ``settings_kept``). The
    version keeps what it made the tool with. Its kind's four risks are those of its operations
    together. Flushed. With the tool, what the ``madetool.built`` entry says of it: its kind's
    risks, those it had before (a new version), and what was withdrawn."""
    operations = read_operations(given.operations)
    # What it made the tool with, kept on the version: Domo may go back to it (``made_current``).
    version.description = given.description
    version.parameters = dict(given.parameters)
    version.operations = kept_operations(operations)
    if build.tool_id is not None:
        tool = await session.get(MadeTool, build.tool_id, with_for_update=True)
        assert tool is not None, "a build's tool is removed, never deleted while it builds"
        kept = await _bounds_written(session, tool, given, policy)
        told = await made_current(session, tool, version, now)
        if kept is not None:
            told["kept"] = kept
        return tool, told
    kind = _kind_of(given.name, given.description, operations)
    known = await session.get(ActionKind, kind.action_type)
    told: JsonObject = {"risks": _risks_shown(kind)}
    if known is None:
        await add_kind(session, kind, now=now)
    else:
        # A name a removed tool freed: its kind is opened again for this one.
        await replace_kind(session, kind, now=now)
    tool = MadeTool(
        name=given.name,
        description=given.description,
        parameters=dict(given.parameters),
        operations=kept_operations(operations),
        settings={},
        created_at=now,
        changed_at=now,
    )
    kept = await _bounds_written(session, tool, given, policy)
    if kept is not None:
        told["kept"] = kept
    session.add(tool)
    await session.flush()
    await session.execute(
        update(MadeToolVersion)
        .where(MadeToolVersion.build_id == build.id, MadeToolVersion.tool_id.is_(None))
        .values(tool_id=tool.id)
        .execution_options(synchronize_session=False)
    )
    version.tool_id = tool.id
    tool.current_version_id = version.id
    await session.flush()
    return tool, told
