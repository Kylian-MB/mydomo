"""The names each person uses for the others ("Sam" for Samuel, "Juju" for someone else).

A nickname is one person's own way of naming someone of the household (spec 6.7): the person who
uses it (``person_id``) and the one it means (``target_id``). Someone else may use the same word
for another person, or none: a nickname means something only in the turns of the person who keeps
it (``household.service.resolve_person``, after the whole name). Two nicknames of one person are
the same nickname when they read the same (``text.name_key``): keeping it again makes it mean the
new person. A nickname of someone who left the household means nobody while they are away, and is
not shown; it goes with either person when their row is deleted.

A nickname is not a memory: the code reads it to find whom a call names, so it is kept on its own.
It is never a right, a rule or a limit. None of these functions commits: the caller owns the
transaction.
"""

import uuid
from datetime import datetime
from typing import Final, Literal

from sqlalchemy import CheckConstraint, ForeignKey, UniqueConstraint, delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from domo_core.accounts.models import Person
from domo_core.accounts.sessions import active_at
from domo_core.db import Base
from domo_core.people.reach import known_records
from domo_core.prompts import MAX_NICKNAMES_SHOWN
from domo_core.text import name_key

# The longest nickname (the ``person_nicknames`` table says the same).
MAX_NICKNAME_LENGTH: Final = 40


class PersonNickname(Base):
    """The name ``person_id`` uses for ``target_id`` (``nickname``, as they say it, cleaned like a
    name: ``tools.nickname_tools``), and what it reads as (``nickname_key``, ``text.name_key``):
    one per person and key (``one_per_person``)."""

    __tablename__ = "person_nicknames"
    __table_args__ = (
        CheckConstraint("char_length(nickname) BETWEEN 1 AND 40", name="nickname_length"),
        UniqueConstraint("person_id", "nickname_key", name="uq_person_nicknames_one_per_person"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    person_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("persons.id", ondelete="CASCADE"))
    target_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("persons.id", ondelete="CASCADE"))
    nickname: Mapped[str]
    nickname_key: Mapped[str]
    created_at: Mapped[datetime]


async def keep(
    session: AsyncSession,
    *,
    person_id: uuid.UUID,
    nickname: str,
    target_id: uuid.UUID,
    now: datetime,
) -> None:
    """``person_id`` uses ``nickname`` for ``target_id`` from now on: the same nickname of theirs
    that meant someone else now means ``target_id``, as they say it this time."""
    statement = insert(PersonNickname).values(
        person_id=person_id,
        target_id=target_id,
        nickname=nickname,
        nickname_key=name_key(nickname),
        created_at=now,
    )
    await session.execute(
        statement.on_conflict_do_update(
            constraint="uq_person_nicknames_one_per_person",
            set_={
                "target_id": statement.excluded.target_id,
                "nickname": statement.excluded.nickname,
                "created_at": statement.excluded.created_at,
            },
        )
    )


async def forget(session: AsyncSession, *, person_id: uuid.UUID, nickname: str) -> bool:
    """``person_id`` no longer uses ``nickname``: whether they kept it."""
    result = await session.execute(
        delete(PersonNickname)
        .where(
            PersonNickname.person_id == person_id,
            PersonNickname.nickname_key == name_key(nickname),
        )
        .returning(PersonNickname.id)
    )
    return result.first() is not None


async def of(
    session: AsyncSession, person_id: uuid.UUID, *, now: datetime, limit: int = MAX_NICKNAMES_SHOWN
) -> list[tuple[str, str]]:
    """The nicknames ``person_id`` keeps for people in the household at ``now``, the oldest kept
    first, ``limit`` at most: each with the whole display name of the one it means."""
    result = await session.execute(
        select(PersonNickname.nickname, Person.display_name)
        .join(Person, Person.id == PersonNickname.target_id)
        .where(PersonNickname.person_id == person_id, active_at(now))
        .order_by(PersonNickname.created_at, PersonNickname.id)
        .limit(limit)
    )
    return [(nickname, name) for nickname, name in result]


async def meant(
    session: AsyncSession,
    person_id: uuid.UUID,
    name: str,
    *,
    now: datetime,
    among: Literal["household", "records"] = "household",
) -> Person | None:
    """The person in the household at ``now`` that ``name`` means as a nickname ``person_id``
    keeps (``among="records"``: any record the assistant may name, whatever its state); ``None``
    when it is no nickname of theirs, or the one it means left (or is no record any more)."""
    key = name_key(name)
    if not key:
        return None
    result = await session.execute(
        select(Person)
        .join(PersonNickname, PersonNickname.target_id == Person.id)
        .where(
            PersonNickname.person_id == person_id,
            PersonNickname.nickname_key == key,
            active_at(now) if among == "household" else known_records(),
        )
    )
    return result.scalars().first()
