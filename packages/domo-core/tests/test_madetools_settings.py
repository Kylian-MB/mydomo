"""The bounds of Domo's tools are settings of his own (spec 12.1, the owner, 2026-11-21).

Each bound has a start, a floor only where a smaller number has no meaning, and no ceiling; what
counts is the tool's own value, else the house's, else the start. Domo reads and changes them by
two tool calls of his own (``tools_settings``, ``tool_set``), never judged nor shown, stopped only
by the owner's deny of his own; no request a program makes reaches them. The module is tried on
the rolled-back ``session``; the tool calls with the sessions of the test database."""

import ast
import inspect
import typing
from dataclasses import fields, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import domo_core
import pytest
from domo_common.sandbox_api import ANSWER_CEILING_BYTES, Machine
from domo_core.db import SessionFactory
from domo_core.madetools import asks, program_actions, settings
from domo_core.madetools.launch import LaunchDeps
from domo_core.madetools.models import MadeTool, ToolSetting
from domo_core.madetools.places import places_pass
from domo_core.madetools.settings import (
    PER_TOOL,
    SETTINGS,
    InvalidSetting,
    all_values,
    check,
    hit,
    set_for_tool,
    set_house,
    value,
)
from domo_core.policy import barrier, program
from domo_core.policy.actions import DescribedAction
from domo_core.policy.barrier import Allow, Refuse
from domo_core.policy.catalog import (
    DEFAULT_POLICY,
    NOT_ON_HIS_OWN,
    PROGRAM_ACTIONS,
    is_silent,
    needs_the_judge,
    on_his_own_tables,
)
from domo_core.policy.facts import facts_of
from domo_core.policy.rights import NO_RIGHTS, Right, RightsTable
from domo_core.policy.roles import domo_actor
from domo_core.tools.context import ActsAsDomo, InvalidArguments, ToolContext
from domo_core.tools.registry import TOOLS
from fakes import FakeSandbox
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from test_made_tools_tools import (
    NOW,
    add_tool,
    audit_entries,
    closed_to,
    context,
    household,
    refusal,
    rows,
    run,
)

STARTS = {
    "chain_depth": 5,
    "chain_launches": 10,
    "requests_per_launch": 1000,
    "answer_seconds": 30,
    "run_seconds": 60,
    "memory_mb": 256,
    "output_bytes": 65536,
    "result_bytes": 65536,
    "rows": 1000,
    "statement_seconds": 10,
    "request_seconds": 15,
    "dry_run_seconds": 60,
    "launches_per_day": 200,
    "wakes_per_day": 20,
    "triggers_per_tool": 10,
    # Fixed bounds that became settings (final review, ruling R33), at today's figure.
    "trigger_argument_chars": 4000,
    "later_days": 365,
    "wake_bytes": 4096,
    "failures_in_a_row": 3,
    "places": 4,
    "free_rounds": 5,
    "first_pause_seconds": 10,
    "longest_pause_seconds": 600,
    "give_up_hours": 24,
    "builds_at_once": 2,
    "making_seconds": 600,
    "review_seconds": 180,
    "purpose_chars": 1800,
    "examples": 5,
    "examples_chars": 20000,
    "code_chars": 20000,
    "tools_offered": 30,
    "hand_runs_kept_days": 7,
    "launches_kept_days": 90,
    "trouble_wakes_per_day": 5,
    # What a program sends out through the gatekeeper (step 2, spec 13.3), and how long a site's
    # session is kept without use (the house's alone).
    "out_seconds": 30,
    "out_bytes": 1_048_576,
    "out_per_launch": 100,
    "session_idle_minutes": 60,
}
EXPECTED_PER_TOOL = {
    "chain_depth",
    "chain_launches",
    "requests_per_launch",
    "answer_seconds",
    "run_seconds",
    "memory_mb",
    "output_bytes",
    "result_bytes",
    "rows",
    "statement_seconds",
    "request_seconds",
    "launches_per_day",
    "wakes_per_day",
    "triggers_per_tool",
    "trigger_argument_chars",
    "later_days",
    "wake_bytes",
    "failures_in_a_row",
    "out_seconds",
    "out_bytes",
    "out_per_launch",
}
SETTING_CALLS = ("tools_settings", "tool_set")


async def a_tool(session: AsyncSession, name: str = "tally") -> MadeTool:
    tool = MadeTool(name=name, description=f"Does {name}.", parameters={"type": "object"})
    session.add(tool)
    await session.flush()
    return tool


# --- the settings themselves ---------------------------------------------------------------------


def test_the_settings_are_these_with_these_starts_and_floors():
    assert {key: setting.start for key, setting in SETTINGS.items()} == STARTS
    assert list(SETTINGS) == list(STARTS)
    assert set(PER_TOOL) == EXPECTED_PER_TOOL and set(SETTINGS) >= PER_TOOL
    for key, setting in SETTINGS.items():
        assert setting.key == key
        assert setting.least == (
            0 if key in ("free_rounds", "trouble_wakes_per_day", "out_per_launch") else 1
        ), key
        assert setting.says.strip() and setting.says.endswith("."), key
        assert setting.start >= setting.least


def test_a_refusal_a_setting_causes_carries_its_key_and_its_figure():
    assert hit("chain_depth", 5) == {"setting": "chain_depth", "value": 5}


@pytest.mark.parametrize(
    ("changes", "for_a_tool", "key"),
    [
        ({"no_such_bound": 3}, False, "no_such_bound"),
        ({"no_such_bound": 3}, True, "no_such_bound"),
        ({"places": 8}, True, "places"),
        ({"run_seconds": 5.0}, False, "run_seconds"),
        ({"run_seconds": "60"}, False, "run_seconds"),
        ({"run_seconds": True}, False, "run_seconds"),
        ({"run_seconds": 0}, False, "run_seconds"),
        ({"run_seconds": -1}, True, "run_seconds"),
        ({"free_rounds": -1}, False, "free_rounds"),
    ],
)
def test_a_change_no_setting_takes_is_refused_with_its_key(changes, for_a_tool, key):
    with pytest.raises(InvalidSetting) as refused:
        check(changes, for_a_tool=for_a_tool)
    assert refused.value.key == key and key in str(refused.value)


def test_no_ceiling_and_the_floor_only_where_smaller_means_nothing():
    assert check({"run_seconds": 10**9, "places": 10**9}, for_a_tool=False) == {
        "run_seconds": 10**9,
        "places": 10**9,
    }
    assert check({"free_rounds": 0, "trouble_wakes_per_day": 0}, for_a_tool=False) == {
        "free_rounds": 0,
        "trouble_wakes_per_day": 0,
    }
    assert check({"run_seconds": None}, for_a_tool=True) == {"run_seconds": None}


async def test_out_bytes_above_the_channel_s_ceiling_is_kept_as_set(session: AsyncSession):
    # The cut to the channel's ceiling is made where the bound is used (Task 6), never here.
    more = ANSWER_CEILING_BYTES + 1
    assert check({"out_bytes": more}, for_a_tool=True) == {"out_bytes": more}
    assert (await set_house(session, {"out_bytes": more}))["out_bytes"] == more
    assert await value(session, "out_bytes") == more
    tool = await a_tool(session)
    assert (await set_for_tool(session, tool, {"out_bytes": more * 2}))["out_bytes"] == more * 2


def test_the_session_s_idle_time_is_the_house_s_alone():
    with pytest.raises(InvalidSetting) as refused:
        check({"session_idle_minutes": 5}, for_a_tool=True)
    assert refused.value.key == "session_idle_minutes"
    assert check({"session_idle_minutes": 5}, for_a_tool=False) == {"session_idle_minutes": 5}


async def test_the_tool_s_own_wins_then_the_house_s_then_the_start(session: AsyncSession):
    tool = await a_tool(session)
    assert await value(session, "run_seconds", tool) == 60
    assert await value(session, "run_seconds") == 60
    await set_house(session, {"run_seconds": 120})
    assert await value(session, "run_seconds") == 120
    assert await value(session, "run_seconds", tool) == 120
    mine = await set_for_tool(session, tool, {"run_seconds": 900})
    assert mine["run_seconds"] == 900
    assert await value(session, "run_seconds", tool) == 900
    assert await value(session, "run_seconds") == 120
    assert (await all_values(session, tool))["run_seconds"] == 900
    assert (await all_values(session))["run_seconds"] == 120
    # A house-only key a tool's row would hold is never its own.
    tool.settings = {**tool.settings, "places": 99}
    await session.flush()
    assert await value(session, "places", tool) == 4
    assert (await all_values(session, tool))["places"] == 4


async def test_none_goes_back_to_the_level_above(session: AsyncSession):
    tool = await a_tool(session)
    await set_house(session, {"chain_depth": 8})
    await set_for_tool(session, tool, {"chain_depth": 12})
    back = await set_for_tool(session, tool, {"chain_depth": None})
    assert back["chain_depth"] == 8 and tool.settings == {}
    house = await set_house(session, {"chain_depth": None})
    assert house["chain_depth"] == 5
    assert await value(session, "chain_depth", tool) == 5
    assert list(await session.scalars(select(ToolSetting))) == []


async def test_a_huge_value_is_kept(session: AsyncSession):
    tool = await a_tool(session)
    await set_house(session, {"memory_mb": 10**9})
    await set_for_tool(session, tool, {"output_bytes": 10**9})
    assert await value(session, "memory_mb") == 10**9
    assert await value(session, "output_bytes", tool) == 10**9


async def test_a_change_with_one_wrong_key_changes_nothing(session: AsyncSession):
    tool = await a_tool(session)
    with pytest.raises(InvalidSetting):
        await set_house(session, {"run_seconds": 90, "rows": 0})
    with pytest.raises(InvalidSetting):
        await set_for_tool(session, tool, {"run_seconds": 90, "places": 2})
    assert list(await session.scalars(select(ToolSetting))) == []
    assert tool.settings == {}
    assert await all_values(session, tool) == STARTS


# --- the two calls -------------------------------------------------------------------------------


async def test_tools_settings_reads_every_key_and_changes_the_house(
    session_factory: SessionFactory,
):
    alex, sam, *_ = await household(session_factory)
    ctx = context(sam, session_factory)
    read = await run("tools_settings", ctx)
    # No sandbox in this turn: what its machine has is not known.
    assert set(read) == {"settings", "machine"} and read["machine"] is None
    assert list(read["settings"]) == list(STARTS)
    for key, shown in read["settings"].items():
        assert shown == {"value": STARTS[key], "start": STARTS[key], "says": SETTINGS[key].says}
    changed = await run("tools_settings", ctx, set={"places": 6, "run_seconds": 10**9})
    assert changed["status"] == "done" and changed["changed"] == ["places", "run_seconds"]
    assert changed["settings"]["places"] == {
        "value": 6,
        "start": 4,
        "says": SETTINGS["places"].says,
    }
    assert (await run("tools_settings", ctx))["settings"]["run_seconds"]["value"] == 10**9
    back = await run("tools_settings", ctx, set={"places": None})
    assert back["settings"]["places"]["value"] == 4


def with_a_sandbox(ctx: ToolContext, sandbox: FakeSandbox, session_factory) -> ToolContext:
    deps = LaunchDeps(
        session_factory=session_factory,
        sandbox=sandbox,
        clock=lambda: NOW,
        policy_base=DEFAULT_POLICY,
    )
    return replace(ctx, launch_deps=deps)


async def test_tools_settings_shows_what_the_sandbox_has_and_gives_it_the_places(
    session_factory: SessionFactory,
):
    alex, sam, *_ = await household(session_factory)
    sandbox = FakeSandbox()
    sandbox.has = Machine(memory_mb=2048, processors=4, places=4)
    ctx = with_a_sandbox(context(sam, session_factory), sandbox, session_factory)
    read = await run("tools_settings", ctx)
    assert read["machine"] == {"memory_mb": 2048, "processors": 4, "places": 4}
    assert sandbox.places_told == []
    # Changed: the sandbox runs that many at once from now, without a restart.
    changed = await run("tools_settings", ctx, set={"places": 6})
    assert sandbox.places_told == [6] and changed["machine"]["places"] == 6
    await run("tools_settings", ctx, set={"rows": 5})
    assert sandbox.places_told == [6]
    await run("tools_settings", ctx, set={"places": None})
    assert sandbox.places_told == [6, 4]
    # A sandbox that does not answer: the setting is changed all the same (the upkeep gives it
    # later), and what the machine has is not known.
    sandbox.has = None
    down = await run("tools_settings", ctx, set={"places": 3})
    assert down["machine"] is None and down["settings"]["places"]["value"] == 3


async def test_the_upkeep_gives_the_sandbox_the_places_at_each_round(
    session_factory: SessionFactory,
):
    sandbox = FakeSandbox()
    giving = places_pass(session_factory, lambda: sandbox)
    await giving(NOW)
    assert sandbox.places_told == [4]
    async with session_factory() as writer:
        await set_house(writer, {"places": 9})
        await writer.commit()
    # A launcher that started again since has its places back within a round.
    await giving(NOW)
    assert sandbox.places_told == [4, 9]
    sandbox.has = None
    await giving(NOW)  # a sandbox that does not answer is tried again at the next round
    assert sandbox.places_told == [4, 9]


async def test_tool_set_reads_and_changes_one_tool_s_own(session_factory: SessionFactory):
    alex, sam, *_ = await household(session_factory)
    await add_tool(session_factory, "tally")
    ctx = context(sam, session_factory)
    await run("tools_settings", ctx, set={"rows": 50})
    read = await run("tool_set", ctx, name="tally")
    assert read["name"] == "tally" and set(read["settings"]) == EXPECTED_PER_TOOL
    assert read["settings"]["rows"] == {"value": 50, "own": False}
    changed = await run("tool_set", ctx, name="tally", set={"rows": 5000})
    assert changed["status"] == "done" and changed["changed"] == ["rows"]
    assert changed["settings"]["rows"] == {"value": 5000, "own": True}
    [tool] = await rows(session_factory, MadeTool)
    assert tool.settings == {"rows": 5000}
    back = await run("tool_set", ctx, name="tally", set={"rows": None})
    assert back["settings"]["rows"] == {"value": 50, "own": False}


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("tools_settings", {"set": {"no_such_bound": 1}}),
        ("tools_settings", {"set": {"run_seconds": 1.5}}),
        ("tools_settings", {"set": {"run_seconds": "60"}}),
        ("tools_settings", {"set": {"run_seconds": False}}),
        ("tools_settings", {"set": {"places": 0}}),
        ("tools_settings", {"set": [1]}),
        ("tools_settings", {"other": 1}),
        ("tool_set", {"name": "tally", "set": {"places": 2}}),
        ("tool_set", {"name": "tally", "set": {"run_seconds": 0}}),
        ("tool_set", {"set": {"run_seconds": 30}}),
    ],
)
async def test_a_wrong_change_is_refused_and_nothing_is_written(
    session_factory: SessionFactory, tool_name: str, arguments: dict[str, Any]
):
    alex, *_ = await household(session_factory)
    await add_tool(session_factory, "tally")
    ctx = context(alex, session_factory)
    with pytest.raises(InvalidArguments) as described:
        await TOOLS[tool_name].describe(arguments, ctx.actor, ctx)
    refused = await refusal(tool_name, ctx, **arguments)
    assert refused.code == described.value.code == "invalid"
    asked = arguments.get("set")
    if isinstance(asked, dict) and (tool_name == "tools_settings" or "name" in arguments):
        # The refusal names the key it is about.
        for key in asked:
            assert refused.detail is not None and key in refused.detail
    assert await rows(session_factory, ToolSetting) == []
    [tool] = await rows(session_factory, MadeTool)
    assert tool.settings == {}
    assert await audit_entries(session_factory, "madetool.settings_changed") == []


async def test_a_tool_closed_to_the_person_has_its_settings_neither_read_nor_changed(
    session_factory: SessionFactory,
):
    alex, _, dana, paul = await household(session_factory)
    await add_tool(session_factory, "split", roles=("owner", "adult", "child", "guest"))
    await closed_to(session_factory, alex, paul, "split")
    closed = context(paul, session_factory)
    for arguments in ({"name": "split"}, {"name": "split", "set": {"rows": 9}}):
        with pytest.raises(InvalidArguments) as described:
            await TOOLS["tool_set"].describe(arguments, closed.actor, closed)
        assert described.value.code == "not_possible"
        assert (await refusal("tool_set", closed, **arguments)).code == "not_possible"
    [tool] = await rows(session_factory, MadeTool)
    assert tool.settings == {}
    # Dana, to whom it is open, reads and changes them; nobody's tool name is refused as none.
    await run("tool_set", context(dana, session_factory), name="split", set={"rows": 9})
    assert (await refusal("tool_set", closed, name="nothing")).code == "not_found"


async def test_each_change_leaves_one_audit_line_of_keys_and_figures(
    session_factory: SessionFactory,
):
    alex, sam, *_ = await household(session_factory)
    await add_tool(session_factory, "tally")
    await run("tools_settings", context(sam, session_factory), set={"places": 6, "rows": 70})
    alone = replace(context(alex, session_factory), on_his_own=True)
    await run("tool_set", alone, name="tally", set={"rows": 7000})
    # Reading leaves none.
    await run("tools_settings", alone)
    await run("tool_set", alone, name="tally")
    house, tool = await audit_entries(session_factory, "madetool.settings_changed")
    # The actor is the person of the turn, or Domo in a turn of his own, as for the other calls.
    assert house.actor == f"person:{sam.id}" and tool.actor == "domo"
    assert house.details == {
        "keys": ["places", "rows"],
        "before": {"places": 4, "rows": 1000},
        "after": {"places": 6, "rows": 70},
    }
    assert tool.details == {
        "keys": ["rows"],
        "before": {"rows": 70},
        "after": {"rows": 7000},
        "tool": "tally",
    }


# --- Domo's own acts ---------------------------------------------------------------------------


def _right_for_domo(action_type: str) -> Right:
    return Right(
        uuid4(),
        None,
        None,
        action_type,
        "deny",
        None,
        None,
        None,
        None,
        "Not for the assistant.",
        for_domo=True,
    )


def test_the_two_calls_are_domo_s_own_silent_unjudged_and_his_on_his_own():
    alone = on_his_own_tables(DEFAULT_POLICY)
    for name in SETTING_CALLS:
        tool = TOOLS[name]
        assert isinstance(tool, ActsAsDomo), name
        assert "Nobody is told" in tool.definition.description, name
        action_type = tool.action_type
        assert action_type == name.replace("_", ".", 1)
        assert action_type in DEFAULT_POLICY.domo and action_type not in NOT_ON_HIS_OWN
        assert is_silent(action_type) and not needs_the_judge(action_type)
        for role in ("owner", "adult", "child", "guest"):
            assert action_type in DEFAULT_POLICY.allowed[role], (name, role)  # type: ignore[index]
        assert NO_RIGHTS.resolve(domo_actor(), action_type, alone).allowed, name


@pytest.mark.parametrize("action_type", ["tools.settings", "tool.set"])
def test_the_owner_stops_them_with_a_deny_of_domo_s_own(action_type: str):
    action = DescribedAction("core", action_type, None, "self_profile", "household")
    domo = domo_actor()

    def decided(rights: RightsTable):
        return barrier.decide(
            domo,
            action,
            DEFAULT_POLICY,
            rights,
            facts_of(domo, action, name="Domo", local_now=None),
        )

    assert isinstance(decided(NO_RIGHTS), Allow)
    assert decided(RightsTable((_right_for_domo(action_type),))) == Refuse("not_allowed")


# --- nothing a program asks reaches them ---------------------------------------------------------

_SETTERS = ("set_house", "set_for_tool")


def _names_in(source: str) -> set[str]:
    tree = ast.parse(source)
    found = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    found |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    found |= {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
    }
    found |= {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    found |= {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    return found


def test_nothing_a_program_asks_reaches_the_settings():
    members = typing.get_args(asks.ProgramAsk.__value__)
    assert members
    for member in members:
        names = {field.name for field in fields(member)} | _names_in(inspect.getsource(member))
        assert not names & {*_SETTERS, "settings", "tools.settings", "tool.set"}, member
    for module in (asks, program, program_actions):
        names = _names_in(inspect.getsource(module))
        assert not names & {*_SETTERS, "tools.settings", "tool.set"}, module.__name__
    assert not {"tools.settings", "tool.set"} & PROGRAM_ACTIONS
    assert not {"tools.settings", "tool.set"} & set(program_actions.PROGRAM_HANDLERS)
    # In the whole core, only the settings themselves and Domo's two calls name the setters.
    root = Path(domo_core.__file__).parent
    naming = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if set(_SETTERS) & _names_in(path.read_text(encoding="utf-8"))
    }
    assert naming == {"madetools/settings.py", "tools/made_tools.py"}
    assert settings.set_house is set_house and settings.set_for_tool is set_for_tool
    # A build's model, Domo's own maker and never a program, writes the time and the launches a
    # day its submission says on its tool: only the build's writes name that.
    from_build = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if "write_for_tool_from_build" in _names_in(path.read_text(encoding="utf-8"))
    }
    assert from_build == {"madetools/settings.py", "madetools/build_writes.py"}
    for module in (asks, program, program_actions):
        assert "write_for_tool_from_build" not in _names_in(inspect.getsource(module))


def test_a_build_writes_only_what_it_says_checked_as_a_tool_s_own():
    tool = MadeTool(name="built", description="d", parameters={}, settings={"rows": 5})
    settings.write_for_tool_from_build(tool, {"run_seconds": 5000, "launches_per_day": None})
    assert tool.settings == {"rows": 5, "run_seconds": 5000}
    for wrong in ({"run_seconds": 0}, {"places": 2}):
        with pytest.raises(InvalidSetting):
            settings.write_for_tool_from_build(tool, wrong)
    assert tool.settings == {"rows": 5, "run_seconds": 5000}


# --- nothing is shown (final review, the two e2e scenarios) ---------------------------------


@pytest.mark.parametrize(
    ("call", "arguments"),
    [
        ("tool_set", {"name": "probe", "set": {"failures_in_a_row": 1}}),
        ("tools_settings", {"set": {"places": 4}}),
        ("tool_look", {"name": "probe", "figures": True}),
    ],
)
async def test_his_acts_on_his_own_tools_show_no_card(owner_chat, session_factory, call, arguments):
    from test_madetools_handover import add_tool as add_ready_tool

    await add_ready_tool(session_factory, "probe")
    reply = await owner_chat.say_with_tool(call, arguments)
    assert reply.actions == [{"tool": call, "status": "done"}]
    assert reply.cards_html == ""
    assert f'data-tool="{call}"' not in (await owner_chat.get("/chat")).text
    assert 'class="action-card"' not in (await owner_chat.get("/chat")).text
