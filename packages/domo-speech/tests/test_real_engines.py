"""The real engines on the floor candidates, with real weights. Opt-in: they need the extra
``models``, ``DOMO_SPEECH_TEST_MODELS=1`` and ``DOMO_SPEECH_MODEL_DIR`` (where the weights are
fetched; a few hundred megabytes). The separate program voice also needs
``DOMO_SPEECH_VOICE_PROGRAM``. The sound made here stays in memory."""

import os
import sys
from functools import cache
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from domo_speech.data import ModelEntryData, candidates
from domo_speech.measure import TRIAL_SENTENCES, heard_back

pytestmark = [
    pytest.mark.speech_models,
    pytest.mark.skipif(
        os.environ.get("DOMO_SPEECH_TEST_MODELS") != "1"
        or "DOMO_SPEECH_MODEL_DIR" not in os.environ,
        reason="real speech models are opt-in (DOMO_SPEECH_TEST_MODELS=1, DOMO_SPEECH_MODEL_DIR)",
    ),
]

GPU = os.environ.get("DOMO_SPEECH_TEST_GPU") == "1"


def _entry(model_id: str) -> ModelEntryData:
    return next(e for e in candidates() if e.id == model_id)


def _floor(does: str, runs: str = "imported") -> list[str]:
    return [e.id for e in candidates() if e.does == does and e.floor and e.runs == runs]


HEAR = _floor("hear")[0]
VOICE = _floor("say")[0]
PRINTS = _floor("voice_print")
PROGRAM_VOICE = next(i for i in _floor("say", "separate_program") if "fr" in _entry(i).languages)


@cache
def _engine(model_id: str) -> Any:
    from domo_speech.real.build import build, uses_gpu
    from domo_speech.real.weights import ensure_weights

    entry = _entry(model_id)
    program = os.environ.get("DOMO_SPEECH_VOICE_PROGRAM")
    weights = ensure_weights(entry, Path(os.environ["DOMO_SPEECH_MODEL_DIR"]))
    return build(
        entry, weights, gpu=uses_gpu(entry, GPU), program=Path(program) if program else None
    )


@cache
def _said(language: str, voice: str) -> bytes:
    """What the floor voice says of the trial sentence, at 16 kHz."""
    from domo_speech.real.says import OUT_RATE
    from domo_speech.real.sound import floats_of, pcm_of, resample

    pcm = b"".join(_engine(VOICE).say(TRIAL_SENTENCES[language], voice, language))
    return pcm_of(resample(floats_of(pcm), OUT_RATE, 16_000))


def test_the_voice_gives_24k_pcm() -> None:
    chunks = list(_engine(VOICE).say("Bonjour. Il fait beau.", "F1", "fr"))

    assert len(chunks) >= 2  # sentence by sentence
    seconds = sum(len(c) for c in chunks) / (2 * 24_000)
    assert 0.8 < seconds < 6


@pytest.mark.parametrize("language", ["fr", "en"])
def test_french_and_english_are_heard_and_told_apart(language: str) -> None:
    transcript = _engine(HEAR).transcribe(_said(language, "M1"), None)

    assert transcript.language == language
    assert heard_back(TRIAL_SENTENCES[language], transcript.text) >= 0.8
    assert transcript.words and all(w.start <= w.end for w in transcript.words)


def test_pyav_is_never_imported() -> None:
    _engine(HEAR)

    assert type(sys.modules["av"]).__name__ == "_NoDecoding"


@pytest.mark.parametrize("model_id", PRINTS)
def test_two_sayings_of_one_voice_are_closer_than_two_voices(model_id: str) -> None:
    printer = _engine(model_id)

    one_fr = np.array(printer.voice_print(_said("fr", "F1")).vector)
    one_en = np.array(printer.voice_print(_said("en", "F1")).vector)
    other = np.array(printer.voice_print(_said("fr", "M1")).vector)

    assert float(one_fr @ one_en) > float(one_fr @ other)


@pytest.mark.skipif(
    "DOMO_SPEECH_VOICE_PROGRAM" not in os.environ, reason="no separate program voice installed"
)
async def test_the_separate_program_voice_is_never_imported() -> None:
    voice = _engine(PROGRAM_VOICE)
    before = set(sys.modules)

    pcm = b"".join([chunk async for chunk in voice.say("Bonjour, je suis là.")])

    assert len(pcm) > 24_000
    assert set(sys.modules) == before  # nothing of the program came into the service


def test_two_hearings_at_once_on_one_model_both_hear_right() -> None:
    from domo_speech.measure import Sample
    from domo_speech.real.trial import RealTrier

    samples = tuple(
        Sample(VOICE, language, TRIAL_SENTENCES[language], _said(language, "M1"))
        for language in ("fr", "en")
    )
    trier = RealTrier(Path(os.environ["DOMO_SPEECH_MODEL_DIR"]), None)

    tried = trier(_entry(HEAR), gpu=GPU, samples=samples, at_once=2)

    assert tried.work_seconds > 0 and tried.seconds_of_sound > 0
    for sample in samples:
        assert heard_back(sample.text, tried.heard[sample.key]) >= 0.8
