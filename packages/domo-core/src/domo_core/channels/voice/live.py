"""The voice sessions live in this process, by person, and the reserved moments shared by them
(``LIVE_VOICES``: one for the process, which the application keeps as ``app.state.live_voices``
and the tool that stops the listening reaches, ``tools.voice_tools``).

A person may listen at several pages at once (a laptop and a phone in one room): a sentence can be
heard by two of them. A phrase moment (a row, ``channels.voice.moments``) is the PERSON's, bound to
the microphone of the reply that opened it or of its first saying; a proof awaited by their phrase
is weighed inside a proof window of a microphone (``channels.voice.session``: the assistant spoke to
them there lately, kept here by microphone and person: ``said_to``, so that a page reloaded or
reconnected within the window finds it open; a restart of the core loses it). A saying one of these
takes is held "in hand" at its microphone while it is handled, and remembered as taken when it is:
another page's copy of the same sentence is never weighed again nor sent to the conversation. That
holds past the saying: a sentence of another microphone that began before it was taken (or within
``LATE_COPY_MARGIN_SECONDS`` after) is a late copy and is dropped the same way (``taken_elsewhere``,
``a_copy``, from when each moment of speech began).

A stop of the listening (``voice_stop_listening``) is acted on at once through here
(``stop_listening``): the page saying the reply whose turn asked for it is told to turn its
microphone off; when no page says that reply (a typed message asked), every listening page of the
person is.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Protocol

# A sentence of another microphone that began before a reserved moment closed, or within this many
# seconds after, is a late copy of the moment's sentence: dropped like one heard during it. The
# close is remembered for ``CLOSE_KEPT_SECONDS``, then forgotten.
LATE_COPY_MARGIN_SECONDS: Final = 2.0
CLOSE_KEPT_SECONDS: Final = 60.0


class LiveSession(Protocol):
    """A voice session as the registry knows it."""

    def says(self, reply_id: uuid.UUID) -> bool:
        """Whether this session's page says the reply ``reply_id``."""
        ...

    async def turn_off(self) -> None:
        """Tell the page to turn its microphone off."""
        ...


@dataclass(frozen=True)
class _Closed:
    """When a reserved moment of the person closed, and on which microphone."""

    microphone: str
    at: datetime
    began: datetime | None = None


class LiveVoices:
    """See the module docstring. ``with_it_only``: a copy is only a sentence that began with the
    one taken (within ``LATE_COPY_MARGIN_SECONDS`` of when it began), never one begun later
    (``LIVE_SENTENCES``)."""

    def __init__(self, *, with_it_only: bool = False) -> None:
        self._with_it_only = with_it_only
        self._sessions: dict[uuid.UUID, list[LiveSession]] = {}
        self._in_hand: dict[uuid.UUID, str] = {}
        # When the saying in hand began, when it is known.
        self._began: dict[uuid.UUID, datetime] = {}
        self._closed: dict[uuid.UUID, _Closed] = {}
        # When a reply to a person said at a microphone last ended: their proof window there runs
        # from then (``channels.voice.session``), whichever page of the microphone said it, so
        # that a page reloaded or reconnected finds it open. Lost when the process stops.
        self._said_to: dict[tuple[str, uuid.UUID], datetime] = {}

    def enter(self, person_id: uuid.UUID, session: LiveSession) -> Callable[[], None]:
        """``session`` listens for ``person_id``; what makes it leave (once is enough). Leaving
        forgets no mark of a saying: those of the page's microphone go with the page
        (``forget``), another microphone's stay as they are."""
        self._sessions.setdefault(person_id, []).append(session)

        def leave() -> None:
            sessions = self._sessions.get(person_id, [])
            if session in sessions:
                sessions.remove(session)
            if not sessions:
                self._sessions.pop(person_id, None)

        return leave

    def forget(self, microphone: str) -> None:
        """The page of ``microphone`` is gone: the marks of the sayings taken there (in hand, or
        closed lately) are forgotten, whoever's; another microphone's stay as they are."""
        for person_id in [p for p, hand in self._in_hand.items() if hand == microphone]:
            del self._in_hand[person_id]
            self._began.pop(person_id, None)
        for person_id in [p for p, c in self._closed.items() if c.microphone == microphone]:
            del self._closed[person_id]

    def persons(self) -> set[uuid.UUID]:
        """Whom a live session listens for now."""
        return set(self._sessions)

    # --- whose sentence it is ------------------------------------------------------------------

    def taken_elsewhere(
        self, person_id: uuid.UUID, microphone: str, began: datetime, now: datetime
    ) -> bool:
        """Whether a sentence of ``microphone`` that began at ``began`` belongs to a reserved
        moment of another microphone: one in hand there now (a saying being taken), or one that
        closed there after it began (``LATE_COPY_MARGIN_SECONDS`` given): a late copy. With
        ``with_it_only``, only a sentence that began with the one taken there is its copy."""
        hand = self._in_hand.get(person_id)
        if hand is not None and hand != microphone:
            if not self._with_it_only:
                return True
            if _with_it(began, self._began.get(person_id)):
                return True
        closed = self._closed.get(person_id)
        if closed is None:
            return False
        if (now - closed.at).total_seconds() > CLOSE_KEPT_SECONDS:
            del self._closed[person_id]
            return False
        if closed.microphone == microphone:
            return False
        if self._with_it_only:
            return _with_it(began, closed.began)
        return began <= closed.at + timedelta(seconds=LATE_COPY_MARGIN_SECONDS)

    def a_copy(self, microphone: str, began: datetime, now: datetime) -> bool:
        """Whether a sentence of ``microphone`` that began at ``began`` is a copy of a saying a
        reserved moment of ANYONE takes, or took, at another microphone (``taken_elsewhere`` for
        each person with a saying in hand or a moment closed lately)."""
        return any(
            self.taken_elsewhere(person_id, microphone, began, now)
            for person_id in {*self._in_hand, *self._closed}
        )

    def take_in_hand(
        self, person_id: uuid.UUID, microphone: str, *, began: datetime | None = None
    ) -> None:
        """A saying of the person's phrase moment is being taken on ``microphone`` (``began``:
        when it began, when known)."""
        self._in_hand[person_id] = microphone
        if began is None:
            self._began.pop(person_id, None)
        else:
            self._began[person_id] = began

    def let_go(self, person_id: uuid.UUID, microphone: str, *, closed_at: datetime | None) -> None:
        """The saying taken on ``microphone`` is over; ``closed_at``: the moment closed with it
        (remembered first, so that there is no instant with neither)."""
        held = self._in_hand.get(person_id) == microphone
        if closed_at is not None:
            began = self._began.get(person_id) if held else None
            self._closed[person_id] = _Closed(microphone, closed_at, began)
        if held:
            del self._in_hand[person_id]
            self._began.pop(person_id, None)

    # --- the proof windows ---------------------------------------------------------------------

    def said_to(self, microphone: str, person_id: uuid.UUID, at: datetime) -> None:
        """A reply to ``person_id`` said at ``microphone`` ended at ``at``."""
        self._said_to.pop((microphone, person_id), None)
        self._said_to[(microphone, person_id)] = at

    def spoken_to(self, microphone: str, now: datetime, within: float) -> list[uuid.UUID]:
        """Whom a reply said at ``microphone`` ended for within ``within`` seconds of ``now``, the
        oldest first; what is older is forgotten (at every microphone)."""
        for key, at in list(self._said_to.items()):
            if (now - at).total_seconds() > within:
                del self._said_to[key]
        return [person_id for (mic, person_id) in self._said_to if mic == microphone]

    # --- stopping ----------------------------------------------------------------------------

    async def stop_listening(self, person_id: uuid.UUID, reply_id: uuid.UUID | None) -> int:
        """Turn the person's listening off now: the pages saying ``reply_id`` (the reply whose turn
        asked), else every page of theirs. How many pages were told."""
        sessions = list(self._sessions.get(person_id, []))
        saying = [s for s in sessions if reply_id is not None and s.says(reply_id)]
        told = saying or sessions
        for session in told:
            await session.turn_off()
        return len(told)


def _with_it(began: datetime, taken_began: datetime | None) -> bool:
    """Whether a sentence that began at ``began`` began with the one taken (``taken_began``,
    ``None``: not known, then never), within ``LATE_COPY_MARGIN_SECONDS``."""
    return (
        taken_began is not None
        and abs((began - taken_began).total_seconds()) <= LATE_COPY_MARGIN_SECONDS
    )


# The voice sessions of this process (one process serves the voice, as it writes the replies).
LIVE_VOICES: Final = LiveVoices()
# The ordinary sentences taken for a person (final fix wave, voice I2), marked as the sayings of
# the reserved moments are but apart from them: the first microphone to take a sentence of that
# person answers it, and a copy of it heard at another microphone (the same person's, begun with it,
# within ``LATE_COPY_MARGIN_SECONDS``) is dropped silently. A sentence begun later, or another
# person's in another room, is never taken for a copy (final fix wave B, I-4).
LIVE_SENTENCES: Final = LiveVoices(with_it_only=True)
