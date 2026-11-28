"""The account of Domo: what each call to a model costs, reserved before the call is made, and
the firm limits it never passes (spec 4.7, 4.8).

Every call is counted in ``model_calls`` (``llm.metered.MeteredModel`` does it for each call):

1. before the call, ``reserve`` takes the lock of its key (``pg_advisory_xact_lock(SPEND_LOCK,
   hashtext(<key id>))``, held until the caller's transaction ends), then, for each limit of the
   key that applies (the whole key's, the person's, and the ones of the role the person has
   now), compares what was spent in the limit's period, plus the most the call can cost, with the
   limit. Past it, the call is refused
   (``LimitReached``: it is not made); otherwise a ``reserved`` row of that maximum is added. Two
   calls at once on one key are so checked one after the other, and the second sees the first's
   reservation;
2. after the call, ``settle`` replaces the maximum with what it cost: from the tokens the provider
   reported, else from the length of the texts (``estimated``); a call that failed before any
   answer costs nothing;
3. a reservation that was never settled (the core stopped in the middle of the call, say) is
   settled at its maximum once it is ``RESERVATION_TTL`` old (``settle_stale``, a pass of the
   core's upkeep).

A key is at its limit for a request (``at_limit``) when it has no margin left for the request's
first call: the same comparison as ``reserve``, with the most that call can cost. The choice of
the key (``router.choose_key``) then passes it over for the next key the person may use.

A limit may be for a role (``adult``, ``child`` or ``guest``) instead of a person, now and
later: it holds for whoever has the role at the time of the call. Its ``split`` says how: ``each``
(every one of them has the amount: it counts what that person spent) or ``together`` (the amount
is shared: it counts what all who have the role spent). Both splits of one role may be on one key,
and every limit that applies holds at once; the lock of the key covers them all. Someone who left
the household keeps their role: what they spent in the period was spent, so it still counts for the
role together, and they may be the one of it who spent the most (``limit_notes``).

What is spent counts the costs settled and the maxima still reserved (``spent_usd``). A limit in
euros is converted at the latest rate Domo read (``exchange_rates``), else at the catalog's
(``usd_per``). A period starts at midnight in the household's time zone (in UTC without one): the
day, the Monday, the first of the month (``period_start``). Amounts are counted in millionths of
a dollar, rounded up.

Before the end, the code warns nobody of itself: the person who set a limit is told where it
stands (``limit_notes``: the share spent, for ``each`` the largest share among the ones of the
role and whose it is), and Domo judges whether to speak of it. A threshold
is told once in the period, when it is passed: the one of the person who set the limit
(``warn_at_percent``, marked ``warned_at``) before the one Domo gave himself on the limit's key
(``choices.warn_at_percent``, the key's own choice else the household's, marked
``domo_warned_at``); each is told once, whether the other was or not.

A limit is set, replaced or removed by ``set_spending_limit`` (it replaces the limit of the same
people: the same key, person, role and split), the one way both tools that write one go
(``key_limit`` on the person's own key, ``keys_house`` on the general key), audited
``spending.limit_set``, or ``spending.limit_removed`` when it is taken away.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from typing import Final, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import ColumnElement, and_, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from domo_core.accounts.models import Person
from domo_core.audit.log import append_audit
from domo_core.llm.catalog import Catalog, Prices
from domo_core.llm.costs import cost_usd, estimate_tokens, speech_cost_usd
from domo_core.llm.models import Provider
from domo_core.llm.router import Route, key_label
from domo_core.llm.speech.catalog import SpeechPrices
from domo_core.llm.spending_models import ExchangeRate, ModelCall, SpendingLimit
from domo_core.llm.types import ChatUsage, ModelCallError

# How long a reservation may stay open before the upkeep settles it at its maximum.
RESERVATION_TTL: Final = timedelta(minutes=10)
# The first key of the advisory lock of a key's account (the second is the hash of its id).
SPEND_LOCK: Final = 4243

Currency = Literal["USD", "EUR"]
Period = Literal["day", "week", "month"]
CURRENCIES: Final[tuple[Currency, ...]] = ("USD", "EUR")
PERIODS: Final[tuple[Period, ...]] = ("day", "week", "month")
# The roles a limit may be for, and how it holds for them.
LimitRole = Literal["adult", "child", "guest"]
LimitSplit = Literal["each", "together"]
LIMIT_ROLES: Final[tuple[LimitRole, ...]] = ("adult", "child", "guest")
LIMIT_SPLITS: Final[tuple[LimitSplit, ...]] = ("each", "together")

# Whose threshold a note tells: the one of who set the limit, or the one Domo gave himself.
ThresholdBy = Literal["setter", "domo"]

# The smallest amount counted: a millionth of a dollar (the columns keep six decimals).
_MICRO: Final = Decimal("0.000001")


def _rounded_up(amount: Decimal) -> Decimal:
    """``amount`` in millionths of a dollar, rounded up: the account never counts less."""
    return amount.quantize(_MICRO, rounding=ROUND_CEILING)


def _currency(value: str) -> Currency:
    for known in CURRENCIES:
        if known == value:
            return known
    raise RuntimeError(f"a limit has an unknown currency: {value!r}")


def _period(value: str) -> Period:
    for known in PERIODS:
        if known == value:
            return known
    raise RuntimeError(f"a limit has an unknown period: {value!r}")


def _limit_role(value: str) -> LimitRole:
    for known in LIMIT_ROLES:
        if known == value:
            return known
    raise RuntimeError(f"a limit has an unknown role: {value!r}")


def _limit_split(value: str) -> LimitSplit:
    for known in LIMIT_SPLITS:
        if known == value:
            return known
    raise RuntimeError(f"a limit has an unknown split: {value!r}")


def period_start(now: datetime, zone: ZoneInfo | None, period: Period) -> datetime:
    """When the ``period`` that ``now`` falls in began: midnight of the day, of the Monday, of the
    first of the month, in the household's time zone ``zone`` (in UTC without one)."""
    where = UTC if zone is None else zone
    day = now.astimezone(where).date()
    match period:
        case "day":
            first = day
        case "week":
            first = day - timedelta(days=day.weekday())
        case "month":
            first = day.replace(day=1)
    return datetime(first.year, first.month, first.day, tzinfo=where)


async def usd_per(session: AsyncSession, currency: Currency, catalog: Catalog) -> Decimal:
    """What one unit of ``currency`` is worth in dollars: 1 for the dollar; for the euro, the
    latest rate Domo read (``exchange_rates``), else the one of the catalog in force."""
    if currency == "USD":
        return Decimal(1)
    rate = await session.execute(
        select(ExchangeRate.usd_per_unit).where(ExchangeRate.currency == currency)
    )
    found = rate.scalar_one_or_none()
    return catalog.usd_per_eur if found is None else found


async def spent_usd(
    session: AsyncSession,
    *,
    provider_id: str,
    person_id: UUID | None,
    since: datetime,
    role: str | None = None,
) -> Decimal:
    """What the key ``provider_id`` spent since ``since``, in dollars, for everyone
    (``person_id=None``), for one person, or for the ones who have ``role`` today (whatever role
    they had when they spent it; the ones who left the household too, who keep their role: the
    money was spent in the period): the costs settled, and the maxima still reserved."""
    query = select(
        func.coalesce(func.sum(func.coalesce(ModelCall.cost_usd, ModelCall.reserved_usd)), 0)
    ).where(ModelCall.provider_id == provider_id, ModelCall.started_at >= since)
    if person_id is not None:
        query = query.where(ModelCall.person_id == person_id)
    if role is not None:
        query = query.join(Person, Person.id == ModelCall.person_id).where(Person.role == role)
    return Decimal((await session.execute(query)).scalar_one())


async def _limits_of(
    session: AsyncSession, provider_id: str, person_id: UUID | None
) -> Sequence[SpendingLimit]:
    """The limits of the key that apply to a call of ``person_id``: the whole key's, theirs, and
    the ones of the role they have now. Without a person, only the whole key's."""
    query = select(SpendingLimit).where(SpendingLimit.provider_id == provider_id)
    whole = and_(SpendingLimit.person_id.is_(None), SpendingLimit.role.is_(None))
    if person_id is None:
        query = query.where(whole)
    else:
        their_role = select(Person.role).where(Person.id == person_id).scalar_subquery()
        query = query.where(
            or_(whole, SpendingLimit.person_id == person_id, SpendingLimit.role == their_role)
        )
    rows = await session.execute(query.order_by(SpendingLimit.set_at, SpendingLimit.id))
    return rows.scalars().all()


async def _measured(
    session: AsyncSession,
    limit: SpendingLimit,
    *,
    for_person: UUID | None,
    now: datetime,
    zone: ZoneInfo | None,
    catalog: Catalog,
) -> tuple[Decimal, Decimal]:
    """What ``limit`` counts as spent in its period, and how much it is, both in dollars, for a
    call of ``for_person``: a limit for each of a role counts what that person spent, one for the
    role together what all who have it spent. ``ValueError`` (a mistake of the caller) for a limit
    for each of a role without a person: it would count everyone's spending."""
    since = period_start(now, zone, _period(limit.period))
    if limit.split == "each":
        if for_person is None:
            raise ValueError("a limit for each of a role is measured for one of them")
        spent = await spent_usd(
            session, provider_id=limit.provider_id, person_id=for_person, since=since
        )
    else:
        spent = await spent_usd(
            session,
            provider_id=limit.provider_id,
            person_id=limit.person_id,
            since=since,
            role=limit.role,
        )
    return spent, await _limit_usd(session, limit, catalog)


async def _limit_usd(session: AsyncSession, limit: SpendingLimit, catalog: Catalog) -> Decimal:
    """How much ``limit`` is, in dollars."""
    return limit.amount * await usd_per(session, _currency(limit.currency), catalog)


async def _limit_passed(
    session: AsyncSession,
    provider_id: str,
    person_id: UUID | None,
    need: Decimal,
    *,
    now: datetime,
    zone: ZoneInfo | None,
    catalog: Catalog,
) -> SpendingLimit | None:
    """The first limit of the key that applies to ``person_id`` and that a call costing at most
    ``need`` (rounded up) would pass: what it counts as spent in its period, plus ``need``, is
    more than it. ``None`` when the call fits under every limit."""
    maximum = _rounded_up(need)
    for limit in await _limits_of(session, provider_id, person_id):
        spent, amount = await _measured(
            session, limit, for_person=person_id, now=now, zone=zone, catalog=catalog
        )
        if spent + maximum > amount:
            return limit
    return None


@dataclass(frozen=True)
class LimitReached:
    """A call is refused: it would pass the limit ``limit_id``."""

    limit_id: UUID


async def reserve(
    session: AsyncSession,
    *,
    route: Route,
    person_id: UUID | None,
    max_usd: Decimal,
    now: datetime,
    catalog: Catalog,
    zone: ZoneInfo | None,
    news: list[UUID] | None = None,
    tool_id: UUID | None = None,
    build_id: UUID | None = None,
) -> UUID | LimitReached:
    """Reserve ``max_usd``, the most a call on ``route`` for ``person_id`` can cost, under the
    lock of its key (see the module docstring): the id of the ``reserved`` row, or
    ``LimitReached`` when a limit of the key would be passed. The caller commits, which lets the
    lock go. Raises ``ModelCallError("no_model")`` when the key is gone. Someone for whom the
    reservation kept news (``newcomers.newcomer_news``) is added to ``news``, for the caller to
    tell once it committed. A call made to build a tool of Domo's names its build
    (``build_id``) and the tool, once there is one (``tool_id``)."""
    provider_id = route.provider.id
    await session.execute(
        select(func.pg_advisory_xact_lock(SPEND_LOCK, func.hashtext(provider_id)))
    )
    still_there = await session.execute(select(Provider.id).where(Provider.id == provider_id))
    if still_there.scalar_one_or_none() is None:
        raise ModelCallError("no_model")
    passed = await _limit_passed(
        session, provider_id, person_id, max_usd, now=now, zone=zone, catalog=catalog
    )
    if passed is not None:
        return LimitReached(passed.id)
    # Imported here: ``newcomers`` reads what was spent from this module.
    from domo_core.llm.newcomers import newcomer_news

    # Someone's first request on a key another gave is news for the giver.
    giver = await newcomer_news(
        session, provider_id=provider_id, person_id=person_id, task=route.task, now=now
    )
    if giver is not None and news is not None:
        news.append(giver)
    call = ModelCall(
        provider_id=provider_id,
        person_id=person_id,
        task=route.task,
        model_id=route.model_id,
        status="reserved",
        reserved_usd=_rounded_up(max_usd),
        price_unknown=not route.price_known,
        started_at=now,
        tool_id=tool_id,
        build_id=build_id,
    )
    session.add(call)
    await session.flush()
    return call.id


async def at_limit(
    session: AsyncSession,
    *,
    provider: Provider,
    person_id: UUID | None,
    need: Decimal,
    now: datetime,
    catalog: Catalog,
    zone: ZoneInfo | None,
) -> bool:
    """Whether the key ``provider`` has no margin left, under one of its limits for
    ``person_id``, for a call that may cost ``need`` (the first call of a request: spec 4.7,
    a key that "n'a plus de marge"). It is the comparison ``reserve`` makes, without the lock:
    a key that is not at its limit can still be refused a call, by a call made meanwhile."""
    passed = await _limit_passed(
        session, provider.id, person_id, need, now=now, zone=zone, catalog=catalog
    )
    return passed is not None


@dataclass(frozen=True)
class CallCost:
    """What a call cost (``usd``), from the tokens it is counted for (``usage``), which are an
    estimate from the length of the texts when ``estimated``."""

    usd: Decimal
    usage: ChatUsage
    estimated: bool


def call_cost(
    *,
    usage: ChatUsage | None,
    input_chars: int,
    output_chars: int,
    prices: Prices,
    failed_before_any_answer: bool,
) -> CallCost:
    """What a call cost at ``prices``: from the ``usage`` the provider reported, else from the
    characters sent and received (``estimated``); nothing for a call that failed before any
    answer."""
    if failed_before_any_answer:
        return CallCost(Decimal(0), ChatUsage(0, 0), estimated=False)
    if usage is not None:
        return CallCost(_rounded_up(cost_usd(prices, usage)), usage, estimated=False)
    counted = ChatUsage(estimate_tokens(input_chars), estimate_tokens(output_chars))
    return CallCost(_rounded_up(cost_usd(prices, counted)), counted, estimated=True)


async def settle(
    session: AsyncSession,
    call_id: UUID,
    *,
    usage: ChatUsage | None,
    input_chars: int,
    output_chars: int,
    prices: Prices,
    failed_before_any_answer: bool,
    now: datetime,
) -> Decimal:
    """Replace the reservation ``call_id`` with what the call cost (``call_cost``), at ``now``;
    returns the cost. The caller commits."""
    cost = call_cost(
        usage=usage,
        input_chars=input_chars,
        output_chars=output_chars,
        prices=prices,
        failed_before_any_answer=failed_before_any_answer,
    )
    await session.execute(
        update(ModelCall)
        .where(ModelCall.id == call_id)
        .values(
            status="settled",
            cost_usd=cost.usd,
            input_tokens=cost.usage.input_tokens,
            output_tokens=cost.usage.output_tokens,
            cache_read_tokens=cost.usage.cache_read_tokens,
            cache_write_tokens=cost.usage.cache_write_tokens,
            estimated=cost.estimated,
            settled_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    return cost.usd


async def settle_speech(
    session: AsyncSession,
    call_id: UUID,
    *,
    heard_seconds: float,
    said_chars: int,
    prices: SpeechPrices,
    failed_before_any_answer: bool,
    now: datetime,
) -> Decimal:
    """Replace the reservation ``call_id`` of a call of sound with what it cost, at ``now``, and
    count what it heard and said (``prices`` are nothing at home: the call costs nothing, and is
    counted all the same); returns the cost. A call that failed before any answer costs nothing
    and counts nothing. The caller commits."""
    if failed_before_any_answer:
        heard_seconds, said_chars = 0.0, 0
    cost = _rounded_up(speech_cost_usd(prices, heard_seconds=heard_seconds, said_chars=said_chars))
    await session.execute(
        update(ModelCall)
        .where(ModelCall.id == call_id)
        .values(
            status="settled",
            cost_usd=cost,
            heard_seconds=Decimal(str(heard_seconds)).quantize(Decimal("0.01")),
            said_chars=said_chars,
            estimated=False,
            settled_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    return cost


async def settle_stale(session: AsyncSession, now: datetime) -> int:
    """Settle at their maximum (``estimated``) the reservations that are ``RESERVATION_TTL`` old
    at ``now``: their call never said what it cost. Returns how many. The caller commits."""
    settled = await session.execute(
        update(ModelCall)
        .where(ModelCall.status == "reserved", ModelCall.started_at <= now - RESERVATION_TTL)
        .values(status="settled", cost_usd=ModelCall.reserved_usd, estimated=True, settled_at=now)
        .returning(ModelCall.id)
        .execution_options(synchronize_session=False)
    )
    return len(settled.all())


@dataclass(frozen=True)
class LimitNote:
    """Where a limit stands, for the person who set it: ``percent`` of it spent in its period
    (Domo's estimate, rounded down), of ``amount`` ``currency`` per ``period`` on the key
    ``key_label`` (``router.key_label``). ``threshold_reached`` and ``threshold_by`` are set the
    first time in the period that a threshold is passed: the setter's, or Domo's own.
    ``person_name`` is the one person the limit is for (``None``: the whole key). A limit for a
    role has its ``role`` and ``split``; for ``each``, ``percent`` is the share of the one of that
    role who spent the most in the period, and ``top_person_name`` is whose (``None`` when none of
    them spent anything)."""

    limit_id: UUID
    percent: int
    amount: Decimal
    currency: Currency
    period: Period
    key_label: str
    threshold_reached: int | None
    threshold_by: ThresholdBy | None
    person_name: str | None = None
    role: LimitRole | None = None
    split: LimitSplit | None = None
    top_person_name: str | None = None


async def _top_spender(
    session: AsyncSession, limit: SpendingLimit, since: datetime
) -> tuple[Decimal, str | None]:
    """What the one who has the role of ``limit`` and spent the most on its key since ``since``
    spent, and their name (someone who left the household too: they keep their role, and what
    they spent was spent); nothing spent: zero, and no name."""
    spent = func.sum(func.coalesce(ModelCall.cost_usd, ModelCall.reserved_usd))
    found = await session.execute(
        select(spent, Person.display_name)
        .join(Person, Person.id == ModelCall.person_id)
        .where(
            ModelCall.provider_id == limit.provider_id,
            ModelCall.started_at >= since,
            Person.role == limit.role,
        )
        .group_by(Person.id, Person.display_name)
        .order_by(spent.desc(), Person.display_name)
        .limit(1)
    )
    top = found.first()
    if top is None or Decimal(top[0]) <= 0:
        return Decimal(0), None
    return Decimal(top[0]), top[1]


def _due(threshold: int | None, percent: int, told_at: datetime | None, since: datetime) -> bool:
    """Whether a threshold is to be told: passed, and not told yet in the period."""
    return threshold is not None and percent >= threshold and (told_at is None or told_at < since)


async def limit_notes(
    session: AsyncSession,
    *,
    person_id: UUID,
    now: datetime,
    catalog: Catalog,
    zone: ZoneInfo | None,
    own_thresholds: Mapping[str, int],
    mark: bool = True,
) -> list[LimitNote]:
    """Where each limit that ``person_id`` set stands, the oldest first (see ``LimitNote``). A
    threshold passed for the first time in the period is told and marked (flushed, the caller
    commits): the setter's (``warned_at``) before Domo's own (``domo_warned_at``), which
    waits for the next time when both are due at once. Domo's own threshold is the one he gave
    himself on the limit's key: ``own_thresholds``, by key id (``choices.warn_thresholds``: the
    key's own choice, else the household's); a key that is not in it has none. Without ``mark``,
    nothing is written: a turn that may say nothing (an arrival, ``conversation.starts``) marks
    the thresholds told only once it said something (``mark_thresholds_told``)."""
    giver = aliased(Person)
    subject = aliased(Person)
    rows = await session.execute(
        select(SpendingLimit, Provider, giver.display_name, subject.display_name)
        .join(Provider, Provider.id == SpendingLimit.provider_id)
        .join(giver, giver.id == Provider.given_by)
        .outerjoin(subject, subject.id == SpendingLimit.person_id)
        .where(SpendingLimit.set_by == person_id)
        .order_by(SpendingLimit.set_at, SpendingLimit.id)
    )
    notes: list[LimitNote] = []
    for limit, provider, giver_name, subject_name in rows.all():
        period = _period(limit.period)
        since = period_start(now, zone, period)
        top_name: str | None = None
        if limit.split == "each":
            spent, top_name = await _top_spender(session, limit, since)
            amount = await _limit_usd(session, limit, catalog)
        else:
            spent, amount = await _measured(
                session, limit, for_person=None, now=now, zone=zone, catalog=catalog
            )
        percent = int(spent * 100 / amount)
        reached: int | None = None
        by: ThresholdBy | None = None
        own_threshold = own_thresholds.get(provider.id)
        if _due(limit.warn_at_percent, percent, limit.warned_at, since):
            reached, by = limit.warn_at_percent, "setter"
            if mark:
                limit.warned_at = now
        elif _due(own_threshold, percent, limit.domo_warned_at, since):
            reached, by = own_threshold, "domo"
            if mark:
                limit.domo_warned_at = now
        notes.append(
            LimitNote(
                limit_id=limit.id,
                percent=percent,
                amount=limit.amount,
                currency=_currency(limit.currency),
                period=period,
                key_label=key_label(provider, giver_name),
                threshold_reached=reached,
                threshold_by=by,
                person_name=subject_name,
                role=None if limit.role is None else _limit_role(limit.role),
                split=None if limit.split is None else _limit_split(limit.split),
                top_person_name=top_name,
            )
        )
    await session.flush()
    return notes


async def mark_thresholds_told(
    session: AsyncSession, notes: Iterable[LimitNote], *, now: datetime
) -> None:
    """Mark the thresholds that ``notes`` told (read without marking: ``limit_notes(mark=False)``)
    as told at ``now``, once the turn they were given to said something: the setter's
    (``warned_at``) or Domo's own (``domo_warned_at``). Written, the caller commits."""
    for note in notes:
        if note.threshold_by is None:
            continue
        told = "warned_at" if note.threshold_by == "setter" else "domo_warned_at"
        await session.execute(
            update(SpendingLimit).where(SpendingLimit.id == note.limit_id).values({told: now})
        )


def _for_whom(
    person_id: UUID | None, role: LimitRole | None, split: LimitSplit | None
) -> ColumnElement[bool]:
    """The limits of the whole key (no person, no role), of one person, or of a role in one
    split."""
    if role is not None:
        return and_(SpendingLimit.role == role, SpendingLimit.split == split)
    if person_id is None:
        return and_(SpendingLimit.person_id.is_(None), SpendingLimit.role.is_(None))
    return SpendingLimit.person_id == person_id


# The smallest step of a limit: ``spending_limits.amount`` keeps four decimals.
LIMIT_STEP: Final = Decimal("0.0001")


def kept_amount(amount: Decimal) -> Decimal:
    """``amount`` as a limit keeps it: to four decimals, the nearest (half up)."""
    return amount.quantize(LIMIT_STEP, rounding=ROUND_HALF_UP)


def amount_text(amount: Decimal) -> str:
    """An amount as it is said and audited: ``20``, ``0.5``, never ``2E+1``."""
    return format(amount.normalize(), "f")


async def set_spending_limit(
    session: AsyncSession,
    *,
    provider_id: str,
    person_id: UUID | None,
    amount: Decimal | None,
    currency: Currency,
    period: Period,
    warn_at_percent: int | None,
    by: UUID,
    now: datetime,
    role: LimitRole | None = None,
    split: LimitSplit | None = None,
) -> bool:
    """Set the firm limit of the key ``provider_id``, for everyone (``person_id=None``), for one
    person, or for a ``role`` (``adult``, ``child`` or ``guest``) in a ``split`` (``each`` of them,
    or all of them ``together``): ``amount`` ``currency`` per ``period``, with the threshold
    ``warn_at_percent`` of the one who sets it (``by``; ``None``: no threshold). It replaces the
    limit that was there for the same people (the same person, or the same role and split), whose
    thresholds are then told again in the period; ``amount=None`` removes it (``currency``,
    ``period`` and ``warn_at_percent`` are not read then). ``ValueError`` (a mistake of the
    caller) for a role together with a person, or for only one of ``role`` and ``split``.

    The audit entry ``spending.limit_set`` is written (``{"provider_id", "person", "amount",
    "currency", "period"}``), or ``spending.limit_removed`` for a limit taken away
    (``{"provider_id", "person"}``); for a role, with ``"role"`` and ``"split"`` too, whoever's
    tool asked: the caller commits right after it (the audit log holds a lock until then). Returns
    whether anything changed: removing a limit that was not there changes nothing, and audits
    nothing."""
    if (role is None) != (split is None):
        raise ValueError("a role goes with its split, and a split with its role")
    if role is not None and person_id is not None:
        raise ValueError("a limit is for a person or for a role, not both")
    person = None if person_id is None else str(person_id)
    if amount is None:
        removed = await session.execute(
            delete(SpendingLimit)
            .where(SpendingLimit.provider_id == provider_id, _for_whom(person_id, role, split))
            .returning(SpendingLimit.id)
            .execution_options(synchronize_session=False)
        )
        if removed.first() is None:
            return False
        taken: dict[str, str | None] = {"provider_id": provider_id, "person": person}
        if role is not None:
            taken = {**taken, "role": role, "split": split}
        await append_audit(
            session, actor=f"person:{by}", action="spending.limit_removed", details=taken
        )
        return True
    values = {
        "amount": amount,
        "currency": currency,
        "period": period,
        "warn_at_percent": warn_at_percent,
        "set_by": by,
        "set_at": now,
        # A limit set again is a new one: its thresholds are told again.
        "warned_at": None,
        "domo_warned_at": None,
    }
    statement = insert(SpendingLimit).values(
        provider_id=provider_id, person_id=person_id, role=role, split=split, **values
    )
    await session.execute(
        statement.on_conflict_do_update(constraint="uq_spending_limits_provider_whom", set_=values)
    )
    details: dict[str, str | None] = {
        "provider_id": provider_id,
        "person": person,
        "amount": amount_text(amount),
        "currency": currency,
        "period": period,
    }
    if role is not None:
        details = {**details, "role": role, "split": split}
    await append_audit(session, actor=f"person:{by}", action="spending.limit_set", details=details)
    return True
