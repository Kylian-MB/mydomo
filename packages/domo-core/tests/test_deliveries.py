"""What each person really received of Domo's replies (spec 6.10, task 11).

The core notes a delivery where a reply goes to someone who listens (``live``: whole, or cut when
the reader left before its end), and where a page or a program comes to get the conversation
(``fetched``). A reply nobody came for has none. Noting one never fails nor slows a turn.
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator

import pytest
from api_support import ApiPilot
from chat_support import WAIT_SECONDS, ChatPilot, ChatServer, Member, consume_sse, post_message
from conversation_support import EVERYTHING, Collecting, request_of, said, stored, turn_deps
from domo_core.channels.page.abilities import PAGE
from domo_core.chat.models import Message
from domo_core.config import CoreSettings
from domo_core.conversation.channel import ListenerGone
from domo_core.conversation.events import Ended, Event, Words
from domo_core.conversation.listeners import Fanout, Listeners
from domo_core.conversation.prepare import TurnDeps
from domo_core.conversation.service import Conversation, fetched_since
from domo_core.db import SessionFactory
from domo_core.deliveries import service as deliveries_service
from domo_core.deliveries.models import Delivery
from domo_core.deliveries.service import DeliveryNotes, Reached
from domo_core.gatekeeper_client import GatekeeperClient
from fakes import FakeChatModel
from sqlalchemy import select

Row = tuple[str, uuid.UUID | None, str, str]


async def deliveries_of(factory: SessionFactory, message_id: uuid.UUID) -> list[Row]:
    """What the database holds of the deliveries of ``message_id``: (channel, device, how,
    extent), by way and channel."""
    async with factory() as session:
        rows = await session.execute(
            select(Delivery.channel, Delivery.device_id, Delivery.how, Delivery.extent)
            .where(Delivery.message_id == message_id)
            .order_by(Delivery.how, Delivery.channel)
        )
        return [(row[0], row[1], row[2], row[3]) for row in rows.all()]


async def idle(deps: TurnDeps) -> None:
    async with asyncio.timeout(WAIT_SECONDS):
        while deps.wakes.pending:
            await asyncio.sleep(0.01)


# --- live, on the page -----------------------------------------------------------------------


async def test_a_reply_streamed_to_the_page_is_delivered_whole(
    adult_chat: ChatPilot, chat_server: ChatServer
):
    adult_chat.use_model(FakeChatModel(["Hel", "lo"]))
    reply = await adult_chat.say("Hi")
    await chat_server.wait_for_wakes()
    assert await deliveries_of(chat_server.session_factory, reply.message_id) == [
        ("page", None, "live", "whole")
    ]


async def test_a_reader_who_leaves_gets_it_cut_then_whole_when_fetched(
    adult_chat: ChatPilot, chat_server: ChatServer
):
    adult_chat.use_model(FakeChatModel(["Hel", "lo", "!"], delay_s=0.3))
    reply_id = uuid.UUID(await post_message(adult_chat.client, "Hi"))
    # The page leaves after the first piece; Domo finishes his reply all the same.
    assert await consume_sse(adult_chat.client, f"/chat/stream/{reply_id}", stop_after_events=1)
    await chat_server.wait_for_wakes()
    assert (await stored(chat_server.session_factory, reply_id)).content == "Hello!"
    assert await deliveries_of(chat_server.session_factory, reply_id) == [
        ("page", None, "live", "cut")
    ]
    # The page opened again shows it: it reached them whole, and the live one stays cut.
    page = await adult_chat.get("/chat")
    assert page.status_code == 200 and "Hello!" in page.text
    assert await deliveries_of(chat_server.session_factory, reply_id) == [
        ("page", None, "fetched", "whole"),
        ("page", None, "live", "cut"),
    ]
    # Opened once more: nothing more, nothing less.
    await adult_chat.get("/chat")
    assert len(await deliveries_of(chat_server.session_factory, reply_id)) == 2


# --- through the API -------------------------------------------------------------------------


@pytest.fixture
async def adult_device(chat_server: ChatServer, adult_member: Member) -> AsyncIterator[ApiPilot]:
    async with ApiPilot.paired(chat_server, adult_member) as device:
        yield device


async def test_a_device_gets_it_live_and_fetched_on_that_device(
    adult_device: ApiPilot, chat_server: ChatServer
):
    adult_device.use_model(FakeChatModel(["Bonjour."]))
    reply = await adult_device.say("Salut")
    await chat_server.wait_for_wakes()
    device = adult_device.device_id
    assert await deliveries_of(chat_server.session_factory, reply.reply_id) == [
        ("api", device, "live", "whole")
    ]
    kept = await adult_device.since()
    assert [message["id"] for message in kept][-1] == str(reply.reply_id)
    assert await deliveries_of(chat_server.session_factory, reply.reply_id) == [
        ("api", device, "fetched", "whole"),
        ("api", device, "live", "whole"),
    ]


# --- what Domo says on his own ---------------------------------------------------------------


class Leaving:
    """A listener that takes ``keeps`` events, then is gone."""

    def __init__(self, keeps: int) -> None:
        self.keeps = keeps
        self.events: list[Event] = []

    async def emit(self, event: Event) -> None:
        if len(self.events) >= self.keeps:
            raise ListenerGone
        self.events.append(event)

    @property
    def reading(self) -> bool:
        return True

    async def open(self) -> None:
        return None


async def test_a_reply_nobody_came_for_has_no_delivery(
    chat_server: ChatServer,
    owner_member: Member,
    settings: CoreSettings,
    gk_client: GatekeeperClient,
):
    deps = turn_deps(
        session_factory=chat_server.session_factory,
        model=FakeChatModel(["never asked"]),
        settings=settings,
        gatekeeper=gk_client,
        clock=chat_server.clock,
    )
    owner = owner_member.person
    listeners = Listeners(DeliveryNotes(deps.wakes.spawn, deps.session_factory, deps.clock))
    # Domo speaks on his own while nobody listens: it reached nobody.
    alone = await said(chat_server.session_factory, owner, "Hi")
    for event in (Words("Coucou"), Ended("complete")):
        await Fanout(listeners, owner.id, alone.id).emit(event)
    await idle(deps)
    assert await deliveries_of(chat_server.session_factory, alone.id) == []
    # A listener of a channel that has no name notes nothing either.
    listeners.open(owner.id, Collecting(), EVERYTHING)
    heard = await said(chat_server.session_factory, owner, "Hi again")
    for event in (Words("Re"), Ended("complete")):
        await Fanout(listeners, owner.id, heard.id).emit(event)
    await idle(deps)
    assert await deliveries_of(chat_server.session_factory, heard.id) == []
    # One of a named channel that hears it to the end gets it whole; one that leaves halfway, cut.
    close = listeners.open(owner.id, Collecting(), EVERYTHING, channel="api")
    listeners.open(owner.id, Leaving(keeps=1), EVERYTHING, channel="test")
    third = await said(chat_server.session_factory, owner, "And now?")
    for event in (Words("Now"), Words(" yes"), Ended("complete")):
        await Fanout(listeners, owner.id, third.id).emit(event)
    await idle(deps)
    assert await deliveries_of(chat_server.session_factory, third.id) == [
        ("api", None, "live", "whole"),
        ("test", None, "live", "cut"),
    ]
    # Closed in the middle of a reply: cut.
    fourth = await said(chat_server.session_factory, owner, "One more")
    await Fanout(listeners, owner.id, fourth.id).emit(Words("Half"))
    close()
    await idle(deps)
    assert await deliveries_of(chat_server.session_factory, fourth.id) == [
        ("api", None, "live", "cut")
    ]


async def test_a_reply_taken_away_leaves_no_delivery(
    chat_server: ChatServer, owner_member: Member, session_factory
):
    owner = owner_member.person
    reply = await said(session_factory, owner, "Hi")
    async with session_factory() as session:
        await session.delete(await session.get(Message, reply.id))
        await session.commit()
    await deliveries_service.write_delivered(
        session_factory,
        Reached(reply.id, owner.id, "page", None, "live", "whole"),
        chat_server.clock(),
    )
    assert await deliveries_of(session_factory, reply.id) == []


# --- never in the way --------------------------------------------------------------------------


async def test_a_delivery_error_never_breaks_a_turn(
    chat_server: ChatServer,
    owner_member: Member,
    settings: CoreSettings,
    gk_client: GatekeeperClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    async def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("the database said: secret words")

    monkeypatch.setattr(deliveries_service, "_upsert", broken)
    deps = turn_deps(
        session_factory=chat_server.session_factory,
        model=FakeChatModel(["Hel", "lo"]),
        settings=settings,
        gatekeeper=gk_client,
        clock=chat_server.clock,
    )
    owner = owner_member.person
    reply = await said(chat_server.session_factory, owner, "Hi")
    listener = Collecting()
    with caplog.at_level(logging.ERROR):
        assert await Conversation(deps).answer(
            owner, reply.id, request_of(owner, chat_server.clock(), PAGE), listener
        )
        await idle(deps)
        assert listener.events == [Words("Hel"), Words("lo"), Ended("complete")]
        kept = await stored(chat_server.session_factory, reply.id)
        assert (kept.content, kept.status) == ("Hello", "complete")
        # Coming to get it works all the same.
        found = await fetched_since(
            chat_server.session_factory,
            owner.id,
            after=None,
            channel="page",
            device_id=None,
            now=chat_server.clock(),
        )
    assert found is not None and [m.id for m in found.messages][-1] == reply.id
    assert await deliveries_of(chat_server.session_factory, reply.id) == []
    noted = [r.getMessage() for r in caplog.records if r.name == deliveries_service.__name__]
    assert noted == [
        "A delivery could not be noted (RuntimeError)",
        "What was fetched could not be noted (RuntimeError)",
    ]
    assert "secret" not in caplog.text


async def test_the_reply_of_a_stream_is_noted_apart_from_the_turn(
    chat_server: ChatServer,
    owner_member: Member,
    settings: CoreSettings,
    gk_client: GatekeeperClient,
    monkeypatch: pytest.MonkeyPatch,
):
    """The note is written in a task of its own: a write that hangs holds no turn back."""
    hanging = asyncio.Event()

    async def slow(*args: object, **kwargs: object) -> None:
        await hanging.wait()

    monkeypatch.setattr(deliveries_service, "_upsert", slow)
    deps = turn_deps(
        session_factory=chat_server.session_factory,
        model=FakeChatModel(["Hello"]),
        settings=settings,
        gatekeeper=gk_client,
        clock=chat_server.clock,
    )
    owner = owner_member.person
    reply = await said(chat_server.session_factory, owner, "Hi")
    over = asyncio.Event()
    listener = Collecting()
    assert await Conversation(deps).answer(
        owner, reply.id, request_of(owner, chat_server.clock()), listener, when_over=over.set
    )
    async with asyncio.timeout(WAIT_SECONDS):
        await over.wait()
    assert listener.events[-1] == Ended("complete")
    assert deps.wakes.pending == 1  # the note, still waiting
    hanging.set()
    await idle(deps)
