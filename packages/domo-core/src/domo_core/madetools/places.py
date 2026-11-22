"""How many programs the sandbox runs at once is Domo's setting ``places`` (spec 12.1, 12.7):
the launcher takes it while it runs, without a restart (``Sandbox.set_places``).

The core gives it the setting when Domo changes it (``tools.made_tools.ToolsSettings``) and at
each round of the upkeep (``places_pass``), the first one as the core starts: a launcher that
started again since, and so went back to its own number (``DOMO_SANDBOX_SLOTS``), has Domo's
again within a round. A sandbox that does not answer is tried again at the next round."""

import logging
from collections.abc import Callable
from datetime import datetime

from domo_core.db import SessionFactory
from domo_core.madetools.settings import value
from domo_core.sandbox_client import Sandbox, SandboxUnavailable
from domo_core.upkeep import UpkeepPass

logger = logging.getLogger(__name__)


def places_pass(session_factory: SessionFactory, sandbox: Callable[[], Sandbox]) -> UpkeepPass:
    """The pass of the upkeep that gives the sandbox (``sandbox``, read at each round: a test may
    replace it) the house's ``places``."""

    async def give_the_places(now: datetime) -> None:
        async with session_factory() as session:
            places = await value(session, "places")
        try:
            await sandbox().set_places(places)
        except SandboxUnavailable:
            logger.debug("The sandbox was not given its places: it did not answer")

    return give_the_places
