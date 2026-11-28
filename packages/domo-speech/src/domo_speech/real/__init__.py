"""The real engines: the candidates that hold on this machine, loaded when first asked for.

``real_engines`` answers at once; the measure of the candidates (``domo_speech.measure``) runs
in the background, at the first start and when the machine changes, and the service says
``preparing`` until the first use is measured. Candidates whose weights could not be fetched are
tried again alone, waiting twice as long each time (10 minutes, then up to 6 hours); meanwhile
the service says ``retrying``, and ``failed`` with ``weights_unreachable`` when nothing holds. A
use served only by a model kept although too slow (``slow``, named in ``/health`` and on the model
in the list), or left with nothing because its trials were too slow, failed or heard wrong, is
measured again the same way, at most ``REMEASURES`` times in one start (also while weights are
still tried again), never while requests are being served (that round waits for the next), and
never losing what held (see ``domo_speech.measure``).
The caller names the model it wants among those that hold: the service never chooses one for
it. One model per use is kept loaded; asking for another of the same use unloads the first once
the requests it is serving are done (a request in flight is never cut).

Each model serves as many requests at once as the measure found it holds on this machine (the
owner's ``DOMO_SPEECH_AT_ONCE`` may lower that, never raise it); the others wait their turn, in
the order they came, and none is dropped. A model that holds one at a time serves one at a time
(a voice, one sentence at a time).
"""

import logging
import threading
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping
from contextlib import aclosing, asynccontextmanager
from pathlib import Path
from typing import Any, Final

from domo_speech.config import SpeechSettings
from domo_speech.data import Does, ModelEntryData, offered
from domo_speech.engines import (
    Engines,
    ModelEntry,
    Readiness,
    Transcript,
    UnknownModel,
    VoicePrint,
)
from domo_speech.measure import (
    TRIAL_SECONDS,
    Machine,
    Measured,
    Trial,
    Trier,
    current,
    measure,
    save,
    this_machine,
)
from domo_speech.real.build import Engine, build, close
from domo_speech.real.device import cuda_devices, runtime
from domo_speech.real.hears import Hearing
from domo_speech.real.prints import EmbeddingPrint
from domo_speech.real.says import FlowVoice, ProgramVoice
from domo_speech.real.serving import Shelf, Slot
from domo_speech.real.trial import RealTrier
from domo_speech.real.weights import ensure_weights

logger = logging.getLogger(__name__)

# When some weights could not be fetched, they are tried again after this long, then twice as
# long each time, up to the longest wait.
RETRY_SECONDS: Final = 600.0
MAX_RETRY_SECONDS: Final = 6 * 3600.0
# A use served only by a slow model, or left with nothing because its trials were too slow,
# failed or heard wrong, is measured again at most this many times in one start, with the same
# waits (10 minutes, doubling: about five hours in all); then at the next start.
REMEASURES: Final = 5

# Builds a model's engine: on the graphics card or not, able to serve that many at once.
Builder = Callable[[ModelEntryData, bool, int], Engine]


class Library:
    """The measure's state, and the engines of the candidates that hold."""

    def __init__(
        self,
        entries: tuple[ModelEntryData, ...],
        model_dir: Path,
        machine: Machine,
        trier: Trier,
        builder: Builder,
        *,
        retry_seconds: float = RETRY_SECONDS,
        max_retry_seconds: float = MAX_RETRY_SECONDS,
        trial_seconds: float = TRIAL_SECONDS,
        sleep: Callable[[float], bool] | None = None,
        at_once_cap: int | None = None,
    ) -> None:
        self._entries = {entry.id: entry for entry in entries}
        self._model_dir = model_dir
        self._machine = machine
        self._trier = trier
        self._builder = builder
        self._retry_seconds = retry_seconds
        self._max_retry_seconds = max_retry_seconds
        self._trial_seconds = trial_seconds
        self._measured: Measured | None = current(model_dir, entries, machine)
        # No round of this machine finished yet: ``preparing`` until a use is measured.
        self._first = self._measured is None
        # Some weights were not fetched: their candidates are tried again later. A use served
        # slowly, or left with nothing because its trials were too slow, failed or heard wrong, is
        # measured again at this start.
        self._retrying = self._measured is not None and (
            not self._measured.complete or bool(self._measured.again())
        )
        self._running = False
        self._serving = 0  # requests being served now
        self._at_once_cap = at_once_cap
        # The loaded models of each use: the one last asked for, and those still serving a
        # request when another was asked for.
        self._loaded: Shelf | None = None  # made on the event loop, at the first request
        # Models that could not be loaded for as many at once as measured: how many they serve.
        self._lowered: dict[str, int] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._sleep = sleep or self._stop.wait
        self._broken = False

    # The measure

    def run_measure(self) -> None:
        """Measures, then tries again what could not be fetched until it is (blocking; run in a
        thread). A measure that breaks leaves the service ``failed``, never ``preparing`` for
        ever."""
        try:
            self._rounds()
        except Exception as error:
            logger.warning("The measure broke (%s)", type(error).__name__)
            with self._lock:
                self._first = False
                self._running = False
                self._broken = True

    def _publish(self, measured: Measured) -> None:
        with self._lock:
            self._measured = measured

    def _rounds(self) -> None:
        wait = self._retry_seconds
        first = True
        remeasured = 0  # later rounds that measured again what was slow, failed or heard wrong
        while True:
            # A re-measure loads a second copy of a model beside the one serving: not while
            # requests are served (the round is left for after the next wait).
            busy = not first and self._serving > 0
            previous = self._measured
            if busy and previous is not None and previous.complete:
                logger.info("Requests are being served; the measure is done again later")
            else:
                again = first or (remeasured < REMEASURES and not busy)
                self._running = True
                measured = measure(
                    self._entries.values(),
                    self._machine,
                    self._trier,
                    previous=previous,
                    trial_seconds=self._trial_seconds,
                    publish=self._publish,
                    again=again,
                )
                if again and not first:
                    remeasured += 1
                first = False
                with self._lock:
                    self._measured = measured
                    self._first = False
                    self._running = False
                    more = bool(measured.again()) and remeasured < REMEASURES
                    self._retrying = not measured.complete or more
                save(self._model_dir, measured)
                logger.info("Measured: %d candidates hold on this machine", len(measured.held()))
                if not self._retrying:
                    return
                if not measured.complete:
                    logger.warning("Some weights could not be fetched; trying them again later")
                if more:
                    logger.warning(
                        "Too slow or failed on this machine, measured again later: %s",
                        ", ".join(measured.again()),
                    )
            if self._sleep(wait):
                return
            wait = min(wait * 2, self._max_retry_seconds)

    def start(self) -> None:
        if self._first or self._retrying:
            threading.Thread(target=self.run_measure, name="measure", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def ready(self) -> Readiness:
        measured = self._measured
        if measured is not None and measured.held():
            return "ready"
        if self._first and not self._broken:
            return "preparing"
        return "failed"

    def health(self) -> Mapping[str, object]:
        """What ``/health`` says besides the status: ``reason`` when failed (``nothing_holds``,
        ``weights_unreachable``, ``measure_failed``), ``missing`` uses, ``slow`` uses (served by a
        model kept although too slow), ``measuring`` while a round runs, ``retrying`` while
        another round is to come."""
        measured = self._measured
        found: dict[str, object] = {}
        if measured is None:
            if self._broken:
                found["reason"] = "measure_failed"
            return found
        if self._running and not self._first:
            found["measuring"] = True
        if self._running and self._first and measured.held():
            found["measuring"] = True
        missing = list(measured.missing())
        if missing and not self._first:
            found["missing"] = missing
        if slow := list(measured.slow()):
            found["slow"] = slow
        if not measured.held() and not self._first:
            if self._broken:
                found["reason"] = "measure_failed"
            else:
                unfetched = self._retrying and not measured.complete
                found["reason"] = "weights_unreachable" if unfetched else "nothing_holds"
        if self._retrying:
            found["retrying"] = True
        return found

    def models(self) -> tuple[ModelEntry, ...]:
        measured = self._measured
        if measured is None:
            return ()
        listed: list[ModelEntry] = []
        for trial in measured.held():
            entry = self._entries[trial.id]
            listed.append(
                ModelEntry(
                    id=trial.id,
                    does=trial.does,
                    measures=trial.measures(),
                    licence=entry.licence,
                    licence_url=entry.licence_url,
                    restricted=entry.restricted,
                    thresholds=entry.thresholds,
                    at_once=self._serves(trial),
                    slow=trial.why == "slow",
                )
            )
        return tuple(listed)

    def _serves(self, trial: Trial) -> int:
        """How many at once a model serves: what was measured, lowered by the owner's cap and by
        a load that did not fit."""
        count = max(1, trial.at_once or 1)
        for lower in (self._at_once_cap, self._lowered.get(trial.id)):
            if lower is not None:
                count = min(count, lower)
        return count

    # The engines

    def _lower(self, model: str, count: int) -> bool:
        """``model`` could not be loaded for more than ``count`` at once here: remembered while
        the service runs, unless a measure round runs (its trials take memory too, the graphics
        card's among it: the model is served with fewer for now, and asked for in full again)."""
        if self._running:
            return False
        self._lowered[model] = count
        return True

    def _shelf(self) -> Shelf:
        if self._loaded is None:

            def load(model: str, at_once: int) -> Engine:
                trial = self._held(model)
                return self._builder(self._entries[model], trial.device == "gpu", at_once)

            self._loaded = Shelf(load, close, self._lower, lambda: self._running)
        return self._loaded

    def _held(self, model: str) -> Trial:
        measured = self._measured
        held = {t.id: t for t in measured.held()} if measured is not None else {}
        return held[model]

    @asynccontextmanager
    async def serving(self, does: Does, model: str | None) -> AsyncGenerator[Slot]:
        """The model ``model`` for one request (its load started if need be), held until the
        request ends; ``UnknownModel`` when it is not one that holds for ``does``."""
        measured = self._measured
        held = {t.id: t for t in measured.held()} if measured is not None else {}
        trial = held.get(model or "")
        if trial is None or trial.does != does:
            raise UnknownModel
        shelf = self._shelf()
        slot = shelf.take(does, trial.id, self._serves(trial))
        with self._lock:
            self._serving += 1
        try:
            yield slot
        finally:
            with self._lock:
                self._serving -= 1
            shelf.give_back(slot)

    def queued(self, does: Does, model: str) -> int:
        """How many requests wait their turn for ``model`` now."""
        return 0 if self._loaded is None else self._loaded.waiting(does, model)

    async def engine(self, slot: Slot) -> Engine:
        return await self._shelf().engine(slot)

    async def run[T](self, slot: Slot, call: Callable[[Any], T]) -> T:
        return await self._shelf().run(slot, call)

    def stream(
        self, slot: Slot, make: Callable[[Any], AsyncGenerator[bytes]]
    ) -> AsyncGenerator[bytes]:
        return self._shelf().stream(slot, make)


class RealHears:
    def __init__(self, library: Library) -> None:
        self._library = library

    async def transcribe(
        self, pcm: bytes, *, language: str | None, model: str | None
    ) -> Transcript:
        async with self._library.serving("hear", model) as slot:
            return await self._library.run(
                slot, lambda engine: _hearing(engine).transcribe(pcm, language)
            )


class RealPrints:
    def __init__(self, library: Library) -> None:
        self._library = library

    async def voice_print(self, pcm: bytes, *, model: str | None) -> VoicePrint:
        async with self._library.serving("voice_print", model) as slot:
            return await self._library.run(slot, lambda engine: _printing(engine).voice_print(pcm))


def _hearing(engine: Any) -> Hearing:
    return engine  # a hearing engine (or a test's stand-in)


def _printing(engine: Any) -> EmbeddingPrint:
    return engine


class RealSays:
    """A voice inside the service lets one sentence of a request in at a time (two answers at
    once on a model that holds one share it sentence by sentence, as before); a separate program
    voice lets one run of the program in, its sound kept until read: a slow reader holds no
    place."""

    def __init__(self, library: Library) -> None:
        self._library = library

    async def say(
        self, text: str, *, voice: str | None, language: str | None, model: str | None
    ) -> AsyncIterator[bytes]:
        async with self._library.serving("say", model) as slot:
            engine = await self._library.engine(slot)
            if isinstance(engine, ProgramVoice):
                async with aclosing(self._library.stream(slot, lambda e: e.say(text))) as chunks:
                    async for chunk in chunks:
                        yield chunk
                return
            flow: FlowVoice = engine  # type: ignore[assignment]
            chunks = flow.say(text, voice, language)
            while (
                chunk := await self._library.run(slot, lambda _: next(chunks, None))
            ) is not None:
                yield chunk


def _gpus(device: str) -> tuple[str, ...]:
    """The graphics cards the runtimes can use (none when the setting says ``cpu``)."""
    if device == "cpu":
        return ()
    found = [f"cuda:{index}" for index in range(cuda_devices())]
    try:
        if "CUDAExecutionProvider" in runtime("onnxruntime").get_available_providers():
            found.append("onnx:cuda")
    except ImportError:
        pass
    return tuple(found)


def tell_restrictions(entries: tuple[ModelEntryData, ...]) -> None:
    """Says, at the start, which offered models have conditions of use and where to read them
    (plain text: it is read by whoever installs the service, before any key)."""
    for entry in entries:
        if entry.restricted:
            logger.warning(
                "The model %s has conditions of use (its licence restricts how it may be used): "
                "%s. Set DOMO_SPEECH_RESTRICTED_MODELS=no to leave it out.",
                entry.id,
                entry.licence_url,
            )


def real_engines(settings: SpeechSettings) -> Engines:
    """The engines of the candidates that hold here; the measure starts in the background."""
    entries = offered(restricted=settings.restricted_models)
    tell_restrictions(entries)
    program = settings.voice_program
    model_dir = settings.model_dir

    def builder(entry: ModelEntryData, gpu: bool, at_once: int) -> Engine:
        weights = ensure_weights(entry, model_dir)
        return build(entry, weights, gpu=gpu, program=program, at_once=at_once)

    library = Library(
        entries,
        model_dir,
        this_machine(settings.device, _gpus(settings.device), str(program or "")),
        RealTrier(model_dir, program),
        builder,
        at_once_cap=settings.at_once,
    )
    library.start()
    return Engines(
        hears=RealHears(library),
        says=RealSays(library),
        prints=RealPrints(library),
        models=library.models,
        ready=library.ready,
        health=library.health,
    )
