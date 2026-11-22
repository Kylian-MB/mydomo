"""The tools of whoever has the right (the owner by default) to change who is in the household
(decisions D1 and D3).

Each of them is judged (the yes-judge says whether a yes is asked first, and which).

* ``household_invite``: invite someone, with a name, a role (adult, child or
  guest) and, for a guest, the days they may stay (2 by default; the household's rule is
  ``GUEST_DAYS_RULE`` days at most, a limit of the catalog the owner may replace, and the tool
  accepts up to the technical ceiling, ``GUEST_DAYS_CEILING``). Once the yes is given, the
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
  those keys, or takes them off the general key); left out, the household's default decides;
* ``household_change_role``: give someone another role. Not the owner's own. Someone made a guest
  stays a number of days, as an invited guest does (2 by default, R78, and the same rule of
  ``GUEST_DAYS_RULE`` days at most); someone who stops being a guest stays for good. The rights the
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
* ``household_transfer_ownership`` (judged like any action: nothing is asked by default):
  hand the owner's role to an adult of the household. The
  owner becomes an adult and keeps their account; the new owner does not have to agree. It is the
  owner's alone, whatever the rights say, and so are its refusals (``fixed_refusal``): the one who
  asks is the owner of now (the role is read again from the database, never taken from the
  caller), not oneself, and the receiver an adult who is in the household. What it moves, and its
  audit entry (``household.ownership_transferred``), are in ``household.service``.

Bringing someone who left back (``household_bring_back``) is in ``tools.household_return``, which
shares the helpers below that are not private.

Who may use them is the catalog's business (``policy.catalog``: the owner only, unless the owner
has given the right to someone else; inviting, giving a link again and cancelling an invitation,
the adults too; leaving, everyone). Nobody ever changes their own role, though, and nobody acts on
the owner: those are in the code of the tools, whatever the rights say. So is whose invitation it
is: anyone but the owner gives the link again of, or cancels, only an invitation they made
(``theirs_to_change``, the owner's trial of 0.9.1). An invitation may say that Domo judged the
owner should say yes first (``owner_first``, a fact of the action): the catalog's limit of inviting
then sends it to the owner.
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
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.accounts.sessions import is_active
from domo_core.audit.log import append_audit
from domo_core.household.models import Invitation
from domo_core.household.service import (
    GUEST_DAYS_CEILING,
    GUEST_DAYS_DEFAULT,
    MEMBER_ROLES,
    InvitationUnavailable,
    MemberRole,
    cancel_invitation,
    create_invitation,
    disable,
    give_link_again,
    lock_display_names,
    name_in_use,
    resolve_invitation,
    resolve_person,
    set_role,
    transfer_ownership,
)
from domo_core.llm.types import JsonObject, ToolDefinition
from domo_core.policy.actions import DescribedAction, FactValue, Present
from domo_core.policy.catalog import GUEST_DAYS_RULE
from domo_core.policy.rights import personal_right_summaries
from domo_core.policy.roles import Actor, Role, as_role
from domo_core.text import MAX_DISPLAY_NAME_LENGTH
from domo_core.tools.context import InvalidArguments, ToolContext, ToolOutcome
from domo_core.tools.profile import display_name_value

logger = logging.getLogger(__name__)

_INVITE_ARGUMENTS: Final = frozenset(
    {"display_name", "role", "guest_days", "uses_my_key", "owner_first"}
)
_CHANGE_ROLE_ARGUMENTS: Final = frozenset({"person", "role", "guest_days"})
_REMOVE_ARGUMENTS: Final = frozenset({"person"})
_CANCEL_ARGUMENTS: Final = frozenset({"display_name"})
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
_STAY_NEEDS_DAYS: Final = "the person is already a guest: give guest_days to change their stay"
_OWNER_NEVER_LEAVES: Final = "the owner never leaves: they hand their role over first"


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


def name_from(value: object, what: str) -> str:
    """A name the call gives to find someone (or an invitation): a text that is not blank."""
    if not isinstance(value, str) or not value.strip():
        raise InvalidArguments("invalid", f"{what} is a name, as a text")
    return value


def _exactly(arguments: JsonObject, expected: frozenset[str]) -> None:
    if frozenset(arguments) != expected:
        raise InvalidArguments("invalid", f"the arguments are {', '.join(sorted(expected))}")


def _invitation_from(arguments: JsonObject) -> tuple[str, MemberRole, int | None]:
    """The name, role and days of a ``household_invite`` call (days only for a guest, ``None``
    when not given). ``InvalidArguments`` for anything else."""
    keys = frozenset(arguments)
    if not {"display_name", "role"} <= keys or not keys <= _INVITE_ARGUMENTS:
        raise InvalidArguments(
            "invalid",
            "the arguments are display_name, role, uses_my_key, owner_first and, for a guest "
            "only, guest_days",
        )
    name = display_name_value(arguments["display_name"])
    role = role_from(arguments["role"])
    return name, role, guest_days_from(arguments, role)


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


def guest_days_from(arguments: JsonObject, role: MemberRole) -> int | None:
    """The ``guest_days`` of a call, ``None`` when not given: a whole number of days from 1 to
    ``GUEST_DAYS_CEILING``, and only for a guest (``InvalidArguments`` otherwise). The household's
    rule (``GUEST_DAYS_RULE`` days at most, unless the owner chose otherwise) is a limit the
    barrier applies."""
    days: object = arguments.get("guest_days")
    if days is None:
        return None
    if role != "guest":
        raise InvalidArguments("invalid", "guest_days is only for a guest")
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= GUEST_DAYS_CEILING:
        raise InvalidArguments(
            "invalid", f"guest_days is a whole number of days, from 1 to {GUEST_DAYS_CEILING}"
        )
    return days


def stay_days(role: MemberRole, days: int | None) -> int | None:
    """The days a person stays with ``role``, for the facts of an action: those the call gave, the
    default for a guest it gave none to, ``None`` for anyone else (they stay for good)."""
    if role != "guest":
        return None
    return GUEST_DAYS_DEFAULT if days is None else days


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


async def person_named(name: str, ctx: ToolContext) -> str | None:
    """Whom a call names (``ActsOn``): ``person:<id>`` of the one person of the household ``name``
    means now, with no check of what would happen to them; ``None`` when it means nobody or
    several."""

    async def find(session: AsyncSession) -> str:
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
    session: AsyncSession, invitation: Invitation, by: Person
) -> None:
    """The audit entry of an invitation cancelled by ``by``: its identifier, the name it was made
    for and its role, never its link. Cancelling and this entry are one transaction (and the entry
    is written last, just before the commit)."""
    await append_audit(
        session,
        actor=f"person:{by.id}",
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
    ``invitation`` again or cancel it: the owner, any; anyone else, only an invitation they made
    themselves (``created_by``), whatever the rights (the owner's trial of 0.9.1)."""
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
            "with wants: the name the household will call them, their role (adult, child or "
            "guest) and, for a guest only, how many days they may stay once they have joined "
            f"({GUEST_DAYS_DEFAULT} if the person does not say; the household's rule is "
            f"{GUEST_DAYS_RULE} days at most, unless the owner's rules say otherwise). With "
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
            "a request that waits shows your choices. The result "
            "says whether it was done, waits for a yes, or was not done. When it is done, the "
            "person is shown a link to send them, which works once, for 7 days (if someone else "
            "said yes, it waits until the person has it shown): you never see it, and the person "
            "sends it themselves."
        ),
        parameters={
            "type": "object",
            "properties": {
                "display_name": {
                    "type": "string",
                    "description": "The name the household will call them.",
                    "minLength": 1,
                    "maxLength": MAX_DISPLAY_NAME_LENGTH,
                },
                "role": {**ROLE_SCHEMA, "description": "Their role in the household."},
                "guest_days": {
                    "type": "integer",
                    "description": "For a guest only: how many days they may stay.",
                    "minimum": 1,
                    "maximum": GUEST_DAYS_CEILING,
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
            },
            "required": ["display_name", "role"],
            "additionalProperties": False,
        },
    )
    action_type = "household.invite"

    async def acts_on(self, arguments: JsonObject, ctx: ToolContext) -> str | None:
        """The invitation that holds the name the call asks for (``ActsOn``): ``invitation:<id>``
        of the one open invitation made for it, found by the name alone; ``None`` when the name
        is free or means a person, or when the tool does not accept the arguments. A chain of
        actions uses it for "cancel the invitation of Dana, then invite Dana": the name is taken
        until the first step is done."""
        try:
            name, _, _ = _invitation_from(arguments)
        except InvalidArguments:
            return None
        return await invitation_named(name, ctx)

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        name, role, days = _invitation_from(arguments)
        _uses_my_key(arguments)
        owner_first = _owner_first(arguments)
        async with ctx.session_factory() as session:
            if await name_in_use(session, name, now=ctx.clock()):
                raise InvalidArguments("invalid", NAME_IN_USE)
        # The name is a fact (it goes to the audit of the decision, which holds names); the action
        # has no target, as the person does not exist yet.
        facts: dict[str, FactValue] = {
            "role": role,
            "days": stay_days(role, days),
            "display_name": name,
            "owner_first": owner_first,
        }
        refused = self.fixed_refusal(actor, facts, {})
        if refused is not None:
            raise InvalidArguments("invalid", refused)
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=None,
            effect="permissions",
            destination="household",
            facts=facts,
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        name, role, days = _invitation_from(arguments)
        uses_my_key = _uses_my_key(arguments)
        _owner_first(arguments)
        now = ctx.clock()
        async with ctx.session_factory() as session:
            # Until the commit: a rename cannot take the name between this check and the insert.
            await lock_display_names(session)
            if await name_in_use(session, name, now=now):
                raise InvalidArguments("invalid", NAME_IN_USE)
            invitation, token = await create_invitation(
                session,
                display_name=name,
                role=role,
                guest_days=days,
                created_by=ctx.person.id,
                now=now,
                with_token=not ctx.leave_on_their_card,
                uses_inviter_key=uses_my_key,
            )
            details: dict[str, str | int | bool] = {
                "invitation_id": str(invitation.id),
                "name": name,
                "role": role,
            }
            if invitation.guest_days is not None:
                details["guest_days"] = invitation.guest_days
            if uses_my_key is not None:
                details["uses_inviter_key"] = uses_my_key
            # Last, just before the commit. Never the token.
            await append_audit(
                session,
                actor=f"person:{ctx.person.id}",
                action="household.invited",
                details=details,
            )
            await session.commit()
        params = {"name": name}
        dates = {"expires": card_date(invitation.expires_at)}
        if token is None:
            # Someone else said yes, or no page reads the turn: the link is for the one who asked,
            # who makes it with the button of their card (the invitation's identifier is no
            # secret).
            return ToolOutcome(
                status="done",
                model_result={"invited": name, "role": role, "link": "waits_for_them"},
                card={
                    "key": "action.card.done.household.invite.on_request",
                    "params": params,
                    "dates": dates,
                    "invitation": str(invitation.id),
                },
            )
        link = f"{ctx.origin.rstrip('/')}/invite/{token}"
        return ToolOutcome(
            status="done",
            model_result={"invited": name, "role": role, "link_shown_to_owner": True},
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
    """Give someone of the household another role."""

    definition = ToolDefinition(
        name="household_change_role",
        description=(
            "Give someone in the household another role (adult, child or guest), when that is "
            "what the person you are talking with wants. Name them as the household knows them. "
            "Nobody is made owner here, and whoever is owner keeps that role (it is only handed "
            "over). Someone made a guest may stay a number of days, from now "
            f"({GUEST_DAYS_DEFAULT} if the person does not say; the household's rule is "
            f"{GUEST_DAYS_RULE} days at most, unless the owner's rules say otherwise), then they "
            "lose access; someone who stops being a guest stays for good. Someone who is already a "
            "guest may be given a new stay (guest_days from now, longer or shorter): say so as a "
            "change of stay. The result says whether it was done, waits for a yes, or was not done."
        ),
        parameters={
            "type": "object",
            "properties": {
                "person": {"type": "string", "description": "Their name in the household."},
                "role": {**ROLE_SCHEMA, "description": "Their new role."},
                "guest_days": {
                    "type": "integer",
                    "description": "For a guest only: how many days they may stay, from now.",
                    "minimum": 1,
                    "maximum": GUEST_DAYS_CEILING,
                },
            },
            "required": ["person", "role"],
            "additionalProperties": False,
        },
    )
    action_type = "household.change_role"
    # The argument that names someone of the household: given its whole name before the call
    # is described (``tools.context.NamesPeople``, spec 6.7).
    person_arguments: Final = ("person",)

    @staticmethod
    def _arguments(arguments: JsonObject) -> tuple[str, MemberRole, int | None]:
        keys = frozenset(arguments)
        if not {"person", "role"} <= keys or not keys <= _CHANGE_ROLE_ARGUMENTS:
            raise InvalidArguments(
                "invalid", "the arguments are person, role and, for a guest only, guest_days"
            )
        role = role_from(arguments["role"])
        name = name_from(arguments["person"], "person")
        return name, role, guest_days_from(arguments, role)

    @staticmethod
    def fixed_refusal(actor: Actor, facts: Mapping[str, FactValue], present: Present) -> str | None:
        """What no right moves (``RefusesWhateverTheRights``): the owner's role never changes,
        nobody changes their own role (it would be giving oneself more power), the owner's role is
        never given, and a person is not given the role they have, unless they are a guest given a
        new stay (spec 5.5: a change of stay, the days of its facts counted from now)."""
        if facts.get("current_role") == "owner":
            return "the owner's role never changes"
        if facts.get("person") == f"person:{actor.person_id}":
            return "nobody changes their own role"
        given = gives_a_member_role(facts)
        if given is not None:
            return given
        new_stay = facts.get("role") == "guest" and facts.get("days") is not None
        if facts.get("role") == facts.get("current_role") and not new_stay:
            return "the person already has this role"
        return None

    def _checked(
        self, person: Person, role: MemberRole, days: int | None, actor: Actor
    ) -> dict[str, FactValue]:
        """The facts of changing ``person``'s role, once ``fixed_refusal`` lets them through. A
        guest who stays a guest is given a new stay: the call says how long (``guest_days``), or
        there is nothing to change (the facts always hold a guest's days, the default when the
        call gives none, so this is read from the call)."""
        facts: dict[str, FactValue] = {
            "person": f"person:{person.id}",
            "current_role": person.role,
            "role": role,
            "days": stay_days(role, days),
        }
        refused = self.fixed_refusal(actor, facts, present_as(person))
        if refused is not None:
            raise InvalidArguments("invalid", refused)
        if role == "guest" and person.role == "guest" and days is None:
            raise InvalidArguments("invalid", _STAY_NEEDS_DAYS)
        return facts

    async def acts_on(self, arguments: JsonObject, ctx: ToolContext) -> str | None:
        """Whom the call names (``ActsOn``), found by the name alone; nobody when the tool does
        not accept its arguments."""
        try:
            name, _, _ = self._arguments(arguments)
        except InvalidArguments:
            return None
        return await person_named(name, ctx)

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        name, role, days = self._arguments(arguments)
        async with ctx.session_factory() as session:
            person = await resolve_person(session, name, now=ctx.clock(), speaker=ctx.person.id)
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=f"person:{person.id}",
            effect="permissions",
            destination="household",
            facts=self._checked(person, role, days, actor),
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        name, role, days = self._arguments(arguments)
        now = ctx.clock()
        async with ctx.session_factory() as session:
            person = await _someone(session, name, ctx, now)
            self._checked(person, role, days, ctx.actor)
            old_role, shown = person.role, person.display_name
            end = await set_role(session, person, role, now=now, guest_days=days)
            # The days of their stay, when they have one now (a guest's).
            stay = GUEST_DAYS_DEFAULT if days is None else days
            model_result: JsonObject = {"person": shown, "role": role}
            params = {"name": shown}
            dates: dict[str, str] = {}
            if end is not None:
                model_result["guest_days"] = stay
                dates["until"] = card_date(end)
            if old_role == role:
                # A guest who stays a guest has a new stay (spec 5.5): a change of their end, not
                # of their role (``_checked`` lets nothing else through), so the rights they keep
                # are no news.
                model_result["stay_changed"] = True
                await append_audit(
                    session,
                    actor=f"person:{ctx.person.id}",
                    action="household.stay_changed",
                    details={"person_id": str(person.id), "name": shown, "guest_days": stay},
                )
                await session.commit()
                return ToolOutcome(
                    status="done",
                    model_result=model_result,
                    card={
                        "key": "action.card.done.household.change_role.stay",
                        "params": params,
                        "dates": dates,
                    },
                )
            details: dict[str, str | int] = {
                "person_id": str(person.id),
                "name": shown,
                "old_role": old_role,
                "new_role": role,
            }
            if end is not None:
                details["guest_days"] = stay
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
            await append_audit(
                session,
                actor=f"person:{ctx.person.id}",
                action="household.role_changed",
                details=details,
            )
            await session.commit()
        return ToolOutcome(
            status="done",
            model_result=model_result,
            card={
                "key": f"action.card.done.household.change_role.{role}",
                "params": params,
                "dates": dates,
                **rights,
            },
        )


class HouseholdRemove:
    """Remove someone from the household (their account is closed, their data kept)."""

    definition = ToolDefinition(
        name="household_remove",
        description=(
            "Remove someone from the household, when the person you are talking with asks: their "
            "account is closed at once, and what they wrote is kept. The rights given to them in "
            "person are paused (paused_rights says how many): if they return, giving them back is "
            "for the owner only. Name them as the household knows them. It may be the person "
            "themselves, who then leaves; never anyone whose role is owner. The result says "
            "whether it was done, waits for a yes, or was not done."
        ),
        parameters={
            "type": "object",
            "properties": {
                "person": {"type": "string", "description": "Their name in the household."}
            },
            "required": ["person"],
            "additionalProperties": False,
        },
    )
    action_type = "household.remove"
    # The argument that names someone of the household: given its whole name before the call
    # is described (``tools.context.NamesPeople``, spec 6.7).
    person_arguments: Final = ("person",)

    @staticmethod
    def _arguments(arguments: JsonObject) -> str:
        _exactly(arguments, _REMOVE_ARGUMENTS)
        return name_from(arguments["person"], "person")

    @staticmethod
    def fixed_refusal(actor: Actor, facts: Mapping[str, FactValue], present: Present) -> str | None:
        """What no right moves (``RefusesWhateverTheRights``): the owner is never removed.
        Oneself is fine: removing oneself gives no power, and the person can be brought back."""
        if facts.get("current_role") == "owner":
            return "the owner is never removed"
        return None

    def _checked(self, person: Person, actor: Actor) -> dict[str, FactValue]:
        """The facts of removing ``person``, once ``fixed_refusal`` lets them through."""
        facts: dict[str, FactValue] = {
            "person": f"person:{person.id}",
            "current_role": person.role,
            "is_self": person.id == actor.person_id,
        }
        refused = self.fixed_refusal(actor, facts, present_as(person))
        if refused is not None:
            raise InvalidArguments("invalid", refused)
        return facts

    async def acts_on(self, arguments: JsonObject, ctx: ToolContext) -> str | None:
        """Whom the call names (``ActsOn``), found by the name alone; nobody when the tool does
        not accept its arguments."""
        try:
            name = self._arguments(arguments)
        except InvalidArguments:
            return None
        return await person_named(name, ctx)

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        name = self._arguments(arguments)
        async with ctx.session_factory() as session:
            person = await resolve_person(session, name, now=ctx.clock(), speaker=ctx.person.id)
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=f"person:{person.id}",
            effect="permissions",
            destination="household",
            facts=self._checked(person, actor),
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        name = self._arguments(arguments)
        now = ctx.clock()
        async with ctx.session_factory() as session:
            person = await _someone(session, name, ctx, now)
            self._checked(person, ctx.actor)
            shown, role = person.display_name, person.role
            # Read before they are paused: the card says which rights wait for the owner, whoever
            # asked (Ruling R26), as at a return.
            waiting = await personal_right_summaries(session, person.id)
            removed = await disable(session, person, now, by=ctx.person.id)
            await append_audit(
                session,
                actor=f"person:{ctx.person.id}",
                action="household.removed",
                details={
                    "person_id": str(person.id),
                    "name": shown,
                    "role": role,
                    "requests_refused": removed.requests_refused,
                },
            )
            await session.commit()
        rights: JsonObject = {}
        if waiting:
            rights["rights"] = {"key": "action.card.rights.paused", "items": waiting}
        return ToolOutcome(
            status="done",
            model_result={"removed": shown, "paused_rights": removed.rights_paused},
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
            # They leave themselves: they are the one who does it (``rights.paused`` too).
            left = await disable(session, person, now, by=person.id)
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
            "are talking with wants: its link stops working. Name it by the name it was made for."
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
    action_type = "household.cancel_invitation"

    @staticmethod
    def _arguments(arguments: JsonObject) -> str:
        _exactly(arguments, _CANCEL_ARGUMENTS)
        return name_from(arguments["display_name"], "display_name")

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
        async with ctx.session_factory() as session:
            invitation = await resolve_invitation(session, name, now=ctx.clock())
        theirs_to_change(invitation, actor)
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=f"invitation:{invitation.id}",
            effect="permissions",
            destination="household",
            facts={"role": invitation.role},
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        name = self._arguments(arguments)
        now = ctx.clock()
        async with ctx.session_factory() as session:
            invitation: Invitation = await resolve_invitation(session, name, now=now)
            described_target(ctx, f"invitation:{invitation.id}")
            theirs_to_change(invitation, ctx.actor)
            try:
                await cancel_invitation(session, invitation, now)
            except InvitationUnavailable:
                raise InvalidArguments(
                    "not_found", "no invitation that waits was made for this name"
                ) from None
            shown = invitation.display_name
            await audit_invitation_cancelled(session, invitation, ctx.person)
            await session.commit()
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
            "with asks for it (they lost it, it did not arrive): do it at once, without asking "
            "whether they are sure. A new link is made for the same invitation, which keeps its "
            "role, its stay and its end date; the old link stops working. The person is shown "
            "the new link once, to send it themselves: you never see it. If the result says the "
            "link waits for them, it waits until they have it shown: tell them. Name the "
            "invitation by the name it was made for."
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
    action_type = "household.invite_again"

    @staticmethod
    def _arguments(arguments: JsonObject) -> str:
        _exactly(arguments, _CANCEL_ARGUMENTS)
        return name_from(arguments["display_name"], "display_name")

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
        if invitation.person_id is not None:
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
                "days": stay_days(role, invitation.guest_days),
                "display_name": invitation.display_name,
                "owner_first": False,
            },
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        name = self._arguments(arguments)
        now = ctx.clock()
        # Someone else said yes, or nobody can be shown the link now: the old link dies, and the
        # new one waits for the one who asked, behind the button of their card (as an invitation
        # does after someone else's yes).
        later = ctx.leave_on_their_card
        async with ctx.session_factory() as session:
            invitation = await self._waiting(session, name, now)
            described_target(ctx, f"invitation:{invitation.id}")
            theirs_to_change(invitation, ctx.actor)
            try:
                token = (
                    await give_link_again(session, invitation.id, now, waits_for=ctx.person.id)
                    if later
                    else await give_link_again(session, invitation.id, now)
                )
            except InvitationUnavailable:
                raise InvalidArguments("not_found", _NO_INVITATION_WAITS) from None
            shown = invitation.display_name
            expires = invitation.expires_at
            invitation_id = invitation.id
            # Last, just before the commit. Never the token.
            await append_audit(
                session,
                actor=f"person:{ctx.person.id}",
                action="household.link_given_again",
                details={
                    "invitation_id": str(invitation.id),
                    "name": shown,
                    "role": invitation.role,
                },
            )
            await session.commit()
        if token is None:
            return ToolOutcome(
                status="done",
                model_result={"invitation": shown, "link": "waits_for_them"},
                card={
                    "key": "action.card.done.household.invite_again.on_request",
                    "params": {"name": shown},
                    "dates": {"expires": card_date(expires)},
                    "invitation": str(invitation_id),
                },
            )
        return ToolOutcome(
            status="done",
            model_result={"invitation": shown, "link_shown_to_them": True},
            card={
                "key": "action.card.done.household.invite_again",
                "params": {"name": shown},
                "dates": {"expires": card_date(expires)},
            },
            # Shown once, to the one who asked, in the answer to this request: never stored,
            # never told to the model.
            card_once={
                "key": "action.card.once.household.invite_again",
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
