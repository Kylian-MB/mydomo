"""Helpers of the tests of the voice session (Task 14): a speech service, a vault and a page that
stand in for the real ones, the keys of a household that hears at home, and the session's
dependencies on them.

No real model and no stack: the speech service is ``FakeSpeechService`` (it answers what it is
told and keeps nothing of the sound but its length), the gatekeeper is ``FakeVault`` (the phrase,
and where a key leads), the page is ``FakePage``. The conversation is the real one, with a
``FakeChatModel``; the small model at home that weighs whether a sentence is for the assistant is
``FakeAttention`` (``mine`` by default, and he goes on when he was speaking), and the sorting of
what was heard has a ``FakeChatModel`` of its own (``HouseModel`` sends each call to its own).
"""

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator, Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, cast

import httpx
from conversation_support import turn_deps
from domo_common.gatekeeper_api import PhraseEnrolled, PhraseResult
from domo_core.accounts.models import Person
from domo_core.accounts.throttle import LoginThrottle
from domo_core.channels.voice.deps import MeteredSpeechOpener, VoiceDeps
from domo_core.channels.voice.listener import SinkClosed
from domo_core.channels.voice.live import LIVE_VOICES
from domo_core.channels.voice.session import VoiceSession
from domo_core.channels.voice.stranger import STRANGERS
from domo_core.chat.models import Message
from domo_core.config import CoreSettings
from domo_core.conversation.listeners import Listeners
from domo_core.conversation.tool_wakes import WakeEnd
from domo_core.db import SessionFactory
from domo_core.gatekeeper_client import GatekeeperError, PhraseHeldBack
from domo_core.llm.models import Provider, ProviderModel, new_provider_id
from domo_core.llm.router import Route
from domo_core.llm.speech.client import Heard, Print, PrintThresholds, SpeechClient, SpeechError
from domo_core.llm.types import (
    ChatChunk,
    ChatModel,
    ChatUsage,
    HistoryItem,
    JsonObject,
    ModelCallError,
    ToolCall,
    ToolChoice,
    ToolDefinition,
)
from domo_core.prompts import ToolWake, attention_prompt, heard_sorting_prompt, whose_voice_prompt
from fakes import FakeChatModel
from sqlalchemy import select

HEAR_MODEL = "Systran/faster-whisper-small"
PRINT_MODEL = "wespeaker-voxceleb"
SAY_MODEL = "kokoro-82m"
CHAT_MODELS = ("example-haiku-1", "example-sonnet-1", "example-opus-1")
# A small chat model the house serves itself: the quick judgement and the sorting of what is heard.
HOME_CHAT_MODEL = "example-home-1"
THRESHOLDS = PrintThresholds(print_low=0.25, same_voice=0.50, print_high=0.75)
VOICE = (1.0, 0.0, 0.0, 0.0)
# A second of sound, 16 kHz, 16 bits.
SECOND = 32_000
WAIT = 10.0


def sound(seconds: float = 2.0) -> bytes:
    return b"\x01\x00" * int(seconds * SECOND / 2)


class FakeSpeechService:
    """The speech service of the house as the core's client reaches it (``SpeechClient``): it
    hears ``text`` in ``language``, prints the voice as ``vector`` and says a sentence as one
    chunk. It keeps nothing of a sound but its length (``heard``), and notes the key of each
    call (``routes``)."""

    def __init__(self) -> None:
        self.text = "Turn on the light."
        self.language = "en"
        self.vector: tuple[float, ...] = VOICE
        self.seconds = 2.0
        self.hear_fails: SpeechError | None = None
        self.print_fails: Exception | None = None
        self.calls: list[str] = []
        self.routes: list[tuple[str, str]] = []
        self.heard: list[int] = []
        self.said: list[str] = []
        self.languages: list[str | None] = []
        self.together = False  # the words wait until the print began: they run at once
        self._printing = asyncio.Event()

    async def transcribe(self, route: Route, pcm: bytes, *, language: str | None = None) -> Heard:
        self.calls.append("transcribe")
        self.routes.append(("hear", route.provider.id))
        self.heard.append(len(pcm))
        if self.together:
            async with asyncio.timeout(WAIT):
                await self._printing.wait()
        if self.hear_fails is not None:
            raise self.hear_fails
        return Heard(self.text, self.language, len(pcm) / SECOND, ())

    async def voice_print(self, route: Route, pcm: bytes) -> Print:
        self.calls.append("voice_print")
        self.routes.append(("voice_print", route.provider.id))
        self._printing.set()
        if self.print_fails is not None:
            raise self.print_fails
        return Print(self.vector, PRINT_MODEL, self.seconds, None, None, None)

    async def say(
        self, route: Route, text: str, *, voice: str | None, language: str | None
    ) -> AsyncGenerator[bytes]:
        self.calls.append("say")
        self.said.append(text)
        self.languages.append(language)
        yield b"\x00\x00"

    async def print_thresholds(
        self, provider: Provider, *, at_home: bool
    ) -> dict[str, PrintThresholds]:
        return {PRINT_MODEL: THRESHOLDS}


class FakeAttention:
    """The small model at home as the quick judgement asks it: what it was given (``asked``, the
    moment as data) and what it answers (``answer``: a line, or a function of the moment; by
    default ``mine``, and ``resume`` when the assistant was speaking), after ``delay_s``."""

    def __init__(self) -> None:
        self.asked: list[dict[str, Any]] = []
        self.answer: str | Callable[[dict[str, Any]], str] | None = None
        self.delay_s = 0.0

    def line_for(self, moment: dict[str, Any]) -> str:
        if callable(self.answer):
            return self.answer(moment)
        if self.answer is not None:
            return self.answer
        return "mine resume" if moment["speaking_at"] else "mine"

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
        [message] = messages
        moment = json.loads(cast(Any, message).content)
        self.asked.append(moment)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        yield ChatChunk(self.line_for(moment))
        yield ChatUsage(3, 2)


class FakeWhose:
    """The small model at home as the question "is this still the page's person?" asks it: what
    it was given (``asked``) and what it answers (``answer``, ``someone_else`` by default: the
    stranger's path as before); ``down``: it fails."""

    def __init__(self) -> None:
        self.asked: list[dict[str, Any]] = []
        self.answer = "someone_else"
        self.down = False

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
        [message] = messages
        self.asked.append(json.loads(cast(Any, message).content))
        if self.down:
            raise ModelCallError("unreachable")
        yield ChatChunk(self.answer)
        yield ChatUsage(3, 1)


class HouseModel:
    """The models of the house's keys: each call goes to the quick judgement's, the question of
    whose voice it is, the sorting's or the conversation's, by its instructions."""

    def __init__(
        self,
        conversation: ChatModel,
        attention: ChatModel,
        sorting: ChatModel,
        whose: ChatModel | None = None,
    ) -> None:
        self.conversation = conversation
        self.attention = attention
        self.sorting = sorting
        self.whose = whose or cast(ChatModel, FakeWhose())

    def stream(
        self,
        *,
        system: str,
        messages: Sequence[HistoryItem],
        max_output_tokens: int,
        tools: Sequence[ToolDefinition] = (),
        tool_choice: ToolChoice = "auto",
        think: bool = True,
    ) -> AsyncGenerator[ChatChunk | ToolCall | ChatUsage]:
        if system.startswith(attention_prompt()):
            model = self.attention
        elif system.startswith(whose_voice_prompt()):
            model = self.whose
        elif system.startswith(heard_sorting_prompt()):
            model = self.sorting
        else:
            model = self.conversation
        return model.stream(
            system=system,
            messages=messages,
            max_output_tokens=max_output_tokens,
            tools=tools,
            tool_choice=tool_choice,
            think=think,
        )


class FakeVault:
    """The gatekeeper as the voice reaches it: where a key leads, and the spoken phrase (kept in
    memory here; ``phrase`` is the one agreed). ``seen`` is what it was sent, to check where
    the words of a phrase went."""

    def __init__(self) -> None:
        self.phrase: dict[str, str] = {}
        self.pending: dict[str, str] = {}
        self.seen: list[str] = []
        self.held_back: float | None = None
        self.down = False
        self.unreachable = False  # a transport error, not wrapped as the gatekeeper's

    async def phrase_remove(self, ref: str) -> bool:
        return self.phrase.pop(ref, None) is not None

    async def provider_zone(self, provider_id: str) -> str:
        return "house"

    async def phrase_enrol(self, ref: str, heard: str) -> PhraseEnrolled:
        if self.unreachable:
            raise httpx.ConnectError("no route")
        if self.down:
            raise GatekeeperError("POST /internal/phrases/{ref}/enrol", status=503)
        self.seen.append(heard)
        if len(heard.split()) < 3:
            return PhraseEnrolled(step="too_short")
        first = self.pending.pop(ref, None)
        if first is None:
            self.pending[ref] = heard
            return PhraseEnrolled(step="first_kept")
        if first != heard:
            return PhraseEnrolled(step="not_the_same")
        self.phrase[ref] = heard
        return PhraseEnrolled(step="agreed")

    async def phrase_compare(self, ref: str, heard: str) -> PhraseResult:
        if self.down:
            raise GatekeeperError("POST /internal/phrases/{ref}/compare", status=503)
        self.seen.append(heard)
        if self.held_back is not None:
            raise PhraseHeldBack(self.held_back)
        kept = self.phrase.get(ref)
        if kept is None:
            return "nothing_like"
        if heard == kept:
            return "it_is"
        if heard.split()[:2] == kept.split()[:2]:
            return "almost"
        return "nothing_like"


class FakePage:
    """The page's socket (``VoiceSink``): what was sent, in order."""

    def __init__(self) -> None:
        self.open = True
        self.sent: list[tuple[str, object]] = []

    async def send_json(self, data: JsonObject) -> None:
        if not self.open:
            raise SinkClosed
        self.sent.append(("json", data))

    async def send_bytes(self, data: bytes) -> None:
        if not self.open:
            raise SinkClosed
        self.sent.append(("bytes", data))

    def messages(self) -> list[JsonObject]:
        return [cast(JsonObject, data) for kind, data in self.sent if kind == "json"]

    def of(self, kind: str) -> list[JsonObject]:
        return [message for message in self.messages() if message["type"] == kind]


@dataclass
class Woken:
    """The wakes asked of the assistant: ``(person id, what the tools say)``."""

    wakes: list[tuple[uuid.UUID, list[ToolWake]]] = field(default_factory=lambda: [])
    runs: bool = True  # whether the turn could run (``False``: busy, no key)
    says: bool = True  # whether a turn that runs writes words (``False``: it ran silently)

    async def __call__(self, person_id: uuid.UUID, wakes: Sequence[ToolWake]) -> WakeEnd:
        self.wakes.append((person_id, list(wakes)))
        if not self.runs:
            return "not_run"
        return "spoke" if self.says else "ran"

    def contents(self) -> list[object]:
        return [wake.content for _, wakes in self.wakes for wake in wakes]


class ClosesOnListeningOff(FakePage):
    """The page's socket as the page uses it: on "listening off" the page closes it at once. The
    worst case of that race: the session's worker never gets past that send (the close cancels
    it there)."""

    def __init__(self) -> None:
        super().__init__()
        self.session: VoiceSession | None = None
        self.closing: asyncio.Task[None] | None = None

    async def send_json(self, data: JsonObject) -> None:
        await super().send_json(data)
        if data == {"type": "listening", "on": False} and self.session is not None:
            self.open = False
            self.closing = asyncio.create_task(self.session.close())
            await asyncio.Event().wait()  # until the close cancels the worker


def closing_page(h: "House", person: Person, microphone: str) -> ClosesOnListeningOff:
    """A page of ``person`` at ``microphone`` whose socket closes on "listening off"."""
    socket = ClosesOnListeningOff()
    socket.session = VoiceSession(
        h.deps,
        person_id=person.id,
        microphone=microphone,
        origin="https://domo.example.com/",
        sink=socket,
    )
    return socket


async def add_key(
    session_factory: SessionFactory,
    giver: Person,
    models: Sequence[str],
    *,
    home: bool,
    general: bool = False,
) -> Provider:
    """A key of ``giver`` listing ``models``: a server of the house with no secret (``home``), or
    a paid one with a key in the vault. Committed."""
    provider_id = new_provider_id()
    row = Provider(
        id=provider_id,
        kind="openai_compatible",
        base_url="http://192.168.1.10:8000/v1" if home else "https://llm.example.com/v1",
        secret_ref=None if home else f"provider:{provider_id}:api_key",
        given_by=giver.id,
        is_general=general,
    )
    async with session_factory() as writer:
        writer.add(row)
        await writer.flush()
        writer.add_all(ProviderModel(provider_id=provider_id, model_id=m) for m in models)
        await writer.commit()
    return row


async def a_household_that_hears_at_home(session_factory: SessionFactory, owner: Person) -> None:
    """The keys of most houses: a paid key for the conversation, and the speech service at home
    with a small chat model beside it (the quick judgement, the sorting of what is heard)."""
    await add_key(session_factory, owner, CHAT_MODELS, home=False, general=True)
    await add_key(
        session_factory, owner, (HEAR_MODEL, PRINT_MODEL, SAY_MODEL, HOME_CHAT_MODEL), home=True
    )


async def home_check(provider: Provider) -> bool:
    """Where a key leads, as the gatekeeper would say: the keys of these tests with no secret are
    at home."""
    return provider.secret_ref is None


def voice_deps(
    *,
    session_factory: SessionFactory,
    settings: CoreSettings,
    clock: Callable[[], datetime],
    service: FakeSpeechService,
    vault: FakeVault,
    model: ChatModel,
    wake: Woken | None = None,
    listeners: Listeners | None = None,
) -> VoiceDeps:
    """What a session needs, on the real conversation with ``model``, the fake speech service and
    vault, and ``wake`` (none: the real wakes of the assistant). The sessions all share
    ``LIVE_VOICES``, which the tool that stops the listening reaches too."""
    turn = turn_deps(
        session_factory=session_factory,
        model=model,
        settings=settings,
        gatekeeper=cast(Any, vault),
        clock=clock,
    )
    turn = replace(turn, listeners=listeners)
    speech = MeteredSpeechOpener(
        cast(SpeechClient, service),
        session_factory=session_factory,
        clock=clock,
        at_home=home_check,
    )
    return VoiceDeps(turn=turn, speech=speech, throttle=LoginThrottle(), wake=wake)


class CountingSession(VoiceSession):
    """A ``VoiceSession`` that counts the moments of speech it handled, whatever became of them
    (``handled``): what a test waits on."""

    handled = 0

    async def _handle(self) -> None:
        try:
            await super()._handle()
        finally:
            self.handled += 1


class Page:
    """One listening page of the household's person: its session and its socket."""

    def __init__(self, house: "House", microphone: str) -> None:
        self.house = house
        self.microphone = microphone
        self.socket = FakePage()
        self.session = CountingSession(
            house.deps,
            person_id=house.person.id,
            microphone=microphone,
            origin="https://domo.example.com/",
            sink=self.socket,
        )

    async def hears(self, text: str) -> None:
        """``text`` heard here (begun now, as the route tells it), and handled to its end (its
        turn and its wakes included)."""
        await self.begins()
        await self.ends(text)

    async def begins(self) -> None:
        """The page tells someone began to speak (its sound is handed on later)."""
        await self.session.speech_began()

    async def ends(self, text: str) -> None:
        """The moment that began is handed on now, heard as ``text``, and handled to its end."""
        before = self.session.handled
        self.house.service.text = text
        await self.session.heard(sound())
        await until(lambda: self.session.handled > before)
        await self.house.turns_over()

    def of(self, kind: str) -> list[JsonObject]:
        return self.socket.of(kind)


class House:
    """The person's voice in a household that hears at home: one speech service, one vault, one
    conversation (``model``), the wakes asked of the assistant, and their listening pages."""

    def __init__(
        self,
        session_factory: SessionFactory,
        settings: CoreSettings,
        clock: Callable[[], datetime],
        person: Person,
        model: ChatModel,
        *,
        listeners: Listeners | None = None,
        real_wakes: bool = False,
    ) -> None:
        self.session_factory = session_factory
        self.clock = clock
        self.person = person
        self.model = model
        self.service = FakeSpeechService()
        self.vault = FakeVault()
        self.woken = Woken()
        self.live = LIVE_VOICES
        self.attention = FakeAttention()
        self.sorting = FakeChatModel(["done"])
        self.whose = FakeWhose()
        self.models = HouseModel(
            model, cast(ChatModel, self.attention), self.sorting, cast(ChatModel, self.whose)
        )
        self.deps = voice_deps(
            session_factory=session_factory,
            settings=settings,
            clock=clock,
            service=self.service,
            vault=self.vault,
            model=self.models,
            wake=None if real_wakes else self.woken,
            listeners=listeners,
        )
        self.pages: list[Page] = []

    def sorts_with(self, model: FakeChatModel) -> None:
        """The sorting of what is heard is ``model``'s from now on."""
        self.sorting = model
        self.models.sorting = model

    def page(self, microphone: str = "desk-1") -> Page:
        found = Page(self, microphone)
        self.pages.append(found)
        return found

    async def turns_over(self) -> None:
        await until(lambda: self.deps.turn.wakes.pending == 0)

    async def close(self) -> None:
        for page in self.pages:
            await page.session.close()
        await self.deps.turn.wakes.stop(5.0)
        # The exchanges with voices nobody knows live in the process: none goes to the next test.
        STRANGERS.clear()

    async def messages(self) -> list[tuple[str, str, str]]:
        return [
            (m.role, m.content, m.status)
            for m in await messages_of(self.session_factory, self.person)
        ]

    async def said(self) -> list[str]:
        """What the person said, in order."""
        return [text for role, text, _ in await self.messages() if role == "user"]


async def messages_of(session_factory: SessionFactory, person: Person) -> list[Message]:
    """Every message of ``person``'s conversations, oldest first."""
    from domo_core.chat.models import Conversation

    async with session_factory() as reader:
        found = await reader.execute(
            select(Message)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(Conversation.person_id == person.id)
            .order_by(Message.created_at, Message.id)
        )
        return list(found.scalars())


async def until(check: Callable[[], bool]) -> None:
    async with asyncio.timeout(WAIT):
        while not check():
            await asyncio.sleep(0.01)
