"""Where a channel that knows people by something of its own plugs in (spec 6.12): the voice, later.

Nothing here listens, recognises anyone or opens a record: the assistant opens a record with his
own tool (``record_open``), with his own sentence for why. What a channel brings stays the
channel's (what a reference is made of, the clues, the device it came through); the core keeps
only that a reference exists for a person on a channel (``person_channel_refs``):

* ``claim_reference``: "this reference now belongs to this person". It gives no access and changes
  nothing of the record. A person may hold several references on one channel.
* ``refs_of`` and ``person_of_ref``: what exists now, for a channel to drop its orphans after a
  notice it missed (notices are never sent again).
* ``RecordListeners``: what a channel subscribes to, told after the commit of a merge, a split, an
  erasure or a forgetting (``RecordEvent``), by whoever committed it (the tools of those gestures,
  in a turn or a review). One instance for the application (``TurnDeps.record_listeners``).

This module imports no channel, and works with none subscribed.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.people.models import ChannelRef, RecordMerge

__all__ = [
    "MAX_CHANNEL",
    "MAX_REFERENCE",
    "NoSuchRecord",
    "Notice",
    "RecordEvent",
    "RecordListeners",
    "ReferenceTaken",
    "claim_reference",
    "merged",
    "person_of_ref",
    "refs_held",
    "refs_of",
]

logger = logging.getLogger(__name__)

# The lengths the table holds (``people.models.ChannelRef``).
MAX_CHANNEL: Final = 40
MAX_REFERENCE: Final = 200

Notice = Literal["merged", "split", "erased", "forgotten"]
# A reference as a notice gives it: ``(channel, reference)``.
Ref = tuple[str, str]


class NoSuchRecord(Exception):
    """The person does not exist, is merged into another record, or is forgotten."""


class ReferenceTaken(Exception):
    """Another person holds that reference on that channel (``holder``)."""

    def __init__(self, holder: UUID) -> None:
        super().__init__("the reference belongs to another person")
        self.holder = holder


def _plain(value: str, most: int, what: str) -> str:
    """``value`` as it is, when it is 1 to ``most`` characters on one plain line (no control
    character): ``ValueError`` otherwise."""
    if not 1 <= len(value) <= most or not value.isprintable():
        raise ValueError(f"{what} is 1 to {most} characters, on one plain line")
    return value


async def _holder(session: AsyncSession, channel: str, reference: str) -> UUID | None:
    return await session.scalar(
        select(ChannelRef.person_id).where(
            ChannelRef.channel == channel, ChannelRef.reference == reference
        )
    )


async def claim_reference(
    session: AsyncSession, *, channel: str, reference: str, person_id: UUID, now: datetime
) -> None:
    """``reference``, an opaque text the channel ``channel`` owns, now belongs to ``person_id``
    (kept exactly as given; never what it is made of). Another reference of the same person on the
    same channel is added beside theirs; the same claim twice changes nothing. ``ReferenceTaken``
    when another person holds it on that channel; ``NoSuchRecord`` for a person that does not
    exist, is merged into another record or is forgotten; ``ValueError`` for a channel of more
    than ``MAX_CHANNEL`` characters, a reference of more than ``MAX_REFERENCE``, an empty one, or
    one of several lines. It gives no access and changes no state. Flushed, not committed."""
    _plain(channel, MAX_CHANNEL, "a channel")
    _plain(reference, MAX_REFERENCE, "a reference")
    person = await session.get(Person, person_id)
    if person is None or person.merged_into_id is not None or person.forgotten_at is not None:
        raise NoSuchRecord
    holder = await _holder(session, channel, reference)
    if holder == person_id:
        return
    if holder is not None:
        raise ReferenceTaken(holder)
    try:
        async with session.begin_nested():
            session.add(
                ChannelRef(
                    person_id=person_id, channel=channel, reference=reference, created_at=now
                )
            )
            await session.flush()
    except IntegrityError:
        # Claimed at the same moment by another call.
        holder = await _holder(session, channel, reference)
        if holder == person_id:
            return
        if holder is None:
            raise
        raise ReferenceTaken(holder) from None


async def refs_of(session: AsyncSession, channel: str) -> set[str]:
    """Every reference that exists now for ``channel``."""
    return set(
        await session.scalars(select(ChannelRef.reference).where(ChannelRef.channel == channel))
    )


async def person_of_ref(session: AsyncSession, *, channel: str, reference: str) -> UUID | None:
    """The person ``reference`` belongs to now on ``channel`` (the record kept, after a merge);
    ``None`` once it was erased or forgotten with them, or when it was never claimed."""
    return await _holder(session, channel, reference)


async def refs_held(session: AsyncSession, person_id: UUID) -> tuple[Ref, ...]:
    """The references ``person_id`` holds now, on every channel, in order: what a notice about
    them lists (read before an erasure or a forgetting)."""
    found = await session.execute(
        select(ChannelRef.channel, ChannelRef.reference)
        .where(ChannelRef.person_id == person_id)
        .order_by(ChannelRef.channel, ChannelRef.reference)
    )
    return tuple((channel, reference) for channel, reference in found.all())


@dataclass(frozen=True)
class RecordEvent:
    """What a channel is told of a record, after the commit.

    * ``merged``: ``person_id`` is the record absorbed, ``into_id`` the record kept, ``refs`` the
      references that moved to the kept one;
    * ``split``: ``person_id`` is the record that exists again, ``into_id`` the one it had been
      merged into, ``refs`` the references that moved back to it (one claimed for the kept record
      since the merge stays there);
    * ``erased``: ``person_id`` the record, ``refs`` those it had, deleted with it;
    * ``forgotten``: ``person_id`` the record, ``refs`` those it had. ``keep_minimum`` says they
      survive the forgetting, on the emptied record (the owner refused or withdrew that person,
      and what is needed to know them again is kept: ``people.forget.keeps_recognition``); else
      they are deleted, and the channel drops what it holds for them.

    ``into_id`` is ``None`` but for a merge and a split; ``keep_minimum`` is ``False`` but for a
    forgetting. Each of ``refs`` is ``(channel, reference)``."""

    kind: Notice
    person_id: UUID
    into_id: UUID | None
    refs: tuple[Ref, ...]
    keep_minimum: bool
    at: datetime


def merged(trace: RecordMerge, at: datetime) -> RecordEvent:
    """The notice of the merge ``trace`` (``people.merge.merge``, which writes the references that
    moved in ``RecordMerge.moved["channel_refs"]``)."""
    moved = trace.moved.get("channel_refs", [])
    return RecordEvent(
        kind="merged",
        person_id=trace.absorbed_id,
        into_id=trace.kept_id,
        refs=tuple((str(channel), str(reference)) for channel, reference in moved),
        keep_minimum=False,
        at=at,
    )


Listener = Callable[[RecordEvent], Awaitable[None]]


class RecordListeners:
    """Who is told of the records: the channels that subscribed, in the order they did. With
    nobody subscribed, telling does nothing."""

    def __init__(self) -> None:
        self._listeners: list[Listener] = []

    def subscribe(self, listener: Listener) -> None:
        """``listener`` is told of every notice from now on."""
        self._listeners.append(listener)

    async def emit(self, event: RecordEvent) -> None:
        """Tell every listener of ``event``, once its gesture is committed. A listener that fails
        is logged by the type of its error alone (what it says may name someone), undoes nothing,
        and the next one is told all the same."""
        for listener in tuple(self._listeners):
            try:
                await listener(event)
            except Exception as error:
                logger.error("A listener of the records failed (%s)", type(error).__name__)
