"""Deterministic engines for tests: no model, no dependency.

The transcript is the UTF-8 text carried in the bytes of the sound (``b"text:<words>"``), the
print is a fixed vector per first byte, and the voice says the text's own bytes.
"""

from collections.abc import AsyncIterator

from domo_speech.engines import Engines, ModelEntry, Transcript, VoicePrint, Word

_PREFIX = b"text:"
_CHUNK = 16
_BYTES_PER_SECOND = 32_000  # 16 kHz, 16 bits, mono
FAKE_PRINT_MODEL = "fake-print"


class FakeHears:
    async def transcribe(
        self, pcm: bytes, *, language: str | None, model: str | None = None
    ) -> Transcript:
        text = ""
        if pcm.startswith(_PREFIX):
            text = pcm[len(_PREFIX) :].decode("utf-8", errors="ignore")
        words = tuple(
            Word(text=word, start=index * 0.5, end=index * 0.5 + 0.4)
            for index, word in enumerate(text.split())
        )
        return Transcript(
            text=text,
            language=language or "en",
            seconds=len(pcm) / _BYTES_PER_SECOND,
            words=words,
        )


class FakeSays:
    async def say(
        self, text: str, *, voice: str | None, language: str | None, model: str | None = None
    ) -> AsyncIterator[bytes]:
        data = text.encode("utf-8")
        for start in range(0, len(data), _CHUNK):
            yield data[start : start + _CHUNK]


class FakePrints:
    async def voice_print(self, pcm: bytes, *, model: str | None = None) -> VoicePrint:
        first = pcm[0] if pcm else 0
        return VoicePrint(
            vector=(first / 255, 1 - first / 255, 0.5, 0.25),
            model=FAKE_PRINT_MODEL,
            seconds=len(pcm) / _BYTES_PER_SECOND,
            pitch_hz=100.0 + first,
            pitch_range_hz=20.0,
            rate=3.5,
        )


def fake_engines() -> Engines:
    return Engines(
        hears=FakeHears(),
        says=FakeSays(),
        prints=FakePrints(),
        models=lambda: (
            ModelEntry(id="fake-hear", does="hear"),
            ModelEntry(id="fake-say", does="say"),
            ModelEntry(id=FAKE_PRINT_MODEL, does="voice_print"),
        ),
        ready=lambda: "ready",
    )
