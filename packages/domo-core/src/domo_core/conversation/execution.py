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
   means), so that what follows is about the person found (names that cannot be read within
   ``_CHECK_SECONDS`` make it ``failed``, with why, and nothing runs); then the tool describes the
   call (``describe``); arguments it rejects make it ``invalid``, with the tool's reason;
3. the policy barrier decides (``policy.decision.decide_now``: the owner's rights, and the
   facts a limit reads, as they are now, read in a short transaction of its own, then
   ``policy.barrier.decide``; if they cannot be read within ``_CHECK_SECONDS``, or reading them
   fails, the action is ``failed`` and nothing runs): ``Refuse`` makes it ``refused``;
4. for an action that changes something (``catalog.needs_the_judge``), the yes-judge is consulted
   (``consult_the_judge``: its case built from the database, spec 4.2), whatever the barrier said
   but a refusal. ``do_not`` makes it ``refused`` (``refused_by_the_judge``: the judge's reason,
   for Domo to tell in his words), and is a fact noted with the action (``went_wrong``,
   ``judge_refused``, with the reason kept for the journal). Otherwise what is asked first is
   the most demanding of the judge's verdict, the owner's rule and what Domo asks more
   (``judge.what_to_ask``; a judge that does not answer says ``do``: no type asks anything by
   default; but on a fact a judge must have weighed, ``catalog.WARNED_FACTS``, the last key, a
   silent judge asks a plain yes). When the code is not sure of who asks and the barrier says
   so (``Allow.judge_identity``, ``NeedsApproval.judge_identity``: what is sensitive, or ordinary
   too by the owner's word), the judge is consulted whatever the type, with the clues of who
   speaks, and its silence asks a proof, never a silent ``do`` (``consult_the_judge``,
   ``identity_doubted``); the barrier reads the risks a launch's operations declare
   (``DeclaresRisks``). An agreement the barrier leaves to the judge first
   (``NeedsApproval.judge_first``: a limit that says "judge", the catalog's adult for a child, the
   latter only with a line of an adult's own words in the case) is spared by the judge's own
   ``do`` on a single action (``_judged_through``); any other verdict asks for it. When the barrier
   needs nobody else's agreement (or the judge spared it) and nothing is asked, the person and the
   barrier are read again (``_decided_again``: the judge may have taken up to
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
the history the yes-judge reads and every audit entry of the call are his then (the judge's case
still reads the words of the person whose turn it is, when one is: ``consult_the_judge``); a
request that waits is stored in a person's conversation (``confirmations``: a requester is a
person). For an idea of his (his mark, a turn he began, a turn on his own) that the barrier asks
nobody's agreement for, it waits for the person the thing concerns, as the code says
(``policy.subjects.concerned_of``: whom it belongs to, the owner for the house; never the judge,
never the owner by default): here when that is the person of the turn, else in their own
conversation, as news for them (``_waits``); nobody is asked, and the action is refused, when the
household has no owner now. What the barrier asks (an adult for a child, an owner's approver)
still wins.

A call Domo marked as his own idea (``act_on_my_own``: ``ToolContext.take_own_idea``, taken by the
next call that changes something) is judged as his too, the judge told his reason, whose words it
was born of and whether the person lacks the right; it is stored as his, with that reason. The
socle comes first: in a person's turn, what the owner denied that person on purpose
(``RightsTable.denied_on_purpose``) is refused at once, no judge asked. The mark lifts only a right
not given: in a person's turn the barrier decides for them too, that right counted as given, and
the stricter stands (ruling R26: an untrusted speaker, the owner's approver, rule or limit for them
or their role, an adult for a child). It never changes who is asked, a call that cannot be
described does not use it up, and a chain never takes it (R27). A judge silent on an idea of his
is asked once more; still silent, nothing is done and nobody is asked (``judge_silent``). In a
turn Domo started himself with a reason (``ToolContext.turn_reason``: not a plan's turn whose
plans all keep the person's words, which is their request), every judged call is such an idea. A
judge silent on a call of a turn that answers the person but was told words from outside is asked
once more too, before its fallback asks them a yes (audit CNV-2).

A call that carries the yes of a stop (a tool of Domo's called with ``yes_for``, a tool that
``AsksItsYes``) is never judged: once the barrier let it through, it waits for the stop's approver
(or the barrier's, when that one is more demanding), asking what the stop asks, who asked it (Domo
for a cost, a rule, an approver) and why (what the operation says), whatever the judge would say;
it is stored and shown as any request that waits. But the person's own plain yes for a cost
(``WaitsForAYes.judge_first``), let through with no rule of the owner's, is weighed by the judge
first, with what it costs (spec part 12, ``_their_cost_weighed``): a usual one runs, and the
assistant tells them after; a judge that does not answer asks them.

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
for the upkeep to resume it. A chain let through that meets a step needing someone's agreement
stores the rest as a request for that person, its steps done kept done (``tools.chain``, JDG-9),
instead of stopping dead.

The model reads the result as JSON (``{"status": "done", ...}``, ``{"status":
"waiting_for_confirmation", "by": "you" | "an adult" | "the owner", "answer_with": ..., "asked_by":
..., "why": ...}`` (``"by": "the person it concerns"`` with ``"who"``, their name, and no
``answer_with``, when it waits in someone else's conversation), where ``answer_with`` says how the
yes can be given (``confirmations.store.how_to_answer``), ``asked_by`` who asked it and ``why`` why,
when there is a reason, ``{"status": "refused", "reason": ...}``, with ``"limit"``, the summary of
the limit that refused it, when one did, or ``"why"``, the judge's reason, ``{"status": "invalid",
"reason": ...}``); it never holds a link, a token or a secret. The people see a card under the reply
(``channels.page.cards.action_card``, with its sentence from ``conversation.cards``): the chat
streams it, and its page shows the stored ones. A part of a card that may only be shown once
(``card_once``) is in the answer that executed the action and nowhere else. When someone else said
yes, that is for the one who asked to ask for, with a button on their card; the card the tool stores
holds only the invitation's identifier, or the provider's, never the link. A step of a chain always
leaves it so, and the chain's card has one button per such step, numbered (spec 4.5).
"""

import json
import logging
import math
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Final, Literal, Protocol, cast, runtime_checkable

import anyio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.actors import actor_of
from domo_core.accounts.models import Person
from domo_core.accounts.sessions import is_active
from domo_core.chat.agent import ActionRecord
from domo_core.chat.store import ConversationStore
from domo_core.confirmations.case import (
    CASE_STEPS,
    Gathered,
    IdeaTold,
    gather,
    history_of,
    known_of_a_step,
    outside_words,
)
from domo_core.confirmations.describe import describe_chain, describe_for_people
from domo_core.confirmations.judge import (
    Judgement,
    RequestTold,
    RightsSay,
    StepTold,
    audit_judgement,
    build_case,
    fallback,
    judge,
    owner_rule_of,
    planned_step_told,
    what_to_ask,
)
from domo_core.confirmations.steps import PlannedStep, add_steps
from domo_core.confirmations.store import (
    create_confirmation,
    for_the_person_concerned,
    how_to_answer,
    judgement_told_to,
    let_through,
)
from domo_core.conversation.cards import storable
from domo_core.conversation.visits import news_for
from domo_core.household.service import names_among, whole_names
from domo_core.learning.models import Kind
from domo_core.learning.record import note_went_wrong, reason_code
from domo_core.learning.undone import note_undoing
from domo_core.llm.choices import load_choices
from domo_core.llm.types import JsonObject, ToolCall
from domo_core.policy.actions import DescribedAction
from domo_core.policy.asks import Ask, Asked, NeverAsk, most_demanding, most_demanding_asked
from domo_core.policy.barrier import (
    Allow,
    Decision,
    NeedsApproval,
    Refuse,
    given_for_an_idea,
    may_act_weakly,
    record_decision,
    stricter,
    writes_for_domo,
)
from domo_core.policy.catalog import (
    CHANGES_NOTHING,
    JUDGED_WITHIN,
    PROGRAM_RUN,
    SILENT_YET_HIS_IDEA,
    WARNED_FACTS,
    Risks,
    is_made,
    is_silent,
    needs_the_judge,
    warned_since,
)
from domo_core.policy.decision import ChainBasis, decide_now, doubted
from domo_core.policy.rights import Approver, load_rights
from domo_core.policy.roles import DOMO_SUBJECT_ID, Actor
from domo_core.policy.settings import load_policy_settings
from domo_core.policy.subjects import concerned_of, keys_of, owner_of_now, subject_of
from domo_core.texts.view import REFERENCE

# From ``tools.context``, not ``tools.registry``: the registry holds the tools that manage
# requests, which run through this module (``confirmations.service``); and the chain of actions,
# which runs its steps through this module too (``confirmations.chain``): its plan is only a type
# here.
from domo_core.tools.context import (
    MAX_IDEA_REASON_LENGTH,
    ActsAsDomo,
    AsksItsYes,
    AsksWhomItsYes,
    BoundsItsOwnSteps,
    InternalTool,
    InvalidArguments,
    NamesPeople,
    NothingDoneInTime,
    OutsideKind,
    OwnIdea,
    PlansSteps,
    ToolContext,
    ToolOutcome,
    WaitsForAYes,
    brings_outside_text,
    idea_of,
    judged_actor_of,
)

if TYPE_CHECKING:
    from domo_core.tools.chain import ChainPlan

logger = logging.getLogger(__name__)


@runtime_checkable
class DeclaresRisks(Protocol):
    """A tool whose launch says every risk it may reach (a tool Domo made,
    ``madetools.offered``, ``launch.reach_of``): the barrier reads them to tell whether its launch
    is ordinary when the code is not sure of who asks (``barrier.may_act_weakly``). ``None`` when
    it may go beyond what it declares, or that cannot be known."""

    async def reach(self) -> Risks | None: ...


@runtime_checkable
class TellsRiskyActs(Protocol):
    """A tool some of whose operations act in the house without being declared harmless (a tool
    Domo made, ``madetools.offered``): what each says (his own words when he built it) is told to
    the judge who weighs a call of it asked by someone the code is not sure of."""

    def risky_acts(self) -> tuple[str, ...]: ...


# How the judge is told what a launch may do in the house that was not declared harmless.
RISKY_ACTS_TOLD: Final = (
    "It acts in the house in a way not declared harmless (its own words, data): "
)


def _with_risky_acts(says: str, tool: InternalTool) -> str:
    """``says``, followed by what the risky acts in the house of ``tool`` say, when it has any."""
    if not isinstance(tool, TellsRiskyActs) or not (acts := tool.risky_acts()):
        return says
    return f"{says} {RISKY_ACTS_TOLD}" + " | ".join(acts)


async def _risks_of(tool: InternalTool, actor: Actor) -> Risks | None:
    """What a call of ``tool`` may reach (``DeclaresRisks``), read only when the code is not sure
    of who asks (``actor``): someone it is sure of needs no such reading."""
    if actor.trusted or not isinstance(tool, DeclaresRisks):
        return None
    try:
        return await tool.reach()
    except Exception as error:
        # Not known: sensitive.
        logger.warning("What a tool may reach could not be read (%s)", type(error).__name__)
        return None


# How long a tool may take to describe a call, and then to run it (see ``execute_allowed``).
_TOOL_SECONDS: Final = 60.0
# How long reading the person again before a call may take (see ``read_the_person_again``), and
# reading the rights for the barrier's decision (see ``_decide_now``).
_CHECK_SECONDS: Final = 15.0
# How long gathering what the judge may read may take (see ``_gathered``).
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


# Why nothing was decided, and so nothing done: what the rules say could not be read in time
# (``took_too_long``), or could not be read at all (``not_read``). Codes, never a library's text.
Undecided = Literal["took_too_long", "not_read"]
_UNDECIDED_DETAIL: Final[Mapping[Undecided, str]] = {
    "took_too_long": (
        "nothing was done: checking what is allowed took too long; you may try again"
    ),
    "not_read": "nothing was done: what is allowed could not be checked; you may try again",
}


# What could not be done in time, or at all, before anything ran: what is allowed (``rights``),
# who the call speaks of (``names``, ``_with_whole_names``), or what the tool does first,
# describing or planning (``before``, ``NothingDoneInTime``).
Unread = Literal["rights", "names", "before"]
_UNREAD_DETAILS: Final[Mapping[Unread, Mapping[Undecided, str]]] = {
    "rights": _UNDECIDED_DETAIL,
    "names": {
        "took_too_long": (
            "nothing was done: finding who the call speaks of took too long; you may try again"
        ),
        "not_read": (
            "nothing was done: who the call speaks of could not be read; you may try again"
        ),
    },
    "before": {
        "took_too_long": (
            "nothing was done: getting the action ready took too long; you may try again"
        ),
        "not_read": "nothing was done: the action could not be got ready; you may try again",
    },
}


def undecided_record(
    tool_name: str, action_type: str | None, why: Undecided, *, what: Unread = "rights"
) -> ActionRecord:
    """An action that never ran because ``what`` could not be done in time or at all (``why``):
    ``failed``, and the model is told plainly what it was, that nothing was done and that it may
    try again."""
    detail = _UNREAD_DETAILS[what][why]
    result: JsonObject = {"status": "failed", "reason": why, "detail": detail}
    card: JsonObject = {"key": "action.card.failed", "params": {}}
    return ActionRecord(tool_name, action_type, "failed", None, result, card)


def timed_out_outcome() -> ToolOutcome:
    """What a tool tells of a call it did not finish in time: nobody knows whether it happened."""
    result: JsonObject = {
        "reason": "timeout",
        "outcome": "unknown",
        "detail": (
            "the action took too long and was given up on, but it may have happened anyway: "
            "do not try it again before checking, and tell the person it is not known"
        ),
    }
    card: JsonObject = {"key": "action.card.failed.timeout", "params": {}, "reason": "timeout"}
    return ToolOutcome("failed", result, card)


def _timed_out(tool_name: str, action_type: str) -> ActionRecord:
    """An action whose tool did not answer in time: nobody knows whether it happened."""
    told = timed_out_outcome()
    result: JsonObject = {"status": "failed", **told.model_result}
    return ActionRecord(tool_name, action_type, "failed", None, result, told.card)


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
        except NothingDoneInTime:
            # Describing or planning took too long, which changes nothing.
            logger.error("The tool %s took too long before doing anything", tool_name)
            return undecided_record(tool_name, action_type, "took_too_long", what="before")
        except TimeoutError:
            # One of its own steps ran out of time (``BoundsItsOwnSteps``, a chain's record of a
            # step, a machine that stalls): whether it happened is not known, as when the whole
            # call takes too long.
            logger.error(
                "A step of the tool %s took too long: whether %s happened is unknown",
                tool_name,
                action_type,
            )
            return _timed_out(tool_name, action_type)
        except Exception as error:
            # Only the kind of error: its message may hold what the arguments held.
            logger.error("The tool %s failed (%s)", tool_name, type(error).__name__)
            return failed_record(tool_name, action_type)
    if outcome is None:
        logger.error(
            "The tool %s took too long: whether %s happened is unknown", tool_name, action_type
        )
        return _timed_out(tool_name, action_type)
    if brings_outside_text(tool.action_type):
        # What a tool he made, or a program run by hand, gave back is text nobody of the house
        # said: from here the turn is marked (``ToolContext.outside_text``: no longer his own
        # answer, and what he keeps in it is not surely the person's words), on this context and
        # on every copy of it (a request decided in the turn runs on one); the judge is told which
        # kind (``ToolContext.outside_kinds``) and which program, when its result holds its own
        # words (``ToolOutcome.source``: else, "not known which").
        kind: OutsideKind = "program" if tool.action_type == PROGRAM_RUN else "tool"
        ctx.raise_outside_text(kind, outcome.source)
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


def _cut(idea_reason: str | None) -> str | None:
    """Domo's reason for an idea of his, as it is kept: at most ``MAX_IDEA_REASON_LENGTH``
    characters; ``None`` when there is none."""
    return idea_reason[:MAX_IDEA_REASON_LENGTH] if idea_reason else None


def _takes_the_mark(tool: InternalTool) -> bool:
    """Whether a call of ``tool`` takes what Domo marked as his own idea
    (``ToolContext.take_own_idea``): one that changes something and is shown, and a silent one
    listed among those an idea of his reaches (``catalog.SILENT_YET_HIS_IDEA``); never a chain
    (``PlansSteps``, ruling R27: its steps run with the person's rights, and the mark waits for
    the next action that is no chain)."""
    if isinstance(tool, PlansSteps):
        return False
    action_type = tool.action_type
    if action_type in SILENT_YET_HIS_IDEA:
        return True
    return action_type not in CHANGES_NOTHING and not is_silent(action_type)


def _subject_key(
    ctx: ToolContext, record: ActionRecord, action: DescribedAction | None
) -> str | None:
    """What the action is about (``policy.subjects``, the one place that builds it): read from the
    result of the tool once it ran, and from the facts before; ``None`` for an action nothing
    described (a refused name, arguments that were rejected)."""
    if action is None:
        return None
    result = record.model_result if record.status == "done" else None
    return subject_of(action, result, ctx.person.id).key


@dataclass(frozen=True)
class _Noted:
    """A fact of what went wrong with a call (``learning.record.note_went_wrong``), noted in the
    transaction that records the action: its ``kind``, who noted it (``by``), whether the act was
    the assistant's idea (``of_domo``) and why (``reason``: what may be said to the person it is
    about, never the journal's own line, since a turn of theirs shows it)."""

    kind: Kind
    by: Literal["domo", "code"]
    of_domo: bool
    reason: str | None


def _failure(record: ActionRecord, *, of_domo: bool) -> _Noted | None:
    """The fact of an action that ``failed`` (``None`` for any other): by the code, the
    assistant's own when the act was his (``of_domo``), with the reason of its result only when
    it is a code (``learning.record.reason_code``). ``None`` too for the record of a request that
    ran and failed (``confirmation_id``: a decision, a chain continued or let through): its fact
    was noted when the request was settled, once."""
    if record.status != "failed" or record.confirmation_id is not None:
        return None
    return _Noted("failed", "code", of_domo, reason_code(record.model_result))


def _may_undo_his(
    ctx: ToolContext, record: ActionRecord, judged_as: Actor, idea_reason: str | None
) -> bool:
    """Whether the action of ``record`` may undo what the assistant did or decided himself: it
    ended ``done``, in a turn that answers the person (``ToolContext.answers_them``), and it is
    the person's, no idea of his (judged as them, no reason of his, no turn he began or acts in on
    his own). An action that failed or waits undoes nothing."""
    return (
        record.status == "done"
        and ctx.answers_them
        and judged_as.subject != "domo"
        and idea_reason is None
        and ctx.turn_reason is None
        and not ctx.on_his_own
    )


@dataclass(frozen=True)
class StoredAction:
    """An action ``store_action`` stored: its ``id`` (``None`` when there is no reply to store it
    under: a review, whose audit and facts are written all the same), and the line that tells the
    assistant it undid what he had done or decided himself (``undoes``, ``None`` when it did
    not)."""

    id: uuid.UUID | None
    undoes: str | None = None


# Why nothing was asked when nobody can answer for the thing (the household has no owner now).
_NOBODY_TO_ASK: Final = "nobody could answer for it: the household has no owner now"


async def store_action(
    ctx: ToolContext,
    reply_id: uuid.UUID | None,
    record: ActionRecord,
    *,
    action: DescribedAction | None = None,
    decision: Decision | None = None,
    judged: Judgement | None = None,
    actor: Actor | None = None,
    idea_reason: str | None = None,
    noted: _Noted | None = None,
) -> StoredAction | None:
    """Store the action with the reply and, when the barrier decided, audit the judge's verdict
    when it was asked (``judged``) then the decision: last, just before the commit (the audit log
    holds a lock until the transaction ends), both under ``actor``, who the call was judged as
    (``judged_actor_of``; when not given, no call was judged: Domo in a turn on his own, else
    the person), and the action is marked as his when it is (``MessageAction.by_domo``: it is
    then in his history, never the person's), with his reason when it was his own idea
    (``idea_reason``, cut to ``MAX_IDEA_REASON``). The card is stored, never ``card_once``.
    ``noted`` is a fact of what went wrong (a refusal of the judge's, nobody to ask), noted with
    the action, about the person of the turn; an action that ``failed``, shown or silent, is one
    by itself (``_failure``, by the code, its reason only when it is a code), but for the record
    of a request that ran and failed (it names it: ``confirmation_id``), whose fact was noted when
    it was settled (``confirmations.service.settle``). The judge's verdict is audited with the id
    of the action stored, which ties them.

    The one place that knows an action undid what the assistant did or decided himself (spec
    9.2): a person's action ``done`` in their own turn (``_may_undo_his``) that takes back, on the
    same thing (``policy.subjects.keys_of``: its keys and aliases), what he did or his judge
    let through (``learning.undone.note_undoing``: guarded, it never loses the record) leaves its
    facts, with the person's own words of the turn as why (``ToolContext.said``), and what is
    returned holds the line he is told (``StoredAction.undoes``: the day and the type, nothing of
    the conversation it was done in). Returns the action stored, ``None`` when it was not.

    Without a reply (``reply_id`` ``None``: a review, which nobody reads) no action row is stored
    and nothing is shown: its fact, the judge's verdict and the decision are written all the same,
    in the name of who it was judged as.

    It is one attempt, shielded from cancellation (a reader who leaves must not stop the record
    of what was done) and limited to ``_STORE_SECONDS``. A failure is logged (its kind only) and
    the turn goes on: what came of the action is still told to the model and shown."""
    judged_as = ctx.turn_subject if actor is None else actor
    if noted is None:
        noted = _failure(record, of_domo=judged_as.subject == "domo")
    with anyio.move_on_after(_STORE_SECONDS, shield=True):
        try:
            async with ctx.session_factory() as session:
                told: str | None = None
                if action is not None and _may_undo_his(ctx, record, judged_as, idea_reason):
                    # Read before this action is stored: it may undo its own type (a role).
                    told = await note_undoing(
                        session,
                        action_type=action.action_type,
                        keys=keys_of(action, record.model_result, ctx.person.id),
                        by=ctx.person.id,
                        now=ctx.clock(),
                        reason=ctx.said_in_turn,
                    )
                subject_key = _subject_key(ctx, record, action)
                stored_id: uuid.UUID | None = None
                if reply_id is not None:
                    stored = await ConversationStore(session).add_action(
                        reply_id,
                        tool_name=record.tool_name,
                        action_type=record.action_type or "",
                        status=record.status,
                        summary=record.card,
                        confirmation_id=record.confirmation_id,
                        by_domo=judged_as.subject == "domo",
                        subject_key=subject_key,
                        idea_reason=_cut(idea_reason),
                    )
                    stored_id = stored.id
                if noted is not None:
                    await note_went_wrong(
                        session,
                        kind=noted.kind,
                        action_type=record.action_type or "",
                        now=ctx.clock(),
                        subject_key=subject_key,
                        action_id=stored_id,
                        by=noted.by,
                        of_domo=noted.of_domo,
                        about=ctx.person.id,
                        reason=noted.reason,
                    )
                if action is not None and judged is not None:
                    await audit_judgement(
                        session,
                        actor=judged_as,
                        tool_name=record.tool_name,
                        action=action,
                        judgement=judged,
                        action_id=stored_id,
                    )
                if action is not None and decision is not None:
                    await record_decision(
                        session, judged_as, action, decision, tool_name=record.tool_name
                    )
                await session.commit()
            return StoredAction(stored_id, told)
        except Exception as error:
            logger.error("An action could not be recorded (%s)", type(error).__name__)
            return None
    logger.error("Recording an action took too long")
    return None


async def _recorded(
    ctx: ToolContext,
    reply_id: uuid.UUID | None,
    record: ActionRecord,
    *,
    action: DescribedAction | None = None,
    decision: Decision | None = None,
    judged: Judgement | None = None,
    actor: Actor | None = None,
    idea_reason: str | None = None,
    noted: _Noted | None = None,
) -> ActionRecord:
    """``store_action``, and the record with the id of the action stored (``action_id``), when it
    was; when it undid what the assistant did or decided himself, what the model reads of it says
    so (``"undoes"``), never its card."""
    stored = await store_action(
        ctx,
        reply_id,
        record,
        action=action,
        decision=decision,
        judged=judged,
        actor=actor,
        idea_reason=idea_reason,
        noted=noted,
    )
    if stored is None:
        return record
    if stored.undoes is not None:
        record = replace(record, model_result={**record.model_result, "undoes": stored.undoes})
    return replace(record, action_id=stored.id)


def _as_news(ctx: ToolContext, reply_id: uuid.UUID | None, concerned: "_Concerned | None") -> bool:
    """Whether a request that waits for the person it concerns is news for them, in their own
    conversation, rather than shown under this reply: it concerns someone else than the person of
    the turn, or there is no reply to show it under (a review, ``reply_id`` ``None``: the person
    reviewed is told it as news too)."""
    return concerned is not None and (concerned.id != ctx.person.id or reply_id is None)


async def _store_waiting(
    ctx: ToolContext,
    reply_id: uuid.UUID | None,
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
    idea_reason: str | None = None,
    marked: bool = False,
    concerned: "_Concerned | None" = None,
    in_their_turn: bool = False,
) -> ActionRecord | None:
    """Store the request that waits for a yes, the waiting action that shows it, and the audit
    (the judge's verdict when it was asked, ``judged``, then the barrier's ``decision``, under
    ``actor``, who the call was judged as: ``store_action``), last, in one transaction. The
    person of the turn (``ctx.actor``) is always its requester: whoever answers it, it was asked
    in their turn, and runs there. One made in a turn on his own is Domo's
    (``Confirmation.by_domo``), judged again and run as his, on his own, and so is one a call of
    his says is his own (``domo_asks``: ``WaitsForAYes.domo_asks``); one he marked as his own idea
    (``marked``, ``act_on_my_own``) in a person's turn is his too, and the only one that keeps his
    reason (``idea_reason``: ``store.made_in_a_turn``), judged again and run as his in that turn.
    Anything else, a call of a turn he began included, stays the person's, wherever it waits
    (ruling R31). Returns the record with its ``confirmation_id`` and, in what the model reads,
    how the yes can be given (``answer_with``, from what the request asks: ``how_to_answer``); or
    ``None`` when nothing could be stored (then no request exists).

    The request waits for ``approver`` (``self``: the person's own yes), asks what ``asked`` says,
    with the question a limit asked (``question``). A chain's ``steps`` are stored with its
    request, and ``chain_basis``, what it remembers of the one who asked.

    ``concerned`` is the person the thing belongs to, who alone answers (``concerned_id``): the
    person of this conversation, or someone else. Then the request waits in their active
    conversation, theirs to answer (it was made in this reply, ``message_id``, which says where
    it runs: ``store.conversation_made_in``; no reply of theirs shows it yet), without the judge's
    reason, which it wrote from the case of the person of the turn (ruling R29: ``why`` dropped;
    the fact that the judge warned is kept, ruling R69, with the code's own line that it cannot be
    undone, R72: ``store.for_the_person_concerned``); it is news for them, in the same
    transaction, and the core is told (``ctx.notify``); nothing of it is shown here but a line
    that it waits for them, by name. So it does too, for the person of this conversation, when
    there is no reply to show it under (``reply_id`` ``None``: a review, ``_as_news``).

    Shielded and limited like ``store_action``."""
    judged_as = ctx.turn_subject if actor is None else actor
    elsewhere = concerned is not None and concerned.id != ctx.person.id
    as_news = _as_news(ctx, reply_id, concerned)
    with anyio.move_on_after(_STORE_SECONDS, shield=True):
        try:
            async with ctx.session_factory() as session:
                conversation_id = ctx.conversation_id
                if concerned is not None and as_news:
                    found = await ConversationStore(session).active_conversation(concerned.id)
                    conversation_id = found.id
                requester = ctx.actor
                # Born in a review and waiting for someone else: the request is the owner's to
                # answer and to run, in his conversation, as one of his own review's (the owner
                # its requester), never the reviewed person's, who may have left by the time the
                # yes comes.
                his_from_a_review = ctx.in_review and elsewhere
                if his_from_a_review:
                    owner = await session.get(Person, await owner_of_now(session, ctx.clock()))
                    assert owner is not None
                    requester = actor_of(owner)
                confirmation = await create_confirmation(
                    session,
                    requester=requester,
                    tool_name=record.tool_name,
                    arguments=arguments,
                    action=action,
                    approver=approver,
                    asked=for_the_person_concerned(asked) if elsewhere else asked,
                    conversation_id=conversation_id,
                    message_id=reply_id,
                    now=ctx.clock(),
                    question=question,
                    chain_basis=chain_basis,
                    by_domo=ctx.on_his_own or domo_asks or marked,
                    # His request, shown to its approver in the approver's own conversation: this
                    # reply shows it to them, and their next words may answer it (R17).
                    shown_to_approver=reply_id if domo_asks and not elsewhere else None,
                    # The ``allow`` written below in his name counts it in his history already.
                    counted_for_domo=isinstance(decision, Allow)
                    and writes_for_domo(judged_as, action),
                    subject_key=subject_of(action, None, ctx.person.id).key,
                    # Only his mark in a person's turn keeps it (``store.made_in_a_turn``).
                    idea_reason=_cut(idea_reason) if marked and not ctx.on_his_own else None,
                    concerned_id=None if concerned is None else concerned.id,
                    # Born of the owner's rule on his act in a person's turn: replayed there.
                    in_their_turn=in_their_turn and not ctx.on_his_own,
                )
                if as_news:
                    # No reply of theirs showed it: their words answer it once one does.
                    confirmation.last_shown_message_id = None
                await add_steps(session, confirmation.id, steps)
                waiting = (
                    record
                    if as_news
                    else replace(
                        record,
                        confirmation_id=confirmation.id,
                        model_result={
                            **record.model_result,
                            "answer_with": how_to_answer(confirmation),
                        },
                    )
                )
                if reply_id is not None:
                    await ConversationStore(session).add_action(
                        reply_id,
                        tool_name=waiting.tool_name,
                        action_type=waiting.action_type or "",
                        status=waiting.status,
                        summary=waiting.card,
                        confirmation_id=waiting.confirmation_id,
                        by_domo=judged_as.subject == "domo",
                        subject_key=confirmation.subject_key,
                        idea_reason=_cut(idea_reason),
                    )
                if concerned is not None and as_news:
                    await news_for(session, concerned.id, now=ctx.clock())
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
            if concerned is not None and as_news and ctx.notify is not None:
                # News for them: a turn of theirs may be due now (the request is stored already,
                # whatever this does).
                try:
                    ctx.notify(concerned.id)
                except Exception as error:
                    logger.error("The core was not told of news (%s)", type(error).__name__)
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

# What the model is told when an owner's directive on people applies (``owner_directive``): the
# owner ruled on it and the code applied it; no right and no judgement of the assistant's changes
# that. In words he can say, never the word of the reason.
OWNER_DIRECTIVE_DETAIL: Final = (
    "the owner ruled on this and nothing may go against it, so nothing was done: tell the "
    "person so in your own words, never with the word of the reason, and never hint at a way "
    "around it"
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
    elif reason == "owner_directive":
        model_result["detail"] = OWNER_DIRECTIVE_DETAIL
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


def _refused_as(ctx: ToolContext, decision: Refuse, actor: Actor) -> Actor:
    """Who a refusal is audited under: the person of the turn when it came from their side of an
    idea of Domo's judged both ways (``Refuse.side``: untrusted, a limit, a deny of the owner's for
    them), as a deny on purpose is; who the call was judged as (``actor``) otherwise."""
    return ctx.actor if decision.side == "person" else actor


# What the model is told when the judge refused an action (spec 4.6): the person may explain.
JUDGE_REFUSED_DETAIL: Final = (
    "The assistant judged not to do it: tell the person why, in your own words; if they explain "
    "or insist, call it again with their new words: your judge weighs them, never you alone"
)


def refused_by_the_judge(tool_name: str, action_type: str, said: str) -> ActionRecord:
    """An action the judge refused (``do_not``), with the reason it said for the person
    (``Judgement.reason``, never the one kept for the journal, ``Judgement.noted``): Domo tells it
    in his words, and the card says it (``action.card.refused.judge``)."""
    return ActionRecord(
        tool_name=tool_name,
        action_type=action_type,
        status="refused",
        confirmation_id=None,
        model_result={
            "status": "refused",
            "reason": "judge",
            "why": said,
            "detail": JUDGE_REFUSED_DETAIL,
        },
        card={"key": "action.card.refused.judge", "params": {"reason": said}, "reason": "judge"},
    )


# What the model is told when its judge stayed silent twice on an idea of his own (spec part 7).
JUDGE_SILENT_WHY: Final = "your judge did not answer; nothing was done, you may come back to it"


def judge_silent(tool_name: str, action_type: str) -> ActionRecord:
    """An idea of Domo's that his judge did not weigh, asked twice: nothing is done, nothing is
    asked of anyone, and he may come back to it."""
    return ActionRecord(
        tool_name=tool_name,
        action_type=action_type,
        status="refused",
        confirmation_id=None,
        model_result={"status": "not_done", "reason": "judge_silent", "why": JUDGE_SILENT_WHY},
        card={
            "key": "action.card.refused.judge_silent",
            "params": {},
            "reason": "judge_silent",
        },
    )


# What the model is told when only the judge's verdict could let a request go and it did not answer.
NOT_WEIGHED_WHY: Final = (
    "it could not be weighed now: nothing was done and nothing is asked; they may ask again later"
)


def not_weighed(tool_name: str, action_type: str) -> ActionRecord:
    """A request only the judge's own verdict lets go (``DescribedAction.verdict_needed``, in a
    chain any step's) that its judge did not weigh: nothing is done, no proof or yes of whoever
    asks stands in for the verdict, and they may ask again later."""
    return ActionRecord(
        tool_name=tool_name,
        action_type=action_type,
        status="refused",
        confirmation_id=None,
        model_result={"status": "not_done", "reason": "not_weighed", "why": NOT_WEIGHED_WHY},
        card={
            "key": "action.card.refused.not_weighed",
            "params": {},
            "reason": "not_weighed",
        },
    )


def _a_verdict_needed(action: DescribedAction, plan: "ChainPlan | None") -> bool:
    """Whether only the judge's verdict lets ``action`` go: itself, or any step of its chain."""
    if action.verdict_needed:
        return True
    return plan is not None and any(
        step.action is not None and step.action.verdict_needed for step in plan.steps
    )


def rights_say(decision: Allow | NeedsApproval, *, judged_first: bool = False) -> RightsSay:
    """What the barrier said, as the judge reads it: allowed, and whose agreement is needed
    first, if anyone's, with the question a limit asked, and whether that agreement is left to
    the judge first (``NeedsApproval.judge_first``): only where its ``do`` is acted on
    (``judged_first``: a single action judged when it is asked for, ``handle_tool_call``), never
    for a request changed, taken up again or a chain, which wait for the agreement as before."""
    if isinstance(decision, NeedsApproval):
        return RightsSay(
            allowed=True,
            needs=decision.approver,
            question=decision.question,
            judge_decides=decision.judge_first and judged_first,
        )
    return RightsSay(allowed=True, needs=None, question=None)


def _judged_through(
    decision: Decision, judgement: Judgement, plan: "ChainPlan | None", asked: Asked | None
) -> bool:
    """Whether the judge's verdict spares the agreement the barrier asked (``NeedsApproval``
    with ``judge_first``: a limit that says "judge", an adult's agreement for a child that only
    the catalog asks for): a single action (a chain's step asks for it as it says), a ``do`` of
    the judge's own model (never its fallback) on a case that left it the decision
    (``Judgement.decided``), and nothing else asked first (an owner's rule still asks, and the
    agreement with it)."""
    return (
        plan is None
        and isinstance(decision, NeedsApproval)
        and decision.judge_first
        and judgement.fallback is None
        and judgement.verdict == "do"
        and judgement.decided
        and asked is None
    )


async def _described_again(
    tool: InternalTool, arguments: JsonObject, actor: Actor, ctx: ToolContext
) -> DescribedAction | None:
    """The call described again by its tool, as it is now, within ``_TOOL_SECONDS``; ``None``
    when it cannot be (the tool's own run then says why)."""
    with anyio.move_on_after(_TOOL_SECONDS):
        try:
            return await tool.describe(arguments, actor, ctx)
        except Exception as error:
            logger.warning("A call could not be described again (%s)", type(error).__name__)
            return None
    return None


def _silence_warns(action: DescribedAction, plan: "ChainPlan | None") -> bool:
    """Whether the action, or a step of the chain, carries a fact a judge must have weighed
    before it runs (``catalog.WARNED_FACTS``: removing the last key, say): a judge that did not
    answer then asks a plain yes, never lets it run at once."""
    actions = [action] if plan is None else [step.action for step in plan.steps]
    return any(
        one is not None
        and any(one.facts.get(fact) is True for fact in WARNED_FACTS.get(one.action_type, ()))
        for one in actions
    )


def warned_first(
    judgement: Judgement,
    action: DescribedAction,
    plan: "ChainPlan | None",
    never_ask: NeverAsk | None,
) -> bool:
    """Whether a plain yes is asked though the judge asked nothing, because the action (or a step
    of the chain) carries a fact a judge must weigh first (``_silence_warns``: the last key): the
    judge did not answer (its fallback), or it let it go under the owner's "never ask me" with
    conditions (it may have read his rule as "ask nothing"). Only his "never ask me" outright
    (``at_all``) lets it run then. Read for a call, a change and a chain alike."""
    if (never_ask is not None and never_ask.at_all) or not _silence_warns(action, plan):
        return False
    return judgement.fallback is not None or (judgement.verdict == "do" and never_ask is not None)


def _answers_with_outside_words(ctx: ToolContext) -> bool:
    """Whether the turn of ``ctx`` answers a message of the person and carries text that is
    neither theirs nor Domo's (``ToolContext.outside_text``: words told to it, a tool's or a
    program's output): a judge silent on one of its calls is asked once more before the fallback
    asks them a yes (audit CNV-2)."""
    return ctx.reply_message_id is not None and ctx.outside_text


def _words_of_plans(ctx: ToolContext) -> tuple[str, ...] | None:
    """What the judge reads as the person's words in a turn a plan woke Domo for
    (``ToolContext.due_plans``): the exact words the plans due keep, checked when they were set
    (none for a plan he made alone); ``None`` in any other turn (their last messages are read)."""
    if not ctx.due_plans:
        return None
    return tuple(plan.their_words for plan in ctx.due_plans if plan.their_words)


# What the case holds when nothing of what the assistant knows could be read.
_NOTHING_GATHERED: Final = Gathered((), (), (), (), None, (), None, ("everything",))
# How long putting back the gathering's own session may take, once it is over or cut.
_PUT_BACK_SECONDS: Final = 2.0


def _slice_of_the_time(parts: int) -> float:
    """An equal part of the time left to the caller's deadline (``_REMEMBERED_SECONDS`` when it set
    none), for each of ``parts`` still to read."""
    left = anyio.current_effective_deadline() - anyio.current_time()
    return (_REMEMBERED_SECONDS if math.isinf(left) else max(left, 0.0)) / parts


async def _gather_in(
    session: AsyncSession,
    ctx: ToolContext,
    *,
    actor: Actor,
    asks: Person | None,
    action: DescribedAction,
    says: str,
    idea: OwnIdea | None,
    planned: Sequence[PlannedStep],
    told: list[StepTold],
) -> tuple[Gathered, str | None, uuid.UUID | None]:
    """What the assistant knows that bears on ``action`` (``case.gather``), read on ``session``:
    about the person who asks (``asks``, ``None`` when nobody does), with the private memories of
    the person of the turn when the case is theirs, and, when it is his own (nobody asks, or an
    idea of his), of the person concerned alone (``concerned_of``; nobody's, and ``concerned``
    named missing, when the household has no owner now); the display name of whose words the
    idea was born of; and whose case it is (whose private memories it read: ``None`` when
    nobody's). The idea's reason is one more thing
    the search looks for, never what decides whose memories enter.

    The steps of a chain (``planned``, ``told`` in the same order, replaced here one by one) are
    read the same way, each about its own action and thing and with the same privacy rules, for
    the first ``CASE_STEPS`` of them that are not checked in their turn: the time left is shared
    among the chain and those steps, and a step that cannot be read in it stays as it was."""
    settings = await load_policy_settings(session)
    now = ctx.clock()
    start = (await load_choices(session, None)).trust_start
    his_own = asks is None or idea is not None
    # Whose case the action's own is (the steps of a chain are read for theirs, never told).
    whose: list[uuid.UUID | None] = []

    async def read(
        of: DescribedAction, in_words: str, extra: str | None, *, with_trust: bool = True
    ) -> Gathered:
        subject = subject_of(of, None, ctx.person.id)
        private_of = ctx.person.id
        missing: tuple[str, ...] = ()
        if his_own:
            # His own case: only the private memories of the one who answers for the thing; none
            # when nobody is known to (no owner now), whose conversation it is or not.
            try:
                private_of = await concerned_of(session, subject, of, now)
            except LookupError:
                private_of = DOMO_SUBJECT_ID
                missing = ("concerned",)
        whose.append(None if private_of == DOMO_SUBJECT_ID else private_of)
        gathered = await gather(
            session,
            about=None if asks is None else asks.id,
            private_of=private_of,
            action=of,
            subject=subject,
            says=in_words,
            extra_query=extra,
            embedder=ctx.embedder,
            zone=settings.time_zone,
            now=now,
            start_trust=start,
            with_trust=with_trust,
            his_own=his_own,
        )
        return replace(gathered, missing=(*missing, *gathered.missing))

    born_of: str | None = None
    if idea is not None and idea.born_of is not None:
        born_of = await session.scalar(select(Person.display_name).where(Person.id == idea.born_of))
    to_read = [index for index, step in enumerate(planned[:CASE_STEPS]) if step.action is not None]
    gathered = _NOTHING_GATHERED
    with anyio.move_on_after(_slice_of_the_time(1 + len(to_read))):
        gathered = await read(action, says, None if idea is None else idea.reason or None)
    case_of = whose[0] if whose else None
    for left, index in zip(range(len(to_read), 0, -1), to_read, strict=True):
        of = planned[index].action
        if of is None:
            continue
        read_whole = False
        with anyio.move_on_after(_slice_of_the_time(left)):
            try:
                # A step's trust is the person's, already in the case: never read again.
                step_gathered = await read(of, told[index].says, None, with_trust=False)
                async with session.begin_nested():
                    history = await history_of(session, actor, of.action_type, now)
                told[index] = known_of_a_step(
                    told[index],
                    of,
                    gathered=step_gathered,
                    history=history,
                    now=now,
                    zone=settings.time_zone,
                )
                read_whole = all(name == "concerned" for name in step_gathered.missing)
            except Exception as error:
                logger.warning(
                    "A step of a chain could not be read for the judge (%s)", type(error).__name__
                )
        if not read_whole:
            # A read that was cut or failed may leave the session unusable: the next ones would
            # only fail, and the steps left stay as they were.
            break
    return gathered, born_of, case_of


async def _gathered(
    ctx: ToolContext,
    *,
    actor: Actor,
    asks: Person | None,
    action: DescribedAction,
    says: str,
    idea: OwnIdea | None,
    planned: Sequence[PlannedStep],
    told: list[StepTold],
) -> tuple[Gathered, str | None, uuid.UUID | None]:
    """``_gather_in`` in a short read-only session of its own, within ``_REMEMBERED_SECONDS``
    (the chain's steps too: ``told`` is what it could read of them, the rest as it was):
    rolled back and closed afterwards, never committed (a share of ``gather`` cut in the middle of
    a query may leave its connection unusable). When nothing can be read, or not in time, the case
    says that everything is missing, and whose case it is is not known: the judgement never fails
    nor waits for it."""
    found: tuple[Gathered, str | None, uuid.UUID | None] | None = None
    with anyio.move_on_after(_REMEMBERED_SECONDS):
        try:
            async with ctx.session_factory() as session:
                try:
                    found = await _gather_in(
                        session,
                        ctx,
                        actor=actor,
                        asks=asks,
                        action=action,
                        says=says,
                        idea=idea,
                        planned=planned,
                        told=told,
                    )
                finally:
                    # Shielded and bounded: what was read, or cut, is put back even when the
                    # time ran out, and nothing of it is ever committed.
                    with anyio.move_on_after(_PUT_BACK_SECONDS, shield=True):
                        try:
                            await session.rollback()
                        except Exception as error:
                            logger.warning(
                                "The judge's reading could not be put back (%s)",
                                type(error).__name__,
                            )
        except Exception as error:
            # What was read before a failure to close the session is kept.
            logger.warning("What the judge may read could not be read (%s)", type(error).__name__)
    if found is None:
        logger.warning("What the judge may read was not read, or not in time")
        return _NOTHING_GATHERED, None, None
    return found


def _idea_told(ctx: ToolContext, idea: OwnIdea | None, born_of: str | None) -> IdeaTold:
    """Whose idea the action is, as the judge reads it: his own when he says so (``idea``, with
    his reason, whose words it was born of and whether the person concerned lacks the right);
    otherwise the person's in a turn that answers a person, his in a turn on his own. Whether the
    turn carries text nobody of the house said (``ToolContext.outside_text``: a note, a request of
    someone else, a tool's or a program's output, a tool's wake) is a fact of the code's
    (``after_outside_words``, ruling R77): what came of it may have been prompted by words that
    are not the person's. Among them, a note born of a source nobody of the house is known for
    (``ToolContext.told_unknown``) is told by name (``UNKNOWN_SOURCE_WORDS``), and so is each
    kind of a program's, a site's or a tool's text in it (``ToolContext.outside_kinds``), with
    which program it is and what it is built to reach (``ToolContext.outside_sources``), the
    person's own words named as theirs beside it, or, with nobody's words in the turn at all
    (``ToolContext.anyones_words``), that nobody said or wrote anything (``outside_words``): the
    judge weighs the source like a person would, and the code refuses nothing for it."""
    told = outside_words(
        unknown=ctx.told_unknown,
        kinds=ctx.outside_kinds,
        their_words=ctx.reply_message_id is not None or ctx.asked_before,
        sources=ctx.outside_sources,
        alone=not ctx.anyones_words,
    )
    outside: bool | str = ctx.outside_text if told is None else told
    if idea is None:
        return IdeaTold("domo" if ctx.on_his_own else "person", after_outside_words=outside)
    return IdeaTold(
        "domo",
        reason=idea.reason or None,
        born_of_words_of=born_of,
        they_lack_the_right=idea.lacks_the_right,
        after_outside_words=outside,
    )


async def consult_the_judge(
    ctx: ToolContext,
    *,
    actor: Actor,
    action: DescribedAction,
    says: str,
    rights: RightsSay,
    steps: Sequence[PlannedStep] = (),
    request: RequestTold | None = None,
    owner_rule: JsonObject | None = None,
    cost: JsonObject | None = None,
    idea: OwnIdea | None = None,
    told_to: uuid.UUID | None = None,
    identity_doubted: bool = False,
) -> Judgement:
    """The yes-judge's verdict on ``action`` judged as ``actor`` (``judged_actor_of``), in the
    turn of ``ctx`` (spec 4). One case, built the same way for every judged action, a person's
    request and the assistant's own act alike (``judge.build_case``): first what the assistant
    knows that bears on it (``_gathered``, within ``_REMEMBERED_SECONDS``: everything missing
    when it cannot be read, never a fallback by itself), then the case itself, in a short
    transaction of its own, within ``_CHECK_SECONDS``, with the household's time zone. Who asks is
    the person of the turn (``ctx.person``), for an act judged as the assistant's too, since it is
    what they asked for; nobody in a turn he acts in on his own (``ToolContext.on_his_own``), where
    the case names him and reads nobody's words. ``idea`` is his own idea (``OwnIdea``), told with
    its reason; ``cost`` what the action costs, when it is known; ``steps`` a chain's planned steps,
    each told with what is known of its own action (``_gather_in``).

    Then the judge's model of the turn judges it (``judge.judge``):
    ``ctx.models.model_for("judge")``, never under ``medium``, and ``None`` outside a turn or when
    the key of the request has no such model. It never raises: a case that cannot be built is a
    ``do`` (``fallback``, logged by its kind), as is a judge that has no model or does not answer:
    nothing is asked of the person because the judge was silent. That holds for a request the
    person made (``ToolContext.they_asked``: the turn answers a message of theirs and was given
    nothing from outside). In a turn that was given words that are not theirs (a note someone left
    them), nobody is known to have asked: the same silence is a ``yes`` (``handle_tool_call`` asks
    the judge once more first). In a turn Domo started himself with a reason
    (``ToolContext.turn_reason``) every judged call is an idea of his: a silence there is never
    acted on (``judge_silent``). In a
    turn a plan woke him for, the words the judge reads are those the plans due keep
    (``_words_of_plans``), and the person asked only when each keeps some
    (``ToolContext.asked_before``). An answer that is no verdict is a lesson about the judge's
    model, kept by the models of the turn (``ModelPicker.note_failure``).

    Its reason is said only to the person whose case it was (ruling R75,
    ``store.judgement_told_to``): the person of the turn when they ask, the person concerned for
    an act of his own. Whoever it is told to (``told_to``, the person of the turn by default:
    whoever changes a request of someone else's) gets the code's own line when it is not theirs;
    the journal keeps the judge's (``Judgement.noted``).

    ``identity_doubted``: the code is not sure of who asks (``Allow.judge_identity``): the judge
    weighs it with the clues of the speaker the case holds, and a judge that does not answer asks
    a proof (``_proof_when_silent``), never a silent ``do``; but for someone heard at another
    person's page asking something ordinary (``barrier.may_act_weakly``), whose silence is the
    usual fallback (final fix wave, case 7) unless the owner said to judge ordinary things too.
    """
    judged = await _judged_by(
        ctx,
        actor=actor,
        action=action,
        says=says,
        rights=rights,
        steps=steps,
        request=request,
        owner_rule=owner_rule,
        cost=cost,
        idea=idea,
        told_to=told_to,
    )
    if not identity_doubted or (
        actor.page_person_id is not None
        and not actor.voice_too_short
        and may_act_weakly(action.action_type, ctx.policy)
        and await _ordinary_done_when_unsure(ctx)
    ):
        return judged
    return _proof_when_silent(judged)


async def _ordinary_done_when_unsure(ctx: ToolContext) -> bool:
    """Whether the owner leaves an ordinary thing asked by an unsure voice to be done
    (``voice.ordinary_when_unsure`` ``"do"``); his "judge" is a rule of his, and so is a setting
    that cannot be read: a silent judge then asks a proof (final fix wave B, I-5)."""
    try:
        async with ctx.session_factory() as session:
            settings = await load_policy_settings(session)
    except Exception as error:
        logger.error(
            "The owner's word on ordinary things could not be read (%s)", type(error).__name__
        )
        return False
    return settings.ordinary_when_unsure == "do"


def _before_the_doubt(
    asked: Asked | None,
    *,
    rule: Ask | None,
    raised: tuple[Ask, str] | None,
    never_ask: NeverAsk | None,
) -> Asked | None:
    """``asked``, when the judge asked a proof of a requester the code is not sure of, with what
    the rules and Domo's raise ask without the judge (at least a yes): a later yes the code is
    sure of, of the real person, meets that (``before_doubt``, final fix wave, case 10). A proof
    the rules or Domo ask stays a proof."""
    if asked is None or asked.ask != "proof" or asked.by != "judge":
        return asked
    without = what_to_ask(None, rule=rule, raised=raised, never_ask=never_ask)
    before = most_demanding("yes", None if without is None else without.ask)
    if before == "proof":
        return asked
    return replace(asked, before_doubt=before)


def _proof_when_silent(judgement: Judgement) -> Judgement:
    """A judge that did not answer on who asks, when the code is not sure of them, asks a proof
    (its fallback kept, so the record says it was not the judge's model)."""
    return judgement if judgement.fallback is None else replace(judgement, verdict="proof")


async def _judged_by(
    ctx: ToolContext,
    *,
    actor: Actor,
    action: DescribedAction,
    says: str,
    rights: RightsSay,
    steps: Sequence[PlannedStep],
    request: RequestTold | None,
    owner_rule: JsonObject | None,
    cost: JsonObject | None,
    idea: OwnIdea | None,
    told_to: uuid.UUID | None,
) -> Judgement:
    """``consult_the_judge``, before what a doubt on who asks changes of a silent judge."""
    they_asked = ctx.they_asked
    asks = None if ctx.on_his_own else ctx.person
    told = [planned_step_told(step) for step in steps]
    gathered, born_of, read_for = await _gathered(
        ctx,
        actor=actor,
        asks=asks,
        action=action,
        says=says,
        idea=idea,
        planned=steps,
        told=told,
    )
    case_of = ctx.person.id if asks is not None and idea is None else read_for
    reader = ctx.person.id if told_to is None else told_to
    case = None
    with anyio.move_on_after(_CHECK_SECONDS):
        try:
            async with ctx.session_factory() as session:
                settings = await load_policy_settings(session)
                case = await build_case(
                    session,
                    actor=actor,
                    asks=asks,
                    action=action,
                    says=says,
                    rights=rights,
                    conversation_id=ctx.conversation_id,
                    now=ctx.clock(),
                    zone=settings.time_zone,
                    idea=_idea_told(ctx, idea, born_of),
                    gathered=gathered,
                    steps=told,
                    request=request,
                    owner_rule=owner_rule,
                    speaker=ctx.speaker,
                    words_override=_words_of_plans(ctx),
                    cost=cost,
                )
        except Exception as error:
            logger.error("The case of the judge could not be built (%s)", type(error).__name__)
            return fallback("error", they_asked=they_asked)
    if case is None:
        logger.error("Building the case of the judge took too long")
        return fallback("error", they_asked=they_asked)
    try:
        with anyio.fail_after(_CHECK_SECONDS):
            model = None if ctx.models is None else await ctx.models.model_for("judge")
        # An answer that is no verdict is a lesson about the judge's model.
        judged = await judge(model, case, learns=ctx.models, they_asked=they_asked)
        # Whether the case left someone's agreement to it, as the case held it; its reason told
        # only to the person whose case it was.
        judged = judgement_told_to(judged, case_of=case_of, reader=reader)
        return replace(judged, decided=case.rights.judge_decides)
    except Exception as error:
        logger.error("The judge failed (%s)", type(error).__name__)
        return fallback("error", they_asked=they_asked)


async def run_with_a_request(
    ctx: ToolContext,
    reply_id: uuid.UUID | None,
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
                # saying it was done before it was (``conversation.prepare``). A review offers no
                # chain: without a reply there is no line to store.
                if reply_id is not None:
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


def needs_them_in_the_household(ctx: ToolContext) -> bool:
    """Whether a call is refused (``not_allowed``) once the person of the turn is no longer in the
    household: in every turn, but never in a review of what was said (``ToolContext.in_review``),
    whose calls through here are the assistant's own, judged as him with his rights: the person
    reviewed, who may have left (their last review), is only where the idea was born."""
    return not ctx.in_review


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
        # The marks of their record as they are now; how sure the code is of who speaks stays the
        # turn's.
        ctx.actor = replace(
            actor_of(person),
            standing=ctx.actor.standing,
            voice_too_short=ctx.actor.voice_too_short,
            page_person_id=ctx.actor.page_person_id,
            subject=ctx.actor.subject,
        )
        return "active"
    logger.error("Reading the person again took too long")
    return "unknown"


async def _decide_now(
    ctx: ToolContext,
    action: DescribedAction,
    actor: Actor,
    *,
    for_the_person: bool = False,
    risks: Risks | None = None,
) -> Decision | Undecided:
    """The barrier's decision on ``action`` for ``actor``, who the call is judged as
    (``judged_actor_of``), with the owner's rights and the facts of the moment as they are now
    (``decide_now``), read in a short transaction of its own; why there is none when they could
    not be read (``not_read``), or not within ``_CHECK_SECONDS`` (``took_too_long``): nothing may
    run then.

    ``for_the_person``: an idea Domo marked in a person's turn (ruling R26). The barrier decides
    for the person of the turn too (``ctx.actor``), a right merely not given counted as given
    (``barrier.given_for_an_idea``), and the stricter stands (``barrier.stricter``): who speaks
    must be trusted, and what the owner wrote for them or their role (an approver, "ask me
    first", a limit) and an adult's agreement for a child still hold. ``risks``: what the
    operations of a launch declare (``_risks_of``). A call decided as the assistant's own act in
    a person's turn keeps the doubt on who asked it (``decision.doubted``: ``judge_identity``)."""
    with anyio.move_on_after(_CHECK_SECONDS):
        try:
            async with ctx.session_factory() as session:
                now = ctx.clock()
                decision = await decide_now(session, actor, action, ctx.policy, now, risks=risks)
                if (
                    actor.subject == "domo"
                    and not ctx.on_his_own
                    and not isinstance(decision, Refuse)
                    and await doubted(session, ctx.actor, action, ctx.policy, risks=risks)
                ):
                    # His own act, asked by someone the code is not sure of: the judge weighs
                    # who asked it, never decided as a sure assistant alone.
                    decision = replace(decision, judge_identity=True)
                if not for_the_person:
                    return decision
                theirs = given_for_an_idea(
                    ctx.policy, ctx.actor.role, action.action_type, child=ctx.actor.child
                )
                # His side read the owner's directives, with his exception: not again here.
                person = await decide_now(
                    session, ctx.actor, action, theirs, now, directives_read=True, risks=risks
                )
                return stricter(person, decision)
        except Exception as error:
            logger.error("The rights could not be read for a decision (%s)", type(error).__name__)
            return "not_read"
    logger.error("Reading the rights for a decision took too long")
    return "took_too_long"


async def _decided_again(
    ctx: ToolContext,
    tool: InternalTool,
    tool_name: str,
    action: DescribedAction,
    taken: OwnIdea | None = None,
) -> Decision | ActionRecord:
    """Once the judge let ``action`` run, what the barrier says of it now: the person read again
    (``read_the_person_again``), then ``_decide_now`` for who the call of ``tool`` is judged as
    now (``judged_actor_of``, with the mark Domo put on it, ``taken``: the person's role read
    again counts). The judge may take a while, and what changed meanwhile counts
    before anything runs: someone no longer in the household is ``refused`` (``not_allowed``); a
    person or rights that cannot be read make it ``failed`` (nothing runs); otherwise the decision
    of now."""
    match await read_the_person_again(ctx):
        case "gone" if needs_them_in_the_household(ctx):
            logger.warning(
                "A call of the model was refused: the person left the household while the "
                "judge answered"
            )
            return _refused(tool_name, action.action_type, "not_allowed")
        case "unknown":
            return undecided_record(tool_name, action.action_type, "not_read")
        case "active" | "gone":
            pass
    decision = await _decide_now(
        ctx,
        action,
        judged_actor_of(tool, ctx, taken),
        for_the_person=taken is not None and not ctx.on_his_own,
        risks=await _risks_of(tool, ctx.actor),
    )
    if isinstance(decision, str):
        return undecided_record(tool_name, action.action_type, decision)
    return decision


async def _their_rights_on(ctx: ToolContext, action_type: str) -> tuple[bool, bool] | Undecided:
    """What the rights in force say of the person of the turn (``ctx.actor``) on
    ``action_type``, for an idea of Domo's in their turn: whether the owner denied it to them on
    purpose (``RightsTable.denied_on_purpose``), and whether they lack it (not allowed for them,
    ``RightsTable.resolve``). Why there is no answer when the rights could not be read
    (``not_read``), or not within ``_CHECK_SECONDS`` (``took_too_long``)."""
    with anyio.move_on_after(_CHECK_SECONDS):
        try:
            async with ctx.session_factory() as session:
                rights = await load_rights(session)
        except Exception as error:
            logger.error("The rights could not be read for an idea (%s)", type(error).__name__)
            return "not_read"
        denied = rights.denied_on_purpose(ctx.actor, action_type)
        lacks = not rights.resolve(ctx.actor, action_type, ctx.policy).allowed
        return denied, lacks
    logger.error("Reading the rights for an idea took too long")
    return "took_too_long"


async def _with_whole_names(
    tool: InternalTool, tool_name: str, arguments: JsonObject, ctx: ToolContext
) -> JsonObject | Undecided:
    """``arguments``, with the people a tool that names them (``NamesPeople``) names by their
    whole name (``household.service.whole_names``): what is described, judged, asked, stored and
    run is about the person found, as the people who read it read it (spec 6.7). Why there are
    none when the names cannot be read (``not_read``), or take more than ``_CHECK_SECONDS``
    (``took_too_long``, a machine that stalls that long): nothing then runs, and it may be tried
    again. The read has a session of its own: a query cut in the middle leaves nothing of the
    turn's behind."""
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
                    among=names_among(tool),
                )
        except Exception as error:
            logger.error(
                "The names of a call of %s could not be read (%s)", tool_name, type(error).__name__
            )
            return "not_read"
    logger.error("Reading the names of a call of %s took too long", tool_name)
    return "took_too_long"


async def _described_and_decided(
    tool: InternalTool,
    tool_name: str,
    arguments: JsonObject,
    ctx: ToolContext,
    actor: Actor,
    *,
    for_the_person: bool = False,
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
        planning = anyio.move_on_after(_TOOL_SECONDS)
        with planning:
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
        if plan is None and planning.cancelled_caught:
            # Planning decides each step: it took too long, and nothing was done.
            return undecided_record(tool_name, tool.action_type, "took_too_long")
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
    decision = await _decide_now(
        ctx, action, actor, for_the_person=for_the_person, risks=await _risks_of(tool, ctx.actor)
    )
    if isinstance(decision, str):
        # Nothing was decided, so nothing ran: the action failed, and may be tried again.
        return undecided_record(tool_name, action.action_type, decision)
    return action, decision, None


async def _waits(
    ctx: ToolContext,
    reply_id: uuid.UUID | None,
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
    idea_reason: str | None = None,
    idea: OwnIdea | None = None,
    marked: bool = False,
    to_the_concerned: bool = False,
    in_their_turn: bool = False,
) -> ActionRecord:
    """The call stored as a request that waits (``_store_waiting``), and recorded when it cannot
    wait: for the agreement the barrier asks (``decision``: the socle, unchanged), else for the
    person's own yes, asking what ``asked`` says (a plain yes of the approver when nothing more is
    asked); for ``approver_at_least`` when that one is more demanding (a call that carries the yes
    of a stop). Nothing has happened: the result says so, who must say yes, how, who asked it and
    why; for a chain, how many steps it has.

    For an idea of Domo's (``idea``: his mark, a turn he began, a turn on his own) that asks
    nobody else's agreement, the yes is that of the person the thing concerns, as the code alone
    says (``_concerned``: never the judge, whose reasons are text): the person of this
    conversation, the request then stored here as theirs to answer, or someone else, in their own
    conversation (``_store_waiting``), the result then saying it waits for them (``"who"``, their
    name). When nobody can answer for it (the household has no owner now), nothing is asked: the
    action is ``refused`` (``nobody_to_ask``), a fact noted. ``marked``: the call is his by his
    mark, and so is the request. A person's own call whose tool says someone else's yes is due
    (``to_the_concerned``: ``AsksWhomItsYes``, a house account the owner removed, Task 27) goes
    the same way to the person the thing concerns, the request staying the person's, when the
    barrier asks nobody else's agreement; when it asks one (a right's adult), the tool's approver
    (``approver_at_least``) stays the least of whom it waits for (ruling R71). A chain
    (``plan``) is never moved to a person concerned (ruling R34): it waits in this conversation
    for the barrier's approver, else the person of this turn (on his own, the owner, whose
    conversation it is).

    ``failed`` when no request could be stored (nobody may then be told that a yes is awaited)."""
    approver: Approver = decision.approver if isinstance(decision, NeedsApproval) else "self"
    # A person's own call that nobody else's agreement holds goes to the person concerned below.
    concerned_first = to_the_concerned and approver == "self"
    if not concerned_first and _APPROVERS_IN_ORDER.index(
        approver_at_least
    ) > _APPROVERS_IN_ORDER.index(approver):
        approver = approver_at_least
    concerned: _Concerned | None = None
    if plan is not None and approver == "self" and ctx.on_his_own:
        # A chain is never moved to a person concerned (ruling R34): it waits here, for the
        # person of this conversation (on his own, the owner, woken for the house), whose yes
        # covers its steps; his own yes is nobody's.
        approver = "owner"
    elif approver == "self" and (idea is not None or concerned_first) and plan is None:
        found = await _concerned(ctx, action)
        if found is None:
            return await _recorded(
                ctx,
                reply_id,
                failed_record(tool_name, action.action_type),
                action=action,
                decision=decision,
                judged=judged,
                actor=actor,
                idea_reason=idea_reason,
            )
        if found == "nobody":
            return await _recorded(
                ctx,
                reply_id,
                _refused(tool_name, action.action_type, "nobody_to_ask"),
                action=action,
                decision=decision,
                judged=judged,
                actor=actor,
                idea_reason=idea_reason,
                noted=_Noted("failed", "code", True, _NOBODY_TO_ASK),
            )
        concerned = found
    elif reply_id is None and ctx.in_review and approver != "self" and plan is None:
        # A review shows nothing under a reply: what waits for the barrier's approver (the
        # owner, or an adult: the owner is one) waits in the owner's conversation, as news for
        # him, never in the conversation of the person reviewed.
        owner = await _the_owner(ctx)
        if owner is None:
            return await _recorded(
                ctx,
                reply_id,
                _refused(tool_name, action.action_type, "nobody_to_ask"),
                action=action,
                decision=decision,
                judged=judged,
                actor=actor,
                idea_reason=idea_reason,
                noted=_Noted("failed", "code", True, _NOBODY_TO_ASK),
            )
        concerned = owner
    asking = asked or Asked("yes", "approver", None)
    elsewhere = _as_news(ctx, reply_id, concerned)
    waits: JsonObject = {
        "status": "waiting_for_confirmation",
        "by": WHO_SAYS_YES[approver] if not elsewhere else _THE_PERSON_IT_CONCERNS,
    }
    if concerned is not None and elsewhere:
        waits["who"] = concerned.name
    if plan is not None:
        waits["steps"] = len(plan.steps)
    waits["asked_by"] = asking.by
    if asking.why is not None:
        waits["why"] = asking.why
    card: JsonObject = (
        {"key": "action.card.waiting.concerned", "params": {"name": concerned.name}}
        if concerned is not None and elsewhere
        else {"key": f"action.card.waiting.{approver}", "params": {}}
    )
    record = ActionRecord(
        tool_name=tool_name,
        action_type=action.action_type,
        status="waiting",
        confirmation_id=None,
        model_result=waits,
        card=card,
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
        idea_reason=idea_reason,
        marked=marked,
        concerned=concerned,
        in_their_turn=in_their_turn,
    )
    if waiting is not None:
        return waiting
    # No request exists: nobody may be told that a yes is awaited.
    return await _recorded(
        ctx,
        reply_id,
        failed_record(tool_name, action.action_type),
        action=action,
        decision=decision,
        judged=judged,
        actor=actor,
        idea_reason=idea_reason,
    )


@dataclass(frozen=True)
class _Concerned:
    """The person who answers for a thing (``policy.subjects.concerned_of``), with their name as
    it is now."""

    id: uuid.UUID
    name: str


# Who says yes to a request that waits for someone else than the person of the turn, as the model
# is told (with ``"who"``, their name).
_THE_PERSON_IT_CONCERNS: Final = "the person it concerns"


async def _concerned(
    ctx: ToolContext, action: DescribedAction
) -> _Concerned | Literal["nobody"] | None:
    """Who answers for what ``action`` is about, as the code says (``policy.subjects``: whom the
    thing belongs to, the owner for the house), read within ``_CHECK_SECONDS``: ``"nobody"`` when
    the household has no owner now, ``None`` when it could not be read (logged)."""
    with anyio.move_on_after(_CHECK_SECONDS):
        try:
            async with ctx.session_factory() as session:
                subject = subject_of(action, None, ctx.person.id)
                try:
                    concerned = await concerned_of(session, subject, action, ctx.clock())
                except LookupError:
                    return "nobody"
                person = await session.get(Person, concerned)
                if person is None:
                    return None
                return _Concerned(person.id, person.display_name)
        except Exception as error:
            logger.error("Who a request concerns could not be read (%s)", type(error).__name__)
            return None
    logger.error("Reading who a request concerns took too long")
    return None


async def _the_owner(ctx: ToolContext) -> _Concerned | None:
    """The owner of now, as the person a request waits for; ``None`` when there is none, or he
    could not be read (logged)."""
    with anyio.move_on_after(_CHECK_SECONDS):
        try:
            async with ctx.session_factory() as session:
                try:
                    owner_id = await owner_of_now(session, ctx.clock())
                except LookupError:
                    return None
                person = await session.get(Person, owner_id)
                return None if person is None else _Concerned(person.id, person.display_name)
        except Exception as error:
            logger.error("The owner could not be read (%s)", type(error).__name__)
            return None
    logger.error("Reading the owner took too long")
    return None


async def _whose_yes(
    tool: InternalTool, tool_name: str, arguments: JsonObject, ctx: ToolContext
) -> Approver | ActionRecord | None:
    """Whose yes a request of this call waits for, when it is not the person's own
    (``AsksWhomItsYes``); ``None`` for any other tool. When it cannot be read, or not within
    ``_CHECK_SECONDS``, the record of the call instead (``undecided_record``): never a request
    waiting for the usual yes when someone else's may be due (ruling R24), nothing done, and it
    may be tried again."""
    if not isinstance(tool, AsksWhomItsYes):
        return None
    try:
        with anyio.fail_after(_CHECK_SECONDS):
            return await tool.whose_yes(arguments, ctx)
    except TimeoutError:
        logger.error("Reading whose yes a call waits for took too long")
        return undecided_record(tool_name, tool.action_type, "took_too_long")
    except Exception as error:
        logger.error("Whose yes a call waits for could not be read (%s)", type(error).__name__)
        return undecided_record(tool_name, tool.action_type, "not_read")


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
    except TimeoutError:
        logger.error("Reading the yes a call carries took too long")
        return undecided_record(tool_name, tool.action_type, "took_too_long")
    except Exception as error:
        logger.error("The yes a call carries could not be read (%s)", type(error).__name__)
        return undecided_record(tool_name, tool.action_type, "not_read")


def _told_after(record: ActionRecord, cost: JsonObject | None) -> ActionRecord:
    """A run the judge let through without the person's yes for its cost: what the assistant
    reads says he tells them what was spent (``told_after``, with the amount), never left to a
    log. Nothing for a call that did not run or failed: what it spent is not known, and a
    spending that never happened is never announced (its result tells the failure)."""
    if record.status != "done":
        return record
    amount = None if cost is None else cost.get("amount_cents")
    told: JsonObject = {**record.model_result, "told_after": True, "amount_cents": amount}
    return replace(record, model_result=told)


async def _their_cost_weighed(
    ctx: ToolContext,
    reply_id: uuid.UUID | None,
    tool: InternalTool,
    tool_name: str,
    arguments: JsonObject,
    action: DescribedAction,
    decision: Allow,
    waiting_for: WaitsForAYes,
    *,
    actor: Actor,
    idea: OwnIdea | None,
    taken: OwnIdea | None,
    idea_reason: str | None,
) -> ActionRecord:
    """A call that carries the person's own yes for a cost (``WaitsForAYes.judge_first``), let
    through by the barrier with no rule of the owner's (spec part 12): their spending limit and
    any rule of his were applied before the stop existed, so only that plain yes is weighed. The
    judge reads the cost (``consult_the_judge``, ``cost``): ``do`` runs it now, as a stop the
    assistant answers himself does, the barrier asked again first, and the result says he tells
    them what was spent (``_told_after``); ``do_not`` refuses it; any other verdict, and a judge
    that did not answer (whatever its fallback), waits for their yes as the stop asks, or more
    when the judge asks more."""
    says = waiting_for.asked.why or describe_for_people(action, arguments, REFERENCE)
    judgement = await consult_the_judge(
        ctx,
        actor=actor,
        action=action,
        says=says,
        rights=rights_say(decision),
        owner_rule=owner_rule_of(decision.never_ask),
        cost=waiting_for.cost,
        idea=idea,
        identity_doubted=decision.judge_identity,
    )
    # A judge silent on who asks, when the code is not sure of them, asked a proof: it counts.
    weighed = judgement.fallback is None or decision.judge_identity
    if weighed and judgement.verdict == "do_not":
        record = refused_by_the_judge(tool_name, action.action_type, judgement.reason)
        return await _recorded(
            ctx,
            reply_id,
            record,
            action=action,
            decision=decision,
            judged=judgement,
            actor=actor,
            idea_reason=idea_reason,
            noted=_Noted("judge_refused", "domo", idea is not None, judgement.reason or None),
        )
    now: Allow | NeedsApproval = decision
    if weighed and judgement.verdict == "do":
        # The judge may take a while: what the barrier says now decides.
        again = await _decided_again(ctx, tool, tool_name, action, taken)
        if isinstance(again, ActionRecord):
            return await _recorded(
                ctx,
                reply_id,
                again,
                action=action,
                judged=judgement,
                actor=actor,
                idea_reason=idea_reason,
            )
        if isinstance(again, Refuse):
            record = _refused(
                tool_name, action.action_type, again.reason, limit=again.limit, side=again.side
            )
            return await _recorded(
                ctx,
                reply_id,
                record,
                action=action,
                decision=again,
                judged=judgement,
                actor=_refused_as(ctx, again, actor),
                idea_reason=idea_reason,
            )
        if isinstance(again, Allow) and again.rule is None:
            ran = await execute_allowed(tool, tool_name, action.action_type, arguments, ctx)
            record = _told_after(ran, waiting_for.cost)
            stored = await _recorded(
                ctx,
                reply_id,
                record,
                action=action,
                decision=again,
                judged=judgement,
                actor=actor,
                idea_reason=idea_reason,
            )
            if stored.action_id is None:
                logger.error(
                    "The tool %s was let through for a cost (it ended %s) but the action could "
                    "not be recorded nor audited: check what it did",
                    tool_name,
                    record.status,
                )
            return stored
        # Something now asks more than their plain yes: it waits, as that says.
        now = again
    rule = now.rule if isinstance(now, Allow) else None
    asked = most_demanding_asked(
        waiting_for.asked,
        what_to_ask(judgement if weighed else None, rule=rule, raised=None),
    )
    return await _waits(
        ctx,
        reply_id,
        tool_name,
        arguments,
        action,
        now,
        asked,
        None,
        judgement,
        approver_at_least=waiting_for.approver,
        actor=actor,
        domo_asks=waiting_for.domo_asks,
        idea_reason=idea_reason,
        idea=idea,
        marked=taken is not None,
    )


async def handle_tool_call(
    call: ToolCall, *, ctx: ToolContext, reply_id: uuid.UUID | None, over_limit: bool = False
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
        case "gone" if needs_them_in_the_household(ctx):
            logger.warning(
                "A call of the model was refused: the person is no longer in the household"
            )
            record = _refused(tool_name, None if tool is None else tool.action_type, "not_allowed")
            return await _recorded(ctx, reply_id, record)
        case "unknown":
            # Nothing can be decided without them: nothing was done, and it may be tried again.
            action_type = None if tool is None else tool.action_type
            record = undecided_record(tool_name, action_type, "not_read")
            return await _recorded(ctx, reply_id, record)
        case "active" | "gone":
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
    if isinstance(understood, str):
        # Nothing was decided, so nothing ran: it may be tried again.
        record = undecided_record(tool_name, tool.action_type, understood, what="names")
        return await _recorded(ctx, reply_id, record)
    # From here on, the people the call names are named by their whole name (spec 6.7).
    arguments = understood
    # What Domo marked as his own idea reaches this call, when it changes something (or is a
    # silent type an idea of his reaches) and is no chain; it is used up once the call is
    # described, whatever becomes of it then.
    marked = ctx.own_idea if _takes_the_mark(tool) else None
    in_their_turn = marked is not None and not ctx.on_his_own
    # Who the call is judged as: the person, or Domo for his own acts, for an idea he marked and
    # in a turn of his own. The mark lifts only a right not given: in a person's turn the barrier
    # decides for them too (R26), and it never changes who is asked.
    actor = judged_actor_of(tool, ctx, marked)
    found = await _described_and_decided(
        tool, tool_name, arguments, ctx, actor, for_the_person=in_their_turn
    )
    if isinstance(found, ActionRecord):
        # Nothing was decided, so nothing ran, and the mark waits for the next call.
        return await _recorded(ctx, reply_id, found)
    action, decision, plan = found
    taken = ctx.take_own_idea() if marked is not None else None
    idea = idea_of(ctx, taken)
    idea_reason = None if idea is None else idea.reason or None
    if in_their_turn and idea is not None:
        # The socle first: what the owner denied the person of the turn on purpose is never done
        # as an idea of Domo's in their turn, and no judge is asked. A right merely not given is
        # told to the judge.
        theirs = await _their_rights_on(ctx, action.action_type)
        if isinstance(theirs, str):
            # Nothing was decided, so nothing ran: it may be tried again.
            record = undecided_record(tool_name, action.action_type, theirs)
            return await _recorded(ctx, reply_id, record, idea_reason=idea_reason)
        denied, lacks = theirs
        if denied:
            # Refused for the person, on the owner's word: audited under them.
            refusal = Refuse("not_allowed", side="person")
            record = _refused(tool_name, action.action_type, "not_allowed", side="person")
            return await _recorded(
                ctx,
                reply_id,
                record,
                action=action,
                decision=refusal,
                actor=ctx.actor,
                idea_reason=idea_reason,
            )
        if lacks:
            idea = replace(idea, lacks_the_right=True)
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
        return await _recorded(
            ctx,
            reply_id,
            record,
            action=action,
            decision=decision,
            actor=_refused_as(ctx, decision, actor),
            idea_reason=idea_reason,
        )
    waiting_for = (
        None if plan is not None else await _waits_for_a_yes(tool, tool_name, arguments, ctx)
    )
    if isinstance(waiting_for, ActionRecord):
        return await _recorded(
            ctx,
            reply_id,
            waiting_for,
            action=action,
            decision=decision,
            actor=actor,
            idea_reason=idea_reason,
        )
    if (
        waiting_for is not None
        and waiting_for.judge_first
        and isinstance(decision, Allow)
        and decision.rule is None
    ):
        # The person's own yes for a cost, within their limit: weighed by the judge first.
        return await _their_cost_weighed(
            ctx,
            reply_id,
            tool,
            tool_name,
            arguments,
            action,
            decision,
            waiting_for,
            actor=actor,
            idea=idea,
            taken=taken,
            idea_reason=idea_reason,
        )
    if waiting_for is not None:
        # Any other call that carries a yes waits for it, never judged (an approver's agreement,
        # an owner's rule, a proof: the code's, not the judge's to weigh).
        return await _waits(
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
            idea_reason=idea_reason,
            idea=idea,
            marked=taken is not None,
        )
    judgement: Judgement | None = None
    asked: Asked | None = None
    owners_rule_on_him = False
    if needs_the_judge(action.action_type) or decision.judge_identity:
        # The code not sure of who asks (``judge_identity``): the judge weighs it whatever the
        # type's usual judging, and its silence asks a proof.
        never_ask = decision.never_ask if plan is None else plan.never_ask
        says = (
            describe_for_people(action, arguments, REFERENCE)
            if plan is None
            else describe_chain(plan.arguments, REFERENCE)
        )
        if plan is None and decision.judge_identity:
            # Who asks is doubted: what it may do in the house with a risk is told (O1/O3).
            says = _with_risky_acts(says, tool)
        # An idea of his is asked of his judge once more when it was silent; still silent, he
        # does nothing for now and asks nobody (spec part 7). So is a call of a turn that answers
        # the person but was told words from outside, before the fallback asks them a yes (audit
        # CNV-2, ruling R6).
        for _ in range(2 if idea is not None or _answers_with_outside_words(ctx) else 1):
            judgement = await consult_the_judge(
                ctx,
                actor=actor,
                action=action,
                says=says,
                rights=rights_say(decision, judged_first=plan is None),
                steps=() if plan is None else plan.steps,
                owner_rule=owner_rule_of(never_ask),
                idea=idea,
                identity_doubted=decision.judge_identity,
            )
            if judgement.fallback is None:
                break
        assert judgement is not None
        if idea is not None and judgement.fallback is not None:
            record = judge_silent(tool_name, action.action_type)
            return await _recorded(
                ctx,
                reply_id,
                record,
                action=action,
                decision=decision,
                judged=judgement,
                actor=actor,
                idea_reason=idea_reason,
            )
        if judgement.fallback is not None and _a_verdict_needed(action, plan):
            # Only the judge's verdict lets it go: silent, nothing is done and nobody is asked.
            record = not_weighed(tool_name, action.action_type)
            return await _recorded(
                ctx,
                reply_id,
                record,
                action=action,
                decision=decision,
                judged=judgement,
                actor=actor,
                idea_reason=idea_reason,
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
                idea_reason=idea_reason,
                # A refusal of the judge is a fact (spec 9.1), with the reason that may be said to
                # the person (the journal alone keeps `noted`: it may tell of someone else).
                noted=_Noted("judge_refused", "domo", idea is not None, judgement.reason or None),
            )
        asked = what_to_ask(
            judgement,
            rule=decision.rule if plan is None else plan.rule,
            raised=raised,
            never_ask=never_ask,
        )
        if decision.judge_identity:
            asked = _before_the_doubt(
                asked,
                rule=decision.rule if plan is None else plan.rule,
                raised=raised,
                never_ask=never_ask,
            )
        if asked is None and warned_first(judgement, action, plan, never_ask):
            # What a judge must have weighed first (the last key) never runs on its silence, nor
            # on a do under the owner's "never ask me" with conditions: a plain yes is asked.
            # Only his "never ask me" outright (at_all) lets it run.
            asked = Asked("yes", "judge", None)
        through = _judged_through(decision, judgement, plan, asked)
        if plan is None and (isinstance(decision, Allow) or through) and asked is None:
            # About to run on a decision made before the judge: what the barrier says now
            # decides. (A chain's runner reads the person and the barrier again at each step.)
            again = await _decided_again(ctx, tool, tool_name, action, taken)
            if isinstance(again, ActionRecord):
                return await _recorded(
                    ctx,
                    reply_id,
                    again,
                    action=action,
                    judged=judgement,
                    actor=actor,
                    idea_reason=idea_reason,
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
                    actor=_refused_as(ctx, again, actor),
                    idea_reason=idea_reason,
                )
            if (
                through
                and isinstance(again, NeedsApproval)
                and isinstance(decision, NeedsApproval)
                and again.judge_first
                and (again.approver, again.question) == (decision.approver, decision.question)
            ):
                # The agreement the judge was left to decide, still the same: its do spares it.
                again = Allow(again.rule, again.never_ask)
            decision = again
            asked = what_to_ask(
                judgement,
                rule=decision.rule,
                raised=raised,
                never_ask=decision.never_ask,
            )
            if asked is None and action.action_type in WARNED_FACTS:
                # What the judge weighed must still be what runs: described again, a fact it
                # must weigh first (the last key) that became true meanwhile asks a plain yes,
                # for the action as it is now.
                now_described = await _described_again(tool, arguments, actor, ctx)
                if now_described is not None and warned_since(
                    action.action_type, now_described.facts, action.facts
                ):
                    action = now_described
                    asked = Asked("yes", "judge", None)
    elif plan is None and is_made(action.action_type) and decision.rule is not None:
        # An owner's rule asks before one of Domo's tools (spec 5.8, final review I2): asked as
        # he says, with no judge; the request waits and is shown like any other.
        asked = Asked(decision.rule, "rule", None)
    elif plan is None and isinstance(decision, Allow) and decision.rule is not None:
        # The owner's rule on what no judge weighs (a plan, a memory, what he keeps on his own:
        # ruling R41): asked as he says, whatever the type changes or shows, with what Domo
        # raised; the request waits and is shown, and it is the owner's rule on him, so the owner
        # answers it.
        asked = what_to_ask(None, rule=decision.rule, raised=raised, never_ask=decision.never_ask)
        owners_rule_on_him = actor.subject == "domo"
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
            # A tool whose facts a judge must weigh first is told what was judged
            # (``ToolContext.described_action``): it never runs on a fact that became true since.
            run_ctx = (
                replace(ctx, described_action=action)
                if plan is None and action.action_type in WARNED_FACTS
                else ctx
            )
            # Who it was judged as, while it runs: whom the tool records as having acted.
            run_ctx.judged_as = actor
            try:
                record = await execute_allowed(
                    tool, tool_name, action.action_type, arguments, run_ctx
                )
            finally:
                run_ctx.judged_as = None
            if plan is not None and record.confirmation_id is not None:
                # A chain run at once whose rest waits for someone's agreement, in a request
                # (``tools.chain``): stored as an action that waits on it, its line following
                # the request, never saying the chain was done.
                record = replace(record, status="waiting")
    else:
        whose = None if plan is not None else await _whose_yes(tool, tool_name, arguments, ctx)
        his = isinstance(tool, ActsAsDomo)
        if isinstance(whose, ActionRecord):
            return await _recorded(
                ctx,
                reply_id,
                whose,
                action=action,
                decision=decision,
                judged=judgement,
                actor=actor,
                idea_reason=idea_reason,
            )
        return await _waits(
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
            # Domo's own request for an act of his own (an opening, ruling R24); a person's own
            # call stays theirs and waits for the person the thing concerns (an account the
            # owner removed, asked again: Task 27), the tool's approver the least (R71).
            approver_at_least="owner" if owners_rule_on_him else whose or "self",
            # The owner's rule on an act of his: his request, the owner's to answer, in the
            # owner's own turn too (never one nobody may say yes to).
            domo_asks=(whose is not None and his) or owners_rule_on_him,
            # ... and replayed in the person's turn, for them, never on his own (R1).
            in_their_turn=owners_rule_on_him,
            to_the_concerned=whose is not None and not his,
            idea_reason=idea_reason,
            idea=idea,
            marked=taken is not None,
        )
    stored = await _recorded(
        ctx,
        reply_id,
        record,
        action=action,
        decision=decision,
        judged=judgement,
        actor=actor,
        idea_reason=idea_reason,
    )
    if stored.action_id is None and reply_id is not None:
        logger.error(
            "The tool %s was allowed to do %s (it ended %s) but the action could not be "
            "recorded nor audited: check what it did",
            tool_name,
            action.action_type,
            record.status,
        )
    return stored
