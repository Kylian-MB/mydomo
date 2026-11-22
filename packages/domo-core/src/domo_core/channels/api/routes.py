"""The routes of the channel of the API (spec 6.7), under ``API_PATH``: JSON in, JSON and
server-sent events out, for a device entrusted to Domo (``channels.api.auth``). No route of it
holds any logic of Domo: each gives the core what the device says, and puts what the core gives
back into JSON (``channels.api.render``). Whatever the page can do by a turn works here by the same
core, in the same conversation.

The routes
----------

* ``POST /pair`` ``{"code", "name"}``: the program trades the code its person was shown
  (``device_entrust``) for a secret of its own (``devices.service.finish_pairing``): 200
  ``{"device_id", "secret", "person", "domo_name"}``, the secret in this answer only, never
  logged (``domo_name``: the name the person calls Domo by, ``null`` for Domo); 404
  ``unknown_code`` for a code refused, whatever the reason (wrong, ended, used); 400 ``bad_name``
  for a name that is not one plain line. Held back per client address like a login
  (``app.state.pairing_throttle``: 429 ``held_back`` with ``Retry-After``). The only route without
  the device's secret.
* ``POST /messages`` ``{"text"}``: the person says something (``Conversation.say``): 200
  ``{"reply_id"}``; 409 ``busy`` while a reply is being written in the conversation (the page's
  too), 409 ``no_key`` for someone Domo has no key for, 413 ``too_long``, 400 ``not_sayable``.
* ``GET /stream/{reply_id}``: the reply, as it is written (``Conversation.answer``): ``words``,
  ``acted``, ``failed``, then ``ended``. The turn runs in a task of the application, never in this
  connection: a device that goes away stops only the relay, and Domo finishes his reply all the
  same. 204 when another connection writes it or it is over; 404 for a reply that is not the
  person's.
* ``GET /listen``: what Domo says on his own (a wake, an arrival, a news turn:
  ``conversation.starts``), for as long as the device keeps it open (spec 6.6), each event with the
  ``message_id`` of the reply it belongs to. Opening it is a listener of the person
  (``Listeners.open_hearing``) and tells the core they came back (``person_is_there``); then every
  ``SEEN_EVERY`` seconds that they are there, and their visit goes on, as the page's asks do. The
  device is looked at again then, and when a new reply begins: a device taken back, or a person
  who left, has its stream closed. A comment line keeps the connection alive meanwhile, and shows
  a device that went away without closing it. The listener is closed whatever way the stream ends.
* ``GET /since?after=<message_id>``: the person's conversation as it is kept (the latest one),
  after that message (all of the latest messages without it): ``{"messages": [{"id", "role",
  "text", "cards"}], "domo_name"}``, up to a reply that is still being written, with the name
  the person calls Domo by (``null`` for Domo). 404 for a message that is not the person's.
  The core reads them (``conversation.service.fetched_since``), and notes that the replies the
  device is given reached the person; what a stream gives them is noted by the core too.
* ``POST /confirmations/{id}/prove`` ``{"means", "answer"}``: a proof that it is them, checked by
  the code (``proofs.service.prove``) with the means this channel carries (a password): 200
  ``{"outcome", "sentence", "once", "request"}`` (``once``: what the request that ran shows only
  once, a link or a code, as in an ``acted`` event, in this answer and nowhere else, or ``null``;
  ``request``: how the request is answered now, as in an ``acted`` event, or ``null`` when it no
  longer waits); 429 ``held_back`` by the same count as the page's
  (``get_password_check_throttle``); 503 ``busy`` when too many passwords wait to be checked. A
  device proves for its own person only. What it gives goes nowhere else: not to the
  conversation, not to a model, not to a log, not to the audit.

Every refusal is ``{"error": <code>}`` with its status, never a page and never a redirection.
"""

import asyncio
import logging
import math
import re
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from itertools import takewhile
from typing import Annotated, Final, cast

import anyio
from fastapi import APIRouter, Depends, FastAPI, Request
from starlette.responses import JSONResponse, Response
from starlette.types import Receive, Scope, Send

from domo_core.accounts.account_routes import get_password_check_throttle
from domo_core.accounts.models import Person
from domo_core.accounts.passwords import Argon2Busy, busy_headers
from domo_core.accounts.throttle import LoginThrottle
from domo_core.channels.api import API_PATH
from domo_core.channels.api.auth import (
    API,
    API_CHANNEL,
    ApiRefusal,
    api_speaker,
    current_device,
    refusal,
    secret_of,
)
from domo_core.channels.api.render import (
    API_PLACE,
    ApiListener,
    Render,
    as_data,
    decision_sentence,
    once_of,
    request_of,
    stored_messages,
)
from domo_core.channels.sse import ClientGone, EventChannel, RelaySink, SSEResponse
from domo_core.channels.turns import get_turn_deps, person_is_there
from domo_core.confirmations.routes import card_after_decision
from domo_core.confirmations.service import requester_context
from domo_core.conversation.channel import ListenerGone
from domo_core.conversation.events import Event
from domo_core.conversation.listeners import Listeners
from domo_core.conversation.prepare import (
    NoKey,
    TurnDeps,
    TurnRequest,
    made_tool_deps,
    made_tool_finder,
)
from domo_core.conversation.service import (
    MAX_TEXT_LENGTH,
    Busy,
    Conversation,
    NotSayable,
    fetched_since,
)
from domo_core.conversation.visits import touch_visit
from domo_core.devices.models import Device
from domo_core.devices.service import device_of, finish_pairing
from domo_core.policy.open_catalog import load_policy
from domo_core.policy.roles import Actor, as_role
from domo_core.proofs.service import ProofHeldBack, prove
from domo_core.texts.view import Texts, person_texts
from domo_core.tools.context import ToolContext
from domo_core.web.deps import SessionFactory, get_clock, get_session_factory

logger = logging.getLogger(__name__)

router = APIRouter(prefix=API_PATH)

# How often a stream of ``GET /listen`` tells the core that the person is there, and looks at the
# device again, in seconds.
SEEN_EVERY: Final = 20.0
# What keeps a stream alive between two events: a comment, which a reader skips.
_STILL_HERE: Final = b": still here\n\n"
_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

CurrentDevice = Annotated[tuple[Device, Person], Depends(current_device)]


def get_pairing_throttle(request: Request) -> LoginThrottle:
    """The throttle of the pairings, per client address (``create_app`` makes it)."""
    throttle: LoginThrottle = request.app.state.pairing_throttle
    return throttle


def get_api_deps(deps: Annotated[TurnDeps, Depends(get_turn_deps)]) -> TurnDeps:
    """What a turn of this channel needs of the application: the page's, but for the heads of
    the cards, which only a page shows."""
    return replace(deps, card_heads=None)


def _id_of(value: str) -> uuid.UUID:
    """An identifier named in a path or a query; 404 ``not_found`` for anything that is not
    one."""
    if _ID.fullmatch(value.lower()) is None:
        raise ApiRefusal(404, "not_found")
    return uuid.UUID(value)


async def _body(request: Request, *names: str) -> tuple[str, ...]:
    """The texts ``names`` of the request's JSON object, in that order; 400 ``bad_request`` for a
    body that is not one, or that lacks one of them."""
    try:
        found: object = await request.json()
    except ValueError:
        raise ApiRefusal(400, "bad_request") from None
    if not isinstance(found, dict):
        raise ApiRefusal(400, "bad_request")
    body = cast(dict[str, object], found)
    values = [body.get(name) for name in names]
    texts = [value for value in values if isinstance(value, str)]
    if len(texts) != len(names):
        raise ApiRefusal(400, "bad_request")
    return tuple(texts)


async def _texts_of(session_factory: SessionFactory, person: Person) -> Texts:
    """What ``person`` reads, wherever they are (never a header of the request)."""
    async with session_factory() as session:
        return await person_texts(session, person)


def _client_address(request: Request) -> str:
    """The client's address, as the web server has it: the real one behind the trusted edge."""
    return request.client.host if request.client is not None else ""


@router.post("/pair")
async def pair(
    request: Request,
    session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
    throttle: Annotated[LoginThrottle, Depends(get_pairing_throttle)],
) -> Response:
    """Trade a code for a device and its secret (see the module docstring)."""
    code, name = await _body(request, "code", "name")
    key = f"pair:{_client_address(request)}"
    # Counted before the code is looked at, and forgotten once it worked (as a login).
    wait = throttle.register_attempt(key)
    if wait is not None:
        return refusal(429, "held_back", {"Retry-After": str(math.ceil(wait))})
    async with session_factory() as session:
        try:
            made = await finish_pairing(session, code, name, clock())
        except ValueError:
            return refusal(400, "bad_name")
        if made is None:
            return refusal(404, "unknown_code")
        device, secret = made
        person = await session.get(Person, device.person_id)
        await session.commit()
    throttle.clear(key)
    return JSONResponse(
        {
            "device_id": str(device.id),
            "secret": secret,
            "person": None if person is None else person.display_name,
            "domo_name": None if person is None else person.calls_domo,
        }
    )


@router.post("/messages")
async def post_message(
    request: Request,
    known: CurrentDevice,
    deps: Annotated[TurnDeps, Depends(get_api_deps)],
) -> Response:
    """What the person says, and the reply that will answer it (see the module docstring)."""
    (text,) = await _body(request, "text")
    if len(text) > MAX_TEXT_LENGTH:
        return refusal(413, "too_long")
    device, person = known
    said = text.replace("\r\n", "\n").replace("\r", "\n")
    try:
        _, reply = await Conversation(deps).say(
            person, said, api_speaker(person.id, device.id, deps.clock())
        )
    except NotSayable:
        return refusal(400, "not_sayable")
    except NoKey:
        return refusal(409, "no_key")
    except Busy:
        return refusal(409, "busy")
    return JSONResponse({"reply_id": str(reply.id)})


@router.get("/stream/{reply_id}")
async def stream_reply(
    reply_id: str,
    request: Request,
    known: CurrentDevice,
    deps: Annotated[TurnDeps, Depends(get_api_deps)],
) -> Response:
    """The reply ``reply_id``, as it is written (see the module docstring)."""
    rid = _id_of(reply_id)
    device, person = known
    texts = await _texts_of(deps.session_factory, person)
    relay = RelaySink()
    listener = ApiListener(
        relay,
        Render(person=person, texts=texts, session_factory=deps.session_factory, clock=deps.clock),
    )
    turn_request = TurnRequest(
        speaker=api_speaker(person.id, device.id, deps.clock()),
        abilities=API,
        origin=str(request.base_url),
    )
    try:
        answering = await Conversation(deps).answer(
            person, rid, turn_request, listener, when_over=relay.close
        )
    except LookupError:
        return refusal(404, "not_found")
    except NoKey:
        return refusal(409, "no_key")
    if not answering:
        return Response(status_code=204)
    return SSEResponse(relay)


@router.get("/since")
async def since(
    known: CurrentDevice,
    session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
    after: str | None = None,
) -> Response:
    """The person's conversation as it is kept (see the module docstring)."""
    device, person = known
    last_shown = None if after is None else _id_of(after)
    now = clock()
    # The core reads them, and notes that the replies this device is given reached the person.
    found = await fetched_since(
        session_factory,
        person.id,
        after=last_shown,
        channel=API_CHANNEL,
        device_id=device.id,
        now=now,
    )
    if found is None:
        return refusal(404, "not_found")
    over = list(takewhile(lambda message: message.status != "streaming", found.messages))
    texts = await _texts_of(session_factory, person)
    kept = await stored_messages(session_factory, over, viewer=person, texts=texts, now=now)
    return JSONResponse({"messages": kept, "domo_name": person.calls_domo})


@router.post("/confirmations/{confirmation_id}/prove")
async def prove_confirmation(
    confirmation_id: str,
    request: Request,
    known: CurrentDevice,
    deps: Annotated[TurnDeps, Depends(get_api_deps)],
    throttle: Annotated[LoginThrottle, Depends(get_password_check_throttle)],
) -> Response:
    """The person's proof of the request ``confirmation_id`` (see the module docstring)."""
    cid = _id_of(confirmation_id)
    means, answer = await _body(request, "means", "answer")
    device, person = known
    texts = await _texts_of(deps.session_factory, person)
    core = Conversation(deps)
    # The catalog with the kinds of Domo's tools laid over it, as the page's decisions read it
    # (``web.deps.policy_now``): a request of one of his tools is judged as it was asked.
    async with deps.session_factory() as session:
        policy = await load_policy(session, deps.policy)
    # The moment of the decision, made the requester's (``requester_context``), as on the page.
    moment = ToolContext(
        session_factory=deps.session_factory,
        actor=Actor(person_id=person.id, role=as_role(person.role)),
        person=person,
        conversation_id=cid,  # replaced by the request's own conversation
        origin=str(request.base_url),
        gatekeeper=deps.gatekeeper,
        clock=deps.clock,
        registry=deps.registry,
        policy=policy,
        reply_message_id=None,
        texts=texts,
        text_writing=deps.text_writing,
        notify=core.notify,
        abilities=API,
        # A stored call of one of Domo's tools is found as a turn finds it, and a program run
        # by hand runs as a turn runs it.
        made_tool_named=made_tool_finder(deps),
        launch_deps=made_tool_deps(deps),
    )
    try:
        outcome, record = await prove(
            session_factory=deps.session_factory,
            confirmation_id=cid,
            person=person,
            means=means,
            answer=answer,
            place=API_PLACE,
            throttle=throttle,
            now=deps.clock(),
            ctx_factory=requester_context(moment),
            registry=deps.registry,
            policy=policy,
            speaker=api_speaker(person.id, device.id, deps.clock()),
        )
    except ProofHeldBack as held:
        return refusal(429, "held_back", {"Retry-After": str(math.ceil(held.wait))})
    except Argon2Busy:
        return refusal(503, "busy", busy_headers())
    await core.wake_after_decision(
        confirmation_id=cid, outcome=outcome, record=record, decider=person, via="proof"
    )
    card = await card_after_decision(
        deps.session_factory,
        cid,
        outcome=outcome,
        record=record,
        viewer=person,
        texts=texts,
        now=deps.clock(),
    )
    # How the request waits now, after this proof (the program offers a proof only while it says
    # so): ``None`` when it no longer waits or this person can no longer answer it.
    async with deps.session_factory() as session:
        waiting = await request_of(session, cid, person, deps.clock())
    # What the call that ran shows only once (an invitation's link, a code), in this answer and
    # nowhere else. It is made only for the one who asked, when they gave the yes themselves: a
    # proof given for someone else's request leaves it on their card (``leave_on_their_card``).
    once = None if record is None else once_of(record.card_once, texts)
    return JSONResponse(
        {
            "outcome": outcome,
            "sentence": decision_sentence(card, texts),
            "once": once,
            "request": waiting,
        }
    )


# --- what Domo says on his own ----------------------------------------------------------------


class _Heard:
    """What a stream of ``GET /listen`` was given and has not sent yet, in order: each event with
    the reply it belongs to. Once the stream is gone, it takes nothing more (``ListenerGone``)."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[tuple[uuid.UUID, Event]] = asyncio.Queue()
        self.gone = False

    async def hear(self, reply_id: uuid.UUID, event: Event) -> None:
        if self.gone:
            raise ListenerGone
        self.queue.put_nowait((reply_id, event))


class ListenResponse(Response):
    """The stream of ``GET /listen`` (see the module docstring): it sends what ``heard`` is given,
    and looks at the device again every ``SEEN_EVERY`` seconds and when a new reply begins; it
    ends when the device is no longer known, or as soon as the reader disconnects. ``close``
    closes the listener, however it ends."""

    def __init__(
        self,
        *,
        app: FastAPI,
        heard: _Heard,
        close: Callable[[], None],
        secret: str,
        person: Person,
        texts: Texts,
        session_factory: SessionFactory,
        clock: Callable[[], datetime],
    ) -> None:
        super().__init__()
        self._app = app
        self._heard = heard
        self._close = close
        self._secret = secret
        self._person = person
        self._texts = texts
        self._session_factory = session_factory
        self._clock = clock

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        channel = EventChannel(send)
        try:
            async with anyio.create_task_group() as group:

                async def watch_for_disconnect() -> None:
                    while (await receive())["type"] != "http.disconnect":
                        pass
                    group.cancel_scope.cancel()

                group.start_soon(watch_for_disconnect)
                try:
                    await self._relay(channel)
                    await channel.finish()
                except ClientGone:
                    pass
                finally:
                    group.cancel_scope.cancel()
        finally:
            self._heard.gone = True
            self._close()

    async def _relay(self, channel: EventChannel) -> None:
        await channel.open()
        begun: set[uuid.UUID] = set()
        # The next time the person is said to be there: every ``SEEN_EVERY``, events or not.
        due = anyio.current_time() + SEEN_EVERY
        while True:
            try:
                with anyio.fail_after(max(due - anyio.current_time(), 0)):
                    reply_id, event = await self._heard.queue.get()
            except TimeoutError:
                if not await self._still_known(there=True):
                    return
                await channel.write(_STILL_HERE)
                due = anyio.current_time() + SEEN_EVERY
                continue
            if reply_id not in begun:
                if not await self._still_known(there=False):
                    return
                begun.add(reply_id)
            try:
                render = Render(
                    person=self._person,
                    texts=self._texts,
                    session_factory=self._session_factory,
                    clock=self._clock,
                )
                name, data = await render.event(event)
            except Exception as error:
                # The kind of error only: its message may hold what the database said.
                logger.error("What the assistant said could not be told (%s)", type(error).__name__)
                return
            await channel.emit(name, as_data({**data, "message_id": str(reply_id)}))

    async def _still_known(self, *, there: bool) -> bool:
        """Whether the device is still known (its person read again); with ``there``, the core
        is told they are there and their visit goes on (``touch_visit``), as the page's asks do.
        Only a device taken back, or a person no longer in the household, is not: when the
        database cannot be read, the error is logged by its kind, the stream goes on, and the
        device is looked at again next time."""
        now = self._clock()
        try:
            async with self._session_factory() as session:
                found = await device_of(session, self._secret, now)
                if found is not None and there:
                    await touch_visit(session, found[1], now=now)
                await session.commit()
        except Exception as error:
            logger.error("A device could not be looked at again (%s)", type(error).__name__)
            return True
        if found is None:
            return False
        self._person = found[1]
        if there:
            person_is_there(self._app, self._person, now)
        return True


@router.get("/listen")
async def listen(
    request: Request,
    known: CurrentDevice,
    session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
) -> Response:
    """What Domo says on his own, for as long as the device listens (see the module
    docstring)."""
    device, person = known
    secret = secret_of(request)
    listeners: Listeners | None = getattr(request.app.state, "listeners", None)
    if secret is None or listeners is None:
        return refusal(503, "not_listening")
    texts = await _texts_of(session_factory, person)
    heard = _Heard()
    close = listeners.open_hearing(
        person.id, heard.hear, API, channel=API_CHANNEL, device_id=device.id
    )
    # Opened first: an arrival turn that this coming back gives Domo is heard here.
    person_is_there(request.app, person, clock(), came_back=True)
    return ListenResponse(
        app=request.app,
        heard=heard,
        close=close,
        secret=secret,
        person=person,
        texts=texts,
        session_factory=session_factory,
        clock=clock,
    )
