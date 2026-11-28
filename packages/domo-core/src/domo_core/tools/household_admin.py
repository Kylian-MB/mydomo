"""The tools of whoever has the right (the owner by default) to change who is in the household
(decisions D1 and D3).

Each of them is judged (the yes-judge says whether a yes is asked first, and which).

* ``household_invite``: invite someone, with a name, a role (adult, child or
  guest) and, for a guest, the days they may stay (``GUEST_DAYS_DEFAULT`` when nothing is said),
  or no end (``no_end``): no length is refused by the code (audit POL-5), from 1 day with no upper
  bound; a long stay is the owner's order or the assistant's judgement, under the limits the owner
  writes himself on ``days``. Once the yes is given, the
  invitation is made and the one who asked is shown its link, once: in the answer to the request
  that made it (``ToolOutcome.card_once``), never in the stored card, the model's result, the
  history, the audit log or a log. When someone else said yes (the owner, to Alex's invitation),
  the link is Alex's, and when no page reads the turn (a wake of Domo after a button), nobody
  would see it (``ToolContext.leave_on_their_card``): the invitation is made without a token, its
  card has the invitation's identifier, and the button on Alex's card makes the token and shows
  the link, once, to Alex alone (``household.service.reveal_link``, ``household.routes``); Domo
  is told the link waits for them (``"link": "waits_for_them"``), never that it was shown. The
  call may say whether the invitee uses the AI keys of the one who invites (``uses_my_key``, kept
  with the invitation as ``uses_inviter_key``: ``household.service.accept_invitation`` gives them
  those keys, or takes them off the general key); left out, the household's default decides.
  It invites someone new by name, or a record that exists (``person``: the account the link opens
  joins that record, with what the assistant knows of them), and may propose a ``username``. It
  may be the assistant's own act (``people.whose``: the call carries ``reason`` exactly then): the
  inviter is then the assistant, never the person of the turn (S-K: no ``created_by``), and its
  link is for the person of the turn only when they could invite by their own right, else for
  the owner (``whose_link``); with no tool to send it, a link nobody asked for waits for the owner
  (WEB-3). On his own idea, he may invite anyone, a record that does not live here too (the
  owner's answer of 2026-11-27, "Oui, tu es prévenu"): the owner is told once (``tell_owner``);
* ``household_change_role``: give someone another role. Not the owner's own. Someone made a guest
  stays a number of days (``guest_days``) or with no end (``no_end``); when nothing is said, a
  record with an account stays ``GUEST_DAYS_DEFAULT`` days (R78), a record without one keeps the
  end it has, or none (the assistant judges when they left: ``record_complete`` ends the stay).
  A record without an account whose stay is over is given a new one. Someone who stops being a
  guest stays for good. The rights the
  owner gave to the person themselves stay theirs (spec 5.12): the card says which they are
  (``"rights"``, their summaries), whoever asked (Ruling R26), so that the owner reads them whoever
  asked and whoever said yes; when the owner made the change, Domo is told them too
  (``their_own_rights``). A guest given the role of guest again, with days, has a new stay (spec
  5.5): a new end counted from now, longer or shorter, under the same right and rule; it is audited
  as such (``household.stay_changed``) and its card says the new end;
* ``household_remove``: remove someone. Their account is disabled at once,
  their sessions end and their waiting requests are refused; what they wrote is kept. The rights
  the owner gave to the person themselves are paused (spec 5.7): the card says which, whoever
  asked, and Domo is told how many (``paused_rights``). Not the owner. Whoever may remove people
  may remove themselves (it gives no power, and they can be brought back);
* ``household_leave`` (every person's, spec 5.9): the person who speaks leaves
  the household themselves, as a removal does it (nothing is erased, and they can be brought
  back), the person being the one who does it. Their own yes, or an adult's for a child (the
  barrier's default approver). Never the owner, who hands their role over first: a refusal of the
  tool, whatever the rights. Removing someone else stays ``household_remove``'s;
* ``household_cancel_invitation``: cancel an invitation that waits; its link stops
  working;
* ``invitation_link_again`` (2026-11-16, the owner's trial of 0.9.0): give an invitation that
  waits a new link, with the rights of inviting (``policy.catalog.RIGHTS_OF``): the old link
  stops working, the invitation keeps its role, its stay and its end. The new link is shown once,
  as an invitation's is (``card_once``); after someone else's yes, or when nobody could be shown
  it (``ToolContext.leave_on_their_card``), the old link dies and the new one waits for the one
  who asked, behind the "show the link" button of their card (``household.service.reveal_link``),
  as an invitation's does;
* ``invitation_link_show``: the link of an invitation that waits for the person who asks (for the
  owner, one that waits for anyone: it then no longer waits for them), shown
  once (``card_once``), as the button of their card does: in words, on any channel that shows
  something once (WEB-3); never the assistant's own idea, never in a turn nobody reads
  (``asked_by_them``);
* ``household_transfer_ownership`` (judged like any action: nothing is asked by default):
  hand the owner's role to an adult of the household. The
  owner becomes an adult and keeps their account; the new owner does not have to agree. It is the
  owner's alone, whatever the rights say, and so are its refusals (``fixed_refusal``): the one who
  asks is the owner of now (the role is read again from the database, never taken from the
  caller), not oneself, and the receiver an adult who is in the household. What it moves, and its
  audit entry (``household.ownership_transferred``), are in ``household.service``.

Giving a role, removing and leaving act on a record (``people.records``), with or without an
account: a role given to someone only known gives them access (``records.give_access``), a removal
takes it away (``records.remove``: the owner's own order is a withdrawal, ruling 13), and each act
leaves its trace with who did it and why. Giving a role and removing may be the assistant's own
act (``people.whose``: the call carries ``reason`` exactly then); a role may be one made for the
household, by its name. The child role's mark is lifted with it by the owner's order alone.

Bringing someone who left back (``household_bring_back``) is in ``tools.household_return``, which
shares the helpers below that are not private.

Who may use them is the catalog's business (``policy.catalog``: the owner only, unless the owner
has given the right to someone else; inviting, giving a link again and cancelling an invitation,
the adults too; leaving, everyone). Nobody ever changes their own role, though, and nobody acts on
the owner: those are in the code of the tools, whatever the rights say. So is whose invitation it
is: anyone but the owner gives the link again of, or cancels, only an invitation they made
(``theirs_to_change``, the owner's trial of 0.9.1); the assistant's own call judges it himself, and
an invitation he made is changed by whoever holds the right, the judge being shown it is his
(``given_by_domo``). An invitation may say that Domo judged
the owner should say yes first (``owner_first``, a fact of the action): the catalog's limit of
inviting then sends it to the owner.
People and
invitations are named by their display name (``household.service.resolve_person`` and
``resolve_invitation``): a name that means several people is ``ambiguous``, one that means nobody
``not_found``, and nothing is guessed. A request is described with the person (or invitation) the
name meant then, as its target, and with the facts a limit can read
(``policy.catalog.ACTION_FACTS``: the role, the days of a guest's stay, whether it is oneself; a
name, an identifier, a role or a number, nothing more); when the request runs after a yes, the name
must still mean that one (``ToolContext.described_action``), or nothing is done.

The tools that name someone or an invitation also say whom or what a call acts on, found by the
name alone (``acts_on``, ``tools.context.ActsOn``): a chain of actions uses it to tell a step that
cannot be described yet only because an earlier step changes whom it acts on ("hand my role to
Alex, then remove me": the owner is never removed, but the first step makes them an adult). An
invitation acts on the open invitation that holds its name ("cancel the invitation of Dana, then
invite Dana": the name is taken until the first step is done). A call whose arguments the tool does
not accept acts on nobody: no earlier step can make it right. The hand-over of the role says more
(``WillTouch``): it will touch the one who gives as well as the one who receives, so that a step
checked in its turn may be followed by one that names either ("make Sam an adult, hand Sam my role,
then remove me"). Leaving acts on the one who asks ("hand my role to Alex, then I leave").

Each change and its audit entry (``household.invited``, ``household.role_changed``,
``household.stay_changed``, ``household.removed``, ``household.left``,
``household.invitation_cancelled``, ``household.link_given_again``,
``household.ownership_transferred``: names, roles, identifiers
and numbers, never a token or a link) are one transaction, the audit entry written last (a removal,
or a departure, also writes ``rights.paused`` when it paused rights, ahead of its own).
"""

import logging
import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Account, Person
from domo_core.accounts.sessions import is_active
from domo_core.audit.log import append_audit
from domo_core.household.models import Invitation
from domo_core.household.service import (
    GUEST_DAYS_DEFAULT,
    LINK_SHOWN,
    MAX_USERNAME_LENGTH,
    MEMBER_ROLES,
    Among,
    InvalidUsername,
    InvitationUnavailable,
    MemberRole,
    cancel_invitation,
    create_invitation,
    give_link_again,
    link_holder,
    lock_display_names,
    name_in_use,
    resolve_invitation,
    resolve_person,
    reveal_link,
    terms_for_record,
    transfer_ownership,
    username_value,
)
from domo_core.llm.types import JsonObject, ToolDefinition
from domo_core.people import directives, made_roles, records, whose
from domo_core.people.acts import note_act, tell_owner, tell_owner_of
from domo_core.people.models import MadeRole, PersonAct
from domo_core.people.news import news_for
from domo_core.policy.actions import DescribedAction, FactValue, Present
from domo_core.policy.barrier import Allow
from domo_core.policy.catalog import GIVEN_BY_DOMO_FACT
from domo_core.policy.decision import decide_now
from domo_core.policy.rights import personal_right_summaries
from domo_core.policy.roles import DOMO_LABEL, Actor, Role, as_role
from domo_core.policy.settings import load_policy_settings
from domo_core.policy.subjects import owner_of_now
from domo_core.text import MAX_DISPLAY_NAME_LENGTH
from domo_core.tools.context import InvalidArguments, ToolContext, ToolOutcome, acts_alone
from domo_core.tools.people_tools import record_named
from domo_core.tools.profile import display_name_value

logger = logging.getLogger(__name__)

_INVITE_ARGUMENTS: Final = frozenset(
    {
        "display_name",
        "person",
        "role",
        "guest_days",
        "no_end",
        "uses_my_key",
        "owner_first",
        "username",
        "reason",
        "exception_of",
    }
)
_CHANGE_ROLE_ARGUMENTS: Final = frozenset(
    {
        "person",
        "role",
        "guest_days",
        "no_end",
        "reason",
        "exception_of",
        "lives_here",
        "tell_owner",
    }
)
_REMOVE_ARGUMENTS: Final = frozenset({"person", "reason", "tell_owner"})
_CANCEL_ARGUMENTS: Final = frozenset({"display_name", "reason", "tell_owner"})
_LINK_AGAIN_ARGUMENTS: Final = frozenset({"display_name", "reason", "exception_of"})
_SHOW_ARGUMENTS: Final = frozenset({"display_name"})
_TRANSFER_ARGUMENTS: Final = frozenset({"person"})

ROLE_SCHEMA: Final[JsonObject] = {"type": "string", "enum": list(MEMBER_ROLES)}
NAME_IN_USE: Final = (
    "someone in the household, or an invitation that waits, already has this name: choose a "
    "name that tells them apart"
)
_NOT_THE_SAME: Final = (
    "this name no longer means the one the request was about, so nothing was done: ask again"
)
OWNER_ROLE_NEVER_GIVEN: Final = "the owner's role is never given"
_ONLY_THE_OWNER: Final = "only the current owner hands the role over"
_NOT_TO_ONESELF: Final = "the role is not handed to oneself"
_TO_AN_ADULT: Final = "the role goes to an adult who is in the household"
_STAY_NEEDS_DAYS: Final = (
    "the person is already a guest: give guest_days or no_end to change their stay"
)
_OWNER_NEVER_LEAVES: Final = "the owner never leaves: they hand their role over first"
# The states of a record a role is given to: someone only known (or refused: the owner alone
# gives them access), and someone who has access. Someone who left is brought back instead.
_GIVES_A_ROLE_TO: Final = ("known", "refused", "has_access")
_THEY_LEFT: Final = "this person left the household: bring them back instead"


def gives_a_member_role(facts: Mapping[str, FactValue]) -> str | None:
    """Why an action that gives someone the role of its facts (``role``) is always refused:
    the owner's role is never given, only one of ``MEMBER_ROLES`` (``role_from`` reads a call
    the same way)."""
    return None if facts.get("role") in MEMBER_ROLES else OWNER_ROLE_NEVER_GIVEN


def present_as(person: Person) -> dict[str, Role]:
    """``person``, who is in the household, as ``Present`` names them."""
    return {f"person:{person.id}": as_role(person.role)}


def role_from(value: object) -> MemberRole:
    for role in MEMBER_ROLES:
        if value == role:
            return role
    raise InvalidArguments("invalid", 'the role is "adult", "child" or "guest"')


_A_ROLE: Final = (
    'the role is "adult", "child", "guest" or the name of a role made for the household'
)


async def role_named(session: AsyncSession, value: object) -> tuple[MemberRole, MadeRole | None]:
    """The role a call gives: a built-in role (``MEMBER_ROLES``), or a role made for the household
    named by its name (``people.made_roles.named``), with its base. The owner's role is never
    given (``OWNER_ROLE_NEVER_GIVEN``, whatever the case it is written in); anything else is
    ``InvalidArguments``."""
    if not isinstance(value, str) or not value.strip():
        raise InvalidArguments("invalid", _A_ROLE)
    key = made_roles.name_key(value)
    if key == "owner":
        raise InvalidArguments("invalid", OWNER_ROLE_NEVER_GIVEN)
    for role in MEMBER_ROLES:
        if key == role:
            return role, None
    made = await made_roles.named(session, value)
    if made is None:
        raise InvalidArguments("invalid", _A_ROLE)
    return role_from(made.base), made


async def has_an_account(session: AsyncSession, person: Person) -> bool:
    """Whether the record has a login account."""
    found = await session.scalar(select(Account.id).where(Account.person_id == person.id))
    return found is not None


async def made_role_name(session: AsyncSession, person: Person) -> str | None:
    """The name of the role made for the household the record carries, if any."""
    if person.made_role_id is None:
        return None
    made = await made_roles.by_id(session, person.made_role_id)
    return None if made is None else made.name


_UNDER_A_DIRECTIVE: Final = (
    "the owner withdrew or refused this person's access: only the owner gives it back"
)
_WHY_NOT: Final = {
    "not_a_record": "no record has this name",
    "the_owner": "the owner is never removed",
    "no_access": (
        "this person has no access to take away; only the household's owner can keep them out "
        "in advance, by withdrawing or refusing them"
    ),
}


def not_possible(error: records.NotPossible) -> InvalidArguments:
    """What a refusal of ``people.records`` says to the assistant."""
    return InvalidArguments("not_found", _WHY_NOT.get(error.code, error.code))


def name_from(value: object, what: str) -> str:
    """A name the call gives to find someone (or an invitation): a text that is not blank."""
    if not isinstance(value, str) or not value.strip():
        raise InvalidArguments("invalid", f"{what} is a name, as a text")
    return value


def _exactly(arguments: JsonObject, expected: frozenset[str]) -> None:
    if frozenset(arguments) != expected:
        raise InvalidArguments("invalid", f"the arguments are {', '.join(sorted(expected))}")


def _at_most(arguments: JsonObject, expected: frozenset[str]) -> None:
    """``InvalidArguments`` unless the call gives ``display_name`` and nothing out of
    ``expected`` (a ``reason`` when the act is the assistant's own)."""
    if "display_name" not in arguments or not frozenset(arguments) <= expected:
        raise InvalidArguments(
            "invalid", f"the arguments are {', '.join(sorted(expected))} (reason if needed)"
        )


@dataclass(frozen=True)
class _Invite:
    """What a ``household_invite`` call asks: a new person by the name the household will call
    them (``name``), or a record that exists (``person``, as a name or ``person:<id>``); the role,
    the days of a guest's stay (``None`` when not given), whether it has no end, and the username
    proposed (``None``)."""

    name: str | None
    person: str | None
    role: MemberRole
    days: int | None
    no_end: bool
    username: str | None


def _invitation_from(arguments: JsonObject) -> _Invite:
    """What a ``household_invite`` call asks (``_Invite``). ``InvalidArguments`` for anything
    else: exactly one of ``display_name`` and ``person``, days only for a guest, a username that
    could be one."""
    keys = frozenset(arguments)
    if (
        "role" not in keys
        or not keys <= _INVITE_ARGUMENTS
        or ("display_name" in keys) == ("person" in keys)
    ):
        raise InvalidArguments(
            "invalid",
            "the arguments are display_name (someone new) or person (someone you know), role, "
            "uses_my_key, owner_first, username, reason and, for a guest only, guest_days or "
            "no_end",
        )
    role = role_from(arguments["role"])
    told: object = arguments.get("username")
    username = None
    if told is not None:
        if not isinstance(told, str):
            raise InvalidArguments("invalid", "username is a text")
        try:
            username = username_value(told)
        except InvalidUsername:
            raise InvalidArguments(
                "invalid",
                f"a username is one plain line of {MAX_USERNAME_LENGTH} characters at most",
            ) from None
    days = guest_days_from(arguments, role)
    no_end = no_end_from(arguments)
    if "person" in keys:
        return _Invite(None, name_from(arguments["person"], "person"), role, days, no_end, username)
    name = display_name_value(arguments["display_name"])
    return _Invite(name, None, role, days, no_end, username)


def _owner_first(arguments: JsonObject) -> bool:
    """Whether Domo judged that the owner says yes before the invitee of a ``household_invite``
    call joins (``False`` when the call does not say): a fact of the action, which the catalog's
    limit of inviting reads (``policy.catalog.DEFAULT_LIMITS``)."""
    told: object = arguments.get("owner_first")
    if told is None:
        return False
    if not isinstance(told, bool):
        raise InvalidArguments("invalid", "owner_first is true or false")
    return told


def _uses_my_key(arguments: JsonObject) -> bool | None:
    """Whether the invitee of a ``household_invite`` call uses the keys of the one who invites
    (``False``: they bring their own), ``None`` when the call does not say (the household's
    default decides when they join)."""
    told: object = arguments.get("uses_my_key")
    if told is not None and not isinstance(told, bool):
        raise InvalidArguments("invalid", "uses_my_key is true or false")
    return told


_DAYS_OR_NO_END: Final = "give guest_days or no_end, not both"


def no_end_from(arguments: JsonObject) -> bool:
    """Whether the call says the stay has no end (``no_end``; ``False`` when it does not say).
    For anyone but a guest it changes nothing: they stay for good."""
    told: object = arguments.get("no_end")
    if told is None:
        return False
    if not isinstance(told, bool):
        raise InvalidArguments("invalid", "no_end is true or false")
    return told


def _comes_to_live(arguments: JsonObject) -> bool:
    """Whether a ``household_change_role`` call says that someone passing by now lives here
    (``lives_here``; ``False`` when it does not say)."""
    told: object = arguments.get("lives_here")
    if told is None:
        return False
    if not isinstance(told, bool):
        raise InvalidArguments("invalid", "lives_here is true or false")
    return told


def guest_days_from(arguments: JsonObject, role: MemberRole) -> int | None:
    """The ``guest_days`` of a call, ``None`` when not given: a whole number of days from 1, with
    no upper bound (no length is refused by the code, audit POL-5), only for a guest, and never
    with ``no_end`` (``InvalidArguments`` otherwise). A limit the owner writes himself on ``days``
    is the barrier's."""
    days: object = arguments.get("guest_days")
    if days is None:
        return None
    if role != "guest":
        raise InvalidArguments("invalid", "guest_days is only for a guest")
    if isinstance(days, bool) or not isinstance(days, int) or days < 1:
        raise InvalidArguments("invalid", "guest_days is a whole number of days, from 1")
    if no_end_from(arguments):
        raise InvalidArguments("invalid", _DAYS_OR_NO_END)
    return days


def stay_days(role: MemberRole, days: int | None, *, no_end: bool = False) -> int | None:
    """The days a person invited with ``role`` stays, for the facts of an action: those the call
    gave, ``GUEST_DAYS_DEFAULT`` for a guest it gave none to (they get an account, or have one),
    ``None`` for a stay with no end and for anyone else (they stay for good)."""
    if role != "guest" or no_end:
        return None
    return GUEST_DAYS_DEFAULT if days is None else days


def days_left(person: Person, now: datetime) -> int | None:
    """The whole days left of the stay ``person`` has now, when it ends later (counted up, one at
    least); ``None`` when it has no end or is over."""
    if person.expires_at is None or person.expires_at <= now:
        return None
    return max(math.ceil((person.expires_at - now) / timedelta(days=1)), 1)


def stay_given(
    role: MemberRole,
    days: int | None,
    no_end: bool,
    *,
    person: Person,
    has_account: bool,
    now: datetime,
) -> tuple[int | None, int | None]:
    """What giving ``person`` the role ``role`` does to their stay: the days passed to
    ``records.give_access`` and the days it lasts from now (the facts' ``days``; ``None``: no end).
    The days the call gave; else the end they have, when it is still to come; else, for a guest
    with an account, ``GUEST_DAYS_DEFAULT`` days; else no end (a record without an account: the
    assistant judges when they left). Anyone but a guest stays for good."""
    if no_end:
        return None, None
    if days is not None:
        # For anyone else than a guest too: an end of their access, their role unchanged (the
        # owner's answer of 2026-11-27, through this judged gesture alone).
        return days, days
    if role != "guest":
        return None, None
    kept = days_left(person, now)
    if kept is not None:
        return None, kept
    if has_account:
        return GUEST_DAYS_DEFAULT, GUEST_DAYS_DEFAULT
    return None, None


def card_date(moment: datetime) -> str:
    """A time as a card keeps it, under its ``"dates"``: ISO 8601, in UTC. The card puts it in
    words when it is shown, for whoever reads it (``conversation.execution``)."""
    return moment.astimezone(UTC).isoformat()


async def _found(find: Callable[[AsyncSession], Awaitable[str]], ctx: ToolContext) -> str | None:
    """What ``find`` finds, read in a short session of its own; ``None`` when it finds nothing
    (``InvalidArguments``) or the database cannot be read (logged): never an error."""
    try:
        async with ctx.session_factory() as session:
            return await find(session)
    except InvalidArguments:
        return None
    except Exception as error:
        logger.error("Whom a call names could not be read (%s)", type(error).__name__)
        return None


async def person_named(name: str, ctx: ToolContext, among: Among = "household") -> str | None:
    """Whom a call names (``ActsOn``): ``person:<id>`` of the one person of the household (or the
    one record, ``among``) ``name`` means now, with no check of what would happen to them; ``None``
    when it means nobody or several."""

    async def find(session: AsyncSession) -> str:
        if among == "records":
            return f"person:{(await record_named(session, name, ctx)).id}"
        found = await resolve_person(session, name, now=ctx.clock(), speaker=ctx.person.id)
        return f"person:{found.id}"

    return await _found(find, ctx)


async def invitation_named(name: str, ctx: ToolContext) -> str | None:
    """The invitation a call names (``ActsOn``): ``invitation:<id>`` of the one open invitation
    made for ``name``, with no check of what would happen to it; ``None`` when it means nobody or
    several."""

    async def find(session: AsyncSession) -> str:
        return f"invitation:{(await resolve_invitation(session, name, now=ctx.clock())).id}"

    return await _found(find, ctx)


async def audit_invitation_cancelled(
    session: AsyncSession, invitation: Invitation, by: str
) -> None:
    """The audit entry of an invitation cancelled by ``by`` (``domo`` or ``person:<id>``): its
    identifier, the name it was made for and its role, never its link. Cancelling and this entry
    are one transaction (and the entry is written last, just before the commit)."""
    await append_audit(
        session,
        actor=by,
        action="household.invitation_cancelled",
        details={
            "invitation_id": str(invitation.id),
            "name": invitation.display_name,
            "role": invitation.role,
        },
    )


_SOMEONE_ELSES_INVITATION: Final = (
    "this invitation was made by someone else: it is theirs, or the owner's, to change; you may "
    "offer to leave them a note"
)


def theirs_to_change(invitation: Invitation, actor: Actor) -> None:
    """``InvalidArguments`` (``not_possible``) unless ``actor`` may give the link of
    ``invitation`` again or cancel it: the assistant's own call, any (his judgement, TLP-5); the
    owner, any; an invitation the assistant made, whoever holds the right, and the judge weighs it
    (the owner's answer of 2026-11-27, "Oui, Domo juge": the fact ``given_by_domo``); anyone else,
    only an invitation they made themselves (``created_by``), whatever the rights (the owner's
    trial of 0.9.1)."""
    if actor.subject == "domo" or invitation.by_domo:
        return
    if actor.role != "owner" and invitation.created_by != actor.person_id:
        raise InvalidArguments("not_possible", _SOMEONE_ELSES_INVITATION)


def described_target(ctx: ToolContext, target: str) -> None:
    """When a stored request runs, the name must still mean what the person said yes to."""
    described = ctx.described_action
    if described is not None and described.target != target:
        raise InvalidArguments("not_found", _NOT_THE_SAME)


async def _someone(session: AsyncSession, name: str, ctx: ToolContext, now: datetime) -> Person:
    """The person ``name`` means, locked for the change, still in the household, and the one a
    stored request was about."""
    person = await resolve_person(session, name, now=now, speaker=ctx.person.id)
    described_target(ctx, f"person:{person.id}")
    locked = await session.execute(
        select(Person)
        .where(Person.id == person.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    person = locked.scalar_one()
    if not is_active(person, now):
        raise InvalidArguments("not_found", "nobody in the household has this name")
    return person


async def record_locked(session: AsyncSession, name: str, ctx: ToolContext) -> Person:
    """The record ``name`` means (``tools.people_tools.record_named``: with or without access,
    never one merged or forgotten), locked for the change, and the one a stored request was
    about."""
    person = await record_named(session, name, ctx)
    described_target(ctx, f"person:{person.id}")
    locked = await session.execute(
        select(Person)
        .where(Person.id == person.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return locked.scalar_one()


async def link_for_his_own(
    session: AsyncSession, ctx: ToolContext, action: DescribedAction
) -> UUID:
    """For whom the link of an invitation the assistant makes on his own idea is (``link_holder``):
    the person of the turn when their own right covers this very invitation and the turn answers
    them; otherwise (someone whose words gave him the idea but who lacks the right, or whose right
    would not let this one through without someone's agreement, a turn of his own, a review) the
    owner of now. Their right is read on ``action`` as the barrier would read their own call
    (``policy.decision.decide_now``: the owner's limit on it, its approver, what it asks): only an
    ``Allow`` covers it ("guests, three days at most": two days are covered, an adult is not)."""
    if not acts_alone(ctx):
        # The owner's directives are read on the assistant's own side, with his exception.
        decision = await decide_now(
            session, ctx.actor, action, ctx.policy, ctx.clock(), directives_read=True
        )
        if isinstance(decision, Allow):
            return ctx.person.id
    return await owner_of_now(session, ctx.clock())


# The effect of an invitation read for the person of the turn (``invitation_action``): never a
# tool's own description of a call.
_PERMISSIONS: Final = "permissions"


def invitation_action(
    action_type: str, role: MemberRole, days: int | None, no_end: bool, name: str
) -> DescribedAction:
    """An invitation as the barrier reads it for the person of the turn (``link_for_his_own``):
    its role, its stay, its name, nobody asked first."""
    lasts = stay_days(role, days, no_end=no_end)
    return DescribedAction(
        connector="core",
        action_type=action_type,
        target=None,
        effect=_PERMISSIONS,
        destination="household",
        facts={
            "role": role,
            "days": lasts,
            "no_end": role == "guest" and lasts is None,
            "display_name": name,
            "owner_first": False,
            "exception_of": None,
            "serves_the_speaker": False,
        },
    )


async def whose_link(
    session: AsyncSession, ctx: ToolContext, his: bool, action: DescribedAction
) -> tuple[UUID, bool]:
    """For whom the link a call makes is, and whether it is shown at once (``card_once``): the
    one who asked, shown unless it is left on their card (``leave_on_their_card``); for the
    assistant's own act, ``link_for_his_own``, shown only to the person of the turn who asked
    for nothing of it in a turn nobody reads (with no tool to send, a link nobody asked for
    waits on the owner's card: WEB-3)."""
    if not his:
        return ctx.person.id, not ctx.leave_on_their_card
    holder = await link_for_his_own(session, ctx, action)
    shows = holder == ctx.person.id and not ctx.leave_on_their_card and not acts_alone(ctx)
    return holder, shows


_HAS_AN_ACCOUNT: Final = "this person has an account already: they sign in with it"


async def _invitable(session: AsyncSession, record: Person, now: datetime) -> None:
    """``InvalidArguments`` unless ``record`` may be given an account by an invitation: it has
    none yet, and it was not here and left (bring them back instead)."""
    if await has_an_account(session, record):
        raise InvalidArguments("invalid", _HAS_AN_ACCOUNT)
    if record.state == "gone" or (record.state == "has_access" and not is_active(record, now)):
        raise InvalidArguments("not_found", _THEY_LEFT)


class HouseholdInvite:
    """Invite someone to join the household (the one who asked sees the link, once)."""

    @staticmethod
    def fixed_refusal(actor: Actor, facts: Mapping[str, FactValue], present: Present) -> str | None:
        """What no right moves (``RefusesWhateverTheRights``): the owner's role is never given."""
        return gives_a_member_role(facts)

    definition = ToolDefinition(
        name="household_invite",
        description=(
            "Invite someone to join the household, when that is what the person you are talking "
            "with wants or your own idea: someone new by the name the household will call them "
            "(display_name), or someone you know already by their record (person: their name or "
            "person:<id>), who keeps everything you know of them; their role (adult, child or "
            "guest) and, for a guest only, how many days they may stay once they have joined "
            f"({GUEST_DAYS_DEFAULT} if the person does not say), or no_end true for a stay with "
            "no end: any length may be given, when the person asks for it or it is your "
            "judgement. With "
            "username, the username they are offered to sign in with (they may change it; "
            "their password is theirs alone). With "
            "uses_my_key, whether they may use the AI keys the person gave (true), or must bring "
            "a key of their own (false); leave it out when the person does not say: the "
            "household's default decides. With owner_first true, the owner must agree "
            "before they join: set it when, in your judgement, the owner must agree first "
            "(someone the household does not know, another adult, someone who will "
            "use the household's general key, anything that seems to deserve it); leave it out "
            "when the request is plain (the person's own child, say). An invitation made by "
            "whoever owns the household waits for nobody. When the request makes the choices "
            "plain, make them "
            "without asking, and say them as you act: someone close to the person who invites "
            "(their partner, say) is an adult, with no end date, on that person's keys "
            "(uses_my_key true). Ask only what would change the invitation and you cannot guess; "
            "a request that waits shows your choices. The result says whether it was done, "
            "waits for a yes, or was not done. When it is done, the person is shown a link to "
            "send them, which works once, for 7 days (if someone else said yes, it waits until "
            "the person has it shown): you never see it, and the person sends it themselves. An "
            "invitation of your own idea is yours: its link goes to the person only if they "
            "could invite themselves, else it waits for whoever owns the household, who is told "
            "of it afterwards. " + whose.HIS_OWN_IDEA
        ),
        parameters={
            "type": "object",
            "properties": {
                "display_name": {
                    "type": "string",
                    "description": "Someone new: the name the household will call them.",
                    "minLength": 1,
                    "maxLength": MAX_DISPLAY_NAME_LENGTH,
                },
                "person": {
                    "type": "string",
                    "description": "Someone you know: their name, or person:<id>.",
                },
                "role": {**ROLE_SCHEMA, "description": "Their role in the household."},
                "guest_days": {
                    "type": "integer",
                    "description": "For a guest only: how many days they may stay.",
                    "minimum": 1,
                },
                "no_end": {
                    "type": "boolean",
                    "description": "For a guest only: true for a stay with no end (not with "
                    "guest_days).",
                },
                "username": {
                    "type": "string",
                    "description": "The username they are offered to sign in with.",
                    "minLength": 1,
                    "maxLength": MAX_USERNAME_LENGTH,
                },
                "uses_my_key": {
                    "type": "boolean",
                    "description": "Whether they may use the person's AI keys (false: they bring "
                    "their own). Only if the person says, or the request makes it plain.",
                },
                "owner_first": {
                    "type": "boolean",
                    "description": "True when, in your judgement, the owner must agree "
                    "before they join. Leave it out when the request is plain.",
                },
                "reason": whose.REASON_PROPERTY,
                "exception_of": whose.EXCEPTION_PROPERTY,
            },
            "required": ["role"],
            "additionalProperties": False,
        },
    )
    action_type = "household.invite"

    async def acts_on(self, arguments: JsonObject, ctx: ToolContext) -> str | None:
        """The invitation that holds the name the call asks for (``ActsOn``): ``invitation:<id>``
        of the one open invitation made for it, found by the name alone; ``None`` when the name
        is free or means a person, or when the tool does not accept the arguments. A chain of
        actions uses it for "cancel the invitation of Dana, then invite Dana": the name is taken
        until the first step is done. A record named is the one it acts on."""
        try:
            call = _invitation_from(arguments)
        except InvalidArguments:
            return None
        if call.person is not None:
            return await person_named(call.person, ctx, "records")
        assert call.name is not None
        return await invitation_named(call.name, ctx)

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        call = _invitation_from(arguments)
        whose.check(arguments, actor)
        _uses_my_key(arguments)
        owner_first = _owner_first(arguments)
        target = None
        role, days, no_end = call.role, call.days, call.no_end
        async with ctx.session_factory() as session:
            if call.person is not None:
                record = await record_named(session, call.person, ctx)
                await _invitable(session, record, ctx.clock())
                name, target = record.display_name, f"person:{record.id}"
                # What the page states, and what they will have: a record with access keeps its
                # own role and stay.
                role, days, no_end = terms_for_record(
                    record, call.role, call.days, ctx.clock(), no_end=call.no_end
                )
                speaker = record.id == ctx.person.id
                excluding: UUID | None = record.id
            else:
                assert call.name is not None
                name, speaker, excluding = call.name, False, None
            if await name_in_use(session, name, now=ctx.clock(), excluding=excluding):
                raise InvalidArguments("invalid", NAME_IN_USE)
        # The name is a fact (it goes to the audit of the decision, which holds names); a new
        # person's invitation has no target, as the person does not exist yet.
        lasts = stay_days(role, days, no_end=no_end)
        facts: dict[str, FactValue] = {
            "role": role,
            "days": lasts,
            "no_end": role == "guest" and lasts is None,
            "display_name": name,
            "owner_first": owner_first,
            "exception_of": whose.exception_of(arguments, actor),
            # Someone who does not exist yet is never the person who speaks; a record may be.
            "serves_the_speaker": speaker,
        }
        refused = self.fixed_refusal(actor, facts, {})
        if refused is not None:
            raise InvalidArguments("invalid", refused)
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=target,
            effect="permissions",
            destination="household",
            facts=facts,
        )

    async def _record(
        self, session: AsyncSession, call: _Invite, arguments: JsonObject, ctx: ToolContext
    ) -> Person | None:
        """The record the call invites, locked and checked again, ``None`` for someone new. The
        owner's own order gives back what the owner withdrew or refused (the directive is
        lifted); anyone else's call meets it (``not_possible``)."""
        if call.person is None:
            return None
        now = ctx.clock()
        record = await record_locked(session, call.person, ctx)
        await _invitable(session, record, now)
        directive = await directives.under_directive(session, record.id, now)
        if directive is not None:
            if not whose.by_the_owner(arguments, ctx):
                raise InvalidArguments("not_possible", _UNDER_A_DIRECTIVE)
            await directives.lift_all_on(session, record.id, owner=ctx.person, now=now)
        return record

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        call = _invitation_from(arguments)
        uses_my_key = _uses_my_key(arguments)
        _owner_first(arguments)
        his = whose.is_his_own(arguments, ctx)
        by = whose.by(arguments, ctx)
        now = ctx.clock()
        async with ctx.session_factory() as session:
            # Until the commit: a rename cannot take the name between this check and the insert.
            await lock_display_names(session)
            record = await self._record(session, call, arguments, ctx)
            name = record.display_name if record is not None else call.name
            assert name is not None
            if await name_in_use(
                session, name, now=now, excluding=None if record is None else record.id
            ):
                raise InvalidArguments("invalid", NAME_IN_USE)
            # The inviter of an act of his is the assistant, never the person of the turn (S-K).
            asked = invitation_action("household.invite", call.role, call.days, call.no_end, name)
            holder, shows = await whose_link(session, ctx, his, asked)
            invitation, token = await create_invitation(
                session,
                display_name=name,
                role=call.role,
                guest_days=call.days,
                no_end=call.no_end,
                created_by=None if his else ctx.person.id,
                now=now,
                with_token=shows,
                uses_inviter_key=uses_my_key,
                record=record,
                proposed_username=call.username,
                link_for=holder if his else None,
                reason=whose.reason_of(arguments),
            )
            owner: UUID | None = None
            if record is not None or his:
                # The trace of the act, with its reason when it is the assistant's. Every
                # invitation of his own idea, by a record or by a name, is told to the owner once
                # (the owner's answer of 2026-11-27, "Oui, tu es prévenu").
                act = await note_act(
                    session,
                    act="invited",
                    by=by,
                    reason=whose.reason_of(arguments),
                    turn_of=whose.turn_of(arguments, ctx),
                    person_id=None if record is None else record.id,
                    invitation_id=invitation.id,
                    owed_to_owner=his,
                    now=now,
                )
                if his:
                    owner = await tell_owner(
                        session, act, knows=lambda one: whose.owner_knows(ctx, one), now=now
                    )
                    await directives.note_exceptions(
                        session,
                        whose.exception_of(arguments, ctx.acting),
                        person_id=None if record is None else record.id,
                        invitation_id=invitation.id,
                        turn_of=whose.turn_of(arguments, ctx),
                        now=now,
                    )
            details: dict[str, str | int | bool] = {
                "invitation_id": str(invitation.id),
                "name": name,
                "role": invitation.role,
            }
            if record is not None:
                details["person_id"] = str(record.id)
            if invitation.guest_days is not None:
                details["guest_days"] = invitation.guest_days
            if uses_my_key is not None:
                details["uses_inviter_key"] = uses_my_key
            # Last, just before the commit. Never the token.
            await append_audit(session, actor=by, action="household.invited", details=details)
            await session.commit()
        if owner is not None and ctx.notify is not None:
            # The owner is owed the telling: a turn for him is due.
            ctx.notify(owner)
        params = {"name": name}
        dates = {"expires": card_date(invitation.expires_at)}
        model_result: JsonObject = {"invited": name, "role": invitation.role}
        if record is not None:
            model_result["record_kept"] = True
        if token is None and holder != ctx.person.id:
            # His own invitation, whose link is not for the person of the turn: it waits for the
            # owner, who has it shown when he asks for it.
            model_result["link"] = "waits_for_the_owner"
            return ToolOutcome(
                status="done",
                model_result=model_result,
                card={"key": "action.card.done.household.invite", "params": params, "dates": dates},
            )
        if token is None:
            # Someone else said yes, or no page reads the turn: the link is for the one who asked,
            # who makes it with the button of their card (the invitation's identifier is no
            # secret), or asks for it in words (``invitation_link_show``).
            model_result["link"] = "waits_for_them"
            return ToolOutcome(
                status="done",
                model_result=model_result,
                card={
                    "key": "action.card.done.household.invite.on_request",
                    "params": params,
                    "dates": dates,
                    "invitation": str(invitation.id),
                },
            )
        link = f"{ctx.origin.rstrip('/')}/invite/{token}"
        model_result["link_shown_to_owner"] = True
        return ToolOutcome(
            status="done",
            model_result=model_result,
            card={"key": "action.card.done.household.invite", "params": params, "dates": dates},
            # Shown once, to the one who asked, in the answer to this request: never stored,
            # never told to the model.
            card_once={
                "key": "action.card.once.household.invite",
                "params": {"name": name},
                "link": link,
            },
        )


class HouseholdChangeRole:
    """Give someone a role: an access to someone only known, another role to someone who has
    one."""

    definition = ToolDefinition(
        name="household_change_role",
        description=(
            "Give someone a role, when that is what the person you are talking with wants or your "
            "own idea: adult, child, guest, or a role made for the household, by its name. To "
            "someone you only know (a record with no access, with or without an account), "
            "it gives them access with that role; to someone in the household, it changes their "
            "role. Name them as the household knows them, or by person:<id> when two have the "
            "same name. Nobody is made owner here, and whoever is owner keeps that role (it is "
            "only handed over). Someone made a guest may stay a number of days, from now, then "
            "they lose access, or with no end (no_end true): any length may be given, when the "
            f"person asks for it or it is your judgement. If nothing is said, {GUEST_DAYS_DEFAULT} "
            "days for someone with an account; someone without one keeps the end they have, or "
            "none (you judge when they left, and end the stay with record_complete). Someone who "
            "stops being a guest stays for good. Someone who is already a guest may be given a new "
            "stay (guest_days from now, longer or shorter, or no_end): say so as a change of stay. "
            "Someone who is not a guest may be given an end of their access the same way "
            "(guest_days from now, their role unchanged), when that is what is asked. Someone "
            "noted as passing by who now lives here is said so here (lives_here true), never "
            "silently: it changes whose keys they use and whether they may agree for a child. "
            + whose.TELL_OWNER_IS_YOURS
            + " "
            "Someone without an account whose stay is over is given a new one; someone else who "
            "left the household is brought back instead. Someone without an account cannot "
            "talk with you yet: they ask through someone of the house, and the request is then "
            "that person's, made with their rights. The result says whether it was done, waits "
            "for a yes, or was not done. " + whose.HIS_OWN_IDEA
        ),
        parameters={
            "type": "object",
            "properties": {
                "person": {"type": "string", "description": "Their name, or person:<id>."},
                "role": {
                    "type": "string",
                    "description": (
                        "Their new role: adult, child, guest, or the name of a role made for "
                        "the household."
                    ),
                },
                "guest_days": {
                    "type": "integer",
                    "description": "How many days they may stay, from now (for someone who is "
                    "not a guest: when their access ends).",
                    "minimum": 1,
                },
                "no_end": {
                    "type": "boolean",
                    "description": "For a guest only: true for a stay with no end (not with "
                    "guest_days).",
                },
                "reason": whose.REASON_PROPERTY,
                "exception_of": whose.EXCEPTION_PROPERTY,
                "lives_here": {
                    "type": "boolean",
                    "description": "True when someone noted as passing by now lives here (with "
                    "the role they have or a new one).",
                },
                "tell_owner": whose.TELL_OWNER_PROPERTY,
            },
            "required": ["person", "role"],
            "additionalProperties": False,
        },
    )
    action_type = "household.change_role"
    # The argument that names someone: given its whole name before the call is described
    # (``tools.context.NamesPeople``, spec 6.7), among every record (a role gives access to
    # someone only known).
    person_arguments: Final = ("person",)
    names_among: Final = "records"

    @staticmethod
    def _arguments(arguments: JsonObject) -> str:
        """The name the call gives; the role and the days are read with the record
        (``_role_and_days``)."""
        keys = frozenset(arguments)
        if not {"person", "role"} <= keys or not keys <= _CHANGE_ROLE_ARGUMENTS:
            raise InvalidArguments(
                "invalid",
                "the arguments are person, role and, if needed, guest_days or no_end (a guest "
                "only) and reason",
            )
        role = arguments["role"]
        if not isinstance(role, str) or not role.strip():
            raise InvalidArguments("invalid", _A_ROLE)
        return name_from(arguments["person"], "person")

    @staticmethod
    async def _role_and_days(
        session: AsyncSession, arguments: JsonObject
    ) -> tuple[MemberRole, MadeRole | None, int | None, bool]:
        _comes_to_live(arguments)
        role, made = await role_named(session, arguments["role"])
        # Days for anyone here: for someone who is not a guest, when their access ends.
        return role, made, guest_days_from(arguments, "guest"), no_end_from(arguments)

    @staticmethod
    def fixed_refusal(actor: Actor, facts: Mapping[str, FactValue], present: Present) -> str | None:
        """What no right moves (``RefusesWhateverTheRights``): the owner's role never changes,
        nobody changes their own role (it would be giving oneself more power), the owner's role is
        never given, and someone who has access is not given the role they have, unless they are
        a guest given a new stay (spec 5.5: a change of stay, the days of its facts counted from
        now). Giving a role to someone only known gives them access: it is never the role they
        have."""
        if facts.get("current_role") == "owner":
            return "the owner's role never changes"
        if facts.get("person") == f"person:{actor.person_id}":
            return "nobody changes their own role"
        given = gives_a_member_role(facts)
        if given is not None:
            return given
        new_stay = (
            facts.get("days") is not None
            or (facts.get("role") == "guest" and facts.get("no_end") is True)
            or facts.get("comes_to_live") is True
        )
        has_access = facts.get("state", "has_access") == "has_access"
        same = facts.get("role") == facts.get("current_role") and facts.get("made_role") is None
        if has_access and same and not new_stay:
            return "the person already has this role"
        return None

    async def _checked(
        self,
        session: AsyncSession,
        person: Person,
        arguments: JsonObject,
        actor: Actor,
        speaker: UUID,
        now: datetime,
    ) -> tuple[dict[str, FactValue], MadeRole | None, int | None, bool]:
        """The facts of giving ``person`` the role of the call, the made role it names, and what
        it does to their stay (``stay_given``: the days given, whether it has no end), once
        ``fixed_refusal`` lets them through. A guest who stays a guest is given a new stay: the
        call says how long (``guest_days``, or ``no_end``), or there is nothing to change (read
        from the call). A record without an account whose stay is over (``stay_over``) is given a
        new one; the facts say when its access wakes at its next visit (``returns``). Anyone else
        who left is brought back, not given a role."""
        role, made, days, no_end = await self._role_and_days(session, arguments)
        state = person.state
        has_account = await has_an_account(session, person)
        stay_over = state == "has_access" and not is_active(person, now)
        if state not in _GIVES_A_ROLE_TO or (stay_over and has_account):
            raise InvalidArguments("not_found", _THEY_LEFT)
        given, lasts = stay_given(
            role, days, no_end, person=person, has_account=has_account, now=now
        )
        facts: dict[str, FactValue] = {
            "person": f"person:{person.id}",
            "current_role": person.role,
            "role": role,
            "days": lasts,
            "no_end": role == "guest" and lasts is None,
            "returns": records.returns_now(person, now),
            "made_role": None if made is None else made.name,
            "has_account": has_account,
            "state": "stay_over" if stay_over else state,
            "exception_of": whose.exception_of(arguments, actor),
            # The record that gets the role is the person whose turn it is: saying who one is
            # gives nothing by itself, and the judge is shown it.
            "serves_the_speaker": person.id == speaker,
            "comes_to_live": False,
            "off_the_guests_key_rule": False,
            "may_then_agree_for_a_child": False,
        }
        if _comes_to_live(arguments) and person.standing != "lives_here":
            # Someone passing by who now lives here (the owner's answer of 2026-11-27, judged and
            # told): what it changes, for the judge.
            settings = await load_policy_settings(session)
            facts["comes_to_live"] = True
            facts["off_the_guests_key_rule"] = settings.guests_bring_own and role != "guest"
            facts["may_then_agree_for_a_child"] = role == "adult" and not person.is_child
        # Someone who carries a made role is not "given a role they have" when they are given its
        # plain base role: that is another role given (and the made role may then be unused).
        carried = await made_role_name(session, person)
        refused = self.fixed_refusal(
            actor, {**facts, "made_role": facts["made_role"] or carried}, present_as(person)
        )
        if refused is not None:
            raise InvalidArguments("invalid", refused)
        made_id = None if made is None else made.id
        same = role == person.role and made_id == person.made_role_id
        moves_in = facts.get("comes_to_live") is True
        if state == "has_access" and not stay_over and same and not moves_in:
            if role == "guest" and days is None and not no_end:
                raise InvalidArguments("invalid", _STAY_NEEDS_DAYS)
            if role != "guest" and days is None:
                raise InvalidArguments("invalid", "the person already has this role")
        return facts, made, given, no_end

    async def acts_on(self, arguments: JsonObject, ctx: ToolContext) -> str | None:
        """Whom the call names (``ActsOn``), found by the name alone among the records; nobody
        when the tool does not accept its arguments."""
        try:
            name = self._arguments(arguments)
        except InvalidArguments:
            return None
        return await person_named(name, ctx, "records")

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        name = self._arguments(arguments)
        whose.check(arguments, actor)
        whose.tells_the_owner(arguments, actor)
        async with ctx.session_factory() as session:
            person = await record_named(session, name, ctx)
            facts, _, _, _ = await self._checked(
                session, person, arguments, actor, ctx.person.id, ctx.clock()
            )
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=f"person:{person.id}",
            effect="permissions",
            destination="household",
            facts=facts,
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        name = self._arguments(arguments)
        by = whose.by(arguments, ctx)
        by_owner = whose.by_the_owner(arguments, ctx)
        now = ctx.clock()
        async with ctx.session_factory() as session:
            person = await record_locked(session, name, ctx)
            facts, made, given, no_end = await self._checked(
                session, person, arguments, ctx.acting, ctx.person.id, now
            )
            role = role_from(facts["role"])
            old_role, shown = person.role, person.display_name
            # Someone whose stay is over has no access now: it is given again, for a new stay.
            had_access = facts["state"] == "has_access"
            leaves_the_child_role = old_role == "child" and role != "child"
            proposes = await records.may_propose_telling(session, person, by=by)
            try:
                await records.give_access(
                    session,
                    person,
                    role=role,
                    made_role=made,
                    standing="lives_here" if facts.get("comes_to_live") is True else None,
                    guest_days=given,
                    no_end=no_end,
                    by=by,
                    by_owner=by_owner,
                    reason=whose.reason_of(arguments),
                    turn_of=whose.turn_of(arguments, ctx),
                    now=now,
                    seen=whose.seen(ctx),
                )
            except records.UnderDirective:
                raise InvalidArguments("not_possible", _UNDER_A_DIRECTIVE) from None
            except records.NotPossible as error:
                raise not_possible(error) from None
            # His own gesture, told to the owner when he judged the owner would want it.
            told = (
                await tell_owner_of(
                    session,
                    "access_given",
                    knows=lambda one: whose.owner_knows(ctx, one),
                    now=now,
                    person_id=person.id,
                )
                if whose.tells_the_owner(arguments, ctx.acting)
                else None
            )
            await directives.note_exceptions(
                session,
                facts["exception_of"] if isinstance(facts["exception_of"], str) else None,
                person_id=person.id,
                turn_of=whose.turn_of(arguments, ctx),
                now=now,
            )
            if leaves_the_child_role and by_owner:
                # The owner lifts the mark the child role carried; anyone else leaves it (it is
                # lifted by the owner, or by the assistant on a date of birth:
                # ``records.complete``).
                await records.complete(
                    session,
                    person,
                    is_child=False,
                    by=by,
                    reason=None,
                    turn_of=whose.turn_of(arguments, ctx),
                    now=now,
                )
            end = person.expires_at
            # The days of their stay, when it has an end (a guest's), or none.
            stay = facts["days"]
            model_result: JsonObject = {"person": shown, "role": made.name if made else role}
            params = {"name": shown}
            dates: dict[str, str] = {}
            # A guest's stay with no end has cards of its own: there is no day to say.
            no_end_card = ".no_end" if role == "guest" and end is None else ""
            if end is not None and isinstance(stay, int):
                model_result["guest_days"] = stay
                dates["until"] = card_date(end)
            elif role == "guest":
                model_result["no_end"] = True
            if facts["returns"] is True:
                model_result["returns"] = True
            if not had_access:
                model_result["access_given"] = True
                if proposes:
                    model_result["may_propose_telling"] = True
            elif old_role == role and made is None:
                # A guest who stays a guest has a new stay (spec 5.5): a change of their end, not
                # of their role (``_checked`` lets nothing else through), so the rights they keep
                # are no news.
                model_result["stay_changed"] = True
                changed: dict[str, str | int | bool] = {"person_id": str(person.id), "name": shown}
                if isinstance(stay, int):
                    changed["guest_days"] = stay
                elif role == "guest":
                    changed["no_end"] = True
                if facts.get("comes_to_live") is True:
                    model_result["lives_here"] = True
                    changed["lives_here"] = True
                await append_audit(
                    session, actor=by, action="household.stay_changed", details=changed
                )
                await session.commit()
                if told is not None and ctx.notify is not None:
                    ctx.notify(told)
                # Someone who is not a guest: an end of their access, or that they live here
                # now, each with its own words.
                card_key = f"action.card.done.household.change_role.stay{no_end_card}"
                if role != "guest" and isinstance(stay, int):
                    card_key = "action.card.done.household.change_role.access_ends"
                elif role != "guest" and facts.get("comes_to_live") is True:
                    card_key = "action.card.done.household.change_role.lives_here"
                return ToolOutcome(
                    status="done",
                    model_result=model_result,
                    card={
                        "key": card_key,
                        "params": params,
                        "dates": dates,
                    },
                )
            # They keep the rights given to them (spec 5.12). The card says which, whoever asked
            # (Ruling R26): the owner reads it, whether they asked or said yes to someone else's
            # request (a yes by the button never sends the model's result back to Domo). Domo
            # is told them when the owner asked, to tell the owner.
            rights: JsonObject = {}
            kept = await personal_right_summaries(session, person.id)
            if kept:
                rights["rights"] = {"key": "action.card.rights.kept", "items": kept}
                if ctx.person.role == "owner":
                    model_result["their_own_rights"] = kept
            if had_access:
                details: dict[str, str | int] = {
                    "person_id": str(person.id),
                    "name": shown,
                    "old_role": old_role,
                    "new_role": role,
                }
                if end is not None and isinstance(stay, int):
                    details["guest_days"] = stay
                await append_audit(
                    session, actor=by, action="household.role_changed", details=details
                )
            owner = await records.owner_to_tell(session, person, now) or told
            if facts.get("comes_to_live") is True:
                model_result["lives_here"] = True
            await session.commit()
        if owner is not None and ctx.notify is not None:
            # The owner asked to be told of each access given: a turn for them is due.
            ctx.notify(owner)
        return ToolOutcome(
            status="done",
            model_result=model_result,
            card={
                "key": f"action.card.done.household.change_role.{role}{no_end_card}",
                "params": params,
                "dates": dates,
                **rights,
            },
        )


async def _given_by_domo(session: AsyncSession, person: Person) -> bool:
    """Whether the access ``person`` has was given by the assistant: the last ``access_given`` act
    on the record is his (``people.acts``)."""
    by = await session.scalar(
        select(PersonAct.by)
        .where(PersonAct.person_id == person.id, PersonAct.act == "access_given")
        .order_by(PersonAct.at.desc(), PersonAct.id)
        .limit(1)
    )
    return by == DOMO_LABEL


def _gone_since_the_yes(ctx: ToolContext, person: Person, now: datetime) -> None:
    """``not_found`` for a removal that runs under a yes (a stored request, a step of a chain)
    of someone no longer in the household: the yes covered taking away an access that is no
    longer there, so it can no longer be done. The owner's withdrawal in advance of someone with
    no access is his own request, on its own (final review B M4)."""
    if (ctx.request_id is not None or ctx.in_a_chain) and not is_active(person, now):
        raise InvalidArguments("not_found", _WHY_NOT["no_access"])


class HouseholdRemove:
    """Take someone's access away (their account, if they have one, is closed; their data kept)."""

    definition = ToolDefinition(
        name="household_remove",
        description=(
            "Take someone's access away: they leave the household. Their account, if they have "
            "one, is closed at once, and what they wrote is kept. The rights given to them in "
            "person are paused (paused_rights says how many): if they return, giving them back is "
            "for the owner only. Name them as the household knows them, or by person:<id>. It may "
            "be the person themselves, who then leaves; never anyone whose role is owner. When "
            "the household's owner orders it, it is their withdrawal: the access is given back "
            "by the owner only. The result says whether it was done, waits for a yes, or was not "
            "done. " + whose.TELL_OWNER_IS_YOURS + " " + whose.HIS_OWN_IDEA
        ),
        parameters={
            "type": "object",
            "properties": {
                "person": {"type": "string", "description": "Their name, or person:<id>."},
                "reason": whose.REASON_PROPERTY,
                "tell_owner": whose.TELL_OWNER_PROPERTY,
            },
            "required": ["person"],
            "additionalProperties": False,
        },
    )
    action_type = "household.remove"
    # The argument that names someone: given its whole name before the call is described
    # (``tools.context.NamesPeople``, spec 6.7), among every record.
    person_arguments: Final = ("person",)
    names_among: Final = "records"

    @staticmethod
    def _arguments(arguments: JsonObject) -> str:
        keys = frozenset(arguments)
        if "person" not in keys or not keys <= _REMOVE_ARGUMENTS:
            raise InvalidArguments(
                "invalid", "the arguments are person and, if needed, reason and tell_owner"
            )
        return name_from(arguments["person"], "person")

    @staticmethod
    def fixed_refusal(actor: Actor, facts: Mapping[str, FactValue], present: Present) -> str | None:
        """What no right moves (``RefusesWhateverTheRights``): the owner is never removed.
        Oneself is fine: removing oneself gives no power, and the person can be brought back."""
        if facts.get("current_role") == "owner":
            return "the owner is never removed"
        return None

    async def _checked(
        self, session: AsyncSession, person: Person, actor: Actor, now: datetime
    ) -> dict[str, FactValue]:
        """The facts of removing ``person``, once ``fixed_refusal`` lets them through. Only
        someone who has access now is removed: a guest whose stay is over is no longer in the
        household, and someone with no access has nothing to take away (the owner refuses or
        withdraws one in advance by a directive of theirs: the owner's own call is that
        withdrawal, ``records.remove``)."""
        owners_own = actor.subject == "person" and actor.role == "owner"
        if person.state != "has_access" and not owners_own:
            raise InvalidArguments("not_found", _WHY_NOT["no_access"])
        if person.state == "has_access" and not is_active(person, now) and not owners_own:
            raise InvalidArguments(
                "not_found",
                "their stay is over: they are no longer here; the owner keeps them away by "
                "withdrawing their access himself",
            )
        facts: dict[str, FactValue] = {
            "person": f"person:{person.id}",
            "current_role": person.role,
            "is_self": person.id == actor.person_id,
            "made_role": await made_role_name(session, person),
            "has_account": await has_an_account(session, person),
            "state": person.state,
            GIVEN_BY_DOMO_FACT: await _given_by_domo(session, person),
        }
        refused = self.fixed_refusal(actor, facts, present_as(person))
        if refused is not None:
            raise InvalidArguments("invalid", refused)
        return facts

    async def acts_on(self, arguments: JsonObject, ctx: ToolContext) -> str | None:
        """Whom the call names (``ActsOn``), found by the name alone among the records; nobody
        when the tool does not accept its arguments."""
        try:
            name = self._arguments(arguments)
        except InvalidArguments:
            return None
        return await person_named(name, ctx, "records")

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        name = self._arguments(arguments)
        whose.check(arguments, actor)
        whose.tells_the_owner(arguments, actor)
        async with ctx.session_factory() as session:
            person = await record_named(session, name, ctx)
            _gone_since_the_yes(ctx, person, ctx.clock())
            facts = await self._checked(session, person, actor, ctx.clock())
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=f"person:{person.id}",
            effect="permissions",
            destination="household",
            facts=facts,
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        name = self._arguments(arguments)
        by = whose.by(arguments, ctx)
        now = ctx.clock()
        async with ctx.session_factory() as session:
            person = await record_locked(session, name, ctx)
            _gone_since_the_yes(ctx, person, now)
            await self._checked(session, person, ctx.acting, now)
            shown, role = person.display_name, person.role
            # Read before they are paused: the card says which rights wait for the owner, whoever
            # asked (Ruling R26), as at a return.
            waiting = await personal_right_summaries(session, person.id)
            try:
                # The owner's own order is a withdrawal (ruling 13): only the owner gives back.
                removed = await records.remove(
                    session,
                    person,
                    by=by,
                    by_owner=whose.by_the_owner(arguments, ctx),
                    reason=whose.reason_of(arguments),
                    turn_of=whose.turn_of(arguments, ctx),
                    now=now,
                )
            except records.NotPossible as error:
                raise not_possible(error) from None
            state = person.state
            # His own removal is told to the owner when he judged the owner would want it.
            owner = (
                await tell_owner_of(
                    session,
                    "access_removed",
                    knows=lambda one: whose.owner_knows(ctx, one),
                    now=now,
                    person_id=person.id,
                )
                if whose.tells_the_owner(arguments, ctx.acting)
                else None
            )
            await append_audit(
                session,
                actor=by,
                action="household.removed",
                details={
                    "person_id": str(person.id),
                    "name": shown,
                    "role": role,
                    "requests_refused": removed.requests_refused,
                },
            )
            await session.commit()
        if owner is not None and ctx.notify is not None:
            ctx.notify(owner)
        rights: JsonObject = {}
        if waiting:
            rights["rights"] = {"key": "action.card.rights.paused", "items": waiting}
        return ToolOutcome(
            status="done",
            model_result={
                "removed": shown,
                "paused_rights": removed.rights_paused,
                # The owner's own order: a withdrawal, which only the owner lifts.
                **({"withdrawn_by_the_owner": True} if state == "withdrawn" else {}),
            },
            card={
                "key": "action.card.done.household.remove",
                "params": {"name": shown},
                **rights,
            },
        )


class HouseholdLeave:
    """The person who speaks leaves the household (their account is closed, their data kept)."""

    definition = ToolDefinition(
        name="household_leave",
        description=(
            "The person you are talking with leaves the household themselves, when that is what "
            "they want (a guest may leave before their stay ends): their account is closed at "
            "once, what they wrote is kept, and they can be brought back later. The rights given "
            "to them in person are paused (paused_rights says how many). For a child, an adult "
            "must agree. If their role is owner, they hand it over first: they cannot leave "
            "before. To remove someone else, it is household_remove."
        ),
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
    )
    action_type = "household.leave"

    @staticmethod
    def _arguments(arguments: JsonObject) -> None:
        if arguments:
            raise InvalidArguments("invalid", "this tool takes no arguments")

    @staticmethod
    def fixed_refusal(actor: Actor, facts: Mapping[str, FactValue], present: Present) -> str | None:
        """What no right moves (``RefusesWhateverTheRights``): the owner never leaves (the
        household would have none); they hand their role over first. ``current_role`` is the role
        the database holds for the person now, never the role the caller believes."""
        if facts.get("current_role") == "owner":
            return _OWNER_NEVER_LEAVES
        return None

    def _checked(self, person: Person, actor: Actor) -> dict[str, FactValue]:
        """The facts of ``person`` leaving, once ``fixed_refusal`` lets them through."""
        facts: dict[str, FactValue] = {"current_role": person.role}
        refused = self.fixed_refusal(actor, facts, present_as(person))
        if refused is not None:
            raise InvalidArguments("invalid", refused)
        return facts

    async def acts_on(self, arguments: JsonObject, ctx: ToolContext) -> str | None:
        """Whom the call acts on (``ActsOn``): the person who asks, so that a chain may hand the
        owner's role over, then have the one who gave it leave (each checked in its turn); nobody
        when the tool does not accept its arguments."""
        try:
            self._arguments(arguments)
        except InvalidArguments:
            return None
        return f"person:{ctx.actor.person_id}"

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        self._arguments(arguments)
        async with ctx.session_factory() as session:
            # The role of now, read again: ``actor`` may carry the role of an earlier moment.
            found = await session.execute(select(Person).where(Person.id == actor.person_id))
            person = found.scalar_one()
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=f"person:{person.id}",
            effect="permissions",
            destination="household",
            facts=self._checked(person, actor),
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        self._arguments(arguments)
        now = ctx.clock()
        async with ctx.session_factory() as session:
            locked = await session.execute(
                select(Person)
                .where(Person.id == ctx.actor.person_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            person = locked.scalar_one()
            described_target(ctx, f"person:{person.id}")
            if not is_active(person, now):
                raise InvalidArguments("not_found", "the person is no longer in the household")
            self._checked(person, ctx.actor)
            shown, role = person.display_name, person.role
            # They leave themselves: they are the one who does it (``rights.paused`` too), and the
            # trace of the act says so.
            try:
                left = await records.remove(
                    session,
                    person,
                    by=f"person:{person.id}",
                    by_owner=False,
                    reason=None,
                    turn_of=whose.turn_of(arguments, ctx),
                    now=now,
                )
            except records.NotPossible as error:
                raise not_possible(error) from None
            await append_audit(
                session,
                actor=f"person:{person.id}",
                action="household.left",
                details={
                    "person_id": str(person.id),
                    "name": shown,
                    "role": role,
                    "requests_refused": left.requests_refused,
                    "rights_paused": left.rights_paused,
                },
            )
            await session.commit()
        return ToolOutcome(
            status="done",
            model_result={"left": shown, "paused_rights": left.rights_paused},
            card={"key": "action.card.done.household.leave", "params": {"name": shown}},
        )


class HouseholdCancelInvitation:
    """Cancel an invitation that waits (its link stops working)."""

    definition = ToolDefinition(
        name="household_cancel_invitation",
        description=(
            "Cancel an invitation that has not been used yet, when that is what the person you "
            "are talking with wants, or your own idea: its link stops working. Name it by the "
            "name it was made for. One you made may be cancelled by whoever holds that right; "
            "one a person made, only by them or whoever owns the household; your own idea may "
            "cancel any. " + whose.TELL_OWNER_IS_YOURS + " " + whose.HIS_OWN_IDEA
        ),
        parameters={
            "type": "object",
            "properties": {
                "display_name": {
                    "type": "string",
                    "description": "The name the invitation was made for.",
                },
                "reason": whose.REASON_PROPERTY,
                "tell_owner": whose.TELL_OWNER_PROPERTY,
            },
            "required": ["display_name"],
            "additionalProperties": False,
        },
    )
    action_type = "household.cancel_invitation"

    @staticmethod
    def _arguments(arguments: JsonObject) -> str:
        _at_most(arguments, _CANCEL_ARGUMENTS)
        return name_from(arguments.get("display_name"), "display_name")

    async def acts_on(self, arguments: JsonObject, ctx: ToolContext) -> str | None:
        """The invitation the call names (``ActsOn``): ``invitation:<id>`` of the one invitation
        that waits for the name, found by the name alone; ``None`` when there is none, or
        several, or when the tool does not accept the arguments."""
        try:
            name = self._arguments(arguments)
        except InvalidArguments:
            return None
        return await invitation_named(name, ctx)

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        name = self._arguments(arguments)
        whose.check(arguments, actor)
        whose.tells_the_owner(arguments, actor)
        async with ctx.session_factory() as session:
            invitation = await resolve_invitation(session, name, now=ctx.clock())
        theirs_to_change(invitation, actor)
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=f"invitation:{invitation.id}",
            effect="permissions",
            destination="household",
            facts={"role": invitation.role, GIVEN_BY_DOMO_FACT: invitation.by_domo},
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        name = self._arguments(arguments)
        by = whose.by(arguments, ctx)
        now = ctx.clock()
        async with ctx.session_factory() as session:
            invitation: Invitation = await resolve_invitation(session, name, now=now)
            described_target(ctx, f"invitation:{invitation.id}")
            theirs_to_change(invitation, ctx.acting)
            try:
                await cancel_invitation(session, invitation, now)
            except InvitationUnavailable:
                raise InvalidArguments(
                    "not_found", "no invitation that waits was made for this name"
                ) from None
            shown = invitation.display_name
            owner: UUID | None = None
            if whose.tells_the_owner(arguments, ctx.acting):
                # His own cancelling, told to the owner as he judged the owner would want.
                act = await note_act(
                    session,
                    act="invitation_cancelled",
                    by=by,
                    reason=whose.reason_of(arguments),
                    turn_of=whose.turn_of(arguments, ctx),
                    person_id=invitation.person_id,
                    invitation_id=invitation.id,
                    owed_to_owner=True,
                    now=now,
                )
                owner = await tell_owner(
                    session, act, knows=lambda one: whose.owner_knows(ctx, one), now=now
                )
            await audit_invitation_cancelled(session, invitation, by)
            await session.commit()
        if owner is not None and ctx.notify is not None:
            ctx.notify(owner)
        return ToolOutcome(
            status="done",
            model_result={"cancelled": shown},
            card={
                "key": "action.card.done.household.cancel_invitation",
                "params": {"name": shown},
            },
        )


_NO_INVITATION_WAITS: Final = (
    "no invitation that waits was made for this name: it was used, cancelled or has expired. "
    "To have them join, invite them again"
)
_A_RETURN_LINK: Final = (
    "this invitation is a link to come back: make it again by bringing the person back"
)


class InvitationLinkAgain:
    """Give an invitation that waits a new link (the old one stops working), shown once."""

    definition = ToolDefinition(
        name="invitation_link_again",
        description=(
            "Give again the link of an invitation that waits, when the person you are talking "
            "with asks for it (they lost it, it did not arrive), or your own idea: do it at once, "
            "without asking whether they are sure. A new link is made for the same invitation, "
            "which keeps its role, its stay and its end date; the old link stops working. The "
            "person is shown the new link once, to send it themselves: you never see it. If the "
            "result says the link waits for them, it waits until they have it shown: tell them. "
            "Name the invitation by the name it was made for. One you made may have its link "
            "given again by whoever holds that right; one a person made, only by them or whoever "
            "owns the household; your own idea may do it for any, and its new link then waits "
            "for whoever owns the household unless the person could invite themselves. "
            + whose.HIS_OWN_IDEA
        ),
        parameters={
            "type": "object",
            "properties": {
                "display_name": {
                    "type": "string",
                    "description": "The name the invitation was made for.",
                },
                "reason": whose.REASON_PROPERTY,
                "exception_of": whose.EXCEPTION_PROPERTY,
            },
            "required": ["display_name"],
            "additionalProperties": False,
        },
    )
    action_type = "household.invite_again"

    @staticmethod
    def _arguments(arguments: JsonObject) -> str:
        _at_most(arguments, _LINK_AGAIN_ARGUMENTS)
        return name_from(arguments.get("display_name"), "display_name")

    @staticmethod
    async def _waiting(session: AsyncSession, name: str, now: datetime) -> Invitation:
        """The joining invitation that waits for ``name``: ``not_found`` (with what to do) when
        there is none, and a return link is made again by bringing the person back."""
        try:
            invitation = await resolve_invitation(session, name, now=now)
        except InvalidArguments as refused:
            if refused.code == "not_found":
                raise InvalidArguments("not_found", _NO_INVITATION_WAITS) from None
            raise
        if invitation.kind == "return":
            raise InvalidArguments("invalid", _A_RETURN_LINK)
        return invitation

    async def acts_on(self, arguments: JsonObject, ctx: ToolContext) -> str | None:
        """The invitation the call names (``ActsOn``), as ``household_cancel_invitation`` finds
        it."""
        try:
            name = self._arguments(arguments)
        except InvalidArguments:
            return None
        return await invitation_named(name, ctx)

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        name = self._arguments(arguments)
        whose.check(arguments, actor)
        async with ctx.session_factory() as session:
            invitation = await self._waiting(session, name, ctx.clock())
        theirs_to_change(invitation, actor)
        role = role_from(invitation.role)
        # The facts of inviting (``policy.catalog.RIGHTS_OF``): a right on inviting someone with
        # this role, for these days, reads them the same way. A new link never sends the
        # invitation to the owner first again.
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=f"invitation:{invitation.id}",
            effect="permissions",
            destination="household",
            facts={
                "role": role,
                # Resolved when it was made: none, for a guest, is a stay with no end.
                "days": invitation.guest_days,
                "no_end": role == "guest" and invitation.guest_days is None,
                "display_name": invitation.display_name,
                "owner_first": False,
                "exception_of": whose.exception_of(arguments, actor),
                # The link of an invitation that waits is never the speaker's own record.
                "serves_the_speaker": False,
                # Whether the invitation is the assistant's (``theirs_to_change``).
                GIVEN_BY_DOMO_FACT: invitation.by_domo,
            },
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        name = self._arguments(arguments)
        his = whose.is_his_own(arguments, ctx)
        by = whose.by(arguments, ctx)
        now = ctx.clock()
        async with ctx.session_factory() as session:
            invitation = await self._waiting(session, name, now)
            described_target(ctx, f"invitation:{invitation.id}")
            theirs_to_change(invitation, ctx.acting)
            # His own new link is for the person of the turn only when they could invite
            # themselves, else for the owner (``whose_link``), as his invitation's is. Someone
            # else said yes, or nobody can be shown the link now: the old link dies, and the new
            # one waits for its holder, behind the button of their card (as an invitation does
            # after someone else's yes).
            asked = invitation_action(
                "household.invite_again",
                role_from(invitation.role),
                invitation.guest_days,
                invitation.guest_days is None,
                invitation.display_name,
            )
            holder, shows = await whose_link(session, ctx, his, asked)
            later = not shows
            # An invitation of the assistant's always says whom its link is for.
            shown_to = holder if his or invitation.created_by is None else None
            try:
                token = (
                    await give_link_again(session, invitation.id, now, waits_for=holder)
                    if later
                    else await give_link_again(session, invitation.id, now, shown_to=shown_to)
                )
            except InvitationUnavailable:
                raise InvalidArguments("not_found", _NO_INVITATION_WAITS) from None
            shown = invitation.display_name
            expires = invitation.expires_at
            invitation_id = invitation.id
            owner: UUID | None = None
            # The trace of the new link, whoever gives it: the link dates from now
            # (``household.service.nobody_new_since``). One of his own idea is told to the owner
            # once, as his invitation is.
            act = await note_act(
                session,
                act="invited",
                by=by,
                reason=whose.reason_of(arguments),
                turn_of=whose.turn_of(arguments, ctx),
                person_id=invitation.person_id,
                invitation_id=invitation.id,
                owed_to_owner=his,
                now=now,
            )
            if his:
                owner = await tell_owner(
                    session, act, knows=lambda one: whose.owner_knows(ctx, one), now=now
                )
                await directives.note_exceptions(
                    session,
                    whose.exception_of(arguments, ctx.acting),
                    person_id=invitation.person_id,
                    invitation_id=invitation.id,
                    turn_of=whose.turn_of(arguments, ctx),
                    now=now,
                )
            # Last, just before the commit. Never the token.
            await append_audit(
                session,
                actor=by,
                action="household.link_given_again",
                details={
                    "invitation_id": str(invitation.id),
                    "name": shown,
                    "role": invitation.role,
                },
            )
            await session.commit()
        if owner is not None and ctx.notify is not None:
            # The owner is owed the telling: a turn for him is due.
            ctx.notify(owner)
        dates = {"expires": card_date(expires)}
        if token is None and holder != ctx.person.id:
            return ToolOutcome(
                status="done",
                model_result={"invitation": shown, "link": "waits_for_the_owner"},
                card={
                    "key": "action.card.done.household.invite_again",
                    "params": {"name": shown},
                    "dates": dates,
                },
            )
        if token is None:
            return ToolOutcome(
                status="done",
                model_result={"invitation": shown, "link": "waits_for_them"},
                card={
                    "key": "action.card.done.household.invite_again.on_request",
                    "params": {"name": shown},
                    "dates": dates,
                    "invitation": str(invitation_id),
                },
            )
        return ToolOutcome(
            status="done",
            model_result={"invitation": shown, "link_shown_to_them": True},
            card={
                "key": "action.card.done.household.invite_again",
                "params": {"name": shown},
                "dates": dates,
            },
            # Shown once, to the one who asked, in the answer to this request: never stored,
            # never told to the model.
            card_once={
                "key": "action.card.once.household.invite_again",
                "params": {"name": shown},
                "link": f"{ctx.origin.rstrip('/')}/invite/{token}",
            },
        )


_NO_LINK_WAITS_FOR_THEM: Final = (
    "no invitation that waits for its link has this name for this person: the link may have "
    "been shown already (give it again with invitation_link_again), or be someone else's"
)
_NOT_ON_YOUR_IDEA: Final = (
    "a link that waits is shown only when the person it waits for asks for it, in a turn that "
    "answers them"
)
_NOT_SHOWN_HERE: Final = (
    "nobody reads this turn, so nothing could be shown once here: they ask for it when they "
    "are there"
)


def _the_owner(ctx: ToolContext) -> bool:
    """Whether the person of the turn is the owner, asking in their own words (the assistant's
    own call never is)."""
    return ctx.acting.subject != "domo" and ctx.person.role == "owner"


def asked_by_them(ctx: ToolContext) -> bool:
    """``InvalidArguments`` (``not_possible``) unless the link that waits may be shown in this
    call: it is not the assistant's own idea, nor a turn nobody asked for (``acts_alone``), and
    the turn answers them on a channel that shows something once (``leave_on_their_card``). A
    step of a chain read on a page is not refused: ``True``, the link stays behind the button of
    the chain's card (each step has its own), as an invitation's does."""
    if ctx.acting.subject == "domo" or acts_alone(ctx):
        raise InvalidArguments("not_possible", _NOT_ON_YOUR_IDEA)
    if ctx.in_a_chain and not ctx.no_reader:
        return True
    if ctx.leave_on_their_card:
        raise InvalidArguments("not_possible", _NOT_SHOWN_HERE)
    return False


class InvitationLinkShow:
    """Show, once, the link of an invitation that waits for the person who asks (WEB-3, part b):
    what the "show the link" button of their card does, in words."""

    definition = ToolDefinition(
        name="invitation_link_show",
        description=(
            "Show the person you are talking with, once, the link of an invitation that waits for "
            "them (someone else said yes, or it was left for them), when they ask for it; whoever "
            "owns the household may also see one that waits for someone else, which then no "
            "longer waits for them. Name the invitation by the name it was made for. You never "
            "see the link: they are shown it, to send it themselves. Never on your own idea."
        ),
        parameters={
            "type": "object",
            "properties": {
                "display_name": {
                    "type": "string",
                    "description": "The name the invitation was made for.",
                }
            },
            "required": ["display_name"],
            "additionalProperties": False,
        },
    )
    action_type = "household.show_link"

    @staticmethod
    def _arguments(arguments: JsonObject) -> str:
        _exactly(arguments, _SHOW_ARGUMENTS)
        return name_from(arguments["display_name"], "display_name")

    @staticmethod
    async def _theirs(session: AsyncSession, name: str, ctx: ToolContext) -> Invitation:
        """The open invitation of ``name`` whose link waits for the person of the turn
        (``link_holder``), or, for the owner, for anyone (the owner's answer of 2026-11-27, "Il te
        le montre"); ``not_found`` otherwise: nobody else learns of an invitation that is not
        theirs."""
        try:
            invitation = await resolve_invitation(session, name, now=ctx.clock())
        except InvalidArguments:
            raise InvalidArguments("not_found", _NO_LINK_WAITS_FOR_THEM) from None
        if link_holder(invitation) != ctx.person.id and not _the_owner(ctx):
            raise InvalidArguments("not_found", _NO_LINK_WAITS_FOR_THEM)
        return invitation

    async def acts_on(self, arguments: JsonObject, ctx: ToolContext) -> str | None:
        """The invitation the call names (``ActsOn``)."""
        try:
            name = self._arguments(arguments)
        except InvalidArguments:
            return None
        return await invitation_named(name, ctx)

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        name = self._arguments(arguments)
        async with ctx.session_factory() as session:
            invitation = await self._theirs(session, name, ctx)
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=f"invitation:{invitation.id}",
            effect="read",
            destination="household",
            facts={},
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        name = self._arguments(arguments)
        on_the_card = asked_by_them(ctx)
        now = ctx.clock()
        async with ctx.session_factory() as session:
            invitation = await self._theirs(session, name, ctx)
            described_target(ctx, f"invitation:{invitation.id}")
            shown, kind = invitation.display_name, invitation.kind
            holder = link_holder(invitation)
            once = "bring_back" if kind == "return" else "invite"
            if on_the_card and holder == ctx.person.id:
                # A step of a chain: the link waits behind the button of its card, for them.
                return ToolOutcome(
                    status="done",
                    model_result={"invitation": shown, "link": "waits_for_them"},
                    card={
                        "key": f"action.card.done.household.{once}.on_request",
                        "params": {"name": shown},
                        "dates": {"expires": card_date(invitation.expires_at)},
                        "invitation": str(invitation.id),
                    },
                )
            if on_the_card:
                # A link that waits for someone else is shown to the owner alone, in words.
                raise InvalidArguments("not_possible", _NOT_SHOWN_HERE)
            # The owner sees a link that waits for someone else: it no longer waits for them.
            someone_else = None if holder == ctx.person.id else holder
            try:
                token = await reveal_link(
                    session,
                    invitation.id,
                    ctx.person.id,
                    now,
                    by_owner=someone_else is not None or holder is None,
                )
            except InvitationUnavailable:
                raise InvalidArguments("not_found", _NO_LINK_WAITS_FOR_THEM) from None
            details = {"invitation_id": str(invitation.id)}
            model_result: JsonObject = {"invitation": shown, "link_shown_to_them": True}
            if someone_else is not None:
                details["waited_for"] = str(someone_else)
                # The one it waited for has news of it (``conversation.visits.what_is_new``).
                await news_for(session, someone_else, now=now)
                other = await session.get(Person, someone_else)
                model_result["waited_for"] = None if other is None else other.display_name
            # Last, just before the commit. The invitation, never the link.
            await append_audit(
                session,
                actor=f"person:{ctx.person.id}",
                action=LINK_SHOWN,
                details=details,
            )
            await session.commit()
        if someone_else is not None and ctx.notify is not None:
            ctx.notify(someone_else)
        return ToolOutcome(
            status="done",
            model_result=model_result,
            card={"key": "action.card.done.household.show_link", "params": {"name": shown}},
            # Shown once, to the one it waits for, in the answer to this request: never stored,
            # never told to the model.
            card_once={
                "key": f"action.card.once.household.{once}",
                "params": {"name": shown},
                "link": f"{ctx.origin.rstrip('/')}/invite/{token}",
            },
        )


class HouseholdTransferOwnership:
    """Hand the owner's role to an adult of the household (the owner becomes an adult)."""

    definition = ToolDefinition(
        name="household_transfer_ownership",
        description=(
            "Hand over their role, the role of owner, to an adult of the household, when that is "
            "what they want. Name the adult as the household knows them. They become an adult and "
            "keep their account, their conversation and what they wrote; the new owner does not "
            "have to agree. There is no way back, except the new owner handing the role over "
            "again."
        ),
        parameters={
            "type": "object",
            "properties": {
                "person": {
                    "type": "string",
                    "description": "The name of the adult who gets the role, in the household.",
                }
            },
            "required": ["person"],
            "additionalProperties": False,
        },
    )
    action_type = "household.transfer_ownership"
    # The argument that names someone of the household: given its whole name before the call
    # is described (``tools.context.NamesPeople``, spec 6.7).
    person_arguments: Final = ("person",)

    @staticmethod
    def _arguments(arguments: JsonObject) -> str:
        _exactly(arguments, _TRANSFER_ARGUMENTS)
        return name_from(arguments["person"], "person")

    @staticmethod
    def fixed_refusal(actor: Actor, facts: Mapping[str, FactValue], present: Present) -> str | None:
        """What no right moves (``RefusesWhateverTheRights``): only the owner of now hands the
        role over (``requester_role`` is the role the database holds for the one who asks, never
        the role the caller believes), never to oneself, and only to an adult who is in the
        household (not a child, a guest, the owner, nor someone who left)."""
        if facts.get("requester_role") != "owner":
            return _ONLY_THE_OWNER
        receiver = facts.get("to")
        if receiver == f"person:{actor.person_id}":
            return _NOT_TO_ONESELF
        if not isinstance(receiver, str) or present.get(receiver) != "adult":
            return _TO_AN_ADULT
        return None

    @staticmethod
    def _facts(requester: Person, receiver: Person) -> dict[str, FactValue]:
        """The facts of handing the role from ``requester`` to ``receiver``: the people, and the
        roles the database holds for them now."""
        return {
            "from": f"person:{requester.id}",
            "to": f"person:{receiver.id}",
            "requester_role": requester.role,
            "to_role": receiver.role,
        }

    def _checked(self, requester: Person, receiver: Person, actor: Actor) -> None:
        """``InvalidArguments`` unless ``fixed_refusal`` lets the hand-over through."""
        refused = self.fixed_refusal(actor, self._facts(requester, receiver), present_as(receiver))
        if refused is not None:
            raise InvalidArguments("invalid", refused)

    async def acts_on(self, arguments: JsonObject, ctx: ToolContext) -> str | None:
        """Whom the call names (``ActsOn``), found by the name alone; nobody when the tool does
        not accept its arguments."""
        try:
            name = self._arguments(arguments)
        except InvalidArguments:
            return None
        return await person_named(name, ctx)

    async def will_touch(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> frozenset[str]:
        """Whom the call will touch (``WillTouch``): the one who gives (``actor``) and, if the
        name means one person, the one who receives; nobody when the tool does not accept its
        arguments."""
        try:
            name = self._arguments(arguments)
        except InvalidArguments:
            return frozenset()
        touched = {f"person:{actor.person_id}"}
        receiver = await person_named(name, ctx)
        if receiver is not None:
            touched.add(receiver)
        return frozenset(touched)

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        name = self._arguments(arguments)
        async with ctx.session_factory() as session:
            receiver = await resolve_person(session, name, now=ctx.clock(), speaker=ctx.person.id)
            # The role of now, read again: ``actor`` may carry the role of an earlier moment.
            found = await session.execute(select(Person).where(Person.id == actor.person_id))
            requester = found.scalar_one()
        if actor.role == "owner":
            # Whoever else asks is refused by the barrier (``not_allowed``): the owner's alone.
            self._checked(requester, receiver, actor)
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=f"person:{receiver.id}",
            effect="permissions",
            destination="household",
            facts=self._facts(requester, receiver),
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        name = self._arguments(arguments)
        now = ctx.clock()
        async with ctx.session_factory() as session:
            receiver = await _someone(session, name, ctx, now)
            locked = await session.execute(
                select(Person)
                .where(Person.id == ctx.actor.person_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            requester = locked.scalar_one()
            self._checked(requester, receiver, ctx.actor)
            shown = receiver.display_name
            try:
                done = await transfer_ownership(
                    session, old_owner=requester, new_owner=receiver, now=now
                )
            except ValueError as refused:
                # (The service says what it refuses in its own words, never a value.)
                raise InvalidArguments("invalid", str(refused)) from None
            await session.commit()
        return ToolOutcome(
            status="done",
            model_result={
                "new_owner": shown,
                "rerouted": done.rerouted,
                "paused_rights": done.paused,
            },
            card={
                "key": "action.card.done.household.transfer_ownership",
                "params": {"name": shown},
            },
        )
