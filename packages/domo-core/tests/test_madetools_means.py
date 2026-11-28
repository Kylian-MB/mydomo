"""A program asks the core while it runs (Task 7, spec 5.2, 5.8; the tools are Domo's,
2026-11-18).

Each request a program writes on its channel is read (``asks.read_ask``), judged at once by the
barrier with the rights of whom the launch is for (Domo's own for the house), and done or
refused (``means.Means``): its answer is what the program reads before it goes on. The programs are
coroutines given to a ``FakeSandbox`` (each tool's code starts with its name, so that the fake
knows which one runs); the tools' base is the test's own (``tools_base``)."""

import asyncio
import contextlib
import json
import logging
from datetime import timedelta
from typing import Any

import pytest
from domo_common.sandbox_api import JsonObject, RunRequest
from domo_core.accounts.actors import actor_of
from domo_core.accounts.models import Person
from domo_core.madetools import means as means_module
from domo_core.madetools.asks import (
    AskNotRight,
    BeginAsk,
    ReadAsk,
    ToolAsk,
    WakeAsk,
    read_ask,
)
from domo_core.madetools.launch import (
    HOUSE,
    LAUNCHED,
    SANDBOX_CUT,
    SANDBOX_DOWN,
    ForWhom,
    Launched,
    launch,
)
from domo_core.madetools.means import (
    TOOL_REFUSED,
    Chain,
    LaunchScope,
    Means,
)
from domo_core.madetools.models import MadeTool, MadeToolTrigger
from domo_core.madetools.risks import read_operations
from domo_core.madetools.settings import SETTINGS, set_for_tool
from domo_core.prompts import ToolWake
from domo_core.sandbox_client import SandboxUnavailable
from domo_core.toolsdb.spaces import ToolsSpace
from fakes import FakeAsk, FakeRefused
from sqlalchemy import select
from test_madetools_handover import (
    BEGIN,
    HOSTILE,
    OPERATIONS,
    Woken,
    add_tool,
    all_audit,
    audit_of,
    by_program,
    deps_of,
    ran,
)


@pytest.fixture
async def alex(add_member) -> Person:
    return await add_member("Alex", "owner")


@pytest.fixture
async def dana(add_member, alex) -> Person:
    return await add_member("Dana", "adult")


@pytest.fixture
async def paul(add_member, alex) -> Person:
    return await add_member("Paul", "child")


async def launched(deps, tool: MadeTool, person: Person | None) -> Launched:
    """``tool`` launched by a trigger for ``person`` (``None``: the house)."""
    for_whom = HOUSE if person is None else ForWhom(person)
    return await launch(deps, tool.id, arguments={}, for_whom=for_whom, cause="trigger")


def refusal_of(error: FakeRefused) -> JsonObject:
    return {"refused": error.reason}


async def made_decisions(session_factory) -> list[Any]:
    """The barrier's decisions on the launches of tools (``made.*``), in order."""
    decisions = await audit_of(session_factory, "policy.decision")
    return [e for e in decisions if str(e.details["action_type"]).startswith("made.")]


# --- reading a request ---------------------------------------------------------------------------


def test_each_request_is_read_and_what_is_not_one_is_refused():
    assert read_ask({"do": "begin", "operation": "order", "amount_cents": 3250}) == BeginAsk(
        "order", 3250
    )
    assert read_ask({"do": "read", "sql": "SELECT 1", "params": [], "space": "common"}) == ReadAsk(
        "SELECT 1", (), "common"
    )
    assert read_ask({"do": "tool", "name": "tally", "arguments": {"n": 1}}) == ToolAsk(
        "tally", {"n": 1}
    )
    assert read_ask({"do": "wake", "content": {"total": 3}}) == WakeAsk({"total": 3})
    # Its weight is the launch's wake_bytes, a setting (``Means``), not a rule of its form.
    assert read_ask({"do": "wake", "content": "x" * 4097}) == WakeAsk("x" * 4097)
    for wrong in (
        {"do": "fly"},
        {"do": "read", "sql": "", "params": [], "space": "common"},
        {"do": "read", "sql": "x" * 20_001, "params": [], "space": "common"},
        {"do": "read", "sql": "SELECT 1", "params": [1] * 101, "space": "common"},
        {"do": "read", "sql": "SELECT 1", "params": [], "space": "theirs"},
        {"do": "later", "at": "2026-11-18T08:00:00", "arguments": {}},
        {"do": "tool", "name": "Tally!", "arguments": {}},
        {"do": "begin", "operation": "order", "amount_cents": -1},
        # More than the columns of what is spent hold.
        {"do": "begin", "operation": "order", "amount_cents": 10_000_001},
        {"do": "begin", "operation": "order", "amount_cents": 5_000_000_000},
    ):
        with pytest.raises(AskNotRight) as refused:
            read_ask(wrong)
        assert "xxxx" not in str(refused.value)


async def test_a_wake_s_content_is_weighed_in_utf8_bytes(session_factory, live_clock, dana):
    # Against the launch's wake_bytes (4096 at the start), in UTF-8 bytes of compact JSON.
    async def program(request: RunRequest, ask: FakeAsk) -> Any:
        await ask(BEGIN)
        met: list[Any] = []
        for count in (4096 // 2 - 1, 4096 // 2):
            try:
                met.append(await ask({"do": "wake", "content": "é" * count}))
            except FakeRefused as refused:
                met.append(refused.reason)
        return met

    a = await add_tool(session_factory, "a")
    deps = deps_of(session_factory, live_clock, by_program(a=program), [])
    assert (await launched(deps, a, dana)).result == [None, "too_big"]


# --- the base ------------------------------------------------------------------------------------


async def test_a_program_writes_then_reads_back_in_its_persons_corner(
    session_factory, live_clock, tools_base, dana, paul
):
    async def notes(request: RunRequest, ask: FakeAsk) -> Any:
        await ask(BEGIN)
        await ask(
            {"do": "write", "sql": "CREATE TABLE notes (n int)", "params": [], "space": "mine"}
        )
        await ask(
            {"do": "write", "sql": "INSERT INTO notes VALUES ($1)", "params": [7], "space": "mine"}
        )
        return await ask(
            {"do": "read", "sql": "SELECT n FROM notes", "params": [], "space": "mine"}
        )

    async def peek(request: RunRequest, ask: FakeAsk) -> Any:
        try:
            return await ask(
                {"do": "read", "sql": "SELECT n FROM notes", "params": [], "space": "mine"}
            )
        except FakeRefused as refused:
            return refusal_of(refused)

    note = await add_tool(session_factory, "note")
    look = await add_tool(session_factory, "look")
    sandbox = by_program(note=notes, look=peek)
    deps = deps_of(session_factory, live_clock, sandbox, [], tools_base=tools_base)
    came = await launched(deps, note, dana)
    assert came == Launched("done", [{"n": 7}])
    # Dana's corner is hers: the same table is not there for Paul.
    assert (await launched(deps, look, dana)).result == [{"n": 7}]
    assert (await launched(deps, look, paul)).result == {"refused": "sql_error"}


async def test_a_house_launch_has_no_corner(session_factory, live_clock, tools_base, alex):
    async def mine(request: RunRequest, ask: FakeAsk) -> Any:
        try:
            return await ask({"do": "read", "sql": "SELECT 1", "params": [], "space": "mine"})
        except FakeRefused as refused:
            return refusal_of(refused)

    tool = await add_tool(session_factory, "house")
    deps = deps_of(session_factory, live_clock, by_program(house=mine), [], tools_base=tools_base)
    assert (await launched(deps, tool, None)).result == {"refused": "no_corner"}


async def test_an_owners_deny_on_writes_refuses_the_write_only(
    session_factory, live_clock, tools_base, dana, grant
):
    async def both(request: RunRequest, ask: FakeAsk) -> Any:
        said: JsonObject = {}
        await ask(BEGIN)
        try:
            await ask(
                {"do": "write", "sql": "CREATE TABLE t (n int)", "params": [], "space": "mine"}
            )
        except FakeRefused as refused:
            said["write"] = refused.reason
        said["read"] = await ask(
            {"do": "read", "sql": "SELECT 1 AS one", "params": [], "space": "common"}
        )
        return said

    tool = await add_tool(session_factory, "both")
    await grant("program.base.write", person=dana, access="deny")
    deps = deps_of(session_factory, live_clock, by_program(both=both), [], tools_base=tools_base)
    came = await launched(deps, tool, dana)
    assert came.result == {"write": "not_allowed", "read": [{"one": 1}]}
    decisions = await audit_of(session_factory, "policy.decision")
    write = [entry for entry in decisions if entry.details["action_type"] == "program.base.write"]
    assert [(entry.actor, entry.details["decision"]) for entry in write] == [
        (f"person:{dana.id}", "refuse")
    ]
    # A read is judged but not audited one by one: it is counted with the launch.
    assert not [entry for entry in decisions if entry.details["action_type"] == "program.base.read"]
    [entry] = await audit_of(session_factory, LAUNCHED)
    assert (entry.details["requests"], entry.details["reads"]) == (3, 1)


# --- bounds --------------------------------------------------------------------------------------


# The starts of the bounds this file pins (``madetools.settings``).
MAX_REQUESTS_PER_LAUNCH = SETTINGS["requests_per_launch"].start
MAX_CHAIN_LAUNCHES = SETTINGS["chain_launches"].start


async def test_the_1001st_request_ends_the_launch(session_factory, live_clock, dana):
    answered: list[str] = []

    async def loops(request: RunRequest, ask: FakeAsk) -> Any:
        # Two judged requests, then requests that are not right (refused at once, no database:
        # the test stays quick), each one counted all the same.
        await ask(BEGIN)
        await ask({"do": "wake", "content": "first"})
        for _ in range(MAX_REQUESTS_PER_LAUNCH - 1):
            try:
                await ask({"do": "begin", "operation": "Not a name"})
            except FakeRefused as refused:
                answered.append(refused.reason)
        return "never"

    tool = await add_tool(session_factory, "loops")
    sandbox = by_program(loops=loops)
    woken: Woken = []
    deps = deps_of(session_factory, live_clock, sandbox, woken)
    came = await launched(deps, tool, dana)
    assert came.status == "failed"
    assert came.result is not None and "requests" in str(came.result)
    assert len(sandbox.asks) == MAX_REQUESTS_PER_LAUNCH + 1 == 1001
    # The 1001st got no answer: the run was stopped there.
    assert answered == ["not_valid"] * (MAX_REQUESTS_PER_LAUNCH - 2)
    [entry] = await audit_of(session_factory, LAUNCHED)
    assert (entry.details["status"], entry.details["requests"]) == ("failed", 1001)
    # What its chain gathered before it was stopped is still given.
    assert woken == [(dana.id, [ToolWake("loops", "first")])]
    # A tool that loops is at fault: it counts as one of its failures.
    async with session_factory() as reader:
        assert (await reader.get(MadeTool, tool.id)).failures_in_a_row == 1  # type: ignore[union-attr]


# --- another tool --------------------------------------------------------------------------------


async def test_a_tool_asks_another_and_gets_its_result(session_factory, live_clock, dana):
    async def a(request: RunRequest, ask: FakeAsk) -> Any:
        await ask(BEGIN)
        return {"from_b": await ask({"do": "tool", "name": "b", "arguments": {"n": 2}})}

    async def b(request: RunRequest, ask: FakeAsk) -> Any:
        arguments = request.entry["arguments"]
        assert isinstance(arguments, dict)
        return {"twice": 2 * int(arguments["n"])}  # type: ignore[arg-type]

    first = await add_tool(session_factory, "a")
    await add_tool(session_factory, "b")
    sandbox = by_program(a=a, b=b)
    deps = deps_of(session_factory, live_clock, sandbox, [])
    came = await launched(deps, first, dana)
    assert came == Launched("done", {"from_b": {"twice": 4}})
    assert ran(sandbox) == ["a", "b"]
    # B ran for the same person, judged as made.b for her, and may not wait for a place.
    assert sandbox.runs[1].entry["for"] == "Dana"
    assert sandbox.runs[1].wait_for_a_place is False
    decisions = await made_decisions(session_factory)
    assert [(e.actor, e.details["action_type"]) for e in decisions] == [
        (f"person:{dana.id}", "made.a"),
        (f"person:{dana.id}", "made.b"),
    ]
    entries = await audit_of(session_factory, LAUNCHED)
    assert [(e.details["tool"], e.details["cause"]) for e in entries] == [
        ("b", "tool"),
        ("a", "trigger"),
    ]


async def test_a_nested_launch_without_a_free_place_is_refused_busy_not_waited(
    session_factory, live_clock, dana
):
    async def a(request: RunRequest, ask: FakeAsk) -> Any:
        await ask(BEGIN)
        try:
            return await ask({"do": "tool", "name": "b", "arguments": {}})
        except FakeRefused as refused:
            return refusal_of(refused)

    first = await add_tool(session_factory, "a")
    second = await add_tool(session_factory, "b")
    sandbox = by_program(a=a)
    sandbox.busy = True
    deps = deps_of(session_factory, live_clock, sandbox, [])
    came = await launched(deps, first, dana)
    # The first one may wait for a place; the one it asks may not, and is refused at once.
    assert came.result == {"refused": "busy"}
    assert ran(sandbox) == ["a"]
    async with session_factory() as reader:
        row = await reader.get(MadeTool, second.id)
    # Not run: its launch of the day is given back, and it is no failure of it.
    assert row is not None and (row.launches_today, row.failures_in_a_row) == (0, 0)


async def test_six_deep_stops_at_five_and_a_cycle_is_refused(session_factory, live_clock, dana):
    names = [f"t{n}" for n in range(6)]

    def asking(next_name: str):
        async def program(request: RunRequest, ask: FakeAsk) -> Any:
            await ask(BEGIN)
            try:
                return {"next": await ask({"do": "tool", "name": next_name, "arguments": {}})}
            except FakeRefused as refused:
                return refusal_of(refused)

        return program

    tools = [await add_tool(session_factory, name) for name in names]
    sandbox = by_program(**{name: asking(names[n + 1]) for n, name in enumerate(names[:-1])})
    deps = deps_of(session_factory, live_clock, sandbox, [])
    came = await launched(deps, tools[0], dana)
    assert ran(sandbox) == names[:5]
    deep: Any = came.result
    for _ in range(4):
        deep = deep["next"]
    assert deep == {"refused": "too_deep"}
    # A asks B, B asks A: refused, A is already in the path that leads to B.
    a = await add_tool(session_factory, "a")
    await add_tool(session_factory, "b")
    cycle = by_program(a=asking("b"), b=asking("a"))
    deps = deps_of(session_factory, live_clock, cycle, [])
    came = await launched(deps, a, dana)
    assert came.result == {"next": {"refused": "in_its_own_path"}}
    assert ran(cycle) == ["a", "b"]


# --- an action of the catalog --------------------------------------------------------------------


async def test_act_does_a_program_action_and_refuses_any_other(session_factory, live_clock, dana):
    at = (live_clock() + timedelta(hours=3)).replace(microsecond=0)

    async def acts(request: RunRequest, ask: FakeAsk) -> Any:
        said: JsonObject = {}
        await ask(BEGIN)
        said["when"] = await ask(
            {
                "do": "act",
                "action": "tool.launch_when",
                "arguments": {"tool": "own", "when": at.isoformat(), "arguments": {"n": 1}},
            }
        )
        try:
            await ask(
                {
                    "do": "act",
                    "action": "note.leave",
                    "arguments": {"recipient": "Alex", "text": "hi"},
                }
            )
        except FakeRefused as refused:
            said["note"] = refused.reason
        return said

    own = await add_tool(session_factory, "own")
    deps = deps_of(session_factory, live_clock, by_program(own=acts), [])
    came = await launched(deps, own, dana)
    assert came.status == "done"
    assert isinstance(came.result, dict) and came.result["note"] == "not_allowed"
    async with session_factory() as reader:
        [trigger] = (await reader.scalars(select(MadeToolTrigger))).all()
    # A trigger for whom the launch was made.
    assert (trigger.tool_id, trigger.kind, trigger.due_at) == (own.id, "time", at)
    assert (trigger.for_person_id, trigger.arguments) == (dana.id, {"n": 1})


async def test_a_program_never_times_a_tool_closed_to_whom_it_runs_for(
    session_factory, live_clock, dana, grant
):
    at = (live_clock() + timedelta(hours=3)).replace(microsecond=0)

    async def acts(request: RunRequest, ask: FakeAsk) -> Any:
        await ask(BEGIN)
        try:
            return await ask(
                {
                    "do": "act",
                    "action": "tool.launch_when",
                    "arguments": {"tool": "closed", "when": at.isoformat()},
                }
            )
        except FakeRefused as refused:
            return refusal_of(refused)

    own = await add_tool(session_factory, "own")
    await add_tool(session_factory, "closed")
    await grant("made.closed", person=dana, access="deny")
    deps = deps_of(session_factory, live_clock, by_program(own=acts), [])
    assert (await launched(deps, own, dana)).result == {"refused": "not_possible"}
    async with session_factory() as reader:
        assert (await reader.scalars(select(MadeToolTrigger))).all() == []


async def test_later_makes_a_time_trigger_for_whom_the_launch_is(session_factory, live_clock, dana):
    at = (live_clock() + timedelta(hours=3)).replace(microsecond=0)

    async def again(request: RunRequest, ask: FakeAsk) -> Any:
        await ask(BEGIN)
        said: list[Any] = [await ask({"do": "later", "at": at.isoformat(), "arguments": {"n": 7}})]
        past = (live_clock() - timedelta(minutes=1)).isoformat()
        for when, arguments in ((past, {}), (at.isoformat(), {"n": "seven"})):
            try:
                await ask({"do": "later", "at": when, "arguments": arguments})
            except FakeRefused as refused:
                said.append(refused.reason)
        return said

    own = await add_tool(session_factory, "own")
    deps = deps_of(session_factory, live_clock, by_program(own=again), [])
    came = await launched(deps, own, dana)
    assert came.result == [None, "past", "invalid"]
    async with session_factory() as reader:
        [trigger] = (await reader.scalars(select(MadeToolTrigger))).all()
    assert (trigger.tool_id, trigger.kind, trigger.due_at) == (own.id, "time", at)
    assert (trigger.for_person_id, trigger.arguments) == (dana.id, {"n": 7})


# --- waking Domo -------------------------------------------------------------------------------


async def test_a_wake_content_reaches_domo_as_data_and_never_the_audit(
    session_factory, live_clock, dana
):
    content = {"total": 12, "note": HOSTILE}

    async def tally(request: RunRequest, ask: FakeAsk) -> Any:
        await ask(BEGIN)
        await ask({"do": "wake", "content": content})
        return 12

    tool = await add_tool(session_factory, "tally")
    woken: Woken = []
    deps = deps_of(session_factory, live_clock, by_program(tally=tally), woken)
    assert (await launched(deps, tool, dana)).status == "done"
    # Domo is woken for whom the launch was made, with the content as it was given.
    assert woken == [(dana.id, [ToolWake("tally", content)])]
    assert HOSTILE not in json.dumps([entry.details for entry in await all_audit(session_factory)])


async def test_one_wake_per_person_per_chain_and_the_house_wakes_the_owner(
    session_factory, live_clock, alex, dana
):
    async def a(request: RunRequest, ask: FakeAsk) -> Any:
        await ask(BEGIN)
        await ask({"do": "wake", "content": 1})
        await ask({"do": "tool", "name": "b", "arguments": {}})
        await ask({"do": "wake", "content": 2})
        return None

    async def b(request: RunRequest, ask: FakeAsk) -> Any:
        await ask(BEGIN)
        await ask({"do": "wake", "content": "b"})
        return None

    first = await add_tool(session_factory, "a")
    await add_tool(session_factory, "b")
    woken: Woken = []
    deps = deps_of(session_factory, live_clock, by_program(a=a, b=b), woken)
    await launched(deps, first, dana)
    # One turn: what each tool said, twice said as a list.
    assert woken == [(dana.id, [ToolWake("a", [1, 2]), ToolWake("b", "b")])]
    woken.clear()
    # For the house, the owner of the moment it is told.
    await launched(deps, first, None)
    assert woken == [(alex.id, [ToolWake("a", [1, 2]), ToolWake("b", "b")])]


async def test_audit_holds_no_sql_and_no_value(session_factory, live_clock, tools_base, dana):
    sql = "CREATE TABLE secret_table_name (v text)"
    value = "a value nobody audits"

    async def keeps(request: RunRequest, ask: FakeAsk) -> Any:
        await ask(BEGIN)
        await ask({"do": "write", "sql": sql, "params": [], "space": "common"})
        await ask(
            {
                "do": "write",
                "sql": "INSERT INTO secret_table_name VALUES ($1)",
                "params": [value],
                "space": "common",
            }
        )
        return await ask(
            {
                "do": "read",
                "sql": "SELECT v FROM secret_table_name",
                "params": [],
                "space": "common",
            }
        )

    tool = await add_tool(session_factory, "keeps")
    deps = deps_of(session_factory, live_clock, by_program(keeps=keeps), [], tools_base=tools_base)
    came = await launched(deps, tool, dana)
    assert came.result == [{"v": value}]
    audited = json.dumps([entry.details for entry in await all_audit(session_factory)])
    for words in ("secret_table_name", "CREATE", "INSERT", "SELECT", value):
        assert words not in audited
    [entry] = await audit_of(session_factory, LAUNCHED)
    assert (entry.details["requests"], entry.details["reads"]) == (4, 1)


# --- ported from the requests a tool gave back (Task 6), and the rest of the channel --------------


def asking_each(*names: str):
    """A program that asks each tool of ``names`` in turn, and gives back what each answer was
    (a tool's result, or the reason it was refused)."""

    async def program(request: RunRequest, ask: FakeAsk) -> Any:
        said: list[Any] = []
        await ask(BEGIN)
        for name in names:
            try:
                said.append(await ask({"do": "tool", "name": name, "arguments": {}}))
            except FakeRefused as refused:
                said.append(refused.reason)
        return said

    return program


async def test_a_chain_launches_ten_tools_at_most(session_factory, live_clock, dana):
    names = ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k"]
    tools = {name: await add_tool(session_factory, name) for name in names}
    sandbox = by_program(a=asking_each(*"bcdef"), b=asking_each(*"ghijk"))
    deps = deps_of(session_factory, live_clock, sandbox, [])
    came = await launched(deps, tools["a"], dana)
    # a, b, what b asks (g to k), then c to e: the tenth launch is the last one, f is refused.
    assert len(sandbox.runs) == MAX_CHAIN_LAUNCHES == 10
    assert ran(sandbox) == ["a", "b", "g", "h", "i", "j", "k", "c", "d", "e"]
    assert came.result == [[None] * 5, None, None, None, "too_many_launches"]
    # Refused before any launch: audited all the same, metadata only.
    [refused] = await audit_of(session_factory, TOOL_REFUSED)
    assert (refused.actor, refused.details) == (
        f"person:{dana.id}",
        {"tool": "a", "reason": "too_many_launches"},
    )


async def test_an_unknown_tool_is_refused_and_the_program_goes_on(
    session_factory, live_clock, dana
):
    async def program(request: RunRequest, ask: FakeAsk) -> Any:
        await ask(BEGIN)
        try:
            await ask({"do": "tool", "name": "nothing", "arguments": {}})
        except FakeRefused as refused:
            await ask({"do": "wake", "content": "still said"})
            return refused.reason
        return None

    a = await add_tool(session_factory, "a")
    woken: Woken = []
    deps = deps_of(session_factory, live_clock, by_program(a=program), woken)
    assert (await launched(deps, a, dana)).result == "no_such_tool"
    assert woken == [(dana.id, [ToolWake("a", "still said")])]
    [refused] = await audit_of(session_factory, TOOL_REFUSED)
    assert refused.details == {"tool": "a", "reason": "no_such_tool"}
    # The name it asked for is the program's: it is not written.
    assert "nothing" not in json.dumps(refused.details)


async def test_too_deep_and_its_own_path_are_audited(session_factory, live_clock, dana):
    a = await add_tool(session_factory, "a")
    await add_tool(session_factory, "b")
    deps = deps_of(
        session_factory, live_clock, by_program(a=asking_each("b"), b=asking_each("a")), []
    )
    await launched(deps, a, dana)
    assert [entry.details for entry in await audit_of(session_factory, TOOL_REFUSED)] == [
        {"tool": "b", "reason": "in_its_own_path"}
    ]


async def test_a_tool_launched_by_another_passes_its_own_barrier(
    session_factory, live_clock, dana, grant
):
    a = await add_tool(session_factory, "a")
    await add_tool(session_factory, "b")
    await grant("made.b", person=dana, access="deny")
    sandbox = by_program(a=asking_each("b"))
    deps = deps_of(session_factory, live_clock, sandbox, [])
    assert (await launched(deps, a, dana)).result == ["not_allowed"]
    assert ran(sandbox) == ["a"]


async def test_a_nested_launch_that_needs_someones_agreement_is_refused_needs_agreement(
    session_factory, live_clock, dana, grant
):
    a = await add_tool(session_factory, "a")
    await add_tool(session_factory, "b")
    await grant("made.b", person=dana, approver="owner")
    sandbox = by_program(a=asking_each("b"))
    deps = deps_of(session_factory, live_clock, sandbox, [])
    # Nobody is asked from a program: the launch is not made.
    assert (await launched(deps, a, dana)).result == ["needs_agreement"]
    assert ran(sandbox) == ["a"]


async def test_a_sandbox_lost_in_the_middle_of_a_run_never_says_nothing_ran(
    session_factory, live_clock, dana
):
    # Final review m2: once the program asked something, part of it may be done.
    async def asks_then_is_lost(request: RunRequest, ask: FakeAsk) -> JsonObject:
        with contextlib.suppress(FakeRefused):
            await ask({"do": "read", "sql": "SELECT 1", "params": [], "space": "common"})
        raise SandboxUnavailable

    async def lost_at_once(request: RunRequest, ask: FakeAsk) -> JsonObject:
        raise SandboxUnavailable

    cut = await add_tool(session_factory, "cut")
    down = await add_tool(session_factory, "down")
    sandbox = by_program(cut=asks_then_is_lost, down=lost_at_once)
    deps = deps_of(session_factory, live_clock, sandbox, [])
    assert await launched(deps, cut, dana) == Launched("failed", {"reason": SANDBOX_CUT})
    assert await launched(deps, down, dana) == Launched("failed", {"reason": SANDBOX_DOWN})


async def test_an_owners_always_ask_on_a_tool_is_never_passed_outside_a_turn(
    session_factory, live_clock, dana, grant
):
    # Final review I2: nobody can say yes outside a turn, so what a rule asks is not made.
    a = await add_tool(session_factory, "a")
    await add_tool(session_factory, "b")
    await grant("made.b", person=dana, ask="yes")
    sandbox = by_program(a=asking_each("b"))
    deps = deps_of(session_factory, live_clock, sandbox, [])
    assert (await launched(deps, a, dana)).result == ["needs_agreement"]
    assert ran(sandbox) == ["a"]
    await grant("made.a", person=dana, ask="explicit_yes")
    came = await launched(deps, a, dana)
    assert (came.status, came.result) == ("refused", {"reason": "needs_confirmation"})
    assert ran(sandbox) == ["a"]


async def test_a_wake_too_heavy_is_refused_and_nothing_is_gathered(
    session_factory, live_clock, dana
):
    async def program(request: RunRequest, ask: FakeAsk) -> Any:
        try:
            await ask({"do": "wake", "content": {"text": "x" * 5000}})
        except FakeRefused as refused:
            return [refused.reason, "xxxx" in (refused.detail or "")]
        return None

    a = await add_tool(session_factory, "a")
    woken: Woken = []
    deps = deps_of(session_factory, live_clock, by_program(a=program), woken)
    assert (await launched(deps, a, dana)).result == ["too_big", False]
    assert woken == []


async def test_later_more_than_a_year_ahead_is_refused(session_factory, live_clock, dana):
    far = (live_clock() + timedelta(days=400)).isoformat()

    async def program(request: RunRequest, ask: FakeAsk) -> Any:
        await ask(BEGIN)
        try:
            await ask({"do": "later", "at": far, "arguments": {}})
        except FakeRefused as refused:
            return refused.reason
        return None

    own = await add_tool(session_factory, "own")
    deps = deps_of(session_factory, live_clock, by_program(own=program), [])
    assert (await launched(deps, own, dana)).result == "too_far"
    async with session_factory() as reader:
        assert (await reader.scalars(select(MadeToolTrigger))).all() == []


async def test_a_dry_run_judges_nothing_and_launches_nothing(
    session_factory, live_clock, dana, grant
):
    a = await add_tool(session_factory, "a")
    await add_tool(session_factory, "b")
    await add_tool(session_factory, "closed")
    await grant("made.closed", person=dana, access="deny")
    sandbox = by_program()
    scope = LaunchScope(
        tool_name="a",
        tool_id=a.id,
        for_whom=ForWhom(dana),
        actor=actor_of(dana),
        depth=0,
        chain=Chain(),
        waited=False,
        path=("a",),
        dry_run=True,
        operations=read_operations(OPERATIONS),
    )
    means = Means(deps_of(session_factory, live_clock, sandbox, []), scope)
    # Nothing but a read before an operation begins.
    assert await means.answer({"do": "tool", "name": "b", "arguments": {}}) == {
        "refused": "no_operation",
        "detail": means_module.NO_OPERATION,
    }
    assert await means.answer(BEGIN) == {"ok": None}
    assert await means.answer({"do": "tool", "name": "b", "arguments": {}}) == {"ok": None}
    # A dry run is a trial of the code, for nobody: no one's rights refuse it (they are judged
    # when it runs for real), and nothing of it is written in the audit as an act.
    assert await means.answer({"do": "tool", "name": "closed", "arguments": {}}) == {"ok": None}
    # What is refused for real whoever it runs for is refused alike.
    refused = await means.answer({"do": "tool", "name": "nothing", "arguments": {}})
    assert refused is not None and refused["refused"] == "no_such_tool"
    refused = await means.answer({"do": "tool", "name": "a", "arguments": {}})
    assert refused is not None and refused["refused"] == "in_its_own_path"
    # Nothing ran, nothing was audited; the two tools asked are counted in the chain, as they
    # would be (its bound is refused alike).
    assert sandbox.runs == []
    assert scope.chain.launches == 3
    assert await made_decisions(session_factory) == []
    assert await audit_of(session_factory, "policy.decision", means_module.TOOL_REFUSED) == []


async def test_an_answer_too_slow_is_refused_too_slow(
    session_factory, live_clock, dana, monkeypatch
):
    async def slow(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(5)

    monkeypatch.setattr(means_module, "decide_now", slow)

    async def program(request: RunRequest, ask: FakeAsk) -> Any:
        try:
            await ask(BEGIN)
        except FakeRefused as refused:
            return refused.reason
        return None

    a = await add_tool(session_factory, "a")
    async with session_factory() as writer:
        row = await writer.get(MadeTool, a.id)
        assert row is not None
        await set_for_tool(writer, row, {"answer_seconds": 1})
        await writer.commit()
    deps = deps_of(session_factory, live_clock, by_program(a=program), [])
    async with asyncio.timeout(4):
        assert (await launched(deps, a, dana)).result == "too_slow"


async def test_the_corners_password_reaches_no_answer_audit_or_log(
    session_factory, live_clock, tools_base, dana, caplog
):
    answers: list[Any] = []

    async def program(request: RunRequest, ask: FakeAsk) -> Any:
        await ask(BEGIN)
        for asked in (
            {"do": "write", "sql": "CREATE TABLE t (v text)", "params": [], "space": "mine"},
            {"do": "write", "sql": "INSERT INTO t VALUES ($1)", "params": ["v"], "space": "mine"},
            {"do": "read", "sql": "SELECT v, current_user FROM t", "params": [], "space": "mine"},
            # Refused by the base: what the server says comes back as a detail.
            {"do": "read", "sql": "SELECT * FROM nowhere", "params": [], "space": "mine"},
            {"do": "read", "sql": "SELECT pg_read_file('x')", "params": [], "space": "mine"},
            {"do": "read", "sql": "SELECT 1/0", "params": [], "space": "common"},
        ):
            try:
                answers.append(await ask(asked))
            except FakeRefused as refused:
                answers.append([refused.reason, refused.detail])
        return answers

    caplog.set_level(logging.DEBUG)
    a = await add_tool(session_factory, "a")
    deps = deps_of(session_factory, live_clock, by_program(a=program), [], tools_base=tools_base)
    came = await launched(deps, a, dana)
    async with session_factory() as reader:
        [space] = (
            await reader.scalars(select(ToolsSpace).where(ToolsSpace.person_id == dana.id))
        ).all()
    password = space.password
    assert len(password) >= 16
    assert answers[2] == [{"v": "v", "current_user": space.role_name}]
    assert [answer[0] for answer in answers[3:] if isinstance(answer, list)] == ["sql_error"] * 3
    for seen in (
        json.dumps(came.result),
        json.dumps(answers),
        json.dumps([entry.details for entry in await all_audit(session_factory)]),
        json.dumps([entry.actor for entry in await all_audit(session_factory)]),
        "\n".join(record.getMessage() for record in caplog.records),
        "\n".join(str(record.args) for record in caplog.records),
    ):
        assert password not in seen


async def test_a_persons_program_never_times_a_tool_for_the_house(
    session_factory, live_clock, dana
):
    # Fix round 1 (R15): a program times a tool for whom its launch is made, never for the house.
    at = (live_clock() + timedelta(hours=3)).replace(microsecond=0)

    async def acts(request: RunRequest, ask: FakeAsk) -> Any:
        await ask(BEGIN)
        try:
            await ask(
                {
                    "do": "act",
                    "action": "tool.launch_when",
                    "arguments": {"tool": "own", "when": at.isoformat(), "for": "house"},
                }
            )
        except FakeRefused as refused:
            return refused.reason
        return "timed"

    own = await add_tool(session_factory, "own")
    deps = deps_of(session_factory, live_clock, by_program(own=acts), [])
    assert (await launched(deps, own, dana)).result == "invalid"
    async with session_factory() as reader:
        assert (await reader.scalars(select(MadeToolTrigger))).all() == []
