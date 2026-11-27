"""The policy barrier: the one function every action of Domo goes through.

``decide`` is pure. It looks at who asks (``Actor``), at the described action, at the tables of
the catalog, at the owner's rights in force and at the facts of the moment (``policy.facts``), and
answers with one of three decisions: ``Allow`` (they may do it), ``NeedsApproval`` (someone else
must agree first, and who) or ``Refuse``. Whether a yes is asked of the person themselves is not
the barrier's: the yes-judge decides it after the barrier (``confirmations.judge``), and the
owner's rules travel with the decision for it (``rule``, the owner's "always ask me before...";
``never_ask``, his "never ask me"). It reads no state and writes nothing, so the rules can be
tested exhaustively, and a caller cannot get a different answer by timing. The caller loads the
rights and the facts first (``policy.decision.decide_now`` does, and every decision of the core
goes through it).

The rules, in this order (spec 4.4, 5.8):

1. nobody known (``Actor.standing`` ``unknown``: no record, so no rights) is refused
   (``untrusted_identity``), whatever the action: they may be answered, never acted for (at a
   page, a voice nobody knows gets a short exchange with no tool, ``channels.voice.stranger``,
   which never reaches the barrier);
2. changing the rules, handing the owner's role over and the list of successors
   (``catalog.OWNER_ONLY_ACTIONS``) are the owner's: asked by anyone else they are refused
   (``not_allowed``), whatever the tables and the rights say. This is the foundation: no right
   given by the owner can move it;
3. access: the person's right, else their role's, else the catalog (``RightsTable.resolve``);
   for Domo (``roles.domo_actor``), his own right, else the catalog's ``PolicyTables.domo``.
   An action they may not do, or that the catalog does not know, is refused (``not_allowed``);
4. the limit, if one applies (the person's right's, else their role's, else the catalog's:
   ``catalog.DEFAULT_LIMITS``), evaluated on the facts of the moment (``policy.limits``):
   ``"refuse"`` refuses (``limit``, with the limit's summary, which Domo may tell the person);
   ``"ask_owner"`` needs the owner's agreement, always: the owner wrote it. ``"judge"`` needs it
   too, but is left to the yes-judge first (``NeedsApproval.judge_first``): when the judge knows
   the answer to the right's question, the action is done without the owner; when it doubts, the
   owner is asked, with the question, as for ``"ask_owner"`` (a limit that also needs someone's
   agreement by rule 5 is never left to the judge: it goes to the owner). The owner's own request
   goes on (asking is the owner's agreement); ``"allow"`` goes on. A
   limit that cannot answer (it does not compile, it fails, it answers something else, or no facts
   were given) refuses (``limit_error``): a limit fails closed;
5. whose agreement is needed (``approval_needed``, which the plan of a chain uses too): an
   adult's when a right says so, the owner's when a right says so (never for the owner's own
   request), and, when no right names anyone, an adult's for a child on the types of
   ``catalog.ADULT_AGREES_FOR_A_CHILD`` (a right that opens such a type to a child keeps the adult,
   unless it says the child agrees for themselves, ``self``); otherwise the action is allowed.
   An adult's agreement for a child that no right asks for (the catalog's default) is left to the
   yes-judge first too (``judge_first``): an adult's yes already heard for this very request lets
   it through; otherwise the adult is asked. An approver a right names is the owner's word: never
   left to the judge;
6. who speaks, when the code is not sure of them (``Actor.standing`` ``unsure``: a probable
   person nothing sure confirms; or a voice too short to tell, ``Actor.voice_too_short``, for what
   is sensitive only): never refused out of hand. They act with the rights of the person who
   probably speaks, all of the above applied as for anyone (a refusal stays a refusal). What is
   ordinary (``may_act_weakly``, with the risks a launch's operations declare) is decided exactly
   as for someone the code is sure of, unless the owner said to judge it too
   (``ordinary_when_unsure`` ``"judge"``, ``settings.VOICE_UNSURE_KEY``); what is sensitive is
   marked ``judge_identity``: the caller has the yes-judge weigh it with the clues of who speaks
   (do it, ask a yes, ask a proof, or not), and a judge that does not answer asks a proof
   (``conversation.execution``). Someone heard at a page signed in as another person
   (``Actor.page_person_id``) has nothing ordinary: every action is marked ``judge_identity``,
   and ``policy.decision.decide_now`` also decides it for the page's person, the stricter of the
   two winning. A launch of a tool Domo made is ordinary under doubt only when its operations
   declare acting physically alone, each such act declared harmless (a light, never a lock), and
   the tool's own means cannot do more
   (``madetools.launch.reach_of``). What the rights or the owner's rules ask is never lowered.

Acknowledged divergences from spec section 10 (decided for 1b):

* spec 10.3 escalates an action of an untrusted identity to a confirmation by the owner on a
  trusted channel. Since the voice (its spec 6.5), someone the code is not sure of is judged
  instead (rule 6), and nobody known may not act (rule 1); nobody's agreement is asked for them,
  though the voice's design lets Domo ask a trusted person: what a voice nobody knows asked
  reaches the page's person only as a word Domo leaves them (``channels.voice.stranger``);
* spec 10.4 asks for at least a confirmation for an action nobody has classified. 1b has only
  the core's own tools, all in the catalog, so an unknown action type is refused. Connector
  actions (1e) follow the spec;
* the barrier checks identity, permission and whose agreement is needed only. The emergency
  stop, the privacy and provenance checks and the budget of spec 10.2 are not there yet;
* a person who is no longer active (removed, or a guest whose stay ended) is not the barrier's
  business: the session and the confirmation service stop them before they get here.
"""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Final, Literal

from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.audit.log import append_audit
from domo_core.policy.actions import DescribedAction
from domo_core.policy.asks import Ask, NeverAsk, most_demanding
from domo_core.policy.catalog import (
    DEFAULT_POLICY,
    OWNER_ONLY_ACTIONS,
    PROGRAM_REQUESTS,
    WEAKLY_FINE,
    PolicyTables,
    Risks,
    an_adult_agrees_for,
    is_made,
    rights_type,
)
from domo_core.policy.limits import (
    InvalidLimit,
    LimitError,
    LimitOutcome,
    compile_limit,
    evaluate_limit,
)
from domo_core.policy.rights import NO_RIGHTS, Limit, Resolved, RightsTable
from domo_core.policy.roles import Actor, Role, actor_label, domo_actor
from domo_core.policy.settings import OrdinaryWhenUnsure

# ``limit``: a limit refused it; ``limit_error``: a limit could not say (refused all the same).
RefusalReason = Literal["not_allowed", "untrusted_identity", "limit", "limit_error"]


@dataclass(frozen=True)
class Allow:
    """Do it; the judge decides the yes. ``rule`` is what the owner's rule asks before it, if one
    does; ``never_ask`` the owner's "never ask me for this", with his conditions
    (``Resolved.never_ask``). ``judge_identity``: the code is not sure of who asks, and the
    yes-judge must weigh it with the clues, whatever the type's usual judging (rule 6)."""

    rule: Ask | None = None
    never_ask: NeverAsk | None = None
    judge_identity: bool = False


@dataclass(frozen=True)
class NeedsApproval:
    """Someone else must agree first: an adult (a child's request on a type of
    ``ADULT_AGREES_FOR_A_CHILD``, or a right that says so) or the owner (a right, or a limit that
    sends to the owner); ``question`` is the right's, when a limit asked it; ``rule`` and
    ``never_ask`` as for ``Allow``. ``judge_first``: the yes-judge decides first, and its ``do``
    lets the action through without that agreement (a limit that says ``"judge"``, and an adult's
    agreement for a child that only the catalog asks for); any other verdict asks for it. Only
    for a single action: a chain's step asks for the agreement as it says. ``judge_identity`` as
    for ``Allow``."""

    approver: Literal["adult", "owner"]
    question: str | None = None
    rule: Ask | None = None
    never_ask: NeverAsk | None = None
    judge_first: bool = False
    judge_identity: bool = False


@dataclass(frozen=True)
class Refuse:
    """Do not do it, and do not ask. ``limit`` is the summary of the limit that refused it, when
    one did (``reason`` ``limit``). ``side`` is whose judgement refused an action judged twice
    (``stricter``): the person's or Domo's; ``None`` for an action judged once."""

    reason: RefusalReason
    limit: str | None = None
    side: Literal["person", "domo"] | None = None


Decision = Allow | NeedsApproval | Refuse

# Whose agreement is the more demanding, the last the most.
_APPROVERS: Final[tuple[str, ...]] = ("adult", "owner")


def stricter(person: Decision, domo: Decision) -> Decision:
    """One decision for an action judged twice (ruling R29: a note in Domo's own name, asked in
    a person's turn): ``person``, the barrier's for the person who asks, exactly as for any action
    of theirs, and ``domo``, its decision for Domo. A refusal of either refuses, saying whose
    (``Refuse.side``; the person's first). Else someone's agreement asked by either is asked, the
    more demanding of the two when both ask one (the owner's over an adult's), with its question.
    Else it is allowed. What the owner's rules ask goes with it: the more demanding of the two
    sides' (``asks.most_demanding``); the owner's "never ask me" only when Domo's side asks
    nothing. Who speaks is weighed by the judge when either side says so (``judge_identity``)."""
    if isinstance(person, Refuse):
        return replace(person, side="person")
    if isinstance(domo, Refuse):
        return replace(domo, side="domo")
    rule = most_demanding(person.rule, domo.rule)
    never_ask = person.never_ask if domo.rule is None else None
    judge_identity = person.judge_identity or domo.judge_identity
    asking = [d for d in (person, domo) if isinstance(d, NeedsApproval)]
    if not asking:
        return Allow(rule=rule, never_ask=never_ask, judge_identity=judge_identity)
    most = max(asking, key=lambda d: _APPROVERS.index(d.approver))
    # Left to the judge only when every side that asks leaves the same agreement to it.
    judge_first = all(d.judge_first and d.approver == most.approver for d in asking)
    return NeedsApproval(
        most.approver,
        question=most.question,
        rule=rule,
        never_ask=never_ask,
        judge_first=judge_first,
        judge_identity=judge_identity,
    )


def may_act_weakly(action_type: str, policy: PolicyTables, *, risks: Risks | None = None) -> bool:
    """Whether ``action_type`` is ordinary, so that someone the code is not sure of has it done as
    anyone (rule 6). A type of the code: when it is in ``catalog.WEAKLY_FINE`` (``risks`` is not
    read). A launch of a tool Domo made (``is_made``) and what its running program asks
    (``PROGRAM_REQUESTS``): only when ``risks`` are given (what its operations declare) and none
    of ``costs``, ``outward``, ``irreversible`` is set, nor acting in the house not declared
    harmless (``Risks.risky_physical``: the owner, 2026-11-27, "the physical that carries risks is
    judged, like unlocking the door; physical actions with no risk may pass with less certainty"):
    acting physically alone is ordinary only when every such act was declared harmless; risks that
    could not be read (``None``) are not. Anything else, a type nobody classified included, is
    sensitive. ``policy`` is the catalog the type is read in (a launch of a kind it does not know
    is sensitive)."""
    if is_made(action_type) or action_type in PROGRAM_REQUESTS:
        if is_made(action_type) and action_type not in policy.known:
            return False
        return risks is not None and not (
            risks.costs or risks.outward or risks.irreversible or risks.risky_physical
        )
    return action_type in WEAKLY_FINE


def given_for_an_idea(policy: PolicyTables, role: Role, action_type: str) -> PolicyTables:
    """``policy`` with ``action_type`` given by the catalog to ``role`` (ruling R26): the barrier
    decides so for the person of the turn when the assistant marked a call as his own idea, so
    that only a right not given is lifted. A right the owner wrote (a deny, an approver, a limit,
    "ask me first") and the rules of the barrier still count, and the stricter of that decision
    and the assistant's applies (``stricter``)."""
    given = policy.allowed.get(role, frozenset()) | {action_type, rights_type(action_type)}
    return replace(policy, allowed={**policy.allowed, role: given})


def approval_needed(actor: Actor, resolved: Resolved, action_type: str) -> NeedsApproval | None:
    """Whose agreement ``actor`` needs first for ``action_type``, as the rights resolved it
    (``resolved``): rule 5 of the module docstring, in one place (the barrier and the plan of a
    chain both use it). ``None`` when nobody else's is needed. The owner's rules travel with it
    (``resolved.ask``, ``resolved.never_ask``)."""
    approver: Literal["adult", "owner"] | None
    match resolved.approver:
        case "adult":
            approver = "adult"
        case "owner":
            # The owner's own request is theirs to agree to.
            approver = None if actor.role == "owner" else "owner"
        case "self":
            approver = None
        case None:
            approver = "adult" if an_adult_agrees_for(actor.role, action_type) else None
    if approver is None:
        return None
    return NeedsApproval(approver, None, resolved.ask, resolved.never_ask)


def decide(
    actor: Actor,
    action: DescribedAction,
    policy: PolicyTables = DEFAULT_POLICY,
    rights: RightsTable = NO_RIGHTS,
    facts: Mapping[str, object] | None = None,
    *,
    risks: Risks | None = None,
    ordinary_when_unsure: OrdinaryWhenUnsure = "do",
) -> Decision:
    """What may happen to ``action`` when ``actor`` asks for it, under the catalog ``policy`` and
    the owner's ``rights`` in force (see the module docstring). ``facts`` are the facts of the
    moment that a limit reads (``policy.facts.decision_facts``): without them, an action under a
    limit is refused (``limit_error``). How sure the code is of who asks is the actor's
    (``Actor.standing``, ``Actor.voice_too_short``); ``risks`` are what the operations of a launch
    declare, when the caller knows them (``may_act_weakly``), and ``ordinary_when_unsure`` the
    owner's word on ordinary things asked by someone the code is not sure of (rule 6)."""
    action_type = action.action_type
    if actor.standing == "unknown":
        return Refuse("untrusted_identity")
    if action_type in OWNER_ONLY_ACTIONS and actor.role != "owner":
        return Refuse("not_allowed")
    resolved = rights.resolve(actor, action_type, policy)
    if not resolved.allowed:
        return Refuse("not_allowed")
    decision: Decision | None = None
    needed = approval_needed(actor, resolved, action_type)
    if resolved.limit is not None:
        match _limit_says(resolved.limit, facts):
            case None:
                return Refuse("limit_error")
            case "refuse":
                return Refuse("limit", limit=resolved.limit.summary)
            case ("ask_owner" | "judge") as says if actor.role != "owner":
                decision = NeedsApproval(
                    "owner",
                    resolved.limit.judge_question or None,
                    resolved.ask,
                    resolved.never_ask,
                    # "ask_owner" is the owner's own word: always his. "judge" is the judge's
                    # first, unless someone's agreement is needed anyway.
                    judge_first=says == "judge" and needed is None,
                )
            case "ask_owner" | "judge" | "allow":
                # (The owner's own request: asking is the owner's agreement.)
                pass
    if decision is None and needed is not None:
        # The catalog's adult for a child (no right names anyone) is left to the judge first;
        # an approver a right names is the owner's word.
        decision = replace(needed, judge_first=resolved.approver is None)
    if decision is None:
        decision = Allow(resolved.ask, resolved.never_ask)
    if action.whose_judged or identity_judged(
        actor, action_type, policy, risks, ordinary_when_unsure
    ):
        # Not sure of who asks: the judge weighs it with the clues, never a refusal out of hand.
        return replace(decision, judge_identity=True)
    return decision


def identity_judged(
    actor: Actor,
    action_type: str,
    policy: PolicyTables,
    risks: Risks | None,
    ordinary_when_unsure: OrdinaryWhenUnsure,
) -> bool:
    """Whether the judge must weigh who asks (rule 6): never for someone the code is sure of;
    for what is sensitive when it is not sure of them, or when their voice was too short to tell;
    for what is ordinary only when it is not sure of them and the owner said to judge it. Also
    read for a call decided as the assistant's own act in a person's turn
    (``conversation.execution``): the doubt on who asked it is never lost."""
    if actor.standing == "sure" and not actor.voice_too_short:
        return False
    if actor.page_person_id is not None:
        # Heard at a page signed in as someone else: nothing is ordinary (review of Task 24).
        return True
    if not may_act_weakly(action_type, policy, risks=risks):
        return True
    return actor.standing == "unsure" and ordinary_when_unsure == "judge"


def _limit_says(limit: Limit, facts: Mapping[str, object] | None) -> LimitOutcome | None:
    """What ``limit`` answers on ``facts``; ``None`` when it cannot answer (no facts, an
    expression that does not compile, an evaluation that fails): the barrier then refuses."""
    if facts is None:
        return None
    try:
        return evaluate_limit(compile_limit(limit.expression), facts)
    except (InvalidLimit, LimitError):
        return None


def writes_for_domo(actor: Actor, action: DescribedAction) -> bool:
    """Whether ``record_decision`` of ``actor`` on ``action`` writes a line in Domo's name (his
    history reads those): ``actor`` is Domo, or a person who asks for a note in his name
    (``DescribedAction.also_domo``)."""
    return actor.subject == "domo" or (action.also_domo and actor.subject == "person")


async def record_decision(
    session: AsyncSession,
    actor: Actor,
    action: DescribedAction,
    decision: Decision,
    *,
    tool_name: str,
) -> None:
    """Write the audit entry ``policy.decision`` for a decision, in the caller's transaction.

    Its actor is the person (``person:<id>``) or Domo (``domo``: ``roles.actor_label``). The
    entry holds the tool, the action type and target, the facts of the action (written by the
    tool: names, identifiers, numbers, never a free text), and what was decided: ``allow``,
    ``needs_approval`` (``reason`` is whose: ``adult`` or ``owner``) or ``refuse`` (``reason`` is
    why). It never holds the arguments of the call: the function is not given any. Like
    ``append_audit``, it does not commit, and the caller writes it last in its transaction.
    """
    match decision:
        case Allow():
            outcome, reason = "allow", None
        case NeedsApproval(approver=approver):
            outcome, reason = "needs_approval", approver
        case Refuse(reason=refusal):
            outcome, reason = "refuse", refusal
    details: dict[str, JsonValue] = {
        "tool": tool_name,
        "action_type": action.action_type,
        "target": action.target,
        "decision": outcome,
        "reason": reason,
        "facts": dict(action.facts),
    }
    if isinstance(decision, Refuse) and decision.side is not None:
        details["side"] = decision.side
    await append_audit(session, actor=actor_label(actor), action="policy.decision", details=details)
    if action.also_domo and actor.subject == "person":
        # Judged for Domo too (``stricter``): his line, in his history, with whose turn it was.
        await append_audit(
            session,
            actor=actor_label(domo_actor()),
            action="policy.decision",
            details={**details, "turn_of": actor_label(actor)},
        )
