"""The model adapters: what each SDK sends through the gatekeeper's relay, what comes back as
``ChatChunk``/``ChatUsage``, and how every failure becomes a ``ModelCallError``.

The tests of the wire format are one per provider; the contract every adapter must keep (streaming,
usage, error mapping, clean-up) runs against all four kinds.
"""

import asyncio
import json
import logging
from contextlib import aclosing

import pytest
from domo_core.llm.factory import build_chat_model
from domo_core.llm.provider_kinds import PROVIDER_KINDS
from domo_core.llm.types import (
    ChatChunk,
    ChatMessage,
    ChatUsage,
    ModelCallError,
    ToolDefinition,
)
from llm_support import (
    CORE_TOKEN,
    GATEKEEPER_URL,
    HELLO,
    RELAY_PATHS,
    SSE_HEADERS,
    TOKEN_FRAGMENT,
    TOKEN_HEADERS,
    anthropic_sse,
    provider,
    run,
    text_of,
)

ALL_KINDS = ["openai", "openai_compatible", "anthropic", "google"]
HISTORY = [
    ChatMessage("user", "hi"),
    ChatMessage("assistant", "yo"),
    ChatMessage("user", "again"),
]


# --- the wire format of each provider ------------------------------------------------------


async def test_openai_compatible_adapter_streams_text_through_the_relay(wire_for):
    wire = wire_for("openai_compatible")
    wire.reply_text(["Hel", "lo"])
    events = await run(wire.model)
    assert text_of(events) == "Hello"
    assert wire.last.url == f"{GATEKEEPER_URL}/llm/p1/chat/completions"
    assert wire.last.headers["authorization"] == f"Bearer {CORE_TOKEN}"
    body = wire.last.json()
    assert body["model"] == "test-model"
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["max_tokens"] == 32 and "max_completion_tokens" not in body
    assert body["messages"] == [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "hi"},
    ]


async def test_the_openai_adapter_asks_for_max_completion_tokens(wire_for):
    wire = wire_for("openai")
    wire.reply_text(["ok"])
    await run(wire.model)
    body = wire.last.json()
    assert body["max_completion_tokens"] == 32 and "max_tokens" not in body
    assert wire.last.url == f"{GATEKEEPER_URL}/llm/p1/chat/completions"
    assert wire.last.headers["authorization"] == f"Bearer {CORE_TOKEN}"


async def test_the_openai_adapter_sends_the_whole_history_in_order(wire_for):
    wire = wire_for("openai_compatible")
    wire.reply_text(["ok"])
    await run(wire.model, messages=HISTORY)
    assert [m["role"] for m in wire.last.json()["messages"]] == [
        "system",
        "user",
        "assistant",
        "user",
    ]


async def test_anthropic_adapter_sends_system_separately(wire_for):
    wire = wire_for("anthropic")
    wire.reply_text(["ok"])
    await run(wire.model, system="SYS", max_output_tokens=8)
    assert wire.last.url == f"{GATEKEEPER_URL}/llm/p1/v1/messages"
    body = wire.last.json()
    assert body["system"][0]["text"] == "SYS"
    assert all(m["role"] != "system" for m in body["messages"])
    assert body["model"] == "test-model" and body["max_tokens"] == 8 and body["stream"] is True


async def test_anthropic_adapter_keeps_the_roles_of_the_history(wire_for):
    wire = wire_for("anthropic")
    wire.reply_text(["ok"])
    await run(wire.model, messages=HISTORY)
    messages = wire.last.json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]


async def test_google_adapter_maps_assistant_to_model_role(wire_for):
    wire = wire_for("google")
    wire.reply_text(["ok"])
    await run(wire.model, messages=HISTORY, max_output_tokens=8)
    assert wire.last.url.startswith(f"{GATEKEEPER_URL}/llm/p1/")
    assert "streamGenerateContent" in wire.last.path
    body = wire.last.json()
    assert [c["role"] for c in body["contents"]] == ["user", "model", "user"]
    assert [c["parts"] for c in body["contents"]][1] == [{"text": "yo"}]
    assert body["systemInstruction"]["parts"] == [{"text": "S"}]
    assert body["generationConfig"]["maxOutputTokens"] == 8
    assert wire.last.headers["x-goog-api-key"] == CORE_TOKEN


@pytest.mark.parametrize("kind", ALL_KINDS)
async def test_the_relay_path_is_the_one_the_gatekeeper_forwards(wire_for, kind):
    wire = wire_for(kind)
    wire.reply_text(["ok"])
    await run(wire.model)
    assert wire.last.path.startswith(RELAY_PATHS[kind])


@pytest.mark.parametrize("kind", ALL_KINDS)
async def test_the_core_token_travels_where_the_gatekeeper_looks_for_it(wire_for, kind):
    """The gatekeeper takes the token from ``authorization``, ``x-api-key`` or ``x-goog-api-key``
    on the relay routes, whichever the SDK uses; it is the only credential the request carries."""
    wire = wire_for(kind)
    wire.reply_text(["ok"])
    await run(wire.model)
    carried = {
        name: value for name, value in wire.last.headers.items() if name.lower() in TOKEN_HEADERS
    }
    assert list(carried.values()) in ([CORE_TOKEN], [f"Bearer {CORE_TOKEN}"])


def test_the_providers_key_goes_where_the_provider_expects_it():
    """Where the gatekeeper injects the real key is the provider's business, not the SDK's."""
    assert PROVIDER_KINDS["anthropic"].key_location.name == "x-api-key"
    assert PROVIDER_KINDS["google"].key_location.name == "x-goog-api-key"
    assert PROVIDER_KINDS["openai"].key_location.name == "authorization"


@pytest.mark.parametrize("kind", ALL_KINDS)
async def test_an_empty_system_prompt_sends_no_system_text(wire_for, kind):
    wire = wire_for(kind)
    wire.reply_text(["ok"])
    await run(wire.model, system="")
    body = wire.last.json()
    assert "system" not in body and "systemInstruction" not in body
    assert all(m.get("role") != "system" for m in body.get("messages", []))


# --- streaming -----------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ALL_KINDS)
async def test_the_text_is_streamed_piece_by_piece_in_order(wire_for, kind):
    wire = wire_for(kind)
    wire.reply_text(["Hel", "lo", " there"])
    events = await run(wire.model)
    assert [e.text for e in events if isinstance(e, ChatChunk)] == ["Hel", "lo", " there"]


@pytest.mark.parametrize("kind", ALL_KINDS)
async def test_the_usage_comes_once_at_the_end(wire_for, kind):
    wire = wire_for(kind)
    wire.reply_text(["a", "b"], usage=(11, 7))
    events = await run(wire.model)
    assert events[-1] == ChatUsage(11, 7)
    assert [type(e) for e in events] == [ChatChunk, ChatChunk, ChatUsage]


@pytest.mark.parametrize("kind", ["openai", "openai_compatible", "google"])
async def test_no_usage_is_streamed_when_the_provider_reports_none(wire_for, kind):
    wire = wire_for(kind)
    wire.reply_text(["a"], usage=None)
    events = await run(wire.model)
    assert events == [ChatChunk("a")]


async def test_a_server_that_repeats_the_usage_in_every_chunk_still_yields_it_once(wire_for):
    wire = wire_for("openai_compatible")
    wire.reply_text(["a", "b", "c"], usage=(5, 4), usage_in_every_chunk=True)
    events = await run(wire.model)
    assert [e for e in events if isinstance(e, ChatUsage)] == [ChatUsage(5, 4)]
    assert text_of(events) == "abc"


async def test_anthropic_marks_the_system_and_the_tools_for_its_cache(wire_for):
    wire = wire_for("anthropic")
    wire.reply_text(["ok"])
    tools = [
        ToolDefinition("first_tool", "One.", {"type": "object", "properties": {}}),
        ToolDefinition("last_tool", "Two.", {"type": "object", "properties": {}}),
    ]
    await run(wire.model, system="SYS", max_output_tokens=8, tools=tools)
    body = wire.last.json()
    assert body["system"] == [
        {"type": "text", "text": "SYS", "cache_control": {"type": "ephemeral"}}
    ]
    assert "cache_control" not in body["tools"][0]
    assert body["tools"][-1]["cache_control"] == {"type": "ephemeral"}


async def test_anthropic_counts_its_cache_in_the_input(wire_for):
    wire = wire_for("anthropic")
    wire.reply_text(["ok"], usage=(10, 4), cache=(300, 50))
    events = await run(wire.model)
    assert events[-1] == ChatUsage(360, 4, cache_read_tokens=300, cache_write_tokens=50)


async def test_openai_reports_its_cached_tokens(wire_for):
    wire = wire_for("openai")
    wire.reply_text(["ok"], usage=(100, 4), cached_tokens=64)
    events = await run(wire.model)
    assert events[-1] == ChatUsage(100, 4, cache_read_tokens=64)
    wire.reply_text(["ok"], usage=(100, 4))
    assert (await run(wire.model))[-1] == ChatUsage(100, 4)


async def test_google_reports_its_cached_tokens(wire_for):
    wire = wire_for("google")
    wire.reply_text(["ok"], usage=(100, 4), cached_tokens=64)
    events = await run(wire.model)
    assert events[-1] == ChatUsage(100, 4, cache_read_tokens=64)
    wire.reply_text(["ok"], usage=(100, 4))
    assert (await run(wire.model))[-1] == ChatUsage(100, 4)


async def test_anthropic_ignores_events_that_are_not_text(wire_for):
    wire = wire_for("anthropic")
    wire.reply_text(["ok"], extra_events=True)
    assert await run(wire.model) == [ChatChunk("ok"), ChatUsage(3, 2)]


async def test_google_ignores_thinking_summaries(wire_for):
    wire = wire_for("google")
    wire.reply_text(["answer"], thoughts=["I should think about this"])
    assert text_of(await run(wire.model)) == "answer"


@pytest.mark.parametrize("kind", ALL_KINDS)
async def test_a_stream_can_arrive_in_arbitrary_pieces(wire_for, kind):
    """The SSE events do not line up with the network chunks: three events in one, and a cut in
    the middle of an event."""
    wire = wire_for(kind)
    probe = wire.reply_tracked(["Hel", "lo"])
    whole = b"".join(probe.chunks)
    probe.chunks[:] = [whole[:7], whole[7:40], whole[40:]]
    assert text_of(await run(wire.model)) == "Hello"


# --- failures ------------------------------------------------------------------------------

STATUS_KINDS = [
    (401, "auth"),
    (403, "auth"),
    (409, "auth"),
    (429, "rate_limited"),
    (400, "bad_request"),
    (404, "bad_request"),
    (422, "bad_request"),
    (502, "unreachable"),
    (503, "unreachable"),
    (504, "unreachable"),
    (500, "upstream"),
    (408, "upstream"),
    (418, "upstream"),
    (529, "upstream"),
]


@pytest.mark.parametrize("kind", ALL_KINDS)
@pytest.mark.parametrize(("status", "error_kind"), STATUS_KINDS)
async def test_http_statuses_map_to_error_kinds(wire_for, kind, status, error_kind):
    wire = wire_for(kind)
    wire.reply(status, headers={"content-type": "application/json"}, body=b'{"error": "x"}')
    with pytest.raises(ModelCallError) as excinfo:
        await run(wire.model)
    assert excinfo.value.kind == error_kind


@pytest.mark.parametrize("kind", ALL_KINDS)
async def test_an_error_page_that_is_not_json_is_still_mapped(wire_for, kind):
    wire = wire_for(kind)
    wire.reply(502, headers={"content-type": "text/html"}, body=b"<html>Bad gateway</html>")
    with pytest.raises(ModelCallError) as excinfo:
        await run(wire.model)
    assert excinfo.value.kind == "unreachable"


@pytest.mark.parametrize("kind", ALL_KINDS)
async def test_a_connection_error_is_unreachable(wire_for, kind):
    wire = wire_for(kind)
    wire.fail("connect")
    with pytest.raises(ModelCallError) as excinfo:
        await run(wire.model)
    assert excinfo.value.kind == "unreachable"


@pytest.mark.parametrize("kind", ALL_KINDS)
async def test_a_timeout_is_unreachable(wire_for, kind):
    wire = wire_for(kind)
    wire.fail("timeout")
    with pytest.raises(ModelCallError) as excinfo:
        await run(wire.model)
    assert excinfo.value.kind == "unreachable"


@pytest.mark.parametrize("kind", ALL_KINDS)
async def test_a_connection_dropped_mid_stream_keeps_the_text_and_is_unreachable(wire_for, kind):
    wire = wire_for(kind)
    wire.reply_broken(["Hel", "lo"])
    received: list[str] = []
    with pytest.raises(ModelCallError) as excinfo:
        async with aclosing(
            wire.model.stream(system="S", messages=HELLO, max_output_tokens=32)
        ) as stream:
            async for event in stream:
                if isinstance(event, ChatChunk):
                    received.append(event.text)
    assert received == ["Hel"]
    assert excinfo.value.kind == "unreachable"


@pytest.mark.parametrize("kind", ALL_KINDS)
@pytest.mark.parametrize("status", [429, 500, 503, 401])
async def test_a_failed_call_is_not_retried(wire_for, kind, status):
    """Retries and escalation belong to the router of a later plan."""
    wire = wire_for(kind)
    wire.reply(status, headers={"content-type": "application/json"}, body=b"{}")
    with pytest.raises(ModelCallError):
        await run(wire.model)
    assert len(wire.requests) == 1


@pytest.mark.parametrize("kind", ALL_KINDS)
async def test_a_redirect_is_not_followed(wire_for, kind):
    """The core's token must go to the gatekeeper and nowhere else."""
    wire = wire_for(kind)
    wire.reply(302, headers={"location": "http://evil.example.net/steal"})
    with pytest.raises(ModelCallError) as excinfo:
        await run(wire.model)
    assert excinfo.value.kind == "upstream"
    assert [r.url for r in wire.requests if "evil.example.net" in r.url] == []
    assert len(wire.requests) == 1


@pytest.mark.parametrize("kind", ALL_KINDS)
async def test_an_answer_that_is_not_a_stream_of_events_is_an_upstream_error(wire_for, kind):
    """A misconfigured address can answer 200 with a web page: that is not a model."""
    wire = wire_for(kind)
    wire.reply(200, headers={"content-type": "text/html"}, body=b"<html>Welcome</html>")
    with pytest.raises(ModelCallError) as excinfo:
        await run(wire.model)
    assert excinfo.value.kind == "upstream"


async def test_an_error_event_in_an_openai_stream_is_an_upstream_error(wire_for):
    wire = wire_for("openai_compatible")
    wire.reply(
        200,
        headers=SSE_HEADERS,
        body=b'data: {"error": {"message": "the server fell over"}}\n\n',
    )
    with pytest.raises(ModelCallError) as excinfo:
        await run(wire.model)
    assert excinfo.value.kind == "upstream"


async def test_an_error_event_in_an_anthropic_stream_is_an_upstream_error(wire_for):
    wire = wire_for("anthropic")
    wire.reply(
        200,
        headers=SSE_HEADERS,
        body=(
            b"event: error\n"
            b'data: {"type": "error", "error": {"type": "overloaded_error", "message": "x"}}\n\n'
        ),
    )
    with pytest.raises(ModelCallError) as excinfo:
        await run(wire.model)
    assert excinfo.value.kind == "upstream"


async def test_a_malformed_event_is_an_upstream_error_and_not_an_sdk_exception(wire_for):
    wire = wire_for("openai_compatible")
    wire.reply(200, headers=SSE_HEADERS, body=b"data: {this is not json\n\n")
    with pytest.raises(ModelCallError) as excinfo:
        await run(wire.model)
    assert excinfo.value.kind == "upstream"


@pytest.mark.parametrize("kind", ALL_KINDS)
@pytest.mark.parametrize("how", ["status", "connect", "broken"])
async def test_an_error_never_carries_the_token_or_a_header(wire_for, kind, how):
    wire = wire_for(kind)
    if how == "status":
        wire.reply(401, headers={"content-type": "application/json"}, body=b"{}")
    elif how == "connect":
        wire.fail("connect")
    else:
        wire.reply_broken(["Hel", "lo"])
    with pytest.raises(ModelCallError) as excinfo:
        await run(wire.model)
    error = excinfo.value
    for text in (str(error), repr(error), str(error.args)):
        assert TOKEN_FRAGMENT not in text
        assert "authorization" not in text.lower() and "bearer" not in text.lower()
    # The SDK's exception holds the request, and so the token: it is not chained.
    assert error.__cause__ is None and error.__context__ is None
    assert error.args == (error.kind,)


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_a_model_never_shows_the_token_in_its_repr(wire_for, kind):
    model = wire_for(kind).model
    for text in (repr(model), str(model), f"{model!r}", f"{model}"):
        assert TOKEN_FRAGMENT not in text
        assert "gatekeeper.test/llm/p1" in text


@pytest.mark.parametrize("kind", ALL_KINDS)
@pytest.mark.parametrize("failing", [False, True])
async def test_a_model_call_never_logs_the_token(wire_for, kind, failing, caplog):
    """At INFO, as the core runs, and at DEBUG too, where the HTTP libraries log the most."""
    caplog.set_level(logging.DEBUG)
    wire = wire_for(kind)
    if failing:
        wire.reply(401, headers={"content-type": "application/json"}, body=b"{}")
    else:
        wire.reply_text(["ok"])
    try:
        await run(wire.model)
    except ModelCallError:
        assert failing
    assert [r for r in caplog.records if TOKEN_FRAGMENT in r.getMessage()] == []


# --- timeouts ------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ALL_KINDS)
async def test_the_read_timeout_is_above_the_gatekeepers_so_its_504_reaches_the_core(
    wire_for, kind
):
    wire = wire_for(kind)
    wire.reply_text(["ok"])
    await run(wire.model)
    assert wire.last.timeouts["read"] == 320.0


@pytest.mark.parametrize("kind", ALL_KINDS)
async def test_every_sdk_request_has_the_documented_timeouts_on_the_wire(wire_for, kind):
    """Connect 10 s, read 320 s, write 60 s, pool 10 s: a gatekeeper that stalls is reported
    after 10 s, not after the read timeout. (The Google SDK replaces the client's timeouts with
    one scalar unless the core puts them back.)"""
    wire = wire_for(kind)
    wire.reply_text(["ok"])
    await run(wire.model)
    assert wire.last.timeouts == {"connect": 10.0, "read": 320.0, "write": 60.0, "pool": 10.0}


@pytest.mark.parametrize("kind", ALL_KINDS)
async def test_no_server_timeout_header_goes_to_the_provider(wire_for, kind):
    wire = wire_for(kind)
    wire.reply_text(["ok"])
    await run(wire.model)
    assert "x-server-timeout" not in {name.lower() for name in wire.last.headers}


# --- clean-up ------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ALL_KINDS)
async def test_stopping_early_closes_the_response(wire_for, kind):
    wire = wire_for(kind)
    probe = wire.reply_tracked([f"piece{i}" for i in range(20)], usage=None)
    async with aclosing(
        wire.model.stream(system="S", messages=HELLO, max_output_tokens=32)
    ) as stream:
        async for event in stream:
            if isinstance(event, ChatChunk):
                break
    assert probe.closed
    assert probe.sent < len(probe.chunks)


@pytest.mark.parametrize("kind", ALL_KINDS)
async def test_a_consumer_that_just_breaks_out_of_the_loop_still_releases_the_response(
    wire_for, kind
):
    """Without ``aclosing`` the generator is finalized when it is dropped: later, but surely."""
    wire = wire_for(kind)
    probe = wire.reply_tracked([f"piece{i}" for i in range(20)], usage=None)
    async for event in wire.model.stream(system="S", messages=HELLO, max_output_tokens=32):
        if isinstance(event, ChatChunk):
            break
    for _ in range(50):
        if probe.closed:
            break
        await asyncio.sleep(0.01)
    assert probe.closed


@pytest.mark.parametrize("kind", ALL_KINDS)
async def test_cancelling_the_consumer_while_the_provider_is_quiet_closes_the_response(
    wire_for, kind
):
    """The browser went away in the middle of an answer: the task is cancelled."""
    wire = wire_for(kind)
    probe = wire.reply_stalled(["Hel", "lo"])
    got_the_first_chunk = asyncio.Event()

    async def consume() -> None:
        async with aclosing(
            wire.model.stream(system="S", messages=HELLO, max_output_tokens=32)
        ) as stream:
            async for event in stream:
                if isinstance(event, ChatChunk):
                    got_the_first_chunk.set()

    task = asyncio.create_task(consume())
    await asyncio.wait_for(got_the_first_chunk.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert probe.closed


@pytest.mark.parametrize("kind", ALL_KINDS)
async def test_closing_the_generator_closes_the_response(wire_for, kind):
    wire = wire_for(kind)
    probe = wire.reply_tracked([f"piece{i}" for i in range(20)], usage=None)
    stream = wire.model.stream(system="S", messages=HELLO, max_output_tokens=32)
    first = await anext(stream)
    assert isinstance(first, ChatChunk)
    assert not probe.closed
    await stream.aclose()
    assert probe.closed


@pytest.mark.parametrize("kind", ALL_KINDS)
async def test_reading_to_the_end_closes_the_response(wire_for, kind):
    wire = wire_for(kind)
    probe = wire.reply_tracked(["a", "b"])
    await run(wire.model)
    assert probe.closed


@pytest.mark.parametrize("kind", ALL_KINDS)
async def test_a_failure_closes_the_response_too(wire_for, kind):
    wire = wire_for(kind)
    probe = wire.reply_broken(["Hel", "lo"])
    with pytest.raises(ModelCallError):
        await run(wire.model)
    assert probe.closed


@pytest.mark.parametrize("kind", ALL_KINDS)
async def test_a_model_can_be_asked_again(wire_for, kind):
    wire = wire_for(kind)
    wire.reply_text(["ok"])
    first = await run(wire.model)
    second = await run(wire.model)
    assert first == second
    assert len(wire.requests) == 2


# --- request limits ------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ALL_KINDS)
async def test_the_request_body_is_valid_json(wire_for, kind):
    wire = wire_for(kind)
    wire.reply_text(["ok"])
    await run(wire.model, system='Say "hi"\nin two words', messages=[ChatMessage("user", "é✓")])
    assert isinstance(json.loads(wire.last.body), dict)


# --- a short answer: the model is asked not to think first ---------------------------------
#
# The current Claude models think before they answer unless told not to, and the thinking counts
# in ``max_tokens``: a call of four tokens got a thinking block cut short and no text at all
# (measured with a real model, 2026-11-15).


def _refusal(message: str) -> tuple[int, bytes]:
    """A 400 of the messages API, as the relay passes it on."""
    error = {"type": "invalid_request_error", "message": message}
    return 400, json.dumps({"type": "error", "error": error}).encode()


# What a model that cannot have its thinking disabled answers (the provider's own words).
USE_BETWEEN_TOOLS = _refusal(
    'To turn thinking off on this model, send "thinking": {"type": "between_tools"} instead of '
    '{"type": "disabled"}. The model does not think before responding. The short updates it '
    "writes between tool calls come back as thinking blocks."
)
# What a model that knows no such parameter answers.
THINKING_UNKNOWN = _refusal("thinking: Extra inputs are not permitted")
OK_STREAM = (200, anthropic_sse(["yes"]))


async def test_anthropic_says_nothing_about_thinking_unless_asked_not_to_think(wire_for):
    wire = wire_for("anthropic")
    wire.reply_text(["ok"])
    await run(wire.model)
    assert "thinking" not in wire.last.json()


async def test_anthropic_is_told_not_to_think_for_a_short_answer(wire_for):
    wire = wire_for("anthropic")
    wire.reply_text(["yes"])
    events = await run(wire.model, max_output_tokens=4, think=False)
    assert text_of(events) == "yes"
    body = wire.last.json()
    assert body["thinking"] == {"type": "disabled"}
    # The answer is still bounded by what the caller asked.
    assert body["max_tokens"] == 4
    assert len(wire.requests) == 1


async def test_anthropic_sends_the_type_the_provider_names_instead_and_remembers_it(wire_for):
    wire = wire_for("anthropic")
    wire.reply_in_turn(USE_BETWEEN_TOOLS, OK_STREAM)
    events = await run(wire.model, max_output_tokens=4, think=False)
    assert text_of(events) == "yes"
    assert [request.json()["thinking"] for request in wire.requests] == [
        {"type": "disabled"},
        {"type": "between_tools"},
    ]
    assert all(request.json()["max_tokens"] == 4 for request in wire.requests)
    # The next short call to that model starts with what worked: no refusal to pay for again.
    await run(wire.model, max_output_tokens=4, think=False)
    assert len(wire.requests) == 3
    assert wire.last.json()["thinking"] == {"type": "between_tools"}
    # A call that may think is sent as before.
    await run(wire.model)
    assert "thinking" not in wire.last.json()


async def test_what_one_model_wants_is_not_sent_to_another(wire_for, gk_client):
    wire = wire_for("anthropic")
    wire.reply_in_turn(USE_BETWEEN_TOOLS, OK_STREAM)
    await run(wire.model, think=False)
    other = build_chat_model(
        provider("anthropic"), "other-model", gk_client, transport=wire.transport
    )
    wire.reply_text(["yes"])
    await run(other, think=False)
    assert wire.last.json()["model"] == "other-model"
    assert wire.last.json()["thinking"] == {"type": "disabled"}


async def test_anthropic_asks_again_without_the_parameter_a_model_does_not_know(wire_for):
    wire = wire_for("anthropic")
    wire.reply_in_turn(THINKING_UNKNOWN, OK_STREAM)
    events = await run(wire.model, max_output_tokens=4, think=False)
    assert text_of(events) == "yes"
    assert wire.requests[0].json()["thinking"] == {"type": "disabled"}
    assert "thinking" not in wire.requests[1].json()
    assert wire.requests[1].json()["max_tokens"] == 4
    # Remembered too: that model is not sent the parameter again.
    await run(wire.model, max_output_tokens=4, think=False)
    assert len(wire.requests) == 3 and "thinking" not in wire.last.json()


async def test_a_type_that_is_refused_too_is_followed_by_one_call_without_the_parameter(wire_for):
    wire = wire_for("anthropic")
    wire.reply_in_turn(USE_BETWEEN_TOOLS, THINKING_UNKNOWN, OK_STREAM)
    assert text_of(await run(wire.model, think=False)) == "yes"
    assert [request.json().get("thinking") for request in wire.requests] == [
        {"type": "disabled"},
        {"type": "between_tools"},
        None,
    ]


async def test_a_third_refusal_is_an_ordinary_error(wire_for):
    wire = wire_for("anthropic")
    wire.reply_in_turn(USE_BETWEEN_TOOLS, THINKING_UNKNOWN, THINKING_UNKNOWN)
    with pytest.raises(ModelCallError) as excinfo:
        await run(wire.model, think=False)
    assert excinfo.value.kind == "bad_request"
    assert len(wire.requests) == 3
    # Nothing was learnt from a call that never worked: the next one starts over.
    wire.reply_text(["yes"])
    await run(wire.model, think=False)
    assert wire.last.json()["thinking"] == {"type": "disabled"}


async def test_the_provider_is_never_followed_into_thinking(wire_for):
    # The type to send instead comes from the provider's text: one that thinks, or that needs
    # more than its name, is not sent.
    wire = wire_for("anthropic")
    wire.reply_in_turn(
        _refusal('thinking: send {"type": "adaptive"} or {"type": "enabled"} instead'), OK_STREAM
    )
    await run(wire.model, think=False)
    assert [request.json().get("thinking") for request in wire.requests] == [
        {"type": "disabled"},
        None,
    ]


@pytest.mark.parametrize(
    "answer",
    [
        _refusal("messages: at least one message is required"),
        (400, b"<html>Bad request</html>"),
        (400, b'{"error": "x"}'),
    ],
)
async def test_a_refusal_that_is_not_about_thinking_is_not_asked_again(wire_for, answer):
    wire = wire_for("anthropic")
    wire.reply_in_turn(answer)
    with pytest.raises(ModelCallError) as excinfo:
        await run(wire.model, think=False)
    assert excinfo.value.kind == "bad_request"
    assert len(wire.requests) == 1


@pytest.mark.parametrize("status", [401, 404, 422, 429, 500, 503])
async def test_only_a_400_is_read_for_what_to_send_instead(wire_for, status):
    wire = wire_for("anthropic")
    wire.reply_in_turn((status, USE_BETWEEN_TOOLS[1]))
    with pytest.raises(ModelCallError):
        await run(wire.model, think=False)
    assert len(wire.requests) == 1


async def test_a_refusal_of_a_call_that_may_think_is_not_asked_again(wire_for):
    wire = wire_for("anthropic")
    wire.reply_in_turn(USE_BETWEEN_TOOLS)
    with pytest.raises(ModelCallError):
        await run(wire.model)
    assert len(wire.requests) == 1


@pytest.mark.parametrize("think", [True, False])
async def test_anthropic_thinking_never_comes_out_as_text(wire_for, think):
    # Whatever was asked: a model that thinks anyway, or whose short updates come back as
    # thinking blocks, gives the caller its answer and nothing else.
    wire = wire_for("anthropic")
    wire.reply_text(["yes"], thinking=["They said ok, ", "so it is a yes."])
    assert await run(wire.model, think=think) == [ChatChunk("yes"), ChatUsage(3, 2)]


async def test_an_answer_that_was_all_thinking_gives_no_text_and_says_it_was_cut(wire_for):
    wire = wire_for("anthropic")
    wire.reply_text([], thinking=["They said"], stop_reason="max_tokens")
    assert await run(wire.model, max_output_tokens=4, think=False) == [
        ChatUsage(3, 2, cut_short=True)
    ]


async def test_a_refusal_about_thinking_never_shows_the_providers_text(wire_for, caplog):
    wire = wire_for("anthropic")
    wire.reply_in_turn(USE_BETWEEN_TOOLS, THINKING_UNKNOWN, THINKING_UNKNOWN)
    with caplog.at_level(logging.DEBUG), pytest.raises(ModelCallError) as excinfo:
        await run(wire.model, think=False)
    assert str(excinfo.value) == "bad_request"
    assert "Extra inputs" not in caplog.text and TOKEN_FRAGMENT not in caplog.text


@pytest.mark.parametrize("kind", ["openai", "openai_compatible", "google"])
async def test_the_other_providers_are_sent_the_same_request_either_way(wire_for, kind):
    # No parameter of theirs is known to be safe on every model: the flag changes nothing there.
    wire = wire_for(kind)
    wire.reply_text(["ok"])
    assert text_of(await run(wire.model, think=False)) == "ok"
    asked_not_to_think = wire.last.json()
    await run(wire.model)
    assert wire.last.json() == asked_not_to_think
