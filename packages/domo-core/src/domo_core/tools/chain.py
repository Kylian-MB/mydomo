"""``do_in_order``: one to twenty of Domo's tools, done in order, in one request with one yes
(spec 4); and ``chain_continue``, which takes up a chain that stopped, from the step that failed
(spec 4.4).

Domo uses it only when the steps depend on each other: one changes what the next one needs
("hand my role to Justine, then remove me"). For actions that do not depend on each other, it
calls the tools one by one, as usual.

When the chain is asked for (``DoInOrder.plan``, which ``conversation.execution`` uses in place of
``describe`` and the barrier's decision):

* it holds ``MIN_STEPS`` to ``MAX_STEPS`` steps (spec 4.6: a chain counts as one call of an
  answer, whatever its number of steps), each a tool and its arguments, as Domo would call it
  itself. A step that is itself a ``do_in_order`` is flattened first: its steps take its place, in
  their order, at any depth, and count in the twenty (spec 4.7). Each tool is one the person is
  shown (``tools_for``), and never a tool Domo made (``t_<name>``: tools call each other their
  own way), nor the relaunch of a chain (``chain_continue``,
  ``NEVER_IN_A_CHAIN``) nor a tool that manages requests (the ``confirmation.*`` types: a chain is
  itself a request) nor one that is never shown (Domo's memory, ``catalog.is_silent``: a step
  is shown on the chain's card): Domo calls those before or after the chain, in the same
  answer, as the detail of the refusal tells him. Otherwise the chain is ``invalid``, and Domo
  is told which step (numbered once flattened). A chain may hold several steps that make a link
  to show the one who asked (an invitation, a return link), and the form for the model's key:
  each leaves it
  behind its own button on the chain's card, for the one who asked only, and none is shown at
  once (``ToolContext.in_a_chain``, spec 4.5);
* each step, in order, is described by its tool for the person, with the facts of now, then goes
  through the barrier (``decide_now``, each in a short transaction of its own). A refused step
  refuses the whole chain, with its reason and its number (``refused_step``); a step that needs
  someone else's agreement makes the chain need it;
* a step its tool cannot describe now is checked in its turn (its ``action`` is ``None``) only if
  the tool says whom its call acts on (``ActsOn``) and a step before it touches that one: that
  earlier step changes what the tool refuses now ("remove me" while one is still the owner). A
  described step touches its target and the people of its facts (``touched``); a step checked in
  its turn touches whom its tool says it will touch (``tools.context.touched_by``: for the
  hand-over, the giver and the receiver), so that it may be followed in its turn by another
  ("make Sam an adult, hand Sam my role, then remove me"). Its access is the person's now
  (``RightsTable.resolve``: a step they may not do refuses the chain), and whose agreement it
  needs is the barrier's rule (``barrier.approval_needed``). Any other step that cannot be
  described makes the chain invalid, with the tool's reason and the step's number;
* a step may wait for someone's arrival (``"after_arrival_of"``: the name of their invitation,
  spec 4.3b): an invitation an earlier step of the chain makes (``household_invite`` with that
  display name, ``household_bring_back`` whose display name, else person, is that name; or a step
  done of a chain planned again that invited that name), or one that already waits in the household
  (``household.service.resolve_invitation``); otherwise the chain is ``invalid``, with the step's
  number. From the first wait to the end, a step its tool cannot describe now is checked in its
  turn whatever the steps before touch: the newcomer does not exist yet. A wait asks for nothing
  by itself (``ChainPlan.waits`` counts them): a chain that waits for an arrival keeps a request
  for the upkeep to resume, even when nobody is asked a yes
  (``conversation.execution.run_with_a_request``, ``confirmations.arrivals``);
* then the chain itself goes through the barrier, as one more action (``chain.run``, the
  members', in the catalog), once its steps are planned: its facts (how many steps, how many
  checked in their turn, how many wait for an arrival) are known only then. The owner's rights
  apply to it as to any action: a guest, someone a right denies it to, or whose identity cannot be
  trusted is refused it (with no step named), a limit of the owner on chains may refuse it or send
  it to the owner, and a right's approver makes it wait for that agreement, whatever its steps.

Then the chain's decision (``ChainPlan.decision``): refused by a step or by itself; else the
agreement of the most demanding of the approvers its steps and itself need (the owner, then an
adult), with the first question a limit asked; else allowed. The yes-judge then judges the chain
as a whole (``conversation.execution``, spec 4.2), once: what its request asks is the most
demanding of the judge's verdict and the owner's rules on its steps and on chains
(``ChainPlan.rule``); no step asks anything by default. Every step is described once: nothing is
tried again.

The chain's own action (``chain.run``) says how many steps it has, how many are checked in their
turn and how many wait for an arrival; it acts on nothing in particular, its effect is
``permissions`` if a step's is, else its first step's, and it leaves the household if a step does.
The request remembers the role of the person and the ids of their own rights then
(``ChainBasis``), for the steps to be judged after the yes (spec 4.3), and keeps as its arguments
the call with its steps flattened, each with the arrival it waits for (``ChainPlan.arguments``).

A chain that waits for its yes may be changed as a whole (spec 4.1,
``confirmations.service.amend_confirmation``): its steps still to do are planned again, after
those done (they count in what the steps before touch, in the numbers of the steps and in the
twenty). A step kept as it was asked for is judged as the chain judges the person (the role it
remembers, ``policy.decision.decide_in_chain``), and may be one of the tools they were shown then;
a step changed or added is judged with the role of now (``PlannedStep.with_basis``). With no step
left described (each checked in its turn), the chain's effect is ``permissions`` when one of its
steps changes the household, the successors, the rights, the rules or the model's key, else
``message``.

Running it (``DoInOrder.execute``): a chain the judge lets through runs at once, its steps taken
from its arguments again (each still one of the person's tools); a chain that waited runs after the
yes to its request (``ToolContext.request_id``), its steps taken from their rows, the source of
truth, under the rule of the chain and the yes of the one who gave it (``ToolContext.approved_by``).
Both go through ``confirmations.chain.run_steps``, which describes each step again, puts it through
the barrier, runs it and records it, and stops at the first that fails, or pauses the request at a
step that waits for an arrival still to come; the outcome says what came of it
(``confirmations.chain.chain_outcome``). A chain that runs at once and meets a step that needs
someone's agreement does not stop: its rest waits for that approver's yes, in a request stored for
it then (``_the_rest_waits``, JDG-9), with its steps done kept done; Domo reads its id
(``"request_id"``).

``chain_continue`` (``chain.continue``, the members', judged inside: the rest it carries is judged,
not the call) takes up a chain that stopped, from the step that failed: only the person's own
request, in this conversation, that ended ``failed`` with none of its steps whose record is not
known (``planned``) and steps left (``confirmations.chain.can_be_continued``: a chain whose last
step failed is taken up again too), while it lives (the owner's lifetime of a request, from when it
was asked for). What was done stays done. Its steps left are the one that failed, then those it
stopped; Domo gives them again changed (``steps``, never those done), or not:

* the rest, as it is or as Domo gives it, is matched with the steps left
  (``confirmations.replan.match_steps``) and planned again after the steps done
  (``DoInOrder.plan``): a step kept as it was asked for keeps the role the chain remembers, a step
  changed or added is judged with the role of now (spec 4.1). A step the barrier refuses refuses it
  (``invalid``, with its number), and nothing changes. The accord the rest needs is the plan's,
  asked of the barrier now, never copied from the request (spec 5.5.1);
* the yes-judge judges the rest (spec 4.7), told the request's history
  (``confirmations.chain.request_told``): when and how its yes was given and by whom, the steps
  done, the step that failed, why and whether it may have acted (a fact of the code:
  ``confirmations.chain.failed_step_may_have_acted``), and what changed. Its ``do_not`` leaves the
  chain as it is, with its reason;
* what it asks then (``confirmations.judge.ask_after_a_change``, with the yes already given: the
  owner's rules count for the steps changed or added, F2, and for a step
  kept only above what that yes gave) and what Domo asks more (``ask_before_acting``): when
  nothing is asked, and the yes given still covers the accord the plan needs (``store.covers``,
  from the one who gave it, ``the_one_who_said_yes``, or the person's own when that one is no
  longer in the household), the rest runs under that yes:
  the step that failed ``stopped`` again (or the rows left replaced by the plan's steps, with a
  change), the request ``executing`` again (``chain.continued`` is audited, with how many steps
  are left), and its steps left run as after the yes, each judged again in its turn; what came of
  it is recorded as for a yes (``run_under_the_same_yes``, ``confirmations.service.settle``), and
  its card is the request's;
* otherwise a new request (``confirmations.replan.replace_chain_request``) replaces the chain, one
  yes for all of the rest: it holds the steps done, copied as they are, then the steps left as
  planned, the step that failed and may have acted, kept as it was asked for, marked as one that
  may already have been done (``confirmations.steps.MAYBE_DONE``); it remembers what the chain
  remembered of the one who asked, asks what was found (with who asked it and why), waits for the
  agreement the plan needs (the person's own when it needs none), and is presented by this turn's
  reply. Domo is told whose yes it waits for and how (``"by"``, ``"answer_with"``), who asked it
  and why (``"asked_by"``, ``"why"``), how many steps are left and whether one may already have
  been done (``"maybe_done"``); ``chain.continued`` is audited with the request it replaced and the
  new one (``"replaced_by"``).
"""

import logging
import uuid
from collections.abc import Iterator, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Final, Literal, cast

import anyio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.accounts.sessions import is_active
from domo_core.audit.log import append_audit
from domo_core.chat.agent import ActionRecord
from domo_core.confirmations.chain import (
    STEPS_LEFT,
    RestAsked,
    can_be_continued,
    chain_outcome,
    failed_step_may_have_acted,
    mark_maybe_done,
    planned_of,
    request_told,
    run_steps,
    stopped_without_a_request,
    stored_steps,
    waits_again,
)
from domo_core.confirmations.describe import describe_chain, step_sentence
from domo_core.confirmations.judge import (
    Judgement,
    ask_after_a_change,
    audit_judgement,
    owner_rule_of,
    planned_step_told,
)
from domo_core.confirmations.models import Confirmation, ConfirmationStep
from domo_core.confirmations.replan import (
    CHAIN_TOOL,
    WAITS_FOR,
    as_call,
    call_of,
    calls_of,
    match_steps,
    replace_chain_request,
    step_call,
    unchanged,
)
from domo_core.confirmations.service import DecidedVia, settle, what_the_yes_gave
from domo_core.confirmations.steps import PlannedStep, add_steps, mark_step, steps_of
from domo_core.confirmations.store import (
    asked_of,
    covers,
    create_confirmation,
    how_to_answer,
    is_past_lifetime,
)
from domo_core.conversation.execution import (
    JUDGE_REFUSED_DETAIL,
    WHO_SAYS_YES,
    consult_the_judge,
    rights_say,
)
from domo_core.household.service import resolve_invitation, resolve_person, whole_names
from domo_core.llm.types import JsonObject, ToolDefinition
from domo_core.madetools.models import OFFERED_PREFIX
from domo_core.policy.actions import DescribedAction, Destination, Effect
from domo_core.policy.asks import Ask, Asked, NeverAsk, most_demanding
from domo_core.policy.barrier import (
    Allow,
    Decision,
    NeedsApproval,
    Refuse,
    approval_needed,
    record_decision,
    stricter,
)
from domo_core.policy.catalog import ACTION_FACTS, is_made, is_silent
from domo_core.policy.decision import ChainBasis, decide_in_chain, decide_now, judged_actor
from domo_core.policy.rights import Approver, RightsTable, load_rights
from domo_core.policy.roles import Actor, domo_actor
from domo_core.policy.settings import load_policy_settings
from domo_core.text import MAX_DISPLAY_NAME_LENGTH, name_key
from domo_core.texts.view import REFERENCE
from domo_core.tools.context import (
    ActsOn,
    InternalTool,
    InvalidArguments,
    NamesPeople,
    NeverAStep,
    ToolContext,
    ToolOutcome,
    judged_actor_of,
    tools_for,
    touched_by,
)

logger = logging.getLogger(__name__)

# A chain has one to twenty steps (spec 4.6), once flattened: it counts as one call of an answer.
MIN_STEPS: Final = 1
MAX_STEPS: Final = 20
# A step is read as a change of a chain reads it (``replan.step_call``), and a step that calls the
# chain's own tool (``replan.CHAIN_TOOL``) is flattened (spec 4.7).

# The tools no chain holds: the relaunch of a chain that stopped, which Domo calls beside the
# chain, never in it. Nor does it hold a tool that manages requests (its type starts with
# ``confirmation.``): a chain is itself a request. A chain inside a chain is not among them: it is
# flattened. The tools that make a link, or the form for the model's key, are steps like any other:
# each leaves what it makes behind its own button on the chain's card.
CONTINUE_TOOL: Final = "chain_continue"
NEVER_IN_A_CHAIN: Final = frozenset({CONTINUE_TOOL})
_REQUEST_TYPES: Final = "confirmation."
_CHAIN_TYPES: Final = "chain."

CHAIN_ACTION_TYPE: Final = "chain.run"
CONTINUE_ACTION_TYPE: Final = "chain.continue"
_ARGUMENTS: Final = frozenset({"steps"})
# Whose agreement a chain needs, from the least demanding to the most.
_APPROVERS: Final[tuple[Approver, ...]] = ("self", "adult", "owner")
# The types of action whose effect on a chain with no step described is ``permissions``: those
# that change the household, the successors, the rights, the rules or the model's key.
_PERMISSION_TYPES: Final = ("household.", "successors.", "rights.", "policy.", "model.")
# The end of a list of steps, as it is read (a step may be any JSON value, ``None`` included).
_END: Final = object()
# What Domo is told when a chain has too few steps, or too many.
_COUNT: Final = (
    f"a chain has {MIN_STEPS} to {MAX_STEPS} steps: make several chains, one after the other"
)
# What Domo is told when a step waits for the arrival of a name that no invitation has.
_NO_INVITATION: Final = (
    f"{WAITS_FOR} names an invitation that waits, or one an earlier step of this chain makes"
)
# The steps that make an invitation, and the arguments that hold its name, in order: the first one
# given is the name (a return link keeps the person's name unless another one is given).
_INVITING: Final[Mapping[str, tuple[str, ...]]] = {
    "household.invite": ("display_name",),
    "household.bring_back": ("display_name", "person"),
}
# A step of a chain, as Domo gives it (to ``do_in_order``, and the steps left to
# ``chain_continue``): one of his tools and its arguments, and the arrival it waits for, if any.
_STEP_SCHEMA: Final[JsonObject] = {
    "type": "object",
    "properties": {
        "tool": {"type": "string", "description": "The name of one of your tools."},
        "arguments": {
            "type": "object",
            "description": "Its arguments, as you would give them to it.",
        },
        WAITS_FOR: {
            "type": "string",
            "description": (
                "Only for a step that waits for someone to join the household: the name of their "
                "invitation (one an earlier step makes, or one that already waits). The chain "
                "pauses before this step and goes on by itself once they have joined; the steps "
                "after it may name them."
            ),
            "minLength": 1,
            "maxLength": MAX_DISPLAY_NAME_LENGTH,
        },
    },
    "required": ["tool", "arguments"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class ChainPlan:
    """A chain as planned when it is asked for (see the module docstring).

    ``action`` is the chain's own (``chain.run``), which is stored and audited; ``decision`` the
    chain's: ``Refuse`` with the reason of the step it refused, numbered from 1 in
    ``refused_step`` (``None`` when the chain itself was refused); else ``NeedsApproval``, the most
    demanding agreement its steps and itself need (``_approval``); else ``Allow``. ``steps`` are
    its steps as planned, ``basis`` what it remembers of the one who asked, and ``arguments`` the
    call with its steps flattened (``{"steps": [{"tool", "arguments"}, ...]}``), which the request
    stores. For a chain planned again after some of its steps were done, ``steps`` and
    ``arguments`` are those still to do, and the numbers count the steps done.

    What the judge's verdict meets (``confirmations.judge.what_to_ask``): ``rule``, the most
    demanding of what the owner's rules ask for its steps and for the chain itself;
    ``never_ask``, the owner's "not at all" when he said it for
    every step of the chain (``None`` otherwise). ``waits`` is how many of its steps wait for
    someone's arrival: a chain that waits keeps a request, even let through. ``chain_rule`` is
    what the owner's rule on chains asks (the chain's own action, ``chain.run``); each step keeps
    its own (``PlannedStep.rule``), for what a chain changed or taken up again asks
    (``confirmations.judge.ask_after_a_change``)."""

    action: DescribedAction
    decision: Decision
    steps: tuple[PlannedStep, ...]
    basis: ChainBasis
    refused_step: int | None
    arguments: JsonObject
    rule: Ask | None
    waits: int
    never_ask: NeverAsk | None = None
    chain_rule: Ask | None = None

    def calls(self) -> list[JsonObject]:
        """The calls of the steps planned, as planned (``arguments``: the people they name by
        their whole names): what a changed request keeps as its call, for its card and its run."""
        steps: list[JsonObject] = self.arguments["steps"]
        return list(steps)


def touched(action: DescribedAction) -> frozenset[str]:
    """Whom and what ``action`` touches: its target, and the people of its facts (those the
    catalog declares as a ``person``, ``catalog.ACTION_FACTS``)."""
    kinds = ACTION_FACTS.get(action.action_type, {})
    found = {
        value
        for name, value in action.facts.items()
        if kinds.get(name) == "person" and isinstance(value, str)
    }
    if action.target is not None:
        found.add(action.target)
    return frozenset(found)


@dataclass(frozen=True)
class _Step:
    """A step as the arguments of the chain give it: the tool, its name and its arguments, and the
    name of the invitation whose arrival it waits for (``after``), if it waits for one."""

    tool: InternalTool
    name: str
    arguments: JsonObject
    after: str | None = None


def _flattened(arguments: JsonObject, *, done: int = 0) -> list[tuple[str, JsonObject, str | None]]:
    """The calls of a call of ``do_in_order``, in order, each the name of a tool, its arguments and
    the arrival it waits for (``replan.step_call``), with a step that calls ``do_in_order`` itself
    replaced by its own steps, in their order, at any depth (spec 4.7); ``MIN_STEPS`` to
    ``MAX_STEPS`` of them once flattened, with the ``done`` steps of a chain that is planned again
    before them. ``InvalidArguments`` otherwise (the detail says which step, numbered once
    flattened, after those done; a chain inside the chain does not wait itself: its steps do). The
    lists still to read are kept on a stack: no depth of chains inside chains can exhaust
    Python's."""
    if frozenset(arguments) != _ARGUMENTS:
        raise InvalidArguments("invalid", "the arguments are steps")
    top: object = arguments["steps"]
    if not isinstance(top, list):
        raise InvalidArguments("invalid", "steps is a list of tools, each with its arguments")
    calls: list[tuple[str, JsonObject, str | None]] = []
    reading: list[Iterator[object]] = [iter(cast(list[object], top))]
    while reading:
        item = next(reading[-1], _END)
        if item is _END:
            # That list is read to its end: back to the one it was a step of.
            reading.pop()
            continue
        number = done + len(calls) + 1
        name, step_arguments, after = step_call(item, number)
        if name != CHAIN_TOOL:
            calls.append((name, step_arguments, after))
            if done + len(calls) > MAX_STEPS:
                raise InvalidArguments("invalid", _COUNT)
            continue
        inner: object = step_arguments.get("steps")
        if frozenset(step_arguments) != _ARGUMENTS or not isinstance(inner, list):
            raise InvalidArguments(
                "invalid",
                f"step {number}: a chain inside this one is a list of steps, each a tool and its "
                "arguments",
            )
        if after is not None:
            raise InvalidArguments(
                "invalid",
                f"step {number}: a chain inside this one does not wait itself: give "
                f"{WAITS_FOR} to the step of it that waits",
            )
        reading.append(iter(cast(list[object], inner)))
    if len(calls) < MIN_STEPS:
        raise InvalidArguments("invalid", _COUNT)
    return calls


def _steps_from(
    arguments: JsonObject,
    registry: Mapping[str, InternalTool],
    shown: frozenset[str],
    *,
    done: int = 0,
) -> list[_Step]:
    """The steps of a call of ``do_in_order``, flattened (``_flattened``, after the ``done`` steps
    of a chain that is planned again), each a tool of ``registry`` among those the person is
    ``shown``, and none that a chain may not hold. ``InvalidArguments`` otherwise (the detail says
    which step, never what it holds, and, for a tool that is never a step, what to do instead)."""
    steps: list[_Step] = []
    flattened = _flattened(arguments, done=done)
    for number, (name, step_arguments, after) in enumerate(flattened, start=done + 1):
        tool = registry.get(name)
        if name.startswith(OFFERED_PREFIX) or (tool is not None and is_made(tool.action_type)):
            # A tool Domo made is never a step: tools call each other their own way.
            raise InvalidArguments(
                "invalid",
                f"step {number}: a tool of your own is not a step: call it on its own, before or "
                "after this chain, in the same answer",
            )
        if tool is None:
            raise InvalidArguments("invalid", f"step {number}: no tool has this name")
        if name in NEVER_IN_A_CHAIN or tool.action_type == CONTINUE_ACTION_TYPE:
            raise InvalidArguments(
                "invalid",
                f"step {number}: chain_continue is not a step: call it on its own, before or "
                "after this chain, in the same answer",
            )
        if tool.action_type.startswith(_REQUEST_TYPES):
            raise InvalidArguments(
                "invalid",
                f"step {number}: a tool that manages requests is not a step (a chain is itself a "
                "request): call it before or after the chain, in the same answer",
            )
        if tool.action_type.startswith(_CHAIN_TYPES):
            raise InvalidArguments(
                "invalid", f"step {number}: this tool is not a step: call it on its own"
            )
        if is_silent(tool.action_type):
            # Never shown (Domo's memory): a step of a chain would be, on its card.
            raise InvalidArguments(
                "invalid",
                f"step {number}: this tool is not a step: call it on its own, before or after "
                "this chain, in the same answer",
            )
        if name not in shown:
            raise InvalidArguments("invalid", f"step {number}: this is not one of your tools")
        steps.append(_Step(tool, name, step_arguments, after))
    return steps


def _arguments_of(steps: Sequence[_Step]) -> JsonObject:
    """The call of ``do_in_order`` that does ``steps``, as its arguments (flattened), each step
    with the arrival it waits for (``replan.as_call``)."""
    return {"steps": [as_call(step.name, step.arguments, step.after) for step in steps]}


def _invites(step: _Step) -> str | None:
    """The name (``text.name_key``) of the invitation ``step`` makes, if it makes one
    (``_INVITING``); ``None`` otherwise."""
    for argument in _INVITING.get(step.tool.action_type, ()):
        value = step.arguments.get(argument)
        if isinstance(value, str):
            return name_key(value)
    return None


def _invited_by(done: Sequence[DescribedAction]) -> frozenset[str]:
    """The names (``text.name_key``) of the invitations the steps ``done`` of a chain planned again
    made, as their actions tell them (an invitation's name is one of its facts)."""
    return frozenset(
        name_key(name)
        for action in done
        if action.action_type == "household.invite"
        and isinstance(name := action.facts.get("display_name"), str)
    )


async def _with_whole_names(
    steps: Sequence[_Step],
    invited: AbstractSet[str],
    ctx: ToolContext,
    *,
    speaker: uuid.UUID,
    first: int = 1,
) -> list[_Step]:
    """``steps``, each one whose tool names people (``NamesPeople``) naming them by their whole
    name as ``speaker`` names them (``household.service.whole_names``, spec 6.7), as a call on its
    own does: the chain's card, its judge and its run are about the people found. Never a name the
    chain may be about to bring in: the name of an invitation a step before makes or a step done
    made (``invited``), nor the one a step waits for the arrival of.

    Once someone may come in (after a step that invites or brings someone back, or a step done
    that invited, and from the first wait on), a step may name the newcomer by a shorter form of
    their name, who is not there yet: a name is then given its whole name only when it is a whole
    name or a nickname of the speaker (``exact_only``); a first name or a beginning stays as it was
    given, and the run checks it in its turn (a target that changed stops the chain).

    When ``speaker`` is not the person of ``ctx`` (an approver who changes the chain), the steps
    are their words: each name is a name the chain brings in, or means one person to them by the
    same rule; otherwise ``InvalidArguments`` (``not_found``, ``ambiguous``), with the step's
    number (``first`` is the number of the first of ``steps``), never read through the nicknames
    of the person of ``ctx``."""
    understood: list[_Step] = []
    leave = set(invited)
    opened = bool(invited)
    others_words = speaker != ctx.person.id
    async with ctx.session_factory() as session:
        for index, step in enumerate(steps):
            if step.after is not None:
                leave.add(name_key(step.after))
                opened = True
            if isinstance(step.tool, NamesPeople):
                arguments = await whole_names(
                    session,
                    step.arguments,
                    step.tool.person_arguments,
                    now=ctx.clock(),
                    speaker=speaker,
                    leave=frozenset(leave),
                    exact_only=opened,
                )
                if others_words:
                    try:
                        await _theirs(session, arguments, step.tool, leave, ctx, speaker, opened)
                    except InvalidArguments as error:
                        raise InvalidArguments(
                            error.code, f"step {first + index}: {error.detail or error.code}"
                        ) from None
                step = replace(step, arguments=arguments)
            if (made := _invites(step)) is not None:
                leave.add(made)
                opened = True
            understood.append(step)
    return understood


async def _theirs(
    session: AsyncSession,
    arguments: JsonObject,
    tool: NamesPeople,
    leave: AbstractSet[str],
    ctx: ToolContext,
    speaker: uuid.UUID,
    exact_only: bool,
) -> None:
    """Each name ``tool`` reads in ``arguments`` is one the chain brings in (``leave``) or means
    one person to ``speaker`` (``resolve_person``, ``exact_only`` once someone may come in)."""
    for argument in tool.person_arguments:
        value = arguments.get(argument)
        if isinstance(value, str) and name_key(value) not in leave:
            await resolve_person(
                session, value, now=ctx.clock(), speaker=speaker, exact_only=exact_only
            )


async def _names_an_invitation(
    name: str, earlier: Sequence[_Step], invited: AbstractSet[str], ctx: ToolContext
) -> bool:
    """Whether ``name``, which a step waits for the arrival of, names an invitation: one a step
    ``earlier`` in the chain makes (``_invites``) or one a step done made (``invited``), else the
    one invitation of that name that waits in the household (``resolve_invitation``)."""
    key = name_key(name)
    if key in invited or any(_invites(step) == key for step in earlier):
        return True
    async with ctx.session_factory() as session:
        try:
            await resolve_invitation(session, name, now=ctx.clock())
        except InvalidArguments:
            return False
    return True


def _chain_action(
    count: int,
    checked: int,
    described: Sequence[DescribedAction],
    planned_types: Sequence[str],
    waits: int = 0,
) -> DescribedAction:
    """The chain's own action, from the steps that were described: its effect is ``permissions``
    if one of theirs is, else the first one's. A chain may have none described (each of its steps
    is checked in its turn: one planned again after some of its steps were done, or one whose
    steps wait for a newcomer): its effect is then ``permissions`` if the type of one of its steps
    (``planned_types``) changes the household, the successors, the rights, the rules or the
    model's key, else ``message``. Its facts: how many steps it has (``count``), how many are
    checked in their turn (``checked``), and how many wait for an arrival (``waits``)."""
    effects: list[Effect] = [action.effect for action in described]
    if not effects:
        effect: Effect = (
            "permissions"
            if any(action_type.startswith(_PERMISSION_TYPES) for action_type in planned_types)
            else "message"
        )
    else:
        effect = "permissions" if "permissions" in effects else effects[0]
    destination: Destination = (
        "external" if any(a.destination == "external" for a in described) else "household"
    )
    return DescribedAction(
        connector="core",
        action_type=CHAIN_ACTION_TYPE,
        target=None,
        effect=effect,
        destination=destination,
        facts={"steps": count, "checked_in_turn": checked, "waits": waits},
    )


def _approval(needs: Sequence[NeedsApproval]) -> NeedsApproval:
    """The agreement a chain needs: the most demanding approver its steps and itself need (the
    owner, then an adult), with the first question a limit asked."""
    approver: Literal["adult", "owner"] = (
        "owner" if any(need.approver == "owner" for need in needs) else "adult"
    )
    return NeedsApproval(approver, next((need.question for need in needs if need.question), None))


def _not_at_all(steps: Sequence[PlannedStep]) -> NeverAsk | None:
    """The owner's "not at all" for a chain: when he said it for every one of its ``steps``."""
    if steps and all(step.never_ask is not None and step.never_ask.at_all for step in steps):
        return NeverAsk(None, at_all=True)
    return None


def _basis_of(actor: Actor, rights: RightsTable) -> ChainBasis:
    """What a chain remembers of ``actor`` as they are now (``ChainBasis``): their role, and the
    ids of the rights in force given to them themselves (``rights``)."""
    return ChainBasis(
        actor.role,
        frozenset(right.id for right in rights.rights if right.person_id == actor.person_id),
    )


def _shown_to(actor: Actor, ctx: ToolContext, rights: RightsTable) -> frozenset[str]:
    """The names of the tools ``actor`` is shown, under ``rights`` (``tools_for``)."""
    return frozenset(
        definition.name
        for definition in tools_for(
            actor.role, ctx.registry, ctx.policy, person_id=actor.person_id, rights=rights
        )
    )


def _also_domo(step: _Step, ctx: ToolContext) -> bool:
    """Whether ``step``, asked in a person's turn, is Domo's own act too (a note in his name,
    ``{"from": "me"}``): what its described action says (``DescribedAction.also_domo``), read
    from its arguments for a step that cannot be described yet (it names a newcomer)."""
    as_domo = getattr(step.tool, "as_domo", None)
    return not ctx.on_his_own and callable(as_domo) and as_domo(step.arguments) is True


async def _acts_on_an_earlier_one(
    step: _Step, touched_so_far: AbstractSet[str], ctx: ToolContext
) -> bool:
    """Whether ``step`` acts on whom or what a step before it touches (``touched_so_far``)."""
    if not isinstance(step.tool, ActsOn):
        return False
    whom = await step.tool.acts_on(step.arguments, ctx)
    return whom is not None and whom in touched_so_far


class DoInOrder:
    """Do several tools in order, in one request with one yes (see the module docstring)."""

    definition = ToolDefinition(
        name="do_in_order",
        description=(
            f"Do {MIN_STEPS} to {MAX_STEPS} of your tools in order, in one request with one yes, "
            "only when the steps depend on each other: one changes what the next one needs "
            '("hand my role to Justine, then remove me"). For actions that do not depend on each '
            "other, call the tools one by one, as usual. Give each step as you would call its "
            "tool; a do_in_order given as a step is flattened (its steps take its place, and "
            f"count in the {MAX_STEPS}). For more steps, make several chains, one after the "
            "other. chain_continue, the confirmation_* tools and the tools nobody sees you use "
            "(your memory, your plans, the name someone calls you, your look at what reached "
            "someone) are never steps: call them "
            "before or after the chain, in the same answer. A step that cannot be checked now, "
            "because an earlier step changes what it touches, is checked in its turn. A step may "
            f"wait for someone to join the household ({WAITS_FOR}: the name of their invitation, "
            "made by an earlier step or already waiting): the chain pauses there and goes on by "
            "itself once they have joined, and the steps after it may name them. A chain is "
            "judged once, as a whole; the result says whether it ran, waits for a yes, or was "
            "not done. If a step fails, the chain stops, and you decide what is best."
        ),
        parameters={
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "description": "The steps, in the order they are to be done.",
                    "minItems": MIN_STEPS,
                    "maxItems": MAX_STEPS,
                    "items": _STEP_SCHEMA,
                }
            },
            "required": ["steps"],
            "additionalProperties": False,
        },
    )
    action_type = CHAIN_ACTION_TYPE
    # Each step will run under its own limit (``conversation.execution.execute_allowed`` puts none
    # around the chain).
    bounds_its_own_steps: Final = True

    async def plan(
        self,
        arguments: JsonObject,
        actor: Actor,
        ctx: ToolContext,
        *,
        done: Sequence[DescribedAction] = (),
        kept: Sequence[bool] | None = None,
        basis: ChainBasis | None = None,
        speaker: uuid.UUID | None = None,
    ) -> ChainPlan:
        """The chain as planned for ``actor`` now (see the module docstring). Raises
        ``InvalidArguments``. It reads only.

        Each step keeps the owner's "never ask me" for it (``PlannedStep.never_ask``), for the
        judge; the plan says whose agreement the chain needs and what the owner's rules ask
        (``ChainPlan.rule``).

        A chain that waits and is changed (``confirmations.service.amend_confirmation``) is
        planned again: ``arguments`` are then its steps still to do, ``done`` the actions of its
        steps already done, in order (they count in what the steps before touch, and in the
        numbers of the steps and the twenty), and ``basis`` what the chain remembers of the one who
        asked. ``kept`` says, for each step, whether it is kept as it was asked for: such a step is
        judged under the rule of the chain (``decide_in_chain``, with ``basis`` and ``done``; one
        checked in its turn with the access of the person as the chain judges them,
        ``judged_actor``), a step changed or added with the role of now (``decide_now``: spec 4.1,
        last point), and each step keeps that (``PlannedStep.with_basis``). Without ``kept``, a
        new chain: every step is judged with the role of now, and keeps the rule of the chain for
        its run.

        The people the steps name are given their whole names as ``speaker`` names them: the
        person whose words the steps are (an approver who changes the chain, whose nicknames
        count, ``household.service.resolve_person``), the person of ``ctx`` by default."""
        async with ctx.session_factory() as session:
            rights = await load_rights(session)
        shown = _shown_to(actor, ctx, rights)
        if basis is not None:
            # A chain planned again may hold the tools the person was shown as the chain
            # remembers them: the barrier judges each step below, with the role the chain
            # remembers for a step kept as it was asked for, and with the role of now for any
            # other (which refuses it, with its number, if that role may not do it).
            shown |= _shown_to(judged_actor(actor, basis, done), ctx, rights)
        before = len(done)
        steps = await _with_whole_names(
            _steps_from(arguments, ctx.registry, shown, done=before),
            _invited_by(done),
            ctx,
            speaker=ctx.person.id if speaker is None else speaker,
            first=before + 1,
        )
        if kept is not None and len(kept) != len(steps):
            raise ValueError("a chain planned again says, for each of its steps, if it is kept")
        remembered = _basis_of(actor, rights) if basis is None else basis
        planned: list[PlannedStep] = []
        described: list[DescribedAction] = []
        # Whom and what the steps before touch (those done included): a described step touches
        # its target and the people of its facts, a step checked in its turn whom its tool says it
        # will touch.
        touched_so_far: set[str] = set()
        for action in done:
            touched_so_far |= touched(action)
        invited = _invited_by(done)
        # What the barrier said of each step, and of the chain itself (what the owner's rules ask
        # comes with each), and the agreements they need.
        decided: list[Allow | NeedsApproval] = []
        needs: list[NeedsApproval] = []
        checked = 0
        waits = 0

        def chain_action() -> DescribedAction:
            return _chain_action(
                before + len(steps),
                checked,
                described,
                [step.action_type for step in planned],
                waits,
            )

        def refused(decision: Refuse, number: int | None) -> ChainPlan:
            return ChainPlan(
                action=chain_action(),
                decision=decision,
                steps=tuple(planned),
                basis=remembered,
                refused_step=number,
                arguments=_arguments_of(steps),
                rule=None,
                waits=waits,
            )

        # From the first step that waits for an arrival on, the newcomer may be named.
        after_a_wait = False
        for index, step in enumerate(steps):
            number = before + index + 1
            if step.after is not None:
                if not await _names_an_invitation(step.after, steps[:index], invited, ctx):
                    raise InvalidArguments("invalid", f"step {number}: {_NO_INVITATION}")
                # A wait asks for nothing by itself: the chain keeps a request for the upkeep.
                after_a_wait = True
                waits += 1
            with_basis = True if kept is None else kept[index]
            # A step kept as it was asked for is judged as the chain judges the person, under the
            # rule of the chain (``in_chain``: what it remembers of them); any other with the role
            # of now.
            in_chain = basis if kept is not None and with_basis else None
            judged = actor if in_chain is None else judged_actor(actor, in_chain, done)
            try:
                action = await step.tool.describe(
                    step.arguments, judged, ctx if in_chain is None else replace(ctx, actor=judged)
                )
            except InvalidArguments as error:
                # After a wait, the step may name the newcomer, who does not exist yet: it is
                # checked in its turn, whatever the steps before touch.
                if not after_a_wait and not await _acts_on_an_earlier_one(
                    step, touched_so_far, ctx
                ):
                    raise InvalidArguments(
                        error.code, f"step {number}: {error.detail or error.code}"
                    ) from None
                # Checked in its turn: the person's access now (as the chain judges them, for a
                # step kept as it was asked for), and whose agreement it needs (the barrier's
                # rule, ``approval_needed``); its limit is read in its turn.
                action_type = step.tool.action_type
                resolved = rights.resolve(judged, action_type, ctx.policy)
                checked += 1
                touched_so_far |= await touched_by(step.tool, step.arguments, actor, ctx)
                planned.append(
                    PlannedStep(
                        step.name,
                        action_type,
                        step.arguments,
                        None,
                        with_basis=with_basis,
                        after_arrival_of=step.after,
                        never_ask=resolved.never_ask,
                        rule=resolved.ask,
                    )
                )
                if not resolved.allowed:
                    return refused(Refuse("not_allowed"), number)
                need = approval_needed(judged, resolved, action_type)
                checked_here: Allow | NeedsApproval = need or Allow(
                    resolved.ask, resolved.never_ask
                )
                if _also_domo(step, ctx):
                    # His own act too (a note in his name): judged for him as well, as the
                    # described step is (``decide_now``), the stricter standing (ruling R34).
                    his = rights.resolve(domo_actor(), action_type, ctx.policy)
                    his_side: Decision = (
                        Refuse("not_allowed")
                        if not his.allowed
                        else approval_needed(domo_actor(), his, action_type)
                        or Allow(his.ask, his.never_ask)
                    )
                    combined = stricter(checked_here, his_side)
                    if isinstance(combined, Refuse):
                        return refused(combined, number)
                    checked_here = combined
                    need = combined if isinstance(combined, NeedsApproval) else None
                if need is not None:
                    needs.append(need)
                decided.append(checked_here)
                continue
            if isinstance(step.tool, NeverAStep):
                # A call that is never a step (an opening that takes the owner's yes, ruling R19):
                # no yes of the chain, nor the judge's, gives it.
                why = await step.tool.never_a_step(step.arguments, ctx)
                if why is not None:
                    raise InvalidArguments("invalid", f"step {number}: {why}")
            async with ctx.session_factory() as session:
                if in_chain is not None:
                    _, decision = await decide_in_chain(
                        session, actor, action, ctx.policy, ctx.clock(), basis=in_chain, done=done
                    )
                else:
                    decision = await decide_now(session, actor, action, ctx.policy, ctx.clock())
            described.append(action)
            touched_so_far |= touched(action)
            planned.append(
                PlannedStep(
                    step.name,
                    action.action_type,
                    step.arguments,
                    action,
                    with_basis=with_basis,
                    after_arrival_of=step.after,
                    never_ask=None if isinstance(decision, Refuse) else decision.never_ask,
                    rule=None if isinstance(decision, Refuse) else decision.rule,
                )
            )
            match decision:
                case Refuse():
                    return refused(decision, number)
                case NeedsApproval():
                    needs.append(decision)
                    decided.append(decision)
                case Allow():
                    decided.append(decision)
        # The chain itself, last: its facts (how many steps are checked in their turn, how many
        # wait) are known only now, and a limit of the owner on chains reads them. The agreement it
        # needs counts, and so does what a rule of the owner on chains asks.
        chain = chain_action()
        async with ctx.session_factory() as session:
            own = await decide_now(session, actor, chain, ctx.policy, ctx.clock())
        match own:
            case Refuse():
                return refused(own, None)
            case NeedsApproval():
                needs.append(own)
                decided.append(own)
            case Allow():
                decided.append(own)
        rules: list[Ask | None] = [told.rule for told in decided]
        return ChainPlan(
            action=chain,
            decision=_approval(needs) if needs else Allow(),
            steps=tuple(planned),
            basis=remembered,
            refused_step=None,
            arguments=_arguments_of(steps),
            rule=most_demanding(*rules),
            waits=waits,
            never_ask=_not_at_all(planned),
            chain_rule=own.rule,
        )

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        return (await self.plan(arguments, actor, ctx)).action

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        """Run the chain (see the module docstring): at once, its steps from ``arguments``; or,
        after the yes to its request, from their rows. ``ValueError`` when what a request
        remembers does not read back, or nobody is named as the one who said yes: it is never
        guessed."""
        if ctx.request_id is None:
            steps = await _steps_now(arguments, ctx)
            run = await run_steps(ctx, steps, basis=None, request_id=None, decider=None)
            outcome = chain_outcome(run, total=len(steps), at_once=True)
            if run.asked is None:
                return outcome
            # A step needs someone's agreement: the rest waits for it, as a request.
            try:
                request_id = await _the_rest_waits(ctx, self.definition.name, steps, run.asked)
            except Exception as error:
                # What ran is told and counted all the same: the chain stops at that step, as
                # when nobody could be asked (its card lists the types of the steps done).
                logger.error(
                    "The rest of a chain that ran at once could not be stored (%s)",
                    type(error).__name__,
                )
                stopped = stopped_without_a_request(run)
                return chain_outcome(stopped, total=len(steps), at_once=True)
            told: JsonObject = {
                **outcome.model_result,
                "request_id": str(request_id),
                "steps": [step.told() for step in run.done],
            }
            return replace(outcome, model_result=told, confirmation_id=request_id)
        async with ctx.session_factory() as session:
            c = await _request(session, ctx.request_id)
            steps = await stored_steps(session, ctx.request_id)
            decider = None if ctx.approved_by is None else await _person(session, ctx.approved_by)
        if c is None or decider is None:
            raise ValueError("a chain runs after the yes to its request, from the one who gave it")
        basis = ChainBasis.from_json(c.chain_basis)
        run = await run_steps(ctx, steps, basis=basis, request_id=ctx.request_id, decider=decider)
        return chain_outcome(run, total=len(steps), at_once=False)


async def _steps_now(arguments: JsonObject, ctx: ToolContext) -> list[PlannedStep]:
    """The steps of a chain that runs at once, from its arguments, each still one of the tools the
    person is shown now (``_steps_from``); each is described when its turn comes."""
    async with ctx.session_factory() as session:
        rights = await load_rights(session)
    return [
        PlannedStep(
            step.name, step.tool.action_type, step.arguments, None, after_arrival_of=step.after
        )
        for step in _steps_from(arguments, ctx.registry, _shown_to(ctx.actor, ctx, rights))
    ]


async def _described_later(
    ctx: ToolContext, steps: Sequence[PlannedStep], asked: RestAsked
) -> list[DescribedAction | None]:
    """The steps after the one that waits (``asked``) of a chain that ran at once, each described
    now for the person as the barrier judged them, as ``DoInOrder.plan`` describes the steps of a
    chain asked for: the approver sees their targets, and each must still be that target when
    its turn comes. A step is left to be checked in its turn (``None``) when ``plan`` would leave
    it so: from the first wait for an arrival on, or when it acts on whom a step before it touches
    (those done and the one that waits included); and when its tool cannot describe it now or
    takes more than ``_PLAN_SECONDS`` (its turn checks it again: nothing is guessed)."""
    judged_ctx = replace(ctx, actor=asked.judged)
    touched_so_far: set[str] = set(touched(asked.action))
    for _, action, _ in asked.done:
        touched_so_far |= touched(action)
    after_a_wait = False
    found: list[DescribedAction | None] = []
    for step in steps[asked.position + 1 :]:
        tool = ctx.registry.get(step.tool_name)
        if tool is None:
            found.append(None)
            continue
        after_a_wait = after_a_wait or step.after_arrival_of is not None
        as_given = _Step(tool, step.tool_name, step.arguments, step.after_arrival_of)
        action: DescribedAction | None = None
        if not after_a_wait and not await _acts_on_an_earlier_one(as_given, touched_so_far, ctx):
            with anyio.move_on_after(_PLAN_SECONDS):
                try:
                    action = await tool.describe(step.arguments, asked.judged, judged_ctx)
                except InvalidArguments:
                    action = None
            if action is not None and action.action_type != step.action_type:
                action = None
        if action is None:
            touched_so_far |= await touched_by(tool, step.arguments, asked.judged, ctx)
        else:
            touched_so_far |= touched(action)
        found.append(action)
    return found


async def _the_rest_waits(
    ctx: ToolContext, tool_name: str, steps: Sequence[PlannedStep], asked: RestAsked
) -> uuid.UUID:
    """The request for the rest of a chain that ran at once (its call ``tool_name``, its
    ``steps``) and met a step that needs someone's agreement (``asked``, JDG-9), stored in one
    transaction within ``_STORE_SECONDS`` (``store.create_confirmation``, ``steps.add_steps``), as
    a request of the chain's call would be: its requester (the person of the turn, whose
    conversation it waits in, made by the reply of the call: ``ToolContext.domo_message_id``), the
    chain's call with all its steps, its own action (the facts of the steps as they stand now:
    the one that waits is described, and those after it as ``_described_later`` finds them), what
    it remembers
    of the one who asked as they are now (``ChainBasis``), and Domo's when the turn is his own.
    It waits for the barrier's approver for that step, never a person concerned (ruling R34),
    asking what the owner's rule on it asks, else a plain yes of the approver, with the question a
    limit asked. Its steps: those done, ``done`` with the action and the card they ran with, then
    the rest; each is judged again in its turn with the role of now, as the chain judged it
    (``with_basis`` is off). The barrier's decision on the step is audited, last. Raises when it
    cannot be stored: the step did not run.

    A step that became one a judge must weigh first (``RestAsked.plain_yes``: the last key)
    waits for a plain yes, the judge's (``Asked("yes", "judge")``), of the approver the barrier
    asks for it, else of the person who asked (``self``), or of the owner for a chain of his own
    (his own yes is nobody's)."""
    need = asked.need
    done = {position: (action, card) for position, action, card in asked.done}
    later = await _described_later(ctx, steps, asked)
    actions: list[DescribedAction | None] = [
        done[position][0]
        if position in done
        else asked.action
        if position == asked.position
        else later[position - asked.position - 1]
        for position in range(len(steps))
    ]
    planned = [
        replace(step, action=action, with_basis=False)
        for step, action in zip(steps, actions, strict=True)
    ]
    rest = planned[asked.position :]
    action = _chain_action(
        len(steps),
        sum(1 for step in rest if step.action is None),
        [step.action for step in rest if step.action is not None],
        [step.action_type for step in rest],
        sum(1 for step in steps if step.after_arrival_of is not None),
    )
    if isinstance(need, NeedsApproval):
        approver: Approver = need.approver
        question = need.question
    else:
        # His own yes is nobody's: a chain of his own waits for the owner, whose conversation it
        # is (as ``execution._waits`` places it).
        approver, question = ("owner" if ctx.on_his_own else "self"), None
    asking = (
        Asked(need.rule, "rule")
        if need.rule is not None
        else Asked("yes", "judge" if asked.plain_yes else "approver")
    )
    with anyio.fail_after(_STORE_SECONDS):
        async with ctx.session_factory() as session:
            now = ctx.clock()
            rights = await load_rights(session)
            c = await create_confirmation(
                session,
                requester=ctx.actor,
                tool_name=tool_name,
                arguments={
                    "steps": [
                        as_call(step.tool_name, step.arguments, step.after_arrival_of)
                        for step in steps
                    ]
                },
                action=action,
                approver=approver,
                asked=asking,
                conversation_id=ctx.conversation_id,
                message_id=ctx.domo_message_id,
                now=now,
                question=question,
                chain_basis=_basis_of(asked.judged, rights),
                by_domo=ctx.on_his_own,
            )
            await add_steps(session, c.id, planned)
            for position, (_, card) in done.items():
                await mark_step(session, c.id, position, status="done", result=card, now=now)
            # Last, just before the commit.
            await record_decision(
                session,
                asked.judged,
                asked.action,
                need,
                tool_name=steps[asked.position].tool_name,
            )
            await session.commit()
            return c.id


async def _request(
    session: AsyncSession, request_id: uuid.UUID, *, lock: bool = False
) -> Confirmation | None:
    """The request, as the database holds it now (locked for a change when ``lock``)."""
    query = select(Confirmation).where(Confirmation.id == request_id)
    if lock:
        query = query.with_for_update()
    found = await session.execute(query.execution_options(populate_existing=True))
    return found.scalar_one_or_none()


async def _person(session: AsyncSession, person_id: uuid.UUID) -> Person | None:
    """The person, as the database holds them now."""
    found = await session.execute(
        select(Person).where(Person.id == person_id).execution_options(populate_existing=True)
    )
    return found.scalar_one_or_none()


# --- continuing a chain that stopped --------------------------------------------------------------

_CONTINUE_ARGUMENTS: Final = frozenset({"request_id", "steps"})
# What Domo is told when an id names no chain of the person that stopped in this conversation
# and may be taken up again (it never learns whether the id exists).
_NO_STOPPED_CHAIN: Final = (
    "no chain of this person that stopped and may be taken up again has this id in this "
    "conversation, or it expired"
)
# How long planning the rest of a chain may take (as for a change of a chain that waits,
# ``confirmations.service``): the tool bounds its own steps.
_PLAN_SECONDS: Final = 60.0
# How long storing the request for the rest of a chain that ran at once may take.
_STORE_SECONDS: Final = 15.0
# What the model reads of the rest of a chain that waits for a new yes, or that the judge did not
# let go on.
_WAITS_FOR_A_NEW_YES: Final = "waiting_for_confirmation"
_NOT_CONTINUED: Final = "not_continued"
# The ways of saying yes, as a request stores them (``confirmations.service.DecidedVia``).
_VIAS: Final[tuple[DecidedVia, ...]] = (
    "button",
    "explicit_phrase",
    "phrase",
    "classifier",
    "change",
    "proof",
    "judge",
)


def _continue_arguments(arguments: JsonObject) -> tuple[uuid.UUID, list[JsonObject] | None]:
    """The ``request_id`` of a call of ``chain_continue`` and, when the rest changes, its
    ``steps``: the calls Domo gives for the steps left (``replan.calls_of``), else ``None``."""
    if "request_id" not in arguments or not frozenset(arguments) <= _CONTINUE_ARGUMENTS:
        raise InvalidArguments(
            "invalid", "the arguments are request_id and, when the rest changes, steps"
        )
    value = arguments["request_id"]
    if not isinstance(value, str):
        raise InvalidArguments("invalid", "request_id is the id of the chain's request, as a text")
    try:
        request_id = uuid.UUID(value)
    except ValueError:
        raise InvalidArguments("invalid", "request_id is the id of the chain's request") from None
    if "steps" not in arguments:
        return request_id, None
    return request_id, calls_of(arguments["steps"])


def via_of(c: Confirmation) -> DecidedVia:
    """How the yes to the request was given, as stored. ``ValueError`` for anything else: it is
    never guessed."""
    for via in _VIAS:
        if via == c.decided_via:
            return via
    raise ValueError("the yes to this chain is not recorded")


def _may_continue(
    c: Confirmation | None,
    rows: Sequence[ConfirmationStep],
    ctx: ToolContext,
    lifetime_over: bool,
) -> bool:
    """Whether ``c`` is a chain of the person of ``ctx``, in this conversation, that may be taken
    up again (``confirmations.chain.can_be_continued``), under its lifetime."""
    if c is None or c.chain_basis is None or lifetime_over:
        return False
    return (
        c.requester_id == ctx.person.id
        and c.conversation_id == ctx.conversation_id
        and can_be_continued(c, rows)
    )


async def the_one_who_said_yes(
    session: AsyncSession, c: Confirmation, requester: Person, now: datetime
) -> Person:
    """Whose yes the rest of the chain ``c`` runs under (a chain continued, or resumed after an
    arrival: ``confirmations.arrivals``): the one who gave it, as the database holds them now, or
    the person who asked when that one is no longer in the household (a yes of the person's own
    covers less, ``confirmations.store.covers``)."""
    decider = None if c.decided_by is None else await _person(session, c.decided_by)
    return decider if decider is not None and is_active(decider, now) else requester


@dataclass(frozen=True)
class UnderTheSameYes:
    """The rest of a chain, ``executing`` again, that runs under the yes already given (a chain
    continued, or resumed after an arrival): its ``steps`` (all of them: their rows say which are
    left), what it remembers of the one who asked, whose yes it runs under and how that yes was
    given."""

    steps: list[PlannedStep]
    basis: ChainBasis
    decider: Person
    via: DecidedVia
    tool_name: str


async def _under_the_same_yes(
    session: AsyncSession,
    c: Confirmation,
    rows: Sequence[ConfirmationStep],
    failed: ConfirmationStep | None,
    ctx: ToolContext,
    now: datetime,
    *,
    decider: Person,
    plan: ChainPlan,
    judged: Judgement,
    changed: list[JsonObject] | None,
) -> UnderTheSameYes:
    """The chain ``c`` (locked) taken up again under the yes already given, whose yes is
    ``decider``'s (``the_one_who_said_yes``), once the judge let its rest as planned now
    (``plan``) go on (``judged``): as it is (``changed`` is ``None``), the step that failed
    ``stopped`` again (it runs first, judged again in its turn); or changed (``changed``, the calls
    Domo gave for the rest), the steps of the plan replacing the rows left, after the steps
    done, while its whole call and its action become those of the change (``amended_arguments``,
    ``action``: its card says what it was before). The request is ``executing`` again; the judge's
    verdict and ``chain.continued`` (how many steps are left) are audited, last; committed."""
    done = [row for row in rows if row.status == "done"]
    if changed is not None:
        for row in rows:
            if row.status in STEPS_LEFT:
                await session.delete(row)
        await session.flush()
        await add_steps(session, c.id, plan.steps, start=len(done))
        c.amended_arguments = {"steps": [*(call_of(row) for row in done), *plan.calls()]}
        c.action = dict(plan.action.as_json())
    elif failed is not None:
        await mark_step(session, c.id, failed.position, status="stopped", result=None, now=now)
    c.status = "executing"
    await session.flush()
    taken = UnderTheSameYes(
        steps=[planned_of(row) for row in await steps_of(session, c.id)],
        basis=ChainBasis.from_json(c.chain_basis),
        decider=decider,
        via=via_of(c),
        tool_name=c.tool_name,
    )
    # Last, just before the commit.
    await audit_judgement(
        session, actor=ctx.actor, tool_name=c.tool_name, action=plan.action, judgement=judged
    )
    await append_audit(
        session,
        actor=f"person:{ctx.person.id}",
        action="chain.continued",
        details={"confirmation_id": str(c.id), "steps": len(plan.steps)},
    )
    await session.commit()
    return taken


async def run_under_the_same_yes(
    ctx: ToolContext, request_id: uuid.UUID, taken: UnderTheSameYes
) -> tuple[ToolOutcome, ActionRecord]:
    """Run the rest of the chain ``request_id`` (``executing``) under the yes already given, as
    after a yes (with the yes of the one who gave it), in the context ``ctx`` of the one who asked,
    and settle what came of it as for a yes (``confirmations.service.settle``), unless the chain
    waits again: for someone else's yes, or for an arrival. What came of it, as the tool tells it
    (its card is the request's) and as its record."""
    decider = taken.decider
    run_ctx = replace(ctx, approved_by=decider.id, request_id=request_id, described_action=None)
    try:
        run = await run_steps(
            run_ctx, taken.steps, basis=taken.basis, request_id=request_id, decider=decider
        )
        outcome = chain_outcome(run, total=len(taken.steps), at_once=False)
    except Exception as error:
        # The request is settled all the same: a step whose record is not known stays as it
        # was, and the chain can no longer be continued.
        logger.error("Continuing a chain failed (%s)", type(error).__name__)
        outcome = ToolOutcome("failed", {}, {"key": "action.card.failed", "params": {}})
    record = ActionRecord(
        tool_name=taken.tool_name,
        action_type=CHAIN_ACTION_TYPE,
        status=outcome.status,
        confirmation_id=request_id,
        model_result={"status": outcome.status, **outcome.model_result},
        card=outcome.card,
    )
    if not waits_again(record):
        # (A chain that waits again, for someone else's yes or for an arrival, is not over:
        # nothing to settle.)
        await settle(
            ctx.session_factory,
            request_id,
            record,
            decider,
            outcome="executed" if outcome.status == "done" else "failed",
            via=taken.via,
            now=ctx.clock(),
        )
    return replace(outcome, confirmation_id=request_id), record


async def _planned(
    calls: Sequence[JsonObject],
    ctx: ToolContext,
    *,
    done: Sequence[DescribedAction],
    kept: Sequence[bool],
    basis: ChainBasis,
) -> ChainPlan | None:
    """The steps ``calls`` of a chain planned again for the person of now (``DoInOrder.plan``),
    after the actions ``done``, each step ``kept`` judged under the rule of the chain (``basis``);
    ``None`` when it takes more than ``_PLAN_SECONDS``. Raises ``InvalidArguments``."""
    with anyio.move_on_after(_PLAN_SECONDS):
        return await DoInOrder().plan(
            {"steps": list(calls)}, ctx.actor, ctx, done=done, kept=kept, basis=basis
        )
    return None


async def _the_rest_planned(
    session: AsyncSession,
    c: Confirmation,
    rows: Sequence[ConfirmationStep],
    failed: ConfirmationStep | None,
    changed: list[JsonObject] | None,
    ctx: ToolContext,
    now: datetime,
    *,
    raised: tuple[Ask, str] | None,
) -> UnderTheSameYes | ToolOutcome:
    """The rest of the chain ``c`` (locked) planned again for the person of now and judged again
    (spec 4.4, 4.7). The steps left as they are (``changed`` is ``None``), or the calls Domo
    gives for them (``changed``), matched with the steps left (``replan.match_steps``: kept,
    changed or added), are planned after the steps done (``DoInOrder.plan``: a step kept as it
    was asked for, by the person the chain remembers, is judged under the rule of the chain, any
    other with the role of now). The plan's ``InvalidArguments`` come back as they are, and a step
    the barrier refuses refuses it (``invalid``, with its number); nothing changes then.

    Then the yes-judge judges the rest (its steps told one by one), with the request's history
    (``confirmations.chain.request_told``: its yes, its steps done, the step that failed, why and
    whether it may have acted, and what changed, nothing when the rest is as it was). Its
    ``do_not`` leaves the chain as it is (``"chain": "not_continued"``, with the judge's reason).
    What it asks then is ``judge.ask_after_a_change`` with the yes already given (it covers what
    it was given to: the owner's rules count for the steps changed or
    added, and for a step kept only when they ask more than that yes gave,
    ``service.what_the_yes_gave``) and what Domo ``raised``. When nothing is asked and the yes
    given still covers the accord the rest needs (the barrier's now, never copied:
    ``store.covers``, from the one who said yes, ``the_one_who_said_yes``), the rest goes on under
    that yes (``_under_the_same_yes``).

    Otherwise it is a new request that replaces ``c`` (``replan.replace_chain_request``: its steps
    done copied, then the rest as planned, the step that failed and may have acted, given as it was
    asked for, marked as one that may already have been done, ``MAYBE_DONE``), presented by this
    turn's reply. It waits for the agreement the plan needs (the person's own yes when it needs
    none: it is never copied from ``c``), and asks what was found (a plain yes of whoever answers
    it when nothing was: the accord alone). What Domo reads says whose yes it waits for, how it
    is given, who asked it and why. The judge's verdict and ``chain.continued`` are audited, then
    committed."""
    done = [row for row in rows if row.status == "done"]
    left = [row for row in rows if row.status in STEPS_LEFT]
    calls = [call_of(row) for row in left] if changed is None else changed
    matches = match_steps(left, calls)
    with_basis = {row.position: row.with_basis for row in left}
    kept = [
        match.kind == "kept" and match.old_position is not None and with_basis[match.old_position]
        for match in matches
    ]
    # The step that failed and may have acted, given again as it was asked for: a new request
    # says it may already have been done.
    maybe_at: int | None = None
    if failed is not None and failed_step_may_have_acted(failed):
        maybe_at = next(
            (
                index
                for index, match in enumerate(matches)
                if match.kind == "kept" and match.old_position == failed.position
            ),
            None,
        )
    done_actions = [DescribedAction.from_json(row.action) for row in done if row.action is not None]
    basis = ChainBasis.from_json(c.chain_basis)
    plan = await _planned(calls, ctx, done=done_actions, kept=kept, basis=basis)
    if plan is None:
        raise TimeoutError("planning the rest of the chain took too long: nothing was done")
    decision = plan.decision
    if isinstance(decision, Refuse):
        refused = f"refused ({decision.reason})"
        raise InvalidArguments(
            "invalid",
            refused if plan.refused_step is None else f"step {plan.refused_step}: {refused}",
        )

    # The judge, with the request's history (nothing changes when the rest is as it was).
    as_it_was = changed is None or unchanged(left, calls)
    told = [planned_step_told(step) for step in plan.steps]
    request = await request_told(
        session,
        c,
        before=() if as_it_was else [step_sentence(row, REFERENCE) for row in left],
        after=() if as_it_was else [step.says for step in told],
        now=now,
    )
    judgement = await consult_the_judge(
        ctx,
        actor=judged_actor_of(DoInOrder(), ctx),
        action=plan.action,
        says=describe_chain(plan.arguments, REFERENCE),
        rights=rights_say(decision),
        steps=plan.steps,
        request=request,
        owner_rule=owner_rule_of(plan.never_ask),
    )
    if judgement.verdict == "do_not":
        # Last, just before the commit: the chain stays as it was.
        await audit_judgement(
            session, actor=ctx.actor, tool_name=c.tool_name, action=plan.action, judgement=judgement
        )
        await session.commit()
        reason = judgement.reason
        return ToolOutcome(
            "done",
            {
                "chain": _NOT_CONTINUED,
                "reason": "judge",
                "why": reason,
                "detail": JUDGE_REFUSED_DETAIL,
            },
            {"key": "action.card.refused.judge", "params": {"reason": reason}, "reason": "judge"},
            confirmation_id=c.id,
        )
    decider = await the_one_who_said_yes(session, c, ctx.person, now)
    covered = isinstance(decision, Allow) or covers(
        c.approver_kind, decision.approver, decider, c.requester_id
    )
    asked = ask_after_a_change(
        judgement,
        old_ask=asked_of(c),
        steps=plan.steps,
        new=[match.kind != "kept" for match in matches],
        raised=raised,
        never_ask=plan.never_ask,
        yes_already_given=True,
        yes_gave=what_the_yes_gave(via_of(c)),
    )
    if asked is None and covered:
        return await _under_the_same_yes(
            session,
            c,
            rows,
            failed,
            ctx,
            now,
            decider=decider,
            plan=plan,
            judged=judgement,
            changed=None if as_it_was else calls,
        )

    approver: Approver = decision.approver if isinstance(decision, NeedsApproval) else "self"
    new = await replace_chain_request(
        session,
        c,
        requester=ctx.actor,
        plan=plan,
        calls=[*(call_of(row) for row in done), *calls],
        done=done,
        approver=approver,
        asked=asked or Asked("yes", "approver", None),
        question=decision.question if isinstance(decision, NeedsApproval) else None,
        message_id=ctx.domo_message_id,
        now=now,
    )
    if maybe_at is not None:
        await mark_maybe_done(session, new.id, len(done) + maybe_at, now=now)
    told_to_domo: JsonObject = {
        "chain": _WAITS_FOR_A_NEW_YES,
        "request_id": str(new.id),
        "by": WHO_SAYS_YES[approver],
        "answer_with": how_to_answer(new),
        "steps": len(calls),
        "maybe_done": maybe_at is not None,
        "asked_by": new.asked_by,
    }
    if new.ask_why is not None:
        told_to_domo["why"] = new.ask_why
    # Last, just before the commit.
    await audit_judgement(
        session, actor=ctx.actor, tool_name=c.tool_name, action=plan.action, judgement=judgement
    )
    await append_audit(
        session,
        actor=f"person:{ctx.person.id}",
        action="chain.continued",
        details={"confirmation_id": str(c.id), "replaced_by": str(new.id), "steps": len(calls)},
    )
    await session.commit()
    return ToolOutcome(
        "done",
        told_to_domo,
        {"key": f"action.card.waiting.{approver}", "params": {}},
        confirmation_id=new.id,
    )


class ChainContinue:
    """Take up a chain that stopped, from the step that failed (see the module docstring)."""

    definition = ToolDefinition(
        name=CONTINUE_TOOL,
        description=(
            "Take up a chain of actions that stopped on a failure, from the step that failed; "
            "what was done stays done. Without steps, the steps left are taken up as they are: "
            "the step that failed first, then those after it, each checked again in its turn. "
            "With steps (the steps left as you understood the person, without those already "
            "done), the rest changes. Your judge decides, with the chain's history, whether the "
            "rest runs under the yes already given or needs a new one (one request for all of the "
            "rest, which says when the step may already have been done), or is not taken up, and "
            "the result says which. When the step that failed may have acted (it took too long, "
            "or its tool failed), check first with your reading tools, and say so: if it was "
            "done, give the steps left without it. Use it only when "
            "doing the rest still makes sense; otherwise drop it, or undo what was done with new "
            "actions. Possible only while the request has not expired."
        ),
        parameters={
            "type": "object",
            "properties": {
                "request_id": {
                    "type": "string",
                    "description": "The id of the chain's request, as its result gave it.",
                },
                "steps": {
                    "type": "array",
                    "description": (
                        "Only when the rest changes: the steps left, in order, as you understood "
                        "the person, each as you would give it to do_in_order, without the steps "
                        "already done."
                    ),
                    "minItems": MIN_STEPS,
                    "maxItems": MAX_STEPS,
                    "items": _STEP_SCHEMA,
                },
            },
            "required": ["request_id"],
            "additionalProperties": False,
        },
    )
    action_type = CONTINUE_ACTION_TYPE
    # Each step runs under its own limit, and what came of it is recorded under one of its own
    # (``conversation.execution.execute_allowed`` puts none around the whole); planning the rest
    # has its own.
    bounds_its_own_steps: Final = True

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        request_id, _ = _continue_arguments(arguments)
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=f"confirmation:{request_id}",
            effect="own_requests",
            destination="household",
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        # What Domo asked more for the next action of his answer is for what this carries
        # (F15): this call uses it up, whatever comes of it.
        raised = ctx.take_ask_more()
        request_id, changed = _continue_arguments(arguments)
        now = ctx.clock()
        async with ctx.session_factory() as session:
            c = await _request(session, request_id, lock=True)
            rows = await steps_of(session, request_id)
            settings = await load_policy_settings(session)
            over = c is not None and is_past_lifetime(c, settings, now)
            if c is None or not _may_continue(c, rows, ctx, over):
                raise InvalidArguments("not_found", _NO_STOPPED_CHAIN)
            failed = next((row for row in rows if row.status == "failed"), None)
            taken = await _the_rest_planned(
                session, c, rows, failed, changed, ctx, now, raised=raised
            )
        match taken:
            case ToolOutcome():
                return taken
            case UnderTheSameYes():
                # The steps were asked for in an earlier turn: its message is not this one's.
                later = replace(ctx, said=None, said_later=())
                outcome, _ = await run_under_the_same_yes(later, request_id, taken)
                return outcome
