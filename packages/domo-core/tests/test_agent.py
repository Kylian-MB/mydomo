"""The turn: what the model streams, as events (no database, no web)."""

import asyncio
from typing import get_args

import pytest
from domo_core.chat.agent import (
    MAX_MORE_ROOM,
    MORE_ROOM_FACTOR,
    TextDelta,
    TurnDone,
    TurnFailed,
    run_turn,
)
from domo_core.llm.types import ChatMessage, ChatUsage, ErrorKind
from fakes import DEFAULT_USAGE, FakeChatModel, FixedModels


def u(content: str) -> ChatMessage:
    return ChatMessage("user", content)


async def collect(model, **overrides):
    arguments = {
        "models": FixedModels(model),
        "system_prompt": "S",
        "history": [u("hi")],
        "max_output_tokens": 32,
    } | overrides
    return [event async for event in run_turn(**arguments)]


async def test_run_turn_yields_deltas_then_done():
    events = await collect(FakeChatModel(["Hel", "lo"]))
    assert events[:2] == [TextDelta("Hel"), TextDelta("lo")] and isinstance(events[2], TurnDone)
    assert len(events) == 3


async def test_run_turn_reports_failure_after_partial_text():
    model = FakeChatModel(["Hel", "lo"], fail_after=1, error_kind="upstream")
    assert await collect(model) == [TextDelta("Hel"), TurnFailed("upstream", "Hel")]


async def test_run_turn_reports_failure_before_any_text():
    model = FakeChatModel(["Hel"], fail_after=0, error_kind="auth")
    assert await collect(model) == [TurnFailed("auth", "")]


@pytest.mark.parametrize("kind", get_args(ErrorKind))
async def test_run_turn_keeps_the_kind_of_the_error(kind):
    model = FakeChatModel(["a", "b"], fail_after=2, error_kind=kind)
    assert await collect(model) == [TextDelta("a"), TextDelta("b"), TurnFailed(kind, "ab")]


async def test_run_turn_hands_over_the_usage_the_provider_reported():
    events = await collect(FakeChatModel(["x"], usage=ChatUsage(11, 7)))
    assert events[-1] == TurnDone(ChatUsage(11, 7))
    assert (await collect(FakeChatModel(["x"])))[-1] == TurnDone(DEFAULT_USAGE)


async def test_run_turn_done_has_no_usage_when_the_provider_reports_none():
    events = await collect(FakeChatModel(["x"], usage=None))
    assert events == [TextDelta("x"), TurnDone(None)]


async def test_run_turn_does_not_yield_empty_deltas():
    events = await collect(FakeChatModel(["", "a", ""]))
    assert events[:-1] == [TextDelta("a")]


async def test_run_turn_asks_the_model_with_the_prompt_the_history_and_the_limit():
    model = FakeChatModel(["x"])
    history = [u("one"), ChatMessage("assistant", "two"), u("three")]
    await collect(model, system_prompt="Be the assistant", history=history, max_output_tokens=77)
    assert [call.system for call in model.calls] == ["Be the assistant"]
    assert [call.messages for call in model.calls] == [history]
    assert [call.max_output_tokens for call in model.calls] == [77]


async def test_run_turn_closes_the_upstream_when_the_consumer_stops_early():
    model = FakeChatModel(["a", "b", "c"])
    events = run_turn(
        models=FixedModels(model), system_prompt="S", history=[u("hi")], max_output_tokens=32
    )
    assert await anext(events) == TextDelta("a")
    assert model.closed == 0
    await events.aclose()
    assert (model.started, model.closed) == (1, 1)


async def test_run_turn_closes_the_upstream_when_cancelled():
    model = FakeChatModel(["a", "b"], delay_s=60)  # waits a minute after its first chunk
    first_seen = asyncio.Event()
    seen: list[object] = []

    async def consume() -> None:
        async for event in run_turn(
            models=FixedModels(model), system_prompt="S", history=[u("hi")], max_output_tokens=32
        ):
            seen.append(event)
            first_seen.set()

    task = asyncio.create_task(consume())
    await asyncio.wait_for(first_seen.wait(), 5)
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert seen == [TextDelta("a")]
    assert (model.started, model.closed) == (1, 1)


async def test_run_turn_closes_the_upstream_after_a_failure():
    model = FakeChatModel(["a"], fail_after=1, error_kind="rate_limited")
    assert await collect(model) == [TextDelta("a"), TurnFailed("rate_limited", "a")]
    assert (model.started, model.closed) == (1, 1)


async def test_run_turn_closes_the_upstream_after_the_last_event():
    model = FakeChatModel(["a"])
    assert isinstance((await collect(model))[-1], TurnDone)
    assert (model.started, model.closed) == (1, 1)


# --- an answer cut by the token limit --------------------------------------------------------

CUT = ChatUsage(10, 32, cut_short=True)


async def test_an_answer_cut_before_any_word_is_asked_again_with_more_room():
    """In the owner's trial of 0.7.5, the model spent its 2048 tokens on a call it could not
    finish: the call was dropped, nothing was said, and the reply was kept empty as if it were an
    answer. The same round is asked again with four times the room, twice at most."""
    model = FakeChatModel([], usage=CUT)
    events = await collect(model)
    assert [call.max_output_tokens for call in model.calls] == [
        32,
        32 * MORE_ROOM_FACTOR,
        32 * MORE_ROOM_FACTOR**2,
    ]
    assert len(model.calls) == MAX_MORE_ROOM + 1
    # More room did not help: the turn ends, and says that it was cut short.
    [done] = events
    assert isinstance(done, TurnDone) and done.cut_short


async def test_an_answer_cut_after_some_words_is_not_asked_again():
    """What was said was shown already: asking again would say it twice."""
    model = FakeChatModel(["I will "], usage=CUT)
    events = await collect(model)
    assert len(model.calls) == 1
    assert events[0] == TextDelta("I will ")
    assert isinstance(events[-1], TurnDone) and events[-1].cut_short


async def test_an_answer_that_ended_by_itself_is_not_cut_short():
    events = await collect(FakeChatModel(["Hello"]))
    assert isinstance(events[-1], TurnDone) and not events[-1].cut_short
