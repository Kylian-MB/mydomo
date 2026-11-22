"""Noting, changing and forgetting what a person's word has been worth, and the score of it
(spec 6.1 and 6.2).

A fact is written by Domo in his own words: never a secret (the refusal of a memory,
``memory.service.refuses_secret_shapes``). Who may note what, in which turn, is for the tools
that call this to check; here the code keeps the fact, who reported it (for a ``reported``
fact, and for it only) and the date, and computes the score (``trust.score``) from the facts
of a person, from the start Domo chose (``Choices.trust_start``). A person with no fact has the
start. Of the facts one person reported about another, only the ``REPORTED_PER_REPORTER`` newest
count: saying the same thing again and again is still one voice. One ``confirmed`` fact at most
comes from a memory (a partial unique index on ``memory_id``): a second raises ``IntegrityError``,
which the tool that notes checks before.

Nothing here is read by the policy, the confirmations or the spending: a score is data for
Domo's judgement. None of the functions commits: the caller owns the transaction.
"""

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.memory.service import SecretRefused, refuses_secret_shapes
from domo_core.trust.models import Established, Outcome, TrustFact
from domo_core.trust.score import score

# The longest sentence of a fact (the ``trust_facts`` table says the same).
MAX_ABOUT: Final = 200
# How many facts ``facts_of`` gives, the newest first, unless asked otherwise.
FACTS_SHOWN: Final = 10
# How many reported facts of one reporter about one person count in the score, the newest: one
# person telling the same thing again and again is still one voice. Facts whose reporter is gone
# count together, as one reporter.
REPORTED_PER_REPORTER: Final = 3


@dataclass(frozen=True)
class Trust:
    """A person's trust percentage (0 to 100) and how many facts it rests on."""

    percent: int
    facts: int

    def as_json(self) -> dict[str, int]:
        """The figure as Domo is given it, wherever he weighs what he is told (spec 6.4):
        ``{"percent": 82, "facts": 5}``."""
        return {"percent": self.percent, "facts": self.facts}


def _check_about(about: str) -> None:
    if not about.strip() or not 1 <= len(about) <= MAX_ABOUT:
        raise ValueError(f"What a fact is about is 1 to {MAX_ABOUT} characters, not blank.")
    if refuses_secret_shapes(about):
        raise SecretRefused


async def note(
    session: AsyncSession,
    *,
    person_id: uuid.UUID,
    outcome: Outcome,
    established: Established,
    about: str,
    now: datetime,
    memory_id: uuid.UUID | None = None,
    noted_for_id: uuid.UUID | None = None,
    reported_by_id: uuid.UUID | None = None,
) -> TrustFact:
    """A new fact about ``person_id``'s word. ``reported_by_id`` is who reported it, required for
    a ``reported`` fact and refused for another (``ValueError``). Raises ``SecretRefused`` for a
    sentence that has the shape of a secret and ``ValueError`` for one out of bounds; nothing is
    written then. Flushed."""
    _check_about(about)
    if (established == "reported") != (reported_by_id is not None):
        raise ValueError("Who reported a fact goes with a reported fact, and only with one.")
    fact = TrustFact(
        person_id=person_id,
        outcome=outcome,
        established=established,
        about=about,
        memory_id=memory_id,
        noted_for_id=noted_for_id,
        reported_by_id=reported_by_id,
        created_at=now,
    )
    session.add(fact)
    await session.flush()
    return fact


async def trust_of_many(
    session: AsyncSession, person_ids: Collection[uuid.UUID], *, start: int
) -> dict[uuid.UUID, Trust]:
    """The trust of each of ``person_ids``, from the facts that count (``facts``: how many): all
    their established facts, and of their reported facts the ``REPORTED_PER_REPORTER`` newest of
    each reporter (those whose reporter is gone, together); the start, on no fact, for someone
    who has none."""
    if not person_ids:
        return {}
    # Each fact's rank among those of the same person, the same way of being known and the same
    # reporter (PostgreSQL partitions the NULL reporters together), the newest first.
    ranked = (
        select(
            TrustFact.person_id,
            TrustFact.outcome,
            TrustFact.established,
            func.row_number()
            .over(
                partition_by=(TrustFact.person_id, TrustFact.established, TrustFact.reported_by_id),
                order_by=(TrustFact.created_at.desc(), TrustFact.id),
            )
            .label("rank"),
        )
        .where(TrustFact.person_id.in_(person_ids))
        .subquery()
    )
    rows = await session.execute(
        select(ranked.c.person_id, ranked.c.outcome, ranked.c.established, func.count())
        .where(
            or_(
                ranked.c.established != "reported",
                ranked.c.rank <= REPORTED_PER_REPORTER,
            )
        )
        .group_by(ranked.c.person_id, ranked.c.outcome, ranked.c.established)
    )
    facts: dict[uuid.UUID, list[tuple[Outcome, Established]]] = {
        person_id: [] for person_id in person_ids
    }
    for person_id, outcome, established, count in rows.all():
        fact: tuple[Outcome, Established] = (outcome, established)
        facts[person_id].extend([fact] * count)
    return {
        person_id: Trust(percent=score(theirs, start=start), facts=len(theirs))
        for person_id, theirs in facts.items()
    }


async def trust_of(session: AsyncSession, person_id: uuid.UUID, *, start: int) -> Trust:
    """The trust of ``person_id``, from all their facts (the start, on no fact, without one)."""
    return (await trust_of_many(session, [person_id], start=start))[person_id]


async def facts_of(
    session: AsyncSession, person_id: uuid.UUID, *, limit: int = FACTS_SHOWN
) -> list[TrustFact]:
    """The ``limit`` newest facts of ``person_id``, the newest first."""
    result = await session.scalars(
        select(TrustFact)
        .where(TrustFact.person_id == person_id)
        .order_by(TrustFact.created_at.desc(), TrustFact.id)
        .limit(limit)
    )
    return list(result.all())


async def noted_for_since(
    session: AsyncSession, person_id: uuid.UUID, noted_for_id: uuid.UUID, since: datetime
) -> list[TrustFact]:
    """The facts about ``person_id`` noted in the turns of ``noted_for_id`` since ``since``, the
    newest first, ``FACTS_SHOWN`` and one at most (the caller leaves out the one it just
    noted)."""
    result = await session.scalars(
        select(TrustFact)
        .where(
            TrustFact.person_id == person_id,
            TrustFact.noted_for_id == noted_for_id,
            TrustFact.created_at >= since,
        )
        .order_by(TrustFact.created_at.desc(), TrustFact.id)
        .limit(FACTS_SHOWN + 1)
    )
    return list(result.all())


async def get(session: AsyncSession, fact_id: uuid.UUID) -> TrustFact | None:
    """The fact with this id, or ``None``."""
    return await session.get(TrustFact, fact_id)


async def change(session: AsyncSession, fact: TrustFact, *, outcome: Outcome) -> TrustFact:
    """``fact`` with another outcome (Domo took a mistake for a lie, or the other way round);
    the rest stays. Flushed."""
    fact.outcome = outcome
    await session.flush()
    return fact


async def forget(session: AsyncSession, fact_id: uuid.UUID) -> bool:
    """Delete this fact for good (Domo noted it wrongly): whether there was one. Flushed."""
    result = await session.execute(
        delete(TrustFact).where(TrustFact.id == fact_id).returning(TrustFact.id)
    )
    return result.first() is not None
