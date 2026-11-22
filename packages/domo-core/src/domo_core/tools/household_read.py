"""``household_list``: who is in the household, as the model may know it.

Each person is ``{"name", "role", "status"}``, in the order they arrived: ``active``, or
``guest_until:<date>`` for a guest (the day, in UTC, their stay ends), then each invitation that
is still waiting for an answer, ``invited``. People who were removed, guests whose stay is over and
invitations that were accepted, cancelled or have expired are left out.

To those who may remove people (``household.remove`` is allowed for them: the owner by default),
each person also has ``"last_seen"``: the day (``YYYY-MM-DD``, in UTC) of the most recent visit of
any of their sessions, or ``null`` when they have none (spec 6): Domo can notice who has not come
for a long time, and offer to remove them (the removal is still confirmed, as always). Anyone else
is not told when the others come.

Nothing else about anyone is given: no username, no identifier, no link.
"""

from datetime import UTC, date
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Account, Person, WebSession
from domo_core.household.models import Invitation
from domo_core.llm.types import JsonObject, ToolDefinition
from domo_core.policy.actions import DescribedAction
from domo_core.policy.rights import load_rights
from domo_core.policy.roles import Actor
from domo_core.tools.context import InvalidArguments, ToolContext, ToolOutcome


class HouseholdList:
    """Read who is in the household (and who is invited to join it)."""

    definition = ToolDefinition(
        name="household_list",
        description=(
            "See who is in the household: each person's name, role (owner, adult, child or "
            "guest) and status (active, guest until a date, or invited and not yet arrived) and, "
            "if the person you are talking with may remove people, the day each one last came "
            "(null if they never did)."
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
            persons = await session.execute(
                select(Person.id, Person.display_name, Person.role, Person.expires_at)
                .where(
                    Person.disabled_at.is_(None),
                    or_(Person.expires_at.is_(None), Person.expires_at > now),
                )
                .order_by(Person.created_at)
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
        for person_id, name, role, expires_at in persons.all():
            person: JsonObject = {
                "name": name,
                "role": role,
                "status": "active"
                if expires_at is None
                else f"guest_until:{expires_at.astimezone(UTC).date().isoformat()}",
            }
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
