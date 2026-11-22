"""How Domo makes himself a tool, on a real model (0.10.0, spec of 2026-11-17, "Domo se crée
un outil").

The owner's rules (2026-11-17): it is Domo's judgement whether he makes himself a tool, and he
does not say so ("everything must be fully transparent for the users"); making one never keeps him
from doing what was asked: the request waits in a plan and is done when the tool is ready, or
another way when its time comes first or the build is abandoned; one build per tool. None of that
is code: the code only offers ``tool_build`` and the plans, and tells him what is being built and
how a build ended. A fake model cannot show it. This evaluation gives a real model the real fixed
prompt and the context of a turn as the code writes it (``build_system_prompt``: the household,
the builds in progress, the tools set aside or given up, the plans due with how their build
stands), lets it answer with the real tool definitions, his own tools offered as ``t_<name>`` (the
calls are answered by a stub: nothing runs), and has a second call grade the answer against the
criteria of each case of ``toolmaking_cases.json``.

A case gives what the person said (``said``), or, for a turn a plan woke him for, the plan due
(``due``: its why, their words, how its build stands and the tool's name once ready), or, for a
turn a tool of his woke him for, what it says (``woken``: each tool's name and content, as
``conversation.tool_wakes`` gives them), after the conversation it came from (``history``); the
builds in progress (``building``), the tools he made that are offered (``made_tools``: each one's
definition and operations, the result its stub gives back, and the triggers ``tool_look`` shows),
the tools set aside or given up (``aside``), and what a program he runs by hand gives
(``hand_result``). The stub's ``tool_build`` and ``tool_change`` answer with a build's id,
``tool_find`` with the case's tools, ``tool_look`` with one of them, ``plan_set`` with a plan,
``program_run`` with ``{"launch": "done", "result": <hand_result>, "run": <its id>}``; a tool of his
with ``{"launch": "done", "result": <its result>}``, or, called with ``yes_for``, with a request
that waits for the person's yes. The checks are the hearsay evaluation's own (``_held``: ``must``,
``must_not``, ``never``, ``tools_any``, ``tools_none``, ``calls_must_not_match``, ``max_chars``
counted by the code), with two more (``_met``): ``tools_all`` (every one of these tools was called)
and ``calls_must_match`` (each of these texts is in one of the calls), and so are the models and the
key.

One more case is on the side of the build (``test_the_tool_he_writes_passes_the_code_s_checks``):
given ``prompts/toolmaking.md`` and a build as the code writes it (a purpose and two examples),
the model's ``submit`` must pass the checks of the core that need no sandbox: ``forbidden`` (what
the code imports and uses) and ``unsupported`` (its parameters).

Like the other evaluations it is a development tool, skipped unless a model is configured, and
never part of the usual suites (the tests of the harness itself, below, are). Run it with::

    EVAL_PROVIDER=anthropic EVAL_MODEL=<model> EVAL_API_KEY_FILE=<a file holding the key> \\
      uv run pytest -m live packages/domo-core/tests/eval/test_toolmaking_eval.py -s

``EVAL_PROVIDER``, ``EVAL_BASE_URL``, ``EVAL_API_KEY``, ``EVAL_API_KEY_FILE``, ``EVAL_GRADER``,
``EVAL_RUNS`` (3) and ``EVAL_ONLY`` are read as by ``test_hearsay_eval.py``. It costs real money:
about twenty calls of the talking model (more when it calls tools), as many of the grader, and
three calls that write a tool (up to 16 000 tokens of output each).

The bar, per case: a criterion marked ``never`` holds in every run (those are the answers that
break the owner's rules: a tool announced, a request refused for a build, a second build), and
the other criteria hold in at least two runs of three. The build's case passes in two runs of
three.
"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest
from domo_core.chat.agent import ActionRecord, TextDelta, run_turn
from domo_core.conversation.prepare import PLAN_MARKER, TOOL_MARKER
from domo_core.llm.types import (
    ChatChunk,
    ChatMessage,
    ChatModel,
    JsonObject,
    ToolCall,
    ToolDefinition,
    ToolResult,
)
from domo_core.madetools.build import GIVE_UP, SUBMIT, toolmaking_prompt
from domo_core.madetools.imports import forbidden
from domo_core.madetools.offered import offered_name
from domo_core.madetools.risks import OperationsNotRight, read_operations
from domo_core.madetools.schema import unsupported
from domo_core.prompts import (
    BeingBuilt,
    DuePlan,
    HouseholdLine,
    ToolAside,
    ToolWake,
    build_system_prompt,
    today_of,
    tool_wake_heading,
)
from domo_core.tools.registry import TOOLS
from fakes import FakeChatModel, FixedModels
from test_hearsay_eval import GRADER, _held, _shown, grader, talking

# The hearsay evaluation's fixtures, used here as they are (pytest finds them by these names).
__all__ = ["grader", "talking"]

CASES = Path(__file__).with_name("toolmaking_cases.json")
# Who lives in the house of every case: the speaker of a case is one of them.
HOUSEHOLD = (("Alex", "owner"), ("Dana", "adult"), ("Paul", "child"), ("Sam", "adult"))
ROLES = ("owner", "adult", "child", "guest")
# What the owner is offered here: the tools of his tools, the plans, and a few others a turn
# offers, so that the cases are not about the only tools there are.
OFFERED: tuple[str, ...] = (
    "tool_build",
    "tool_change",
    "tool_remove",
    "tool_look",
    "tool_find",
    "tool_launch_when",
    # What he does by hand at once (the tools are Domo's, Task 12).
    "program_run",
    "plan_set",
    "plan_change",
    "plan_cancel",
    "memory_keep",
    "memory_search",
    "note_leave",
    "household_list",
)
NOW = datetime(2026, 11, 16, 15, 5, tzinfo=UTC)
ZONE = ZoneInfo("Europe/Brussels")
BUILD_ID = "6f1c2a52-8d0b-4c1e-9a43-0f6f4e1d2b7a"
PLAN_ID = "0b7e3f9c-2a14-4d6b-8f21-5c9a7d3e1f80"
DUE_ID = "3c5d7e9f-1a2b-4c6d-8e0f-a1b2c3d4e5f6"
# The id of the program the stub says it ran by hand (``program_run``'s ``run``), and of the request
# that waits for a yes it makes of a call with ``yes_for``.
RUN_ID = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"
WAITING_ID = "5d4c3b2a-1f0e-4d9c-8b7a-6f5e4d3c2b1a"


@pytest.fixture
def cases() -> list[dict[str, Any]]:
    """The cases of the evaluation."""
    return json.loads(CASES.read_text(encoding="utf-8"))


def _made(case: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The case's tools of his, by the name they are offered under (``t_<name>``)."""
    return {offered_name(tool["name"]): tool for tool in case.get("made_tools", [])}


def _definitions(case: dict[str, Any]) -> list[ToolDefinition]:
    """The tools of the case's turn: those of the code the owner is offered, then his own, as a
    turn offers them (``madetools.offered.MadeToolOffer``)."""
    mine = [
        ToolDefinition(name=name, description=tool["description"], parameters=tool["parameters"])
        for name, tool in _made(case).items()
    ]
    return [*(TOOLS[name].definition for name in OFFERED), *mine]


def _due(case: dict[str, Any]) -> tuple[DuePlan, ...]:
    due = case.get("due")
    if due is None:
        return ()
    return (
        DuePlan(
            id=DUE_ID,
            why=due["why"],
            their_words=due.get("their_words"),
            late_minutes=None,
            build=due.get("build"),
            tool=due.get("tool"),
        ),
    )


def _woken(case: dict[str, Any]) -> tuple[ToolWake, ...]:
    """What woke him in the case's turn: each tool and what it says."""
    return tuple(
        ToolWake(tool=woke["tool"], content=woke["content"]) for woke in case.get("woken", [])
    )


def _prompt(case: dict[str, Any]) -> str:
    """The system prompt of the case's turn, as the code writes it."""
    speaker = case["speaker"]
    return build_system_prompt(
        display_name=speaker["name"],
        role=speaker["role"],
        can_act=True,
        today=today_of(NOW, ZONE),
        household=tuple(
            HouseholdLine(name=name, role=role, is_you=name == speaker["name"])  # type: ignore[arg-type]
            for name, role in HOUSEHOLD
        ),
        offered=OFFERED,
        reads="en",
        due_plans=_due(case),
        aside=tuple(ToolAside(**aside) for aside in case.get("aside", [])),
        building=tuple(BeingBuilt(**built) for built in case.get("building", [])),
        tool_wakes=_woken(case),
    )


def _history(case: dict[str, Any]) -> list[ChatMessage]:
    """The conversation of the case's turn: what came before (``history``), then the person's
    words, or, for a turn a plan or a tool woke him for, the code's line (``PLAN_MARKER``,
    ``TOOL_MARKER``)."""
    history = [ChatMessage(role, text) for role, text in case.get("history", [])]
    if "due" in case:
        last = PLAN_MARKER
    elif "woken" in case:
        last = TOOL_MARKER
    else:
        last = case["said"]
    return [*history, ChatMessage("user", last)]


def _arguments(call: ToolCall) -> JsonObject:
    try:
        arguments = json.loads(call.arguments or "{}")
    except ValueError:
        return {}
    return cast(JsonObject, arguments) if isinstance(arguments, dict) else {}


def _stubbed(case: dict[str, Any], call: ToolCall) -> tuple[str, JsonObject]:
    """What the stub answers ``call`` in the case: its status and its result."""
    made = _made(case)
    if call.name in made:
        if "yes_for" in _arguments(call):
            # As a request that waits for the person's yes is told.
            waiting: JsonObject = {
                "status": "waiting_for_confirmation",
                "request_id": WAITING_ID,
                "answered_by": "the person",
                "answer_with": "the button, or a yes in words",
            }
            return "waiting", waiting
        return "done", {"launch": "done", "result": made[call.name]["result"]}
    if call.name == "program_run":
        ran: JsonObject = {"launch": "done", "result": case.get("hand_result"), "run": RUN_ID}
        return "done", ran
    if call.name in ("tool_build", "tool_change"):
        return "done", {"build": BUILD_ID}
    if call.name == "tool_find":
        found: list[JsonObject] = [
            {"name": tool["name"], "description": tool["description"]}
            for tool in case.get("made_tools", [])
        ]
        return "done", {"tools": cast(JsonObject, found)}
    if call.name == "tool_look":
        name = _arguments(call).get("name")
        tool = next((t for t in case.get("made_tools", []) if t["name"] == name), None)
        if tool is None:
            return "not_found", {"error": "no tool of yours has this name"}
        looked: JsonObject = {
            "name": tool["name"],
            "description": tool["description"],
            "state": "ready",
            "operations": tool["operations"],
            "triggers": tool.get("triggers", []),
        }
        return "done", looked
    if call.name == "tool_launch_when" and "cancel" in _arguments(call):
        return "done", {"status": "done", "cancelled": _arguments(call)["cancel"]}
    if call.name == "plan_set":
        return "done", {"id": PLAN_ID, **_arguments(call)}
    if call.name == "memory_search":
        return "done", {"memories": []}
    if call.name == "household_list":
        people: list[JsonObject] = [{"name": name, "role": role} for name, role in HOUSEHOLD]
        return "done", {"people": cast(JsonObject, people)}
    return "done", {"done": True}


async def _answer(model: ChatModel, case: dict[str, Any]) -> tuple[str, list[str]]:
    """What the model answers to the case: its words, and the calls it made (name and arguments,
    ``_shown``). A stub answers every call (``_stubbed``)."""
    calls: list[str] = []

    async def handle(call: ToolCall, *, over_limit: bool = False) -> ActionRecord:
        calls.append(_shown(call))
        status, result = _stubbed(case, call)
        tool = TOOLS.get(call.name)
        return ActionRecord(
            tool_name=call.name,
            action_type=f"made.{call.name[2:]}" if tool is None else tool.action_type,
            status=status,
            confirmation_id=None,
            model_result={"status": status, **result},
            card={},
        )

    words: list[str] = []
    async for event in run_turn(
        models=FixedModels(model),
        system_prompt=_prompt(case),
        history=_history(case),
        max_output_tokens=1200,
        tools=_definitions(case),
        handle_tool_call=handle,
    ):
        if isinstance(event, TextDelta):
            words.append(event.text)
    return "".join(words).strip(), calls


def _knew(case: dict[str, Any]) -> list[str]:
    """What the grader is told the assistant knew, in plain words."""
    knew: list[str] = []
    for role, text in case.get("history", []):
        knew.append(f"- Earlier, {'the person' if role == 'user' else 'the assistant'}: {text}")
    for built in case.get("building", []):
        knew.append(f"- A tool of its own was being built for: {built['purpose']}")
    for tool in case.get("made_tools", []):
        knew.append(f"- It had made a tool of its own: {tool['description']}")
    for aside in case.get("aside", []):
        knew.append(f"- It had given up making a tool: {aside['reason']}")
    for woke in case.get("woken", []):
        content = json.dumps(woke["content"], ensure_ascii=False)
        knew.append(f"- A tool of its own woke it, saying: {content}")
    if "hand_result" in case:
        given = json.dumps(case["hand_result"], ensure_ascii=False)
        knew.append(f"- Done by hand, the request gave: {given}")
    due = case.get("due")
    if due is not None:
        knew.append(f"- A plan of its own came due: {due['why']}")
        if due.get("build") == "ready":
            knew.append(f"- The tool it waited for was ready: {due['tool']}")
        elif due.get("build") == "abandoned":
            knew.append("- The tool it waited for will not be made")
    return knew or ["- nothing special"]


async def _graded(
    grader: ChatModel, case: dict[str, Any], words: str, calls: list[str]
) -> dict[str, bool]:
    """Each criterion of the case, true or false of this answer, by the grader."""
    criteria = [*case["must"], *case["must_not"], *case["never"]]
    if not criteria:
        return {}
    speaker = case["speaker"]
    if "due" in case:
        wrote = "(nothing: a plan of the assistant woke it)"
    elif "woken" in case:
        wrote = "(nothing: a tool of the assistant woke it)"
    else:
        wrote = case["said"]
    asked = "\n".join(
        [
            f"The person ({speaker['name']}, {speaker['role']}) wrote: {wrote}",
            "The assistant knew:",
            *_knew(case),
            "Tools it called: " + ("; ".join(calls) if calls else "none"),
            f"It answered: {words or '(no words)'}",
            "Criteria:",
            *[f"{number}. {text}" for number, text in enumerate(criteria, start=1)],
        ]
    )
    pieces: list[str] = []
    async for item in grader.stream(
        system=GRADER,
        messages=[ChatMessage("user", asked)],
        max_output_tokens=300,
        think=False,
    ):
        if isinstance(item, ChatChunk):
            pieces.append(item.text)
    text = "".join(pieces).strip()
    start, end = text.find("{"), text.rfind("}")
    verdicts: dict[str, Any] = json.loads(text[start : end + 1]) if start >= 0 else {}
    return {
        criterion: bool(verdicts.get(str(number), False))
        for number, criterion in enumerate(criteria, start=1)
    }


def _met(
    case: dict[str, Any], graded: dict[str, bool], calls: list[str], words: str = ""
) -> tuple[bool, bool]:
    """``_held``, and two checks of the calls more: every tool of ``tools_all`` was called, and
    each text of ``calls_must_match`` is in one of the calls (``_shown``: ``"from_run": "<id>"``,
    say)."""
    fine, hurt = _held(case, graded, calls, words)
    names = {call.split("(", 1)[0] for call in calls}
    fine = (
        fine
        and set(case.get("tools_all", ())) <= names
        and all(any(text in call for call in calls) for text in case.get("calls_must_match", ()))
    )
    return fine, hurt


@pytest.mark.live
async def test_he_makes_his_tools_without_a_word_and_never_drops_a_request(
    talking: ChatModel, grader: ChatModel, cases: list[dict[str, Any]]
):
    runs = int(os.environ.get("EVAL_RUNS", "3"))
    only = os.environ.get("EVAL_ONLY")
    failed: list[str] = []
    for case in cases:
        if only and only not in case["id"]:
            continue
        fine_runs, hurts, shown = 0, 0, []
        for _ in range(runs):
            words, calls = await _answer(talking, case)
            graded = await _graded(grader, case, words, calls)
            fine, hurt = _met(case, graded, calls, words)
            fine_runs += fine
            hurts += hurt
            shown.append(
                f"    calls={calls} chars={len(words)} words={words[:600]!r} graded={graded}"
            )
        verdict = "ok" if hurts == 0 and fine_runs * 3 >= runs * 2 else "FAILED"
        print(f"{verdict} {case['id']}: {fine_runs}/{runs} fine, {hurts} hurtful")
        if verdict != "ok":
            failed.append("\n".join([f"{case['id']} ({case['why']})", *shown]))
            print("\n".join(shown))
    assert not failed, "\n\n".join(failed)


# --- the build's side ----------------------------------------------------------------------------

BUILD_NAME = "split_expenses"
BUILD_PURPOSE = (
    "Split a month's expenses between two people: one of them paid all of the rent, the other "
    "all of the food, and both are shared equally. Given the rent and the food (whole numbers), "
    "give each one's share (half of the total, rounded down) and how much the one who paid the "
    "food owes the one who paid the rent (the rent minus that share)."
)
# The arithmetic of each example: 900 + 420 = 1320, each 1320 // 2 = 660, owed 900 - 660 = 240
# (the food's payer paid 420 of their 660); 1000 + 200 = 1200, each 600, owed 1000 - 600 = 400
# (the food's payer paid 200 of their 600).
BUILD_EXAMPLES: list[JsonObject] = [
    {"entry": {"rent": 900, "food": 420}, "expected": {"each": 660, "owed_to_rent_payer": 240}},
    {"entry": {"rent": 1000, "food": 200}, "expected": {"each": 600, "owed_to_rent_payer": 400}},
]


def build_message(name: str, purpose: str, examples: list[JsonObject]) -> str:
    """The message of the first round of a build, as ``madetools.build`` writes it."""
    told = {
        "round": 1,
        "purpose": purpose,
        "examples": examples,
        "name": name,
    }
    return json.dumps(told, ensure_ascii=False)


async def written_tool(model: ChatModel) -> JsonObject | None:
    """What ``model`` submits at the first round of the build of ``BUILD_NAME`` (the arguments of
    its ``submit``), or ``None`` when it gave up or submitted nothing that reads."""
    calls: list[ToolCall] = []
    async for item in model.stream(
        system=toolmaking_prompt(),
        messages=[ChatMessage("user", build_message(BUILD_NAME, BUILD_PURPOSE, BUILD_EXAMPLES))],
        max_output_tokens=16_000,
        tools=[SUBMIT, GIVE_UP],
    ):
        if isinstance(item, ToolCall):
            calls.append(item)
    submitted = next((call for call in calls if call.name == SUBMIT.name), None)
    if submitted is None:
        return None
    arguments = _arguments(submitted)
    return arguments or None


def the_code_s_findings(submitted: JsonObject | None) -> list[str]:
    """What the checks of the core that need no sandbox find in a submission: its parameters
    (``unsupported``), what its code imports and uses (``forbidden``), and its name; a
    submission that does not read is one finding."""
    if submitted is None:
        return ["nothing was submitted"]
    code, parameters = submitted.get("code"), submitted.get("parameters")
    if not isinstance(code, str) or not isinstance(parameters, dict):
        return ["the submission has no code or no parameters"]
    found = [*unsupported(parameters), *forbidden(code)]
    try:
        read_operations(submitted.get("operations"))
    except OperationsNotRight as wrong:
        found.append(f"operations: {wrong}")
    if submitted.get("name") != BUILD_NAME:
        found.append(f"the name is not the one asked: {submitted.get('name')!r}")
    return found


@pytest.mark.live
async def test_the_tool_he_writes_passes_the_code_s_checks(talking: ChatModel):
    runs = int(os.environ.get("EVAL_RUNS", "3"))
    shown: list[str] = []
    passed = 0
    for _ in range(runs):
        findings = the_code_s_findings(await written_tool(talking))
        passed += not findings
        shown.append(f"    findings={findings}")
    print(f"the build: {passed}/{runs} passed\n" + "\n".join(shown))
    assert passed * 3 >= runs * 2, "\n".join(shown)


# --- the harness itself (offline: these run in the usual suite) ---------------------------------


def test_the_cases_have_the_shape_the_evaluation_reads(cases: list[dict[str, Any]]):
    """Each case names a speaker of the household, gives what they said or a plan due after a
    conversation, and at least one criterion; no case twice. The tools a case names exist (the
    code's, or one of its own tools as offered); its builds, tools and plans have the shape the
    code gives them."""
    names = {name for name, _ in HOUSEHOLD}
    assert len(cases) >= 6
    assert len({case["id"] for case in cases}) == len(cases)
    for case in cases:
        made = _made(case)
        assert case["speaker"]["name"] in names and case["speaker"]["role"] in ROLES, case["id"]
        assert case["why"], case["id"]
        # What the turn answers: words, a plan due or a tool that woke him, one of them.
        assert sum(key in case for key in ("said", "due", "woken")) == 1, case["id"]
        assert case["must"] or case["never"], case["id"]
        named = [
            *case.get("tools_any", ()),
            *case.get("tools_none", ()),
            *case.get("tools_all", ()),
        ]
        for tool in named:
            assert tool in TOOLS or tool.startswith("t_"), (case["id"], tool)
        for tool in case.get("made_tools", []):
            # A tool knows its operations (Task 11), and may have triggers that wait.
            assert (
                {"name", "description", "parameters", "operations", "result"}
                <= set(tool)
                <= {
                    "name",
                    "description",
                    "parameters",
                    "operations",
                    "result",
                    "triggers",
                }
            ), case["id"]
            assert unsupported(tool["parameters"]) == [], (case["id"], tool["name"])
            read_operations(tool["operations"])
        for woke in case.get("woken", []):
            assert set(woke) == {"tool", "content"}, case["id"]
            assert offered_name(woke["tool"]) in made, case["id"]
        for role, text in case.get("history", []):
            assert role in ("user", "assistant") and text, case["id"]
        due = case.get("due")
        if due is not None:
            assert due["build"] in ("ready", "abandoned", "building"), case["id"]
            assert (due["build"] == "ready") == ("tool" in due), case["id"]
            assert due["build"] != "ready" or offered_name(due["tool"]) in made, case["id"]
        # A case that grades a split says who paid what: the figures hold only then.
        if "660" in json.dumps([*case["must"], *case["never"]]):
            asked = case.get("said") or case["due"]["their_words"]
            assert "Alex paid the rent and Dana paid the food" in asked, case["id"]
            assert "1320 / 2 = 660" in case["why"], case["id"]
        # The context the case gives reads as the code writes it (its builds and tools set aside
        # are the code's own dataclasses).
        _prompt(case)
    # Every case of the brief is there.
    ids = {case["id"] for case in cases}
    assert {
        "expenses_once_split_now",
        "expenses_every_month_split_now_and_build",
        "reminder_in_five_minutes_while_a_build_runs",
        "expenses_again_while_its_build_runs",
        "what_tools_have_you_made",
        "due_plan_build_ready_uses_the_tool",
        "due_plan_build_abandoned_does_it_another_way",
        # The tools are Domo's (Task 12).
        "hand_now_then_build_silently",
        "asks_the_yes_not_the_tool",
        "stops_an_initiative_when_told",
    } <= ids


def test_a_case_gives_the_context_as_a_turn_would(cases: list[dict[str, Any]]):
    """The builds in progress, the plan due with how its build stands, the tools given up and
    the tools of his reach the turn where a real turn puts them."""
    by_id = {case["id"]: case for case in cases}
    building = _prompt(by_id["reminder_in_five_minutes_while_a_build_runs"])
    assert 'Being built (data): [{"name": "kitchen_timers"' in building
    ready = by_id["due_plan_build_ready_uses_the_tool"]
    prompt = _prompt(ready)
    assert '"build": "ready", "tool": "split_expenses"' in prompt
    assert [message.content for message in _history(ready)][-1] == PLAN_MARKER
    assert "t_split_expenses" in [tool.name for tool in _definitions(ready)]
    abandoned = _prompt(by_id["due_plan_build_abandoned_does_it_another_way"])
    assert '"build": "abandoned"' in abandoned
    assert "Your own tools you gave up making lately" in abandoned
    said = by_id["expenses_every_month_split_now_and_build"]
    assert [message.content for message in _history(said)] == [said["said"]]
    assert not any(tool.name.startswith("t_") for tool in _definitions(said))


async def test_the_stub_answers_a_turn_of_a_case(cases: list[dict[str, Any]]):
    """In a turn of a case, ``tool_build`` is answered with a build's id, a tool of his with what
    it gave, and the calls are kept as the checks read them."""
    case = next(case for case in cases if case["id"] == "due_plan_build_ready_uses_the_tool")
    model = FakeChatModel(
        tool_script=[
            ToolCall(id="c1", name="t_split_expenses", arguments='{"rent":900,"food":420}'),
            ToolCall(id="c2", name="tool_build", arguments='{"name":"x"}'),
            "Each of you pays 660: Dana owes you 240.",
        ]
    )
    words, calls = await _answer(model, case)
    assert words == "Each of you pays 660: Dana owes you 240."
    assert calls == [
        't_split_expenses({"rent": 900, "food": 420})',
        'tool_build({"name": "x"})',
    ]
    results = [item.content for item in model.calls[-1].messages if isinstance(item, ToolResult)]
    assert '"launch": "done"' in results[0] and '"dana_owes_alex": 240' in results[0]
    assert BUILD_ID in results[1]
    assert model.calls[0].system == _prompt(case)
    # What the checks read of it: every criterion met, the call of his tool counts for tools_any,
    # but tool_build is among the case's tools_none: the case is not met, and nobody is hurt.
    criteria = [*case["must"], *case["must_not"], *case["never"]]
    graded = {text: text in case["must"] for text in criteria}
    assert _held(case, graded, calls, words) == (False, False)
    assert _held(case, graded, calls[:1], words) == (True, False)


async def test_a_case_done_by_hand_is_answered_with_a_run_and_checked_on_from_run(
    cases: list[dict[str, Any]],
):
    """``program_run`` is answered with what the program gave and its run's id; the case is met
    only when the tool is built from that run (``tools_all``, ``calls_must_match``)."""
    case = next(case for case in cases if case["id"] == "hand_now_then_build_silently")
    assert "program_run" in [tool.name for tool in _definitions(case)]
    ran = ToolCall(
        id="c1", name="program_run", arguments='{"code":"x","arguments":{},"operations":{}}'
    )
    built = ToolCall(
        id="c2",
        name="tool_build",
        arguments=json.dumps({"name": "loans", "purpose": "x", "examples": [], "from_run": RUN_ID}),
    )
    model = FakeChatModel(tool_script=[ran, built, "Noted: Sam owes you 20 euros."])
    words, calls = await _answer(model, case)
    results = [item.content for item in model.calls[-1].messages if isinstance(item, ToolResult)]
    assert f'"run": "{RUN_ID}"' in results[0] and BUILD_ID in results[1]
    criteria = [*case["must"], *case["must_not"], *case["never"]]
    graded = {text: text in case["must"] for text in criteria}
    assert _met(case, graded, calls, words) == (True, False)
    # By hand alone, or a build that does not start from the run: not met.
    assert _met(case, graded, calls[:1], words) == (False, False)
    other = calls[1].replace(RUN_ID, BUILD_ID)
    assert _met(case, graded, [calls[0], other], words) == (False, False)


async def test_a_case_woken_by_a_tool_gives_the_wake_and_a_yes_for_waits(
    cases: list[dict[str, Any]],
):
    """A turn a tool woke him for ends with the code's line, the context says what the tool said,
    and a call of the tool with ``yes_for`` is answered as a request that waits."""
    case = next(case for case in cases if case["id"] == "asks_the_yes_not_the_tool")
    assert [message.content for message in _history(case)][-1] == TOOL_MARKER
    [woke] = case["woken"]
    assert tool_wake_heading(woke["tool"]) in _prompt(case)
    stop = woke["content"]["needs_yes"]["stop"]
    asked = ToolCall(
        id="c1", name=offered_name(woke["tool"]), arguments=json.dumps({"yes_for": stop})
    )
    model = FakeChatModel(tool_script=[asked, "Shall I order them?"])
    words, calls = await _answer(model, case)
    [result] = [item.content for item in model.calls[-1].messages if isinstance(item, ToolResult)]
    assert '"waiting_for_confirmation"' in result and WAITING_ID in result
    criteria = [*case["must"], *case["must_not"], *case["never"]]
    graded = {text: text in case["must"] for text in criteria}
    assert _met(case, graded, calls, words) == (True, False)


async def test_a_case_that_stops_an_initiative_looks_cancels_and_keeps(
    cases: list[dict[str, Any]],
):
    """``tool_look`` shows the tool's triggers (whom each is for), a cancel is answered, and the
    case is met only with the cancel of that trigger and a memory kept."""
    case = next(case for case in cases if case["id"] == "stops_an_initiative_when_told")
    [tool] = case["made_tools"]
    [trigger] = tool["triggers"]
    looked = ToolCall(id="c1", name="tool_look", arguments=json.dumps({"name": tool["name"]}))
    cancel = {"name": tool["name"], "cancel": trigger["id"]}
    cancelled = ToolCall(id="c2", name="tool_launch_when", arguments=json.dumps(cancel))
    kept = ToolCall(id="c3", name="memory_keep", arguments='{"text": "x"}')
    model = FakeChatModel(tool_script=[looked, cancelled, kept, "Okay, I stopped."])
    words, calls = await _answer(model, case)
    results = [item.content for item in model.calls[-1].messages if isinstance(item, ToolResult)]
    assert trigger["id"] in results[0] and '"cancelled"' in results[1]
    criteria = [*case["must"], *case["must_not"], *case["never"]]
    graded = {text: text in case["must"] for text in criteria}
    assert _met(case, graded, calls, words) == (True, False)
    assert _met(case, graded, calls[:2], words) == (False, False)


async def test_the_build_s_case_reads_the_submission_and_checks_it():
    """The build's case: the model is given the toolmaking prompt, a build as the code writes it
    and the two tools of a build; what it submits is checked by ``unsupported`` and
    ``forbidden``."""
    good = {
        "name": BUILD_NAME,
        "description": "Split a month's rent and food between two people.",
        "parameters": {
            "type": "object",
            "properties": {
                "rent": {"type": "integer"},
                "food": {"type": "integer"},
            },
            "required": ["rent", "food"],
        },
        "operations": {"split": {"says": "Splits the rent and the food.", "risks": []}},
        "code": (
            "def run(entry):\n"
            '    given = entry["arguments"]\n'
            '    each = (given["rent"] + given["food"]) // 2\n'
            '    return {"each": each, "owed_to_rent_payer": given["rent"] - each}\n'
        ),
    }
    # The purpose and its examples agree: this code gives each example's expected result.
    for example in BUILD_EXAMPLES:
        given = cast(dict[str, int], example["entry"])
        each = (given["rent"] + given["food"]) // 2
        assert example["expected"] == {"each": each, "owed_to_rent_payer": given["rent"] - each}
    model = FakeChatModel(
        tool_script=[ToolCall(id="c1", name="submit", arguments=json.dumps(good))]
    )
    submitted = await written_tool(model)
    assert submitted == good
    assert the_code_s_findings(submitted) == []
    [call] = model.calls
    assert call.system == toolmaking_prompt()
    assert {tool.name for tool in call.tools} == {"submit", "give_up"}
    told = json.loads(str(call.messages[0].content))
    assert told["name"] == BUILD_NAME and told["examples"] == BUILD_EXAMPLES
    assert told["round"] == 1 and "rounds_at_most" not in told

    reaching = {**good, "code": "import os\ndef run(entry):\n    return os.getcwd()\n"}
    assert the_code_s_findings(reaching) != []
    loose = {**good, "parameters": {"type": "object", "properties": {"x": {"default": 1}}}}
    assert the_code_s_findings(loose) != []
    assert the_code_s_findings({**good, "name": "other"}) != []
    assert the_code_s_findings({**good, "operations": {}}) != []
    assert the_code_s_findings(None) == ["nothing was submitted"]
    gave_up = FakeChatModel(
        tool_script=[ToolCall(id="c1", name="give_up", arguments='{"reason":"no"}')]
    )
    assert await written_tool(gave_up) is None
