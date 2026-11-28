"""Building one candidate's engine from its weights, on the processor or the graphics card."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

from domo_speech.data import ModelEntryData
from domo_speech.real.device import cuda_devices, runtime
from domo_speech.real.hears import Hearing
from domo_speech.real.prints import EmbeddingPrint
from domo_speech.real.says import FlowVoice, ProgramVoice, program_voice_rate

Engine = Hearing | FlowVoice | ProgramVoice | EmbeddingPrint


class NoProgramSet(Exception):
    """The candidate is a separate program, and none is set (``DOMO_SPEECH_VOICE_PROGRAM``)."""


def uses_gpu(entry: ModelEntryData, gpu_allowed: bool) -> bool:
    """Whether ``entry`` runs on the graphics card here: when allowed, and its runtime sees one."""
    if not gpu_allowed or entry.engine == "program":
        return False
    if entry.engine == "faster_whisper":
        return cuda_devices() > 0
    try:
        providers = runtime("onnxruntime").get_available_providers()
    except ImportError:
        return False
    return "CUDAExecutionProvider" in providers


def build(
    entry: ModelEntryData, weights: Path, *, gpu: bool, program: Path | None, at_once: int = 1
) -> Engine:
    """The engine of ``entry``, loaded (blocking: seconds, sometimes more), able to serve
    ``at_once`` requests side by side (the others are safe to call from several threads as they
    are; a separate program runs once per answer)."""
    if entry.engine == "faster_whisper":
        return Hearing(weights, gpu=gpu, at_once=at_once)
    if entry.engine == "flow_voice":
        return FlowVoice(weights, entry.voices, gpu=gpu)
    if entry.engine == "speaker_embedding":
        return EmbeddingPrint(weights, entry.id, gpu=gpu)
    if entry.engine == "program":
        if program is None:
            raise NoProgramSet
        return ProgramVoice(
            program, entry.program_args, weights, program_voice_rate(weights, entry.program_args)
        )
    raise ValueError("unknown_engine")


def close(engine: Engine) -> None:
    if not isinstance(engine, ProgramVoice):
        engine.close()


async def _collect(chunks: AsyncIterator[bytes]) -> bytes:
    return b"".join([chunk async for chunk in chunks])


def say_all(engine: FlowVoice | ProgramVoice, text: str, language: str) -> bytes:
    """All the sound of ``text`` at 24 kHz (blocking; for the trials)."""
    if isinstance(engine, ProgramVoice):
        return asyncio.run(_collect(engine.say(text)))
    return b"".join(engine.say(text, None, language))
