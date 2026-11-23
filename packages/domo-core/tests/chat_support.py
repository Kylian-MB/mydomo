"""Helpers of the chat tests: a live server, a server-sent events reader, shortcuts to post a
message and to look at what the database holds, and the chat pilot.

The live server is there because the pieces under test are about timing (a reply that arrives
piece by piece, a reader that leaves in the middle of it) and ``httpx.ASGITransport`` buffers a
whole response before handing it over: it can show neither. The application runs under a real
``uvicorn`` server, on the loopback interface and a free port, in the loop of the test, and the
tests talk to it over TCP.

The chat pilot (``ChatPilot``, at the end) talks with Domo as one person of the household, on
that live server: it says something, reads the whole reply, and tells what Domo said and did.
The fixtures that build it (``owner_chat``, ``adult_chat``, ``child_chat``, ``guest_chat`` and the
``live_<role>_client`` browsers) are in ``conftest.py``.
"""

import asyncio
import dataclasses
import json
import re
import socket
import uuid
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
    Collection,
    Sequence,
)
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, NamedTuple
from urllib.parse import unquote

import httpx
import uvicorn
from domo_core.accounts.models import Person
from domo_core.accounts.sessions import SESSION_COOKIE
from domo_core.audit.models import AuditEntry
from domo_core.chat.models import Message, MessageAction
from domo_core.llm.factory import ModelFactory
from domo_core.llm.models import Provider, ProviderModel, new_provider_id
from domo_core.llm.picker import ModelPicker
from domo_core.llm.types import (
    ChatChunk,
    ChatModel,
    ChatUsage,
    HistoryItem,
    ToolCall,
    ToolChoice,
    ToolDefinition,
    ToolResult,
)
from fakes import (
    JUDGE_DEFAULT,
    FakeCall,
    FakeChatModel,
    FakeSandbox,
    fake_factory,
    is_judge_call,
    is_text_checker_call,
    is_text_writer_call,
)
from fastapi import FastAPI
from sqlalchemy import Row, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from web_support import OWNER_PASSWORD, OWNER_USERNAME

# How long a test waits for the server, a stream or a database change before giving up.
# Long enough for a pause of a shared build machine (11 s seen); a test that hangs still fails.
WAIT_SECONDS = 30.0


class _Server(uvicorn.Server):
    """A uvicorn server that leaves the process signals (and so the test runner's) alone."""

    @contextmanager
    def capture_signals(self):
        yield


@asynccontextmanager
async def live_server(app: FastAPI, *, access_log: bool = False) -> AsyncIterator[str]:
    """Serve ``app`` with uvicorn on 127.0.0.1, on a free port, until the block ends; yields the
    base URL. The server runs in the current event loop. The application's lifespan is not run
    (the tests put what they need on ``app.state``), as with ``ASGITransport``. ``access_log``
    turns uvicorn's access log on (to the ``uvicorn.access`` logger)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = _Server(
        uvicorn.Config(
            app,
            log_config=None,
            access_log=access_log,
            lifespan="off",
            http="h11",
            ws="none",
            proxy_headers=False,
            server_header=False,
            timeout_graceful_shutdown=2,
        )
    )
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(WAIT_SECONDS):
            while not server.started:
                if task.done():
                    task.result()  # raises what stopped it
                    raise RuntimeError("the live server stopped before it started")
                await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, WAIT_SECONDS)
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        sock.close()


def live_http_client(base_url: str) -> httpx.AsyncClient:
    """A browser-like client of a live server: same-origin ``Origin`` header, and a timeout."""
    return httpx.AsyncClient(
        base_url=base_url,
        headers={"origin": base_url},
        timeout=httpx.Timeout(WAIT_SECONDS),
    )


async def log_in_live(
    client: httpx.AsyncClient, username: str = OWNER_USERNAME, password: str = OWNER_PASSWORD
) -> None:
    """Log in over plain http. The session cookie is ``Secure``, which an http client would not
    send back: it is read from the answer and kept as an ordinary cookie."""
    response = await client.post("/login", data={"username": username, "password": password})
    assert response.status_code == 303, response.text
    token = response.cookies[SESSION_COOKIE]
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE, token)


# --- server-sent events ----------------------------------------------------------------------


class SseEvent(NamedTuple):
    event: str
    data: str


class SseParser:
    """The reading rules of the standard for an ``text/event-stream`` body, fed piece by piece."""

    def __init__(self) -> None:
        self._buffer = ""
        self._event = "message"
        self._data: list[str] = []

    def feed(self, text: str) -> list[SseEvent]:
        self._buffer += text
        events: list[SseEvent] = []
        while True:
            # A trailing CR may be the first half of a CRLF: wait for the next piece.
            match = re.search(r"\r\n|\r(?!$)|\n", self._buffer)
            if match is None:
                return events
            line, self._buffer = self._buffer[: match.start()], self._buffer[match.end() :]
            if line == "":
                if self._data:
                    events.append(SseEvent(self._event, "\n".join(self._data)))
                self._event, self._data = "message", []
            elif not line.startswith(":"):
                name, _, value = line.partition(":")
                value = value.removeprefix(" ")
                if name == "event":
                    self._event = value
                elif name == "data":
                    self._data.append(value)


def parse_sse(body: str) -> list[SseEvent]:
    """The events of a complete body."""
    return SseParser().feed(body + "\n\n")


async def consume_sse(
    client: httpx.AsyncClient, url: str, *, stop_after_events: int | None = None
) -> list[SseEvent]:
    """Read the events of a stream. With ``stop_after_events`` the reader leaves (the connection
    is closed) once it has that many, as a browser that navigates away does; otherwise it reads
    to the end."""
    parser = SseParser()
    events: list[SseEvent] = []
    async with asyncio.timeout(WAIT_SECONDS):
        async with client.stream("GET", url, headers={"accept": "text/event-stream"}) as response:
            assert response.status_code == 200, response.status_code
            assert response.headers["content-type"].startswith("text/event-stream")
            async for text in response.aiter_text():
                events.extend(parser.feed(text))
                if stop_after_events is not None and len(events) >= stop_after_events:
                    break
    return events


async def read_stream(client: httpx.AsyncClient, url: str) -> tuple[int, list[SseEvent]]:
    """Open a stream whatever the answer is: its status, and its events (none unless it is a
    stream). Reads to the end."""
    parser = SseParser()
    events: list[SseEvent] = []
    async with asyncio.timeout(WAIT_SECONDS):
        async with client.stream("GET", url, headers={"accept": "text/event-stream"}) as response:
            if response.status_code == 200:
                async for text in response.aiter_text():
                    events.extend(parser.feed(text))
            else:
                await response.aread()
            return response.status_code, events


async def read_raw_stream(client: httpx.AsyncClient, url: str) -> bytes:
    """The bytes of a stream, exactly as they were sent."""
    async with asyncio.timeout(WAIT_SECONDS):
        async with client.stream("GET", url) as response:
            assert response.status_code == 200, response.status_code
            return b"".join([chunk async for chunk in response.aiter_raw()])


# --- posting and looking ---------------------------------------------------------------------

_STREAM_URL = re.compile(r'sse-connect="/chat/stream/([0-9a-f-]{36})"')


def assistant_id_in(fragment: str) -> str:
    """The id of the assistant message whose stream the fragment opens (exactly one)."""
    found = _STREAM_URL.findall(fragment)
    assert len(found) == 1, fragment
    return found[0]


async def post_message(client: httpx.AsyncClient, text: str) -> str:
    """Post a message; returns the id of the assistant message that was opened for the reply."""
    response = await client.post("/chat/messages", data={"text": text})
    assert response.status_code == 200, response.text
    return assistant_id_in(response.text)


async def get_message(session: AsyncSession, message_id: str) -> Row[tuple[object, ...]]:
    """What the database holds for a message, read as columns (never the session's copy)."""
    result = await session.execute(
        select(
            Message.id,
            Message.conversation_id,
            Message.role,
            Message.content,
            Message.status,
            Message.provenance,
            Message.model,
        ).where(Message.id == uuid.UUID(message_id))
    )
    return result.one()


async def wait_for_status(
    session: AsyncSession, message_id: str, statuses: Collection[str], timeout: float = WAIT_SECONDS
) -> Row[tuple[object, ...]]:
    """Wait until the message has one of ``statuses`` (the server finishes it on its own time)."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        message = await get_message(session, message_id)
        if message.status in statuses:
            return message
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"the message is still {message.status!r}, not in {statuses}")
        await asyncio.sleep(0.02)


async def last_audit(session: AsyncSession, action: str) -> Row[tuple[object, ...]]:
    """The newest audit entry of ``action`` (``details`` as stored)."""
    result = await session.execute(
        select(AuditEntry.id, AuditEntry.actor, AuditEntry.action, AuditEntry.details)
        .where(AuditEntry.action == action)
        .order_by(AuditEntry.id.desc())
        .limit(1)
    )
    return result.one()


# --- the chat pilot ----------------------------------------------------------------------------
#
# One live server per test (``ChatServer``), shared by every pilot and browser of the test: they
# are the people of one household. Each pilot is logged in as its person, with a browser of its
# own. The server's model is one slot that ``use_model`` fills: whichever pilot speaks, the server
# asks that model.

# The password of every account the pilot fixtures create.
MEMBER_PASSWORD = OWNER_PASSWORD
# How long a guest made by ``add_member`` may stay (decision D3's default).
GUEST_STAY = timedelta(days=2)
# The provider of the pilots' server (its model is whatever ``use_model`` put in the slot): an
# OpenAI-compatible server reached over https, whose key is in the vault.
# The models the pilots' key lists, the cheapest first.
PILOT_MODELS = ("example-haiku-1", "example-sonnet-1", "example-opus-1")
PILOT_PROVIDER_URL = "https://llm.example.com/v1"


@dataclass(frozen=True)
class Member:
    """A person of the test household and the account they log in with."""

    person: Person
    username: str
    password: str = MEMBER_PASSWORD


def is_text_call(system: str) -> bool:
    """Whether a call under the system prompt ``system`` is one of the writer or the checker of
    the texts."""
    return is_text_writer_call(system) or is_text_checker_call(system)


class _SlotModel:
    """What the server's model factory builds: it asks the model in the slot at the time of each
    call, and writes down what every call received (``ChatServer.calls``)."""

    def __init__(self, server: "ChatServer") -> None:
        self._server = server

    def stream(
        self,
        *,
        system: str,
        messages: Sequence[HistoryItem],
        max_output_tokens: int,
        tools: Sequence[ToolDefinition] = (),
        tool_choice: ToolChoice = "auto",
        think: bool = True,
    ) -> AsyncGenerator[Any]:
        self._server.calls.append(
            FakeCall(system, list(messages), tuple(tools), max_output_tokens, tool_choice)
        )
        return self._server.model.stream(
            system=system,
            messages=messages,
            max_output_tokens=max_output_tokens,
            tools=tools,
            tool_choice=tool_choice,
        )


@dataclass
class ChatServer:
    """The live server the pilots of a test talk to.

    ``app`` is the application (tests override its dependencies: ``get_policy``,
    ``get_tool_registry``...), ``base_url`` where it listens, ``clock`` the test clock it reads the
    time from (set to the real time when the server starts, so that what the database stamps and
    what the clock says agree), ``session_factory`` sessions that see what it committed.
    ``model`` is the model it asks (``ChatPilot.use_model`` sets it) and ``calls`` what every call
    to it received, whichever model was in the slot. ``provider_id`` is its provider's id (its key's
    reference in the vault is ``provider:<id>:api_key``).

    A page shown to someone has their texts written in the background (``app.state.text_writing``,
    on the keys they may use, as in production), by ``text_model`` (a ``FakeChatModel``: each text
    its meaning, or the text the reader has today, and no refusal; its calls cost nothing), which
    ``text_factory`` builds for every key: never the model in the slot, and their calls are in
    neither ``calls`` nor ``requested``. ``texts_idle`` waits until those writings are over.

    A yes or a no given by a button wakes Domo in the background (``conversation.starts``), and
    the turn of a reply a page streams runs there too, whether the page stays or not:
    ``wait_for_wakes`` waits until he is done with them. ``browser`` is a browser of a member of the
    household, logged in; it waits for the wakes after each answer, as the browsers of the pilots
    do, unless told not to.
    """

    app: FastAPI
    clock: Any
    session_factory: async_sessionmaker[AsyncSession]
    model: ChatModel = field(default_factory=lambda: FakeChatModel(["ok"]))
    base_url: str = ""
    calls: list[FakeCall] = field(default_factory=lambda: [])
    provider_id: str = ""
    # What the application asked its factory for: ``(provider id, model id)``, in order.
    requested: list[tuple[str, str]] = field(default_factory=lambda: [])
    # What the writer and the checker of the texts are asked.
    text_model: ChatModel = field(default_factory=lambda: FakeChatModel(usage=ChatUsage(0, 0)))
    # The key itself (``add_provider``).
    provider: Provider | None = None
    # The sandbox of the application: it executes nothing (``fakes.FakeSandbox``).
    sandbox: FakeSandbox = field(default_factory=FakeSandbox)

    def model_factory(self) -> ModelFactory:
        """The model factory the application uses: every provider gets the slot."""

        def factory(provider: Provider, model_id: str) -> ChatModel:
            self.requested.append((provider.id, model_id))
            return _SlotModel(self)

        return factory

    def text_factory(self) -> ModelFactory:
        """The model factory the writing of the texts uses: every provider gets ``text_model``
        (as it is at each call)."""

        def factory(provider: Provider, model_id: str) -> ChatModel:
            return self.text_model

        return factory

    async def add_provider(self, given_by: uuid.UUID) -> None:
        """The key the chat needs before it accepts a message (committed): the owner's, general,
        with a key in the vault, as most installations have, and the models ``PILOT_MODELS``."""
        provider_id = new_provider_id()
        provider = Provider(
            id=provider_id,
            kind="openai_compatible",
            base_url=PILOT_PROVIDER_URL,
            secret_ref=f"provider:{provider_id}:api_key",
            given_by=given_by,
            is_general=True,
        )
        async with self.session_factory() as writer:
            writer.add(provider)
            await writer.flush()
            writer.add_all(
                ProviderModel(provider_id=provider_id, model_id=model_id)
                for model_id in PILOT_MODELS
            )
            await writer.commit()
        self.provider_id = provider_id
        self.provider = provider

    def picker(self, model: ChatModel | None, person: Person) -> ModelPicker | None:
        """The models of a turn of ``person`` on the server's key, every call given ``model``
        (``None`` for none: outside a turn), for a test that calls a tool by hand."""
        if model is None:
            return None
        assert self.provider is not None, "the server has no key yet"
        return ModelPicker(
            session_factory=self.session_factory,
            model_factory=fake_factory(model),
            provider=self.provider,
            person_id=person.id,
            clock=self.clock,
        )

    @property
    def wakes_of_domo(self) -> int:
        """How many wakes of Domo, and turns of a reply, run: the tasks of ``app.state.wakes``
        but the writings of people's texts."""
        state = self.app.state
        return state.wakes.pending - state.text_writing.running

    async def texts_idle(self) -> None:
        """Wait until every writing of someone's texts asked so far is over (``TextWriting.idle``),
        for at most ``WAIT_SECONDS``."""
        async with asyncio.timeout(WAIT_SECONDS):
            await self.app.state.text_writing.idle()

    async def wait_for_wakes(self) -> None:
        """Wait until no wake of Domo, nor any turn of a reply, runs any more
        (``app.state.wakes``), for at most ``WAIT_SECONDS``."""
        async with asyncio.timeout(WAIT_SECONDS):
            while self.app.state.wakes.pending:
                await asyncio.sleep(0.01)

    @asynccontextmanager
    async def browser(
        self, member: Member, *, waits_for_wakes: bool = True
    ) -> AsyncIterator[httpx.AsyncClient]:
        """A browser of ``member`` on the server, logged in. With ``waits_for_wakes`` every answer
        it gets is only handed over once the wakes of Domo are over (a button's answer starts
        one): the steps of a test then follow each other, as a person's do. A test that looks at
        a wake while it runs uses a browser that does not wait. A reply's stream is the exception:
        its turn runs in ``app.state.wakes`` too, and the stream is read while it runs."""
        async with live_http_client(self.base_url) as client:
            await log_in_live(client, member.username, member.password)
            if waits_for_wakes:

                async def after_each_answer(response: httpx.Response) -> None:
                    if response.headers.get("content-type", "").startswith("text/event-stream"):
                        return
                    await self.wait_for_wakes()

                client.event_hooks["response"].append(after_each_answer)
            yield client


class ToolCallingModel:
    """The model of a ``say_with_tool`` turn.

    The first call that may call tools (it is given tools, with ``tool_choice="auto"``) makes
    ``call``; every later one answers "ok". The yes-judge's call (``fakes.is_judge_call``) is
    answered ``judge`` (a yes, by default). Any other call without tools (the isolated classifier
    of a reply to a pending request) is answered "unclear", so that the turn goes on to the main
    model. The writer and the checker of the texts are answered as ``FakeChatModel`` answers them.
    What each call received is in ``calls``, but for those of the writer and the checker.
    """

    def __init__(self, call: ToolCall, judge: str = JUDGE_DEFAULT) -> None:
        self._call = call
        self._judge = judge
        self._made = False
        self._texts = FakeChatModel()
        self.calls: list[FakeCall] = []

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
        if is_text_call(system):
            async for item in self._texts.stream(
                system=system,
                messages=messages,
                max_output_tokens=max_output_tokens,
                tools=tools,
                tool_choice=tool_choice,
            ):
                yield item
            return
        self.calls.append(
            FakeCall(system, list(messages), tuple(tools), max_output_tokens, tool_choice)
        )
        if is_judge_call(system):
            yield ChatChunk(self._judge)
        elif not tools:
            yield ChatChunk("unclear")
        elif tool_choice == "auto" and not self._made:
            self._made = True
            yield self._call
        else:
            yield ChatChunk("ok")
        yield ChatUsage(3, 2)


class ActsBeforeItsCall:
    """The model of a turn in which something happens while Domo thinks: the first call that
    may use tools awaits ``act`` (the person is removed, say), then makes ``call``; every later
    one answers "ok". The yes-judge's call is answered ``judge`` (a yes, by default), and any other
    call without tools (the isolated classifier) "unclear"; the writer and the checker of the
    texts as ``FakeChatModel`` answers them."""

    def __init__(
        self, act: Callable[[], Awaitable[None]], call: ToolCall, judge: str = JUDGE_DEFAULT
    ) -> None:
        self._act = act
        self._call = call
        self._judge = judge
        self._made = False
        self._texts = FakeChatModel()

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
        if is_text_call(system):
            async for item in self._texts.stream(
                system=system,
                messages=messages,
                max_output_tokens=max_output_tokens,
                tools=tools,
                tool_choice=tool_choice,
            ):
                yield item
            return
        if is_judge_call(system):
            yield ChatChunk(self._judge)
        elif not tools:
            yield ChatChunk("unclear")
        elif tool_choice == "auto" and not self._made:
            self._made = True
            await self._act()
            yield self._call
        else:
            yield ChatChunk("ok")
        yield ChatUsage(3, 2)


_CONFIRMATION_ID = re.compile(r'data-confirmation-id="([0-9a-f-]{36})"')
_CONFIRMATION_STATUS = re.compile(r'data-confirmation-status="([a-z_]+)"')
_OUTCOME = re.compile(r'data-outcome="([a-z_]+)"')
_INVITATION_TOKEN = re.compile(r"/invite/([A-Za-z0-9_-]+)")
# The poll of a vault form for a key (one given again, or a new one): its address, and the
# gatekeeper's request id in it.
_KEY_STATUS_URL = re.compile(r'hx-get="(/chat/key(?:s/new|/admin)?/status/[^"/]+/([^"/]+))"')


@dataclass(frozen=True)
class Reply:
    """What a turn gave.

    ``text`` and ``status`` are the reply as stored; ``actions`` its actions, in order, as
    ``{"tool", "status"}`` with ``"reason"`` when the action was refused or invalid;
    ``model_result_text`` the results of the tools as the model read them (one per line, in
    order; empty when no tool ran) and ``model_results`` the same results, each one read as JSON;
    ``events`` the stream as it was read, and ``cards_html`` the HTML of the action cards it
    carried.
    """

    message_id: uuid.UUID
    text: str
    status: str
    actions: list[dict[str, str]]
    model_result_text: str
    events: list[SseEvent]
    model_results: list[dict[str, Any]] = field(default_factory=lambda: [])

    @property
    def cards_html(self) -> str:
        return "".join(event.data for event in self.events if event.event == "action")

    @property
    def pending_confirmation_id(self) -> uuid.UUID | None:
        """The confirmation the reply shows (the last one, if it shows several), read on its
        card."""
        found = _CONFIRMATION_ID.findall(self.cards_html)
        return uuid.UUID(found[-1]) if found else None


@dataclass(frozen=True)
class Card:
    """A confirmation card as the server answered it: ``status`` is the outcome of the decision
    (``data-outcome``), or the state of the confirmation (``data-confirmation-status``) when the
    card carries no outcome; ``invitation_token`` is the token of an invitation link it shows."""

    id: uuid.UUID
    status: str
    html: str
    invitation_token: str | None = None

    @property
    def key_status_url(self) -> str | None:
        """The address the card's vault form for the model's key polls, if it shows one."""
        found = _KEY_STATUS_URL.search(self.html)
        return found.group(1) if found else None

    def status_url_after_fulfilment(self, gatekeeper: Any) -> str:
        """The address the card's vault form for the model's key polls, once ``gatekeeper`` (a
        ``gatekeeper_support.GatekeeperMock``) says that the owner filled the form in."""
        found = _KEY_STATUS_URL.search(self.html)
        assert found is not None, f"the card shows no form for the key: {self.html}"
        gatekeeper.set_status(unquote(found.group(2)), "fulfilled")
        return found.group(1)

    @classmethod
    def read(cls, html: str, confirmation_id: uuid.UUID) -> "Card":
        found_id = _CONFIRMATION_ID.search(html)
        outcome = _OUTCOME.search(html) or _CONFIRMATION_STATUS.search(html)
        assert outcome is not None, html
        token = _INVITATION_TOKEN.search(html)
        return cls(
            id=uuid.UUID(found_id.group(1)) if found_id else confirmation_id,
            status=outcome.group(1),
            html=html,
            invitation_token=token.group(1) if token else None,
        )


def dump(item: HistoryItem) -> dict[str, Any]:
    """An item of the history a model was sent, as plain JSON-ready data (every field of it)."""
    return dataclasses.asdict(item)


def tool_result_contents(calls: Sequence[FakeCall]) -> list[str]:
    """The results of the tools the model was sent during a turn, in order, as they were sent.

    Every round of a turn sends the whole turn again, so the call that holds the most results
    holds them all (the history of the earlier turns has none: it only keeps a summary)."""
    results = [
        [item.content for item in call.messages if isinstance(item, ToolResult)] for call in calls
    ]
    return max(results, key=len, default=[])


def tool_results_text(calls: Sequence[FakeCall]) -> str:
    """The results of the tools the model was sent during a turn, one per line, in order."""
    return "\n".join(tool_result_contents(calls))


def tool_results(calls: Sequence[FakeCall]) -> list[dict[str, Any]]:
    """The same results, each one read as JSON: what the model read back for each call."""
    return [json.loads(content) for content in tool_result_contents(calls)]


async def actions_of(session: AsyncSession, message_id: uuid.UUID) -> list[dict[str, str]]:
    """The actions of a reply, in order, as ``Reply.actions`` gives them."""
    result = await session.execute(
        select(MessageAction.tool_name, MessageAction.status, MessageAction.summary)
        .where(MessageAction.message_id == message_id)
        .order_by(MessageAction.created_at, MessageAction.id)
    )
    actions: list[dict[str, str]] = []
    for tool_name, status, summary in result.all():
        action = {"tool": tool_name, "status": status}
        if "reason" in summary:
            action["reason"] = summary["reason"]
        actions.append(action)
    return actions


class ChatPilot:
    """One person of the household, talking with Domo on the live server.

    ``say`` posts a message and reads the whole reply; ``say_with_tool`` makes the model of that
    one turn call a tool (and says what the yes-judge answers); ``confirm_last`` presses the
    confirm button of the confirmation a reply shows; ``post`` and ``get`` reach any page as this
    person (``post`` like an htmx button).
    ``use_model`` puts a model in the server's slot (and returns it). ``person`` is the person,
    ``client`` their browser, ``clock`` the server's clock, ``server`` the server itself.

    The steps of a pilot follow each other: its browser waits, after each answer, for the wakes
    of Domo that a button started (``ChatServer.browser``), and ``say_with_tool`` waits for any
    other before it puts its model in the slot, so that a wake never takes that model's call.
    ``say`` waits, once the reply is read, for the writing of the texts the reply showed
    (``texts_idle``).
    """

    def __init__(self, server: ChatServer, member: Member, client: httpx.AsyncClient) -> None:
        self.server = server
        self.member = member
        self.client = client
        self._calls_made = 0

    @property
    def person(self) -> Person:
        return self.member.person

    @property
    def clock(self) -> Any:
        return self.server.clock

    @property
    def app(self) -> FastAPI:
        return self.server.app

    async def texts_idle(self) -> None:
        """Wait until every writing of someone's texts asked so far is over."""
        await self.server.texts_idle()

    def use_model[M: ChatModel](self, model: M) -> M:
        """The server asks ``model`` from now on (until the next ``use_model``)."""
        self.server.model = model
        return model

    async def say(self, text: str) -> Reply:
        """Post ``text``, read the reply to its end, and tell what the turn gave."""
        first_call = len(self.server.calls)
        reply_id = await post_message(self.client, text)
        events = await consume_sse(self.client, f"/chat/stream/{reply_id}")
        await self.texts_idle()
        async with self.server.session_factory() as session:
            message = await get_message(session, reply_id)
            actions = await actions_of(session, uuid.UUID(reply_id))
        return Reply(
            message_id=uuid.UUID(reply_id),
            text=str(message.content),
            status=str(message.status),
            actions=actions,
            model_result_text=tool_results_text(self.server.calls[first_call:]),
            events=events,
            model_results=tool_results(self.server.calls[first_call:]),
        )

    async def say_with_tool(
        self,
        name: str,
        args: dict[str, Any],
        *,
        text: str | None = None,
        judge: str = JUDGE_DEFAULT,
    ) -> Reply:
        """A turn in which the model calls ``name`` with ``args`` (see ``ToolCallingModel``).

        The person's message is ``text``, by default a sentence that is neither a yes nor a no.
        ``judge`` is what the yes-judge answers, if it is asked (a yes by default: a test that
        expects the action done at once says ``"do"``). The model that was in the slot is put back
        afterwards."""
        await self.server.wait_for_wakes()
        self._calls_made += 1
        call = ToolCall(f"pilot-{self._calls_made}", name, json.dumps(args))
        previous = self.server.model
        self.server.model = ToolCallingModel(call, judge=judge)
        try:
            return await self.say(text if text is not None else f"Please use {name}.")
        finally:
            self.server.model = previous

    async def confirm_last(self, reply: Reply) -> Card:
        """Press the confirm button of the confirmation ``reply`` shows; the card that comes
        back."""
        confirmation_id = reply.pending_confirmation_id
        assert confirmation_id is not None, f"the reply shows no confirmation: {reply}"
        response = await self.post(f"/confirmations/{confirmation_id}/approve")
        assert response.status_code == 200, response.text
        return Card.read(response.text, confirmation_id)

    async def post(self, path: str, **kwargs: Any) -> httpx.Response:
        """POST to ``path`` as this person, the way an htmx button of the page does."""
        headers = {"hx-request": "true", **kwargs.pop("headers", {})}
        return await self.client.post(path, headers=headers, **kwargs)

    async def get(self, path: str, **kwargs: Any) -> httpx.Response:
        """GET ``path`` as this person."""
        return await self.client.get(path, **kwargs)


def status_of(response: httpx.Response) -> str:
    """What the answer to a decision says: its outcome (``data-outcome``, R23) when it carries
    one, else the state of the confirmation it shows (``data-confirmation-status``)."""
    found = _OUTCOME.search(response.text) or _CONFIRMATION_STATUS.search(response.text)
    assert found is not None, (response.status_code, response.text)
    return found.group(1)


_DIV_TAG = re.compile(r"<(/?)div(?=[\s>])[^>]*>")


def confirmation_cards(page: str) -> list[str]:
    """The confirmation cards of a page or of a stream, each one whole, as HTML."""
    cards: list[str] = []
    for start in re.finditer(r'<div class="confirmation-card"', page):
        depth = 0
        for tag in _DIV_TAG.finditer(page, start.start()):
            depth += -1 if tag.group(1) else 1
            if depth == 0:
                cards.append(page[start.start() : tag.end()])
                break
    return cards


def turn_tools(role: str) -> dict[str, Any]:
    """What a turn of someone of ``role`` tells ``build_system_prompt`` of the tools, under the
    default rights: those offered (``offered``) and those the rights keep from them (``kept``)."""
    from domo_core.policy.catalog import DEFAULT_POLICY
    from domo_core.tools.context import tools_for, tools_kept_from
    from domo_core.tools.registry import TOOLS

    return {
        "offered": frozenset(tool.name for tool in tools_for(role, TOOLS, DEFAULT_POLICY)),  # type: ignore[arg-type]
        "kept": tools_kept_from(role, TOOLS, DEFAULT_POLICY),  # type: ignore[arg-type]
    }
