"""What Domo told a person before an action, and what the yes-judge may read of the memories
(spec 6.8).

When the judge warns a person (its ``warn``: a yes asked only because of something they must know
first, said in its reason) and they answer that request, yes or no, the code keeps it (``told``):
a memory said in their turn, of origin ``told_by_domo``, that Domo keeps for himself, out of
the core, with the type of the action and the reason as it was said. There is one per person and
type of action: a new warning refreshes it. A model never writes one: no tool makes that origin,
and a tool may only forget it.

The judge reads only what the person said themselves (``own_words``) or what Domo told them
(``told_by_domo``), and only memories said in their own turns (``for_judge``): never one said by
someone else, nor one of the words of someone else, of a deduction or of an outside content. The
filter is in the queries themselves.

Nothing here commits: the caller owns the transaction. Nothing here knows the web.
"""

import logging
from datetime import datetime
from typing import Final
from uuid import UUID

from sqlalchemy import ColumnElement, and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.memory.embedder import Embedder
from domo_core.memory.models import Memory, Origin
from domo_core.memory.search import search
from domo_core.memory.service import MAX_SUBJECT, SecretRefused, change, keep

logger = logging.getLogger(__name__)

# The origins the judge may read: the person's own words, and what Domo told them.
FOR_THE_JUDGE: Final[tuple[Origin, ...]] = ("own_words", "told_by_domo")


def _told_them(person_id: UUID, action_type: str) -> ColumnElement[bool]:
    return and_(
        Memory.said_by_id == person_id,
        Memory.origin == "told_by_domo",
        Memory.action_type == action_type,
    )


async def told(
    session: AsyncSession, *, person_id: UUID, action_type: str, reason: str, now: datetime
) -> None:
    """Keep that Domo told ``person_id``, before an action of ``action_type``, ``reason`` (the
    judge's reason as it was said to them), at ``now``: a fact said in their turn, of origin
    ``told_by_domo``, kept for himself, out of the core, its subject the type of the action. A
    memory of this person and type already there is refreshed instead (its content and
    ``changed_at``). A reason the memories refuse (blank, too long, the shape of a secret) is not
    kept: logged, never raised. Flushed."""
    found = (
        await session.scalars(
            select(Memory)
            .where(_told_them(person_id, action_type))
            .order_by(Memory.changed_at.desc(), Memory.id)
            .limit(1)
        )
    ).first()
    try:
        if found is not None:
            await change(session, found, by=person_id, now=now, content=reason)
            return
        await keep(
            session,
            said_by=person_id,
            subject=action_type[:MAX_SUBJECT],
            content=reason,
            kind="fact",
            origin="told_by_domo",
            kept_private=True,
            core=False,
            now=now,
            action_type=action_type,
        )
    except (ValueError, SecretRefused) as error:
        logger.warning(
            "What the assistant told a person could not be kept (%s)", type(error).__name__
        )


async def for_judge(
    session: AsyncSession,
    person_id: UUID,
    action_type: str,
    query: str,
    *,
    embedder: Embedder,
    limit: int = 5,
) -> list[Memory]:
    """What the judge may read of ``person_id``'s memories about an action of ``action_type``,
    ``limit`` at most: what Domo told them before such an action first (the latest first), then
    what the search finds from ``query`` (the action in words), the best first. Only memories said
    in their turns (``said_by_id``) and of origin ``own_words`` or ``told_by_domo``, filtered in
    the queries: never someone else's, nor the words of someone else, a deduction or an outside
    content, nor what another person keeps private."""
    first = list(
        (
            await session.scalars(
                select(Memory)
                .where(_told_them(person_id, action_type))
                .order_by(Memory.changed_at.desc(), Memory.id)
                .limit(limit)
            )
        ).all()
    )
    if len(first) >= limit:
        return first
    theirs = and_(Memory.said_by_id == person_id, Memory.origin.in_(FOR_THE_JUDGE))
    if first:
        theirs = and_(theirs, Memory.id.not_in([memory.id for memory in first]))
    found = await search(
        session, person_id, query, embedder=embedder, limit=limit - len(first), only=theirs
    )
    return first + found
