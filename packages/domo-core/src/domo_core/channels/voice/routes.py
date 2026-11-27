"""The page's voice socket: ``/chat/voice`` (spec 6.1, 6.2), the first WebSocket of the core.

The security middleware (``web.security``) only looks at ``http`` scopes, so this route checks the
handshake itself, before anything is accepted: exactly one ``Origin`` naming the ``Host`` the
browser used (``web.security.is_same_origin``; no ``Referer`` stands in for it, a browser always
sends ``Origin`` with a socket), then the page's session cookie, read as the pages read it
(``accounts.sessions.resolve_session``). Anything else is closed with ``CLOSE_POLICY`` before it is
accepted, which a server answers 403: the browser learns nothing of why. Whoever the session is, is
the socket's person.

Once accepted, the page says ``hello`` (within ``HELLO_SECONDS``) with the id of its microphone and
the id of the person it was rendered for. A tab rendered for someone else than the session's
person (another person signed in since, in another tab) is closed with ``CLOSE_ANOTHER_PERSON``:
it reloads. When the application serves no voice session (``app.state.voice_sessions`` unset,
as in tests of the route alone), the socket is accepted and closed with ``CLOSE_NOT_YET``, a code
the page can read. Otherwise the socket is handed to a voice session
(``VoiceSessionFactory``): the route reads the frames (``channels.voice.protocol``), gathers the
sound of each moment of speech and gives it whole (``VoiceSession.heard``); the session speaks to
the page through the ``VoiceSink`` it is given.

Bounds, for a socket any signed-in browser can open: a text frame is a message of the protocol or
the socket is closed; a binary frame is sound, inside a moment of speech only, at most
``MAX_FRAME_BYTES`` (the server itself refuses a frame a little bigger, ``__main__``); a moment
longer than ``MAX_UTTERANCE_BYTES`` is handed on there and goes on (two ``heard`` for one
``speech_began``), what is shorter than ``MIN_UTTERANCE_BYTES`` is dropped, a moment the page leaves
open for ``UTTERANCE_IDLE_SECONDS`` ends as if ``speech_end`` came. In any sliding minute at most
``UTTERANCES_PER_MINUTE`` moments are begun (``speech_began``, then ``heard`` or ``nothing_heard``)
and at most as many pieces are handed on; the others are dropped without a word to the session.

A session cookie has at most ``PENDING_PER_COOKIE`` sockets waiting for their ``hello``: one more is
refused before it is accepted. One socket per session cookie and ``SOCKETS_PER_PERSON`` per
person: a new one closes the oldest
(``CLOSE_REPLACED``), once its ``hello`` named the session's person (a socket that never says hello,
or a tab of someone else, takes no other's place). That cap is a default for one person at a few
pages; raise it when a person has several microphones (rooms, a phone), and revisit then the
assumption it rests on: every microphone is a page with a signed-in person (spec 6.2).

The session is read again before each piece of sound is handed on, and every ``SEEN_EVERY`` seconds
as the API's stream does: someone signed out, removed or whose stay ended is closed
(``CLOSE_POLICY``), and nothing they said after is handed on.

The sound lives in memory for the time of one moment and is never logged nor written anywhere; the
frames are never logged either, only the kind of a refusal.
"""

import hashlib
import json
import logging
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Final, Protocol
from uuid import UUID

import anyio
from fastapi import APIRouter, Depends, WebSocket
from starlette.datastructures import Headers
from starlette.types import Message
from starlette.websockets import WebSocketDisconnect, WebSocketState

from domo_core.accounts.sessions import SESSION_COOKIE, resolve_session
from domo_core.channels.page.speaker import signed_in_at_of_token
from domo_core.channels.voice import protocol
from domo_core.channels.voice.listener import SinkClosed, VoiceSink
from domo_core.channels.voice.protocol import (
    CLOSE_ANOTHER_PERSON,
    CLOSE_ERROR,
    CLOSE_NORMAL,
    CLOSE_NOT_YET,
    CLOSE_POLICY,
    CLOSE_REPLACED,
    MAX_FRAME_BYTES,
    MAX_UTTERANCE_BYTES,
    MIN_UTTERANCE_BYTES,
    SAMPLE_BYTES,
    Hello,
    Off,
    Played,
    ProtocolError,
    SpeechEnd,
    SpeechStart,
)
from domo_core.db import SessionFactory
from domo_core.llm.types import JsonObject
from domo_core.web.deps import get_clock
from domo_core.web.security import is_same_origin

logger = logging.getLogger(__name__)

VOICE_PATH: Final = "/chat/voice"

# How long the page has to say ``hello`` once the socket is open, in seconds.
HELLO_SECONDS: Final = 10.0
# A moment of speech with no frame for this long ends as if ``speech_end`` came, in seconds.
UTTERANCE_IDLE_SECONDS: Final = 5.0
# How many moments of speech a socket begins, and how many pieces it hands on, in a minute.
UTTERANCES_PER_MINUTE: Final = 20
# How many voice sockets one person keeps open (pages of several devices): see the docstring.
SOCKETS_PER_PERSON: Final = 4
# How many sockets of one session cookie may wait for their ``hello`` at once: past it, a new one
# is refused (``CLOSE_POLICY``), so that a page cannot hold sockets open without saying who it is.
PENDING_PER_COOKIE: Final = 2
# How often the session of the socket is read again, in seconds (the API's stream's ``SEEN_EVERY``).
SEEN_EVERY: Final = 20.0

router = APIRouter()


class VoiceSession(Protocol):
    """One socket's voice session (Task 14). Each call returns soon: what takes time (hearing, a
    turn, speaking) runs in the session's own tasks.

    A moment begins with ``speech_began`` and ends with one ``heard`` or more (a moment longer than
    ``MAX_UTTERANCE_BYTES`` is handed on in pieces: two ``heard`` for one ``speech_began``), or
    with ``nothing_heard``. All three are bounded per minute (``UTTERANCES_PER_MINUTE``)."""

    async def speech_began(self) -> None:
        """Someone begins to speak at the page (the page lowers its sound by itself)."""
        ...

    async def heard(self, sound: bytes) -> None:
        """A moment of speech, or a piece of a long one (16-bit, 16 000 Hz, mono). The session
        keeps no reference to it once handled."""
        ...

    async def nothing_heard(self) -> None:
        """The moment that began came to nothing to hear (too short, or one too many)."""
        ...

    async def played(self, reply: UUID, *, whole: bool) -> None:
        """The page played ``reply`` to its end, or not."""
        ...

    async def off(self) -> None:
        """The person turned the voice off at the page; the socket closes next."""
        ...

    async def close(self) -> None:
        """The socket is closed, whatever the way."""
        ...


class VoiceSessionFactory(Protocol):
    """Opens the voice session of a socket. ``person_id`` is the id of the page's signed-in
    person: an id, not a person read once, for their rights and role are read again at each turn
    (and the route reads again that they are still there). ``microphone`` is the id the page gave
    its microphone: the page's claim, never a proof of a place. ``origin`` is the address the page
    was reached at (its checked ``Origin``, with a ``/``: the links a turn makes). ``sink`` is the
    way back to the page. ``signed_in_at`` is when the page's session was opened (``None`` when it
    cannot be read): the date of the session's clue."""

    async def __call__(
        self,
        *,
        person_id: UUID,
        microphone: str,
        origin: str,
        sink: VoiceSink,
        signed_in_at: datetime | None,
    ) -> VoiceSession: ...


def get_voice_sessions(websocket: WebSocket) -> VoiceSessionFactory | None:
    """The factory of the voice sessions, kept on ``app.state.voice_sessions`` (``None`` while the
    voice is not served). Tests replace it through ``app.dependency_overrides``."""
    factory: VoiceSessionFactory | None = getattr(websocket.app.state, "voice_sessions", None)
    return factory


# --- the sockets that are open ---------------------------------------------------------------


@dataclass(eq=False)
class _Open:
    """One open socket, as the registry knows it."""

    cookie: str  # a hash of the session token: the token itself is not kept
    person_id: UUID
    replaced: anyio.Event = field(default_factory=anyio.Event)


def _cookie_of(token: str) -> str:
    """A hash of a session token: the token itself is not kept."""
    return hashlib.sha256(token.encode()).hexdigest()


class VoiceSockets:
    """The voice sockets open in this process (``app.state.voice_sockets``, made with the
    application): one per session cookie and ``SOCKETS_PER_PERSON`` per person, the newest taking
    the place of the oldest; and, before their ``hello``, at most ``PENDING_PER_COOKIE`` waiting
    per session cookie."""

    def __init__(self) -> None:
        self._open: list[_Open] = []
        self._pending: dict[str, int] = {}

    def wait(self, token: str) -> bool:
        """One more socket of this cookie waits for its ``hello``: ``False`` (and nothing counted)
        when as many wait already."""
        cookie = _cookie_of(token)
        if self._pending.get(cookie, 0) >= PENDING_PER_COOKIE:
            return False
        self._pending[cookie] = self._pending.get(cookie, 0) + 1
        return True

    def waited(self, token: str) -> None:
        """A socket of this cookie waits no more (it said ``hello``, or it ended)."""
        cookie = _cookie_of(token)
        left = self._pending.get(cookie, 0) - 1
        if left > 0:
            self._pending[cookie] = left
        else:
            self._pending.pop(cookie, None)

    def enter(self, token: str, person_id: UUID) -> _Open:
        entry = _Open(_cookie_of(token), person_id)
        same_cookie = [other for other in self._open if other.cookie == entry.cookie]
        same_person = [
            other
            for other in self._open
            if other.person_id == person_id and other not in same_cookie
        ]
        gone = same_cookie + same_person[: max(len(same_person) - SOCKETS_PER_PERSON + 1, 0)]
        for other in gone:
            self.leave(other)
            other.replaced.set()
        self._open.append(entry)
        return entry

    def leave(self, entry: _Open) -> None:
        if entry in self._open:
            self._open.remove(entry)


# --- the way back to the page ----------------------------------------------------------------


class _Sink:
    """The page's socket as the session sees it (``VoiceSink``): sends are one at a time, and a
    socket that is closed raises ``SinkClosed``."""

    def __init__(self, websocket: WebSocket) -> None:
        self._websocket = websocket
        self._lock = anyio.Lock()
        self._open = True

    @property
    def open(self) -> bool:
        return self._open

    def shut(self) -> None:
        self._open = False

    async def send_json(self, data: JsonObject) -> None:
        await self._send({"type": "websocket.send", "text": json.dumps(data)})

    async def send_bytes(self, data: bytes) -> None:
        await self._send({"type": "websocket.send", "bytes": data})

    async def _send(self, message: dict[str, object]) -> None:
        async with self._lock:
            if not self._open:
                raise SinkClosed
            try:
                await self._websocket.send(message)
            except (WebSocketDisconnect, RuntimeError, OSError):
                self._open = False
                raise SinkClosed from None


# --- the route -------------------------------------------------------------------------------


class _Closing(Exception):
    """Ends the socket with ``code``; ``why`` is a fixed word for the log."""

    def __init__(self, code: int, why: str) -> None:
        super().__init__(why)
        self.code = code


@dataclass(frozen=True)
class _Signed:
    """How the socket's session is read again: its token and the clock of the pages."""

    websocket: WebSocket
    token: str
    clock: Callable[[], datetime]

    async def person_id(self) -> UUID | None:
        """The id of the session's person now, or ``None`` (signed out, removed, stay over)."""
        session_factory: SessionFactory = self.websocket.app.state.session_factory
        async with session_factory() as session:
            person = await resolve_session(session, self.token, self.clock())
            await session.commit()
        return None if person is None else person.id

    async def signed_in_at(self) -> datetime | None:
        """When the session was opened (the person signed in), or ``None`` when it is gone."""
        session_factory: SessionFactory = self.websocket.app.state.session_factory
        async with session_factory() as session:
            return await signed_in_at_of_token(session, self.token)


@router.websocket(VOICE_PATH)
async def voice_socket(
    websocket: WebSocket,
    sessions: Annotated[VoiceSessionFactory | None, Depends(get_voice_sessions)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
) -> None:
    headers = Headers(scope=websocket.scope)
    token = websocket.cookies.get(SESSION_COOKIE)
    if len(headers.getlist("origin")) != 1 or not is_same_origin(headers) or not token:
        await websocket.close(CLOSE_POLICY)
        return
    signed = _Signed(websocket, token, clock)
    person_id = await signed.person_id()
    if person_id is None:
        await websocket.close(CLOSE_POLICY)
        return
    if sessions is None:
        # Accepted first: a socket closed before it is accepted shows the page no code.
        await websocket.accept()
        await websocket.close(CLOSE_NOT_YET)
        return
    registry: VoiceSockets = websocket.app.state.voice_sockets
    if not registry.wait(token):
        await websocket.close(CLOSE_POLICY)
        return
    waiting = True

    def hello_said() -> None:
        nonlocal waiting
        if waiting:
            waiting = False
            registry.waited(token)

    # Entered only once ``hello`` named the session's person: a stale tab of someone else, or a
    # socket that never says hello, takes no other socket's place.
    entries: list[_Open] = []
    entered = anyio.Event()

    def enter() -> None:
        hello_said()
        entries.append(registry.enter(token, person_id))
        entered.set()

    # Where the person reached the assistant: the Origin checked above.
    origin = f"{headers['origin'].rstrip('/')}/"
    sink = _Sink(websocket)
    code = CLOSE_NORMAL
    try:
        await websocket.accept()
        async with anyio.create_task_group() as group:

            async def until_replaced() -> None:
                nonlocal code
                await entered.wait()
                await entries[0].replaced.wait()
                code = CLOSE_REPLACED
                group.cancel_scope.cancel()

            async def until_signed_out() -> None:
                nonlocal code
                while True:
                    await anyio.sleep(SEEN_EVERY)
                    try:
                        still = await signed.person_id()
                    except Exception as error:
                        # Looked at again next time: a database that cannot be read ends nothing.
                        logger.error(
                            "A voice socket's session could not be read (%s)", type(error).__name__
                        )
                        continue
                    if still != person_id:
                        code = CLOSE_POLICY
                        group.cancel_scope.cancel()
                        return

            group.start_soon(until_replaced)
            group.start_soon(until_signed_out)
            try:
                code = await _Reading(
                    websocket, sessions, person_id, origin, signed, sink, enter
                ).run()
            except _Closing as closing:
                logger.info("A voice socket was closed: %s", closing)
                code = closing.code
            except Exception as error:
                # The kind only: a message may hold what the session was working on.
                logger.error("A voice socket failed (%s)", type(error).__name__)
                code = CLOSE_ERROR
            group.cancel_scope.cancel()
    finally:
        sink.shut()
        hello_said()
        for entry in entries:
            registry.leave(entry)
        with anyio.CancelScope(shield=True):
            await _close(websocket, code)


async def _close(websocket: WebSocket, code: int) -> None:
    if (
        websocket.client_state == WebSocketState.CONNECTED
        and websocket.application_state == WebSocketState.CONNECTED
    ):
        # A browser that dropped the socket first makes starlette raise WebSocketDisconnect.
        with suppress(WebSocketDisconnect, RuntimeError, OSError):
            await websocket.close(code)


def _room_in(times: deque[float]) -> bool:
    """Whether one more fits in the sliding minute of ``times``; if so, it is counted."""
    now = anyio.current_time()
    while times and now - times[0] >= 60.0:
        times.popleft()
    if len(times) >= UTTERANCES_PER_MINUTE:
        return False
    times.append(now)
    return True


class _Reading:
    """The frames of one accepted socket, read until it ends."""

    def __init__(
        self,
        websocket: WebSocket,
        sessions: VoiceSessionFactory,
        person_id: UUID,
        origin: str,
        signed: _Signed,
        sink: _Sink,
        enter: Callable[[], None],
    ) -> None:
        self._websocket = websocket
        self._enter = enter
        self._sessions = sessions
        self._person_id = person_id
        self._origin = origin
        self._signed = signed
        self._sink = sink
        self._session: VoiceSession | None = None
        self._sound: bytearray | None = None  # the moment of speech being gathered
        self._told = False  # the session was told the moment began
        self._gave = False  # something of the moment was handed on
        self._begun: deque[float] = deque()  # when the last moments began (told)
        self._handed: deque[float] = deque()  # when the last pieces were handed on

    async def run(self) -> int:
        """Reads until the page leaves (``CLOSE_NORMAL``); ``_Closing`` for anything else."""
        try:
            return await self._read()
        finally:
            self._sound = None
            if self._session is not None:
                with anyio.CancelScope(shield=True):
                    try:
                        await self._session.close()
                    except Exception as error:
                        logger.error(
                            "A voice session could not be closed (%s)", type(error).__name__
                        )

    async def _read(self) -> int:
        first: Message | None = None
        with anyio.move_on_after(HELLO_SECONDS):
            first = await self._websocket.receive()
        hello = None if first is None else self._message(first)
        if not isinstance(hello, Hello):
            raise _Closing(CLOSE_POLICY, "no hello")
        if hello.person != self._person_id:
            raise _Closing(CLOSE_ANOTHER_PERSON, "a tab of another person")
        self._enter()
        try:
            self._session = session = await self._sessions(
                person_id=self._person_id,
                microphone=hello.microphone,
                origin=self._origin,
                sink=self._sink,
                signed_in_at=await self._signed.signed_in_at(),
            )
        except Exception as error:
            logger.error("A voice session could not be opened (%s)", type(error).__name__)
            raise _Closing(CLOSE_ERROR, "no session") from None
        while True:
            message = await self._next()
            if message is None:
                # The page left its moment of speech open: it ends as if ``speech_end`` came.
                await self._end()
                continue
            if message["type"] == "websocket.disconnect":
                return CLOSE_NORMAL
            sound = message.get("bytes")
            if sound is not None:
                await self._gather(sound)
                continue
            match self._message(message):
                case SpeechStart():
                    if self._sound is not None:
                        await self._end()
                    self._sound = bytearray()
                    self._gave = False
                    self._told = _room_in(self._begun)
                    if self._told:
                        await session.speech_began()
                case SpeechEnd():
                    if self._sound is not None:
                        await self._end()
                case Played(reply=reply, whole=whole):
                    await session.played(reply, whole=whole)
                case Off():
                    await session.off()
                    return CLOSE_NORMAL
                case Hello():
                    raise _Closing(CLOSE_POLICY, "hello again")

    async def _next(self) -> Message | None:
        """The next frame; ``None`` when a moment of speech waits longer than
        ``UTTERANCE_IDLE_SECONDS`` for it."""
        if self._sound is None:
            return await self._websocket.receive()
        with anyio.move_on_after(UTTERANCE_IDLE_SECONDS):
            return await self._websocket.receive()
        return None

    def _message(self, message: Message) -> protocol.PageMessage:
        """The page's message of a text frame; ``_Closing`` for anything else."""
        if message["type"] == "websocket.disconnect":
            raise _Closing(CLOSE_NORMAL, "left")
        text = message.get("text")
        if not isinstance(text, str):
            raise _Closing(CLOSE_POLICY, "sound out of a moment of speech")
        try:
            return protocol.parse(text)
        except ProtocolError as error:
            raise _Closing(CLOSE_POLICY, str(error)) from None

    async def _gather(self, sound: object) -> None:
        if not isinstance(sound, bytes) or self._sound is None:
            raise _Closing(CLOSE_POLICY, "sound out of a moment of speech")
        if len(sound) > MAX_FRAME_BYTES or len(sound) % SAMPLE_BYTES:
            raise _Closing(CLOSE_POLICY, "bad frame of sound")
        room = MAX_UTTERANCE_BYTES - len(self._sound)
        self._sound += sound[:room]
        if len(sound) > room:
            # At the bound: what was gathered is handed on, and the moment goes on.
            piece, self._sound = self._sound, bytearray(sound[room:])
            await self._hand(piece)

    async def _end(self) -> None:
        """The moment of speech being gathered is over: its last piece is handed on, or the
        session is told it came to nothing."""
        piece, self._sound = self._sound, None
        assert self._session is not None and piece is not None
        await self._hand(piece)
        if self._told and not self._gave:
            await self._session.nothing_heard()

    async def _hand(self, piece: bytearray) -> None:
        """Hands ``piece`` on, when the moment was told, is long enough and the minute has room,
        and the person is still the session's."""
        assert self._session is not None
        if not self._told or len(piece) < MIN_UTTERANCE_BYTES or not _room_in(self._handed):
            return
        try:
            still = await self._signed.person_id()
        except Exception as error:
            # Not handed on: nobody can be sure it is still the person's page.
            logger.error("A voice socket's session could not be read (%s)", type(error).__name__)
            return
        if still != self._person_id:
            raise _Closing(CLOSE_POLICY, "signed out")
        self._gave = True
        await self._session.heard(bytes(piece))
