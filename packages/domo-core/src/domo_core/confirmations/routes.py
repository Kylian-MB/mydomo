"""The confirmation card, and its two buttons: ``POST /confirmations/{id}/approve`` and
``POST /confirmations/{id}/refuse``; and the zone where an adult sees the requests of the
children, and the owner those that wait for the owner too (``GET /approvals``).

The buttons are htmx requests from the chat page (the security middleware checks their Origin,
as for every request that changes something). Each answer is the card again, as it is after the
decision, with ``data-outcome`` saying what the decision came to (``already_decided``,
``expired``, ``approver_changed`` when the request now waits for someone else's yes...): htmx
puts it in the place of the card that was clicked. A request the person may
not decide gets a card that says only that (``not_yours``), with nothing of the request in it.
A button that decided something (``conversation.starts.WAKE_OUTCOMES``) also wakes Domo, in
the background, in the conversation of the person who asked (``Conversation.wake_after_decision``):
he tells them what came of it, if that helps, whether their page is open or not.

The card (``fragments/confirmation_card.html``) shows the request in its current state: an
action card (``channels.page.cards.action_card``) that says it waits for a yes (for a chain that
waits for it again once the person it waited for joined, a note that says who joined), or, for a
chain paused until someone's arrival, whom it waits for (``waiting_names_of``, spec 4.3b), or what
came of it (with, on the card of the one who asked only, the button that shows the link or opens the
form that someone else's yes left for them; for a chain, one button per step done that left one,
numbered, read from its rows, whatever the state of the request); what it would do, in a plain
sentence (``describe_for_people``), and for a change of rights the rights and the examples of its
draft (``draft_lines_of``), for a change of the list of successors that waits the list now and as
it would be (``successor_lines_of``), for a chain of actions each of its steps, in order, with what
came of it (``step_lines_of``); what it was before, when someone asked for a change, and who
(``confirmation.changed``: "as you asked" to whoever changed it; to anyone else, by the one who
asked, an adult or the owner, ``changers_of``); while it waits, who asks what
(``confirmation.asked.*``: Domo, with his reason, a rule of the owner, what the owner decided by
default, or whose agreement the rights ask for; to the owner, in his own words); and, for the
person who may answer it, what Domo could not tell when a limit sent it to the owner
(``confirmation.ask_reason``), how to answer (``confirmation.hint.*``, which follows what the
request asks) and the buttons (no "Confirm" for a proof: it is given in its own frame).

While the request waits for a yes, the parts of its card that say what it does, who asks what,
how to answer, what changed, the remark and what Domo could not tell give way to the head Domo
wrote for its reader, when one is kept (``ConfirmationCard.head``, ``texts.cards``): every builder
of a card reads it (``head_of``, ``heads_of``). Its state, and the rest of the card, come from the
texts. Each reader's head is written from their card built in the reference texts, someone
else's words left as holes (``confirmation_card`` with ``holed``, ``texts.card_heads``); the
approvals zone asks the heads it shows without one.

The approvals zone (``fragments/approvals.html``) is a fragment of the chat page, which asks for it
again every 15 seconds. For an adult or the owner it lists the children's requests that wait for an
adult's yes, and for the owner also the requests whose yes a right gives to the owner
(``awaiting_approval``), oldest first, each as who asks and the same card, with the same two
buttons: the first adult or owner who decides wins, and the others find the request already
answered. A request the one who asked nudged (``nudged_at``, spec 5.2) comes first, and says so.
For anyone else it is empty.

The proof (``proofs.service``, ``channels.page.proof``)
-----------------------------------
A request that asks a proof offers, to the person who may answer it, the means they have where
their page was opened (``ConfirmationCard.proof``, computed by every builder of a card: the answer
to a decision, the card of the stream, the cards of the chat page and of the approvals zone, F12):
their passkey on a page that can use passkeys (``passkey_availability_of``), and their password
always. ``POST /confirmations/{id}/proof/options`` gives the options of the passkey's ceremony
(JSON, for ``static/passkeys.js``); ``POST /confirmations/{id}/prove`` takes the proof (``means``,
and ``credential`` or ``password``), checked by the code, and answers with the card as a button
does (a proof that holds wakes Domo too, ``via`` ``proof``). What the person gives there is never
part of the conversation: Domo never sees it.
"""

import math
import re
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Annotated, ClassVar, Final, Literal, cast

from domo_common.meanings import has_meaning
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse, Response

from domo_core.accounts.account_routes import get_password_check_throttle
from domo_core.accounts.models import Account, Person
from domo_core.accounts.passkey_routes import get_passkey_options_throttle
from domo_core.accounts.passkeys import PasskeyRejected, PasskeySite, passkey_availability_of
from domo_core.accounts.passwords import Argon2Busy, busy_headers
from domo_core.accounts.throttle import LoginThrottle
from domo_core.channels.page.abilities import PAGE
from domo_core.channels.page.cards import ActionCard, CardButton, action_card, card_buttons
from domo_core.channels.page.proof import ProofOffer, page_place, proof_offer, proof_offers_of
from domo_core.channels.page.speaker import page_speaker
from domo_core.chat.agent import ActionRecord
from domo_core.config import CoreSettings
from domo_core.confirmations.describe import (
    DraftLines,
    StepLine,
    SuccessorLines,
    description_before_the_change,
    domo_question_of,
    draft_lines_of,
    note_words_of,
    request_description,
    step_lines_of,
    successor_lines_of,
    waiting_names_of,
    what_domo_could_not_tell,
    words_before_the_change,
)
from domo_core.confirmations.models import Confirmation
from domo_core.confirmations.service import (
    MAX_APPROVALS_SHOWN,
    NOT_SURE_REASON,
    DecideOutcome,
    awaiting_approval,
    confirmations_by_id,
    decide_confirmation,
    is_past_lifetime,
    may_decide,
    may_see,
    predecessors_of,
    requester_context,
)
from domo_core.confirmations.store import (
    action_type_of,
    approves,
    ask_of,
    asked_by_of,
    effective_arguments,
    requester_of,
)
from domo_core.conversation.service import Conversation
from domo_core.gatekeeper_client import GatekeeperClient
from domo_core.llm.factory import ModelFactory
from domo_core.llm.listing import ModelLister
from domo_core.policy.asks import Ask
from domo_core.policy.catalog import PolicyTables
from domo_core.policy.roles import Actor, as_role
from domo_core.policy.settings import PolicySettings, load_policy_settings
from domo_core.proofs.kinds import PROOF_KINDS
from domo_core.proofs.service import ProofHeldBack, prove
from domo_core.texts import cards
from domo_core.texts.cards import HeadSource, KeptHead, head_of, heads_of
from domo_core.texts.view import REFERENCE, Texts
from domo_core.tools.context import InternalTool, Notify, ToolContext
from domo_core.web.deps import (
    SessionFactory,
    current_person,
    get_clock,
    get_gatekeeper,
    get_model_factory,
    get_model_lister,
    get_policy,
    get_session,
    get_session_factory,
    get_settings,
    get_streaming_replies,
    get_tool_registry,
    policy_now,
)
from domo_core.web.language import request_texts
from domo_core.web.templating import render

router = APIRouter()

CONFIRMATIONS_PATH: Final = "/confirmations"
APPROVALS_PATH: Final = "/approvals"

_CONFIRMATION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

# The outcomes that say something of their own on the card, beside the request's state.
_OUTCOME_NOTES: Final = frozenset(
    {"already_decided", "expired", "needs_explicit", "needs_proof", "proof_failed", "not_yours"}
)


@dataclass(frozen=True)
class ConfirmationCard:
    """A confirmation as ``fragments/confirmation_card.html`` shows it.

    ``status`` is the request's state as the person should understand it (a request past its
    lifetime is ``expired``, whatever the row says yet), or ``None`` on a card that must tell
    nothing about the request (``not_yours``). ``outcome`` is the outcome of the decision this
    card answers, if any. ``action`` is the action card inside: the state, or what came of it.
    ``changed`` says what the request was before the person asked for a change, if they did.
    ``ask`` is what it asks (``yes``, ``explicit_yes``, ``proof``, or ``None``), and ``asked``
    the sentence that says who asks it, while it waits. ``hint`` says how to answer;
    ``can_decide`` shows the buttons. ``question``
    says what Domo could not tell, when a limit sent the request to the owner. ``note`` is a
    sentence about the outcome. ``draft`` is, for a change of rights, the rights and the examples
    of its draft, and ``successors`` is, for a change of the list of successors that waits, the
    list now and as it would be. ``steps`` are, for a chain of actions, its steps, in order.
    A chain paused until someone's arrival (``waiting_arrival``) says whom it waits for, in
    ``action``; one that waits for a yes again once they joined says who joined, in ``note``.
    ``proof`` is the proof the card offers, for a request that asks one, to the person who may
    answer it (``channels.page.proof.ProofOffer``: the means, and where the card posts them).

    ``head`` is the head Domo wrote for the reader (``texts.cards``), shown while the request
    waits in the place of what it does, who asks what, how to answer, what changed, the remark
    and the question (``head_parts``; a note on the outcome of a decision stays: it is its
    state). ``source`` is what a card built for the writing of a head holds of the request
    (``texts.cards.HeadSource``).
    """

    kind: ClassVar[str] = "confirmation"

    id: str
    status: str | None
    outcome: str | None = None
    action: ActionCard | None = None
    description: str | None = None
    changed: str | None = None
    ask: str | None = None
    asked: str | None = None
    can_decide: bool = False
    hint: str | None = None
    question: str | None = None
    note: str | None = None
    draft: DraftLines | None = None
    successors: SuccessorLines | None = None
    steps: tuple[StepLine, ...] = ()
    proof: ProofOffer | None = None
    head: str | None = None
    source: HeadSource | None = None

    @property
    def head_parts(self) -> tuple[str | None, ...]:
        """What the head of the card takes the place of, in the order its reference says them:
        what it does, who asks what, how to answer, what changed, the remark and what Domo could
        not tell."""
        return (self.description, self.asked, self.hint, self.changed, self.note, self.question)


def _step_buttons(
    c: Confirmation, steps: tuple[StepLine, ...], viewer: Person
) -> tuple[CardButton, ...]:
    """What the steps done of the chain ``c`` left for the one who asked, each behind its own
    button (``StepLine.button``, from the rows: spec 4.5), for ``viewer`` when they are the one
    who asked; nobody else is given them. A key's button names the request: the form opens for the
    yes given to it."""
    if viewer.id != requester_of(c):
        return ()
    return card_buttons([step.button.as_json() for step in steps if step.button is not None], c.id)


def _state_card(
    c: Confirmation,
    status: str,
    texts: Texts,
    record: ActionRecord | None,
    viewer: Person,
    *,
    buttons: tuple[CardButton, ...],
    steps_done: bool,
    waiting_for: str | None = None,
) -> ActionCard:
    """The action card inside: the request's state, or what came of it (``_state_of``; a chain
    with ``steps_done`` that stopped says that only its rest was not done). What someone else's
    yes left for the one who asked to do (a link to show, a form to open) is on their card only.
    So are the ``buttons`` the steps done of a chain left for them (``_step_buttons``): they are on
    the card whatever the state of the request, so that a step done keeps its button when the
    chain waits again for someone else's yes, pauses until someone's arrival (the one who asked
    sends them their link from there: spec 4.3b), runs, stops, is refused, expires or is replaced
    (spec 4.5)."""
    return replace(
        _state_of(c, status, texts, record, viewer, steps_done=steps_done, waiting_for=waiting_for),
        buttons=buttons,
    )


# The states in which nothing more of a request is done: a chain with steps done says that only its
# rest was not done (``confirmation.card.<state>.rest``), as its steps done stay done.
_STOPPED: Final = frozenset({"refused", "expired", "replaced"})
# Whom a paused chain waits for, when the name of its invitation cannot be read (it always can:
# an invitation is never deleted).
_UNKNOWN_NAME: Final = "…"


def _state_of(
    c: Confirmation,
    status: str,
    texts: Texts,
    record: ActionRecord | None,
    viewer: Person,
    *,
    steps_done: bool,
    waiting_for: str | None = None,
) -> ActionCard:
    """The action card of the request's state, or of what came of it, without the buttons of the
    steps of a chain (``_state_card`` puts them on every one). ``steps_done``: the request is a
    chain some of whose steps were done; ``waiting_for``: the name of the invitation a chain
    paused until someone's arrival waits for."""
    match status:
        case "pending":
            # The requester's own yes is "your yes" to them only: to anyone else who sees the
            # card (the one whose yes it waited for before, Ruling R27), it is the requester's.
            waits_for = (
                "requester"
                if c.approver_kind == "self" and viewer.id != requester_of(c)
                else c.approver_kind
            )
            return ActionCard("waiting", c.tool_name, texts(f"action.card.waiting.{waits_for}"))
        case "executing":
            # A yes runs it, or ran it and what came of it could not be recorded.
            return ActionCard("running", c.tool_name, texts("action.card.running"))
        case "waiting_arrival":
            # A chain that did its steps up to one that waits for someone's arrival.
            name = _UNKNOWN_NAME if waiting_for is None else waiting_for
            return ActionCard(
                "waiting", c.tool_name, texts("action.card.waiting_arrival", name=name)
            )
        case "executed" | "failed":
            shown = "done" if status == "executed" else "failed"
            return action_card(
                tool_name=c.tool_name,
                status=shown,
                card=c.result or {},
                texts=texts,
                card_once=None if record is None else record.card_once,
                for_requester=viewer.id == requester_of(c),
                confirmation_id=c.id,
            )
        case "replaced" if c.replaced_by is None:
            # A chain taken up again whose rest ran at once: no new request replaced it.
            return ActionCard("refused", c.tool_name, texts("confirmation.card.replaced.at_once"))
        case _:
            # refused, expired, replaced: nothing was done, or, for a chain with steps done,
            # nothing more (those stay done: the card does not say "not done").
            rest = ".rest" if steps_done and status in _STOPPED else ""
            return ActionCard("refused", c.tool_name, texts(f"confirmation.card.{status}{rest}"))


# Who changed a request, as its card tells someone else than them: the one who asked, an adult
# (the approver of a child's request) or the owner.
Changer = Literal["requester", "adult", "owner"]


def _changer_id(c: Confirmation, replaced: Confirmation | None) -> uuid.UUID | None:
    """Who changed the request ``c``, if someone did: whoever's change ran on it
    (``amended_arguments``, ``decided_by``), or whoever's change made it the new request that
    replaced ``replaced`` (its ``decided_by``, decided by a change). A new request that replaced
    ``replaced`` otherwise is the one who asked's: a chain they took up again
    (``tools.chain.ChainContinue``), whose ``decided_by`` is whoever said yes to it before."""
    if c.amended_arguments is not None:
        return c.decided_by
    if replaced is None:
        return None
    return replaced.decided_by if replaced.decided_via == "change" else c.requester_id


async def changers_of(
    session: AsyncSession,
    requests: Iterable[Confirmation],
    replaced: Mapping[uuid.UUID, Confirmation],
) -> dict[uuid.UUID, tuple[uuid.UUID, Changer]]:
    """For each of ``requests`` that someone changed (``replaced``: the requests they replaced,
    ``predecessors_of``), who did, by request id: their id, and what they are to the one who asked
    (``requester``; else ``owner`` or ``adult``, by their role now). Reads only."""
    found: dict[uuid.UUID, tuple[uuid.UUID, uuid.UUID]] = {}
    for c in requests:
        changer = _changer_id(c, replaced.get(c.id))
        if changer is not None:
            found[c.id] = (changer, c.requester_id)
    others = {changer for changer, requester in found.values() if changer != requester}
    roles: dict[uuid.UUID, str] = {}
    if others:
        result = await session.execute(select(Person.id, Person.role).where(Person.id.in_(others)))
        roles = {person_id: role for person_id, role in result.all()}

    def what(changer: uuid.UUID, requester: uuid.UUID) -> Changer:
        if changer == requester:
            return "requester"
        return "owner" if roles.get(changer) == "owner" else "adult"

    return {
        request_id: (changer, what(changer, requester))
        for request_id, (changer, requester) in found.items()
    }


def _changed_key(changed_by: tuple[uuid.UUID, Changer] | None, viewer: Person) -> str:
    """The sentence that says the request was changed, for ``viewer``: "as you asked" to whoever
    changed it (or when who did is not known: a request changed before this was kept), else by
    whom."""
    if changed_by is None or changed_by[0] == viewer.id:
        return "confirmation.changed"
    return f"confirmation.changed.by_{changed_by[1]}"


# The hint of each ask, for the one who asked: a yes in their own words, the button or "I
# confirm", or a proof.
_HINTS: Final[Mapping[Ask | None, str]] = {
    None: "confirmation.hint.natural",
    "yes": "confirmation.hint.natural",
    "explicit_yes": "confirmation.hint.explicit",
    "proof": "confirmation.hint.proof",
}
# The sentences that have a version for the owner, when he reads them (spec 5.7.3, F8).
_FOR_THE_OWNER: Final = frozenset(
    {
        "confirmation.asked.rule.yes",
        "confirmation.asked.rule.explicit_yes",
        "confirmation.asked.rule.proof",
        "confirmation.asked.approver.owner",
    }
)
# The sentences that have a version for the one who asked when someone else answers
# (``.others``): they say what is asked of the one who answers (spec 5.7.3).
_FOR_THE_ONE_WHO_ASKED: Final = frozenset({"confirmation.asked.rule.proof"})
# The agreement of an adult: whoever may give it reads that it is theirs (``.yours``).
_AN_ADULT_AGREES: Final = "confirmation.asked.approver.adult"


def _hint(c: Confirmation, viewer: Person, ask: Ask | None, texts: Texts) -> str:
    """How ``viewer`` answers the request. The requester's own words can answer it, as it asks
    (``ask``); someone who answers for them (an adult for a child, the owner) uses the buttons, or
    tells Domo in their own conversation (spec 5.1: Domo shows it to them there, and their
    words answer it, never the words of the one who asked); a proof is given on the card."""
    if viewer.id == requester_of(c) or ask == "proof":
        return texts(_HINTS[ask])
    return texts("confirmation.hint.approver")


def _asked(c: Confirmation, viewer: Person, texts: Texts, *, holed: bool = False) -> str | None:
    """Who asks what of the request ``c``, in one sentence for ``viewer``: Domo (or his judge),
    with his reason when he gave one; a rule of the owner; someone's agreement the rights ask
    for. Each reader reads what fits them (spec 5.7.3): to the
    owner, the sentences about him speak to him (``.yours``), and so does the agreement of an
    adult to whoever may give it; the one who asked, when someone else answers, reads whose yes
    or proof is asked (``.others``), never "your yes". ``None`` when it asks nothing (a chain the
    judge let through). With ``holed``, the reason is left as the hole ``{reason}``: someone
    else's words (``texts.cards``)."""
    ask, by = ask_of(c), asked_by_of(c)
    if ask is None or by is None:
        return None
    # The one who asked, when an adult or the owner answers: what is asked is theirs, not his.
    to_whom = (
        texts(f"confirmation.asked.to.{c.approver_kind}")
        if viewer.id == requester_of(c) and c.approver_kind in ("adult", "owner")
        else None
    )
    match by:
        case "domo" | "judge":
            key = f"confirmation.asked.domo.{ask}"
            others = {} if to_whom is None else {"to_whom": to_whom}
            if to_whom is not None:
                key = f"{key}.others"
            if c.ask_why:
                # The reason Domo gives when he asks a change again is a text of the page's
                # language, not the English one kept with the request.
                reason = (
                    texts("confirmation.asked.domo.not_sure")
                    if c.ask_why == NOT_SURE_REASON
                    else cards.hole(cards.REASON)
                    if holed
                    else c.ask_why
                )
                return texts(key, reason=reason, **others)
            return texts(f"{key}.plain", **others)
        case "rule":
            key = f"confirmation.asked.rule.{ask}"
        case "approver":
            if c.approver_kind not in ("adult", "owner"):
                return None
            key = f"confirmation.asked.approver.{c.approver_kind}"
    if to_whom is not None and key in _FOR_THE_ONE_WHO_ASKED:
        return texts(f"{key}.others", to_whom=to_whom)
    speaks_to_them = (viewer.role == "owner" and key in _FOR_THE_OWNER) or (
        key == _AN_ADULT_AGREES and approves("adult", viewer, requester_of(c))
    )
    if speaks_to_them:
        key = f"{key}.yours"
    return texts(key) if has_meaning(key) else None


def confirmation_card(
    c: Confirmation,
    *,
    viewer: Person,
    settings: PolicySettings,
    texts: Texts,
    now: datetime,
    outcome: DecideOutcome | None = None,
    record: ActionRecord | None = None,
    note: str | None = None,
    replaced: Confirmation | None = None,
    draft: DraftLines | None = None,
    successors: SuccessorLines | None = None,
    steps: tuple[StepLine, ...] = (),
    waiting_for: tuple[str, bool] | None = None,
    changed_by: tuple[uuid.UUID, Changer] | None = None,
    proof: ProofOffer | None = None,
    head: KeptHead | None = None,
    holed: bool = False,
) -> ConfirmationCard:
    """The card of ``c`` as ``viewer`` sees it now. ``outcome`` (and the ``record`` of the call
    that ran, whose ``card_once`` is shown this once) when it answers a decision; ``note`` says
    something more about it. ``replaced`` is the request ``c`` replaced, if a change made it a
    new one (``predecessors_of``): the card says what it was. ``draft`` is the draft of a change
    of rights (``draft_lines_of``), ``successors`` the lists of a change of the successors
    (``successor_lines_of``), ``steps`` the steps of a chain (``step_lines_of``) and
    ``waiting_for`` the name of the invitation a chain paused until someone's arrival waits for,
    and whether it was accepted (``waiting_names_of``): a chain that waits for a yes again once
    that person joined says so to whoever may give it, in its note (spec 4.3b). ``changed_by`` is
    who changed it, if someone did (``changers_of``): the card tells whoever else reads it by whom
    (an approver's change, spec 5.1). ``proof`` is the proof it offers ``viewer``
    (``channels.page.proof.proof_offers_of``), shown only while they may answer it.

    ``head`` is the head kept for ``viewer`` (``texts.cards.head_of``), shown while the request
    waits, and only while it still says what this card says to them in their language (its
    fingerprint, ``KeptHead.shown_on``: the card as its head is written from, ``holed``). With
    ``holed``, the card is the one a head is written from (``texts.card_heads``): in
    ``texts`` (the reference texts), someone else's words left as holes (``{reason}``,
    ``{question}``, ``{words}``, ``{words_before}``), with its ``source``."""
    status = "expired" if c.status == "pending" and is_past_lifetime(c, settings, now) else c.status
    can_decide = status == "pending" and may_decide(c, viewer, now)
    awaited, arrived = (None, False) if waiting_for is None else waiting_for
    ask = ask_of(c)
    if note is None and outcome in _OUTCOME_NOTES:
        note = texts(f"confirmation.outcome.{outcome}")
    elif note is None and outcome == "approver_changed":
        # Whose yes it waits for now (Ruling R27).
        note = texts(f"confirmation.outcome.approver_changed.{c.approver_kind}")
    elif note is None and outcome == "paused":
        # It waits for someone's arrival now (spec 4.3b).
        name = _UNKNOWN_NAME if awaited is None else awaited
        note = texts("confirmation.outcome.paused", name=name)
    elif note is None and can_decide and arrived and awaited is not None:
        # The one it waited for joined, and its rest waits for this yes: the one who gives it
        # sees who actually arrived (spec 4.3b).
        note = texts("confirmation.outcome.arrived", name=awaited)
    words = note_words_of(action_type_of(c), effective_arguments(c))
    words_before = words_before_the_change(c, replaced)
    before = description_before_the_change(
        c,
        replaced,
        texts,
        words=cards.hole(cards.WORDS_BEFORE) if holed and words_before is not None else None,
    )
    buttons = _step_buttons(c, steps, viewer)
    shown_head = None
    if head is not None and status == "pending" and not holed:
        written_from = confirmation_card(
            c,
            viewer=viewer,
            settings=settings,
            texts=REFERENCE,
            now=now,
            replaced=replaced,
            waiting_for=waiting_for,
            changed_by=changed_by,
            holed=True,
        )
        shown_head = head.shown_on(written_from, texts.language)
    if shown_head is not None and outcome is None:
        # The remark is the head's to say: only a note on the outcome of a decision stays.
        note = None
    return ConfirmationCard(
        id=str(c.id),
        status=status,
        outcome=outcome,
        action=_state_card(
            c,
            status,
            texts,
            record,
            viewer,
            buttons=buttons,
            steps_done=any(step.state == "done" for step in steps),
            waiting_for=awaited,
        ),
        description=request_description(
            c, texts, words=cards.hole(cards.WORDS) if holed and words is not None else None
        ),
        changed=(
            None if before is None else texts(_changed_key(changed_by, viewer), before=before)
        ),
        ask=ask,
        asked=_asked(c, viewer, texts, holed=holed) if status == "pending" else None,
        can_decide=can_decide,
        hint=_hint(c, viewer, ask, texts) if can_decide else None,
        question=(
            what_domo_could_not_tell(
                c, texts, question=cards.hole(cards.QUESTION) if holed else None
            )
            if can_decide
            else None
        ),
        note=note,
        draft=draft,
        # The list is the owner's to know: whoever else reads this card (the owner of before, now
        # an adult) is told what the request does, not what the list holds.
        successors=successors if viewer.role == "owner" else None,
        # The same for a step of a chain that changes the list.
        steps=steps
        if viewer.role == "owner"
        else tuple(replace(step, successors=None) for step in steps),
        proof=proof if can_decide else None,
        head=shown_head,
        source=_head_source(c, words=words, words_before=words_before) if holed else None,
    )


def _head_source(c: Confirmation, *, words: str | None, words_before: str | None) -> HeadSource:
    """What the card of ``c`` holds for the writing of its head (``texts.cards``): who asked, who
    may say yes, its action's type, target and facts, the values of its arguments and facts
    (its numbers among them), and the words of its holes: the reason of the judge or of Domo,
    the question of the rights, a note's words, now and before a change."""
    action = c.action
    facts_of = action.get("facts")
    facts = (
        {str(name): value for name, value in cast(dict[object, object], facts_of).items()}
        if isinstance(facts_of, dict)
        else {}
    )
    target = action.get("target")
    reason = c.ask_why if c.ask_why and c.ask_why != NOT_SURE_REASON else None
    holes = {
        cards.REASON: reason,
        cards.QUESTION: domo_question_of(c),
        cards.WORDS: words,
        cards.WORDS_BEFORE: words_before,
    }
    return HeadSource(
        asked_by=requester_of(c),
        approver_kind=c.approver_kind,
        action_type=action_type_of(c),
        target=target if isinstance(target, str) else None,
        facts=facts,
        numbers=(*effective_arguments(c).values(), *facts.values()),
        holes={name: shown for name, shown in holes.items() if shown is not None},
    )


def not_yours_card(confirmation_id: uuid.UUID, texts: Texts) -> ConfirmationCard:
    """The card of a request the person may not decide: the outcome, and nothing else."""
    return ConfirmationCard(
        id=str(confirmation_id),
        status=None,
        outcome="not_yours",
        note=texts("confirmation.outcome.not_yours"),
    )


async def card_now(
    session_factory: SessionFactory,
    confirmation_id: uuid.UUID,
    *,
    viewer: Person,
    texts: Texts,
    now: datetime,
    record: ActionRecord | None = None,
    site: PasskeySite | None = None,
) -> ConfirmationCard | None:
    """The card of this confirmation as ``viewer`` sees it now (``None`` if there is none), read
    in a short transaction of its own; ``record`` is the call that just ran it, if one did (its
    ``card_once`` is shown this once). A viewer who may not see the request (``may_see``) gets
    the card that tells nothing about it (``not_yours_card``), whatever named it. ``site`` is
    where the viewer's page uses passkeys (``passkey_availability_of``; ``None`` where it cannot):
    the proof the card offers follows it."""
    async with session_factory() as session:
        found = await confirmations_by_id(session, [confirmation_id])
        if confirmation_id not in found:
            return None
        if not may_see(found[confirmation_id], viewer, now):
            return not_yours_card(confirmation_id, texts)
        settings = await load_policy_settings(session)
        replaced = await predecessors_of(session, [confirmation_id])
        drafts = await draft_lines_of(session, [found[confirmation_id]], texts)
        successors = await successor_lines_of(session, [found[confirmation_id]], texts, now)
        steps = await step_lines_of(session, [found[confirmation_id]], texts, now)
        waiting = await waiting_names_of(session, [found[confirmation_id]])
        changers = await changers_of(session, [found[confirmation_id]], replaced)
        proofs = await proof_offers_of(session, [found[confirmation_id]], viewer, site, now)
        head = await head_of(
            session, confirmation_id=confirmation_id, message_action_id=None, reader_id=viewer.id
        )
    return confirmation_card(
        found[confirmation_id],
        viewer=viewer,
        settings=settings,
        texts=texts,
        now=now,
        record=record,
        replaced=replaced.get(confirmation_id),
        draft=drafts.get(confirmation_id),
        successors=successors.get(confirmation_id),
        steps=steps.get(confirmation_id, ()),
        waiting_for=waiting.get(confirmation_id),
        changed_by=changers.get(confirmation_id),
        proof=proofs.get(confirmation_id),
        head=head,
    )


# The notes of a yes the replay refused, by the reason its record gives (``RefusalReason``).
_REFUSAL_NOTES: Final = frozenset({"not_allowed", "no_longer_possible", "limit", "limit_error"})


async def card_after_decision(
    session_factory: SessionFactory,
    confirmation_id: uuid.UUID,
    *,
    outcome: DecideOutcome,
    record: ActionRecord | None,
    viewer: Person,
    texts: Texts,
    now: datetime,
    site: PasskeySite | None = None,
) -> ConfirmationCard:
    """The card that answers a decision (a button's, a proof's, or the person's words in the
    chat): only the outcome when it is ``not_yours``; otherwise the request read again, as
    ``viewer`` sees it, with the outcome, the ``record`` of the call that ran (its ``card_once`` is
    shown this once) and, for a yes that ended ``refused``, a note that says why: the decider may
    no longer do it, the request can no longer be carried out, or a limit of the owner refused it
    or could not be checked; for a yes that was not the one the request needs now, a note that
    says whose it waits for (``approver_changed``, Ruling R27); for a yes that ran a chain up to a
    step that waits for someone's arrival, a note that says whom it waits for (``paused``); for a
    proof the code found wrong, a note that says nothing was done (``proof_failed``), the proof
    still offered. ``site`` is ``card_now``'s."""
    if outcome == "not_yours":
        return not_yours_card(confirmation_id, texts)
    async with session_factory() as session:
        c = (await confirmations_by_id(session, [confirmation_id]))[confirmation_id]
        settings = await load_policy_settings(session)
        replaced = await predecessors_of(session, [confirmation_id])
        drafts = await draft_lines_of(session, [c], texts)
        successors = await successor_lines_of(session, [c], texts, now)
        steps = await step_lines_of(session, [c], texts, now)
        waiting = await waiting_names_of(session, [c])
        changers = await changers_of(session, [c], replaced)
        proofs = await proof_offers_of(session, [c], viewer, site, now)
        head = await head_of(
            session, confirmation_id=confirmation_id, message_action_id=None, reader_id=viewer.id
        )
    reason = (
        record.card.get("reason") if record is not None and record.status == "refused" else None
    )
    note = texts(f"confirmation.outcome.{reason}") if reason in _REFUSAL_NOTES else None
    if outcome == "proof_failed":
        note = texts("confirmation.proof.failed")
    return confirmation_card(
        c,
        viewer=viewer,
        settings=settings,
        texts=texts,
        now=now,
        outcome=outcome,
        record=record,
        note=note,
        replaced=replaced.get(confirmation_id),
        draft=drafts.get(confirmation_id),
        successors=successors.get(confirmation_id),
        steps=steps.get(confirmation_id, ()),
        waiting_for=waiting.get(confirmation_id),
        changed_by=changers.get(confirmation_id),
        proof=proofs.get(confirmation_id),
        head=head,
    )


async def _decide(
    confirmation_id: str,
    *,
    approve: bool,
    request: Request,
    person: Person,
    session_factory: SessionFactory,
    gatekeeper: GatekeeperClient,
    clock: Callable[[], datetime],
    registry: Mapping[str, InternalTool],
    policy: PolicyTables,
    base_policy: PolicyTables,
    settings: CoreSettings,
    model_factory: ModelFactory,
    model_lister: ModelLister,
    streaming_replies: set[uuid.UUID],
) -> Response:
    """Decide with a button, wake Domo when that decided something, and answer with the card as
    it is now (see the module docstring)."""
    cid = _request_id(confirmation_id)
    texts = request_texts(request)
    core = _core(
        request,
        session_factory=session_factory,
        model_factory=model_factory,
        model_lister=model_lister,
        streaming_replies=streaming_replies,
        settings=settings,
        gatekeeper=gatekeeper,
        clock=clock,
        registry=registry,
        base=base_policy,
    )
    outcome, record = await decide_confirmation(
        session_factory=session_factory,
        confirmation_id=cid,
        decider=person,
        approve=approve,
        via="button",
        reply_message_id=None,
        ctx_factory=_deciders_context(
            request,
            person,
            cid,
            session_factory=session_factory,
            gatekeeper=gatekeeper,
            clock=clock,
            registry=registry,
            policy=policy,
            notify=core.notify,
        ),
        registry=registry,
        policy=policy,
        now=clock(),
    )
    await core.wake_after_decision(
        confirmation_id=cid, outcome=outcome, record=record, decider=person, via="button"
    )
    card = await card_after_decision(
        session_factory,
        cid,
        outcome=outcome,
        record=record,
        viewer=person,
        texts=texts,
        now=clock(),
        site=passkey_availability_of(request, settings).site,
    )
    return render(request, "fragments/confirmation_card.html", card=card)


def _request_id(confirmation_id: str) -> uuid.UUID:
    """The id of a request named in a path; 404 for anything that is not one."""
    if _CONFIRMATION_ID.fullmatch(confirmation_id.lower()) is None:
        raise HTTPException(status_code=404)
    return uuid.UUID(confirmation_id)


def _deciders_context(
    request: Request,
    person: Person,
    confirmation_id: uuid.UUID,
    *,
    session_factory: SessionFactory,
    gatekeeper: GatekeeperClient,
    clock: Callable[[], datetime],
    registry: Mapping[str, InternalTool],
    policy: PolicyTables,
    notify: Notify,
) -> Callable[[Person, uuid.UUID], ToolContext]:
    """The ``ctx_factory`` of a decision made on this page (a button, a proof): the moment of the
    decision, made the requester's (``requester_context``), on a page (``PAGE``: a vault form it
    opens is shown in the answer); what the request does that is news for someone is told to the
    core (``notify``); a stored call of one of Domo's tools is found as a turn finds it."""
    # Here: the channels' turns import this module's neighbours.
    from domo_core.channels.turns import app_made_tool_finder, app_turn_deps
    from domo_core.conversation.prepare import made_tool_deps

    return requester_context(
        ToolContext(
            session_factory=session_factory,
            actor=Actor(person_id=person.id, role=as_role(person.role)),
            person=person,
            conversation_id=confirmation_id,  # replaced by the request's own conversation
            origin=str(request.base_url),
            gatekeeper=gatekeeper,
            clock=clock,
            registry=registry,
            policy=policy,
            reply_message_id=None,
            texts=request_texts(request),
            text_writing=getattr(request.app.state, "text_writing", None),
            notify=notify,
            abilities=PAGE,
            made_tool_named=app_made_tool_finder(request.app),
            # A stored program run by hand runs as a turn runs it (``tools.program_run``).
            launch_deps=made_tool_deps(app_turn_deps(request.app)),
        )
    )


def _core(
    request: Request,
    *,
    session_factory: SessionFactory,
    model_factory: ModelFactory,
    model_lister: ModelLister,
    streaming_replies: set[uuid.UUID],
    settings: CoreSettings,
    gatekeeper: GatekeeperClient,
    clock: Callable[[], datetime],
    registry: Mapping[str, InternalTool],
    base: PolicyTables,
) -> Conversation:
    """The conversation core, with what a turn needs taken from this request: what a decision of
    this page tells (a note a request leaves is news, ``Conversation.notify``), and what it wakes
    Domo with when it decided something (``Conversation.wake_after_decision``, which checks the
    outcome). ``base`` is the catalog of the code (``get_policy``), never the one the request
    loaded: the woken turn lays the open kinds over it as they are then (``prepare_turn``), and a
    kind laid already would stay as the request read it."""
    # Imported here, not at the top: the chat's routes import this module for the cards.
    from domo_core.channels.turns import turn_deps_of

    return Conversation(
        turn_deps_of(
            request.app,
            session_factory=session_factory,
            model_factory=model_factory,
            model_lister=model_lister,
            streaming_replies=streaming_replies,
            settings=settings,
            gatekeeper=gatekeeper,
            clock=clock,
            registry=registry,
            policy=base,
        )
    )


@router.post(CONFIRMATIONS_PATH + "/{confirmation_id}/approve")
async def approve_confirmation(
    confirmation_id: str,
    request: Request,
    person: Annotated[Person, Depends(current_person)],
    session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
    gatekeeper: Annotated[GatekeeperClient, Depends(get_gatekeeper)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
    registry: Annotated[Mapping[str, InternalTool], Depends(get_tool_registry)],
    policy: Annotated[PolicyTables, Depends(policy_now)],
    base_policy: Annotated[PolicyTables, Depends(get_policy)],
    settings: Annotated[CoreSettings, Depends(get_settings)],
    model_factory: Annotated[ModelFactory, Depends(get_model_factory)],
    model_lister: Annotated[ModelLister, Depends(get_model_lister)],
    streaming_replies: Annotated[set[uuid.UUID], Depends(get_streaming_replies)],
) -> Response:
    """The person's yes, by the button of the card."""
    return await _decide(
        confirmation_id,
        approve=True,
        request=request,
        person=person,
        session_factory=session_factory,
        gatekeeper=gatekeeper,
        clock=clock,
        registry=registry,
        policy=policy,
        base_policy=base_policy,
        settings=settings,
        model_factory=model_factory,
        model_lister=model_lister,
        streaming_replies=streaming_replies,
    )


@router.post(CONFIRMATIONS_PATH + "/{confirmation_id}/refuse")
async def refuse_confirmation(
    confirmation_id: str,
    request: Request,
    person: Annotated[Person, Depends(current_person)],
    session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
    gatekeeper: Annotated[GatekeeperClient, Depends(get_gatekeeper)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
    registry: Annotated[Mapping[str, InternalTool], Depends(get_tool_registry)],
    policy: Annotated[PolicyTables, Depends(policy_now)],
    base_policy: Annotated[PolicyTables, Depends(get_policy)],
    settings: Annotated[CoreSettings, Depends(get_settings)],
    model_factory: Annotated[ModelFactory, Depends(get_model_factory)],
    model_lister: Annotated[ModelLister, Depends(get_model_lister)],
    streaming_replies: Annotated[set[uuid.UUID], Depends(get_streaming_replies)],
) -> Response:
    """The person's no, by the button of the card."""
    return await _decide(
        confirmation_id,
        approve=False,
        request=request,
        person=person,
        session_factory=session_factory,
        gatekeeper=gatekeeper,
        clock=clock,
        registry=registry,
        policy=policy,
        base_policy=base_policy,
        settings=settings,
        model_factory=model_factory,
        model_lister=model_lister,
        streaming_replies=streaming_replies,
    )


# --- the proof that it is the person (``proofs.service``) ----------------------------------

# The key of a person's passkey options in the options throttle (``passkey_routes``), apart from
# the client addresses the login's options are counted under.
_PROOF_OPTIONS_KEY: Final = "proof:{account_id}"


def _proof_notice(
    request: Request, status_code: int, key: str, headers: dict[str, str], **params: str
) -> Response:
    """A proof that was not checked (held back, or argon2 busy), said in the notice area of the
    chat page, as its refusals are (``channels.page.routes``)."""
    # Imported here, not at the top: the chat's routes import this module for the cards.
    from domo_core.channels.page.routes import NOTICE_TARGET

    return render(
        request,
        "fragments/chat_notice.html",
        status_code=status_code,
        headers={"HX-Retarget": NOTICE_TARGET, "HX-Reswap": "innerHTML", **headers},
        key=key,
        params=params,
    )


@router.post(CONFIRMATIONS_PATH + "/{confirmation_id}/proof/options")
async def proof_passkey_options(
    confirmation_id: str,
    request: Request,
    person: Annotated[Person, Depends(current_person)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[CoreSettings, Depends(get_settings)],
    throttle: Annotated[LoginThrottle, Depends(get_passkey_options_throttle)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
) -> Response:
    """The options of the passkey's ceremony for a proof of this request (JSON, for
    ``static/passkeys.js``): for the person who may answer it, when the card offers them their
    passkey. 404 where passkeys cannot be used, for someone else, and for a request that asks no
    proof or no longer waits; 429 (JSON, with ``Retry-After``) when the person asked for too many
    in the life of a challenge, as for the login's options."""
    cid = _request_id(confirmation_id)
    site = passkey_availability_of(request, settings).site
    if site is None:
        raise HTTPException(status_code=404)
    now = clock()
    found = (await confirmations_by_id(session, [cid])).get(cid)
    offer = None if found is None else await proof_offer(session, found, person, site, now)
    if offer is None or "passkey" not in offer.means:
        raise HTTPException(status_code=404)
    account = (
        await session.execute(select(Account).where(Account.person_id == person.id))
    ).scalar_one()
    wait = throttle.register_attempt(_PROOF_OPTIONS_KEY.format(account_id=account.id))
    if wait is not None:
        return JSONResponse(
            {"error": request_texts(request)("confirmation.proof.busy")},
            status_code=429,
            headers={"Retry-After": str(math.ceil(wait))},
        )
    try:
        options = await PROOF_KINDS["passkey"].begin(session, person, page_place(site), now)
    except PasskeyRejected:
        await session.rollback()
        raise HTTPException(status_code=404) from None
    await session.commit()
    return JSONResponse(options)


@router.post(CONFIRMATIONS_PATH + "/{confirmation_id}/prove")
async def prove_confirmation(
    confirmation_id: str,
    request: Request,
    person: Annotated[Person, Depends(current_person)],
    session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
    gatekeeper: Annotated[GatekeeperClient, Depends(get_gatekeeper)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
    registry: Annotated[Mapping[str, InternalTool], Depends(get_tool_registry)],
    policy: Annotated[PolicyTables, Depends(policy_now)],
    base_policy: Annotated[PolicyTables, Depends(get_policy)],
    settings: Annotated[CoreSettings, Depends(get_settings)],
    model_factory: Annotated[ModelFactory, Depends(get_model_factory)],
    model_lister: Annotated[ModelLister, Depends(get_model_lister)],
    streaming_replies: Annotated[set[uuid.UUID], Depends(get_streaming_replies)],
    throttle: Annotated[LoginThrottle, Depends(get_password_check_throttle)],
    means: Annotated[str, Form()] = "",
    credential: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
) -> Response:
    """The person's proof, from the card (``means``: ``passkey`` with the browser's answer in
    ``credential``, or ``password`` with ``password``), checked by the code
    (``proofs.service.prove``); a proof that holds is their yes. Then a wake, as after a
    button, and the card as it is now (``proof_failed``: nothing was done, the proof still
    offered). 429 with ``Retry-After`` when wrong passwords are held back, 503 with
    ``Retry-After`` when too many wait for argon2 (nothing was checked): both said in the notice
    area of the page. The answer given never goes anywhere else: not to the conversation, not to a
    model, not to the log or the audit."""
    cid = _request_id(confirmation_id)
    texts = request_texts(request)
    site = passkey_availability_of(request, settings).site
    core = _core(
        request,
        session_factory=session_factory,
        model_factory=model_factory,
        model_lister=model_lister,
        streaming_replies=streaming_replies,
        settings=settings,
        gatekeeper=gatekeeper,
        clock=clock,
        registry=registry,
        base=base_policy,
    )
    try:
        outcome, record = await prove(
            session_factory=session_factory,
            confirmation_id=cid,
            person=person,
            means=means,
            answer=credential if means == "passkey" else password,
            place=page_place(site),
            throttle=throttle,
            now=clock(),
            ctx_factory=_deciders_context(
                request,
                person,
                cid,
                session_factory=session_factory,
                gatekeeper=gatekeeper,
                clock=clock,
                registry=registry,
                policy=policy,
                notify=core.notify,
            ),
            registry=registry,
            policy=policy,
            speaker=page_speaker(person.id, clock()),
        )
    except ProofHeldBack as held:
        return _proof_notice(
            request,
            429,
            "account.password.error.throttled",
            {"Retry-After": str(math.ceil(held.wait))},
            minutes=str(math.ceil(held.wait / 60)),
        )
    except Argon2Busy:
        return _proof_notice(request, 503, "passwords.busy", busy_headers())
    await core.wake_after_decision(
        confirmation_id=cid, outcome=outcome, record=record, decider=person, via="proof"
    )
    card = await card_after_decision(
        session_factory,
        cid,
        outcome=outcome,
        record=record,
        viewer=person,
        texts=texts,
        now=clock(),
        site=site,
    )
    return render(request, "fragments/confirmation_card.html", card=card)


@dataclass(frozen=True)
class Approval:
    """A request in the approvals zone: who asks (``requester``, their display name), its card,
    and whether the one who asked nudged its approvers (``nudged``, spec 5.2: it comes first)."""

    requester: str
    card: ConfirmationCard
    nudged: bool = False


@router.get(APPROVALS_PATH)
async def approvals(
    request: Request,
    person: Annotated[Person, Depends(current_person)],
    session: Annotated[AsyncSession, Depends(get_session)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
    core_settings: Annotated[CoreSettings, Depends(get_settings)],
    session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
) -> Response:
    """The requests that wait for ``person``'s yes: the children's, for an adult or the owner,
    and those whose yes is the owner's, for the owner (empty for anyone who cannot answer them),
    each with who asks and its card, as ``person`` sees it (the proof a request asks offered as
    the page can give it, F12); those the one who asked nudged first, and saying so (spec 5.2)."""
    texts = request_texts(request)
    now = clock()
    settings = await load_policy_settings(session)
    waiting, total = await awaiting_approval(
        session, person, settings, now, limit=MAX_APPROVALS_SHOWN
    )
    drafts = await draft_lines_of(session, (c for c, _ in waiting), texts)
    successors = await successor_lines_of(session, (c for c, _ in waiting), texts, now)
    steps = await step_lines_of(session, (c for c, _ in waiting), texts, now)
    names = await waiting_names_of(session, (c for c, _ in waiting))
    replaced = await predecessors_of(session, (c.id for c, _ in waiting))
    changers = await changers_of(session, (c for c, _ in waiting), replaced)
    site = passkey_availability_of(request, core_settings).site
    proofs = await proof_offers_of(session, (c for c, _ in waiting), person, site, now)
    heads = await heads_of(
        session, reader_id=person.id, confirmation_ids=(c.id for c, _ in waiting)
    )
    shown = [
        Approval(
            requester=name,
            card=confirmation_card(
                c,
                viewer=person,
                settings=settings,
                texts=texts,
                now=now,
                draft=drafts.get(c.id),
                successors=successors.get(c.id),
                steps=steps.get(c.id, ()),
                waiting_for=names.get(c.id),
                replaced=replaced.get(c.id),
                changed_by=changers.get(c.id),
                proof=proofs.get(c.id),
                head=heads.get(c.id),
            ),
            nudged=c.nudged_at is not None,
        )
        for c, name in waiting
    ]
    # Imported here, not at the top: what asks for the heads builds the cards of this module.
    from domo_core.texts.card_heads import ask_missing_heads

    # A request shown without a head for the person: theirs is asked, for the next display.
    ask_missing_heads(
        getattr(request.app.state, "text_writing", None),
        session_factory,
        (approval.card for approval in shown),
        person,
        clock=clock,
    )
    return render(
        request,
        "fragments/approvals.html",
        approvals=shown,
        more=total - len(shown),
    )
