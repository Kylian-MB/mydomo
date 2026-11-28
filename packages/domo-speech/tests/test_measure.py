"""The service measures its candidates where it runs and lists only those that hold."""

import asyncio
import dataclasses
import json
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest
from domo_speech.app import create_app
from domo_speech.data import ModelEntryData, candidates
from domo_speech.engines import Engines, EngineUnavailable, UnknownModel
from domo_speech.fakes import FakeHears, FakePrints, FakeSays
from domo_speech.measure import (
    MEASURE_FILE,
    TRIAL_SENTENCES,
    Machine,
    NoProgram,
    Sample,
    Tried,
    Unfetched,
    current,
    heard_back,
    measure,
    warmed_median,
)
from domo_speech.real import REMEASURES, Library

CPU_ONLY = Machine(cpu="x86", cores=4, ram_mb=8192, gpus=(), device="auto")
WITH_GPU = dataclasses.replace(CPU_ONLY, gpus=("cuda:0",))


class FakeTrier:
    """Says what each candidate does, by id: ``speed`` in seconds of work per second of sound,
    ``fails`` (an exception to raise when tried), ``unfetched`` (weights that cannot be had),
    ``deaf`` (a hearing that hears nothing of what the voices said), ``deaf_to`` (a voice no
    hearing understands), ``hang`` (tried until ``release`` is set), ``card_fails`` (an exception
    raised only on the graphics card), ``card_speed`` (its speed on the card only). Every candidate
    is too slow with two requests at once; those tries are recorded in ``together``, with whether
    they ran on the card (``together_on_card``)."""

    def __init__(self) -> None:
        self.speed: dict[str, float] = {}
        self.fails: dict[str, Exception] = {}
        self.unfetched: set[str] = set()
        self.deaf: set[str] = set()
        self.deaf_to: set[str] = set()
        self.hang: set[str] = set()
        self.release = threading.Event()
        self.tried: list[tuple[str, bool, tuple[Sample, ...]]] = []
        self.fetched: list[str] = []
        self.together: list[tuple[str, int]] = []
        self.together_on_card: list[bool] = []
        self.card_fails: dict[str, Exception] = {}
        self.card_speed: dict[str, float] = {}

    def fetch(self, entry: ModelEntryData) -> None:
        self.fetched.append(entry.id)
        if entry.id in self.unfetched:
            raise Unfetched

    def __call__(
        self, entry: ModelEntryData, *, gpu: bool, samples: tuple[Sample, ...], at_once: int = 1
    ) -> Tried:
        if at_once > 1:
            self.together.append((entry.id, at_once))
            self.together_on_card.append(gpu)
            return Tried("cpu", 1.5, 600.0, work_seconds=40.0, seconds_of_sound=4.0)
        self.tried.append((entry.id, gpu, samples))
        if entry.id in self.hang:
            self.release.wait(30)
        if entry.id in self.fails:
            raise self.fails[entry.id]
        if gpu and entry.id in self.card_fails:
            raise self.card_fails[entry.id]
        made = ()
        if entry.does == "say":
            made = tuple(
                Sample(entry.id, language, text, b"sound:" + language.encode())
                for language, text in TRIAL_SENTENCES.items()
            )
        heard = {
            s.key: "" if entry.id in self.deaf or s.voice in self.deaf_to else s.text
            for s in samples
        }
        return Tried(
            device="gpu" if gpu else "cpu",
            load_seconds=1.5,
            memory_mb=300.0,
            work_seconds=(
                self.card_speed[entry.id]
                if gpu and entry.id in self.card_speed
                else self.speed.get(entry.id, 0.1)
            )
            * 4,
            seconds_of_sound=4.0,
            samples=made,
            heard=heard,
        )


def _ids(does: str, **flags: object) -> list[str]:
    return [
        e.id
        for e in candidates()
        if e.does == does and all(getattr(e, k) == v for k, v in flags.items())
    ]


HEAR_FLOOR = _ids("hear", floor=True, runs="imported")[0]
HEAR_BETTER = _ids("hear", floor=False, needs="cpu")[0]  # runs on a processor, not the floor
HEAR_GPU = _ids("hear", needs="gpu")[0]
VOICE_FLOOR = _ids("say", floor=True, runs="imported")[0]
PRINT_FLOOR = _ids("voice_print", floor=True)[0]


def test_without_a_graphics_card_only_what_a_processor_runs_is_tried() -> None:
    trier = FakeTrier()

    measured = measure(candidates(), CPU_ONLY, trier)

    tried = {entry_id for entry_id, _, _ in trier.tried}
    gpu_only = {e.id for e in candidates() if e.needs == "gpu"}
    assert not tried & gpu_only
    assert {t.id: t.why for t in measured.trials if t.id in gpu_only} == dict.fromkeys(
        gpu_only, "needs_gpu"
    )
    assert all(not gpu for _, gpu, _ in trier.tried)


def test_with_a_graphics_card_the_better_ones_are_tried_after_the_floor() -> None:
    trier = FakeTrier()

    measured = measure(candidates(), WITH_GPU, trier)

    order = [entry_id for entry_id, _, _ in trier.tried]
    for use in ("hear", "say", "voice_print"):
        floor = [order.index(i) for i in _ids(use, floor=True) if i in order]
        better = [order.index(i) for i in _ids(use, floor=False) if i in order]
        if floor and better:
            assert max(floor) < min(better), use
    assert {t.id for t in measured.held()} >= {e.id for e in candidates() if e.needs == "gpu"}


def test_the_setting_cpu_keeps_off_the_graphics_card() -> None:
    trier = FakeTrier()

    measure(candidates(), dataclasses.replace(WITH_GPU, device="cpu"), trier)

    assert all(not gpu for _, gpu, _ in trier.tried)


def test_hearing_is_tried_on_what_a_voice_said_and_must_hear_it_back() -> None:
    trier = FakeTrier()
    deaf, good = _ids("hear", needs="cpu")[:2]
    trier.deaf.add(deaf)

    measured = measure(candidates(), CPU_ONLY, trier)

    samples = next(s for entry_id, _, s in trier.tried if entry_id == good)
    assert {s.language for s in samples} == {"fr", "en"}
    by_id = {t.id: t for t in measured.trials}
    assert (by_id[deaf].holds, by_id[deaf].why) == (False, "heard_wrong")
    assert by_id[good].holds and by_id[good].heard_back == 1.0


def test_a_failing_candidate_does_not_hold_and_a_slow_one_is_kept_only_as_slow() -> None:
    trier = FakeTrier()
    slow, broken = _ids("voice_print", needs="cpu")[:2]
    trier.speed[slow] = 0.9
    trier.fails[broken] = RuntimeError("out of memory")

    measured = measure(candidates(), CPU_ONLY, trier)

    by_id = {t.id: t for t in measured.trials}
    # Nothing else of its use holds: the slow one is kept, marked slow.
    assert (by_id[slow].holds, by_id[slow].why, by_id[slow].real_time_factor) == (
        True,
        "slow",
        0.9,
    )
    assert (by_id[broken].holds, by_id[broken].why) == (False, "failed")
    assert "voice_print" not in measured.missing()
    assert measured.slow() == ("voice_print",)


def test_a_candidate_that_fails_on_the_graphics_card_is_measured_on_the_processor() -> None:
    # Seen in the trials: every hearing failed on the card (its libraries missing), and the
    # service had no hearing at all although the processor held one.
    trier = FakeTrier()
    for entry_id in _ids("hear", needs="cpu"):
        trier.card_fails[entry_id] = RuntimeError("libcublas not found")

    measured = measure(candidates(), WITH_GPU, trier)

    tried = [(entry_id, gpu) for entry_id, gpu, _ in trier.tried if entry_id == HEAR_FLOOR]
    assert tried == [(HEAR_FLOOR, True), (HEAR_FLOOR, False)]
    by_id = {t.id: t for t in measured.trials}
    assert (by_id[HEAR_FLOOR].holds, by_id[HEAR_FLOOR].device) == (True, "cpu")
    assert "hear" not in measured.missing()
    # Served as measured: tried with more at once on the processor too.
    assert [
        on_card
        for (entry_id, _), on_card in zip(trier.together, trier.together_on_card, strict=True)
        if entry_id == HEAR_FLOOR
    ] == [False]


def test_a_candidate_too_slow_on_the_graphics_card_is_measured_on_the_processor() -> None:
    trier = FakeTrier()
    trier.card_speed[PRINT_FLOOR] = 0.9

    measured = measure(candidates(), WITH_GPU, trier)

    by_id = {t.id: t for t in measured.trials}
    assert (by_id[PRINT_FLOOR].holds, by_id[PRINT_FLOOR].device) == (True, "cpu")
    assert [gpu for entry_id, gpu, _ in trier.tried if entry_id == PRINT_FLOOR] == [True, False]


def test_what_fails_on_both_or_needs_the_card_is_tried_once_more_at_most() -> None:
    trier = FakeTrier()
    trier.fails[PRINT_FLOOR] = RuntimeError("broken")
    trier.card_fails[HEAR_GPU] = RuntimeError("out of memory")

    measured = measure(candidates(), WITH_GPU, trier)

    by_id = {t.id: t for t in measured.trials}
    assert (by_id[PRINT_FLOOR].holds, by_id[PRINT_FLOOR].why) == (False, "failed")
    assert [gpu for entry_id, gpu, _ in trier.tried if entry_id == PRINT_FLOOR] == [True, False]
    # A candidate that needs the card is never tried on the processor.
    assert [gpu for entry_id, gpu, _ in trier.tried if entry_id == HEAR_GPU] == [True]
    assert (by_id[HEAR_GPU].holds, by_id[HEAR_GPU].why) == (False, "failed")


def test_what_holds_on_the_graphics_card_is_not_tried_again() -> None:
    trier = FakeTrier()

    measure(candidates(), WITH_GPU, trier)

    assert all(gpu for _, gpu, _ in trier.tried)


def test_a_program_voice_without_its_program_is_left_out() -> None:
    trier = FakeTrier()
    programs = [e.id for e in candidates() if e.runs == "separate_program"]
    for entry_id in programs:
        trier.fails[entry_id] = NoProgram()

    measured = measure(candidates(), CPU_ONLY, trier)

    assert {t.why for t in measured.trials if t.id in programs} == {"no_program"}
    assert measured.complete


def test_heard_back_counts_the_words_said() -> None:
    assert heard_back("Allume la lumière.", "allume la lumière") == 1.0
    assert heard_back("Allume la lumière.", "allume la") == pytest.approx(2 / 3)
    assert heard_back("", "anything") == 0.0


# The service around the measure


def _library(tmp_path: Path, trier: FakeTrier, machine: Machine = CPU_ONLY) -> Library:
    def builder(entry: ModelEntryData, gpu: bool, at_once: int) -> Any:
        raise AssertionError("no engine is built in these tests")

    return Library(candidates(), tmp_path, machine, trier, builder)


def _app(library: Library) -> httpx.AsyncClient:
    engines = Engines(
        hears=FakeHears(),
        says=FakeSays(),
        prints=FakePrints(),
        models=library.models,
        ready=library.ready,
        health=library.health,
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(engines)), base_url="http://s"
    )


async def test_health_is_preparing_while_the_measure_runs_then_models_list_what_holds(
    tmp_path: Path,
) -> None:
    trier = FakeTrier()
    trier.hang.add(VOICE_FLOOR)
    trier.speed[HEAR_BETTER] = 0.9
    library = _library(tmp_path, trier)
    library.start()
    async with _app(library) as client:
        waiting = await client.get("/health")
        listed_early = (await client.get("/v1/models")).json()["data"]

        trier.release.set()
        for _ in range(200):
            if library.ready() != "preparing":
                break
            threading.Event().wait(0.01)
        ready = await client.get("/health")
        listed = (await client.get("/v1/models")).json()["data"]

    assert (waiting.status_code, waiting.json()) == (503, {"status": "preparing"})
    assert listed_early == []
    assert ready.status_code == 200 and ready.json()["status"] == "ready"
    ids = {m["id"] for m in listed}
    assert HEAR_BETTER not in ids
    assert not ids & {e.id for e in candidates() if e.needs == "gpu"}
    assert HEAR_FLOOR in ids
    entry = next(m for m in listed if m["id"] == HEAR_FLOOR)
    assert entry["does"] == "hear"
    assert entry["measures"] == {
        "device": "cpu",
        "load_seconds": 1.5,
        "memory_mb": 300.0,
        "real_time_factor": 0.1,
        "heard_back": 1.0,
    }


async def test_when_not_even_the_floor_holds_health_says_it_plainly(tmp_path: Path) -> None:
    trier = FakeTrier()
    for entry in candidates():
        trier.fails[entry.id] = RuntimeError("no")
    library = Library(candidates(), tmp_path, CPU_ONLY, trier, _never_built, sleep=_Sleeps(1))
    library.run_measure()

    async with _app(library) as client:
        health = await client.get("/health")
        listed = (await client.get("/v1/models")).json()["data"]

    assert health.status_code == 503
    assert health.json() == {
        "status": "failed",
        "missing": ["hear", "say", "voice_print"],
        "reason": "nothing_holds",
        "retrying": True,
    }
    assert listed == []


async def test_a_use_with_nothing_that_holds_is_named(tmp_path: Path) -> None:
    trier = FakeTrier()
    for entry_id in _ids("say"):
        trier.fails[entry_id] = RuntimeError("no")
    library = Library(candidates(), tmp_path, CPU_ONLY, trier, _never_built, sleep=_Sleeps(1))
    library.run_measure()

    async with _app(library) as client:
        health = await client.get("/health")

    assert (health.status_code, health.json()) == (
        200,
        {"status": "ready", "missing": ["say"], "retrying": True},
    )


def test_the_measure_is_written_in_the_model_directory_and_kept(tmp_path: Path) -> None:
    trier = FakeTrier()
    library = _library(tmp_path, trier)
    library.run_measure()

    written = json.loads((tmp_path / MEASURE_FILE).read_text(encoding="utf-8"))
    assert written["machine"]["cores"] == 4
    assert {t["id"] for t in written["trials"]} == {e.id for e in candidates()}

    again = FakeTrier()
    restarted = _library(tmp_path, again)
    restarted.start()
    assert restarted.ready() == "ready"
    assert again.tried == []


@pytest.mark.parametrize(
    "changed",
    [
        dataclasses.replace(CPU_ONLY, gpus=("cuda:0",)),
        dataclasses.replace(CPU_ONLY, ram_mb=32768),
        dataclasses.replace(CPU_ONLY, cpu="another"),
        dataclasses.replace(CPU_ONLY, device="cpu"),
    ],
)
def test_a_changed_machine_is_measured_again(tmp_path: Path, changed: Machine) -> None:
    _library(tmp_path, FakeTrier()).run_measure()

    assert current(tmp_path, candidates(), CPU_ONLY) is not None
    assert current(tmp_path, candidates(), changed) is None
    assert current(tmp_path, candidates()[1:], CPU_ONLY) is None


def _never_built(entry: ModelEntryData, gpu: bool, at_once: int) -> Any:
    raise AssertionError("no engine is built in this test")


class _Sleeps:
    """Stands for the wait between two tries: records it, and stops after ``rounds``."""

    def __init__(self, rounds: int) -> None:
        self.waits: list[float] = []
        self.rounds = rounds

    def __call__(self, seconds: float) -> bool:
        self.waits.append(seconds)
        return len(self.waits) >= self.rounds


def test_weights_never_fetched_are_saved_and_only_they_are_tried_again(tmp_path: Path) -> None:
    trier = FakeTrier()
    trier.unfetched.add(PRINT_FLOOR)
    sleeps = _Sleeps(rounds=3)
    library = Library(
        candidates(), tmp_path, CPU_ONLY, trier, _never_built, retry_seconds=10, sleep=sleeps
    )

    library.run_measure()

    # Saved after the first round, with the line that could not be fetched.
    saved = json.loads((tmp_path / MEASURE_FILE).read_text(encoding="utf-8"))
    assert {t["id"]: t["why"] for t in saved["trials"]}[PRINT_FLOOR] == "not_fetched"
    # Each later round fetched that one alone, waiting longer each time.
    first_round = len([e for e in candidates() if e.needs == "cpu"])
    assert trier.fetched[first_round:] == [PRINT_FLOOR] * 2
    assert sleeps.waits == [10, 20, 40]
    assert library.ready() == "ready"
    assert library.health()["retrying"] is True


def test_a_restart_keeps_what_held_and_retries_only_what_was_not_fetched(tmp_path: Path) -> None:
    first = FakeTrier()
    first.unfetched.add(PRINT_FLOOR)
    Library(candidates(), tmp_path, CPU_ONLY, first, _never_built, sleep=_Sleeps(1)).run_measure()

    again = FakeTrier()
    restarted = Library(candidates(), tmp_path, CPU_ONLY, again, _never_built, sleep=_Sleeps(1))
    assert restarted.ready() == "ready"  # what held is listed at once
    restarted.run_measure()

    assert [entry_id for entry_id, _, _ in again.tried] == [PRINT_FLOOR]
    assert PRINT_FLOOR in {m.id for m in restarted.models()}


def test_a_use_served_slowly_is_measured_again_at_the_next_start(tmp_path: Path) -> None:
    """A busy machine at the first start must not leave the service slow for good."""
    first = FakeTrier()
    for hearing in _ids("hear", needs="cpu"):
        first.speed[hearing] = 5.0
    Library(candidates(), tmp_path, CPU_ONLY, first, _never_built, sleep=_Sleeps(1)).run_measure()

    again = FakeTrier()
    restarted = Library(candidates(), tmp_path, CPU_ONLY, again, _never_built, sleep=_Sleeps(1))
    assert restarted.health()["slow"] == ["hear"]
    restarted.run_measure()

    tried = [entry_id for entry_id, _, _ in again.tried]
    assert VOICE_FLOOR in tried and HEAR_FLOOR in tried  # a voice for the hearing to hear
    assert "slow" not in restarted.health() and "missing" not in restarted.health()


def test_a_trial_is_timed_after_a_first_call_that_is_not_counted() -> None:
    """The first call of a freshly loaded model is slow (caches, kernels): it never decides."""
    times = iter([9.0, 0.3, 0.2, 0.4])
    calls: list[float] = []

    def run() -> float:
        calls.append(next(times))
        return calls[-1]

    assert warmed_median(run, work=lambda done: done) == 0.3
    assert len(calls) == 4


def test_a_hearing_tried_again_gets_a_voice_to_hear(tmp_path: Path) -> None:
    trier = FakeTrier()
    trier.unfetched.add(HEAR_FLOOR)
    library = Library(candidates(), tmp_path, CPU_ONLY, trier, _never_built, sleep=_Sleeps(1))
    library.run_measure()
    trier.unfetched.clear()
    trier.tried.clear()

    Library(candidates(), tmp_path, CPU_ONLY, trier, _never_built, sleep=_Sleeps(1)).run_measure()

    tried = [entry_id for entry_id, _, _ in trier.tried]
    assert tried == [VOICE_FLOOR, HEAR_FLOOR]
    samples = trier.tried[1][2]
    assert {s.voice for s in samples} == {VOICE_FLOOR}


async def test_without_network_the_first_start_says_failed_and_retrying(tmp_path: Path) -> None:
    trier = FakeTrier()
    trier.unfetched.update(e.id for e in candidates())
    library = Library(candidates(), tmp_path, CPU_ONLY, trier, _never_built, sleep=_Sleeps(1))
    library.run_measure()

    async with _app(library) as client:
        health = await client.get("/health")
        heard = await client.post(
            "/v1/audio/transcriptions", files={"file": ("a.pcm", b"x", "audio/pcm")}
        )

    assert health.status_code == 503
    assert health.json()["status"] == "failed"
    assert health.json()["reason"] == "weights_unreachable"
    assert health.json()["retrying"] is True
    assert heard.json() == {"error": "failed", "reason": "weights_unreachable"}


def test_a_trial_that_hangs_is_given_up_and_the_others_go_on() -> None:
    trier = FakeTrier()
    trier.hang.add(HEAR_FLOOR)
    try:
        measured = measure(candidates(), CPU_ONLY, trier, trial_seconds=0.2)
    finally:
        trier.release.set()

    by_id = {t.id: t for t in measured.trials}
    assert (by_id[HEAR_FLOOR].holds, by_id[HEAR_FLOOR].why) == (False, "failed")
    assert by_id[HEAR_BETTER].holds and by_id[PRINT_FLOOR].holds


async def test_what_is_measured_is_listed_while_the_rest_is_measured(tmp_path: Path) -> None:
    trier = FakeTrier()
    trier.hang.update(_ids("voice_print"))
    library = Library(candidates(), tmp_path, CPU_ONLY, trier, _never_built, sleep=_Sleeps(1))
    library.start()
    try:
        for _ in range(500):
            if library.models():
                break
            threading.Event().wait(0.01)
        async with _app(library) as client:
            health = await client.get("/health")
        listed = {m.does for m in library.models()}
    finally:
        trier.release.set()

    assert listed == {"say", "hear"}
    assert health.status_code == 200
    assert health.json()["measuring"] is True


def test_a_voice_holds_only_if_some_hearing_hears_it_back() -> None:
    trier = FakeTrier()
    voices = _ids("say", runs="imported")
    trier.deaf_to.add(voices[0])
    measured = measure(candidates(), CPU_ONLY, trier)

    by_id = {t.id: t for t in measured.trials}
    assert (by_id[voices[0]].holds, by_id[voices[0]].why) == (False, "heard_wrong")
    assert by_id[voices[0]].heard_back == 0.0


def test_a_voice_program_set_later_is_measured_again(tmp_path: Path) -> None:
    trier = FakeTrier()
    for entry in candidates():
        if entry.runs == "separate_program":
            trier.fails[entry.id] = NoProgram()
    Library(candidates(), tmp_path, CPU_ONLY, trier, _never_built).run_measure()

    with_program = dataclasses.replace(CPU_ONLY, voice_program="/opt/voice")
    assert current(tmp_path, candidates(), CPU_ONLY) is not None
    assert current(tmp_path, candidates(), with_program) is None


class _Loaded:
    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id
        self.closed = False

    def close(self) -> None:
        self.closed = True


async def test_only_a_model_that_holds_is_loaded_and_one_per_use(tmp_path: Path) -> None:
    built: list[_Loaded] = []

    def builder(entry: ModelEntryData, gpu: bool, at_once: int) -> _Loaded:
        built.append(_Loaded(entry.id))
        return built[-1]

    async def engine(does: Any, model: str | None) -> Any:
        async with library.serving(does, model) as slot:  # one request, served
            return await library.engine(slot)

    trier = FakeTrier()
    trier.speed[HEAR_BETTER] = 0.3
    library = Library(candidates(), tmp_path, CPU_ONLY, trier, builder)  # type: ignore[arg-type]
    library.run_measure()

    with pytest.raises(UnknownModel):
        await engine("hear", HEAR_GPU)  # needs a graphics card
    with pytest.raises(UnknownModel):
        await engine("hear", None)  # the caller chooses; the service never does
    with pytest.raises(UnknownModel):
        await engine("say", HEAR_FLOOR)  # not a voice

    base = await engine("hear", HEAR_FLOOR)
    assert await engine("hear", HEAR_FLOOR) is base
    small = await engine("hear", HEAR_BETTER)
    for _ in range(1000):  # unloaded in a thread
        if base.closed:
            break
        await asyncio.sleep(0.005)
    assert base.closed and not small.closed
    assert [b.entry_id for b in built] == [HEAR_FLOOR, HEAR_BETTER]


async def test_a_model_that_cannot_be_loaded_is_unavailable(tmp_path: Path) -> None:
    def builder(entry: ModelEntryData, gpu: bool, at_once: int) -> _Loaded:
        raise MemoryError

    library = Library(candidates(), tmp_path, CPU_ONLY, FakeTrier(), builder)  # type: ignore[arg-type]
    library.run_measure()

    with pytest.raises(EngineUnavailable):
        async with library.serving("hear", HEAR_FLOOR) as slot:
            await library.engine(slot)


async def test_a_measure_that_breaks_says_failed_not_preparing(tmp_path: Path) -> None:
    class Broken(FakeTrier):
        def __call__(
            self,
            entry: ModelEntryData,
            *,
            gpu: bool,
            samples: tuple[Sample, ...],
            at_once: int = 1,
        ) -> Tried:
            return None  # type: ignore[return-value]  # a trier with a bug

    broken = Broken()
    library = Library(candidates(), tmp_path, CPU_ONLY, broken, _never_built)
    library.run_measure()

    async with _app(library) as client:
        health = await client.get("/health")

    assert (health.status_code, health.json()) == (
        503,
        {"status": "failed", "reason": "measure_failed"},
    )


def test_weights_lost_between_fetching_and_trying_are_not_fetched_not_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Weights that cannot be had when the trial loads them are tried again later
    (``not_fetched``), never kept as a model that failed."""
    trial = pytest.importorskip("domo_speech.real.trial")
    weights = pytest.importorskip("domo_speech.real.weights")

    def gone(entry: ModelEntryData, model_dir: Path) -> Any:
        raise weights.WeightsError("gone")

    monkeypatch.setattr(trial, "ensure_weights", gone)
    entry = next(e for e in candidates() if e.id == PRINT_FLOOR)
    with pytest.raises(Unfetched):
        trial.RealTrier(tmp_path, None)(entry, gpu=False, samples=())


# A use is never left empty for speed alone


def _all_hearings(trier: FakeTrier, speed: float) -> None:
    for hearing in _ids("hear", needs="cpu"):
        trier.speed[hearing] = speed


def test_when_no_hearing_is_fast_enough_the_fastest_that_hears_right_is_kept_as_slow() -> None:
    """A processor at its limit hears slowly rather than not at all."""
    trier = FakeTrier()
    trier.speed[HEAR_FLOOR] = 0.6
    trier.speed[HEAR_BETTER] = 0.9

    measured = measure(candidates(), CPU_ONLY, trier)

    by_id = {t.id: t for t in measured.trials}
    assert (by_id[HEAR_FLOOR].holds, by_id[HEAR_FLOOR].why) == (True, "slow")
    assert by_id[HEAR_FLOOR].real_time_factor == 0.6
    assert (by_id[HEAR_BETTER].holds, by_id[HEAR_BETTER].why) == (False, "too_slow")
    assert "hear" not in measured.missing()
    assert measured.slow() == ("hear",)
    # Voices are judged by the hearing kept: they are heard back.
    assert by_id[VOICE_FLOOR].holds and by_id[VOICE_FLOOR].heard_back == 1.0
    # A slow model is never tried with more at once (it does not hold even one).
    assert HEAR_FLOOR not in {entry_id for entry_id, _ in trier.together}


def test_a_slow_hearing_that_hears_wrong_or_fails_is_never_kept() -> None:
    trier = FakeTrier()
    trier.speed[HEAR_FLOOR] = 0.6
    trier.deaf.add(HEAR_FLOOR)
    trier.fails[HEAR_BETTER] = RuntimeError("no")

    measured = measure(candidates(), CPU_ONLY, trier)

    assert "hear" in measured.missing()
    assert measured.slow() == ()
    assert not any(t.holds for t in measured.trials if t.does == "hear")


def test_a_slow_voice_is_kept_only_if_it_is_heard_back() -> None:
    trier = FakeTrier()
    voices = _ids("say", needs="cpu")
    for index, voice in enumerate(voices):
        trier.speed[voice] = 0.6 + index / 10
    trier.deaf_to.add(voices[0])  # the fastest is not understood

    measured = measure(candidates(), CPU_ONLY, trier)

    by_id = {t.id: t for t in measured.trials}
    assert by_id[voices[0]].why == "too_slow" and not by_id[voices[0]].holds
    assert (by_id[voices[1]].holds, by_id[voices[1]].why) == (True, "slow")
    assert by_id[voices[1]].heard_back == 1.0
    assert measured.slow() == ("say",)


async def test_a_slow_use_is_listed_and_health_says_it_is_slow(tmp_path: Path) -> None:
    trier = FakeTrier()
    _all_hearings(trier, 0.6)
    library = Library(candidates(), tmp_path, CPU_ONLY, trier, _never_built, sleep=_Sleeps(1))
    library.run_measure()

    async with _app(library) as client:
        health = await client.get("/health")
        listed = (await client.get("/v1/models")).json()["data"]

    assert health.status_code == 200
    assert health.json() == {"status": "ready", "slow": ["hear"], "retrying": True}
    hearing = [m for m in listed if m["does"] == "hear"]
    assert [(m["id"], m["slow"]) for m in hearing] == [(HEAR_FLOOR, True)]
    assert all("slow" not in m for m in listed if m["does"] != "hear")


class _Then:
    """The wait between two rounds: calls ``change`` at the first wait, stops after ``rounds``."""

    def __init__(self, change: Any, rounds: int = 10) -> None:
        self.change = change
        self.waits: list[float] = []
        self.rounds = rounds

    def __call__(self, seconds: float) -> bool:
        self.waits.append(seconds)
        if len(self.waits) == 1:
            self.change()
        return len(self.waits) >= self.rounds


def test_a_slow_use_is_measured_again_in_the_same_start_and_a_fast_one_replaces_it(
    tmp_path: Path,
) -> None:
    trier = FakeTrier()
    _all_hearings(trier, 0.6)
    sleeps = _Then(lambda: _all_hearings(trier, 0.1))
    library = Library(
        candidates(), tmp_path, CPU_ONLY, trier, _never_built, retry_seconds=10, sleep=sleeps
    )

    library.run_measure()

    assert sleeps.waits == [10]  # once it holds, no more rounds
    hearing = [m for m in library.models() if m.does == "hear"]
    assert {m.id for m in hearing} == {HEAR_FLOOR, HEAR_BETTER}
    assert not any(m.slow for m in library.models())
    assert library.health() == {}
    assert library.ready() == "ready"


def test_a_hearing_whose_trials_all_failed_is_measured_again_in_the_same_start(
    tmp_path: Path,
) -> None:
    """A trial given up at its time limit on a busy start is not final."""
    trier = FakeTrier()
    for hearing in _ids("hear", needs="cpu"):
        trier.fails[hearing] = RuntimeError("timed out")
    sleeps = _Then(trier.fails.clear)
    library = Library(
        candidates(), tmp_path, CPU_ONLY, trier, _never_built, retry_seconds=10, sleep=sleeps
    )
    library.run_measure()

    assert sleeps.waits == [10]
    assert HEAR_FLOOR in {m.id for m in library.models()}
    assert "missing" not in library.health()


def test_a_hearing_that_heard_wrong_is_not_kept_but_tried_again(tmp_path: Path) -> None:
    trier = FakeTrier()
    trier.deaf.update(_ids("hear", needs="cpu"))
    seen: list[object] = []

    def change() -> None:
        seen.append(library.health())
        trier.deaf.clear()

    sleeps = _Then(change)
    library = Library(
        candidates(), tmp_path, CPU_ONLY, trier, _never_built, retry_seconds=10, sleep=sleeps
    )
    library.run_measure()

    assert seen == [{"missing": ["hear"], "retrying": True}]
    assert sleeps.waits == [10]
    assert HEAR_FLOOR in {m.id for m in library.models()}


def test_measuring_again_in_one_start_is_bounded_and_waits_longer_each_time(
    tmp_path: Path,
) -> None:
    trier = FakeTrier()
    for hearing in _ids("hear", needs="cpu"):
        trier.fails[hearing] = RuntimeError("no")
    sleeps = _Sleeps(rounds=100)
    library = Library(
        candidates(),
        tmp_path,
        CPU_ONLY,
        trier,
        _never_built,
        retry_seconds=10,
        max_retry_seconds=30,
        sleep=sleeps,
    )

    library.run_measure()

    assert sleeps.waits == [10, 20, 30, 30, 30][:REMEASURES]
    assert len(sleeps.waits) == REMEASURES
    assert library.health() == {"missing": ["hear"]}  # no more rounds in this start


def test_a_saved_measure_with_a_failed_hearing_is_measured_again_at_the_next_start(
    tmp_path: Path,
) -> None:
    first = FakeTrier()
    for hearing in _ids("hear", needs="cpu"):
        first.fails[hearing] = RuntimeError("no")
    Library(candidates(), tmp_path, CPU_ONLY, first, _never_built, sleep=_Sleeps(1)).run_measure()

    again = FakeTrier()
    restarted = Library(candidates(), tmp_path, CPU_ONLY, again, _never_built, sleep=_Sleeps(1))
    assert restarted.health()["retrying"] is True
    restarted.run_measure()

    assert HEAR_FLOOR in {m.id for m in restarted.models()}


# A re-measure never takes away what the house has


@pytest.mark.parametrize("then", ["fails", "hears_wrong"])
def test_a_hearing_kept_slow_stays_when_its_re_trial_does_not_hold(then: str) -> None:
    """A machine busy at the re-measure must not leave the house without the hearing it had."""
    trier = FakeTrier()
    _all_hearings(trier, 0.6)
    before = measure(candidates(), CPU_ONLY, trier)
    for hearing in _ids("hear", needs="cpu"):
        if then == "fails":
            trier.fails[hearing] = RuntimeError("timed out")
        else:
            trier.deaf.add(hearing)
    trier.tried.clear()

    after = measure(candidates(), CPU_ONLY, trier, previous=before)

    assert HEAR_FLOOR in {entry_id for entry_id, _, _ in trier.tried}  # it was tried again
    kept = {t.id: t for t in before.trials}[HEAR_FLOOR]
    assert {t.id: t for t in after.trials}[HEAR_FLOOR] == kept
    assert after.slow() == ("hear",) and "hear" not in after.missing()
    # The voices are not judged by what a hearing that did not hold heard.
    assert {t.id: t for t in after.trials}[VOICE_FLOOR].holds


def test_a_better_re_trial_still_replaces_a_slow_one() -> None:
    trier = FakeTrier()
    _all_hearings(trier, 0.9)
    before = measure(candidates(), CPU_ONLY, trier)
    trier.speed[HEAR_FLOOR] = 0.7

    after = measure(candidates(), CPU_ONLY, trier, previous=before)

    floor = {t.id: t for t in after.trials}[HEAR_FLOOR]
    assert (floor.why, floor.real_time_factor) == ("slow", 0.7)


def test_a_helper_tried_again_never_loses_what_it_held() -> None:
    """A slow voice measured again re-runs a fast hearing to hear it: a hearing that fails under
    load then still holds as it did."""
    trier = FakeTrier()
    for voice in _ids("say", needs="cpu"):
        trier.speed[voice] = 0.6
    before = measure(candidates(), CPU_ONLY, trier)
    assert before.slow() == ("say",)
    trier.fails[HEAR_FLOOR] = RuntimeError("timed out")
    trier.together.clear()

    after = measure(candidates(), CPU_ONLY, trier, previous=before)

    assert HEAR_FLOOR in {entry_id for entry_id, _, _ in trier.tried[-3:]}
    assert {t.id: t for t in after.trials}[HEAR_FLOOR] == {t.id: t for t in before.trials}[
        HEAR_FLOOR
    ]
    assert HEAR_FLOOR not in {entry_id for entry_id, _ in trier.together}


def test_measuring_again_is_bounded_also_while_weights_are_not_fetched(tmp_path: Path) -> None:
    trier = FakeTrier()
    trier.unfetched.add(PRINT_FLOOR)
    for hearing in _ids("hear", needs="cpu"):
        trier.fails[hearing] = RuntimeError("no")
    library = Library(
        candidates(), tmp_path, CPU_ONLY, trier, _never_built, retry_seconds=10, sleep=_Sleeps(9)
    )

    library.run_measure()

    tries = [entry_id for entry_id, _, _ in trier.tried if entry_id == HEAR_FLOOR]
    assert len(tries) == 1 + REMEASURES
    assert trier.fetched.count(PRINT_FLOOR) == 9  # the weights are still tried every round


def test_a_hearing_hears_a_voice_too_slow_only_when_no_voice_holds() -> None:
    trier = FakeTrier()
    voices = _ids("say", needs="cpu")
    trier.speed[voices[1]] = 0.6
    trier.deaf_to.add(voices[1])  # its sound is poor: no hearing understands it

    measured = measure(candidates(), CPU_ONLY, trier)

    samples = next(s for entry_id, _, s in trier.tried if entry_id == HEAR_FLOOR)
    assert voices[1] not in {s.voice for s in samples}
    assert {t.id: t for t in measured.trials}[HEAR_FLOOR].why == "holds"


async def test_a_round_to_measure_again_waits_while_requests_are_served(tmp_path: Path) -> None:
    """A re-measure loads a second copy of a model beside the one that serves: not while it
    serves."""
    trier = FakeTrier()
    _all_hearings(trier, 0.6)
    waits: list[float] = []
    asleep = threading.Event()
    wake = threading.Event()

    def sleep(seconds: float) -> bool:
        waits.append(seconds)
        if len(waits) == 1:
            asleep.set()
            wake.wait(5)
        return len(waits) >= 3

    library = Library(
        candidates(), tmp_path, CPU_ONLY, trier, _never_built, retry_seconds=10, sleep=sleep
    )
    measuring = threading.Thread(target=library.run_measure)
    measuring.start()
    await asyncio.to_thread(asleep.wait, 5)
    async with library.serving("hear", HEAR_FLOOR):
        wake.set()
        await asyncio.to_thread(measuring.join, 5)

    assert waits == [10, 20, 40]  # the rounds were left for later, each wait longer
    assert [entry_id for entry_id, _, _ in trier.tried].count(HEAR_FLOOR) == 1
    assert library.health()["retrying"] is True
