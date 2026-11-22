"""The adapter for Anthropic's messages API.

The SDK is pointed at the gatekeeper's relay and given the core's token as its API key (the SDK
sends it in ``x-api-key``, or as ``Authorization: Bearer`` from its version 2 on; the relay
accepts both); the gatekeeper drops it and puts the real key, in ``x-api-key``, on the way out.

**A short answer, asked without thinking first** (``think=False``). The current models think
before they answer unless told not to, and the thinking counts in ``max_tokens``: asked for one
word in four tokens, they sent a thinking block cut short and no text (measured 2026-11-15). So
such a call says ``thinking: {"type": "disabled"}``. Not every model takes that:

- a model whose thinking cannot be disabled refuses it (400) and names the type to send instead
  (``"between_tools"`` today: it does not think before it answers, and what it writes between two
  tool calls comes back as thinking blocks). The call is made once more with the type the
  provider named (``_named_instead``: never one that turns thinking on);
- a model that knows no such parameter refuses it (400, about ``thinking``, naming nothing to
  send): the call is made once more without it.

So three requests at most; a refusal after that, a refusal that is not about ``thinking`` and any
other error are the call's error, as for a call that may think. What worked is remembered for the
model, for the life of the process (``_what_models_want``: a type's name or nothing, no secret),
so that the next short call does not pay for the refusal again.

Whatever was asked, thinking never comes out as text: only ``text_delta`` pieces are streamed,
and a thinking block (its ``thinking_delta`` and ``signature_delta``) is skipped. A model that
thinks anyway is still bounded by the caller's ``max_output_tokens``, and says so (``cut_short``).
"""

import re
from collections.abc import AsyncGenerator, Sequence
from typing import Final, cast

import httpx2
from anthropic import APIConnectionError, APIStatusError, AsyncAnthropic, omit
from anthropic.types import (
    ContentBlockParam,
    MessageParam,
    TextBlockParam,
    ThinkingConfigParam,
    ToolParam,
    ToolResultBlockParam,
)

from domo_core.llm.adapters._support import (
    PendingCall,
    arguments_as_object,
    kind_for_status,
    log_failure,
    logger,
    new_sdk_http_client,
    sdk_timeout,
)
from domo_core.llm.types import (
    AssistantToolCalls,
    ChatChunk,
    ChatUsage,
    ErrorKind,
    HistoryItem,
    ModelCallError,
    ToolCall,
    ToolChoice,
    ToolDefinition,
    ToolResult,
)

# The stop reasons of an answer that ran out of room: what it was writing is cut.
_LIMIT_STOP_REASONS = ("max_tokens", "model_context_window_exceeded")

# What a model is sent first when it is asked not to think.
_DISABLED: Final = "disabled"
# The types that turn thinking on (or need more than their name): never sent for a call that
# asked not to think, even when the provider's text names them.
_THINKING_TYPES: Final = frozenset({"enabled", "adaptive"})
# A type of ``thinking`` as the provider's refusal writes it: ``{"type": "between_tools"}``.
_NAMED_TYPE: Final = re.compile(r'\{\s*"type"\s*:\s*"([a-z_]{1,40})"\s*\}')
# What each model was found to want when it is asked not to think, by model id: the type of
# ``thinking`` it takes, or ``None`` for a model that is sent no such parameter. A model that is
# not here is sent ``_DISABLED``. Kept for the life of the process, and learnt again after it.
_what_models_want: dict[str, str | None] = {}


def forget_what_models_want() -> None:
    """Forget what the models were found to want when asked not to think (for the tests)."""
    _what_models_want.clear()


def _refusal_about_thinking(error: APIStatusError) -> str | None:
    """The provider's words when it refused the request (400) because of ``thinking``, else
    ``None``. They are read here and nowhere else: never logged, never given to the caller."""
    if error.status_code != 400:
        return None
    body: object = error.body
    if not isinstance(body, dict):
        return None
    inner: object = cast(dict[str, object], body).get("error")
    if not isinstance(inner, dict):
        return None
    message: object = cast(dict[str, object], inner).get("message")
    if not isinstance(message, str) or "thinking" not in message.casefold():
        return None
    return message


def _named_instead(message: str, sent: Sequence[str | None]) -> str | None:
    """The type of ``thinking`` that ``message`` (a refusal) says to send instead: the first one
    it names that was not ``sent`` yet and does not turn thinking on, or ``None``."""
    names: list[str] = _NAMED_TYPE.findall(message)
    for name in names:
        if name not in sent and name not in _THINKING_TYPES:
            return name
    return None


class _EmptyStream(Exception):
    """The server answered 200 and sent no event at all: it is not the API (a web page, say)."""


def _classify(error: Exception) -> ErrorKind:
    if isinstance(error, APIStatusError):
        return kind_for_status(error.status_code)
    # The SDK does not wrap the errors of a body that breaks while it is read.
    if isinstance(error, APIConnectionError | httpx2.TransportError):
        return "unreachable"
    return "upstream"


def _request_messages(messages: Sequence[HistoryItem]) -> list[MessageParam]:
    request: list[MessageParam] = []
    # The results of the calls of one answer, which the API wants together, in one user message.
    results: list[ToolResultBlockParam] | None = None
    for message in messages:
        if isinstance(message, ToolResult):
            block: ToolResultBlockParam = {
                "type": "tool_result",
                "tool_use_id": message.call_id,
                "content": message.content,
            }
            if results is None:
                results = [block]
                request.append({"role": "user", "content": results})
            else:
                results.append(block)
            continue
        results = None
        if isinstance(message, AssistantToolCalls):
            content: list[ContentBlockParam] = []
            if message.text:
                content.append({"type": "text", "text": message.text})
            content.extend(
                {
                    "type": "tool_use",
                    "id": call.id,
                    "name": call.name,
                    "input": arguments_as_object(call.arguments),
                }
                for call in message.calls
            )
            request.append({"role": "assistant", "content": content})
        elif message.role == "user":
            request.append({"role": "user", "content": message.content})
        else:
            request.append({"role": "assistant", "content": message.content})
    return request


def _request_system(system: str) -> list[TextBlockParam]:
    # The fixed text is marked for the provider's cache: what comes before the mark is reused.
    return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]


def _request_tools(tools: Sequence[ToolDefinition]) -> list[ToolParam]:
    request: list[ToolParam] = [
        {"name": tool.name, "description": tool.description, "input_schema": tool.parameters}
        for tool in tools
    ]
    # The mark on the last definition covers all of them.
    request[-1]["cache_control"] = {"type": "ephemeral"}
    return request


class AnthropicChat:
    """A ``ChatModel`` on the messages API."""

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        model: str,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url
        self._token = token
        self._model = model
        self._transport = transport

    def __repr__(self) -> str:
        return f"AnthropicChat(base_url={self._base_url!r}, model={self._model!r})"

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
        request = _request_messages(messages)

        failure: ErrorKind | None = None
        input_tokens: int | None = None
        output_tokens: int | None = None
        cache_read = 0
        cache_write = 0
        # The tool_use blocks being received, by position in the answer, and the finished ones.
        receiving: dict[int, PendingCall] = {}
        finished: list[PendingCall] = []
        # Whether the answer stopped at the token limit.
        cut_by_the_limit = False
        try:
            # One SDK client (and one connection pool) per call, closed with the call.
            async with AsyncAnthropic(
                api_key=self._token,
                base_url=self._base_url,
                max_retries=0,
                timeout=sdk_timeout(),
                http_client=new_sdk_http_client(self._transport),
            ) as client:
                # What ``thinking`` says: nothing for a call that may think; else what the model
                # is known to want, or ``disabled`` (``None``: no parameter either).
                mode = None if think else _what_models_want.get(self._model, _DISABLED)
                sent: list[str | None] = []
                followed_a_name = False
                while True:
                    try:
                        events = await client.messages.create(
                            model=self._model,
                            max_tokens=max_output_tokens,
                            system=_request_system(system) if system else omit,
                            messages=request,
                            tools=_request_tools(tools) if tools else omit,
                            tool_choice=(
                                {"type": "none"} if tools and tool_choice == "none" else omit
                            ),
                            thinking=(
                                omit if mode is None else cast(ThinkingConfigParam, {"type": mode})
                            ),
                            stream=True,
                        )
                        break
                    except APIStatusError as refused:
                        # A model that does not take what it was sent about thinking is asked
                        # again: with the type the provider names instead, once, then without
                        # the parameter, once. Anything else is the call's error.
                        if think or (told := _refusal_about_thinking(refused)) is None:
                            raise
                        sent.append(mode)
                        named = None if followed_a_name else _named_instead(told, sent)
                        if named is not None:
                            followed_a_name = True
                            mode = named
                        elif None not in sent:
                            mode = None
                        else:
                            raise
                if sent:
                    # The request went through after a refusal: that is what this model wants.
                    _what_models_want[self._model] = mode
                    logger.info(
                        "The model %s is asked not to think with %s",
                        self._model,
                        "no parameter" if mode is None else f"the thinking type {mode}",
                    )
                async with events:
                    seen_an_event = False
                    async for event in events:
                        seen_an_event = True
                        if event.type == "message_start":
                            input_tokens = event.message.usage.input_tokens
                            output_tokens = event.message.usage.output_tokens
                            cache_read = event.message.usage.cache_read_input_tokens or 0
                            cache_write = event.message.usage.cache_creation_input_tokens or 0
                        elif event.type == "message_delta":
                            # Cumulative counts: the last ones are the total.
                            output_tokens = event.usage.output_tokens
                            if event.usage.input_tokens is not None:
                                input_tokens = event.usage.input_tokens
                            if event.usage.cache_read_input_tokens is not None:
                                cache_read = event.usage.cache_read_input_tokens
                            if event.usage.cache_creation_input_tokens is not None:
                                cache_write = event.usage.cache_creation_input_tokens
                            if event.delta.stop_reason in _LIMIT_STOP_REASONS:
                                cut_by_the_limit = True
                        elif event.type == "content_block_start":
                            if event.content_block.type == "tool_use":
                                receiving[event.index] = PendingCall(
                                    event.content_block.id, event.content_block.name
                                )
                        elif event.type == "content_block_delta":
                            if event.delta.type == "text_delta" and event.delta.text:
                                yield ChatChunk(event.delta.text)
                            elif event.delta.type == "input_json_delta":
                                # The arguments of a tool_use block, in pieces (the delta of any
                                # other kind of block is of no interest).
                                call = receiving.get(event.index)
                                if call is not None:
                                    call.arguments.append(event.delta.partial_json)
                        elif event.type == "content_block_stop":
                            if (done := receiving.pop(event.index, None)) is not None:
                                finished.append(done)
                    if not seen_an_event:
                        raise _EmptyStream
        except Exception as error:
            failure = _classify(error)
            log_failure(failure, error)
        if failure is not None:
            # Raised outside the handler: the SDK's exception holds the request, and so the token.
            raise ModelCallError(failure)
        # Streamed once the response is released, and only the blocks that were completed, of an
        # answer that did not run out of tokens (a block closed by the limit is cut, and one cut
        # before its arguments looks like a call with none).
        for pending in [] if cut_by_the_limit else finished:
            if (tool_call := pending.complete()) is not None:
                yield tool_call
        if input_tokens is not None or output_tokens is not None:
            # Anthropic's input count leaves the cache out: the whole input is the three together.
            yield ChatUsage(
                (input_tokens or 0) + cache_read + cache_write,
                output_tokens or 0,
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
                cut_short=cut_by_the_limit,
            )
        elif cut_by_the_limit:
            yield ChatUsage(0, 0, cut_short=True)
