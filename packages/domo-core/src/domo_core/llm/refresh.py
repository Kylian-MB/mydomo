"""What Domo reads again from the world, through the gatekeeper's relay (spec 4.4, 4.7, 5.7): the
catalog of models and prices, the euro's rate, and the models each key lists.

The core reaches no address of the Internet itself: each source is a route of the gatekeeper
without a key (``CATALOG_ROUTE``, models.dev; ``RATES_ROUTE``, the European Central Bank), declared
before each read (``GatekeeperClient.register_provider``), and read through the relay with the
core's token, like a model's call (``relay_get``). What a source answers is not trusted: it is
read only when it answers 200 with a body under a bound, and checked before it is used.

* ``refresh_catalog``: the models.dev list (``/api.json``) is trimmed as the shipped one is
  (``catalog.trim_models_dev``), with the latest euro's rate, and kept as the catalog in force
  (``catalog_snapshots``), audited ``catalog.refreshed`` (``{"models", "usd_per_eur"}``). An
  answer that cannot be read, that is over ``MAX_CATALOG_BYTES``, or that leaves a kind of
  provider Domo knows without a model, changes nothing: the catalog in force stays.
* ``refresh_rate``: the dollar's line of the ECB's daily rates
  (``/stats/eurofxref/eurofxref-daily.xml``) is the dollars of one euro (``exchange_rates``), which
  every check of a limit in euros reads (``spending.usd_per``). A rate that makes no sense is
  left aside.
* ``relist_keys``: a key used in the last day whose list of models is older than
  ``MODEL_LIST_MAX_AGE`` is listed again (``relist_key``, its route declared again first, as when
  it was checked), so that a model the provider added or took away is seen. A listing that fails
  keeps the list the key had, and so does a server that gives no list (its model was named). A
  key removed while it is listed gets its routes declared again without a secret, and nothing is
  written (``provider_keys.key_still_there``).

Each one commits what it read, and never waits on the network in a transaction of its own. A
failure of the network or of the gatekeeper is ``None`` (logged where it happens, by its kind
only), never an exception.
"""

import json
import logging
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Final, Literal, cast

import httpx
from domo_common.gatekeeper_api import ProviderRoute
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.audit.log import append_audit
from domo_core.gatekeeper_client import GatekeeperClient, GatekeeperError
from domo_core.llm.catalog import CATALOG_KINDS, Catalog, catalog_from_json, trim_models_dev
from domo_core.llm.catalog_store import current_catalog
from domo_core.llm.listing import ModelLister
from domo_core.llm.models import Provider, ProviderModel
from domo_core.llm.provider_keys import general_when_none, key_still_there
from domo_core.llm.spending import amount_text, usd_per
from domo_core.llm.spending_models import CatalogSnapshot, ExchangeRate, ModelCall
from domo_core.llm.types import JsonObject, ModelCallError

logger = logging.getLogger(__name__)

# The routes of the gatekeeper, without a key, to the sources Domo reads again.
CATALOG_ROUTE: Final = ProviderRoute(
    id="catalog-models-dev", base_url="https://models.dev", secret_ref=None
)
RATES_ROUTE: Final = ProviderRoute(
    id="rates-ecb", base_url="https://www.ecb.europa.eu", secret_ref=None
)
_CATALOG_PATH: Final = "api.json"
_RATES_PATH: Final = "stats/eurofxref/eurofxref-daily.xml"
# The most a source's answer may weigh (models.dev's whole list is about 6 MB).
MAX_CATALOG_BYTES: Final = 20_000_000
MAX_RATES_BYTES: Final = 100_000
# How old a key's list of models may be when Domo uses the key, and how recent a call makes a
# key used.
MODEL_LIST_MAX_AGE: Final = timedelta(hours=24)
_USED_WITHIN: Final = timedelta(hours=24)

# The dollar's line of the ECB's daily rates: ``<Cube currency='USD' rate='1.1165'/>``.
_USD_RATE: Final = re.compile(
    r"""<Cube\s+currency=['"]USD['"]\s+rate=['"]([0-9]{1,6}(?:\.[0-9]{1,10})?)['"]\s*/>"""
)
# The dollars of one euro never were outside these by far: a rate past them is no rate.
_LOWEST_RATE: Final = Decimal("0.1")
_HIGHEST_RATE: Final = Decimal("10")
# ``exchange_rates.usd_per_unit`` keeps six decimals.
_RATE_STEP: Final = Decimal("0.000001")

# Where a call through the relay carries the core's token: as the OpenAI SDK sends it, or as the
# Anthropic SDK does.
TokenIn = Literal["authorization", "x-api-key"]


async def relay_get(
    gatekeeper: GatekeeperClient,
    http: httpx.AsyncClient,
    route_id: str,
    path: str,
    *,
    max_bytes: int,
    params: Mapping[str, str] | None = None,
    headers: Mapping[str, str] | None = None,
    token_in: TokenIn = "authorization",
) -> bytes | None:
    """The body of ``GET <relay of route_id>/<path>``, with ``params`` and ``headers`` and the
    core's token (``token_in``, which the gatekeeper replaces with the route's key, if it has one).
    ``None`` when the answer is not 200, is longer than ``max_bytes`` (not read further), or does
    not come: logged by its status or its kind, never with the request (it holds the token)."""
    sent = dict(headers or {})
    token = gatekeeper.token
    sent[token_in] = f"Bearer {token}" if token_in == "authorization" else token
    url = f"{gatekeeper.relay_base_url(route_id)}/{path}"
    failure: str | None = None
    body = bytearray()
    try:
        async with http.stream("GET", url, params=params, headers=sent) as response:
            if response.status_code != 200:
                failure = f"status {response.status_code}"
            else:
                declared = response.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > max_bytes:
                    failure = "too big"
                else:
                    async for piece in response.aiter_bytes():
                        body += piece
                        if len(body) > max_bytes:
                            failure = "too big"
                            break
    except (httpx.HTTPError, httpx.InvalidURL) as error:
        failure = type(error).__name__
    if failure is not None:
        # Outside the handler: the HTTP exception holds the request, and so the token.
        logger.warning("A read through the relay of %s failed (%s)", route_id, failure)
        return None
    return bytes(body)


async def _declared(gatekeeper: GatekeeperClient, route: ProviderRoute) -> bool:
    """Declare ``route`` to the gatekeeper; whether it took it (a refusal is logged where it is
    raised, ``gatekeeper_client``)."""
    try:
        await gatekeeper.register_provider(route)
    except GatekeeperError:
        return False
    return True


def _trimmed(raw: object, *, fetched: datetime, usd_per_eur: Decimal) -> JsonObject | None:
    """The catalog's file from the raw models.dev list, if it reads, and gives each kind of
    provider Domo knows at least one model."""
    if not isinstance(raw, dict):
        return None
    try:
        trimmed = trim_models_dev(
            cast(JsonObject, raw), fetched=fetched.astimezone(UTC).date(), usd_per_eur=usd_per_eur
        )
        catalog = catalog_from_json(trimmed)
    except (ValueError, TypeError, ArithmeticError):
        return None
    if any(not catalog.entries[kind] for kind in CATALOG_KINDS):
        return None
    return trimmed


async def refresh_catalog(
    session: AsyncSession, gatekeeper: GatekeeperClient, *, now: datetime, http: httpx.AsyncClient
) -> Catalog | None:
    """Read the models.dev list again and keep it as the catalog in force, fetched at ``now``,
    with the latest euro's rate (see the module docstring): the new catalog, or ``None`` when the
    answer is bad, too big or missing, and the catalog in force stays. Committed."""
    if not await _declared(gatekeeper, CATALOG_ROUTE):
        return None
    body = await relay_get(
        gatekeeper, http, CATALOG_ROUTE.id, _CATALOG_PATH, max_bytes=MAX_CATALOG_BYTES
    )
    if body is None:
        return None
    try:
        raw: object = json.loads(body)
    except ValueError:
        raw = None
    rate = Decimal(amount_text(await usd_per(session, "EUR", await current_catalog(session))))
    trimmed = _trimmed(raw, fetched=now, usd_per_eur=rate)
    if trimmed is None:
        await session.rollback()
        logger.warning("The catalog of models read again cannot be used: the one in force stays")
        return None
    catalog = catalog_from_json(trimmed)
    session.add(CatalogSnapshot(fetched_at=now, data=trimmed))
    await append_audit(
        session,
        actor="system",
        action="catalog.refreshed",
        details={
            "models": sum(len(models) for models in catalog.entries.values()),
            "usd_per_eur": amount_text(rate),
        },
    )
    await session.commit()
    return catalog


async def refresh_rate(
    session: AsyncSession, gatekeeper: GatekeeperClient, *, now: datetime, http: httpx.AsyncClient
) -> Decimal | None:
    """Read the dollars of one euro again from the ECB's daily rates, and keep them, fetched at
    ``now``: the rate, or ``None`` when it could not be read (the latest one stays). Committed."""
    if not await _declared(gatekeeper, RATES_ROUTE):
        return None
    body = await relay_get(gatekeeper, http, RATES_ROUTE.id, _RATES_PATH, max_bytes=MAX_RATES_BYTES)
    if body is None:
        return None
    found = _USD_RATE.search(body.decode("utf-8", errors="replace"))
    try:
        rate = None if found is None else Decimal(found.group(1)).quantize(_RATE_STEP)
    except InvalidOperation:
        rate = None
    if rate is None or not _LOWEST_RATE < rate < _HIGHEST_RATE:
        logger.warning("The euro's rate read again cannot be used: the latest one stays")
        return None
    values = {"usd_per_unit": rate, "fetched_at": now}
    await session.execute(
        insert(ExchangeRate)
        .values(currency="EUR", **values)
        .on_conflict_do_update(index_elements=[ExchangeRate.currency], set_=values)
    )
    await session.commit()
    logger.info("The euro's rate was read again")
    return rate


async def _listed(session: AsyncSession, provider_id: str) -> tuple[str, ...]:
    found = await session.execute(
        select(ProviderModel.model_id)
        .where(ProviderModel.provider_id == provider_id)
        .order_by(ProviderModel.model_id)
    )
    return tuple(found.scalars().all())


async def relist_key(
    session: AsyncSession,
    gatekeeper: GatekeeperClient,
    provider: Provider,
    model_lister: ModelLister,
    *,
    now: datetime,
) -> tuple[str, ...] | None:
    """List again the models of the key ``provider`` (its route declared again first, as when it
    was checked) and keep them, listed at ``now``: the models it lists now, or ``None`` when it
    could not be listed (the list it had stays), or the key was removed meanwhile
    (``provider_keys.key_still_there``: its routes are declared again without a secret, and
    nothing is written). A server that gives no list keeps the model it was named with, read at
    ``now``. ``session`` holds no transaction when it is called (the provider is asked first);
    committed."""
    route = ProviderRoute(
        id=provider.id, base_url=provider.base_url, secret_ref=provider.secret_ref
    )
    if not await _declared(gatekeeper, route):
        return None
    failure: str | None = None
    try:
        models = await model_lister(provider)
    except ModelCallError as error:
        failure, models = error.kind, ()
    # The route was declared with its key: a key removed meanwhile must not keep it.
    if not await key_still_there(session, gatekeeper, provider):
        return None
    if failure is not None:
        # Nothing was written: the lock goes, the session's objects stay readable.
        await session.commit()
        logger.warning("The models of a key could not be listed again (%s)", failure)
        return None
    if models:
        await session.execute(
            delete(ProviderModel).where(
                ProviderModel.provider_id == provider.id, ProviderModel.model_id.not_in(models)
            )
        )
        statement = insert(ProviderModel).values(
            [
                {"provider_id": provider.id, "model_id": model_id, "listed_at": now}
                for model_id in models
            ]
        )
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=[ProviderModel.provider_id, ProviderModel.model_id],
                set_={"listed_at": now},
            )
        )
        # A key of the owner's that works again takes the general key's place when there is none.
        await general_when_none(session, provider)
    else:
        await session.execute(
            update(ProviderModel)
            .where(ProviderModel.provider_id == provider.id)
            .values(listed_at=now)
        )
    listed = await _listed(session, provider.id)
    await session.commit()
    return listed


async def relist_keys(
    session: AsyncSession, gatekeeper: GatekeeperClient, model_lister: ModelLister, *, now: datetime
) -> int:
    """List again the models of each key used in the last day (a call counted on it) whose list
    is older than ``MODEL_LIST_MAX_AGE`` at ``now`` (``relist_key``); a key with no list is not
    one (its check failed: it waits for a key that works). Returns how many were listed again.
    Committed, key by key."""
    used = select(ModelCall.provider_id).where(
        ModelCall.provider_id.is_not(None), ModelCall.started_at >= now - _USED_WITHIN
    )
    old = (
        select(ProviderModel.provider_id)
        .group_by(ProviderModel.provider_id)
        .having(func.max(ProviderModel.listed_at) < now - MODEL_LIST_MAX_AGE)
    )
    found = await session.execute(
        select(Provider)
        .where(Provider.id.in_(used), Provider.id.in_(old))
        .order_by(Provider.created_at, Provider.id)
    )
    keys = list(found.scalars().all())
    # The read ends here: no transaction waits for the providers.
    await session.commit()
    relisted = 0
    for key in keys:
        if await relist_key(session, gatekeeper, key, model_lister, now=now) is not None:
            relisted += 1
    return relisted
