"""The core's client of the sandbox launcher, and the double the other tests use.

The client is tried against the launcher's own application (``domo_sandbox.launcher``, a
workspace package that the core never depends on at run time: its tests only), mounted on an
in-process transport. Its runs are real child processes (``domo_sandbox.runner``), led one
request at a time over the run's channel.
"""

import asyncio
import json
import logging
import socket
from pathlib import Path

import httpx
import pytest
from domo_common.sandbox_api import (
    LONGEST_SECONDS,
    JsonObject,
    Machine,
    RunEnded,
    RunOpened,
    RunRequest,
    RunResult,
    ScanFinding,
)
from domo_core import sandbox_client
from domo_core.sandbox_client import (
    NO_SANDBOX,
    REFUSE_ALL,
    RUN_MARGIN_S,
    SandboxBusy,
    SandboxClient,
    SandboxNotHere,
    SandboxUnavailable,
    new_http_client,
)
from domo_core.web.deps import get_sandbox, sandbox_of
from domo_sandbox import runs
from domo_sandbox.launcher import WAITS_HEADER, create_app
from domo_sandbox.limits import MachineHas
from fakes import FakeAsk, FakeSandbox
from fastapi import FastAPI
from pydantic import JsonValue

CODE = "def run(entry):\n    return entry\n"
READ = 'import domo\n\ndef run(entry):\n    return {"got": domo.read("SELECT 1")}\n'


def request(**overrides: object) -> RunRequest:
    return RunRequest(code=CODE, entry={"a": 1}, **overrides)  # type: ignore[arg-type]


def client_of(transport: httpx.AsyncBaseTransport) -> SandboxClient:
    return SandboxClient(new_http_client(Path("/nowhere.sock"), transport=transport))


def launcher(seen: list[httpx.Request] | None = None, *, slots: int = 2) -> SandboxClient:
    """A client on the real launcher application, whose runs are real programs."""
    transport = httpx.ASGITransport(app=create_app(slots=slots))
    if seen is None:
        return client_of(transport)

    async def record(http_request: httpx.Request) -> httpx.Response:
        seen.append(http_request)
        return await transport.handle_async_request(http_request)

    return client_of(httpx.MockTransport(record))


def answering(*replies: JsonObject | None) -> tuple[list[JsonObject], sandbox_client.Answerer]:
    """An answerer that gives ``replies`` in turn, and the asks it was given."""
    asked: list[JsonObject] = []
    left = list(replies)

    async def answer(ask: JsonObject) -> JsonObject | None:
        asked.append(ask)
        return left.pop(0)

    return asked, answer


# --- a run over the channel -------------------------------------------------------------------


async def test_a_run_answers_each_ask_then_ends():
    asked, answer = answering({"ok": [[1]]})
    result = await launcher().run(RunRequest(code=READ, entry={}), answer=answer)
    assert asked == [{"do": "read", "sql": "SELECT 1", "params": [], "space": "common"}]
    assert (result.status, result.output) == ("done", {"got": [[1]]})


async def test_a_run_without_an_ask_round_trips_through_the_launcher():
    asked, answer = answering()
    result = await launcher().run(request(), answer=answer)
    assert (result.status, result.output, result.error) == ("done", {"a": 1}, None)
    assert asked == []


async def test_refuse_all_refuses_every_ask():
    code = (
        "import domo\n\n"
        "def run(entry):\n"
        "    try:\n"
        '        domo.read("SELECT 1")\n'
        "    except domo.Refused as refused:\n"
        "        return refused.reason\n"
    )
    result = await launcher().run(RunRequest(code=code, entry={}), answer=REFUSE_ALL)
    assert (result.status, result.output) == ("done", "not_allowed")
    assert await REFUSE_ALL({"do": "read"}) == {"refused": "not_allowed"}


async def test_an_answer_of_none_stops_the_run():
    seen: list[httpx.Request] = []
    asked, answer = answering(None)
    result = await launcher(seen).run(RunRequest(code=READ, entry={}), answer=answer)
    assert result.status == "stopped"
    assert len(asked) == 1
    assert seen[-1].url.path.endswith("/stop")


async def test_a_cancelled_caller_stops_the_run():
    seen: list[httpx.Request] = []
    asking = asyncio.Event()

    async def never(ask: JsonObject) -> JsonObject | None:
        asking.set()
        await asyncio.Event().wait()
        return None

    app = create_app(slots=1)
    transport = httpx.ASGITransport(app=app)

    async def record(http_request: httpx.Request) -> httpx.Response:
        seen.append(http_request)
        return await transport.handle_async_request(http_request)

    client = client_of(httpx.MockTransport(record))
    task = asyncio.create_task(client.run(RunRequest(code=READ, entry={}), answer=never))
    await asking.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert seen[-1].url.path.endswith("/stop")
    # The run is gone, and its place with it: the only place is free again.
    async with httpx.AsyncClient(transport=transport, base_url="http://sandbox") as direct:
        next_path = seen[-1].url.path.removesuffix("/stop") + "/next"
        assert (await direct.post(next_path, json={})).status_code == 404
    assert (await client.run(request(wait_for_a_place=False), answer=REFUSE_ALL)).status == "done"


async def test_an_answerer_that_fails_stops_the_run_and_raises():
    seen: list[httpx.Request] = []

    async def broken(ask: JsonObject) -> JsonObject | None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await launcher(seen).run(RunRequest(code=READ, entry={}), answer=broken)
    assert seen[-1].url.path.endswith("/stop")


async def test_busy_is_raised_without_a_place():
    holding = asyncio.Event()
    release = asyncio.Event()

    async def hold(ask: JsonObject) -> JsonObject | None:
        holding.set()
        await release.wait()
        return {"ok": 1}

    client = launcher(slots=1)
    first = asyncio.create_task(client.run(RunRequest(code=READ, entry={}), answer=hold))
    await holding.wait()
    with pytest.raises(SandboxBusy):
        await client.run(request(wait_for_a_place=False), answer=REFUSE_ALL)
    release.set()
    assert (await first).status == "done"
    # Once the place is free, a run that may not wait gets it.
    assert (await client.run(request(wait_for_a_place=False), answer=REFUSE_ALL)).status == "done"


async def test_a_run_a_turn_waits_on_carries_the_header_and_another_does_not():
    seen: list[httpx.Request] = []
    client = launcher(seen)
    await client.run(request(), answer=REFUSE_ALL, waited_by_a_turn=True)
    turn = [r.headers for r in seen if r.url.path == "/runs"]
    seen.clear()
    await client.run(request(), answer=REFUSE_ALL)
    other = [r.headers for r in seen if r.url.path == "/runs"]
    assert turn[0][WAITS_HEADER] == "turn"
    assert WAITS_HEADER not in other[0]


async def test_the_transport_waits_for_the_run_and_a_margin():
    timeouts: dict[str, httpx.Timeout] = {}

    async def answer(http_request: httpx.Request) -> httpx.Response:
        path = http_request.url.path
        timeouts[path.rsplit("/", 1)[-1]] = httpx.Timeout(**http_request.extensions["timeout"])
        if path == "/runs":
            return httpx.Response(200, json=RunOpened(run="r").model_dump())
        ended = RunEnded(result=RunResult(status="done", seconds=0.0))
        return httpx.Response(200, json=ended.model_dump())

    await client_of(httpx.MockTransport(answer)).run(request(max_seconds=120), answer=REFUSE_ALL)
    assert timeouts["runs"].read == 120 + RUN_MARGIN_S
    assert timeouts["next"].read == 120 + RUN_MARGIN_S
    # A time past any clock: the transport waits as long as a clock goes, never an error.
    timeouts.clear()
    huge = request(max_seconds=10**400)
    await client_of(httpx.MockTransport(answer)).run(huge, answer=REFUSE_ALL)
    assert timeouts["runs"].read == LONGEST_SECONDS + RUN_MARGIN_S


# --- the launcher's bounds are the core's ---------------------------------------------------------


def on_a_machine(memory_mb: int | None, processors: int | None = 2, slots: int = 2):
    """A client on the real launcher, whose machine has ``memory_mb`` and ``processors``."""
    has = MachineHas(memory_mb=memory_mb, processors=processors)
    return client_of(httpx.ASGITransport(app=create_app(slots=slots, machine=lambda: has)))


async def test_a_run_the_machine_cannot_give_is_not_here_with_what_it_has():
    client = on_a_machine(1024)
    with pytest.raises(SandboxNotHere) as refused:
        await client.run(request(memory_mb=2048), answer=REFUSE_ALL)
    assert refused.value.beyond == ["memory_mb"]
    assert refused.value.has == Machine(memory_mb=1024, processors=2, places=2)
    with pytest.raises(SandboxNotHere) as output:
        await client.run(request(output_bytes=2 * 1024**3), answer=REFUSE_ALL)
    assert output.value.beyond == ["output_bytes"]
    # What it has, it gives; a time of more than an hour is no machine's matter.
    ran = await client.run(request(memory_mb=1024, max_seconds=5000), answer=REFUSE_ALL)
    assert ran.status == "done"


async def test_the_places_are_set_and_the_machine_read_through_the_launcher():
    client = on_a_machine(4096, processors=8, slots=4)
    assert await client.machine() == Machine(memory_mb=4096, processors=8, places=4)
    await client.set_places(1)
    assert (await client.machine()).places == 1
    with pytest.raises(ValueError):
        await client.set_places(0)


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(409, json={"detail": "no free place"}),
        httpx.Response(409, json={"refused": "not_here"}),
        httpx.Response(409, text="busy"),
    ],
)
async def test_a_refusal_that_is_not_one_fails_closed(answer: httpx.Response):
    client = client_of(httpx.MockTransport(lambda _: answer))
    with pytest.raises(SandboxUnavailable):
        await client.run(request(wait_for_a_place=False), answer=REFUSE_ALL)


async def test_a_launcher_that_does_not_answer_sets_no_place_and_says_no_machine():
    def refuse(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client = client_of(httpx.MockTransport(refuse))
    with pytest.raises(SandboxUnavailable):
        await client.set_places(3)
    with pytest.raises(SandboxUnavailable):
        await client.machine()
    with pytest.raises(SandboxUnavailable):
        await NO_SANDBOX.set_places(3)
    with pytest.raises(SandboxUnavailable):
        await NO_SANDBOX.machine()


# --- keeping a run alive while the core answers ------------------------------------------------


async def test_a_run_whose_answer_takes_longer_than_the_idle_time_is_kept_alive(
    monkeypatch: pytest.MonkeyPatch,
):
    """An answer may take minutes (a tool that launches another): the core keeps the run alive
    while it works on it."""
    monkeypatch.setattr(runs, "IDLE_SECONDS", 1)
    monkeypatch.setattr(sandbox_client, "ALIVE_EVERY_SECONDS", 0.3)
    seen: list[httpx.Request] = []

    async def slow(ask: JsonObject) -> JsonObject | None:
        await asyncio.sleep(2.5)
        return {"ok": 7}

    result = await launcher(seen).run(RunRequest(code=READ, entry={}), answer=slow)
    assert (result.status, result.output) == ("done", {"got": 7})
    assert any(r.url.path.endswith("/alive") for r in seen)


async def test_a_run_nobody_keeps_alive_still_dies(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(runs, "IDLE_SECONDS", 1)
    monkeypatch.setattr(sandbox_client, "ALIVE_EVERY_SECONDS", 100)

    async def slow(ask: JsonObject) -> JsonObject | None:
        await asyncio.sleep(2.5)
        return {"ok": 7}

    with pytest.raises(SandboxUnavailable):
        await launcher().run(RunRequest(code=READ, entry={}), answer=slow)


# --- failures ----------------------------------------------------------------------------------


async def test_a_refused_connection_is_unavailable(caplog: pytest.LogCaptureFixture):
    def refuse(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client = client_of(httpx.MockTransport(refuse))
    with caplog.at_level(logging.WARNING), pytest.raises(SandboxUnavailable):
        await client.run(request(), answer=REFUSE_ALL)
    with pytest.raises(SandboxUnavailable):
        await client.scan(CODE)
    assert CODE not in caplog.text


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Unix sockets: the sandbox is Linux's")
async def test_a_socket_nobody_listens_on_is_unavailable_and_building_the_client_touches_nothing(
    tmp_path: Path,
):
    http = new_http_client(tmp_path / "no-launcher.sock")
    client = SandboxClient(http)
    with pytest.raises(SandboxUnavailable):
        await client.run(request(), answer=REFUSE_ALL)
    with pytest.raises(SandboxUnavailable):
        await client.scan(CODE)
    await http.aclose()


async def test_a_system_without_unix_sockets_has_a_launcher_that_is_down(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    # A developer's Windows: the upkeep gives the places at each round, and it must fail as a
    # launcher that is down, never otherwise.
    monkeypatch.delattr(sandbox_client._socket, "AF_UNIX", raising=False)  # pyright: ignore[reportPrivateUsage]
    http = new_http_client(tmp_path / "launcher.sock")
    with pytest.raises(SandboxUnavailable):
        await SandboxClient(http).set_places(2)
    await http.aclose()


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(500, text="boom"),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"status": "done"}),
        httpx.Response(200, json={"findings": "none"}),
        httpx.Response(404, json={"detail": "no"}),
    ],
)
async def test_an_answer_that_is_not_a_result_fails_closed(answer: httpx.Response):
    client = client_of(httpx.MockTransport(lambda _: answer))
    with pytest.raises(SandboxUnavailable):
        await client.run(request(), answer=REFUSE_ALL)
    with pytest.raises(SandboxUnavailable):
        await client.scan(CODE)


@pytest.mark.parametrize(
    "event",
    [
        {"kind": "end"},
        {"kind": "ask", "n": 1},
        {"kind": "other", "result": {"status": "done", "seconds": 0}},
        {"kind": "end", "result": {"status": "fine", "seconds": 0}},
    ],
)
async def test_a_next_that_is_not_an_event_fails_closed_and_stops_the_run(event: object):
    stopped: list[str] = []

    async def launcher_of(http_request: httpx.Request) -> httpx.Response:
        path = http_request.url.path
        if path == "/runs":
            return httpx.Response(200, json={"run": "r"})
        if path.endswith("/stop"):
            stopped.append(path)
            ended = RunEnded(result=RunResult(status="stopped", seconds=0.0))
            return httpx.Response(200, json=ended.model_dump())
        return httpx.Response(200, content=json.dumps(event).encode())

    with pytest.raises(SandboxUnavailable):
        await client_of(httpx.MockTransport(launcher_of)).run(request(), answer=REFUSE_ALL)
    assert stopped == ["/runs/r/stop"]


async def test_a_transport_that_times_out_is_unavailable():
    def slow(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow")

    client = client_of(httpx.MockTransport(slow))
    with pytest.raises(SandboxUnavailable):
        await client.run(request(), answer=REFUSE_ALL)
    with pytest.raises(SandboxUnavailable):
        await client.scan(CODE)


async def test_a_scan_round_trips_through_the_launcher():
    findings = (await launcher().scan("import subprocess\nsubprocess.run(['ls'])\n")).findings
    assert findings
    assert all(isinstance(finding, ScanFinding) for finding in findings)
    assert (await launcher().scan(CODE)).findings == []


async def test_no_sandbox_is_always_down():
    with pytest.raises(SandboxUnavailable):
        await NO_SANDBOX.run(request(), answer=REFUSE_ALL)
    with pytest.raises(SandboxUnavailable):
        await NO_SANDBOX.scan(CODE)


# --- the double ----------------------------------------------------------------------------------


async def test_the_fake_executes_nothing_and_notes_what_it_was_asked():
    fake = FakeSandbox()
    assert await fake.run(request(), answer=REFUSE_ALL) == RunResult(
        status="done", output=None, error=None, seconds=0.0
    )
    assert (await fake.scan(CODE)).findings == []
    finding = ScanFinding(test_id="B602", severity="HIGH", line=1, text="shell")
    fake.findings.append(finding)
    assert (await fake.scan("x")).findings == [finding]
    assert [r.code for r in fake.runs] == [CODE]
    assert fake.scans == [CODE, "x"]


async def test_the_fake_answers_with_the_function_it_is_given():
    fake = FakeSandbox(lambda r: RunResult(status="error", error="boom", seconds=1.0))
    assert (await fake.run(request(), answer=REFUSE_ALL)).status == "error"


async def test_the_fake_s_program_asks_through_the_answerer():
    async def program(run_request: RunRequest, ask: FakeAsk) -> JsonValue:
        return {"got": await ask({"do": "read", "sql": "SELECT 1"})}

    fake = FakeSandbox(program=program)
    _, answer = answering({"ok": 3})
    result = await fake.run(request(), answer=answer)
    assert (result.status, result.output) == ("done", {"got": 3})
    assert fake.asks == [{"do": "read", "sql": "SELECT 1"}]


async def test_the_fake_s_program_meets_a_refusal_and_a_stop():
    async def program(run_request: RunRequest, ask: FakeAsk) -> JsonValue:
        return await ask({"do": "read"})

    fake = FakeSandbox(program=program)
    refused = await fake.run(request(), answer=REFUSE_ALL)
    assert (refused.status, refused.error) == ("error", "domo.Refused: not_allowed")
    _, stop = answering(None)
    assert (await fake.run(request(), answer=stop)).status == "stopped"


async def test_the_fake_is_busy_only_for_a_run_that_may_not_wait():
    fake = FakeSandbox()
    fake.busy = True
    with pytest.raises(SandboxBusy):
        await fake.run(request(wait_for_a_place=False), answer=REFUSE_ALL)
    assert fake.runs == []
    assert (await fake.run(request(), answer=REFUSE_ALL)).status == "done"


async def test_the_fake_takes_places_says_its_machine_and_refuses_what_it_has_not():
    fake = FakeSandbox()
    await fake.set_places(6)
    assert fake.places_told == [6]
    assert await fake.machine() == Machine(memory_mb=None, processors=None, places=6)
    fake.has = Machine(memory_mb=512, processors=1, places=6)
    with pytest.raises(SandboxNotHere) as refused:
        await fake.run(request(memory_mb=1024), answer=REFUSE_ALL)
    assert (refused.value.beyond, refused.value.has) == (["memory_mb"], fake.has)
    assert fake.runs == []
    fake.has = None
    with pytest.raises(SandboxUnavailable):
        await fake.machine()
    with pytest.raises(SandboxUnavailable):
        await fake.set_places(2)


def test_an_application_without_a_lifespan_has_a_sandbox_that_is_down_unless_overridden():
    app = FastAPI()
    assert sandbox_of(app) is NO_SANDBOX
    fake = FakeSandbox()
    app.dependency_overrides[get_sandbox] = lambda: fake
    assert sandbox_of(app) is fake
