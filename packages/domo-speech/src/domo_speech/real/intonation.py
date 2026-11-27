"""The intonation of a voice: its median pitch, the range of its pitch and its rate.

A microphone changes the colour of a voice more than its melody: these measures help to know a
person whose print a new microphone blurs. Plain numbers, computed here with no model.
"""

from typing import Final

import numpy as np

from domo_speech.real.sound import Samples, floats_of

_RATE: Final = 16_000
_FRAME: Final = 640  # 40 ms
_HOP: Final = 160  # 10 ms
_LOW_HZ: Final = 60.0
_HIGH_HZ: Final = 400.0
# A frame is voiced when it is loud enough (against the loudest) and periodic enough.
_LOUD: Final = 0.05
_PERIODIC: Final = 0.5
_MIN_VOICED_SECONDS: Final = 1.0


def _frames(samples: Samples) -> Samples:
    if len(samples) < _FRAME:
        return np.zeros((0, _FRAME), dtype=np.float32)
    count = 1 + (len(samples) - _FRAME) // _HOP
    index = np.arange(_FRAME)[None, :] + _HOP * np.arange(count)[:, None]
    return samples[index]


def _pitch(frame: Samples) -> float | None:
    """The pitch of one frame by autocorrelation, ``None`` if it is not periodic."""
    centred = frame - frame.mean()
    spectrum = np.fft.rfft(centred, n=2 * _FRAME)
    correlation = np.fft.irfft(spectrum * np.conj(spectrum))[:_FRAME]
    if correlation[0] <= 0:
        return None
    correlation = correlation / correlation[0]
    low, high = int(_RATE / _HIGH_HZ), int(_RATE / _LOW_HZ)
    lag = low + int(np.argmax(correlation[low:high]))
    if correlation[lag] < _PERIODIC:
        return None
    # A parabola through the peak and its neighbours gives the lag between two samples.
    before, at, after = correlation[lag - 1], correlation[lag], correlation[lag + 1]
    bend = before - 2 * at + after
    shift = 0.0 if bend == 0 else 0.5 * (before - after) / bend
    return _RATE / (lag + shift)


def intonation(pcm: bytes) -> tuple[float | None, float | None, float | None]:
    """(median pitch in Hz, pitch range between the 10th and 90th centile in Hz, rate in loudness
    peaks per voiced second) of 16 kHz mono s16le sound; three ``None`` for under a second of
    voiced sound."""
    frames = _frames(floats_of(pcm))
    if len(frames) == 0:
        return None, None, None
    loudness = np.sqrt((frames**2).mean(axis=1))
    loud = loudness >= _LOUD * float(loudness.max()) if loudness.max() > 1e-4 else loudness > 1
    pitches = [(index, p) for index in np.flatnonzero(loud) if (p := _pitch(frames[index]))]
    if len(pitches) * _HOP / _RATE < _MIN_VOICED_SECONDS:
        return None, None, None
    values = np.array([p for _, p in pitches])
    median = float(np.median(values))
    spread = float(np.percentile(values, 90) - np.percentile(values, 10))
    # Peaks of the loudness, smoothed over 50 ms, at least 100 ms apart: about the syllables.
    voiced = np.zeros(len(frames), dtype=bool)
    voiced[[index for index, _ in pitches]] = True
    envelope = np.convolve(np.where(voiced, loudness, 0.0), np.ones(5) / 5, mode="same")
    peaks = 0
    last = -10
    for index in range(1, len(envelope) - 1):
        if (
            envelope[index] > envelope[index - 1]
            and envelope[index] >= envelope[index + 1]
            and envelope[index] > 0.3 * envelope.max()
            and index - last >= 10
        ):
            peaks += 1
            last = index
    rate = peaks / (len(pitches) * _HOP / _RATE)
    return median, spread, rate
