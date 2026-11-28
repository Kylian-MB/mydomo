"""The core's entry point: migrate, make the tools' database, issue the setup code, then serve.

The migration, the tools' database and the web server are replaced by recorders: the order of the
steps and what is handed to them is what is tested (the three have their own tests).
"""

import asyncio
import logging
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import asyncpg
import httpx
import pytest
import uvicorn
from chat_support import live_server
from domo_core import __main__ as entry
from domo_core.accounts.models import Person
from domo_core.channels.voice.protocol import MAX_FRAME_BYTES
from domo_core.config import CoreSettings
from domo_core.db import create_engine, make_session_factory
from domo_core.setup_flow.code import verify_setup_code
from fastapi import FastAPI
from sqlalchemy import text

real_prepare_tools = entry.prepare_tools

CODE = r"[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}"
CODE_LINE = re.compile(rf"The assistant setup code: ({CODE})")


@pytest.fixture(autouse=True)
def _logging_put_back() -> Iterator[None]:
    """``configure_logging`` (called here directly, and by ``main``) pins loggers for the whole
    process and gives the access log its filters: put them back as they were after each test, so
    that a later test of another module reads the logs it expects."""
    names = ("", "uvicorn.access", *entry.HTTP_LOGGERS)
    levels = {name: logging.getLogger(name).level for name in names}
    access = logging.getLogger("uvicorn.access")
    filters = list(access.filters)
    yield
    for name, level in levels.items():
        logging.getLogger(name).setLevel(level)
    access.filters[:] = filters


class Recorder:
    """What `main` did, in order, with the migration and the server replaced."""

    def __init__(self) -> None:
        self.steps: list[str] = []
        self.migrated_url: str | None = None
        self.app: object = None
        self.server_options: dict[str, object] = {}
        self.records_when_served: list[str] = []
        self.loop_running_during_migration: bool | None = None
        self.codes_announced_before_migration: list[int] = []
        self.code_usable_when_served: bool | None = None
        self.tools_database: str | None = None


@pytest.fixture
def recorder(
    monkeypatch: pytest.MonkeyPatch,
    database_url: str,
    core_token_file: Path,
    caplog: pytest.LogCaptureFixture,
):
    monkeypatch.setenv("DOMO_DATABASE_URL", database_url)
    monkeypatch.setenv("DOMO_CORE_TOKEN_FILE", str(core_token_file))
    caplog.set_level(logging.INFO)
    rec = Recorder()

    def fake_migration(url: str) -> None:
        rec.steps.append("migrate")
        rec.migrated_url = url
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            rec.loop_running_during_migration = False
        else:
            rec.loop_running_during_migration = True
        rec.codes_announced_before_migration.append(len(logged_codes(caplog)))

    def fake_run(app: object, **options: object) -> None:
        rec.steps.append("serve")
        rec.app = app
        rec.server_options = options
        rec.records_when_served = [r.getMessage() for r in caplog.records]
        # From another connection: the code must be committed before the first request can come.
        announced = [m for r in caplog.records if (m := CODE_LINE.search(r.getMessage()))]
        if announced:
            rec.code_usable_when_served = asyncio.run(code_is_valid(database_url, announced[0][1]))

    async def fake_tools(settings: CoreSettings) -> None:
        # The tools' database has its own tests (``test_toolsdb``): its roles are the server's,
        # and must not outlive a test.
        rec.steps.append("tools")
        rec.tools_database = settings.tools_database

    monkeypatch.setattr(entry, "upgrade_to_head", fake_migration)
    monkeypatch.setattr(entry, "prepare_tools", fake_tools)
    monkeypatch.setattr(uvicorn, "run", fake_run)
    return rec


def logged_codes(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if CODE_LINE.search(r.getMessage())]


async def code_is_valid(url: str, code: str) -> bool:
    engine = create_engine(CoreSettings(database_url_override=url))
    try:
        async with make_session_factory(engine)() as db_session:
            return await verify_setup_code(db_session, code)
    finally:
        await engine.dispose()


async def add_committed_owner(url: str) -> None:
    engine = create_engine(CoreSettings(database_url_override=url))
    try:
        async with make_session_factory(engine)() as db_session:
            db_session.add(Person(display_name="Owner", role="owner"))
            await db_session.commit()
    finally:
        await engine.dispose()


async def read_state(url: str) -> object:
    engine = create_engine(CoreSettings(database_url_override=url))
    try:
        async with engine.connect() as connection:
            return (await connection.execute(text("SELECT setup_code_hash FROM setup_state"))).one()
    finally:
        await engine.dispose()


# --- the order of the steps ------------------------------------------------------------------


def test_main_migrates_then_announces_the_code_then_serves(recorder, caplog, database_url):
    entry.main()
    assert recorder.steps == ["migrate", "tools", "serve"]
    assert recorder.migrated_url == database_url
    assert recorder.tools_database == "domo_tools"
    assert recorder.codes_announced_before_migration == [0]  # the code comes after the migration
    [record] = logged_codes(caplog)
    assert any(CODE_LINE.search(message) for message in recorder.records_when_served)
    # The line is exactly the one the end-to-end test greps for, at a level nobody filters out.
    assert record.levelno == logging.WARNING
    match = CODE_LINE.fullmatch(record.getMessage())
    assert match is not None
    assert asyncio.run(code_is_valid(database_url, match.group(1)))


def test_the_migration_runs_before_any_event_loop(recorder):
    entry.main()
    assert recorder.loop_running_during_migration is False


def test_the_code_is_committed_before_the_server_starts(recorder):
    entry.main()
    assert recorder.code_usable_when_served is True


def test_each_start_replaces_the_code(recorder, caplog, database_url):
    entry.main()
    entry.main()
    first, second = (CODE_LINE.fullmatch(r.getMessage()) for r in logged_codes(caplog))
    assert first is not None and second is not None
    assert first.group(1) != second.group(1)
    assert not asyncio.run(code_is_valid(database_url, first.group(1)))
    assert asyncio.run(code_is_valid(database_url, second.group(1)))


def test_no_code_is_announced_once_there_is_an_owner(recorder, caplog, database_url):
    asyncio.run(add_committed_owner(database_url))
    entry.main()
    assert logged_codes(caplog) == []
    assert recorder.steps == ["migrate", "tools", "serve"]  # the server starts all the same
    assert asyncio.run(read_state(database_url)) == (None,)


def test_the_code_is_logged_once_and_only_in_its_own_line(recorder, caplog):
    entry.main()
    [record] = logged_codes(caplog)
    code = CODE_LINE.fullmatch(record.getMessage())
    assert code is not None
    others = [r.getMessage() for r in caplog.records if r is not record]
    assert not any(code.group(1) in message for message in others)


# --- the server ------------------------------------------------------------------------------


def test_the_server_is_started_on_port_8000_with_the_application(recorder):
    entry.main()
    options = recorder.server_options
    assert isinstance(recorder.app, FastAPI)
    assert options["host"] == "0.0.0.0"
    assert options["port"] == 8000
    assert options["access_log"] is True


def test_no_websocket_frame_bigger_than_the_voice_route_takes_is_assembled(recorder):
    """The voice socket closes on any frame over ``MAX_FRAME_BYTES``: the server stops one a
    little above it, before it holds megabytes of it (its own default is 16 MiB)."""
    entry.main()
    limit = recorder.server_options["ws_max_size"]
    assert isinstance(limit, int)
    assert MAX_FRAME_BYTES < limit <= MAX_FRAME_BYTES + 16 * 1024


def test_only_the_trusted_proxy_may_set_the_forwarded_headers(recorder):
    entry.main()
    options = recorder.server_options
    assert options["proxy_headers"] is True
    assert options["forwarded_allow_ips"] == "127.0.0.1"
    assert options["forwarded_allow_ips"] != "*"


def test_the_trusted_proxies_come_from_the_settings(recorder, monkeypatch):
    monkeypatch.setenv("DOMO_TRUSTED_PROXIES", "172.20.0.0/16,172.21.0.2")
    entry.main()
    assert recorder.server_options["forwarded_allow_ips"] == "172.20.0.0/16,172.21.0.2"


def test_the_application_given_to_the_server_is_built_from_the_settings(recorder, database_url):
    entry.main()
    app = recorder.app
    assert isinstance(app, FastAPI)
    assert app.state.settings.database_url == database_url


# --- bad configuration -----------------------------------------------------------------------


def test_a_missing_password_file_stops_the_start_with_a_clear_message(
    recorder, monkeypatch, tmp_path
):
    monkeypatch.delenv("DOMO_DATABASE_URL")
    monkeypatch.setenv("DOMO_DB_PASSWORD_FILE", str(tmp_path / "missing"))
    with pytest.raises(SystemExit) as stopped:
        entry.main()
    assert "missing" in str(stopped.value)
    assert str(tmp_path / "missing") in str(stopped.value)
    assert recorder.steps == []  # neither migrated nor served


@pytest.mark.parametrize("content", [None, "", "two words"])
def test_an_unusable_core_token_stops_the_start_before_anything_is_done(
    recorder, monkeypatch, tmp_path, content
):
    path = tmp_path / "token-file"
    if content is not None:
        path.write_text(content, encoding="ascii")
    monkeypatch.setenv("DOMO_CORE_TOKEN_FILE", str(path))
    with pytest.raises(SystemExit) as stopped:
        entry.main()
    assert "core token" in str(stopped.value)
    assert str(path) in str(stopped.value)
    assert "two words" not in str(stopped.value)
    assert recorder.steps == []  # no migration, no setup code, no server


def test_invalid_settings_stop_the_start_without_echoing_the_values(recorder, monkeypatch):
    monkeypatch.setenv("DOMO_SESSION_TTL_DAYS", "not-a-number-secret-looking")
    with pytest.raises(SystemExit) as stopped:
        entry.main()
    message = str(stopped.value)
    assert "DOMO_SESSION_TTL_DAYS" in message
    assert "not-a-number-secret-looking" not in message
    assert recorder.steps == []


def test_a_tools_database_that_cannot_be_prepared_stops_the_start_with_a_clear_message(
    recorder, monkeypatch
):
    async def refused(admin_dsn: str, *, core_database: str, name: str) -> None:
        raise asyncpg.InsufficientPrivilegeError("must be superuser to create an event trigger")

    # The real step, with the server's refusal in it.
    monkeypatch.setattr(entry, "prepare_tools", real_prepare_tools)
    monkeypatch.setattr(entry, "prepare_tools_database", refused)
    with pytest.raises(SystemExit) as stopped:
        entry.main()
    message = str(stopped.value)
    assert message.startswith("domo-core: the tools' database could not be prepared")
    assert "must be superuser to create an event trigger" in message
    assert "Traceback" not in message
    assert recorder.steps == ["migrate"]  # neither a setup code nor a server


def test_a_failing_migration_stops_the_start(recorder, monkeypatch):
    def failing(url: str) -> None:
        raise RuntimeError("the migration failed")

    monkeypatch.setattr(entry, "upgrade_to_head", failing)
    with pytest.raises(RuntimeError, match="migration failed"):
        entry.main()
    assert recorder.steps == []  # the server never started


# --- the log line ----------------------------------------------------------------------------


def test_the_setup_code_line_reaches_stderr_where_docker_logs_it():
    program = (
        "from domo_core.__main__ import announce_setup_code, configure_logging\n"
        "configure_logging()\n"
        "announce_setup_code('ABCD-EFGH-JKMN')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, timeout=120, check=True
    )
    lines = [line for line in result.stderr.splitlines() if "The assistant setup code" in line]
    assert len(lines) == 1
    assert lines[0].endswith("The assistant setup code: ABCD-EFGH-JKMN")
    assert "WARNING" in lines[0]
    assert result.stdout == ""


def test_the_http_libraries_and_sdks_cannot_log_the_requests_they_make():
    """They log the URL of every call at INFO and the headers (the core's token) at DEBUG."""
    program = (
        "import logging\n"
        "from domo_core.__main__ import HTTP_LOGGERS, configure_logging\n"
        "configure_logging()\n"
        "print(','.join(f'{n}={logging.getLogger(n).getEffectiveLevel()}' for n in HTTP_LOGGERS))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, timeout=120, check=True
    )
    levels = dict(item.split("=") for item in result.stdout.strip().split(","))
    assert set(levels) >= {"httpx", "httpx2", "httpcore", "httpcore2", "openai", "anthropic"}
    assert all(int(level) >= logging.WARNING for level in levels.values())


def test_trusting_every_address_stops_the_start_with_a_clear_message(recorder, monkeypatch):
    monkeypatch.setenv("DOMO_TRUSTED_PROXIES", "*")
    with pytest.raises(SystemExit) as stopped:
        entry.main()
    assert "DOMO_TRUSTED_PROXIES" in str(stopped.value)
    assert "every address" in str(stopped.value)
    assert recorder.steps == []


# --- the access log --------------------------------------------------------------------------
# The container's health check calls /healthz every few seconds: those lines would bury the ones
# that matter (the setup code is written just before the server starts).


def access_record(path: str) -> logging.LogRecord:
    """A record shaped like the ones uvicorn's access logger writes."""
    return logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:50000", "GET", path, "1.1", 200),
        None,
    )


@pytest.mark.parametrize("path", ["/healthz", "/healthz?probe=1"])
def test_the_access_log_filter_drops_the_health_checks(path):
    assert not entry.HealthCheckFilter().filter(access_record(path))


@pytest.mark.parametrize("path", ["/", "/login", "/healthz/more", "/healthzz", "/static/healthz"])
def test_the_access_log_filter_keeps_every_other_request(path):
    assert entry.HealthCheckFilter().filter(access_record(path))


def test_the_access_log_filter_keeps_records_of_another_shape():
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 0, "a message", None, None)
    assert entry.HealthCheckFilter().filter(record)


def test_the_logging_setup_filters_the_access_log_once():
    entry.configure_logging()
    entry.configure_logging()
    access = logging.getLogger("uvicorn.access")
    assert len([f for f in access.filters if isinstance(f, entry.HealthCheckFilter)]) == 1
    assert len([f for f in access.filters if isinstance(f, entry.InvitationLinkFilter)]) == 1


# An invitation link carries its token in the path (task 9): the access log must not keep it.


@pytest.mark.parametrize(
    "path", ["/invite/abcDEF_123-xyz", "/invite/abcDEF_123-xyz?lang=fr", "/invite/abcDEF_123-xyz/"]
)
def test_the_access_log_hides_the_token_of_an_invitation_link(path):
    record = access_record(path)
    assert entry.InvitationLinkFilter().filter(record)  # the request is still logged
    line = record.getMessage()
    assert "abcDEF_123-xyz" not in line
    assert '"GET /invite/[hidden] HTTP/1.1" 200' in line


@pytest.mark.parametrize("path", ["/", "/chat", "/invitex/abc", "/static/invite/abc", "/invite"])
def test_the_access_log_keeps_every_other_path_as_it_is(path):
    record = access_record(path)
    assert entry.InvitationLinkFilter().filter(record)
    assert f'"GET {path} HTTP/1.1" 200' in record.getMessage()


def test_the_invitation_filter_keeps_records_of_another_shape():
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 0, "a message", None, None)
    assert entry.InvitationLinkFilter().filter(record)
    assert record.getMessage() == "a message"


async def test_a_real_server_logs_the_requests_but_not_the_health_checks(caplog, monkeypatch):
    entry.configure_logging()
    # Another test's server (access_log=False) may have cut this logger off from the root.
    monkeypatch.setattr(logging.getLogger("uvicorn.access"), "propagate", True)
    caplog.set_level(logging.INFO)
    app = FastAPI()

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ping")
    async def ping() -> dict[str, str]:
        return {"status": "pong"}

    async with live_server(app, access_log=True) as base_url, httpx.AsyncClient() as client:
        assert (await client.get(f"{base_url}/healthz")).status_code == 200
        assert (await client.get(f"{base_url}/ping")).status_code == 200
    access = [r.getMessage() for r in caplog.records if r.name == "uvicorn.access"]
    assert any('"GET /ping HTTP/1.1" 200' in line for line in access)
    assert not any("/healthz" in line for line in access)


async def test_a_real_server_never_logs_the_token_of_an_invitation_link(caplog, monkeypatch):
    entry.configure_logging()
    monkeypatch.setattr(logging.getLogger("uvicorn.access"), "propagate", True)
    caplog.set_level(logging.INFO)
    app = FastAPI()

    @app.get("/invite/{token}")
    async def invite(token: str) -> dict[str, str]:
        return {"status": "ok"}

    hidden = "abcDEF_123-xyz-abcDEF_123-xyz"
    async with live_server(app, access_log=True) as base_url, httpx.AsyncClient() as client:
        assert (await client.get(f"{base_url}/invite/{hidden}")).status_code == 200
    access = [r.getMessage() for r in caplog.records if r.name == "uvicorn.access"]
    assert any('"GET /invite/[hidden] HTTP/1.1" 200' in line for line in access)
    assert hidden not in caplog.text
