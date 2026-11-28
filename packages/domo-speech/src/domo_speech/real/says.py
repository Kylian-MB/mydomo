"""Saying: a voice run inside the service (ONNX Runtime), or a voice that is a separate program.

Every voice answers PCM s16le, 24 000 Hz, mono, sentence by sentence.

The imported voice follows the published inference code of its authors (MIT; the notice is in
the package's ``NOTICE`` file), rewritten for one voice and a stream. The separate program (a
GPL voice, for one) is never imported: it is started with its arguments, fed the text on its
input and read as raw PCM on its output.
"""

import asyncio
import json
import re
import threading
import unicodedata
from collections.abc import AsyncIterator, Awaitable, Iterator
from pathlib import Path
from typing import Any, Final

import numpy as np

from domo_speech.real.device import onnx_providers, runtime
from domo_speech.real.sound import Resampler, Samples, floats_of, pcm_of

OUT_RATE: Final = 24_000
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")
_MAX_CHUNK: Final = 300
# Languages the imported voice reads by name; any other is read without one ("na").
_LANGUAGES: Final = frozenset(
    [
        "en",
        "ko",
        "ja",
        "ar",
        "bg",
        "cs",
        "da",
        "de",
        "el",
        "es",
        "et",
        "fi",
        "fr",
        "hi",
        "hr",
        "hu",
        "id",
        "it",
        "lt",
        "lv",
        "nl",
        "pl",
        "pt",
        "ro",
        "ru",
        "sk",
        "sl",
        "sv",
        "tr",
        "uk",
        "vi",
    ]
)
_STEPS: Final = 5
_SPEED: Final = 1.05
_PAUSE_SECONDS: Final = 0.25
# A separate program voice has this long to answer, plus a little per character of text.
PROGRAM_BASE_SECONDS: Final = 20.0
PROGRAM_PER_CHAR_SECONDS: Final = 0.1


def sentences(text: str, longest: int = _MAX_CHUNK) -> list[str]:
    """``text`` cut at the ends of sentences, no piece longer than ``longest`` characters unless
    a single sentence is."""
    pieces: list[str] = []
    current = ""
    for sentence in _SENTENCE_END.split(" ".join(text.split())):
        if current and len(current) + 1 + len(sentence) > longest:
            pieces.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        pieces.append(current)
    return pieces


def _clean(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    for old, new in (("–", "-"), ("—", "-"), ("’", "'"), ("‘", "'"), ("“", '"'), ("”", '"')):
        text = text.replace(old, new)
    text = re.sub(r"\s+", " ", text).strip()
    if text and not re.search(r"[.!?;:,'\")\]}…]$", text):
        text += "."
    return text


class FlowVoice:
    """The imported voice: four ONNX models (duration, text, flow, vocoder) and voice styles.
    Several answers may be said at once from several threads (an ONNX Runtime session may be run
    concurrently); only the random draw is taken one at a time."""

    def __init__(self, weights: Path, voices: tuple[str, ...], *, gpu: bool) -> None:
        ort = runtime("onnxruntime")
        onnx = weights / "onnx"
        providers = onnx_providers(gpu)
        options = ort.SessionOptions()

        def load(name: str) -> Any:
            return ort.InferenceSession(str(onnx / name), sess_options=options, providers=providers)

        self._duration = load("duration_predictor.onnx")
        self._text = load("text_encoder.onnx")
        self._flow = load("vector_estimator.onnx")
        self._vocoder = load("vocoder.onnx")
        config = json.loads((onnx / "tts.json").read_text(encoding="utf-8"))
        self.rate: int = int(config["ae"]["sample_rate"])
        self._chunk = int(config["ae"]["base_chunk_size"]) * int(
            config["ttl"]["chunk_compress_factor"]
        )
        self._latent_dim = int(config["ttl"]["latent_dim"]) * int(
            config["ttl"]["chunk_compress_factor"]
        )
        self._index: list[int] = json.loads((onnx / "unicode_indexer.json").read_text("utf-8"))
        self._styles: dict[str, tuple[Any, Any]] = {}
        for name in voices:
            style = json.loads((weights / "voice_styles" / f"{name}.json").read_text("utf-8"))
            self._styles[name] = tuple(  # type: ignore[assignment]
                np.array(style[key]["data"], dtype=np.float32).reshape(style[key]["dims"])
                for key in ("style_ttl", "style_dp")
            )
        self.voices = voices
        self._lock = threading.Lock()
        self._random = np.random.default_rng()

    def _ids(self, text: str, language: str) -> np.ndarray[Any, Any]:
        tagged = f"<{language}>{_clean(text)}</{language}>"
        codes = [ord(char) for char in tagged]
        ids = [self._index[code] if code < len(self._index) else -1 for code in codes]
        return np.array([[i for i in ids if i >= 0]], dtype=np.int64)

    def _one(self, text: str, language: str, voice: str) -> Samples:
        ttl, dp = self._styles[voice]
        ids = self._ids(text, language)
        mask = np.ones((1, 1, ids.shape[1]), dtype=np.float32)
        (duration,) = self._duration.run(
            None, {"text_ids": ids, "style_dp": dp, "text_mask": mask}
        )[:1]
        seconds = float(np.asarray(duration).reshape(-1)[0]) / _SPEED
        (embedded,) = self._text.run(None, {"text_ids": ids, "style_ttl": ttl, "text_mask": mask})[
            :1
        ]
        length = max(1, int(np.ceil(seconds * self.rate / self._chunk)))
        with self._lock:
            noise = self._random.standard_normal((1, self._latent_dim, length))
        latent = noise.astype(np.float32)
        latent_mask = np.ones((1, 1, length), dtype=np.float32)
        total = np.array([_STEPS], dtype=np.float32)
        for step in range(_STEPS):
            (latent,) = self._flow.run(
                None,
                {
                    "noisy_latent": latent,
                    "text_emb": embedded,
                    "style_ttl": ttl,
                    "text_mask": mask,
                    "latent_mask": latent_mask,
                    "current_step": np.array([step], dtype=np.float32),
                    "total_step": total,
                },
            )[:1]
        (wav,) = self._vocoder.run(None, {"latent": latent})[:1]
        samples = np.asarray(wav, dtype=np.float32).reshape(-1)
        return samples[: int(seconds * self.rate)]

    def say(self, text: str, voice: str | None, language: str | None) -> Iterator[bytes]:
        """Sentence by sentence, PCM at ``OUT_RATE``. Blocking."""
        chosen = voice if voice in self._styles else self.voices[0]
        reading = language if language in _LANGUAGES else "na"
        resampler = Resampler(self.rate, OUT_RATE)
        pause = np.zeros(int(_PAUSE_SECONDS * self.rate), dtype=np.float32)
        for index, piece in enumerate(sentences(text)):
            samples = self._one(piece, reading, chosen)
            if index:
                samples = np.concatenate([pause, samples])
            out = resampler.push(samples)
            if len(out):
                yield pcm_of(out)
        tail = resampler.flush()
        if len(tail):
            yield pcm_of(tail)

    def close(self) -> None:
        self._styles.clear()


class ProgramFailed(Exception):
    """The separate program stopped with an error (a plain code: nothing it printed)."""


class ProgramVoice:
    """A voice that is a separate program: ``program *args`` reads one line of text on its input
    and writes raw PCM s16le mono at ``rate`` on its output. It has until a deadline (a base,
    plus a little per character) to finish: past it, it is killed (``program_timeout``)."""

    def __init__(
        self,
        program: Path,
        args: tuple[str, ...],
        weights: Path,
        rate: int,
        *,
        base_seconds: float = PROGRAM_BASE_SECONDS,
        per_char_seconds: float = PROGRAM_PER_CHAR_SECONDS,
    ) -> None:
        self._program = program
        self._args = tuple(arg.replace("{weights}", str(weights)) for arg in args)
        self.rate = rate
        self._base = base_seconds
        self._per_char = per_char_seconds

    async def say(self, text: str) -> AsyncIterator[bytes]:
        process = await asyncio.create_subprocess_exec(
            str(self._program),
            *self._args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert process.stdin is not None and process.stdout is not None
        clock = asyncio.get_running_loop().time
        deadline = clock() + self._base + self._per_char * len(text)
        try:
            process.stdin.write(" ".join(text.split()).encode("utf-8") + b"\n")
            await process.stdin.drain()
            process.stdin.close()
            resampler = Resampler(self.rate, OUT_RATE)
            odd = b""
            while block := await self._before(deadline, process.stdout.read(16_384)):
                block = odd + block
                odd = block[len(block) - len(block) % 2 :]
                out = resampler.push(floats_of(block))
                if len(out):
                    yield pcm_of(out)
            if await self._before(deadline, process.wait()) != 0:
                raise ProgramFailed("program_failed")
            tail = resampler.flush()
            if len(tail):
                yield pcm_of(tail)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    @staticmethod
    async def _before[T](deadline: float, step: Awaitable[T]) -> T:
        left = deadline - asyncio.get_running_loop().time()
        try:
            return await asyncio.wait_for(step, max(0.0, left))
        except TimeoutError:
            raise ProgramFailed("program_timeout") from None


def program_voice_rate(weights: Path, args: tuple[str, ...]) -> int:
    """The sample rate of a voice program's model, read from the ``.onnx.json`` beside it."""
    model = next(arg for arg in args if arg.endswith(".onnx")).replace("{weights}", str(weights))
    config = json.loads(Path(model + ".json").read_text(encoding="utf-8"))
    return int(config["audio"]["sample_rate"])
