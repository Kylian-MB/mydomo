"""A request that waits is no longer left to lapse unseen (spec 11.2 of "Domo judges first"): a pass
of the core's upkeep (``expiring_pass``, run every round) does two things.

* It wakes the assistant once, shortly before a waiting request lapses (``about_to_expire``: it
  has waited into the last ``wake_lead`` of the owner's lifetime). He is woken in the conversation
  of the one who asked (``conversation.starts.wake_before_expiry``: where the request was first
  made, never someone else's) and judges whether to remind whoever must answer, tell the one who
  asked, drop it, or do nothing. That it woke him is written first (``expiry_woken_at``,
  committed), so that no second wake follows, whatever the wake meets.
* It writes the lapse when it happens (``lapse``): a request past its lifetime expires now, by the
  same expiry as the one that happens when someone opens it (``service.expire_past_lifetime``), so
  one fact is noted (``went_wrong``, kind ``expired``, dated at the lapse) and one entry audited,
  whichever comes first, and a request already expired is never expired again.

A chain paused until someone's arrival never lapses (``store.is_past_lifetime``): it lives while
its invitation can be accepted. A pass that fails for one request is logged, by its kind, and goes
on with the others; the next pass tries again.
"""

import logging
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.accounts.sessions import active_at
from domo_core.confirmations.models import Confirmation
from domo_core.confirmations.service import expire_past_lifetime
from domo_core.conversation.prepare import TurnDeps
from domo_core.conversation.starts import Wakes, wake_before_expiry
from domo_core.policy.settings import PolicySettings, load_policy_settings
from domo_core.upkeep import UpkeepPass

logger = logging.getLogger(__name__)

# How many requests one pass wakes him for at most: the others wait for the next round.
WAKES_PER_PASS: Final = 20
# The least he is woken before a lapse, unless the lifetime itself is shorter.
_LEAST_LEAD: Final = timedelta(hours=1)


def wake_lead(lifetime: timedelta) -> timedelta:
    """How long before a request lapses the assistant is woken for it: a fifth of its lifetime, but
    never under an hour, and never more than the lifetime itself."""
    return min(lifetime, max(lifetime / 5, _LEAST_LEAD))


async def about_to_expire(
    session: AsyncSession,
    settings: PolicySettings,
    now: datetime,
    *,
    limit: int = WAKES_PER_PASS,
) -> list[Confirmation]:
    """The requests that wait for an answer and have waited into the last ``wake_lead`` of the
    owner's lifetime at ``now`` without lapsing, and that he was not woken for yet, oldest first,
    ``limit`` at most; only those of someone still in the household. Reads only."""
    lifetime = settings.max_lifetime
    found = await session.scalars(
        select(Confirmation)
        .join(Person, Person.id == Confirmation.requester_id)
        .where(
            Confirmation.status == "pending",
            Confirmation.expiry_woken_at.is_(None),
            # created_at + lifetime - lead <= now < created_at + lifetime
            Confirmation.created_at <= now - lifetime + wake_lead(lifetime),
            Confirmation.created_at > now - lifetime,
            active_at(now),
        )
        .order_by(Confirmation.created_at, Confirmation.id)
        .limit(limit)
        .execution_options(populate_existing=True)
    )
    return list(found.all())


async def lapse(session: AsyncSession, settings: PolicySettings, now: datetime) -> int:
    """Expire, now, the requests that wait and are past their lifetime at ``now``
    (``service.expire_past_lifetime``: the fact is noted and the expiry audited, once). Each is
    expired in a savepoint of its own: one that cannot be is logged, by its kind, and left for the
    next pass. How many were expired. Flushed, not committed."""
    due = await session.scalars(
        select(Confirmation.id)
        .where(
            Confirmation.status == "pending",
            Confirmation.created_at <= now - settings.max_lifetime,
        )
        .order_by(Confirmation.created_at, Confirmation.id)
    )
    expired = 0
    for confirmation_id in list(due.all()):
        try:
            async with session.begin_nested():
                if await expire_past_lifetime(session, confirmation_id, settings, now):
                    expired += 1
        except Exception as error:
            logger.error(
                "A request past its lifetime could not be expired (%s)", type(error).__name__
            )
    return expired


async def _claim_wake(session: AsyncSession, confirmation_id: uuid.UUID, now: datetime) -> bool:
    """Write that he is woken for the request ``confirmation_id`` (``expiry_woken_at``), if it still
    waits and he was not: whether this call did it, so that two passes never both wake him.
    Flushed, not committed."""
    claimed = await session.execute(
        update(Confirmation)
        .where(
            Confirmation.id == confirmation_id,
            Confirmation.status == "pending",
            Confirmation.expiry_woken_at.is_(None),
        )
        .values(expiry_woken_at=now)
        .returning(Confirmation.id)
    )
    return claimed.first() is not None


def expiring_pass(deps_factory: Callable[[], TurnDeps], wakes: Wakes) -> UpkeepPass:
    """The pass of the upkeep that writes the lapses (``lapse``) and wakes the assistant once for
    each request about to lapse (``about_to_expire``), with what the application gives now
    (``deps_factory``, read at each pass). The wake is written first, committed
    (``expiry_woken_at``), then the turn starts in a task of ``wakes``
    (``conversation.starts.wake_before_expiry``). Never raises: what goes wrong is logged, by its
    kind, and the next pass tries again."""

    async def expiring(now: datetime) -> None:
        deps = deps_factory()
        try:
            async with deps.session_factory() as session:
                settings = await load_policy_settings(session)
                lapsed = await lapse(session, settings, now)
                await session.commit()
                due = [c.id for c in await about_to_expire(session, settings, now)]
            if lapsed:
                logger.info("%d request(s) lapsed", lapsed)
        except Exception as error:
            logger.error("The requests about to lapse could not be read (%s)", type(error).__name__)
            return
        for confirmation_id in due:
            try:
                async with deps.session_factory() as session:
                    claimed = await _claim_wake(session, confirmation_id, now)
                    await session.commit()
                if claimed:
                    wakes.spawn(wake_before_expiry(deps, confirmation_id))
            except Exception as error:
                logger.error(
                    "A request about to lapse could not be woken for (%s)", type(error).__name__
                )

    return expiring
