"""What an engine is: three small interfaces and what each returns.

The service only knows these; the real engines (one module each, imported lazily) and the fake
ones (``fakes``) both fit them.
"""

from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol

Readiness = Literal["ready", "preparing", "failed"]


@dataclass(frozen=True)
class Word:
    text: str
    start: float
    end: float


@dataclass(frozen=True)
class Transcript:
    text: str
    language: str
    seconds: float
    words: tuple[Word, ...]


@dataclass(frozen=True)
class VoicePrint:
    vector: tuple[float, ...]
    model: str
    seconds: float
    pitch_hz: float | None
    pitch_range_hz: float | None
    # Syllables or words per second, whichever the engine measures (said in ``model``'s entry
    # of the data file).
    rate: float | None


class UnknownModel(Exception):
    """The model asked for is not one the service lists (the caller chooses among those)."""


class EngineUnavailable(Exception):
    """The model is listed but cannot be loaded now (its weights are gone, memory is short)."""


class Hears(Protocol):
    async def transcribe(
        self, pcm: bytes, *, language: str | None, model: str | None, hint: str | None = None
    ) -> Transcript:
        """``pcm`` is s16le, 16 kHz, mono; ``language`` is heard in, ``hint`` (the speaker's known
        language) only when the model's own detection is not clear."""
        ...


class Says(Protocol):
    def say(
        self, text: str, *, voice: str | None, language: str | None, model: str | None
    ) -> AsyncIterator[bytes]:
        """Sound as chunks of PCM s16le, 24 000 Hz, mono."""
        ...


class Prints(Protocol):
    async def voice_print(self, pcm: bytes, *, model: str | None) -> VoicePrint:
        """``pcm`` is s16le, 16 kHz, mono."""
        ...


@dataclass(frozen=True)
class ModelEntry:
    id: str
    does: Literal["hear", "say", "voice_print"]
    # What the service measured of it on this machine (device, load time, memory, real-time
    # factor...), empty for an engine that measures nothing.
    measures: Mapping[str, float | str] = field(default_factory=dict[str, float | str])
    # The licence of its weights (an SPDX id, or ``LicenseRef-...``), where to read it, and
    # whether it restricts how the model may be used.
    licence: str = ""
    licence_url: str = ""
    restricted: bool = False
    # For a voice print: the cosine similarities (print_low, same_voice, print_high).
    thresholds: tuple[float, float, float] | None = None
    # How many requests it serves at once (what was measured, lowered by the owner's setting).
    at_once: int = 1
    # Kept although too slow on this machine: nothing of its use was fast enough.
    slow: bool = False


def _nothing_more() -> Mapping[str, object]:
    return {}


@dataclass(frozen=True)
class Engines:
    hears: Hears
    says: Says
    prints: Prints
    # The models that hold now (the list may change once the service has measured them).
    models: Callable[[], tuple[ModelEntry, ...]]
    ready: Callable[[], Readiness]
    # What ``/health`` says besides the status (for one, the uses for which nothing holds).
    health: Callable[[], Mapping[str, object]] = _nothing_more
