"""What reached whom of Domo's replies (spec 6.10): written by the conversation core, read by
Domo.

A delivery is written where a reply goes to someone who listens (``live``: the listener was given
its end, ``whole``, or left before it, ``cut``), and where a page or a program comes to get the
conversation (``fetched``, always ``whole``). One row per reply, channel, device and way: a reply
cut, then read whole on the same channel, is whole from then on, and a whole one is never cut
again. A reply that is no longer there (the reply of a silent turn, taken away) is noted nowhere.

Writing one never fails nor slows what is delivered: ``DeliveryNotes`` writes each apart, in a task
of its own and a session of its own, and ``write_delivered`` and ``write_fetched`` log a failure by
its kind alone (its message may hold what the database said) and swallow it.

Domo reads them for the person who speaks (``received_of``: his last replies to them, their state
and an extract of his own words) and, for anyone else, only the fact (``reached``: when a reply was
said, on which channels it arrived, whether whole; never a word of it). Nothing here knows the
web."""

import logging
import uuid
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import DateTime, Uuid, and_, literal, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.chat.models import Conversation, Message
from domo_core.db import SessionFactory
from domo_core.deliveries.models import Delivery, Extent, How

logger = logging.getLogger(__name__)

# How many characters of one of his replies Domo is given back, for the person who speaks.
EXTRACT_LENGTH: Final = 80
# The most replies ``reached`` tells of: the latest ones.
MAX_REACHED: Final = 50

# What runs a coroutine apart, in a task of its own (``conversation.starts.Wakes.spawn``).
Spawn = Callable[[Coroutine[Any, Any, None]], object]


@dataclass(frozen=True)
class Reached:
    """A reply of Domo (``message_id``) that reached ``person_id`` on ``channel`` (and the
    device ``device_id``, if any), ``how``, and how much of it: what one row of ``deliveries``
    says, but when."""

    message_id: uuid.UUID
    person_id: uuid.UUID
    channel: str
    device_id: uuid.UUID | None
    how: How
    extent: Extent


@dataclass(frozen=True)
class ReplyReceipt:
    """One reply of Domo to the person who speaks, and what reached them of it: when he said it
    (``said_at``), how it ended (``status``: ``complete``, ``partial`` or ``error``), why it failed
    when the code knows (``failure``), whether it holds no word (``empty``), its first
    ``EXTRACT_LENGTH`` characters (``extract``: his own words, for them alone), and its
    deliveries, oldest first (none: it reached nobody)."""

    message_id: uuid.UUID
    said_at: datetime
    status: str
    failure: str | None
    empty: bool
    extract: str
    deliveries: tuple[Delivery, ...]


@dataclass(frozen=True)
class ReachLine:
    """One reply of Domo to someone, told to another person: only the fact. When he said it, the
    channels it reached (none: nobody saw it), and whether it reached them whole on one of them.
    Never a word of it."""

    said_at: datetime
    channels: tuple[str, ...]
    whole: bool


async def delivered(
    session: AsyncSession,
    *,
    message_id: uuid.UUID,
    person_id: uuid.UUID,
    channel: str,
    device_id: uuid.UUID | None,
    how: How,
    extent: Extent,
    now: datetime,
) -> None:
    """Note that the reply ``message_id`` reached ``person_id`` (see the module docstring): a row
    that is there already is made whole by a whole one, and is left as it is otherwise. Nothing for
    a message that is no reply of Domo to them (any more). The caller commits."""
    await _upsert(
        session,
        [message_id],
        person_id=person_id,
        channel=channel,
        device_id=device_id,
        how=how,
        extent=extent,
        now=now,
    )


async def _upsert(
    session: AsyncSession,
    message_ids: Sequence[uuid.UUID],
    *,
    person_id: uuid.UUID,
    channel: str,
    device_id: uuid.UUID | None,
    how: How,
    extent: Extent,
    now: datetime,
) -> None:
    """``delivered`` for each of ``message_ids``, in one statement: the rows come from the replies
    of Domo in ``person_id``'s conversations that are still there."""
    if not message_ids:
        return
    rows = (
        select(
            Message.id,
            literal(person_id, Uuid()),
            literal(channel),
            literal(device_id, Uuid()),
            literal(how),
            literal(extent),
            literal(now, DateTime(timezone=True)),
        )
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Message.id.in_(message_ids),
            Message.role == "assistant",
            Conversation.person_id == person_id,
        )
    )
    statement = insert(Delivery).from_select(
        ["message_id", "person_id", "channel", "device_id", "how", "extent", "at"], rows
    )
    await session.execute(
        statement.on_conflict_do_update(
            constraint="uq_deliveries_one_per_way",
            set_={"extent": statement.excluded.extent, "at": statement.excluded.at},
            where=and_(Delivery.extent == "cut", statement.excluded.extent == "whole"),
        )
    )


async def write_delivered(session_factory: SessionFactory, reached: Reached, now: datetime) -> None:
    """``delivered`` in a session of its own, committed. Never raises: a failure is logged by its
    kind alone."""
    try:
        async with session_factory() as session:
            await delivered(
                session,
                message_id=reached.message_id,
                person_id=reached.person_id,
                channel=reached.channel,
                device_id=reached.device_id,
                how=reached.how,
                extent=reached.extent,
                now=now,
            )
            await session.commit()
    except Exception as error:
        logger.error("A delivery could not be noted (%s)", type(error).__name__)


async def write_fetched(
    session_factory: SessionFactory,
    message_ids: Sequence[uuid.UUID],
    *,
    person_id: uuid.UUID,
    channel: str,
    device_id: uuid.UUID | None,
    now: datetime,
) -> None:
    """Note that the replies of Domo among ``message_ids`` reached ``person_id`` whole, on
    ``channel`` (and ``device_id``), that came to get them: in a session of its own, committed.
    Never raises: a failure is logged by its kind alone."""
    if not message_ids:
        return
    try:
        async with session_factory() as session:
            await _upsert(
                session,
                message_ids,
                person_id=person_id,
                channel=channel,
                device_id=device_id,
                how="fetched",
                extent="whole",
                now=now,
            )
            await session.commit()
    except Exception as error:
        logger.error("What was fetched could not be noted (%s)", type(error).__name__)


class DeliveryNotes:
    """What notes that a reply reached someone, apart from what delivers it: each note is written
    in a task of its own (``spawn``), in a session of its own, at the time ``clock`` gives when it
    is noted. ``note`` returns at once and never raises."""

    def __init__(
        self,
        spawn: Spawn,
        session_factory: SessionFactory,
        clock: Callable[[], datetime],
    ) -> None:
        self._spawn = spawn
        self._session_factory = session_factory
        self._clock = clock

    def note(self, reached: Reached) -> None:
        """Write ``reached`` apart (``write_delivered``)."""
        try:
            now = self._clock()
        except Exception as error:
            logger.error("A delivery could not be noted (%s)", type(error).__name__)
            return
        writing = write_delivered(self._session_factory, reached, now)
        try:
            self._spawn(writing)
        except Exception as error:
            writing.close()
            logger.error("A delivery could not be noted (%s)", type(error).__name__)


async def received_of(
    session: AsyncSession, person_id: uuid.UUID, *, limit: int = 10
) -> list[ReplyReceipt]:
    """The latest ``limit`` replies of Domo to ``person_id`` that are over (not the one being
    written), the latest first, each with what reached them of it (``ReplyReceipt``)."""
    replies = list(
        (
            await session.scalars(
                select(Message)
                .join(Conversation, Conversation.id == Message.conversation_id)
                .where(
                    Conversation.person_id == person_id,
                    Message.role == "assistant",
                    Message.status != "streaming",
                )
                .order_by(Message.created_at.desc(), Message.id.desc())
                .limit(limit)
            )
        ).all()
    )
    found: dict[uuid.UUID, list[Delivery]] = {reply.id: [] for reply in replies}
    if found:
        rows = await session.scalars(
            select(Delivery)
            .where(Delivery.message_id.in_(list(found)))
            .order_by(Delivery.at, Delivery.id)
        )
        for delivery in rows:
            found[delivery.message_id].append(delivery)
    return [
        ReplyReceipt(
            message_id=reply.id,
            said_at=reply.created_at,
            status=reply.status,
            failure=reply.failure,
            empty=not reply.content.strip(),
            extract=reply.content[:EXTRACT_LENGTH],
            deliveries=tuple(found[reply.id]),
        )
        for reply in replies
    ]


async def reached(session: AsyncSession, person_id: uuid.UUID, since: datetime) -> list[ReachLine]:
    """Domo's replies to ``person_id`` said since ``since`` that are over (the latest
    ``MAX_REACHED``), the latest first, each told by the fact alone (``ReachLine``): never a word
    of them."""
    replies = (
        await session.execute(
            select(Message.id, Message.created_at)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Conversation.person_id == person_id,
                Message.role == "assistant",
                Message.status != "streaming",
                Message.created_at >= since,
            )
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(MAX_REACHED)
        )
    ).all()
    channels: dict[uuid.UUID, set[str]] = {message_id: set() for message_id, _ in replies}
    whole: set[uuid.UUID] = set()
    if channels:
        rows = await session.execute(
            select(Delivery.message_id, Delivery.channel, Delivery.extent).where(
                Delivery.message_id.in_(list(channels))
            )
        )
        for message_id, channel, extent in rows.all():
            channels[message_id].add(channel)
            if extent == "whole":
                whole.add(message_id)
    return [
        ReachLine(
            said_at=said_at,
            channels=tuple(sorted(channels[message_id])),
            whole=message_id in whole,
        )
        for message_id, said_at in replies
    ]
