"""Hearing: speech-recognition models run by CTranslate2 (the library ``faster-whisper``).

``faster-whisper`` imports PyAV at the top of its audio module, to decode files; PyAV's wheels
bundle FFmpeg, which is LGPL, and LGPL code is never imported into the service. The sound given
here is already decoded, so a stand-in named ``av`` takes its place before the import; it fails
loudly if anything ever tries to decode with it. The workspace never installs PyAV (root
``pyproject.toml``).
"""

import logging
import os
import sys
import types
from pathlib import Path
from typing import Any, Final

from domo_speech.engines import Transcript, Word
from domo_speech.real.device import cuda_libraries, runtime
from domo_speech.real.sound import floats_of

_BYTES_PER_SECOND: Final = 32_000
# A detected language is clear from this probability, on a sound of at least this many seconds:
# below either, the speaker's known language (a hint) is heard in instead. The trials: every full
# sentence was detected right; a half-second "Oui." was read as English by every model.
CLEAR_AT: Final = 0.8
CLEAR_FROM_S: Final = 1.5

# The library logs the length and the language of every sound it hears: not worth a trace of who
# spoke when.
logging.getLogger("faster_whisper").setLevel(logging.WARNING)


class _NoDecoding(types.ModuleType):
    """Stands for PyAV: a version for the import, nothing else."""

    __version__ = "19.0.0"

    def __getattr__(self, name: str) -> Any:
        raise RuntimeError("decoding sound files is not part of the speech service")


def _faster_whisper() -> Any:
    if "av" not in sys.modules:
        sys.modules["av"] = _NoDecoding("av")
    return runtime("faster_whisper")


class Hearing:
    """One speech-recognition model, loaded on the processor or the graphics card, able to hear
    ``at_once`` sounds side by side (one worker of the library each; the library is safe to call
    from several threads). Blocking; the caller lets no more than ``at_once`` in at a time. On
    the processor the cores are shared among the workers."""

    def __init__(self, weights: Path, *, gpu: bool, at_once: int = 1) -> None:
        if gpu:
            cuda_libraries()
        cores = max(1, os.cpu_count() or 1)
        self._model: Any = _faster_whisper().WhisperModel(
            str(weights),
            device="cuda" if gpu else "cpu",
            compute_type="float16" if gpu else "int8",
            cpu_threads=0 if gpu else max(1, cores // at_once),
            num_workers=at_once,
        )
        self._languages: frozenset[str] = frozenset(self._model.supported_languages)

    def transcribe(
        self, pcm: bytes, language: str | None, *, hint: str | None = None
    ) -> Transcript:
        """The words of ``pcm``, heard in ``language`` when the model knows it; else the model
        detects the language, and ``hint`` (the speaker's known language) is heard in instead
        when that detection is not clear: a sound shorter than ``CLEAR_FROM_S`` or a detected
        language under ``CLEAR_AT``. The detection is made before any decoding (the library
        decodes lazily), so going to the hint costs one more pass of the encoder, not two
        transcriptions."""
        asked = language if language in self._languages else None
        audio = floats_of(pcm)
        segments, info = self._hear(audio, asked)
        if (
            asked is None
            and hint in self._languages
            and str(info.language) != hint
            and (
                len(pcm) / _BYTES_PER_SECOND < CLEAR_FROM_S
                or float(info.language_probability) < CLEAR_AT
            )
        ):
            segments, info = self._hear(audio, hint)
        parts = list(segments)
        words = tuple(
            Word(text=w.word.strip(), start=float(w.start), end=float(w.end))
            for part in parts
            for w in part.words or ()
        )
        return Transcript(
            text=" ".join(part.text.strip() for part in parts).strip(),
            language=str(info.language),
            seconds=len(pcm) / _BYTES_PER_SECOND,
            words=words,
        )

    def _hear(self, audio: Any, language: str | None) -> tuple[Any, Any]:
        return self._model.transcribe(
            audio,
            language=language,
            beam_size=5,
            word_timestamps=True,
            condition_on_previous_text=False,
            vad_filter=False,
        )

    def close(self) -> None:
        self._model = None
