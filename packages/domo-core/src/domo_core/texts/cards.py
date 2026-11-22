"""The head of a card, written on the moment for each of its readers (spec 4.4).

The top of a card in the chat (a request that waits for a yes, or the card of an action done,
refused or that failed) shows what the action does, who asks what, how to answer, what changed, a
remark and what Domo could not tell: parts of the texts (``confirmations.routes``,
``conversation.execution``). When Domo chooses it (``Choices.write_now``: ``requests``,
``done``), the writer writes them again as one free head for each reader, which takes their place:

* the head of reference of a reader (``CardFacts.reference``) is what their card shows today in
  English (``REFERENCE``) for those parts, one per line (``head_parts`` of the card), with what
  someone else wrote left as holes: the reason of the judge or of Domo (``{reason}``), the
  question of the rights (``{question}``), a note's words (``{words}``; ``{words_before}``, those
  of a note before a change) and, on the card of an action, its dates (``{expires}``...), said in
  the reader's words. The writer and the checker never read them: the code puts them in once the
  head is checked (``write_card_head``);
* the head is checked by the code against the exact facts of the action (``checks.check_head``):
  each name of the people it is about (who asked, who may say yes, the people of its facts, the
  names its facts give as text) and each amount of its arguments that the reference shows is in
  it (``card_facts``: a name or an amount the reference does not show is not asked of it). Then the
  checker reads it (``writer.head_written``);
* a head kept is kept with its card and its reader (``card_texts``), with a line of the journal
  (``texts.written``, of the group ``card``), and with the fingerprint of what it was written from
  (``head_fingerprint``: the reference, the words of its holes and the reader's language). It is
  shown at each display (``head_of``, ``heads_of``) while its card still has that fingerprint
  for its reader (``card_fingerprint``): a request that changes as it waits (another yes asked,
  its arguments, the one it waited for joined), or a reader who reads another language now, shows
  the parts of the texts until a head is written again. A head refused in the end, by the code or
  the checker, is journaled with why
  (``texts.refused``, the key ``card.head``), never with its text nor the words of a hole. A writer
  that does not answer, no model or no key for the reader journals nothing;
* it is written on the reader's keys (``scheduler.TextWriting.write_head``): for the one whose
  turn it is, in their turn, ``CARD_HEAD_SECONDS`` at most (``conversation.turn``); for anyone else
  who
 reads the card, in the background. A reader with no key they may use has no head: their card
  shows the parts of the texts.

A card whose head is not written (not yet, refused, or no key) shows those parts in the reader's
texts: where Domo wrote none for them, the default phrases of their language, else the English
ones (``meanings.default_text``), never a sign in the place of a text.

The yes runs the action the request stores, never the words of its head (spec 6).
"""

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Protocol
from uuid import UUID

from domo_common.text_rules import HOLE
from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.audit.log import append_audit
from domo_core.confirmations.store import approves
from domo_core.db import SessionFactory
from domo_core.policy.catalog import ACTION_FACTS
from domo_core.texts.checks import numbers_in
from domo_core.texts.models import CardText
from domo_core.texts.view import fill_holes
from domo_core.texts.writer import ForWhom, TextModels, head_written

if TYPE_CHECKING:
    from domo_core.channels.page.cards import ActionCard
    from domo_core.confirmations.routes import ConfirmationCard

# How long the turn of the one who asks waits for their head, in seconds: after that, the card
# goes out with the parts of the texts (the writing goes on, and is kept for the next display).
CARD_HEAD_SECONDS: Final = 8
# The group a head is journaled under (``texts.written``), and the key of its refusal
# (``texts.refused``).
HEAD_GROUP: Final = "card"
REFUSED_KEY: Final = "card.head"
# The holes of a head that hold someone else's words.
REASON: Final = "reason"
QUESTION: Final = "question"
WORDS: Final = "words"
WORDS_BEFORE: Final = "words_before"

# A person as the facts of an action name them.
_PERSON: Final = re.compile(
    r"person:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
)
_CENT: Final = Decimal("0.01")


def hole(name: str) -> str:
    """The hole ``name`` as a text holds it: ``{name}``."""
    return f"{{{name}}}"


@dataclass(frozen=True)
class CardOf:
    """Which card a head is of: a request's (``confirmation_id``), or an action's
    (``message_action_id``), never both."""

    confirmation_id: UUID | None = None
    message_action_id: UUID | None = None


def _nothing() -> Mapping[str, str]:
    return MappingProxyType({})


@dataclass(frozen=True)
class HeadSource:
    """What a card holds for its head (``card_facts``), besides the parts it shows: who asked
    (``asked_by``: the one whose turn made the card of an action), who may say yes
    (``approver_kind``, for a request), the type of the action (``action_type``), its ``target``
    and its ``facts`` as the request keeps them (people as ``person:<uuid>``), the names the card
    gives as text (``names``: those of the card of an action), the ``numbers`` of the arguments,
    the facts and the card (any value: those that are no number are passed over), and the words
    of each hole (``holes``, by name)."""

    asked_by: UUID | None = None
    approver_kind: str | None = None
    action_type: str | None = None
    target: str | None = None
    facts: Mapping[str, object] = field(default_factory=_nothing)
    names: tuple[str, ...] = ()
    numbers: tuple[object, ...] = ()
    holes: Mapping[str, str] = field(default_factory=_nothing)


@dataclass(frozen=True)
class CardFacts:
    """What the head of a card is written from, for one reader (``card_facts``): ``reference``,
    the English head this reader sees today, holes left as ``{reason}``, ``{question}``,
    ``{words}``; ``names``, the display names of the requester, the approvers and every
    ``person:<uuid>`` of the facts (and the names the facts give), those the reference shows;
    ``amounts``, the amounts of the arguments, as digits; ``holes``, the words of each hole,
    filled by the code once the head is checked."""

    reference: str
    names: tuple[str, ...]
    amounts: tuple[str, ...]
    holes: Mapping[str, str]

    def fingerprint(self, language: str) -> str:
        """The fingerprint of a head written from these facts for a reader of ``language``
        (``head_fingerprint``)."""
        return head_fingerprint(self.reference, self.holes, language)


@dataclass(frozen=True)
class KeptHead:
    """A head kept for a reader (``head_of``): its ``text``, and the fingerprint of what it was
    written from (``facts_hash``)."""

    text: str
    facts_hash: str

    def shown_on(self, card: "ConfirmationCard | ActionCard", language: str) -> str | None:
        """The head, when ``card`` (the reader's card built for its head, ``holed``) still has
        the fingerprint it was written from for a reader of ``language``; else ``None``: the card
        shows the parts of the texts."""
        return self.text if card_fingerprint(card, language) == self.facts_hash else None


def head_fingerprint(reference: str, holes: Mapping[str, str], language: str) -> str:
    """The fingerprint of a head: of its reference, the words of its holes and the language of
    its reader (sha256, hex)."""
    told = json.dumps([language, reference, sorted(holes.items())], ensure_ascii=False)
    return hashlib.sha256(told.encode("utf-8")).hexdigest()


class _HasHead(Protocol):
    @property
    def source(self) -> HeadSource | None: ...

    @property
    def head_parts(self) -> tuple[str | None, ...]: ...


def _people_of(source: HeadSource, people: Mapping[UUID, Person]) -> list[UUID]:
    """The people the card is about, by id: who asked, who may say yes, and the people of its
    target and facts."""
    found: list[UUID] = []
    if source.asked_by is not None:
        found.append(source.asked_by)
        if source.approver_kind is not None:
            found.extend(
                person.id
                for person in people.values()
                if approves(source.approver_kind, person, source.asked_by)
            )
    for value in (source.target, *source.facts.values()):
        if isinstance(value, str):
            found.extend(UUID(match) for match in _PERSON.findall(value))
    return found


def _given_names(source: HeadSource) -> list[str]:
    """The names the card gives as text: its facts of the kind ``text`` (an invitee's name, a
    successor's, who gave a key), and its own."""
    kinds = ACTION_FACTS.get(source.action_type or "", {})
    named = [
        value
        for name, value in source.facts.items()
        if kinds.get(name) == "text" and isinstance(value, str)
    ]
    return [*named, *source.names]


def _amount_forms(value: object) -> list[str]:
    """How a number may be shown on a card, in digits: as it is (``5``, ``0.5``), or as an amount
    of money is (``5.00``, ``0.50``, every digit of it kept)."""
    if isinstance(value, bool):
        return []
    if isinstance(value, str):
        return [value] if value.isascii() and value.isdigit() else []
    if not isinstance(value, int | float):
        return []
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return []
    if not number.is_finite():
        return []
    plain = format(number.normalize(), "f")
    exponent = number.normalize().as_tuple().exponent
    money = number.quantize(_CENT) if isinstance(exponent, int) and exponent > -2 else number
    return list(dict.fromkeys((plain, format(money, "f"))))


def card_facts(
    card: "ConfirmationCard | ActionCard", *, reader: Person, people: Mapping[UUID, Person]
) -> CardFacts:
    """What the head of ``card`` is written from, for ``reader``: ``card`` is the card as
    ``reader`` sees it, built in the reference texts with its holes (``confirmation_card`` and
    ``action_card`` with ``holed``), ``people`` the people of the household (by id). Its
    reference is the parts its head takes the place of, one per line. A name is the display name
    of someone the card is about, or a name its facts give (``_people_of``, ``_given_names``); an
    amount a number of its arguments and its facts, in digits (``_amount_forms``). Of them, only
    those the reference shows are asked of the head: the writer is asked to say what the card
    says, and no more. ``ValueError`` for a card built without its source."""
    reference, holes, source = _reference_of(card)
    # What the reference shows of the facts, someone else's words left out.
    shown = HOLE.sub(" ", reference)
    folded = shown.casefold()
    candidates = [
        people[person].display_name for person in _people_of(source, people) if person in people
    ]
    candidates.extend(_given_names(source))
    names = tuple(
        dict.fromkeys(name for name in candidates if name.strip() and name.casefold() in folded)
    )
    numbers = numbers_in(shown)
    amounts: list[str] = []
    for value in source.numbers:
        form = next(
            (form for form in _amount_forms(value) if re.sub(r"[^0-9]", "", form) in numbers),
            None,
        )
        if form is not None:
            amounts.append(form)
    return CardFacts(reference, names, tuple(dict.fromkeys(amounts)), holes)


def _reference_of(
    card: "ConfirmationCard | ActionCard",
) -> tuple[str, Mapping[str, str], HeadSource]:
    """The reference of ``card`` (built for its head: ``holed``), the words of the holes it
    holds, and its source. ``ValueError`` for a card built without its source."""
    held: _HasHead = card
    source = held.source
    if source is None:
        raise ValueError("a card made for its head has its source")
    reference = "\n".join(part for part in held.head_parts if part)
    holes = {name: words for name, words in source.holes.items() if hole(name) in reference}
    return reference, MappingProxyType(holes), source


def card_fingerprint(card: "ConfirmationCard | ActionCard", language: str) -> str:
    """The fingerprint ``card`` (built for its head: ``holed``) gives a head written for a reader
    of ``language`` (``head_fingerprint``)."""
    reference, holes, _ = _reference_of(card)
    return head_fingerprint(reference, holes, language)


def _of(card: CardOf) -> ColumnElement[bool]:
    if card.confirmation_id is not None:
        return CardText.confirmation_id == card.confirmation_id
    return CardText.message_action_id == card.message_action_id


async def head_of(
    session: AsyncSession,
    *,
    confirmation_id: UUID | None,
    message_action_id: UUID | None,
    reader_id: UUID,
) -> KeptHead | None:
    """The head kept for ``reader_id`` on the card of the request ``confirmation_id``, or of the
    action ``message_action_id``, with what it was written from; ``None`` when there is none.
    Reads only."""
    card = CardOf(confirmation_id, message_action_id)
    if (confirmation_id is None) == (message_action_id is None):
        raise ValueError("a head is of a request or of an action")
    found = await session.execute(
        select(CardText.head, CardText.facts_hash).where(_of(card), CardText.reader_id == reader_id)
    )
    row = found.one_or_none()
    return None if row is None else KeptHead(row.head, row.facts_hash)


async def heads_of(
    session: AsyncSession,
    *,
    reader_id: UUID,
    confirmation_ids: Iterable[UUID] = (),
    message_action_ids: Iterable[UUID] = (),
) -> dict[UUID, KeptHead]:
    """The heads kept for ``reader_id`` on the cards of these requests and these actions, by the
    id of the card (a request's or an action's), in one query. Reads only."""
    requests, actions = set(confirmation_ids), set(message_action_ids)
    if not requests and not actions:
        return {}
    rows = await session.execute(
        select(
            CardText.confirmation_id,
            CardText.message_action_id,
            CardText.head,
            CardText.facts_hash,
        ).where(
            CardText.reader_id == reader_id,
            or_(CardText.confirmation_id.in_(requests), CardText.message_action_id.in_(actions)),
        )
    )
    found: dict[UUID, KeptHead] = {}
    for confirmation_id, message_action_id, head, facts_hash in rows.all():
        card_id = confirmation_id if confirmation_id is not None else message_action_id
        if card_id is not None:
            found[card_id] = KeptHead(head, facts_hash)
    return found


async def write_card_head(
    models: TextModels,
    session_factory: SessionFactory,
    *,
    card: CardOf,
    reader: Person,
    for_whom: ForWhom,
    facts: CardFacts,
    clock: Callable[[], datetime],
) -> str | None:
    """The head of ``card`` written for ``reader`` (``for_whom``) on ``models`` from ``facts``
    (``writer.head_written``), its holes then filled by the code, kept in ``card_texts`` in the
    place of the one before, with its fingerprint (``facts`` for ``for_whom.language``) and its
    line of the journal (``texts.written``), at the time ``clock`` gives. ``None`` when none could
    be written: a head refused in the end is journaled with why (``texts.refused``), a writer that
    did not answer is not. Whoever asks looks first for a head kept from these facts
    (``scheduler.TextWriting``): this writes one."""
    fingerprint = facts.fingerprint(for_whom.language)
    written = await head_written(
        models,
        reference=facts.reference,
        names=facts.names,
        amounts=facts.amounts,
        holes=tuple(facts.holes),
        for_whom=for_whom,
    )
    if written.head is None:
        if written.refused is not None:
            async with session_factory() as session:
                await append_audit(
                    session,
                    actor="system",
                    action="texts.refused",
                    details={"key": REFUSED_KEY, "why": written.refused},
                )
                await session.commit()
        return None
    head = fill_holes(written.head, facts.holes)
    kept_now = {
        "head": head,
        "facts_hash": fingerprint,
        "model_id": written.model_id,
        "written_at": clock(),
    }
    async with session_factory() as session:
        # The head written before, from what the card said then, gives way to this one.
        await session.execute(
            insert(CardText)
            .values(
                confirmation_id=card.confirmation_id,
                message_action_id=card.message_action_id,
                reader_id=reader.id,
                **kept_now,
            )
            .on_conflict_do_update(constraint="uq_card_texts_card_reader", set_=kept_now)
        )
        # Last, just before the commit: the audit log holds a lock until the transaction ends.
        await append_audit(
            session,
            actor="system",
            action="texts.written",
            details={
                "person": str(reader.id),
                "language": for_whom.language,
                "group": HEAD_GROUP,
                "count": 1,
                "model": written.model_id,
            },
        )
        await session.commit()
    return head
