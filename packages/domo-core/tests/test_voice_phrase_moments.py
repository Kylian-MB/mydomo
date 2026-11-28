"""The spoken phrase at the voice (spec 6.9, Review Focus 4): agreed in a reserved moment whose
sayings go to the vault and nowhere else, and given as a proof only in a proof window of a
microphone (the assistant spoke to the person there within ``PROOF_WINDOW_S``)."""

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Iterator, Sequence
from datetime import timedelta
from typing import Any

import pytest
from chat_support import dump
from domo_core.accounts.models import Person
from domo_core.audit.models import AuditEntry
from domo_core.channels.page.abilities import PAGE
from domo_core.channels.page.speaker import page_speaker
from domo_core.channels.voice import session as session_module
from domo_core.channels.voice.deps import RETRY_TELL_AFTER_S
from domo_core.channels.voice.live import LATE_COPY_MARGIN_SECONDS
from domo_core.channels.voice.models import VoiceMoment
from domo_core.channels.voice.moments import open_moment
from domo_core.confirmations.models import Confirmation
from domo_core.conversation.events import Event
from domo_core.conversation.prepare import TurnRequest
from domo_core.conversation.service import Conversation
from domo_core.llm.types import ToolCall
from domo_core.policy.settings import HEAR_OUTSIDE_KEY
from domo_core.proofs.kinds import PROOF_KINDS, SpokenPhraseProof, phrase_ref
from domo_core.proofs.models import ProofMean
from domo_core.proofs.service import agree
from domo_core.tools.voice_tools import WEAKNESS_TOLD
from fakes import FakeChatModel
from sqlalchemy import select
from sqlalchemy import text as sql_text
from voice_session_support import (
    CHAT_MODELS,
    HEAR_MODEL,
    PRINT_MODEL,
    SAY_MODEL,
    House,
    a_household_that_hears_at_home,
    add_key,
    sound,
    until,
)

PHRASE = "blue horses sing at dawn"
NEAR = "blue horses sleep at noon"
ORDINARY = "no, send me a message instead"
MIC = "desk-1"


@pytest.fixture(autouse=True)
def no_phrase_waits() -> Iterator[None]:
    kind = PROOF_KINDS["spoken_phrase"]
    assert isinstance(kind, SpokenPhraseProof)
    kind.forget_waits()
    yield
    kind.forget_waits()


@pytest.fixture
async def owner(add_member: Callable[..., Awaitable[Person]]) -> Person:
    return await add_member("Alex", "owner")


class Voice:
    """A page of the owner (``microphone``), with the household it stands on (``house``: a new
    one, or a page more of another's)."""

    def __init__(self, session_factory, settings, clock, owner, model, microphone, house) -> None:
        self.session_factory = session_factory
        self.clock = clock
        self.owner = owner
        self.house = house or House(session_factory, settings, clock, owner, model)
        self.at = self.house.page(microphone)
        self.session = self.at.session
        self.page = self.at.socket
        self.model: FakeChatModel = model
        self.service = self.house.service
        self.vault = self.house.vault
        self.woken = self.house.woken
        self.deps = self.house.deps

    async def say(self, text: str) -> None:
        """``text`` said here, and handled to its end (its turn or its wake included)."""
        await self.at.hears(text)

    async def written(self) -> str:
        """Every message and every audit entry, as one text."""
        async with self.session_factory() as reader:
            rows = (await reader.execute(sql_text("SELECT * FROM messages"))).all()
            audit = await reader.execute(sql_text("SELECT row_to_json(a)::text FROM audit_log a"))
            entries = [row[0] for row in audit.all()]
        return repr([tuple(row) for row in rows]) + "\n".join(entries)

    def model_saw(self) -> str:
        calls = [*self.model.calls, *self.model.judge_calls()]
        return "".join(call.system + json.dumps([dump(m) for m in call.messages]) for call in calls)

    async def user_messages(self) -> list[str]:
        return await self.house.said()


@pytest.fixture
async def make_voice(session_factory, settings, live_clock, owner):
    made: list[Voice] = []

    def make(
        model: FakeChatModel | None = None, microphone: str = MIC, house: House | None = None
    ) -> Voice:
        found = Voice(
            session_factory,
            settings,
            live_clock,
            owner,
            model or FakeChatModel(["ok"]),
            microphone,
            house,
        )
        made.append(found)
        return found

    yield make
    for each in made:
        if each.house.pages:
            await each.house.close()
            each.house.pages.clear()


@pytest.fixture
async def voice(make_voice, session_factory, owner):
    await a_household_that_hears_at_home(session_factory, owner)
    return make_voice


async def a_moment(session_factory, owner: Person, now, microphone: str | None = None):
    async with session_factory() as writer:
        await open_moment(
            writer, person_id=owner.id, microphone=microphone, purpose="phrase_agree", now=now
        )
        await writer.commit()


async def the_moment(session_factory) -> VoiceMoment | None:
    async with session_factory() as reader:
        return (await reader.execute(select(VoiceMoment))).scalars().first()


async def means_of(session_factory, owner: Person) -> list[str]:
    async with session_factory() as reader:
        found = await reader.execute(
            select(ProofMean.kind).where(
                ProofMean.person_id == owner.id, ProofMean.removed_at.is_(None)
            )
        )
        return list(found.scalars())


# --- agreeing ---------------------------------------------------------------------------------


async def test_the_two_sayings_go_to_the_vault_and_nowhere_else(
    voice, session_factory, owner, live_clock, caplog
):
    caplog.set_level(logging.DEBUG)
    v = voice()
    await a_moment(session_factory, owner, live_clock())
    await v.say(PHRASE)
    moment = await the_moment(session_factory)
    assert moment is not None and (moment.step, moment.microphone) == (1, MIC)
    await v.say(PHRASE)
    # The vault holds it, the means is the person's, the moment is over.
    assert v.vault.phrase == {phrase_ref(owner.id): PHRASE}
    assert await means_of(session_factory, owner) == ["spoken_phrase"]
    assert await the_moment(session_factory) is None
    # He is woken to tell each outcome in his own words: data, never the words.
    assert v.woken.contents() == [{"phrase": "first_kept"}, {"phrase": "agreed"}]
    assert [person for person, _ in v.woken.wakes] == [owner.id, owner.id]
    # Nowhere else: no message, no model, no journal, no log.
    for word in ("horses", "dawn"):
        assert word not in await v.written()
        assert word not in v.model_saw()
        assert word not in caplog.text
        assert word not in json.dumps(v.woken.contents())
    assert v.model.calls == []
    assert v.service.said == []
    # The weakness was told, and is remembered now that a first saying was received.
    async with session_factory() as reader:
        told = await reader.scalar(select(AuditEntry.id).where(AuditEntry.action == WEAKNESS_TOLD))
    assert told is not None


async def test_a_second_saying_that_differs_starts_again(voice, session_factory, owner, live_clock):
    v = voice()
    await a_moment(session_factory, owner, live_clock())
    await v.say(PHRASE)
    await v.say("red foxes run at night")
    moment = await the_moment(session_factory)
    assert moment is not None and moment.step == 0
    assert v.vault.phrase == {}
    await v.say("red foxes run at night")
    await v.say("red foxes run at night")
    assert v.vault.phrase == {phrase_ref(owner.id): "red foxes run at night"}
    assert v.woken.contents() == [
        {"phrase": "first_kept"},
        {"phrase": "not_the_same"},
        {"phrase": "first_kept"},
        {"phrase": "agreed"},
    ]
    assert await v.user_messages() == []


async def test_a_moment_bound_to_another_microphone_takes_nothing_here(
    voice, session_factory, owner, live_clock
):
    v = voice(microphone="kitchen")
    await a_moment(session_factory, owner, live_clock(), microphone=MIC)
    await v.say(PHRASE)
    # Not heard at all: no transcription, no vault, no message; the page is told.
    assert v.service.calls == []
    assert v.vault.seen == []
    assert await v.user_messages() == []
    assert v.page.of("not_taken") == [{"type": "not_taken", "reason": "elsewhere"}]


async def test_a_phrase_moment_opened_by_a_voice_reply_is_that_microphones(
    voice, session_factory, owner
):
    desk = voice(
        FakeChatModel(
            tool_script=[ToolCall("c1", "spoken_phrase_agree", "{}"), "Say it."], judge="do"
        )
    )
    kitchen = voice(house=desk.house, microphone="kitchen")
    await desk.say("I want a phrase.")
    moment = await the_moment(session_factory)
    assert moment is not None and moment.microphone == MIC
    # The kitchen heard the same saying: not taken there, and the page is told.
    await kitchen.say(PHRASE)
    assert kitchen.page.of("not_taken") == [{"type": "not_taken", "reason": "elsewhere"}]
    assert desk.vault.seen == []
    await desk.say(PHRASE)
    assert desk.vault.seen == [PHRASE]
    assert await desk.user_messages() == ["I want a phrase."]


async def test_one_saying_heard_by_two_pages_is_taken_once(
    voice, session_factory, owner, live_clock
):
    desk = voice()
    kitchen = voice(house=desk.house, microphone="kitchen")
    await a_moment(session_factory, owner, live_clock())
    desk.service.text = PHRASE
    before = (desk.session.handled, kitchen.session.handled)
    await desk.session.heard(sound())
    await kitchen.session.heard(sound())
    await until(lambda: desk.session.handled > before[0] and kitchen.session.handled > before[1])
    await desk.house.turns_over()
    # Once to the vault: one page took it, the other was told it was taken elsewhere.
    assert desk.vault.seen == [PHRASE]
    told = desk.page.of("not_taken") + kitchen.page.of("not_taken")
    assert told == [{"type": "not_taken", "reason": "elsewhere"}]
    moment = await the_moment(session_factory)
    assert moment is not None and moment.step == 1
    assert desk.woken.contents() == [{"phrase": "first_kept"}]


async def test_a_vault_out_of_reach_keeps_nothing_and_says_so(
    voice, session_factory, owner, live_clock
):
    v = voice()
    await a_moment(session_factory, owner, live_clock())
    v.vault.unreachable = True  # a transport error, not the gatekeeper's own
    await v.say(PHRASE)
    assert v.woken.contents() == [{"phrase": "not_kept"}]
    moment = await the_moment(session_factory)
    assert moment is not None and moment.step == 0


async def test_a_wake_that_cannot_run_is_told_to_the_page(
    voice, session_factory, owner, live_clock
):
    v = voice()
    await a_moment(session_factory, owner, live_clock())
    v.woken.runs = False  # his conversation stays busy
    await v.say(PHRASE)
    assert v.page.of("failed") == [{"type": "failed", "reason": "not_told"}]


# --- giving it as a proof ---------------------------------------------------------------------


def asks_a_proof() -> FakeChatModel:
    """A model that renames the person, which the judge holds for a proof."""
    return FakeChatModel(
        tool_script=[ToolCall("c1", "profile_rename", '{"name": "Alex B"}'), "Say your phrase."],
        judge="proof: not sure",
    )


async def a_proof_is_asked(v: Voice, session_factory, owner: Person) -> Confirmation:
    """The person agreed a phrase, then asked something the judge holds for a proof, by voice."""
    async with session_factory() as writer:
        await agree(writer, owner.id, "spoken_phrase", proposed_by="person", now=v.clock())
        await writer.commit()
    v.vault.phrase[phrase_ref(owner.id)] = PHRASE
    await v.say("Call me Alex B.")
    async with session_factory() as reader:
        [c] = (await reader.execute(select(Confirmation))).scalars().all()
    assert (c.status, c.ask) == ("pending", "proof")
    return c


async def name_of(session_factory, owner: Person) -> str:
    async with session_factory() as reader:
        return (await reader.get(Person, owner.id, populate_existing=True)).display_name  # type: ignore[union-attr]


async def test_a_phrase_said_right_when_a_proof_is_awaited_is_the_yes_and_is_not_written(
    voice, session_factory, owner, caplog
):
    caplog.set_level(logging.DEBUG)
    v = voice(asks_a_proof())
    c = await a_proof_is_asked(v, session_factory, owner)
    await v.say(PHRASE)
    async with session_factory() as reader:
        status = await reader.scalar(select(Confirmation.status).where(Confirmation.id == c.id))
    assert status != "pending"
    assert await name_of(session_factory, owner) == "Alex B"
    assert await v.user_messages() == ["Call me Alex B."]
    for word in ("horses", "dawn"):
        assert word not in await v.written()
        assert word not in v.model_saw()
        assert word not in caplog.text


async def test_a_near_phrase_is_thrown_away_and_he_is_woken_to_say_so(
    voice, session_factory, owner, caplog
):
    caplog.set_level(logging.DEBUG)
    v = voice(asks_a_proof())
    c = await a_proof_is_asked(v, session_factory, owner)
    await v.say(NEAR)
    assert v.woken.contents() == [{"proof": "did_not_work"}]
    async with session_factory() as reader:
        status = await reader.scalar(select(Confirmation.status).where(Confirmation.id == c.id))
    assert status == "pending"
    assert await v.user_messages() == ["Call me Alex B."]
    for word in ("sleep", "noon"):
        assert word not in await v.written()
        assert word not in v.model_saw()
        assert word not in caplog.text
        assert word not in json.dumps(v.woken.contents())
    # One more try is left in the moment: the phrase said right is the yes.
    await v.say(PHRASE)
    assert await name_of(session_factory, owner) == "Alex B"


class Collected:
    reading = True

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def open(self) -> None:
        return None

    async def emit(self, event: Event) -> None:
        self.events.append(event)


async def test_while_the_phrase_is_held_back_the_window_takes_nothing_by_voice(
    voice, session_factory, owner, live_clock, caplog
):
    caplog.set_level(logging.DEBUG)
    v = voice(asks_a_proof())
    c = await a_proof_is_asked(v, session_factory, owner)
    asked, called = len(v.house.attention.asked), len(v.model.calls)
    v.vault.held_back = 60.0
    await v.say(PHRASE)
    await v.say(ORDINARY)
    # Told once, as a failed try (a bystander cannot tell a held phrase from a wrong one); the
    # second sentence is dropped silently, and not even sent: the hold is remembered.
    assert v.woken.contents() == [{"proof": "did_not_work"}]
    assert v.vault.seen == [PHRASE]
    # Neither reaches a model, nor is written anywhere.
    assert len(v.house.attention.asked) == asked
    assert len(v.model.calls) == called
    assert await v.user_messages() == ["Call me Alex B."]
    for word in ("horses", "dawn", ORDINARY):
        assert word not in await v.written()
        assert word not in v.model_saw()
        assert word not in caplog.text
    # A typed message is untouched.
    typed = page_speaker(owner.id, live_clock())
    conversation = Conversation(v.deps.turn)
    _, reply = await conversation.say(owner, "Turn on the light.", typed)
    assert reply is not None
    await conversation.answer(owner, reply.id, TurnRequest(typed, PAGE), Collected())
    await v.house.turns_over()
    assert await v.user_messages() == ["Call me Alex B.", "Turn on the light."]
    # Once the hold is over, in the same window, the phrase is weighed again: the yes.
    live_clock.advance(timedelta(seconds=61))
    v.vault.held_back = None
    await v.say(PHRASE)
    async with session_factory() as reader:
        status = await reader.scalar(select(Confirmation.status).where(Confirmation.id == c.id))
    assert status != "pending"
    assert await name_of(session_factory, owner) == "Alex B"


@pytest.mark.parametrize(
    ("trouble", "fact"),
    [("held_back", {"proof": "did_not_work"}), ("down", {"proof": "not_checked"})],
    ids=["hold", "outage"],
)
async def test_a_hold_or_outage_whose_wake_wrote_nothing_is_told_a_minute_later(
    voice, session_factory, owner, live_clock, trouble, fact
):
    v = voice(asks_a_proof())
    await a_proof_is_asked(v, session_factory, owner)
    if trouble == "held_back":
        v.vault.held_back = 60.0
    else:
        v.vault.down = True
    v.woken.runs = False  # his conversation stays busy
    await v.say(PHRASE)
    # Not told: not tried again at every sentence, only a minute later at the soonest.
    v.woken.runs = True
    await v.say(ORDINARY)
    assert v.woken.contents() == [fact]
    live_clock.advance(timedelta(seconds=RETRY_TELL_AFTER_S + 1))
    await v.say(ORDINARY)
    assert v.woken.contents() == [fact] * 2
    # Once told, never again in this hold or outage.
    live_clock.advance(timedelta(seconds=5))
    await v.say(ORDINARY)
    assert v.woken.contents() == [fact] * 2


async def test_the_phrase_outcome_is_told_though_the_page_closes(
    voice, session_factory, owner, live_clock, monkeypatch
):
    v = voice()
    await a_moment(session_factory, owner, live_clock())
    entered, let_go = asyncio.Event(), asyncio.Event()
    finished: list[object] = []

    async def slow(person_id: uuid.UUID, wakes: Sequence[Any]) -> str:
        entered.set()
        await let_go.wait()  # his conversation is busy while the page goes
        finished.extend(wake.content for wake in wakes)
        return "spoke"

    monkeypatch.setattr(type(v.woken), "__call__", lambda self, p, w: slow(p, w))
    v.service.text = PHRASE
    await v.session.speech_began()
    await v.session.heard(sound())
    async with asyncio.timeout(10):
        await entered.wait()
    # The page goes while he is being woken: the wake goes on without it.
    await v.session.close()
    let_go.set()
    await v.house.turns_over()
    assert finished == [{"phrase": "first_kept"}]


async def test_with_the_gatekeeper_out_of_reach_he_is_woken_once(voice, session_factory, owner):
    v = voice(asks_a_proof())
    c = await a_proof_is_asked(v, session_factory, owner)
    v.vault.down = True
    await v.say(PHRASE)
    await v.say(ORDINARY)
    # Both dropped (nothing can tell the phrase from talk), the request still waits, and the
    # outage is told once, not once per sentence.
    assert v.woken.contents() == [{"proof": "not_checked"}]
    assert await v.user_messages() == ["Call me Alex B."]
    async with session_factory() as reader:
        status = await reader.scalar(select(Confirmation.status).where(Confirmation.id == c.id))
    assert status == "pending"
    assert "horses" not in await v.written()


async def test_an_ordinary_sentence_while_a_proof_is_awaited_is_a_message(
    voice, session_factory, owner
):
    v = voice(asks_a_proof())
    c = await a_proof_is_asked(v, session_factory, owner)
    await v.say(ORDINARY)
    assert await v.user_messages() == ["Call me Alex B.", ORDINARY]
    assert v.woken.wakes == []
    async with session_factory() as reader:
        status = await reader.scalar(select(Confirmation.status).where(Confirmation.id == c.id))
    assert status == "pending"


async def test_while_a_proof_is_awaited_every_sentence_is_weighed_first(
    voice, session_factory, owner
):
    v = voice(asks_a_proof())
    await a_proof_is_asked(v, session_factory, owner)
    for _ in range(3):
        await v.say(ORDINARY)
    await v.say("what time is it")
    # Each weighed by code first (review 3 of Task 24), then, no answer, its usual way.
    assert v.vault.seen == [ORDINARY, ORDINARY, ORDINARY, "what time is it"]
    assert await v.user_messages() == [
        "Call me Alex B.",
        ORDINARY,
        ORDINARY,
        ORDINARY,
        "what time is it",
    ]
    # However many came before, the phrase is still the proof, and never written.
    await v.say(PHRASE)
    assert await name_of(session_factory, owner) == "Alex B"
    assert "horses" not in await v.written()


async def test_a_proof_asked_at_one_page_opens_no_window_at_another(
    voice, session_factory, owner, live_clock
):
    desk = voice(asks_a_proof())
    kitchen = voice(house=desk.house, microphone="kitchen")
    await a_proof_is_asked(desk, session_factory, owner)
    # Past the copy gap: a new sentence, not a copy of the one the desk took (voice I2).
    live_clock.advance(timedelta(seconds=LATE_COPY_MARGIN_SECONDS + 1))
    # Nothing was said to him in the kitchen: no window there, a sentence is compared with
    # nothing (a limit the controller accepted: a phrase said there would be ordinary words).
    await kitchen.say(ORDINARY)
    assert desk.vault.seen == []
    assert kitchen.page.of("not_taken") == []
    assert await desk.user_messages() == ["Call me Alex B.", ORDINARY]


async def test_after_a_failed_try_the_moment_opens_again(voice, session_factory, owner):
    v = voice(asks_a_proof())
    await a_proof_is_asked(v, session_factory, owner)
    for _ in range(2):
        await v.say(NEAR)
    # A third try, natural after two misses, is weighed too: never in the discussion.
    await v.say(PHRASE)
    assert await name_of(session_factory, owner) == "Alex B"
    assert await v.user_messages() == ["Call me Alex B."]
    assert v.woken.contents() == [{"proof": "did_not_work"}] * 2


async def test_near_misses_are_weighed_as_long_as_the_request_waits(voice, session_factory, owner):
    v = voice(asks_a_proof())
    await a_proof_is_asked(v, session_factory, owner)
    for _ in range(3):
        await v.say(NEAR)
    # Never written; the gatekeeper's own limits bound the tries (held back: not weighed).
    await v.say(ORDINARY)
    assert await v.user_messages() == ["Call me Alex B.", ORDINARY]
    assert v.woken.contents() == [{"proof": "did_not_work"}] * 3
    assert "sleep" not in await v.written()


async def test_a_proof_sentence_is_heard_at_home_when_outside_is_allowed(
    make_voice, session_factory, owner, set_setting
):
    paid = await add_key(
        session_factory, owner, (*CHAT_MODELS, "whisper-1"), home=False, general=True
    )
    home = await add_key(session_factory, owner, (HEAR_MODEL, PRINT_MODEL, SAY_MODEL), home=True)
    await set_setting(HEAR_OUTSIDE_KEY, "yes")
    v = make_voice(asks_a_proof())
    await a_proof_is_asked(v, session_factory, owner)
    assert ("hear", paid.id) in v.service.routes
    v.service.routes.clear()
    await v.say(PHRASE)
    assert [r for r in v.service.routes if r[0] == "hear"] == [("hear", home.id)]
    assert await name_of(session_factory, owner) == "Alex B"


async def test_in_a_window_nothing_is_heard_outside_even_when_nothing_at_home_can(
    make_voice, session_factory, owner, set_setting
):
    await add_key(session_factory, owner, (*CHAT_MODELS, "whisper-1"), home=False, general=True)
    await set_setting(HEAR_OUTSIDE_KEY, "yes")
    v = make_voice(asks_a_proof())
    await a_proof_is_asked(v, session_factory, owner)
    v.service.routes.clear()
    # The window is open: what is said may be the phrase, and is heard at home or not at all.
    await v.say(PHRASE)
    assert [r for r in v.service.routes if r[0] == "hear"] == []
    assert v.page.of("state")[-1] == {"type": "state", "hearing": "unavailable"}
    assert await v.user_messages() == ["Call me Alex B."]
    assert "horses" not in await v.written()


async def test_once_the_window_closed_hearing_follows_the_setting_and_nothing_is_compared(
    make_voice, session_factory, owner, set_setting, live_clock
):
    paid = await add_key(
        session_factory, owner, (*CHAT_MODELS, "whisper-1"), home=False, general=True
    )
    home = await add_key(session_factory, owner, (HEAR_MODEL, PRINT_MODEL, SAY_MODEL), home=True)
    await set_setting(HEAR_OUTSIDE_KEY, "yes")
    v = make_voice(asks_a_proof())
    await a_proof_is_asked(v, session_factory, owner)
    v.service.routes.clear()
    await v.say(ORDINARY)
    # In the window: heard at home, compared first.
    assert [r for r in v.service.routes if r[0] == "hear"] == [("hear", home.id)]
    assert v.vault.seen == [ORDINARY]
    # Ninety seconds after the last reply said here, nothing more said to him: the window is
    # closed, the request alone opens nothing.
    live_clock.advance(timedelta(seconds=session_module.PROOF_WINDOW_S + 1))
    v.service.routes.clear()
    await v.say("what time is it")
    assert [r for r in v.service.routes if r[0] == "hear"] == [("hear", paid.id)]
    assert v.vault.seen == [ORDINARY]
    assert await v.user_messages() == ["Call me Alex B.", ORDINARY, "what time is it"]


async def test_the_phrase_said_with_no_proof_awaited_is_an_ordinary_sentence(
    voice, session_factory, owner
):
    v = voice()
    async with session_factory() as writer:
        await agree(writer, owner.id, "spoken_phrase", proposed_by="person", now=v.clock())
        await writer.commit()
    v.vault.phrase[phrase_ref(owner.id)] = PHRASE
    await v.say(PHRASE)
    assert v.vault.seen == []
    assert await v.user_messages() == [PHRASE]


# --- a late copy of a reserved moment's sentence (ruling, re-review round 2) ------------------


async def test_a_late_copy_after_the_proof_was_given_is_not_conversed(
    voice, session_factory, owner
):
    desk = voice(asks_a_proof())
    kitchen = voice(house=desk.house, microphone="kitchen")
    await a_proof_is_asked(desk, session_factory, owner)
    # The kitchen heard the phrase begin too, and hands it on after the desk gave the proof.
    await kitchen.at.begins()
    await desk.say(PHRASE)
    assert await name_of(session_factory, owner) == "Alex B"
    await kitchen.at.ends(PHRASE)
    assert kitchen.page.of("not_taken") == [{"type": "not_taken", "reason": "elsewhere"}]
    assert await desk.user_messages() == ["Call me Alex B."]
    assert "horses" not in await desk.written()
    assert desk.service.heard == [len(sound())] * 2  # "Call me...", then the desk's phrase


async def test_a_late_copy_after_the_phrase_was_agreed_is_not_conversed(
    voice, session_factory, owner, live_clock
):
    desk = voice()
    kitchen = voice(house=desk.house, microphone="kitchen")
    await a_moment(session_factory, owner, live_clock())
    await desk.say(PHRASE)
    await kitchen.at.begins()
    await desk.say(PHRASE)
    assert await the_moment(session_factory) is None  # agreed: the moment is over
    await kitchen.at.ends(PHRASE)
    assert kitchen.page.of("not_taken") == [{"type": "not_taken", "reason": "elsewhere"}]
    assert await desk.user_messages() == []
    assert desk.vault.seen == [PHRASE, PHRASE]


async def test_a_copy_arriving_while_the_proof_is_weighed_is_not_conversed(
    voice, session_factory, owner, monkeypatch
):
    desk = voice(asks_a_proof())
    kitchen = voice(house=desk.house, microphone="kitchen")
    await a_proof_is_asked(desk, session_factory, owner)
    weigh = session_module.give_the_phrase

    async def slow(*args, **kwargs):
        # While the desk weighs the saying, the kitchen hands on a copy of it.
        await kitchen.at.ends(NEAR)
        return await weigh(*args, **kwargs)

    monkeypatch.setattr(session_module, "give_the_phrase", slow)
    await kitchen.at.begins()
    await desk.say(NEAR)
    assert kitchen.page.of("not_taken") == [{"type": "not_taken", "reason": "elsewhere"}]
    assert await desk.user_messages() == ["Call me Alex B."]
    assert "noon" not in await desk.written()


async def test_a_sentence_begun_after_the_moment_closed_is_conversed(
    voice, session_factory, owner, live_clock
):
    desk = voice(asks_a_proof())
    kitchen = voice(house=desk.house, microphone="kitchen")
    await a_proof_is_asked(desk, session_factory, owner)
    await desk.say(PHRASE)
    live_clock.advance(timedelta(seconds=LATE_COPY_MARGIN_SECONDS + 1))
    await kitchen.say(ORDINARY)
    assert kitchen.page.of("not_taken") == []
    assert await desk.user_messages() == ["Call me Alex B.", ORDINARY]


async def test_a_new_phrase_said_in_its_moment_is_never_weighed_against_the_old_one(
    voice, session_factory, owner
):
    """A proof waits (a window is open here) and the person changes their phrase: what they say
    in that moment goes to the moment, never first compared with the old phrase (a near miss
    would be eaten and spend the gatekeeper's cap)."""
    v = voice(asks_a_proof())
    await a_proof_is_asked(v, session_factory, owner)
    async with session_factory() as writer:
        await open_moment(
            writer, person_id=owner.id, microphone=None, purpose="phrase_change", now=v.clock()
        )
        await writer.commit()
    await v.say(NEAR)
    assert v.vault.pending[phrase_ref(owner.id)] == NEAR
    assert v.vault.seen == [NEAR]
    assert {"proof": "did_not_work"} not in v.woken.contents()


# --- the window is the microphone's, not the socket's (final fix wave, voice I1) -----------------


async def test_a_page_reloaded_within_the_window_still_weighs_the_phrase_first(
    voice, session_factory, owner
):
    v = voice(asks_a_proof())
    c = await a_proof_is_asked(v, session_factory, owner)
    # The socket drops (a reload, a reconnect): a new page of the same microphone opens.
    first = v.house.pages.pop()
    await first.session.close()
    again = v.house.page(MIC)
    await again.hears(PHRASE)
    async with session_factory() as reader:
        status = await reader.scalar(select(Confirmation.status).where(Confirmation.id == c.id))
    assert status != "pending"
    assert await v.user_messages() == ["Call me Alex B."]
    assert "horses" not in await v.written()
    assert "horses" not in v.model_saw()


# --- one sentence heard by two pages is taken once (final fix wave, voice I2) ---------------------


async def test_an_ordinary_sentence_heard_by_two_pages_is_taken_once(voice, session_factory):
    desk = voice(FakeChatModel(["Added."]))
    kitchen = voice(house=desk.house, microphone="kitchen")
    desk.service.text = "add milk to the list"
    before = (desk.session.handled, kitchen.session.handled)
    await desk.at.begins()
    await kitchen.at.begins()
    await desk.session.heard(sound())
    await kitchen.session.heard(sound())
    await until(lambda: desk.session.handled > before[0] and kitchen.session.handled > before[1])
    await desk.house.turns_over()
    assert await desk.user_messages() == ["add milk to the list"]
    replies = [m for m in await desk.house.messages() if m[0] == "assistant"]
    assert replies == [("assistant", "Added.", "complete")]
    # The copy is dropped silently: no page is told anything of it.
    assert desk.page.of("not_taken") + kitchen.page.of("not_taken") == []
