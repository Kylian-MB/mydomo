"""``household_list``: who is in the household, as the model may know it.

A person is a record, with or without a login (``people.records``): what is listed is who has an
access now, whether or not they have an account. Each person is ``{"name", "role", "status"}``, in
the order they arrived: ``active``, or ``guest_until:<date>`` when their stay has an end (the day,
in UTC, it ends), then each invitation that
is still waiting for an answer, ``invited``. People who were removed, guests whose stay is over and
invitations that were accepted, cancelled or have expired are left out.

To those who may remove people (``household.remove`` is allowed for them: the owner by default),
each person also has ``"last_seen"``: the day (``YYYY-MM-DD``, in UTC) of the most recent visit of
any of their sessions, or ``null`` when they have none (spec 6): the assistant can notice who has
not come for a long time, and offer to remove them, or remove one he gave access to (the judge
weighs each removal). Anyone else is not told when the others come.

To the household's owner when they ask, and to the assistant in a turn of his own
(``people.whose.may_see_every_record``), the list is
every record he may name, not only those who have access now: each one also says where its record
stands (``state``), where the person stands towards the house (``standing``), whether they have
an account (``has_account``), and, for a role the assistant made, that role's name. A record with
no access now has the status ``no_access``. Anyone else is given the people who have access, as
above, and nothing of the records. A list changes nothing, so it never takes what he marked
``act_on_my_own`` (``conversation.execution``): in someone else's turn it is judged as theirs and
the mark waits for the next call; his check for a duplicate there is ``record_look``, which takes
it.

Nothing else about anyone is given: no username, no identifier, no link.
"""

from datetime import UTC, date
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Account, Person, WebSession
from domo_core.accounts.sessions import active_at
from domo_core.household.models import Invitation
from domo_core.llm.types import JsonObject, ToolDefinition
from domo_core.people import whose
from domo_core.people.models import MadeRole
from domo_core.people.reach import has_account, known_records
from domo_core.policy.actions import DescribedAction
from domo_core.policy.rights import load_rights
from domo_core.policy.roles import Actor
from domo_core.tools.context import InvalidArguments, ToolContext, ToolOutcome
from domo_core.tools.people_tools import BETWEEN_PEOPLE


class HouseholdList:
    """Read who is in the household (and who is invited to join it)."""

    definition = ToolDefinition(
        name="household_list",
        description=(
            "See who is in the household: each person's name, role (owner, adult, child, guest, "
            "or for you and the household's owner a role made for the household) and status "
            "(active, guest until a date, or invited and not yet arrived) and, if the person you "
            "are talking with may remove people, the day each one last came (null if they never "
            "did). In a turn of your own, or at the request of the household's owner, you also see "
            "every record you may name (status no_access for those with none), with its state, "
            "where they stand and whether they have an account: look here before you open a "
            "record. In someone else's turn this list takes no mark and shows only who has "
            "access: check for a duplicate with record_look after act_on_my_own. What you see of "
            "those with no access is for you. " + BETWEEN_PEOPLE
        ),
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
    )
    action_type = "household.list"

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        if arguments:
            raise InvalidArguments("invalid", "this tool takes no arguments")
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=None,
            effect="read",
            destination="household",
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        if arguments:
            raise InvalidArguments("invalid", "this tool takes no arguments")
        now = ctx.clock()
        async with ctx.session_factory() as session:
            rights = await load_rights(session)
            may_remove = rights.resolve(ctx.actor, "household.remove", ctx.policy).allowed
            last_seen = await _last_visits(session) if may_remove else {}
            every = whose.may_see_every_record(ctx, ctx.acting)
            persons = await session.execute(
                select(
                    Person.id,
                    Person.display_name,
                    Person.role,
                    Person.expires_at,
                    Person.state,
                    Person.standing,
                    has_account(),
                    MadeRole.name,
                    active_at(now),
                )
                .outerjoin(MadeRole, MadeRole.id == Person.made_role_id)
                .where(known_records() if every else active_at(now))
                .order_by(Person.created_at, Person.id)
            )
            invitations = await session.execute(
                select(Invitation.display_name, Invitation.role)
                .where(
                    Invitation.accepted_at.is_(None),
                    Invitation.cancelled_at.is_(None),
                    Invitation.expires_at > now,
                )
                .order_by(Invitation.created_at)
            )
        people: list[JsonObject] = []
        for (
            person_id,
            name,
            role,
            expires_at,
            state,
            standing,
            account,
            made,
            in_house,
        ) in persons.all():
            person: JsonObject = {
                "name": name,
                "role": (made if every else None) or role,
                "status": "no_access"
                if not in_house
                else "active"
                if expires_at is None
                else f"guest_until:{expires_at.astimezone(UTC).date().isoformat()}",
            }
            if every:
                person["state"] = state
                person["standing"] = standing
                person["has_account"] = account
            if may_remove:
                visit = last_seen.get(person_id)
                person["last_seen"] = None if visit is None else visit.isoformat()
            people.append(person)
        people.extend(
            {"name": name, "role": role, "status": "invited"} for name, role in invitations.all()
        )
        return ToolOutcome(
            status="done",
            model_result={"people": people},
            card={"key": "action.card.done.household.list", "params": {}},
        )


async def _last_visits(session: AsyncSession) -> dict[UUID, date]:
    """The day (UTC) of the most recent visit of each person who has a session: the latest
    ``last_seen_at`` of any of their sessions, whether or not the session still works."""
    found = await session.execute(
        select(Account.person_id, func.max(WebSession.last_seen_at))
        .join(WebSession, WebSession.account_id == Account.id)
        .group_by(Account.person_id)
    )
    return {person_id: seen.astimezone(UTC).date() for person_id, seen in found.all()}
