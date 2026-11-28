"""Voice prints follow the record of the person they are of (spec 6.5, 6.10; Task 27).

The voice keeps its own store: ``voice_refs.person_id`` is the link to the person (``ON DELETE
CASCADE`` erases a voice with its person), and the voice claims nothing in
``person_channel_refs`` (``prints`` makes its ``VoiceRef`` rows itself). The records' own paths
handle the voice's tables directly: a merge moves ``voice_refs`` and ``voice_moments`` with every
other row that names a record, except onto the owner's record (``people.merge``), a split puts
back what moved, and forgetting deletes them (``people.forget``); a record that may be erased
never had an access, so never a voice learnt (prints are learnt for a signed-in person only).
Nothing here is subscribed to the records' notices (``people.hooks``).

The names below (``claim_reference``, ``refs_of``, ``person_of_ref``, ``on_record``,
``RecordEvent``, ``NoSuchRecord``, ``ReferenceTaken``) are a stand-in written before the records
existed, over ``voice_refs`` itself; only tests call them. They must NOT be wired to
``people.hooks`` as they are: that module reads ``person_channel_refs``, where the voice holds
nothing, so ``reconcile`` fed with its ``person_of_ref`` would read every voice as nobody's and
delete it with its prints at the next round. As it is, ``reconcile`` reads ``voice_refs`` against
itself and changes nothing.

What a notice would do to the prints (``on_record``):

=============  =================================================================================
``merged``     the references in ``refs`` now belong to ``into_id`` (the record kept)
``split``      the references in ``refs`` go back to ``person_id``
``erased``     the references, and every reference of ``person_id``, are deleted with their prints
``forgotten``  the same, ALWAYS, ``keep_minimum`` or not (the owner's decision 12: "description
               only"); a reference nobody claimed (an unknown voice) named there goes too
=============  =================================================================================

``reconcile`` would mend a notice that was missed (given a ``person_of`` that reads the voice's
own links truly; see above): each reference of a person that was here when the pass began is
weighed on its own (``person_of_ref``,
read for that row): one whose owner is nobody now is erased, one that is someone else's now
follows them; a reference made while the pass runs is never touched. A voice nobody knows is not
the record's: it is left to its own end (``prints.sweep``). The upkeep pass (``voice_upkeep_pass``)
reconciles when the core starts and every hour, and sweeps the passing prints, the unknown voices
and the moments past their end at each round, each in a transaction of its own.

Nothing here logs a reference, a person or a print.
"""

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.channels.voice.abilities import VOICE_CHANNEL
from domo_core.channels.voice.models import VoicePrint, VoiceRef
from domo_core.channels.voice.prints import sweep
from domo_core.db import SessionFactory

logger = logging.getLogger(__name__)

Notice = Literal["merged", "split", "erased", "forgotten"]
# How often a missed notice is looked for, after the first time (when the core starts).
RECONCILE_EVERY: Final = timedelta(hours=1)


class NoSuchRecord(Exception):
    """The person a reference is claimed for does not exist."""


class ReferenceTaken(Exception):
    """Another person holds the reference on that channel."""

    def __init__(self, person_id: uuid.UUID) -> None:
        super().__init__("the reference belongs to another person")
        self.person_id = person_id


@dataclass(frozen=True)
class RecordEvent:
    """A notice of the record (that branch's contract): ``person_id`` is the record absorbed
    (``merged``), back (``split``), erased or forgotten; ``into_id`` the record kept by a merge or
    left by a split (``None`` otherwise); ``refs`` the ``(channel, reference)`` concerned."""

    kind: Notice
    person_id: uuid.UUID
    into_id: uuid.UUID | None
    refs: tuple[tuple[str, str], ...]
    keep_minimum: bool
    at: datetime


PersonOfRef = Callable[..., Awaitable[uuid.UUID | None]]


async def claim_reference(
    session: AsyncSession, *, channel: str, reference: str, person_id: uuid.UUID, now: datetime
) -> None:
    """``reference`` (of ``channel``) is ``person_id``'s from now on: an unknown voice's keeps its
    prints, which no longer die as an unknown voice's. The same claim twice changes nothing;
    ``ReferenceTaken`` when another person holds it, ``NoSuchRecord`` for no such person, and
    ``ValueError`` for a channel that is not the voice (this stand-in holds no other). Two claims
    of one new reference at once: the second reads the first's (``ReferenceTaken`` when it is
    another's). Flushed, not committed."""
    if channel != VOICE_CHANNEL:
        raise ValueError("this stand-in holds the voice's references only")
    if await session.get(Person, person_id) is None:
        raise NoSuchRecord()
    ref = await session.scalar(
        select(VoiceRef).where(VoiceRef.reference == reference).with_for_update()
    )
    if ref is None:
        try:
            async with session.begin_nested():
                session.add(VoiceRef(reference=reference, person_id=person_id))
                await session.flush()
            return
        except IntegrityError:
            # Claimed by another transaction meanwhile: read what it made.
            ref = await session.scalar(
                select(VoiceRef)
                .where(VoiceRef.reference == reference)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if ref is None:
                raise
    if ref.person_id is None:
        ref.person_id = person_id
        await session.execute(
            update(VoicePrint).where(VoicePrint.ref_id == ref.id).values(expires_at=None)
        )
    elif ref.person_id != person_id:
        raise ReferenceTaken(ref.person_id)
    await session.flush()


async def refs_of(session: AsyncSession, channel: str) -> set[str]:
    """Every reference of ``channel`` that belongs to a person now."""
    if channel != VOICE_CHANNEL:
        return set()
    found = await session.scalars(select(VoiceRef.reference).where(VoiceRef.person_id.is_not(None)))
    return set(found)


async def person_of_ref(session: AsyncSession, *, channel: str, reference: str) -> uuid.UUID | None:
    """Whom ``reference`` of ``channel`` belongs to now; ``None`` when nobody, or never claimed."""
    if channel != VOICE_CHANNEL:
        return None
    return await session.scalar(select(VoiceRef.person_id).where(VoiceRef.reference == reference))


def _voice_refs(event: RecordEvent) -> list[str]:
    return [reference for channel, reference in event.refs if channel == VOICE_CHANNEL]


async def on_record(session: AsyncSession, event: RecordEvent) -> int:
    """Apply a notice of the record to the voice's references and prints (the module's table);
    how many references moved or went. The caller commits."""
    named = _voice_refs(event)
    match event.kind:
        case "merged" | "split":
            owner = event.into_id if event.kind == "merged" else event.person_id
            if owner is None or not named:
                return 0
            moved = await session.execute(
                update(VoiceRef)
                .where(VoiceRef.reference.in_(named))
                .values(person_id=owner)
                .returning(VoiceRef.id)
            )
            return len(moved.all())
        case "erased" | "forgotten":
            # ``keep_minimum`` is never read: a forgotten voice keeps nothing (decision 12).
            gone = await session.execute(
                delete(VoiceRef)
                .where(VoiceRef.reference.in_(named) | (VoiceRef.person_id == event.person_id))
                .returning(VoiceRef.id)
            )
            return len(gone.all())


async def reconcile(
    session: AsyncSession, now: datetime, *, person_of: PersonOfRef | None = None
) -> int:
    """Mend what a missed notice left (the module's docstring), as the record sees each
    reference now (``person_of``: ``person_of_ref`` unless given); how many references were
    moved or erased. Only the rows read when the pass began are weighed, each as it still is (a
    row someone changed meanwhile is left). The caller commits."""
    whose = person_of or person_of_ref
    rows = (
        await session.execute(
            select(VoiceRef.id, VoiceRef.reference, VoiceRef.person_id).where(
                VoiceRef.person_id.is_not(None)
            )
        )
    ).all()
    mended = 0
    for ref_id, reference, person_id in rows:
        owner = await whose(session, channel=VOICE_CHANNEL, reference=reference)
        if owner == person_id:
            continue
        same = (VoiceRef.id == ref_id, VoiceRef.person_id == person_id)
        changed = await session.execute(
            delete(VoiceRef).where(*same).returning(VoiceRef.id)
            if owner is None
            else update(VoiceRef).where(*same).values(person_id=owner).returning(VoiceRef.id)
        )
        mended += len(changed.all())
    return mended


def voice_upkeep_pass(session_factory: SessionFactory) -> Callable[[datetime], Awaitable[None]]:
    """The upkeep's pass of the voice (``upkeep.UpkeepPass``): the passing prints, the unknown
    voices and the moments past their end swept at each round; a missed notice mended at the
    first round and then every ``RECONCILE_EVERY``."""
    last: list[datetime] = []

    async def keep_the_voices(now: datetime) -> None:
        # Each in a transaction of its own: one that fails never holds the other back.
        if not last or now - last[0] >= RECONCILE_EVERY:
            try:
                async with session_factory() as session:
                    mended = await reconcile(session, now)
                    await session.commit()
                last[:] = [now]
                if mended:
                    logger.info("%d voice reference(s) mended after a missed notice", mended)
            except Exception as error:
                # Tried again at the next round.
                logger.error("The voice references could not be mended (%s)", type(error).__name__)
        async with session_factory() as session:
            swept = await sweep(session, now)
            await session.commit()
        if swept:
            logger.debug("%d voice row(s) past their end swept", swept)

    return keep_the_voices
