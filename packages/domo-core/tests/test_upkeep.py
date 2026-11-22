"""The upkeep of the account of Domo: a reservation whose call never said what it cost (the core
stopped in the middle, say) is settled at its maximum once it is ``RESERVATION_TTL`` old, by a
pass of the core's upkeep that the application runs; the real spending of a key with an admin key
is read once a day, and a key removed meanwhile keeps no secret on its routes. The catalog of
models and the euro's rate are not the upkeep's: Domo reads them again when he judges it
useful."""

import uuid
from datetime import datetime, timedelta
from decimal import Decimal

import anyio
import httpx
from domo_common.gatekeeper_api import ProviderRoute
from domo_core.accounts.models import Person
from domo_core.audit.models import AuditEntry
from domo_core.gatekeeper_client import new_http_client
from domo_core.llm import real_spend
from domo_core.llm.models import Provider, new_provider_id
from domo_core.llm.spending import RESERVATION_TTL
from domo_core.llm.spending_models import ModelCall, RealSpend
from domo_core.upkeep import UpkeepPass, real_spend_pass, stale_reservations_pass
from domo_core.web import app as web_app
from domo_core.web.app import create_app
from domo_core.web.deps import get_clock
from llm_support import GATEKEEPER_URL
from sqlalchemy import delete, select


async def a_reservation(session_factory, *, started_at: datetime) -> uuid.UUID:
    """A call reserved at ``started_at`` on a new key, at most 0.3 dollars (committed)."""
    async with session_factory() as writer:
        alex = Person(display_name="Alex", role="adult")
        writer.add(alex)
        await writer.flush()
        provider_id = new_provider_id()
        writer.add(
            Provider(
                id=provider_id,
                kind="openai_compatible",
                base_url="https://llm.example.com/v1",
                secret_ref=f"provider:{provider_id}:api_key",
                given_by=alex.id,
                is_general=False,
            )
        )
        await writer.flush()
        call = ModelCall(
            provider_id=provider_id,
            person_id=alex.id,
            task="conversation",
            model_id="example-sonnet-1",
            status="reserved",
            reserved_usd=Decimal("0.3"),
            started_at=started_at,
        )
        writer.add(call)
        await writer.commit()
        return call.id


async def status_of(session_factory, call_id: uuid.UUID) -> tuple[str, Decimal | None]:
    async with session_factory() as reader:
        row = (
            await reader.execute(
                select(ModelCall.status, ModelCall.cost_usd).where(ModelCall.id == call_id)
            )
        ).one()
        return row.status, row.cost_usd


async def test_the_stale_reservations_pass_settles_them_and_commits(session_factory, clock):
    stale = await a_reservation(session_factory, started_at=clock() - RESERVATION_TTL)
    fresh = await a_reservation(session_factory, started_at=clock() - timedelta(minutes=1))

    await stale_reservations_pass(session_factory)(clock())

    assert await status_of(session_factory, stale) == ("settled", Decimal("0.3"))
    assert await status_of(session_factory, fresh) == ("reserved", None)


async def test_the_app_settles_the_stale_reservations(settings, session_factory, clock):
    # Stale on the application's clock (2030), not on the real one: the upkeep reads the time the
    # way the pages do.
    stale = await a_reservation(
        session_factory, started_at=clock() - RESERVATION_TTL - timedelta(minutes=1)
    )
    app = create_app(settings)
    app.dependency_overrides[get_clock] = lambda: clock
    async with app.router.lifespan_context(app):
        with anyio.fail_after(30):
            while (await status_of(session_factory, stale))[0] != "settled":
                await anyio.sleep(0.01)
    assert await status_of(session_factory, stale) == ("settled", Decimal("0.3"))


# --- what the upkeep reads of the world ---------------------------------------------------------


async def test_the_upkeep_reads_neither_the_catalog_nor_the_rate_by_itself(
    settings, gk_mock, clock, monkeypatch
):
    # Domo reads the catalog and the euro's rate again when he judges it useful
    # (``models_refresh``), never on his own schedule: a fresh start asks the world nothing.
    gk_mock.router.get(path__regex=r"^/llm/").mock(return_value=httpx.Response(503))
    rounds: list[list[UpkeepPass]] = []

    async def one_round(passes, clock, *, every):
        rounds.append(list(passes))

    monkeypatch.setattr(web_app, "run_upkeep", one_round)
    app = create_app(settings.model_copy(update={"gatekeeper_url": GATEKEEPER_URL}))
    app.dependency_overrides[get_clock] = lambda: clock
    async with app.router.lifespan_context(app):
        with anyio.fail_after(30):
            while not rounds:
                await anyio.sleep(0.01)
        for one_pass in rounds[0]:
            await one_pass(clock())
    assert gk_mock.routes() == []
    assert not [call for call in gk_mock.router.calls if call.request.url.path.startswith("/llm/")]


async def test_the_upkeep_reads_the_real_spend_once_a_day(
    session_factory, gk_client, gk_mock, clock
):
    async with session_factory() as writer:
        alex = Person(display_name="Alex", role="adult")
        writer.add(alex)
        await writer.flush()
        with_admin = Provider(
            id=new_provider_id(),
            kind="anthropic",
            base_url="https://api.anthropic.com",
            given_by=alex.id,
            is_general=False,
        )
        with_admin.secret_ref = f"provider:{with_admin.id}:api_key"
        with_admin.admin_secret_ref = f"provider:{with_admin.id}:admin_key"
        without = Provider(
            id=new_provider_id(),
            kind="anthropic",
            base_url="https://api.anthropic.com",
            given_by=alex.id,
            is_general=False,
        )
        without.secret_ref = f"provider:{without.id}:api_key"
        writer.add_all([with_admin, without])
        await writer.commit()
    now = clock()
    yesterday = now.date() - timedelta(days=1)
    report = gk_mock.router.get(f"/llm/{with_admin.id}-admin/v1/organizations/cost_report").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "starting_at": f"{yesterday.isoformat()}T00:00:00Z",
                        "ending_at": f"{now.date().isoformat()}T00:00:00Z",
                        "results": [{"amount": "250", "currency": "USD"}],
                    }
                ],
                "has_more": False,
                "next_page": None,
            },
        )
    )
    async with new_http_client() as http:
        upkeep = real_spend_pass(session_factory, lambda: gk_client, http)
        await upkeep(now)
        await upkeep(now + timedelta(hours=3))
        assert report.call_count == 1
        await upkeep(now + timedelta(days=1, minutes=1))
        assert report.call_count == 2
    async with session_factory() as reader:
        rows = await reader.execute(select(RealSpend.provider_id, RealSpend.day, RealSpend.usd))
        assert rows.all() == [(with_admin.id, yesterday, Decimal("2.5"))]
    # Its admin route is declared before each read, with the admin key.
    route = gk_mock.routes()[-1]
    assert (route.id, route.secret_ref) == (f"{with_admin.id}-admin", with_admin.admin_secret_ref)


async def test_a_key_removed_while_its_real_spend_is_read_keeps_no_secret_on_its_routes(
    session_factory, gk_client, gk_mock, clock, monkeypatch
):
    async with session_factory() as writer:
        alex = Person(display_name="Alex", role="adult")
        writer.add(alex)
        await writer.flush()
        key = Provider(
            id=new_provider_id(),
            kind="anthropic",
            base_url="https://api.anthropic.com",
            given_by=alex.id,
            is_general=False,
        )
        key.secret_ref = f"provider:{key.id}:api_key"
        key.admin_secret_ref = f"provider:{key.id}:admin_key"
        writer.add(key)
        await writer.commit()

    async def read_while_removed(gatekeeper, provider, *, since, until, http):
        # The key is removed while its provider answers: its routes were declared without a key,
        # then its row went (``key_remove``).
        async with session_factory() as writer:
            await writer.execute(delete(Provider).where(Provider.id == provider.id))
            await writer.commit()
        return {since: Decimal("1")}

    monkeypatch.setattr(real_spend, "fetch_real_spend", read_while_removed)
    async with new_http_client() as http:
        await real_spend_pass(session_factory, lambda: gk_client, http)(clock())

    # The admin route was declared with its key for the read; once the key is found gone, both
    # routes are declared again without one, and nothing is written.
    assert [route for route in gk_mock.routes() if route.id.startswith(key.id)] == [
        ProviderRoute(id=f"{key.id}-admin", base_url=key.base_url, secret_ref=key.admin_secret_ref),
        ProviderRoute(id=key.id, base_url=key.base_url, secret_ref=None),
        ProviderRoute(id=f"{key.id}-admin", base_url=key.base_url, secret_ref=None),
    ]
    async with session_factory() as reader:
        assert (await reader.execute(select(RealSpend))).all() == []
        read = await reader.execute(
            select(AuditEntry.id).where(AuditEntry.action == "spending.real_read")
        )
        assert read.all() == []
