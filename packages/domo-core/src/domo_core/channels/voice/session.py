"""The voice session of one page's socket (spec 6.1, 6.7, 6.9, 6.11): each moment of speech the
route hands on is heard, and becomes the person's words, or a saying of a reserved moment.

The route calls the session (``routes.VoiceSession``) and each call returns at once: the moments
wait in a short queue (``WAITING_UTTERANCES``; past it a new one is dropped) and one worker takes
them in order. The session is one of the person's live sessions (``live.LiveVoices``): a person
may listen at several pages, and a reserved moment is the person's, bound to the microphone that
asked for it. For each moment of speech, in this order:

1. A phrase moment of the person bound to ANOTHER microphone, or a saying taken there (a copy):
   nothing of this one is heard, and the page is told (``{"type": "not_taken", "reason":
   "elsewhere"}``): one sentence heard by two pages is taken once.
2. It is heard (``hearing.hear``), at home whatever the owner's setting when the words may be a
   phrase (a phrase moment lives, or a proof window is open here, see 4). When nothing can hear,
   the page is told (``{"type": "state", "hearing"}``) and the sound is dropped; when no model may
   hear ordinary words at all, the page is also told to turn the microphone off, and the owner is
   told once, in his conversation, by the assistant (a wake with the fact as data). Nothing is
   said by the code.
3. A phrase moment (``phrase_agree``, ``phrase_change``) of this microphone (bound when the reply
   that opened it is said here, else by its first saying): the words go to the vault and nowhere
   else (``proof.take_for_the_vault``: no message, no model, no journal, no log), and the
   assistant is woken with the outcome as data (``{"phrase": ...}``) to tell it in his own words.
4. A proof window (fix round 5 of Task 24): one is open at this microphone for a person P while
   P has a request waiting for a proof they can give by their phrase (``proof.proofs_awaited``,
   held back by the gatekeeper or not) AND a reply to P was said here, or is being said, within
   the last ``PROOF_WINDOW_S`` seconds (from the end of that reply, whatever it said). While one
   is open, EVERY sentence heard here, whoever the voice is taken for (P, someone else, nobody), is
   heard at home and weighed by code against the phrase of each person whose window is open
   (``proof.give_the_phrase``), before the quick judgement, any buffer, discussion or model. A
   proof given wakes the assistant as any proof does (and is given to the other requests waiting
   for it too); a voice not surely P's that says the phrase is checked all the same and the judge
   weighs it with the clues (``{"proof": "another_proof"}`` when he asks another); a near miss is
   dropped and the assistant woken with ``{"proof": "did_not_work"}``; while the phrase is held
   back, every sentence is dropped, and he is woken once per hold with the same
   ``did_not_work`` (a bystander cannot tell a held phrase from a wrong one); the gatekeeper out
   of reach drops it, with one wake for the outage (``not_checked``); an ordinary sentence goes
   its usual way. Outside any open window nothing is compared, and hearing follows the setting.
5. Otherwise the message of the one who probably spoke: ``Conversation.say`` then ``answer``, with
   the speaker of this sentence (``channels.voice.speaker.voice_speaker``: the probable person,
   the clues of its voice and the page's session), in THAT person's conversation, with their
   rights (Paul heard at Alex's page speaks in Paul's conversation), and a ``VoiceListener``
   that says the reply here, in the language heard. Words said while a reply is being written in the
   person's conversation are kept for the turn that writes it, with this channel's request (spec
   11.3, ``Conversation.say``), which says that this page hears what answers them
   (``TurnRequest.also_hears``, ``VoiceSession._joins``): a reply of another channel (the chat
   page, the text program) is said aloud here from when its turn reads them; this page's own
   reply, stopped by the quick judgement, speaks again then; one that answers them after the turn
   (``Conversation.answer_kept``) is said here, once, with its requests. Words that cannot be said
   are dropped; a person with no key, or a failure of the house, is shown a notice by the page
   (``{"type": "failed"}``).

Is it for him? Should he stop? (spec 6.6): once heard, and before whose sentence it is decides
anything else (the stranger's path comes after), a sentence is weighed by the quick judgement
(``channels.voice.attention``), unless a reserved moment or a proof window may take it (a phrase
moment bound to this microphone or the probable person's, or a window open here): it is then
weighed by the code first and reaches no model; once it is no answer, it is weighed by the quick
judgement before going its way. While the
assistant's voice is heard somewhere (``speaking``: a sentence of a reply sent to a page and not
yet played), the judgement says what it does: ``stop`` (the voice of THIS microphone goes quiet,
another room's is never cut by code; the sentence goes on as the next message when it is for
him, kept, never refused, else it is not his), ``resume`` (the page raises its sound; the
sentence goes on when it is for him, else it is not his), ``drop`` (quiet, nothing more). For a
voice nobody knows, the last lines it is given are those of its own exchange with him when one is
remembered, else the page person's (at home, like the sentence). A sentence that is not for him
enters no discussion: it is kept in memory for this microphone (``channels.voice.unaddressed``)
and sorted once the room is quiet. With no model for the judgement (none at home, and the owner
keeps what is heard at home), the voice stays off (the page is told so, ``no_attention``, never
that it cannot hear) and the owner is told once by the assistant, who offers the choice
(``speech.hear_outside``, or ``voice.after_a_gesture``), once meaning once his turn ran (a turn
that could not run is told at the next try); once the owner chose to be heard after a gesture,
the page is told (``{"type": "state", "hearing": "gesture"}``) and a sentence said with the
gesture is for him.

A voice read like nobody known at this signed-in page is not made a stranger by the score alone
(audit case 5): once any open proof window weighed it by code, and when no phrase moment may take
it, the quick model is asked whether it is still the page's person carrying on
(``channels.voice.whose``); "theirs" takes it as theirs, unsure, the voice's clue against them
kept (the judge sees it), and nothing of its voice is learnt; anything else, the stranger's path.

A stop of the listening reaches the session at once (``turn_off``, through ``live.LiveVoices``).

Whose sentence it is (spec 6.5): the voice is heard first, and its speaker read; the phrase
moments that take it are then the PROBABLE person's (step 3), never the page's by principle; the
proof windows are this microphone's, whoever speaks (step 4). So that nothing that may be a phrase
is heard outside the house, a sentence is heard at home whenever a phrase moment lives or a proof
window is open here. A sentence of nobody known (``person_id`` ``None``) goes to nobody's
conversation. Once the windows weighed it and the quick judgement said it is for him, it gets a
short exchange of its own (``channels.voice.stranger``: one model call with no tool and nothing of
anybody, on the page person's key, said aloud here, and a word left to the page's person); while
he paused that voice, or past the microphone's flood bound, it reaches no model at all, not even
the quick judgement, and gets silence. Said while
a phrase moment lives here, it is dropped with no model and no notice (the silence of a sentence
not said to him); not answered otherwise, the page is told (``not_taken``, ``not_now``). One
said while a phrase moment of ANOTHER person is this microphone's (or the page person's own, not
bound yet) is dropped with that notice: a phrase of one person said by another is never
written, and the notice tells neither an imitator whether he passed nor that a phrase is awaited
here. A saying weighed for a moment or in a window is in hand at this microphone while it is
handled and remembered as taken: a copy heard by another microphone meanwhile reaches nothing
(``LiveVoices.a_copy``).

Someone heard at a page signed in as another person (``Speaker.page_person_id``) is told only of
what was said and answered here in this visit (``TurnRequest.heard_here``), and their words never
join a turn of theirs running elsewhere with their whole context: while one runs, the words wait
in the session (``Conversation.say`` raises ``ReplyBeingWritten``, nothing is stored) and are
answered after it by a turn of their own (``_answer_later``).

Every print of a voice is read (``hearing``) and given to the learning (``prints.learn``) of the
page's person, sure by the page's session only when the voice is theirs (the probable person is
the page's) and it is no phrase moment's; else it only keeps the visit. The reading of the last
sentence is kept on the session (``last_voice``, ``None`` when it had none) and makes the
sentence's speaker (``voice_speaker``, with the proofs kept for the page's person on this channel
and microphone); only its verdict is ever logged.

What the assistant says on his own reaches the voice too: the session listens to the page's person
while theirs is the last voice heard here (``Listeners.open_hearing``, with ``VOICE``), and says
each such reply with a listener of its own. What he says on his own is never said aloud here while
the last voice heard is not the page person's, or weighs against them (a stranger, another
person): it stays written in their conversation. Another person heard here hears only the replies
of their own turns born here in this visit, never what he says to them on his own (a wake, a
reminder, what answers words they typed elsewhere): that goes to their own pages and channels.
What the session does with a reply's events never fails the reply: an error there is logged by its
kind.

The sound lives in memory while it is heard, and the session holds none once it is handled
(``holds_sound``); no word heard is logged.
"""

import asyncio
import logging
import time
import uuid
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.accounts.sessions import is_active
from domo_core.channels.api.render import decision_sentence, sentence_of_card
from domo_core.channels.voice import protocol, unaddressed
from domo_core.channels.voice.abilities import VOICE, VOICE_CHANNEL
from domo_core.channels.voice.attention import Exchanged, Moment, open_attention
from domo_core.channels.voice.deps import VoiceDeps
from domo_core.channels.voice.hearing import CannotHear, HeardUtterance, hear
from domo_core.channels.voice.listener import VoiceListener, VoiceSink
from domo_core.channels.voice.live import LIVE_SENTENCES, LIVE_VOICES
from domo_core.channels.voice.models import VoiceMoment
from domo_core.channels.voice.moments import moment_of
from domo_core.channels.voice.prints import NOT_LEARNED, Reading, learn, mark_warned
from domo_core.channels.voice.proof import (
    PHRASE_ACTIONS,
    Given,
    PhraseOutcome,
    Weighed,
    bind_phrase_moment,
    give_the_phrase,
    proofs_awaited,
    take_for_the_vault,
)
from domo_core.channels.voice.speaker import carrying_on, voice_speaker
from domo_core.channels.voice.stranger import (
    STRANGERS,
    answer_a_stranger,
    exchanged_with,
    key_of,
)
from domo_core.channels.voice.thresholds import JOIN_WITHIN_S
from domo_core.channels.voice.unaddressed import HeardLine, Unaddressed, sort_heard
from domo_core.channels.voice.whose import Asked, open_whose_voice
from domo_core.chat.models import Message
from domo_core.chat.store import ConversationStore
from domo_core.confirmations.routes import card_after_decision
from domo_core.conversation.channel import Listener, ListenerGone
from domo_core.conversation.events import Acted, Decided, Ended, Event
from domo_core.conversation.prepare import AlsoHears, NoKey, TurnRequest
from domo_core.conversation.service import (
    Conversation,
    NotSayable,
    ReplyBeingWritten,
    reply_is_alive,
)
from domo_core.conversation.speaker import Clue, Speaker, standing, voice_against
from domo_core.conversation.tool_wakes import tool_wake_turn
from domo_core.identity.clues import kept_clues
from domo_core.llm.speech.client import Print
from domo_core.llm.types import JsonObject
from domo_core.naming import name_shown
from domo_core.policy.settings import (
    HEAR_OUTSIDE_KEY,
    VOICE_GESTURE_KEY,
    load_policy_settings,
)
from domo_core.prompts import ToolWake
from domo_core.texts.view import person_texts

logger = logging.getLogger(__name__)

# How many moments of speech may wait to be heard; past it, a new one is dropped.
WAITING_UTTERANCES: Final = 3
# The name the voice channel wakes the assistant under, with a fact of the channel as data.
WAKE_TOOL: Final = "voice_channel"
# What the owner is told, as data, when nothing at home can weigh what is heard and he did not
# choose: why the voice cannot stay on, and what each choice means for the household. He says it
# in his own plain words; the settings' names are for his tool (``POLICY_TOOL``).
NO_ATTENTION_WHY: Final = (
    "nothing at home can judge quickly whether a sentence heard is said to you, so the voice "
    "cannot stay on yet; hearing itself works"
)
GESTURE_MEANS: Final = (
    "the person holds a button on the page while talking to you, and only then are they heard; "
    "everything heard stays at home"
)
OUTSIDE_MEANS: Final = (
    "the voice listens freely, without a button: what the microphones hear goes to a model "
    "outside the house, so the sound and the words said near them leave the house"
)
# The tool that records the owner's choice.
POLICY_TOOL: Final = "policy_update"
_PHRASE_PURPOSES: Final = frozenset({"phrase_agree", "phrase_change"})
# How long a sentence sent to the page counts as heard there with no word that it was played.
SOUNDING_AT_MOST_S: Final = 120.0
# How long the voice of someone else than the page's person stays "the last voice heard here"
# when it is not heard again: then what the assistant says on his own is said to the page's
# person again, and the other person is no longer listened for here.
LAST_VOICE_LASTS_S: Final = 180.0
# How long after a reply to a person said here ends a proof window stays open for them at this
# microphone (see the module docstring, step 4).
PROOF_WINDOW_S: Final = 90.0
# How often words heard at someone else's page look whether the reply being written in their
# conversation is over (``_answer_later``), and how many may wait at once at a page: they wait as
# long as that reply is written (never dropped for the time), and one more past the bound is not
# taken (the page is told). Final fix wave, case 11.
WRITTEN_CHECK_S: Final = 0.5
WAITING_LATER_AT_MOST: Final = 20
# A reply being said here holds its person's proof window open while its turn gives it events;
# one silent for longer than this (a turn that never ends) holds it no more.
REPLY_QUIET_AT_MOST_S: Final = 120.0
# Why a sentence is not taken that the page is told, one notice for every case where telling
# more would tell an imitator whether he passed, or that someone's phrase is awaited here: a voice
# nobody knows not answered, a sentence while another person's phrase moment is this microphone's.
NOT_NOW: Final = "not_now"


@dataclass(frozen=True)
class _Window:
    """A proof window open at this microphone: whose, and the requests waiting for their phrase,
    the newest first."""

    person: Person
    awaited: list[uuid.UUID]


@dataclass(frozen=True)
class HeardVoice:
    """What the voice of the last sentence said of who speaks (``voice_speaker`` turns it into
    clues): the reading against the page's person, the voice's reference, the microphone, and
    when."""

    reading: Reading
    reference: str | None
    microphone: str
    at: datetime


class _Reply:
    """The listener of one reply said aloud (``VoiceListener``), of ``person_id``'s conversation,
    watched by the session (see ``VoiceSession.watched``). ``asked``: the reply answers words this
    page heard, and its turn gives it its events itself; otherwise it is what the assistant says
    on his own, given through the person's listeners (``VoiceSession._unasked``)."""

    def __init__(
        self,
        session: "VoiceSession",
        reply_id: uuid.UUID,
        voice: VoiceListener,
        *,
        person_id: uuid.UUID,
        asked: bool,
    ) -> None:
        self._session = session
        self.reply_id = reply_id
        self.voice = voice
        self.person_id = person_id
        self.asked = asked
        # When its turn last gave it an event (from its opening).
        self.last_at = session.deps.turn.clock()

    @property
    def reading(self) -> bool:
        return self.voice.reading

    async def open(self) -> None:
        await self.voice.open()

    async def emit(self, event: Event) -> None:
        self.last_at = self._session.deps.turn.clock()
        try:
            await self.voice.emit(event)
        finally:
            await self._session.watched(self, event)


class _Watched:
    """The page's socket as the session's listeners reach it: each sentence of a reply sent to it
    (``say``) is noted, until the page tells it was played (``VoiceSession.played``)."""

    def __init__(self, sink: VoiceSink, said: Callable[[uuid.UUID], None]) -> None:
        self._sink = sink
        self._said = said

    @property
    def open(self) -> bool:
        return self._sink.open

    async def send_json(self, data: JsonObject) -> None:
        await self._sink.send_json(data)
        reply = data.get("reply")
        if data.get("type") == "say" and isinstance(reply, str):
            self._said(uuid.UUID(reply))

    async def send_bytes(self, data: bytes) -> None:
        await self._sink.send_bytes(data)


# The voice sessions of this process: where the assistant's voice is heard now is read from them.
_SESSIONS: "set[VoiceSession]" = set()


def speaking_at() -> list[str]:
    """The microphones where the assistant's voice is heard now (``VoiceSession.speaking``)."""
    return sorted({s.microphone for s in _SESSIONS if s.speaking})


class VoiceSession:
    """The voice session of a socket (see the module docstring), for ``person_id``'s page on
    ``microphone``, reached at ``origin``, speaking back through ``sink``. ``signed_in_at``: when
    the page's session was opened (``None`` when it is not known: then when this one opens)."""

    def __init__(
        self,
        deps: VoiceDeps,
        *,
        person_id: uuid.UUID,
        microphone: str,
        origin: str,
        sink: VoiceSink,
        signed_in_at: datetime | None = None,
    ) -> None:
        self._deps = deps
        self._person_id = person_id
        self._signed_in_at = signed_in_at or deps.turn.clock()
        self._microphone = microphone
        self._origin = origin
        self._sink = _Watched(sink, self._sounds)
        # The replies sent to the page and not yet played, with when their last sentence was
        # sent (``time.monotonic``); those the quick judgement stopped while they were written.
        self._sounding: dict[uuid.UUID, float] = {}
        self._hushed: set[uuid.UUID] = set()
        # What this microphone heard that was not said to him, and when the room last made a
        # sound (someone spoke, or his voice was heard): sorted once it has been quiet.
        self._unaddressed = Unaddressed()
        self._last_sound = time.monotonic()
        self._quiet: asyncio.Task[None] | None = None
        # When the page was told to listen only with the gesture: what began before came without.
        self._gesture_since: datetime | None = None
        # When the last voice heard here, someone else's, stops being the last (``_the_voice_is``).
        self._last_voice_ends: asyncio.Task[None] | None = None
        # Whose voice this visit here is (``_the_voice_is``), and the messages said and answered
        # here in it: another person heard at this page is told of those alone of their history.
        self._visit: uuid.UUID | None = None
        self._heard_here: set[uuid.UUID] = set()
        self._said_here_at: datetime | None = None  # when the last of them was said
        # Those told of a hold of their phrase in their window (when the last reply said here to
        # each person ended is the microphone's: ``LiveVoices.said_to``), and whether an outage of
        # the gatekeeper was told.
        self._hold_told: set[uuid.UUID] = set()
        self._outage_told = False
        # Words heard at someone else's page that wait for a reply of theirs to be over, one at a
        # time.
        self._later: set[asyncio.Task[None]] = set()
        self._later_turn = asyncio.Lock()
        # The replies said here to a voice nobody knows, until the page tells they were played:
        # their voice, and the reference of the voice they answer.
        self._strangers_said: dict[uuid.UUID, tuple[VoiceListener, str]] = {}
        # Each moment of speech with when it began (``speech_began``).
        self._queue: asyncio.Queue[tuple[datetime, bytes]] = asyncio.Queue(WAITING_UTTERANCES)
        self._sound: bytes | None = None  # the moment of speech being heard
        self._began: datetime | None = None  # when the moment being gathered began
        self._sound_began: datetime | None = None  # when the one being heard began
        self._recent: deque[tuple[datetime, Print]] = deque()
        self._replies: dict[uuid.UUID, _Reply] = {}
        self._hearing: protocol.Hearing = "ready"
        self._closed = False
        self.last_voice: HeardVoice | None = None
        # Whom what the assistant says on his own is said aloud to here: the person whose voice
        # was heard last (the page's until another is), until the listening is turned off.
        self._hearing_for: dict[uuid.UUID, Callable[[], None]] = {}
        self._unasked_on = True
        self._hear_for(person_id)
        # The people this session listens for in the registry: the page's, and whoever else
        # spoke here (a stop of the listening of theirs reaches this page).
        self._leaves: dict[uuid.UUID, Callable[[], None]] = {
            person_id: LIVE_VOICES.enter(person_id, self)
        }
        # The person an ordinary sentence of this microphone is held in hand for (``_take``).
        self._holding: uuid.UUID | None = None
        self._worker = asyncio.create_task(self._work())
        _SESSIONS.add(self)

    @property
    def microphone(self) -> str:
        """The page's microphone (its claim)."""
        return self._microphone

    @property
    def speaking(self) -> bool:
        """Whether the assistant's voice is being heard at this page now: a sentence of a reply
        was sent to it and the page did not say it was played (for ``SOUNDING_AT_MOST_S`` at
        most)."""
        now = time.monotonic()
        for reply_id, at in list(self._sounding.items()):
            if now - at > SOUNDING_AT_MOST_S:
                del self._sounding[reply_id]
        return bool(self._sounding)

    def _sounds(self, reply_id: uuid.UUID) -> None:
        now = time.monotonic()
        self._sounding[reply_id] = now
        self._last_sound = now

    @property
    def deps(self) -> VoiceDeps:
        """What the session stands on."""
        return self._deps

    @property
    def holds_sound(self) -> bool:
        """Whether the session holds any sound now (one being heard, or waiting)."""
        return self._sound is not None or not self._queue.empty()

    # --- what the route and the registry call --------------------------------------------------

    async def speech_began(self) -> None:
        # The page lowers its sound by itself; whether the voice stops is weighed once the
        # sentence is heard. When it began is kept: a late copy of a reserved moment's sentence is
        # told by it.
        self._began = self._deps.turn.clock()
        self._last_sound = time.monotonic()

    async def heard(self, sound: bytes) -> None:
        if self._closed:
            return
        self._last_sound = time.monotonic()
        try:
            began = self._began or self._deps.turn.clock()
            self._queue.put_nowait((began, sound))
        except asyncio.QueueFull:
            logger.info("A moment of speech was dropped: too many wait to be heard")

    async def nothing_heard(self) -> None:
        return None

    async def played(self, reply: uuid.UUID, *, whole: bool) -> None:
        if self._sounding.pop(reply, None) is not None:
            self._last_sound = time.monotonic()
        found = self._replies.get(reply)
        if found is not None:
            found.voice.played(whole=whole)
        stranger = self._strangers_said.pop(reply, None)
        if stranger is not None:
            stranger[0].played(whole=whole)
            if whole:
                await self._warned(stranger[1])

    async def off(self) -> None:
        self._stop_listening_unasked()

    async def close(self) -> None:
        self._closed = True
        _SESSIONS.discard(self)
        self._stop_listening_unasked()
        # What was heard and not said to him goes with the page: never sorted, never kept.
        self._unaddressed.drop()
        if self._quiet is not None:
            self._quiet.cancel()
            with suppress(asyncio.CancelledError):
                await self._quiet
        if self._last_voice_ends is not None:
            self._last_voice_ends.cancel()
        for later in list(self._later):
            later.cancel()
        if self._later:
            await asyncio.gather(*self._later, return_exceptions=True)
        for leave in self._leaves.values():
            leave()
        self._leaves.clear()
        self._worker.cancel()
        with suppress(asyncio.CancelledError):
            await self._worker
        # The sayings taken here go with the last page of this microphone; another microphone's
        # marks stay.
        if not any(other.microphone == self._microphone for other in _SESSIONS):
            LIVE_VOICES.forget(self._microphone)
            LIVE_SENTENCES.forget(self._microphone)
        for reply in list(self._replies.values()):
            # Cut with the page: its person's window here runs from now (a page reloaded finds
            # it open).
            self._reply_over(reply.reply_id)
            await reply.voice.close()
        self._replies.clear()
        for voice, _ in list(self._strangers_said.values()):
            await voice.close()
        self._strangers_said.clear()
        while not self._queue.empty():
            self._queue.get_nowait()
        self._sound = None

    def says(self, reply_id: uuid.UUID) -> bool:
        """Whether this page says the reply ``reply_id`` (``live.LiveSession``)."""
        return reply_id in self._replies

    async def turn_off(self) -> None:
        """The person stopped the listening: the page turns its microphone off now."""
        await self._send(protocol.listening(on=False))

    def _stop_listening_unasked(self) -> None:
        self._unasked_on = False
        for stop in self._hearing_for.values():
            stop()
        self._hearing_for.clear()

    def _hear_for(self, person_id: uuid.UUID) -> None:
        """What the assistant says on his own to ``person_id`` is said aloud here from now on."""
        listeners = self._deps.turn.listeners
        if listeners is None or not self._unasked_on or person_id in self._hearing_for:
            return

        async def hear(reply_id: uuid.UUID, event: Event) -> None:
            await self._unasked(person_id, reply_id, event)

        self._hearing_for[person_id] = listeners.open_hearing(
            person_id, hear, VOICE, channel=VOICE_CHANNEL
        )

    async def _the_voice_is(self, speaker: Speaker | None) -> None:
        """The last voice heard here is ``speaker``'s (``None``: nobody known): what the assistant
        says on his own is said aloud only to its probable person, and only when the voice does
        not weigh against them. Whatever he was saying on his own here to anyone else stops being
        said (it stays written in their conversation). Someone else than the page's person is
        listened for here (``live.LiveVoices``: their stop of the listening reaches this page)
        only while their voice is the last one, and for ``LAST_VOICE_LASTS_S`` at most when it is
        not heard again; then the page's person is the one he speaks to here again."""
        page = self._person_id
        fits = None if speaker is None or voice_against(speaker) else speaker.person_id
        who = None if speaker is None else speaker.person_id
        if self._visit != who:
            # A visit of this voice here begins: what is told of their history starts here.
            self._visit = who
            self._heard_here = set()
        # At this page only the page's person hears what he says on his own: another person's
        # (with their whole context) goes to their own pages, never aloud here; their replies said
        # here are those of their own turns born here in this visit (final fix wave, voice I4).
        await self._said_aloud_to(fits if fits == page else None)
        self._let_go_of_others(keep=fits)
        if self._last_voice_ends is not None:
            self._last_voice_ends.cancel()
            self._last_voice_ends = None
        if (speaker is None or speaker.person_id != page) and not self._closed:
            self._last_voice_ends = asyncio.create_task(self._back_to_the_page())

    async def _back_to_the_page(self) -> None:
        """Someone else's voice was the last one here and was not heard again: the page's person
        is the one the assistant speaks to here again."""
        await asyncio.sleep(LAST_VOICE_LASTS_S)
        self._last_voice_ends = None
        self._visit = None
        self._heard_here = set()
        try:
            await self._said_aloud_to(self._person_id)
            self._let_go_of_others(keep=None)
        except Exception as error:
            logger.error(
                "The page's person could not be listened for again (%s)", type(error).__name__
            )

    def _let_go_of_others(self, *, keep: uuid.UUID | None) -> None:
        """No one but the page's person and ``keep`` is listened for here any more (a proof
        awaited of someone is read from the requests, never from who is listened for here)."""
        for person_id in [p for p in self._leaves if p not in (self._person_id, keep)]:
            self._leaves.pop(person_id)()

    async def _said_aloud_to(self, fits: uuid.UUID | None) -> None:
        """What the assistant says on his own is said aloud here to ``fits`` alone (none: to
        nobody)."""
        for person_id in [p for p in self._hearing_for if p != fits]:
            self._hearing_for.pop(person_id)()
            for reply in [
                r for r in self._replies.values() if not r.asked and r.person_id == person_id
            ]:
                self._reply_over(reply.reply_id)
                await reply.voice.close()
        if fits is not None:
            self._hear_for(fits)

    # --- the worker ------------------------------------------------------------------------------

    async def _opening(self) -> None:
        """When the session opens, before any sound is taken: with no model to weigh what is heard
        (and one to hear it), the page is told at once how it may listen: only with the gesture,
        or not at all (the owner told once)."""
        page = await self._person()
        if page is None or await self._deps.speech.open(page, "hear") is None:
            return  # nothing hears: told with the first sentence (``_cannot_hear``)
        if await open_attention(self._deps, page) is not None:
            return
        if await self._after_a_gesture():
            await self._hearing_is("gesture")
        else:
            await self._no_attention(page)

    async def _work(self) -> None:
        try:
            await self._opening()
        except Exception as error:
            logger.error("The voice could not be readied (%s)", type(error).__name__)
        while True:
            self._sound_began, self._sound = await self._queue.get()
            try:
                await self._handle()
            except Exception as error:
                # The kind only: a message may hold what was heard. The page is told the words
                # were not taken (a database that cannot be read, say).
                logger.error("A moment of speech could not be handled (%s)", type(error).__name__)
                await self._send(protocol.failed("failed"))
            finally:
                self._sound = None

    async def _handle(self) -> None:
        deps = self._deps
        now = deps.turn.clock()
        page = await self._person()
        if page is None:
            return
        async with deps.turn.session_factory() as session:
            moment = await moment_of(session, person_id=page.id, now=now)
            phrases = await _phrase_moments(session, now)
        # Someone agreeing or changing their phrase has no window: what they say goes to the
        # moment, never first compared with the old phrase.
        changing = {owner for owner, _ in phrases}
        windows = [w for w in await self._windows(now) if w.person.id not in changing]
        phrase = _phrase_of(moment)
        began = self._sound_began or now
        if self._elsewhere(page.id, phrase, began, now) or self._a_copy(began, now):
            # A phrase moment of another microphone, or a saying in hand or taken there after this
            # sentence began (a late copy), the page person's or anyone's: nothing of it is heard.
            await self._send(protocol.not_taken("elsewhere"))
            return
        if (
            self._hearing == "gesture"
            and self._gesture_since is not None
            and began < self._gesture_since
        ):
            return  # begun before the page was told to wait for the gesture: not heard
        # A saying a moment or a proof window may take is in hand here while it is handled, and
        # remembered as taken once it is: a copy heard by another microphone meanwhile is dropped.
        owners = self._owners_here(page.id, phrases) | {w.person.id for w in windows}
        for owner in owners:
            LIVE_VOICES.take_in_hand(owner, self._microphone)
        try:
            await self._take(page, phrases, windows, phrase, began, now)
        finally:
            at = deps.turn.clock()
            for owner in owners:
                LIVE_VOICES.let_go(owner, self._microphone, closed_at=at)

    async def _take(
        self,
        page: Person,
        phrases: list[tuple[uuid.UUID, str | None]],
        windows: list[_Window],
        phrase: VoiceMoment | None,
        began: datetime,
        now: datetime,
    ) -> None:
        """The sentence of ``_handle``, once nothing keeps it from being heard. ``windows``: the
        proof windows open at this microphone."""
        deps = self._deps
        sound, self._sound = self._sound, None
        assert sound is not None
        try:
            heard = await hear(
                deps,
                person=page,
                microphone=self._microphone,
                pcm=sound,
                # Whoever speaks, a phrase may be said while a moment lives or a window is open
                # here: heard at home.
                for_a_proof=bool(windows) or bool(phrases),
                now=now,
                recent=self._recent_prints(now),
            )
        except CannotHear as cannot:
            await self._cannot_hear(cannot)
            return
        finally:
            del sound
        if heard is None:
            return
        if self._a_copy(began, deps.turn.clock()):
            # Taken by a moment elsewhere while it was heard here: its words reach nothing.
            await self._send(protocol.not_taken("elsewhere"))
            return
        speaker = await self._speaker(page, heard)
        who = await self._probable(page, speaker)
        mine = None if who is None else who.id
        if self._a_sentence_copy(mine, began):
            # Another microphone took this very sentence (two pages in one room): it answers it,
            # and this copy reaches nothing, silently.
            return
        if mine is not None:
            # In hand from now until it is stored (``_say``): its copy elsewhere is dropped.
            LIVE_SENTENCES.take_in_hand(mine, self._microphone, began=began)
            self._holding = mine
        try:
            await self._taken(page, phrases, windows, phrase, began, now, heard, speaker, who)
        finally:
            self._let_the_sentence_go()

    def _a_sentence_copy(self, who_id: uuid.UUID | None, began: datetime) -> bool:
        """Whether a sentence of this microphone that began at ``began``, taken for ``who_id``
        (``None``: nobody known), is a copy of one another microphone took (``LIVE_SENTENCES``):
        the same person's; for a voice nobody knows, anyone's."""
        now = self._deps.turn.clock()
        if who_id is None:
            return LIVE_SENTENCES.a_copy(self._microphone, began, now)
        return LIVE_SENTENCES.taken_elsewhere(who_id, self._microphone, began, now)

    def _let_the_sentence_go(self) -> None:
        """The sentence held in hand here is stored, or went no further: remembered as taken."""
        if self._holding is not None:
            LIVE_SENTENCES.let_go(
                self._holding, self._microphone, closed_at=self._deps.turn.clock()
            )
            self._holding = None

    async def _taken(
        self,
        page: Person,
        phrases: list[tuple[uuid.UUID, str | None]],
        windows: list[_Window],
        phrase: VoiceMoment | None,
        began: datetime,
        now: datetime,
        heard: HeardUtterance,
        speaker: Speaker,
        who: Person | None,
    ) -> None:
        """The sentence of ``_take``, once heard and taken here for ``who``."""
        deps = self._deps
        visit, heard_before = self._visit, frozenset(self._heard_here)
        await self._the_voice_is(None if who is None else speaker)
        if windows:
            # A phrase may be said here: weighed by code first, whoever speaks, before any model.
            await self._hearing_is("gesture" if self._hearing == "gesture" else "ready")
            if await self._in_the_windows(windows, who, speaker, heard, now):
                await self._keep_the_voice(page, heard, sure=who is not None and who.id == page.id)
                return
        here = self._moment_here(None if who is None else who.id, page.id, phrases)
        attended = False
        if who is None and STRANGERS.held(key_of(heard), self._microphone, heard.at):
            # A voice nobody knows he paused, or a flooded microphone: no model at all, and
            # silence (the same with or without a phrase moment here: nothing tells one is
            # awaited). A pause on the microphone's key holds only after the usual questions:
            # the page's person may share that key.
            await self._keep_the_voice(page, heard, sure=False)
            return
        if here:
            # A phrase may be said here: it reaches no model before the code weighed it.
            await self._hearing_is("gesture" if self._hearing == "gesture" else "ready")
        elif await self._attended(page, who, speaker, heard):
            attended = True
        else:
            # Not said to him: kept as a voice nobody knows says it, when it is one (case 5 asks
            # nothing of such a sentence: final fix wave C, I-2).
            await self._keep_the_voice(page, heard, sure=False)
            return
        # Said to him by a voice like nobody known: taken for the page's person by the question
        # of whose voice it is (asked only now, after the quick judgement), never learnt from.
        carried = False
        if who is None and attended:
            theirs = await self._theirs_after_all(
                page, speaker, heard, phrases, heard_before if visit == page.id else frozenset()
            )
            if theirs is not None:
                if self._a_sentence_copy(None, began):
                    # Another microphone took this very sentence while the question was asked
                    # (final fix wave C, I-1): it answers it, and this copy reaches nothing.
                    return
                who, speaker, carried = page, theirs, True
                await self._the_voice_is(speaker)
                if visit == page.id:
                    # The same visit goes on: what was said and answered here in it stays told.
                    self._heard_here |= heard_before
                LIVE_SENTENCES.take_in_hand(page.id, self._microphone, began=began)
                self._holding = page.id
        if who is None:
            # Nobody known: none of it goes in anybody's conversation. Said to him, it is answered
            # by a short exchange of its own, unless a phrase moment lives here: then no model,
            # and silence, as for a sentence not said to him (a notice would tell that a phrase
            # is awaited here).
            await self._keep_the_voice(page, heard, sure=False)
            if not here:
                deps.turn.wakes.spawn(self._a_stranger(page, heard))
            return
        if who.id != page.id:
            async with deps.turn.session_factory() as session:
                their_moment = await moment_of(session, person_id=who.id, now=now)
            phrase = _phrase_of(their_moment)
            if self._elsewhere(who.id, phrase, began, now):
                await self._keep_the_voice(page, heard, sure=False)
                await self._send(protocol.not_taken("elsewhere"))
                return
        if self._kept_for_another(who.id, page.id, phrases):
            # Another person's phrase moment here: never weighed nor written for someone else.
            await self._keep_the_voice(page, heard, sure=False)
            await self._send(protocol.not_taken(NOT_NOW))
            return
        if phrase is not None:
            await self._keep_the_voice(page, heard, sure=False)
            await self._to_the_vault(who, phrase, heard.text, now)
            return
        if not attended and not await self._attended(page, who, speaker, heard):
            # Not a phrase: weighed as any sentence now, and not for him.
            await self._keep_the_voice(page, heard, sure=False)
            return
        await self._keep_the_voice(page, heard, sure=who.id == page.id and not carried)
        await self._say(who, heard.text, heard.language, speaker)

    async def _theirs_after_all(
        self,
        page: Person,
        speaker: Speaker,
        heard: HeardUtterance,
        phrases: list[tuple[uuid.UUID, str | None]],
        heard_before: frozenset[uuid.UUID],
    ) -> Speaker | None:
        """A voice read like nobody known at this signed-in page: the page's person after all,
        unsure, when the quick model judges the sentence theirs (``channels.voice.whose``, audit
        case 5), with what was said and answered here in this visit (``heard_before``); ``None``
        (the stranger's path) otherwise. No model is asked while a phrase moment may take the
        sentence, nor while he paused this voice (under its own reference) as a stranger or the
        microphone is flooded."""
        if self._moment_here(page.id, page.id, phrases) or STRANGERS.held(
            key_of(heard), self._microphone, heard.at
        ):
            return None
        whose = await open_whose_voice(self._deps, page)
        if whose is None:
            return None
        exchanged = await self._lines_of(heard_before)
        minutes = (
            None
            if not exchanged or self._said_here_at is None
            else max(0, int((heard.at - self._said_here_at).total_seconds() // 60))
        )
        asked = Asked(
            sentence=heard.text,
            page_person=page.display_name,
            microphone=self._microphone,
            exchanged=exchanged,
            last_minutes_ago=minutes,
        )
        if not await whose.theirs(asked):
            return None
        async with self._deps.turn.session_factory() as session:
            proofs = await kept_clues(
                session,
                person_id=page.id,
                channel=VOICE_CHANNEL,
                device_id=None,
                now=heard.at,
                microphone=self._microphone,
            )
        return carrying_on(
            speaker,
            page_person=page,
            signed_in_at=self._signed_in_at,
            proofs=proofs,
            now=heard.at,
        )

    async def _lines_of(self, ids: frozenset[uuid.UUID]) -> list[Exchanged]:
        """The messages ``ids`` (said and answered here), oldest first, as lines exchanged."""
        if not ids:
            return []
        async with self._deps.turn.session_factory() as session:
            found = await session.execute(
                select(Message).where(Message.id.in_(ids)).order_by(Message.created_at)
            )
            messages = list(found.scalars())
        return [
            Exchanged("person" if m.role == "user" else "assistant", m.content)
            for m in messages
            if m.role in ("user", "assistant") and m.content.strip()
        ]

    # --- the proof windows ---------------------------------------------------------------------

    def _spoken_to(self, now: datetime) -> list[uuid.UUID]:
        """Whom a reply was said to here within ``PROOF_WINDOW_S`` (from its end), or is being
        said to now, in that order."""
        ended = LIVE_VOICES.spoken_to(self._microphone, now, PROOF_WINDOW_S)
        being_said = [
            reply.person_id
            for reply in self._replies.values()
            if (now - reply.last_at).total_seconds() <= REPLY_QUIET_AT_MOST_S
        ]
        return list(dict.fromkeys([*ended, *being_said]))

    async def _windows(self, now: datetime) -> list[_Window]:
        """The proof windows open at this microphone now (see the module docstring, step 4):
        someone spoken to here lately who waits to give a proof by their phrase. Nothing is read
        when nobody was spoken to here lately."""
        spoken_to = self._spoken_to(now)
        # A hold is told once per window: one that closed forgets it.
        self._hold_told &= set(spoken_to)
        found: list[_Window] = []
        for person_id in spoken_to:
            person = await self._person_of(person_id)
            if person is None:
                continue
            awaited = await proofs_awaited(self._deps, person, now)
            if awaited:
                found.append(_Window(person, awaited))
        return found

    async def _in_the_windows(
        self,
        windows: list[_Window],
        who: Person | None,
        speaker: Speaker,
        heard: HeardUtterance,
        now: datetime,
    ) -> bool:
        """``heard`` weighed by code against the phrase of each person whose window is open here,
        in turn, until one takes it: whether one did (it is then not to be said). Taken for that
        person by a voice that fits them, it is their proof as it is; taken for someone else, for
        nobody, or by a voice that weighs against them or is too short to tell, it is weighed as
        theirs by a voice against them (``doubted``), and a phrase that holds goes to the judge.
        A phrase held back is not compared: the next window is still weighed, and the sentence
        is dropped all the same when none takes it."""
        held = False
        for window in windows:
            person = window.person
            theirs = who is not None and who.id == person.id
            doubted = not theirs or voice_against(speaker) or speaker.voice_too_short
            said_by = (
                speaker
                if theirs
                else Speaker(
                    person.id,
                    VOICE_CHANNEL,
                    clues=(Clue("voice", "against", "weak", "code", heard.at),),
                    microphone=self._microphone,
                )
            )
            given = await self._as_a_proof(
                person, window.awaited, heard.text, said_by, now, doubted=doubted
            )
            if given == "held_back":
                held = True
            elif given != "not_an_answer":
                return True
        return held

    # --- is it for him? should he stop? ------------------------------------------------------

    def _owners_here(
        self, page_id: uuid.UUID, phrases: list[tuple[uuid.UUID, str | None]]
    ) -> set[uuid.UUID]:
        """Whose phrase moments may take a sentence of this microphone: one bound here, or the
        page person's not bound yet."""
        mine = self._microphone
        return {
            owner
            for owner, bound in phrases
            if bound == mine or (bound is None and owner == page_id)
        }

    def _a_copy(self, began: datetime, now: datetime) -> bool:
        """Whether a sentence of this microphone that began at ``began`` is a copy of a saying a
        reserved moment of ANYONE in the house takes, or took, at another microphone, whoever the
        voice is taken for here (``LiveVoices.a_copy``)."""
        return LIVE_VOICES.a_copy(self._microphone, began, now)

    def _moment_here(
        self,
        who_id: uuid.UUID | None,
        page_id: uuid.UUID,
        phrases: list[tuple[uuid.UUID, str | None]],
    ) -> bool:
        """Whether a phrase moment may take this sentence: one bound to this microphone, or the
        page person's not bound yet, or any of the probable person's (one elsewhere drops the
        sentence). Then the sentence is weighed by the code first, never by a model."""
        mine = self._microphone
        return any(
            bound == mine or (bound is None and other == page_id) for other, bound in phrases
        ) or (who_id is not None and any(o == who_id for o, _ in phrases))

    async def _attended(
        self, page: Person, who: Person | None, speaker: Speaker, heard: HeardUtterance
    ) -> bool:
        """The quick judgement on ``heard`` (see the module docstring): whether it goes its usual
        way (said to him); otherwise what became of it is done here (the caller keeps the
        voice's visit). For a voice nobody knows, the last lines given are the page person's."""
        attention = await open_attention(self._deps, page)
        if attention is None:
            if not await self._after_a_gesture():
                await self._no_attention(page)
                return False
            began = self._sound_began or heard.at
            if self._hearing != "gesture" or (
                self._gesture_since is not None and began < self._gesture_since
            ):
                # The page listened freely until it was told: this sentence came with no gesture.
                await self._hearing_is("gesture")
                return False
            return True  # said with the gesture: to him
        await self._hearing_is("ready")
        # A voice nobody knows that talks with him already: the lines of that exchange.
        with_it: list[tuple[str, str]] = (
            [] if who is not None else list(exchanged_with(key_of(heard), heard.at))
        )
        exchanged = [
            line
            for said, answered in with_it
            for line in (Exchanged("person", said), Exchanged("assistant", answered))
        ] or await self._exchanged(who or page)
        async with self._deps.turn.session_factory() as session:
            house_name = (await load_policy_settings(session)).house_domo_name
        moment = Moment(
            sentence=heard.text,
            # The name to listen for: the speaker's own for him, else the house's (a voice nobody
            # knows has only the house's).
            name=name_shown(None if who is None else who.calls_domo, house_name),
            who=None if who is None else who.display_name,
            standing=standing(speaker),
            microphone=self._microphone,
            exchanged=exchanged,
            speaking_at=speaking_at(),
        )
        judged = await attention.weigh(moment)
        if moment.speaking:
            # Only this microphone's voice: another room's is never cut by code.
            match judged.voice:
                case "stop":
                    await self.hush()
                case "drop":
                    await self.hush()
                    return False
                case _:
                    await self._send(protocol.resume())
        if judged.for_him:
            return True  # the next message, kept, never refused
        # The page person surely heard: the sorting may correct one of their own memories.
        sure = (
            who is not None
            and who.id == page.id
            and standing(speaker) == "sure"
            and not speaker.voice_too_short
        )
        self._not_for_him(who, heard, sure=sure)
        return False

    async def _exchanged(self, person: Person) -> list[Exchanged]:
        """The last lines between ``person`` and the assistant, oldest first."""
        async with self._deps.turn.session_factory() as session:
            store = ConversationStore(session)
            conversation = await store.latest_conversation(person.id)
            if conversation is None:
                return []
            messages = await store.recent_messages(conversation.id, limit=8)
        return [
            Exchanged("person" if m.role == "user" else "assistant", m.content)
            for m in messages
            if m.role in ("user", "assistant") and m.content.strip()
        ]

    async def hush(self) -> None:
        """The quick judgement said to stop: what this page's voice says goes quiet now. A reply
        still being written speaks again when its turn reads the words that stopped it
        (``_joins``); otherwise it ends silent with its turn, and is closed then (``watched``)."""
        heard = set(self._sounding)
        self._sounding.clear()
        stopped = False
        for reply_id in heard:
            found = self._replies.get(reply_id)
            if found is not None:
                self._hushed.add(reply_id)
                await found.voice.stop()
                stopped = True
            elif reply_id in self._strangers_said:
                await self._strangers_said[reply_id][0].stop()
                stopped = True
        if heard and not stopped:
            # All of it was sent already: only the page still plays it.
            await self._send(protocol.stop())

    def _not_for_him(self, who: Person | None, heard: HeardUtterance, *, sure: bool) -> None:
        """``heard`` was not said to him: kept in memory for this microphone, sorted once the room
        is quiet (``channels.voice.unaddressed``); ``sure``: surely the page person's voice."""
        self._unaddressed.add(
            HeardLine(None if who is None else who.display_name, heard.text, heard.at, sure=sure)
        )
        if self._quiet is None or self._quiet.done():
            self._quiet = asyncio.create_task(self._when_quiet())

    async def _when_quiet(self) -> None:
        """Once the room has been quiet for ``unaddressed.QUIET_FOR``, what was heard and not said
        to him is given to him in one batch, for the page's person, then dropped."""
        quiet = unaddressed.QUIET_FOR.total_seconds()
        while True:
            left = quiet - (time.monotonic() - self._last_sound)
            if left > 0:
                await asyncio.sleep(left)
            elif self.speaking or self.holds_sound:
                await asyncio.sleep(quiet)
            else:
                break
        lines = self._unaddressed.take(self._deps.turn.clock())
        try:
            page = await self._person()
        except Exception as error:
            logger.error("What was heard could not be sorted (%s)", type(error).__name__)
            return
        if page is not None and lines:
            self._deps.turn.wakes.spawn(self._sort(page, lines))

    async def _sort(self, page: Person, lines: list[HeardLine]) -> None:
        await sort_heard(self._deps, page, lines, microphone=self._microphone)

    async def _after_a_gesture(self) -> bool:
        """Whether the owner chose that the voice hears only after a gesture."""
        async with self._deps.turn.session_factory() as session:
            return (await load_policy_settings(session)).after_a_gesture

    async def _no_attention(self, page: Person) -> None:
        """No model may weigh what is heard, and the owner did not choose: the voice stays off,
        and the owner is told once, offered the choice. The page is told so as it is (its own
        state, never the "cannot hear" of a hearing that fails), and whether its person is the
        one who chooses."""
        self._hearing = "no_attention"
        await self._send(protocol.no_attention(chooses=page.id == await self._owner_id()))
        await self._send(protocol.listening(on=False))
        await self._tell_the_owner_once("no_attention")

    def _elsewhere(
        self,
        person_id: uuid.UUID,
        phrase: VoiceMoment | None,
        began: datetime,
        now: datetime,
    ) -> bool:
        """Whether a sentence of ``person_id`` heard here belongs to a phrase moment of theirs on
        another microphone, or to a saying of theirs in hand or taken there after it began (a late
        copy)."""
        return (
            phrase is not None and phrase.microphone not in (None, self._microphone)
        ) or LIVE_VOICES.taken_elsewhere(person_id, self._microphone, began, now)

    def _kept_for_another(
        self,
        person_id: uuid.UUID,
        page_id: uuid.UUID,
        phrases: list[tuple[uuid.UUID, str | None]],
    ) -> bool:
        """Whether this microphone waits for someone else's phrase than ``person_id``'s being
        agreed: a phrase moment of another is bound here (or the page person's, not bound yet:
        their first saying would bind it here). A proof awaited is weighed in its window instead
        (``_in_the_windows``)."""
        mine = self._microphone
        return any(
            other != person_id and (bound == mine or (bound is None and other == page_id))
            for other, bound in phrases
        )

    async def _probable(self, page: Person, speaker: Speaker) -> Person | None:
        """The person who probably spoke (``speaker``): the page's, someone else of the household
        (who is then listened for here too, ``live.LiveVoices``), or ``None`` for nobody known (or
        no longer in the household)."""
        if speaker.person_id is None:
            return None
        if speaker.person_id == page.id:
            return page
        who = await self._person_of(speaker.person_id)
        if who is not None and who.id not in self._leaves and not self._closed:
            self._leaves[who.id] = LIVE_VOICES.enter(who.id, self)
        return who

    async def _person(self) -> Person | None:
        """The page's person, while they are in the household."""
        return await self._person_of(self._person_id)

    async def _person_of(self, person_id: uuid.UUID) -> Person | None:
        """``person_id``'s person, while they are in the household."""
        async with self._deps.turn.session_factory() as session:
            person = await session.get(Person, person_id, populate_existing=True)
        if person is None or not is_active(person, self._deps.turn.clock()):
            return None
        return person

    # --- hearing -------------------------------------------------------------------------------

    def _recent_prints(self, now: datetime) -> list[Print]:
        while self._recent and (now - self._recent[0][0]).total_seconds() > JOIN_WITHIN_S:
            self._recent.popleft()
        return [print_ for _, print_ in self._recent]

    async def _keep_the_voice(self, person: Person, heard: HeardUtterance, *, sure: bool) -> None:
        """The reading of this sentence is kept for Part 2 (none when it had none); its print is
        remembered for the short sentences that follow and given to the learning (``sure``: the
        page's session says it is the person; a phrase moment's only keeps the visit)."""
        self.last_voice = (
            None
            if heard.reading is None
            else HeardVoice(heard.reading, heard.reference, heard.microphone, heard.at)
        )
        print_ = heard.print_
        if print_ is None:
            return
        self._recent.append((heard.at, print_))
        try:
            async with self._deps.turn.session_factory() as session:
                await learn(
                    session,
                    person_id=person.id,
                    microphone=self._microphone,
                    print_=print_,
                    thresholds=heard.thresholds,
                    sure_otherwise=sure,
                    visit_at=heard.at,
                    now=heard.at,
                )
                await session.commit()
        except Exception as error:
            # The words go on: learning waits for another sentence.
            logger.error("A voice could not be learnt from (%s)", type(error).__name__)

    async def _cannot_hear(self, cannot: CannotHear) -> None:
        await self._hearing_is(cannot.hearing)
        if not cannot.no_model:
            return
        await self._send(protocol.listening(on=False))
        await self._tell_the_owner_once("no_model")

    async def _hearing_is(self, hearing: protocol.Hearing) -> None:
        """Tell the page whether (and how) the assistant can hear, when it changed (or when he
        cannot)."""
        if hearing == self._hearing and hearing in ("ready", "gesture"):
            return
        if hearing == "gesture":
            self._gesture_since = self._deps.turn.clock()
        self._hearing = hearing
        await self._send(protocol.state(hearing))

    async def _owner_id(self) -> uuid.UUID | None:
        """The owner of the household (the first, should there be several): the one told."""
        async with self._deps.turn.session_factory() as session:
            return await session.scalar(
                select(Person.id).where(Person.role == "owner").order_by(Person.created_at).limit(1)
            )

    async def _tell_the_owner_once(self, fact: str) -> None:
        """No model may hear (``no_model``), or weigh what is heard (``no_attention``): the owner is
        told, once in this process, by the assistant in his conversation (a wake with the fact as
        data; for the weighing, with why, what each choice means and the setting that makes it).

        Once is once TOLD: the fact is told when his turn ran (``deps.Told``). While it is being
        told nobody tells it again; a turn that could not run (his conversation stayed busy past
        the wake's wait for it to be free, no key, a failure) leaves it to tell, logged, and the
        next try tells it: the next time the voice is turned on, or the next sentence heard."""
        told = self._deps.told
        if not told.begin(fact):
            return
        try:
            owner_id = await self._owner_id()
            async with self._deps.turn.session_factory() as session:
                outside = (await load_policy_settings(session)).hear_outside
        except BaseException:
            told.end(fact, told=False)
            raise
        if owner_id is None:
            told.end(fact, told=False)
            return
        content: JsonObject = {"hearing": fact, "outside_allowed": outside}
        if fact == "no_attention":
            content = {
                "hearing": fact,
                "why": NO_ATTENTION_WHY,
                "outside_allowed": outside,
                "choices": [
                    {"setting": VOICE_GESTURE_KEY, "value": "yes", "means": GESTURE_MEANS},
                    {"setting": HEAR_OUTSIDE_KEY, "value": "yes", "means": OUTSIDE_MEANS},
                ],
                "chosen_with": POLICY_TOOL,
            }
        self._deps.turn.wakes.spawn(self._tell(fact, owner_id, content))

    async def _tell(self, fact: str, owner_id: uuid.UUID, content: JsonObject) -> None:
        """Tell the owner ``fact`` (``_tell_the_owner_once``): told only when his turn ran."""
        ran = False
        try:
            ran = await self._wake(owner_id, content, tell_the_page=False)
        except Exception as error:
            logger.error("A fact of the voice could not be told (%s)", type(error).__name__)
        finally:
            self._deps.told.end(fact, told=ran)

    # --- reserved moments ----------------------------------------------------------------------

    async def _to_the_vault(
        self, person: Person, moment: VoiceMoment, heard: str, now: datetime
    ) -> None:
        # In hand from before the vault is asked to after the moment's close is remembered: a copy
        # of this saying on another page never finds the moment gone and nothing in its place.
        LIVE_VOICES.take_in_hand(person.id, self._microphone)
        outcome: PhraseOutcome | None = None
        try:
            outcome = await take_for_the_vault(
                self._deps, moment, heard, person, microphone=self._microphone, now=now
            )
        finally:
            closed_at = self._deps.turn.clock() if outcome == "agreed" else None
            LIVE_VOICES.let_go(person.id, self._microphone, closed_at=closed_at)
        if outcome == "elsewhere":
            await self._send(protocol.not_taken("elsewhere"))
            return
        await self._wake(person.id, {"phrase": outcome})

    async def _as_a_proof(
        self,
        person: Person,
        awaited: list[uuid.UUID],
        heard: str,
        speaker: Speaker,
        now: datetime,
        *,
        doubted: bool,
    ) -> Given:
        """``heard`` weighed as the phrase the requests ``awaited`` (the newest first) wait for:
        what became of it (``not_an_answer``: not taken, it may be said). Given, it is given to
        each of them."""
        first, *rest = awaited
        weighed = await self._given(person, first, heard, speaker, now, doubted=doubted)
        if weighed.given != "not_checked":
            self._outage_told = False  # the gatekeeper answered
        if weighed.given != "held_back":
            self._hold_told.discard(person.id)  # a hold, if any, is over
        match weighed.given:
            case "not_an_answer":
                pass
            case "decided":
                await self._proof_decided(person, first, weighed)
                for other in rest:
                    more = await self._given(person, other, heard, speaker, now, doubted=doubted)
                    if more.given == "decided":
                        await self._proof_decided(person, other, more)
            case "held_back":
                # Not compared, dropped all the same; told once per hold and window, as a failed
                # try (a bystander cannot tell a held phrase from a wrong one); then silently.
                # Only their own voice tells them: another's sentence never wakes them about it.
                if (
                    not doubted
                    and person.id not in self._hold_told
                    and await self._wake(person.id, {"proof": "did_not_work"})
                ):
                    # Told once it was told: a turn that could not run tells it at the next one.
                    self._hold_told.add(person.id)
            case "not_checked":
                # The gatekeeper out of reach: dropped, the request still waits; told once for
                # the outage, not once per sentence.
                if not self._outage_told and await self._wake(person.id, {"proof": "not_checked"}):
                    self._outage_told = True
            case "another_proof" | "did_not_work":
                # The request still waits: he is woken to say it (another proof, a failed try),
                # and the next sentences are weighed again.
                await self._wake(person.id, {"proof": weighed.given})
        return weighed.given

    async def _given(
        self,
        person: Person,
        confirmation_id: uuid.UUID,
        heard: str,
        speaker: Speaker,
        now: datetime,
        *,
        doubted: bool,
    ) -> Weighed:
        return await give_the_phrase(
            self._deps,
            confirmation_id,
            person=person,
            heard=heard,
            speaker=speaker,
            origin=self._origin,
            now=now,
            doubted=doubted,
        )

    async def _proof_decided(
        self, person: Person, confirmation_id: uuid.UUID, weighed: Weighed
    ) -> None:
        assert weighed.outcome is not None
        await Conversation(self._deps.turn).wake_after_decision(
            confirmation_id=confirmation_id,
            outcome=weighed.outcome,
            record=weighed.record,
            decider=person,
            via="proof",
        )

    async def _moment_asked(self, event: Event, person_id: uuid.UUID) -> None:
        """A reply of ``person_id``'s said here shows the tool that opened a phrase moment of
        theirs: the moment is this microphone's."""
        if (
            isinstance(event, Acted)
            and event.record.status == "done"
            and event.record.action_type in PHRASE_ACTIONS
        ):
            await bind_phrase_moment(
                self._deps, person_id, self._microphone, self._deps.turn.clock()
            )

    async def _wake(
        self, person_id: uuid.UUID, content: JsonObject, *, tell_the_page: bool = True
    ) -> bool:
        """Wake the assistant in ``person_id``'s conversation with a fact of the channel, as data:
        he says it in his own words, through whoever listens to them. Whether his turn ran. When
        he cannot be woken (his conversation stays busy past the wake's wait, no key), a line is
        logged (the fact's name alone) and the page is told (``failed``, ``not_told``)."""
        # The channel's own fact: given by the house's code, never told as a tool of a name.
        wakes = (ToolWake(tool=WAKE_TOOL, content=content, given_by="channel"),)
        wake = self._deps.wake
        if wake is not None:
            ran = await wake(person_id, wakes)
        else:
            ran = await tool_wake_turn(self._deps.turn, person_id, wakes)
        if not ran:
            logger.warning(
                "A fact of the voice could not be told: his turn could not run (%s)",
                _fact_name(content),
            )
            if tell_the_page:
                await self._send(protocol.failed("not_told"))
        return ran

    # --- a voice nobody knows ------------------------------------------------------------------

    async def _a_stranger(self, page: Person, heard: HeardUtterance) -> None:
        """A voice nobody knows said ``heard`` to him at this page: a short exchange of its own
        (``stranger.answer_a_stranger``), said aloud here by the voice of the page's person, on
        their key, in the language heard. When nothing is said, the page is told as for any
        sentence not taken. Never raises: what goes wrong is logged by its kind."""
        turn = self._deps.turn
        reply_id = uuid.uuid4()
        said = False
        try:
            async with turn.session_factory() as session:
                texts = await person_texts(session, page)
                house = (await load_policy_settings(session)).house_language
            voice = VoiceListener(
                self._sink,
                reply_id=reply_id,
                texts=texts,
                speech=lambda: self._deps.speech.open(page, "say"),
                heard_language=heard.language or None,
                person_language=None,
                house_language=house,
                decision_sentence=_no_decision,
                card_in_words=sentence_of_card,
            )
            key = key_of(heard)
            self._strangers_said[reply_id] = (voice, key)
            try:
                said = await answer_a_stranger(
                    self._deps, page_person=page, heard=heard, reference=key, listener=voice
                )
            finally:
                await voice.close()
                if not said:
                    self._strangers_said.pop(reply_id, None)
        except Exception as error:
            logger.error("A voice nobody knows could not be answered (%s)", type(error).__name__)
        if (
            not said
            and not self._closed
            and not STRANGERS.paused(key_of(heard), self._microphone, self._deps.turn.clock())
        ):
            # Paused by him or past the flood bound: silence, as above.
            await self._send(protocol.not_taken(NOT_NOW))

    async def _warned(self, key: str) -> None:
        """A reply to the voice ``key`` was played whole here: it was told he listens, if he
        judged it useful (``voice_refs.warned_at``)."""
        STRANGERS.said_whole(key)
        try:
            async with self._deps.turn.session_factory() as session:
                await mark_warned(session, key, self._deps.turn.clock())
                await session.commit()
        except Exception as error:
            logger.error("A voice could not be marked as told (%s)", type(error).__name__)

    # --- the conversation ----------------------------------------------------------------------

    async def _speaker(self, person: Person, heard: HeardUtterance) -> Speaker:
        """Who probably speaks the sentence ``heard`` (``voice_speaker``): its voice read against
        the page's person (a voice that could not be read says nothing: no print), the page's
        session, and the proofs kept for the page's person on this channel and microphone."""
        async with self._deps.turn.session_factory() as session:
            proofs = await kept_clues(
                session,
                person_id=person.id,
                channel=VOICE_CHANNEL,
                device_id=None,
                now=heard.at,
                microphone=self._microphone,
            )
        return voice_speaker(
            page_person=person,
            reading=heard.reading or NOT_LEARNED,
            print_=None if heard.reading is None else heard.print_,
            microphone=self._microphone,
            signed_in_at=self._signed_in_at,
            proofs=proofs,
            now=heard.at,
        )

    async def _say(
        self, person: Person, text: str, language: str, speaker: Speaker, *, held: bool = True
    ) -> None:
        """Say ``text`` in the conversation of ``person``, who probably said it (``speaker``), and
        have the reply said aloud here. While a reply is being written there, the words are kept
        for its turn with this channel's request (``Conversation.say``), and what answers them is
        said here (``_joins``). Heard at a page signed in as someone else, they never join a turn
        of theirs: while one runs, they wait here and are answered after it (``_answer_later``)."""
        turn = self._deps.turn
        conversation = Conversation(turn)
        elsewhere = speaker.page_person_id is not None
        # The turn answers in the language heard (else the person's, else the house's).
        request = TurnRequest(
            speaker=speaker,
            abilities=VOICE,
            origin=self._origin,
            language=language or None,
            also_hears=None if elsewhere else self._joins(person, language or None),
            # Heard at a page signed in as someone else: only what was said and answered here
            # in this visit is told of their history (review 3 of Task 24).
            heard_here=(
                None
                if not elsewhere
                else frozenset(self._heard_here)
                if self._visit == speaker.person_id
                else frozenset()
            ),
        )
        try:
            said, reply = await conversation.say(person, text, speaker, request)
        except NotSayable:
            return
        except NoKey:
            await self._send(protocol.failed("no_key"))
            return
        except ReplyBeingWritten as busy:
            self._answer_later(person, text, language, speaker, busy.reply_id)
            return
        finally:
            # Stored (or not to be): a copy of it elsewhere is told by when it was taken. Words
            # said later (``_when_written``) hold nothing: the worker's sentence is not theirs.
            if held:
                self._let_the_sentence_go()
        if self._visit == speaker.person_id:
            self._heard_here.update({said.id} if reply is None else {said.id, reply.id})
            self._said_here_at = turn.clock()
        if reply is None:
            return  # kept for the turn being written
        listener = await self._reply(person, reply.id, language, asked=True)
        answering = False
        try:
            answering = await conversation.answer(person, reply.id, request, listener)
        except (NoKey, LookupError):
            pass
        finally:
            if not answering:
                # No turn says it: its listener goes now (a worker would wait for ever).
                self._replies.pop(reply.id, None)
                await listener.voice.close()

    def _answer_later(
        self, person: Person, text: str, language: str, speaker: Speaker, reply_id: uuid.UUID
    ) -> None:
        """Words of ``person`` heard here, a page signed in as someone else, while the reply
        ``reply_id`` is being written in their conversation (elsewhere, with their whole context):
        they wait here, stored nowhere, and are said once it is over, for a turn of their own
        that knows only this visit (``_say``), in the order they were heard, however long that
        reply takes. They go with the page; past ``WAITING_LATER_AT_MOST`` waiting here, a new one
        is not taken (the page told)."""
        if self._closed:
            return
        if len(self._later) >= WAITING_LATER_AT_MOST:
            logger.info("Words heard at someone else's page were dropped: too many wait")
            self._deps.turn.wakes.spawn(self._send(protocol.failed("failed")))
            return
        task = asyncio.create_task(self._when_written(person, text, language, speaker, reply_id))
        self._later.add(task)
        task.add_done_callback(self._later.discard)

    async def _when_written(
        self,
        person: Person,
        text: str,
        language: str,
        speaker: Speaker,
        reply_id: uuid.UUID,
    ) -> None:
        try:
            # One at a time, in the order heard (the lock serves its waiters in turn).
            async with self._later_turn:
                while await self._being_written(reply_id):
                    await asyncio.sleep(WRITTEN_CHECK_S)
                if not self._closed:
                    await self._say(person, text, language, speaker, held=False)
        except Exception as error:
            logger.error("Words waiting for a reply could not be said (%s)", type(error).__name__)

    async def _being_written(self, reply_id: uuid.UUID) -> bool:
        """Whether the reply ``reply_id`` is still being written (``reply_is_alive``)."""
        turn = self._deps.turn
        async with turn.session_factory() as session:
            reply = await session.get(Message, reply_id, populate_existing=True)
        return (
            reply is not None
            and reply.status == "streaming"
            and reply_is_alive(reply, turn.streaming_replies, turn.clock())
        )

    def _joins(self, person: Person, language: str | None) -> AlsoHears:
        """Who hears, here, the reply that answers words of ``person`` kept while a reply was being
        written (``TurnRequest.also_hears``), given its id: a reply of another channel is said
        here from then on (a listener of its own, its events given by its turn); this page's own
        reply, stopped by the quick judgement, speaks again; one this page says on his own is from
        then on given by its turn (a request it shows opens its moment here); one it says already
        needs nothing more: said once."""

        async def joins(reply_id: uuid.UUID) -> Listener | None:
            if self._closed:
                return None
            found = self._replies.get(reply_id)
            if found is None:
                return await self._reply(person, reply_id, language, asked=True)
            if reply_id in self._hushed:
                self._hushed.discard(reply_id)
                await found.voice.resume()
            if found.asked:
                return None
            found.asked = True
            return found

        return joins

    async def _reply(
        self, person: Person, reply_id: uuid.UUID, language: str | None, *, asked: bool
    ) -> _Reply:
        """The listener that says the reply ``reply_id`` of ``person``'s conversation aloud, in
        ``language`` (else the person's, else the house's). It is said by the voice of this page,
        as its sound is heard: on the page person's speech (another person heard here may have no
        speech of their own)."""
        turn = self._deps.turn
        async with turn.session_factory() as session:
            texts = await person_texts(session, person)
            house = (await load_policy_settings(session)).house_language
        voice_of = await self._person() or person

        async def decided(event: Decided) -> str | None:
            card = await card_after_decision(
                turn.session_factory,
                event.confirmation_id,
                outcome=event.outcome,
                record=event.record,
                viewer=person,
                texts=texts,
                now=turn.clock(),
            )
            return decision_sentence(card, texts)

        reply = _Reply(
            self,
            reply_id,
            VoiceListener(
                self._sink,
                reply_id=reply_id,
                texts=texts,
                speech=lambda: self._deps.speech.open(voice_of, "say"),
                heard_language=language or None,
                person_language=person.language,
                house_language=house,
                decision_sentence=decided,
                # Words only: never a button the voice cannot show.
                card_in_words=sentence_of_card,
            ),
            person_id=person.id,
            asked=asked,
        )
        self._replies[reply_id] = reply
        return reply

    async def watched(self, reply: _Reply, event: Event) -> None:
        """What the session does with an event of a reply it says (``_Reply``): a moment it asks
        for, and its end. Never raises: the reply is the turn's."""
        try:
            if isinstance(event, Acted | Decided):
                await self._moment_asked(event, reply.person_id)
        except Exception as error:
            logger.error("A reply's event could not be followed (%s)", type(error).__name__)
        finally:
            if isinstance(event, Ended):
                # Said to its end (or given up): its worker stops, whatever came before.
                self._reply_over(reply.reply_id)
                self._hushed.discard(reply.reply_id)
                try:
                    await reply.voice.close()
                except Exception as error:
                    logger.error("A reply could not be let go (%s)", type(error).__name__)

    async def _unasked(self, person_id: uuid.UUID, reply_id: uuid.UUID, event: Event) -> None:
        """What the assistant says on his own to ``person_id`` (``Listeners``): said aloud. A reply
        whose turn gives this page its events itself (``_joins``) is not given twice."""
        reply = self._replies.get(reply_id)
        if reply is not None and reply.asked:
            return
        if reply is None:
            if isinstance(event, Ended):
                return
            person = await self._person_of(person_id)
            if person is None:
                raise ListenerGone
            reply = await self._reply(person, reply_id, None, asked=False)
            await reply.open()
        try:
            await reply.emit(event)
        except ListenerGone:
            self._reply_over(reply_id)
            if not self._sink.open:
                raise

    def _reply_over(self, reply_id: uuid.UUID) -> None:
        """The reply ``reply_id`` said here is over (said, cut or given up): the proof window of
        its person runs from now."""
        reply = self._replies.pop(reply_id, None)
        if reply is not None:
            LIVE_VOICES.said_to(self._microphone, reply.person_id, self._deps.turn.clock())

    async def _send(self, data: JsonObject) -> None:
        try:
            await self._sink.send_json(data)
        except Exception as error:
            logger.info("The page could not be told (%s)", type(error).__name__)


def _fact_name(content: JsonObject) -> str:
    """The name of a fact of the channel, for a log: its first entry, a code of the channel
    (``hearing=no_attention``, ``proof=did_not_work``), never anything heard."""
    for key, value in content.items():
        return f"{key}={value}" if isinstance(value, str) else key
    return "nothing"


async def _no_decision(event: Decided) -> str | None:
    """A reply to a voice nobody knows decides nothing: no sentence for a decision."""
    return None


def _phrase_of(moment: VoiceMoment | None) -> VoiceMoment | None:
    """``moment`` when it is a phrase moment (``phrase_agree``, ``phrase_change``)."""
    return moment if moment is not None and moment.purpose in _PHRASE_PURPOSES else None


async def _phrase_moments(
    session: AsyncSession, now: datetime
) -> list[tuple[uuid.UUID, str | None]]:
    """Every live phrase moment of the house: whose, and the microphone it is bound to."""
    found = await session.execute(
        select(VoiceMoment.person_id, VoiceMoment.microphone).where(
            VoiceMoment.expires_at > now, VoiceMoment.purpose.in_(_PHRASE_PURPOSES)
        )
    )
    return [(person_id, microphone) for person_id, microphone in found.all()]
