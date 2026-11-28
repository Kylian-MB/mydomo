"""What Domo says is said aloud, sentence by sentence, on the voice channel (spec 6.7)."""

import asyncio
import uuid
from collections.abc import AsyncGenerator
from typing import Any, cast

import pytest
from domo_common.i18n import Language
from domo_core.channels.voice import listener as listener_module
from domo_core.channels.voice.listener import SinkClosed, VoiceListener
from domo_core.chat.agent import ActionRecord
from domo_core.conversation.channel import ListenerGone
from domo_core.conversation.events import Acted, Decided, Ended, Failed, Words
from domo_core.conversation.service import _Delivering  # pyright: ignore[reportPrivateUsage]
from domo_core.llm.speech.client import SpeechError
from domo_core.llm.types import JsonObject
from domo_core.texts.view import CatalogTexts

REPLY = uuid.UUID(int=7)


class FakeSink:
    """The socket of the page: what was sent, in order."""

    def __init__(self) -> None:
        self.open = True
        self.sent: list[tuple[str, object]] = []
        self.slow = 0.0  # the time each send takes
        self.hangs = False  # a half-open socket: a send never returns
        self.breaks: Exception | None = None  # a send raises this

    async def _check(self) -> None:
        if not self.open:
            raise SinkClosed
        if self.breaks is not None:
            raise self.breaks
        if self.hangs:
            await asyncio.Event().wait()
        if self.slow:
            await asyncio.sleep(self.slow)

    async def send_json(self, data: JsonObject) -> None:
        await self._check()
        self.sent.append(("json", data))

    async def send_bytes(self, data: bytes) -> None:
        await self._check()
        self.sent.append(("bytes", data))

    def types(self) -> list[str]:
        return [str(cast(JsonObject, d)["type"]) for kind, d in self.sent if kind == "json"]

    def sound(self) -> list[bytes]:
        return [cast(bytes, d) for kind, d in self.sent if kind == "bytes"]


class FakeSpeech:
    """A speech client: each sentence comes back as two chunks naming it. A sentence that is a
    key of ``hold`` waits for its event before its second chunk."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, str | None]] = []
        self.hold: dict[str, asyncio.Event] = {}
        self.started = asyncio.Event()
        self.fail: SpeechError | None = None
        self.fail_midway: Exception | None = None
        self.closed = 0  # how many streams were closed
        self.chunks = 0  # more chunks than the two of a sentence
        self.delay = 0.0  # the time a sentence takes before its sound starts

    async def say(
        self, text: str, *, voice: str | None, language: str | None
    ) -> AsyncGenerator[bytes]:
        self.calls.append((text, voice, language))
        self.started.set()
        try:
            await asyncio.sleep(self.delay)
            if self.fail is not None:
                raise self.fail
            yield b"a:" + text.encode()
            for n in range(self.chunks):
                yield b"c:" + str(n).encode()
            if self.fail_midway is not None:
                raise self.fail_midway
            if text in self.hold:
                await self.hold[text].wait()
            yield b"b:" + text.encode()
        finally:
            self.closed += 1


def listener_of(
    sink: FakeSink,
    speech: FakeSpeech | None,
    *,
    heard: str | None = "fr",
    person: str | None = None,
    house: str = "en",
    lang: Language = "en",
    decision_fails: bool = False,
    **more: Any,
) -> VoiceListener:
    async def open_speech() -> FakeSpeech | None:
        return speech

    async def decision(event: Decided) -> str | None:
        if decision_fails:
            raise RuntimeError("the sentence of a decision could not be read")
        return "La demande est acceptée."

    return VoiceListener(
        sink,
        reply_id=REPLY,
        texts=CatalogTexts(lang),
        speech=open_speech,
        heard_language=heard,
        person_language=person,
        house_language=house,
        decision_sentence=decision,
        **more,
    )


def record_of(
    card: JsonObject, *, once: JsonObject | None = None, status: str = "done"
) -> ActionRecord:
    return ActionRecord(
        tool_name="household_list",
        action_type=None,
        status=status,  # pyright: ignore[reportArgumentType]
        confirmation_id=None,
        model_result={},
        card=card,
        card_once=once,
    )


async def test_words_are_said_sentence_by_sentence_in_order():
    sink, speech = FakeSink(), FakeSpeech()
    listener = listener_of(sink, speech)
    await listener.open()
    await listener.emit(Words("Bonjour tout le monde. Je suis "))
    await listener.emit(Words("là pour toi aujourd'hui."))
    await listener.emit(Ended("complete"))
    assert [c[0] for c in speech.calls] == [
        "Bonjour tout le monde.",
        "Je suis là pour toi aujourd'hui.",
    ]
    assert sink.types() == ["say", "say_end", "say", "say_end"]
    assert sink.sound() == [
        b"a:Bonjour tout le monde.",
        b"b:Bonjour tout le monde.",
        "a:Je suis là pour toi aujourd'hui.".encode(),
        "b:Je suis là pour toi aujourd'hui.".encode(),
    ]
    assert sink.sent[0][1] == {"type": "say", "reply": str(REPLY), "index": 0, "rate": 24_000}
    assert sink.sent[-1][1] == {"type": "say_end", "reply": str(REPLY), "index": 1}


async def test_the_first_piece_of_a_reply_is_said_early_at_a_comma():
    # Final fix wave C: his first word comes sooner; what is shown as text is not touched here.
    sink, speech = FakeSink(), FakeSpeech()
    listener = listener_of(sink, speech)
    await listener.emit(Words("Je regarde ce que tu as dans ton agenda demain, et je "))
    await listener.emit(Words("te dis tout, avec les heures. Puis, la suite, enfin."))
    await listener.emit(Ended("complete"))
    assert [c[0] for c in speech.calls] == [
        "Je regarde ce que tu as dans ton agenda demain,",
        "et je te dis tout, avec les heures.",
        "Puis, la suite, enfin.",
    ]


async def test_what_is_only_for_the_eye_is_not_said():
    sink, speech = FakeSink(), FakeSpeech()
    listener = listener_of(sink, speech)
    await listener.emit(Words("- **Voici** le lien https://example.org/x pour toi."))
    await listener.emit(Ended("complete"))
    assert [c[0] for c in speech.calls] == ["Voici le lien pour toi."]


async def test_a_card_is_said_by_its_sentence():
    sink, speech = FakeSink(), FakeSpeech()
    listener = listener_of(sink, speech, heard="en")
    card: JsonObject = {"key": "action.card.done.household.list", "params": {}}
    await listener.emit(Words("Je regarde qui est là."))
    await listener.emit(Acted(record_of(card)))
    await listener.emit(Acted(record_of(card, status="waiting")))
    await listener.emit(Ended("complete"))
    assert [c[0] for c in speech.calls] == [
        "Je regarde qui est là.",
        "The assistant looked at who is in the household.",
        "The assistant looked at who is in the household.",
    ]


async def test_a_silent_action_or_an_unreadable_card_is_not_said():
    sink, speech = FakeSink(), FakeSpeech()
    listener = listener_of(sink, speech, heard="en")
    silent = ActionRecord(
        tool_name="memory_keep",
        action_type="memory.keep",
        status="done",
        confirmation_id=None,
        model_result={},
        card={"key": "action.card.done.household.list", "params": {}},
    )
    await listener.emit(Acted(silent))
    await listener.emit(Acted(record_of({"key": "no.such.key", "params": {}})))
    await listener.emit(Ended("complete"))
    assert speech.calls == []
    assert sink.types() == []


async def test_what_is_shown_once_is_never_said():
    sink, speech = FakeSink(), FakeSpeech()
    listener = listener_of(sink, speech, heard="en")
    card: JsonObject = {"key": "action.card.done.household.list", "params": {}}
    once: JsonObject = {
        "key": "action.card.done.household.invite",
        "params": {"name": "Sam", "code": "SECRET-123"},
        "link": "https://example.org/join/SECRET-123",
    }
    await listener.emit(Acted(record_of(card, once=once)))
    await listener.emit(Ended("complete"))
    said = " ".join(c[0] for c in speech.calls)
    assert "SECRET-123" not in said and "example.org" not in said
    assert b"SECRET" not in b"".join(sink.sound())
    assert len(speech.calls) == 1


async def test_a_decision_is_said_by_its_sentence():
    sink, speech = FakeSink(), FakeSpeech()
    listener = listener_of(sink, speech)
    await listener.emit(Decided(uuid.uuid4(), None, "executed"))
    await listener.emit(Ended("complete"))
    assert [c[0] for c in speech.calls] == ["La demande est acceptée."]


async def test_the_reply_is_said_in_the_language_heard():
    sink, speech = FakeSink(), FakeSpeech()
    listener = listener_of(sink, speech, heard="fr", person="en", house="de")
    await listener.emit(Words("Bonjour tout le monde."))
    await listener.emit(Ended("complete"))
    assert speech.calls[0][2] == "fr"


async def test_without_a_heard_language_the_persons_then_the_houses():
    sink, speech = FakeSink(), FakeSpeech()
    listener = listener_of(sink, speech, heard=None, person="nl", house="de")
    await listener.emit(Words("Hello there my friend."))
    await listener.emit(Ended("complete"))
    assert speech.calls[0][2] == "nl"

    sink, speech = FakeSink(), FakeSpeech()
    listener = listener_of(sink, speech, heard=None, person=None, house="de")
    await listener.emit(Words("Hello there my friend."))
    await listener.emit(Ended("complete"))
    assert speech.calls[0][2] == "de"


async def test_stop_drops_what_was_not_said_and_the_note_says_cut():
    sink, speech = FakeSink(), FakeSpeech()
    speech.hold["Première phrase ici."] = asyncio.Event()
    listener = listener_of(sink, speech)
    notes: list[str] = []
    delivering = _Delivering(listener, notes.append)  # pyright: ignore[reportArgumentType]
    await delivering.open()
    await delivering.emit(Words("Première phrase ici. Deuxième phrase là. "))
    async with asyncio.timeout(2):
        await speech.started.wait()
    await listener.stop()
    assert sink.types()[-1] == "stop"
    await delivering.emit(Words("Troisième phrase encore."))
    await delivering.emit(Ended("complete"))
    assert [c[0] for c in speech.calls] == ["Première phrase ici."]
    assert notes == ["cut"]
    assert "say_end" not in sink.types()
    assert speech.closed == 1  # the stream to the service was closed at once


async def test_a_reply_said_to_its_end_notes_whole():
    sink, speech = FakeSink(), FakeSpeech()
    listener = listener_of(sink, speech)
    notes: list[str] = []
    delivering = _Delivering(listener, notes.append)  # pyright: ignore[reportArgumentType]
    await delivering.emit(Words("Une phrase qui se dit en entier."))
    await delivering.emit(Ended("complete"))
    assert notes == ["whole"]


async def test_played_whole_notes_whole_and_played_cut_notes_cut():
    for whole, expected in ((True, "whole"), (False, "cut")):
        sink, speech = FakeSink(), FakeSpeech()
        listener = listener_of(sink, speech)
        notes: list[str] = []
        delivering = _Delivering(listener, notes.append)  # pyright: ignore[reportArgumentType]
        await delivering.emit(Words("Une phrase qui se dit en entier."))
        listener.played(whole=whole)
        await delivering.emit(Ended("complete"))
        assert notes == [expected]


async def test_a_failed_turn_is_said_with_his_own_text_of_that_failure():
    sink, speech = FakeSink(), FakeSpeech()
    listener = listener_of(sink, speech, heard="en", lang="en")
    await listener.emit(Failed("upstream"))
    await listener.emit(Ended("error"))
    assert [c[0] for c in speech.calls] == [
        "The model provider returned an error. Try again in a moment."
    ]


async def test_no_voice_available_says_nothing_and_fails_nothing():
    sink = FakeSink()
    listener = listener_of(sink, None)
    await listener.emit(Words("Bonjour tout le monde. Encore une phrase ici."))
    await listener.emit(Ended("complete"))
    assert sink.types() == ["speech_unavailable"]
    assert sink.sound() == []


async def test_a_speech_that_fails_says_so_once_and_goes_on_silently():
    sink, speech = FakeSink(), FakeSpeech()
    speech.fail = SpeechError("preparing")
    listener = listener_of(sink, speech)
    await listener.emit(Words("Bonjour tout le monde. Encore une phrase ici."))
    await listener.emit(Ended("complete"))
    assert sink.sent == [("json", {"type": "speech_unavailable", "reason": "preparing"})]
    assert len(speech.calls) == 1


async def test_what_he_says_on_his_own_is_said_when_the_page_listens():
    sink, speech = FakeSink(), FakeSpeech()
    listener = listener_of(sink, speech, heard=None, person="fr")
    assert listener.reading
    await listener.open()
    await listener.emit(Words("Tom a laissé un mot pour toi."))
    await listener.emit(Ended("complete"))
    assert [c[0] for c in speech.calls] == ["Tom a laissé un mot pour toi."]
    sink.open = False
    assert not listener.reading


async def test_a_listener_whose_socket_closed_raises_listener_gone():
    sink, speech = FakeSink(), FakeSpeech()
    listener = listener_of(sink, speech)
    sink.open = False
    with pytest.raises(ListenerGone):
        await listener.emit(Words("Bonjour tout le monde."))
    # Closed in the middle of the sound: the stream is closed, and the end says so.
    sink2, speech2 = FakeSink(), FakeSpeech()
    speech2.hold["Une première phrase ici."] = asyncio.Event()
    listener2 = listener_of(sink2, speech2)
    await listener2.emit(Words("Une première phrase ici. "))
    async with asyncio.timeout(2):
        await speech2.started.wait()
    sink2.open = False
    speech2.hold["Une première phrase ici."].set()
    with pytest.raises(ListenerGone):
        await listener2.emit(Words("Encore."))
    with pytest.raises(ListenerGone):
        await listener2.emit(Ended("complete"))
    assert speech2.closed == 1


async def test_a_socket_that_closes_while_sentences_wait_releases_the_end():
    sink, speech = FakeSink(), FakeSpeech()
    sink.hangs = True
    listener = listener_of(sink, speech)
    await listener.emit(Words("Première phrase ici. Deuxième phrase là. "))
    async with asyncio.timeout(2):
        await speech.started.wait()
    ending = asyncio.create_task(listener.emit(Ended("complete")))
    await asyncio.sleep(0.05)
    assert not ending.done()
    await listener.close()
    async with asyncio.timeout(2):
        with pytest.raises(ListenerGone):
            await ending
    assert speech.closed == 1


async def test_a_sink_that_fails_in_another_way_does_not_hang_the_end():
    sink, speech = FakeSink(), FakeSpeech()
    sink.breaks = RuntimeError("boom")
    listener = listener_of(sink, speech)
    await listener.emit(Words("Première phrase ici. Deuxième phrase là. "))
    async with asyncio.timeout(2):
        with pytest.raises(ListenerGone):
            await listener.emit(Ended("complete"))
    assert not listener.reading


async def test_a_sink_that_stops_answering_mid_reply_is_given_up_and_noted_cut(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(listener_module, "STALL_SECONDS", 0.1)
    monkeypatch.setattr(listener_module, "STOP_WAIT_SECONDS", 0.05)
    sink, speech = FakeSink(), FakeSpeech()
    listener = listener_of(sink, speech)
    notes: list[str] = []
    delivering = _Delivering(listener, notes.append)  # pyright: ignore[reportArgumentType]
    await delivering.emit(Words("Première phrase ici. Deuxième phrase là. Troisième encore ici."))
    sink.hangs = True  # the socket stops answering after the first sentence started
    async with asyncio.timeout(3):
        await delivering.emit(Ended("complete"))
    assert notes == ["cut"]
    assert speech.closed >= 1  # the stream in flight was closed before the end returned


async def test_a_long_reply_that_keeps_producing_sound_is_said_to_its_end(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(listener_module, "STALL_SECONDS", 0.15)
    sink, speech = FakeSink(), FakeSpeech()
    speech.delay = 0.06
    listener = listener_of(sink, speech)
    notes: list[str] = []
    delivering = _Delivering(listener, notes.append)  # pyright: ignore[reportArgumentType]
    words = " ".join(f"La phrase numéro {n} est dite ici." for n in range(6))
    await delivering.emit(Words(words))
    async with asyncio.timeout(5):
        await delivering.emit(Ended("complete"))  # six sentences take more than the bound in all
    assert notes == ["whole"]
    assert len(speech.calls) == 6
    assert sink.types().count("say_end") == 6


async def test_an_open_code_fence_silences_the_words_only_and_notes_cut():
    sink, speech = FakeSink(), FakeSpeech()
    listener = listener_of(sink, speech, heard="en")
    notes: list[str] = []
    delivering = _Delivering(listener, notes.append)  # pyright: ignore[reportArgumentType]
    card: JsonObject = {"key": "action.card.done.household.list", "params": {}}
    await delivering.emit(Words("Voici ce que je fais. ```py x = 1. Et encore une suite."))
    await delivering.emit(Acted(record_of(card)))
    await delivering.emit(Decided(uuid.uuid4(), None, "executed"))
    await delivering.emit(Failed("upstream"))
    await delivering.emit(Ended("error"))
    assert [c[0] for c in speech.calls] == [
        "Voici ce que je fais.",
        "The assistant looked at who is in the household.",
        "La demande est acceptée.",
        "The model provider returned an error. Try again in a moment.",
    ]
    assert notes == ["cut"]


async def test_inline_code_that_spans_a_cut_is_not_said():
    sink, speech = FakeSink(), FakeSpeech()
    listener = listener_of(sink, speech)
    await listener.emit(Words("Lance `rm -rf. puis encore` maintenant ici. "))
    await listener.emit(Ended("complete"))
    said = " ".join(c[0] for c in speech.calls)
    assert "rm" not in said and "encore" not in said
    assert "maintenant ici." in said


async def test_nothing_is_queued_once_nobody_can_receive():
    sink, speech = FakeSink(), FakeSpeech()
    listener = listener_of(sink, speech)
    await listener.close()
    listener._queue_all(["Une phrase qui ne partira pas."])  # pyright: ignore[reportPrivateUsage]
    assert listener._pending == 0  # pyright: ignore[reportPrivateUsage]


async def test_the_clock_does_not_run_while_nothing_is_queued(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(listener_module, "STALL_SECONDS", 0.1)
    # The model works for longer than the bound, then says one sentence.
    sink, speech = FakeSink(), FakeSpeech()
    listener = listener_of(sink, speech)
    notes: list[str] = []
    delivering = _Delivering(listener, notes.append)  # pyright: ignore[reportArgumentType]
    await delivering.open()
    await asyncio.sleep(0.25)
    await delivering.emit(Words("Une phrase après un long travail."))
    await delivering.emit(Ended("complete"))
    assert notes == ["whole"]
    assert "stop" not in sink.types()
    # A long gap between two sentences.
    sink, speech = FakeSink(), FakeSpeech()
    listener = listener_of(sink, speech)
    notes = []
    delivering = _Delivering(listener, notes.append)  # pyright: ignore[reportArgumentType]
    await delivering.emit(Words("Une première phrase ici. "))
    await asyncio.sleep(0.25)
    await delivering.emit(Words("Une seconde phrase là."))
    await delivering.emit(Ended("complete"))
    assert notes == ["whole"]
    assert "stop" not in sink.types()


async def test_a_sentence_that_goes_on_for_ever_drop_by_drop_is_cut(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(listener_module, "STALL_SECONDS", 1.0)
    monkeypatch.setattr(listener_module, "SENTENCE_BASE_SECONDS", 0.15)
    monkeypatch.setattr(listener_module, "SENTENCE_SECONDS_PER_CHAR", 0.0)
    monkeypatch.setattr(listener_module, "STOP_WAIT_SECONDS", 0.05)
    sink, speech = FakeSink(), FakeSpeech()
    sink.slow = 0.03  # every chunk is accepted, slowly, under the stall bound
    speech.chunks = 1000
    listener = listener_of(sink, speech)
    notes: list[str] = []
    delivering = _Delivering(listener, notes.append)  # pyright: ignore[reportArgumentType]
    await delivering.emit(Words("Une phrase qui ne finit jamais."))
    async with asyncio.timeout(3):
        await delivering.emit(Ended("complete"))
    assert notes == ["cut"]
    assert speech.closed == 1
    assert sink.types()[-1] == "stop"


async def test_a_decision_that_cannot_be_read_is_not_said_and_breaks_nothing():
    sink, speech = FakeSink(), FakeSpeech()
    listener = listener_of(sink, speech, decision_fails=True)
    await listener.emit(Decided(uuid.uuid4(), None, "executed"))
    await listener.emit(Words("Une phrase qui se dit."))
    await listener.emit(Ended("complete"))
    assert [c[0] for c in speech.calls] == ["Une phrase qui se dit."]


async def test_a_card_is_said_as_the_channel_puts_it_in_words():
    sink, speech = FakeSink(), FakeSpeech()
    seen: list[str] = []

    def in_words(card: JsonObject, texts: object) -> str | None:
        seen.append(str(card["key"]))
        return "Un bouton est sur la page."

    listener = listener_of(sink, speech, card_in_words=in_words)
    card: JsonObject = {"key": "action.card.done.household.list", "params": {}}
    await listener.emit(Acted(record_of(card)))
    await listener.emit(Ended("complete"))
    assert seen == ["action.card.done.household.list"]
    assert [c[0] for c in speech.calls] == ["Un bouton est sur la page."]

    def broken(card: JsonObject, texts: object) -> str | None:
        raise RuntimeError("unreadable")

    sink, speech = FakeSink(), FakeSpeech()
    listener = listener_of(sink, speech, card_in_words=broken)
    await listener.emit(Acted(record_of(card)))
    await listener.emit(Ended("complete"))
    assert speech.calls == []


async def test_a_failure_in_the_middle_of_a_sentence_closes_its_index():
    for error in (SpeechError("failed"), RuntimeError("boom")):
        sink, speech = FakeSink(), FakeSpeech()
        speech.fail_midway = error
        listener = listener_of(sink, speech)
        await listener.emit(Words("Une phrase qui se coupe en route."))
        await listener.emit(Ended("complete"))
        assert sink.types() == ["say", "say_end", "speech_unavailable"]
        assert speech.closed == 1


async def test_a_card_between_two_segments_of_words_does_not_leave_the_next_silenced():
    sink, speech = FakeSink(), FakeSpeech()
    listener = listener_of(sink, speech, heard="en")
    notes: list[str] = []
    delivering = _Delivering(listener, notes.append)  # pyright: ignore[reportArgumentType]
    card: JsonObject = {"key": "action.card.done.household.list", "params": {}}
    await delivering.emit(Words("Je commence ici. ```py x = 1"))
    await delivering.emit(Acted(record_of(card)))
    await delivering.emit(Words("Et voici la suite à dire."))
    await delivering.emit(Ended("complete"))
    assert [c[0] for c in speech.calls] == [
        "Je commence ici.",
        "The assistant looked at who is in the household.",
        "Et voici la suite à dire.",
    ]
    assert notes == ["cut"]  # a part of the words was code, and was not said
