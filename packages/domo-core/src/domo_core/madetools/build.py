"""Building a tool outside anyone's turn (spec 5.5, 5.7, 5.8, 6): a model writes its code, the
code is checked four ways (``madetools.checks``), and a version that passes them all becomes the
tool, or its new version.

A build (``ToolBuild``, opened by ``tool_build`` or ``tool_change``) is taken by the upkeep
(``madetools.upkeep.toolmaking_pass``), which claims it (``claim``: ``waiting``, or ``building``
with no progress for ``STALE_AFTER``, becomes ``building`` with the time of the pass) and runs its
rounds one after the other in a task (``run_build``) until it is ``ready``, ``abandoned``, or must
wait. A round (``build_round``) is one call of a model on the task ``toolmaking``
(``prompts/toolmaking.md``), with the model picker of the person who opened the build, or of the
owner when that person left. It is given, as JSON data, the purpose, the examples, the name asked
(and whether it is taken by another tool), the addresses open for a tool of that name
(``open_addresses``, which the reviewer is shown too: a build opens none), the round it is at, the
report of the build's last
submission and what the last round came to, for a ``tool_change``, the tool as it is, and, for a
build that starts from a program Domo ran by hand (``ToolBuild.from_run_id``, while the run is
kept), that program as its starting point (``start_from``: its code, operations and time). It
answers by calling ``submit`` or ``give_up``:

* ``submit``: the version is checked. The name must be the one asked, unless that name is taken
  by another tool (then a free name); a new version keeps its tool's name. It declares the tool's
  operations and the risks of each (``risks.read_operations``: what is not 1 to 10 well-formed
  operations is a finding of the imports check, and nothing more is checked), how long a launch
  may take (``max_seconds``) and how many launches a day it may have (``launches_per_day``), 1 at
  least, written in the tool's own settings (``run_seconds``, ``launches_per_day``) once it is
  made; what it does not say is left as it is (the house's, for a new tool). Its code has the
  house's ``code_chars`` at most. The version is kept (``MadeToolVersion``,
  with the build's id, its fingerprint and the report) ``ready`` when it passed, ``refused``
  otherwise, and the report is what the next round reads, with the digest of the whole submission
  (``SUBMITTED``: its code, name, description, parameters, time, operations and launches a day).
  A submission whose digest is that of a version this build already had refused (the same code,
  submitted the same way) is not checked again: the next round is told it submitted the same
  code;
* ``give_up``: the build is ``abandoned``, with the reason as its ``outcome``;
* neither: a round with nothing submitted, which the next round is told.

Each of these is a round (``ToolBuild.rounds``). There is no cap on the rounds (the owner's choice,
2026-11-18: the budget bounds a build, through the keys' limits); the model may give up or change
approach whenever it judges. Every round in which the model was asked and its answer arrived
counts; a call that did not arrive is a wait, bounded by the day (below), never a round. Rounds
that make no tool are paced past a few in a row (ruling R18, ``build_writes.pause_after``, at the
pace of Domo's settings): the build goes back to ``waiting``, not taken before its
``not_before``, which ``tool_look`` tells. The model is told the bounds its tool will run within
(``bounds``: the settings of the build's tool, or the house's for a new one).

A submission is kept before it is checked (a version ``checking``, with the rest of the
submission: ``keep``). When its checks cannot be made (the sandbox or the reviewer did not answer:
``CheckReport.waits``), the build waits with it, and its next round checks it again without asking
the model that writes: an outage costs a probe, never a new submission. A round with no model to
ask (no key the person may use, the key at its limit: no call was sent) waits too, uncounted; a
call that was sent and failed or took longer than ``making_seconds`` (the provider, or the
gatekeeper, out of reach) is not counted as a round, and ends as a wait: an outage is never
counted as a round, nor hits a provider that refuses again and again. A build that waits stays
``building``, its progress refreshed and its ``waits`` counted, and the upkeep takes it again later
and later (``retried_after``: 5 minutes, then twice as long at each wait in a row, an hour at
most); each wait adds to ``waited_seconds`` no more than its back-off step (a core that was down
does not count), and once it has waited its ``give_up_hours`` in all, it is abandoned
(``waited_too_long``), so that the requests waiting on it are done another way. Whatever raises once
the call was sent (reading the answer, keeping the submission, its checks, its settling) counts
the round and refuses the submission as not settled, and the next round is told (``UNSETTLED``);
``UNSETTLED_MOST`` such rounds in a row abandon the build. A text with a NUL character anywhere in a
submission, or values nested more than ``MAX_NESTING`` deep, are mistakes of its form, told to the
model (read without recursion). An abandoned build, like one whose tool was removed, has its
submission waiting for its checks refused (``service.refuse_unchecked``). A build abandoned for
any reason but its tool removed wakes Domo on his own with it once its round ended
(``madetools.troubles``: its name, why, the setting that caused it, the last error of its dry
runs).

A version that passed is made, in one transaction: for a new tool, its kind ``made.<name>``
(``policy.open_catalog.add_kind``, or ``replace_kind`` for a name a removed tool freed), whose four
risks are those of its operations together, and the tool, which takes the build's versions; for a
``tool_change``, the new version is the tool's in force, with its description, parameters, time,
operations and launches a day, its triggers, name and kind kept, the kind's risks those of its new
operations (a tool set aside is ready again: the new version passed every check). The build is
``ready`` and points at the tool. A tool is Domo's: its kind is open to every role, guests
included (the owner may close it to someone, and his rules on a kind outlive a tool removed or
rebuilt, whatever its risks become), whoever opened the build and whatever became of them.

The dry runs of the checks run for the house (``madetools.means``: a trial of the code, judged by
no one's rights, its reads and writes in the common space, rolled back), given the name of whoever
opened the build as whom they run for (``None`` for a build Domo opened on his own).

A build whose tool was removed was abandoned by ``tool_remove``: its next round ends. A round's
writes are made only while the build is still ``building`` with the progress the round read (its
claim): another worker that took it, or ``tool_remove``, leaves this one without a word
(``lost``).

The audit log keeps each check (``madetool.checked``: the tool's name, the round, each check's
result and counts, ``CheckReport.audited``), a tool built (``madetool.built``) and a build
abandoned (``madetool.build_abandoned``): the name and the rounds; never the code, the
purpose, the examples or the reviewer's words.
"""

import json
import logging
import re
import uuid
from contextlib import aclosing
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from functools import cache
from importlib import resources
from typing import Final, cast

import anyio
from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.accounts.sessions import is_active
from domo_core.conversation.prepare import TurnDeps, made_tool_deps
from domo_core.llm.costs import estimate_tokens
from domo_core.llm.person_picker import person_picker
from domo_core.llm.picker import ModelPicker
from domo_core.llm.types import (
    ChatMessage,
    ChatModel,
    ModelCallError,
    ToolCall,
    ToolDefinition,
)
from domo_core.madetools.build_writes import (
    ABANDONED,
    BUILT,
    CHECKED,
    MAX_OUTCOME,
    OPEN_TO,
    PENDING,
    SUBMITTED,
    UNSETTLED,
    UNSETTLED_MOST,
    UNSETTLED_TOO_OFTEN,
    Reading,
    RoundEnd,
    RoundOutcome,
    Submission,
    abandoned,
    count,
    digest,
    keep,
    settle,
    unsettled,
    wait,
    waited_too_long,
)
from domo_core.madetools.checks import (
    MAX_DESCRIPTION,
    CheckDeps,
    CheckReport,
    check,
)
from domo_core.madetools.launch import LaunchDeps
from domo_core.madetools.models import (
    NAME_PATTERN,
    HandRun,
    MadeTool,
    MadeToolVersion,
    ToolBuild,
)
from domo_core.madetools.risks import OperationsNotRight, read_operations
from domo_core.madetools.schema import unsupported
from domo_core.madetools.service import (
    build_in_progress,
    tool_named,
)
from domo_core.madetools.settings import all_values, own_values, told, wait_for
from domo_core.madetools.troubles import wake_for_trouble
from domo_core.outward.service import open_for
from domo_core.policy.settings import load_policy_settings
from domo_core.text import is_plain_line

__all__ = [
    "ABANDONED",
    "BUILT",
    "CHECKED",
    "OPEN_TO",
    "UNSETTLED",
    "UNSETTLED_MOST",
    "UNSETTLED_TOO_OFTEN",
    "RoundEnd",
    "waited_too_long",
    "RoundOutcome",
]

logger = logging.getLogger(__name__)

# A build ``building`` with no progress for this long is nobody's any more (a restart). One that
# waits (for a model, the sandbox or the reviewer) is tried again later and later: this, then twice
# as long at each wait in a row, ``WAIT_MOST`` at most (``retried_after``).
STALE_AFTER: Final = timedelta(minutes=5)
WAIT_MOST: Final = timedelta(hours=1)
# How long one call of the model that writes a tool may take, and how much it may write (its
# thinking and the code).
MAKING_OUTPUT_TOKENS: Final = 16_000
# The bounds a round tells the model that writes (``bounds``): those its tool's launches run
# within, which Domo may change.
BOUNDS_TOLD: Final = (
    "chain_depth",
    "chain_launches",
    "requests_per_launch",
    "answer_seconds",
    "run_seconds",
    "result_bytes",
    "rows",
    "statement_seconds",
    "request_seconds",
    "dry_run_seconds",
    "launches_per_day",
    "wakes_per_day",
    "triggers_per_tool",
    "trigger_argument_chars",
    "later_days",
    "wake_bytes",
    "code_chars",
)
# What a round tells the next one of itself, when it kept no version.
NOTHING_SUBMITTED: Final = "you called neither submit nor give_up: nothing was submitted"
SAME_CODE: Final = (
    "you submitted the same code, the same way, as a version already refused: it was not "
    "checked again"
)
_UNREADABLE: Final = "your submission could not be read: "
_NUL: Final = "a text holds a NUL character"
# How deep a submission's values may nest (its parameters, a JSON Schema, need few levels).
MAX_NESTING: Final = 32
_TOO_DEEP: Final = f"its values nest more than {MAX_NESTING} levels deep"
# The failures of a call that was never sent: nothing was paid, the round waits.
_NOT_SENT: Final = frozenset({"no_model", "limit_reached"})
TOOL_REMOVED: Final = "the tool was removed"

SUBMIT: Final = ToolDefinition(
    name="submit",
    description=(
        "Submit the tool: its name, its description, its parameters, its operations and their "
        "risks, its code, how long a launch may take and how many launches a day it may have. "
        "It is then checked; you read the report at the next round."
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "pattern": NAME_PATTERN},
            "description": {"type": "string", "minLength": 1, "maxLength": MAX_DESCRIPTION},
            "parameters": {
                "type": "object",
                "description": "The JSON Schema of the arguments, its top level an object.",
            },
            "operations": {
                "type": "object",
                "description": (
                    "What the tool does, by operation name (1 to 30 of a-z, 0-9 and _): "
                    '{"<name>": {"says": "<one plain line>", "risks": [<among costs, '
                    'irreversible, physical, outward>]}}, 1 to 10 of them; "harmless": true '
                    "on one whose only risk is physical and that can hurt or open nothing (a "
                    "light, never a lock). One that goes out "
                    'also names its accounts ("accounts": [<name>]) and the addresses it sends '
                    'to ("addresses": [{"host", "port"}]), 10 of each at most.'
                ),
            },
            "code": {"type": "string", "minLength": 1},
            "max_seconds": {"type": "integer", "minimum": 1},
            "launches_per_day": {"type": "integer", "minimum": 1},
        },
        "required": ["name", "description", "parameters", "operations", "code"],
        "additionalProperties": False,
    },
)
GIVE_UP: Final = ToolDefinition(
    name="give_up",
    description=(
        "Stop making this tool, with why: you will do the request another way, and remember why."
    ),
    parameters={
        "type": "object",
        "properties": {"reason": {"type": "string", "minLength": 1, "maxLength": MAX_OUTCOME}},
        "required": ["reason"],
        "additionalProperties": False,
    },
)


@cache
def toolmaking_prompt() -> str:
    """The system prompt of the model that writes a tool (``prompts/toolmaking.md``)."""
    path = resources.files("domo_core.prompts").joinpath("toolmaking.md")
    return path.read_text(encoding="utf-8").strip()


@dataclass(frozen=True)
class _GiveUp:
    reason: str


@dataclass(frozen=True)
class _DidNotArrive:
    """A call that was sent and failed or took too long: not a round: a wait."""


_DID_NOT_ARRIVE: Final = _DidNotArrive()


@dataclass(frozen=True)
class _Waits:
    """A round that waits before the model is asked (no model to ask)."""

    progress: datetime


def retried_after(waits: int) -> timedelta:
    """How long a build ``building`` stays untouched after its last progress before a worker
    takes it again: ``STALE_AFTER`` (a worker a restart stopped), and for one that waited
    ``waits`` times in a row, twice as long at each wait after the first, ``WAIT_MOST`` at
    most."""
    if waits <= 1:
        return STALE_AFTER
    return min(STALE_AFTER * 2 ** min(waits - 1, 10), WAIT_MOST)


def may_take(build: ToolBuild, now: datetime) -> bool:
    """Whether a worker may take ``build`` at ``now``: ``waiting`` (from its ``not_before``, when
    its rounds are paced), or ``building`` untouched for ``retried_after`` its waits."""
    if build.state == "waiting":
        return build.not_before is None or build.not_before <= now
    return build.state == "building" and build.last_progress_at + retried_after(build.waits) <= now


async def claim(deps: TurnDeps, build_id: uuid.UUID, now: datetime) -> bool:
    """Take the build for a worker, if it may be taken (``may_take``): it becomes ``building`` with
    ``now`` as its progress. Whether it was taken (committed): the row is locked, so two workers
    never take it at once."""
    async with deps.session_factory() as session:
        build = await session.get(ToolBuild, build_id, with_for_update=True, populate_existing=True)
        if build is None or not may_take(build, now):
            return False
        if build.state == "building" and build.waits > 0:
            # The time since its last wait: it waited all along.
            waited = min(now - build.last_progress_at, retried_after(build.waits))
            build.waited_seconds += max(0, int(waited.total_seconds()))
        build.state = "building"
        build.not_before = None
        build.last_progress_at = now
        await session.commit()
    return True


async def run_build(deps: TurnDeps, build_id: uuid.UUID) -> None:
    """The rounds of a build the caller claimed (``claim``), one after the other, until it is
    over, waits or is no longer this worker's. It never raises: what goes wrong is logged, by its
    kind, and the build is left to be taken again once it is stale (a submission whose checks or
    settling raised is refused and counted first: ``unsettled``)."""
    note: str | None = None
    while True:
        try:
            outcome = await build_round(deps, build_id, told=note)
        except Exception as error:
            logger.error("A round of a tool's build failed (%s)", type(error).__name__)
            return
        if outcome.trouble is not None:
            # Abandoned: Domo is woken on his own with it.
            await wake_for_trouble(made_tool_deps(deps), outcome.trouble)
        if outcome.end != "again":
            return
        note = outcome.note


async def build_round(
    deps: TurnDeps, build_id: uuid.UUID, *, told: str | None = None
) -> RoundOutcome:
    """One round of the build ``build_id``, which must be ``building`` (see the module
    docstring). ``told`` is what the last round said of itself (``RoundOutcome.note``)."""
    reading = await _read(deps, build_id, told)
    if isinstance(reading, RoundOutcome):
        return reading
    pending = reading.pending
    model = None
    if pending is None:
        model = await reading.picker.model_for("toolmaking")
        if model is None:
            return await wait(deps, reading.build_id, reading.progress)
    # The call goes (and may be paid), or a submission waits: from here on, whatever happens,
    # reading the answer included, counts the round (``unsettled``).
    kept_id: uuid.UUID | None = None if pending is None else pending[0]
    try:
        if pending is not None:
            # A submission that waited for its checks: checked again, the model is not asked.
            version_id, given = pending
        else:
            assert model is not None
            answer = await _ask(reading, model)
            if answer is None:
                return await wait(deps, reading.build_id, reading.progress)
            if isinstance(answer, _GiveUp):
                return await _end(deps, reading, answer.reason)
            if isinstance(answer, _DidNotArrive):
                # The provider or the gatekeeper failed: no round is counted, the build waits.
                return await count(deps, reading, None, waits=True)
            if isinstance(answer, str):
                return await count(deps, reading, answer)
            if digest(answer) in reading.refused:
                return await count(deps, reading, SAME_CODE)
            kept = await keep(deps, reading, answer)
            if kept is None:
                return RoundOutcome("lost")
            version_id, progress = kept
            kept_id = version_id
            reading = replace(reading, progress=progress)
            given = answer
        report = await _checked(deps, reading, given)
        if report.waits:
            return await wait(deps, reading.build_id, reading.progress)
        return await settle(deps, reading, version_id, given, report)
    except Exception as error:
        logger.error("A round of a tool's build could not be settled (%s)", type(error).__name__)
        return await unsettled(deps, reading, kept_id)


# --- what a round reads --------------------------------------------------------------------------


async def _picker(
    session: AsyncSession, deps: TurnDeps, person: Person, now: datetime, build: ToolBuild
) -> ModelPicker | None:
    """The models of ``build`` paid by ``person``: a ``ModelPicker`` for the task ``toolmaking`` on
    the key ``choose_key`` takes among the keys they may use (a key past one of its limits is
    passed over), its calls naming the build and its tool, once there is one (what the tool cost
    to make, ``madetools.launches.figures``); ``None`` without one."""
    return await person_picker(
        session,
        person,
        session_factory=deps.session_factory,
        model_factory=deps.model_factory,
        clock=deps.clock,
        now=now,
        main_task="toolmaking",
        tool_id=build.tool_id,
        build_id=build.id,
    )


async def _active(
    session: AsyncSession, person_id: uuid.UUID | None, now: datetime
) -> Person | None:
    if person_id is None:
        return None
    person = await session.get(Person, person_id)
    return person if person is not None and is_active(person, now) else None


async def _owner(session: AsyncSession, now: datetime) -> Person | None:
    found = await session.scalars(select(Person).where(Person.role == "owner"))
    return next((person for person in found.all() if is_active(person, now)), None)


def _pending_of(version: MadeToolVersion) -> "tuple[uuid.UUID, Submission] | None":
    """The submission a version that waits for its checks keeps (``keep``)."""
    kept = version.checks.get(PENDING)
    if not isinstance(kept, dict):
        return None
    fields = cast(dict[str, object], kept)
    name, description = fields.get("name"), fields.get("description")
    parameters, max_seconds = fields.get("parameters"), fields.get("max_seconds")
    launches = fields.get("launches_per_day")
    if not (
        isinstance(name, str)
        and isinstance(description, str)
        and isinstance(parameters, dict)
        and (max_seconds is None or isinstance(max_seconds, int))
        and (launches is None or isinstance(launches, int))
    ):
        return None
    given = Submission(
        name,
        description,
        cast(dict[str, JsonValue], parameters),
        version.code,
        max_seconds,
        cast(JsonValue, fields.get("operations")),
        launches,
    )
    return version.id, given


async def _last_report(
    session: AsyncSession, build_id: uuid.UUID
) -> tuple[dict[str, object] | None, frozenset[str], "tuple[uuid.UUID, Submission] | None"]:
    """The report of the build's last submission that was settled, the digests of its
    submissions refused, and its submission that waits for its checks, if any."""
    found = await session.scalars(
        select(MadeToolVersion)
        .where(MadeToolVersion.build_id == build_id)
        .order_by(MadeToolVersion.created_at, MadeToolVersion.id)
    )
    versions = list(found.all())
    refused = frozenset(
        str(v.checks[SUBMITTED]) for v in versions if v.state == "refused" and SUBMITTED in v.checks
    )
    waiting = next((v for v in reversed(versions) if v.state == "checking"), None)
    pending = None if waiting is None else _pending_of(waiting)
    settled = [v for v in versions if v.state != "checking"]
    if not settled:
        return None, refused, pending
    # The digest is the code's own business: the model reads what the checks said.
    told = {key: value for key, value in settled[-1].checks.items() if key != SUBMITTED}
    return told, refused, pending


async def _read(deps: TurnDeps, build_id: uuid.UUID, told: str | None) -> Reading | RoundOutcome:
    """What a round reads, in one short session; or how it ends before the model is asked: a build
    whose tool was removed is abandoned."""
    read = await _read_in(deps, build_id, told)
    if isinstance(read, _Waits):
        return await wait(deps, build_id, read.progress)
    if isinstance(read, tuple):
        progress, reason = read
        # Domo removed its tool himself: nothing to wake him with.
        return await abandoned(deps, build_id, progress, reason, counted=False, in_trouble=False)
    return read


async def _read_in(
    deps: TurnDeps, build_id: uuid.UUID, told: str | None
) -> Reading | RoundOutcome | _Waits | tuple[datetime, str]:
    now = deps.clock()
    async with deps.session_factory() as session:
        build = await session.get(ToolBuild, build_id, populate_existing=True)
        if build is None or build.state != "building":
            return RoundOutcome("lost")
        progress = build.last_progress_at
        tool = None if build.tool_id is None else await session.get(MadeTool, build.tool_id)
        if build.tool_id is not None and (tool is None or tool.state == "removed"):
            return progress, TOOL_REMOVED
        opener = await _active(session, build.opened_by, now)
        payer = opener or await _owner(session, now)
        picker = None if payer is None else await _picker(session, deps, payer, now, build)
        if picker is None:
            return _Waits(progress)
        taken = await tool_named(session, build.name)
        name_taken = taken is not None and taken.id != build.tool_id
        report, refused, pending = await _last_report(session, build.id)
        zone = (await load_policy_settings(session)).time_zone
        # The bounds of the build's tool: its own, else the house's (a new tool's, the house's).
        values = await all_values(session, tool)
        told_model: dict[str, object] = {
            "round": build.rounds + 1,
            "purpose": build.purpose,
            "examples": build.examples,
            "name": build.name,
            "bounds": {key: values[key] for key in BOUNDS_TOLD},
            # Where the tool of this name may go out now: Domo opened them, the build opens none.
            "open_addresses": [
                address.model_dump(mode="json") for address in await open_for(session, build.name)
            ],
        }
        if name_taken:
            told_model["name_taken"] = True
        if tool is not None:
            current = (
                None
                if tool.current_version_id is None
                else await session.get(MadeToolVersion, tool.current_version_id)
            )
            # The time and the launches a day the tool carries of its own (``None``: it follows
            # the house's, in ``bounds``): copied back, they never pin the house's on it.
            own = own_values(tool)
            told_model["current"] = {
                "description": tool.description,
                "parameters": tool.parameters,
                "operations": tool.operations,
                "max_seconds": own.get("run_seconds"),
                "launches_per_day": own.get("launches_per_day"),
                "code": None if current is None else current.code,
            }
        start = None if build.from_run_id is None else await session.get(HandRun, build.from_run_id)
        if start is not None:
            # The program Domo ran by hand for this, while it is kept: the starting point.
            told_model["start_from"] = {
                "code": start.code,
                "operations": start.operations,
                "max_seconds": start.max_seconds,
            }
        if report is not None:
            told_model["last_report"] = report
        if told is not None:
            told_model["last_round"] = told
        return Reading(
            build_id=build.id,
            progress=progress,
            name=build.name,
            purpose=build.purpose,
            examples=list(build.examples),
            tool_id=build.tool_id,
            rounds=build.rounds,
            name_taken=name_taken,
            message=json.dumps(told_model, ensure_ascii=False),
            picker=picker,
            refused=refused,
            now_text=now.astimezone(UTC if zone is None else zone).isoformat(),
            # The dry runs run for the one whose turn opened the build (none: the house).
            for_name=None if opener is None else opener.display_name,
            pending=pending,
            settings=values,
        )


# --- asking the model ----------------------------------------------------------------------------


def _submission(raw: str) -> Submission | str:
    """The submission in a call's arguments, or what the next round is told of it."""
    try:
        given: object = json.loads(raw)
    except (ValueError, RecursionError):
        return _UNREADABLE + "its arguments are not JSON"
    if not isinstance(given, dict):
        return _UNREADABLE + "its arguments are not an object"
    arguments = cast(dict[str, object], given)
    name, description = arguments.get("name"), arguments.get("description")
    parameters, code = arguments.get("parameters"), arguments.get("code")
    # Left out: the tool's settings say it (the house's, for a new tool).
    max_seconds = arguments.get("max_seconds")
    launches = arguments.get("launches_per_day")
    if not isinstance(name, str) or not isinstance(description, str):
        return _UNREADABLE + "name and description are texts"
    if not isinstance(parameters, dict):
        return _UNREADABLE + "parameters is an object"
    if not isinstance(code, str) or not code.strip():
        return _UNREADABLE + "code is a text that is not empty"
    if max_seconds is not None and (
        not isinstance(max_seconds, int) or isinstance(max_seconds, bool)
    ):
        return _UNREADABLE + "max_seconds is a whole number"
    if launches is not None and (not isinstance(launches, int) or isinstance(launches, bool)):
        return _UNREADABLE + "launches_per_day is a whole number"
    wrong = _form_of(arguments)
    if wrong is not None:
        return _UNREADABLE + wrong
    return Submission(
        name,
        description,
        cast(dict[str, JsonValue], parameters),
        code,
        max_seconds,
        # Read with the checks: what is wrong with them is a finding the next round reads.
        cast(JsonValue, arguments.get("operations")),
        launches,
    )


def _form_of(value: object) -> str | None:
    """What is wrong with the texts and the nesting of ``value`` (keys and values): a NUL
    character, which the database refuses, or more than ``MAX_NESTING`` levels. Read without
    recursion."""
    stack: list[tuple[object, int]] = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        if depth > MAX_NESTING:
            return _TOO_DEEP
        if isinstance(item, str):
            if "\x00" in item:
                return _NUL
        elif isinstance(item, dict):
            for key, inner in cast(dict[object, object], item).items():
                stack.append((key, depth + 1))
                stack.append((inner, depth + 1))
        elif isinstance(item, list):
            stack.extend((inner, depth + 1) for inner in cast(list[object], item))
    return None


def _reason(raw: str) -> str:
    """The reason of a ``give_up``, as a build's outcome keeps it: one plain line, cut at
    ``MAX_OUTCOME``."""
    try:
        given: object = json.loads(raw)
    except (ValueError, RecursionError):
        given = None
    reason = cast(dict[str, object], given).get("reason") if isinstance(given, dict) else None
    text = " ".join(reason.split())[:MAX_OUTCOME].strip() if isinstance(reason, str) else ""
    if not text or "\x00" in text or not is_plain_line(text):
        return "gave up without saying why"
    return text


async def _ask(
    reading: Reading, model: ChatModel
) -> Submission | _GiveUp | _DidNotArrive | str | None:
    """The round's call of ``model``: what it submitted, that it gave up, what the next round is
    told of an answer that submitted nothing usable, or ``_DID_NOT_ARRIVE`` (a call sent that
    failed or took too long); ``None`` when the call was refused before it went (a key at its
    limit)."""
    system = toolmaking_prompt()
    reading.picker.min_context = estimate_tokens(len(system) + len(reading.message))
    calls: list[ToolCall] = []
    with anyio.move_on_after(wait_for(reading.settings["making_seconds"])) as waited:
        try:
            stream = model.stream(
                system=system,
                messages=[ChatMessage("user", reading.message)],
                max_output_tokens=MAKING_OUTPUT_TOKENS,
                tools=[SUBMIT, GIVE_UP],
            )
            async with aclosing(stream) as items:
                async for item in items:
                    if isinstance(item, ToolCall):
                        calls.append(item)
        except ModelCallError as error:
            logger.info("The model that writes a tool did not answer (%s)", error.kind)
            return None if error.kind in _NOT_SENT else _DID_NOT_ARRIVE
    if waited.cancelled_caught:
        logger.info("The model that writes a tool took too long")
        return _DID_NOT_ARRIVE
    for call in calls:
        if call.name == GIVE_UP.name:
            return _GiveUp(_reason(call.arguments))
        if call.name == SUBMIT.name:
            return _submission(call.arguments)
    return NOTHING_SUBMITTED


# --- the checks ----------------------------------------------------------------------------------


async def _form_findings(deps: TurnDeps, reading: Reading, given: Submission) -> list[str]:
    """What is wrong with a submission beyond its code: its name (the one asked, unless that one
    is taken; the tool's own for a new version; free), its operations, its time, its launches a
    day and its size."""
    found: list[str] = []
    if re.fullmatch(NAME_PATTERN, given.name) is None:
        found.append("the name is 1 to 40 characters: a to z, 0 to 9 and _, from a letter")
    elif reading.tool_id is not None and given.name != reading.name:
        found.append(f"a new version keeps the tool's name: {reading.name}")
    elif given.name != reading.name and not reading.name_taken:
        found.append(f"keep the name asked: {reading.name}")
    else:
        async with deps.session_factory() as session:
            taken = await tool_named(session, given.name)
            other_build = (
                None if given.name == reading.name else await build_in_progress(session, given.name)
            )
        if (taken is not None and taken.id != reading.tool_id) or other_build is not None:
            found.append(f"the name {given.name} is taken: choose another")
    if given.operations is None:
        found.append("operations: say what the tool does, by operation, and the risks of each")
    else:
        try:
            read_operations(given.operations)
        except OperationsNotRight as wrong:
            found.append(f"operations: {wrong}")
    if given.max_seconds is not None and given.max_seconds < 1:
        found.append("max_seconds is 1 at least")
    if given.launches_per_day is not None and given.launches_per_day < 1:
        found.append("launches_per_day is 1 at least")
    most = reading.settings["code_chars"]
    if len(given.code) > most:
        found.append(f"the code has more than {most} characters; {told('code_chars', most)}")
    return found


async def _checked(deps: TurnDeps, reading: Reading, given: Submission) -> CheckReport:
    form = await _form_findings(deps, reading, given)
    if form:
        report = CheckReport(parameters=unsupported(given.parameters))
        if not report.parameters:
            report.imports = form
        return report
    async with deps.session_factory() as session:
        # What the reviewer is shown beside the code: the addresses open for the tool now.
        open_now = await open_for(session, given.name)
    return await check(
        CheckDeps(
            # What answers the dry runs: nobody is woken by them, nothing is handed over.
            launch=LaunchDeps(
                session_factory=deps.session_factory,
                sandbox=deps.sandbox,
                clock=deps.clock,
                policy_base=deps.policy,
                tools_base=deps.tools_base,
            ),
            models=reading.picker,
            now=reading.now_text,
            for_name=reading.for_name,
        ),
        code=given.code,
        description=given.description,
        parameters=given.parameters,
        examples=reading.examples,
        purpose=reading.purpose,
        max_seconds=given.max_seconds,
        # Read already, without a finding (``_form_findings``).
        operations=read_operations(given.operations),
        name=given.name,
        settings=reading.settings,
        open_addresses=open_now,
    )


async def _end(deps: TurnDeps, reading: Reading, reason: str) -> RoundOutcome:
    return await abandoned(deps, reading.build_id, reading.progress, reason, counted=True)
