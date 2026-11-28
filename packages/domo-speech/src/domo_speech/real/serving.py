"""Serving the loaded models: how many requests each one lets in at once, in the order they came,
and when a model is loaded and unloaded.

Everything here runs on the event loop and takes no lock. Loading and unloading a model (seconds,
sometimes gigabytes) run in threads; one load at a time, after the models of the same use being
unloaded are gone. No request is let in before its model is loaded, and then only as many as it
was loaded for. A model that is short of memory beside another model of its use still serving
waits for that one to be gone; short of memory alone, it is loaded for fewer at once (down to
one), and the lower number is remembered. A model no longer asked for is unloaded once its last
request is served: a request in flight is never cut.
"""

import asyncio
import logging
import sys
from collections import deque
from collections.abc import AsyncGenerator, Callable, Coroutine
from contextlib import aclosing, suppress
from dataclasses import dataclass, field
from typing import Any

from domo_speech.data import Does
from domo_speech.engines import EngineUnavailable
from domo_speech.real.build import Engine

logger = logging.getLogger(__name__)

# Loads a model (its id) able to serve that many at once. Blocking: run in a thread.
Loader = Callable[[str, int], Engine]
# The errors of the runtimes that may say a load is short of memory (one runtime raises
# ``RuntimeError``, the other its own types, named here), and the words that say it is.
_RUNTIME_ERRORS: frozenset[str] = frozenset({"RuntimeException", "Fail", "EPFail"})
_MEMORY_WORDS = ("memory", "alloc")


def short_of_room(error: BaseException) -> bool:
    """Whether a load failed for want of memory (the machine's or a graphics card's): with
    fewer at once, or once another model is gone, it may not. The message is read, never kept."""
    if isinstance(error, MemoryError):
        return True
    if isinstance(error, RuntimeError) or type(error).__name__ in _RUNTIME_ERRORS:
        said = str(error).casefold()
        return any(word in said for word in _MEMORY_WORDS)
    return False


class Gate:
    """Lets ``places`` requests in at a time, the others in the order they came. A request that
    goes away while it waits leaves its turn; one given a place just as it went away passes the
    place on. ``places`` may be lowered or raised while requests are inside."""

    def __init__(self, places: int) -> None:
        self.places = places
        self.inside = 0
        self._queue: deque[asyncio.Future[None]] = deque()

    @property
    def waiting(self) -> int:
        return sum(1 for place in self._queue if not place.done())

    async def enter(self) -> None:
        if self.inside < self.places and not self.waiting:
            self.inside += 1
            return
        place: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._queue.append(place)
        try:
            await place
        except asyncio.CancelledError:
            if place.done() and not place.cancelled():
                self.leave()
            elif place in self._queue:
                self._queue.remove(place)
            raise

    def leave(self) -> None:
        self.inside -= 1
        self._let_in()

    def resize(self, places: int) -> None:
        self.places = places
        self._let_in()

    def open(self) -> None:
        """Lets everyone in (to hear that the model cannot be had)."""
        self.resize(sys.maxsize)

    def _let_in(self) -> None:
        while self.inside < self.places and self._queue:
            place = self._queue.popleft()
            if not place.done():
                self.inside += 1
                place.set_result(None)


@dataclass(eq=False)
class Slot:
    """One loaded model (or one being loaded): its gate (no place until it is loaded), how many
    requests and calls hold it, whether another model of its use was asked for since
    (``retired``: unloaded once free)."""

    id: str
    does: Does
    at_once: int
    gate: Gate = field(init=False)
    loaded: "asyncio.Future[Engine]" = field(init=False)
    closed: "asyncio.Future[None]" = field(init=False)
    users: int = 0
    retired: bool = False
    # The number it was asked to be loaded for (``at_once`` may end lower), and whether that
    # lower number is for now only (found beside another model or during a measure round).
    asked: int = field(init=False)
    temporary: bool = False

    def __post_init__(self) -> None:
        self.asked = self.at_once
        loop = asyncio.get_running_loop()
        self.gate = Gate(0)
        self.loaded = loop.create_future()
        self.closed = loop.create_future()


class Shelf:
    """The loaded models of each use."""

    def __init__(
        self,
        load: Loader,
        unload: Callable[[Engine], None],
        lowered: Callable[[str, int], bool],
        measuring: Callable[[], bool],
    ) -> None:
        self._load_engine = load
        self._unload_engine = unload
        # Records a lower number for a model; False when it was not recorded (a measure round).
        self._lowered = lowered
        self._measuring = measuring
        self._shelves: dict[Does, list[Slot]] = {}
        self._closing: dict[Does, set[asyncio.Future[None]]] = {}
        self._changes: dict[Does, asyncio.Event] = {}
        self._loading: asyncio.Lock | None = None
        self._tasks: set[asyncio.Future[Any]] = set()

    def take(self, does: Does, model: str, at_once: int) -> Slot:
        """``model`` for one request, loaded for ``at_once`` (its load started if need be); the
        other models of its use are retired. Hold it until ``give_back``."""
        shelf = self._shelves.setdefault(does, [])
        slot = next((s for s in shelf if s.id == model and self._fits(s, at_once)), None)
        for other in shelf:
            if other is not slot:
                other.retired = True
        for idle in [s for s in shelf if s.retired and not s.users]:
            self._unload(idle)
        if slot is None:
            slot = Slot(model, does, at_once)
            shelf.append(slot)
            self._start(self._load(slot))
        slot.retired = False
        slot.users += 1
        self._signal(does)
        return slot

    def _fits(self, slot: Slot, at_once: int) -> bool:
        """Whether ``slot`` serves a request for ``at_once``: loaded for it, or asked for it and
        lowered for good, or lowered for now while the measure round that lowered it runs."""
        if slot.at_once == at_once:
            return True
        return slot.asked == at_once and (not slot.temporary or self._measuring())

    def give_back(self, slot: Slot) -> None:
        slot.users -= 1
        if not slot.users and slot.retired:
            self._unload(slot)

    def waiting(self, does: Does, model: str) -> int:
        return sum(s.gate.waiting for s in self._shelves.get(does, []) if s.id == model)

    async def engine(self, slot: Slot) -> Engine:
        """The engine of ``slot`` once loaded (``EngineUnavailable`` when it cannot be)."""
        return await asyncio.shield(slot.loaded)

    async def run[T](self, slot: Slot, call: Callable[[Any], T]) -> T:
        """``call(engine)`` in a thread once ``slot`` lets one more in. The place is freed, and
        the model kept, when the call ends, even if the request went away meanwhile."""
        await slot.gate.enter()
        try:
            engine = await self.engine(slot)
        except BaseException:
            slot.gate.leave()
            raise
        slot.users += 1

        def done(working: "asyncio.Future[T]") -> None:
            if not working.cancelled():
                working.exception()  # read here: nobody else may (the request went away)
            slot.gate.leave()
            self.give_back(slot)

        working = asyncio.ensure_future(asyncio.to_thread(call, engine))
        working.add_done_callback(done)
        return await asyncio.shield(working)

    async def stream(
        self, slot: Slot, make: Callable[[Any], AsyncGenerator[bytes]]
    ) -> AsyncGenerator[bytes]:
        """The chunks of ``make(engine)``, made inside the gate and kept until read: the place is
        freed when the work is done, however slowly the caller reads."""
        made: asyncio.Queue[bytes | Exception | None] = asyncio.Queue()

        async def produce() -> None:
            try:
                await slot.gate.enter()
                try:
                    engine = await self.engine(slot)
                    async with aclosing(make(engine)) as chunks:
                        async for chunk in chunks:
                            made.put_nowait(chunk)
                    made.put_nowait(None)
                except Exception as error:
                    made.put_nowait(error)
                finally:
                    slot.gate.leave()
            finally:
                self.give_back(slot)

        slot.users += 1  # the producer holds the model until it has stopped
        producer = asyncio.ensure_future(produce())
        try:
            while (item := await made.get()) is not None:
                if isinstance(item, Exception):
                    raise item
                yield item
        finally:
            producer.cancel()
            with suppress(asyncio.CancelledError):
                await producer

    # Loading and unloading

    def _start(self, work: Coroutine[Any, Any, None]) -> None:
        task = asyncio.ensure_future(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _signal(self, does: Does) -> None:
        event = self._changes.pop(does, None)
        if event is not None:
            event.set()

    async def _changed(self, does: Does) -> None:
        await self._changes.setdefault(does, asyncio.Event()).wait()

    async def _load(self, slot: Slot) -> None:
        """Loads ``slot`` with the turn to load held, except while it waits for another model
        of its use to be gone: that wait has no bound (a request may be read slowly), and the
        models of the other uses are loaded meanwhile."""
        if self._loading is None:
            self._loading = asyncio.Lock()
        turn = self._loading
        await turn.acquire()
        held = True
        try:
            count = slot.at_once
            waited = crowded = False
            while True:
                if not self._on_shelf(slot):
                    # Unloaded before it could be loaded (nobody waits for it): nothing to load.
                    self._fail(slot)
                    return
                # What is being unloaded of this use is gone first.
                await asyncio.gather(*(self._closing.get(slot.does, set()) - {slot.closed}))
                try:
                    engine = await asyncio.to_thread(self._load_engine, slot.id, count)
                except Exception as error:  # any failure: the waiting requests are told
                    if not short_of_room(error):
                        logger.warning("A model could not be loaded (%s)", type(error).__name__)
                        self._fail(slot)
                        return
                    if not waited and self._others(slot):
                        # Another model of this use still serves: once it is gone, the full
                        # number again. The turn to load is given up meanwhile.
                        waited = True
                        turn.release()
                        held = False
                        while self._others(slot) and not slot.retired:
                            await self._changed(slot.does)
                        await turn.acquire()
                        held = True
                        if slot.retired:
                            # Another model of its use was asked for meanwhile: it keeps the
                            # room (the requests waiting here are told; they ask again).
                            self._fail(slot)
                            return
                        continue
                    if count == 1:
                        logger.warning("A model could not be loaded (%s)", type(error).__name__)
                        self._fail(slot)
                        return
                    crowded = crowded or self._others(slot)
                    count -= 1
                    continue
                if self._on_shelf(slot):
                    break
                # Unloaded while it loaded (asked for no more, nobody waits): let it go now.
                self._closing.setdefault(slot.does, set()).add(slot.closed)
                try:
                    await asyncio.to_thread(self._unload_engine, engine)
                finally:
                    self._fail(slot)
                return
            if count != slot.at_once:
                # Kept lower only when found alone and outside a measure round; otherwise the
                # slot serves fewer for now and is loaded in full at the next chance.
                slot.temporary = crowded or not self._lowered(slot.id, count)
                slot.at_once = count
            slot.loaded.set_result(engine)
            slot.gate.resize(count)
        finally:
            if held:
                turn.release()

    def _on_shelf(self, slot: Slot) -> bool:
        return slot in self._shelves.get(slot.does, [])

    def _fail(self, slot: Slot) -> None:
        """``slot`` cannot be had: the requests waiting for it are let in to be told so."""
        if not slot.loaded.done():
            slot.loaded.set_exception(EngineUnavailable())
            slot.loaded.exception()  # the requests waiting read it; none may
        self._drop(slot)
        if not slot.closed.done():
            slot.closed.set_result(None)
        self._closing.get(slot.does, set()).discard(slot.closed)
        slot.gate.open()

    def _others(self, slot: Slot) -> bool:
        """Whether another model of ``slot``'s use is in memory (loaded, or being unloaded)."""
        loaded = (s for s in self._shelves.get(slot.does, []) if s is not slot)
        return any(s.loaded.done() for s in loaded) or bool(self._closing.get(slot.does))

    def _drop(self, slot: Slot) -> None:
        shelf = self._shelves.get(slot.does, [])
        if slot in shelf:
            shelf.remove(slot)
        self._signal(slot.does)

    def _unload(self, slot: Slot) -> None:
        self._drop(slot)
        if not slot.loaded.done():
            return  # never loaded: its load, when its turn comes, finds it gone
        self._closing.setdefault(slot.does, set()).add(slot.closed)
        self._start(self._close(slot))

    async def _close(self, slot: Slot) -> None:
        try:
            if slot.loaded.exception() is None:
                await asyncio.to_thread(self._unload_engine, slot.loaded.result())
        finally:
            if not slot.closed.done():
                slot.closed.set_result(None)
            self._closing.get(slot.does, set()).discard(slot.closed)
            self._signal(slot.does)
