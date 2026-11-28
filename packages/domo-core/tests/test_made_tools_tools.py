"""``tool_build``, ``tool_change``, ``tool_remove``, ``tool_look``, ``tool_find`` and
``tool_launch_when``: Domo opens, changes, removes, looks at, finds and times the tools of his own
(Task 8, spec 5.5 to 5.7). The tools are Domo's (2026-11-18): any of them is his to change,
remove, look at or time in anyone's turn, and a trigger launches its tool for someone.

The tools are tried directly, with the sessions of the test database, and through the barrier's
decision for what only the rights can say; one pilot on the live server shows that no card is made
and no judge asked. Nothing builds here: a build is a row the build pass makes by itself later.
"""

import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any

import pytest
from chat_support import ChatPilot
from domo_common.i18n import t
from domo_core.accounts.actors import actor_of
from domo_core.accounts.models import Person
from domo_core.audit.log import append_audit
from domo_core.audit.models import AuditEntry
from domo_core.confirmations.describe import request_description
from domo_core.confirmations.judge import Judgement
from domo_core.confirmations.service import decide_confirmation, requester_context
from domo_core.conversation.execution import handle_tool_call
from domo_core.db import SessionFactory
from domo_core.household.service import disable
from domo_core.llm.types import ToolCall
from domo_core.madetools.build import OPEN_TO
from domo_core.madetools.launches import record
from domo_core.madetools.means import OUT_DONE
from domo_core.madetools.models import (
    ActionKind,
    MadeTool,
    MadeToolTrigger,
    MadeToolVersion,
    ToolBuild,
)
from domo_core.madetools.offered import MadeToolOffer
from domo_core.madetools.service import fingerprint
from domo_core.madetools.settings import set_for_tool, set_house
from domo_core.outward.service import close_address, open_address
from domo_core.plans.models import Plan
from domo_core.policy.barrier import Allow, decide
from domo_core.policy.catalog import (
    ALLOWED_ACTIONS,
    DEFAULT_POLICY,
    KNOWN_ACTIONS,
    OWNER_ONLY_ACTIONS,
    OpenKind,
    is_silent,
    needs_the_judge,
)
from domo_core.policy.models import PolicyRight
from domo_core.policy.open_catalog import load_policy, replace_kind
from domo_core.policy.rights import RightsTable, load_rights
from domo_core.policy.roles import Actor
from domo_core.policy.subjects import Subject, concerned_of, subject_of
from domo_core.texts.view import CatalogTexts
from domo_core.tools.context import ActsAsDomo, InvalidArguments, NamesPeople, ToolContext
from domo_core.tools.registry import TOOLS
from fakes import FakeChatModel
from sqlalchemy import select
from test_concerned_person import _judge, _people, _requests, _turn
from test_confirmation_tools import ask_before, invite
from test_own_idea import LIVE

NOW = datetime(2030, 1, 7, 9, 0, tzinfo=UTC)
MADE_TOOLS = (
    "tool_build",
    "tool_change",
    "tool_remove",
    "tool_look",
    "tool_find",
    "tool_launch_when",
    "tools_settings",
    "tool_set",
    "tool_back",
    "tool_check",
)
CODE = "def run(entry):\n    return {'result': None}\n"
TALLY_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {"amount": {"type": "number"}},
    "additionalProperties": False,
}
EXAMPLES = [{"entry": {"amount": 2}, "expected": {"total": 2}}]
PURPOSE = "Keep a running total of the amounts it is given and give the total back."
PRIVATE_WORDS = "grandma's secret recipe for the harbour fair"


def context(person: Person, session_factory: SessionFactory) -> ToolContext:
    return ToolContext(
        session_factory=session_factory,
        actor=actor_of(person),  # type: ignore[arg-type]
        person=person,
        conversation_id=uuid.uuid4(),
        origin="",
        gatekeeper=None,  # type: ignore[arg-type]
        clock=lambda: NOW,
        registry=TOOLS,
        policy=DEFAULT_POLICY,
        reply_message_id=uuid.uuid4(),
    )


async def run(tool_name: str, ctx: ToolContext, /, **arguments: Any) -> dict[str, Any]:
    """Describe then execute ``name`` in ``ctx``; what the model reads."""
    tool = TOOLS[tool_name]
    await tool.describe(arguments, ctx.actor, ctx)
    outcome = await tool.execute(arguments, ctx)
    assert outcome.card == {"key": "action.card.done", "params": {}}
    return dict(outcome.model_result)


async def refusal(tool_name: str, ctx: ToolContext, /, **arguments: Any) -> InvalidArguments:
    """The refusal of ``name`` in ``ctx``, the same at describe and at execute when the arguments
    alone or the tool named make it (``tool_build`` checks the names at execute only)."""
    tool = TOOLS[tool_name]
    with pytest.raises(InvalidArguments) as executed:
        await tool.execute(arguments, ctx)
    return executed.value


async def household(session_factory: SessionFactory) -> tuple[Person, Person, Person, Person]:
    async with session_factory() as writer:
        alex = Person(display_name="Alex", role="owner")
        sam = Person(display_name="Sam Martin", role="adult")
        dana = Person(display_name="Dana", role="child")
        paul = Person(display_name="Paul", role="guest", expires_at=NOW + timedelta(days=5))
        writer.add_all([alex, sam, dana, paul])
        await writer.commit()
    return alex, sam, dana, paul


async def add_tool(
    session_factory: SessionFactory,
    name: str,
    *,
    roles: tuple[str, ...] = ("owner", "adult", "child"),
    description: str | None = None,
    state: str = "ready",
) -> uuid.UUID:
    """A tool ``name`` of Domo's, its version and its kind open to ``roles``; committed."""
    async with session_factory() as writer:
        tool = MadeTool(
            name=name,
            description=description or f"Does {name}.",
            parameters=TALLY_PARAMETERS,
            state=state,
        )
        writer.add(tool)
        await writer.flush()
        version = MadeToolVersion(
            tool_id=tool.id,
            fingerprint=fingerprint(CODE),
            code=CODE,
            state="ready",
            checks={"imports": "ok", "review": "ok"},
        )
        writer.add(version)
        await writer.flush()
        tool.current_version_id = version.id
        writer.add(
            ActionKind(
                action_type=f"made.{name}", default_roles=list(roles), description=f"Run {name}."
            )
        )
        await writer.commit()
        return tool.id


async def rows[T](session_factory: SessionFactory, model: type[T]) -> list[T]:
    async with session_factory() as reader:
        return list((await reader.scalars(select(model))).all())


async def audit_entries(session_factory: SessionFactory, action: str) -> list[AuditEntry]:
    async with session_factory() as session:
        found = await session.scalars(
            select(AuditEntry).where(AuditEntry.action == action).order_by(AuditEntry.id)
        )
        return list(found.all())


def build_arguments(name: str = "tally", **more: Any) -> dict[str, Any]:
    return {"name": name, "purpose": PURPOSE, "examples": EXAMPLES, **more}


# --- tool_build ------------------------------------------------------------------------------


async def test_a_build_is_opened_for_the_person_and_kept_as_given(session_factory: SessionFactory):
    alex, sam, _, _ = await household(session_factory)
    ctx = context(sam, session_factory)
    result = await run("tool_build", ctx, **build_arguments())
    assert set(result) == {"status", "build"} and result["status"] == "done"
    (build,) = await rows(session_factory, ToolBuild)
    assert str(build.id) == result["build"]
    assert (build.name, build.state) == ("tally", "waiting")
    assert build.opened_by == sam.id and build.tool_id is None
    assert build.examples == EXAMPLES and build.purpose == PURPOSE
    (entry,) = await audit_entries(session_factory, "madetool.build_opened")
    assert entry.actor == f"person:{sam.id}"
    assert entry.details == {"tool": "tally", "examples": 1, "change": False}


async def test_two_builds_of_one_name_for_one_thing_give_one_build(session_factory: SessionFactory):
    _, sam, *_ = await household(session_factory)
    ctx = context(sam, session_factory)
    first = await run("tool_build", ctx, **build_arguments())
    second = await run("tool_build", ctx, **build_arguments(purpose="Something else entirely."))
    # The second is the first build, and Domo is told so: its own purpose is not the one built.
    assert second == {**first, "already_in_progress": True}
    assert "already_in_progress" not in first
    (build,) = await rows(session_factory, ToolBuild)
    assert build.purpose == PURPOSE
    assert len(await audit_entries(session_factory, "madetool.build_opened")) == 1


async def test_a_build_of_a_name_in_progress_is_one_build_whoever_asks(
    session_factory: SessionFactory,
):
    alex, sam, *_ = await household(session_factory)
    first = await run("tool_build", context(alex, session_factory), **build_arguments())
    second = await run("tool_build", context(sam, session_factory), **build_arguments())
    assert second == {**first, "already_in_progress": True}
    assert len(await rows(session_factory, ToolBuild)) == 1


async def test_a_name_in_use_is_told_to_domo(session_factory: SessionFactory):
    _, sam, *_ = await household(session_factory)
    await add_tool(session_factory, "tally")
    await add_tool(session_factory, "lamp")
    for name in ("tally", "lamp"):
        taken = await refusal("tool_build", context(sam, session_factory), **build_arguments(name))
        assert taken.code == "not_possible" and taken.detail is not None
        assert "already exists" in taken.detail and "tool_change" in taken.detail
    assert await rows(session_factory, ToolBuild) == []


@pytest.mark.parametrize(
    "more",
    [
        {"name": "Tally"},
        {"name": "1tally"},
        {"name": "a" * 41},
        {"name": 3},
        {"for": "me"},
        {"for": "house"},
        {"purpose": ""},
        {"purpose": "x" * 1801},
        {"purpose": "line\x00break"},
        {"examples": []},
        {"examples": [EXAMPLES[0]] * 6},
        {"examples": [{"entry": {}}]},
        {"examples": [{"entry": [], "expected": {}}]},
        {"examples": [{"entry": {}, "expected": {}, "more": 1}]},
        {"examples": "none"},
        {"replaces": "old_tally"},
        {"unknown": 1},
    ],
)
async def test_a_malformed_build_is_invalid(session_factory: SessionFactory, more):
    _, sam, *_ = await household(session_factory)
    ctx = context(sam, session_factory)
    arguments = build_arguments(**more)
    with pytest.raises(InvalidArguments) as described:
        await TOOLS["tool_build"].describe(arguments, ctx.actor, ctx)
    assert described.value.code in ("invalid", "too_long")
    assert (await refusal("tool_build", ctx, **arguments)).code == described.value.code
    assert await rows(session_factory, ToolBuild) == []


async def test_the_size_of_a_build_s_purpose_and_examples_is_domo_s_setting(
    session_factory: SessionFactory,
):
    # Final review, ruling R33: purpose_chars, examples and examples_chars are his settings.
    _, sam, *_ = await household(session_factory)
    ctx = context(sam, session_factory)
    long = build_arguments(purpose="x" * 2500)
    many = build_arguments(name="many", examples=[EXAMPLES[0]] * 6)
    heavy = build_arguments(name="heavy", examples=[{"entry": {"t": "x" * 25_000}, "expected": {}}])
    for arguments, setting in (
        (long, ("purpose_chars", 1800)),
        (many, ("examples", 5)),
        (heavy, ("examples_chars", 20000)),
    ):
        refused = await refusal("tool_build", ctx, **arguments)
        assert (refused.code, refused.setting) == ("too_long", setting)
        assert f"{setting[0]} is {setting[1]}" in str(refused.detail)
    async with session_factory() as writer:
        await set_house(writer, {"purpose_chars": 3000, "examples": 10, "examples_chars": 50_000})
        await writer.commit()
    for arguments in (long, many, heavy):
        assert (await run("tool_build", ctx, **arguments))["status"] == "done"


async def test_the_size_of_a_trigger_s_arguments_is_the_tool_s_setting(
    session_factory: SessionFactory,
):
    _, sam, *_ = await household(session_factory)
    await add_tool(session_factory, "tally")
    ctx = context(sam, session_factory)
    timed = {"name": "tally", "when": "2030-01-08T08:00:00+00:00", "arguments": {"amount": 5}}

    async def own(chars: int) -> None:
        async with session_factory() as writer:
            tool = (await writer.scalars(select(MadeTool).where(MadeTool.name == "tally"))).one()
            await set_for_tool(writer, tool, {"trigger_argument_chars": chars})
            await writer.commit()

    # Its own figure, read where the trigger is set: under the 13 characters of its arguments.
    await own(5)
    refused = await refusal("tool_launch_when", ctx, **timed)
    assert (refused.code, refused.setting) == ("too_long", ("trigger_argument_chars", 5))
    assert "trigger_argument_chars is 5" in str(refused.detail)
    await own(4000)
    assert (await run("tool_launch_when", ctx, **timed))["status"] == "done"


async def test_no_secret_goes_in_a_tool(session_factory: SessionFactory):
    _, sam, *_ = await household(session_factory)
    ctx = context(sam, session_factory)
    secret = "ab12" * 7  # 28 letters and digits in one piece: the shape of a key
    for more in (
        {"purpose": f"Keep the key {secret} for later."},
        {"examples": [{"entry": {"token": secret}, "expected": {}}]},
        {"examples": [{"entry": {}, "expected": {"note": ["fine", {"deep": secret}]}}]},
        {"examples": [{"entry": {secret: 1}, "expected": {}}]},
    ):
        error = await refusal("tool_build", ctx, **build_arguments(**more))
        assert error.code == "not_possible" and error.detail is not None
        assert "secret" in error.detail and secret not in error.detail
    assert await rows(session_factory, ToolBuild) == []


async def test_the_audit_of_a_build_holds_metadata_only(session_factory: SessionFactory):
    _, sam, *_ = await household(session_factory)
    await run(
        "tool_build",
        context(sam, session_factory),
        **build_arguments(
            purpose=PRIVATE_WORDS, examples=[{"entry": {"a": PRIVATE_WORDS}, "expected": {}}]
        ),
    )
    for entry in await rows(session_factory, AuditEntry):
        assert PRIVATE_WORDS not in str(entry.details)


# --- anyone may have Domo make a tool ----------------------------------------------------


def barrier_says(role: str, person_id: uuid.UUID, rights: RightsTable, action_type: str):
    from domo_core.policy.actions import DescribedAction

    described = DescribedAction("core", action_type, None, "self_profile", "household")
    return decide(Actor(person_id, role), described, DEFAULT_POLICY, rights)  # type: ignore[arg-type]


async def test_tool_build_asks_no_scope_and_a_child_may_open_one(session_factory: SessionFactory):
    _, _, dana, paul = await household(session_factory)
    tool = TOOLS["tool_build"]
    assert "for" not in tool.definition.parameters["properties"]
    assert tool.definition.parameters["required"] == ["name", "purpose", "examples"]
    for person in (dana, paul):
        ctx = context(person, session_factory)
        described = await tool.describe(build_arguments(), ctx.actor, ctx)
        assert described.action_type == "tool.build"
        assert isinstance(
            barrier_says(person.role, person.id, RightsTable(()), "tool.build"), Allow
        )
    result = await run("tool_build", context(dana, session_factory), **build_arguments())
    assert result["status"] == "done"
    (build,) = await rows(session_factory, ToolBuild)
    assert build.opened_by == dana.id
    assert "tool.make_for_house" not in KNOWN_ACTIONS


# --- tool_change -----------------------------------------------------------------------------


async def test_a_change_opens_a_build_that_names_the_tool(session_factory: SessionFactory):
    _, sam, *_ = await household(session_factory)
    tool_id = await add_tool(session_factory, "tally")
    result = await run(
        "tool_change",
        context(sam, session_factory),
        name="tally",
        purpose=PURPOSE,
        examples=EXAMPLES,
    )
    (build,) = await rows(session_factory, ToolBuild)
    assert str(build.id) == result["build"] and result["status"] == "done"
    assert build.tool_id == tool_id and build.opened_by == sam.id
    (entry,) = await audit_entries(session_factory, "madetool.build_opened")
    assert entry.details["change"] is True
    # Asked twice, it is one build.
    again = await run(
        "tool_change",
        context(sam, session_factory),
        name="tally",
        purpose="Else",
        examples=EXAMPLES,
    )
    assert again == {**result, "already_in_progress": True}
    assert len(await rows(session_factory, ToolBuild)) == 1


async def test_any_tool_is_changed_in_anyone_s_turn(session_factory: SessionFactory):
    alex, _, dana, _ = await household(session_factory)
    tool_id = await add_tool(session_factory, "chores")
    ctx = context(dana, session_factory)
    arguments = {"name": "chores", "purpose": PURPOSE, "examples": EXAMPLES}
    described = await TOOLS["tool_change"].describe(arguments, ctx.actor, ctx)
    assert described.action_type == "tool.change"
    await run("tool_change", ctx, **arguments)
    (build,) = await rows(session_factory, ToolBuild)
    assert (build.tool_id, build.opened_by) == (tool_id, dana.id)
    missing = await refusal("tool_change", ctx, **{**arguments, "name": "nothing"})
    assert missing.code == "not_found"


# --- tool_remove -----------------------------------------------------------------------------


async def test_removing_a_tool_frees_its_name_and_clears_what_hangs_on_it(
    session_factory: SessionFactory,
):
    alex, sam, dana, _ = await household(session_factory)
    tool_id = await add_tool(session_factory, "tally")
    async with session_factory() as writer:
        # The owner's word on this tool for someone else, and one on another tool.
        for action_type, access in (("made.tally", "deny"), ("made.other", "allow")):
            writer.add(
                PolicyRight(
                    person_id=dana.id,
                    action_type=action_type,
                    access=access,
                    summary="Said by the owner.",
                    owner_words="so be it",
                    created_by=alex.id,
                )
            )
        writer.add(MadeToolTrigger(tool_id=tool_id, kind="time", due_at=NOW + timedelta(days=1)))
        writer.add(ToolBuild(name="tally", purpose="more", opened_by=sam.id, tool_id=tool_id))
        await writer.commit()
    ctx = context(sam, session_factory)
    assert await run("tool_remove", ctx, name="tally") == {"status": "done", "name": "tally"}
    (tool,) = await rows(session_factory, MadeTool)
    assert tool.state == "removed"
    (trigger,) = await rows(session_factory, MadeToolTrigger)
    assert trigger.state == "cancelled"
    (build,) = await rows(session_factory, ToolBuild)
    assert build.state == "abandoned"
    (entry,) = await audit_entries(session_factory, "madetool.removed")
    assert entry.details == {"tool": "tally"}
    # The owner's rules on its kind stay: a later tool of that name is closed to the same people.
    kept = {
        right.action_type: right.revoked_at for right in await rows(session_factory, PolicyRight)
    }
    assert kept == {"made.tally": None, "made.other": None}
    # It is gone: another one may take the name.
    assert (await refusal("tool_remove", ctx, name="tally")).code == "not_found"
    again = await run("tool_build", ctx, **build_arguments())
    assert again["status"] == "done"


async def test_any_tool_is_removed_in_anyone_s_turn(session_factory: SessionFactory):
    _, _, dana, _ = await household(session_factory)
    await add_tool(session_factory, "chores")
    ctx = context(dana, session_factory)
    described = await TOOLS["tool_remove"].describe({"name": "chores"}, ctx.actor, ctx)
    assert described.action_type == "tool.remove"
    await run("tool_remove", ctx, name="chores")
    (tool,) = await rows(session_factory, MadeTool)
    assert tool.state == "removed"


# --- tool_look -------------------------------------------------------------------------------


async def test_a_look_at_ones_own_tool_shows_how_it_is_going(session_factory: SessionFactory):
    _, sam, *_ = await household(session_factory)
    tool_id = await add_tool(session_factory, "tally")
    async with session_factory() as writer:
        writer.add(
            MadeToolTrigger(
                tool_id=tool_id,
                kind="time",
                due_at=NOW + timedelta(days=1),
                repeat="FREQ=DAILY",
                for_person_id=sam.id,
            )
        )
        writer.add(MadeToolTrigger(tool_id=tool_id, kind="arrival"))
        await writer.commit()
    other_id = await add_tool(session_factory, "another")
    for minutes, status in ((3, "done"), (2, "failed"), (1, "done")):
        await launched(session_factory, tool_id, status, at=NOW - timedelta(minutes=minutes))
    await launched(session_factory, other_id, "failed", at=NOW)
    ctx = context(sam, session_factory)
    seen = await run("tool_look", ctx, name="tally")
    # The error text its program said came into the turn (ruling R46).
    assert ctx.outside_text
    assert seen["name"] == "tally" and seen["state"] == "ready" and "for" not in seen
    assert seen["description"] == "Does tally." and seen["aside_reason"] is None
    assert seen["checks"] == {"version": "ready", "summary": {"imports": "ok", "review": "ok"}}
    # A tool keeps no data of its own: none is shown.
    assert "data" not in seen and "data_characters" not in seen
    # Each trigger says for whom it launches the tool.
    assert [(t["kind"], t["repeat"], t["for"]) for t in seen["triggers"]] == [
        ("time", "FREQ=DAILY", "Sam Martin"),
        ("arrival", None, "house"),
    ]
    # Its latest launches, the last first, each with its time and the error its program said.
    assert [launch["status"] for launch in seen["launches"]] == ["done", "failed", "done"]
    assert [launch["seconds"] for launch in seen["launches"]] == [0.5, 0.5, 0.5]
    assert [launch["error"] for launch in seen["launches"]] == [None, "ValueError: no", None]
    assert "code" not in seen
    # The figures and the versions only when asked.
    assert "figures" not in seen and "versions" not in seen
    with_code = await run("tool_look", ctx, name="tally", code=True)
    assert with_code["code"] == CODE


async def launched(
    session_factory: SessionFactory, tool_id: uuid.UUID, status: str, *, at: datetime
) -> None:
    """A launch of the tool ``tool_id`` that ended ``status`` at ``at``, by a turn (committed)."""
    async with session_factory() as writer:
        tool = await writer.get(MadeTool, tool_id)
        assert tool is not None
        await record(
            writer,
            tool_id=tool_id,
            version_id=tool.current_version_id,
            at=at,
            cause="turn",
            for_house=False,
            by_turn=True,
            status=status,
            seconds=0.5,
            requests=1,
            reads=1,
            answer_bytes=4,
            error="ValueError: no" if status == "failed" else None,
        )
        await writer.commit()


async def test_a_look_gives_the_figures_and_the_versions_when_asked(
    session_factory: SessionFactory,
):
    _, sam, *_ = await household(session_factory)
    tool_id = await add_tool(session_factory, "tally")
    older = "def run(entry):\n    return {'result': 0}\n"
    async with session_factory() as writer:
        tool = await writer.get(MadeTool, tool_id)
        assert tool is not None
        current = tool.current_version_id
        first = MadeToolVersion(
            tool_id=tool_id,
            fingerprint=fingerprint(older),
            code=older,
            state="ready",
            checks={"passed": True, "checks": {"imports": "pass", "review": "pass"}},
            # Long before the tool's first version was written.
            created_at=datetime(2020, 1, 1, tzinfo=UTC),
        )
        writer.add(first)
        await writer.commit()
    for status in ("done", "failed"):
        await launched(session_factory, tool_id, status, at=NOW - timedelta(hours=1))
    ctx = context(sam, session_factory)
    seen = await run("tool_look", ctx, name="tally", figures=True)
    # Numbered from the first one made; the one in force says so, its checks a word each.
    assert [(v["number"], v["current"]) for v in seen["versions"]] == [(1, False), (2, True)]
    assert seen["versions"][0]["checks"] == {"imports": "pass", "review": "pass"}
    assert all(v["made_at"] for v in seen["versions"])
    [figured] = seen["figures"]["per_version"]
    assert (figured["version"], figured["launches"], figured["failed"]) == (2, 2, 1)
    assert seen["figures"]["build"] == {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
    # The code of a version asked, with code; the current one without a version.
    assert (await run("tool_look", ctx, name="tally", code=True, version=1))["code"] == older
    assert (await run("tool_look", ctx, name="tally", code=True, version=2))["code"] == CODE
    assert current is not None
    refused = await refusal("tool_look", ctx, name="tally", code=True, version=3)
    assert refused.code == "invalid" and "version" in str(refused.detail)


LIGHTS_OPERATIONS: dict[str, Any] = {
    "read_state": {
        "says": "Reads a state.",
        "risks": [],
        "accounts": ["lights"],
        "addresses": [{"host": "192.168.1.20", "port": 8080}],
    },
    "switch": {
        "says": "Switches a light.",
        "risks": ["irreversible", "physical"],
        "accounts": ["lights", "hall"],
        "addresses": [
            {"host": "192.168.1.20", "port": 8080},
            {"host": "lamps.home.arpa", "port": 80},
        ],
    },
}


async def with_operations(session_factory: SessionFactory, tool_id: uuid.UUID, operations) -> None:
    async with session_factory() as writer:
        tool = await writer.get(MadeTool, tool_id)
        assert tool is not None
        tool.operations = operations
        await writer.commit()


async def test_a_look_shows_where_a_tool_goes_out_and_with_which_accounts(
    session_factory: SessionFactory,
):
    # Task 7: its addresses, who opened and closed them, those it names that are not open, and
    # the accounts its operations name (as declared).
    alex, sam, _, paul = await household(session_factory)
    tool_id = await add_tool(session_factory, "lights")
    await with_operations(session_factory, tool_id, LIGHTS_OPERATIONS)
    async with session_factory() as writer:
        await open_address(
            writer,
            tool_name="lights",
            host="192.168.1.20",
            port=8080,
            on_host=False,
            by="domo",
            now=NOW,
        )
        await open_address(
            writer,
            tool_name="lights",
            host="lamps.home.arpa",
            port=80,
            on_host=False,
            by="domo",
            now=NOW,
        )
        await close_address(
            writer, tool_name="lights", host="lamps.home.arpa", port=80, by=str(alex.id), now=NOW
        )
        await open_address(
            writer,
            tool_name="lights",
            host="host",
            port=9000,
            on_host=True,
            by=str(alex.id),
            now=NOW,
        )
        # Another tool's, and the programs run by hand's: not this tool's.
        await open_address(
            writer, tool_name="other", host="10.0.0.9", port=80, on_host=False, by="domo", now=NOW
        )
        await open_address(
            writer, tool_name=None, host="10.0.0.8", port=80, on_host=False, by="domo", now=NOW
        )
        await writer.commit()
    seen = await run("tool_look", context(sam, session_factory), name="lights")
    assert seen["addresses"] == {
        "open": [
            {"host": "192.168.1.20", "port": 8080, "on_host": False, "opened_by": "you"},
            {"host": "host", "port": 9000, "on_host": True, "opened_by": "Alex"},
        ],
        "closed": [
            {
                "host": "lamps.home.arpa",
                "port": 80,
                "on_host": False,
                "opened_by": "you",
                "closed_by": "Alex",
                "closed_by_the_owner": True,
            }
        ],
        # What its operations name and is not open for it: nothing opens it but an act of yours.
        "missing": [
            {
                "host": "lamps.home.arpa",
                "port": 80,
                "operations": ["switch"],
                "closed_by_the_owner": True,
            }
        ],
    }
    assert seen["accounts"] == [
        {"name": "hall", "operations": ["switch"]},
        {"name": "lights", "operations": ["read_state", "switch"]},
    ]
    # Someone it is closed to (a guest, here) sees none of it.
    closed = await run("tool_look", context(paul, session_factory), name="lights")
    assert "addresses" not in closed and "accounts" not in closed


async def test_a_look_names_a_missing_address_the_owner_closed_for_another_tool(
    session_factory: SessionFactory,
):
    # Ruling R20: what the owner closed is closed for every tool; the tool that lacks it is told
    # it takes his yes, and one closed by anyone else is just missing.
    alex, sam, *_ = await household(session_factory)
    tool_id = await add_tool(session_factory, "lights")
    await with_operations(session_factory, tool_id, LIGHTS_OPERATIONS)
    async with session_factory() as writer:
        for host, port, by in (("192.168.1.20", 8080, alex), ("lamps.home.arpa", 80, sam)):
            await open_address(
                writer, tool_name="other", host=host, port=port, on_host=False, by="domo", now=NOW
            )
            await close_address(
                writer, tool_name="other", host=host, port=port, by=str(by.id), now=NOW
            )
        await writer.commit()
    seen = await run("tool_look", context(sam, session_factory), name="lights")
    assert seen["addresses"]["closed"] == []
    assert seen["addresses"]["missing"] == [
        {
            "host": "192.168.1.20",
            "port": 8080,
            "operations": ["read_state", "switch"],
            "closed_by_the_owner": True,
        },
        {
            "host": "lamps.home.arpa",
            "port": 80,
            "operations": ["switch"],
            "closed_by_the_owner": False,
        },
    ]


async def test_a_tool_that_does_not_go_out_shows_no_address_and_no_account(
    session_factory: SessionFactory,
):
    _, sam, *_ = await household(session_factory)
    await add_tool(session_factory, "tally")
    seen = await run("tool_look", context(sam, session_factory), name="tally")
    assert seen["addresses"] == {"open": [], "closed": [], "missing": []}
    assert seen["accounts"] == []


async def out_line(session_factory: SessionFactory, tool: str | None, **details: Any) -> None:
    """One second audit line of a request out (``madetool.out_done``), as ``means`` writes it."""
    line: dict[str, Any] = {
        "tool": tool,
        "operation": "read_state",
        "for_person": None,
        "host": "192.168.1.20",
        "port": 8080,
        "method": "GET",
        "status": 200,
        "refused": None,
        "bytes_out": 0,
        "bytes_in": 100,
        "ms": 20,
        "hops": 0,
        "zone": "house",
        **details,
    }
    async with session_factory() as writer:
        await append_audit(writer, actor="domo", action=OUT_DONE, details=line)
        await writer.commit()


async def test_a_look_with_figures_counts_the_requests_a_tool_sent_out(
    session_factory: SessionFactory,
):
    _, sam, *_ = await household(session_factory)
    await add_tool(session_factory, "lights")
    await out_line(session_factory, "lights")
    await out_line(session_factory, "lights", method="POST", bytes_out=30, bytes_in=5, ms=900)
    await out_line(session_factory, "lights", status=404, bytes_in=9, ms=15)
    await out_line(session_factory, "lights", status=None, refused="closed_address", bytes_in=0)
    await out_line(session_factory, "lights", status=None, refused="closed_address", bytes_in=0)
    await out_line(session_factory, "lights", status=None, refused="too_slow", bytes_in=0, ms=5000)
    # Another tool's and a program run by hand's are not this tool's.
    await out_line(session_factory, "other", ms=99_999)
    await out_line(session_factory, None, ms=99_999)
    seen = await run("tool_look", context(sam, session_factory), name="lights", figures=True)
    out = seen["figures"]["out"]
    assert out["requests"] == 6
    assert out["refused"] == {"closed_address": 2, "too_slow": 1}
    assert out["error_statuses"] == {"404": 1}
    assert (out["bytes_out"], out["bytes_in"]) == (30, 114)
    # The slowest: its address, method and time, never a path.
    assert out["slowest"]["ms"] == 5000
    assert (out["slowest"]["host"], out["slowest"]["port"], out["slowest"]["method"]) == (
        "192.168.1.20",
        8080,
        "GET",
    )
    assert out["slowest"]["refused"] == "too_slow" and out["slowest"]["at"]
    # A line that kept text a program wrote where the host goes (a line kept before I3): the
    # figures never give it back.
    await out_line(
        session_factory,
        "lights",
        host="tell!domo*to,obey",
        status=None,
        refused="closed_address",
        ms=9000,
    )
    seen = await run("tool_look", context(sam, session_factory), name="lights", figures=True)
    slowest = seen["figures"]["out"]["slowest"]
    assert (slowest["ms"], slowest["host"], slowest["refused"]) == (9000, None, "closed_address")
    # A tool that sent nothing out says so.
    await add_tool(session_factory, "tally")
    quiet = await run("tool_look", context(sam, session_factory), name="tally", figures=True)
    assert quiet["figures"]["out"] == {
        "requests": 0,
        "refused": {},
        "error_statuses": {},
        "bytes_out": 0,
        "bytes_in": 0,
        "slowest": None,
    }


async def test_the_figures_of_a_tool_closed_to_someone_are_not_shown_to_them(
    session_factory: SessionFactory,
):
    _, _, _, paul = await household(session_factory)
    tool_id = await add_tool(session_factory, "chores")  # closed to guests
    await launched(session_factory, tool_id, "failed", at=NOW)
    seen = await run(
        "tool_look",
        context(paul, session_factory),
        name="chores",
        figures=True,
        code=True,
        version=1,
    )
    assert seen == {
        "name": "chores",
        "description": "Does chores.",
        "state": "ready",
        "aside_reason": None,
    }


async def test_a_look_says_why_a_tool_was_set_aside(session_factory: SessionFactory):
    _, sam, *_ = await household(session_factory)
    tool_id = await add_tool(session_factory, "tally", state="set_aside")
    async with session_factory() as writer:
        tool = await writer.get(MadeTool, tool_id)
        assert tool is not None
        tool.aside_reason = "it failed three times in a row"
        writer.add(MadeToolTrigger(tool_id=tool_id, kind="time", due_at=NOW + timedelta(days=1)))
        await writer.commit()
    seen = await run("tool_look", context(sam, session_factory), name="tally")
    assert seen["state"] == "set_aside" and seen["aside_reason"] == "it failed three times in a row"
    # Its triggers are kept, paused until a new version makes it ready.
    [trigger] = seen["triggers"]
    assert trigger["paused"] is True


async def test_a_look_shows_any_tool_and_none_that_is_not(session_factory: SessionFactory):
    _, sam, *_ = await household(session_factory)
    # Made in someone else's turn, it is Domo's all the same.
    await add_tool(session_factory, "lamp")
    ctx = context(sam, session_factory)
    assert (await run("tool_look", ctx, name="lamp"))["name"] == "lamp"
    assert (await refusal("tool_look", ctx, name="nothing")).code == "not_found"


async def test_someone_a_tool_is_closed_to_sees_its_description_and_state_only(
    session_factory: SessionFactory,
):
    alex, sam, dana, paul = await household(session_factory)
    tool_id = await add_tool(session_factory, "chores")  # open to the owner, adults and children
    async with session_factory() as writer:
        writer.add(MadeToolTrigger(tool_id=tool_id, kind="arrival"))
        await writer.commit()
    seen = await run("tool_look", context(paul, session_factory), name="chores", code=True)
    assert seen == {
        "name": "chores",
        "description": "Does chores.",
        "state": "ready",
        "aside_reason": None,
    }
    # Those who may launch it see more: its triggers, its code if they ask.
    child = await run("tool_look", context(dana, session_factory), name="chores", code=True)
    assert child["code"] == CODE and "data" not in child
    assert [trigger["kind"] for trigger in child["triggers"]] == ["arrival"]
    # A guest the tool is open to is one of them.
    await add_tool(session_factory, "greeting", roles=("owner", "adult", "child", "guest"))
    welcome = await run("tool_look", context(paul, session_factory), name="greeting")
    assert "checks" in welcome


async def test_a_look_is_for_the_tool_asked_and_the_code_is_a_boolean(
    session_factory: SessionFactory,
):
    _, sam, *_ = await household(session_factory)
    ctx = context(sam, session_factory)
    for arguments in ({"name": "tally", "code": "yes"}, {"name": "T"}, {}, {"name": "a", "x": 1}):
        with pytest.raises(InvalidArguments) as described:
            await TOOLS["tool_look"].describe(arguments, ctx.actor, ctx)
        assert described.value.code == "invalid"


# --- tool_find -------------------------------------------------------------------------------


async def test_a_find_gives_the_tools_the_person_may_use_about_something(
    session_factory: SessionFactory,
):
    alex, sam, dana, paul = await household(session_factory)
    await add_tool(session_factory, "tally", description="Keeps a running total.")
    await add_tool(session_factory, "split_bill", description="Splits a bill.")
    await add_tool(session_factory, "lamp_total", description="Totals of lamps.")
    await add_tool(
        session_factory, "chores", description="Who does which chore, and the total done."
    )
    await add_tool(session_factory, "adult_total", description="A total.", roles=("owner", "adult"))
    await add_tool(session_factory, "old_total", description="A total.", state="set_aside")
    found = await run("tool_find", context(sam, session_factory), about="the total, please")
    # The best match first (two of the words), then by name: every tool of Domo's they may use.
    assert [tool["name"] for tool in found["tools"]] == [
        "chores",
        "adult_total",
        "lamp_total",
        "tally",
    ]
    assert found["tools"][3] == {"name": "tally", "description": "Keeps a running total."}
    # A child sees the tools open to children, not the adults'.
    child = await run("tool_find", context(dana, session_factory), about="total")
    assert [tool["name"] for tool in child["tools"]] == ["chores", "lamp_total", "tally"]
    nothing = await run("tool_find", context(paul, session_factory), about="total")
    assert nothing == {"tools": []}
    none = await run("tool_find", context(sam, session_factory), about="zebra")
    assert none == {"tools": []}


# --- tool_launch_when ------------------------------------------------------------------------


async def trigger_rows(session_factory: SessionFactory) -> list[MadeToolTrigger]:
    return await rows(session_factory, MadeToolTrigger)


async def test_a_tool_is_launched_at_a_time(session_factory: SessionFactory):
    _, sam, *_ = await household(session_factory)
    tool_id = await add_tool(session_factory, "tally")
    ctx = context(sam, session_factory)
    result = await run(
        "tool_launch_when",
        ctx,
        name="tally",
        when="2030-01-08T08:00:00+00:00",
        arguments={"amount": 5},
    )
    assert result["status"] == "done"
    (trigger,) = await trigger_rows(session_factory)
    assert str(trigger.id) == result["trigger"]["id"]
    assert (trigger.tool_id, trigger.kind, trigger.state) == (tool_id, "time", "waiting")
    assert trigger.due_at == datetime(2030, 1, 8, 8, 0, tzinfo=UTC) and trigger.repeat is None
    # For the person of the turn unless Domo says otherwise.
    assert trigger.for_person_id == sam.id and trigger.returner_id is None
    assert trigger.arguments == {"amount": 5} and result["trigger"]["for"] == "Sam Martin"
    (entry,) = await audit_entries(session_factory, "madetool.trigger_set")
    assert entry.details == {"tool": "tally", "kind": "time"}


async def test_a_repeating_trigger_starts_at_the_first_time_of_its_rule(
    session_factory: SessionFactory,
):
    _, sam, *_ = await household(session_factory)
    await add_tool(session_factory, "tally")
    await run(
        "tool_launch_when",
        context(sam, session_factory),
        name="tally",
        repeat="FREQ=DAILY;BYHOUR=18;BYMINUTE=0",
    )
    (trigger,) = await trigger_rows(session_factory)
    assert trigger.due_at == datetime(2030, 1, 7, 18, 0, tzinfo=UTC)
    assert trigger.repeat == "FREQ=DAILY;BYHOUR=18;BYMINUTE=0" and trigger.arguments == {}


async def test_a_trigger_for_the_house_launches_for_nobody_in_particular(
    session_factory: SessionFactory,
):
    alex, *_ = await household(session_factory)
    await add_tool(session_factory, "chores")
    # The owner's to set (R15).
    ctx = context(alex, session_factory)
    arguments = {"name": "chores", "when": "2030-01-08T08:00:00+00:00", "for": "house"}
    described = await TOOLS["tool_launch_when"].describe(arguments, ctx.actor, ctx)
    assert described.action_type == "tool.launch_when"
    result = await run("tool_launch_when", ctx, **arguments)
    assert result["trigger"]["for"] == "house"
    (trigger,) = await trigger_rows(session_factory)
    assert trigger.for_person_id is None and trigger.kind == "time"


async def test_a_return_is_the_speakers_or_a_named_persons(session_factory: SessionFactory):
    alex, sam, dana, _ = await household(session_factory)
    await add_tool(session_factory, "tally")
    ctx = context(sam, session_factory)
    mine = await run("tool_launch_when", ctx, name="tally", on="return")
    assert mine["trigger"]["person"] == "Sam Martin"
    named = await run("tool_launch_when", ctx, name="tally", on="return", person="Dana")
    assert named["trigger"]["person"] == "Dana"
    # Whose return is one thing; for whom it launches, another: here the person of the turn.
    triggers = {
        (t.kind, t.returner_id, t.for_person_id) for t in await trigger_rows(session_factory)
    }
    assert triggers == {("return", sam.id, sam.id), ("return", dana.id, sam.id)}
    unknown = await refusal("tool_launch_when", ctx, name="tally", on="return", person="Nobody")
    assert unknown.code == "not_found"
    assert isinstance(TOOLS["tool_launch_when"], NamesPeople)


async def test_an_arrival_names_nobody(session_factory: SessionFactory):
    _, sam, *_ = await household(session_factory)
    await add_tool(session_factory, "chores")
    ctx = context(sam, session_factory)
    result = await run("tool_launch_when", ctx, name="chores", on="arrival")
    assert result["trigger"]["kind"] == "arrival" and "person" not in result["trigger"]
    (trigger,) = await trigger_rows(session_factory)
    assert trigger.returner_id is None and trigger.due_at is None
    assert trigger.for_person_id == sam.id
    assert (
        await refusal("tool_launch_when", ctx, name="chores", on="arrival", person="Dana")
    ).code == "invalid"


@pytest.mark.parametrize(
    "more",
    [
        {},
        {"on": "now"},
        {"on": "arrival", "when": "2030-01-08T08:00:00+00:00"},
        {"on": "return", "repeat": "FREQ=DAILY"},
        {"when": "2030-01-06T08:00:00+00:00"},
        {"when": "tomorrow"},
        {"repeat": "EVERY DAY"},
        {"repeat": "FREQ=SECONDLY"},
        {"when": "2030-01-08T08:00:00+00:00", "arguments": "five"},
        {"when": "2030-01-08T08:00:00+00:00", "arguments": {"amount": "five"}},
        {"when": "2030-01-08T08:00:00+00:00", "arguments": {"other": 1}},
        {"when": "2030-01-08T08:00:00+00:00", "person": "Dana"},
        {"when": "2030-01-08T08:00:00+00:00", "for": "everyone"},
        {"when": "2030-01-08T08:00:00+00:00", "unknown": 1},
    ],
)
async def test_a_malformed_trigger_is_invalid(session_factory: SessionFactory, more):
    _, sam, *_ = await household(session_factory)
    await add_tool(session_factory, "tally")
    error = await refusal("tool_launch_when", context(sam, session_factory), name="tally", **more)
    assert error.code == "invalid"
    assert await trigger_rows(session_factory) == []


async def test_no_secret_goes_in_the_arguments_of_a_trigger(session_factory: SessionFactory):
    _, sam, *_ = await household(session_factory)
    async with session_factory() as writer:
        writer.add(MadeTool(name="notes", description="Notes.", parameters={"type": "object"}))
        await writer.commit()
    secret = "ab12" * 7  # 28 letters and digits in one piece: the shape of a key
    error = await refusal(
        "tool_launch_when",
        context(sam, session_factory),
        name="notes",
        when="2030-01-08T08:00:00+00:00",
        arguments={"text": secret},
    )
    assert error.code == "not_possible" and secret not in (error.detail or "")


async def test_a_trigger_is_cancelled_by_its_id(session_factory: SessionFactory):
    alex, sam, *_ = await household(session_factory)
    await add_tool(session_factory, "tally")
    await add_tool(session_factory, "lamp")
    ctx = context(sam, session_factory)
    made = await run("tool_launch_when", ctx, name="tally", on="return")
    trigger_id = made["trigger"]["id"]
    # Another tool's trigger is not this tool's to cancel.
    lamp = await run("tool_launch_when", context(alex, session_factory), name="lamp", on="return")
    wrong = await refusal("tool_launch_when", ctx, name="tally", cancel=lamp["trigger"]["id"])
    assert wrong.code == "not_found"
    assert (await refusal("tool_launch_when", ctx, name="tally", cancel="nope")).code == "not_found"
    assert (
        await refusal("tool_launch_when", ctx, name="tally", cancel=trigger_id, on="return")
    ).code == "invalid"
    done = await run("tool_launch_when", ctx, name="tally", cancel=trigger_id)
    assert done == {"status": "done", "cancelled": trigger_id}
    states = {str(t.id): t.state for t in await trigger_rows(session_factory)}
    assert states[trigger_id] == "cancelled" and states[lamp["trigger"]["id"]] == "waiting"
    assert (await refusal("tool_launch_when", ctx, name="tally", cancel=trigger_id)).code == (
        "not_found"
    )
    assert len(await audit_entries(session_factory, "madetool.trigger_cancelled")) == 1


async def test_a_tool_waits_with_a_few_triggers_only(session_factory: SessionFactory):
    _, sam, *_ = await household(session_factory)
    await add_tool(session_factory, "tally")
    ctx = context(sam, session_factory)
    for day in range(10):
        await run(
            "tool_launch_when", ctx, name="tally", when=f"2030-02-{day + 1:02d}T08:00:00+00:00"
        )
    error = await refusal("tool_launch_when", ctx, name="tally", when="2030-03-01T08:00:00+00:00")
    assert error.code == "not_possible"


# --- the person who leaves -------------------------------------------------------------------


async def test_a_plan_waiting_on_a_build_of_the_leaver_is_kept(session_factory: SessionFactory):
    alex, sam, dana, _ = await household(session_factory)
    async with session_factory() as writer:
        build = ToolBuild(name="chores", purpose="a", opened_by=sam.id)
        writer.add(build)
        await writer.flush()
        writer.add(
            Plan(person_id=dana.id, why="Tell Dana.", after_build_id=build.id, waits_since=NOW)
        )
        await writer.commit()
        leaver = await writer.get(Person, sam.id)
        assert leaver is not None
        await disable(writer, leaver, NOW, by=alex.id)
        await writer.commit()
    assert len(await rows(session_factory, Plan)) == 1
    (build_row,) = await rows(session_factory, ToolBuild)
    assert build_row.state == "waiting"


# --- silent, never judged ---------------------------------------------------------------------


async def test_the_tools_about_tools_are_silent_and_never_judged():
    for name in MADE_TOOLS:
        tool = TOOLS[name]
        assert tool.action_type == name.replace("_", ".", 1)
        assert is_silent(tool.action_type) and not needs_the_judge(tool.action_type)
        for role in ("owner", "adult", "child", "guest"):
            assert tool.action_type in DEFAULT_POLICY.allowed[role]  # type: ignore[index]


async def test_the_descriptions_say_that_nobody_is_told_and_a_build_runs_by_itself():
    description = TOOLS["tool_build"].definition.description
    assert "Nobody is told" in description and "unless they ask" in description
    assert "by itself" in description and "plan that waits for this build" in description
    assert "plan_set with after_build" in description
    for name in MADE_TOOLS:
        assert "Nobody is told" in TOOLS[name].definition.description, name


def test_a_program_and_a_tool_say_how_they_go_out_and_that_a_secret_travels_by_an_account():
    # Task 7: domo.out in one sentence each, and a secret only through an account.
    for name in ("program_run", "tool_build"):
        description = TOOLS[name].definition.description
        assert description.count("domo.out") == 1, name
        assert "a secret only travels through an account" in description, name
    # What a build says of the addresses its operations name and the tool lacks.
    assert "address_open" in TOOLS["tool_build"].definition.description
    look = TOOLS["tool_look"].definition.description
    assert "addresses" in look and "accounts" in look and "requests out" in look


async def test_a_build_shows_no_card_and_asks_no_yes(owner_chat: ChatPilot, session_factory):
    reply = await owner_chat.say_with_tool("tool_build", build_arguments(), judge="yes")
    assert reply.actions == [{"tool": "tool_build", "status": "done"}]
    assert reply.cards_html == ""
    assert 'data-tool="tool_build"' not in (await owner_chat.get("/chat")).text
    (build,) = await rows(owner_chat.server.session_factory, ToolBuild)
    assert build.name == "tally" and build.opened_by == owner_chat.person.id
    reply = await owner_chat.say_with_tool("tool_build", build_arguments("chores"), judge="yes")
    assert reply.actions == [{"tool": "tool_build", "status": "done"}]
    assert reply.cards_html == ""
    reply = await owner_chat.say_with_tool("tool_find", {"about": "tally"})
    assert reply.actions == [{"tool": "tool_find", "status": "done"}]
    assert reply.cards_html == ""


async def test_a_silent_tool_never_uses_up_what_domo_asked_more_for(
    owner_chat: ChatPilot, count_invitations
):
    owner_chat.use_model(
        FakeChatModel(
            tool_script=[
                ask_before("explicit_yes"),
                ToolCall("b1", "tool_build", json.dumps(build_arguments())),
                invite(),
                "ok",
            ],
            judge="do",
        )
    )
    reply = await owner_chat.say("invite Robin")
    assert [a["status"] for a in reply.actions][:2] == ["done", "done"]
    # The raise reached the invitation, not the tool: it waits for a yes.
    assert reply.pending_confirmation_id is not None
    assert await count_invitations() == 0


# --- a tool the owner closed to someone ---------------------------------------------------------


async def closed_to(session_factory: SessionFactory, owner: Person, person: Person, name: str):
    async with session_factory() as writer:
        writer.add(
            PolicyRight(
                person_id=person.id,
                action_type=f"made.{name}",
                access="deny",
                summary="Not for them.",
                owner_words="Not for them.",
                created_by=owner.id,
            )
        )
        await writer.commit()


async def may_launch(session_factory: SessionFactory, person: Person, name: str) -> bool:
    async with session_factory() as reader:
        policy = await load_policy(reader)
        rights = await load_rights(reader)
    return rights.resolve(actor_of(person), f"made.{name}", policy).allowed  # type: ignore[arg-type]


async def test_someone_a_tool_is_closed_to_may_not_change_remove_or_time_it(
    session_factory: SessionFactory,
):
    alex, _, dana, paul = await household(session_factory)
    await add_tool(session_factory, "split", roles=("owner", "adult", "child", "guest"))
    await closed_to(session_factory, alex, paul, "split")
    closed = context(paul, session_factory)
    opened = context(dana, session_factory)
    when = "2030-01-08T08:00:00+00:00"
    kept = await run("tool_launch_when", opened, name="split", when=when)
    refused_calls: list[dict[str, Any]] = [
        {"tool": "tool_change", "name": "split", "purpose": PURPOSE, "examples": EXAMPLES},
        {"tool": "tool_launch_when", "name": "split", "when": when},
        {"tool": "tool_launch_when", "name": "split", "when": when, "for": "house"},
        {"tool": "tool_launch_when", "name": "split", "cancel": kept["trigger"]["id"]},
        {"tool": "tool_remove", "name": "split"},
    ]
    for call in refused_calls:
        tool_name, arguments = call.pop("tool"), call
        with pytest.raises(InvalidArguments) as described:
            await TOOLS[tool_name].describe(arguments, closed.actor, closed)
        assert described.value.code == "not_possible", (tool_name, arguments)
        refused = await refusal(tool_name, closed, **arguments)
        assert refused.code == "not_possible", (tool_name, arguments)
    # Nothing was changed: no build, the one trigger Dana set still waits, the tool is ready.
    assert await rows(session_factory, ToolBuild) == []
    assert [t.state for t in await rows(session_factory, MadeToolTrigger)] == ["waiting"]
    assert [t.state for t in await rows(session_factory, MadeTool)] == ["ready"]
    # Dana, to whom it is open, does each.
    await run("tool_change", opened, name="split", purpose=PURPOSE, examples=EXAMPLES)
    # For the house, the owner's (R15).
    owners = context(alex, session_factory)
    await run("tool_launch_when", owners, name="split", when=when, **{"for": "house"})
    await run("tool_launch_when", opened, name="split", cancel=kept["trigger"]["id"])
    await run("tool_remove", opened, name="split")
    assert [t.state for t in await rows(session_factory, MadeTool)] == ["removed"]


async def test_removing_a_tool_keeps_the_owners_rule_for_the_one_made_again(
    session_factory: SessionFactory,
):
    alex, _, dana, paul = await household(session_factory)
    await add_tool(session_factory, "split", roles=("owner", "adult", "child", "guest"))
    await closed_to(session_factory, alex, paul, "split")
    await run("tool_remove", context(dana, session_factory), name="split")
    # Built again under the same name, its kind opened to every role, as a build does.
    async with session_factory() as writer:
        await replace_kind(
            writer,
            OpenKind(
                action_type="made.split",
                costs=False,
                irreversible=False,
                physical=False,
                outward=False,
                facts=MappingProxyType({}),
                default_roles=OPEN_TO,
                description="Splits again.",
            ),
            now=NOW,
        )
        writer.add(MadeTool(name="split", description="Splits.", parameters=TALLY_PARAMETERS))
        await writer.commit()
    assert await may_launch(session_factory, dana, "split")
    assert not await may_launch(session_factory, paul, "split")


# --- Domo on his own (Task 9) ------------------------------------------------------------------


async def closed_to_domo(session_factory: SessionFactory, owner: Person, name: str) -> None:
    async with session_factory() as writer:
        writer.add(
            PolicyRight(
                for_domo=True,
                action_type=f"made.{name}",
                access="deny",
                summary="Not for the assistant alone.",
                owner_words="Not for the assistant alone.",
                created_by=owner.id,
            )
        )
        await writer.commit()


def test_the_tools_about_tools_are_domo_s_own_acts():
    for name in MADE_TOOLS:
        assert isinstance(TOOLS[name], ActsAsDomo), name


async def test_in_a_turn_of_his_own_a_trigger_is_the_houses_and_his_own_rights_guard_the_tool(
    session_factory: SessionFactory,
):
    alex, *_ = await household(session_factory)
    await add_tool(session_factory, "split", roles=("owner", "adult", "child", "guest"))
    alone = replace(context(alex, session_factory), on_his_own=True)
    when = "2030-01-08T08:00:00+00:00"
    # Nobody asked: a trigger he sets is the house's, and one for the owner is refused.
    set_ = await run("tool_launch_when", alone, name="split", when=when)
    assert set_["trigger"]["for"] == "house"
    [trigger] = await rows(session_factory, MadeToolTrigger)
    assert trigger.for_person_id is None
    refused = await refusal("tool_launch_when", alone, name="split", when=when, **{"for": "me"})
    assert refused.code == "not_possible"
    [entry] = await audit_entries(session_factory, "madetool.trigger_set")
    assert entry.actor == "domo"
    # The owner closing the tool to themselves changes nothing for Domo on his own...
    await closed_to(session_factory, alex, alex, "split")
    await run("tool_change", alone, name="split", purpose=PURPOSE, examples=EXAMPLES)
    [opened] = await audit_entries(session_factory, "madetool.build_opened")
    assert opened.actor == "domo"
    # ... closing it to Domo does (R11: the guard reads his own rights there).
    await closed_to_domo(session_factory, alex, "split")
    for tool_name, arguments in (
        ("tool_remove", {"name": "split"}),
        ("tool_launch_when", {"name": "split", "cancel": set_["trigger"]["id"]}),
    ):
        refused = await refusal(tool_name, alone, **arguments)
        assert refused.code == "not_possible", tool_name
    # In the owner's own turn, their own rule is what counts.
    owners = context(alex, session_factory)
    assert (await refusal("tool_remove", owners, name="split")).code == "not_possible"


async def test_only_who_may_set_it_times_a_tool_for_the_house_at_once(
    session_factory: SessionFactory,
):
    # Ruling R17 (Task 27): a trigger for the house is set at once by whom the rights allow
    # ``house.set`` (the owner by default) and by Domo in a turn of his own; anyone else's waits
    # for the owner's yes, as Domo's request, and is never set without it.
    alex, sam, dana, _ = await household(session_factory)
    await add_tool(session_factory, "chores")
    house = {"name": "chores", "when": "2030-01-08T08:00:00+00:00", "for": "house"}
    tool = TOOLS["tool_launch_when"]
    for person in (sam, dana):
        ctx = context(person, session_factory)
        # Described, then waiting for the owner: not refused.
        await tool.describe(house, ctx.actor, ctx)
        waits = await tool.waits_for(house, ctx)  # type: ignore[attr-defined]
        assert waits is not None and (waits.approver, waits.domo_asks) == ("owner", True)
        refused = await refusal("tool_launch_when", ctx, **house)
        assert refused.code == "not_possible", person.display_name
    assert await trigger_rows(session_factory) == []
    owners = context(alex, session_factory)
    assert await tool.waits_for(house, owners) is None  # type: ignore[attr-defined]
    await run("tool_launch_when", owners, **house)
    alone = replace(context(alex, session_factory), on_his_own=True)
    assert await tool.waits_for(house, alone) is None  # type: ignore[attr-defined]
    await run("tool_launch_when", alone, **house)
    assert [t.for_person_id for t in await trigger_rows(session_factory)] == [None, None]
    # For oneself, nothing waits.
    mine = {**house, "for": "me"}
    assert await tool.waits_for(mine, context(dana, session_factory)) is None  # type: ignore[attr-defined]


# --- Task 27: "for the house" asked by someone who may not set it goes to the owner -------------

HOUSE_AT: dict[str, Any] = {
    "name": "chores",
    "when": "2030-01-08T08:00:00+00:00",
    "for": "house",
}


async def _the_house(session_factory: SessionFactory) -> dict[str, Person]:
    """Alex, the owner; Dana, an adult; Paul, a child, each with a conversation; and the tool
    ``chores``."""
    people = await _people(
        session_factory, ("Alex", "owner"), ("Dana", "adult"), ("Paul", "child"), grants=False
    )
    await add_tool(session_factory, "chores")
    return people


def _timed(arguments: dict[str, Any]) -> ToolCall:
    return ToolCall("w1", "tool_launch_when", json.dumps(arguments))


async def _yes(ctx: ToolContext, request_id: uuid.UUID, decider: Person) -> tuple[str, Any]:
    return await decide_confirmation(
        session_factory=ctx.session_factory,
        confirmation_id=request_id,
        decider=decider,
        approve=True,
        via="button",
        reply_message_id=None,
        ctx_factory=requester_context(ctx),
        registry=ctx.registry,
        policy=DEFAULT_POLICY,
        now=LIVE,
    )


async def test_dana_s_trigger_for_the_house_waits_for_the_owner(
    session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch
):
    people = await _the_house(session_factory)
    dana = people["Dana"]
    judged = _judge(monkeypatch, Judgement("do"))
    ctx, reply_id, _ = await _turn(session_factory, dana)
    record = await handle_tool_call(_timed(HOUSE_AT), ctx=ctx, reply_id=reply_id)
    assert record.status == "waiting", record.model_result
    [request] = await _requests(session_factory)
    assert (request.approver_kind, request.by_domo, request.requester_id) == (
        "owner",
        True,
        dana.id,
    )
    # A request for the owner, nothing set, and no judge asked (a trigger is never judged).
    assert await trigger_rows(session_factory) == []
    assert judged.cases == []


@pytest.mark.parametrize(
    ("arguments", "key", "when"),
    [
        (
            HOUSE_AT,
            "action.describe.tool.launch_when.house.time",
            datetime(2030, 1, 8, 8, tzinfo=UTC),
        ),
        (
            {"name": "chores", "on": "arrival", "for": "house"},
            "action.describe.tool.launch_when.house.arrival",
            None,
        ),
        (
            {"name": "chores", "on": "return", "for": "house"},
            "action.describe.tool.launch_when.house.return",
            None,
        ),
    ],
)
async def test_the_owner_is_told_the_trigger_is_for_the_whole_house(
    session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch, arguments, key, when
):
    # Fix round 1: the request the owner says yes to says it is for the whole house, which tool,
    # and when, in his language (the catalogs, never a sentence of the code).
    people = await _the_house(session_factory)
    _judge(monkeypatch, Judgement("do"))
    ctx, reply_id, _ = await _turn(session_factory, people["Dana"])
    await handle_tool_call(_timed(arguments), ctx=ctx, reply_id=reply_id)
    [request] = await _requests(session_factory)
    for lang in ("en", "fr"):
        texts = CatalogTexts(lang)
        holes = {"tool": "chores"} if when is None else {"tool": "chores", "when": texts.date(when)}
        assert request_description(request, texts) == t(key, lang, **holes)


async def test_after_his_yes_it_is_set_for_the_house_and_judged_as_the_assistant_s(
    session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch
):
    people = await _the_house(session_factory)
    _judge(monkeypatch, Judgement("do"))
    ctx, reply_id, _ = await _turn(session_factory, people["Dana"])
    await handle_tool_call(_timed(HOUSE_AT), ctx=ctx, reply_id=reply_id)
    [request] = await _requests(session_factory)
    outcome, ran = await _yes(ctx, request.id, people["Alex"])
    assert outcome == "executed", ran
    # For the house: launched for nobody in particular, with the assistant's own rights.
    [trigger] = await trigger_rows(session_factory)
    assert trigger.for_person_id is None
    # The assistant is told the owner may let it hold from now on, through the rights.
    assert ran is not None and "house.set" in json.dumps(ran.model_result)


async def test_with_the_right_house_set_dana_sets_it_at_once(
    session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch
):
    people = await _the_house(session_factory)
    alex, dana = people["Alex"], people["Dana"]
    async with session_factory() as writer:
        writer.add(
            PolicyRight(
                person_id=dana.id,
                action_type="house.set",
                access="allow",
                summary="Dana sets things for the house.",
                owner_words="Dana peut régler des choses pour la maison",
                created_by=alex.id,
            )
        )
        await writer.commit()
    _judge(monkeypatch, Judgement("do"))
    ctx, reply_id, _ = await _turn(session_factory, dana)
    record = await handle_tool_call(_timed(HOUSE_AT), ctx=ctx, reply_id=reply_id)
    assert record.status == "done", record.model_result
    assert await _requests(session_factory) == []
    [trigger] = await trigger_rows(session_factory)
    assert trigger.for_person_id is None


def test_setting_for_the_house_is_a_type_of_its_own_the_owner_s_by_default():
    assert "house.set" in KNOWN_ACTIONS
    assert needs_the_judge("house.set") is False
    assert {role for role, types in ALLOWED_ACTIONS.items() if "house.set" in types} == {"owner"}
    assert "house.set" not in OWNER_ONLY_ACTIONS


async def test_a_launch_for_the_house_is_described_so_and_the_owner_answers_for_it(
    session_factory: SessionFactory,
):
    # The subject rule of a tool Domo made reads ``for_house``: a launch's real description says
    # it, so that the thing is the house's and the owner answers for it.
    people = await _the_house(session_factory)
    alex, dana = people["Alex"], people["Dana"]
    async with session_factory() as reader:
        made = await reader.scalar(select(MadeTool).where(MadeTool.name == "chores"))
    assert made is not None
    offer = MadeToolOffer(made, None)  # type: ignore[arg-type]
    alone = replace(context(alex, session_factory), on_his_own=True)
    house = await offer.describe({"amount": 2}, alone.actor, alone)
    hers = await offer.describe(
        {"amount": 2}, Actor(dana.id, "adult"), context(dana, session_factory)
    )
    assert (house.facts["for_house"], hers.facts["for_house"]) == (True, False)
    async with session_factory() as reader:
        for action, belongs, answers in (
            (house, "house", alex.id),
            (hers, dana.id, dana.id),
        ):
            subject = subject_of(action, None, dana.id)
            assert subject == Subject("tool:chores", belongs)
            assert await concerned_of(reader, subject, action, LIVE) == answers


async def test_a_look_without_an_error_leaves_the_turn_free_of_outside_text(
    session_factory: SessionFactory,
):
    _, sam, *_ = await household(session_factory)
    tool_id = await add_tool(session_factory, "tally")
    await launched(session_factory, tool_id, "done", at=NOW - timedelta(minutes=1))
    ctx = context(sam, session_factory)
    seen = await run("tool_look", ctx, name="tally")
    assert [launch["error"] for launch in seen["launches"]] == [None]
    assert not ctx.outside_text
