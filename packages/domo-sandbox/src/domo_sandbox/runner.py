"""Runs one tool's code in a process of its own, and holds the channel of that run.

The child is a fresh ``python -I -S``: an empty environment, no site packages, no file left
behind. The code, its entry and the module ``domo`` reach it as the first line of its standard
input. While it runs, the program's requests come as lines of JSON on a descriptor of its own
(the answer descriptor; what the tool prints goes nowhere) and the core's answers go back as lines
on its standard input, one request at a time. Its last line is its answer. Both pipes are the
run's alone: the child holds nothing of the launcher nor of another run. The launcher holds the
clock, the caps on each line, and kills the whole process group when it has to.

What the child writes is data, not trusted: the child itself runs code a model wrote.
"""

import asyncio
import contextlib
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Callable
from importlib import resources
from typing import Any, Literal, cast

from domo_common.sandbox_api import (
    LONGEST_SECONDS,
    MAX_ASK_BYTES,
    PROGRAM_FUNCTIONS,
    PROGRAM_MODULE,
    JsonObject,
    RunAsk,
    RunEnded,
    RunRequest,
    RunResult,
    RunStatus,
)
from pydantic import JsonValue

from domo_sandbox import limits

# What runs in the child. ``-I -S`` means it cannot import this package: it stands alone. It first
# makes itself the process the kernel ends at the memory wall the runs share with the launcher
# (``oom_score_adj`` 1000: raising one's own needs no capability, and its children inherit it). The
# answer descriptor is a copy of its standard output (3), whose own descriptor goes to nothing;
# the answers are read on a copy of its standard input, whose own descriptor reads nothing.
_BOOTSTRAP = r"""
import json, os, sys, traceback, types

# The first the kernel kills when the memory runs out, before the launcher (Linux; elsewhere
# there is no such file).
try:
    with open("/proc/self/oom_score_adj", "w") as adjust:
        adjust.write("1000")
except OSError:
    pass

def _last_line(exc):
    text = "".join(traceback.format_exception_only(type(exc), exc)).strip()
    return text.splitlines()[-1] if text else type(exc).__name__

answer = os.fdopen(os.dup(1), "wb")
answers = os.fdopen(os.dup(0), "rb")
sink = os.open(os.devnull, os.O_WRONLY)
os.dup2(sink, 1)
empty = os.open(os.devnull, os.O_RDONLY)
os.dup2(empty, 0)
sys.stdout = open(os.devnull, "w")
sys.stdin = open(os.devnull)
try:
    request = json.loads(answers.readline())
    module = types.ModuleType(request["name"])
    exec(compile(request["module"], "<" + request["name"] + ">", "exec"), module.__dict__)
    module._connect(answers, answer, request["entry"].get("dry_run") is True)
    sys.modules[request["name"]] = module
    namespace = {"__name__": "tool"}
    exec(compile(request["code"], "<tool>", "exec"), namespace)
    run = namespace.get("run")
    if not callable(run):
        raise TypeError("the code defines no run(entry)")
    message = {"ok": run(request["entry"])}
    try:
        data = json.dumps(message, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except UnicodeEncodeError:
        data = json.dumps(message, allow_nan=False).encode("utf-8")
except BaseException as exc:
    data = json.dumps({"error": _last_line(exc)}).encode("utf-8")
answer.write(data)
answer.flush()
"""

# The source of the module ``domo``, read once when the launcher starts: never imported here.
_PROGRAM_SOURCE = (
    resources.files("domo_sandbox").joinpath("program_module.py").read_text(encoding="utf-8")
)

_ERROR_MAX = 300
# A path of the host or of the container, in whatever way a message spells it.
_PATH = re.compile(r"(?<![\w.])(?:[A-Za-z]:)?[\\/][\w.\-]+(?:[\\/][\w.\-]+)+")
# What an answer may carry besides the tool's output: the envelope's keys.
_ENVELOPE_ROOM = 64
# The room an answer always has, whatever the run's ``answer_bytes``: a refusal and its words
# (the core's own, that name the setting) fit in it. A floor, never a ceiling.
_REFUSAL_ROOM = 4096

_REAP_SECONDS = 5

logger = logging.getLogger(__name__)
_NOT_ENDED = "The run could not be ended."
_NOT_VALID = "The tool's answer is not valid."
_TOO_BIG = "The answer is bigger than allowed."
_OUT_OF_TIME = "The tool ran out of time."
_ASK_NOT_VALID = "A request of the tool is not valid."
_ASK_TOO_BIG = "A request of the tool is bigger than allowed."
_STOPPED = "The run was stopped."
_ENDED_BY_MACHINE = (
    "The machine ended the tool (a signal the sandbox did not send): memory is the likely cause."
)
_NO_ANSWER = "The tool ended without an answer."

# What the program reads when the core's answer cannot be given to it as it is.
_ANSWER_TOO_BIG: JsonObject = {"refused": "too_big"}
_ANSWER_NOT_VALID: JsonObject = {"refused": "not_valid"}
# How the last line begins (the bootstrap writes it): a line too long that begins otherwise is a
# request too big.
_LAST_LINE_STARTS = (b'{"ok"', b'{"error"')

_taken_accounts: set[int] = set()

# The interpreter itself, not the virtual environment's launcher: on some systems that launcher
# is a separate process, which a kill would leave the real one behind.
_INTERPRETER: str = cast("str", getattr(sys, "_base_executable", sys.executable))


def _one_line(text: str) -> str:
    line = _PATH.sub("<path>", text.strip().splitlines()[-1] if text.strip() else "")
    return line[:_ERROR_MAX] or "The tool ended with an error."


def _kill(process: asyncio.subprocess.Process) -> None:
    """Try to kill the child and its group. Whether it worked is checked afterwards, not here."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        if sys.platform == "win32":
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)


async def _clear_account(uid: int) -> bool:
    """Kill every process of ``uid`` (a child that left the group included), then remove what it
    left in the IPC namespace (shared memory, semaphores, message queues, which outlive a process
    and which a later run under another account could read), and confirm both."""
    deadline = time.monotonic() + _REAP_SECONDS
    while True:
        await limits.kill_account(uid)
        if not limits.account_has_processes(uid):
            break
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.05)
    await limits.clear_ipc(uid)
    if limits.account_has_ipc(uid):
        logger.error("a run left IPC objects that could not be removed (account %s)", uid)
        return False
    return True


async def _end(process: asyncio.subprocess.Process, uid: int | None) -> bool:
    """End the run for good; True only when nothing of it is left alive.

    The child and its group are killed, the pipe is emptied (a process is not finished while it
    holds data) and the child is waited for. When the run had an account of its own, every
    process of the account is killed too, so a child that left the group cannot outlive the run,
    and what the account left in the IPC namespace is removed (``_clear_account``).
    """
    _kill(process)
    cleared = uid is None or await _clear_account(uid)
    stdout = process.stdout
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(_REAP_SECONDS):
            while stdout is not None and await stdout.read(65536):
                pass
            await process.wait()
    gone = process.returncode is not None and cleared
    if not gone:
        logger.error("a run could not be ended (account %s): it is kept out of use", uid)
    return gone


async def _feed(process: asyncio.subprocess.Process, data: bytes) -> None:
    """Write the first line (code, entry, module) on the child's standard input, which stays open
    for the answers."""
    stdin = process.stdin
    if stdin is None:
        return
    with contextlib.suppress(OSError):  # the child may be gone already
        stdin.write(data)
        await stdin.drain()


type _LineKind = Literal["line", "last", "too_long"]
# How a run ends: with a status, or with its last line (``answer``), read once it is over.
type _Ending = RunStatus | Literal["answer"]


class _Lines:
    """What the child writes on its answer descriptor, one line at a time, never more than ``cap``
    bytes of one.

    The pipe is read by one task at a time, whose chunk goes to the buffer when it comes: a wait
    that is cancelled loses nothing, and the end of the run can halt the reading (``halt``)
    before it empties the pipe itself, even while a ``next`` waits."""

    def __init__(self, stream: asyncio.StreamReader | None) -> None:
        self._stream = stream
        self._buffer = bytearray()
        self._closed = stream is None
        self._reading: asyncio.Future[bytes] | None = None

    async def next(self, cap: int) -> tuple[bytes, _LineKind]:
        """A line without its end (``line``); what the child left when it closed the descriptor
        (``last``, maybe empty); or the first ``cap + 1`` bytes of a line longer than ``cap``
        (``too_long``)."""
        while True:
            end = self._buffer.find(b"\n")
            if 0 <= end <= cap:
                line = bytes(self._buffer[:end])
                del self._buffer[: end + 1]
                return line, "line"
            if len(self._buffer) > cap:
                return bytes(self._buffer[: cap + 1]), "too_long"
            if self._closed:
                last = bytes(self._buffer)
                self._buffer.clear()
                return last, "last"
            await self._more()

    async def halt(self) -> None:
        """Read no more: what waits in ``next`` gets what was read so far as the last line."""
        self._closed = True
        reading = self._reading
        if reading is not None:
            reading.cancel()
            await asyncio.wait({reading})

    async def _more(self) -> None:
        stream = self._stream
        if self._reading is None and stream is not None:
            self._reading = asyncio.ensure_future(stream.read(65536))
            self._reading.add_done_callback(self._took)
        if self._reading is not None:
            await asyncio.wait({self._reading})

    def _took(self, reading: "asyncio.Future[bytes]") -> None:
        self._reading = None
        if reading.cancelled() or reading.exception() is not None:
            self._closed = True
            return
        chunk = reading.result()
        if chunk:
            self._buffer += chunk
        else:
            self._closed = True


def _parsed(data: bytes) -> object:
    """``data`` as JSON, or ``None`` when it is none (too deep to read, a number that is no
    JSON, no JSON at all)."""
    try:
        return cast("object", json.loads(data, parse_constant=_no_constant))
    except (ValueError, RecursionError):
        return None


def _ask_of(message: dict[str, object], size: int) -> JsonObject | str:
    """The request a line holds, or why it is not one: exactly ``{"ask": {"do": <a function of
    the module>, ...}}``, ``MAX_ASK_BYTES`` at most, and one that can be given to the core."""
    if size > MAX_ASK_BYTES:
        return _ASK_TOO_BIG
    ask = message.get("ask")
    if set(message) != {"ask"} or not isinstance(ask, dict):
        return _ASK_NOT_VALID
    fields = cast("JsonObject", ask)
    if fields.get("do") not in PROGRAM_FUNCTIONS:
        return _ASK_NOT_VALID
    try:
        RunAsk(n=1, ask=fields).model_dump_json()
    except (ValueError, RecursionError):
        return _ASK_NOT_VALID
    return fields


def _answer_line(answer: JsonObject, most: int) -> bytes:
    """The core's answer as the line the program reads: one it can read, ``most`` bytes at most
    (the run's ``answer_bytes``, never less than ``_REFUSAL_ROOM``) besides its envelope, or a
    refusal that says why it is not. Weighed in UTF-8, as the core weighs rows and results
    (``ensure_ascii=False``): what passed there passes here (the core refuses a heavier answer
    itself, naming the setting: this is only the channel's guard)."""
    try:
        line = json.dumps(answer, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, RecursionError):
        line = json.dumps(_ANSWER_NOT_VALID).encode("utf-8")
    if len(line) > max(most, _REFUSAL_ROOM) + _ENVELOPE_ROOM:
        line = json.dumps(_ANSWER_TOO_BIG).encode("utf-8")
    return line + b"\n"


def _account_options(uid: int) -> dict[str, Any]:
    return {"user": uid, "group": uid, "extra_groups": []}


def _spawn_options(request: RunRequest, uid: int | None) -> dict[str, Any]:
    options: dict[str, Any] = {}
    if sys.platform == "win32":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    if uid is not None:
        options.update(_account_options(uid))
    setup = limits.child_setup(
        max_seconds=request.max_seconds, memory_mb=request.memory_mb, own_account=uid is not None
    )
    if setup is not None:
        options["preexec_fn"] = setup
    return options


class LiveRun:
    """One running program and its channel: its requests (``events``, or ``next_event``), the
    answers to them (``answer``), its end.

    The run's clock (``max_seconds``) counts the time the program works, not the time the core
    takes to answer it: the core's own wait is bounded by the one who holds the run (``Runs``).
    The run ends once, whoever ends it (its last line, its clock, a request that is not one, a
    stop): the end is shared by all who wait on it, and it goes on when one of them is cancelled.
    """

    def __init__(
        self,
        request: RunRequest,
        process: asyncio.subprocess.Process,
        uid: int | None,
        started: float,
    ) -> None:
        self._request = request
        self._process = process
        self._uid = uid
        self._started = started
        # The core's figure (Domo's ``run_seconds``), past which no clock goes.
        self._budget = float(min(request.max_seconds, LONGEST_SECONDS))
        # A line may be a request or the last answer, whichever is longer (``output_bytes`` is
        # Domo's ``output_bytes``; the launcher took it as one its machine can hold).
        self._cap = max(MAX_ASK_BYTES, request.output_bytes + _ENVELOPE_ROOM)
        self._lines = _Lines(process.stdout)
        self._asks = 0
        self._undrained = False
        self._feeder: asyncio.Task[None] | None = None
        self._ending: asyncio.Task[RunResult] | None = None
        self._when_ended: list[Callable[[], None]] = []

    @property
    def finished(self) -> bool:
        """Whether its end has begun."""
        return self._ending is not None

    def when_ended(self, callback: Callable[[], None]) -> None:
        """Call ``callback`` once the end is over (confirmed or not)."""
        self._when_ended.append(callback)

    async def events(self) -> AsyncIterator[RunAsk | RunEnded]:
        """The program's requests, each to be answered before the next comes, then its end."""
        while True:
            event = await self.next_event()
            yield event
            if isinstance(event, RunEnded):
                return

    async def answer(self, answer: JsonObject) -> None:
        """Give the program the answer to its last request."""
        stdin = self._process.stdin
        if stdin is None or self._ending is not None:
            return
        with contextlib.suppress(OSError):  # the child may be gone already
            stdin.write(_answer_line(answer, self._request.answer_bytes))
        self._undrained = True

    async def next_event(self) -> RunAsk | RunEnded:
        """The program's next request, or its end."""
        if self._ending is not None:
            return await self._shared_end()
        began = time.monotonic()
        try:
            async with asyncio.timeout(self._budget):
                if self._undrained:
                    self._undrained = False
                    stdin = self._process.stdin
                    if stdin is not None:
                        with contextlib.suppress(OSError):
                            await stdin.drain()
                data, kind = await self._lines.next(self._cap)
        except TimeoutError:
            return await self._finish("timeout", _OUT_OF_TIME)
        finally:
            self._budget -= time.monotonic() - began
        if self._ending is not None:  # stopped while it worked
            return await self._shared_end()
        if kind == "too_long":
            # Only the last line, the program's answer, may be longer than a request.
            if data.lstrip().startswith(_LAST_LINE_STARTS):
                return await self._finish("too_big", _TOO_BIG)
            return await self._finish("error", _ASK_TOO_BIG)
        message = _parsed(data)
        if isinstance(message, dict) and "ask" in message:
            ask = _ask_of(cast("dict[str, object]", message), len(data))
            if isinstance(ask, str):
                return await self._finish("error", ask)
            self._asks += 1
            return RunAsk(n=self._asks, ask=ask)
        # Anything else is its last line: its answer, read once the run is over.
        return await self._finish("answer", data=data)

    async def stop(self) -> RunEnded:
        """End the run now, as a timeout is: status ``stopped``."""
        return await self._finish("stopped", _STOPPED)

    def feed(self, payload: bytes) -> None:
        """Give the child its first line: the code, its entry and the module ``domo``."""
        self._feeder = asyncio.create_task(_feed(self._process, payload))

    async def _finish(
        self, status: _Ending, note: str | None = None, data: bytes = b""
    ) -> RunEnded:
        """End the run as ``status``, with the sandbox's own ``note`` (never the program's
        words), or read its answer ``data``."""
        if self._ending is None:
            self._ending = asyncio.create_task(self._conclude(status, note, data))
        return await self._shared_end()

    async def _shared_end(self) -> RunEnded:
        ending = cast("asyncio.Task[RunResult]", self._ending)
        return RunEnded(result=await asyncio.shield(ending))

    def _result(
        self,
        status: RunStatus,
        output: JsonValue | None = None,
        error: str | None = None,
        note: str | None = None,
    ) -> RunResult:
        seconds = round(time.monotonic() - self._started, 3)
        return RunResult(status=status, output=output, error=error, note=note, seconds=seconds)

    async def _conclude(self, status: _Ending, note: str | None, data: bytes) -> RunResult:
        # True only once the process and the account are confirmed gone (``_end``): a run
        # stopped anywhere else, ``_end`` included, keeps its account out of use.
        ended = False
        try:
            if self._feeder is not None:
                self._feeder.cancel()
                await asyncio.gather(self._feeder, return_exceptions=True)
            await self._lines.halt()  # ``_end`` empties the pipe itself
            ended = await _end(self._process, self._uid)
            if not ended:
                return self._result("killed", note=_NOT_ENDED)
            if status == "answer":
                code = cast("int", self._process.returncode)
                return _interpret(data, code, self._request, self._result)
            return self._result(status, note=note)
        finally:
            # An account whose processes could not all be killed stays taken: nobody gets it
            # again.
            if self._uid is not None and ended:
                _taken_accounts.discard(self._uid)
            for callback in self._when_ended:
                callback()


async def start(request: RunRequest) -> LiveRun:
    """Start ``request.code`` with its entry, inside the limits of the request: the run is live
    until its end."""
    started = time.monotonic()
    uid = limits.pick_account(_taken_accounts) if limits.can_switch_account() else None
    if uid is not None:
        _taken_accounts.add(uid)
    try:
        process = await asyncio.create_subprocess_exec(
            _INTERPRETER,
            "-I",
            "-S",
            "-c",
            _BOOTSTRAP,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={},
            **_spawn_options(request, uid),
        )
    except Exception:
        # Refused before it started (a cancellation here is not known to be so).
        if uid is not None:
            _taken_accounts.discard(uid)
        raise
    live = LiveRun(request, process, uid, started)
    try:
        first = {
            "code": request.code,
            "entry": request.entry,
            "name": PROGRAM_MODULE,
            "module": _PROGRAM_SOURCE,
        }
        live.feed(json.dumps(first).encode("utf-8") + b"\n")
    except BaseException:
        await live.stop()
        raise
    return live


async def check_runs_can_be_ended() -> None:
    """Refuse to start a launcher that could not end a run: raises ``RuntimeError``.

    Only where runs get accounts of their own (Linux, as root): start a sleeper on an account,
    with a child that leaves its group, and end it the way a run is ended.
    """
    if not limits.can_switch_account():
        return
    uid = limits.pick_account(_taken_accounts)
    code = "import os, time\nif os.fork() == 0:\n    os.setsid()\ntime.sleep(60)\n"
    process = await asyncio.create_subprocess_exec(
        _INTERPRETER,
        "-I",
        "-S",
        "-c",
        code,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        env={},
        start_new_session=True,
        **_account_options(uid),
    )
    await asyncio.sleep(0.3)  # let the child fork and leave the group
    if not await _end(process, uid):
        raise RuntimeError("the launcher cannot end a process of another account")


def _no_constant(name: str) -> object:
    """``NaN``, ``Infinity`` and ``-Infinity`` are no JSON: an answer that holds one is not
    valid."""
    raise ValueError(f"{name} is not a JSON number")


def _interpret(data: bytes, code: int, request: RunRequest, result: Any) -> RunResult:
    """What the child's answer comes to. Whatever it holds (a nesting too deep to read or to give
    back, a number that is no JSON), it ends as a ``RunResult``, never as an error of the
    launcher. Only the error the program said is its ``error``; how the sandbox saw it end is its
    ``note``. A child that died of a signal without answering was ended by the machine (the
    sandbox sends none here: its own kills end a run by ``timeout`` or ``stopped``)."""
    if not data:
        if code < 0:
            return result("ended_by_machine", note=_ENDED_BY_MACHINE)
        return result("error", note=_NO_ANSWER)
    try:
        message = cast("object", json.loads(data, parse_constant=_no_constant))
    except (ValueError, RecursionError):
        return result("error", note=_NOT_VALID)
    if not isinstance(message, dict):
        return result("error", note=_NOT_VALID)
    fields = cast("dict[str, JsonValue]", message)
    if "error" in fields:
        return result("error", error=_one_line(str(fields["error"])))
    if "ok" not in fields:
        return result("error", note=_NOT_VALID)
    output = fields["ok"]
    try:
        # Weighed in UTF-8, as the core weighs a result (``ensure_ascii=False``).
        weight = len(json.dumps(output, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        if weight > request.output_bytes:
            return result("too_big", note=_TOO_BIG)
        done: RunResult = result("done", output=output)
        # What the launcher sends back must be written as JSON too.
        done.model_dump_json()
    except (ValueError, RecursionError):
        return result("error", note=_NOT_VALID)
    return done
