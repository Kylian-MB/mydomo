import asyncio
import os
import stat
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, Literal, cast

import httpx
import pytest
from bandit.core.manager import BanditManager
from domo_common.sandbox_api import RunEnded, RunRequest, RunResult
from domo_sandbox import __main__ as sandbox_main
from domo_sandbox.__main__ import _core_gid, bind_socket
from domo_sandbox.launcher import create_app
from domo_sandbox.limits import MachineHas
from domo_sandbox.runs import Starter
from fastapi import FastAPI


def client_for(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sandbox")


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with client_for(create_app()) as client:
        yield client


def test_a_launcher_that_is_not_root_starts_only_when_told_so(monkeypatch: pytest.MonkeyPatch):
    # Final review m6: unconfined runs are never the default.
    monkeypatch.setattr(sandbox_main.limits, "can_switch_account", lambda: False)
    monkeypatch.delenv("DOMO_SANDBOX_UNCONFINED", raising=False)
    with pytest.raises(SystemExit) as stopped:
        sandbox_main.main()
    assert "DOMO_SANDBOX_UNCONFINED=yes" in str(stopped.value)
    monkeypatch.setenv("DOMO_SANDBOX_UNCONFINED", "yes")
    sandbox_main._confined_or_told()  # pyright: ignore[reportPrivateUsage]


async def test_health(client: httpx.AsyncClient):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def ran(client: httpx.AsyncClient, code: str, entry: dict[str, object]) -> dict[str, Any]:
    """Open a run of ``code`` and lead it to its end (it asks nothing): its result."""
    opened = await client.post("/runs", json={"code": code, "entry": entry})
    assert opened.status_code == 200, opened.text
    end = await client.post(f"/runs/{opened.json()['run']}/next", json={})
    assert end.status_code == 200, end.text
    assert end.json()["kind"] == "end"
    return end.json()["result"]


async def test_a_run_round_trips(client: httpx.AsyncClient):
    result = await ran(client, "def run(entry):\n    return entry['n'] * 2\n", {"n": 21})
    assert (result["status"], result["output"]) == ("done", 42)


async def test_the_old_route_is_gone(client: httpx.AsyncClient):
    response = await client.post("/run", json={"code": "def run(entry):\n    return 1\n"})
    assert response.status_code == 404


@pytest.mark.parametrize(
    "answer", ['{"ok": ' + "[" * 600 + "]" * 600 + "}", '{"ok": NaN}'], ids=["deep", "nan"]
)
async def test_an_answer_that_cannot_be_given_back_is_no_failure_of_the_launcher(
    client: httpx.AsyncClient, answer: str
):
    code = f"import os\n\ndef run(entry):\n    os.write(3, {answer.encode()!r})\n    os._exit(0)\n"
    result = await ran(client, code, {})
    assert (result["status"], result["output"]) == ("error", None)


async def test_alive_is_204_for_a_run_and_404_for_none(client: httpx.AsyncClient):
    code = 'import domo\n\ndef run(entry):\n    return domo.read("SELECT 1")\n'
    opened = await client.post("/runs", json={"code": code, "entry": {}})
    run = opened.json()["run"]
    await client.post(f"/runs/{run}/next", json={})
    alive = await client.post(f"/runs/{run}/alive")
    assert (alive.status_code, alive.content) == (204, b"")
    assert (await client.post(f"/runs/{run}/stop")).status_code == 200
    assert (await client.post(f"/runs/{run}/alive")).status_code == 404
    assert (await client.post("/runs/nobody/alive")).status_code == 404


async def test_a_scan_finds_a_shell_call(client: httpx.AsyncClient):
    code = 'import subprocess; subprocess.run("ls", shell=True)\n'
    response = await client.post("/scan", json={"code": code})
    assert response.status_code == 200
    findings = response.json()["findings"]
    assert findings
    assert {"B404", "B602"} & {finding["test_id"] for finding in findings}


async def test_a_scan_cannot_be_silenced_by_nosec(client: httpx.AsyncClient):
    code = 'import subprocess  # nosec\nsubprocess.run("ls", shell=True)  # nosec\n'
    response = await client.post("/scan", json={"code": code})
    assert response.json()["findings"]


async def test_a_clean_code_has_no_finding(client: httpx.AsyncClient):
    response = await client.post("/scan", json={"code": "def run(entry):\n    return 1\n"})
    assert response.json() == {"findings": []}


async def test_a_code_that_does_not_parse_is_a_finding(client: httpx.AsyncClient):
    response = await client.post("/scan", json={"code": "def run(:\n"})
    assert [finding["test_id"] for finding in response.json()["findings"]] == ["B000"]


class FakeLive:
    """A run that executes nothing: its program ends after ``seconds``, or when stopped."""

    def __init__(self, seconds: float, on_end: Callable[[], None]) -> None:
        self._seconds = seconds
        self._callbacks = [on_end]
        self._end: RunEnded | None = None

    def when_ended(self, callback: Callable[[], None]) -> None:
        self._callbacks.append(callback)

    async def next_event(self) -> RunEnded:
        await asyncio.sleep(self._seconds)
        return self._ended("done")

    async def stop(self) -> RunEnded:
        return self._ended("stopped")

    def _ended(self, status: Literal["done", "stopped"]) -> RunEnded:
        if self._end is None:
            self._end = RunEnded(result=RunResult(status=status, seconds=0.0))
            for callback in self._callbacks:
                callback()
        return self._end


async def test_a_request_a_turn_waits_on_is_served_first():
    order: list[str] = []

    async def fake_start(request: RunRequest) -> FakeLive:
        order.append(request.code)
        return FakeLive(3600, lambda: None)

    async with client_for(create_app(slots=1, start=cast(Starter, fake_start))) as client:

        def post(name: str, waits: str | None = None) -> "asyncio.Task[httpx.Response]":
            headers = {"x-domo-waits": waits} if waits else {}
            return asyncio.create_task(
                client.post("/runs", json={"code": name, "entry": {}}, headers=headers)
            )

        first = await post("first")
        other = post("other")
        await asyncio.sleep(0.05)
        turn = post("turn", "turn")
        await asyncio.sleep(0.05)
        assert order == ["first"]
        await client.post(f"/runs/{first.json()['run']}/stop")
        opened = await turn
        await asyncio.sleep(0.05)
        assert order == ["first", "turn"]
        await client.post(f"/runs/{opened.json()['run']}/stop")
        last = await other
        await client.post(f"/runs/{last.json()['run']}/stop")

    assert order == ["first", "turn", "other"]


async def test_at_most_the_slots_run_at_once():
    running = peak = 0

    def ended() -> None:
        nonlocal running
        running -= 1

    async def fake_start(request: RunRequest) -> FakeLive:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        return FakeLive(0.05, ended)

    async def one(client: httpx.AsyncClient) -> None:
        opened = await client.post("/runs", json={"code": "c", "entry": {}})
        await client.post(f"/runs/{opened.json()['run']}/next", json={})

    async with client_for(create_app(slots=2, start=cast(Starter, fake_start))) as client:
        await asyncio.gather(*(one(client) for _ in range(6)))
    assert peak == 2


# --- the bounds are the core's: the launcher refuses only what its machine cannot give ------------


def has(memory_mb: int | None = 1024, processors: int | None = 2) -> Callable[[], MachineHas]:
    return lambda: MachineHas(memory_mb=memory_mb, processors=processors)


def counting_start(started: list[str], seconds: float = 3600) -> Starter:
    async def fake_start(request: RunRequest) -> FakeLive:
        started.append(request.code)
        return FakeLive(seconds, lambda: None)

    return cast(Starter, fake_start)


async def test_a_run_of_more_than_an_hour_is_accepted(client: httpx.AsyncClient):
    # Spec 12.1: the time of a launch is Domo's setting; no ceiling of the launcher above it.
    assert RunRequest(code="", entry={}, max_seconds=5000).max_seconds == 5000
    code = "def run(entry):\n    return 1\n"
    for seconds in (3601, 10**12, 10**400):
        opened = await client.post(
            "/runs", json={"code": code, "entry": {}, "max_seconds": seconds}
        )
        assert opened.status_code == 200, opened.text
        end = await client.post(f"/runs/{opened.json()['run']}/next", json={})
        assert end.json()["result"]["status"] == "done"


async def test_memory_the_machine_does_not_have_is_refused_with_what_it_has():
    started: list[str] = []
    app = create_app(slots=1, start=counting_start(started, 0), machine=has(memory_mb=1024))
    async with client_for(app) as client:
        refused = await client.post("/runs", json={"code": "big", "entry": {}, "memory_mb": 1025})
        assert refused.status_code == 409
        assert refused.json() == {
            "refused": "not_here",
            "beyond": ["memory_mb"],
            "has": {"memory_mb": 1024, "processors": 2, "places": 1},
        }
        # An output the machine could not hold either.
        output = await client.post(
            "/runs", json={"code": "big", "entry": {}, "output_bytes": 1025 * 1024 * 1024}
        )
        assert output.json()["beyond"] == ["output_bytes"]
        assert started == []
        # Nothing took the only place: a run within what the machine has starts at once.
        body = {"code": "fits", "entry": {}, "memory_mb": 1024, "wait_for_a_place": False}
        assert (await client.post("/runs", json=body)).status_code == 200
    assert started == ["fits"]


async def test_an_output_the_launcher_could_not_hold_is_refused_well_under_its_memory():
    # Fix round 1, I1: the launcher holds a run's output itself, several times over (its buffer, a
    # copy, the value read from it, the answer written again), and once per place: an output it
    # accepts must never fill the memory it shares with its runs.
    megabyte = 1024 * 1024
    started: list[str] = []
    app = create_app(slots=1, start=counting_start(started, 0), machine=has(memory_mb=1024))
    async with client_for(app) as client:

        async def answer(output_bytes: int) -> httpx.Response:
            body = {"code": "c", "entry": {}, "output_bytes": output_bytes}
            return await client.post("/runs", json=body)

        refused = await answer(100 * megabyte)
        assert refused.status_code == 409
        assert refused.json()["beyond"] == ["output_bytes"]
        assert refused.json()["has"]["memory_mb"] == 1024
        assert (await answer(10 * megabyte)).status_code == 200
        # Two places: each may hold its output at once, so each gets half.
        await client.put("/places", json={"places": 2})
        assert (await answer(30 * megabyte)).status_code == 409
        assert (await answer(10 * megabyte)).status_code == 200
    assert started == ["c", "c"]


async def test_a_machine_that_cannot_tell_its_memory_refuses_none():
    started: list[str] = []
    app = create_app(start=counting_start(started, 0), machine=has(memory_mb=None))
    async with client_for(app) as client:
        body = {"code": "c", "entry": {}, "memory_mb": 10**9, "output_bytes": 10**15}
        assert (await client.post("/runs", json=body)).status_code == 200
    assert started == ["c"]


async def test_the_launcher_says_what_its_machine_has():
    app = create_app(slots=3, machine=has(memory_mb=2048, processors=8))
    async with client_for(app) as client:
        found = await client.get("/machine")
        assert found.status_code == 200
        assert found.json() == {"memory_mb": 2048, "processors": 8, "places": 3}


async def test_places_are_raised_and_lowered_while_it_runs():
    started: list[str] = []
    app = create_app(slots=1, start=counting_start(started), machine=has())
    async with client_for(app) as client:

        def now(name: str) -> "asyncio.Task[httpx.Response]":
            body = {"code": name, "entry": {}, "wait_for_a_place": False}
            return asyncio.create_task(client.post("/runs", json=body))

        first = await now("first")
        assert (await now("refused")).status_code == 409
        # Raised: a second place, at once.
        raised = await client.put("/places", json={"places": 2})
        assert (raised.status_code, raised.json()) == (200, {"places": 2})
        second = await now("second")
        assert second.status_code == 200
        assert (await client.get("/machine")).json()["places"] == 2
        # Lowered under what runs: both go on, and no new run starts until under the count.
        assert (await client.put("/places", json={"places": 1})).status_code == 200
        waiting = asyncio.create_task(client.post("/runs", json={"code": "waits", "entry": {}}))
        await asyncio.sleep(0.05)
        await client.post(f"/runs/{first.json()['run']}/stop")
        await asyncio.sleep(0.05)
        assert not waiting.done()
        await client.post(f"/runs/{second.json()['run']}/stop")
        last = await waiting
        assert last.status_code == 200
        await client.post(f"/runs/{last.json()['run']}/stop")
    assert started == ["first", "second", "waits"]


@pytest.mark.parametrize("places", [0, -1, "two", None])
async def test_places_are_a_whole_number_of_one_at_least(places: object):
    async with client_for(create_app(slots=2, machine=has())) as client:
        assert (await client.put("/places", json={"places": places})).status_code == 422
        assert (await client.get("/machine")).json()["places"] == 2


async def test_a_run_that_may_not_wait_is_refused_at_once_and_never_queued():
    # Review Focus 3: a nested launch finds no place; it is refused, and nothing of it starts later.
    started: list[str] = []
    app = create_app(slots=1, start=counting_start(started), machine=has())
    async with client_for(app) as client:
        holder = await client.post("/runs", json={"code": "holder", "entry": {}})
        body = {"code": "nested", "entry": {}, "wait_for_a_place": False}
        async with asyncio.timeout(1):
            refused = await client.post("/runs", json=body)
        assert (refused.status_code, refused.json()) == (409, {"refused": "busy"})
        await client.post(f"/runs/{holder.json()['run']}/stop")
        await asyncio.sleep(0.05)
    assert started == ["holder"]


async def test_a_scan_that_bandit_could_not_do_is_an_alert_not_a_clean_code(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    def skipping(self: BanditManager, name: str, data: object, files: list[str]) -> None:
        self.skipped.append((name, "exception while scanning file"))

    monkeypatch.setattr(BanditManager, "_parse_file", skipping)
    response = await client.post("/scan", json={"code": "def run(entry):\n    return 1\n"})
    findings = response.json()["findings"]
    assert [(f["test_id"], f["severity"]) for f in findings] == [("B000", "HIGH")]


async def test_a_scan_where_bandit_raises_is_an_alert(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    def raising(self: BanditManager, name: str, data: object, files: list[str]) -> None:
        raise SystemExit(2)

    monkeypatch.setattr(BanditManager, "_parse_file", raising)
    response = await client.post("/scan", json={"code": "def run(entry):\n    return 1\n"})
    assert [f["test_id"] for f in response.json()["findings"]] == ["B000"]


# --- the socket: the core's and root's alone ------------------------------------------------------


@pytest.mark.skipif(
    sys.platform != "linux" or os.geteuid() != 0,
    reason="the socket's group is set by changing the effective gid: Linux, as root (the stack "
    "tests check it in the container)",
)
def test_the_socket_is_made_root_s_and_the_core_s_group_s_mode_0660(tmp_path: Path):
    path = tmp_path / "sandbox" / "launcher.sock"
    path.parent.mkdir()
    path.touch()  # a stale one
    umask = os.umask(0o022)
    try:
        server = bind_socket(path, 12345)
        try:
            found = path.stat()
            assert stat.S_ISSOCK(found.st_mode)
            assert (found.st_uid, found.st_gid, stat.S_IMODE(found.st_mode)) == (0, 12345, 0o660)
            # The launcher's own gid and umask are given back.
            assert os.getegid() == 0
            assert os.umask(0o022) == 0o022
        finally:
            server.close()
    finally:
        os.umask(umask)


def test_the_core_s_group_is_10001_unless_told(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("DOMO_SANDBOX_CORE_GID", raising=False)
    assert _core_gid() == 10001
    monkeypatch.setenv("DOMO_SANDBOX_CORE_GID", "2000")
    assert _core_gid() == 2000
    monkeypatch.setenv("DOMO_SANDBOX_CORE_GID", "core")
    with pytest.raises(SystemExit):
        _core_gid()


@pytest.mark.parametrize("gid", ["0", "-1", "20000", "25000", "29999"])
def test_the_core_s_group_is_never_root_s_nor_a_run_s(monkeypatch: pytest.MonkeyPatch, gid: str):
    """Root's group, or a group of the runs' accounts (each run's group is its uid), would open
    the socket to a run: the launcher refuses to start."""
    monkeypatch.setenv("DOMO_SANDBOX_CORE_GID", gid)
    with pytest.raises(SystemExit):
        _core_gid()
    monkeypatch.setenv("DOMO_SANDBOX_CORE_GID", "30000")
    assert _core_gid() == 30000
