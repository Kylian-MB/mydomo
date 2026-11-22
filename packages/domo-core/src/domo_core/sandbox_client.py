"""The core's side of the sandbox: a client of the launcher that runs a tool's code.

The launcher listens on a Unix socket (``DOMO_SANDBOX_SOCKET``). The core never runs a model's
code itself: it sends it there (``run``) or has it read by the analyser (``scan``).

A run is led over its own channel: the core opens it (``POST /runs``), then carries the answer to
each request of the program and gets the next one (``POST /runs/{run}/next``) until its end. Each
request is answered by the caller's ``Answerer``; while it works on one, the core keeps the run
alive (``POST /runs/{run}/alive``). The run is stopped (``POST /runs/{run}/stop``) when the
answerer says so (``None``), fails, or its caller is cancelled. Its bounds are Domo's settings,
which the core sends with it; the launcher refuses only what its machine cannot give
(``SandboxNotHere``, with what it has). The core also tells the launcher how many runs go at once
(``set_places``, Domo's ``places``) and reads what its machine has (``machine``).

Building the client never touches the socket: the core starts and serves people whether or not
the sandbox is up. Only a call fails, with ``SandboxUnavailable``, and it fails closed: an answer
that is not well formed (an HTTP error, a body that does not validate, a transport that timed
out) is the same failure, so a scan can never come back as "no finding" because of an error.
"""

import asyncio
import contextlib
import logging
import socket as _socket
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Final, Protocol

import httpx
from domo_common.sandbox_api import (
    ALIVE_EVERY_SECONDS,
    LONGEST_SECONDS,
    Beyond,
    JsonObject,
    Machine,
    Places,
    RunEnded,
    RunEvent,
    RunNext,
    RunOpened,
    RunRefused,
    RunRequest,
    RunResult,
    ScanRequest,
    ScanResult,
)
from pydantic import BaseModel, TypeAdapter, ValidationError

logger = logging.getLogger(__name__)

# The header of a run a person's turn waits on: the launcher serves it before the others.
WAITS_HEADER: Final = "x-domo-waits"
WAITS_FOR_A_TURN: Final = "turn"

# What the transport waits beyond a run's own ``max_seconds``: for a place in the launcher's queue
# (handed by priority) when it opens, for the clean-up of the run when it ends. A caller bounds
# its own wait; this only keeps a launcher that is stuck from holding the core's call for ever.
RUN_MARGIN_S: Final = 60.0
# The analyser reads a code; it needs no slot, and a code is small.
SCAN_TIMEOUT_S: Final = 60.0
# A stop or an alive is answered at once (a stop ends the run as a timeout is).
SHORT_TIMEOUT_S: Final = 30.0
CONNECT_TIMEOUT_S: Final = 5.0
WRITE_TIMEOUT_S: Final = 30.0
POOL_TIMEOUT_S: Final = 10.0

_BASE_URL: Final = "http://sandbox"
_EVENT: Final = TypeAdapter[RunEvent](RunEvent)

# What answers a request of the program: the answer it reads (``{"ok": value}`` or
# ``{"refused": reason, "detail"?: text}``), or ``None`` to stop the run.
type Answerer = Callable[[JsonObject], Awaitable[JsonObject | None]]


async def _refuse_all(ask: JsonObject) -> JsonObject | None:
    return {"refused": "not_allowed"}


# The answerer of a run whose requests nobody serves yet: each one is refused.
REFUSE_ALL: Final[Answerer] = _refuse_all


class SandboxUnavailable(Exception):
    """The launcher did not give a usable answer: it is down, too slow, or answered something that
    is not a result. Nothing is known of what became of the code."""


class SandboxBusy(Exception):
    """Every place of the launcher is taken, and the run may not wait for one
    (``RunRequest.wait_for_a_place`` false): it was not started."""


class SandboxNotHere(Exception):
    """The launcher's machine cannot give what the run asks (``beyond``: which of its limits),
    said with what it has (``has``): it was not started."""

    def __init__(self, beyond: list[Beyond], has: Machine) -> None:
        super().__init__(f"The sandbox's machine cannot give {', '.join(beyond)}.")
        self.beyond = beyond
        self.has = has


class Sandbox(Protocol):
    """What the core asks of the sandbox (the client, or a double in the tests)."""

    async def run(
        self, request: RunRequest, *, answer: Answerer, waited_by_a_turn: bool = False
    ) -> RunResult: ...

    async def scan(self, code: str) -> ScanResult: ...

    async def set_places(self, places: int) -> None: ...

    async def machine(self) -> Machine: ...


def _no_unix_sockets(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("this system has no Unix sockets", request=request)


def new_http_client(
    socket: Path, *, transport: httpx.AsyncBaseTransport | None = None
) -> httpx.AsyncClient:
    """The HTTP client of the calls to the launcher at ``socket``. Nothing connects until a call;
    ``transport`` is for tests. The read timeout is set by each call. On a system without Unix
    sockets (a developer's Windows) there is no launcher to reach: each call fails as one that is
    down."""
    if transport is None:
        transport = (
            httpx.AsyncHTTPTransport(uds=str(socket))
            if hasattr(_socket, "AF_UNIX")
            else httpx.MockTransport(_no_unix_sockets)
        )
    return httpx.AsyncClient(
        base_url=_BASE_URL,
        transport=transport,
        follow_redirects=False,
        trust_env=False,
    )


def _timeout(read: float) -> httpx.Timeout:
    return httpx.Timeout(
        connect=CONNECT_TIMEOUT_S, read=read, write=WRITE_TIMEOUT_S, pool=POOL_TIMEOUT_S
    )


class SandboxClient:
    """The launcher's API. ``http`` is owned by the caller (the lifespan closes it)."""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def run(
        self, request: RunRequest, *, answer: Answerer, waited_by_a_turn: bool = False
    ) -> RunResult:
        """Run ``request``, answer each request of its program with ``answer``, and say what
        happened. ``waited_by_a_turn`` puts the run before the others that queued, for a person
        who is waiting for the answer. Raises ``SandboxBusy`` when the run may not wait for a
        place and none is free, ``SandboxNotHere`` when its machine cannot give what ``request``
        asks."""
        headers = {WAITS_HEADER: WAITS_FOR_A_TURN} if waited_by_a_turn else {}
        # A time past any clock is waited for as long as a clock goes.
        waits = _timeout(min(request.max_seconds, LONGEST_SECONDS) + RUN_MARGIN_S)
        content = await self._post("/runs", request, waits, headers, refusal_is_known=True)
        opened = self._read("/runs", content, RunOpened.model_validate_json)
        path = f"/runs/{opened.run}"
        over = False
        try:
            reply: JsonObject | None = None
            while True:
                content = await self._post(f"{path}/next", RunNext(answer=reply), waits, {})
                event = self._read("/runs/next", content, _EVENT.validate_json)
                if isinstance(event, RunEnded):
                    over = True
                    return event.result
                reply = await self._answered(path, answer, event.ask)
                if reply is None:
                    content = await self._post(f"{path}/stop", None, _timeout(SHORT_TIMEOUT_S), {})
                    over = True
                    return self._read("/runs/stop", content, RunEnded.model_validate_json).result
        finally:
            if not over:
                # Failed, or its caller is gone: the run must not go on alone.
                await self._stop_quietly(path)

    async def scan(self, code: str) -> ScanResult:
        """What the analyser finds in ``code``, which is not run."""
        content = await self._post("/scan", ScanRequest(code=code), _timeout(SCAN_TIMEOUT_S), {})
        return self._read("/scan", content, ScanResult.model_validate_json)

    async def set_places(self, places: int) -> None:
        """Have the launcher run ``places`` runs at once from now (Domo's setting ``places``,
        one at least: ``ValueError`` otherwise, nothing sent), without a restart."""
        body = Places(places=places)
        content = await self._post(
            "/places", body, _timeout(SHORT_TIMEOUT_S), {}, method="PUT", quiet=True
        )
        self._read("/places", content, Places.model_validate_json)

    async def machine(self) -> Machine:
        """What the launcher's machine has, and how many runs it runs at once now."""
        content = await self._post("/machine", None, _timeout(SHORT_TIMEOUT_S), {}, method="GET")
        return self._read("/machine", content, Machine.model_validate_json)

    async def _answered(self, path: str, answer: Answerer, ask: JsonObject) -> JsonObject | None:
        """``answer`` to ``ask``; the run is kept alive while it is being worked on."""
        keeping = asyncio.create_task(self._keep_alive(path))
        try:
            return await answer(ask)
        finally:
            keeping.cancel()
            await asyncio.wait({keeping})

    async def _keep_alive(self, path: str) -> None:
        while True:
            await asyncio.sleep(ALIVE_EVERY_SECONDS)
            # A failed alive is not the run's end: the next request says what became of it.
            with contextlib.suppress(SandboxUnavailable):
                await self._post(f"{path}/alive", None, _timeout(SHORT_TIMEOUT_S), {}, expect=204)

    async def _stop_quietly(self, path: str) -> None:
        """Stop the run, and go on even when the stop itself fails or is cancelled: a run the
        launcher still holds ends on its own once nobody asks about it."""
        stopping = asyncio.ensure_future(
            self._post(f"{path}/stop", None, _timeout(SHORT_TIMEOUT_S), {})
        )
        with contextlib.suppress(SandboxUnavailable):
            await asyncio.shield(stopping)

    async def _post(
        self,
        path: str,
        body: BaseModel | None,
        timeout: httpx.Timeout,
        headers: dict[str, str],
        *,
        expect: int = 200,
        refusal_is_known: bool = False,
        method: str = "POST",
        quiet: bool = False,
    ) -> bytes:
        """The body of the launcher's answer to ``path`` (asked with ``method``). ``path`` names a
        route of the launcher, and may carry a run's id: it is never logged. A run it refuses
        (409, ``refusal_is_known``) raises ``SandboxBusy`` or ``SandboxNotHere``. ``quiet``: a
        failure is logged for debugging only (a call the upkeep makes again each round)."""
        route = _route(path)
        failure: str
        try:
            response = await self._http.request(
                method,
                path,
                content=b"" if body is None else body.model_dump_json().encode(),
                headers={"content-type": "application/json", **headers},
                timeout=timeout,
            )
        except (httpx.HTTPError, httpx.InvalidURL, OSError) as exc:
            failure = type(exc).__name__
        else:
            if response.status_code == expect:
                return response.content
            if refusal_is_known and response.status_code == 409:
                _raise_refusal(response.content)
                failure = "bad refusal"
            else:
                failure = f"status {response.status_code}"
        # Raised outside the handlers, and with nothing of the code or of what answered.
        logger.log(
            logging.DEBUG if quiet else logging.WARNING,
            "The sandbox call %s failed: %s",
            route,
            failure,
        )
        raise SandboxUnavailable(f"The sandbox call {route} failed ({failure}).")

    @staticmethod
    def _read[M](route: str, content: bytes, parse: Callable[[bytes], M]) -> M:
        try:
            return parse(content)
        except ValidationError:
            logger.warning("The sandbox call %s failed: bad response", route)
            raise SandboxUnavailable(f"The sandbox call {route} failed (bad response).") from None


def _raise_refusal(content: bytes) -> None:
    """Raise what the launcher's refusal of a run says (``RunRefused``); return when it says
    nothing that can be read (the caller fails closed)."""
    try:
        refused = RunRefused.model_validate_json(content)
    except ValidationError:
        return
    if refused.refused == "busy":
        raise SandboxBusy
    if refused.beyond and refused.has is not None:
        raise SandboxNotHere(list(refused.beyond), refused.has)


def _route(path: str) -> str:
    """``path`` without a run's id: ``/runs/<id>/next`` is ``/runs/next``."""
    parts = path.split("/")
    if len(parts) == 4 and parts[1] == "runs":
        return f"/runs/{parts[3]}"
    return path


class _NoSandbox:
    """The sandbox of a core that was given none: every call fails as a launcher that is down."""

    async def run(
        self, request: RunRequest, *, answer: Answerer, waited_by_a_turn: bool = False
    ) -> RunResult:
        raise SandboxUnavailable("There is no sandbox.")

    async def scan(self, code: str) -> ScanResult:
        raise SandboxUnavailable("There is no sandbox.")

    async def set_places(self, places: int) -> None:
        raise SandboxUnavailable("There is no sandbox.")

    async def machine(self) -> Machine:
        raise SandboxUnavailable("There is no sandbox.")


NO_SANDBOX: Final[Sandbox] = _NoSandbox()
