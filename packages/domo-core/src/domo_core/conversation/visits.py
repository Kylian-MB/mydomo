"""A person's visits, what is new for them, and what went unanswered (spec 4.9, 4.10): what the
channels and the turns read and write of them, apart from the arrival turn itself
(``conversation.starts``).

The visits
----------

The core notes when each person was last there (``persons.last_visit_at``): a channel that says
they came back (``Conversation.seen`` with ``came_back``: the chat page opened, say) sets the time
(``arrival_is_due``), and an open page keeps it going as it asks what is new every few seconds
(``touch_visit``, written at most every ``VISIT_WRITE_EVERY`` seconds). Coming back counts as an
arrival when the person was not there for ``ARRIVAL_AFTER`` at least, or never was: reloading the
page, or opening it in a second tab, does not. Two comings back at once count once (the person's
row is locked while the time is read and set).

The code offers the moment, Domo decides. How he takes these moments is one of his choices
(``Choices.arrival``, the house's, which he changes with ``writing_choose``): at every arrival
(``every_visit``, the default), only when something is new (``when_new``), or never (``never``).
What is new (``what_is_new``, counts of the code): the notes left for the person he has not told
yet, their requests someone else decided since their last visit, the people who began using a key
they gave and whom they were not told of, and the moments he could not answer them since his last
complete reply (``unanswered_in``).

News for someone does not wait
------------------------------

Something new for a person (a note left for them, a request of theirs that someone else decided,
a newcomer on a key they gave) is marked on them (``news_for``: ``persons.news_at``), in the
transaction that makes it. The person is there when a channel says so (``Conversation.seen``:
the page's ask of what is new, every few seconds), or when a listener of theirs hears what Domo
says unasked (``conversation.listeners``, told at once by ``Conversation.notify``): Domo then
has an arrival turn for them at once (``conversation.starts.consider_news``), a news turn.
Otherwise, their coming back is an arrival even within ``ARRIVAL_AFTER``, when the news came after
their last arrival turn (``arrival_is_due``). ``Choices.arrival`` still applies (``never``: no
turn at all). A reply of Domo under way for them is never stopped for news: the news wait, and
the next sign that they are there once it is over gives the turn. At most one news turn per person
every ``NEWS_TURN_EVERY``. A turn that failed (an error, no model, a limit), or that a message of
the person erased, counts for nothing but that limit: the next sign may try again. One that ended
in silence was Domo's choice: the signs that they are there do not offer that news again until
something newer comes; their next coming back does, once (an arrival that ends in silence too
blocks the comings back for that news, until newer news or ``ARRIVAL_AFTER``). Any turn of
theirs that speaks (but an upkeep turn, which is told nothing of the household) takes the news it
saw away (``clear_news``): it was told them; but for a turn that was not told a request of theirs
someone else decided (``untold_decision``), whose news stay.
``ArrivalTurns`` keeps, in the process's memory, when each person's last arrival turn was offered
and the last ones that ended, from a sign that the person is there and from their coming back
(lost at a restart: a coming back then gives one turn for news still set).

The arrival turns that run
--------------------------

The person may write as soon as they are there, while the arrival turn runs. Their message is
never refused as busy: the arrival turn steps aside. The turns that run hold their reply here
(``ArrivalTurns``, ``TurnDeps.arrival_turns``), from before the reply is committed, so that the
message finds it: ``conversation.service.Conversation.say`` stops the turn (``ArrivalTurns.stop``:
its task is cancelled, the model's stream with it) and takes its reply away (``step_aside``), then
takes the message. A reply that something already names (an action it took, a request it showed
again) is ended as it stands instead, so that its cards stay. Where there are no arrival turns
(``TurnDeps.arrival_turns`` is ``None``), none is offered at all.

What went unanswered
--------------------

A reply that ends in error keeps why (``Message.failure``). The moments Domo could not answer
the person since his last complete reply are his replies that ended in error since then
(``unanswered_in``), but for a wake's reply whose turn could not be written at all, kept for the
request it presents again (``TAKEN_BACK``, ``conversation.starts.take_back``): it answered no
message.
"""

import asyncio
import logging
import uuid
from collections.abc import Collection, Sequence
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.chat.models import Message
from domo_core.chat.store import ConversationStore
from domo_core.confirmations.models import Confirmation
from domo_core.db import SessionFactory
from domo_core.llm.choices import load_choices
from domo_core.llm.newcomers import untold_newcomers
from domo_core.prompts import MAX_UNANSWERED, UnansweredMoment
from domo_core.tools.notes import notes_to_tell

# How long the person must not have been there for their coming back to be an arrival.
ARRIVAL_AFTER: Final = timedelta(minutes=15)
# How often, at most, an open page writes the time of the visit again, in seconds: well within
# ``PAGE_OPEN_FOR``, so that a page that asks every few seconds is always seen open.
VISIT_WRITE_EVERY: Final = 20
# How recently a page must have asked what is new to be open: each of its asks says the person is
# there (``Conversation.seen``), and keeps the time of the visit within this.
PAGE_OPEN_FOR: Final = timedelta(seconds=60)
# How often, at most, a person is offered a news turn (``conversation.starts.consider_news``).
NEWS_TURN_EVERY: Final = timedelta(seconds=60)
# Why a wake's reply ended in error when its turn could not be written at all, kept for the request
# it presents again (``conversation.starts.take_back``): it is no moment without an answer.
TAKEN_BACK: Final = "taken_back"


logger = logging.getLogger(__name__)


class ArrivalTurns:
    """The arrival turns that run in the application (``TurnDeps.arrival_turns``), by the reply
    each one writes: a message the person posts meanwhile stops the turn (``stop``). One process:
    the registry is in its memory, like the replies being written (``streaming_replies``).

    It keeps too, for each person, when their last arrival turn was offered (``offered``: at most
    one news turn every ``NEWS_TURN_EVERY``), and when the last ones that ended (spoke or stayed
    silent: ``settled``) were offered: any of them (``last_settled``: the news that came before it
    were its to tell, the signs that the person is there offer them no more), and those of a coming
    back (``last_page_arrival``: a coming back offers news that came before it no more). A turn that
    failed, or that a message of the person erased, settles nothing."""

    def __init__(self) -> None:
        self._running: dict[uuid.UUID, asyncio.Task[object]] = {}
        self._offered: dict[uuid.UUID, datetime] = {}
        self._settled: dict[uuid.UUID, datetime] = {}
        self._page_settled: dict[uuid.UUID, datetime] = {}

    def offered(self, person_id: uuid.UUID, at: datetime) -> None:
        """An arrival turn of ``person_id`` is offered at ``at``."""
        self._offered[person_id] = at

    def settled(self, person_id: uuid.UUID, at: datetime, *, page: bool) -> None:
        """The arrival turn of ``person_id`` offered at ``at`` ended, in words or in silence:
        Domo was told the news that came before it. ``page``: it was a coming back's."""
        for kept in (self._settled, self._page_settled) if page else (self._settled,):
            last = kept.get(person_id)
            if last is None or at > last:
                kept[person_id] = at

    def last_settled(self, person_id: uuid.UUID) -> datetime | None:
        """When the last arrival turn of ``person_id`` that ended was offered, if one was since the
        process started."""
        return self._settled.get(person_id)

    def last_page_arrival(self, person_id: uuid.UUID) -> datetime | None:
        """When the last arrival turn of a coming back of ``person_id`` that ended was offered, if
        one was since the process started."""
        return self._page_settled.get(person_id)

    def could_be_due(self, person_id: uuid.UUID, *, news_at: datetime, now: datetime) -> bool:
        """Whether a news turn could be offered to ``person_id`` at ``now`` for news of
        ``news_at``: none was offered within ``NEWS_TURN_EVERY``, and no arrival turn that ended
        was offered since the news came. Reads nothing but the memory."""
        last = self._offered.get(person_id)
        if last is not None and now - last < NEWS_TURN_EVERY:
            return False
        settled = self._settled.get(person_id)
        return settled is None or news_at > settled

    def news_turn_due(self, person_id: uuid.UUID, *, news_at: datetime, now: datetime) -> bool:
        """``could_be_due``, marked offered when it is, in the same step: no other ask finds it
        free."""
        if not self.could_be_due(person_id, news_at=news_at, now=now):
            return False
        self._offered[person_id] = now
        return True

    def hold(self, reply_id: uuid.UUID) -> None:
        """The task that runs now writes the arrival reply ``reply_id``."""
        task = asyncio.current_task()
        if task is not None:
            self._running[reply_id] = task

    def release(self, reply_id: uuid.UUID) -> None:
        """The arrival turn of ``reply_id`` is over."""
        self._running.pop(reply_id, None)

    def stop(self, reply_id: uuid.UUID) -> bool:
        """Stop the arrival turn that writes ``reply_id``, if one does: its task is cancelled (what
        the turn stores at its end is shielded). Whether ``reply_id`` was an arrival's."""
        task = self._running.pop(reply_id, None)
        if task is None:
            return False
        task.cancel()
        return True


async def arrival_is_due(
    session: AsyncSession,
    person: Person,
    *,
    now: datetime,
    last_page_arrival: datetime | None = None,
) -> tuple[bool, timedelta | None]:
    """Whether coming back at ``now`` offers Domo an arrival turn for ``person``, and how long
    they were away (``None``: their first visit; ``now`` less that is their last visit, exactly).
    Due when they were never there, or not for ``ARRIVAL_AFTER``, or when news came for them that
    no coming back of theirs offered yet (``news_at`` after ``last_page_arrival``, when the last
    arrival turn of a coming back that ended was offered; any news when the process
    knows of none: ``ArrivalTurns``), and Domo's choice allows it (``Choices.arrival``:
    ``when_new`` only when something is new, ``never`` never). Sets the time of the visit to
    ``now`` whatever it finds, the person's row locked meanwhile, so that two comings back at once
    count once (flushed: the caller commits)."""
    # FOR NO KEY UPDATE: it waits for another coming back at once, never for what refers to the
    # person (a row of theirs being written).
    found = await session.execute(
        select(Person)
        .where(Person.id == person.id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    )
    locked = found.scalar_one()
    last = locked.last_visit_at
    locked.last_visit_at = now
    await session.flush()
    away = None if last is None else now - last
    news = locked.news_at is not None and (
        last_page_arrival is None or locked.news_at > last_page_arrival
    )
    if away is not None and away < ARRIVAL_AFTER and not news:
        return False, away
    choice = (await load_choices(session, None)).arrival
    if choice == "never":
        return False, away
    if choice == "when_new" and not news:
        new = await what_is_new(session, locked, since=last)
        return any(new.values()), away
    return True, away


async def news_for(session: AsyncSession, person_id: uuid.UUID, *, now: datetime) -> None:
    """Something new came at ``now`` for ``person_id`` that no turn of Domo said yet: a note left
    for them, a request of theirs someone else decided, a newcomer on a key they gave
    (``persons.news_at``, see the module docstring). In the transaction that makes it: flushed, the
    caller commits, then tells the core (``Conversation.notify``): a news turn may be due.
    Never back in time: a decision passes when it was made, which may be before news already
    marked."""
    await session.execute(
        update(Person)
        .where(Person.id == person_id)
        .values(news_at=func.greatest(Person.news_at, now))
    )


async def untold_decision(
    session: AsyncSession,
    person_id: uuid.UUID,
    *,
    since: datetime | None,
    told: Collection[uuid.UUID],
) -> bool:
    """Whether a request of ``person_id`` that someone else decided after ``since`` (when the
    turn's last complete reply was written; ``None``: ever) is none of ``told``, the requests the
    turn is told of (those its history names, the one a wake was woken for): its news then stay
    when the turn speaks (``clear_news`` is not called). Reads only."""
    query = select(Confirmation.id).where(
        Confirmation.requester_id == person_id,
        Confirmation.decided_by.is_not(None),
        Confirmation.decided_by != person_id,
    )
    if since is not None:
        query = query.where(Confirmation.decided_at > since)
    if told:
        query = query.where(Confirmation.id.not_in(list(told)))
    return await session.scalar(select(query.exists())) or False


async def clear_news(
    session_factory: SessionFactory, person_id: uuid.UUID, *, seen: datetime
) -> None:
    """A turn of ``person_id`` that read their news as of ``seen`` (``persons.news_at`` then) spoke:
    those news were told; newer ones stay. Committed; a failure is logged (its kind only), and the
    news then stay for the next turn."""
    try:
        async with session_factory() as session:
            await session.execute(
                update(Person)
                .where(Person.id == person_id, Person.news_at <= seen)
                .values(news_at=None)
            )
            await session.commit()
    except Exception as error:
        logger.error("The news told could not be cleared (%s)", type(error).__name__)


async def touch_visit(session: AsyncSession, person: Person, *, now: datetime) -> None:
    """An open page keeps the visit going (``GET /chat/since``): the time of the visit becomes
    ``now``, at most every ``VISIT_WRITE_EVERY`` seconds (flushed: the caller commits)."""
    last = person.last_visit_at
    if last is not None and now - last < timedelta(seconds=VISIT_WRITE_EVERY):
        return
    person.last_visit_at = now
    await session.flush()


async def what_is_new(
    session: AsyncSession, person: Person, *, since: datetime | None
) -> dict[str, int]:
    """What is new for ``person`` (``prompts.ArrivalNote.new``), as counts: the notes left for
    them that Domo has not told yet (``tools.notes.notes_to_tell``), their requests that someone
    else, or nobody, decided after ``since`` (their last visit; none for a first one), the people
    who began using a key they gave and whom they were not told of
    (``llm.newcomers.untold_newcomers``), and the moments he could not answer them since his last
    complete reply (``unanswered_in``). Reads only."""
    notes = len(await notes_to_tell(session, person.id))
    decided = 0
    if since is not None:
        found = await session.execute(
            select(func.count())
            .select_from(Confirmation)
            .where(
                Confirmation.requester_id == person.id,
                Confirmation.decided_at > since,
                or_(Confirmation.decided_by.is_(None), Confirmation.decided_by != person.id),
            )
        )
        decided = found.scalar_one()
    store = ConversationStore(session)
    conversation = await store.latest_conversation(person.id)
    moments = (
        () if conversation is None else unanswered_in(await store.recent_messages(conversation.id))
    )
    return {
        "notes": notes,
        "decided": decided,
        "newcomers": await untold_newcomers(session, giver_id=person.id),
        "unanswered": len(moments),
    }


def unanswered_in(messages: Sequence[Message]) -> tuple[UnansweredMoment, ...]:
    """The moments Domo could not answer the person, among ``messages`` (oldest first: their
    conversation): his replies that ended in error since his last complete one, oldest first,
    the latest ``MAX_UNANSWERED`` at most, each with when and why (``Message.failure``; a reply
    nobody wrote any more has none: ``unknown``). A reply cut short or still being written is no
    such moment, a turn that stayed silent left no reply (spec 4.10), and a wake's reply whose
    turn could not be written (``TAKEN_BACK``) answered nothing."""
    moments: list[UnansweredMoment] = []
    for message in messages:
        if message.role != "assistant":
            continue
        if message.status == "complete":
            moments.clear()
        elif message.status == "error" and message.failure != TAKEN_BACK:
            moments.append(
                UnansweredMoment(
                    at=message.created_at.astimezone(UTC).isoformat(),
                    why=message.failure or "unknown",
                )
            )
    return tuple(moments[-MAX_UNANSWERED:])


async def step_aside(session: AsyncSession, reply_id: uuid.UUID) -> None:
    """The reply ``reply_id`` of an arrival turn that the person's message stopped
    (``ArrivalTurns.stop``), in the transaction of that message (the conversation locked): taken
    away, as if it had never been opened; ended as it stands instead when something already names
    it (an action it took, a request it showed again), so that its cards stay. One the turn
    settled meanwhile stays as it is. Flushed, not committed.

    The reply's row is locked first: an action of the turn being recorded at that moment (its
    row refers to the reply) waits for this transaction, and is never taken away with the reply
    unseen; nor does the turn settle it meanwhile."""
    store = ConversationStore(session)
    found = await session.execute(
        select(Message.content)
        .where(Message.id == reply_id, Message.status == "streaming")
        .with_for_update()
    )
    content = found.scalar_one_or_none()
    if content is None:
        # The turn settled it meanwhile (or took it away): it stays as it is.
        return
    if not await store.actions_of([reply_id]):
        try:
            async with session.begin_nested():
                await store.drop_reply(reply_id)
            return
        except IntegrityError:
            # A request names it (it showed one again): it stays.
            pass
    await store.finish_message(reply_id, content=content, status="partial")
