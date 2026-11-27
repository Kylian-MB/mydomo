"""A launch a turn waits on, and what the launches of Domo's tools wake him with (spec 5.1, 5.4,
5.8): what stays of the requests a tool gave back once it ended, now that a program asks the core
while it runs (``madetools.means``) and nothing is carried out after a launch.

Each chain of launches gathers the wakes its programs ask (``means.Chain``), one per person: a
launch for a person wakes that person alone, a launch for the house the owner, looked up when the
wake is told (``wake_chain``), in a turn where Domo acts on his own (``launch.Waker``,
``on_his_own``: a wake gathered for the house, or a row of ``made_tool_runs`` that is the house's,
``for_house``). Each wake is a paid turn (``conversation.tool_wakes.tool_wake_turn``),
where the content is data, never instructions; what a tool says twice becomes a list. When the
turn cannot run (no key, every key at its limit, the model failed before it acted), what it would
have told is kept in ``made_tool_runs`` (a ``wake`` row) for the upkeep to try again. A wake for
the house when the household has no owner now is dropped, and audited (``WAKE_DROPPED``): nobody
else is woken in the owner's place.

A turn (``launch_in_a_turn``, for its person, or for the house in a turn Domo acts in on his
own) waits ``HAND_OVER_SECONDS`` at most for the launch, which runs in a
task of the application: past that, or once the person stops the turn (they asked for the thing,
not for the turn), the turn is told ``running``, a row of ``made_tool_runs`` says the run was
handed over, and the task goes on; at its end the row holds what came of it (the result, and what
the chain asked to tell that person), and delivering it only wakes Domo for that person
(``deliver_run``: claimed, then settled once the turn ran). The upkeep (``deliver_runs``, the
pass ``conversation.tool_wakes.tool_wakes_pass``) delivers the rows a restart left: claimed and not
settled for ``CLAIM_AGAIN_AFTER`` (the person may be woken twice with the same result, never left
without it), or never finished long after the tool's time (the person is woken with a run that did
not finish, ``UNFINISHED``); and the rows whose turn could not run, later and later
(``retold_after``), until a day after they ended (``GIVEN_UP_AFTER``): then they are given up,
audited and logged.

A program Domo runs by hand (spec 5.4, ``tools.program_run``) is waited on and handed over the
same way (``AskingTool.of_run``: its rows point at its ``hand_runs`` row, and Domo is woken with
it as a program he ran by hand, by its run id, under the name of the tool he ran it with,
``BY_HAND``: ``prompts.ToolWake.run``).

The audit log keeps that a wake was given up (``madetool.wake_dropped``: the tools' names, and the
tries); never a content."""

import asyncio
import logging
import uuid
from collections.abc import Coroutine, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, cast

from domo_common.sandbox_api import JsonObject
from pydantic import JsonValue
from sqlalchemy import func, or_, select, true, update

from domo_core.accounts.models import Person
from domo_core.accounts.sessions import active_at
from domo_core.audit.log import append_audit
from domo_core.madetools.launch import ForWhom, LaunchDeps, Launched, launch, launch_by_hand
from domo_core.madetools.means import Chain, WakeFor
from domo_core.madetools.models import HandRun, LaunchStop, MadeTool, MadeToolRun
from domo_core.madetools.settings import own_values, span, value
from domo_core.policy.roles import Actor
from domo_core.prompts import ToolWake

logger = logging.getLogger(__name__)

# How long a turn waits for a launch before it hands it over (spec 5.1).
HAND_OVER_SECONDS: Final = 20.0
# The name a program run by hand is told under when it wakes Domo: the tool he ran it with.
BY_HAND: Final = "program_run"
# How many runs one pass of the upkeep delivers at most.
RUNS_PER_PASS: Final = 20
# How long a row claimed and not settled waits before the upkeep takes it again (a restart), and
# the longest it waits after turns that could not run (``retold_after``).
CLAIM_AGAIN_AFTER: Final = timedelta(minutes=5)
RETOLD_MOST: Final = timedelta(hours=1)
# How long after it ended a wake that could not be given is tried again before it is given up.
GIVEN_UP_AFTER: Final = timedelta(hours=24)
# How long past its tool's time a run handed over that never finished is taken as lost (a
# restart while its chain ran).
UNFINISHED_AFTER: Final = timedelta(minutes=5)
# What Domo reads of a run that a restart cut short.
UNFINISHED: Final = (
    "it did not finish: the place where it ran stopped before its end; do the request another way"
)

# The audit entry of a wake dropped: for someone who left, for the house with no owner, or given up
# after a day of tries.
WAKE_DROPPED: Final = "madetool.wake_dropped"

# The runs handed over whose chain still runs in this process: the upkeep never takes one of them
# for lost, however long it takes.
_RUNNING: set[uuid.UUID] = set()
# The rows being delivered in this process (a turn may take longer than a claim lasts): the upkeep
# never takes one of them again meanwhile.
_DELIVERING: set[uuid.UUID] = set()


@dataclass(frozen=True)
class AskingTool:
    """A tool as its row was read: its id and name; or a program Domo runs by hand
    (``by_hand``: the id of its ``HandRun``, and the name of the tool he runs it with,
    ``BY_HAND``), launched and handed over the same way."""

    id: uuid.UUID
    name: str
    by_hand: bool = False

    @classmethod
    def of(cls, tool: MadeTool) -> "AskingTool":
        return cls(tool.id, tool.name)

    @classmethod
    def of_run(cls, run_id: uuid.UUID) -> "AskingTool":
        """The program run by hand ``run_id``."""
        return cls(run_id, BY_HAND, by_hand=True)

    def row(self, **values: Any) -> MadeToolRun:
        """A row of ``made_tool_runs`` with ``values``, of this tool or program run by hand."""
        return MadeToolRun(
            tool_id=None if self.by_hand else self.id,
            hand_run_id=self.id if self.by_hand else None,
            **values,
        )


type Said = Sequence[tuple[str, list[JsonValue]]]


async def wake_chain(deps: LaunchDeps, chain: Chain) -> None:
    """Wake Domo once for each person the chain asked to wake (``None``: the owner, for the
    house), with what each tool said (two things a tool said: a list): each in a task of
    ``deps.wakes`` (``_start``). The chain's wakes are given once, then emptied."""
    gathered, chain.wakes = chain.wakes, {}
    for person_id, said in gathered.items():
        name = next(iter(said))
        first = AskingTool(chain.tool_ids[name], name)
        await _start(deps, _told_or_kept(deps, person_id, first, list(said.items())))


async def wake_for(
    deps: LaunchDeps, person_id: WakeFor, tool: AskingTool, content: JsonValue
) -> None:
    """Wake Domo for ``person_id`` (``None``: the owner, for the house) with what the code says
    of ``tool`` (``content``, data), as a tool's own wake is: kept for the upkeep when the turn
    cannot run."""
    await _start(deps, _told_or_kept(deps, person_id, tool, [(tool.name, [content])]))


async def wake_with(deps: LaunchDeps, person_id: WakeFor, name: str, content: JsonValue) -> None:
    """Wake Domo for ``person_id`` (``None``: the owner, for the house) with ``content`` (data),
    under the name of a tool not made yet (a build of it: ``madetools.troubles``): there is no row
    of a tool to keep it on, so a turn that cannot run is logged, not tried again."""

    async def told() -> None:
        if not await _tell(deps, person_id, [(name, [content])]):
            logger.warning("A wake of a build of a tool was not given")

    await _start(deps, told())


async def _start(deps: LaunchDeps, coroutine: Coroutine[Any, Any, None]) -> None:
    """Run ``coroutine`` in a task of ``deps.wakes``; without them, here and now."""
    if deps.wakes is None:
        await coroutine
    else:
        deps.wakes.spawn(coroutine)


async def _owner_now(deps: LaunchDeps) -> uuid.UUID | None:
    """The owner of the household now, who is woken for the house."""
    async with deps.session_factory() as session:
        return await session.scalar(
            select(Person.id).where(Person.role == "owner", active_at(deps.clock())).limit(1)
        )


async def _tell(
    deps: LaunchDeps, person_id: WakeFor, said: Said, *, hand_run: uuid.UUID | None = None
) -> bool:
    """Wake Domo for ``person_id`` (``None``: the owner of now, for the house, in a turn where
    he acts on his own) with what each tool of ``said`` says (what ``BY_HAND`` says, as the
    program run by hand ``hand_run``, by its id): whether the turn ran (``launch.Waker``; nobody
    to wake is nothing to try again). Never raises but for its cancellation."""
    if deps.waker is None or not said:
        return True
    wakes = [
        ToolWake(
            tool=name,
            content=contents[0] if len(contents) == 1 else contents,
            run=str(hand_run) if hand_run is not None and name == BY_HAND else None,
        )
        for name, contents in said
    ]
    try:
        whom = await _owner_now(deps) if person_id is None else person_id
        if whom is None:
            # Nobody else answers for the house: the wake is dropped, and the audit says so.
            logger.warning("A wake for the house found no owner to tell")
            await _dropped(deps, said)
            return True
        return await deps.waker(whom, wakes, on_his_own=person_id is None)
    except Exception as error:
        logger.error("The assistant could not be woken for a tool (%s)", type(error).__name__)
        return False


async def _dropped(deps: LaunchDeps, said: Said) -> None:
    """The audit entry of a wake for the house dropped for want of an owner: the tools' names
    alone."""
    tools: list[JsonValue] = [*sorted({name for name, _ in said})]
    async with deps.session_factory() as session:
        await append_audit(session, actor="system", action=WAKE_DROPPED, details={"tools": tools})
        await session.commit()


async def _told_or_kept(
    deps: LaunchDeps, person_id: WakeFor, source: AskingTool, said: Said
) -> None:
    """Wake Domo for ``person_id`` (``None``: the owner, for the house) with ``said``; when the
    turn could not run, what it would have told is kept (a ``wake`` row of ``made_tool_runs`` of
    ``source``, its first try counted; ``for_house`` when it is the house's) for the upkeep to try
    again (``deliver_runs``). Never raises but for its cancellation."""
    if await _tell(deps, person_id, said, hand_run=source.id if source.by_hand else None):
        return
    also: list[JsonValue] = [
        {"tool": name, "content": content} for name, contents in said for content in contents
    ]
    try:
        now = deps.clock()
        async with deps.session_factory() as session:
            session.add(
                source.row(
                    person_id=person_id,
                    for_house=person_id is None,
                    kind="wake",
                    status="done",
                    also=also,
                    handed_over_at=now,
                    finished_at=now,
                    claimed_at=now,
                    tries=1,
                )
            )
            await session.commit()
    except Exception as error:
        logger.error("A wake that could not be given was not kept (%s)", type(error).__name__)


# --- a launch a turn waits on --------------------------------------------------------------------


async def launch_in_a_turn(
    deps: LaunchDeps,
    tool: AskingTool,
    *,
    arguments: JsonObject,
    for_person: Person | None,
    yes: LaunchStop | None = None,
    judged_as_domo: bool = False,
    asked_by: Actor | None = None,
) -> Launched:
    """Launch ``tool`` for the turn of ``for_person`` (``None``: for the house, in a turn Domo
    acts in on his own, or a yes the owner gave to a stop of the house's; ``judged_as_domo``
    when that turn judged it as his: ``launch.launch``) (with the yes of the stop ``yes`` its
    caller took, when it carries one) (its program's requests answered while it
    runs, and the launches they make waited for with it), and say what came of it. It runs in a
    task of ``deps.wakes``, waited for up to ``HAND_OVER_SECONDS``; past that, or once the turn is
    stopped, the turn is told ``running``, a row says the run was handed over
    (``made_tool_runs``), and the task goes on; at its end the row holds what came of it, and
    Domo is woken for the person with it (``deliver_run``; the owner, for the house). Without
    ``deps.wakes``, the turn waits whatever it takes. A program Domo runs by hand
    (``AskingTool.of_run``) goes the same way, with its own code and arguments
    (``launch.launch_by_hand``: ``arguments`` is not read), told under ``BY_HAND``.
    ``asked_by``: who asked it in the turn, as the barrier saw them: how sure the code was of
    them travels with the launch (``ForWhom.standing``), for the launches made from inside it."""
    chain = Chain(turn_waits=True)
    whom: WakeFor = None if for_person is None else for_person.id
    wakes = deps.wakes
    if wakes is None:
        launched = await _launched(
            deps, chain, tool, arguments, _for(for_person, asked_by), yes, judged_as_domo
        )
        await wake_chain(deps, chain)
        return launched
    handed_over = False
    came: list[Launched] = []
    written = asyncio.Event()
    rows: list[uuid.UUID | None] = []

    async def run() -> None:
        launched = await _launched(
            deps, chain, tool, arguments, _for(for_person, asked_by), yes, judged_as_domo
        )
        # Nothing awaits between this look and its outcome: the turn either reads what came, or
        # was told ``running`` before.
        if not handed_over:
            came.append(launched)
            await wake_chain(deps, chain)
            return
        await written.wait()
        await _finish(deps, rows[0], tool, launched, chain, whom)

    async def write_the_row() -> None:
        try:
            rows.append(await _handed_over(deps, tool, whom))
        finally:
            if not rows:
                rows.append(None)
            written.set()

    def hand_over() -> None:
        nonlocal handed_over
        handed_over = True
        # What it launches from now on goes after the launches turns wait on.
        chain.turn_waits = False
        wakes.spawn(write_the_row())

    task = wakes.spawn(run())
    try:
        await asyncio.wait({task}, timeout=HAND_OVER_SECONDS)
    except asyncio.CancelledError:
        # The person stopped the turn: they asked for the thing, not for the turn.
        if not came:
            hand_over()
        else:
            # It ended as the turn stopped: a stop the turn will not tell wakes Domo, once.
            stop = stop_of(came[0].result)
            if stop is not None:
                wakes.spawn(wake_for(deps, whom, tool, {"needs_yes": stop}))
        raise
    if came:
        return came[0]
    hand_over()
    return Launched("running", None)


def _for(person: Person | None, asked_by: Actor | None) -> ForWhom:
    """Whom a launch of a turn is for: ``person`` (``None``: the house), with how sure the code
    was of who asked it (``asked_by``: that person, someone else, or an owner's yes to a stop of
    the house's): never sure by default. Nobody asking (``None``) is the house's or his own."""
    if asked_by is None:
        return ForWhom(person)
    return ForWhom.asked_by(person, asked_by)


async def _launched(
    deps: LaunchDeps,
    chain: Chain,
    tool: AskingTool,
    arguments: JsonObject,
    for_whom: ForWhom,
    yes: LaunchStop | None,
    judged_as_domo: bool,
) -> Launched:
    try:
        if tool.by_hand:
            # A program run by hand: its own code and arguments, kept with it.
            return await launch_by_hand(deps, tool.id, for_whom=for_whom, chain=chain, yes=yes)
        return await launch(
            deps,
            tool.id,
            arguments=arguments,
            for_whom=for_whom,
            cause="turn",
            chain=chain,
            yes=yes,
            judged_as_domo=judged_as_domo,
        )
    except Exception as error:
        logger.error("A launch of a tool failed (%s)", type(error).__name__)
        return Launched("failed", {"reason": "the launch could not be kept"})


async def _handed_over(deps: LaunchDeps, tool: AskingTool, whom: WakeFor) -> uuid.UUID | None:
    """The row of a run a turn stopped waiting for, ``running``, for the person ``whom`` (``None``:
    the house's, ``for_house``): its id (``None``: it could not be written, logged)."""
    try:
        async with deps.session_factory() as session:
            run = tool.row(
                person_id=whom,
                for_house=whom is None,
                status="running",
                handed_over_at=deps.clock(),
            )
            session.add(run)
            await session.commit()
            _RUNNING.add(run.id)
            return run.id
    except Exception as error:
        logger.error("A run handed over could not be noted (%s)", type(error).__name__)
        return None


async def _finish(
    deps: LaunchDeps,
    run_id: uuid.UUID | None,
    tool: AskingTool,
    launched: Launched,
    chain: Chain,
    whom: WakeFor,
) -> None:
    """The end of a run handed over: the others are woken, its row holds what came of it, and
    Domo is woken for the person ``whom`` with it (``deliver_run``; ``None``: the owner, for the
    house). Without a row, or when its row
    could not be finished, he is woken at once (``_told_or_kept``); a row that could not be
    finished stays among the runs whose chain runs here, so that it is never told, in this
    process, as a run that did not finish."""
    status = launched.status if launched.status in ("done", "set_aside") else "failed"
    said = chain.wakes.pop(whom, {})
    await wake_chain(deps, chain)
    also: list[JsonValue] = [
        {"tool": name, "content": content}
        for name, contents in said.items()
        for content in contents
    ]
    told = _wakes_of(tool.name, status, launched.result, also)
    if run_id is None:
        await _told_or_kept(deps, whom, tool, told)
        return
    try:
        async with deps.session_factory() as session:
            await session.execute(
                update(MadeToolRun)
                .where(MadeToolRun.id == run_id, MadeToolRun.finished_at.is_(None))
                .values(
                    status=status,
                    result=launched.result,
                    also=also,
                    finished_at=deps.clock(),
                )
                .execution_options(synchronize_session=False)
            )
            await session.commit()
    except Exception as error:
        logger.error("What a run handed over gave could not be kept (%s)", type(error).__name__)
        await _told_or_kept(deps, whom, tool, told)
        return
    _RUNNING.discard(run_id)
    await deliver_run(deps, run_id)


def _wakes_of(
    name: str,
    status: str,
    result: object,
    also: Sequence[object],
    *,
    run: bool = True,
) -> list[tuple[str, list[JsonValue]]]:
    """What a row wakes Domo with, by tool: for a ``run``, how it ended and its result (a run
    that stopped for a yes, as the stop itself, ``needs_yes``, unless its chain told it already);
    then, for any row, what the tools asked to tell that person (``also``)."""
    by_tool: dict[str, list[JsonValue]] = {}
    # What the chain told already of this tool.
    contents = [
        cast(JsonObject, item).get("content")
        for item in also
        if isinstance(item, dict) and cast(JsonObject, item).get("tool") == name
    ]
    if run:
        stop = stop_of(result)
        told: JsonObject = (
            {"status": status, "result": cast(JsonValue, result)}
            if stop is None
            else {"needs_yes": stop}
        )
        if stop is None or told not in contents:
            by_tool[name] = [told]
    for item in also:
        if isinstance(item, dict):
            said = cast(JsonObject, item)
            by_tool.setdefault(str(said.get("tool")), []).append(said.get("content"))
    return list(by_tool.items())


def stop_of(result: object) -> JsonObject | None:
    """What a launch that stopped for a yes was to do (``{"stop", "says", "amount_cents", "why",
    "by"}``), from its result; ``None`` for any other result."""
    if not isinstance(result, dict):
        return None
    given = cast(JsonObject, result)
    if given.get("reason") != "needs_yes":
        return None
    return {key: value for key, value in given.items() if key != "reason"}


# --- delivering what waits -----------------------------------------------------------------------


def retold_after(tries: int) -> timedelta:
    """How long a row claimed and not settled waits before it is taken again: ``CLAIM_AGAIN_AFTER``
    (a restart), and after ``tries`` turns that could not run, twice as long at each try after the
    first, ``RETOLD_MOST`` at most."""
    if tries <= 1:
        return CLAIM_AGAIN_AFTER
    return min(CLAIM_AGAIN_AFTER * 2 ** min(tries - 1, 10), RETOLD_MOST)


def _may_claim(run: MadeToolRun, now: datetime) -> bool:
    """Whether the row ``run`` may be claimed at ``now``: ended, not settled, and never claimed
    or claimed ``retold_after`` its tries ago."""
    if run.finished_at is None or run.delivered_at is not None:
        return False
    return run.claimed_at is None or run.claimed_at + retold_after(run.tries) <= now


async def deliver_run(deps: LaunchDeps, run_id: uuid.UUID) -> None:
    """Wake Domo for the person of the row ``run_id``, which ended, with what it holds:
    claimed first (``claimed_at``: by the task that ran it, or by the upkeep when nobody settled
    it within ``retold_after`` its tries), then settled (``delivered_at``) once the turn ran. A
    turn that could not run (no key, every key at its limit, the model failed before it acted)
    leaves it claimed, one more try counted: the upkeep takes it again later and later, and once
    it ended ``GIVEN_UP_AFTER`` ago it is given up, audited (``WAKE_DROPPED``: the tools' names and
    the tries) and logged. It only wakes: nothing is carried out again. A tool no longer ready
    still has its result told. One row is delivered once at a time in this process. Never raises
    but for its cancellation, which is logged (the row stays claimed: the upkeep takes it
    again)."""
    if run_id in _DELIVERING:
        return
    _DELIVERING.add(run_id)
    await _delivered(deps, run_id)


async def _delivered(deps: LaunchDeps, run_id: uuid.UUID) -> None:
    """``deliver_run`` for a row its caller put among those being delivered, which it leaves."""
    name = "?"
    try:
        now = deps.clock()
        async with deps.session_factory() as session:
            run = await session.get(
                MadeToolRun, run_id, with_for_update=True, populate_existing=True
            )
            if run is None or not _may_claim(run, now):
                return
            run.claimed_at = now
            person_id, for_house, kind = run.person_id, run.for_house, run.kind
            hand_run = run.hand_run_id
            told = (run.status, run.result, list(run.also))
            # Of a tool, or of a program run by hand (told under the tool Domo ran it with).
            tool = None if run.tool_id is None else await session.get(MadeTool, run.tool_id)
            known = tool.name if tool is not None else BY_HAND if run.hand_run_id else None
            await session.commit()
        if known is not None:
            name = known
        settled = True
        if known is not None and (person_id is not None or for_house):
            # A row of the house's is told to the owner of now.
            status, result, also = told
            wakes = _wakes_of(known, status, result, also, run=kind == "run")
            settled = await _tell(deps, person_id, wakes, hand_run=hand_run)
        async with deps.session_factory() as session:
            run = await session.get(
                MadeToolRun, run_id, with_for_update=True, populate_existing=True
            )
            if run is None:
                return
            now = deps.clock()
            if not settled:
                run.tries += 1
            if settled or (run.finished_at is not None and run.finished_at + GIVEN_UP_AFTER <= now):
                run.delivered_at = now
            if not settled and run.delivered_at is not None:
                said = _wakes_of(name, "", None, run.also, run=kind == "run")
                tools: list[JsonValue] = [*sorted({tool_name for tool_name, _ in said})]
                await append_audit(
                    session,
                    actor="system",
                    action=WAKE_DROPPED,
                    details={"tools": tools, "tries": run.tries},
                )
                logger.warning("A wake of the tool %s was given up after a day of tries", name)
            await session.commit()
    except asyncio.CancelledError:
        logger.warning("The delivery of a run of the tool %s was stopped", name)
        raise
    except Exception as error:
        logger.error("A tool's run could not be delivered (%s)", type(error).__name__)
    finally:
        _DELIVERING.discard(run_id)


async def deliver_runs(deps: LaunchDeps) -> None:
    """The upkeep's delivery of what waits in ``made_tool_runs``: the runs handed over that never
    finished long after their tool's time (``UNFINISHED_AFTER``, none whose chain still runs here)
    end as failed (``UNFINISHED``), then the rows that ended and are not settled (never claimed,
    or claimed ``retold_after`` their tries ago, none being delivered here) are delivered, the
    oldest first, ``RUNS_PER_PASS`` at most, each in a task of ``deps.wakes`` (``_start``). Never
    raises but for its cancellation."""
    now = deps.clock()
    try:
        async with deps.session_factory() as session:
            running = await session.execute(
                select(MadeToolRun.id, MadeToolRun.handed_over_at, MadeTool, HandRun.max_seconds)
                .outerjoin(MadeTool, MadeTool.id == MadeToolRun.tool_id)
                .outerjoin(HandRun, HandRun.id == MadeToolRun.hand_run_id)
                .where(MadeToolRun.finished_at.is_(None))
                .order_by(MadeToolRun.handed_over_at)
                .limit(RUNS_PER_PASS)
            )
            # The time a launch of its tool (its own ``run_seconds``, else the house's), or of its
            # program run by hand, may take.
            house = await value(session, "run_seconds")
            lost: list[uuid.UUID] = []
            for run_id, handed_over_at, joined, by_hand in running.all():
                # Outer joined: no tool for a program run by hand.
                tool = cast(MadeTool | None, joined)
                allowed = by_hand
                if tool is not None:
                    allowed = own_values(tool).get("run_seconds", house)
                # Past what a date holds, never lost: the others are delivered all the same.
                waited = span(allowed or 0)
                if run_id not in _RUNNING and handed_over_at + waited + UNFINISHED_AFTER < now:
                    lost.append(run_id)
            if lost:
                await session.execute(
                    update(MadeToolRun)
                    .where(MadeToolRun.id.in_(lost), MadeToolRun.finished_at.is_(None))
                    .values(status="failed", result={"reason": UNFINISHED}, finished_at=now)
                    .execution_options(synchronize_session=False)
                )
            # ``retold_after``, in the database's words.
            seconds = func.least(
                CLAIM_AGAIN_AFTER.total_seconds()
                * func.power(2, func.greatest(MadeToolRun.tries - 1, 0)),
                RETOLD_MOST.total_seconds(),
            )
            wait = func.make_interval(0, 0, 0, 0, 0, 0, seconds)
            found = await session.scalars(
                select(MadeToolRun.id)
                .where(
                    MadeToolRun.finished_at.is_not(None),
                    MadeToolRun.delivered_at.is_(None),
                    or_(MadeToolRun.claimed_at.is_(None), MadeToolRun.claimed_at + wait <= now),
                    MadeToolRun.id.not_in(list(_DELIVERING)) if _DELIVERING else true(),
                )
                .order_by(MadeToolRun.handed_over_at, MadeToolRun.id)
                .limit(RUNS_PER_PASS)
            )
            ids = list(found.all())
            await session.commit()
    except Exception as error:
        logger.error("The runs to deliver could not be read (%s)", type(error).__name__)
        return
    for run_id in ids:
        if run_id in _DELIVERING:
            continue
        _DELIVERING.add(run_id)
        await _start(deps, _delivered(deps, run_id))
