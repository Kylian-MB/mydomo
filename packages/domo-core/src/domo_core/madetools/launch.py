"""Launching a tool Domo made (spec 5.2, 5.4, 5.7, 5.8, 6): its code runs in the sandbox, never
here.

A tool is Domo's; each launch of it is made for someone (``ForWhom``): a person, or the house
(``HOUSE``: Domo acting on his own for the whole household). ``launch`` reads the tool and the
version in force, judges the launch (below), checks the arguments against the tool's schema
(``madetools.schema.schema_problem``: a mistake of the model, counted against nobody; a schema the
core cannot check, ``unsupported``, is the tool's fault and counts as one of its failures, nothing
run), counts it on the household's day (``service.count_launch``: past its ``launches_per_day``
it is set aside), and gives the code its entry: ``{"arguments", "now",
"for"}`` and nothing else (the time in the household's zone, and the display name of whom it runs
for, or ``null`` for the house: never an id, a role or a secret).

Who judges a launch:

* a launch for a person who is no longer in the household is refused, whatever its cause;
* ``cause="turn"``: the turn already put the call through the barrier
  (``conversation.execution.handle_tool_call`` judges ``made.<name>`` for the person, with the
  owner's rights of now): it is not judged twice. A launch for the house from a turn is judged
  here all the same, unless that turn judged it as Domo (``judged_as_domo``: a turn he acts in
  on his own, Task 9); a yes the owner gave to a stop of the house's was judged as the owner's;
* any other launch: the barrier (``policy.decision.decide_now``) on ``made.<name>``, with the
  person's actor of now, or Domo's own (``policy.roles.domo_actor``) for the house, and the
  catalog read now (``load_policy``), audited (``policy.decision``). Nobody can say yes outside
  a turn: what needs someone's agreement, or a yes an owner's rule asks, is refused.

While it runs, the program asks the core what it needs (the module ``domo``): each request is
answered at once by ``madetools.means.Means``, judged for whom the launch is made, and nothing of a
launch is carried out after it ends. A tool launched by a program (``within``: the launch that asks
it) runs in the same chain, for the same person, one level deeper, and takes a place of the sandbox
only if one is free at once (``SandboxBusy``: refused ``busy`` with the ``places`` setting, its
count of the day given back): never queued. The wakes the programs of a chain ask are given once the
first launch of the chain ends (``madetools.handover.wake_chain``), unless its caller gathers them
itself (``chain``).

The bounds of a launch are settings of Domo's (``madetools.settings``), read once as it is
judged and counted (the tool's own values, else the house's, else the starts; a program run by
hand has the house's, and its own time): how long it runs (``run_seconds``), what its result
weighs, how many requests it makes, how many launches a day and how many failures in a row its
tool has. What stops on one of them says the setting and its figure (``settings.hit``).

What the code gives back is its result itself, any JSON value of ``result_bytes`` at most
(``result_of``; a dry run of a build reads it the same way, ``madetools.checks``). A run that ended
in an error, a timeout or an output too big, or that asked more than its ``requests_per_launch``
(the core stopped it), is a failure (a timeout says ``run_seconds``, an output too big
``output_bytes``, with its figure); ``failures_in_a_row`` in a row set the tool aside, with the
reason kept on its row (``aside_reason``); a run the sandbox could not end (``killed``) sets it
aside at once. A tool set aside by a launch no turn made wakes Domo on his own with it
(``madetools.troubles``). The sandbox is given the launch's ``run_seconds``, ``memory_mb`` and
``output_bytes``, with no ceiling of the core's; what its machine cannot give
(``SandboxNotHere``) runs nothing: the launch fails with the setting and what the machine has
(``not_here``), not counted against the tool, its count of the day given back. A run the core
stopped otherwise is ``stopped``, no failure of the tool: one that stopped for a yes says so, with
what was to be done (``{"reason": "needs_yes", "stop", "says", "amount_cents", "why", "by"}``;
Domo is woken with it unless a turn waits on the launch, ``madetools.means``). A launch with a
yes (``yes``: the stop it answers, taken by its caller, ``madetools.yes.take_yes``) lets that
operation through, up to its amount, once. A sandbox that does not answer (``SandboxUnavailable``)
is no failure of the tool: the launch is ``failed`` with a reason Domo reads (``SANDBOX_DOWN``),
and he does the request another way; the launch gives its count of the day back. One lost once its
program had asked something says that part of it may be done (``SANDBOX_CUT``), and is never fired
again by its trigger.

A launch a turn waits on goes before the others (``waited_by_a_turn``: a turn's own launch, and
those its chain asks while the turn still waits, ``Chain.turn_waits``). How long a turn waits, and
what goes on once it stops waiting, is ``madetools.handover.launch_in_a_turn``'s.

A program is a tool's version, or one Domo runs by hand to answer at once what no tool does yet
(``launch_by_hand``, spec 5.4: a ``HandRun``, from ``tools.program_run``): it goes through the same
path, its requests answered by the same ``Means`` with the same bounds, stops and yes; it is
counted on no tool, and its audit entry is ``program.ran`` (``PROGRAM_RAN``: the status, the
seconds and how many requests; never its code, its arguments nor its result).

The audit log keeps one entry per launch (``madetool.launched``: the tool's name, the cause, the
seconds, the status, and how many requests its program made and how many of them were reads; its
actor the person it ran for, or ``domo`` for the house) and one when a tool is set aside
(``madetool.set_aside``: its name and why), never the arguments, a request or the output. Beside
the entry of a launch of a tool, its trace is kept for Domo (``madetools.launches.record``): its
figures, and the error its program said, which never goes into the audit.
"""

import asyncio
import json
import logging
import math
import uuid
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol, cast
from zoneinfo import ZoneInfo

from domo_common.gatekeeper_api import OutRequest, OutResponse
from domo_common.sandbox_api import ANSWER_CEILING_BYTES, JsonObject, RunRequest, RunResult
from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.accounts.sessions import is_active
from domo_core.audit.log import append_audit
from domo_core.db import SessionFactory
from domo_core.madetools.launches import record
from domo_core.madetools.models import (
    OFFERED_PREFIX,
    HandRun,
    LaunchEnd,
    LaunchStop,
    MadeTool,
    MadeToolVersion,
)
from domo_core.madetools.risks import Operation, operations_of
from domo_core.madetools.schema import schema_problem, unsupported
from domo_core.madetools.service import count_launch, set_aside
from domo_core.madetools.settings import all_values, hit, told
from domo_core.policy.actions import DescribedAction, FactValue
from domo_core.policy.barrier import Allow, Refuse, record_decision
from domo_core.policy.catalog import MADE_FACTS_WITHOUT_A_YES, MADE_PREFIX, PolicyTables
from domo_core.policy.decision import decide_now
from domo_core.policy.open_catalog import load_policy
from domo_core.policy.roles import Actor, actor_label, as_role, domo_actor
from domo_core.policy.settings import load_policy_settings
from domo_core.prompts import ToolWake
from domo_core.sandbox_client import Sandbox, SandboxBusy, SandboxNotHere, SandboxUnavailable
from domo_core.toolsdb.store import ToolsBase

if TYPE_CHECKING:
    # ``means`` launches tools in turn: it is imported where a launch runs.
    from domo_core.madetools.means import Chain, LaunchScope, Means

logger = logging.getLogger(__name__)

# The audit entries of a launch, of a tool set aside, and of a program run by hand.
LAUNCHED: Final = "madetool.launched"
SET_ASIDE: Final = "madetool.set_aside"
PROGRAM_RAN: Final = "program.ran"
# What a launch of a program run by hand that is no longer kept says.
NO_SUCH_RUN: Final = "this program is no longer kept: write it and run it again"

# What Domo reads when the sandbox does not answer: nothing ran, and it is not the tool's fault.
SANDBOX_DOWN: Final = (
    "the place where your tools run does not answer: nothing ran; do the request another way"
)
# What Domo reads when the sandbox stopped answering once the program had asked something: part
# of its work may be done, so it is never done again blindly (a trigger is not fired again).
SANDBOX_CUT: Final = (
    "the place where your tools run stopped answering while it ran: it may have done part of its "
    "work; check what it did before doing it again"
)
# What a tool launched by a program is refused with when no place of the sandbox is free at once.
BUSY: Final = "busy"
_FAILURES: Final[Mapping[str, str]] = {
    "error": "it stopped on an error",
    "timeout": "it took longer than its time",
    "too_big": "its output was too big",
    "killed": "it could not be stopped",
    # A signal the sandbox did not send (ruling R32): a failure like any, never "could not be
    # stopped".
    "ended_by_machine": (
        "the machine ended it, not the sandbox (a signal): memory is the likely cause, its own or "
        "that of the runs beside it"
    ),
}
# The setting a failure of the sandbox comes from: the time and the output it was given.
_STOPPED_BY: Final[Mapping[str, str]] = {"timeout": "run_seconds", "too_big": "output_bytes"}

type Cause = Literal["turn", "trigger", "tool"]
type LaunchStatus = Literal["done", "failed", "refused", "running", "set_aside", "stopped"]


class NotAResult(ValueError):
    """What a program gave back is not a result: its message says why, never a value of it;
    ``setting`` is the bound it went past, when it did (``result_bytes`` and its figure)."""

    def __init__(self, why: str, setting: tuple[str, int] | None = None) -> None:
        super().__init__(why)
        self.setting = setting


@dataclass(frozen=True)
class ForWhom:
    """Whom a launch is made for: a person (their rights of now, their name in the entry), or,
    with no person, the house (Domo's own rights, no name)."""

    person: Person | None


# A launch for the whole household: Domo acts on his own.
HOUSE: Final = ForWhom(None)


class Spawner(Protocol):
    """Where a run handed over goes on (``conversation.starts.Wakes``): a task kept until it
    ends."""

    def spawn(self, coroutine: Coroutine[Any, Any, None]) -> "asyncio.Task[None]": ...


class Waker(Protocol):
    """What wakes Domo in someone's conversation with what tools say
    (``conversation.tool_wakes.tool_wake_turn``): it runs the turn, and says whether it ran (the
    person was told, or Domo chose to say nothing; someone who left is dropped, which is settled
    too). ``False``: the turn could not run, try again. ``on_his_own``: the wake is the house's
    (``person_id`` is the owner, woken for it), and Domo acts there on his own, with his own
    rights."""

    def __call__(
        self, person_id: uuid.UUID, wakes: Sequence[ToolWake], /, *, on_his_own: bool
    ) -> Awaitable[bool]: ...


class WayOut(Protocol):
    """What sends a program's request out (``gatekeeper_client.GatekeeperClient.out``): its
    answer, or ``gatekeeper_client.OutRefused`` with the relay's code."""

    def out(self, request: OutRequest, /) -> Awaitable[OutResponse]: ...


@dataclass(frozen=True)
class LaunchDeps:
    """What a launch needs: the database, the sandbox, the clock, the catalog of the code the open
    kinds are laid over (``policy_base``, read again for a launch outside a turn), where a run
    a turn handed over and the turns that wake Domo go on (``wakes``: ``None``, the turn waits
    for the run whatever it takes, and a wake is awaited where it is asked), what wakes Domo
    when a tool asks it (``waker``: ``None``, nobody is woken;
    ``conversation.prepare.made_tool_deps`` gives the application's), the tools' database the
    programs read and write (``tools_base``: ``None``, there is none, and each such request is
    refused) and the gatekeeper their requests out go through (``gatekeeper``: ``None``, nothing
    goes out, each such request is refused ``unreachable``)."""

    session_factory: SessionFactory
    sandbox: Sandbox
    clock: Callable[[], datetime]
    policy_base: PolicyTables
    wakes: Spawner | None = None
    waker: Waker | None = None
    tools_base: ToolsBase | None = None
    gatekeeper: WayOut | None = None


@dataclass(frozen=True)
class Launched:
    """What came of a launch: ``done`` (``result`` is the tool's), ``failed`` (``result`` says
    why), ``refused`` (by the barrier, for someone who left, or ``busy``), ``running`` (handed over
    to the background), ``set_aside`` (the tool is set aside, by this launch or before it) or
    ``stopped`` (the core stopped its run)."""

    status: LaunchStatus
    result: JsonValue | None


@dataclass(frozen=True)
class _Ready:
    """A launch judged and counted, ready to run: whom for (``person``, ``None`` the house) and
    with whose rights (``who``; ``actor`` as the audit names it). The program is a tool's version
    (``tool_id``, ``name``), or one Domo runs by hand (``hand_run_id``: no tool, no name)."""

    tool_id: uuid.UUID | None
    name: str | None
    code: str
    # The version of the tool that runs (``None``: a program run by hand).
    version_id: uuid.UUID | None
    max_seconds: int
    # The bounds of this launch (``madetools.settings``), read as it was judged and counted.
    settings: Mapping[str, int]
    entry: JsonObject
    who: Actor
    actor: str
    person: Person | None
    cause: Cause
    counted_on: date | None
    operations: Mapping[str, Operation]
    arguments: JsonObject
    hand_run_id: uuid.UUID | None = None


def _entry(
    arguments: JsonObject, now: datetime, zone: ZoneInfo | None, person: Person | None
) -> JsonObject:
    """What a program is given: its arguments, the time in the household's zone and the name of
    whom it runs for (``None``: the house); never an id, a role or a secret."""
    return {
        "arguments": arguments,
        "now": now.astimezone(UTC if zone is None else zone).isoformat(),
        "for": None if person is None else person.display_name,
    }


def action_type_of(name: str) -> str:
    """The action type of the tool ``name`` (``made.<name>``)."""
    return f"{MADE_PREFIX}{name}"


def described(name: str, facts: Mapping[str, FactValue] | None = None) -> DescribedAction:
    """What a launch of the tool ``name`` does, for the barrier: it reads and computes in the
    household, and acts on nothing; its facts (``catalog.MADE_FACTS``) say the yes it carries, when
    it carries one (``facts``), else that it carries none."""
    return DescribedAction(
        connector="core",
        action_type=action_type_of(name),
        target=None,
        effect="read",
        destination="household",
        facts=dict(MADE_FACTS_WITHOUT_A_YES if facts is None else facts),
    )


# --- the launch ----------------------------------------------------------------------------------


async def launch(
    deps: LaunchDeps,
    tool_id: uuid.UUID,
    *,
    arguments: JsonObject,
    for_whom: ForWhom,
    cause: Cause,
    waited: bool | None = None,
    chain: "Chain | None" = None,
    within: "LaunchScope | None" = None,
    yes: LaunchStop | None = None,
    judged_as_domo: bool = False,
) -> Launched:
    """Launch the tool ``tool_id`` with ``arguments``, for ``for_whom`` (a person, or ``HOUSE``),
    and say what came of it (see the module docstring). ``waited`` says whether a turn waits on
    this run (``None``: when the cause is a turn).

    The first launch of a chain gathers the wakes its programs ask in ``chain`` when its caller
    gives one (and tells them itself); without one, they are given once it ends. A launch asked by
    a running program (``within``: that program's launch) goes in its chain, one level deeper, and
    may not wait for a place; how deep a chain goes and how many launches it makes is bounded by
    what answers the program (``madetools.means``). ``yes`` is the stop whose yes the caller took
    (``madetools.yes.take_yes``): this launch lets its operation through, up to its amount, once.
    ``judged_as_domo``: a turn put this launch for the house through the barrier as Domo's
    already (it is not judged twice).

    It raises only what the database raises: a launch that fails otherwise is a ``Launched``."""
    ready = await _judged_and_counted(
        deps, tool_id, arguments, for_whom, cause, judged_as_domo=judged_as_domo
    )
    if isinstance(ready, Launched):
        return ready
    return await _launched(deps, ready, waited=waited, chain=chain, within=within, yes=yes)


async def launch_by_hand(
    deps: LaunchDeps,
    run_id: uuid.UUID,
    *,
    for_whom: ForWhom,
    waited: bool | None = None,
    chain: "Chain | None" = None,
    yes: LaunchStop | None = None,
) -> Launched:
    """Launch the program Domo runs by hand ``run_id`` (``HandRun``: its code, operations and
    arguments, for whom it was opened), in a turn, for ``for_whom``, through the very path of a
    tool's launch (``launch``): the same answers to its requests, judged with the rights of whom
    it runs for (``madetools.means``), the same bounds, the same stops and yes (``yes``: the stop
    whose yes the caller took). Nothing judges the launch itself: the turn judged ``program.run``
    as Domo's act. It runs only for whom it was opened (else ``refused``), and never for
    someone who left; a run that is no longer kept is ``failed``. It is no tool's: nothing is
    counted on a tool, and its audit entry is ``PROGRAM_RAN`` (the status, the seconds and how
    many requests; never its code, its arguments nor its result). It raises only what the
    database raises."""
    ready = await _hand_run_ready(deps, run_id, for_whom)
    if isinstance(ready, Launched):
        return ready
    return await _launched(deps, ready, waited=waited, chain=chain, within=None, yes=yes)


async def _launched(
    deps: LaunchDeps,
    ready: _Ready,
    *,
    waited: bool | None,
    chain: "Chain | None",
    within: "LaunchScope | None",
    yes: LaunchStop | None,
) -> Launched:
    """Run ``ready`` in its chain (see ``launch``), and give the chain's wakes once it ends when
    nobody else gathers them."""
    # Imported here: what answers a program launches tools in turn.
    from domo_core.madetools.means import Chain, LaunchScope, YesGiven

    if within is not None:
        gathers, depth, path, waits = (
            within.chain,
            within.depth + 1,
            within.path,
            within.chain.turn_waits,
        )
    else:
        waits = ready.cause == "turn" if waited is None else waited
        gathers, depth, path = chain or Chain(turn_waits=waits), 0, ()
    scope = LaunchScope(
        tool_name=ready.name,
        tool_id=ready.tool_id,
        for_whom=ForWhom(ready.person),
        actor=ready.who,
        depth=depth,
        chain=gathers,
        waited=waits,
        # A program run by hand is in no tool's path: the tools it launches start one.
        path=path if ready.name is None else (*path, ready.name),
        operations=ready.operations,
        arguments=ready.arguments,
        yes=None if yes is None else YesGiven.of(yes),
        hand_run_id=ready.hand_run_id,
        settings=ready.settings,
    )
    launched = await _run(deps, ready, scope, nested=within is not None)
    if within is None and chain is None:
        from domo_core.madetools.handover import wake_chain

        await wake_chain(deps, gathers)
    return launched


async def _person_now(session: AsyncSession, person: Person, now: datetime) -> Person | None:
    """``person`` as the database holds them now, when they are still in the household."""
    found = await session.execute(select(Person).where(Person.id == person.id))
    current: Person | None = found.scalar_one_or_none()
    if current is None or not is_active(current, now):
        return None
    return current


async def _judged(
    session: AsyncSession, deps: LaunchDeps, who: Actor, name: str, now: datetime
) -> Launched | None:
    """The barrier's word on launching the tool ``name`` for ``who``, audited: ``None`` when it
    may go, else the launch refused (committed). Nobody can say yes outside a turn: what needs it
    (someone's agreement, or a yes an owner's rule asks before the tool) is refused."""
    action = described(name)
    policy = await load_policy(session, deps.policy_base)
    decision = await decide_now(session, who, action, policy, now)
    await record_decision(session, who, action, decision, tool_name=f"{OFFERED_PREFIX}{name}")
    if isinstance(decision, Allow) and decision.rule is None:
        return None
    await session.commit()
    reason = decision.reason if isinstance(decision, Refuse) else "needs_confirmation"
    return Launched("refused", {"reason": reason})


async def _judged_and_counted(
    deps: LaunchDeps,
    tool_id: uuid.UUID,
    arguments: JsonObject,
    for_whom: ForWhom,
    cause: Cause,
    *,
    judged_as_domo: bool = False,
) -> "_Ready | Launched":
    """The launch, judged and counted, ready to run, in one transaction; or what came of it when
    it may not run."""
    now = deps.clock()
    async with deps.session_factory() as session:
        # Locked first: the tool's row before the audit's lock, as at the end of a run, so that
        # two launches of the same tool never wait on each other.
        tool = await session.get(MadeTool, tool_id, with_for_update=True)
        if tool is None or tool.state == "removed":
            return Launched("failed", {"reason": "there is no such tool"})
        if tool.state == "set_aside":
            return Launched("set_aside", {"reason": tool.aside_reason})
        version = (
            None
            if tool.current_version_id is None
            else await session.get(MadeToolVersion, tool.current_version_id)
        )
        if version is None or version.state != "ready":
            return Launched("failed", {"reason": "the tool has no version ready"})
        name = tool.name
        person: Person | None = None
        if for_whom.person is not None:
            person = await _person_now(session, for_whom.person, now)
            if person is None:
                # Someone who left: nothing is launched for them any more.
                return Launched("refused", {"reason": "not_allowed"})
        who = domo_actor() if person is None else Actor(person.id, as_role(person.role))
        actor = actor_label(who)
        if cause != "turn" or (person is None and not judged_as_domo):
            # A turn judged its call already (for the house, as Domo's); any other launch is
            # judged now.
            refused = await _judged(session, deps, who, name, now)
            if refused is not None:
                return refused
        values = await all_values(session, tool)
        flagged = unsupported(tool.parameters)
        if flagged:
            # Parameters the core cannot check (never stored once the build checks them): the
            # tool's fault, not the model's call. Nothing runs, and it counts against the tool.
            why = "its parameters use what the core does not check: " + ", ".join(flagged[:5])
            launched = await _counted_failure(
                session, tool, now, why, actor, killed=False, most=values["failures_in_a_row"]
            )
            await _audit_launch(session, actor, name, cause, 0.0, launched.status)
            await _traced(
                session,
                tool_id=tool.id,
                version_id=version.id,
                at=now,
                cause=cause,
                for_house=person is None,
                by_turn=cause == "turn",
                status=_ended(launched.status),
                seconds=0.0,
                requests=0,
                reads=0,
                answer_bytes=0,
                error=None,
            )
            await session.commit()
            if launched.status == "set_aside":
                await _in_trouble(deps, cause, tool_id, name, launched.result)
            return launched
        problem = schema_problem(tool.parameters, arguments)
        if problem is not None:
            # A mistake of the model's form: nothing is counted.
            return Launched("failed", {"reason": "invalid", "detail": problem})
        zone = (await load_policy_settings(session)).time_zone
        most = values["launches_per_day"]
        if not await count_launch(session, tool, now, zone, most):
            why = f"it was launched more than {most} times today; {told('launches_per_day', most)}"
            await set_aside_audited(session, tool, now, why, actor)
            await session.commit()
            aside = Launched("set_aside", {"reason": why, **_hit("launches_per_day", most)})
            await _in_trouble(deps, cause, tool_id, name, aside.result)
            return aside
        tool.last_launched_at = now
        ready = _Ready(
            tool_id=tool.id,
            name=name,
            code=version.code,
            version_id=version.id,
            max_seconds=values["run_seconds"],
            settings=values,
            entry=_entry(arguments, now, zone, person),
            who=who,
            actor=actor,
            person=person,
            cause=cause,
            counted_on=tool.launches_day,
            operations=operations_of(tool.operations),
            arguments=arguments,
        )
        await session.commit()
    return ready


async def _hand_run_ready(
    deps: LaunchDeps, run_id: uuid.UUID, for_whom: ForWhom
) -> "_Ready | Launched":
    """The program run by hand ``run_id``, ready to run for ``for_whom``; or what came of it when
    it may not run (see ``launch_by_hand``)."""
    now = deps.clock()
    async with deps.session_factory() as session:
        run = await session.get(HandRun, run_id)
        if run is None:
            return Launched("failed", {"reason": NO_SUCH_RUN})
        person: Person | None = None
        if for_whom.person is not None:
            person = await _person_now(session, for_whom.person, now)
            if person is None:
                # Someone who left: nothing is launched for them any more.
                return Launched("refused", {"reason": "not_allowed"})
        if run.for_person_id != (None if person is None else person.id):
            # Only for whom it was opened.
            return Launched("refused", {"reason": "not_allowed"})
        who = domo_actor() if person is None else Actor(person.id, as_role(person.role))
        zone = (await load_policy_settings(session)).time_zone
        arguments = cast(JsonObject, dict(run.arguments))
        # The house's bounds, but its time: its own (``program_run``'s ``max_seconds``).
        values = {**await all_values(session), "run_seconds": run.max_seconds}
        return _Ready(
            tool_id=None,
            name=None,
            code=run.code,
            version_id=None,
            max_seconds=run.max_seconds,
            settings=values,
            entry=_entry(arguments, now, zone, person),
            who=who,
            actor=actor_label(who),
            person=person,
            cause="turn",
            counted_on=None,
            operations=operations_of(run.operations),
            arguments=arguments,
            hand_run_id=run.id,
        )


async def set_aside_audited(
    session: AsyncSession, tool: MadeTool, now: datetime, why: str, actor: str
) -> None:
    """Set ``tool`` aside for ``why`` (kept on its row), and audit it (its name and why)."""
    await set_aside(session, tool, now, reason=why)
    await append_audit(
        session, actor=actor, action=SET_ASIDE, details={"tool": tool.name, "reason": why}
    )


def _hit(key: str, figure: int) -> JsonObject:
    """``settings.hit`` as a launch's result holds it (never an audit line)."""
    return cast(JsonObject, hit(key, figure))


async def _counted_failure(
    session: AsyncSession,
    tool: MadeTool,
    now: datetime,
    failure: str,
    actor: str,
    *,
    killed: bool,
    most: int,
) -> Launched:
    """Count a failure of ``tool`` (one that could not be stopped, ``killed``, sets it aside at
    once; the ``most``-th in a row too, its ``failures_in_a_row``): ``failed``, or ``set_aside``
    when it was set aside now."""
    if tool.state != "ready":
        return Launched("failed", {"reason": failure})
    tool.failures_in_a_row += 1
    if killed:
        why = "it could not be stopped"
        caused: JsonObject = {}
    elif tool.failures_in_a_row >= most:
        why = (
            f"it failed {tool.failures_in_a_row} times in a row, the last time: {failure}; "
            f"{told('failures_in_a_row', most)}"
        )
        caused = _hit("failures_in_a_row", most)
    else:
        return Launched("failed", {"reason": failure})
    await set_aside_audited(session, tool, now, why, actor)
    return Launched("set_aside", {"reason": why, **caused})


async def _audit_launch(
    session: AsyncSession,
    actor: str,
    name: str,
    cause: str,
    seconds: float,
    status: str,
    *,
    requests: int = 0,
    reads: int = 0,
) -> None:
    """The audit entry of a launch: the tool, the cause, the seconds, the status, and how many
    requests its program made (``reads`` of them reads); never the arguments, a request or the
    output."""
    await append_audit(
        session,
        actor=actor,
        action=LAUNCHED,
        details={
            "tool": name,
            "cause": cause,
            "seconds": round(seconds, 2) if math.isfinite(seconds) else None,
            "status": status,
            "requests": requests,
            "reads": reads,
        },
    )


def result_of(output: JsonValue | None, most: int) -> JsonValue:
    """What a program gave back, as its result: any JSON value of ``most`` bytes at most, as JSON
    text in UTF-8 (its ``result_bytes``). Raises ``NotAResult`` otherwise. The dry runs of a build
    read it too (``madetools.checks``)."""
    try:
        size = len(json.dumps(output, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (ValueError, TypeError, RecursionError):
        raise NotAResult("what it gave back is not JSON") from None
    if size > most:
        raise NotAResult(
            f"what it gave back is bigger than {most} bytes; {told('result_bytes', most)}",
            ("result_bytes", most),
        )
    return output


def not_here(refused: SandboxNotHere, settings: Mapping[str, int]) -> JsonObject:
    """What Domo reads when the sandbox's machine cannot give what a run asks: the first setting
    it went past (``memory_mb`` or ``output_bytes``) with its figure (``settings``), and what the
    machine has (``has``). Nothing ran. A dry run of a build says it too (``madetools.checks``)."""
    key = refused.beyond[0]
    figure = settings[key]
    memory = refused.has.memory_mb
    if key == "memory_mb":
        lacks = f"has {memory} MB of memory for its programs"
    else:
        lacks = f"has {memory} MB of memory: it cannot hold an output of {figure} bytes"
    why = f"the place where your tools run {lacks}: nothing ran; {told(key, figure)}"
    return {"reason": why, **_hit(key, figure), "has": cast(JsonObject, refused.has.model_dump())}


async def _run(deps: LaunchDeps, ready: _Ready, scope: "LaunchScope", *, nested: bool) -> Launched:
    """Run ``ready`` in the sandbox with the bounds of its launch (its ``run_seconds``,
    ``memory_mb`` and ``output_bytes``), its program's requests answered (``means.Means``), keep
    what came of it, and say it. A ``nested`` run (asked by a program) takes a place only if one
    is free."""
    from domo_core.madetools.means import Means

    means = Means(deps, scope)
    started = asyncio.get_running_loop().time()
    request = RunRequest(
        code=ready.code,
        entry=ready.entry,
        max_seconds=ready.max_seconds,
        memory_mb=ready.settings["memory_mb"],
        output_bytes=ready.settings["output_bytes"],
        # What its program reads of one answer: what a tool it launches may give back, or what a
        # request out may read (ruling R21), the larger.
        answer_bytes=min(
            max(ready.settings["result_bytes"], ready.settings["out_bytes"]), ANSWER_CEILING_BYTES
        ),
        wait_for_a_place=not nested,
    )
    ran: RunResult | None = None
    busy = False
    lacking: SandboxNotHere | None = None
    try:
        ran = await deps.sandbox.run(request, answer=means.answer, waited_by_a_turn=scope.waited)
    except SandboxUnavailable:
        pass
    except SandboxBusy:
        busy = True
    except SandboxNotHere as refused:
        lacking = refused
    seconds = asyncio.get_running_loop().time() - started if ran is None else ran.seconds
    now = deps.clock()
    if lacking is not None:
        # Nothing ran, but it never will while its settings ask more than the machine has: counted
        # as a failure like any (final review I3), so that a tool no turn launches is set aside
        # and Domo woken with why, instead of failing at every firing unseen.
        told_back = not_here(lacking, ready.settings)
        launched, failure = Launched("failed", told_back), str(told_back["reason"])
    else:
        launched, failure = _came_of(ran, busy, means, ready.settings)
    # What the program itself said, as Domo reads it in the launch's result (``error``): never
    # a note of the sandbox's or the core's.
    said = _program_said(launched)
    if ready.tool_id is None or ready.name is None:
        # A program run by hand: no tool to count anything on.
        async with deps.session_factory() as session:
            await append_audit(
                session,
                actor=ready.actor,
                action=PROGRAM_RAN,
                details={
                    "status": launched.status,
                    "seconds": round(seconds, 2) if math.isfinite(seconds) else None,
                    "requests": means.requests,
                },
            )
            await session.commit()
        return launched
    aside_now = False
    async with deps.session_factory() as session:
        tool = await session.get(MadeTool, ready.tool_id, with_for_update=True)
        if tool is None:
            # Removed while it ran: nothing is kept.
            return Launched("failed", {"reason": "there is no such tool"})
        if launched.status == "done":
            tool.failures_in_a_row = 0
        if ran is None and tool.launches_day == ready.counted_on and tool.launches_today > 0:
            # A sandbox that does not answer, has no place or cannot give what it asks gives the
            # launch its count back: nothing ran (an outage never sets a tool aside, through the
            # launches of the day neither).
            tool.launches_today -= 1
        if failure is not None:
            counted = await _counted_failure(
                session,
                tool,
                now,
                failure,
                ready.actor,
                # Only a run the sandbox tried to end and could not (ruling R32).
                killed=ran is not None and ran.status == "killed",
                most=ready.settings["failures_in_a_row"],
            )
            if counted.status == "set_aside":
                launched, aside_now = counted, True
        await _audit_launch(
            session,
            ready.actor,
            ready.name,
            ready.cause,
            seconds,
            launched.status,
            requests=means.requests,
            reads=means.reads,
        )
        # Its trace, for Domo: the error its program said goes there, and why the core or the
        # sandbox ended it, never into the audit.
        reason, setting = _why(launched)
        await _traced(
            session,
            tool_id=tool.id,
            version_id=ready.version_id,
            at=now,
            cause=ready.cause,
            for_house=ready.person is None,
            by_turn=ready.cause == "turn",
            status=_ended(launched.status),
            seconds=seconds,
            requests=means.requests,
            reads=means.reads,
            answer_bytes=_answer_bytes(ran),
            error=said,
            reason=reason,
            setting=setting,
        )
        await session.commit()
    if aside_now:
        await _in_trouble(deps, ready.cause, ready.tool_id, ready.name, launched.result)
    return launched


async def _in_trouble(
    deps: LaunchDeps, cause: Cause, tool_id: uuid.UUID, name: str, result: JsonValue | None
) -> None:
    """The tool ``tool_id`` was just set aside by a launch (``result`` says why): Domo is woken
    on his own with it (``madetools.troubles``), unless a turn made that launch (``cause``): the
    turn reads it, or is woken with it once the run it handed over ends (one voice, R14)."""
    if cause == "turn":
        return
    # Imported here: what wakes Domo launches tools in turn.
    from domo_core.madetools.troubles import Trouble, wake_for_trouble

    await wake_for_trouble(deps, Trouble.set_aside(tool_id, name, result))


def _program_said(launched: Launched) -> str | None:
    """The error a program said, as a failed launch's result gives it to Domo (``error``: the
    program's words); ``None`` when it said none (a run the core or the sandbox stopped)."""
    if launched.status != "failed" or not isinstance(launched.result, dict):
        return None
    error = cast(JsonObject, launched.result).get("error")
    return error if isinstance(error, str) else None


# Why a launch that ended with no result of its own ended so, as its trace keeps it.
_ENDED_WITHOUT_WORDS: Final[Mapping[str, str]] = {
    "stopped": "it was stopped before it ended (its run was ended, not by its program)",
}


def _why(launched: Launched) -> tuple[str | None, tuple[str, int] | None]:
    """Why a launch that did not end ``done`` ended so, in the core's own words (with the
    sandbox's note beside them, never the program's words), and the setting that did it with its
    figure; ``(None, None)`` for a launch done. A launch that ended with nothing to say (a run
    stopped before its end, no result) is told by how it ended."""
    if launched.status == "done":
        return None, None
    if not isinstance(launched.result, dict):
        return _ENDED_WITHOUT_WORDS.get(launched.status, launched.status), None
    result = cast(JsonObject, launched.result)
    reason, note = result.get("reason"), result.get("note")
    said = reason if isinstance(reason, str) else launched.status
    if isinstance(note, str):
        said = f"{said} (the sandbox: {note})"
    key, figure = result.get("setting"), result.get("value")
    if isinstance(key, str) and isinstance(figure, int) and not isinstance(figure, bool):
        return said, (key, figure)
    return said, None


async def _traced(session: AsyncSession, **trace: Any) -> None:
    """The trace of a launch (``launches.record``), in a savepoint of the caller's transaction: a
    trace that cannot be written is logged by the kind of its error alone (never its words) and
    costs the launch nothing, neither its audit line nor what is counted on its tool."""
    try:
        async with session.begin_nested():
            await record(session, **trace)
    except Exception as error:
        logger.error("The trace of a launch could not be kept (%s)", type(error).__name__)


def _ended(status: LaunchStatus) -> LaunchEnd:
    """How a launch that ran, or was refused, ended, as its trace keeps it (a launch handed over
    to the background is traced where it ends: never ``running``)."""
    if status == "running":
        raise ValueError("a launch is traced once it ended")
    return status


def _answer_bytes(ran: RunResult | None) -> int:
    """What the program gave back weighs, as JSON in UTF-8 (0 when it gave nothing back)."""
    if ran is None or ran.output is None:
        return 0
    try:
        return len(json.dumps(ran.output, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (ValueError, TypeError, RecursionError):
        return 0


def _came_of(
    ran: RunResult | None, busy: bool, means: "Means", settings: Mapping[str, int]
) -> tuple[Launched, str | None]:
    """What came of a run (``ran``: ``None`` when the sandbox gave no place, ``busy``, or did not
    answer), and the failure it counts against its tool, if any."""
    from domo_core.madetools.means import too_many_requests

    if ran is None:
        # Not the tool's fault: it is not counted against it.
        if busy:
            # Never queued: the places are Domo's to change.
            return Launched("refused", {"reason": BUSY, **_hit("places", settings["places"])}), None
        if means.requests > 0:
            # Lost in the middle: what it asked may have been done (final review m2).
            return Launched("failed", {"reason": SANDBOX_CUT}), None
        return Launched("failed", {"reason": SANDBOX_DOWN}), None
    if means.too_many:
        # The core stopped a program that asked too much: its fault.
        most = settings["requests_per_launch"]
        said = too_many_requests(most)
        return Launched("failed", {"reason": said, **_hit("requests_per_launch", most)}), said
    if ran.status == "done":
        try:
            return Launched("done", result_of(ran.output, settings["result_bytes"])), None
        except NotAResult as wrong:
            told_back: JsonObject = {"reason": str(wrong)}
            if wrong.setting is not None:
                told_back.update(_hit(*wrong.setting))
            return Launched("failed", told_back), str(wrong)
    if ran.status == "stopped" and means.stopped_for is not None:
        # A yes is needed: Domo is woken with what was to be done (``means``).
        return Launched("stopped", {"reason": "needs_yes", **means.stop_told}), None
    if ran.status == "stopped":
        return Launched("stopped", None), None
    failure = _FAILURES[ran.status]
    caused: JsonObject = {}
    bound = _STOPPED_BY.get(ran.status)
    if bound is not None:
        # The sandbox stopped it on a figure of the launch's own: Domo reads which.
        failure = f"{failure}; {told(bound, settings[bound])}"
        caused = _hit(bound, settings[bound])
    elif ran.status == "ended_by_machine":
        # The runs at once share the machine's memory: each one's, and how many there are.
        memory, places = settings["memory_mb"], settings["places"]
        failure = f"{failure}; {told('memory_mb', memory)}; {told('places', places)}"
        caused = _hit("memory_mb", memory)
    failed_with: JsonObject = {"reason": failure, **caused}
    if ran.error is not None:
        # The program's own words, data for Domo.
        failed_with["error"] = ran.error
    if ran.note is not None:
        # The sandbox's own words on how it ended: never the program's.
        failed_with["note"] = ran.note
    return Launched("failed", failed_with), failure
