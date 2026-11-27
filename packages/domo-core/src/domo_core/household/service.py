"""The household: who is in it, the invitations to join it, and the owner's changes to it.

Who is in the household
-----------------------

A person is in the household while they are active (``accounts.sessions.is_active``): not removed
(``disabled_at``) and, for a guest, before the end of their stay (``expires_at``). Domo and the
owner name people by their display name: ``resolve_person`` finds the one person who has that
name among the people in the household now (or, when nobody has it, whose first name it is, or
the beginning of whose name: spec 6.7). Two names are the same name when they read the same
(``text.name_key``: whatever the case, the spaces, the invisible characters or the accents'
encoding). A name that means nobody is ``not_found``, one that means several people is
``ambiguous``: nothing is guessed (Review Focus 5). The same comparison keeps a name from being
copied (R81): a name that reads like another cannot be taken, so nobody can speak as someone else.

Invitations (decision D3)
-------------------------

The owner, or whoever the owner gave the right, invites someone with a name and a role (adult,
child or guest; never owner) and, for a guest, the number of days they may stay once they have
joined (2 by default). The household's rule is 30 days at most, unless the owner chose otherwise:
that is a limit of the catalog (``policy.catalog.DEFAULT_LIMITS``), which the barrier applies; here
the stay is only kept within a technical ceiling (1 to ``GUEST_DAYS_CEILING`` days). The invitation
is a link, single-use and valid for seven days (``INVITATION_TTL``). Its token is 256 random bits:
the raw token is returned once, by ``create_invitation``, for the one who asked to see; only its
SHA-256 is stored, so a leak of the database does not leak working links.

The link is for the one who asked. When someone else said yes (the owner, to an invitation Alex
asked for), the invitation is made without a token (``with_token=False``: its hash is empty), and
the one who asked makes the token, and sees the link, once, with a button on their card
(``reveal_link``). Nobody else can, and it can be made once: the hash is then set.

A link can be given again while its invitation waits (2026-11-16, the owner's trial of 0.9.0):
``give_link_again`` replaces the token, so the old link stops working, and the invitation keeps
everything else, its end included. The new link is shown once too, and stored nowhere; after
someone else's yes, or when nobody can be shown it, the old link is cleared and the new one waits
for the one who asked, behind the button of their card (``link_for_id``, ``reveal_link``).

Joining takes two steps, with the slow hash of the password between them. ``check_joining`` checks
the link, the username and the password, and only reads: the caller ends its transaction then, and
hashes the password on the limited path of argon2 (``accounts.passwords.hash_password_limited``),
which may have to wait for its turn, without holding a connection of the database (R79). Then
``accept_invitation`` turns the invitation into a person and an account: it locks the invitation's
row and checks it again, so that two people who use one link at the same time cannot both get in.

What keys the invitee uses (0.6.0, spec 4.3, 5.6, 11) is decided as they join: the invitation's
word (``uses_inviter_key``), else the household's default (``keys.invitee_default``), else their
inviter's. Their inviter's keys: a line lets them use each key their inviter gave (written as the
inviter's: only the giver's word lets someone use a key), unless the inviter is the owner, whose
general key is the household's already. They follow their inviter on the general key with no line
(``llm.router.allowed``), unless the household's rule is that invitees who join never use it
(``keys.invitee_general`` ``never``): a line then takes them off the general key, unless their
inviter gave it. Their own: a line takes them off the general key.

Coming back
-----------

Someone who left the household (removed, or a guest whose stay is over) keeps their account and
everything they wrote; only their access is closed. The owner brings them back with a *return
link*: an invitation tied to that person (``Invitation.person_id``) with the role, the days and the
display name they come back with. It is made like any invitation, with the same delay and the same
"shown once" token (``create_return_invitation``), and nothing of the old access works before the
person uses it. A name that means several former members is ``ambiguous``, and Domo tells them
apart by the role they had and the day they left (``resolve_former_person``, ``left_on``).

Coming back takes the two steps of joining, with the slow hash of the new password between them:
``check_return`` reads, and ``accept_return`` locks the link and the person, checks them again and
brings the person back. The username does not change and is never asked again. The old password is
replaced, the passkeys are deleted (they can add new ones), the sessions end and the requests that
still waited for a yes are refused: of the old access, nothing is left. The rights the owner had
given to the person themselves were paused when they left (spec 5.7), and any still in force are
paused now (spec 5.12): the owner says which to give back. A return link and a joining link are
never taken for each other. A new return link replaces the one that waits for the same person: the
caller cancels the old one (``cancel_invitation``) and the name check leaves it out
(``name_in_use``, ``excluding_invitation``).

The owner's changes
-------------------

``set_role`` changes a role. Someone who becomes a guest gets an end of stay, as an invited guest
does (``guest_days`` from the change, 2 by default: R78); someone who stops being a guest keeps
none. ``disable`` removes someone: their account is disabled, every session they have ends, the
requests they made that wait for a yes are refused, and the rights the owner gave them themselves
are paused (spec 5.7; the upkeep does the same for anyone who left without it,
``household.upkeep``). Nothing they wrote is deleted.

Handing the owner's role over
-----------------------------

``transfer_ownership`` gives the owner's role to an adult of the household and makes the owner an
adult, in the caller's transaction. It is the one way the role moves (spec 8): the tool that asks
for it (``tools.household_admin``) is the owner's alone and waits for an explicit yes every time.
The two rows are locked first, in the order of their identifiers (so that two hand-overs never
wait for each other in circles), and checked again: the one who gives is the owner and in the
household, the one who receives an adult in the household, and not the same person. Then, in this
order, the owner becomes an adult and the adult becomes the owner (the unique index
``uq_persons_single_owner`` allows one owner at a time, so there are never two, and with one
transaction never none). The role has no end: the new owner's ``expires_at`` is cleared. In the
same transaction the requests of the new owner that waited for the owner's yes wait for their own
(there is no other owner to give it, and the owner does not say yes to their own request on
someone's behalf), their personal rights are paused (as at a return, spec 5.12), they are taken
off the owner's list of successors if they were on it (an owner is never their own successor: the
places after theirs move up, so the ranks stay from 1 without a gap, and the old owner is not
added: nothing is automatic), and the audit entry ``household.ownership_transferred`` is written
last: who, to whom (identifiers and names), and how many requests, rights and places on the list
were moved. ``ownership_note`` finds that entry again, for the new owner's context, for
``OWNERSHIP_NOTE_DAYS`` days: that note, and only it, ends.

None of these functions commits: the caller owns the transaction (and writes the audit entry last).
"""

import hashlib
import secrets
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Final, Literal, cast, get_args, overload
from uuid import UUID

from sqlalchemy import ColumnElement, and_, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Account, Person
from domo_core.accounts.passkey_models import PasskeyCredential
from domo_core.accounts.passwords import MIN_PASSWORD_LENGTH
from domo_core.accounts.sessions import active_at, is_active, revoke_sessions_of
from domo_core.accounts.usernames import normalize_username
from domo_core.audit.log import append_audit, readable_ts
from domo_core.audit.models import AuditEntry
from domo_core.confirmations.models import Confirmation
from domo_core.devices.service import forget_devices_of
from domo_core.household import nicknames
from domo_core.household.models import Invitation, Successor
from domo_core.llm.models import Provider, ProviderUser
from domo_core.llm.types import JsonObject
from domo_core.policy.rights import pause_personal_rights, pause_rights_and_audit
from domo_core.policy.settings import load_policy_settings
from domo_core.prompts import OwnershipNote
from domo_core.text import InvalidDisplayName, display_name_value, is_plain_line, name_key
from domo_core.tools.context import InvalidArguments

# How long an invitation link works.
INVITATION_TTL: Final = timedelta(days=7)
# How long a guest stays once they have joined, unless the owner said otherwise; and the
# technical ceiling of a stay (the household's rule, 30 days unless the owner chose otherwise, is a
# limit of the catalog, ``policy.catalog.DEFAULT_LIMITS``). The ``invitations`` table says the
# same.
GUEST_DAYS_DEFAULT: Final = 2
GUEST_DAYS_CEILING: Final = 365
# The longest username (the same limit as the owner's, at the first launch).
MAX_USERNAME_LENGTH: Final = 64
# The key of the advisory lock that serialises the claims on a display name (see
# ``lock_display_names``).
DISPLAY_NAMES_LOCK_KEY: Final = 4343
# The key of the advisory lock that serialises the changes of the list of successors
# (``household.succession`` takes it to change the list, and a hand-over to take the new owner off).
SUCCESSORS_LOCK_KEY: Final = 4345
# How long the new owner's context tells them that the role was handed to them (the role itself
# has no end). The line of the system prompt (``prompts.build_system_prompt``) says the same days.
OWNERSHIP_NOTE_DAYS: Final = 30
# The audit entry of a hand-over (``transfer_ownership``), which ``ownership_note`` reads back.
OWNERSHIP_TRANSFERRED: Final = "household.ownership_transferred"

# The roles an invitation, or a change of role, may give: never the owner's.
MemberRole = Literal["adult", "child", "guest"]
MEMBER_ROLES: Final[tuple[MemberRole, ...]] = get_args(MemberRole)


class InvitationUnavailable(Exception):
    """The link cannot be used: it names no invitation, or one that was already used, was
    cancelled or has expired (which of these is never told)."""


class InvalidUsername(Exception):
    """The username is empty, too long (``MAX_USERNAME_LENGTH``) or not one plain line."""


class UsernameTaken(Exception):
    """Someone already has this username."""


class WeakPassword(Exception):
    """The password is shorter than ``MIN_PASSWORD_LENGTH`` characters."""


class NameInUse(Exception):
    """Someone in the household, or an invitation that waits, already has this display name."""


def hash_token(raw_token: str) -> str:
    """The SHA-256 of an invitation's token, as stored (the token is 256 random bits: a plain
    hash is enough, there is nothing to brute-force)."""
    return hashlib.sha256(raw_token.encode("utf-8", "surrogatepass")).hexdigest()


def is_open(invitation: Invitation, now: datetime) -> bool:
    """Whether the invitation can still be used: not accepted, not cancelled, not expired."""
    return (
        invitation.accepted_at is None
        and invitation.cancelled_at is None
        and invitation.expires_at > now
    )


def open_at(now: datetime) -> ColumnElement[bool]:
    """``is_open`` as a condition on ``invitations``, for a query."""
    return and_(
        Invitation.accepted_at.is_(None),
        Invitation.cancelled_at.is_(None),
        Invitation.expires_at > now,
    )


async def active_persons(session: AsyncSession, now: datetime) -> list[Person]:
    """The people in the household at ``now``, in the order they arrived."""
    result = await session.execute(
        select(Person).where(active_at(now)).order_by(Person.created_at, Person.id)
    )
    return list(result.scalars())


async def resolve_person(
    session: AsyncSession,
    name: str,
    *,
    now: datetime,
    whole_name_only: bool = False,
    speaker: UUID | None = None,
    exact_only: bool = False,
) -> Person:
    """The one person in the household at ``now`` that ``name`` means (spec 6.7). Names are
    compared as they read (``text.name_key``: the case, the spaces and invisible characters do not
    count), among the people in the household alone (never someone removed, nor a guest whose
    stay is over), in four steps, each tried only when the one before finds nobody:

    1. the whole display name;
    2. a nickname the person who speaks (``speaker``) keeps for someone in the household
       (``household.nicknames``: "Juju" for whoever they mean by it); never someone else's
       nickname, and nothing without a speaker;
    3. the first word of the display name ("Alex" for "Alex Martin");
    4. the beginning of the display name ("Sam" for "Samuel"), which starts where the name starts:
       a piece from inside a word ("muel") or from a later word ("Martin") means nobody.

    So "Sam" is Sam when there is one, never Samuel, whatever nickname anyone keeps: a whole name
    always means its owner. ``whole_name_only`` keeps to the first step, for a caller that looks
    somewhere else before a shorter name may count; ``exact_only`` to the first two, for a caller
    that must not take a first name or a beginning for a member (a later step of a chain, which
    may name someone who has not joined yet by a shorter form of their name).

    A name that is the whole name of an invitation that waits never goes past the first step:
    that name is someone who has not joined yet, never a member whose name begins the same way
    nor one a nickname means ("remove Sam", while Sam's invitation waits, removes nobody, not
    Samuel). The refusal then names the members it could have meant, for Domo to ask.

    ``InvalidArguments("not_found")`` when nobody matches, ``InvalidArguments("ambiguous")`` when
    several people do: nobody is picked at random. When a first name or a beginning matches
    several people, the refusal gives their whole names, so that Domo can choose with what he
    knows or ask (the names of the household are in his context already)."""
    key = name_key(name)
    persons = await active_persons(session, now) if key else []
    matches = [p for p in persons if name_key(p.display_name) == key]
    if len(matches) > 1:
        raise InvalidArguments(
            "ambiguous", "several people in the household have this name: ask which one is meant"
        )
    if matches:
        return matches[0]
    if whole_name_only or not key:
        raise InvalidArguments("not_found", "nobody in the household has this name")
    invited = any(name_key(i.display_name) == key for i in await open_invitations(session, now))
    if speaker is not None and not invited:
        nicknamed = await nicknames.meant(session, speaker, name, now=now)
        if nicknamed is not None:
            return nicknamed
    if exact_only and not invited:
        raise InvalidArguments("not_found", "nobody in the household has this name")
    loose = [p for p in persons if name_key(p.display_name).split(" ")[0] == key] or [
        p for p in persons if name_key(p.display_name).startswith(key)
    ]
    names = ", ".join(p.display_name for p in loose)
    if invited:
        detail = "no member has this name yet: an invitation that waits has it"
        raise InvalidArguments(
            "not_found", f"{detail}; did you mean: {names}?" if loose else detail
        )
    if len(loose) > 1:
        raise InvalidArguments("ambiguous", f"several people match: {names}")
    if not loose:
        raise InvalidArguments("not_found", "nobody in the household has this name")
    return loose[0]


async def whole_names(
    session: AsyncSession,
    arguments: JsonObject,
    names: Sequence[str],
    *,
    now: datetime,
    speaker: UUID | None,
    leave: AbstractSet[str] = frozenset(),
    exact_only: bool = False,
) -> JsonObject:
    """``arguments`` with each name among the arguments ``names`` (a ``tools.context.NamesPeople``
    tool's ``person_arguments``) replaced by the whole display name of the one person of the
    household it means now to the person who speaks (``resolve_person`` with ``speaker``, whose
    nicknames count; ``None`` for none): "Sam" becomes "Samuel" when Samuel is the one it means. A
    name that means nobody or several, or whose key (``text.name_key``) is in ``leave`` (someone a
    chain is about to invite, say), stays as it was given: the tool refuses it, or reads it,
    itself. With ``exact_only``, only a whole name or a nickname of the speaker is replaced: a
    first name or a beginning stays as it was given (``resolve_person``). Reads only."""
    given: JsonObject = dict(arguments)
    for argument in names:
        value = given.get(argument)
        if not isinstance(value, str) or name_key(value) in leave:
            continue
        try:
            given[argument] = (
                await resolve_person(
                    session, value, now=now, speaker=speaker, exact_only=exact_only
                )
            ).display_name
        except InvalidArguments:
            continue
    return given


async def open_invitations(session: AsyncSession, now: datetime) -> list[Invitation]:
    """The invitations that can still be used at ``now``, oldest first."""
    result = await session.execute(
        select(Invitation).where(open_at(now)).order_by(Invitation.created_at, Invitation.id)
    )
    return list(result.scalars())


async def resolve_invitation(
    session: AsyncSession, display_name: str, *, now: datetime
) -> Invitation:
    """The one open invitation made for ``display_name`` (the same rule as ``resolve_person``):
    ``InvalidArguments("not_found")`` or ``InvalidArguments("ambiguous")`` otherwise."""
    key = name_key(display_name)
    matches = [i for i in await open_invitations(session, now) if name_key(i.display_name) == key]
    if not matches:
        raise InvalidArguments("not_found", "no invitation that waits was made for this name")
    if len(matches) > 1:
        raise InvalidArguments("ambiguous", "several invitations that wait have this name")
    return matches[0]


async def name_in_use(
    session: AsyncSession,
    name: str,
    *,
    now: datetime,
    excluding: UUID | None = None,
    excluding_invitation: UUID | None = None,
) -> bool:
    """Whether someone in the household, or an invitation that waits, already has this name: a
    second one would make the name ambiguous for every later action, and a copy of a name lets
    someone speak as another. A name that reads like another one is the same name
    (``text.name_key``: an invisible character, a doubled space or full-width letters do not make
    a different name).

    ``excluding`` is a person who does not count: the one who is renaming themselves may keep
    their name, or change its case. ``excluding_invitation`` is an invitation that does not count:
    the return link that waits for a person, which a new link for them replaces, carries the very
    name they come back with."""
    key = name_key(name)
    persons = await active_persons(session, now)
    invitations = await open_invitations(session, now)
    return any(name_key(p.display_name) == key for p in persons if p.id != excluding) or any(
        name_key(i.display_name) == key for i in invitations if i.id != excluding_invitation
    )


async def lock_display_names(session: AsyncSession) -> None:
    """Take, until the end of the transaction, the lock that serialises the claims on a display
    name: whoever sets a name (a rename, an invitation) takes it before ``name_in_use`` and keeps
    it until their commit, so that two of them cannot both find a name free and both take it.
    Names are few and set rarely, so one lock for all of them costs nothing."""
    await session.execute(select(func.pg_advisory_xact_lock(DISPLAY_NAMES_LOCK_KEY)))


async def rename_person(session: AsyncSession, person: Person, name: str, *, now: datetime) -> bool:
    """Give the person the display name ``name`` (already checked with ``display_name_value``).

    ``NameInUse`` when someone else in the household, or an invitation that waits, has it (the
    person's own name does not count: they may change its case). Returns ``False`` when the
    person no longer exists. Takes the lock of the display names; flushed, not committed."""
    await lock_display_names(session)
    if await name_in_use(session, name, now=now, excluding=person.id):
        raise NameInUse
    renamed = await session.execute(
        update(Person).where(Person.id == person.id).values(display_name=name).returning(Person.id)
    )
    await session.flush()
    return renamed.scalar_one_or_none() is not None


def _guest_days(role: MemberRole, guest_days: int | None) -> int | None:
    """The days of an invitation's stay: ``GUEST_DAYS_DEFAULT`` for a guest who was given none,
    nothing for anyone else. ``ValueError`` for days out of range, or days given to a role that
    does not end."""
    if role != "guest":
        if guest_days is not None:
            raise ValueError("only a guest's stay has a number of days")
        return None
    days = GUEST_DAYS_DEFAULT if guest_days is None else guest_days
    if not 1 <= days <= GUEST_DAYS_CEILING:
        raise ValueError(f"a guest stays 1 to {GUEST_DAYS_CEILING} days")
    return days


async def _make_invitation(
    session: AsyncSession,
    *,
    display_name: str,
    role: MemberRole,
    guest_days: int | None,
    created_by: UUID,
    now: datetime,
    person_id: UUID | None,
    with_token: bool,
    uses_inviter_key: bool | None = None,
) -> tuple[Invitation, str | None]:
    """The invitation of ``create_invitation`` and ``create_return_invitation``, tied to the
    person it brings back (``person_id``) or to nobody. Without a token (``with_token`` false) it
    waits for its link (``reveal_link``). Flushed, not committed."""
    if role not in MEMBER_ROLES:
        raise ValueError("an invitation is for an adult, a child or a guest")
    try:
        name = display_name_value(display_name)
    except InvalidDisplayName as refused:
        raise ValueError(f"not a display name ({refused.code})") from None
    token = secrets.token_urlsafe(32) if with_token else None
    invitation = Invitation(
        token_hash=None if token is None else hash_token(token),
        display_name=name,
        role=role,
        guest_days=_guest_days(role, guest_days),
        created_by=created_by,
        created_at=now,
        expires_at=now + INVITATION_TTL,
        person_id=person_id,
        uses_inviter_key=uses_inviter_key,
    )
    session.add(invitation)
    await session.flush()
    return invitation, token


@overload
async def create_invitation(
    session: AsyncSession,
    *,
    display_name: str,
    role: MemberRole,
    guest_days: int | None,
    created_by: UUID,
    now: datetime,
    with_token: Literal[True] = True,
    uses_inviter_key: bool | None = None,
) -> tuple[Invitation, str]: ...


@overload
async def create_invitation(
    session: AsyncSession,
    *,
    display_name: str,
    role: MemberRole,
    guest_days: int | None,
    created_by: UUID,
    now: datetime,
    with_token: bool,
    uses_inviter_key: bool | None = None,
) -> tuple[Invitation, str | None]: ...


async def create_invitation(
    session: AsyncSession,
    *,
    display_name: str,
    role: MemberRole,
    guest_days: int | None,
    created_by: UUID,
    now: datetime,
    with_token: bool = True,
    uses_inviter_key: bool | None = None,
) -> tuple[Invitation, str | None]:
    """Make an invitation, valid until ``now + INVITATION_TTL``, and return it with its raw token.

    The raw token is returned this once and stored nowhere (only its SHA-256 is): the caller shows
    it to the one who asked, and to nobody and nothing else. When someone else said yes, the
    invitation is made without a token (``with_token`` false, and the token returned is ``None``)
    and waits for its link: ``reveal_link`` makes the token when the one who asked asks for it.
    ``uses_inviter_key`` is whether the invitee uses the keys of the one who invites them
    (``False``: they bring their own; ``None``: not said, the household's default decides when
    they join, ``accept_invitation``).
    ``ValueError`` for a role that is not an adult's, a child's or a guest's, or days that make no
    sense (``_guest_days``), or a name that is not a display name (``text.display_name_value``).
    Flushed, not committed."""
    return await _make_invitation(
        session,
        display_name=display_name,
        role=role,
        guest_days=guest_days,
        created_by=created_by,
        now=now,
        person_id=None,
        with_token=with_token,
        uses_inviter_key=uses_inviter_key,
    )


async def reveal_link(
    session: AsyncSession, invitation_id: UUID, person_id: UUID, now: datetime
) -> str:
    """Make the token of an invitation that waits for its link, and return it: this once, and
    stored nowhere (only its SHA-256 is).

    Only for the one the link waits for (``link_holder``: the one who made the invitation, or the
    one who asked for it again), while it is open
    and has no token yet; ``InvitationUnavailable`` otherwise (it is not theirs, it is closed, or
    its link was made already: which of these is not told). The row is locked and checked again,
    so that two requests at once cannot both be given a link. Flushed, not committed."""
    locked = await session.execute(
        select(Invitation)
        .where(Invitation.id == invitation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    invitation = locked.scalar_one_or_none()
    if (
        invitation is None
        or link_holder(invitation) != person_id
        or not is_open(invitation, now)
        or invitation.token_hash is not None
    ):
        raise InvitationUnavailable
    token = secrets.token_urlsafe(32)
    invitation.token_hash = hash_token(token)
    await session.flush()
    return token


def link_holder(invitation: Invitation) -> UUID:
    """Whom the link of ``invitation`` is for, when it waits behind the "show the link" button:
    the one who asked for it again (``link_for_id``), else the one who made it."""
    return invitation.link_for_id or invitation.created_by


@overload
async def give_link_again(
    session: AsyncSession, invitation_id: UUID, now: datetime, *, waits_for: None = None
) -> str: ...


@overload
async def give_link_again(
    session: AsyncSession, invitation_id: UUID, now: datetime, *, waits_for: UUID
) -> None: ...


async def give_link_again(
    session: AsyncSession, invitation_id: UUID, now: datetime, *, waits_for: UUID | None = None
) -> str | None:
    """Give an invitation that waits a new link. The old link stops working at once; the
    invitation keeps its name, its role, its stay, its word on the keys and its end
    (``expires_at``): only its link changes.

    Without ``waits_for``, the new token is made and returned: this once, and stored nowhere (only
    its SHA-256 is). With it (someone else said yes, or nobody can be shown the link now), no token
    is made: the old one is cleared, and the link waits for that person, who makes it, once, with
    the button of their card (``reveal_link``), as an invitation does after someone else's yes.

    The row is locked and checked again: ``InvitationUnavailable`` when it was used, cancelled or
    has expired meanwhile. Who may ask for it is the barrier's business (the rights of inviting:
    ``policy.catalog.RIGHTS_OF``). Flushed, not committed."""
    locked = await session.execute(
        select(Invitation)
        .where(Invitation.id == invitation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    invitation = locked.scalar_one_or_none()
    if invitation is None or not is_open(invitation, now):
        raise InvitationUnavailable
    if waits_for is not None:
        invitation.token_hash = None
        invitation.link_for_id = waits_for
        await session.flush()
        return None
    token = secrets.token_urlsafe(32)
    invitation.token_hash = hash_token(token)
    invitation.link_for_id = None
    await session.flush()
    return token


async def invitation_of(session: AsyncSession, token: str, now: datetime) -> Invitation | None:
    """The open invitation of a link's token, if there is one (nothing is locked)."""
    result = await session.execute(
        select(Invitation).where(Invitation.token_hash == hash_token(token), open_at(now))
    )
    return result.scalar_one_or_none()


def _username_from(username: str) -> str:
    name = normalize_username(username)
    if not name or len(name) > MAX_USERNAME_LENGTH or not is_plain_line(name):
        raise InvalidUsername
    return name


async def _username_exists(session: AsyncSession, username: str) -> bool:
    found = await session.execute(
        select(func.count()).select_from(Account).where(Account.username == username)
    )
    return found.scalar_one() > 0


@dataclass(frozen=True)
class Joining:
    """What ``check_joining`` found good: the invitation to accept, the username to create (as it
    is stored) and the display name the person will have."""

    invitation_id: UUID
    username: str
    display_name: str


async def check_joining(
    session: AsyncSession, *, token: str, username: str, password: str, now: datetime
) -> Joining:
    """Check what someone typed to join with the invitation of ``token``, before the slow hash of
    their password. Reads only, and locks nothing.

    ``InvitationUnavailable`` when the link cannot be used (a return link cannot: it is not for
    joining); ``InvalidUsername``, ``UsernameTaken`` or ``WeakPassword`` otherwise, and the link
    still works. The name is checked with the rule of every display name (R70)."""
    invitation = await invitation_of(session, token, now)
    if invitation is None or invitation.person_id is not None:
        # A return link brings someone back (``check_return``): it makes nobody new.
        raise InvitationUnavailable
    name = _username_from(username)
    # Characters, not bytes; and not trimmed: spaces may be part of a password.
    if len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPassword
    if await _username_exists(session, name):
        raise UsernameTaken
    try:
        display_name = display_name_value(invitation.display_name)
    except InvalidDisplayName:
        raise InvitationUnavailable from None
    return Joining(invitation_id=invitation.id, username=name, display_name=display_name)


async def accept_invitation(
    session: AsyncSession, joining: Joining, password_hash: str, *, now: datetime
) -> Person:
    """Join the household as ``check_joining`` found it good, with the hash of the password that
    was checked: the person (the invitation's name and role, who invited them, and for a guest the
    end of their stay, ``guest_days`` from ``now``) and their account are created, the keys they
    use are given (``_keys_of_the_invitee``), and the invitation is marked accepted. Returns the
    person.

    The invitation's row is locked first and checked again: ``InvitationUnavailable`` when it was
    used (or ended) since, ``UsernameTaken`` when the username was taken since. Flushed, not
    committed: after an exception the caller rolls back.
    """
    locked = await session.execute(
        select(Invitation)
        .where(Invitation.id == joining.invitation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    invitation = locked.scalar_one_or_none()
    if invitation is None or not is_open(invitation, now) or invitation.person_id is not None:
        # Someone else used it meanwhile (or it ended, or it is a return link).
        raise InvitationUnavailable
    stay = None
    if invitation.role == "guest":
        stay = now + timedelta(days=invitation.guest_days or GUEST_DAYS_DEFAULT)
    person = Person(
        display_name=joining.display_name,
        role=invitation.role,
        expires_at=stay,
        invited_by=invitation.created_by,
        # A stay ends by itself: the last review it is owed once it is over (``memory.review``).
        last_review_due=stay is not None,
    )
    session.add(person)
    await session.flush()
    try:
        async with session.begin_nested():
            session.add(
                Account(person_id=person.id, username=joining.username, password_hash=password_hash)
            )
    except IntegrityError:
        # Taken between the check and now.
        raise UsernameTaken from None
    await _the_means_of_a_new_account(session, person, now)
    await _keys_of_the_invitee(session, invitation, person, now)
    invitation.accepted_at = now
    invitation.accepted_person_id = person.id
    await session.flush()
    return person


async def _the_means_of_a_new_account(session: AsyncSession, person: Person, now: datetime) -> None:
    """The account of a person who joined has the means of proof every account has: its password."""
    # Imported here, not at the top: the proofs read the confirmations, which read this module.
    from domo_core.proofs.service import agree

    await agree(session, person.id, "password", proposed_by="code", now=now)


async def _keys_of_the_invitee(
    session: AsyncSession, invitation: Invitation, person: Person, now: datetime
) -> None:
    """The keys the person who joined with ``invitation`` uses (see the module docstring): their
    inviter's (a line for them on each key the inviter gave, none when the inviter is the owner),
    or a key of their own (a line takes them off the general key). An invitee of the inviter's
    keys follows them on the general key with no line, unless the household's rule is that
    invitees who join never use it (``keys.invitee_general`` ``never``): a line takes them off
    each general key their inviter did not give. Flushed."""
    settings = await load_policy_settings(session)
    uses_inviter = invitation.uses_inviter_key
    if uses_inviter is None:
        uses_inviter = settings.invitee_default != "own"
    lines: dict[str, bool] = {}
    if uses_inviter:
        inviter = await session.get(Person, invitation.created_by)
        if inviter is not None and inviter.role != "owner":
            given = select(Provider.id).where(Provider.given_by == inviter.id)
            lines.update(dict.fromkeys((await session.execute(given)).scalars().all(), True))
    if not uses_inviter or settings.invitee_general == "never":
        general = select(Provider.id).where(Provider.is_general)
        if uses_inviter:
            # The inviter's own general key is one of the keys they let the invitee use.
            general = general.where(Provider.given_by != invitation.created_by)
        for provider_id in (await session.execute(general)).scalars().all():
            lines.setdefault(provider_id, False)
    for provider_id, allowed in lines.items():
        session.add(
            ProviderUser(
                provider_id=provider_id,
                scope="person",
                person_id=person.id,
                allowed=allowed,
                set_by=invitation.created_by,
                set_at=now,
            )
        )
    await session.flush()


async def cancel_invitation(session: AsyncSession, invitation: Invitation, now: datetime) -> None:
    """Cancel the invitation: its link stops working. The row is locked and checked first:
    ``InvitationUnavailable`` when it can no longer be used anyway (accepted meanwhile, say).
    Flushed, not committed."""
    locked = await session.execute(
        select(Invitation)
        .where(Invitation.id == invitation.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    current = locked.scalar_one()
    if not is_open(current, now):
        raise InvitationUnavailable
    current.cancelled_at = now
    await session.flush()


async def set_role(
    session: AsyncSession,
    person: Person,
    role: MemberRole,
    *,
    now: datetime,
    guest_days: int | None = None,
) -> datetime | None:
    """Give the person another role (never the owner's: ``ValueError``), at ``now``, and return
    the end of their stay, if they have one now.

    Someone who becomes a guest stays ``guest_days`` from ``now`` (``GUEST_DAYS_DEFAULT`` when not
    given), as an invited guest does (R78); someone who gets another role stays for good (their
    end of stay is cleared). ``ValueError`` for days out of range, or days given to a role that
    does not end (``_guest_days``). Flushed, not committed."""
    if role not in MEMBER_ROLES:
        raise ValueError("a role is given to an adult, a child or a guest")
    days = _guest_days(role, guest_days)
    end = None if days is None else now + timedelta(days=days)
    statement = update(Person).where(Person.id == person.id).values(role=role, expires_at=end)
    if end is not None:
        # A stay ends by itself: the last review it is owed once it is over (``memory.review``).
        statement = statement.values(last_review_due=True)
    await session.execute(statement)
    await session.flush()
    return end


async def refuse_requests_of(session: AsyncSession, person_id: UUID, now: datetime) -> int:
    """Refuse the requests of the person that wait for a yes (nobody can approve them any more,
    or they were made with an access that is over). Returns how many were refused. Flushed by the
    caller."""
    refused = await session.execute(
        update(Confirmation)
        .where(Confirmation.requester_id == person_id, Confirmation.status == "pending")
        .values(status="refused", decided_at=now)
        .returning(Confirmation.id)
        .execution_options(synchronize_session=False)
    )
    return len(refused.all())


@dataclass(frozen=True)
class Removed:
    """What ``disable`` did: how many requests of the person that waited for a yes were refused,
    and how many of their own rights were paused."""

    requests_refused: int
    rights_paused: int


async def disable(session: AsyncSession, person: Person, now: datetime, *, by: UUID) -> Removed:
    """Remove the person from the household at ``now``, ``by`` the one who does it (the person
    themselves when they leave): their account is disabled (they are not deleted, nor is anything
    they wrote), every session of theirs ends, their requests that wait for a yes are refused
    (nobody can approve them any more) and the rights the owner gave them themselves are paused
    (``policy.rights.pause_rights_and_audit``: the audit entry ``rights.paused`` says whose and
    how many, ``by`` being its actor, as for the removal's own entry). The tools Domo made stay:
    they are his; what was set to launch for them waits until they come back
    (``madetools.triggers``). What was done, in numbers. Flushed, not committed."""
    await session.execute(
        update(Person)
        .where(Person.id == person.id, Person.disabled_at.is_(None))
        # Domo owes them one last review now (``memory.review``).
        .values(disabled_at=now, last_review_due=True)
    )
    await revoke_sessions_of(session, person.id)
    await forget_devices_of(session, person.id, now)
    refused = await refuse_requests_of(session, person.id, now)
    paused = await pause_rights_and_audit(session, person.id, now, actor=f"person:{by}")
    await session.flush()
    return Removed(requests_refused=refused, rights_paused=paused)


# --- bringing someone back -----------------------------------------------------------------------

# How a former member is told from another of the same name: the role they had, in words.
_ROLE_WORDS: Final = {"adult": "an adult", "child": "a child", "guest": "a guest"}


def left_on(person: Person) -> date:
    """The day (UTC) the person left the household: the day they were removed (``disabled_at``),
    otherwise the day their stay ended (``expires_at``). ``ValueError`` for someone who has
    neither, who never left."""
    moment = person.disabled_at if person.disabled_at is not None else person.expires_at
    if moment is None:
        raise ValueError("this person did not leave the household")
    return moment.astimezone(UTC).date()


def _who_left_on(people: list[Person], day: date) -> list[Person]:
    return [person for person in people if left_on(person) == day]


def _told_apart(people: list[Person]) -> str:
    """The former members as Domo can tell them apart, the earliest departure first: the role
    they had and the day they left, never an identifier or a username."""
    ordered = sorted(people, key=lambda person: (left_on(person), person.role))
    return ", ".join(
        f"{_ROLE_WORDS.get(person.role, 'a person')} who left on {left_on(person).isoformat()}"
        for person in ordered
    )


async def former_persons(session: AsyncSession, now: datetime) -> list[Person]:
    """The people who left the household by ``now`` (removed, or a guest whose stay is over), in
    the order they arrived. The owner never leaves."""
    result = await session.execute(
        select(Person)
        .where(~active_at(now), Person.role != "owner")
        .order_by(Person.created_at, Person.id)
    )
    return list(result.scalars())


async def resolve_former_person(
    session: AsyncSession, name: str, *, now: datetime, left_on: date | None = None
) -> Person:
    """The one former member whose display name reads like ``name`` (``text.name_key``), as
    ``resolve_person`` finds the one person in the household.

    ``InvalidArguments("not_found")`` when nobody who left has it, with the detail that the person
    is still in the household when someone who is has it. ``InvalidArguments("ambiguous")`` when
    several do: the detail tells them apart by the role they had and the day they left, and
    ``left_on`` (that day) picks one of them; a day that fits nobody is ``not_found``."""
    key = name_key(name)
    named = [p for p in await former_persons(session, now) if name_key(p.display_name) == key]
    if not named:
        if any(name_key(p.display_name) == key for p in await active_persons(session, now)):
            raise InvalidArguments("not_found", "this person is still in the household")
        raise InvalidArguments("not_found", "nobody who left the household has this name")
    if left_on is not None:
        named = _who_left_on(named, left_on)
        if not named:
            raise InvalidArguments("not_found", "nobody with this name left on that day")
    if len(named) > 1:
        ask = (
            "" if left_on is not None else "; ask which one is meant, then call again with left_on"
        )
        raise InvalidArguments(
            "ambiguous",
            f"several people who left the household have this name: {_told_apart(named)}{ask}",
        )
    return named[0]


@overload
async def create_return_invitation(
    session: AsyncSession,
    *,
    person: Person,
    role: MemberRole,
    guest_days: int | None,
    display_name: str,
    created_by: UUID,
    now: datetime,
    with_token: Literal[True] = True,
) -> tuple[Invitation, str]: ...


@overload
async def create_return_invitation(
    session: AsyncSession,
    *,
    person: Person,
    role: MemberRole,
    guest_days: int | None,
    display_name: str,
    created_by: UUID,
    now: datetime,
    with_token: bool,
) -> tuple[Invitation, str | None]: ...


async def create_return_invitation(
    session: AsyncSession,
    *,
    person: Person,
    role: MemberRole,
    guest_days: int | None,
    display_name: str,
    created_by: UUID,
    now: datetime,
    with_token: bool = True,
) -> tuple[Invitation, str | None]:
    """Make the return link of a person who left the household: an invitation like
    ``create_invitation`` (the same delay, the same raw token returned once and stored nowhere, or
    none when someone else said yes: ``with_token``), tied to ``person``, with the role, the days
    and the display name they come back with.

    ``ValueError`` for someone who has not left (or the owner), as for the cases of
    ``create_invitation``. The caller checks that the name is free (``name_in_use``). Flushed, not
    committed."""
    if person.role == "owner" or is_active(person, now):
        raise ValueError("only someone who left the household can be brought back")
    return await _make_invitation(
        session,
        display_name=display_name,
        role=role,
        guest_days=guest_days,
        created_by=created_by,
        now=now,
        person_id=person.id,
        with_token=with_token,
    )


async def open_return_invitation_of(
    session: AsyncSession, person_id: UUID, now: datetime
) -> Invitation | None:
    """The return link of the person that still waits (open), if there is one."""
    result = await session.execute(
        select(Invitation)
        .where(Invitation.person_id == person_id, open_at(now))
        .order_by(Invitation.created_at, Invitation.id)
        .limit(1)
    )
    return result.scalars().first()


async def returning_account(
    session: AsyncSession, invitation: Invitation, now: datetime
) -> Account | None:
    """The account of the person a return link is for, if the invitation is one and the person is
    still away at ``now`` (not in the household): the one whose username the page shows and whose
    password the link replaces. Nothing is locked."""
    if invitation.person_id is None:
        return None
    found = await session.execute(
        select(Person, Account)
        .join(Account, Account.person_id == Person.id)
        .where(Person.id == invitation.person_id)
    )
    row = found.first()
    if row is None:
        return None
    person, account = row
    return None if person.role == "owner" or is_active(person, now) else account


@dataclass(frozen=True)
class Returning:
    """What ``check_return`` found good: the return link to accept, the person who comes back, the
    username they keep (as it is stored) and the display name they will have."""

    invitation_id: UUID
    person_id: UUID
    username: str
    display_name: str


async def check_return(
    session: AsyncSession, *, token: str, password: str, now: datetime
) -> Returning:
    """Check what someone typed to come back with the return link of ``token``, before the slow
    hash of their password. Reads only, and locks nothing.

    ``InvitationUnavailable`` when the link cannot be used: it names no invitation, one that was
    used, cancelled or has expired, one that is not a return link (a joining link is not for
    coming back), or the person is no longer away. ``WeakPassword`` otherwise, and the link still
    works."""
    invitation = await invitation_of(session, token, now)
    if invitation is None or invitation.person_id is None:
        raise InvitationUnavailable
    account = await returning_account(session, invitation, now)
    if account is None:
        raise InvitationUnavailable
    # Characters, not bytes; and not trimmed: spaces may be part of a password.
    if len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPassword
    try:
        display_name = display_name_value(invitation.display_name)
    except InvalidDisplayName:
        raise InvitationUnavailable from None
    return Returning(
        invitation_id=invitation.id,
        person_id=invitation.person_id,
        username=account.username,
        display_name=display_name,
    )


async def accept_return(
    session: AsyncSession, returning: Returning, password_hash: str, *, now: datetime
) -> Person:
    """Bring the person back as ``check_return`` found it good, with the hash of the new password.

    The link's row and the person's are locked and checked again: ``InvitationUnavailable`` when
    the link was used (or ended) since, or the person is in the household again (a link brings
    back someone who is away). Then the person is back at ``now``: ``disabled_at`` is emptied; the
    role, the end of the stay (``guest_days`` from ``now`` for a guest, none otherwise) and the
    display name are the link's; the password is replaced; their passkeys are deleted (they may
    add new ones); every session of theirs ends, and the requests of theirs that waited for a yes
    are refused: of the old access, nothing is left. The rights the owner had given to them
    (not to their role) that are still in force are paused
    (``policy.rights.pause_rights_and_audit``; most were paused when they left): they no longer
    count until the owner gives them back, and when there were some the audit entry
    ``rights.paused`` says whose and how many. The link is marked accepted. Their username and
    everything they wrote stay. Returns the person. Flushed, not committed: after an exception the
    caller rolls back."""
    locked = await session.execute(
        select(Invitation)
        .where(Invitation.id == returning.invitation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    invitation = locked.scalar_one_or_none()
    if (
        invitation is None
        or not is_open(invitation, now)
        or invitation.person_id != returning.person_id
    ):
        raise InvitationUnavailable
    found = await session.execute(
        select(Person, Account)
        .join(Account, Account.person_id == Person.id)
        .where(Person.id == returning.person_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    row = found.first()
    if row is None:
        raise InvitationUnavailable
    person, account = row
    if person.role == "owner" or is_active(person, now):
        raise InvitationUnavailable
    person.disabled_at = None
    person.role = invitation.role
    person.expires_at = (
        now + timedelta(days=invitation.guest_days or GUEST_DAYS_DEFAULT)
        if invitation.role == "guest"
        else None
    )
    if person.expires_at is not None:
        # A stay ends by itself: the last review it is owed once it is over (``memory.review``).
        person.last_review_due = True
    person.display_name = returning.display_name
    account.password_hash = password_hash
    await session.execute(
        delete(PasskeyCredential)
        .where(PasskeyCredential.account_id == account.id)
        .execution_options(synchronize_session=False)
    )
    # No passkey is left, so the means that went with them goes too; the password stays.
    from domo_core.proofs.service import remove

    await remove(session, person.id, "passkey", now)
    # Nor does the phrase they said aloud outlive the old access (ruling T11): the means goes. Its
    # stored copy at the vault is removed by the caller once this is committed (the vault is not
    # in this transaction); it can prove nothing without the means, and agreeing a phrase again
    # removes it first.
    await remove(session, person.id, "spoken_phrase", now)
    await revoke_sessions_of(session, person.id)
    # A guest whose stay ended, and whom the upkeep has not swept yet, still has their devices.
    await forget_devices_of(session, person.id, now)
    await refuse_requests_of(session, person.id, now)
    invitation.accepted_at = now
    invitation.accepted_person_id = person.id
    await session.flush()
    await pause_rights_and_audit(session, person.id, now)
    return person


# --- handing the owner's role over ---------------------------------------------------------------


@dataclass(frozen=True)
class Transferred:
    """What ``transfer_ownership`` did: who gave the role and who has it now, how many requests of
    the new owner that waited for the owner's yes now wait for their own (``rerouted``), how many
    of their personal rights were put on pause (``paused``), and whether they were taken off the
    owner's list of successors (``left_the_successors``: 1 or 0)."""

    old_owner_id: UUID
    new_owner_id: UUID
    rerouted: int
    paused: int
    left_the_successors: int


async def _off_the_successors(session: AsyncSession, person_id: UUID) -> int:
    """Take the person off the owner's list of successors, if they are on it, and close the gap:
    the places after theirs move up one rank, so that the ranks go on from 1 without a gap (as
    ``household.succession`` keeps them). Returns how many places were taken off (1 or 0). Whoever
    changes the list takes the same advisory lock first. Flushed, not committed."""
    await session.execute(select(func.pg_advisory_xact_lock(SUCCESSORS_LOCK_KEY)))
    found = await session.execute(
        select(Successor)
        .order_by(Successor.rank)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    places = list(found.scalars())
    leaving = [place for place in places if place.person_id == person_id]
    if not leaving:
        return 0
    await session.delete(leaving[0])
    await session.flush()
    # One at a time, the nearest first: each rank is free when its turn comes (the column is unique,
    # and an update of all the ranks at once could meet itself).
    for rank, place in enumerate((p for p in places if p is not leaving[0]), start=1):
        if place.rank != rank:
            place.rank = rank
            await session.flush()
    return 1


async def transfer_ownership(
    session: AsyncSession, *, old_owner: Person, new_owner: Person, now: datetime
) -> Transferred:
    """Give the owner's role to ``new_owner`` and make ``old_owner`` an adult, at ``now`` (see the
    module docstring). Only the identifiers of the two people are used: their rows are locked and
    read again.

    ``ValueError`` unless ``old_owner`` is the owner and in the household, ``new_owner`` an adult in
    the household and the two are not the same person; nothing is changed then. Flushed, not
    committed. The objects of ``session`` for the two people are updated; those of another session
    are not."""
    if old_owner.id == new_owner.id:
        raise ValueError("the role is not handed to oneself")
    locked = await session.execute(
        select(Person)
        .where(Person.id.in_((old_owner.id, new_owner.id)))
        .order_by(Person.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    people = {person.id: person for person in locked.scalars()}
    old, new = people.get(old_owner.id), people.get(new_owner.id)
    if old is None or old.role != "owner" or not is_active(old, now):
        raise ValueError("only the current owner hands the role over")
    if new is None or new.role != "adult" or not is_active(new, now):
        raise ValueError("the role goes to an adult who is in the household")
    old_id, new_id, old_name, new_name = old.id, new.id, old.display_name, new.display_name
    # In this order: the index allows one owner, so the old one stops being it first.
    await session.execute(update(Person).where(Person.id == old_id).values(role="adult"))
    await session.execute(
        update(Person).where(Person.id == new_id).values(role="owner", expires_at=None)
    )
    # The new owner's requests that waited for the owner's yes: the owner does not say yes to their
    # own request on someone's behalf, and there is no other owner. They wait for their own yes.
    rerouted = await session.execute(
        update(Confirmation)
        .where(
            Confirmation.requester_id == new_id,
            Confirmation.approver_kind == "owner",
            Confirmation.status == "pending",
        )
        .values(approver_kind="self")
        .returning(Confirmation.id)
        .execution_options(synchronize_session=False)
    )
    moved = len(rerouted.all())
    paused = await pause_personal_rights(session, new_id, now)
    left = await _off_the_successors(session, new_id)
    # Last, just before the commit: identifiers, names and numbers.
    await append_audit(
        session,
        actor=f"person:{old_id}",
        action=OWNERSHIP_TRANSFERRED,
        details={
            "from": f"person:{old_id}",
            "to": f"person:{new_id}",
            "from_name": old_name,
            "to_name": new_name,
            "rerouted": moved,
            "paused": paused,
            "left_the_successors": left,
        },
    )
    await session.flush()
    return Transferred(
        old_owner_id=old_id,
        new_owner_id=new_id,
        rerouted=moved,
        paused=paused,
        left_the_successors=left,
    )


async def ownership_note(
    session: AsyncSession, person_id: UUID, now: datetime
) -> OwnershipNote | None:
    """What the new owner is told about how they became the owner: the most recent hand-over to
    ``person_id`` (the audit entry ``household.ownership_transferred`` whose ``to`` is them) that
    is less than ``OWNERSHIP_NOTE_DAYS`` days old at ``now``, as the day it was done (UTC) and the
    name of the one who handed the role over; ``None`` if there is none. Reads only."""
    found = await session.execute(
        select(readable_ts(), AuditEntry.details)
        .where(
            AuditEntry.action == OWNERSHIP_TRANSFERRED,
            AuditEntry.details.contains({"to": f"person:{person_id}"}),
            AuditEntry.ts > now - timedelta(days=OWNERSHIP_NOTE_DAYS),
        )
        .order_by(AuditEntry.id.desc())
        .limit(1)
    )
    row = found.first()
    if row is None:
        return None
    # (NULL when the time is one that Python cannot read: only a hand-edited row has it.)
    moment = cast(datetime | None, row[0])
    from_name = row[1].get("from_name")
    if moment is None or not isinstance(from_name, str):
        return None
    return OwnershipNote(since=moment.astimezone(UTC).date().isoformat(), from_name=from_name)
