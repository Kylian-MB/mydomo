"""Server-sent events: the framing, and a response that relays a producer running elsewhere.

``text/event-stream`` is line based, and the text of an event can come from anywhere (the chat
sends what a model wrote): ``format_event`` writes it so that nothing in it can end the event or
begin another one. Every kind of line break in the data is turned into a plain LF and each line
gets its own ``data:`` field, so a reader joins them back with LF.

A producer writes to an ``EventSink``. A chat turn never runs in the response that a page reads:
it runs in a task of its own (``conversation.starts.Wakes``), so that a reader who leaves
(another tab, the page closed, the connection lost) does not stop it. It writes to a
``RelaySink``, which keeps each event, in order, for the connection that reads it, and drops them
once that connection is gone; ``SSEResponse`` is that connection. A turn that Domo starts
himself, which no page asked for, gives its events to whoever listens to the person
(``conversation.listeners.Fanout``): the page finds its reply by asking what is new.

``SSEResponse`` stops relaying as soon as the reader disconnects. It is not Starlette's
``StreamingResponse``: that one only notices a disconnect when the next chunk is written, which
for a model that is thinking can be a long time away, and it hands the cancellation to a
generator, which is awkward to finish properly.
"""

import asyncio
import re
from typing import Protocol

import anyio
from starlette.responses import Response
from starlette.types import Receive, Scope, Send

_LINE_BREAK = re.compile(r"\r\n|\r")
_EVENT_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")


class ClientGone(Exception):
    """The reader is no longer there: what was being written could not be delivered."""


def format_event(event: str, data: str) -> bytes:
    """One event, ready to be sent: ``event: <event>`` and the data, a line each, then a blank
    line. ``event`` is a plain name; ``data`` is any text (possibly empty)."""
    if _EVENT_NAME.fullmatch(event) is None:
        raise ValueError(f"not an event name: {event!r}")
    lines = _LINE_BREAK.sub("\n", data).split("\n")
    body = "".join(f"data: {line}\n" for line in lines)
    return f"event: {event}\n{body}\n".encode()


class EventSink(Protocol):
    """Where a producer writes its events: ``open`` once it is ready to speak, ``emit`` for each
    event. Either may raise ``ClientGone`` when nobody can receive them any more. ``reading``
    says whether someone may still read them: a chat turn that nobody reads leaves what a call
    would show only once on the person's card instead (``tools.context.ToolContext.no_reader``).
    """

    @property
    def reading(self) -> bool: ...

    async def open(self) -> None: ...

    async def emit(self, event: str, data: str) -> None: ...


class EventChannel:
    """The connection of a response: what is written here goes to the reader. The response starts
    with the first call to ``open`` or ``write`` (``open`` first, to give the reader its headers
    before there is anything to say)."""

    def __init__(self, send: Send) -> None:
        self._send = send
        self._opened = False

    async def open(self) -> None:
        if self._opened:
            return
        self._opened = True
        await self._deliver(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"text/event-stream; charset=utf-8"),
                    (b"cache-control", b"no-store"),
                    # An nginx-style proxy must not hold the events back to batch them.
                    (b"x-accel-buffering", b"no"),
                ],
            }
        )

    async def emit(self, event: str, data: str) -> None:
        """Send an event. Raises ``ClientGone`` when it cannot be delivered."""
        await self.write(format_event(event, data))

    async def write(self, chunk: bytes) -> None:
        """Send an event already framed (``format_event``). Raises ``ClientGone`` when it cannot
        be delivered."""
        await self.open()
        await self._deliver({"type": "http.response.body", "body": chunk, "more_body": True})

    async def finish(self) -> None:
        await self.open()
        await self._deliver({"type": "http.response.body", "body": b"", "more_body": False})

    async def _deliver(self, message: dict[str, object]) -> None:
        try:
            await self._send(message)
        except OSError as error:
            raise ClientGone from error


class RelaySink:
    """The ``EventSink`` of a producer that runs on its own (a chat turn, in a task of
    ``conversation.starts.Wakes``) while a connection may read it (``SSEResponse``).

    Each event it is given is kept, in order, until the connection has sent it; once the
    connection is gone, what it is given is dropped. It never raises ``ClientGone``: the producer
    goes on, whether anyone reads or not. ``close`` says that the producer is done, whatever the
    way: the connection then ends, once it has sent what is left. The events are framed as they
    are given (``format_event``), so that a name that is not one is the producer's mistake, raised
    to it."""

    def __init__(self) -> None:
        self._events: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._gone = False

    @property
    def reading(self) -> bool:
        """Whether the connection may still read what comes: true until it is gone."""
        return not self._gone

    async def open(self) -> None:
        return None

    async def emit(self, event: str, data: str) -> None:
        chunk = format_event(event, data)
        if not self._gone:
            self._events.put_nowait(chunk)

    def close(self) -> None:
        """The producer is done: nothing more comes."""
        if not self._gone:
            self._events.put_nowait(None)

    async def relay(self, channel: EventChannel) -> None:
        """Send to ``channel`` what the producer gives, in order, until it is done (``close``).
        Raises ``ClientGone`` when the reader left, and is cancelled when the response sees them
        leave: from then on, what the producer gives is dropped."""
        try:
            await channel.open()
            while (chunk := await self._events.get()) is not None:
                await channel.write(chunk)
        finally:
            self._gone = True
            while not self._events.empty():
                self._events.get_nowait()


class SSEResponse(Response):
    """A 200 ``text/event-stream`` response that relays what a producer running elsewhere gives
    its ``RelaySink``.

    It ends when the producer is done, or as soon as the reader disconnects: only the relay stops
    then, never the producer, whose later events are dropped.
    """

    def __init__(self, relay: RelaySink) -> None:
        super().__init__()
        self._relay = relay

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        channel = EventChannel(send)
        async with anyio.create_task_group() as group:

            async def watch_for_disconnect() -> None:
                while (await receive())["type"] != "http.disconnect":
                    pass
                group.cancel_scope.cancel()

            group.start_soon(watch_for_disconnect)
            try:
                await self._relay.relay(channel)
                await channel.finish()
            except ClientGone:
                pass
            finally:
                group.cancel_scope.cancel()
