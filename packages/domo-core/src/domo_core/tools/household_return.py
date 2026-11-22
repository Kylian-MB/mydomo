"""``household_bring_back``: the owner, or whoever has the right, brings someone who left the
household back (spec 6).

A person who left is one who was removed, or a guest whose stay is over. They keep their account
and everything they wrote, but their access is closed, and nothing can reopen it without a new
invitation to the household, which would make a new person and leave their username taken. Bringing
them back makes a *return link* instead: an invitation tied to that person (``household.service``,
``create_return_invitation``) with the role they come back with and, for a guest, how long they
stay. Nothing of the old access works before they use it; using it replaces their password and ends
what was left of their access (``accept_return``). Their username does not change.

Like ``household_invite`` it is judged, and once the yes is given the link is shown to
the one who asked, once: in the answer to the request that made it (``ToolOutcome.card_once``),
never in the stored card, the model's result, the history, the audit log or a log. When someone
else said yes, the link is the one who asked's, and when no page reads the turn (a wake of Domo
after a button), nobody would see it (``ToolContext.leave_on_their_card``): it is made without a
token, the card has the invitation's identifier, and the button on their card makes the token and
shows the link, once, to them alone (``household.service.reveal_link``); Domo is told the link
waits for them (``"link": "waits_for_them"``), never that it was shown.

The person is named by the name they had in the household, among the people who left only
(``household.service.resolve_former_person``): someone who is still in the household is
``not_found`` (and the detail says so), and a name that means several people who left is
``ambiguous``. Then Domo is told each one's role and the day they left, never an identifier or a
username; it asks the owner which one, and calls again with ``left_on`` (that day). A request is
described with the person the name meant then, as its target; when the request runs after a yes,
the name must still mean that one (``ToolContext.described_action``), or nothing is done. Its
facts, which a limit can read, are the person, the role they come back with and the days of a
guest's stay (``None`` for anyone else). If the name they had is taken meanwhile (someone has it
now, or a link waits for it), the person chooses another with ``display_name``.

The card says the rights the owner had given to the person themselves (``"rights"``, their
summaries), whoever asked (Ruling R26): they were paused when the person left (spec 5.7) and stay
so when the person comes back (spec 5.12; any still in force are paused then), and the owner reads
them whoever asked and whoever said yes. When the owner asks, Domo is told them too
(``paused_on_return``), and asks the owner which to give back.

The link and its audit entries: ``household.bring_back_offered`` when it is made, and
``household.brought_back`` when it is used (``household.routes``): identifiers, roles and days,
never the link. A person who has a link that waits already (the owner lost it, say) is given a new
one that replaces it (spec 5.8): the old one is cancelled in the same operation, with the audit
entry ``household.invitation_cancelled`` ahead of the new link's, Domo is told
(``replaced_link``) and the card says the old link no longer works. It is the one request, with
the one yes. The name they come back with is checked without the link it replaces, which holds
that very name.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.accounts.sessions import is_active
from domo_core.audit.log import append_audit
from domo_core.household.service import (
    GUEST_DAYS_CEILING,
    GUEST_DAYS_DEFAULT,
    InvitationUnavailable,
    MemberRole,
    cancel_invitation,
    create_return_invitation,
    lock_display_names,
    name_in_use,
    open_return_invitation_of,
    resolve_former_person,
)
from domo_core.llm.types import JsonObject, ToolDefinition
from domo_core.policy.actions import DescribedAction, FactValue, Present
from domo_core.policy.catalog import GUEST_DAYS_RULE
from domo_core.policy.rights import personal_right_summaries
from domo_core.policy.roles import Actor
from domo_core.text import MAX_DISPLAY_NAME_LENGTH
from domo_core.tools.context import InvalidArguments, ToolContext, ToolOutcome
from domo_core.tools.household_admin import (
    ROLE_SCHEMA,
    audit_invitation_cancelled,
    card_date,
    described_target,
    gives_a_member_role,
    guest_days_from,
    name_from,
    present_as,
    role_from,
    stay_days,
)
from domo_core.tools.profile import display_name_value

_ARGUMENTS: Final = frozenset({"person", "role", "guest_days", "display_name", "left_on"})
_DAY: Final = re.compile(r"\d{4}-\d{2}-\d{2}")

_NAME_TAKEN: Final = (
    "someone in the household, or an invitation that waits, already has this name: call again "
    "with display_name, a name that tells them apart"
)
_STILL_HERE: Final = "this person is still in the household"


@dataclass(frozen=True)
class _Call:
    """What a ``household_bring_back`` call says: who (``name``, and ``left_on`` to tell two of
    the same name apart), the role and days they come back with, and a new display name."""

    name: str
    role: MemberRole
    days: int | None
    new_name: str | None
    left_on: date | None


def _day_from(value: object) -> date:
    """A day written ``YYYY-MM-DD`` (only that: other ISO forms are refused)."""
    if not isinstance(value, str) or not _DAY.fullmatch(value):
        raise InvalidArguments("invalid", "left_on is a day, written YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise InvalidArguments("invalid", "left_on is a day, written YYYY-MM-DD") from None


def _call_from(arguments: JsonObject) -> _Call:
    """The call's arguments, checked: ``InvalidArguments`` for anything else. An optional argument
    that is ``null`` is not given."""
    keys = frozenset(arguments)
    if not {"person", "role"} <= keys or not keys <= _ARGUMENTS:
        raise InvalidArguments(
            "invalid",
            "the arguments are person, role and, if needed, guest_days (a guest only), "
            "display_name and left_on",
        )
    role = role_from(arguments["role"])
    new_name = arguments.get("display_name")
    left_on = arguments.get("left_on")
    return _Call(
        name=name_from(arguments["person"], "person"),
        role=role,
        days=guest_days_from(arguments, role),
        new_name=None if new_name is None else display_name_value(new_name),
        left_on=None if left_on is None else _day_from(left_on),
    )


async def _the_name_they_come_back_with(
    session: AsyncSession, person: Person, call: _Call, ctx: ToolContext
) -> str:
    """The display name the person will have, once nothing stops a link for them: the name is
    free. The return link that waits for them, which the new one replaces, does not count: it
    holds the name they come back with."""
    now = ctx.clock()
    waiting = await open_return_invitation_of(session, person.id, now)
    name = call.new_name or person.display_name
    if await name_in_use(
        session, name, now=now, excluding_invitation=None if waiting is None else waiting.id
    ):
        raise InvalidArguments("invalid", _NAME_TAKEN)
    return name


class HouseholdBringBack:
    """Bring someone who left the household back, through a link the one who asked is shown
    once."""

    definition = ToolDefinition(
        name="household_bring_back",
        description=(
            "Bring back someone who left the household, when that is what the person you are "
            "talking with wants: a person who was removed, or a guest whose stay is over. Name "
            "them as the household knew them. They keep their username and everything they "
            "wrote; they choose a new password, and nothing of their old access works until they "
            "do. Give the role they come back with (adult, child or guest) and, for a guest "
            f"only, how many days they may stay once back ({GUEST_DAYS_DEFAULT} if the person "
            f"does not say; the household's rule is {GUEST_DAYS_RULE} days at most, unless the "
            "owner's rules say otherwise). Give display_name only if their old name is taken now. "
            "If two people who left have this name, you are told each one's role and the day they "
            "left: ask which one, then call again with left_on, that day written YYYY-MM-DD. "
            "The result says whether it was done, waits for a yes, or was not done. When it is "
            "done, the person is shown a link to send them, which works once, for 7 days (if "
            "someone else said yes, it waits until the person has it shown): you never see it, "
            "and the person sends it themselves. A link that already waits for "
            "them is replaced by the new one in the same operation, with the same one yes: the "
            "old one stops working (when the person lost it, say)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "person": {
                    "type": "string",
                    "description": "The name they had in the household.",
                },
                "role": {**ROLE_SCHEMA, "description": "Their role when they come back."},
                "guest_days": {
                    "type": "integer",
                    "description": "For a guest only: how many days they may stay once back.",
                    "minimum": 1,
                    "maximum": GUEST_DAYS_CEILING,
                },
                "display_name": {
                    "type": "string",
                    "description": "Only if their old name is taken now: the name to call them.",
                    "minLength": 1,
                    "maxLength": MAX_DISPLAY_NAME_LENGTH,
                },
                "left_on": {
                    "type": "string",
                    "description": (
                        "Only to tell apart two people of the same name: the day this one left, "
                        "written YYYY-MM-DD."
                    ),
                },
            },
            "required": ["person", "role"],
            "additionalProperties": False,
        },
    )
    action_type = "household.bring_back"

    @staticmethod
    def fixed_refusal(actor: Actor, facts: Mapping[str, FactValue], present: Present) -> str | None:
        """What no right moves (``RefusesWhateverTheRights``): the owner's role is never given,
        and only someone who left comes back (someone in the household, the owner first of all,
        never does)."""
        given = gives_a_member_role(facts)
        if given is not None:
            return given
        if facts.get("person") in present:
            return _STILL_HERE
        return None

    def _checked(
        self, person: Person, call: _Call, actor: Actor, now: datetime
    ) -> dict[str, FactValue]:
        """The facts of bringing ``person`` back, once ``fixed_refusal`` lets them through
        (``not_found`` otherwise: they are not someone who left)."""
        facts: dict[str, FactValue] = {
            "person": f"person:{person.id}",
            "role": call.role,
            "days": stay_days(call.role, call.days),
        }
        present = present_as(person) if is_active(person, now) else {}
        refused = self.fixed_refusal(actor, facts, present)
        if refused is not None:
            raise InvalidArguments("not_found", refused)
        return facts

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        call = _call_from(arguments)
        async with ctx.session_factory() as session:
            person = await resolve_former_person(
                session, call.name, now=ctx.clock(), left_on=call.left_on
            )
            await _the_name_they_come_back_with(session, person, call, ctx)
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=f"person:{person.id}",
            effect="permissions",
            destination="household",
            facts=self._checked(person, call, actor, ctx.clock()),
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        call = _call_from(arguments)
        now = ctx.clock()
        async with ctx.session_factory() as session:
            # Until the commit: a rename or an invitation cannot take the name between this check
            # and the insert.
            await lock_display_names(session)
            person = await resolve_former_person(session, call.name, now=now, left_on=call.left_on)
            described_target(ctx, f"person:{person.id}")
            # The link that waits is replaced: cancelled here, before the person is locked, as
            # ``accept_return`` locks the link before the person; one that ended meanwhile (it was
            # used, say) leaves nothing to replace, and the checks below say if the person is back.
            replaced = await open_return_invitation_of(session, person.id, now)
            if replaced is not None:
                try:
                    await cancel_invitation(session, replaced, now)
                except InvitationUnavailable:
                    replaced = None
            locked = await session.execute(
                select(Person)
                .where(Person.id == person.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            person = locked.scalar_one()
            self._checked(person, call, ctx.actor, now)
            name = await _the_name_they_come_back_with(session, person, call, ctx)
            invitation, token = await create_return_invitation(
                session,
                person=person,
                role=call.role,
                guest_days=call.days,
                display_name=name,
                created_by=ctx.person.id,
                now=now,
                with_token=not ctx.leave_on_their_card,
            )
            details: dict[str, str | int] = {
                "invitation_id": str(invitation.id),
                "person_id": str(person.id),
                "role": call.role,
            }
            model_result: JsonObject = {"brought_back": name, "role": call.role}
            if replaced is not None:
                model_result["replaced_link"] = True
            if invitation.guest_days is not None:
                details["guest_days"] = invitation.guest_days
                model_result["guest_days"] = invitation.guest_days
            if token is None:
                model_result["link"] = "waits_for_them"
            else:
                model_result["link_shown_to_owner"] = True
            rights: JsonObject = {}
            # Paused when they left (spec 5.7), and what is still in force when they come back
            # (spec 5.12): the owner says which to give back. The card
            # says which, whoever asked (Ruling R26): the owner reads it, whether they asked or
            # said yes to someone else's request (a yes by the button never sends the model's
            # result back to Domo). Domo is told them when the owner asked, to remind them.
            paused = await personal_right_summaries(session, person.id, with_paused=True)
            if paused:
                rights["rights"] = {"key": "action.card.rights.paused", "items": paused}
                if ctx.person.role == "owner":
                    model_result["paused_on_return"] = paused
            # Last, just before the commit: the old link cancelled, then the new one offered.
            # Never the token.
            if replaced is not None:
                await audit_invitation_cancelled(session, replaced, ctx.person)
            await append_audit(
                session,
                actor=f"person:{ctx.person.id}",
                action="household.bring_back_offered",
                details=details,
            )
            await session.commit()
        params = {"name": name}
        dates = {"expires": card_date(invitation.expires_at)}
        also = ".replaced" if replaced is not None else ""
        if token is None:
            # Someone else said yes, or no page reads the turn: the link is for the one who asked,
            # who makes it with the button of their card (the invitation's identifier is no
            # secret).
            return ToolOutcome(
                status="done",
                model_result=model_result,
                card={
                    "key": f"action.card.done.household.bring_back.on_request{also}",
                    "params": params,
                    "dates": dates,
                    "invitation": str(invitation.id),
                    **rights,
                },
            )
        link = f"{ctx.origin.rstrip('/')}/invite/{token}"
        return ToolOutcome(
            status="done",
            model_result=model_result,
            card={
                "key": f"action.card.done.household.bring_back{also}",
                "params": params,
                "dates": dates,
                **rights,
            },
            # Shown once, to the one who asked, in the answer to this request: never stored,
            # never told to the model.
            card_once={
                "key": "action.card.once.household.bring_back",
                "params": {"name": name},
                "link": link,
            },
        )
