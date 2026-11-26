"""Confirmations: a request that waits for a person's yes, and the decision on it.

When the barrier needs someone's agreement (``NeedsApproval``), or the yes-judge, a rule of the
owner or Domo himself asks a yes, the chat stores the call as a request
(``create_confirmation``, from ``conversation.execution``), with what it asks (a yes, an explicit
yes or a proof), who asked it and why. The person who may answer it (the requester, an adult for a
child's request: decision D2, or the owner when a right of the owner says so) decides
with a button or in their own words (an approver's in their own conversation, once Domo showed
the request there: spec 5.1), and ``decide_confirmation`` carries the decision out. The main model
never decides: the words are read by the code and by an isolated classifier
(``confirmations.intent``).

Deciding
--------

``decide_confirmation`` runs in three phases, so that no row lock is held while a tool runs (a
tool may talk to the network):

1. in one transaction (``_open_pending``): the request's row is locked (``FOR UPDATE``), then

   * the decider must be the one who may answer it (``self``: the requester; ``adult``: an adult
     or the owner who is not the requester; ``owner``: the owner, who is not the requester), and
     still active; otherwise ``not_yours``, and nothing about the request is told;
   * a request that is not ``pending`` any more is ``already_decided`` (``executing`` included:
     someone's yes is running it), or ``expired``;
   * one past its lifetime (the owner's setting, read again now) becomes ``expired``;

   then

   * a yes that does not give what the request asks (``ask``) changes nothing: a plain phrase, the
     classifier or a change for an explicit yes is ``needs_explicit``, and anything but a proof for
     a proof is ``needs_proof`` (``confirmations.intent`` and ``amend_confirmation`` already keep
     that case out: this is a second lock on the same door);
   * a no makes it ``refused``;
   * a yes replays the barrier for the **requester** with the owner's rights and the facts as
     they are now (``policy.decision.decide_now``: their role, a right, or what a limit reads may
     have changed meanwhile). The stored call is described again first, by its tool, for the
     requester (Ruling R6): what its facts say of the present (someone's role, whether a note was
     seen, whether one acts on oneself) may be out of date. A call the tool no longer accepts,
     that fails or takes too long to describe, or that now describes another type of action or
     another target, makes it ``refused`` (``no_longer_possible``), as does a requester who is no
     longer active. A ``Refuse`` makes it ``refused`` (``limit`` when a limit refused it, with
     the limit's summary). A ``NeedsApproval`` on replay is expected when this decision is that
     agreement, but the decider must be one its approver allows: a requester made a child since,
     say, may no longer approve their own request (it needs an adult now), nor may anyone but the
     owner once a right or a limit gives the yes to the owner; and the owner who lifted the limit
     that sent a request to them may no longer say yes for the one who asked. Such a request is
     not refused (Ruling R27): it now waits for the agreement the barrier asks for
     (``approver_changed``, ``_reroute``), with its age (and so its lifetime), and the one who said
     yes is told whose yes it waits for. Then what the request asks now (F21): a rule of the owner
     added since it was made raises what it asks (``asked_by``
     ``rule``), and the yes must give it: else ``needs_explicit`` or
     ``needs_proof``, and nothing but that raise changes. Otherwise the request becomes
     ``executing``;
   * a chain of actions (``chain_basis`` is set: ``tools.chain``) is never described again as a
     whole, nor are its steps here: each step is described again and put through the barrier in
     its turn, after the yes (``confirmations.chain.run_steps``). The chain's own action
     (``chain.run``) is replayed as stored, so that a right or a limit of the owner on chains
     given since applies; its yes covers its steps, so the decider must only not be outdone by a
     more demanding approver (``store.covers``). What it asks now counts the rules
     on its steps still to run, each resolved for the requester;
2. the commit that makes it ``executing``, the stored call (with the arguments the card shows:
   ``effective_arguments``, and the stored action as the one the yes was given to), run through
   the same handling as any allowed call (``conversation.execution.execute_allowed``) in the
   context of the **requester**, even when an adult approves a child's request (for a chain, its
   tool runs its steps from their rows, ``ToolContext.request_id``), and
3. in a new transaction, the request made ``executed`` or ``failed``, with ``result`` = the
   stored card of the action (never ``card_once``) (``settle``). A chain that went back to
   waiting for someone else's yes on the way (a right changed between its yes and one of its
   steps) is left as it is: it waits again (``approver_changed``), and what its steps done so far
   made for the one who asked waits behind their buttons on its card (``confirmations.routes``);
   so is a chain that paused at a step that waits for someone's arrival (``paused``, spec 4.3b:
   the upkeep resumes it, ``confirmations.arrivals``),

are one unit shielded from cancellation (``_run_and_settle``): once a yes has made a request
``executing``, its action is run and recorded, whoever leaves meanwhile. Each step keeps its own
time limit.

A request Domo made on his own (``Confirmation.by_domo``, a turn a launch for the house woke
him for, Task 9) is his, wherever it waits (the owner's conversation): whoever may answer it is
read with Domo as the one who asked (``store.requester_of``: an adult's agreement any adult's,
the owner included, the owner's the owner's, his own yes nobody's), and a yes replays the barrier
for Domo himself (``policy.roles.domo_actor``: his rights, limits and history, audited as his;
when the agreement it waited for lets it through, one ``allow`` in his name unless one was written
already, ``Confirmation.counted_for_domo``, so that his history counts it once) and runs it as
his, on his own (``ToolContext.on_his_own``: what it launches is for the house). A person answers
it yes, no, or "yes, but" (the owner's decision of 2026-11-24), never drops it; Domo, in a turn of
his own, drops it (``expire_by_domo``) or changes it. A change of it, his or the answerer's
(``amend_confirmation``, ``_amend_his_own``), is made again in his name and judged from scratch
as his: it runs when nothing more is asked (his own change: the barrier lets it through; the
answerer's: their yes is the one it needs and their words give what it asks), and waits again for
a yes otherwise (spec 12.5). He never drops nor changes a
person's request under cover of his own there, a note in his name they asked for included, but
by a right the owner gives him (``asker_on_his_own``, ruling R30); and a person's request
changed in such a turn is judged as theirs (ledger n3).

Every decision records who decided, when, how (``decided_via``) and from which message; the audit
entry ``confirmation.decided`` is written last, just before the commit. A yes given by a proof
(``proof``: the person's passkey or password, on the card) is checked first, by the code
(``proofs.service.prove``), which then calls ``decide_confirmation``; its audit entry names
the means (``means``), never what was given.

What Domo does with the requests
----------------------------------

Domo manages the person's own pending requests in the conversation (decision D2), and shows
the person the requests of others that wait for their yes (spec 5.1), through the tools of
``tools.confirmation_tools``. It never approves or refuses one:

* ``remind`` counts a reminder, and the reply that presents the request again is the one the
  person's next words may answer (R74); for a request of someone else that waits for the
  person's yes, the reply is the approver's (``shown_to_approver_message_id``), and what the one
  who asked was shown does not move; a request of the person that waits for someone else's yes
  (spec 5.2) is nudged instead (``nudged_at``): it comes first in its approver's zone, and their
  Domo is told; the person's words never answer it;
* ``expire_by_domo`` drops a request that has waited too long or no longer makes sense: it can
  no longer be approved; a request of the person that waits for someone else's yes, and a chain
  paused until someone's arrival, are dropped the same way (the invitation it made stays); and,
  in a turn of his own, a request of his own;
* ``require_explicit`` asks for an explicit yes (a firm yes in words, or the button) for a
  request Domo judges needs one, and says why on its card (``asked_by`` ``domo``); a
  request that asks as much or more already is left as it is. Neither it nor ``remind``
  touches a paused chain: no yes is asked of it (it goes on under the yes already given), and
  Domo is told so;
* ``amend_confirmation`` applies a "yes, but change X": only right after the person answered the
  request with a change, in that turn (``ToolContext.change_grant``), with the same tool and
  type of action, an actual change, and through the barrier again. The yes-judge judges the
  changed action again (spec 4.1, 4.7), for the one who asked, told the request's history
  (``confirmations.chain.request_told``): its ``do_not`` refuses it at once, sparing whoever's
  agreement it would need. When the rights decide whose yes it needs (an agreement the person
  cannot give, Ruling R27), it becomes a new request asking what is asked then
  (``judge.ask_after_a_change``: the judge's verdict, what Domo asks more, what the request
  asked when words never give it, the owner's rules); otherwise the change
  runs at once (the person's words are their yes to it), through the same phases as a decision,
  only when that is nothing or a plain yes that words give (``judge.given_by_the_change``), and
  becomes a new request otherwise, stored with the action that shows it, waiting for the yes the
  barrier asks for, asking what was found. An approver may answer someone else's
  request with a change too (spec 5.1): the changed action is planned, judged and run for the one
  who asked (their role, their rights, their context, their case for the judge), never for the
  approver, and its yes is the approver's; with it, Domo keeps what the approver said and how
  firm they were (``approver_note``), for the one who asked. A change of rights is changed in
  words (spec 5.3): the rights writer drafts it again with the owner's change, before the request
  is locked, and the new draft is judged like any other change (``_amend_rights``). A chain of
  actions is changed as a whole (spec 4.1): Domo gives all of its steps as the person wants
  them now, the steps already done first, as they were; it is planned again, each step kept as
  it was asked for judged with the role the chain remembers, each step changed or added with the
  role of now (``confirmations.replan``), then judged again as a whole, with every step told;
  the chain given back as it is changes nothing (``_amend_chain``).
  A chain paused until someone's arrival is not changed: Domo drops it and proposes a new chain,
  which waits for the same invitation. A request of the person that waits for someone else's yes
  (spec 5.2) may be changed without an answer of theirs (their words never answer it): nothing
  runs, it always becomes a new request for the same approver.

The first three work on the caller's session and leave the commit to it; ``amend_confirmation``,
like ``decide_confirmation``, owns its transactions.
"""

import logging
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, Final, Literal

import anyio
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.accounts.sessions import is_active
from domo_core.audit.log import append_audit
from domo_core.chat.agent import ActionRecord
from domo_core.chat.models import Message
from domo_core.chat.store import ConversationStore
from domo_core.confirmations.chain import paused, request_told, waits_again
from domo_core.confirmations.describe import (
    describe_chain,
    describe_for_people,
    draft_of,
    note_for_a_model,
    note_told_as,
    request_description,
    step_sentence,
)
from domo_core.confirmations.judge import (
    Judgement,
    ask_after_a_change,
    audit_judgement,
    given_by_the_change,
    owner_rule_of,
    planned_step_told,
)
from domo_core.confirmations.models import Confirmation, ConfirmationStep
from domo_core.confirmations.replan import (
    NOTE_IS_THE_AUTHORS,
    calls_of,
    done_prefix_holds,
    keep_notes_as_written,
    match_steps,
    notes_as_told,
    replace_chain_request,
    unchanged,
)
from domo_core.confirmations.steps import PlannedStep, add_steps, steps_of
from domo_core.confirmations.store import (
    CANNOT_BE_UNDONE_REASON,
    action_type_of,
    approver_of,
    approves,
    ask_of,
    asked_of,
    awaiting_approval,
    confirmations_by_id,
    conversation_made_in,
    covers,
    create_confirmation,
    decided_for_him,
    effective_arguments,
    for_the_person_concerned,
    is_past_lifetime,
    made_in_a_turn,
    mark_shown,
    mark_shown_to_approver,
    pending_for,
    pending_to_approve,
    predecessors_of,
    replayed_on_his_own,
    request_state,
    requester_of,
    warned_one,
    who_answers,
)
from domo_core.conversation.execution import (
    LIMIT_ERROR_DETAIL,
    NOT_ALLOWED_DETAIL,
    consult_the_judge,
    execute_allowed,
    failed_record,
    refused_by_the_judge,
    rights_say,
    warned_first,
)
from domo_core.conversation.speaker import Speaker
from domo_core.db import SessionFactory
from domo_core.household.service import resolve_person, whole_names
from domo_core.identity.clues import keep_proof_clue
from domo_core.learning.models import Kind
from domo_core.learning.record import kept_reason, note_went_wrong, reason_code
from domo_core.learning.undone import note_undoing
from domo_core.llm.types import JsonObject
from domo_core.memory.told import told
from domo_core.policy.actions import DescribedAction
from domo_core.policy.asks import Ask, Asked, most_demanding
from domo_core.policy.barrier import (
    Allow,
    Decision,
    NeedsApproval,
    Refuse,
    given_for_an_idea,
    record_decision,
    stricter,
    writes_for_domo,
)
from domo_core.policy.catalog import (
    DOMO_ACTIONS,
    NOT_ON_HIS_OWN,
    PolicyTables,
    on_his_own_requests,
    on_his_own_tables,
)
from domo_core.policy.decision import ChainBasis, decide_now
from domo_core.policy.rights import Approver, load_rights
from domo_core.policy.roles import (
    DOMO_LABEL,
    DOMO_SUBJECT_ID,
    Actor,
    as_role,
    domo_actor,
)
from domo_core.policy.settings import PolicySettings, load_policy_settings
from domo_core.policy.subjects import concerned_of, keys_of, subject_of
from domo_core.text import is_plain_line
from domo_core.texts.view import REFERENCE
from domo_core.tools.context import (
    InternalTool,
    InvalidArguments,
    NamesPeople,
    OwnIdea,
    PlansSteps,
    ToolContext,
    judged_actor_of,
)
from domo_core.tools.rights_tools import redraft_rights

if TYPE_CHECKING:
    # Only a type here: the chain's module imports this one (the chain is reached through the
    # registry).
    from domo_core.tools.chain import ChainPlan

__all__ = [
    "MAX_APPROVER_NOTE_LENGTH",
    "MAX_EXPLICIT_REASON_LENGTH",
    "AmendOutcome",
    "DecideOutcome",
    "DecidedVia",
    "RefusalReason",
    "action_type_of",
    "amend_confirmation",
    "approver_note_from",
    "approves",
    "asker_on_his_own",
    "awaiting_approval",
    "confirmations_by_id",
    "create_confirmation",
    "decide_confirmation",
    "effective_arguments",
    "expire_by_domo",
    "expire_past_lifetime",
    "explicit_reason_from",
    "is_active",
    "is_past_lifetime",
    "mark_shown",
    "mark_shown_to_approver",
    "may_decide",
    "may_see",
    "open_for_a_proof",
    "pending_for",
    "pending_to_approve",
    "predecessors_of",
    "remind",
    "request_state",
    "require_explicit",
    "rerouted_yes",
    "settle",
    "short_plain_line",
]

logger = logging.getLogger(__name__)

# ``needs_explicit``, ``needs_proof``: the yes did not give what the request asks (the button or
# a firm yes, a proof), and nothing changed. ``proof_failed``: a proof was given on the card, and
# the code found it wrong (``proofs.service``): nothing changed. ``approver_changed``: the yes
# was not the one the request needs now; it waits for that one (Ruling R27). ``paused``: the yes
# ran a chain up to a step that waits for someone's arrival (spec 4.3b). ``arrived``: a chain
# resumed after that arrival has steps left of which the owner's rules ask more than the yes
# given gave, so it waits for a yes again (only the resume gives it: ``confirmations.arrivals``).
DecideOutcome = Literal[
    "executed",
    "failed",
    "refused",
    "expired",
    "already_decided",
    "not_yours",
    "needs_explicit",
    "needs_proof",
    "proof_failed",
    "approver_changed",
    "paused",
    "arrived",
]
# How a yes was given; ``judge``: nobody said yes, the judge let a chain go
# (``conversation.execution``).
DecidedVia = Literal[
    "button", "explicit_phrase", "phrase", "classifier", "change", "proof", "judge"
]
# ``approver_changed``: the change became a new request that waits for someone else's yes;
# ``paused``: the changed chain ran up to a step that waits for someone's arrival; ``writer_asks``:
# the rights writer asks the owner a question instead of drafting a change of rights again (the
# request still waits).
AmendOutcome = Literal[
    "executed",
    "failed",
    "refused",
    "asked_again",
    "approver_changed",
    "expired",
    "already_decided",
    "paused",
    "writer_asks",
]

# How long the last phase (the record of what came of the action) may take.
_SETTLE_SECONDS: Final = 15.0
# How long the request's tool may take to describe a changed call, or the stored call again at a
# yes (as for any call: ``conversation.execution``). The run of the call has its own limit there,
# and its record above.
_DESCRIBE_SECONDS: Final = 60.0

# The longest reason Domo may give for an explicit check (the column holds 200).
MAX_EXPLICIT_REASON_LENGTH: Final = 200
# The longest note Domo may keep of what an approver said with a change (the column holds 300).
MAX_APPROVER_NOTE_LENGTH: Final = 300
# What looks like a link: an approver's note never carries one (spec 5.1).
_LINK: Final = re.compile(r"https?://|www\.", re.IGNORECASE)

# Why a yes ended ``refused``: the decider's own rights failed (``not_allowed``), or the request
# can no longer be carried out whoever approves it (the requester left, or lost the right, or the
# call no longer describes the same action), or a limit refused it now (``limit``), or a limit
# could not be checked (``limit_error``).
RefusalReason = Literal["not_allowed", "no_longer_possible", "limit", "limit_error"]

# The ways of saying yes that do not give an explicit yes (the button, a firm yes in words,
# ``explicit_phrase``: the firm yes the code knows or the one the classifier read, and a proof
# do).
_NOT_EXPLICIT: Final = frozenset({"phrase", "classifier", "change"})
# The ways of answering in words: a no said so keeps the person's words (``refusal_words``).
_SAID_IN_WORDS: Final = frozenset({"explicit_phrase", "phrase", "classifier"})

# What Domo is told when an id names no request of the person that waits in this conversation
# (it never learns whether the id exists).
_NOT_PENDING: Final = "no request of this person waits with this id in this conversation"
# What Domo is told when he changes a chain of the person that paused until someone's arrival.
_PAUSED_NOT_CHANGED: Final = (
    "a chain that waits for someone to join is not changed: drop it and propose a new chain, which "
    "waits for the same invitation"
)
# What Domo is told when he shows again, or asks an explicit yes for, a chain of the person that
# paused until someone's arrival: no yes is asked of it, it goes on under the yes already given
# (spec 4.3b).
_PAUSED_NOT_SHOWN: Final = (
    "a chain that waits for someone to join is not shown again: nothing answers it, it goes on by "
    "itself once they join; drop it with confirmation_expire if it no longer makes sense"
)
_PAUSED_NO_YES: Final = (
    "a chain that waits for someone to join asks for no yes: it goes on under the yes already "
    "given once they join; drop it with confirmation_expire if it no longer makes sense"
)

# A change of rights, changed in words: the writer drafts it again (spec 5.3).
_RIGHTS_APPLY: Final = "rights.apply"
# The type of a chain of actions (``tools.chain.CHAIN_ACTION_TYPE``; not imported: the chain
# imports this module).
_CHAIN_RUN: Final = "chain.run"
_RIGHTS_CHANGE: Final = frozenset({"change"})
_RIGHTS_IN_WORDS: Final = (
    "a change of rights is changed in words: give change, what the owner wants different"
)
# What Domo is told when a change comes without the person's answer of this turn.
_NO_GRANT: Final = "a request can only be changed right after the person answered it with a change"

# The arguments of a change of a chain of actions: the whole chain, as the person wants it now.
_CHAIN_CHANGE: Final = frozenset({"steps"})
# What Domo is told when he changes a chain of his own that waits: drop it and propose another.
_HIS_CHAIN_NOT_CHANGED: Final = (
    "a chain of yours that waits is not changed: drop it with confirmation_expire and propose the "
    "chain you want now. Nothing was changed"
)
# What Domo is told when a person answers a chain of his own with a change: it is not changed.
_HIS_CHAIN_ANSWERED: Final = (
    "a chain of yours that waits is answered yes or no, never changed: tell the person, and "
    "propose the chain they want as a new one. Nothing was changed"
)
# What Domo is told when nobody could answer a request changed now.
_NOBODY_TO_ASK: Final = (
    "nobody could answer it now (the household has no owner): nothing was changed"
)
# The type of a note, which an approver never changes, neither its words nor its recipient
# (spec 6.7).
_NOTE_LEAVE: Final = "note.leave"


def may_decide(c: Confirmation, person: Person, now: datetime) -> bool:
    """Whether ``person`` may answer the request: the requester for a ``self`` request; an adult
    or the owner other than the requester for an ``adult`` one; the owner, not the requester, for
    an ``owner`` one; when a person is concerned (``concerned_id``), that person alone, whatever
    the kind. Only an active person may."""
    if c.concerned_id is not None:
        return is_active(person, now) and person.id == c.concerned_id
    return is_active(person, now) and approves(c.approver_kind, person, requester_of(c))


def _waits_for_someone_else(
    c: Confirmation, person: Person, conversation_id: uuid.UUID, now: datetime
) -> bool:
    """Whether ``c`` is a request of ``person``, who is active, in this conversation, that waits
    for someone else's yes (an adult's, the owner's: spec 5.2): they may change it without an
    answer of theirs, since such a change only ever asks its approver again."""
    return (
        is_active(person, now)
        and (requester_of(c), c.conversation_id) == (person.id, conversation_id)
        and c.approver_kind != "self"
    )


def _use_grant(ctx: ToolContext, confirmation_id: uuid.UUID) -> None:
    """Use up the change grant of the turn once the request it names, ``confirmation_id``, is
    decided one way or another (or found over). A change of another request, one of the person's
    that waits for someone else's yes (it needs no grant), leaves it for the one it names."""
    if ctx.change_grant == confirmation_id:
        ctx.change_grant = None


def may_see(c: Confirmation, person: Person, now: datetime) -> bool:
    """Whether ``person`` may see the request's card: its requester, or someone who may answer
    it. Anyone else is told nothing about it."""
    return person.id == c.requester_id or may_decide(c, person, now)


def _refused_yes(
    c: Confirmation, reason: RefusalReason, *, limit: str | None = None
) -> ActionRecord:
    """The record of a yes that the replay refused, and why (its card says it plainly); with the
    summary of the limit that refused it, if one did, for Domo to tell. A limit that could not
    be checked comes with the same words for Domo as a call that was refused at once
    (``conversation.execution``): tell the person, who may ask the owner. So does a yes of
    someone who may not do it any more (``not_allowed``: their role or the rights changed since
    they asked): in words he can say, never the bare word of the reason."""
    model_result: JsonObject = {"status": "refused", "reason": reason}
    if limit is not None:
        model_result["limit"] = limit
    if reason == "limit_error":
        model_result["detail"] = LIMIT_ERROR_DETAIL
    elif reason == "not_allowed":
        model_result["detail"] = NOT_ALLOWED_DETAIL
    return ActionRecord(
        tool_name=c.tool_name,
        action_type=action_type_of(c),
        status="refused",
        confirmation_id=c.id,
        model_result=model_result,
        card={"key": f"confirmation.card.refused.{reason}", "params": {}, "reason": reason},
    )


async def _locked(session: AsyncSession, confirmation_id: uuid.UUID) -> Confirmation | None:
    result = await session.execute(
        select(Confirmation)
        .where(Confirmation.id == confirmation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def _person(session: AsyncSession, person_id: uuid.UUID) -> Person | None:
    """The person as the database holds them now (their role or status may have changed)."""
    result = await session.execute(
        select(Person).where(Person.id == person_id).execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def _audit_decided(
    session: AsyncSession,
    c: Confirmation,
    decider: Person,
    *,
    outcome: DecideOutcome | AmendOutcome,
    via: DecidedVia,
    changed: bool = False,
    replaced_by: uuid.UUID | None = None,
    means: str | None = None,
    by_domo: bool = False,
) -> None:
    """The audit entry of a decision: metadata only, never the arguments of the call. A decision
    that came with a change says so (``changed``), and names the new request it made, if any. A
    yes given by a proof names the means of it (``means``: ``passkey``, ``password``), never what
    was given. Domo's change of a request of his own (``by_domo``) is his: in his name, and
    news for nobody. A decision someone else made is news for the one who asked
    (``conversation.visits``), marked here, in its transaction. The core is not told of it at once
    (``Conversation.notify``): every decision that is news wakes Domo in their conversation
    (``conversation.starts.WAKE_OUTCOMES``), and the wake, which is told it, is the turn that says
    it to whoever listens to them; a news turn would only race it."""
    # Every decision is audited here: a path that skipped this would skip the news too.
    # A request he made on his own that someone else decided is no news for the one in whose
    # conversation he made it, who asked nothing: it wakes him (ruling R33).
    if (
        not by_domo
        and decider.id != c.requester_id
        and c.decided_at is not None
        and not decided_for_him(c)
    ):
        # Imported here: ``conversation.visits`` reads the requests from this package.
        from domo_core.conversation.visits import news_for

        await news_for(session, c.requester_id, now=c.decided_at)
    details: dict[str, str | bool] = {
        "confirmation_id": str(c.id),
        "action_type": action_type_of(c),
        "outcome": outcome,
        "approver_kind": c.approver_kind,
        "via": via,
        "changed": changed,
    }
    if replaced_by is not None:
        details["replaced_by"] = str(replaced_by)
    if means is not None:
        details["means"] = means
    actor = DOMO_LABEL if by_domo else f"person:{decider.id}"
    await append_audit(session, actor=actor, action="confirmation.decided", details=details)


def _of_domo(c: Confirmation) -> bool:
    """Whether what went wrong with ``c`` was the assistant's own: a request he made on his own
    (``by_domo``: a turn of his own, his explicit mark, a call that says it is his), never one
    merely moved to the person it concerns."""
    return c.by_domo or c.idea_reason is not None


async def _note(
    session: AsyncSession,
    c: Confirmation,
    kind: Kind,
    now: datetime,
    *,
    by: uuid.UUID | Literal["domo", "code"] = "code",
    reason: str | None = None,
    action_type: str | None = None,
) -> None:
    """Note a fact of what went wrong with ``c`` (``learning.record.note_went_wrong``), in the
    caller's transaction: about the one who asked, on what it is about. It never stops the
    decision."""
    await note_went_wrong(
        session,
        kind=kind,
        action_type=action_type_of(c) if action_type is None else action_type,
        now=now,
        subject_key=c.subject_key,
        confirmation_id=c.id,
        by=by,
        of_domo=_of_domo(c),
        about=c.requester_id,
        reason=reason,
    )


# Why a yes was refused when no rule refused it: what it asked can no longer be done as it was.
_NO_LONGER_POSSIBLE: Final = "no_longer_possible"
# Why a yes was refused when the rights now ask for the yes of whoever just gave it.
_NOT_ALLOWED: Final = "not_allowed"


async def _expire(
    session: AsyncSession,
    c: Confirmation,
    *,
    by: Literal["domo", "lifetime"],
    actor: str,
    now: datetime,
) -> None:
    """Make the request ``expired`` and audit it (``confirmation.expired``), last: the caller
    commits right after. A silence, never a refusal: the fact noted is ``expired``, by the code
    (its lifetime) or by the assistant (he dropped it)."""
    c.status = "expired"
    c.expired_by = by
    await session.flush()
    await _note(session, c, "expired", now, by="domo" if by == "domo" else "code")
    await append_audit(
        session,
        actor=actor,
        action="confirmation.expired",
        details={
            "confirmation_id": str(c.id),
            "action_type": action_type_of(c),
            "expired_by": by,
        },
    )


async def expire_past_lifetime(
    session: AsyncSession, confirmation_id: uuid.UUID, settings: PolicySettings, now: datetime
) -> bool:
    """Expire the request ``confirmation_id`` if it still waits and has waited as long as the owner
    allows (``is_past_lifetime``), its row locked: the same expiry as the one that happens when
    someone opens it (``_open_pending``), by the code, so one fact is noted and one entry audited,
    whichever comes first, and a request already expired is never expired again. Whether it was
    expired now. Flushed, not committed."""
    c = await _locked(session, confirmation_id)
    if c is None or c.status != "pending" or not is_past_lifetime(c, settings, now):
        return False
    await _expire(session, c, by="lifetime", actor="system", now=now)
    return True


async def _mark_decided(
    session: AsyncSession,
    c: Confirmation,
    decider: Person,
    *,
    via: DecidedVia,
    reply_message_id: uuid.UUID | None,
    now: datetime,
    answer: Literal["yes", "no"] | None,
) -> None:
    """``decider`` answered the request ``c`` (``via``, with their message ``reply_message_id``)
    at ``now``, and ``answer`` is what they answered to its warning, if it had one: ``yes`` or
    ``no``; ``None`` when they answered with a change that was refused, and the whole request
    with it (ruling R48: they answered a change, not the warning). When the one warned (the one
    person the thing belongs to: the person it concerns, who alone answers it, or else the one who
    asked: ``store.warned_one``) answers a request the judge asked to warn them (``warned``), what
    it told them is remembered with their answer (``_remember_the_warning``), for them alone, in
    the caller's transaction: kept when it commits the answer."""
    c.decided_by = decider.id
    c.decided_at = now
    c.decided_via = via
    c.decided_message_id = reply_message_id
    if decider.id == warned_one(c) and answer is not None:
        reason = c.ask_why or CANNOT_BE_UNDONE_REASON
        await _remember_the_warning(session, c, decider, reason, now, answer == "yes")


async def _warned_types(session: AsyncSession, c: Confirmation) -> list[str]:
    """The type of action a warning given for ``c`` is about: its own; for a chain, never the
    chain's own (``chain.run``), which says nothing of its steps (final review M9), but the one
    type of its steps when they all have it. The judge gives one verdict for a whole chain: with
    several types, which one it was about is not known, and it is kept under none (ruling R49:
    it would refresh what was told of another type, which would no longer count as a first
    time)."""
    if action_type_of(c) != _CHAIN_RUN:
        return [action_type_of(c)]
    types = {step.action_type for step in await steps_of(session, c.id)}
    return list(types) if len(types) == 1 else []


async def _remember_the_warning(
    session: AsyncSession,
    c: Confirmation,
    decider: Person,
    reason: str,
    now: datetime,
    approved: bool,
) -> None:
    """``decider``, who was warned, answered ``c``, which the judge asked to warn them of
    ``reason``: keep that Domo told them, with their answer (``memory.told.told``, spec 6.8), and,
    for a no, the fact that a warning was declined (``warning_declined``), one for each type it
    was about (``_warned_types``), in a savepoint of ``session``. A failure is logged and leaves
    the answer as it is: a memory never stops a decision."""
    try:
        async with session.begin_nested():
            for action_type in await _warned_types(session, c):
                await told(
                    session,
                    person_id=decider.id,
                    action_type=action_type,
                    reason=reason,
                    answer="yes" if approved else "no",
                    now=now,
                )
                if not approved:
                    await _note(
                        session,
                        c,
                        "warning_declined",
                        now,
                        by=decider.id,
                        reason=reason,
                        action_type=action_type,
                    )
    except SQLAlchemyError as error:
        logger.warning(
            "What the assistant told a person could not be kept (%s)", type(error).__name__
        )


@dataclass(frozen=True)
class _Open:
    """A request locked for a decision that still waits: the request, the person who decides as
    the database holds them now, and the owner's settings, read again now."""

    c: Confirmation
    person: Person
    settings: PolicySettings


# What a request locked for a decision came to when it cannot be decided: the person may not act
# on it (nothing more is told), or it is over.
_Closed = Literal["not_allowed", "expired", "already_decided"]


async def _open_pending(
    session: AsyncSession,
    confirmation_id: uuid.UUID,
    person_id: uuid.UUID,
    now: datetime,
    *,
    allowed: Callable[[Confirmation, Person], bool],
) -> _Open | _Closed:
    """The start of a decision, the same for a yes or no and for a change: lock the request's
    row, read the person who acts as the database holds them, and check that they may act on it
    (``allowed``; else ``not_allowed``, whatever the reason, the request's state included). A
    request that no longer waits is ``already_decided`` (``executing`` too) or ``expired``; one
    past its lifetime expires now (audited and committed): ``expired``."""
    c = await _locked(session, confirmation_id)
    person = None if c is None else await _person(session, person_id)
    if c is None or person is None or not allowed(c, person):
        return "not_allowed"
    if c.status != "pending":
        return "expired" if c.status == "expired" else "already_decided"
    settings = await load_policy_settings(session)
    if is_past_lifetime(c, settings, now):
        await _expire(session, c, by="lifetime", actor="system", now=now)
        await session.commit()
        return "expired"
    return _Open(c, person, settings)


async def open_for_a_proof(
    session: AsyncSession, confirmation_id: uuid.UUID, person_id: uuid.UUID, now: datetime
) -> tuple[Confirmation, Person] | Literal["not_yours", "expired", "already_decided"]:
    """The start of a proof (``proofs.service.prove``, F10): the request's row is locked and
    read in this order. Someone who may not see it (``may_see``) gets ``not_yours`` and learns
    nothing of it; then its state, as ``decide_confirmation`` reads it (``already_decided``,
    ``expired``; one past its lifetime expires now, audited and committed); then whether the person
    may answer it (``may_decide``), else ``not_yours``. So a second proof of a request already
    answered (from another tab) is told so before any means is read. Returns the request and the
    person as the database holds them; nothing else is written."""
    opened = await _open_pending(
        session,
        confirmation_id,
        person_id,
        now,
        allowed=lambda c, person: may_see(c, person, now),
    )
    if not isinstance(opened, _Open):
        return "not_yours" if opened == "not_allowed" else opened
    if not may_decide(opened.c, opened.person, now):
        return "not_yours"
    return opened.c, opened.person


async def _refuse(
    session: AsyncSession,
    c: Confirmation,
    decider: Person,
    *,
    via: DecidedVia,
    changed: bool = False,
    now: datetime,
    replay: tuple[Actor, DescribedAction, Decision] | None = None,
    judged: Judgement | None = None,
    means: str | None = None,
    said_no: bool = False,
    why: str | None = None,
) -> None:
    """Make the request ``refused`` and commit, with its audit: the judge's verdict when it
    refused a change (``judge.verdict``, with the replay's actor and action), why the barrier
    refused it or what it said when a replay was made (``policy.decision``), then the decision
    (``confirmation.decided``, with the ``means`` of a proof), last.

    The fact of it is noted first, in the same transaction (``_noted_refusal``): who refused it,
    the decider when they said no (``said_no``), the judge when it refused a change, else the
    code (the rules of now, or what can no longer be done: ``why``)."""
    c.status = "refused"
    await session.flush()
    await _noted_refusal(
        session, c, decider, now, replay=replay, judged=judged, said_no=said_no, why=why
    )
    if replay is not None:
        actor, action, decision = replay
        if judged is not None:
            await audit_judgement(
                session, actor=actor, tool_name=c.tool_name, action=action, judgement=judged
            )
        await record_decision(session, actor, action, decision, tool_name=c.tool_name)
    await _audit_decided(
        session, c, decider, outcome="refused", via=via, changed=changed, means=means
    )
    await session.commit()


async def _noted_refusal(
    session: AsyncSession,
    c: Confirmation,
    decider: Person,
    now: datetime,
    *,
    replay: tuple[Actor, DescribedAction, Decision] | None,
    judged: Judgement | None,
    said_no: bool,
    why: str | None,
) -> None:
    """The fact of the refusal of ``c``: ``refused`` by the decider who said no, with their words
    when they said it in words (``refusal_words``: data, never what the request does); the
    judge's refusal of a change (``judge_refused``, with its reason); else ``refused`` by the
    code, with why (a code: the barrier's reason, or ``why``)."""
    if said_no:
        await _note(session, c, "refused", now, by=decider.id, reason=c.refusal_words)
        return
    if judged is not None and judged.verdict == "do_not":
        await _note(session, c, "judge_refused", now, by="domo", reason=judged.reason)
        return
    decision = None if replay is None else replay[2]
    code = decision.reason if isinstance(decision, Refuse) else why
    await _note(session, c, "refused", now, reason=code)


def _why_refused(decision: Refuse, decider: Person, requester_id: uuid.UUID) -> RefusalReason:
    """Why the replay refused a yes: a limit's refusal says so; any other refuses the decider
    when they asked themselves, else the requester (the request can no longer be carried
    out)."""
    match decision.reason:
        case "limit":
            return "limit"
        case "limit_error":
            return "limit_error"
        case _ if decider.id != requester_id:
            return "no_longer_possible"
        case _:
            return "not_allowed"


def _refusal_of(decision: Decision) -> str | None:
    """The summary of the limit that refused, if one did."""
    return decision.limit if isinstance(decision, Refuse) else None


def _not_enough(
    needed: Ask | None, via: DecidedVia
) -> Literal["needs_explicit", "needs_proof"] | None:
    """Whether a yes given ``via`` does not give what the request asks (``needed``): only a proof
    gives a proof (``needs_proof``), and only the button, a firm yes in words or a proof give an
    explicit yes (``needs_explicit``). ``None`` when it is enough."""
    if needed == "proof" and via != "proof":
        return "needs_proof"
    if needed == "explicit_yes" and via in _NOT_EXPLICIT:
        return "needs_explicit"
    return None


def what_the_yes_gave(via: DecidedVia) -> Ask | None:
    """What a yes given ``via`` gives (as ``_not_enough`` reads it): a proof gives a proof, the
    button and a firm yes in words an explicit yes, other words a yes; the judge's leave
    (``judge``: nobody said yes) gives nothing."""
    if via == "judge":
        return None
    if via == "proof":
        return "proof"
    return "yes" if via in _NOT_EXPLICIT else "explicit_yes"


def _raise_to(c: Confirmation, rule: Ask | None) -> bool:
    """Raise what the request ``c`` asks to what the owner's rules (``rule``) ask now, when that
    is more demanding (F21: a rule added since the request was made counts at its yes):
    ``asked_by`` ``rule``, with no reason. Whether it was raised."""
    stored = ask_of(c)
    if rule is None or most_demanding(stored, rule) == stored:
        return False
    c.ask = rule
    c.asked_by = "rule"
    c.ask_why = None
    return True


async def _what_a_chain_asks_now(
    session: AsyncSession,
    c: Confirmation,
    actor: Actor,
    decision: Allow | NeedsApproval,
    policy: PolicyTables,
) -> Ask | None:
    """What the owner's rules ask now of the chain ``c``, for the requester (``actor``): the most
    demanding of its own action's (``decision``, the replay of the chain's action) and of each of
    its steps still to run, each resolved by its type with the rights in force."""
    rights = await load_rights(session)
    rules: list[Ask | None] = [decision.rule]
    for row in await steps_of(session, c.id):
        if row.status == "done":
            continue
        rules.append(rights.resolve(actor, row.action_type, policy).ask)
    return most_demanding(*rules)


def _the_rights_ask_again(
    decision: Allow | NeedsApproval,
    c: Confirmation,
    person: Person,
    requester_id: uuid.UUID,
    *,
    for_others: bool,
) -> bool:
    """Whether a change of the single request ``c`` by ``person`` is a new request whatever the
    judge would say, because the rights decide whose yes it needs: the one who asked changed their
    request that waits for someone else's yes (spec 5.2: they give it none); the barrier asks for
    an agreement ``person`` cannot give (Ruling R27); or ``person``, an approver, changed it while
    nobody else's agreement is needed any more, and it asks a yes of the one who asked for the
    action itself (``_the_requesters_own_yes``)."""
    if for_others:
        return True
    if isinstance(decision, NeedsApproval):
        return not approves(decision.approver, person, requester_id)
    return _the_requesters_own_yes(asked_of(c), person, requester_id)


# What Domo asks when he asks a change again (``ask_again``): a plain yes, with this reason.
NOT_SURE_REASON: Final = "The assistant is not sure"
_NOT_SURE: Final[tuple[Ask, str]] = ("yes", NOT_SURE_REASON)


def _raised_with(raised: tuple[Ask, str] | None, ask_again: bool) -> tuple[Ask, str] | None:
    """What Domo asks more of a change: what he asked for the next action of his answer
    (``ask_before_acting``, ``raised``: it asks at least a yes), else, when he asks the change
    again (``ask_again``), a plain yes because he is not sure (F15)."""
    if raised is not None:
        return raised
    return _NOT_SURE if ask_again else None


def _unmark(c: Confirmation) -> None:
    """Nothing of a yes that changed nothing is kept on ``c``."""
    c.decided_by = None
    c.decided_at = None
    c.decided_via = None
    c.decided_message_id = None


async def _described_again(
    tool: InternalTool, c: Confirmation, arguments: JsonObject, actor: Actor, ctx: ToolContext
) -> DescribedAction | None:
    """The stored call (``arguments``) described again now by its ``tool``, for the requester
    (``actor``, in their context ``ctx``): the facts a limit reads about the present come from
    now, not from when the request was made (Ruling R6). ``None`` when the tool no longer accepts
    the call, fails, or takes more than ``_DESCRIBE_SECONDS`` (describing changes nothing)."""
    with anyio.move_on_after(_DESCRIBE_SECONDS):
        try:
            return await tool.describe(arguments, actor, ctx)
        except InvalidArguments:
            return None
        except Exception as error:
            logger.error(
                "A confirmed %s could not be described again (%s)",
                c.tool_name,
                type(error).__name__,
            )
            return None
    logger.error("Describing a confirmed %s again took too long", c.tool_name)
    return None


def _yes_of_someone_else(
    decision: Decision, decider: Person, requester_id: uuid.UUID
) -> NeedsApproval | None:
    """The barrier's decision, replayed for the requester, when it asks for an agreement the
    decider cannot give (the requester was made a child, say, a right gave the yes to the owner):
    the request then waits for that agreement (Ruling R27). ``None`` when the decider's yes is
    the one it asks for, or it asks for none."""
    if isinstance(decision, NeedsApproval) and not approves(
        decision.approver, decider, requester_id
    ):
        return decision
    return None


def _yes_not_covering(
    decision: Decision, c: Confirmation, decider: Person, requester_id: uuid.UUID
) -> NeedsApproval | None:
    """For a chain: the replay of its own action, when it asks for a more demanding agreement
    than the one ``decider`` gave to it as an approver of ``c``'s kind (``store.covers``); the
    chain then waits for that agreement (Ruling R27). ``None`` when the decider's yes covers it,
    or it asks for none."""
    if isinstance(decision, NeedsApproval) and not covers(
        c.approver_kind, decision.approver, decider, requester_id
    ):
        return decision
    return None


def rerouted_yes(c: Confirmation) -> ActionRecord:
    """The record of a yes that was not the one the request needs now (``_reroute``): the
    request waits for the yes of ``c``'s approver now. Its card says whose
    (``confirmation.outcome.approver_changed.<approver>``), and so does what Domo would read.

    Nothing was done, unless the yes ran a chain of actions that went back to waiting on the way:
    what its steps done made for the one who asked waits behind their buttons, which the card of
    the request shows them from its rows (``confirmations.routes``), as in any state."""
    approver = approver_of(c)
    return ActionRecord(
        tool_name=c.tool_name,
        action_type=action_type_of(c),
        status="waiting",
        confirmation_id=c.id,
        model_result={"status": "waiting", "reason": "approver_changed", "by": who_answers(c)},
        card={
            "key": f"confirmation.outcome.approver_changed.{approver}",
            "params": {},
            "reason": "approver_changed",
            "approver": approver,
        },
    )


async def _count_for_domo(
    session: AsyncSession, c: Confirmation, actor: Actor, action: DescribedAction
) -> None:
    """A yes lets ``c`` through, judged for ``actor`` as ``action`` now: when it is Domo's own
    act or a note in his name (``barrier.writes_for_domo``) that his history has not counted yet
    (``Confirmation.counted_for_domo``: the barrier asked someone's agreement when it was made,
    whatever the judge asked with it), one ``allow`` in his name, whatever the replay says, and
    it is counted (final review, Task 9 N2; Task 7). So it counts once, however many approvers it
    was sent to on the way (``_reroute``). A call that carries a yes (its ``yes`` fact) is never
    counted again: its first call was (``policy.facts``). The person's history counts a note in
    his name as their request done after a yes."""
    if c.counted_for_domo or not writes_for_domo(actor, action):
        return
    if action.facts.get("yes") is True:
        return
    await record_decision(session, domo_actor(), action, Allow(), tool_name=c.tool_name)
    c.counted_for_domo = True


def _the_requesters_own_yes(asked: Asked | None, decider: Person, requester_id: uuid.UUID) -> bool:
    """For a request the barrier now lets through without anyone else's agreement (the owner
    lifted the limit that sent it to them, say): whether the yes it asks (``asked``) is the
    requester's own, which ``decider``, asked as an approver, cannot give. It is when the request
    asks a yes for the action itself (asked by other than ``approver``: the judge, a rule, Domo,
    the owner by default) and ``decider`` is not the one who asked (Ruling R27): the request then
    waits for their yes. Only an approver's agreement was asked otherwise, and none is needed now.
    """
    return decider.id != requester_id and asked is not None and asked.by != "approver"


async def _reroute(
    session: AsyncSession,
    c: Confirmation,
    decider: Person,
    approver: Approver,
    question: str | None,
    *,
    replay: tuple[Actor, DescribedAction, Decision],
) -> None:
    """The yes of ``decider`` is not the one the request needs now (``_yes_of_someone_else``,
    ``_the_requesters_own_yes``): the request is not refused (Ruling R27). It waits for the
    agreement the barrier asks for, or the requester's own yes (``approver_kind``), with what
    Domo could not tell when a limit sends it to the owner (``ask_reason``: the right's
    ``question``, else nothing), and it keeps what it asks and its age, and so its lifetime.
    Nothing of the yes is kept on it. The audit says what the barrier said (``policy.decision``),
    then ``confirmation.rerouted`` (the request, its type, who says yes now and before), last.
    Committed."""
    before = c.approver_kind
    c.approver_kind = approver
    c.ask_reason = question
    if approver != "self":
        # The barrier's approver wins: the person it concerned no longer answers it alone.
        c.concerned_id = None
    _unmark(c)
    await session.flush()
    actor, action, decision = replay
    await record_decision(session, actor, action, decision, tool_name=c.tool_name)
    if isinstance(decision, Allow) and writes_for_domo(actor, action):
        # Let through by the barrier now, it is in his history from this line: counted once
        # (``_count_for_domo`` writes no other at its yes; fix round 1, I1).
        c.counted_for_domo = True
    await append_audit(
        session,
        actor=f"person:{decider.id}",
        action="confirmation.rerouted",
        details={
            "confirmation_id": str(c.id),
            "action_type": action_type_of(c),
            "approver_kind": c.approver_kind,
            "before": before,
        },
    )
    await session.commit()


async def _run_and_settle(
    session: AsyncSession,
    session_factory: SessionFactory,
    c: Confirmation,
    *,
    tool: InternalTool,
    action: DescribedAction,
    arguments: JsonObject,
    ctx: ToolContext,
    decider: Person,
    via: DecidedVia,
    changed: bool = False,
    means: str | None = None,
    by_domo: bool = False,
) -> tuple[Literal["executed", "failed"], ActionRecord]:
    """The end of a yes, the same for a decision and for a change. The caller made the request
    ``executing`` on ``session`` (which holds its row lock), with what goes with it (and, for a yes
    given by a proof, its ``means``, for the audit; for a change of his own request that runs with
    nobody's yes, ``by_domo``: decided in his name).

    From here on it is one unit, shielded from cancellation: the commit that makes the request
    ``executing``; the call, run in ``ctx`` (the requester's), with the session closed first (no
    lock and no connection are held while a tool runs); and the record of what came of it
    (``settle``; not for a chain that went back to waiting for someone else's yes on the way,
    which is not over). So no cancellation can leave a request ``executing`` with its action never
    run, or never recorded. Each step keeps its own time limit: the run's
    (``conversation.execution.execute_allowed``) and the record's (``_SETTLE_SECONDS``).

    The tool is told the ``action`` the yes was given to (``ToolContext.described_action``), so
    that it acts on that target and no other, who said yes (``ToolContext.approved_by``), so
    that what it makes for the one who asked (a link, a form) is theirs when someone else did, and
    the request (``ToolContext.request_id``): a chain runs its steps from their rows."""
    with anyio.CancelScope(shield=True):
        await session.commit()
        await session.close()
        record = await execute_allowed(
            tool,
            c.tool_name,
            action.action_type,
            arguments,
            # The message of the turn that made the call is not kept: never the one of the yes.
            replace(
                ctx,
                described_action=action,
                approved_by=decider.id,
                request_id=c.id,
                said=None,
                said_later=(),
            ),
        )
        record = replace(record, confirmation_id=c.id)
        outcome: Literal["executed", "failed"] = "executed" if record.status == "done" else "failed"
        if not waits_again(record):
            # (A chain that waits for someone else's yes again is not over: nothing to settle.)
            await settle(
                session_factory,
                c.id,
                record,
                decider,
                outcome=outcome,
                via=via,
                changed=changed,
                means=means,
                by_domo=by_domo,
                now=ctx.clock(),
            )
        return outcome, record
    # A shield without a deadline that nobody cancels never stops what it holds.
    raise AssertionError("unreachable")


# How many requests of others that wait for someone's yes are shown at once (the approvals zone,
# and the context of a turn): the others wait their turn.
MAX_APPROVALS_SHOWN: Final = 20


def requester_context(base: ToolContext) -> Callable[[Person, uuid.UUID], ToolContext]:
    """A ``ctx_factory`` for ``decide_confirmation``: ``base`` (the moment of the decision) made
    the requester's, in the request's conversation (the action is theirs, whoever approved it).
    The plans due in the turn of the decision, if any, are not the request's: they are left out
    (``due_plans``, ``asked_before``); and the request runs as the requester's, never as an act of
    Domo on his own (``on_his_own``: a yes a person gave is theirs)."""

    def build(requester: Person, conversation_id: uuid.UUID) -> ToolContext:
        return replace(
            base,
            actor=Actor(person_id=requester.id, role=as_role(requester.role)),
            person=requester,
            conversation_id=conversation_id,
            due_plans=(),
            asked_before=False,
            on_his_own=False,
        )

    return build


def _in_their_turn(policy: PolicyTables) -> PolicyTables:
    """``policy`` as it is in a person's turn, for a request of Domo's born of his mark there
    that he changes in a turn of his own (whose ``policy`` is ``on_his_own_tables``'s): what is
    about the person's own things is his again as his default rights say (``DOMO_ACTIONS``)."""
    return replace(policy, domo=policy.domo | (DOMO_ACTIONS & NOT_ON_HIS_OWN))


async def idea_decided_with_the_speaker(
    session: AsyncSession,
    speaker: Actor,
    action: DescribedAction,
    policy: PolicyTables,
    domo: Decision,
    now: datetime,
) -> Decision:
    """The barrier's decision on an idea of Domo's made in ``speaker``'s turn, replayed (ruling
    R26, as ``conversation.execution`` decides it at first): his own decision (``domo``) and the
    one for the speaker, a right merely not given counted as given (``barrier.given_for_an_idea``),
    the stricter standing (``barrier.stricter``): what the owner wrote for them or their role, and
    an adult's agreement for a child, still hold."""
    theirs = given_for_an_idea(policy, speaker.role, action.action_type)
    person = await decide_now(session, speaker, action, theirs, now)
    return stricter(person, domo)


async def _made_tool(ctx: ToolContext, name: str) -> InternalTool | None:
    """The tool of Domo's called ``name`` (``ToolContext.made_tool_named``), when the context
    of the decision finds them; ``None`` otherwise, or when it cannot be read (logged)."""
    find = ctx.made_tool_named
    if find is None:
        return None
    try:
        return await find(name)
    except Exception as error:
        logger.error("A tool of the assistant could not be looked for (%s)", type(error).__name__)
        return None


async def decide_confirmation(
    *,
    session_factory: SessionFactory,
    confirmation_id: uuid.UUID,
    decider: Person,
    approve: bool,
    via: DecidedVia,
    reply_message_id: uuid.UUID | None,
    ctx_factory: Callable[[Person, uuid.UUID], ToolContext],
    registry: Mapping[str, InternalTool],
    policy: PolicyTables,
    now: datetime,
    means: str | None = None,
    speaker: Speaker | None = None,
    words: str | None = None,
) -> tuple[DecideOutcome, ActionRecord | None]:
    """Carry out ``decider``'s yes (``approve``) or no on the request (see the module docstring).

    A no said in words (``via`` a phrase or the classifier) keeps the person's message, ``words``,
    cut short and without the shape of a secret (``Confirmation.refusal_words``: what they said
    when they refused it, data, never what the request does); a button has none.

    Returns the outcome and, when the stored call ran, its record (status, card, ``card_once``,
    what the model would read), with ``confirmation_id`` set; when a yes ended ``refused``, a
    ``refused`` record whose card's ``reason`` says why (``RefusalReason``); when the request
    now needs someone else's yes (``approver_changed``), a ``waiting`` record whose card says
    whose (``rerouted_yes``): what the steps a chain did before it went back to waiting made for
    the one who asked waits behind their buttons, on the card of the request.
    ``ctx_factory(requester, conversation_id)`` builds the context the call runs in: the
    requester's. ``registry`` and ``policy`` (the catalog under the owner's rights, which are
    read again now) are the ones the barrier is replayed with. A request that does not exist is
    ``not_yours``: nobody learns which ids exist. A yes given by a proof (``via`` ``proof``, from
    ``proofs.service.prove``, which checked it) names its ``means`` in the audit entry of the
    decision, and, when the ``speaker`` it was given by is known, is kept as one more clue that it
    is the decider (``identity.clues.keep_proof_clue``), for the channel and the device it was given
    on.
    """
    if approve and via == "proof" and speaker is not None and means is not None:
        async with session_factory() as clues:
            await keep_proof_clue(
                clues,
                person_id=decider.id,
                channel=speaker.channel,
                device_id=speaker.device_id,
                means=means,
                now=now,
            )
            await clues.commit()
    async with session_factory() as session:
        opened = await _open_pending(
            session,
            confirmation_id,
            decider.id,
            now,
            allowed=lambda c, person: may_decide(c, person, now),
        )
        if not isinstance(opened, _Open):
            return ("not_yours" if opened == "not_allowed" else opened), None
        c, current = opened.c, opened.person
        if approve:
            # A second lock on the same door: a yes that does not give what the request asks
            # changes nothing.
            early = _not_enough(ask_of(c), via)
            if early is not None:
                return early, None
        await _mark_decided(
            session,
            c,
            current,
            via=via,
            reply_message_id=reply_message_id,
            now=now,
            answer="yes" if approve else "no",
        )
        if not approve:
            if via in _SAID_IN_WORDS:
                c.refusal_words = kept_reason(words)
            await _refuse(session, c, current, via=via, now=now, means=means, said_no=True)
            return "refused", None

        # A yes: the barrier again, for the requester, with the stored call described again.
        requester = await _person(session, c.requester_id)
        # Where it runs: the conversation of the turn it was first made in, the requester's (for
        # a request that waited for the person it concerns in their own conversation, not that
        # one); unknown, it runs nowhere (below).
        made_in = await conversation_made_in(session, c)
        tool = registry.get(c.tool_name)
        if tool is None and requester is not None and made_in is not None:
            # A tool of Domo's (``t_<name>``, a launch that carries a yes): found as a turn finds
            # one it did not offer, whichever way the yes is given (words, a button, a proof).
            tool = await _made_tool(ctx_factory(requester, made_in), c.tool_name)
        try:
            action: DescribedAction | None = DescribedAction.from_json(c.action)
        except ValueError:
            action = None
        if action is None or tool is None or tool.action_type != action.action_type:
            # The stored call can no longer be run as it was described: it is never guessed.
            logger.error("A confirmed %s could not be run as stored", c.tool_name)
            c.status = "failed"
            c.result = failed_record(c.tool_name, action_type_of(c)).card
            await session.flush()
            await _note(session, c, "failed", now, reason=_NO_LONGER_POSSIBLE)
            await _audit_decided(session, c, current, outcome="failed", via=via, means=means)
            await session.commit()
            return "failed", None
        if requester is None or not is_active(requester, now) or made_in is None:
            # Gone, or the turn it was made in can no longer be known: never run in someone
            # else's conversation as theirs.
            await _refuse(
                session, c, current, via=via, now=now, means=means, why=_NO_LONGER_POSSIBLE
            )
            return "refused", _refused_yes(c, "no_longer_possible")
        # Who asked: the requester, or Domo for a request he made on his own (judged and run
        # as his, on his own, with what is about the owner's own things not his by default), or
        # born of his mark in a person's turn (judged and run as his in that turn, the
        # requester's, whose socle holds as at first: ruling R30; a requester who left was
        # refused above, never replaced by whoever answers). One born of the owner's rule on his
        # act in a person's turn (``in_their_turn``) is the owner's to answer, and runs in that
        # turn, for that person, as it was judged (re-review R1).
        asker = requester_of(c)
        idea_speaker: Actor | None = None
        if made_in_a_turn(c):
            actor = domo_actor()
            ctx = ctx_factory(requester, made_in)
            idea_speaker = Actor(requester.id, as_role(requester.role))
        elif replayed_on_his_own(c):
            actor = domo_actor()
            policy = on_his_own_tables(policy)
            theirs = ctx_factory(requester, made_in)
            ctx = replace(theirs, on_his_own=True, policy=on_his_own_tables(theirs.policy))
        else:
            ctx = ctx_factory(requester, made_in)
            # Who the call was judged as when it was asked (final review I3): Domo for his own
            # acts (``program_run``, the ``tool_*``), whose rights of now (a "no more" of the
            # owner's since) decide, and whose history and audit it is; the requester otherwise.
            actor = judged_actor_of(tool, ctx)
        # What the card describes is what runs (R75).
        arguments = effective_arguments(c)
        chain = c.chain_basis is not None
        if chain:
            # Each step of a chain is described again and judged in its turn, after the yes
            # (``confirmations.chain``); the chain's own action is replayed as stored.
            now_action: DescribedAction | None = action
        else:
            now_action = await _described_again(tool, c, arguments, actor, ctx)
        if now_action is None or (now_action.action_type, now_action.target) != (
            action.action_type,
            action.target,
        ):
            # The call no longer describes what the yes was given to.
            await _refuse(
                session, c, current, via=via, now=now, means=means, why=_NO_LONGER_POSSIBLE
            )
            return "refused", _refused_yes(c, "no_longer_possible")
        if c.concerned_id is not None and not await _still_concerns(
            session, c, now_action, requester.id, now
        ):
            # The thing is someone else's now (given away, entrusted): this yes no longer
            # answers for it.
            await _refuse(
                session, c, current, via=via, now=now, means=means, why=_NO_LONGER_POSSIBLE
            )
            return "refused", _refused_yes(c, "no_longer_possible")
        decision = await decide_now(session, actor, now_action, policy, now)
        if idea_speaker is not None:
            decision = await idea_decided_with_the_speaker(
                session, idea_speaker, now_action, policy, decision, now
            )
        replay = (actor, now_action, decision)
        if isinstance(decision, Refuse):
            await _refuse(session, c, current, via=via, now=now, replay=replay, means=means)
            reason = _why_refused(decision, current, asker)
            return "refused", _refused_yes(c, reason, limit=_refusal_of(decision))
        need = (
            _yes_not_covering(decision, c, current, asker)
            if chain
            else _yes_of_someone_else(decision, current, asker)
        )
        if need is not None:
            if need.approver == c.approver_kind:
                # ``may_decide`` let the decider give this kind of yes a moment ago, so this is
                # never reached; were it, it is never guessed: refused.
                await _refuse(
                    session,
                    c,
                    current,
                    via=via,
                    now=now,
                    replay=replay,
                    means=means,
                    why=_NOT_ALLOWED,
                )
                return "refused", _refused_yes(c, "not_allowed")
            await _reroute(session, c, current, need.approver, need.question, replay=replay)
            return "approver_changed", rerouted_yes(c)
        # What the request asks now (F21): a rule added since it was made counts.
        rule = (
            await _what_a_chain_asks_now(session, c, actor, decision, policy)
            if chain
            else decision.rule
        )
        raised = _raise_to(c, rule)
        if (
            not chain
            # Domo's own yes is nobody's: the yes of whoever may answer his request stands.
            and not c.by_domo
            # The person it concerns answers it alone, not the one who asked.
            and c.concerned_id is None
            and isinstance(decision, Allow)
            and _the_requesters_own_yes(asked_of(c), current, asker)
        ):
            # Nobody else's agreement is needed any more, and the yes it asks is theirs (a chain's
            # approver's yes covers its requester's own: ``store.covers``).
            await _reroute(session, c, current, "self", None, replay=replay)
            return "approver_changed", rerouted_yes(c)
        not_enough = _not_enough(ask_of(c), via)
        if not_enough is not None:
            # This yes does not give it: nothing changes but what the request asks now.
            _unmark(c)
            if raised:
                await session.commit()
            return not_enough, None
        c.status = "executing"
        await _count_for_domo(session, c, actor, now_action)
        outcome, record = await _run_and_settle(
            session,
            session_factory,
            c,
            tool=tool,
            action=action,
            arguments=arguments,
            # Who it was judged as, for a tool that records who acted (a chain's steps are
            # judged each in its turn).
            ctx=ctx if chain else replace(ctx, judged_as=actor),
            decider=current,
            via=via,
            means=means,
        )
        if chain and paused(record):
            # The chain did its steps up to one that waits for someone's arrival: it pauses there
            # until the upkeep resumes it (``confirmations.arrivals``).
            return "paused", record
        if chain and waits_again(record):
            # A step asked for a more demanding yes than this one: the chain waits for it now
            # (what the steps done so far made for the one who asked waits behind their buttons).
            async with session_factory() as reader:
                waiting = (await confirmations_by_id(reader, [c.id]))[c.id]
            return "approver_changed", rerouted_yes(waiting)
        return outcome, record


async def settle(
    session_factory: SessionFactory,
    confirmation_id: uuid.UUID,
    record: ActionRecord,
    decider: Person,
    *,
    outcome: Literal["executed", "failed"],
    via: DecidedVia,
    changed: bool = False,
    means: str | None = None,
    by_domo: bool = False,
    now: datetime,
) -> None:
    """Phase 3: store what came of the approved action, and audit the decision (last, with the
    ``means`` of a proof; in his name, ``by_domo``, for a change of his own that ran). One
    attempt, limited to ``_SETTLE_SECONDS``; a failure is logged, and the request stays
    ``executing`` (shown as ``running``: its outcome is not known): the action ran (or failed),
    and is not run again. A request that is no longer ``executing`` is left as it is: a chain of
    actions that went back to waiting for someone else's yes on the way
    (``confirmations.chain``) is not over. ``tools.chain.ChainContinue`` settles the rest of a
    chain it continued the same way. A request that ``failed`` is a fact of what went wrong
    (``failed``, by the code, its reason only when it is a code), noted in the same transaction,
    at ``now``, when it failed (a chain taken up again may fail long after its yes). A person's
    request that ran may undo what the assistant did or decided (``_undoing_noted``, R56)."""
    with anyio.move_on_after(_SETTLE_SECONDS):
        try:
            async with session_factory() as session:
                c = await _locked(session, confirmation_id)
                if c is None:
                    raise LookupError("the confirmation is gone")
                if c.status != "executing":
                    return
                c.status = outcome
                c.result = dict(record.card)
                await session.flush()
                if outcome == "failed":
                    # A request that ran and failed, at ``now``: a fact, its reason only when it
                    # is a code.
                    await _note(session, c, "failed", now, reason=reason_code(record.model_result))
                else:
                    await _undoing_noted(session, c, record, now)
                await _audit_decided(
                    session,
                    c,
                    decider,
                    outcome=outcome,
                    via=via,
                    changed=changed,
                    means=means,
                    by_domo=by_domo,
                )
                await session.commit()
            return
        except Exception as error:
            logger.error(
                "What came of the confirmed %s (%s) could not be stored (%s)",
                record.tool_name,
                outcome,
                type(error).__name__,
            )
            return
    logger.error(
        "Storing what came of the confirmed %s (%s) took too long", record.tool_name, outcome
    )


async def _undoing_noted(
    session: AsyncSession, c: Confirmation, record: ActionRecord, now: datetime
) -> None:
    """What a person's request that ran after a yes undoes of the assistant's
    (``learning.undone.note_undoing``, R56), as an action run at once does
    (``conversation.execution.store_action``): by the one who asked, never for a request of his
    (``by_domo``, or his reason). No words are kept with it: the yes may be someone else's. The
    action is the one that ran (its stored description, its result for the keys); nothing when
    it cannot be read."""
    if c.by_domo or c.idea_reason is not None:
        return
    try:
        action = DescribedAction.from_json(c.action)
    except ValueError:
        return
    await note_undoing(
        session,
        action_type=action.action_type,
        keys=keys_of(action, record.model_result),
        by=requester_of(c),
        now=now,
        reason=None,
        ran_from=c.id,
    )


# --- what Domo does with the requests ----------------------------------------------------------


async def _own_pending(
    session: AsyncSession,
    confirmation_id: uuid.UUID,
    requester_id: uuid.UUID,
    conversation_id: uuid.UUID,
    now: datetime,
    *,
    paused: str | None,
    concerned_too: bool = False,
) -> Confirmation:
    """The request, locked, if it is one of ``requester_id`` in this conversation that waits for a
    yes, still under its lifetime; ``InvalidArguments("not_found")`` otherwise (whatever the
    reason: nobody learns whether the id exists, nor whose it is). A chain of theirs paused until
    someone's arrival (``waiting_arrival``, which lives while its invitation can be accepted) is
    managed too when ``paused`` is ``None``; otherwise ``paused`` is what Domo is told
    (``InvalidArguments("invalid")``): it waits for no yes."""
    c = await _locked(session, confirmation_id)
    mine = c is not None and (
        _shows_it(c, requester_id, conversation_id)
        if concerned_too
        else (requester_of(c), c.conversation_id) == (requester_id, conversation_id)
    )
    if c is None or not mine:
        raise InvalidArguments("not_found", _NOT_PENDING)
    if c.status == "waiting_arrival":
        if paused is not None:
            raise InvalidArguments("invalid", paused)
        return c
    if c.status != "pending" or is_past_lifetime(c, await load_policy_settings(session), now):
        raise InvalidArguments("not_found", _NOT_PENDING)
    return c


def _shows_it(c: Confirmation, person_id: uuid.UUID, conversation_id: uuid.UUID) -> bool:
    """Whether ``c`` is a request of ``person_id``'s own in this conversation, as their reply
    shows it: one they asked, or one that waits there for them alone as the person it concerns
    (``concerned_id``)."""
    if c.conversation_id != conversation_id:
        return False
    return requester_of(c) == person_id or c.concerned_id == person_id


async def remind(
    session: AsyncSession,
    *,
    confirmation_id: uuid.UUID,
    requester_id: uuid.UUID,
    conversation_id: uuid.UUID,
    now: datetime,
    shown_in: uuid.UUID | None = None,
    approver: Person | None = None,
) -> Confirmation:
    """Count a reminder of the request (``reminder_count``, ``last_reminded_at``). ``shown_in``
    is the reply that presents it again: the person's answer to that reply may decide it in
    words (R74). ``InvalidArguments("not_found")`` unless it is a request of ``requester_id``
    that waits in this conversation; ``InvalidArguments("invalid")`` for a chain of theirs paused
    until someone's arrival (nothing answers it). A request of theirs that waits for someone
    else's yes (an adult's, the owner's) is nudged instead (``nudged_at``, spec 5.2; counted, but
    ``last_reminded_at`` does not move): it comes first in its approver's zone, and their Domo is
    told until he shows it to them (``store.still_nudged``); the words of the one who asked never
    answer it, so ``shown_in`` moves nothing then.

    With ``approver`` (the person, as one who may say yes to the requests of others), a request
    that is not one of ``requester_id``'s in this conversation may be someone else's that waits
    for their yes (``store.pending_to_approve``, spec 5.1; ``not_found`` otherwise): ``shown_in``
    is then the approver's reply that presents it (``shown_to_approver_message_id``), and what the
    one who asked was shown does not move. Flushed, not committed."""
    c = await _locked(session, confirmation_id)
    own = c is not None and _shows_it(c, requester_id, conversation_id)
    if not own and approver is not None:
        c = await pending_to_approve(session, confirmation_id, approver, now)
        if shown_in is not None:
            c.shown_to_approver_message_id = shown_in
    else:
        c = await _own_pending(
            session,
            confirmation_id,
            requester_id,
            conversation_id,
            now,
            paused=_PAUSED_NOT_SHOWN,
            # Shown to the person it concerns too, in their conversation: their words answer it.
            concerned_too=True,
        )
        if c.approver_kind != "self":
            # It waits for someone else's yes: the one who asked nudges its approver (spec 5.2).
            # Their words never answer it, so what they were shown does not move; nor was it shown
            # to its approver (``store.still_nudged`` reads ``last_reminded_at`` for that).
            c.nudged_at = now
            c.reminder_count += 1
            await session.flush()
            return c
        if shown_in is not None:
            c.last_shown_message_id = shown_in
    c.reminder_count += 1
    c.last_reminded_at = now
    await session.flush()
    return c


async def asker_on_his_own(
    session: AsyncSession, ctx: ToolContext, confirmation_id: uuid.UUID, action_type: str
) -> uuid.UUID:
    """In a turn Domo acts in on his own (``ctx.on_his_own``), whose requests ``action_type``
    (``confirmation.expire``, ``confirmation.amend``) reaches, as ``store.requester_of`` names
    them: his own (``DOMO_SUBJECT_ID``) always (spec 12.5); the person's whose conversation it
    is (``ctx.person``) when ``confirmation_id`` names a request of theirs and a right the owner
    gave him opens their requests to him (``catalog.on_his_own_requests``, ruling R30). A request
    that does not exist is read as his: nobody learns whether an id exists."""
    found = (await confirmations_by_id(session, [confirmation_id])).get(confirmation_id)
    if found is None or found.by_domo:
        return DOMO_SUBJECT_ID
    rights = await load_rights(session)
    opened = rights.resolve(domo_actor(), action_type, on_his_own_requests(ctx.policy))
    return ctx.person.id if opened.allowed else DOMO_SUBJECT_ID


async def expire_by_domo(
    session: AsyncSession,
    *,
    confirmation_id: uuid.UUID,
    requester_id: uuid.UUID,
    conversation_id: uuid.UUID,
    now: datetime,
) -> Confirmation:
    """Drop the request: it becomes ``expired`` (``expired_by="domo"``) and can no longer be
    approved. The audit entry ``confirmation.expired`` is written last (the caller commits right
    after), in the name of the one who asked. ``InvalidArguments("not_found")`` unless it is a
    request of ``requester_id`` that waits in this conversation, or a chain of theirs paused until
    someone's arrival (the invitation it made stays). ``requester_id`` is ``DOMO_SUBJECT_ID``
    for his own requests (``store.requester_of``), which he drops in a turn of his own (spec 12.5):
    a person's are never his to drop there, nor his theirs."""
    c = await _own_pending(
        session, confirmation_id, requester_id, conversation_id, now, paused=None
    )
    asker = DOMO_LABEL if requester_id == DOMO_SUBJECT_ID else f"person:{requester_id}"
    await _expire(session, c, by="domo", actor=asker, now=now)
    return c


def short_plain_line(value: object, max_length: int, what: str) -> str:
    """``value``, a text Domo wrote, without the spaces around it: ``InvalidArguments`` unless
    it is 1 to ``max_length`` characters on one plain line (no control character, line break or
    direction mark). ``what`` names it in the detail ("the reason"), which never repeats it."""
    if not isinstance(value, str):
        raise InvalidArguments("invalid", f"{what} is a text")
    line = value.strip()
    if not line:
        raise InvalidArguments("invalid", f"{what} is empty")
    if len(line) > max_length:
        raise InvalidArguments("too_long", f"{what} has more than {max_length} characters")
    if not is_plain_line(line):
        raise InvalidArguments(
            "invalid", f"{what} has a control character, a line break or a direction mark"
        )
    return line


def explicit_reason_from(value: object) -> str:
    """The reason Domo gives for an explicit check (``short_plain_line``, 1 to
    ``MAX_EXPLICIT_REASON_LENGTH`` characters): it is shown on the request's card, to the
    person."""
    return short_plain_line(value, MAX_EXPLICIT_REASON_LENGTH, "the reason")


def approver_note_from(value: object) -> str:
    """What Domo keeps of what an approver said with a change, and how firm they were
    (``short_plain_line``, 1 to ``MAX_APPROVER_NOTE_LENGTH`` characters): it reaches the one who
    asked, as data (spec 5.1). It never carries a secret or a link: anything that looks like a
    link (``http://``, ``https://``, ``www.``) is ``InvalidArguments``."""
    note = short_plain_line(value, MAX_APPROVER_NOTE_LENGTH, "the note")
    if _LINK.search(note) is not None:
        raise InvalidArguments(
            "invalid", "the note holds what looks like a link: it never carries a link or a secret"
        )
    return note


async def require_explicit(
    session: AsyncSession,
    *,
    confirmation_id: uuid.UUID,
    requester_id: uuid.UUID,
    conversation_id: uuid.UUID,
    reason: str,
    now: datetime,
    shown_in: uuid.UUID | None = None,
) -> tuple[Confirmation, bool]:
    """Make the request ask an explicit yes (a firm yes in words, or the button), asked by Domo
    (``asked_by`` ``domo``), and keep the ``reason`` (1 to 200 characters, ``ask_why``), which
    its card shows; a request that asks as much or more already (an explicit yes, a proof) is
    left as it is: Domo asks more, never less. ``shown_in`` is the reply that presents it again
    (R74, as for ``remind``). The audit entry ``confirmation.explicit_required``
    (``{"confirmation_id", "action_type"}``: never the reason) is written last, when it was
    raised. ``InvalidArguments`` for a reason that is not one short plain line, and ``not_found``
    unless it is a request of ``requester_id`` that waits in this conversation; ``invalid`` for a
    chain of theirs paused until someone's arrival (no yes is asked of it: it goes on under the
    yes already given). The request, and whether it was raised."""
    text = explicit_reason_from(reason)
    c = await _own_pending(
        session, confirmation_id, requester_id, conversation_id, now, paused=_PAUSED_NO_YES
    )
    stored = ask_of(c)
    raised = most_demanding(stored, "explicit_yes") != stored
    if raised:
        c.ask = "explicit_yes"
        c.asked_by = "domo"
        c.ask_why = text
    if shown_in is not None:
        c.last_shown_message_id = shown_in
    await session.flush()
    if raised:
        await append_audit(
            session,
            actor=f"person:{requester_id}",
            action="confirmation.explicit_required",
            details={"confirmation_id": str(c.id), "action_type": action_type_of(c)},
        )
    return c, raised


async def amend_confirmation(
    *,
    session_factory: SessionFactory,
    confirmation_id: uuid.UUID,
    new_arguments: JsonObject,
    ask_again: bool,
    ctx: ToolContext,
    now: datetime,
    note: str | None = None,
) -> tuple[AmendOutcome, ActionRecord | None, Confirmation | None]:
    """Apply the change the person asked for when they answered the request with a yes and a
    change (decision D2). The main model only translates their change into ``new_arguments``:
    the arguments of the request that change, with their new value (the others keep theirs).

    The person is the one who asked, or an approver of someone else's request (an adult for a
    child's, the owner when a right says so: spec 5.1) who answered it in their own conversation.
    Either way the changed action is the one who asked's: it is described, judged and run for
    them (their role, their rights now, their context and conversation), never for the approver,
    whose yes comes with the change. ``note`` is, for an approver, what they said with their
    change and how firm they were, as Domo wrote it (``approver_note_from``): it is kept with
    the request (``approver_note``), for the one who asked; a note on one's own request is not
    kept.

    1. ``ctx.change_grant`` must be this request: the person's words, read at the start of this
       turn, answered it with a change. Otherwise ``InvalidArguments("invalid")``; but for a
       request of the person, in this conversation, that waits for someone else's yes (an
       adult's, the owner's: spec 5.2): their words never answer it, so no grant ever names it,
       and their change needs none, since it never runs: it is always asked again of its approver
       (step 5, as with ``ask_again``). Their Domo was told such a note without its words (a
       waiting note reaches no model, D4): words given back as he was told of them
       (``describe.note_for_a_model``) stay as they wrote them;
    2. the row is locked (``_open_pending``); it must be a request of the person that waits in
       this conversation, or someone else's that waits for the person's yes (``may_decide``, its
       approver is not ``self``): otherwise ``InvalidArguments("not_found")``. It must still be
       ``pending`` (else ``expired`` or ``already_decided``) and under its lifetime (else it
       expires now: ``expired``). Someone else's request whose requester is no
       longer in the household is ``refused`` (``no_longer_possible``). An approver never changes
       a note, neither its words nor its recipient (``invalid``: it is its author's alone, spec
       6.7). A chain of actions is changed as a whole, and planned again (``_amend_chain``);
    3. the changed call must change something: a change that leaves every argument as it was
       would run the original as if it were the person's change (``invalid``). The request's
       own tool (the change cannot choose another one) describes it for the requester, within
       ``_DESCRIBE_SECONDS``: its ``InvalidArguments`` come back as they are, and so does a type
       of action that is not the request's (``invalid``). The request then still waits;
    4. the barrier is replayed for the requester with the changed action and the owner's rights
       as they are now (``decide_now``): a ``Refuse`` makes it ``refused`` (``limit`` when a
       limit refused it, with its summary); but the one who asked's change of their request that
       waits for someone else's yes, refused, leaves it as it was, waiting (``_change_refused``:
       they gave it no yes and no no), and only the refusal comes back;
    5. the yes-judge judges the changed action again (spec 4.1, 4.7), for the one who asked
       (their case, their words: an approver's words are someone else's, F22), told the request's
       history (``confirmations.chain.request_told``: what it asked, what it did before and does
       after the change; nobody had said yes yet, F23). Its ``do_not`` makes it ``refused``, with
       the judge's reason, at once: whoever's agreement it would need is never bothered (spec
       4.4); but the one who asked's change of their request that waits for someone else's yes
       leaves it as it was, waiting (``_judged_change_refused``), and only the refusal comes back;
    6. what it asks then (``judge.ask_after_a_change``): the most demanding of the judge's
       verdict, what Domo asks more (``ask_before_acting`` in this answer, else a plain yes with
       ``ask_again``: ``ctx.take_ask_more``, F15), what the request asked when it was an explicit
       yes or a proof (words never give those, F2) and the owner's rule on the type (F14); a plain
       yes of whoever answers it
       when that is nothing and someone else's agreement is needed. When the rights decide whose
       yes it needs (``_the_rights_ask_again``: the one who asked changed their request that waits
       for someone else's yes; the barrier asks for an agreement the person cannot give, Ruling
       R27; or an approver changed it once nobody else's agreement is needed, and it asked the one
       who asked's own yes), it is a new request whatever the judge says; otherwise too, unless
       the words of the change give what is asked (nothing, or the plain yes of a rule:
       ``judge.given_by_the_change``);
    7. a new request: the request becomes ``replaced`` by a new one of the requester that holds
       the changed call (asking what was found, who asked it and why; same approver, or the one
       the barrier asks for now, or the requester's own yes once nobody else's agreement is
       needed; the approver's note), in the same transaction as the ``waiting`` action that shows
       it under this turn's reply (as for any request, R69). That reply presents it (R74): to the
       requester when they changed it (``ctx.domo_message_id``), to the approver when they did
       and still are one of its approvers (``shown_to_approver_message_id``; it has no message of
       the requester's then). ``asked_again``, or ``approver_changed`` when its yes is no longer
       one the person can give (for the one who asked, nor that of the approver it waited for). A
       nudge of the request it replaces is not carried over (``nudged_at``). If that cannot be
       stored, nothing is, and the request still waits;
    8. otherwise the change runs, as the person's yes to it: the request becomes ``executing``,
       with ``amended_arguments`` and the action they make (so what its card shows is what ran,
       R75), ``decided_via="change"`` and ``decided_message_id`` (the person's message); then
       the call runs in the requester's context, and the request becomes ``executed`` or
       ``failed``, with its card as ``result`` (``_run_and_settle``, as for a decision: what it
       makes for the one who asked waits behind their button when an approver said yes);
    9. ``confirmation.decided`` (``"changed": true``) is audited last before each commit, with
       the person who changed it; the ``judge.verdict`` entry says what the judge said of the
       change, when it was asked, and the ``policy.decision`` entries what the barrier said of the
       changed action, for the requester.

    What Domo asked more for the next action of his answer is taken here (``take_ask_more``),
    first: this call uses it up, whatever comes of it.

    The grant is used up (``ctx.change_grant = None``) once the request it names is decided one
    way or another (or found already over); a change the tool rejects leaves it, so that Domo may
    correct it, and so does a grant-less change of another request (``_use_grant``).
    ``registry``, ``policy`` and the person's message come from ``ctx`` (R31).

    Before all that, a chain of the person, in this conversation, that paused until someone's
    arrival (``waiting_arrival``) is not changed, grant or not: ``InvalidArguments("invalid")``,
    with what Domo is to do (drop it, and propose a new chain that waits for the same
    invitation). A request of Domo's own is changed by ``_amend_his_own``: by him in a turn of his
    own, or, with this turn's grant, by the person who answered it "yes, but" (``answerer``). And a
    change of rights (``rights.apply``) is changed in words, and drafted again
    by the rights writer before anything is locked (``_amend_rights``, spec 5.3): the new draft
    is then judged like any other change, or the writer asks the owner a question
    (``writer_asks``).

    Returns the outcome, the record of the call that ran (or of a refused one), and the request
    the outcome is about: the one that ran or was refused, or the new one that waits (none when
    the request was already over).
    """
    raised = _raised_with(ctx.take_ask_more(), ask_again)
    async with session_factory() as session:
        found = (await confirmations_by_id(session, [confirmation_id])).get(confirmation_id)
    # Whose requests this turn changes as theirs: the person's, or, in a turn Domo acts in on
    # his own, his own (spec 12.5), and the person's only by a right the owner gave him (R30).
    asker = ctx.person.id
    if ctx.on_his_own:
        async with session_factory() as session:
            asker = await asker_on_his_own(session, ctx, confirmation_id, "confirmation.amend")
        if found is not None and not found.by_domo and asker == DOMO_SUBJECT_ID:
            # A person's request, not opened to him: nothing about it is told.
            raise InvalidArguments("not_found", _NOT_PENDING)
    mine = (
        found
        if found is not None
        and (requester_of(found), found.conversation_id) == (asker, ctx.conversation_id)
        else None
    )
    if mine is not None and mine.status == "waiting_arrival":
        raise InvalidArguments("invalid", _PAUSED_NOT_CHANGED)
    if mine is not None and mine.by_domo:
        return await _amend_his_own(
            session_factory, confirmation_id, new_arguments, ctx=ctx, now=now, raised=raised
        )
    if found is not None and found.by_domo and not ctx.on_his_own:
        # A request of his that the person answered "yes, but" (when they may answer it): his
        # change, made again in his name, that their yes covers if his judge lets it go.
        if ctx.change_grant != confirmation_id:
            raise InvalidArguments("invalid", _NO_GRANT)
        changed = await _amend_his_own(
            session_factory,
            confirmation_id,
            new_arguments,
            ctx=ctx,
            now=now,
            raised=raised,
            answerer=ctx.person,
        )
        _use_grant(ctx, confirmation_id)
        return changed
    if found is not None and action_type_of(found) == _RIGHTS_APPLY:
        return await _amend_rights(
            session_factory, confirmation_id, mine, new_arguments, ctx=ctx, now=now, raised=raised
        )
    # A request of the person that waits for someone else's yes (spec 5.2): their words never
    # answer it, so no change grant ever names it, and their change of it needs none: nothing
    # runs, it is always asked again of its approver (F7).
    for_others = mine is not None and mine.approver_kind != "self"
    if not for_others and ctx.change_grant != confirmation_id:
        raise InvalidArguments("invalid", _NO_GRANT)
    async with session_factory() as session:
        opened = await _open_pending(
            session,
            confirmation_id,
            ctx.person.id,
            now,
            # The person's own request in this conversation, or someone else's that waits for
            # their yes (never their own when an adult or the owner must say yes to it); without
            # a grant, their own that waits for someone else's yes, and nothing else.
            allowed=lambda c, person: (
                _waits_for_someone_else(c, person, ctx.conversation_id, now)
                if for_others
                else may_decide(c, person, now)
                and (c.approver_kind != "self" or c.conversation_id == ctx.conversation_id)
            ),
        )
        if not isinstance(opened, _Open):
            if opened == "not_allowed":
                raise InvalidArguments("not_found", _NOT_PENDING)
            _use_grant(ctx, confirmation_id)
            return opened, None, None
        c, person, settings = opened.c, opened.person, opened.settings
        tool = ctx.registry.get(c.tool_name)
        if tool is None or tool.action_type != action_type_of(c):
            raise InvalidArguments("invalid", "this request can no longer be changed")
        # Whose change: the one who asked, or an approver of their request (spec 5.1), whose note
        # alone is kept.
        by_approver = c.requester_id != person.id
        requester = await _person(session, c.requester_id) if by_approver else person
        if requester is None or not is_active(requester, now):
            await _mark_decided(
                session,
                c,
                person,
                via="change",
                reply_message_id=ctx.reply_message_id,
                now=now,
                answer=None,
            )
            await _refuse(
                session, c, person, via="change", now=now, changed=True, why=_NO_LONGER_POSSIBLE
            )
            _use_grant(ctx, confirmation_id)
            return "refused", _refused_yes(c, "no_longer_possible"), c
        kept_note = note if by_approver else None
        if c.chain_basis is not None:
            return await _amend_chain(
                session,
                session_factory,
                c,
                tool,
                requester=requester,
                decider=person,
                settings=settings,
                new_arguments=new_arguments,
                raised=raised,
                ctx=ctx,
                now=now,
                note=kept_note,
            )

        if by_approver and action_type_of(c) == _NOTE_LEAVE:
            # An approver never changes a note, neither its words (their Domo was never told
            # them, D4) nor its recipient: it is its author's alone (spec 6.7). Refused before
            # anything is compared, whatever it gives: the answer never tells whether words given
            # are the note's own (a right guess would otherwise change nothing).
            raise InvalidArguments("invalid", NOTE_IS_THE_AUTHORS)
        # The changed call, described by the request's own tool, for the requester.
        arguments: JsonObject = {**c.arguments, **new_arguments}
        if (
            for_others
            and action_type_of(c) == _NOTE_LEAVE
            and arguments.get("text") == note_for_a_model(dict(c.arguments))
        ):
            # Their Domo was told their waiting note without its words (D4): given back as he
            # was told of it, its words stay as they wrote them.
            arguments["text"] = c.arguments.get("text")
        if isinstance(tool, NamesPeople):
            # The people it names by their whole name, as a call on its own (spec 6.7).
            arguments = await whole_names(
                session, arguments, tool.person_arguments, now=now, speaker=person.id
            )
            if by_approver:
                # An approver's words name people as the approver does: a name that means nobody
                # or several to them is refused, never read through the nicknames of the one who
                # asked (the request is described for them below).
                for argument in tool.person_arguments:
                    value = arguments.get(argument)
                    if isinstance(value, str):
                        await resolve_person(session, value, now=now, speaker=person.id)
        if arguments == dict(c.arguments):
            raise InvalidArguments(
                "invalid", "the change changes nothing: give the arguments that change"
            )
        # Run, if it runs, in the turn it was first made in (the requester's conversation, not
        # the one of the person it concerns): unknown, it is not changed.
        made_in = await conversation_made_in(session, c)
        if made_in is None:
            raise InvalidArguments("invalid", "this request can no longer be changed")
        run_ctx = replace(
            ctx,
            actor=Actor(person_id=requester.id, role=as_role(requester.role)),
            person=requester,
            conversation_id=made_in,
            change_grant=None,
            # The plans due in this turn, if any, are not the request's.
            due_plans=(),
            asked_before=False,
            # A person's request, even changed in a turn of Domo's own, is theirs (ledger n3).
            on_his_own=False,
            # The clues of who speaks are the changer's: never read as the requester's.
            speaker=ctx.speaker if requester.id == ctx.person.id else None,
        )
        # Described, judged and audited as the call was when it was asked (final review I3).
        actor = judged_actor_of(tool, run_ctx)
        action: DescribedAction | None = None
        with anyio.move_on_after(_DESCRIBE_SECONDS):
            action = await tool.describe(arguments, actor, run_ctx)
        if action is None:
            raise TimeoutError("describing the changed call took too long: nothing was changed")
        if action.action_type != action_type_of(c):
            raise InvalidArguments("invalid", "a change keeps the same kind of action")

        decision = await decide_now(session, actor, action, ctx.policy, now)
        if isinstance(decision, Refuse) and for_others:
            return await _change_refused(session, c, person, actor, action, decision)
        replay = (actor, action, decision)
        if isinstance(decision, Refuse):
            await _mark_decided(
                session,
                c,
                person,
                via="change",
                reply_message_id=ctx.reply_message_id,
                now=now,
                answer=None,
            )
            c.approver_note = kept_note
            await _refuse(session, c, person, via="change", now=now, changed=True, replay=replay)
            _use_grant(ctx, confirmation_id)
            reason = _why_refused(decision, person, requester.id)
            return "refused", _refused_yes(c, reason, limit=_refusal_of(decision)), c

        # The rights may decide whose yes it needs (an agreement the person cannot give: Ruling
        # R27): it is then a new request whatever the judge says, but the judge is consulted on
        # the new version all the same (spec 4.1, 4.4), and what it asks counts.
        by_the_rights = _the_rights_ask_again(
            decision, c, person, requester.id, for_others=for_others
        )
        # A request that waited for the person it concerns: whose yes the changed one waits for,
        # and where, is the code's again (invariant ii); the change is a yes only from whoever
        # answers the changed one.
        placed = (
            None
            if c.concerned_id is None
            else await _placed_again(session, requester.id, action, decision, made_in, now)
        )
        if placed is not None:
            by_the_rights = not _answers(placed, person, requester.id)
        # The judge judges the changed action again, for the one who asked (F22), with the
        # request's history, told before the change marks it decided (F23).
        says = describe_for_people(action, arguments, REFERENCE)
        request = await request_told(
            session, c, before=[request_description(c, REFERENCE)], after=[says], now=now
        )
        judgement = await consult_the_judge(
            run_ctx,
            actor=judged_actor_of(tool, run_ctx),
            action=action,
            says=says,
            rights=rights_say(decision),
            request=request,
            owner_rule=owner_rule_of(decision.never_ask),
            # Its reason goes back to whoever changes it, theirs only when the case is.
            told_to=person.id,
        )
        if judgement.verdict == "do_not" and for_others:
            # Their change of their request that waits for someone else's yes, which the judge
            # refuses: it never reaches that person, and the request stays as it was.
            record = await _judged_change_refused(session, c, actor, action, decision, judgement)
            _use_grant(ctx, confirmation_id)
            return "refused", record, c
        await _mark_decided(
            session,
            c,
            person,
            via="change",
            reply_message_id=ctx.reply_message_id,
            now=now,
            answer=None if judgement.verdict == "do_not" else "yes",
        )
        c.approver_note = kept_note
        if judgement.verdict == "do_not":
            # Refused at once: whoever's agreement it would need is never bothered (spec 4.4).
            await _refuse(
                session,
                c,
                person,
                via="change",
                now=now,
                changed=True,
                replay=replay,
                judged=judgement,
            )
            _use_grant(ctx, confirmation_id)
            refused = refused_by_the_judge(c.tool_name, action.action_type, judgement.reason)
            return "refused", replace(refused, confirmation_id=c.id), c
        asked = ask_after_a_change(
            judgement,
            old_ask=asked_of(c),
            steps=[
                PlannedStep(
                    c.tool_name,
                    action.action_type,
                    arguments,
                    action,
                    never_ask=decision.never_ask,
                    rule=decision.rule,
                )
            ],
            new=[True],
            raised=raised,
            never_ask=decision.never_ask,
            yes_already_given=False,
        )
        if given_by_the_change(asked) and warned_first(judgement, action, None, decision.never_ask):
            # What a judge must weigh first (the last key), on a silent judge: a new request
            # asks a plain yes, never the change's words.
            asked = Asked("yes", "judge", None)
        if by_the_rights or not given_by_the_change(asked):
            outcome, new = await _ask_again(
                session,
                c,
                person,
                actor,
                action,
                decision,
                arguments,
                ctx,
                now,
                # Asked of whoever answers it: a plain yes when nothing more is (the accord alone).
                asked=asked or Asked("yes", "approver", None),
                note=kept_note,
                judged=judgement,
                placed=placed,
                made_in=made_in,
            )
            _use_grant(ctx, confirmation_id)
            if placed is not None:
                _notify_the_concerned(ctx, placed)
            return outcome, None, new

        # The words of the change give what is asked: it runs now, as the person's yes to it.
        c.status = "executing"
        c.amended_arguments = arguments
        c.action = dict(action.as_json())
        await audit_judgement(
            session, actor=actor, tool_name=c.tool_name, action=action, judgement=judgement
        )
        await _count_for_domo(session, c, actor, action)
        ran, record = await _run_and_settle(
            session,
            session_factory,
            c,
            tool=tool,
            action=action,
            arguments=arguments,
            ctx=run_ctx,
            decider=person,
            via="change",
            changed=True,
        )
        _use_grant(ctx, confirmation_id)
        return ran, record, c


async def _amend_his_own(
    session_factory: SessionFactory,
    confirmation_id: uuid.UUID,
    new_arguments: JsonObject,
    *,
    ctx: ToolContext,
    now: datetime,
    raised: tuple[Ask, str] | None,
    answerer: Person | None = None,
) -> tuple[AmendOutcome, ActionRecord | None, Confirmation | None]:
    """``amend_confirmation`` for a request of Domo's own (``by_domo``): changed by him in a turn
    he acts in on his own (spec 12.5), when it waits in this conversation, needing no grant; or,
    with ``answerer``, by a person who may answer it (``may_decide``: the barrier's approver, or the
    person it concerns) and answered it "yes, but" in this turn (the owner's decision of
    2026-11-24). Either way the changed call is made again in his name, and follows the verdict.

    1. the row is locked (``_open_pending``): it must still be ``pending`` (else ``expired`` or
       ``already_decided``) and under its lifetime (else it expires now); a chain of his is not
       changed (``invalid``: he drops it and proposes another; answered, it is answered yes or
       no); its tool, found as at a yes (``_made_tool`` for a tool of his), must still be the
       request's;
    2. the changed call (the arguments given over the stored ones, the people it names by their
       whole names, as whoever changes it names them) must change something (``invalid``), and is
       described by that tool for him, in the context of the turn it was first made in
       (``store.conversation_made_in``: unknown, ``invalid``, and nothing runs);
    3. it is judged again from scratch, as his: made on his own, the barrier with his own rights
       and the house's limits (``decide_now`` for ``domo_actor``, ``on_his_own_tables``), on his
       own; born of his mark in a person's turn (``store.made_in_a_turn``), in that turn, with the
       plain rules (``_in_their_turn``) and that person's socle too, as at first
       (``idea_decided_with_the_speaker``); then the yes-judge, told the request's history and
       his idea (with whether that person lacks the right, and, answered, whose words it was
       born of). A ``Refuse`` or the judge's ``do_not`` leaves the request as it was, waiting
       (``_change_refused``, ``_judged_change_refused``), and only the refusal comes back;
    4. what it asks then (``judge.ask_after_a_change``: the judge, he (``raised``), what the
       request asked when words never give it, the owner's rules) and whose yes it needs
       (``_placed_again``: the agreement the barrier asks now, else the yes of the person the
       changed action concerns, ``policy.subjects.concerned_of``, never the owner by default)
       decide whether it runs now: his own change when nothing is asked and the barrier lets it
       through; the answerer's when their yes is the one it needs and their words give what it
       asks (``judge.given_by_the_change``: a proof or an explicit yes never). It then runs as a
       person's change that the judge lets through does (``_run_and_settle``, ``via="change"``,
       ``amended_arguments``), in the turn it was first made in, as its requester's, decided by
       the answerer (or in his name);
    5. otherwise a new request of his replaces it (``replaced``), in one transaction, waiting
       where the code says: in the conversation of the turn it was first made in, or in the
       conversation of the person it concerns (then without the judge's reason, and news for
       them); keeping a reply of that turn (``_message_in``); a plain yes of the approver when
       nothing more is asked; counted in his history already when the barrier let it through
       (``counted_for_domo``); shown by this turn's reply where it waits here (for whoever answers
       it, when answered), with the ``waiting`` action that shows it under this turn's reply, and
       the audit (the judge's verdict, the barrier's decision, then ``confirmation.decided`` in
       his name, or the answerer's), last. ``asked_again`` while it waits for the same approver
       (answered: for the answerer again), ``approver_changed`` otherwise."""

    def allowed(c: Confirmation, person: Person) -> bool:
        if not c.by_domo:
            return False
        if answerer is None:
            return c.conversation_id == ctx.conversation_id
        return may_decide(c, person, now)

    async with session_factory() as session:
        opened = await _open_pending(session, confirmation_id, ctx.person.id, now, allowed=allowed)
        if not isinstance(opened, _Open):
            if opened == "not_allowed":
                raise InvalidArguments("not_found", _NOT_PENDING)
            return opened, None, None
        c = opened.c
        if c.chain_basis is not None:
            raise InvalidArguments(
                "invalid", _HIS_CHAIN_NOT_CHANGED if answerer is None else _HIS_CHAIN_ANSWERED
            )
        requester = await _person(session, c.requester_id)
        tool = ctx.registry.get(c.tool_name) or await _made_tool(ctx, c.tool_name)
        if (
            requester is None
            or not is_active(requester, now)
            or tool is None
            or tool.action_type != action_type_of(c)
        ):
            raise InvalidArguments("invalid", "this request can no longer be changed")
        arguments: JsonObject = {**c.arguments, **new_arguments}
        if isinstance(tool, NamesPeople):
            # The people it names by their whole name, as whoever changes it names them.
            arguments = await whole_names(
                session,
                arguments,
                tool.person_arguments,
                now=now,
                speaker=requester.id if answerer is None else answerer.id,
            )
        if arguments == dict(c.arguments):
            raise InvalidArguments(
                "invalid", "the change changes nothing: give the arguments that change"
            )
        # His, judged from scratch: his own rights, in a turn of his own, in that conversation;
        # or, born of his mark in a person's turn, in that turn (the requester's, where it was
        # made), with the plain rules, never those of a turn of his own (ruling S1).
        in_a_turn = made_in_a_turn(c)
        made_in = await conversation_made_in(session, c)
        if made_in is None:
            raise InvalidArguments("invalid", "this request can no longer be changed")
        run_ctx = replace(
            ctx,
            actor=Actor(person_id=requester.id, role=as_role(requester.role)),
            person=requester,
            conversation_id=made_in,
            change_grant=None,
            due_plans=(),
            asked_before=False,
            on_his_own=replayed_on_his_own(c),
            policy=on_his_own_tables(ctx.policy)
            if replayed_on_his_own(c)
            else _in_their_turn(ctx.policy),
            # The clues of who speaks are the answerer's: never read as the requester's.
            speaker=ctx.speaker if requester.id == ctx.person.id else None,
        )
        actor = domo_actor()
        action: DescribedAction | None = None
        with anyio.move_on_after(_DESCRIBE_SECONDS):
            action = await tool.describe(arguments, actor, run_ctx)
        if action is None:
            raise TimeoutError("describing the changed call took too long: nothing was changed")
        if action.action_type != action_type_of(c):
            raise InvalidArguments("invalid", "a change keeps the same kind of action")
        decision = await decide_now(session, actor, action, run_ctx.policy, now)
        lacks = False
        if in_a_turn:
            # Born of his mark in a person's turn: their socle holds, as at first.
            decision = await idea_decided_with_the_speaker(
                session, run_ctx.actor, action, run_ctx.policy, decision, now
            )
            # Told to the judge, as at first: whether they lack the right themselves.
            rights = await load_rights(session)
            lacks = not rights.resolve(run_ctx.actor, action.action_type, run_ctx.policy).allowed
        if isinstance(decision, Refuse):
            return await _change_refused(session, c, requester, actor, action, decision)
        # Whose yes it waits for, and where: the code's again, on the changed action (never the
        # owner by default); born of the owner's rule on his act in a person's turn, still the
        # owner's while his rule asks (re-review RC1).
        placed = (
            _Placed("owner", None, None, made_in, False)
            if c.in_their_turn and isinstance(decision, Allow) and decision.rule is not None
            else await _placed_again(session, requester.id, action, decision, made_in, now)
        )
        says = describe_for_people(action, arguments, REFERENCE)
        request = await request_told(
            session, c, before=[request_description(c, REFERENCE)], after=[says], now=now
        )
        judgement = await consult_the_judge(
            run_ctx,
            actor=actor,
            action=action,
            says=says,
            rights=rights_say(decision),
            request=request,
            owner_rule=owner_rule_of(decision.never_ask),
            # Whose words it was born of is not kept, never guessed: but a change answered is
            # born of the answerer's words.
            idea=OwnIdea(
                c.idea_reason or "",
                born_of=None if answerer is None else answerer.id,
                lacks_the_right=lacks,
            ),
            # Its reason goes back to whoever changes it, theirs only when the case is.
            told_to=ctx.person.id,
        )
        if judgement.verdict == "do_not":
            record = await _judged_change_refused(session, c, actor, action, decision, judgement)
            return "refused", record, c
        asked = ask_after_a_change(
            judgement,
            old_ask=asked_of(c),
            steps=[
                PlannedStep(
                    c.tool_name,
                    action.action_type,
                    arguments,
                    action,
                    never_ask=decision.never_ask,
                    rule=decision.rule,
                )
            ],
            new=[True],
            raised=raised,
            never_ask=decision.never_ask,
            yes_already_given=False,
        )
        if given_by_the_change(asked) and warned_first(judgement, action, None, decision.never_ask):
            # What a judge must weigh first (the last key), on a silent judge: a new request
            # asks a plain yes, never the change's words.
            asked = Asked("yes", "judge", None)
        # Whether the answerer's yes is the one it needs now (his own yes is nobody's).
        theirs = answerer is not None and _answers(placed, answerer, requester_of(c))
        if answerer is None:
            runs = asked is None and isinstance(decision, Allow)
        else:
            runs = theirs and given_by_the_change(asked)
            await _mark_decided(
                session,
                c,
                answerer,
                via="change",
                reply_message_id=ctx.reply_message_id,
                now=now,
                answer="yes",
            )
        if runs:
            # It runs now, as a person's change that the judge lets through does.
            return await _run_his_change(
                session,
                session_factory,
                c,
                tool=tool,
                actor=actor,
                action=action,
                arguments=arguments,
                judgement=judgement,
                run_ctx=run_ctx,
                decider=requester if answerer is None else answerer,
                by_domo=answerer is None,
                now=now,
            )
        approver = placed.approver
        new = await _changed_request(
            session,
            c,
            actor=run_ctx.actor,
            action=action,
            arguments=arguments,
            decision=decision,
            approver=approver,
            asked=_shown_there(asked or Asked("yes", "approver", None), placed),
            conversation_id=placed.conversation_id,
            # Made in the turn it was first made in, still (``store.conversation_made_in``).
            message_id=await _message_in(session, c, made_in),
            now=now,
            question=_question_of(c, placed),
            note=None,
            concerned_id=placed.concerned_id,
            by_domo=True,
            judged_as=actor,
        )
        # This reply shows it, where it waits in this very conversation (answered, to the answerer
        # only when it waits for them again: their next words answer it).
        here = placed.conversation_id == ctx.conversation_id
        if answerer is None:
            new.last_shown_message_id = ctx.domo_message_id if here else None
        else:
            shown = ctx.domo_message_id if here and theirs else None
            new.last_shown_message_id = shown if placed.approver == "self" else None
            new.shown_to_approver_message_id = shown if placed.approver != "self" else None
        await _tell_who_answers(session, ctx, placed, now)
        c.status = "replaced"
        c.replaced_by = new.id
        await session.flush()
        if ctx.domo_message_id is not None:
            await ConversationStore(session).add_action(
                ctx.domo_message_id,
                tool_name=c.tool_name,
                action_type=action_type_of(c),
                status="waiting",
                summary={"key": _waiting_key(placed), "params": {}},
                confirmation_id=new.id,
                by_domo=True,
            )
        same = theirs if answerer is not None else approver == c.approver_kind
        outcome: AmendOutcome = "asked_again" if same else "approver_changed"
        await audit_judgement(
            session, actor=actor, tool_name=c.tool_name, action=action, judgement=judgement
        )
        await record_decision(session, actor, action, decision, tool_name=c.tool_name)
        await _audit_decided(
            session,
            c,
            requester if answerer is None else answerer,
            outcome=outcome,
            via="change",
            changed=True,
            replaced_by=new.id,
            by_domo=answerer is None,
        )
        await session.commit()
    _notify_the_concerned(ctx, placed)
    return outcome, None, new


async def _run_his_change(
    session: AsyncSession,
    session_factory: SessionFactory,
    c: Confirmation,
    *,
    tool: InternalTool,
    actor: Actor,
    action: DescribedAction,
    arguments: JsonObject,
    judgement: Judgement,
    run_ctx: ToolContext,
    decider: Person,
    by_domo: bool,
    now: datetime,
) -> tuple[AmendOutcome, ActionRecord, Confirmation]:
    """Step 4 of ``_amend_his_own``: his changed call (``action``, judged as ``actor``) runs now,
    on ``session`` (which holds the row lock of ``c``): ``c`` becomes ``executing`` with what runs
    (``amended_arguments``, its action), decided by a change (by ``decider``, the answerer, who
    was marked so; or in his name, ``by_domo``, his own change being nobody's yes), counted once
    in his history (``_count_for_domo``), after the judge's verdict is audited; then the call runs
    in ``run_ctx``, the turn it was first made in, as its requester's (``_run_and_settle``)."""
    if by_domo:
        c.decided_at = now
        c.decided_via = "change"
    c.status = "executing"
    c.amended_arguments = arguments
    c.action = dict(action.as_json())
    await audit_judgement(
        session, actor=actor, tool_name=c.tool_name, action=action, judgement=judgement
    )
    await _count_for_domo(session, c, actor, action)
    ran, record = await _run_and_settle(
        session,
        session_factory,
        c,
        tool=tool,
        action=action,
        arguments=arguments,
        ctx=run_ctx,
        decider=decider,
        via="change",
        changed=True,
        by_domo=by_domo,
    )
    return ran, record, c


@dataclass(frozen=True)
class _Placed:
    """Whose yes a request born of a change waits for, and where (invariant ii of fix round 2):
    the agreement the barrier asks (``approver``, ``question``), in the conversation of the turn
    it was first made in; else the yes of the person the changed action concerns
    (``concerned_id``, ``policy.subjects.concerned_of``): there when that is the one who asked,
    else in their own active conversation (``elsewhere``)."""

    approver: Approver
    question: str | None
    concerned_id: uuid.UUID | None
    conversation_id: uuid.UUID
    elsewhere: bool


async def _placed_again(
    session: AsyncSession,
    requester_id: uuid.UUID,
    action: DescribedAction,
    decision: Allow | NeedsApproval,
    made_in: uuid.UUID,
    now: datetime,
) -> _Placed:
    """Where and for whom the changed ``action`` of ``requester_id`` waits (``_Placed``).
    ``InvalidArguments("not_possible")`` when nobody can answer for it (no owner now)."""
    if isinstance(decision, NeedsApproval):
        return _Placed(decision.approver, decision.question, None, made_in, False)
    try:
        concerned = await concerned_of(session, subject_of(action, None, requester_id), action, now)
    except LookupError:
        raise InvalidArguments("not_possible", _NOBODY_TO_ASK) from None
    if concerned == requester_id:
        return _Placed("self", None, concerned, made_in, False)
    theirs = await ConversationStore(session).active_conversation(concerned)
    return _Placed("self", None, concerned, theirs.id, True)


async def _still_concerns(
    session: AsyncSession,
    c: Confirmation,
    action: DescribedAction,
    requester_id: uuid.UUID,
    now: datetime,
) -> bool:
    """Whether the person ``c`` waits for (``concerned_id``) still answers for the thing
    ``action`` (as described at the yes) is about (``concerned_of``, read again): false once the
    thing is someone else's, or nobody can answer for it (no owner now)."""
    try:
        concerned = await concerned_of(session, subject_of(action, None, requester_id), action, now)
    except LookupError:
        return False
    return concerned == c.concerned_id


async def _message_in(
    session: AsyncSession, c: Confirmation, made_in: uuid.UUID
) -> uuid.UUID | None:
    """A reply of the conversation ``made_in`` (the turn ``c`` was first made in), for a request
    born of a change of ``c`` to keep (``message_id``: ``store.conversation_made_in``): its own,
    when it is one, else the latest there; ``None`` when there is none."""
    if c.message_id is not None:
        found = await session.scalar(
            select(Message.conversation_id).where(Message.id == c.message_id)
        )
        if found == made_in:
            return c.message_id
    return await session.scalar(
        select(Message.id)
        .where(Message.conversation_id == made_in)
        .order_by(Message.created_at.desc())
        .limit(1)
    )


def _waiting_key(placed: _Placed) -> str:
    """The card of the action that shows a request born of a change (``placed``): whose yes it
    waits for, the person it concerns, by no name, when it waits in their own conversation."""
    return (
        "action.card.waiting.the_concerned"
        if placed.elsewhere
        else f"action.card.waiting.{placed.approver}"
    )


def _told_elsewhere(ctx: ToolContext, placed: _Placed) -> uuid.UUID | None:
    """The person a request born of a change waits for (``placed``) when they must be told of it
    apart from this turn: the person it concerns, when it waits in another conversation than this
    turn's and they are not the person of this turn; ``None`` otherwise."""
    concerned = placed.concerned_id
    if concerned is None or concerned == ctx.person.id:
        return None
    return None if placed.conversation_id == ctx.conversation_id else concerned


async def _tell_who_answers(
    session: AsyncSession, ctx: ToolContext, placed: _Placed, now: datetime
) -> None:
    """News for the person a request born of a change waits for, when they are told of it apart
    from this turn (``_told_elsewhere``), in the transaction that makes it."""
    told = _told_elsewhere(ctx, placed)
    if told is not None:
        # Imported here: ``conversation.visits`` reads the requests from this package.
        from domo_core.conversation.visits import news_for

        await news_for(session, told, now=now)


def _shown_there(asked: Asked | None, placed: _Placed) -> Asked | None:
    """What a request born of a change asks, as stored: without the judge's reason when it waits
    in the conversation of someone else than the one who asked, who wrote nothing of it (the
    judge wrote it from the one who asked's case: ruling R29). The fact that the judge warned is
    kept, never its words (ruling R69), with the code's own line that it cannot be undone (R72):
    the answer of the person it waits for is remembered."""
    if asked is None or not placed.elsewhere:
        return asked
    return for_the_person_concerned(asked)


def _question_of(c: Confirmation, placed: _Placed) -> str | None:
    """The question a limit asked of a request born of a change of ``c``: the barrier's now when
    it asks another approver's agreement, else the one ``c`` kept, while it waits for the same
    person's yes."""
    if placed.question is not None:
        return placed.question
    same = placed.approver == c.approver_kind and placed.concerned_id == c.concerned_id
    return c.ask_reason if same else None


async def _changed_request(
    session: AsyncSession,
    c: Confirmation,
    *,
    actor: Actor,
    action: DescribedAction,
    arguments: JsonObject,
    decision: Decision,
    approver: Approver,
    asked: Asked | None,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID | None,
    now: datetime,
    question: str | None,
    note: str | None,
    concerned_id: uuid.UUID | None = None,
    by_domo: bool = False,
    judged_as: Actor | None = None,
) -> Confirmation:
    """The one way a change makes the request that replaces ``c`` (``create_confirmation``):
    the changed call of the one who asked (``actor``), whose yes it waits for and where, what it
    asks and the question a limit asked, what an approver said with the change (``note``), what
    it is about (``policy.subjects``), his reason when it was born of his mark (``c``'s), the
    person it concerns, and whether his history counts it already (the barrier let it through
    for ``judged_as``, the actor it was judged as, ``actor`` when not given)."""
    counted = isinstance(decision, Allow) and writes_for_domo(judged_as or actor, action)
    return await create_confirmation(
        session,
        requester=actor,
        tool_name=c.tool_name,
        arguments=arguments,
        action=action,
        approver=approver,
        asked=asked,
        conversation_id=conversation_id,
        message_id=message_id,
        now=now,
        question=question,
        approver_note=note,
        by_domo=by_domo,
        # The ``allow`` written in his name counts it in his history already.
        counted_for_domo=counted,
        subject_key=subject_of(action, None, actor.person_id).key,
        idea_reason=c.idea_reason,
        concerned_id=concerned_id,
        # His request born in a person's turn of the owner's rule stays so (re-review RC1).
        in_their_turn=by_domo and c.in_their_turn,
    )


def _notify_the_concerned(ctx: ToolContext, placed: _Placed) -> None:
    """Once a request waits for someone who is told of it apart from this turn
    (``_told_elsewhere``), the core is told that a turn of theirs may be due (``ctx.notify``); a
    failure is logged."""
    told = _told_elsewhere(ctx, placed)
    if told is None or ctx.notify is None:
        return
    try:
        ctx.notify(told)
    except Exception as error:
        logger.error("The core was not told of news (%s)", type(error).__name__)


async def _change_refused(
    session: AsyncSession,
    c: Confirmation,
    person: Person,
    actor: Actor,
    action: DescribedAction,
    decision: Refuse,
) -> tuple[AmendOutcome, ActionRecord, Confirmation]:
    """A change that the one who asked (``person``) made to their request ``c``, which waits for
    someone else's yes (spec 5.2), and that the barrier refuses: the request stays as it was,
    waiting for its approver (the one who asked gave it no yes and no no: nothing ran, nothing
    changed). What the barrier said of the changed action (``action``, for ``actor``) is audited
    (``policy.decision``); committed. ``refused``, with why (``_why_refused``: ``not_allowed``, or
    the limit), and the request, as it still waits."""
    await record_decision(session, actor, action, decision, tool_name=c.tool_name)
    await session.commit()
    reason = _why_refused(decision, person, c.requester_id)
    return "refused", _refused_yes(c, reason, limit=_refusal_of(decision)), c


async def _judged_change_refused(
    session: AsyncSession,
    c: Confirmation,
    actor: Actor,
    action: DescribedAction,
    decision: Decision,
    judged: Judgement,
) -> ActionRecord:
    """A change that the one who asked made to their request ``c``, which waits for someone
    else's yes (spec 5.2), and that the judge refuses (``do_not``): it never reaches that person,
    and the request stays as it was, waiting (the one who asked gave it no yes and no no, as when
    the barrier refuses such a change: ``_change_refused``). The judge's verdict and what the
    barrier said of the changed action (``action``, for ``actor``) are audited; committed. The
    judge's refusal, with its reason, about ``c``."""
    await audit_judgement(
        session, actor=actor, tool_name=c.tool_name, action=action, judgement=judged
    )
    await record_decision(session, actor, action, decision, tool_name=c.tool_name)
    await session.commit()
    refused = refused_by_the_judge(c.tool_name, action.action_type, judged.reason)
    return replace(refused, confirmation_id=c.id)


def _presented_by(
    ctx: ToolContext,
    person: Person,
    requester_id: uuid.UUID,
    approver: Approver,
    *,
    before: str,
) -> tuple[AmendOutcome, uuid.UUID | None, uuid.UUID | None]:
    """For a change that made a new request waiting for the yes of ``approver``, of a request that
    waited for the yes of ``before`` (its ``approver_kind``): the outcome (``asked_again`` while its
    yes is still one ``person``, who changed it, may give, or, for the one who asked, while it waits
    for the approver it waited for, an adult or the owner: spec 5.2; ``approver_changed``
    otherwise), and the replies that present it (R74): this turn's to the one who asked when they
    changed it (``message_id``), or to the approver when they did and still are one of its approvers
    (``shown_to_approver``)."""
    theirs = approves(approver, person, requester_id)
    by_requester = person.id == requester_id
    # The one who asked: their own yes, or that of the approver their request waited for.
    same = theirs or (by_requester and approver == before)
    outcome: AmendOutcome = "asked_again" if same else "approver_changed"
    if by_requester:
        return outcome, ctx.domo_message_id, None
    return outcome, None, ctx.domo_message_id if theirs else None


async def _ask_again(
    session: AsyncSession,
    c: Confirmation,
    person: Person,
    actor: Actor,
    action: DescribedAction,
    decision: Decision,
    arguments: JsonObject,
    ctx: ToolContext,
    now: datetime,
    *,
    asked: Asked | None,
    note: str | None = None,
    judged: Judgement | None = None,
    placed: "_Placed | None" = None,
    made_in: uuid.UUID | None = None,
) -> tuple[AmendOutcome, Confirmation]:
    """Step 5 of ``amend_confirmation``, in one transaction: the new request of the requester
    (``actor``; the changed call, asking ``asked``, with the approver of the one it replaces and
    what Domo could not tell; or, when the barrier now asks for another approver's agreement,
    that approver and the question it comes with; or the requester's own yes once nobody else's
    agreement is needed), presented by this turn's reply (``_presented_by``), with what an
    approver said (``note``), the ``waiting`` action that shows it under this turn's reply, the
    request it replaces made ``replaced``, and the audit (the judge's verdict on the change when
    it judged it, ``judged``; what the barrier said of the changed action; then the decision of
    ``person``, as the outcome), last. Committed; an error leaves nothing stored. Returns the
    outcome and the new request.

    ``placed``: for a request that waited for the person it concerns, whose yes the new one waits
    for and where (``_placed_again``), keeping the turn it was first made in (``made_in``: a reply
    of it, ``_message_in``), shown by this turn's reply only where it waits in this very
    conversation, without the judge's reason in someone else's (ruling R29), and news for them."""
    if placed is not None and made_in is not None:
        return await _ask_again_placed(
            session,
            c,
            person,
            actor,
            action,
            decision,
            arguments,
            ctx,
            now,
            asked=asked,
            note=note,
            judged=judged,
            placed=placed,
            made_in=made_in,
        )
    approver: Approver
    if isinstance(decision, NeedsApproval) and decision.approver != approver_of(c):
        approver, question = decision.approver, decision.question
    elif isinstance(decision, Allow) and approver_of(c) != "self":
        # Nobody else's agreement is needed any more: the yes is the requester's own (R27).
        approver, question = "self", None
    else:
        approver, question = approver_of(c), c.ask_reason
    outcome, message_id, shown_to_approver = _presented_by(
        ctx, person, actor.person_id, approver, before=c.approver_kind
    )
    new = await _changed_request(
        session,
        c,
        actor=actor,
        action=action,
        arguments=arguments,
        decision=decision,
        approver=approver,
        asked=asked,
        conversation_id=c.conversation_id,
        message_id=message_id,
        now=now,
        question=question,
        note=note,
    )
    new.shown_to_approver_message_id = shown_to_approver
    c.status = "replaced"
    c.replaced_by = new.id
    await session.flush()
    if ctx.domo_message_id is not None:
        await ConversationStore(session).add_action(
            ctx.domo_message_id,
            tool_name=c.tool_name,
            action_type=action_type_of(c),
            status="waiting",
            summary={"key": f"action.card.waiting.{approver}", "params": {}},
            confirmation_id=new.id,
        )
    if judged is not None:
        await audit_judgement(
            session, actor=actor, tool_name=c.tool_name, action=action, judgement=judged
        )
    await record_decision(session, actor, action, decision, tool_name=c.tool_name)
    await _audit_decided(
        session, c, person, outcome=outcome, via="change", changed=True, replaced_by=new.id
    )
    await session.commit()
    return outcome, new


async def _ask_again_placed(
    session: AsyncSession,
    c: Confirmation,
    person: Person,
    actor: Actor,
    action: DescribedAction,
    decision: Decision,
    arguments: JsonObject,
    ctx: ToolContext,
    now: datetime,
    *,
    asked: Asked | None,
    note: str | None,
    judged: Judgement | None,
    placed: _Placed,
    made_in: uuid.UUID,
) -> tuple[AmendOutcome, Confirmation]:
    """``_ask_again`` for a request that waited for the person it concerns (``placed``)."""
    here = placed.conversation_id == ctx.conversation_id
    new = await _changed_request(
        session,
        c,
        actor=actor,
        action=action,
        arguments=arguments,
        decision=decision,
        approver=placed.approver,
        asked=_shown_there(asked, placed),
        conversation_id=placed.conversation_id,
        message_id=await _message_in(session, c, made_in),
        now=now,
        question=_question_of(c, placed),
        note=note,
        concerned_id=placed.concerned_id,
    )
    # Shown by this turn's reply where it waits in this very conversation, to whoever answers it.
    answers = _answers(placed, person, actor.person_id)
    new.last_shown_message_id = ctx.domo_message_id if here and answers else None
    if not here or not answers or placed.concerned_id is not None:
        new.shown_to_approver_message_id = None
    elif placed.approver != "self":
        new.shown_to_approver_message_id = ctx.domo_message_id
    c.status = "replaced"
    c.replaced_by = new.id
    await session.flush()
    await _tell_who_answers(session, ctx, placed, now)
    if ctx.domo_message_id is not None:
        await ConversationStore(session).add_action(
            ctx.domo_message_id,
            tool_name=c.tool_name,
            action_type=action_type_of(c),
            status="waiting",
            summary={
                "key": "action.card.waiting.self" if answers else _waiting_key(placed),
                "params": {},
            },
            confirmation_id=new.id,
        )
    outcome: AmendOutcome = (
        "asked_again" if answers and placed.approver == c.approver_kind else "approver_changed"
    )
    if judged is not None:
        await audit_judgement(
            session, actor=actor, tool_name=c.tool_name, action=action, judgement=judged
        )
    await record_decision(session, actor, action, decision, tool_name=c.tool_name)
    await _audit_decided(
        session, c, person, outcome=outcome, via="change", changed=True, replaced_by=new.id
    )
    await session.commit()
    return outcome, new


def _answers(placed: _Placed, person: Person, requester_id: uuid.UUID) -> bool:
    """Whether ``person`` answers a request placed so (``_Placed``): the person it concerns, or
    an approver of the agreement it waits for."""
    if placed.concerned_id is not None:
        return placed.concerned_id == person.id
    return approves(placed.approver, person, requester_id)


# --- a change of rights, drafted again by the writer (spec 5.3) ----------------------------------


def _writer_asks(c: Confirmation, question: str) -> ActionRecord:
    """The record of a change of rights that the writer did not draft: it asks the owner
    ``question`` (its own text, for Domo to ask: a result, never the detail of an error, F33),
    and the request ``c`` still waits as it was. Its card says Domo needs to know more; it names
    no request, so the reply does not present ``c`` again."""
    return ActionRecord(
        tool_name=c.tool_name,
        action_type=action_type_of(c),
        status="done",
        confirmation_id=None,
        model_result={"status": "done", "question": question},
        card={"key": "action.card.done.rights.propose.question", "params": {}},
    )


async def _amend_rights(
    session_factory: SessionFactory,
    confirmation_id: uuid.UUID,
    mine: Confirmation | None,
    new_arguments: JsonObject,
    *,
    ctx: ToolContext,
    now: datetime,
    raised: tuple[Ask, str] | None = None,
) -> tuple[AmendOutcome, ActionRecord | None, Confirmation | None]:
    """``amend_confirmation`` for a change of rights (``rights.apply``): "yes, but 3 days instead
    of 2", on its card (spec 5.3). The rights writer drafts the rights again from the owner's
    words and their change, and the new draft is judged like any other change (the owner's
    decision of 2026-11-15: what is asked for and understood is done): applied at once when the
    words of the change give what is asked, a new request that replaces this one otherwise (the
    owner then reads its examples before saying yes). ``mine`` is the request as it was read
    without a lock, if it is the person's
    in this conversation. The writer may take minutes: it runs before the request is locked
    (F18), so that a button on it is never held up meanwhile, and the time of what follows it
    (the request's lifetime, the new request's) is read again once it is over (``ctx.clock``):
    ``now`` is the time the change was asked for.

    1. without a lock: the change grant must name the request (``invalid``); it must be the
       person's, in this conversation, and ``pending`` (``not_found``); its tool must be the
       registry's for a change of rights (``invalid``); and ``new_arguments`` are exactly
       ``{"change": <text>}``, what the owner wants different, in their words (``invalid``);
    2. the writer drafts the rights again, in the person's context, with no lock or session held
       (``tools.rights_tools.redraft_rights``: only the owner has rights written). Its
       ``InvalidArguments`` come back as they are, and the request still waits. A question of the
       writer is ``writer_asks``, with its record (``_writer_asks``): nothing is stored, the
       request still waits, and the grant stays for this turn;
    3. then, as for any change: the request is locked and checked again (``_open_pending``: a
       button may have answered it meanwhile, ``already_decided``, and the new draft is then
       never applied); the call that applies the new draft is described by the request's tool,
       for the person, and the barrier replayed (a ``Refuse`` makes it ``refused``); the
       yes-judge judges the changed call, told the request's history (its ``do_not`` makes it
       ``refused``, with its reason); what it asks then is ``judge.ask_after_a_change`` (the
       judge's verdict, what Domo asks more of the change, ``raised``, F15, what the request
       asked when it was an explicit yes or a proof, and the owner's rule on applying rights):
       when the words of the change give it (``judge.given_by_the_change``), the new draft is
       applied now, as the owner's yes to it (``_run_and_settle``); otherwise it is asked again
       (``_ask_again``: ``asked_again``), with who asked it and why."""
    if ctx.change_grant != confirmation_id:
        raise InvalidArguments("invalid", _NO_GRANT)
    if mine is None or mine.status != "pending":
        raise InvalidArguments("not_found", _NOT_PENDING)
    tool = ctx.registry.get(mine.tool_name)
    draft_id = draft_of(mine)
    if tool is None or tool.action_type != _RIGHTS_APPLY or draft_id is None:
        raise InvalidArguments("invalid", "this request can no longer be changed")
    change = new_arguments.get("change")
    if frozenset(new_arguments) != _RIGHTS_CHANGE or not isinstance(change, str):
        raise InvalidArguments("invalid", _RIGHTS_IN_WORDS)
    # No lock and no session are held while the writer writes.
    redraft = await redraft_rights(ctx, draft_id, change)
    if redraft.question is not None:
        return "writer_asks", _writer_asks(mine, redraft.question), mine
    # The writer may have taken minutes.
    now = ctx.clock()
    arguments: JsonObject = {"draft": str(redraft.draft_id)}
    async with session_factory() as session:
        opened = await _open_pending(
            session,
            confirmation_id,
            ctx.person.id,
            now,
            allowed=lambda c, person: (
                may_decide(c, person, now)
                and (c.requester_id, c.conversation_id) == (person.id, ctx.conversation_id)
            ),
        )
        if not isinstance(opened, _Open):
            if opened == "not_allowed":
                raise InvalidArguments("not_found", _NOT_PENDING)
            _use_grant(ctx, confirmation_id)
            return opened, None, None
        c, person = opened.c, opened.person
        actor = Actor(person_id=person.id, role=as_role(person.role))
        run_ctx = replace(
            ctx,
            actor=actor,
            person=person,
            change_grant=None,
            # The plans due in this turn, if any, are not the request's.
            due_plans=(),
            asked_before=False,
            # The owner's request is theirs, whatever the turn (ledger n3).
            on_his_own=False,
        )
        action: DescribedAction | None = None
        with anyio.move_on_after(_DESCRIBE_SECONDS):
            action = await tool.describe(arguments, actor, run_ctx)
        if action is None:
            raise TimeoutError("describing the changed call took too long: nothing was changed")
        if action.action_type != action_type_of(c):
            raise InvalidArguments("invalid", "a change keeps the same kind of action")
        decision = await decide_now(session, actor, action, ctx.policy, now)
        replay = (actor, action, decision)
        if isinstance(decision, Refuse):
            await _mark_decided(
                session,
                c,
                person,
                via="change",
                reply_message_id=ctx.reply_message_id,
                now=now,
                answer=None,
            )
            await _refuse(session, c, person, via="change", now=now, changed=True, replay=replay)
            _use_grant(ctx, confirmation_id)
            reason = _why_refused(decision, person, person.id)
            return "refused", _refused_yes(c, reason, limit=_refusal_of(decision)), c
        # Judged like any other change, with the request's history, told before the change marks
        # it decided (F23).
        says = describe_for_people(action, arguments, REFERENCE)
        request = await request_told(
            session, c, before=[request_description(c, REFERENCE)], after=[says], now=now
        )
        judgement = await consult_the_judge(
            run_ctx,
            actor=judged_actor_of(tool, run_ctx),
            action=action,
            says=says,
            rights=rights_say(decision),
            request=request,
            owner_rule=owner_rule_of(decision.never_ask),
        )
        await _mark_decided(
            session,
            c,
            person,
            via="change",
            reply_message_id=ctx.reply_message_id,
            now=now,
            answer=None if judgement.verdict == "do_not" else "yes",
        )
        if judgement.verdict == "do_not":
            await _refuse(
                session,
                c,
                person,
                via="change",
                now=now,
                changed=True,
                replay=replay,
                judged=judgement,
            )
            _use_grant(ctx, confirmation_id)
            refused = refused_by_the_judge(c.tool_name, action.action_type, judgement.reason)
            return "refused", replace(refused, confirmation_id=c.id), c
        asked = ask_after_a_change(
            judgement,
            old_ask=asked_of(c),
            steps=[
                PlannedStep(
                    c.tool_name,
                    action.action_type,
                    arguments,
                    action,
                    never_ask=decision.never_ask,
                    rule=decision.rule,
                )
            ],
            new=[True],
            raised=raised,
            never_ask=decision.never_ask,
            yes_already_given=False,
        )
        if given_by_the_change(asked) and warned_first(judgement, action, None, decision.never_ask):
            # What a judge must weigh first (the last key), on a silent judge: a new request
            # asks a plain yes, never the change's words.
            asked = Asked("yes", "judge", None)
        if not given_by_the_change(asked):
            outcome, new = await _ask_again(
                session,
                c,
                person,
                actor,
                action,
                decision,
                arguments,
                ctx,
                now,
                asked=asked,
                judged=judgement,
            )
            _use_grant(ctx, confirmation_id)
            return outcome, None, new
        # The words of the change give what is asked: the new draft is applied now.
        c.status = "executing"
        c.amended_arguments = arguments
        c.action = dict(action.as_json())
        await audit_judgement(
            session, actor=actor, tool_name=c.tool_name, action=action, judgement=judgement
        )
        ran, record = await _run_and_settle(
            session,
            session_factory,
            c,
            tool=tool,
            action=action,
            arguments=arguments,
            ctx=run_ctx,
            decider=person,
            via="change",
            changed=True,
        )
        _use_grant(ctx, confirmation_id)
        return ran, record, c


# --- a chain of actions, changed as a whole (spec 4.1) -------------------------------------------


async def _amend_chain(
    session: AsyncSession,
    session_factory: SessionFactory,
    c: Confirmation,
    tool: InternalTool,
    *,
    requester: Person,
    decider: Person,
    settings: PolicySettings,
    new_arguments: JsonObject,
    raised: tuple[Ask, str] | None,
    ctx: ToolContext,
    now: datetime,
    note: str | None = None,
) -> tuple[AmendOutcome, ActionRecord | None, Confirmation | None]:
    """``amend_confirmation`` for a chain of actions ``c``, locked and still waiting, whose tool
    (``tool``, reached through the registry) plans its steps: Domo gives the whole chain as the
    person wants it now, and it is planned again (``confirmations.replan``). The chain is planned
    and runs for the one who asked (``requester``: their role now, their rights, their tools,
    their context), whoever changes it (``decider``, the requester or an approver of theirs,
    whose yes comes with the change: spec 5.1). ``note`` is what an approver said with the change
    (kept as ``approver_note``, on the request and on the new one that replaces it); ``raised``
    what Domo asks more of it (``amend_confirmation``).

    1. ``new_arguments`` are exactly ``{"steps": [...]}``, a list of calls (``replan.calls_of``);
       an approver's note steps are the notes of the chain as they were written, to the same
       recipients: they never change a note's words or recipient, and may only take a note away
       (``replan.keep_notes_as_written``: ``InvalidArguments`` otherwise); a note step the one
       who asked gives back as their Domo was told of it, while their chain waits for someone
       else's yes, keeps the words they wrote, to whomever they send it now
       (``replan.notes_as_told``);
    2. the steps already done are the first calls, as they were (``replan.done_prefix_holds``),
       and at least one step is left to do; the chain given back as it is changes nothing
       (``replan.unchanged``: ``invalid``, as for any request); each step left is matched with the
       steps still to do of the chain (``replan.match_steps``): kept, changed or added;
    3. the steps left are planned again (``PlansSteps.plan``), after those done: a step kept as
       it was asked for, by the person the chain remembers (its row's ``with_basis``), is judged
       under the rule of the chain, any other with the role of now (spec 4.1, last point). The
       plan's ``InvalidArguments`` come back as they are, and the request still waits;
    4. a ``Refuse`` makes it ``refused``, with the number of the step that refused it
       (``"step"``, from 1, the steps done counted); a change by the one who asked of their chain
       that waits for someone else's yes leaves it as it was (``_change_refused``);
    5. the yes-judge judges the new version (spec 4.1, 4.7), for the one who asked, its steps
       told one by one, with the request's history (``confirmations.chain.request_told``: what it
       asked, the steps it did before the change and does after; nobody had said yes yet, F23).
       Its ``do_not`` makes it ``refused``, with the judge's reason, at once: whoever's agreement
       it would need is never bothered (spec 4.4); a change by the one who asked of their chain
       that waits for someone else's yes leaves it as it was (``_judged_change_refused``);
    6. what it asks then is ``judge.ask_after_a_change`` over every step still to run (F2: no yes
       was given yet; the owner's rule on chains counts too), or a plain yes of whoever answers
       it when that is nothing. When the rights decide whose yes it needs (the one who asked
       changed their chain that waits for someone else's yes, or the plan needs an agreement the
       decider cannot give), it is a new request whatever the judge says; otherwise too, unless
       the words of the change give what is asked (nothing, or the plain yes of a rule:
       ``judge.given_by_the_change``);
    7. a new request replaces it (``replan.replace_chain_request``, ``_ask_the_chain_again``),
       presented by this turn's reply: ``approver_changed`` when its yes is no longer one the
       decider may give, else ``asked_again``. It is the requester's, asking what was found,
       waiting for the agreement the plan needs (the requester's own when it needs none),
       presented to whoever changed it, as ``amend_confirmation`` presents it (``_presented_by``);
    8. otherwise it runs at once, as the decider's yes to it: ``amended_arguments`` hold its
       whole call, its action is the one planned now, and its steps left are the ones planned
       (each with ``with_basis``), after those done; then as any
       yes (``_run_and_settle``). A chain that goes back to waiting for someone else's yes on the
       way is ``approver_changed`` (what its steps did for the one who asked waits behind their
       buttons, on the card of the request); one that runs up to a step that waits for someone's
       arrival is ``paused``.
    """
    if not isinstance(tool, PlansSteps):
        raise InvalidArguments("invalid", "this request can no longer be changed")
    if frozenset(new_arguments) != _CHAIN_CHANGE:
        raise InvalidArguments(
            "invalid",
            "a chain is changed as a whole: give steps, the whole chain as the person wants it now",
        )
    calls = calls_of(new_arguments["steps"])
    rows = await steps_of(session, c.id)
    # The one who asked changes their chain that waits for someone else's yes (spec 5.2).
    for_others = decider.id == requester.id and c.approver_kind != "self"
    if decider.id != requester.id:
        # An approver's Domo was never told the words of a note (D4): each note stays as the
        # one who asked wrote it, to whom they chose, and an approver never changes one (spec 6.7).
        calls = keep_notes_as_written(calls, rows)
    elif for_others:
        # Nor was the one who asked's, while it waits for someone else's yes (spec 5.2): a note
        # given back as he was told of it keeps its words as they wrote them.
        calls = notes_as_told(calls, rows, note_told_as)
    done = [row for row in rows if row.status == "done"]
    if not done_prefix_holds(done, calls):
        raise InvalidArguments(
            "invalid", "the steps already done cannot change: give them first, as they were"
        )
    rest = calls[len(done) :]
    if not rest:
        raise InvalidArguments("invalid", "a chain keeps at least one step to do")
    if unchanged(rows, calls):
        # The chain given back as it is would run as if it were the person's change.
        raise InvalidArguments("invalid", "the change changes nothing: give the steps that change")
    to_do = [row for row in rows if row.status != "done"]
    matches = match_steps(to_do, rest)
    with_basis = {row.position: row.with_basis for row in to_do}
    kept = [
        match.kind == "kept" and match.old_position is not None and with_basis[match.old_position]
        for match in matches
    ]
    actor = Actor(person_id=requester.id, role=as_role(requester.role))
    run_ctx = replace(
        ctx,
        actor=actor,
        person=requester,
        conversation_id=c.conversation_id,
        change_grant=None,
        # The plans due in this turn, if any, are not the chain's.
        due_plans=(),
        asked_before=False,
        # A person's chain is theirs, whatever the turn (ledger n3).
        on_his_own=False,
        # The clues of who speaks are the decider's: never read as the requester's.
        speaker=ctx.speaker if requester.id == decider.id else None,
    )
    done_actions = [DescribedAction.from_json(row.action) for row in done if row.action is not None]
    plan: ChainPlan | None = None
    with anyio.move_on_after(_DESCRIBE_SECONDS):
        plan = await tool.plan(
            {"steps": rest},
            actor,
            run_ctx,
            done=done_actions,
            kept=kept,
            basis=ChainBasis.from_json(c.chain_basis),
            # The changed steps are the decider's words: the people they name are read as the
            # decider names them (spec 6.7).
            speaker=decider.id,
        )
    if plan is None:
        raise TimeoutError("planning the changed chain took too long: nothing was changed")

    decision = plan.decision
    if isinstance(decision, Refuse):
        if for_others:
            # The one who asked changed their chain that waits for someone else's yes: it stays
            # as it was (``_change_refused``).
            _, record, _ = await _change_refused(session, c, decider, actor, plan.action, decision)
        else:
            await _mark_decided(
                session,
                c,
                decider,
                via="change",
                reply_message_id=ctx.reply_message_id,
                now=now,
                answer=None,
            )
            c.approver_note = note
            await _refuse(
                session,
                c,
                decider,
                via="change",
                now=now,
                changed=True,
                replay=(actor, plan.action, decision),
            )
            _use_grant(ctx, c.id)
            record = _refused_yes(
                c, _why_refused(decision, decider, requester.id), limit=_refusal_of(decision)
            )
        if plan.refused_step is not None:
            record = replace(
                record, model_result={**record.model_result, "step": plan.refused_step}
            )
        return "refused", record, c
    replay = (actor, plan.action, decision)
    chain_again = _ChainAgain(c, decider, requester, actor, plan, calls, done, note)
    # The rights may decide whose yes it needs (the one who asked changed their chain that waits
    # for someone else's yes, or the plan needs an agreement the decider cannot give): it is then
    # a new request whatever the judge says, but the judge is consulted on the new version all the
    # same (spec 4.1, 4.4), and what it asks counts.
    by_the_rights = for_others or (
        isinstance(decision, NeedsApproval)
        and not approves(decision.approver, decider, requester.id)
    )

    # The judge judges the new version, for the one who asked, with the request's history,
    # told before the change marks it decided (F23).
    told = [planned_step_told(step) for step in plan.steps]
    request = await request_told(
        session,
        c,
        before=[step_sentence(row, REFERENCE) for row in to_do],
        after=[step.says for step in told],
        now=now,
    )
    judgement = await consult_the_judge(
        run_ctx,
        actor=judged_actor_of(tool, run_ctx),
        action=plan.action,
        says=describe_chain(plan.arguments, REFERENCE),
        rights=rights_say(decision),
        steps=plan.steps,
        request=request,
        owner_rule=owner_rule_of(plan.never_ask),
        # Its reason goes back to whoever changes it, theirs only when the case is.
        told_to=decider.id,
    )
    if judgement.verdict == "do_not" and for_others:
        # Their change of their chain that waits for someone else's yes, which the judge refuses:
        # it never reaches that person, and the chain stays as it was.
        record = await _judged_change_refused(session, c, actor, plan.action, decision, judgement)
        _use_grant(ctx, c.id)
        return "refused", record, c
    await _mark_decided(
        session,
        c,
        decider,
        via="change",
        reply_message_id=ctx.reply_message_id,
        now=now,
        answer=None if judgement.verdict == "do_not" else "yes",
    )
    c.approver_note = note
    if judgement.verdict == "do_not":
        # Refused at once: whoever's agreement it would need is never bothered (spec 4.4).
        await _refuse(
            session,
            c,
            decider,
            via="change",
            now=now,
            changed=True,
            replay=replay,
            judged=judgement,
        )
        _use_grant(ctx, c.id)
        refused = refused_by_the_judge(c.tool_name, plan.action.action_type, judgement.reason)
        return "refused", replace(refused, confirmation_id=c.id), c
    asked = ask_after_a_change(
        judgement,
        old_ask=asked_of(c),
        steps=plan.steps,
        new=[match.kind != "kept" for match in matches],
        raised=raised,
        never_ask=plan.never_ask,
        yes_already_given=False,
        chain_rule=plan.chain_rule,
    )
    if given_by_the_change(asked) and warned_first(judgement, plan.action, plan, plan.never_ask):
        # A step a judge must weigh first (the last key), on a silent judge: asked again.
        asked = Asked("yes", "judge", None)
    if by_the_rights or not given_by_the_change(asked):
        # Asked of whoever answers it: a plain yes when nothing more is (the accord alone).
        return await _ask_the_chain_again(
            session,
            chain_again,
            ctx,
            now,
            asked=asked or Asked("yes", "approver", None),
            judged=judgement,
        )

    # The words of the change give what is asked: the changed chain runs now, as planned (the
    # people its steps name by their whole names, spec 6.7).
    arguments: JsonObject = {"steps": [*calls[: len(done)], *plan.calls()]}
    c.status = "executing"
    c.amended_arguments = arguments
    c.action = dict(plan.action.as_json())
    for row in to_do:
        await session.delete(row)
    await session.flush()
    await add_steps(session, c.id, plan.steps, start=len(done))
    await audit_judgement(
        session, actor=actor, tool_name=c.tool_name, action=plan.action, judgement=judgement
    )
    outcome, record = await _run_and_settle(
        session,
        session_factory,
        c,
        tool=tool,
        action=plan.action,
        arguments=arguments,
        ctx=run_ctx,
        decider=decider,
        via="change",
        changed=True,
    )
    _use_grant(ctx, c.id)
    if paused(record):
        # It ran up to a step that waits for someone's arrival: it pauses there.
        return "paused", record, c
    if waits_again(record):
        # A step asked for a more demanding yes than this one: the chain waits for it now.
        async with session_factory() as reader:
            waiting = (await confirmations_by_id(reader, [c.id]))[c.id]
        return "approver_changed", rerouted_yes(waiting), waiting
    return outcome, record, c


@dataclass(frozen=True)
class _ChainAgain:
    """A chain ``c`` changed by ``decider`` for ``requester`` (``actor``) and planned again
    (``plan``, from the whole ``calls``, the steps ``done`` first), with what an approver said
    (``note``): what its new request is made of."""

    c: Confirmation
    decider: Person
    requester: Person
    actor: Actor
    plan: "ChainPlan"
    calls: list[JsonObject]
    done: list[ConfirmationStep]
    note: str | None


async def _ask_the_chain_again(
    session: AsyncSession,
    again: _ChainAgain,
    ctx: ToolContext,
    now: datetime,
    *,
    asked: Asked | None,
    judged: Judgement | None = None,
) -> tuple[AmendOutcome, ActionRecord | None, Confirmation | None]:
    """The changed chain of ``again`` made a new request that replaces it, in one transaction
    (``replan.replace_chain_request``): asking ``asked``, waiting for the agreement its plan needs
    (the requester's own yes when it needs none), presented to whoever changed it
    (``_presented_by``), with the ``waiting`` action that shows it under this turn's reply, and the
    audit (the judge's verdict when it judged the change, what the barrier said of the chain, then
    the decision, as the outcome), last. Committed."""
    c, plan = again.c, again.plan
    decision = plan.decision
    if isinstance(decision, NeedsApproval):
        approver: Approver = decision.approver
        question = decision.question
    else:
        # Nobody else's agreement is needed: the yes is the requester's own (N1).
        approver, question = "self", None
    outcome, message_id, shown_to_approver = _presented_by(
        ctx, again.decider, again.requester.id, approver, before=c.approver_kind
    )
    new = await replace_chain_request(
        session,
        c,
        requester=again.actor,
        plan=plan,
        calls=again.calls,
        done=again.done,
        approver=approver,
        asked=asked,
        question=question,
        message_id=message_id,
        now=now,
        shown_to_approver=shown_to_approver,
        approver_note=again.note,
    )
    if ctx.domo_message_id is not None:
        await ConversationStore(session).add_action(
            ctx.domo_message_id,
            tool_name=c.tool_name,
            action_type=action_type_of(c),
            status="waiting",
            summary={"key": f"action.card.waiting.{approver}", "params": {}},
            confirmation_id=new.id,
        )
    if judged is not None:
        await audit_judgement(
            session, actor=again.actor, tool_name=c.tool_name, action=plan.action, judgement=judged
        )
    await record_decision(session, again.actor, plan.action, decision, tool_name=c.tool_name)
    await _audit_decided(
        session, c, again.decider, outcome=outcome, via="change", changed=True, replaced_by=new.id
    )
    await session.commit()
    _use_grant(ctx, c.id)
    return outcome, None, new
