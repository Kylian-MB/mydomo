"""Running a program's request on the tools' database (spec 5.2, "Base").

``ToolsBase.query`` runs one statement a program sent, as the role of whoever the launch is for
(``spaces.SpaceLogin``), in a transaction of its own:

* the statement is prepared alone: two statements in one request are refused by the server
  (``sql_error``), and the first is never run;
* the transaction starts with the core's settings (``SET LOCAL``): the search path is the person's
  corner (``mine``) or the common space (``common``), and the bounds a session could change itself
  (``setup.SESSION_SETTINGS``) are set again, whatever the role made of its own defaults;
* a read runs in a ``READ ONLY`` transaction: nothing it calls may write;
* a write is refused (``full``) while the database is bigger than ``bound_bytes``
  (``pg_database_size``, read before it), unless it begins with ``DROP`` or ``TRUNCATE``, which free
  room. It is read once, before: one write may go past the bound;
* the answer is at most ``BaseBounds.rows`` rows and ``BaseBounds.answer_bytes`` of JSON (the
  launch's ``result_bytes``; ``too_big`` beyond, naming the setting), and never more than
  ``ANSWER_CEILING_BYTES`` whatever the setting (``too_big``, told as the code's ceiling),
  each row an object of its columns: a date or a time as ISO text, a ``numeric`` as text, a
  ``json`` value as itself, bytes as hexadecimal. The rows are read one at a time, the size counted
  as they come, and the reading stops at the first row past the bound: the core never holds more
  than the bound and one row. That one row is bounded by PostgreSQL alone (1 GB a value): nothing
  in the protocol lets the core refuse a row before it has read it;
* the whole request is bounded in time: each statement by ``statement_timeout``
  (``BaseBounds.statement_seconds``), and its whole transaction, the reading of its rows one at a
  time included (each read restarts the statement's timer), by ``transaction_timeout``
  (PostgreSQL 17), and a trial idle between two requests by
  ``idle_in_transaction_session_timeout``, both three seconds past the request's, set on the
  session before each transaction (``session_bounds``: the server arms its transaction's timer
  then; ``DISCARD ALL`` takes them back when the connection goes back to the pool), after which
  the server closes the connection. A program cannot lift these (``set_config`` is taken from
  it, and a ``SET`` is a request of its own), but the bound that always holds is the core's: at
  ``BaseBounds.request_seconds`` it ends the server's process itself, by its pid, as its own user
  (``pg_terminate_backend``, within ``END_BACKEND_SECONDS``), and drops the connection without
  waiting for the server to answer a cancel or a rollback. The pool replaces a closed connection:
  the next request finds a clean one;
* a write whose COMMIT was sent but never answered (the connection lost, or the core's deadline
  reached in the middle of it) is neither done nor refused: ``outcome_unknown`` tells the program
  to read before writing it again;
* a dry run's statements (``trial``, from ``ToolsBase.trial``) all run in one transaction of their
  own, each in a savepoint (a read's set ``READ ONLY`` there, and rolled back; a write's kept until
  the trial ends, so the next statement sees it), the settings set again before each one; the
  whole transaction is rolled back when the trial ends, whatever it did. That transaction can
  never be kept, by PostgreSQL itself, whatever the program sends (no word of it is read): the
  core plants a row of its own guard first (``setup.GUARD_TABLE``), whose deferred constraint
  trigger fails any ``COMMIT``, ``END``, ``PREPARE TRANSACTION`` or ``SET CONSTRAINTS ...
  IMMEDIATE``, and no role of the tools' database may alter, disable, empty or drop it. The
  transaction's identity (``pg_current_xact_id``) is taken with the guard and read again before
  and after each statement: a program that ended or replaced it (``ROLLBACK``, ``ABORT``,
  ``ROLLBACK AND CHAIN``, a failed ``COMMIT``) has lost its trial, whose server process is ended
  and in which nothing more runs. Each statement is bounded by the core's deadline, and the whole
  trial by its ``request_seconds`` counted by the core from its opening, whatever the program set
  (``transaction_timeout`` and the other bounds are the program's to change within its own
  session): past either, the core ends the trial's server process by its pid, its locks with it,
  and the trial is lost. A trial ended by a cancellation drops its connection (the server rolls a
  dropped connection back), never gives it back to the pool in its transaction. Once rolled back,
  the role's own defaults are read as after a write (``_keep_bounds``);
* after a write, the role's own defaults are read: a role may change them (``ALTER ROLE
  CURRENT_USER SET ...``), and the core puts the bounds back at once (``setup.ROLE_CONFIG``).

The three bounds (``BaseBounds``: the rows, the seconds of a statement and of a request) are
settings of Domo's (``madetools.settings``), given by the launch with each request; set up, a
role's own defaults are their starts (``setup.ROLE_SETTINGS``). A refusal one of them caused names
it (``BaseRefused.setting``). The pool's wait for a connection is no bound of use: a busy pool is
a blocking, and stays ``ACQUIRE_SECONDS``.

What the program gave is never read by the core: no word of it is parsed, logged or audited. A
refusal's ``detail`` is the SQLSTATE and the first line of the server's message, 200 characters
at most: for the program, never for the audit log.

Each role has a small pool of connections of its own, made the first time it is used. A pooled
connection is reset (``DISCARD ALL``) when the request gives it back, so nothing a request set on
its session lasts. A role may change its own password: when a connection is refused for that, the
core gives the role its password back and tries once more.
"""

import asyncio
import contextlib
import json
import logging
import math
import re
import uuid
from collections.abc import AsyncGenerator, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Final, Literal, cast

import asyncpg  # pyright: ignore[reportMissingTypeStubs]
from domo_common.sandbox_api import ANSWER_CEILING_BYTES, ANSWER_CEILING_WORDS, JsonObject
from pydantic import JsonValue

from domo_core.madetools.settings import SETTINGS
from domo_core.toolsdb.setup import (
    COMMON_SCHEMA,
    GUARD_TABLE,
    ROLE_CONFIG,
    ROLE_SETTINGS,
    SERVER_AFTER_SECONDS,
    SESSION_SETTINGS,
    PgConnection,
    PgPool,
    PgTransaction,
    ToolsAdmin,
    bounds_reset_statements,
    password_statement,
    pg_pool,
    quoted,
)
from domo_core.toolsdb.spaces import SpaceLogin

logger = logging.getLogger(__name__)

type Space = Literal["common", "mine"]
RefusalReason = Literal["sql_error", "too_big", "full", "no_corner", "outcome_unknown"]

# The most characters of a refusal's detail.
MAX_DETAIL: Final = 200
# The longest timeout PostgreSQL takes, in milliseconds: what the server cannot give past it.
_LONGEST_MS: Final = 2_147_483_647
# How long the core gives itself to end the server's process of a request past its time.
END_BACKEND_SECONDS: Final = 2
# How many connections a role's pool holds at most (under the role's own limit).
POOL_SIZE: Final = 2
# How long a request waits for a connection of its role's pool (the others may hold them, a
# dry run's trial for up to its whole time): past it, the request is refused cleanly.
ACQUIRE_SECONDS: Final = 10.0
_BUSY: Final = f"the base is busy: no connection came within {ACQUIRE_SECONDS:g} s; try again later"
# The transaction's identity, as a trial reads it: the catalog's own function, named with its
# schema, whatever the search path the program set.
_XACT: Final = "SELECT pg_catalog.pg_current_xact_id()::text"

# A role's own defaults, as the role reads them: everywhere, and in the tools' database.
_ROLE_CONFIG_QUERY: Final = (
    "SELECT array_agg(c) FROM pg_db_role_setting s, unnest(s.setconfig) AS c "
    "WHERE s.setrole = (SELECT oid FROM pg_roles WHERE rolname = current_user) "
    "AND s.setdatabase IN (0, (SELECT oid FROM pg_database WHERE datname = current_database()))"
)

# What frees room: a write that begins with it passes when the database is full.
_FREES_ROOM = re.compile(r"\s*(DROP|TRUNCATE)\b", re.IGNORECASE)

_INTEGERS = frozenset({"int2", "int4", "int8", "oid"})
_FLOATS = frozenset({"float4", "float8"})
_JSON = frozenset({"json", "jsonb"})


@dataclass(frozen=True)
class BaseRows:
    """What a request gave back: its rows (none for a statement that returns none)."""

    rows: list[JsonObject]


@dataclass(frozen=True)
class BaseBounds:
    """The bounds of one launch's requests (settings of Domo's): the rows an answer holds at
    most, how many seconds one statement and one whole request may take (a dry run's whole trial
    as long as one request), and what an answer weighs at most as JSON (``answer_bytes``: the
    launch's ``result_bytes``, what its program reads)."""

    rows: int
    statement_seconds: int
    request_seconds: int
    answer_bytes: int

    @classmethod
    def at_start(cls) -> "BaseBounds":
        """The bounds before Domo set any: the starts of his settings."""
        return cls(
            rows=SETTINGS["rows"].start,
            statement_seconds=SETTINGS["statement_seconds"].start,
            request_seconds=SETTINGS["request_seconds"].start,
            answer_bytes=SETTINGS["result_bytes"].start,
        )


@dataclass(frozen=True)
class BaseRefused:
    """A request that was not carried out: why, for a ``sql_error`` what the server said, and the
    bound that refused it, when one did (``rows``, ``statement_seconds``, ``request_seconds``)."""

    reason: RefusalReason
    detail: str | None
    setting: str | None = None


class Trial:
    """The one transaction a dry run's statements all run in (``ToolsBase.trial``), on a
    connection of its own, rolled back when the trial ends. ``lost`` once its connection is gone
    or was ended, or its transaction was ended or replaced: every statement after that is refused.
    ``xact`` is the transaction's identity, taken with the guard; ``wrote``, whether a write ran."""

    def __init__(self, connection: PgConnection) -> None:
        self.connection = connection
        self.lost = False
        self.xact = ""
        self.wrote = False


# What a statement of a trial that was lost is told.
_TRIAL_LOST: Final = "the dry run's transaction was ended: nothing more runs in it"
# The savepoint each statement of a trial runs in.
_SAVEPOINT: Final = "request"


class _Progress:
    """Where a request is: once its COMMIT is sent, an end without an answer leaves its outcome
    unknown."""

    def __init__(self) -> None:
        self.committing = False


# What a write is told when its COMMIT was sent but never answered.
_COMMIT_UNANSWERED: Final = (
    "the write was sent to be kept, but no answer came: it may or may not have been kept; "
    "read before writing it again"
)


class _Unusable(ValueError):
    """A value of the program's that does not fit the parameter it is given to."""


def _detail(error: BaseException) -> str:
    code = getattr(error, "sqlstate", None)
    message = str(error).strip().splitlines()
    first = message[0] if message else type(error).__name__
    return (f"{code}: {first}" if code else first)[:MAX_DETAIL]


def _whole(value: JsonValue) -> int:
    if isinstance(value, bool):
        raise _Unusable("a whole number was expected")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        return int(value.strip())
    raise _Unusable("a whole number was expected")


def _argument(value: JsonValue, type_name: str) -> object:
    """The program's ``value`` as asyncpg wants it for a parameter of type ``type_name``."""
    if value is None:
        return None
    if type_name.startswith("_"):
        if not isinstance(value, list):
            raise _Unusable("a list was expected")
        return [_argument(item, type_name[1:]) for item in value]
    try:
        match type_name:
            case name if name in _INTEGERS:
                return _whole(value)
            case name if name in _FLOATS:
                if isinstance(value, bool) or not isinstance(value, int | float | str):
                    raise _Unusable("a number was expected")
                return float(value)
            case "numeric":
                if isinstance(value, bool) or not isinstance(value, int | float | str):
                    raise _Unusable("a number was expected")
                return Decimal(str(value))
            case "bool":
                if not isinstance(value, bool):
                    raise _Unusable("true or false was expected")
                return value
            case name if name in _JSON:
                return json.dumps(value, ensure_ascii=False)
            case "uuid":
                return uuid.UUID(str(value))
            case "date":
                return date.fromisoformat(str(value))
            case "timestamptz" | "timestamp":
                return datetime.fromisoformat(str(value))
            case "time":
                return time.fromisoformat(str(value))
            case "bytea":
                return bytes.fromhex(str(value))
            case _:
                if isinstance(value, str):
                    return value
                if isinstance(value, bool):
                    return "true" if value else "false"
                if isinstance(value, int | float):
                    return str(value)
                return json.dumps(value, ensure_ascii=False)
    except (ValueError, TypeError, ArithmeticError) as error:
        raise _Unusable(str(error) or "a value does not fit its parameter") from None


def _json_of(value: object, type_name: str) -> JsonValue:
    """A value of a column as JSON."""
    if value is None or isinstance(value, bool | int | str):
        if isinstance(value, str) and type_name in _JSON:
            return json.loads(value)
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, timedelta | uuid.UUID):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, list | tuple):
        inner = type_name[1:] if type_name.startswith("_") else type_name
        return [_json_of(item, inner) for item in cast(Iterable[object], value)]
    items = getattr(value, "items", None)
    if callable(items):  # a composite value (a record)
        pairs = cast(Callable[[], Iterable[tuple[object, object]]], items)()
        return {str(key): _json_of(item, "") for key, item in pairs}
    return str(value)


async def _discard(connection: PgConnection) -> None:
    """What a connection is reset with when a request gives it back: nothing of the session
    lasts (settings, prepared statements, listens, locks)."""
    await connection.execute("DISCARD ALL")


class BaseBusy(Exception):
    """No connection of the role's pool came within ``ACQUIRE_SECONDS``: nothing was run."""


class ToolsBase:
    """The tools' database as the programs reach it (see the module docstring)."""

    def __init__(self, admin: ToolsAdmin) -> None:
        self._admin = admin
        self._pools: dict[str, PgPool] = {}

    @property
    def admin(self) -> ToolsAdmin:
        """The core's hand on the tools' database (``spaces.space_for`` makes a corner with it)."""
        return self._admin

    async def _pool(self, login: SpaceLogin) -> PgPool:
        pool = self._pools.get(login.role_name)
        if pool is None:
            host, port = self._admin.server
            pool = await pg_pool(
                host=host,
                port=port,
                user=login.role_name,
                password=login.password,
                database=self._admin.database,
                max_size=POOL_SIZE,
                reset=_discard,
            )
            self._pools[login.role_name] = pool
        return pool

    async def _acquire(self, login: SpaceLogin) -> PgConnection:
        """A connection of ``login``'s pool, within ``ACQUIRE_SECONDS``; ``BaseBusy`` past it."""
        pool = await self._pool(login)
        try:
            try:
                return await pool.acquire(timeout=ACQUIRE_SECONDS)
            except asyncpg.InvalidPasswordError:
                # The role changed its own password: it gets the core's back.
                logger.warning("A role of the tools' database had its password changed; set back")
                await self._admin.execute(password_statement(login.role_name, login.password))
                return await pool.acquire(timeout=ACQUIRE_SECONDS)
        except TimeoutError:
            raise BaseBusy(_BUSY) from None

    async def query(
        self,
        login: SpaceLogin,
        *,
        sql: str,
        params: Sequence[JsonValue],
        write: bool,
        space: Space,
        bound_bytes: int,
        trial: Trial | None = None,
        bounds: BaseBounds | None = None,
    ) -> BaseRows | BaseRefused:
        """Run ``sql`` with ``params`` as ``login``, in ``space`` (see the module docstring); in
        ``trial`` for a dry run (its connection, ``login``'s, is the trial's), within ``bounds``
        (``None``: the starts)."""
        bounds = BaseBounds.at_start() if bounds is None else bounds
        if space == "mine":
            if login.corner is None:
                return BaseRefused("no_corner", None)
            path = quoted(login.corner)
        else:
            path = quoted(COMMON_SCHEMA)
        if trial is not None:
            return await self._tried(
                trial,
                path=path,
                sql=sql,
                params=params,
                write=write,
                bound_bytes=bound_bytes,
                bounds=bounds,
            )
        pool = await self._pool(login)
        try:
            connection = await self._acquire(login)
        except BaseBusy:
            return BaseRefused("sql_error", _BUSY)
        progress = _Progress()
        try:
            try:
                async with asyncio.timeout(bounds.request_seconds):
                    answer = await self._run(
                        connection,
                        progress,
                        path=path,
                        sql=sql,
                        params=params,
                        write=write,
                        bound_bytes=bound_bytes,
                        bounds=bounds,
                    )
            except TimeoutError:
                # The core's own end of a request past its time: the server's process is ended by
                # the core's own hand (its locks and its snapshot go with it), and the connection
                # is dropped, without waiting for the server to answer a cancel or a rollback.
                await self._end_backend(connection)
                # The connection may already be gone with its server process (asyncpg then gives
                # it back to the pool by itself).
                with contextlib.suppress(asyncpg.InterfaceError):
                    connection.terminate()
                if progress.committing:
                    return BaseRefused("outcome_unknown", _COMMIT_UNANSWERED, "request_seconds")
                return _too_long(bounds)
            if write and isinstance(answer, BaseRows):
                await self._keep_bounds(connection, login)
            return answer
        finally:
            await pool.release(connection)

    async def _end_backend(self, connection: PgConnection) -> None:
        """End the server's process of ``connection`` as the core's own user, within
        ``END_BACKEND_SECONDS``. A failure is logged by its kind: the request is answered all the
        same, and the server's own checks remain."""
        try:
            pid = int(connection.get_server_pid())
            async with asyncio.timeout(END_BACKEND_SECONDS):
                await self._admin.execute(f"SELECT pg_terminate_backend({pid})")
        except Exception as error:
            logger.error("A request past its time could not be ended (%s)", type(error).__name__)

    async def _keep_bounds(self, connection: PgConnection, login: SpaceLogin) -> None:
        """A role that changed its own defaults (a write may, ``ALTER ROLE CURRENT_USER SET``)
        gets the core's back at once, so that none it set lasts."""
        found = await connection.fetchval(_ROLE_CONFIG_QUERY)
        kept = frozenset(cast(Iterable[str], found or ()))
        if kept != ROLE_CONFIG:
            logger.warning("A role of the tools' database changed its own bounds; set back")
            await self._admin.execute(
                ";\n".join(bounds_reset_statements(login.role_name, self._admin.database))
            )

    async def _run(
        self,
        connection: PgConnection,
        progress: _Progress,
        *,
        path: str,
        sql: str,
        params: Sequence[JsonValue],
        write: bool,
        bound_bytes: int,
        bounds: BaseBounds,
    ) -> BaseRows | BaseRefused:
        transaction = connection.transaction(isolation="read_committed", readonly=not write)
        await connection.execute(session_bounds(bounds))
        await transaction.start()
        try:
            await connection.execute(_settings(path, bounds))
            if write and await _full(connection, sql, bound_bytes):
                await transaction.rollback()
                return BaseRefused("full", None)
            answer = await self._statement(connection, sql, params, bounds)
            if isinstance(answer, BaseRefused):
                await transaction.rollback()
                return answer
        except (
            asyncpg.PostgresError,
            asyncpg.InterfaceError,
            asyncpg.InternalClientError,
            _Unusable,
        ) as error:
            # The server's end of a request that went past its time too: the server closed the
            # connection, which the pool replaces. A column of a type asyncpg cannot read (a
            # pseudo-type such as ``anyarray``) is the program's to change.
            await _roll_back(transaction)
            return _server_refused(error)
        except asyncio.CancelledError:
            # Past the core's deadline: no rollback is awaited (the server may never answer);
            # the caller drops the connection.
            raise
        except BaseException:
            await _roll_back(transaction)
            raise
        # A read keeps nothing: only a write's COMMIT can leave its outcome unknown.
        progress.committing = write
        unknown = BaseRefused("outcome_unknown", _COMMIT_UNANSWERED)
        try:
            await transaction.commit()
        except asyncpg.PostgresError as error:
            lost = str(getattr(error, "sqlstate", "") or "").startswith("08")
            if lost or connection.is_closed():
                # The connection went with the COMMIT: it may have been carried out.
                return unknown if write else BaseRefused("sql_error", _detail(error))
            # Refused by the server (a constraint checked at the end, say): nothing was kept.
            await _roll_back(transaction)
            return BaseRefused("sql_error", _detail(error))
        except (asyncpg.InterfaceError, OSError) as error:
            return unknown if write else BaseRefused("sql_error", _detail(error))
        progress.committing = False
        return answer

    @contextlib.asynccontextmanager
    async def trial(
        self, login: SpaceLogin, bounds: BaseBounds | None = None
    ) -> AsyncGenerator[Trial]:
        """A dry run's transaction (``Trial``), as ``login``: its statements are given it
        (``query``, ``trial``), and it is rolled back when the context ends, whatever they did
        (see the module docstring). Opening it is bounded by the core's deadline too, and the
        whole trial by the ``request_seconds`` of ``bounds`` (``None``: the starts)."""
        bounds = BaseBounds.at_start() if bounds is None else bounds
        pool = await self._pool(login)
        connection = await self._acquire(login)
        trial = Trial(connection)
        transaction = connection.transaction(isolation="read_committed", readonly=False)
        deadline: asyncio.Task[None] | None = None
        try:
            try:
                async with asyncio.timeout(bounds.request_seconds):
                    await connection.execute(session_bounds(bounds))
                    await transaction.start()
                    # The guard first: this transaction can never be kept.
                    await connection.execute(f"INSERT INTO {GUARD_TABLE} DEFAULT VALUES")
                    trial.xact = str(await connection.fetchval(_XACT))
            except BaseException:
                trial.lost = True
                raise
            deadline = asyncio.create_task(self._trial_deadline(trial, bounds.request_seconds))
            yield trial
        finally:
            if deadline is not None:
                deadline.cancel()
            try:
                await self._end_trial(trial, transaction)
                if not trial.lost and trial.wrote:
                    await self._keep_bounds(connection, login)
            finally:
                await pool.release(connection)

    async def _trial_deadline(self, trial: Trial, seconds: int) -> None:
        """At ``seconds`` from its opening, the trial's server process is ended by the core,
        whatever the program set, and the trial is lost."""
        await asyncio.sleep(seconds)
        trial.lost = True
        await self._end_backend(trial.connection)

    async def _end_trial(self, trial: Trial, transaction: PgTransaction) -> None:
        """Roll the trial back, within ``END_BACKEND_SECONDS``; a trial lost, or one whose rollback
        does not come, has its server process ended and its connection dropped. Cancelled while it
        ends, its connection is dropped all the same (the server rolls it back), never given back
        to the pool in its transaction."""
        connection = trial.connection
        try:
            if not trial.lost:
                try:
                    async with asyncio.timeout(END_BACKEND_SECONDS):
                        await transaction.rollback()
                    return
                except (TimeoutError, asyncpg.PostgresError, asyncpg.InterfaceError, OSError):
                    trial.lost = True
            await self._end_backend(connection)
        except BaseException:
            trial.lost = True
            raise
        finally:
            if trial.lost:
                with contextlib.suppress(asyncpg.InterfaceError):
                    connection.terminate()

    async def _same_transaction(self, trial: Trial) -> bool:
        """Whether the trial is still in the transaction it opened (the program may have ended or
        replaced it); a connection that cannot tell is not."""
        try:
            return str(await trial.connection.fetchval(_XACT)) == trial.xact
        except (asyncpg.PostgresError, asyncpg.InterfaceError, asyncpg.InternalClientError):
            return False

    async def _lose(self, trial: Trial) -> None:
        """The trial is lost: its server process is ended, its connection dropped."""
        trial.lost = True
        await self._end_backend(trial.connection)
        with contextlib.suppress(asyncpg.InterfaceError):
            trial.connection.terminate()

    async def _tried(
        self,
        trial: Trial,
        *,
        path: str,
        sql: str,
        params: Sequence[JsonValue],
        write: bool,
        bound_bytes: int,
        bounds: BaseBounds,
    ) -> BaseRows | BaseRefused:
        """One statement of a dry run, in its trial (see the module docstring)."""
        if trial.lost:
            return BaseRefused("sql_error", _TRIAL_LOST)
        connection = trial.connection
        try:
            async with asyncio.timeout(bounds.request_seconds):
                if not await self._same_transaction(trial):
                    await self._lose(trial)
                    return BaseRefused("sql_error", _TRIAL_LOST)
                answer = await self._in_savepoint(
                    trial,
                    path=path,
                    sql=sql,
                    params=params,
                    write=write,
                    bound_bytes=bound_bytes,
                    bounds=bounds,
                )
                if trial.lost or not await self._same_transaction(trial):
                    # The statement ended or replaced the trial's transaction: nothing more runs.
                    await self._lose(trial)
                    return BaseRefused("sql_error", _TRIAL_LOST)
                if write:
                    trial.wrote = True
                return answer
        except TimeoutError:
            # Past the core's deadline: the trial's server process is ended, and the trial with
            # it.
            trial.lost = True
            await self._end_backend(connection)
            with contextlib.suppress(asyncpg.InterfaceError):
                connection.terminate()
            return _too_long(bounds)

    async def _in_savepoint(
        self,
        trial: Trial,
        *,
        path: str,
        sql: str,
        params: Sequence[JsonValue],
        write: bool,
        bound_bytes: int,
        bounds: BaseBounds,
    ) -> BaseRows | BaseRefused:
        connection = trial.connection
        try:
            await connection.execute(f"SAVEPOINT {_SAVEPOINT}")
            await connection.execute(_settings(path, bounds))
            if not write:
                await connection.execute("SET LOCAL transaction_read_only = on")
            elif await _full(connection, sql, bound_bytes):
                await connection.execute(f"ROLLBACK TO SAVEPOINT {_SAVEPOINT}")
                return BaseRefused("full", None)
            answer = await self._statement(connection, sql, params, bounds)
            # A read keeps nothing, and a write refused neither; a write's rows are kept until
            # the trial ends.
            keeps = write and isinstance(answer, BaseRows)
            end = "RELEASE SAVEPOINT" if keeps else "ROLLBACK TO SAVEPOINT"
            await connection.execute(f"{end} {_SAVEPOINT}")
            return answer
        except (
            asyncpg.PostgresError,
            asyncpg.InterfaceError,
            asyncpg.InternalClientError,
            _Unusable,
        ) as error:
            with contextlib.suppress(asyncpg.PostgresError, asyncpg.InterfaceError):
                await connection.execute(f"ROLLBACK TO SAVEPOINT {_SAVEPOINT}")
            if connection.is_closed():
                trial.lost = True
            return _server_refused(error)

    async def _statement(
        self, connection: PgConnection, sql: str, params: Sequence[JsonValue], bounds: BaseBounds
    ) -> BaseRows | BaseRefused:
        statement = await connection.prepare(sql)
        types = [parameter.name for parameter in statement.get_parameters()]
        if len(types) != len(params):
            return BaseRefused(
                "sql_error", f"the statement takes {len(types)} parameter(s), {len(params)} given"
            )
        arguments = [_argument(value, name) for value, name in zip(params, types, strict=True)]
        columns = [(column.name, column.type.name) for column in statement.get_attributes()]
        if not columns:
            await statement.fetch(*arguments)
            return BaseRows([])
        cursor = await statement.cursor(*arguments)
        rows: list[JsonObject] = []
        size = 2
        while True:
            # One row at a time: the size is counted before the next one is asked for.
            batch = await cursor.fetch(1)
            if not batch:
                break
            if len(rows) == bounds.rows:
                return BaseRefused("too_big", f"more than {bounds.rows} rows", "rows")
            (record,) = batch
            row: JsonObject = {
                name: _json_of(value, type_name)
                for (name, type_name), value in zip(columns, record.values(), strict=True)
            }
            size += len(json.dumps(row, ensure_ascii=False).encode("utf-8")) + 1
            if size > ANSWER_CEILING_BYTES:
                # The code's ceiling, whatever result_bytes says: the core never gathers more.
                return BaseRefused("too_big", ANSWER_CEILING_WORDS)
            if size > bounds.answer_bytes:
                return BaseRefused(
                    "too_big", f"more than {bounds.answer_bytes} bytes", "result_bytes"
                )
            rows.append(row)
        return BaseRows(rows)

    async def close(self) -> None:
        """Close every pool (a later request makes its pool again)."""
        pools, self._pools = list(self._pools.values()), {}
        for pool in pools:
            await pool.close()


def _milliseconds(seconds: int) -> str:
    """``seconds`` as a timeout of the server, which takes ``_LONGEST_MS`` at most."""
    return f"{min(seconds * 1000, _LONGEST_MS)}ms"


def _server_times(bounds: BaseBounds) -> dict[str, str]:
    """The server's own times of a request, from ``bounds``: a statement's, and, past the request's
    by ``SERVER_AFTER_SECONDS``, the whole transaction's and a trial's idle time."""
    behind = _milliseconds(bounds.request_seconds + SERVER_AFTER_SECONDS)
    return {
        "statement_timeout": _milliseconds(bounds.statement_seconds),
        "transaction_timeout": behind,
        "idle_in_transaction_session_timeout": behind,
    }


def session_bounds(bounds: BaseBounds) -> str:
    """What a connection is set with before a request's transaction starts (the server arms the
    transaction's timer then, from the value in force): the times of ``bounds``. ``DISCARD ALL``
    takes them back when the connection goes back to the pool."""
    return "; ".join(f"SET {name} = '{given}'" for name, given in _server_times(bounds).items())


def _settings(path: str, bounds: BaseBounds) -> str:
    """What a request's transaction is set with first: its search path and the bounds a session
    could change itself (``setup.SESSION_SETTINGS``), the times those of ``bounds``."""
    given = {**ROLE_SETTINGS, **_server_times(bounds)}
    return "; ".join(
        [
            f"SET LOCAL search_path = {path}",
            *(f"SET LOCAL {name} = '{given[name]}'" for name in SESSION_SETTINGS),
        ]
    )


# The SQLSTATE of a statement the server cancelled: past ``statement_timeout``, here. (A
# transaction the server ends past ``transaction_timeout`` closes the connection: the client reads
# no SQLSTATE of the server's, and the core's own deadline, earlier, ends a request first.)
_CANCELLED: Final = "57014"


def _server_refused(error: BaseException) -> BaseRefused:
    """What the server's refusal of a statement is told: one it cancelled went past its time
    (``statement_seconds``)."""
    cancelled = str(getattr(error, "sqlstate", "") or "") == _CANCELLED
    return BaseRefused("sql_error", _detail(error), "statement_seconds" if cancelled else None)


def _too_long(bounds: BaseBounds) -> BaseRefused:
    """A request the core ended past its time."""
    return BaseRefused(
        "sql_error", f"the request took more than {bounds.request_seconds} s", "request_seconds"
    )


async def _full(connection: PgConnection, sql: str, bound_bytes: int) -> bool:
    """Whether a write is refused for room: the database is bigger than ``bound_bytes`` and the
    write does not free room. It is read once, before."""
    if _FREES_ROOM.match(sql):
        return False
    size = await connection.fetchval("SELECT pg_database_size(current_database())")
    return isinstance(size, int) and size > bound_bytes


async def _roll_back(transaction: PgTransaction) -> None:
    # The connection is gone or the transaction already ended: the pool resets it.
    with contextlib.suppress(asyncpg.PostgresError, asyncpg.InterfaceError):
        await transaction.rollback()
