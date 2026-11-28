"""The records of the people of the house: the one writer of where a record stands.

A record is a row of ``persons``, with or without a login account. This module opens one
(``known``: nobody is given anything by being known), completes it, gives it an access
(``give_access``: the single place ``state`` becomes ``has_access``), takes the access away
(``remove``) and erases a record that never had one (``erase``). Each act leaves its trace with
its reason (``people.acts.note_act``).

What the code holds here, whoever asks:

* the child mark is lifted by the owner, or by the assistant on a date of birth he knows that
  makes them of age (``PolicySettings.adult_age``); a date of birth given in the turn of the
  child it is about is kept as their word, never as the date the mark is lifted on;
* the owner's directives: no access is given to a record under one unless the owner does it
  (``UnderDirective``); an access the owner takes away is a withdrawal
  (``directives.set_directive``);
* a record that ever had access is never erased (that is forgetting, ``NotPossible``).

Whether a record may be opened, or an access given, at all is the barrier's and the judge's. The
functions here flush and do not commit: the caller does.
"""

from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Final
from uuid import UUID

from sqlalchemy import delete, or_, select, true, update
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Account, Person, Standing
from domo_core.accounts.sessions import is_active
from domo_core.household.nicknames import PersonNickname
from domo_core.household.service import MemberRole, Removed, disable
from domo_core.memory.models import Memory
from domo_core.memory.service import visible_to
from domo_core.people import directives, made_roles
from domo_core.people.acts import note_act
from domo_core.people.models import (
    ChannelRef,
    MadeRole,
    OwnerDirective,
    PersonAct,
    PersonLink,
)
from domo_core.people.news import news_for
from domo_core.policy.roles import DOMO_LABEL
from domo_core.policy.settings import load_policy_settings
from domo_core.policy.subjects import owner_of_now
from domo_core.text import display_name_value
from domo_core.trust.models import TrustFact

__all__ = [
    "NotPossible",
    "UnderDirective",
    "age_on",
    "attach",
    "complete",
    "erase",
    "give_access",
    "keeping_directives_against",
    "leave_role",
    "may_propose_telling",
    "only_shortens",
    "open_record",
    "owner_to_tell",
    "remove",
    "returns_now",
    "stay_may_end",
]

_PERSON: Final = "person:"
# The key of a date of birth a child gave in their own words, kept as a memory with its source.
BORN_ON: Final = "born_on"


class UnderDirective(Exception):
    """The owner's withdrawal or refusal is in force for the record: nobody but the owner gives it
    an access."""


class NotPossible(Exception):
    """What was asked cannot be done to the record as it stands. ``code`` says why, in a word the
    tool gives back to the assistant."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def age_on(born_on: date, now: datetime) -> int:
    """The age in whole years of someone born on ``born_on``, at ``now``."""
    had_birthday = (now.month, now.day) >= (born_on.month, born_on.day)
    return now.year - born_on.year - (0 if had_birthday else 1)


def _person_of(by: str) -> UUID | None:
    """The person a label ``person:<id>`` names, ``None`` for the assistant."""
    return UUID(by.removeprefix(_PERSON)) if by.startswith(_PERSON) else None


async def _owner_of(session: AsyncSession, by: str) -> Person | None:
    """The owner, when ``by`` is the owner; ``None`` otherwise (the assistant, or anyone else)."""
    who = _person_of(by)
    person = None if who is None else await session.get(Person, who)
    return person if person is not None and person.role == "owner" else None


def _a_record(person: Person) -> None:
    if person.merged_into_id is not None or person.forgotten_at is not None:
        raise NotPossible("not_a_record")


def _needs_a_reason(by: str, reason: str | None) -> None:
    if by == DOMO_LABEL and not reason:
        raise ValueError("an act of the assistant has its reason")


async def open_record(
    session: AsyncSession,
    *,
    name: str,
    standing: Standing,
    is_child: bool,
    role: MemberRole = "guest",
    by: str,
    reason: str | None,
    turn_of: UUID | None,
    now: datetime,
) -> Person:
    """A new record of someone who comes, lives here, will come, or matters, ``known``: with no
    access and no account. ``by`` is the assistant (``origin`` ``domo``, who keeps his ``reason``)
    or a person (``invitation``). The name is a display name (``InvalidDisplayName`` otherwise);
    **nobody checks that it is free**: two people may be called Tom. A child role is always the
    mark of a child. Flushed."""
    _needs_a_reason(by, reason)
    person = Person(
        display_name=display_name_value(name),
        role=role,
        state="known",
        standing=standing,
        is_child=is_child or role == "child",
        origin="domo" if by == DOMO_LABEL else "invitation",
        origin_reason=reason,
    )
    session.add(person)
    await session.flush()
    await note_act(
        session, act="opened", by=by, reason=reason, turn_of=turn_of, person_id=person.id, now=now
    )
    return person


async def complete(
    session: AsyncSession,
    person: Person,
    *,
    name: str | None = None,
    standing: Standing | None = None,
    is_child: bool | None = None,
    born_on: date | None = None,
    access_returns: bool | None = None,
    stay_until: datetime | None = None,
    memories: Sequence[UUID] = (),
    seen_by: UUID | None = None,
    by: str,
    reason: str | None,
    turn_of: UUID | None,
    now: datetime,
) -> Person:
    """Say more of a record: its name, where they stand, whether they carry the child mark, their
    date of birth, whether their access wakes at the next visit (``access_returns``: the
    assistant's gesture that gives it again at their next visit says ``returns``), the end of their
    stay (``stay_until``, which may be ``now``: they left; only for a guest or a visiting record
    without an account, ``stay_may_end``, and only to end or shorten a stay that runs,
    ``only_shortens``; else left as it is), and the memories (``memories``, free
    text until now) that are about them.

    A date of birth given in the turn of the child it is about, while they carry the mark, is
    kept as what they said (a memory ``born_on`` they said about themselves), never as the date
    the mark is lifted on.

    ``NotPossible``: ``not_a_record``; ``owner_no_mark`` for the mark of a child set on the owner;
    for lifting the mark, ``child_role`` (the
    built-in child role carries it: change the role), ``not_by_owner`` (a person who is not the
    owner), ``no_birth_date``, ``too_young`` (the assistant lifts it only on a date of birth he
    knows that makes them at least ``PolicySettings.adult_age``); ``memory_taken`` for a memory
    that is about someone else. A memory private to someone other than ``seen_by`` is
    skipped (``attach``). Flushed."""
    _a_record(person)
    _needs_a_reason(by, reason)
    if stay_until is not None and not (
        await stay_may_end(session, person) and only_shortens(person, stay_until, now)
    ):
        # The owner's stay never ends, and a member's access ends by a removal, not here. A later
        # end, or a stay that is over woken, gives an access: that is the judged gesture's.
        stay_until = None
    if is_child is True and person.role == "owner":
        # The owner never carries the mark of a child, by any path.
        raise NotPossible("owner_no_mark")
    carries_the_mark = person.is_child or is_child is True
    their_word = None
    if born_on is not None and carries_the_mark and turn_of == person.id:
        # A child's own word on their date of birth is kept as what they said (a memory with its
        # source), never as the date the mark is lifted on.
        their_word, born_on = born_on, None
    if is_child is False and person.is_child:
        await _may_lift_the_mark(session, person, born_on, by, now)
    attached = await _free_memories(session, person, memories)
    if name is not None:
        person.display_name = display_name_value(name)
    if standing is not None:
        person.standing = standing
    if is_child is not None:
        person.is_child = is_child or person.role == "child"
    if born_on is not None:
        person.born_on = born_on
    if access_returns is not None:
        person.access_returns = access_returns
    if stay_until is not None:
        # The end of their stay, in the future or now (the assistant judges that they left): once
        # it is past the access sleeps (the record is no longer active, it keeps its state, its
        # role and everything known of it), and the last review is owed as for any stay that ends.
        person.expires_at = stay_until
        person.last_review_due = True
    if their_word is not None:
        session.add(
            Memory(
                subject=person.display_name or BORN_ON,
                content=f"{BORN_ON}: {their_word.isoformat()}",
                kind="fact",
                origin="own_words",
                said_by_id=person.id,
                about_id=person.id,
            )
        )
        await session.flush()
    await attach(session, person, [memory.id for memory in attached], seen_by=seen_by)
    await note_act(
        session,
        act="completed",
        by=by,
        reason=reason,
        turn_of=turn_of,
        person_id=person.id,
        now=now,
    )
    return person


async def _may_lift_the_mark(
    session: AsyncSession, person: Person, born_on: date | None, by: str, now: datetime
) -> None:
    """``NotPossible`` unless the mark of a child may be lifted by ``by`` now: the owner, whatever
    the age, or the assistant on a date of birth he knows that makes them of age."""
    if person.role == "child":
        raise NotPossible("child_role")
    if await _owner_of(session, by) is not None:
        return
    if by != DOMO_LABEL:
        raise NotPossible("not_by_owner")
    known = born_on or person.born_on
    if known is None:
        raise NotPossible("no_birth_date")
    if age_on(known, now) < (await load_policy_settings(session)).adult_age:
        raise NotPossible("too_young")


async def _free_memories(
    session: AsyncSession, person: Person, ids: Sequence[UUID]
) -> list[Memory]:
    """The memories ``ids``, which may become about ``person``: free text, or already about them.
    ``NotPossible("memory_taken")`` for one that is about someone else, or that does not exist."""
    if not ids:
        return []
    found = list(await session.scalars(select(Memory).where(Memory.id.in_(set(ids)))))
    if len(found) != len(set(ids)) or any(
        memory.about_id not in (None, person.id) for memory in found
    ):
        raise NotPossible("memory_taken")
    return found


async def attach(
    session: AsyncSession,
    person: Person,
    memory_ids: Sequence[UUID],
    *,
    seen_by: UUID | None = None,
) -> int:
    """These memories, free text until now, are about ``person``: the number attached. A memory
    that is about someone else is never re-pointed (nor is one that does not exist); and nothing
    is attached to a record merged into another, forgotten or erased (0). With ``seen_by``, in
    that person's turn, only the memories that person may see are attached (``visible_to``): the
    others are skipped, not refused; ``None`` (a turn the assistant acts in alone) sees all.
    Flushed."""
    if not memory_ids or person.merged_into_id is not None or person.forgotten_at is not None:
        return 0
    result = await session.execute(
        update(Memory)
        .where(
            Memory.id.in_(set(memory_ids)),
            Memory.about_id.is_(None),
            true() if seen_by is None else visible_to(seen_by),
        )
        .values(about_id=person.id)
        .returning(Memory.id)
        .execution_options(synchronize_session="fetch")
    )
    return len(result.all())


async def keeping_directives_against(
    session: AsyncSession, person: Person, now: datetime
) -> list[OwnerDirective]:
    """The owner's ``no_keeping`` directives in force at ``now`` that were given before this
    record was opened (the time of its opening act, else its row's): nobody new is kept, so nothing
    is kept about them unless the assistant names the directive whose words let it through
    (``directives.lets_through``). A record already there when the owner ruled is kept as ever.
    Reads only."""
    opened = await session.scalar(
        select(PersonAct.at).where(PersonAct.person_id == person.id, PersonAct.act == "opened")
    )
    since = opened or person.created_at
    return [
        directive
        for directive in await directives.directives_now(session, now)
        if directive.kind == "no_keeping" and directive.set_at < since
    ]


async def leave_role(session: AsyncSession, person: Person, *, now: datetime) -> None:
    """The record leaves the role made for the household it carries, if any (it carries none
    afterwards). A role no other record carries and no promise that waits names is deleted at
    once, by the assistant, with the reason ``unused``. Called where a record leaves its role:
    another role given, an access the owner takes away, a record erased, forgotten or merged
    away. Flushed."""
    old_id = person.made_role_id
    if old_id is None:
        return
    person.made_role_id = None
    await session.flush()
    role = await made_roles.by_id(session, old_id)
    if role is None or role.deleted_at is not None:
        return
    if not await made_roles.is_wanted(session, role, besides=person.id):
        await made_roles.delete(
            session, role, by=DOMO_LABEL, reason=made_roles.UNUSED, turn_of=None, now=now
        )


async def give_access(
    session: AsyncSession,
    person: Person,
    *,
    role: MemberRole,
    made_role: MadeRole | None,
    standing: Standing | None,
    guest_days: int | None,
    by: str,
    by_owner: bool,
    reason: str | None,
    turn_of: UUID | None,
    now: datetime,
    seen: bool = True,
    no_end: bool = False,
) -> Person:
    """The record has access from now: the single place ``state`` becomes ``has_access``. It
    carries ``role`` (and ``made_role``, whose base is that role), and the mark of a child stays
    whatever the role (it is lifted by ``complete`` alone). A guest stays ``guest_days`` from now,
    with no end (``no_end``), or keeps the end of stay the record already has when it is still to
    come (none otherwise); any other role stays for good. No length is refused here. A record whose
    access sleeps (its stay is over; not one removed) and wakes at the next visit
    (``access_returns``) is given a new stay: the act says it (``returns``).

    ``UnderDirective`` when the owner's withdrawal or refusal is in force for the record, unless
    ``by_owner``: the owner, giving it back, lifts it. ``NotPossible("not_a_record")``. The first
    access is dated once. Nothing here asks who the record is: when it is the person of the turn,
    the described action says so (``serves_the_speaker``) and the judge weighs it. ``seen``: the
    person of the turn sees it happen (``people.whose.seen``); an access owed to the owner is told
    to him at once only in his own turn that he sees, never in a review. Flushed."""
    _a_record(person)
    _needs_a_reason(by, reason)
    if made_role is not None and made_role.base != role:
        raise ValueError("a made role is given with its base role")
    directive = await directives.under_directive(session, person.id, now)
    if directive is not None:
        owner = await _owner_of(session, by) if by_owner else None
        if owner is None:
            raise UnderDirective
        # Every withdrawal and refusal of his on the record: giving it back lifts them all.
        await directives.lift_all_on(session, person.id, owner=owner, now=now)
    returns = returns_now(person, now)
    if guest_days is not None and not no_end:
        # A guest's stay, or the end of anyone else's access the owner asked for (their role is
        # unchanged: the owner's answer of 2026-11-27).
        person.expires_at = now + timedelta(days=guest_days)
        # A stay ends by itself: the last review it is owed once it is over (``memory.review``).
        person.last_review_due = True
    elif role != "guest" or no_end or person.expires_at is None or person.expires_at <= now:
        person.expires_at = None
    if person.made_role_id is not None and (
        made_role is None or made_role.id != person.made_role_id
    ):
        await leave_role(session, person, now=now)
    person.role = role
    person.is_child = person.is_child or role == "child"
    person.made_role_id = None if made_role is None else made_role.id
    if standing is not None:
        person.standing = standing
    person.state = "has_access"
    person.disabled_at = None
    # Someone new, given access by the assistant on his own, while the owner asked to be told of
    # each: the act is owed to the owner, and his news is kept so that a message always leaves (the
    # caller tells the core once it committed: ``owner_to_tell``).
    first_time = person.first_access_at is None
    owed = (
        first_time
        and by == DOMO_LABEL
        and (await load_policy_settings(session)).tell_access is True
    )
    if first_time:
        person.first_access_at = now
    await session.flush()
    act = await note_act(
        session,
        act="access_given",
        by=by,
        reason=reason,
        turn_of=turn_of,
        person_id=person.id,
        owed_to_owner=owed,
        returns=returns,
        now=now,
    )
    if owed and seen and await _is_the_owner(session, turn_of):
        # Given in the owner's own turn: the turn is the message, he is told as it happens.
        act.told_owner_at = now
    elif owed:
        owner = await owner_to_tell(session, person, now)
        if owner is not None:
            await news_for(session, owner, now=now)
    return person


async def stay_may_end(session: AsyncSession, person: Person) -> bool:
    """Whether ``complete`` may set the end of ``person``'s stay: a guest, or a visiting record
    without an account; never the owner. For anyone else, ending their access is a removal
    (``remove``, the judged ``household.remove``)."""
    if person.role == "owner":
        return False
    if person.role == "guest":
        return True
    if person.standing != "visiting":
        return False
    has_account = await session.scalar(select(Account.id).where(Account.person_id == person.id))
    return has_account is None


def only_shortens(person: Person, stay_until: datetime, now: datetime) -> bool:
    """Whether ending ``person``'s stay at ``stay_until`` only ends or shortens a stay that runs
    at ``now``: they have access now, and the new end is no later than the one they have (any end
    when they have none). A later end, or a stay already over, gives an access: that goes through
    ``give_access`` (the judged ``household.change_role`` or ``household.bring_back``)."""
    if not is_active(person, now):
        return False
    return person.expires_at is None or stay_until <= person.expires_at


def returns_now(person: Person, now: datetime) -> bool:
    """Whether giving ``person`` access at ``now`` wakes an access that sleeps: they had one
    (``first_access_at``), their stay is over (never someone removed: ``gone``, or
    ``disabled_at``), and it wakes at their next visit (``access_returns``). Still under the
    owner's directives."""
    stay_over = (
        person.state == "has_access"
        and person.disabled_at is None
        and person.expires_at is not None
        and person.expires_at <= now
    )
    return person.access_returns and person.first_access_at is not None and stay_over


async def _is_the_owner(session: AsyncSession, turn_of: UUID | None) -> bool:
    """Whether the turn the act is made in is the owner's (``turn_of``; none in a turn the
    assistant acts in on his own)."""
    who = None if turn_of is None else await session.get(Person, turn_of)
    return who is not None and who.role == "owner"


async def owner_to_tell(session: AsyncSession, person: Person, now: datetime) -> UUID | None:
    """The owner of now, when the access given to ``person`` at ``now`` (the act of that call) is
    owed to him and he was not told yet: whom the caller tells the core of once it committed
    (``ctx.notify``; a review, which has no ``ctx.notify``, calls
    ``conversation.starts.news_kept``). ``None`` when nothing is owed."""
    owed = await session.scalar(
        select(PersonAct.id)
        .where(
            PersonAct.act == "access_given",
            PersonAct.person_id == person.id,
            PersonAct.at == now,
            PersonAct.owed_to_owner.is_(True),
            PersonAct.told_owner_at.is_(None),
        )
        .limit(1)
    )
    if owed is None:
        return None
    try:
        return await owner_of_now(session, now)
    except LookupError:
        return None


async def may_propose_telling(session: AsyncSession, person: Person, *, by: str) -> bool:
    """Whether the assistant, giving ``person`` access on his own (``by``), may propose to the
    owner to be told of each: it is the first time he does it (no ``access_given`` of his is
    traced yet), the record had none before, and the owner never said (``people.tell_access``).
    Read before ``give_access``, which writes the act."""
    if by != DOMO_LABEL or person.first_access_at is not None:
        return False
    if (await load_policy_settings(session)).tell_access is not None:
        return False
    before = await session.scalar(
        select(PersonAct.id)
        .where(PersonAct.act == "access_given", PersonAct.by == DOMO_LABEL)
        .limit(1)
    )
    return before is None


async def remove(
    session: AsyncSession,
    person: Person,
    *,
    by: str,
    by_owner: bool,
    reason: str | None,
    turn_of: UUID | None,
    now: datetime,
) -> Removed:
    """The record has no access any more. The assistant's removal leaves it ``gone`` (he may bring
    them back); the owner's is a withdrawal (``directives.set_directive``): ``withdrawn``, and
    nobody but the owner gives it back (ruling 13). Their sessions, devices and requests that wait
    end as a removal does (``household.service.disable``). The owner is never removed, and a
    record with no access is removed by the owner alone (a withdrawal that is only in advance).
    ``NotPossible``: ``not_a_record``, ``the_owner``, ``no_access``. Flushed."""
    _a_record(person)
    _needs_a_reason(by, reason)
    if person.role == "owner":
        raise NotPossible("the_owner")
    owner = await _owner_of(session, by) if by_owner else None
    if person.state != "has_access" and owner is None:
        raise NotPossible("no_access")
    removed = Removed(0, 0)
    if person.state == "has_access":
        who = _person_of(by)
        removed = await disable(session, person, now, by="domo" if who is None else who)
    if owner is not None:
        await leave_role(session, person, now=now)
        await directives.set_directive(
            session,
            kind="withdraw",
            person=person,
            description=None,
            except_words=None,
            until=None,
            owner=owner,
            now=now,
        )
    await session.refresh(person)
    await note_act(
        session,
        act="access_removed",
        by=by,
        reason=reason,
        turn_of=turn_of,
        person_id=person.id,
        now=now,
    )
    return removed


async def erase(
    session: AsyncSession,
    person: Person,
    *,
    by: str,
    reason: str | None,
    turn_of: UUID | None,
    now: datetime,
) -> list[UUID]:
    """A record opened by mistake, or that nothing comes of, goes: the memories about them become
    free text again (kept, ``about_id`` empty: their ids are returned), their links, nicknames,
    trust facts, channel references and the reasons of the acts done on them are deleted (the act
    of erasing keeps none either), and the row is emptied (``forget.empty_row``). Only a record
    that never had access and has no account: ``NotPossible("has_been_here")`` otherwise (that is
    forgetting, ``people.forget``), and ``NotPossible("under_directive")`` while the owner's
    withdrawal or refusal is in force for it (erasing would hide it). Flushed."""
    _a_record(person)
    _needs_a_reason(by, reason)
    if await directives.under_directive(session, person.id, now) is not None:
        raise NotPossible("under_directive")
    has_account = await session.scalar(select(Account.id).where(Account.person_id == person.id))
    if person.first_access_at is not None or person.state != "known" or has_account is not None:
        raise NotPossible("has_been_here")
    freed = list(
        await session.scalars(
            update(Memory)
            .where(Memory.about_id == person.id)
            .values(about_id=None)
            .returning(Memory.id)
            .execution_options(synchronize_session=False)
        )
    )
    await session.execute(
        delete(PersonLink).where(
            or_(PersonLink.from_id == person.id, PersonLink.to_id == person.id)
        )
    )
    await session.execute(
        delete(PersonNickname).where(
            or_(PersonNickname.person_id == person.id, PersonNickname.target_id == person.id)
        )
    )
    await session.execute(delete(TrustFact).where(TrustFact.person_id == person.id))
    await session.execute(delete(ChannelRef).where(ChannelRef.person_id == person.id))
    await session.execute(
        update(PersonAct)
        .where(or_(PersonAct.person_id == person.id, PersonAct.other_id == person.id))
        .values(reason=None)
        .execution_options(synchronize_session=False)
    )
    await leave_role(session, person, now=now)
    # Not at the top: ``people.forget`` builds on this module.
    from domo_core.people.forget import empty_row

    await empty_row(session, person, now)
    # The act keeps no reason either: it would name them.
    await note_act(
        session, act="erased", by=by, reason=None, turn_of=turn_of, person_id=person.id, now=now
    )
    return freed
