"""A request that waits for the build of a tool (Task 10, spec 5.5, 5.8; Review Focus 1).

A plan with ``after_build`` is due when its build ends, ready or abandoned, or at its time if that
comes first; the turn it wakes is told how the build stands. It is served once: done whatever woke
it when it does not repeat (the other wake never comes), and a repeating plan goes on at its times
and uses the tool. The plans pass and the turns run on a live server (``chat_support.py``); the
builds are rows the test writes, as the build pass would leave them.
"""

import json
import uuid
from datetime import timedelta
from typing import Any

import pytest
from chat_support import ChatPilot, ChatServer
from domo_core.accounts.models import Person
from domo_core.channels.turns import app_turn_deps
from domo_core.llm.types import ToolCall
from domo_core.madetools.models import BuildState, MadeTool, ToolBuild
from domo_core.plans.models import Plan
from domo_core.plans.service import change
from domo_core.plans.upkeep import plans_pass
from domo_core.prompts import BeingBuilt, build_system_prompt
from domo_core.tools.context import InvalidArguments
from domo_core.tools.registry import TOOLS
from fakes import FakeChatModel
from sqlalchemy import update
from test_plan_tools import context, plans_in
from test_plan_turns import LATER, UTC_ZONE, due_in, make_plan, plan_calls, plan_row, run_the_pass

DAILY = "FREQ=DAILY;BYHOUR=8;BYMINUTE=0"


async def open_build(
    server: ChatServer,
    person: Person,
    *,
    name: str = "tally",
    state: BuildState = "building",
) -> uuid.UUID:
    """A build of ``name`` that ``person`` opened, in ``state``, opened now; committed."""
    async with server.session_factory() as session:
        build = ToolBuild(
            name=name,
            purpose=f"Keep the {name} of the household.",
            opened_by=person.id,
            state=state,
            created_at=server.clock(),
            last_progress_at=server.clock(),
        )
        session.add(build)
        await session.commit()
        return build.id


async def end_build(
    server: ChatServer, build_id: uuid.UUID, state: BuildState, person: Person | None = None
) -> None:
    """The build ends in ``state``: ``ready`` makes its tool (Domo's), named as the build;
    ``person`` is whose turn the build was opened in, which the tool does not keep."""
    async with server.session_factory() as session:
        build = await session.get(ToolBuild, build_id)
        assert build is not None
        if state == "ready":
            tool = MadeTool(
                name=build.name, description=f"Does {build.name}.", parameters={"type": "object"}
            )
            session.add(tool)
            await session.flush()
            build.tool_id = tool.id
        build.state = state
        build.outcome = "given up" if state == "abandoned" else None
        await session.commit()


async def waiting_for(chat: ChatPilot, why: str, build_id: uuid.UUID, **more: Any) -> uuid.UUID:
    """A plan of ``chat``'s person that waits for ``build_id`` (``more``: its time, its rule)."""
    return await make_plan(
        chat, why, after=more.pop("after", None), after_build_id=build_id, **more
    )


# --- one wake, whatever comes first --------------------------------------------------------------


async def test_a_plan_waits_for_its_build_and_is_due_once_when_the_tool_is_ready(
    owner_chat: ChatPilot,
):
    server = owner_chat.server
    build = await open_build(server, owner_chat.person)
    plan_id = await waiting_for(owner_chat, "tell them the tally of the bins", build)
    owner_chat.use_model(FakeChatModel(["The bins: three this week."]))
    # No time, the build runs: nothing is due.
    server.clock.advance(timedelta(days=2))
    await run_the_pass(server)
    assert plan_calls(server) == []

    await end_build(server, build, "ready", owner_chat.person)
    await run_the_pass(server)
    [call] = plan_calls(server)
    assert due_in(call.system) == [
        {
            "id": str(plan_id),
            "why": "tell them the tally of the bins",
            "build": "ready",
            "tool": "tally",
        }
    ]
    plan = await plan_row(server, plan_id)
    assert (plan.state, plan.last_outcome) == ("done", "spoke")
    # Once.
    await run_the_pass(server)
    assert len(plan_calls(server)) == 1


async def test_the_deadline_first_then_the_end_of_the_build_wakes_nothing(owner_chat: ChatPilot):
    server = owner_chat.server
    build = await open_build(server, owner_chat.person)
    plan_id = await waiting_for(owner_chat, "remind them of the bins", build, after=LATER)
    owner_chat.use_model(FakeChatModel(["Bins tonight."]))
    server.clock.advance(LATER)
    await run_the_pass(server)
    [call] = plan_calls(server)
    # Its time came while the tool was being built: Domo does it another way.
    [shown] = due_in(call.system)
    assert shown["build"] == "building" and "tool" not in shown
    assert (await plan_row(server, plan_id)).state == "done"

    await end_build(server, build, "ready", owner_chat.person)
    await run_the_pass(server)
    assert len(plan_calls(server)) == 1


async def test_both_in_the_same_pass_make_one_turn_and_one_settle(owner_chat: ChatPilot):
    server = owner_chat.server
    build = await open_build(server, owner_chat.person)
    plan_id = await waiting_for(owner_chat, "say how many bins", build, after=LATER)
    await end_build(server, build, "ready", owner_chat.person)
    owner_chat.use_model(FakeChatModel(["Three bins."]))
    server.clock.advance(LATER)
    await run_the_pass(server)
    [call] = plan_calls(server)
    assert [p["build"] for p in due_in(call.system)] == ["ready"]
    plan = await plan_row(server, plan_id)
    assert (plan.state, plan.last_fired_at) == ("done", server.clock())
    await run_the_pass(server)
    assert len(plan_calls(server)) == 1


async def test_a_build_abandoned_wakes_the_plan_that_had_no_time(owner_chat: ChatPilot):
    server = owner_chat.server
    build = await open_build(server, owner_chat.person)
    plan_id = await waiting_for(owner_chat, "tell them the tally", build)
    await end_build(server, build, "abandoned")
    owner_chat.use_model(FakeChatModel(["I counted by hand: three."]))
    await run_the_pass(server)
    [call] = plan_calls(server)
    assert due_in(call.system) == [
        {"id": str(plan_id), "why": "tell them the tally", "build": "abandoned"}
    ]
    assert (await plan_row(server, plan_id)).state == "done"
    await run_the_pass(server)
    assert len(plan_calls(server)) == 1


async def test_three_plans_on_one_build_are_all_due_when_it_ends(owner_chat: ChatPilot):
    server = owner_chat.server
    build = await open_build(server, owner_chat.person)
    ids = [await waiting_for(owner_chat, f"request {n}", build) for n in range(3)]
    await end_build(server, build, "ready", owner_chat.person)
    owner_chat.use_model(FakeChatModel(["All three done."]))
    await run_the_pass(server)
    [call] = plan_calls(server)
    assert sorted(p["id"] for p in due_in(call.system)) == sorted(str(i) for i in ids)
    assert [(await plan_row(server, i)).state for i in ids] == ["done"] * 3


async def test_a_restart_between_the_end_of_the_build_and_the_pass_loses_nothing(
    owner_chat: ChatPilot,
):
    server = owner_chat.server
    build = await open_build(server, owner_chat.person)
    plan_id = await waiting_for(owner_chat, "tell them the tally", build)
    # The build ends; the core stops before any pass looked.
    await end_build(server, build, "ready", owner_chat.person)
    server.clock.advance(timedelta(hours=3))
    owner_chat.use_model(FakeChatModel(["Three."]))
    # A new pass, as after a restart (nothing kept in memory).
    await run_the_pass(server, plans_pass(app_turn_deps(server.app)))
    assert len(plan_calls(server)) == 1
    assert (await plan_row(server, plan_id)).state == "done"


async def test_a_failed_turn_leaves_the_plan_due_for_its_build(owner_chat: ChatPilot):
    server = owner_chat.server
    build = await open_build(server, owner_chat.person)
    plan_id = await waiting_for(owner_chat, "tell them the tally", build)
    await end_build(server, build, "ready", owner_chat.person)
    owner_chat.use_model(FakeChatModel(["never said"], fail_after=0))
    await run_the_pass(server)
    plan = await plan_row(server, plan_id)
    assert (plan.state, plan.last_outcome, plan.after_build_id) == ("waiting", "failed", build)
    owner_chat.use_model(FakeChatModel(["Three."]))
    await run_the_pass(server)
    assert len(plan_calls(server)) == 2
    assert (await plan_row(server, plan_id)).state == "done"


# --- a request that repeats ----------------------------------------------------------------------


async def test_a_repeating_plan_woken_by_its_build_keeps_its_time_and_stops_waiting(
    owner_chat: ChatPilot,
):
    server = owner_chat.server
    build = await open_build(server, owner_chat.person)
    plan_id = await waiting_for(owner_chat, "say the tally each morning", build, repeat=DAILY)
    first = (await plan_row(server, plan_id)).due_at
    await end_build(server, build, "ready", owner_chat.person)
    owner_chat.use_model(FakeChatModel(["I have a tally now."]))
    # A turn starts after the plan began to wait (a pass never starts at that very moment).
    server.clock.advance(timedelta(seconds=1))
    await run_the_pass(server)
    assert len(plan_calls(server)) == 1
    plan = await plan_row(server, plan_id)
    assert (plan.state, plan.due_at, plan.after_build_id) == ("waiting", first, None)
    # Its next time comes at its date, once.
    await run_the_pass(server)
    assert len(plan_calls(server)) == 1
    assert first is not None
    server.clock.advance(first - server.clock())
    await run_the_pass(server)
    assert len(plan_calls(server)) == 2


async def test_a_repeating_plan_served_by_its_date_is_not_woken_again_by_its_build(
    owner_chat: ChatPilot,
):
    server = owner_chat.server
    build = await open_build(server, owner_chat.person)
    plan_id = await waiting_for(owner_chat, "say the tally each morning", build, repeat=DAILY)
    first = (await plan_row(server, plan_id)).due_at
    assert first is not None
    owner_chat.use_model(FakeChatModel(["Counted by hand today."]))
    server.clock.advance(first - server.clock())
    await run_the_pass(server)
    [call] = plan_calls(server)
    assert due_in(call.system)[0]["build"] == "building"
    plan = await plan_row(server, plan_id)
    # Its next time, and it still waits for the build.
    assert (plan.state, plan.due_at, plan.after_build_id) == (
        "waiting",
        first + timedelta(days=1),
        build,
    )
    # The build ends: no turn for that alone.
    await end_build(server, build, "ready", owner_chat.person)
    await run_the_pass(server)
    assert len(plan_calls(server)) == 1
    # Its next time is told the tool is ready, and it no longer waits for it.
    server.clock.advance(timedelta(days=1))
    await run_the_pass(server)
    [_, second] = plan_calls(server)
    assert {k: due_in(second.system)[0].get(k) for k in ("build", "tool")} == {
        "build": "ready",
        "tool": "tally",
    }
    plan = await plan_row(server, plan_id)
    assert (plan.due_at, plan.after_build_id) == (first + timedelta(days=2), None)


async def test_a_plan_domo_puts_off_in_its_turn_is_not_woken_again_by_the_ended_build(
    owner_chat: ChatPilot,
):
    server = owner_chat.server
    build = await open_build(server, owner_chat.person)
    plan_id = await waiting_for(owner_chat, "tell them the tally", build)
    await end_build(server, build, "abandoned")
    server.clock.advance(LATER)
    later = server.clock() + timedelta(days=1)
    change = ToolCall("p1", "plan_change", f'{{"id": "{plan_id}", "when": "{later.isoformat()}"}}')
    owner_chat.use_model(FakeChatModel(tool_script=[change, ""]))
    await run_the_pass(server)
    plan = await plan_row(server, plan_id)
    assert (plan.state, plan.due_at, plan.after_build_id) == ("waiting", later, None)
    await run_the_pass(server)
    assert len(plan_calls(server)) == 1


# --- plan_set and plan_change --------------------------------------------------------------------


async def test_plan_set_waits_for_a_build_with_no_time(owner_chat: ChatPilot):
    server = owner_chat.server
    build = await open_build(server, owner_chat.person)
    reply = await owner_chat.say_with_tool(
        "plan_set", {"why": "tell them the tally", "after_build": str(build)}
    )
    assert reply.actions == [{"tool": "plan_set", "status": "done"}]
    result = reply.model_results[0]
    assert (result["when"], result["after_build"]) == (None, str(build))
    async with server.session_factory() as session:
        plan = await session.get(Plan, uuid.UUID(result["id"]))
        assert plan is not None and plan.after_build_id == build


async def plan_set_refused(chat: ChatPilot, build: uuid.UUID | str) -> InvalidArguments:
    ctx = context(chat.person, chat.server.session_factory)
    arguments = {"why": "tell them the tally", "after_build": str(build)}
    with pytest.raises(InvalidArguments) as error:
        await TOOLS["plan_set"].execute(arguments, ctx)
    return error.value


async def test_plan_set_refuses_a_build_abandoned_or_none(
    owner_chat: ChatPilot, adult_chat: ChatPilot
):
    server = owner_chat.server
    gone = await open_build(server, owner_chat.person, name="gone", state="abandoned")
    refused = await plan_set_refused(owner_chat, gone)
    assert refused.code == "not_possible" and "abandoned" in (refused.detail or "")
    assert (await plan_set_refused(owner_chat, uuid.uuid4())).code == "not_possible"
    assert (await plan_set_refused(owner_chat, "not an id")).code == "not_found"
    # A build opened in someone else's turn may be waited for: every tool is Domo's.
    theirs = await open_build(server, adult_chat.person, name="their_tally")
    ctx = context(owner_chat.person, server.session_factory)
    done = await TOOLS["plan_set"].execute({"why": "x", "after_build": str(theirs)}, ctx)
    assert done.model_result["after_build"] == str(theirs)


async def test_a_plan_on_a_build_already_ready_is_due_at_the_next_pass(owner_chat: ChatPilot):
    server = owner_chat.server
    build = await open_build(server, owner_chat.person)
    await end_build(server, build, "ready", owner_chat.person)
    ctx = context(owner_chat.person, server.session_factory)
    await TOOLS["plan_set"].execute({"why": "tell them the tally", "after_build": str(build)}, ctx)
    owner_chat.use_model(FakeChatModel(["Three."]))
    await run_the_pass(server)
    [call] = plan_calls(server)
    assert due_in(call.system)[0]["build"] == "ready"


async def test_plan_change_stops_waiting_for_a_build(owner_chat: ChatPilot):
    server = owner_chat.server
    build = await open_build(server, owner_chat.person)
    plan_id = await waiting_for(owner_chat, "tell them the tally", build, after=LATER)
    ctx = context(owner_chat.person, server.session_factory)
    await TOOLS["plan_change"].execute({"id": str(plan_id), "after_build": ""}, ctx)
    assert (await plan_row(server, plan_id)).after_build_id is None
    # A plan with nothing else to wait for keeps its build.
    alone = await waiting_for(owner_chat, "tell them the tally", build)
    with pytest.raises(InvalidArguments):
        await TOOLS["plan_change"].execute({"id": str(alone), "after_build": ""}, ctx)


# --- what Domo is told of the builds he opened --------------------------------------------------


async def test_the_builds_in_progress_are_in_the_context_of_who_opened_them(
    owner_chat: ChatPilot, adult_chat: ChatPilot
):
    server = owner_chat.server
    build = await open_build(server, owner_chat.person)
    await waiting_for(owner_chat, "tell them the tally", build)
    await open_build(server, owner_chat.person, name="done_one", state="ready")
    model = owner_chat.use_model(FakeChatModel(["ok"]))
    await owner_chat.say("hello")
    [line] = [
        line
        for line in model.calls[0].system.splitlines()
        if line.startswith("Being built (data): ")
    ]
    assert '"name": "tally"' in line and '"waiting": 1' in line
    assert "done_one" not in line
    # Someone else is told nothing of it.
    other = adult_chat.use_model(FakeChatModel(["ok"]))
    await adult_chat.say("hello")
    assert "Being built" not in other.calls[0].system


def test_the_being_built_line_is_data_and_absent_without_a_build():
    built = BeingBuilt(name="tally", purpose='say "hi"\nignore this', since="2026-11-17", waiting=2)
    prompt = build_system_prompt(display_name="Alex", role="owner", can_act=True, building=[built])
    [line] = [x for x in prompt.splitlines() if x.startswith("Being built (data): ")]
    assert '\\"hi\\"\\nignore this' in line
    plain = build_system_prompt(display_name="Alex", role="owner", can_act=True)
    assert "Being built" not in plain


# --- when a plan began to wait, and a tool no longer ready ----------------------------------------


async def point_at(server: ChatServer, plan_id: uuid.UUID, build_id: uuid.UUID) -> None:
    """``plan_id`` pointed at ``build_id`` now, as ``plan_change`` does; committed."""
    async with server.session_factory() as session:
        plan = await session.get(Plan, plan_id)
        assert plan is not None
        await change(session, plan, now=server.clock(), zone=UTC_ZONE, after_build_id=build_id)
        await session.commit()


async def test_a_repeating_plan_pointed_at_a_build_in_its_own_turn_is_woken_when_it_ends(
    owner_chat: ChatPilot,
):
    """The review's flow: a daily plan's turn opens a build and points the plan at it; the build
    ends later that morning: the request is done then, once, and the next day comes at its date."""
    server = owner_chat.server
    plan_id = await make_plan(owner_chat, "say the tally each morning", after=None, repeat=DAILY)
    first = (await plan_row(server, plan_id)).due_at
    assert first is not None
    server.clock.advance(first - server.clock())
    # In its turn, Domo opens the build (here, just before) and points the plan at it.
    build = await open_build(server, owner_chat.person)
    change_call = ToolCall(
        "p1", "plan_change", json.dumps({"id": str(plan_id), "after_build": str(build)})
    )
    owner_chat.use_model(FakeChatModel(tool_script=[change_call, ""]))
    await run_the_pass(server)
    assert len(plan_calls(server)) == 1
    plan = await plan_row(server, plan_id)
    # Its time today was served; it waits for the build, and its next time is tomorrow.
    assert (plan.state, plan.after_build_id, plan.due_at) == (
        "waiting",
        build,
        first + timedelta(days=1),
    )
    await run_the_pass(server)
    assert len(plan_calls(server)) == 1

    # The build ends at 08:20: that wakes it, once, told the tool is ready.
    server.clock.advance(timedelta(minutes=20))
    await end_build(server, build, "ready", owner_chat.person)
    owner_chat.use_model(FakeChatModel(["The tally today: three."]))
    await run_the_pass(server)
    [_, woken] = plan_calls(server)
    assert {k: due_in(woken.system)[0].get(k) for k in ("build", "tool")} == {
        "build": "ready",
        "tool": "tally",
    }
    plan = await plan_row(server, plan_id)
    assert (plan.after_build_id, plan.due_at) == (None, first + timedelta(days=1))
    await run_the_pass(server)
    assert len(plan_calls(server)) == 2
    # The next occurrence at its date.
    server.clock.advance(first + timedelta(days=1) - server.clock())
    await run_the_pass(server)
    assert len(plan_calls(server)) == 3


async def test_a_plan_pointed_later_at_an_older_build_still_running_is_woken_when_it_ends(
    owner_chat: ChatPilot,
):
    server = owner_chat.server
    build = await open_build(server, owner_chat.person)
    plan_id = await make_plan(owner_chat, "say the tally each morning", after=None, repeat=DAILY)
    first = (await plan_row(server, plan_id)).due_at
    assert first is not None
    owner_chat.use_model(FakeChatModel(["Counted by hand."]))
    server.clock.advance(first - server.clock())
    await run_the_pass(server)
    assert len(plan_calls(server)) == 1
    # Later, in a turn of the person, the plan is pointed at that build (opened before it fired).
    server.clock.advance(timedelta(hours=1))
    await point_at(server, plan_id, build)
    server.clock.advance(timedelta(minutes=10))
    await end_build(server, build, "ready", owner_chat.person)
    owner_chat.use_model(FakeChatModel(["Now with the tool."]))
    await run_the_pass(server)
    assert len(plan_calls(server)) == 2
    await run_the_pass(server)
    assert len(plan_calls(server)) == 2


async def test_a_plan_pointed_at_an_older_build_already_ended_is_due_once(owner_chat: ChatPilot):
    server = owner_chat.server
    build = await open_build(server, owner_chat.person)
    await end_build(server, build, "ready", owner_chat.person)
    plan_id = await make_plan(owner_chat, "say the tally each morning", after=None, repeat=DAILY)
    first = (await plan_row(server, plan_id)).due_at
    server.clock.advance(timedelta(seconds=1))
    await point_at(server, plan_id, build)
    owner_chat.use_model(FakeChatModel(["Three."]))
    server.clock.advance(timedelta(seconds=1))
    await run_the_pass(server)
    await run_the_pass(server)
    assert len(plan_calls(server)) == 1
    plan = await plan_row(server, plan_id)
    assert (plan.after_build_id, plan.due_at) == (None, first)


@pytest.mark.parametrize("now_state", ["removed", "set_aside"])
async def test_a_build_whose_tool_is_no_longer_ready_is_told_abandoned(
    owner_chat: ChatPilot, now_state: str
):
    server = owner_chat.server
    build = await open_build(server, owner_chat.person)
    plan_id = await waiting_for(owner_chat, "tell them the tally", build)
    await end_build(server, build, "ready", owner_chat.person)
    async with server.session_factory() as session:
        await session.execute(
            update(MadeTool).where(MadeTool.name == "tally").values(state=now_state)
        )
        await session.commit()
    owner_chat.use_model(FakeChatModel(["By hand: three."]))
    server.clock.advance(timedelta(seconds=1))
    await run_the_pass(server)
    [call] = plan_calls(server)
    assert due_in(call.system) == [
        {"id": str(plan_id), "why": "tell them the tally", "build": "abandoned"}
    ]


async def test_plan_change_takes_a_build_opened_in_someone_else_s_turn(
    owner_chat: ChatPilot, adult_chat: ChatPilot
):
    server = owner_chat.server
    mine = await open_build(server, owner_chat.person)
    plan_id = await waiting_for(owner_chat, "tell them the tally", mine)
    theirs = await open_build(server, adult_chat.person, name="their_tally")
    gone = await open_build(server, adult_chat.person, name="gone", state="abandoned")
    ctx = context(owner_chat.person, server.session_factory)
    with pytest.raises(InvalidArguments) as refused:
        await TOOLS["plan_change"].execute({"id": str(plan_id), "after_build": str(gone)}, ctx)
    assert refused.value.code == "not_possible"
    assert (await plan_row(server, plan_id)).after_build_id == mine
    await TOOLS["plan_change"].execute({"id": str(plan_id), "after_build": str(theirs)}, ctx)
    assert (await plan_row(server, plan_id)).after_build_id == theirs


async def test_the_plans_line_names_the_build_a_plan_waits_for(owner_chat: ChatPilot):
    server = owner_chat.server
    build = await open_build(server, owner_chat.person)
    plan_id = await waiting_for(owner_chat, "tell them the tally", build)
    other = await make_plan(owner_chat, "water the plants")
    model = owner_chat.use_model(FakeChatModel(["ok"]))
    await owner_chat.say("hello")
    shown = {plan["id"]: plan for plan in plans_in(model.calls[0].system)}
    assert shown[str(plan_id)]["after_build"] == "tally"
    assert "after_build" not in shown[str(other)]
