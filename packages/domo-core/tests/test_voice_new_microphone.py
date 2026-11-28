"""On a microphone where the person has no base print of their own, a voice is never read as
nobody known for them: it is unsure, and judged as any unsure voice (the owner, 2026-11-28,
"oui, pas sûr"). Where they have one, "resembles nobody" stays as it was."""

import json
import uuid
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from domo_core.accounts.models import Person
from domo_core.channels.voice.models import VoicePrint
from domo_core.channels.voice.prints import Reading, learn, read_voice
from domo_core.channels.voice.speaker import voice_speaker
from domo_core.channels.voice.stranger import STRANGERS
from domo_core.chat.models import Message
from domo_core.conversation.speaker import standing
from domo_core.llm.speech.client import Print
from domo_core.proofs.kinds import PROOF_KINDS, SpokenPhraseProof
from fakes import FakeChatModel
from sqlalchemy import select
from voice_session_support import PRINT_MODEL, House, a_household_that_hears_at_home
from voice_support import (
    ALEX,
    DANA,
    MIC,
    NOW,
    SAM,
    THRESHOLDS,
    learnt_voice,
    person,
    print_of,
    with_cosine,
)

WEBCAM = "webcam"


async def _read(session, who, vector, microphone):
    return await read_voice(
        session,
        person_id=who.id,
        microphone=microphone,
        print_=print_of(vector),
        thresholds=THRESHOLDS,
        now=NOW,
    )


# --- The reading ----------------------------------------------------------------------------------


async def test_a_far_voice_on_a_new_microphone_is_unsure_not_nobody(session):
    alex = await person(session)
    await learnt_voice(session, alex, ALEX)
    there = await _read(session, alex, SAM, WEBCAM)
    assert there.learnt and there.new_microphone
    assert there.weighs == "unknown" and not there.resembles_nobody
    here = await _read(session, alex, SAM, MIC)
    assert not here.new_microphone and here.resembles_nobody


async def test_another_named_between_the_thresholds_on_a_new_microphone_is_unsure(session):
    """Dana named, not clearly her (a degree between the thresholds), Alex far: "against" where
    Alex has a base, unsure where he has none."""
    alex = await person(session)
    dana = await person(session, "Dana", "adult")
    await learnt_voice(session, alex, ALEX)
    await learnt_voice(session, dana, DANA)
    near_dana = with_cosine(DANA, 0.45, SAM)
    here = await _read(session, alex, near_dana, MIC)
    assert here.weighs == "against" and here.best_other == dana.id
    there = await _read(session, alex, near_dana, WEBCAM)
    assert there.weighs == "unknown" and there.best_other == dana.id


async def test_another_known_voice_clearly_is_still_that_person_on_a_new_microphone(session):
    alex = await person(session)
    dana = await person(session, "Dana", "adult")
    await learnt_voice(session, alex, ALEX)
    await learnt_voice(session, dana, DANA)
    there = await _read(session, alex, DANA, WEBCAM)
    assert there.weighs == "against" and there.best_other == dana.id


async def test_nothing_learnt_anywhere_is_no_new_microphone(session):
    alex = await person(session)
    reading = await _read(session, alex, SAM, WEBCAM)
    assert not reading.learnt and not reading.new_microphone


async def test_a_far_voice_on_a_new_microphone_teaches_nothing(session):
    """Read unsure, it is given to the learning as the page person's: their bases elsewhere do
    not read "for" it, so no candidate is made or touched there."""
    alex = await person(session)
    await learnt_voice(session, alex, ALEX)
    result = await learn(
        session,
        person_id=alex.id,
        microphone=WEBCAM,
        print_=print_of(SAM),
        thresholds=THRESHOLDS,
        sure_otherwise=True,
        visit_at=NOW,
        now=NOW,
    )
    assert result == "nothing"
    rows = await session.scalars(select(VoicePrint).where(VoicePrint.microphone == WEBCAM))
    assert list(rows) == []


# --- Who speaks -----------------------------------------------------------------------------------

_NOW = datetime(2026, 11, 28, 20, 0, tzinfo=UTC)
_PAGE = Person(id=uuid.uuid4(), display_name="Alex", role="owner")
_PRINT = Print(
    vector=(0.1, 0.2, 0.3), model="print-1", seconds=2.0, pitch_hz=None, pitch_range_hz=None,
    rate=None,
)  # fmt: skip


def _speaker(reading: Reading):
    return voice_speaker(
        page_person=_PAGE,
        reading=reading,
        print_=_PRINT,
        microphone=WEBCAM,
        signed_in_at=_NOW - timedelta(hours=1),
        proofs=(),
        now=_NOW,
    )


def _far(**fields: Any) -> Reading:
    return Reading(
        **{
            "weighs": "unknown",
            "degree": 0.05,
            "learnt": True,
            "best_other": None,
            "other_degree": 0.0,
            "intonation_fits": None,
            **fields,
        }
    )


def test_a_far_voice_on_a_new_microphone_is_the_page_person_unsure():
    speaker = _speaker(_far(new_microphone=True))
    assert speaker.person_id == _PAGE.id
    assert standing(speaker) == "unsure"
    [voice] = [c for c in speaker.clues if c.kind == "voice"]
    assert voice.weighs == "against" and voice.degree == 0.05


def test_a_far_voice_where_the_person_has_a_base_is_still_nobody_known():
    speaker = _speaker(_far())
    assert speaker.person_id is None
    assert standing(speaker) == "unknown"


def test_a_named_other_between_the_thresholds_on_a_new_microphone_is_unsure():
    other = uuid.uuid4()
    reading = _far(new_microphone=True, weighs="unknown", best_other=other, other_degree=0.45)
    speaker = _speaker(reading)
    assert speaker.person_id == _PAGE.id and standing(speaker) == "unsure"


# --- At a page ------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean() -> Iterator[None]:
    kind = PROOF_KINDS["spoken_phrase"]
    assert isinstance(kind, SpokenPhraseProof)
    kind.forget_waits()
    STRANGERS.clear()
    yield
    kind.forget_waits()
    STRANGERS.clear()


@pytest.fixture
async def owner(add_member: Callable[..., Awaitable[Person]]) -> Person:
    return await add_member("Alex", "owner")


@pytest.fixture
async def house(session_factory, settings, live_clock, owner):
    await a_household_that_hears_at_home(session_factory, owner)
    async with session_factory() as writer:
        await learnt_voice(writer, owner, ALEX, microphone=MIC, model=PRINT_MODEL)
        await writer.commit()
    made: list[House] = []

    def make(model: FakeChatModel) -> House:
        found = House(session_factory, settings, live_clock, owner, model)
        found.service.vector = SAM
        made.append(found)
        return found

    yield make
    for each in made:
        await each.close()


def _stranger_calls(model: FakeChatModel) -> list[Any]:
    return [
        c for c in model.calls if c.system.startswith("You are the assistant of a household, heard")
    ]


async def _stored(session_factory, text: str) -> list[Message]:
    async with session_factory() as reader:
        return list(await reader.scalars(select(Message).where(Message.content == text)))


async def test_a_voice_unlike_the_page_person_at_a_new_microphone_is_answered_as_theirs_unsure(
    house, session_factory, owner
):
    """No whose-voice question, no stranger's exchange: the sentence is the page person's,
    marked heard unsure, and answered in their conversation."""
    model = FakeChatModel(["The light is on."])
    h = house(model)
    await h.page(WEBCAM).hears("Turn on the light.")
    assert h.whose.asked == []
    assert _stranger_calls(model) == []
    [stored] = await _stored(session_factory, "Turn on the light.")
    assert stored.heard_unsure is True
    assert ("assistant", "The light is on.", "complete") in await h.messages()


async def test_the_same_voice_at_the_microphone_he_was_learnt_on_is_asked_whose_it_is(
    house, session_factory
):
    stranger = json.dumps({"say": "Hello.", "note": "A voice I did not know spoke."})
    model = FakeChatModel([stranger])
    h = house(model)
    await h.page(MIC).hears("Turn on the light.")
    assert len(h.whose.asked) == 1
    assert await _stored(session_factory, "Turn on the light.") == []
