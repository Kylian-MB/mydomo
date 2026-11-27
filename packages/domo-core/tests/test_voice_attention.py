"""Is this for him? Should he stop? (spec 6.6, Task 25): a quick judgement of a small model at home
weighs each sentence heard; no word is looked for in the code. And words said while a reply is
being written are kept, and what answers them is said aloud, once, where they were heard (ruling
T25 of the Task 19 review)."""

import ast
import asyncio
import re
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

import domo_core
import pytest
from conversation_support import request_of
from domo_core.accounts.models import Person
from domo_core.channels.page.abilities import PAGE
from domo_core.channels.voice import attention as attention_module
from domo_core.channels.voice import session as session_module
from domo_core.channels.voice.attention import (
    Exchanged,
    Judged,
    Moment,
    fallback,
    read_judgement,
)
from domo_core.channels.voice.live import LATE_COPY_MARGIN_SECONDS
from domo_core.channels.voice.proof import proofs_awaited
from domo_core.confirmations.models import Confirmation
from domo_core.conversation.events import Ended, Event, Words
from domo_core.conversation.service import Conversation
from domo_core.conversation.speaker import Speaker
from domo_core.llm.models import Provider
from domo_core.llm.spending_models import ModelCall
from domo_core.llm.types import (
    ChatChunk,
    ChatUsage,
    HistoryItem,
    ToolCall,
    ToolChoice,
    ToolDefinition,
)
from domo_core.policy.settings import VOICE_GESTURE_KEY, update_policy_setting
from domo_core.proofs.kinds import PROOF_KINDS, SpokenPhraseProof, phrase_ref
from domo_core.proofs.service import agree
from fakes import FakeChatModel, is_judge_call
from sqlalchemy import select
from voice_session_support import (
    CHAT_MODELS,
    HEAR_MODEL,
    HOME_CHAT_MODEL,
    PRINT_MODEL,
    SAY_MODEL,
    House,
    a_household_that_hears_at_home,
    add_key,
    sound,
    until,
)

MIC = "desk-1"
VOICE_DIR = Path(domo_core.__file__).parent / "channels" / "voice"


@pytest.fixture(autouse=True)
def no_phrase_waits():
    kind = PROOF_KINDS["spoken_phrase"]
    assert isinstance(kind, SpokenPhraseProof)
    kind.forget_waits()
    yield
    kind.forget_waits()


@pytest.fixture
async def owner(add_member: Callable[..., Awaitable[Person]]) -> Person:
    return await add_member("Alex", "owner")


@pytest.fixture
async def house(session_factory, settings, live_clock, owner):
    made: list[House] = []

    async def make(model: Any = None, *, keys: bool = True, **others: Any) -> House:
        if keys:
            await a_household_that_hears_at_home(session_factory, owner)
        found = House(
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
        await each.close()


async def all_said(h: House) -> list[str]:
    return [text for role, text, _ in await h.messages() if role == "user"]


async def heard_now(page: Any, text: str) -> None:
    """``text`` heard at ``page`` and handled, without waiting for the turns it starts."""
    before = page.session.handled
    page.house.service.text = text
    await page.session.speech_began()
    await page.session.heard(sound())
    await until(lambda: page.session.handled > before)


# --- the quick judgement --------------------------------------------------------------------


async def test_a_sentence_for_him_while_he_speaks_stops_him_and_becomes_the_next_message(house):
    h = await house(FakeChatModel(tool_script=["The weather is fine today.", "The kitchen, then."]))
    page = h.page(MIC)
    await page.hears("How is the weather?")
    assert page.of("say_end")
    assert page.session.speaking  # sent, and the page did not say it was played
    h.attention.answer = "mine stop"
    await page.hears("Wait, turn on the kitchen light instead.")
    # Weighed with where he speaks; told to stop; the sentence is the next message, never refused.
    assert h.attention.asked[-1]["speaking_at"] == [{"microphone": MIC}]
    assert h.attention.asked[-1]["sentence"] == "Wait, turn on the kitchen light instead."
    assert page.of("stop") == [{"type": "stop"}]
    assert await all_said(h) == ["How is the weather?", "Wait, turn on the kitchen light instead."]
    assert h.service.said[-1] == "The kitchen, then."


async def test_talk_between_people_while_he_speaks_lets_him_go_on(house):
    h = await house(FakeChatModel(["The weather is fine today."]))
    page = h.page(MIC)
    await page.hears("How is the weather?")
    h.attention.answer = "not_mine resume"
    await page.hears("Did you feed the cat?")
    assert page.of("stop") == []
    assert page.of("resume") == [{"type": "resume"}]
    assert page.session.speaking
    # Not his: in no discussion, asked of no model of the conversation.
    assert await all_said(h) == ["How is the weather?"]
    assert len(h.model.calls) == 1


async def test_a_voice_from_another_room_does_not_stop_him(house):
    h = await house(FakeChatModel(["The weather is fine today."]))
    bedroom = h.page("bedroom")
    living = h.page("living-room")
    await bedroom.hears("How is the weather?")
    assert bedroom.session.speaking and not living.session.speaking
    # A new sentence, past the copy gap (not a copy of the bedroom's: final fix wave, voice I2).
    h.clock.advance(timedelta(seconds=LATE_COPY_MARGIN_SECONDS + 1))  # type: ignore[attr-defined]
    # Even a model that says to stop: only the microphone that heard it is quieted, never another.
    h.attention.answer = "mine stop"
    await living.hears("Hey, turn on the light.")
    assert h.attention.asked[-1]["microphone"] == "living-room"
    assert h.attention.asked[-1]["speaking_at"] == [{"microphone": "bedroom"}]
    assert bedroom.of("stop") == []
    assert bedroom.session.speaking
    # The sentence was for him: the next message, answered where it was heard.
    assert await all_said(h) == ["How is the weather?", "Hey, turn on the light."]


async def test_a_sentence_not_for_him_that_stops_him_enters_no_discussion(house):
    h = await house(FakeChatModel(["The weather is fine today."]))
    page = h.page(MIC)
    await page.hears("How is the weather?")
    h.attention.answer = "not_mine stop"
    await page.hears("Stop that, Tom.")
    assert page.of("stop") == [{"type": "stop"}]
    assert await all_said(h) == ["How is the weather?"]
    assert len(h.model.calls) == 1
    assert len(page.session._unaddressed) == 1  # noqa: SLF001 - in memory only


async def test_a_reply_dropped_while_written_ends_cleanly(house):
    model = _Gated("Let me look at who is here.\n", LOOK, FakeChatModel(["Nobody else."]))
    h = await house(model)
    page = h.page(MIC)
    await heard_now(page, "Who is here?")
    await until(lambda: bool(page.of("say_end")))
    h.attention.answer = "mine drop"
    await heard_now(page, "Be quiet.")
    assert page.of("stop")
    model.gate.set()
    await h.turns_over()
    # Stopped for good, its turn over: nothing of it is kept waiting, nothing more said.
    assert h.service.said == ["Let me look at who is here."]
    assert not page.session._replies  # noqa: SLF001
    assert not page.session._hushed  # noqa: SLF001
    assert await all_said(h) == ["Who is here?"]


async def test_a_proof_awaited_of_someone_not_spoken_to_here_leaves_this_room_weighed(
    house, add_member, monkeypatch
):
    paul = await add_member("Paul", "child")
    h = await house()
    living = h.page("living-room")
    # A proof by his phrase is awaited of Paul, but no reply to him was said in the living room:
    # no window there; Alex's voice talks, and is weighed as usual, compared with nothing.
    awaited = {paul.id: [uuid.uuid4()]}

    async def proofs_awaited(deps: Any, person: Any, now: Any) -> list[uuid.UUID]:
        return awaited.get(person.id, [])

    monkeypatch.setattr(session_module, "proofs_awaited", proofs_awaited)
    h.attention.answer = "not_mine"
    await living.hears("Do you want to play outside?")
    assert h.vault.seen == []
    assert h.attention.asked[-1]["sentence"] == "Do you want to play outside?"
    assert await h.messages() == []
    assert h.model.calls == []


async def test_a_sentence_that_is_no_answer_to_the_moment_is_weighed_then(
    house, session_factory, owner, live_clock, monkeypatch
):
    model = FakeChatModel(
        tool_script=[ToolCall("c1", "profile_rename", '{"name": "Alex B"}'), "Say your phrase."],
        judge="proof: not sure",
    )
    h = await house(model)
    async with session_factory() as writer:
        await agree(writer, owner.id, "spoken_phrase", proposed_by="person", now=live_clock())
        await writer.commit()
    h.vault.phrase[phrase_ref(owner.id)] = "blue horses sing at dawn"
    page = h.page(MIC)
    await page.hears("Call me Alex B.")
    assert await proofs_awaited(h.deps, owner, live_clock())
    asked = len(h.attention.asked)
    h.attention.answer = "not_mine"
    learnt: list[bool] = []
    real_learn = session_module.learn

    async def learn(*args: Any, **kwargs: Any) -> Any:
        learnt.append(kwargs["sure_otherwise"])
        return await real_learn(*args, **kwargs)

    monkeypatch.setattr(session_module, "learn", learn)
    await page.hears("Did you feed the cat?")
    # Not for him: its voice teaches nothing as sure.
    assert learnt == [False]
    # Weighed by the code first (never by a model), then, no answer, by the quick judgement.
    assert h.vault.seen == ["Did you feed the cat?"]
    assert [m["sentence"] for m in h.attention.asked[asked:]] == ["Did you feed the cat?"]
    assert "Did you feed the cat?" not in await all_said(h)


@pytest.mark.parametrize("taken_for", ["nobody", "the_page_person"])
async def test_a_copy_of_a_phrase_heard_in_another_room_reaches_nothing(
    taken_for, session_factory, settings, live_clock, owner, add_member, monkeypatch
):
    phrase = "blue horses sing at dawn"
    paul = await add_member("Paul", "child")
    # Alex's own paid key; the house's speech service and small model are everyone's.
    await add_key(session_factory, owner, CHAT_MODELS, home=False)
    await add_key(
        session_factory,
        owner,
        (HEAR_MODEL, PRINT_MODEL, SAY_MODEL, HOME_CHAT_MODEL),
        home=True,
        general=True,
    )
    model = FakeChatModel(
        tool_script=[ToolCall("c1", "profile_rename", '{"name": "Alex B"}'), "Say your phrase."],
        judge="proof: not sure",
    )
    alex = House(session_factory, settings, live_clock, owner, model)
    pauls = House(session_factory, settings, live_clock, paul, FakeChatModel(["ok"]))
    if taken_for == "nobody":
        real = session_module.voice_speaker

        def heard_as(**kwargs: Any) -> Speaker:
            found = real(**kwargs)
            if kwargs["microphone"] != "living-room":
                return found
            return Speaker(None, "voice", clues=(), microphone="living-room")

        monkeypatch.setattr(session_module, "voice_speaker", heard_as)
    try:
        async with session_factory() as writer:
            await agree(writer, owner.id, "spoken_phrase", proposed_by="person", now=live_clock())
            await writer.commit()
        alex.vault.phrase[phrase_ref(owner.id)] = phrase
        bedroom = alex.page("bedroom")
        living = pauls.page("living-room")
        await bedroom.hears("Call me Alex B.")
        assert await proofs_awaited(alex.deps, owner, live_clock())
        # Alex says his phrase in the bedroom; the living room hears it too, at the same time.
        await living.session.speech_began()
        await bedroom.hears(phrase)
        before = living.session.handled
        pauls.service.text = phrase
        await living.session.heard(sound())
        await until(lambda: living.session.handled > before)
        await pauls.turns_over()
        # The copy is never transcribed, never weighed by a model, kept nowhere, written nowhere.
        assert pauls.service.heard == []
        assert all(m["sentence"] != phrase for m in [*pauls.attention.asked, *alex.attention.asked])
        assert len(living.session._unaddressed) == 0  # noqa: SLF001
        assert await pauls.messages() == []
        assert living.of("not_taken") == [{"type": "not_taken", "reason": "elsewhere"}]
        # Later, other talk in the living room is weighed as usual.
        live_clock.advance(timedelta(seconds=10))
        pauls.attention.answer = "not_mine"
        await living.hears("Shall we play outside?")
        assert pauls.attention.asked[-1]["sentence"] == "Shall we play outside?"
    finally:
        await alex.close()
        await pauls.close()


async def test_a_slow_attention_never_silences_him(house, monkeypatch):
    monkeypatch.setattr(attention_module, "ATTENTION_SECONDS", 0.05)
    h = await house(FakeChatModel(["The weather is fine today."]))
    page = h.page(MIC)
    await page.hears("How is the weather?")
    h.attention.answer = "mine stop"
    h.attention.delay_s = 1.0
    await page.hears("And tomorrow?")
    # Too late to be read: he goes on, and the sentence is his.
    assert page.of("stop") == []
    assert page.of("resume") == [{"type": "resume"}]
    assert await all_said(h) == ["How is the weather?", "And tomorrow?"]


def test_an_unreadable_or_missing_judgement_takes_the_sentence_for_him():
    speaking = Moment("Hello", "Alex", "sure", MIC, (Exchanged("person", "Hi"),), (MIC,))
    silent = Moment("Hello", "Alex", "sure", MIC, (), ())
    assert read_judgement("I think so", speaking) is None
    assert fallback(speaking) == Judged(True, "resume")
    assert fallback(silent).for_him
    assert read_judgement("mine", speaking) == Judged(True, "resume")
    assert read_judgement("Not_mine, DROP.", speaking) == Judged(False, "drop")
    assert read_judgement("not_mine", silent) == Judged(False, None)
    # What was said never reaches a log by a repr.
    assert "Hello" not in repr(speaking) and "Hi" not in repr(speaking)


# Words a person says to make the assistant go quiet, in the languages of the house: none of
# them is looked for in the code (the model understands them).
_SPOKEN_STOPS = (
    "tais",
    "arrête",
    "arrete",
    "attends",
    "chut",
    "silence",
    "shut up",
    "be quiet",
    "hush",
    "hold on",
    "enough",
    "wait",
)
# The words of the model's answer: data of the answer, not words looked for in what is heard.
_ANSWER_WORDS = {"mine", "not_mine", "stop", "resume", "drop"}


def _literals(path: Path) -> list[str]:
    """The string literals of a module, docstrings left out."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    documented = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in documented
    ]


def test_no_word_list_exists_in_the_code():
    for name in ("attention.py", "session.py", "unaddressed.py"):
        for literal in _literals(VOICE_DIR / name):
            lowered = literal.lower()
            if len(lowered.split()) > 2:
                continue  # a sentence of a log, not a word looked for
            for word in _SPOKEN_STOPS:
                assert not re.search(rf"\b{re.escape(word)}\b", lowered), (name, literal)
            # A word alone that holds "stop" is only ever the answer's own word.
            if "stop" in lowered and " " not in lowered:
                assert lowered in _ANSWER_WORDS, (name, literal)


async def test_attention_stays_at_home_by_default(house, session_factory):
    h = await house()
    page = h.page(MIC)
    await page.hears("Turn on the light.")
    async with session_factory() as reader:
        rows = (
            await reader.execute(
                select(Provider.secret_ref)
                .join(ModelCall, ModelCall.provider_id == Provider.id)
                .where(ModelCall.task == "attention")
            )
        ).all()
    # Weighed once, on the house's own model (a key with no secret: at home), never the paid one.
    assert rows == [(None,)]
    assert h.attention.asked[0]["sentence"] == "Turn on the light."


async def test_without_a_local_model_the_voice_stays_off_and_the_owner_is_told_once(
    house, session_factory, owner, live_clock
):
    # A paid key that thinks, the speech service at home, and no small model at home.
    await add_key(session_factory, owner, CHAT_MODELS, home=False, general=True)
    await add_key(session_factory, owner, (HEAR_MODEL, PRINT_MODEL, SAY_MODEL), home=True)
    h = await house(keys=False)
    page = h.page(MIC)
    # Told as the session opens, before any sound was taken.
    await until(lambda: bool(page.of("listening")))
    assert h.service.heard == []
    await page.hears("Turn on the light.")
    await page.hears("Hello?")
    # Told as the session opens, before any sound, then again for each sentence that came.
    assert page.of("listening") and set(map(str, page.of("listening"))) == {
        str({"type": "listening", "on": False})
    }
    assert page.socket.messages()[0] == {"type": "state", "hearing": "unavailable"}
    assert h.attention.asked == []
    assert await h.messages() == []
    # Told once, in his own conversation, as a fact with the choices he has (his words say it).
    assert [w[0] for w in h.woken.wakes] == [owner.id]
    assert h.woken.contents() == [
        {
            "hearing": "no_attention",
            "outside_allowed": False,
            "choices": ["speech.hear_outside", VOICE_GESTURE_KEY],
        }
    ]
    # He chose: only after a gesture. The page is told, and a sentence said with it is his.
    async with session_factory() as writer:
        await update_policy_setting(
            writer, key=VOICE_GESTURE_KEY, value="yes", by=owner.id, now=live_clock()
        )
        await writer.commit()
    # Two sentences begun before the page could be told: the first tells it, the second (begun
    # before that) is not even transcribed.
    heard_before = len(h.service.heard)
    await page.session.speech_began()
    handled = page.session.handled
    h.service.text = "Can you hear me?"
    await page.session.heard(sound())
    live_clock.advance(timedelta(seconds=1))
    await page.session.heard(sound())
    await until(lambda: page.session.handled == handled + 2)
    assert len(h.service.heard) == heard_before + 1
    assert page.of("state")[-1] == {"type": "state", "hearing": "gesture"}
    live_clock.advance(timedelta(seconds=1))
    assert await h.messages() == []
    await page.begins()
    await page.ends("Turn on the light.")
    assert await all_said(h) == ["Turn on the light."]
    assert len(h.woken.wakes) == 1
    # A page that opens now is told at once to listen only with the gesture.
    kitchen = h.page("kitchen")
    await until(lambda: bool(kitchen.of("state")))
    assert kitchen.socket.messages()[0] == {"type": "state", "hearing": "gesture"}


# --- words kept while a reply is written are answered aloud, once (ruling T25) --------------


class _Collect:
    """The page chat's listener: every event, as it comes."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    @property
    def reading(self) -> bool:
        return True

    async def open(self) -> None:
        return None

    async def emit(self, event: Event) -> None:
        self.events.append(event)


class _Gated:
    """A conversation model whose first call says a sentence, waits for ``gate``, then calls
    ``tool`` (none: it ends there); every other call (and the judge's) is ``then``'s."""

    def __init__(self, sentence: str, tool: ToolCall | None, then: FakeChatModel) -> None:
        self.sentence = sentence
        self.tool = tool
        self.then = then
        self.gate = asyncio.Event()
        self.first = True

    @property
    def calls(self) -> list[Any]:
        return self.then.calls

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
        if self.first and not is_judge_call(system):
            self.first = False
            yield ChatChunk(self.sentence)
            await self.gate.wait()
            if self.tool is not None:
                yield self.tool
            yield ChatUsage(3, 2)
            return
        async for item in self.then.stream(
            system=system,
            messages=messages,
            max_output_tokens=max_output_tokens,
            tools=tools,
            tool_choice=tool_choice,
            think=think,
        ):
            yield item


LOOK = ToolCall("c1", "household_list", "{}")


async def test_words_answered_in_a_reply_of_the_chat_page_are_said_aloud(house, owner, live_clock):
    model = _Gated("Let me look at who is here.\n", LOOK, FakeChatModel(["The radio is on."]))
    h = await house(model)
    page = h.page(MIC)
    # A message typed on the chat page: its reply is the page's, not the voice's.
    conversation = Conversation(h.deps.turn)
    typed = request_of(owner, live_clock(), PAGE)
    _, reply = await conversation.say(owner, "Who is here?", typed.speaker, typed)
    assert reply is not None
    chat = _Collect()
    assert await conversation.answer(owner, reply.id, typed, chat)
    await until(lambda: any(isinstance(e, Words) for e in chat.events))
    # Spoken while that reply is written: kept for it, and what answers it is said aloud here.
    await heard_now(page, "And turn on the radio.")
    assert page.of("say") == []
    model.gate.set()
    await until(lambda: any(isinstance(e, Ended) for e in chat.events))
    await until(lambda: bool(page.of("say_end")))
    await h.turns_over()
    assert await all_said(h) == ["Who is here?", "And turn on the radio."]
    # Said once, from when the turn read the words: what answers them.
    assert h.service.said == ["The radio is on."]
    [reply_text] = [text for role, text, _ in await h.messages() if role == "assistant"]
    assert reply_text.endswith("The radio is on.")
    # The chat page got its reply whole as well.
    assert any(isinstance(e, Words) and "radio" in e.text for e in chat.events)


async def test_words_kept_during_his_own_reply_are_answered_once_aloud_even_after_a_stop(house):
    model = _Gated("Let me look at who is here.\n", LOOK, FakeChatModel(["The kitchen, then."]))
    h = await house(model)
    page = h.page(MIC)
    await heard_now(page, "Who is here?")
    await until(lambda: bool(page.of("say_end")))
    assert page.session.speaking
    h.attention.answer = "mine stop"
    await heard_now(page, "No, turn on the kitchen light.")
    assert page.of("stop")
    model.gate.set()
    await until(lambda: "The kitchen, then." in h.service.said)
    await h.turns_over()
    # Stopped, then the same reply spoke again once its turn read the words: said once.
    assert page.of("resume")
    assert h.service.said == ["Let me look at who is here.", "The kitchen, then."]
    assert await all_said(h) == ["Who is here?", "No, turn on the kitchen light."]


async def test_a_kept_spoken_request_that_needs_a_proof_is_awaited_by_voice(
    house, session_factory, owner, live_clock
):
    then = FakeChatModel(
        tool_script=[ToolCall("c1", "profile_rename", '{"name": "Alex B"}'), "Say your phrase."],
        judge="proof: not sure",
    )
    model = _Gated("One moment, please.\n", None, then)
    h = await house(model)
    async with session_factory() as writer:
        await agree(writer, owner.id, "spoken_phrase", proposed_by="person", now=live_clock())
        await writer.commit()
    h.vault.phrase[phrase_ref(owner.id)] = "blue horses sing at dawn"
    page = h.page(MIC)
    await heard_now(page, "Hello there.")
    await until(lambda: bool(page.of("say_end")))
    # Said while the first reply is written, which reads nothing more: answered after it.
    await heard_now(page, "Call me Alex B.")
    model.gate.set()
    await until(lambda: "Say your phrase." in h.service.said)
    await h.turns_over()
    async with session_factory() as reader:
        [c] = (await reader.execute(select(Confirmation))).scalars().all()
    assert (c.status, c.ask) == ("pending", "proof")
    # Awaited of him by his phrase: what he says next is weighed first, never written.
    assert await proofs_awaited(h.deps, owner, live_clock()) == [c.id]
    assert h.service.said.count("Say your phrase.") == 1
    await heard_now(page, "blue horses sing at dawn")
    await h.turns_over()
    assert h.vault.seen == ["blue horses sing at dawn"]
    assert "blue horses sing at dawn" not in await all_said(h)


async def test_the_judgement_listens_for_the_name_he_is_called_by(
    house, owner, session_factory, set_setting
):
    h = await house()
    page = h.page(MIC)
    await page.hears("Hello.")
    assert h.attention.asked[-1]["his_name"] == "Domo"
    await set_setting("house.domo_name", "Friday")
    await page.hears("Friday, the light.")
    assert h.attention.asked[-1]["his_name"] == "Friday"
    async with session_factory() as writer:
        found = await writer.get(Person, owner.id)
        assert found is not None
        found.calls_domo = "Brenda"
        await writer.commit()
    await page.hears("Brenda, the light.")
    assert h.attention.asked[-1]["his_name"] == "Brenda"


def test_the_instructions_say_what_his_name_is():
    from domo_core.prompts import attention_prompt

    told = attention_prompt()
    # The name he is given is explained, not left to be guessed (final fix wave B2).
    assert "- his_name:" in told
    assert "his_name" in told.split("Was it for him?")[1]
