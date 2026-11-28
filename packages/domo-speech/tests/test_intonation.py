"""Intonation, filter banks and resampling, on sounds made in the test."""

import numpy as np
import pytest
from domo_speech.real.fbank import fbank
from domo_speech.real.intonation import intonation
from domo_speech.real.prints import unit_vector
from domo_speech.real.sound import Resampler, floats_of, pcm_of, resample

RATE = 16_000


def _tone(hz: float, seconds: float, rate: int = RATE) -> np.ndarray:
    times = np.arange(int(seconds * rate)) / rate
    return (0.3 * np.sin(2 * np.pi * hz * times)).astype(np.float32)


def test_a_220_hz_tone_reads_about_220() -> None:
    pitch, spread, rate = intonation(pcm_of(_tone(220, 2.0)))

    assert pitch is not None and abs(pitch - 220) < 3
    assert spread is not None and spread < 5
    assert rate is not None


def test_a_rising_voice_has_a_range() -> None:
    times = np.arange(2 * RATE) / RATE
    pitch_curve = 150 + 50 * times  # 150 Hz to 250 Hz
    sound = 0.3 * np.sin(2 * np.pi * np.cumsum(pitch_curve) / RATE)

    pitch, spread, _ = intonation(pcm_of(sound))

    assert pitch is not None and 180 < pitch < 220
    assert spread is not None and 60 < spread < 100


def test_silence_gives_no_measure() -> None:
    assert intonation(bytes(2 * RATE * 2)) == (None, None, None)


def test_under_a_second_of_voice_gives_no_measure() -> None:
    assert intonation(pcm_of(_tone(220, 0.6))) == (None, None, None)


def test_floats_and_pcm_go_back_and_forth() -> None:
    sound = _tone(440, 0.1)
    assert np.allclose(floats_of(pcm_of(sound)), sound, atol=1e-4)


def test_resampling_keeps_the_pitch_and_the_length() -> None:
    sound = _tone(440, 1.0, rate=22_050)

    out = resample(sound, 22_050, 24_000)

    assert abs(len(out) - 24_000) <= 1
    spectrum = np.abs(np.fft.rfft(out))
    assert abs(np.argmax(spectrum) * 24_000 / len(out) - 440) < 2


def test_resampling_chunk_by_chunk_equals_resampling_at_once() -> None:
    sound = _tone(300, 0.5, rate=44_100)
    resampler = Resampler(44_100, 24_000)

    pieces = [resampler.push(sound[i : i + 1000]) for i in range(0, len(sound), 1000)]
    out = np.concatenate([*pieces, resampler.flush()])

    assert np.allclose(out, resample(sound, 44_100, 24_000), atol=1e-5)


def test_going_down_a_rate_removes_what_it_cannot_hold() -> None:
    # 15 kHz cannot exist at 24 kHz: it is filtered, not folded down to 9 kHz.
    sound = _tone(15_000, 0.5, rate=44_100)

    out = resample(sound, 44_100, 24_000)

    # (the very start and end are a sudden step, which holds every frequency)
    assert np.abs(out[100:-100]).max() < 0.01


def test_filter_banks_have_80_bands_every_10_ms() -> None:
    features = fbank(_tone(440, 1.0))

    assert features.shape == (98, 80)
    assert np.allclose(features.mean(axis=0), 0, atol=1e-3)
    assert fbank(_tone(440, 0.01)).shape == (0, 80)


def test_a_print_vector_is_unit_finite_and_not_empty() -> None:
    assert np.allclose(np.linalg.norm(unit_vector(np.array([3.0, 4.0]))), 1.0)
    for broken in (np.zeros(4), np.array([1.0, np.nan]), np.array([np.inf, 1.0]), np.zeros(0)):
        with pytest.raises(ValueError):
            unit_vector(broken)


def test_the_hearing_library_does_not_log_each_utterance() -> None:
    import logging

    import domo_speech.real.hears  # noqa: F401

    assert logging.getLogger("faster_whisper").level == logging.WARNING
