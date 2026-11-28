"""A person at their own signed-in page is never made a stranger by the voice score alone (audit
case 5): when the voice reads like nobody known, the quick model is asked one small question
(``channels.voice.whose``) before the stranger's path."""

import json
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterator
from datetime import datetime
from typing import Any, cast

import pytest
from domo_core.accounts.models import Person
from domo_core.channels.voice import session as session_module
from domo_core.channels.voice.stranger import STRANGERS
from domo_core.channels.voice.whose import read_answer
from domo_core.chat.models import Message
from domo_core.confirmations.models import Confirmation
from domo_core.conversation.speaker import Clue, Speaker
from domo_core.llm.types import ToolCall
from domo_core.proofs.kinds import PROOF_KINDS, SpokenPhraseProof, phrase_ref
from domo_core.proofs.service import agree
from fakes import FakeChatModel
from sqlalchemy import select
from voice_session_support import (
    FakeWhose,
    House,
    a_household_that_hears_at_home,
    messages_of,
    until,
)

MIC = "desk-1"
PHRASE = "blue horses sing at dawn"
STRANGER_SAYS = json.dumps({"say": "Hello.", "note": "A voice I did not know spoke."})


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


def nobody(now: datetime) -> Speaker:
    """A voice far from the page's person and like nobody known (the row "nobody")."""
    return Speaker(
        None,
        "voice",
        clues=(Clue("voice", "against", "weak", "code", now, degree=0.1),),
        microphone=MIC,
    )


class Heard:
    """Whether the next sentences read like nobody known (else as the voice itself says), and
    what was given to the learning."""

    def __init__(self) -> None:
        self.nobody = False
        self.learnt: list[bool] = []


@pytest.fixture
def heard(monkeypatch) -> Heard:
    found = Heard()
    real_speaker, real_learn = session_module.voice_speaker, session_module.learn

    def voice_speaker(**kwargs: Any) -> Speaker:
        return nobody(kwargs["now"]) if found.nobody else real_speaker(**kwargs)

    async def learn(*args: Any, **kwargs: Any) -> Any:
        found.learnt.append(kwargs["sure_otherwise"])
        return await real_learn(*args, **kwargs)

    monkeypatch.setattr(session_module, "voice_speaker", voice_speaker)
    monkeypatch.setattr(session_module, "learn", learn)
    return found


@pytest.fixture
async def house(session_factory, settings, live_clock, owner):
    await a_household_that_hears_at_home(session_factory, owner)
    made: list[House] = []

    def make(model: FakeChatModel) -> House:
        found = House(session_factory, settings, live_clock, owner, model)
        made.append(found)
        return found

    yield make
    for each in made:
        await each.close()


def invites(judge: list[str]) -> FakeChatModel:
    """Lights first (ordinary), then an invitation (sensitive), which the judge weighs."""
    return FakeChatModel(
        tool_script=[
            "The light is on.",
            ToolCall(
                "c1", "household_invite", json.dumps({"display_name": "Tom", "role": "guest"})
            ),
            "Say your phrase.",
        ],
        judge=judge,
    )


def stranger_calls(model: FakeChatModel) -> list[Any]:
    return [
        c for c in model.calls if c.system.startswith("You are the assistant of a household, heard")
    ]


async def heard_unsure(session_factory, text: str) -> bool | None:
    async with session_factory() as reader:
        return await reader.scalar(select(Message.heard_unsure).where(Message.content == text))


def test_only_theirs_is_theirs():
    assert read_answer("theirs") is True
    assert read_answer(" Theirs.\n") is True
    for other in ("someone_else", "", "I think theirs", "not theirs"):
        assert read_answer(other) is False


async def test_a_hoarse_page_person_carries_on_unsure_and_a_sensitive_ask_is_judged(
    house, heard, session_factory, owner
):
    h = house(invites(["proof: the voice is not theirs"]))
    page = h.page(MIC)
    await page.hears("Turn on the light.")
    heard.nobody = True
    h.whose.answer = "theirs"
    await page.hears("And invite Tom as a guest.")
    # The question was given the page's person, the microphone and the talk in progress here.
    [asked] = h.whose.asked
    assert (asked["page_person"], asked["microphone"]) == ("Alex", MIC)
    assert asked["sentence"] == "And invite Tom as a guest."
    assert [line["text"] for line in asked["exchanged"]] == [
        "Turn on the light.",
        "The light is on.",
    ]
    assert asked["last_minutes_ago"] == 0
    # In their own conversation, as an unsure speaker: the sensitive thing went to the judge.
    said = [m.content for m in await messages_of(session_factory, owner) if m.role == "user"]
    assert said == ["Turn on the light.", "And invite Tom as a guest."]
    assert await heard_unsure(session_factory, "And invite Tom as a guest.") is True
    async with session_factory() as reader:
        [c] = (await reader.execute(select(Confirmation))).scalars().all()
    assert (c.status, c.ask, c.asked_by) == ("pending", "proof", "judge")
    [judged] = h.model.judge_calls()
    case = json.dumps([m.content for m in judged.messages if hasattr(m, "content")])
    assert '\\"against\\"' in case or '"against"' in case
    assert stranger_calls(h.model) == []
    # Nothing of that sentence's voice was learnt for them.
    assert heard.learnt[-1] is False


async def test_someone_else_takes_the_strangers_path(house, heard, session_factory, owner):
    h = house(FakeChatModel([STRANGER_SAYS]))
    page = h.page(MIC)
    heard.nobody = True
    h.whose.answer = "someone_else"
    await page.hears("Who lives here?")
    assert len(h.whose.asked) == 1
    assert len(stranger_calls(h.model)) == 1
    assert await messages_of(session_factory, owner) == []


async def test_a_silent_or_garbled_question_takes_the_strangers_path(
    house, heard, session_factory, owner
):
    h = house(FakeChatModel([STRANGER_SAYS, STRANGER_SAYS]))
    page = h.page(MIC)
    heard.nobody = True
    h.whose.down = True
    await page.hears("Who lives here?")
    h.whose.down = False
    h.whose.answer = "hmm, maybe"
    await page.hears("Who lives here, then?")
    assert len(h.whose.asked) == 2
    assert len(stranger_calls(h.model)) == 2
    assert await messages_of(session_factory, owner) == []


class AskedWhileAnotherTakes(FakeWhose):
    """The question answers ``theirs``, but only once ``meanwhile`` ran: what another microphone
    does while the question is asked here."""

    def __init__(self, meanwhile: Callable[[], Awaitable[None]]) -> None:
        super().__init__()
        self.answer = "theirs"
        self._meanwhile: Callable[[], Awaitable[None]] | None = meanwhile

    async def stream(self, **kwargs: Any) -> AsyncGenerator[Any]:
        meanwhile, self._meanwhile = self._meanwhile, None
        if meanwhile is not None:
            await meanwhile()
        async for chunk in super().stream(**kwargs):
            yield chunk


async def test_a_sentence_another_microphone_took_while_asked_whose_is_never_taken_twice(
    house, heard, session_factory, owner
):
    # Final fix wave C (B review I-1): two pages of Alex's in one room. The laptop reads the
    # hoarse voice like nobody known and asks whose it is; meanwhile the tablet takes the very
    # same sentence as Alex's and answers it. "theirs" then must not take it a second time.
    h = house(FakeChatModel(["Milk is on the list.", "Milk is on the list again."]))
    laptop, tablet = h.page(MIC), h.page("tablet-1")
    said = "Add milk to the shopping list."

    async def the_tablet_takes_it() -> None:
        heard.nobody = False
        await tablet.hears(said)

    h.models.whose = cast(Any, AskedWhileAnotherTakes(the_tablet_takes_it))
    heard.nobody = True
    await laptop.hears(said)
    mine = [m.content for m in await messages_of(session_factory, owner) if m.role == "user"]
    assert mine == [said]


async def test_a_sentence_not_said_to_him_is_never_asked_whose_nor_named_the_page_persons(
    house, heard, session_factory, owner
):
    # Final fix wave C (B review I-2): the television, two visitors talking. A sentence read like
    # nobody known that the quick judgement finds not said to him goes as before case 5: no
    # question of whose voice it is, and sorted as nobody known's, never as the page person's.
    h = house(FakeChatModel(["unused"]))
    page = h.page(MIC)
    heard.nobody = True
    h.whose.answer = "theirs"
    h.attention.answer = "not_mine"
    await page.hears("The dentist moved to Friday.")
    assert h.whose.asked == []
    assert len(h.attention.asked) == 1
    [line] = page.session._unaddressed.take(h.clock())  # noqa: SLF001 - in memory only
    assert line.who is None and line.sure is False
    assert await messages_of(session_factory, owner) == []


async def test_the_question_of_whose_voice_comes_after_the_quick_judgement(
    house, heard, session_factory, owner
):
    # Asked only for a sentence the quick judgement found said to him (final fix wave C, I-2).
    h = house(FakeChatModel(["The light is on."]))
    page = h.page(MIC)
    order: list[str] = []
    h.attention.answer = lambda moment: order.append("attention") or "mine"
    real = h.whose.stream

    def whose_stream(**kwargs: Any) -> AsyncGenerator[Any]:
        order.append("whose")
        return real(**kwargs)

    h.whose.stream = whose_stream  # type: ignore[method-assign]
    heard.nobody = True
    h.whose.answer = "theirs"
    await page.hears("Turn on the light.")
    assert order == ["attention", "whose"]
    said = [m.content for m in await messages_of(session_factory, owner) if m.role == "user"]
    assert said == ["Turn on the light."]


async def test_a_phrase_in_an_open_window_never_reaches_the_question(
    house, heard, session_factory, owner
):
    h = house(
        FakeChatModel(
            tool_script=[ToolCall("c1", "profile_rename", '{"name": "Alex B"}'), "Say it."],
            judge="proof: not sure",
        )
    )
    page = h.page(MIC)
    async with session_factory() as writer:
        await agree(writer, owner.id, "spoken_phrase", proposed_by="person", now=h.clock())
        await writer.commit()
    h.vault.phrase[phrase_ref(owner.id)] = PHRASE
    await page.hears("Call me Alex B.")
    await until(lambda: bool(page.of("say_end")))
    heard.nobody = True
    h.whose.answer = "theirs"
    await page.hears(PHRASE)
    # Weighed by the code against the window: no model ever saw it.
    assert all(PHRASE not in json.dumps(asked) for asked in h.whose.asked)
    assert h.whose.asked == []
