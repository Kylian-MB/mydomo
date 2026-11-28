"""Several people at once: how many requests each model serves side by side is measured on the
machine, and the service serves up to that number, the rest waiting their turn in order."""

import asyncio
import dataclasses
import gc
import json
import logging
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from domo_speech.app import create_app
from domo_speech.config import SpeechSettings
from domo_speech.data import ModelEntryData, candidates
from domo_speech.engines import Engines, EngineUnavailable, Transcript, VoicePrint
from domo_speech.fakes import FakeSays
from domo_speech.measure import (
    MAX_AT_ONCE,
    MEASURE_FILE,
    TRIAL_SENTENCES,
    Machine,
    Measured,
    Sample,
    Tried,
    current,
    measure,
)
from domo_speech.real import Library, RealHears, RealPrints, RealSays
from domo_speech.real.says import ProgramVoice

CPU_ONLY = Machine(cpu="x86", cores=4, ram_mb=8192, gpus=(), device="auto")


def _ids(does: str, **flags: object) -> list[str]:
    return [
        e.id
        for e in candidates()
        if e.does == does and all(getattr(e, k) == v for k, v in flags.items())
    ]


HEAR_FLOOR = _ids("hear", floor=True, runs="imported")[0]
HEAR_BETTER = _ids("hear", floor=False, needs="cpu")[0]
VOICE_FLOOR = _ids("say", floor=True, runs="imported")[0]
PRINT_FLOOR = _ids("voice_print", floor=True)[0]


class Trier:
    """Every candidate holds alone; ``holds_at`` says up to how many at once it stays fast
    enough, ``memory_mb`` what one more request at once costs, ``breaks_at`` the count at which
    loading fails."""

    def __init__(self, holds_at: dict[str, int] | None = None) -> None:
        self.holds_at = holds_at or {}
        self.memory_mb: dict[str, float] = {}
        self.breaks_at: dict[str, int] = {}
        self.levels: list[tuple[str, int]] = []

    def fetch(self, entry: ModelEntryData) -> None:
        pass

    def __call__(
        self, entry: ModelEntryData, *, gpu: bool, samples: tuple[Sample, ...], at_once: int = 1
    ) -> Tried:
        self.levels.append((entry.id, at_once))
        if at_once >= self.breaks_at.get(entry.id, MAX_AT_ONCE + 1):
            raise MemoryError
        made = ()
        if entry.does == "say":
            made = tuple(
                Sample(entry.id, language, text, b"sound:" + language.encode())
                for language, text in TRIAL_SENTENCES.items()
            )
        slow = at_once > self.holds_at.get(entry.id, 1)
        return Tried(
            device="cpu",
            load_seconds=1.0,
            memory_mb=self.memory_mb.get(entry.id, 100.0) * at_once,
            work_seconds=4.0 if slow else 0.4,
            seconds_of_sound=4.0,
            samples=made,
            heard={s.key: s.text for s in samples},
        )


def _tried_levels(trier: Trier, entry_id: str) -> list[int]:
    return [count for tried, count in trier.levels if tried == entry_id]


# The measure


def test_the_measure_records_how_many_at_once_each_candidate_holds() -> None:
    trier = Trier({HEAR_FLOOR: 3, VOICE_FLOOR: 2})

    measured = measure(candidates(), CPU_ONLY, trier)

    by_id = {t.id: t for t in measured.trials}
    assert by_id[HEAR_FLOOR].at_once == 3
    assert by_id[VOICE_FLOOR].at_once == 2
    assert by_id[PRINT_FLOOR].at_once == 1  # the floor case: one at a time
    # Tried with one more each time, until it no longer holds.
    assert _tried_levels(trier, HEAR_FLOOR) == [1, 2, 3, 4]
    assert _tried_levels(trier, PRINT_FLOOR) == [1, 2]
    # A candidate that does not hold alone is not tried with more.
    gpu_only = _ids("hear", needs="gpu")[0]
    assert by_id[gpu_only].at_once is None
    assert _tried_levels(trier, gpu_only) == []


def test_the_count_stops_at_the_highest_one_tried() -> None:
    trier = Trier({HEAR_FLOOR: 1000})

    measured = measure(candidates(), CPU_ONLY, trier)

    assert {t.id: t for t in measured.trials}[HEAR_FLOOR].at_once == MAX_AT_ONCE
    assert max(_tried_levels(trier, HEAR_FLOOR)) == MAX_AT_ONCE


def test_more_at_once_that_does_not_fit_in_memory_does_not_hold() -> None:
    trier = Trier({HEAR_FLOOR: 4, PRINT_FLOOR: 4})
    trier.memory_mb[HEAR_FLOOR] = 1500.0  # two at once: 3 GB, more than a quarter of 8 GB
    trier.breaks_at[PRINT_FLOOR] = 3  # a third at once cannot even be loaded

    measured = measure(candidates(), CPU_ONLY, trier)

    by_id = {t.id: t for t in measured.trials}
    assert by_id[HEAR_FLOOR].at_once == 1
    assert by_id[PRINT_FLOOR].at_once == 2


def test_the_count_is_saved_with_the_measure_and_an_older_measure_is_done_again(
    tmp_path: Path,
) -> None:
    Library(candidates(), tmp_path, CPU_ONLY, Trier({HEAR_FLOOR: 2}), _no_build).run_measure()

    saved = json.loads((tmp_path / MEASURE_FILE).read_text(encoding="utf-8"))
    assert {t["id"]: t["at_once"] for t in saved["trials"]}[HEAR_FLOOR] == 2
    assert current(tmp_path, candidates(), CPU_ONLY) is not None

    saved["version"] = 1  # measured before the service counted how many at once
    (tmp_path / MEASURE_FILE).write_text(json.dumps(saved), encoding="utf-8")
    assert current(tmp_path, candidates(), CPU_ONLY) is None


def test_every_use_is_served_one_at_a_time_before_the_trials_with_more() -> None:
    trier = Trier({HEAR_FLOOR: 3})
    seen: list[tuple[dict[str, int | None], int]] = []

    def publish(measured: Measured) -> None:
        tried_more = sum(1 for _, count in trier.levels if count > 1)
        seen.append(({t.id: t.at_once for t in measured.held()}, tried_more))

    final = measure(candidates(), CPU_ONLY, trier, publish=publish)

    every_use = next(
        (counts, more)
        for counts, more in seen
        if {u for u in ("hear", "say", "voice_print") if any(i in counts for i in _ids(u))}
        == {"hear", "say", "voice_print"}
    )
    assert every_use[1] == 0  # published before any trial with more at once
    assert set(every_use[0].values()) == {1}
    assert {t.id: t for t in final.trials}[HEAR_FLOOR].at_once == 3
    assert seen[-1][0][HEAR_FLOOR] == 3  # published once known


def test_the_trials_with_more_at_once_have_an_overall_time() -> None:
    trier = Trier(dict.fromkeys((e.id for e in candidates()), 3))
    ticks = iter(range(0, 10_000, 100))

    measured = measure(
        candidates(), CPU_ONLY, trier, at_once_seconds=150, clock=lambda: next(ticks)
    )

    refined = {t.id for t in measured.held() if t.at_once != 1}
    assert len(refined) == 1  # the time was up after the first
    assert {entry_id for entry_id, count in trier.levels if count > 1} == refined


def test_more_at_once_that_does_not_fit_on_the_graphics_card_does_not_hold() -> None:
    class OnCard(Trier):
        def __call__(
            self,
            entry: ModelEntryData,
            *,
            gpu: bool,
            samples: tuple[Sample, ...],
            at_once: int = 1,
        ) -> Tried:
            tried = super().__call__(entry, gpu=gpu, samples=samples, at_once=at_once)
            return dataclasses.replace(tried, gpu_memory_mb=1500.0 * at_once, gpu_total_mb=8192.0)

    measured = measure(candidates(), CPU_ONLY, OnCard({HEAR_FLOOR: 4}))

    assert {t.id: t for t in measured.trials}[HEAR_FLOOR].at_once == 1


@pytest.mark.parametrize(("written", "read"), [("3", 1), (50, MAX_AT_ONCE), (True, 1), (-2, 1)])
def test_a_count_written_wrong_in_the_file_is_read_safely(
    tmp_path: Path, written: object, read: int
) -> None:
    Library(candidates(), tmp_path, CPU_ONLY, Trier(), _no_build).run_measure()
    saved = json.loads((tmp_path / MEASURE_FILE).read_text(encoding="utf-8"))
    for trial in saved["trials"]:
        if trial["id"] == HEAR_FLOOR:
            trial["at_once"] = written
    (tmp_path / MEASURE_FILE).write_text(json.dumps(saved), encoding="utf-8")

    measured = current(tmp_path, candidates(), CPU_ONLY)

    assert measured is not None
    assert {t.id: t.at_once for t in measured.trials}[HEAR_FLOOR] == read


def test_the_peak_of_memory_is_taken_during_the_work(monkeypatch: pytest.MonkeyPatch) -> None:
    from domo_speech.real import trial

    working = threading.Event()
    sampled_while_working = threading.Event()

    def rss() -> float:
        if working.is_set():
            sampled_while_working.set()
            return 900.0
        return 100.0

    monkeypatch.setattr(trial, "rss_mb", rss)
    with trial.Peak(False, every=0.001) as peak:
        working.set()
        assert sampled_while_working.wait(HANG_GUARD)
        working.clear()

    assert peak.memory_mb == 800.0  # 100 before and after, 900 at the peak
    assert peak.gpu_memory_mb == 0.0


# Serving several at once

# The longest a fake engine or load waits for the test to let it go: a guard against a hang only
# (every wait below ends on an event, a barrier or a condition long before it).
HANG_GUARD = 60.0


def _no_build(entry: ModelEntryData, gpu: bool, at_once: int) -> Any:
    raise AssertionError("no engine is built in this test")


class Waiting:
    """A hearing (or print) engine whose calls wait for ``go``, or for the ``barrier`` (passed
    only by calls running side by side); records what started and how many ran at once. A sound
    in ``fail`` makes its call fail."""

    def __init__(self, entry_id: str, at_once: int) -> None:
        self.entry_id = entry_id
        self.at_once = at_once
        self.go = threading.Event()
        self.barrier: threading.Barrier | None = None
        self.fail: set[bytes] = set()
        self.closed = False
        self.closed_in: int | None = None
        self.started: list[bytes] = []
        self.running = 0
        self.most = 0
        self._lock = threading.Lock()

    def _enter(self, pcm: bytes) -> None:
        assert not self.closed, "an engine was closed under a request in flight"
        with self._lock:
            self.started.append(pcm)
            self.running += 1
            self.most = max(self.most, self.running)
        if self.barrier is not None:
            self.barrier.wait(HANG_GUARD)  # broken unless the calls run side by side
        else:
            self.go.wait(HANG_GUARD)
        assert not self.closed, "an engine was closed under a request in flight"
        with self._lock:
            self.running -= 1
        if pcm in self.fail:
            raise RuntimeError("the engine broke")

    def transcribe(
        self, pcm: bytes, language: str | None, *, hint: str | None = None
    ) -> Transcript:
        self._enter(pcm)
        return Transcript(text=pcm.decode(), language="en", seconds=1.0, words=())

    def voice_print(self, pcm: bytes) -> VoicePrint:
        self._enter(pcm)
        return VoicePrint((1.0,), self.entry_id, 1.0, None, None, None)

    def close(self) -> None:
        self.closed = True
        self.closed_in = threading.get_ident()


class Built:
    """Builds ``Waiting`` engines; a model in ``slow`` loads once its event is set; a model in
    ``too_big`` cannot be loaded for more than that many at once (0: not at all); a model in
    ``crowded`` cannot be loaded while another engine of its use is still loaded; ``error`` is
    what a load that does not fit raises."""

    def __init__(self) -> None:
        self.engines: dict[str, Waiting] = {}
        self.slow: dict[str, threading.Event] = {}
        self.too_big: dict[str, int] = {}
        self.crowded: set[str] = set()
        # Models whose engine is built with a barrier for that many calls side by side (set
        # before any call can reach the engine).
        self.together: dict[str, int] = {}
        self.error: Exception = MemoryError()
        self.loads: list[tuple[str, int]] = []
        self._uses: dict[str, str] = {}

    def __call__(self, entry: ModelEntryData, gpu: bool, at_once: int) -> Any:
        self.loads.append((entry.id, at_once))
        self._uses[entry.id] = entry.does
        if entry.id in self.slow:
            self.slow[entry.id].wait(HANG_GUARD)
        beside = [
            e for i, e in self.engines.items() if i != entry.id and self._uses[i] == entry.does
        ]
        if at_once > self.too_big.get(entry.id, MAX_AT_ONCE) or (
            entry.id in self.crowded and any(not e.closed for e in beside)
        ):
            raise self.error
        engine = Waiting(entry.id, at_once)
        if entry.id in self.together:
            engine.barrier = threading.Barrier(self.together[entry.id])
        self.engines[entry.id] = engine
        return engine


def _measured(
    tmp_path: Path, holds_at: dict[str, int], built: Any, cap: int | None = None
) -> Library:
    library = Library(candidates(), tmp_path, CPU_ONLY, Trier(holds_at), built, at_once_cap=cap)
    library.run_measure()
    return library


async def _until(check: Any) -> None:
    """Until ``check()`` holds (a condition, not a guess: it fails only if it never comes)."""
    for _ in range(int(HANG_GUARD / 0.005)):
        if check():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("never happened")


def _hear(hears: RealHears, word: bytes, model: str = HEAR_FLOOR) -> "asyncio.Task[Transcript]":
    return asyncio.create_task(hears.transcribe(word, language=None, model=model))


async def test_two_hearings_at_once_overlap_when_the_measure_says_two(tmp_path: Path) -> None:
    built = Built()
    built.together[HEAR_FLOOR] = 2  # each call waits until the other one runs too
    library = _measured(tmp_path, {HEAR_FLOOR: 2}, built)
    hears = RealHears(library)
    first, second = _hear(hears, b"one"), _hear(hears, b"two")

    assert ((await first).text, (await second).text) == ("one", "two")
    engine = built.engines[HEAR_FLOOR]
    assert (engine.most, engine.at_once) == (2, 2)


async def test_two_hearings_at_once_queue_when_the_measure_says_one(tmp_path: Path) -> None:
    built = Built()
    library = _measured(tmp_path, {HEAR_FLOOR: 1}, built)
    hears = RealHears(library)
    first, second = _hear(hears, b"one"), _hear(hears, b"two")
    await _until(lambda: library.queued("hear", HEAR_FLOOR) == 1)
    engine = built.engines[HEAR_FLOOR]
    await _until(lambda: engine.started)

    assert engine.started == [b"one"]  # the second waits its turn
    engine.go.set()
    assert ((await first).text, (await second).text) == ("one", "two")
    assert (engine.most, engine.at_once) == (1, 1)


@pytest.mark.parametrize("measured", [1, 2])
async def test_beyond_the_count_requests_wait_their_turn_in_order(
    tmp_path: Path, measured: int
) -> None:
    built = Built()
    library = _measured(tmp_path, {HEAR_FLOOR: measured}, built)
    hears = RealHears(library)
    words = [b"one", b"two", b"three", b"four"]
    tasks = [_hear(hears, word) for word in words]  # they arrive in this order
    await _until(lambda: library.queued("hear", HEAR_FLOOR) == len(words) - measured)
    engine = built.engines[HEAR_FLOOR]
    await _until(lambda: len(engine.started) == measured)

    assert engine.started == words[:measured]  # the others wait, none is dropped
    engine.go.set()
    heard = [t.text for t in await asyncio.gather(*tasks)]

    assert heard == ["one", "two", "three", "four"]
    assert engine.started == words
    assert engine.most == measured


async def test_the_order_is_kept_while_the_model_loads(tmp_path: Path) -> None:
    built = Built()
    built.slow[HEAR_FLOOR] = threading.Event()
    library = _measured(tmp_path, {HEAR_FLOOR: 1}, built)
    hears = RealHears(library)
    words = [b"one", b"two", b"three"]
    tasks = [_hear(hears, word) for word in words]
    await _until(lambda: library.queued("hear", HEAR_FLOOR) == 3)  # none in before the load

    built.slow[HEAR_FLOOR].set()
    await _until(lambda: HEAR_FLOOR in built.engines)
    built.engines[HEAR_FLOOR].go.set()
    await asyncio.gather(*tasks)

    assert built.engines[HEAR_FLOOR].started == words


async def test_a_request_in_flight_survives_a_model_switch(tmp_path: Path) -> None:
    built = Built()
    hears = RealHears(_measured(tmp_path, {}, built))

    in_flight = _hear(hears, b"long")
    await _until(lambda: HEAR_FLOOR in built.engines and built.engines[HEAR_FLOOR].running)
    switched = _hear(hears, b"other", HEAR_BETTER)
    await _until(lambda: HEAR_BETTER in built.engines)
    built.engines[HEAR_BETTER].go.set()

    assert (await switched).text == "other"
    old = built.engines[HEAR_FLOOR]
    assert not old.closed  # still serving the request in flight
    old.go.set()
    assert (await in_flight).text == "long"
    # Once its last request is served, the model asked for no more is unloaded, in a thread.
    await _until(lambda: old.closed)
    assert old.closed_in != threading.get_ident()
    assert not built.engines[HEAR_BETTER].closed


async def test_a_switch_back_reuses_the_model_still_in_flight(tmp_path: Path) -> None:
    built = Built()
    hears = RealHears(_measured(tmp_path, {}, built))

    in_flight = _hear(hears, b"long")
    await _until(lambda: HEAR_FLOOR in built.engines and built.engines[HEAR_FLOOR].running)
    first = built.engines[HEAR_FLOOR]
    switched = _hear(hears, b"b", HEAR_BETTER)
    await _until(lambda: HEAR_BETTER in built.engines)
    built.engines[HEAR_BETTER].go.set()
    await switched
    back = _hear(hears, b"back")
    first.go.set()

    assert ((await in_flight).text, (await back).text) == ("long", "back")
    assert built.engines[HEAR_FLOOR] is first  # not loaded twice
    assert built.loads.count((HEAR_FLOOR, 1)) == 1
    await _until(lambda: built.engines[HEAR_BETTER].closed)
    assert not first.closed


async def test_a_model_loading_holds_up_no_request_of_a_model_already_loaded(
    tmp_path: Path,
) -> None:
    built = Built()
    library = _measured(tmp_path, {}, built)
    hears, prints = RealHears(library), RealPrints(library)
    warm = asyncio.create_task(prints.voice_print(b"warm", model=PRINT_FLOOR))
    await _until(lambda: PRINT_FLOOR in built.engines)
    built.engines[PRINT_FLOOR].go.set()
    await warm

    built.slow[HEAR_FLOOR] = threading.Event()
    loading = _hear(hears, b"x")
    await _until(lambda: (HEAR_FLOOR, 1) in built.loads)
    printed = await asyncio.wait_for(prints.voice_print(b"now", model=PRINT_FLOOR), HANG_GUARD)

    assert printed.model == PRINT_FLOOR and not loading.done()
    built.slow[HEAR_FLOOR].set()
    await _until(lambda: HEAR_FLOOR in built.engines)
    built.engines[HEAR_FLOOR].go.set()
    assert (await loading).text == "x"


async def test_a_model_that_does_not_fit_for_the_count_is_served_with_fewer(
    tmp_path: Path,
) -> None:
    built = Built()
    built.too_big[HEAR_FLOOR] = 1  # measured alone at 3, but here only one fits
    library = _measured(tmp_path, {HEAR_FLOOR: 3}, built)
    hears = RealHears(library)

    first = _hear(hears, b"one")
    await _until(lambda: HEAR_FLOOR in built.engines)
    built.engines[HEAR_FLOOR].go.set()
    assert (await first).text == "one"
    assert (await _hear(hears, b"two")).text == "two"

    assert built.loads == [(HEAR_FLOOR, 3), (HEAR_FLOOR, 2), (HEAR_FLOOR, 1)]
    assert {m.id: m.at_once for m in library.models()}[HEAR_FLOOR] == 1  # remembered


async def test_a_model_that_cannot_be_loaded_at_all_answers_unavailable_then_tries_again(
    tmp_path: Path,
) -> None:
    built = Built()
    built.too_big[HEAR_FLOOR] = 0
    hears = RealHears(_measured(tmp_path, {}, built))

    with pytest.raises(EngineUnavailable):
        await hears.transcribe(b"x", language=None, model=HEAR_FLOOR)
    del built.too_big[HEAR_FLOOR]
    again = _hear(hears, b"y")
    await _until(lambda: HEAR_FLOOR in built.engines)
    built.engines[HEAR_FLOOR].go.set()

    assert (await again).text == "y"


async def test_a_request_that_goes_away_while_waiting_leaves_its_turn(tmp_path: Path) -> None:
    built = Built()
    library = _measured(tmp_path, {HEAR_FLOOR: 1}, built)
    hears = RealHears(library)
    first, gone = _hear(hears, b"one"), _hear(hears, b"gone")
    await _until(lambda: library.queued("hear", HEAR_FLOOR) == 1)

    gone.cancel()
    await _until(lambda: library.queued("hear", HEAR_FLOOR) == 0)
    last = _hear(hears, b"last")
    await _until(lambda: library.queued("hear", HEAR_FLOOR) == 1)
    built.engines[HEAR_FLOOR].go.set()

    assert ((await first).text, (await last).text) == ("one", "last")
    assert built.engines[HEAR_FLOOR].started == [b"one", b"last"]
    with pytest.raises(asyncio.CancelledError):
        await gone


async def test_a_request_that_goes_away_during_its_call_keeps_the_place_until_the_call_ends(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    built = Built()
    library = _measured(tmp_path, {HEAR_FLOOR: 1}, built)
    hears = RealHears(library)
    gone = _hear(hears, b"gone")
    await _until(lambda: HEAR_FLOOR in built.engines and built.engines[HEAR_FLOOR].running)
    engine = built.engines[HEAR_FLOOR]
    engine.fail.add(b"gone")  # its call will fail, and nobody will be there to hear it

    gone.cancel()
    after = _hear(hears, b"after")
    await _until(lambda: library.queued("hear", HEAR_FLOOR) == 1)
    assert engine.started == [b"gone"]  # its call still runs: the place is still taken
    engine.go.set()

    assert (await after).text == "after"
    assert engine.most == 1
    gc.collect()
    assert "never retrieved" not in caplog.text


async def test_an_engine_that_fails_frees_its_place(tmp_path: Path) -> None:
    built = Built()
    library = _measured(tmp_path, {HEAR_FLOOR: 1}, built)
    hears = RealHears(library)
    broken, after = _hear(hears, b"broken"), _hear(hears, b"after")
    await _until(lambda: library.queued("hear", HEAR_FLOOR) == 1)
    built.engines[HEAR_FLOOR].fail.add(b"broken")
    built.engines[HEAR_FLOOR].go.set()

    with pytest.raises(RuntimeError):
        await broken
    assert (await after).text == "after"


async def test_voice_prints_are_served_side_by_side_too(tmp_path: Path) -> None:
    built = Built()
    built.together[PRINT_FLOOR] = 2  # each call waits until the other one runs too
    prints = RealPrints(_measured(tmp_path, {PRINT_FLOOR: 2}, built))

    first = asyncio.create_task(prints.voice_print(b"one", model=PRINT_FLOOR))
    second = asyncio.create_task(prints.voice_print(b"two", model=PRINT_FLOOR))
    await asyncio.gather(first, second)

    assert built.engines[PRINT_FLOOR].most == 2


class _Program(ProgramVoice):
    """A separate program voice without a program: three chunks per answer."""

    def __init__(self) -> None:
        self.runs = 0

    async def say(self, text: str) -> AsyncIterator[bytes]:
        self.runs += 1
        for index in range(3):
            yield f"{text}:{index}".encode()


async def test_a_slow_reader_of_a_program_voice_holds_no_place(tmp_path: Path) -> None:
    program = _Program()

    def build_program(entry: ModelEntryData, gpu: bool, at_once: int) -> Any:
        return program

    says = RealSays(_measured(tmp_path, {VOICE_FLOOR: 1}, build_program))
    slow = says.say("a", voice=None, language=None, model=VOICE_FLOOR)
    assert await anext(slow) == b"a:0"  # and then reads no more for now

    other = [chunk async for chunk in says.say("b", voice=None, language=None, model=VOICE_FLOOR)]

    assert other == [b"b:0", b"b:1", b"b:2"]
    assert [chunk async for chunk in slow] == [b"a:1", b"a:2"]
    assert program.runs == 2


# The owner's cap


@pytest.mark.parametrize(("cap", "served"), [(2, 2), (5, 3), (None, 3)])
async def test_the_owner_may_lower_the_count_never_raise_it(
    tmp_path: Path, cap: int | None, served: int
) -> None:
    built = Built()
    library = _measured(tmp_path, {HEAR_FLOOR: 3}, built, cap=cap)
    hears = RealHears(library)

    listed = {m.id: m.at_once for m in library.models()}
    words = [b"a", b"b", b"c", b"d", b"e"]
    tasks = [_hear(hears, word) for word in words]
    await _until(lambda: library.queued("hear", HEAR_FLOOR) == len(words) - served)
    engine = built.engines[HEAR_FLOOR]
    await _until(lambda: len(engine.started) == served)
    engine.go.set()
    await asyncio.gather(*tasks)

    assert listed[HEAR_FLOOR] == served
    assert engine.at_once == served
    assert engine.most == served


def test_the_owners_cap_is_a_setting_of_at_least_one(monkeypatch: pytest.MonkeyPatch) -> None:
    assert SpeechSettings().at_once is None
    monkeypatch.setenv("DOMO_SPEECH_AT_ONCE", "2")
    assert SpeechSettings().at_once == 2
    monkeypatch.setenv("DOMO_SPEECH_AT_ONCE", "0")
    with pytest.raises(ValueError):
        SpeechSettings()


def test_a_setting_left_empty_is_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    # The compose file passes every setting, empty when the owner set none.
    for name in ("AT_ONCE", "VOICE_PROGRAM", "RESTRICTED_MODELS", "DEVICE"):
        monkeypatch.setenv(f"DOMO_SPEECH_{name}", "")
    found = SpeechSettings()
    assert (found.at_once, found.voice_program, found.restricted_models, found.device) == (
        None,
        None,
        True,
        "auto",
    )


# What the caller reads, and what is never logged


async def test_models_say_how_many_at_once_each_one_holds(tmp_path: Path) -> None:
    library = _measured(tmp_path, {HEAR_FLOOR: 3, VOICE_FLOOR: 2}, Built())
    engines = Engines(
        hears=RealHears(library),
        says=FakeSays(),
        prints=RealPrints(library),
        models=library.models,
        ready=library.ready,
        health=library.health,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(engines)), base_url="http://s"
    ) as client:
        listed = (await client.get("/v1/models")).json()["data"]

    by_id = {m["id"]: m for m in listed}
    assert by_id[HEAR_FLOOR]["at_once"] == 3
    assert by_id[VOICE_FLOOR]["at_once"] == 2
    assert by_id[PRINT_FLOOR]["at_once"] == 1
    assert "at_once" not in by_id[HEAR_FLOOR]["measures"]


async def test_nothing_of_the_sound_is_logged_while_requests_wait(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    built = Built()
    library = _measured(tmp_path, {HEAR_FLOOR: 1}, built)
    engines = Engines(
        hears=RealHears(library),
        says=FakeSays(),
        prints=RealPrints(library),
        models=library.models,
        ready=library.ready,
    )
    secret = "ripple banana seven"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(engines)), base_url="http://s"
    ) as client:

        async def hear() -> httpx.Response:
            return await client.post(
                "/v1/audio/transcriptions",
                files={"file": ("a.pcm", secret.encode(), "audio/pcm")},
                data={"model": HEAR_FLOOR},
            )

        tasks = [asyncio.create_task(hear()) for _ in range(3)]
        await _until(lambda: library.queued("hear", HEAR_FLOOR) == 2)
        built.engines[HEAR_FLOOR].go.set()
        answers = await asyncio.gather(*tasks)

    assert all(a.json()["text"] == secret for a in answers)
    logged = "\n".join(record.getMessage() for record in caplog.records) + caplog.text
    assert secret not in logged


# Second review: what the load does with the requests and the other models


PRINT_OTHER = [i for i in _ids("voice_print", needs="cpu") if i != PRINT_FLOOR][0]


async def test_a_model_unloaded_before_its_turn_to_load_blocks_no_later_load(
    tmp_path: Path,
) -> None:
    built = Built()
    built.slow[HEAR_FLOOR] = threading.Event()
    library = _measured(tmp_path, {}, built)
    hears, prints = RealHears(library), RealPrints(library)
    hearing = _hear(hears, b"h")  # 1. its load holds the turn to load
    await _until(lambda: (HEAR_FLOOR, 1) in built.loads)
    first = asyncio.create_task(prints.voice_print(b"x", model=PRINT_FLOOR))  # 2. waits its turn
    await _until(lambda: library.queued("voice_print", PRINT_FLOOR) == 1)
    first.cancel()  # 3. goes away: that model serves nobody
    await _until(lambda: library.queued("voice_print", PRINT_FLOOR) == 0)
    second = asyncio.create_task(prints.voice_print(b"y", model=PRINT_OTHER))  # 4. retires it
    await _until(lambda: library.queued("voice_print", PRINT_OTHER) == 1)

    built.slow[HEAR_FLOOR].set()  # 5. the first load ends
    await _until(lambda: PRINT_OTHER in built.engines and HEAR_FLOOR in built.engines)
    built.engines[PRINT_OTHER].go.set()
    built.engines[HEAR_FLOOR].go.set()

    assert (await asyncio.wait_for(second, HANG_GUARD)).model == PRINT_OTHER  # 6. no deadlock
    assert (await asyncio.wait_for(hearing, HANG_GUARD)).text == "h"
    assert PRINT_FLOOR not in built.engines  # never loaded for nobody


async def test_a_model_short_of_memory_beside_another_waits_for_it_at_the_full_number(
    tmp_path: Path,
) -> None:
    built = Built()
    built.crowded.add(HEAR_BETTER)  # does not fit while the other hearing is loaded
    library = _measured(tmp_path, {HEAR_BETTER: 4}, built)
    hears = RealHears(library)
    long = _hear(hears, b"long")
    await _until(lambda: HEAR_FLOOR in built.engines and built.engines[HEAR_FLOOR].running)

    better = _hear(hears, b"better", HEAR_BETTER)
    await _until(lambda: (HEAR_BETTER, 4) in built.loads)
    assert not better.done()
    built.engines[HEAR_FLOOR].go.set()  # the long request ends, its model is unloaded
    await _until(lambda: HEAR_BETTER in built.engines)
    built.engines[HEAR_BETTER].go.set()

    assert ((await long).text, (await better).text) == ("long", "better")
    assert [load for load in built.loads if load[0] == HEAR_BETTER] == [
        (HEAR_BETTER, 4),
        (HEAR_BETTER, 4),
    ]
    assert {m.id: m.at_once for m in library.models()}[HEAR_BETTER] == 4  # nothing lowered


async def test_requests_queued_during_a_load_that_falls_back_run_only_as_many_as_it_fits(
    tmp_path: Path,
) -> None:
    built = Built()
    built.slow[HEAR_FLOOR] = threading.Event()
    built.too_big[HEAR_FLOOR] = 1
    library = _measured(tmp_path, {HEAR_FLOOR: 3}, built)
    hears = RealHears(library)
    words = [b"one", b"two", b"three"]
    tasks = [_hear(hears, word) for word in words]
    await _until(lambda: library.queued("hear", HEAR_FLOOR) == 3)

    built.slow[HEAR_FLOOR].set()
    await _until(lambda: HEAR_FLOOR in built.engines)
    engine = built.engines[HEAR_FLOOR]
    await _until(lambda: library.queued("hear", HEAR_FLOOR) == 2 and bool(engine.started))
    assert engine.started == [b"one"]  # built for one: one in, the others wait
    engine.go.set()
    await asyncio.gather(*tasks)

    assert (engine.at_once, engine.most, engine.started) == (1, 1, words)


@pytest.mark.parametrize(
    ("error", "steps"),
    [
        (RuntimeError("CUDA failed: out of memory"), True),
        (type("RuntimeException", (Exception,), {})("Failed to allocate memory"), True),
        (RuntimeError("unsupported model format"), False),
        (ValueError("out of memory"), False),
    ],
)
async def test_only_a_load_short_of_memory_is_tried_with_fewer(
    tmp_path: Path, error: Exception, steps: bool
) -> None:
    built = Built()
    built.too_big[HEAR_FLOOR] = 1
    built.error = error
    hears = RealHears(_measured(tmp_path, {HEAR_FLOOR: 3}, built))

    asked = _hear(hears, b"x")
    if steps:
        await _until(lambda: HEAR_FLOOR in built.engines)
        built.engines[HEAR_FLOOR].go.set()
        assert (await asked).text == "x"
        assert built.loads == [(HEAR_FLOOR, 3), (HEAR_FLOOR, 2), (HEAR_FLOOR, 1)]
    else:
        with pytest.raises(EngineUnavailable):
            await asked
        assert built.loads == [(HEAR_FLOOR, 3)]  # no load repeated for nothing


async def test_a_switch_with_requests_queued_for_both_models_serves_them_all_in_order(
    tmp_path: Path,
) -> None:
    built = Built()
    library = _measured(tmp_path, {}, built)
    hears = RealHears(library)
    a = [_hear(hears, word) for word in (b"a1", b"a2")]
    await _until(lambda: library.queued("hear", HEAR_FLOOR) == 1)
    b = [_hear(hears, word, HEAR_BETTER) for word in (b"b1", b"b2")]
    await _until(lambda: HEAR_BETTER in built.engines)
    await _until(lambda: library.queued("hear", HEAR_BETTER) == 1)
    built.engines[HEAR_BETTER].go.set()
    built.engines[HEAR_FLOOR].go.set()

    assert [t.text for t in await asyncio.gather(*a, *b)] == ["a1", "a2", "b1", "b2"]
    assert built.engines[HEAR_FLOOR].started == [b"a1", b"a2"]
    assert built.engines[HEAR_BETTER].started == [b"b1", b"b2"]
    await _until(lambda: built.engines[HEAR_FLOOR].closed)
    assert not built.engines[HEAR_BETTER].closed


class _Speaking(ProgramVoice):
    """A separate program voice that says one chunk, then waits for ``more`` before the rest;
    counts the runs that were closed."""

    def __init__(self) -> None:
        self.more = asyncio.Event()
        self.closed = 0

    async def say(self, text: str) -> AsyncIterator[bytes]:
        try:
            yield f"{text}:0".encode()
            await self.more.wait()
            yield f"{text}:1".encode()
        finally:
            self.closed += 1


async def test_a_client_that_goes_away_from_a_voice_stops_its_run_and_frees_its_place(
    tmp_path: Path,
) -> None:
    voice = _Speaking()

    def build_voice(entry: ModelEntryData, gpu: bool, at_once: int) -> Any:
        return voice

    says = RealSays(_measured(tmp_path, {VOICE_FLOOR: 1}, build_voice))
    gone = says.say("a", voice=None, language=None, model=VOICE_FLOOR)
    assert await anext(gone) == b"a:0"

    await gone.aclose()  # type: ignore[attr-defined]  # the client went away mid-answer

    assert voice.closed == 1
    after = says.say("b", voice=None, language=None, model=VOICE_FLOOR)
    assert await asyncio.wait_for(anext(after), HANG_GUARD) == b"b:0"
    voice.more.set()
    assert [chunk async for chunk in after] == [b"b:1"]


async def test_stopping_with_requests_queued_leaves_nothing_behind(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    built = Built()
    library = _measured(tmp_path, {HEAR_FLOOR: 1}, built)
    hears = RealHears(library)
    tasks = [_hear(hears, word) for word in (b"a", b"b", b"c")]
    await _until(lambda: library.queued("hear", HEAR_FLOOR) == 2)
    await _until(lambda: bool(built.engines[HEAR_FLOOR].started))

    for task in tasks:  # what the server does to its requests when it stops
        task.cancel()
    done = await asyncio.gather(*tasks, return_exceptions=True)
    built.engines[HEAR_FLOOR].go.set()  # the call in flight ends by itself
    await _until(lambda: built.engines[HEAR_FLOOR].running == 0)
    gc.collect()

    assert all(isinstance(result, asyncio.CancelledError) for result in done)
    assert library.queued("hear", HEAR_FLOOR) == 0
    assert built.engines[HEAR_FLOOR].started == [b"a"]
    assert "never retrieved" not in caplog.text


# Third review: waiting for room holds up no other use, and a measure round lowers nothing


async def test_a_model_waiting_for_room_holds_up_no_load_of_another_use(tmp_path: Path) -> None:
    built = Built()
    built.crowded.add(HEAR_BETTER)
    library = _measured(tmp_path, {}, built)
    hears, prints = RealHears(library), RealPrints(library)
    long = _hear(hears, b"long")  # read slowly: its model stays loaded
    await _until(lambda: HEAR_FLOOR in built.engines and built.engines[HEAR_FLOOR].running)
    better = _hear(hears, b"better", HEAR_BETTER)  # short of room beside it: waits
    await _until(lambda: (HEAR_BETTER, 1) in built.loads)

    printing = asyncio.create_task(prints.voice_print(b"p", model=PRINT_FLOOR))
    await _until(lambda: PRINT_FLOOR in built.engines)  # loaded while the hearing waits
    built.engines[PRINT_FLOOR].go.set()

    assert (await asyncio.wait_for(printing, HANG_GUARD)).model == PRINT_FLOOR
    assert not better.done() and not long.done()
    built.engines[HEAR_FLOOR].go.set()
    await _until(lambda: HEAR_BETTER in built.engines)
    built.engines[HEAR_BETTER].go.set()
    assert ((await long).text, (await better).text) == ("long", "better")


class _HeldTrier(Trier):
    """Holds the trials of the voice prints with several at once until ``release`` is set
    (``holding`` is set when one waits): a measure round still running."""

    def __init__(self, holds_at: dict[str, int]) -> None:
        super().__init__(holds_at)
        self.holding = threading.Event()
        self.release = threading.Event()

    def __call__(
        self, entry: ModelEntryData, *, gpu: bool, samples: tuple[Sample, ...], at_once: int = 1
    ) -> Tried:
        if entry.does == "voice_print" and at_once > 1:
            self.holding.set()
            self.release.wait(HANG_GUARD)
        return super().__call__(entry, gpu=gpu, samples=samples, at_once=at_once)


async def test_a_model_that_fits_fewer_while_a_measure_round_runs_keeps_its_count(
    tmp_path: Path,
) -> None:
    trier = _HeldTrier({HEAR_FLOOR: 3})
    built = Built()
    built.too_big[HEAR_FLOOR] = 1  # the round's trials take the memory for now
    library = Library(candidates(), tmp_path, CPU_ONLY, trier, built)
    library.start()
    try:
        await _until(trier.holding.is_set)
        assert {m.id: m.at_once for m in library.models()}[HEAR_FLOOR] == 3

        asked = _hear(RealHears(library), b"x")
        await _until(lambda: HEAR_FLOOR in built.engines)
        built.engines[HEAR_FLOOR].go.set()
        assert (await asked).text == "x"
        assert built.engines[HEAR_FLOOR].at_once == 1  # served with fewer for now
    finally:
        trier.release.set()
    await _until(lambda: library.health().get("measuring") is None)

    assert {m.id: m.at_once for m in library.models()}[HEAR_FLOOR] == 3  # nothing lowered


# Fourth review: no request dropped on a switch, the full number after a round, no engine left


async def test_requests_queued_for_a_model_before_its_load_are_served_after_a_switch(
    tmp_path: Path,
) -> None:
    built = Built()
    built.slow[PRINT_FLOOR] = threading.Event()
    library = _measured(tmp_path, {}, built)
    hears, prints = RealHears(library), RealPrints(library)
    printing = asyncio.create_task(prints.voice_print(b"p", model=PRINT_FLOOR))  # holds the turn
    await _until(lambda: (PRINT_FLOOR, 1) in built.loads)
    first = _hear(hears, b"a1")  # its model waits for its turn to load
    second = _hear(hears, b"b1", HEAR_BETTER)  # retires the first model, which has a request
    await _until(lambda: library.queued("hear", HEAR_BETTER) == 1)

    built.slow[PRINT_FLOOR].set()
    for model in (PRINT_FLOOR, HEAR_FLOOR, HEAR_BETTER):
        await _until(lambda model=model: model in built.engines)
        built.engines[model].go.set()

    assert ((await first).text, (await second).text) == ("a1", "b1")  # none dropped
    await printing
    await _until(lambda: built.engines[HEAR_FLOOR].closed)  # then let go


async def test_after_the_measure_round_a_model_lowered_for_it_serves_the_full_number(
    tmp_path: Path,
) -> None:
    trier = _HeldTrier({HEAR_FLOOR: 3})
    built = Built()
    built.too_big[HEAR_FLOOR] = 1
    library = Library(candidates(), tmp_path, CPU_ONLY, trier, built)
    hears = RealHears(library)
    library.start()
    try:
        await _until(trier.holding.is_set)
        during = _hear(hears, b"during")
        await _until(lambda: HEAR_FLOOR in built.engines)
        lowered = built.engines[HEAR_FLOOR]
        lowered.go.set()
        await during
        assert lowered.at_once == 1  # fewer for now
    finally:
        trier.release.set()
    await _until(lambda: library.health().get("measuring") is None)
    del built.too_big[HEAR_FLOOR]  # the round's trials no longer take the memory

    words = [b"a", b"b", b"c"]
    after = [_hear(hears, word) for word in words]
    await _until(lambda: built.engines[HEAR_FLOOR] is not lowered)
    full = built.engines[HEAR_FLOOR]
    await _until(lambda: len(full.started) == 3)  # three served side by side
    full.go.set()

    assert [t.text for t in await asyncio.gather(*after)] == ["a", "b", "c"]
    assert (full.at_once, full.most) == (3, 3)
    await _until(lambda: lowered.closed)


async def test_a_model_whose_requests_went_away_while_it_loaded_is_let_go(
    tmp_path: Path,
) -> None:
    built = Built()
    built.slow[HEAR_FLOOR] = threading.Event()
    library = _measured(tmp_path, {}, built)
    hears = RealHears(library)
    gone = _hear(hears, b"gone")
    await _until(lambda: (HEAR_FLOOR, 1) in built.loads)
    gone.cancel()  # its only request goes away while it loads
    await _until(lambda: library.queued("hear", HEAR_FLOOR) == 0)
    other = _hear(hears, b"other", HEAR_BETTER)  # asks for the other model of its use

    built.slow[HEAR_FLOOR].set()
    await _until(lambda: HEAR_FLOOR in built.engines)
    await _until(lambda: built.engines[HEAR_FLOOR].closed)  # loaded for nobody: let go
    assert built.engines[HEAR_FLOOR].closed_in != threading.get_ident()
    await _until(lambda: HEAR_BETTER in built.engines)
    built.engines[HEAR_BETTER].go.set()

    assert (await other).text == "other"
