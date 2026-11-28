"""The clues a proof leaves behind (spec 6.3): a proof that succeeded is one more clue that it is
the person, for ``PROOF_CLUE_FOR`` and for the channel, the device and the microphone it was given
on, kept alive while the person goes on speaking there. The code makes it, so it is strong. A
clue Domo made himself says ``domo`` as its source: his reading of how someone speaks (``manner``,
``keep_manner_clue``), weak, which never changes how sure the code is
(``conversation.speaker.standing``).

Nothing here commits: the caller's transaction does.
"""

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Final, Literal
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.conversation.speaker import Clue
from domo_core.identity.models import IdentityClue

# How long a proof counts as a clue, from the proof or from the last time the person spoke.
PROOF_CLUE_FOR: Final = timedelta(minutes=15)
_PROOF: Final = "proof"
_MANNER: Final = "manner"


async def _forget_the_past(session: AsyncSession, person_id: UUID, now: datetime) -> None:
    await session.execute(
        delete(IdentityClue).where(
            IdentityClue.person_id == person_id, IdentityClue.expires_at < now
        )
    )


async def keep_proof_clue(
    session: AsyncSession,
    *,
    person_id: UUID,
    channel: str,
    device_id: UUID | None,
    means: str,
    now: datetime,
    microphone: str | None = None,
) -> None:
    """Keep that ``person_id`` just proved it is them by ``means`` on ``channel`` (and
    ``device_id``, and the ``microphone`` that heard it), for ``PROOF_CLUE_FOR``. The person's
    clues already past are forgotten."""
    await _forget_the_past(session, person_id, now)
    session.add(
        IdentityClue(
            person_id=person_id,
            kind=_PROOF,
            weighs="for",
            strength="strong",
            source="code",
            channel=channel,
            device_id=device_id,
            microphone=microphone,
            means=means,
            at=now,
            expires_at=now + PROOF_CLUE_FOR,
        )
    )
    await session.flush()


async def keep_manner_clue(
    session: AsyncSession,
    *,
    person_id: UUID,
    channel: str,
    microphone: str | None,
    weighs: Literal["for", "against"],
    now: datetime,
    device_id: UUID | None = None,
) -> None:
    """Keep Domo's reading that how someone spoke at ``now`` on ``channel`` (``device_id``,
    ``microphone``) ``weighs`` for or against them being ``person_id``: a weak clue of his
    (``manner``, source ``domo``) for ``PROOF_CLUE_FOR``, read with the clues of the sentences that
    follow there (``kept_clues``). The person's clues already past are forgotten."""
    await _forget_the_past(session, person_id, now)
    session.add(
        IdentityClue(
            person_id=person_id,
            kind=_MANNER,
            weighs=weighs,
            strength="weak",
            source="domo",
            channel=channel,
            device_id=device_id,
            microphone=microphone,
            at=now,
            expires_at=now + PROOF_CLUE_FOR,
        )
    )
    await session.flush()


# How a stored weight is read: each known one as itself; one the code does not know (a value a
# widened check let in) weighs against, never for.
_WEIGHS: Final[Mapping[str, Literal["for", "against", "unknown"]]] = {
    "for": "for",
    "against": "against",
    "unknown": "unknown",
}


async def kept_clues(
    session: AsyncSession,
    *,
    person_id: UUID,
    channel: str,
    device_id: UUID | None,
    now: datetime,
    microphone: str | None = None,
) -> tuple[Clue, ...]:
    """The clues kept for ``person_id`` that are not past at ``now`` (one still counts at the very
    moment it ends), for the same channel, the same device and the same microphone (none when
    there is none on both sides), oldest first."""
    found = await session.execute(
        select(IdentityClue)
        .where(
            IdentityClue.person_id == person_id,
            IdentityClue.channel == channel,
            IdentityClue.device_id.is_not_distinct_from(device_id),
            IdentityClue.microphone.is_not_distinct_from(microphone),
            IdentityClue.expires_at >= now,
        )
        .order_by(IdentityClue.at, IdentityClue.id)
    )
    return tuple(
        Clue(
            kind=row.kind,
            weighs=_WEIGHS.get(row.weighs, "against"),
            strength="strong" if row.strength == "strong" else "weak",
            source="code" if row.source == "code" else "domo",
            at=row.at,
            means=row.means,
            degree=row.degree,
        )
        for row in found.scalars()
    )


async def still_speaking(
    session: AsyncSession,
    *,
    person_id: UUID,
    channel: str,
    now: datetime,
    microphone: str | None = None,
) -> None:
    """``person_id`` says something on ``channel`` (heard by ``microphone``, when one heard it) at
    ``now``: their proofs there, for that same microphone (none when none), that are not past live
    ``PROOF_CLUE_FOR`` more, from now. A proof that is past is not brought back."""
    await session.execute(
        update(IdentityClue)
        .where(
            IdentityClue.person_id == person_id,
            IdentityClue.channel == channel,
            IdentityClue.kind == _PROOF,
            IdentityClue.microphone.is_not_distinct_from(microphone),
            IdentityClue.expires_at >= now,
        )
        .values(expires_at=now + PROOF_CLUE_FOR)
    )
