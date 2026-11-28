"""Two records of one person made one (``merge``), split again within ``MERGE_KEPT`` (``split``),
and the trace emptied once that time is past (``purge``).

A merge moves to the kept record **everything that points at the absorbed one**: every column of
every table that names a record (read from the metadata, so that a table added later moves too),
but the trace of the acts done on each (``person_acts``) and the traces of merges themselves. That
covers the memories (about them and said by them), the nicknames (kept for them and kept by them),
the links, the trust facts, the owner's personal rights, the promises kept for them, the requests
that wait, the plans, the devices, a place on the successors' list, the keys they gave and their
invitations. The ids of what moved are written in the trace (``RecordMerge.moved``), with the
absorbed record's state, so that a split puts back exactly that; what was added to the kept record
since stays on it. A link that waits for the absorbed record (to join, or to come back) is
cancelled first: it opens nothing once the record is merged (Review Focus 5).

A row that cannot move without breaking a rule of its table stays where it is, on the absorbed
record, and the trace says so (``stayed``): a link that would join the record to itself, the same
nickname kept twice by one person, a second account. Where two rows say how far the person may go,
the stricter one is the kept record's: of two rights of one person on the same type, the stricter
is in force on the kept record (the other is revoked until a split); of two spending limits on one
key, the lower; of two places on the successors' list, the better rank. What stays is out of every
listing with the absorbed record (it is merged), is there again after a split, and goes when the
trace is emptied. When the kept record is the owner's, nothing that would bind him lands on him:
the other record's personal rights and spending limits stay with it, and its child mark is not
his.

A split gives back an access the absorbed record had only through ``records.give_access`` (its
act, the owner's directives): when that door says no, the records are two again and the access
stays off.

Where the kept record stands afterwards: when the owner's withdrawal or refusal is in force for
either, the stricter of the two (``refused`` > ``withdrawn`` > ``gone`` > ``known`` >
``has_access``), with what the directive does to a record (``directives.apply_in_force``: sessions
and devices ended, links and requests that wait cancelled, rights paused); the directive moves to
the kept record. A record forgotten under such a directive is still merged, always as the absorbed
one: the living record is kept and carries it (``people.forget``). Otherwise the kept record's
own, or the absorbed record's access when only it had one. A record that was ever given an access
stays one that was (its first access is the earlier of the two). When only the absorbed record
has an account, the two are swapped, so that the account's record is the kept one; when each has
one (the owner's decision of 2026-11-26: hard to undo, so the assistant must be sure, which is his
judgement), the kept record's account stays and the other is closed: its sessions and devices end,
and it no longer signs in, since its record is merged; a split within 90 days brings it back.

The functions flush and do not commit: the caller does.
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from functools import cache
from typing import Any, Final, cast

from sqlalchemy import Column, Table, and_, delete, func, select, tuple_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Account, Person
from domo_core.accounts.sessions import is_active, revoke_sessions_of
from domo_core.db import Base, load_models
from domo_core.devices.service import forget_devices_of
from domo_core.household.models import Invitation, Successor
from domo_core.household.service import SUCCESSORS_LOCK_KEY, MemberRole, off_the_successors
from domo_core.llm.spending_models import SpendingLimit
from domo_core.people import directives, forget, made_roles, records
from domo_core.people.acts import note_act
from domo_core.people.models import (
    ChannelRef,
    MadeRole,
    OwnerDirective,
    PersonAct,
    RecordMerge,
)
from domo_core.policy.models import PolicyRight

__all__ = ["MERGE_KEPT", "Split", "TraceGone", "merge", "purge", "split", "told_to_owner"]

# How long a merge can be split again (ruling 4); the trace is emptied afterwards.
MERGE_KEPT: Final = timedelta(days=90)

# The tables whose rows name a record and never move: the trace of what was done to each record,
# and the traces of merges.
_NEVER_MOVED: Final = frozenset({"person_acts", "record_merges"})
# What of the kept record a merge may change, and a split put back.
_KEPT_COLUMNS: Final = (
    "state",
    "role",
    "made_role_id",
    "expires_at",
    "first_access_at",
    "disabled_at",
    "is_child",
)
# The rows whose collision keeps the stricter of the two on the kept record.
_RIGHTS: Final = "policy_rights.person_id"
_LIMITS: Final = "spending_limits.person_id"
_PLACES: Final = "successors.person_id"
_DIRECTIVES: Final = "owner_directives.person_id"
# What a spending limit says, exchanged between two limits when the absorbed record's is lower.
_LIMIT_VALUES: Final = (
    "amount",
    "currency",
    "period",
    "warn_at_percent",
    "warned_at",
    "domo_warned_at",
    "set_by",
    "set_at",
)
_PERIOD_DAYS: Final = {"day": 1, "week": 7, "month": 30}


class TraceGone(Exception):
    """The merge can no longer be split: its time is past, its trace emptied, or it was split
    already."""


@dataclass(frozen=True)
class Split:
    """What a split gave back: the absorbed record, how many rows by ``table.column`` stayed on
    the kept record (a rule of their table, met since), whether its access was not given back
    (``without_access``: ``made_role_gone``, its made role was deleted and with it its limits),
    whether a closed account is open again (``account_back``: the person signs in again) and how
    many of its devices the merge ended, which stay removed (``devices_stay_removed``: paired
    again if wanted), and the references of a channel that went back to it (``refs_back``,
    ``(channel, reference)``: what a channel is told, ``people.hooks``)."""

    person: Person
    stayed: dict[str, int] = field(default_factory=dict[str, int])
    without_access: str | None = None
    account_back: bool = False
    devices_stay_removed: int = 0
    refs_back: tuple[tuple[str, str], ...] = ()


@cache
def _pointing_columns() -> tuple[Column[Any], ...]:
    """Every column of every table that points at a record, but those that never move."""
    load_models()
    return tuple(
        column
        for table in Base.metadata.sorted_tables
        if table.name not in _NEVER_MOVED
        for column in table.columns
        if any(key.column.table.name == "persons" for key in column.foreign_keys)
    )


def _where(column: Column[Any]) -> str:
    return f"{column.table.name}.{column.name}"


def _column_named(where: str) -> Column[Any] | None:
    return next((column for column in _pointing_columns() if _where(column) == where), None)


def _key_of(row: Sequence[Any]) -> list[str]:
    """The primary key of a row, as the trace keeps it: texts."""
    return [str(value) for value in row]


def _read_key(table: Table, key: Sequence[str]) -> tuple[Any, ...]:
    """A primary key the trace kept, read back with the types of its columns."""
    values: list[Any] = []
    for column, text in zip(table.primary_key.columns, key, strict=True):
        kind = column.type.python_type
        values.append(kind(text) if kind in (uuid.UUID, int) else text)
    return tuple(values)


async def _repoint(
    session: AsyncSession,
    column: Column[Any],
    *,
    to: uuid.UUID,
    where: Any,
) -> tuple[list[list[str]], list[list[str]]]:
    """Point at ``to`` the rows that ``where`` selects: all at once when the table's rules allow
    it, else one by one. Returns the primary keys of the rows moved (as they are after the move)
    and of those that stayed (a rule of the table would break)."""
    table = column.table
    key = list(table.primary_key.columns)
    statement = update(table).where(where).values({column.name: to}).returning(*key)
    try:
        async with session.begin_nested():
            moved = (await session.execute(statement)).all()
        return [_key_of(row) for row in moved], []
    except IntegrityError:
        pass
    rows = (await session.execute(select(*key).where(where))).all()
    moved_keys: list[list[str]] = []
    stayed: list[list[str]] = []
    for row in rows:
        one = tuple_(*key) == tuple(row)
        try:
            async with session.begin_nested():
                found = (
                    await session.execute(
                        update(table).where(one).values({column.name: to}).returning(*key)
                    )
                ).one()
            moved_keys.append(_key_of(found))
        except IntegrityError:
            stayed.append(_key_of(row))
    return moved_keys, stayed


async def _rows_on(
    session: AsyncSession, person_id: uuid.UUID, wheres: Sequence[str]
) -> dict[str, list[list[str]]]:
    """The primary keys of the rows of ``wheres`` (``table.column``) that point at the record."""
    found: dict[str, list[list[str]]] = {}
    for where in wheres:
        column = _column_named(where)
        if column is None:
            continue
        rows = await session.execute(
            select(*column.table.primary_key.columns).where(column == person_id)
        )
        keys = [_key_of(row) for row in rows.all()]
        if keys:
            found[where] = keys
    return found


async def _move_everything(
    session: AsyncSession, absorbed: Person, kept: Person, *, skip: frozenset[str] = frozenset()
) -> tuple[dict[str, list[list[str]]], dict[str, list[list[str]]]]:
    """Point at ``kept`` every row that points at ``absorbed`` (but the traces and the columns of
    ``skip``): what moved and what stayed, by ``table.column``."""
    moved: dict[str, list[list[str]]] = {}
    stayed: dict[str, list[list[str]]] = {}
    for column in _pointing_columns():
        if _where(column) in skip:
            continue
        where = column == absorbed.id
        if column.table.name == "persons":
            # The two records themselves are left as they are.
            where = and_(where, column.table.c.id.not_in((kept.id, absorbed.id)))
        went, left = await _repoint(session, column, to=kept.id, where=where)
        if went:
            moved[_where(column)] = went
        if left:
            stayed[_where(column)] = left
    return moved, stayed


# --- the stricter of two rows that collide -------------------------------------------------------


def _right_weight(right: PolicyRight) -> int:
    """How far a right holds the person back: a deny, then a right that asks someone first, then
    one with a limit, then a plain allow."""
    if right.access == "deny":
        return 3
    if right.approver is not None or right.ask is not None:
        return 2
    return 1 if right.limit_expression is not None else 0


def _per_day(limit: SpendingLimit) -> Decimal:
    return limit.amount / _PERIOD_DAYS.get(limit.period, 1)


async def _stricter_rights(
    session: AsyncSession, ids: Sequence[uuid.UUID], kept: uuid.UUID, now: datetime
) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
    """Of the absorbed record's rights that met a right of the kept record on the same type, those
    that are stricter move, and the kept record's is revoked: the ids moved, and those revoked."""
    moved: list[uuid.UUID] = []
    revoked: list[uuid.UUID] = []
    for right_id in ids:
        theirs = await session.get(PolicyRight, right_id)
        if theirs is None:
            continue
        ours = await session.scalar(
            select(PolicyRight).where(
                PolicyRight.person_id == kept,
                PolicyRight.role.is_not_distinct_from(theirs.role),
                PolicyRight.for_domo == theirs.for_domo,
                PolicyRight.action_type == theirs.action_type,
                PolicyRight.revoked_at.is_(None),
            )
        )
        if ours is None or _right_weight(theirs) <= _right_weight(ours):
            continue
        ours.revoked_at = now
        await session.flush()
        theirs.person_id = kept
        await session.flush()
        moved.append(theirs.id)
        revoked.append(ours.id)
    return moved, revoked


async def _swap_limit_values(session: AsyncSession, one: uuid.UUID, other: uuid.UUID) -> bool:
    first = await session.get(SpendingLimit, one)
    second = await session.get(SpendingLimit, other)
    if first is None or second is None:
        return False
    for name in _LIMIT_VALUES:
        values = getattr(first, name), getattr(second, name)
        setattr(first, name, values[1])
        setattr(second, name, values[0])
    await session.flush()
    return True


async def _lower_limits(
    session: AsyncSession, ids: Sequence[uuid.UUID], kept: uuid.UUID
) -> tuple[list[list[str]], list[list[str]]]:
    """Of the absorbed record's spending limits that met one of the kept record on the same key:
    in the same currency, a lower one per day (a week's counted over 7 days, a month's over 30)
    becomes the kept record's (the two limits exchange what they say); in another currency, the
    kept record's holds, since no rate is read here, and the other is dropped (the owner is told
    of it). The pairs ``[kept's, absorbed's]`` exchanged, and those dropped."""
    swapped: list[list[str]] = []
    dropped: list[list[str]] = []
    for limit_id in ids:
        theirs = await session.get(SpendingLimit, limit_id)
        if theirs is None:
            continue
        ours = await session.scalar(
            select(SpendingLimit).where(
                SpendingLimit.person_id == kept,
                SpendingLimit.provider_id == theirs.provider_id,
                SpendingLimit.role.is_(None),
            )
        )
        if ours is None:
            continue
        if ours.currency != theirs.currency:
            dropped.append([str(ours.id), str(theirs.id)])
        elif _per_day(theirs) < _per_day(ours):
            await _swap_limit_values(session, ours.id, theirs.id)
            swapped.append([str(ours.id), str(theirs.id)])
    return swapped, dropped


async def _swap_ranks(session: AsyncSession, one: uuid.UUID, other: uuid.UUID) -> bool:
    """Exchange the ranks of two places of the successors' list (the column is unique: through a
    free rank)."""
    await session.execute(select(func.pg_advisory_xact_lock(SUCCESSORS_LOCK_KEY)))
    first = await session.get(Successor, one)
    second = await session.get(Successor, other)
    if first is None or second is None:
        return False
    free = (await session.scalar(select(func.max(Successor.rank))) or 0) + 1
    ranks = first.rank, second.rank
    first.rank = free
    await session.flush()
    second.rank = ranks[0]
    await session.flush()
    first.rank = ranks[1]
    await session.flush()
    return True


async def _better_places(
    session: AsyncSession, ids: Sequence[uuid.UUID], kept: uuid.UUID
) -> list[list[str]]:
    """The absorbed record's place on the successors' list, when it is better than the kept
    record's, gives the kept record its rank: the pairs ``[kept's, absorbed's]`` exchanged."""
    swapped: list[list[str]] = []
    for place_id in ids:
        theirs = await session.get(Successor, place_id)
        ours = await session.scalar(select(Successor).where(Successor.person_id == kept))
        if theirs is not None and ours is not None and theirs.rank < ours.rank:
            await _swap_ranks(session, ours.id, theirs.id)
            swapped.append([str(ours.id), str(theirs.id)])
    return swapped


def _ids(stayed: Mapping[str, list[list[str]]], where: str) -> list[uuid.UUID]:
    return [uuid.UUID(key[0]) for key in stayed.get(where, [])]


async def _keep_the_stricter(
    session: AsyncSession,
    moved: dict[str, list[list[str]]],
    stayed: dict[str, list[list[str]]],
    kept: uuid.UUID,
    now: datetime,
) -> dict[str, Any]:
    """Settle the rows that collided where the stricter must win (rights, limits, places), and
    say how, for a split to undo it."""
    went, revoked = await _stricter_rights(session, _ids(stayed, _RIGHTS), kept, now)
    if went:
        moved.setdefault(_RIGHTS, []).extend([str(one)] for one in went)
        stayed[_RIGHTS] = [key for key in stayed[_RIGHTS] if uuid.UUID(key[0]) not in went]
        if not stayed[_RIGHTS]:
            del stayed[_RIGHTS]
    swapped, dropped = await _lower_limits(session, _ids(stayed, _LIMITS), kept)
    return {
        "revoked_rights": [str(one) for one in revoked],
        "swapped_limits": swapped,
        "dropped_limits": dropped,
        "swapped_places": await _better_places(session, _ids(stayed, _PLACES), kept),
    }


# --- the records ---------------------------------------------------------------------------------


async def _refs_of(session: AsyncSession, person_id: uuid.UUID) -> list[list[str]]:
    """The references of a channel a record holds, ``[channel, reference]``, in order."""
    found = await session.execute(
        select(ChannelRef.channel, ChannelRef.reference)
        .where(ChannelRef.person_id == person_id)
        .order_by(ChannelRef.channel, ChannelRef.reference)
    )
    return [[channel, reference] for channel, reference in found.all()]


async def _has_account(session: AsyncSession, person: Person) -> bool:
    return (
        await session.scalar(select(Account.id).where(Account.person_id == person.id))
    ) is not None


def _text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _columns_of(person: Person) -> dict[str, str | None]:
    return {name: _text(getattr(person, name)) for name in _KEPT_COLUMNS}


def _read_column(name: str, value: str | None) -> object:
    if value is None:
        return None
    if name in ("expires_at", "first_access_at", "disabled_at"):
        return datetime.fromisoformat(value)
    if name == "made_role_id":
        return uuid.UUID(value)
    if name == "is_child":
        return value == "True"
    return value


async def _a_record(session: AsyncSession, person: Person, now: datetime) -> None:
    """``records.NotPossible("not_a_record")`` unless ``person`` may be merged: a record, or one
    forgotten whose withdrawal or refusal is in force, or that keeps a deny of the owner's (what is
    kept to know them again, so that someone who comes back under another name is found to be
    them)."""
    if person.merged_into_id is not None:
        raise records.NotPossible("not_a_record")
    if person.forgotten_at is not None and not (
        await directives.under_directive(session, person.id, now) is not None
        or await forget.owners_deny_kept(session, person.id)
    ):
        raise records.NotPossible("not_a_record")


def _earliest(*moments: datetime | None) -> datetime | None:
    known = [moment for moment in moments if moment is not None]
    return min(known) if known else None


def kept_first(
    kept: Person, absorbed: Person, *, kept_account: bool, absorbed_account: bool
) -> bool:
    """Whether ``kept`` stays the record kept, rather than ``absorbed``: the owner's record is
    always the one kept (the household keeps its owner), then the record that has an account. The
    one rule of which record is kept (``tools.merge_tools`` reads it to describe the merge). A
    forgotten record is never kept over a living one: the living one carries the directive."""
    if kept.forgotten_at is not None and absorbed.forgotten_at is None:
        return False
    if absorbed.role == "owner":
        return False
    return kept.role == "owner" or kept_account or not absorbed_account


async def _end_for_the_owner(
    session: AsyncSession, kept: Person, absorbed: Person, now: datetime
) -> list[str]:
    """When the kept record is the owner's, the withdrawals and refusals in force for the other
    end now (the owner is never under one): their ids, for a split to put them back in force."""
    if kept.role != "owner":
        return []
    found = list(
        await session.scalars(
            select(OwnerDirective).where(
                OwnerDirective.person_id == absorbed.id,
                OwnerDirective.kind.in_(("withdraw", "refuse")),
                directives.in_force(now),
            )
        )
    )
    for directive in found:
        directive.lifted_at = now
    await session.flush()
    return [str(directive.id) for directive in found]


def _limit_facts(limit: SpendingLimit | None) -> dict[str, str] | None:
    if limit is None:
        return None
    return {"amount": str(limit.amount), "currency": limit.currency, "period": limit.period}


async def _owed_facts(
    session: AsyncSession, closed_account: bool, dropped: Sequence[Sequence[str]]
) -> dict[str, Any]:
    """The facts the owner is told of a merge (``people.acts.owed_rows``): whether an account was
    closed, and each spending limit dropped with the one that holds (amounts, currencies,
    periods)."""
    limits: list[dict[str, Any]] = []
    for ours, theirs in dropped:
        holds = await session.get(SpendingLimit, uuid.UUID(ours), populate_existing=True)
        gone = await session.get(SpendingLimit, uuid.UUID(theirs), populate_existing=True)
        limits.append({"holds": _limit_facts(holds), "dropped": _limit_facts(gone)})
    return {"closed_account": closed_account, "limits_dropped": limits}


async def merge(
    session: AsyncSession,
    *,
    kept: Person,
    absorbed: Person,
    by: str,
    reason: str | None,
    turn_of: uuid.UUID | None,
    now: datetime,
    owed_to_owner: bool = False,
) -> RecordMerge:
    """Make ``absorbed`` one with ``kept`` (swapped when only ``absorbed`` has an account), as the
    module says, and return the trace. ``records.NotPossible``: ``not_a_record``,
    ``same_record``. The absorbed record leaves its made role, carries ``merged_into_id`` and
    stands ``known`` (its state is in the trace: it gives nothing while it is merged), and is
    otherwise left as it was, so that it can be split again. ``owed_to_owner``: the owner must be
    told of it (the act says so). Flushed."""
    await _a_record(session, kept, now)
    await _a_record(session, absorbed, now)
    if kept.id == absorbed.id:
        raise records.NotPossible("same_record")
    if by == "domo" and not reason:
        raise ValueError("an act of the assistant has its reason")
    kept_account, absorbed_account = (
        await _has_account(session, kept),
        await _has_account(session, absorbed),
    )
    if not kept_first(kept, absorbed, kept_account=kept_account, absorbed_account=absorbed_account):
        kept, absorbed = absorbed, kept
    closes_an_account = kept_account and absorbed_account
    # The owner is never under a directive, a merge included: one on the record made one with his
    # ends now, and a split gives it back to that record.
    ended = await _end_for_the_owner(session, kept, absorbed, now)
    under = (
        await directives.under_directive(session, kept.id, now) is not None
        or await directives.under_directive(session, absorbed.id, now) is not None
    )
    before = _columns_of(kept)
    kept_directives = await _in_force_on(session, kept.id, now)
    absorbed_state = absorbed.state
    absorbed_role = None if absorbed.made_role_id is None else str(absorbed.made_role_id)
    closed_devices = 0
    if closes_an_account:
        # The account that is closed: it no longer signs in (its record is merged), and nothing
        # of it stays open.
        await revoke_sessions_of(session, absorbed.id)
        closed_devices = await forget_devices_of(session, absorbed.id, now)
    # A link made for the absorbed record no longer opens anything (Review Focus 5): the
    # assistant makes one for the kept record when it is still wanted.
    await session.execute(
        update(Invitation)
        .where(
            Invitation.person_id == absorbed.id,
            Invitation.accepted_at.is_(None),
            Invitation.cancelled_at.is_(None),
        )
        .values(cancelled_at=now)
        .execution_options(synchronize_session=False)
    )
    # The references of a channel that move with everything else (a channel is told of them).
    channel_refs = await _refs_of(session, absorbed.id)
    # Nothing of the other record that would bind the owner lands on him: its personal rights
    # and spending limits stay with it (as rows that stayed), and its child mark is not his.
    for_the_owner = kept.role == "owner"
    spared = await _rows_on(session, absorbed.id, (_RIGHTS, _LIMITS)) if for_the_owner else {}
    moved, stayed = await _move_everything(session, absorbed, kept, skip=frozenset(spared))
    settled = await _keep_the_stricter(session, moved, stayed, kept.id, now)
    for where, keys in spared.items():
        stayed.setdefault(where, []).extend(keys)
    await session.refresh(kept)
    await session.refresh(absorbed)
    acts = list(
        await session.scalars(
            update(PersonAct)
            .where(PersonAct.act == "promise_kept", PersonAct.person_id == absorbed.id)
            .values(person_id=kept.id)
            .returning(PersonAct.id)
            .execution_options(synchronize_session=False)
        )
    )
    kept.is_child = kept.is_child or (absorbed.is_child and not for_the_owner)
    # Someone who was ever given an access stays someone who was (a record that was is never
    # erased).
    kept.first_access_at = _earliest(kept.first_access_at, absorbed.first_access_at)
    if under:
        await directives.apply_in_force(session, kept, now)
    elif kept.state != "has_access" and absorbed.state == "has_access":
        # The access only the absorbed record had follows it: its role and its stay.
        kept.state = "has_access"
        kept.role = absorbed.role
        kept.made_role_id = absorbed.made_role_id
        kept.expires_at = absorbed.expires_at
        kept.disabled_at = None
    absorbed.merged_into_id = kept.id
    absorbed.state = "known"
    await session.flush()
    await records.leave_role(session, absorbed, now=now)
    trace = RecordMerge(
        kept_id=kept.id,
        absorbed_id=absorbed.id,
        moved={
            "state": absorbed_state,
            "made_role_id": absorbed_role,
            "rows": moved,
            "stayed": stayed,
            "acts": [str(act) for act in acts],
            "kept": {"before": before, "after": _columns_of(kept)},
            "closed_account": closes_an_account,
            "closed_devices": closed_devices,
            # The kept record's own directives, before the merge: they stay its own at a split.
            "kept_directives": kept_directives,
            "ended_directives": ended,
            # The references of a channel that moved, ``[channel, reference]``: the merge's
            # notice lists them (``people.hooks.merged``), a split tells those that went back.
            "channel_refs": channel_refs,
            # What the owner is told of the merge, kept as facts (numbers, never a text) for as
            # long as he was not told, after the trace is emptied too (``purge``).
            "owed": await _owed_facts(session, closes_an_account, settled["dropped_limits"]),
            **settled,
        },
        by=by,
        merged_at=now,
        expires_at=now + MERGE_KEPT,
    )
    session.add(trace)
    await session.flush()
    await note_act(
        session,
        act="merged",
        by=by,
        reason=reason,
        turn_of=turn_of,
        person_id=kept.id,
        other_id=absorbed.id,
        # A limit dropped is told to the owner, whoever asked: nobody decided it.
        owed_to_owner=owed_to_owner or bool(settled["dropped_limits"]),
        now=now,
    )
    return trace


async def _put_back(
    session: AsyncSession, rows: Mapping[str, Any], *, kept: uuid.UUID, absorbed: uuid.UUID
) -> dict[str, int]:
    """Point at ``absorbed`` again the rows a merge moved, those that still point at ``kept``.
    Returns how many could not go back, by ``table.column`` (a rule of their table, met since)."""
    left: dict[str, int] = {}
    for where, keys in rows.items():
        column = _column_named(where)
        if column is None or not keys:
            continue
        table = column.table
        key = tuple_(*table.primary_key.columns)
        wanted = [_read_key(table, one) for one in cast("list[list[str]]", keys)]
        _, stayed = await _repoint(
            session, column, to=absorbed, where=and_(column == kept, key.in_(wanted))
        )
        if stayed:
            left[where] = len(stayed)
    return left


async def _undo_the_stricter(
    session: AsyncSession, moved: Mapping[str, Any], left: dict[str, int]
) -> None:
    """Undo what ``_keep_the_stricter`` settled: the kept record's rights in force again, the
    limits and the places exchanged back."""
    for right_id in moved.get("revoked_rights", []):
        try:
            async with session.begin_nested():
                await session.execute(
                    update(PolicyRight)
                    .where(PolicyRight.id == uuid.UUID(right_id))
                    .values(revoked_at=None)
                )
        except IntegrityError:
            left[_RIGHTS] = left.get(_RIGHTS, 0) + 1
    for ours, theirs in moved.get("swapped_limits", []):
        await _swap_limit_values(session, uuid.UUID(ours), uuid.UUID(theirs))
    for ours, theirs in moved.get("swapped_places", []):
        await _swap_ranks(session, uuid.UUID(ours), uuid.UUID(theirs))


async def _in_force_on(session: AsyncSession, person_id: uuid.UUID, now: datetime) -> list[str]:
    """The ids of the withdrawals and refusals in force for a record."""
    found = await session.scalars(
        select(OwnerDirective.id).where(
            OwnerDirective.person_id == person_id,
            OwnerDirective.kind.in_(("withdraw", "refuse")),
            directives.in_force(now),
        )
    )
    return [str(one) for one in found]


async def _in_force_again(
    session: AsyncSession, ids: Sequence[str], absorbed: uuid.UUID, left: dict[str, int]
) -> None:
    """The directives a merge with the owner's record ended are in force again, on the absorbed
    record (where the moved rows put them back). One that cannot be (another of its kind is in
    force for it since) is counted in ``left``."""
    for directive_id in ids:
        try:
            async with session.begin_nested():
                await session.execute(
                    update(OwnerDirective)
                    .where(
                        OwnerDirective.id == uuid.UUID(directive_id),
                        OwnerDirective.person_id == absorbed,
                    )
                    .values(lifted_at=None)
                )
        except IntegrityError:
            left[_DIRECTIVES] = left.get(_DIRECTIVES, 0) + 1
    await session.flush()


async def _directives_reach_both(
    session: AsyncSession,
    kept: Person,
    absorbed: Person,
    *,
    before: Sequence[str],
    came_back: Sequence[str],
    now: datetime,
) -> None:
    """A withdrawal or a refusal set on the kept record since the merge (it was about the merged
    person) binds the absorbed one too once they are split, under a directive of its own, set by
    whoever set it (a directive in force binds, whoever is the owner now). One the kept record had
    before the merge (``before``) stays its own. One that came from the absorbed record
    (``came_back``) is the absorbed record's alone: when it could not go back (the absorbed record
    is under one of that kind already), it ends on the kept record."""
    for directive_id in came_back:
        left = await session.get(OwnerDirective, uuid.UUID(directive_id), populate_existing=True)
        if left is not None and left.person_id == kept.id and left.lifted_at is None:
            left.lifted_at = now
    await session.flush()
    has = {
        kind
        for kind in await session.scalars(
            select(OwnerDirective.kind).where(
                OwnerDirective.person_id == absorbed.id, directives.in_force(now)
            )
        )
    }
    found = await session.scalars(
        select(OwnerDirective).where(
            OwnerDirective.person_id == kept.id,
            OwnerDirective.kind.in_(("withdraw", "refuse")),
            directives.in_force(now),
        )
    )
    for directive in list(found):
        if directive.kind in has or str(directive.id) in (*before, *came_back):
            continue
        session.add(
            OwnerDirective(
                kind=directive.kind,
                person_id=absorbed.id,
                description=directive.description,
                except_words=directive.except_words,
                until=directive.until,
                set_by=directive.set_by,
                set_at=now,
            )
        )
    await session.flush()


async def _access_back(
    session: AsyncSession,
    absorbed: Person,
    role: MadeRole | None,
    by: str,
    reason: str | None,
    turn_of: uuid.UUID | None,
    now: datetime,
) -> str | None:
    """The access the absorbed record had when it was merged, given back through
    ``records.give_access`` with its role, its made role and its stay as they were: ``None`` when
    given, ``not_given`` when the door said no (a directive of the owner's, say): the record stays
    ``gone``. Flushed."""
    if absorbed.state != "gone":
        # A directive in force put it where it stands (``apply_in_force``).
        return "not_given"
    owner = (
        await session.get(Person, uuid.UUID(by.removeprefix("person:")))
        if by.startswith("person:")
        else None
    )
    try:
        await records.give_access(
            session,
            absorbed,
            role=cast(MemberRole, absorbed.role),
            made_role=role if role is not None and role.base == absorbed.role else None,
            standing=None,
            guest_days=None,
            by=by,
            by_owner=owner is not None and owner.role == "owner",
            reason=reason,
            turn_of=turn_of,
            now=now,
        )
    except (records.UnderDirective, records.NotPossible):
        return "not_given"
    return None


async def split(
    session: AsyncSession,
    merged: RecordMerge,
    *,
    by: str,
    reason: str | None,
    turn_of: uuid.UUID | None,
    now: datetime,
) -> Split:
    """Make the two records of ``merged`` two again, before ``expires_at``: what moved goes back,
    what was added to the kept record since stays on it; the absorbed record gets back its state
    and its made role. A withdrawal or a refusal in force then decides where it stands: its own
    (gone back to it), or one set on the kept record since the merge, which binds both; one the
    kept record had before the merge stays the kept record's alone. Its access is not
    given back when the made role it carried was deleted (its limits went with it). The kept record
    gets back what the merge changed of it, unless that changed since or a directive is in force
    for it. ``TraceGone`` past the time, once emptied, or split already;
    ``records.NotPossible("merged_since")`` while the kept record is itself merged into another
    (that later merge is split first). Flushed."""
    if merged.split_at is not None or not merged.moved or now >= merged.expires_at:
        raise TraceGone
    if by == "domo" and not reason:
        raise ValueError("an act of the assistant has its reason")
    kept = await session.get(Person, merged.kept_id)
    absorbed = await session.get(Person, merged.absorbed_id)
    if kept is None or absorbed is None:
        raise TraceGone
    if kept.merged_into_id is not None:
        raise records.NotPossible("merged_since")
    if absorbed.merged_into_id != kept.id:
        raise TraceGone
    moved = merged.moved
    left = await _put_back(session, moved.get("rows", {}), kept=kept.id, absorbed=absorbed.id)
    await _undo_the_stricter(session, moved, left)
    await _in_force_again(session, moved.get("ended_directives", []), absorbed.id, left)
    acts = [uuid.UUID(one) for one in moved.get("acts", [])]
    if acts:
        await session.execute(
            update(PersonAct)
            .where(PersonAct.id.in_(acts), PersonAct.person_id == kept.id)
            .values(person_id=absorbed.id)
            .execution_options(synchronize_session=False)
        )
    await session.refresh(kept)
    await session.refresh(absorbed)
    absorbed.merged_into_id = None
    absorbed.state = moved.get("state", "known")
    without_access = None
    # An access that ran when they were merged comes back only through the single door
    # (``records.give_access``: its act, the owner's directives, his rule on being told); until
    # then, and when the door says no, the record stands ``gone``.
    wants_access = absorbed.state == "has_access" and is_active(absorbed, now)
    if wants_access:
        absorbed.state = "gone"
        absorbed.disabled_at = absorbed.disabled_at or now
    role = None
    role_id = moved.get("made_role_id")
    if role_id is not None:
        role = await made_roles.by_id(session, uuid.UUID(role_id))
        if role is not None and role.deleted_at is None:
            absorbed.made_role_id = role.id
        else:
            role = None
            if absorbed.state == "has_access" or wants_access:
                # Back with access but without the limits of its role: back without access.
                absorbed.state = "gone"
                absorbed.disabled_at = absorbed.disabled_at or now
                without_access = "made_role_gone"
                wants_access = False
    await session.flush()
    await _directives_reach_both(
        session,
        kept,
        absorbed,
        before=moved.get("kept_directives", []),
        came_back=[key[0] for key in moved.get("rows", {}).get(_DIRECTIVES, [])],
        now=now,
    )
    await directives.apply_in_force(session, absorbed, now)
    if wants_access:
        without_access = await _access_back(session, absorbed, role, by, reason, turn_of, now)
    sides = moved.get("kept", {})
    if (
        sides
        and _columns_of(kept) == sides.get("after")
        and await directives.under_directive(session, kept.id, now) is None
    ):
        for name, value in cast("dict[str, str | None]", sides.get("before", {})).items():
            setattr(kept, name, _read_column(name, value))
    merged.split_at = now
    await session.flush()
    went = {(str(c), str(r)) for c, r in moved.get("channel_refs", [])}
    refs_back = tuple(
        (channel, reference)
        for channel, reference in await _refs_of(session, absorbed.id)
        if (channel, reference) in went
    )
    await note_act(
        session,
        act="split",
        by=by,
        reason=reason,
        turn_of=turn_of,
        person_id=kept.id,
        other_id=absorbed.id,
        now=now,
    )
    await session.refresh(absorbed)
    return Split(
        person=absorbed,
        stayed=left,
        without_access=without_access,
        account_back=bool(moved.get("closed_account")) and absorbed.state == "has_access",
        devices_stay_removed=int(moved.get("closed_devices", 0)),
        refs_back=refs_back,
    )


async def _drop_what_stayed(
    session: AsyncSession, stayed: Mapping[str, Any], absorbed: uuid.UUID, now: datetime
) -> None:
    """The rows that stayed on an absorbed record go with it: a right is revoked and a directive
    ended (neither is ever deleted), anything else is deleted, each in a savepoint (a row something
    else still needs stays)."""
    for where, keys in stayed.items():
        column = _column_named(where)
        if column is None:
            continue
        if where == _PLACES:
            # The place goes, and the places after it move up: the ranks keep no gap.
            await off_the_successors(session, absorbed)
            continue
        table = column.table
        for key in cast("list[list[str]]", keys):
            one = tuple_(*table.primary_key.columns) == _read_key(table, key)
            if table.name == "policy_rights":
                statement: Any = update(table).where(one, table.c.revoked_at.is_(None))
                statement = statement.values(revoked_at=now)
            elif table.name == "owner_directives":
                statement = update(table).where(one, table.c.lifted_at.is_(None))
                statement = statement.values(lifted_at=now)
            else:
                statement = delete(table).where(one)
            try:
                async with session.begin_nested():
                    await session.execute(statement)
            except IntegrityError:
                continue


async def purge(session: AsyncSession, now: datetime) -> int:
    """Empty the traces whose time is past at ``now``: for a merge that was not split, what stayed
    on the absorbed record goes (``_drop_what_stayed``) and the record is emptied as a forgotten one
    (``forget.empty_row``); the trace keeps no ids any more. Returns how many merges were not
    split and are now for good. Flushed."""
    due = list(
        await session.scalars(
            select(RecordMerge).where(
                RecordMerge.expires_at <= now, RecordMerge.moved.has_key("rows")
            )
        )
    )
    emptied = 0
    for trace in due:
        if trace.split_at is None:
            await _drop_what_stayed(session, trace.moved.get("stayed", {}), trace.absorbed_id, now)
            absorbed = await session.get(Person, trace.absorbed_id)
            if absorbed is not None and absorbed.forgotten_at is None:
                await forget.empty_row(session, absorbed, now)
                emptied += 1
        # What the owner is still owed of the merge stays (facts, numbers only): the record
        # emptied, his telling must not lose it.
        trace.moved = (
            {} if await told_to_owner(session, trace) else {"owed": trace.moved.get("owed", {})}
        )
    await session.flush()
    return emptied


async def told_to_owner(session: AsyncSession, trace: RecordMerge) -> bool:
    """Whether nothing of the merge is owed to the owner any more: its act was not owed, or was
    told."""
    waiting = await session.scalar(
        select(PersonAct.id)
        .where(
            PersonAct.act == "merged",
            PersonAct.person_id == trace.kept_id,
            PersonAct.other_id == trace.absorbed_id,
            PersonAct.owed_to_owner.is_(True),
            PersonAct.told_owner_at.is_(None),
        )
        .limit(1)
    )
    return waiting is None
