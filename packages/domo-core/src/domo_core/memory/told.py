"""What Domo told a person before an action (spec 6.8).

When the judge warns a person (its ``warn``: a yes asked only because of something they must know
first, said in its reason) and they answer that request, yes or no, the code keeps it (``told``):
a memory said in their turn, of origin ``told_by_domo``, that Domo keeps for himself, out of
the core, with the type of the action and the reason as it was said. There is one per person and
type of action: a new warning refreshes it. A model never writes one: no tool makes that origin,
and a tool may only forget it.

What the yes-judge reads of the memories, this one first, is gathered with the rest of its case
(``confirmations.case.gather``).

Nothing here commits: the caller owns the transaction. Nothing here knows the web.
"""

import logging
from datetime import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import ColumnElement, and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.memory.models import Memory
from domo_core.memory.service import MAX_SUBJECT, SecretRefused, change, keep

logger = logging.getLogger(__name__)


def _told_them(person_id: UUID, action_type: str) -> ColumnElement[bool]:
    return and_(
        Memory.said_by_id == person_id,
        Memory.origin == "told_by_domo",
        Memory.action_type == action_type,
    )


async def told(
    session: AsyncSession,
    *,
    person_id: UUID,
    action_type: str,
    reason: str,
    answer: Literal["yes", "no"],
    now: datetime,
) -> None:
    """Keep that Domo told ``person_id``, before an action of ``action_type``, ``reason`` (the
    judge's reason as it was said to them), at ``now``: a fact said in their turn, of origin
    ``told_by_domo``, kept for himself, out of the core, its subject the type of the action. A
    memory of this person and type already there is refreshed instead (its content and
    ``changed_at``). The memory keeps the ``answer`` the person gave; when it changed since the
    last time, what they agreed about being asked again (``told_keep_asking``) is forgotten.
    A reason the memories refuse (blank, too long, the shape of a secret) is not kept: logged,
    never raised. Flushed."""
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
            if found.told_answer != answer:
                found.told_keep_asking = None
            found.told_answer = answer
            await session.flush()
            return
        memory = await keep(
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
        memory.told_answer = answer
        await session.flush()
    except (ValueError, SecretRefused) as error:
        logger.warning(
            "What the assistant told a person could not be kept (%s)", type(error).__name__
        )


async def agreed_asking(
    session: AsyncSession, *, person_id: UUID, action_type: str, keep_asking: bool, now: datetime
) -> bool:
    """Keep that ``person_id`` agreed (``keep_asking`` true) or not to be asked again before an
    action of ``action_type``, on what Domo told them about it. ``False`` when he told them
    nothing about this type of action. Flushed."""
    found = (
        await session.scalars(
            select(Memory)
            .where(_told_them(person_id, action_type))
            .order_by(Memory.changed_at.desc(), Memory.id)
            .limit(1)
        )
    ).first()
    if found is None:
        return False
    found.told_keep_asking = keep_asking
    found.changed_at = now
    await session.flush()
    return True
