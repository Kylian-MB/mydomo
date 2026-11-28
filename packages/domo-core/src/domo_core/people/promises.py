"""Access promised to someone the assistant does not know yet, and kept the day he recognises them.

The owner says "give access to my partner and her children": nobody can be given anything yet, so
the assistant notes the promise (``promise``): what it is, the role and where they stand, who asked
and whether it was the owner's order. The day he recognises someone it is for, he opens their record
and keeps the promise (``keep``): the access is given by ``people.records.give_access``, the one
place that gives it, so the owner's withdrawal or refusal (``UnderDirective``), the child mark and
the judge hold as for any access. A promise is kept for one person or several ("her children"):
each keeping leaves its own act, and ``close`` ends the promise once the last is kept. A promise
that is no longer wanted is dropped (``drop``). A promise ends once, kept or dropped: the owner's
withdrawal of someone it was kept for takes their access back and leaves the promise as it is.

What a waiting promise names stays: a role made for the household that a promise that waits names
is never deleted (``people.made_roles``).

Whose act it is follows ``people.whose``: ``by`` is ``domo`` (with his reason) or ``person:<id>``.
The functions here flush and do not commit: the caller does.
"""

from datetime import datetime
from typing import Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person, Standing
from domo_core.household.service import GUEST_DAYS_DEFAULT, MemberRole
from domo_core.llm.types import JsonObject
from domo_core.people import made_roles, records
from domo_core.people.acts import note_act
from domo_core.people.models import AccessPromise, MadeRole, PersonAct
from domo_core.policy.roles import DOMO_LABEL

__all__ = [
    "MAX_DESCRIPTION_LENGTH",
    "PromiseOver",
    "close",
    "drop",
    "keep",
    "promise",
    "shown",
    "waiting",
]

MAX_DESCRIPTION_LENGTH: Final = 300
_STANDINGS: Final = ("lives_here", "visiting", "elsewhere")


class PromiseOver(records.NotPossible):
    """The promise was kept for its last person or dropped already: nobody is kept by it."""

    def __init__(self) -> None:
        super().__init__("promise_over")


def _needs_a_reason(by: str, reason: str | None) -> None:
    if by == DOMO_LABEL and not reason:
        raise ValueError("an act of the assistant has its reason")


def _is_over(promised: AccessPromise) -> bool:
    return promised.kept_at is not None or promised.dropped_at is not None


async def promise(
    session: AsyncSession,
    *,
    description: str,
    role: MemberRole,
    made_role: MadeRole | None,
    standing: Standing,
    guest_days: int | None,
    asked_by: str,
    is_order: bool,
    reason: str | None,
    turn_of: UUID | None,
    now: datetime,
) -> AccessPromise:
    """Note that access is promised to someone not known yet: ``description`` says who ("my
    partner and her children"), ``role`` (and ``made_role``, whose base it is) what they will have,
    ``standing`` where they stand, ``guest_days`` how long a guest stays (a guest only; the default
    stay when none). ``asked_by`` is who asked (``domo`` or ``person:<id>``), ``is_order`` whether
    it is the owner's own order (the owner's request, in their turn: never the assistant's idea),
    ``reason`` the assistant's own (a person's has none). ``ValueError`` for a description that is
    empty or too long, a role that disagrees with its made role, a made role no longer in use,
    days for someone who is not a guest, an unknown standing. Flushed."""
    _needs_a_reason(asked_by, reason)
    text = " ".join(description.split())
    if not text or len(text) > MAX_DESCRIPTION_LENGTH:
        raise ValueError(f"the description is 1 to {MAX_DESCRIPTION_LENGTH} characters")
    if standing not in _STANDINGS:
        raise ValueError("standing is lives_here, visiting or elsewhere")
    if made_role is not None and (made_role.base != role or made_role.deleted_at is not None):
        raise ValueError("a made role is promised with its base role, and while it is in use")
    if guest_days is not None and (role != "guest" or guest_days < 1):
        raise ValueError("days are for a guest, one day or more")
    row = AccessPromise(
        description=text,
        role=role,
        made_role_id=None if made_role is None else made_role.id,
        standing=standing,
        guest_days=guest_days,
        asked_by=asked_by,
        is_order=is_order,
        reason=reason or made_roles.ASKED,
        created_at=now,
    )
    session.add(row)
    await session.flush()
    await note_act(
        session,
        act="promised",
        by=asked_by,
        reason=reason,
        turn_of=turn_of,
        promise_id=row.id,
        now=now,
    )
    return row


async def waiting(session: AsyncSession) -> list[AccessPromise]:
    """The promises that wait: neither kept for their last person nor dropped, the oldest first."""
    found = await session.scalars(
        select(AccessPromise)
        .where(AccessPromise.kept_at.is_(None), AccessPromise.dropped_at.is_(None))
        .order_by(AccessPromise.created_at, AccessPromise.id)
    )
    return list(found)


async def keep(
    session: AsyncSession,
    promised: AccessPromise,
    person: Person,
    *,
    by: str,
    reason: str | None,
    turn_of: UUID | None,
    now: datetime,
    seen: bool = True,
    by_owner: bool = False,
) -> Person:
    """Keep ``promised`` for ``person``: they have access from now with the role and the standing it
    names, given by ``people.records.give_access`` (so the owner's withdrawal or refusal stops it,
    ``records.UnderDirective``, and what the code holds there holds here: the owner is not asked
    again). The act ``promise_kept`` is written for them. The promise stays open for the next
    person until ``close``. Someone who has access already is given the role it names, as a change
    of role: what they say of themselves gives nothing by itself, and the judge weighs it (the
    fact ``serves_the_speaker``). ``PromiseOver`` when it ended; ``NotPossible("the_owner")`` for
    the owner, whose role is never given. Flushed."""
    _needs_a_reason(by, reason)
    if _is_over(promised):
        raise PromiseOver
    if person.role == "owner":
        raise records.NotPossible("the_owner")
    made = (
        None
        if promised.made_role_id is None
        else await made_roles.by_id(session, promised.made_role_id)
    )
    days = promised.guest_days
    if promised.role == "guest" and days is None:
        days = GUEST_DAYS_DEFAULT
    given = await records.give_access(
        session,
        person,
        role=promised.role,
        made_role=made,
        standing=promised.standing,  # type: ignore[arg-type]
        guest_days=days,
        by=by,
        # The owner's own keeping (``by_owner``) gives back what he withdrew, as his invitation
        # does.
        by_owner=by_owner,
        reason=reason,
        turn_of=turn_of,
        now=now,
        seen=seen,
    )
    promised.kept_person_id = person.id
    await session.flush()
    await note_act(
        session,
        act="promise_kept",
        by=by,
        reason=reason,
        turn_of=turn_of,
        person_id=person.id,
        promise_id=promised.id,
        now=now,
    )
    return given


async def close(session: AsyncSession, promised: AccessPromise, *, now: datetime) -> None:
    """The promise is kept for its last person: it ends, once. ``PromiseOver`` when it ended
    already, ``NotPossible("not_kept")`` when it was kept for nobody. Flushed."""
    if _is_over(promised):
        raise PromiseOver
    if promised.kept_person_id is None:
        raise records.NotPossible("not_kept")
    promised.kept_at = now
    await session.flush()


async def drop(
    session: AsyncSession,
    promised: AccessPromise,
    *,
    by: str,
    reason: str | None,
    turn_of: UUID | None,
    now: datetime,
) -> None:
    """The promise is no longer wanted: it ends, once, and keeps nobody. What was given by keeping
    it stays (the owner takes it away as for any access). ``PromiseOver`` when it ended already.
    Flushed."""
    _needs_a_reason(by, reason)
    if _is_over(promised):
        raise PromiseOver
    promised.dropped_at = now
    promised.dropped_by = by
    await session.flush()
    await note_act(
        session,
        act="promise_dropped",
        by=by,
        reason=reason,
        turn_of=turn_of,
        promise_id=promised.id,
        now=now,
    )


async def _name_of(session: AsyncSession, label: str) -> str:
    """Who an author label (``domo`` or ``person:<id>``) names, as the assistant reads it."""
    if label == DOMO_LABEL:
        return "the assistant"
    person = await session.get(Person, UUID(label.removeprefix("person:")))
    return "someone" if person is None else person.display_name


async def shown(session: AsyncSession) -> list[JsonObject]:
    """The promises that wait, as data for the assistant (``waiting``): their identifier, what is
    promised and to whom, the role and where they stand, who asked and whether it was the owner's
    order, since when, and who it was kept for so far."""
    found: list[JsonObject] = []
    for row in await waiting(session):
        made = (
            None if row.made_role_id is None else await made_roles.by_id(session, row.made_role_id)
        )
        kept_for = await session.scalars(
            select(Person.display_name)
            .join(PersonAct, PersonAct.person_id == Person.id)
            .where(PersonAct.act == "promise_kept", PersonAct.promise_id == row.id)
            .order_by(PersonAct.at, PersonAct.id)
        )
        found.append(
            {
                "id": str(row.id),
                "description": row.description,
                "role": row.role if made is None else made.name,
                "standing": row.standing,
                "days": row.guest_days,
                "asked_by": await _name_of(session, row.asked_by),
                "is_order": row.is_order,
                "since": row.created_at.isoformat(),
                "kept_for": list(kept_for),
            }
        )
    return found
