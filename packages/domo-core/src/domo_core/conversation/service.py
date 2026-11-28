"""The conversation as a channel uses it: say something, have Domo answer it, and tell the core
when the person is there.

``Conversation.say`` records the person's words and opens the reply that will answer them, as one
step: the words are checked (``MAX_TEXT_LENGTH``), the conversation is locked, and while a reply is
being written there (``reply_is_alive``) their words are stored all the same, and kept for the
turn that writes it (spec 11.3, ``conversation.kept``): it reads them between two of its actions,
or, when it has no round left, they are answered right after it by a reply of their own
(``answer_kept``, given to whoever listens: ``Fanout``). One turn at a time still writes in a
conversation. A reply that nobody writes any more, older than ``STALE_AFTER`` (a turn that never
ended, say after a crash), is marked as failed and holds nothing back. An arrival turn that runs
stops for their words, and its reply goes (``visits.step_aside``). Someone who has no key Domo may
use for them, and whom no upkeep turn answers, is refused (``NoKey``).

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

import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from itertools import takewhile
from typing import TYPE_CHECKING, Final

from sqlalchemy import select

from domo_core.accounts.models import Person
from domo_core.accounts.sessions import is_active
from domo_core.chat.agent import ActionRecord
from domo_core.chat.models import Message
from domo_core.chat.store import ConversationStore
from domo_core.confirmations.service import AmendOutcome, DecideOutcome
from domo_core.conversation.channel import NOBODY_LISTENS, Listener, ListenerGone
from domo_core.conversation.events import Ended, Event, Failed
from domo_core.conversation.listeners import Fanout
from domo_core.conversation.prepare import (
    NoKey,
    TurnDeps,
    TurnRequest,
    prepare_turn,
    upkeep_key,
)
from domo_core.conversation.speaker import (
    Speaker,
    heard_unsure,
    least_sure,
    voice_against,
)
from domo_core.conversation.visits import step_aside
from domo_core.db import SessionFactory
from domo_core.deliveries.models import Extent
from domo_core.deliveries.service import DeliveryNotes, Reached, write_fetched
from domo_core.identity.clues import still_speaking
from domo_core.llm.router import text_keys
from domo_core.tools.context import WakeStarter

if TYPE_CHECKING:
    # Only for the way a wake is given: the turns Domo starts are written there.
    from domo_core.conversation.starts import WakeVia

logger = logging.getLogger(__name__)

# The longest message a person may send, in characters.
MAX_TEXT_LENGTH: Final = 8000
# A reply that has been streaming for this long, and that nobody writes, is taken for dead.
STALE_AFTER: Final = timedelta(minutes=2)


class NotSayable(Exception):
    """The words cannot be said: empty, longer than ``MAX_TEXT_LENGTH``, or holding a NUL."""


class ReplyBeingWritten(Exception):
    """Words heard at a page signed in as someone else (``Speaker.page_person_id``) while a reply
    is being written in the person's conversation (``reply_id``): never kept for that turn, whose
    context is the person's whole one; nothing is recorded, and the channel says them again once
    that reply is over, for a turn of their own."""

    def __init__(self, reply_id: uuid.UUID) -> None:
        super().__init__("a reply is being written")
        self.reply_id = reply_id


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

    async def say(
        self,
        person: Person,
        text: str,
        speaker: Speaker,
        request: TurnRequest | None = None,
    ) -> tuple[Message, Message | None]:
        """Record what ``person`` says through ``speaker``'s channel, and open the reply that will
        answer it (``streaming``, empty): both, in that order. While a reply is being written in
        their conversation, none is opened (``None``): their words are kept for the turn that
        writes it, with what the channel says of a turn (``request``; without it, one through
        which nobody listens: ``NOBODY_LISTENS``), see the module docstring. The proofs they gave
        on that channel live on while they speak (``identity.clues.still_speaking``). Raises
        ``NotSayable`` or ``NoKey`` (see the module docstring); nothing is recorded then. Words
        heard at a page signed in as someone else are never kept for a turn: while a reply is
        being written, ``ReplyBeingWritten`` is raised and nothing is recorded.

        The words are the speaker's: ``speaker`` must be ``person`` (``ValueError`` otherwise,
        nothing recorded): someone else's words, or nobody known's, never go in a person's
        conversation as theirs. Their proofs live on only while no voice the code heard weighs
        against them (``voice_against``)."""
        if speaker.person_id != person.id:
            raise ValueError("The words are not this person's")
        if not text.strip() or len(text) > MAX_TEXT_LENGTH or "\x00" in text:
            raise NotSayable
        deps = self._deps
        # The reply being written that their words come during, if any.
        during: Message | None = None
        async with deps.session_factory() as session:
            if not await text_keys(session, person) and await upkeep_key(session) is None:
                raise NoKey
            store = ConversationStore(session)
            conversation = await store.active_conversation(person.id)
            # From here to the commit nobody else adds a message to this conversation: looking at
            # the reply being written, and adding theirs, are one step.
            await store.lock_conversation(conversation.id)
            streaming = await store.streaming_message(conversation.id)
            turns = deps.arrival_turns
            if streaming is not None and turns is not None and turns.stop(streaming.id):
                # Domo was writing first, as the person came back: his arrival turn steps aside
                # for their words (``conversation.starts``).
                await step_aside(session, streaming.id)
            elif streaming is not None:
                if reply_is_alive(streaming, deps.streaming_replies, deps.clock()):
                    during = streaming
                else:
                    await store.finish_message(
                        streaming.id, content=streaming.content, status="error"
                    )
            if during is not None and speaker.page_person_id is not None:
                # Heard at someone else's page: never read by a turn with their whole context,
                # nor said there by it (fix round 5 of Task 24). Nothing is stored.
                raise ReplyBeingWritten(during.id)
            said = await store.add_message(
                conversation.id,
                role="user",
                content=text,
                status="complete",
                provenance="person",
                # Not sure it is theirs (ruling I1): never read as their own words later.
                heard_unsure=heard_unsure(speaker),
            )
            # The model that writes it is chosen by the turn, call by call: the reply says which
            # once it is over.
            reply = (
                None
                if during is not None
                else await store.add_message(
                    conversation.id,
                    role="assistant",
                    content="",
                    status="streaming",
                    provenance="model",
                )
            )
            if not voice_against(speaker):
                await still_speaking(
                    session,
                    person_id=person.id,
                    channel=speaker.channel,
                    now=deps.clock(),
                    microphone=speaker.microphone,
                )
            await session.commit()
        if during is None:
            # What was kept for a turn that ended without answering it (one nobody wrote any
            # more) comes before this reply, which answers it with the rest.
            deps.kept.take(conversation.id)
        else:
            # Stored first, kept then: what the turn takes is in the database.
            deps.kept.keep(
                conversation.id,
                said.id,
                request if request is not None else TurnRequest(speaker, NOBODY_LISTENS),
            )
            await self._kept_unless_over(person.id, conversation.id, during.id)
        return said, reply

    async def _kept_unless_over(
        self, person_id: uuid.UUID, conversation_id: uuid.UUID, reply_id: uuid.UUID
    ) -> None:
        """Just after something was kept for ``conversation_id`` while the reply ``reply_id`` was
        being written. Its turn takes what is kept once that reply is settled: when it is still
        being written now, the turn will; when it is over, the turn may have taken what was kept
        before this, and what is kept is answered now, apart (``answer_kept``)."""
        deps = self._deps
        async with deps.session_factory() as session:
            status = await session.scalar(select(Message.status).where(Message.id == reply_id))
        if status == "streaming":
            return
        taken = deps.kept.take(conversation_id)
        if taken:
            deps.wakes.spawn(self.answer_kept(person_id, conversation_id, taken))

    async def answer_kept(
        self,
        person_id: uuid.UUID,
        conversation_id: uuid.UUID,
        kept: Sequence[tuple[uuid.UUID, TurnRequest]],
    ) -> None:
        """Answer what ``person_id`` wrote in ``conversation_id`` while a turn ran there and that
        it had no round left to read (``kept``, oldest first): a reply of its own, opened after
        them, written by a turn as for a message said once that turn was over, with what the
        channel of the last one said of a turn (its ``TurnRequest``), whose speaker is the least
        sure of theirs (``least_sure``: ruling C1), its events given to whoever
        listens to them (``Fanout``) and to whoever their channels say hears them besides
        (``TurnRequest.also_hears``). Nothing when a later message of theirs came since (its reply
        answers them all), or when they are no longer in the household. While another reply is
        being written there, they are kept for that one's turn instead. Never raises: what goes
        wrong is logged, by its kind."""
        theirs = [(m, said) for m, said in kept if said.speaker.person_id == person_id]
        if len(theirs) != len(kept):
            # Never answered as theirs, nor kept again: words of someone else, or of nobody
            # known (``say`` keeps none; this holds whoever kept them).
            logger.error("Words kept for a person were not theirs: dropped")
        kept = theirs
        if not kept:
            return
        deps = self._deps
        last, request = kept[-1]
        # The reply is as sure as the least sure of those it answers (ruling C1).
        request = replace(request, speaker=least_sure([said.speaker for _, said in kept]))
        try:
            async with deps.session_factory() as session:
                store = ConversationStore(session)
                await store.lock_conversation(conversation_id)
                latest = await session.scalar(
                    select(Message.id)
                    .where(
                        Message.conversation_id == conversation_id,
                        Message.role == "user",
                        Message.provenance == "person",
                    )
                    .order_by(Message.created_at.desc(), Message.id.desc())
                    .limit(1)
                )
                person = await session.get(Person, person_id)
                if latest != last or person is None or not is_active(person, deps.clock()):
                    return
                streaming = await store.streaming_message(conversation_id)
                reply: Message | None = None
                if streaming is None or not reply_is_alive(
                    streaming, deps.streaming_replies, deps.clock()
                ):
                    if streaming is not None:
                        # Nobody writes it any more: it no longer holds anyone.
                        await store.finish_message(
                            streaming.id, content=streaming.content, status="error"
                        )
                    reply = await store.add_message(
                        conversation_id,
                        role="assistant",
                        content="",
                        status="streaming",
                        provenance="model",
                    )
                    await session.commit()
                    deps.streaming_replies.add(reply.id)
            if reply is None:
                assert streaming is not None
                for message_id, said_with in kept:
                    deps.kept.keep(conversation_id, message_id, said_with)
                await self._kept_unless_over(person_id, conversation_id, streaming.id)
                return
            await self._write_kept_reply(person, reply, request, [r for _, r in kept])
        except Exception as error:
            logger.error(
                "What was written during a turn could not be answered (%s)", type(error).__name__
            )

    async def _write_kept_reply(
        self,
        person: Person,
        reply: Message,
        request: TurnRequest,
        kept: Sequence[TurnRequest],
    ) -> None:
        """Write ``reply``, claimed, opened for what ``person`` wrote during a turn: its events go
        to whoever listens to them, and to whoever the channels of the words it answers (``kept``)
        say hears them besides (``TurnRequest.also_hears``: the voice that heard them says it
        aloud). For a person without a key, it is marked as failed (``no_key``)."""
        deps = self._deps
        handed_over = False
        try:
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
                return
            handed_over = True  # the turn lets go of the claim when it ends
        finally:
            if not handed_over:
                deps.streaming_replies.discard(reply.id)
        await turn.hear_also(kept)
        await turn.run(Fanout(deps.listeners, person.id, reply.id))

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
