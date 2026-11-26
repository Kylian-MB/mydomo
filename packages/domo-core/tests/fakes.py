"""Test doubles of the model layer: a scripted ``ChatModel`` and a factory that hands it out.

They are for the tests of the pages that talk to a model (first-model setup, chat) and of the
agent that lets a model call tools: no HTTP, no SDK, and a model that behaves exactly as the test
says. The yes-judge's calls (``confirmations.judge``) are answered apart (``JUDGE_FIRST_LINE``),
and so are those of the writer and the checker of the texts (``texts.writer``).
"""

import asyncio
import json
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final

from domo_common.sandbox_api import (
    OUTPUT_HELD,
    Beyond,
    JsonObject,
    Machine,
    RunRequest,
    RunResult,
    ScanFinding,
    ScanResult,
)
from domo_core.confirmations.intent import classifier_prompt
from domo_core.llm.models import Provider
from domo_core.llm.types import (
    ChatChunk,
    ChatMessage,
    ChatModel,
    ChatUsage,
    ErrorKind,
    HistoryItem,
    ModelCallError,
    ToolCall,
    ToolChoice,
    ToolDefinition,
    ToolResult,
)
from domo_core.sandbox_client import Answerer, SandboxBusy, SandboxNotHere, SandboxUnavailable
from domo_core.texts.writer import (
    REVIEW_TOOL,
    TEXT_CHECKER_FIRST_LINE,
    TEXT_WRITER_FIRST_LINE,
    TEXTS_TOOL,
    ForWhom,
)
from pydantic import JsonValue

DEFAULT_USAGE = ChatUsage(3, 2)

# The first line of the yes-judge's prompt (``prompts/yes_judge.md``): a call whose system prompt
# starts with it is the judge's.
JUDGE_FIRST_LINE: Final = (
    "You judge whether an assistant may do one action now, and what it should ask first."
)
# What a fake judge says when nothing tells it what to say.
JUDGE_DEFAULT: Final = "yes"


def is_judge_call(system: str) -> bool:
    """Whether a call under the system prompt ``system`` is the yes-judge's."""
    return system.startswith(JUDGE_FIRST_LINE)


# The words of the isolated reader of the replies to requests (``confirmations.intent``): a test
# that scripts one of them alone means the line of the first request shown ("1: yes").
READER_WORDS: Final = frozenset({"yes", "firm", "no", "change", "unclear"})


def is_reader_call(system: str) -> bool:
    """Whether a call under the system prompt ``system`` is the reader's of the replies."""
    return system == classifier_prompt()


def as_the_reader_says(item: ChatChunk | ToolCall) -> ChatChunk | ToolCall:
    """What the reader streams for ``item`` of a script: a bare word of its five is its line for
    the first request shown; anything else, as it is."""
    if isinstance(item, ChatChunk) and item.text.strip() in READER_WORDS:
        return ChatChunk(f"1: {item.text.strip()}")
    return item


# What a fake writer of texts writes for a key: ``(key, meaning, for_whom) -> text``; and what a
# fake checker says of a text: ``(key, text) -> why``, or ``None`` when it refuses nothing.
TextWriter = Callable[[str, str, ForWhom], str]
TextChecker = Callable[[str, str], str | None]


def is_text_writer_call(system: str) -> bool:
    """Whether a call under the system prompt ``system`` is the writer's of the texts."""
    return system.startswith(TEXT_WRITER_FIRST_LINE)


def is_text_checker_call(system: str) -> bool:
    """Whether a call under the system prompt ``system`` is the checker's of the texts."""
    return system.startswith(TEXT_CHECKER_FIRST_LINE)


def _asked(messages: Sequence[HistoryItem]) -> dict[str, Any]:
    """The JSON of the one message a call of the writer or the checker starts with."""
    first = messages[0]
    assert isinstance(first, ChatMessage), "the writer and the checker start with one message"
    return json.loads(first.content)


@dataclass(frozen=True)
class FakeCall:
    """What one call to a ``FakeChatModel`` received."""

    system: str
    messages: list[HistoryItem]
    tools: tuple[ToolDefinition, ...]
    max_output_tokens: int
    tool_choice: ToolChoice = "auto"
    think: bool = True


class FakeChatModel:
    """A model that streams what it is told to, one by one, then the usage.

    What each call streams comes from one of two scripts (not both):

    * ``chunks``: every call streams the same text chunks;
    * ``tool_script``: each call takes the next entry, which is either a ``ToolCall`` (streamed
      as the call the model makes) or a text (streamed as one chunk). A call that is not given
      tools, or is given them with ``tool_choice="none"``, has no use for a ``ToolCall``: it
      skips them, up to and including the next text, which it streams. Once the script is used
      up, a call streams an empty text.

    The other options:

    * ``delay_s`` is the pause between two streamed items (none before the first), so that a test
      can stop the consumer while the stream is still going;
    * with ``fail_after`` set, the stream raises ``ModelCallError(error_kind)`` once that many
      items (chunks or tool calls) were streamed (0: before any); the usage is not streamed then.
      A value beyond the number of items never triggers;
    * ``usage=None`` streams no usage, like a provider that does not report it;
    * with ``refuse_tools``, a call that is given tools (whatever its ``tool_choice``) fails at
      once with ``ModelCallError("bad_request")``, like a model that does not know tools; the
      script is not used up by it;
    * ``judge`` is what the yes-judge is answered (a call whose system prompt starts with
      ``JUDGE_FIRST_LINE``), apart from everything above: one text for every call of the judge,
      or a list whose next entry each call takes (``"yes"`` once it is used up). The script is
      not touched, and ``fail_after``, ``delay_s`` and ``refuse_tools`` do not count for it.
      ``judge=None`` turns that off: the judge's call is then served like any other.
    * The isolated reader of the replies to requests (``is_reader_call``) answers one line per
      request, ``<n>: word``: a script entry that is one of its words alone (``READER_WORDS``)
      is streamed as the line of the first request, ``1: word``, as the tests of one request
      shown write it; any other entry, as it is.
    * The writer of the texts (a call whose system prompt starts with ``TEXT_WRITER_FIRST_LINE``)
      and their checker (``TEXT_CHECKER_FIRST_LINE``) are answered apart too, always, by a call of
      their tool, followed by the usage. The writer gives each key of its message its text:
      ``writer(key, meaning, for_whom)`` when ``writer`` is given, else the key's ``current``
      text, or its meaning as it is; asked again with errors (the result of its call), it writes
      the keys named, and those alone. The checker refuses each text for which
      ``checker(key, text)`` gives a reason; without ``checker``, none. The script is not
      touched, and ``fail_after``, ``delay_s`` and ``refuse_tools`` do not count for them.

    What each call received is recorded in ``calls``, a ``FakeCall`` per call, in order, but for
    the judge's calls answered apart: ``judge_calls()`` lists the judge's calls, either way. The
    calls of the writer and the checker are never in ``calls``: ``writer_calls()`` and
    ``checker_calls()`` list them. ``started`` counts the streams that began and ``closed`` those
    that were released (finished, failed, abandoned by their reader or cancelled), the judge's,
    the writer's and the checker's included: a reader that stops early must see them equal.
    """

    def __init__(
        self,
        chunks: Sequence[str] = (),
        *,
        tool_script: Sequence[ToolCall | str] | None = None,
        fail_after: int | None = None,
        error_kind: ErrorKind = "upstream",
        usage: ChatUsage | None = DEFAULT_USAGE,
        delay_s: float = 0.0,
        refuse_tools: bool = False,
        judge: str | Sequence[str] | None = JUDGE_DEFAULT,
        writer: TextWriter | None = None,
        checker: TextChecker | None = None,
    ) -> None:
        if chunks and tool_script is not None:
            raise ValueError("Give chunks or a tool_script, not both")
        self._writer = writer
        self._checker = checker
        self._wrote: list[FakeCall] = []
        self._checked: list[FakeCall] = []
        self._judge: str | list[str] | None = (
            judge if judge is None or isinstance(judge, str) else list(judge)
        )
        self._next_verdict = 0
        self._judged: list[FakeCall] = []
        self._chunks = list(chunks)
        self._script = None if tool_script is None else list(tool_script)
        self._next_entry = 0
        self._fail_after = fail_after
        self._error_kind: ErrorKind = error_kind
        self._usage = usage
        self._delay_s = delay_s
        self._refuse_tools = refuse_tools
        self.calls: list[FakeCall] = []
        self.started = 0
        self.closed = 0

    def judge_calls(self) -> list[FakeCall]:
        """What each call of the yes-judge received, in order: those answered apart, or, with
        ``judge=None``, those served like any other call."""
        return [*self._judged, *(call for call in self.calls if is_judge_call(call.system))]

    def writer_calls(self) -> list[FakeCall]:
        """What each call of the writer of the texts received, in order."""
        return list(self._wrote)

    def checker_calls(self) -> list[FakeCall]:
        """What each call of the checker of the texts received, in order."""
        return list(self._checked)

    def _written(self, messages: Sequence[HistoryItem]) -> ToolCall:
        """The writer's call: each key asked (those named by the errors, when it is asked again),
        with its text."""
        asked = _asked(messages)
        for_whom = ForWhom(**asked["for_whom"])
        entries: dict[str, dict[str, Any]] = asked["texts"]
        keys = list(entries)
        last = messages[-1]
        if isinstance(last, ToolResult):
            keys = [key for key in json.loads(last.content)["errors"] if key in entries]
        texts = {
            key: (
                self._writer(key, entries[key]["meaning"], for_whom)
                if self._writer is not None
                else entries[key].get("current", entries[key]["meaning"])
            )
            for key in keys
        }
        arguments = json.dumps({"texts": texts}, ensure_ascii=False)
        return ToolCall(f"fake-writer-{len(self._wrote)}", TEXTS_TOOL.name, arguments)

    def _reviewed(self, messages: Sequence[HistoryItem]) -> ToolCall:
        """The checker's call: the texts ``checker`` refuses, each with its reason."""
        texts: dict[str, dict[str, Any]] = _asked(messages)["texts"]
        refused: list[dict[str, str]] = []
        if self._checker is not None:
            for key, told in texts.items():
                why = self._checker(key, told["text"])
                if why is not None:
                    refused.append({"key": key, "why": why})
        arguments = json.dumps({"refused": refused}, ensure_ascii=False)
        return ToolCall(f"fake-checker-{len(self._checked)}", REVIEW_TOOL.name, arguments)

    def _verdict(self) -> str:
        """What the judge's call is answered (and the list moves on)."""
        if isinstance(self._judge, str):
            return self._judge
        assert self._judge is not None
        if self._next_verdict < len(self._judge):
            self._next_verdict += 1
            return self._judge[self._next_verdict - 1]
        return JUDGE_DEFAULT

    def _items_for(self, may_call_tools: bool) -> list[ChatChunk | ToolCall]:
        """What this call streams, and the script moves on (all of it before the first await, so
        that concurrent calls each take their own entry)."""
        if self._script is None:
            return [ChatChunk(text) for text in self._chunks]
        while self._next_entry < len(self._script):
            entry = self._script[self._next_entry]
            self._next_entry += 1
            if isinstance(entry, ToolCall):
                if may_call_tools:
                    return [entry]
                continue
            return [ChatChunk(entry)]
        return [ChatChunk("")]

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
        call = FakeCall(system, list(messages), tuple(tools), max_output_tokens, tool_choice, think)
        if is_text_writer_call(system) or is_text_checker_call(system):
            if is_text_writer_call(system):
                self._wrote.append(call)
                answer = self._written(messages)
            else:
                self._checked.append(call)
                answer = self._reviewed(messages)
            self.started += 1
            try:
                yield answer
                if self._usage is not None:
                    yield self._usage
            finally:
                self.closed += 1
            return
        if self._judge is not None and is_judge_call(system):
            self._judged.append(call)
            verdict = self._verdict()
            self.started += 1
            try:
                yield ChatChunk(verdict)
                if self._usage is not None:
                    yield self._usage
            finally:
                self.closed += 1
            return
        self.calls.append(call)
        self.started += 1
        try:
            if tools and self._refuse_tools:
                raise ModelCallError("bad_request")
            items = self._items_for(bool(tools) and tool_choice == "auto")
            if is_reader_call(system):
                items = [as_the_reader_says(item) for item in items]
            for index, item in enumerate(items):
                if self._fail_after == index:
                    raise ModelCallError(self._error_kind)
                if index > 0 and self._delay_s > 0:
                    await asyncio.sleep(self._delay_s)
                yield item
            if self._fail_after == len(items):
                raise ModelCallError(self._error_kind)
            if self._usage is not None:
                yield self._usage
        finally:
            self.closed += 1


class FakeModelFactory:
    """A model factory that gives ``model`` whatever the key and the model id, and notes what was
    asked for in ``requested``: ``(provider id, model id)``, in order."""

    def __init__(self, model: ChatModel) -> None:
        self._model = model
        self.requested: list[tuple[str, str]] = []

    def __call__(self, provider: Provider, model_id: str) -> ChatModel:
        self.requested.append((provider.id, model_id))
        return self._model


def fake_factory(model: ChatModel) -> FakeModelFactory:
    """A model factory that gives ``model`` whatever the provider and the model id."""
    return FakeModelFactory(model)


class FixedModels:
    """The models of a turn for the tests of ``run_turn`` (``llm.picker.TurnModels``): every
    round is asked of ``model``, which never goes up a level (``retry_up`` is ``False``)."""

    def __init__(self, model: ChatModel) -> None:
        self.model = model

    async def conversation(self) -> ChatModel:
        return self.model

    def retry_up(self) -> bool:
        return False


# The models a fake key lists unless a test says otherwise.
LISTED_MODELS: Final = ("example-haiku-1", "example-sonnet-1", "example-opus-1")


class FakeModelLister:
    """A lister of models for the tests: ``models`` is what every key lists (``()`` for a server
    that gives no list), ``error`` makes the listing fail (``ModelCallError(error)``, a key the
    provider refuses). ``listed`` notes the ids of the keys it was asked about.
    ``while_asked``, if set, runs while the provider is asked, before it answers (a key removed
    in the meantime, say)."""

    def __init__(self) -> None:
        self.models: tuple[str, ...] = LISTED_MODELS
        self.error: ErrorKind | None = None
        self.listed: list[str] = []
        self.while_asked: Callable[[Provider], Awaitable[None]] | None = None

    async def __call__(self, provider: Provider) -> tuple[str, ...]:
        self.listed.append(provider.id)
        if self.while_asked is not None:
            await self.while_asked(provider)
        if self.error is not None:
            raise ModelCallError(self.error)
        return self.models


class FakeRefused(Exception):
    """What a fake program meets when the core refuses its request (``domo.Refused``)."""

    def __init__(
        self,
        reason: str,
        detail: str | None = None,
        setting: str | None = None,
        value: int | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail
        # The setting of Domo's that caused it, and its figure (``madetools.settings.hit``).
        self.setting = setting
        self.value = value


class FakeStopped(Exception):
    """What a fake program meets when the core stops its run instead of answering."""


# A request of a fake program to the core: the ``ok`` value of the answer, or ``FakeRefused``,
# or ``FakeStopped``.
type FakeAsk = Callable[[JsonObject], Awaitable[JsonValue]]


class FakeSandbox:
    """A sandbox for the tests: it executes nothing. A run is noted in ``runs`` (and whether a
    turn waited on it in ``waited``) and answered by ``program`` (a coroutine of the request and
    of its ``ask``, whose requests go to the run's answerer and are noted in ``asks``), or by
    ``run`` (a function of the request), or, by default, as a tool that finished and gave back
    ``None``; while ``gate`` is set, a run waits for it to open first. While ``busy``, a run that
    may not wait for a place raises ``SandboxBusy``. A scan is noted in ``scans`` and finds
    ``findings`` (none by default). Its machine is ``has`` (memory not known by default: nothing
    refused); a run that asks more memory or output than it has raises ``SandboxNotHere``, and
    with ``has`` at ``None`` it answers neither ``machine`` nor ``set_places`` (noted in
    ``places_told``)."""

    def __init__(
        self,
        run: Callable[[RunRequest], RunResult] | None = None,
        program: Callable[[RunRequest, FakeAsk], Awaitable[JsonValue]] | None = None,
    ) -> None:
        self._run = run
        self._program = program
        self.runs: list[RunRequest] = []
        self.waited: list[bool] = []
        self.asks: list[JsonObject] = []
        self.busy = False
        self.gate: asyncio.Event | None = None
        self.scans: list[str] = []
        self.findings: list[ScanFinding] = []
        self.has: Machine | None = Machine(memory_mb=None, processors=None, places=4)
        self.places_told: list[int] = []

    def _beyond(self, request: RunRequest) -> list[Beyond]:
        """What the machine ``has`` cannot give ``request``, as the launcher judges it."""
        memory = None if self.has is None else self.has.memory_mb
        if memory is None:
            return []
        beyond: list[Beyond] = []
        if request.memory_mb > memory:
            beyond.append("memory_mb")
        assert self.has is not None
        if request.output_bytes * OUTPUT_HELD * self.has.places > memory * 1024 * 1024:
            beyond.append("output_bytes")
        return beyond

    async def run(
        self, request: RunRequest, *, answer: Answerer, waited_by_a_turn: bool = False
    ) -> RunResult:
        beyond = self._beyond(request)
        if beyond:
            assert self.has is not None
            raise SandboxNotHere(beyond, self.has)
        if self.busy and not request.wait_for_a_place:
            raise SandboxBusy
        self.runs.append(request)
        self.waited.append(waited_by_a_turn)
        if self.gate is not None:
            await self.gate.wait()
        if self._program is not None:
            return await self._led(request, answer, self._program)
        if self._run is not None:
            return self._run(request)
        return RunResult(status="done", output=None, error=None, seconds=0.0)

    async def _led(
        self,
        request: RunRequest,
        answer: Answerer,
        program: Callable[[RunRequest, FakeAsk], Awaitable[JsonValue]],
    ) -> RunResult:
        async def ask(question: JsonObject) -> JsonValue:
            self.asks.append(question)
            reply = await answer(question)
            if reply is None:
                raise FakeStopped
            if "refused" in reply:
                detail, setting, value = (
                    reply.get("detail"),
                    reply.get("setting"),
                    reply.get("value"),
                )
                raise FakeRefused(
                    str(reply["refused"]),
                    detail if isinstance(detail, str) else None,
                    setting if isinstance(setting, str) else None,
                    value if isinstance(value, int) else None,
                )
            return reply.get("ok")

        try:
            output = await program(request, ask)
        except FakeStopped:
            return RunResult(status="stopped", note="The run was stopped.", seconds=0.0)
        except FakeRefused as refused:
            return RunResult(status="error", error=f"domo.Refused: {refused.reason}", seconds=0.0)
        return RunResult(status="done", output=output, error=None, seconds=0.0)

    async def scan(self, code: str) -> ScanResult:
        self.scans.append(code)
        return ScanResult(findings=list(self.findings))

    async def set_places(self, places: int) -> None:
        if self.has is None:
            raise SandboxUnavailable("The fake sandbox does not answer.")
        self.places_told.append(places)
        self.has = self.has.model_copy(update={"places": places})

    async def machine(self) -> Machine:
        if self.has is None:
            raise SandboxUnavailable("The fake sandbox does not answer.")
        return self.has
