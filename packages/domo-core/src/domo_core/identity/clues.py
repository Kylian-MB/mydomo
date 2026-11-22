"""The clues a proof leaves behind (spec 6.3): a proof that succeeded is one more clue that it is
the person, for ``PROOF_CLUE_FOR`` and for the channel and the device it was given on, kept alive
while the person goes on speaking there. The code makes it, so it is strong; a clue Domo made
himself would say ``domo`` as its source.

Nothing here commits: the caller's transaction does.
"""

from datetime import datetime, timedelta
from typing import Final
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.conversation.speaker import Clue
from domo_core.identity.models import IdentityClue

# How long a proof counts as a clue, from the proof or from the last time the person spoke.
PROOF_CLUE_FOR: Final = timedelta(minutes=15)
_PROOF: Final = "proof"


async def keep_proof_clue(
    session: AsyncSession,
    *,
    person_id: UUID,
    channel: str,
    device_id: UUID | None,
    means: str,
    now: datetime,
) -> None:
    """Keep that ``person_id`` just proved it is them by ``means`` on ``channel`` (and
    ``device_id``), for ``PROOF_CLUE_FOR``. The person's clues already past are forgotten."""
    await session.execute(
        delete(IdentityClue).where(
            IdentityClue.person_id == person_id, IdentityClue.expires_at < now
        )
    )
    session.add(
        IdentityClue(
            person_id=person_id,
            kind=_PROOF,
            weighs="for",
            strength="strong",
            source="code",
            channel=channel,
            device_id=device_id,
            means=means,
            at=now,
            expires_at=now + PROOF_CLUE_FOR,
        )
    )
    await session.flush()


async def kept_clues(
    session: AsyncSession,
    *,
    person_id: UUID,
    channel: str,
    device_id: UUID | None,
    now: datetime,
) -> tuple[Clue, ...]:
    """The clues kept for ``person_id`` that are not past at ``now`` (one still counts at the very
    moment it ends), for the same channel and the same device (none when there is none on both
    sides), oldest first."""
    found = await session.execute(
        select(IdentityClue)
        .where(
            IdentityClue.person_id == person_id,
            IdentityClue.channel == channel,
            IdentityClue.device_id.is_not_distinct_from(device_id),
            IdentityClue.expires_at >= now,
        )
        .order_by(IdentityClue.at, IdentityClue.id)
    )
    return tuple(
        Clue(
            kind=row.kind,
            weighs="for" if row.weighs == "for" else "against",
            strength="strong" if row.strength == "strong" else "weak",
            source="code" if row.source == "code" else "domo",
            at=row.at,
            means=row.means,
        )
        for row in found.scalars()
    )


async def still_speaking(
    session: AsyncSession, *, person_id: UUID, channel: str, now: datetime
) -> None:
    """``person_id`` says something on ``channel`` at ``now``: their proofs there that are not past
    live ``PROOF_CLUE_FOR`` more, from now. A proof that is past is not brought back."""
    await session.execute(
        update(IdentityClue)
        .where(
            IdentityClue.person_id == person_id,
            IdentityClue.channel == channel,
            IdentityClue.kind == _PROOF,
            IdentityClue.expires_at >= now,
        )
        .values(expires_at=now + PROOF_CLUE_FOR)
    )
