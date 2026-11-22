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

1. an identity that cannot be trusted is refused (``untrusted_identity``), except for the types of
   ``UNTRUSTED_MAY`` that a guest may do: those do not depend on who the person is. A right cannot
   make such an identity more than that: whoever it claims to be, its personal rights unlock
   nothing, and nobody's agreement makes it more (an agreement the rights ask for refuses it);
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
   ``"ask_owner"`` and ``"judge"`` need the owner's agreement (``judge``: in 1b Domo has no
   memory to judge with, spec 5.4), with the right's question, when it has one, for the owner to
   read; the owner's own request goes on (asking is the owner's agreement); ``"allow"`` goes on. A
   limit that cannot answer (it does not compile, it fails, it answers something else, or no facts
   were given) refuses (``limit_error``): a limit fails closed;
5. whose agreement is needed (``approval_needed``, which the plan of a chain uses too): an
   adult's when a right says so, the owner's when a right says so (never for the owner's own
   request), and, when no right names anyone, an adult's for a child on the types of
   ``catalog.ADULT_AGREES_FOR_A_CHILD`` (a right that opens such a type to a child keeps the adult,
   unless it says the child agrees for themselves, ``self``); otherwise the action is allowed.

Acknowledged divergences from spec section 10 (decided for 1b):

* spec 10.3 escalates an action of an untrusted identity to a confirmation by the owner on a
  trusted channel. 1b has no untrusted channel (the web page is the only one, and it logs people
  in), so such an actor is refused. The escalation comes with the channels;
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
    ADULT_AGREES_FOR_A_CHILD,
    DEFAULT_POLICY,
    OWNER_ONLY_ACTIONS,
    PolicyTables,
)
from domo_core.policy.limits import (
    InvalidLimit,
    LimitError,
    LimitOutcome,
    compile_limit,
    evaluate_limit,
)
from domo_core.policy.rights import NO_RIGHTS, Limit, Resolved, RightsTable
from domo_core.policy.roles import Actor, actor_label, domo_actor

# ``limit``: a limit refused it; ``limit_error``: a limit could not say (refused all the same).
RefusalReason = Literal["not_allowed", "untrusted_identity", "limit", "limit_error"]

# What an identity that cannot be trusted may still do (when a guest may): renaming oneself,
# which does not depend on who the person is (F27).
UNTRUSTED_MAY: Final = frozenset({"profile.rename"})


@dataclass(frozen=True)
class Allow:
    """Do it; the judge decides the yes. ``rule`` is what the owner's rule asks before it, if one
    does; ``never_ask`` the owner's "never ask me for this", with his conditions
    (``Resolved.never_ask``)."""

    rule: Ask | None = None
    never_ask: NeverAsk | None = None


@dataclass(frozen=True)
class NeedsApproval:
    """Someone else must agree first: an adult (a child's request on a type of
    ``ADULT_AGREES_FOR_A_CHILD``, or a right that says so) or the owner (a right, or a limit that
    sends to the owner); ``question`` is the right's, when a limit asked it; ``rule`` and
    ``never_ask`` as for ``Allow``."""

    approver: Literal["adult", "owner"]
    question: str | None = None
    rule: Ask | None = None
    never_ask: NeverAsk | None = None


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
    nothing."""
    if isinstance(person, Refuse):
        return replace(person, side="person")
    if isinstance(domo, Refuse):
        return replace(domo, side="domo")
    rule = most_demanding(person.rule, domo.rule)
    never_ask = person.never_ask if domo.rule is None else None
    asking = [d for d in (person, domo) if isinstance(d, NeedsApproval)]
    if not asking:
        return Allow(rule=rule, never_ask=never_ask)
    most = max(asking, key=lambda d: _APPROVERS.index(d.approver))
    return NeedsApproval(most.approver, question=most.question, rule=rule, never_ask=never_ask)


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
            needs_an_adult = actor.role == "child" and action_type in ADULT_AGREES_FOR_A_CHILD
            approver = "adult" if needs_an_adult else None
    if approver is None:
        return None
    return NeedsApproval(approver, None, resolved.ask, resolved.never_ask)


def decide(
    actor: Actor,
    action: DescribedAction,
    policy: PolicyTables = DEFAULT_POLICY,
    rights: RightsTable = NO_RIGHTS,
    facts: Mapping[str, object] | None = None,
) -> Decision:
    """What may happen to ``action`` when ``actor`` asks for it, under the catalog ``policy`` and
    the owner's ``rights`` in force (see the module docstring). ``facts`` are the facts of the
    moment that a limit reads (``policy.facts.decision_facts``): without them, an action under a
    limit is refused (``limit_error``)."""
    action_type = action.action_type
    if not actor.trusted and not (
        action_type in UNTRUSTED_MAY and action_type in policy.allowed.get("guest", frozenset())
    ):
        return Refuse("untrusted_identity")
    if action_type in OWNER_ONLY_ACTIONS and actor.role != "owner":
        return Refuse("not_allowed")
    resolved = rights.resolve(actor, action_type, policy)
    if not resolved.allowed:
        return Refuse("not_allowed")
    decision: Decision | None = None
    if resolved.limit is not None:
        match _limit_says(resolved.limit, facts):
            case None:
                return Refuse("limit_error")
            case "refuse":
                return Refuse("limit", limit=resolved.limit.summary)
            case "ask_owner" | "judge" if actor.role != "owner":
                decision = NeedsApproval(
                    "owner",
                    resolved.limit.judge_question or None,
                    resolved.ask,
                    resolved.never_ask,
                )
            case "ask_owner" | "judge" | "allow":
                # (The owner's own request: asking is the owner's agreement.)
                pass
    if decision is None:
        decision = approval_needed(actor, resolved, action_type) or Allow(
            resolved.ask, resolved.never_ask
        )
    if not actor.trusted and isinstance(decision, NeedsApproval):
        # Nobody's agreement makes an identity that cannot be trusted more than it may do alone.
        return Refuse("untrusted_identity")
    return decision


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
