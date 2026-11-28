"""Every call of sound is reserved, settled and limited like a chat call: a home call costs
nothing and is counted all the same."""

import asyncio
from decimal import Decimal

import pytest
from domo_core.llm.catalog import UNKNOWN_PRICES, shipped_catalog
from domo_core.llm.models import Provider, ProviderModel, new_provider_id
from domo_core.llm.router import Route
from domo_core.llm.speech.catalog import FREE_SPEECH, UNKNOWN_SPEECH_PRICES, SpeechPrices
from domo_core.llm.speech.client import Heard, Print, SpeechError
from domo_core.llm.speech.metered import MeteredSpeech, prices_of
from domo_core.llm.spending_models import ModelCall, SpendingLimit
from sqlalchemy import select

TWO_SECONDS = b"\x00\x01" * 32000  # 64 000 bytes: 2.0 s at 16 kHz, 16 bits


class FakeSpeech:
    """The inner client: answers as told and remembers what it was asked."""

    def __init__(self, *, fail: SpeechError | None = None, chunks: int = 1) -> None:
        self.calls: list[str] = []
        self.fail = fail
        self.chunks = chunks
        self.closed = False
        self.started = asyncio.Event()
        self.fail_after_first = False
        self.hang = False

    async def transcribe(self, route, pcm, *, language=None) -> Heard:
        self.calls.append("transcribe")
        if self.hang:
            self.started.set()
            await asyncio.Event().wait()
        if self.fail:
            raise self.fail
        return Heard("hello", "en", 2.0, ())

    async def voice_print(self, route, pcm) -> Print:
        self.calls.append("voice_print")
        if self.fail:
            raise self.fail
        return Print((0.1,), "m", 2.0, None, None, None)

    async def say(self, route, text, *, voice, language):
        self.calls.append("say")
        if self.fail:
            raise self.fail
        try:
            for index in range(self.chunks):
                if index == 1 and self.fail_after_first:
                    raise SpeechError("timeout")
                yield b"\x00\x00"
        finally:
            self.closed = True


async def add_key(session_factory, giver, *, home: bool, model: str, kind: str | None = None):
    provider_id = new_provider_id()
    row = Provider(
        id=provider_id,
        kind=kind or ("openai_compatible" if home else "openai"),
        base_url="http://192.168.1.10:8000/v1" if home else "https://api.openai.com/v1",
        secret_ref=None if home else f"provider:{provider_id}:api_key",
        given_by=giver,
    )
    async with session_factory() as writer:
        writer.add(row)
        await writer.flush()
        writer.add(ProviderModel(provider_id=provider_id, model_id=model))
        await writer.commit()
    return row


def at_home(route: Route) -> bool:
    """What the gatekeeper would say of a key: the home keys of this file have no secret."""
    return route.provider.secret_ref is None


def route_of(key: Provider, task: str, model: str, *, known: bool = True) -> Route:
    return Route(
        provider=key,
        model_id=model,
        level="small",
        prices=UNKNOWN_PRICES,
        task=task,  # type: ignore[arg-type]
        price_known=known,
    )


def metered(inner, route, person, session_factory, clock) -> MeteredSpeech:
    return MeteredSpeech(
        inner,
        route=route,
        person_id=person.id,
        session_factory=session_factory,
        clock=clock,
        catalog=shipped_catalog(),
        prices=prices_of(route, at_home=at_home(route)),
    )


async def rows(session_factory, provider_id: str) -> list[ModelCall]:
    async with session_factory() as reader:
        found = await reader.execute(
            select(ModelCall)
            .where(ModelCall.provider_id == provider_id)
            .order_by(ModelCall.started_at, ModelCall.id)
        )
        return list(found.scalars().all())


@pytest.fixture
async def owner(add_member):
    return await add_member("Owner", "owner")


async def test_a_home_call_costs_nothing_and_is_counted(owner, session_factory, clock):
    key = await add_key(session_factory, owner.id, home=True, model="whisper-large")
    route = route_of(key, "hear", "whisper-large")
    speech = metered(FakeSpeech(), route, owner, session_factory, clock)
    await speech.transcribe(TWO_SECONDS)
    (call,) = await rows(session_factory, key.id)
    assert (call.task, call.status) == ("hear", "settled")
    assert call.heard_seconds == Decimal("2.00") and call.said_chars == 0
    assert call.cost_usd == 0 and call.person_id == owner.id


async def test_a_paid_voice_is_priced_by_its_characters(owner, session_factory, clock):
    key = await add_key(session_factory, owner.id, home=False, model="tts-1")
    route = route_of(key, "say", "tts-1")
    assert prices_of(route, at_home=at_home(route)) == SpeechPrices(Decimal(0), Decimal(15))
    speech = metered(FakeSpeech(chunks=3), route, owner, session_factory, clock)
    sound = [c async for c in speech.say("x" * 1000, voice=None, language=None)]
    assert len(sound) == 3
    (call,) = await rows(session_factory, key.id)
    assert call.task == "say" and call.said_chars == 1000 and call.heard_seconds == 0
    assert call.cost_usd == Decimal("0.015")  # 1000 characters at 15 dollars the million
    assert call.reserved_usd == Decimal("0.015")
    assert speech.cost_usd == Decimal("0.015")


async def test_a_paid_hearing_is_priced_by_its_minutes(owner, session_factory, clock):
    key = await add_key(session_factory, owner.id, home=False, model="whisper-1")
    route = route_of(key, "hear", "whisper-1")
    speech = metered(FakeSpeech(), route, owner, session_factory, clock)
    await speech.transcribe(b"\x00\x00" * 16000 * 30)  # 30 s
    (call,) = await rows(session_factory, key.id)
    assert call.cost_usd == Decimal("0.003") and call.heard_seconds == Decimal("30.00")


async def test_a_call_over_the_limit_is_not_made(owner, session_factory, clock):
    key = await add_key(session_factory, owner.id, home=False, model="tts-1")
    async with session_factory() as writer:
        writer.add(
            SpendingLimit(
                provider_id=key.id,
                amount=Decimal("0.001"),
                currency="USD",
                period="month",
                set_by=owner.id,
            )
        )
        await writer.commit()
    inner = FakeSpeech()
    speech = metered(inner, route_of(key, "say", "tts-1"), owner, session_factory, clock)
    with pytest.raises(SpeechError) as raised:
        _ = [c async for c in speech.say("x" * 1000, voice=None, language=None)]
    assert raised.value.reason == "limit_reached"
    assert inner.calls == []
    assert await rows(session_factory, key.id) == []


async def test_a_failed_call_before_any_answer_costs_nothing(owner, session_factory, clock):
    key = await add_key(session_factory, owner.id, home=False, model="tts-1")
    inner = FakeSpeech(fail=SpeechError("timeout"))
    speech = metered(inner, route_of(key, "say", "tts-1"), owner, session_factory, clock)
    with pytest.raises(SpeechError):
        _ = [c async for c in speech.say("x" * 1000, voice=None, language=None)]
    (call,) = await rows(session_factory, key.id)
    assert call.status == "settled" and call.cost_usd == 0
    assert call.said_chars == 0 and call.heard_seconds == 0


async def test_an_unknown_price_is_counted_at_the_highest(owner, session_factory, clock):
    key = await add_key(session_factory, owner.id, home=False, model="some-tts")
    route = route_of(key, "say", "some-tts", known=False)
    assert prices_of(route, at_home=at_home(route)) == UNKNOWN_SPEECH_PRICES
    speech = metered(FakeSpeech(), route, owner, session_factory, clock)
    _ = [c async for c in speech.say("x" * 1000, voice=None, language=None)]
    (call,) = await rows(session_factory, key.id)
    assert call.price_unknown
    assert call.cost_usd == Decimal("0.030")  # the highest price of the file: 30 the million


async def test_a_call_of_another_kind_is_refused(owner, session_factory, clock):
    key = await add_key(session_factory, owner.id, home=True, model="m")
    route = route_of(key, "say", "m")
    speech = metered(FakeSpeech(), route, owner, session_factory, clock)
    with pytest.raises(ValueError):
        await speech.transcribe(TWO_SECONDS)
    assert prices_of(route, at_home=at_home(route)) == FREE_SPEECH


async def test_a_say_given_up_after_one_chunk_is_settled_and_its_stream_closed(
    owner, session_factory, clock
):
    key = await add_key(session_factory, owner.id, home=False, model="tts-1")
    inner = FakeSpeech(chunks=5)
    speech = metered(inner, route_of(key, "say", "tts-1"), owner, session_factory, clock)
    closed_when_settling: list[bool] = []
    settle = speech._settle  # pyright: ignore[reportPrivateUsage]

    async def spy(*args, **kwargs):
        # The inner stream must be closed already when the settlement starts: the garbage
        # collector's finalizer would only close it later, during the settlement's awaits.
        closed_when_settling.append(inner.closed)
        await settle(*args, **kwargs)

    speech._settle = spy  # type: ignore[method-assign]
    stream = speech.say("x" * 1000, voice=None, language=None)
    assert await anext(stream) == b"\x00\x00"
    await stream.aclose()
    assert closed_when_settling == [True]
    (call,) = await rows(session_factory, key.id)
    assert call.status == "settled" and call.said_chars == 1000
    assert call.cost_usd == Decimal("0.015")


async def test_a_say_that_fails_after_its_first_chunk_is_counted_in_full(
    owner, session_factory, clock
):
    key = await add_key(session_factory, owner.id, home=False, model="tts-1")
    inner = FakeSpeech(chunks=3)
    inner.fail_after_first = True
    speech = metered(inner, route_of(key, "say", "tts-1"), owner, session_factory, clock)
    with pytest.raises(SpeechError):
        _ = [c async for c in speech.say("x" * 1000, voice=None, language=None)]
    (call,) = await rows(session_factory, key.id)
    assert call.status == "settled" and call.said_chars == 1000 and inner.closed


async def test_a_hearing_cancelled_midway_is_settled_at_nothing(owner, session_factory, clock):
    key = await add_key(session_factory, owner.id, home=False, model="whisper-1")
    inner = FakeSpeech()
    inner.hang = True
    speech = metered(inner, route_of(key, "hear", "whisper-1"), owner, session_factory, clock)
    task = asyncio.create_task(speech.transcribe(TWO_SECONDS))
    await inner.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    (call,) = await rows(session_factory, key.id)
    assert call.status == "settled" and call.cost_usd == 0 and call.heard_seconds == 0
