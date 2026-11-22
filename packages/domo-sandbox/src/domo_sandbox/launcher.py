"""The launcher: an ASGI app that runs a tool's code, and scans it, for the core.

A run is opened (``POST /runs``), then led one request of its program at a time
(``POST /runs/{run}/next``) until its end, or stopped (``POST /runs/{run}/stop``). While the core
works on an answer, ``POST /runs/{run}/alive`` keeps the run from being ended as idle.

The bounds of a run are the core's (Domo's settings, spec 12.1): the launcher refuses only what
its machine cannot give (409, ``not_here``, with what it has), and says what it has
(``GET /machine``). How many runs go at once is the core's too (``PUT /places``, Domo's
``places``), applied while runs go: ``slots`` (``DOMO_SANDBOX_SLOTS``) until the core says. A
run a turn waits on (the header ``x-domo-waits: turn``) is served before the others that
queued, so a hostile or busy tool cannot starve a person who is waiting; a run that may not wait
for a place (``wait_for_a_place`` false) is refused at once (409, ``busy``) when none is free,
never queued. A scan does not take a slot: it only reads the code.

Every route is on the launcher's socket, the core's and root's alone (``__main__``).
"""

import asyncio
import heapq
import itertools
from collections.abc import Callable
from typing import Annotated

from domo_common.sandbox_api import (
    OUTPUT_HELD,
    Beyond,
    Machine,
    Places,
    RunEnded,
    RunEvent,
    RunNext,
    RunOpened,
    RunRefused,
    RunRequest,
    ScanRequest,
    ScanResult,
)
from fastapi import FastAPI, Header, HTTPException, Response
from fastapi.responses import JSONResponse

from domo_sandbox import limits, runner
from domo_sandbox.limits import MachineHas
from domo_sandbox.runs import NothingAsked, Runs, Starter, UnknownRun
from domo_sandbox.scan import scan

WAITS_HEADER = "x-domo-waits"
_TURN, _OTHER = 0, 1


class Slots:
    """A number of places, handed to the waiting requests by priority, then by arrival. The number
    changes while runs go (``resize``): the runs that hold a place keep it, and a place is handed
    on only while fewer than the number are held."""

    def __init__(self, count: int) -> None:
        self._count = count
        self._held = 0
        self._queue: list[tuple[int, int, asyncio.Future[None]]] = []
        self._arrival = itertools.count()

    @property
    def count(self) -> int:
        return self._count

    async def acquire(self, priority: int) -> None:
        if self.try_acquire():
            return
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        entry = (priority, next(self._arrival), waiter)
        heapq.heappush(self._queue, entry)
        try:
            await waiter
        except asyncio.CancelledError:
            if waiter.done() and not waiter.cancelled():
                self.release()  # the place was given as the request left: pass it on
            raise

    def try_acquire(self) -> bool:
        """Take a place only if one is free now and nobody waits for one (a request that left
        while it waited is nobody)."""
        while self._queue and self._queue[0][2].done():
            heapq.heappop(self._queue)
        if self._held < self._count and not self._queue:
            self._held += 1
            return True
        return False

    def release(self) -> None:
        self._held -= 1
        self._hand_on()

    def resize(self, count: int) -> None:
        """Run ``count`` at once from now: more hands places to who waits; fewer ends nothing."""
        self._count = count
        self._hand_on()

    def _hand_on(self) -> None:
        while self._queue and self._held < self._count:
            _, _, waiter = heapq.heappop(self._queue)
            if not waiter.done():
                self._held += 1
                waiter.set_result(None)


def _refused(refusal: RunRefused) -> JSONResponse:
    return JSONResponse(status_code=409, content=refusal.model_dump(exclude_none=True))


def create_app(
    *,
    slots: int = 4,
    start: Starter = runner.start,
    machine: Callable[[], MachineHas] = limits.machine,
) -> FastAPI:
    """The launcher, running ``slots`` runs at once until the core says how many (``PUT
    /places``), its runs started by ``start``, its machine read by ``machine``."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    places = Slots(slots)
    runs = Runs(start)

    def has() -> Machine:
        found = machine()
        return Machine(memory_mb=found.memory_mb, processors=found.processors, places=places.count)

    @app.get("/health")
    async def health() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"status": "ok"}

    @app.get("/machine")
    async def what_it_has() -> Machine:  # pyright: ignore[reportUnusedFunction]
        return has()

    @app.put("/places")
    async def set_places(body: Places) -> Places:  # pyright: ignore[reportUnusedFunction]
        places.resize(body.places)
        return Places(places=places.count)

    @app.post("/runs", response_model=None)
    async def open_run(  # pyright: ignore[reportUnusedFunction]
        request: RunRequest,
        x_domo_waits: Annotated[str | None, Header()] = None,
    ) -> RunOpened | JSONResponse:
        machine_has = has()
        beyond = _beyond(request, machine_has)
        if beyond:
            return _refused(RunRefused(refused="not_here", beyond=beyond, has=machine_has))
        if not request.wait_for_a_place:
            if not places.try_acquire():
                return _refused(RunRefused(refused="busy"))
        else:
            await places.acquire(_TURN if x_domo_waits == "turn" else _OTHER)
        try:
            run_id = await runs.open(request, on_end=places.release)
        except BaseException:
            places.release()
            raise
        return RunOpened(run=run_id)

    @app.post("/runs/{run_id}/next")
    async def next_of_run(run_id: str, body: RunNext) -> RunEvent:  # pyright: ignore[reportUnusedFunction]
        try:
            return await runs.next(run_id, body.answer)
        except UnknownRun:
            raise HTTPException(status_code=404, detail="no such run") from None
        except NothingAsked:
            raise HTTPException(status_code=409, detail="nothing was asked") from None

    @app.post("/runs/{run_id}/stop")
    async def stop_run(run_id: str) -> RunEnded:  # pyright: ignore[reportUnusedFunction]
        try:
            return await runs.stop(run_id)
        except UnknownRun:
            raise HTTPException(status_code=404, detail="no such run") from None

    @app.post("/runs/{run_id}/alive", status_code=204)
    async def keep_run(run_id: str) -> Response:  # pyright: ignore[reportUnusedFunction]
        try:
            runs.alive(run_id)
        except UnknownRun:
            raise HTTPException(status_code=404, detail="no such run") from None
        return Response(status_code=204)

    @app.post("/scan")
    async def scan_code(request: ScanRequest) -> ScanResult:  # pyright: ignore[reportUnusedFunction]
        return await asyncio.to_thread(scan, request.code)

    return app


def _beyond(request: RunRequest, machine: Machine) -> list[Beyond]:
    """The limits of ``request`` its machine cannot give: more memory than it has, or an output
    the launcher could not hold (``OUTPUT_HELD`` times over, for each of its places at once: it
    holds the outputs itself, under the same memory as its runs). A machine that cannot tell its
    memory refuses none; a time is never beyond it."""
    if machine.memory_mb is None:
        return []
    beyond: list[Beyond] = []
    if request.memory_mb > machine.memory_mb:
        beyond.append("memory_mb")
    held = request.output_bytes * OUTPUT_HELD * machine.places
    if held > machine.memory_mb * 1024 * 1024:
        beyond.append("output_bytes")
    return beyond
