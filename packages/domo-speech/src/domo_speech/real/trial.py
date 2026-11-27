"""The trier of the real engines: fetches, loads, tries and unloads one candidate, with one
request or several at once."""

import ctypes
import os
import sys
import threading
import time
from pathlib import Path

import numpy as np

from domo_speech.data import ModelEntryData
from domo_speech.measure import (
    TRIAL_SENTENCES,
    NoProgram,
    Sample,
    Tried,
    Unfetched,
    heard_back,
    warmed_median,
)
from domo_speech.real.build import Engine, NoProgramSet, build, close, say_all, uses_gpu
from domo_speech.real.device import gpu_memory_mb
from domo_speech.real.hears import Hearing
from domo_speech.real.says import OUT_RATE, FlowVoice, ProgramVoice
from domo_speech.real.sound import floats_of, pcm_of, resample
from domo_speech.real.weights import WeightsError, ensure_weights

_HEARD_RATE = 16_000
_MADE_UP_SECONDS = 5.0


def rss_mb() -> float:
    """The memory this process holds, in megabytes (0 when it cannot be read)."""
    try:
        if sys.platform == "win32":

            class _Counters(ctypes.Structure):
                _fields_ = [
                    ("cb", ctypes.c_ulong),
                    ("faults", ctypes.c_ulong),
                    ("peak_working_set", ctypes.c_size_t),
                    ("working_set", ctypes.c_size_t),
                    ("quota_peak_paged", ctypes.c_size_t),
                    ("quota_paged", ctypes.c_size_t),
                    ("quota_peak_non_paged", ctypes.c_size_t),
                    ("quota_non_paged", ctypes.c_size_t),
                    ("pagefile", ctypes.c_size_t),
                    ("peak_pagefile", ctypes.c_size_t),
                ]

            counters = _Counters()
            counters.cb = ctypes.sizeof(_Counters)
            # The handle is pointer-sized: without these types it is cut to 32 bits.
            current = ctypes.windll.kernel32.GetCurrentProcess
            current.restype = ctypes.c_void_p
            info = ctypes.windll.psapi.GetProcessMemoryInfo
            info.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
            if info(current(), ctypes.byref(counters), counters.cb):
                return counters.working_set / 2**20
            return 0.0
        with Path("/proc/self/statm").open() as statm:
            return int(statm.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 2**20
    except (OSError, ValueError, AttributeError):
        return 0.0


def _made_up_sound() -> bytes:
    """A voice-like hum (a tone with harmonics, rising and falling), when no voice holds."""
    times = np.arange(int(_MADE_UP_SECONDS * _HEARD_RATE)) / _HEARD_RATE
    pitch = 140 + 30 * np.sin(2 * np.pi * 0.5 * times)
    phase = 2 * np.pi * np.cumsum(pitch) / _HEARD_RATE
    hum = sum(np.sin(k * phase) / k for k in range(1, 6))
    envelope = 0.5 + 0.5 * np.sin(2 * np.pi * 4 * times)
    return pcm_of(0.2 * hum * envelope)


# What one request's work gives: work seconds, seconds of sound, samples made, heard by sample.
_Done = tuple[float, float, tuple[Sample, ...], dict[str, str]]


def _try(engine: Engine, entry: ModelEntryData, samples: tuple[Sample, ...]) -> _Done:
    """(work seconds, seconds of sound, samples made, heard by sample)."""
    if isinstance(engine, FlowVoice | ProgramVoice):
        made: list[Sample] = []
        work = sound = 0.0
        languages = [lang for lang in TRIAL_SENTENCES if lang in entry.languages] or (
            list(TRIAL_SENTENCES) if "*" in entry.languages else []
        )
        for language in languages:
            text = TRIAL_SENTENCES[language]
            start = time.perf_counter()
            pcm = say_all(engine, text, language)
            work += time.perf_counter() - start
            sound += len(pcm) / (2 * OUT_RATE)
            at_16k = pcm_of(resample(floats_of(pcm), OUT_RATE, _HEARD_RATE))
            made.append(Sample(voice=entry.id, language=language, text=text, pcm=at_16k))
        return work, sound, tuple(made), {}
    sounds = [s.pcm for s in samples] or [_made_up_sound()]
    work = sound = 0.0
    heard: dict[str, str] = {}
    for index, pcm in enumerate(sounds):
        start = time.perf_counter()
        if isinstance(engine, Hearing):
            transcript = engine.transcribe(pcm, None)
            if samples:
                heard[samples[index].key] = transcript.text
        else:
            engine.voice_print(pcm)
        work += time.perf_counter() - start
        sound += len(pcm) / (2 * _HEARD_RATE)
    return work, sound, (), heard


def _together(
    engine: Engine, entry: ModelEntryData, samples: tuple[Sample, ...], at_once: int
) -> _Done:
    """The work of ``at_once`` requests started together, each in a thread of its own: the
    seconds until the last is done, the sound of one, what the first made, and what the one
    that heard worst heard (a hearing must hear right side by side too)."""
    if at_once == 1:
        return _try(engine, entry, samples)
    results: list[_Done | BaseException] = [ValueError("not_done")] * at_once

    def one(index: int) -> None:
        try:
            results[index] = _try(engine, entry, samples)
        except BaseException as error:
            results[index] = error

    workers = [threading.Thread(target=one, args=(i,), daemon=True) for i in range(at_once)]
    start = time.perf_counter()
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    wall = time.perf_counter() - start
    done: list[_Done] = []
    for result in results:
        if isinstance(result, BaseException):
            raise result
        done.append(result)

    def heard_right(result: _Done) -> float:
        return min((heard_back(s.text, result[3].get(s.key, "")) for s in samples), default=1.0)

    _, sound, made, _ = done[0]
    return wall, sound, made, min(done, key=heard_right)[3]


class Peak:
    """The most memory taken from the start of a trial to its end (the process's, and the
    graphics cards' when the model runs there), sampled every ``every`` seconds by a thread of
    its own: several requests at once take their most during the work, not after it."""

    def __init__(self, gpu: bool, *, every: float = 0.02) -> None:
        self._gpu = gpu
        self._every = every
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._watch, name="peak", daemon=True)
        self._rss_before = rss_mb()
        self._rss_most = self._rss_before
        before = gpu_memory_mb() if gpu else None
        self._gpu_before = before[0] if before else 0.0
        self._gpu_most = self._gpu_before
        self.gpu_total_mb = before[1] if before else 0.0

    def _sample(self) -> None:
        self._rss_most = max(self._rss_most, rss_mb())
        if self.gpu_total_mb:
            found = gpu_memory_mb()
            if found is not None:
                self._gpu_most = max(self._gpu_most, found[0])

    def _watch(self) -> None:
        while not self._stop.wait(self._every):
            self._sample()

    def __enter__(self) -> "Peak":
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join()
        self._sample()

    @property
    def memory_mb(self) -> float:
        return max(0.0, self._rss_most - self._rss_before)

    @property
    def gpu_memory_mb(self) -> float:
        return max(0.0, self._gpu_most - self._gpu_before)


class RealTrier:
    """Fetches the weights of a candidate, then loads, tries and unloads it."""

    def __init__(self, model_dir: Path, program: Path | None) -> None:
        self._model_dir = model_dir
        self._program = program

    def fetch(self, entry: ModelEntryData) -> None:
        if entry.engine == "program" and self._program is None:
            return  # not tried at all: nothing to fetch
        try:
            ensure_weights(entry, self._model_dir)
        except WeightsError:  # unreachable, or not the published weights
            raise Unfetched from None

    def __call__(
        self, entry: ModelEntryData, *, gpu: bool, samples: tuple[Sample, ...], at_once: int = 1
    ) -> Tried:
        if entry.engine == "program" and self._program is None:
            raise NoProgram
        try:
            weights = ensure_weights(entry, self._model_dir)
        except WeightsError:  # gone or changed since they were fetched: tried again later
            raise Unfetched from None
        on_gpu = uses_gpu(entry, gpu)
        with Peak(on_gpu) as peak:
            start = time.perf_counter()
            try:
                engine = build(entry, weights, gpu=on_gpu, program=self._program, at_once=at_once)
            except NoProgramSet:
                raise NoProgram from None
            loaded = time.perf_counter() - start
            try:
                # Never one cold call: a first call not counted, then the median of a few.
                work, sound, made, heard = warmed_median(
                    lambda: _together(engine, entry, samples, at_once), work=lambda done: done[0]
                )
            finally:
                close(engine)
        return Tried(
            device="gpu" if on_gpu else "cpu",
            load_seconds=loaded,
            memory_mb=peak.memory_mb,
            work_seconds=work,
            seconds_of_sound=sound,
            samples=made,
            heard=heard,
            gpu_memory_mb=peak.gpu_memory_mb,
            gpu_total_mb=peak.gpu_total_mb,
        )
