"""A voice nobody of the household has, at someone's page: a short exchange, and a word left to
the person of the page (spec 6.5, "Une voix inconnue devant la page de quelqu'un").

Where a record will be opened: once "Domo crée les personnes" is there, this is where the
assistant may open a record for this voice (``record_open`` is his call, with his own reason,
spec 6.10). Until then no record is opened: the voice has its reference alone.

The voice session comes here once the proof windows weighed the sentence by code and the quick
judgement said it is for him (``channels.voice.session``). ``answer_a_stranger`` makes ONE call of
a model (task ``conversation``, on the key the page's person may use and counted for them, ruling
R11), with no tool and nothing of anybody of the house: no discussion, memory, note, plan, request
or name, but the assistant's own name in the house (``house.domo_name``, else the default). It
is given, as data, what the
voice just said, the last ``EXCHANGES_KEPT`` exchanges with this same reference that are not
``EXCHANGE_LASTS`` old (counted from the last one: then the whole exchange is forgotten), how many
answers this voice had in ``STRANGER_WINDOW``, the word he wrote so far for the person of the
page, whether this voice was told that he listens (its ``voice_refs.warned_at``, or a reply of this
exchange said whole) and whether this answer reaches the microphone's flood bound. He answers with
what he says aloud (said through ``listener``: the voice of the page's person, on their key, at
home or not as for everybody), the word for the person of the page, both in his own words, and
whether he goes on answering this voice (``go_on``) or pauses it, for how many minutes. The page
sets ``warned_at`` when it played such a reply whole (``VoiceSession.played``): whether he told it
is his to judge.

Cost and abuse (the owner, 2026-11-27, S4: "the assistant judges"): no count stops a voice; he
judges in that same answer whether he goes on (no other call). A voice he paused gets no model call
at all until the pause he chose ends (``pause_minutes``, kept within ``PAUSE_LEAST`` and
``PAUSE_MOST``; the quick judgement and the whose-voice question included: the session asks
``paused`` first), and silence: the page is told nothing (an imitator learns nothing). A pause on a
microphone's key (a voice with no print, or read near the page's person) never silences the page's
person, who shares that key when not recognised: it lasts at most ``EXCHANGE_LASTS``, and holds
only for a sentence that still goes the stranger's path after the usual questions (``held``,
``take``). What stays
in code is what is no judgement: the page person's key and its spending limits (no model left on
it: nothing is said, the page gets the notice of a sentence not taken), and the microphone's flood
bound (``STRANGER_FLOOD``, every voice at it counted together), past which it is silence as for a
pause; the answer that reaches it is told so. An answer whose ``go_on`` cannot be read goes on:
never silent by a parsing accident (the flood bound still holds). Each pause is one line in the
journal (``voice.stranger_paused``: the microphone and the minutes, never words).

The word left: one per reference and page's person, at the end of the exchange (once it is
forgotten, ``EXCHANGE_LASTS`` after its last answer) or as soon as he pauses the voice or the
flood bound is reached, and never
two within ``NOTE_EVERY``: each answer writes the whole word again (he is given the one so far),
the code keeps the last one in memory and leaves it then. It goes through the notes' own path
(``tools.notes.NoteLeave``), as a note of the assistant's own in a turn he acts in on his own:
decided for him by the barrier (``decide_now`` with ``domo_actor``, his rights and limits,
``on_his_own_tables``), audited, and left only when it is allowed with no rule asking first. Its
reader's turn is one of words from outside, as with any note (``ToolContext.told_outside``): the
voice's words never count as the page's person's own, nor as an order. A word a rule holds back
is tried again every ``NOTE_EVERY`` for ``HELD_WORD_LASTS``, then dropped with one line in the
journal (never its words).

Nothing else is kept: the exchanges, the counts and the word waiting live in this process's memory
only, are never logged nor written, and no ``repr`` shows a word of them.
"""

import asyncio
import json
import logging
import uuid
from collections import deque
from collections.abc import Sequence
from contextlib import aclosing
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final, Literal, cast

import anyio
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.actors import actor_of
from domo_core.accounts.models import Person
from domo_core.accounts.sessions import is_active
from domo_core.audit.log import append_audit
from domo_core.channels.voice.deps import VoiceDeps
from domo_core.channels.voice.hearing import HeardUtterance
from domo_core.channels.voice.listener import VoiceListener
from domo_core.channels.voice.prints import warned_at
from domo_core.conversation.channel import ListenerGone
from domo_core.conversation.events import Ended, Words
from domo_core.conversation.execution import execute_allowed
from domo_core.conversation.service import Conversation
from domo_core.llm.person_picker import person_picker
from domo_core.llm.types import ChatChunk, ChatMessage, ChatModel, JsonObject, ModelCallError
from domo_core.naming import name_shown
from domo_core.policy.barrier import Allow, Refuse, record_decision
from domo_core.policy.catalog import on_his_own_tables
from domo_core.policy.decision import decide_now
from domo_core.policy.open_catalog import load_policy
from domo_core.policy.roles import domo_actor
from domo_core.policy.settings import load_policy_settings
from domo_core.prompts import build_stranger_prompt
from domo_core.tools.context import InvalidArguments, ToolContext
from domo_core.tools.notes import MAX_NOTE_LENGTH, NoteLeave

logger = logging.getLogger(__name__)

# How many of the last exchanges with a voice he is given, and how long after the last one the
# exchange is remembered.
EXCHANGES_KEPT: Final = 3
EXCHANGE_LASTS: Final = timedelta(minutes=10)
# A FLOOD bound, not a judgement of any voice: at most this many answers to voices nobody knows at
# one microphone in ``STRANGER_WINDOW`` (one every 10 s sustained), against a radio, a recording
# played again or a child talking on and on, which would keep the house talking and paying. Whether
# he goes on answering a voice is his (``Answer.go_on``); the page person's key keeps its limits.
STRANGER_FLOOD: Final = 60
STRANGER_WINDOW: Final = timedelta(minutes=10)
# The pause he chooses for a voice he stops answering, in minutes: at least 1, at most
# ``PAUSE_MOST``; ``EXCHANGE_LASTS`` when he gives none that can be read.
PAUSE_LEAST: Final = 1
PAUSE_MOST: Final = 60
# At most one word for the page's person about one voice in this time.
NOTE_EVERY: Final = timedelta(minutes=10)
# How often the words waiting are looked at, in seconds.
NOTE_CHECK_S: Final = 30.0
# How long a word a rule holds back is tried again (every ``NOTE_EVERY``): then it is dropped,
# with one line in the journal (never its words).
HELD_WORD_LASTS: Final = timedelta(hours=24)
# The longest answer asked of the model, in tokens, and how long it may take, in seconds.
STRANGER_OUTPUT_TOKENS: Final = 600
STRANGER_SECONDS: Final = 60.0
_NO_CONVERSATION: Final = uuid.UUID(int=0)
# What begins the key of an exchange kept under the microphone's (``key_of``).
_AT_THE_MICROPHONE: Final = "@"


def key_of(heard: HeardUtterance) -> str:
    """The reference an exchange with the voice of ``heard`` is kept under: its own voice
    reference, else the microphone's, which no other reference looks like: when there is no
    print, and when the voice is read near the page's person without resembling nobody (the
    reading then keeps it under the page person's own reference, which a stranger never shares)."""
    if heard.reading is not None and not heard.reading.resembles_nobody:
        return f"{_AT_THE_MICROPHONE}{heard.microphone}"
    return heard.reference or f"{_AT_THE_MICROPHONE}{heard.microphone}"


def at_the_microphone(key: str) -> bool:
    """Whether ``key`` is a microphone's (``key_of``): a key the page person's own voice, when not
    recognised, may share."""
    return key.startswith(_AT_THE_MICROPHONE)


@dataclass(frozen=True)
class Exchange:
    """One exchange with a voice nobody knows: what it said, what he answered, and when."""

    heard: str = field(repr=False)
    said: str = field(repr=False)
    at: datetime


@dataclass
class _Talk:
    """The exchange going on with one voice: its last answers, and whether a reply of it was said
    whole (the voice was told he listens, if he judged it useful)."""

    exchanges: deque[Exchange] = field(default_factory=lambda: deque(maxlen=EXCHANGES_KEPT))
    last_at: datetime | None = None
    warned: bool = False


@dataclass
class _Word:
    """The word for the page's person about one voice, as he wrote it last (``text``), waiting to
    be left; ``capped``: he paused the voice or the flood bound was reached, it is left as soon as
    it may be."""

    deps: VoiceDeps = field(repr=False)
    text: str = field(repr=False)
    last_at: datetime
    microphone: str
    capped: bool = False
    # When it was first held back (a rule asks first, or it could not be left).
    held_since: datetime | None = None


class Strangers:
    """The exchanges with voices nobody knows, in this process's memory (see the module
    docstring)."""

    def __init__(self) -> None:
        self._talks: dict[str, _Talk] = {}
        self._by_reference: dict[str, deque[datetime]] = {}
        self._by_microphone: dict[str, deque[datetime]] = {}
        # The voices he stopped answering, until when.
        self._paused: dict[str, datetime] = {}
        self._words: dict[tuple[str, uuid.UUID], _Word] = {}
        self._noted: dict[tuple[str, uuid.UUID], datetime] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._flusher: asyncio.Task[None] | None = None

    def __repr__(self) -> str:
        return f"Strangers({len(self._talks)} talks, {len(self._words)} words waiting)"

    def _talk(self, key: str, now: datetime) -> _Talk | None:
        """The exchange with ``key`` still remembered at ``now`` (the old ones are forgotten)."""
        for other, talk in list(self._talks.items()):
            if talk.last_at is not None and now - talk.last_at >= EXCHANGE_LASTS:
                del self._talks[other]
        return self._talks.get(key)

    def exchanges(self, key: str, now: datetime) -> list[Exchange]:
        """The last exchanges with ``key`` remembered at ``now``, oldest first."""
        talk = self._talk(key, now)
        return [] if talk is None else list(talk.exchanges)

    def told(self, key: str, now: datetime) -> bool:
        """Whether a reply of the exchange with ``key`` going on was said whole."""
        talk = self._talk(key, now)
        return talk is not None and talk.warned

    def said_whole(self, key: str) -> None:
        """A reply to ``key`` was played whole at the page."""
        talk = self._talks.get(key)
        if talk is not None:
            talk.warned = True

    @staticmethod
    def _within(times: deque[datetime], now: datetime) -> deque[datetime]:
        while times and now - times[0] >= STRANGER_WINDOW:
            times.popleft()
        return times

    def _flooded(self, microphone: str, now: datetime) -> bool:
        """Whether ``microphone`` had its ``STRANGER_FLOOD`` answers in the window."""
        times = self._within(self._by_microphone.get(microphone, deque()), now)
        return len(times) >= STRANGER_FLOOD

    def paused(self, key: str, microphone: str, now: datetime) -> bool:
        """Whether no model is called for ``key`` at ``microphone`` now: he paused that voice
        (``pause``), or the microphone reached its flood bound."""
        until = self._paused.get(key)
        return (until is not None and now < until) or self._flooded(microphone, now)

    def held(self, key: str, microphone: str, now: datetime) -> bool:
        """Whether no model at all is asked for ``key`` at ``microphone`` now, not even the quick
        judgement or the whose-voice question: the microphone is flooded, or he paused a voice
        kept under its own reference. A pause on a microphone's key never holds here: that key is
        shared by the page person's own voice when not recognised, and a pause never silences
        the page's person; it holds only for a sentence that still goes the stranger's path after
        the usual questions (``take``)."""
        if at_the_microphone(key):
            return self._flooded(microphone, now)
        return self.paused(key, microphone, now)

    def pause(self, key: str, now: datetime, length: timedelta) -> None:
        """He stops answering ``key`` for ``length`` from ``now``."""
        self._paused[key] = now + length

    def answers_so_far(self, key: str, now: datetime) -> int:
        """How many answers ``key`` had in the window (told to him; never a bound)."""
        return len(self._within(self._by_reference.get(key, deque()), now))

    def take(self, key: str, microphone: str, now: datetime) -> bool | None:
        """One more answer for ``key`` at ``microphone``, checked and counted in one step (no
        await between): ``None`` when the voice is paused or the microphone flooded already, else
        whether this one reaches the flood bound. The counts older than the window are
        forgotten."""
        self._prune(now)
        if self.paused(key, microphone, now):
            return None
        self._by_reference.setdefault(key, deque()).append(now)
        self._by_microphone.setdefault(microphone, deque()).append(now)
        return self._flooded(microphone, now)

    def _prune(self, now: datetime) -> None:
        """Forget the counts past the window, the pauses over and the words left long enough
        ago: a voice with no stable print gets a new reference each sentence."""
        for counts in (self._by_reference, self._by_microphone):
            for key, times in list(counts.items()):
                if not self._within(times, now):
                    del counts[key]
        for key, until in list(self._paused.items()):
            if now >= until:
                del self._paused[key]
        for k, at in list(self._noted.items()):
            if now - at >= NOTE_EVERY:
                del self._noted[k]

    def sizes(self) -> dict[str, int]:
        """How many references, microphones, pauses and words left are counted (no word of
        them)."""
        return {
            "by_reference": len(self._by_reference),
            "by_microphone": len(self._by_microphone),
            "paused": len(self._paused),
            "noted": len(self._noted),
        }

    def lock(self, microphone: str) -> asyncio.Lock:
        """The exchanges of one microphone go one after the other: each reads and writes the
        memory and the word the one before left, and two replies are never said at once."""
        found = self._locks.get(microphone)
        if found is None:
            found = self._locks[microphone] = asyncio.Lock()
        return found

    def add(self, key: str, exchange: Exchange) -> None:
        talk = self._talks.setdefault(key, _Talk())
        talk.exchanges.append(exchange)
        talk.last_at = exchange.at

    def word(self, key: str, page_person_id: uuid.UUID) -> str | None:
        """The word waiting for the page's person about ``key`` (``None``: none)."""
        found = self._words.get((key, page_person_id))
        return None if found is None else found.text

    def keep_word(
        self,
        deps: VoiceDeps,
        key: str,
        page_person_id: uuid.UUID,
        text: str,
        *,
        microphone: str,
        now: datetime,
        capped: bool,
    ) -> None:
        """The word he wrote last for the page's person about ``key``: it replaces the one
        waiting, and is left at the end of the exchange (``leave_due``). When the one it replaces
        was held back, it was held back since then: a voice that comes back never keeps it past
        ``HELD_WORD_LASTS``."""
        replaced = self._words.get((key, page_person_id))
        self._words[(key, page_person_id)] = _Word(
            deps,
            text,
            now,
            microphone,
            capped,
            held_since=None if replaced is None else replaced.held_since,
        )
        self._watch()

    def _watch(self) -> None:
        """The words waiting are looked at every ``NOTE_CHECK_S`` while there are any."""
        loop = asyncio.get_running_loop()
        flusher = self._flusher
        if flusher is not None and not flusher.done() and flusher.get_loop() is loop:
            return
        self._flusher = loop.create_task(self._flush())

    async def _flush(self) -> None:
        while self._words:
            await asyncio.sleep(NOTE_CHECK_S)
            await self.leave_due()

    async def leave_due(self, *, holding: str | None = None) -> None:
        """Leave the words whose exchange is over (or bound reached), when no word about that
        voice was left to that person within ``NOTE_EVERY``. Each is left under the lock of its
        microphone, so that no exchange there writes a word meanwhile. ``holding``: the caller
        holds that microphone's lock already, and only that microphone's words are looked at (a
        lock is never waited for while one is held). Never raises."""
        for k, word in list(self._words.items()):
            if holding is not None:
                if word.microphone == holding:
                    await self._leave(k, word)
                continue
            async with self.lock(word.microphone):
                await self._leave(k, word)

    async def _leave(self, k: tuple[str, uuid.UUID], word: _Word) -> None:
        now = word.deps.turn.clock()
        noted = self._noted.get(k)
        if noted is not None and now - noted >= NOTE_EVERY:
            del self._noted[k]
            noted = None
        over = word.capped or now - word.last_at >= EXCHANGE_LASTS
        if not over or noted is not None or self._words.get(k) is not word:
            return
        del self._words[k]
        self._noted[k] = now
        left = "waits"
        try:
            left = await _leave_word(word.deps, k[1], word.text)
        except Exception as error:
            # Not known to be left: it waits, as for a rule that asks first.
            logger.error(
                "A word about a voice nobody knows could not be left (%s)", type(error).__name__
            )
        if left == "waits" and k not in self._words:
            # Tried again after ``NOTE_EVERY``, for ``HELD_WORD_LASTS``; then dropped.
            word.held_since = word.held_since or now
            if now - word.held_since < HELD_WORD_LASTS:
                self._words[k] = word
            else:
                await _word_dropped(word.deps, k[1])

    def clear(self) -> None:
        """Forget everything (the words waiting included)."""
        if self._flusher is not None:
            self._flusher.cancel()
            self._flusher = None
        self._talks.clear()
        self._by_reference.clear()
        self._by_microphone.clear()
        self._paused.clear()
        self._words.clear()
        self._noted.clear()
        self._locks.clear()


# The exchanges of this process with voices nobody knows.
STRANGERS = Strangers()


async def answer_a_stranger(
    deps: VoiceDeps,
    *,
    page_person: Person,
    heard: HeardUtterance,
    reference: str,
    listener: VoiceListener,
) -> bool:
    """Answer the voice nobody knows that said ``heard`` at ``page_person``'s page (see the module
    docstring), under ``reference`` (``key_of``), aloud through ``listener``: whether something
    was said. Nothing when the bound is reached, with no model on a key of the page's person, or
    when the model fails or answers nothing readable (only the kind is logged)."""
    async with STRANGERS.lock(heard.microphone):
        return await _answer(deps, page_person, heard, reference, listener)


async def _answer(
    deps: VoiceDeps,
    page_person: Person,
    heard: HeardUtterance,
    reference: str,
    listener: VoiceListener,
) -> bool:
    turn = deps.turn
    now = turn.clock()
    so_far = STRANGERS.answers_so_far(reference, now)
    last_one = STRANGERS.take(reference, heard.microphone, now)
    if last_one is None:
        return False
    async with turn.session_factory() as session:
        told = await warned_at(session, reference) is not None
        settings = await load_policy_settings(session)
        language = settings.house_language
        # The name the house calls him by: a voice nobody knows has no name of its own for him.
        name = name_shown(None, settings.house_domo_name)
        picker = await person_picker(
            session,
            page_person,
            session_factory=turn.session_factory,
            model_factory=turn.model_factory,
            clock=turn.clock,
            now=now,
        )
    model = None if picker is None else await picker.model_for("conversation")
    if model is None:
        return False
    asked: JsonObject = {
        "voice_said": heard.text,
        "voice_language": heard.language,
        "before": [{"voice": e.heard, "you": e.said} for e in STRANGERS.exchanges(reference, now)],
        "answers_so_far": so_far,
        "your_word_so_far": STRANGERS.word(reference, page_person.id),
        "word_language": language,
        "told_you_listen": told or STRANGERS.told(reference, now),
        "last_answer_for_a_while": last_one,
    }
    answer = await _ask(model, json.dumps(asked, ensure_ascii=False), name)
    if answer is None:
        return False
    say, word = answer.say, answer.word
    STRANGERS.add(reference, Exchange(heard.text, say, now))
    if not answer.go_on:
        # His own judgement: no model for this voice until the pause he chose ends; on a
        # microphone's key, at most the exchange's own life (others' voices share that key).
        pause = min(answer.pause, EXCHANGE_LASTS) if at_the_microphone(reference) else answer.pause
        STRANGERS.pause(reference, now, pause)
        await _paused(deps, heard.microphone, pause)
    stopped = last_one or not answer.go_on
    waiting = STRANGERS.word(reference, page_person.id)
    if word or (stopped and waiting is not None):
        # His last word stands for the whole exchange; once he stopped, the one waiting goes now.
        text = word[:MAX_NOTE_LENGTH] if word else waiting or ""
        STRANGERS.keep_word(
            deps,
            reference,
            page_person.id,
            text,
            now=now,
            microphone=heard.microphone,
            capped=stopped,
        )
    if stopped:
        await STRANGERS.leave_due(holding=heard.microphone)
    if not say:
        return False
    try:
        await listener.open()
        await listener.emit(Words(say))
        await listener.emit(Ended("complete"))
    except ListenerGone:
        pass
    return True


@dataclass(frozen=True)
class Answer:
    """His answer to a voice nobody knows: what he says aloud (``say``), the word for the page's
    person (``word``), whether he goes on answering the voice (``go_on``) and, when not, for how
    long he pauses it (``pause``)."""

    say: str = field(repr=False)
    word: str = field(repr=False)
    go_on: bool = True
    pause: timedelta = EXCHANGE_LASTS


async def _ask(model: ChatModel, asked: str, name: str) -> Answer | None:
    """The model's answer to ``asked`` (``read_answer``); ``None`` when it failed, took too long
    or said nothing readable."""
    pieces: list[str] = []
    with anyio.move_on_after(STRANGER_SECONDS):
        try:
            stream = model.stream(
                system=build_stranger_prompt(name=name),
                messages=[ChatMessage("user", asked)],
                max_output_tokens=STRANGER_OUTPUT_TOKENS,
                think=False,
            )
            async with aclosing(stream) as items:
                async for item in items:
                    if isinstance(item, ChatChunk):
                        pieces.append(item.text)
        except ModelCallError as error:
            logger.info("A voice nobody knows could not be answered (%s)", error.kind)
            return None
        return read_answer("".join(pieces))
    logger.info("A voice nobody knows was not answered in time")
    return None


def read_answer(answer: str) -> Answer | None:
    """``{"say", "note", "go_on", "pause_minutes"}`` in ``answer`` (the first object in it);
    ``None`` when there is none. ``go_on`` is false only when it is ``false``: missing or anything
    else, he goes on. ``pause_minutes`` is a number kept within ``PAUSE_LEAST`` and
    ``PAUSE_MOST``; missing or not a number, ``EXCHANGE_LASTS``."""
    start, end = answer.find("{"), answer.rfind("}")
    try:
        found = json.loads(answer[start : end + 1]) if 0 <= start < end else None
    except ValueError:
        found = None
    if not isinstance(found, dict):
        logger.info("The answer to a voice nobody knows was unreadable")
        return None
    read = cast(dict[str, object], found)
    say, word, minutes = read.get("say"), read.get("note"), read.get("pause_minutes")
    pause = EXCHANGE_LASTS
    if isinstance(minutes, (int, float)) and not isinstance(minutes, bool) and minutes == minutes:
        pause = timedelta(minutes=int(min(PAUSE_MOST, max(PAUSE_LEAST, minutes))))
    return Answer(
        say.strip() if isinstance(say, str) else "",
        word.strip() if isinstance(word, str) else "",
        go_on=read.get("go_on") is not False,
        pause=pause,
    )


async def _paused(deps: VoiceDeps, microphone: str, pause: timedelta) -> None:
    """One line in the journal: he stopped answering a voice at ``microphone`` for ``pause``
    (never its words). Never raises."""
    try:
        async with deps.turn.session_factory() as session:
            await append_audit(
                session,
                actor="domo",
                action="voice.stranger_paused",
                details={"microphone": microphone, "minutes": int(pause.total_seconds() // 60)},
            )
            await session.commit()
    except Exception as error:
        logger.error("A pause could not be journaled (%s)", type(error).__name__)


class _WordFor(NoteLeave):
    """``note_leave`` for a person the code already knows by their id: never another member who
    shares their name."""

    def __init__(self, person: Person) -> None:
        self._person = person

    def from_unknown(self, ctx: ToolContext) -> bool:
        """Always: a voice nobody knows is a source nobody of the house is known for."""
        return True

    async def recipient(
        self, session: AsyncSession, name: str, *, now: datetime, ctx: ToolContext
    ) -> Person:
        found = await session.get(Person, self._person.id, populate_existing=True)
        if found is None or not is_active(found, now):
            raise InvalidArguments("not_found", "this person is no longer in the household")
        return found


async def _leave_word(
    deps: VoiceDeps, page_person_id: uuid.UUID, text: str
) -> Literal["left", "waits", "dropped"]:
    """Leave ``text`` to the page's person as a note of the assistant's own, through the notes'
    path, decided for him as any note of his (see the module docstring): ``left``; ``waits`` when
    a rule asks first (the request for a yes is made in a turn, which this is not: the word is
    tried again later, for ``HELD_WORD_LASTS``); ``dropped`` when refused or the person is gone."""
    turn = deps.turn
    now = turn.clock()
    async with turn.session_factory() as session:
        page = await session.get(Person, page_person_id, populate_existing=True)
        if page is None or not is_active(page, now):
            return "dropped"
        policy = on_his_own_tables(await load_policy(session, turn.policy))
    ctx = ToolContext(
        session_factory=turn.session_factory,
        actor=actor_of(page),
        person=page,
        conversation_id=_NO_CONVERSATION,
        origin="",
        gatekeeper=turn.gatekeeper,
        clock=turn.clock,
        registry=turn.registry,
        policy=policy,
        reply_message_id=None,
        no_reader=True,
        notify=Conversation(turn).notify,
        on_his_own=True,
    )
    tool = _WordFor(page)
    name = tool.definition.name
    arguments: JsonObject = {"recipient": page.display_name, "text": text}
    domo = domo_actor()
    try:
        action = await tool.describe(arguments, domo, ctx)
    except InvalidArguments as error:
        logger.info("A word about a voice nobody knows was not left (%s)", error.code)
        return "dropped"
    async with turn.session_factory() as session:
        decision = await decide_now(session, domo, action, policy, now)
        await record_decision(session, domo, action, decision, tool_name=name)
        await session.commit()
    if isinstance(decision, Refuse):
        logger.info("A word about a voice nobody knows was refused (%s)", decision.reason)
        return "dropped"
    if not isinstance(decision, Allow) or decision.rule is not None or decision.judge_identity:
        logger.info("A word about a voice nobody knows waits (%s)", type(decision).__name__)
        return "waits"
    record = await execute_allowed(tool, name, tool.action_type, arguments, ctx)
    if record.status != "done":
        logger.info("A word about a voice nobody knows was not left (%s)", record.status)
        return "dropped"
    return "left"


async def _word_dropped(deps: VoiceDeps, page_person_id: uuid.UUID) -> None:
    """A word for ``page_person_id`` held back for ``HELD_WORD_LASTS`` is dropped: one line in
    the journal, never its words. Never raises."""
    try:
        async with deps.turn.session_factory() as session:
            await append_audit(
                session,
                actor="system",
                action="voice.stranger_word_dropped",
                details={"person_id": str(page_person_id)},
            )
            await session.commit()
    except Exception as error:
        logger.error("A dropped word could not be journaled (%s)", type(error).__name__)


def exchanged_with(key: str, now: datetime) -> Sequence[tuple[str, str]]:
    """The last exchanges with ``key`` remembered at ``now``: what the voice said and what he
    answered, oldest first (the quick judgement is given them)."""
    return [(e.heard, e.said) for e in STRANGERS.exchanges(key, now)]
