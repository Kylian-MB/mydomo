"""A voice nobody knows at someone's page (spec 6.5; Task 26): a short exchange, with no tool and
nothing of anybody, a word left to the page's person, and nothing else kept."""

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from chat_support import dump
from domo_core.accounts.models import Person
from domo_core.audit.models import AuditEntry
from domo_core.channels.voice import session as session_module
from domo_core.channels.voice import stranger as stranger_module
from domo_core.channels.voice.hearing import HeardUtterance
from domo_core.channels.voice.models import VoiceMoment, VoiceRef
from domo_core.channels.voice.prints import Reading
from domo_core.channels.voice.stranger import (
    EXCHANGE_LASTS,
    NOTE_EVERY,
    STRANGER_FLOOD,
    STRANGERS,
    key_of,
)
from domo_core.chat.models import Message
from domo_core.confirmations.models import Confirmation
from domo_core.conversation.events import Event
from domo_core.conversation.speaker import Clue, Speaker
from domo_core.llm.spending_models import ModelCall
from domo_core.llm.types import ToolCall
from domo_core.memory.models import Memory
from domo_core.notes.models import Note
from domo_core.policy.barrier import Allow
from domo_core.prompts import stranger_prompt
from fakes import FakeChatModel
from sqlalchemy import func, select
from sqlalchemy import text as sql_text
from voice_session_support import House, a_household_that_hears_at_home

MIC = "desk-1"
STRANGER_REF = "v-00000000000000aa"
SAY = "Hello. I cannot do that for a voice I do not know, but I will pass the word on."
WORD = "A voice I did not know asked me, in its words, to open the garage."
ANSWER = json.dumps({"say": SAY, "note": WORD})
# He answers, then stops answering this voice for five minutes.
PAUSE = json.dumps({"say": SAY, "note": WORD, "go_on": False, "pause_minutes": 5})


@pytest.fixture(autouse=True)
def forget_the_strangers() -> Iterator[None]:
    STRANGERS.clear()
    yield
    STRANGERS.clear()


@pytest.fixture
async def owner(add_member: Callable[..., Awaitable[Person]]) -> Person:
    return await add_member("Alex", "owner")


def nobody(now: datetime) -> Speaker:
    """A voice far from the page's person and like nobody known."""
    return Speaker(
        None,
        "voice",
        clues=(Clue("voice", "against", "weak", "code", now, degree=0.1),),
        microphone=MIC,
    )


# A voice the reading finds far from the page's person and near nobody known.
LIKE_NOBODY = Reading(
    weighs="unknown",
    degree=0.1,
    learnt=True,
    best_other=None,
    other_degree=0.0,
    intonation_fits=None,
)


class Voice:
    """Whose voice the next sentences are: the page's person's (``None``, as the voice says) or
    one nobody knows, under ``STRANGER_REF``."""

    def __init__(self) -> None:
        self.unknown = False
        # The voice has no print: its exchange is kept under the microphone's key.
        self.no_print = False


@pytest.fixture
async def voice(monkeypatch, session_factory) -> Voice:
    found = Voice()
    async with session_factory() as writer:
        writer.add(VoiceRef(reference=STRANGER_REF))
        await writer.commit()
    real_speaker, real_hear = session_module.voice_speaker, session_module.hear

    def voice_speaker(**kwargs: Any) -> Speaker:
        return nobody(kwargs["now"]) if found.unknown else real_speaker(**kwargs)

    async def hear(*args: Any, **kwargs: Any) -> Any:
        heard = await real_hear(*args, **kwargs)
        if heard is None or not found.unknown:
            return heard
        if found.no_print:
            return replace(heard, reference=None, reading=None)
        # Read as resembling nobody known: kept under a reference of its own.
        return replace(heard, reference=STRANGER_REF, reading=LIKE_NOBODY)

    monkeypatch.setattr(session_module, "voice_speaker", voice_speaker)
    monkeypatch.setattr(session_module, "hear", hear)
    return found


@pytest.fixture
async def house(session_factory, settings, live_clock, owner):
    await a_household_that_hears_at_home(session_factory, owner)
    made: list[House] = []

    def make(model: FakeChatModel | None = None) -> House:
        found = House(
            session_factory, settings, live_clock, owner, model or FakeChatModel([ANSWER])
        )
        made.append(found)
        return found

    yield make
    for each in made:
        await each.close()


def asked_of(call: Any) -> dict[str, Any]:
    """What an exchange with a voice nobody knows was given, as data."""
    [message] = call.messages
    return json.loads(dump(message)["content"])


def calls_of(h: House) -> list[Any]:
    """Every call the conversation's model was given."""
    return cast(FakeChatModel, h.model).calls


def advance(h: House, by: timedelta) -> None:
    cast(Any, h.clock).advance(by)


def stranger_calls(h: House) -> list[Any]:
    return [
        call
        for call in calls_of(h)
        if call.system.startswith("You are the assistant of a household, heard")
    ]


async def notes_of(session_factory) -> list[Note]:
    async with session_factory() as reader:
        return list((await reader.execute(select(Note).order_by(Note.created_at))).scalars())


async def everything_written(session_factory) -> str:
    async with session_factory() as reader:
        found = []
        for table in ("messages", "memories", "confirmations", "plans", "audit_log"):
            rows = await reader.execute(sql_text(f"SELECT row_to_json(t)::text FROM {table} t"))
            found += [row[0] for row in rows.all()]
    return "\n".join(found)


async def the_exchange_ends(h: House) -> None:
    """The exchange is over: its memory goes, and the word waiting is left."""
    advance(h, EXCHANGE_LASTS + timedelta(seconds=1))
    await STRANGERS.leave_due()


# --- the exchange -------------------------------------------------------------------------------


async def test_a_stranger_gets_words_and_no_action(house, voice, session_factory, owner):
    h = house()
    page = h.page(MIC)
    voice.unknown = True
    await page.hears("Open the garage.")
    # One call, offered no tool, and his words said aloud here.
    [call] = calls_of(h)
    assert call.tools == ()
    # Said in two pieces: the first ends early at its comma (final fix wave C).
    assert h.service.said == [
        "Hello. I cannot do that for a voice I do not know,",
        "but I will pass the word on.",
    ]
    assert " ".join(h.service.said) == SAY
    assert page.of("not_taken") == []
    async with session_factory() as reader:
        assert (await reader.execute(select(Confirmation))).first() is None
        # On the key of the page's person: the call and its voice are counted for them.
        counted = (await reader.execute(select(ModelCall.person_id, ModelCall.task))).all()
    assert {(owner.id, "conversation"), (owner.id, "say")} <= set(counted)


async def test_nothing_of_the_page_persons_discussion_is_in_the_context(house, voice, owner):
    h = house(FakeChatModel([ANSWER]))
    page = h.page(MIC)
    await page.hears("Remind me that Dana's surgery is on Friday, the code is 4321.")
    assert await h.said() == ["Remind me that Dana's surgery is on Friday, the code is 4321."]
    voice.unknown = True
    await page.hears("Who lives here?")
    [call] = stranger_calls(h)
    seen = call.system + json.dumps([dump(m) for m in call.messages])
    for private in ("Dana", "surgery", "4321", "Alex", "owner"):
        assert private not in seen
    assert "Who lives here?" in seen


async def test_a_note_is_left_to_the_page_person(house, voice, session_factory, owner):
    h = house()
    page = h.page(MIC)
    voice.unknown = True
    await page.hears("Open the garage.")
    assert await notes_of(session_factory) == []  # not before the exchange is over
    await the_exchange_ends(h)
    [note] = await notes_of(session_factory)
    # His own word, for the page's person, with no exact words of anybody.
    assert (note.by_domo, note.author_id, note.recipient_id) == (True, None, owner.id)
    assert (note.text, note.exact_text) == (WORD, None)
    # Born of a voice nobody knows: it keeps its source (final fix wave B2).
    assert note.from_unknown is True
    async with session_factory() as reader:
        actors = (
            await reader.execute(
                sql_text(
                    "SELECT actor, action FROM audit_log WHERE action LIKE 'note.%' OR "
                    "action = 'policy.decision'"
                )
            )
        ).all()
    assert ("domo", "note.left") in actors
    assert ("domo", "policy.decision") in actors


async def test_nothing_of_the_exchange_is_stored_but_the_note(house, voice, session_factory, owner):
    h = house()
    page = h.page(MIC)
    voice.unknown = True
    await page.hears("Open the garage.")
    await the_exchange_ends(h)
    written = await everything_written(session_factory)
    for word in ("Open the garage", "pass the word", "garage"):
        assert word not in written
    async with session_factory() as reader:
        assert await reader.scalar(select(func.count()).select_from(Message)) == 0
        assert await reader.scalar(select(func.count()).select_from(Memory)) == 0
    assert len(await notes_of(session_factory)) == 1


async def test_a_new_voice_is_told_once_that_he_listens_if_he_chooses(
    house, voice, session_factory
):
    h = house()
    page = h.page(MIC)
    voice.unknown = True
    await page.hears("Open the garage.")
    [first] = stranger_calls(h)
    assert asked_of(first)["told_you_listen"] is False
    reply = page.of("say")[0]["reply"]
    # Cut by the page: not told.
    await page.session.played(uuid.UUID(reply), whole=False)
    async with session_factory() as reader:
        assert (
            await reader.scalar(
                select(VoiceRef.warned_at).where(VoiceRef.reference == STRANGER_REF)
            )
            is None
        )
    await page.hears("Please?")
    second = stranger_calls(h)[-1]
    assert asked_of(second)["told_you_listen"] is False
    # Said whole: told, kept on the reference.
    reply = page.of("say")[-1]["reply"]
    await page.session.played(uuid.UUID(reply), whole=True)
    async with session_factory() as reader:
        assert (
            await reader.scalar(
                select(VoiceRef.warned_at).where(VoiceRef.reference == STRANGER_REF)
            )
            is not None
        )
    await page.hears("Hello again.")
    assert asked_of(stranger_calls(h)[-1])["told_you_listen"] is True
    # The next exchange, much later, still knows it.
    await the_exchange_ends(h)
    await page.hears("Hello.")
    assert asked_of(stranger_calls(h)[-1])["told_you_listen"] is True


async def test_the_exchange_is_forgotten_after_ten_minutes(house, voice):
    h = house()
    page = h.page(MIC)
    voice.unknown = True
    await page.hears("Open the garage.")
    await page.hears("Please?")
    assert asked_of(stranger_calls(h)[-1])["before"] == [{"voice": "Open the garage.", "you": SAY}]
    assert asked_of(stranger_calls(h)[-1])["your_word_so_far"] == WORD
    advance(h, EXCHANGE_LASTS)
    await page.hears("Hello.")
    assert asked_of(stranger_calls(h)[-1])["before"] == []


# --- order, cost and abuse ----------------------------------------------------------------------


async def test_what_is_not_for_him_goes_to_the_buffer_unchanged(house, voice):
    h = house()
    page = h.page(MIC)
    h.attention.answer = "not_mine"
    voice.unknown = True
    await page.hears("Shall we go out tonight?")
    assert stranger_calls(h) == []
    assert len(page.session._unaddressed) == 1  # noqa: SLF001
    assert page.of("not_taken") == []


async def test_no_model_while_a_phrase_moment_lives_here(house, voice, session_factory, owner):
    h = house()
    page = h.page(MIC)
    async with session_factory() as writer:
        writer.add(
            VoiceMoment(
                person_id=owner.id,
                microphone=MIC,
                purpose="phrase_agree",
                expires_at=h.clock() + timedelta(minutes=1),
            )
        )
        await writer.commit()
    voice.unknown = True
    await page.hears("Open the garage.")
    assert calls_of(h) == [] and h.attention.asked == []
    # Silence, as for any sentence not said to him: nothing tells that a phrase is awaited here.
    assert page.of("not_taken") == []


def answers(h: House, text: str) -> None:
    """What the conversation's model answers from now on."""
    cast(Any, h.model)._chunks = [text]  # noqa: SLF001 - the fake's script


async def test_no_fixed_count_he_goes_on_answering_a_voice(house, voice, session_factory):
    # The owner (S4): "the assistant judges" whether he goes on; no count of 6 stops a voice.
    h = house()
    page = h.page(MIC)
    voice.unknown = True
    for _ in range(20):
        await page.hears("Open the garage.")
    calls = stranger_calls(h)
    assert len(calls) == 20
    # He is told how many answers this voice had, and the flood bound is not near.
    assert [asked_of(c)["answers_so_far"] for c in calls] == list(range(20))
    assert not any(asked_of(c)["last_answer_for_a_while"] for c in calls)
    assert page.of("not_taken") == []
    assert await notes_of(session_factory) == []  # the exchange goes on: no word yet


async def test_he_pauses_a_voice_and_nothing_is_called_for_it_meanwhile(
    house, voice, session_factory
):
    h = house(FakeChatModel([PAUSE]))
    page = h.page(MIC)
    voice.unknown = True
    await page.hears("Open the garage. Open it. OPEN IT.")
    assert len(stranger_calls(h)) == 1
    assert " ".join(h.service.said) == SAY
    # He stopped: the word is left at once, one.
    assert len(await notes_of(session_factory)) == 1
    attention, whose = len(h.attention.asked), len(h.whose.asked)
    advance(h, timedelta(minutes=4))
    await page.hears("Open the garage!")
    # While paused: no model at all (quick judgement and whose-voice included), and silence.
    assert len(stranger_calls(h)) == 1
    assert (len(h.attention.asked), len(h.whose.asked)) == (attention, whose)
    assert page.of("not_taken") == []
    async with session_factory() as reader:
        [paused] = (
            await reader.execute(
                select(AuditEntry).where(AuditEntry.action == "voice.stranger_paused")
            )
        ).scalars()
    assert paused.details == {"microphone": MIC, "minutes": 5}
    # The pause he chose is over: answered again.
    answers(h, ANSWER)
    advance(h, timedelta(minutes=1, seconds=1))
    await page.hears("Hello?")
    assert len(stranger_calls(h)) == 2


async def test_a_pause_on_the_microphone_never_silences_the_page_person(
    house, voice, session_factory, owner
):
    # A voice with no print is kept under the microphone's key, which the page person's own
    # voice, when not recognised, shares: a stranger asking him to be quiet for an hour pauses
    # only the sentences that still go the stranger's path after the usual questions.
    hour = json.dumps({"say": SAY, "note": WORD, "go_on": False, "pause_minutes": 60})
    h = house(FakeChatModel([hour]))
    page = h.page(MIC)
    voice.unknown = voice.no_print = True
    await page.hears("Be quiet for an hour.")
    assert len(stranger_calls(h)) == 1
    async with session_factory() as reader:
        [paused] = (
            await reader.execute(
                select(AuditEntry).where(AuditEntry.action == "voice.stranger_paused")
            )
        ).scalars()
    # At most the exchange's own life on the microphone's key, whatever he asked.
    assert paused.details == {"microphone": MIC, "minutes": 10}
    # The page person, hoarse, two minutes later: the whose-voice question is asked, and they
    # are answered.
    advance(h, timedelta(minutes=2))
    answers(h, "The light is on.")
    h.whose.answer = "theirs"
    asked = len(h.whose.asked)
    await page.hears("Turn on the light.")
    assert len(h.whose.asked) == asked + 1
    assert await h.said() == ["Turn on the light."]
    # A voice that is truly nobody's stays paused: no stranger's answer, and silence.
    h.whose.answer = "someone_else"
    await page.hears("Hello?")
    assert len(h.whose.asked) == asked + 2
    assert len(stranger_calls(h)) == 1
    assert page.of("not_taken") == []
    # The exchange's life over: answered again (not after the hour he asked).
    answers(h, ANSWER)
    advance(h, EXCHANGE_LASTS - timedelta(minutes=2))
    await page.hears("Hello again?")
    assert len(stranger_calls(h)) == 2


async def test_a_voice_with_its_own_print_keeps_the_pause_he_chose(house, voice):
    hour = json.dumps({"say": SAY, "note": WORD, "go_on": False, "pause_minutes": 60})
    h = house(FakeChatModel([hour]))
    page = h.page(MIC)
    voice.unknown = True
    await page.hears("Be quiet for an hour.")
    answers(h, ANSWER)
    advance(h, timedelta(minutes=59))
    await page.hears("Hello?")
    assert len(stranger_calls(h)) == 1
    advance(h, timedelta(minutes=1, seconds=1))
    await page.hears("Hello?")
    assert len(stranger_calls(h)) == 2


@pytest.mark.parametrize(
    ("given", "minutes"),
    [(0, 1), (500, 60), ("x", 10), (None, 10), (5.7, 5), (True, 10), (float("inf"), 60)],
    ids=["zero", "too_long", "text", "missing", "fraction", "boolean", "infinite"],
)
def test_the_pause_he_chooses_is_kept_within_bounds(given, minutes):
    told: dict[str, Any] = {"say": "Bye.", "note": "", "go_on": False}
    if given is not None:
        told["pause_minutes"] = given
    read = stranger_module.read_answer(json.dumps(told))
    assert read is not None
    assert (read.go_on, read.pause) == (False, timedelta(minutes=minutes))


@pytest.mark.parametrize(
    "told",
    [{"say": "Hi.", "note": ""}, {"say": "Hi.", "note": "", "go_on": "no"}],
    ids=["missing", "garbled"],
)
def test_a_missing_or_garbled_go_on_means_he_goes_on(told):
    # Never silent by a parsing accident.
    read = stranger_module.read_answer(json.dumps(told))
    assert read is not None and read.go_on is True
    assert stranger_module.read_answer("no JSON here") is None


def test_the_flood_bound_is_per_microphone_whatever_the_voice():
    strangers = stranger_module.Strangers()
    at = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)
    flood = stranger_module.STRANGER_FLOOD
    taken = [strangers.take(f"v-{n % 7}", MIC, at) for n in range(flood)]
    # The answer that reaches the bound is told so; one more is no model call at all.
    assert taken == [False] * (flood - 1) + [True]
    assert strangers.take("v-new", MIC, at) is None
    assert strangers.paused("v-new", MIC, at)
    # Another microphone is answered; this one again once the window frees.
    assert strangers.take("v-new", "another-mic", at) is False
    later = at + stranger_module.STRANGER_WINDOW
    assert not strangers.paused("v-new", MIC, later)
    assert strangers.take("v-new", MIC, later) is False


async def test_a_flooded_microphone_calls_no_model_and_stays_silent(house, voice, session_factory):
    h = house()
    page = h.page(MIC)
    for n in range(stranger_module.STRANGER_FLOOD):
        STRANGERS.take(f"v-radio{n}", MIC, h.clock())
    voice.unknown = True
    await page.hears("Open the garage.")
    assert stranger_calls(h) == [] and h.attention.asked == []
    assert page.of("not_taken") == []


async def test_the_last_answer_before_the_flood_bound_is_told_so(house, voice, session_factory):
    h = house()
    page = h.page(MIC)
    for n in range(stranger_module.STRANGER_FLOOD - 1):
        STRANGERS.take(f"v-radio{n}", MIC, h.clock())
    voice.unknown = True
    await page.hears("Open the garage.")
    [call] = stranger_calls(h)
    assert asked_of(call)["last_answer_for_a_while"] is True
    # The bound reached: the word goes at once.
    assert len(await notes_of(session_factory)) == 1


async def test_with_the_page_persons_keys_at_their_limit_nothing_is_called(
    house, voice, monkeypatch
):
    async def no_picker(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(stranger_module, "person_picker", no_picker)
    h = house()
    page = h.page(MIC)
    voice.unknown = True
    await page.hears("Open the garage.")
    assert stranger_calls(h) == []
    # Not paused, not flooded: the indistinct notice of a sentence not taken.
    assert page.of("not_taken") != []


async def test_while_paused_the_silence_is_the_same_with_a_phrase_moment(
    house, voice, session_factory, owner
):
    h = house(FakeChatModel([PAUSE]))
    page = h.page(MIC)
    voice.unknown = True
    await page.hears("Open the garage.")
    await page.hears("Open it!")
    async with session_factory() as writer:
        writer.add(
            VoiceMoment(
                person_id=owner.id,
                microphone=MIC,
                purpose="phrase_agree",
                expires_at=h.clock() + timedelta(minutes=1),
            )
        )
        await writer.commit()
    await page.hears("Open it now!")
    # Nothing tells him a phrase is awaited: silence before and after it began.
    assert page.of("not_taken") == []
    assert len(stranger_calls(h)) == 1


async def test_at_most_one_word_per_voice_in_ten_minutes(house, voice, session_factory):
    h = house()
    page = h.page(MIC)
    voice.unknown = True
    await page.hears("Open the garage.")
    await the_exchange_ends(h)
    assert len(await notes_of(session_factory)) == 1
    # A new exchange he stops before ten minutes passed since that word: it waits.
    advance(h, timedelta(seconds=30))
    answers(h, PAUSE)
    await page.hears("Open the garage.")
    assert len(await notes_of(session_factory)) == 1
    advance(h, NOTE_EVERY)
    await STRANGERS.leave_due()
    assert len(await notes_of(session_factory)) == 2


async def test_the_word_is_outside_words_in_the_page_persons_turn(
    house, voice, session_factory, owner
):
    keep = ToolCall(
        "c1",
        "memory_keep",
        json.dumps(
            {
                "content": "Someone wants the garage open.",
                "subject": "garage",
                "kind": "fact",
                "private": False,
                "core": False,
            }
        ),
    )
    h = house(FakeChatModel(tool_script=[ANSWER, keep, "Noted."]))
    page = h.page(MIC)
    voice.unknown = True
    await page.hears("Open the garage.")
    await the_exchange_ends(h)
    voice.unknown = False
    await page.hears("Anything new?")
    turn = calls_of(h)[-2]
    assert WORD in turn.system  # the word is told to the page's person, as a note
    async with session_factory() as reader:
        origin = await reader.scalar(select(Memory.origin))
    # Never the page person's own words.
    assert origin == "others_words"


async def test_nothing_is_logged_of_the_words(house, voice, caplog):
    caplog.set_level(logging.DEBUG)
    h = house()
    page = h.page(MIC)
    voice.unknown = True
    await page.hears("Open the garage.")
    await the_exchange_ends(h)
    assert "garage" not in caplog.text
    assert "garage" not in repr(STRANGERS)
    assert "garage" not in repr(stranger_module.Exchange("Open the garage.", SAY, h.clock()))


# --- fix round 1 --------------------------------------------------------------------------------


class Said:
    """A listener of a reply to a voice nobody knows, in a test: what it was given."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def open(self) -> None:
        return None

    async def emit(self, event: Event) -> None:
        self.events.append(event)


def heard_now(h: House, text: str) -> HeardUtterance:
    return HeardUtterance(text, "en", 2.0, MIC, None, None, STRANGER_REF, h.clock())


async def answer(h: House, owner: Person, text: str) -> bool:
    return await stranger_module.answer_a_stranger(
        h.deps,
        page_person=owner,
        heard=heard_now(h, text),
        reference=STRANGER_REF,
        listener=cast(Any, Said()),
    )


def test_the_prompt_lets_him_say_he_does_not_know_the_voice_and_nothing_more():
    prompt = stranger_prompt()
    assert "do not know" in prompt
    assert "recognised" not in prompt  # no contradiction with saying he does not know it
    for never in ("how close", "names", "proof"):
        assert never in prompt
    # He judges whether he goes on, knowing who pays.
    for weighed in ('"go_on"', '"pause_minutes"', "paid on the key", "how many answers"):
        assert weighed in prompt


async def test_two_sentences_at_once_are_answered_one_after_the_other(house, owner):
    h = house(FakeChatModel([ANSWER], delay_s=0.05))
    await asyncio.gather(answer(h, owner, "Open the garage."), answer(h, owner, "Please?"))
    first, second = stranger_calls(h)
    assert asked_of(first)["before"] == []
    assert len(asked_of(second)["before"]) == 1


async def test_two_sentences_at_once_never_pass_the_flood_bound(house, owner):
    h = house(FakeChatModel([ANSWER], delay_s=0.05))
    for n in range(STRANGER_FLOOD - 1):
        STRANGERS.take(f"v-radio{n}", MIC, h.clock())
    results = await asyncio.gather(answer(h, owner, "Now."), answer(h, owner, "Now!"))
    assert sorted(results) == [False, True]
    assert len(stranger_calls(h)) == 1


async def test_two_sentences_at_once_after_he_paused_get_no_second_call(house, owner):
    h = house(FakeChatModel([PAUSE], delay_s=0.05))
    results = await asyncio.gather(answer(h, owner, "Now."), answer(h, owner, "Now!"))
    assert sorted(results) == [False, True]
    assert len(stranger_calls(h)) == 1


async def test_a_rule_asking_first_keeps_the_word_waiting(
    house, owner, session_factory, monkeypatch
):
    h = house()
    real = stranger_module.decide_now
    asks = {"on": True}

    async def decide_now(*args: Any, **kwargs: Any) -> Any:
        found = await real(*args, **kwargs)
        return Allow(rule=cast(Any, "ask")) if asks["on"] else found

    monkeypatch.setattr(stranger_module, "decide_now", decide_now)
    assert await answer(h, owner, "Open the garage.")
    advance(h, EXCHANGE_LASTS)
    await STRANGERS.leave_due()
    # Not left, and not dropped: it waits.
    assert await notes_of(session_factory) == []
    assert STRANGERS.word(STRANGER_REF, owner.id) == WORD
    asks["on"] = False
    advance(h, NOTE_EVERY)
    await STRANGERS.leave_due()
    [note] = await notes_of(session_factory)
    assert note.text == WORD


async def test_the_word_goes_to_the_page_person_even_when_another_shares_their_name(
    house, owner, session_factory, add_member
):
    other = await add_member("Alex", "adult")
    h = house()
    assert await answer(h, owner, "Open the garage.")
    advance(h, EXCHANGE_LASTS)
    await STRANGERS.leave_due()
    [note] = await notes_of(session_factory)
    assert note.recipient_id == owner.id != other.id


async def test_the_counts_of_old_voices_are_forgotten(house, owner):
    h = house()
    for n in range(4):
        STRANGERS.take(f"v-old{n}", f"mic-{n}", h.clock())
        STRANGERS.pause(f"v-old{n}", h.clock(), timedelta(minutes=5))
    advance(h, NOTE_EVERY + EXCHANGE_LASTS)
    STRANGERS.take("v-new", "mic-new", h.clock())
    sizes = STRANGERS.sizes()
    assert sizes == {"by_reference": 1, "by_microphone": 1, "paused": 0, "noted": 0}


# --- fix round 2 --------------------------------------------------------------------------------


async def test_a_word_being_left_is_not_lost_to_a_new_exchange(house, owner, monkeypatch):
    h = house()
    assert await answer(h, owner, "Open the garage.")
    advance(h, EXCHANGE_LASTS)
    release = asyncio.Event()
    entered = asyncio.Event()

    async def leave_word(*args: Any) -> str:
        entered.set()
        await release.wait()
        return "waits"

    monkeypatch.setattr(stranger_module, "_leave_word", leave_word)
    leaving = asyncio.create_task(STRANGERS.leave_due())
    await entered.wait()
    answering = asyncio.create_task(answer(h, owner, "Hello?"))
    # The new exchange waits for the word being left (else it would end on its own meanwhile).
    await asyncio.wait({answering}, timeout=2.0)
    release.set()
    await leaving
    assert await answering
    # The new exchange read the word that waited: nothing of it was lost.
    assert asked_of(stranger_calls(h)[-1])["your_word_so_far"] == WORD


async def test_a_word_whose_leaving_fails_waits(house, owner, monkeypatch):
    h = house()
    assert await answer(h, owner, "Open the garage.")
    advance(h, EXCHANGE_LASTS)

    async def leave_word(*args: Any) -> str:
        raise RuntimeError("down")

    monkeypatch.setattr(stranger_module, "_leave_word", leave_word)
    await STRANGERS.leave_due()
    assert STRANGERS.word(STRANGER_REF, owner.id) == WORD


# --- final fix wave -----------------------------------------------------------------------------


def test_a_stranger_never_shares_the_page_persons_voice_reference():
    """A voice taken for nobody known but read near the page's person (not "resembles nobody")
    is kept by the reading under the PAGE PERSON's reference: the stranger's exchange, its caps
    and its warning are kept under the microphone's instead; a voice that resembles nobody keeps
    its own reference."""
    near = replace(LIKE_NOBODY, degree=0.45)
    at = datetime(2026, 11, 26, 12, 0)
    heard = HeardUtterance("Hello?", "en", 2.0, MIC, None, near, "v-the-page-person", at)
    assert not near.resembles_nobody
    assert key_of(heard) == f"@{MIC}"
    assert LIKE_NOBODY.resembles_nobody
    assert key_of(replace(heard, reading=LIKE_NOBODY, reference=STRANGER_REF)) == STRANGER_REF


async def test_a_word_held_by_a_rule_is_tried_for_a_day_then_dropped_with_one_audit_line(
    house, owner, monkeypatch, session_factory
):
    h = house()
    assert await answer(h, owner, "Open the garage.")
    advance(h, EXCHANGE_LASTS)
    tries: list[int] = []

    async def leave_word(*args: Any) -> str:
        tries.append(1)
        return "waits"  # an owner's rule asks first: no turn here may ask the yes

    monkeypatch.setattr(stranger_module, "_leave_word", leave_word)
    await STRANGERS.leave_due()
    assert STRANGERS.word(STRANGER_REF, owner.id) == WORD
    advance(h, stranger_module.HELD_WORD_LASTS - timedelta(minutes=1))
    await STRANGERS.leave_due()
    assert STRANGERS.word(STRANGER_REF, owner.id) == WORD
    advance(h, timedelta(minutes=NOTE_EVERY.seconds // 60 + 1))
    await STRANGERS.leave_due()
    # A day of tries: it goes, with one line in the journal (never its words).
    assert STRANGERS.word(STRANGER_REF, owner.id) is None
    assert len(tries) == 3
    async with session_factory() as reader:
        found = await reader.execute(
            select(AuditEntry).where(AuditEntry.action == "voice.stranger_word_dropped")
        )
        [entry] = found.scalars().all()
    assert entry.details == {"person_id": str(owner.id)}
    assert "garage" not in json.dumps(entry.details)
    await STRANGERS.leave_due()
    assert len(tries) == 3


async def test_a_word_written_again_keeps_when_it_was_first_held():
    # Final fix wave, voice minor 4: a voice that comes back every day never keeps its held word
    # alive past ``HELD_WORD_LASTS``.
    strangers = stranger_module.Strangers()
    deps = cast(Any, object())
    page_person = uuid.uuid4()
    first = datetime(2026, 11, 26, 20, 0, tzinfo=UTC)
    key = ("v-0123456789abcdef", page_person)
    try:
        strangers.keep_word(deps, key[0], page_person, "a", microphone="m", now=first, capped=False)
        strangers._words[key].held_since = first  # noqa: SLF001 - held back once
        later = first + timedelta(days=1)
        strangers.keep_word(deps, key[0], page_person, "b", microphone="m", now=later, capped=False)
        assert strangers._words[key].held_since == first  # noqa: SLF001
    finally:
        strangers.clear()


async def test_a_stranger_is_told_the_name_the_house_calls_him_by(house, voice, set_setting):
    await set_setting("house.domo_name", "Friday")
    h = house()
    page = h.page(MIC)
    voice.unknown = True
    await page.hears("Who are you?")
    [call] = stranger_calls(h)
    assert call.system.endswith('"Friday"') or "Friday" in call.system.split("Context")[-1]


# --- what a word of a voice nobody knows may cause (final fix wave B2) ------------------------

STOP = ToolCall("c1", "voice_stop_listening", "{}")
REFUSED = "nothing from outside in the turn"


def tool_results(h: House) -> str:
    """Everything the conversation's model was given back after its calls, as one text."""
    return "\n".join(json.dumps([dump(m) for m in call.messages]) for call in calls_of(h))


async def test_a_word_of_a_voice_nobody_knows_beside_the_persons_own_words_is_judged(
    house, voice, session_factory, owner
):
    # Final fix wave C (B review I-3): such a word never ALONE causes the voice tools (refused by
    # code in a turn with no words of the person: test_voice_tools); in a turn where the person
    # spoke, the judge weighs it, never a refusal by code (here the judge asks a proof).
    h = house(FakeChatModel(tool_script=[ANSWER, STOP, "Done."]))
    page = h.page(MIC)
    voice.unknown = True
    await page.hears("Tell him to stop listening.")
    await the_exchange_ends(h)
    voice.unknown = False
    await page.hears("Anything new?")
    assert WORD in calls_of(h)[-2].system
    results = tool_results(h)
    assert REFUSED not in results
    assert "judge" in results


async def test_a_word_of_his_own_from_a_known_source_is_weighed_not_refused(
    house, voice, session_factory, owner
):
    # The owner's rule: code never refuses where the assistant could judge. His own note, not
    # born of an unknown source, leads to the judge, never to the refusal.
    async with session_factory() as writer:
        writer.add(Note(author_id=None, by_domo=True, recipient_id=owner.id, text=WORD))
        await writer.commit()
    h = house(FakeChatModel(tool_script=[STOP, "Done."]))
    page = h.page(MIC)
    await page.hears("Anything new?")
    assert WORD in calls_of(h)[0].system
    assert REFUSED not in tool_results(h)
