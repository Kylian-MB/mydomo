"""When the heads of the cards are asked (spec 4.4): what ``texts.cards`` leaves to its callers.

Domo writes on the moment what he chose to (``Choices.write_now``): the requests that wait for a
yes (``requests``), and the cards of what he did, done, refused or that failed (``done``). Each
head is written by ``scheduler.TextWriting`` on its reader's keys; this module says for whom, from
what (``head_facts_of``), and when:

* a request is asked for each of its readers, the one who asked and whoever may answer it, as soon
  as it waits for a yes (``write_request_heads``): from the turn that shows it (the person of the
  turn waits ``cards.CARD_HEAD_SECONDS`` at most for theirs, ``conversation.turn``), and in the
  background from every decision that leaves it waiting, for another yes or again
  (``ask_request_heads``: a button, a proof, someone's words, an arrival,
  ``conversation.starts``);
* a page that shows a request that waits with no head for its reader (none kept, or one written
  from what the card said before) asks theirs in the background (``ask_missing_heads``: the chat
  page, the approvals zone), unless one is being written for them or was left unwritten lately
  (``TextWriting.asked_lately``): the next display shows it;
* the card of an action of a turn is written for the person of the turn, ``CARD_HEAD_SECONDS`` at
  most (``write_action_head``).

Nothing here raises: what fails is logged by its kind, and the card shows the parts of the texts.
"""

import logging
import uuid
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.channels.page.cards import action_card
from domo_core.chat.agent import ActionRecord
from domo_core.confirmations.describe import waiting_names_of
from domo_core.confirmations.routes import ConfirmationCard, changers_of, confirmation_card
from domo_core.confirmations.service import (
    confirmations_by_id,
    is_past_lifetime,
    may_decide,
    predecessors_of,
)
from domo_core.db import SessionFactory
from domo_core.llm.choices import load_choices
from domo_core.policy.open_catalog import load_policy
from domo_core.policy.rights import load_rights
from domo_core.policy.settings import load_policy_settings
from domo_core.texts.cards import CardFacts, CardOf, KeptHead, card_facts, head_of
from domo_core.texts.scheduler import TextWriting
from domo_core.texts.view import REFERENCE, Texts

logger = logging.getLogger(__name__)

# What Domo may write on the moment (``Choices.write_now``): the requests that wait for a yes,
# and the cards of what he did (an action done, refused or that failed).
WRITE_REQUESTS: Final = "requests"
WRITE_DONE: Final = "done"
WRITTEN_AS_DONE: Final = frozenset({"done", "refused", "failed"})


async def head_facts_of(
    session: AsyncSession, confirmation_id: uuid.UUID, *, now: datetime
) -> list[tuple[Person, CardFacts]]:
    """What the head of the request ``confirmation_id`` is written from for each of its readers
    while it waits for a yes (``texts.cards.card_facts``): the one who asked first, then whoever
    may answer it, each with their card built in the reference texts, someone else's words left
    as holes. None when it does not wait. Reads only."""
    found = (await confirmations_by_id(session, [confirmation_id])).get(confirmation_id)
    if found is None or found.status != "pending":
        return []
    settings = await load_policy_settings(session)
    if is_past_lifetime(found, settings, now):
        return []
    replaced = await predecessors_of(session, [confirmation_id])
    waiting = await waiting_names_of(session, [found])
    changers = await changers_of(session, [found], replaced)
    people = {person.id: person for person in (await session.execute(select(Person))).scalars()}
    rights, policy = await load_rights(session), await load_policy(session)
    readers = [people[found.requester_id]] if found.requester_id in people else []
    readers.extend(
        person
        for person in people.values()
        if person.id != found.requester_id
        and may_decide(found, person, now, rights=rights, policy=policy)
    )
    facts: list[tuple[Person, CardFacts]] = []
    for reader in readers:
        card = confirmation_card(
            found,
            viewer=reader,
            settings=settings,
            texts=REFERENCE,
            now=now,
            rights=rights,
            policy=policy,
            replaced=replaced.get(confirmation_id),
            waiting_for=waiting.get(confirmation_id),
            changed_by=changers.get(confirmation_id),
            holed=True,
        )
        facts.append(
            (reader, card_facts(card, reader=reader, people=people, rights=rights, policy=policy))
        )
    return facts


async def write_request_heads(
    writing: TextWriting | None,
    session_factory: SessionFactory,
    confirmation_id: uuid.UUID,
    *,
    now: datetime,
    wait_for: uuid.UUID | None = None,
    only: uuid.UUID | None = None,
) -> None:
    """When Domo writes the requests that wait for a yes on the moment, have the head of the
    request ``confirmation_id`` written for each of its readers (``only``: for that one alone),
    in the background, on their keys (``TextWriting.write_head``); for ``wait_for`` (the person
    of a turn), wait ``CARD_HEAD_SECONDS`` at most (``TextWriting.head_now``). Nothing for a
    request that no longer waits, nor without ``writing``."""
    if writing is None:
        return
    try:
        async with session_factory() as session:
            if WRITE_REQUESTS not in (await load_choices(session, None)).write_now:
                return
            readers = await head_facts_of(session, confirmation_id, now=now)
        card = CardOf(confirmation_id=confirmation_id)
        readers = [(reader, facts) for reader, facts in readers if only in (None, reader.id)]
        for reader, facts in readers:
            if reader.id != wait_for:
                writing.write_head(card, reader, facts)
        for reader, facts in readers:
            if reader.id == wait_for:
                await writing.head_now(card, reader, facts)
    except Exception as error:
        # The kind of error only: what the database said is not logged.
        logger.error("The heads of a request could not be asked (%s)", type(error).__name__)


def ask_request_heads(
    writing: TextWriting | None,
    session_factory: SessionFactory,
    confirmation_id: uuid.UUID,
    *,
    clock: Callable[[], datetime],
    only: uuid.UUID | None = None,
) -> None:
    """``write_request_heads`` in the background (one at a time for a request and ``only``), at
    the time ``clock`` gives then: after a decision that leaves the request waiting, or for a
    page that shows it with no head for its reader. It returns at once."""
    if writing is None:
        return

    async def ask() -> None:
        await write_request_heads(writing, session_factory, confirmation_id, now=clock(), only=only)

    writing.in_background((confirmation_id, only), ask)


def ask_missing_heads(
    writing: TextWriting | None,
    session_factory: SessionFactory,
    cards: Iterable[object],
    reader: Person,
    *,
    clock: Callable[[], datetime],
) -> None:
    """For each card of ``cards`` that ``reader`` sees of a request that waits for a yes, without
    a head (none kept, or one written from what the card said before), have theirs asked in the
    background (``ask_request_heads``), unless one is being written for them or was left
    unwritten lately (``TextWriting.asked_lately``). It returns at once."""
    if writing is None:
        return
    for card in cards:
        if not isinstance(card, ConfirmationCard) or card.status != "pending" or card.head:
            continue
        confirmation_id = uuid.UUID(card.id)
        if writing.asked_lately(CardOf(confirmation_id=confirmation_id), reader.id):
            continue
        ask_request_heads(writing, session_factory, confirmation_id, clock=clock, only=reader.id)


async def write_action_head(
    writing: TextWriting | None,
    session_factory: SessionFactory,
    record: ActionRecord,
    *,
    reader: Person,
    texts: Texts,
) -> KeptHead | None:
    """When Domo writes the cards of what he did on the moment, the head of the card of
    ``record``, an action of a turn done, refused or that failed (stored, with no request behind
    it), written for ``reader`` (the person of the turn, whose texts are ``texts``) on their keys,
    ``CARD_HEAD_SECONDS`` at most; ``None`` when there is none (the card shows its sentence)."""
    if (
        writing is None
        or record.action_id is None
        or record.confirmation_id is not None
        or record.status not in WRITTEN_AS_DONE
    ):
        return None
    try:
        async with session_factory() as session:
            if WRITE_DONE not in (await load_choices(session, None)).write_now:
                return None
            rights, policy = await load_rights(session), await load_policy(session)
        reference = action_card(
            tool_name=record.tool_name,
            status=record.status,
            card=record.card,
            texts=REFERENCE,
            for_requester=True,
            holed=texts,
        )
        people: Mapping[uuid.UUID, Person] = {reader.id: reader}
        facts = card_facts(reference, reader=reader, people=people, rights=rights, policy=policy)
        await writing.head_now(CardOf(message_action_id=record.action_id), reader, facts)
        async with session_factory() as session:
            return await head_of(
                session,
                confirmation_id=None,
                message_action_id=record.action_id,
                reader_id=reader.id,
            )
    except Exception as error:
        logger.error("The head of a card could not be written (%s)", type(error).__name__)
        return None


class CardHeadWriting:
    """The heads of the cards a turn shows, written as this module says, by ``writing`` on the
    readers' keys, read in ``session_factory``: what the conversation is given to write them
    (``conversation.turn.CardHeads``)."""

    def __init__(self, writing: TextWriting, session_factory: SessionFactory) -> None:
        self._writing = writing
        self._session_factory = session_factory

    async def of_request(
        self, confirmation_id: uuid.UUID, *, now: datetime, wait_for: uuid.UUID
    ) -> None:
        await write_request_heads(
            self._writing, self._session_factory, confirmation_id, now=now, wait_for=wait_for
        )

    async def of_action(self, record: ActionRecord, *, reader: Person, texts: Texts) -> None:
        await write_action_head(
            self._writing, self._session_factory, record, reader=reader, texts=texts
        )

    def ask_for_request(self, confirmation_id: uuid.UUID, *, clock: Callable[[], datetime]) -> None:
        ask_request_heads(self._writing, self._session_factory, confirmation_id, clock=clock)


def card_head_writing(
    writing: TextWriting | None, session_factory: SessionFactory
) -> CardHeadWriting | None:
    """The heads of the cards a turn shows, written by ``writing``; none without it."""
    return None if writing is None else CardHeadWriting(writing, session_factory)
