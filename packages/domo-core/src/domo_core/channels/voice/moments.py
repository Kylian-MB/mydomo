"""The reserved moments of a voice (spec 6.9): a short time in which a person's next words
mean something particular, kept as a row (``voice_moments``).

The tools open a moment (``tools.voice_tools``): the two sayings of a phrase being agreed or
changed. The voice channel reads it for each sentence it hears (``moment_of``): while one lives
for the person, what they say goes to the vault, and never to a model, the discussion, the journal
or a log. A stop of the listening keeps no moment any more: it acts at once
(``channels.voice.live.LiveVoices.stop_listening``); the ``stop_listening`` purpose is left in the
schema, unused. A proof awaited by the phrase is no moment either: it is weighed inside a proof
window of a microphone (``channels.voice.session``). A moment never holds a word that was said: it
holds the purpose, the microphone it was opened on (when known) and how many sayings it has taken
(``step``).

A person has one moment (a unique index; a new one replaces it by upsert). A moment is over at
the latest ``MOMENT_LIVES`` after it was opened (the upkeep sweeps those past: ``prints.sweep``),
whatever was said or not.
"""

import uuid
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.channels.voice.models import MomentPurpose, VoiceMoment

Purpose = MomentPurpose

# How long a moment lives, whatever happens in it.
MOMENT_LIVES: Final = timedelta(minutes=3)


async def open_moment(
    session: AsyncSession,
    *,
    person_id: uuid.UUID,
    microphone: str | None,
    purpose: Purpose,
    now: datetime,
) -> None:
    """Reserve a moment for ``person_id`` (on ``microphone`` when known), replacing the one they
    had. Flushed, not committed."""
    values = {
        "microphone": microphone,
        "purpose": purpose,
        "step": 0,
        "expires_at": now + MOMENT_LIVES,
    }
    await session.execute(
        insert(VoiceMoment)
        .values(person_id=person_id, **values)
        .on_conflict_do_update(index_elements=[VoiceMoment.person_id], set_=values)
    )
    await session.flush()


async def moment_of(
    session: AsyncSession, *, person_id: uuid.UUID, now: datetime
) -> VoiceMoment | None:
    """The live moment of ``person_id`` at ``now``, if any (one ends at ``expires_at``)."""
    found = await session.execute(
        select(VoiceMoment)
        .where(VoiceMoment.person_id == person_id, VoiceMoment.expires_at > now)
        .order_by(VoiceMoment.expires_at.desc())
        .limit(1)
        .execution_options(populate_existing=True)
    )
    return found.scalar_one_or_none()


async def advance(session: AsyncSession, moment: VoiceMoment) -> None:
    """The moment has taken one more saying."""
    moment.step += 1
    await session.flush()


async def close_moment(session: AsyncSession, moment: VoiceMoment) -> None:
    """The moment is over."""
    await session.delete(moment)
    await session.flush()
