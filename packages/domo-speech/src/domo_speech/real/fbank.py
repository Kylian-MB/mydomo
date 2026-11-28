"""Log mel filter banks computed the way Kaldi does (what the voice print models were trained
on): 25 ms frames every 10 ms, a Hamming window, 80 bands, no dither."""

from functools import cache
from typing import Final

import numpy as np
import numpy.typing as npt

from domo_speech.real.sound import Samples

_RATE: Final = 16_000
_LENGTH: Final = 400
_SHIFT: Final = 160
_FFT: Final = 512
_LOW_HZ: Final = 20.0
_PREEMPHASIS: Final = 0.97


def _mel(hz: npt.ArrayLike) -> npt.NDArray[np.float64]:
    return 1127.0 * np.log(1.0 + np.asarray(hz, dtype=np.float64) / 700.0)


@cache
def _banks(bands: int) -> npt.NDArray[np.float32]:
    """Triangles on the mel scale over the first ``_FFT / 2`` bins, as Kaldi builds them."""
    bins = _FFT // 2
    low, high = float(_mel(_LOW_HZ)), float(_mel(_RATE / 2))
    step = (high - low) / (bands + 1)
    mel = _mel(np.arange(bins) * (_RATE / _FFT))
    banks = np.zeros((bands, bins), dtype=np.float64)
    for band in range(bands):
        left, centre, right = low + band * step, low + (band + 1) * step, low + (band + 2) * step
        rising = (mel - left) / (centre - left)
        falling = (right - mel) / (right - centre)
        inside = (mel > left) & (mel < right)
        banks[band] = np.where(inside, np.where(mel <= centre, rising, falling), 0.0)
    return banks.astype(np.float32)


def fbank(samples: Samples, bands: int = 80) -> npt.NDArray[np.float32]:
    """(frames, bands) log mel energies of 16 kHz floats in [-1, 1), mean removed per band (what
    the voice print models expect). Empty for under 25 ms."""
    scaled = samples.astype(np.float64) * 32768.0
    if len(scaled) < _LENGTH:
        return np.zeros((0, bands), dtype=np.float32)
    count = 1 + (len(scaled) - _LENGTH) // _SHIFT
    index = np.arange(_LENGTH)[None, :] + _SHIFT * np.arange(count)[:, None]
    frames = scaled[index]
    frames = frames - frames.mean(axis=1, keepdims=True)
    previous = np.concatenate([frames[:, :1], frames[:, :-1]], axis=1)
    frames = frames - _PREEMPHASIS * previous
    frames = frames * np.hamming(_LENGTH)[None, :]
    power = np.abs(np.fft.rfft(frames, n=_FFT, axis=1)) ** 2
    energies = power[:, : _FFT // 2] @ _banks(bands).T.astype(np.float64)
    logs = np.log(np.maximum(energies, np.finfo(np.float32).eps))
    return (logs - logs.mean(axis=0, keepdims=True)).astype(np.float32)
