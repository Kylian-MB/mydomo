"""The trust figures Domo is shown where he weighs what he is told (spec 6.4).

Next to each memory that is someone's word about another person, not confirmed by them
(``memory.service.unconfirmed``), said by someone else than the person of the turn: the trust of
whoever said it. And, for a turn, the trust of the person who speaks. Only for someone who is in
the household now (``accounts.sessions.active_at``): a person who left, or whose row is gone, has
no figure shown.

``trust_shown`` reads them all at once: one savepoint, one query on the people, one grouped query
on the facts (``trust.service.trust_of_many``), and the household's start when the caller does
not give it. A read that fails never fails what reads it (``db.read_or_nothing``): it gives no
figure. The figures are data for Domo's judgement: nothing of the policy, the barrier or the
spending reads them (the yes-judge reads the facts in its case, to weigh, never as a yes).
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.accounts.sessions import active_at
from domo_core.db import read_or_nothing
from domo_core.llm.choices import load_choices
from domo_core.memory.models import Memory
from domo_core.memory.service import unconfirmed
from domo_core.trust.service import Trust, trust_of_many


@dataclass(frozen=True)
class TrustShown:
    """The figures a read gives: by memory id, the trust of who said it (``of_memories``), and
    the trust of the person who speaks (``of_speaker``, ``None``: not asked, not in the household,
    or the read failed)."""

    of_memories: Mapping[uuid.UUID, Trust] = field(default_factory=lambda: {})
    of_speaker: Trust | None = None


NOTHING_SHOWN = TrustShown()


async def trust_shown(
    session: AsyncSession,
    memories: Sequence[Memory],
    *,
    speaker: uuid.UUID,
    with_speaker: bool,
    start: int | None,
    now: datetime,
) -> TrustShown:
    """The trust of whoever said each of ``memories`` that is unconfirmed and was said by someone
    else than ``speaker`` (the person of the turn), and, ``with_speaker``, the speaker's own: from
    the household's start (``start``; ``None``: read here, ``Choices.trust_start``), for those
    who are in the household at ``now``. Nothing at all when the read fails; no statement when
    nobody is asked for."""
    sayers = {
        memory.id: memory.said_by_id
        for memory in memories
        if unconfirmed(memory) and memory.said_by_id is not None and memory.said_by_id != speaker
    }
    wanted: set[uuid.UUID] = set(sayers.values())
    if with_speaker:
        wanted.add(speaker)
    if not wanted:
        return NOTHING_SHOWN

    async def read() -> TrustShown:
        begins = (await load_choices(session, None)).trust_start if start is None else start
        present = await session.scalars(
            select(Person.id).where(Person.id.in_(wanted), active_at(now))
        )
        trusts = await trust_of_many(session, set(present.all()), start=begins)
        return TrustShown(
            of_memories={
                memory_id: trusts[said_by]
                for memory_id, said_by in sayers.items()
                if said_by in trusts
            },
            of_speaker=trusts.get(speaker) if with_speaker else None,
        )

    return await read_or_nothing(session, "trust figures", read, NOTHING_SHOWN)
