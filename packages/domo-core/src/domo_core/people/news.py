"""What is new for a person (``persons.news_at``), kept where the people layer can reach it.

``conversation.visits`` explains the news and the turns they give (and re-exports ``news_for``).
"""

import uuid
from datetime import datetime

from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.people import reach

__all__ = ["news_for"]


async def news_for(session: AsyncSession, person_id: uuid.UUID, *, now: datetime) -> None:
    """Something new came at ``now`` for ``person_id`` that no turn of Domo said yet: a note left
    for them, a request of theirs someone else decided, a newcomer on a key they gave, an act the
    owner is owed the telling of, a link that waited for them which the owner was shown
    (``persons.news_at``, see ``conversation.visits``). In the
    transaction that makes it: flushed, the caller commits, then tells the core
    (``Conversation.notify``): a news turn may be due. Never back in time: a decision passes when
    it was made, which may be before news already marked. Nothing is kept for someone no turn of
    the assistant can reach (``reach.reachable``, the one switch: today, someone without an
    account)."""
    await session.execute(
        update(Person)
        .where(Person.id == person_id, reach.reachable())
        .values(news_at=func.greatest(Person.news_at, now))
    )
