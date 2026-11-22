"""The first-model check: ask for one word and see whether an answer comes back."""

from collections.abc import AsyncGenerator, Sequence

import pytest
from domo_core.llm.probe import probe_model
from domo_core.llm.types import ChatChunk, ChatMessage, ChatUsage, ModelCallError
from fakes import FakeChatModel


async def test_401_maps_to_auth_error(wire_for):
    wire = wire_for("openai_compatible")
    wire.reply(401, headers={"content-type": "application/json"}, body=b'{"error": {}}')
    with pytest.raises(ModelCallError) as e:
        await probe_model(wire.model)
    assert e.value.kind == "auth"


async def test_connect_error_maps_to_unreachable(wire_for):
    wire = wire_for("openai_compatible")
    wire.fail("connect")
    with pytest.raises(ModelCallError) as e:
        await probe_model(wire.model)
    assert e.value.kind == "unreachable"


async def test_the_probe_passes_when_the_model_answers(wire_for):
    wire = wire_for("openai_compatible")
    wire.reply_text(["pong"])
    assert await probe_model(wire.model) is None
    assert len(wire.requests) == 1


async def test_the_probe_asks_for_one_word_with_a_tiny_limit():
    model = FakeChatModel(["pong"])
    await probe_model(model)
    assert [call.system for call in model.calls] == ["Reply with the single word: pong."]
    assert [call.messages for call in model.calls] == [[ChatMessage("user", "ping")]]
    assert [call.max_output_tokens for call in model.calls] == [16]
    # One word: the model is asked not to think first.
    assert [call.think for call in model.calls] == [False]


async def test_the_probe_reads_the_whole_stream():
    consumed: list[str] = []

    class Recording:
        async def stream(
            self,
            *,
            system: str,
            messages: Sequence[ChatMessage],
            max_output_tokens: int,
            think: bool,
        ) -> AsyncGenerator[ChatChunk | ChatUsage]:
            for text in ("po", "ng"):
                yield ChatChunk(text)
                consumed.append(text)
            yield ChatUsage(1, 1)
            consumed.append("usage")

    await probe_model(Recording())
    assert consumed == ["po", "ng", "usage"]


@pytest.mark.parametrize("kind", ["auth", "unreachable", "rate_limited", "bad_request", "upstream"])
async def test_the_probe_lets_the_error_of_the_model_through(kind):
    with pytest.raises(ModelCallError) as e:
        await probe_model(FakeChatModel([], fail_after=0, error_kind=kind))
    assert e.value.kind == kind


async def test_the_probe_lets_an_error_in_the_middle_of_the_stream_through():
    with pytest.raises(ModelCallError) as e:
        await probe_model(FakeChatModel(["po", "ng"], fail_after=1, error_kind="unreachable"))
    assert e.value.kind == "unreachable"
