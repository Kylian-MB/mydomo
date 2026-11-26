"""The events of the conversation, and what it keeps, in the JSON of the channel of the API.

The core gives a turn's events in data (``conversation.events``); the channel says each one in
words, in the texts of the person (``texts.view.person_texts``: never a header of the request), on
its stream (``channels.sse``), each event's data one JSON object:

* ``Words``: ``words`` ``{"text"}``, the plain text the core gives;
* ``Acted``: ``acted`` ``{"status", "sentence", "once", "request"}``. ``sentence`` is the card in
  words (``sentence_of``, ``conversation.cards.card_sentence``); for an action that asked for a
  yes, or that managed a request, the request's card as it is now, in words (``request_sentence``:
  its state and what it does, or the head Domo wrote for the person). ``once`` is what is shown
  only once (``once_of``: a link, or a code), in this event and nowhere else. ``request`` tells the
  client how a request that waits is answered (``request_of``): ``{"id", "answer_with",
  "proofs"}``: the client answers a yes in words by sending a message, a proof through
  ``POST /confirmations/{id}/prove``;
* ``Decided`` (the person's words answered a request): ``acted`` too, its ``status`` what the
  decision came to, its ``sentence`` the card of the decision (``decision_sentence``: what a
  yes in words still needs is said without the card or the button the page shows);
* a card that, on the page, sends the person to the button under it (``CARDS_IN_WORDS``) is said
  by a sentence of its own that says where they find it (``in_words``);
* ``Failed``: ``failed`` ``{"reason", "sentence"}``, the sentence of ``chat.error.<reason>``;
* ``Ended``: ``ended`` ``{"status"}``, after which the stream ends.

A step this channel cannot take (a vault form, which needs a safe screen: the key of a model) is
said by the sentence of its card, and nothing else of it is given: its address is not.

``stored_messages`` gives the conversation as it is kept (``GET /since``): each message with its
cards in words, a request's as it is now (as the page reads it).
"""

import json
import logging
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Final, cast

from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.channels.api.auth import API
from domo_core.channels.sse import ClientGone, RelaySink
from domo_core.chat.agent import ActionRecord
from domo_core.chat.models import Message, MessageAction
from domo_core.chat.store import ConversationStore
from domo_core.confirmations.routes import ConfirmationCard, card_after_decision, card_now
from domo_core.confirmations.service import (
    DecideOutcome,
    confirmations_by_id,
    may_decide,
    may_see,
)
from domo_core.confirmations.store import approver_of, ask_of
from domo_core.conversation.cards import card_sentence, shown_actions
from domo_core.conversation.channel import ListenerGone
from domo_core.conversation.events import Acted, Decided, Ended, Event, Failed, Words
from domo_core.db import SessionFactory
from domo_core.llm.types import JsonObject
from domo_core.proofs.kinds import ProofPlace
from domo_core.proofs.service import offers
from domo_core.texts.view import Texts
from domo_core.tools.model_key import key_form_of

logger = logging.getLogger(__name__)

# Where a proof is asked on this channel: no passkey (it is bound to the page).
API_PLACE: Final = ProofPlace(API)


# The cards whose sentence, on the page, sends the person to the button under them: here, where
# there is none, each is said by a sentence of its own (``.in_words``) that says where they find it.
CARDS_IN_WORDS: Final = frozenset(
    {
        "action.card.done.model.rekey.on_request",
        "action.card.done.model.rekey.yours",
        "action.card.done.key.add.on_request",
        "action.card.done.key.add.yours",
        "action.card.done.key.admin.on_request",
        "action.card.done.key.admin.yours",
        "action.card.done.key.add.check",
    }
)


def in_words(sentence: str, texts: Texts) -> str:
    """``sentence`` as this channel says it: each sentence of ``CARDS_IN_WORDS`` it holds, in
    ``texts``, replaced by its own sentence in words. A request's card, or a decision's, is said
    from the page's card (``confirmations.routes``): its sentence is changed here, once said."""
    for key in CARDS_IN_WORDS:
        page_sentence = texts(key)
        if page_sentence and page_sentence in sentence:
            sentence = sentence.replace(page_sentence, texts(f"{key}.in_words"))
    return sentence


def sentence_of_card(card: Mapping[str, object], texts: Texts) -> str | None:
    """A card in words (``card_sentence``), as this channel says it (``in_words``)."""
    sentence = card_sentence(card, texts)
    return None if sentence is None else in_words(sentence, texts)


def sentence_of(record: ActionRecord, texts: Texts) -> str:
    """The card of an action in words; what its status says when the card cannot be read."""
    return sentence_of_card(record.card, texts) or texts(f"action.card.{record.status}")


def once_of(card_once: Mapping[str, object] | None, texts: Texts) -> JsonObject | None:
    """What is shown only once (``ToolOutcome.card_once``) as the channel gives it: ``{"kind":
    "link" | "code", "value", "sentence"}``. ``None`` for nothing, for a part whose sentence
    cannot be read, and for a vault form (``key_form_of``): this channel opens no safe screen."""
    if card_once is None or key_form_of(card_once) is not None:
        return None
    sentence = card_sentence(card_once, texts)
    if sentence is None:
        return None
    link = card_once.get("link")
    if isinstance(link, str) and link.startswith(("https://", "http://")):
        return {"kind": "link", "value": link, "sentence": sentence}
    params = card_once.get("params")
    code = cast(dict[str, object], params).get("code") if isinstance(params, dict) else None
    if isinstance(code, str) and code:
        return {"kind": "code", "value": code, "sentence": sentence}
    return None


def request_sentence(card: ConfirmationCard) -> str:
    """A request's card in words: its state, then the head Domo wrote for the reader or what
    it does, and the note on it."""
    parts = (
        None if card.action is None else card.action.text,
        card.head or card.description,
        card.note,
    )
    return " ".join(part for part in parts if part)


async def request_of(
    session: AsyncSession, confirmation_id: uuid.UUID, viewer: Person, now: datetime
) -> JsonObject | None:
    """How the request ``confirmation_id`` that waits is answered, for ``viewer``: ``{"id",
    "answer_with", "proofs"}``. ``answer_with`` is what it asks of them when they may answer it
    (``ask_of``: ``yes``, a yes in words; ``explicit_yes``, a firm yes in words; ``proof``, one of
    ``proofs``, the kinds this channel offers them), or whose yes it waits for (``approver_of``:
    ``adult``, ``owner``, ``requester`` for the yes of the one who asked, or ``concerned`` for
    the yes of the person it concerns). ``None`` for a
    request that no longer waits, or that they may not see. A request whose row cannot be read
    (``ValueError``: a database edited by hand) has no ``answer_with``: logged by its kind, it
    never ends a stream."""
    c = (await confirmations_by_id(session, [confirmation_id])).get(confirmation_id)
    if c is None or c.status != "pending" or not may_see(c, viewer, now):
        return None
    try:
        if may_decide(c, viewer, now):
            answer_with = ask_of(c) or "yes"
        else:
            approver = approver_of(c)
            answer_with = (
                # The person it concerns answers it alone (never "the one who asked").
                "concerned"
                if c.concerned_id is not None
                else "requester"
                if approver == "self"
                else approver
            )
    except ValueError as error:
        logger.error("A request could not be read (%s)", type(error).__name__)
        return {"id": str(c.id), "answer_with": None, "proofs": []}
    proofs = list(await offers(session, viewer, API_PLACE, now)) if answer_with == "proof" else []
    return {"id": str(c.id), "answer_with": answer_with, "proofs": proofs}


# The notes of a decision that, on the page, point at its card or its button: here, where there is
# neither, what the request still needs is said in words (``.in_words``).
_NOTES_IN_WORDS: Final = frozenset({"needs_explicit", "needs_proof"})


def decision_sentence(card: ConfirmationCard, texts: Texts) -> str:
    """What a decision came to, in words, from the card that answers it
    (``card_after_decision``): the request's state, or what came of it, and the note on the
    outcome, in ``texts``; a yes that did not give what the request needs says what it needs
    without the card or the button the page shows."""
    note = card.note
    if card.outcome in _NOTES_IN_WORDS:
        note = texts(f"confirmation.outcome.{card.outcome}.in_words")
    parts = (None if card.action is None else card.action.text, note)
    return in_words(" ".join(part for part in parts if part), texts)


class Render:
    """Each event of a turn as the channel gives it: its name and its data. The cards are said
    for ``person``, in ``texts``, at the time of ``clock``."""

    def __init__(
        self,
        *,
        person: Person,
        texts: Texts,
        session_factory: SessionFactory,
        clock: Callable[[], datetime],
    ) -> None:
        self._person = person
        self._texts = texts
        self._session_factory = session_factory
        self._clock = clock

    async def event(self, event: Event) -> tuple[str, JsonObject]:
        match event:
            case Words(text=text):
                return "words", {"text": text}
            case Acted(record=record):
                return "acted", await self._acted(record)
            case Decided(confirmation_id=confirmation_id, record=record, outcome=outcome):
                return "acted", await self._decided(confirmation_id, record, outcome)
            case Failed(reason=reason):
                return "failed", {"reason": reason, "sentence": self._texts(f"chat.error.{reason}")}
            case Ended(status=status):
                return "ended", {"status": status}

    async def _acted(self, record: ActionRecord) -> JsonObject:
        sentence = sentence_of(record, self._texts)
        request: JsonObject | None = None
        if record.confirmation_id is not None:
            now = self._clock()
            card = await card_now(
                self._session_factory,
                record.confirmation_id,
                viewer=self._person,
                texts=self._texts,
                now=now,
                record=record,
            )
            if card is not None:
                sentence = in_words(request_sentence(card), self._texts) or sentence
            async with self._session_factory() as session:
                request = await request_of(session, record.confirmation_id, self._person, now)
        return {
            "status": record.status,
            "sentence": sentence,
            "once": once_of(record.card_once, self._texts),
            "request": request,
        }

    async def _decided(
        self, confirmation_id: uuid.UUID, record: ActionRecord | None, outcome: DecideOutcome
    ) -> JsonObject:
        now = self._clock()
        card = await card_after_decision(
            self._session_factory,
            confirmation_id,
            outcome=outcome,
            record=record,
            viewer=self._person,
            texts=self._texts,
            now=now,
        )
        async with self._session_factory() as session:
            request = await request_of(session, confirmation_id, self._person, now)
        return {
            "status": outcome,
            "sentence": decision_sentence(card, self._texts),
            "once": None if record is None else once_of(record.card_once, self._texts),
            "request": request,
        }


def as_data(data: JsonObject) -> str:
    """The data of an event: one line of JSON."""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


class ApiListener:
    """The ``conversation.channel.Listener`` of a reply the channel streams: each event, said by
    ``render``, goes to ``relay``, which the device's connection reads while it is there."""

    def __init__(self, relay: RelaySink, render: Render) -> None:
        self._relay = relay
        self._render = render

    @property
    def reading(self) -> bool:
        return self._relay.reading

    async def open(self) -> None:
        try:
            await self._relay.open()
        except ClientGone as gone:
            raise ListenerGone from gone

    async def emit(self, event: Event) -> None:
        name, data = await self._render.event(event)
        try:
            await self._relay.emit(name, as_data(data))
        except ClientGone as gone:
            raise ListenerGone from gone


async def _card_of(
    session_factory: SessionFactory,
    action: MessageAction,
    *,
    viewer: Person,
    texts: Texts,
    now: datetime,
) -> str:
    """A stored card in words. For an action that asked for a yes, or that managed a request,
    the request's card as it is now for ``viewer``, as the page and an ``acted`` event say it
    (``request_sentence``); otherwise, or when the request cannot be read, the card as it was
    stored, and what its status says when that cannot be read either."""
    stored = sentence_of_card(action.summary, texts) or texts(f"action.card.{action.status}")
    if action.confirmation_id is None:
        return stored
    card = await card_now(
        session_factory, action.confirmation_id, viewer=viewer, texts=texts, now=now
    )
    if card is None:
        return stored
    return in_words(request_sentence(card), texts) or stored


async def stored_messages(
    session_factory: SessionFactory,
    messages: Sequence[Message],
    *,
    viewer: Person,
    texts: Texts,
    now: datetime,
) -> list[JsonObject]:
    """``messages`` as the conversation keeps them (oldest first): ``{"id", "role", "text",
    "cards"}``, the cards of a reply's actions in words, in order, each request's as it is now
    (``_card_of``; what is shown only once was never kept), none for an action that is never shown
    (``policy.catalog.SILENT_ACTIONS``) nor for a failed attempt the same reply made good
    (``conversation.cards.shown_actions``, spec 6.7)."""
    async with session_factory() as session:
        actions = await ConversationStore(session).actions_of(
            m.id for m in messages if m.role == "assistant"
        )
    kept: list[JsonObject] = []
    for m in messages:
        cards = [
            await _card_of(session_factory, action, viewer=viewer, texts=texts, now=now)
            # What is never shown (Domo's memory), and an attempt made good, has no card.
            for action in shown_actions(actions.get(m.id, []))
        ]
        kept.append(
            {"id": str(m.id), "role": m.role, "text": m.content, "cards": cast(list[object], cards)}
        )
    return kept
