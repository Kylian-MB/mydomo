"""The roles the assistant makes: a name, a built-in base, and what they add or take away.

A made role starts from the rights of a built-in base (an adult of the house, a child, someone
passing by) and gives (``allow``) or denies (``deny``) action types beside it. It never holds what
is above its ceiling (``catalog.ROLE_CEILING``: the owner's own, spending limits, the gestures on
people, what is closed to the assistant himself): refused here when it is written
(``AboveTheCeiling``), and ignored when it is read (``policy.rights.RightsTable.resolve``). It never
takes the name of a built-in role, nor the owner's (``RoleNameTaken``). Deleted, it is kept, marked,
and whoever carried it goes back to its base; it is not deleted while a promise that waits names it
(``RoleInUse``).

Each change leaves its reason in ``person_acts`` and its line in the audit log
(``people.acts.note_act``: ``role_created``, ``role_changed``, ``role_deleted``); an act a person
asked for has no reason of its own (the role keeps ``ASKED`` in its ``reason`` column, which cannot
be empty). A role nobody carries any more is deleted by ``people.records.leave_role``, or, never
given, by ``unused`` after a few days. The functions here flush and do not commit: the caller does.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Literal
from uuid import UUID

from sqlalchemy import ColumnElement, select
from sqlalchemy import delete as delete_rows
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from domo_core.accounts.models import Person
from domo_core.people.acts import note_act
from domo_core.people.models import Access, AccessPromise, Act, MadeRole, RoleAccess
from domo_core.people.news import news_for
from domo_core.policy.catalog import ROLE_CEILING, ROLE_MAY_DENY
from domo_core.policy.open_catalog import load_policy
from domo_core.policy.roles import DOMO_LABEL, ROLES

# What the role keeps as its reason when a person asked for it (the column is never empty).
ASKED = "asked by a person"
# The reason of the deletion of a role nobody carries any more.
UNUSED = "unused"


class AboveTheCeiling(Exception):
    """A made role was to give what no made role holds: ``types``, sorted."""

    def __init__(self, types: Sequence[str]) -> None:
        self.types: tuple[str, ...] = tuple(sorted(types))
        super().__init__(f"above the ceiling of a made role: {', '.join(self.types)}")


class RoleInUse(Exception):
    """A made role a promise of access that waits names cannot be deleted."""


class RoleNameTaken(Exception):
    """The name is a built-in role's, the owner's, or another made role's."""


class OwnersRule(Exception):
    """A line the owner said for the role is in the way: only his own request changes it, or
    deletes a role that holds one. ``types``: the action types of his lines, sorted."""

    def __init__(self, types: Sequence[str]) -> None:
        self.types: tuple[str, ...] = tuple(sorted(types))
        super().__init__(f"the owner's rule on the role: {', '.join(self.types)}")


@dataclass(frozen=True)
class OwnerLine:
    """What the owner says of one action type for a role (beside allow or deny): a limit (with
    the question to its judge), whose agreement first, what to ask before, and its words."""

    limit_expression: str | None = None
    judge_question: str | None = None
    approver: Literal["self", "adult", "owner"] | None = None
    ask: Literal["yes", "explicit_yes", "proof"] | None = None
    summary: str | None = None


async def owners_lines(session: AsyncSession, role_id: UUID) -> dict[str, RoleAccess]:
    """The lines the owner said for the role, by action type."""
    found = await session.scalars(
        select(RoleAccess).where(RoleAccess.role_id == role_id, RoleAccess.by_owner.is_(True))
    )
    return {line.action_type: line for line in found}


def name_key(name: str) -> str:
    """``name`` as it is compared: its words, single-spaced, case-folded."""
    return " ".join(name.split()).casefold()


async def check_types(
    session: AsyncSession, allow: Sequence[str], deny: Sequence[str], forget: Sequence[str] = ()
) -> None:
    """``ValueError`` for a type the catalog does not know, or one both given and denied;
    ``AboveTheCeiling`` for a type given or denied above the ceiling (a role never says anything
    of it: what it said would be ignored when read)."""
    known = (await load_policy(session)).known
    unknown = sorted({*allow, *deny, *forget} - known)
    if unknown:
        raise ValueError(f"not action types of the catalog: {', '.join(unknown)}")
    both = sorted(set(allow) & set(deny))
    if both:
        raise ValueError(f"both given and denied: {', '.join(both)}")
    above = ({*allow} | ({*deny} - ROLE_MAY_DENY)) & ROLE_CEILING
    if above:
        raise AboveTheCeiling(sorted(above))


async def _free_name(session: AsyncSession, name: str, *, besides: UUID | None = None) -> str:
    """The key of ``name`` when no built-in role, the owner, nor another made role in use has it;
    ``RoleNameTaken`` otherwise."""
    key = name_key(name)
    if not key:
        raise ValueError("a role has a name")
    if key in ROLES:
        raise RoleNameTaken(name)
    other = await session.scalar(
        select(MadeRole.id).where(MadeRole.name_key == key, MadeRole.deleted_at.is_(None))
    )
    if other is not None and other != besides:
        raise RoleNameTaken(name)
    return key


def _needs_a_reason(by: str, reason: str | None) -> None:
    if by == DOMO_LABEL and not reason:
        raise ValueError("an act of the assistant has its reason")


async def _trace(
    session: AsyncSession,
    act: Act,
    role: MadeRole,
    *,
    by: str,
    reason: str | None,
    turn_of: UUID | None,
    now: datetime,
) -> None:
    await note_act(
        session, act=act, by=by, reason=reason, turn_of=turn_of, role_id=role.id, now=now
    )


def carries(role_id: UUID | InstrumentedAttribute[UUID]) -> ColumnElement[bool]:
    """A record that carries the role: one that is still a record (not merged away, not
    forgotten)."""
    return (
        (Person.made_role_id == role_id)
        & Person.merged_into_id.is_(None)
        & Person.forgotten_at.is_(None)
    )


def _waiting(role_id: UUID | InstrumentedAttribute[UUID]) -> ColumnElement[bool]:
    """A promise of access that names the role and is neither kept nor dropped."""
    return (
        (AccessPromise.made_role_id == role_id)
        & AccessPromise.kept_at.is_(None)
        & AccessPromise.dropped_at.is_(None)
    )


async def is_wanted(session: AsyncSession, role: MadeRole, *, besides: UUID | None = None) -> bool:
    """Whether a record other than ``besides`` carries the role, a promise that waits names it,
    or the owner said a line for it (his rule stays, for whoever gets the role later)."""
    if await session.scalar(select(_owners_line(role.id)).limit(1)):
        return True
    carrier = select(Person.id).where(carries(role.id)).limit(1)
    if besides is not None:
        carrier = carrier.where(Person.id != besides)
    if await session.scalar(carrier) is not None:
        return True
    promise = select(AccessPromise.id).where(_waiting(role.id)).limit(1)
    return await session.scalar(promise) is not None


def _owners_line(role_id: UUID | InstrumentedAttribute[UUID]) -> ColumnElement[bool]:
    """A line the owner said for the role exists."""
    return (
        select(RoleAccess.action_type)
        .where(RoleAccess.role_id == role_id, RoleAccess.by_owner.is_(True))
        .exists()
    )


async def _set_accesses(
    session: AsyncSession,
    role_id: UUID,
    given: Mapping[str, Access],
    *,
    by_owner: bool = False,
    lines: Mapping[str, OwnerLine] | None = None,
) -> None:
    """Write ``given`` on the role, each replacing what the role said of that type; the owner's
    (``by_owner``) with what he said more of each (``lines``)."""
    if not given:
        return
    await session.execute(
        delete_rows(RoleAccess).where(
            RoleAccess.role_id == role_id, RoleAccess.action_type.in_(list(given))
        )
    )
    said = lines or {}
    session.add_all(
        RoleAccess(
            role_id=role_id,
            action_type=action_type,
            access=access,
            by_owner=by_owner,
            limit_expression=said[action_type].limit_expression if action_type in said else None,
            judge_question=said[action_type].judge_question if action_type in said else None,
            approver=said[action_type].approver if action_type in said else None,
            ask=said[action_type].ask if action_type in said else None,
            summary=said[action_type].summary if action_type in said else None,
        )
        for action_type, access in given.items()
    )


def _given(allow: Sequence[str], deny: Sequence[str]) -> dict[str, Access]:
    return {**dict.fromkeys(allow, "allow"), **dict.fromkeys(deny, "deny")}


async def create(
    session: AsyncSession,
    *,
    name: str,
    base: Literal["adult", "child", "guest"],
    allow: Sequence[str],
    deny: Sequence[str],
    by: str,
    reason: str | None,
    turn_of: UUID | None = None,
    now: datetime,
) -> MadeRole:
    """A new role named ``name``, from ``base``, giving ``allow`` and denying ``deny`` beside
    it; ``by`` who made it (``domo`` or ``person:<id>``) and why (``reason``: the assistant's own
    act has one, a person's has none). ``RoleNameTaken``, ``ValueError`` (a type the catalog does
    not know), ``AboveTheCeiling``. Flushed."""
    _needs_a_reason(by, reason)
    key = await _free_name(session, name)
    await check_types(session, allow, deny)
    role = MadeRole(
        name=" ".join(name.split()),
        name_key=key,
        base=base,
        created_by=by,
        reason=reason or ASKED,
        created_at=now,
    )
    session.add(role)
    await session.flush()
    await _set_accesses(session, role.id, _given(allow, deny))
    await _trace(session, "role_created", role, by=by, reason=reason, turn_of=turn_of, now=now)
    return role


async def change(
    session: AsyncSession,
    role: MadeRole,
    *,
    name: str | None,
    allow: Sequence[str],
    deny: Sequence[str],
    forget: Sequence[str],
    by: str,
    reason: str | None,
    turn_of: UUID | None = None,
    now: datetime,
    by_owner: bool = False,
    lines: Mapping[str, OwnerLine] | None = None,
) -> MadeRole:
    """``role`` renamed (``name``, ``None``: as it is), giving ``allow`` and denying ``deny``
    from now on, and saying nothing any more of ``forget`` (the base decides them again). The
    same refusals as ``create``. The owner's own change (``by_owner``) writes his lines (his rule
    for the role, with what he said more of each: ``lines``); anyone else's never touches one of
    them (``OwnersRule``). Flushed."""
    _needs_a_reason(by, reason)
    if role.deleted_at is not None:
        raise ValueError("a deleted role is not changed")
    if not by_owner:
        his = set(await owners_lines(session, role.id)) & {*allow, *deny, *forget}
        if his:
            raise OwnersRule(sorted(his))
    key = None if name is None else await _free_name(session, name, besides=role.id)
    await check_types(session, allow, deny, forget)
    if name is not None and key is not None:
        role.name_key = key
        role.name = " ".join(name.split())
    if forget:
        await session.execute(
            delete_rows(RoleAccess).where(
                RoleAccess.role_id == role.id, RoleAccess.action_type.in_(list(forget))
            )
        )
    await _set_accesses(session, role.id, _given(allow, deny), by_owner=by_owner, lines=lines)
    await _trace(session, "role_changed", role, by=by, reason=reason, turn_of=turn_of, now=now)
    return role


async def delete(
    session: AsyncSession,
    role: MadeRole,
    *,
    by: str,
    reason: str | None,
    turn_of: UUID | None = None,
    now: datetime,
    by_owner: bool = False,
) -> list[UUID]:
    """``role`` deleted (kept, marked) at once: each record that carries it goes back to the
    role's base (its built-in role, which it already holds) and is told (its news are kept); the
    owner's answer of 2026-11-27. ``RoleInUse`` while a promise of access that waits names it.
    Returns the ids of the records that carried it. A role that holds a line the owner said is
    deleted only on his own request (``by_owner``; ``OwnersRule`` otherwise). Flushed."""
    _needs_a_reason(by, reason)
    if not by_owner:
        his = await owners_lines(session, role.id)
        if his:
            raise OwnersRule(sorted(his))
    if await session.scalar(select(AccessPromise.id).where(_waiting(role.id)).limit(1)):
        raise RoleInUse(role.name)
    holders = list(await session.scalars(select(Person).where(carries(role.id))))
    for holder in holders:
        holder.made_role_id = None
    role.deleted_at = now
    await session.flush()
    await _trace(session, "role_deleted", role, by=by, reason=reason, turn_of=turn_of, now=now)
    for holder in holders:
        await news_for(session, holder.id, now=now)
    return [holder.id for holder in holders]


async def unused(session: AsyncSession, *, before: datetime) -> list[MadeRole]:
    """The roles in use that nobody carries (merged and forgotten records do not count), that no
    waiting promise names, and that were made before ``before``: oldest first."""
    carried = select(Person.id).where(carries(MadeRole.id))
    promised = select(AccessPromise.id).where(_waiting(MadeRole.id))
    found = await session.scalars(
        select(MadeRole)
        .where(
            MadeRole.deleted_at.is_(None),
            MadeRole.created_at < before,
            ~carried.exists(),
            ~promised.exists(),
            ~_owners_line(MadeRole.id),
        )
        .order_by(MadeRole.created_at, MadeRole.id)
    )
    return list(found)


async def named(session: AsyncSession, name: str) -> MadeRole | None:
    """The made role in use whose name is ``name`` (compared as ``name_key`` does)."""
    return await session.scalar(
        select(MadeRole).where(MadeRole.name_key == name_key(name), MadeRole.deleted_at.is_(None))
    )


async def by_id(session: AsyncSession, role_id: UUID) -> MadeRole | None:
    """The made role ``role_id``, deleted or not."""
    return await session.get(MadeRole, role_id)


async def kept_at_return(session: AsyncSession, person: Person, base: str) -> MadeRole | None:
    """The made role ``person`` carries and comes back with through a return link of the built-in
    role ``base``: the assistant's removal keeps it for a return, so it applies again while it
    exists and its base is the link's role; ``None`` otherwise (it is then left at the return)."""
    if person.made_role_id is None:
        return None
    role = await by_id(session, person.made_role_id)
    if role is None or role.deleted_at is not None or role.base != base:
        return None
    return role


async def all_roles(session: AsyncSession) -> list[MadeRole]:
    """The made roles in use, oldest first."""
    result = await session.scalars(
        select(MadeRole)
        .where(MadeRole.deleted_at.is_(None))
        .order_by(MadeRole.created_at, MadeRole.id)
    )
    return list(result)


async def accesses_of(session: AsyncSession, role_id: UUID) -> Mapping[str, Access]:
    """What the made role ``role_id`` gives or denies, by action type."""
    result = await session.execute(
        select(RoleAccess.action_type, RoleAccess.access).where(RoleAccess.role_id == role_id)
    )
    return MappingProxyType({action_type: access for action_type, access in result.all()})
