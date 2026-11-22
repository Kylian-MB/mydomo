"""What changed for a person, by someone else, since Domo last spoke with them (2026-11-16, the
owner's trial of 0.9.0).

Alex had given the owner's role to Sam; Sam gave it back, in her own conversation. When Alex then
asked Domo to give it to Sam again, Domo went by his own earlier reply ("Sam is now the owner,
you are an adult") over the context of the turn, which said ``Role: owner``, and refused twice. The
turn is now told, in one line of its context, what changed for the person by someone else since
their last complete reply: their role, the owner's role given to them or taken from them, a right
of their own given, changed, taken away or paused, being removed or brought back, a spending limit
set for them or taken away.

It is read from the audit log, and from nothing else: every one of those changes writes its entry
in the transaction of the change, last, just before the commit (``audit.log.append_audit``), so an
entry is there if and only if the change was made, with who made it (``actor``) and when (``ts``);
the log is append-only and chained, never edited. The tables themselves keep only the state of
now, not who changed it nor when. A change the person made themselves (the actor is them) is not
told: they know it. One no person made (the upkeep pausing the rights of someone who left) is told
as made "automatically". The entries about the person are picked in the query, then the latest
``_READ_AT_MOST`` read.

"Since their last complete reply" is the moment Domo's last complete reply of their conversation
began (``conversation.prepare``): the line is shown until a turn of theirs answered with it, and a
turn that failed or said nothing leaves it for the next. Nothing is told when Domo never answered
them yet. Domo is not made to speak unasked for it: the line waits for their next turn, an
arrival included (opening their page), where he may speak first if he judges it worth it. The
words of a right (its summary) are data, as the whole line is.
"""

import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Final, cast

from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.audit.log import readable_ts
from domo_core.audit.models import AuditEntry
from domo_core.prompts import ChangeLine

# How many changes the line tells at most: the latest.
MAX_CHANGES_TOLD: Final = 10
# The entries that may tell a change for the person.
_TOLD: Final = (
    "household.role_changed",
    "household.stay_changed",
    "household.ownership_transferred",
    "household.removed",
    "household.bring_back_offered",
    "rights.changed",
    "rights.paused",
    "spending.limit_set",
    "spending.limit_removed",
)
_SOMEONE_WHO_LEFT: Final = "someone who left"
# Who made a change no person made (the upkeep pausing the rights of someone who left).
_AUTOMATICALLY: Final = "automatically"
# How many entries are read at most, once those about the person are picked.
_READ_AT_MOST: Final = 200


def _about(person_id: uuid.UUID) -> ColumnElement[bool]:
    """The entries of ``_TOLD`` that may be about ``person_id``, picked in the query (``_what``
    reads them again)."""
    person = f"person:{person_id}"
    details = AuditEntry.details
    return or_(
        details.contains({"person_id": str(person_id)}),
        details.contains({"to": person}),
        details.contains({"from": person}),
        details.contains({"person": str(person_id)}),
        details.contains({"changes": [{"subject": person}]}),
    )


def _what(action: str, details: Mapping[str, object], person: str) -> list[str]:
    """What an entry tells of a change for ``person`` (``person:<id>``), in a few words; nothing
    when it is not about them."""
    person_id = person.removeprefix("person:")
    match action:
        case "household.role_changed" if details.get("person_id") == person_id:
            return [f"role changed from {details.get('old_role')} to {details.get('new_role')}"]
        case "household.stay_changed" if details.get("person_id") == person_id:
            return [f"stay as a guest set again: {details.get('guest_days')} days from then"]
        case "household.ownership_transferred" if details.get("to") == person:
            return ["the owner's role was handed to them: they are the owner"]
        case "household.ownership_transferred" if details.get("from") == person:
            return ["the owner's role was taken from them: they are an adult"]
        case "household.removed" if details.get("person_id") == person_id:
            return ["removed from the household"]
        case "household.bring_back_offered" if details.get("person_id") == person_id:
            return [f"brought back to the household, as {details.get('role')}"]
        case "rights.paused" if details.get("person_id") == person_id:
            return [f"{details.get('count')} rights of their own paused"]
        case "spending.limit_set" if details.get("person") == person_id:
            amount = f"{details.get('amount')} {details.get('currency')}"
            return [f"a spending limit set for them: {amount} per {details.get('period')}"]
        case "spending.limit_removed" if details.get("person") == person_id:
            return ["their spending limit taken away"]
        case "rights.changed":
            return _rights_changed(details.get("changes"), person)
        case _:
            return []


def _rights_changed(changes: object, person: str) -> list[str]:
    """The rights of their own a change of the rights gave, changed or took away."""
    if not isinstance(changes, list):
        return []
    told: list[str] = []
    for change in cast(list[object], changes):
        if not isinstance(change, dict):
            continue
        entry = cast(dict[str, object], change)
        if entry.get("subject") != person:
            continue
        before, after = entry.get("before"), entry.get("after")
        if after is None:
            told.append(f"a right of theirs taken away: {before}")
        elif before is None:
            told.append(f"a right given to them: {after}")
        else:
            told.append(f"a right of theirs changed: {after}")
    return told


async def changes_for(
    session: AsyncSession, person_id: uuid.UUID, since: datetime | None
) -> tuple[ChangeLine, ...]:
    """What changed for ``person_id``, by someone else, after ``since`` (see the module
    docstring): ``MAX_CHANGES_TOLD`` at most, the latest, oldest first. Nothing without
    ``since``. Reads only."""
    if since is None:
        return ()
    person = f"person:{person_id}"
    found = await session.execute(
        select(AuditEntry.actor, AuditEntry.action, AuditEntry.details, readable_ts())
        .where(
            AuditEntry.action.in_(_TOLD),
            AuditEntry.ts > since,
            AuditEntry.actor != person,
            _about(person_id),
        )
        .order_by(AuditEntry.id.desc())
        .limit(_READ_AT_MOST)
    )
    told: list[tuple[str, str, datetime | None]] = []
    for actor, action, details, ts in found.all():
        for what in _what(action, cast(Mapping[str, object], details), person):
            told.append((what, actor, cast(datetime | None, ts)))
    told = told[:MAX_CHANGES_TOLD]
    names = await _names(session, [actor for _, actor, _ in told])
    return tuple(
        ChangeLine(
            what=what,
            by=names.get(
                actor, _SOMEONE_WHO_LEFT if actor.startswith("person:") else _AUTOMATICALLY
            ),
            when="" if ts is None else ts.astimezone(UTC).isoformat(),
        )
        for what, actor, ts in reversed(told)
    )


async def _names(session: AsyncSession, actors: Sequence[str]) -> dict[str, str]:
    """The display name now of each actor (``person:<id>``) that is a person."""
    ids: set[uuid.UUID] = set()
    for actor in actors:
        try:
            ids.add(uuid.UUID(actor.removeprefix("person:")))
        except ValueError:
            continue
    if not ids:
        return {}
    found = await session.execute(select(Person.id, Person.display_name).where(Person.id.in_(ids)))
    return {f"person:{person_id}": name for person_id, name in found.all()}
