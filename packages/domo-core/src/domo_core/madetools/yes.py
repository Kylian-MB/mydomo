"""The yes of a program's operation, and the agreements that last (spec 5.3, 5.8).

When a request of a running program needs a yes (``policy.program.judge_request``), nothing is
done: the run stops there (``stop``: a ``launch_stops`` row, with the arguments of the launch and
their digest), and Domo is woken with what was to be done. He asks, in his own words; the yes he
is given runs the launch again, once (``take_yes``): the same tool, for the same one, with the same
arguments, for that operation and up to its amount. A yes is never used twice.

An operation that costs passes without a yes when a standing agreement covers it
(``agreement_covers``): kept for that tool, that operation and the same one (a person, or the
house), not withdrawn, within its amount, on one of its weekdays (ISO numbers, Monday 1, in the
household's zone; any day without them), and fewer times than it allows over the current calendar
day, ISO week or month in the household's zone, counted on what was declared under it. It is read
under a row lock of the agreement, held until the caller commits what it declares
(``declare``): two launches at once under "once a week" pass once.

What a program says an operation costs is declared (``declare``: ``declared_spending``), apart
from what models cost; the owner's limits read it as ``spent`` (``spent_by``, ``policy.facts``)."""

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo

from domo_common.sandbox_api import JsonObject
from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from domo_core.madetools.models import (
    AgreementPer,
    DeclaredSpending,
    LaunchStop,
    StandingAgreement,
)
from domo_core.policy.asks import Ask
from domo_core.policy.program import Why
from domo_core.policy.rights import Approver

# Whose yes, from the least demanding to the most.
_APPROVERS: tuple[Approver, ...] = ("self", "adult", "owner")

if TYPE_CHECKING:
    from domo_core.madetools.means import LaunchScope

type Period = Literal["day", "week", "month"]


def digest_of(arguments: JsonObject) -> str:
    """The sha256 (hex) of ``arguments`` as canonical JSON: two launches with the same arguments
    have the same digest, whatever the order of their keys."""
    canonical = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def period_start(per: Period | AgreementPer, now: datetime, zone: ZoneInfo | None) -> datetime:
    """When the calendar ``per`` (day, ISO week from Monday, month) that holds ``now`` began, in
    the household's zone (UTC when it is not set)."""
    local = now.astimezone(zone or UTC)
    day = local.date()
    if per == "week":
        day -= timedelta(days=local.isoweekday() - 1)
    elif per == "month":
        day = day.replace(day=1)
    return datetime.combine(day, time(), tzinfo=zone or UTC)


def _for(
    column: InstrumentedAttribute[uuid.UUID | None], for_person_id: uuid.UUID | None
) -> ColumnElement[bool]:
    """That ``column`` is ``for_person_id``'s, or the house's (``NULL``) for ``None``."""
    return column.is_(None) if for_person_id is None else column == for_person_id


async def agreement_covers(
    session: AsyncSession,
    *,
    tool_id: uuid.UUID,
    operation: str,
    for_person_id: uuid.UUID | None,
    amount_cents: int,
    now: datetime,
    zone: ZoneInfo | None,
    leaving_out: uuid.UUID | None = None,
) -> uuid.UUID | None:
    """The standing agreement that covers this operation now (see the module docstring), the
    oldest first; ``None`` when none does. The agreements read are locked until the caller's
    transaction ends: what it declares under the one found counts for the next one asking.
    ``leaving_out`` is the declaration an earlier run of the same chain made for this very begin
    (``declare_again`` replaces it): it is not another use."""
    found = await session.scalars(
        select(StandingAgreement)
        .where(
            StandingAgreement.tool_id == tool_id,
            StandingAgreement.operation == operation,
            _for(StandingAgreement.for_person_id, for_person_id),
            StandingAgreement.withdrawn_at.is_(None),
            StandingAgreement.amount_cents_at_most >= amount_cents,
        )
        .order_by(StandingAgreement.created_at, StandingAgreement.id)
        .with_for_update()
    )
    weekday = now.astimezone(zone or UTC).isoweekday()
    for agreement in found.all():
        if agreement.weekdays is not None and weekday not in agreement.weekdays:
            continue
        since = period_start(agreement.per, now, zone)
        used = await session.scalar(
            select(func.count())
            .select_from(DeclaredSpending)
            .where(
                DeclaredSpending.agreement_id == agreement.id,
                DeclaredSpending.at >= since,
                *([] if leaving_out is None else [DeclaredSpending.id != leaving_out]),
            )
        )
        if (used or 0) < agreement.times:
            return agreement.id
    return None


async def stop(
    session: AsyncSession,
    scope: "LaunchScope",
    operation: str | None,
    why: Why,
    approver: Approver,
    ask: Ask,
    arguments: JsonObject,
    amount_cents: int | None,
    *,
    occurrence: int = 0,
    answered_before: Sequence[JsonObject] = (),
    declared: Mapping[str, JsonObject] | None = None,
) -> uuid.UUID:
    """The row of a launch stopped for a yes (``launch_stops``), for the tool, or the program run
    by hand, and the one of ``scope`` (``None`` for the house), with the yeses the stopped run was
    given (``answered_before``, each at its place) and the money it had declared, by begin
    (``declared``: ``"<operation>#<occurrence>"`` → ``{"id", "cents"}``); ``occurrence`` is which
    begin of ``operation`` the run was in (from 1; 0 before any). Flushed, its id returned."""
    if scope.tool_id is None and scope.hand_run_id is None:
        raise ValueError("only the launch of a tool or of a program run by hand stops for a yes")
    person = scope.for_whom.person
    row = LaunchStop(
        tool_id=scope.tool_id,
        hand_run_id=None if scope.tool_id is not None else scope.hand_run_id,
        for_person_id=None if person is None else person.id,
        operation=operation,
        occurrence=occurrence,
        amount_cents=amount_cents,
        why=why,
        approver=approver,
        ask=ask,
        arguments=dict(arguments),
        arguments_digest=digest_of(arguments),
        answered_before=list(answered_before),
        declared=dict(declared or {}),
    )
    session.add(row)
    await session.flush()
    return row.id


async def stop_named(
    session: AsyncSession, stop_id: uuid.UUID, *, tool_id: uuid.UUID | None
) -> LaunchStop | None:
    """The stop ``stop_id`` of the tool ``tool_id`` (``None``: of a program run by hand) whose yes
    is not used yet; ``None`` otherwise."""
    row = await session.get(LaunchStop, stop_id, populate_existing=True)
    if row is None or row.tool_id != tool_id or row.yes_used_at is not None:
        return None
    return row


async def take_yes(
    session: AsyncSession,
    stop_id: uuid.UUID,
    *,
    tool_id: uuid.UUID | None,
    for_person_id: uuid.UUID | None,
    arguments: JsonObject,
    given_by: Approver,
    now: datetime | None = None,
) -> LaunchStop | None:
    """The yes given for the stop ``stop_id``, taken once (``yes_used_at`` set, flushed): ``None``
    when it was taken already, or when the stop is of another tool (``tool_id``; ``None``: a
    program run by hand), for someone else, or with other arguments. ``given_by`` is who really
    gave it, as an approver: kept on the stop (``yes_approver``), never more than the stop asked,
    and what the relaunch carries. The row is locked until the caller commits."""
    row = await session.get(LaunchStop, stop_id, with_for_update=True, populate_existing=True)
    if (
        row is None
        or row.yes_used_at is not None
        or row.tool_id != tool_id
        or row.for_person_id != for_person_id
        or row.arguments_digest != digest_of(arguments)
    ):
        return None
    row.yes_used_at = now or datetime.now(UTC)
    asked = _APPROVERS.index(row.approver)
    row.yes_approver = _APPROVERS[min(asked, _APPROVERS.index(given_by))]
    await session.flush()
    return row


async def declare(
    session: AsyncSession,
    *,
    for_person_id: uuid.UUID | None,
    tool_id: uuid.UUID | None,
    operation: str,
    amount_cents: int,
    agreement_id: uuid.UUID | None,
    at: datetime,
) -> uuid.UUID | None:
    """Keep what a program said an operation let through costs (``declared_spending``), of the
    tool ``tool_id`` (``None``: a program run by hand), with the agreement it passed under
    (``None``: a yes, or for the house); its id. Nothing for nothing spent. Flushed."""
    if amount_cents <= 0:
        return None
    row = DeclaredSpending(
        for_person_id=for_person_id,
        tool_id=tool_id,
        operation=operation,
        amount_cents=amount_cents,
        agreement_id=agreement_id,
        at=at,
    )
    session.add(row)
    await session.flush()
    return row.id


async def carried_over(
    session: AsyncSession, declaration: DeclaredSpending, now: datetime, zone: ZoneInfo | None
) -> bool:
    """Whether ``declaration`` passed under a standing agreement in an earlier period of it (its
    day, week or month) than the one that holds ``now``: let through now, it is a use of this
    period, to be read under the agreements again like any other."""
    if declaration.agreement_id is None:
        return False
    agreement = await session.get(StandingAgreement, declaration.agreement_id)
    return agreement is not None and declaration.at < period_start(agreement.per, now, zone)


async def declare_again(
    session: AsyncSession,
    declaration_id: uuid.UUID,
    *,
    amount_cents: int,
    agreement_id: uuid.UUID | None,
    at: datetime,
) -> None:
    """The declaration an earlier run made for a begin, as a relaunch lets that begin through:
    what it costs now (lower, or more after its own yes), what it passes under now, and counted
    in the period it is spent in. The ledger holds, for each begin, what was really let through
    last, once. Flushed."""
    row = await session.get(DeclaredSpending, declaration_id, with_for_update=True)
    if row is not None:
        row.amount_cents = amount_cents
        row.agreement_id = agreement_id
        row.at = at
        await session.flush()


async def spent_by(
    session: AsyncSession,
    for_person_id: uuid.UUID | None,
    now: datetime,
    zone: ZoneInfo | None,
    *,
    leaving_out: uuid.UUID | None = None,
) -> dict[str, int]:
    """What programs declared spending for ``for_person_id`` (``None``: the house), in cents, over
    the current calendar day, ISO week and month in the household's zone; without the declaration
    ``leaving_out`` (a relaunch's begin whose money it is: counted once, as the begin's amount)."""
    starts = {per: period_start(per, now, zone) for per in ("day", "week", "month")}
    amount = DeclaredSpending.amount_cents
    at = DeclaredSpending.at
    row = (
        await session.execute(
            select(
                func.coalesce(func.sum(amount).filter(at >= starts["day"]), 0),
                func.coalesce(func.sum(amount).filter(at >= starts["week"]), 0),
                func.coalesce(func.sum(amount).filter(at >= starts["month"]), 0),
            ).where(
                _for(DeclaredSpending.for_person_id, for_person_id),
                at >= min(starts.values()),
                *([] if leaving_out is None else [DeclaredSpending.id != leaving_out]),
            )
        )
    ).one()
    day, week, month = (int(value) for value in row)
    return {"day": day, "week": week, "month": month}
