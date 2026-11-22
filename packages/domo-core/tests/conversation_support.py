"""Helpers of the tests of the conversation core, with no channel: a listener that keeps what it
is given, and what a turn needs of the application."""

import uuid
from collections.abc import Callable
from datetime import datetime

from domo_core.accounts.models import Person
from domo_core.chat.models import Message
from domo_core.chat.store import ConversationStore
from domo_core.config import CoreSettings
from domo_core.conversation.channel import Abilities
from domo_core.conversation.events import Event
from domo_core.conversation.prepare import TurnDeps, TurnRequest
from domo_core.conversation.speaker import Clue, Speaker
from domo_core.conversation.starts import Wakes
from domo_core.db import SessionFactory
from domo_core.gatekeeper_client import GatekeeperClient
from domo_core.llm.types import ChatModel
from domo_core.policy.catalog import DEFAULT_POLICY
from domo_core.tools.registry import TOOLS
from fakes import FakeModelLister, fake_factory

# A channel that can do everything a page can, but that is no page.
EVERYTHING: Abilities = Abilities(
    buttons=True,
    shows_once=True,
    safe_screen=True,
    hears_unasked=True,
    proof_kinds=frozenset({"password"}),
)


class Collecting:
    """A ``conversation.channel.Listener`` that keeps every event it is given, in order
    (``events``). ``reading`` is whether someone reads it (true unless set otherwise)."""

    def __init__(self) -> None:
        self.events: list[Event] = []
        self.opened = False
        self.is_reading = True

    @property
    def reading(self) -> bool:
        return self.is_reading

    async def open(self) -> None:
        self.opened = True

    async def emit(self, event: Event) -> None:
        self.events.append(event)


def turn_deps(
    *,
    session_factory: SessionFactory,
    model: ChatModel,
    settings: CoreSettings,
    gatekeeper: GatekeeperClient,
    clock: Callable[[], datetime],
) -> TurnDeps:
    """What a turn needs, with ``model`` for every key and model id, the tools of the core and the
    default catalog, tasks of its own for the turns, no writing of texts, no arrival turns and
    nobody listening."""
    return TurnDeps(
        session_factory=session_factory,
        model_factory=fake_factory(model),
        model_lister=FakeModelLister(),
        streaming_replies=set(),
        settings=settings,
        gatekeeper=gatekeeper,
        clock=clock,
        registry=TOOLS,
        policy=DEFAULT_POLICY,
        text_writing=None,
        wakes=Wakes(),
    )


def request_of(person: Person, now: datetime, abilities: Abilities = EVERYTHING) -> TurnRequest:
    """The request of a turn of ``person``, sure of them (their session), on a channel that can do
    ``abilities``."""
    session = Clue("session", "for", "strong", "code", now)
    return TurnRequest(
        speaker=Speaker(person.id, "test", clues=(session,)),
        abilities=abilities,
        origin="https://domo.example.com/",
    )


async def said(session_factory: SessionFactory, person: Person, text: str) -> Message:
    """``person`` said ``text`` in their conversation: the reply that will answer it, ``streaming``
    and empty (committed)."""
    async with session_factory() as session:
        store = ConversationStore(session)
        conversation = await store.active_conversation(person.id)
        await store.add_message(
            conversation.id, role="user", content=text, status="complete", provenance="person"
        )
        reply = await store.add_message(
            conversation.id, role="assistant", content="", status="streaming", provenance="model"
        )
        await session.commit()
        return reply


async def stored(session_factory: SessionFactory, reply_id: uuid.UUID) -> Message:
    """The reply ``reply_id`` as the database holds it now."""
    async with session_factory() as session:
        found = await session.get(Message, reply_id, populate_existing=True)
        assert found is not None
        return found
