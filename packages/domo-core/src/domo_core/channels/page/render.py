"""The page as a channel: what it can do, and the events of a turn in the form it shows them.

The core gives a turn's events in data (``conversation.events``); the page turns each into what it
showed before there was a core apart, on its stream (``channels.sse``):

* ``Words`` are a ``delta`` event, HTML-escaped: what the model writes is not trusted, and the page
  adds it to the bubble as text;
* ``Acted`` is an ``action`` event: the card of the action (``fragments/action_card.html``, with
  what it shows only once and the head Domo wrote for the person), or, for an action that waits
  for a yes or that managed a request, the request's card as it is now
  (``fragments/confirmation_card.html``, with the proof it offers where this page can give it);
* ``Decided`` is an ``action`` event too: the card of the request the person's words just
  decided, with what came of it;
* ``Failed`` is an ``error`` event: the sentence of the failure and, for a key the provider
  refuses, the "Enter the key again" button, for the one who gave it (``channels.page.key_routes``):
  with a key that does not work, Domo cannot answer, so they could not ask him for a new form;
* ``Ended`` is ``done``, which is also what the page closes the stream on. Right before it, when
  the name the person calls Domo by is no longer the one their page was given (the turn gave
  him one, or called him Domo again: ``tools.profile.DomoName``, which is never shown), a
  ``frame`` event gives the page's new brand, title and placeholder, as a JSON object of plain
  texts (``frame_of``), which chat.js puts in place as text: the page follows the name without
  a reload (2026-11-16, the owner's trial of 0.9.0).

``cards_of`` gives the cards of the stored replies, as the chat page shows them.
"""

import html
import json
import logging
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Final

from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.accounts.passkeys import PasskeySite
from domo_core.channels.page.cards import ActionCard, action_card
from domo_core.channels.page.proof import proof_offers_of
from domo_core.channels.sse import ClientGone, RelaySink
from domo_core.chat.agent import ActionRecord
from domo_core.chat.models import Message, MessageAction
from domo_core.chat.store import ConversationStore
from domo_core.confirmations.describe import (
    draft_lines_of,
    step_lines_of,
    successor_lines_of,
    waiting_names_of,
)
from domo_core.confirmations.models import Confirmation
from domo_core.confirmations.routes import (
    ConfirmationCard,
    card_after_decision,
    card_now,
    changers_of,
    confirmation_card,
    not_yours_card,
)
from domo_core.confirmations.service import (
    confirmations_by_id,
    may_see,
    predecessors_of,
    request_state,
)
from domo_core.conversation.cards import shown_actions
from domo_core.conversation.channel import ListenerGone
from domo_core.conversation.events import Acted, Decided, Ended, Event, Failed, Words
from domo_core.db import SessionFactory
from domo_core.llm.types import JsonObject
from domo_core.naming import name_shown
from domo_core.policy.open_catalog import load_policy
from domo_core.policy.rights import load_rights
from domo_core.policy.settings import PolicySettings, load_policy_settings
from domo_core.texts.cards import KeptHead, head_of, heads_of
from domo_core.texts.view import Texts
from domo_core.tools.model_key import KEY_FORM_PATH
from domo_core.web.templating import render_fragment

logger = logging.getLogger(__name__)

# The card of an action whose request a yes runs, or ran without what came of it being recorded.
_RUNNING_CARD: Final[JsonObject] = {"key": "action.card.running", "params": {}}


class PageListener:
    """The ``conversation.channel.Listener`` of a page's stream: each event, rendered as the page
    shows it, goes to ``relay``, which the page's connection reads while it is there. The cards
    are rendered for ``person``, in the texts of their page (``texts``), at the time of ``clock``,
    with the proofs this page can give (``site``)."""

    def __init__(
        self,
        relay: RelaySink,
        *,
        person: Person,
        domo: str,
        texts: Texts,
        templates: Jinja2Templates,
        session_factory: SessionFactory,
        clock: Callable[[], datetime],
        site: PasskeySite | None,
    ) -> None:
        self._relay = relay
        self._person = person
        # The name for Domo the page was given (``_follow_the_name``).
        self._domo = domo
        self._texts = texts
        self._templates = templates
        self._session_factory = session_factory
        self._clock = clock
        self._site = site

    @property
    def reading(self) -> bool:
        return self._relay.reading

    async def open(self) -> None:
        try:
            await self._relay.open()
        except ClientGone as gone:
            raise ListenerGone from gone

    async def emit(self, event: Event) -> None:
        match event:
            case Words(text=text):
                name, data = "delta", html.escape(text)
            case Acted(record=record):
                name, data = "action", await self._action_html(record)
            case Decided(confirmation_id=confirmation_id, record=record, outcome=outcome):
                card = await card_after_decision(
                    self._session_factory,
                    confirmation_id,
                    outcome=outcome,
                    record=record,
                    viewer=self._person,
                    texts=self._texts,
                    now=self._clock(),
                    site=self._site,
                )
                name, data = "action", self._fragment("confirmation_card.html", card=card)
            case Failed():
                name, data = "error", self._error_html(event)
            case Ended():
                await self._follow_the_name()
                name, data = "done", ""
        try:
            await self._relay.emit(name, data)
        except ClientGone as gone:
            raise ListenerGone from gone

    async def _follow_the_name(self) -> None:
        """When the person's name for Domo changed since their page was given it (``frame``):
        the new brand, title and placeholder of the page. A failure to read it is logged and
        leaves the page as it is (a reload shows the name)."""
        try:
            async with self._session_factory() as session:
                calls_domo = await session.scalar(
                    select(Person.calls_domo).where(Person.id == self._person.id)
                )
                house_name = (await load_policy_settings(session)).house_domo_name
        except Exception as error:
            logger.error("The name for the assistant could not be read (%s)", type(error).__name__)
            return
        now = name_shown(calls_domo, house_name)
        if now == self._domo:
            return
        self._domo = now
        try:
            await self._relay.emit("frame", frame_of(now, self._texts))
        except ClientGone as gone:
            raise ListenerGone from gone

    def _fragment(self, template: str, **context: object) -> str:
        return render_fragment(self._templates, f"fragments/{template}", self._texts, **context)

    async def _action_html(self, record: ActionRecord) -> str:
        """The card of an action (with its ``card_once``); for an action that waits for a yes, or
        that managed a request, the request's card as it is now. Its head is the one Domo wrote
        for the person, if he did (``conversation.turn`` has it written first)."""
        if record.confirmation_id is not None:
            request_card = await card_now(
                self._session_factory,
                record.confirmation_id,
                viewer=self._person,
                texts=self._texts,
                now=self._clock(),
                record=record,
                site=self._site,
            )
            if request_card is not None:
                return self._fragment("confirmation_card.html", card=request_card)
        head = await self._action_head(record)
        card = action_card(
            tool_name=record.tool_name,
            status=record.status,
            card=record.card,
            card_once=record.card_once,
            texts=self._texts,
            # An action of this person's own turn, with no request of someone else behind it.
            for_requester=True,
            head=head,
        )
        return self._fragment("action_card.html", action=card)

    async def _action_head(self, record: ActionRecord) -> KeptHead | None:
        """The head Domo wrote for the person of the card of an action of the turn (none for a
        request's, nor when there is none). A failure to read it is logged (its kind only): the
        card shows its sentence."""
        if record.action_id is None or record.confirmation_id is not None:
            return None
        try:
            async with self._session_factory() as session:
                return await head_of(
                    session,
                    confirmation_id=None,
                    message_action_id=record.action_id,
                    reader_id=self._person.id,
                )
        except Exception as error:
            logger.error("The head of a card could not be read (%s)", type(error).__name__)
            return None

    def _error_html(self, failed: Failed) -> str:
        """What the reply's note says of a failure (escaped), with the "Enter the key again"
        button for a key the provider refuses, when the person may give it again."""
        text = html.escape(self._texts(f"chat.error.{failed.reason}"))
        if failed.reason != "auth" or failed.provider_id is None:
            return text
        url = KEY_FORM_PATH.format(provider_id=failed.provider_id)
        button = self._fragment("key_rekey.html", rekey_url=url, target="closest .key-rekey")
        return f"{text}{button}"


def frame_of(domo: str, texts: Texts) -> str:
    """The texts of a chat page that name Domo, for the name ``domo`` the reader sees, as the
    ``frame`` event gives them: a JSON object of plain texts (``brand``, ``title``,
    ``placeholder``), never HTML: the page puts them in as text."""
    return json.dumps(
        {
            "brand": domo,
            "title": f"{texts('chat.title')} - {domo}",
            "placeholder": texts("chat.input.placeholder", domo=domo),
        },
        ensure_ascii=False,
    )


async def cards_of(
    session: AsyncSession,
    person: Person,
    messages: Sequence[Message],
    texts: Texts,
    now: datetime,
    site: PasskeySite | None,
) -> dict[uuid.UUID, list[ActionCard | ConfirmationCard]]:
    """The cards of the actions of the replies among ``messages`` (oldest first), as ``person``
    sees them at ``now``, by reply (none for an action that is never shown,
    ``policy.catalog.SILENT_ACTIONS``, nor for a failed attempt the same reply made good,
    ``conversation.cards.shown_actions``): an action that asked for a yes shows its request, as
    it is now (or, under the reply that answered the person's words deciding it, what came of
    it). The buttons the steps of a chain left are on the request's own card: under the reply
    that answered the words, only when that card is not among these replies (a button is in one
    place on a page: pressed a second time, it would answer 410). A request that asks a proof
    offers the means the person has where their page was opened (``site``, F12)."""
    store = ConversationStore(session)
    stored = await store.actions_of(m.id for m in messages if m.role == "assistant")
    # What is never shown (Domo's memory, ``policy.catalog.SILENT_ACTIONS``), and a failed
    # attempt the same answer made good (spec 6.7), have no card (``shown_actions``).
    actions = {
        message_id: shown_ones
        for message_id, taken in stored.items()
        if (shown_ones := shown_actions(taken))
    }
    # An action that asked for a yes shows its request, as it is now.
    requests = await confirmations_by_id(
        session,
        (a.confirmation_id for taken in actions.values() for a in taken if a.confirmation_id),
    )
    settings = await load_policy_settings(session) if requests else PolicySettings({})
    rights, policy = await load_rights(session), await load_policy(session)
    replaced = await predecessors_of(session, requests)
    changers = await changers_of(session, requests.values(), replaced)
    drafts = await draft_lines_of(
        session, requests.values(), texts, name_shown(person.calls_domo, settings.house_domo_name)
    )
    successors = await successor_lines_of(session, requests.values(), texts, now)
    steps = await step_lines_of(session, requests.values(), texts, now)
    waiting = await waiting_names_of(session, requests.values())
    proofs = await proof_offers_of(session, requests.values(), person, site, now)
    # The heads Domo wrote for the person, of these requests and of the actions of their turns.
    heads = await heads_of(
        session,
        reader_id=person.id,
        confirmation_ids=requests,
        message_action_ids=(
            a.id for taken in actions.values() for a in taken if a.confirmation_id is None
        ),
    )
    # The person's message each reply answers.
    answering: dict[uuid.UUID, uuid.UUID | None] = {}
    last_person: uuid.UUID | None = None
    for message in messages:
        if message.role == "user":
            last_person = message.id
        else:
            answering[message.id] = last_person

    def decided_by_its_words(message_id: uuid.UUID, request_of: Confirmation) -> bool:
        """Whether the reply ``message_id`` answered the person's words that decided the request
        (a yes, a no, or a change Domo applied)."""
        return (
            request_of.decided_message_id is not None
            and request_of.decided_message_id == answering.get(message_id)
        )

    # The requests whose own card is among these replies (under the reply that presented them).
    with_own_card = {
        action.confirmation_id
        for message_id, taken in actions.items()
        for action in taken
        if action.confirmation_id is not None
        and (request_of := requests.get(action.confirmation_id)) is not None
        and not decided_by_its_words(message_id, request_of)
    }

    def shown(message_id: uuid.UUID, action: MessageAction) -> ActionCard | ConfirmationCard:
        request_of = requests.get(action.confirmation_id) if action.confirmation_id else None
        if request_of is None:
            # An action of the person's own turn: theirs.
            return action_card(
                tool_name=action.tool_name,
                status=action.status,
                card=action.summary,
                texts=texts,
                for_requester=True,
                head=heads.get(action.id),
            )
        if not may_see(request_of, person, now, rights=rights, policy=policy):
            return not_yours_card(request_of.id, texts)
        # The reply that answered the person's words deciding the request (a yes, a no, or a
        # change Domo applied) shows what came of it: the request's own card is under the
        # reply that presented it.
        if decided_by_its_words(message_id, request_of):
            state = request_state(request_of, settings, now)
            summary: Mapping[str, object] = action.summary
            if request_of.id in with_own_card:
                # The buttons of the steps are on the request's own card, on this page too.
                summary = {name: value for name, value in summary.items() if name != "buttons"}
            return action_card(
                tool_name=action.tool_name,
                status=state,
                # What came of it is not known yet: the stored card would say more than is known.
                card=_RUNNING_CARD if state == "running" else summary,
                texts=texts,
                for_requester=person.id == request_of.requester_id,
                # A button for the model's key that a step of a chain left opens the form for the
                # yes given to this request.
                confirmation_id=request_of.id,
            )
        return confirmation_card(
            request_of,
            viewer=person,
            settings=settings,
            texts=texts,
            now=now,
            rights=rights,
            policy=policy,
            replaced=replaced.get(request_of.id),
            draft=drafts.get(request_of.id),
            successors=successors.get(request_of.id),
            steps=steps.get(request_of.id, ()),
            waiting_for=waiting.get(request_of.id),
            changed_by=changers.get(request_of.id),
            proof=proofs.get(request_of.id),
            head=heads.get(request_of.id),
        )

    def reply_cards(
        message_id: uuid.UUID, taken: list[MessageAction]
    ) -> list[ActionCard | ConfirmationCard]:
        """The cards of a reply's actions: one card per request (a reply that made a new request
        from a change stores two actions with it, the waiting one and the change)."""
        cards: list[ActionCard | ConfirmationCard] = []
        requests_shown: set[uuid.UUID] = set()
        for action in taken:
            if action.confirmation_id is not None:
                if action.confirmation_id in requests_shown:
                    continue
                requests_shown.add(action.confirmation_id)
            cards.append(shown(message_id, action))
        return cards

    return {message_id: reply_cards(message_id, taken) for message_id, taken in actions.items()}
