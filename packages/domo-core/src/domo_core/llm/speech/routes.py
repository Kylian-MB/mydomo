"""Where a task of sound goes: a key the person may use, and a model on it that does the job.

What is heard stays at home (``HOME_ONLY``: hearing, the voice print, attention and the sorting
of what was heard) unless the owner let it go out (``speech.hear_outside``). A phrase awaited as
a proof is always heard at home. Saying goes wherever a model that says is found. Where a key is
is the gatekeeper's word (``speech.at_home``), never a guess from the form of its address.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.llm import router
from domo_core.llm.catalog import Catalog
from domo_core.llm.models import Provider
from domo_core.llm.router import Route
from domo_core.llm.speech.at_home import HomeCheck
from domo_core.llm.tasks import HOME_ONLY, TaskKind
from domo_core.policy.settings import PolicySettings


class NoHomeModel(Exception):
    """No model at home can do the task. Callers read ``None`` from ``speech_route`` instead;
    this is for the one who wants to say it."""


async def _household_keys(session: AsyncSession) -> list[Provider]:
    rows = await session.execute(
        select(Provider).where(Provider.is_general).order_by(Provider.created_at, Provider.id)
    )
    return list(rows.scalars())


async def speech_route(
    session: AsyncSession,
    person: Person | None,
    task: TaskKind,
    *,
    catalog: Catalog,
    settings: PolicySettings,
    at_home: HomeCheck,
    for_a_proof: bool = False,
) -> Route | None:
    """The first route found for ``task`` among the keys ``person`` may use, in their order (the
    household's keys when ``person`` is ``None``), or ``None`` when nothing can do it. A task of
    ``HOME_ONLY`` only takes a key of the house (``at_home``, the gatekeeper's judgement of where
    its address leads), unless ``settings.hear_outside``; a phrase awaited as a proof
    (``for_a_proof``) is heard at home whatever the setting."""
    keys = (
        await _household_keys(session)
        if person is None
        else await router.usable_keys(session, person)
    )
    home_only = task in HOME_ONLY and (for_a_proof or not settings.hear_outside)
    for key in keys:
        if home_only and not await at_home(key):
            continue
        route = await router.choose_route(session, provider=key, task=task, catalog=catalog)
        if route is not None:
            return route
    return None
