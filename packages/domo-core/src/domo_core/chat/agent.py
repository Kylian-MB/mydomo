"""One turn of the conversation: the model's answer, and the actions it takes, as events.

``run_turn`` asks the model to continue the conversation and turns what it streams into
``TextDelta`` events, then a ``TurnDone`` (with the token usage, when the provider reported it)
or, when the model fails, a ``TurnFailed`` that carries the text received so far. It knows
nothing of the database or of the web: the caller stores the text and shows it.

The model is asked of ``models`` (``llm.picker.TurnModels``) before each round: each round takes
the route of its moment, and what changed during the turn (a key removed, a step up) holds for the
rounds that follow. When there is none (``ModelCallError``, ``no_model``), the turn fails as when
the model fails.

Tools
-----

With ``tools``, the model may answer with calls. The turn then goes in rounds: each call of the
answer is handed to ``handle_tool_call`` (which puts it through the policy barrier and, if
allowed, runs it: see ``conversation.execution``), an ``ActionTaken`` event reports what came of
it (with its round, and whether it is the last call of that round), and the model is asked
again with its answer (``AssistantToolCalls``) and one ``ToolResult`` per call, in the order of
the calls, added to the conversation. After ``max_tool_rounds`` such rounds, the last answer is
asked with the tools still declared (the conversation now holds calls and results, which some
providers refuse without them) but ``tool_choice="none"``: the model must
answer in words. A call it makes anyway, or a call made when no tool was offered, is not handled.
The calls of an answer that failed are not handled either: it is not an answer.

One answer may make ``max_calls_per_answer`` calls (8 by default). The calls beyond that are
handed over with ``over_limit=True``: the handler records them as refused without running them,
and the model gets a result for each of them all the same (a provider wants one per call).

A round whose every call could not be acted on (``invalid``: a name that is no tool's, or
arguments that are not a JSON object or that the tool rejected as such: the reasons
``unknown_tool`` and ``invalid``) is a mistake the code sees: ``models.retry_up()`` is asked, and
the next round, which has the errors in its history, is the new try, one level up while the turn
may still go up (spec 4.6). Such calls never ran, so a new try never does an action twice. A call
refused for any other reason (a name that means nobody, too many calls) is no such mistake.

The text of every round is the reply; when a round speaks after an earlier one did, a blank line
separates them.

A model that does not know tools (decision D6) refuses a request that declares them
(``bad_request``), on the first call of the turn: that call is then made once more without the
tools, and with ``system_prompt_without_tools`` when the caller gave one (the prompt that tells
Domo it cannot act). The log says so. The turn goes on in words only. A ``bad_request`` after a
round of calls is not that (the model took the tools), and a request without the tools could
not carry the calls and results of the conversation anyway: it ends the turn like any failure.

Release
-------

The model's stream is released whatever happens: at the end of each round, when the turn fails,
when the caller stops reading (``aclose``) and when the task is cancelled. The release is
shielded from the cancellation, so that the connection to the provider is closed properly and not
abandoned half way (and the provider stops generating, and charging, for a reply nobody reads).
"""

import json
import logging
from collections.abc import AsyncGenerator, Awaitable, Sequence
from dataclasses import dataclass, field
from typing import Final, Literal, Protocol
from uuid import UUID

import anyio

from domo_core.llm.picker import TurnModels
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

logger = logging.getLogger(__name__)

# How long the release of the model's stream may take once the turn is over or cancelled.
_RELEASE_SECONDS = 5.0
# What separates the text of a round from the text of the round before.
_ROUND_SEPARATOR = "\n\n"
# How many calls of one answer are run, by default (the others are recorded as refused).
MAX_CALLS_PER_ANSWER = 8

# An answer that stopped at the token limit before it said anything is asked again with more
# room: this many times at most, each time with this many times the tokens (the account of the
# call keeps the model's own limit and the spending limits).
MAX_MORE_ROOM = 2
MORE_ROOM_FACTOR = 4
# Why a call could not be acted on when the model made a mistake of form: a name that is no
# tool's, or arguments that cannot be read (``conversation.execution.invalid_record``).
_MISFIRES: Final = frozenset({"unknown_tool", "invalid"})

ActionStatus = Literal["done", "waiting", "refused", "invalid", "failed"]


@dataclass(frozen=True)
class ActionRecord:
    """What came of one call of the model: the action as the chat stores and shows it.

    ``tool_name`` is the name the model called; ``action_type`` the type of action of the catalog
    (``None`` when the name is no tool's). ``status``: ``done``; ``waiting`` for a yes (the
    confirmation is ``confirmation_id``, once there is one); ``refused`` by the barrier;
    ``invalid`` (unknown tool, arguments that are not a JSON object or that the tool rejected);
    ``failed``. ``model_result`` is what the model is given back, as JSON; ``card`` what the
    people see (stored with the reply). ``card_once`` is shown once and never stored, nor in the
    repr (see ``tools.context.ToolOutcome``). ``action_id`` is the action as it was stored with
    the reply (``message_actions``), once it is: the head of its card is kept with it
    (``texts.cards``).
    """

    tool_name: str
    action_type: str | None
    status: ActionStatus
    confirmation_id: UUID | None
    model_result: JsonObject
    card: JsonObject
    card_once: JsonObject | None = field(default=None, repr=False)
    action_id: UUID | None = None


@dataclass(frozen=True)
class TextDelta:
    """A piece of the answer, in the order it arrives."""

    text: str


@dataclass(frozen=True)
class ActionTaken:
    """A call of the model was handled: ``record`` is what came of it.

    ``round`` is the round of calls the call belongs to (1 for the calls of the first answer,
    then one more for each answer that called again), and ``closes_round`` says that it is the
    last call of that round handled: the model is asked again next, or the turn ends. A caller
    that tells the rounds apart reads them; neither counts when events are compared."""

    record: ActionRecord
    round: int = field(default=1, compare=False)
    closes_round: bool = field(default=True, compare=False)


@dataclass(frozen=True)
class TurnDone:
    """The answer is complete. ``usage`` is what the provider counted, if it said (the sum of
    every round). ``without_tools``: the model did not know tools, and answered without them
    (decision D6), so it could call none. ``cut_short``: the last answer stopped at the token
    limit, more room did not help (or the model had begun to speak): what it was writing then, a
    call included, is lost, and the caller says so."""

    usage: ChatUsage | None
    without_tools: bool = False
    cut_short: bool = False


@dataclass(frozen=True)
class TurnFailed:
    """The model failed. ``kind`` says why, ``partial_text`` is the answer received before."""

    kind: ErrorKind
    partial_text: str


TurnEvent = TextDelta | ActionTaken | TurnDone | TurnFailed


class ToolCallHandler(Protocol):
    """Handles one call of the model and says what came of it.

    ``over_limit`` is set for a call beyond the number of calls an answer may make: it must not
    run, only be recorded as refused (``invalid``, ``too_many``)."""

    def __call__(self, call: ToolCall, *, over_limit: bool = False) -> Awaitable[ActionRecord]: ...


def _misfired(record: ActionRecord) -> bool:
    """Whether a call could not be acted on because of a mistake of the model's form."""
    return record.status == "invalid" and record.model_result.get("reason") in _MISFIRES


def _added(total: ChatUsage | None, usage: ChatUsage) -> ChatUsage:
    if total is None:
        return usage
    return ChatUsage(
        total.input_tokens + usage.input_tokens, total.output_tokens + usage.output_tokens
    )


async def run_turn(
    *,
    models: TurnModels,
    system_prompt: str,
    history: Sequence[HistoryItem],
    max_output_tokens: int,
    tools: Sequence[ToolDefinition] = (),
    handle_tool_call: ToolCallHandler | None = None,
    max_tool_rounds: int = 4,
    system_prompt_without_tools: str | None = None,
    max_calls_per_answer: int = MAX_CALLS_PER_ANSWER,
) -> AsyncGenerator[TurnEvent]:
    """Ask the model of ``models`` to continue ``history`` and stream what it says and does as
    events (the model is asked for again before each round).

    The last event is always a ``TurnDone`` or a ``TurnFailed``. Failures of the model
    (``ModelCallError``) become a ``TurnFailed``; anything else is a bug and propagates, and so
    does what ``handle_tool_call`` raises (it is expected to report every outcome as a record).
    ``tools`` need a ``handle_tool_call`` (``ValueError`` otherwise). A caller that may stop early
    wraps the generator in ``contextlib.aclosing``.
    """
    if tools and handle_tool_call is None:
        raise ValueError("Tools need a handle_tool_call")
    if max_tool_rounds < 0:
        raise ValueError("max_tool_rounds cannot be negative")
    if max_calls_per_answer < 1:
        raise ValueError("max_calls_per_answer must be at least 1")
    messages: list[HistoryItem] = list(history)
    offered = tuple(tools)
    retried_without_tools = False
    rounds = 0
    more_room = 0
    received: list[str] = []
    usage: ChatUsage | None = None
    while True:
        may_call = bool(offered) and rounds < max_tool_rounds
        tool_choice: ToolChoice = "none" if offered and not may_call else "auto"
        prompt = system_prompt
        if retried_without_tools and system_prompt_without_tools is not None:
            prompt = system_prompt_without_tools
        try:
            model = await models.conversation()
        except ModelCallError as error:
            yield TurnFailed(error.kind, "".join(received))
            return
        stream = model.stream(
            system=prompt,
            messages=list(messages),
            max_output_tokens=max_output_tokens,
            tools=offered,
            tool_choice=tool_choice,
        )
        spoke_before = bool(received)
        text: list[str] = []
        calls: list[ToolCall] = []
        failure: ErrorKind | None = None
        cut_short = False
        try:
            async for item in stream:
                if isinstance(item, ChatChunk):
                    if item.text:
                        if spoke_before and not text:
                            received.append(_ROUND_SEPARATOR)
                            yield TextDelta(_ROUND_SEPARATOR)
                        text.append(item.text)
                        received.append(item.text)
                        yield TextDelta(item.text)
                elif isinstance(item, ToolCall):
                    calls.append(item)
                else:
                    cut_short = cut_short or item.cut_short
                    usage = _added(usage, item)
        except ModelCallError as error:
            failure = error.kind
        finally:
            # Before the next event is handed over, so that a caller who sees it knows the
            # upstream connection is gone.
            with anyio.move_on_after(_RELEASE_SECONDS, shield=True):
                await stream.aclose()
        if failure is not None:
            if failure == "bad_request" and offered and rounds == 0 and not text and not calls:
                # D6: a model that does not know tools. Once, the same call without them.
                logger.warning(
                    "The model refused the first request of the turn, which offered tools (it "
                    "may not know them): it is asked again without them, and the assistant cannot "
                    "act "
                    "in this reply"
                )
                offered = ()
                retried_without_tools = True
                continue
            yield TurnFailed(failure, "".join(received))
            return
        if cut_short and not calls:
            # The answer stopped at the token limit: a call it was writing is lost. Nothing said
            # yet: the same round again, with more room. Otherwise the turn ends, and says so.
            if not text and more_room < MAX_MORE_ROOM:
                more_room += 1
                max_output_tokens *= MORE_ROOM_FACTOR
                logger.info(
                    "The answer stopped at the token limit before it said anything: asked again "
                    "with more room (%d tokens)",
                    max_output_tokens,
                )
                continue
            logger.warning("The answer stopped at the token limit: the turn ends cut short")
            yield TurnDone(usage, without_tools=retried_without_tools, cut_short=True)
            return
        if calls and not may_call:
            logger.warning(
                "The model made %d tool call(s) it was not allowed to make: not handled",
                len(calls),
            )
        if not calls or not may_call or handle_tool_call is None:
            yield TurnDone(usage, without_tools=retried_without_tools)
            return
        results: list[ToolResult] = []
        records: list[ActionRecord] = []
        for position, call in enumerate(calls):
            if position < max_calls_per_answer:
                record = await handle_tool_call(call)
            else:
                record = await handle_tool_call(call, over_limit=True)
            records.append(record)
            yield ActionTaken(record, round=rounds + 1, closes_round=position == len(calls) - 1)
            content = json.dumps(record.model_result, ensure_ascii=False)
            results.append(ToolResult(call.id, call.name, content))
        messages.append(AssistantToolCalls("".join(text), tuple(calls)))
        messages.extend(results)
        rounds += 1
        if all(_misfired(record) for record in records):
            went_up = models.retry_up()
            logger.info(
                "No call of the round could be acted on: the next round is a new try%s",
                " one level up" if went_up else "",
            )
