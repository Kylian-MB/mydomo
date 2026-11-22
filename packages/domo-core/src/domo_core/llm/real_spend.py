"""The real spending of a key, read with an admin key, and the estimate it corrects (spec 4.8).

Domo counts every call (``llm.spending``): that is his estimate. Two providers also tell what an
organization really spent, day by day, to an admin key (checked on 2026-11-08):

* Anthropic: ``GET /v1/organizations/cost_report?starting_at=...&ending_at=...&bucket_width=1d``,
  with the header ``anthropic-version`` (``ANTHROPIC_VERSION``); each ``data[].results[].amount``
  is a decimal text in cents of a dollar;
* OpenAI: ``GET /v1/organization/costs?start_time=...&end_time=...&bucket_width=1d`` (Unix
  seconds); each ``data[].results[].amount`` is ``{"value", "currency"}``, in dollars.

Both follow their pages (``has_more``, ``next_page``, given back as ``page``). Google gives no
spending to a key: there, it stays an estimate (``real_spend_supported``).

An admin key is a secret like the others: the person types it in a vault form for the reference
``provider:<id>:admin_key`` (``request_admin_key``), bound to the host and the header of the key
itself, and the gatekeeper injects it on the key's admin route, ``<id>-admin`` (``admin_route``),
at the key's own address. The core never holds it.

What a provider told is kept by day (``real_spend``, ``read_real_spend``: the last
``REAL_SPEND_DAYS`` whole days, in UTC as the providers count them), audited
``spending.real_read`` (``{"provider_id", "days"}``). From it, the key's ``spend_factor`` is the
real spending over Domo's estimate, over the days of the last ``REAL_SPEND_DAYS`` that both
count (``correct_factor``), held between ``FACTOR_FLOOR`` and ``FACTOR_CEILING``: Domo keeps the
gap for the days the provider has not told yet.

What a person cost (``spent_by``): each call at its cost, where the day of its key has a real
figure, by the share of that figure its key's calls of the day had (the provider tells a key's
day, not a person's); where it has none, at its cost times the key's factor (1 without one). It is
an estimate (``Spent.estimated``) unless every call fell on a day the provider told.
"""

import json
import logging
from collections import defaultdict
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Final, cast
from uuid import UUID

import httpx
from domo_common.gatekeeper_api import ProviderRoute, SecretRequestView
from sqlalchemy import ColumnElement, Date, func, literal_column, select, update
from sqlalchemy import cast as as_type
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.audit.log import append_audit
from domo_core.gatekeeper_client import GatekeeperClient
from domo_core.llm.models import Provider
from domo_core.llm.provider_keys import (
    KeyForm,
    admin_route_id,
    binding_for,
    key_still_there,
    known_kind,
)
from domo_core.llm.provider_kinds import ProviderKind
from domo_core.llm.refresh import TokenIn, relay_get
from domo_core.llm.spending_models import ModelCall, RealSpend

logger = logging.getLogger(__name__)

# The version of Anthropic's API the cost report is read with.
ANTHROPIC_VERSION: Final = "2023-06-01"
# The text that says what the form of an admin key is for.
ADMIN_PURPOSE_KEY: Final = "chat.key.admin.secret_purpose"
# How many whole days back the real spending is read, and the factor is measured over.
REAL_SPEND_DAYS: Final = 30
# How far the factor may take the estimate: a quarter of it, four times it.
FACTOR_FLOOR: Final = Decimal("0.25")
FACTOR_CEILING: Final = Decimal("4")
# ``providers.spend_factor`` keeps four decimals.
_FACTOR_STEP: Final = Decimal("0.0001")
# The most a page of a report may weigh, and how many pages are followed.
MAX_REPORT_BYTES: Final = 2_000_000
MAX_REPORT_PAGES: Final = 20
_SUPPORTED: Final[frozenset[ProviderKind]] = frozenset({"anthropic", "openai"})
_CENTS: Final = Decimal(100)


def admin_ref(provider_id: str) -> str:
    """The reference of the admin key of the key ``provider_id`` in the vault."""
    return f"provider:{provider_id}:admin_key"


def admin_route(provider: Provider) -> ProviderRoute:
    """The admin route of the key ``provider``: at its own address, with its admin key (none
    until one was given)."""
    return ProviderRoute(
        id=admin_route_id(provider.id),
        base_url=provider.base_url,
        secret_ref=provider.admin_secret_ref,
    )


def real_spend_supported(kind: ProviderKind) -> bool:
    """Whether a provider of ``kind`` tells the real spending to an admin key."""
    return kind in _SUPPORTED


async def request_admin_key(
    gatekeeper: GatekeeperClient, provider: Provider, form: KeyForm
) -> SecretRequestView:
    """Ask the gatekeeper for a one-time form where the person types the admin key of
    ``provider``: for ``admin_ref``, bound to the host and the header of the key itself, in the
    words of ``form`` (``provider_keys.key_form``: the texts of the one who gives it). A form
    asked for again replaces the open one, and its value rotates the secret. Raises
    ``GatekeeperError``."""
    return await gatekeeper.create_secret_request(
        form.request(
            admin_ref(provider.id),
            binding_for(known_kind(provider), provider.base_url),
            ADMIN_PURPOSE_KEY,
        )
    )


@dataclass(frozen=True)
class _Report:
    """How a provider's cost report is asked for, and read."""

    path: str
    token_in: TokenIn
    headers: Mapping[str, str]


_REPORTS: Final[Mapping[ProviderKind, _Report]] = {
    "anthropic": _Report(
        "v1/organizations/cost_report", "x-api-key", {"anthropic-version": ANTHROPIC_VERSION}
    ),
    # Under the key's address, which already ends with ``/v1``.
    "openai": _Report("organization/costs", "authorization", {}),
}


class _Unreadable(Exception):
    """A report is not what its provider documents."""


def _midnight(day: date) -> datetime:
    return datetime.combine(day, time(), tzinfo=UTC)


def _range_params(kind: ProviderKind, since: date, until: date) -> dict[str, str]:
    if kind == "anthropic":
        return {
            "starting_at": f"{since.isoformat()}T00:00:00Z",
            "ending_at": f"{until.isoformat()}T00:00:00Z",
            "bucket_width": "1d",
        }
    return {
        "start_time": str(int(_midnight(since).timestamp())),
        "end_time": str(int(_midnight(until).timestamp())),
        "bucket_width": "1d",
    }


def _amount(value: object) -> Decimal:
    """A non-negative, finite amount (a decimal text, or a number read as a decimal)."""
    if isinstance(value, bool) or not isinstance(value, str | int | Decimal):
        raise _Unreadable
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise _Unreadable from None
    if not amount.is_finite() or amount < 0:
        raise _Unreadable
    return amount


def _objects(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise _Unreadable
    items = cast(list[object], value)
    if not all(isinstance(item, dict) for item in items):
        raise _Unreadable
    return cast(list[dict[str, object]], items)


def _bucket_day(kind: ProviderKind, bucket: Mapping[str, object]) -> date:
    start = bucket.get("starting_at" if kind == "anthropic" else "start_time")
    try:
        if kind == "anthropic" and isinstance(start, str):
            return datetime.fromisoformat(start).astimezone(UTC).date()
        if kind == "openai" and isinstance(start, int) and not isinstance(start, bool):
            return datetime.fromtimestamp(start, UTC).date()
    except (ValueError, OverflowError, OSError):
        pass
    raise _Unreadable


def _bucket_usd(kind: ProviderKind, bucket: Mapping[str, object]) -> Decimal:
    total = Decimal(0)
    for result in _objects(bucket.get("results")):
        if kind == "anthropic":
            if result.get("currency", "USD") != "USD":
                raise _Unreadable
            total += _amount(result.get("amount")) / _CENTS
        else:
            amount = result.get("amount")
            if not isinstance(amount, dict):
                raise _Unreadable
            money = cast(dict[str, object], amount)
            if money.get("currency", "usd") != "usd":
                raise _Unreadable
            total += _amount(money.get("value"))
    return total


def _page(
    kind: ProviderKind, body: bytes, since: date, until: date
) -> tuple[dict[date, Decimal], str | None]:
    """The days of one page of a report (between ``since`` and ``until``, excluded), and the
    cursor of the next page, if there is one."""
    try:
        page: object = json.loads(body, parse_float=Decimal)
    except ValueError:
        raise _Unreadable from None
    if not isinstance(page, dict):
        raise _Unreadable
    read = cast(dict[str, object], page)
    days: dict[date, Decimal] = {}
    for bucket in _objects(read.get("data")):
        day = _bucket_day(kind, bucket)
        if since <= day < until:
            days[day] = days.get(day, Decimal(0)) + _bucket_usd(kind, bucket)
    following = read.get("next_page")
    if read.get("has_more") is True:
        if not isinstance(following, str) or not following:
            raise _Unreadable
        return days, following
    return days, None


async def fetch_real_spend(
    gatekeeper: GatekeeperClient,
    provider: Provider,
    *,
    since: date,
    until: date,
    http: httpx.AsyncClient,
) -> dict[date, Decimal] | None:
    """What the key ``provider`` really spent, in dollars, each day from ``since`` to ``until``
    (excluded), as its provider tells its admin key, through the key's admin route (see the module
    docstring); ``None`` for a kind that tells none, or an answer that does not come or does not
    read (logged)."""
    kind = known_kind(provider)
    report = _REPORTS.get(kind)
    if report is None:
        return None
    days: dict[date, Decimal] = {}
    params = _range_params(kind, since, until)
    for _ in range(MAX_REPORT_PAGES):
        body = await relay_get(
            gatekeeper,
            http,
            admin_route_id(provider.id),
            report.path,
            max_bytes=MAX_REPORT_BYTES,
            params=params,
            headers=report.headers,
            token_in=report.token_in,
        )
        if body is None:
            return None
        try:
            read, following = _page(kind, body, since, until)
        except _Unreadable:
            logger.warning("The real spending of a key cannot be read: it stays an estimate")
            return None
        for day, usd in read.items():
            days[day] = days.get(day, Decimal(0)) + usd
        if following is None:
            return days
        params = {**_range_params(kind, since, until), "page": following}
    logger.warning("The real spending of a key has too many pages: it stays an estimate")
    return None


def _utc_day() -> ColumnElement[date]:
    """The day of a call, in UTC (as the providers count the days)."""
    # The zone inline, not as a parameter: the day is grouped by, and must read as one expression.
    return as_type(func.timezone(literal_column("'UTC'"), ModelCall.started_at), Date)


def _counted() -> ColumnElement[Decimal]:
    """What a call counts on the account: its cost once settled, its maximum until then."""
    return func.coalesce(ModelCall.cost_usd, ModelCall.reserved_usd)


async def correct_factor(
    session: AsyncSession, provider: Provider, *, now: datetime
) -> Decimal | None:
    """The key's real spending over Domo's estimate, over the days of the last
    ``REAL_SPEND_DAYS`` (before the day of ``now``) that the provider told and Domo counted a
    call on, held between ``FACTOR_FLOOR`` and ``FACTOR_CEILING``, kept as the key's
    ``spend_factor`` (flushed, the caller commits); ``None`` (and nothing changes) when there is no
    such day."""
    today = now.astimezone(UTC).date()
    since = today - timedelta(days=REAL_SPEND_DAYS)
    estimates = await session.execute(
        select(_utc_day(), func.sum(_counted()))
        .where(
            ModelCall.provider_id == provider.id,
            ModelCall.started_at >= _midnight(since),
            ModelCall.started_at < _midnight(today),
        )
        .group_by(_utc_day())
    )
    estimated = {day: Decimal(usd) for day, usd in estimates.all() if usd > 0}
    if not estimated:
        return None
    told = await session.execute(
        select(RealSpend.day, RealSpend.usd).where(
            RealSpend.provider_id == provider.id, RealSpend.day.in_(list(estimated))
        )
    )
    real = dict(told.all())
    if not real:
        return None
    ratio = sum(real.values(), Decimal(0)) / sum((estimated[day] for day in real), Decimal(0))
    factor = min(max(ratio, FACTOR_FLOOR), FACTOR_CEILING).quantize(_FACTOR_STEP, ROUND_HALF_UP)
    await session.execute(
        update(Provider)
        .where(Provider.id == provider.id)
        .values(spend_factor=factor)
        .execution_options(synchronize_session=False)
    )
    return factor


async def read_real_spend(
    session: AsyncSession,
    gatekeeper: GatekeeperClient,
    provider: Provider,
    *,
    now: datetime,
    http: httpx.AsyncClient,
) -> int | None:
    """Declare the admin route of ``provider``, read what it really spent over the last
    ``REAL_SPEND_DAYS`` whole days (``fetch_real_spend``), keep it by day, fetched at ``now``,
    correct its factor (``correct_factor``) and audit ``spending.real_read``. Returns how many days
    were told, or ``None`` when nothing could be read (the estimate stays as it was) or the key was
    removed meanwhile (``provider_keys.key_still_there``: its routes are declared again without a
    secret, and nothing is written). Raises ``GatekeeperError`` when the gatekeeper refuses the
    route. ``session`` holds no transaction when it is called; committed."""
    await gatekeeper.register_provider(admin_route(provider))
    today = now.astimezone(UTC).date()
    days = await fetch_real_spend(
        gatekeeper, provider, since=today - timedelta(days=REAL_SPEND_DAYS), until=today, http=http
    )
    # The admin route was declared with its key: a key removed meanwhile must not keep it.
    if not await key_still_there(session, gatekeeper, provider):
        return None
    if days is None:
        # Nothing was written: the lock goes, the session's objects stay readable.
        await session.commit()
        return None
    for day, usd in days.items():
        values = {"usd": usd, "fetched_at": now}
        await session.execute(
            insert(RealSpend)
            .values(provider_id=provider.id, day=day, **values)
            .on_conflict_do_update(
                index_elements=[RealSpend.provider_id, RealSpend.day], set_=values
            )
        )
    await correct_factor(session, provider, now=now)
    # Last, just before the commit: the audit log holds a lock until it ends.
    await append_audit(
        session,
        actor="system",
        action="spending.real_read",
        details={"provider_id": provider.id, "days": len(days)},
    )
    await session.commit()
    return len(days)


async def admin_keys_due(
    session: AsyncSession, *, now: datetime, every: timedelta
) -> list[Provider]:
    """The keys with an admin key, of a kind that tells the real spending, whose real spending
    was not read in the last ``every`` at ``now`` (or never), oldest first."""
    last_read = (
        select(func.max(RealSpend.fetched_at))
        .where(RealSpend.provider_id == Provider.id)
        .scalar_subquery()
    )
    found = await session.execute(
        select(Provider)
        .where(
            Provider.admin_secret_ref.is_not(None),
            Provider.kind.in_(_SUPPORTED),
            func.coalesce(last_read < now - every, True),
        )
        .order_by(Provider.created_at, Provider.id)
    )
    return list(found.scalars().all())


# --- what a person cost --------------------------------------------------------------------------


@dataclass(frozen=True)
class Spent:
    """What a person cost: ``usd``, the provider's figures where a day of a key has one, else
    Domo's estimate corrected by the key's factor; ``real_usd`` the part that came from the
    providers' figures (``None`` when none did); ``estimated`` whether any of it is Domo's
    estimate; ``unknown_prices`` whether a call was counted at a fall-back price."""

    usd: Decimal = Decimal(0)
    real_usd: Decimal | None = None
    estimated: bool = True
    unknown_prices: bool = False


@dataclass(frozen=True)
class _Row:
    provider_id: str | None
    person_id: UUID | None
    day: date
    usd: Decimal
    price_unknown: bool


async def _rows(
    session: AsyncSession,
    *,
    since: datetime,
    person_id: UUID | None,
    keys: Collection[str] | None,
) -> list[_Row]:
    query = (
        select(
            ModelCall.provider_id,
            ModelCall.person_id,
            _utc_day(),
            func.sum(_counted()),
            func.bool_or(ModelCall.price_unknown),
        )
        .where(ModelCall.started_at >= since)
        .group_by(ModelCall.provider_id, ModelCall.person_id, _utc_day())
    )
    if person_id is not None:
        query = query.where(ModelCall.person_id == person_id)
    if keys is not None:
        query = query.where(ModelCall.provider_id.in_(keys))
    found = await session.execute(query)
    return [_Row(*row) for row in found.all()]


async def _ratios(
    session: AsyncSession, rows: Sequence[_Row]
) -> Mapping[tuple[str, date], Decimal]:
    """For each key and day of ``rows`` that the provider told: its real figure over Domo's
    estimate of the whole day (1 when Domo counted nothing that day)."""
    keyed = {(row.provider_id, row.day) for row in rows if row.provider_id is not None}
    if not keyed:
        return {}
    keys = {key for key, _ in keyed}
    days = {day for _, day in keyed}
    told = await session.execute(
        select(RealSpend.provider_id, RealSpend.day, RealSpend.usd).where(
            RealSpend.provider_id.in_(keys), RealSpend.day.in_(days)
        )
    )
    real = {(key, day): usd for key, day, usd in told.all() if (key, day) in keyed}
    if not real:
        return {}
    whole = await session.execute(
        select(ModelCall.provider_id, _utc_day(), func.sum(_counted()))
        .where(
            ModelCall.provider_id.in_({key for key, _ in real}),
            _utc_day().in_({day for _, day in real}),
        )
        .group_by(ModelCall.provider_id, _utc_day())
    )
    estimated = {(key, day): Decimal(usd) for key, day, usd in whole.all() if key is not None}
    return {
        found: usd / estimated[found] if estimated.get(found, Decimal(0)) > 0 else Decimal(1)
        for found, usd in real.items()
    }


async def spent_by(
    session: AsyncSession,
    *,
    since: datetime,
    person_id: UUID | None = None,
    keys: Collection[str] | None = None,
) -> dict[UUID | None, Spent]:
    """What each person cost since ``since`` (see the module docstring), on the keys ``keys``
    (all of them, those removed since included, when ``None``), for ``person_id`` alone when it
    is given; a person with no call is not in it."""
    rows = await _rows(session, since=since, person_id=person_id, keys=keys)
    ratios = await _ratios(session, rows)
    found_factors = await session.execute(
        select(Provider.id, Provider.spend_factor).where(
            Provider.id.in_({row.provider_id for row in rows if row.provider_id is not None})
        )
    )
    factors = {key: factor for key, factor in found_factors.all() if factor is not None}
    usd: defaultdict[UUID | None, Decimal] = defaultdict(Decimal)
    real: dict[UUID | None, Decimal] = {}
    estimated: set[UUID | None] = set()
    unknown: set[UUID | None] = set()
    for row in rows:
        who = row.person_id
        ratio = None if row.provider_id is None else ratios.get((row.provider_id, row.day))
        if ratio is None:
            factor = (
                Decimal(1) if row.provider_id is None else factors.get(row.provider_id, Decimal(1))
            )
            usd[who] += row.usd * factor
            estimated.add(who)
        else:
            usd[who] += row.usd * ratio
            real[who] = real.get(who, Decimal(0)) + row.usd * ratio
        if row.price_unknown:
            unknown.add(who)
    return {
        who: Spent(
            usd=total,
            real_usd=real.get(who),
            estimated=who in estimated,
            unknown_prices=who in unknown,
        )
        for who, total in usd.items()
    }
