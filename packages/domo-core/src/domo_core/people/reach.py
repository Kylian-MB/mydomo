"""Who the assistant can name, and who he can ask or write to.

A record of ``persons`` is a person, with or without a login account. Three questions are kept
apart here: which records he may name at all (``known_records``: not merged into another, not
forgotten, whatever their state), which have an account (``has_account``), and who can answer now
(``can_answer``: in the household now and with an account).

Whether a turn of the assistant can reach a record at all is one switch, ``reachable``: today, the
record has an account; the day a voice can answer too, this one function widens. Everything that
waits on "can they be answered, asked or told" reads it, through this module (``reach.reachable``,
``reach.answering``, ``reach.can_answer``, ``reach.answerers``): a note left for them, a plan kept
and coming due, an approver's place, who may agree for a child in a judged case, the news kept for
them, and a review of their conversation when they leave. What needs the account itself (signing
in, a password proof, a return link, a successor who takes over) reads ``has_account``.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import ColumnElement, exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Account, Person
from domo_core.accounts.sessions import active_at


def known_records() -> ColumnElement[bool]:
    """Every record the assistant may name, whatever its state, as a condition on ``persons``."""
    return Person.merged_into_id.is_(None) & Person.forgotten_at.is_(None)


def has_account() -> ColumnElement[bool]:
    """The record has a login account, as a condition on ``persons``."""
    return exists().where(Account.person_id == Person.id)


def reachable() -> ColumnElement[bool]:
    """Whether a turn of the assistant can reach the record, as a condition on ``persons``: today,
    it has an account. The one switch (see the module docstring): a channel that can speak to a
    record without an account (the voice, later) widens it here."""
    return has_account()


def answering(now: datetime) -> ColumnElement[bool]:
    """Who can answer at ``now``, as a condition on ``persons``: in the household now, and
    ``reachable``."""
    return active_at(now) & reachable()


async def can_answer(session: AsyncSession, person_id: UUID, now: datetime) -> bool:
    """Whether the person can answer at ``now`` (``answering``). While it says no, nobody leaves
    them a note, a plan or an approver's place."""
    found = await session.scalar(select(Person.id).where(Person.id == person_id, answering(now)))
    return found is not None


async def answerers(session: AsyncSession, now: datetime) -> list[Person]:
    """The people who can answer at ``now``, in the order they arrived."""
    result = await session.execute(
        select(Person).where(answering(now)).order_by(Person.created_at, Person.id)
    )
    return list(result.scalars())
