"""What Domo costs, in words (spec 4.8): ``spending_show`` tells a person their own spending and,
for the keys they gave, person by person (never a conversation), always saying whether it is an
estimate; ``key_admin`` opens the vault form of an admin key (Anthropic, OpenAI), with which the
real spending is read and corrects the estimate; ``models_refresh`` reads the catalog, the euro's
rate and the models of the turn's key again.

The chat pilots talk with Domo on a live server (see ``chat_support.py``); ``gk_mock`` plays the
gatekeeper (its relay included, ``gk_mock.router``) and ``model_lister`` the lists of models."""

import dataclasses
import json
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
from chat_support import Card
from domo_common.gatekeeper_api import ProviderRoute, SecretRequestCreate
from domo_common.i18n import t
from domo_core.accounts.models import Person
from domo_core.llm.models import Provider, ProviderModel, ProviderUser
from domo_core.llm.picker import ModelPicker
from domo_core.llm.provider_keys import binding_for
from domo_core.llm.real_spend import ANTHROPIC_VERSION, correct_factor, real_spend_supported
from domo_core.llm.refresh import CATALOG_ROUTE, RATES_ROUTE
from domo_core.llm.spending_models import ExchangeRate, ModelCall, RealSpend
from domo_core.policy.catalog import DEFAULT_POLICY, needs_the_judge
from domo_core.policy.roles import Actor, as_role
from domo_core.tools.context import ToolContext
from domo_core.tools.registry import TOOLS, InvalidArguments
from fakes import LISTED_MODELS, FakeChatModel, FakeModelLister, fake_factory
from gatekeeper_support import form_texts
from llm_support import CORE_TOKEN
from sqlalchemy import select
from test_refresh import ECB_DAILY, MODELS_DEV, remove_while_asked, routes_of, without_a_key
from test_spending import add_key

# The middle of a month (a Tuesday), so that a day, a week and a month are three periods.
MID_MONTH = datetime(2030, 1, 15, 12, 0, tzinfo=UTC)


def context(person: Person, session_factory, clock, *, gatekeeper: Any = None) -> ToolContext:
    """The context of a call ``person`` makes outside a turn."""
    return ToolContext(
        session_factory=session_factory,
        actor=Actor(person.id, as_role(person.role)),
        person=person,
        conversation_id=uuid.uuid4(),
        origin="https://domo.example.com/",
        gatekeeper=gatekeeper,
        clock=clock,
        registry=TOOLS,
        policy=DEFAULT_POLICY,
        reply_message_id=None,
    )


async def people(session_factory, *names_and_roles: tuple[str, str]) -> list[Person]:
    async with session_factory() as writer:
        found = [Person(display_name=name, role=role) for name, role in names_and_roles]
        writer.add_all(found)
        await writer.commit()
        return found


async def a_call(
    session_factory,
    provider_id: str,
    usd: str,
    *,
    person_id: uuid.UUID,
    at: datetime,
    price_unknown: bool = False,
) -> None:
    """A call of ``person_id`` on the key, settled at ``usd``, made at ``at`` (committed)."""
    async with session_factory() as writer:
        writer.add(
            ModelCall(
                provider_id=provider_id,
                person_id=person_id,
                task="conversation",
                model_id="example-sonnet-1",
                status="settled",
                reserved_usd=Decimal(usd),
                cost_usd=Decimal(usd),
                price_unknown=price_unknown,
                started_at=at,
                settled_at=at,
            )
        )
        await writer.commit()


async def shown(person: Person, session_factory, clock, **arguments: Any) -> dict[str, Any]:
    tool = TOOLS["spending_show"]
    ctx = context(person, session_factory, clock)
    action = await tool.describe(arguments, ctx.actor, ctx)
    assert (action.action_type, action.effect) == ("spending.show", "read")
    outcome = await tool.execute(arguments, ctx)
    assert outcome.status == "done"
    return dict(outcome.model_result)


# --- what Domo costs ---------------------------------------------------------------------------


async def test_spending_show_tells_an_estimate(session_factory, clock):
    clock.advance(MID_MONTH - clock())
    owner, alex = await people(session_factory, ("Owner", "owner"), ("Alex", "adult"))
    general = await add_key(session_factory, owner.id, general=True)
    now = clock()
    await a_call(session_factory, general.id, "0.5", person_id=alex.id, at=now - timedelta(hours=1))
    await a_call(session_factory, general.id, "0.25", person_id=alex.id, at=now - timedelta(days=1))
    await a_call(
        session_factory, general.id, "0.125", person_id=alex.id, at=datetime(2030, 1, 3, tzinfo=UTC)
    )
    # Last month's, and the owner's own, are not Alex's spending of this month.
    await a_call(
        session_factory, general.id, "2", person_id=alex.id, at=datetime(2029, 12, 20, tzinfo=UTC)
    )
    await a_call(session_factory, general.id, "1", person_id=owner.id, at=now)
    async with session_factory() as writer:
        writer.add(ExchangeRate(currency="EUR", usd_per_unit=Decimal("1.25"), fetched_at=now))
        await writer.commit()

    assert await shown(alex, session_factory, clock) == {
        "period": "month",
        "estimate_usd": "0.875",
        "estimate_eur": "0.7",
        "estimated": True,
        "unknown_prices": False,
        "per_person": [],
    }
    assert (await shown(alex, session_factory, clock, period="week"))["estimate_usd"] == "0.75"
    assert (await shown(alex, session_factory, clock, period="day"))["estimate_usd"] == "0.5"
    # Asking what one costs changes nothing: the judge is not asked.
    assert not needs_the_judge("spending.show")
    ctx = context(alex, session_factory, clock)
    try:
        await TOOLS["spending_show"].describe({"period": "year"}, ctx.actor, ctx)
    except InvalidArguments as error:
        assert error.code == "invalid"
    else:
        raise AssertionError("a period of the catalog only")


async def test_the_giver_sees_per_person_never_conversations(owner_chat, adult_chat):
    await adult_chat.say("Here is my secret recipe: saffron and cardamom")
    reply = await owner_chat.say_with_tool("spending_show", {})
    assert reply.actions == [{"tool": "spending_show", "status": "done"}]
    result = reply.model_results[-1]
    names = {line["name"] for line in result["per_person"]}
    assert names == {"Owner", "Alex"}
    assert all(Decimal(line["estimate_usd"]) > 0 for line in result["per_person"])
    # The pilots' models are not in the catalog: their calls are counted at a fallback price.
    assert result["estimated"] is True and result["unknown_prices"] is True
    told = json.dumps(result)
    for word in ("secret", "saffron", "cardamom", "recipe"):
        assert word not in told
    assert str(adult_chat.person.id) not in told

    # Alex gave no key: he sees his own spending only.
    mine = (await adult_chat.say_with_tool("spending_show", {})).model_results[-1]
    assert mine["per_person"] == [] and Decimal(mine["estimate_usd"]) > 0


async def test_someone_else_s_spending_is_not_shown(adult_chat, child_chat, owner_chat):
    await child_chat.say("bonjour")
    # Sam uses the owner's general key, not a key Alex gave: not Alex's to see.
    refused = await adult_chat.say_with_tool("spending_show", {"person": "Sam"})
    assert refused.actions == [
        {"tool": "spending_show", "status": "invalid", "reason": "not_possible"}
    ]
    nobody = await adult_chat.say_with_tool("spending_show", {"person": "Nobody"})
    assert nobody.actions == [{"tool": "spending_show", "status": "invalid", "reason": "not_found"}]
    himself = await adult_chat.say_with_tool("spending_show", {"person": "Alex"})
    assert himself.actions == [{"tool": "spending_show", "status": "done"}]
    # The owner gave the general key, which Sam uses.
    seen = await owner_chat.say_with_tool("spending_show", {"person": "Sam"})
    assert seen.actions == [{"tool": "spending_show", "status": "done"}]
    assert Decimal(seen.model_results[-1]["estimate_usd"]) > 0

    # Once Alex lets Sam use a key of his, Alex sees what Sam spent on it, and nothing else.
    alexs = await add_key(adult_chat.server.session_factory, adult_chat.person.id)
    async with adult_chat.server.session_factory() as writer:
        writer.add(
            ProviderUser(
                provider_id=alexs.id,
                scope="person",
                person_id=child_chat.person.id,
                allowed=True,
                set_by=adult_chat.person.id,
            )
        )
        await writer.commit()
    await a_call(
        adult_chat.server.session_factory,
        alexs.id,
        "0.5",
        person_id=child_chat.person.id,
        at=adult_chat.server.clock(),
    )
    on_his_key = await adult_chat.say_with_tool("spending_show", {"person": "Sam"})
    assert on_his_key.actions == [{"tool": "spending_show", "status": "done"}]
    assert on_his_key.model_results[-1]["estimate_usd"] == "0.5"


async def test_spending_show_flags_unknown_prices(session_factory, clock):
    clock.advance(MID_MONTH - clock())
    owner, alex = await people(session_factory, ("Owner", "owner"), ("Alex", "adult"))
    general = await add_key(session_factory, owner.id, general=True)
    await a_call(session_factory, general.id, "0.5", person_id=alex.id, at=clock())
    # Last month's call at a fallback price is not this month's.
    await a_call(
        session_factory,
        general.id,
        "1",
        person_id=alex.id,
        at=datetime(2029, 12, 20, tzinfo=UTC),
        price_unknown=True,
    )
    assert (await shown(alex, session_factory, clock))["unknown_prices"] is False
    await a_call(
        session_factory, general.id, "0.5", person_id=alex.id, at=clock(), price_unknown=True
    )
    result = await shown(alex, session_factory, clock)
    assert result["unknown_prices"] is True and result["estimate_usd"] == "1"
    # So does the owner's view of his key, person by person.
    assert (await shown(owner, session_factory, clock))["unknown_prices"] is True


# --- the real spending ---------------------------------------------------------------------------


async def test_the_real_spend_corrects_the_estimate(session_factory, clock):
    clock.advance(MID_MONTH - clock())
    (alex,) = await people(session_factory, ("Alex", "adult"))
    key = await add_key(
        session_factory, alex.id, kind="anthropic", base_url="https://api.anthropic.com"
    )
    await a_call(
        session_factory, key.id, "1", person_id=alex.id, at=datetime(2030, 1, 10, 9, tzinfo=UTC)
    )
    await a_call(
        session_factory, key.id, "0.5", person_id=alex.id, at=datetime(2030, 1, 11, 9, tzinfo=UTC)
    )
    await a_call(session_factory, key.id, "0.25", person_id=alex.id, at=clock())
    async with session_factory() as writer:
        writer.add_all(
            [
                RealSpend(provider_id=key.id, day=date(2030, 1, 10), usd=Decimal("2")),
                RealSpend(provider_id=key.id, day=date(2030, 1, 11), usd=Decimal("1.5")),
            ]
        )
        await writer.commit()

    # Over the last 30 days, the provider counted 3.5 dollars where Domo counted 1.5.
    async with session_factory() as session:
        factor = await correct_factor(session, key, now=clock())
        await session.commit()
    assert factor == Decimal("2.3333")
    async with session_factory() as reader:
        assert (await reader.get_one(Provider, key.id)).spend_factor == Decimal("2.3333")

    result = await shown(alex, session_factory, clock)
    # The days the provider told are its figures; today's estimate is corrected by the factor.
    assert result["real_usd"] == "3.5"
    assert result["estimate_usd"] == "4.0833"
    assert result["estimated"] is True
    assert result["per_person"] == [{"name": "Alex", "estimate_usd": "4.0833"}]

    # A factor is held between a quarter and four times the estimate.
    async with session_factory() as writer:
        row = await writer.get_one(RealSpend, (key.id, date(2030, 1, 10)))
        row.usd = Decimal("100")
        await writer.commit()
    async with session_factory() as session:
        assert await correct_factor(session, key, now=clock()) == Decimal("4")


async def test_an_admin_key_reads_the_real_anthropic_spend(adult_chat, gk_mock, last_audit_entry):
    server = adult_chat.server
    key = await add_key(
        server.session_factory,
        adult_chat.person.id,
        kind="anthropic",
        base_url="https://api.anthropic.com",
    )
    reply = await adult_chat.say_with_tool("key_admin", {"key": key.id}, judge="do")
    assert reply.actions == [{"tool": "key_admin", "status": "done"}]
    assert reply.model_results[-1] == {"status": "done", "admin_form_shown": True}
    # The vault form is for the admin key, bound like the key itself.
    assert 'src="/vault/r/token-req1"' in reply.cards_html
    assert "token-req1" not in reply.model_result_text
    assert SecretRequestCreate.model_validate_json(
        gk_mock.secret_requests.calls.last.request.content
    ) == SecretRequestCreate(
        ref=f"provider:{key.id}:admin_key",
        purpose=t("chat.key.admin.secret_purpose", "en"),
        bindings=[binding_for("anthropic", "https://api.anthropic.com")],
        language="en",
        texts=form_texts("en"),
    )

    today = server.clock().astimezone(UTC).date()
    first, second = today - timedelta(days=2), today - timedelta(days=1)

    def bucket(day: date, *cents: str) -> dict[str, Any]:
        return {
            "starting_at": f"{day.isoformat()}T00:00:00Z",
            "ending_at": f"{(day + timedelta(days=1)).isoformat()}T00:00:00Z",
            "results": [{"amount": amount, "currency": "USD"} for amount in cents],
        }

    def cost_report(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page") == "page-2":
            return httpx.Response(
                200, json={"data": [bucket(second, "50")], "has_more": False, "next_page": None}
            )
        return httpx.Response(
            200,
            json={
                "data": [bucket(first, "123.45", "76.55")],
                "has_more": True,
                "next_page": "page-2",
            },
        )

    report = gk_mock.router.get(f"/llm/{key.id}-admin/v1/organizations/cost_report").mock(
        side_effect=cost_report
    )
    status_url = Card(uuid.uuid4(), "", reply.cards_html).status_url_after_fulfilment(gk_mock)
    checked = await adult_chat.get(status_url)
    assert checked.status_code == 200 and 'data-key-status="checked"' in checked.text

    # Once it is filled in, the admin route is declared, and the real spending is read.
    assert (
        ProviderRoute(
            id=f"{key.id}-admin",
            base_url="https://api.anthropic.com",
            secret_ref=f"provider:{key.id}:admin_key",
        )
        in gk_mock.routes()
    )
    asked = report.calls[0].request
    assert asked.headers["x-api-key"] == CORE_TOKEN
    assert asked.headers["anthropic-version"] == ANTHROPIC_VERSION == "2023-06-01"
    assert dict(asked.url.params) == {
        "starting_at": f"{(today - timedelta(days=30)).isoformat()}T00:00:00Z",
        "ending_at": f"{today.isoformat()}T00:00:00Z",
        "bucket_width": "1d",
    }
    assert report.calls[1].request.url.params["page"] == "page-2"
    async with server.session_factory() as reader:
        stored = await reader.execute(
            select(RealSpend.day, RealSpend.usd).where(RealSpend.provider_id == key.id)
        )
        assert dict(stored.all()) == {first: Decimal("2"), second: Decimal("0.5")}
        assert (await reader.get_one(Provider, key.id)).admin_secret_ref == (
            f"provider:{key.id}:admin_key"
        )
    entry = await last_audit_entry("spending.real_read")
    assert entry.details == {"provider_id": key.id, "days": 2}
    # A later answer for the same form says what was found, without reading again.
    again = await adult_chat.get(status_url)
    assert 'data-key-status="checked"' in again.text and len(report.calls) == 2


async def test_a_child_s_admin_key_is_opened_from_their_card_once(child_chat, adult_chat, gk_mock):
    key = await add_key(
        child_chat.server.session_factory,
        child_chat.person.id,
        kind="openai",
        base_url="https://api.openai.com/v1",
    )
    reply = await child_chat.say_with_tool("key_admin", {"key": key.id})
    assert reply.actions == [{"tool": "key_admin", "status": "waiting"}]
    cid = reply.pending_confirmation_id
    answer = await adult_chat.post(f"/confirmations/{cid}/approve")
    assert 'data-outcome="executed"' in answer.text
    # The form is the child's: nothing is asked of the vault until they press the button.
    assert not gk_mock.secret_requests.called and "/vault/r/" not in answer.text
    page = (await child_chat.get("/chat")).text
    button = f"/chat/key/{key.id}/admin/form?confirmation={cid}"
    assert f'hx-post="{button}"' in page.replace("&amp;", "&")
    form = await child_chat.post(button)
    assert form.status_code == 200 and 'src="/vault/r/token-req1"' in form.text
    assert (
        SecretRequestCreate.model_validate_json(
            gk_mock.secret_requests.calls.last.request.content
        ).ref
        == f"provider:{key.id}:admin_key"
    )

    def costs(request: httpx.Request) -> httpx.Response:
        bucket = {
            "object": "bucket",
            "start_time": int(request.url.params["start_time"]),
            "end_time": int(request.url.params["start_time"]) + 86400,
            "results": [
                {
                    "object": "organization.costs.result",
                    "amount": {"value": 0.06, "currency": "usd"},
                }
            ],
        }
        return httpx.Response(
            200, json={"object": "page", "data": [bucket], "has_more": False, "next_page": None}
        )

    report = gk_mock.router.get(f"/llm/{key.id}-admin/organization/costs").mock(side_effect=costs)
    status_url = Card(uuid.uuid4(), "", form.text).status_url_after_fulfilment(gk_mock)
    checked = await child_chat.get(status_url)
    assert 'data-key-status="checked"' in checked.text
    asked = report.calls[0].request
    assert asked.headers["authorization"] == f"Bearer {CORE_TOKEN}"
    assert asked.url.params["bucket_width"] == "1d"
    async with child_chat.server.session_factory() as reader:
        [usd] = (
            await reader.execute(select(RealSpend.usd).where(RealSpend.provider_id == key.id))
        ).scalars()
        assert usd == Decimal("0.06")
    # One yes, one admin key: the key is saved, so the button opens nothing more.
    assert (await child_chat.post(button)).status_code == 403


async def test_google_has_no_real_spend(adult_chat, gk_mock):
    assert real_spend_supported("anthropic") and real_spend_supported("openai")
    assert not real_spend_supported("google")
    assert not real_spend_supported("openai_compatible")
    key = await add_key(
        adult_chat.server.session_factory,
        adult_chat.person.id,
        kind="google",
        base_url="https://generativelanguage.googleapis.com",
    )
    reply = await adult_chat.say_with_tool("key_admin", {"key": key.id})
    assert reply.actions == [{"tool": "key_admin", "status": "invalid", "reason": "not_possible"}]
    assert reply.model_results[-1]["detail"] == "Google gives no spending to a key: estimates only"
    assert not gk_mock.secret_requests.called
    # Someone else's key is not theirs to give an admin key for.
    general = await adult_chat.say_with_tool("key_admin", {"key": adult_chat.server.provider_id})
    assert general.actions == [{"tool": "key_admin", "status": "invalid", "reason": "not_found"}]


# --- the catalog, the rate and the models of the key, read again --------------------------------


async def test_models_refresh_relists_the_turns_key(owner_chat, gk_mock, model_lister):
    gk_mock.router.get(f"/llm/{CATALOG_ROUTE.id}/api.json").mock(
        return_value=httpx.Response(200, json=MODELS_DEV)
    )
    gk_mock.router.get(f"/llm/{RATES_ROUTE.id}/stats/eurofxref/eurofxref-daily.xml").mock(
        return_value=httpx.Response(200, text=ECB_DAILY)
    )
    model_lister.models = (*LISTED_MODELS, "example-new-1")
    reply = await owner_chat.say_with_tool("models_refresh", {})
    assert reply.actions == [{"tool": "models_refresh", "status": "done"}]
    assert reply.model_results[-1] == {
        "status": "done",
        "catalog": "refreshed",
        "models": 3,
        "rate": "refreshed",
        "usd_per_eur": "1.1165",
        "key_list": "refreshed",
        "key_models": 4,
    }
    provider_id = owner_chat.server.provider_id
    assert model_lister.listed[-1] == provider_id
    async with owner_chat.server.session_factory() as reader:
        found = await reader.execute(
            select(ProviderModel.model_id).where(ProviderModel.provider_id == provider_id)
        )
        assert set(found.scalars().all()) == set(model_lister.models)


async def test_models_refresh_on_a_key_removed_meanwhile_keeps_no_secret_on_its_routes(
    session_factory, gk_client, gk_mock, clock
):
    # The catalog and the rate do not answer: only the key's list is at stake here.
    gk_mock.router.get(path__regex=r"^/llm/(catalog-models-dev|rates-ecb)/").mock(
        return_value=httpx.Response(503)
    )
    (owner,) = await people(session_factory, ("Owner", "owner"))
    key = await add_key(session_factory, owner.id, general=True)
    lister = FakeModelLister()
    lister.while_asked = await remove_while_asked(session_factory, key.id)
    ctx = dataclasses.replace(
        context(owner, session_factory, clock, gatekeeper=gk_client),
        models=ModelPicker(
            session_factory=session_factory,
            model_factory=fake_factory(FakeChatModel(["ok"])),
            provider=key,
            person_id=owner.id,
            clock=clock,
        ),
        model_lister=lister,
    )

    outcome = await TOOLS["models_refresh"].execute({}, ctx)

    assert lister.listed == [key.id]
    assert outcome.model_result["key_list"] == "kept"
    assert routes_of(gk_mock, key) == [
        ProviderRoute(id=key.id, base_url=key.base_url, secret_ref=key.secret_ref),
        *without_a_key(key),
    ]
    async with session_factory() as reader:
        found = await reader.execute(
            select(ProviderModel.model_id).where(ProviderModel.provider_id == key.id)
        )
        assert found.all() == []
