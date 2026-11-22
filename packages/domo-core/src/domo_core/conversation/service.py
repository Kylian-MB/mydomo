"""The conversation as a channel uses it: say something, have Domo answer it, and tell the core
when the person is there.

``Conversation.say`` records the person's words and opens the reply that will answer them, as one
step: the words are checked (``MAX_TEXT_LENGTH``), the conversation is locked, and while a reply is
being written there (``reply_is_alive``) nothing more is said (``Busy``); a reply that nobody
writes any more, older than ``STALE_AFTER`` (a turn that never ended, say after a crash), is marked
as failed and no longer blocks anyone. An arrival turn that runs never makes the person busy: it
stops, and its reply goes (``visits.step_aside``). Someone who has no key Domo may use for
them, and whom no upkeep turn answers, is refused (``NoKey``).

``Conversation.answer`` writes that reply, once. The first one to ask for it claims it (in
``TurnDeps.streaming_replies``: the core is one process) and starts its turn; the others are told
it is claimed already. The claim comes first and the look at the reply after it: the other way
round, one that read ``streaming`` just before the first one stored its end could claim it just
after the first one let it go, and write a reply that is over. The turn runs apart, in a task of
the application (``TurnDeps.wakes``, stopped with the core), never in the caller: a listener that
leaves stops nothing, and Domo finishes his reply all the same (spec 5.7). The claim lasts as
long as the turn.

What reaches the person is noted (``deliveries``, spec 6.10): the reply a channel streams, whole
when its listener was given the end while someone still read it, cut when they left before; the
replies a channel comes to get (``fetched_since``: the asks of a page or a program, the chat page
opened), whole. The notes are written apart, and a note that fails is logged and forgotten: they
never fail nor slow a turn, nor what a channel is given.

The turns Domo starts himself are the core's to decide (``conversation.starts``, spec 6.6): a
channel only says that the person is there (``Conversation.seen``) or came back, that news were
kept for someone (``Conversation.notify``), or that a decision was made
(``Conversation.wake_after_decision``). What Domo then says goes to whoever listens to the person
(``conversation.listeners``).
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import takewhile
from typing import TYPE_CHECKING, Final

from domo_core.accounts.models import Person
from domo_core.chat.agent import ActionRecord
from domo_core.chat.models import Message
from domo_core.chat.store import ConversationStore
from domo_core.confirmations.service import AmendOutcome, DecideOutcome
from domo_core.conversation.channel import Listener, ListenerGone
from domo_core.conversation.events import Ended, Event, Failed
from domo_core.conversation.prepare import (
    NoKey,
    TurnDeps,
    TurnRequest,
    prepare_turn,
    upkeep_key,
)
from domo_core.conversation.speaker import Speaker
from domo_core.conversation.visits import step_aside
from domo_core.db import SessionFactory
from domo_core.deliveries.models import Extent
from domo_core.deliveries.service import DeliveryNotes, Reached, write_fetched
from domo_core.identity.clues import still_speaking
from domo_core.llm.router import usable_keys
from domo_core.tools.context import WakeStarter

if TYPE_CHECKING:
    # Only for the way a wake is given: the turns Domo starts are written there.
    from domo_core.conversation.starts import WakeVia

# The longest message a person may send, in characters.
MAX_TEXT_LENGTH: Final = 8000
# A reply that has been streaming for this long, and that nobody writes, is taken for dead.
STALE_AFTER: Final = timedelta(minutes=2)


class Busy(Exception):
    """A reply is being written in the conversation: nothing more is said until it is over."""


class NotSayable(Exception):
    """The words cannot be said: empty, longer than ``MAX_TEXT_LENGTH``, or holding a NUL."""


def reply_is_alive(reply: Message, streaming_replies: set[uuid.UUID], now: datetime) -> bool:
    """Whether a reply that is ``streaming`` is being written: someone writes it (a turn claimed
    it in ``streaming_replies``), however long that takes, or it is younger than ``STALE_AFTER``.
    Only one that nobody writes any more (the process stopped, say) is taken for dead."""
    return reply.id in streaming_replies or now - reply.created_at < STALE_AFTER


class _Delivering:
    """The listener of a reply a channel streams (``Conversation.answer``), noting what reached the
    person (``note``, once): whole when it is given the end while someone still reads it, cut when
    it is given the end once nobody does, or when nobody can receive any more (``ListenerGone``).
    A turn that never ends (stopped with the core) notes nothing."""

    def __init__(self, listener: Listener, note: Callable[[Extent], None]) -> None:
        self._listener = listener
        self._note = note
        self._noted = False

    @property
    def reading(self) -> bool:
        return self._listener.reading

    async def open(self) -> None:
        await self._listener.open()

    async def emit(self, event: Event) -> None:
        try:
            await self._listener.emit(event)
        except ListenerGone:
            self._once("cut")
            raise
        if isinstance(event, Ended):
            self._once("whole" if self._listener.reading else "cut")

    def _once(self, extent: Extent) -> None:
        if not self._noted:
            self._noted = True
            self._note(extent)


@dataclass(frozen=True)
class Since:
    """What a channel that comes to get a person's conversation is given (``fetched_since``):
    their latest conversation (``None``: they have none), and its messages asked for, oldest
    first, up to and with a reply still being written, if any (the last one: one reply at a
    time)."""

    conversation_id: uuid.UUID | None
    messages: list[Message]


async def fetched_since(
    session_factory: SessionFactory,
    person_id: uuid.UUID,
    *,
    after: uuid.UUID | None,
    channel: str,
    device_id: uuid.UUID | None,
    now: datetime,
) -> Since | None:
    """What the channel ``channel`` (on the device ``device_id``, if any) comes to get of
    ``person_id``'s latest conversation: its messages after the message ``after``, or its latest
    ones (``None``). ``None`` when ``after`` is no message of theirs.

    The replies of Domo it is given that are over reached them whole, and are noted so
    (``fetched``), in a session of their own: a note that fails is logged, and they are given all
    the same."""
    async with session_factory() as session:
        store = ConversationStore(session)
        conversation = await store.latest_conversation(person_id)
        if after is not None and await store.message_of(person_id, after) is None:
            return None
        if conversation is None:
            messages: list[Message] = []
        elif after is None:
            messages = await store.recent_messages(conversation.id)
        else:
            messages = await store.messages_after(conversation.id, after)
    over = takewhile(lambda message: message.status != "streaming", messages)
    await write_fetched(
        session_factory,
        [message.id for message in over if message.role == "assistant"],
        person_id=person_id,
        channel=channel,
        device_id=device_id,
        now=now,
    )
    return Since(None if conversation is None else conversation.id, messages)


class Conversation:
    """The conversation of the application, with what ``deps`` give every turn: the tasks the turns
    run in, the arrival turns that run and whoever listens. It keeps nothing of its own."""

    def __init__(self, deps: TurnDeps) -> None:
        self._deps = deps

    async def say(self, person: Person, text: str, speaker: Speaker) -> tuple[Message, Message]:
        """Record what ``person`` says through ``speaker``'s channel, and open the reply that will
        answer it (``streaming``, empty): both, in that order. The proofs they gave on that channel
        live on while they speak (``identity.clues.still_speaking``). Raises ``NotSayable``,
        ``NoKey`` or ``Busy`` (see the module docstring); nothing is recorded then."""
        if not text.strip() or len(text) > MAX_TEXT_LENGTH or "\x00" in text:
            raise NotSayable
        deps = self._deps
        async with deps.session_factory() as session:
            if not await usable_keys(session, person) and await upkeep_key(session) is None:
                raise NoKey
            store = ConversationStore(session)
            conversation = await store.active_conversation(person.id)
            # From here to the commit nobody else adds a message to this conversation: checking
            # that no reply is streaming, and adding one, are one step.
            await store.lock_conversation(conversation.id)
            streaming = await store.streaming_message(conversation.id)
            turns = deps.arrival_turns
            if streaming is not None and turns is not None and turns.stop(streaming.id):
                # Domo was writing first, as the person came back: never busy, his arrival
                # turn steps aside for their words (``conversation.starts``).
                await step_aside(session, streaming.id)
            elif streaming is not None:
                if reply_is_alive(streaming, deps.streaming_replies, deps.clock()):
                    raise Busy
                await store.finish_message(streaming.id, content=streaming.content, status="error")
            said = await store.add_message(
                conversation.id, role="user", content=text, status="complete", provenance="person"
            )
            # The model that writes it is chosen by the turn, call by call: the reply says which
            # once it is over.
            reply = await store.add_message(
                conversation.id,
                role="assistant",
                content="",
                status="streaming",
                provenance="model",
            )
            await still_speaking(
                session, person_id=person.id, channel=speaker.channel, now=deps.clock()
            )
            await session.commit()
        return said, reply

    async def answer(
        self,
        person: Person,
        reply_id: uuid.UUID,
        request: TurnRequest,
        listener: Listener,
        *,
        when_over: Callable[[], None] | None = None,
    ) -> bool:
        """Claim the reply ``reply_id`` of ``person`` and run its turn apart, giving its events to
        ``listener`` (see the module docstring). ``False`` when the reply is claimed already, or
        is not being written any more. ``when_over`` is called once the turn is over, however it
        ended, even cancelled before it began (the core stopping). Raises ``LookupError`` for a
        reply that is not one of Domo's in the person's conversations, and ``NoKey`` when the
        person has no key Domo may use for them: the reply is then marked as failed, and the
        listener is told so (``Failed`` with ``no_key``, then ``Ended``)."""
        deps = self._deps
        streaming_replies = deps.streaming_replies
        # There is no await between the test and the claim: they are one step.
        claimed_by_another = reply_id in streaming_replies
        if not claimed_by_another:
            streaming_replies.add(reply_id)
        handed_over = False
        try:
            async with deps.session_factory() as session:
                reply = await ConversationStore(session).message_of(person.id, reply_id)
            if reply is None or reply.role != "assistant":
                raise LookupError(reply_id)
            if claimed_by_another or reply.status != "streaming":
                return False
            try:
                turn = await prepare_turn(
                    deps,
                    person=person,
                    reply=reply,
                    request=request,
                    wake=self._wake_starter(),
                    notify=self.notify,
                )
            except NoKey:
                async with deps.session_factory() as session:
                    await ConversationStore(session).finish_message(
                        reply.id, content=reply.content, status="error", failure="no_key"
                    )
                    await session.commit()
                await listener.emit(Failed("no_key"))
                await listener.emit(Ended("error"))
                raise

            notes = DeliveryNotes(deps.wakes.spawn, deps.session_factory, deps.clock)
            speaker = request.speaker

            def note(extent: Extent) -> None:
                notes.note(
                    Reached(
                        message_id=reply.id,
                        person_id=person.id,
                        channel=speaker.channel,
                        device_id=speaker.device_id,
                        how="live",
                        extent=extent,
                    )
                )

            async def run() -> None:
                await turn.run(_Delivering(listener, note))

            task = deps.wakes.spawn(run())
            handed_over = True  # the turn lets go of the claim when it ends

            def over(_: object) -> None:
                streaming_replies.discard(reply_id)
                if when_over is not None:
                    when_over()

            task.add_done_callback(over)
            return True
        finally:
            if not claimed_by_another and not handed_over:
                streaming_replies.discard(reply_id)

    def seen(self, person: Person, *, now: datetime, came_back: bool = False) -> None:
        """A channel says ``person`` is there at ``now`` (the page's ask of what is new, a stream
        of theirs): the news that wait for them are considered (a news turn,
        ``starts.news_seen``). With ``came_back`` (the chat page opened, a stream that opens), an
        arrival turn is considered first, and the time of their visit noted
        (``starts.came_back``). It returns at once: what is weighed is weighed apart, in a task of
        the application."""
        # Imported here, not at the top: a turn Domo starts is prepared there.
        from domo_core.conversation import starts

        if came_back:
            starts.came_back(self._deps, person.id, now=now)
        else:
            starts.news_seen(self._deps, person, now=now)

    def notify(self, person_id: uuid.UUID) -> None:
        """News were just kept for ``person_id`` (committed): consider a turn for them now, if they
        are there (``starts.news_kept``). It returns at once, and never raises."""
        # Imported here, not at the top: a turn Domo starts is prepared there.
        from domo_core.conversation import starts

        starts.news_kept(self._deps, person_id)

    async def wake_after_decision(
        self,
        *,
        confirmation_id: uuid.UUID,
        outcome: DecideOutcome | AmendOutcome,
        record: ActionRecord | None,
        decider: Person,
        via: "WakeVia" = "button",
    ) -> None:
        """``decider`` decided the request ``confirmation_id`` (by a button, a proof, an arrival):
        wake Domo in the conversation of the one who asked, in a task of the application, when
        ``outcome`` decided something (``starts.spawn_wake``). It returns once the wake is
        started, never waiting for it."""
        # Imported here, not at the top: a wake prepares a turn of its own.
        from domo_core.conversation import starts

        starts.spawn_wake(
            self._deps,
            confirmation_id=confirmation_id,
            outcome=outcome,
            record=record,
            decider=decider,
            via=via,
        )

    def _wake_starter(self) -> WakeStarter:
        """What wakes Domo in the conversation of the one who asked for a request that the
        person decided in words, as its approver (spec 5.1): a wake, as after a button
        (``starts.spawn_wake``, ``via`` ``words``), in a task of the application."""
        # Imported here, not at the top: a wake prepares a turn of its own.
        from domo_core.conversation import starts

        deps = self._deps

        def start(
            confirmation_id: uuid.UUID,
            outcome: DecideOutcome | AmendOutcome,
            record: ActionRecord | None,
            decider: Person,
        ) -> None:
            starts.spawn_wake(
                deps,
                confirmation_id=confirmation_id,
                outcome=outcome,
                record=record,
                decider=decider,
                via="words",
            )

        return start
