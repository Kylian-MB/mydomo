"""The decision of the barrier, at a moment: the owner's rights and the facts as they are now,
then ``barrier.decide``.

``decide`` is pure and takes everything as parameters; ``decide_now`` is the one entry point the
core uses (the chat, for a call of the model; the confirmations, when a yes replays the decision,
and for a change): it reads the rights in force on the caller's session (``rights.load_rights``),
so that a right given, changed, taken away or paused applies at once, to the requests that wait
too. When a limit applies to the action (a right's, or the catalog's), it also reads the facts of
the moment ``now`` that the limit reads (``policy.facts.decision_facts``), with the household's
time zone from the owner's settings; an action without a limit needs none, and none are read. The
actor may be Domo himself (``roles.domo_actor``): his own rights, and his own facts.

A limit that cannot answer refuses the action (``limit_error``): that is written to the log here
(which action type, never the facts), and to the audit log by the caller with the decision
(``barrier.record_decision``, as for every decision).

A chain of actions (spec 4) remembers, from the moment it was asked for, the role of the one who
asked and the ids of their own rights in force then (``ChainBasis``): the steps of the chain are
judged later, after the yes, and what the chain itself took from the person must not stop them
(spec 4.3). ``decide_in_chain`` is the rule of the chain, for one step, for the person as the
database holds them now:

* their role: the chain itself changes it only by handing the owner's role over
  (``household.transfer_ownership``, from them: they become an adult). If their role now is the one
  the steps done so far would leave them with, only the chain changed it, and the barrier judges
  them with the role they had when they asked; otherwise someone else changed it, and the role of
  now counts (``judged_actor``);
* their own rights: the chain itself adds some only by applying a draft of rights
  (``rights.apply``) that gives them one. If their own rights in force now, less those, are the
  ones they had when they asked, the barrier reads the rights in force without those; otherwise
  someone else changed them (a right taken away since cannot be told from one the chain took, and
  counts as someone else's change: the rights of now apply, which closes rather than opens);
* the facts a limit reads are those of the moment, for the person as they are judged.

This never reaches past the foundation: the owner's own actions are the owner's by the role the
step is judged with, but the tools still refuse what they always refuse on the facts of now (the
owner's role never changes, only the owner of now hands it over: spec 8.3), and a step is
described again before it is judged.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Final, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.people.directives import refusal_for
from domo_core.policy.actions import DescribedAction
from domo_core.policy.barrier import Decision, Refuse, decide, stricter
from domo_core.policy.catalog import PolicyTables
from domo_core.policy.facts import decision_facts
from domo_core.policy.models import PolicyRight
from domo_core.policy.rights import RightsTable, load_rights
from domo_core.policy.roles import Actor, Role, as_role, asked_as, domo_actor
from domo_core.policy.settings import load_policy_settings

logger = logging.getLogger(__name__)

_BASIS_FIELDS = frozenset({"role", "right_ids"})

# The steps by which a chain changes the role or the own rights of the one who asked.
_HANDS_THE_ROLE_OVER: Final = "household.transfer_ownership"
_APPLIES_RIGHTS: Final = "rights.apply"
_DRAFT: Final = "draft:"


@dataclass(frozen=True)
class ChainBasis:
    """What a chain of actions remembers of the one who asked, as they were when they asked: their
    ``role``, and the ids of the rights in force given to them themselves (``right_ids``: not
    those of their role). It is stored with the request of the chain
    (``confirmations.chain_basis``)."""

    role: Role
    right_ids: frozenset[UUID]

    def as_json(self) -> dict[str, object]:
        """The basis as it is stored: the role, and the ids as texts, in order."""
        return {"role": self.role, "right_ids": sorted(str(right) for right in self.right_ids)}

    @classmethod
    def from_json(cls, data: object) -> "ChainBasis":
        """The inverse of ``as_json``. ``ValueError`` unless ``data`` is exactly what it writes:
        a stored basis that does not read back is never guessed."""
        if (
            not isinstance(data, Mapping)
            or frozenset(cast(Mapping[str, object], data)) != _BASIS_FIELDS
        ):
            raise ValueError("the basis of a chain is exactly a role and the ids of rights")
        stored = cast(Mapping[str, object], data)
        role, right_ids = stored["role"], stored["right_ids"]
        if not isinstance(role, str):
            raise ValueError("the role of a chain's basis is a text")
        if not isinstance(right_ids, list):
            raise ValueError("the rights of a chain's basis are a list of ids")
        ids: set[UUID] = set()
        for right in cast(list[object], right_ids):
            if not isinstance(right, str):
                raise ValueError("the id of a right is a text")
            ids.add(UUID(right))
        return cls(as_role(role), frozenset(ids))


async def decide_now(
    session: AsyncSession,
    actor: Actor,
    action: DescribedAction,
    policy: PolicyTables,
    now: datetime,
    *,
    not_counting: UUID | None = None,
    rights: RightsTable | None = None,
    unrecorded: Sequence[str] = (),
    spent_leaving_out: UUID | None = None,
    directives_read: bool = False,
) -> Decision:
    """What may happen to ``action`` when ``actor`` asks for it now: the rights in force, read on
    ``session`` (or ``rights``, when the caller read them: ``decide_in_chain``), on top of the
    catalog ``policy``, and the facts of the moment ``now`` when a limit applies (see
    ``barrier.decide``). Reads only: nothing is written, nothing committed.

    ``not_counting`` is a request (a confirmation) that a limit's history leaves out: the one a
    yes has been given to and that is now being carried out, which a limit read when the request
    was made, and at the yes, did not count either (``policy.key_forms``). ``unrecorded``
    are the types of the actions the person did a moment ago that no table holds yet, which the
    history counts all the same (``policy.facts``: the steps of a chain that runs at once).
    ``spent_leaving_out`` is a declaration of spending the facts' ``spent`` leaves out (a
    program's relaunch whose begin it is).

    An action that is Domo's own act too, asked by a person (``DescribedAction.also_domo``,
    ruling R29), is decided for the person, exactly as any action of theirs, and for Domo, and
    the stricter stands (``barrier.stricter``): what the person may not do never goes out in his
    name, and what the owner took from him neither."""
    if action.also_domo and actor.subject == "person":
        once = replace(action, also_domo=False)
        decided = [
            await decide_now(
                session,
                who,
                once,
                policy,
                now,
                not_counting=not_counting,
                rights=rights,
                unrecorded=unrecorded,
                spent_leaving_out=spent_leaving_out,
            )
            for who in (actor, domo_actor())
        ]
        return stricter(*decided)
    # The owner's directives on people come first and are applied, never weighed: not even the
    # rights are read for an action they refuse. The person's side of the assistant's own idea
    # (``directives_read``) does not read them again: his side did, with his exception.
    refused = None if directives_read else await refusal_for(session, actor, action, now)
    if refused is not None:
        return refused
    table = await load_rights(session) if rights is None else rights
    facts: dict[str, object] | None = None
    if table.resolve(actor, action.action_type, policy).limit is not None:
        settings = await load_policy_settings(session)
        facts = await decision_facts(
            session,
            actor,
            action,
            now,
            settings.time_zone,
            not_counting=not_counting,
            unrecorded=unrecorded,
            spent_leaving_out=spent_leaving_out,
        )
    decision = decide(actor, action, policy, table, facts)
    if isinstance(decision, Refuse) and decision.reason == "limit_error":
        logger.error(
            "A limit on %s could not be checked for %s: the action is refused",
            action.action_type,
            "Domo" if actor.subject == "domo" else f"a {actor.role}",
        )
    return decision


def judged_actor(actor_now: Actor, basis: ChainBasis, done: Sequence[DescribedAction]) -> Actor:
    """The person, as a step of their chain judges them (see the module docstring):
    ``actor_now`` (their role as the database holds it now) with the role they had when they asked
    (``basis``), when the steps ``done`` so far are the only change to it; with their role of now
    otherwise. Handing the owner's role over is the one step that changes it: they become an
    adult."""
    me = f"person:{actor_now.person_id}"
    handed_over = any(
        action.action_type == _HANDS_THE_ROLE_OVER and action.facts.get("from") == me
        for action in done
    )
    expected: Role = "adult" if handed_over else basis.role
    return asked_as(actor_now, basis.role) if actor_now.role == expected else actor_now


async def _given_by_the_chain(
    session: AsyncSession, person_id: UUID, done: Sequence[DescribedAction]
) -> frozenset[UUID]:
    """The ids of the rights given to ``person_id`` themselves by the drafts of rights that the
    steps ``done`` applied (their target is ``draft:<id>``)."""
    drafts: set[UUID] = set()
    for action in done:
        target = action.target
        if (
            action.action_type == _APPLIES_RIGHTS
            and target is not None
            and target.startswith(_DRAFT)
        ):
            drafts.add(UUID(target.removeprefix(_DRAFT)))
    if not drafts:
        return frozenset()
    found = await session.execute(
        select(PolicyRight.id).where(
            PolicyRight.person_id == person_id, PolicyRight.draft_id.in_(drafts)
        )
    )
    return frozenset(found.scalars())


async def decide_in_chain(
    session: AsyncSession,
    actor_now: Actor,
    action: DescribedAction,
    policy: PolicyTables,
    now: datetime,
    *,
    basis: ChainBasis,
    done: Sequence[DescribedAction],
) -> tuple[Actor, Decision]:
    """What may happen to ``action``, a step of a chain, for the person who asked it as the
    database holds them now (``actor_now``), under the rule of the chain (see the module
    docstring): ``basis`` is what the chain remembers of them, ``done`` the actions of its steps
    done so far, in order. Returns the person as the step is judged (for the audit of the
    decision, ``barrier.record_decision``, and the run of the step) and the decision. Reads only:
    nothing is written, nothing committed."""
    actor = judged_actor(actor_now, basis, done)
    rights = await load_rights(session)
    mine_now = frozenset(right.id for right in rights.rights if right.person_id == actor.person_id)
    added = await _given_by_the_chain(session, actor.person_id, done)
    if mine_now - added == basis.right_ids:
        # Only the chain changed them: the person is judged as they were when they asked.
        rights = replace(rights, rights=tuple(r for r in rights.rights if r.id not in added))
    decision = await decide_now(session, actor, action, policy, now, rights=rights)
    return actor, decision
