"""The live channel of a run: real child processes, the launcher's app over an in-process client."""

import ast
import asyncio
import json
import re
import time
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from domo_common.sandbox_api import PROGRAM_FUNCTIONS, RunAsk, RunRequest
from domo_sandbox import limits, runner, runs
from domo_sandbox.launcher import create_app

READ = 'import domo\n\ndef run(entry):\n    return {"got": domo.read("SELECT 1")}\n'
CAUGHT = (
    "import domo\n\n"
    "def run(entry):\n"
    "    try:\n"
    '        domo.read("SELECT 1")\n'
    "    except domo.Refused as refused:\n"
    '        return {"caught": refused.reason, "detail": refused.detail}\n'
    '    return {"caught": None}\n'
)


def client_for(slots: int = 4) -> httpx.AsyncClient:
    app = create_app(slots=slots)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sandbox")


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with client_for() as client:
        yield client


async def post(client: httpx.AsyncClient, path: str, body: object = None) -> dict[str, object]:
    response = await client.post(path, json=body)
    assert response.status_code == 200, response.text
    return response.json()


async def open_run(client: httpx.AsyncClient, code: str, **fields: object) -> str:
    opened = await post(client, "/runs", RunRequest(code=code, **fields).model_dump())  # type: ignore[arg-type]
    return str(opened["run"])


def entry(**fields: object) -> dict[str, object]:
    return {"entry": fields}


class FakeAccount:
    """Stands in for the Linux account of a run (as in ``test_runner``)."""

    def __init__(self) -> None:
        self.killed: list[int] = []

    async def kill_account(self, uid: int) -> None:
        self.killed.append(uid)

    async def clear_ipc(self, uid: int) -> None:
        return None


@pytest.fixture
def account(monkeypatch: pytest.MonkeyPatch) -> FakeAccount:
    fake = FakeAccount()
    monkeypatch.setattr(limits, "can_switch_account", lambda: True)
    # The first account nobody holds, as the real one: a run that kept its account would push
    # the next one to 20124.
    monkeypatch.setattr(limits, "pick_account", lambda taken: min(set(range(20123, 20200)) - taken))
    monkeypatch.setattr(limits, "kill_account", fake.kill_account)
    monkeypatch.setattr(limits, "account_has_processes", lambda uid: False)
    monkeypatch.setattr(limits, "clear_ipc", fake.clear_ipc)
    monkeypatch.setattr(limits, "account_has_ipc", lambda uid: False)
    monkeypatch.setattr(runner, "_account_options", lambda uid: {})
    monkeypatch.setattr(runner, "_taken_accounts", set())
    return fake


# --- a request, its answer ------------------------------------------------------------------------


async def test_a_program_asks_and_gets_its_answer(client: httpx.AsyncClient):
    run = await open_run(client, READ, entry={})
    event = await post(client, f"/runs/{run}/next", {})
    assert event["kind"] == "ask"
    assert event["n"] == 1
    assert event["ask"] == {"do": "read", "sql": "SELECT 1", "params": [], "space": "common"}
    end = await post(client, f"/runs/{run}/next", {"answer": {"ok": [[1]]}})
    assert end["kind"] == "end"
    result = end["result"]
    assert isinstance(result, dict)
    assert result["status"] == "done"
    assert result["output"] == {"got": [[1]]}
    # The run is gone once it ended.
    assert (await client.post(f"/runs/{run}/next", json={})).status_code == 404


async def test_a_refusal_raises_in_the_program(client: httpx.AsyncClient):
    run = await open_run(client, CAUGHT, entry={})
    await post(client, f"/runs/{run}/next", {})
    end = await post(client, f"/runs/{run}/next", {"answer": {"refused": "not_allowed"}})
    assert end["result"] == {
        "status": "done",
        "output": {"caught": "not_allowed", "detail": None},
        "error": None,
        "note": None,
        "seconds": end["result"]["seconds"],  # type: ignore[index]
    }


async def test_a_refusal_carries_its_detail(client: httpx.AsyncClient):
    run = await open_run(client, CAUGHT, entry={})
    await post(client, f"/runs/{run}/next", {})
    answer = {"refused": "sql_error", "detail": "42P01 no such table"}
    end = await post(client, f"/runs/{run}/next", {"answer": answer})
    output = end["result"]["output"]  # type: ignore[index]
    assert output == {"caught": "sql_error", "detail": "42P01 no such table"}


async def test_a_refusal_the_program_does_not_catch_ends_it_in_error(client: httpx.AsyncClient):
    run = await open_run(client, READ, entry={})
    await post(client, f"/runs/{run}/next", {})
    end = await post(client, f"/runs/{run}/next", {"answer": {"refused": "not_allowed"}})
    assert end["result"]["status"] == "error"  # type: ignore[index]
    assert end["result"]["error"] == "domo.Refused: not_allowed"  # type: ignore[index]


async def test_every_function_of_the_module_is_one_request(client: httpx.AsyncClient):
    code = (
        "import domo\n\n"
        "def run(entry):\n"
        '    domo.begin("order", 3250)\n'
        '    domo.read("SELECT a FROM t WHERE b = %s", [2], space="mine")\n'
        '    domo.write("DELETE FROM t")\n'
        '    domo.act("tool.launch_when", {"x": 1})\n'
        '    domo.tool("weather", {"city": "Lyon"})\n'
        '    domo.later("2026-11-19T08:00:00+00:00", {"again": True})\n'
        '    domo.wake({"says": "done"})\n'
        '    domo.out("GET", "http://192.168.1.20:8123/api")\n'
        '    domo.out("POST", "http://192.168.1.20:8123/x", {"a": "1"}, "é", account="lights")\n'
        '    return domo.out("PUT", "http://192.168.1.20:8123/y", body=b"\\xff\\x00")\n'
    )
    run = await open_run(client, code, entry={})
    asks: list[object] = []
    event = await post(client, f"/runs/{run}/next", {})
    while event["kind"] == "ask":
        asks.append(event["ask"])
        event = await post(client, f"/runs/{run}/next", {"answer": {"ok": len(asks)}})
    assert asks == [
        {"do": "begin", "operation": "order", "amount_cents": 3250},
        {"do": "read", "sql": "SELECT a FROM t WHERE b = %s", "params": [2], "space": "mine"},
        {"do": "write", "sql": "DELETE FROM t", "params": [], "space": "common"},
        {"do": "act", "action": "tool.launch_when", "arguments": {"x": 1}},
        {"do": "tool", "name": "weather", "arguments": {"city": "Lyon"}},
        {"do": "later", "at": "2026-11-19T08:00:00+00:00", "arguments": {"again": True}},
        {"do": "wake", "content": {"says": "done"}},
        {
            "do": "out",
            "method": "GET",
            "url": "http://192.168.1.20:8123/api",
            "headers": {},
            "account": None,
        },
        {
            "do": "out",
            "method": "POST",
            "url": "http://192.168.1.20:8123/x",
            "headers": {"a": "1"},
            "account": "lights",
            "body": "é",
        },
        # Bytes go in base64.
        {
            "do": "out",
            "method": "PUT",
            "url": "http://192.168.1.20:8123/y",
            "headers": {},
            "account": None,
            "body_b64": "/wA=",
        },
    ]
    assert event["result"]["output"] == 10  # type: ignore[index]


async def test_a_body_out_bigger_than_a_request_ends_the_run_as_any_request_too_big(
    client: httpx.AsyncClient,
):
    # The frame of a request is not raised for a body: it is what one request holds.
    code = (
        "import domo\n\n"
        "def run(entry):\n"
        '    return domo.out("POST", "http://192.168.1.20:8123/x", body="x" * 70_000)\n'
    )
    run = await open_run(client, code, entry={})
    end = await post(client, f"/runs/{run}/next", {})
    assert end["kind"] == "end"
    assert end["result"]["status"] == "error"  # type: ignore[index]
    assert end["result"]["note"] == "A request of the tool is bigger than allowed."  # type: ignore[index]


async def test_a_live_run_gives_its_asks_then_its_end():
    live = await runner.start(RunRequest(code=READ, entry={}))
    kinds: list[str] = []
    async for event in live.events():
        kinds.append(event.kind)
        if isinstance(event, RunAsk):
            await live.answer({"ok": event.n})
        else:
            assert (event.result.status, event.result.output) == ("done", {"got": 1})
    assert kinds == ["ask", "end"]


def test_the_module_has_exactly_the_program_functions():
    source = Path(runner.__file__).with_name("program_module.py").read_text(encoding="utf-8")
    public = {
        node.name
        for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
    }
    assert public == set(PROGRAM_FUNCTIONS)


@pytest.mark.parametrize(("given", "seen"), [({"dry_run": True}, True), ({}, False)])
async def test_the_module_says_whether_the_run_is_a_dry_run(
    client: httpx.AsyncClient, given: dict[str, object], seen: bool
):
    code = "import domo\n\ndef run(entry):\n    return domo.dry_run\n"
    run = await open_run(client, code, entry=given)
    end = await post(client, f"/runs/{run}/next", {})
    assert (end["result"]["status"], end["result"]["output"]) == ("done", seen)  # type: ignore[index]


# A program that reads one answer and gives back its length.
CAUGHT_LEN = 'import domo\n\ndef run(entry):\n    return len(domo.read("SELECT 1"))\n'


async def test_an_answer_too_big_reaches_the_program_as_a_refusal(client: httpx.AsyncClient):
    run = await open_run(client, CAUGHT, entry={})
    await post(client, f"/runs/{run}/next", {})
    end = await post(client, f"/runs/{run}/next", {"answer": {"ok": "x" * 70_000}})
    assert end["result"]["output"] == {"caught": "too_big", "detail": None}  # type: ignore[index]


async def test_the_answer_frame_follows_the_run_s_answer_bytes(client: httpx.AsyncClient):
    # Final review I5, ruling R33: the core sends the launch's result_bytes as answer_bytes.
    run = await open_run(client, CAUGHT_LEN, entry={}, answer_bytes=200_000)
    await post(client, f"/runs/{run}/next", {})
    end = await post(client, f"/runs/{run}/next", {"answer": {"ok": "x" * 70_000}})
    assert end["result"]["output"] == 70_000  # type: ignore[index]


async def test_an_answer_and_an_output_are_weighed_in_utf8_as_the_core_does(
    client: httpx.AsyncClient,
):
    # Final review m4: 30 000 accented letters are 60 KB of UTF-8, 180 KB escaped.
    accented = "é" * 30_000
    run = await open_run(client, READ, entry={})
    await post(client, f"/runs/{run}/next", {})
    end = await post(client, f"/runs/{run}/next", {"answer": {"ok": accented}})
    assert end["result"]["status"] == "done"  # type: ignore[index]
    assert end["result"]["output"] == {"got": accented}  # type: ignore[index]


async def test_the_same_ask_is_given_again_until_it_is_answered(client: httpx.AsyncClient):
    run = await open_run(client, READ, entry={})
    first = await post(client, f"/runs/{run}/next", {})
    again = await post(client, f"/runs/{run}/next", {})
    assert again == first
    end = await post(client, f"/runs/{run}/next", {"answer": {"ok": 2}})
    assert end["result"]["output"] == {"got": 2}  # type: ignore[index]


async def test_an_answer_when_nothing_was_asked_is_409(client: httpx.AsyncClient):
    run = await open_run(client, READ, entry={})
    response = await client.post(f"/runs/{run}/next", json={"answer": {"ok": 1}})
    assert response.status_code == 409
    await post(client, f"/runs/{run}/stop")


async def test_the_time_of_a_run_is_the_program_s_own_not_the_core_s(client: httpx.AsyncClient):
    """While the core answers (a tool it launches, a slow query), the program's clock waits."""
    run = await open_run(client, READ, entry={}, max_seconds=1)
    await post(client, f"/runs/{run}/next", {})
    await asyncio.sleep(1.5)
    end = await post(client, f"/runs/{run}/next", {"answer": {"ok": 1}})
    assert end["result"]["status"] == "done"  # type: ignore[index]


async def test_a_program_that_runs_out_of_time_between_two_asks_is_stopped(
    client: httpx.AsyncClient,
):
    code = (
        "import domo\n\ndef run(entry):\n    domo.read('SELECT 1')\n    while True:\n        pass\n"
    )
    run = await open_run(client, code, entry={}, max_seconds=1)
    await post(client, f"/runs/{run}/next", {})
    started = time.monotonic()
    end = await post(client, f"/runs/{run}/next", {"answer": {"ok": 1}})
    assert end["result"]["status"] == "timeout"  # type: ignore[index]
    assert time.monotonic() - started < 3


# --- stop, idle, size ----------------------------------------------------------------------------


async def test_stop_ends_the_run_as_stopped(client: httpx.AsyncClient, account: FakeAccount):
    run = await open_run(client, READ, entry={})
    await post(client, f"/runs/{run}/next", {})
    end = await post(client, f"/runs/{run}/stop")
    assert end["kind"] == "end"
    assert end["result"]["status"] == "stopped"  # type: ignore[index]
    assert end["result"]["output"] is None  # type: ignore[index]
    # Ended as a timeout is: the account cleared, then freed.
    assert account.killed == [20123]
    assert runner._taken_accounts == set()
    assert (await client.post(f"/runs/{run}/next", json={})).status_code == 404
    assert (await client.post(f"/runs/{run}/stop")).status_code == 404


async def test_stop_while_the_program_works_ends_it_at_once(
    client: httpx.AsyncClient, account: FakeAccount
):
    """A stop does not wait for the next that waits on the program."""
    code = "def run(entry):\n    while True:\n        pass\n"
    run = await open_run(client, code, entry={}, max_seconds=30)
    waiting = asyncio.create_task(client.post(f"/runs/{run}/next", json={}))
    await asyncio.sleep(0.5)
    started = time.monotonic()
    end = await post(client, f"/runs/{run}/stop")
    assert end["result"]["status"] == "stopped"  # type: ignore[index]
    assert time.monotonic() - started < 5
    response = await waiting
    assert response.json()["result"]["status"] == "stopped"
    assert runner._taken_accounts == set()


@pytest.mark.parametrize("output_bytes", [65_536, 1_048_576])
async def test_an_ask_line_over_64_kb_ends_the_run_in_error(
    client: httpx.AsyncClient, output_bytes: int
):
    code = 'import domo\n\ndef run(entry):\n    return domo.read("x" * 70_000)\n'
    run = await open_run(client, code, entry={}, output_bytes=output_bytes)
    end = await post(client, f"/runs/{run}/next", {})
    assert end["kind"] == "end"
    assert end["result"]["status"] == "error"  # type: ignore[index]
    assert end["result"]["note"] == "A request of the tool is bigger than allowed."  # type: ignore[index]


@pytest.mark.parametrize(
    "start",
    [b'{ "ask": {"do": "read", "sql": "', b'{"x": 0, "ask": {"do": "read", "sql": "'],
    ids=["spaced", "another_key_first"],
)
async def test_any_line_too_long_that_is_not_the_answer_ends_the_run_in_error(
    client: httpx.AsyncClient, start: bytes
):
    line = start + b"x" * 70_000 + b'"}}\n'
    code = f"import os, time\n\ndef run(entry):\n    os.write(3, {line!r})\n    time.sleep(30)\n"
    run = await open_run(client, code, entry={})
    end = await post(client, f"/runs/{run}/next", {})
    assert end["result"]["status"] == "error"  # type: ignore[index]
    assert end["result"]["note"] == "A request of the tool is bigger than allowed."  # type: ignore[index]


async def test_a_run_the_core_stops_asking_about_is_killed(
    monkeypatch: pytest.MonkeyPatch, account: FakeAccount
):
    monkeypatch.setattr(runs, "IDLE_SECONDS", 1)
    async with client_for(slots=1) as client:
        run = await open_run(client, READ, entry={})
        assert runner._taken_accounts == {20123}
        # No next: the run waits on the core, which is gone.
        started = time.monotonic()
        other: str | None = None
        while other is None and time.monotonic() - started < 3:
            body = RunRequest(code=READ, entry={}, wait_for_a_place=False).model_dump()
            response = await client.post("/runs", json=body)
            if response.status_code == 200:
                other = str(response.json()["run"])
            else:
                assert response.status_code == 409
                await asyncio.sleep(0.1)
        # Its place was given back: it is gone, account and all. Its account was cleared, then
        # freed: the next run got it again.
        assert other is not None
        assert account.killed == [20123]
        assert runner._taken_accounts == {20123}
        assert (await client.post(f"/runs/{run}/next", json={})).status_code == 404
        await post(client, f"/runs/{other}/stop")
        assert runner._taken_accounts == set()


async def test_a_run_kept_alive_outlives_the_idle_time(monkeypatch: pytest.MonkeyPatch):
    """The core works on an answer longer than the idle time, and says so."""
    monkeypatch.setattr(runs, "IDLE_SECONDS", 1)
    async with client_for() as client:
        run = await open_run(client, READ, entry={})
        await post(client, f"/runs/{run}/next", {})
        for _ in range(8):
            await asyncio.sleep(0.3)
            assert (await client.post(f"/runs/{run}/alive")).status_code == 204
        end = await post(client, f"/runs/{run}/next", {"answer": {"ok": 5}})
        assert (end["result"]["status"], end["result"]["output"]) == ("done", {"got": 5})  # type: ignore[index]


async def test_alive_while_the_program_works_does_not_arm_the_idle_clock(
    monkeypatch: pytest.MonkeyPatch,
):
    """A run is not idle while a next waits on its program: an alive then changes nothing."""
    monkeypatch.setattr(runs, "IDLE_SECONDS", 1)
    code = "import time\n\ndef run(entry):\n    time.sleep(2.5)\n    return 1\n"
    async with client_for() as client:
        run = await open_run(client, code, entry={}, max_seconds=10)
        waiting = asyncio.create_task(client.post(f"/runs/{run}/next", json={}))
        await asyncio.sleep(0.3)
        assert (await client.post(f"/runs/{run}/alive")).status_code == 204
        end = (await waiting).json()
        assert (end["result"]["status"], end["result"]["output"]) == ("done", 1)


async def test_a_run_that_is_answered_in_time_is_not_idle(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(runs, "IDLE_SECONDS", 1)
    code = "import domo\n\ndef run(entry):\n    return [domo.read('SELECT 1') for _ in range(3)]\n"
    async with client_for() as client:
        run = await open_run(client, code, entry={})
        event = await post(client, f"/runs/{run}/next", {})
        while event["kind"] == "ask":
            await asyncio.sleep(0.6)
            event = await post(client, f"/runs/{run}/next", {"answer": {"ok": 1}})
        assert event["result"]["output"] == [1, 1, 1]  # type: ignore[index]


# --- one run's channel is its own -----------------------------------------------------------------


async def test_a_run_id_is_unguessable_and_one_runs_answers_never_reach_another(
    client: httpx.AsyncClient,
):
    first = await open_run(client, READ, entry={})
    second = await open_run(client, READ, entry={})
    assert first != second
    for run in (first, second):
        assert re.fullmatch(r"[A-Za-z0-9_-]{22}", run)  # 16 random bytes
    await post(client, f"/runs/{first}/next", {})
    asked = await post(client, f"/runs/{second}/next", {})
    end = await post(client, f"/runs/{first}/next", {"answer": {"ok": "first's"}})
    assert end["result"]["output"] == {"got": "first's"}  # type: ignore[index]
    # The second still waits on its own answer: the same ask, given again.
    assert await post(client, f"/runs/{second}/next", {}) == asked
    end = await post(client, f"/runs/{second}/next", {"answer": {"ok": "second's"}})
    assert end["result"]["output"] == {"got": "second's"}  # type: ignore[index]
    for path in ("/runs/nobody/next", "/runs/nobody/stop"):
        assert (await client.post(path, json={})).status_code == 404


async def test_no_free_place_without_waiting_is_409():
    async with client_for(slots=1) as client:
        run = await open_run(client, READ, entry={})
        body = RunRequest(code=READ, entry={}, wait_for_a_place=False).model_dump()
        response = await client.post("/runs", json=body)
        assert response.status_code == 409
        await post(client, f"/runs/{run}/stop")
        # Once the place is free, it is given.
        other = await post(client, "/runs", body)
        await post(client, f"/runs/{other['run']}/stop")


async def test_a_program_cannot_write_to_its_answer_pipe_for_another_run(
    client: httpx.AsyncClient,
):
    other = await open_run(client, READ, entry={})
    code = (
        "import json, os\n"
        "import domo\n\n"
        "def run(entry):\n"
        "    for fd in (0, 1):\n"
        "        try:\n"
        '            os.write(fd, b\'{"ask": {"do": "read"}}\\n\')\n'
        "        except OSError:\n"
        "            pass\n"
        '    line = {"run": entry["other"], "ask": {"do": "read", "sql": "SELECT 2",'
        ' "params": [], "space": "common"}}\n'
        "    os.write(3, (json.dumps(line) + '\\n').encode())\n"
        '    return domo.read("SELECT 3")\n'
    )
    run = await open_run(client, code, entry={"other": other})
    end = await post(client, f"/runs/{run}/next", {})
    assert end["kind"] == "end"
    assert end["result"]["status"] == "error"  # type: ignore[index]
    assert end["result"]["note"] == "A request of the tool is not valid."  # type: ignore[index]
    # The other run only ever sees its own program's request.
    asked = await post(client, f"/runs/{other}/next", {})
    assert asked["n"] == 1
    assert asked["ask"] == {"do": "read", "sql": "SELECT 1", "params": [], "space": "common"}
    await post(client, f"/runs/{other}/stop")


@pytest.mark.parametrize(
    "line",
    [
        b"not json\n",
        b'{"ask": {"do": "format_disk"}}\n',
        b'{"ask": ["read"]}\n',
        b'{"ask": {"sql": "SELECT 1"}}\n',
        b'{"ask": {"do": "read", "n": NaN}}\n',
        b'{"ask": {"do": "read", "x": ' + b"[" * 600 + b"]" * 600 + b"}}\n",
        b'{"ask": {"do": "read", "x": ' + b"[" * 30_000 + b"]" * 30_000 + b"}}\n",
    ],
    ids=["not_json", "unknown_do", "not_an_object", "no_do", "nan", "too_deep", "deeper"],
)
async def test_a_malformed_ask_ends_the_run_in_error(client: httpx.AsyncClient, line: bytes):
    code = f"import os, time\n\ndef run(entry):\n    os.write(3, {line!r})\n    time.sleep(30)\n"
    run = await open_run(client, code, entry={})
    end = await post(client, f"/runs/{run}/next", {})
    assert end["kind"] == "end"
    assert end["result"]["status"] == "error"  # type: ignore[index]
    json.dumps(end)
