"""Helpers of the voice print tests: persons, prints and learnt voices written straight in."""

import math
from datetime import UTC, datetime

from domo_core.accounts.models import Person
from domo_core.channels.voice.models import VoicePrint, VoiceRef
from domo_core.llm.speech.client import Print, PrintThresholds
from sqlalchemy.ext.asyncio import AsyncSession

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
MODEL = "print-a"
# The measured bounds of a made-up print model: the starting values of the module.
THRESHOLDS = PrintThresholds(print_low=0.25, same_voice=0.50, print_high=0.75)
MIC = "desk"

# Four voices that are nowhere near each other (cosine 0), and one near the first.
ALEX = (1.0, 0.0, 0.0, 0.0)
SAM = (0.0, 1.0, 0.0, 0.0)
DANA = (0.0, 0.0, 1.0, 0.0)
TOM = (0.0, 0.0, 0.0, 1.0)
ALEX_NEAR = (0.98, 0.1, 0.05, 0.0)


def with_cosine(
    base: tuple[float, ...], cosine: float, away: tuple[float, ...]
) -> tuple[float, ...]:
    """A unit vector whose cosine with ``base`` is ``cosine`` (``away`` is orthogonal to it)."""
    rest = math.sqrt(1.0 - cosine * cosine)
    return tuple(cosine * b + rest * a for b, a in zip(base, away, strict=True))


def at_degree(degree: float) -> tuple[float, ...]:
    """A voice that reads as ``degree`` against ALEX with ``THRESHOLDS``."""
    return with_cosine(ALEX, THRESHOLDS.print_low + degree * 0.5, SAM)


def print_of(
    vector: tuple[float, ...],
    *,
    model: str = MODEL,
    seconds: float = 3.0,
    pitch_hz: float | None = None,
    rate: float | None = None,
) -> Print:
    return Print(
        vector=vector,
        model=model,
        seconds=seconds,
        pitch_hz=pitch_hz,
        pitch_range_hz=None,
        rate=rate,
    )


async def person(session: AsyncSession, name: str = "Alex", role: str = "owner") -> Person:
    found = Person(display_name=name, role=role)
    session.add(found)
    await session.flush()
    return found


async def learnt_voice(
    session: AsyncSession,
    who: Person,
    vector: tuple[float, ...],
    *,
    microphone: str = MIC,
    model: str = MODEL,
    habits: tuple[float, float, float, float] | None = None,
) -> VoiceRef:
    """``who`` has a base and a recent print of ``vector`` on ``microphone`` (under the person's
    reference, made if they have none)."""
    from sqlalchemy import select

    ref = await session.scalar(select(VoiceRef).where(VoiceRef.person_id == who.id))
    if ref is None:
        ref = VoiceRef(reference=f"v-{who.id.hex[:16]}", person_id=who.id)
        session.add(ref)
        await session.flush()
    for kind in ("base", "recent"):
        row = VoicePrint(
            ref_id=ref.id,
            microphone=microphone,
            kind=kind,
            model=model,
            vector=list(vector),
            utterances=5,
            visits=3,
            first_visit_at=NOW,
            last_at=NOW,
        )
        if habits is not None and kind == "base":
            row.pitch_mean, row.pitch_spread, row.rate_mean, row.rate_spread = habits
        session.add(row)
    await session.flush()
    return ref
