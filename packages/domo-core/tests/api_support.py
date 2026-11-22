"""The API pilot: a device of a person of the household, talking with Domo through the channel
of the API (``/api/channel``) on the pilots' live server (``chat_support.ChatServer``).

A device is entrusted as the program on it would be: a code is begun for the person (as the tool
``device_entrust`` does) and the pilot trades it for the device's secret (``POST /pair``). From
then on it speaks with that secret alone (``Authorization: Bearer``): no cookie, no ``Origin``.
``say`` posts a message and reads its stream to the end; ``listen`` keeps the stream of what
Domo says on his own open, and reads it in the background. After each answer that is not a
stream, the pilot waits for the wakes of Domo it started, as the browsers of the pilots do.
"""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
from chat_support import (
    WAIT_SECONDS,
    ChatServer,
    Member,
    SseParser,
    ToolCallingModel,
    actions_of,
    get_message,
)
from domo_core.devices.service import begin_pairing
from domo_core.llm.types import ChatModel, ToolCall
from fakes import JUDGE_DEFAULT

API = "/api/channel"


@dataclass(frozen=True)
class ApiEvent:
    """One event of a stream of the API: its name and its data, read as JSON."""

    name: str
    data: dict[str, Any]


def events_of(parser: SseParser, text: str) -> list[ApiEvent]:
    return [ApiEvent(event.event, json.loads(event.data)) for event in parser.feed(text)]


@dataclass(frozen=True)
class ApiReply:
    """What a turn gave on the API: the events of its stream, and the reply as stored."""

    reply_id: uuid.UUID
    events: list[ApiEvent]
    text: str
    status: str
    actions: list[dict[str, str]]

    @property
    def said(self) -> str:
        return "".join(event.data["text"] for event in self.events if event.name == "words")

    @property
    def acted(self) -> list[dict[str, Any]]:
        return [event.data for event in self.events if event.name == "acted"]

    @property
    def request_id(self) -> uuid.UUID | None:
        """The request the last card that waits names, if any."""
        found = [a["request"]["id"] for a in self.acted if a.get("request") is not None]
        return uuid.UUID(found[-1]) if found else None


async def pairing_code(server: ChatServer, member: Member) -> str:
    """A code for ``member`` to entrust a device with, as ``device_entrust`` makes it
    (committed)."""
    async with server.session_factory() as writer:
        code = await begin_pairing(writer, member.person.id, server.clock())
        await writer.commit()
    return code


def plain_client(server: ChatServer) -> httpx.AsyncClient:
    """A client of the server that is no browser: no ``Origin``, no cookie."""
    return httpx.AsyncClient(base_url=server.base_url, timeout=httpx.Timeout(WAIT_SECONDS))


@dataclass
class Listening:
    """The stream of what Domo says on his own, read in the background: ``events`` so far,
    ``ended`` once the server closed it."""

    events: list[ApiEvent] = field(default_factory=lambda: [])
    status: int | None = None
    ended: asyncio.Event = field(default_factory=asyncio.Event)
    _new: asyncio.Event = field(default_factory=asyncio.Event)

    def heard(self, event: ApiEvent) -> None:
        self.events.append(event)
        self._new.set()

    async def until(self, name: str, count: int = 1) -> list[ApiEvent]:
        """Wait until ``count`` events named ``name`` came; the events so far."""
        async with asyncio.timeout(WAIT_SECONDS):
            while sum(1 for event in self.events if event.name == name) < count:
                self._new.clear()
                await self._new.wait()
        return list(self.events)

    async def answered(self) -> int:
        """Wait until the server answered the stream: its status."""
        async with asyncio.timeout(WAIT_SECONDS):
            while self.status is None:
                await asyncio.sleep(0.01)
        return self.status

    async def closed(self) -> None:
        """Wait until the server closed the stream."""
        async with asyncio.timeout(WAIT_SECONDS):
            await self.ended.wait()


class ApiPilot:
    """A device of ``member``, entrusted to Domo, on the pilots' live server (see the module
    docstring). ``secret`` is its secret, ``device_id`` its id, ``client`` what it speaks with."""

    def __init__(
        self,
        server: ChatServer,
        member: Member,
        client: httpx.AsyncClient,
        secret: str,
        device_id: uuid.UUID,
    ) -> None:
        self.server = server
        self.member = member
        self.client = client
        self.secret = secret
        self.device_id = device_id
        self._calls_made = 0

    @classmethod
    @asynccontextmanager
    async def paired(
        cls, server: ChatServer, member: Member, *, name: str = "Laptop"
    ) -> AsyncIterator["ApiPilot"]:
        """A device of ``member`` called ``name``, paired with a code of theirs."""
        code = await pairing_code(server, member)
        async with plain_client(server) as client:
            response = await client.post(f"{API}/pair", json={"code": code, "name": name})
            assert response.status_code == 200, response.text
            body = response.json()
            client.headers["authorization"] = f"Bearer {body['secret']}"

            async def after_each_answer(answer: httpx.Response) -> None:
                if answer.headers.get("content-type", "").startswith("text/event-stream"):
                    return
                await server.wait_for_wakes()

            client.event_hooks["response"].append(after_each_answer)
            yield cls(server, member, client, body["secret"], uuid.UUID(body["device_id"]))

    def use_model[M: ChatModel](self, model: M) -> M:
        """The server asks ``model`` from now on."""
        self.server.model = model
        return model

    async def post_message(self, text: str) -> uuid.UUID:
        """Post ``text``; the id of the reply that will answer it."""
        response = await self.client.post(f"{API}/messages", json={"text": text})
        assert response.status_code == 200, response.text
        return uuid.UUID(response.json()["reply_id"])

    async def read_stream(
        self, reply_id: uuid.UUID, *, stop_after: str | None = None
    ) -> list[ApiEvent]:
        """The events of the stream of ``reply_id``: to its end, or, with ``stop_after``, until
        the first event of that name (the device then goes away)."""
        parser = SseParser()
        events: list[ApiEvent] = []
        async with asyncio.timeout(WAIT_SECONDS):
            async with self.client.stream("GET", f"{API}/stream/{reply_id}") as response:
                assert response.status_code == 200, response.status_code
                assert response.headers["content-type"].startswith("text/event-stream")
                async for text in response.aiter_text():
                    events.extend(events_of(parser, text))
                    if stop_after is not None and any(e.name == stop_after for e in events):
                        break
        return events

    async def say(self, text: str) -> ApiReply:
        """Post ``text``, read the reply to its end, and tell what the turn gave."""
        reply_id = await self.post_message(text)
        events = await self.read_stream(reply_id)
        await self.server.texts_idle()
        async with self.server.session_factory() as session:
            message = await get_message(session, str(reply_id))
            actions = await actions_of(session, reply_id)
        return ApiReply(reply_id, events, str(message.content), str(message.status), actions)

    async def say_with_tool(
        self,
        name: str,
        args: dict[str, Any],
        *,
        text: str | None = None,
        judge: str = JUDGE_DEFAULT,
    ) -> ApiReply:
        """A turn in which the model calls ``name`` with ``args``
        (``chat_support.ToolCallingModel``, as ``ChatPilot.say_with_tool``)."""
        await self.server.wait_for_wakes()
        self._calls_made += 1
        call = ToolCall(f"api-pilot-{self._calls_made}", name, json.dumps(args))
        previous = self.server.model
        self.server.model = ToolCallingModel(call, judge=judge)
        try:
            return await self.say(text if text is not None else f"Please use {name}.")
        finally:
            self.server.model = previous

    async def prove(self, request_id: uuid.UUID | None, means: str, answer: str) -> httpx.Response:
        """Give a proof of the request ``request_id``."""
        return await self.client.post(
            f"{API}/confirmations/{request_id}/prove", json={"means": means, "answer": answer}
        )

    async def since(self, after: uuid.UUID | None = None) -> list[dict[str, Any]]:
        """The conversation as stored, from ``after`` on."""
        params = {} if after is None else {"after": str(after)}
        response = await self.client.get(f"{API}/since", params=params)
        assert response.status_code == 200, response.text
        return response.json()["messages"]

    @asynccontextmanager
    async def listen(self) -> AsyncIterator[Listening]:
        """Keep the stream of what Domo says on his own open while the block runs, read in the
        background; the device goes away when it ends."""
        listening = Listening()

        async def read() -> None:
            parser = SseParser()
            try:
                async with self.client.stream("GET", f"{API}/listen") as response:
                    listening.status = response.status_code
                    if response.status_code != 200:
                        await response.aread()
                        return
                    async for text in response.aiter_text():
                        for event in events_of(parser, text):
                            listening.heard(event)
            finally:
                listening.ended.set()

        task = asyncio.create_task(read())
        try:
            yield listening
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
