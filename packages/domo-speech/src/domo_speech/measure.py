"""What holds on this machine: the service tries its candidates where it runs.

At the first start, and again when the machine changes (its processor, graphics cards, memory,
or the device setting) or the list of candidates does, every candidate the machine may run is
fetched, loaded and tried: the floor (small models for a plain processor) and the others, those
that need a graphics card only when there is one. Load time, memory and real-time factor are
written in ``measure.json`` inside the model directory (never in the repository), with which
candidates hold. The service lists only those; the assistant chooses among them.

Every voice fast enough says two sentences (French, English) (a voice too slow does too, and is
heard only when no voice is fast enough); hearing is tried on what they said and holds only if
it hears the words back, and a voice holds only if some hearing that holds heard it back (with
no hearing at all, a voice is judged on its speed). Prints are tried on the first voice's sound.
Without any voice, hearing and prints are tried on a made-up sound and judged on their speed
alone.

Once every use is measured (and published, each holding candidate serving one at a time), a
candidate that holds alone is tried again with two requests at once, then three, and so on,
until it no longer holds (the same speed and hearing rules, and the memory of the whole must
fit: at most a share of the machine's memory, and of the graphics card's when it runs there, at
its peak during the work; and loading must not fail), up to ``MAX_AT_ONCE``, within an overall
time (``AT_ONCE_SECONDS``). How many at once it holds is published as each candidate is done and
saved with the rest; the service serves up to that number side by side; on a machine that holds
one at a time, it serves one at a time.

On a machine with a graphics card, a candidate that can also run on the processor and does not
hold on the card (it fails to load or to run there, hangs, or is too slow) is tried once more on
the processor, and kept there when it holds: a card whose libraries are missing, or that runs out
of memory, never leaves a use with nothing the processor could do. It is then served on the
processor, as measured.

A use is never left with nothing for speed alone: when none of its candidates is fast enough,
the fastest that did its work right (it was not ``failed``, and hearing heard the voices back, a
voice was heard back) is kept, marked ``slow``: it holds, one request at a time, and the service
says the use is slow on this machine. A later measure where some candidate is fast enough
replaces it, and so does the fastest of a later measure where all are too slow; a measure where
nothing of the use holds (likely a machine busier then) keeps what held before. A model that
held fast and is tried again only to hear or be heard (a helper) keeps what it held, whatever
its new trial gives.

Each trial has a time limit: one that hangs is given up (``failed``) and the others go on. What
is measured is published as each use completes (voices with hearing, then prints). A candidate
whose weights could not be fetched (unreachable, or not the published ones) is saved as
``not_fetched``; the next round tries only those again, with one voice and one hearing that held
when they need them.

Nothing here knows a model: a ``Trier`` does the work (``domo_speech.real.trial``; fakes in the
tests).
"""

import hashlib
import json
import logging
import os
import platform
import re
import sys
import threading
import time
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal, Protocol, cast

from domo_speech.data import Does, ModelEntryData

logger = logging.getLogger(__name__)

MEASURE_FILE: Final = "measure.json"
# 2: each candidate that holds says how many requests at once it holds.
_VERSION: Final = 2
# The slowest a candidate may be and still hold: seconds of work per second of sound.
MAX_REAL_TIME: Final[Mapping[Does, float]] = {"hear": 0.5, "say": 0.5, "voice_print": 0.2}
# The share of the words a hearing must give back of what a voice said.
MIN_HEARD_BACK: Final = 0.6
# The most requests at once a candidate is tried with (a house where more people speak at the
# same moment is not expected).
MAX_AT_ONCE: Final = 8
# The most of the machine's memory (and of the graphics card's, for a model that runs there) one
# model may take to serve several at once: the three uses are loaded together, beside the rest.
MAX_MEMORY_SHARE: Final = 0.25
# The longest the trials with several requests at once may take, all candidates together, after
# the first measure of every use: past it, the candidates not yet tried serve one at a time.
AT_ONCE_SECONDS: Final = 900.0
# The longest one trial (load, try, unload; the weights are fetched before) may take.
TRIAL_SECONDS: Final = 300.0
# A model just loaded is slow on its first call (lazy loads, caches, kernels): a trial makes that
# call first, not counted, then times the work this many times and keeps the median.
TIMED_RUNS: Final = 3
# The sentences of the trial, said and heard back by the models only: no person hears them.
TRIAL_SENTENCES: Final[Mapping[str, str]] = {
    "fr": "Bonjour, peux-tu allumer la lumière du salon, s'il te plaît ?",
    "en": "Good morning, could you turn on the light in the living room, please?",
}

Device = Literal["cpu", "gpu"]
Why = Literal[
    "holds",
    "slow",  # holds although too slow: the fastest of a use none of whose candidates is fast
    "too_slow",
    "heard_wrong",
    "failed",
    "not_fetched",
    "needs_gpu",
    "no_program",
]


@dataclass(frozen=True)
class Machine:
    """What the measures depend on."""

    cpu: str
    cores: int
    ram_mb: int
    gpus: tuple[str, ...]
    device: str  # the setting: auto, cpu or gpu
    voice_program: str = ""  # the separate program voice, "" when none is set

    def fingerprint(self, candidates: Iterable[ModelEntryData]) -> str:
        """Changes when the machine, the setting or the candidates change. Memory is rounded to
        the gigabyte: a few megabytes reserved differently are the same machine."""
        described = {
            "cpu": self.cpu,
            "cores": self.cores,
            "ram_gb": round(self.ram_mb / 1024),
            "gpus": list(self.gpus),
            "device": self.device,
            "voice_program": self.voice_program,
            "candidates": sorted(f"{e.id}@{e.revision}" for e in candidates),
        }
        return hashlib.sha256(json.dumps(described, sort_keys=True).encode()).hexdigest()

    @property
    def gpu_allowed(self) -> bool:
        return bool(self.gpus) and self.device != "cpu"


@dataclass(frozen=True)
class Sample:
    """Sound a voice made for the trials (16 kHz mono s16le), and what it says."""

    voice: str
    language: str
    text: str
    pcm: bytes

    @property
    def key(self) -> str:
        return f"{self.voice}/{self.language}"


@dataclass(frozen=True)
class Tried:
    """What a trier measured of one candidate. ``seconds_of_sound`` is the sound it handled;
    ``samples`` what a voice said; ``heard`` what a hearing heard, by ``Sample.key``."""

    device: Device
    load_seconds: float
    memory_mb: float
    work_seconds: float
    seconds_of_sound: float
    samples: tuple[Sample, ...] = ()
    heard: Mapping[str, str] = field(default_factory=dict[str, str])
    # On a graphics card: the most of its memory the model took during the work, and how much
    # memory the card has (0 when it cannot be read, or the model runs on the processor).
    gpu_memory_mb: float = 0.0
    gpu_total_mb: float = 0.0


class Trier(Protocol):
    def fetch(self, entry: ModelEntryData) -> None:
        """Fetches the weights of ``entry`` (no time limit: a stalled download times out on its
        own). Raises ``Unfetched`` when they cannot be had or are not the published ones."""
        ...

    def __call__(
        self, entry: ModelEntryData, *, gpu: bool, samples: tuple[Sample, ...], at_once: int = 1
    ) -> Tried:
        """Loads ``entry`` (able to serve ``at_once`` requests side by side), does the work of
        ``at_once`` requests at the same time, and unloads it. ``work_seconds`` is the time until
        the last of them was done, ``seconds_of_sound`` the sound of one of them, ``memory_mb``
        (and ``gpu_memory_mb``) the most the whole took while working. Raises ``NoProgram`` when
        it is a separate program that is not set, any other exception when it fails."""
        ...


class Unfetched(Exception):
    pass


class NoProgram(Exception):
    pass


class _TimedOut(Exception):
    pass


@dataclass(frozen=True)
class Trial:
    """One line of ``measure.json``."""

    id: str
    does: Does
    holds: bool
    why: Why
    device: Device | None = None
    load_seconds: float | None = None
    memory_mb: float | None = None
    real_time_factor: float | None = None
    heard_back: float | None = None
    # How many requests at once it holds (a candidate that holds holds at least one).
    at_once: int | None = None

    def measures(self) -> dict[str, float | str]:
        found = {
            "device": self.device,
            "load_seconds": self.load_seconds,
            "memory_mb": self.memory_mb,
            "real_time_factor": self.real_time_factor,
            "heard_back": self.heard_back,
        }
        return {key: value for key, value in found.items() if value is not None}


@dataclass(frozen=True)
class Measured:
    fingerprint: str
    measured_at: str
    machine: Machine
    trials: tuple[Trial, ...]

    def held(self) -> tuple[Trial, ...]:
        return tuple(trial for trial in self.trials if trial.holds)

    def missing(self) -> tuple[Does, ...]:
        """The uses for which nothing holds, not even the floor."""
        holding = {trial.does for trial in self.held()}
        return tuple(use for use in ("hear", "say", "voice_print") if use not in holding)

    def slow(self) -> tuple[Does, ...]:
        """The uses served only by a model kept although too slow."""
        fast = {trial.does for trial in self.trials if trial.why == "holds"}
        return tuple(
            use
            for use in ("hear", "say", "voice_print")
            if use not in fast and any(t.does == use and t.why == "slow" for t in self.trials)
        )

    def again(self) -> tuple[Does, ...]:
        """The uses to measure again (a machine busy at one start must not leave the service
        slow or without them for good): served only slowly, or left with nothing while some
        candidate was too slow, failed or heard wrong."""
        fast = {trial.does for trial in self.trials if trial.why == "holds"}
        return tuple(
            use
            for use in ("hear", "say", "voice_print")
            if use not in fast
            and any(trial.does == use and trial.why in _AGAIN for trial in self.trials)
        )

    @property
    def complete(self) -> bool:
        """False when some weights could not be fetched: the measure is to be done again."""
        return all(trial.why != "not_fetched" for trial in self.trials)


# What a candidate of a use with nothing fast enough was, to be tried again.
_AGAIN: Final[frozenset[Why]] = frozenset({"slow", "too_slow", "failed", "heard_wrong"})


def _keep_slowest(trials: dict[str, Trial], use: Does) -> None:
    """When nothing of ``use`` holds, the fastest of its candidates that were only too slow (and,
    once judged, heard back) is kept, marked ``slow``."""
    if any(trial.does == use and trial.holds for trial in trials.values()):
        return
    slow = [
        trial
        for trial in trials.values()
        if trial.does == use
        and trial.why == "too_slow"
        and trial.real_time_factor is not None
        and (trial.heard_back is None or trial.heard_back >= MIN_HEARD_BACK)
    ]
    if slow:
        kept = min(slow, key=lambda trial: cast(float, trial.real_time_factor))
        trials[kept.id] = replace(kept, holds=True, why="slow", at_once=1)


def _words(text: str) -> list[str]:
    plain = unicodedata.normalize("NFKC", text).casefold()
    return re.findall(r"\w+", plain)


def heard_back(said: str, heard: str) -> float:
    """The share of the words said that were heard, in order of appearance (a plain measure, no
    model)."""
    wanted = _words(said)
    if not wanted:
        return 0.0
    remaining = _words(heard)
    found = 0
    for word in wanted:
        if word in remaining:
            remaining.remove(word)
            found += 1
    return found / len(wanted)


def _order(candidates: Iterable[ModelEntryData]) -> list[ModelEntryData]:
    """Voices first (their sound is the others' sample), then hearing, then prints; within a
    use, the floor first."""
    uses: tuple[Does, ...] = ("say", "hear", "voice_print")
    return sorted(candidates, key=lambda e: (uses.index(e.does), not e.floor))


def warmed_median[T](
    run: Callable[[], T], *, work: Callable[[T], float], runs: int = TIMED_RUNS
) -> T:
    """``run()`` once not counted, then ``runs`` times: the run whose ``work`` is the median."""
    run()
    done = sorted((run() for _ in range(runs)), key=work)
    return done[len(done) // 2]


def _limited(call: Callable[[], Tried], seconds: float) -> Tried:
    """``call()`` in a thread of its own, given up after ``seconds`` (the thread is left to end
    by itself: a library call cannot be stopped from outside)."""
    box: list[Tried | BaseException] = []

    def run() -> None:
        try:
            box.append(call())
        except BaseException as error:
            box.append(error)

    worker = threading.Thread(target=run, name="trial", daemon=True)
    worker.start()
    worker.join(seconds)
    if not box:
        raise _TimedOut
    if isinstance(box[0], BaseException):
        raise box[0]
    return box[0]


def _back(samples: Iterable[Sample], heard: Mapping[str, str]) -> float:
    """How well the samples of one voice were heard back: the worst of its sentences."""
    return min(heard_back(sample.text, heard.get(sample.key, "")) for sample in samples)


def _by_voice(samples: Iterable[Sample]) -> dict[str, list[Sample]]:
    found: dict[str, list[Sample]] = {}
    for sample in samples:
        found.setdefault(sample.voice, []).append(sample)
    return found


def _to_run(
    listed: tuple[ModelEntryData, ...], prior: Mapping[str, "Trial"], *, again: bool = True
) -> set[str]:
    """Everything on a first measure; else what was not fetched, and (when ``again``) what was
    slow, too slow, failed or heard wrong for a use with nothing fast enough, with one voice that
    held when a hearing is tried again (it needs something to hear) and one hearing that held when
    a voice is (it must be heard back)."""
    if not prior:
        return {entry.id for entry in listed}
    fast = {trial.does for trial in prior.values() if trial.why == "holds"}
    run = {
        entry.id
        for entry in listed
        if prior[entry.id].why == "not_fetched"
        or (again and prior[entry.id].why in _AGAIN and entry.does not in fast)
    }
    retried = {prior[i].does for i in run}
    for use, helper in (("hear", "say"), ("say", "hear")):
        if use in retried:
            held = [e.id for e in _order(listed) if e.does == helper and prior[e.id].holds]
            run.update(held[:1])
    return run


def measure(
    candidates: Iterable[ModelEntryData],
    machine: Machine,
    trier: Trier,
    *,
    previous: "Measured | None" = None,
    trial_seconds: float = TRIAL_SECONDS,
    publish: "Callable[[Measured], None] | None" = None,
    at_once_seconds: float = AT_ONCE_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    again: bool = True,
) -> "Measured":
    """Tries the candidates this machine may run, in order: all of them, or, given a previous
    measure of this machine and these candidates, only those it could not fetch, and (when
    ``again``) those of a use with nothing fast enough (and their helpers). Then how many at once
    each one that holds holds, within ``at_once_seconds``. A re-measure never takes away what
    held (see the module docstring)."""
    listed = tuple(candidates)
    fingerprint = machine.fingerprint(listed)
    prior: dict[str, Trial] = {}
    if previous is not None and previous.fingerprint == fingerprint:
        prior = {trial.id: trial for trial in previous.trials}
    run = _to_run(listed, prior, again=again)
    trials: dict[str, Trial] = dict(prior)
    heard: dict[str, Mapping[str, str]] = {}  # what each hearing tried now heard
    voices: dict[str, tuple[Sample, ...]] = {}  # what each voice fast enough said now
    kept: set[str] = set()  # tried again, and what it held before is kept

    def keep(entry_id: str) -> None:
        trials[entry_id] = prior[entry_id]
        heard.pop(entry_id, None)  # what it heard now does not judge the voices
        kept.add(entry_id)

    def keep_prior(use: Does) -> None:
        """With nothing of ``use`` holding now, what held before is kept."""
        if not any(t.does == use and t.holds for t in trials.values()):
            for entry_id, before in prior.items():
                if before.does == use and before.holds:
                    keep(entry_id)

    def snapshot() -> Measured:
        return Measured(
            fingerprint=fingerprint,
            measured_at=datetime.now(UTC).isoformat(timespec="seconds"),
            machine=machine,
            trials=tuple(trials[e.id] for e in listed if e.id in trials),
        )

    def judge_voices() -> None:
        """A voice tried now holds only if a hearing that holds heard it back."""
        hearing = [i for i in heard if trials[i].holds]
        if not hearing:
            return
        for voice, samples in voices.items():
            if voice in kept:
                continue
            back = max(_back(samples, heard[i]) for i in hearing)
            trial = replace(trials[voice], heard_back=round(back, 3))
            if back < MIN_HEARD_BACK and trial.holds:
                trial = replace(trial, holds=False, why="heard_wrong")
            trials[voice] = trial

    ordered = [entry for entry in _order(listed) if entry.id in run]
    for index, entry in enumerate(ordered):
        trials[entry.id] = _try_one(entry, machine, trier, voices, heard, trials, trial_seconds)
        before = prior.get(entry.id)
        if before is not None and before.why == "holds" and trials[entry.id].why != "holds":
            keep(entry.id)  # a helper only judges: it never loses what it held
        following = ordered[index + 1] if index + 1 < len(ordered) else None
        use_done = following is None or following.does != entry.does
        # Voices are published with hearing, which judges them (a hearing kept slow too).
        if use_done and (entry.does != "say" or not any(e.does == "hear" for e in ordered)):
            _keep_slowest(trials, entry.does)
            keep_prior(entry.does)
            judge_voices()
            _keep_slowest(trials, "say")
            keep_prior("say")
            if publish is not None and following is not None:
                publish(snapshot())
    # Every use is served (one at a time) while the candidates are tried with more at once; a
    # model kept slow does not hold even one, and is not tried with more.
    if publish is not None and any(trials[e.id].holds for e in ordered):
        publish(snapshot())
    holding = [e for e in ordered if trials[e.id].why == "holds" and e.id not in kept]
    until = clock() + at_once_seconds
    for entry in holding:
        if clock() >= until:
            logger.info("No time left to try %s with several at once", entry.id)
            continue
        samples = _samples_for(entry, voices, trials)
        # Where it held: a candidate kept on the processor is tried there with more at once.
        on_card = machine.gpu_allowed and trials[entry.id].device != "cpu"
        count = _most_at_once(entry, machine, trier, samples, trial_seconds, gpu=on_card)
        trials[entry.id] = replace(trials[entry.id], at_once=count)
        if publish is not None and count > 1:
            publish(snapshot())
    return snapshot()


def _samples_for(
    entry: ModelEntryData,
    voices: Mapping[str, tuple[Sample, ...]],
    trials: Mapping[str, "Trial"],
) -> tuple[Sample, ...]:
    """What a candidate is tried on: everything the voices said for a hearing, the first voice's
    sound for a print (a voice says its own). The sound of voices too slow only when no voice
    holds: a slow voice's poor sound must not fail a good hearing."""
    holding = {voice: said for voice, said in voices.items() if trials[voice].why == "holds"}
    chosen = holding or voices
    if entry.does == "hear":
        return tuple(s for said in chosen.values() for s in said)
    return next(iter(chosen.values()), ())


def _try_one(
    entry: ModelEntryData,
    machine: Machine,
    trier: Trier,
    voices: dict[str, tuple[Sample, ...]],
    heard: dict[str, Mapping[str, str]],
    trials: Mapping[str, "Trial"],
    trial_seconds: float,
) -> "Trial":
    """Tries ``entry`` where it may run: on the graphics card when allowed and, when it did not
    hold there and can run on the processor, once more on the processor (see the module
    docstring). What the processor gives is kept when it holds, or when the card only failed."""
    if entry.needs == "gpu" and not machine.gpu_allowed:
        return Trial(entry.id, entry.does, holds=False, why="needs_gpu")
    samples = _samples_for(entry, voices, trials)
    try:
        trier.fetch(entry)
    except Unfetched:
        return Trial(entry.id, entry.does, holds=False, why="not_fetched")
    trial = _try_on(entry, trier, samples, voices, heard, trial_seconds, gpu=machine.gpu_allowed)
    never_on_card = trial.why == "too_slow" and trial.device != "gpu"
    if (
        not machine.gpu_allowed
        or entry.needs == "gpu"
        or trial.why not in ("failed", "too_slow")
        or never_on_card
    ):
        return trial
    logger.info(
        "%s did not hold on the graphics card (%s): tried on the processor", entry.id, trial.why
    )
    on_cpu = _try_on(entry, trier, samples, voices, heard, trial_seconds, gpu=False)
    return on_cpu if on_cpu.holds or trial.why == "failed" else trial


def _try_on(
    entry: ModelEntryData,
    trier: Trier,
    samples: tuple[Sample, ...],
    voices: dict[str, tuple[Sample, ...]],
    heard: dict[str, Mapping[str, str]],
    trial_seconds: float,
    *,
    gpu: bool,
) -> "Trial":
    """One trial of ``entry`` (fetched), on the graphics card when ``gpu`` allows it."""
    try:
        tried = _limited(lambda: trier(entry, gpu=gpu, samples=samples), trial_seconds)
    except Unfetched:
        return Trial(entry.id, entry.does, holds=False, why="not_fetched")
    except NoProgram:
        return Trial(entry.id, entry.does, holds=False, why="no_program")
    except _TimedOut:
        logger.warning("The trial of %s took too long and was given up", entry.id)
        return Trial(entry.id, entry.does, holds=False, why="failed")
    except Exception as error:
        logger.warning("The trial of %s failed (%s)", entry.id, type(error).__name__)
        return Trial(entry.id, entry.does, holds=False, why="failed")
    factor = tried.work_seconds / tried.seconds_of_sound if tried.seconds_of_sound else None
    back: float | None = None
    why: Why = "holds"
    if factor is None or factor > MAX_REAL_TIME[entry.does]:
        why = "too_slow"
    if entry.does == "hear" and samples:
        back = max(_back(said, tried.heard) for said in _by_voice(samples).values())
        heard[entry.id] = tried.heard
        if back < MIN_HEARD_BACK and why == "holds":
            why = "heard_wrong"
    # A voice too slow is heard back too: it may be the one kept slow (it is heard only when
    # no voice is fast enough).
    if entry.does == "say" and why in ("holds", "too_slow") and tried.samples:
        voices[entry.id] = tried.samples
    return Trial(
        entry.id,
        entry.does,
        holds=why == "holds",
        why=why,
        device=tried.device,
        load_seconds=round(tried.load_seconds, 3),
        memory_mb=round(tried.memory_mb, 1),
        real_time_factor=None if factor is None else round(factor, 4),
        heard_back=None if back is None else round(back, 3),
        at_once=1 if why == "holds" else None,
    )


def _most_at_once(
    entry: ModelEntryData,
    machine: Machine,
    trier: Trier,
    samples: tuple[Sample, ...],
    trial_seconds: float,
    *,
    gpu: bool,
) -> int:
    """How many requests at once ``entry`` holds (on the graphics card when ``gpu``): one more
    each time, until it no longer does (too slow, hears wrong, does not fit in memory, fails or
    hangs) or ``MAX_AT_ONCE``."""
    held = 1
    while held < MAX_AT_ONCE:
        count = held + 1
        try:
            tried = _limited(
                lambda count=count: trier(entry, gpu=gpu, samples=samples, at_once=count),
                trial_seconds,
            )
        except _TimedOut:
            logger.info("%s at %d at once took too long", entry.id, count)
            return held
        except Exception as error:
            logger.info("%s at %d at once failed (%s)", entry.id, count, type(error).__name__)
            return held
        if not _holds_together(entry, machine, samples, tried):
            return held
        held = count
    return held


def _holds_together(
    entry: ModelEntryData, machine: Machine, samples: tuple[Sample, ...], tried: Tried
) -> bool:
    if not tried.seconds_of_sound:
        return False
    if tried.work_seconds / tried.seconds_of_sound > MAX_REAL_TIME[entry.does]:
        return False
    if machine.ram_mb and tried.memory_mb > machine.ram_mb * MAX_MEMORY_SHARE:
        return False
    if tried.gpu_total_mb and tried.gpu_memory_mb > tried.gpu_total_mb * MAX_MEMORY_SHARE:
        return False
    if entry.does == "hear" and samples:
        back = max(_back(said, tried.heard) for said in _by_voice(samples).values())
        return back >= MIN_HEARD_BACK
    return True


def save(model_dir: Path, measured: Measured) -> None:
    """Written beside the weights, replacing the last measure in one step."""
    model_dir.mkdir(parents=True, exist_ok=True)
    body = {
        "version": _VERSION,
        "fingerprint": measured.fingerprint,
        "measured_at": measured.measured_at,
        "machine": asdict(measured.machine),
        "trials": [asdict(trial) for trial in measured.trials],
    }
    part = model_dir / (MEASURE_FILE + ".part")
    part.write_text(json.dumps(body, indent=2), encoding="utf-8")
    part.replace(model_dir / MEASURE_FILE)


def load(model_dir: Path) -> Measured | None:
    """The last measure, ``None`` when there is none or it cannot be read."""
    try:
        body = cast(dict[str, object], json.loads((model_dir / MEASURE_FILE).read_text("utf-8")))
        if body.get("version") != _VERSION:
            return None
        machine = cast(dict[str, object], body["machine"])
        return Measured(
            fingerprint=str(body["fingerprint"]),
            measured_at=str(body["measured_at"]),
            machine=Machine(
                cpu=str(machine["cpu"]),
                cores=int(cast(int, machine["cores"])),
                ram_mb=int(cast(int, machine["ram_mb"])),
                gpus=tuple(cast(list[str], machine["gpus"])),
                device=str(machine["device"]),
                voice_program=str(machine.get("voice_program", "")),
            ),
            trials=tuple(
                _read_trial(cast(dict[str, object], trial))
                for trial in cast(list[object], body["trials"])
            ),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _read_trial(line: dict[str, object]) -> Trial:
    """One line of the file; an ``at_once`` that is not a whole number counts as one at a time,
    and it is kept between one and ``MAX_AT_ONCE`` (the file may have been edited by hand)."""
    trial = Trial(**line)  # type: ignore[arg-type]
    count = line.get("at_once")
    if not trial.holds:
        return replace(trial, at_once=None)
    if not isinstance(count, int) or isinstance(count, bool):
        return replace(trial, at_once=1)
    return replace(trial, at_once=min(max(count, 1), MAX_AT_ONCE))


def current(
    model_dir: Path, candidates: Iterable[ModelEntryData], machine: Machine
) -> Measured | None:
    """The saved measure if it still fits this machine and these candidates, else ``None`` (the
    measure is to be done)."""
    saved = load(model_dir)
    if saved is None or saved.fingerprint != machine.fingerprint(candidates):
        return None
    return saved


def ram_mb() -> int:
    """The machine's memory, in megabytes (0 when it cannot be read)."""
    try:
        if sys.platform != "win32":
            return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") // 2**20
        import ctypes

        class _Status(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_ulong),
                ("load", ctypes.c_ulong),
                ("total", ctypes.c_ulonglong),
                ("available", ctypes.c_ulonglong),
                ("page_total", ctypes.c_ulonglong),
                ("page_available", ctypes.c_ulonglong),
                ("virtual_total", ctypes.c_ulonglong),
                ("virtual_available", ctypes.c_ulonglong),
                ("extended", ctypes.c_ulonglong),
            ]

        status = _Status()
        status.length = ctypes.sizeof(_Status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
            return int(status.total) // 2**20
    except (OSError, ValueError, AttributeError):
        pass
    return 0


def this_machine(device: str, gpus: tuple[str, ...], voice_program: str = "") -> Machine:
    """This machine, its graphics cards given by the runtime that will use them."""
    return Machine(
        cpu=platform.processor() or platform.machine(),
        cores=os.cpu_count() or 1,
        ram_mb=ram_mb(),
        gpus=gpus,
        device=device,
        voice_program=voice_program,
    )
