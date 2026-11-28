"""The trace of an act on a person: one row of ``person_acts`` with its reason, and its audit line.

Every act the assistant (or someone) does on a record, a role or a promise leaves a row: what, to
whom, why and who did it. The audit log holds identifiers only, never free text: its line
``people.<act>`` carries the id of the row (``act_id``) and the ids the act is about, and the reason
stays in the row, which the owner may read (``why``).
"""

from collections.abc import Callable, Collection, Mapping
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.audit.log import append_audit
from domo_core.household.models import Invitation
from domo_core.llm.types import JsonObject
from domo_core.people.models import Act, MadeRole, PersonAct, RecordMerge
from domo_core.people.news import news_for

__all__ = [
    "AUDIT_ACTION",
    "note_act",
    "owed",
    "owed_count",
    "owed_rows",
    "tell_owner",
    "tell_owner_of",
    "told",
    "why",
]

# The audit action of each act: ``people.<act>``, written out so that every action the code audits
# can be read where it is written.
AUDIT_ACTION: Final[Mapping[Act, str]] = {
    "opened": "people.opened",
    "completed": "people.completed",
    "erased": "people.erased",
    "forgotten": "people.forgotten",
    "linked": "people.linked",
    "unlinked": "people.unlinked",
    "merged": "people.merged",
    "split": "people.split",
    "role_created": "people.role_created",
    "role_changed": "people.role_changed",
    "role_deleted": "people.role_deleted",
    "access_given": "people.access_given",
    "access_removed": "people.access_removed",
    "access_mistake": "people.access_mistake",
    "promised": "people.promised",
    "promise_kept": "people.promise_kept",
    "promise_dropped": "people.promise_dropped",
    "directive_set": "people.directive_set",
    "directive_lifted": "people.directive_lifted",
    "exception_used": "people.exception_used",
    "invited": "people.invited",
    "link_sent": "people.link_sent",
    "joining_stopped": "people.joining_stopped",
    "invitation_cancelled": "people.invitation_cancelled",
}


async def note_act(
    session: AsyncSession,
    *,
    act: Act,
    by: str,
    reason: str | None,
    turn_of: UUID | None,
    person_id: UUID | None = None,
    other_id: UUID | None = None,
    role_id: UUID | None = None,
    promise_id: UUID | None = None,
    directive_id: UUID | None = None,
    invitation_id: UUID | None = None,
    owed_to_owner: bool = False,
    returns: bool = False,
    now: datetime,
) -> PersonAct:
    """Write the act (``by`` is ``domo`` or ``person:<id>``, ``turn_of`` the person whose turn it
    was, ``None`` in a turn he acts in on his own), then its audit line ``people.<act>`` whose
    details are ids only, plus ``act_id``: never the reason. ``owed_to_owner``: the owner must be
    told of it. ``returns``: an access given that wakes one that slept (``records.returns_now``),
    which its audit line says. Flushed, not committed."""
    row = PersonAct(
        act=act,
        person_id=person_id,
        other_id=other_id,
        role_id=role_id,
        promise_id=promise_id,
        directive_id=directive_id,
        invitation_id=invitation_id,
        reason=reason,
        by=by,
        turn_of=turn_of,
        at=now,
        owed_to_owner=owed_to_owner,
    )
    session.add(row)
    await session.flush()
    named = {
        "person_id": person_id,
        "other_id": other_id,
        "role_id": role_id,
        "promise_id": promise_id,
        "directive_id": directive_id,
        "invitation_id": invitation_id,
    }
    details = {"act_id": str(row.id)}
    details.update({key: str(value) for key, value in named.items() if value is not None})
    if returns:
        details["returns"] = "true"
    await append_audit(session, actor=by, action=AUDIT_ACTION[act], details=details)
    return row


async def tell_owner(
    session: AsyncSession, act: PersonAct, *, knows: Callable[[UUID], bool], now: datetime
) -> UUID | None:
    """An act owed to the owner (``owed_to_owner``) is told to him once: marked told at ``now``
    when he knows of it already (``knows``: he saw it in his own turn, or agreed to it himself),
    else his news is kept and his id returned, for the caller to tell the core once it committed.
    ``None`` when there is no owner now. Flushed."""
    # Not at the top: the policy reads the household, which reads the acts.
    from domo_core.policy.subjects import owner_of_now

    try:
        owner = await owner_of_now(session, now)
    except LookupError:
        return None
    if knows(owner):
        act.told_owner_at = now
        await session.flush()
        return None
    await news_for(session, owner, now=now)
    return owner


async def tell_owner_of(
    session: AsyncSession,
    act: Act,
    *,
    knows: Callable[[UUID], bool],
    now: datetime,
    person_id: UUID | None = None,
    role_id: UUID | None = None,
    invitation_id: UUID | None = None,
) -> UUID | None:
    """The act ``act`` written at ``now`` on what the ids name (the latest one) becomes owed to the
    owner and is told to him (``tell_owner``): for a gesture whose telling is the assistant's
    judgement (``whose.tells_the_owner``). ``None`` when there is no such act, or nothing to tell
    the core. Flushed."""
    query = select(PersonAct).where(PersonAct.act == act, PersonAct.at == now)
    if person_id is not None:
        query = query.where(PersonAct.person_id == person_id)
    if role_id is not None:
        query = query.where(PersonAct.role_id == role_id)
    if invitation_id is not None:
        query = query.where(PersonAct.invitation_id == invitation_id)
    row = await session.scalar(query.order_by(PersonAct.id.desc()).limit(1))
    if row is None:
        return None
    row.owed_to_owner = True
    await session.flush()
    return await tell_owner(session, row, knows=knows, now=now)


async def why(session: AsyncSession, person_id: UUID) -> list[PersonAct]:
    """The acts done on a record, oldest first, with their reasons."""
    found = await session.scalars(
        select(PersonAct)
        .where(PersonAct.person_id == person_id)
        .order_by(PersonAct.at, PersonAct.id)
    )
    return list(found)


async def owed(session: AsyncSession) -> list[PersonAct]:
    """The acts the owner is owed the telling of and was not told yet, oldest first."""
    found = await session.scalars(
        select(PersonAct)
        .where(PersonAct.owed_to_owner.is_(True), PersonAct.told_owner_at.is_(None))
        .order_by(PersonAct.at, PersonAct.id)
    )
    return list(found)


async def owed_count(session: AsyncSession) -> int:
    """How many acts the owner is owed the telling of and was not told yet."""
    return (
        await session.scalar(
            select(func.count())
            .select_from(PersonAct)
            .where(PersonAct.owed_to_owner.is_(True), PersonAct.told_owner_at.is_(None))
        )
        or 0
    )


async def told(session: AsyncSession, ids: Collection[UUID], now: datetime) -> None:
    """The acts ``ids`` were told to the owner, at ``now`` (those told already keep their time).
    Flushed."""
    if not ids:
        return
    await session.execute(
        update(PersonAct)
        .where(PersonAct.id.in_(list(ids)), PersonAct.told_owner_at.is_(None))
        .values(told_owner_at=now)
    )
    await session.flush()


async def _merged_row(session: AsyncSession, act: PersonAct, kept: object) -> JsonObject:
    """What a merge owed to the owner says, from the facts its trace keeps for him
    (``RecordMerge.moved["owed"]``, kept after the trace is emptied while he was not told): the
    account kept and the one closed, when it closed one; and each spending limit of the other
    record that was dropped (another currency: the kept record's holds), with the one that
    holds."""
    trace = await session.scalar(
        select(RecordMerge)
        .where(RecordMerge.kept_id == act.person_id, RecordMerge.absorbed_id == act.other_id)
        .order_by(RecordMerge.merged_at.desc())
        .limit(1)
    )
    owed: dict[str, Any] = {} if trace is None else trace.moved.get("owed", {})
    found: JsonObject = {}
    if owed.get("closed_account", trace is None):
        other = None if act.other_id is None else await session.get(Person, act.other_id)
        found["account_kept"] = kept
        found["account_closed"] = None if other is None else other.display_name
    if owed.get("limits_dropped"):
        found["limits_dropped"] = owed["limits_dropped"]
    return found


async def owed_rows(session: AsyncSession, limit: int | None = None) -> list[JsonObject]:
    """What the assistant needs to tell the owner of each act he is owed (``owed``), as data:
    the act, when, by whom, and the names, the role and the account concerned, never anything
    more. A merge of two accounts says which account was kept and which was closed (the record
    kept carries the act, the other one is its ``other_id``), a merge that dropped a spending
    limit says both limits (``_merged_row``), someone new invited is told by the name and role
    of the invitation, and someone forgotten is told by the role they had
    alone, their name gone with them. ``limit``: the oldest ones only, at most that many
    (the ones that wait longest first)."""
    rows: list[JsonObject] = []
    for act in (await owed(session))[:limit]:
        person = None if act.person_id is None else await session.get(Person, act.person_id)
        row: JsonObject = {
            "act_id": str(act.id),
            "act": act.act,
            "when": act.at.isoformat(),
            "by": "the assistant" if act.by == "domo" else "a person of the house",
            "person": None if person is None else person.display_name,
        }
        if act.act == "access_given" and person is not None:
            made = (
                None
                if person.made_role_id is None
                else await session.get(MadeRole, person.made_role_id)
            )
            row["role"] = person.role if made is None else made.name
        if act.act == "merged":
            row.update(await _merged_row(session, act, row["person"]))
        if (
            act.act in ("invited", "joining_stopped", "invitation_cancelled")
            and person is None
            and act.invitation_id is not None
        ):
            # Someone new: no record before they join, the invitation's name and role say who.
            invitation = await session.get(Invitation, act.invitation_id)
            if invitation is not None:
                row["person"] = invitation.display_name
                row["role"] = invitation.role
        if act.act == "forgotten" and person is not None:
            # Their name went with them: the role they had says who it was, nothing more.
            row["person"] = None
            row["role"] = person.role
        rows.append(row)
    return rows
