"""The model adapters and tool calling: how tools are declared, how the tool calls that arrive in a
stream are assembled, and how a tool call and its result go back to each provider.

The wire format is checked for each provider (OpenAI and the servers that imitate it, Anthropic,
Google); what an adapter owes its callers (one complete ``ToolCall`` per call, in order, before the
usage; nothing for a call that was cut short; the guarantees of the 1a adapters) runs against all
four kinds. The scripted relay and the streams are those of ``llm_support.py``.
"""

import base64
import json
import re
from collections.abc import Sequence
from contextlib import aclosing
from typing import Any

import pytest
from domo_core.llm.types import (
    AssistantToolCalls,
    ChatChunk,
    ChatMessage,
    ChatUsage,
    HistoryItem,
    ModelCallError,
    ToolCall,
    ToolChoice,
    ToolDefinition,
    ToolResult,
)
from llm_support import (
    CORE_TOKEN,
    GATEKEEPER_URL,
    RELAY_PATHS,
    SSE_HEADERS,
    ScriptedToolCall,
    run,
    text_of,
    tool_calls_of,
)

ALL_KINDS = ["openai", "openai_compatible", "anthropic", "google"]
OPENAI_KINDS = ["openai", "openai_compatible"]

NO_ARGUMENTS = {"type": "object", "properties": {}}
LIST_HOUSEHOLD = ToolDefinition("household_list", "List the household.", NO_ARGUMENTS)
FIND_NOTE_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string", "description": "What to look for."}},
    "required": ["query"],
}
FIND_NOTE = ToolDefinition("note_find", "Find a note.", FIND_NOTE_SCHEMA)
TOOLS = [LIST_HOUSEHOLD]

ASK = ChatMessage("user", "who?")
LIST_CALL = ToolCall("c1", "household_list", "{}")
LIST_RESULT = ToolResult("c1", "household_list", '{"status":"done"}')
FIND_CALL = ToolCall("c2", "note_find", '{"query":"keys"}')
FIND_RESULT = ToolResult("c2", "note_find", '{"status":"done","notes":[]}')

# The arguments of a call, cut where no one would cut them.
KEYS_PIECES = ['{"quer', 'y": "ke', 'ys"}']

# What Gemini 3 puts on a function call: opaque bytes, which come as base64 text. These make
# the same text in the standard and in the URL-safe alphabet (no "+", "/", "-" or "_").
SIGNATURE = base64.b64encode(b"opaque-signature-of-thought").decode()
OTHER_SIGNATURE = base64.b64encode(b"signature-bytes-0123456789").decode()


def assert_call_id(kind: str, call: ToolCall, provider_id: str) -> None:
    """OpenAI and Anthropic name their calls; Google does not, and the adapter makes one up."""
    if kind == "google":
        assert re.fullmatch(r"call-\d+", call.id)
    else:
        assert call.id == provider_id


# --- declaring the tools ---------------------------------------------------------------------


@pytest.mark.parametrize("kind", OPENAI_KINDS)
async def test_openai_tools_are_declared_as_functions(wire_for, kind):
    wire = wire_for(kind)
    wire.reply_text(["ok"])
    await run(wire.model, tools=[LIST_HOUSEHOLD, FIND_NOTE])
    assert wire.last.json()["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "household_list",
                "description": "List the household.",
                "parameters": NO_ARGUMENTS,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "note_find",
                "description": "Find a note.",
                "parameters": FIND_NOTE_SCHEMA,
            },
        },
    ]


async def test_anthropic_tools_are_declared_with_an_input_schema(wire_for):
    wire = wire_for("anthropic")
    wire.reply_text(["ok"])
    await run(wire.model, tools=[LIST_HOUSEHOLD, FIND_NOTE])
    assert wire.last.json()["tools"] == [
        {
            "name": "household_list",
            "description": "List the household.",
            "input_schema": NO_ARGUMENTS,
        },
        {
            "name": "note_find",
            "description": "Find a note.",
            "input_schema": FIND_NOTE_SCHEMA,
            # The last definition carries the mark that makes the provider cache all of them.
            "cache_control": {"type": "ephemeral"},
        },
    ]


async def test_google_tools_are_declared_as_function_declarations(wire_for):
    """The SDK writes the schema under the field's own name (``parameters_json_schema``), which
    the API takes as it takes ``parametersJsonSchema``."""
    wire = wire_for("google")
    wire.reply_text(["ok"])
    await run(wire.model, tools=[FIND_NOTE])
    assert wire.last.json()["tools"] == [
        {
            "functionDeclarations": [
                {
                    "name": "note_find",
                    "description": "Find a note.",
                    "parameters_json_schema": FIND_NOTE_SCHEMA,
                }
            ]
        }
    ]


async def test_google_declares_a_tool_without_parameters_without_a_schema(wire_for):
    """Gemini refuses an object schema with no property: such a function declares none."""
    wire = wire_for("google")
    wire.reply_text(["ok"])
    await run(wire.model, tools=[LIST_HOUSEHOLD, FIND_NOTE])
    declarations = wire.last.json()["tools"][0]["functionDeclarations"]
    assert declarations[0] == {"name": "household_list", "description": "List the household."}
    assert declarations[1]["parameters_json_schema"] == FIND_NOTE_SCHEMA


@pytest.mark.parametrize("kind", ALL_KINDS)
async def test_tools_are_declared_only_when_given(wire_for, kind):
    wire = wire_for(kind)
    wire.reply_text(["hi"])
    await run(wire.model)
    body = wire.last.json()
    assert not {"tools", "tool_choice", "toolConfig"} & set(body)
    assert "tools" not in body.get("generationConfig", {})


# What each provider is told when the model may see the tools but not call them.
NO_CALLS_ALLOWED = {
    "openai": ("tool_choice", "none"),
    "openai_compatible": ("tool_choice", "none"),
    "anthropic": ("tool_choice", {"type": "none"}),
    "google": ("toolConfig", {"functionCallingConfig": {"mode": "NONE"}}),
}


@pytest.mark.parametrize("kind", ALL_KINDS)
async def test_tool_choice_none_keeps_the_tools_and_forbids_calling_them(wire_for, kind):
    wire = wire_for(kind)
    wire.reply_text(["ok"])
    await run(wire.model, tools=TOOLS, tool_choice="none")
    body = wire.last.json()
    field, value = NO_CALLS_ALLOWED[kind]
    assert body[field] == value
    assert "tools" in body
    others = {"tool_choice", "toolConfig"} - {field}
    assert not others & set(body)


@pytest.mark.parametrize("kind", ALL_KINDS)
@pytest.mark.parametrize("choice", ["auto", None])
async def test_tool_choice_auto_sends_nothing_about_the_choice(wire_for, kind, choice):
    """``auto`` is what every provider does with tools by default: it is the default here too."""
    wire = wire_for(kind)
    wire.reply_text(["ok"])
    if choice is None:
        await run(wire.model, tools=TOOLS)
    else:
        await run(wire.model, tools=TOOLS, tool_choice=choice)
    body = wire.last.json()
    assert "tools" in body
    assert not {"tool_choice", "toolConfig"} & set(body)


@pytest.mark.parametrize("kind", ALL_KINDS)
@pytest.mark.parametrize("choice", ["auto", "none"])
async def test_tool_choice_means_nothing_without_tools(wire_for, kind, choice):
    wire = wire_for(kind)
    wire.reply_text(["ok"])
    await run(wire.model, tool_choice=choice)
    assert not {"tools", "tool_choice", "toolConfig"} & set(wire.last.json())


@pytest.mark.parametrize("kind", ALL_KINDS)
async def test_a_tool_call_request_has_nothing_the_1a_request_did_not(wire_for, kind):
    """Only the tools are added: no tool choice, no automatic execution, no other setting."""
    wire = wire_for(kind)
    wire.reply_text(["hi"])
    await run(wire.model)
    without_tools = set(wire.last.json())
    await run(wire.model, tools=TOOLS)
    assert set(wire.last.json()) - without_tools == {"tools"}


# --- assembling the calls of a stream --------------------------------------------------------


@pytest.mark.parametrize("kind", ALL_KINDS)
async def test_a_streamed_tool_call_is_assembled(wire_for, kind):
    wire = wire_for(kind)
    wire.reply_tool_calls(ScriptedToolCall("household_list", ["{", "}"], id="c1"))
    calls = tool_calls_of(await run(wire.model, messages=[ASK], tools=TOOLS))
    assert len(calls) == 1
    assert calls[0].name == "household_list"
    assert json.loads(calls[0].arguments) == {}
    assert_call_id(kind, calls[0], "c1")


@pytest.mark.parametrize("kind", ALL_KINDS)
async def test_arguments_cut_in_pieces_are_joined(wire_for, kind):
    wire = wire_for(kind)
    wire.reply_tool_calls(ScriptedToolCall("note_find", KEYS_PIECES, id="c2"))
    (call,) = tool_calls_of(await run(wire.model, tools=[FIND_NOTE]))
    assert call.name == "note_find"
    assert json.loads(call.arguments) == {"query": "keys"}
    assert isinstance(call, ToolCall)


@pytest.mark.parametrize("kind", ALL_KINDS)
@pytest.mark.parametrize("pieces", [[], [""]])
async def test_a_call_without_arguments_has_an_empty_object_for_them(wire_for, kind, pieces):
    """A tool with no parameter is called with nothing, and nothing is ``{}``."""
    wire = wire_for(kind)
    wire.reply_tool_calls(ScriptedToolCall("household_list", pieces))
    (call,) = tool_calls_of(await run(wire.model, tools=TOOLS))
    assert call.arguments == "{}"


@pytest.mark.parametrize("kind", ALL_KINDS)
async def test_the_text_comes_first_then_the_calls_in_order_then_the_usage(wire_for, kind):
    wire = wire_for(kind)
    wire.reply_tool_calls(
        ScriptedToolCall("household_list", ["{}"], id="c1"),
        ScriptedToolCall("note_find", KEYS_PIECES, id="c2"),
        texts=["Let me ", "look."],
        usage=(11, 7),
    )
    events = await run(wire.model, tools=[LIST_HOUSEHOLD, FIND_NOTE])
    assert [type(e) for e in events] == [ChatChunk, ChatChunk, ToolCall, ToolCall, ChatUsage]
    assert text_of(events) == "Let me look."
    first, second = tool_calls_of(events)
    assert (first.name, second.name) == ("household_list", "note_find")
    assert json.loads(second.arguments) == {"query": "keys"}
    assert first.id != second.id
    assert events[-1] == ChatUsage(11, 7)


@pytest.mark.parametrize("kind", ALL_KINDS)
async def test_an_answer_with_no_call_streams_no_call_even_when_tools_were_given(wire_for, kind):
    wire = wire_for(kind)
    wire.reply_text(["Nobody."])
    events = await run(wire.model, tools=TOOLS)
    assert tool_calls_of(events) == []
    assert text_of(events) == "Nobody."


@pytest.mark.parametrize("kind", ["openai", "openai_compatible", "anthropic"])
async def test_a_call_cut_short_is_never_streamed(wire_for, kind):
    """The connection drops while the arguments arrive: the half call is not a call."""
    wire = wire_for(kind)
    call = ScriptedToolCall("note_find", KEYS_PIECES, id="c2")
    probe = wire.reply_broken_in_tool_call(call)
    seen: list[Any] = []
    with pytest.raises(ModelCallError) as excinfo:
        async with aclosing(
            wire.model.stream(system="S", messages=[ASK], max_output_tokens=32, tools=[FIND_NOTE])
        ) as stream:
            async for event in stream:
                seen.append(event)
    assert excinfo.value.kind == "unreachable"
    assert tool_calls_of(seen) == []
    assert probe.closed


@pytest.mark.parametrize("kind", ALL_KINDS)
async def test_the_response_is_closed_before_the_calls_are_handed_over(wire_for, kind):
    """A caller that acts on a call (and takes its time) holds no connection to the provider."""
    wire = wire_for(kind)
    probe = wire.reply_tracked([], tool_calls=[ScriptedToolCall("household_list", ["{}"])])
    async with aclosing(
        wire.model.stream(system="S", messages=[ASK], max_output_tokens=32, tools=TOOLS)
    ) as stream:
        async for event in stream:
            if isinstance(event, ToolCall):
                assert probe.closed
                break
        else:
            pytest.fail("no tool call")


# --- an answer cut by the token limit --------------------------------------------------------

# How each streaming provider says that the answer stopped at a limit.
CUT_BY_A_LIMIT = [
    ("openai", {"finish_reason": "length"}),
    ("openai_compatible", {"finish_reason": "length"}),
    ("anthropic", {"stop_reason": "max_tokens"}),
    ("anthropic", {"stop_reason": "model_context_window_exceeded"}),
]


@pytest.mark.parametrize(("kind", "cut"), CUT_BY_A_LIMIT)
@pytest.mark.parametrize(
    "pieces", [[], ['{"quer']], ids=["before-the-arguments", "in-the-arguments"]
)
async def test_a_call_cut_by_the_token_limit_is_not_streamed(wire_for, kind, cut, pieces):
    """The call being written when the answer stopped is half a call, or, when nothing of its
    arguments came yet, a call that looks like one with no arguments: neither is a call."""
    wire = wire_for(kind)
    wire.reply_tool_calls(ScriptedToolCall("note_find", pieces, id="c2"), texts=["Let me "], **cut)
    events = await run(wire.model, tools=[FIND_NOTE])
    assert tool_calls_of(events) == []
    assert text_of(events) == "Let me "
    # The answer says that it was cut: the caller may give it more room.
    assert events[-1] == ChatUsage(3, 2, cut_short=True)


@pytest.mark.parametrize(("kind", "cut"), CUT_BY_A_LIMIT)
async def test_an_answer_cut_by_the_token_limit_streams_no_call_even_the_complete_ones(
    wire_for, kind, cut
):
    """Conservative: an answer that ran out of tokens is not trusted to have said what it meant."""
    wire = wire_for(kind)
    wire.reply_tool_calls(
        ScriptedToolCall("household_list", ["{}"], id="c1"),
        ScriptedToolCall("note_find", ['{"quer'], id="c2"),
        **cut,
    )
    assert tool_calls_of(await run(wire.model, tools=[LIST_HOUSEHOLD, FIND_NOTE])) == []


@pytest.mark.parametrize("kind", ["openai", "openai_compatible", "anthropic"])
async def test_an_answer_that_ended_normally_streams_its_calls(wire_for, kind):
    wire = wire_for(kind)
    wire.reply_tool_calls(ScriptedToolCall("household_list", ["{}"], id="c1"))
    assert len(tool_calls_of(await run(wire.model, tools=TOOLS))) == 1


# --- what the servers that imitate OpenAI do -------------------------------------------------


def openai_body(*deltas: dict[str, Any]) -> bytes:
    """Chunks of a chat completion stream with the given deltas, then [DONE]."""
    events = [
        {
            "id": "c",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "test-model",
            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
        }
        for delta in deltas
    ]
    lines = [f"data: {json.dumps(event)}\n\n" for event in events] + ["data: [DONE]\n\n"]
    return "".join(lines).encode()


def tool_delta(index: int, *, id: str | None = None, name: str | None = None, args: str | None):
    function: dict[str, Any] = {}
    if name is not None:
        function["name"] = name
    if args is not None:
        function["arguments"] = args
    delta: dict[str, Any] = {"index": index, "function": function}
    if id is not None:
        delta["id"] = id
        delta["type"] = "function"
    return {"tool_calls": [delta]}


@pytest.mark.parametrize("kind", OPENAI_KINDS)
async def test_openai_calls_that_arrive_interleaved_are_told_apart_by_their_index(wire_for, kind):
    wire = wire_for(kind)
    wire.reply(
        200,
        headers=SSE_HEADERS,
        body=openai_body(
            tool_delta(0, id="a", name="household_list", args=""),
            tool_delta(1, id="b", name="note_find", args=""),
            tool_delta(1, args='{"query":'),
            tool_delta(0, args="{}"),
            tool_delta(1, args=' "keys"}'),
        ),
    )
    first, second = tool_calls_of(await run(wire.model, tools=[LIST_HOUSEHOLD, FIND_NOTE]))
    assert (first.id, first.name, first.arguments) == ("a", "household_list", "{}")
    assert (second.id, second.name) == ("b", "note_find")
    assert json.loads(second.arguments) == {"query": "keys"}


@pytest.mark.parametrize("kind", OPENAI_KINDS)
async def test_an_openai_call_that_comes_whole_in_one_chunk_is_a_call(wire_for, kind):
    wire = wire_for(kind)
    wire.reply(
        200,
        headers=SSE_HEADERS,
        body=openai_body(tool_delta(0, id="a", name="note_find", args='{"query":"keys"}')),
    )
    (call,) = tool_calls_of(await run(wire.model, tools=[FIND_NOTE]))
    assert call == ToolCall("a", "note_find", '{"query":"keys"}')


@pytest.mark.parametrize("kind", OPENAI_KINDS)
async def test_a_server_that_sends_no_call_identifier_gets_one_from_the_adapter(wire_for, kind):
    wire = wire_for(kind)
    wire.reply(
        200,
        headers=SSE_HEADERS,
        body=openai_body(
            tool_delta(0, name="household_list", args="{}"),
            tool_delta(1, name="household_list", args="{}"),
        ),
    )
    first, second = tool_calls_of(await run(wire.model, tools=TOOLS))
    assert re.fullmatch(r"call-\d+", first.id) and re.fullmatch(r"call-\d+", second.id)
    assert first.id != second.id


@pytest.mark.parametrize("kind", OPENAI_KINDS)
async def test_a_server_that_gives_every_call_the_same_index_still_has_two_calls(wire_for, kind):
    """Some servers number each call 0: a new identifier begins a new call."""
    wire = wire_for(kind)
    wire.reply(
        200,
        headers=SSE_HEADERS,
        body=openai_body(
            tool_delta(0, id="a", name="household_list", args="{}"),
            tool_delta(0, id="b", name="note_find", args='{"query":"keys"}'),
        ),
    )
    first, second = tool_calls_of(await run(wire.model, tools=[LIST_HOUSEHOLD, FIND_NOTE]))
    assert (first.id, first.name, first.arguments) == ("a", "household_list", "{}")
    assert (second.id, second.name) == ("b", "note_find")


@pytest.mark.parametrize("kind", OPENAI_KINDS)
async def test_a_server_that_repeats_the_identifier_in_every_chunk_still_has_one_call(
    wire_for, kind
):
    wire = wire_for(kind)
    wire.reply(
        200,
        headers=SSE_HEADERS,
        body=openai_body(
            tool_delta(0, id="a", name="note_find", args='{"query":'),
            tool_delta(0, id="a", args='"keys"}'),
        ),
    )
    (call,) = tool_calls_of(await run(wire.model, tools=[FIND_NOTE]))
    assert call == ToolCall("a", "note_find", '{"query":"keys"}')


@pytest.mark.parametrize("kind", OPENAI_KINDS)
async def test_a_chunk_without_a_name_is_not_a_call(wire_for, kind):
    wire = wire_for(kind)
    wire.reply(200, headers=SSE_HEADERS, body=openai_body(tool_delta(0, id="a", args="{}")))
    assert tool_calls_of(await run(wire.model, tools=TOOLS)) == []


# --- what Anthropic and Google do ------------------------------------------------------------


async def test_anthropic_arguments_are_not_mixed_with_the_deltas_of_other_blocks(wire_for):
    wire = wire_for("anthropic")
    wire.reply_tool_calls(
        ScriptedToolCall("note_find", KEYS_PIECES, id="toolu_1"),
        texts=["Looking."],
        extra_events=True,
    )
    (call,) = tool_calls_of(await run(wire.model, tools=[FIND_NOTE]))
    assert call == ToolCall("toolu_1", "note_find", '{"query": "keys"}')


async def test_google_ignores_thinking_summaries_around_a_call(wire_for):
    wire = wire_for("google")
    wire.reply_tool_calls(
        ScriptedToolCall("note_find", ['{"query": "keys"}']), thoughts=["I should look it up"]
    )
    events = await run(wire.model, tools=[FIND_NOTE])
    assert text_of(events) == ""
    (call,) = tool_calls_of(events)
    assert json.loads(call.arguments) == {"query": "keys"}


async def test_google_makes_up_identifiers_that_do_not_repeat(wire_for):
    """Google sends none: the adapter's are unique, also from one call to the model to the next."""
    wire = wire_for("google")
    wire.reply_tool_calls(ScriptedToolCall("household_list"), ScriptedToolCall("household_list"))
    first = tool_calls_of(await run(wire.model, tools=TOOLS))
    second = tool_calls_of(await run(wire.model, tools=TOOLS))
    ids = [call.id for call in first + second]
    assert len(ids) == 4 and len(set(ids)) == 4
    assert all(re.fullmatch(r"call-\d+", i) for i in ids)


# --- provider state: what a provider wants back with a call ----------------------------------


async def test_google_keeps_the_thought_signature_of_a_call_as_its_provider_state(wire_for):
    wire = wire_for("google")
    wire.reply_tool_calls(
        ScriptedToolCall("note_find", ['{"query": "keys"}'], thought_signature=SIGNATURE)
    )
    (call,) = tool_calls_of(await run(wire.model, tools=[FIND_NOTE]))
    assert call.provider_state == SIGNATURE
    assert (call.name, json.loads(call.arguments)) == ("note_find", {"query": "keys"})


async def test_google_sends_the_signature_back_on_the_function_call_part(wire_for):
    wire = wire_for("google")
    call = ToolCall("call-1", "note_find", '{"query":"keys"}', SIGNATURE)
    await send(wire, answer_of([AssistantToolCalls("", (call,)), FIND_RESULT]), [FIND_NOTE])
    part = wire.last.json()["contents"][1]["parts"][0]
    assert part == {
        "functionCall": {"name": "note_find", "args": {"query": "keys"}},
        "thoughtSignature": SIGNATURE,
    }


async def test_a_signature_goes_round_the_adapter_unchanged(wire_for):
    """What the stream carried is what the next request carries, on the call it belonged to."""
    wire = wire_for("google")
    wire.reply_tool_calls(
        ScriptedToolCall("household_list", thought_signature=SIGNATURE),
        ScriptedToolCall("note_find", ['{"query": "keys"}']),
        ScriptedToolCall("household_list", thought_signature=OTHER_SIGNATURE),
    )
    calls = tool_calls_of(await run(wire.model, tools=[LIST_HOUSEHOLD, FIND_NOTE]))
    assert [c.provider_state for c in calls] == [SIGNATURE, None, OTHER_SIGNATURE]
    results = [ToolResult(c.id, c.name, '{"status":"done"}') for c in calls]
    await send(wire, answer_of([AssistantToolCalls("", tuple(calls)), *results]))
    parts = wire.last.json()["contents"][1]["parts"]
    assert [p.get("thoughtSignature") for p in parts] == [SIGNATURE, None, OTHER_SIGNATURE]
    assert [p["functionCall"]["name"] for p in parts] == [
        "household_list",
        "note_find",
        "household_list",
    ]


async def test_the_signature_bytes_are_not_changed_by_the_alphabet_of_their_base64(wire_for):
    """Whatever alphabet the API writes the bytes in, the same bytes go back."""
    raw = bytes([0xFB, 0xFF, 0xFE, 1, 2, 3, 250, 251])
    standard = base64.b64encode(raw).decode()
    assert "+" in standard or "/" in standard
    wire = wire_for("google")
    wire.reply_tool_calls(ScriptedToolCall("household_list", thought_signature=standard))
    (call,) = tool_calls_of(await run(wire.model, tools=TOOLS))
    assert base64.b64decode(call.provider_state or "") == raw
    await send(wire, answer_of([AssistantToolCalls("", (call,)), LIST_RESULT]))
    sent = wire.last.json()["contents"][1]["parts"][0]["thoughtSignature"]
    assert base64.urlsafe_b64decode(sent) == raw


async def test_a_call_without_a_signature_sends_none(wire_for):
    wire = wire_for("google")
    wire.reply_tool_calls(ScriptedToolCall("household_list"))
    (call,) = tool_calls_of(await run(wire.model, tools=TOOLS))
    assert call.provider_state is None
    await send(wire, answer_of([AssistantToolCalls("", (call,)), LIST_RESULT]))
    part = wire.last.json()["contents"][1]["parts"][0]
    assert "thoughtSignature" not in part and "thought_signature" not in part


async def test_a_provider_state_that_is_not_base64_is_not_sent_and_does_not_fail_the_call(
    wire_for,
):
    wire = wire_for("google")
    call = ToolCall("call-1", "household_list", "{}", "not base64!")
    await send(wire, answer_of([AssistantToolCalls("", (call,)), LIST_RESULT]))
    part = wire.last.json()["contents"][1]["parts"][0]
    assert part == {"functionCall": {"name": "household_list", "args": {}}}


@pytest.mark.parametrize("kind", ["openai", "openai_compatible", "anthropic"])
async def test_the_other_adapters_have_no_provider_state_and_ignore_it(wire_for, kind):
    wire = wire_for(kind)
    wire.reply_tool_calls(ScriptedToolCall("household_list", id="c1"))
    (call,) = tool_calls_of(await run(wire.model, tools=TOOLS))
    assert call.provider_state is None
    call = ToolCall("c1", "household_list", "{}", "state-nobody-else-understands")
    await send(wire, answer_of([AssistantToolCalls("", (call,)), LIST_RESULT]))
    assert "state-nobody-else-understands" not in wire.last.body.decode()


# --- sending a call and its result back ------------------------------------------------------


def answer_of(items: Sequence[HistoryItem]) -> list[HistoryItem]:
    return [ASK, *items]


async def send(
    wire: Any,
    history: Sequence[HistoryItem],
    tools: Sequence[ToolDefinition] = TOOLS,
    tool_choice: ToolChoice = "auto",
):
    wire.reply_text(["ok"])
    return await run(wire.model, messages=history, tools=tools, tool_choice=tool_choice)


@pytest.mark.parametrize("kind", OPENAI_KINDS)
async def test_openai_gets_the_call_on_the_assistant_message_and_the_result_as_a_tool_message(
    wire_for, kind
):
    wire = wire_for(kind)
    await send(wire, answer_of([AssistantToolCalls("", (LIST_CALL,)), LIST_RESULT]))
    assert wire.last.json()["messages"] == [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "who?"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "household_list", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": '{"status":"done"}'},
    ]


@pytest.mark.parametrize("kind", OPENAI_KINDS)
async def test_openai_keeps_what_the_assistant_said_before_calling(wire_for, kind):
    wire = wire_for(kind)
    await send(wire, answer_of([AssistantToolCalls("Let me look.", (LIST_CALL,)), LIST_RESULT]))
    assert wire.last.json()["messages"][2]["content"] == "Let me look."


@pytest.mark.parametrize("kind", OPENAI_KINDS)
async def test_openai_gets_one_tool_message_per_result_in_order(wire_for, kind):
    wire = wire_for(kind)
    history = answer_of([AssistantToolCalls("", (LIST_CALL, FIND_CALL)), FIND_RESULT, LIST_RESULT])
    await send(wire, history, [LIST_HOUSEHOLD, FIND_NOTE])
    messages = wire.last.json()["messages"]
    assert [c["id"] for c in messages[2]["tool_calls"]] == ["c1", "c2"]
    assert [(m["role"], m["tool_call_id"]) for m in messages[3:]] == [
        ("tool", "c2"),
        ("tool", "c1"),
    ]


async def test_anthropic_gets_the_call_as_a_tool_use_and_the_result_in_a_user_message(wire_for):
    wire = wire_for("anthropic")
    await send(wire, answer_of([AssistantToolCalls("", (LIST_CALL,)), LIST_RESULT]))
    assert wire.last.json()["messages"] == [
        {"role": "user", "content": "who?"},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "c1", "name": "household_list", "input": {}}],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "c1", "content": '{"status":"done"}'}
            ],
        },
    ]


async def test_anthropic_keeps_what_the_assistant_said_before_calling(wire_for):
    wire = wire_for("anthropic")
    await send(wire, answer_of([AssistantToolCalls("Let me look.", (FIND_CALL,)), FIND_RESULT]))
    assert wire.last.json()["messages"][1]["content"] == [
        {"type": "text", "text": "Let me look."},
        {"type": "tool_use", "id": "c2", "name": "note_find", "input": {"query": "keys"}},
    ]


async def test_anthropic_gets_the_results_of_one_answer_in_a_single_user_message(wire_for):
    """The API wants every result right after the calls, in one message."""
    wire = wire_for("anthropic")
    history = answer_of([AssistantToolCalls("", (LIST_CALL, FIND_CALL)), FIND_RESULT, LIST_RESULT])
    await send(wire, history, [LIST_HOUSEHOLD, FIND_NOTE])
    messages = wire.last.json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert [b["id"] for b in messages[1]["content"]] == ["c1", "c2"]
    assert [b["tool_use_id"] for b in messages[2]["content"]] == ["c2", "c1"]


async def test_anthropic_goes_on_with_the_conversation_after_the_results(wire_for):
    wire = wire_for("anthropic")
    history = answer_of(
        [
            AssistantToolCalls("", (LIST_CALL,)),
            LIST_RESULT,
            ChatMessage("assistant", "Nobody yet."),
            ChatMessage("user", "ok"),
        ]
    )
    await send(wire, history)
    roles = [m["role"] for m in wire.last.json()["messages"]]
    assert roles == ["user", "assistant", "user", "assistant", "user"]


async def test_google_gets_the_call_as_a_function_call_and_the_result_as_a_function_response(
    wire_for,
):
    wire = wire_for("google")
    await send(wire, answer_of([AssistantToolCalls("", (LIST_CALL,)), LIST_RESULT]))
    assert wire.last.json()["contents"] == [
        {"role": "user", "parts": [{"text": "who?"}]},
        {"role": "model", "parts": [{"functionCall": {"name": "household_list", "args": {}}}]},
        {
            "role": "user",
            "parts": [
                {"functionResponse": {"name": "household_list", "response": {"status": "done"}}}
            ],
        },
    ]


async def test_google_keeps_what_the_assistant_said_before_calling(wire_for):
    wire = wire_for("google")
    await send(wire, answer_of([AssistantToolCalls("Let me look.", (FIND_CALL,)), FIND_RESULT]))
    assert wire.last.json()["contents"][1]["parts"] == [
        {"text": "Let me look."},
        {"functionCall": {"name": "note_find", "args": {"query": "keys"}}},
    ]


async def test_google_matches_results_to_calls_by_name_and_order(wire_for):
    """It has no identifier: the results follow the calls, one part each, with the name."""
    wire = wire_for("google")
    history = answer_of([AssistantToolCalls("", (LIST_CALL, FIND_CALL)), LIST_RESULT, FIND_RESULT])
    await send(wire, history, [LIST_HOUSEHOLD, FIND_NOTE])
    contents = wire.last.json()["contents"]
    assert [c["role"] for c in contents] == ["user", "model", "user"]
    assert [p["functionCall"]["name"] for p in contents[1]["parts"]] == [
        "household_list",
        "note_find",
    ]
    assert [p["functionResponse"]["name"] for p in contents[2]["parts"]] == [
        "household_list",
        "note_find",
    ]
    assert "id" not in json.dumps(contents)


async def test_google_wraps_a_result_that_is_not_an_object(wire_for):
    wire = wire_for("google")
    for content in ("Nobody lives here.", "[1, 2]", "42"):
        history = answer_of(
            [AssistantToolCalls("", (LIST_CALL,)), ToolResult("c1", "household_list", content)]
        )
        await send(wire, history)
        part = wire.last.json()["contents"][2]["parts"][0]
        assert part["functionResponse"]["response"] == {"output": content}


@pytest.mark.parametrize("arguments", ["not json", "[1, 2]", '"text"', ""])
async def test_arguments_that_are_not_an_object_go_back_as_an_empty_one_where_one_is_needed(
    wire_for, arguments
):
    """The model can produce anything: what was stored is replayed without failing the call."""
    call = ToolCall("c1", "household_list", arguments)
    history = answer_of([AssistantToolCalls("", (call,)), LIST_RESULT])

    wire = wire_for("anthropic")
    await send(wire, history)
    assert wire.last.json()["messages"][1]["content"][0]["input"] == {}

    wire = wire_for("google")
    await send(wire, history)
    assert wire.last.json()["contents"][1]["parts"][0]["functionCall"]["args"] == {}

    # The servers that imitate OpenAI parse the arguments of the history for their templates.
    for kind in OPENAI_KINDS:
        wire = wire_for(kind)
        await send(wire, history)
        sent = wire.last.json()["messages"][2]["tool_calls"][0]["function"]["arguments"]
        assert sent == "{}"


@pytest.mark.parametrize("kind", OPENAI_KINDS)
async def test_openai_replays_arguments_that_are_an_object_as_received(wire_for, kind):
    wire = wire_for(kind)
    call = ToolCall("c2", "note_find", '{ "query":  "keys" }')
    await send(wire, answer_of([AssistantToolCalls("", (call,)), FIND_RESULT]), [FIND_NOTE])
    sent = wire.last.json()["messages"][2]["tool_calls"][0]["function"]["arguments"]
    assert sent == '{ "query":  "keys" }'


@pytest.mark.parametrize("kind", ALL_KINDS)
async def test_the_last_round_of_a_turn_keeps_the_tools_and_forbids_calls(wire_for, kind):
    """A history that holds calls and results is sent with the tools declared (Anthropic refuses
    it otherwise) and the model told not to call any."""
    wire = wire_for(kind)
    history = answer_of([AssistantToolCalls("", (LIST_CALL,)), LIST_RESULT])
    await send(wire, history, TOOLS, "none")
    body = wire.last.json()
    field, value = NO_CALLS_ALLOWED[kind]
    assert body[field] == value and "tools" in body


# --- what stays as in 1a ---------------------------------------------------------------------


@pytest.mark.parametrize("kind", ALL_KINDS)
async def test_a_call_with_tools_goes_to_the_relay_with_the_token_and_the_timeouts(wire_for, kind):
    wire = wire_for(kind)
    wire.reply_tool_calls(ScriptedToolCall("household_list"))
    await run(wire.model, tools=TOOLS)
    assert wire.last.path.startswith(RELAY_PATHS[kind])
    assert wire.last.url.startswith(GATEKEEPER_URL)
    assert wire.last.timeouts == {"connect": 10.0, "read": 320.0, "write": 60.0, "pool": 10.0}
    carried = [
        value
        for name, value in wire.last.headers.items()
        if name.lower() in ("authorization", "x-api-key", "x-goog-api-key")
    ]
    assert carried in ([CORE_TOKEN], [f"Bearer {CORE_TOKEN}"])
    assert [r.url for r in wire.requests if not r.url.startswith(GATEKEEPER_URL)] == []


@pytest.mark.parametrize("kind", ALL_KINDS)
async def test_the_core_token_is_not_in_the_body_of_a_request_with_tools(wire_for, kind):
    wire = wire_for(kind)
    wire.reply_text(["ok"])
    history = answer_of([AssistantToolCalls("", (LIST_CALL,)), LIST_RESULT])
    await run(wire.model, messages=history, tools=TOOLS)
    assert CORE_TOKEN not in wire.last.body.decode()


@pytest.mark.parametrize("kind", ALL_KINDS)
@pytest.mark.parametrize(("status", "error_kind"), [(400, "bad_request"), (500, "upstream")])
async def test_a_failed_call_with_tools_is_one_request_and_a_classified_error(
    wire_for, kind, status, error_kind
):
    """A model that does not know tools answers 400: the caller learns it (``bad_request``)."""
    wire = wire_for(kind)
    wire.reply(status, headers={"content-type": "application/json"}, body=b"{}")
    with pytest.raises(ModelCallError) as excinfo:
        await run(wire.model, tools=TOOLS)
    assert excinfo.value.kind == error_kind
    assert len(wire.requests) == 1


@pytest.mark.parametrize("kind", ALL_KINDS)
async def test_stopping_early_closes_the_response_of_a_call_with_tools(wire_for, kind):
    wire = wire_for(kind)
    probe = wire.reply_tracked(
        [f"piece{i}" for i in range(20)],
        usage=None,
        tool_calls=[ScriptedToolCall("household_list")],
    )
    async with aclosing(
        wire.model.stream(system="S", messages=[ASK], max_output_tokens=32, tools=TOOLS)
    ) as stream:
        async for event in stream:
            if isinstance(event, ChatChunk):
                break
    assert probe.closed
    assert probe.sent < len(probe.chunks)
