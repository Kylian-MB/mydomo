"""The tables of the tools Domo makes, and the service that keeps them (Task 4).

The service is tried directly on the shared rolled-back ``session``."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from domo_core.madetools.models import (
    ActionKind,
    MadeTool,
    MadeToolRun,
    MadeToolTrigger,
    MadeToolVersion,
    ToolBuild,
)
from domo_core.madetools.service import (
    builds_in_progress,
    count_launch,
    fingerprint,
    open_build,
    ready_tools,
    set_aside,
    tool_named,
)
from domo_core.plans.models import Plan
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

NOW = datetime(2026, 11, 17, 10, 0, tzinfo=UTC)


def make_tool(name: str, *, state: str = "ready") -> MadeTool:
    return MadeTool(
        name=name, description="does a small thing", parameters={"type": "object"}, state=state
    )


async def build(session, person, name: str = "tally", **more):
    return await open_build(
        session,
        name=name,
        purpose="count things",
        opened_by=person.id,
        examples=[],
        now=NOW,
        **more,
    )


async def test_opening_the_same_name_twice_gives_one_build(session, add_person):
    alex = await add_person("Alex", "owner")
    first = await build(session, alex)
    second = await build(session, alex)
    assert second.id == first.id
    assert [b.id for b in await builds_in_progress(session)] == [first.id]


async def test_a_name_can_be_built_again_once_its_build_ended(session, add_person):
    alex = await add_person("Alex", "owner")
    first = await build(session, alex)
    first.state = "ready"
    await session.flush()
    second = await build(session, alex)
    assert second.id != first.id
    assert [b.id for b in await builds_in_progress(session)] == [second.id]


async def test_the_database_holds_one_build_per_name_in_progress(session, add_person):
    alex = await add_person("Alex", "owner")
    await build(session, alex)
    session.add(ToolBuild(name="tally", purpose="again", opened_by=alex.id, state="building"))
    with pytest.raises(IntegrityError):
        await session.flush()


async def with_version(session, tool: MadeTool, state: str = "ready") -> MadeTool:
    """``tool`` with a version in force in ``state`` (flushed)."""
    session.add(tool)
    await session.flush()
    code = f"def run(entry):\n    return {{'result': {tool.name!r}}}\n"
    version = MadeToolVersion(
        tool_id=tool.id, fingerprint=fingerprint(code), code=code, state=state
    )
    session.add(version)
    await session.flush()
    tool.current_version_id = version.id
    await session.flush()
    return tool


async def test_every_ready_tool_is_ready_for_everyone(session):
    for tool in (
        make_tool("alex_tool"),
        make_tool("dana_tool"),
        make_tool("aside_tool", state="set_aside"),
        make_tool("gone_tool", state="removed"),
    ):
        await with_version(session, tool)
    # A tool in state ready whose version in force is not ready, or that has none, is not.
    await with_version(session, make_tool("checking_tool"), state="checking")
    await with_version(session, make_tool("refused_tool"), state="refused")
    session.add(make_tool("bare_tool"))
    await session.flush()
    assert [t.name for t in await ready_tools(session)] == ["alex_tool", "dana_tool"]


async def test_a_name_is_unique_in_the_household_until_its_tool_is_removed(session):
    old = make_tool("shared_name")
    session.add(old)
    await session.flush()
    assert (await tool_named(session, "shared_name")) is old
    session.add(make_tool("shared_name"))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_a_removed_tool_frees_its_name(session):
    old = make_tool("shared_name", state="removed")
    session.add(old)
    await session.flush()
    assert await tool_named(session, "shared_name") is None
    fresh = make_tool("shared_name")
    session.add(fresh)
    await session.flush()
    assert await tool_named(session, "shared_name") is fresh


@pytest.mark.parametrize("name", ["", "Upper", "1start", "has-dash", "has space", "x" * 41, "é"])
async def test_a_bad_name_is_refused(session, name):
    session.add(make_tool(name))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_the_longest_name_is_kept(session):
    session.add(make_tool("a" + "b_1" * 13))
    session.add(make_tool("z" * 40))
    await session.flush()


async def test_the_201st_launch_of_a_day_is_refused_and_the_next_day_counts(session):
    tool = make_tool("busy")
    session.add(tool)
    await session.flush()
    for _ in range(200):
        assert await count_launch(session, tool, NOW, None, 200) is True
    assert await count_launch(session, tool, NOW, None, 200) is False
    assert tool.launches_today == 200
    assert await count_launch(session, tool, NOW + timedelta(days=1), None, 200) is True
    assert tool.launches_today == 1


async def test_a_tool_is_counted_up_to_the_launches_a_day_it_is_given(session):
    # How many is the tool's ``launches_per_day`` setting (``launch`` reads it): no ceiling.
    tool = make_tool("few")
    session.add(tool)
    await session.flush()
    assert [await count_launch(session, tool, NOW, None, 2) for _ in range(3)] == [
        True,
        True,
        False,
    ]
    many = make_tool("many")
    session.add(many)
    await session.flush()
    many.launches_day, many.launches_today = NOW.date(), 10**6
    assert await count_launch(session, many, NOW, None, 10**9) is True


async def test_the_day_is_the_households_day(session):
    tool = make_tool("zoned")
    session.add(tool)
    await session.flush()
    zone = ZoneInfo("Pacific/Auckland")  # UTC+13 in November
    late_utc = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)  # 01:00 on the 18th there
    assert await count_launch(session, tool, late_utc, zone, 200) is True
    # The same UTC day, but the day before in Auckland: another day there.
    assert await count_launch(session, tool, datetime(2026, 11, 17, 8, 0, tzinfo=UTC), zone, 200)
    assert tool.launches_today == 1
    assert tool.launches_day is not None
    assert tool.launches_day.isoformat() == "2026-11-17"


async def test_setting_a_tool_aside_takes_it_out_of_the_ready_ones(session):
    tool = await with_version(session, make_tool("shaky"))
    assert await ready_tools(session) == [tool]
    await set_aside(session, tool, NOW, reason="it failed three times")
    assert tool.state == "set_aside"
    assert tool.aside_reason == "it failed three times"
    assert tool.changed_at == NOW
    assert await ready_tools(session) == []


def test_the_fingerprint_is_the_sha256_of_the_code():
    assert fingerprint("") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert fingerprint("é") == fingerprint("é")
    assert fingerprint("a") != fingerprint("b")


async def test_a_plan_waiting_only_for_a_build_satisfies_its_constraint(session, add_person):
    alex = await add_person("Alex", "owner")
    opened = await build(session, alex)
    plan = Plan(
        person_id=alex.id,
        why="tell Alex once the tool is ready",
        after_build_id=opened.id,
        waits_since=NOW,
    )
    session.add(plan)
    await session.flush()
    assert plan.state == "waiting"


async def test_a_build_and_since_when_a_plan_waits_for_it_go_together(session, add_person):
    alex = await add_person("Alex", "owner")
    opened = await build(session, alex)
    session.add(Plan(person_id=alex.id, why="no since", due_at=NOW, after_build_id=opened.id))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_a_waiting_plan_with_nothing_to_wait_for_is_refused(session, add_person):
    alex = await add_person("Alex", "owner")
    session.add(Plan(person_id=alex.id, why="nothing to wait for"))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_a_deleted_person_takes_their_triggers_and_runs_but_no_tool_nor_build(
    session, add_person
):
    alex = await add_person("Alex", "owner")
    dana = await add_person("Dana", "adult")
    tool = make_tool("dana_asked")
    session.add(tool)
    await session.flush()
    version = MadeToolVersion(tool_id=tool.id, fingerprint=fingerprint("x"), code="x")
    session.add(version)
    await session.flush()
    tool.current_version_id = version.id
    session.add_all(
        [
            MadeToolTrigger(tool_id=tool.id, kind="arrival", for_person_id=dana.id),
            MadeToolTrigger(tool_id=tool.id, kind="return", returner_id=dana.id),
            MadeToolTrigger(tool_id=tool.id, kind="arrival", for_person_id=alex.id),
            MadeToolTrigger(tool_id=tool.id, kind="arrival"),
            MadeToolRun(tool_id=tool.id, person_id=dana.id, handed_over_at=NOW),
            MadeToolRun(
                tool_id=tool.id,
                for_house=True,
                kind="wake",
                status="done",
                handed_over_at=NOW,
                finished_at=NOW,
            ),
        ]
    )
    await build(session, dana, "dana_build")
    await session.flush()

    await session.execute(delete(type(dana)).where(type(dana).id == dana.id))
    session.expire_all()

    names = (await session.execute(select(MadeTool.name))).scalars().all()
    assert names == ["dana_asked"]
    for model, expected in (
        (MadeToolVersion, 1),
        (MadeToolTrigger, 2),
        (MadeToolRun, 1),
        (ToolBuild, 1),
    ):
        count = await session.execute(select(func.count()).select_from(model))
        assert count.scalar_one() == expected, model


async def test_an_action_kind_is_kept_with_its_risks(session):
    session.add(
        ActionKind(
            action_type="made.say_hello",
            costs=False,
            facts={"what": "text"},
            default_roles=["owner"],
            description="says hello",
        )
    )
    await session.flush()
    kind = await session.get(ActionKind, "made.say_hello")
    assert kind is not None
    assert (kind.irreversible, kind.physical, kind.outward) == (False, False, False)
    assert kind.created_at is not None


async def test_a_tool_version_takes_a_fingerprint_only_in_hex(session):
    tool = make_tool("versioned")
    session.add(tool)
    await session.flush()
    session.add(MadeToolVersion(tool_id=tool.id, fingerprint="not-a-hash", code="x"))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_a_long_reason_is_cut_to_300_and_an_empty_one_is_none(session):
    tool = make_tool("reasoned")
    session.add(tool)
    await session.flush()
    await set_aside(session, tool, NOW, reason="x" * 500)
    assert tool.aside_reason == "x" * 300
    await set_aside(session, tool, NOW, reason="")
    assert tool.aside_reason is None


async def test_deleting_who_opened_a_build_keeps_it_and_other_peoples_waiting_plans(
    session, add_person
):
    alex = await add_person("Alex", "owner")
    dana = await add_person("Dana", "adult")
    opened = await build(session, alex)
    waiting = Plan(
        person_id=dana.id,
        why="tell Dana when it is ready",
        after_build_id=opened.id,
        waits_since=NOW,
    )
    session.add(waiting)
    await session.flush()
    build_id, plan_id = opened.id, waiting.id

    await session.execute(delete(type(alex)).where(type(alex).id == alex.id))
    session.expire_all()

    kept = await session.get(ToolBuild, build_id)
    assert kept is not None
    assert kept.opened_by is None
    plan = await session.get(Plan, plan_id)
    assert plan is not None
    assert plan.after_build_id == build_id


async def test_a_build_cannot_be_deleted_while_a_plan_waits_on_it(session, add_person):
    alex = await add_person("Alex", "owner")
    opened = await build(session, alex)
    session.add(Plan(person_id=alex.id, why="waiting", after_build_id=opened.id, waits_since=NOW))
    await session.flush()
    with pytest.raises(IntegrityError):
        await session.execute(delete(ToolBuild).where(ToolBuild.id == opened.id))


async def test_a_return_trigger_names_whose_return_and_an_arrival_names_nobody(session, add_person):
    alex = await add_person("Alex", "owner")
    tool = make_tool("ours")
    session.add(tool)
    await session.flush()
    session.add(MadeToolTrigger(tool_id=tool.id, kind="arrival", returner_id=None))
    session.add(MadeToolTrigger(tool_id=tool.id, kind="return", returner_id=alex.id))
    await session.flush()
    session.add(MadeToolTrigger(tool_id=tool.id, kind="return", for_person_id=alex.id))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_a_run_is_a_persons_or_the_houses(session, add_person):
    alex = await add_person("Alex", "owner")
    tool = make_tool("ours")
    session.add(tool)
    await session.flush()
    for wrong in ({"person_id": alex.id, "for_house": True}, {"person_id": None}):
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                session.add(MadeToolRun(tool_id=tool.id, handed_over_at=NOW, **wrong))
                await session.flush()
