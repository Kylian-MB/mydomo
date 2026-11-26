"""The owner's rights: who may do what, per role and per person (spec 5.1 to 5.3, 5.9, 5.12).

A right is about an action type of the catalog and a subject: a role (``adult``, ``child``...) or a
person. It may say whether the subject may do it (``access``: ``allow`` or ``deny``, which can take
away what the catalog gives), whose agreement is needed first (``approver``: ``self``, ``adult`` or
``owner``), under which limit (``limit``, a CEL expression the barrier evaluates:
``policy.limits``), and what Domo must ask before the action whatever he would judge (``ask``:
``yes``, ``explicit_yes`` or ``proof``: the owner's "always ask me before..."), or the owner's
"never ask me" (``ask`` ``judge``, with his conditions in his words, ``ask_when``; or ``never``, not
at all). Each part is optional: what a right does not say, the next one down says. A right with no
subject at all is a rule for everyone: it says what to ask and nothing else (the table holds it so).

Domo is a subject of his own (``for_domo``, the owner, 2026-11-18), never a person nor a role:
for what he does on his own for the house (``roles.domo_actor``), his own right, else the
catalog's ``PolicyTables.domo`` (every type but the owner's own). No role's right reaches him (a
right given to the adults is theirs), and the owner's rules for everyone on what to ask apply to
him as to anyone. A right of his is no rule for everyone: it reaches nobody else.

Resolution (``RightsTable.resolve``), part by part: the person's own right, else their role's,
else the catalog (``policy.catalog.PolicyTables``, which the web dependency ``get_policy`` hands
in, and tests replace). So a person's right that only says who agrees keeps the access of their
role's right. The limit goes the same way, down to the catalog's own limit for the
action type (``catalog.DEFAULT_LIMITS``): a right's limit replaces it, and a right whose limit is
``"allow"`` lifts it. What to ask is not one right's: it is the most demanding of the person's,
their role's and everyone's (``asks.most_demanding``). The owner's "never ask me" is the nearest
one (the person's, then the role's, then everyone's), and only when nothing asks: a rule that asks
always wins over one that says not to. On the owner's own actions (``catalog.OWNER_ONLY_ACTIONS``)
only the person's own right says "never ask me", never a role's or everyone's, and only there does
"not at all" (``NeverAsk.at_all``) hold; anywhere else it is guidance for the judge, as "judge" is.
Nothing is asked by default (2026-11-15), so a "never ask me" lifts nothing: it only tells the
judge, and with "not at all" keeps the judge and Domo from asking a yes. Three things
no right moves:

* an action type the catalog does not know (absent from ``policy.known``) is never allowed;
* an action type whose rights are another's (``catalog.RIGHTS_OF``: giving an invitation's link
  again is inviting) is resolved on that other type, every part of it;
* the owner's own actions (``catalog.OWNER_ONLY_ACTIONS``: changing the rules, handing the
  owner's role over) ignore every right but what to ask: the catalog decides them, and only for
  the owner. ``barrier.decide`` refuses them to anyone else first anyway.

The rights are stored in ``policy_rights`` (``models.PolicyRight``) and read again at every
decision (``load_rights``, from ``policy.decision.decide_now``) and at every turn, for the tools a
person is shown (``tools.registry.tools_for``): a change applies at once, to the requests that
wait too. A right is never deleted: replaced or taken away it is ``revoked_at``. A person's own
rights are paused, not lost, when they leave the household (``pause_rights_and_audit``, spec
5.7) and again when they come back to it (spec 5.12); paused rights do not count, and stay listed
(``paused_rights``) until the owner gives them back or takes them away. A right of a role, or a
rule for everyone, is never paused.

The functions here do not commit: the caller does. They write no audit entry either, except
``pause_rights_and_audit``, the one place where a pause is made and said.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.audit.log import append_audit
from domo_core.policy.asks import Ask, NeverAsk, RuleAsk, as_rule_ask, most_demanding
from domo_core.policy.catalog import (
    OWNER_ONLY_ACTIONS,
    Limit,
    PolicyTables,
    rights_type,
)
from domo_core.policy.models import PolicyRight
from domo_core.policy.roles import Actor, Role, as_role

__all__ = [
    "NO_RIGHTS",
    "Access",
    "Approver",
    "Limit",
    "Resolved",
    "Right",
    "RightsTable",
    "load_rights",
    "pause_personal_rights",
    "pause_rights_and_audit",
    "paused_rights",
    "personal_right_summaries",
]

Access = Literal["allow", "deny"]
# Whose agreement a right asks for first: the requester's own (``self``: nobody else's), an adult
# other than them, or the owner (never for the owner's own request: asking is their agreement).
Approver = Literal["self", "adult", "owner"]

_ACCESS: Final[tuple[Access, ...]] = ("allow", "deny")
_APPROVERS: Final[tuple[Approver, ...]] = ("self", "adult", "owner")


@dataclass(frozen=True)
class Right:
    """A right in force, as the barrier reads it: its subject (``person_id``, ``role`` or Domo,
    ``for_domo``, one at most; none for a rule for everyone), its action type, what it says
    (each part ``None`` when it says nothing of it: ``ask`` is what to ask, or the owner's "never
    ask me", ``ask_when`` his conditions to a ``judge``) and its ``summary``, in plain words."""

    id: UUID
    person_id: UUID | None
    role: Role | None
    action_type: str
    access: Access | None
    approver: Approver | None
    limit: Limit | None
    ask: RuleAsk | None
    ask_when: str | None
    summary: str
    for_domo: bool = False


@dataclass(frozen=True)
class Resolved:
    """What applies to one person for one action type, once the rights and the catalog are read.

    ``allowed``: whether they may do it at all (never for a type the catalog does not know).
    ``approver``: whose agreement is needed first, when a right says so (``None``: the barrier's
    default, an adult for a child on ``catalog.ADULT_AGREES_FOR_A_CHILD``, nobody else's
    otherwise).
    ``limit``: the limit that applies, if any: the person's right's, else their role's, else the
    catalog's. ``ask``: what the owner's rules ask before it, the most demanding of the person's,
    their role's and everyone's (``None``: none asks anything). ``never_ask``: the owner's "never
    ask me", the nearest one, only when no rule asks (``None`` otherwise)."""

    allowed: bool
    approver: Approver | None
    limit: Limit | None
    ask: Ask | None
    never_ask: NeverAsk | None


def _first[T](*values: T | None) -> T | None:
    """The first of ``values`` that is not ``None``."""
    return next((value for value in values if value is not None), None)


def _demand(right: Right | None) -> Ask | None:
    """What ``right`` asks before the action (``yes``, ``explicit_yes`` or ``proof``), if it
    asks anything: not a "never ask me"."""
    if right is None:
        return None
    match right.ask:
        case "yes" | "explicit_yes" | "proof" as ask:
            return ask
        case _:
            return None


def _asks(action_type: str, rights: tuple[Right | None, ...]) -> tuple[Ask | None, NeverAsk | None]:
    """What the owner's ``rights`` for an action type (the person's, their role's, everyone's, in
    that order) say to ask: the most demanding of what they ask, and, only when none asks
    anything, the owner's "never ask me" of the nearest one that says it. "Not at all" holds only
    on the owner's own actions (``catalog.OWNER_ONLY_ACTIONS``, F5). On those, only the person's
    own right says "never ask me", never a role's or everyone's (F4): it must not pass to a later
    owner. The rights writer refuses those already; resolution holds it too."""
    demands: list[Ask | None] = [_demand(right) for right in rights]
    ask = most_demanding(*demands)
    if ask is not None:
        return ask, None
    owners_own = action_type in OWNER_ONLY_ACTIONS
    lifting = next(
        (
            right
            for right in (rights[:1] if owners_own else rights)
            if right is not None and right.ask in ("judge", "never")
        ),
        None,
    )
    if lifting is None:
        return None, None
    at_all = lifting.ask == "never" and owners_own
    return None, NeverAsk(when=lifting.ask_when, at_all=at_all)


@dataclass(frozen=True)
class RightsTable:
    """The rights in force at one moment (``load_rights``)."""

    rights: tuple[Right, ...]

    def _own_right(self, actor: Actor, action_type: str) -> Right | None:
        """The right given to ``actor`` themselves: the person's, or Domo's (``for_domo``)."""
        if actor.subject == "domo":
            return next(
                (r for r in self.rights if r.for_domo and r.action_type == action_type), None
            )
        return next(
            (
                r
                for r in self.rights
                if r.person_id == actor.person_id and r.action_type == action_type
            ),
            None,
        )

    def _role_right(self, role: Role, action_type: str) -> Right | None:
        return next(
            (
                r
                for r in self.rights
                if r.person_id is None and r.role == role and r.action_type == action_type
            ),
            None,
        )

    def _everyone_right(self, action_type: str) -> Right | None:
        """The rule for everyone for ``action_type``, if there is one (no person, no role)."""
        return next(
            (
                r
                for r in self.rights
                if r.person_id is None
                and r.role is None
                and not r.for_domo
                and r.action_type == action_type
            ),
            None,
        )

    def denied_on_purpose(self, actor: Actor, action_type: str) -> bool:
        """Whether the owner wrote a ``deny`` for ``actor`` on ``action_type`` (read on the type
        whose rights it follows, ``rights_type``): their own right says it, or, when they have
        none, their role's. A right merely not given is no deny. Never true for Domo: a right of
        his is his own, not a person's."""
        if actor.subject == "domo":
            return False
        action_type = rights_type(action_type)
        mine = self._own_right(actor, action_type)
        role = self._role_right(actor.role, action_type)
        access = _first(
            None if mine is None else mine.access,
            None if role is None else role.access,
        )
        return access == "deny"

    def resolve(self, actor: Actor, action_type: str, policy: PolicyTables) -> Resolved:
        """What applies to ``actor`` for ``action_type``, part by part: their own right, else
        their role's, else the catalog ``policy``; and what to ask, from their own right, their
        role's and everyone's (see the module docstring). For Domo (``Actor.subject``), his own
        right, else the catalog's ``policy.domo``: no role's right reaches him."""
        if action_type not in policy.known:
            return Resolved(False, None, None, None, None)
        # Giving an invitation's link again is inviting: read on the type it follows.
        action_type = rights_type(action_type)
        if action_type not in policy.known:
            return Resolved(False, None, None, None, None)
        is_domo = actor.subject == "domo"
        in_catalog = action_type in (
            policy.domo if is_domo else policy.allowed.get(actor.role, frozenset())
        )
        mine = self._own_right(actor, action_type)
        role = None if is_domo else self._role_right(actor.role, action_type)
        ask, never_ask = _asks(action_type, (mine, role, self._everyone_right(action_type)))
        if action_type in OWNER_ONLY_ACTIONS:
            # The foundation: no right moves it, and it is the owner's alone. Only what to ask
            # is read on it (F4): the owner's own rules about his own yes.
            return Resolved(actor.role == "owner" and in_catalog, None, None, ask, never_ask)
        access = _first(
            None if mine is None else mine.access,
            None if role is None else role.access,
        )
        return Resolved(
            allowed=in_catalog if access is None else access == "allow",
            approver=_first(
                None if mine is None else mine.approver,
                None if role is None else role.approver,
            ),
            limit=_first(
                None if mine is None else mine.limit,
                None if role is None else role.limit,
                policy.limits.get(action_type),
            ),
            ask=ask,
            never_ask=never_ask,
        )


NO_RIGHTS: Final = RightsTable(())
"""No right at all: the catalog decides everything."""


def _one_of[T: str](value: str | None, accepted: tuple[T, ...], what: str) -> T | None:
    """The stored ``value`` as one of ``accepted`` (``None`` stays ``None``). The table's
    constraints keep any other value out: a ``ValueError`` means a bug or a database edited by
    hand, and every decision then fails rather than guess."""
    if value is None:
        return None
    for candidate in accepted:
        if candidate == value:
            return candidate
    raise ValueError(f"Not a value of {what}: {value!r}")


def _right_of(row: PolicyRight) -> Right:
    limit = (
        None
        if row.limit_expression is None
        else Limit(row.limit_expression, row.judge_question, row.summary)
    )
    return Right(
        id=row.id,
        person_id=row.person_id,
        role=None if row.role is None else as_role(row.role),
        action_type=row.action_type,
        access=_one_of(row.access, _ACCESS, "access"),
        approver=_one_of(row.approver, _APPROVERS, "approver"),
        limit=limit,
        ask=as_rule_ask(row.ask),
        ask_when=row.ask_when,
        summary=row.summary,
        for_domo=row.for_domo,
    )


async def _rights_where(session: AsyncSession, *, paused: bool) -> tuple[Right, ...]:
    result = await session.execute(
        select(PolicyRight)
        .where(
            PolicyRight.revoked_at.is_(None),
            PolicyRight.paused_at.is_not(None) if paused else PolicyRight.paused_at.is_(None),
        )
        .order_by(PolicyRight.created_at, PolicyRight.id)
        .execution_options(populate_existing=True)
    )
    return tuple(_right_of(row) for row in result.scalars())


async def load_rights(session: AsyncSession) -> RightsTable:
    """The rights in force, as the database holds them now: neither revoked nor paused."""
    return RightsTable(await _rights_where(session, paused=False))


async def paused_rights(session: AsyncSession) -> tuple[Right, ...]:
    """The rights that are paused (and not revoked): people's own rights, kept since they left
    the household (or came back to it), until the owner gives them back or takes them away.
    Oldest first."""
    return await _rights_where(session, paused=True)


async def pause_personal_rights(session: AsyncSession, person_id: UUID, now: datetime) -> int:
    """Pause the rights of ``person_id`` that are in force (spec 5.7 and 5.12: they left the
    household, or came back to it), at ``now``; returns how many. The rights of their role are not
    theirs and stay, and so do the rules for everyone (no person: Review Focus 4). Flushed, not
    committed."""
    paused = await session.execute(
        update(PolicyRight)
        .where(
            PolicyRight.person_id == person_id,
            PolicyRight.revoked_at.is_(None),
            PolicyRight.paused_at.is_(None),
        )
        .values(paused_at=now)
        .returning(PolicyRight.id)
        .execution_options(synchronize_session=False)
    )
    count = len(paused.all())
    await session.flush()
    return count


async def pause_rights_and_audit(
    session: AsyncSession, person_id: UUID, now: datetime, *, actor: str | None = None
) -> int:
    """Pause the rights of ``person_id`` that are in force (``pause_personal_rights``), at ``now``,
    and, when there were some, write the audit entry ``rights.paused`` (the person's identifier and
    how many: the rights themselves are where the owner reads them). Returns how many.

    Whoever makes a person leave, or come back, calls this: a removal
    (``household.service.disable``), a return (``accept_return``) and the upkeep for a guest whose
    stay ended (``household.upkeep``). ``actor`` is the audit entry's: the person themselves
    unless said (``"system"`` for the upkeep). Flushed, not committed."""
    paused = await pause_personal_rights(session, person_id, now)
    if paused:
        await append_audit(
            session,
            actor=f"person:{person_id}" if actor is None else actor,
            action="rights.paused",
            details={"person_id": str(person_id), "count": paused},
        )
    return paused


async def personal_right_summaries(
    session: AsyncSession, person_id: UUID, *, with_paused: bool = False
) -> list[str]:
    """The summaries of the rights in force given to ``person_id`` themselves (not to their
    role), oldest first: what the owner is told the person has. With ``with_paused``, those that
    are paused count too (what waits for the owner to be given back)."""
    query = select(PolicyRight.summary).where(
        PolicyRight.person_id == person_id, PolicyRight.revoked_at.is_(None)
    )
    if not with_paused:
        query = query.where(PolicyRight.paused_at.is_(None))
    result = await session.execute(query.order_by(PolicyRight.created_at, PolicyRight.id))
    return list(result.scalars())
