"""The core's periodic upkeep: the passes the household needs now and then, made in turn, when the
core starts and every ``UPKEEP_SECONDS`` after (the scheduler the architecture plans for the core).

A pass (``UpkeepPass``) is a function of the time of the pass that does its own work in sessions of
its own and commits it: erasing the notes of the guests who left (``notes.upkeep.notes_pass``),
pausing the rights of the people who left (``household.upkeep.departures_pass``, spec 5.7),
deleting the roles made for the household that nobody carries
(``household.upkeep.unused_roles_pass``), emptying the traces of the merges of two records
once they can no longer be split (``household.upkeep.merge_traces_pass``),
resuming or stopping the chains paused until someone's arrival
(``confirmations.arrivals.arrivals_pass``, spec 4.3b), expiring the requests past their
lifetime and waking Domo once for those about to lapse
(``confirmations.expiring.expiring_pass``), settling at their maximum the reservations
of the calls to a model that never said what they cost (``stale_reservations_pass``,
``llm.spending.settle_stale``), listing again the models of the keys used in the last day once
their list is a day old (``model_lists_pass``, ``llm.refresh``), and reading the real spending
of the keys with an admin key once a day (``real_spend_pass``, ``llm.real_spend``); a read that
fails is tried again ``RETRY_AFTER`` later, not at every round. The vectors of the memories that
lack one are computed by the local model (``memory_vectors_pass``,
``memory.search.embed_missing``), and the conversations that are quiet are gone back over
(``memory.review.review_pass``, in tasks of the wakes), and Domo is woken for the plans whose
time came (``plans.upkeep.plans_pass``, in tasks of the wakes too). The texts have a pass of
their own (``texts.upkeep.text_gaps_pass``): for a house whose language has no default phrases
of its own, the texts missing from the house's version are written again, in the background, at
the first round and then at most every hour. The catalog of models and the euro's rate are not
the upkeep's: Domo reads them again when he judges it useful
(``tools.model_tools.models_refresh``, spec 4.4). ``run_upkeep`` is the loop the application
starts with its lifespan (``web.app``) and cancels when it stops; the time between two rounds is
the core's setting ``upkeep_seconds`` (``DOMO_UPKEEP_SECONDS``, a minute by default: a chain
resumes at most a minute after the arrival, even after a restart, since the first round is made at
once).

A pass that fails (the database is away for a moment, say) is noted in the log, by the kind of
error alone as everywhere in the core, and the passes after it in the round still run; the next
round tries it again. The loop only ends when it is cancelled, never by what it meets.
"""

import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timedelta
from typing import Final

import anyio
import httpx

from domo_core.db import SessionFactory
from domo_core.gatekeeper_client import GatekeeperClient, GatekeeperError
from domo_core.llm.listing import ModelLister
from domo_core.llm.real_spend import admin_keys_due, read_real_spend
from domo_core.llm.refresh import relist_keys
from domo_core.llm.spending import settle_stale
from domo_core.memory.embedder import Embedder
from domo_core.memory.search import embed_missing

logger = logging.getLogger(__name__)

# How long the upkeep waits between two rounds of passes, in seconds, unless the core's settings
# say otherwise (``CoreSettings.upkeep_seconds``). The first round is made at once.
UPKEEP_SECONDS: Final = 60.0

# One pass of the upkeep: given the time of the round, it does its work and commits it.
UpkeepPass = Callable[[datetime], Awaitable[None]]

# How often the real spending of a key is read again, how often the lists of models are looked
# at, and how long after a read that failed it is tried again.
REFRESH_EVERY: Final = timedelta(days=1)
RELIST_EVERY: Final = timedelta(hours=1)
RETRY_AFTER: Final = timedelta(hours=1)
# How many memories one round of the upkeep computes the vectors of, at most.
VECTORS_PER_PASS: Final = 200


async def run_upkeep(
    passes: Sequence[UpkeepPass],
    clock: Callable[[], datetime],
    *,
    every: float = UPKEEP_SECONDS,
) -> None:
    """Make each of ``passes`` in turn, at the time of ``clock``, right away, then again every
    ``every`` seconds, for as long as it runs.

    It never returns: stop it by cancelling it. A pass that raises is logged (the kind of error
    only: its message may hold what the database said) and the passes after it still run."""
    while True:
        for one_pass in passes:
            try:
                await one_pass(clock())
            except Exception as error:
                logger.error(
                    "An upkeep pass failed (%s); it is made again in %s s",
                    type(error).__name__,
                    every,
                )
        await anyio.sleep(every)


def stale_reservations_pass(session_factory: SessionFactory) -> UpkeepPass:
    """The pass that settles, at their maximum, the reservations of the calls to a model that are
    ``llm.spending.RESERVATION_TTL`` old (their call never said what it cost: the core stopped in
    the middle of it, say), in a session of its own."""

    async def settle_the_stale_reservations(now: datetime) -> None:
        async with session_factory() as session:
            settled = await settle_stale(session, now)
            await session.commit()
        if settled:
            logger.info("%d call(s) to a model settled at their maximum", settled)

    return settle_the_stale_reservations


def model_lists_pass(
    session_factory: SessionFactory,
    gatekeeper: Callable[[], GatekeeperClient],
    model_lister: Callable[[], ModelLister],
) -> UpkeepPass:
    """The pass that lists again the models of the keys used in the last day whose list is a day
    old (``llm.refresh.relist_keys``), at most every ``RELIST_EVERY``, with the gatekeeper and the
    lister read at each pass, as a request reads them."""
    ran: datetime | None = None

    async def relist_the_keys(now: datetime) -> None:
        nonlocal ran
        if ran is not None and now < ran + RELIST_EVERY:
            return
        ran = now
        async with session_factory() as session:
            relisted = await relist_keys(session, gatekeeper(), model_lister(), now=now)
        if relisted:
            logger.info("The models of %d key(s) were listed again", relisted)

    return relist_the_keys


def real_spend_pass(
    session_factory: SessionFactory,
    gatekeeper: Callable[[], GatekeeperClient],
    http: httpx.AsyncClient,
) -> UpkeepPass:
    """The pass that reads the real spending of each key with an admin key whose real spending
    was not read in the last ``REFRESH_EVERY`` (``llm.real_spend.read_real_spend``), each in a
    session of its own, through ``gatekeeper`` (read at each pass) with ``http``. A key whose read
    failed is tried again ``RETRY_AFTER`` later."""
    tried: dict[str, datetime] = {}

    async def read_the_real_spending(now: datetime) -> None:
        async with session_factory() as session:
            due = await admin_keys_due(session, now=now, every=REFRESH_EVERY)
        client = gatekeeper()
        for key in due:
            last = tried.get(key.id)
            if last is not None and now < last + RETRY_AFTER:
                continue
            tried[key.id] = now
            async with session_factory() as session:
                try:
                    await read_real_spend(session, client, key, now=now, http=http)
                except GatekeeperError:
                    continue  # logged where it is raised (``gatekeeper_client``)

    return read_the_real_spending


def memory_vectors_pass(session_factory: SessionFactory, embedder: Embedder) -> UpkeepPass:
    """The pass that computes the vectors the memories lack (``memory.search.embed_missing``):
    those whose vector could not be computed when they were written, or that changed since,
    ``VECTORS_PER_PASS`` at most (a backlog is worked off over several rounds, never in one that
    holds the passes after it). Without a model, or while it fails, it does nothing."""

    async def compute_the_missing_vectors(now: datetime) -> None:
        del now
        written = await embed_missing(session_factory, embedder, limit=VECTORS_PER_PASS)
        if written:
            logger.info("The meaning of %d memory(ies) was computed", written)

    return compute_the_missing_vectors
