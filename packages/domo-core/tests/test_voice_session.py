"""The voice session (spec 6.1, 6.7, 6.11): what is heard becomes the person's message, the reply is
said aloud, and the page is told what it must show."""

import asyncio
import uuid
from collections.abc import Awaitable, Callable

import pytest
from domo_core.accounts.models import Person
from domo_core.channels.page.abilities import PAGE
from domo_core.channels.voice import session as session_module
from domo_core.channels.voice.live import LIVE_VOICES
from domo_core.channels.voice.models import VoiceMoment, VoicePrint, VoiceRef
from domo_core.channels.voice.session import VoiceSession
from domo_core.config import CoreSettings
from domo_core.conversation import service as conversation_service
from domo_core.conversation.events import Ended, Words
from domo_core.conversation.listeners import Listeners
from domo_core.conversation.prepare import NoKey
from domo_core.conversation.speaker import Speaker, standing
from domo_core.deliveries.models import Delivery
from domo_core.llm.speech.at_home import AtHome
from domo_core.llm.speech.client import SpeechError
from domo_core.llm.spending_models import ModelCall
from domo_core.llm.types import ToolCall
from domo_core.policy.catalog import DEFAULT_POLICY
from domo_core.policy.roles import Actor
from domo_core.tools.context import ToolContext
from domo_core.tools.registry import TOOLS
from domo_core.web.app import create_app
from fakes import FakeChatModel
from sqlalchemy import select
from voice_session_support import (
    CHAT_MODELS,
    FakePage,
    FakeSpeechService,
    House,
    a_household_that_hears_at_home,
    add_key,
    messages_of,
    sound,
    until,
)

MIC = "desk-1"
ORIGIN = "https://domo.example.com/"


@pytest.fixture
async def owner(add_member: Callable[..., Awaitable[Person]]) -> Person:
    return await add_member("Alex", "owner")


class Voice:
    """A page of the owner (``microphone``), with the household it stands on (``house``: a new
    one, or a page more of another's)."""

    def __init__(
        self,
        session_factory,
        settings: CoreSettings,
        clock,
        owner: Person,
        model: FakeChatModel,
        *,
        listeners: Listeners | None = None,
        microphone: str = MIC,
        house: House | None = None,
    ) -> None:
        self.house = house or House(
            session_factory, settings, clock, owner, model, listeners=listeners
        )
        self.at = self.house.page(microphone)
        self.session = self.at.session
        self.page = self.at.socket
        self.service = self.house.service
        self.vault = self.house.vault
        self.woken = self.house.woken
        self.model = self.house.model
        self.deps = self.house.deps

    async def say(self, text: str, *, seconds: float = 2.0, taken: bool = True) -> None:
        # The fake service reads ``text`` when the worker gets to the sound, not when it is
        # handed on: wait until the sentence was taken up (heard, or given up) before the next
        # one may change the text.
        before = (len(self.service.heard), self.session.handled)
        self.service.text = text
        await self.session.heard(sound(seconds))
        if taken:
            await until(lambda: (len(self.service.heard), self.session.handled) != before)

    async def turns_over(self) -> None:
        await self.house.turns_over()

    async def messages(self) -> list[tuple[str, str, str]]:
        return await self.house.messages()


@pytest.fixture
async def voice(session_factory, settings, live_clock, owner):
    await a_household_that_hears_at_home(session_factory, owner)
    made: list[Voice] = []

    def make(model: FakeChatModel | None = None, **others) -> Voice:
        found = Voice(
            session_factory,
            settings,
            live_clock,
            owner,
            model or FakeChatModel(["Of course."]),
            **others,
        )
        made.append(found)
        return found

    yield make
    for each in made:
        await each.house.close()


async def test_an_utterance_becomes_a_message_and_a_reply(voice, session_factory):
    v = voice()
    await v.say("Turn on the light.")
    await until(lambda: bool(v.page.of("say_end")))
    await v.turns_over()
    assert await v.messages() == [
        ("user", "Turn on the light.", "complete"),
        ("assistant", "Of course.", "complete"),
    ]
    assert v.service.said == ["Of course."]
    # The model was asked the words as the person's own, said through the voice.
    [call] = v.model.calls
    assert "Turn on the light." in repr(call.messages)
    # It reached the person on the voice: the speaker's channel.
    async with session_factory() as reader:
        channels = (await reader.execute(select(Delivery.channel))).scalars().all()
    assert channels == ["voice"]


async def test_a_service_still_preparing_tells_the_page_and_says_nothing(voice):
    v = voice()
    v.service.hear_fails = SpeechError("preparing")
    await v.say("Turn on the light.")
    await until(lambda: bool(v.page.of("state")))
    assert v.page.of("state") == [{"type": "state", "hearing": "preparing"}]
    assert await v.messages() == []
    assert "say" not in v.service.calls
    # Ready again: the page is told, and the words go on.
    v.service.hear_fails = None
    await v.say("Turn on the light.")
    await until(lambda: bool(v.page.of("say_end")))
    assert v.page.of("state")[-1] == {"type": "state", "hearing": "ready"}


async def test_no_model_at_home_turns_the_voice_off_and_tells_the_owner_once(
    session_factory, settings, live_clock, owner
):
    # Only a paid key that hears, and the default setting: what is heard may not go there.
    await add_key(session_factory, owner, (*CHAT_MODELS, "whisper-1"), home=False, general=True)
    v = Voice(session_factory, settings, live_clock, owner, FakeChatModel(["ok"]))
    try:
        await v.say("Turn on the light.")
        await until(lambda: bool(v.page.of("listening")))
        await v.say("Hello?")
        await until(lambda: len(v.page.of("listening")) == 2)
        assert v.page.of("state")[0] == {"type": "state", "hearing": "unavailable"}
        assert v.page.of("listening")[0] == {"type": "listening", "on": False}
        await v.turns_over()
        # Told once, in the owner's conversation, as a fact (his words say it).
        assert v.woken.wakes and [w[0] for w in v.woken.wakes] == [owner.id]
        assert v.woken.contents() == [{"hearing": "no_model", "outside_allowed": False}]
        # The channel's own fact: given by the house's code, never as a tool of a name.
        assert [w.given_by for _, wakes in v.woken.wakes for w in wakes] == ["channel"]
        assert await v.messages() == []
    finally:
        await v.house.close()


async def test_utterances_during_a_reply_are_all_kept_and_answered_after(voice):
    v = voice(FakeChatModel(["One", " two", " three."], delay_s=0.4))
    await v.say("First.")
    await until(lambda: len(v.model.calls) == 1)
    # Two more while the reply is written: both are kept (spec 11.3), none is lost.
    await v.say("Second.")
    await v.say("Third.")
    await until(lambda: len(v.model.calls) == 2)
    await v.turns_over()
    said = [m for m in await v.messages() if m[0] == "user"]
    assert [text for _, text, _ in said] == ["First.", "Second.", "Third."]


async def test_a_sure_utterance_teaches_the_voice(voice, session_factory, owner):
    v = voice()
    await v.say("Turn on the light.")
    await until(lambda: bool(v.page.of("say_end")))
    async with session_factory() as reader:
        kinds = (
            await reader.execute(
                select(VoicePrint.kind, VoicePrint.microphone)
                .join(VoiceRef, VoiceRef.id == VoicePrint.ref_id)
                .where(VoiceRef.person_id == owner.id)
            )
        ).all()
    assert kinds == [("candidate", MIC)]
    # The reading is kept on the session for the clues of Part 2.
    assert v.session.last_voice is not None
    assert v.session.last_voice.microphone == MIC


async def test_stop_listening_turns_the_asking_page_off_at_once(voice, session_factory):
    model = FakeChatModel(
        tool_script=[ToolCall("c1", "voice_stop_listening", "{}"), "I stop now."], delay_s=0.3
    )
    desk = voice(model)
    kitchen = voice(house=desk.house, microphone="kitchen")
    await desk.say("Stop listening now.")
    await until(lambda: bool(desk.page.of("listening")))
    # At once: before the reply's words are said, and only on the page that asked.
    assert desk.page.of("listening") == [{"type": "listening", "on": False}]
    assert desk.page.of("say_end") == []
    await until(lambda: bool(desk.page.of("say_end")))
    assert kitchen.page.of("listening") == []
    async with session_factory() as reader:
        assert (await reader.execute(select(VoiceMoment))).first() is None


async def test_a_stop_asked_by_a_typed_message_turns_every_listening_page_off_at_once(
    voice, session_factory, owner, live_clock
):
    desk = voice()
    kitchen = voice(house=desk.house, microphone="kitchen")
    ctx = ToolContext(
        session_factory=session_factory,
        actor=Actor(owner.id, "owner"),
        person=owner,
        conversation_id=uuid.uuid4(),
        origin="",
        gatekeeper=desk.vault,  # type: ignore[arg-type]
        clock=live_clock,
        registry=TOOLS,
        policy=DEFAULT_POLICY,
        reply_message_id=uuid.uuid4(),
        domo_message_id=uuid.uuid4(),  # the page's reply: no voice says it
        abilities=PAGE,
    )
    outcome = await TOOLS["voice_stop_listening"].execute({}, ctx)
    assert outcome.status == "done"
    for page in (desk, kitchen):
        assert page.page.of("listening") == [{"type": "listening", "on": False}]
    # Nothing is left to act on later: no sentence is needed.
    async with session_factory() as reader:
        assert (await reader.execute(select(VoiceMoment))).first() is None


async def test_a_failure_while_following_a_reply_never_fails_the_reply(voice, monkeypatch):
    async def broken(self: object, event: object) -> None:
        raise RuntimeError("the database is gone")

    monkeypatch.setattr(VoiceSession, "_moment_asked", broken)
    v = voice(FakeChatModel(tool_script=[ToolCall("c1", "household_list", "{}"), "Here."]))
    await v.say("Who is here?")
    await until(lambda: bool(v.page.of("say_end")))
    await v.turns_over()
    assert ("assistant", "Here.", "complete") in await v.messages()


async def test_words_said_while_a_reply_is_written_are_kept_for_it(voice):
    # Spec 11.3 (master's "a message sent while he replies is kept"): no reply of their own is
    # opened here and nothing is refused; the reply that reads them says what answers them.
    v = voice(FakeChatModel(["One", " two", " three."], delay_s=0.4))
    await v.say("First.")
    await until(lambda: len(v.model.calls) == 1)
    await v.say("Second.")
    await until(lambda: len(v.model.calls) == 2)
    await v.turns_over()
    assert await v.house.said() == ["First.", "Second."]
    assert v.page.of("not_taken") == []


async def test_a_failure_of_the_house_is_told_to_the_page(voice, monkeypatch):
    async def broken(*args: object, **kwargs: object) -> object:
        raise RuntimeError("the database is gone")

    monkeypatch.setattr(session_module, "moment_of", broken)
    v = voice()
    await v.say("Turn on the light.")
    await until(lambda: bool(v.page.of("failed")))
    assert v.page.of("failed") == [{"type": "failed", "reason": "failed"}]


async def test_a_reply_no_turn_says_leaves_no_listener(voice, monkeypatch):
    async def claimed(*args: object, **kwargs: object) -> bool:
        return False

    monkeypatch.setattr(conversation_service.Conversation, "answer", claimed)
    v = voice()
    await v.at.hears("Turn on the light.")
    kept = await messages_of(v.house.session_factory, v.house.person)
    assert [m.role for m in kept] == ["user", "assistant"]
    assert not v.session.says(kept[-1].id)


async def test_the_reading_is_of_the_last_sentence(voice):
    v = voice()
    await v.at.hears("Turn on the light.")
    assert v.session.last_voice is not None
    v.service.print_fails = SpeechError("unavailable")
    await v.at.hears("And the radio.")
    assert v.session.last_voice is None


async def test_the_words_are_said_with_the_speaker_of_their_voice(voice, monkeypatch, owner):
    said: list[Speaker] = []
    say = conversation_service.Conversation.say

    async def watched(self, person, text, speaker, request=None):
        said.append(speaker)
        return await say(self, person, text, speaker, request)

    monkeypatch.setattr(conversation_service.Conversation, "say", watched)
    v = voice()
    await v.at.hears("Turn on the light.")
    v.service.print_fails = SpeechError("unavailable")
    await v.at.hears("And the radio.")
    heard, no_print = said
    # A voice not learnt yet: the page's person, sure by the page; the microphone that heard it.
    assert heard.person_id == owner.id
    assert heard.channel == "voice"
    assert heard.microphone == MIC
    assert [(c.kind, c.weighs) for c in heard.clues] == [("session", "for"), ("voice", "unknown")]
    assert standing(heard) == "sure"
    # No print: the voice says nothing of who speaks, so it is too short to tell (ruling T22):
    # sure for what is ordinary, unsure for what is sensitive.
    assert [(c.kind, c.weighs) for c in no_print.clues] == [
        ("session", "for"),
        ("voice", "unknown"),
    ]
    assert standing(no_print) == "sure"
    assert no_print.voice_too_short


async def test_what_is_not_sayable_is_dropped(voice):
    v = voice()
    await v.say("word " * 2000)
    await v.say("Turn on the light.")
    await until(lambda: bool(v.page.of("say_end")))
    await v.turns_over()
    assert [m[1] for m in await v.messages() if m[0] == "user"] == ["Turn on the light."]


async def test_no_key_is_shown_by_the_page_and_nothing_is_said(voice, monkeypatch):
    async def no_key(*args: object, **kwargs: object) -> object:
        raise NoKey

    monkeypatch.setattr(conversation_service.Conversation, "say", no_key)
    v = voice()
    await v.say("Turn on the light.")
    await until(lambda: bool(v.page.of("failed")))
    assert v.page.of("failed") == [{"type": "failed", "reason": "no_key"}]
    assert "say" not in v.service.calls


async def test_the_session_holds_no_sound_after_handling(voice):
    v = voice()
    await v.say("Turn on the light.")
    await until(lambda: bool(v.page.of("say_end")))
    assert not v.session.holds_sound
    await v.session.close()
    assert not v.session.holds_sound


async def test_each_call_of_sound_is_counted(voice, session_factory):
    v = voice()
    await v.say("Turn on the light.")
    await until(lambda: bool(v.page.of("say_end")))
    await v.turns_over()
    async with session_factory() as reader:
        tasks = (await reader.execute(select(ModelCall.task))).scalars().all()
    speech = sorted(task for task in tasks if task in {"hear", "voice_print", "say"})
    assert speech == ["hear", "say", "voice_print"]


async def test_the_answer_is_said_in_the_language_heard(voice):
    v = voice(FakeChatModel(["Bien sûr."]))
    v.service.language = "fr"
    await v.say("Allume la lumière.")
    await until(lambda: bool(v.page.of("say_end")))
    assert v.service.languages == ["fr"]
    # The turn is told the language heard, so that it answers in it (before the person's own).
    [call] = v.model.calls
    assert "This person reads French (fr)" in call.system


async def test_what_he_says_on_his_own_is_said_aloud(voice, owner):
    listeners = Listeners()
    v = voice(listeners=listeners)
    reply = uuid.uuid4()
    await listeners.say(owner.id, reply, Words("Your parcel came."))
    await listeners.say(owner.id, reply, Ended("complete"))
    assert v.service.said == ["Your parcel came."]
    assert listeners.hearing(owner.id)
    await v.session.close()
    assert not listeners.hearing(owner.id)


async def test_a_page_that_leaves_ends_the_session(voice):
    v = voice()
    await v.session.close()
    await v.say("Turn on the light.", taken=False)  # a closed session takes nothing up
    await asyncio.sleep(0.1)
    assert v.service.calls == []


async def test_the_lifespan_serves_the_voice(settings, owner):
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        assert isinstance(app.state.at_home, AtHome)
        assert app.state.live_voices is LIVE_VOICES
        app.state.speech = FakeSpeechService()
        opened = [
            await app.state.voice_sessions(
                person_id=owner.id,
                microphone=microphone,
                origin=ORIGIN,
                sink=FakePage(),
                signed_in_at=None,
            )
            for microphone in ("desk-1", "kitchen")
        ]
        try:
            for session in opened:
                assert isinstance(session, VoiceSession)
                # One shared check of where a key leads, the one registry, the account page's
                # count of password checks, and a turn's dependencies.
                assert session.deps.speech.at_home is app.state.at_home
                assert session.deps.throttle is app.state.password_check_throttle
                assert session.deps.turn.listeners is app.state.listeners
                assert session.deps.turn.wakes is app.state.wakes
        finally:
            for session in opened:
                await session.close()


async def test_a_voice_that_cannot_be_learnt_from_loses_no_words(voice, monkeypatch):
    async def broken(*args: object, **kwargs: object) -> object:
        raise RuntimeError("the database is gone")

    monkeypatch.setattr(session_module, "learn", broken)
    v = voice()
    await v.say("Turn on the light.")
    await until(lambda: bool(v.page.of("say_end")))
    assert ("user", "Turn on the light.", "complete") in await v.messages()
