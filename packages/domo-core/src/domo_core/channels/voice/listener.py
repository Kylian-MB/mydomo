"""What Domo says is said aloud: words become sentences, sentences become sound (spec 6.7).

``VoiceListener`` is the ``Listener`` of one reply on the voice channel. The words (``Words``) are
cut into sentences (``SentenceCutter``) as they come; a card is said by its sentence
(``conversation.cards.card_sentence``) and a decision by the sentence the session gives
(``decision_sentence``); a failure by the text of the person's own key ``chat.error.<reason>`` (the
text the page shows: the code writes no sentence). What a card shows once (``record.card_once``) is
never said: it stays on the page's card.

One worker per reply takes the sentences in order, asks the speech client (``speech``) for the
sound of each and gives it to the page through ``sink``: ``{"type": "say", "reply", "index",
"rate"}``, the sound as binary frames (24 000 Hz, 16-bit, mono), ``{"type": "say_end", "reply",
"index"}`` (the messages are defined once, in ``channels.voice.protocol``). A
sentence that is being turned into sound is abandoned at once by ``stop`` (the stream to the
service is closed, the call settled); the page ducks the sound by itself (``duck`` only tells it)
and raises it again on ``resume``.

A voice that cannot speak (no route, a service still getting ready, a limit) never breaks the turn:
the words stay written in the discussion, the sink is told once, by a typed message
``{"type": "speech_unavailable", "reason"}`` that the page puts in words, and the rest of the reply
goes on silently.

``reading`` is true while the socket is open and the microphone is on (``sink.open``) and nothing of
the reply was cut: ``stop`` before the last sentence was sent, or the page telling that what it
played was not whole (``played``). ``emit(Ended)`` waits until what was queued is sent (or dropped),
so that the note of the delivery (``conversation.service._Delivering``) is true: whole, or cut.

No sound and no sentence is written anywhere: nothing of either is logged.
"""

import asyncio
import logging
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import aclosing, suppress
from typing import Final, Protocol
from uuid import UUID

from domo_core.channels.voice import protocol
from domo_core.channels.voice.sentences import Fence, SentenceCutter, speakable
from domo_core.conversation.cards import card_sentence as plain_card_sentence
from domo_core.conversation.channel import ListenerGone
from domo_core.conversation.events import Acted, Decided, Ended, Event, Failed, Words
from domo_core.llm.speech.client import SpeechError
from domo_core.llm.types import JsonObject
from domo_core.policy.catalog import shown
from domo_core.texts.view import Texts

logger = logging.getLogger(__name__)

# How long a reply that is being said may go on with no progress (no sentence finished and no sound
# sent) before it is given up: a half-open socket, or a voice that never answers, must not hold the
# turn. Each sentence finished and each chunk sent starts the time again, so a long reply that keeps
# producing sound is said to its end. Past it the reply is noted cut.
STALL_SECONDS: Final = 30.0
# How long telling the page to stop may take, when the reply was given up.
STOP_WAIT_SECONDS: Final = 2.0
# A ceiling for one sentence besides the stall bound: a chunk sent counts as progress however slow
# the socket, so a client taking the sound drop by drop would hold the turn for ever. A sentence has
# a base plus a budget per character. Generous on purpose for a slow voice at home (a processor with
# no graphics card says a character in a fraction of a second, several times slower than the speech
# itself lasts): 300 characters get 20 + 90 s. Past it the reply is cut as for a stall (on a very
# slow link a long sentence is cut).
SENTENCE_BASE_SECONDS: Final = 20.0
SENTENCE_SECONDS_PER_CHAR: Final = 0.3


class SinkClosed(Exception):
    """The page cannot receive any more: its socket is closed."""


class VoiceSink(Protocol):
    """Where the page is reached: one socket, JSON messages and binary sound frames."""

    @property
    def open(self) -> bool:
        """Whether the socket is open and the microphone is on."""
        ...

    async def send_json(self, data: JsonObject) -> None:
        """Raises ``SinkClosed``."""
        ...

    async def send_bytes(self, data: bytes) -> None:
        """Raises ``SinkClosed``."""
        ...


class Speech(Protocol):
    """What turns a sentence into sound (``llm.speech.metered.MeteredSpeech``). Read it as
    ``async with aclosing(speech.say(...))``."""

    def say(
        self, text: str, *, voice: str | None, language: str | None
    ) -> AsyncGenerator[bytes]: ...


# Gives the speech client of the route for ``say``, or ``None`` when no voice can speak.
OpenSpeech = Callable[[], Awaitable[Speech | None]]
# The sentence of a decision the person's words came to, or ``None`` for nothing to say.
DecisionSentence = Callable[[Decided], Awaitable[str | None]]
# A card in words, for this channel (what the person is said, never a button the voice cannot show).
CardInWords = Callable[[Mapping[str, object], Texts], str | None]


class VoiceListener:
    """The listener of one reply said aloud."""

    def __init__(
        self,
        sink: VoiceSink,
        *,
        reply_id: UUID,
        texts: Texts,
        speech: OpenSpeech,
        heard_language: str | None,
        person_language: str | None,
        house_language: str,
        decision_sentence: DecisionSentence,
        voice: str | None = None,
        card_in_words: CardInWords = plain_card_sentence,
    ) -> None:
        self._sink = sink
        self._reply_id = reply_id
        self._texts = texts
        self._open_speech = speech
        # The language of what is said: the utterance's, else the person's, else the house's.
        self._language = heard_language or person_language or house_language
        self._decision_sentence = decision_sentence
        self._card_in_words = card_in_words
        self._fence = Fence()
        self._voice = voice
        # The first piece of the reply is said sooner: it may end at a pause (final fix wave C).
        self._cutter = SentenceCutter(early_first=True)
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        self._current: asyncio.Task[None] | None = None
        self._speech: Speech | None = None
        self._speech_asked = False
        self._muted = False  # told the page it cannot speak: the rest is silent
        self._stopped = False
        self._cut = False
        self._gone = False
        self._ended = False
        self._pending = 0  # sentences queued and not yet sent or dropped
        self._idle = asyncio.Event()  # nothing waits to be sent, or nobody can receive any more
        self._idle.set()
        self._index = 0
        self._progress = time.monotonic()  # the last sentence finished or chunk sent

    @property
    def reading(self) -> bool:
        return self._sink.open and not self._cut and not self._gone

    async def open(self) -> None:
        self._start()

    async def emit(self, event: Event) -> None:
        if self._gone or not self._sink.open:
            current = self._current
            self._go_away()
            if current is not None:
                await asyncio.wait({current})  # its stream to the service is closed
            raise ListenerGone
        self._start()
        match event:
            case Words(text=text):
                self._queue_words(self._cutter.feed(text))
            case Acted(record=record):
                self._end_of_words()
                if record.action_type is None or shown(record.action_type, record.status):
                    # Only the card: what is shown once is on the page's card, never said.
                    self._queue_all([self._guarded(self._card_in_words, record.card, self._texts)])
            case Decided():
                self._end_of_words()
                self._queue_all([await self._decision(event)])
            case Failed(reason=reason):
                self._end_of_words()
                self._queue_all([self._failure(reason)])
            case Ended():
                self._queue_words(self._cutter.flush())
                if self._fence.open:
                    self._cut = True  # most of the reply was code: it was not said
                self._ended = True
                await self._wait_for_the_end()
                if self._gone:
                    raise ListenerGone

    def played(self, *, whole: bool) -> None:
        """The page tells whether it played the reply to its end (``{"type": "played"}``): what it
        did not play was cut."""
        if not whole:
            self._cut = True

    async def duck(self) -> None:
        """Someone speaks over the voice: the page lowers the sound (a mechanical gesture)."""
        await self._send(protocol.duck())

    async def stop(self) -> None:
        """Drop what was not said, abandon the sentence being turned into sound, tell the page."""
        if not (self._ended and self._pending == 0 and not self._cutter.pending):
            self._cut = True
        self._drop()
        self._stopped = True
        await self._send(protocol.stop())

    async def resume(self) -> None:
        """Speak again from now on, and the page raises the sound."""
        self._stopped = False
        await self._send(protocol.resume())

    async def close(self) -> None:
        """The socket is closed: nobody can receive any more; stop the worker, release the end."""
        self._go_away()
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.wait({self._worker})

    def _drop(self) -> None:
        """Forget what waits to be said and abandon the sentence being turned into sound."""
        self._cutter.flush()
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._pending -= 1
            self._queue.task_done()
        if self._current is not None:
            self._current.cancel()
        self._release()

    def _go_away(self) -> None:
        """Nobody can receive any more: what waits is dropped, the end is released."""
        self._gone = True
        self._drop()

    def _release(self) -> None:
        if self._pending <= 0 or self._gone:
            self._idle.set()

    async def _wait_for_the_end(self) -> None:
        """Wait until what was queued is sent, dropped, or nobody can receive it; give up when
        ``STALL_SECONDS`` pass with no progress: the reply is then cut, the sentence in flight is
        abandoned (and awaited), and the page is told to stop if it answers."""
        while not self._idle.is_set():
            left = STALL_SECONDS - (time.monotonic() - self._progress)
            if left <= 0:
                break
            with suppress(TimeoutError):
                await asyncio.wait_for(self._idle.wait(), left)
        else:
            return
        logger.warning("A reply stalled while it was being said")
        await self._give_up()

    async def _give_up(self) -> None:
        """The reply is cut: what waits is dropped, the sentence in flight is abandoned and awaited
        (its stream is closed), and the page is told to stop if it answers."""
        self._cut = True
        current = self._current
        self._drop()
        self._stopped = True
        if current is not None:
            await asyncio.wait({current})
        try:
            async with asyncio.timeout(STOP_WAIT_SECONDS):
                await self._send(protocol.stop())
        except TimeoutError:
            pass

    def _end_of_words(self) -> None:
        """A segment of the model's words is over (a card, a decision or a failure follows): what
        is left is said, a block of code still open was not said (the reply is cut), and the
        next segment starts clean."""
        self._queue_words(self._cutter.flush())
        if self._fence.open:
            self._cut = True
        self._fence.reset()

    # --- what is said -------------------------------------------------------------------------

    def _failure(self, reason: str) -> str | None:
        return self._guarded(self._texts, f"chat.error.{reason}")

    @staticmethod
    def _guarded[**P](
        read: Callable[P, str | None], *args: P.args, **kwargs: P.kwargs
    ) -> str | None:
        """What ``read`` gives, or nothing when it raises: a sentence that cannot be read is not
        said and never breaks the turn (only the kind of error is logged)."""
        try:
            return read(*args, **kwargs)
        except Exception as error:
            logger.warning("A sentence could not be read (%s)", type(error).__name__)
            return None

    async def _decision(self, event: Decided) -> str | None:
        try:
            return await self._decision_sentence(event)
        except Exception as error:
            logger.warning("A decision could not be put in words (%s)", type(error).__name__)
            return None

    def _queue_words(self, sentences: list[str]) -> None:
        """The model's words: what lies in code is not said."""
        self._queue_all([self._fence.outside(sentence) for sentence in sentences])

    def _queue_all(self, sentences: list[str] | list[str | None]) -> None:
        for sentence in sentences:
            if sentence is None or self._stopped or self._muted or self._gone:
                continue
            text = speakable(sentence)
            if text:
                if self._pending == 0:
                    self._progress = time.monotonic()  # the clock starts with the first one
                self._pending += 1
                self._idle.clear()
                self._queue.put_nowait(text)

    async def _send(self, data: JsonObject) -> None:
        try:
            await self._sink.send_json(data)
        except SinkClosed:
            self._go_away()
        except Exception as error:
            logger.warning("The page could not be reached (%s)", type(error).__name__)
            self._go_away()

    # --- the worker ---------------------------------------------------------------------------

    def _start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._work())

    async def _work(self) -> None:
        while True:
            sentence = await self._queue.get()
            try:
                if not (self._stopped or self._gone or self._muted):
                    await self._say_one(sentence)
            except Exception as error:
                logger.warning("The voice stopped (%s)", type(error).__name__)
                self._go_away()
            finally:
                self._pending -= 1
                self._queue.task_done()
                self._progress = time.monotonic()
                self._release()

    async def _say_one(self, sentence: str) -> None:
        index = self._index
        self._index += 1
        task = asyncio.create_task(self._speak(sentence, index))
        self._current = task
        error: BaseException | None = None
        ceiling = SENTENCE_BASE_SECONDS + SENTENCE_SECONDS_PER_CHAR * len(sentence)
        try:
            done, _ = await asyncio.wait({task}, timeout=ceiling)
            if not done:
                logger.warning("A sentence took too long to be said")
                await self._give_up()
        finally:
            self._current = None
            if not task.done():
                task.cancel()
                await asyncio.wait({task})
            if not task.cancelled():
                error = task.exception()  # always read, so that none is left unretrieved
        if error is None:
            return
        if isinstance(error, SinkClosed):
            self._go_away()
            return
        logger.warning("A sentence could not be said (%s)", type(error).__name__)
        await self._unavailable(error.reason if isinstance(error, SpeechError) else "failed")

    async def _speak(self, sentence: str, index: int) -> None:
        if not self._speech_asked:
            self._speech_asked = True
            try:
                self._speech = await self._open_speech()
            except Exception as error:
                logger.warning("The voice could not be opened (%s)", type(error).__name__)
                self._speech = None
        if self._speech is None:
            await self._unavailable("no_model")
            return
        began = False
        try:
            async with aclosing(
                self._speech.say(sentence, voice=self._voice, language=self._language)
            ) as chunks:
                async for chunk in chunks:
                    if not began:
                        began = True
                        await self._sink.send_json(protocol.say(self._reply_id, index))
                    await self._sink.send_bytes(chunk)
                    self._progress = time.monotonic()
        except SinkClosed:
            raise
        except Exception as error:
            if began:
                # The page never waits on an index that does not end.
                await self._sink.send_json(protocol.say_end(self._reply_id, index))
            if isinstance(error, SpeechError):
                await self._unavailable(error.reason)
                return
            raise
        if began:
            await self._sink.send_json(protocol.say_end(self._reply_id, index))

    async def _unavailable(self, reason: str) -> None:
        """Tell the page, once, that the voice cannot speak; the rest of the reply is silent."""
        if self._muted:
            return
        self._muted = True
        await self._send(protocol.speech_unavailable(reason))
