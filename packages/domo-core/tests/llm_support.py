"""Helpers shared by the tests of the gatekeeper client and the model adapters.

They live here, and not in ``conftest.py``, for the reason given in ``web_support.py``.

The OpenAI and Anthropic SDKs talk through ``httpx2``, which ``respx`` cannot intercept, so their
requests go to a ``ScriptedTransport`` that plays the gatekeeper's relay. The Google SDK uses
classic ``httpx``, which ``respx`` does intercept. ``Wire`` hides the difference: a test says what
the relay answers (``reply``, ``fail``...) and looks at what the SDK sent (``requests``).
"""

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx
import httpx2
import respx
from domo_core.gatekeeper_client import GatekeeperClient
from domo_core.llm.factory import build_chat_model
from domo_core.llm.models import Provider
from domo_core.llm.provider_kinds import PROVIDER_KINDS, ProviderKind
from domo_core.llm.types import (
    ChatChunk,
    ChatMessage,
    ChatModel,
    ChatUsage,
    HistoryItem,
    ToolCall,
    ToolChoice,
    ToolDefinition,
)

# The core's token, and where the (fake) gatekeeper is.
CORE_TOKEN = "core-token"
GATEKEEPER_URL = "http://gatekeeper.test"
# Distinctive header values: no error message or repr may ever contain them.
TOKEN_FRAGMENT = "core-tok"

SSE_HEADERS = {"content-type": "text/event-stream"}
JSON_HEADERS = {"content-type": "application/json"}

HELLO = (ChatMessage("user", "hi"),)


async def run(
    model: ChatModel,
    *,
    system: str = "S",
    messages: Sequence[HistoryItem] = HELLO,
    max_output_tokens: int = 32,
    tools: Sequence[ToolDefinition] = (),
    tool_choice: ToolChoice = "auto",
    think: bool = True,
) -> list[ChatChunk | ToolCall | ChatUsage]:
    """Everything ``model`` streams for one call (the stream is closed at the end)."""
    async with aclosing(
        model.stream(
            system=system,
            messages=messages,
            max_output_tokens=max_output_tokens,
            tools=tools,
            tool_choice=tool_choice,
            think=think,
        )
    ) as events:
        return [event async for event in events]


def text_of(events: Sequence[ChatChunk | ToolCall | ChatUsage]) -> str:
    """The text of the chunks among ``events``."""
    return "".join(event.text for event in events if isinstance(event, ChatChunk))


def tool_calls_of(events: Sequence[ChatChunk | ToolCall | ChatUsage]) -> list[ToolCall]:
    """The tool calls among ``events``, in order."""
    return [event for event in events if isinstance(event, ToolCall)]


def provider(kind: ProviderKind, id: str = "p1") -> Provider:
    """A provider row (not saved anywhere) of ``kind``, as the factory receives it."""
    spec = PROVIDER_KINDS[kind]
    return Provider(
        id=id,
        kind=kind,
        base_url=spec.default_base_url or "http://192.168.1.10:11434/v1",
        secret_ref=f"provider:{id}:api_key",
    )


# --- what the providers stream ---------------------------------------------------------------


@dataclass(frozen=True)
class ScriptedToolCall:
    """A tool call the fake provider streams.

    ``arguments`` are the pieces the arguments arrive in (OpenAI and Anthropic stream them; Google
    sends them whole, so its builder joins them: they must make a JSON object). ``id`` is the
    provider's identifier of the call, or ``""`` for a default one (Google sends none).
    ``thought_signature`` is the base64 text Gemini 3 puts on a function call (Google only).
    """

    name: str
    arguments: Sequence[str] = ("{}",)
    id: str = ""
    thought_signature: str | None = None

    def id_at(self, index: int) -> str:
        return self.id or f"call_{index + 1}"


def _sse(events: Sequence[tuple[str | None, object]]) -> bytes:
    """Server-sent events: an optional event name and a JSON payload (or a raw string)."""
    lines: list[str] = []
    for name, payload in events:
        if name is not None:
            lines.append(f"event: {name}")
        data = payload if isinstance(payload, str) else json.dumps(payload)
        lines.append(f"data: {data}")
        lines.append("")
    return ("\n".join(lines) + "\n").encode()


def openai_sse(
    texts: Sequence[str],
    *,
    usage: tuple[int, int] | None = (3, 2),
    usage_in_every_chunk: bool = False,
    tool_calls: Sequence[ScriptedToolCall] = (),
    finish_reason: str | None = None,
    cached_tokens: int | None = None,
) -> bytes:
    """A chat completion stream: the role, one chunk per text, the tool calls (a chunk with the
    id and the name, then one per piece of arguments), the finish, the usage, [DONE].

    ``usage_in_every_chunk`` imitates servers that repeat the counts in each chunk instead of
    sending them once, at the end. ``finish_reason`` replaces the usual one ("tool_calls" after
    calls, "stop" otherwise), e.g. "length" for an answer cut by the token limit.
    """

    def chunk(delta: dict[str, Any], finish: str | None = None, *, with_usage: bool = False):
        body: dict[str, Any] = {
            "id": "c1",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "test-model",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        if with_usage and usage is not None:
            body["usage"] = {
                "prompt_tokens": usage[0],
                "completion_tokens": usage[1],
                "total_tokens": usage[0] + usage[1],
            }
        return body

    events: list[tuple[str | None, object]] = [
        (None, chunk({"role": "assistant", "content": ""}, with_usage=usage_in_every_chunk))
    ]
    events += [(None, chunk({"content": text}, with_usage=usage_in_every_chunk)) for text in texts]
    for index, call in enumerate(tool_calls):
        opening = {
            "index": index,
            "id": call.id_at(index),
            "type": "function",
            "function": {"name": call.name, "arguments": ""},
        }
        events.append((None, chunk({"tool_calls": [opening]}, with_usage=usage_in_every_chunk)))
        for piece in call.arguments:
            delta = {"tool_calls": [{"index": index, "function": {"arguments": piece}}]}
            events.append((None, chunk(delta, with_usage=usage_in_every_chunk)))
    finish = finish_reason or ("tool_calls" if tool_calls else "stop")
    events.append((None, chunk({}, finish, with_usage=usage_in_every_chunk)))
    if usage is not None and not usage_in_every_chunk:
        events.append(
            (
                None,
                {
                    "id": "c1",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "test-model",
                    "choices": [],
                    "usage": {
                        "prompt_tokens": usage[0],
                        "completion_tokens": usage[1],
                        "total_tokens": usage[0] + usage[1],
                        **(
                            {"prompt_tokens_details": {"cached_tokens": cached_tokens}}
                            if cached_tokens is not None
                            else {}
                        ),
                    },
                },
            )
        )
    events.append((None, "[DONE]"))
    return _sse(events)


def anthropic_sse(
    texts: Sequence[str],
    *,
    usage: tuple[int, int] | None = (3, 2),
    extra_events: bool = False,
    tool_calls: Sequence[ScriptedToolCall] = (),
    stop_reason: str | None = None,
    thinking: Sequence[str] = (),
    cache: tuple[int, int] | None = None,
) -> bytes:
    """A messages stream: message_start (input tokens), one text delta per text, a tool_use block
    per tool call (its arguments in input_json_delta pieces), message_delta (output tokens),
    message_stop. ``extra_events`` adds a ping and a delta that is not text. A stream of tool
    calls only (no ``texts``) has no text block. ``stop_reason`` replaces the usual one
    ("tool_use" after calls, "end_turn" otherwise), e.g. "max_tokens". ``thinking`` are the pieces
    of a thinking block, sent first (with its signature), that are not part of the answer."""
    input_tokens, output_tokens = usage if usage is not None else (0, 0)
    with_text_block = bool(texts) or not tool_calls
    events: list[tuple[str | None, object]] = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "m1",
                    "type": "message",
                    "role": "assistant",
                    "model": "test-model",
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {
                        "input_tokens": input_tokens,
                        "output_tokens": 1,
                        **(
                            {
                                "cache_read_input_tokens": cache[0],
                                "cache_creation_input_tokens": cache[1],
                            }
                            if cache is not None
                            else {}
                        ),
                    },
                },
            },
        )
    ]
    # The block the text is in: the first one, or the one after the thinking.
    text_block = 1 if thinking else 0
    if thinking:
        events.append(
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "thinking", "thinking": "", "signature": ""},
                },
            )
        )
        events += [
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "thinking_delta", "thinking": piece},
                },
            )
            for piece in thinking
        ]
        events.append(
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "signature_delta", "signature": "c2lnbmVk"},
                },
            )
        )
        events.append(("content_block_stop", {"type": "content_block_stop", "index": 0}))
    if with_text_block:
        events.append(
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": text_block,
                    "content_block": {"type": "text", "text": ""},
                },
            )
        )
    if extra_events:
        events.append(("ping", {"type": "ping"}))
        events.append(
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": text_block,
                    "delta": {"type": "input_json_delta", "partial_json": '{"a":'},
                },
            )
        )
    for text in texts:
        events.append(
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": text_block,
                    "delta": {"type": "text_delta", "text": text},
                },
            )
        )
    first_tool_block = text_block + (1 if with_text_block else 0)
    if with_text_block:
        events.append(("content_block_stop", {"type": "content_block_stop", "index": text_block}))
    for offset, call in enumerate(tool_calls):
        block = first_tool_block + offset
        events.append(
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": block,
                    "content_block": {
                        "type": "tool_use",
                        "id": call.id_at(offset),
                        "name": call.name,
                        "input": {},
                    },
                },
            )
        )
        for piece in call.arguments:
            events.append(
                (
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": block,
                        "delta": {"type": "input_json_delta", "partial_json": piece},
                    },
                )
            )
        events.append(("content_block_stop", {"type": "content_block_stop", "index": block}))
    events.append(
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {
                    "stop_reason": stop_reason or ("tool_use" if tool_calls else "end_turn"),
                    "stop_sequence": None,
                },
                "usage": {"output_tokens": output_tokens},
            },
        )
    )
    events.append(("message_stop", {"type": "message_stop"}))
    return _sse(events)


def google_sse(
    texts: Sequence[str],
    *,
    usage: tuple[int, int] | None = (3, 2),
    thoughts: Sequence[str] = (),
    tool_calls: Sequence[ScriptedToolCall] = (),
    cached_tokens: int | None = None,
) -> bytes:
    """A streamGenerateContent stream: one chunk per text, then one chunk with a functionCall part
    per tool call (Google sends the arguments whole); the counts ride on the last one.
    ``thoughts`` are thinking summaries, sent first, that are not part of the answer."""

    def chunk(parts: list[dict[str, Any]], *, last: bool = False) -> dict[str, Any]:
        body: dict[str, Any] = {
            "candidates": [{"content": {"role": "model", "parts": parts}, "index": 0}],
            "modelVersion": "test-model",
        }
        if last:
            body["candidates"][0]["finishReason"] = "STOP"
            if usage is not None:
                body["usageMetadata"] = {
                    "promptTokenCount": usage[0],
                    "candidatesTokenCount": usage[1],
                    "totalTokenCount": usage[0] + usage[1],
                    **(
                        {"cachedContentTokenCount": cached_tokens}
                        if cached_tokens is not None
                        else {}
                    ),
                }
        return body

    events: list[tuple[str | None, object]] = [
        (None, chunk([{"text": thought, "thought": True}])) for thought in thoughts
    ]
    for index, text in enumerate(texts):
        events.append(
            (None, chunk([{"text": text}], last=not tool_calls and index == len(texts) - 1))
        )
    if tool_calls:
        parts: list[dict[str, Any]] = []
        for call in tool_calls:
            joined = "".join(call.arguments)
            function_call: dict[str, Any] = {"name": call.name}
            if joined:
                function_call["args"] = json.loads(joined)
            part: dict[str, Any] = {"functionCall": function_call}
            if call.thought_signature is not None:
                part["thoughtSignature"] = call.thought_signature
            parts.append(part)
        events.append((None, chunk(parts, last=True)))
    return _sse(events)


SSE_BUILDERS: Mapping[ProviderKind, Callable[..., bytes]] = {
    "openai": openai_sse,
    "openai_compatible": openai_sse,
    "anthropic": anthropic_sse,
    "google": google_sse,
}

# The path the relay receives for each kind (the provider is "p1").
RELAY_PATHS: Mapping[ProviderKind, str] = {
    "openai": "/llm/p1/chat/completions",
    "openai_compatible": "/llm/p1/chat/completions",
    "anthropic": "/llm/p1/v1/messages",
    "google": "/llm/p1/v1beta/models/test-model:streamGenerateContent",
}

# The headers the gatekeeper takes the core's token from on the relay routes.
TOKEN_HEADERS = ("authorization", "x-api-key", "x-goog-api-key")


# --- a relay that can be scripted ------------------------------------------------------------


@dataclass(frozen=True)
class SeenRequest:
    """What an SDK sent to the (fake) gatekeeper."""

    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes
    timeouts: Mapping[str, float | None]

    @property
    def path(self) -> str:
        return self.url.split("?", 1)[0].removeprefix(GATEKEEPER_URL)

    def json(self) -> Any:
        return json.loads(self.body)


class StreamProbe:
    """A response body of chunks that records how much was read and whether it was closed.

    After its last chunk the body fails with ``then_raise`` (the connection broke mid-stream), or
    waits for ever with ``stall`` (the provider went quiet).
    """

    def __init__(
        self,
        chunks: Sequence[bytes],
        *,
        then_raise: Exception | None = None,
        stall: bool = False,
    ) -> None:
        self.chunks = list(chunks)
        self._then_raise = then_raise
        self._stall = stall
        self.sent = 0
        self.closed = False

    async def iterate(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.sent += 1
            yield chunk
        if self._then_raise is not None:
            raise self._then_raise
        if self._stall:
            await asyncio.Event().wait()


class _Httpx2Body(httpx2.AsyncByteStream):
    def __init__(self, probe: StreamProbe) -> None:
        self._probe = probe

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._probe.iterate():
            yield chunk

    async def aclose(self) -> None:
        self._probe.closed = True


class _HttpxBody(httpx.AsyncByteStream):
    def __init__(self, probe: StreamProbe) -> None:
        self._probe = probe

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._probe.iterate():
            yield chunk

    async def aclose(self) -> None:
        self._probe.closed = True


FailureKind = Literal["connect", "timeout"]


class Wire:
    """The fake gatekeeper relay seen from a test, whatever HTTP library the SDK uses.

    A test scripts the answer (``reply``, ``reply_text``, ``fail``...), runs ``model`` and looks
    at what the SDK sent (``requests``, ``last``).
    """

    def __init__(self, kind: ProviderKind) -> None:
        self.kind = kind
        self.requests: list[SeenRequest] = []
        self.model: ChatModel
        # The scripted transport of the SDKs on httpx2 (none for Google, which respx intercepts):
        # what ``list_models`` is given to talk to this relay.
        self.transport: httpx2.AsyncBaseTransport | None

    # -- to implement for each HTTP library --
    def reply(
        self, status: int = 200, *, headers: Mapping[str, str] | None = None, body: bytes = b""
    ) -> None:
        raise NotImplementedError

    def reply_probe(self, probe: StreamProbe) -> None:
        raise NotImplementedError

    def reply_each(self, bodies: Sequence[bytes]) -> None:
        """Answer the first request with ``bodies[0]`` (JSON, status 200), the second with
        ``bodies[1]``, and so on (the last one again for the requests after them): the pages of a
        list."""
        raise NotImplementedError

    def reply_json(self, payload: object) -> None:
        """Answer every request with ``payload`` as JSON (status 200)."""
        self.reply(200, headers=JSON_HEADERS, body=json.dumps(payload).encode())

    def fail(self, how: FailureKind) -> None:
        raise NotImplementedError

    def read_error(self) -> Exception:
        """What the HTTP library raises when the connection drops in the middle of a body."""
        raise NotImplementedError

    # -- the same for all --
    def _events(self, texts: Sequence[str], **options: Any) -> list[bytes]:
        body = SSE_BUILDERS[self.kind](texts, **options)
        return [event + b"\n\n" for event in body.split(b"\n\n") if event]

    def reply_text(self, texts: Sequence[str], **options: Any) -> None:
        self.reply(200, headers=SSE_HEADERS, body=SSE_BUILDERS[self.kind](texts, **options))

    def reply_tool_calls(
        self, *calls: ScriptedToolCall, texts: Sequence[str] = (), **options: Any
    ) -> None:
        """Answer with ``texts`` (if any), then the given tool calls."""
        self.reply_text(texts, tool_calls=calls, **options)

    def reply_broken_in_tool_call(self, call: ScriptedToolCall) -> StreamProbe:
        """Answer with a stream that goes as far as the first piece of the call's arguments, then
        drops the connection (for the providers that stream the arguments in pieces)."""
        first_piece = json.dumps(next(piece for piece in call.arguments if piece)).encode()
        events = self._events([], tool_calls=[call])
        cut = next(i for i, event in enumerate(events) if first_piece in event)
        probe = StreamProbe(events[: cut + 1], then_raise=self.read_error())
        self.reply_probe(probe)
        return probe

    def reply_tracked(self, texts: Sequence[str], **options: Any) -> StreamProbe:
        """Answer with the stream of ``texts``, one event per chunk, and tell whether it was
        read and closed."""
        probe = StreamProbe(self._events(texts, **options))
        self.reply_probe(probe)
        return probe

    def _up_to_first_text(self, texts: Sequence[str]) -> list[bytes]:
        events = self._events(texts)
        first_text = next(i for i, event in enumerate(events) if texts[0].encode() in event)
        return events[: first_text + 1]

    def reply_broken(self, texts: Sequence[str]) -> StreamProbe:
        """Answer with a stream that goes as far as the first text, then drops the connection."""
        probe = StreamProbe(self._up_to_first_text(texts), then_raise=self.read_error())
        self.reply_probe(probe)
        return probe

    def reply_stalled(self, texts: Sequence[str]) -> StreamProbe:
        """Answer with a stream that goes as far as the first text, then says nothing more."""
        probe = StreamProbe(self._up_to_first_text(texts), stall=True)
        self.reply_probe(probe)
        return probe

    @property
    def last(self) -> SeenRequest:
        return self.requests[-1]


class Httpx2Wire(Wire):
    """For the SDKs on ``httpx2`` (OpenAI, Anthropic): a scripted transport."""

    def __init__(self, kind: ProviderKind, gatekeeper: GatekeeperClient) -> None:
        super().__init__(kind)
        self._script: Callable[[], httpx2.Response] = lambda: httpx2.Response(500)
        self.transport = self._transport()
        self.model = build_chat_model(
            provider(kind), "test-model", gatekeeper, transport=self.transport
        )

    def _transport(self) -> httpx2.AsyncBaseTransport:
        wire = self

        class Transport(httpx2.AsyncBaseTransport):
            async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
                await request.aread()
                wire.requests.append(
                    SeenRequest(
                        request.method,
                        str(request.url),
                        dict(request.headers),
                        request.content,
                        dict(request.extensions["timeout"]),
                    )
                )
                return wire._script()

        return Transport()

    def reply(
        self, status: int = 200, *, headers: Mapping[str, str] | None = None, body: bytes = b""
    ) -> None:
        self._script = lambda: httpx2.Response(status, headers=dict(headers or {}), content=body)

    def reply_probe(self, probe: StreamProbe) -> None:
        self._script = lambda: httpx2.Response(200, headers=SSE_HEADERS, stream=_Httpx2Body(probe))

    def reply_in_turn(self, *answers: tuple[int, bytes]) -> None:
        """Answer the first request with ``answers[0]`` (a status and a body: JSON for an error,
        a stream of events for 200), the second with ``answers[1]``, and so on (the last one
        again for the requests after them)."""

        def script() -> httpx2.Response:
            status, body = answers[min(len(self.requests), len(answers)) - 1]
            headers = SSE_HEADERS if status == 200 else JSON_HEADERS
            return httpx2.Response(status, headers=dict(headers), content=body)

        self._script = script

    def reply_each(self, bodies: Sequence[bytes]) -> None:
        def script() -> httpx2.Response:
            body = bodies[min(len(self.requests), len(bodies)) - 1]
            return httpx2.Response(200, headers=JSON_HEADERS, content=body)

        self._script = script

    def fail(self, how: FailureKind) -> None:
        def script() -> httpx2.Response:
            if how == "connect":
                raise httpx2.ConnectError("refused")
            raise httpx2.ReadTimeout("too slow")

        self._script = script

    def read_error(self) -> Exception:
        return httpx2.ReadError("cut")


class HttpxWire(Wire):
    """For the SDK on classic ``httpx`` (Google): ``respx`` intercepts it."""

    def __init__(
        self, kind: ProviderKind, gatekeeper: GatekeeperClient, respx_mock: respx.MockRouter
    ) -> None:
        super().__init__(kind)
        self._script: Callable[[], httpx.Response] = lambda: httpx.Response(500)
        self.transport = None
        self.model = build_chat_model(provider(kind), "test-model", gatekeeper)
        respx_mock.route(host="gatekeeper.test").mock(side_effect=self._answer)

    def _answer(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(
            SeenRequest(
                request.method,
                str(request.url),
                dict(request.headers),
                request.content,
                dict(request.extensions["timeout"]),
            )
        )
        return self._script()

    def reply(
        self, status: int = 200, *, headers: Mapping[str, str] | None = None, body: bytes = b""
    ) -> None:
        self._script = lambda: httpx.Response(status, headers=dict(headers or {}), content=body)

    def reply_probe(self, probe: StreamProbe) -> None:
        self._script = lambda: httpx.Response(200, headers=SSE_HEADERS, stream=_HttpxBody(probe))

    def reply_each(self, bodies: Sequence[bytes]) -> None:
        def script() -> httpx.Response:
            body = bodies[min(len(self.requests), len(bodies)) - 1]
            return httpx.Response(200, headers=JSON_HEADERS, content=body)

        self._script = script

    def fail(self, how: FailureKind) -> None:
        def script() -> httpx.Response:
            if how == "connect":
                raise httpx.ConnectError("refused")
            raise httpx.ReadTimeout("too slow")

        self._script = script

    def read_error(self) -> Exception:
        return httpx.ReadError("cut")


class WireFactory(Protocol):
    def __call__(self, kind: ProviderKind) -> Wire: ...
