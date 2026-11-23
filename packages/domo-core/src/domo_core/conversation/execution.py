"""How Domo acts: one call of the model, from the barrier to the record and the card.

The model is offered the tools the person may use (``tools.registry.tools_for``, with the
registry and the policy of ``web.deps`` and the owner's rights in force), and the prompt says
whether there are any. Each call it makes goes through ``handle_tool_call``, and nothing runs any
other way:

0. the person who asks is read again, as the database holds them now: someone removed while
   their reply streams gets ``refused`` (``not_allowed``), and someone given another role acts
   in it from this call on (the tools offered stay those of the start of the turn, but the
   barrier decides with the role of now);
1. a call beyond the eight an answer may make (``chat.agent``), a name that is no tool's, or
   arguments that are not a JSON object, make the action ``invalid``;
2. the people the call names by a name (a tool that ``NamesPeople``) are named by their whole
   name from then on (``_with_whole_names``, spec 6.7: "Sam" is "Samuel" when he is the one it
   means), so that what follows is about the person found; then the tool describes the call
   (``describe``); arguments it rejects make it ``invalid``, with the tool's reason;
3. the policy barrier decides (``policy.decision.decide_now``: the owner's rights, and the
   facts a limit reads, as they are now, read in a short transaction of its own, then
   ``policy.barrier.decide``; if they cannot be read within ``_CHECK_SECONDS``, or reading them
   fails, the action is ``failed`` and nothing runs): ``Refuse`` makes it ``refused``;
4. for an action that changes something (``catalog.needs_the_judge``), the yes-judge is consulted
   (``consult_the_judge``: its case built from the database, spec 4.2), whatever the barrier said
   but a refusal. ``do_not`` makes it ``refused`` (``refused_by_the_judge``: the judge's reason,
   for Domo to tell in his words). Otherwise what is asked first is the most demanding of the
   judge's verdict, the owner's rule and what Domo asks more (``judge.what_to_ask``; a judge
   that does not answer says ``do``: no type asks anything by default). When the barrier needs
   nobody else's agreement and nothing is asked,
   the person and the barrier are read again (``_decided_again``: the judge may have taken up to
   ``_CHECK_SECONDS`` and ``JUDGE_SECONDS``, and a right taken away or a person removed meanwhile
   counts), and what they say now decides, with the same verdict. Then the call runs
   (``execute_allowed``), which makes it ``done`` or ``failed`` (or ``invalid``, when the tool
   rejects the arguments only then); otherwise it is ``waiting``: the call is
   stored as a request that waits for a yes (``confirmations``), with what it asks, who asked it
   and why, which a yes executes exactly as stored. A tool that does not answer within
   ``_TOOL_SECONDS`` is given up on: the action is ``failed`` with the reason ``timeout``, and
   the model is told that it may have happened all the same, so that it does not do it again
   blindly;
5. the action is stored with the reply (``message_actions``, with its card) and, whenever the
   barrier decided, the judge's verdict (``judge.verdict``) then the decision are audited
   (``policy.decision``), last, then committed. This is a transaction of its own, with its own
   time limit: the tool commits what it does in its own session, before. Should the record fail,
   the log says which action ran unrecorded. A waiting action is stored in the same transaction
   as its request: if that fails, there is no request, and the action is ``failed`` (never
   "waiting for a yes" when nothing waits).

Who each call is judged as (``judged_actor_of``): the person who asks, but Domo himself
(``policy.roles.domo_actor``, his own rights and facts) for a tool whose every call is his own act
(``ActsAsDomo``: making his tools) and for every call of a turn he acts in on his own
(``ToolContext.on_his_own``: a launch for the house woke him). The barrier, the facts a limit reads,
the yes-judge's case and every audit entry of the call are his then; a request that waits is still
stored in the person's conversation, as theirs to answer (``confirmations``: a requester is a
person).

A call that carries the yes of a stop (a tool of Domo's called with ``yes_for``, a tool that
``AsksItsYes``) is never judged: once the barrier let it through, it waits for the stop's approver
(or the barrier's, when that one is more demanding), asking what the stop asks, who asked it (Domo
for a cost, a rule, an approver) and why (what the operation says), whatever the judge would say;
it is stored and shown as any request that waits.

A chain of actions (``tools.chain``, a tool that ``PlansSteps``) is planned instead of steps 2 and
3: its ``plan`` describes each of its steps and puts each through the barrier, within the same
``_TOOL_SECONDS``, and gives the chain's own action and decision. A refused chain says which step
refused it (``"step"``, from 1). The judge judges a chain once, as a whole, with its steps told in
words (``judge.planned_step_told``). A chain that waits is one request, stored with its steps in
the same transaction, with what it remembers of the one who asked (``ChainBasis``) and its call
with its steps flattened, and Domo is told how many steps it has (``"steps"``). A chain let
through runs at once (``execute_allowed``): its tool runs each step in turn, through the barrier
again, as ``confirmations.chain`` does after the yes to a chain that waited; one that waits for
someone's arrival keeps a request all the same, which nobody said yes to (``run_with_a_request``),
for the upkeep to resume it.
 The model reads the result as JSON (``{"status": "done", ...}``, ``{"status":
"waiting_for_confirmation", "by": "you" | "an adult" | "the owner", "answer_with": ..., "asked_by":
..., "why": ...}``, where ``answer_with`` says how the yes can be given
(``confirmations.store.how_to_answer``), ``asked_by`` who asked it and ``why`` why, when there is a
reason, ``{"status": "refused", "reason": ...}``, with ``"limit"``, the summary of the limit that
refused it, when one did, or ``"why"``, the judge's reason, ``{"status": "invalid", "reason":
...}``); it never holds a link, a token or a secret. The people see a card under the reply
(``channels.page.cards.action_card``, with its sentence from ``conversation.cards``): the chat
streams it, and its page shows the stored ones. A part of a card that may only be shown once
(``card_once``) is in the answer that executed the action and nowhere else. When someone else said
yes, that is for the one who asked to ask for, with a button on their card; the card the tool
stores holds only the invitation's identifier, or the provider's, never the link. A step of a
chain always leaves it so, and the chain's card has one button per such step, numbered (spec 4.5).
"""

import json
import logging
import math
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Final, Literal, cast
from zoneinfo import ZoneInfo

import anyio
from sqlalchemy import select

from domo_core.accounts.models import Person
from domo_core.accounts.sessions import is_active
from domo_core.chat.agent import ActionRecord
from domo_core.chat.store import ConversationStore
from domo_core.confirmations.describe import describe_chain, describe_for_people
from domo_core.confirmations.judge import (
    Judgement,
    RememberedLine,
    RequestTold,
    RightsSay,
    StepTold,
    audit_judgement,
    build_case,
    fallback,
    judge,
    owner_rule_of,
    planned_step_told,
    remembered_line,
    what_to_ask,
)
from domo_core.confirmations.steps import PlannedStep, add_steps
from domo_core.confirmations.store import create_confirmation, how_to_answer, let_through
from domo_core.conversation.cards import storable
from domo_core.household.service import whole_names
from domo_core.llm.types import JsonObject, ToolCall
from domo_core.memory.told import for_judge
from domo_core.policy.actions import DescribedAction
from domo_core.policy.asks import Asked
from domo_core.policy.barrier import (
    Allow,
    Decision,
    NeedsApproval,
    Refuse,
    record_decision,
    writes_for_domo,
)
from domo_core.policy.catalog import (
    CHANGES_NOTHING,
    JUDGED_ON_THE_TURN_S_WORDS,
    JUDGED_WITHIN,
    is_made,
    is_silent,
    needs_the_judge,
)
from domo_core.policy.decision import ChainBasis, decide_now
from domo_core.policy.rights import Approver
from domo_core.policy.roles import Actor, as_role
from domo_core.policy.settings import load_policy_settings
from domo_core.texts.view import REFERENCE

# From ``tools.context``, not ``tools.registry``: the registry holds the tools that manage
# requests, which run through this module (``confirmations.service``); and the chain of actions,
# which runs its steps through this module too (``confirmations.chain``): its plan is only a type
# here.
from domo_core.tools.context import (
    AsksItsYes,
    AsksWhomItsYes,
    BoundsItsOwnSteps,
    InternalTool,
    InvalidArguments,
    NamesPeople,
    PlansSteps,
    ToolContext,
    ToolOutcome,
    WaitsForAYes,
    judged_actor_of,
)

if TYPE_CHECKING:
    from domo_core.tools.chain import ChainPlan

logger = logging.getLogger(__name__)

# How long a tool may take to describe a call, and then to run it (see ``execute_allowed``).
_TOOL_SECONDS: Final = 60.0
# How long reading the person again before a call may take (see ``read_the_person_again``), and
# reading the rights for the barrier's decision (see ``_decide_now``).
_CHECK_SECONDS: Final = 15.0
# How long reading what the judge may read of the memories may take (see ``_remembered``).
_REMEMBERED_SECONDS: Final = 5.0
# How long storing an action and its audit entry may take (see ``store_action``).
_STORE_SECONDS: Final = 15.0
# The longest tool name that is kept: the model may call any name (providers allow 64).
_MAX_TOOL_NAME_LENGTH: Final = 64
# The longest arguments of a call that are read, in characters of JSON.
_MAX_ARGUMENTS_LENGTH: Final = 16_000

# Who says yes to a request, as the model is told (``"by"``).
WHO_SAYS_YES: Final[Mapping[Approver, str]] = {
    "self": "you",
    "adult": "an adult",
    "owner": "the owner",
}
# Whose yes is more demanding: the requester's own, an adult's, the owner's.
_APPROVERS_IN_ORDER: Final[tuple[Approver, ...]] = ("self", "adult", "owner")


def _arguments_of(raw: str) -> JsonObject | None:
    """The arguments of a call, if they are a JSON object (and not absurdly long or deep)."""
    if len(raw) > _MAX_ARGUMENTS_LENGTH:
        return None
    try:
        arguments: object = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    if not isinstance(arguments, dict):
        return None
    return cast(JsonObject, arguments)


def invalid_record(
    tool_name: str,
    action_type: str | None,
    reason: str,
    detail: str | None = None,
    setting: tuple[str, int] | None = None,
) -> ActionRecord:
    """An action whose call or arguments could not be acted on (``reason`` says why; a setting
    of Domo's that refused it, ``setting``, is given with its key and figure)."""
    result: JsonObject = {"status": "invalid", "reason": reason}
    if detail is not None:
        result["detail"] = detail
    if setting is not None:
        result["setting"], result["value"] = setting
    card: JsonObject = {"key": f"action.card.invalid.{reason}", "params": {}, "reason": reason}
    return ActionRecord(tool_name, action_type, "invalid", None, result, card)


def failed_record(tool_name: str, action_type: str | None) -> ActionRecord:
    """An action that did not work (nothing more is told: the details may hold what the
    arguments held)."""
    card: JsonObject = {"key": "action.card.failed", "params": {}}
    return ActionRecord(tool_name, action_type, "failed", None, {"status": "failed"}, card)


def _timed_out(tool_name: str, action_type: str) -> ActionRecord:
    """An action whose tool did not answer in time: nobody knows whether it happened."""
    result: JsonObject = {
        "status": "failed",
        "reason": "timeout",
        "outcome": "unknown",
        "detail": (
            "the action took too long and was given up on, but it may have happened anyway: "
            "do not try it again before checking, and tell the person it is not known"
        ),
    }
    card: JsonObject = {"key": "action.card.failed.timeout", "params": {}, "reason": "timeout"}
    return ActionRecord(tool_name, action_type, "failed", None, result, card)


async def execute_allowed(
    tool: InternalTool, tool_name: str, action_type: str, arguments: JsonObject, ctx: ToolContext
) -> ActionRecord:
    """Run a call the barrier allowed. Whatever goes wrong becomes the action's status; a tool
    that does not answer within ``_TOOL_SECONDS`` is given up on (``failed``, reason
    ``timeout``), except a tool that bounds each of its own steps (``BoundsItsOwnSteps``). A tool
    that managed a pending request names it (``ToolOutcome.confirmation_id``): the record carries
    it, so that the request's card is shown under the reply. A tool whose call is stored under
    another type of action than its own says which (``ToolOutcome.recorded_as``: the rest of a
    chain continued that runs at once is a chain that ran at once, ``chain.run``)."""
    outcome: ToolOutcome | None = None
    seconds = math.inf if isinstance(tool, BoundsItsOwnSteps) else _TOOL_SECONDS
    with anyio.move_on_after(seconds):
        try:
            outcome = await tool.execute(arguments, ctx)
        except InvalidArguments as error:
            return invalid_record(tool_name, action_type, error.code, error.detail, error.setting)
        except Exception as error:
            # Only the kind of error: its message may hold what the arguments held.
            logger.error("The tool %s failed (%s)", tool_name, type(error).__name__)
            return failed_record(tool_name, action_type)
    if outcome is None:
        logger.error(
            "The tool %s took too long: whether %s happened is unknown", tool_name, action_type
        )
        return _timed_out(tool_name, action_type)
    result: JsonObject = {"status": outcome.status}
    result.update((key, value) for key, value in outcome.model_result.items() if key != "status")
    return ActionRecord(
        tool_name=tool_name,
        action_type=action_type if outcome.recorded_as is None else outcome.recorded_as,
        status=outcome.status,
        confirmation_id=outcome.confirmation_id,
        model_result=result,
        card=outcome.card,
        card_once=outcome.card_once,
    )


async def store_action(
    ctx: ToolContext,
    reply_id: uuid.UUID,
    record: ActionRecord,
    *,
    action: DescribedAction | None = None,
    decision: Decision | None = None,
    judged: Judgement | None = None,
    actor: Actor | None = None,
) -> uuid.UUID | None:
    """Store the action with the reply and, when the barrier decided, audit the judge's verdict
    when it was asked (``judged``) then the decision: last, just before the commit (the audit log
    holds a lock until the transaction ends), both under ``actor``, who the call was judged as
    (``judged_actor_of``; when not given, no call was judged: Domo in a turn on his own, else
    the person), and the action is marked as his when it is (``MessageAction.by_domo``: it is
    then in his history, never the person's). The card is stored, never ``card_once``. Returns the
    id of the action stored, ``None`` when it was not.

    It is one attempt, shielded from cancellation (a reader who leaves must not stop the record
    of what was done) and limited to ``_STORE_SECONDS``. A failure is logged (its kind only) and
    the turn goes on: what came of the action is still told to the model and shown."""
    judged_as = ctx.turn_subject if actor is None else actor
    with anyio.move_on_after(_STORE_SECONDS, shield=True):
        try:
            async with ctx.session_factory() as session:
                stored = await ConversationStore(session).add_action(
                    reply_id,
                    tool_name=record.tool_name,
                    action_type=record.action_type or "",
                    status=record.status,
                    summary=record.card,
                    confirmation_id=record.confirmation_id,
                    by_domo=judged_as.subject == "domo",
                )
                if action is not None and judged is not None:
                    await audit_judgement(
                        session,
                        actor=judged_as,
                        tool_name=record.tool_name,
                        action=action,
                        judgement=judged,
                    )
                if action is not None and decision is not None:
                    await record_decision(
                        session, judged_as, action, decision, tool_name=record.tool_name
                    )
                await session.commit()
            return stored.id
        except Exception as error:
            logger.error("An action could not be recorded (%s)", type(error).__name__)
            return None
    logger.error("Recording an action took too long")
    return None


async def _recorded(
    ctx: ToolContext,
    reply_id: uuid.UUID,
    record: ActionRecord,
    *,
    action: DescribedAction | None = None,
    decision: Decision | None = None,
    judged: Judgement | None = None,
    actor: Actor | None = None,
) -> ActionRecord:
    """``store_action``, and the record with the id of the action stored (``action_id``), when it
    was."""
    action_id = await store_action(
        ctx, reply_id, record, action=action, decision=decision, judged=judged, actor=actor
    )
    return record if action_id is None else replace(record, action_id=action_id)


async def _store_waiting(
    ctx: ToolContext,
    reply_id: uuid.UUID,
    record: ActionRecord,
    *,
    arguments: JsonObject,
    action: DescribedAction,
    decision: Decision,
    approver: Approver,
    asked: Asked,
    question: str | None = None,
    steps: Sequence[PlannedStep] = (),
    chain_basis: ChainBasis | None = None,
    judged: Judgement | None = None,
    actor: Actor | None = None,
    domo_asks: bool = False,
) -> ActionRecord | None:
    """Store the request that waits for a yes, the waiting action that shows it, and the audit
    (the judge's verdict when it was asked, ``judged``, then the barrier's ``decision``, under
    ``actor``, who the call was judged as: ``store_action``), last, in one transaction. The
    request waits in the person's conversation (``ctx.actor`` is its requester there); one made in
    a turn on his own is Domo's (``Confirmation.by_domo``), judged again and run as his, and
    so is one a call of his says is his own (``domo_asks``: ``WaitsForAYes.domo_asks``).
    Returns the record with its ``confirmation_id`` and, in what the model reads, how the yes
    can be given (``answer_with``, from what the request asks: ``how_to_answer``); or
    ``None`` when nothing could be stored (then no request exists).

    The request waits for ``approver`` (``self``: the person's own yes), asks what ``asked`` says,
    with the question a limit asked (``question``). A chain's ``steps`` are stored with its
    request, and ``chain_basis``, what it remembers of the one who asked.

    Shielded and limited like ``store_action``."""
    judged_as = ctx.turn_subject if actor is None else actor
    with anyio.move_on_after(_STORE_SECONDS, shield=True):
        try:
            async with ctx.session_factory() as session:
                confirmation = await create_confirmation(
                    session,
                    requester=ctx.actor,
                    tool_name=record.tool_name,
                    arguments=arguments,
                    action=action,
                    approver=approver,
                    asked=asked,
                    conversation_id=ctx.conversation_id,
                    message_id=reply_id,
                    now=ctx.clock(),
                    question=question,
                    chain_basis=chain_basis,
                    by_domo=ctx.on_his_own or domo_asks,
                    # His request, shown to its approver in the approver's own conversation: this
                    # reply shows it to them, and their next words may answer it (R17).
                    shown_to_approver=reply_id if domo_asks else None,
                    # The ``allow`` written below in his name counts it in his history already.
                    counted_for_domo=isinstance(decision, Allow)
                    and writes_for_domo(judged_as, action),
                )
                await add_steps(session, confirmation.id, steps)
                waiting = replace(
                    record,
                    confirmation_id=confirmation.id,
                    model_result={
                        **record.model_result,
                        "answer_with": how_to_answer(confirmation),
                    },
                )
                await ConversationStore(session).add_action(
                    reply_id,
                    tool_name=waiting.tool_name,
                    action_type=waiting.action_type or "",
                    status=waiting.status,
                    summary=waiting.card,
                    confirmation_id=waiting.confirmation_id,
                    by_domo=judged_as.subject == "domo",
                )
                if judged is not None:
                    await audit_judgement(
                        session,
                        actor=judged_as,
                        tool_name=record.tool_name,
                        action=action,
                        judgement=judged,
                    )
                await record_decision(
                    session, judged_as, action, decision, tool_name=record.tool_name
                )
                await session.commit()
            return waiting
        except Exception as error:
            logger.error("A request could not be stored (%s)", type(error).__name__)
            return None
    logger.error("Storing a request took too long")
    return None


# What the model is told when a limit could not be checked (spec 5.5).
LIMIT_ERROR_DETAIL: Final = (
    "a limit set by the owner could not be checked, so nothing was done: tell the person, who may "
    "ask the owner"
)

# What the model is told when the person may not do the action (``not_allowed``): the reason is a
# word of the code, which he repeated to the owner as it is ("refused (not_allowed)", the trial of
# 0.8.0). In words he can say.
NOT_ALLOWED_DETAIL: Final = (
    "the person may not do this, with the role and the rights they have now, so nothing was "
    "done: tell them so in your own words, never with the word of the reason"
)


def _refused(
    tool_name: str,
    action_type: str | None,
    reason: str,
    *,
    limit: str | None = None,
    side: str | None = None,
) -> ActionRecord:
    """An action that is refused, and why (its card says it plainly). ``limit`` is the summary
    of the limit that refused it, if one did: Domo may tell it to the person, as the owner
    approved it (spec 5.8). ``side`` is whose judgement refused an action judged both for the
    person and for Domo (``policy.barrier.stricter``): Domo is told whose rights it was."""
    model_result: JsonObject = {"status": "refused", "reason": reason}
    if limit is not None:
        model_result["limit"] = limit
    if side is not None:
        model_result["whose_rights"] = _WHOSE_RIGHTS[side]
    if reason == "limit_error":
        model_result["detail"] = LIMIT_ERROR_DETAIL
    elif reason == "not_allowed":
        model_result["detail"] = NOT_ALLOWED_DETAIL
    return ActionRecord(
        tool_name=tool_name,
        action_type=action_type,
        status="refused",
        confirmation_id=None,
        model_result=model_result,
        card={"key": f"action.card.refused.{reason}", "params": {}, "reason": reason},
    )


# Whose rights refused an action judged both for the person and for Domo, as Domo is told.
_WHOSE_RIGHTS: Final = {"person": "the person's", "domo": "yours"}

# What the model is told when the judge refused an action (spec 4.6): the person may explain.
JUDGE_REFUSED_DETAIL: Final = (
    "The assistant judged not to do it: tell the person why, in your own words; if they explain "
    "and you "
    "are convinced, ask again"
)


def refused_by_the_judge(tool_name: str, action_type: str, reason: str) -> ActionRecord:
    """An action the judge refused (``do_not``), with its reason: Domo tells it in his words,
    and the card says it (``action.card.refused.judge``)."""
    return ActionRecord(
        tool_name=tool_name,
        action_type=action_type,
        status="refused",
        confirmation_id=None,
        model_result={
            "status": "refused",
            "reason": "judge",
            "why": reason,
            "detail": JUDGE_REFUSED_DETAIL,
        },
        card={"key": "action.card.refused.judge", "params": {"reason": reason}, "reason": "judge"},
    )


def rights_say(decision: Allow | NeedsApproval) -> RightsSay:
    """What the barrier said, as the judge reads it: allowed, and whose agreement is needed
    first, if anyone's, with the question a limit asked."""
    if isinstance(decision, NeedsApproval):
        return RightsSay(allowed=True, needs=decision.approver, question=decision.question)
    return RightsSay(allowed=True, needs=None, question=None)


async def _remembered(
    ctx: ToolContext, actor: Actor, action: DescribedAction, says: str, zone: ZoneInfo | None
) -> tuple[RememberedLine, ...]:
    """What the judge may read of the person's memories about ``action`` (``memory.told.
    for_judge``: what Domo told them before such an action, then what the search finds from
    ``says``), in a short transaction of its own, within ``_REMEMBERED_SECONDS``, each dated in
    the household's time zone (``zone``). Nothing when they cannot be read, or not in time: the
    judgement never fails nor waits for them. Nothing for Domo on his own: no person's
    memories are his case."""
    if actor.subject == "domo":
        return ()
    found: tuple[RememberedLine, ...] | None = None
    with anyio.move_on_after(_REMEMBERED_SECONDS):
        try:
            async with ctx.session_factory() as session:
                memories = await for_judge(
                    session, actor.person_id, action.action_type, says, embedder=ctx.embedder
                )
                found = tuple(remembered_line(memory, zone) for memory in memories)
        except Exception as error:
            logger.warning("The memories of the judge could not be read (%s)", type(error).__name__)
            return ()
    if found is None:
        logger.warning("Reading the memories of the judge took too long")
        return ()
    return found


def _words_of_plans(ctx: ToolContext) -> tuple[str, ...] | None:
    """What the judge reads as the person's words in a turn a plan woke Domo for
    (``ToolContext.due_plans``): the exact words the plans due keep, checked when they were set
    (none for a plan he made alone); ``None`` in any other turn (their last messages are read)."""
    if not ctx.due_plans:
        return None
    return tuple(plan.their_words for plan in ctx.due_plans if plan.their_words)


def judges_the_words_of(ctx: ToolContext, action: DescribedAction, actor: Actor) -> Actor:
    """Whose case the yes-judge reads for ``action``, judged as ``actor`` (ruling R18): for an act
    of Domo's of ``JUDGED_ON_THE_TURN_S_WORDS`` in a person's turn, the person's (their words,
    their requests), since it is what they asked for; any other, and every call of a turn he acts
    in on his own, ``actor``'s. Only the judge's case: the barrier and the audit keep ``actor``."""
    if (
        actor.subject == "domo"
        and not ctx.on_his_own
        and action.action_type in JUDGED_ON_THE_TURN_S_WORDS
    ):
        return ctx.actor
    return actor


async def consult_the_judge(
    ctx: ToolContext,
    *,
    actor: Actor,
    action: DescribedAction,
    says: str,
    rights: RightsSay,
    steps: Sequence[StepTold] = (),
    request: RequestTold | None = None,
    owner_rule: JsonObject | None = None,
) -> Judgement:
    """The yes-judge's verdict on ``action`` for the person of ``ctx`` (spec 4): its case is built
    from the database (``judge.build_case``) in a short transaction of its own, within
    ``_CHECK_SECONDS``, with the household's time zone; then the judge's model of the turn judges
    it (``judge.judge``): ``ctx.models.model_for("judge")``, never under ``medium``, and ``None``
    outside a turn or when the key of the request has no such model. It never raises: a case that
    cannot be built is a ``do`` (``fallback``, logged by its kind), as is a judge that has no
    model or does not answer: nothing is asked of the person because the judge was silent. That
    holds for a request the person made (``ToolContext.they_asked``: the turn answers a message of
    theirs and was given nothing from outside). In a turn Domo started himself (a wake, an
    arrival) or that was given words that are not theirs (a note someone left them), nobody is
    known to have asked: the same silence is a ``yes``. In a turn a plan woke him for, the words
    the judge reads are those the plans due keep (``_words_of_plans``), and the person asked only
    when one keeps some (``ToolContext.asked_before``). The case holds what the judge may read of
    the person's memories (``_remembered``: nothing when they cannot be read). An
    answer that is no verdict is a lesson about the judge's model, kept by the models
    of the turn (``ModelPicker.note_failure``). The case is ``actor``'s, who the call is judged as
    (``judged_actor_of``): for Domo (a turn he acts in on his own), his name, his history,
    nobody's words, never the person's whose conversation it is."""
    case = None
    zone: ZoneInfo | None = None
    they_asked = ctx.they_asked
    with anyio.move_on_after(_CHECK_SECONDS):
        try:
            async with ctx.session_factory() as session:
                settings = await load_policy_settings(session)
                zone = settings.time_zone
                case = await build_case(
                    session,
                    actor=actor,
                    action=action,
                    says=says,
                    rights=rights,
                    conversation_id=ctx.conversation_id,
                    now=ctx.clock(),
                    zone=settings.time_zone,
                    steps=steps,
                    request=request,
                    owner_rule=owner_rule,
                    speaker=ctx.speaker,
                    words_override=_words_of_plans(ctx),
                )
        except Exception as error:
            logger.error("The case of the judge could not be built (%s)", type(error).__name__)
            return fallback("error", they_asked=they_asked)
    if case is None:
        logger.error("Building the case of the judge took too long")
        return fallback("error", they_asked=they_asked)
    case = replace(case, remembered=await _remembered(ctx, actor, action, says, zone))
    try:
        with anyio.fail_after(_CHECK_SECONDS):
            model = None if ctx.models is None else await ctx.models.model_for("judge")
        # An answer that is no verdict is a lesson about the judge's model.
        return await judge(model, case, learns=ctx.models, they_asked=they_asked)
    except Exception as error:
        logger.error("The judge failed (%s)", type(error).__name__)
        return fallback("error", they_asked=they_asked)


async def run_with_a_request(
    ctx: ToolContext,
    reply_id: uuid.UUID,
    record: ActionRecord,
    *,
    plan: "ChainPlan",
    tool: InternalTool,
    tool_name: str,
    judged: Judgement | None = None,
) -> ActionRecord:
    """A chain the judge let through that waits for someone's arrival (``plan.waits``): nobody said
    yes, but the upkeep needs a request to resume it (spec 4.7). In one transaction: the request
    (``self``, asking nothing, with the chain's basis, its call with its steps flattened and its
    steps), made ``executing`` at once by the one who asked (``store.let_through``, ``decided_via``
    ``judge``), the action stored under the reply with that request as one that waits on it (its
    line in the history follows the request: running, paused, done or failed), and the audit (the
    judge's verdict, then the barrier's decision), last. Then the chain runs under that request as
    under a yes (``tools.chain.run_under_the_same_yes``): up to its first wait, where it pauses, or
    to its end, or to the step that stops it. What Domo reads is the chain's (``chain_outcome``),
    with the request's id (``"request_id"``). When the request cannot be stored, nothing ran: the
    action is ``failed``."""
    # Here, not at the top: the chain's module imports this one.
    from domo_core.tools.chain import UnderTheSameYes, run_under_the_same_yes

    request_id: uuid.UUID | None = None
    with anyio.move_on_after(_STORE_SECONDS, shield=True):
        try:
            async with ctx.session_factory() as session:
                now = ctx.clock()
                confirmation = await create_confirmation(
                    session,
                    requester=ctx.actor,
                    tool_name=tool_name,
                    arguments=plan.arguments,
                    action=plan.action,
                    approver="self",
                    asked=None,
                    conversation_id=ctx.conversation_id,
                    message_id=reply_id,
                    now=now,
                    chain_basis=plan.basis,
                )
                await add_steps(session, confirmation.id, plan.steps)
                let_through(confirmation, ctx.person.id, now)
                await session.flush()
                # Stored as an action that waits on its request (review I2): Domo's line of it,
                # in the turns after, follows the request (running, paused, done, failed), never
                # saying it was done before it was (``conversation.prepare``).
                await ConversationStore(session).add_action(
                    reply_id,
                    tool_name=tool_name,
                    action_type=plan.action.action_type,
                    status="waiting",
                    summary={"key": "action.card.running", "params": {}},
                    confirmation_id=confirmation.id,
                )
                if judged is not None:
                    await audit_judgement(
                        session,
                        actor=ctx.actor,
                        tool_name=tool_name,
                        action=plan.action,
                        judgement=judged,
                    )
                await record_decision(
                    session, ctx.actor, plan.action, plan.decision, tool_name=tool_name
                )
                await session.commit()
                request_id = confirmation.id
        except Exception as error:
            logger.error("A chain let through could not be stored (%s)", type(error).__name__)
    if request_id is None:
        return failed_record(tool_name, plan.action.action_type)
    taken = UnderTheSameYes(
        list(plan.steps), plan.basis, decider=ctx.person, via="judge", tool_name=tool_name
    )
    outcome, ran = await run_under_the_same_yes(ctx, request_id, taken)
    return replace(
        ran,
        confirmation_id=request_id,
        model_result={**ran.model_result, "request_id": str(request_id)},
        card=outcome.card,
    )


async def read_the_person_again(ctx: ToolContext) -> Literal["active", "gone", "unknown"]:
    """Read the person who asks again, as the database holds them now, and make ``ctx`` theirs
    when they are still in the household (``person``, and the role of ``actor``): someone
    removed, or made a guest or a child, while their reply streams acts from the next call on as
    what they are now. ``ctx`` is changed in place: it is the turn's, and the rest of the turn
    goes on with the person as they are. The runner of a chain of actions reads the one who
    asked again before each step, the same way (``confirmations.chain``).

    ``gone``: no longer in the household (removed, or a guest whose stay ended); ``unknown``:
    they could not be read within ``_CHECK_SECONDS``."""
    with anyio.move_on_after(_CHECK_SECONDS):
        try:
            async with ctx.session_factory() as session:
                result = await session.execute(select(Person).where(Person.id == ctx.person.id))
                person = result.scalar_one_or_none()
        except Exception as error:
            logger.error("The person could not be read again (%s)", type(error).__name__)
            return "unknown"
        if person is None or not is_active(person, ctx.clock()):
            return "gone"
        ctx.person = person
        ctx.actor = replace(ctx.actor, role=as_role(person.role))
        return "active"
    logger.error("Reading the person again took too long")
    return "unknown"


async def _decide_now(ctx: ToolContext, action: DescribedAction, actor: Actor) -> Decision | None:
    """The barrier's decision on ``action`` for ``actor``, who the call is judged as
    (``judged_actor_of``), with the owner's rights and the facts of the moment as they are now
    (``decide_now``), read in a short transaction of its own; ``None`` when they could not be
    read, or not within ``_CHECK_SECONDS`` (nothing may run then)."""
    with anyio.move_on_after(_CHECK_SECONDS):
        try:
            async with ctx.session_factory() as session:
                return await decide_now(session, actor, action, ctx.policy, ctx.clock())
        except Exception as error:
            logger.error("The rights could not be read for a decision (%s)", type(error).__name__)
            return None
    logger.error("Reading the rights for a decision took too long")
    return None


async def _decided_again(
    ctx: ToolContext, tool: InternalTool, tool_name: str, action: DescribedAction
) -> Decision | ActionRecord:
    """Once the judge let ``action`` run, what the barrier says of it now: the person read again
    (``read_the_person_again``), then ``_decide_now`` for who the call of ``tool`` is judged
    as. The judge may take a while, and what changed meanwhile counts before anything runs:
    someone no longer in the household is ``refused`` (``not_allowed``); a person or rights that
    cannot be read make it ``failed`` (nothing runs); otherwise the decision of now."""
    match await read_the_person_again(ctx):
        case "gone":
            logger.warning(
                "A call of the model was refused: the person left the household while the "
                "judge answered"
            )
            return _refused(tool_name, action.action_type, "not_allowed")
        case "unknown":
            return failed_record(tool_name, action.action_type)
        case "active":
            pass
    decision = await _decide_now(ctx, action, judged_actor_of(tool, ctx))
    if decision is None:
        return failed_record(tool_name, action.action_type)
    return decision


async def _with_whole_names(
    tool: InternalTool, tool_name: str, arguments: JsonObject, ctx: ToolContext
) -> JsonObject | None:
    """``arguments``, with the people a tool that names them (``NamesPeople``) names by their
    whole name (``household.service.whole_names``): what is described, judged, asked, stored and
    run is about the person found, as the people who read it read it (spec 6.7). ``None`` when
    the names cannot be read, or take more than ``_CHECK_SECONDS`` (nothing then runs)."""
    if not isinstance(tool, NamesPeople):
        return arguments
    with anyio.move_on_after(_CHECK_SECONDS):
        try:
            async with ctx.session_factory() as session:
                return await whole_names(
                    session,
                    arguments,
                    tool.person_arguments,
                    now=ctx.clock(),
                    speaker=ctx.person.id,
                )
        except Exception as error:
            logger.error(
                "The names of a call of %s could not be read (%s)", tool_name, type(error).__name__
            )
            return None
    logger.error("Reading the names of a call of %s took too long", tool_name)
    return None


async def _described_and_decided(
    tool: InternalTool, tool_name: str, arguments: JsonObject, ctx: ToolContext, actor: Actor
) -> "tuple[DescribedAction, Decision, ChainPlan | None] | ActionRecord":
    """What the call would do and the barrier's decision on it, for ``actor`` (who the call is
    judged as, ``judged_actor_of``): the
    tool's description, then ``_decide_now``; or, for a chain (``PlansSteps``), its plan, which
    describes and decides each of its steps (the plan is the third value, ``None`` for any other
    tool). Describing or planning may take ``_TOOL_SECONDS``. When nothing can be decided, the
    record of the call instead: ``invalid`` (with the tool's reason) or ``failed`` (it failed, or
    took too long; either way nothing happened: describing and planning change nothing)."""
    if isinstance(tool, PlansSteps):
        plan: ChainPlan | None = None
        with anyio.move_on_after(_TOOL_SECONDS):
            try:
                plan = await tool.plan(arguments, actor, ctx)
            except InvalidArguments as error:
                return invalid_record(
                    tool_name, tool.action_type, error.code, error.detail, error.setting
                )
            except Exception as error:
                logger.error(
                    "The tool %s could not plan its steps (%s)", tool_name, type(error).__name__
                )
        if plan is None:
            return failed_record(tool_name, tool.action_type)
        return plan.action, plan.decision, plan
    action: DescribedAction | None = None
    with anyio.move_on_after(_TOOL_SECONDS):
        try:
            action = await tool.describe(arguments, actor, ctx)
        except InvalidArguments as error:
            return invalid_record(
                tool_name, tool.action_type, error.code, error.detail, error.setting
            )
        except Exception as error:
            logger.error(
                "The tool %s could not describe a call (%s)", tool_name, type(error).__name__
            )
    if action is None:
        return failed_record(tool_name, tool.action_type)
    decision = await _decide_now(ctx, action, actor)
    if decision is None:
        # Nothing was decided, so nothing ran: the action failed.
        return failed_record(tool_name, action.action_type)
    return action, decision, None


async def _waits(
    ctx: ToolContext,
    reply_id: uuid.UUID,
    tool_name: str,
    arguments: JsonObject,
    action: DescribedAction,
    decision: Allow | NeedsApproval,
    asked: Asked | None,
    plan: "ChainPlan | None",
    judged: Judgement | None,
    *,
    approver_at_least: Approver = "self",
    actor: Actor | None = None,
    domo_asks: bool = False,
) -> ActionRecord:
    """The call stored as a request that waits (``_store_waiting``): for the agreement the barrier
    asks (``decision``), else for the person's own yes, asking what ``asked`` says (a plain yes of
    the approver when nothing more is asked); for ``approver_at_least`` when that one is more
    demanding (a call that carries the yes of a stop); in a turn Domo acts in on his own, the
    owner's rather than his own (the request is his: he never answers it). Nothing has happened:
    the result says so,
    who must say yes, how, who asked it and why; for a chain, how many steps it has. ``failed``
    when no request could be stored (nobody may then be told that a yes is awaited)."""
    approver: Approver = decision.approver if isinstance(decision, NeedsApproval) else "self"
    if _APPROVERS_IN_ORDER.index(approver_at_least) > _APPROVERS_IN_ORDER.index(approver):
        approver = approver_at_least
    if ctx.on_his_own and approver == "self":
        # His own request: Domo never gives its yes himself; he asks the owner (who is woken
        # for the house).
        approver = "owner"
    asking = asked or Asked("yes", "approver", None)
    waits: JsonObject = {"status": "waiting_for_confirmation", "by": WHO_SAYS_YES[approver]}
    if plan is not None:
        waits["steps"] = len(plan.steps)
    waits["asked_by"] = asking.by
    if asking.why is not None:
        waits["why"] = asking.why
    record = ActionRecord(
        tool_name=tool_name,
        action_type=action.action_type,
        status="waiting",
        confirmation_id=None,
        model_result=waits,
        card={"key": f"action.card.waiting.{approver}", "params": {}},
    )
    waiting = await _store_waiting(
        ctx,
        reply_id,
        record,
        # A chain's call with its steps flattened: what its card counts and tells.
        arguments=arguments if plan is None else plan.arguments,
        action=action,
        decision=decision,
        approver=approver,
        asked=asking,
        question=decision.question if isinstance(decision, NeedsApproval) else None,
        steps=() if plan is None else plan.steps,
        chain_basis=None if plan is None else plan.basis,
        judged=judged,
        actor=actor,
        domo_asks=domo_asks,
    )
    if waiting is not None:
        return waiting
    # No request exists: nobody may be told that a yes is awaited.
    return failed_record(tool_name, action.action_type)


async def _whose_yes(
    tool: InternalTool, arguments: JsonObject, ctx: ToolContext
) -> Approver | None:
    """Whose yes a request of this call waits for, when it is not the person's own
    (``AsksWhomItsYes``); ``None`` for any other tool, or when it cannot be read (logged: the
    request then waits for the usual yes)."""
    if not isinstance(tool, AsksWhomItsYes):
        return None
    try:
        with anyio.fail_after(_CHECK_SECONDS):
            return await tool.whose_yes(arguments, ctx)
    except Exception as error:
        logger.error("Whose yes a call waits for could not be read (%s)", type(error).__name__)
        return None


async def _waits_for_a_yes(
    tool: InternalTool, tool_name: str, arguments: JsonObject, ctx: ToolContext
) -> WaitsForAYes | ActionRecord | None:
    """What a call of ``tool`` waits for when it carries the yes of a stop (``AsksItsYes``):
    ``None`` for any other call; the record of the call when it cannot be read (``invalid`` with
    the tool's reason, or ``failed``)."""
    if not isinstance(tool, AsksItsYes):
        return None
    try:
        with anyio.fail_after(_CHECK_SECONDS):
            return await tool.waits_for(arguments, ctx)
    except InvalidArguments as error:
        return invalid_record(tool_name, tool.action_type, error.code, error.detail, error.setting)
    except Exception as error:
        logger.error("The yes a call carries could not be read (%s)", type(error).__name__)
        return failed_record(tool_name, tool.action_type)


async def handle_tool_call(
    call: ToolCall, *, ctx: ToolContext, reply_id: uuid.UUID, over_limit: bool = False
) -> ActionRecord:
    """Put one call of the model through the barrier, run it if allowed, and record it (see the
    module docstring). Every outcome is a record, and nothing here raises.

    The person who asks is read again first (``read_the_person_again``): someone who is no
    longer in the household gets ``refused`` (``not_allowed``), and nothing runs; if they cannot
    be read, the action is ``failed``.

    ``over_limit`` is a call beyond the number an answer may make: it is recorded as ``invalid``
    (``too_many``) and never runs. The effect of a tool and its record are two transactions (the
    tool commits in its own session, the record comes after): if the record of an action that
    the barrier allowed cannot be written, the log says which one, for an operator to reconcile.
    """
    tool_name = storable(call.name)[:_MAX_TOOL_NAME_LENGTH]
    tool = ctx.registry.get(call.name)
    if tool is None and ctx.made_tool_named is not None:
        # A tool of Domo the turn did not offer (past the ones it offers): found now, and it
        # goes the same way as an offered one; any other name is no tool.
        try:
            tool = await ctx.made_tool_named(call.name)
        except Exception as error:
            logger.error(
                "A tool of the assistant could not be looked for (%s)", type(error).__name__
            )
    match await read_the_person_again(ctx):
        case "gone":
            logger.warning(
                "A call of the model was refused: the person is no longer in the household"
            )
            record = _refused(tool_name, None if tool is None else tool.action_type, "not_allowed")
            return await _recorded(ctx, reply_id, record)
        case "unknown":
            record = failed_record(tool_name, None if tool is None else tool.action_type)
            return await _recorded(ctx, reply_id, record)
        case "active":
            pass
    if over_limit:
        record = invalid_record(
            tool_name,
            None if tool is None else tool.action_type,
            "too_many",
            "an answer may make only a few calls: ask again for the ones that matter",
        )
        return await _recorded(ctx, reply_id, record)
    if tool is None:
        record = invalid_record(tool_name, None, "unknown_tool")
        return await _recorded(ctx, reply_id, record)
    arguments = _arguments_of(call.arguments)
    if arguments is None:
        record = invalid_record(
            tool_name, tool.action_type, "invalid", "the arguments must be a JSON object"
        )
        return await _recorded(ctx, reply_id, record)
    understood = await _with_whole_names(tool, tool_name, arguments, ctx)
    if understood is None:
        record = failed_record(tool_name, tool.action_type)
        return await _recorded(ctx, reply_id, record)
    # From here on, the people the call names are named by their whole name (spec 6.7).
    arguments = understood
    # Who the call is judged as: the person, or Domo for his own acts and in a turn of his own.
    actor = judged_actor_of(tool, ctx)
    found = await _described_and_decided(tool, tool_name, arguments, ctx, actor)
    if isinstance(found, ActionRecord):
        # Nothing was decided, so nothing ran.
        return await _recorded(ctx, reply_id, found)
    action, decision, plan = found
    # What Domo asked for the next action reaches this one, whatever becomes of it: refused or
    # not done, it is used up, and no other action of the answer inherits it.
    # A silent action (never shown, never judged) never uses it up: it is meant for a later one.
    raised = (
        None
        if action.action_type in CHANGES_NOTHING or is_silent(action.action_type)
        else ctx.take_ask_more()
    )
    if isinstance(decision, Refuse):
        record = _refused(
            tool_name,
            action.action_type,
            decision.reason,
            limit=decision.limit,
            side=decision.side,
        )
        if plan is not None and plan.refused_step is not None:
            # Which step of the chain refused it, from 1.
            refused: JsonObject = {**record.model_result, "step": plan.refused_step}
            record = replace(record, model_result=refused)
        return await _recorded(ctx, reply_id, record, action=action, decision=decision, actor=actor)
    waiting_for = (
        None if plan is not None else await _waits_for_a_yes(tool, tool_name, arguments, ctx)
    )
    if isinstance(waiting_for, ActionRecord):
        return await _recorded(
            ctx, reply_id, waiting_for, action=action, decision=decision, actor=actor
        )
    if waiting_for is not None:
        # A call that carries a yes always waits for it, never judged (a program's stop is the
        # code's, not the judge's to weigh).
        record = await _waits(
            ctx,
            reply_id,
            tool_name,
            arguments,
            action,
            decision,
            waiting_for.asked,
            None,
            None,
            approver_at_least=waiting_for.approver,
            actor=actor,
            domo_asks=waiting_for.domo_asks,
        )
        if record.status == "waiting":
            return record
        return await _recorded(ctx, reply_id, record, action=action, decision=decision, actor=actor)
    judgement: Judgement | None = None
    asked: Asked | None = None
    if needs_the_judge(action.action_type):
        never_ask = decision.never_ask if plan is None else plan.never_ask
        judgement = await consult_the_judge(
            ctx,
            actor=judges_the_words_of(ctx, action, actor),
            action=action,
            says=(
                describe_for_people(action, arguments, REFERENCE)
                if plan is None
                else describe_chain(plan.arguments, REFERENCE)
            ),
            rights=rights_say(decision),
            steps=() if plan is None else [planned_step_told(step) for step in plan.steps],
            owner_rule=owner_rule_of(never_ask),
        )
        if judgement.verdict == "do_not":
            record = refused_by_the_judge(tool_name, action.action_type, judgement.reason)
            return await _recorded(
                ctx,
                reply_id,
                record,
                action=action,
                decision=decision,
                judged=judgement,
                actor=actor,
            )
        asked = what_to_ask(
            judgement,
            rule=decision.rule if plan is None else plan.rule,
            raised=raised,
            never_ask=never_ask,
        )
        if plan is None and isinstance(decision, Allow) and asked is None:
            # About to run on a decision made before the judge: what the barrier says now
            # decides. (A chain's runner reads the person and the barrier again at each step.)
            again = await _decided_again(ctx, tool, tool_name, action)
            if isinstance(again, ActionRecord):
                return await _recorded(
                    ctx, reply_id, again, action=action, judged=judgement, actor=actor
                )
            if isinstance(again, Refuse):
                record = _refused(
                    tool_name,
                    action.action_type,
                    again.reason,
                    limit=again.limit,
                    side=again.side,
                )
                return await _recorded(
                    ctx,
                    reply_id,
                    record,
                    action=action,
                    decision=again,
                    judged=judgement,
                    actor=actor,
                )
            decision = again
            asked = what_to_ask(
                judgement,
                rule=decision.rule,
                raised=raised,
                never_ask=decision.never_ask,
            )
    elif plan is None and is_made(action.action_type) and decision.rule is not None:
        # An owner's rule asks before one of Domo's tools (spec 5.8, final review I2): asked as
        # he says, with no judge; the request waits and is shown like any other.
        asked = Asked(decision.rule, "rule", None)
    if isinstance(decision, Allow) and asked is None:
        if plan is not None and plan.waits > 0:
            # A chain let through that waits for an arrival keeps a request for the upkeep.
            record = ActionRecord(tool_name, action.action_type, "done", None, {}, {})
            return await run_with_a_request(
                ctx, reply_id, record, plan=plan, tool=tool, tool_name=tool_name, judged=judgement
            )
        if action.action_type in JUDGED_WITHIN:
            # What it carries is judged inside it: Domo's raise goes to that judgement (F15),
            # and is used up with this call whatever it does.
            ctx.ask_more = raised
            try:
                record = await execute_allowed(tool, tool_name, action.action_type, arguments, ctx)
            finally:
                ctx.ask_more = None
        else:
            record = await execute_allowed(tool, tool_name, action.action_type, arguments, ctx)
    else:
        whose = None if plan is not None else await _whose_yes(tool, arguments, ctx)
        record = await _waits(
            ctx,
            reply_id,
            tool_name,
            arguments,
            action,
            decision,
            asked,
            plan,
            judgement,
            actor=actor,
            approver_at_least=whose or "self",
            domo_asks=whose is not None,
        )
        if record.status == "waiting":
            return record
    stored = await _recorded(
        ctx, reply_id, record, action=action, decision=decision, judged=judgement, actor=actor
    )
    if stored.action_id is None and isinstance(decision, Allow):
        logger.error(
            "The tool %s was allowed to do %s (it ended %s) but the action could not be "
            "recorded nor audited: check what it did",
            tool_name,
            action.action_type,
            record.status,
        )
    return stored
