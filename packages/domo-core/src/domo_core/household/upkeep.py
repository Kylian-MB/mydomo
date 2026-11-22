"""Pausing the rights of the people who left the household: a pass of the core's periodic upkeep.

Someone who leaves the household has the rights the owner gave them themselves paused, not lost:
the owner decides what to give back if they return (spec 5.7). A person who is removed has them
paused at once (``household.service.disable``). A guest whose stay ends is not removed by
anybody: they are gone the moment the stay is no longer in the future
(``accounts.sessions.active_at``), and nothing happens to their rights then. And someone removed
before the removal paused rights still has theirs in force. This pass does both: it pauses, once
and audited, the rights in force of everyone who left (``pause_rights_of_those_who_left``: removed,
or stay ended; never the owner, who never leaves), so that a guest who leaves between two passes
has their rights paused within a minute. If the owner brings them back first,
``household.service.accept_return`` pauses what is still in force.

``pause_rights_of_those_who_left`` writes one audit entry ``rights.paused`` per person who had some
(by the system: the person's identifier and how many rights, as for a removal or a return), and
commits nothing, so that the entries and the pauses end together. ``departures_pass`` is the pass
of the core's upkeep (``upkeep.run_upkeep``) that does it in a session of its own and commits it,
when the core starts and every minute after (``web.app``).
"""

from datetime import datetime

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domo_core.accounts.models import Person
from domo_core.accounts.sessions import active_at
from domo_core.devices.service import forget_devices_of_those_who_left
from domo_core.policy.models import PolicyRight
from domo_core.policy.rights import pause_rights_and_audit
from domo_core.upkeep import UpkeepPass


async def pause_rights_of_those_who_left(session: AsyncSession, now: datetime) -> int:
    """Pause the rights that are still in force of everyone who left the household by ``now``
    (removed, or a guest whose stay ended; not the owner), and return how many rights were paused:
    one audit entry ``rights.paused`` per person, by the system
    (``policy.rights.pause_rights_and_audit``). Someone whose rights are paused already, or who has
    none, is left alone: a second pass finds nothing to do. Nothing is committed: the caller owns
    the transaction (and it must be a read committed one, as for any audit entry)."""
    in_force = exists().where(
        PolicyRight.person_id == Person.id,
        PolicyRight.revoked_at.is_(None),
        PolicyRight.paused_at.is_(None),
    )
    departed = await session.execute(
        select(Person.id)
        .where(~active_at(now), Person.role != "owner", in_force)
        .order_by(Person.created_at, Person.id)
    )
    paused = 0
    for person_id in departed.scalars().all():
        paused += await pause_rights_and_audit(session, person_id, now, actor="system")
    return paused


def departures_pass(session_factory: async_sessionmaker[AsyncSession]) -> UpkeepPass:
    """The pass of the upkeep that pauses the rights of the people who left
    (``pause_rights_of_those_who_left``, at the time of the pass) and forgets their devices
    (``devices.service.forget_devices_of_those_who_left``), in a session of its own, committed.
    The tools Domo made stay: they are his."""

    async def pause(now: datetime) -> None:
        async with session_factory() as session:
            await pause_rights_of_those_who_left(session, now)
            await forget_devices_of_those_who_left(session, now)
            await session.commit()

    return pause
