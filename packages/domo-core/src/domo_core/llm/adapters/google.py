"""The adapter for Google's Gemini API.

The SDK is pointed at the gatekeeper's relay and given the core's token as its API key (it sends
it in ``x-goog-api-key``); the gatekeeper replaces it with the real key on the way out.

This SDK runs on classic ``httpx``. It is given an HTTP client of the core's making, so that the
timeouts, the redirects and the proxies are the core's choice, and so that every response is
closed when the call ends.

The SDK gives each request one timeout of its own (none unless ``HttpOptions.timeout`` is set,
which then is one scalar for connect, read, write and pool, and a ``X-Server-Timeout`` header for
the provider), replacing the client's. A request hook puts the core's four timeouts back just
before the request is sent.

``think=False`` (a short answer, asked without thinking first) changes nothing here yet: the
models do not take the same setting (a ``thinking_budget`` for some, a ``thinking_level`` for the
newer ones, which refuse the budget). Thinking summaries are never streamed as text, and
thinking tokens count in the model's limit: a short call to a model that thinks first can come
back cut short.
"""

import base64
import binascii
import json
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from typing import cast

import httpx
from google import genai
from google.genai import errors, types

from domo_core.gatekeeper_client import new_http_client
from domo_core.llm.adapters._support import (
    PendingCall,
    arguments_as_object,
    kind_for_status,
    log_failure,
)
from domo_core.llm.types import (
    AssistantToolCalls,
    ChatChunk,
    ChatUsage,
    ErrorKind,
    HistoryItem,
    JsonObject,
    ModelCallError,
    ToolCall,
    ToolChoice,
    ToolDefinition,
    ToolResult,
)


class _EmptyStream(Exception):
    """The server answered 200 and sent no chunk at all: it is not the API (a web page, say)."""


def _classify(error: Exception) -> ErrorKind:
    if isinstance(error, errors.APIError):
        return kind_for_status(error.code)
    # The SDK does not wrap the errors of the HTTP library.
    if isinstance(error, httpx.TransportError):
        return "unreachable"
    return "upstream"


def _count(value: int | None) -> int:
    return value if isinstance(value, int) and value >= 0 else 0


def _response_object(content: str) -> JsonObject:
    """What a tool returned as the object Gemini wants for a function response: the result itself
    when it is a JSON object, else the text under ``output``."""
    try:
        parsed: object = json.loads(content)
    except ValueError:
        return {"output": content}
    return cast(JsonObject, parsed) if isinstance(parsed, dict) else {"output": content}


def _signature_to_state(signature: bytes | None) -> str | None:
    """The thought signature of a function call as the text kept in ``ToolCall.provider_state``."""
    return base64.b64encode(signature).decode("ascii") if signature else None


def _state_to_signature(state: str | None) -> bytes | None:
    """The thought signature kept in ``provider_state``, to send back with the call. A state that
    is not ours (not base64) is not sent: a bad value must not make the whole call fail."""
    if not state:
        return None
    try:
        return base64.b64decode(state, validate=True)
    except (binascii.Error, ValueError):
        return None


def _request_contents(messages: Sequence[HistoryItem]) -> list[types.Content]:
    """The history as Gemini's contents.

    Gemini names no call: the results of an answer's calls come in one user content, one
    ``function_response`` per call, in the order of the calls and with their names.
    """
    contents: list[types.Content] = []
    # The function responses of the last answer's calls, to become one user content.
    results: list[types.Part] = []

    def end_results() -> None:
        if results:
            contents.append(types.Content(role="user", parts=list(results)))
            results.clear()

    for message in messages:
        if isinstance(message, ToolResult):
            results.append(
                types.Part(
                    function_response=types.FunctionResponse(
                        name=message.name, response=_response_object(message.content)
                    )
                )
            )
            continue
        end_results()
        if isinstance(message, AssistantToolCalls):
            parts = [types.Part(text=message.text)] if message.text else []
            parts.extend(
                types.Part(
                    function_call=types.FunctionCall(
                        name=call.name, args=arguments_as_object(call.arguments)
                    ),
                    thought_signature=_state_to_signature(call.provider_state),
                )
                for call in message.calls
            )
            contents.append(types.Content(role="model", parts=parts))
        else:
            contents.append(
                types.Content(
                    role="model" if message.role == "assistant" else "user",
                    parts=[types.Part(text=message.content)],
                )
            )
    end_results()
    return contents


def _request_tools(tools: Sequence[ToolDefinition]) -> list[types.Tool]:
    """The tools as function declarations. A tool with no parameter declares none: Gemini refuses
    an object schema without properties."""
    declarations = [
        types.FunctionDeclaration(
            name=tool.name,
            description=tool.description,
            parameters_json_schema=tool.parameters if tool.parameters.get("properties") else None,
        )
        for tool in tools
    ]
    return [types.Tool(function_declarations=declarations)]


def apply_client_timeouts(timeout: httpx.Timeout) -> Callable[[httpx.Request], Awaitable[None]]:
    """A request hook that gives every request the client's four timeouts."""

    async def hook(request: httpx.Request) -> None:
        request.extensions["timeout"] = timeout.as_dict()

    return hook


class _ResponseTracker:
    """Remembers the responses of an HTTP client, to close them when the call is over: the SDK
    leaves a streamed response open until its generators are finalized."""

    def __init__(self) -> None:
        self._responses: list[httpx.Response] = []

    async def track(self, response: httpx.Response) -> None:
        self._responses.append(response)

    async def aclose(self) -> None:
        for response in self._responses:
            await response.aclose()


class GoogleChat:
    """A ``ChatModel`` on the Gemini API."""

    def __init__(self, *, base_url: str, token: str, model: str) -> None:
        self._base_url = base_url
        self._token = token
        self._model = model

    def __repr__(self) -> str:
        return f"GoogleChat(base_url={self._base_url!r}, model={self._model!r})"

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
        # The list the library takes holds any of its kinds of content (the images among them once
        # ``pillow`` is installed): ours are all ``Content``.
        contents: list[types.ContentUnion] = [*_request_contents(messages)]
        config = types.GenerateContentConfig(
            system_instruction=system or None,
            max_output_tokens=max_output_tokens,
            tools=_request_tools(tools) if tools else None,
            # The tools are declared but the model may not call any (the last round of a turn).
            tool_config=(
                types.ToolConfig(
                    function_calling_config=types.FunctionCallingConfig(
                        mode=types.FunctionCallingConfigMode.NONE
                    )
                )
                if tools and tool_choice == "none"
                else None
            ),
            # The SDK would run the functions it is given as Python callables; none is, but the
            # core is the one that runs tools, behind the policy barrier.
            automatic_function_calling=(
                types.AutomaticFunctionCallingConfig(disable=True) if tools else None
            ),
        )

        failure: ErrorKind | None = None
        usage: ChatUsage | None = None
        calls: list[PendingCall] = []
        tracker = _ResponseTracker()
        http = new_http_client()
        http.event_hooks["request"].append(apply_client_timeouts(http.timeout))
        http.event_hooks["response"].append(tracker.track)
        client: genai.Client | None = None
        try:
            client = genai.Client(
                api_key=self._token,
                vertexai=False,
                http_options=types.HttpOptions(base_url=self._base_url, httpx_async_client=http),
            )
            stream = await client.aio.models.generate_content_stream(  # pyright: ignore[reportUnknownMemberType]
                model=self._model, contents=contents, config=config
            )
            try:
                seen_a_chunk = False
                async for chunk in stream:
                    seen_a_chunk = True
                    counts = chunk.usage_metadata
                    if counts is not None:
                        # Thinking tokens are billed as output.
                        usage = ChatUsage(
                            _count(counts.prompt_token_count),
                            _count(counts.candidates_token_count)
                            + _count(counts.thoughts_token_count),
                            cache_read_tokens=_count(counts.cached_content_token_count),
                        )
                    for candidate in chunk.candidates or []:
                        if candidate.content is None:
                            continue
                        for part in candidate.content.parts or []:
                            if part.thought:
                                continue
                            if part.text:
                                yield ChatChunk(part.text)
                            elif part.function_call is not None:
                                # Gemini sends a call whole, with no identifier; Gemini 3 adds
                                # a thought signature, which it wants back with the call.
                                calls.append(
                                    PendingCall(
                                        name=part.function_call.name or "",
                                        arguments=[json.dumps(part.function_call.args or {})],
                                        provider_state=_signature_to_state(part.thought_signature),
                                    )
                                )
                        break
                if not seen_a_chunk:
                    raise _EmptyStream
            finally:
                aclose = getattr(stream, "aclose", None)
                if aclose is not None:
                    await aclose()
        except Exception as error:
            failure = _classify(error)
            log_failure(failure, error)
        finally:
            await tracker.aclose()
            await http.aclose()
            if client is not None:
                client.close()
        if failure is not None:
            # Raised outside the handler: the SDK's exception holds the request, and so the token.
            raise ModelCallError(failure)
        # Streamed once the response is released.
        for pending in calls:
            if (tool_call := pending.complete()) is not None:
                yield tool_call
        if usage is not None:
            yield usage
