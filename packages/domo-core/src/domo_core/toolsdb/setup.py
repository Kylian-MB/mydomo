"""Making the tools' database, and the core's own hand on it (spec 5.2, "Base").

The programs Domo writes keep what they did in a PostgreSQL database of their own, on the same
server as the core's but apart from it (``CoreSettings.tools_database``, ``domo_tools`` by
default). The SQL a program sends was written by a model: whatever it says, PostgreSQL itself keeps
it in its place, by roles, privileges and transaction modes. Nothing here reads that SQL.

``prepare_tools_database`` makes it, and can be run again at every start (each step is idempotent,
every statement autocommits):

* the database is created; ``PUBLIC`` loses ``CONNECT`` on the core's database and every privilege
  on the tools' one (no temporary tables, no schema, no extension: those need ``TEMPORARY`` or
  ``CREATE`` on the database), and only the group ``jt_all`` may connect to it;
* its schema ``public`` is dropped; the schema ``common`` is the core's, and ``jt_all`` may use it
  and create in it. The schema itself is not ``jt_all``'s: a member could otherwise drop or rename
  the common space of everyone. An event trigger gives every object made in ``common`` to
  ``jt_all`` (a table one person made is everyone's), from a function of the core's own schema
  ``domo_admin``, which nobody else may use;
* nobody may write a function (``USAGE`` on the languages ``sql`` and ``plpgsql`` is taken from
  ``PUBLIC``): a function, a procedure or a trigger left in the common space would run with the
  rights of whoever touches it next, and could carry one person's rows to another. ``DO`` blocks go
  with it;
* the functions of the server that make large objects (database-wide, outside any corner), take
  advisory locks that outlive a request, or stop other connections are taken from ``PUBLIC`` too;
  so are those that run SQL as their caller (``SQL_RUNNING_FUNCTIONS``: ``query_to_xml`` and its
  family, ``ts_stat``, ``ts_rewrite``), which a default or a rule left on a common table would run
  as the next person who writes there; so are the functions that act on a sequence or a table
  named by its number (``CORNER_REACHING_FUNCTIONS``: ``nextval``, ``setval``,
  ``pg_relation_size``...), for the same reason: a program numbers its rows with an identity
  column (``GENERATED ... AS IDENTITY``), never ``serial``. The same list takes the index upkeep
  functions run with the owner's rights (``brin_summarize_*``, ``gin_clean_pending_list``), those
  that show the writer's own statement (``current_query``, and the views ``pg_prepared_statements``
  and ``pg_cursors``), and ``set_config``, with which a statement could lift its own bounds as it
  runs (the core sets them with ``SET LOCAL``, a statement of its own). The statistics of the tables
  (``pg_stat_*`` and ``pg_statio_*``, views and functions) are taken away too: they would tell one
  corner how much another holds. So are ``pg_stats``, ``pg_stats_ext`` and
  ``pg_stats_ext_exprs``: they hold real values of the columns (the most common ones, histograms),
  filtered by the privileges of whoever reads them, and a row-level policy on a common table is
  read as whoever writes there;
* no role of the tools' database makes a rule, anywhere (an event trigger at ``ddl_command_start``
  refuses ``CREATE RULE`` to anyone but a superuser): a rule's action runs as the next writer.

  What remains visible of another corner: the names of its tables and their columns, the
  estimates of ``pg_class`` (``reltuples``, ``relpages``), which no privilege can take away, and
  the definitions of its views, defaults and checks (``pg_get_viewdef``, ``pg_get_expr``,
  ``pg_get_constraintdef``); and the size of the whole database (``pg_database_size``), which
  moves as others write;
* the group ``jt_all`` (no login) and the house's login role ``jt_house``, a member of it that
  inherits its privileges but cannot become it (``SET FALSE``), with its bounds (``ROLE_SETTINGS``,
  ``CONNECTION_LIMIT``: time, locks, memory, temporary files, no parallel workers). Its password
  is set when it is first used (``spaces.space_for``).

A role's password never travels in a statement: the server is given its SCRAM verifier
(``scram_verifier``), computed here, so that a statement logged by the server holds no password.

``ToolsAdmin`` is the core's own hand on that database: a statement run as the core's own user
(a superuser of the server, as the stack makes it), on the tools' database, each in its own
autocommitted connection. What it runs is written by the core alone, with names it generated.
"""

import base64
import contextlib
import hashlib
import hmac
import re
import secrets
from collections.abc import Awaitable, Callable, Iterable, Mapping
from types import MappingProxyType
from typing import Final, Protocol, cast

import asyncpg  # pyright: ignore[reportMissingTypeStubs]
from sqlalchemy.engine import make_url

from domo_core.madetools.settings import SETTINGS

# --- asyncpg, as the tools' database uses it (asyncpg has no types of its own) -------------------


class PgType(Protocol):
    """The type of a parameter or of a column of a prepared statement."""

    @property
    def name(self) -> str: ...


class PgAttribute(Protocol):
    """A column of a prepared statement."""

    @property
    def name(self) -> str: ...

    @property
    def type(self) -> PgType: ...


class PgRecord(Protocol):
    """A row."""

    def values(self) -> Iterable[object]: ...


class PgCursor(Protocol):
    async def fetch(self, n: int, /) -> list[PgRecord]: ...


class PgStatement(Protocol):
    """A prepared statement."""

    def get_parameters(self) -> tuple[PgType, ...]: ...

    def get_attributes(self) -> tuple[PgAttribute, ...]: ...

    async def fetch(self, *args: object) -> list[PgRecord]: ...

    def cursor(self, *args: object) -> Awaitable[PgCursor]: ...


class PgTransaction(Protocol):
    async def start(self) -> None: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


class PgConnection(Protocol):
    """A connection (or a pool's proxy of one)."""

    async def execute(self, query: str, *args: object) -> str: ...

    async def fetchval(self, query: str, *args: object) -> object: ...

    async def fetch(self, query: str, *args: object) -> list[PgRecord]: ...

    async def prepare(self, query: str) -> PgStatement: ...

    def transaction(self, *, isolation: str, readonly: bool) -> PgTransaction: ...

    def terminate(self) -> None: ...

    def get_server_pid(self) -> int: ...

    def is_closed(self) -> bool: ...

    async def close(self) -> None: ...


class PgPool(Protocol):
    async def acquire(self, *, timeout: float | None = None) -> PgConnection: ...

    async def release(self, connection: PgConnection) -> None: ...

    async def close(self) -> None: ...


async def pg_connect(dsn: str, *, database: str | None = None) -> PgConnection:
    """A connection to ``dsn`` (to ``database`` instead of the DSN's, if given)."""
    connect = cast(Callable[..., Awaitable[PgConnection]], asyncpg.connect)  # pyright: ignore[reportUnknownMemberType]
    if database is None:
        return await connect(dsn)
    return await connect(dsn, database=database)


async def pg_pool(
    *,
    host: str | None,
    port: int | None,
    user: str,
    password: str,
    database: str,
    max_size: int,
    reset: Callable[[PgConnection], Awaitable[None]],
) -> PgPool:
    """A pool of connections that opens none before it is used, caches no statement, and resets
    each connection given back with ``reset``. Its connections never call ``set_config``, which
    the tools' roles may not (``_without_jit_dance``)."""
    create = cast(Callable[..., Awaitable[PgPool]], asyncpg.create_pool)  # pyright: ignore[reportUnknownMemberType]
    return await create(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        min_size=0,
        max_size=max_size,
        statement_cache_size=0,
        max_inactive_connection_lifetime=60.0,
        reset=reset,
        init=_without_jit_dance,
    )


async def _without_jit_dance(connection: object) -> None:
    """asyncpg turns the expression compiler off with ``set_config`` around its reading of an
    unknown type, when the server has one (``Connection._introspect_types``). A tools' role may not
    call ``set_config``, and has the compiler off already (``ROLE_SETTINGS``, set again in each
    transaction): the connection is told the server has none, so that it never tries. This reads
    a private attribute of asyncpg (0.32, pinned by ``uv.lock``); without it, a type asyncpg must
    read would be refused for want of ``set_config``, which a test shows."""
    caps = getattr(connection, "_server_caps", None)
    replace = getattr(caps, "_replace", None)
    if callable(replace):
        setattr(connection, "_server_caps", replace(jit=False))  # noqa: B010


# The group of every role of the tools' database, the house's login role, the common space, and
# the core's own schema there.
ALL_ROLE: Final = "jt_all"
HOUSE_ROLE: Final = "jt_house"
COMMON_SCHEMA: Final = "common"
ADMIN_SCHEMA: Final = "domo_admin"
# The guard of the dry runs (``store.ToolsBase.trial``): a table of the core's own, in a schema of
# its own, where every role may only add a row. Its deferred constraint trigger always fails, so
# that a transaction holding one of its rows can never be kept: a ``COMMIT``, ``END``, ``PREPARE
# TRANSACTION`` or ``SET CONSTRAINTS ... IMMEDIATE`` fails, and the transaction is rolled back. No
# role but the core may alter, disable, empty or drop it (it is not theirs, and
# ``session_replication_role`` is a superuser's).
GUARD_SCHEMA: Final = "domo_guard"
GUARD_TABLE: Final = f"{GUARD_SCHEMA}.trial_guard"
_GUARD_FUNCTION: Final = f"{GUARD_SCHEMA}.never_kept()"
_GUARD_TRIGGER: Final = "jt_never_kept"
_GUARD_FUNCTION_BODY: Final = f"""
CREATE OR REPLACE FUNCTION {_GUARD_FUNCTION} RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $fn$
BEGIN
    RAISE EXCEPTION 'a dry run is never kept' USING ERRCODE = 'insufficient_privilege';
END
$fn$
"""

# How many connections a role of the tools' database may hold at once.
CONNECTION_LIMIT: Final = 4
# How much longer the server's bounds on a request's whole transaction, and on a trial idle between
# two requests, are than the core's own (``request_seconds``): the core ends a request first, by
# its own hand, and says so with the setting; the server's bound is the one behind it, if the core
# ever could not.
SERVER_AFTER_SECONDS: Final = 3
# The bounds of a role's sessions, set on the role (``ALTER ROLE ... SET``). A role may change its
# own defaults of the settings a session may change: every transaction of the core sets those
# again (``store.ToolsBase``), the times of a statement and of a transaction as Domo's settings
# say (``statement_seconds``, ``request_seconds``); set up, they are their starts.
# ``temp_file_limit`` only a superuser may change.
ROLE_SETTINGS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "statement_timeout": f"{SETTINGS['statement_seconds'].start}s",
        # A request's whole transaction: its statements, and the reading of its rows one at a time
        # (each read is a statement of its own to the server). PostgreSQL 17 and later.
        # PostgreSQL arms this timer when a transaction starts, from the value of then: the core
        # sets it (and the idle one below) on the session before each transaction, as its
        # ``request_seconds`` says (``store.session_bounds``); a ``SET LOCAL`` inside the
        # transaction would not move a timer already running.
        "transaction_timeout": f"{SETTINGS['request_seconds'].start + SERVER_AFTER_SECONDS}s",
        # A session whose client is gone (the core dropped it past its own deadline) notices it
        # while it works, and stops, instead of running on until its statement ends.
        "client_connection_check_interval": "1s",
        "lock_timeout": "5s",
        "idle_in_transaction_session_timeout": (
            f"{SETTINGS['request_seconds'].start + SERVER_AFTER_SECONDS}s"
        ),
        "temp_file_limit": "64MB",
        "work_mem": "8MB",
        # What multiplies the memory of one statement: hash tables, index builds, and parallel
        # workers (each with its own ``work_mem``). The compiler of expressions is off too.
        "hash_mem_multiplier": "1",
        "maintenance_work_mem": "16MB",
        "max_parallel_workers_per_gather": "0",
        "max_parallel_maintenance_workers": "0",
        "jit": "off",
    }
)
# The settings a session may change itself: set again in each transaction (``SET LOCAL``).
SESSION_SETTINGS: Final = tuple(name for name in ROLE_SETTINGS if name != "temp_file_limit")
# A role's own defaults as the server keeps them (``pg_db_role_setting``): anything else means the
# role changed them, and the core puts them back (``store.ToolsBase``).
ROLE_CONFIG: Final = frozenset(f"{key}={value}" for key, value in ROLE_SETTINGS.items())

# The functions of the server ``PUBLIC`` may no longer call in the tools' database.
_TAKEN_FUNCTIONS: Final = (
    "lo_create(oid)",
    "lo_creat(integer)",
    "lo_from_bytea(oid, bytea)",
    "pg_advisory_lock(bigint)",
    "pg_advisory_lock(integer, integer)",
    "pg_advisory_lock_shared(bigint)",
    "pg_advisory_lock_shared(integer, integer)",
    "pg_try_advisory_lock(bigint)",
    "pg_try_advisory_lock(integer, integer)",
    "pg_try_advisory_lock_shared(bigint)",
    "pg_try_advisory_lock_shared(integer, integer)",
    "pg_cancel_backend(integer)",
    "pg_terminate_backend(integer, bigint)",
)
# The functions of the server that run SQL given as text, a table, a schema, the whole database
# or a cursor, as whoever calls them: the families ``*_to_xml``, ``*_to_xmlschema`` and
# ``*_to_xml_and_xmlschema``, ``ts_stat`` and ``ts_rewrite``. Left callable, a default or a rule
# one person's program puts on a common table would run them as the next person who writes there,
# and copy that person's corner into the common space. Read from the catalog at each start, every
# signature included.
SQL_RUNNING_FUNCTIONS: Final = (
    "SELECT p.oid::regprocedure::text FROM pg_proc p "
    "WHERE p.pronamespace = 'pg_catalog'::regnamespace AND ("
    "p.proname ~ '_to_xml(schema|_and_xmlschema)?$' OR p.proname IN ('ts_stat', 'ts_rewrite'))"
)
# The functions of the server that act on a table or a sequence named by its number, as their
# caller, without running SQL: a default or a rule left on a common table would read or reset the
# next writer's sequences (``nextval``, ``setval``...) or tell their tables' exact sizes. An
# identity column (``GENERATED ... AS IDENTITY``) still numbers rows: it does not call ``nextval``.
CORNER_REACHING_FUNCTIONS: Final = (
    "SELECT p.oid::regprocedure::text FROM pg_proc p "
    "WHERE p.pronamespace = 'pg_catalog'::regnamespace AND p.proname IN ("
    "'nextval', 'setval', 'currval', 'lastval', 'pg_sequence_last_value', "
    "'pg_relation_size', 'pg_table_size', 'pg_total_relation_size', 'pg_indexes_size', "
    "'pg_stat_have_stats', "
    # Run with the owner's rights on an index given by number, they tell (and change) its recent
    # growth.
    "'brin_summarize_new_values', 'brin_summarize_range', 'brin_desummarize_range', "
    "'gin_clean_pending_list', "
    # The text of the statement the writer is running: a default would copy it into a common row.
    "'current_query', 'pg_prepared_statement', 'pg_cursor', "
    # What lets a session change its own bounds in the middle of a statement (``SET LOCAL``, the
    # core's own way, is no function): a program would lift its timers, and the check that stops
    # a session whose client is gone.
    "'set_config')"
)
# What the server counts of every table (rows, pages, reads), which would tell one corner how much
# another holds: the statistics views and the functions under them; and the views of the writer's
# own statements (``pg_prepared_statements``, ``pg_cursors``), which a policy would read.
_STATISTICS_VIEWS: Final = (
    "SELECT c.oid::regclass::text FROM pg_class c "
    "WHERE c.relnamespace = 'pg_catalog'::regnamespace AND c.relkind = 'v' "
    "AND c.relname ~ '^pg_stat(io)?_|^pg_stats(_|$)|^pg_prepared_statements$|^pg_cursors$'"
)
_STATISTICS_FUNCTIONS: Final = (
    "SELECT p.oid::regprocedure::text FROM pg_proc p "
    "WHERE p.pronamespace = 'pg_catalog'::regnamespace AND p.proname ~ '^pg_stat(io)?_get_'"
)

# A name the core puts in a statement: lower-case letters, digits and ``_``. Anything else is
# refused rather than quoted.
_NAME = re.compile(r"[a-z_][a-z0-9_]{0,62}")

# The SCRAM verifier's iterations (PostgreSQL's own default).
_SCRAM_ITERATIONS: Final = 4096

# No role of the tools' database makes a rule, in the common space or in a corner: a rule's action
# runs as whoever writes to its table next, and a program has no need of one. Only a superuser (the
# core itself) may. ``session_user`` is the role that logged in: none can become another.
_NO_RULE_TRIGGER: Final = "jt_no_rule"
_NO_RULE_FUNCTION: Final = f"{ADMIN_SCHEMA}.no_rule()"
_NO_RULE_FUNCTION_BODY: Final = f"""
CREATE OR REPLACE FUNCTION {_NO_RULE_FUNCTION} RETURNS event_trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
BEGIN
    IF NOT coalesce((SELECT rolsuper FROM pg_roles WHERE rolname = session_user), false) THEN
        RAISE EXCEPTION 'rules are not allowed here' USING ERRCODE = 'insufficient_privilege';
    END IF;
END
$fn$
"""

_EVENT_TRIGGER: Final = "jt_common_is_everyones"
_OWNER_FUNCTION: Final = f"{ADMIN_SCHEMA}.common_to_all()"
# Whatever is made in the common space is given to ``jt_all``. The owner is checked first: the
# change of owner is itself a command the trigger sees. A sequence that belongs to a column goes
# with its table.
_OWNER_FUNCTION_BODY: Final = f"""
CREATE OR REPLACE FUNCTION {_OWNER_FUNCTION} RETURNS event_trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
DECLARE
    made record;
    kind "char";
    owner oid;
BEGIN
    FOR made IN SELECT * FROM pg_event_trigger_ddl_commands()
        WHERE schema_name = '{COMMON_SCHEMA}' AND NOT in_extension
    LOOP
        IF made.classid = 'pg_class'::regclass THEN
            SELECT relkind, relowner INTO kind, owner FROM pg_class WHERE oid = made.objid;
            CONTINUE WHEN owner IS NULL OR owner = '{ALL_ROLE}'::regrole;
            IF kind IN ('r', 'p') THEN
                EXECUTE format('ALTER TABLE %s OWNER TO {ALL_ROLE}', made.object_identity);
            ELSIF kind = 'v' THEN
                EXECUTE format('ALTER VIEW %s OWNER TO {ALL_ROLE}', made.object_identity);
            ELSIF kind = 'm' THEN
                EXECUTE format(
                    'ALTER MATERIALIZED VIEW %s OWNER TO {ALL_ROLE}', made.object_identity
                );
            ELSIF kind = 'S' AND NOT EXISTS (
                SELECT FROM pg_depend WHERE classid = 'pg_class'::regclass
                AND objid = made.objid AND deptype IN ('a', 'i')
            ) THEN
                EXECUTE format('ALTER SEQUENCE %s OWNER TO {ALL_ROLE}', made.object_identity);
            END IF;
        ELSIF made.classid = 'pg_type'::regclass THEN
            SELECT typtype, typowner INTO kind, owner FROM pg_type WHERE oid = made.objid;
            CONTINUE WHEN owner IS NULL OR owner = '{ALL_ROLE}'::regrole;
            IF kind = 'd' THEN
                EXECUTE format('ALTER DOMAIN %s OWNER TO {ALL_ROLE}', made.object_identity);
            ELSIF kind IN ('e', 'c', 'r') THEN
                EXECUTE format('ALTER TYPE %s OWNER TO {ALL_ROLE}', made.object_identity);
            END IF;
        END IF;
    END LOOP;
END
$fn$
"""


def quoted(name: str) -> str:
    """``name`` as an identifier in a statement. Only the names the core makes are accepted
    (lower-case letters, digits, ``_``): ``ValueError`` for anything else."""
    if not _NAME.fullmatch(name):
        raise ValueError("not a name the core makes")
    return f'"{name}"'


def asyncpg_dsn(database_url: str) -> str:
    """The core's SQLAlchemy URL (``postgresql+asyncpg://``) as a DSN asyncpg reads."""
    return make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)


def database_of(database_url: str) -> str:
    """The name of the database a URL points at."""
    name = make_url(database_url).database
    if not name:
        raise ValueError("the URL names no database")
    return name


def new_password() -> str:
    """A new password for a role of the tools' database: random, ASCII, never chosen by anyone."""
    return secrets.token_urlsafe(32)


def scram_verifier(password: str) -> str:
    """What PostgreSQL keeps of ``password`` (SCRAM-SHA-256), so that the password itself is never
    sent in a statement. ``password`` is ASCII (``new_password``): it needs no SASLprep."""
    salt = secrets.token_bytes(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("ascii"), salt, _SCRAM_ITERATIONS)
    client_key = hmac.digest(salted, b"Client Key", "sha256")
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.digest(salted, b"Server Key", "sha256")

    def b64(raw: bytes) -> str:
        return base64.b64encode(raw).decode("ascii")

    return f"SCRAM-SHA-256${_SCRAM_ITERATIONS}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"


def password_statement(role: str, password: str) -> str:
    """The statement that gives ``role`` the password ``password`` (its verifier)."""
    return f"ALTER ROLE {quoted(role)} PASSWORD '{scram_verifier(password)}'"


def bounds_statements(role: str) -> list[str]:
    """The statements that make ``role`` a plain login role of the tools' database, a member of
    ``jt_all`` that cannot become it, with its bounds."""
    name = quoted(role)
    statements = [
        f"ALTER ROLE {name} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS "
        f"CONNECTION LIMIT {CONNECTION_LIMIT}",
        *(f"ALTER ROLE {name} SET {key} = '{value}'" for key, value in ROLE_SETTINGS.items()),
        f"GRANT {quoted(ALL_ROLE)} TO {name} WITH INHERIT TRUE, SET FALSE",
    ]
    return statements


def bounds_reset_statements(role: str, database: str) -> list[str]:
    """The statements that take away every default ``role`` set itself (everywhere, and in the
    tools' database ``database``) and give it its bounds again."""
    name = quoted(role)
    return [
        f"ALTER ROLE {name} RESET ALL",
        f"ALTER ROLE {name} IN DATABASE {quoted(database)} RESET ALL",
        *bounds_statements(role),
    ]


class ToolsAdmin:
    """The core's own hand on the tools' database ``database``, as the user of ``admin_dsn`` (the
    core's own, a superuser of the server). Each statement runs in a connection of its own, and
    autocommits."""

    def __init__(self, admin_dsn: str, database: str) -> None:
        quoted(database)
        self._dsn = admin_dsn
        self._database = database

    @property
    def database(self) -> str:
        """The name of the tools' database."""
        return self._database

    @property
    def server(self) -> tuple[str | None, int | None]:
        """The host and port of the server (those of the core's own database)."""
        url = make_url(self._dsn)
        return url.host, url.port

    async def execute(self, statement: str) -> None:
        """Run ``statement`` (one, or several separated by ``;``, all or nothing) on the tools'
        database. The errors are asyncpg's; none holds the statement."""
        connection = await pg_connect(self._dsn, database=self._database)
        try:
            await connection.execute(statement)
        finally:
            await connection.close()

    async def fetch(self, query: str, *args: object) -> list[tuple[object, ...]]:
        """The rows ``query`` reads on the tools' database, as the core's own user."""
        connection = await pg_connect(self._dsn, database=self._database)
        try:
            return [tuple(record.values()) for record in await connection.fetch(query, *args)]
        finally:
            await connection.close()


# The tools' database does not exist (yet).
MISSING_DATABASE: Final[type[BaseException]] = asyncpg.InvalidCatalogNameError

# What may go wrong while the tools' database is made: the server refuses (a user that is not a
# superuser, say) or cannot be reached.
PREPARE_ERRORS: Final[tuple[type[BaseException], ...]] = (
    asyncpg.PostgresError,
    asyncpg.InterfaceError,
    OSError,
)


def describe_failure(error: BaseException) -> str:
    """What went wrong, in one line: the kind of error and the first line of the server's message
    (asyncpg's messages hold no password)."""
    lines = str(error).strip().splitlines()
    return f"{type(error).__name__}: {lines[0]}" if lines else type(error).__name__


async def _create_role(connection: PgConnection, role: str, *, login: bool) -> None:
    found = await connection.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", role)
    if found:
        return
    with contextlib.suppress(asyncpg.DuplicateObjectError):  # made by another start in between
        await connection.execute(f"CREATE ROLE {quoted(role)} {'LOGIN' if login else 'NOLOGIN'}")


async def prepare_tools_database(admin_dsn: str, *, core_database: str, name: str) -> None:
    """Make the tools' database ``name`` and its roles, or bring them back to what they must be
    (see the module docstring). ``admin_dsn`` is the core's own (on ``core_database``)."""
    tools = quoted(name)
    connection = await pg_connect(admin_dsn)
    try:
        if not await connection.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", name):
            with contextlib.suppress(asyncpg.DuplicateDatabaseError):
                await connection.execute(f"CREATE DATABASE {tools}")
        await _create_role(connection, ALL_ROLE, login=False)
        await _create_role(connection, HOUSE_ROLE, login=True)
        await connection.execute(
            f"ALTER ROLE {quoted(ALL_ROLE)} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOREPLICATION NOBYPASSRLS"
        )
        for statement in bounds_statements(HOUSE_ROLE):
            await connection.execute(statement)
        await connection.execute(f"REVOKE CONNECT ON DATABASE {quoted(core_database)} FROM PUBLIC")
        await connection.execute(f"REVOKE ALL ON DATABASE {tools} FROM PUBLIC")
        await connection.execute(f"GRANT CONNECT ON DATABASE {tools} TO {quoted(ALL_ROLE)}")
    finally:
        await connection.close()

    connection = await pg_connect(admin_dsn, database=name)
    try:
        await connection.execute("DROP SCHEMA IF EXISTS public")
        common = quoted(COMMON_SCHEMA)
        await connection.execute(f"CREATE SCHEMA IF NOT EXISTS {common}")
        await connection.execute(f"REVOKE ALL ON SCHEMA {common} FROM PUBLIC")
        await connection.execute(f"GRANT USAGE, CREATE ON SCHEMA {common} TO {quoted(ALL_ROLE)}")
        await connection.execute(f"CREATE SCHEMA IF NOT EXISTS {quoted(ADMIN_SCHEMA)}")
        await connection.execute(f"REVOKE ALL ON SCHEMA {quoted(ADMIN_SCHEMA)} FROM PUBLIC")
        await connection.execute(_OWNER_FUNCTION_BODY)
        await connection.execute(f"REVOKE ALL ON FUNCTION {_OWNER_FUNCTION} FROM PUBLIC")
        if not await connection.fetchval(
            "SELECT 1 FROM pg_event_trigger WHERE evtname = $1", _EVENT_TRIGGER
        ):
            await connection.execute(
                f"CREATE EVENT TRIGGER {_EVENT_TRIGGER} ON ddl_command_end "
                f"EXECUTE FUNCTION {_OWNER_FUNCTION}"
            )
        guard = quoted(GUARD_SCHEMA)
        await connection.execute(f"CREATE SCHEMA IF NOT EXISTS {guard}")
        await connection.execute(f"REVOKE ALL ON SCHEMA {guard} FROM PUBLIC")
        await connection.execute(f"GRANT USAGE ON SCHEMA {guard} TO {quoted(ALL_ROLE)}")
        await connection.execute(
            f"CREATE TABLE IF NOT EXISTS {GUARD_TABLE} (planted boolean NOT NULL DEFAULT true)"
        )
        await connection.execute(f"REVOKE ALL ON TABLE {GUARD_TABLE} FROM PUBLIC")
        await connection.execute(f"GRANT INSERT ON TABLE {GUARD_TABLE} TO {quoted(ALL_ROLE)}")
        await connection.execute(_GUARD_FUNCTION_BODY)
        await connection.execute(f"REVOKE ALL ON FUNCTION {_GUARD_FUNCTION} FROM PUBLIC")
        if not await connection.fetchval(
            "SELECT 1 FROM pg_trigger WHERE tgname = $1 AND tgrelid = $2::regclass",
            _GUARD_TRIGGER,
            GUARD_TABLE,
        ):
            await connection.execute(
                f"CREATE CONSTRAINT TRIGGER {_GUARD_TRIGGER} AFTER INSERT ON {GUARD_TABLE} "
                f"DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION {_GUARD_FUNCTION}"
            )
        await connection.execute(_NO_RULE_FUNCTION_BODY)
        await connection.execute(f"REVOKE ALL ON FUNCTION {_NO_RULE_FUNCTION} FROM PUBLIC")
        if not await connection.fetchval(
            "SELECT 1 FROM pg_event_trigger WHERE evtname = $1", _NO_RULE_TRIGGER
        ):
            await connection.execute(
                f"CREATE EVENT TRIGGER {_NO_RULE_TRIGGER} ON ddl_command_start "
                f"WHEN TAG IN ('CREATE RULE') EXECUTE FUNCTION {_NO_RULE_FUNCTION}"
            )
        await connection.execute("REVOKE USAGE ON LANGUAGE sql, plpgsql FROM PUBLIC")
        for function in _TAKEN_FUNCTIONS:
            await connection.execute(f"REVOKE EXECUTE ON FUNCTION {function} FROM PUBLIC")
        for query in (SQL_RUNNING_FUNCTIONS, CORNER_REACHING_FUNCTIONS, _STATISTICS_FUNCTIONS):
            for record in await connection.fetch(query):
                (function,) = record.values()
                await connection.execute(f"REVOKE EXECUTE ON FUNCTION {function} FROM PUBLIC")
        for record in await connection.fetch(_STATISTICS_VIEWS):
            (view,) = record.values()
            await connection.execute(f"REVOKE SELECT ON {view} FROM PUBLIC")
    finally:
        await connection.close()
