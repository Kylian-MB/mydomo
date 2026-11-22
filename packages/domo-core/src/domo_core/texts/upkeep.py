"""The upkeep of the texts: the house's version kept whole (spec 4.1; 0.7.1 spec 5.1).

A house whose language has default phrases of its own (English, French: ``meanings.has_defaults``)
reads them, and the upkeep writes nothing for it. For a house in another language,
``text_gaps_pass`` is the pass of the core's upkeep (``upkeep.run_upkeep``) that asks for the
missing texts of the house's language (``install.rewrite_changed_meanings``: those never written,
the gaps, and the texts whose meaning a new version of Domo changed). It does so at once when the
core starts (the upkeep's first round), then at most every ``GAP_RETRY`` after the last writing of
the house's version began; never before the installation is over, nor while another writing of the
house's version runs (``install.HouseWriting``, in whose background the writing goes on: the
upkeep's other passes do not wait for it).
"""

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Final

from domo_common.meanings import has_defaults

from domo_core.db import SessionFactory
from domo_core.policy.settings import load_policy_settings
from domo_core.setup_flow.state import setup_stage
from domo_core.texts.install import HouseWriting, rewrite_changed_meanings
from domo_core.texts.writer import TextModels
from domo_core.upkeep import UpkeepPass

# How long after a writing of the house's version began the missing texts are asked again.
GAP_RETRY: Final = timedelta(hours=1)


def text_gaps_pass(
    session_factory: SessionFactory,
    models_for_the_house: Callable[[], Awaitable[TextModels | None]],
    *,
    writing: HouseWriting,
    now: Callable[[], datetime],
) -> UpkeepPass:
    """The pass that writes again the missing texts of the house's language (see the module
    docstring), in the background of ``writing``, on the models ``models_for_the_house`` gives
    then (none: nothing is written), the texts kept at the time ``now`` gives."""

    async def fill_the_gaps(when: datetime) -> None:
        if not writing.due(when, GAP_RETRY):
            return
        async with session_factory() as session:
            installed = await setup_stage(session) == "done"
            language = (await load_policy_settings(session)).house_language
        if not installed or has_defaults(language):
            return
        models = await models_for_the_house()
        if models is None:
            return

        async def write() -> None:
            await rewrite_changed_meanings(session_factory, models, now=now)

        writing.start(write, began=when)

    return fill_the_gaps
