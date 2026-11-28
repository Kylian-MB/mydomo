"""Sound as numbers: PCM s16le to floats and back, and a resampler that works chunk by chunk."""

import math
from typing import Final

import numpy as np
import numpy.typing as npt

Samples = npt.NDArray[np.float32]

# Half the width of the interpolation kernel, in input samples.
_HALF: Final = 24


def floats_of(pcm: bytes) -> Samples:
    """PCM s16le as floats in [-1, 1)."""
    usable = len(pcm) - len(pcm) % 2
    return np.frombuffer(pcm[:usable], dtype="<i2").astype(np.float32) / 32768.0


def pcm_of(samples: npt.NDArray[np.floating]) -> bytes:
    """Floats in [-1, 1] as PCM s16le (clipped)."""
    clipped = np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0)
    return (clipped * 32767.0).astype("<i2").tobytes()


class Resampler:
    """Changes the rate of a stream of samples, one chunk at a time (a windowed sinc, with a
    low-pass when the rate goes down). ``push`` gives what can already be computed, ``flush`` the
    rest at the end."""

    def __init__(self, rate_in: int, rate_out: int) -> None:
        self._step = rate_in / rate_out
        self._cutoff = min(1.0, rate_out / rate_in) * 0.94
        self._same = rate_in == rate_out
        # Zeros before the first sample stand for the silence before the sound.
        self._buffer: Samples = np.zeros(_HALF, dtype=np.float32)
        self._time = float(_HALF)  # where the next output falls, in samples of the buffer
        self._fed = 0  # real samples received so far
        self._made = 0  # outputs given so far

    def _compute(self, until: float) -> Samples:
        count = max(0, math.ceil((until - self._time) / self._step))
        if count == 0:
            return np.zeros(0, dtype=np.float32)
        times = self._time + self._step * np.arange(count)
        base = np.floor(times).astype(np.int64)
        offsets = np.arange(-_HALF + 1, _HALF + 1)
        index = base[:, None] + offsets[None, :]
        distance = times[:, None] - index
        window = 0.5 + 0.5 * np.cos(np.pi * np.clip(distance / _HALF, -1.0, 1.0))
        kernel = self._cutoff * np.sinc(self._cutoff * distance) * window
        out = (self._buffer[index] * kernel).sum(axis=1).astype(np.float32)
        self._time += self._step * count
        # Keep only what the next outputs still need.
        drop = max(0, int(math.floor(self._time)) - _HALF)
        self._buffer = self._buffer[drop:]
        self._time -= drop
        return out

    def push(self, samples: Samples) -> Samples:
        if self._same:
            return samples
        self._fed += len(samples)
        self._buffer = np.concatenate([self._buffer, samples.astype(np.float32)])
        # An output needs ``_HALF`` samples after it.
        out = self._compute(len(self._buffer) - _HALF)
        self._made += len(out)
        return out

    def flush(self) -> Samples:
        if self._same:
            return np.zeros(0, dtype=np.float32)
        total = round(self._fed / self._step)
        self._buffer = np.concatenate([self._buffer, np.zeros(2 * _HALF, dtype=np.float32)])
        out = self._compute(len(self._buffer) - _HALF)[: max(0, total - self._made)]
        self._made += len(out)
        return out


def resample(samples: Samples, rate_in: int, rate_out: int) -> Samples:
    """All of ``samples`` at another rate."""
    resampler = Resampler(rate_in, rate_out)
    return np.concatenate([resampler.push(samples), resampler.flush()])
