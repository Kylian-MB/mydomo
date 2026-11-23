"""Domo builds a tool outside anyone's turn (Task 9, spec 5.5, 5.7, 5.8, 6): the pass of the
upkeep (``toolmaking_pass``) takes the builds to make, a model writes the code in rounds
(``submit`` or ``give_up``), and a version is checked four ways before it becomes the tool.

The builds run on the pilots' live server: its model slot holds a ``Maker`` that answers the calls
of the model that writes (one answer per round, in order) and those of the reviewer, and its
sandbox is a ``FakeSandbox`` that runs nothing."""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncGenerator, Sequence
from datetime import timedelta
from typing import Any

import pytest
from chat_support import ChatPilot, ChatServer, Member
from domo_common.sandbox_api import RunRequest, RunResult, ScanFinding
from domo_core.accounts.models import Person
from domo_core.audit.models import AuditEntry
from domo_core.channels.turns import app_turn_deps
from domo_core.conversation.prepare import made_tool_deps
from domo_core.household.service import disable
from domo_core.llm.types import (
    ChatChunk,
    ChatMessage,
    ChatUsage,
    HistoryItem,
    ModelCallError,
    ToolCall,
    ToolChoice,
    ToolDefinition,
)
from domo_core.madetools import build as building
from domo_core.madetools import build_writes, upkeep
from domo_core.madetools.build import (
    ABANDONED,
    BUILT,
    CHECKED,
    NOTHING_SUBMITTED,
    SAME_CODE,
    UNSETTLED,
    UNSETTLED_TOO_OFTEN,
    claim,
    retried_after,
    toolmaking_prompt,
    waited_too_long,
)
from domo_core.madetools.build_writes import (
    YES_WITHDRAWN,
    pause_after,
    withdrawn_outcome,
)
from domo_core.madetools.checks import review_prompt
from domo_core.madetools.launch import LAUNCHED, ForWhom, launch
from domo_core.madetools.models import (
    ActionKind,
    DeclaredSpending,
    LaunchStop,
    MadeTool,
    MadeToolTrigger,
    MadeToolVersion,
    StandingAgreement,
    ToolBuild,
)
from domo_core.madetools.offered import set_aside_for
from domo_core.madetools.service import fingerprint, open_build
from domo_core.madetools.settings import SETTINGS, set_house, starts
from domo_core.madetools.upkeep import toolmaking_pass
from domo_core.madetools.yes import digest_of
from domo_core.outward.models import OpenAddress
from domo_core.outward.service import close_address, open_address
from domo_core.policy.catalog import OpenKind
from domo_core.policy.models import PolicyRight
from domo_core.policy.open_catalog import KindRefused, load_policy, replace_kind
from domo_core.policy.rights import load_rights
from domo_core.policy.roles import Actor
from domo_core.prompts import ToolAside, build_system_prompt
from domo_core.toolsdb.spaces import space_for
from domo_core.toolsdb.store import BaseRows, ToolsBase
from fakes import FakeAsk, FakeCall, FakeChatModel, FakeRefused, FakeSandbox
from sqlalchemy import select, update

PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
    "required": ["a", "b"],
    "additionalProperties": False,
}
CODE = "def run(entry):\n    a = entry['arguments']\n    return {'sum': a['a'] + a['b']}\n"
EXAMPLES: list[Any] = [{"entry": {"a": 1, "b": 2}, "expected": {"sum": 3}}]
PURPOSE = "Add two numbers a and b."
FINE = "fine: it adds the two numbers"

_ids = iter(range(1, 1_000_000))
# The pace of a build at its start (``madetools.settings``).
PACED_AFTER = SETTINGS["free_rounds"].start
FIRST_PAUSE = timedelta(seconds=SETTINGS["first_pause_seconds"].start)
LONGEST_PAUSE = timedelta(seconds=SETTINGS["longest_pause_seconds"].start)
WAITED_A_DAY = waited_too_long(SETTINGS["give_up_hours"].start)


NO_RISKS: dict[str, bool] = {
    "costs": False,
    "irreversible": False,
    "physical": False,
    "outward": False,
}
# What the tool of the examples does: one operation, no risk.
OPERATIONS: dict[str, Any] = {"add": {"says": "Adds two numbers.", "risks": []}}
_LEFT_OUT = object()


def submit(
    code: str = CODE, *, name: str = "add", operations: Any = OPERATIONS, **more: Any
) -> ToolCall:
    arguments = {
        "name": name,
        "description": "Adds two numbers.",
        "parameters": PARAMETERS,
        "code": code,
        **({} if operations is _LEFT_OUT else {"operations": operations}),
        **more,
    }
    return ToolCall(f"make-{next(_ids)}", "submit", json.dumps(arguments))


def give_up(reason: str) -> ToolCall:
    return ToolCall(f"make-{next(_ids)}", "give_up", json.dumps({"reason": reason}))


class Maker:
    """The model of the builds: each call of the model that writes takes the next answer (a call
    of ``submit`` or ``give_up``, a text: nothing submitted, a ``ModelCallError``: the call fails
    after a first piece of text, a number: the call takes that many seconds; once they are used
    up, the call fails: a wait, so that a build with no cap on its rounds stops there);
    each call of the reviewer takes the next verdict (``None``: the call fails). What each one
    received is in ``making`` and ``reviewing``."""

    def __init__(
        self,
        *answers: ToolCall | str | ModelCallError | float,
        reviews: Sequence[str | None] = (),
    ) -> None:
        self.answers = list(answers)
        self.reviews = list(reviews)
        self.making: list[FakeCall] = []
        self.reviewing: list[FakeCall] = []

    async def stream(
        self,
        *,
        system: str,
        messages: Sequence[HistoryItem],
        max_output_tokens: int,
        tools: Sequence[ToolDefinition] = (),
        tool_choice: ToolChoice = "auto",
        think: bool = True,
    ) -> AsyncGenerator[ChatChunk | ToolCall | ChatUsage]:
        call = FakeCall(system, list(messages), tuple(tools), max_output_tokens, tool_choice)
        if system == review_prompt():
            self.reviewing.append(call)
            verdict = self.reviews.pop(0) if self.reviews else FINE
            if verdict is None:
                raise ModelCallError("upstream")
            yield ChatChunk(verdict)
        elif system == toolmaking_prompt():
            self.making.append(call)
            answer = self.answers.pop(0) if self.answers else ModelCallError("upstream")
            if isinstance(answer, ModelCallError):
                if answer.kind == "limit_reached":
                    raise answer
                yield ChatChunk("Let me")
                raise answer
            if isinstance(answer, float):
                await asyncio.sleep(answer)
                answer = "too late"
            yield answer if isinstance(answer, ToolCall) else ChatChunk(answer)
        else:
            yield ChatChunk("ok")
        yield ChatUsage(3, 2)

    def told(self, index: int = -1) -> dict[str, Any]:
        """What the model that writes was given at its ``index``-th call."""
        message = self.making[index].messages[0]
        assert isinstance(message, ChatMessage)
        return json.loads(message.content)


def summing(request: RunRequest) -> RunResult:
    arguments = request.entry["arguments"]
    assert isinstance(arguments, dict)
    total = int(arguments.get("a", 0)) + int(arguments.get("b", 0))  # type: ignore[arg-type]
    return RunResult(status="done", output={"sum": total}, seconds=0.1)


@pytest.fixture
def sandbox(chat_server: ChatServer) -> FakeSandbox:
    chat_server.sandbox = FakeSandbox(summing)
    return chat_server.sandbox


def use(server: ChatServer, maker: Maker) -> Maker:
    server.model = maker
    return maker


async def run_the_pass(server: ChatServer) -> None:
    """One pass of the builds at the server's time, and the rounds it started run to their end."""
    await toolmaking_pass(app_turn_deps(server.app))(server.clock())
    await server.wait_for_wakes()


async def opened(
    server: ChatServer,
    person: Person,
    *,
    name: str = "add",
    tool_id: uuid.UUID | None = None,
    purpose: str = PURPOSE,
) -> uuid.UUID:
    async with server.session_factory() as session:
        build = await open_build(
            session,
            name=name,
            purpose=purpose,
            opened_by=person.id,
            examples=EXAMPLES,
            now=server.clock(),
            tool_id=tool_id,
        )
        await session.commit()
        return build.id


async def build_of(server: ChatServer, build_id: uuid.UUID) -> ToolBuild:
    async with server.session_factory() as session:
        build = await session.get(ToolBuild, build_id)
        assert build is not None
        return build


async def tools(server: ChatServer) -> list[MadeTool]:
    async with server.session_factory() as session:
        return list((await session.scalars(select(MadeTool).order_by(MadeTool.created_at))).all())


async def versions_of(server: ChatServer, build_id: uuid.UUID) -> list[MadeToolVersion]:
    async with server.session_factory() as session:
        found = await session.scalars(
            select(MadeToolVersion)
            .where(MadeToolVersion.build_id == build_id)
            .order_by(MadeToolVersion.created_at, MadeToolVersion.id)
        )
        return list(found.all())


async def kind_of(server: ChatServer, name: str) -> ActionKind | None:
    async with server.session_factory() as session:
        return await session.get(ActionKind, f"made.{name}")


async def audit_of(server: ChatServer, *actions: str) -> list[AuditEntry]:
    async with server.session_factory() as session:
        found = await session.scalars(
            select(AuditEntry).where(AuditEntry.action.in_(actions)).order_by(AuditEntry.id)
        )
        return list(found.all())


async def add_ready_tool(server: ChatServer, *, name: str = "add", code: str = CODE) -> MadeTool:
    """A ready tool ``name`` with one version and its kind (committed)."""
    async with server.session_factory() as writer:
        tool = MadeTool(name=name, description="Adds.", parameters=PARAMETERS)
        writer.add(tool)
        await writer.flush()
        version = MadeToolVersion(
            tool_id=tool.id, fingerprint=fingerprint(code), code=code, state="ready"
        )
        writer.add(version)
        await writer.flush()
        tool.current_version_id = version.id
        writer.add(
            ActionKind(
                action_type=f"made.{name}",
                default_roles=["owner", "adult", "child"],
                description="Adds.",
            )
        )
        await writer.commit()
        return tool


@pytest.fixture
async def alex(adult_member: Member) -> Person:
    return adult_member.person


# --- a build that passes -------------------------------------------------------------------------


async def test_a_first_submission_that_passes_makes_the_tool_its_kind_and_a_ready_build(
    chat_server: ChatServer, sandbox: FakeSandbox, alex: Person
):
    maker = use(chat_server, Maker(submit()))
    build_id = await opened(chat_server, alex)
    await run_the_pass(chat_server)
    build = await build_of(chat_server, build_id)
    assert (build.state, build.rounds) == ("ready", 1)
    [tool] = await tools(chat_server)
    assert (tool.name, tool.state, tool.operations) == ("add", "ready", OPERATIONS)
    # A submission that says neither its time nor its launches a day leaves the house's.
    assert (tool.description, tool.parameters, tool.settings) == (
        "Adds two numbers.",
        PARAMETERS,
        {},
    )
    assert build.tool_id == tool.id
    [version] = await versions_of(chat_server, build_id)
    assert (version.state, version.tool_id, version.code) == ("ready", tool.id, CODE)
    assert tool.current_version_id == version.id
    assert version.fingerprint == fingerprint(CODE)
    assert version.checks["passed"] is True
    # Its kind: Domo's, open to every role, guests too (the owner may close it), no risk.
    kind = await kind_of(chat_server, "add")
    assert kind is not None and kind.default_roles == ["owner", "adult", "child", "guest"]
    assert not (kind.costs or kind.irreversible or kind.physical or kind.outward)
    # One call of the model that writes, with the two tools, and one of the reviewer.
    [making] = maker.making
    assert [tool.name for tool in making.tools] == ["submit", "give_up"]
    told = maker.told()
    assert told["purpose"] == PURPOSE and told["examples"] == EXAMPLES
    assert (told["round"], told["name"]) == (1, "add") and "for" not in told
    assert "last_report" not in told and "current" not in told
    assert len(maker.reviewing) == 1
    # The dry run was given the name of whose turn opened the build, no data, and that it is one.
    assert sandbox.runs[0].entry["for"] == "Alex"
    assert set(sandbox.runs[0].entry) == {"arguments", "now", "for", "dry_run"}
    assert sandbox.runs[0].entry["dry_run"] is True


async def test_a_failure_to_forget_old_hand_runs_never_stops_the_pass_s_builds(
    chat_server: ChatServer,
    sandbox: FakeSandbox,
    alex: Person,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    """The pass forgets the programs run by hand a week ago first: a failure there is logged on
    its own, and the builds of the pass are taken all the same."""

    async def broken(*_: object) -> int:
        raise RuntimeError("the hand runs cannot be read")

    monkeypatch.setattr(upkeep, "forget_hand_runs", broken)
    use(chat_server, Maker(submit()))
    build_id = await opened(chat_server, alex)
    with caplog.at_level(logging.ERROR, logger=upkeep.__name__):
        await run_the_pass(chat_server)
    assert (await build_of(chat_server, build_id)).state == "ready"
    [record] = [r for r in caplog.records if r.name == upkeep.__name__]
    # Its kind alone, no traceback, as every other log of the tools (final review m7).
    assert record.getMessage() == "the programs run by hand could not be forgotten (RuntimeError)"
    assert record.exc_info is None


# --- rounds --------------------------------------------------------------------------------------


async def test_a_failing_then_a_passing_submission_make_one_tool_and_two_versions(
    chat_server: ChatServer, sandbox: FakeSandbox, alex: Person
):
    first = "import os\n" + CODE
    maker = use(chat_server, Maker(submit(first), submit()))
    build_id = await opened(chat_server, alex)
    await run_the_pass(chat_server)
    build = await build_of(chat_server, build_id)
    assert (build.state, build.rounds) == ("ready", 2)
    [tool] = await tools(chat_server)
    versions = await versions_of(chat_server, build_id)
    assert [(v.state, v.tool_id) for v in versions] == [("refused", tool.id), ("ready", tool.id)]
    assert tool.current_version_id == versions[1].id
    # The report of the failed round is in the next round's message.
    second = maker.told(1)
    assert second["round"] == 2
    report = second["last_report"]
    assert report["passed"] is False
    assert report["imports"] == ["line 1: import os is not allowed"]
    assert report["checks"]["imports"] == "fail" and report["checks"]["scan"] == "not_run"
    # The failed version was never run nor reviewed.
    assert len(sandbox.runs) == 1 and len(maker.reviewing) == 1


async def test_a_review_alert_is_told_with_its_reason_at_the_next_round(
    chat_server: ChatServer, sandbox: FakeSandbox, alex: Person
):
    maker = use(
        chat_server,
        Maker(submit(), submit(CODE + "# again\n"), reviews=["alert: it keeps the date", FINE]),
    )
    build_id = await opened(chat_server, alex)
    await run_the_pass(chat_server)
    assert (await build_of(chat_server, build_id)).state == "ready"
    report = maker.told(1)["last_report"]
    assert report["review"] == "alert" and report["review_reason"] == "it keeps the date"


async def test_the_same_code_as_a_version_refused_is_not_checked_again(
    chat_server: ChatServer, sandbox: FakeSandbox, alex: Person
):
    bad = "import os\n" + CODE
    maker = use(chat_server, Maker(submit(bad), submit(bad), submit()))
    build_id = await opened(chat_server, alex)
    await run_the_pass(chat_server)
    build = await build_of(chat_server, build_id)
    assert (build.state, build.rounds) == ("ready", 3)
    assert [v.state for v in await versions_of(chat_server, build_id)] == ["refused", "ready"]
    assert maker.told(2)["last_round"] == SAME_CODE
    assert len(await audit_of(chat_server, CHECKED)) == 2


async def test_an_answer_that_submits_nothing_is_a_round_told_to_the_next(
    chat_server: ChatServer, sandbox: FakeSandbox, alex: Person
):
    maker = use(chat_server, Maker("Let me think about it.", submit()))
    build_id = await opened(chat_server, alex)
    await run_the_pass(chat_server)
    build = await build_of(chat_server, build_id)
    assert (build.state, build.rounds) == ("ready", 2)
    assert maker.told(1)["last_round"] == NOTHING_SUBMITTED


async def test_give_up_abandons_the_build_with_its_reason(
    chat_server: ChatServer, sandbox: FakeSandbox, alex: Person
):
    use(chat_server, Maker(give_up("a calendar cannot be read from here")))
    build_id = await opened(chat_server, alex)
    await run_the_pass(chat_server)
    build = await build_of(chat_server, build_id)
    assert (build.state, build.outcome) == ("abandoned", "a calendar cannot be read from here")
    assert await tools(chat_server) == []
    [entry] = await audit_of(chat_server, ABANDONED)
    assert entry.details == {"tool": "add", "rounds": 1}


async def test_a_build_goes_past_25_rounds(
    chat_server: ChatServer, sandbox: FakeSandbox, alex: Person
):
    # No cap on the rounds: the budget bounds a build. 26 refused submissions, then the model is
    # out of reach (a wait), and the build is still being made.
    failing = [submit(f"import os\n# try {i}\n" + CODE) for i in range(26)]
    maker = use(chat_server, Maker(*failing))
    build_id = await opened(chat_server, alex)
    await run_the_pass(chat_server)
    # Past a few refused rounds in a row, each next one waits for its time.
    while len(maker.making) < 27:
        build = await build_of(chat_server, build_id)
        assert build.not_before is not None
        chat_server.clock.advance(build.not_before - chat_server.clock() + timedelta(seconds=1))
        await run_the_pass(chat_server)
    build = await build_of(chat_server, build_id)
    assert (build.state, build.rounds, build.outcome) == ("building", 26, None)
    assert len(maker.making) == 27
    assert await tools(chat_server) == []
    versions = await versions_of(chat_server, build_id)
    assert len(versions) == 26 and {v.state for v in versions} == {"refused"}
    assert {v.tool_id for v in versions} == {None}
    told = maker.told(25)
    assert told["round"] == 26 and "rounds_at_most" not in told
    assert not hasattr(building, "MAX_ROUNDS") and not hasattr(building, "TOO_MANY_ROUNDS")


async def wait_and_pass(server: ChatServer, build_id: uuid.UUID) -> None:
    """The clock moves past the wait of the build (``retried_after`` its waits), and a pass runs."""
    build = await build_of(server, build_id)
    server.clock.advance(retried_after(build.waits) + timedelta(seconds=1))
    await run_the_pass(server)


async def test_a_reviewer_that_does_not_answer_costs_one_submission_whatever_the_passes(
    chat_server: ChatServer, sandbox: FakeSandbox, alex: Person
):
    maker = use(chat_server, Maker(submit(), reviews=[None, None, None, FINE]))
    build_id = await opened(chat_server, alex)
    await run_the_pass(chat_server)
    build = await build_of(chat_server, build_id)
    # The model was asked: the round counts; the submission waits for its checks.
    assert (build.state, build.rounds, build.waits) == ("building", 1, 1)
    [version] = await versions_of(chat_server, build_id)
    assert version.state == "checking" and version.code == CODE
    assert await audit_of(chat_server, CHECKED) == []
    # Fresh, it is nobody's to take again.
    await run_the_pass(chat_server)
    assert len(maker.reviewing) == 1
    # Two more passes, later and later: the same submission is checked again, never rewritten.
    await wait_and_pass(chat_server, build_id)
    await wait_and_pass(chat_server, build_id)
    build = await build_of(chat_server, build_id)
    assert (build.state, build.rounds, build.waits) == ("building", 1, 3)
    assert len(maker.making) == 1 and len(maker.reviewing) == 3
    # The reviewer answers: the very same submission becomes the tool.
    await wait_and_pass(chat_server, build_id)
    build = await build_of(chat_server, build_id)
    assert (build.state, build.rounds, build.waits) == ("ready", 1, 0)
    assert len(maker.making) == 1
    [version] = await versions_of(chat_server, build_id)
    assert version.state == "ready"
    [tool] = await tools(chat_server)
    assert tool.current_version_id == version.id and tool.description == "Adds two numbers."


async def test_a_sandbox_that_does_not_answer_costs_one_submission_too(
    chat_server: ChatServer, alex: Person
):
    class Down(FakeSandbox):
        down = True

        async def scan(self, code: str) -> Any:
            if self.down:
                from domo_core.sandbox_client import SandboxUnavailable

                raise SandboxUnavailable("down")
            return await super().scan(code)

    sandbox = Down(summing)
    chat_server.sandbox = sandbox
    maker = use(chat_server, Maker(submit()))
    build_id = await opened(chat_server, alex)
    await run_the_pass(chat_server)
    await wait_and_pass(chat_server, build_id)
    await wait_and_pass(chat_server, build_id)
    build = await build_of(chat_server, build_id)
    assert (build.state, build.rounds, build.waits) == ("building", 1, 3)
    assert len(maker.making) == 1 and maker.reviewing == []
    sandbox.down = False
    await wait_and_pass(chat_server, build_id)
    assert (await build_of(chat_server, build_id)).state == "ready"
    assert len(maker.making) == 1


def test_a_build_that_waits_is_tried_again_later_and_later():
    assert [retried_after(waits) for waits in range(7)] == [
        timedelta(minutes=5),
        timedelta(minutes=5),
        timedelta(minutes=10),
        timedelta(minutes=20),
        timedelta(minutes=40),
        timedelta(hours=1),
        timedelta(hours=1),
    ]
    assert retried_after(10_000) == timedelta(hours=1)


async def test_a_submission_whose_settling_raises_is_counted_and_three_in_a_row_abandon(
    chat_server: ChatServer,
    sandbox: FakeSandbox,
    alex: Person,
    monkeypatch: pytest.MonkeyPatch,
):
    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("settling failed")

    monkeypatch.setattr(build_writes, "_made", broken)
    answers = [submit(CODE + f"# try {i}\n") for i in range(5)]
    maker = use(chat_server, Maker(*answers))
    build_id = await opened(chat_server, alex)
    await run_the_pass(chat_server)
    build = await build_of(chat_server, build_id)
    assert (build.state, build.rounds, build.outcome) == ("abandoned", 3, UNSETTLED_TOO_OFTEN)
    assert len(maker.making) == 3
    assert maker.told(1)["last_round"] == UNSETTLED
    assert maker.told(1)["last_report"] == {"passed": False, "unsettled": UNSETTLED}
    versions = await versions_of(chat_server, build_id)
    assert [v.state for v in versions] == ["refused"] * 3
    assert await tools(chat_server) == []


async def test_a_submission_whose_settling_raised_once_may_be_submitted_again(
    chat_server: ChatServer,
    sandbox: FakeSandbox,
    alex: Person,
    monkeypatch: pytest.MonkeyPatch,
):
    real = build_writes._made
    failed: list[bool] = []

    async def once(*args: Any, **kwargs: Any) -> Any:
        if not failed:
            failed.append(True)
            raise RuntimeError("settling failed")
        return await real(*args, **kwargs)

    monkeypatch.setattr(build_writes, "_made", once)
    # The same code again: a submission not settled keeps no digest, so it is checked again.
    maker = use(chat_server, Maker(submit(), submit()))
    build_id = await opened(chat_server, alex)
    await run_the_pass(chat_server)
    build = await build_of(chat_server, build_id)
    assert (build.state, build.rounds) == ("ready", 2)
    assert len(maker.making) == 2
    assert [v.state for v in await versions_of(chat_server, build_id)] == ["refused", "ready"]


async def test_a_build_left_building_by_a_restart_is_taken_again_once_stale(
    chat_server: ChatServer, sandbox: FakeSandbox, alex: Person
):
    maker = use(chat_server, Maker(submit()))
    build_id = await opened(chat_server, alex)
    now = chat_server.clock()
    async with chat_server.session_factory() as session:
        await session.execute(
            update(ToolBuild)
            .where(ToolBuild.id == build_id)
            .values(state="building", last_progress_at=now - timedelta(minutes=2))
        )
        await session.commit()
    await run_the_pass(chat_server)
    assert maker.making == [] and (await build_of(chat_server, build_id)).state == "building"
    async with chat_server.session_factory() as session:
        await session.execute(
            update(ToolBuild)
            .where(ToolBuild.id == build_id)
            .values(last_progress_at=now - timedelta(minutes=6))
        )
        await session.commit()
    await run_the_pass(chat_server)
    assert (await build_of(chat_server, build_id)).state == "ready"


async def test_a_build_is_claimed_once(chat_server: ChatServer, alex: Person):
    build_id = await opened(chat_server, alex)
    deps = app_turn_deps(chat_server.app)
    now = chat_server.clock()
    assert await claim(deps, build_id, now) is True
    assert await claim(deps, build_id, now) is False
    assert await claim(deps, build_id, now + timedelta(minutes=6)) is True


async def test_without_a_key_the_build_waits_and_nothing_is_counted(
    chat_server: ChatServer, sandbox: FakeSandbox, alex: Person
):
    maker = use(chat_server, Maker(submit()))
    build_id = await opened(chat_server, alex)
    async with chat_server.session_factory() as session:
        from domo_core.llm.models import Provider

        await session.execute(update(Provider).values(is_general=False))
        await session.commit()
    await run_the_pass(chat_server)
    build = await build_of(chat_server, build_id)
    assert (build.state, build.rounds, build.waits) == ("building", 0, 1)
    assert maker.making == []


# --- names ---------------------------------------------------------------------------------------


async def test_a_name_taken_by_another_tool_may_be_changed_and_the_build_keeps_the_new_one(
    chat_server: ChatServer, sandbox: FakeSandbox, alex: Person, owner_member: Member
):
    maker = use(chat_server, Maker(submit(name="add"), submit(name="add_two")))
    build_id = await opened(chat_server, alex)
    # Meanwhile another tool took the name.
    await add_ready_tool(chat_server, name="add")
    await run_the_pass(chat_server)
    assert maker.told(0)["name_taken"] is True
    assert maker.told(1)["last_report"]["imports"] == ["the name add is taken: choose another"]
    build = await build_of(chat_server, build_id)
    assert (build.state, build.name) == ("ready", "add_two")
    assert {tool.name for tool in await tools(chat_server)} == {"add", "add_two"}


async def test_a_name_other_than_the_one_asked_is_refused_while_it_is_free(
    chat_server: ChatServer, sandbox: FakeSandbox, alex: Person
):
    maker = use(chat_server, Maker(submit(name="other"), submit()))
    build_id = await opened(chat_server, alex)
    await run_the_pass(chat_server)
    assert maker.told(1)["last_report"]["imports"] == ["keep the name asked: add"]
    assert (await build_of(chat_server, build_id)).name == "add"


async def test_a_name_a_removed_tool_freed_is_built_again_with_a_new_kind(
    chat_server: ChatServer, sandbox: FakeSandbox, alex: Person, owner_member: Member
):
    old = await add_ready_tool(chat_server, name="add")
    async with chat_server.session_factory() as session:
        await session.execute(update(MadeTool).where(MadeTool.id == old.id).values(state="removed"))
        await session.commit()
    use(chat_server, Maker(submit()))
    await opened(chat_server, alex)
    await run_the_pass(chat_server)
    made = [tool for tool in await tools(chat_server) if tool.state == "ready"]
    assert [tool.name for tool in made] == ["add"]
    kind = await kind_of(chat_server, "add")
    assert kind is not None and kind.default_roles == ["owner", "adult", "child", "guest"]


async def test_the_kind_of_a_tool_in_use_is_never_replaced(chat_server: ChatServer, alex: Person):
    tool = await add_ready_tool(chat_server, name="add")
    kind = OpenKind(
        action_type="made.add",
        costs=False,
        irreversible=False,
        physical=False,
        outward=False,
        facts={},
        default_roles=frozenset({"adult"}),
        description="Adds.",
    )
    async with chat_server.session_factory() as session:
        with pytest.raises(KindRefused, match="in use"):
            await replace_kind(session, kind, now=chat_server.clock())
        await session.execute(
            update(MadeTool).where(MadeTool.id == tool.id).values(state="removed")
        )
        await replace_kind(session, kind, now=chat_server.clock())
        await session.commit()
    replaced = await kind_of(chat_server, "add")
    assert replaced is not None and replaced.default_roles == ["adult"]


# --- a new version -------------------------------------------------------------------------------


async def test_a_change_makes_a_new_version_and_keeps_the_tool_and_its_kind(
    chat_server: ChatServer, sandbox: FakeSandbox, alex: Person
):
    tool = await add_ready_tool(chat_server, name="add")
    async with chat_server.session_factory() as session:
        await session.execute(
            update(MadeTool)
            .where(MadeTool.id == tool.id)
            .values(state="set_aside", aside_reason="it failed 3 times in a row")
        )
        await session.commit()
    new_code = CODE + "# a better version\n"
    maker = use(chat_server, Maker(submit(name="other"), submit(new_code, max_seconds=120)))
    build_id = await opened(chat_server, alex, tool_id=tool.id, purpose="Add, better.")
    await run_the_pass(chat_server)
    first = maker.told(0)
    # Only what the tool carries of its own (final review m1): none here. The house's values are
    # in bounds, apart, so that copying current back pins nothing on the tool.
    assert first["current"] == {
        "description": "Adds.",
        "parameters": PARAMETERS,
        "operations": {},
        "max_seconds": None,
        "launches_per_day": None,
        "code": CODE,
    }
    assert (first["bounds"]["run_seconds"], first["bounds"]["launches_per_day"]) == (60, 200)
    assert maker.told(1)["last_report"]["imports"] == ["a new version keeps the tool's name: add"]
    build = await build_of(chat_server, build_id)
    assert (build.state, build.tool_id) == ("ready", tool.id)
    [changed] = await tools(chat_server)
    assert changed.id == tool.id and changed.name == "add"
    assert (changed.state, changed.aside_reason, changed.settings) == (
        "ready",
        None,
        {"run_seconds": 120},
    )
    async with chat_server.session_factory() as session:
        current = await session.get(MadeToolVersion, changed.current_version_id)
        all_versions = await session.scalars(
            select(MadeToolVersion.state).where(MadeToolVersion.tool_id == tool.id)
        )
        states = sorted(all_versions.all())
    assert current is not None and current.code == new_code
    assert states == ["ready", "ready", "refused"]
    kind = await kind_of(chat_server, "add")
    assert kind is not None and kind.default_roles == ["owner", "adult", "child"]
    [built] = await audit_of(chat_server, BUILT)
    assert built.details == {
        "tool": "add",
        "rounds": 2,
        "change": True,
        "risks": NO_RISKS,
        "risks_before": NO_RISKS,
    }


# --- whoever asked for it ------------------------------------------------------------------------


async def test_a_build_whose_opener_left_goes_on_paid_by_the_owner(
    chat_server: ChatServer, sandbox: FakeSandbox, alex: Person, owner_member: Member
):
    use(chat_server, Maker(submit()))
    build_id = await opened(chat_server, alex)
    async with chat_server.session_factory() as session:
        person = await session.get(Person, alex.id)
        assert person is not None
        await disable(session, person, chat_server.clock(), by=owner_member.person.id)
        await session.commit()
    await run_the_pass(chat_server)
    # A tool is Domo's: whoever asked for it leaving ends nothing.
    assert (await build_of(chat_server, build_id)).state == "ready"
    [tool] = await tools(chat_server)
    assert tool.state == "ready"
    kind = await kind_of(chat_server, "add")
    assert kind is not None and kind.default_roles == ["owner", "adult", "child", "guest"]
    # The dry run ran for nobody named: the one who opened it is gone.
    assert sandbox.runs[0].entry["for"] is None


# --- what the audit and the context keep ---------------------------------------------------------


async def test_the_audit_keeps_each_check_and_never_the_code(
    chat_server: ChatServer, sandbox: FakeSandbox, alex: Person
):
    sandbox.findings = [ScanFinding(test_id="B311", severity="LOW", line=1, text="random")]
    use(chat_server, Maker(submit("import os\n" + CODE), submit()))
    await opened(chat_server, alex)
    await run_the_pass(chat_server)
    first, second = await audit_of(chat_server, CHECKED)
    assert first.actor == "system"
    assert first.details == {
        "tool": "add",
        "round": 1,
        "parameters": "pass",
        "imports": "fail",
        "scan": "not_run",
        "dry_runs": "not_run",
        "review": "not_run",
        "parameter_findings": 0,
        "import_findings": 1,
        "scan_findings": 0,
        "dry_runs_made": 0,
        "dry_runs_passed": 0,
    }
    assert {key: second.details[key] for key in ("parameters", "imports", "scan", "dry_runs")} == {
        "parameters": "pass",
        "imports": "pass",
        "scan": "pass",
        "dry_runs": "pass",
    }
    assert second.details["review"] == "pass"
    [built] = await audit_of(chat_server, BUILT)
    assert built.details == {"tool": "add", "rounds": 2, "change": False, "risks": NO_RISKS}
    everything = json.dumps(
        [entry.details for entry in await audit_of(chat_server, CHECKED, BUILT)]
    )
    for secret_of_the_build in ("def run", "import os", PURPOSE, "it adds the two numbers"):
        assert secret_of_the_build not in everything


async def test_a_build_abandoned_lately_is_in_the_context_of_whoever_opened_it(
    chat_server: ChatServer, sandbox: FakeSandbox, alex: Person, owner_member: Member
):
    use(chat_server, Maker(give_up("no way to read the weather")))
    build_id = await opened(chat_server, alex)
    await run_the_pass(chat_server)
    async with chat_server.session_factory() as session:
        theirs = await set_aside_for(session, alex, chat_server.clock())
        others = await set_aside_for(session, owner_member.person, chat_server.clock())
        later = await set_aside_for(session, alex, chat_server.clock() + timedelta(hours=25))
    assert theirs == (ToolAside("t_add", "no way to read the weather", abandoned=True),)
    assert others == () and later == ()
    prompt = build_system_prompt(display_name="Alex", role="adult", can_act=True, aside=theirs)
    line = next(line for line in prompt.splitlines() if "gave up making" in line)
    assert line.endswith('[{"tool": "t_add", "why": "no way to read the weather"}]')
    assert "set aside, no longer usable" not in prompt
    assert (await build_of(chat_server, build_id)).state == "abandoned"


async def test_a_turn_names_the_build_abandoned_by_its_own_clock(
    chat_server: ChatServer, sandbox: FakeSandbox, adult_chat: ChatPilot
):
    alex = adult_chat.person
    use(chat_server, Maker(give_up("no way to read the weather")))
    await opened(chat_server, alex)
    await run_the_pass(chat_server)
    adult_chat.use_model(FakeChatModel(["ok"]))
    await adult_chat.say("Hello")
    assert "no way to read the weather" in chat_server.calls[-1].system
    # A day later by the turn's clock, it is no longer named.
    chat_server.clock.advance(timedelta(hours=25))
    await adult_chat.say("Hello again")
    assert "no way to read the weather" not in chat_server.calls[-1].system


# --- round 2: nothing paid goes uncounted ---------------------------------------------------------


async def test_a_submission_with_a_nul_character_is_a_counted_mistake_of_its_form(
    chat_server: ChatServer, sandbox: FakeSandbox, alex: Person
):
    maker = use(chat_server, Maker(submit(CODE + "# \x00\n"), submit()))
    build_id = await opened(chat_server, alex)
    await run_the_pass(chat_server)
    build = await build_of(chat_server, build_id)
    assert (build.state, build.rounds) == ("ready", 2)
    assert "NUL" in maker.told(1)["last_round"]


async def test_a_submission_that_cannot_be_kept_counts_and_three_in_a_row_abandon(
    chat_server: ChatServer,
    sandbox: FakeSandbox,
    alex: Person,
    monkeypatch: pytest.MonkeyPatch,
):
    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("the database refused it")

    monkeypatch.setattr(building, "keep", broken)
    maker = use(chat_server, Maker(*(submit(CODE + f"# {i}\n") for i in range(5))))
    build_id = await opened(chat_server, alex)
    await run_the_pass(chat_server)
    build = await build_of(chat_server, build_id)
    assert (build.state, build.rounds, build.outcome) == ("abandoned", 3, UNSETTLED_TOO_OFTEN)
    assert len(maker.making) == 3
    assert maker.told(1)["last_round"] == UNSETTLED


async def test_a_call_that_fails_mid_stream_or_takes_too_long_is_a_wait_not_a_round(
    chat_server: ChatServer,
    sandbox: FakeSandbox,
    alex: Person,
):
    # How long one call may take is Domo's making_seconds (final review, ruling R33).
    async with chat_server.session_factory() as writer:
        await set_house(writer, {"making_seconds": 1})
        await writer.commit()
    maker = use(chat_server, Maker(ModelCallError("upstream"), 3.0, submit()))
    build_id = await opened(chat_server, alex)
    await run_the_pass(chat_server)
    # Neither counts as a round: each is a wait, and the next is tried after its back-off.
    build = await build_of(chat_server, build_id)
    assert (build.state, build.rounds, build.waits) == ("building", 0, 1)
    await wait_and_pass(chat_server, build_id)
    build = await build_of(chat_server, build_id)
    assert (build.state, build.rounds, build.waits) == ("building", 0, 2)
    await wait_and_pass(chat_server, build_id)
    build = await build_of(chat_server, build_id)
    assert (build.state, build.rounds, build.waits) == ("ready", 1, 0)
    assert len(maker.making) == 3
    # The answer that arrived was told it is the first round.
    assert maker.told(2)["round"] == 1


async def test_an_outage_of_the_provider_spends_no_rounds_in_a_row(
    chat_server: ChatServer, sandbox: FakeSandbox, alex: Person
):
    outages = [ModelCallError(kind) for kind in ("unreachable", "rate_limited", "auth", "upstream")]
    maker = use(chat_server, Maker(*outages, submit()))
    build_id = await opened(chat_server, alex)
    # A pass, then passes before the back-off is over: one call, never 25 in one task.
    await run_the_pass(chat_server)
    for _ in range(3):
        chat_server.clock.advance(timedelta(minutes=1))
        await run_the_pass(chat_server)
    assert len(maker.making) == 1
    build = await build_of(chat_server, build_id)
    assert (build.state, build.rounds, build.waits) == ("building", 0, 1)
    # Each call that does not arrive is a wait, spread over the back-off, and no round.
    for waits in (2, 3, 4):
        await wait_and_pass(chat_server, build_id)
        build = await build_of(chat_server, build_id)
        assert (build.state, build.rounds, build.waits) == ("building", 0, waits)
    assert len(maker.making) == 4
    # Then an answer arrives: the tool is built, in its first round.
    await wait_and_pass(chat_server, build_id)
    build = await build_of(chat_server, build_id)
    assert (build.state, build.rounds, build.waits) == ("ready", 1, 0)
    assert maker.told(4)["round"] == 1


async def test_a_build_that_waited_24_hours_is_abandoned(
    chat_server: ChatServer, sandbox: FakeSandbox, alex: Person
):
    maker = use(chat_server, Maker(*(ModelCallError("unreachable") for _ in range(40))))
    build_id = await opened(chat_server, alex)
    await run_the_pass(chat_server)
    while (await build_of(chat_server, build_id)).state == "building":
        await wait_and_pass(chat_server, build_id)
    build = await build_of(chat_server, build_id)
    # The same sentence as a build whose checks could not run for a day: an outage is never told
    # as a tool that did not pass its checks, and spends no round.
    assert (build.state, build.outcome, build.rounds) == ("abandoned", WAITED_A_DAY, 0)
    assert build.waited_seconds >= 24 * 3600
    # About thirty calls in the day, spaced by the back-off.
    assert 25 < len(maker.making) < 35


async def test_after_a_long_outage_the_model_still_has_all_its_rounds(
    chat_server: ChatServer, sandbox: FakeSandbox, alex: Person
):
    maker = use(chat_server, Maker(*(ModelCallError("upstream") for _ in range(26)), submit()))
    build_id = await opened(chat_server, alex)
    await run_the_pass(chat_server)
    while len(maker.making) < 27:
        await wait_and_pass(chat_server, build_id)
    build = await build_of(chat_server, build_id)
    # 26 calls that did not arrive, then one that did: told round 1, and built.
    assert (build.state, build.rounds) == ("ready", 1)
    told = maker.told(26)
    assert told["round"] == 1 and "rounds_at_most" not in told


async def test_a_call_refused_before_it_is_sent_waits_uncounted(
    chat_server: ChatServer, sandbox: FakeSandbox, alex: Person
):
    use(chat_server, Maker(ModelCallError("limit_reached")))
    build_id = await opened(chat_server, alex)
    await run_the_pass(chat_server)
    build = await build_of(chat_server, build_id)
    assert (build.state, build.rounds, build.waits) == ("building", 0, 1)


async def test_a_build_whose_checks_cannot_run_for_a_day_is_abandoned_and_its_submission_refused(
    chat_server: ChatServer, sandbox: FakeSandbox, alex: Person
):
    maker = use(chat_server, Maker(submit(), reviews=[None] * 50))
    build_id = await opened(chat_server, alex)
    await run_the_pass(chat_server)
    for _ in range(4):
        await wait_and_pass(chat_server, build_id)
    build = await build_of(chat_server, build_id)
    assert build.state == "building" and 0 < build.waited_seconds < 24 * 3600
    # Hourly probes, until it has waited a day in all.
    while (await build_of(chat_server, build_id)).state == "building":
        await wait_and_pass(chat_server, build_id)
    build = await build_of(chat_server, build_id)
    assert (build.state, build.outcome) == ("abandoned", WAITED_A_DAY)
    assert build.waited_seconds >= 24 * 3600
    assert len(maker.making) == 1 and len(maker.reviewing) < 40
    [version] = await versions_of(chat_server, build_id)
    assert version.state == "refused" and version.checks["not_checked"]
    [entry] = await audit_of(chat_server, ABANDONED)
    assert entry.details["tool"] == "add"


async def test_removing_a_tool_refuses_the_submission_of_its_build_that_waits(
    chat_server: ChatServer, sandbox: FakeSandbox, adult_chat: ChatPilot
):
    alex = adult_chat.person
    tool = await add_ready_tool(chat_server, name="add")
    use(chat_server, Maker(submit(CODE + "# better\n"), reviews=[None]))
    build_id = await opened(chat_server, alex, tool_id=tool.id)
    await run_the_pass(chat_server)
    [pending] = await versions_of(chat_server, build_id)
    assert pending.state == "checking"
    reply = await adult_chat.say_with_tool("tool_remove", {"name": "add"})
    assert reply.actions[0]["status"] == "done"
    build = await build_of(chat_server, build_id)
    assert (build.state, build.outcome) == ("abandoned", "the tool was removed")
    [version] = await versions_of(chat_server, build_id)
    assert version.state == "refused"


# --- round 3 ------------------------------------------------------------------------------------


async def test_a_submission_nested_too_deep_is_a_counted_mistake_of_its_form(
    chat_server: ChatServer, sandbox: FakeSandbox, alex: Person
):
    deep: Any = "x"
    for _ in range(900):
        deep = {"k": deep}
    arguments = {
        "name": "add",
        "description": "Adds two numbers.",
        "parameters": PARAMETERS,
        "code": CODE,
        "extra": deep,
    }
    nested = ToolCall("make-deep", "submit", json.dumps(arguments))
    maker = use(chat_server, Maker(nested, submit()))
    build_id = await opened(chat_server, alex)
    await run_the_pass(chat_server)
    build = await build_of(chat_server, build_id)
    assert (build.state, build.rounds) == ("ready", 2)
    assert "nest more than" in maker.told(1)["last_round"]


async def test_reading_the_answer_that_raises_counts_the_round(
    chat_server: ChatServer,
    sandbox: FakeSandbox,
    alex: Person,
    monkeypatch: pytest.MonkeyPatch,
):
    def broken(raw: str) -> Any:
        raise RecursionError("too deep")

    monkeypatch.setattr(building, "_submission", broken)
    maker = use(chat_server, Maker(*(submit(CODE + f"# {i}\n") for i in range(5))))
    build_id = await opened(chat_server, alex)
    await run_the_pass(chat_server)
    build = await build_of(chat_server, build_id)
    assert (build.state, build.rounds, build.outcome) == ("abandoned", 3, UNSETTLED_TOO_OFTEN)
    assert len(maker.making) == 3


async def test_a_core_that_was_down_does_not_count_as_a_build_s_wait(
    chat_server: ChatServer, sandbox: FakeSandbox, alex: Person
):
    maker = use(chat_server, Maker(submit(), reviews=[None, None, FINE]))
    build_id = await opened(chat_server, alex)
    await run_the_pass(chat_server)
    # The core is down for two days, then comes back.
    chat_server.clock.advance(timedelta(days=2))
    await run_the_pass(chat_server)
    build = await build_of(chat_server, build_id)
    assert (build.state, build.waits) == ("building", 2)
    assert build.waited_seconds <= retried_after(1).total_seconds()
    await wait_and_pass(chat_server, build_id)
    assert (await build_of(chat_server, build_id)).state == "ready"
    assert len(maker.making) == 1


# --- operations, launches per day, and dry runs that touch nothing (Task 11) ---------------------

ORDER_OPERATIONS: dict[str, Any] = {
    "order": {"says": "Orders the pizzas.", "risks": ["costs"]},
    "cancel": {"says": "Cancels an order at the shop.", "risks": ["irreversible", "outward"]},
    "follow": {"says": "Reads where the order is.", "risks": []},
}


async def test_operations_are_kept_and_the_kinds_risks_are_their_union(
    chat_server: ChatServer, sandbox: FakeSandbox, alex: Person
):
    use(chat_server, Maker(submit(operations=ORDER_OPERATIONS)))
    await opened(chat_server, alex)
    await run_the_pass(chat_server)
    [tool] = await tools(chat_server)
    assert tool.operations == ORDER_OPERATIONS
    kind = await kind_of(chat_server, "add")
    assert kind is not None
    assert (kind.costs, kind.irreversible, kind.physical, kind.outward) == (True, True, False, True)
    assert kind.default_roles == ["owner", "adult", "child", "guest"]


async def test_a_submission_without_operations_is_refused(
    chat_server: ChatServer, sandbox: FakeSandbox, alex: Person
):
    wrong = {"add": {"says": "Adds.", "risks": ["cheap"]}}
    maker = use(
        chat_server,
        Maker(
            submit(operations=_LEFT_OUT),
            submit(CODE + "# 2\n", operations=wrong),
            submit(CODE + "# 3\n", operations={}),
            submit(),
        ),
    )
    build_id = await opened(chat_server, alex)
    await run_the_pass(chat_server)
    build = await build_of(chat_server, build_id)
    assert (build.state, build.rounds) == ("ready", 4)
    for index in (1, 2, 3):
        report = maker.told(index)["last_report"]
        assert report["passed"] is False and report["checks"]["imports"] == "fail"
        assert any(finding.startswith("operations: ") for finding in report["imports"]), report
    # Refused before the sandbox: only the last one ran.
    assert len(sandbox.runs) == 1
    assert [v.state for v in await versions_of(chat_server, build_id)] == [
        "refused",
        "refused",
        "refused",
        "ready",
    ]


async def test_launches_per_day_is_kept_on_the_tool(
    chat_server: ChatServer, sandbox: FakeSandbox, alex: Person
):
    maker = use(
        chat_server,
        Maker(submit(launches_per_day=0), submit(CODE + "# 2\n", launches_per_day=50)),
    )
    await opened(chat_server, alex)
    await run_the_pass(chat_server)
    assert maker.told(1)["last_report"]["imports"] == ["launches_per_day is 1 at least"]
    [tool] = await tools(chat_server)
    assert tool.settings == {"launches_per_day": 50}
    # A new version may change it; one that does not say leaves it as it is.
    use(chat_server, Maker(submit(CODE + "# 3\n")))
    await opened(chat_server, alex, tool_id=tool.id)
    await run_the_pass(chat_server)
    [changed] = await tools(chat_server)
    assert changed.settings == {"launches_per_day": 50}
    use(chat_server, Maker(submit(CODE + "# 4\n", launches_per_day=20_000)))
    await opened(chat_server, alex, tool_id=tool.id)
    await run_the_pass(chat_server)
    [changed] = await tools(chat_server)
    # No ceiling: the tool's own setting.
    assert changed.settings == {"launches_per_day": 20_000}


async def test_the_reviewer_reads_the_operations(
    chat_server: ChatServer, sandbox: FakeSandbox, alex: Person
):
    maker = use(chat_server, Maker(submit(operations=ORDER_OPERATIONS)))
    await opened(chat_server, alex)
    await run_the_pass(chat_server)
    [review] = maker.reviewing
    [message] = review.messages
    assert isinstance(message, ChatMessage)
    case = json.loads(message.content)
    assert case["operations"] == ORDER_OPERATIONS
    assert set(case) == {
        "purpose",
        "description",
        "parameters",
        "operations",
        "open_addresses",
        "accounts",
        "code",
    }


# What a tool that goes out declares: the addresses and the accounts of its operations.
GOING_OUT: dict[str, Any] = {
    "add": {
        "says": "Adds two numbers, read from a device.",
        "risks": [],
        "accounts": ["lights"],
        "addresses": [{"host": "192.168.1.20", "port": 8080}, {"host": "lamps", "port": 80}],
    }
}


async def open_rows(server: ChatServer) -> list[OpenAddress]:
    async with server.session_factory() as session:
        found = await session.scalars(select(OpenAddress).order_by(OpenAddress.opened_at))
        return list(found.all())


async def test_a_build_that_names_an_address_the_tool_lacks_opens_nothing_and_says_which(
    chat_server: ChatServer, sandbox: FakeSandbox, alex: Person
):
    # Task 7: opening is Domo's act, never a build's; the build's result tells him what lacks.
    async with chat_server.session_factory() as session:
        await open_address(
            session,
            tool_name="add",
            host="192.168.1.20",
            port=8080,
            on_host=False,
            by="domo",
            now=chat_server.clock(),
        )
        await session.commit()
    maker = use(chat_server, Maker(submit(operations=GOING_OUT)))
    build_id = await opened(chat_server, alex)
    await run_the_pass(chat_server)
    build = await build_of(chat_server, build_id)
    assert build.state == "ready"
    [tool] = await tools(chat_server)
    assert tool.operations == GOING_OUT
    # Nothing was opened by the build.
    assert [(row.tool_name, row.host, row.port) for row in await open_rows(chat_server)] == [
        ("add", "192.168.1.20", 8080)
    ]
    assert build.outcome is not None
    assert "lamps:80" in build.outcome and "address_open" in build.outcome
    assert "192.168.1.20" not in build.outcome
    # The model that writes and the reviewer were told the addresses open for it.
    shown = [{"host": "192.168.1.20", "port": 8080, "on_host": False}]
    assert maker.told(0)["open_addresses"] == shown
    [review] = maker.reviewing
    [message] = review.messages
    assert isinstance(message, ChatMessage)
    case = json.loads(message.content)
    assert case["open_addresses"] == shown and case["accounts"] == ["lights"]
    # Audited by their number alone.
    [built] = await audit_of(chat_server, BUILT)
    assert built.details["missing_addresses"] == 1
    assert "lamps" not in json.dumps(built.details)


async def test_a_build_that_withdraws_and_lacks_addresses_keeps_the_withdrawal_whole(
    chat_server: ChatServer, sandbox: FakeSandbox, alex: Person
):
    # Fix round 1: the withdrawn notice is never cut by the addresses, which are said short
    # (tool_look lists them all), those the owner closed said apart: they take his yes.
    tool = await add_ready_tool(chat_server, name="add")
    before = {"add": {"says": "Adds two numbers.", "risks": []}}
    many = [{"host": f"device-number-{n}.home.arpa", "port": 8000 + n} for n in range(8)]
    after = {
        "add": {
            "says": "Adds two numbers, read from a device.",
            "risks": ["outward"],
            "addresses": many,
        },
    }
    now = chat_server.clock()
    async with chat_server.session_factory() as writer:
        await writer.execute(
            update(MadeTool).where(MadeTool.id == tool.id).values(operations=before)
        )
        writer.add(
            StandingAgreement(
                tool_id=tool.id,
                operation="add",
                for_person_id=alex.id,
                amount_cents_at_most=4000,
                times=1,
                per="week",
                given_by=alex.id,
            )
        )
        owner = (await writer.scalars(select(Person).where(Person.role == "owner"))).first()
        assert owner is not None
        # One of them the owner closed, for another tool: closed for this one too (ruling R20).
        await open_address(
            writer,
            tool_name="another",
            host="device-number-3.home.arpa",
            port=8003,
            on_host=False,
            by="domo",
            now=now,
        )
        await close_address(
            writer,
            tool_name="another",
            host="device-number-3.home.arpa",
            port=8003,
            by=str(owner.id),
            now=now,
        )
        await writer.commit()
    use(chat_server, Maker(submit(CODE + "# goes out\n", operations=after)))
    build_id = await opened(chat_server, alex, tool_id=tool.id)
    await run_the_pass(chat_server)
    build = await build_of(chat_server, build_id)
    assert build.state == "ready" and build.outcome is not None
    withdrawn = withdrawn_outcome({"operations": ["add"], "agreements": 1, "stops": 0})
    assert build.outcome.startswith(withdrawn)
    assert len(build.outcome) <= build_writes.MAX_OUTCOME
    rest = build.outcome[len(withdrawn) :]
    assert "8 addresses" in rest and "tool_look" in rest
    assert "device-number-3.home.arpa:8003" in rest and "owner" in rest


LACKING = [(f"device-number-{n}.home.arpa", 8000 + n) for n in range(3)]


def test_with_no_room_left_the_missing_addresses_say_nothing():
    # Fix round 2: a room of nothing, or less, gives nothing, never a head cut from its end.
    for room in (0, -1, -37):
        assert build_writes.missing_outcome("add", LACKING, set(), room) == ""
    assert len(build_writes.missing_outcome("add", LACKING, set(), 20)) <= 20


def test_an_outcome_whose_withdrawn_and_kept_notices_fill_the_bound_leaves_the_addresses_out():
    names = [f"operation_with_a_long_name_{n}" for n in range(6)]
    withdrawn = withdrawn_outcome({"operations": names, "agreements": 6, "stops": 0})
    kept = build_writes.settings_kept(["launches_per_day", "run_seconds"])
    said = build_writes.outcome_of(withdrawn, kept, "add", LACKING, {LACKING[0]})
    assert len(said) <= build_writes.MAX_OUTCOME
    assert said.startswith(withdrawn) and "device-number" not in said
    # Almost full: what room is left is used, within the bound.
    short = withdrawn_outcome({"operations": ["add"], "agreements": 1, "stops": 0})
    for kept_text in (kept, None):
        said = build_writes.outcome_of(short, kept_text, "add", LACKING, {LACKING[0]})
        assert len(said) <= build_writes.MAX_OUTCOME and said.startswith(short)
    # Nothing withdrawn nor kept: the addresses alone, whole.
    alone = build_writes.outcome_of(None, None, "add", LACKING, set())
    assert alone.startswith("built; 3 addresses") and len(alone) <= build_writes.MAX_OUTCOME


async def test_a_build_whose_addresses_are_all_open_says_nothing_of_them(
    chat_server: ChatServer, sandbox: FakeSandbox, alex: Person
):
    use(chat_server, Maker(submit()))
    build_id = await opened(chat_server, alex)
    await run_the_pass(chat_server)
    build = await build_of(chat_server, build_id)
    assert (build.state, build.outcome) == ("ready", None)
    [built] = await audit_of(chat_server, BUILT)
    assert "missing_addresses" not in built.details


# The table the dry runs below write in, made for real in the common space beforehand.
NOTES_TABLE = (
    "CREATE TABLE notes (id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY, what text NOT NULL)"
)


@pytest.fixture
async def base(chat_server: ChatServer, tools_base: ToolsBase) -> ToolsBase:
    """The tools' base of the server, with the table ``notes`` in its common space."""
    chat_server.app.state.tools_base = tools_base
    async with chat_server.session_factory() as session:
        login = await space_for(session, tools_base.admin, None)
        await session.commit()
    made = await tools_base.query(
        login, sql=NOTES_TABLE, params=[], write=True, space="common", bound_bytes=1 << 30
    )
    assert isinstance(made, BaseRows)
    return tools_base


async def notes_left(server: ChatServer, base: ToolsBase) -> list[Any]:
    async with server.session_factory() as session:
        login = await space_for(session, base.admin, None)
        await session.commit()
    rows = await base.query(
        login, sql="SELECT what FROM notes", params=[], write=False, space="common", bound_bytes=0
    )
    assert isinstance(rows, BaseRows)
    return [row["what"] for row in rows.rows]


def led(program: Any) -> FakeSandbox:
    return FakeSandbox(program=program)


async def test_a_dry_run_writes_nothing_that_stays(
    chat_server: ChatServer, base: ToolsBase, alex: Person
):
    async def keeps_a_note(request: RunRequest, ask: FakeAsk) -> Any:
        assert request.entry["dry_run"] is True
        await ask({"do": "begin", "operation": "add"})
        await ask({"do": "write", "sql": "INSERT INTO notes (what) VALUES ($1)", "params": ["x"]})
        # A second write sees the first: one transaction for the whole dry run.
        await ask({"do": "write", "sql": "INSERT INTO notes (what) VALUES ($1)", "params": ["y"]})
        rows = await ask({"do": "read", "sql": "SELECT what FROM notes ORDER BY id"})
        # The house has no corner: a dry run reads and writes the common space only.
        try:
            await ask({"do": "read", "sql": "SELECT 1", "space": "mine"})
        except FakeRefused as refused:
            assert refused.reason == "no_corner"
        return {"sum": 3, "rows": rows}

    chat_server.sandbox = led(keeps_a_note)
    examples: list[Any] = [
        {"entry": {"a": 1, "b": 2}, "expected": {"sum": 3, "rows": [{"what": "x"}, {"what": "y"}]}}
    ]
    use(chat_server, Maker(submit()))
    async with chat_server.session_factory() as session:
        build = await open_build(
            session,
            name="add",
            purpose=PURPOSE,
            opened_by=alex.id,
            examples=examples,
            now=chat_server.clock(),
        )
        await session.commit()
    await run_the_pass(chat_server)
    assert (await build_of(chat_server, build.id)).state == "ready"
    [version] = await versions_of(chat_server, build.id)
    assert version.checks["dry_runs"] == [{"example": 1, "status": "done", "matched": True}]
    assert await notes_left(chat_server, base) == []


async def test_a_dry_run_that_writes_before_any_begin_fails_as_it_would_for_real(
    chat_server: ChatServer, base: ToolsBase, alex: Person
):
    async def writes_first(request: RunRequest, ask: FakeAsk) -> Any:
        if "# begins first" in request.code:
            await ask({"do": "begin", "operation": "add"})
        await ask({"do": "write", "sql": "INSERT INTO notes (what) VALUES ('x')"})
        return {"sum": 3}

    chat_server.sandbox = led(writes_first)
    maker = use(chat_server, Maker(submit(), submit(CODE + "# begins first\n")))
    build_id = await opened(chat_server, alex)
    await run_the_pass(chat_server)
    report = maker.told(1)["last_report"]
    [run] = report["dry_runs"]
    assert (run["status"], run["matched"]) == ("error", False)
    assert "no_operation" in run["error"]
    assert (await build_of(chat_server, build_id)).state == "ready"
    assert await notes_left(chat_server, base) == []


async def test_a_dry_runs_actions_do_nothing(
    chat_server: ChatServer, sandbox: FakeSandbox, alex: Person
):
    other = await add_ready_tool(chat_server, name="other")
    soon = (chat_server.clock() + timedelta(hours=1)).isoformat()

    async def acts(request: RunRequest, ask: FakeAsk) -> Any:
        await ask({"do": "begin", "operation": "add"})
        said = [
            await ask(
                {
                    "do": "act",
                    "action": "tool.launch_when",
                    "arguments": {"tool": "other", "when": soon, "arguments": {"a": 1, "b": 2}},
                }
            ),
            await ask({"do": "tool", "name": "other", "arguments": {"a": 1, "b": 2}}),
            await ask({"do": "later", "at": soon, "arguments": {"a": 1, "b": 2}}),
            await ask({"do": "wake", "content": {"done": True}}),
        ]
        refused: list[str] = []
        for asked in (
            # What is refused for real by its form is refused in a dry run too.
            {"do": "later", "at": "2020-01-01T08:00:00+00:00", "arguments": {}},
            {"do": "tool", "name": "add", "arguments": {}},
            {"do": "tool", "name": "nothing", "arguments": {}},
            {"do": "act", "action": "note.leave", "arguments": {}},
        ):
            try:
                await ask(asked)
            except FakeRefused as no:
                refused.append(no.reason)
        return {"sum": 3, "said": said, "refused": refused}

    chat_server.sandbox = led(acts)
    examples: list[Any] = [
        {
            "entry": {"a": 1, "b": 2},
            "expected": {
                "sum": 3,
                "said": [None, None, None, None],
                "refused": ["past", "in_its_own_path", "no_such_tool", "not_allowed"],
            },
        }
    ]
    use(chat_server, Maker(submit()))
    async with chat_server.session_factory() as session:
        build = await open_build(
            session,
            name="add",
            purpose=PURPOSE,
            opened_by=alex.id,
            examples=examples,
            now=chat_server.clock(),
        )
        await session.commit()
    await run_the_pass(chat_server)
    assert (await build_of(chat_server, build.id)).state == "ready"
    # No nested run, no trigger, no wake, no stop, nothing declared, no launch audited, and no
    # entry of the barrier that would read as a real act.
    assert len(chat_server.sandbox.runs) == 1
    async with chat_server.session_factory() as session:
        for model in (MadeToolTrigger, LaunchStop, DeclaredSpending):
            assert (await session.scalars(select(model))).all() == [], model
        tool = await session.get(MadeTool, other.id)
        assert tool is not None and tool.launches_today == 0
    assert await audit_of(chat_server, LAUNCHED, "madetool.tool_refused") == []
    decided = await audit_of(chat_server, "policy.decision")
    ours = [e for e in decided if str(e.details["action_type"]).startswith(("program.", "made."))]
    assert ours == []


ORDERING = """import domo

def run(entry):
    domo.begin('order', 3250)
    domo.write('INSERT INTO notes (what) VALUES ($1)', ['pizzas'])
    domo.wake({'ordered': 'pizzas'})
    return {'ordered': True}
"""


async def test_a_tool_that_orders_writes_and_wakes_is_built_and_its_first_launch_stops_for_the_cost(
    chat_server: ChatServer, base: ToolsBase, alex: Person, add_member: Any
):
    dana = await add_member("Dana", "adult")

    async def ordering(request: RunRequest, ask: FakeAsk) -> Any:
        await ask({"do": "begin", "operation": "order", "amount_cents": 3250})
        await ask({"do": "write", "sql": "INSERT INTO notes (what) VALUES ($1)", "params": ["x"]})
        await ask({"do": "wake", "content": {"ordered": "pizzas"}})
        return {"ordered": True}

    chat_server.sandbox = led(ordering)
    order = {"order": {"says": "Orders the pizzas.", "risks": ["costs"]}}
    examples: list[Any] = [{"entry": {"a": 1, "b": 2}, "expected": {"ordered": True}}]
    use(chat_server, Maker(submit(ORDERING, operations=order)))
    async with chat_server.session_factory() as session:
        build = await open_build(
            session,
            name="add",
            purpose="Order the pizzas.",
            opened_by=alex.id,
            examples=examples,
            now=chat_server.clock(),
        )
        await session.commit()
    await run_the_pass(chat_server)
    assert (await build_of(chat_server, build.id)).state == "ready"
    [tool] = await tools(chat_server)
    assert tool.state == "ready"
    assert await notes_left(chat_server, base) == []
    # Its first real launch, for Dana, stops at the cost: nothing written, a stop kept.
    deps = made_tool_deps(app_turn_deps(chat_server.app))
    came = await launch(
        deps, tool.id, arguments={"a": 1, "b": 2}, for_whom=ForWhom(dana), cause="trigger"
    )
    assert came.status == "stopped"
    assert isinstance(came.result, dict) and came.result["reason"] == "needs_yes"
    assert (came.result["why"], came.result["amount_cents"]) == ("costs", 3250)
    async with chat_server.session_factory() as session:
        [stop] = (await session.scalars(select(LaunchStop))).all()
        assert (stop.operation, stop.for_person_id) == ("order", dana.id)
    assert await notes_left(chat_server, base) == []


async def test_the_kinds_risks_follow_a_rebuild_while_an_owners_deny_stays(
    chat_server: ChatServer, sandbox: FakeSandbox, alex: Person, owner_member: Member
):
    tool = await add_ready_tool(chat_server, name="add")
    async with chat_server.session_factory() as writer:
        writer.add(
            PolicyRight(
                person_id=alex.id,
                action_type="made.add",
                access="deny",
                summary="Not for them.",
                owner_words="Not for them.",
                created_by=owner_member.person.id,
            )
        )
        await writer.commit()
    use(chat_server, Maker(submit(CODE + "# orders\n", operations=ORDER_OPERATIONS)))
    await opened(chat_server, alex, tool_id=tool.id)
    await run_the_pass(chat_server)
    [changed] = await tools(chat_server)
    assert changed.operations == ORDER_OPERATIONS
    kind = await kind_of(chat_server, "add")
    assert kind is not None
    assert (kind.costs, kind.irreversible, kind.physical, kind.outward) == (True, True, False, True)
    # The kind's roles are kept, and the owner's deny still holds.
    assert kind.default_roles == ["owner", "adult", "child"]
    # The audit says what the kind's risks were and are (fix round 1, M-3).
    [built] = await audit_of(chat_server, BUILT)
    assert built.details["risks_before"] == NO_RISKS
    assert built.details["risks"] == {
        **NO_RISKS,
        "costs": True,
        "irreversible": True,
        "outward": True,
    }
    async with chat_server.session_factory() as reader:
        policy = await load_policy(reader)
        rights = await load_rights(reader)
    assert not rights.resolve(Actor(alex.id, "adult"), "made.add", policy).allowed
    # Fewer risks in a later version: the kind follows again.
    use(chat_server, Maker(submit(CODE + "# adds again\n")))
    await opened(chat_server, alex, tool_id=tool.id)
    await run_the_pass(chat_server)
    kind = await kind_of(chat_server, "add")
    assert kind is not None and not (kind.costs or kind.irreversible or kind.outward)


# --- fix round 1: paced rounds, yeses taken back --------------------------------------------


def test_the_pause_after_rounds_that_make_no_tool():
    pace = starts()
    assert [pause_after(n, pace) for n in range(PACED_AFTER)] == [None] * PACED_AFTER
    assert pause_after(PACED_AFTER, pace) == FIRST_PAUSE == timedelta(seconds=10)
    assert pause_after(PACED_AFTER + 1, pace) == timedelta(seconds=20)
    assert pause_after(PACED_AFTER + 10, pace) == LONGEST_PAUSE == timedelta(minutes=10)
    assert pause_after(10_000, pace) == LONGEST_PAUSE


def test_the_pace_of_a_build_is_read_from_the_settings():
    # No free round: the first round that makes no tool pauses.
    none_free = {**starts(), "free_rounds": 0}
    assert pause_after(1, none_free) == FIRST_PAUSE
    assert pause_after(2, none_free) == 2 * FIRST_PAUSE
    # Many free rounds: none pauses before them.
    many = {**starts(), "free_rounds": 1000}
    assert [pause_after(n, many) for n in (1, 5, 999)] == [None, None, None]
    assert pause_after(1000, many) == FIRST_PAUSE
    # Its first and longest pauses, raised: no ceiling of the code below what a date holds.
    slow = {**starts(), "first_pause_seconds": 60, "longest_pause_seconds": 10**9}
    assert pause_after(PACED_AFTER + 3, slow) == timedelta(seconds=480)
    assert pause_after(PACED_AFTER + 40, slow) == timedelta(seconds=10**9)
    past_a_date = {**slow, "longest_pause_seconds": 10**12}
    assert pause_after(PACED_AFTER + 40, past_a_date) == timedelta(days=36_500)
    # Fix round 1, I2: past what a timedelta holds, the longest pause a date holds, no error.
    beyond = {**slow, "first_pause_seconds": 10**15, "longest_pause_seconds": 10**18}
    assert pause_after(PACED_AFTER, beyond) == timedelta(days=36_500)
    assert pause_after(PACED_AFTER + 40, beyond) == timedelta(days=36_500)


async def test_with_no_free_round_the_first_round_that_makes_no_tool_waits(
    chat_server: ChatServer, sandbox: FakeSandbox, alex: Person
):
    async with chat_server.session_factory() as writer:
        await set_house(writer, {"free_rounds": 0, "first_pause_seconds": 30})
        await writer.commit()
    maker = use(chat_server, Maker(submit("import os\n" + CODE), submit()))
    build_id = await opened(chat_server, alex)
    await run_the_pass(chat_server)
    build = await build_of(chat_server, build_id)
    assert len(maker.making) == 1
    assert (build.state, build.refused_in_a_row) == ("waiting", 1)
    assert build.not_before == chat_server.clock() + timedelta(seconds=30)


async def test_with_many_free_rounds_a_build_is_never_paced(
    chat_server: ChatServer, sandbox: FakeSandbox, alex: Person
):
    async with chat_server.session_factory() as writer:
        await set_house(writer, {"free_rounds": 50})
        await writer.commit()
    failing = [submit(f"import os\n# try {i}\n" + CODE) for i in range(8)]
    maker = use(chat_server, Maker(*failing, submit()))
    build_id = await opened(chat_server, alex)
    await run_the_pass(chat_server)
    # Nine rounds at once: none waited.
    build = await build_of(chat_server, build_id)
    assert len(maker.making) == 9
    assert (build.state, build.not_before) == ("ready", None)


async def test_give_up_hours_is_read_from_the_settings(
    chat_server: ChatServer, sandbox: FakeSandbox, alex: Person
):
    async with chat_server.session_factory() as writer:
        await set_house(writer, {"give_up_hours": 1})
        await writer.commit()
    maker = use(chat_server, Maker(*(ModelCallError("unreachable") for _ in range(40))))
    build_id = await opened(chat_server, alex)
    await run_the_pass(chat_server)
    while (await build_of(chat_server, build_id)).state == "building":
        await wait_and_pass(chat_server, build_id)
    build = await build_of(chat_server, build_id)
    assert (build.state, build.outcome) == ("abandoned", waited_too_long(1))
    assert "give_up_hours is 1" in str(build.outcome)
    assert 3600 <= build.waited_seconds < 24 * 3600
    assert len(maker.making) < 10


async def test_the_code_s_size_and_the_time_of_a_build_are_domo_s_settings(
    chat_server: ChatServer, sandbox: FakeSandbox, alex: Person
):
    long_code = CODE + "# " + "x" * 25_000 + "\n"
    maker = use(chat_server, Maker(submit(long_code), submit(long_code, max_seconds=5000)))
    build_id = await opened(chat_server, alex)
    await run_the_pass(chat_server)
    # The start: 20 000 characters, said with the setting.
    [refused] = maker.told(1)["last_report"]["imports"]
    assert refused.startswith("the code has more than 20000 characters")
    assert "code_chars is 20000" in refused
    assert maker.told(0)["bounds"]["code_chars"] == 20000
    async with chat_server.session_factory() as writer:
        await set_house(writer, {"code_chars": 30_000})
        await writer.commit()
    await opened(chat_server, alex, name="longer")
    use(chat_server, Maker(submit(long_code, name="longer", max_seconds=5000)))
    await run_the_pass(chat_server)
    built = {tool.name: tool for tool in await tools(chat_server)}
    # More than the hour the launcher gives today, kept as the tool's own setting.
    assert built["longer"].settings == {"run_seconds": 5000}
    assert (await build_of(chat_server, build_id)).state != "ready"


async def test_the_sixth_refused_round_in_a_row_is_not_run_before_its_time(
    chat_server: ChatServer, sandbox: FakeSandbox, alex: Person
):
    failing = [submit(f"import os\n# try {i}\n" + CODE) for i in range(6)]
    # Text, the same code: they make no tool either.
    maker = use(chat_server, Maker(*failing[:4], "Let me think.", failing[4], submit()))
    build_id = await opened(chat_server, alex)
    await run_the_pass(chat_server)
    # Five rounds in a row made no tool: the build waits, told when it is taken again.
    build = await build_of(chat_server, build_id)
    assert len(maker.making) == 5
    assert (build.state, build.rounds, build.refused_in_a_row) == ("waiting", 5, 5)
    assert build.not_before == chat_server.clock() + FIRST_PAUSE
    # Not before its time, whatever the passes (and a restart keeps it: it is in the row).
    chat_server.clock.advance(FIRST_PAUSE - timedelta(seconds=1))
    await run_the_pass(chat_server)
    assert len(maker.making) == 5
    chat_server.clock.advance(timedelta(seconds=2))
    await run_the_pass(chat_server)
    # The sixth made no tool either: the next pause is twice as long.
    build = await build_of(chat_server, build_id)
    assert len(maker.making) == 6
    assert (build.state, build.refused_in_a_row) == ("waiting", 6)
    assert build.not_before == chat_server.clock() + 2 * FIRST_PAUSE
    # A version that passes makes the tool, and the count is reset.
    chat_server.clock.advance(2 * FIRST_PAUSE + timedelta(seconds=1))
    await run_the_pass(chat_server)
    build = await build_of(chat_server, build_id)
    assert (build.state, build.refused_in_a_row, build.not_before) == ("ready", 0, None)


async def test_a_paced_build_says_when_it_goes_on(
    chat_server: ChatServer, sandbox: FakeSandbox, adult_chat: ChatPilot
):
    alex = adult_chat.person
    tool = await add_ready_tool(chat_server, name="add")
    use(chat_server, Maker(*(submit(f"import os\n# {i}\n" + CODE) for i in range(5))))
    await opened(chat_server, alex, tool_id=tool.id)
    await run_the_pass(chat_server)
    reply = await adult_chat.say_with_tool("tool_look", {"name": "add"})
    [result] = reply.model_results
    assert result["build"]["state"] == "waiting" and result["build"]["not_before"]


async def test_a_rebuild_that_changes_an_operation_takes_back_its_yeses(
    chat_server: ChatServer, sandbox: FakeSandbox, adult_chat: ChatPilot
):
    alex = adult_chat.person
    before = {
        "order": {"says": "Orders the pizzas.", "risks": ["costs"]},
        "follow": {"says": "Reads where the order is.", "risks": []},
        "cancel": {"says": "Cancels the order.", "risks": ["irreversible"]},
    }
    after = {
        # Its risks change, ``follow`` stays the same, ``cancel`` goes.
        "order": {"says": "Orders the pizzas.", "risks": ["costs", "outward"]},
        "follow": {"says": "Reads where the order is.", "risks": []},
    }
    tool = await add_ready_tool(chat_server, name="add")
    async with chat_server.session_factory() as writer:
        await writer.execute(
            update(MadeTool).where(MadeTool.id == tool.id).values(operations=before)
        )
        for operation in ("order", "follow", "cancel"):
            writer.add(
                StandingAgreement(
                    tool_id=tool.id,
                    operation=operation,
                    for_person_id=alex.id,
                    amount_cents_at_most=4000,
                    times=1,
                    per="week",
                    given_by=alex.id,
                )
            )
        for operation in ("order", "follow"):
            writer.add(
                LaunchStop(
                    tool_id=tool.id,
                    for_person_id=alex.id,
                    operation=operation,
                    occurrence=1,
                    amount_cents=3250,
                    why="costs",
                    approver="self",
                    ask="yes",
                    arguments={},
                    arguments_digest=digest_of({}),
                )
            )
        await writer.commit()
    use(chat_server, Maker(submit(CODE + "# goes out\n", operations=after)))
    build_id = await opened(chat_server, alex, tool_id=tool.id)
    await run_the_pass(chat_server)
    build = await build_of(chat_server, build_id)
    assert build.state == "ready"
    async with chat_server.session_factory() as reader:
        agreements = {
            a.operation: a.withdrawn_at
            for a in (await reader.scalars(select(StandingAgreement))).all()
        }
        stops = {
            s.operation: s.yes_used_at for s in (await reader.scalars(select(LaunchStop))).all()
        }
    assert agreements["follow"] is None and stops == {"follow": None}
    assert agreements["order"] is not None and agreements["cancel"] is not None
    [entry] = await audit_of(chat_server, YES_WITHDRAWN)
    assert entry.details == {
        "tool": "add",
        "operations": ["cancel", "order"],
        "agreements": 2,
        "stops": 1,
    }
    [built] = await audit_of(chat_server, BUILT)
    assert built.details["withdrawn"] == {
        "operations": ["cancel", "order"],
        "agreements": 2,
        "stops": 1,
    }
    # The build's outcome tells Domo, and so does the tool.
    assert build.outcome is not None and "cancel, order" in build.outcome
    reply = await adult_chat.say_with_tool("tool_look", {"name": "add"})
    [result] = reply.model_results
    assert result["last_build"] == build.outcome


async def test_how_many_builds_run_at_once_is_domo_s_setting(
    chat_server: ChatServer, sandbox: FakeSandbox, alex: Person
):
    async with chat_server.session_factory() as writer:
        await set_house(writer, {"builds_at_once": 3})
        await writer.commit()
    # Each call takes a while: the builds taken stay in their round while the pass goes on.
    use(chat_server, Maker(*(2.0 for _ in range(4))))
    ids = [await opened(chat_server, alex, name=name) for name in ("one", "two", "three", "four")]
    await toolmaking_pass(app_turn_deps(chat_server.app))(chat_server.clock())
    # Three taken at once (claimed as the pass starts them), the fourth left for a later pass.
    states = [(await build_of(chat_server, build_id)).state for build_id in ids]
    assert sorted(states) == ["building", "building", "building", "waiting"]
    await chat_server.wait_for_wakes()


async def test_what_a_build_writes_in_its_tool_s_settings_is_audited_as_figures(
    chat_server: ChatServer, sandbox: FakeSandbox, alex: Person
):
    # Fix round 1, S3 (spec 12.1: every change of a setting is in the audit log).
    use(chat_server, Maker(submit(max_seconds=120, launches_per_day=50)))
    await opened(chat_server, alex)
    await run_the_pass(chat_server)
    [entry] = await audit_of(chat_server, "madetool.settings_changed")
    assert entry.actor == "domo"
    assert entry.details == {
        "tool": "add",
        "keys": ["launches_per_day", "run_seconds"],
        "before": {"launches_per_day": 200, "run_seconds": 60},
        "after": {"launches_per_day": 50, "run_seconds": 120},
    }
    # A new version that says nothing of them changes nothing, and writes no line.
    [tool] = await tools(chat_server)
    again = use(chat_server, Maker(submit(CODE + "# 2\n")))
    await opened(chat_server, alex, tool_id=tool.id)
    await run_the_pass(chat_server)
    assert len(await audit_of(chat_server, "madetool.settings_changed")) == 1
    # What the tool carries of its own is what current shows (final review m1).
    current = again.told(0)["current"]
    assert (current["max_seconds"], current["launches_per_day"]) == (120, 50)


async def test_an_owner_s_deny_of_tool_set_holds_through_a_build(
    chat_server: ChatServer, sandbox: FakeSandbox, alex: Person, owner_member: Member
):
    # Final review I1, ruling R31: a build writes its tool's own settings only if Domo's right
    # on tool.set allows it.
    async with chat_server.session_factory() as writer:
        writer.add(
            PolicyRight(
                action_type="tool.set",
                access="deny",
                summary="Not for the assistant.",
                owner_words="Not for the assistant.",
                created_by=owner_member.person.id,
                for_domo=True,
            )
        )
        await writer.commit()
    # A new tool: built, its settings untouched, nothing audited, and the build says why.
    use(chat_server, Maker(submit(max_seconds=120, launches_per_day=50)))
    first = await opened(chat_server, alex)
    await run_the_pass(chat_server)
    [tool] = await tools(chat_server)
    assert tool.settings == {}
    assert await audit_of(chat_server, "madetool.settings_changed") == []
    outcome = (await build_of(chat_server, first)).outcome
    assert outcome is not None and "tool_set" in outcome and "run_seconds" in outcome
    # A change (tool_change) cannot walk around it either.
    use(chat_server, Maker(submit(CODE + "# 2\n", max_seconds=500)))
    again = await opened(chat_server, alex, tool_id=tool.id)
    await run_the_pass(chat_server)
    assert (await build_of(chat_server, again)).state == "ready"
    [changed] = await tools(chat_server)
    assert changed.settings == {}
    assert await audit_of(chat_server, "madetool.settings_changed") == []
