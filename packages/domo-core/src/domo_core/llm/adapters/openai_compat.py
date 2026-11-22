"""The adapter for OpenAI and for the servers that speak its chat completions API.

The SDK is pointed at the gatekeeper's relay and given the core's token as its API key; the
gatekeeper replaces it with the real key (or with none, for a local server) on the way out.

``think=False`` (a short answer, asked without thinking first) changes nothing here yet: what
turns the reasoning down (``reasoning_effort``) takes values that differ from one model to the
next, and a server that does not know it may refuse the request. A model of these that reasons
first spends its token limit on it, and a short call to it can come back cut short.
"""

from collections.abc import AsyncGenerator, Sequence
from dataclasses import replace
from typing import Literal

import httpx2
from openai import APIConnectionError, APIStatusError, AsyncOpenAI, omit
from openai.types.chat import (
    ChatCompletionMessageFunctionToolCallParam,
    ChatCompletionMessageParam,
    ChatCompletionToolParam,
)

from domo_core.llm.adapters._support import (
    PendingCall,
    kind_for_status,
    log_failure,
    new_sdk_http_client,
    object_arguments_text,
    sdk_timeout,
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
)

# OpenAI's own models want ``max_completion_tokens`` (the reasoning ones refuse ``max_tokens``);
# the servers that imitate the API know ``max_tokens``.
TokenLimitParameter = Literal["max_tokens", "max_completion_tokens"]


class _EmptyStream(Exception):
    """The server answered 200 and sent no event at all: it is not a model (a web page, say)."""


def _classify(error: Exception) -> ErrorKind:
    if isinstance(error, APIStatusError):
        return kind_for_status(error.status_code)
    # Also the SDK's timeout, and the connection that breaks while the answer is being read.
    if isinstance(error, APIConnectionError | httpx2.TransportError):
        return "unreachable"
    return "upstream"


def _count(value: object) -> int:
    return value if isinstance(value, int) and value >= 0 else 0


def _request_messages(
    system: str, messages: Sequence[HistoryItem]
) -> list[ChatCompletionMessageParam]:
    request: list[ChatCompletionMessageParam] = []
    if system:
        request.append({"role": "system", "content": system})
    for message in messages:
        if isinstance(message, ChatMessage):
            if message.role == "user":
                request.append({"role": "user", "content": message.content})
            else:
                request.append({"role": "assistant", "content": message.content})
        elif isinstance(message, AssistantToolCalls):
            calls: list[ChatCompletionMessageFunctionToolCallParam] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": object_arguments_text(call.arguments),
                    },
                }
                for call in message.calls
            ]
            request.append(
                {"role": "assistant", "content": message.text or None, "tool_calls": calls}
            )
        else:
            request.append(
                {"role": "tool", "tool_call_id": message.call_id, "content": message.content}
            )
    return request


def _request_tools(tools: Sequence[ToolDefinition]) -> list[ChatCompletionToolParam]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        }
        for tool in tools
    ]


class OpenAICompatChat:
    """A ``ChatModel`` on the chat completions API."""

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        model: str,
        token_limit: TokenLimitParameter,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url
        self._token = token
        self._model = model
        self._token_limit: TokenLimitParameter = token_limit
        self._transport = transport

    def __repr__(self) -> str:
        return f"OpenAICompatChat(base_url={self._base_url!r}, model={self._model!r})"

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
        request = _request_messages(system, messages)

        failure: ErrorKind | None = None
        usage: ChatUsage | None = None
        # The calls of the answer, in the order they began, and the one each index stands for.
        calls: list[PendingCall] = []
        call_at: dict[int, PendingCall] = {}
        # Whether the answer stopped at the token limit.
        cut_by_the_limit = False
        try:
            # One SDK client (and one connection pool) per call, closed with the call.
            async with AsyncOpenAI(
                api_key=self._token,
                base_url=self._base_url,
                max_retries=0,
                timeout=sdk_timeout(),
                http_client=new_sdk_http_client(self._transport),
            ) as client:
                completion = await client.chat.completions.create(
                    model=self._model,
                    messages=request,
                    stream=True,
                    # Asks for the token counts; a server that ignores the option sends none.
                    stream_options={"include_usage": True},
                    max_tokens=max_output_tokens if self._token_limit == "max_tokens" else omit,
                    max_completion_tokens=(
                        max_output_tokens if self._token_limit == "max_completion_tokens" else omit
                    ),
                    tools=_request_tools(tools) if tools else omit,
                    tool_choice="none" if tools and tool_choice == "none" else omit,
                )
                async with completion:
                    seen_an_event = False
                    async for chunk in completion:
                        seen_an_event = True
                        # Some servers repeat the counts in every chunk: the last ones count.
                        if chunk.usage is not None:
                            usage = ChatUsage(
                                _count(chunk.usage.prompt_tokens),
                                _count(chunk.usage.completion_tokens),
                                cache_read_tokens=_count(
                                    chunk.usage.prompt_tokens_details.cached_tokens
                                    if chunk.usage.prompt_tokens_details is not None
                                    else 0
                                ),
                            )
                        if chunk.choices:
                            if chunk.choices[0].finish_reason == "length":
                                cut_by_the_limit = True
                            delta = chunk.choices[0].delta
                            if delta.content:
                                yield ChatChunk(delta.content)
                            for piece in delta.tool_calls or []:
                                call = call_at.get(piece.index)
                                if call is None or (call.id and piece.id and call.id != piece.id):
                                    # The first piece of a call (or, for a server that gives
                                    # every call the same index, of another one).
                                    call = PendingCall()
                                    calls.append(call)
                                    call_at[piece.index] = call
                                call.id = call.id or piece.id or ""
                                if piece.function is not None:
                                    call.name = call.name or piece.function.name or ""
                                    if piece.function.arguments:
                                        call.arguments.append(piece.function.arguments)
                    if not seen_an_event:
                        raise _EmptyStream
        except Exception as error:
            failure = _classify(error)
            log_failure(failure, error)
        if failure is not None:
            # Raised outside the handler: the SDK's exception holds the request, and so the token.
            raise ModelCallError(failure)
        # Streamed once the response is released, and only the calls that are complete: a stream
        # that failed has none, and neither has an answer that ran out of tokens (the call being
        # written then is half a call, or looks like one with no arguments).
        for pending in [] if cut_by_the_limit else calls:
            if (tool_call := pending.complete()) is not None:
                yield tool_call
        if usage is not None:
            yield replace(usage, cut_short=cut_by_the_limit)
        elif cut_by_the_limit:
            yield ChatUsage(0, 0, cut_short=True)
