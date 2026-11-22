"""Fixtures of the core's tests: a real PostgreSQL with pgvector, and per-test isolation.

The database is a throwaway ``pgvector/pgvector:pg17`` container (testcontainers), started once
per test session and migrated to the latest revision. Tests reach it through:

* ``database_url``: the asyncpg URL of that database (use it to configure the code under test);
* ``settings``: a ``CoreSettings`` that points at it;
* ``session``: an ``AsyncSession`` whose work is rolled back at the end of the test, even when the
  code under test commits (it joins an external transaction and turns commits into savepoints);
* ``session_factory``: independent sessions with their own connections and transactions, for
  tests that need real commits or concurrency. What they commit is wiped after the test;
* ``tools_admin`` and ``tools_base``: the tools' own database of a test (``toolsdb``), made as the
  core makes it under a name of its own, the core's hand on it and the way programs reach it. It
  is dropped after the test, and the roles of the tools' databases with it (they are the server's).

The gatekeeper side of the model tests has its own fixtures, at the end of this file: ``gk_client``
(a ``GatekeeperClient`` at ``http://gatekeeper.test``, with nothing mocked) and ``wire_for(kind)``
(the model of a provider of that kind, and the scripted relay it talks to); see ``llm_support.py``.
For the pages that stream (the chat): ``live_base_url`` (the ``app`` fixture served by a real
uvicorn server on 127.0.0.1, on a free port: ``httpx.ASGITransport`` buffers responses, so it can
show neither a stream nor a reader who leaves) and ``live_client`` (a client of it, over TCP); see
``chat_support.py``. A live app needs sessions that commit for real: override ``app`` with
``app_factory()``.
For the pages of a person who is not the owner (the account page): ``adult_account`` (an adult,
"Alex"), ``adult_client`` and ``second_adult_client`` (two browsers of that adult, so two
sessions), all on the ``app`` fixture and its shared ``session``.
For the passkeys (task 12): ``soft_authenticator`` (a software authenticator that signs with real
ES256 keys, see ``passkey_support.py``), and two browsers that are not logged in, on the ``app``
fixture: ``client_by_ip`` (the page opened by IP address, ``https://192.168.1.10:8443``) and
``client_by_name`` (opened by host name, ``https://localhost:8443``, the stack's default).
For the pages that ask the gatekeeper for secrets: ``gk_mock`` (its internal API, scripted, see
``gatekeeper_support.py``), ``owner_client`` (the owner, logged in, on an application whose
gatekeeper is that mock) and ``use_model`` (what the application's model factory builds).
``settings`` and ``offline_settings`` point at a core token file, which the application reads when
it starts.

For talking with Domo as a person of the household (tasks 5 and after), at the end of this file:
``chat_server`` (one live server per test, with sessions that commit, the test clock set to the
real time, the gatekeeper client ``gk_client`` and a model slot), the pilots ``owner_chat``,
``adult_chat``, ``child_chat`` and ``guest_chat`` (see ``chat_support.ChatPilot``), the browsers
``live_owner_client``, ``live_adult_client``, ``live_child_client`` and ``live_guest_client``
(each logged in as that person, on the same server), and ``add_member`` (a person the server
sees: committed). They have names of their own: ``owner_client`` above is another application.
For the confirmations (tasks 6 and after): ``display_name_of``, ``confirmation_status``,
``confirmation_ask`` (what a request asks, who asked it and why), ``set_setting``, ``set_role``,
``current_settings``, ``last_audit_entry`` and ``recent_replies``, which read or change the
database as their names say, and ``grant`` (a right of the owner, written straight into
``policy_rights``). The browsers of the pilots wait, after each answer, for the wakes of Domo it
started (``ChatServer.browser``). For the household (task 9): ``create_invitation_for_test`` (an
invitation of the owner, on the shared ``session``), and ``count_invitations``, ``audit_dump``,
``stored_text`` and ``person_row``, which read what the database committed. For the notes (task 10):
``a_note_for_owner`` (a note from the adult to the owner), ``leave_note`` (any note, written
straight into the database), ``count_notes`` and ``stored_notes``. For the model's key (task 13):
``audit_actions`` (the actions of the committed audit log); the pilots' provider has a key in the
vault (``ChatServer.provider_id``), and ``gk_mock`` lets the pilots' own traffic through. For a
flood of logins (task 14): ``slow_hash`` (argon2 slowed down, counted and, if asked, held: see
``web_support.HashProbe``), and ``max_checked_out_connections`` and ``checked_out_connections``
(the connections of the database an ``app_factory()`` application holds, the most at once or now).
For the chains of actions: ``count_confirmations``, ``steps_rows`` (the
steps of a chain, in order) and ``chain_basis_of`` (what a chain remembers of the one who asked),
which read what the database committed; ``tool_context`` (a ``ToolContext`` of the owner on the
pilots' server, to call a tool by hand) and ``invite_for_test`` (an open invitation the server
sees); for a chain that waits for an arrival, ``join_with_invitation`` (someone joins with an
invitation) and ``upkeep_pass`` (one pass of the arrivals, as the application makes it).

Isolation rule: after any test that used ``database_url`` (directly or through another fixture),
every table is emptied again (``setup_state`` is reset to its initial values, and its row is
put back if the test deleted it), so a test never sees what another one committed.

The texts: before any test that uses the database, the house's version of the texts is written in
English, each text its meaning as it is (``house_texts``, automatic; the house's language is
English, ``house.language``'s default). The pages of an installed instance so show the English
sentences of the catalog of meanings, which ``t(key, "en")`` reads. A module about the writing of
the texts themselves, or about an installation, says ``house_texts`` is nothing (a fixture of its
own of that name): its instance has no text yet.

Docker note: the container is a Linux one. Where the Docker engine only runs Windows containers
(the Windows runners of GitHub Actions), the database tests are skipped explicitly. Without a
running Docker engine they fail with a message that says so.

Windows note: asyncpg works with the default (Proactor) event loop, so no loop policy is set.
"""

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest
import respx
from chat_support import (
    GUEST_STAY,
    WAIT_SECONDS,
    ChatPilot,
    ChatServer,
    Member,
    last_audit,
    live_http_client,
    live_server,
)
from domo_common.meanings import all_keys, group_of, meaning, meaning_hash, written_groups
from domo_core.accounts import passwords
from domo_core.accounts.models import Account, Person
from domo_core.accounts.passkey_routes import new_passkey_options_throttle
from domo_core.accounts.passwords import hash_password
from domo_core.accounts.throttle import LoginThrottle
from domo_core.audit.models import AuditEntry
from domo_core.chat.models import Conversation, Message
from domo_core.config import CoreSettings
from domo_core.confirmations.arrivals import arrivals_pass
from domo_core.confirmations.models import Confirmation, ConfirmationStep
from domo_core.confirmations.steps import steps_of
from domo_core.db import create_engine, make_session_factory
from domo_core.gatekeeper_client import GatekeeperClient, new_http_client
from domo_core.household.models import Invitation
from domo_core.household.service import Joining, accept_invitation, create_invitation
from domo_core.llm.adapters import anthropic as anthropic_adapter
from domo_core.llm.provider_kinds import ProviderKind
from domo_core.llm.types import ChatModel
from domo_core.migrate import upgrade_to_head
from domo_core.notes.models import Note
from domo_core.policy.catalog import DEFAULT_POLICY
from domo_core.policy.models import PolicyRight
from domo_core.policy.roles import Actor, as_role
from domo_core.policy.settings import (
    PolicySettings,
    load_policy_settings,
    update_policy_setting,
)
from domo_core.proofs.service import agree
from domo_core.setup_flow.code import rotate_setup_code
from domo_core.setup_flow.middleware import mark_setup_done
from domo_core.setup_flow.owner_routes import new_setup_code_throttle
from domo_core.texts.scheduler import TextWriting, reader_models
from domo_core.tools.context import ToolContext
from domo_core.tools.registry import TOOLS
from domo_core.toolsdb.setup import ALL_ROLE, ToolsAdmin, prepare_tools_database
from domo_core.toolsdb.store import ToolsBase
from domo_core.web.app import arrival_deps, create_app
from domo_core.web.deps import (
    get_clock,
    get_gatekeeper,
    get_model_factory,
    get_model_lister,
    get_sandbox,
)
from fakes import FakeModelFactory, FakeModelLister, fake_factory
from fastapi import FastAPI
from gatekeeper_support import GatekeeperMock
from llm_support import CORE_TOKEN, GATEKEEPER_URL, Httpx2Wire, HttpxWire, Wire
from passkey_support import SoftAuthenticator
from sqlalchemy import event, func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.docker_client import DockerClient
from web_support import (
    ADULT_PASSWORD,
    ADULT_USERNAME,
    OWNER_PASSWORD,
    OWNER_USERNAME,
    CountingExecutor,
    HashProbe,
    SlowHasher,
    build_test_app,
    log_in,
)
from web_support import audit_actions as audit_actions_in

POSTGRES_IMAGE = "pgvector/pgvector:pg17"
# Tables the reset keeps: the migration's bookkeeping, and the single seeded row of setup_state.
_RESET_KEEP = ("alembic_version", "setup_state")
# The URL of the migrated database, once the session fixture has made it (empty before, and when
# the database tests are skipped: then there is nothing to reset).
_migrated_database_urls: list[str] = []


def run_async[T](coroutine: Coroutine[Any, Any, T]) -> T:
    """Run ``coroutine`` to completion on a fresh event loop in a worker thread.

    The fixtures that are not async (container, migration, reset) use it, so that they never
    touch the event loop pytest-asyncio gives the test.
    """
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coroutine).result()


def _asyncpg_dsn(database_url: str) -> str:
    """The same database as a plain DSN (asyncpg itself does not know the SQLAlchemy driver)."""
    return make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """No DOMO_ variable of the machine running the tests may leak into the settings."""
    for name in list(os.environ):
        if name.upper().startswith("DOMO_"):
            monkeypatch.delenv(name)


@pytest.fixture(scope="session")
def postgres_container() -> Iterator[PostgresContainer]:
    try:
        engine_os = DockerClient().client.info().get("OSType")
    except Exception as error:
        pytest.fail(f"The database tests need a running Docker engine: {error}", pytrace=False)
    if engine_os != "linux":
        pytest.skip(f"the database tests need a Linux container engine (found: {engine_os})")
    with PostgresContainer(POSTGRES_IMAGE, driver="asyncpg") as container:
        yield container


@pytest.fixture(scope="session")
def database_url(postgres_container: PostgresContainer) -> str:
    """The asyncpg URL of the test database, migrated to the latest revision."""
    url = postgres_container.get_connection_url()
    upgrade_to_head(url)
    _migrated_database_urls.append(url)
    return url


async def _reset_database(database_url: str) -> None:
    connection = await asyncpg.connect(_asyncpg_dsn(database_url))
    try:
        # Fail loudly instead of hanging when a test leaked a connection that holds a lock.
        await connection.execute("SET lock_timeout = '10s'")
        rows = await connection.fetch(
            "SELECT quote_ident(tablename) AS name FROM pg_tables "
            "WHERE schemaname = 'public' AND tablename <> ALL($1::text[])",
            list(_RESET_KEEP),
        )
        if rows:
            names = ", ".join(row["name"] for row in rows)
            await connection.execute(f"TRUNCATE {names} RESTART IDENTITY CASCADE")
        # An upsert, not an UPDATE: a test that deleted the row must not leave the following
        # ones without it (an UPDATE would silently match nothing).
        await connection.execute(
            "INSERT INTO setup_state (id) VALUES (1) ON CONFLICT (id) DO UPDATE SET "
            "setup_code_hash = NULL, code_created_at = NULL, "
            "owner_created_at = NULL, model_configured_at = NULL"
        )
    finally:
        await connection.close()


# The model said to have written the house's texts of the tests.
HOUSE_TEXTS_MODEL = "house-texts-of-the-tests"


async def _write_house_texts(database_url: str) -> None:
    """The house's version of the texts, in English, each text its meaning as it is: committed,
    in one statement."""
    groups = frozenset(written_groups())
    keys = [key for key in all_keys() if group_of(key) in groups]
    connection = await asyncpg.connect(_asyncpg_dsn(database_url))
    try:
        await connection.execute(
            "INSERT INTO written_texts (language, key, text, meaning_hash, model_id, written_at) "
            "SELECT 'en', key, text, hash, $4, now() "
            "FROM unnest($1::text[], $2::text[], $3::text[]) AS t(key, text, hash)",
            keys,
            [meaning(key) for key in keys],
            [meaning_hash(key) for key in keys],
            HOUSE_TEXTS_MODEL,
        )
    finally:
        await connection.close()


@pytest.fixture(autouse=True)
def house_texts(request: pytest.FixtureRequest) -> None:
    """Before a test that uses the database: the house's version of the texts, in English (the
    house's language by default), each text its meaning as it is, as the writer of the tests
    writes it (see the module docstring). A module whose instance must have no text yet defines
    a fixture of this name that does nothing."""
    if "database_url" in request.fixturenames:
        run_async(_write_house_texts(request.getfixturevalue("database_url")))


@pytest.fixture(autouse=True)
def _reset_database_after_test(request: pytest.FixtureRequest) -> Iterator[None]:
    """Wipe what a test committed, when it used the database (and only then: no Docker needed
    by the tests that do not touch it)."""
    yield
    if _migrated_database_urls and "database_url" in request.fixturenames:
        run_async(_reset_database(_migrated_database_urls[0]))


@pytest.fixture
def core_token_file(tmp_path: Path) -> Path:
    """The file the core reads its token from (the gatekeeper's other end of the shared secret)."""
    path = tmp_path / "core_token"
    path.write_text(CORE_TOKEN + "\n", encoding="ascii")
    return path


@pytest.fixture
def settings(database_url: str, core_token_file: Path) -> CoreSettings:
    """The core's settings, pointing at the test database and at a core token file."""
    return CoreSettings(database_url_override=database_url, core_token_file=core_token_file)


@pytest.fixture
async def engine(settings: CoreSettings) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(settings)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Independent sessions on the test database: each one commits for real."""
    return make_session_factory(engine)


@pytest.fixture
async def session(
    engine: AsyncEngine, session_factory: async_sessionmaker[AsyncSession]
) -> AsyncIterator[AsyncSession]:
    """A session whose work never outlives the test, whatever the code under test commits.

    The session joins a transaction that the fixture owns ("create_savepoint" mode): a commit
    inside the test only releases a savepoint, and the outer transaction is rolled back at the end.
    """
    async with engine.connect() as connection:
        outer = await connection.begin()
        test_session = session_factory(bind=connection, join_transaction_mode="create_savepoint")
        try:
            yield test_session
        finally:
            await test_session.close()
            await outer.rollback()


@pytest.fixture
def list_tables() -> Callable[[str], Awaitable[set[str]]]:
    """``await list_tables(url)``: the names of the tables of the public schema."""

    async def _list_tables(url: str) -> set[str]:
        engine = create_engine(CoreSettings(database_url_override=url))
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                )
                return {row[0] for row in result}
        finally:
            await engine.dispose()

    return _list_tables


async def _execute_autocommit(database_url: str, statement: str) -> None:
    engine = create_engine(CoreSettings(database_url_override=database_url))
    engine = engine.execution_options(isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(text(statement))
    finally:
        await engine.dispose()


@pytest.fixture
def scratch_database_url(database_url: str) -> Iterator[str]:
    """An empty, unmigrated database next to the test one, dropped after the test."""
    name = f"scratch_{uuid.uuid4().hex[:8]}"
    run_async(_execute_autocommit(database_url, f'CREATE DATABASE "{name}"'))
    try:
        yield make_url(database_url).set(database=name).render_as_string(hide_password=False)
    finally:
        run_async(_execute_autocommit(database_url, f'DROP DATABASE "{name}" WITH (FORCE)'))


async def _drop_tools_database(database_url: str, name: str) -> None:
    """The tools' database ``name`` dropped, and every role of the tools' databases with it (they
    are the server's, not the database's: a test's must not reach the next)."""
    connection = await asyncpg.connect(_asyncpg_dsn(database_url))
    try:
        await connection.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        roles = await connection.fetch(
            "SELECT rolname FROM pg_roles WHERE rolname LIKE 'jt\\_%' ORDER BY rolname = $1",
            ALL_ROLE,
        )
        for role in roles:
            await connection.execute(f'DROP ROLE "{role["rolname"]}"')
    finally:
        await connection.close()


@pytest.fixture
def tools_admin(database_url: str) -> Iterator[ToolsAdmin]:
    """The tools' own database of a test, made as the core makes it (``prepare_tools_database``)
    under a name of its own, and the core's hand on it. Dropped after the test, and every role
    of the tools' databases with it."""
    name = f"tools_{uuid.uuid4().hex[:8]}"
    dsn = _asyncpg_dsn(database_url)
    run_async(
        prepare_tools_database(dsn, core_database=make_url(database_url).database or "", name=name)
    )
    try:
        yield ToolsAdmin(dsn, name)
    finally:
        run_async(_drop_tools_database(database_url, name))


@pytest.fixture
async def tools_base(tools_admin: ToolsAdmin) -> AsyncIterator[ToolsBase]:
    """The tools' database of the test (``tools_admin``) as the programs reach it: its pools are
    closed after the test."""
    base = ToolsBase(tools_admin)
    try:
        yield base
    finally:
        await base.close()


# --- row factories -------------------------------------------------------------------------
# Each one adds a row to the ``session`` fixture and flushes it, so ids and defaults are set.


@pytest.fixture
def add_person(session: AsyncSession) -> Callable[..., Awaitable[Person]]:
    async def _add_person(display_name: str = "A", role: str = "owner") -> Person:
        person = Person(display_name=display_name, role=role)
        session.add(person)
        await session.flush()
        return person

    return _add_person


@pytest.fixture
def add_account(session: AsyncSession) -> Callable[..., Awaitable[Account]]:
    async def _add_account(
        person: Person, username: str = "owner", password_hash: str = "hash"
    ) -> Account:
        account = Account(person_id=person.id, username=username, password_hash=password_hash)
        session.add(account)
        await session.flush()
        await agree(session, person.id, "password", proposed_by="code", now=datetime.now(UTC))
        return account

    return _add_account


@pytest.fixture
def add_conversation(
    session: AsyncSession, add_person: Callable[..., Awaitable[Person]]
) -> Callable[..., Awaitable[Conversation]]:
    async def _add_conversation(person: Person | None = None) -> Conversation:
        conversation = Conversation(person_id=(person or await add_person()).id)
        session.add(conversation)
        await session.flush()
        return conversation

    return _add_conversation


@pytest.fixture
def add_message(session: AsyncSession) -> Callable[..., Awaitable[Message]]:
    async def _add_message(conversation: Conversation, **overrides: str | bool) -> Message:
        values: dict[str, str | bool] = {
            "role": "user",
            "content": "hello",
            "status": "complete",
            "provenance": "person",
        } | overrides
        message = Message(conversation_id=conversation.id, **values)
        session.add(message)
        await session.flush()
        return message

    return _add_message


@pytest.fixture
def make_audit_entry() -> Callable[..., AuditEntry]:
    """An audit row with a made-up (not verifiable) chain: ``index`` makes its hash unique."""

    def _make_audit_entry(index: int, **overrides: Any) -> AuditEntry:
        values: dict[str, Any] = {
            "actor": "system",
            "action": "test.event",
            "details": {"i": index},
            "prev_hash": "0" * 64,
            "hash": f"{index:064x}",
        } | overrides
        return AuditEntry(**values)

    return _make_audit_entry


@pytest.fixture
def count_persons() -> Callable[[AsyncSession], Awaitable[int]]:
    async def _count_persons(db_session: AsyncSession) -> int:
        result = await db_session.execute(select(func.count()).select_from(Person))
        return result.scalar_one()

    return _count_persons


# --- accounts and the web app --------------------------------------------------------------


class FakeClock:
    """A settable clock (``Callable[[], datetime]``, UTC): code under test reads it on each call."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2030, 1, 1, 12, 0, tzinfo=UTC))


@pytest.fixture(scope="session")
def owner_password_hash() -> str:
    """The hash of ``OWNER_PASSWORD``, computed once: hashing is slow on purpose."""
    return hash_password(OWNER_PASSWORD)


@pytest.fixture
async def account(
    add_person: Callable[..., Awaitable[Person]], add_account: Callable[..., Awaitable[Account]]
) -> Account:
    """An account (and its person) whose password nobody knows."""
    return await add_account(await add_person())


@pytest.fixture
async def owner_account(
    add_person: Callable[..., Awaitable[Person]],
    add_account: Callable[..., Awaitable[Account]],
    owner_password_hash: str,
) -> Account:
    """The owner's account: username ``OWNER_USERNAME``, password ``OWNER_PASSWORD``."""
    person = await add_person("Owner", "owner")
    return await add_account(person, OWNER_USERNAME, owner_password_hash)


@pytest.fixture
def offline_settings(core_token_file: Path) -> CoreSettings:
    """Settings that point at a database nobody listens on: for the tests that never use it."""
    return CoreSettings(
        database_url_override="postgresql+asyncpg://domo:unused@localhost:1/db",
        core_token_file=core_token_file,
    )


@pytest.fixture
def offline_app(offline_settings: CoreSettings, clock: FakeClock) -> FastAPI:
    """The web app without a database: only the routes that do not touch it can be called
    (the login page, static files, refused requests). Runs without Docker. The first launch
    counts as over, so that the setup gate does not look for the database."""
    app = create_app(offline_settings)
    app.state.login_throttle = LoginThrottle(clock=clock)
    mark_setup_done(app)
    return app


@pytest.fixture
def app(settings: CoreSettings, session: Any, clock: FakeClock) -> FastAPI:
    """The web app on the test database, as an installed instance (the first launch is over):
    every request uses the rolled-back ``session``. Test modules about the first launch override
    this fixture with ``build_test_app`` alone, which leaves the setup gate active."""
    app = build_test_app(settings, session, clock)
    mark_setup_done(app)
    return app


def make_client(
    app: FastAPI,
    origin: str | None = "https://testserver",
    base_url: str = "https://testserver",
    address: str = "127.0.0.1",
) -> httpx.AsyncClient:
    """A browser-like client of ``app`` (``origin=None``: it sends no Origin header). The page is
    opened at ``base_url``, whose host and port are the request's ``Host``; ``address`` is the
    client's address as the application sees it (behind the trusted edge, the real one)."""
    headers = {} if origin is None else {"origin": origin}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(address, 123)),
        base_url=base_url,
        headers=headers,
    )


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with make_client(app) as http_client:
        yield http_client


@pytest.fixture
async def offline_client(offline_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with make_client(offline_app) as http_client:
        yield http_client


@pytest.fixture
async def live_base_url(app: FastAPI) -> AsyncIterator[str]:
    """The URL of ``app`` served by a real uvicorn server (127.0.0.1, free port) for the test.

    The turns of the replies (and the wakes) still running when the test ends are stopped with the
    server (``app.state.wakes``): the lifespan, which stops them in production, does not run
    here."""
    async with live_server(app) as base_url:
        try:
            yield base_url
        finally:
            await app.state.wakes.stop(WAIT_SECONDS)


@pytest.fixture
async def live_client(live_base_url: str) -> AsyncIterator[httpx.AsyncClient]:
    """A browser-like client of the live server, over TCP (it sends a same-origin Origin header;
    log in with ``chat_support.log_in_live``)."""
    async with live_http_client(live_base_url) as http_client:
        yield http_client


@pytest.fixture
async def second_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Another browser on the same application: its own cookies."""
    async with make_client(app) as http_client:
        yield http_client


@pytest.fixture
def client_factory() -> Callable[..., httpx.AsyncClient]:
    """``client_factory(app, origin=...)``: a client with another Origin header (or none).
    Use it with ``async with``."""
    return make_client


@pytest.fixture
def app_factory(
    settings: CoreSettings,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FakeClock,
) -> Callable[[], FastAPI]:
    """``app_factory()``: a new application with a session per request, as in production (what a
    request commits is committed for real), and the first launch not over. For the tests of
    concurrency and of what is really committed; use ``client_factory`` to talk to it.

    Opening the chat page offers Domo no arrival turn there (``app.state.arrival_turns``,
    ``conversation.starts``): the tests count what their own turns do, and an arrival would be one
    more in the background. ``test_arrival.py`` puts them back."""

    def _app_factory() -> FastAPI:
        app = create_app(settings)
        app.state.login_throttle = LoginThrottle(clock=clock)
        app.state.password_check_throttle = LoginThrottle(clock=clock)
        app.state.setup_code_throttle = new_setup_code_throttle(clock=clock)
        app.state.invitation_throttle = LoginThrottle(clock=clock)
        app.state.passkey_login_throttle = LoginThrottle(clock=clock)
        app.state.passkey_options_throttle = new_passkey_options_throttle(clock=clock)
        app.state.session_factory = session_factory
        app.state.arrival_turns = None
        return app

    return _app_factory


@pytest.fixture
async def setup_code(session_factory: async_sessionmaker[AsyncSession]) -> str:
    """The one-time setup code of a fresh instance, committed (so that every connection, and
    every application on the test database, sees it)."""
    async with session_factory() as writer:
        code = await rotate_setup_code(writer)
        await writer.commit()
    assert code is not None
    return code


@pytest.fixture
def add_owner_in_database(
    session_factory: async_sessionmaker[AsyncSession], owner_password_hash: str
) -> Callable[[], Awaitable[Account]]:
    """``await add_owner_in_database()``: the owner's account, committed for real (for the tests
    that run the application with a session per request, like production). The first launch is
    recorded as over, as it is for any instance that has an owner."""

    async def _add_owner_in_database() -> Account:
        async with session_factory() as writer:
            person = Person(display_name="Owner", role="owner")
            writer.add(person)
            await writer.flush()
            account = Account(
                person_id=person.id, username=OWNER_USERNAME, password_hash=owner_password_hash
            )
            writer.add(account)
            await writer.flush()
            await agree(writer, person.id, "password", proposed_by="code", now=datetime.now(UTC))
            await writer.execute(
                text("UPDATE setup_state SET owner_created_at = now(), model_configured_at = now()")
            )
            await writer.commit()
            return account

    return _add_owner_in_database


# --- argon2 and the connections of the database (task 14) ---------------------------------------


@pytest.fixture
def slow_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[..., AbstractAsyncContextManager[HashProbe]]:
    """``async with slow_hash(seconds=0.5) as probe:``: inside the block, every argon2 hash and
    verification takes ``seconds`` more, and ``probe`` (``web_support.HashProbe``) counts them;
    with ``gated=True`` each one is also held until ``probe.release()``. The block releases them
    on its way out, whatever happened, and puts the real argon2 back."""

    @asynccontextmanager
    async def _slow_hash(seconds: float = 0.0, *, gated: bool = False) -> AsyncIterator[HashProbe]:
        probe = HashProbe(seconds, gated=gated)
        real_hasher = passwords._hasher
        real_threads = passwords._argon2_threads
        monkeypatch.setattr(passwords, "_hasher", SlowHasher(real_hasher, probe))
        monkeypatch.setattr(passwords, "_argon2_threads", CountingExecutor(real_threads, probe))
        try:
            yield probe
        finally:
            probe.release()
            monkeypatch.setattr(passwords, "_hasher", real_hasher)
            monkeypatch.setattr(passwords, "_argon2_threads", real_threads)

    return _slow_hash


class _CheckedOutConnections:
    """How many connections of a pool are checked out now, and the most that were at once."""

    def __init__(self) -> None:
        self.now = 0
        self.most = 0

    def checkout(self, *_: object) -> None:
        self.now += 1
        self.most = max(self.most, self.now)

    def checkin(self, *_: object) -> None:
        self.now -= 1


@pytest.fixture
def _watched_connections(engine: AsyncEngine) -> Iterator[_CheckedOutConnections]:
    """Watches the pool of the test engine (the one of ``session_factory`` and ``app_factory()``)
    from the moment a test asks for it."""
    watched = _CheckedOutConnections()
    pool = engine.sync_engine.pool
    event.listen(pool, "checkout", watched.checkout)
    event.listen(pool, "checkin", watched.checkin)
    try:
        yield watched
    finally:
        event.remove(pool, "checkout", watched.checkout)
        event.remove(pool, "checkin", watched.checkin)


def _uses_the_test_engine(app: FastAPI, engine: AsyncEngine) -> None:
    factory = app.state.session_factory
    assert isinstance(factory, async_sessionmaker) and factory.kw.get("bind") is engine, (
        "the application must use the test engine's pool: build it with app_factory()"
    )


@pytest.fixture
def max_checked_out_connections(
    engine: AsyncEngine, _watched_connections: _CheckedOutConnections
) -> Callable[[FastAPI], int]:
    """``max_checked_out_connections(app)``: the most connections of the database the application
    held at once since the test began (``app`` from ``app_factory()``, whose sessions come from
    the test engine's pool)."""

    def _max_checked_out_connections(app: FastAPI) -> int:
        _uses_the_test_engine(app, engine)
        return _watched_connections.most

    return _max_checked_out_connections


@pytest.fixture
def checked_out_connections(
    engine: AsyncEngine, _watched_connections: _CheckedOutConnections
) -> Callable[[FastAPI], int]:
    """``checked_out_connections(app)``: how many connections of the database the application
    holds right now (``app`` from ``app_factory()``)."""

    def _checked_out_connections(app: FastAPI) -> int:
        _uses_the_test_engine(app, engine)
        return _watched_connections.now

    return _checked_out_connections


# --- the gatekeeper and the model adapters -------------------------------------------------


@pytest.fixture
async def gk_client() -> AsyncIterator[GatekeeperClient]:
    """A client of the gatekeeper at ``http://gatekeeper.test`` with the token ``CORE_TOKEN``.
    Nothing is mocked: use ``respx_mock`` (or ``wire_for``) to say what the gatekeeper answers."""
    async with new_http_client() as http:
        yield GatekeeperClient(GATEKEEPER_URL, CORE_TOKEN, http)


@pytest.fixture
def wire_for(
    gk_client: GatekeeperClient, respx_mock: respx.MockRouter
) -> Callable[[ProviderKind], Wire]:
    """``wire_for(kind)``: the model of a provider of that kind, built the way the application
    builds it, and the fake relay it talks to (see ``llm_support.Wire``). What the Anthropic
    adapter learnt about its models, which it keeps for the life of the process, is forgotten
    first."""
    anthropic_adapter.forget_what_models_want()

    def _wire_for(kind: ProviderKind) -> Wire:
        if kind == "google":
            return HttpxWire(kind, gk_client, respx_mock)
        return Httpx2Wire(kind, gk_client)

    return _wire_for


@pytest.fixture
def gk_mock() -> Iterator[GatekeeperMock]:
    """The gatekeeper's internal API at ``http://gatekeeper.test``, scripted (see
    ``gatekeeper_support.GatekeeperMock``). A call it has no route for fails the test."""
    with respx.mock(base_url=GATEKEEPER_URL, assert_all_called=False) as router:
        yield GatekeeperMock(router)


@pytest.fixture
async def owner_client(
    app: FastAPI,
    owner_account: Account,
    gk_client: GatekeeperClient,
    gk_mock: GatekeeperMock,
) -> AsyncIterator[httpx.AsyncClient]:
    """A browser logged in as the owner, on an application whose gatekeeper client talks to
    ``gk_mock`` (the test application never runs its lifespan, so it has none of its own)."""
    app.dependency_overrides[get_gatekeeper] = lambda: gk_client
    async with make_client(app) as http_client:
        await log_in(http_client)
        yield http_client


@pytest.fixture
async def adult_account(
    add_person: Callable[..., Awaitable[Person]],
    add_account: Callable[..., Awaitable[Account]],
    owner_password_hash: str,
) -> Account:
    """An adult of the household, "Alex": username ``ADULT_USERNAME``, password
    ``ADULT_PASSWORD`` (on the shared ``session``, like ``owner_account``)."""
    person = await add_person("Alex", "adult")
    return await add_account(person, ADULT_USERNAME, owner_password_hash)


@pytest.fixture
async def adult_client(app: FastAPI, adult_account: Account) -> AsyncIterator[httpx.AsyncClient]:
    """A browser logged in as ``adult_account``, on the ``app`` fixture (the shared ``session``).
    Not the pilots' ``live_adult_client``, which is on a live server."""
    async with make_client(app) as http_client:
        await log_in(http_client, ADULT_USERNAME, ADULT_PASSWORD)
        yield http_client


@pytest.fixture
async def second_adult_client(
    app: FastAPI, adult_account: Account, adult_client: httpx.AsyncClient
) -> AsyncIterator[httpx.AsyncClient]:
    """Another browser of the same adult: its own cookies, so its own session."""
    async with make_client(app) as http_client:
        await log_in(http_client, ADULT_USERNAME, ADULT_PASSWORD)
        yield http_client


# The addresses the passkey tests open the page with (the default site addresses of the stack are
# "localhost": an address of the example settings stands for an IP address).
BY_IP_URL = "https://192.168.1.10:8443"
BY_NAME_URL = "https://localhost:8443"


@pytest.fixture
def soft_authenticator() -> SoftAuthenticator:
    """A software authenticator (ES256, real signatures) that answers like a browser on
    ``https://localhost:8443``."""
    return SoftAuthenticator(BY_NAME_URL)


@pytest.fixture
async def client_by_ip(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """A browser that opened the page by IP address (its ``Host`` is an IPv4 address and port),
    not logged in."""
    async with make_client(app, origin=BY_IP_URL, base_url=BY_IP_URL) as http_client:
        yield http_client


@pytest.fixture
async def client_by_name(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """A browser that opened the page by host name (``localhost:8443``), not logged in."""
    async with make_client(app, origin=BY_NAME_URL, base_url=BY_NAME_URL) as http_client:
        yield http_client


@pytest.fixture
def model_lister() -> FakeModelLister:
    """The lister of models of the tests (``use_model`` and ``chat_server`` install it): set its
    ``models`` (``()`` for a server that gives no list) or its ``error`` (a key the provider
    refuses)."""
    return FakeModelLister()


@pytest.fixture
def use_model(
    app: FastAPI, model_lister: FakeModelLister
) -> Callable[[ChatModel], FakeModelFactory]:
    """``use_model(model)``: the application builds ``model`` for every provider and model id (a
    scripted model, instead of the SDKs talking to the gatekeeper's relay) and lists the models of
    ``model_lister``. Returns the factory, which notes what it was asked for (``requested``)."""

    def _use_model(model: ChatModel) -> FakeModelFactory:
        factory = fake_factory(model)
        app.dependency_overrides[get_model_factory] = lambda: factory
        app.dependency_overrides[get_model_lister] = lambda: model_lister
        return factory

    return _use_model


# --- the household talking with Domo (the chat pilot) -----------------------------------------


@pytest.fixture
def live_clock(clock: FakeClock) -> FakeClock:
    """The test clock, set to the real time: on a live server the database stamps rows with its
    own clock, and the two must agree. It is the ``clock`` fixture itself (moving one moves the
    other)."""
    clock.advance(datetime.now(UTC) - clock())
    return clock


@pytest.fixture
def add_member(
    session_factory: async_sessionmaker[AsyncSession],
    owner_password_hash: str,
    live_clock: FakeClock,
) -> Callable[..., Awaitable[Person]]:
    """``await add_member(display_name, role, username=None)``: a person of the household,
    committed (so that the live server sees them). With ``username`` they get an account whose
    password is ``chat_support.MEMBER_PASSWORD``. A guest's stay ends ``GUEST_STAY`` from now; the
    owner's arrival ends the first launch, as it does for a real instance."""

    async def _add_member(display_name: str, role: str, *, username: str | None = None) -> Person:
        async with session_factory() as writer:
            person = Person(
                display_name=display_name,
                role=role,
                expires_at=live_clock() + GUEST_STAY if role == "guest" else None,
            )
            writer.add(person)
            await writer.flush()
            if username is not None:
                writer.add(
                    Account(
                        person_id=person.id, username=username, password_hash=owner_password_hash
                    )
                )
                await writer.flush()
                await agree(
                    writer, person.id, "password", proposed_by="code", now=datetime.now(UTC)
                )
            if role == "owner":
                await writer.execute(text("UPDATE setup_state SET owner_created_at = now()"))
            await writer.commit()
            return person

    return _add_member


@pytest.fixture
async def chat_server(
    app_factory: Callable[[], FastAPI],
    live_clock: FakeClock,
    gk_client: GatekeeperClient,
    session_factory: async_sessionmaker[AsyncSession],
    owner_member: Member,
    model_lister: FakeModelLister,
) -> AsyncIterator[ChatServer]:
    """The live server of the pilots: an installed instance with one provider, sessions that
    commit, the test clock (at the real time), the gatekeeper client ``gk_client`` (request
    ``gk_mock`` to script it) and the model slot of ``ChatServer`` (a model that says "ok" until
    a test puts another one there). People's texts are written in the background as in
    production (``reader_models``), by the server's ``text_model``.

    A wake of Domo after a button (``app.state.wakes``) still running when the test ends is
    stopped with the server: the lifespan, which stops them in production, does not run here."""
    app = app_factory()
    mark_setup_done(app)
    server = ChatServer(app=app, clock=live_clock, session_factory=session_factory)
    app.dependency_overrides[get_clock] = lambda: live_clock
    app.dependency_overrides[get_gatekeeper] = lambda: gk_client
    app.dependency_overrides[get_sandbox] = lambda: server.sandbox
    app.dependency_overrides[get_model_factory] = server.model_factory
    app.dependency_overrides[get_model_lister] = lambda: model_lister
    app.state.text_writing = TextWriting(
        app.state.wakes,
        session_factory,
        reader_models(session_factory, server.text_factory, live_clock),
        live_clock,
    )
    await server.add_provider(owner_member.person.id)
    async with live_server(app) as base_url:
        server.base_url = base_url
        try:
            yield server
        finally:
            await app.state.wakes.stop(WAIT_SECONDS)


async def _member(
    add_member: Callable[..., Awaitable[Person]], display_name: str, role: str, username: str
) -> Member:
    return Member(await add_member(display_name, role, username=username), username)


@pytest.fixture
async def owner_member(add_member: Callable[..., Awaitable[Person]]) -> Member:
    """The owner of the pilots' household: "Owner", username ``OWNER_USERNAME``."""
    return await _member(add_member, "Owner", "owner", OWNER_USERNAME)


@pytest.fixture
async def adult_member(add_member: Callable[..., Awaitable[Person]]) -> Member:
    """An adult of the pilots' household: "Alex"."""
    return await _member(add_member, "Alex", "adult", "alex")


@pytest.fixture
async def child_member(add_member: Callable[..., Awaitable[Person]]) -> Member:
    """A child of the pilots' household: "Sam"."""
    return await _member(add_member, "Sam", "child", "sam")


@pytest.fixture
async def guest_member(add_member: Callable[..., Awaitable[Person]]) -> Member:
    """A guest of the pilots' household: "Robin", staying ``GUEST_STAY``."""
    return await _member(add_member, "Robin", "guest", "robin")


@asynccontextmanager
async def _logged_in(server: ChatServer, member: Member) -> AsyncIterator[httpx.AsyncClient]:
    # It waits for the wakes of Domo after each answer (``ChatServer.browser``).
    async with server.browser(member) as client:
        yield client


@pytest.fixture
async def live_owner_client(
    chat_server: ChatServer, owner_member: Member
) -> AsyncIterator[httpx.AsyncClient]:
    """The owner's browser on the pilots' server, logged in."""
    async with _logged_in(chat_server, owner_member) as client:
        yield client


@pytest.fixture
async def live_adult_client(
    chat_server: ChatServer, adult_member: Member
) -> AsyncIterator[httpx.AsyncClient]:
    """The adult's browser on the pilots' server, logged in."""
    async with _logged_in(chat_server, adult_member) as client:
        yield client


@pytest.fixture
async def live_child_client(
    chat_server: ChatServer, child_member: Member
) -> AsyncIterator[httpx.AsyncClient]:
    """The child's browser on the pilots' server, logged in."""
    async with _logged_in(chat_server, child_member) as client:
        yield client


@pytest.fixture
async def live_guest_client(
    chat_server: ChatServer, guest_member: Member
) -> AsyncIterator[httpx.AsyncClient]:
    """The guest's browser on the pilots' server, logged in."""
    async with _logged_in(chat_server, guest_member) as client:
        yield client


@pytest.fixture
def owner_chat(
    chat_server: ChatServer, owner_member: Member, live_owner_client: httpx.AsyncClient
) -> ChatPilot:
    """The owner, talking with Domo (their browser is ``live_owner_client``)."""
    return ChatPilot(chat_server, owner_member, live_owner_client)


@pytest.fixture
def adult_chat(
    chat_server: ChatServer, adult_member: Member, live_adult_client: httpx.AsyncClient
) -> ChatPilot:
    """The adult, talking with Domo (their browser is ``live_adult_client``)."""
    return ChatPilot(chat_server, adult_member, live_adult_client)


@pytest.fixture
def child_chat(
    chat_server: ChatServer, child_member: Member, live_child_client: httpx.AsyncClient
) -> ChatPilot:
    """The child, talking with Domo (their browser is ``live_child_client``)."""
    return ChatPilot(chat_server, child_member, live_child_client)


@pytest.fixture
def guest_chat(
    chat_server: ChatServer, guest_member: Member, live_guest_client: httpx.AsyncClient
) -> ChatPilot:
    """The guest, talking with Domo (their browser is ``live_guest_client``)."""
    return ChatPilot(chat_server, guest_member, live_guest_client)


# --- confirmations (task 6 and after) ----------------------------------------------------------


@pytest.fixture
def recent_replies(
    session_factory: async_sessionmaker[AsyncSession],
) -> Callable[..., Awaitable[list[Any]]]:
    """``await recent_replies(person, after=message_id)``: the replies of Domo in the person's
    conversations made after that message, oldest first (``id``, ``content`` and ``status``, as
    committed)."""

    async def _recent_replies(person: Person, *, after: uuid.UUID) -> list[Any]:
        since = select(Message.created_at).where(Message.id == after).scalar_subquery()
        async with session_factory() as reader:
            result = await reader.execute(
                select(Message.id, Message.content, Message.status)
                .join(Conversation, Conversation.id == Message.conversation_id)
                .where(
                    Conversation.person_id == person.id,
                    Message.role == "assistant",
                    Message.created_at > since,
                )
                .order_by(Message.created_at)
            )
            return list(result.all())

    return _recent_replies


@pytest.fixture
def display_name_of(
    session_factory: async_sessionmaker[AsyncSession],
) -> Callable[[Person], Awaitable[str]]:
    """``await display_name_of(person)``: the name the database holds for the person now."""

    async def _display_name_of(person: Person) -> str:
        async with session_factory() as reader:
            result = await reader.execute(select(Person.display_name).where(Person.id == person.id))
            return result.scalar_one()

    return _display_name_of


@pytest.fixture
def confirmation_status(
    session_factory: async_sessionmaker[AsyncSession],
) -> Callable[[uuid.UUID | str | None], Awaitable[str]]:
    """``await confirmation_status(id)``: the state of the confirmation in the database."""

    async def _confirmation_status(confirmation_id: uuid.UUID | str | None) -> str:
        assert confirmation_id is not None, "no confirmation was shown"
        async with session_factory() as reader:
            result = await reader.execute(
                select(Confirmation.status).where(
                    Confirmation.id == uuid.UUID(str(confirmation_id))
                )
            )
            return result.scalar_one()

    return _confirmation_status


def _confirmation_id(confirmation_id: uuid.UUID | str | None) -> uuid.UUID:
    assert confirmation_id is not None, "no confirmation was shown"
    return uuid.UUID(str(confirmation_id))


@pytest.fixture
async def an_adult_agrees_to_a_childs_rename(grant: Callable[..., Awaitable[PolicyRight]]) -> None:
    """A right of the owner: a child's rename waits for an adult's yes (a rename is not one of the
    actions an adult agrees to for a child by default, ``ADULT_AGREES_FOR_A_CHILD``)."""
    await grant("profile.rename", role="child", approver="adult")


@pytest.fixture
def confirmation_ask(
    session_factory: async_sessionmaker[AsyncSession],
) -> Callable[[uuid.UUID | str | None], Awaitable[tuple[str | None, str | None, str | None]]]:
    """``await confirmation_ask(id)``: what the request asks (``ask``), who asked it
    (``asked_by``) and why (``ask_why``), as stored (committed)."""

    async def _confirmation_ask(
        confirmation_id: uuid.UUID | str | None,
    ) -> tuple[str | None, str | None, str | None]:
        async with session_factory() as reader:
            result = await reader.execute(
                select(Confirmation.ask, Confirmation.asked_by, Confirmation.ask_why).where(
                    Confirmation.id == _confirmation_id(confirmation_id)
                )
            )
            ask, asked_by, ask_why = result.one()
            return ask, asked_by, ask_why

    return _confirmation_ask


@pytest.fixture
def count_confirmations(
    session_factory: async_sessionmaker[AsyncSession],
) -> Callable[[], Awaitable[int]]:
    """``await count_confirmations()``: how many requests the database holds (committed)."""

    async def _count_confirmations() -> int:
        async with session_factory() as reader:
            found = await reader.execute(select(func.count()).select_from(Confirmation))
            return found.scalar_one()

    return _count_confirmations


@pytest.fixture
def steps_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> Callable[[uuid.UUID | str | None], Awaitable[list[ConfirmationStep]]]:
    """``await steps_rows(id)``: the steps of a chain as the database holds them (committed), in
    their order."""

    async def _steps_rows(confirmation_id: uuid.UUID | str | None) -> list[ConfirmationStep]:
        async with session_factory() as reader:
            return await steps_of(reader, _confirmation_id(confirmation_id))

    return _steps_rows


@pytest.fixture
def chain_basis_of(
    session_factory: async_sessionmaker[AsyncSession],
) -> Callable[[uuid.UUID | str | None], Awaitable[dict[str, Any] | None]]:
    """``await chain_basis_of(id)``: what a chain remembers of the one who asked (its role and
    the ids of their own rights then), as stored; ``None`` for a request that is no chain."""

    async def _chain_basis_of(confirmation_id: uuid.UUID | str | None) -> dict[str, Any] | None:
        async with session_factory() as reader:
            result = await reader.execute(
                select(Confirmation.chain_basis).where(
                    Confirmation.id == _confirmation_id(confirmation_id)
                )
            )
            return result.scalar_one()

    return _chain_basis_of


@pytest.fixture
def set_setting(
    session_factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> Callable[[str, Any], Awaitable[None]]:
    """``await set_setting(key, value)``: the owner's choice for a policy setting, written with
    ``update_policy_setting`` and committed (by the first person of the household)."""

    async def _set_setting(key: str, value: Any) -> None:
        async with session_factory() as writer:
            by = (
                await writer.execute(select(Person.id).order_by(Person.created_at).limit(1))
            ).scalar_one()
            await update_policy_setting(writer, key=key, value=value, by=by, now=clock())
            await writer.commit()

    return _set_setting


@pytest.fixture
def current_settings(
    session_factory: async_sessionmaker[AsyncSession],
) -> Callable[[], Awaitable[PolicySettings]]:
    """``await current_settings()``: the owner's policy settings, read again from the database."""

    async def _current_settings() -> PolicySettings:
        async with session_factory() as reader:
            return await load_policy_settings(reader)

    return _current_settings


@pytest.fixture
def last_audit_entry(
    session_factory: async_sessionmaker[AsyncSession],
) -> Callable[[str], Awaitable[Any]]:
    """``await last_audit_entry(action)``: the newest audit entry of that action (``actor``,
    ``action`` and ``details`` as stored)."""

    async def _last_audit_entry(action: str) -> Any:
        async with session_factory() as reader:
            return await last_audit(reader, action)

    return _last_audit_entry


@pytest.fixture
def set_role(
    session_factory: async_sessionmaker[AsyncSession],
) -> Callable[[Person, str], Awaitable[None]]:
    """``await set_role(person, role)``: the person's role changed in the database (committed)."""

    async def _set_role(person: Person, role: str) -> None:
        async with session_factory() as writer:
            await writer.execute(update(Person).where(Person.id == person.id).values(role=role))
            await writer.commit()

    return _set_role


# --- the owner's rights ------------------------------------------------------------------------


@pytest.fixture
def grant(
    session_factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> Callable[..., Awaitable[PolicyRight]]:
    """``await grant(action_type, *, person=None, role=None, access=None, approver=None, limit=None,
    judge_question=None, ask=None, ask_when=None, summary="test right", for_domo=False)``: a right
    of the owner, written straight into ``policy_rights`` and committed (by the owner, or else the
    first person of the household). Its subject is ``person`` (a ``Person`` or an id) or ``role``,
    or Domo (``for_domo``), or none for a rule for everyone (which only says ``ask``); ``limit``
    is its CEL expression; ``ask`` what to ask before the action (or "judge", with the owner's
    conditions in ``ask_when``, or "never")."""

    async def _grant(
        action_type: str,
        *,
        person: Person | uuid.UUID | None = None,
        role: str | None = None,
        access: str | None = None,
        approver: str | None = None,
        limit: str | None = None,
        judge_question: str | None = None,
        ask: str | None = None,
        ask_when: str | None = None,
        summary: str = "test right",
        for_domo: bool = False,
    ) -> PolicyRight:
        async with session_factory() as writer:
            by = (
                await writer.execute(
                    select(Person.id).order_by((Person.role != "owner"), Person.created_at).limit(1)
                )
            ).scalar_one()
            right = PolicyRight(
                person_id=person.id if isinstance(person, Person) else person,
                role=role,
                action_type=action_type,
                access=access,
                approver=approver,
                limit_expression=limit,
                judge_question=judge_question,
                ask=ask,
                ask_when=ask_when,
                summary=summary,
                for_domo=for_domo,
                owner_words="",
                created_by=by,
                created_at=clock(),
            )
            writer.add(right)
            await writer.commit()
            return right

    return _grant


@pytest.fixture
def revoke_right(
    session_factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> Callable[[PolicyRight], Awaitable[None]]:
    """``await revoke_right(right)``: the owner takes the right away (``revoked_at`` is now),
    committed."""

    async def _revoke_right(right: PolicyRight) -> None:
        async with session_factory() as writer:
            await writer.execute(
                update(PolicyRight).where(PolicyRight.id == right.id).values(revoked_at=clock())
            )
            await writer.commit()

    return _revoke_right


# --- the household (task 9) ----------------------------------------------------------------------


@pytest.fixture
def count_owners(
    session_factory: async_sessionmaker[AsyncSession],
) -> Callable[[], Awaitable[int]]:
    """``await count_owners()``: how many people the database holds as the owner (committed)."""

    async def _count_owners() -> int:
        async with session_factory() as reader:
            found = await reader.execute(
                select(func.count()).select_from(Person).where(Person.role == "owner")
            )
            return found.scalar_one()

    return _count_owners


@pytest.fixture
def create_invitation_for_test(
    session: AsyncSession, clock: FakeClock
) -> Callable[..., Awaitable[tuple[Invitation, str]]]:
    """``await create_invitation_for_test(display_name, role, guest_days=None)``: an invitation
    made by the owner (who must exist in the ``session`` fixture) with
    ``household.service.create_invitation``, at the test clock's time, on the ``session``
    fixture (the one the ``app`` and ``client`` fixtures share). Returns it and its raw token.

    It is committed: on that session a commit only releases a savepoint (the test's work is still
    rolled back at the end), and a page that rolls back after a refused attempt does not take the
    invitation, or the owner, with it."""

    async def _create_invitation_for_test(
        display_name: str = "Alex", role: str = "adult", guest_days: int | None = None
    ) -> tuple[Invitation, str]:
        owner_id = (
            await session.execute(select(Person.id).where(Person.role == "owner"))
        ).scalar_one()
        made = await create_invitation(
            session,
            display_name=display_name,
            role=role,  # type: ignore[arg-type]
            guest_days=guest_days,
            created_by=owner_id,
            now=clock(),
        )
        await session.commit()
        return made

    return _create_invitation_for_test


@pytest.fixture
def invite_for_test(
    session_factory: async_sessionmaker[AsyncSession], live_clock: FakeClock
) -> Callable[..., Awaitable[Invitation]]:
    """``await invite_for_test(display_name, role, *, guest_days=None)``: an open invitation made
    by the owner (who must exist) with ``household.service.create_invitation``, at the test
    clock's time, committed on its own session (as ``add_member`` does), so that the pilots'
    server sees it. Its raw token is dropped: nobody follows its link."""

    async def _invite_for_test(
        display_name: str, role: str, *, guest_days: int | None = None
    ) -> Invitation:
        async with session_factory() as writer:
            owner_id = (
                await writer.execute(select(Person.id).where(Person.role == "owner"))
            ).scalar_one()
            invitation, _ = await create_invitation(
                writer,
                display_name=display_name,
                role=role,  # type: ignore[arg-type]
                guest_days=guest_days,
                created_by=owner_id,
                now=live_clock(),
            )
            await writer.commit()
            return invitation

    return _invite_for_test


@pytest.fixture
def join_with_invitation(
    session_factory: async_sessionmaker[AsyncSession],
    owner_password_hash: str,
    live_clock: FakeClock,
) -> Callable[[uuid.UUID, str], Awaitable[Person]]:
    """``await join_with_invitation(invitation_id, username)``: someone joins the household with
    that invitation (``household.service.accept_invitation``, as its link's page does), at the test
    clock's time, committed. Returns the person they are; their password is
    ``chat_support.MEMBER_PASSWORD``."""

    async def _join_with_invitation(invitation_id: uuid.UUID, username: str) -> Person:
        async with session_factory() as writer:
            invitation = await writer.get(Invitation, invitation_id)
            assert invitation is not None, "no such invitation"
            joining = Joining(
                invitation_id=invitation_id,
                username=username,
                display_name=invitation.display_name,
            )
            person = await accept_invitation(writer, joining, owner_password_hash, now=live_clock())
            await writer.commit()
            return person

    return _join_with_invitation


@pytest.fixture
def upkeep_pass(chat_server: ChatServer) -> Callable[[], Awaitable[None]]:
    """``await upkeep_pass()``: one pass of the arrivals on the pilots' server, as its application
    makes it (``confirmations.arrivals.arrivals_pass``, with the dependencies the application
    reads, ``web.app.arrival_deps``), at the time of its clock; then the chains it resumed run to
    their end (``ChatServer.wait_for_wakes``)."""

    async def _upkeep_pass() -> None:
        app = chat_server.app
        await arrivals_pass(arrival_deps(app), app.state.wakes)(chat_server.clock())
        await chat_server.wait_for_wakes()

    return _upkeep_pass


@pytest.fixture
def tool_context(
    chat_server: ChatServer, owner_member: Member, gk_client: GatekeeperClient
) -> ToolContext:
    """A ``ToolContext`` of the owner on the pilots' server (its database, its test clock, its
    gatekeeper client, the registry and the catalog of the tools), to call a tool by hand
    (``describe``, ``execute``, ``acts_on``, ``will_touch``...). The owner exists once the fixture
    is requested."""
    person = owner_member.person
    return ToolContext(
        session_factory=chat_server.session_factory,
        actor=Actor(person.id, as_role(person.role)),
        person=person,
        conversation_id=uuid.uuid4(),
        origin=chat_server.base_url,
        gatekeeper=gk_client,
        clock=chat_server.clock,
        registry=TOOLS,
        policy=DEFAULT_POLICY,
        reply_message_id=None,
    )


@pytest.fixture
def count_invitations(
    session_factory: async_sessionmaker[AsyncSession],
) -> Callable[[], Awaitable[int]]:
    """``await count_invitations()``: how many invitations the database holds (committed)."""

    async def _count_invitations() -> int:
        async with session_factory() as reader:
            return (await reader.execute(select(func.count()).select_from(Invitation))).scalar_one()

    return _count_invitations


@pytest.fixture
def audit_dump(session_factory: async_sessionmaker[AsyncSession]) -> Callable[[], Awaitable[str]]:
    """``await audit_dump()``: the whole audit log (committed) as one text, every column of every
    entry."""

    async def _audit_dump() -> str:
        async with session_factory() as reader:
            rows = await reader.execute(text("SELECT row_to_json(a)::text FROM audit_log a"))
            return "\n".join(row[0] for row in rows.all())

    return _audit_dump


@pytest.fixture
def audit_actions(
    session_factory: async_sessionmaker[AsyncSession],
) -> Callable[[], Awaitable[list[str]]]:
    """``await audit_actions()``: the actions of the audit log (committed), oldest first."""

    async def _audit_actions() -> list[str]:
        async with session_factory() as reader:
            return await audit_actions_in(reader)

    return _audit_actions


# The tables that keep what was said and done in the chat: a secret must never land in one.
_KEPT_TABLES = ("messages", "message_actions", "confirmations", "confirmation_steps", "audit_log")


@pytest.fixture
def stored_text(session_factory: async_sessionmaker[AsyncSession]) -> Callable[[], Awaitable[str]]:
    """``await stored_text()``: every row of the tables that keep what was said and done
    (messages, message_actions, confirmations, confirmation_steps and audit_log), committed, as
    one text."""

    async def _stored_text() -> str:
        async with session_factory() as reader:
            parts: list[str] = []
            for table in _KEPT_TABLES:
                rows = await reader.execute(text(f"SELECT row_to_json(t)::text FROM {table} t"))
                parts.extend(row[0] for row in rows.all())
            return "\n".join(parts)

    return _stored_text


@pytest.fixture
def person_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> Callable[[Person | uuid.UUID], Awaitable[Person]]:
    """``await person_row(person)``: the person as the database holds them now (committed)."""

    async def _person_row(person: Person | uuid.UUID) -> Person:
        person_id = person if isinstance(person, uuid.UUID) else person.id
        async with session_factory() as reader:
            found = await reader.execute(select(Person).where(Person.id == person_id))
            return found.scalar_one()

    return _person_row


# --- notes (task 10) -----------------------------------------------------------------------------

# The text of ``a_note_for_owner``.
OWNER_NOTE_TEXT = "Please walk the dog before noon."


@pytest.fixture
def leave_note(
    session_factory: async_sessionmaker[AsyncSession], live_clock: FakeClock
) -> Callable[..., Awaitable[Note]]:
    """``await leave_note(author, recipient, text, at=None, exact=None)``: a note written straight
    into the database (committed), without the tool and without anyone's yes. ``at`` is when it was
    left (the test clock's time by default), ``exact`` the message its author wrote to Domo
    (none by default: the note keeps only what Domo noted)."""

    async def _leave_note(
        author: Person,
        recipient: Person,
        text: str,
        *,
        at: datetime | None = None,
        exact: str | None = None,
    ) -> Note:
        async with session_factory() as writer:
            note = Note(
                author_id=author.id,
                recipient_id=recipient.id,
                text=text,
                exact_text=exact,
                created_at=live_clock() if at is None else at,
            )
            writer.add(note)
            await writer.commit()
            return note

    return _leave_note


@pytest.fixture
async def a_note_for_owner(
    leave_note: Callable[..., Awaitable[Note]], adult_member: Member, owner_member: Member
) -> Note:
    """A note from the adult ("Alex") to the owner, committed and not dismissed: its text is
    ``OWNER_NOTE_TEXT``."""
    return await leave_note(adult_member.person, owner_member.person, OWNER_NOTE_TEXT)


# The text of ``a_note_for_adult``.
ADULT_NOTE_TEXT = "Please take the bins out tonight."


@pytest.fixture
async def a_note_for_adult(
    leave_note: Callable[..., Awaitable[Note]], adult_member: Member, owner_member: Member
) -> Note:
    """A note from the owner to the adult ("Alex"), committed and not dismissed: its text is
    ``ADULT_NOTE_TEXT``. For the tests about what the adult is never told of a note they
    received."""
    return await leave_note(owner_member.person, adult_member.person, ADULT_NOTE_TEXT)


@pytest.fixture
def count_notes(
    session_factory: async_sessionmaker[AsyncSession],
) -> Callable[[], Awaitable[int]]:
    """``await count_notes()``: how many notes the database holds (committed)."""

    async def _count_notes() -> int:
        async with session_factory() as reader:
            return (await reader.execute(select(func.count()).select_from(Note))).scalar_one()

    return _count_notes


@pytest.fixture
def stored_notes(
    session_factory: async_sessionmaker[AsyncSession],
) -> Callable[[], Awaitable[list[Note]]]:
    """``await stored_notes()``: every note the database holds (committed), oldest first."""

    async def _stored_notes() -> list[Note]:
        async with session_factory() as reader:
            result = await reader.execute(select(Note).order_by(Note.created_at, Note.id))
            return list(result.scalars())

    return _stored_notes
