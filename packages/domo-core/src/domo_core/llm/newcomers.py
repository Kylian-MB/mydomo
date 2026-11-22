"""Who began using a key: the giver of the key is told once, and Domo judges whether to say so
(spec 11.4). The code tells no one by itself.

Someone begins using a key with their first ``conversation`` call on it: every request of theirs
makes one. No other kind of call counts: not the upkeep, nor a check of the key, nor the yes-judge,
which an upkeep turn asks too when the person adds a key of their own (being asked for a key is not
using one). Two turns of the giver at once may find the same newcomer: the one that writes the row
first tells them, the other leaves them out.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Final
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import Select, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from domo_core.accounts.models import Person
from domo_core.llm.catalog import Catalog
from domo_core.llm.models import Provider
from domo_core.llm.router import key_label
from domo_core.llm.spending import period_start, spent_usd, usd_per
from domo_core.llm.spending_models import KeyNewcomer, ModelCall
from domo_core.policy.roles import Role, as_role

# Most newcomers told in one turn; the others wait for the next one.
MAX_NEWCOMERS_TOLD: Final = 10

# The kinds of call that make someone a user of a key: a request of theirs (see the module
# docstring).
NEWCOMER_TASKS: Final = ("conversation",)

_CENT: Final = Decimal("0.01")


@dataclass(frozen=True)
class Newcomer:
    """Someone who began using a key (``key_label``): their name, their role, who invited them
    (``host_name``, even if that person left; ``None`` without a host), and what they spent on the
    key this month, in dollars and in euros (an estimate). The names are someone's words. The key
    (``provider_id``) and the person (``person_id``) are what marks them as told
    (``mark_newcomers_told``)."""

    key_label: str
    person_name: str
    role: Role
    host_name: str | None
    spent_usd: Decimal
    spent_eur: Decimal
    provider_id: str
    person_id: UUID


def _cents(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def _untold[*Ts](query: Select[*Ts], giver_id: UUID) -> Select[*Ts]:
    """``query``, from the keys ``giver_id`` gave, joined to the people, other than them, who
    began using one and whom they were not told of yet: one row per key, person and call of
    theirs (the caller groups them)."""
    return (
        query.select_from(Provider)
        .join(ModelCall, ModelCall.provider_id == Provider.id)
        .join(Person, Person.id == ModelCall.person_id)
        .outerjoin(
            KeyNewcomer,
            (KeyNewcomer.provider_id == Provider.id) & (KeyNewcomer.person_id == Person.id),
        )
        .where(
            Provider.given_by == giver_id,
            ModelCall.task.in_(NEWCOMER_TASKS),
            Person.id != giver_id,
            KeyNewcomer.person_id.is_(None),
        )
    )


async def untold_newcomers(session: AsyncSession, *, giver_id: UUID) -> int:
    """How many newcomers on the keys ``giver_id`` gave they were not told of yet (what
    ``newcomers_told`` would tell them, but for its limit), without marking any (an arrival
    weighs what is new, ``conversation.starts``). Reads only."""
    pairs = _untold(select(Provider.id, Person.id), giver_id).group_by(Provider.id, Person.id)
    found = await session.execute(select(func.count()).select_from(pairs.subquery()))
    return found.scalar_one()


async def newcomers_told(
    session: AsyncSession,
    *,
    giver_id: UUID,
    now: datetime,
    zone: ZoneInfo | None,
    catalog: Catalog,
    mark: bool = True,
) -> list[Newcomer]:
    """The people, other than ``giver_id``, who began using a key that ``giver_id`` gave and whom
    they were not told of yet: at most ``MAX_NEWCOMERS_TOLD``, the ones whose first call is the
    oldest first. They are marked as told (written, the caller commits); one that another turn
    marked first is left out, and is no error. Without ``mark``, nothing is written: a turn that
    may say nothing (an arrival, ``conversation.starts``) marks them only once it said something
    (``mark_newcomers_told``)."""
    host = aliased(Person)
    first_call = func.min(ModelCall.started_at)
    found = await session.execute(
        _untold(select(Provider, Person, host.display_name), giver_id)
        .outerjoin(host, host.id == Person.invited_by)
        .group_by(Provider.id, Person.id, host.id)
        .order_by(first_call, Person.id, Provider.id)
        .limit(MAX_NEWCOMERS_TOLD)
    )
    rows = found.all()
    if not rows:
        return []
    giver_name = (
        await session.execute(select(Person.display_name).where(Person.id == giver_id))
    ).scalar_one()
    since = period_start(now, zone, "month")
    euro = await usd_per(session, "EUR", catalog)
    newcomers: list[Newcomer] = []
    for provider, person, host_name in rows:
        # Another turn of the giver may have told them meanwhile: its row stands, and this one
        # leaves them out.
        if mark and not await _marked(session, provider.id, person.id, now):
            continue
        spent = await spent_usd(session, provider_id=provider.id, person_id=person.id, since=since)
        newcomers.append(
            Newcomer(
                key_label=key_label(provider, giver_name),
                person_name=person.display_name,
                role=as_role(person.role),
                host_name=host_name,
                spent_usd=_cents(spent),
                spent_eur=_cents(spent / euro),
                provider_id=provider.id,
                person_id=person.id,
            )
        )
    return newcomers


async def _marked(session: AsyncSession, provider_id: str, person_id: UUID, now: datetime) -> bool:
    """Mark the person ``person_id`` told as a newcomer on the key ``provider_id``, at ``now``;
    ``False`` when another turn marked them first (its row stands)."""
    written = await session.execute(
        insert(KeyNewcomer)
        .values(provider_id=provider_id, person_id=person_id, told_at=now)
        .on_conflict_do_nothing(index_elements=[KeyNewcomer.provider_id, KeyNewcomer.person_id])
        .returning(KeyNewcomer.person_id)
    )
    return written.first() is not None


async def mark_newcomers_told(
    session: AsyncSession, newcomers: Iterable[Newcomer], *, now: datetime
) -> None:
    """Mark ``newcomers`` (read without marking: ``newcomers_told(mark=False)``) as told at
    ``now``, once the turn they were given to said something; one another turn marked first keeps
    its row. Written, the caller commits."""
    for newcomer in newcomers:
        await _marked(session, newcomer.provider_id, newcomer.person_id, now)


async def newcomer_news(
    session: AsyncSession,
    *,
    provider_id: str,
    person_id: UUID | None,
    task: str,
    now: datetime,
) -> UUID | None:
    """A call of the kind ``task`` for ``person_id`` on the key ``provider_id`` is being reserved
    (``spending.reserve``): when it is the first that makes them a newcomer on a key someone else
    gave, and the giver was not told of them yet, it is news for the giver
    (``conversation.visits``), marked in the reservation's transaction (flushed, the caller
    commits). The giver when it is, so that the caller tells the core once it committed
    (``Conversation.notify``); ``None`` otherwise."""
    if task not in NEWCOMER_TASKS or person_id is None:
        return None
    giver = await session.scalar(select(Provider.given_by).where(Provider.id == provider_id))
    if giver is None or giver == person_id:
        return None
    told = select(KeyNewcomer.person_id).where(
        KeyNewcomer.provider_id == provider_id, KeyNewcomer.person_id == person_id
    )
    if await session.scalar(select(told.exists())):
        return None
    earlier = select(ModelCall.id).where(
        ModelCall.provider_id == provider_id,
        ModelCall.person_id == person_id,
        ModelCall.task.in_(NEWCOMER_TASKS),
    )
    if await session.scalar(select(earlier.exists())):
        return None
    # Imported here: ``conversation.visits`` counts the newcomers with this module.
    from domo_core.conversation.visits import news_for

    await news_for(session, giver, now=now)
    return giver
