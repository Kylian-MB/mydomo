"""The turn with tools: rounds of calls and results, the cap, models that refuse tools (no
database, no web: the calls are handled by a function of the test); and what the descriptions of
the core's tools would make Domo say to the person who reads him."""

import ast
import json
import logging
import re
from collections.abc import AsyncGenerator, Iterator, Sequence
from pathlib import Path
from typing import cast

import domo_core
import pytest
from domo_core.chat.agent import (
    ActionRecord,
    ActionTaken,
    TextDelta,
    TurnDone,
    TurnFailed,
    run_turn,
)
from domo_core.llm.types import (
    AssistantToolCalls,
    ChatChunk,
    ChatMessage,
    ChatUsage,
    ErrorKind,
    HistoryItem,
    ModelCallError,
    ToolCall,
    ToolChoice,
    ToolDefinition,
    ToolResult,
)
from domo_core.people import whose
from domo_core.policy.catalog import OWNER_ONLY_ACTIONS, PEOPLE_GESTURES
from domo_core.tools.people_tools import BETWEEN_PEOPLE
from domo_core.tools.registry import TOOLS as REGISTRY
from fakes import DEFAULT_USAGE, FakeChatModel, FixedModels

TOOLS = (
    ToolDefinition("lookup", "Look something up.", {"type": "object", "properties": {}}),
    ToolDefinition("rename", "Rename.", {"type": "object", "properties": {}}),
)


def u(content: str) -> ChatMessage:
    return ChatMessage("user", content)


def record_for(call: ToolCall, **result: object) -> ActionRecord:
    return ActionRecord(
        tool_name=call.name,
        action_type=f"test.{call.name}",
        status="done",
        confirmation_id=None,
        model_result={"status": "done", **result},
        card={"key": "action.card.done", "params": {}},
    )


class Handler:
    """Handles the calls of a turn: each one is done, with its id as the result. A call beyond
    the limit of an answer is only noted (in ``over_limit``), as the chat records it unrun."""

    def __init__(self) -> None:
        self.calls: list[ToolCall] = []
        self.over_limit: list[ToolCall] = []

    async def __call__(self, call: ToolCall, *, over_limit: bool = False) -> ActionRecord:
        if over_limit:
            self.over_limit.append(call)
            return record_for(call, refused="too_many")
        self.calls.append(call)
        return record_for(call, id=call.id)


async def collect(model, **overrides):
    arguments = {
        "models": FixedModels(model),
        "system_prompt": "S",
        "history": [u("hi")],
        "max_output_tokens": 32,
        "tools": TOOLS,
        "handle_tool_call": Handler(),
    } | overrides
    return [event async for event in run_turn(**arguments)]


class RoundsModel:
    """A model whose every call streams the items given for it (then the default usage),
    whatever it is offered; the last list is repeated."""

    def __init__(self, *rounds: Sequence[str | ToolCall]) -> None:
        self._rounds = rounds
        self.calls: list[tuple[list[HistoryItem], tuple[ToolDefinition, ...], ToolChoice]] = []
        self.started = 0
        self.closed = 0

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
        self.calls.append((list(messages), tuple(tools), tool_choice))
        items = self._rounds[min(len(self.calls), len(self._rounds)) - 1]
        self.started += 1
        try:
            for item in items:
                yield ChatChunk(item) if isinstance(item, str) else item
            yield DEFAULT_USAGE
        finally:
            self.closed += 1


# --- rounds of calls and results -------------------------------------------------------------


async def test_each_action_says_its_round_and_whether_it_closes_it():
    """The rounds are the engine's: the calls of one answer share a round, the last one handled
    closes it, and the next answer that calls is the next round."""
    first, second = ToolCall("c1", "lookup", "{}"), ToolCall("c2", "rename", "{}")
    third = ToolCall("c3", "lookup", "{}")
    model = RoundsModel(["Looking.", first, second], [third], ["Done."])
    events = await collect(model)
    taken = [
        (e.record.tool_name, e.round, e.closes_round) for e in events if isinstance(e, ActionTaken)
    ]
    assert taken == [("lookup", 1, False), ("rename", 1, True), ("lookup", 2, True)]


async def test_a_call_is_handled_and_its_result_goes_back_to_the_model():
    call = ToolCall("c1", "lookup", '{"q": 1}')
    model = FakeChatModel(tool_script=[call, "Here."])
    handler = Handler()
    events = await collect(model, handle_tool_call=handler)
    record = record_for(call, id="c1")
    assert events == [
        ActionTaken(record),
        TextDelta("Here."),
        TurnDone(ChatUsage(6, 4)),
    ]
    assert handler.calls == [call]
    assert model.calls[1].messages == [
        u("hi"),
        AssistantToolCalls("", (call,)),
        ToolResult("c1", "lookup", json.dumps(record.model_result, ensure_ascii=False)),
    ]
    assert [c.tools for c in model.calls] == [TOOLS, TOOLS]
    assert [c.tool_choice for c in model.calls] == ["auto", "auto"]


async def test_the_result_is_json_the_model_can_read():
    call = ToolCall("c1", "lookup", "{}")
    model = FakeChatModel(tool_script=[call, "ok"])

    async def handle(call: ToolCall) -> ActionRecord:
        return record_for(call, name="Élise")

    await collect(model, handle_tool_call=handle)
    content = model.calls[1].messages[-1].content
    assert json.loads(content) == {"status": "done", "name": "Élise"}
    assert '"status": "done"' in content and "Élise" in content


async def test_several_calls_of_one_answer_are_handled_in_order_and_answered_in_order():
    first, second = ToolCall("a", "lookup", "{}"), ToolCall("b", "rename", "{}")
    model = RoundsModel(["Let me see.", first, second], ["Done."])
    handler = Handler()
    events = await collect(model, handle_tool_call=handler)
    assert handler.calls == [first, second]
    assert [type(e).__name__ for e in events] == [
        "TextDelta",
        "ActionTaken",
        "ActionTaken",
        "TextDelta",
        "TextDelta",
        "TurnDone",
    ]
    history = model.calls[1][0]
    assert history[1] == AssistantToolCalls("Let me see.", (first, second))
    assert [(r.call_id, r.name) for r in history[2:]] == [("a", "lookup"), ("b", "rename")]


async def test_the_text_of_two_rounds_is_not_glued_together():
    model = RoundsModel(["Let me see.", ToolCall("a", "lookup", "{}")], ["Done."])
    events = await collect(model)
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "Let me see.\n\nDone."


async def test_the_usage_of_every_round_is_counted():
    model = FakeChatModel(tool_script=[ToolCall("a", "lookup", "{}"), "x"], usage=ChatUsage(5, 1))
    events = await collect(model)
    assert events[-1] == TurnDone(ChatUsage(10, 2))


async def test_each_round_releases_its_stream():
    model = FakeChatModel(tool_script=[ToolCall("a", "lookup", "{}"), "x"])
    await collect(model)
    assert (model.started, model.closed) == (2, 2)


# --- the cap ------------------------------------------------------------------------------------


async def test_the_rounds_are_capped_and_the_last_one_may_not_call():
    calls = [ToolCall(f"c{i}", "lookup", "{}") for i in range(5)]
    model = FakeChatModel(tool_script=[*calls, "fin"])
    handler = Handler()
    events = await collect(model, handle_tool_call=handler, max_tool_rounds=2)
    assert handler.calls == calls[:2]
    assert [c.tool_choice for c in model.calls] == ["auto", "auto", "none"]
    # The last round still declares the tools: its history holds calls and results.
    assert model.calls[-1].tools == TOOLS
    assert [e for e in events if isinstance(e, TextDelta)] == [TextDelta("fin")]


async def test_the_default_cap_is_four_rounds():
    model = FakeChatModel(tool_script=[ToolCall(f"c{i}", "lookup", "{}") for i in range(9)])
    handler = Handler()
    await collect(model, handle_tool_call=handler)
    assert len(handler.calls) == 4 and len(model.calls) == 5


async def test_a_call_made_when_calling_is_not_allowed_is_not_handled(caplog):
    caplog.set_level(logging.WARNING, logger="domo_core.chat.agent")
    model = RoundsModel([ToolCall("a", "lookup", "{}")])  # calls whatever it is told
    handler = Handler()
    events = await collect(model, handle_tool_call=handler, max_tool_rounds=0)
    assert handler.calls == [] and isinstance(events[-1], TurnDone)
    assert model.calls[0][2] == "none"
    assert "not handled" in caplog.text


async def test_a_call_without_tools_is_not_handled():
    model = RoundsModel([ToolCall("a", "lookup", "{}"), "x"])
    events = await collect(model, tools=(), handle_tool_call=None)
    assert [type(e).__name__ for e in events] == ["TextDelta", "TurnDone"]
    assert model.calls[0][1] == ()


async def test_calls_beyond_the_limit_of_an_answer_are_reported_never_handled():
    calls = [ToolCall(f"c{i}", "lookup", "{}") for i in range(5)]
    model = RoundsModel(calls, ["Done."])
    handler = Handler()
    events = await collect(model, handle_tool_call=handler, max_calls_per_answer=3)
    assert handler.calls == calls[:3] and handler.over_limit == calls[3:]
    assert len([e for e in events if isinstance(e, ActionTaken)]) == 5
    # Every call of the answer gets its result, in order: the model must see what came of each.
    history = model.calls[1][0]
    assert history[1] == AssistantToolCalls("", tuple(calls))
    assert [r.call_id for r in history[2:]] == ["c0", "c1", "c2", "c3", "c4"]
    assert json.loads(history[-1].content)["refused"] == "too_many"


async def test_an_answer_may_make_eight_calls():
    calls = [ToolCall(f"c{i}", "lookup", "{}") for i in range(9)]
    handler = Handler()
    await collect(RoundsModel(calls, ["Done."]), handle_tool_call=handler)
    assert (len(handler.calls), len(handler.over_limit)) == (8, 1)


async def test_tools_need_a_handler():
    with pytest.raises(ValueError):
        await collect(FakeChatModel(["x"]), handle_tool_call=None)


# --- failures -----------------------------------------------------------------------------------


async def test_a_model_that_refuses_tools_is_asked_again_once_without_them(caplog):  # D6
    caplog.set_level(logging.WARNING, logger="domo_core.chat.agent")
    model = FakeChatModel(["salut"], refuse_tools=True)
    events = await collect(model, system_prompt_without_tools="S without tools")
    assert events == [TextDelta("salut"), TurnDone(DEFAULT_USAGE, without_tools=True)]
    assert [c.tools for c in model.calls] == [TOOLS, ()]
    # The retry is the same call, without the tools and with the prompt that says so.
    assert model.calls[1].messages == model.calls[0].messages
    assert [c.system for c in model.calls] == ["S", "S without tools"]
    assert "cannot act" in caplog.text


async def test_without_a_prompt_for_it_the_retry_keeps_the_prompt():
    model = FakeChatModel(["salut"], refuse_tools=True)
    await collect(model)
    assert [c.system for c in model.calls] == ["S", "S"]


async def test_the_retry_without_tools_happens_once():
    model = FakeChatModel(["x"], fail_after=0, error_kind="bad_request")
    events = await collect(model)
    assert events == [TurnFailed("bad_request", "")]
    assert [c.tools for c in model.calls] == [TOOLS, ()]


async def test_a_bad_request_after_text_is_a_failure_not_a_retry():
    model = FakeChatModel(["a", "b"], fail_after=1, error_kind="bad_request")
    events = await collect(model)
    assert events == [TextDelta("a"), TurnFailed("bad_request", "a")]
    assert len(model.calls) == 1


@pytest.mark.parametrize("kind", ["auth", "unreachable", "rate_limited", "upstream"])
async def test_other_failures_are_not_retried(kind):
    model = FakeChatModel(["x"], fail_after=0, error_kind=kind)
    assert await collect(model) == [TurnFailed(kind, "")]
    assert len(model.calls) == 1


class FailsOnItsSecondCall:
    """Says something and calls a tool, then fails (``upstream``, or the error it is given)."""

    def __init__(self, kind: ErrorKind = "upstream", text: str = "One.") -> None:
        self.calls: list[tuple[ToolDefinition, ...]] = []
        self._kind: ErrorKind = kind
        self._text = text

    async def stream(
        self, *, tools: Sequence[ToolDefinition] = (), **_: object
    ) -> AsyncGenerator[ChatChunk | ToolCall | ChatUsage]:
        self.calls.append(tuple(tools))
        if len(self.calls) > 1:
            raise ModelCallError(self._kind)
        if self._text:
            yield ChatChunk(self._text)
        yield ToolCall("a", "lookup", "{}")


async def test_a_bad_request_after_a_tool_round_is_a_failure_not_a_retry(caplog):
    """D6 is about models that do not know tools: they refuse the first call. After a round of
    calls the history holds calls and results, which a request without tools cannot carry."""
    model = FailsOnItsSecondCall("bad_request", text="")
    handler = Handler()
    events = await collect(model, handle_tool_call=handler)
    assert [call.id for call in handler.calls] == ["a"]
    assert events[-1] == TurnFailed("bad_request", "")
    assert model.calls == [TOOLS, TOOLS]  # no third call without the tools
    assert "cannot act" not in caplog.text


async def test_a_failure_in_a_later_round_keeps_the_text_and_the_actions():
    handler = Handler()
    events = await collect(FailsOnItsSecondCall(), handle_tool_call=handler)
    assert [call.id for call in handler.calls] == ["a"]
    assert [type(e).__name__ for e in events] == ["TextDelta", "ActionTaken", "TurnFailed"]
    assert events[-1] == TurnFailed("upstream", "One.")


async def test_calls_of_an_answer_that_failed_are_not_handled():
    model = FakeChatModel(tool_script=[ToolCall("a", "lookup", "{}")], fail_after=1)
    handler = Handler()
    events = await collect(model, handle_tool_call=handler)
    assert handler.calls == [] and events == [TurnFailed("upstream", "")]


# --- the new try one level up (spec 4.6) ---------------------------------------------------------


class RisingModels(FixedModels):
    """Every round is asked of ``model``; each ``retry_up`` is counted, with how many rounds had
    been asked for by then."""

    def __init__(self, model) -> None:
        super().__init__(model)
        self.asked = 0
        self.retried_after: list[int] = []

    async def conversation(self):
        self.asked += 1
        return await super().conversation()

    def retry_up(self) -> bool:
        self.retried_after.append(self.asked)
        return True


def invalid(call: ToolCall, reason: str) -> ActionRecord:
    return ActionRecord(
        tool_name=call.name,
        action_type=None,
        status="invalid",
        confirmation_id=None,
        model_result={"status": "invalid", "reason": reason},
        card={"key": f"action.card.invalid.{reason}", "params": {}, "reason": reason},
    )


class Misfires(Handler):
    """Each call is invalid for the reason its name says (``bad_<reason>``), or done."""

    async def __call__(self, call: ToolCall, *, over_limit: bool = False) -> ActionRecord:
        self.calls.append(call)
        if call.name.startswith("bad_"):
            return invalid(call, call.name.removeprefix("bad_"))
        return record_for(call, id=call.id)


async def test_a_round_whose_calls_could_not_be_acted_on_is_asked_again_one_level_up():
    calls = [ToolCall("a", "bad_unknown_tool", "{}"), ToolCall("b", "bad_invalid", "x")]
    model = FakeChatModel(tool_script=[*calls, "Fixed."])
    models = RisingModels(model)
    events = await collect(model, models=models, handle_tool_call=Misfires())
    # One round per call of the script: each was asked again one level up, once.
    assert models.retried_after == [1, 2]
    assert events[-2:] == [TextDelta("Fixed."), TurnDone(ChatUsage(9, 6))]
    # The new try has the error in its history.
    assert '"reason": "unknown_tool"' in model.calls[1].messages[-1].content


async def test_a_round_with_a_call_acted_on_or_refused_otherwise_is_not_tried_again():
    for script in (
        # One of the calls ran.
        [RoundsModel([ToolCall("a", "bad_unknown_tool", "{}"), ToolCall("b", "lookup", "{}")])],
        # A name that means nobody, or too many calls: no mistake of the model's form.
        [RoundsModel([ToolCall("a", "bad_not_found", "{}")])],
        [RoundsModel([ToolCall("a", "bad_too_many", "{}")])],
    ):
        [model] = script
        models = RisingModels(model)
        await collect(model, models=models, handle_tool_call=Misfires(), max_tool_rounds=1)
        assert models.retried_after == []


def test_the_fixed_models_of_a_test_never_go_up():
    assert FixedModels(FakeChatModel(["ok"])).retry_up() is False


# --- what the descriptions of the tools make Domo say (spec 5.7.3, scenario 21) ---------------

# The phrases in which a tool that anyone may use names the owner: who must agree, or whose rules
# hold. Anything else ("never the owner", "unless the owner chose otherwise") is a sentence about
# the owner that Domo would repeat as it is to the owner himself.
OWNER_PHRASES_ALLOWED = (
    "the owner's yes",
    "the owner must agree",
    "the owner only",
    "the owner's rules",
)
_THE_OWNER = re.compile(r"\bthe owner\b", re.IGNORECASE)


def descriptions_of(schema: object, where: str) -> Iterator[tuple[str, str]]:
    """Every ``description`` of a JSON schema (its properties', its items', at any depth), with
    where it is."""
    if isinstance(schema, dict):
        for key, value in cast("dict[str, object]", schema).items():
            if key == "description" and isinstance(value, str):
                yield where, value
            else:
                yield from descriptions_of(value, f"{where}.{key}")
    elif isinstance(schema, list):
        for index, value in enumerate(cast("list[object]", schema)):
            yield from descriptions_of(value, f"{where}[{index}]")


def test_no_tool_description_has_domo_speak_of_the_owner_to_the_owner():
    """A tool of the owner's alone (``OWNER_ONLY_ACTIONS``) is only ever read in the owner's
    conversation: its texts never say "the owner". Any other tool names the owner only inside one
    of ``OWNER_PHRASES_ALLOWED``. Its own description and those of its parameters alike."""
    found: list[str] = []
    for name, tool in REGISTRY.items():
        owner_only = tool.action_type in OWNER_ONLY_ACTIONS
        texts = [
            ("description", tool.definition.description),
            *descriptions_of(tool.definition.parameters, "parameters"),
        ]
        for where, text in texts:
            for match in _THE_OWNER.finditer(text):
                said = text[match.start() :].lower()
                if not owner_only and said.startswith(OWNER_PHRASES_ALLOWED):
                    continue
                found.append(
                    f"{name} {where}: ...{text[max(0, match.start() - 40) : match.end() + 40]}..."
                )
    assert found == [], "\n".join(found)


def test_what_is_shown_once_is_not_in_the_repr_of_an_action():
    """A listener that logs an event (``Acted``, ``Decided``) must not log a link or a code."""
    record = ActionRecord(
        tool_name="device_entrust",
        action_type="device.entrust",
        status="done",
        confirmation_id=None,
        model_result={"code": "shown_once"},
        card={"key": "action.card.done.device.entrust", "params": {}},
        card_once={"key": "action.card.once.device.entrust", "params": {"code": "ABCD-EFGH-JKLM"}},
    )
    assert "ABCD-EFGH-JKLM" not in repr(record)
    assert record.card_once is not None


# --- what the people tools tell the assistant (Task 25) ---------------------------------------

# The modules of the tools that make, change and look at the people of the house.
_PEOPLE_TOOL_MODULES = (
    "people_tools",
    "link_tools",
    "role_tools",
    "promise_tools",
    "directive_tools",
    "merge_tools",
    "invitation_send",
    "household_admin",
    "household_return",
    "household_read",
    "nickname_tools",
    "memory_tools",
    "profile",
)
# The keys of a result that would hold a sentence to say, rather than data.
_SENTENCE_KEYS = frozenset({"message", "say", "text", "sentence", "reply", "tell"})
_CORE = Path(domo_core.__file__).parent


def test_every_tool_has_a_description():
    missing = [name for name, tool in REGISTRY.items() if not tool.definition.description.strip()]
    assert missing == []


def test_every_people_gesture_ends_with_the_same_sentence_on_his_own_ideas():
    """A gesture on people nobody asked for is the assistant's own idea: every tool of
    ``PEOPLE_GESTURES`` says it last, in one sentence written once (``whose.HIS_OWN_IDEA``)."""
    gestures = {
        name: tool for name, tool in REGISTRY.items() if tool.action_type in PEOPLE_GESTURES
    }
    assert {tool.action_type for tool in gestures.values()} == PEOPLE_GESTURES
    assert "act_on_my_own" in whose.HIS_OWN_IDEA
    wrong = [
        name
        for name, tool in gestures.items()
        if not tool.definition.description.endswith(whose.HIS_OWN_IDEA)
        or tool.definition.description.count(whose.HIS_OWN_IDEA) != 1
    ]
    assert wrong == []


def test_what_is_never_said_between_people_is_written_once():
    """Spec 6.8, on the two tools that show records: one sentence (``BETWEEN_PEOPLE``)."""
    for name in ("record_look", "household_list"):
        assert REGISTRY[name].definition.description.endswith(BETWEEN_PEOPLE), name


def test_no_tool_result_holds_a_sentence_for_a_person():
    """What a people tool gives back is data for the assistant, who writes every text: no key of
    a dictionary those modules build (a literal, an assignment or ``dict(...)``) is named for a
    sentence to say."""
    found: list[str] = []
    for module in _PEOPLE_TOOL_MODULES:
        source = (_CORE / "tools" / f"{module}.py").read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            keys: list[ast.expr | None] = []
            if isinstance(node, ast.Dict):
                keys = list(node.keys)
            elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store):
                keys = [node.slice]
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "dict"
            ):
                keys = [ast.Constant(word.arg) for word in node.keywords]
            for key in keys:
                if isinstance(key, ast.Constant) and key.value in _SENTENCE_KEYS:
                    found.append(f"{module} {key.value}")
    assert found == []


def _core_texts() -> Iterator[tuple[str, str]]:
    for path in sorted(_CORE.rglob("*.py")):
        yield str(path.relative_to(_CORE)), " ".join(path.read_text(encoding="utf-8").split())


def test_no_docstring_still_says_only_a_person_invites():
    """The assistant invites, removes and changes roles too, and a person is a record with or
    without a login: no docstring of the core still says otherwise."""
    stale = (
        "a member of the household they invited",
        "made by the owner or whoever has the right",
        "The owner, or whoever the owner gave the right, invites",
        "nobody but the owner, and he, may change such an invitation",
        "stays the owner's, or the right of whoever the owner gave it to",
        "``role`` is ``owner``, ``adult``, ``child`` or ``guest``",
    )
    found = [(where, said) for where, text in _core_texts() for said in stale if said in text]
    assert found == []


def test_no_docstring_still_says_a_yes_is_always_asked():
    """TLP-11: the judge decides whether a yes is asked; no docstring promises one every time."""
    stale = ("waits for an explicit yes every time", "still confirmed, as always")
    found = [(where, said) for where, text in _core_texts() for said in stale if said in text]
    assert found == []
