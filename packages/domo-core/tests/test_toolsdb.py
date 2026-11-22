"""The tools' own database: a common space and one corner per person (spec 5.2, "Base").

Everything runs on the real test PostgreSQL, in a tools' database of the test's own
(``tools_admin``, ``tools_base``). What keeps a program in its place is PostgreSQL's: roles,
privileges and transaction modes. Each test below sends what a hostile or mistaken program could,
and checks the server refuses it.
"""

import asyncio
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from domo_core.accounts.models import Person
from domo_core.audit.models import AuditEntry
from domo_core.toolsdb import store
from domo_core.toolsdb.setup import HOUSE_ROLE, ROLE_CONFIG, ToolsAdmin
from domo_core.toolsdb.spaces import (
    CORNER_DROPPED,
    ORPHAN_GRACE,
    SpaceLogin,
    ToolsSpace,
    drop_corners_of_the_gone,
    drop_orphans,
    space_for,
)
from domo_core.toolsdb.store import BaseBounds, BaseRefused, BaseRows, ToolsBase
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

GB = 1024**3

AddPerson = Callable[..., Awaitable[Person]]


@pytest.fixture
async def dana(add_person: AddPerson) -> Person:
    return await add_person("Dana", "adult")


@pytest.fixture
async def paul(add_person: AddPerson) -> Person:
    return await add_person("Paul", "child")


@pytest.fixture
async def dana_login(session: AsyncSession, tools_admin: ToolsAdmin, dana: Person) -> SpaceLogin:
    return await space_for(session, tools_admin, dana.id)


@pytest.fixture
async def paul_login(session: AsyncSession, tools_admin: ToolsAdmin, paul: Person) -> SpaceLogin:
    return await space_for(session, tools_admin, paul.id)


@pytest.fixture
async def house_login(session: AsyncSession, tools_admin: ToolsAdmin) -> SpaceLogin:
    return await space_for(session, tools_admin, None)


async def read(base: ToolsBase, login: SpaceLogin, sql: str, *params, space="mine", **more):
    return await base.query(
        login, sql=sql, params=list(params), write=False, space=space, bound_bytes=GB, **more
    )


async def write(base: ToolsBase, login: SpaceLogin, sql: str, *params, space="mine", **more):
    return await base.query(
        login, sql=sql, params=list(params), write=True, space=space, bound_bytes=GB, **more
    )


# The rows an answer holds at the start (``madetools.settings``).
MAX_ROWS = BaseBounds.at_start().rows


def bounded(**given: int) -> BaseBounds:
    """The bounds at their starts, but those ``given``."""
    return replace(BaseBounds.at_start(), **given)


def refused(answer: BaseRows | BaseRefused, reason: str = "sql_error") -> bool:
    return isinstance(answer, BaseRefused) and answer.reason == reason


async def admin_value(tools_admin: ToolsAdmin, database_url: str, query: str, *args):
    """What the core's own user reads on the tools' database."""
    url = re.sub(r"^postgresql\+asyncpg", "postgresql", database_url)
    connection = await asyncpg.connect(url, database=tools_admin.database)
    try:
        return await connection.fetchval(query, *args)
    finally:
        await connection.close()


# --- the brief's tests ---------------------------------------------------------------------------


async def test_dana_creates_in_her_corner_and_paul_cannot_see_it(
    tools_base, dana_login, paul_login
):
    assert isinstance(
        await tools_base.query(
            dana_login,
            sql="CREATE TABLE notes (t text)",
            params=[],
            write=True,
            space="mine",
            bound_bytes=GB,
        ),
        BaseRows,
    )
    found = await tools_base.query(
        paul_login,
        sql=f"SELECT * FROM {dana_login.corner}.notes",
        params=[],
        write=False,
        space="mine",
        bound_bytes=GB,
    )
    assert isinstance(found, BaseRefused) and found.reason == "sql_error"
    # Nor write in it, nor make anything there.
    assert refused(
        await write(tools_base, paul_login, f"INSERT INTO {dana_login.corner}.notes VALUES ('x')")
    )
    assert refused(
        await write(tools_base, paul_login, f"CREATE TABLE {dana_login.corner}.mine (t text)")
    )
    # Dana sees hers.
    assert isinstance(
        await write(tools_base, dana_login, "INSERT INTO notes VALUES ($1)", "hi"), BaseRows
    )
    assert await read(tools_base, dana_login, "SELECT t FROM notes") == BaseRows([{"t": "hi"}])


async def test_a_table_of_the_common_space_is_everyones(tools_base, dana_login, house_login):
    made = await write(tools_base, dana_login, "CREATE TABLE shared (n int)", space="common")
    assert isinstance(made, BaseRows)
    assert isinstance(
        await write(tools_base, dana_login, "INSERT INTO shared VALUES ($1)", 3, space="common"),
        BaseRows,
    )
    assert await read(tools_base, house_login, "SELECT n FROM common.shared", space="common") == (
        BaseRows([{"n": 3}])
    )
    assert isinstance(
        await write(tools_base, house_login, "DROP TABLE shared", space="common"), BaseRows
    )
    assert refused(
        await read(tools_base, dana_login, "SELECT n FROM common.shared", space="common")
    )


async def test_the_core_database_is_out_of_reach(tools_base, dana_login, paul_login):
    assert refused(await read(tools_base, dana_login, "SELECT * FROM public.persons"))
    assert refused(await read(tools_base, dana_login, "SELECT pg_read_file('/etc/passwd')"))
    assert refused(await write(tools_base, dana_login, f"SET ROLE {HOUSE_ROLE}"))
    assert refused(await write(tools_base, dana_login, f"SET ROLE {paul_login.role_name}"))


async def test_two_statements_in_one_request_are_refused(tools_base, dana_login):
    assert isinstance(await write(tools_base, dana_login, "CREATE TABLE x (n int)"), BaseRows)
    assert refused(await write(tools_base, dana_login, "SELECT 1; DROP TABLE x"))
    assert await read(tools_base, dana_login, "SELECT count(*) AS n FROM x") == BaseRows([{"n": 0}])


async def test_a_read_cannot_write(tools_base, dana_login):
    assert isinstance(await write(tools_base, dana_login, "CREATE TABLE x (n int)"), BaseRows)
    assert refused(await read(tools_base, dana_login, "INSERT INTO x VALUES (1)"))
    assert refused(await read(tools_base, dana_login, "CREATE TABLE y (n int)"))
    assert refused(
        await read(
            tools_base,
            dana_login,
            "WITH w AS (INSERT INTO x VALUES (1) RETURNING n) SELECT * FROM w",
        )
    )
    assert await read(tools_base, dana_login, "SELECT count(*) AS n FROM x") == BaseRows([{"n": 0}])


async def test_a_long_query_stops_at_ten_seconds(tools_base, dana_login):
    started = time.monotonic()
    assert refused(await read(tools_base, dana_login, "SELECT pg_sleep(11)"))
    assert time.monotonic() - started < 12


async def test_over_the_bound_only_drop_and_truncate_pass(tools_base, dana_login):
    assert isinstance(await write(tools_base, dana_login, "CREATE TABLE x (n int)"), BaseRows)
    full = await tools_base.query(
        dana_login,
        sql="INSERT INTO x VALUES (1)",
        params=[],
        write=True,
        space="mine",
        bound_bytes=1,
    )
    assert full == BaseRefused("full", None)
    emptied = await tools_base.query(
        dana_login, sql="TRUNCATE x", params=[], write=True, space="mine", bound_bytes=1
    )
    assert isinstance(emptied, BaseRows)
    dropped = await tools_base.query(
        dana_login, sql="  drop table x", params=[], write=True, space="mine", bound_bytes=1
    )
    assert isinstance(dropped, BaseRows)
    # A read is no write: it passes.
    assert isinstance(
        await tools_base.query(
            dana_login, sql="SELECT 1 AS n", params=[], write=False, space="mine", bound_bytes=1
        ),
        BaseRows,
    )


async def test_the_house_has_no_corner(tools_base, house_login):
    assert house_login.corner is None
    assert await read(tools_base, house_login, "SELECT 1 AS n") == BaseRefused("no_corner", None)
    assert await read(tools_base, house_login, "SELECT 1 AS n", space="common") == BaseRows(
        [{"n": 1}]
    )


async def test_a_corner_goes_with_its_person(
    session, tools_admin, tools_base, add_person, database_url
):
    tom = await add_person("Tom", "adult")
    login = await space_for(session, tools_admin, tom.id)
    assert isinstance(await write(tools_base, login, "CREATE TABLE kept (t text)"), BaseRows)
    # Something of Tom's in the common space too, given to everyone.
    assert isinstance(
        await write(tools_base, login, "CREATE TABLE left_behind (t text)", space="common"),
        BaseRows,
    )
    await session.delete(tom)
    await session.flush()
    assert await drop_corners_of_the_gone(session, tools_admin) == 1
    assert (
        await admin_value(
            tools_admin,
            database_url,
            "SELECT count(*) FROM pg_roles WHERE rolname = $1",
            login.role_name,
        )
        == 0
    )
    assert (
        await admin_value(
            tools_admin,
            database_url,
            "SELECT count(*) FROM pg_namespace WHERE nspname = $1",
            login.corner,
        )
        == 0
    )
    assert (
        await admin_value(
            tools_admin, database_url, "SELECT to_regclass('common.left_behind') IS NOT NULL"
        )
        is True
    )
    assert (await session.scalars(select(ToolsSpace))).all() == []
    entry = (
        await session.scalars(select(AuditEntry).where(AuditEntry.action == CORNER_DROPPED))
    ).one()
    assert entry.actor == "system" and entry.details == {}
    # A second pass finds nothing.
    assert await drop_corners_of_the_gone(session, tools_admin) == 0


async def test_role_and_schema_names_say_nothing_of_the_person(dana, dana_login, paul, paul_login):
    for person, login in ((dana, dana_login), (paul, paul_login)):
        for name in (login.role_name, login.corner or ""):
            assert person.id.hex not in name and str(person.id) not in name
            assert person.display_name.lower() not in name
        assert re.fullmatch(r"jt_[0-9a-f]{12}", login.role_name)
        assert re.fullmatch(r"c_[0-9a-f]{12}", login.corner or "")
    assert dana_login.role_name != paul_login.role_name


# --- one corner per person, and the passwords --------------------------------------------------


async def test_a_corner_is_made_once(session, tools_admin, dana, dana_login):
    assert await space_for(session, tools_admin, dana.id) == dana_login
    assert await space_for(session, tools_admin, None) == await space_for(
        session, tools_admin, None
    )


async def test_a_password_never_shows(dana_login, house_login):
    for login in (dana_login, house_login):
        assert login.password not in repr(login)
        assert len(login.password) >= 32


async def test_a_role_that_changes_its_own_password_gets_the_core_s_back(tools_admin, dana_login):
    first = ToolsBase(tools_admin)
    try:
        # The server lets a role change its own password: the core keeps reaching the database
        # with its own all the same.
        changed = await write(first, dana_login, "ALTER ROLE CURRENT_USER PASSWORD 'mine now'")
        assert changed == BaseRows([])
    finally:
        await first.close()
    second = ToolsBase(tools_admin)
    try:
        assert await read(second, dana_login, "SELECT 1 AS n") == BaseRows([{"n": 1}])
    finally:
        await second.close()


# --- what the brief's design left open, closed -------------------------------------------------


async def test_no_temporary_table_no_schema_no_extension(tools_base, dana_login):
    assert refused(await write(tools_base, dana_login, "CREATE TEMP TABLE t (n int)"))
    assert refused(await write(tools_base, dana_login, "CREATE SCHEMA mine"))
    assert refused(await write(tools_base, dana_login, "CREATE EXTENSION pgcrypto"))
    assert refused(await write(tools_base, dana_login, "CREATE EXTENSION dblink"))
    assert refused(await read(tools_base, dana_login, "SELECT dblink_connect('dbname=postgres')"))


async def test_no_function_no_trigger_no_do_block(tools_base, dana_login):
    assert refused(
        await write(
            tools_base,
            dana_login,
            "CREATE FUNCTION common.f() RETURNS int LANGUAGE sql AS 'SELECT 1'",
            space="common",
        )
    )
    assert refused(
        await write(
            tools_base,
            dana_login,
            "CREATE FUNCTION f() RETURNS trigger LANGUAGE plpgsql AS 'BEGIN RETURN NEW; END'",
        )
    )
    assert refused(await write(tools_base, dana_login, "DO 'BEGIN NULL; END'"))


async def test_the_common_space_itself_cannot_be_dropped_or_renamed(tools_base, dana_login):
    assert refused(
        await write(tools_base, dana_login, "DROP SCHEMA common CASCADE", space="common")
    )
    assert refused(
        await write(tools_base, dana_login, "ALTER SCHEMA common RENAME TO gone", space="common")
    )
    assert refused(await write(tools_base, dana_login, f"DROP SCHEMA {dana_login.corner} CASCADE"))
    assert refused(
        await write(tools_base, dana_login, f"ALTER SCHEMA {dana_login.corner} RENAME TO c_x")
    )


async def test_a_table_of_the_common_space_stays_everyones(
    tools_base, tools_admin, dana_login, database_url
):
    assert isinstance(
        await write(tools_base, dana_login, "CREATE TABLE t (n serial)", space="common"), BaseRows
    )
    owner = "SELECT tableowner FROM pg_tables WHERE schemaname = 'common' AND tablename = 't'"
    assert await admin_value(tools_admin, database_url, owner) == "jt_all"
    # Taking it for oneself does not hold either.
    await write(
        tools_base, dana_login, f"ALTER TABLE t OWNER TO {dana_login.role_name}", space="common"
    )
    assert await admin_value(tools_admin, database_url, owner) == "jt_all"


async def test_no_role_is_another(tools_base, dana_login):
    assert refused(await write(tools_base, dana_login, "SET ROLE jt_all"))
    assert refused(await write(tools_base, dana_login, "SET SESSION AUTHORIZATION jt_house"))
    assert refused(await write(tools_base, dana_login, "GRANT jt_house TO CURRENT_USER"))


async def test_no_file_no_program_no_large_object_no_lock_no_signal(tools_base, dana_login):
    assert refused(await write(tools_base, dana_login, "COPY (SELECT 1) TO PROGRAM 'id'"))
    assert refused(await write(tools_base, dana_login, "COPY (SELECT 1) TO '/tmp/x'"))
    assert refused(await read(tools_base, dana_login, "SELECT pg_ls_dir('.')"))
    assert refused(await write(tools_base, dana_login, "SELECT lo_create(0)"))
    assert refused(await write(tools_base, dana_login, "SELECT lo_from_bytea(0, 'x')"))
    assert refused(await read(tools_base, dana_login, "SELECT pg_advisory_lock(1)"))
    assert refused(await read(tools_base, dana_login, "SELECT pg_try_advisory_lock(1)"))
    assert refused(await read(tools_base, dana_login, "SELECT pg_terminate_backend(1)"))
    assert refused(await read(tools_base, dana_login, "SELECT pg_cancel_backend(1)"))


async def test_a_role_cannot_lift_its_own_bounds(tools_base, dana_login):
    # A role may set its own defaults: the core sets the bounds again in every transaction.
    lifted = await write(
        tools_base, dana_login, "ALTER ROLE CURRENT_USER SET statement_timeout = 0"
    )
    assert lifted == BaseRows([])
    await tools_base.close()
    started = time.monotonic()
    assert refused(await read(tools_base, dana_login, "SELECT pg_sleep(11)"))
    assert time.monotonic() - started < 12


async def test_a_role_cannot_keep_a_default_it_set_itself(
    tools_base, tools_admin, dana_login, database_url
):
    for change in (
        "ALTER ROLE CURRENT_USER SET hash_mem_multiplier = 1000",
        "ALTER ROLE CURRENT_USER SET max_parallel_workers_per_gather = 8",
        f"ALTER ROLE CURRENT_USER IN DATABASE {tools_admin.database} SET work_mem = '1GB'",
    ):
        assert await write(tools_base, dana_login, change) == BaseRows([])
        kept = await admin_value(
            tools_admin,
            database_url,
            "SELECT array_agg(c ORDER BY c) FROM pg_db_role_setting s, unnest(s.setconfig) AS c "
            "WHERE s.setrole = (SELECT oid FROM pg_roles WHERE rolname = $1)",
            dana_login.role_name,
        )
        assert kept == sorted(ROLE_CONFIG)
    await tools_base.close()  # a new session reads the role's defaults
    for name, value in (
        ("hash_mem_multiplier", "1"),
        ("max_parallel_workers_per_gather", "0"),
        ("maintenance_work_mem", "16MB"),
        ("work_mem", "8MB"),
        ("jit", "off"),
    ):
        assert await read(tools_base, dana_login, f"SHOW {name}") == BaseRows([{name: value}])


async def test_what_a_request_sets_on_its_session_does_not_last(tools_base, dana_login):
    assert isinstance(await read(tools_base, dana_login, "SET work_mem = '1GB'"), BaseRows)
    assert isinstance(await read(tools_base, dana_login, "SET search_path = common"), BaseRows)
    for _ in range(3):  # whatever pooled connection answers
        assert await read(tools_base, dana_login, "SHOW work_mem") == BaseRows(
            [{"work_mem": "8MB"}]
        )
        assert await read(tools_base, dana_login, "SELECT current_schema() AS s") == BaseRows(
            [{"s": dana_login.corner}]
        )


# --- the answers ----------------------------------------------------------------------------------


async def test_values_come_back_as_json(tools_base, dana_login):
    answer = await read(
        tools_base,
        dana_login,
        "SELECT $1::int AS i, $2::numeric AS n, $3::date AS d, $4::jsonb AS j, $5::text AS t, "
        "$6::timestamptz AS ts, NULL AS z, ARRAY[1, 2] AS a, true AS b",
        3,
        "1.50",
        "2030-01-02",
        {"k": [1, None]},
        "hi",
        "2030-01-02T03:04:05+00:00",
    )
    assert answer == BaseRows(
        [
            {
                "i": 3,
                "n": "1.50",
                "d": "2030-01-02",
                "j": {"k": [1, None]},
                "t": "hi",
                "ts": "2030-01-02T03:04:05+00:00",
                "z": None,
                "a": [1, 2],
                "b": True,
            }
        ]
    )


async def test_a_value_that_does_not_fit_or_a_missing_one_is_an_sql_error(tools_base, dana_login):
    assert refused(await read(tools_base, dana_login, "SELECT $1::int AS i", "three"))
    assert refused(await read(tools_base, dana_login, "SELECT $1::int AS i"))


async def test_too_many_rows_or_too_many_bytes_are_too_big(tools_base, dana_login):
    many = await read(tools_base, dana_login, f"SELECT generate_series(1, {MAX_ROWS + 1}) AS n")
    assert refused(many, "too_big")
    assert isinstance(many, BaseRefused) and many.setting == "rows"
    enough = await read(tools_base, dana_login, f"SELECT generate_series(1, {MAX_ROWS}) AS n")
    assert isinstance(enough, BaseRows) and len(enough.rows) == MAX_ROWS
    big = await read(tools_base, dana_login, "SELECT repeat('x', 70000) AS t")
    assert refused(big, "too_big")


async def test_the_rows_and_the_times_of_a_request_follow_the_bounds_given(
    tools_base, dana_login, house_login
):
    # The rows: raised past the start, and lowered under it.
    sql = f"SELECT generate_series(1, {MAX_ROWS + 500}) AS n"
    raised = await read(tools_base, dana_login, sql, bounds=bounded(rows=MAX_ROWS + 500))
    assert isinstance(raised, BaseRows) and len(raised.rows) == MAX_ROWS + 500
    lowered = await read(
        tools_base, dana_login, "SELECT 1 AS n UNION SELECT 2", bounds=bounded(rows=1)
    )
    assert lowered == BaseRefused("too_big", "more than 1 rows", "rows")
    # A statement past its seconds is cancelled by the server, said with the setting.
    slow = await read(
        tools_base, dana_login, "SELECT pg_sleep(2)", bounds=bounded(statement_seconds=1)
    )
    assert isinstance(slow, BaseRefused) and slow.setting == "statement_seconds"
    let_through = await read(
        tools_base, dana_login, "SELECT pg_sleep(2)::text AS s", bounds=bounded(statement_seconds=4)
    )
    assert let_through == BaseRows([{"s": ""}])
    # The whole request past its seconds is ended by the core's own deadline (the server's bound
    # comes after it), said with the setting.
    started = time.monotonic()
    ended = await read(
        tools_base,
        dana_login,
        "SELECT pg_sleep(5)",
        bounds=bounded(statement_seconds=60, request_seconds=1),
    )
    assert ended == BaseRefused("sql_error", "the request took more than 1 s", "request_seconds")
    assert time.monotonic() - started < 4
    # Huge bounds are what the server can hold at most, never refused.
    huge = bounded(rows=10**9, statement_seconds=10**9, request_seconds=10**9)
    assert await read(tools_base, dana_login, "SELECT 1 AS n", bounds=huge) == BaseRows([{"n": 1}])
    async with tools_base.trial(house_login, huge) as trial:
        assert await write(
            tools_base, house_login, "CREATE TABLE kept (n int)", space="common", trial=trial
        ) == BaseRows([])
        assert refused(await write(tools_base, house_login, "COMMIT", space="common", trial=trial))
    # Whatever the bounds, the trial kept nothing.
    gone = await read(
        tools_base, house_login, "SELECT to_regclass('common.kept')::text AS t", space="common"
    )
    assert gone == BaseRows([{"t": None}])


async def test_raised_times_of_a_request_are_the_bounds_the_server_keeps_too(
    tools_base, dana_login, house_login
):
    # Fix round 1, I1: a time raised past its start is the bound, not the role's own default.
    long_request = bounded(statement_seconds=60, request_seconds=20)
    slept = await read(
        tools_base, dana_login, "SELECT pg_sleep(14)::text AS s", bounds=long_request
    )
    assert slept == BaseRows([{"s": ""}])
    long_statement = bounded(statement_seconds=12, request_seconds=30)
    slept = await read(
        tools_base, dana_login, "SELECT pg_sleep(11)::text AS s", bounds=long_statement
    )
    assert slept == BaseRows([{"s": ""}])
    # A dry run that waits longer than the old idle bound between two requests keeps its trial.
    async with tools_base.trial(house_login, bounded(request_seconds=60)) as trial:
        tried = {"space": "common", "trial": trial, "bounds": bounded(request_seconds=60)}
        assert await read(tools_base, house_login, "SELECT 1 AS n", **tried) == BaseRows([{"n": 1}])
        await asyncio.sleep(32)
        assert await read(tools_base, house_login, "SELECT 2 AS n", **tried) == BaseRows([{"n": 2}])
        assert not trial.lost


async def test_a_transaction_the_server_ends_closes_its_connection_with_no_sqlstate_of_its_own(
    tools_admin, database_url
):
    # Why the core's deadline, not the server's, ends a request (``setup.SERVER_AFTER_SECONDS``):
    # past its ``transaction_timeout`` the server closes the connection, and the client reads no
    # SQLSTATE of the server's (asyncpg's own 08003), only its words. Asked of the server itself.
    url = re.sub(r"^postgresql\+asyncpg", "postgresql", database_url)
    connection = await asyncpg.connect(url, database=tools_admin.database)
    try:
        await connection.execute("SET transaction_timeout = '500ms'")
        with pytest.raises(asyncpg.ConnectionDoesNotExistError) as ended:
            await connection.execute("BEGIN; SELECT pg_sleep(3)")
        assert "transaction timeout" in str(ended.value)
        assert ended.value.sqlstate == "08003"
    finally:
        await connection.close()


async def test_rows_of_several_megabytes_are_too_big_and_read_one_at_a_time(
    tools_base, dana_login, monkeypatch
):
    asked: list[int] = []
    real_fetch = ToolsBase._statement

    async def counting(self, connection, sql, params, bounds):
        # Every batch the core asks the server for holds one row at most.
        class Watched:
            def __getattr__(self, name):
                return getattr(connection, name)

            async def prepare(self, query):
                statement = await connection.prepare(query)

                class Statement:
                    def __getattr__(self, name):
                        return getattr(statement, name)

                    def cursor(self, *args):
                        async def opened():
                            cursor = await statement.cursor(*args)

                            class Cursor:
                                async def fetch(self, n):
                                    asked.append(n)
                                    return await cursor.fetch(n)

                            return Cursor()

                        return opened()

                return Statement()

        return await real_fetch(self, Watched(), sql, params, bounds)

    monkeypatch.setattr(ToolsBase, "_statement", counting)
    answer = await read(
        tools_base, dana_login, "SELECT repeat('x', 5000000) AS t FROM generate_series(1, 20)"
    )
    assert refused(answer, "too_big")
    assert asked == [1]  # the first row was past the bound: no other was read


async def test_a_read_is_never_held_past_the_code_s_ceiling_whatever_result_bytes(
    tools_base, dana_login
):
    # Re-review, ruling R37: result_bytes of 2**40 still holds a read to the 8 MB ceiling, and
    # the refusal says it is the code's, not a setting to raise.
    huge = bounded(answer_bytes=2**40)
    over = await read(
        tools_base,
        dana_login,
        "SELECT repeat('x', 1000000) AS t FROM generate_series(1, 9)",
        bounds=huge,
    )
    assert isinstance(over, BaseRefused) and over.reason == "too_big"
    assert over.setting is None and "ceiling" in str(over.detail)
    under = await read(
        tools_base,
        dana_login,
        "SELECT repeat('x', 1000000) AS t FROM generate_series(1, 7)",
        bounds=huge,
    )
    assert isinstance(under, BaseRows) and len(under.rows) == 7


async def test_a_dry_run_leaves_nothing(tools_base, house_login):
    house = house_login
    assert isinstance(
        await write(tools_base, house, "CREATE TABLE x (n int)", space="common"), BaseRows
    )
    async with tools_base.trial(house) as trial:
        tried = {"space": "common", "trial": trial}
        # One transaction for the whole dry run: each statement sees what the last one wrote.
        assert await write(tools_base, house, "INSERT INTO x VALUES (1) RETURNING n", **tried) == (
            BaseRows([{"n": 1}])
        )
        assert isinstance(
            await write(tools_base, house, "CREATE TABLE y (n int)", **tried), BaseRows
        )
        assert await write(tools_base, house, "INSERT INTO y VALUES (2)", **tried) == BaseRows([])
        assert await read(tools_base, house, "SELECT n FROM y", **tried) == BaseRows([{"n": 2}])
        # A read writes nothing, and the next write is allowed again.
        assert refused(await read(tools_base, house, "DELETE FROM x RETURNING n", **tried))
        # A statement refused does not end the trial: the next one runs.
        assert refused(await write(tools_base, house, "SELECT * FROM nowhere", **tried))
        assert await write(tools_base, house, "DELETE FROM x RETURNING n", **tried) == BaseRows(
            [{"n": 1}]
        )
        assert await read(tools_base, house, "SELECT count(*) AS n FROM x", **tried) == BaseRows(
            [{"n": 0}]
        )
    # Rolled back at its end: nothing stays.
    common = {"space": "common"}
    assert await read(tools_base, house, "SELECT count(*) AS n FROM x", **common) == BaseRows(
        [{"n": 0}]
    )
    assert refused(await read(tools_base, house, "SELECT * FROM y", **common))
    assert isinstance(
        await write(tools_base, house, "INSERT INTO x VALUES (3)", **common), BaseRows
    )


async def test_a_dry_run_past_the_core_s_deadline_is_lost_and_refuses_what_follows(
    tools_base, house_login
):
    short = bounded(request_seconds=1)
    async with tools_base.trial(house_login, short) as trial:
        tried = {"space": "common", "trial": trial, "bounds": short}
        late = await read(tools_base, house_login, "SELECT pg_sleep(5)", **tried)
        assert refused(late) and trial.lost
        after = await read(tools_base, house_login, "SELECT 1 AS n", **tried)
        assert refused(after) and "ended" in (after.detail or "")
    # The base answers the next request.
    assert await read(tools_base, house_login, "SELECT 1 AS n", space="common") == BaseRows(
        [{"n": 1}]
    )


# What a dry run's program may send to keep what it did, or to end or replace the trial's
# transaction (fix round 1, C-1): each is tried after a row was written; nothing may stay.
ESCAPES_OF_A_TRIAL: list[tuple[str, list[tuple[str, str]]]] = [
    ("commit", [("write", "COMMIT")]),
    ("commit as a read", [("read", "COMMIT")]),
    ("commented commit", [("write", "/* x */ commit")]),
    ("end", [("write", "END")]),
    ("commit and chain", [("write", "COMMIT AND CHAIN"), ("write", "INSERT INTO x VALUES (2)")]),
    (
        "rollback and chain",
        [
            ("write", "ROLLBACK AND CHAIN"),
            ("write", "INSERT INTO x VALUES (2)"),
            ("write", "COMMIT"),
        ],
    ),
    ("rollback", [("write", "ROLLBACK"), ("write", "INSERT INTO x VALUES (2)")]),
    ("abort", [("write", "ABORT"), ("write", "INSERT INTO x VALUES (2)")]),
    ("prepare transaction", [("write", "PREPARE TRANSACTION 'kept'")]),
    ("constraints immediate", [("write", "SET CONSTRAINTS ALL IMMEDIATE"), ("write", "COMMIT")]),
    (
        "guard disabled",
        [
            ("write", "ALTER TABLE domo_guard.trial_guard DISABLE TRIGGER ALL"),
            ("write", "COMMIT"),
        ],
    ),
    ("guard dropped", [("write", "DROP TABLE domo_guard.trial_guard"), ("write", "COMMIT")]),
    ("guard emptied", [("write", "DELETE FROM domo_guard.trial_guard"), ("write", "COMMIT")]),
    ("guard truncated", [("write", "TRUNCATE domo_guard.trial_guard"), ("write", "COMMIT")]),
    (
        "replication role",
        [("write", "SET session_replication_role = replica"), ("write", "COMMIT")],
    ),
    (
        "a default of the role",
        [("write", "ALTER ROLE CURRENT_USER SET work_mem = '1GB'"), ("write", "COMMIT")],
    ),
    # The guard is planted before the first savepoint (Task 12, N-2 of Task 11): the core's own
    # savepoint, released, rolled back to or named again by the program, never takes it away,
    # and a transaction started inside the trial is the trial's.
    (
        "the core's savepoint released",
        [("write", f"RELEASE SAVEPOINT {store._SAVEPOINT}"), ("write", "COMMIT")],
    ),
    (
        "rolled back to the core's savepoint",
        [("write", f"ROLLBACK TO SAVEPOINT {store._SAVEPOINT}"), ("write", "COMMIT")],
    ),
    (
        "a savepoint of the core's name",
        [
            ("write", f"SAVEPOINT {store._SAVEPOINT}"),
            ("write", "INSERT INTO x VALUES (2)"),
            ("write", "COMMIT"),
        ],
    ),
    (
        "start transaction",
        [
            ("write", "START TRANSACTION"),
            ("write", "INSERT INTO x VALUES (2)"),
            ("write", "COMMIT"),
        ],
    ),
]


async def test_a_request_that_finds_no_free_connection_is_refused_in_time(
    tools_base, house_login, monkeypatch
):
    """Two dry runs hold the house's two connections: a request, and a third trial, are refused
    cleanly once ``ACQUIRE_SECONDS`` passed, never left waiting (fix round 1 of Task 12, F4)."""
    monkeypatch.setattr(store, "ACQUIRE_SECONDS", 0.5)
    async with tools_base.trial(house_login), tools_base.trial(house_login):
        started = time.monotonic()
        busy = await read(tools_base, house_login, "SELECT 1 AS n", space="common")
        assert refused(busy) and "busy" in (busy.detail or ""), busy
        with pytest.raises(store.BaseBusy):
            async with tools_base.trial(house_login):
                pass
        assert time.monotonic() - started < 5
    # Once they end, the next request has its connection.
    assert await read(tools_base, house_login, "SELECT 1 AS n", space="common") == BaseRows(
        [{"n": 1}]
    )


def test_a_trial_reads_its_identity_from_the_catalog_whatever_the_search_path():
    """The identity a trial checks before each statement is the catalog's own function, named
    with its schema (Task 12, N-3 of Task 11)."""
    assert store._XACT == "SELECT pg_catalog.pg_current_xact_id()::text"


async def test_a_dry_run_can_never_keep_what_it_did(
    tools_base, tools_admin, house_login, database_url
):
    common = {"space": "common"}
    assert isinstance(
        await write(tools_base, house_login, "CREATE TABLE x (n int)", **common), BaseRows
    )
    for case, statements in ESCAPES_OF_A_TRIAL:
        async with tools_base.trial(house_login) as trial:
            tried = {"space": "common", "trial": trial}
            assert await write(tools_base, house_login, "INSERT INTO x VALUES (1)", **tried) == (
                BaseRows([])
            ), case
            for kind, sql in statements:
                send = write if kind == "write" else read
                await send(tools_base, house_login, sql, **tried)
        left = await read(tools_base, house_login, "SELECT count(*) AS n FROM x", **common)
        assert left == BaseRows([{"n": 0}]), case
        kept = await admin_value(
            tools_admin,
            database_url,
            "SELECT array_agg(c ORDER BY c) FROM pg_db_role_setting s, unnest(s.setconfig) AS c "
            "WHERE s.setrole = (SELECT oid FROM pg_roles WHERE rolname = $1)",
            house_login.role_name,
        )
        assert kept == sorted(ROLE_CONFIG), case
        prepared = await admin_value(
            tools_admin, database_url, "SELECT count(*) FROM pg_prepared_xacts"
        )
        assert prepared == 0, case
        # A real request works after it.
        assert isinstance(
            await write(tools_base, house_login, "DELETE FROM x", **common), BaseRows
        ), case


async def test_a_trial_whose_transaction_was_replaced_runs_nothing_more(tools_base, house_login):
    common = {"space": "common"}
    assert isinstance(
        await write(tools_base, house_login, "CREATE TABLE x (n int)", **common), BaseRows
    )
    async with tools_base.trial(house_login) as trial:
        tried = {"space": "common", "trial": trial}
        await write(tools_base, house_login, "ROLLBACK AND CHAIN", **tried)
        assert trial.lost
        after = await write(tools_base, house_login, "INSERT INTO x VALUES (2)", **tried)
        assert refused(after) and "ended" in (after.detail or "")


async def test_a_dry_run_is_ended_at_its_deadline_whatever_it_sets(tools_base, house_login):
    common = {"space": "common"}
    assert isinstance(
        await write(tools_base, house_login, "CREATE TABLE x (n int)", **common), BaseRows
    )
    async with tools_base.trial(house_login, bounded(request_seconds=2)) as trial:
        tried = {"space": "common", "trial": trial}
        for lift in (
            "SET transaction_timeout = 0",
            "SET idle_in_transaction_session_timeout = 0",
            "SET statement_timeout = 0",
            "LOCK TABLE x IN ACCESS EXCLUSIVE MODE",
        ):
            assert await write(tools_base, house_login, lift, **tried) == BaseRows([]), lift
        await asyncio.sleep(3)
        assert trial.lost
        assert refused(await read(tools_base, house_login, "SELECT 1 AS n", **tried))
    # The lock went with it: a real request on that table works at once.
    assert await read(tools_base, house_login, "SELECT count(*) AS n FROM x", **common) == (
        BaseRows([{"n": 0}])
    )


async def test_a_trial_ended_by_a_cancellation_leaves_nothing_and_no_dirty_connection(
    tools_base, tools_admin, house_login, database_url
):
    common = {"space": "common"}
    assert isinstance(
        await write(tools_base, house_login, "CREATE TABLE x (n int)", **common), BaseRows
    )
    holding = asyncio.Event()
    pids: list[int] = []

    async def dry_run() -> None:
        async with tools_base.trial(house_login) as trial:
            pids.append(trial.connection.get_server_pid())
            await write(tools_base, house_login, "INSERT INTO x VALUES (1)", trial=trial, **common)
            holding.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(dry_run())
    await holding.wait()
    task.cancel()
    # Cancelled again while it ends: the trial still ends, its connection dropped.
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Its connection was dropped, not given back to the pool in its transaction.
    [pid] = pids
    alive = await admin_value(
        tools_admin, database_url, "SELECT count(*) FROM pg_stat_activity WHERE pid = $1", pid
    )
    assert alive == 0
    assert await read(tools_base, house_login, "SELECT count(*) AS n FROM x", **common) == (
        BaseRows([{"n": 0}])
    )


async def test_an_error_s_detail_is_short_and_says_the_sqlstate(tools_base, dana_login):
    answer = await read(tools_base, dana_login, "SELECT * FROM nowhere")
    assert isinstance(answer, BaseRefused) and answer.detail is not None
    assert answer.detail.startswith("42P01: ") and len(answer.detail) <= 200
    assert dana_login.password not in answer.detail


async def test_a_person_s_space_needs_the_person(session, tools_admin, database_url):
    with pytest.raises(IntegrityError):
        await space_for(session, tools_admin, uuid.uuid4())
    roles = "SELECT count(*) FROM pg_roles WHERE rolname ~ '^jt_[0-9a-f]{12}$'"
    assert await admin_value(tools_admin, database_url, roles) == 0


# --- what a program could still reach through the server, closed ---------------------------------


@dataclass(frozen=True)
class PaulsCorner:
    """What Paul's program keeps in his corner: a table, and rows numbered by an identity column
    (whose sequence is ``sequence``). Dana's program can read their numbers in ``pg_class``."""

    secret: int
    sequence: int


async def next_number(tools_base, paul_login) -> object:
    numbered = await write(
        tools_base, paul_login, "INSERT INTO numbered (t) VALUES ('next') RETURNING n"
    )
    assert isinstance(numbered, BaseRows)
    return numbered.rows[0]["n"]


@pytest.fixture
async def pauls_corner(tools_base, tools_admin, paul_login, database_url) -> PaulsCorner:
    for statement in (
        "CREATE TABLE secret (t text)",
        "INSERT INTO secret VALUES ('paul only')",
        # The same value again and again, and the table analysed: the server's statistics of
        # the column now hold it among its most common values.
        "INSERT INTO secret SELECT 'paul only' FROM generate_series(1, 9)",
        "ANALYZE secret",
        # A program numbers its rows with an identity column: no ``nextval`` of its own needed.
        "CREATE TABLE numbered (n int GENERATED ALWAYS AS IDENTITY, t text)",
        "INSERT INTO numbered (t) VALUES ('a'), ('b'), ('c')",
    ):
        assert await write(tools_base, paul_login, statement) == BaseRows([]), statement
    corner = paul_login.corner
    secret = await admin_value(
        tools_admin, database_url, f"SELECT '{corner}.secret'::regclass::oid::int"
    )
    sequence = await admin_value(
        tools_admin,
        database_url,
        f"SELECT pg_get_serial_sequence('{corner}.numbered', 'n')::regclass::oid::int",
    )
    assert isinstance(secret, int) and isinstance(sequence, int)
    return PaulsCorner(secret, sequence)


def _planted(template: str, paul_login: SpaceLogin, pauls: PaulsCorner) -> str:
    return template.format(corner=paul_login.corner, secret=pauls.secret, sequence=pauls.sequence)


# Each of these, left callable, carries something of Paul's corner to Dana, or resets his
# sequence, when Paul's program writes in the common space. Each names Paul's objects in a way Dana
# can write (by a name read at run time, or by number): the plant itself succeeds.
_PLANTED_DEFAULTS = [
    "database_to_xml(true, false, '')::text",
    "schema_to_xml('{corner}', true, false, '')::text",
    "query_to_xml('SELECT * FROM {corner}.secret', true, false, '')::text",
    "table_to_xml({secret}::regclass, true, false, '')::text",
    "pg_sequence_last_value({sequence}::regclass)::text",
    "setval({sequence}::regclass, 1)::text",
    "nextval({sequence}::regclass)::text",
    "pg_relation_size({secret}::regclass)::text",
    # The text of the statement Paul's program sends, whatever it names in his corner.
    "current_query()",
]


@pytest.mark.parametrize("default", _PLANTED_DEFAULTS)
async def test_a_default_on_a_common_table_never_reaches_another_s_corner(
    tools_base, dana_login, paul_login, pauls_corner, default
):
    planted = _planted(default, paul_login, pauls_corner)
    made = await write(
        tools_base,
        dana_login,
        f"CREATE TABLE inbox (n int, d text DEFAULT {planted})",
        space="common",
    )
    assert made == BaseRows([])  # planted
    await write(tools_base, paul_login, "INSERT INTO inbox (n) VALUES (1)", space="common")
    found = await read(tools_base, dana_login, "SELECT d FROM inbox", space="common")
    assert isinstance(found, BaseRows) and all(row["d"] is None for row in found.rows)
    assert await next_number(tools_base, paul_login) == 4  # his sequence untouched


# What a rule on a common table would carry, run as Paul when he writes there. Every plant would
# succeed if rules were allowed (each names Paul's objects by a name read at run time, or by
# number; ``pg_stats`` is read through ``pg_catalog``). The ``SELECT`` of a table in Paul's corner
# by its name is not among them: naming it needs a right on Paul's schema, so that plant would fail
# by itself and prove nothing.
_PLANTED_RULES = [
    "INSERT INTO leak VALUES (database_to_xml(true, false, '')::text)",
    "INSERT INTO leak VALUES "
    "(query_to_xml('SELECT * FROM {corner}.secret', true, false, '')::text)",
    "INSERT INTO leak SELECT word FROM ts_stat('SELECT to_tsvector(t) FROM {corner}.secret')",
    "INSERT INTO leak VALUES (pg_sequence_last_value({sequence}::regclass)::text)",
    "INSERT INTO leak VALUES (setval({sequence}::regclass, 1)::text)",
    # The real values of Paul's columns (the most common ones), filtered by the privileges of
    # whoever reads them: Paul.
    "INSERT INTO leak SELECT most_common_vals::text FROM pg_stats WHERE schemaname = '{corner}'",
]


@pytest.mark.parametrize("action", _PLANTED_RULES)
async def test_a_rule_on_a_common_table_never_reaches_another_s_corner(
    tools_base, dana_login, paul_login, pauls_corner, action
):
    for made in ("CREATE TABLE leak (d text)", "CREATE TABLE inbox (n int)"):
        assert await write(tools_base, dana_login, made, space="common") == BaseRows([])
    planted = _planted(action, paul_login, pauls_corner)
    rule = await write(
        tools_base,
        dana_login,
        f"CREATE RULE copy AS ON INSERT TO inbox DO ALSO {planted}",
        space="common",
    )
    assert isinstance(rule, BaseRefused) and (rule.detail or "").startswith("42501: ")
    await write(tools_base, paul_login, "INSERT INTO inbox VALUES (1)", space="common")
    assert await read(tools_base, dana_login, "SELECT d FROM leak", space="common") == BaseRows([])
    assert await next_number(tools_base, paul_login) == 4


@pytest.mark.parametrize("space", ["common", "mine"])
async def test_no_program_makes_a_rule(tools_base, dana_login, space):
    for made in ("CREATE TABLE t (n int)", "CREATE TABLE u (n int)"):
        assert await write(tools_base, dana_login, made, space=space) == BaseRows([])
    for rule in (
        "CREATE RULE r AS ON INSERT TO t DO ALSO INSERT INTO u VALUES (1)",
        "CREATE RULE r AS ON INSERT TO t DO INSTEAD NOTHING",
        "CREATE OR REPLACE RULE r AS ON UPDATE TO t DO ALSO NOTHING",
    ):
        made = await write(tools_base, dana_login, rule, space=space)
        assert isinstance(made, BaseRefused) and (made.detail or "").startswith("42501: "), rule
    # A view is still a view (its own rule is the server's, not a ``CREATE RULE``).
    view = await write(tools_base, dana_login, "CREATE VIEW v AS SELECT n FROM t", space=space)
    assert view == BaseRows([])


async def test_the_statistics_of_the_columns_are_not_shown(tools_base, dana_login, pauls_corner):
    for view in ("pg_stats", "pg_stats_ext", "pg_stats_ext_exprs"):
        found = await read(tools_base, dana_login, f"SELECT schemaname, tablename FROM {view}")
        assert isinstance(found, BaseRefused) and (found.detail or "").startswith("42501: "), view
        # A column of a type the core cannot read (``anyarray``) is a refusal, never an error of
        # the core.
        assert refused(await read(tools_base, dana_login, f"SELECT * FROM {view}")), view


async def test_a_policy_on_a_common_table_never_reads_another_s_column_values(
    tools_base, dana_login, paul_login, pauls_corner
):
    # A row-level policy is read as whoever writes: Dana would learn, from whether Paul's row got
    # in, whether his columns hold a value (one bit per insert). Its subquery can only reach
    # ``pg_stats``, which no program may read.
    for statement in (
        "CREATE TABLE inbox (n int)",
        "ALTER TABLE inbox ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE inbox FORCE ROW LEVEL SECURITY",
        "CREATE POLICY seen ON inbox FOR SELECT USING (true)",
        "CREATE POLICY probe ON inbox FOR INSERT WITH CHECK (EXISTS (SELECT 1 FROM pg_stats "
        f"WHERE schemaname = '{paul_login.corner}' AND most_common_vals::text LIKE '%paul only%'))",
    ):
        assert await write(tools_base, dana_login, statement, space="common") == BaseRows([])
    await write(tools_base, paul_login, "INSERT INTO inbox VALUES (1)", space="common")
    assert await read(tools_base, dana_login, "SELECT n FROM inbox", space="common") == BaseRows([])


# Read from the catalog, not from a list: every function of the server that takes a cursor, a
# query given as text, or belongs to the families that turn a table, a schema or the database into
# XML; and those that read, move or measure a sequence or a table given by its number (a function
# the server adds to them later is caught too).
_CORNER_REACHING = """
SELECT p.oid::regprocedure::text FROM pg_proc p
WHERE p.pronamespace = 'pg_catalog'::regnamespace AND (
    'refcursor'::regtype = ANY (p.proargtypes)
    OR (p.proargnames IS NOT NULL AND p.proargnames[1] = 'query'
        AND p.proargtypes[0] = 'text'::regtype)
    OR p.proname ~ '_to_xml'
    OR p.proname IN ('ts_stat', 'ts_rewrite', 'lastval', 'pg_stat_have_stats')
    OR ('regclass'::regtype = ANY (p.proargtypes)
        AND p.proname ~ '(^|_)(next|set|curr)val$|^pg_sequence_|_size$|^(brin|gin)_')
    OR p.proname IN ('current_query', 'pg_prepared_statement', 'pg_cursor', 'set_config')
)
"""


async def test_no_function_that_reaches_another_corner_is_left_to_the_programs(
    tools_admin, dana_login, database_url
):
    url = re.sub(r"^postgresql\+asyncpg", "postgresql", database_url)
    connection = await asyncpg.connect(url, database=tools_admin.database)
    try:
        functions = [row[0] for row in await connection.fetch(_CORNER_REACHING)]
        callable_ = [
            name
            for name in functions
            if await connection.fetchval(
                "SELECT has_function_privilege($1, $2::regprocedure, 'EXECUTE')",
                dana_login.role_name,
                name,
            )
        ]
    finally:
        await connection.close()
    assert len(functions) >= 28
    assert {
        "query_to_xml(text,boolean,boolean,text)",
        "ts_stat(text)",
        "nextval(regclass)",
        "setval(regclass,bigint)",
        "pg_sequence_last_value(regclass)",
        "pg_relation_size(regclass)",
    } <= set(functions)
    assert callable_ == []


@pytest.mark.parametrize(
    "call",
    [
        "SELECT query_to_xml('SELECT 1', true, false, '')",
        "SELECT database_to_xml(true, false, '')",
        "SELECT schema_to_xml('common', true, false, '')",
        "SELECT ts_stat('SELECT to_tsvector(''a'')')",
        "SELECT ts_rewrite('a'::tsquery, 'SELECT ''a''::tsquery, ''b''::tsquery')",
        "SELECT cursor_to_xml('c', 1, true, false, '')",
        "SELECT nextval({sequence}::regclass)",
        "SELECT setval({sequence}::regclass, 1)",
        "SELECT pg_sequence_last_value({sequence}::regclass)",
        "SELECT lastval()",
        "SELECT pg_relation_size({secret}::regclass)",
        "SELECT pg_table_size({secret}::regclass)",
        "SELECT pg_total_relation_size({secret}::regclass)",
        "SELECT pg_indexes_size({secret}::regclass)",
        "SELECT pg_stat_have_stats('relation', 0, {secret}::oid)",
        "SELECT brin_summarize_new_values({secret}::regclass)",
        "SELECT brin_summarize_range({secret}::regclass, 0)",
        "SELECT brin_desummarize_range({secret}::regclass, 0)",
        "SELECT gin_clean_pending_list({secret}::regclass)",
        "SELECT current_query()",
        "SELECT name FROM pg_prepared_statements",
        "SELECT name FROM pg_cursors",
        "SELECT set_config('work_mem', '1GB', true)",
    ],
)
async def test_each_function_that_reaches_another_corner_is_refused(
    tools_base, dana_login, paul_login, pauls_corner, call
):
    answer = await write(tools_base, dana_login, _planted(call, paul_login, pauls_corner))
    assert isinstance(answer, BaseRefused) and answer.detail is not None
    assert answer.detail.startswith("42501: ")  # insufficient privilege


async def test_the_statistics_of_the_tables_are_not_shown(tools_base, dana_login, pauls_corner):
    for query in (
        "SELECT n_live_tup FROM pg_stat_user_tables",
        "SELECT * FROM pg_statio_user_tables",
        "SELECT * FROM pg_stat_all_tables",
        "SELECT pg_stat_get_live_tuples(c.oid) FROM pg_class c WHERE relname = 'secret'",
    ):
        assert refused(await read(tools_base, dana_login, query)), query


async def test_a_request_whose_rows_come_slowly_ends_within_its_bound(tools_base, dana_login):
    # Each row is under the statement's seconds; together they are not: the whole request is
    # bounded, the reading of its rows one at a time included, by the core's own deadline (the
    # server's ``transaction_timeout`` comes after it, fix round 1 I1).
    started = time.monotonic()
    slow = await read(
        tools_base,
        dana_login,
        "SELECT pg_sleep(2), n FROM generate_series(1, 4) AS n",
        bounds=bounded(statement_seconds=10, request_seconds=6),
    )
    assert slow == BaseRefused("sql_error", "the request took more than 6 s", "request_seconds")
    assert time.monotonic() - started < 9
    # The next request of the same role finds a clean connection.
    assert await read(tools_base, dana_login, "SELECT 1 AS n") == BaseRows([{"n": 1}])
    assert await read(tools_base, dana_login, "SELECT 2 AS n") == BaseRows([{"n": 2}])


async def test_the_core_ends_a_request_the_server_would_not(
    tools_base, tools_admin, dana_login, paul_login, database_url, monkeypatch
):
    # A program cannot lift its own bounds while its statement runs: ``set_config`` is not its.
    for lift in ("statement_timeout", "transaction_timeout", "client_connection_check_interval"):
        lifted = await read(tools_base, dana_login, f"SELECT set_config('{lift}', '0', true)")
        assert isinstance(lifted, BaseRefused) and (lifted.detail or "").startswith("42501: ")
        through_view = await write(
            tools_base, dana_login, f"UPDATE pg_settings SET setting = '0' WHERE name = '{lift}'"
        )
        assert refused(through_view)
    # The core's own deadline, made short here, comes first: a statement that holds a common
    # table locked for everyone (``ALTER TABLE`` takes it whole) and sleeps.
    assert await write(tools_base, dana_login, "CREATE TABLE t (n int)", space="common") == (
        BaseRows([])
    )
    assert await write(tools_base, dana_login, "INSERT INTO t VALUES (1)", space="common") == (
        BaseRows([])
    )
    ended: list[str] = []
    real_execute = tools_admin.execute

    async def watched(statement: str) -> None:
        ended.append(statement)
        await real_execute(statement)

    monkeypatch.setattr(tools_admin, "execute", watched)
    started = time.monotonic()
    answer = await write(
        tools_base,
        dana_login,
        "ALTER TABLE t ADD CONSTRAINT slow CHECK (pg_sleep(60) IS NOT NULL)",
        space="common",
        bounds=bounded(request_seconds=2),
    )
    assert answer == BaseRefused("sql_error", "the request took more than 2 s", "request_seconds")
    assert time.monotonic() - started < 5
    # The core ended the server's process itself, by its own hand.
    assert any("pg_terminate_backend" in statement for statement in ended)
    # The process is gone at once, and its lock with it: another role reads the table.
    working = "SELECT count(*) FROM pg_stat_activity WHERE usename = $1 AND state <> 'idle'"
    for _ in range(10):
        if await admin_value(tools_admin, database_url, working, dana_login.role_name) == 0:
            break
        await asyncio.sleep(0.1)
    assert await admin_value(tools_admin, database_url, working, dana_login.role_name) == 0
    other = await read(tools_base, paul_login, "SELECT n FROM t", space="common")
    assert other == BaseRows([{"n": 1}])
    assert time.monotonic() - started < 6
    # And the next requests of the same role find a clean connection.
    assert await read(tools_base, dana_login, "SELECT 1 AS n") == BaseRows([{"n": 1}])
    assert await read(tools_base, dana_login, "SELECT 2 AS n") == BaseRows([{"n": 2}])


class _CommitAnswersNot:
    """A connection whose COMMIT reaches the server, but whose answer is lost (``lost``) or never
    comes before the core's deadline (``slow``)."""

    def __init__(self, connection, how: str) -> None:
        self._connection = connection
        self._how = how

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def transaction(self, **options):
        real = self._connection.transaction(**options)
        how = self._how

        class Transaction:
            def __getattr__(self, name):
                return getattr(real, name)

            async def commit(self):
                await real.commit()
                if how == "lost":
                    raise asyncpg.ConnectionDoesNotExistError("connection was closed")
                await asyncio.sleep(60)

        return Transaction()


@pytest.mark.parametrize("how", ["lost", "slow"])
async def test_a_write_whose_commit_goes_unanswered_is_not_said_refused(
    tools_base, dana_login, monkeypatch, how
):
    assert await write(tools_base, dana_login, "CREATE TABLE x (n int)") == BaseRows([])
    real_run = ToolsBase._run

    async def unanswered(self, connection, progress, **arguments):
        return await real_run(self, _CommitAnswersNot(connection, how), progress, **arguments)

    monkeypatch.setattr(ToolsBase, "_run", unanswered)
    answer = await write(
        tools_base, dana_login, "INSERT INTO x VALUES (1)", bounds=bounded(request_seconds=2)
    )
    assert isinstance(answer, BaseRefused) and answer.reason == "outcome_unknown"
    assert answer.detail is not None and "may or may not" in answer.detail
    monkeypatch.setattr(ToolsBase, "_run", real_run)
    # It was in fact kept: the program had to read before writing again.
    assert await read(tools_base, dana_login, "SELECT n FROM x") == BaseRows([{"n": 1}])


async def test_a_corner_never_committed_is_dropped_by_the_upkeep(
    session, session_factory, tools_admin, add_person, database_url
):
    tom = await add_person("Tom", "adult")
    lost = await space_for(session, tools_admin, tom.id)
    await session.rollback()  # the caller never commits: no row is left
    async with session_factory() as writer:
        sam = Person(display_name="Sam", role="adult")
        writer.add(sam)
        await writer.flush()
        kept = await space_for(writer, tools_admin, sam.id)
        await writer.commit()

    roles = "SELECT count(*) FROM pg_roles WHERE rolname = $1"
    schemas = "SELECT count(*) FROM pg_namespace WHERE nspname = $1"
    async with session_factory() as reader:
        # Within the grace, a row may still be on its way: nothing is dropped.
        assert await drop_orphans(reader, tools_admin, datetime.now(UTC)) == 0
        assert await admin_value(tools_admin, database_url, roles, lost.role_name) == 1
        later = datetime.now(UTC) + ORPHAN_GRACE + timedelta(minutes=1)
        assert await drop_orphans(reader, tools_admin, later) == 2  # its role and its schema
    assert await admin_value(tools_admin, database_url, roles, lost.role_name) == 0
    assert await admin_value(tools_admin, database_url, schemas, lost.corner) == 0
    assert await admin_value(tools_admin, database_url, roles, kept.role_name) == 1
    assert await admin_value(tools_admin, database_url, schemas, kept.corner) == 1


class _FailingFor(ToolsAdmin):
    """The core's hand on the tools' database, failing every statement that names ``role``."""

    def __init__(self, admin: ToolsAdmin, dsn: str, role: str) -> None:
        super().__init__(dsn, admin.database)
        self._role = role

    async def execute(self, statement: str) -> None:
        if self._role in statement:
            raise OSError("the server went away")
        await super().execute(statement)


async def test_one_corner_that_cannot_be_dropped_holds_back_no_other(
    session, tools_admin, add_person, database_url
):
    tom, sam = await add_person("Tom", "adult"), await add_person("Sam", "adult")
    stuck = await space_for(session, tools_admin, tom.id)
    other = await space_for(session, tools_admin, sam.id)
    await session.delete(tom)
    await session.delete(sam)
    await session.flush()
    dsn = re.sub(r"^postgresql\+asyncpg", "postgresql", database_url)
    failing = _FailingFor(tools_admin, dsn, stuck.role_name)
    assert await drop_corners_of_the_gone(session, failing) == 1
    roles = "SELECT count(*) FROM pg_roles WHERE rolname = $1"
    assert await admin_value(tools_admin, database_url, roles, other.role_name) == 0
    assert await admin_value(tools_admin, database_url, roles, stuck.role_name) == 1
    left = (await session.scalars(select(ToolsSpace.role_name))).all()
    assert left == [stuck.role_name]  # tried again at the next pass
    assert await drop_corners_of_the_gone(session, tools_admin) == 1
