import asyncio
import ipaddress
import logging
import os
import signal
import socket
import threading
from contextlib import closing, suppress
from pathlib import Path

import httpx
import pytest
import uvicorn
from domo_common.gatekeeper_api import Binding, HeaderLocation
from domo_gatekeeper.__main__ import (
    build_servers,
    create_http_client,
    main,
    pin_http_loggers,
    run_servers,
    serve,
    stop_on_signals,
)
from domo_gatekeeper.config import GatekeeperSettings
from domo_gatekeeper.errors import ServerStartupError
from domo_gatekeeper.master_key import create_master_key
from domo_gatekeeper.vault import Vault
from fastapi import FastAPI

TOKEN = "core-token-for-main-tests"
FORM_TOKEN = "form-token-that-must-not-be-logged"


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.upper().startswith("DOMO_"):
            monkeypatch.delenv(name)


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    path = tmp_path / "core_token"
    path.write_text(TOKEN + "\n")
    return path


def free_port() -> int:
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def local_settings(tmp_path: Path, token_file: Path) -> GatekeeperSettings:
    return GatekeeperSettings(
        gatekeeper_data_dir=tmp_path / "data",
        core_token_file=token_file,
        gatekeeper_internal_port=free_port(),
        gatekeeper_public_port=free_port(),
        gatekeeper_host="127.0.0.1",
    )


# --- The shared http client ---------------------------------------------------------------------


async def test_the_http_client_has_explicit_timeouts_and_never_follows_redirects():
    async with create_http_client() as client:
        assert client.timeout == httpx.Timeout(connect=10.0, read=300.0, write=60.0, pool=10.0)
        assert client.follow_redirects is False
        assert client.trust_env is False


async def test_the_http_client_carries_no_credentials():
    async with create_http_client() as client:
        assert client.auth is None
        assert len(client.cookies) == 0
        assert "authorization" not in client.headers and "cookie" not in client.headers


# --- Logging ------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def restore_loggers():
    names = ("httpcore", "httpx")
    levels = {name: logging.getLogger(name).level for name in names}
    yield
    for name, level in levels.items():
        logging.getLogger(name).setLevel(level)


@pytest.mark.parametrize("name", ["httpcore", "httpx"])
def test_the_http_loggers_are_pinned_above_debug(restore_loggers, name):
    logging.getLogger(name).setLevel(logging.NOTSET)
    pin_http_loggers()
    assert logging.getLogger(name).level >= logging.INFO


def test_pinning_does_not_lower_a_stricter_level(restore_loggers):
    logging.getLogger("httpx").setLevel(logging.ERROR)
    pin_http_loggers()
    assert logging.getLogger("httpx").level == logging.ERROR


def test_the_pin_holds_when_the_root_logger_is_at_debug(restore_loggers):
    root = logging.getLogger()
    previous = root.level
    root.setLevel(logging.DEBUG)
    try:
        pin_http_loggers()
        assert not logging.getLogger("httpcore.http11").isEnabledFor(logging.DEBUG)
        assert not logging.getLogger("httpx").isEnabledFor(logging.DEBUG)
    finally:
        root.setLevel(previous)


# --- Starting and stopping servers --------------------------------------------------------------


class FakeServer:
    """What ``run_servers`` needs of a uvicorn server."""

    def __init__(self, *, fail_with: BaseException | None = None, run_for: float | None = None):
        self.should_exit = False
        self.finished = False
        self._fail_with = fail_with
        self._run_for = run_for

    async def serve(self) -> None:
        try:
            if self._fail_with is not None:
                raise self._fail_with
            waited = 0.0
            while not self.should_exit and (self._run_for is None or waited < self._run_for):
                await asyncio.sleep(0.01)
                waited += 0.01
        finally:
            self.finished = True


async def test_run_servers_stops_the_others_when_one_stops():
    survivor, quitter = FakeServer(), FakeServer(run_for=0.05)
    await asyncio.wait_for(run_servers([survivor, quitter]), 5)
    assert survivor.should_exit and survivor.finished and quitter.finished


async def test_run_servers_reports_a_server_that_cannot_start():
    survivor = FakeServer()
    # uvicorn ends the interpreter with SystemExit when a port is taken.
    doomed = FakeServer(fail_with=SystemExit(1))
    with pytest.raises(ServerStartupError):
        await asyncio.wait_for(run_servers([survivor, doomed]), 5)
    assert survivor.should_exit and survivor.finished


async def test_run_servers_propagates_a_crash_and_stops_the_others():
    survivor = FakeServer()
    with pytest.raises(RuntimeError, match="crash"):
        await asyncio.wait_for(
            run_servers([survivor, FakeServer(fail_with=RuntimeError("crash"))]), 5
        )
    assert survivor.should_exit and survivor.finished


async def test_run_servers_stops_everything_when_cancelled():
    first, second = FakeServer(), FakeServer()
    task = asyncio.create_task(run_servers([first, second]))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert first.finished and second.finished and first.should_exit and second.should_exit


def test_build_servers_configures_both_without_an_access_log():
    internal, public = FastAPI(), FastAPI()
    servers = build_servers(
        internal, public, host="127.0.0.1", internal_port=9101, public_port=9102
    )
    assert [s.config.app for s in servers] == [internal, public]
    assert [s.config.port for s in servers] == [9101, 9102]
    assert all(s.config.host == "127.0.0.1" for s in servers)
    # The path of the vault form carries a one-time token: it must not be logged.
    assert all(s.config.access_log is False for s in servers)
    assert all(isinstance(s, uvicorn.Server) for s in servers)
    assert all(s.config.server_header is False for s in servers)
    assert all(s.config.proxy_headers is False for s in servers)


def test_the_servers_leave_the_signals_to_the_process():
    servers = build_servers(
        FastAPI(), FastAPI(), host="127.0.0.1", internal_port=9101, public_port=9102
    )
    before = signal.getsignal(signal.SIGINT)
    with servers[0].capture_signals():
        assert signal.getsignal(signal.SIGINT) == before


async def start(settings: GatekeeperSettings) -> asyncio.Task[None]:
    """Run ``serve`` in the background and wait until the internal API answers."""
    task = asyncio.create_task(serve(settings))
    url = f"http://127.0.0.1:{settings.gatekeeper_internal_port}/internal/health"
    async with httpx.AsyncClient() as client:
        for _ in range(400):
            if task.done():
                task.result()  # raises what stopped it
                pytest.fail("the servers stopped before answering")
            try:
                if (await client.get(url)).status_code == 200:
                    return task
            except httpx.TransportError:
                pass
            await asyncio.sleep(0.025)
    task.cancel()
    pytest.fail("the internal API never answered")


async def stop(task: asyncio.Task[None]) -> None:
    task.cancel()
    with suppress(asyncio.CancelledError):
        await asyncio.wait_for(task, 10)


async def test_serve_runs_the_internal_and_the_public_servers(tmp_path, token_file):
    settings = local_settings(tmp_path, token_file)
    task = await start(settings)
    internal = f"http://127.0.0.1:{settings.gatekeeper_internal_port}"
    public = f"http://127.0.0.1:{settings.gatekeeper_public_port}"
    try:
        async with httpx.AsyncClient() as client:
            # Internal: the API, behind the token that was read from the file.
            assert (await client.get(f"{internal}/internal/secrets")).status_code == 401
            r = await client.get(
                f"{internal}/internal/secrets", headers={"authorization": f"Bearer {TOKEN}"}
            )
            assert r.status_code == 200 and r.json() == []
            # Public: only the vault form, and none of the internal routes.
            r = await client.get(f"{public}/vault/r/unknown-token")
            assert r.status_code == 404 and r.headers["x-domo-component"] == "gatekeeper"
            for path in ("/internal/health", "/internal/secrets", "/llm/p1/x"):
                assert (await client.get(f"{public}{path}")).status_code == 404
            # The server does not announce itself.
            assert "server" not in (await client.get(f"{internal}/internal/health")).headers
    finally:
        await stop(task)


async def test_serve_creates_the_vault_files_and_the_master_key(tmp_path, token_file):
    settings = local_settings(tmp_path, token_file)
    task = await start(settings)
    await stop(task)
    data = settings.gatekeeper_data_dir
    assert {p.name for p in data.iterdir()} >= {
        "master.key",
        "vault.db",
        "requests.db",
        "providers.db",
    }
    assert len((data / "master.key").read_bytes()) == 32


async def test_serve_says_what_a_program_may_never_reach(tmp_path, token_file, caplog):
    settings = local_settings(tmp_path, token_file).model_copy(
        update={
            "own_networks": (ipaddress.ip_network("10.99.7.0/24"),),
            "host_addresses": (ipaddress.ip_address("192.168.1.20"),),
        }
    )
    caplog.set_level(logging.INFO, logger="domo_gatekeeper")
    task = await start(settings)
    await stop(task)
    assert "10.99.7.0/24" in caplog.text and "192.168.1.20" in caplog.text


async def test_serve_relays_programs_within_what_it_may_reach(tmp_path, token_file):
    settings = local_settings(tmp_path, token_file).model_copy(
        update={"own_networks": (ipaddress.ip_network("10.99.7.0/24"),)}
    )
    task = await start(settings)
    request = {
        "method": "GET",
        "url": "http://10.99.7.5:9000/",
        "open": [{"host": "10.99.7.5", "port": 9000}],
        "seconds": 5,
        "most_bytes": 1000,
        "session_idle_seconds": 60,
        "resolve_only": True,
    }
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"http://127.0.0.1:{settings.gatekeeper_internal_port}/internal/out",
                headers={"authorization": f"Bearer {TOKEN}"},
                json=request,
            )
    finally:
        await stop(task)
    assert (r.status_code, r.json()) == (403, {"code": "never_reachable"})


async def test_the_one_time_form_path_is_not_written_to_any_log(tmp_path, token_file, caplog):
    settings = local_settings(tmp_path, token_file)
    pin_http_loggers()  # the test's own client would otherwise log the URL it requests
    caplog.set_level(logging.DEBUG)
    task = await start(settings)
    try:
        # uvicorn writes an access line only when ``uvicorn.access`` has a handler to write to
        # (it checks once per connection): the servers must leave it without one.
        assert not logging.getLogger("uvicorn.access").hasHandlers()
        async with httpx.AsyncClient() as client:
            public = f"http://127.0.0.1:{settings.gatekeeper_public_port}"
            await client.get(f"{public}/vault/r/{FORM_TOKEN}")
    finally:
        await stop(task)
    assert "uvicorn.error" in caplog.text  # the servers do log: just not the requests
    assert FORM_TOKEN not in caplog.text


async def test_a_stream_is_relayed_as_it_arrives_and_its_end_releases_the_provider(
    tmp_path, token_file
):
    # Through the real servers (an ASGI test transport buffers): the first event must reach the
    # caller while the provider is still talking, and a caller that goes away must free the
    # provider's connection.
    first_sent, upstream_saw_eof = asyncio.Event(), asyncio.Event()

    async def provider(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(
            b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\n"
            b"transfer-encoding: chunked\r\n\r\n"
            b"b\r\ndata: one\n\n\r\n"
        )
        await writer.drain()
        first_sent.set()
        await reader.read()  # the rest of the answer never comes: wait for the relay to hang up
        upstream_saw_eof.set()
        writer.close()

    upstream = await asyncio.start_server(provider, "127.0.0.1", 0)
    port = upstream.sockets[0].getsockname()[1]
    settings = local_settings(tmp_path, token_file)
    task = await start(settings)
    internal = f"http://127.0.0.1:{settings.gatekeeper_internal_port}"
    bearer = {"authorization": f"Bearer {TOKEN}"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            route = {"id": "local", "base_url": f"http://127.0.0.1:{port}", "secret_ref": None}
            r = await client.put(f"{internal}/internal/providers/local", json=route, headers=bearer)
            assert r.status_code == 204
            async with client.stream("GET", f"{internal}/llm/local/events", headers=bearer) as r:
                assert r.status_code == 200
                assert r.headers["content-type"] == "text/event-stream"
                first = await anext(r.aiter_bytes())
                assert first == b"data: one\n\n" and first_sent.is_set()
            # The caller left (the stream context closed its connection).
            await asyncio.wait_for(upstream_saw_eof.wait(), 10)
    finally:
        upstream.close()
        await stop(task)


async def test_one_stop_signal_stops_both_servers(tmp_path, token_file):
    settings = local_settings(tmp_path, token_file)
    previous = signal.signal(signal.SIGTERM, lambda *_: None)  # a harmless default for the test
    try:
        task = await start(settings)
        signal.raise_signal(signal.SIGTERM)
        await asyncio.wait_for(task, 10)  # returns normally: a requested stop is not an error
    finally:
        signal.signal(signal.SIGTERM, previous)
    for port in (settings.gatekeeper_internal_port, settings.gatekeeper_public_port):
        with pytest.raises(OSError), closing(socket.create_connection(("127.0.0.1", port), 1)):
            pass


async def test_stop_on_signals_restores_the_previous_handlers():
    stops: list[str] = []
    previous = signal.signal(signal.SIGTERM, signal.SIG_DFL)
    try:
        with stop_on_signals(lambda: stops.append("stop")):
            await asyncio.sleep(0)
        assert signal.getsignal(signal.SIGTERM) == signal.SIG_DFL
        assert stops == []
    finally:
        signal.signal(signal.SIGTERM, previous)


async def test_stop_on_signals_calls_stop_when_the_process_is_asked_to_stop():
    stops: list[str] = []
    previous = signal.signal(signal.SIGTERM, lambda *_: None)
    try:
        with stop_on_signals(lambda: stops.append("stop")):
            signal.raise_signal(signal.SIGTERM)
            for _ in range(100):
                if stops:
                    break
                await asyncio.sleep(0.01)
    finally:
        signal.signal(signal.SIGTERM, previous)
    assert stops == ["stop"]


def test_stop_on_signals_outside_the_main_thread_installs_nothing():
    # Signal handlers can only be set from the main thread (and there is no loop here): the
    # context manager must stand aside rather than fail.
    entered: list[bool] = []

    def attempt() -> None:
        with stop_on_signals(lambda: None):
            entered.append(True)

    worker = threading.Thread(target=attempt)
    worker.start()
    worker.join()
    assert entered == [True]


# --- main(): startup failures that must be explained -------------------------------------------


def run_main_expecting_exit() -> str:
    with pytest.raises(SystemExit) as excinfo:
        main()
    code = excinfo.value.code
    assert isinstance(code, str), "the exit must carry a message"
    return code


def test_a_missing_token_file_stops_the_start_with_a_clear_message(monkeypatch, tmp_path):
    monkeypatch.setenv("DOMO_GATEKEEPER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DOMO_CORE_TOKEN_FILE", str(tmp_path / "missing" / "core_token"))
    message = run_main_expecting_exit()
    assert "core token" in message.lower() and str(tmp_path / "missing") in message
    # Nothing was created: there is no fallback token and no half-started gatekeeper.
    assert not (tmp_path / "data").exists()


def test_a_vault_without_its_master_key_stops_the_start(monkeypatch, tmp_path, token_file):
    data = tmp_path / "data"
    key = create_master_key(tmp_path / "elsewhere.key")
    binding = Binding(host="api.example.com", location=HeaderLocation(kind="header", name="x"))
    Vault(data / "vault.db", key).put("provider:p1:api_key", "v", purpose="p", bindings=[binding])
    monkeypatch.setenv("DOMO_GATEKEEPER_DATA_DIR", str(data))
    monkeypatch.setenv("DOMO_CORE_TOKEN_FILE", str(token_file))
    message = run_main_expecting_exit()
    assert "restore the key from your backup" in message
    assert not (data / "master.key").exists()  # a fresh key would orphan the secrets


def test_a_corrupt_master_key_stops_the_start(monkeypatch, tmp_path, token_file):
    data = tmp_path / "data"
    data.mkdir()
    (data / "master.key").write_bytes(b"too short")
    monkeypatch.setenv("DOMO_GATEKEEPER_DATA_DIR", str(data))
    monkeypatch.setenv("DOMO_CORE_TOKEN_FILE", str(token_file))
    assert "corrupt" in run_main_expecting_exit()


def test_an_invalid_configuration_stops_the_start(monkeypatch):
    monkeypatch.setenv("DOMO_GATEKEEPER_INTERNAL_PORT", "99999")
    message = run_main_expecting_exit()
    assert "DOMO_GATEKEEPER_INTERNAL_PORT" in message and "99999" not in message
