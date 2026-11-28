"""Pausing the rights of the people who left the household: a pass of the core's periodic upkeep.

Someone who leaves the household has the rights the owner gave them themselves paused, not lost:
the owner decides what to give back if they return (spec 5.7). A person who is removed has them
paused at once (``household.service.disable``). A guest whose stay ends is not removed by
anybody: they are gone the moment the stay is no longer in the future
(``accounts.sessions.active_at``), and nothing happens to their rights then. And someone removed
before the removal paused rights still has theirs in force. This pass does both: it pauses, once
and audited, the rights in force of everyone who left (``pause_rights_of_those_who_left``: removed,
or stay ended; never the owner, who never leaves), so that a guest who leaves between two passes
has their rights paused within a minute. A stay that ended without an account (the assistant
judged that they left, ``people.records.complete``) is one like any other: the record stays,
whole, its access asleep. If the owner brings them back first,
``household.service.accept_return`` pauses what is still in force.

``pause_rights_of_those_who_left`` writes one audit entry ``rights.paused`` per person who had some
(by the system: the person's identifier and how many rights, as for a removal or a return), and
commits nothing, so that the entries and the pauses end together. ``departures_pass`` is the pass
of the core's upkeep (``upkeep.run_upkeep``) that does it in a session of its own and commits it,
when the core starts and every minute after (``web.app``).
"""

from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domo_core.accounts.models import Person
from domo_core.accounts.sessions import active_at
from domo_core.devices.service import forget_devices_of_those_who_left
from domo_core.people.directives import end_expired
from domo_core.people.forget import finish_the_forgotten
from domo_core.people.made_roles import UNUSED, delete, unused
from domo_core.people.merge import purge
from domo_core.policy.models import PolicyRight
from domo_core.policy.rights import pause_rights_and_audit
from domo_core.policy.roles import DOMO_LABEL
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
    (``devices.service.forget_devices_of_those_who_left``), and ends the owner's directives whose
    time limit is past (``people.directives.end_expired``), and finishes what a forgetting
    left for after its call (``people.forget.finish_the_forgotten``), in a session of its own,
    committed.
    The tools Domo made stay: they are his."""

    async def pause(now: datetime) -> None:
        async with session_factory() as session:
            await end_expired(session, now)
            await pause_rights_of_those_who_left(session, now)
            await forget_devices_of_those_who_left(session, now)
            await finish_the_forgotten(session, now)
            await session.commit()

    return pause


# How long a role just made and not yet given is left alone before it is cleaned up.
UNUSED_ROLE_GRACE: Final = timedelta(days=7)


def unused_roles_pass(session_factory: async_sessionmaker[AsyncSession]) -> UpkeepPass:
    """The pass of the upkeep that deletes the roles made for the household that nobody carries
    and no waiting promise names, once they are ``UNUSED_ROLE_GRACE`` old
    (``people.made_roles.unused``), by the assistant, with the reason ``unused``, in a session of
    its own, committed. A role nobody carries any more is deleted at once, where its last bearer
    leaves it (``people.records.leave_role``): this one is for a role never given."""

    async def clean_up(now: datetime) -> None:
        async with session_factory() as session:
            for role in await unused(session, before=now - UNUSED_ROLE_GRACE):
                await delete(session, role, by=DOMO_LABEL, reason=UNUSED, turn_of=None, now=now)
            await session.commit()

    return clean_up


def merge_traces_pass(session_factory: async_sessionmaker[AsyncSession]) -> UpkeepPass:
    """The pass of the upkeep that empties the traces of the merges past their
    ``people.merge.MERGE_KEPT`` (``people.merge.purge``: the absorbed record of a merge never
    split is emptied as a forgotten one), in a session of its own, committed."""

    async def purge_the_traces(now: datetime) -> None:
        async with session_factory() as session:
            await purge(session, now)
            await session.commit()

    return purge_the_traces
