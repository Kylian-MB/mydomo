"""Erasing the notes of the guests who left: a pass of the core's periodic upkeep.

A guest may receive notes while they are there (``tools.notes``). A guest's stay is short, and what
people wrote to them is not the household's to keep: once the guest has left, their stay ended
(``expires_at``) or they were removed (``disabled_at``), the notes they *received* are erased for
good, read or not. If they come back, they come back without them. The notes the guest *left* for
others stay, and so do the notes of an adult or a child who was removed: those people have them
back if they come back. "Left" is what ``accounts.sessions.active_at`` says, the same test as for
a login, so that a note never outlives the guest's right to be in the household.

There is one exception, the author's (spec 5.10): the author of a note left for a guest may keep it
until a day (``Note.keep_until``, the end of that day in UTC, a year ahead at most:
``tools.notes.NoteKeep``) because the guest is coming back. Such a note stays after the guest has
left, and is erased once that day is over if the guest is still away; a guest who comes back
before finds it. A guest who is there loses nothing, kept or not.

``erase_notes_of_departed_guests`` does it: it deletes those notes, writes one audit entry
``notes.erased`` per guest who lost some (by the system: the guest's id and how many notes, never
a word of a note, spec 6), and commits nothing, so that the entries and the erasure end together.
``notes_pass`` is the pass of the core's upkeep (``upkeep.run_upkeep``) that does it in a session
of its own and commits it, when the core starts and every minute after (``web.app``): a guest who
leaves between two passes keeps their notes for up to a minute; a note left for a guest at the
moment they leave is erased by the next pass.
"""

from collections import Counter
from datetime import datetime

from sqlalchemy import delete, or_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domo_core.accounts.actors import VISITING
from domo_core.accounts.models import Person
from domo_core.accounts.sessions import active_at
from domo_core.audit.log import append_audit
from domo_core.notes.models import Note
from domo_core.upkeep import UpkeepPass


async def erase_notes_of_departed_guests(session: AsyncSession, now: datetime) -> int:
    """Delete every note whose recipient is a guest who is no longer in the household at ``now``
    (``~active_at(now)``), except the notes their author keeps until a day that is not over yet
    (``Note.keep_until`` after ``now``), and return how many were deleted.

    One audit entry ``notes.erased`` is added for each guest who lost notes: acting as the system,
    with the details ``{"recipient": "person:<id>", "count": <n>}``. Nothing is committed: the
    caller owns the transaction (and it must be a read committed one, as for any audit entry)."""
    # The session's own notes are not reconciled (``synchronize_session``): the upkeep opens a
    # session for this pass alone, which holds none.
    erased = await session.execute(
        delete(Note)
        .where(
            Note.recipient_id == Person.id,
            VISITING,
            ~active_at(now),
            or_(Note.keep_until.is_(None), Note.keep_until <= now),
        )
        .returning(Note.recipient_id)
        .execution_options(synchronize_session=False)
    )
    per_guest = Counter(erased.scalars())
    for recipient_id, count in sorted(per_guest.items()):
        await append_audit(
            session,
            actor="system",
            action="notes.erased",
            details={"recipient": f"person:{recipient_id}", "count": count},
        )
    return sum(per_guest.values())


def notes_pass(session_factory: async_sessionmaker[AsyncSession]) -> UpkeepPass:
    """The pass of the upkeep that erases the notes of the guests who left
    (``erase_notes_of_departed_guests``, at the time of the pass), in a session of its own,
    committed."""

    async def erase(now: datetime) -> None:
        async with session_factory() as session:
            await erase_notes_of_departed_guests(session, now)
            await session.commit()

    return erase
