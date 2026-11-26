"""The tools Domo made, in a turn (spec 5.2): each ready tool is Domo's and is offered to the
model in anyone's turn as any tool of the core, under the name ``t_<name>``, and does the action
``made.<name>``, launched for the person of the turn (for the house in a turn Domo acts in on his
own, ``ToolContext.on_his_own``).

``offers`` makes them for a turn (``conversation.prepare.prepare_turn`` lays them over the registry
of the code): ``tools_offered`` at most (a setting of Domo's), the tool a plan due in the turn is
told is ready first, then the most recently launched or made. Who may use one is the barrier's to
say, as for any tool (``tools_for``: the kind's default roles, every role for a tool Domo made,
and the owner's rights, who may close a tool to someone); ``madetools.service.ready_tools`` gives
every ready tool. A tool past the ones offered (``tool_find`` names it) is found when the model
calls it (``offer_named``, ``ToolContext.made_tool_named``) and goes the same way as an offered one.

A call goes through the barrier as any call (``conversation.execution.handle_tool_call``), but is
never shown and never judged (``made.*``: ``policy.catalog.is_silent``, ``needs_the_judge``); it
runs in the sandbox (``launch.launch``, ``cause="turn"``), as long as its ``run_seconds``: the tool
bounds its own time (``BoundsItsOwnSteps``), handing a long run over to the background
(``handover.launch_in_a_turn``). What its program asks while it runs is answered then
(``madetools.means``); the model reads its result.

A launch that stopped for a yes (``madetools.yes``) is run again by the same tool called with the
reserved argument ``yes_for`` alone (``{"yes_for": "<stop id>"}``: the stop's own arguments are
used, any other argument is refused). Such a call is described as ``made.<name>`` with the facts of
the yes (``{"operation", "amount_cents", "yes": true, "approver"}``): it waits for the stop's
approver, asking what the stop asks, why being what the operation says (``AsksItsYes.waits_for``,
``conversation.execution.handle_tool_call``), and is stored and shown as any request that waits
(``policy.catalog.shown``). Only the person's own plain yes for a cost (``their_own_cost``: within
their limit, which the code applied before the stop existed) is weighed by the yes-judge first,
with what it costs and what they spent before of the kind (``cost_of``): a usual one runs at once,
and the assistant tells them after (spec part 12). The yes runs the stored call, which takes the yes
once (``yes.take_yes``) and launches with it. In a person's turn, a yes is for the person of the
turn, never for a stop of the house's. A stop of the house's is carried in a turn Domo acts in on
his own (Task 9, spec 5.8): the one he answers himself (``domo_answers``: its cost, a plain yes
asked of no one else) runs at once, nobody asked, and its money is declared for the house; any
other (an adult's or the owner's agreement, an owner's rule, a proof) is never his to give: it
waits as his own request, in the owner's conversation, for the adult or the owner it asks, whose
yes runs it for the house (``_given_for_the_house``). A program Domo ran by hand
(``tools.program_run``) carries the yes of its stops through these same rules (``carried_stop``,
``yes_facts``, ``waits_for_its_yes``, ``yes_taken``).

``set_aside_for`` gives the context of a turn the tools set aside that the person asked for (a
build they opened made them), with why: they are no longer offered, and Domo knows why one is
gone; and the builds they opened that were abandoned lately, with why, so that Domo does the
request another way and learns from it. ``being_built_for`` gives it the builds they opened that
are still in progress: what each is for, since when, and how many requests wait for it, so that
Domo opens one build per tool and judges when a wait is no longer reasonable (spec 5.5, 5.8).
"""

import re
import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Final, Literal, cast
from zoneinfo import ZoneInfo

from domo_common.sandbox_api import JsonObject
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.db import SessionFactory
from domo_core.llm.types import ToolDefinition
from domo_core.madetools.handover import AskingTool, launch_in_a_turn, stop_of
from domo_core.madetools.launch import (
    LaunchDeps,
    Launched,
    action_type_of,
    described,
)
from domo_core.madetools.models import (
    NAME_PATTERN,
    OFFERED_PREFIX,
    LaunchStop,
    MadeTool,
    StandingAgreement,
    ToolBuild,
)
from domo_core.madetools.risks import operations_of
from domo_core.madetools.schema import schema_problem, unsupported
from domo_core.madetools.service import IN_PROGRESS, ready_tools
from domo_core.madetools.yes import (
    agreements_of,
    spent_like,
    stop_named,
    take_yes,
    used_in_its_period,
)
from domo_core.plans.models import Plan
from domo_core.plans.schedule import as_local
from domo_core.policy.actions import DescribedAction, FactValue
from domo_core.policy.asks import Asked, AskedBy
from domo_core.policy.facts import STOPPED_FOR_A_YES
from domo_core.policy.rights import Approver
from domo_core.policy.roles import Actor
from domo_core.policy.settings import load_policy_settings
from domo_core.prompts import BeingBuilt, ToolAside
from domo_core.tools.context import (
    InternalTool,
    InvalidArguments,
    ToolContext,
    ToolOutcome,
    WaitsForAYes,
)

# How many tools set aside the context of a turn names at most, the latest first (and as many
# builds abandoned).
MAX_ASIDE_SHOWN: Final = 10
# How long a build abandoned is named in the context of the one who opened it.
ABANDONED_SHOWN_FOR: Final = timedelta(hours=24)
# How many builds in progress the context names at most, the oldest first, and how much of what
# each one is for.
MAX_BUILDING_SHOWN: Final = 10
PURPOSE_SHOWN: Final = 120

# What Domo reads of a run that goes on after the turn stopped waiting for it.
RUNNING_DETAIL: Final = (
    "the tool is still running: answer the person now; you will be woken with what it gives "
    "when it ends"
)
# The reserved argument of a call that carries the yes of a stop.
YES_FOR: Final = "yes_for"
# What Domo reads with a launch that stopped for a yes (fix round 1 of Task 12, R20): the call
# with ``yes_for`` is itself the ask, and waits for the approver's yes; it is never put off until
# the person agreed. A tool's own description is its maker's words, so this is told here.
STOPPED_NEXT: Final = (
    "call this tool again at once with yes_for alone (the stop): that call is the ask, and waits "
    "for the yes, unless a cost of theirs is usual for them: then it runs at once (told_after: "
    "tell them what was spent). Else in the same reply tell the person in your words what is to be "
    "done and at what cost; their yes, however they give it, runs it. On your own for the house, "
    "the cost is yours to judge: your yes_for runs it at once"
)
# Who asked the yes of a stop, by why it stopped (``policy.asks.Asked.by``).
_ASKED_BY: Final[dict[str, AskedBy]] = {
    "costs": "domo",
    "owner_rule": "rule",
    "approval": "approver",
}
_YES_ALONE: Final = (
    "yes_for is given alone: the arguments of the launch that stopped are the ones used"
)
_NO_SUCH_YES: Final = (
    "no launch of this tool waits for this yes (it was given already, or is someone else's): "
    "launch the tool again"
)
_SILENT_CARD: Final[JsonObject] = {"key": "action.card.done", "params": {}}
# The card of a call whose launch stopped for a yes: the call went as far as it could, and the
# tool's work is not done. Its mark keeps it out of the person's history of the tool
# (``policy.facts``): one request counts once, when a launch of it ends, however many stops it
# went through.
_STOPPED_CARD: Final[JsonObject] = {**_SILENT_CARD, "launch": STOPPED_FOR_A_YES}
_FAILED_CARD: Final[JsonObject] = {"key": "action.card.failed", "params": {}}
_LONG_AGO: Final = datetime.min.replace(tzinfo=UTC)


def offered_name(name: str) -> str:
    """The name the tool ``name`` is offered to the model under (``t_<name>``)."""
    return f"{OFFERED_PREFIX}{name}"


class MadeToolOffer:
    """One tool Domo made, as a tool of the turn (``tools.context.InternalTool``). It keeps what
    it needs of the tool's row, never the row itself (the turn's session is closed)."""

    bounds_its_own_steps: Literal[True] = True

    def __init__(self, tool: MadeTool, deps: LaunchDeps) -> None:
        self._id: uuid.UUID = tool.id
        self._asking = AskingTool.of(tool)
        self._parameters: dict[str, object] = dict(tool.parameters)
        self._deps = deps
        self._definition = ToolDefinition(
            name=offered_name(tool.name),
            description=tool.description,
            parameters=dict(tool.parameters),
        )
        self._action_type = action_type_of(tool.name)
        self._name = tool.name
        # What each operation says (Domo's own words when he built it): why a yes is asked.
        self._operations: dict[str, str] = {
            name: operation.says for name, operation in operations_of(tool.operations).items()
        }

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    @property
    def action_type(self) -> str:
        return self._action_type

    @property
    def tool_id(self) -> uuid.UUID:
        """The id of the tool's row."""
        return self._id

    def _checked(self, arguments: JsonObject) -> None:
        if unsupported(self._parameters):
            # Parameters the core cannot check are the tool's fault: the launch says so.
            return
        problem = schema_problem(self._parameters, arguments)
        if problem is not None:
            # A mistake of the model's form, never a value of the arguments.
            raise InvalidArguments("invalid", problem)

    async def _yes_of(self, arguments: JsonObject, ctx: ToolContext) -> LaunchStop | None:
        """The stop of this tool whose yes the call carries (``carried_stop``)."""
        return await carried_stop(arguments, ctx, tool_id=self._id, missing=_NO_SUCH_YES)

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        found = await self._yes_of(arguments, ctx)
        if found is None:
            self._checked(arguments)
            # For the house in a turn Domo acts in on his own (``execute``).
            return described(self._name, for_house=ctx.on_his_own)
        return described(self._name, yes_facts(found), for_house=found.for_person_id is None)

    async def waits_for(self, arguments: JsonObject, ctx: ToolContext) -> WaitsForAYes | None:
        """What the call waits for (``AsksItsYes``, ``waits_for_its_yes``)."""
        found = await self._yes_of(arguments, ctx)
        if found is None:
            return None
        says = self._operations.get(found.operation or "")
        return await waits_for_its_yes(found, ctx, says, tool=self._name, missing=_NO_SUCH_YES)

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        found = await self._yes_of(arguments, ctx)
        yes: LaunchStop | None = None
        # For the person of the turn; for the house in a turn Domo acts in on his own.
        for_person: Person | None = None if ctx.on_his_own else ctx.person
        if found is None:
            self._checked(arguments)
        else:
            for_person = None if found.for_person_id is None else ctx.person
            # The yes, taken once, for the stop's own arguments.
            arguments = cast(JsonObject, dict(found.arguments))
            yes = await yes_taken(found, ctx, tool_id=self._id, missing=_NO_SUCH_YES)
        # The launch, its program's requests answered while it runs, waited for a while: the
        # turn reads what came of it, or that it goes on and Domo will be woken with it.
        launched = await launch_in_a_turn(
            self._deps,
            self._asking,
            arguments=arguments,
            for_person=for_person,
            yes=yes,
            # A turn of his own judged it as his; a yes the owner gave was judged as theirs.
            judged_as_domo=ctx.on_his_own and for_person is None,
        )
        return outcome_of(launched)


def domo_answers(stop: LaunchStop) -> bool:
    """Whether Domo gives the yes of ``stop`` himself, in a turn of his own: a stop of the
    house's that asks only his own yes (``self``, for the house: spec 5.8), for its cost, a plain
    yes. Never an adult's or the owner's agreement, an owner's rule, nor a proof: he never counts
    as the adult or the owner who agrees, and gives no proof."""
    return (
        stop.for_person_id is None
        and stop.approver == "self"
        and stop.why == "costs"
        and stop.ask == "yes"
    )


async def carried_stop(
    arguments: JsonObject, ctx: ToolContext, *, tool_id: uuid.UUID | None, missing: str
) -> LaunchStop | None:
    """The stop whose yes a call carries (``yes_for``, given alone), when it carries one; its yes
    not used yet, of the tool ``tool_id`` (``None``: of a program run by hand,
    ``tools.program_run``), for the person of the turn (never in a turn Domo acts in on his
    own), or the house's (who may carry it is ``waits_for_its_yes``'s and ``yes_taken``'s to
    say). ``InvalidArguments`` otherwise (``missing`` says what to do, the same whether the stop
    exists or not)."""
    if YES_FOR not in arguments:
        return None
    if set(arguments) != {YES_FOR}:
        raise InvalidArguments("invalid", _YES_ALONE)
    try:
        stop_id = uuid.UUID(str(arguments[YES_FOR]))
    except ValueError:
        raise InvalidArguments("invalid", "yes_for is the id of a stop") from None
    async with ctx.session_factory() as session:
        found = await stop_named(session, stop_id, tool_id=tool_id)
    if found is None or (
        found.for_person_id is not None and (found.for_person_id != ctx.person.id or ctx.on_his_own)
    ):
        raise InvalidArguments("not_found", missing)
    return found


def yes_facts(stop: LaunchStop) -> dict[str, FactValue]:
    """The facts of a call that carries the yes of ``stop`` (``catalog.MADE_FACTS``): the
    operation, its amount, that it carries a yes, and whose."""
    return {
        "operation": stop.operation,
        "amount_cents": stop.amount_cents,
        "yes": True,
        "approver": stop.approver,
    }


def their_own_cost(stop: LaunchStop) -> bool:
    """Whether the yes of ``stop`` is a person's own plain yes for its cost (``self``, for them):
    the one the yes-judge may weigh first, within their limit (spec part 12). Never an adult's or
    the owner's agreement, an owner's rule, nor a proof; never the house's."""
    return (
        stop.for_person_id is not None
        and stop.approver == "self"
        and stop.why == "costs"
        and stop.ask == "yes"
    )


async def cost_of(stop: LaunchStop, ctx: ToolContext, tool: str | None) -> JsonObject:
    """A person's cost, as the yes-judge weighs it: the amount, the tool (``None``: a program run
    by hand), the operation, their own agreements on the tool that still count (each with how
    many of its times were used in its period of now, ``used``) and their own
    last spendings of the same operation (each its ``day`` and ``amount_cents``), never anyone
    else's. A program run by hand has neither: no habit, no agreement (ruling R63)."""
    person_id = stop.for_person_id
    if person_id is None:
        raise ValueError("a cost of the house's is never weighed by the judge")
    spent: list[tuple[date, int]] = []
    agreements: list[tuple[StandingAgreement, int]] = []
    if stop.tool_id is not None:
        # A program run by hand has no habit (ruling R63): another program may have named its
        # operation the same, and a new one could borrow that name; its cost passes only on the
        # person's own words.
        async with ctx.session_factory() as session:
            spent = await spent_like(
                session, person_id=person_id, tool_id=stop.tool_id, operation=stop.operation or ""
            )
            zone = (await load_policy_settings(session)).time_zone
            now = ctx.clock()
            agreements = [
                (agreement, await used_in_its_period(session, agreement, now, zone))
                for agreement in await agreements_of(
                    session, person_id=person_id, tool_id=stop.tool_id
                )
            ]
    return {
        "amount_cents": stop.amount_cents,
        "tool": tool,
        "operation": stop.operation,
        "agreements": [
            {
                "operation": agreement.operation,
                "amount_cents_at_most": agreement.amount_cents_at_most,
                "times": agreement.times,
                "per": agreement.per,
                "weekdays": None if agreement.weekdays is None else list(agreement.weekdays),
                # How many of its times were used in its period of now.
                "used": used,
            }
            for agreement, used in agreements
        ],
        "spent_before": [{"day": day.isoformat(), "amount_cents": amount} for day, amount in spent],
    }


async def waits_for_its_yes(
    stop: LaunchStop, ctx: ToolContext, says: str | None, *, tool: str | None, missing: str
) -> WaitsForAYes | None:
    """What a call that carries the yes of ``stop`` waits for (``AsksItsYes``): the stop's
    approver, asked what it asks, why being what the operation says (``says``). A stop of the
    house's is carried only in a turn Domo acts in on his own: the one he answers himself waits
    for nobody (``None``: it runs at once); any other waits, as his own request
    (``Confirmation.by_domo``), for the adult or the owner it asks: an adult's or the owner's
    agreement is never Domo's, nor a proof. A person's own yes for a cost
    (``their_own_cost``) is weighed by the judge first (``judge_first``), with what it costs
    (``cost_of``, of the tool ``tool``)."""
    if stop.for_person_id is None:
        if not ctx.on_his_own:
            # Not his turn: a person's yes never answers a stop of the house's there.
            raise InvalidArguments("not_found", missing)
        if domo_answers(stop):
            return None
    # Asked of an adult or the owner: an approval, whatever brought it (never the requester's own
    # yes, ``confirmations.service._the_requesters_own_yes``).
    by = "approver" if stop.approver in ("adult", "owner") else _ASKED_BY[stop.why]
    asked = Asked(stop.ask, by, says)
    if their_own_cost(stop):
        cost = await cost_of(stop, ctx, tool)
        return WaitsForAYes(approver=stop.approver, asked=asked, judge_first=True, cost=cost)
    return WaitsForAYes(approver=stop.approver, asked=asked)


async def yes_taken(
    stop: LaunchStop, ctx: ToolContext, *, tool_id: uuid.UUID | None, missing: str
) -> LaunchStop:
    """The yes of ``stop``, taken once (``yes.take_yes``) for its own arguments, as given by who
    really gave it: for a stop of the house's, Domo himself or the adult or owner who said yes
    (``_given_for_the_house``); for a person's, them or the approver who said yes (``_given_by``).
    ``InvalidArguments`` (``missing``) when nobody who may give it did, or it was taken
    already."""
    yes: LaunchStop | None = None
    async with ctx.session_factory() as session:
        given = (
            await _given_for_the_house(session, ctx, stop)
            if stop.for_person_id is None
            else await _given_by(session, ctx, stop.approver)
        )
        if given is not None:
            yes = await take_yes(
                session,
                stop.id,
                tool_id=tool_id,
                for_person_id=stop.for_person_id,
                arguments=cast(JsonObject, dict(stop.arguments)),
                given_by=given,
                now=ctx.clock(),
            )
        await session.commit()
    if yes is None:
        raise InvalidArguments("not_found", missing)
    return yes


async def _given_for_the_house(
    session: AsyncSession, ctx: ToolContext, stop: LaunchStop
) -> Approver | None:
    """Who really gives the yes of ``stop``, a stop of the house's, as an approver: Domo
    himself in a turn of his own, for the one he answers (``domo_answers``: ``self``); after a
    yes a person gave to the request that carried it (``ctx.approved_by``: it waited in the
    owner's conversation), that person, as their role makes them an approver (the owner, an
    adult). ``None``: nobody who may give it (nothing is taken)."""
    decider_id = ctx.approved_by
    if decider_id is None:
        return "self" if ctx.on_his_own and domo_answers(stop) else None
    decider = await session.get(Person, decider_id)
    if decider is None:
        return None
    if decider.role == "owner":
        return "owner"
    if decider.role == "adult":
        return "adult"
    return None


async def _given_by(session: AsyncSession, ctx: ToolContext, asked: Approver) -> Approver:
    """Who really gave the yes being run, as an approver: someone else than the requester who
    may approve as the stop asks (``confirmations.store.approves``: an adult, the owner) gives
    ``asked``; the requester themselves, or anyone else, gives ``self``."""
    # Here: the confirmations' store imports the tools' context, as this module does.
    from domo_core.confirmations.store import approves

    decider_id = ctx.approved_by
    if decider_id is None or decider_id == ctx.person.id or asked == "self":
        return "self"
    decider = await session.get(Person, decider_id)
    if decider is None or not approves(asked, decider, ctx.person.id):
        return "self"
    return asked


def outcome_of(launched: Launched) -> ToolOutcome:
    """What the model reads of a launch in a turn (``"launch"``: how it ended, and ``"result"``),
    with a card nobody is shown (``made.*`` is silent). A launch that stopped for a yes is no
    failure (the call, and the yes that ran it, went as far as they could: ``"launch":
    "stopped"``, and what it waits for in its result) and no use of the tool either: its card says
    so (``_STOPPED_CARD``). One that stopped with nothing to ask is a failure."""
    told: JsonObject = {"launch": launched.status, "result": launched.result}
    if launched.status == "running":
        told["detail"] = RUNNING_DETAIL
    if launched.status in ("done", "running"):
        return ToolOutcome(status="done", model_result=told, card=_SILENT_CARD)
    if launched.status == "stopped" and stop_of(launched.result) is not None:
        return ToolOutcome(
            status="done", model_result={**told, "next": STOPPED_NEXT}, card=_STOPPED_CARD
        )
    return ToolOutcome(status="failed", model_result=told, card=_FAILED_CARD)


def offers(
    tools: Sequence[MadeTool], deps: LaunchDeps, *, most: int, first: Iterable[str] = ()
) -> dict[str, InternalTool]:
    """The tools of a turn among ``tools`` (every ready one), by the name the model calls them:
    ``most`` at most (the ``tools_offered`` setting; the others wait for ``tool_find``); the
    tools named ``first`` (by their name of the household: the tool a
    plan due in the turn is told is ready), then the most recently launched or made (a new tool,
    or one just changed, is at the top), then the newest."""
    wanted = frozenset(first)
    ordered = sorted(
        tools,
        key=lambda tool: (
            tool.name in wanted,
            max(tool.last_launched_at or _LONG_AGO, tool.changed_at or _LONG_AGO),
            tool.created_at or _LONG_AGO,
            tool.name,
        ),
        reverse=True,
    )
    return {offered_name(tool.name): MadeToolOffer(tool, deps) for tool in ordered[:most]}


async def offer_named(
    session_factory: SessionFactory, deps: LaunchDeps, name: str
) -> InternalTool | None:
    """The tool the model calls ``name`` (``t_<name>``) in a turn that did not offer it (past
    ``tools_offered``, ``tool_find`` names it): the same tool an offer would be, when it is a ready
    tool (the barrier says, as for any call, whether the person may use it); ``None`` otherwise
    (set aside, removed, no tool)."""
    bare = name.removeprefix(OFFERED_PREFIX)
    if bare == name or re.fullmatch(NAME_PATTERN, bare) is None:
        return None
    async with session_factory() as session:
        found = [tool for tool in await ready_tools(session) if tool.name == bare]
        return MadeToolOffer(found[0], deps) if found else None


async def set_aside_for(
    session: AsyncSession, person: Person, now: datetime
) -> tuple[ToolAside, ...]:
    """The tools set aside that ``person`` asked for (a build they opened made them), the latest
    first, ``MAX_ASIDE_SHOWN`` at most: each by the name it was offered under,
    and why it was set aside. Then the builds they opened that were abandoned in the
    ``ABANDONED_SHOWN_FOR`` before ``now`` (the turn's time), the latest first,
    ``MAX_ASIDE_SHOWN`` at most (``ToolAside.abandoned``): each by the name the tool would have
    had, and the build's outcome; a build abandoned because its tool was removed is left out
    (someone chose it)."""
    asked = select(ToolBuild.tool_id).where(
        ToolBuild.opened_by == person.id, ToolBuild.tool_id.is_not(None)
    )
    found = await session.execute(
        select(MadeTool.name, MadeTool.aside_reason)
        .where(
            MadeTool.state == "set_aside",
            MadeTool.id.in_(asked),
        )
        .order_by(MadeTool.changed_at.desc(), MadeTool.name)
        .limit(MAX_ASIDE_SHOWN)
    )
    aside = [
        ToolAside(name=offered_name(name), reason=reason or "") for name, reason in found.all()
    ]
    since = now - ABANDONED_SHOWN_FOR
    removed = select(MadeTool.id).where(MadeTool.state == "removed")
    gave_up = await session.execute(
        select(ToolBuild.name, ToolBuild.outcome)
        .where(
            ToolBuild.opened_by == person.id,
            ToolBuild.state == "abandoned",
            ToolBuild.last_progress_at >= since,
            or_(ToolBuild.tool_id.is_(None), ToolBuild.tool_id.not_in(removed)),
        )
        .order_by(ToolBuild.last_progress_at.desc(), ToolBuild.name)
        .limit(MAX_ASIDE_SHOWN)
    )
    aside.extend(
        ToolAside(name=offered_name(name), reason=outcome or "", abandoned=True)
        for name, outcome in gave_up.all()
    )
    return tuple(aside)


async def being_built_for(
    session: AsyncSession, person: Person, zone: ZoneInfo | None
) -> tuple[BeingBuilt, ...]:
    """The builds ``person`` opened that are in progress (waiting or building), the oldest first,
    ``MAX_BUILDING_SHOWN`` at most: each by the name its tool will have, what it is for
    (``PURPOSE_SHOWN`` characters at most), since when (on the household's clock, ``zone``) and how
    many plans wait for it."""
    waiting = (
        select(func.count())
        .select_from(Plan)
        .where(Plan.after_build_id == ToolBuild.id, Plan.state == "waiting")
        .scalar_subquery()
    )
    found = await session.execute(
        select(ToolBuild.name, ToolBuild.purpose, ToolBuild.created_at, waiting)
        .where(ToolBuild.opened_by == person.id, ToolBuild.state.in_(IN_PROGRESS))
        .order_by(ToolBuild.created_at, ToolBuild.name)
        .limit(MAX_BUILDING_SHOWN)
    )
    return tuple(
        BeingBuilt(
            name=name,
            purpose=purpose[:PURPOSE_SHOWN],
            since=as_local(created_at, zone) or "",
            waiting=count,
        )
        for name, purpose, created_at, count in found.all()
    )
