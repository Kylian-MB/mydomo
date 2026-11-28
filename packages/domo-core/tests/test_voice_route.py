"""The page's voice socket, ``/chat/voice`` (spec 6.1, 6.2), and its wire messages.

The socket is driven in the test's own event loop by a small ASGI client (``Socket``): what the
application sends is read message by message, so a refusal is seen to come before any accept. The
session behind the socket (Task 14) is replaced by a recorder (``Recorder``).
"""

import asyncio
import json
import socket as socket_module
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager, contextmanager
from datetime import UTC, datetime
from typing import Any, cast

import httpx
import pytest
import uvicorn
import websockets
from chat_support import log_in_live
from domo_core.accounts.models import Account, Person, WebSession
from domo_core.accounts.sessions import SESSION_COOKIE
from domo_core.channels.voice import protocol, routes
from domo_core.channels.voice.listener import SinkClosed, VoiceSink
from domo_core.channels.voice.protocol import (
    MAX_FRAME_BYTES,
    MAX_TEXT_BYTES,
    MAX_UTTERANCE_BYTES,
    MIN_UTTERANCE_BYTES,
    Hello,
    Off,
    PageMessage,
    Played,
    ProtocolError,
    SpeechEnd,
    SpeechStart,
)
from domo_core.channels.voice.routes import VOICE_PATH, get_voice_sessions
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.types import Message
from web_support import log_in

WAIT = 10.0
ORIGIN = "https://testserver"
# One second of sound, as the page sends it (16 000 Hz, 16-bit, mono).
SECOND = 32_000


# --- the wire messages ----------------------------------------------------------------------


def test_every_message_round_trips() -> None:
    messages: list[PageMessage] = [
        Hello(microphone="kitchen.1:left-A_b", person=PERSON),
        SpeechStart(),
        SpeechEnd(),
        Played(reply=uuid.uuid4(), whole=True),
        Played(reply=uuid.uuid4(), whole=False),
        Off(),
    ]
    for message in messages:
        frame = protocol.dump(message)
        assert len(frame.encode()) <= MAX_TEXT_BYTES
        assert protocol.parse(frame) == message


@pytest.mark.parametrize(
    "frame",
    [
        '{"type": "shout"}',
        '{"type": "say"}',  # a message of the core's, never the page's
        '{"kind": "hello"}',
        '{"type": 1}',
        '["hello"]',
        "hello",
        "",
    ],
)
def test_unknown_types_are_errors(frame: str) -> None:
    with pytest.raises(ProtocolError):
        protocol.parse(frame)


SECRET_LOOKING = "x" * 30
PERSON = uuid.UUID(int=9)


@pytest.mark.parametrize(
    "frame",
    [
        '{"type": "hello"}',
        '{"type": "hello", "microphone": ""}',
        '{"type": "hello", "microphone": "' + "a" * 81 + '"}',
        '{"type": "hello", "microphone": "a b"}',
        '{"type": "hello", "microphone": "../x"}',
        '{"type": "hello", "microphone": 3}',
        '{"type": "hello", "microphone": "a", "extra": 1}',
        '{"type": "hello", "microphone": "a"}',  # ``person`` is always there
        '{"type": "hello", "microphone": "a", "person": "' + SECRET_LOOKING + '"}',
        '{"type": "hello", "microphone": "a", "person": 9}',
        '{"type": "speech_start", "at": 1}',
        '{"type": "played", "whole": true}',  # ``reply`` is always there
        '{"type": "played", "reply": "' + SECRET_LOOKING + '", "whole": true}',
        '{"type": "played", "reply": "' + str(uuid.UUID(int=1)) + '", "whole": 1}',
        '{"type": "off", "type2": "x"}',
        '{"type": "hello", "microphone": "' + "a" * 2000 + '"}',
    ],
)
def test_badly_formed_messages_are_errors_that_do_not_repeat_the_frame(frame: str) -> None:
    with pytest.raises(ProtocolError) as raised:
        protocol.parse(frame)
    assert SECRET_LOOKING not in str(raised.value)
    assert "aaaa" not in str(raised.value)


def test_a_text_frame_over_its_bound_is_an_error() -> None:
    padded = '{"type": "off"' + " " * MAX_TEXT_BYTES + "}"
    with pytest.raises(ProtocolError):
        protocol.parse(padded)


def test_the_core_messages_are_one_definition() -> None:
    reply = uuid.UUID(int=5)
    assert protocol.say(reply, 2) == {
        "type": "say",
        "reply": str(reply),
        "index": 2,
        "rate": protocol.SAY_RATE,
    }
    assert protocol.SAY_RATE == 24_000
    assert protocol.say_end(reply, 2) == {"type": "say_end", "reply": str(reply), "index": 2}
    assert protocol.state("ready") == {"type": "state", "hearing": "ready"}
    assert protocol.listening(on=False) == {"type": "listening", "on": False}
    assert protocol.stop() == {"type": "stop"}
    assert protocol.resume() == {"type": "resume"}
    assert protocol.duck() == {"type": "duck"}
    # Each one is a message of its own: what one sender does with it reaches no other.
    assert protocol.stop() is not protocol.stop()
    assert protocol.speech_unavailable("preparing") == {
        "type": "speech_unavailable",
        "reason": "preparing",
    }


def test_the_bounds() -> None:
    assert MAX_FRAME_BYTES == 64 * 1024
    assert MAX_UTTERANCE_BYTES == 30 * SECOND == 960_000
    assert int(0.3 * SECOND) == MIN_UTTERANCE_BYTES


# --- the socket -----------------------------------------------------------------------------


class Recorder:
    """Stands in for the voice session of Task 14: it records what the route hands it."""

    def __init__(self) -> None:
        self.made: list[tuple[uuid.UUID, str]] = []
        self.origins: list[str] = []
        self.signed_in: list[datetime | None] = []
        self.calls: list[tuple[Any, ...]] = []
        self.sinks: list[VoiceSink] = []

    async def __call__(
        self,
        *,
        person_id: uuid.UUID,
        microphone: str,
        origin: str,
        sink: VoiceSink,
        signed_in_at: datetime | None,
    ) -> "Recorder":
        self.made.append((person_id, microphone))
        self.origins.append(origin)
        self.signed_in.append(signed_in_at)
        self.sinks.append(sink)
        return self

    async def speech_began(self) -> None:
        self.calls.append(("began",))

    async def heard(self, sound: bytes) -> None:
        self.calls.append(("heard", len(sound)))

    async def nothing_heard(self) -> None:
        self.calls.append(("nothing",))

    async def played(self, reply: uuid.UUID, *, whole: bool) -> None:
        self.calls.append(("played", reply, whole))

    async def off(self) -> None:
        self.calls.append(("off",))

    async def close(self) -> None:
        self.calls.append(("close",))

    def of(self, kind: str) -> list[tuple[Any, ...]]:
        return [call for call in self.calls if call[0] == kind]

    async def until(self, check: Callable[[], bool]) -> None:
        async with asyncio.timeout(WAIT):
            while not check():
                await asyncio.sleep(0.01)


class Socket:
    """A WebSocket client of ``app`` in the test's loop, at the ASGI level."""

    def __init__(self, app: FastAPI, headers: list[tuple[bytes, bytes]]) -> None:
        self._to_app: asyncio.Queue[Message] = asyncio.Queue()
        self._from_app: asyncio.Queue[Message] = asyncio.Queue()
        scope = {
            "type": "websocket",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "scheme": "wss",
            "server": ("testserver", 443),
            "client": ("127.0.0.1", 123),
            "root_path": "",
            "path": VOICE_PATH,
            "raw_path": VOICE_PATH.encode(),
            "query_string": b"",
            "headers": headers,
            "subprotocols": [],
            "state": {},
        }
        self.task = asyncio.create_task(app(scope, self._to_app.get, self._from_app.put))
        self._to_app.put_nowait({"type": "websocket.connect"})

    async def next(self) -> Message:
        async with asyncio.timeout(WAIT):
            return await self._from_app.get()

    async def accepted(self) -> bool:
        """Whether the application accepts the socket (``False``: closed before any accept)."""
        first = await self.next()
        if first["type"] == "websocket.accept":
            return True
        assert first["type"] == "websocket.close", first
        self.refused_with = first.get("code", 1000)
        return False

    def send_text(self, text: str) -> None:
        self._to_app.put_nowait({"type": "websocket.receive", "text": text})

    def send(self, message: PageMessage) -> None:
        self.send_text(protocol.dump(message))

    def send_bytes(self, data: bytes) -> None:
        self._to_app.put_nowait({"type": "websocket.receive", "bytes": data})

    def leave(self) -> None:
        self._to_app.put_nowait({"type": "websocket.disconnect", "code": 1001})

    async def closed_with(self) -> int:
        """The code the application closes the socket with, skipping what it sent before."""
        while True:
            message = await self.next()
            if message["type"] == "websocket.close":
                return cast(int, message.get("code", 1000))

    async def sent_json(self) -> str:
        message = await self.next()
        assert message["type"] == "websocket.send", message
        return cast(str, message["text"])

    async def finish(self) -> None:
        self.leave()
        async with asyncio.timeout(WAIT):
            await self.task


def _serialised(
    factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
) -> Callable[[], AbstractAsyncContextManager[AsyncSession]]:
    """The application's sessions one at a time: the route's look at the session and a request of
    the test share one database session, which takes one operation at a time."""
    lock = asyncio.Lock()

    @asynccontextmanager
    async def serialised() -> AsyncIterator[AsyncSession]:
        async with lock, factory() as session:
            yield session

    return serialised


@pytest.fixture
def recorder(app: FastAPI) -> Recorder:
    recorder = Recorder()
    app.dependency_overrides[get_voice_sessions] = lambda: recorder
    app.state.session_factory = _serialised(app.state.session_factory)
    return recorder


@pytest.fixture
async def cookie(client: httpx.AsyncClient, owner_account: Account) -> str:
    await log_in(client)
    return cast(str, client.cookies[SESSION_COOKIE])


def headers(
    *, cookie: str | None = None, origin: str | None = ORIGIN, **others: str
) -> list[tuple[bytes, bytes]]:
    found = [(b"host", b"testserver")]
    if origin is not None:
        found.append((b"origin", origin.encode()))
    if cookie is not None:
        found.append((b"cookie", f"{SESSION_COOKIE}={cookie}".encode()))
    found.extend((name.encode(), value.encode()) for name, value in others.items())
    return found


@pytest.fixture
def connect(
    app: FastAPI, cookie: str, recorder: Recorder, owner_account: Account
) -> Callable[..., Awaitable[Socket]]:
    """Opens the socket as the signed-in page does, and says hello."""

    async def _connect(microphone: str = "desk-1", **overrides: Any) -> Socket:
        socket = Socket(app, headers(cookie=cookie, **overrides))
        assert await socket.accepted()
        socket.send(Hello(microphone=microphone, person=owner_account.person_id))
        count = len(recorder.made)
        await recorder.until(lambda: len(recorder.made) > count)
        return socket

    return _connect


async def test_another_sites_page_is_refused_before_accept(
    app: FastAPI, cookie: str, recorder: Recorder
) -> None:
    socket = Socket(app, headers(cookie=cookie, origin="https://elsewhere.example"))
    assert not await socket.accepted()
    assert socket.refused_with == 1008
    assert recorder.made == []


async def test_no_origin_is_refused(app: FastAPI, cookie: str, recorder: Recorder) -> None:
    # No fallback on Referer: a socket names its Origin, or it is not opened.
    socket = Socket(app, headers(cookie=cookie, origin=None, referer=f"{ORIGIN}/chat"))
    assert not await socket.accepted()
    assert socket.refused_with == 1008
    twice = headers(cookie=cookie) + [(b"origin", ORIGIN.encode())]
    socket = Socket(app, twice)
    assert not await socket.accepted()
    assert socket.refused_with == 1008
    # The Origin of a page with no origin of its own (a sandboxed frame, a file).
    socket = Socket(app, headers(cookie=cookie, origin="null"))
    assert not await socket.accepted()
    assert socket.refused_with == 1008
    assert recorder.made == []


async def test_no_session_is_refused(app: FastAPI, recorder: Recorder) -> None:
    for cookie in (None, "not-a-session"):
        socket = Socket(app, headers(cookie=cookie))
        assert not await socket.accepted()
        assert socket.refused_with == 1008
    assert recorder.made == []


async def test_a_removed_person_is_refused(
    app: FastAPI, cookie: str, recorder: Recorder, session: AsyncSession, owner_account: Account
) -> None:
    person = await session.get(Person, owner_account.person_id)
    assert person is not None
    person.disabled_at = datetime.now(UTC)
    await session.commit()
    socket = Socket(app, headers(cookie=cookie))
    assert not await socket.accepted()
    assert socket.refused_with == 1008
    assert recorder.made == []


async def test_a_tab_of_another_person_is_told_so_and_closed(
    app: FastAPI, cookie: str, recorder: Recorder
) -> None:
    # A tab rendered for Dana, after Alex signed in in another tab: the cookie is Alex's now.
    socket = Socket(app, headers(cookie=cookie))
    assert await socket.accepted()
    socket.send(Hello(microphone="desk-1", person=uuid.uuid4()))
    assert await socket.closed_with() == protocol.CLOSE_ANOTHER_PERSON
    assert recorder.made == []


async def test_hello_then_an_utterance_reaches_the_session(
    connect: Callable[..., Awaitable[Socket]], recorder: Recorder, owner_account: Account
) -> None:
    socket = await connect("kitchen-1")
    assert recorder.made == [(owner_account.person_id, "kitchen-1")]
    socket.send(SpeechStart())
    socket.send_bytes(b"\x01\x00" * (SECOND // 4))
    socket.send_bytes(b"\x01\x00" * (SECOND // 4))
    socket.send(SpeechEnd())
    await recorder.until(lambda: bool(recorder.of("heard")))
    assert recorder.calls == [("began",), ("heard", SECOND)]
    # The session speaks to the page through the sink.
    sink = recorder.sinks[0]
    assert sink.open
    await sink.send_json(protocol.state("ready"))
    assert json.loads(await socket.sent_json()) == {"type": "state", "hearing": "ready"}
    reply = uuid.uuid4()
    socket.send(Played(reply=reply, whole=False))
    await recorder.until(lambda: bool(recorder.of("played")))
    assert recorder.of("played") == [("played", reply, False)]
    await socket.finish()
    assert recorder.calls[-1] == ("close",)
    assert not sink.open
    with pytest.raises(SinkClosed):
        await sink.send_json(protocol.stop())


async def test_an_oversized_frame_closes_the_socket(
    connect: Callable[..., Awaitable[Socket]], recorder: Recorder
) -> None:
    socket = await connect()
    socket.send(SpeechStart())
    socket.send_bytes(b"\x00" * (MAX_FRAME_BYTES + 2))
    assert await socket.closed_with() == 1008
    assert recorder.of("heard") == []
    assert recorder.calls[-1] == ("close",)

    socket = await connect()
    socket.send_text('{"type": "off"' + " " * MAX_TEXT_BYTES + "}")
    assert await socket.closed_with() == 1008
    assert recorder.of("off") == []


async def test_sound_outside_an_utterance_closes_the_socket(
    connect: Callable[..., Awaitable[Socket]], recorder: Recorder
) -> None:
    socket = await connect()
    socket.send_bytes(b"\x00" * SECOND)
    assert await socket.closed_with() == 1008
    assert recorder.of("heard") == []


async def test_sound_before_hello_closes_the_socket(
    app: FastAPI, cookie: str, recorder: Recorder
) -> None:
    socket = Socket(app, headers(cookie=cookie))
    assert await socket.accepted()
    socket.send(SpeechStart())
    assert await socket.closed_with() == 1008
    assert recorder.made == []


async def test_an_unknown_message_closes_the_socket(
    connect: Callable[..., Awaitable[Socket]], recorder: Recorder
) -> None:
    socket = await connect()
    socket.send_text('{"type": "shout"}')
    assert await socket.closed_with() == 1008
    socket = await connect()
    socket.send(Hello(microphone="again", person=uuid.uuid4()))
    assert await socket.closed_with() == 1008


async def test_an_odd_frame_of_sound_closes_the_socket(
    connect: Callable[..., Awaitable[Socket]], recorder: Recorder
) -> None:
    # 16-bit samples: a frame is a whole number of them.
    socket = await connect()
    socket.send(SpeechStart())
    socket.send_bytes(b"\x00" * 3)
    assert await socket.closed_with() == 1008


async def test_a_too_long_utterance_is_cut_in_two(
    connect: Callable[..., Awaitable[Socket]], recorder: Recorder
) -> None:
    socket = await connect()
    socket.send(SpeechStart())
    frame = b"\x02\x00" * (MAX_FRAME_BYTES // 2 - 1000)
    sent = 0
    while sent < MAX_UTTERANCE_BYTES + SECOND:
        socket.send_bytes(frame)
        sent += len(frame)
    socket.send(SpeechEnd())
    await recorder.until(lambda: len(recorder.of("heard")) == 2)
    assert recorder.of("heard") == [
        ("heard", MAX_UTTERANCE_BYTES),
        ("heard", sent - MAX_UTTERANCE_BYTES),
    ]
    assert recorder.of("began") == [("began",)]
    await socket.finish()


async def test_a_too_short_utterance_is_dropped(
    connect: Callable[..., Awaitable[Socket]], recorder: Recorder
) -> None:
    socket = await connect()
    socket.send(SpeechStart())
    socket.send_bytes(b"\x01\x00" * (MIN_UTTERANCE_BYTES // 2 - 1))
    socket.send(SpeechEnd())
    await recorder.until(lambda: bool(recorder.of("nothing")))
    assert recorder.of("heard") == []
    await socket.finish()


async def test_an_utterance_the_page_leaves_open_ends_on_its_own(
    connect: Callable[..., Awaitable[Socket]],
    recorder: Recorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(routes, "UTTERANCE_IDLE_SECONDS", 0.1)
    socket = await connect()
    socket.send(SpeechStart())
    socket.send_bytes(b"\x01\x00" * (SECOND // 2))
    await recorder.until(lambda: bool(recorder.of("heard")))
    assert recorder.of("heard") == [("heard", SECOND)]
    # A late ``speech_end`` is nothing.
    socket.send(SpeechEnd())
    socket.send(SpeechStart())
    socket.send(SpeechEnd())
    await recorder.until(lambda: bool(recorder.of("nothing")))
    await socket.finish()


async def _all_read(socket: Socket, recorder: Recorder) -> None:
    """Waits until the route read everything sent before (a ``played`` marks the end)."""
    marker = uuid.uuid4()
    socket.send(Played(reply=marker, whole=True))
    await recorder.until(lambda: ("played", marker, True) in recorder.calls)


async def test_a_flood_of_utterances_is_bounded(
    connect: Callable[..., Awaitable[Socket]], recorder: Recorder
) -> None:
    socket = await connect()
    for _ in range(routes.UTTERANCES_PER_MINUTE + 5):
        socket.send(SpeechStart())
        socket.send_bytes(b"\x01\x00" * (SECOND // 2))
        socket.send(SpeechEnd())
    await _all_read(socket, recorder)
    assert len(recorder.of("began")) == routes.UTTERANCES_PER_MINUTE
    assert len(recorder.of("heard")) == routes.UTTERANCES_PER_MINUTE
    assert recorder.of("nothing") == []
    await socket.finish()


async def test_a_flood_of_empty_moments_is_bounded(
    connect: Callable[..., Awaitable[Socket]], recorder: Recorder
) -> None:
    socket = await connect()
    for _ in range(routes.UTTERANCES_PER_MINUTE * 3):
        socket.send(SpeechStart())
        socket.send(SpeechEnd())
    await _all_read(socket, recorder)
    assert len(recorder.of("began")) == routes.UTTERANCES_PER_MINUTE
    assert len(recorder.of("nothing")) == routes.UTTERANCES_PER_MINUTE
    await socket.finish()


async def test_one_endless_moment_is_bounded_too(
    connect: Callable[..., Awaitable[Socket]],
    recorder: Recorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(routes, "UTTERANCES_PER_MINUTE", 2)
    monkeypatch.setattr(routes, "MAX_UTTERANCE_BYTES", SECOND)
    socket = await connect()
    socket.send(SpeechStart())
    for _ in range(12):
        socket.send_bytes(b"\x01\x00" * (SECOND // 4))
    socket.send(SpeechEnd())
    await _all_read(socket, recorder)
    # One moment, cut at the size bound: two hand-offs for one beginning, then nothing more.
    assert recorder.of("began") == [("began",)]
    assert recorder.of("heard") == [("heard", SECOND), ("heard", SECOND)]
    assert recorder.of("nothing") == []
    await socket.finish()


async def test_no_hello_in_time_closes_the_socket(
    app: FastAPI, cookie: str, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(routes, "HELLO_SECONDS", 0.1)
    socket = Socket(app, headers(cookie=cookie))
    assert await socket.accepted()
    assert await socket.closed_with() == 1008
    assert recorder.made == []


async def test_a_bad_microphone_name_is_refused(
    app: FastAPI, cookie: str, recorder: Recorder
) -> None:
    socket = Socket(app, headers(cookie=cookie))
    assert await socket.accepted()
    socket.send_text('{"type": "hello", "microphone": "../../etc"}')
    assert await socket.closed_with() == 1008
    assert recorder.made == []


async def test_a_session_that_fails_closes_the_socket_and_nothing_else(
    connect: Callable[..., Awaitable[Socket]], recorder: Recorder
) -> None:
    async def broken(sound: bytes) -> None:
        raise RuntimeError("a failure")

    socket = await connect()
    recorder.heard = broken  # type: ignore[method-assign]
    socket.send(SpeechStart())
    socket.send_bytes(b"\x01\x00" * (SECOND // 2))
    socket.send(SpeechEnd())
    assert await socket.closed_with() == protocol.CLOSE_ERROR
    await socket.task
    assert recorder.calls[-1] == ("close",)


async def test_off_tells_the_session_and_closes(
    connect: Callable[..., Awaitable[Socket]], recorder: Recorder
) -> None:
    socket = await connect()
    socket.send(Off())
    assert await socket.closed_with() == 1000
    assert recorder.calls[-2:] == [("off",), ("close",)]


async def test_a_second_connection_of_the_same_session_closes_the_first(
    connect: Callable[..., Awaitable[Socket]], recorder: Recorder
) -> None:
    first = await connect("desk-1")
    second = await connect("desk-1")
    assert await first.closed_with() == protocol.CLOSE_REPLACED
    assert not recorder.sinks[0].open
    assert recorder.sinks[1].open
    second.send(SpeechStart())
    second.send_bytes(b"\x01\x00" * (SECOND // 2))
    second.send(SpeechEnd())
    await recorder.until(lambda: bool(recorder.of("heard")))
    await second.finish()


async def _still_hears(socket: Socket, recorder: Recorder) -> None:
    """``socket`` is open and its moments still reach the session."""
    heard = len(recorder.of("heard"))
    socket.send(SpeechStart())
    socket.send_bytes(b"\x01\x00" * (SECOND // 2))
    socket.send(SpeechEnd())
    await recorder.until(lambda: len(recorder.of("heard")) > heard)
    assert not socket.task.done()


async def test_a_tab_of_another_person_does_not_evict_the_real_socket(
    app: FastAPI, cookie: str, connect: Callable[..., Awaitable[Socket]], recorder: Recorder
) -> None:
    real = await connect()
    stale = Socket(app, headers(cookie=cookie))
    assert await stale.accepted()
    stale.send(Hello(microphone="desk-1", person=uuid.uuid4()))
    assert await stale.closed_with() == protocol.CLOSE_ANOTHER_PERSON
    await _still_hears(real, recorder)
    assert recorder.sinks[0].open
    await real.finish()


async def test_a_socket_that_never_says_hello_does_not_evict_the_real_one(
    app: FastAPI,
    cookie: str,
    connect: Callable[..., Awaitable[Socket]],
    recorder: Recorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = await connect()
    monkeypatch.setattr(routes, "HELLO_SECONDS", 0.2)
    silent = Socket(app, headers(cookie=cookie))
    assert await silent.accepted()
    assert await silent.closed_with() == 1008
    await _still_hears(real, recorder)
    assert recorder.sinks[0].open
    await real.finish()


async def test_one_person_has_a_bounded_number_of_sockets(
    app: FastAPI,
    client_factory: Callable[..., httpx.AsyncClient],
    owner_account: Account,
    recorder: Recorder,
) -> None:
    sockets: list[Socket] = []
    for _ in range(routes.SOCKETS_PER_PERSON + 1):
        async with client_factory(app) as browser:
            await log_in(browser)
            cookie = cast(str, browser.cookies[SESSION_COOKIE])
        socket = Socket(app, headers(cookie=cookie))
        assert await socket.accepted()
        socket.send(Hello(microphone="desk-1", person=owner_account.person_id))
        sockets.append(socket)
        await recorder.until(lambda: len(recorder.made) == len(sockets))
    # The oldest gives way to the newest.
    assert await sockets[0].closed_with() == protocol.CLOSE_REPLACED
    for socket in sockets[1:]:
        assert not socket.task.done()
        await socket.finish()


async def test_the_session_is_given_the_address_the_page_was_reached_at(
    connect: Callable[..., Awaitable[Socket]], recorder: Recorder
) -> None:
    socket = await connect()
    # The Origin the route checked: where the person reached the assistant (links of a turn).
    assert recorder.origins == [f"{ORIGIN}/"]
    await socket.finish()


async def test_the_session_is_given_when_the_person_signed_in(
    connect: Callable[..., Awaitable[Socket]],
    recorder: Recorder,
    app: FastAPI,
    owner_account: Account,
) -> None:
    socket = await connect()
    async with app.state.session_factory() as reader:
        opened = await reader.scalar(
            select(WebSession.created_at).where(WebSession.account_id == owner_account.id)
        )
    # The date of the page session's clue: when the person signed in, not when the socket opened.
    assert opened is not None
    assert recorder.signed_in == [opened]
    await socket.finish()


async def test_a_cookie_holds_a_bounded_number_of_sockets_waiting_for_hello(
    app: FastAPI, cookie: str, recorder: Recorder, owner_account: Account
) -> None:
    waiting: list[Socket] = []
    for _ in range(routes.PENDING_PER_COOKIE):
        socket = Socket(app, headers(cookie=cookie))
        assert await socket.accepted()
        waiting.append(socket)
    # One more, before any of them said hello: refused.
    over = Socket(app, headers(cookie=cookie))
    assert not await over.accepted()
    assert over.refused_with == protocol.CLOSE_POLICY
    # Once one has said hello, a place is free again.
    waiting[0].send(Hello(microphone="desk-1", person=owner_account.person_id))
    await recorder.until(lambda: len(recorder.made) == 1)
    again = Socket(app, headers(cookie=cookie))
    assert await again.accepted()
    for socket in [*waiting, again]:
        await socket.finish()


async def test_signed_out_closes_the_socket(
    connect: Callable[..., Awaitable[Socket]],
    client: httpx.AsyncClient,
    recorder: Recorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(routes, "SEEN_EVERY", 0.05)
    socket = await connect()
    await asyncio.sleep(0.2)
    assert not socket.task.done()
    response = await client.post("/logout")
    assert response.status_code == 303
    assert await socket.closed_with() == 1008
    await recorder.until(lambda: bool(recorder.of("close")))


async def test_an_utterance_after_signing_out_is_not_handed_on(
    connect: Callable[..., Awaitable[Socket]], client: httpx.AsyncClient, recorder: Recorder
) -> None:
    # Long before the session is looked at again on its own (``SEEN_EVERY``).
    socket = await connect()
    response = await client.post("/logout")
    assert response.status_code == 303
    socket.send(SpeechStart())
    socket.send_bytes(b"\x01\x00" * (SECOND // 2))
    socket.send(SpeechEnd())
    assert await socket.closed_with() == 1008
    assert recorder.of("heard") == []


async def test_no_voice_session_yet_is_said_by_the_close_code(
    app: FastAPI, cookie: str, recorder: Recorder
) -> None:
    # Accepted first: a browser reads no code from a socket closed before it is accepted.
    app.dependency_overrides[get_voice_sessions] = lambda: None
    socket = Socket(app, headers(cookie=cookie))
    assert await socket.accepted()
    assert await socket.closed_with() == protocol.CLOSE_NOT_YET


class _Server(uvicorn.Server):
    """A uvicorn server that leaves the process signals (and so the test runner's) alone."""

    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


@asynccontextmanager
async def _served(app: FastAPI) -> AsyncIterator[str]:
    """``app`` served by uvicorn on the loopback, with the WebSocket support uvicorn picks in
    production (``ws="auto"``: the ``websockets`` library)."""
    with socket_module.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        config = uvicorn.Config(app, log_config=None, lifespan="off", ws="auto", http="h11")
        server = _Server(config)
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            async with asyncio.timeout(WAIT):
                while not server.started:
                    await asyncio.sleep(0.01)
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
            await task


async def test_a_real_server_opens_the_socket_and_refuses_another_site(
    app: FastAPI, owner_account: Account, recorder: Recorder
) -> None:
    async with (
        _served(app) as base_url,
        httpx.AsyncClient(base_url=base_url, headers={"origin": base_url}) as browser,
    ):
        await log_in_live(browser)
        cookie = f"{SESSION_COOKIE}={browser.cookies[SESSION_COOKIE]}"
        address = base_url.replace("http://", "ws://") + VOICE_PATH
        with pytest.raises(websockets.InvalidStatus) as refused:
            async with websockets.connect(
                address,
                origin=cast(Any, "http://elsewhere.example"),
                additional_headers={"cookie": cookie},
            ):
                pass
        assert refused.value.response.status_code == 403
        assert recorder.made == []
        async with websockets.connect(
            address, origin=cast(Any, base_url), additional_headers={"cookie": cookie}
        ) as page:
            await page.send(
                protocol.dump(Hello(microphone="desk-1", person=owner_account.person_id))
            )
            await recorder.until(lambda: bool(recorder.made))
            await page.send(protocol.dump(SpeechStart()))
            await page.send(b"\x01\x00" * (SECOND // 2))
            await page.send(protocol.dump(SpeechEnd()))
            await recorder.until(lambda: bool(recorder.of("heard")))
            await recorder.sinks[0].send_bytes(b"\x00\x01")
            assert await page.recv() == b"\x00\x01"
        await recorder.until(lambda: bool(recorder.of("close")))


async def test_closing_a_socket_the_browser_already_dropped_raises_nothing() -> None:
    # Seen in the stack: a page closed while its socket was open; starlette's close then raises
    # WebSocketDisconnect, and the route logged a traceback of its own end.
    from starlette.websockets import WebSocketDisconnect, WebSocketState

    class Dropped:
        client_state = WebSocketState.CONNECTED
        application_state = WebSocketState.CONNECTED

        async def close(self, code: int) -> None:
            raise WebSocketDisconnect(code=1006)

    await routes._close(cast(Any, Dropped()), protocol.CLOSE_NORMAL)  # pyright: ignore[reportPrivateUsage]
