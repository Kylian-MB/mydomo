"""A tool Domo made, in a turn and launched (Task 6, spec 5.2, 5.4, 5.7, 5.8, 6; the tools
are Domo's, 2026-11-18).

Every ready tool is offered to the model as ``t_<name>`` in anyone's turn, judged by the barrier
as ``made.<name>`` for whom it is launched, never shown, never judged by the yes-judge, and run in
the sandbox (a ``FakeSandbox`` here, which runs nothing). Nobody builds tools yet: the tests write
the tools and their kinds straight into the database.

The turns are tried through the chat pilots on a live server; ``launch`` itself is tried directly,
with the sessions of the test database."""

import asyncio
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from chat_support import ChatPilot, ChatServer
from domo_common.sandbox_api import RunRequest, RunResult
from domo_core.accounts.models import Person
from domo_core.audit.models import AuditEntry
from domo_core.channels.turns import app_turn_deps
from domo_core.confirmations.models import Confirmation
from domo_core.confirmations.service import decide_confirmation
from domo_core.conversation.prepare import made_tool_deps
from domo_core.conversation.starts import Wakes
from domo_core.household.service import disable
from domo_core.llm.types import ToolCall
from domo_core.madetools import handover
from domo_core.madetools.handover import AskingTool, launch_in_a_turn
from domo_core.madetools.launch import (
    HOUSE,
    LAUNCHED,
    SANDBOX_DOWN,
    SET_ASIDE,
    ForWhom,
    LaunchDeps,
    Launched,
    launch,
)
from domo_core.madetools.models import (
    ActionKind,
    MadeTool,
    MadeToolRun,
    MadeToolVersion,
    ToolBuild,
)
from domo_core.madetools.offered import offers
from domo_core.madetools.schema import schema_problem
from domo_core.madetools.service import fingerprint, ready_tools
from domo_core.madetools.settings import SETTINGS, set_for_tool, set_house
from domo_core.policy.actions import DescribedAction
from domo_core.policy.catalog import DEFAULT_POLICY
from domo_core.policy.facts import decision_facts
from domo_core.policy.roles import Actor, domo_actor
from domo_core.sandbox_client import NO_SANDBOX
from domo_core.tools.context import ToolContext
from domo_core.tools.registry import TOOLS
from fakes import FakeAsk, FakeChatModel, FakeSandbox, is_judge_call
from sqlalchemy import select, update
from test_madetools_yes import deciders, policy_of
from test_plan_turns import due_in, make_plan, plan_calls
from test_plan_turns import run_the_pass as run_plans

SPLIT_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "what": {"type": "string", "maxLength": 100},
        "amount": {"type": "number", "minimum": 0},
        "people": {"type": "integer", "minimum": 1},
    },
    "required": ["amount", "people"],
    "additionalProperties": False,
}
# What a call of the split gives, and what it is asked: words that must reach no audit entry.
LUNCH = {"what": "lunch at the harbour", "amount": 42.5, "people": 2}
EACH = "twenty-one point twenty-five each"
CODE = "def run(entry):\n    return None\n"
# The operation the test tools declare, and its beginning: every request of a program but a read
# follows one (Task 8).
OPERATIONS: dict[str, Any] = {"run": {"says": "Does what it does.", "risks": []}}
BEGIN: dict[str, Any] = {"do": "begin", "operation": "run"}
# How many tools a turn offers at its start (``madetools.settings``).
MAX_OFFERED = SETTINGS["tools_offered"].start


async def add_tool(
    session_factory,
    name: str,
    *,
    roles: tuple[str, ...] = ("owner", "adult", "child", "guest"),
    parameters: dict[str, Any] | None = None,
    last_launched_at: datetime | None = None,
    opened_by: Person | None = None,
) -> uuid.UUID:
    """A ready tool ``name``, its version and its kind, open to ``roles`` (by default every role,
    as a build opens it); committed. ``opened_by`` opened the build that made it."""
    async with session_factory() as writer:
        tool = MadeTool(
            name=name,
            description=f"Does {name}.",
            parameters=parameters or SPLIT_PARAMETERS,
            last_launched_at=last_launched_at,
            operations=OPERATIONS,
        )
        writer.add(tool)
        await writer.flush()
        version = MadeToolVersion(
            tool_id=tool.id, fingerprint=fingerprint(CODE), code=CODE, state="ready"
        )
        writer.add(version)
        await writer.flush()
        tool.current_version_id = version.id
        writer.add(
            ActionKind(
                action_type=f"made.{name}",
                default_roles=list(roles),
                description=f"Launch {name}.",
            )
        )
        if opened_by is not None:
            writer.add(
                ToolBuild(
                    name=name,
                    purpose="for the tests",
                    opened_by=opened_by.id,
                    tool_id=tool.id,
                    state="ready",
                )
            )
        await writer.commit()
        return tool.id


async def tool_row(session_factory, tool_id: uuid.UUID) -> MadeTool:
    async with session_factory() as reader:
        tool = await reader.get(MadeTool, tool_id)
        assert tool is not None
        return tool


async def audit_of(session_factory, *actions: str) -> list[AuditEntry]:
    async with session_factory() as reader:
        found = await reader.scalars(
            select(AuditEntry).where(AuditEntry.action.in_(actions)).order_by(AuditEntry.id)
        )
        return list(found.all())


def answers(*outputs: RunResult) -> Callable[[RunRequest], RunResult]:
    """A sandbox's answers, one per run, in order (the last one again after them)."""
    given = list(outputs)

    def run(request: RunRequest) -> RunResult:
        return given.pop(0) if len(given) > 1 else given[0]

    return run


def done(output: Any) -> RunResult:
    return RunResult(status="done", output=output, seconds=0.25)


async def offered_in_a_turn(chat: ChatPilot) -> tuple[set[str], str]:
    """The tools a turn of ``chat`` offers the model, and its system prompt."""
    first = len(chat.server.calls)
    await chat.say("hello")
    main = [call for call in chat.server.calls[first:] if call.tools]
    assert main, "the turn offered no tool"
    return {tool.name for tool in main[0].tools}, main[0].system


# --- in a turn -----------------------------------------------------------------------------------


async def test_a_tool_is_offered_to_whom_its_kind_is_open(
    owner_chat: ChatPilot,
    adult_chat: ChatPilot,
    child_chat: ChatPilot,
    guest_chat: ChatPilot,
    session_factory,
):
    await add_tool(session_factory, "tally", roles=("owner", "adult", "child"))
    await add_tool(session_factory, "welcome")
    for chat in (owner_chat, adult_chat, child_chat):
        offered, _ = await offered_in_a_turn(chat)
        assert {"t_tally", "t_welcome"} <= offered, chat.person.role
    offered, _ = await offered_in_a_turn(guest_chat)
    assert "t_welcome" in offered
    assert "t_tally" not in offered


async def test_a_call_is_run_in_the_sandbox_and_shown_nowhere(
    adult_chat: ChatPilot, chat_server: ChatServer, session_factory
):
    chat_server.sandbox = FakeSandbox(answers(done(EACH)))
    await add_tool(session_factory, "split")
    reply = await adult_chat.say_with_tool("t_split", LUNCH)
    # Stored with the reply, as any action, and read by the model in the turn...
    assert reply.actions == [{"tool": "t_split", "status": "done"}]
    assert reply.model_results == [{"status": "done", "launch": "done", "result": EACH}]
    # ... but never shown: not live, not on the page,
    assert not [event for event in reply.events if event.event == "action"]
    assert reply.cards_html == ""
    assert 'data-tool="t_split"' not in (await adult_chat.get("/chat")).text
    # never judged by the yes-judge,
    assert not [call for call in chat_server.calls if is_judge_call(call.system)]
    # and no line in the history the model reads on the next turns.
    model = adult_chat.use_model(FakeChatModel(["ok"]))
    await adult_chat.say("and now?")
    sent = json.dumps([[str(m) for m in call.messages] for call in model.calls])
    assert "made." not in sent
    assert "[action" not in sent
    # The code was given its arguments, the time and the name of whom it ran for: never an id.
    (run,) = chat_server.sandbox.runs
    assert set(run.entry) == {"arguments", "now", "for"}
    assert run.entry["arguments"] == LUNCH
    assert run.entry["for"] == "Alex"
    assert datetime.fromisoformat(str(run.entry["now"])).tzinfo is not None
    assert str(adult_chat.person.id) not in json.dumps(run.entry)
    # A turn waited for it: it went before the others.
    assert chat_server.sandbox.waited == [True]
    # The audit keeps the launch and the barrier's decision, never the arguments or the output.
    (launched,) = await audit_of(session_factory, LAUNCHED)
    assert launched.actor == f"person:{adult_chat.person.id}"
    assert set(launched.details) == {"tool", "cause", "seconds", "status", "requests", "reads"}
    assert (launched.details["requests"], launched.details["reads"]) == (0, 0)
    assert (launched.details["tool"], launched.details["cause"]) == ("split", "turn")
    assert launched.details["status"] == "done"
    audited = json.dumps(
        [entry.details for entry in await audit_of(session_factory, LAUNCHED, "policy.decision")]
    )
    for words in ("harbour", "42.5", EACH):
        assert words not in audited


async def test_a_right_that_denies_the_tool_refuses_it_and_runs_nothing(
    adult_chat: ChatPilot, chat_server: ChatServer, session_factory, grant
):
    await add_tool(session_factory, "split")
    await grant("made.split", person=adult_chat.person, access="deny")
    offered, _ = await offered_in_a_turn(adult_chat)
    assert "t_split" not in offered
    reply = await adult_chat.say_with_tool("t_split", LUNCH)
    assert reply.actions == [{"tool": "t_split", "status": "refused", "reason": "not_allowed"}]
    assert reply.cards_html == ""
    assert chat_server.sandbox.runs == []


async def test_an_owners_always_ask_on_the_tool_makes_its_call_wait_for_the_yes(
    adult_chat: ChatPilot, chat_server: ChatServer, session_factory, grant
):
    # Final review I2, spec 5.8: the owner's rule asks before the tool, as for any request.
    await add_tool(session_factory, "split")
    await grant("made.split", person=adult_chat.person, ask="explicit_yes")
    reply = await adult_chat.say_with_tool("t_split", LUNCH)
    assert reply.actions == [{"tool": "t_split", "status": "waiting"}]
    [result] = reply.model_results
    assert (result["status"], result["by"], result["asked_by"]) == (
        "waiting_for_confirmation",
        "you",
        "rule",
    )
    assert reply.pending_confirmation_id is not None
    assert chat_server.sandbox.runs == []


async def test_arguments_that_do_not_fit_the_tool_are_a_mistake_of_the_model(
    adult_chat: ChatPilot, chat_server: ChatServer, session_factory
):
    await add_tool(session_factory, "split")
    reply = await adult_chat.say_with_tool("t_split", {"amount": "a lot", "people": 2})
    assert reply.actions == [{"tool": "t_split", "status": "invalid", "reason": "invalid"}]
    assert "amount" in reply.model_results[0]["detail"]
    assert "a lot" not in reply.model_results[0]["detail"]
    assert chat_server.sandbox.runs == []


async def test_a_tool_of_domo_is_never_a_step_of_a_chain(
    adult_chat: ChatPilot, chat_server: ChatServer, session_factory
):
    await add_tool(session_factory, "split")
    reply = await adult_chat.say_with_tool(
        "do_in_order",
        {
            "steps": [
                {"tool": "household_list", "arguments": {}},
                {"tool": "t_split", "arguments": LUNCH},
            ]
        },
    )
    assert reply.actions == [{"tool": "do_in_order", "status": "invalid", "reason": "invalid"}]
    assert "step 2: a tool of your own is not a step" in reply.model_results[0]["detail"]
    # Even a name that is no tool's: no t_ step is ever planned.
    reply = await adult_chat.say_with_tool(
        "do_in_order",
        {
            "steps": [
                {"tool": "t_nothing", "arguments": {}},
                {"tool": "household_list", "arguments": {}},
            ]
        },
    )
    assert "step 1: a tool of your own is not a step" in reply.model_results[0]["detail"]
    assert chat_server.sandbox.runs == []


async def test_a_tool_set_aside_is_no_longer_offered_and_who_asked_for_it_is_told_why(
    adult_chat: ChatPilot, owner_chat: ChatPilot, chat_server: ChatServer, session_factory
):
    tool_id = await add_tool(session_factory, "split", opened_by=adult_chat.person)
    offered, system = await offered_in_a_turn(adult_chat)
    assert "t_split" in offered
    assert "set aside" not in system
    sandbox = FakeSandbox(answers(RunResult(status="error", error="ZeroDivisionError", seconds=1)))
    deps = launch_deps(session_factory, adult_chat.clock, sandbox)
    statuses = [
        (
            await launch(
                deps, tool_id, arguments=LUNCH, for_whom=ForWhom(adult_chat.person), cause="tool"
            )
        ).status
        for _ in range(3)
    ]
    assert statuses == ["failed", "failed", "set_aside"]
    offered, system = await offered_in_a_turn(adult_chat)
    assert "t_split" not in offered
    assert "Your own tools set aside" in system
    assert '"t_split"' in system and "3 times in a row" in system
    # Nobody else is told of it: they did not ask for it.
    _, system = await offered_in_a_turn(owner_chat)
    assert "t_split" not in system


# --- what is offered -----------------------------------------------------------------------------


def test_thirty_tools_at_most_are_offered_the_most_recently_used_first():
    now = datetime(2026, 11, 17, 10, 0, tzinfo=UTC)
    tools = [
        MadeTool(
            id=uuid.uuid4(),
            name=f"tool_{number:02d}",
            description="does a thing",
            parameters={"type": "object"},
            last_launched_at=now - timedelta(hours=number),
            created_at=now - timedelta(days=1),
        )
        for number in range(MAX_OFFERED + 1)
    ]
    deps = LaunchDeps(
        session_factory=None,  # type: ignore[arg-type]
        sandbox=FakeSandbox(),
        clock=lambda: now,
        policy_base=DEFAULT_POLICY,
    )
    offered = offers(list(reversed(tools)), deps, most=MAX_OFFERED)
    assert len(offered) == MAX_OFFERED == 30
    # The 31st, launched longest ago, is not offered.
    assert "t_tool_30" not in offered
    assert list(offered)[:2] == ["t_tool_00", "t_tool_01"]
    split = offered["t_tool_00"]
    assert split.action_type == "made.tool_00"
    assert split.definition.parameters == {"type": "object"}


async def test_how_many_tools_a_turn_offers_is_domo_s_setting(
    adult_chat: ChatPilot, session_factory
):
    for n in range(4):
        await add_tool(session_factory, f"many_{n}")
    offered, _ = await offered_in_a_turn(adult_chat)
    assert len([name for name in offered if name.startswith("t_")]) == 4
    async with session_factory() as writer:
        await set_house(writer, {"tools_offered": 2})
        await writer.commit()
    offered, _ = await offered_in_a_turn(adult_chat)
    assert len([name for name in offered if name.startswith("t_")]) == 2


def test_a_tool_new_or_just_changed_and_the_one_a_plan_is_told_ready_are_offered_first():
    now = datetime(2026, 11, 17, 10, 0, tzinfo=UTC)
    long_ago = now - timedelta(days=60)
    used = [
        MadeTool(
            id=uuid.uuid4(),
            name=f"used_{number:02d}",
            description="does a thing",
            parameters={"type": "object"},
            last_launched_at=now - timedelta(minutes=number + 1),
            created_at=long_ago,
            changed_at=long_ago,
        )
        for number in range(MAX_OFFERED)
    ]
    fresh = MadeTool(
        id=uuid.uuid4(),
        name="fresh",
        description="just made",
        parameters={"type": "object"},
        created_at=now,
        changed_at=now,
    )
    old = MadeTool(
        id=uuid.uuid4(),
        name="old",
        description="made long ago, never launched",
        parameters={"type": "object"},
        created_at=long_ago,
        changed_at=long_ago,
    )
    deps = LaunchDeps(
        session_factory=None,  # type: ignore[arg-type]
        sandbox=FakeSandbox(),
        clock=lambda: now,
        policy_base=DEFAULT_POLICY,
    )
    offered = offers([*used, fresh, old], deps, most=MAX_OFFERED)
    # A tool just made, never launched, is at the top; the least used is no longer offered.
    assert list(offered)[0] == "t_fresh"
    assert "t_old" not in offered and "t_used_29" not in offered
    # The tool a plan due in the turn is told is ready is always offered.
    offered = offers([*used, fresh, old], deps, most=MAX_OFFERED, first=["old"])
    assert list(offered)[:2] == ["t_old", "t_fresh"]
    assert len(offered) == MAX_OFFERED


async def changed_long_ago(session_factory, *tool_ids: uuid.UUID) -> None:
    async with session_factory() as writer:
        await writer.execute(
            update(MadeTool)
            .where(MadeTool.id.in_(tool_ids))
            .values(changed_at=datetime(2020, 1, 1, tzinfo=UTC))
        )
        await writer.commit()


async def test_past_thirty_tools_one_not_offered_still_runs_when_called(
    adult_chat: ChatPilot, chat_server: ChatServer, session_factory
):
    now = chat_server.clock()
    adult = adult_chat.person
    ids = [
        await add_tool(
            session_factory, f"used_{n:02d}", last_launched_at=now - timedelta(minutes=n + 1)
        )
        for n in range(MAX_OFFERED)
    ]
    ids.append(await add_tool(session_factory, "split", last_launched_at=now - timedelta(days=30)))
    aside = await add_tool(session_factory, "aside", opened_by=adult)
    await changed_long_ago(session_factory, *ids, aside)
    async with session_factory() as writer:
        await writer.execute(update(MadeTool).where(MadeTool.id == aside).values(state="set_aside"))
        await writer.commit()
    offered, _ = await offered_in_a_turn(adult_chat)
    assert "t_split" not in offered and "t_used_00" in offered
    # Not offered, found by tool_find: called, it runs the way an offered one does.
    chat_server.sandbox = FakeSandbox(answers(done(EACH)))
    reply = await adult_chat.say_with_tool("t_split", LUNCH)
    assert reply.actions == [{"tool": "t_split", "status": "done"}]
    assert reply.model_results == [{"status": "done", "launch": "done", "result": EACH}]
    assert reply.cards_html == ""
    assert [run.entry["arguments"] for run in chat_server.sandbox.runs] == [LUNCH]
    # One set aside is no tool at all.
    for name in ("t_aside", "t_nothing"):
        reply = await adult_chat.say_with_tool(name, LUNCH)
        assert reply.actions == [{"tool": name, "status": "invalid", "reason": "unknown_tool"}]
    assert len(chat_server.sandbox.runs) == 1
    # A tool of Domo is never a step of a chain, offered or not.
    reply = await adult_chat.say_with_tool(
        "do_in_order", {"steps": [{"tool": "t_split", "arguments": LUNCH}]}
    )
    assert reply.actions == [{"tool": "do_in_order", "status": "invalid", "reason": "invalid"}]
    assert len(chat_server.sandbox.runs) == 1


class MakesATool(FakeChatModel):
    """A model whose first main call, before it streams, runs ``make``: a tool made while the
    turn runs, after its catalog was read."""

    def __init__(self, make: Callable[[], Any], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._make: Callable[[], Any] | None = make

    async def stream(self, **kwargs: Any) -> Any:  # type: ignore[override]
        if self._make is not None and kwargs.get("tools") and not is_judge_call(kwargs["system"]):
            make, self._make = self._make, None
            await make()
        async for item in super().stream(**kwargs):
            yield item


async def test_a_tool_made_after_the_turn_read_its_catalog_is_refused_when_called(
    adult_chat: ChatPilot, chat_server: ChatServer, session_factory
):
    """Its kind ``made.split`` is not in the catalog the turn read: the barrier refuses it,
    nothing runs (the next turn reads it, and offers the tool)."""

    async def make() -> None:
        await add_tool(session_factory, "split")

    model = MakesATool(make, tool_script=[ToolCall("c1", "t_split", json.dumps(LUNCH)), "ok"])
    adult_chat.use_model(model)
    reply = await adult_chat.say("split the lunch")
    assert reply.actions == [{"tool": "t_split", "status": "refused", "reason": "not_allowed"}]
    assert chat_server.sandbox.runs == []
    offered, _ = await offered_in_a_turn(adult_chat)
    assert "t_split" in offered


async def test_a_plan_told_its_tool_is_ready_is_offered_that_tool(
    adult_chat: ChatPilot, chat_server: ChatServer, session_factory
):
    now = chat_server.clock()
    adult = adult_chat.person
    ids = [
        await add_tool(
            session_factory, f"used_{n:02d}", last_launched_at=now - timedelta(minutes=n + 1)
        )
        for n in range(MAX_OFFERED)
    ]
    old = await add_tool(
        session_factory, "split", last_launched_at=now - timedelta(days=30), opened_by=adult
    )
    await changed_long_ago(session_factory, *ids, old)
    async with session_factory() as reader:
        build = await reader.scalar(select(ToolBuild).where(ToolBuild.tool_id == old))
    assert build is not None
    await make_plan(adult_chat, "split the lunch", after=None, after_build_id=build.id)
    adult_chat.use_model(FakeChatModel(["Done."]))
    await run_plans(chat_server)
    [call] = plan_calls(chat_server)
    assert [plan["tool"] for plan in due_in(call.system)] == ["split"]
    assert "t_split" in {tool.name for tool in call.tools}


@pytest.mark.parametrize(
    ("arguments", "problem"),
    [
        ({"amount": 3, "people": 2}, None),
        ({"amount": 3}, "people is missing"),
        ({"amount": 3, "people": 2, "tip": 1}, "the tool takes only"),
        ({"amount": -1, "people": 2}, "amount: too small"),
        ({"amount": 3, "people": 2.5}, "people: must be of type integer"),
        ({"amount": 3, "people": 2, "what": "x" * 101}, "what: too long"),
    ],
)
def test_the_arguments_are_checked_against_the_tool_s_schema(arguments, problem):
    found = schema_problem(SPLIT_PARAMETERS, arguments)
    if problem is None:
        assert found is None
    else:
        assert found is not None and problem in found


# --- launching -----------------------------------------------------------------------------------


def launch_deps(session_factory, clock, sandbox, wakes=None) -> LaunchDeps:
    return LaunchDeps(
        session_factory=session_factory,
        sandbox=sandbox,
        clock=clock,
        policy_base=DEFAULT_POLICY,
        wakes=wakes,
    )


@pytest.fixture
async def alex(add_member) -> Person:
    await add_member("Dana", "owner")
    return await add_member("Alex", "adult")


@pytest.fixture
async def sam(add_member) -> Person:
    return await add_member("Sam", "child")


async def test_a_tool_runs_for_whoever_it_is_launched_for(session_factory, live_clock, alex, sam):
    sandbox = FakeSandbox(answers(done(1), done({})))
    tool_id = await add_tool(session_factory, "tally")
    deps = launch_deps(session_factory, live_clock, sandbox)
    for person in (alex, sam):
        launched = await launch(
            deps, tool_id, arguments=LUNCH, for_whom=ForWhom(person), cause="turn"
        )
        assert launched.status == "done"
    assert [run.entry["for"] for run in sandbox.runs] == ["Alex", "Sam"]
    # A turn judged each call already: the launch does not judge it again.
    assert await audit_of(session_factory, "policy.decision") == []


async def test_a_right_that_denies_the_tool_refuses_a_launch_outside_a_turn(
    session_factory, live_clock, alex, grant
):
    sandbox = FakeSandbox()
    tool_id = await add_tool(session_factory, "tally")
    await grant("made.tally", person=alex, access="deny")
    deps = launch_deps(session_factory, live_clock, sandbox)
    refused = await launch(deps, tool_id, arguments=LUNCH, for_whom=ForWhom(alex), cause="tool")
    assert refused == Launched("refused", {"reason": "not_allowed"})
    assert sandbox.runs == []
    (decision,) = await audit_of(session_factory, "policy.decision")
    assert decision.details["decision"] == "refuse"
    assert await audit_of(session_factory, LAUNCHED) == []


async def test_a_sandbox_that_does_not_answer_is_no_failure_of_the_tool(
    session_factory, live_clock, alex
):
    tool_id = await add_tool(session_factory, "tally")
    deps = launch_deps(session_factory, live_clock, NO_SANDBOX)
    async with session_factory() as writer:
        await writer.execute(
            update(MadeTool)
            .where(MadeTool.id == tool_id)
            .values(launches_day=live_clock().date(), launches_today=199)
        )
        await writer.commit()
    for _ in range(4):
        launched = await launch(
            deps, tool_id, arguments=LUNCH, for_whom=ForWhom(alex), cause="turn"
        )
        assert launched == Launched("failed", {"reason": SANDBOX_DOWN})
    tool = await tool_row(session_factory, tool_id)
    assert (tool.state, tool.failures_in_a_row) == ("ready", 0)
    # Each launch gave its count of the day back: an outage never reaches the cap of 200.
    assert tool.launches_today == 199
    assert await audit_of(session_factory, SET_ASIDE) == []


async def test_three_failures_in_a_row_set_the_tool_aside_and_a_success_resets_them(
    session_factory, live_clock, alex
):
    error = RunResult(status="error", error="KeyError: 'amount'", seconds=0.1)
    sandbox = FakeSandbox(
        answers(
            error,
            RunResult(status="timeout", seconds=60),
            done(1),
            done("x" * 70_000),
            RunResult(status="too_big", seconds=1),
            error,
        )
    )
    tool_id = await add_tool(session_factory, "tally")
    deps = launch_deps(session_factory, live_clock, sandbox)
    came = [
        await launch(deps, tool_id, arguments=LUNCH, for_whom=ForWhom(alex), cause="turn")
        for _ in range(6)
    ]
    assert [launched.status for launched in came] == [
        "failed",
        "failed",
        "done",
        "failed",
        "failed",
        "set_aside",
    ]
    # Domo reads why a launch failed, with the last line of the error.
    assert came[0].result == {"reason": "it stopped on an error", "error": "KeyError: 'amount'"}
    tool = await tool_row(session_factory, tool_id)
    assert tool.state == "set_aside"
    assert tool.aside_reason is not None and "3 times in a row" in tool.aside_reason
    (aside,) = await audit_of(session_factory, SET_ASIDE)
    assert aside.details == {"tool": "tally", "reason": tool.aside_reason}
    # Set aside, it is not launched any more.
    again = await launch(deps, tool_id, arguments=LUNCH, for_whom=ForWhom(alex), cause="turn")
    assert again.status == "set_aside"
    assert len(sandbox.runs) == 6


async def test_a_run_that_could_not_be_stopped_sets_the_tool_aside_at_once(
    session_factory, live_clock, alex
):
    sandbox = FakeSandbox(answers(RunResult(status="killed", seconds=3700)))
    tool_id = await add_tool(session_factory, "tally")
    deps = launch_deps(session_factory, live_clock, sandbox)
    launched = await launch(deps, tool_id, arguments=LUNCH, for_whom=ForWhom(alex), cause="turn")
    assert launched.status == "set_aside"
    assert (await tool_row(session_factory, tool_id)).state == "set_aside"


async def test_a_run_the_machine_ended_is_a_failure_like_any_told_with_memory_and_places(
    session_factory, live_clock, alex
):
    # Final review I4, ruling R32: never set aside at once as one that could not be stopped.
    note = "The machine ended the tool (a signal the sandbox did not send)."
    sandbox = FakeSandbox(answers(RunResult(status="ended_by_machine", note=note, seconds=2.0)))
    tool_id = await add_tool(session_factory, "tally")
    deps = launch_deps(session_factory, live_clock, sandbox)
    launched = await launch(deps, tool_id, arguments=LUNCH, for_whom=ForWhom(alex), cause="turn")
    assert launched.status == "failed" and isinstance(launched.result, dict)
    assert (launched.result["setting"], launched.result["value"]) == ("memory_mb", 256)
    reason = str(launched.result["reason"])
    assert "the machine ended it" in reason and "memory_mb is 256" in reason
    assert "places is 4" in reason and "could not be stopped" not in reason
    assert launched.result["note"] == note and "error" not in launched.result
    row = await tool_row(session_factory, tool_id)
    assert (row.state, row.failures_in_a_row) == ("ready", 1)


async def test_a_tool_launched_too_often_in_a_day_is_set_aside(session_factory, live_clock, alex):
    sandbox = FakeSandbox()
    tool_id = await add_tool(session_factory, "tally")
    async with session_factory() as writer:
        await writer.execute(
            update(MadeTool)
            .where(MadeTool.id == tool_id)
            .values(launches_day=live_clock().date(), launches_today=200)
        )
        await writer.commit()
    deps = launch_deps(session_factory, live_clock, sandbox)
    launched = await launch(deps, tool_id, arguments=LUNCH, for_whom=ForWhom(alex), cause="turn")
    assert launched.status == "set_aside"
    assert sandbox.runs == []


async def test_what_a_tool_gives_back_is_its_result_itself(session_factory, live_clock, alex):
    old_envelope = {"result": 2, "requests": [{"do": "wake", "content": "x"}]}
    sandbox = FakeSandbox(answers(done([1, "two"]), done(old_envelope), done("x" * 70_000)))
    tool_id = await add_tool(session_factory, "tally")
    deps = launch_deps(session_factory, live_clock, sandbox)
    came = [
        await launch(deps, tool_id, arguments=LUNCH, for_whom=ForWhom(alex), cause="turn")
        for _ in range(3)
    ]
    # Any JSON value is its result; what looks like the old envelope is a result like any other,
    # and nothing in it is carried out.
    assert came[0] == Launched("done", [1, "two"])
    assert came[1] == Launched("done", old_envelope)
    # More than its result_bytes (64 KB at the start): the tool failed, told with the setting.
    assert came[2].status == "failed"
    assert came[2].result == {
        "reason": (
            "what it gave back is bigger than 65536 bytes; result_bytes is 65536, a setting of "
            "yours: you may change it (tools_settings, or tool_set for one tool)"
        ),
        "setting": "result_bytes",
        "value": 65536,
    }


async def test_a_long_run_a_turn_waits_on_goes_on_in_the_background(
    session_factory, live_clock, alex, monkeypatch
):
    monkeypatch.setattr(handover, "HAND_OVER_SECONDS", 0.05)

    async def late(request: RunRequest, ask: FakeAsk) -> Any:
        await ask(BEGIN)
        await ask({"do": "wake", "content": "late"})
        return "late"

    sandbox = FakeSandbox(program=late)
    sandbox.gate = asyncio.Event()
    tool_id = await add_tool(session_factory, "tally")
    tool = AskingTool.of(await tool_row(session_factory, tool_id))
    wakes = Wakes()
    deps = launch_deps(session_factory, live_clock, sandbox, wakes)
    launched = await launch_in_a_turn(deps, tool, arguments=LUNCH, for_person=alex)
    assert launched == Launched("running", None)
    sandbox.gate.set()
    async with asyncio.timeout(5):
        while wakes.pending:
            await asyncio.sleep(0.01)
    async with session_factory() as reader:
        (run,) = (await reader.scalars(select(MadeToolRun))).all()
    assert (run.tool_id, run.person_id, run.status) == (tool_id, alex.id, "done")
    # What its program asked was answered while it ran; the row holds what came of it, the wake
    # it asked for whom it ran for among it, and was delivered at once (nobody to wake in this
    # test).
    assert run.result == "late"
    assert run.also == [{"tool": "tally", "content": "late"}]
    assert run.finished_at is not None and run.delivered_at is not None
    assert sandbox.waited == [True]
    # A short one is waited for, and nothing is kept for later.
    sandbox.gate = None
    monkeypatch.setattr(handover, "HAND_OVER_SECONDS", 10.0)
    quick = await launch_in_a_turn(deps, tool, arguments=LUNCH, for_person=alex)
    assert quick.status == "done"
    async with session_factory() as reader:
        assert len((await reader.scalars(select(MadeToolRun))).all()) == 1


async def test_parameters_the_core_cannot_check_are_the_tool_s_fault(
    session_factory, live_clock, alex
):
    sandbox = FakeSandbox()
    tool_id = await add_tool(
        session_factory,
        "tally",
        parameters={"type": "object", "properties": {"code": {"type": "string", "pattern": "^a"}}},
    )
    deps = launch_deps(session_factory, live_clock, sandbox)
    came = [
        await launch(deps, tool_id, arguments={"code": "zzz"}, for_whom=ForWhom(alex), cause="turn")
        for _ in range(3)
    ]
    # Nothing runs, and each launch counts against the tool, not the model's call.
    assert [launched.status for launched in came] == ["failed", "failed", "set_aside"]
    assert came[0].result is not None and "code.pattern" in str(came[0].result)
    assert sandbox.runs == []
    tool = await tool_row(session_factory, tool_id)
    assert tool.state == "set_aside"
    assert [entry.details["status"] for entry in await audit_of(session_factory, LAUNCHED)] == [
        "failed",
        "failed",
        "set_aside",
    ]


async def test_a_tool_whose_parameters_the_core_cannot_check_fails_in_a_turn_as_its_own_fault(
    adult_chat: ChatPilot, chat_server: ChatServer, session_factory
):
    tool_id = await add_tool(
        session_factory,
        "split",
        parameters={"type": "object", "anyOf": [{"required": ["amount"]}]},
    )
    reply = await adult_chat.say_with_tool("t_split", {"anything": 1})
    # Not the model's mistake (no invalid): the tool failed, and it is counted.
    assert reply.actions == [{"tool": "t_split", "status": "failed"}]
    assert reply.model_results[0]["launch"] == "failed"
    assert chat_server.sandbox.runs == []
    assert (await tool_row(session_factory, tool_id)).failures_in_a_row == 1


# --- a tool is Domo's; each launch is for someone ----------------------------------------------


async def test_every_ready_tool_is_offered_to_everyone_a_guest_too(
    adult_chat: ChatPilot, child_chat: ChatPilot, guest_chat: ChatPilot, session_factory
):
    # Made in the adult's turn, with the kind a build opens: Domo's, offered to everyone.
    await add_tool(session_factory, "split", opened_by=adult_chat.person)
    for chat in (adult_chat, child_chat, guest_chat):
        offered, _ = await offered_in_a_turn(chat)
        assert "t_split" in offered, chat.person.role


async def test_the_owner_closes_a_tool_to_someone(
    adult_chat: ChatPilot, child_chat: ChatPilot, chat_server: ChatServer, session_factory, grant
):
    await add_tool(session_factory, "split", opened_by=adult_chat.person)
    await grant("made.split", person=child_chat.person, access="deny")
    chat_server.sandbox = FakeSandbox(answers(done(EACH)))
    reply = await child_chat.say_with_tool("t_split", LUNCH)
    assert reply.actions == [{"tool": "t_split", "status": "refused", "reason": "not_allowed"}]
    assert chat_server.sandbox.runs == []
    reply = await adult_chat.say_with_tool("t_split", LUNCH)
    assert reply.actions == [{"tool": "t_split", "status": "done"}]
    assert [run.entry["for"] for run in chat_server.sandbox.runs] == ["Alex"]


async def disabled(session_factory, person: Person, at: datetime) -> None:
    async with session_factory() as writer:
        await writer.execute(update(Person).where(Person.id == person.id).values(disabled_at=at))
        await writer.commit()


async def test_a_launch_for_someone_who_left_is_refused(session_factory, live_clock, alex):
    sandbox = FakeSandbox()
    tool_id = await add_tool(session_factory, "split")
    await disabled(session_factory, alex, live_clock())
    deps = launch_deps(session_factory, live_clock, sandbox)
    for cause in ("tool", "trigger", "turn"):
        refused = await launch(deps, tool_id, arguments=LUNCH, for_whom=ForWhom(alex), cause=cause)
        assert refused == Launched("refused", {"reason": "not_allowed"})
    assert sandbox.runs == []
    assert await audit_of(session_factory, LAUNCHED) == []


async def test_the_entry_holds_arguments_now_and_for_only(session_factory, live_clock, alex):
    sandbox = FakeSandbox(answers(done(1)))
    tool_id = await add_tool(session_factory, "split")
    deps = launch_deps(session_factory, live_clock, sandbox)
    for_alex = await launch(deps, tool_id, arguments=LUNCH, for_whom=ForWhom(alex), cause="tool")
    for_the_house = await launch(deps, tool_id, arguments=LUNCH, for_whom=HOUSE, cause="trigger")
    assert for_alex == for_the_house == Launched("done", 1)
    assert [set(run.entry) for run in sandbox.runs] == [{"arguments", "now", "for"}] * 2
    assert [run.entry["for"] for run in sandbox.runs] == ["Alex", None]
    # For the house, Domo's own rights were judged: the audit says it was his.
    decisions = await audit_of(session_factory, "policy.decision")
    assert [entry.actor for entry in decisions] == [f"person:{alex.id}", "domo"]
    assert [entry.actor for entry in await audit_of(session_factory, LAUNCHED)] == [
        f"person:{alex.id}",
        "domo",
    ]


async def test_launches_per_day_is_the_tools_own(session_factory, live_clock, alex):
    sandbox = FakeSandbox()
    tool_id = await add_tool(session_factory, "split")
    async with session_factory() as writer:
        row = await writer.get(MadeTool, tool_id)
        assert row is not None
        await set_for_tool(writer, row, {"launches_per_day": 3})
        await writer.commit()
    deps = launch_deps(session_factory, live_clock, sandbox)
    came = [
        (await launch(deps, tool_id, arguments=LUNCH, for_whom=ForWhom(alex), cause="turn")).status
        for _ in range(4)
    ]
    assert came == ["done", "done", "done", "set_aside"]
    assert len(sandbox.runs) == 3
    tool = await tool_row(session_factory, tool_id)
    assert tool.state == "set_aside" and tool.launches_today == 3


async def test_a_departure_sets_no_tool_aside(session_factory, live_clock, alex):
    tool_id = await add_tool(session_factory, "split", opened_by=alex)
    async with session_factory() as writer:
        leaving = await writer.get(Person, alex.id)
        assert leaving is not None
        await disable(writer, leaving, live_clock(), by=alex.id)
        await writer.commit()
    assert (await tool_row(session_factory, tool_id)).state == "ready"
    async with session_factory() as reader:
        assert [tool.name for tool in await ready_tools(reader)] == ["split"]
    assert await audit_of(session_factory, SET_ASIDE) == []


# --- what Domo does where nobody asked (Task 9, spec 5.1, 5.8) ---------------------------------

PURPOSE = "Keep a running total of the amounts it is given and give the total back."
BUILD: dict[str, Any] = {
    "name": "tally",
    "purpose": PURPOSE,
    "examples": [{"entry": {"amount": 2}, "expected": {"total": 2}}],
}


async def decisions_on(session_factory, action_type: str) -> list[AuditEntry]:
    return [
        entry
        for entry in await audit_of(session_factory, "policy.decision")
        if entry.details["action_type"] == action_type
    ]


async def builds(session_factory) -> list[ToolBuild]:
    async with session_factory() as reader:
        return list((await reader.scalars(select(ToolBuild))).all())


async def test_the_owner_stops_domo_making_tools(adult_chat: ChatPilot, session_factory, grant):
    # Making a tool is Domo's own act: the owner stops it with a right of Domo's.
    await grant("tool.build", for_domo=True, access="deny")
    reply = await adult_chat.say_with_tool("tool_build", BUILD)
    assert reply.actions == [{"tool": "tool_build", "status": "refused", "reason": "not_allowed"}]
    assert await builds(session_factory) == []
    [decided] = await decisions_on(session_factory, "tool.build")
    assert (decided.actor, decided.details["decision"]) == ("domo", "refuse")


async def test_dana_denied_tool_build_still_gets_one_built(
    adult_chat: ChatPilot, session_factory, grant
):
    # A person's right on making a tool changes nothing: it is Domo's act, not theirs.
    await grant("tool.build", person=adult_chat.person, access="deny")
    reply = await adult_chat.say_with_tool("tool_build", BUILD)
    assert reply.actions == [{"tool": "tool_build", "status": "done"}]
    [build] = await builds(session_factory)
    # The cost of the build follows the key of the turn that opened it, as before.
    assert build.opened_by == adult_chat.person.id
    [decided] = await decisions_on(session_factory, "tool.build")
    assert (decided.actor, decided.details["decision"]) == ("domo", "allow")


def wakes_once(then: Any = None) -> Any:
    """A program that wakes Domo on its first run only (then gives back ``then``)."""
    runs: list[int] = []

    async def program(request: RunRequest, ask: Any) -> Any:
        await ask(BEGIN)
        runs.append(1)
        if len(runs) == 1:
            await ask({"do": "wake", "content": {"seen": "a parcel at the door"}})
        return then

    return program


async def woken_for_the_house(chat_server: ChatServer, tool_id: uuid.UUID) -> None:
    """``tool_id`` launched by a trigger for the house, with the application's own deps: its
    program wakes Domo, in the owner's conversation, on his own; waited for until that turn
    ended."""
    deps = made_tool_deps(app_turn_deps(chat_server.app))
    await launch(deps, tool_id, arguments=LUNCH, for_whom=HOUSE, cause="trigger")
    await chat_server.wait_for_wakes()


async def test_a_woken_house_turn_acts_with_domo_rights(
    chat_server: ChatServer, owner_chat: ChatPilot, session_factory, grant, revoke_right
):
    # Fix round 1 (R17): an act Domo has by default in a house turn, the owner's too: reading
    # who is in the household. The owner's right of Domo's refuses it; without it, it passes.
    tool_id = await add_tool(session_factory, "split")
    chat_server.sandbox = FakeSandbox(program=wakes_once())
    denied = await grant("household.list", for_domo=True, access="deny")
    look = ToolCall("h1", "household_list", "{}")
    owner_chat.use_model(FakeChatModel(tool_script=[look, "Done."], judge="do"))
    await woken_for_the_house(chat_server, tool_id)
    # Refused although the owner may: nobody asked, so it is judged with Domo's own rights.
    [decided] = await decisions_on(session_factory, "household.list")
    assert (decided.actor, decided.details["decision"], decided.details["reason"]) == (
        "domo",
        "refuse",
        "not_allowed",
    )
    # Without the owner's right, Domo does it on his own.
    await revoke_right(denied)
    chat_server.sandbox = FakeSandbox(program=wakes_once())
    owner_chat.use_model(FakeChatModel(tool_script=[look, "Done."], judge="do"))
    await woken_for_the_house(chat_server, tool_id)
    _, allowed = await decisions_on(session_factory, "household.list")
    assert (allowed.actor, allowed.details["decision"]) == ("domo", "allow")
    # The owner, in a turn of theirs, may.
    reply = await owner_chat.say_with_tool("household_list", {})
    assert reply.actions == [{"tool": "household_list", "status": "done"}]


# A note Domo leaves on his own is his own (the owner, 2026-11-21): ``test_notes_of_domo.py``.


async def test_a_launch_in_a_woken_house_turn_is_for_the_house(
    chat_server: ChatServer, owner_chat: ChatPilot, session_factory
):
    tool_id = await add_tool(session_factory, "split")
    chat_server.sandbox = FakeSandbox(program=wakes_once(then=EACH))
    call = ToolCall("s1", "t_split", json.dumps(LUNCH))
    owner_chat.use_model(FakeChatModel(tool_script=[call, "Split."]))
    await woken_for_the_house(chat_server, tool_id)
    trigger, in_the_turn = chat_server.sandbox.runs
    # Launched in the owner's conversation, but by Domo on his own: for the house, no name.
    assert trigger.entry["for"] is None
    assert in_the_turn.entry["for"] is None
    launched = await audit_of(session_factory, LAUNCHED)
    assert [entry.actor for entry in launched] == ["domo", "domo"]
    # Judged once each: the trigger's launch by itself, the turn's call by the turn.
    decided = await decisions_on(session_factory, "made.split")
    assert [(entry.actor, entry.details["decision"]) for entry in decided] == [
        ("domo", "allow"),
        ("domo", "allow"),
    ]


# --- fix round 1: a request Domo makes on his own is his (R16, R17) -----------------------------


async def requests_made(session_factory) -> list[Confirmation]:
    async with session_factory() as reader:
        found = await reader.scalars(select(Confirmation).order_by(Confirmation.created_at))
        return list(found.all())


async def house_turn_asks(
    chat_server: ChatServer, owner_chat: ChatPilot, session_factory, calls: int
) -> list[Confirmation]:
    """A house trigger of ``bell`` wakes Domo on his own; he calls ``t_split`` ``calls`` times,
    each of which waits for a yes: the requests made."""
    bell = await add_tool(session_factory, "bell")
    chat_server.sandbox = FakeSandbox(program=wakes_once(then=EACH))
    split = [ToolCall(f"s{i}", "t_split", json.dumps(LUNCH)) for i in range(calls)]
    owner_chat.use_model(FakeChatModel(tool_script=[*split, "Asked."], judge="do"))
    await woken_for_the_house(chat_server, bell)
    made = await requests_made(session_factory)
    assert [c.status for c in made] == ["pending"] * calls
    return made


async def decided_by(
    chat_server: ChatServer, session_factory, tool_context: ToolContext, c: Confirmation, who
) -> Any:
    deps = made_tool_deps(app_turn_deps(chat_server.app))
    async with session_factory() as reader:
        decider = await reader.get(Person, who.id)
    outcome, _ = await decide_confirmation(
        session_factory=session_factory,
        confirmation_id=c.id,
        decider=decider,
        approve=True,
        via="button",
        reply_message_id=None,
        ctx_factory=deciders(tool_context, deps, session_factory),
        registry=TOOLS,
        policy=await policy_of(session_factory),
        now=chat_server.clock(),
    )
    return outcome


async def test_a_launch_waiting_in_a_house_turn_runs_for_the_house_on_the_owners_yes(
    chat_server: ChatServer,
    owner_chat: ChatPilot,
    adult_chat: ChatPilot,
    session_factory,
    grant,
    tool_context: ToolContext,
):
    await add_tool(session_factory, "split")
    # The owner wants to be asked before Domo splits anything on his own.
    await grant("made.split", for_domo=True, approver="owner")
    [asked] = await house_turn_asks(chat_server, owner_chat, session_factory, 1)
    # Domo's request: the owner answers it, nobody else, and Domo never.
    assert (asked.by_domo, asked.approver_kind) == (True, "owner")
    assert await decided_by(
        chat_server, session_factory, tool_context, asked, adult_chat.person
    ) == ("not_yours")
    assert await decided_by(
        chat_server, session_factory, tool_context, asked, owner_chat.person
    ) == ("executed")
    # Run as his, for the house: no name, and every decision on it is his.
    assert [run.entry["for"] for run in chat_server.sandbox.runs] == [None, None]
    decided = await decisions_on(session_factory, "made.split")
    assert {entry.actor for entry in decided} == {"domo"}
    launched = await audit_of(session_factory, LAUNCHED)
    assert [entry.actor for entry in launched] == ["domo", "domo"]
    # Counted once, in the house's history, never in the owner's.
    async with session_factory() as reader:
        action = DescribedAction("core", "made.split", None, "self_profile", "household")
        now = chat_server.clock()
        his = await decision_facts(reader, domo_actor(), action, now, None)
        owner = Actor(owner_chat.person.id, "owner")
        theirs = await decision_facts(reader, owner, action, now, None)
    assert his["history"] == {"day": 1, "week": 1, "month": 1}
    assert theirs["history"] == {"day": 0, "week": 0, "month": 0}


async def test_a_house_request_for_an_adult_is_answered_by_an_adult_or_the_owner(
    chat_server: ChatServer,
    owner_chat: ChatPilot,
    adult_chat: ChatPilot,
    session_factory,
    grant,
    tool_context: ToolContext,
):
    await add_tool(session_factory, "split")
    await grant("made.split", for_domo=True, approver="adult")
    first, second = await house_turn_asks(chat_server, owner_chat, session_factory, 2)
    assert (first.approver_kind, second.approver_kind) == ("adult", "adult")
    # Another adult answers one, the owner the other: each runs for the house.
    assert await decided_by(
        chat_server, session_factory, tool_context, first, adult_chat.person
    ) == ("executed")
    assert await decided_by(
        chat_server, session_factory, tool_context, second, owner_chat.person
    ) == ("executed")
    assert [run.entry["for"] for run in chat_server.sandbox.runs] == [None, None, None]


async def test_a_tool_opened_on_his_own_names_nobody(
    chat_server: ChatServer, owner_chat: ChatPilot, session_factory
):
    tool_id = await add_tool(session_factory, "split")
    chat_server.sandbox = FakeSandbox(program=wakes_once())
    build = ToolCall("b1", "tool_build", json.dumps(BUILD))
    owner_chat.use_model(FakeChatModel(tool_script=[build, "Done."]))
    await woken_for_the_house(chat_server, tool_id)
    [opened] = await builds(session_factory)
    # The house's, not the owner's: nobody opened it in their turn.
    assert opened.opened_by is None
