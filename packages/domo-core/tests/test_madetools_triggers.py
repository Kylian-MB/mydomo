"""A tool Domo made that launches by itself (Task 10, spec 5.6, 5.7, 5.8; for someone, a person
or the house, 2026-11-18).

The upkeep's pass (``triggers_pass``) fires the time triggers due, claimed and moved to their next
time before the launch, and the arrival triggers someone's joining fires; the return triggers fire
when the person comes back after an absence (``conversation.starts.came_back``). The launches run
on a live server whose sandbox executes nothing (``fakes.FakeSandbox``); each tool's code starts
with its name, so that the runs say which tool ran.
"""

import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from chat_support import ChatPilot, ChatServer
from domo_common.sandbox_api import RunRequest
from domo_core.accounts.models import Person
from domo_core.audit.models import AuditEntry
from domo_core.channels.turns import app_turn_deps
from domo_core.conversation.prepare import made_tool_deps
from domo_core.conversation.visits import ArrivalTurns
from domo_core.household.models import Invitation
from domo_core.madetools import triggers
from domo_core.madetools.launch import LAUNCHED
from domo_core.madetools.models import ActionKind, MadeTool, MadeToolTrigger, ToolBuild
from domo_core.madetools.service import set_aside
from domo_core.madetools.triggers import TRIGGERS_PER_PASS, triggers_pass
from domo_core.policy.models import PolicyRight
from domo_core.sandbox_client import SandboxUnavailable
from domo_core.web import app as web_app
from fakes import FakeAsk, FakeChatModel, FakeSandbox
from pydantic import JsonValue
from sqlalchemy import select, update
from test_arrival import open_page
from test_madetools_handover import (
    BEGIN,
    add_tool,
    by_program,
    by_tool,
    data_lines,
    ran,
    tool_calls,
    waking,
)

LATER = timedelta(minutes=5)


async def add_trigger(server: ChatServer, tool: MadeTool, **values: Any) -> uuid.UUID:
    """A waiting trigger of ``tool``, set now; committed."""
    async with server.session_factory() as session:
        trigger = MadeToolTrigger(tool_id=tool.id, created_at=server.clock(), **values)
        session.add(trigger)
        await session.commit()
        return trigger.id


async def trigger_row(server: ChatServer, trigger_id: uuid.UUID) -> MadeToolTrigger:
    async with server.session_factory() as session:
        found = await session.get(MadeToolTrigger, trigger_id)
    assert found is not None
    return found


async def run_the_pass(server: ChatServer, one_pass: Any = None) -> None:
    """One pass of the triggers at the server's time, and the launches it started to their end."""
    await (one_pass or triggers_pass(app_turn_deps(server.app)))(server.clock())
    await server.wait_for_wakes()


async def launches(server: ChatServer) -> list[dict[str, Any]]:
    async with server.session_factory() as session:
        found = await session.scalars(
            select(AuditEntry).where(AuditEntry.action == LAUNCHED).order_by(AuditEntry.id)
        )
        return [{"actor": e.actor, **e.details} for e in found.all()]


# --- at a time -----------------------------------------------------------------------------------


async def test_a_time_trigger_launches_once_at_its_time_and_its_rule_moves_it_on(
    owner_chat: ChatPilot,
):
    server = owner_chat.server
    tool = await add_tool(server.session_factory, "tally")
    # A rule's times fall on whole minutes.
    due = (server.clock() + LATER).replace(second=0, microsecond=0)
    trigger_id = await add_trigger(
        server,
        tool,
        kind="time",
        due_at=due,
        repeat="FREQ=DAILY",
        for_person_id=owner_chat.person.id,
        arguments={"n": 2},
    )
    await run_the_pass(server)
    assert ran(server.sandbox) == []

    server.clock.advance(LATER)
    one_pass = triggers_pass(app_turn_deps(server.app))
    await run_the_pass(server, one_pass)
    # A second pass at the same time, or a restart, never fires the same time twice.
    await run_the_pass(server, one_pass)
    await run_the_pass(server)
    assert ran(server.sandbox) == ["tally"]
    [request] = server.sandbox.runs
    assert request.entry["arguments"] == {"n": 2}
    assert request.entry["for"] == owner_chat.person.display_name
    # Never ahead of the launches a turn waits on.
    assert server.sandbox.waited == [False]
    trigger = await trigger_row(server, trigger_id)
    assert (trigger.state, trigger.due_at, trigger.last_fired_at) == (
        "waiting",
        due + timedelta(days=1),
        server.clock(),
    )
    # Judged for its person and counted: one launch of the day, by a trigger.
    [launched] = await launches(server)
    assert (launched["actor"], launched["cause"], launched["status"]) == (
        f"person:{owner_chat.person.id}",
        "trigger",
        "done",
    )

    # Stopped for three days: the times missed fire once, at the next pass.
    server.clock.advance(timedelta(days=3))
    await run_the_pass(server)
    assert ran(server.sandbox) == ["tally", "tally"]
    trigger = await trigger_row(server, trigger_id)
    assert trigger.due_at is not None and trigger.due_at > server.clock()


async def test_a_time_trigger_without_a_rule_is_done_once_fired(owner_chat: ChatPilot):
    server = owner_chat.server
    tool = await add_tool(server.session_factory, "tally")
    trigger_id = await add_trigger(
        server, tool, kind="time", due_at=server.clock() + LATER, for_person_id=owner_chat.person.id
    )
    server.clock.advance(LATER)
    await run_the_pass(server)
    await run_the_pass(server)
    assert ran(server.sandbox) == ["tally"]
    assert (await trigger_row(server, trigger_id)).state == "done"


async def test_what_a_triggered_tool_asks_is_answered_while_it_runs(owner_chat: ChatPilot):
    server = owner_chat.server

    async def first_program(request: RunRequest, ask: FakeAsk) -> JsonValue:
        await ask(BEGIN)
        return await ask({"do": "tool", "name": "second", "arguments": {}})

    server.sandbox = by_program(first=first_program)
    first = await add_tool(server.session_factory, "first")
    await add_tool(server.session_factory, "second")
    await add_trigger(
        server,
        first,
        kind="time",
        due_at=server.clock() + LATER,
        for_person_id=owner_chat.person.id,
    )
    server.clock.advance(LATER)
    await run_the_pass(server)
    # The tool it asked for was launched too, for the same person, in its chain.
    assert ran(server.sandbox) == ["first", "second"]
    assert [run.entry["for"] for run in server.sandbox.runs] == [owner_chat.person.display_name] * 2


async def test_a_tool_set_aside_keeps_its_triggers_paused_until_it_is_repaired(
    owner_chat: ChatPilot,
):
    server = owner_chat.server
    tool = await add_tool(server.session_factory, "tally")
    due = (server.clock() + LATER).replace(second=0, microsecond=0)
    trigger_id = await add_trigger(
        server,
        tool,
        kind="time",
        due_at=due,
        repeat="FREQ=DAILY",
        for_person_id=owner_chat.person.id,
    )
    async with server.session_factory() as session:
        row = await session.get(MadeTool, tool.id)
        assert row is not None
        await set_aside(session, row, server.clock(), reason="it failed")
        await session.commit()
    # Set aside: no launch, and its trigger neither fired, moved nor cancelled.
    server.clock.advance(timedelta(days=2))
    await run_the_pass(server)
    assert ran(server.sandbox) == []
    trigger = await trigger_row(server, trigger_id)
    assert (trigger.state, trigger.due_at, trigger.last_fired_at) == ("waiting", due, None)
    # Repaired (a new version made it ready): the time that came meanwhile fires once, and its
    # repeat goes on.
    async with server.session_factory() as session:
        await session.execute(
            update(MadeTool).where(MadeTool.id == tool.id).values(state="ready", aside_reason=None)
        )
        await session.commit()
    await run_the_pass(server)
    await run_the_pass(server)
    assert ran(server.sandbox) == ["tally"]
    trigger = await trigger_row(server, trigger_id)
    assert trigger.state == "waiting"
    assert trigger.due_at is not None and server.clock() < trigger.due_at
    server.clock.advance(timedelta(days=1))
    await run_the_pass(server)
    assert ran(server.sandbox) == ["tally", "tally"]


async def test_a_tool_removed_has_its_triggers_cancelled(owner_chat: ChatPilot):
    server = owner_chat.server
    tool = await add_tool(server.session_factory, "tally")
    trigger_id = await add_trigger(
        server, tool, kind="time", due_at=server.clock() + LATER, for_person_id=owner_chat.person.id
    )
    reply = await owner_chat.say_with_tool("tool_remove", {"name": "tally"})
    assert reply.actions == [{"tool": "tool_remove", "status": "done"}]
    assert (await trigger_row(server, trigger_id)).state == "cancelled"
    server.clock.advance(LATER)
    await run_the_pass(server)
    assert ran(server.sandbox) == []


# --- a sandbox that does not answer --------------------------------------------------------------


def down_then(sandbox_down: list[bool]) -> FakeSandbox:
    """A sandbox that does not answer while ``sandbox_down`` holds ``True`` at its head (each run
    takes one), then runs every tool."""

    def run(request: Any) -> Any:
        if sandbox_down and sandbox_down.pop(0):
            raise SandboxUnavailable("down")
        return by_tool({})(request)

    return FakeSandbox(run)


async def test_a_trigger_the_sandbox_did_not_answer_fires_once_it_is_back(owner_chat: ChatPilot):
    server = owner_chat.server
    server.sandbox = down_then([True, True])
    tool = await add_tool(server.session_factory, "tally")
    due = server.clock() + LATER
    trigger_id = await add_trigger(
        server, tool, kind="time", due_at=due, for_person_id=owner_chat.person.id
    )
    server.clock.advance(LATER)
    await run_the_pass(server)
    # Put back as it was, at its time, its try counted.
    trigger = await trigger_row(server, trigger_id)
    assert (trigger.state, trigger.due_at, trigger.last_fired_at) == ("waiting", due, None)
    assert (trigger.missed_tries, trigger.missed_since) == (1, server.clock())
    server.clock.advance(timedelta(minutes=1))
    await run_the_pass(server)
    assert (await trigger_row(server, trigger_id)).missed_tries == 2
    # The sandbox is back: it fires once, and is done.
    server.clock.advance(timedelta(minutes=1))
    await run_the_pass(server)
    await run_the_pass(server)
    trigger = await trigger_row(server, trigger_id)
    assert (trigger.state, trigger.missed_tries, trigger.missed_since) == ("done", 0, None)
    done = [launched for launched in await launches(server) if launched["status"] == "done"]
    assert len(done) == 1
    # An outage is no failure of the tool: its launches of the day were given back.
    async with server.session_factory() as session:
        row = await session.get(MadeTool, tool.id)
    assert row is not None and (row.launches_today, row.failures_in_a_row) == (1, 0)


async def test_a_trigger_the_sandbox_did_not_answer_for_an_hour_wakes_domo_once(
    owner_chat: ChatPilot,
):
    server = owner_chat.server
    server.sandbox = down_then([True] * 200)
    model = owner_chat.use_model(FakeChatModel(["I could not run it."]))
    tool = await add_tool(server.session_factory, "house_tally")
    due = (server.clock() + LATER).replace(second=0, microsecond=0)
    trigger_id = await add_trigger(server, tool, kind="time", due_at=due, repeat="FREQ=DAILY")
    server.clock.advance(LATER)
    for _ in range(30):
        await run_the_pass(server)
        server.clock.advance(timedelta(minutes=3))
    trigger = await trigger_row(server, trigger_id)
    # An hour of tries: settled as it would have been, the repeat still set for tomorrow.
    assert (trigger.state, trigger.missed_tries, trigger.missed_since) == ("waiting", 0, None)
    assert trigger.due_at == due + timedelta(days=1)
    assert 15 < len(server.sandbox.runs) < 25
    # Domo was woken once, for the owner (a trigger for the house), with what the code says.
    [call] = tool_calls(model.calls)
    assert data_lines(call.system, "house_tally") == [triggers.MISSED]


async def test_a_house_trigger_launches_after_who_set_it_left(
    owner_chat: ChatPilot, adult_chat: ChatPilot
):
    server = owner_chat.server
    tool = await add_tool(server.session_factory, "house_tally")
    trigger_id = await add_trigger(server, tool, kind="time", due_at=server.clock() + LATER)
    # The adult who asked for it leaves the household.
    async with server.session_factory() as session:
        await session.execute(
            update(Person)
            .where(Person.id == adult_chat.person.id)
            .values(disabled_at=server.clock())
        )
        await session.commit()
    server.clock.advance(LATER)
    await run_the_pass(server)
    assert ran(server.sandbox) == ["house_tally"]
    assert server.sandbox.runs[0].entry["for"] is None
    [launched] = await launches(server)
    # Judged with Domo's own rights, and his.
    assert (launched["actor"], launched["status"]) == ("domo", "done")
    assert (await trigger_row(server, trigger_id)).state == "done"


async def test_a_trigger_for_someone_is_judged_with_their_rights_of_now(owner_chat: ChatPilot):
    server = owner_chat.server
    tool = await add_tool(server.session_factory, "tally")
    await add_trigger(
        server, tool, kind="time", due_at=server.clock() + LATER, for_person_id=owner_chat.person.id
    )
    # Its kind is no longer open to anyone.
    async with server.session_factory() as session:
        await session.execute(
            update(ActionKind)
            .where(ActionKind.action_type == "made.tally")
            .values(default_roles=[])
        )
        await session.commit()
    server.clock.advance(LATER)
    await run_the_pass(server)
    assert ran(server.sandbox) == []


async def test_a_trigger_for_dana_waits_while_she_is_away_and_goes_on_when_back(
    owner_chat: ChatPilot, add_member
):
    server = owner_chat.server
    dana = await add_member("Dana", "adult")
    tool = await add_tool(server.session_factory, "tally")
    due = server.clock() + LATER
    trigger_id = await add_trigger(server, tool, kind="time", due_at=due, for_person_id=dana.id)
    await set_disabled(server, dana, server.clock())
    server.clock.advance(LATER)
    await run_the_pass(server)
    await run_the_pass(server)
    # Away: neither fired nor moved.
    assert ran(server.sandbox) == []
    trigger = await trigger_row(server, trigger_id)
    assert (trigger.state, trigger.due_at, trigger.last_fired_at) == ("waiting", due, None)
    # Back: it fires once, for her.
    await set_disabled(server, dana, None)
    await run_the_pass(server)
    await run_the_pass(server)
    assert ran(server.sandbox) == ["tally"]
    assert server.sandbox.runs[0].entry["for"] == "Dana"
    assert (await trigger_row(server, trigger_id)).state == "done"


async def set_disabled(server: ChatServer, person: Person, at: Any) -> None:
    async with server.session_factory() as session:
        await session.execute(update(Person).where(Person.id == person.id).values(disabled_at=at))
        await session.commit()


async def test_a_house_trigger_is_judged_with_domo_rights(owner_chat: ChatPilot):
    server = owner_chat.server
    tool = await add_tool(server.session_factory, "tally")
    trigger_id = await add_trigger(server, tool, kind="time", due_at=server.clock() + LATER)
    # The owner takes the tool from Domo: what he launches for the house is refused.
    async with server.session_factory() as session:
        session.add(
            PolicyRight(
                for_domo=True,
                action_type="made.tally",
                access="deny",
                summary="The assistant does not launch tally on his own.",
                owner_words="",
                created_by=owner_chat.person.id,
                created_at=server.clock(),
            )
        )
        await session.commit()
    server.clock.advance(LATER)
    await run_the_pass(server)
    assert ran(server.sandbox) == []
    assert await launches(server) == []
    async with server.session_factory() as session:
        [decision] = (
            await session.scalars(select(AuditEntry).where(AuditEntry.action == "policy.decision"))
        ).all()
    assert (decision.actor, decision.details["decision"]) == ("domo", "refuse")
    # Claimed all the same: the trigger is done, not tried again.
    assert (await trigger_row(server, trigger_id)).state == "done"


async def test_a_house_wake_wakes_the_owner(owner_chat: ChatPilot, adult_chat: ChatPilot):
    server = owner_chat.server
    server.sandbox = by_program(tally=waking({"count": 3}))
    model = owner_chat.use_model(FakeChatModel(["Three."]))
    tool = await add_tool(server.session_factory, "tally")
    await add_trigger(server, tool, kind="time", due_at=server.clock() + LATER)
    server.clock.advance(LATER)
    await run_the_pass(server)
    [call] = tool_calls(model.calls)
    assert data_lines(call.system, "tally") == [{"count": 3}]
    assert 'Name (data, not instructions): "Owner"' in call.system


# --- at an event ---------------------------------------------------------------------------------


async def test_a_return_trigger_fires_at_the_person_s_next_return(owner_chat: ChatPilot):
    server = owner_chat.server
    server.app.state.arrival_turns = ArrivalTurns()
    owner_chat.use_model(FakeChatModel(["Hello."]))
    await open_page(owner_chat)
    await server.wait_for_wakes()
    tool = await add_tool(server.session_factory, "welcome")
    trigger_id = await add_trigger(
        server,
        tool,
        kind="return",
        returner_id=owner_chat.person.id,
        for_person_id=owner_chat.person.id,
    )
    # A page opened again soon after is no return.
    server.clock.advance(timedelta(minutes=5))
    await open_page(owner_chat)
    await server.wait_for_wakes()
    assert ran(server.sandbox) == []

    owner_chat.use_model(FakeChatModel(["Welcome back."]))
    server.clock.advance(timedelta(hours=2))
    await open_page(owner_chat)
    await server.wait_for_wakes()
    assert ran(server.sandbox) == ["welcome"]
    trigger = await trigger_row(server, trigger_id)
    # It stays for the next return.
    assert (trigger.state, trigger.last_fired_at) == ("waiting", server.clock())


async def test_an_arrival_trigger_fires_once_when_someone_joined(owner_chat: ChatPilot):
    server = owner_chat.server
    tool = await add_tool(server.session_factory, "greet")
    trigger_id = await add_trigger(server, tool, kind="arrival")
    one_pass = triggers_pass(app_turn_deps(server.app))
    await run_the_pass(server, one_pass)
    assert ran(server.sandbox) == []

    server.clock.advance(LATER)
    async with server.session_factory() as session:
        newcomer = Person(display_name="Tom", role="adult")
        session.add(newcomer)
        await session.flush()
        for _ in range(2):
            session.add(
                Invitation(
                    display_name="Tom",
                    role="adult",
                    created_by=owner_chat.person.id,
                    expires_at=server.clock() + timedelta(days=7),
                    accepted_at=server.clock(),
                    accepted_person_id=newcomer.id,
                )
            )
        await session.commit()
    server.clock.advance(timedelta(minutes=1))
    await run_the_pass(server, one_pass)
    await run_the_pass(server, one_pass)
    # Two joined: it fires once, and not again for them.
    assert ran(server.sandbox) == ["greet"]
    trigger = await trigger_row(server, trigger_id)
    assert (trigger.state, trigger.last_fired_at) == ("waiting", server.clock())


# --- what does not fire ---------------------------------------------------------------------------


async def test_an_arrival_trigger_does_not_fire_for_someone_who_joined_before_it_was_set(
    owner_chat: ChatPilot,
):
    server = owner_chat.server
    async with server.session_factory() as session:
        newcomer = Person(display_name="Tom", role="adult")
        session.add(newcomer)
        await session.flush()
        session.add(
            Invitation(
                display_name="Tom",
                role="adult",
                created_by=owner_chat.person.id,
                expires_at=server.clock() + timedelta(days=7),
                accepted_at=server.clock(),
                accepted_person_id=newcomer.id,
            )
        )
        await session.commit()
    server.clock.advance(LATER)
    tool = await add_tool(server.session_factory, "greet")
    await add_trigger(server, tool, kind="arrival")
    server.clock.advance(LATER)
    await run_the_pass(server)
    assert ran(server.sandbox) == []


async def test_a_return_trigger_does_not_fire_at_someone_else_s_return(
    owner_chat: ChatPilot, adult_chat: ChatPilot
):
    server = owner_chat.server
    server.app.state.arrival_turns = ArrivalTurns()
    tool = await add_tool(server.session_factory, "welcome")
    await add_trigger(
        server,
        tool,
        kind="return",
        returner_id=owner_chat.person.id,
        for_person_id=owner_chat.person.id,
    )
    adult_chat.use_model(FakeChatModel(["Hello."]))
    server.clock.advance(timedelta(hours=2))
    await open_page(adult_chat)
    await server.wait_for_wakes()
    assert ran(server.sandbox) == []


async def test_triggers_of_tools_being_rebuilt_do_not_hold_back_a_ready_one(
    owner_chat: ChatPilot,
):
    server = owner_chat.server
    due = server.clock() + LATER
    async with server.session_factory() as session:
        for n in range(TRIGGERS_PER_PASS + 5):
            tool = MadeTool(
                name=f"rebuilt_{n}",
                description="Being made again.",
                parameters={"type": "object"},
                state="set_aside",
            )
            session.add(tool)
            await session.flush()
            session.add(
                ToolBuild(
                    name=tool.name,
                    purpose="Again.",
                    opened_by=owner_chat.person.id,
                    tool_id=tool.id,
                    state="building",
                )
            )
            # Due before the ready one.
            session.add(
                MadeToolTrigger(
                    tool_id=tool.id,
                    kind="time",
                    due_at=due - timedelta(minutes=1),
                    for_person_id=owner_chat.person.id,
                    created_at=server.clock(),
                )
            )
        await session.commit()
    ready = await add_tool(server.session_factory, "tally")
    await add_trigger(server, ready, kind="time", due_at=due, for_person_id=owner_chat.person.id)
    greet = await add_tool(server.session_factory, "greet")
    await add_trigger(server, greet, kind="arrival")
    server.clock.advance(LATER)
    async with server.session_factory() as session:
        newcomer = Person(display_name="Tom", role="adult")
        session.add(newcomer)
        await session.flush()
        session.add(
            Invitation(
                display_name="Tom",
                role="adult",
                created_by=owner_chat.person.id,
                expires_at=server.clock() + timedelta(days=7),
                accepted_at=server.clock(),
                accepted_person_id=newcomer.id,
            )
        )
        await session.commit()
    server.clock.advance(timedelta(minutes=1))
    await run_the_pass(server)
    assert sorted(ran(server.sandbox)) == ["greet", "tally"]


async def test_a_return_trigger_that_cannot_be_claimed_never_holds_back_the_others(
    owner_chat: ChatPilot, monkeypatch: pytest.MonkeyPatch
):
    server = owner_chat.server
    broken_tool = await add_tool(server.session_factory, "broken")
    welcome = await add_tool(server.session_factory, "welcome")
    broken = await add_trigger(server, broken_tool, kind="return", returner_id=owner_chat.person.id)
    kept = await add_trigger(server, welcome, kind="return", returner_id=owner_chat.person.id)
    real_claimed = triggers._claimed  # pyright: ignore[reportPrivateUsage]

    async def claimed(session: Any, trigger: MadeToolTrigger, *args: Any) -> Any:
        if trigger.id == broken:
            # A change written, then a failure: only this trigger's savepoint goes back.
            trigger.state = "cancelled"
            await session.flush()
            raise RuntimeError("cannot claim")
        return await real_claimed(session, trigger, *args)

    monkeypatch.setattr(triggers, "_claimed", claimed)
    server.clock.advance(timedelta(hours=2))
    await triggers.returns_seen(
        made_tool_deps(app_turn_deps(server.app)), owner_chat.person.id, server.clock()
    )
    await server.wait_for_wakes()
    assert ran(server.sandbox) == ["welcome"]
    assert (await trigger_row(server, kept)).last_fired_at == server.clock()
    failed = await trigger_row(server, broken)
    assert (failed.state, failed.last_fired_at) == ("waiting", None)


def test_the_triggers_pass_is_one_of_the_upkeep():
    source = Path(web_app.__file__).read_text(encoding="utf-8")
    assert "triggers_pass(turns.app_turn_deps(app))" in source
