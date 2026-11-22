"""When a running program's request needs a yes (spec 5.3, 5.8): pure, read without a model.

A program judges nothing. For each of its requests the barrier decides (``policy.barrier``) with
the rights of whom the launch is for, and ``judge_request`` turns that decision, the request's four
risks (its operation's, never less than the code's minimum: ``madetools.risks``) and what stands
for it into one of three answers: ``Go`` (it is done), ``No`` (it is refused, ``reason`` the
barrier's) or ``NeedsYes`` (nothing is done: the run stops there, and Domo is woken with what was
to be done; he asks, in his own words, and runs it again once the yes is given).

The order of judgement:

1. the barrier refused it (``Refuse``): ``No``, whatever stands for it;
2. the yeses it needs, in this order (the first gives the stop its ``why``; the stop asks the
   strongest approver and the strongest ask among all of them, ``joined``, so that one yes answers
   the request whole):
3. the barrier needs someone else's agreement (``NeedsApproval``): ``NeedsYes("approval", <the
   adult or the owner>, "yes")``. That one is always a person: Domo never counts as the adult
   or the owner who agrees for someone else, the house included;
4. an owner's rule asks before this type (``decision.rule``): ``NeedsYes("owner_rule", ...)``
   asking what the rule asks. For a person the yes is their own (``self``); for the house
   (``for_domo``) it is the owner's (``owner``): the owner's "always ask me" on a right of
   Domo's is the owner's to answer, never Domo's;
5. the request costs money and no standing agreement covers it (``covered``):
   ``NeedsYes("costs", "self", "yes")``. For the house, ``self`` is Domo: he judges, and the
   code refuses him nothing for a risk;
6. otherwise ``Go``.

A request run under the yeses already given in a chain of relaunches (``judge_under_yes``) is
lifted of each need one of them answers (``answers``: no stronger approver, no stronger ask); a
refusal is never lifted.

Being irreversible, acting on the physical world or reaching outside the house never ask a yes by
themselves: Domo judged them when he launched the tool. A limit of the owner's on the type (a
right's ``limit``, read by the barrier with what was spent: ``policy.facts``) applies to everyone,
Domo included, through the decision itself."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

from domo_core.policy.asks import Ask
from domo_core.policy.barrier import Decision, NeedsApproval, Refuse
from domo_core.policy.catalog import Risks
from domo_core.policy.rights import Approver

# Why a request needs a yes: it costs and nothing covers it, an owner's rule asks before its
# type, or the barrier asks someone else's agreement.
Why = Literal["costs", "owner_rule", "approval"]


@dataclass(frozen=True)
class Go:
    """The request may be done."""


@dataclass(frozen=True)
class No:
    """The request is refused: ``reason`` is the barrier's."""

    reason: str


@dataclass(frozen=True)
class NeedsYes:
    """Nothing is done: the run stops, and the yes of ``approver`` (``self``: the one the launch
    is for, Domo for the house; ``adult`` or ``owner``: a person) asking ``ask`` lets it run
    again. ``why`` says which of the three cases it is."""

    why: Why
    approver: Approver
    ask: Ask


type Judged = Go | No | NeedsYes


def _needs(decision: Decision, *, risks: Risks, for_domo: bool, covered: bool) -> list[NeedsYes]:
    """Every yes a request that the barrier did not refuse needs, in the order of judgement."""
    needs: list[NeedsYes] = []
    if isinstance(decision, NeedsApproval):
        needs.append(NeedsYes("approval", decision.approver, "yes"))
    if not isinstance(decision, Refuse) and decision.rule is not None:
        needs.append(NeedsYes("owner_rule", "owner" if for_domo else "self", decision.rule))
    if risks.costs and not covered:
        needs.append(NeedsYes("costs", "self", "yes"))
    return needs


def judge_request(decision: Decision, *, risks: Risks, for_domo: bool, covered: bool) -> Judged:
    """What comes of a program's request with no yes given yet (see the module docstring):
    ``decision`` is the barrier's, ``risks`` the request's, ``for_domo`` whether the launch is
    for the house, ``covered`` whether a standing agreement covers its cost."""
    return judge_under_yes(decision, risks=risks, for_domo=for_domo, covered=covered, yes=())


# Whose yes, and what yes, from the least demanding to the most.
_APPROVERS: Final[tuple[Approver, ...]] = ("self", "adult", "owner")
_ASKS: Final[tuple[Ask, ...]] = ("yes", "explicit_yes", "proof")


def answers(given: NeedsYes, needed: NeedsYes) -> bool:
    """Whether the yes ``given`` (what a stop asked, and was answered) answers ``needed``: it
    asks no more, neither a more demanding approver (``self`` < ``adult`` < ``owner``) nor a more
    demanding yes (``yes`` < ``explicit_yes`` < ``proof``)."""
    return _APPROVERS.index(needed.approver) <= _APPROVERS.index(given.approver) and _ASKS.index(
        needed.ask
    ) <= _ASKS.index(given.ask)


def joined(first: NeedsYes, *more: NeedsYes) -> NeedsYes:
    """One yes that answers ``first`` and every one of ``more``: the strongest approver and the
    strongest ask among them (as a request's ask is raised, ``confirmations.service._raise_to``),
    and the why of the first need that asks that approver: a stop asked of an adult or the owner
    is an approval or a rule of theirs, never a cost the requester answers."""
    every = (first, *more)
    strongest = max(_APPROVERS.index(need.approver) for need in every)
    approver = _APPROVERS[strongest]
    ask = _ASKS[max(_ASKS.index(need.ask) for need in every)]
    named = next(need for need in every if need.approver == approver)
    return NeedsYes(named.why, approver, ask)


def judge_under_yes(
    decision: Decision,
    *,
    risks: Risks,
    for_domo: bool,
    covered: bool,
    yes: Sequence[NeedsYes],
    for_cost: Sequence[NeedsYes] | None = None,
) -> Judged:
    """What comes of a request run under the yeses ``yes`` (what each stop answered asked): each
    need is lifted only when one of them answers it (``answers``); what is left is needed again,
    as one yes that answers all of it (``joined``). A refusal is never lifted. A yes is bound to
    its stop: a requester's own yes never stands for an adult's approval or the owner's
    proof. ``for_cost`` are the yeses that may answer the cost, when not all of ``yes`` may
    (``None``: all of them): a cost asked again is answered only by a yes given for it."""
    if isinstance(decision, Refuse):
        return No(decision.reason)
    needs = _needs(decision, risks=risks, for_domo=for_domo, covered=covered)
    left = [
        need
        for need in needs
        if not any(
            answers(given, need)
            for given in (for_cost if need.why == "costs" and for_cost is not None else yes)
        )
    ]
    return joined(*left) if left else Go()
