"""The vocabulary shared by the model adapters and their callers.

A ``ChatModel`` streams text, the tool calls the model makes and, at the end, the token usage the
provider reported; whatever goes wrong is a ``ModelCallError`` of one kind (``ErrorKind``), so that
callers (the chat page, the router, the first-model check) never see an SDK's exception types.

Tool calling is described in the providers' common terms: the tools the model may call
(``ToolDefinition``), a call it makes (``ToolCall``, with its arguments as the JSON text received),
and, in the history sent back, the calls of one answer (``AssistantToolCalls``) followed by one
``ToolResult`` per call.
"""

from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

JsonObject = dict[str, Any]

# What the model may do with the tools it is given: ``auto`` leaves it to the model, ``none``
# lets it see them (a history that holds calls and results needs them declared) but not call any.
ToolChoice = Literal["auto", "none"]

ErrorKind = Literal[
    "auth", "unreachable", "rate_limited", "bad_request", "upstream", "no_model", "limit_reached"
]


@dataclass(frozen=True)
class ChatMessage:
    """One turn of the conversation sent to the model."""

    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True)
class ToolDefinition:
    """A tool the model may call. ``parameters`` is a JSON Schema describing an object (the
    arguments of the call)."""

    name: str
    description: str
    parameters: JsonObject


@dataclass(frozen=True)
class ToolCall:
    """A call the model makes. It is only streamed once complete: ``arguments`` is the JSON text
    the provider sent (the pieces are joined by the adapter), not parsed or checked, because the
    model can send anything. Google sends no identifier: its adapter makes ``id`` up.

    ``provider_state`` is opaque text that the adapter which produced the call may need to get
    back when the call is replayed in the history (it is kept with the call, and an
    ``AssistantToolCalls`` hands it back to that adapter). Google puts the thought signature that
    Gemini 3 models want returned with a function call there; the other adapters set none and
    ignore it. Callers store and pass it on, and never read it."""

    id: str
    name: str
    arguments: str
    provider_state: str | None = None


@dataclass(frozen=True)
class AssistantToolCalls:
    """In the history: an answer of the model that calls tools. ``text`` is what it said before
    the calls (it can be empty), ``calls`` are its calls, in order."""

    text: str
    calls: tuple[ToolCall, ...]


@dataclass(frozen=True)
class ToolResult:
    """In the history: what a call returned, to follow the ``AssistantToolCalls`` that made it
    (one result per call). ``call_id`` and ``name`` are those of the call; ``content`` is the text
    the model reads."""

    call_id: str
    name: str
    content: str


HistoryItem = ChatMessage | AssistantToolCalls | ToolResult


@dataclass(frozen=True)
class ChatChunk:
    """A piece of the model's answer, in the order it arrives."""

    text: str


@dataclass(frozen=True)
class ChatUsage:
    """The tokens the provider counted for the call. Streamed once, at the very end, and only by
    providers that report it.

    ``input_tokens`` counts the whole input, cache included; ``cache_read_tokens`` and
    ``cache_write_tokens`` say how much of it was read from, or written to, the provider's cache
    (they are priced differently). Both are 0 when the provider reports no cache.

    ``cut_short`` says that the answer stopped at the token limit, before the model was done:
    what it was writing then is lost (a call being written is never streamed). A provider that
    counted no token still says so, with counts of 0."""

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cut_short: bool = False


class ModelCallError(Exception):
    """A call to a model failed.

    ``kind`` says what the caller can do about it: ``auth`` (the key is wrong or missing: enter
    it again), ``unreachable`` (the provider or the gatekeeper cannot be reached, or took too
    long), ``rate_limited`` (wait, or use another model), ``bad_request`` (the provider refused
    the request or the model name), ``upstream`` (anything else), ``no_model`` (the key opens no
    model Domo can use, or is gone: no adapter raises it, the check of a key and the account of
    a call do) and ``limit_reached`` (the call would pass a firm spending limit of its key, so it
    is not made: ``llm.metered``).

    The message is the kind and nothing else: it never carries a header value, the core's token
    or text from the provider, so it is safe to log and to show.
    """

    def __init__(self, kind: ErrorKind) -> None:
        super().__init__(kind)
        self.kind: ErrorKind = kind


class ChatModel(Protocol):
    """A model that can be asked to continue a conversation."""

    def stream(
        self,
        *,
        system: str,
        messages: Sequence[HistoryItem],
        max_output_tokens: int,
        tools: Sequence[ToolDefinition] = (),
        tool_choice: ToolChoice = "auto",
        think: bool = True,
    ) -> AsyncGenerator[ChatChunk | ToolCall | ChatUsage]:
        """Stream the answer: a ``ChatChunk`` per piece of text, then a ``ToolCall`` per call the
        model makes (each one complete, in order, once the provider's stream is over and
        released), then one ``ChatUsage`` when the provider reports its token counts. Raises
        ``ModelCallError``, before or in the middle of the stream; a call that was cut short is
        not streamed.

        Without ``tools`` (the default) no tool is declared and the model cannot call one. With
        them, the model may answer with calls, to which the caller answers by adding an
        ``AssistantToolCalls`` and its ``ToolResult``s to ``messages`` and asking again. A
        provider that does not know tools refuses the request (``bad_request``).

        ``tool_choice="none"`` declares the tools but forbids calling them: the way to ask for a
        last answer over a history that holds calls and results (Anthropic refuses such a
        history when no tool is declared). It means nothing without ``tools``.

        A call is never streamed from an answer that stopped at the token limit: what the model
        was writing then is not a call. The ``ChatUsage`` of such an answer says so
        (``cut_short``), and is streamed even when the provider counted nothing.

        ``think=False`` says that the answer is a short one (one word, one line) and asks the
        model not to think before it: a model that thinks first by default spends its
        ``max_output_tokens`` on its thinking, and an answer of a few tokens is cut before its
        first word. It is a wish, not a promise: an adapter does what its provider allows (see
        each one), a model that cannot stop thinking still does, and its answer is then cut
        short like any other. Thinking is never streamed as text, whatever was asked.

        It is an async generator (so an ``AsyncIterator`` too): a caller that may stop early wraps
        it in ``contextlib.aclosing``, so that the HTTP connection is released at once.
        """
        ...
