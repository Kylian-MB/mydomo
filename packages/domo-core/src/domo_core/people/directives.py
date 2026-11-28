"""The owner's directives on the people of the house, applied before any judgement.

The owner has four: no new people, nobody kept, someone's access withdrawn, someone refused. The
code applies them (``refusal_for``, called first by ``policy.decision.decide_now``): the assistant
does not weigh a directive, he is refused. Only the owner sets one and lifts one.

A time limit is the code's (``in_force``). An exception the owner said in words ("except the
family") is the assistant's to judge: the code lets through the call that names the directive it
is an exception to (the fact ``exception_of``), when the assistant makes it himself and the
directive has such words. A withdrawal or a refusal has no exception: the assistant never gives
back what the owner took.

A record under a withdrawal is ``withdrawn``, under a refusal ``refused``; lifting one leaves the
record ``gone`` or ``known``, with nothing given back.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Final, Literal
from uuid import UUID

from sqlalchemy import ColumnElement, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.accounts.sessions import revoke_sessions_of
from domo_core.devices.service import forget_devices_of
from domo_core.household.models import Invitation
from domo_core.llm.types import JsonObject
from domo_core.people.models import OwnerDirective
from domo_core.policy.actions import DescribedAction
from domo_core.policy.barrier import Refuse
from domo_core.policy.catalog import GIVES_ACCESS, MAKES_SOMEONE_NEW
from domo_core.policy.rights import pause_rights_and_audit
from domo_core.policy.roles import Actor

__all__ = [
    "GIVES_ACCESS",
    "MAKES_SOMEONE_NEW",
    "Kind",
    "apply_in_force",
    "directives_now",
    "end_expired",
    "for_others",
    "in_force",
    "lets_through",
    "lift",
    "lift_all_on",
    "note_exceptions",
    "refusal_for",
    "set_directive",
    "under_directive",
]

Kind = Literal["no_new_people", "no_keeping", "withdraw", "refuse"]

_ABOUT_A_PERSON: Final = ("withdraw", "refuse")
_ABOUT_NEW_PEOPLE: Final = ("no_new_people", "no_keeping")
_PERSON: Final = "person:"


def in_force(now: datetime) -> ColumnElement[bool]:
    """The directives in force at ``now``, as a condition on ``owner_directives``: not lifted, and
    not past their time limit (the code's, whatever anyone says)."""
    return OwnerDirective.lifted_at.is_(None) & (
        OwnerDirective.until.is_(None) | (OwnerDirective.until > now)
    )


async def directives_now(session: AsyncSession, now: datetime) -> list[OwnerDirective]:
    """The directives in force at ``now``, oldest first."""
    found = await session.execute(
        select(OwnerDirective)
        .where(in_force(now))
        .order_by(OwnerDirective.set_at, OwnerDirective.id)
    )
    return list(found.scalars())


async def under_directive(
    session: AsyncSession, person_id: UUID, now: datetime
) -> OwnerDirective | None:
    """The withdrawal or the refusal in force at ``now`` for that record, if there is one."""
    found = await session.execute(
        select(OwnerDirective).where(
            OwnerDirective.person_id == person_id,
            OwnerDirective.kind.in_(_ABOUT_A_PERSON),
            in_force(now),
        )
    )
    return found.scalars().first()


async def shown(session: AsyncSession, now: datetime) -> list[JsonObject]:
    """The directives in force at ``now``, as data for the assistant: what each is, the record it
    is about (its name and identifier) or what the owner said, the words that let some through and
    when it ends."""
    found: list[JsonObject] = []
    for directive in await directives_now(session, now):
        row: JsonObject = {"id": str(directive.id), "kind": directive.kind}
        if directive.person_id is not None:
            name = await session.scalar(
                select(Person.display_name).where(Person.id == directive.person_id)
            )
            row["person"] = f"person:{directive.person_id}"
            row["name"] = name
        if directive.description:
            row["description"] = directive.description
        if directive.except_words:
            row["except"] = directive.except_words
        if directive.until is not None:
            row["until"] = directive.until.isoformat()
        found.append(row)
    return found


def for_others(rows: Sequence[JsonObject]) -> tuple[JsonObject, ...]:
    """What of the directives shown (``shown``) is told in a turn that is not the owner's: only
    that nobody new is kept, with none of his words."""
    return tuple({"kind": "no_keeping"} for row in rows if row["kind"] == "no_keeping")


def _only_the_owner(owner: Person) -> None:
    if owner.role != "owner":
        raise ValueError("only the owner sets or lifts a directive")


async def set_directive(
    session: AsyncSession,
    *,
    kind: Kind,
    person: Person | None,
    description: str | None,
    except_words: str | None,
    until: datetime | None,
    owner: Person,
    now: datetime,
) -> OwnerDirective:
    """The owner's directive, in force from ``now``. One in force exists per kind and record: a
    new word on the same one replaces what it said. A withdrawal moves the record to ``withdrawn``
    and a refusal to ``refused``; either cuts the person's sessions and devices as a removal
    does, and pauses the rights the owner gave them themselves. A promise kept for them stays as it
    is (a promise ends once): only the access it gave is taken back.
    ``ValueError`` unless ``owner`` is the owner, the record is named for a withdrawal or a
    refusal and for no other kind, and it is not the owner's. Flushed, not committed."""
    _only_the_owner(owner)
    if (kind in _ABOUT_A_PERSON) != (person is not None):
        raise ValueError("a withdrawal or a refusal names a record, no other directive does")
    if person is not None and person.role == "owner":
        raise ValueError("the owner is never under a directive")
    person_id = None if person is None else person.id
    existing = (
        await session.execute(
            select(OwnerDirective).where(
                OwnerDirective.kind == kind,
                OwnerDirective.person_id.is_(None)
                if person_id is None
                else OwnerDirective.person_id == person_id,
                OwnerDirective.lifted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        directive = OwnerDirective(
            kind=kind,
            person_id=person_id,
            description=description,
            except_words=except_words,
            until=until,
            set_by=owner.id,
            set_at=now,
        )
        session.add(directive)
    else:
        directive = existing
        directive.description = description
        directive.except_words = except_words
        directive.until = until
        directive.set_by = owner.id
        directive.set_at = now
    await session.flush()
    if person is not None:
        await _take_the_access_away(session, person, kind, owner=owner, now=now)
    return directive


async def _take_the_access_away(
    session: AsyncSession, person: Person, kind: Kind, *, owner: Person, now: datetime
) -> None:
    """What a withdrawal or a refusal does to the record at once (``set_directive``)."""
    # Not at the top: ``household.service`` reads the directives to bring someone back.
    from domo_core.household.service import refuse_requests_of

    by = f"{_PERSON}{owner.id}"
    if kind == "withdraw":
        # Someone who left already keeps the date they left.
        values: dict[str, object] = {
            "state": "withdrawn",
            "disabled_at": func.coalesce(Person.disabled_at, now),
        }
    else:
        values = {"state": "refused"}
    await session.execute(
        update(Person)
        .where(Person.id == person.id)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    await revoke_sessions_of(session, person.id)
    await forget_devices_of(session, person.id, now)
    # Nothing they were given before stays redeemable: the links that wait are cancelled, the
    # requests that wait are refused, as for a removal.
    await session.execute(
        update(Invitation)
        .where(
            Invitation.person_id == person.id,
            Invitation.accepted_at.is_(None),
            Invitation.cancelled_at.is_(None),
        )
        .values(cancelled_at=now)
        .execution_options(synchronize_session=False)
    )
    await refuse_requests_of(session, person.id, now, by=owner.id)
    await pause_rights_and_audit(session, person.id, now, actor=by)
    await session.flush()
    await session.refresh(person)


async def apply_in_force(session: AsyncSession, person: Person, now: datetime) -> None:
    """What the withdrawal and the refusal in force for the record do to it, as when the owner set
    them (``set_directive``): a record merged with one under a directive ends as that directive
    says (``people.merge``). A refusal is applied last: it is the stricter. Flushed."""
    for kind in _ABOUT_A_PERSON:
        directive = (
            await session.execute(
                select(OwnerDirective).where(
                    OwnerDirective.person_id == person.id,
                    OwnerDirective.kind == kind,
                    in_force(now),
                )
            )
        ).scalar_one_or_none()
        if directive is None:
            continue
        owner = await session.get(Person, directive.set_by)
        if owner is not None:
            await _take_the_access_away(session, person, kind, owner=owner, now=now)


async def lift(
    session: AsyncSession, directive: OwnerDirective, *, owner: Person, now: datetime
) -> None:
    """The owner lifts a directive at ``now``. Nothing is given back: a withdrawn record becomes
    ``gone`` (the assistant may bring them back), a refused one ``known``. ``ValueError`` unless
    ``owner`` is the owner. Flushed, not committed."""
    _only_the_owner(owner)
    if directive.lifted_at is not None:
        return
    directive.lifted_at = now
    directive.lifted_by = owner.id
    await _settle(session, directive, now)
    await session.flush()


async def lift_all_on(
    session: AsyncSession, person_id: UUID, *, owner: Person, now: datetime
) -> bool:
    """The owner gives back what he took: every withdrawal and refusal in force for the record is
    lifted at ``now``, in one gesture (``lift`` each). Whether there was one. ``ValueError``
    unless ``owner`` is the owner. Flushed, not committed."""
    _only_the_owner(owner)
    found = (
        await session.execute(
            select(OwnerDirective)
            .where(
                OwnerDirective.person_id == person_id,
                OwnerDirective.kind.in_(_ABOUT_A_PERSON),
                in_force(now),
            )
            .order_by(OwnerDirective.set_at, OwnerDirective.id)
        )
    ).scalars()
    lifted = list(found)
    for directive in lifted:
        await lift(session, directive, owner=owner, now=now)
    return bool(lifted)


async def _settle(session: AsyncSession, directive: OwnerDirective, now: datetime) -> None:
    """Where a record stands once ``directive`` ended: as the other withdrawal or refusal still in
    force says, else ``gone`` (a withdrawal) or ``known`` (a refusal). Nothing is given back."""
    if directive.person_id is None:
        return
    person = await session.get(Person, directive.person_id)
    if person is None or person.state not in ("withdrawn", "refused"):
        return
    others = (
        await session.execute(
            select(OwnerDirective.kind).where(
                OwnerDirective.person_id == person.id,
                OwnerDirective.id != directive.id,
                OwnerDirective.kind.in_(_ABOUT_A_PERSON),
                in_force(now),
            )
        )
    ).scalars()
    kinds = set(others)
    if "withdraw" in kinds:
        person.state = "withdrawn"
    elif "refuse" in kinds:
        person.state = "refused"
    else:
        person.state = "gone" if directive.kind == "withdraw" else "known"


async def end_expired(session: AsyncSession, now: datetime) -> int:
    """End the directives whose time limit is past at ``now``, exactly as a lift does (nothing
    given back; no one lifted it: ``lifted_by`` stays empty). Every reader already counts them
    ended (``in_force``); this settles the records. Returns how many ended. Flushed, not
    committed."""
    found = await session.execute(
        select(OwnerDirective).where(
            OwnerDirective.lifted_at.is_(None),
            OwnerDirective.until.is_not(None),
            OwnerDirective.until <= now,
        )
    )
    ended = list(found.scalars())
    for directive in ended:
        directive.lifted_at = now
    await session.flush()
    for directive in ended:
        await _settle(session, directive, now)
    await session.flush()
    return len(ended)


def _named(value: object) -> UUID | None:
    """The record a text names (``person:<id>`` or the bare id), or ``None``."""
    if not isinstance(value, str):
        return None
    try:
        return UUID(value.removeprefix(_PERSON))
    except ValueError:
        return None


def _records_of(action: DescribedAction) -> list[UUID]:
    """The record an action names, when its target is one."""
    target = action.target
    named = [_named(target)] if target is not None and target.startswith(_PERSON) else []
    return [record for record in named if record is not None]


async def _is_someone_new(session: AsyncSession, action: DescribedAction) -> bool:
    """Whether what the action gives an access to is someone who never had one: no record, or a
    record whose first access is still to come."""
    records = _records_of(action)
    if not records:
        return True
    first = await session.scalar(select(Person.first_access_at).where(Person.id == records[0]))
    return first is None


def lets_through(blocking: Sequence[OwnerDirective], *, named: object, by_domo: bool) -> bool:
    """The assistant (``by_domo``) names, as the exception he judged, every directive that blocks
    (``named``: their ids, separated by commas), and each has words for one. Anyone else's naming
    changes nothing."""
    if not by_domo or not isinstance(named, str):
        return False
    ids = {part.strip() for part in named.split(",")}
    return all(str(d.id) in ids and d.except_words for d in blocking)


async def note_exceptions(
    session: AsyncSession,
    named: str | None,
    *,
    person_id: UUID | None,
    turn_of: UUID | None,
    now: datetime,
    invitation_id: UUID | None = None,
) -> None:
    """Trace each directive on new people in force at ``now`` that the assistant named as the
    exception he judged (``named``, the ids of ``exception_of``) and that has words for one: an
    act ``exception_used`` of his, with no reason (it would name them). Flushed."""
    if not named:
        return
    # Not at the top: ``people.acts`` reads the household, which reads this module.
    from domo_core.people.acts import note_act

    ids = {part.strip() for part in named.split(",")}
    for directive in await directives_now(session, now):
        if (
            directive.kind in _ABOUT_NEW_PEOPLE
            and directive.except_words
            and str(directive.id) in ids
        ):
            await note_act(
                session,
                act="exception_used",
                by="domo",
                reason=None,
                turn_of=turn_of,
                person_id=person_id,
                directive_id=directive.id,
                invitation_id=invitation_id,
                now=now,
            )


def _excepted(action: DescribedAction, actor: Actor, blocking: list[OwnerDirective]) -> bool:
    """``lets_through`` for the fact ``exception_of`` of the described action."""
    return lets_through(
        blocking, named=action.facts.get("exception_of"), by_domo=actor.subject == "domo"
    )


async def refusal_for(
    session: AsyncSession, actor: Actor, action: DescribedAction, now: datetime
) -> Refuse | None:
    """``Refuse("owner_directive")`` when a directive in force applies to the action, else
    ``None``: the owner's own request is never refused. The action is refused when it gives an
    access to a record under a withdrawal or a refusal (never excepted), or, while nobody new is
    wanted or kept, opens a record or gives a first access, unless the assistant names the
    directive whose words let it through (``exception_of``). A merge is never refused here: it
    only ever leaves the stricter state, with the directive on the record kept
    (``people.merge``), and it is how someone refused who came back under another name is found
    out. Reads only."""
    if actor.subject == "person" and actor.role == "owner":
        return None
    kind = action.action_type
    if kind in GIVES_ACCESS:
        for record in _records_of(action):
            if await under_directive(session, record, now) is not None:
                return Refuse("owner_directive")
    if kind not in MAKES_SOMEONE_NEW and kind not in GIVES_ACCESS:
        return None
    blocking = [d for d in await directives_now(session, now) if d.kind in _ABOUT_NEW_PEOPLE]
    if not blocking:
        return None
    if kind not in MAKES_SOMEONE_NEW and not await _is_someone_new(session, action):
        return None
    return None if _excepted(action, actor, blocking) else Refuse("owner_directive")
