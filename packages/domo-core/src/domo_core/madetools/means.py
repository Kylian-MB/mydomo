"""What answers a running program's requests (spec 5.2, 5.8): each one judged at once, then done or
refused, while the program waits for its answer.

A launch (``launch.launch``) gives its run a ``Means`` (``answer``, an ``sandbox_client.Answerer``)
and the scope of the launch (``LaunchScope``): the tool, for whom it runs (a person, or the house)
and with whose rights (``actor``: the person's of the launch, Domo's own for the house), how deep
it is in its chain (``depth``, the first launch is 0), the chain it belongs to (``Chain``: the
launches it made, the wakes gathered, whether a turn still waits on it) and the tools that lead to
it (``path``, itself last). Each request is read (``asks.read_ask``; one that is not right is
refused ``not_valid``), described as an action of its own type and judged by the barrier
(``policy.decision.decide_now``) with ``scope.actor``, the catalog read once at the launch's first
request:

* ``begin`` (``program.begin``, its facts the tool, the operation, the amount and the operation's
  four risks): names one of the tool's declared operations (``LaunchScope.operations``; else
  refused ``unknown_operation``); an operation that costs says its amount (else ``amount_missing``).
  Every request but a read must follow one (else ``no_operation``), and belongs to the operation
  begun last: its risks are that operation's, never less than the code's minimum
  (``risks.minimum_risks``). The yes given for this launch (``LaunchScope.yes``) and the standing
  agreements (``yes.agreement_covers``) are read here, where the amount is declared; a begin let
  through that costs is declared (``yes.declare``, with the agreement it passed under). In a
  relaunch, the begin an earlier run of the chain declared is not declared again: within that
  amount its cost is settled (unless it passed under an agreement in an earlier period of it:
  then it is read under the agreements again, as a use of this period), beyond it the cost is
  asked again, and once let through the ledger holds what it is let through for now
  (``yes.declare_again``);
* ``read`` and ``write`` (``program.base.read``, ``program.base.write``): one statement on the
  tools' base (``toolsdb.store.ToolsBase.query``), as the role of whom the launch is for
  (``toolsdb.spaces.space_for``, read once per launch; its password never leaves the core), in the
  common space or their corner (``mine``: ``no_corner`` for the house), the base bounded by the
  owner's setting (``PolicySettings.tools_base_bytes``); its rows, or the base's refusal;
* ``act``: an action of the catalog a program may do (``program_actions.PROGRAM_HANDLERS``), as its
  handler describes it, then done; any other type is refused ``not_allowed``;
* ``tool``: another tool of Domo, launched for the same person in the same chain
  (``launch.launch`` with ``within``, judged there as ``made.<name>``, taking a place of the
  sandbox only if one is free at once: ``busy``, with the ``places`` setting), its result given;
  refused ``too_deep`` past ``chain_depth`` levels, ``in_its_own_path`` for a tool already in the
  path that leads to it, ``too_many_launches`` past ``chain_launches`` in the chain,
  ``no_such_tool`` (each refusal before a launch audited ``TOOL_REFUSED``: the asking tool and
  why, never the name asked);
* ``later`` (``program.later``): a ``time`` trigger of the same tool at that moment, for whom the
  launch is (``program_actions.time_trigger``): refused ``past``, ``too_far`` (more than its
  ``later_days`` ahead, with the setting), ``invalid`` (arguments that do not fit the tool),
  ``too_long``, or ``not_possible`` (the shape of a secret, or the tool waits with
  ``triggers_per_tool`` already);
* ``wake`` (``program.wake``): gathered for whom the launch is (``None``: the house, told to the
  owner of the moment it is given), one wake per person per chain (what each tool said, in the
  order asked), counted on the tool (``service.count_wake``: past ``wakes_per_day`` the tool is
  set aside and the wake refused). It is given once the chain ends (``handover.wake_chain``);
* ``out`` (``program.out``, step 2, spec 13.3 to 13.6): an HTTP request the gatekeeper sends for
  the program (``_out``, below).

**Going out.** An ``out`` is counted first (past ``out_per_launch`` in the launch:
``too_many_requests``). In a dry run nothing is looked up: an address the internet's as written
under an operation that does not declare ``outward`` is refused ``risk_not_declared``, but for a
port the tool has open on the machine that hosts Domo. For real, before
anything is judged or sent: its host and port must be one of the live addresses of the launch's
tool (``outward.service.open_for``; a program run by hand: those open without a tool), else
``closed_address``; then its zone is found (rulings R12, R14): ``host`` for a port of the machine
that hosts Domo opened as such (the house's for the barrier), the literal of an address, or, for
a name (a host its literal does not settle), what the gatekeeper finds on a first ``resolve_only``
lookup, whose refusal is the request's. The internet by the literal or by the lookup (never for
an opened host port) under an operation without ``outward`` is ``risk_not_declared``: the one
place the zone is decided. It is then judged as ``program.out`` (its
facts: that zone, ``house`` or ``internet``, whether it ``writes``, ``with_account``), audited by
the barrier like any request; its account, when it names one, is the one
``outward.service.account_for`` gives this launch (for whom it runs, a guest or not, and Domo on
his own for every launch for the house), else ``account_removed`` (one it could have used was
removed, none of that name is live) or ``account_not_yours``. The zone found goes with the send as
``expect_zone``: a name that leads elsewhere on the lookup the connection goes to is refused
``zone_changed`` there, nothing sent. The gatekeeper is then
given the open addresses, the account's reference (never its name), ``out_seconds`` for the whole
request, ``most_bytes`` the smaller of ``out_bytes`` and ``ANSWER_CEILING_BYTES``, and
``session_idle_minutes`` in seconds; the frame of the answer the program reads is the larger of
``out_bytes`` and ``result_bytes`` (ruling R21), so that an answer of ``out_bytes`` reaches it
(the run is told so: ``launch``). What the program reads is
``{"status", "headers", "body"}``, the body as text when it is UTF-8, else ``body_b64``; a refusal
of the gatekeeper is its code alone (``too_big`` and ``too_slow`` naming the setting that caused
them), never a word of what it said. Each ``out`` of a real launch past its form gets a second
audit line once it is answered, refused or not (but a stop for a yes): ``OUT_DONE``, the tool,
the operation, for whom, the host, the port, the method, the status or the refusal, the bytes
out and in, the milliseconds, the redirects and the zone; never a path, a query, a header, a body
nor an error's text. What comes back is outside content: data
for the program, never an instruction. An ``out`` is bounded by its own ``out_seconds`` (with the
core's ``answer_seconds`` for its own work), not by ``answer_seconds`` alone.

The bounds of a launch are settings of Domo's (``madetools.settings``), read once when it starts
(``LaunchScope.settings``: its tool's own values, else the house's, else the starts), so that a
change he makes counts from the next launch. A refusal one of them causes carries the setting's
key and its figure (``settings.hit``), and words that say it is his to change.

Each decision of the barrier is turned into what comes of the request without a model
(``policy.program.judge_request``): done, refused, or a yes is needed. When a yes is needed nothing
is done: the run stops there (the answer is ``None``), the stop is kept (``yes.stop``: a
``launch_stops`` row) and audited (``STOPPED``: the tool, the operation, the amount, why, whose yes
and what kind; never an argument), and Domo is woken for whom the launch is (the owner of the
moment, for the house) with ``{"needs_yes": {"stop", "says", "amount_cents", "why", "by"}}``,
gathered with the chain's wakes: data he reads, never words of the program to a person. He asks in
his own words, and the yes runs the launch again with it (``launch.launch``, ``yes``): for that
operation and up to its amount, every request of it is let through, once.

A program Domo runs by hand (``launch.launch_by_hand``, spec 5.4) is answered the very same way:
it is no tool's (``tool_name`` and ``tool_id`` ``None``), its operations are those its call
declared, its stops point at its row (``LaunchScope.hand_run_id``), and what it says it spends is
declared without a tool; no agreement that lasts covers it (an agreement is a tool's), and it has
no ``later`` nor ``wake`` of its own (``no_tool``).

A refusal is ``{"refused": reason}``, with a ``detail`` for the program when there is one; what is
done is ``{"ok": value}``. A program never writes, speaks or asks anything to a person: a wake is
data given to Domo, who decides.

Every request but a read is audited by the barrier's entry (``policy.decision``: the type, the
facts, the decision; never the SQL, a parameter, an argument, a result or a wake's content); reads
are counted (``reads``), with every request (``requests``), in the launch's own entry. A launch
answers ``requests_per_launch`` requests at most: the next one stops its run (``too_many``).
Each answer but a tool's is bounded by ``answer_seconds`` (``too_slow``); a tool's by that tool's
own limits.

A dry run (``LaunchScope.dry_run``, the checks of a build: ``madetools.checks``) is a trial of the
code, for nobody: what a request of it would do for real is never done, and it is judged by no
one's rights (the launch it would be part of is judged when it runs for real, for whom it runs),
so nothing of it is written in the audit as an act. What is refused for real whoever the launch is
for, by the form of the request alone, is refused alike, in the same order, so that a dry run
never passes code that is always refused: a request that is not right (``not_valid``), anything
but a read before any ``begin`` (``no_operation``), an operation the tool does not declare
(``unknown_operation``) or one that costs without its amount (``amount_missing``), an action no
program may do (``not_allowed``) or whose arguments its handler refuses, or what its handler would
refuse doing it whoever it is for (``ProgramAction.tried``: a tool timed that does not exist,
arguments that do not fit it), a ``later`` in the past, more than ``later_days`` ahead or whose
arguments do not fit the parameters submitted (``LaunchScope.parameters``), a tool too deep, in
its own path or that does not exist, past the chain's ``chain_launches`` (each tool asked is
counted in the dry run's chain, as it would be; the launches those tools would make in turn are
not seen), more than ``requests_per_launch`` requests, more than ``out_per_launch`` outs, and an
out to an address the internet's as written under an operation that does not declare ``outward``.
Otherwise a ``begin`` is
let through (nothing stops for a yes, nothing is declared), an ``act``, a ``tool``, a ``later``, a
``wake`` and an ``out`` answer ``null`` and do nothing (no trigger, no launch, nobody woken, nothing
sent: whether an out's address is open and its account exists is not checked, for they may not
be yet), and the reads and
writes run as the house, in the common space alone (``mine`` is ``no_corner``), all in one
transaction of the tools' base (``toolsdb.store.ToolsBase.trial``) that is rolled back when the
run ends (``Means.end``): what a write did is seen by the next request, and nothing stays."""

import asyncio
import base64
import json
import logging
import math
import time
import uuid
from collections.abc import Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Final, cast

from domo_common.gatekeeper_api import OpenAddress, OutMethod, OutRequest, OutResponse, Zone
from domo_common.sandbox_api import ANSWER_CEILING_BYTES, ANSWER_CEILING_WORDS, JsonObject
from pydantic import JsonValue
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.audit.log import append_audit
from domo_core.gatekeeper_client import OutRefused
from domo_core.madetools.asks import (
    ActAsk,
    AskNotRight,
    BeginAsk,
    LaterAsk,
    OutAsk,
    ProgramAsk,
    ReadAsk,
    ToolAsk,
    WakeAsk,
    WriteAsk,
    read_ask,
)
from domo_core.madetools.launch import ForWhom, LaunchDeps, launch, set_aside_audited
from domo_core.madetools.models import (
    OFFERED_PREFIX,
    DeclaredSpending,
    LaunchStop,
    MadeTool,
)
from domo_core.madetools.program_actions import (
    PROGRAM_HANDLERS,
    ProgramAction,
    time_trigger,
    trigger_arguments,
)
from domo_core.madetools.risks import Operation, minimum_risks
from domo_core.madetools.service import count_wake, tool_named
from domo_core.madetools.settings import hit, span, starts, told, wait_for
from domo_core.madetools.yes import (
    agreement_covers,
    carried_over,
    declare,
    declare_again,
    stop,
)
from domo_core.outward.literal import is_a_host, is_an_address, literal_zone
from domo_core.outward.service import account_for, open_for, was_removed
from domo_core.policy.actions import DescribedAction, FactValue
from domo_core.policy.barrier import Refuse, record_decision
from domo_core.policy.catalog import (
    NO_RISK,
    PROGRAM_BEGIN,
    PROGRAM_LATER,
    PROGRAM_OUT,
    PROGRAM_READ,
    PROGRAM_WAKE,
    PROGRAM_WRITE,
    PolicyTables,
    Risks,
)
from domo_core.policy.decision import decide_now
from domo_core.policy.open_catalog import load_policy
from domo_core.policy.program import (
    Go,
    Judged,
    NeedsYes,
    answers,
    judge_under_yes,
)
from domo_core.policy.roles import Actor, actor_label
from domo_core.policy.settings import load_policy_settings
from domo_core.tools.context import InvalidArguments
from domo_core.toolsdb.spaces import SpaceLogin, space_for
from domo_core.toolsdb.store import BaseBounds, BaseBusy, BaseRefused, ToolsBase, Trial

logger = logging.getLogger(__name__)


def too_many_requests(most: int) -> str:
    """What a launch that asked more than ``most`` requests is told, and kept as its failure."""
    return f"it made more than {most} requests in one launch; {told('requests_per_launch', most)}"


# The audit entry of a tool a program asked that was refused before any launch: the asking tool's
# name and why, never the name it asked.
TOOL_REFUSED: Final = "madetool.tool_refused"
# The audit entry of a launch stopped for a yes: the tool, the operation, the amount in cents, why,
# whose yes and what kind; never an argument nor what the operation says.
STOPPED: Final = "madetool.stopped"
# The audit entry of a request sent out once it is answered (the barrier's line came before it):
# see the module docstring. Never a path, a query, a header, a body nor an error's text.
OUT_DONE: Final = "madetool.out_done"
# What a program is told of a request out to the internet under an operation that does not say it
# leaves the house.
OUTWARD_NOT_DECLARED: Final = (
    "this address is outside the house: the operation must declare outward (nothing was sent)"
)

# Whom a wake goes to: a person, or ``None``, the owner for the house (looked up when it is told).
type WakeFor = uuid.UUID | None


@dataclass
class Chain:
    """What one chain of launches gathers while it runs: whom to wake and with what (by person,
    ``None`` the owner for the house, then by tool, in the order asked), the id of each tool that
    asked a wake (a wake kept for later points at one), how many launches it made (the first one
    included), and whether a turn still waits on it (its launches go first)."""

    wakes: dict[WakeFor, dict[str, list[JsonValue]]] = field(
        default_factory=lambda: dict[WakeFor, dict[str, list[JsonValue]]]()
    )
    tool_ids: dict[str, uuid.UUID] = field(default_factory=lambda: dict[str, uuid.UUID]())
    launches: int = 1
    turn_waits: bool = False


@dataclass(frozen=True)
class YesFor:
    """One yes given in a chain of relaunches, at its place: an operation (``None``: before any)
    and which begin of it in the run (``occurrence``, from 1; 0 before any), up to an amount
    (``None``: nothing), answering what was really given (``answered``: why, the approver who
    really gave it, what kind; it lifts nothing that asks more)."""

    operation: str | None
    amount_cents: int | None
    answered: NeedsYes
    occurrence: int = 0

    def as_json(self) -> JsonObject:
        """As a stop keeps it (``launch_stops.answered_before``)."""
        return {
            "operation": self.operation,
            "occurrence": self.occurrence,
            "amount_cents": self.amount_cents,
            "why": self.answered.why,
            "approver": self.answered.approver,
            "ask": self.answered.ask,
        }


@dataclass(frozen=True)
class YesGiven:
    """The yes a launch runs again with (``launch.launch``, ``yes``): the stop it answers, every
    yes given in the chain of relaunches that led to it, that stop's last (``answers``: a relaunch
    never forgets what an earlier yes answered), and the money the stopped run had declared, by
    begin (``declared``: ``"<operation>#<occurrence>"`` → the declaration's id and its cents; the
    relaunch never declares it again, up to that amount)."""

    stop_id: uuid.UUID
    answers: tuple[YesFor, ...]
    declared: Mapping[str, tuple[uuid.UUID, int]] = field(
        default_factory=lambda: dict[str, tuple[uuid.UUID, int]]()
    )

    @classmethod
    def of(cls, stop_row: "LaunchStop") -> "YesGiven":
        """The yes of the stop ``stop_row``, as it was kept (a value that does not read back is
        left out, never guessed)."""
        before: list[YesFor] = []
        for item in stop_row.answered_before:
            read = _yes_for(item)
            if read is not None:
                before.append(read)
        # What was really given (``take_yes`` keeps it), never what the stop asked.
        own = YesFor(
            stop_row.operation,
            stop_row.amount_cents,
            NeedsYes(stop_row.why, stop_row.yes_approver or "self", stop_row.ask),
            stop_row.occurrence,
        )
        declared: dict[str, tuple[uuid.UUID, int]] = {}
        for place, kept in stop_row.declared.items():
            read = _declaration(kept)
            if read is not None:
                declared[str(place)] = read
        return cls(stop_row.id, (*before, own), declared)


def _declaration(kept: object) -> tuple[uuid.UUID, int] | None:
    """A declaration a stop keeps (``{"id", "cents"}``), or ``None`` when it does not read back."""
    if not isinstance(kept, dict):
        return None
    given = cast(dict[str, object], kept)
    cents = given.get("cents")
    if not isinstance(cents, int) or isinstance(cents, bool):
        return None
    try:
        return uuid.UUID(str(given.get("id"))), cents
    except ValueError:
        return None


def _yes_for(item: object) -> YesFor | None:
    """An earlier yes as a stop keeps it, or ``None`` when it does not read back."""
    if not isinstance(item, dict):
        return None
    kept = cast(dict[str, object], item)
    operation, amount = kept.get("operation"), kept.get("amount_cents")
    why, approver, ask = kept.get("why"), kept.get("approver"), kept.get("ask")
    occurrence = kept.get("occurrence")
    if operation is not None and not isinstance(operation, str):
        return None
    if not isinstance(occurrence, int) or isinstance(occurrence, bool):
        return None
    if amount is not None and (not isinstance(amount, int) or isinstance(amount, bool)):
        return None
    if why not in ("costs", "owner_rule", "approval") or approver not in ("self", "adult", "owner"):
        return None
    if ask not in ("yes", "explicit_yes", "proof"):
        return None
    return YesFor(operation, amount, NeedsYes(why, approver, ask), occurrence)


@dataclass
class LaunchScope:
    """The launch a program runs in (see the module docstring). ``tool_name`` and ``tool_id`` are
    ``None`` for a program that is no tool's (a dry run's, or one Domo runs by hand:
    ``hand_run_id``, its row, which a stop points at); ``waited`` says whether a turn waits on
    this run. ``operations`` are the declared operations (the tool's, or the hand run's),
    ``arguments`` those of the launch (kept with a stop), ``yes`` the yes it runs again with."""

    tool_name: str | None
    tool_id: uuid.UUID | None
    for_whom: ForWhom
    actor: Actor
    depth: int
    chain: Chain
    waited: bool
    path: tuple[str, ...]
    dry_run: bool = False
    operations: Mapping[str, Operation] = field(default_factory=lambda: dict[str, Operation]())
    arguments: JsonObject = field(default_factory=lambda: dict[str, JsonValue]())
    yes: YesGiven | None = None
    hand_run_id: uuid.UUID | None = None
    # A dry run's: the parameters its tool is submitted with (a ``later`` must fit them).
    parameters: JsonObject | None = None
    # The bounds of this launch (``madetools.settings``), read once when it starts.
    settings: Mapping[str, int] = field(default_factory=starts)

    def bound(self, key: str) -> int:
        """The value of the setting ``key`` for this launch."""
        return self.settings[key]


def _refused(
    reason: str, detail: str | None = None, setting: tuple[str, int] | None = None
) -> JsonObject:
    """A refusal; one a setting caused (``setting``: its key and figure) carries them, and its
    detail says they are Domo's to change."""
    answer: JsonObject = {"refused": reason}
    if setting is not None:
        key, figure = setting
        words = told(key, figure)
        if detail is None:
            detail = words
        elif words not in detail:
            detail = f"{detail}; {words}"
        answer.update(cast(JsonObject, hit(key, figure)))
    if detail is not None:
        answer["detail"] = detail
    return answer


@dataclass
class _OutTrace:
    """What the second audit line of a request out says (``OUT_DONE``), gathered as it goes:
    whether it is to be written (``audited``: every request out of a real launch past its form,
    whatever comes of it but a stop for a yes), its status or its refusal, the bytes out and in,
    the redirects and the zone the gatekeeper found."""

    host: str
    port: int
    method: str
    bytes_out: int
    audited: bool = False
    status: int | None = None
    refused: str | None = None
    bytes_in: int = 0
    hops: int = 0
    zone: Zone | None = None
    started: float = field(default_factory=time.monotonic)

    def refuse(self, reason: str, detail: str | None = None) -> JsonObject:
        """The refusal ``reason``, noted for the audit line."""
        self.refused = reason
        return _refused(reason, detail)

    def details(self) -> dict[str, JsonValue]:
        return {
            # Only ever a valid host name or address: else what the program wrote, kept out.
            "host": self.host if is_a_host(self.host) else None,
            "port": self.port,
            "method": self.method,
            "status": self.status,
            "refused": self.refused,
            "bytes_out": self.bytes_out,
            "bytes_in": self.bytes_in,
            "ms": round((time.monotonic() - self.started) * 1000),
            "hops": self.hops,
            "zone": self.zone,
        }


def _on_host(open_now: list[OpenAddress], host: str, port: int) -> bool:
    """Whether ``host``:``port`` is open as a port of the machine that hosts Domo."""
    return any(o.on_host and o.host.strip("[]") == host and o.port == port for o in open_now)


class _StopRun(Exception):
    """A request needs a yes: the run stops there (``Means.answer`` answers ``None``)."""

    def __init__(self, stop_id: uuid.UUID | None) -> None:
        super().__init__("a yes is needed")
        self.stop_id = stop_id


# What a program is told of a request before any operation, of an operation its tool does not
# declare, and of a costing operation begun without its amount (a dry run says the same).
NO_OPERATION: Final = "begin an operation first: every request but a read belongs to one"
UNKNOWN_OPERATION: Final = "this tool declares no operation of this name"
AMOUNT_MISSING: Final = "this operation costs: begin it with its amount in cents, 1 or more"
# The key space of the lock that holds two begins that spend for the same one apart
# (``pg_advisory_xact_lock(key, hashtext(subject))``; the audit's lock is a single key).
SPENDING_LOCK: Final = 4243


def _program_action(action_type: str, facts: dict[str, FactValue] | None = None) -> DescribedAction:
    return DescribedAction(
        connector="program",
        action_type=action_type,
        target=None,
        effect="program",
        destination="household",
        facts=facts or {},
    )


class Means:
    """What answers the requests of one run (see the module docstring)."""

    def __init__(self, deps: LaunchDeps, scope: LaunchScope) -> None:
        self._deps = deps
        self._scope = scope
        self._policy: PolicyTables | None = None
        # How the launch logs in to the tools' base, and the base's bound: read at its first
        # statement. The login's password never leaves the core.
        self._login: SpaceLogin | None = None
        self._bound_bytes = 0
        self.requests = 0
        self.reads = 0
        # How many requests out this launch asked (``out_per_launch``).
        self.outs = 0
        self.too_many = False
        # The operation begun last, what it said it costs, and whether the yes of this launch
        # covers it.
        self._begun: Operation | None = None
        self._amount: int | None = None
        # The yeses of the chain of relaunches, each at its place, and what those of the place of
        # the operation begun last answered; how many begins of each operation this run made.
        given = scope.yes
        self._yeses: tuple[YesFor, ...] = () if given is None else given.answers
        self._begins: dict[str, int] = {}
        self._occurrence = 0
        self._under_yes: list[NeedsYes] = self._at(None, 0, None)
        # What the earlier runs declared and this run has not met yet, and what this run declared
        # (or met), by begin: what a stop keeps for the relaunch.
        self._declared_before: dict[str, tuple[uuid.UUID, int]] = (
            {} if given is None else dict(given.declared)
        )
        self._declared: dict[str, tuple[uuid.UUID, int]] = {}
        # The stop this run ended on, when it stopped for a yes.
        self._stop_id: uuid.UUID | None = None
        self.stopped_for: uuid.UUID | None = None
        # What the stop this run ended on was for, as the launch gives it.
        self.stop_told: JsonObject = {}
        # A dry run's transaction of the tools' base, opened at its first statement and rolled
        # back by ``end``.
        self._trial: Trial | None = None
        self._ends = AsyncExitStack()

    async def end(self) -> None:
        """What the run leaves behind is let go once it ended: a dry run's transaction of the tools'
        base is rolled back (nothing it wrote stays). Nothing for a real launch."""
        self._trial = None
        await self._ends.aclose()

    @property
    def _tool_label(self) -> str:
        name = self._scope.tool_name
        return "program" if name is None else f"{OFFERED_PREFIX}{name}"

    async def answer(self, raw: JsonObject) -> JsonObject | None:
        """The answer to the request ``raw``, or ``None`` to stop the run (past
        ``requests_per_launch``, or a yes is needed: ``stopped_for``). Never raises but for its
        cancellation: a request that could not be answered is refused ``failed``, logged by its kind
        alone."""
        self.requests += 1
        if self.requests > self._scope.bound("requests_per_launch"):
            self.too_many = True
            return None
        try:
            ask = read_ask(raw)
        except AskNotRight as wrong:
            return _refused("not_valid", str(wrong))
        if isinstance(ask, WakeAsk):
            # Weighed with its form, before anything: the launch's wake_bytes.
            too_big = self._wake_too_big(ask)
            if too_big is not None:
                return too_big
        if not isinstance(ask, BeginAsk | ReadAsk | ActAsk) and self._begun is None:
            return _refused("no_operation", NO_OPERATION)
        try:
            if isinstance(ask, ToolAsk):
                # Bounded by the tool's own limits (its time, its depth, its chain).
                return self._framed(await self._tool(ask), ask)
            if isinstance(ask, OutAsk):
                # Bounded by its own out_seconds (``_out``).
                return self._framed(await self._out(ask), ask)
            async with asyncio.timeout(wait_for(self._scope.bound("answer_seconds"))):
                return self._framed(await self._answered(ask), ask)
        except _StopRun as stopped:
            self.stopped_for = stopped.stop_id
            return None
        except TimeoutError:
            logger.warning("A program's request took too long to answer")
            return _refused(
                "too_slow", setting=("answer_seconds", self._scope.bound("answer_seconds"))
            )
        except Exception as error:
            # Its kind alone: nothing of the request nor of the base's login.
            logger.error("A program's request could not be answered (%s)", type(error).__name__)
            return _refused("failed")

    async def _answered(self, ask: ProgramAsk) -> JsonObject:
        if self._scope.dry_run:
            return await self._tried(ask)
        match ask:
            case BeginAsk():
                return await self._begin(ask)
            case ReadAsk():
                self.reads += 1
                return await self._statement(ask, write=False)
            case WriteAsk():
                return await self._statement(ask, write=True)
            case ActAsk():
                return await self._act(ask)
            case LaterAsk():
                return await self._later(ask)
            case WakeAsk():
                return await self._wake(ask)
            case ToolAsk():
                return await self._tool(ask)
            case OutAsk():
                return await self._out(ask)

    async def _tried(self, ask: ProgramAsk) -> JsonObject:
        """A request of a dry run (see the module docstring): refused for its form as for real,
        otherwise done without effect, the reads and writes in the run's trial."""
        match ask:
            case BeginAsk():
                operation = self._operation_of(ask)
                if not isinstance(operation, Operation):
                    return operation
                self._begun, self._amount = operation, ask.amount_cents
                return {"ok": None}
            case ReadAsk():
                self.reads += 1
                return await self._on_base(ask, write=False)
            case WriteAsk():
                return await self._on_base(ask, write=True)
            case ActAsk():
                found = self._act_of(ask)
                if not isinstance(found, tuple):
                    return found
                try:
                    await found[0].tried(ask.arguments, self._deps)
                except InvalidArguments as wrong:
                    return _refused(wrong.code, wrong.detail)
                return {"ok": None}
            case LaterAsk():
                refused = self._when_refused(ask)
                if refused is not None:
                    return refused
                if self._scope.parameters is not None:
                    try:
                        trigger_arguments(
                            self._scope.parameters,
                            ask.arguments,
                            self._scope.bound("trigger_argument_chars"),
                        )
                    except InvalidArguments as wrong:
                        return _refused(wrong.code, wrong.detail)
                return {"ok": None}
            case WakeAsk():
                return {"ok": None}
            case ToolAsk():
                return await self._tool(ask)
            case OutAsk():
                return await self._out(ask)

    # --- judging ---------------------------------------------------------------------------------

    async def _catalog(self, session: AsyncSession) -> PolicyTables:
        """The catalog this launch is judged with: read at its first request."""
        if self._policy is None:
            self._policy = await load_policy(session, self._deps.policy_base)
        return self._policy

    @property
    def _for_domo(self) -> bool:
        """Whether the launch is for the house: Domo's own rights, and his own yes."""
        return self._scope.actor.subject == "domo"

    def _risks(self, ask: ProgramAsk, policy: PolicyTables) -> Risks:
        """The risks of a request: its operation's, never less than the code's minimum."""
        begun = NO_RISK if self._begun is None else self._begun.risks
        return begun | minimum_risks(ask, policy)

    async def _judged(
        self, action: DescribedAction, ask: ProgramAsk, *, audited: bool
    ) -> JsonObject | None:
        """The barrier's word on ``action`` for ``scope.actor``, turned into what comes of the
        request (``judge_under_yes``; its cost was settled at its operation's begin): ``None``
        when it may go, else the refusal; a yes needed stops the run (``_StopRun``). Audited
        (``policy.decision``) unless it is a read."""
        async with self._deps.session_factory() as session:
            policy = await self._catalog(session)
            decision = await decide_now(
                session, self._scope.actor, action, policy, self._deps.clock()
            )
            judged = judge_under_yes(
                decision,
                risks=self._risks(ask, policy),
                for_domo=self._for_domo,
                covered=self._begun is not None and self._begun.risks.costs,
                yes=self._under_yes,
            )
            await self._stop_kept(session, judged)
            # The audit last in the transaction (it holds a lock until the end).
            if audited:
                await record_decision(
                    session, self._scope.actor, action, decision, tool_name=self._tool_label
                )
            await self._stop_audited(session, judged)
            await session.commit()
        self._wake_for_the_yes(judged)
        return None if isinstance(judged, Go | NeedsYes) else _refused(judged.reason)

    def _stops(self, judged: Judged) -> bool:
        """Whether ``judged`` stops the run: a yes is needed, and it is no dry run."""
        return isinstance(judged, NeedsYes) and not self._scope.dry_run

    async def _stop_kept(self, session: AsyncSession, judged: Judged) -> None:
        """When ``judged`` stops the run, the stop kept (``yes.stop``), in ``session``."""
        if not isinstance(judged, NeedsYes) or not self._stops(judged):
            return
        begun = self._begun
        self._stop_id = await stop(
            session,
            self._scope,
            None if begun is None else begun.name,
            judged.why,
            judged.approver,
            judged.ask,
            self._scope.arguments,
            self._amount,
            occurrence=self._occurrence,
            answered_before=[]
            if self._scope.yes is None
            else [yes.as_json() for yes in self._scope.yes.answers],
            declared={
                place: {"id": str(declaration), "cents": cents}
                for place, (declaration, cents) in {
                    **self._declared_before,
                    **self._declared,
                }.items()
            },
        )

    async def _stop_audited(self, session: AsyncSession, judged: Judged) -> None:
        """When ``judged`` stopped the run, its audit entry (``STOPPED``), in ``session``."""
        if not isinstance(judged, NeedsYes) or not self._stops(judged):
            return
        begun = self._begun
        await append_audit(
            session,
            actor=actor_label(self._scope.actor),
            action=STOPPED,
            details={
                "tool": self._scope.tool_name,
                "operation": None if begun is None else begun.name,
                "amount_cents": self._amount,
                "why": judged.why,
                "approver": judged.approver,
                "ask": judged.ask,
            },
        )

    def _wake_for_the_yes(self, judged: Judged) -> None:
        """Once the stop is kept, what was to be done is told (``stop_told``, which the launch
        gives as its result), and the run stops (``_StopRun``). A turn that waits on this launch
        reads it there (ruling R14): nobody is woken. Otherwise Domo is woken for whom the
        launch is (the owner, for the house), gathered with the chain's wakes. Nothing in a dry
        run."""
        if not isinstance(judged, NeedsYes) or not self._stops(judged):
            return
        name, tool_id = self._scope.tool_name, self._scope.tool_id
        begun = self._begun
        told: JsonObject = {
            "stop": str(self._stop_id),
            "says": None if begun is None else begun.says,
            "amount_cents": self._amount,
            "why": judged.why,
            "by": judged.approver,
        }
        self.stop_told = told
        turn_reads_it = self._scope.depth == 0 and self._scope.chain.turn_waits
        if name is not None and tool_id is not None and not turn_reads_it:
            person = self._scope.for_whom.person
            whom: WakeFor = None if person is None else person.id
            wake: JsonObject = {"needs_yes": told}
            self._scope.chain.wakes.setdefault(whom, {}).setdefault(name, []).append(wake)
            self._scope.chain.tool_ids.setdefault(name, tool_id)
        raise _StopRun(self._stop_id)

    # --- each request ----------------------------------------------------------------------------

    def _at(
        self, operation: str | None, occurrence: int, amount_cents: int | None
    ) -> list[NeedsYes]:
        """What the yeses of the chain given at this place answered: the ``occurrence``-th begin
        of ``operation`` (``None``, 0: before any), up to their amount. A yes applies within the
        begin it was given at and nowhere else, so each relaunch gets past the request its last
        stop was at (``policy.program.judge_under_yes``)."""
        return [
            yes.answered
            for yes in self._yeses
            if yes.operation == operation
            and yes.occurrence == occurrence
            and (amount_cents or 0) <= (yes.amount_cents or 0)
        ]

    async def _declared_at(self, session: AsyncSession, place: str) -> DeclaredSpending | None:
        """The declaration an earlier run of this chain made for the begin at ``place``
        (``"<operation>#<occurrence>"``), as the ledger holds it now; ``None`` when there is none.
        Met once: this run keeps it from then on (a stop carries it to the next relaunch)."""
        kept = self._declared_before.pop(place, None)
        if kept is None:
            return None
        found = await session.get(DeclaredSpending, kept[0])
        if found is not None:
            self._declared[place] = (found.id, found.amount_cents)
        return found

    async def _hold_spending(self, session: AsyncSession, for_person_id: uuid.UUID | None) -> None:
        """Hold apart, until ``session`` ends, two begins that spend for the same one (a person,
        or the house): what one declares is in what the other's limits read (``spent``). Taken
        before the decision, in the order every path keeps: the tool's row (shared), then this
        lock, then the agreements, the declaration and the stop, and the audit last. A program run
        by hand has no tool's row."""
        if self._scope.tool_id is not None:
            await session.execute(
                select(MadeTool.id)
                .where(MadeTool.id == self._scope.tool_id)
                .with_for_update(key_share=True)
            )
        subject = "house" if for_person_id is None else str(for_person_id)
        await session.execute(
            select(func.pg_advisory_xact_lock(SPENDING_LOCK, func.hashtext(f"spending:{subject}")))
        )

    def _operation_of(self, ask: BeginAsk) -> Operation | JsonObject:
        """The declared operation a ``begin`` names, or what it is refused for its form: an
        operation the tool does not declare, or one that costs said without its amount."""
        operation = self._scope.operations.get(ask.operation)
        if operation is None:
            return _refused("unknown_operation", UNKNOWN_OPERATION)
        if operation.risks.costs and (ask.amount_cents is None or ask.amount_cents < 1):
            return _refused("amount_missing", AMOUNT_MISSING)
        return operation

    async def _begin(self, ask: BeginAsk) -> JsonObject:
        operation = self._operation_of(ask)
        if not isinstance(operation, Operation):
            return operation
        kept = (
            self._occurrence,
            dict(self._begins),
            dict(self._declared_before),
            dict(self._declared),
        )
        try:
            judged = await self._begin_judged(ask, operation)
        except BaseException:
            # Not answered (an error, too slow): its transaction rolled back, nothing of it was
            # kept, so no operation is begun (what follows is refused ``no_operation``, never
            # covered by a cost nobody settled) and what earlier runs declared is still to meet.
            self._begun, self._amount, self._under_yes = None, None, []
            self._occurrence, self._begins, self._declared_before, self._declared = kept
            raise
        self._wake_for_the_yes(judged)
        if isinstance(judged, Go | NeedsYes):
            return {"ok": None}
        self._begun, self._amount, self._under_yes = None, None, []
        return _refused(judged.reason)

    async def _begin_judged(self, ask: BeginAsk, operation: Operation) -> Judged:
        """The barrier's word on the begin of ``operation``, its declaration and its stop kept and
        audited, committed."""
        risks = operation.risks
        # The operation begun now, and which begin of it: what follows belongs to it, whatever
        # comes of this one.
        self._begun, self._amount = operation, ask.amount_cents
        self._occurrence = self._begins.get(operation.name, 0) + 1
        self._begins[operation.name] = self._occurrence
        self._under_yes = self._at(operation.name, self._occurrence, ask.amount_cents)
        place = f"{operation.name}#{self._occurrence}"
        amount = ask.amount_cents or 0
        facts: dict[str, FactValue] = {
            "tool": self._scope.tool_name,
            "operation": operation.name,
            "amount_cents": ask.amount_cents,
            "costs": risks.costs,
            "irreversible": risks.irreversible,
            "physical": risks.physical,
            "outward": risks.outward,
        }
        action = _program_action(PROGRAM_BEGIN, facts)
        person = self._scope.for_whom.person
        for_person_id = None if person is None else person.id
        now = self._deps.clock()
        # A tool's launch, or a program run by hand: a dry run spends nothing.
        spends = (
            risks.costs
            and not self._scope.dry_run
            and (self._scope.tool_id is not None or self._scope.hand_run_id is not None)
        )
        async with self._deps.session_factory() as session:
            if spends:
                await self._hold_spending(session, for_person_id)
            policy = await self._catalog(session)
            # What an earlier run of the chain declared for this very begin: that money is never
            # declared nor counted twice (the limits read this begin's amount, and what was spent
            # without that declaration).
            before = await self._declared_at(session, place) if spends else None
            decision = await decide_now(
                session,
                self._scope.actor,
                action,
                policy,
                now,
                spent_leaving_out=None if before is None else before.id,
            )
            zone = (await load_policy_settings(session)).time_zone if spends else None
            # Settled: its cost was let through before, up to that amount, and still stands. One
            # that passed under an agreement in an earlier period of it does not: let through
            # now, it is a use of this period, read again like any other.
            settled = (
                before is not None
                and amount <= before.amount_cents
                and not await carried_over(session, before, now, zone)
            )
            agreement = before.agreement_id if before is not None and settled else None
            cost = NeedsYes("costs", "self", "yes")
            # A cost asked again of a begin an earlier run declared is answered only by a yes
            # given for that cost: one given for another request of that begin (the approval of
            # its write, say) never stands for it.
            for_cost = (
                self._under_yes
                if before is None or settled
                else [given for given in self._under_yes if given.why == "costs"]
            )
            answered = any(answers(given, cost) for given in for_cost)
            asks_one = not settled and not answered and not isinstance(decision, Refuse)
            tool_id = self._scope.tool_id
            if spends and asks_one and tool_id is not None:
                # Locked until this transaction ends: what is declared under it counts for the
                # next launch that asks.
                agreement = await agreement_covers(
                    session,
                    tool_id=tool_id,
                    operation=operation.name,
                    for_person_id=for_person_id,
                    amount_cents=amount,
                    now=now,
                    zone=zone,
                    leaving_out=None if before is None else before.id,
                )
            judged = judge_under_yes(
                decision,
                risks=risks,
                for_domo=self._for_domo,
                covered=settled or agreement is not None or self._scope.dry_run,
                yes=self._under_yes,
                for_cost=for_cost,
            )
            if isinstance(judged, Go) and spends and before is not None:
                # Spent now: the ledger holds what this begin is really let through for (lower,
                # or more after its own yes), under what it passes now, in the period of now.
                await declare_again(
                    session, before.id, amount_cents=amount, agreement_id=agreement, at=now
                )
                self._declared[place] = (before.id, amount)
            elif isinstance(judged, Go) and spends:
                # A tool's, or a program run by hand's (no tool: its spending outlives it).
                declaration = await declare(
                    session,
                    for_person_id=for_person_id,
                    tool_id=tool_id,
                    operation=operation.name,
                    amount_cents=amount,
                    agreement_id=agreement,
                    at=now,
                )
                if declaration is not None:
                    self._declared[place] = (declaration, amount)
            await self._stop_kept(session, judged)
            # The audit last in the transaction (it holds a lock until the end).
            await record_decision(
                session, self._scope.actor, action, decision, tool_name=self._tool_label
            )
            await self._stop_audited(session, judged)
            await session.commit()
        return judged

    async def _statement(self, ask: ReadAsk | WriteAsk, *, write: bool) -> JsonObject:
        action = _program_action(PROGRAM_WRITE if write else PROGRAM_READ)
        refused = await self._judged(action, ask, audited=write)
        if refused is not None:
            return refused
        return await self._on_base(ask, write=write)

    async def _on_base(self, ask: ReadAsk | WriteAsk, *, write: bool) -> JsonObject:
        """The statement of a read or a write let through, on the tools' base: as whom the launch
        is for, or, in a dry run, as the house, in its trial."""
        base = self._deps.tools_base
        if base is None:
            return _refused("no_base")
        if ask.space == "mine" and (self._scope.dry_run or self._scope.for_whom.person is None):
            # The house has no corner: only the common space is its.
            return _refused("no_corner")
        login = await self._login_to(base)
        scope = self._scope
        bounds = BaseBounds(
            rows=scope.bound("rows"),
            statement_seconds=scope.bound("statement_seconds"),
            request_seconds=scope.bound("request_seconds"),
            answer_bytes=scope.bound("result_bytes"),
        )
        if scope.dry_run and self._trial is None:
            try:
                self._trial = await self._ends.enter_async_context(base.trial(login, bounds))
            except BaseBusy as busy:
                return _refused("sql_error", str(busy))
        answer = await base.query(
            login,
            sql=ask.sql,
            params=list(ask.params),
            write=write,
            space=ask.space,
            bound_bytes=self._bound_bytes,
            trial=self._trial,
            bounds=bounds,
        )
        if isinstance(answer, BaseRefused):
            caused = answer.setting
            setting = None if caused is None else (caused, scope.bound(caused))
            return _refused(answer.reason, answer.detail, setting)
        return {"ok": cast(JsonValue, answer.rows)}

    async def _login_to(self, base: ToolsBase) -> SpaceLogin:
        """How this launch logs in to the tools' base (the person's role, made on first use, or
        the house's: a dry run's too), and the base's bound, read once per launch."""
        if self._login is None:
            person = None if self._scope.dry_run else self._scope.for_whom.person
            async with self._deps.session_factory() as session:
                login = await space_for(session, base.admin, None if person is None else person.id)
                self._bound_bytes = (await load_policy_settings(session)).tools_base_bytes
                await session.commit()
            self._login = login
        return self._login

    def _act_of(self, ask: ActAsk) -> tuple[ProgramAction, DescribedAction] | JsonObject:
        """The handler of an action and what it would do, or what it is refused for its form: no
        program may do its type (``not_allowed``), its arguments are not right, or no operation
        was begun (``no_operation``)."""
        handler = PROGRAM_HANDLERS.get(ask.action)
        if handler is None:
            return _refused("not_allowed")
        try:
            action = handler.describe(ask.arguments, self._scope)
        except InvalidArguments as wrong:
            return _refused(wrong.code, wrong.detail)
        if self._begun is None:
            return _refused("no_operation", NO_OPERATION)
        return handler, action

    async def _act(self, ask: ActAsk) -> JsonObject:
        found = self._act_of(ask)
        if not isinstance(found, tuple):
            if ask.action not in PROGRAM_HANDLERS:
                await self._refused_type(ask.action)
            return found
        handler, action = found
        refused = await self._judged(action, ask, audited=True)
        if refused is not None:
            return refused
        try:
            return {"ok": await handler.run(ask.arguments, self._scope, self._deps)}
        except InvalidArguments as wrong:
            return _refused(wrong.code, wrong.detail)

    async def _refused_type(self, action_type: str) -> None:
        """A type no program may do, refused: audited when it is a type of the catalog (the owner
        sees that a program tried it); a name the catalog does not know is no type, and is not
        written anywhere."""
        async with self._deps.session_factory() as session:
            if action_type not in (await self._catalog(session)).known:
                return
            await record_decision(
                session,
                self._scope.actor,
                _program_action(action_type),
                Refuse("not_allowed"),
                tool_name=self._tool_label,
            )
            await session.commit()

    def _when_refused(self, ask: LaterAsk) -> JsonObject | None:
        """What a ``later`` is refused for its time: in the past, or more than ``later_days``
        ahead (with the setting)."""
        now = self._deps.clock()
        if ask.at <= now:
            return _refused("past", "at is past")
        days = self._scope.bound("later_days")
        # Past what a date holds, as far as it holds: never an error of the arithmetic.
        if ask.at > now + span(days * 86_400):
            return _refused("too_far", f"at is more than {days} days ahead", ("later_days", days))
        return None

    def _wake_too_big(self, ask: WakeAsk) -> JsonObject | None:
        """A wake whose content weighs more than the launch's ``wake_bytes``, refused with the
        setting."""
        most = self._scope.bound("wake_bytes")
        size = len(
            json.dumps(ask.content, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        if size > most:
            return _refused(
                "too_big", f"the content weighs more than {most} bytes", ("wake_bytes", most)
            )
        return None

    def _framed(self, answer: JsonObject | None, ask: ProgramAsk) -> JsonObject | None:
        """``answer`` when the program can read it: its launch's ``result_bytes`` at most (for a
        request out, the larger of ``out_bytes`` and ``result_bytes``: ``_frame_of``), and
        ``ANSWER_CEILING_BYTES`` whatever the setting (ruling R37), as JSON in UTF-8. Else a
        refusal that says which: the setting (``hit``), or the code's ceiling (no setting: read in
        pieces). When what was asked was carried out (anything but a read that answered), the
        refusal begins "done:", so that the program never does it again for lack of an answer
        (it is small: it always fits the sandbox's floor of an answer)."""
        if answer is None:
            return None
        setting, most = self._frame_of(ask)
        try:
            size = len(json.dumps(answer, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        except (ValueError, TypeError, RecursionError):
            # Not JSON: the sandbox says it is not valid.
            return answer
        if size <= min(most, ANSWER_CEILING_BYTES):
            return answer
        done = "ok" in answer and not isinstance(ask, ReadAsk)
        if isinstance(ask, ToolAsk) and done:
            said = "the tool ran and did what it does; its result is bigger than this program reads"
        elif done:
            said = "done: it was carried out; its answer is bigger than this program reads"
        else:
            said = "the answer is bigger than this program reads"
        if done and not said.startswith("done:"):
            said = f"done: {said}"
        if size > ANSWER_CEILING_BYTES:
            return _refused("too_big", f"{said}; {ANSWER_CEILING_WORDS}")
        return _refused("too_big", said, (setting, most))

    def _frame_of(self, ask: ProgramAsk) -> tuple[str, int]:
        """The setting that frames the answer to ``ask``, and its figure: ``result_bytes``; for a
        request out, the larger of ``out_bytes`` and ``result_bytes`` (ruling R21: an answer of
        ``out_bytes`` reaches the program)."""
        result = ("result_bytes", self._scope.bound("result_bytes"))
        if not isinstance(ask, OutAsk):
            return result
        out = ("out_bytes", self._scope.bound("out_bytes"))
        return out if out[1] > result[1] else result

    async def _later(self, ask: LaterAsk) -> JsonObject:
        refused = await self._judged(_program_action(PROGRAM_LATER), ask, audited=True)
        if refused is not None:
            return refused
        tool_id = self._scope.tool_id
        if tool_id is None:
            return _refused("no_tool", "a program that is no tool's is not launched again")
        refused = self._when_refused(ask)
        if refused is not None:
            return refused
        person = self._scope.for_whom.person
        async with self._deps.session_factory() as session:
            row = await session.get(MadeTool, tool_id, with_for_update=True)
            if row is None or row.state != "ready":
                return _refused("no_such_tool", "the tool is no longer ready")
            try:
                await time_trigger(
                    session,
                    row,
                    due_at=ask.at,
                    repeat=None,
                    for_person_id=None if person is None else person.id,
                    arguments=ask.arguments,
                )
            except InvalidArguments as wrong:
                return _refused(wrong.code, wrong.detail, wrong.setting)
            await session.commit()
        return {"ok": None}

    async def _wake(self, ask: WakeAsk) -> JsonObject:
        # The content is data for Domo: never a fact, nor in the audit.
        refused = await self._judged(_program_action(PROGRAM_WAKE), ask, audited=True)
        if refused is not None:
            return refused
        name, tool_id = self._scope.tool_name, self._scope.tool_id
        if name is None or tool_id is None:
            return _refused("no_tool", "a program that is no tool's wakes nobody yet")
        now = self._deps.clock()
        async with self._deps.session_factory() as session:
            row = await session.get(MadeTool, tool_id)
            if row is None:
                return _refused("no_such_tool")
            zone = (await load_policy_settings(session)).time_zone
            most = self._scope.bound("wakes_per_day")
            if not await count_wake(session, row, now, zone, most):
                why = f"it asked to wake you more than {most} times today"
                reason = f"{why}; {told('wakes_per_day', most)}"
                aside_now = row.state == "ready"
                if aside_now:
                    await set_aside_audited(session, row, now, reason, "system")
                await session.commit()
                if aside_now:
                    # Imported here: what wakes Domo answers programs in turn.
                    from domo_core.madetools.troubles import Trouble, wake_for_trouble

                    # Told on his own, whoever the launch is for: the launch itself goes on, and
                    # what it gives back says nothing of it.
                    setting = ("wakes_per_day", most)
                    await wake_for_trouble(
                        self._deps,
                        Trouble("set_aside", name, reason, tool_id=tool_id, setting=setting),
                    )
                return _refused("too_many_wakes", why, ("wakes_per_day", most))
            await session.commit()
        person = self._scope.for_whom.person
        chain = self._scope.chain
        whom: WakeFor = None if person is None else person.id
        chain.wakes.setdefault(whom, {}).setdefault(name, []).append(ask.content)
        chain.tool_ids.setdefault(name, tool_id)
        return {"ok": None}

    async def _tool_refused(self, reason: str) -> None:
        """The audit entry of a tool asked and refused before any launch: the asking tool's name
        and why (``TOOL_REFUSED``); never the name it asked, which is the program's."""
        async with self._deps.session_factory() as session:
            await append_audit(
                session,
                actor=actor_label(self._scope.actor),
                action=TOOL_REFUSED,
                details={"tool": self._scope.tool_name, "reason": reason},
            )
            await session.commit()

    async def _tool(self, ask: ToolAsk) -> JsonObject:
        scope = self._scope
        refused: JsonObject | None = None
        found: MadeTool | None = None
        deepest, most = scope.bound("chain_depth"), scope.bound("chain_launches")
        if scope.depth + 1 >= deepest:
            refused = _refused(
                "too_deep",
                f"tools launched by tools go {deepest} deep at most",
                ("chain_depth", deepest),
            )
        elif ask.tool in scope.path:
            refused = _refused(
                "in_its_own_path", "this tool is already in the path that leads here"
            )
        elif scope.chain.launches >= most:
            refused = _refused(
                "too_many_launches",
                f"one chain launches {most} tools at most",
                ("chain_launches", most),
            )
        else:
            async with self._deps.session_factory() as session:
                found = await tool_named(session, ask.tool)
            if found is None:
                refused = _refused("no_such_tool", "there is no tool of this name")
        if found is None:
            # Every path that finds no tool said why above.
            assert refused is not None
            if not scope.dry_run:
                await self._tool_refused(str(refused["refused"]))
            return refused
        scope.chain.launches += 1
        if scope.dry_run:
            # Nothing is launched (it is counted in the chain, as it would be): the launch is
            # judged when it runs for real.
            return {"ok": None}
        came = await launch(
            self._deps,
            found.id,
            arguments=ask.arguments,
            for_whom=scope.for_whom,
            cause="tool",
            within=scope,
        )
        if came.status == "done":
            return {"ok": came.result}
        result = came.result if isinstance(came.result, dict) else {}
        reason, key, figure = result.get("reason"), result.get("setting"), result.get("value")
        said = reason if isinstance(reason, str) else None
        # A setting that stopped the tool asked (``busy``: the places) is told to the program too.
        setting = (
            (key, figure)
            if isinstance(key, str) and isinstance(figure, int) and not isinstance(figure, bool)
            else None
        )
        if came.status == "refused":
            # Nobody is asked from here: what needs someone's agreement is refused (Task 8).
            if said is None or said == "needs_confirmation":
                return _refused("not_allowed" if said is None else "needs_agreement")
            if setting is not None:
                return _refused(said, None, setting)
            return _refused(said)
        return _refused(came.status, said, setting)

    # --- going out -------------------------------------------------------------------------------

    @property
    def _declares_outward(self) -> bool:
        """Whether the operation begun last says it leaves the house."""
        return self._begun is not None and self._begun.risks.outward

    async def _out(self, ask: OutAsk) -> JsonObject:
        """A request out (see the module docstring, "Going out"): counted, then, in a dry run,
        weighed by its literal (but for an opened host port), and for real, its zone found once,
        judged, checked and sent within ``out_seconds`` (and the core's own
        ``answer_seconds``), its second audit line written once it is answered."""
        scope = self._scope
        most = scope.bound("out_per_launch")
        self.outs += 1
        if self.outs > most:
            return _refused(
                "too_many_requests",
                f"one launch sends {most} requests out at most",
                ("out_per_launch", most),
            )
        host, port = ask.address
        literal = literal_zone(host)
        if scope.dry_run:
            # Nothing is looked up nor sent, and its address and account may not exist yet: the
            # literal alone is weighed, but for a port the tool has open on the host machine.
            if literal == "internet" and not self._declares_outward:
                async with self._deps.session_factory() as session:
                    open_now = await open_for(session, scope.tool_name)
                if not _on_host(open_now, host, port):
                    return _refused("risk_not_declared", OUTWARD_NOT_DECLARED)
            return {"ok": None}
        trace = _OutTrace(host, port, ask.method, len(ask.body or b""))
        seconds = scope.bound("out_seconds")
        bound = wait_for(seconds) + wait_for(scope.bound("answer_seconds"))
        try:
            async with asyncio.timeout(bound):
                answer = await self._sent_out(ask, literal, trace)
        except TimeoutError:
            logger.warning("A program's request out took too long to answer")
            trace.refused = "too_slow"
            answer = _refused("too_slow", setting=("out_seconds", seconds))
        except _StopRun:
            # A yes is needed: the barrier's line and the stop say so; nothing went out.
            raise
        except Exception:
            if trace.audited:
                trace.refused = "failed"
                await self._out_audited(trace)
            raise
        if trace.audited:
            await self._out_audited(trace)
        return answer

    async def _sent_out(self, ask: OutAsk, literal: str, trace: _OutTrace) -> JsonObject:
        """The request out: its address checked open, its zone found (a name is looked up first,
        ruling R14), judged with that zone, its account checked, then sent held to that zone:
        what the program reads (``trace`` notes what came of it, for the audit)."""
        trace.audited = True
        gatekeeper = self._deps.gatekeeper
        if gatekeeper is None:
            return trace.refuse("unreachable")
        open_now = await self._open_now(trace)
        if not isinstance(open_now, list):
            return open_now
        scope = self._scope
        seconds = scope.bound("out_seconds")
        ends = time.monotonic() + seconds
        # The body read: out_bytes, under the channel's ceiling (the frame of its answer follows
        # the larger of out_bytes and result_bytes, ``_framed``: ruling R21).
        most = min(scope.bound("out_bytes"), ANSWER_CEILING_BYTES)
        idle = scope.bound("session_idle_minutes") * 60
        # The zone judged, which the barrier reads and the gatekeeper holds the send to (rulings
        # R12, R14): a port of the machine that hosts Domo opened as such (the house's, for the
        # barrier), the literal of an address, or, for a name, where the gatekeeper finds it leads
        # (the internet when its literal or its lookup says so).
        judged: Zone = cast(Zone, literal)
        if _on_host(open_now, trace.host, trace.port):
            judged = "host"
        elif not is_an_address(trace.host):
            # Always looked up (ruling R16): its refusal (host_not_opened, never_reachable...)
            # says what is true, before the risk is weighed.
            looked = OutRequest(
                method=cast(OutMethod, ask.method),
                url=ask.url,
                open=open_now,
                seconds=seconds,
                most_bytes=0,
                session_idle_seconds=idle,
                resolve_only=True,
            )
            try:
                judged = (await gatekeeper.out(looked)).zone
            except OutRefused as refusal:
                return self._out_refused(refusal, trace, most)
            trace.zone = judged
        # The one decision of the zone: an opened host port is the house, whatever its literal.
        internet = judged != "host" and (literal == "internet" or judged == "internet")
        if internet and not self._declares_outward:
            return trace.refuse("risk_not_declared", OUTWARD_NOT_DECLARED)
        facts: dict[str, FactValue] = {
            "zone": "internet" if internet else "house",
            "writes": ask.writes,
            "with_account": ask.account is not None,
        }
        refused = await self._judged(_program_action(PROGRAM_OUT, facts), ask, audited=True)
        if refused is not None:
            trace.refused = str(refused["refused"])
            return refused
        ref: str | None = None
        if ask.account is not None:
            found = await self._account_ref(ask.account, trace)
            if not isinstance(found, str):
                return found
            ref = found
        sent = OutRequest(
            method=cast(OutMethod, ask.method),
            url=ask.url,
            headers=ask.headers,
            body_b64=None if ask.body is None else base64.b64encode(ask.body).decode("ascii"),
            account=ref,
            open=open_now,
            seconds=max(1, math.ceil(ends - time.monotonic())),
            most_bytes=most,
            session_idle_seconds=idle,
            expect_zone=judged,
        )
        try:
            response = await gatekeeper.out(sent)
        except OutRefused as refusal:
            return self._out_refused(refusal, trace, most)
        return self._read_back(response, trace)

    async def _open_now(self, trace: _OutTrace) -> list[OpenAddress] | JsonObject:
        """The addresses open for the launch's tool (``None``: a program run by hand), when the
        request's host and port are one of them; else ``closed_address``."""
        async with self._deps.session_factory() as session:
            open_now = await open_for(session, self._scope.tool_name)
        if not any(o.host.strip("[]") == trace.host and o.port == trace.port for o in open_now):
            return trace.refuse("closed_address")
        return open_now

    async def _account_ref(self, name: str, trace: _OutTrace) -> str | JsonObject:
        """The reference of the account ``name`` this launch may use (``account_for``: for whom
        it runs, a guest or not, Domo on his own for every launch for the house, spec 13.5);
        else ``account_removed`` or ``account_not_yours`` (``was_removed``)."""
        person = self._scope.for_whom.person
        for_person = None if person is None else person.id
        async with self._deps.session_factory() as session:
            found = await account_for(
                session,
                name=name,
                for_person=for_person,
                on_his_own=person is None,
                is_guest=person is not None and person.role == "guest",
            )
            if found is not None:
                return found.ref
            removed = await was_removed(
                session, name=name, for_person=for_person, on_his_own=person is None
            )
        return trace.refuse("account_removed" if removed else "account_not_yours")

    def _out_refused(self, refusal: OutRefused, trace: _OutTrace, most: int) -> JsonObject:
        """What the program reads of a refusal of the gatekeeper: its code alone; ``too_slow``
        and ``too_big`` name the setting that caused them (``most``: what it was given to read)."""
        scope = self._scope
        code = refusal.refusal
        trace.refused = code
        if code == "too_slow":
            return _refused(code, setting=("out_seconds", scope.bound("out_seconds")))
        if code == "too_big":
            if most == scope.bound("out_bytes"):
                return _refused(code, setting=("out_bytes", most))
            return _refused(code, ANSWER_CEILING_WORDS)
        return _refused(code)

    def _read_back(self, response: OutResponse, trace: _OutTrace) -> JsonObject:
        """What the program reads of an answer: its status, its headers and its body, as text when
        it is UTF-8, else in base64. Outside content: data, never an instruction."""
        body = base64.b64decode(response.body_b64)
        trace.status, trace.bytes_in, trace.hops = response.status, len(body), response.hops
        trace.zone = response.zone
        answer: JsonObject = {
            "status": response.status,
            "headers": cast(JsonObject, dict(response.headers)),
        }
        try:
            answer["body"] = body.decode("utf-8")
        except UnicodeDecodeError:
            answer["body_b64"] = response.body_b64
        return {"ok": answer}

    async def _out_audited(self, trace: _OutTrace) -> None:
        """The second audit line of a request out (``OUT_DONE``)."""
        person = self._scope.for_whom.person
        begun = self._begun
        async with self._deps.session_factory() as session:
            await append_audit(
                session,
                actor=actor_label(self._scope.actor),
                action=OUT_DONE,
                details={
                    "tool": self._scope.tool_name,
                    "operation": None if begun is None else begun.name,
                    "for_person": None if person is None else str(person.id),
                    **trace.details(),
                },
            )
            await session.commit()
