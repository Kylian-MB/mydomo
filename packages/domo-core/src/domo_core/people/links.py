"""What one person is to another: partner of, child of, friend of... in the assistant's own words.

A link is ``from`` ``relation`` ``to`` ("Dana", "mother of", "Alex"). It always says who said it
(``said_by``): the assistant's own deduction has nobody. What the code holds, whoever asks:

* a link **stands** (``confirmed_at``) only by the word of who can know: the owner, or one of the
  two people it is about **while they have access**. Someone else's word is only theirs; and a
  newcomer who says they are the owner's brother, while they have no access yet, confirms nothing
  (they could be anyone). The owner, or the one of the two who has access, confirms it later
  (``confirm``);
* the same link (the same two records, the same words) is kept once;
* every act leaves its trace with its reason (``people.acts.note_act``).

The functions flush and do not commit: the caller does.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.accounts.sessions import is_active
from domo_core.people.acts import note_act
from domo_core.people.models import Act, PersonLink
from domo_core.people.records import NotPossible
from domo_core.policy.roles import DOMO_LABEL
from domo_core.text import name_key

__all__ = [
    "MAX_RELATION_LENGTH",
    "confirm",
    "drop",
    "find",
    "links_of",
    "note_link",
    "plain_relation",
    "related",
]

MAX_RELATION_LENGTH = 60


def plain_relation(relation: str) -> str:
    """The relation as it is kept: one plain line of 1 to ``MAX_RELATION_LENGTH`` characters.
    ``NotPossible("bad_relation")`` otherwise."""
    words = relation.strip()
    if not words or len(words) > MAX_RELATION_LENGTH or len(words.splitlines()) > 1:
        raise NotPossible("bad_relation")
    return words


def _a_record(person: Person) -> None:
    if person.merged_into_id is not None or person.forgotten_at is not None:
        raise NotPossible("not_a_record")


def _needs_a_reason(by: str, reason: str | None) -> None:
    if by == DOMO_LABEL and not reason:
        raise ValueError("an act of the assistant has its reason")


def _knows(person: Person, link_ends: tuple[UUID, UUID], now: datetime) -> bool:
    """Whether ``person``'s word makes a link between ``link_ends`` stand: the owner, or one of the
    two while they have access now."""
    return person.role == "owner" or (person.id in link_ends and is_active(person, now))


async def find(session: AsyncSession, start: UUID, end: UUID, relation: str) -> PersonLink | None:
    """The link ``start`` ``relation`` ``end`` already kept, whatever its case and spacing."""
    wanted = name_key(relation)
    found = await session.scalars(
        select(PersonLink).where(PersonLink.from_id == start, PersonLink.to_id == end)
    )
    return next((link for link in found if name_key(link.relation) == wanted), None)


async def note_link(
    session: AsyncSession,
    *,
    from_person: Person,
    relation: str,
    to_person: Person,
    said_by: Person | None,
    by: str,
    reason: str | None,
    turn_of: UUID | None,
    now: datetime,
) -> PersonLink:
    """``from_person`` is ``relation`` ``to_person``, said by ``said_by`` (``None``: the assistant
    deduced it). It stands at once only when the call is a person's own request and ``said_by`` is
    the owner, or is one of the two and has access now; an act of the assistant's never makes it
    stand, whatever ``said_by`` says. Otherwise it waits for ``confirm``. The same link already
    kept is returned (and made to stand if this word makes it stand). ``NotPossible``:
    ``not_a_record`` (an end, or ``said_by``), ``same_record``, ``bad_relation``. Flushed."""
    _a_record(from_person)
    _a_record(to_person)
    if said_by is not None:
        _a_record(said_by)
    _needs_a_reason(by, reason)
    if from_person.id == to_person.id:
        raise NotPossible("same_record")
    words = plain_relation(relation)
    ends = (from_person.id, to_person.id)
    # The assistant's own act never makes a link stand, whoever he says told him: that is the own
    # request of one who can know (the owner, an end of the link), in their turn.
    stands = by != DOMO_LABEL and said_by is not None and _knows(said_by, ends, now)
    kept = await find(session, from_person.id, to_person.id, words)
    if kept is not None:
        if stands and kept.confirmed_at is None:
            kept.confirmed_at = now
            await session.flush()
            await _trace(session, kept, by=by, reason=reason, turn_of=turn_of, now=now)
        return kept
    link = PersonLink(
        from_id=from_person.id,
        to_id=to_person.id,
        relation=words,
        said_by_id=None if said_by is None else said_by.id,
        confirmed_at=now if stands else None,
    )
    session.add(link)
    await session.flush()
    await _trace(session, link, by=by, reason=reason, turn_of=turn_of, now=now)
    return link


async def _trace(
    session: AsyncSession,
    link: PersonLink,
    *,
    by: str,
    reason: str | None,
    turn_of: UUID | None,
    now: datetime,
    act: Act = "linked",
) -> None:
    await note_act(
        session,
        act=act,
        by=by,
        reason=reason,
        turn_of=turn_of,
        person_id=link.from_id,
        other_id=link.to_id,
        now=now,
    )


async def confirm(
    session: AsyncSession, link: PersonLink, *, by_person: Person, now: datetime
) -> bool:
    """The link stands, by the word of ``by_person``: the owner, or the one of the two who has
    access now. ``False`` (nothing changes) for anyone else; ``True`` when it stands, as it
    already did or as of now. Flushed."""
    if not _knows(by_person, (link.from_id, link.to_id), now):
        return False
    if link.confirmed_at is None:
        link.confirmed_at = now
        await session.flush()
        await _trace(
            session, link, by=f"person:{by_person.id}", reason=None, turn_of=by_person.id, now=now
        )
    return True


async def drop(
    session: AsyncSession,
    link: PersonLink,
    *,
    by: str,
    reason: str | None,
    turn_of: UUID | None,
    now: datetime,
) -> None:
    """The link goes: it was wrong, or no longer true. Its trace stays. Flushed."""
    _needs_a_reason(by, reason)
    await session.delete(link)
    await session.flush()
    await _trace(session, link, by=by, reason=reason, turn_of=turn_of, now=now, act="unlinked")


async def links_of(session: AsyncSession, person_id: UUID) -> list[PersonLink]:
    """The links of a record, both the ones that start from it and the ones that end at it, oldest
    first."""
    found = await session.scalars(
        select(PersonLink)
        .where(or_(PersonLink.from_id == person_id, PersonLink.to_id == person_id))
        .order_by(PersonLink.created_at, PersonLink.id)
    )
    return list(found)


async def related(
    session: AsyncSession, to_id: UUID, *, confirmed_only: bool
) -> list[tuple[Person, str]]:
    """Who is what to that person: each record that ``relation`` ``to_id``, with the relation
    ("Dana", "mother of"), oldest link first. Only the links that stand with ``confirmed_only``.
    A record that is gone is left out."""
    query = (
        select(Person, PersonLink.relation)
        .join(PersonLink, PersonLink.from_id == Person.id)
        .where(PersonLink.to_id == to_id, Person.forgotten_at.is_(None))
        .order_by(PersonLink.created_at, PersonLink.id)
    )
    if confirmed_only:
        query = query.where(PersonLink.confirmed_at.is_not(None))
    return [(person, relation) for person, relation in await session.execute(query)]
