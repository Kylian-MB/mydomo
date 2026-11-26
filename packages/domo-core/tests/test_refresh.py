"""The catalog of models and the euro's rate, read again through the gatekeeper's relay (spec 4.4,
4.7, 5.7), and the lists of models of the keys, read again once a day when Domo uses them (spec
4.2): an answer that cannot be read, or that is too big, leaves the catalog in force as it was."""

import uuid
from datetime import datetime, timedelta
from decimal import Decimal

import httpx
from domo_common.gatekeeper_api import ProviderRoute
from domo_core.accounts.models import Person
from domo_core.audit.models import AuditEntry
from domo_core.gatekeeper_client import new_http_client
from domo_core.llm import refresh
from domo_core.llm.catalog import shipped_catalog
from domo_core.llm.catalog_store import current_catalog
from domo_core.llm.models import Provider, ProviderModel, new_provider_id
from domo_core.llm.refresh import (
    CATALOG_ROUTE,
    MAX_CATALOG_BYTES,
    MODEL_LIST_MAX_AGE,
    RATES_ROUTE,
    refresh_catalog,
    refresh_rate,
)
from domo_core.llm.spending import usd_per
from domo_core.llm.spending_models import CatalogSnapshot, ExchangeRate, ModelCall
from domo_core.upkeep import model_lists_pass
from fakes import FakeModelLister
from llm_support import CORE_TOKEN
from sqlalchemy import delete, func, select

CATALOG_PATH = f"/llm/{CATALOG_ROUTE.id}/api.json"
RATES_PATH = f"/llm/{RATES_ROUTE.id}/stats/eurofxref/eurofxref-daily.xml"


def _model(input_price: float, output_price: float) -> dict[str, object]:
    return {
        "cost": {"input": input_price, "output": output_price},
        "limit": {"context": 200000, "output": 8000},
        "tool_call": True,
        "modalities": {"input": ["text", "image"]},
    }


# A small answer of models.dev, in its own shape: one chat model for each kind it knows.
MODELS_DEV = {
    "anthropic": {"models": {"claude-test-1": _model(1, 5)}},
    "openai": {"models": {"gpt-test-1": _model(2, 8)}},
    "google": {"models": {"gemini-test-1": _model(0.5, 2)}},
    "someone-else": {"models": {"other-1": _model(1, 1)}},
}

# The ECB's daily rates, as it publishes them (three of its lines).
ECB_DAILY = """<?xml version="1.0" encoding="UTF-8"?>
<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01" xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">
	<gesmes:subject>Reference rates</gesmes:subject>
	<gesmes:Sender>
		<gesmes:name>European Central Bank</gesmes:name>
	</gesmes:Sender>
	<Cube>
		<Cube time='2030-01-01'>
			<Cube currency='USD' rate='1.1165'/>
			<Cube currency='JPY' rate='176.77'/>
			<Cube currency='GBP' rate='0.83415'/>
		</Cube>
	</Cube>
</gesmes:Envelope>
"""


async def snapshots(session_factory) -> int:
    async with session_factory() as reader:
        return (
            await reader.execute(select(func.count()).select_from(CatalogSnapshot))
        ).scalar_one()


async def refreshed_entries(session_factory) -> list[AuditEntry]:
    async with session_factory() as reader:
        rows = await reader.execute(
            select(AuditEntry)
            .where(AuditEntry.action == "catalog.refreshed")
            .order_by(AuditEntry.id)
        )
        return list(rows.scalars().all())


async def test_a_bad_catalog_answer_keeps_the_old_one(session_factory, gk_client, gk_mock, clock):
    answer = gk_mock.router.get(CATALOG_PATH).mock(
        return_value=httpx.Response(200, json=MODELS_DEV)
    )
    async with session_factory() as session, new_http_client() as http:
        first = await refresh_catalog(session, gk_client, now=clock(), http=http)
    # The route is declared to the gatekeeper without a key, and the core's token goes to the relay.
    assert CATALOG_ROUTE in gk_mock.routes() and CATALOG_ROUTE.secret_ref is None
    assert answer.calls.last.request.headers["authorization"] == f"Bearer {CORE_TOKEN}"
    assert first is not None
    assert first.lookup("anthropic", "claude-test-1") is not None
    assert first.lookup("openai", "gpt-test-1") is not None
    async with session_factory() as reader:
        assert (await current_catalog(reader)).lookup("google", "gemini-test-1") is not None
    [entry] = await refreshed_entries(session_factory)
    assert entry.actor == "system"
    assert entry.details == {"models": 3, "usd_per_eur": str(shipped_catalog().usd_per_eur)}

    for bad in (
        httpx.Response(200, text="not json at all"),
        httpx.Response(200, json=[1, 2, 3]),
        # An answer with no model of a kind Domo knows would leave him without prices.
        httpx.Response(200, json={"anthropic": {"models": {}}}),
        httpx.Response(200, json={"anthropic": {"models": {"claude-test-1": {"cost": "free"}}}}),
        httpx.Response(502, json=MODELS_DEV),
    ):
        answer.mock(return_value=bad)
        clock.advance(timedelta(days=1))
        async with session_factory() as session, new_http_client() as http:
            assert await refresh_catalog(session, gk_client, now=clock(), http=http) is None
        async with session_factory() as reader:
            kept = await current_catalog(reader)
        assert kept.fetched == first.fetched
        assert kept.lookup("anthropic", "claude-test-1") is not None
    assert await snapshots(session_factory) == 1
    assert len(await refreshed_entries(session_factory)) == 1


async def test_a_catalog_too_big_is_refused(
    session_factory, gk_client, gk_mock, clock, monkeypatch
):
    assert MAX_CATALOG_BYTES == 20_000_000
    # A smaller bound for the test: the same rule, without twenty megabytes of answer.
    monkeypatch.setattr(refresh, "MAX_CATALOG_BYTES", 2_000)
    padded = {**MODELS_DEV, "padding": {"models": {}, "text": "x" * 3_000}}
    gk_mock.router.get(CATALOG_PATH).mock(return_value=httpx.Response(200, json=padded))
    async with session_factory() as session, new_http_client() as http:
        assert await refresh_catalog(session, gk_client, now=clock(), http=http) is None
    assert await snapshots(session_factory) == 0
    async with session_factory() as reader:
        assert (await current_catalog(reader)) == shipped_catalog()
    assert await refreshed_entries(session_factory) == []


async def test_the_ecb_rate_is_read(session_factory, gk_client, gk_mock, clock):
    rates = gk_mock.router.get(RATES_PATH).mock(return_value=httpx.Response(200, text=ECB_DAILY))
    async with session_factory() as session, new_http_client() as http:
        rate = await refresh_rate(session, gk_client, now=clock(), http=http)
    assert rate == Decimal("1.1165")
    assert RATES_ROUTE in gk_mock.routes() and RATES_ROUTE.secret_ref is None
    assert rates.calls.last.request.headers["authorization"] == f"Bearer {CORE_TOKEN}"
    async with session_factory() as reader:
        row = await reader.get_one(ExchangeRate, "EUR")
        assert (row.usd_per_unit, row.fetched_at) == (Decimal("1.1165"), clock())
        assert await usd_per(reader, "EUR", shipped_catalog()) == Decimal("1.1165")

    # The catalog read next carries that rate.
    gk_mock.router.get(CATALOG_PATH).mock(return_value=httpx.Response(200, json=MODELS_DEV))
    async with session_factory() as session, new_http_client() as http:
        catalog = await refresh_catalog(session, gk_client, now=clock(), http=http)
    assert catalog is not None and catalog.usd_per_eur == Decimal("1.1165")
    [entry] = await refreshed_entries(session_factory)
    assert entry.details == {"models": 3, "usd_per_eur": "1.1165"}

    # An answer without a dollar rate, or with one that makes no sense, changes nothing.
    for bad in (
        httpx.Response(200, text="<Cube currency='JPY' rate='176.77'/>"),
        httpx.Response(200, text="<Cube currency='USD' rate='0'/>"),
        httpx.Response(200, text="<Cube currency='USD' rate='abc'/>"),
        httpx.Response(503, text=ECB_DAILY.replace("1.1165", "1.5")),
    ):
        rates.mock(return_value=bad)
        async with session_factory() as session, new_http_client() as http:
            assert await refresh_rate(session, gk_client, now=clock(), http=http) is None
        async with session_factory() as reader:
            assert (await reader.get_one(ExchangeRate, "EUR")).usd_per_unit == Decimal("1.1165")


async def add_listed_key(
    session_factory, giver: uuid.UUID, *, listed_at: datetime, used_at: datetime | None
) -> Provider:
    """A key of ``giver`` that listed two models at ``listed_at``, with a call at ``used_at``."""
    provider_id = new_provider_id()
    key = Provider(
        id=provider_id,
        kind="openai_compatible",
        base_url="https://llm.example.com/v1",
        secret_ref=f"provider:{provider_id}:api_key",
        given_by=giver,
        is_general=False,
    )
    async with session_factory() as writer:
        writer.add(key)
        await writer.flush()
        writer.add_all(
            ProviderModel(provider_id=provider_id, model_id=model_id, listed_at=listed_at)
            for model_id in ("old-small-1", "old-medium-1")
        )
        if used_at is not None:
            writer.add(
                ModelCall(
                    provider_id=provider_id,
                    person_id=giver,
                    task="conversation",
                    model_id="old-medium-1",
                    status="settled",
                    reserved_usd=Decimal("0.01"),
                    cost_usd=Decimal("0.01"),
                    started_at=used_at,
                    settled_at=used_at,
                )
            )
        await writer.commit()
    return key


async def listed(session_factory, provider_id: str) -> dict[str, datetime]:
    async with session_factory() as reader:
        rows = await reader.execute(
            select(ProviderModel.model_id, ProviderModel.listed_at).where(
                ProviderModel.provider_id == provider_id
            )
        )
        return dict(rows.all())


async def test_the_upkeep_relists_a_key_used_today(session_factory, gk_client, gk_mock, clock):
    async with session_factory() as writer:
        alex = Person(display_name="Alex", role="adult")
        writer.add(alex)
        await writer.commit()
    now = clock()
    old = now - MODEL_LIST_MAX_AGE - timedelta(hours=1)
    used_today = await add_listed_key(
        session_factory, alex.id, listed_at=old, used_at=now - timedelta(hours=2)
    )
    not_used = await add_listed_key(
        session_factory, alex.id, listed_at=old, used_at=now - timedelta(days=3)
    )
    fresh = await add_listed_key(
        session_factory,
        alex.id,
        listed_at=now - timedelta(hours=3),
        used_at=now - timedelta(hours=1),
    )
    lister = FakeModelLister()
    lister.models = ("new-small-1", "new-medium-1", "new-strong-1")
    upkeep = model_lists_pass(session_factory, lambda: gk_client, lambda: lister)

    await upkeep(now)

    assert lister.listed == [used_today.id]
    assert await listed(session_factory, used_today.id) == dict.fromkeys(lister.models, now)
    assert set(await listed(session_factory, not_used.id)) == {"old-small-1", "old-medium-1"}
    assert set(await listed(session_factory, fresh.id)) == {"old-small-1", "old-medium-1"}
    # Its route is declared again first, as when it was checked.
    route = gk_mock.routes()[-1]
    assert (route.id, route.secret_ref) == (used_today.id, used_today.secret_ref)

    # A key whose listing fails, or that gives no list, keeps the list it had.
    later = now + MODEL_LIST_MAX_AGE + timedelta(hours=2)
    async with session_factory() as writer:
        writer.add(
            ModelCall(
                provider_id=used_today.id,
                person_id=alex.id,
                task="conversation",
                model_id="new-medium-1",
                status="settled",
                reserved_usd=Decimal("0.01"),
                cost_usd=Decimal("0.01"),
                started_at=later - timedelta(minutes=5),
                settled_at=later - timedelta(minutes=5),
            )
        )
        await writer.commit()
    lister.error = "unreachable"
    await upkeep(later)
    assert lister.listed == [used_today.id, used_today.id]
    assert set(await listed(session_factory, used_today.id)) == set(lister.models)


async def test_the_owners_key_that_works_again_becomes_the_general_key_when_there_is_none(
    session_factory, gk_client, gk_mock, clock
):
    # Ruling R65 (task 25, fix round 1): the general key went with nobody to take over; the
    # owner's key listed again takes its place, never someone else's.
    async with session_factory() as writer:
        owner = Person(display_name="Alex", role="owner")
        dana = Person(display_name="Dana", role="adult")
        writer.add_all([owner, dana])
        await writer.commit()
    now = clock()
    old = now - MODEL_LIST_MAX_AGE - timedelta(hours=1)
    danas = await add_listed_key(session_factory, dana.id, listed_at=old, used_at=None)
    alexs = await add_listed_key(session_factory, owner.id, listed_at=old, used_at=None)
    lister = FakeModelLister()
    lister.models = ("new-small-1",)
    for key in (danas, alexs):
        async with session_factory() as session:
            await refresh.relist_key(session, gk_client, key, lister, now=now)
    async with session_factory() as reader:
        generals = (await reader.scalars(select(Provider.id).where(Provider.is_general))).all()
    assert generals == [alexs.id]


async def remove_while_asked(session_factory, key_id: str):
    """What ``key_remove`` does while a provider is asked about the key ``key_id``: its routes
    were declared without a key, then its row goes."""

    async def removal(provider: Provider) -> None:
        if provider.id == key_id:
            async with session_factory() as writer:
                await writer.execute(delete(Provider).where(Provider.id == key_id))
                await writer.commit()

    return removal


def routes_of(gk_mock, key: Provider) -> list[ProviderRoute]:
    return [route for route in gk_mock.routes() if route.id.startswith(key.id)]


def without_a_key(key: Provider) -> list[ProviderRoute]:
    """The two routes of ``key`` declared again without a secret."""
    return [
        ProviderRoute(id=key.id, base_url=key.base_url, secret_ref=None),
        ProviderRoute(id=f"{key.id}-admin", base_url=key.base_url, secret_ref=None),
    ]


async def test_a_key_removed_while_it_is_listed_again_keeps_no_secret_on_its_routes(
    session_factory, gk_client, gk_mock, clock
):
    async with session_factory() as writer:
        alex = Person(display_name="Alex", role="adult")
        writer.add(alex)
        await writer.commit()
    now = clock()
    old = now - MODEL_LIST_MAX_AGE - timedelta(hours=1)
    removed = await add_listed_key(
        session_factory, alex.id, listed_at=old, used_at=now - timedelta(hours=2)
    )
    other = await add_listed_key(
        session_factory, alex.id, listed_at=old, used_at=now - timedelta(hours=1)
    )
    lister = FakeModelLister()
    lister.models = ("new-medium-1",)
    lister.while_asked = await remove_while_asked(session_factory, removed.id)

    await model_lists_pass(session_factory, lambda: gk_client, lambda: lister)(now)

    # Its route was declared with its key for the listing; once the key is found gone, both of
    # its routes are declared again without one, and nothing is written for it.
    assert routes_of(gk_mock, removed) == [
        ProviderRoute(id=removed.id, base_url=removed.base_url, secret_ref=removed.secret_ref),
        *without_a_key(removed),
    ]
    assert await listed(session_factory, removed.id) == {}
    # The pass goes on with the other keys.
    assert set(await listed(session_factory, other.id)) == {"new-medium-1"}
