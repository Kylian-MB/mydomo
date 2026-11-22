"""The runs the launcher holds open for the core, each with its own channel.

A run is known by an id nobody can guess (``secrets.token_urlsafe(16)``) and reached only by it:
the core carries the answer to the program's last request and gets the next one (``next``), one
at a time, or ends it (``stop``). The program never sees its id nor another run's: its requests go
on its own pipe, its answers come on its own standard input.

A run whose core stops asking about it (no ``next``, ``alive`` nor ``stop`` for ``IDLE_SECONDS``
while it waits on the core) is ended: a core that went away leaves no program running. A core
that takes long to answer says so with ``alive``.
"""

import asyncio
import logging
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from domo_common.sandbox_api import IDLE_SECONDS, JsonObject, RunAsk, RunEnded, RunRequest

from domo_sandbox import runner
from domo_sandbox.runner import LiveRun

logger = logging.getLogger(__name__)


type Starter = Callable[[RunRequest], Awaitable[LiveRun]]


class UnknownRun(Exception):
    """No run has this id (it never had, or it ended)."""


class NothingAsked(Exception):
    """An answer came while the program had asked nothing."""


@dataclass(eq=False)
class _Held:
    live: LiveRun
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # The request the program waits on, given again until it is answered.
    pending: RunAsk | None = None
    watchdog: asyncio.Task[None] | None = None
    idle: bool = False


class Runs:
    def __init__(self, start: Starter = runner.start) -> None:
        self._start = start
        self._held: dict[str, _Held] = {}

    async def open(self, request: RunRequest, *, on_end: Callable[[], None] | None = None) -> str:
        """Start a run; ``on_end`` is called once it is over. Returns its id."""
        live = await self._start(request)
        run_id = secrets.token_urlsafe(16)
        held = _Held(live)
        self._held[run_id] = held
        live.when_ended(lambda: self._forget(run_id, held))
        if on_end is not None:
            live.when_ended(on_end)
        self._watch(held)
        return run_id

    async def next(self, run_id: str, answer: JsonObject | None) -> RunAsk | RunEnded:
        """Give the program ``answer`` to its last request, then wait for its next one or its end.
        Without an answer, a request still waiting is given again."""
        held = self._get(run_id)
        async with held.lock:
            if self._held.get(run_id) is not held:
                raise UnknownRun
            self._quiet(held)
            try:
                if held.pending is not None:
                    if answer is None:
                        return held.pending
                    held.pending = None
                    await held.live.answer(answer)
                elif answer is not None:
                    raise NothingAsked
                event = await held.live.next_event()
                if isinstance(event, RunAsk):
                    held.pending = event
                return event
            finally:
                if self._held.get(run_id) is held:
                    self._watch(held)

    def alive(self, run_id: str) -> None:
        """The core still works on the answer to the program's request: the idle clock starts
        again. Nothing changes while a ``next`` waits on the program (it is not idle then)."""
        held = self._get(run_id)
        if held.watchdog is not None:
            self._watch(held)

    async def stop(self, run_id: str) -> RunEnded:
        """End the run now, even while a ``next`` waits on its program."""
        held = self._get(run_id)
        self._quiet(held)
        return await held.live.stop()

    def _get(self, run_id: str) -> _Held:
        held = self._held.get(run_id)
        if held is None:
            raise UnknownRun
        return held

    def _forget(self, run_id: str, held: _Held) -> None:
        if self._held.get(run_id) is held:
            del self._held[run_id]
        self._quiet(held)

    def _watch(self, held: _Held) -> None:
        if held.idle:
            return
        self._quiet(held)
        held.watchdog = asyncio.create_task(self._end_when_idle(held))

    @staticmethod
    def _quiet(held: _Held) -> None:
        """Stop watching for idleness; a watch that already fired ends the run all the same."""
        if held.watchdog is not None and not held.idle:
            held.watchdog.cancel()
            held.watchdog = None

    @staticmethod
    async def _end_when_idle(held: _Held) -> None:
        await asyncio.sleep(IDLE_SECONDS)
        held.idle = True
        logger.warning("a run was ended: the core had stopped asking about it")
        await held.live.stop()
