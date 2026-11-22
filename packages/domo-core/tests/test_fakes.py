"""The test doubles of the model layer: later tasks build their tests on them."""

import asyncio
import json
from contextlib import aclosing

import fakes
import pytest
from chat_support import ActsBeforeItsCall, ToolCallingModel
from domo_core.llm.models import Provider
from domo_core.llm.types import (
    AssistantToolCalls,
    ChatChunk,
    ChatMessage,
    ChatUsage,
    ModelCallError,
    ToolCall,
    ToolChoice,
    ToolDefinition,
    ToolResult,
)
from fakes import FakeCall, FakeChatModel, fake_factory

MESSAGES = [ChatMessage("user", "hi")]
FIND = ToolDefinition("note_find", "Find a note.", {"type": "object", "properties": {}})
TOOLS = [FIND]
CALL = ToolCall("c1", "note_find", "{}")
OTHER_CALL = ToolCall("c2", "note_find", '{"query": "keys"}')


async def collect(
    model: FakeChatModel,
    system: str = "S",
    limit: int = 32,
    tools: tuple[ToolDefinition, ...] | list[ToolDefinition] = (),
    tool_choice: ToolChoice = "auto",
):
    async with aclosing(
        model.stream(
            system=system,
            messages=MESSAGES,
            max_output_tokens=limit,
            tools=tools,
            tool_choice=tool_choice,
        )
    ) as stream:
        return [event async for event in stream]


async def test_the_chunks_come_one_by_one_then_the_default_usage():
    assert await collect(FakeChatModel(["a", "b"])) == [
        ChatChunk("a"),
        ChatChunk("b"),
        ChatUsage(3, 2),
    ]


async def test_the_usage_can_be_chosen_or_left_out():
    assert (await collect(FakeChatModel(["a"], usage=ChatUsage(9, 8))))[-1] == ChatUsage(9, 8)
    assert await collect(FakeChatModel(["a"], usage=None)) == [ChatChunk("a")]


async def test_the_model_fails_after_the_given_number_of_chunks():
    model = FakeChatModel(["a", "b", "c"], fail_after=2, error_kind="rate_limited")
    seen: list[ChatChunk | ChatUsage] = []
    with pytest.raises(ModelCallError) as e:
        async with aclosing(
            model.stream(system="S", messages=MESSAGES, max_output_tokens=1)
        ) as stream:
            async for event in stream:
                seen.append(event)
    assert seen == [ChatChunk("a"), ChatChunk("b")]
    assert e.value.kind == "rate_limited"


async def test_the_error_kind_defaults_to_upstream_and_zero_fails_at_once():
    with pytest.raises(ModelCallError) as e:
        await collect(FakeChatModel(["a"], fail_after=0))
    assert e.value.kind == "upstream"


async def test_failing_after_all_the_chunks_still_fails_without_the_usage():
    model = FakeChatModel(["a"], fail_after=1)
    seen: list[ChatChunk | ChatUsage] = []
    with pytest.raises(ModelCallError):
        async with aclosing(
            model.stream(system="S", messages=MESSAGES, max_output_tokens=1)
        ) as stream:
            async for event in stream:
                seen.append(event)
    assert seen == [ChatChunk("a")]


async def test_a_failure_point_beyond_the_chunks_never_triggers():
    assert await collect(FakeChatModel(["a"], fail_after=5)) == [ChatChunk("a"), ChatUsage(3, 2)]


async def test_the_delay_is_between_chunks_and_not_before_the_first(
    monkeypatch: pytest.MonkeyPatch,
):
    pauses: list[float] = []
    streamed_when_pausing: list[int] = []
    seen: list[ChatChunk | ChatUsage] = []

    async def recording_sleep(seconds: float) -> None:
        pauses.append(seconds)
        streamed_when_pausing.append(len(seen))

    monkeypatch.setattr(fakes.asyncio, "sleep", recording_sleep)
    model = FakeChatModel(["a", "b", "c"], delay_s=0.05)
    async with aclosing(model.stream(system="S", messages=MESSAGES, max_output_tokens=1)) as stream:
        async for event in stream:
            seen.append(event)
    assert pauses == [0.05, 0.05]
    assert streamed_when_pausing == [1, 2]  # after the first chunk, then after the second


async def test_without_a_delay_the_model_never_sleeps(monkeypatch: pytest.MonkeyPatch):
    async def forbidden_sleep(seconds: float) -> None:
        raise AssertionError("slept")

    monkeypatch.setattr(fakes.asyncio, "sleep", forbidden_sleep)
    assert len(await collect(FakeChatModel(["a", "b", "c"]))) == 4


async def test_a_consumer_can_stop_a_slow_stream():
    model = FakeChatModel(["a"] * 100, delay_s=0.05)
    async with aclosing(model.stream(system="S", messages=MESSAGES, max_output_tokens=1)) as stream:
        first = await anext(stream)
    assert first == ChatChunk("a")


async def test_what_each_call_received_is_recorded():
    model = FakeChatModel(["a"])
    await collect(model, "FIRST", 10)
    await collect(model, "SECOND", 20)
    assert model.calls == [
        FakeCall("FIRST", MESSAGES, (), 10),
        FakeCall("SECOND", MESSAGES, (), 20),
    ]


async def test_the_recorded_messages_are_a_copy():
    model = FakeChatModel(["a"])
    messages = [ChatMessage("user", "hi")]
    async with aclosing(model.stream(system="S", messages=messages, max_output_tokens=1)) as stream:
        async for _ in stream:
            pass
    messages.append(ChatMessage("user", "later"))
    assert [call.messages for call in model.calls] == [[ChatMessage("user", "hi")]]


def test_the_fake_factory_hands_out_the_same_model_whatever_the_provider_and_notes_the_requests():
    model = FakeChatModel(["a"])
    factory = fake_factory(model)
    one = Provider(id="p1", kind="openai", base_url="x", secret_ref=None)
    two = Provider(id="p2", kind="google", base_url="y", secret_ref=None)
    assert factory(one, "m-1") is model
    assert factory(two, "n-1") is model
    assert factory.requested == [("p1", "m-1"), ("p2", "n-1")]


async def test_streams_of_one_model_are_independent():
    model = FakeChatModel(["a", "b"])
    one, two = await asyncio.gather(collect(model), collect(model))
    assert one == two


# --- tools -----------------------------------------------------------------------------------


async def test_each_call_records_the_tools_it_received_as_a_tuple():
    model = FakeChatModel(["a"])
    await collect(model, tools=TOOLS)
    await collect(model)
    assert [call.tools for call in model.calls] == [(FIND,), ()]


async def test_a_recorded_call_is_a_value_that_cannot_be_changed():
    model = FakeChatModel(["a"])
    await collect(model, "S", 5, TOOLS)
    assert model.calls == [FakeCall("S", MESSAGES, (FIND,), 5)]
    with pytest.raises(AttributeError):
        model.calls[0].system = "other"  # pyright: ignore[reportAttributeAccessIssue]


async def test_chunks_are_streamed_on_every_call_whether_or_not_tools_are_given():
    model = FakeChatModel(["a", "b"])
    expected = [ChatChunk("a"), ChatChunk("b"), ChatUsage(3, 2)]
    assert await collect(model) == expected
    assert await collect(model, tools=TOOLS) == expected


async def test_a_tool_script_gives_each_call_the_next_entry():
    model = FakeChatModel(tool_script=[CALL, "done"])
    assert await collect(model, tools=TOOLS) == [CALL, ChatUsage(3, 2)]
    assert await collect(model, tools=TOOLS) == [ChatChunk("done"), ChatUsage(3, 2)]


async def test_a_call_without_tools_skips_the_tool_calls_up_to_the_next_text():
    model = FakeChatModel(tool_script=[CALL, OTHER_CALL, "words", "later"])
    assert await collect(model) == [ChatChunk("words"), ChatUsage(3, 2)]
    # The skipped calls and the text are used up.
    assert await collect(model, tools=TOOLS) == [ChatChunk("later"), ChatUsage(3, 2)]


async def test_once_the_script_is_used_up_a_call_streams_an_empty_text():
    model = FakeChatModel(tool_script=["a"])
    await collect(model)
    assert await collect(model) == [ChatChunk(""), ChatUsage(3, 2)]
    assert await collect(model, tools=TOOLS) == [ChatChunk(""), ChatUsage(3, 2)]


async def test_a_call_without_tools_that_finds_only_tool_calls_left_streams_an_empty_text():
    model = FakeChatModel(tool_script=[CALL, OTHER_CALL])
    assert await collect(model) == [ChatChunk(""), ChatUsage(3, 2)]
    assert await collect(model, tools=TOOLS) == [ChatChunk(""), ChatUsage(3, 2)]


async def test_a_tool_script_can_be_empty():
    assert await collect(FakeChatModel(tool_script=[])) == [ChatChunk(""), ChatUsage(3, 2)]


def test_chunks_and_a_tool_script_cannot_be_given_together():
    with pytest.raises(ValueError):
        FakeChatModel(["a"], tool_script=["b"])
    with pytest.raises(ValueError):
        FakeChatModel(["a"], tool_script=[])


async def test_a_tool_call_is_followed_by_the_usage_unless_there_is_none():
    assert await collect(FakeChatModel(tool_script=[CALL], usage=ChatUsage(9, 8)), tools=TOOLS) == [
        CALL,
        ChatUsage(9, 8),
    ]
    assert await collect(FakeChatModel(tool_script=[CALL], usage=None), tools=TOOLS) == [CALL]


async def test_failing_counts_a_tool_call_as_one_item():
    model = FakeChatModel(tool_script=[CALL], fail_after=1, error_kind="rate_limited")
    seen: list[object] = []
    with pytest.raises(ModelCallError) as e:
        async with aclosing(
            model.stream(system="S", messages=MESSAGES, max_output_tokens=1, tools=TOOLS)
        ) as stream:
            async for event in stream:
                seen.append(event)
    assert seen == [CALL] and e.value.kind == "rate_limited"
    with pytest.raises(ModelCallError):
        await collect(FakeChatModel(tool_script=[CALL], fail_after=0), tools=TOOLS)


async def test_a_model_that_refuses_tools_says_bad_request_as_soon_as_it_is_sent_some():
    model = FakeChatModel(["a"], refuse_tools=True)
    with pytest.raises(ModelCallError) as e:
        await collect(model, tools=TOOLS)
    assert e.value.kind == "bad_request"
    # The call is recorded, and released like any other.
    assert [call.tools for call in model.calls] == [(FIND,)]
    assert (model.started, model.closed) == (1, 1)
    # Without tools it answers.
    assert await collect(model) == [ChatChunk("a"), ChatUsage(3, 2)]


async def test_a_refused_call_does_not_use_up_the_script():
    model = FakeChatModel(tool_script=["a"], refuse_tools=True)
    with pytest.raises(ModelCallError):
        await collect(model, tools=TOOLS)
    assert await collect(model) == [ChatChunk("a"), ChatUsage(3, 2)]


async def test_a_model_that_accepts_tools_is_the_default():
    assert await collect(FakeChatModel(["a"]), tools=TOOLS) == [ChatChunk("a"), ChatUsage(3, 2)]


async def test_the_script_is_shared_by_concurrent_calls_one_entry_each():
    model = FakeChatModel(tool_script=["x", "y"])
    one, two = await asyncio.gather(collect(model), collect(model))
    assert sorted([one[0], two[0]], key=lambda chunk: str(chunk)) == [
        ChatChunk("x"),
        ChatChunk("y"),
    ]


async def test_each_call_records_its_tool_choice():
    model = FakeChatModel(["a"])
    await collect(model, tools=TOOLS)
    await collect(model, tools=TOOLS, tool_choice="none")
    assert [call.tool_choice for call in model.calls] == ["auto", "none"]
    assert model.calls[1] == FakeCall("S", MESSAGES, (FIND,), 32, "none")


async def test_a_call_that_may_not_call_tools_skips_the_tool_calls_like_one_without_tools():
    model = FakeChatModel(tool_script=[CALL, "words", CALL, "later"])
    assert await collect(model, tools=TOOLS, tool_choice="none") == [
        ChatChunk("words"),
        ChatUsage(3, 2),
    ]
    # With the choice left to the model the call is made, as before.
    assert await collect(model, tools=TOOLS) == [CALL, ChatUsage(3, 2)]
    assert await collect(model, tools=TOOLS, tool_choice="auto") == [
        ChatChunk("later"),
        ChatUsage(3, 2),
    ]


async def test_a_model_that_refuses_tools_refuses_them_even_when_it_may_not_call_them():
    model = FakeChatModel(["a"], refuse_tools=True)
    with pytest.raises(ModelCallError) as e:
        await collect(model, tools=TOOLS, tool_choice="none")
    assert e.value.kind == "bad_request"
    assert await collect(model, tool_choice="none") == [ChatChunk("a"), ChatUsage(3, 2)]


# --- the judge of the yes ----------------------------------------------------------------------

JUDGE_SYSTEM = fakes.JUDGE_FIRST_LINE + "\n\nThe rest of the judge's prompt."


async def test_the_fake_model_answers_the_judge_apart_from_the_script():
    model = FakeChatModel(tool_script=[CALL, "text"], judge="proof: x")
    assert await collect(model, system=JUDGE_SYSTEM, limit=80) == [
        ChatChunk("proof: x"),
        ChatUsage(3, 2),
    ]
    # The script is untouched: the classifier, then the main model, take their entries.
    assert await collect(model, tools=TOOLS) == [CALL, ChatUsage(3, 2)]
    assert await collect(model, system=JUDGE_SYSTEM) == [ChatChunk("proof: x"), ChatUsage(3, 2)]
    assert await collect(model) == [ChatChunk("text"), ChatUsage(3, 2)]
    # The judge's calls are recorded apart.
    assert model.judge_calls() == [
        FakeCall(JUDGE_SYSTEM, MESSAGES, (), 80),
        FakeCall(JUDGE_SYSTEM, MESSAGES, (), 32),
    ]
    assert [call.tools for call in model.calls] == [(FIND,), ()]
    assert (model.started, model.closed) == (4, 4)

    # With judge=None, the judge's call is served like any other: it takes the script's next
    # entry (a call without tools skips the tool calls up to the next text).
    served = FakeChatModel(tool_script=[CALL, "text"], judge=None)
    assert await collect(served, system=JUDGE_SYSTEM) == [ChatChunk("text"), ChatUsage(3, 2)]
    assert served.judge_calls() == served.calls == [FakeCall(JUDGE_SYSTEM, MESSAGES, (), 32)]
    # And so it fails like any other.
    with pytest.raises(ModelCallError):
        await collect(FakeChatModel(fail_after=0, judge=None), system=JUDGE_SYSTEM)


async def test_the_fake_judge_says_yes_unless_told_otherwise_and_follows_a_list():
    assert await collect(FakeChatModel(["hello"]), system=JUDGE_SYSTEM) == [
        ChatChunk("yes"),
        ChatUsage(3, 2),
    ]
    listed = FakeChatModel(["hello"], judge=["do", "do_not: rude"])
    answers = [(await collect(listed, system=JUDGE_SYSTEM))[0] for _ in range(3)]
    assert answers == [ChatChunk("do"), ChatChunk("do_not: rude"), ChatChunk("yes")]
    # Any other call streams the chunks, as before.
    assert await collect(listed) == [ChatChunk("hello"), ChatUsage(3, 2)]
    # Only a prompt that starts with the judge's first line is the judge's.
    assert await collect(listed, system="Be brief.\n" + fakes.JUDGE_FIRST_LINE) == [
        ChatChunk("hello"),
        ChatUsage(3, 2),
    ]


async def test_the_tool_calling_models_answer_the_judge():
    call = ToolCall("c1", "note_find", "{}")

    async def nothing() -> None:
        return None

    async def stream(model, system: str, tools=()):
        return [
            item
            async for item in model.stream(
                system=system, messages=MESSAGES, max_output_tokens=80, tools=tools
            )
        ]

    for model in (
        ToolCallingModel(call, judge="do: fine"),
        ActsBeforeItsCall(nothing, call, judge="do: fine"),
    ):
        assert await stream(model, JUDGE_SYSTEM) == [ChatChunk("do: fine"), ChatUsage(3, 2)]
        # Any other call without tools (the classifier) is "unclear", as before.
        assert (await stream(model, "You classify one reply."))[0] == ChatChunk("unclear")
        # The first call that may use tools makes the call, the next ones say "ok".
        assert (await stream(model, "S", TOOLS))[0] == call
        assert (await stream(model, JUDGE_SYSTEM))[0] == ChatChunk("do: fine")
        assert (await stream(model, "S", TOOLS))[0] == ChatChunk("ok")
    # By default, the judge says yes.
    assert (await stream(ToolCallingModel(call), JUDGE_SYSTEM))[0] == ChatChunk("yes")
    assert (await stream(ActsBeforeItsCall(nothing, call), JUDGE_SYSTEM))[0] == ChatChunk("yes")


# --- the writer and the checker of the texts ----------------------------------------------------

WRITER_SYSTEM = fakes.TEXT_WRITER_FIRST_LINE + "\n\nThe rest of the writer's prompt."
CHECKER_SYSTEM = fakes.TEXT_CHECKER_FIRST_LINE + "\n\nThe rest of the checker's prompt."
FOR_SAM = {
    "language": "de",
    "name": "Sam",
    "role": "child",
    "writing_note": "Du.",
    "indication": None,
}
TO_WRITE = {
    "for_whom": FOR_SAM,
    "texts": {
        "a.hello": {"meaning": "Hello {name}", "holes": ["name"], "max_length": 64},
        "a.bye": {"meaning": "Bye", "holes": [], "max_length": 46, "current": "Tschüss"},
    },
}


async def answer_of(model, system: str, messages: list, tools=()) -> list:
    async with aclosing(
        model.stream(system=system, messages=messages, max_output_tokens=512, tools=tools)
    ) as stream:
        return [item async for item in stream]


def the_call(items: list) -> tuple[str, dict]:
    """The one call of an answer, its name and its arguments, and the usage after it."""
    [call] = [item for item in items if isinstance(item, ToolCall)]
    assert items[-1] == ChatUsage(3, 2)
    return call.name, json.loads(call.arguments)


async def test_the_fake_writer_gives_each_meaning_or_the_current_text_apart_from_the_script():
    model = FakeChatModel(tool_script=[CALL, "text"])
    asked = [ChatMessage("user", json.dumps(TO_WRITE))]
    assert the_call(await answer_of(model, WRITER_SYSTEM, asked)) == (
        "submit_texts",
        {"texts": {"a.hello": "Hello {name}", "a.bye": "Tschüss"}},
    )
    # The script is untouched, and the writer's call is not among the calls.
    assert await collect(model, tools=TOOLS) == [CALL, ChatUsage(3, 2)]
    assert [call.system for call in model.writer_calls()] == [WRITER_SYSTEM]
    assert model.writer_calls()[0].messages == asked
    assert [call.system for call in model.calls] == ["S"]
    assert (model.started, model.closed) == (2, 2)


async def test_the_fake_writer_follows_its_function_and_answers_the_errors_alone():
    seen: list[tuple[str, str, fakes.ForWhom]] = []

    def writer(key: str, meaning: str, for_whom: fakes.ForWhom) -> str:
        seen.append((key, meaning, for_whom))
        return f"{meaning}!"

    model = FakeChatModel(writer=writer)
    asked = [ChatMessage("user", json.dumps(TO_WRITE))]
    _, first = the_call(await answer_of(model, WRITER_SYSTEM, asked))
    assert first == {"texts": {"a.hello": "Hello {name}!", "a.bye": "Bye!"}}
    sam = fakes.ForWhom(language="de", name="Sam", role="child", writing_note="Du.")
    assert seen == [("a.hello", "Hello {name}", sam), ("a.bye", "Bye", sam)]

    # Given errors as the result of its call, it writes again the keys named, and those alone.
    call = ToolCall("w1", "submit_texts", json.dumps(first))
    errors = {"accepted": False, "errors": {"a.bye": "too loud"}}
    again = [
        *asked,
        AssistantToolCalls("", (call,)),
        ToolResult("w1", "submit_texts", json.dumps(errors)),
    ]
    assert the_call(await answer_of(model, WRITER_SYSTEM, again)) == (
        "submit_texts",
        {"texts": {"a.bye": "Bye!"}},
    )
    assert len(model.writer_calls()) == 2 and model.calls == []


async def test_the_fake_checker_refuses_nothing_unless_its_function_says_so():
    review = {
        "language": "de",
        "texts": {
            "a.hello": {"meaning": "Hello {name}", "holes": ["name"], "text": "Hallo {name}"},
            "a.bye": {"meaning": "Bye", "holes": [], "text": "Bye"},
        },
    }
    asked = [ChatMessage("user", json.dumps(review))]
    model = FakeChatModel(["hello"])
    assert the_call(await answer_of(model, CHECKER_SYSTEM, asked)) == (
        "submit_review",
        {"refused": []},
    )

    checked: list[tuple[str, str]] = []

    def checker(key: str, text: str) -> str | None:
        checked.append((key, text))
        return "not German" if text == "Bye" else None

    model = FakeChatModel(["hello"], checker=checker)
    assert the_call(await answer_of(model, CHECKER_SYSTEM, asked)) == (
        "submit_review",
        {"refused": [{"key": "a.bye", "why": "not German"}]},
    )
    assert checked == [("a.hello", "Hallo {name}"), ("a.bye", "Bye")]
    assert [call.system for call in model.checker_calls()] == [CHECKER_SYSTEM]
    # Any other call streams the chunks, as before; only the exact first line counts.
    assert await collect(model) == [ChatChunk("hello"), ChatUsage(3, 2)]
    assert await collect(model, system="Be brief.\n" + fakes.TEXT_CHECKER_FIRST_LINE) == [
        ChatChunk("hello"),
        ChatUsage(3, 2),
    ]
    assert len(model.calls) == 2
