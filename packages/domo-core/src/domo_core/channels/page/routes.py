"""The chat page: the conversation, posting a message, and the reply that streams.

The page is a channel of the conversation (``conversation``): these routes give the core what the
person says, and turn what Domo says and does into the page (``channels.page.render``). What a
turn needs of the application, every channel reads alike (``channels.turns``).

How a message gets its answer
-----------------------------

The page posts the message with htmx (``POST /chat/messages``, ``Conversation.say``). The answer to
that request is the pair of bubbles: the person's message, and an empty one for Domo whose
element opens a server-sent events connection (``GET /chat/stream/{id}``) as soon as it is in the
page. The stream has the core answer it (``Conversation.answer``): the turn runs on the server, in
a task of the application, and its events come to the page through a relay
(``channels.sse.RelaySink``) that the connection reads while the page is there: ``delta``
events piece by piece, ``action`` events for the cards, and ``done``, or ``error`` (a translated
message) and then ``done``. ``done`` is also what the page closes the connection on, so that the
browser never reconnects to a reply that is over. The page that leaves (another tab, the page
closed, the connection lost) stops the relay, never the turn, and Domo finishes his reply all the
same (spec 5.7).

One connection writes a reply, once: the first one claims it and the others get a 204, which is
also what makes a browser stop reconnecting (without the claim, every reconnection would make the
model write the reply again, and be paid again). A reply that is not being written any more is
answered 204 too.

An open page asks every five seconds for what came after its last bubble (``GET /chat/since/{id}``:
the messages that are over, rendered as the page renders them), so the reply of a turn Domo
started appears in it (a wake, an arrival, a news turn: ``conversation.starts``): that is how the
page receives what he says unasked. Each ask tells the core the person is there
(``Conversation.seen``), and opening the page that they came back; a page opened later shows it with
the rest, the reply of a page that left included. A reply that is still being written is only ever
shown word by word in the bubble that opened its stream. Anywhere else (the page opened again,
another device, a wake), while a turn writes it on the server, the page shows Domo writing in its
place, and the reply there once it is over (spec 5.7.2); so does a page whose stream stopped short
(``chat.js``).

A new message is refused (409, ``chat.error.busy``) while a reply is being written, unless that
reply is an arrival turn's (it steps aside: ``conversation.starts``). Someone with no key Domo may
use for them, whom no upkeep turn answers, is refused with the fixed message ``chat.error.no_key``
and, when they may open it, the button of the form of a key of their own (``KEY_NEW_PATH``).

What the model writes is not trusted: every ``delta`` is HTML-escaped before it is sent, the page
adds it to the bubble as text, and the stored messages are shown through the templates'
autoescaping.
"""

import re
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime
from itertools import takewhile
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import HTMLResponse, Response

from domo_core.accounts.models import Person
from domo_core.accounts.passkeys import passkey_availability_of
from domo_core.channels.page.abilities import PAGE
from domo_core.channels.page.render import PageListener, cards_of
from domo_core.channels.page.speaker import PAGE_CHANNEL, page_speaker
from domo_core.channels.sse import RelaySink, SSEResponse
from domo_core.channels.turns import get_conversation, get_turn_deps, person_is_there
from domo_core.chat.models import Message
from domo_core.chat.store import ConversationStore
from domo_core.config import CoreSettings
from domo_core.conversation.prepare import NoKey, TurnDeps, TurnRequest, upkeep_key
from domo_core.conversation.service import (
    MAX_TEXT_LENGTH,
    Busy,
    Conversation,
    NotSayable,
    fetched_since,
)
from domo_core.conversation.visits import touch_visit
from domo_core.gatekeeper_client import GatekeeperClient
from domo_core.llm.key_flow import KEY_NEW_PATH
from domo_core.llm.router import usable_keys
from domo_core.policy.catalog import PolicyTables
from domo_core.policy.key_forms import may_add_a_key, may_add_a_new_key
from domo_core.texts.card_heads import ask_missing_heads
from domo_core.web.deps import (
    SessionFactory,
    current_person,
    get_clock,
    get_gatekeeper,
    get_policy,
    get_session,
    get_session_factory,
    get_settings,
    get_streaming_replies,
)
from domo_core.web.language import request_texts
from domo_core.web.templating import domo_of, render, render_fragment

router = APIRouter()

CHAT_PATH: Final = "/chat"
MESSAGES_PATH: Final = "/chat/messages"
STREAM_PATH: Final = "/chat/stream"
SINCE_PATH: Final = "/chat/since"

# Where the answers that are not a pair of bubbles go (see ``chat.html``).
NOTICE_TARGET: Final = "#chat-notice"

_MESSAGE_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _seen(request: Request, person: Person, now: datetime, *, came_back: bool = False) -> None:
    """``person_is_there``, for a request of the page."""
    person_is_there(request.app, person, now, came_back=came_back)


def _turn_request(
    request: Request, person: Person, settings: CoreSettings, now: datetime
) -> TurnRequest:
    """What the page says of a turn: the person of the session, the page's abilities, the address
    the page was opened at, and where it can give a passkey."""
    return TurnRequest(
        speaker=page_speaker(person.id, now),
        abilities=PAGE,
        origin=str(request.base_url),
        passkey_site=passkey_availability_of(request, settings).site,
    )


def _notice(
    request: Request, status_code: int, key: str, *, key_new_url: str | None = None, **params: str
) -> Response:
    """A refusal, as a message for the notice area of the page (htmx puts it there, whatever
    element made the request); with ``key_new_url``, the button that opens the form of a key of
    one's own there."""
    return render(
        request,
        "fragments/chat_notice.html",
        status_code=status_code,
        headers={"HX-Retarget": NOTICE_TARGET, "HX-Reswap": "innerHTML"},
        key=key,
        params=params,
        key_new_url=key_new_url,
    )


def _over(messages: Sequence[Message]) -> list[Message]:
    """``messages`` (oldest first) up to the first reply that is still being written: such a
    reply is shown once it is over (only the bubble that opened its stream fills it as it
    comes; elsewhere it shows Domo writing, ``_being_written``), and it is the last message of
    its conversation (one reply at a time)."""
    return list(takewhile(lambda message: message.status != "streaming", messages))


def _being_written(
    messages: Sequence[Message], streaming_replies: set[uuid.UUID]
) -> Message | None:
    """The reply among ``messages`` that a turn writes on the server right now (a page's, whose
    page left, or a wake's: it is claimed in ``streaming_replies``), if any. A page that does not
    follow it as it comes shows Domo writing it, in its place, and chat.js puts the reply there
    once it is over (spec 5.7.2). A reply that is ``streaming`` but that nobody writes (its stream
    never opened, the process stopped) is not shown at all."""
    return next(
        (m for m in messages if m.status == "streaming" and m.id in streaming_replies), None
    )


@router.get(CHAT_PATH)
async def chat_page(
    request: Request,
    person: Annotated[Person, Depends(current_person)],
    session: Annotated[AsyncSession, Depends(get_session)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
    settings: Annotated[CoreSettings, Depends(get_settings)],
    streaming_replies: Annotated[set[uuid.UUID], Depends(get_streaming_replies)],
    session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
    policy: Annotated[PolicyTables, Depends(get_policy)],
) -> Response:
    """The conversation. Opening it tells the core the person came back (``Conversation.seen``),
    which may offer Domo an arrival turn (``conversation.starts``): its reply comes to the page
    through ``/chat/since``, once the page is there. Someone without a key Domo may use for them,
    when the general key does not serve to ask them for theirs, is offered none: the page says at
    once what a message would be answered (spec 4.11), the fixed message ``chat.error.no_key``
    with, when they may open it, the button of the form of a key of their own."""
    now = clock()
    no_key = not await usable_keys(session, person) and await upkeep_key(session) is None
    key_new_url = (
        KEY_NEW_PATH if no_key and await may_add_a_new_key(session, person, policy, now) else None
    )
    await session.commit()
    # The core reads them, and notes that the replies the page shows reached the person.
    found = await fetched_since(
        session_factory, person.id, after=None, channel=PAGE_CHANNEL, device_id=None, now=now
    )
    recent = [] if found is None else found.messages
    messages = _over(recent)
    action_cards = await cards_of(
        session,
        person,
        messages,
        request_texts(request),
        clock(),
        passkey_availability_of(request, settings).site,
    )
    # A request that waits, shown without a head for the person: theirs is asked, for the next
    # display (``texts.card_heads``).
    ask_missing_heads(
        getattr(request.app.state, "text_writing", None),
        session_factory,
        (card for cards in action_cards.values() for card in cards),
        person,
        clock=clock,
    )
    page = render(
        request,
        "chat.html",
        person=person,
        messages=messages,
        running=_being_written(recent, streaming_replies),
        action_cards=action_cards,
        max_length=MAX_TEXT_LENGTH,
        no_key=no_key,
        key_new_url=key_new_url,
    )
    _seen(request, person, now, came_back=True)
    return page


@router.get(SINCE_PATH)
async def messages_from_the_start(
    request: Request,
    person: Annotated[Person, Depends(current_person)],
    session: Annotated[AsyncSession, Depends(get_session)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
    settings: Annotated[CoreSettings, Depends(get_settings)],
    streaming_replies: Annotated[set[uuid.UUID], Depends(get_streaming_replies)],
    session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
) -> Response:
    """The bubbles of the person's conversation (the latest one), as ``messages_since`` gives
    them: what a page asks for when it has no bubble to ask from (an empty conversation, whose
    first reply an arrival turn writes, or one whose first bubble shows Domo writing). Empty
    when there is none."""
    return await _since(
        request, person, session, session_factory, clock(), settings, streaming_replies, None
    )


@router.get(SINCE_PATH + "/{message_id}")
async def messages_since(
    message_id: str,
    request: Request,
    person: Annotated[Person, Depends(current_person)],
    session: Annotated[AsyncSession, Depends(get_session)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
    settings: Annotated[CoreSettings, Depends(get_settings)],
    streaming_replies: Annotated[set[uuid.UUID], Depends(get_streaming_replies)],
    session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
) -> Response:
    """The bubbles that came after the message ``message_id`` in the person's conversation (the
    latest one), once they are over, as the chat page shows them, cards included: what an open
    page adds after its last bubble (a reply of a wake, say). Last, a reply that Domo is still
    writing on the server comes as a bubble that shows him writing (``_being_written``), which
    the page replaces with the reply once it is over (spec 5.7.2). Empty when nothing came; 404
    for a message that is not this person's.

    A page that asks keeps the person's visit going (``conversation.visits.touch_visit``):
    reloading it soon after is no arrival. It tells the core the person is there
    (``Conversation.seen``): the news that wait for them may give Domo a turn."""
    if _MESSAGE_ID.fullmatch(message_id.lower()) is None:
        raise HTTPException(status_code=404)
    return await _since(
        request,
        person,
        session,
        session_factory,
        clock(),
        settings,
        streaming_replies,
        uuid.UUID(message_id),
    )


async def _since(
    request: Request,
    person: Person,
    session: AsyncSession,
    session_factory: SessionFactory,
    now: datetime,
    settings: CoreSettings,
    streaming_replies: set[uuid.UUID],
    message_id: uuid.UUID | None,
) -> Response:
    """The bubbles of ``messages_since`` (after ``message_id``), or of ``messages_from_the_start``
    (``None``). The core reads them, and notes that the replies the page is given reached the
    person (``conversation.service.fetched_since``)."""
    found = await fetched_since(
        session_factory,
        person.id,
        after=message_id,
        channel=PAGE_CHANNEL,
        device_id=None,
        now=now,
    )
    if found is None:
        raise HTTPException(status_code=404)
    conversation_id = found.conversation_id
    after = found.messages
    await touch_visit(session, person, now=now)
    await session.commit()
    # They are there: news that wait for them may give Domo a turn now.
    _seen(request, person, now)
    new = _over(after)
    running = _being_written(after, streaming_replies)
    if conversation_id is None or (not new and running is None):
        return HTMLResponse("")
    texts = request_texts(request)
    # The cards are read as the page reads them, from the conversation: a reply's cards depend
    # on the person's message it answers, which may be older than the bubbles asked for.
    recent = _over(await ConversationStore(session).recent_messages(conversation_id))
    action_cards = await cards_of(
        session, person, recent, texts, now, passkey_availability_of(request, settings).site
    )
    templates: Jinja2Templates = request.app.state.templates
    bubbles = [
        render_fragment(
            templates,
            "fragments/message.html",
            texts,
            message=message,
            live=False,
            action_cards=action_cards,
            domo=domo_of(person.calls_domo),
        )
        for message in new
    ]
    if running is not None:
        bubbles.append(
            render_fragment(
                templates,
                "fragments/message.html",
                texts,
                message=running,
                writing=True,
                domo=domo_of(person.calls_domo),
            )
        )
    return HTMLResponse("".join(bubbles))


@router.post(MESSAGES_PATH)
async def post_message(
    request: Request,
    person: Annotated[Person, Depends(current_person)],
    session: Annotated[AsyncSession, Depends(get_session)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
    gatekeeper: Annotated[GatekeeperClient, Depends(get_gatekeeper)],
    policy: Annotated[PolicyTables, Depends(get_policy)],
    conversation: Annotated[Conversation, Depends(get_conversation)],
    text: Annotated[str, Form()] = "",
) -> Response:
    """Record the message and open the reply that will answer it (``Conversation.say``): the pair
    of bubbles. A refusal is a message for the notice area: the text (400), no key (409, with the
    form of a key of one's own if they may open it), a reply being written (409)."""
    # A browser sends the line breaks of a text box as CRLF.
    body = text.replace("\r\n", "\n").replace("\r", "\n")
    try:
        user_message, reply = await conversation.say(person, body, page_speaker(person.id, clock()))
    except NotSayable:
        return _notice(request, 400, "chat.error.text", max=str(MAX_TEXT_LENGTH))
    except NoKey:
        # No model is asked: the form of a key of their own, if they may open it.
        opens = await may_add_a_key(session, gatekeeper, person, None, policy, clock())
        return _notice(
            request, 409, "chat.error.no_key", key_new_url=KEY_NEW_PATH if opens else None
        )
    except Busy:
        return _notice(request, 409, "chat.error.busy")
    return render(request, "fragments/message_pair.html", user_message=user_message, reply=reply)


@router.get(STREAM_PATH + "/{message_id}")
async def stream_reply(
    message_id: str,
    request: Request,
    person: Annotated[Person, Depends(current_person)],
    settings: Annotated[CoreSettings, Depends(get_settings)],
    deps: Annotated[TurnDeps, Depends(get_turn_deps)],
    conversation: Annotated[Conversation, Depends(get_conversation)],
) -> Response:
    """Stream the reply that ``message_id`` is waiting for (``Conversation.answer``): 204 when
    another connection writes it, or when it is not being written any more (a browser stops
    reconnecting on it), 404 for a message that is not an assistant's, or not this person's, and
    409 when the person has no key Domo may use for them (the reply is then marked as failed).
    The turn runs in a task of the application, never in this connection, which only relays it:
    when the reader leaves, only the relay stops."""
    if _MESSAGE_ID.fullmatch(message_id.lower()) is None:
        raise HTTPException(status_code=404)
    relay = RelaySink()
    turn_request = _turn_request(request, person, settings, deps.clock())
    listener = PageListener(
        relay,
        person=person,
        texts=request_texts(request),
        templates=request.app.state.templates,
        session_factory=deps.session_factory,
        clock=deps.clock,
        site=turn_request.passkey_site,
    )
    try:
        answering = await conversation.answer(
            person, uuid.UUID(message_id), turn_request, listener, when_over=relay.close
        )
    except LookupError as missing:
        raise HTTPException(status_code=404) from missing
    except NoKey:
        return Response(status_code=409)
    if not answering:
        return Response(status_code=204)
    return SSEResponse(relay)
