"""Whoever listens to a person now, and how what Domo says on his own reaches them (spec 6.6).

A channel that a person keeps open (a stream of the text program, say) opens a listener for them
(``Listeners.open``, with what the channel can do), and closes it when it goes. A turn that Domo
starts himself (a wake, an arrival, a news turn: ``conversation.starts``) has no channel that asked
for it: it runs with ``Fanout``, which gives what it says to every listener of the person open at
that moment that hears what Domo says unasked (``Abilities.hears_unasked``). The reply itself is
kept once, as any other, whoever hears it: a channel that opens later finds it there. A channel
that needs to know which reply an event belongs to (to find it again among the stored messages)
opens with ``Listeners.open_hearing``: it is given each event with the reply's id.

A listener opened with the name of its channel (and its device, if any) notes what reached the
person (``deliveries``, spec 6.10): a reply whose end it was given reached them whole (``live``),
one it was given a part of and then closed or dropped, cut. The notes are written apart
(``deliveries.service.DeliveryNotes``): they never slow nor stop what Domo says.

What is shown only once, or a request that waits for a yes, is never given out so: a turn Domo
starts runs as if nobody listened (``NOBODY_LISTENS``), so that nothing is made to be shown once,
and the card of a request that waits stays under the reply, for a channel that can show it when
the person asks. A listener that cannot receive any more (``ListenerGone``, or any failure) is
dropped, and the others go on: the turn is never stopped by one of them.

One process: the listeners are in its memory (``app.state.listeners``), like the replies being
written.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from uuid import UUID

from domo_core.conversation.channel import Abilities, Listener, ListenerGone
from domo_core.conversation.events import Acted, Ended, Event
from domo_core.deliveries.models import Extent
from domo_core.deliveries.service import DeliveryNotes, Reached

logger = logging.getLogger(__name__)

# What hears what Domo says on his own: each event, with the id of the reply it belongs to.
# Raises ``ListenerGone`` when nobody can receive any more.
Hear = Callable[[UUID, Event], Awaitable[None]]


@dataclass(frozen=True, eq=False)
class _Open:
    """One listener open for a person, with what its channel can do, the name of its channel and
    its device (none: what reaches it is not noted), and the replies it was given a part of and
    not the end yet (``begun``)."""

    hear: Hear
    abilities: Abilities
    channel: str | None
    device_id: UUID | None
    begun: set[UUID] = field(default_factory=lambda: set[UUID]())


class Listeners:
    """The listeners open in the application, by person (see the module docstring). ``notes``
    writes what reached them (none: nothing is noted)."""

    def __init__(self, notes: DeliveryNotes | None = None) -> None:
        self._open: dict[UUID, list[_Open]] = {}
        self._notes = notes

    def open(
        self,
        person_id: UUID,
        listener: Listener,
        abilities: Abilities,
        *,
        channel: str | None = None,
        device_id: UUID | None = None,
    ) -> Callable[[], None]:
        """``listener`` listens to ``person_id`` from now on, on a channel that can do
        ``abilities``: it is ready to receive, and is given each event alone. What reaches it is
        noted with ``channel`` and ``device_id`` (see the module docstring). What closes it (once
        is enough)."""

        async def hear(_reply_id: UUID, event: Event) -> None:
            await listener.emit(event)

        return self.open_hearing(person_id, hear, abilities, channel=channel, device_id=device_id)

    def open_hearing(
        self,
        person_id: UUID,
        hear: Hear,
        abilities: Abilities,
        *,
        channel: str | None = None,
        device_id: UUID | None = None,
    ) -> Callable[[], None]:
        """``hear`` listens to ``person_id`` from now on, on a channel that can do ``abilities``:
        it is given each event with the id of the reply it belongs to. What reaches it is noted
        with ``channel`` and ``device_id`` (see the module docstring). What closes it (once is
        enough): a reply it was given a part of, and not the end, reached them cut."""
        entry = _Open(hear, abilities, channel, device_id)
        self._open.setdefault(person_id, []).append(entry)

        def close() -> None:
            self._drop(person_id, entry)

        return close

    def hearing(self, person_id: UUID) -> bool:
        """Whether a listener of ``person_id`` that hears what Domo says unasked is open."""
        return any(entry.abilities.hears_unasked for entry in self._open.get(person_id, ()))

    async def say(self, person_id: UUID, reply_id: UUID, event: Event) -> None:
        """Give ``event``, of the reply ``reply_id``, to every listener of ``person_id`` open now
        that hears what Domo says unasked, in the order they were opened. One that cannot
        receive any more is dropped (a failure other than ``ListenerGone`` is logged, by its
        kind), and the others are given it all the same. Never raises."""
        for entry in list(self._open.get(person_id, ())):
            if not entry.abilities.hears_unasked:
                continue
            try:
                await entry.hear(reply_id, event)
            except ListenerGone:
                self._drop(person_id, entry)
                continue
            except Exception as error:
                logger.error("A listener failed and was dropped (%s)", type(error).__name__)
                self._drop(person_id, entry)
                continue
            if isinstance(event, Ended):
                entry.begun.discard(reply_id)
                self._note(person_id, entry, reply_id, "whole")
            else:
                entry.begun.add(reply_id)

    def _drop(self, person_id: UUID, entry: _Open) -> None:
        entries = self._open.get(person_id)
        if entries is None or entry not in entries:
            return
        entries.remove(entry)
        if not entries:
            del self._open[person_id]
        # What it was given a part of, and will never be given the end of.
        for reply_id in sorted(entry.begun):
            self._note(person_id, entry, reply_id, "cut")
        entry.begun.clear()

    def _note(self, person_id: UUID, entry: _Open, reply_id: UUID, extent: Extent) -> None:
        """Note that ``reply_id`` reached ``person_id`` through ``entry``, live, ``extent``:
        apart, never raising; nothing for a listener opened without the name of its channel."""
        if self._notes is None or entry.channel is None:
            return
        self._notes.note(
            Reached(
                message_id=reply_id,
                person_id=person_id,
                channel=entry.channel,
                device_id=entry.device_id,
                how="live",
                extent=extent,
            )
        )


def _given_out(event: Event) -> bool:
    """Whether a turn Domo started gives ``event`` to the person's listeners: everything but the
    card of a request that waits for a yes, or of what would be shown once (none is made in such a
    turn), which wait for a channel that can show them."""
    if isinstance(event, Acted):
        return event.record.status != "waiting" and event.record.card_once is None
    return True


class Fanout(Listener):
    """The listener of a turn Domo starts for ``person_id`` (``conversation.starts``), which
    writes the reply ``reply_id``: nobody asked for it, so nobody reads it as it comes
    (``reading`` is false), and what it gives goes to the person's open listeners that hear what
    Domo says unasked (``Listeners.say``); to nobody without ``listeners``. It never raises
    ``ListenerGone``: the turn always ends, and its reply is kept."""

    def __init__(self, listeners: Listeners | None, person_id: UUID, reply_id: UUID) -> None:
        self._listeners = listeners
        self._person_id = person_id
        self._reply_id = reply_id

    @property
    def reading(self) -> bool:
        return False

    async def open(self) -> None:
        return None

    async def emit(self, event: Event) -> None:
        if self._listeners is not None and _given_out(event):
            await self._listeners.say(self._person_id, self._reply_id, event)
