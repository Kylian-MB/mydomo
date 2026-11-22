"""``agreement_keep`` and ``agreement_drop``: the agreements that last (spec 5.3, 2026-11-18).

A person may let an operation of one of Domo's tools that costs pass without being asked each
time: "order without asking me on Fridays, up to 40 euros" is ``{"tool": "t_order", "operation":
"order", "amount_cents_at_most": 4000, "times": 1, "per": "week", "weekdays": [5], "for": "me"}``.
The barrier's code reads it when a launch begins that operation
(``madetools.yes.agreement_covers``), never a model: within its amount, on its weekdays (ISO
numbers, Monday 1, in the household's zone), and its times per calendar day, ISO week or month. It
is kept for the one who keeps it (``me``), everyone's, a guest's too; for the house (``house``, the
launches Domo makes on his own) by the owner, or by Domo himself in a turn he acts in on his own
(the tool's own guard, ``_may_keep_for_the_house``; kept there by nobody, ``given_by`` empty: it is
his). In such a turn nobody asked: an agreement is the house's, and one for the person whose
conversation it is (said so) is refused. Who may not launch a tool may not keep one on it (the
owner closed it to them, or to Domo: ``not_possible``). ``agreement_drop`` withdraws one: one's
own, or the house's for the owner and for Domo on his own. Both are judged and shown like the
other actions; what they describe holds the tool's name, the operation and the bounds, never more.

The context of a turn lists the person's agreements, and the house's to the owner, as data
(``agreements_of``)."""

import uuid
from typing import Final, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.llm.types import JsonObject, ToolDefinition
from domo_core.madetools.models import OFFERED_PREFIX, MadeTool, StandingAgreement
from domo_core.madetools.risks import operations_of
from domo_core.madetools.service import tool_named
from domo_core.policy.actions import DescribedAction, FactValue
from domo_core.policy.catalog import MADE_PREFIX
from domo_core.policy.open_catalog import load_policy
from domo_core.policy.rights import load_rights
from domo_core.policy.roles import Actor
from domo_core.prompts import AgreementLine
from domo_core.tools.context import InvalidArguments, ToolContext, ToolOutcome

# The most an agreement lets through in one operation, in cents, and the most times per period.
MOST_CENTS: Final = 10_000_000
MOST_TIMES: Final = 1000
_PERIODS: Final = ("day", "week", "month")
_FOR: Final = ("me", "house")
_KEEP_KEYS: Final = frozenset(
    {"tool", "operation", "amount_cents_at_most", "times", "per", "weekdays", "for"}
)

_NO_TOOL: Final = "no tool of yours has this name"
_NO_OPERATION: Final = "this tool has no operation of this name"
_COSTS_NOTHING: Final = "this operation costs nothing: it needs no agreement"
_CLOSED: Final = "this tool is not open to the person: no agreement is kept on it. Nothing was done"
_HOUSE_IS_THE_OWNERS: Final = (
    "an agreement for the house is the owner's to keep or withdraw, or yours on your own. "
    "Nothing was done"
)
_NOT_YOURS: Final = "no agreement of theirs has this id"
_NOBODY_ASKED: Final = (
    "nobody asked you here, you act on your own: an agreement you keep here is the house's "
    "(for: house). Nothing was done"
)


class _Kept:
    """The arguments of an ``agreement_keep``, checked."""

    def __init__(self, arguments: JsonObject) -> None:
        if (
            not {"tool", "operation", "amount_cents_at_most", "times", "per"} <= set(arguments)
            or not set(arguments) <= _KEEP_KEYS
        ):
            raise InvalidArguments(
                "invalid",
                "the arguments are tool, operation, amount_cents_at_most, times, per, and "
                "weekdays and for",
            )
        tool, operation = arguments["tool"], arguments["operation"]
        if not isinstance(tool, str) or not isinstance(operation, str):
            raise InvalidArguments("invalid", "tool and operation are names")
        self.tool = tool.removeprefix(OFFERED_PREFIX)
        self.operation = operation
        self.amount = _whole(arguments["amount_cents_at_most"], MOST_CENTS, "amount_cents_at_most")
        self.times = _whole(arguments["times"], MOST_TIMES, "times")
        per = arguments["per"]
        if per not in _PERIODS:
            raise InvalidArguments("invalid", "per is day, week or month")
        self.per = cast(str, per)
        self.weekdays = _weekdays(arguments.get("weekdays"))
        whose = arguments.get("for")
        if whose is not None and whose not in _FOR:
            raise InvalidArguments("invalid", "for is me or house")
        self.for_house = whose == "house"
        # Said, not left to the default: in a turn of Domo's own, "me" is refused.
        self.for_me = whose == "me"


def _whole(value: object, most: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= most:
        raise InvalidArguments("invalid", f"{name} is a whole number from 1 to {most}")
    return value


def _weekdays(value: object) -> list[int] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise InvalidArguments("invalid", "weekdays is a list of days, Monday 1 to Sunday 7")
    days = cast(list[object], value)
    if (
        not days
        or any(
            isinstance(day, bool) or not isinstance(day, int) or not 1 <= day <= 7 for day in days
        )
        or len(set(cast(list[int], days))) != len(days)
    ):
        raise InvalidArguments(
            "invalid", "weekdays is a list of days, Monday 1 to Sunday 7, each once"
        )
    return sorted(cast(list[int], days))


def _may_keep_for_the_house(ctx: ToolContext) -> bool:
    """Whether this turn may keep or withdraw an agreement for the house: the owner's, and
    Domo's own in a turn he acts in on his own (``ToolContext.may_act_for_the_house``)."""
    return ctx.may_act_for_the_house


async def _may_launch(session: AsyncSession, ctx: ToolContext, name: str) -> bool:
    """Whether the person may launch the tool ``name`` (Domo himself in a turn he acts in on
    his own, ``ToolContext.turn_subject``; the owner may close a tool to someone): who may not
    launch it keeps no agreement on it."""
    policy = await load_policy(session, ctx.policy)
    rights = await load_rights(session)
    return rights.resolve(ctx.turn_subject, f"{MADE_PREFIX}{name}", policy).allowed


async def _keepable(session: AsyncSession, kept: _Kept, ctx: ToolContext) -> MadeTool:
    """The tool the agreement is on, once it may be kept (see the module docstring)."""
    if ctx.on_his_own:
        if kept.for_me:
            raise InvalidArguments("not_possible", _NOBODY_ASKED)
        # Nobody asked: it is the house's.
        kept.for_house = True
    if kept.for_house and not _may_keep_for_the_house(ctx):
        raise InvalidArguments("not_possible", _HOUSE_IS_THE_OWNERS)
    tool = await tool_named(session, kept.tool)
    if tool is None:
        raise InvalidArguments("not_found", _NO_TOOL)
    if not await _may_launch(session, ctx, tool.name):
        raise InvalidArguments("not_possible", _CLOSED)
    operation = operations_of(tool.operations).get(kept.operation)
    if operation is None:
        raise InvalidArguments("not_found", _NO_OPERATION)
    if not operation.risks.costs:
        raise InvalidArguments("invalid", _COSTS_NOTHING)
    return tool


def _kept_described(kept: _Kept, tool: MadeTool) -> DescribedAction:
    facts: dict[str, FactValue] = {
        "tool": tool.name,
        "operation": kept.operation,
        "amount_cents_at_most": kept.amount,
        "times": kept.times,
        "per": kept.per,
        "for_house": kept.for_house,
    }
    return DescribedAction(
        connector="core",
        action_type=AgreementKeep.action_type,
        target=None,
        effect="own_requests",
        destination="household",
        facts=facts,
    )


class AgreementKeep:
    """Keep an agreement that lasts (see the module docstring)."""

    definition = ToolDefinition(
        name="agreement_keep",
        description=(
            "Keep a standing agreement: an operation of one of your tools that costs money passes "
            "without asking each time, within its bounds (an amount in cents at most, a number of "
            "times per day, week or month, and the weekdays it may happen on, Monday 1 to Sunday "
            '7). For the person you talk with ("me"), or for the house ("house": the launches '
            "made for the house: the owner only, or you when you act on your own). The tool "
            "is yours and serves anyone; someone it is closed to keeps no agreement on it. For a "
            "person, only when they asked for it in their words."
        ),
        parameters={
            "type": "object",
            "properties": {
                "tool": {"type": "string", "description": "The tool (t_<name>)."},
                "operation": {"type": "string", "description": "The operation of the tool."},
                "amount_cents_at_most": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MOST_CENTS,
                    "description": "The most it may cost each time, in cents of euros.",
                },
                "times": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MOST_TIMES,
                    "description": "How many times per period at most.",
                },
                "per": {"type": "string", "enum": list(_PERIODS)},
                "weekdays": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 1, "maximum": 7},
                    "description": "The days it may happen on (Monday 1 to Sunday 7); any day "
                    "when left out.",
                },
                "for": {"type": "string", "enum": list(_FOR)},
            },
            "required": ["tool", "operation", "amount_cents_at_most", "times", "per"],
            "additionalProperties": False,
        },
    )
    action_type = "agreement.keep"

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        kept = _Kept(arguments)
        async with ctx.session_factory() as session:
            tool = await _keepable(session, kept, ctx)
        return _kept_described(kept, tool)

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        kept = _Kept(arguments)
        async with ctx.session_factory() as session:
            tool = await _keepable(session, kept, ctx)
            row = StandingAgreement(
                tool_id=tool.id,
                operation=kept.operation,
                for_person_id=None if kept.for_house else ctx.person.id,
                amount_cents_at_most=kept.amount,
                times=kept.times,
                per=kept.per,
                weekdays=kept.weekdays,
                # Domo's own, kept on his own: given by no person.
                given_by=None if ctx.on_his_own else ctx.person.id,
                created_at=ctx.clock(),
            )
            session.add(row)
            await session.flush()
            agreement_id = row.id
            await session.commit()
        return ToolOutcome(
            status="done",
            model_result={"agreement": str(agreement_id)},
            card={"key": "action.card.done.agreement.keep", "params": {}},
        )


def _drop_argument(arguments: JsonObject) -> uuid.UUID:
    if set(arguments) != {"id"}:
        raise InvalidArguments("invalid", 'the only argument is "id"')
    try:
        return uuid.UUID(str(arguments["id"]))
    except ValueError:
        raise InvalidArguments("invalid", "id is the id of an agreement") from None


async def _droppable(
    session: AsyncSession, agreement_id: uuid.UUID, ctx: ToolContext, *, lock: bool = False
) -> StandingAgreement:
    """The agreement ``agreement_id`` in force, when this turn may withdraw it: one's own, or the
    house's for the owner and for Domo on his own (who withdraws only the house's there)."""
    row = await session.get(StandingAgreement, agreement_id, with_for_update=lock)
    if row is None or row.withdrawn_at is not None:
        raise InvalidArguments("not_found", _NOT_YOURS)
    if row.for_person_id is None:
        if not _may_keep_for_the_house(ctx):
            raise InvalidArguments("not_possible", _HOUSE_IS_THE_OWNERS)
    elif row.for_person_id != ctx.person.id or ctx.on_his_own:
        raise InvalidArguments("not_found", _NOT_YOURS)
    return row


class AgreementDrop:
    """Withdraw an agreement that lasts (see the module docstring)."""

    definition = ToolDefinition(
        name="agreement_drop",
        description=(
            "Withdraw a standing agreement (agreement_keep), by its id: from then on that "
            "operation is asked again. The person's own, or the house's: the owner only, or you "
            "when you act on your own."
        ),
        parameters={
            "type": "object",
            "properties": {"id": {"type": "string", "description": "The agreement's id."}},
            "required": ["id"],
            "additionalProperties": False,
        },
    )
    action_type = "agreement.drop"

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        agreement_id = _drop_argument(arguments)
        async with ctx.session_factory() as session:
            row = await _droppable(session, agreement_id, ctx)
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=None,
            effect="own_requests",
            destination="household",
            facts={"for_house": row.for_person_id is None},
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        agreement_id = _drop_argument(arguments)
        async with ctx.session_factory() as session:
            row = await _droppable(session, agreement_id, ctx, lock=True)
            row.withdrawn_at = ctx.clock()
            await session.commit()
        return ToolOutcome(
            status="done",
            model_result={"withdrawn": str(agreement_id)},
            card={"key": "action.card.done.agreement.drop", "params": {}},
        )


async def agreements_of(
    session: AsyncSession, ctx_person_id: uuid.UUID, *, owner: bool
) -> tuple[AgreementLine, ...]:
    """The agreements in force of the person ``ctx_person_id``, and the house's when they are the
    owner, the oldest first: each as the context gives it (``prompts.AgreementLine``, data)."""
    whose = StandingAgreement.for_person_id == ctx_person_id
    if owner:
        whose = whose | StandingAgreement.for_person_id.is_(None)
    found = await session.execute(
        select(StandingAgreement, MadeTool.name)
        .join(MadeTool, MadeTool.id == StandingAgreement.tool_id)
        .where(whose, StandingAgreement.withdrawn_at.is_(None), MadeTool.state != "removed")
        .order_by(StandingAgreement.created_at, StandingAgreement.id)
    )
    return tuple(
        AgreementLine(
            id=str(row.id),
            tool=f"{OFFERED_PREFIX}{name}",
            operation=row.operation,
            amount_cents_at_most=row.amount_cents_at_most,
            times=row.times,
            per=row.per,
            weekdays=None if row.weekdays is None else tuple(row.weekdays),
            for_house=row.for_person_id is None,
        )
        for row, name in found.all()
    )
