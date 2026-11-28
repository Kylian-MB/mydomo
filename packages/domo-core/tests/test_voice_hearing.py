"""Hearing a moment of speech (spec 6.1, 6.4, 6.5, 6.11): the words and the voice print at once, at
home, the voice read against the page's person, and nothing of the sound kept."""

import logging
import os
import tempfile
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

import pytest
from domo_core.accounts.models import Person
from domo_core.channels.voice import hearing as hearing_module
from domo_core.channels.voice.hearing import CannotHear, hear
from domo_core.channels.voice.models import VoicePrint, VoiceRef
from domo_core.config import CoreSettings
from domo_core.llm.speech.client import SpeechError
from domo_core.llm.spending_models import ModelCall
from domo_core.policy.settings import HEAR_OUTSIDE_KEY
from fakes import FakeChatModel
from sqlalchemy import select
from voice_session_support import (
    CHAT_MODELS,
    HEAR_MODEL,
    PRINT_MODEL,
    FakeSpeechService,
    FakeVault,
    a_household_that_hears_at_home,
    add_key,
    sound,
    voice_deps,
)


@pytest.fixture
async def owner(add_member: Callable[..., Awaitable[Person]]) -> Person:
    return await add_member("Alex", "owner")


@pytest.fixture
def service() -> FakeSpeechService:
    return FakeSpeechService()


@pytest.fixture
def deps(session_factory, settings: CoreSettings, live_clock, service: FakeSpeechService):
    return voice_deps(
        session_factory=session_factory,
        settings=settings,
        clock=live_clock,
        service=service,
        vault=FakeVault(),
        model=FakeChatModel(["ok"]),
    )


async def _hear(deps: Any, owner: Person, now: datetime, *, for_a_proof: bool = False, pcm=None):
    return await hear(
        deps,
        person=owner,
        microphone="desk-1",
        pcm=sound() if pcm is None else pcm,
        for_a_proof=for_a_proof,
        now=now,
    )


async def test_words_and_print_come_back_together(
    deps, owner, service: FakeSpeechService, session_factory, live_clock
):
    await a_household_that_hears_at_home(session_factory, owner)
    # The words wait until the print began: heard one after the other, this would never end.
    service.together = True
    heard = await _hear(deps, owner, live_clock())
    assert heard is not None
    assert (heard.text, heard.language, heard.microphone) == ("Turn on the light.", "en", "desk-1")
    assert heard.seconds == pytest.approx(2.0)
    assert heard.print_ is not None and heard.print_.model == PRINT_MODEL
    # The voice is read against the page's person (nothing learnt yet), under their reference.
    assert heard.reading is not None and heard.reading.weighs == "unknown"
    async with session_factory() as reader:
        reference = await reader.scalar(
            select(VoiceRef.reference).where(VoiceRef.person_id == owner.id)
        )
    assert heard.reference == reference


async def test_the_language_of_a_very_short_utterance_is_not_trusted(
    deps, owner, service: FakeSpeechService, session_factory, live_clock
):
    """A half-second "Oui." is read as English by every hearing model: under
    ``LANGUAGE_FROM_S`` the language is left unknown (the answer falls back to the person's,
    then the house's)."""
    await a_household_that_hears_at_home(session_factory, owner)
    service.text, service.language = "Oui.", "en"
    short = await _hear(deps, owner, live_clock(), pcm=sound(0.5))
    assert short is not None and short.language == ""
    long_enough = await _hear(deps, owner, live_clock(), pcm=sound(hearing_module.LANGUAGE_FROM_S))
    assert long_enough is not None and long_enough.language == "en"


async def test_hearing_is_asked_at_home(
    deps, owner, service: FakeSpeechService, session_factory, live_clock
):
    # Only a paid key that hears, and the default setting: nothing may hear.
    await add_key(session_factory, owner, (*CHAT_MODELS, "whisper-1"), home=False, general=True)
    with pytest.raises(CannotHear) as cannot:
        await _hear(deps, owner, live_clock())
    assert (cannot.value.hearing, cannot.value.no_model) == ("unavailable", True)
    assert service.calls == []
    # With the service of the house, the sound goes there and nowhere else.
    home = await add_key(session_factory, owner, (HEAR_MODEL, PRINT_MODEL), home=True)
    assert await _hear(deps, owner, live_clock()) is not None
    assert {provider for _, provider in service.routes} == {home.id}


async def test_a_proof_moment_is_heard_at_home_even_when_outside_is_allowed(
    deps, owner, service: FakeSpeechService, session_factory, live_clock, set_setting
):
    paid = await add_key(
        session_factory, owner, (*CHAT_MODELS, "whisper-1"), home=False, general=True
    )
    home = await add_key(session_factory, owner, (HEAR_MODEL, PRINT_MODEL), home=True)
    await set_setting(HEAR_OUTSIDE_KEY, "yes")
    await _hear(deps, owner, live_clock())
    assert ("hear", paid.id) in service.routes
    service.routes.clear()
    await _hear(deps, owner, live_clock(), for_a_proof=True)
    assert service.routes and {provider for _, provider in service.routes} == {home.id}


async def test_a_failed_print_keeps_the_words(
    deps, owner, service: FakeSpeechService, session_factory, live_clock
):
    await a_household_that_hears_at_home(session_factory, owner)
    service.print_fails = SpeechError("unavailable")
    heard = await _hear(deps, owner, live_clock())
    assert heard is not None and heard.text == "Turn on the light."
    assert (heard.print_, heard.reading) == (None, None)


async def test_a_service_still_preparing_cannot_hear_yet(
    deps, owner, service: FakeSpeechService, session_factory, live_clock
):
    await a_household_that_hears_at_home(session_factory, owner)
    service.hear_fails = SpeechError("preparing")
    with pytest.raises(CannotHear) as cannot:
        await _hear(deps, owner, live_clock())
    assert (cannot.value.hearing, cannot.value.no_model) == ("preparing", False)
    service.hear_fails = SpeechError("timeout")
    with pytest.raises(CannotHear) as cannot:
        await _hear(deps, owner, live_clock())
    assert cannot.value.hearing == "unavailable"


@pytest.mark.parametrize("text", ["", "   ", "[noise]", "(music) ...", "*cough* ♪"])
async def test_an_empty_transcript_is_nothing(
    deps, owner, service: FakeSpeechService, session_factory, live_clock, text: str
):
    await a_household_that_hears_at_home(session_factory, owner)
    service.text = text
    assert await _hear(deps, owner, live_clock()) is None


async def test_each_call_of_sound_is_counted(
    deps, owner, service: FakeSpeechService, session_factory, live_clock
):
    await a_household_that_hears_at_home(session_factory, owner)
    await _hear(deps, owner, live_clock())
    async with session_factory() as reader:
        rows = (await reader.execute(select(ModelCall.task, ModelCall.model_id))).all()
    assert sorted(rows) == sorted([("hear", HEAR_MODEL), ("voice_print", PRINT_MODEL)])


async def test_the_sound_is_not_kept_after_hearing(
    deps,
    owner,
    service: FakeSpeechService,
    session_factory,
    live_clock,
    caplog: pytest.LogCaptureFixture,
):
    await a_household_that_hears_at_home(session_factory, owner)
    caplog.set_level(logging.DEBUG)
    places = (os.getcwd(), tempfile.gettempdir())
    before = {place: set(os.listdir(place)) for place in places}
    service.text = "My secret is under the blue lamp."
    heard = await _hear(deps, owner, live_clock())
    assert heard is not None
    # The service had the sound; nothing of it, nor the words, was written to disk or logged.
    assert service.heard == [len(sound())]
    assert {place: set(os.listdir(place)) for place in places} == before
    assert "blue lamp" not in caplog.text
    assert "\x01\x00" not in caplog.text
    assert "\\x01\\x00" not in caplog.text  # nor its escaped form (a repr of the bytes)
    # What is kept of the voice is a vector, never sound.
    async with session_factory() as reader:
        kept = (await reader.execute(select(VoicePrint.vector))).scalars().all()
    assert all(isinstance(x, float) for vector in kept for x in vector)


async def test_a_voice_that_cannot_be_read_keeps_the_words(
    deps, owner, session_factory, live_clock, monkeypatch: pytest.MonkeyPatch
):
    await a_household_that_hears_at_home(session_factory, owner)

    async def broken(*args: object, **kwargs: object) -> object:
        raise RuntimeError("the database is gone")

    monkeypatch.setattr(hearing_module, "read_voice", broken)
    heard = await _hear(deps, owner, live_clock())
    assert heard is not None and heard.text == "Turn on the light."
    assert (heard.reading, heard.reference) == (None, None)


async def test_a_phrase_with_nothing_at_home_to_hear_it_is_not_no_model(
    deps, owner, session_factory, live_clock, set_setting
):
    # Hearing may go out, but a phrase is only heard at home, where nothing hears.
    await add_key(session_factory, owner, (*CHAT_MODELS, "whisper-1"), home=False, general=True)
    await set_setting(HEAR_OUTSIDE_KEY, "yes")
    with pytest.raises(CannotHear) as cannot:
        await _hear(deps, owner, live_clock(), for_a_proof=True)
    assert (cannot.value.hearing, cannot.value.no_model) == ("unavailable", False)
