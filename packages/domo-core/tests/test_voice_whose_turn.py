"""The turn belongs to the one who speaks (spec 6.5, 6.9; Task 24): whoever the voice says
probably spoke gives the rights and the conversation, a phrase is only ever weighed for the
probable person, what the assistant says on his own is never said aloud to a voice that is not
the person's, and an agreement in words counts only from the person whose agreement it is."""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from chat_support import dump
from domo_core.accounts.models import Person
from domo_core.channels.page.abilities import PAGE
from domo_core.channels.page.speaker import page_speaker
from domo_core.channels.voice import session as session_module
from domo_core.channels.voice.abilities import VOICE
from domo_core.channels.voice.live import LATE_COPY_MARGIN_SECONDS, LIVE_VOICES, LiveVoices
from domo_core.channels.voice.prints import NOT_LEARNED
from domo_core.channels.voice.proof import Weighed
from domo_core.channels.voice.speaker import voice_speaker
from domo_core.chat.store import ConversationStore
from domo_core.confirmations.models import Confirmation
from domo_core.confirmations.store import NO_NOT_TAKEN
from domo_core.conversation import prepare as prepare_module
from domo_core.conversation.events import Event
from domo_core.conversation.listeners import Listeners
from domo_core.conversation.prepare import TurnRequest
from domo_core.conversation.service import Conversation
from domo_core.conversation.speaker import Clue, Speaker, actor_of
from domo_core.conversation.tool_wakes import tool_wake_turn
from domo_core.identity.clues import PROOF_CLUE_FOR, keep_proof_clue
from domo_core.identity.models import IdentityClue
from domo_core.llm.speech.client import Print
from domo_core.llm.types import ChatChunk, ChatUsage, ToolCall
from domo_core.memory.models import Memory
from domo_core.policy.actions import DescribedAction
from domo_core.policy.barrier import Refuse, identity_judged, may_act_weakly
from domo_core.policy.catalog import DEFAULT_POLICY
from domo_core.policy.decision import decide_now
from domo_core.policy.roles import Actor
from domo_core.prompts import ToolWake
from domo_core.proofs.kinds import PROOF_KINDS, SpokenPhraseProof, phrase_ref
from domo_core.proofs.service import agree
from fakes import FakeChatModel, is_judge_call, is_reader_call
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
    messages_of,
    sound,
    until,
)

MIC = "desk-1"
PHRASE = "blue horses sing at dawn"
INVITE_ROBIN = {"display_name": "Robin", "role": "guest"}


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


@pytest.fixture
async def paul(add_member: Callable[..., Awaitable[Person]]) -> Person:
    return await add_member("Paul", "child")


class Heard:
    """Who the voice of the next sentences says spoke (``None``: as the voice itself says)."""

    def __init__(self) -> None:
        self.speaker: Callable[[datetime], Speaker] | None = None


@pytest.fixture
def heard(monkeypatch) -> Heard:
    found = Heard()
    real = session_module.voice_speaker

    def voice_speaker(**kwargs: Any) -> Speaker:
        if found.speaker is None:
            return real(**kwargs)
        return found.speaker(kwargs["now"])

    monkeypatch.setattr(session_module, "voice_speaker", voice_speaker)
    return found


@pytest.fixture
async def house(session_factory, settings, live_clock, owner):
    await a_household_that_hears_at_home(session_factory, owner)
    made: list[House] = []

    def make(model: FakeChatModel | None = None, **others: Any) -> House:
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


def voice_for(
    person: Person, degree: float = 0.8, *, at: Person | None = None
) -> Callable[[datetime], Speaker]:
    """Another person of the household, clearly, by the voice (the row "other"); ``at``: the
    person whose signed-in page heard them, as ``voice_speaker`` says it."""
    return lambda now: Speaker(
        person.id,
        "voice",
        clues=(Clue("voice", "for", "weak", "code", now, degree=degree),)
        + (() if at is None else (Clue("session", "against", "weak", "code", now),)),
        microphone=MIC,
        page_person_id=None if at is None else at.id,
    )


def nobody(now: datetime) -> Speaker:
    """A voice far from the page's person and like nobody known."""
    return Speaker(
        None,
        "voice",
        clues=(Clue("voice", "against", "weak", "code", now, degree=0.1),),
        microphone=MIC,
    )


def against(person: Person) -> Callable[[datetime], Speaker]:
    """The page's person by the page, a voice that weighs against them (someone else, a cold)."""
    return lambda now: Speaker(
        person.id,
        "voice",
        clues=(
            Clue("session", "for", "strong", "code", now),
            Clue("voice", "against", "weak", "code", now, degree=0.45),
        ),
        microphone=MIC,
    )


def too_short(person: Person) -> Callable[[datetime], Speaker]:
    """The page's person by the page, a sound too short to tell who said it."""
    return lambda now: Speaker(
        person.id,
        "voice",
        clues=(
            Clue("session", "for", "strong", "code", now),
            Clue("voice", "unknown", "weak", "code", now),
        ),
        microphone=MIC,
        voice_too_short=True,
    )


async def said_by(session_factory, person: Person) -> list[tuple[str, str, str]]:
    return [(m.role, m.content, m.status) for m in await messages_of(session_factory, person)]


async def written(session_factory) -> str:
    from sqlalchemy import text as sql_text

    async with session_factory() as reader:
        rows = (await reader.execute(sql_text("SELECT * FROM messages"))).all()
        audit = await reader.execute(sql_text("SELECT row_to_json(a)::text FROM audit_log a"))
        entries = [row[0] for row in audit.all()]
    return repr([tuple(row) for row in rows]) + "\n".join(entries)


def voice_clues_told(call: Any) -> list[dict[str, str]]:
    """The clues of the speaker a judge's call was told, each as its kind and how it weighs."""
    found: list[dict[str, str]] = []

    def walk(node: object) -> None:
        if isinstance(node, str):
            try:
                walk(json.loads(node))
            except ValueError:
                return
        elif isinstance(node, dict):
            if "kind" in node and "weighs" in node:
                found.append({"kind": node["kind"], "weighs": node["weighs"]})
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk([dump(m) for m in call.messages])
    return found


def model_saw(model: FakeChatModel) -> str:
    calls = [*model.calls, *model.judge_calls()]
    return "".join(call.system + json.dumps([dump(m) for m in call.messages]) for call in calls)


# --- whose conversation, whose rights ---------------------------------------------------------


async def test_paul_at_alexs_page_speaks_in_his_own_discussion_with_his_rights(
    house, heard, session_factory, paul
):
    h = house()
    page = h.page(MIC)
    heard.speaker = voice_for(paul)
    await page.hears("Turn on the light.")
    await until(lambda: bool(page.of("say_end")))
    await h.turns_over()
    assert await said_by(session_factory, paul) == [
        ("user", "Turn on the light.", "complete"),
        ("assistant", "Of course.", "complete"),
    ]
    # Said aloud here, at the page that heard him.
    assert h.service.said == ["Of course."]
    # The turn is Paul's: his role, and the line of who probably speaks names him.
    [call] = h.model.calls
    assert "Role: child" in call.system
    assert "Role: owner" not in call.system
    assert 'probably "Paul" who speaks' in call.system


async def test_alexs_discussion_gets_nothing_of_it(house, heard, session_factory, owner, paul):
    h = house()
    page = h.page(MIC)
    heard.speaker = voice_for(paul)
    await page.hears("Turn on the light.")
    await h.turns_over()
    assert await said_by(session_factory, owner) == []


async def test_a_voice_nobody_knows_enters_no_conversation(
    house, heard, session_factory, owner, caplog
):
    caplog.set_level(logging.INFO)
    h = house()
    page = h.page(MIC)
    heard.speaker = nobody
    await page.hears("Turn on the light.")
    await h.turns_over()
    # Its short exchange (Task 26) answered nothing readable: the page's one notice.
    assert page.of("not_taken") == [{"type": "not_taken", "reason": "not_now"}]
    # In nobody's conversation, no reply opened, one call with no tool, nothing said.
    async with session_factory() as reader:
        assert (await reader.execute(select(Confirmation))).first() is None
    assert await said_by(session_factory, owner) == []
    assert "Turn on the light." not in await written(session_factory)
    assert [call.tools for call in h.model.calls] == [()]
    assert h.service.said == []
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


async def test_words_go_in_a_conversation_only_as_the_speakers_own(
    house, session_factory, owner, paul, live_clock
):
    h = house()
    conversation = Conversation(h.deps.turn)
    for speaker in (nobody(live_clock()), voice_for(paul)(live_clock())):
        with pytest.raises(ValueError):
            await conversation.say(owner, "Turn on the light.", speaker)
    # Kept words of nobody known are never answered as the person's either.
    await conversation.answer_kept(
        owner.id, uuid.uuid4(), [(uuid.uuid4(), TurnRequest(nobody(live_clock()), VOICE))]
    )
    assert await said_by(session_factory, owner) == []


async def test_a_voice_that_contradicts_keeps_no_proof_alive(
    house, session_factory, owner, live_clock
):
    h = house()
    async with session_factory() as writer:
        await keep_proof_clue(
            writer,
            person_id=owner.id,
            channel="voice",
            device_id=None,
            means="spoken_phrase",
            now=live_clock(),
            microphone=MIC,
        )
        await writer.commit()
    first = live_clock() + PROOF_CLUE_FOR

    async def expiry() -> datetime:
        async with session_factory() as reader:
            return await reader.scalar(select(IdentityClue.expires_at))  # type: ignore[return-value]

    live_clock.advance(timedelta(minutes=5))
    await Conversation(h.deps.turn).say(owner, "Turn on the light.", against(owner)(live_clock()))
    assert await expiry() == first
    # The person's own voice keeps it alive, as before.
    live_clock.advance(timedelta(minutes=1))
    mine = Speaker(
        owner.id,
        "voice",
        clues=(Clue("session", "for", "strong", "code", live_clock()),),
        microphone=MIC,
    )
    await Conversation(h.deps.turn).say(owner, "And the radio.", mine)
    assert await expiry() == live_clock() + PROOF_CLUE_FOR


# --- the proof awaited ------------------------------------------------------------------------


def asks_a_proof(judge: str | list[str] = "proof: not sure") -> FakeChatModel:
    """A model that renames the person, which the judge holds for a proof."""
    return FakeChatModel(
        tool_script=[ToolCall("c1", "profile_rename", '{"name": "Alex B"}'), "Say your phrase."],
        judge=judge,
    )


async def a_proof_is_asked(h: House, page: Any, session_factory, owner: Person) -> Confirmation:
    async with session_factory() as writer:
        await agree(writer, owner.id, "spoken_phrase", proposed_by="person", now=h.clock())
        await writer.commit()
    h.vault.phrase[phrase_ref(owner.id)] = PHRASE
    await page.hears("Call me Alex B.")
    async with session_factory() as reader:
        [c] = (await reader.execute(select(Confirmation))).scalars().all()
    assert (c.status, c.ask) == ("pending", "proof")
    return c


async def status_of(session_factory, c: Confirmation) -> str | None:
    async with session_factory() as reader:
        return await reader.scalar(select(Confirmation.status).where(Confirmation.id == c.id))


async def name_of(session_factory, person: Person) -> str:
    async with session_factory() as reader:
        found = await reader.get(Person, person.id, populate_existing=True)
    assert found is not None
    return found.display_name


async def test_a_right_phrase_from_a_voice_that_contradicts_is_verified_and_the_judge_sees_the_clue(
    house, heard, session_factory, owner, caplog
):
    caplog.set_level(logging.DEBUG)
    h = house(asks_a_proof())
    page = h.page(MIC)
    c = await a_proof_is_asked(h, page, session_factory, owner)
    judged_before = len(h.model.judge_calls())
    heard.speaker = against(owner)
    await page.hears(PHRASE)
    # Checked by code: the gatekeeper compared it, and it is the phrase.
    assert h.vault.seen == [PHRASE]
    # The judge read the voice against the person, and asked another proof: nothing decided.
    [judged] = h.model.judge_calls()[judged_before:]
    case = json.dumps([dump(m) for m in judged.messages])
    assert '\\"against\\"' in case or '"against"' in case
    assert await status_of(session_factory, c) == "pending"
    assert await name_of(session_factory, owner) == "Alex"
    assert h.woken.contents() == [{"proof": "another_proof"}]
    for word in ("horses", "dawn"):
        assert word not in await written(session_factory)
        assert word not in model_saw(h.model)
        assert word not in caplog.text


async def test_a_right_phrase_from_a_voice_that_fits_needs_no_judge(house, session_factory, owner):
    h = house(asks_a_proof())
    page = h.page(MIC)
    c = await a_proof_is_asked(h, page, session_factory, owner)
    judged_before = len(h.model.judge_calls())
    await page.hears(PHRASE)
    assert h.model.judge_calls()[judged_before:] == []
    assert await status_of(session_factory, c) != "pending"
    assert await name_of(session_factory, owner) == "Alex B"


async def test_alexs_phrase_in_a_voice_read_as_pauls_is_weighed_and_never_written(
    house, heard, session_factory, owner, paul, caplog
):
    caplog.set_level(logging.DEBUG)
    h = house(asks_a_proof())
    page = h.page(MIC)
    c = await a_proof_is_asked(h, page, session_factory, owner)
    judged = len(h.model.judge_calls())
    # Alex's phrase, in a voice read as Paul's, while Alex's proof is awaited here: weighed by
    # code as Alex's, against the voice; the judge (silent on the voice: a proof) asks another.
    heard.speaker = voice_for(paul, at=owner)
    await page.hears(PHRASE)
    assert h.vault.seen == [PHRASE]
    [case] = h.model.judge_calls()[judged:]
    assert {"kind": "voice", "weighs": "against"} in voice_clues_told(case)
    assert await status_of(session_factory, c) == "pending"
    assert h.woken.contents() == [{"proof": "another_proof"}]
    assert await said_by(session_factory, paul) == []
    for word in ("horses", "dawn"):
        assert word not in await written(session_factory)
        assert word not in model_saw(h.model)
        assert word not in caplog.text
    # Alex's own voice: his phrase is the yes.
    heard.speaker = None
    await page.hears(PHRASE)
    assert await name_of(session_factory, owner) == "Alex B"


async def test_paul_is_answered_at_alexs_page_while_alex_awaits_a_proof(
    house, heard, session_factory, owner, paul
):
    h = house(
        FakeChatModel(
            tool_script=[
                ToolCall("c1", "profile_rename", '{"name": "Alex B"}'),
                "Say your phrase.",
                "Of course, Paul.",
            ],
            judge="proof: not sure",
        )
    )
    page = h.page(MIC)
    c = await a_proof_is_asked(h, page, session_factory, owner)
    heard.speaker = voice_for(paul, at=owner)
    await page.hears("Can I watch a film?")
    await h.turns_over()
    # Weighed first against Alex's phrase (no answer), then Paul's own way: answered.
    assert h.vault.seen == ["Can I watch a film?"]
    assert page.of("not_taken") == []
    assert await said_by(session_factory, paul) == [
        ("user", "Can I watch a film?", "complete"),
        ("assistant", "Of course, Paul.", "complete"),
    ]
    assert await status_of(session_factory, c) == "pending"


# --- what he says on his own ------------------------------------------------------------------


async def test_his_own_words_for_alex_are_not_said_to_a_stranger_and_stay_written(
    house, heard, session_factory, owner
):
    listeners = Listeners()
    h = house(
        # The voice nobody knows gets its short exchange first (Task 26): it says nothing here.
        FakeChatModel(
            tool_script=[
                '{"say": "", "note": ""}',
                "Your parcel came.",
                "Of course.",
                "Your bank called.",
            ]
        ),
        listeners=listeners,
    )
    page = h.page(MIC)
    assert listeners.hearing(owner.id)
    heard.speaker = nobody
    await page.hears("Hello there.")
    assert not listeners.hearing(owner.id)
    wake = (ToolWake(tool="voice_channel", content={"news": "parcel"}),)
    assert await tool_wake_turn(h.deps.turn, owner.id, wake)
    await h.turns_over()
    assert h.service.said == []
    assert ("assistant", "Your parcel came.", "complete") in await said_by(session_factory, owner)
    # Alex's own voice again: what he says to Alex on his own is said aloud again.
    heard.speaker = None
    await page.hears("Hi.")
    await until(lambda: h.service.said == ["Of course."])
    assert listeners.hearing(owner.id)
    assert await tool_wake_turn(h.deps.turn, owner.id, wake)
    await h.turns_over()
    await until(lambda: h.service.said == ["Of course.", "Your bank called."])


async def test_another_persons_voice_hears_nothing_said_on_his_own_here(
    house, heard, session_factory, owner, paul
):
    # Final fix wave, voice I4: at Alex's page only the replies of Paul's turns born here in this
    # visit are said; what the assistant says to Paul on his own (with Paul's whole context) goes
    # to Paul's own pages, never aloud here. Nor is Alex's said while Paul's voice is the last.
    listeners = Listeners()
    h = house(
        FakeChatModel(tool_script=["Light on.", "Your doctor's note came."]), listeners=listeners
    )
    page = h.page(MIC)
    heard.speaker = voice_for(paul, at=owner)
    await page.hears("Turn on the light.")
    await h.turns_over()
    assert h.service.said == ["Light on."]
    assert not listeners.hearing(paul.id)
    assert not listeners.hearing(owner.id)
    wake = (ToolWake(tool="voice_channel", content={"news": "note"}),)
    assert await tool_wake_turn(h.deps.turn, paul.id, wake)
    await h.turns_over()
    assert h.service.said == ["Light on."]
    assert ("assistant", "Your doctor's note came.", "complete") in await said_by(
        session_factory, paul
    )


# --- an agreement in words --------------------------------------------------------------------


def invites(judge: list[str]) -> FakeChatModel:
    return FakeChatModel(
        tool_script=[
            ToolCall("c1", "household_invite", json.dumps(INVITE_ROBIN)),
            "Shall I?",
            "Done.",
        ],
        judge=judge,
    )


async def a_request_waits(page: Any, session_factory) -> Confirmation:
    await page.hears("Invite Robin as a guest.")
    async with session_factory() as reader:
        [c] = (await reader.execute(select(Confirmation))).scalars().all()
    assert (c.status, c.ask) == ("pending", "yes")
    return c


async def test_a_strangers_short_yes_to_a_sensitive_request_does_not_agree_by_itself(
    house, heard, session_factory, owner
):
    h = house(invites(["yes: a first time", "proof: a short voice"]))
    page = h.page(MIC)
    c = await a_request_waits(page, session_factory)
    heard.speaker = too_short(owner)
    await page.hears("yes")
    async with session_factory() as reader:
        found = await reader.get(Confirmation, c.id, populate_existing=True)
    assert found is not None
    assert (found.status, found.ask, found.asked_by) == ("pending", "proof", "judge")
    # The judge weighed who agreed, with the clues of the voice.
    # The judge weighed who agreed, with the clue of a voice too short to tell.
    assert {"kind": "voice", "weighs": "unknown"} in voice_clues_told(h.model.judge_calls()[-1])


async def test_the_persons_own_yes_after_their_long_sentence_agrees(house, session_factory, owner):
    h = house(invites(["yes: a first time"]))
    page = h.page(MIC)
    c = await a_request_waits(page, session_factory)
    judged = len(h.model.judge_calls())
    await page.hears("yes")
    assert await status_of(session_factory, c) == "executed"
    assert len(h.model.judge_calls()) == judged


class Collected:
    reading = True

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def open(self) -> None:
        return None

    async def emit(self, event: Event) -> None:
        self.events.append(event)


async def test_a_typed_yes_is_unchanged(house, session_factory, owner, live_clock):
    h = house(invites(["yes: a first time"]))
    page = h.page(MIC)
    c = await a_request_waits(page, session_factory)
    judged = len(h.model.judge_calls())
    typed = page_speaker(owner.id, live_clock())
    conversation = Conversation(h.deps.turn)
    _, reply = await conversation.say(owner, "yes", typed)
    assert reply is not None
    await conversation.answer(owner, reply.id, TurnRequest(typed, PAGE), Collected())
    await h.turns_over()
    assert await status_of(session_factory, c) == "executed"
    assert len(h.model.judge_calls()) == judged


# --- a refusal in words from a voice the code is not sure of (the owner, 2026-11-27) -------------


def refuses(judge: list[str]) -> FakeChatModel:
    return FakeChatModel(
        tool_script=[
            ToolCall("c1", "household_invite", json.dumps(INVITE_ROBIN)),
            "Shall I?",
            "Was that you?",
            "As you wish.",
        ],
        judge=judge,
    )


async def shown_by(session_factory, c: Confirmation) -> uuid.UUID | None:
    async with session_factory() as reader:
        return await reader.scalar(
            select(Confirmation.last_shown_message_id).where(Confirmation.id == c.id)
        )


async def test_a_short_no_the_judge_takes_for_theirs_refuses(house, heard, session_factory, owner):
    h = house(refuses(["yes: a first time", "do: it is plainly them"]))
    page = h.page(MIC)
    c = await a_request_waits(page, session_factory)
    heard.speaker = too_short(owner)
    await page.hears("no")
    assert await status_of(session_factory, c) == "refused"
    # Weighed with the clues of the voice: the judge was asked about dropping that request.
    [_, weighed] = h.model.judge_calls()
    assert {"kind": "voice", "weighs": "unknown"} in voice_clues_told(weighed)
    told = weighed.system + json.dumps([dump(m) for m in weighed.messages])
    assert "confirmation.expire" in told and f"confirmation:{c.id}" in told


@pytest.mark.parametrize(
    "verdict", ["yes: was that them?", "proof: a short voice", "do_not: not them", "nonsense"]
)
async def test_a_short_no_the_judge_doubts_leaves_the_request_waiting_and_shown_again(
    house, heard, session_factory, owner, verdict
):
    # Doubted, refused by nothing, or the judge silent ("nonsense" is no verdict): the request
    # stays waiting and is shown again; nothing is done, nothing refused.
    h = house(refuses(["yes: a first time", verdict]))
    page = h.page(MIC)
    c = await a_request_waits(page, session_factory)
    first_shown = await shown_by(session_factory, c)
    heard.speaker = too_short(owner)
    await page.hears("no")
    async with session_factory() as reader:
        found = await reader.get(Confirmation, c.id, populate_existing=True)
    assert found is not None
    # Nothing raised: a no asks nothing more of a later yes.
    assert (found.status, found.ask, found.ask_before_doubt) == ("pending", "yes", None)
    assert found.last_shown_message_id not in (None, first_shown)
    # The assistant is told their no was not taken, and may ask again.
    assert NO_NOT_TAKEN in model_saw(h.model)
    # Their sure no, said again, refuses it at once, with no judge.
    judged = len(h.model.judge_calls())
    heard.speaker = None
    await page.hears("no")
    assert await status_of(session_factory, c) == "refused"
    assert len(h.model.judge_calls()) == judged


async def test_a_short_yes_the_judge_takes_for_theirs_agrees(house, heard, session_factory, owner):
    # The judge of a word read as an answer has the turn's models: he is really asked, not
    # silent (before, he was never reached there and a doubted yes always asked a proof).
    h = house(invites(["yes: a first time", "do: it is plainly them"]))
    page = h.page(MIC)
    c = await a_request_waits(page, session_factory)
    heard.speaker = too_short(owner)
    await page.hears("yes")
    assert await status_of(session_factory, c) == "executed"
    [_, weighed] = h.model.judge_calls()
    assert {"kind": "voice", "weighs": "unknown"} in voice_clues_told(weighed)


async def test_a_sure_no_refuses_at_once_with_no_judge(house, session_factory, owner):
    h = house(refuses(["yes: a first time"]))
    page = h.page(MIC)
    c = await a_request_waits(page, session_factory)
    judged = len(h.model.judge_calls())
    await page.hears("no")
    assert await status_of(session_factory, c) == "refused"
    assert len(h.model.judge_calls()) == judged


# --- fix round 1 (review of Task 24) ----------------------------------------------------------


async def test_a_yes_raised_to_a_proof_opens_the_moment_and_the_phrase_is_never_written(
    house, heard, session_factory, owner, caplog
):
    caplog.set_level(logging.DEBUG)
    h = house(invites(["yes: a first time", "proof: a short voice"]))
    page = h.page(MIC)
    async with session_factory() as writer:
        await agree(writer, owner.id, "spoken_phrase", proposed_by="person", now=h.clock())
        await writer.commit()
    h.vault.phrase[phrase_ref(owner.id)] = PHRASE
    c = await a_request_waits(page, session_factory)
    heard.speaker = too_short(owner)
    await page.hears("yes")
    assert await status_of(session_factory, c) == "pending"
    # His phrase, said next: weighed as the proof, never written, never sent to a model.
    heard.speaker = None
    await page.hears(PHRASE)
    assert h.vault.seen == [PHRASE]
    assert await status_of(session_factory, c) == "executed"
    for word in ("horses", "dawn"):
        assert word not in await written(session_factory)
        assert word not in model_saw(h.model)
        assert word not in caplog.text


async def test_another_voice_is_the_last_one_only_for_a_while(
    house, heard, owner, paul, monkeypatch
):
    monkeypatch.setattr(session_module, "LAST_VOICE_LASTS_S", 0.05)
    listeners = Listeners()
    h = house(listeners=listeners)
    page = h.page(MIC)
    heard.speaker = voice_for(paul)
    await page.hears("Turn on the light.")
    # Then, not heard again: Alex is the one spoken to here again, and Paul is no longer
    # listened for here (his stop of the listening, given anywhere, does not reach this page).
    await until(lambda: listeners.hearing(owner.id) and not listeners.hearing(paul.id))
    assert await LIVE_VOICES.stop_listening(paul.id, None) == 0
    assert page.of("listening") == []


async def test_another_voice_stops_being_listened_for_once_another_is_heard(
    house, heard, owner, paul
):
    listeners = Listeners()
    h = house(listeners=listeners)
    page = h.page(MIC)
    heard.speaker = voice_for(paul)
    await page.hears("Turn on the light.")
    # Never what is said to him on his own here (voice I4), but his stop of the listening
    # reaches this page while his voice is the last.
    assert not listeners.hearing(paul.id)
    assert paul.id in LIVE_VOICES.persons()
    heard.speaker = None
    await page.hears("And the radio.")
    assert listeners.hearing(owner.id)
    assert not listeners.hearing(paul.id)
    assert await LIVE_VOICES.stop_listening(paul.id, None) == 0


def test_a_voice_heard_at_someone_elses_page_says_so(owner, paul, live_clock):
    reading = replace(NOT_LEARNED, learnt=True, best_other=paul.id, other_degree=0.9, degree=0.1)
    speaker = voice_speaker(
        page_person=owner,
        reading=reading,
        print_=Print((1.0, 0.0), "m", 3.0, None, None, None),
        microphone=MIC,
        signed_in_at=live_clock(),
        proofs=(),
        now=live_clock(),
    )
    assert speaker.person_id == paul.id
    assert speaker.page_person_id == owner.id
    assert [(c.kind, c.weighs, c.strength) for c in speaker.clues] == [
        ("voice", "for", "weak"),
        ("session", "against", "weak"),
    ]
    assert actor_of(speaker, paul).page_person_id == owner.id


def test_nothing_is_ordinary_for_a_voice_at_someone_elses_page():
    at_their_page = Actor(uuid.uuid4(), "adult", standing="unsure", page_person_id=uuid.uuid4())
    unsure = Actor(uuid.uuid4(), "adult", standing="unsure")
    assert may_act_weakly("profile.rename", DEFAULT_POLICY)
    assert not identity_judged(unsure, "profile.rename", DEFAULT_POLICY, None, "do")
    assert identity_judged(at_their_page, "profile.rename", DEFAULT_POLICY, None, "do")


async def test_the_page_persons_rules_count_for_a_voice_heard_there(
    session_factory, owner, add_member, live_clock
):
    tom = await add_member("Tom", "child")
    look = DescribedAction("core", "net.look", None, "read", "household")
    async with session_factory() as session:
        alone = await decide_now(
            session,
            Actor(owner.id, "owner", standing="unsure"),
            look,
            DEFAULT_POLICY,
            live_clock(),
        )
        as_tom = await decide_now(
            session, Actor(tom.id, "child", standing="unsure"), look, DEFAULT_POLICY, live_clock()
        )
        at_toms_page = await decide_now(
            session,
            Actor(owner.id, "owner", standing="unsure", page_person_id=tom.id),
            look,
            DEFAULT_POLICY,
            live_clock(),
        )
    assert not isinstance(alone, Refuse)
    # What the owner wrote for Tom is not escaped by sounding like Alex at Tom's page.
    assert isinstance(as_tom, Refuse)
    assert isinstance(at_toms_page, Refuse)
    assert at_toms_page.reason == as_tom.reason


async def test_what_he_keeps_to_himself_is_not_brought_in_when_unsure(
    house, heard, session_factory, owner
):
    async with session_factory() as writer:
        writer.add(
            Memory(
                said_by_id=owner.id,
                about_id=owner.id,
                subject="me",
                content="ZEBRA-SECRET is the code of the safe",
                kind="fact",
                origin="own_words",
                kept_private=True,
                core=True,
            )
        )
        await writer.commit()
    h = house(FakeChatModel(tool_script=["Of course.", "Of course."]))
    page = h.page(MIC)
    await page.hears("Turn on the light.")
    await h.turns_over()
    assert "ZEBRA-SECRET" in h.model.calls[-1].system
    heard.speaker = against(owner)
    await page.hears("And the radio.")
    await h.turns_over()
    assert "ZEBRA-SECRET" not in h.model.calls[-1].system


async def test_kept_words_not_the_persons_are_dropped_and_theirs_answered(
    house, session_factory, owner, live_clock
):
    h = house()
    conversation = Conversation(h.deps.turn)
    mine = Speaker(
        owner.id, "voice", clues=(Clue("session", "for", "strong", "code", live_clock()),)
    )
    said, reply = await conversation.say(owner, "Turn on the light.", mine)
    assert reply is not None
    async with session_factory() as writer:
        await ConversationStore(writer).finish_message(reply.id, content="", status="error")
        await writer.commit()
    await conversation.answer_kept(
        owner.id,
        said.conversation_id,
        [
            (uuid.uuid4(), TurnRequest(nobody(live_clock()), VOICE)),
            (said.id, TurnRequest(mine, VOICE)),
        ],
    )
    await h.turns_over()
    assert ("assistant", "Of course.", "complete") in await said_by(session_factory, owner)


# --- fix round 2 (second review of Task 24) ---------------------------------------------------


def renames_with_a_proof() -> FakeChatModel:
    return FakeChatModel(
        tool_script=[
            ToolCall("c1", "profile_rename", '{"name": "Alex B"}'),
            "Say your phrase.",
            "Say your phrase, please.",
            "Of course.",
            "Of course.",
        ],
        judge="proof: not sure",
    )


async def test_a_proof_asked_on_the_chat_page_alone_opens_no_window(
    house, session_factory, owner, live_clock
):
    h = house(renames_with_a_proof())
    page = h.page(MIC)
    async with session_factory() as writer:
        await agree(writer, owner.id, "spoken_phrase", proposed_by="person", now=h.clock())
        await writer.commit()
    h.vault.phrase[phrase_ref(owner.id)] = PHRASE
    # Asked on the chat page, typed: nothing of it was ever said at the microphone.
    typed = page_speaker(owner.id, live_clock())
    conversation = Conversation(h.deps.turn)
    _, reply = await conversation.say(owner, "Call me Alex B.", typed)
    assert reply is not None
    await conversation.answer(owner, reply.id, TurnRequest(typed, PAGE), Collected())
    await h.turns_over()
    async with session_factory() as reader:
        [c] = (await reader.execute(select(Confirmation))).scalars().all()
    assert (c.status, c.ask) == ("pending", "proof")
    # Nothing was said to him at the microphone: no window there, a sentence is compared with
    # nothing (a limit the controller accepted: a phrase said there would be ordinary words).
    await page.hears("Turn on the light.")
    assert h.vault.seen == []
    assert "Turn on the light." in [
        m.content for m in await messages_of(session_factory, owner) if m.role == "user"
    ]


async def test_the_phrase_after_three_exchanges_is_weighed_never_written(
    house, session_factory, owner, caplog
):
    caplog.set_level(logging.DEBUG)
    h = house(
        FakeChatModel(
            tool_script=[
                ToolCall("c1", "profile_rename", '{"name": "Alex B"}'),
                "Say your phrase.",
                "To Alex B.",
                "No fee. Your phrase, please.",
            ],
            judge="proof: not sure",
        )
    )
    page = h.page(MIC)
    c = await a_proof_is_asked(h, page, session_factory, owner)
    await page.hears("Which name?")
    await page.hears("Any fee?")
    # However many sentences came before, the phrase is weighed by code first.
    await page.hears(PHRASE)
    assert await status_of(session_factory, c) != "pending"
    assert await name_of(session_factory, owner) == "Alex B"
    assert h.vault.seen == ["Which name?", "Any fee?", PHRASE]
    for word in ("horses", "dawn"):
        assert word not in await written(session_factory)
        assert word not in model_saw(h.model)
        assert word not in caplog.text


async def test_with_no_proof_awaited_nothing_goes_to_the_gatekeeper(house, session_factory, owner):
    h = house()
    page = h.page(MIC)
    async with session_factory() as writer:
        await agree(writer, owner.id, "spoken_phrase", proposed_by="person", now=h.clock())
        await writer.commit()
    h.vault.phrase[phrase_ref(owner.id)] = PHRASE
    await page.hears("Turn on the light.")
    assert h.vault.seen == []


async def test_a_person_with_no_phrase_is_heard_as_before(house, session_factory, owner):
    h = house(renames_with_a_proof())
    page = h.page(MIC)
    await page.hears("Call me Alex B.")
    async with session_factory() as reader:
        [c] = (await reader.execute(select(Confirmation))).scalars().all()
    assert (c.status, c.ask) == ("pending", "proof")
    await page.hears("Turn on the light.")
    assert h.vault.seen == []
    async with session_factory() as reader:
        said = [m.content for m in await messages_of(session_factory, owner) if m.role == "user"]
    assert said == ["Call me Alex B.", "Turn on the light."]


async def test_at_someone_elses_page_only_what_was_said_there_is_told(
    house, heard, session_factory, owner, paul, live_clock
):
    h = house(FakeChatModel(tool_script=["Noted.", "Noted.", "Of course."]))
    page = h.page(MIC)
    heard.speaker = voice_for(paul, at=owner)
    await page.hears("What did I tell you this morning?")
    await h.turns_over()
    # Meanwhile, Paul's own words, typed at his own page.
    typed = page_speaker(paul.id, live_clock())
    conversation = Conversation(h.deps.turn)
    _, reply = await conversation.say(paul, "MANGO-NOTE: the bonus is 5000", typed)
    assert reply is not None
    await conversation.answer(paul, reply.id, TurnRequest(typed, PAGE), Collected())
    await h.turns_over()
    await page.hears("And what did I just tell you?")
    await h.turns_over()
    told = json.dumps([dump(m) for m in h.model.calls[-1].messages])
    # What was said and answered here in this visit is told; what was typed elsewhere is not.
    assert "What did I tell you this morning?" in told
    assert "And what did I just tell you?" in told
    assert "MANGO-NOTE" not in told


async def test_at_someone_elses_page_nothing_else_of_theirs_is_told(
    house, heard, owner, paul, monkeypatch
):
    asked: list[str] = []

    async def notes(*args: Any, **kwargs: Any) -> tuple[()]:
        asked.append("notes")
        return ()

    async def plans(*args: Any, **kwargs: Any) -> tuple[()]:
        asked.append("plans")
        return ()

    elsewhere = SimpleNamespace(last_shown_message_id=uuid.uuid4())

    async def pending(*args: Any, **kwargs: Any) -> list[Any]:
        return [elsewhere]

    monkeypatch.setattr(prepare_module, "notes_to_tell", notes)
    monkeypatch.setattr(prepare_module, "plans_shown", plans)
    monkeypatch.setattr(prepare_module, "pending_for", pending)
    h = house()
    page = h.page(MIC)
    heard.speaker = voice_for(paul, at=owner)
    await page.hears("Turn on the light.")
    await until(lambda: bool(page.of("say_end")))
    await h.turns_over()
    # No request shown elsewhere; his notes and plans are looked at only to know whether
    # something waits for him (case 9), and nothing waits.
    assert set(asked) <= {"notes", "plans"}
    assert "Something waits for them" not in h.model.calls[-1].system
    assert h.service.said == ["Of course."]


async def test_at_someone_elses_page_he_knows_that_something_waits_and_a_sure_voice_what(
    house, heard, session_factory, owner, paul
):
    # Case 9 of the final wave: never "nothing" when a note waits for Paul at Alex's page.
    from domo_core.notes.models import Note

    async with session_factory() as writer:
        writer.add(
            Note(
                author_id=owner.id,
                recipient_id=paul.id,
                text="KIWI-NOTE: dinner at eight",
                created_at=datetime.now(UTC),
            )
        )
        await writer.commit()
    h = house(FakeChatModel(tool_script=["Look at your own page.", "Dinner is at eight."]))
    page = h.page(MIC)
    heard.speaker = voice_for(paul, at=owner)
    await page.hears("Anything for me?")
    await h.turns_over()
    unsure = h.model.calls[-1].system
    assert "Something waits for them" in unsure and "KIWI-NOTE" not in unsure
    # Sure of him (a proof he gave here, and this voice clearly his: final fix wave B, I-3):
    # what waits is his context, his to tell here or not.
    proved = Clue("proof", "for", "strong", "code", datetime.now(UTC), means="password")
    clearly = Clue("voice", "for", "weak", "code", datetime.now(UTC), degree=0.8)
    heard.speaker = lambda now: replace(voice_for(paul, at=owner)(now), clues=(clearly, proved))
    await page.hears("Tell me.")
    await h.turns_over()
    sure = h.model.calls[-1].system + json.dumps([dump(m) for m in h.model.calls[-1].messages])
    assert "KIWI-NOTE" in sure and "others may hear" in sure


async def test_an_unsure_person_at_their_own_page_keeps_their_history(
    house, heard, session_factory, owner
):
    h = house(FakeChatModel(tool_script=["Noted.", "Of course."]))
    page = h.page(MIC)
    await page.hears("MANGO-NOTE: the bonus is 5000")
    heard.speaker = against(owner)
    await page.hears("What did I say?")
    await h.turns_over()
    assert "MANGO-NOTE" in json.dumps([dump(m) for m in h.model.calls[-1].messages])


async def test_a_voice_too_short_to_tell_brings_nothing_private(
    house, heard, session_factory, owner
):
    async with session_factory() as writer:
        writer.add(
            Memory(
                said_by_id=owner.id,
                about_id=owner.id,
                subject="me",
                content="ZEBRA-SECRET is the code of the safe",
                kind="fact",
                origin="own_words",
                kept_private=True,
                core=True,
            )
        )
        await writer.commit()
    h = house(FakeChatModel(tool_script=["Of course."]))
    page = h.page(MIC)
    heard.speaker = too_short(owner)
    await page.hears("Hi.")
    await h.turns_over()
    assert "ZEBRA-SECRET" not in h.model.calls[-1].system


async def test_a_reply_that_fails_to_follow_still_lets_its_listener_go(house, monkeypatch):
    h = house()
    page = h.page(MIC)

    async def fails(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(page.session, "_moment_asked", fails)
    await page.hears("Turn on the light.")
    await until(lambda: bool(page.of("say_end")))
    await h.turns_over()
    await until(lambda: not page.session._replies)  # noqa: SLF001


# --- fix round 5: the proof window of a microphone ----------------------------------------------


async def test_an_unknown_voice_in_a_window_is_weighed_before_any_model(
    house, heard, session_factory, owner, caplog
):
    caplog.set_level(logging.DEBUG)
    h = house(asks_a_proof())
    page = h.page(MIC)
    c = await a_proof_is_asked(h, page, session_factory, owner)
    asked, judged = len(h.attention.asked), len(h.model.judge_calls())
    heard.speaker = nobody
    await page.hears(PHRASE)
    # Compared first, as Alex's phrase by a voice against him: verified, then the judge (who
    # asks another proof); never sent to the quick judgement, never written.
    assert h.vault.seen == [PHRASE]
    assert len(h.attention.asked) == asked
    [case] = h.model.judge_calls()[judged:]
    assert {"kind": "voice", "weighs": "against"} in voice_clues_told(case)
    assert await status_of(session_factory, c) == "pending"
    assert h.woken.contents() == [{"proof": "another_proof"}]
    for word in ("horses", "dawn"):
        assert word not in await written(session_factory)
        assert word not in model_saw(h.model)
        assert word not in caplog.text
    # Ordinary words of that voice: compared, then their usual way (the quick judgement, then
    # its short exchange, which answers nothing readable here: the page's one notice).
    await page.hears("Hello there.")
    assert h.vault.seen == [PHRASE, "Hello there."]
    assert len(h.attention.asked) == asked + 1
    assert page.of("not_taken")[-1] == {"type": "not_taken", "reason": "not_now"}


async def test_alexs_phrase_misread_as_the_page_persons_at_pauls_page_is_weighed_first(
    session_factory, settings, live_clock, heard, owner, paul, caplog
):
    caplog.set_level(logging.DEBUG)
    # Alex's own paid key; the house's speech service and small model are everyone's.
    await add_key(session_factory, owner, CHAT_MODELS, home=False)
    await add_key(
        session_factory,
        owner,
        (HEAR_MODEL, PRINT_MODEL, SAY_MODEL, HOME_CHAT_MODEL),
        home=True,
        general=True,
    )
    desk = House(session_factory, settings, live_clock, owner, asks_a_proof())
    paul_model = FakeChatModel(tool_script=["Your phrase, then."], judge="proof: not sure")
    pauls = House(session_factory, settings, live_clock, paul, paul_model)
    try:
        c = await a_proof_is_asked(desk, desk.page(MIC), session_factory, owner)
        pauls.vault.phrase[phrase_ref(owner.id)] = PHRASE
        living = pauls.page("living-room")
        # Past the copy gap: a new sentence of Alex's, not a copy of the desk's (voice I2).
        live_clock.advance(timedelta(seconds=LATE_COPY_MARGIN_SECONDS + 1))
        # Alex at Paul's page, answered there: his window opens at that microphone.
        heard.speaker = voice_for(owner, at=paul)
        await living.hears("Hello.")
        await until(lambda: pauls.service.said == ["Your phrase, then."])
        await pauls.turns_over()
        assert pauls.vault.seen == []
        # Then his phrase, too short to tell and taken for the page's person, Paul: compared
        # first as Alex's, by a voice against him; the judge asks another proof.
        judged = len(paul_model.judge_calls())
        heard.speaker = too_short(paul)
        await living.hears(PHRASE)
        assert pauls.vault.seen == [PHRASE]
        [case] = paul_model.judge_calls()[judged:]
        assert {"kind": "voice", "weighs": "against"} in voice_clues_told(case)
        assert await status_of(session_factory, c) == "pending"
        assert pauls.woken.contents() == [{"proof": "another_proof"}]
        assert await said_by(session_factory, paul) == []
        assert all(m["sentence"] != PHRASE for m in pauls.attention.asked)
        for word in ("horses", "dawn"):
            assert word not in await written(session_factory)
            assert word not in model_saw(paul_model)
            assert word not in model_saw(desk.model)  # type: ignore[arg-type]
            assert word not in caplog.text
    finally:
        await desk.close()
        await pauls.close()


async def test_two_windows_at_one_microphone_are_both_weighed(
    house, heard, owner, paul, monkeypatch
):
    h = house(FakeChatModel(tool_script=["Yes?", "Yes, Paul?", "Of course."]))
    page = h.page(MIC)
    awaited = {owner.id: [uuid.uuid4()], paul.id: [uuid.uuid4()]}
    weighed: list[tuple[str, str, bool]] = []

    async def proofs_awaited(deps: Any, person: Person, now: datetime) -> list[uuid.UUID]:
        return awaited.get(person.id, [])

    async def give_the_phrase(
        deps: Any, confirmation_id: uuid.UUID, *, person: Person, heard: str, **kwargs: Any
    ) -> Weighed:
        weighed.append((person.display_name, heard, kwargs["doubted"]))
        return Weighed("not_an_answer")

    monkeypatch.setattr(session_module, "proofs_awaited", proofs_awaited)
    monkeypatch.setattr(session_module, "give_the_phrase", give_the_phrase)
    # Nobody spoken to here yet: no window, nothing weighed.
    await page.hears("Hello.")
    # Alex was answered here: his window is open; Paul's words are weighed against his phrase.
    heard.speaker = voice_for(paul, at=owner)
    await page.hears("Hi.")
    # Both were answered here: both windows are open, the next sentence is weighed for each.
    heard.speaker = None
    await page.hears("Turn on the light.")
    assert weighed == [
        ("Alex", "Hi.", True),
        ("Alex", "Turn on the light.", False),
        ("Paul", "Turn on the light.", True),
    ]
    # No answer to either: their usual ways.
    assert [m for m in await said_by(h.session_factory, paul) if m[0] == "user"] == [
        ("user", "Hi.", "complete")
    ]
    assert ("user", "Turn on the light.", "complete") in await said_by(h.session_factory, owner)


class _Listening:
    def says(self, reply_id: uuid.UUID) -> bool:
        return False

    async def turn_off(self) -> None:
        return None


def test_letting_someone_go_keeps_another_microphones_marks(live_clock):
    voices = LiveVoices()
    paul = uuid.uuid4()
    now = live_clock()
    # Paul was heard at Alex's desk (entered there); a saying of his is in hand in the kitchen.
    leave = voices.enter(paul, _Listening())
    voices.take_in_hand(paul, "kitchen")
    leave()  # the desk lets him go
    assert voices.taken_elsewhere(paul, MIC, now, now)
    voices.let_go(paul, "kitchen", closed_at=now)
    assert voices.a_copy(MIC, now, now)
    # The desk's page going forgets the desk's marks, never the kitchen's.
    voices.forget(MIC)
    assert voices.a_copy(MIC, now, now)
    voices.forget("kitchen")
    assert not voices.a_copy(MIC, now, now)


async def test_words_at_someone_elses_page_never_join_their_running_turn(
    house, heard, session_factory, owner, paul, live_clock, monkeypatch
):
    monkeypatch.setattr(session_module, "WRITTEN_CHECK_S", 0.01)
    h = house(FakeChatModel(tool_script=["Of course, Paul."]))
    page = h.page(MIC)
    # Paul's own turn runs elsewhere (his chat page), with his whole context.
    typed = page_speaker(paul.id, live_clock())
    conversation = Conversation(h.deps.turn)
    _, running = await conversation.say(paul, "MANGO-NOTE: the bonus is 5000", typed)
    assert running is not None
    h.deps.turn.streaming_replies.add(running.id)
    heard.speaker = voice_for(paul, at=owner)
    await page.hears("Turn on the light.")
    # Not kept for that turn, stored nowhere yet, nothing asked of a model, nothing said.
    assert "Turn on the light." not in [m[1] for m in await said_by(session_factory, paul)]
    assert not h.deps.turn.kept.take(running.conversation_id)
    assert h.model.calls == []
    # His turn over: the words are answered by a turn of their own, said here, told only of
    # this visit.
    async with session_factory() as writer:
        await ConversationStore(writer).finish_message(
            running.id, content="Noted.", status="complete"
        )
        await writer.commit()
    h.deps.turn.streaming_replies.discard(running.id)
    await until(lambda: h.service.said == ["Of course, Paul."])
    await h.turns_over()
    [call] = h.model.calls
    told = call.system + json.dumps([dump(m) for m in call.messages])
    assert "Turn on the light." in told
    assert "MANGO-NOTE" not in told
    assert ("assistant", "Of course, Paul.", "complete") in await said_by(session_factory, paul)


# --- final fix wave -----------------------------------------------------------------------------


class _FirstCallWaits:
    """A conversation model whose first main call says a sentence, waits for ``gate``, then calls
    ``tool``; every other call is ``then``'s."""

    def __init__(self, sentence: str, tool: ToolCall, then: FakeChatModel) -> None:
        self.sentence, self.tool, self.then = sentence, tool, then
        self.gate = asyncio.Event()
        self.started = False

    @property
    def calls(self) -> list[Any]:
        return self.then.calls

    async def stream(self, *, system: str, **rest: Any) -> AsyncGenerator[Any]:
        if not self.started and not is_judge_call(system) and not is_reader_call(system):
            self.started = True
            yield ChatChunk(self.sentence)
            await self.gate.wait()
            yield self.tool
            yield ChatUsage(3, 2)
            return
        async for item in self.then.stream(system=system, **rest):
            yield item


async def test_typed_words_never_join_a_turn_heard_at_someone_elses_page(
    house, heard, session_factory, owner, paul, live_clock
):
    """Paul heard at Alex's page: his turn there knows only this visit. What he types on his own
    page meanwhile is never read by that turn nor said aloud at Alex's page: it is answered after
    it, by a turn of his own."""
    model = _FirstCallWaits(
        "Let me look.\n",
        ToolCall("c1", "household_list", "{}"),
        FakeChatModel(tool_script=["Done here.", "About your note."]),
    )
    h = house(model)
    page = h.page(MIC)
    heard.speaker = voice_for(paul, at=owner)
    hearing = asyncio.create_task(page.hears("Who is here?"))
    await until(lambda: model.started)
    typed = page_speaker(paul.id, live_clock())
    _, reply = await Conversation(h.deps.turn).say(paul, "TYPED-NOTE: the bonus is 5000", typed)
    assert reply is None  # kept: the turn being written is the one heard at Alex's page
    model.gate.set()
    await hearing
    await h.turns_over()

    main = [c for c in model.calls if not is_judge_call(c.system) and not is_reader_call(c.system)]
    told = [c.system + json.dumps([dump(m) for m in c.messages]) for c in main]
    at_the_page = [text for text in told if "Who is here?" in text and "Done here." not in text]
    assert at_the_page and all("TYPED-NOTE" not in text for text in at_the_page)
    assert any("TYPED-NOTE" in text for text in told)  # answered after it, apart
    assert "About your note." not in h.service.said


async def _paul_runs_a_turn_elsewhere(h: House, paul: Person, live_clock: Any) -> Any:
    """Paul's own turn, typed on his chat page, is being written (with his whole context)."""
    typed = page_speaker(paul.id, live_clock())
    _, running = await Conversation(h.deps.turn).say(paul, "Something typed.", typed)
    assert running is not None
    h.deps.turn.streaming_replies.add(running.id)
    return running


async def test_words_waiting_at_someone_elses_page_are_answered_in_order(
    house, heard, session_factory, owner, paul, live_clock, monkeypatch
):
    monkeypatch.setattr(session_module, "WRITTEN_CHECK_S", 0.01)
    h = house(FakeChatModel(tool_script=["First.", "Second.", "Third."]))
    page = h.page(MIC)
    running = await _paul_runs_a_turn_elsewhere(h, paul, live_clock)
    heard.speaker = voice_for(paul, at=owner)
    for text in ("One.", "Two.", "Three."):
        await page.hears(text)
    async with session_factory() as writer:
        await ConversationStore(writer).finish_message(running.id, content="Ok.", status="complete")
        await writer.commit()
    h.deps.turn.streaming_replies.discard(running.id)
    await until(lambda: len(h.service.said) == 3)
    await h.turns_over()
    said = [text for role, text, _ in await said_by(session_factory, paul) if role == "user"]
    assert said == ["Something typed.", "One.", "Two.", "Three."]
    assert h.service.said == ["First.", "Second.", "Third."]


async def test_words_waiting_long_are_still_answered_once_his_turn_is_over(
    house, heard, session_factory, owner, paul, live_clock, monkeypatch
):
    # Case 11 of the final wave: never dropped for the time they waited.
    monkeypatch.setattr(session_module, "WRITTEN_CHECK_S", 0.01)
    h = house(FakeChatModel(tool_script=["Late, but here."]))
    page = h.page(MIC)
    running = await _paul_runs_a_turn_elsewhere(h, paul, live_clock)
    heard.speaker = voice_for(paul, at=owner)
    await page.hears("One.")
    live_clock.advance(timedelta(minutes=10))
    await asyncio.sleep(0.05)
    assert page.of("failed") == []
    async with session_factory() as writer:
        await ConversationStore(writer).finish_message(running.id, content="Ok.", status="complete")
        await writer.commit()
    h.deps.turn.streaming_replies.discard(running.id)
    await until(lambda: h.service.said == ["Late, but here."])
    await h.turns_over()


async def test_past_the_bound_of_words_waiting_a_new_one_is_not_taken(
    house, heard, session_factory, owner, paul, live_clock, monkeypatch
):
    monkeypatch.setattr(session_module, "WRITTEN_CHECK_S", 0.01)
    monkeypatch.setattr(session_module, "WAITING_LATER_AT_MOST", 1)
    h = house()
    page = h.page(MIC)
    await _paul_runs_a_turn_elsewhere(h, paul, live_clock)
    heard.speaker = voice_for(paul, at=owner)
    await page.hears("One.")
    assert page.of("failed") == []
    await page.hears("Two.")
    await until(lambda: bool(page.of("failed")))
    await page.session.close()


async def test_words_waiting_when_the_page_closes_leave_no_task_behind(
    house, heard, owner, paul, live_clock, monkeypatch
):
    monkeypatch.setattr(session_module, "WRITTEN_CHECK_S", 0.01)
    h = house()
    page = h.page(MIC)
    await _paul_runs_a_turn_elsewhere(h, paul, live_clock)
    heard.speaker = voice_for(paul, at=owner)
    await page.hears("One.")
    await page.session.close()
    waiting = [
        task
        for task in asyncio.all_tasks()
        if getattr(task.get_coro(), "__name__", "") == "_when_written"
    ]
    assert waiting == []


def _windows_of(
    monkeypatch, awaited: dict[uuid.UUID, list[uuid.UUID]], given: dict[uuid.UUID, str]
) -> list[tuple[str, str, bool]]:
    """Proofs awaited by phrase (``awaited``, read at each sentence) and what weighing a sentence
    gives for each person (``given``; else not an answer): what was weighed, for whom."""
    weighed: list[tuple[str, str, bool]] = []

    async def proofs_awaited(deps: Any, person: Person, now: datetime) -> list[uuid.UUID]:
        return awaited.get(person.id, [])

    async def give_the_phrase(
        deps: Any, confirmation_id: uuid.UUID, *, person: Person, heard: str, **kwargs: Any
    ) -> Weighed:
        weighed.append((person.display_name, heard, kwargs["doubted"]))
        return Weighed(given.get(person.id, "not_an_answer"))  # type: ignore[arg-type]

    monkeypatch.setattr(session_module, "proofs_awaited", proofs_awaited)
    monkeypatch.setattr(session_module, "give_the_phrase", give_the_phrase)
    return weighed


async def test_a_hold_on_one_phrase_never_stops_anothers_window(
    house, heard, owner, paul, monkeypatch
):
    h = house(FakeChatModel(tool_script=["Yes?", "Yes, Paul?", "Of course."]))
    page = h.page(MIC)
    awaited: dict[uuid.UUID, list[uuid.UUID]] = {}
    weighed = _windows_of(monkeypatch, awaited, {owner.id: "held_back", paul.id: "did_not_work"})
    await page.hears("Hello.")
    heard.speaker = voice_for(paul, at=owner)
    await page.hears("Hi.")
    # Both were answered here and both now await a proof by their phrase.
    awaited.update({owner.id: [uuid.uuid4()], paul.id: [uuid.uuid4()]})
    await page.hears("red kites fly low")
    # Alex's phrase is held back: Paul's is weighed all the same, and only Paul is told; a
    # sentence of Paul's never wakes Alex about his own hold.
    assert weighed == [("Alex", "red kites fly low", True), ("Paul", "red kites fly low", False)]
    assert [person_id for person_id, _ in h.woken.wakes] == [paul.id]


async def test_a_reply_being_said_opens_the_window_and_one_that_never_ends_closes_it(
    house, owner, live_clock, monkeypatch
):
    model = _FirstCallWaits(
        "Let me see.\n", ToolCall("c1", "household_list", "{}"), FakeChatModel()
    )
    h = house(model)
    page = h.page(MIC)
    weighed = _windows_of(monkeypatch, {owner.id: [uuid.uuid4()]}, {})
    await _heard_now(page, "Who is here?")
    await until(lambda: model.started)
    # His reply is being said (its turn never ends): the window is open.
    live_clock.advance(timedelta(seconds=10))
    await _heard_now(page, "Hello?")
    assert [text for _, text, _ in weighed] == ["Hello?"]
    # Silent for longer than any reply is said: it no longer holds the window open.
    live_clock.advance(timedelta(seconds=session_module.REPLY_QUIET_AT_MOST_S + 1))
    await _heard_now(page, "Anyone?")
    assert [text for _, text, _ in weighed] == ["Hello?"]
    model.gate.set()


async def test_what_he_says_on_his_own_here_opens_the_window(house, owner, monkeypatch):
    listeners = Listeners()
    h = house(FakeChatModel(tool_script=["Your parcel came.", "Of course."]), listeners=listeners)
    page = h.page(MIC)
    weighed = _windows_of(monkeypatch, {owner.id: [uuid.uuid4()]}, {})
    wake = (ToolWake(tool="voice_channel", content={"news": "parcel"}),)
    assert await tool_wake_turn(h.deps.turn, owner.id, wake)
    await h.turns_over()
    await until(lambda: h.service.said == ["Your parcel came."])
    await page.hears("Thanks.")
    assert [text for _, text, _ in weighed] == ["Thanks."]


async def _heard_now(page: Any, text: str) -> None:
    """``text`` heard at ``page`` and handled, without waiting for the turns it starts."""
    before = page.session.handled
    page.house.service.text = text
    await page.session.speech_began()
    await page.session.heard(sound())
    await until(lambda: page.session.handled > before)


async def test_closing_one_page_keeps_the_marks_of_another_page_on_the_same_microphone(
    house, paul, live_clock
):
    h = house()
    first, second = h.page(MIC), h.page(MIC)
    LIVE_VOICES.take_in_hand(paul.id, MIC)
    now = live_clock()
    await first.session.close()
    # The other page on this microphone still takes that saying: a copy elsewhere is dropped.
    assert LIVE_VOICES.a_copy("kitchen", now, now)
    await second.session.close()
    assert not LIVE_VOICES.a_copy("kitchen", now, now)


async def test_a_yes_raised_by_a_doubt_is_met_by_the_persons_own_sure_yes(
    house, heard, session_factory, owner, live_clock
):
    # Case 10 of the final wave: hoarse Alex's "yes" was raised to a proof; Alex then types "yes"
    # at their own signed-in page: that sure yes is enough.
    model = FakeChatModel(
        tool_script=[
            ToolCall("c1", "household_invite", json.dumps(INVITE_ROBIN)),
            "Shall I?",
            "Your phrase, then.",
            "Done.",
        ],
        judge=["yes: a first time", "proof: a short voice"],
    )
    h = house(model)
    page = h.page(MIC)
    c = await a_request_waits(page, session_factory)
    heard.speaker = too_short(owner)
    await page.hears("yes")
    async with session_factory() as reader:
        found = await reader.get(Confirmation, c.id, populate_existing=True)
    assert found is not None
    assert (found.status, found.ask, found.ask_before_doubt) == ("pending", "proof", "yes")
    typed = page_speaker(owner.id, live_clock())
    conversation = Conversation(h.deps.turn)
    _, reply = await conversation.say(owner, "yes", typed)
    assert reply is not None
    await conversation.answer(owner, reply.id, TurnRequest(typed, PAGE), Collected())
    await h.turns_over()
    assert await status_of(session_factory, c) == "executed"


async def test_a_sure_voice_at_someone_elses_page_acts_with_its_own_rights(
    session_factory, owner, add_member, live_clock
):
    # Case 6 of the final wave: Alex, sure (a proof given here), at Tom's page: Alex's own rights,
    # not the stricter of Alex's and Tom's.
    tom = await add_member("Tom", "child")
    look = DescribedAction("core", "net.look", None, "read", "household")
    async with session_factory() as session:
        at_toms_page = await decide_now(
            session,
            Actor(owner.id, "owner", standing="sure", page_person_id=tom.id),
            look,
            DEFAULT_POLICY,
            live_clock(),
        )
    assert not isinstance(at_toms_page, Refuse)


def a_doubted_request_then_a_typed_yes() -> FakeChatModel:
    return FakeChatModel(
        tool_script=[
            ToolCall("c1", "household_invite", json.dumps(INVITE_ROBIN)),
            "Your phrase, then.",
            "Done.",
        ],
        judge=["proof: a short voice"],
    )


async def test_a_proof_asked_at_the_request_for_a_doubted_requester_is_met_by_a_sure_yes(
    house, heard, session_factory, owner, live_clock
):
    # Case 10, at creation: the judge asked a proof only because the voice that asked was too
    # short to tell; the person then types "yes" at their own signed-in page: that is enough.
    h = house(a_doubted_request_then_a_typed_yes())
    page = h.page(MIC)
    heard.speaker = too_short(owner)
    await page.hears("Invite Robin as a guest.")
    async with session_factory() as reader:
        [c] = (await reader.execute(select(Confirmation))).scalars().all()
    assert (c.status, c.ask, c.asked_by, c.ask_before_doubt) == ("pending", "proof", "judge", "yes")
    typed = page_speaker(owner.id, live_clock())
    conversation = Conversation(h.deps.turn)
    _, reply = await conversation.say(owner, "yes", typed)
    assert reply is not None
    await conversation.answer(owner, reply.id, TurnRequest(typed, PAGE), Collected())
    await h.turns_over()
    assert await status_of(session_factory, c) == "executed"


async def test_a_proof_the_judge_asks_of_a_sure_requester_stays(house, session_factory, owner):
    # Not doubted: a proof the judge asked for the action itself is not lowered by any yes.
    h = house(a_doubted_request_then_a_typed_yes())
    page = h.page(MIC)
    await page.hears("Invite Robin as a guest.")
    async with session_factory() as reader:
        [c] = (await reader.execute(select(Confirmation))).scalars().all()
    assert (c.ask, c.asked_by, c.ask_before_doubt) == ("proof", "judge", None)


def test_an_ordinary_sentence_is_a_copy_only_if_it_began_with_the_one_taken():
    # Final fix wave B, I-4: two pages hear one sentence together; a sentence that began later
    # at another microphone, the same person's or another's, is its own.
    from domo_core.channels.voice.live import LIVE_SENTENCES

    alex, paul = uuid.uuid4(), uuid.uuid4()
    t0 = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    gap = timedelta(seconds=LATE_COPY_MARGIN_SECONDS)
    try:
        LIVE_SENTENCES.take_in_hand(alex, "office", began=t0)
        later = t0 + timedelta(seconds=10)
        # The same sentence heard in the kitchen too: a copy.
        assert LIVE_SENTENCES.taken_elsewhere(alex, "kitchen", t0 + gap / 2, later)
        # A new sentence begun there later while the first is still in hand: not a copy.
        assert not LIVE_SENTENCES.taken_elsewhere(alex, "kitchen", t0 + 3 * gap, later)
        assert not LIVE_SENTENCES.taken_elsewhere(paul, "kitchen", t0 + 3 * gap, later)
        LIVE_SENTENCES.let_go(alex, "office", closed_at=t0 + timedelta(seconds=20))
        after = t0 + timedelta(seconds=21)
        # Once stored: a sentence begun with it is still its copy, one begun later is not.
        assert LIVE_SENTENCES.taken_elsewhere(alex, "kitchen", t0, after)
        assert not LIVE_SENTENCES.taken_elsewhere(alex, "kitchen", t0 + 15 * gap, after)
        assert not LIVE_SENTENCES.a_copy("kitchen", t0 + 15 * gap, after)
    finally:
        LIVE_SENTENCES.forget("office")
