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
facts, which a limit can read, are the person, the role they come back with, the days of a
guest's stay (``None`` for anyone else and for a stay with no end, ``no_end``) and whether their
access wakes at their next visit (``returns``): no length is refused by the code (audit POL-5).
When nothing is said, a guest with an account (a return link) stays
``household.service.GUEST_DAYS_DEFAULT`` days, one without an account has no end (the assistant
judges when they left). If the name they had is taken meanwhile (someone has it now, or a link
waits for it), the person chooses another with ``display_name``.

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

Someone with no account has no page to come back to: their access is given again at once
(``people.records.give_access``), with no link, and the role may be one made for the household.
The act may be the assistant's own (``people.whose``: the call carries ``reason`` exactly then),
and each leaves its trace with its reason (``access_given``, or ``invited`` for a return link).
Someone whose access the owner withdrew is named too (``resolve_former_person``, ``withdrawn``):
the owner's directive refuses anyone else before any judgement, and the owner's own order lifts it.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.accounts.sessions import is_active
from domo_core.audit.log import append_audit
from domo_core.household.service import (
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
from domo_core.people import directives, made_roles, records, whose
from domo_core.people.acts import note_act, tell_owner
from domo_core.people.models import MadeRole, PersonAct
from domo_core.policy.actions import DescribedAction, FactValue, Present
from domo_core.policy.rights import personal_right_summaries
from domo_core.policy.roles import Actor
from domo_core.text import MAX_DISPLAY_NAME_LENGTH
from domo_core.tools.context import InvalidArguments, ToolContext, ToolOutcome
from domo_core.tools.household_admin import (
    audit_invitation_cancelled,
    card_date,
    described_target,
    gives_a_member_role,
    guest_days_from,
    has_an_account,
    made_role_name,
    name_from,
    no_end_from,
    not_possible,
    present_as,
    role_from,
    role_named,
    stay_days,
    stay_given,
    whose_link,
)
from domo_core.tools.profile import display_name_value

_ARGUMENTS: Final = frozenset(
    {"person", "role", "guest_days", "no_end", "display_name", "left_on", "reason", "exception_of"}
)
_DAY: Final = re.compile(r"\d{4}-\d{2}-\d{2}")

_NAME_TAKEN: Final = (
    "someone in the household, or an invitation that waits, already has this name: call again "
    "with display_name, a name that tells them apart"
)
_STILL_HERE: Final = "this person is still in the household"
_UNDER_A_DIRECTIVE: Final = (
    "the owner withdrew or refused this person's access: only the owner gives it back"
)


@dataclass(frozen=True)
class _Call:
    """What a ``household_bring_back`` call says: who (``name``, and ``left_on`` to tell two of
    the same name apart), the role they come back with (``None``: the one they had) and a new
    display name. The days of a guest's stay are read with the role (``_role_of``)."""

    name: str
    role: str | None
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
    if "person" not in keys or not keys <= _ARGUMENTS:
        raise InvalidArguments(
            "invalid",
            "the arguments are person and, if needed, role, guest_days or no_end (a guest "
            "only), display_name, left_on and reason",
        )
    role = arguments.get("role")
    if role is not None and not isinstance(role, str):
        raise InvalidArguments("invalid", "role is a name, as a text")
    new_name = arguments.get("display_name")
    left_on = arguments.get("left_on")
    return _Call(
        name=name_from(arguments["person"], "person"),
        role=role,
        new_name=None if new_name is None else display_name_value(new_name),
        left_on=None if left_on is None else _day_from(left_on),
    )


async def _role_of(
    session: AsyncSession, person: Person, call: _Call, arguments: JsonObject
) -> tuple[MemberRole, MadeRole | None, int | None]:
    """The role ``person`` comes back with: the call's (a built-in role, or a role made for the
    household, by its name), else the one they had (their made role with it, while it is in
    use); and the days of a guest's stay the call gives."""
    if call.role is not None:
        role, made = await role_named(session, call.role)
    else:
        role = role_from(person.role)
        made_name = await made_role_name(session, person)
        made = None if made_name is None else await made_roles.named(session, made_name)
    return role, made, guest_days_from(arguments, role)


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
    """Bring someone who left the household back: at once when they have no account, else through
    a link the one who asked is shown once."""

    definition = ToolDefinition(
        name="household_bring_back",
        description=(
            "Bring back someone who left the household, when that is what the person you are "
            "talking with wants or your own idea: a person who was removed, or a guest whose "
            "stay is over. Name them as the household knew them. Someone with no account "
            "has their access again at once. Someone with an account keeps their username and "
            "everything they wrote; they choose a new password, and nothing of their old access "
            "works until they do. Give the role they come back with (adult, child, guest, or a "
            "role made for the household, by its name; left out, "
            "the one they had) and, for a guest only, how many days they may stay once back, "
            "or no_end true for a stay with no end: any length may be given, when the person "
            f"asks for it or it is your judgement. If nothing is said, {GUEST_DAYS_DEFAULT} days "
            "for someone with an account, no end for someone without one (you judge when they "
            "left, and end the stay with record_complete). Give "
            "display_name only if their old name is taken now. If two people who left have this "
            "name, you are told each one's role and the day they left: ask which one, then call "
            "again with left_on, that day written YYYY-MM-DD. The result says whether it was "
            "done, waits for a yes, or was not done. For someone with an account, when it is "
            "done, the person is shown "
            "a link to send them, which works once, for 7 days (if someone else said yes, it "
            "waits until the person has it shown): you never see it, and the person sends it "
            "themselves. A link that already waits for them is replaced by the new one in the "
            "same operation, with the same one yes: the old one stops working (when the person "
            "lost it, say). " + whose.HIS_OWN_IDEA
        ),
        parameters={
            "type": "object",
            "properties": {
                "person": {
                    "type": "string",
                    "description": "The name they had in the household.",
                },
                "role": {
                    "type": "string",
                    "description": (
                        "Their role when they come back: adult, child, guest, or the name of a "
                        "role made for the household. Left out: the one they had."
                    ),
                },
                "guest_days": {
                    "type": "integer",
                    "description": "For a guest only: how many days they may stay once back.",
                    "minimum": 1,
                },
                "no_end": {
                    "type": "boolean",
                    "description": "For a guest only: true for a stay with no end (not with "
                    "guest_days).",
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
                "reason": whose.REASON_PROPERTY,
                "exception_of": whose.EXCEPTION_PROPERTY,
            },
            "required": ["person"],
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

    async def _checked(
        self,
        session: AsyncSession,
        person: Person,
        call: _Call,
        arguments: JsonObject,
        actor: Actor,
        now: datetime,
        speaker: UUID,
    ) -> tuple[dict[str, FactValue], MadeRole | None]:
        """The facts of bringing ``person`` back, and the made role they come back with, once
        ``fixed_refusal`` lets them through (``not_found`` otherwise: they are not someone who
        left). Someone with an account comes back with a built-in role (the link carries it)."""
        role, made, days = await _role_of(session, person, call, arguments)
        no_end = no_end_from(arguments)
        has_account = await has_an_account(session, person)
        if has_account and made is None:
            # The link carries a built-in role; the made role they carry comes back with it while
            # it exists and has that base (``made_roles.kept_at_return``). A made role the call
            # names is carried by the record from now, and applies once they use the link.
            made = await made_roles.kept_at_return(session, person, role)
        # A return link resolves its days as an invitation does; an access given again at once
        # gives the stay ``stay_given`` says (no end when nothing is said).
        lasts = (
            stay_days(role, days, no_end=no_end)
            if has_account
            else stay_given(role, days, no_end, person=person, has_account=False, now=now)[1]
        )
        facts: dict[str, FactValue] = {
            "person": f"person:{person.id}",
            "role": role,
            "days": lasts,
            "no_end": role == "guest" and lasts is None,
            "returns": records.returns_now(person, now),
            "made_role": None if made is None else made.name,
            "has_account": has_account,
            "state": person.state,
            "exception_of": whose.exception_of(arguments, actor),
            # The record that comes back is the person whose turn it is.
            "serves_the_speaker": person.id == speaker,
        }
        present = present_as(person) if is_active(person, now) else {}
        refused = self.fixed_refusal(actor, facts, present)
        if refused is not None:
            raise InvalidArguments("not_found", refused)
        return facts, made

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        call = _call_from(arguments)
        whose.check(arguments, actor)
        async with ctx.session_factory() as session:
            person = await resolve_former_person(
                session, call.name, now=ctx.clock(), left_on=call.left_on, withdrawn=True
            )
            await _the_name_they_come_back_with(session, person, call, ctx)
            facts, _ = await self._checked(
                session, person, call, arguments, actor, ctx.clock(), ctx.person.id
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
        call = _call_from(arguments)
        by = whose.by(arguments, ctx)
        by_owner = whose.by_the_owner(arguments, ctx)
        now = ctx.clock()
        async with ctx.session_factory() as session:
            # Until the commit: a rename or an invitation cannot take the name between this check
            # and the insert.
            await lock_display_names(session)
            person = await resolve_former_person(
                session, call.name, now=now, left_on=call.left_on, withdrawn=True
            )
            described_target(ctx, f"person:{person.id}")
            directive = await directives.under_directive(session, person.id, now)
            if directive is not None and not by_owner:
                # The barrier refused it already, unless the directive came meanwhile.
                raise InvalidArguments("not_possible", _UNDER_A_DIRECTIVE)
            if not await has_an_account(session, person):
                return await self._at_once(session, person, call, arguments, ctx, by, by_owner)
            if directive is not None:
                # The owner's own order gives back what the owner withdrew: the directive is
                # lifted, and the link brings them back.
                await directives.lift_all_on(session, person.id, owner=ctx.person, now=now)
            # The link that waits is replaced: cancelled here, before the person is locked, as
            # ``accept_return`` locks the link before the person; one that ended meanwhile (it was
            # used, say) leaves nothing to replace, and the checks below say if the person is back.
            replaced = await open_return_invitation_of(session, person.id, now)
            if replaced is not None:
                try:
                    await cancel_invitation(session, replaced, now)
                except InvitationUnavailable:
                    replaced = None
            person = await self._locked(session, person)
            facts, named = await self._checked(
                session, person, call, arguments, ctx.acting, now, ctx.person.id
            )
            role = role_from(facts["role"])
            if named is not None and person.made_role_id != named.id:
                # A made role the call names: the record carries it from now (its access sleeps
                # until the link is used), and it applies once they are back.
                await records.leave_role(session, person, now=now)
                person.made_role_id = named.id
                await session.flush()
            name = await _the_name_they_come_back_with(session, person, call, ctx)
            # The inviter of an act of his is the assistant, never the person of the turn (S-K):
            # his link is for the person only when they could invite themselves, else the owner.
            his = whose.is_his_own(arguments, ctx)
            # Their right read on this very return, as the barrier would read their own call.
            asked = DescribedAction(
                connector="core",
                action_type=self.action_type,
                target=f"person:{person.id}",
                effect="permissions",
                destination="household",
                facts={**facts, "exception_of": None},
            )
            holder, shows = await whose_link(session, ctx, his, asked)
            invitation, token = await create_return_invitation(
                session,
                person=person,
                role=role,
                guest_days=guest_days_from(arguments, role),
                no_end=no_end_from(arguments),
                display_name=name,
                created_by=None if his else ctx.person.id,
                now=now,
                with_token=shows,
                link_for=holder if his else None,
                reason=whose.reason_of(arguments),
            )
            # The trace of the act, with its reason when it is the assistant's own; a bring-back
            # of his own idea is told to the owner once, as an invitation of his is.
            act = await note_act(
                session,
                act="invited",
                by=by,
                reason=whose.reason_of(arguments),
                turn_of=whose.turn_of(arguments, ctx),
                person_id=person.id,
                invitation_id=invitation.id,
                owed_to_owner=his,
                now=now,
            )
            owner = (
                await tell_owner(
                    session, act, knows=lambda one: whose.owner_knows(ctx, one), now=now
                )
                if his
                else None
            )
            await directives.note_exceptions(
                session,
                whose.exception_of(arguments, ctx.acting),
                person_id=person.id,
                invitation_id=invitation.id,
                turn_of=whose.turn_of(arguments, ctx),
                now=now,
            )
            details: dict[str, str | int] = {
                "invitation_id": str(invitation.id),
                "person_id": str(person.id),
                "role": role,
            }
            model_result: JsonObject = {"brought_back": name, "role": role}
            carried = await made_role_name(session, person)
            kept = await made_roles.kept_at_return(session, person, role)
            if kept is not None:
                # The assistant's removal kept it for a return: it applies again once they use
                # the link.
                model_result["made_role"] = kept.name
            elif carried is not None:
                # It no longer exists, or its base is not the link's role: they leave it.
                model_result["made_role_not_given_back"] = carried
            if replaced is not None:
                model_result["replaced_link"] = True
            if invitation.guest_days is not None:
                details["guest_days"] = invitation.guest_days
                model_result["guest_days"] = invitation.guest_days
            elif role == "guest":
                model_result["no_end"] = True
            if token is None and holder != ctx.person.id:
                model_result["link"] = "waits_for_the_owner"
            elif token is None:
                model_result["link"] = "waits_for_them"
            else:
                model_result["link_shown_to_owner"] = True
            rights = await self._paused(session, person, ctx, model_result)
            # Last, just before the commit: the old link cancelled, then the new one offered.
            # Never the token.
            if replaced is not None:
                await audit_invitation_cancelled(session, replaced, by)
            await append_audit(
                session,
                actor=by,
                action="household.bring_back_offered",
                details=details,
            )
            await session.commit()
        if owner is not None and ctx.notify is not None:
            # The owner is owed the telling: a turn for him is due.
            ctx.notify(owner)
        params = {"name": name}
        dates = {"expires": card_date(invitation.expires_at)}
        also = ".replaced" if replaced is not None else ""
        if token is None and holder != ctx.person.id:
            # His own link, not for the person of the turn: it waits for the owner.
            return ToolOutcome(
                status="done",
                model_result=model_result,
                card={
                    "key": f"action.card.done.household.bring_back{also}",
                    "params": params,
                    "dates": dates,
                    **rights,
                },
            )
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

    @staticmethod
    async def _locked(session: AsyncSession, person: Person) -> Person:
        locked = await session.execute(
            select(Person)
            .where(Person.id == person.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return locked.scalar_one()

    @staticmethod
    async def _paused(
        session: AsyncSession, person: Person, ctx: ToolContext, model_result: JsonObject
    ) -> JsonObject:
        """The rights paused when they left (spec 5.7), and what is still in force when they come
        back (spec 5.12): the owner says which to give back. The card says which, whoever asked
        (Ruling R26): the owner reads it, whether they asked or said yes to someone else's
        request (a yes by the button never sends the model's result back to Domo). Domo is told
        them when the owner asked, to remind them."""
        paused = await personal_right_summaries(session, person.id, with_paused=True)
        if not paused:
            return {}
        if ctx.person.role == "owner":
            model_result["paused_on_return"] = paused
        return {"rights": {"key": "action.card.rights.paused", "items": paused}}

    async def _at_once(
        self,
        session: AsyncSession,
        person: Person,
        call: _Call,
        arguments: JsonObject,
        ctx: ToolContext,
        by: str,
        by_owner: bool,
    ) -> ToolOutcome:
        """Someone with no account has their access again at once (no link: they have no page to
        come back to), with the role of the call (a made role too) and their name, or the new one
        when theirs is taken. The owner's own order gives back what the owner withdrew."""
        now = ctx.clock()
        person = await self._locked(session, person)
        facts, made = await self._checked(
            session, person, call, arguments, ctx.acting, now, ctx.person.id
        )
        role = role_from(facts["role"])
        name = await _the_name_they_come_back_with(session, person, call, ctx)
        person.display_name = name
        no_end = no_end_from(arguments)
        days = guest_days_from(arguments, role)
        given, lasts = stay_given(role, days, no_end, person=person, has_account=False, now=now)
        proposes = await records.may_propose_telling(session, person, by=by)
        try:
            await records.give_access(
                session,
                person,
                role=role,
                made_role=made,
                standing=None,
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
        await directives.note_exceptions(
            session,
            whose.exception_of(arguments, ctx.acting),
            person_id=person.id,
            turn_of=whose.turn_of(arguments, ctx),
            now=now,
        )
        model_result: JsonObject = {
            "brought_back": name,
            "role": role if made is None else made.name,
            "access_given": True,
        }
        if proposes:
            model_result["may_propose_telling"] = True
        if facts["returns"] is True:
            model_result["returns"] = True
        if person.expires_at is not None and lasts is not None:
            model_result["guest_days"] = lasts
        elif role == "guest":
            model_result["no_end"] = True
        rights = await self._paused(session, person, ctx, model_result)
        owner = await records.owner_to_tell(session, person, now)
        if whose.is_his_own(arguments, ctx):
            # A bring-back of his own idea is told to the owner once, as an invitation of his is.
            given = await session.scalar(
                select(PersonAct)
                .where(
                    PersonAct.act == "access_given",
                    PersonAct.person_id == person.id,
                    PersonAct.at == now,
                )
                .order_by(PersonAct.id.desc())
                .limit(1)
            )
            if given is not None and given.told_owner_at is None:
                given.owed_to_owner = True
                owner = await tell_owner(
                    session, given, knows=lambda one: whose.owner_knows(ctx, one), now=now
                )
        await session.commit()
        if owner is not None and ctx.notify is not None:
            # The owner asked to be told of each access given: a turn for them is due.
            ctx.notify(owner)
        return ToolOutcome(
            status="done",
            model_result=model_result,
            card={
                "key": "action.card.done.household.bring_back.at_once",
                "params": {"name": name},
                **rights,
            },
        )
