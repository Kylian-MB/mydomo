"""Every bound of a launch and of a chain is a setting of Domo's (spec 12.1, the owner,
2026-11-21): the core reads it where it counts, the launch's tool's own value first, then the
house's, then its start (``madetools.settings``).

Raising a setting lets through what its start refused; lowering it refuses earlier; a tool's own
value wins over the house's; each refusal a setting causes carries its key and its figure
(``settings.hit``), with words that say it is Domo's to change. Whatever the settings, the socle
holds: a spending limit, a right, a stop that waits for its yes, a dry run that cannot keep
anything. The programs are coroutines given to a ``FakeSandbox``."""

import asyncio
import contextlib
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from domo_common.sandbox_api import JsonObject, Machine, RunRequest, RunResult
from domo_core.accounts.models import Person
from domo_core.audit.models import AuditEntry
from domo_core.madetools import means as means_module
from domo_core.madetools.launch import HOUSE, LAUNCHED, ForWhom, Launched, launch
from domo_core.madetools.means import Chain, LaunchScope, Means
from domo_core.madetools.models import MadeTool, MadeToolTrigger
from domo_core.madetools.risks import read_operations
from domo_core.madetools.settings import SETTINGS, set_for_tool, set_house
from domo_core.policy.roles import domo_actor
from fakes import FakeAsk, FakeRefused, FakeSandbox
from pydantic import JsonValue
from sqlalchemy import func, select
from test_madetools_handover import BEGIN, OPERATIONS, add_tool, by_program, deps_of, ran
from test_madetools_yes import ORDER, Shop, stops

# Every setting at a figure no use reaches, the time of a launch too: the programs of these tests
# are bounded by themselves (they never wait on their time).
HUGE = 10**9
ALL_HUGE: dict[str, int | None] = dict.fromkeys(SETTINGS, HUGE)


@pytest.fixture
async def alex(add_member) -> Person:
    return await add_member("Alex", "owner")


@pytest.fixture
async def dana(add_member, alex) -> Person:
    return await add_member("Dana", "adult")


async def house(session_factory, **values: int | None) -> None:
    async with session_factory() as writer:
        await set_house(writer, values)
        await writer.commit()


async def own(session_factory, tool: MadeTool, **values: int | None) -> None:
    async with session_factory() as writer:
        row = await writer.get(MadeTool, tool.id)
        assert row is not None
        await set_for_tool(writer, row, values)
        await writer.commit()


async def launched(deps, tool: MadeTool, person: Person | None) -> Launched:
    for_whom = HOUSE if person is None else ForWhom(person)
    return await launch(deps, tool.id, arguments={}, for_whom=for_whom, cause="trigger")


def said(refused: FakeRefused) -> list[Any]:
    """What a program met: the reason, and the setting with its figure."""
    return [refused.reason, refused.setting, refused.value]


def asking_each(*names: str):
    """A program that asks each tool of ``names`` in turn, and gives back what each answer was."""

    async def program(request: RunRequest, ask: FakeAsk) -> Any:
        answers: list[Any] = []
        await ask(BEGIN)
        for name in names:
            try:
                answers.append(await ask({"do": "tool", "name": name, "arguments": {}}))
            except FakeRefused as refused:
                answers.append(said(refused))
        return answers

    return program


def asking_next(after: str | None):
    """A program that asks the tool ``after`` (none: it gives back ``"end"``), and gives back its
    answer, or what it met."""

    async def program(request: RunRequest, ask: FakeAsk) -> Any:
        if after is None:
            return "end"
        await ask(BEGIN)
        try:
            return await ask({"do": "tool", "name": after, "arguments": {}})
        except FakeRefused as refused:
            return said(refused)

    return program


# --- a chain -------------------------------------------------------------------------------------


async def test_chain_launches_is_read_from_the_settings(session_factory, live_clock, dana):
    names = [f"c{n}" for n in range(12)]
    first, *others = [await add_tool(session_factory, name) for name in names]
    sandbox = by_program(c0=asking_each(*names[1:]))
    deps = deps_of(session_factory, live_clock, sandbox, [])
    # The start: ten launches in all, the first included.
    came = await launched(deps, first, dana)
    assert came.result == [None] * 9 + [["too_many_launches", "chain_launches", 10]] * 2
    # Raised for the house: a chain of twelve launches.
    await house(session_factory, chain_launches=20)
    sandbox.runs.clear()
    assert (await launched(deps, first, dana)).result == [None] * 11
    assert len(sandbox.runs) == 12
    # The asking tool's own value wins over the house's, and refuses earlier.
    await own(session_factory, first, chain_launches=3)
    came = await launched(deps, first, dana)
    assert came.result == [None, None] + [["too_many_launches", "chain_launches", 3]] * 9


async def test_chain_depth_is_read_from_the_settings(session_factory, live_clock, dana):
    names = [f"d{n}" for n in range(8)]
    tools = [await add_tool(session_factory, name) for name in names]
    programs = {
        name: asking_next(names[place + 1] if place + 1 < len(names) else None)
        for place, name in enumerate(names)
    }
    sandbox = by_program(**programs)
    deps = deps_of(session_factory, live_clock, sandbox, [])
    # The start: five deep, the first at depth 0; the fifth may launch no other.
    assert (await launched(deps, tools[0], dana)).result == ["too_deep", "chain_depth", 5]
    assert ran(sandbox) == names[:5]
    # Raised: the eighth tool runs at depth 7.
    await house(session_factory, chain_depth=8)
    sandbox.runs.clear()
    assert (await launched(deps, tools[0], dana)).result == "end"
    assert ran(sandbox) == names
    # Lowered for the house: two deep.
    await house(session_factory, chain_depth=2)
    sandbox.runs.clear()
    assert (await launched(deps, tools[0], dana)).result == ["too_deep", "chain_depth", 2]
    assert ran(sandbox) == names[:2]


async def test_a_nested_launch_with_no_free_place_is_refused_busy_with_the_places(
    session_factory, live_clock, dana
):
    # Review Focus 3: never queued, whatever the settings say.
    await house(session_factory, chain_depth=HUGE, chain_launches=HUGE, places=1)
    first = await add_tool(session_factory, "first")
    await add_tool(session_factory, "second")
    sandbox = by_program(first=asking_each("second"))
    # Every place taken: the first launch waits for one, a nested one may not.
    sandbox.busy = True
    deps = deps_of(session_factory, live_clock, sandbox, [])
    assert (await launched(deps, first, dana)).result == [["busy", "places", 1]]
    assert ran(sandbox) == ["first"]


# --- what the sandbox is given, and what its machine cannot give ---------------------------------


async def test_the_bounds_of_a_run_are_sent_to_the_sandbox_from_the_settings(
    session_factory, live_clock, dana
):
    tool = await add_tool(session_factory, "long")
    sandbox = FakeSandbox()
    deps = deps_of(session_factory, live_clock, sandbox, [])
    assert (await launched(deps, tool, dana)).status == "done"
    # More than an hour is no longer refused by the core: the sandbox takes it.
    await house(session_factory, run_seconds=5000, memory_mb=4096, output_bytes=2_000_000)
    await own(session_factory, tool, memory_mb=8192)
    assert (await launched(deps, tool, dana)).status == "done"
    sent = [(run.max_seconds, run.memory_mb, run.output_bytes) for run in sandbox.runs]
    assert sent == [(60, 256, 65536), (5000, 8192, 2_000_000)]


async def test_memory_the_sandbox_has_not_fails_with_the_setting_and_what_it_has(
    session_factory, live_clock, dana
):
    tool = await add_tool(session_factory, "greedy")
    sandbox = FakeSandbox()
    sandbox.has = Machine(memory_mb=1024, processors=2, places=4)
    deps = deps_of(session_factory, live_clock, sandbox, [])
    await own(session_factory, tool, memory_mb=4096, failures_in_a_row=2)
    came = await launched(deps, tool, dana)
    assert came.status == "failed" and isinstance(came.result, dict)
    assert (came.result["setting"], came.result["value"]) == ("memory_mb", 4096)
    assert came.result["has"] == {"memory_mb": 1024, "processors": 2, "places": 4}
    assert "1024" in str(came.result["reason"]) and "memory_mb is 4096" in str(
        came.result["reason"]
    )
    # Nothing ran: its count of the day is given back, but it fails at every launch while its
    # settings ask more than the machine has, so it counts as a failure (final review I3).
    assert sandbox.runs == []
    async with session_factory() as reader:
        row = await reader.get(MadeTool, tool.id)
        assert row is not None
        assert (row.state, row.failures_in_a_row, row.launches_today) == ("ready", 1, 0)
    # Within what it has, it runs.
    await own(session_factory, tool, memory_mb=1024)
    assert (await launched(deps, tool, dana)).status == "done"


@pytest.mark.parametrize(
    ("status", "key", "figure"), [("timeout", "run_seconds", 90), ("too_big", "output_bytes", 5000)]
)
async def test_a_run_past_its_time_or_its_output_says_the_setting(
    session_factory, live_clock, dana, status, key, figure
):
    # Fix round 1, G2: the sandbox stops a run on the launch's own figures; Domo reads which.
    def stopped(request: RunRequest) -> RunResult:
        return RunResult(status=status, note="The tool ran out of time.", seconds=1.0)

    tool = await add_tool(session_factory, "bounded")
    await own(session_factory, tool, run_seconds=90, output_bytes=5000, failures_in_a_row=2)
    deps = deps_of(session_factory, live_clock, FakeSandbox(stopped), [])
    came = await launched(deps, tool, dana)
    assert came.status == "failed" and isinstance(came.result, dict)
    assert (came.result["setting"], came.result["value"]) == (key, figure)
    assert f"{key} is {figure}, a setting of yours" in str(came.result["reason"])
    # The second sets it aside (its failures in a row), its reason saying the setting too.
    came = await launched(deps, tool, dana)
    assert came.status == "set_aside" and isinstance(came.result, dict)
    assert (came.result["setting"], came.result["value"]) == ("failures_in_a_row", 2)
    assert f"{key} is {figure}" in str(came.result["reason"])
    async with session_factory() as reader:
        row = await reader.get(MadeTool, tool.id)
        assert row is not None and f"{key} is {figure}" in str(row.aside_reason)
        details = (
            await reader.scalars(select(AuditEntry.details).where(AuditEntry.action == LAUNCHED))
        ).all()
    # The fragment never goes in an audit line.
    assert details and all("setting" not in d and "value" not in d for d in details)


async def test_an_output_the_sandbox_cannot_hold_fails_with_its_setting(
    session_factory, live_clock, dana
):
    tool = await add_tool(session_factory, "wordy")
    sandbox = FakeSandbox()
    sandbox.has = Machine(memory_mb=1, processors=1, places=4)
    await own(session_factory, tool, memory_mb=1, output_bytes=2 * 1024 * 1024)
    came = await launched(deps_of(session_factory, live_clock, sandbox, []), tool, dana)
    assert came.status == "failed" and isinstance(came.result, dict)
    assert (came.result["setting"], came.result["value"]) == ("output_bytes", 2 * 1024 * 1024)


# --- one launch ----------------------------------------------------------------------------------


def many_requests(count: int):
    """A program that makes ``count`` requests (one begin, then requests that are not right,
    refused at once), then gives back ``"done"``."""

    async def program(request: RunRequest, ask: FakeAsk) -> Any:
        await ask(BEGIN)
        for _ in range(count - 1):
            with contextlib.suppress(FakeRefused):
                await ask({"do": "begin", "operation": "Not a name"})
        return "done"

    return program


async def test_requests_per_launch_is_read_from_the_settings(session_factory, live_clock, dana):
    tool = await add_tool(session_factory, "many")
    deps = deps_of(session_factory, live_clock, by_program(many=many_requests(1500)), [])
    came = await launched(deps, tool, dana)
    assert came.status == "failed"
    assert isinstance(came.result, dict)
    assert (came.result["setting"], came.result["value"]) == ("requests_per_launch", 1000)
    assert "requests_per_launch is 1000" in str(came.result["reason"])
    await house(session_factory, requests_per_launch=2000)
    assert await launched(deps, tool, dana) == Launched("done", "done")
    # The tool's own value wins.
    await own(session_factory, tool, requests_per_launch=10)
    came = await launched(deps, tool, dana)
    assert came.status == "failed"
    assert isinstance(came.result, dict)
    assert (came.result["setting"], came.result["value"]) == ("requests_per_launch", 10)


async def test_answer_seconds_is_read_from_the_settings(
    session_factory, live_clock, dana, monkeypatch
):
    async def slow(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(5)

    monkeypatch.setattr(means_module, "decide_now", slow)

    async def program(request: RunRequest, ask: FakeAsk) -> Any:
        try:
            await ask(BEGIN)
        except FakeRefused as refused:
            return said(refused)
        return None

    tool = await add_tool(session_factory, "slow")
    await own(session_factory, tool, answer_seconds=1)
    deps = deps_of(session_factory, live_clock, by_program(slow=program), [])
    async with asyncio.timeout(4):
        assert (await launched(deps, tool, dana)).result == ["too_slow", "answer_seconds", 1]


async def test_result_bytes_is_read_from_the_settings(session_factory, live_clock, dana):
    big = "x" * 100_000

    async def program(request: RunRequest, ask: FakeAsk) -> Any:
        return big

    tool = await add_tool(session_factory, "big")
    deps = deps_of(session_factory, live_clock, by_program(big=program), [])
    came = await launched(deps, tool, dana)
    assert came.status == "failed"
    assert isinstance(came.result, dict)
    assert (came.result["setting"], came.result["value"]) == ("result_bytes", 65536)
    await house(session_factory, result_bytes=200_000)
    assert await launched(deps, tool, dana) == Launched("done", big)
    await own(session_factory, tool, result_bytes=100)
    came = await launched(deps, tool, dana)
    assert isinstance(came.result, dict)
    assert (came.result["setting"], came.result["value"]) == ("result_bytes", 100)


async def test_failures_in_a_row_is_read_from_the_settings(session_factory, live_clock, dana):
    def fails(request: RunRequest) -> RunResult:
        return RunResult(status="error", error="ValueError: no", seconds=0.1)

    tool = await add_tool(session_factory, "fails")
    deps = deps_of(session_factory, live_clock, FakeSandbox(fails), [])
    await own(session_factory, tool, failures_in_a_row=1)
    came = await launched(deps, tool, dana)
    assert came.status == "set_aside"
    assert isinstance(came.result, dict)
    assert (came.result["setting"], came.result["value"]) == ("failures_in_a_row", 1)
    async with session_factory() as reader:
        row = await reader.get(MadeTool, tool.id)
        assert row is not None and row.state == "set_aside"
        assert "failures_in_a_row is 1" in str(row.aside_reason)


async def test_launches_per_day_is_read_from_the_settings(session_factory, live_clock, dana):
    tool = await add_tool(session_factory, "daily")
    deps = deps_of(session_factory, live_clock, FakeSandbox(), [])
    await house(session_factory, launches_per_day=5)
    await own(session_factory, tool, launches_per_day=2)
    assert [(await launched(deps, tool, dana)).status for _ in range(2)] == ["done", "done"]
    came = await launched(deps, tool, dana)
    assert came.status == "set_aside"
    assert isinstance(came.result, dict)
    assert (came.result["setting"], came.result["value"]) == ("launches_per_day", 2)


async def test_wakes_per_day_is_read_from_the_settings(session_factory, live_clock, dana):
    async def wakes_twice(request: RunRequest, ask: FakeAsk) -> Any:
        await ask(BEGIN)
        await ask({"do": "wake", "content": 1})
        try:
            await ask({"do": "wake", "content": 2})
        except FakeRefused as refused:
            return said(refused)
        return None

    tool = await add_tool(session_factory, "waker")
    await own(session_factory, tool, wakes_per_day=1)
    deps = deps_of(session_factory, live_clock, by_program(waker=wakes_twice), [])
    assert (await launched(deps, tool, dana)).result == ["too_many_wakes", "wakes_per_day", 1]


async def test_triggers_per_tool_is_read_from_the_settings(session_factory, live_clock, dana):
    async def twelve(request: RunRequest, ask: FakeAsk) -> Any:
        await ask(BEGIN)
        met: list[Any] = []
        for hours in range(1, 13):
            at = (live_clock() + timedelta(hours=hours)).isoformat()
            try:
                met.append(await ask({"do": "later", "at": at, "arguments": {}}))
            except FakeRefused as refused:
                met.append(said(refused))
        return met

    tool = await add_tool(session_factory, "again")
    deps = deps_of(session_factory, live_clock, by_program(again=twelve), [])
    came = await launched(deps, tool, dana)
    assert came.result == [None] * 10 + [["not_possible", "triggers_per_tool", 10]] * 2
    await own(session_factory, tool, triggers_per_tool=30)
    assert (await launched(deps, tool, dana)).result == [None] * 12
    async with session_factory() as reader:
        waiting = await reader.scalar(select(func.count()).select_from(MadeToolTrigger))
    assert waiting == 22


# --- the tools' base -----------------------------------------------------------------------------


async def test_rows_and_the_statement_time_are_read_from_the_settings(
    session_factory, live_clock, tools_base, dana
):
    async def reads(request: RunRequest, ask: FakeAsk) -> Any:
        met: list[Any] = []
        for sql in ("SELECT generate_series(1, 1500) AS n", "SELECT pg_sleep(1.5)"):
            try:
                rows = await ask({"do": "read", "sql": sql, "params": [], "space": "common"})
                met.append(len(rows) if isinstance(rows, list) else rows)
            except FakeRefused as refused:
                met.append(said(refused))
        return met

    tool = await add_tool(session_factory, "reads")
    deps = deps_of(session_factory, live_clock, by_program(reads=reads), [], tools_base=tools_base)
    await own(session_factory, tool, statement_seconds=1)
    assert (await launched(deps, tool, dana)).result == [
        ["too_big", "rows", 1000],
        ["sql_error", "statement_seconds", 1],
    ]
    await own(session_factory, tool, rows=2000, statement_seconds=3)
    assert (await launched(deps, tool, dana)).result == [1500, 1]


# --- the socle holds, whatever the settings ------------------------------------------------------


async def test_with_every_setting_huge_a_spending_limit_still_stops_a_begin(
    session_factory, live_clock, dana, grant
):
    await house(session_factory, **ALL_HUGE)
    tool = await add_tool(session_factory, "order", ORDER)
    await grant(
        "program.begin",
        person=dana,
        limit='action.facts.amount_cents != null && action.facts.amount_cents > 100 ? "refuse" '
        ': "allow"',
    )
    shop = Shop()

    async def program(request: RunRequest, ask: FakeAsk) -> JsonValue:
        try:
            return await shop(request, ask)
        except FakeRefused as refused:
            return refused.reason

    deps = deps_of(session_factory, live_clock, by_program(order=program), [])
    assert (await launched(deps, tool, dana)).result == "limit"
    assert shop.ordered == 0


async def test_with_every_setting_huge_a_right_still_refuses(
    session_factory, live_clock, dana, grant
):
    await house(session_factory, **ALL_HUGE)

    async def writes(request: RunRequest, ask: FakeAsk) -> Any:
        await ask(BEGIN)
        try:
            await ask(
                {"do": "write", "sql": "CREATE TABLE t (n int)", "params": [], "space": "mine"}
            )
        except FakeRefused as refused:
            return refused.reason
        return None

    tool = await add_tool(session_factory, "writes")
    await grant("program.base.write", person=dana, access="deny")
    deps = deps_of(session_factory, live_clock, by_program(writes=writes), [])
    assert (await launched(deps, tool, dana)).result == "not_allowed"


async def test_with_every_setting_huge_a_stop_still_waits_for_its_yes(
    session_factory, live_clock, dana
):
    await house(session_factory, **ALL_HUGE)
    tool = await add_tool(session_factory, "order", ORDER)
    shop = Shop()
    deps = deps_of(session_factory, live_clock, by_program(order=shop), [])
    came = await launched(deps, tool, dana)
    assert came.status == "stopped" and shop.ordered == 0
    assert len(await stops(session_factory)) == 1


async def test_with_every_setting_huge_a_dry_run_still_keeps_nothing(
    session_factory, live_clock, tools_base, alex
):
    await house(session_factory, **ALL_HUGE)
    huge = {key: HUGE for key in SETTINGS}
    scope = LaunchScope(
        tool_name=None,
        tool_id=None,
        for_whom=HOUSE,
        actor=domo_actor(),
        depth=0,
        chain=Chain(),
        waited=False,
        path=(),
        dry_run=True,
        operations=read_operations(OPERATIONS),
        settings=huge,
    )
    deps = deps_of(session_factory, live_clock, by_program(), [], tools_base=tools_base)
    means = Means(deps, scope)

    def write(sql: str) -> JsonObject:
        return {"do": "write", "sql": sql, "params": [], "space": "common"}

    assert await means.answer(BEGIN) == {"ok": None}
    assert await means.answer(write("CREATE TABLE kept_by_nobody (n int)")) == {"ok": []}
    committed = await means.answer(write("COMMIT"))
    assert committed is not None and committed["refused"] == "sql_error"
    await means.end()
    # Another dry run finds nothing of it.
    again = Means(deps, replace(scope, chain=Chain()))
    try:
        found = await again.answer(
            {
                "do": "read",
                "sql": "SELECT to_regclass('common.kept_by_nobody')::text AS t",
                "params": [],
                "space": "common",
            }
        )
    finally:
        await again.end()
    assert found == {"ok": [{"t": None}]}


# --- bounds that were fixed in the code, now his settings (final review, ruling R33) -------------


async def test_a_wake_s_weight_is_read_from_the_settings(session_factory, live_clock, dana):
    async def wakes_heavy(request: RunRequest, ask: FakeAsk) -> Any:
        await ask(BEGIN)
        try:
            await ask({"do": "wake", "content": "x" * 5000})
        except FakeRefused as refused:
            return said(refused)
        return None

    tool = await add_tool(session_factory, "heavy")
    deps = deps_of(session_factory, live_clock, by_program(heavy=wakes_heavy), [])
    assert (await launched(deps, tool, dana)).result == ["too_big", "wake_bytes", 4096]
    await own(session_factory, tool, wake_bytes=8000)
    assert (await launched(deps, tool, dana)).result is None


async def test_how_far_ahead_a_later_goes_is_read_from_the_settings(
    session_factory, live_clock, dana
):
    async def later_by(days: int):
        at = (live_clock() + timedelta(days=days)).isoformat()

        async def program(request: RunRequest, ask: FakeAsk) -> Any:
            await ask(BEGIN)
            try:
                return await ask({"do": "later", "at": at, "arguments": {}})
            except FakeRefused as refused:
                return said(refused)

        return program

    tool = await add_tool(session_factory, "again")
    deps = deps_of(session_factory, live_clock, by_program(again=await later_by(400)), [])
    assert (await launched(deps, tool, dana)).result == ["too_far", "later_days", 365]
    await own(session_factory, tool, later_days=500)
    assert (await launched(deps, tool, dana)).result is None
    # No figure is too big for the arithmetic of a date.
    await own(session_factory, tool, later_days=10**30)
    deps = deps_of(session_factory, live_clock, by_program(again=await later_by(9000)), [])
    assert (await launched(deps, tool, dana)).result is None


async def test_the_arguments_of_a_later_are_weighed_by_the_settings(
    session_factory, live_clock, dana
):
    long = {"n": 1, "note": "x" * 5000}

    async def program(request: RunRequest, ask: FakeAsk) -> Any:
        await ask(BEGIN)
        at = (live_clock() + timedelta(hours=1)).isoformat()
        try:
            return await ask({"do": "later", "at": at, "arguments": long})
        except FakeRefused as refused:
            return said(refused)

    tool = await add_tool(session_factory, "again")
    deps = deps_of(session_factory, live_clock, by_program(again=program), [])
    assert (await launched(deps, tool, dana)).result == [
        "too_long",
        "trigger_argument_chars",
        4000,
    ]
    await own(session_factory, tool, trigger_argument_chars=10_000)
    assert (await launched(deps, tool, dana)).result is None


async def test_what_a_program_reads_of_a_tool_it_launched_follows_result_bytes(
    session_factory, live_clock, dana
):
    big = "x" * 100_000

    async def gives_big(request: RunRequest, ask: FakeAsk) -> Any:
        return big

    async def reads_it(request: RunRequest, ask: FakeAsk) -> Any:
        await ask(BEGIN)
        try:
            got = await ask({"do": "tool", "name": "big", "arguments": {}})
        except FakeRefused as refused:
            return [*said(refused), refused.detail]
        return len(str(got))

    asker = await add_tool(session_factory, "asker")
    given = await add_tool(session_factory, "big")
    await own(session_factory, given, result_bytes=200_000)
    sandbox = by_program(asker=reads_it, big=gives_big)
    deps = deps_of(session_factory, live_clock, sandbox, [])
    # The tool it launched ran; what it gave is over the asking launch's result_bytes: never a
    # bare too_big, the setting is named and the detail says the tool ran.
    met = (await launched(deps, asker, dana)).result
    assert isinstance(met, list) and met[:3] == ["too_big", "result_bytes", 65536]
    assert "the tool ran" in str(met[3])
    await own(session_factory, asker, result_bytes=300_000)
    assert (await launched(deps, asker, dana)).result == len(big)
    # The run is told the frame of its answers: its result_bytes.
    assert {request.answer_bytes for request in sandbox.runs} >= {65536, 300_000}


async def test_a_tool_s_result_is_held_to_the_code_s_ceiling_whatever_result_bytes(
    session_factory, live_clock, dana
):
    # Re-review, ruling R37: an answer over 8 MB is refused by the ceiling, told apart from
    # result_bytes; under it, it passes.
    sizes = {"big": 8 * 1024 * 1024 + 1000}

    async def gives(request: RunRequest, ask: FakeAsk) -> Any:
        return "x" * sizes["big"]

    async def reads_it(request: RunRequest, ask: FakeAsk) -> Any:
        await ask(BEGIN)
        try:
            got = await ask({"do": "tool", "name": "big", "arguments": {}})
        except FakeRefused as refused:
            return [*said(refused), refused.detail]
        return len(str(got))

    asker = await add_tool(session_factory, "asker")
    given = await add_tool(session_factory, "big")
    for tool in (asker, given):
        await own(session_factory, tool, result_bytes=2**40)
    sandbox = by_program(asker=reads_it, big=gives)
    deps = deps_of(session_factory, live_clock, sandbox, [])
    met = (await launched(deps, asker, dana)).result
    assert isinstance(met, list) and met[:3] == ["too_big", None, None]
    assert "ceiling" in str(met[3]) and "result_bytes" not in str(met[3])
    # The sandbox is never told to hold more than the ceiling either.
    assert max(request.answer_bytes for request in sandbox.runs) == 8 * 1024 * 1024
    sizes["big"] = 1_000_000
    assert (await launched(deps, asker, dana)).result == 1_000_000


async def test_an_act_carried_out_over_a_small_result_bytes_is_told_it_was_done(
    session_factory, live_clock, dana
):
    # Re-review point 3: the program is never left without knowing that what it asked was done.
    met: list[Any] = []

    async def begins(request: RunRequest, ask: FakeAsk) -> Any:
        try:
            met.append(await ask(BEGIN))
        except FakeRefused as refused:
            met.extend([refused.reason, refused.detail])
        return None  # "null": within its result_bytes of 5

    tool = await add_tool(session_factory, "tiny")
    await own(session_factory, tool, result_bytes=5)
    deps = deps_of(session_factory, live_clock, by_program(tiny=begins), [])
    assert (await launched(deps, tool, dana)).status == "done"
    assert met[0] == "too_big" and str(met[1]).startswith("done:")
