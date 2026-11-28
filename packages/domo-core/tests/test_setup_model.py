"""First launch, second step: the owner chooses a model provider, enters its key in the
gatekeeper's vault form, and Domo checks that the model answers: once it does, the step is over
at once, without a text written (spec 5.1)."""

import logging
import re
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from html import unescape
from typing import get_args

import httpx
import pytest
from domo_common.gatekeeper_api import ProviderRoute, SecretRequestCreate
from domo_common.i18n import t
from domo_core.accounts.models import Person
from domo_core.audit.log import ChainCheck, verify_audit_chain
from domo_core.llm.models import Provider, ProviderModel
from domo_core.llm.provider_kinds import PROVIDER_KINDS
from domo_core.llm.speech.home_key import HOME_SPEECH_ID
from domo_core.llm.types import (
    ChatChunk,
    ChatUsage,
    ErrorKind,
    HistoryItem,
    ToolCall,
    ToolChoice,
    ToolDefinition,
)
from domo_core.policy.settings import load_policy_settings
from domo_core.setup_flow.models import SetupState
from domo_core.setup_flow.state import setup_stage
from domo_core.texts.models import WrittenText
from domo_core.web.app import STATIC_DIR
from domo_core.web.deps import get_session_factory
from domo_core.web.templating import static_url
from fakes import FakeChatModel
from fastapi import FastAPI
from gatekeeper_support import GatekeeperMock
from llm_support import CORE_TOKEN
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from web_support import OWNER_PASSWORD, audit_entries, build_test_app, log_in

KEYED_FORM = {"kind": "anthropic"}
LOCAL_FORM = {
    "kind": "openai_compatible",
    "base_url": "http://192.168.1.10:11434/v1",
}
STATUS_URL = re.compile(r'hx-get="(/setup/model/status/(p-[0-9a-f]{8})/([^"/]+))"')
ACTION = re.compile(r'data-action="([a-z]+)"')


@pytest.fixture
def house_texts() -> None:
    """A fresh instance has no text yet, and its installation writes none."""


@pytest.fixture
def app(settings, session, clock) -> FastAPI:
    """The application on the shared rolled-back session, with the first launch not over."""
    return build_test_app(settings, session, clock)


@dataclass(frozen=True)
class StartedRequest:
    """A cloud provider that was chosen, and the secret request its form opened."""

    provider_id: str
    request_id: str
    ref: str
    status_url: str


async def start_request(owner_client: httpx.AsyncClient, gk_mock: GatekeeperMock) -> StartedRequest:
    r = await owner_client.post("/setup/model", data=KEYED_FORM)
    match = STATUS_URL.search(r.text)
    assert match, r.text
    status_url, provider_id, request_id = match.groups()
    return StartedRequest(provider_id, request_id, gk_mock.refs[request_id], status_url)


@pytest.fixture
async def pending_request(owner_client, gk_mock) -> StartedRequest:
    return await start_request(owner_client, gk_mock)


@pytest.fixture
async def fulfilled_request(owner_client, gk_mock) -> StartedRequest:
    started = await start_request(owner_client, gk_mock)
    gk_mock.set_status(started.request_id, "fulfilled")
    return started


def visible(response: httpx.Response) -> str:
    """The text of a page as a person reads it (HTML entities decoded)."""
    return unescape(response.text)


def actions(response: httpx.Response) -> set[str]:
    """The ``data-action`` buttons of a fragment."""
    return set(ACTION.findall(response.text))


def sent_request(gk_mock: GatekeeperMock) -> SecretRequestCreate:
    """The body of the last secret request the core sent."""
    return SecretRequestCreate.model_validate_json(
        gk_mock.secret_requests.calls.last.request.content
    )


def registered_route(gk_mock: GatekeeperMock) -> ProviderRoute:
    return ProviderRoute.model_validate_json(gk_mock.providers.calls.last.request.content)


async def step_entries(session):
    """The audit entries of the model step (logging in, to reach it, is not one of them)."""
    return [entry for entry in await audit_entries(session) if not entry.action.startswith("auth.")]


async def audit_actions(session) -> list[str]:
    return [entry.action for entry in await step_entries(session)]


async def listed_models(session, provider_id: str) -> list[str]:
    rows = await session.execute(
        select(ProviderModel.model_id)
        .where(ProviderModel.provider_id == provider_id)
        .order_by(ProviderModel.model_id)
    )
    return list(rows.scalars())


async def provider_rows(session) -> list[Provider]:
    return list((await session.execute(select(Provider).order_by(Provider.id))).scalars())


async def setup_state_row(session) -> SetupState:
    return (await session.execute(select(SetupState))).scalar_one()


# --- the form ---------------------------------------------------------------------------------


async def test_the_form_offers_the_four_kinds_and_the_fields(owner_client):
    r = await owner_client.get("/setup/model")
    assert r.status_code == 200
    html = r.text
    assert 'name="kind"' in html and 'name="model"' not in html
    assert 'name="base_url"' in html and 'name="has_key"' in html
    for kind in PROVIDER_KINDS:
        assert f'<option value="{kind}"' in html
    assert t("setup.model.title", "en") in html
    assert "<iframe" not in html


async def test_the_form_is_in_the_language_of_the_visitor(owner_client):
    r = await owner_client.get("/setup/model", headers={"Accept-Language": "fr-FR,fr;q=0.9"})
    assert t("setup.model.title", "fr") in visible(r)
    assert t("setup.model.title", "en") not in visible(r)


async def test_the_form_submits_through_htmx_into_the_step_container(owner_client):
    html = (await owner_client.get("/setup/model")).text
    assert 'id="model-step"' in html
    assert 'hx-post="/setup/model"' in html and 'hx-target="#model-step"' in html
    assert f'<script src="{static_url("model-step.js")}" defer></script>' in html


def live_region_of(html: str, note_id: str) -> re.Match[str] | None:
    """The live region (``role="status"``, never hidden) around the note ``note_id`` (hidden while
    nothing runs); group 1 is the text of the note."""
    return re.search(
        rf'<p class="[^"]*busy-status[^"]*" role="status"><span id="{note_id}" '
        rf'class="busy-note">([^<]*)</span></p>',
        html,
    )


async def test_the_form_shows_that_the_model_is_being_checked(owner_client):
    """Checking the model can take a while (the provider answers a question): the button is
    disabled meanwhile, and a note says what is going on (task 14)."""
    html = (await owner_client.get("/setup/model")).text
    form = re.search(r"<form[^>]*>", html)
    assert form is not None
    assert 'hx-disabled-elt="find button"' in form.group(0)
    indicator = re.search(r'hx-indicator="#([a-z-]+)"', form.group(0))
    assert indicator is not None
    # The note shows while the check runs, inside a live region that is always there: a screen
    # reader hears the note when it appears (fix round 1).
    region = live_region_of(html, indicator.group(1))
    assert region is not None and region.group(1) == t("setup.model.checking", "en")
    french = await owner_client.get("/setup/model", headers={"accept-language": "fr"})
    assert t("setup.model.checking", "fr") in visible(french)


def test_the_note_of_the_check_is_translated():
    assert t("setup.model.checking", "en") == "Checking the model..."
    assert t("setup.model.checking", "fr") == "Vérification du modèle..."


def test_the_note_shows_only_while_the_check_runs():
    """htmx puts ``htmx-request`` on the element its ``hx-indicator`` names while the request
    runs; otherwise the note is not displayed. Its live region is never hidden: it is there before
    the note appears, so that the note is announced."""
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)  # the rules, not their comments
    assert re.search(r"\.busy-note:not\(\.htmx-request\)\s*\{\s*display:\s*none;", css)
    assert not re.search(r"\.busy-status[^{]*\{[^}]*display:\s*none", css)


async def test_trying_again_shows_the_same_note(owner_client, gk_mock, use_model):
    use_model(FakeChatModel([], fail_after=0, error_kind="unreachable"))
    r = await owner_client.post("/setup/model", data=LOCAL_FORM)
    assert 'data-action="retry"' in r.text
    retry = re.search(r'<button[^>]*data-action="retry"[^>]*>', r.text)
    assert retry is not None
    assert 'hx-disabled-elt="this"' in retry.group(0)
    assert 'hx-indicator="#model-checking"' in retry.group(0)
    assert live_region_of(r.text, "model-checking") is not None


async def test_the_model_step_is_for_the_owner_only(
    client, add_person, add_account, owner_password_hash
):
    await add_account(await add_person("Owner", "owner"), "owner", "hash")
    await add_account(await add_person("Member", "adult"), "member", owner_password_hash)
    await log_in(client, "member", OWNER_PASSWORD)
    for method, path in (
        ("GET", "/setup/model"),
        ("POST", "/setup/model"),
        ("GET", "/setup/model/status/p-00000000/req1"),
        ("POST", "/setup/model/p-00000000/rekey"),
        ("POST", "/setup/model/p-00000000/retry"),
        ("POST", "/setup/model/p-00000000/discard"),
    ):
        assert (await client.request(method, path)).status_code == 403, (method, path)


async def test_anonymous_requests_are_sent_to_the_login(client, session):
    await session.execute(text("UPDATE setup_state SET owner_created_at = now()"))
    for method, path in (
        ("POST", "/setup/model"),
        ("GET", "/setup/model/status/p-00000000/req1"),
        ("POST", "/setup/model/p-00000000/discard"),
    ):
        r = await client.request(method, path)
        assert (r.status_code, r.headers["location"]) == (303, "/login"), (method, path)


# --- the models of the key: listed, and the cheapest one that calls tools checks it --------------


async def owner_id(session):
    return (await session.execute(select(Person.id).where(Person.role == "owner"))).scalar_one()


async def test_the_model_step_asks_no_model_name(owner_client):
    html = (await owner_client.get("/setup/model")).text
    assert 'name="model"' not in html
    assert t("setup.model.name.label", "en") not in html


async def test_a_key_is_checked_with_the_cheapest_model_that_calls_tools(
    app, owner_client, gk_mock, fulfilled_request, use_model, model_lister, session
):
    model_lister.models = ("claude-sonnet-4-5", "claude-haiku-4-5")
    factory = use_model(FakeChatModel(["pong"]))
    r = await owner_client.get(fulfilled_request.status_url)
    assert r.headers["hx-redirect"] == "/chat"
    # The check, and nothing else.
    assert factory.requested == [(fulfilled_request.provider_id, "claude-haiku-4-5")]
    assert await listed_models(session, fulfilled_request.provider_id) == [
        "claude-haiku-4-5",
        "claude-sonnet-4-5",
    ]
    (row,) = await provider_rows(session)
    await session.refresh(row)
    assert row.given_by == await owner_id(session) and row.is_general


async def test_a_server_without_a_list_asks_the_name_on_the_page(
    app, owner_client, use_model, model_lister, session
):
    factory = use_model(FakeChatModel(["pong"]))
    model_lister.models = ()
    r = await owner_client.post("/setup/model", data=LOCAL_FORM)
    (row,) = await provider_rows(session)
    assert r.status_code == 200 and "hx-redirect" not in r.headers
    assert f'action="/setup/model/{row.id}/name"' in r.text and 'name="model"' in r.text
    assert t("setup.model.name.intro", "en") in visible(r)
    assert factory.requested == [] and await setup_stage(session) == "model"
    named = await owner_client.post(f"/setup/model/{row.id}/name", data={"model": "local-model"})
    assert named.headers["hx-redirect"] == "/chat"
    assert set(factory.requested) == {(row.id, "local-model")}
    assert await setup_stage(session) == "done"


async def test_a_key_that_opens_no_usable_model_is_refused(
    owner_client, gk_mock, use_model, model_lister, session
):
    # Neither of these models calls tools (the catalog says so).
    model_lister.models = ("gpt-3.5-turbo", "gpt-image-2")
    factory = use_model(FakeChatModel(["pong"]))
    r = await owner_client.post("/setup/model", data={"kind": "openai"})
    started = STATUS_URL.search(r.text)
    assert started
    gk_mock.set_status(started.group(3), "fulfilled")
    r = await owner_client.get(started.group(1))
    assert 'data-model-error="no_model"' in r.text
    assert t("setup.model.error.no_model", "en") in r.text
    assert factory.requested == [] and await setup_stage(session) == "model"


async def test_a_key_the_provider_refuses_to_list_is_told_so(
    owner_client, fulfilled_request, use_model, model_lister, session
):
    model_lister.error = "auth"
    use_model(FakeChatModel(["pong"]))
    r = await owner_client.get(fulfilled_request.status_url)
    assert 'data-model-error="auth"' in r.text and await setup_stage(session) == "model"


# --- a provider with a key: the vault form --------------------------------------------------


async def test_cloud_provider_creates_secret_request_with_binding(owner_client, gk_mock):
    r = await owner_client.post("/setup/model", data=KEYED_FORM)
    sent = SecretRequestCreate.model_validate_json(
        gk_mock.secret_requests.calls.last.request.content
    )
    assert sent.ref.startswith("provider:p-") and sent.ref.endswith(":api_key")
    assert (
        sent.bindings[0].host == "api.anthropic.com"
        and sent.bindings[0].location.name == "x-api-key"
    )
    assert 'src="/vault/r/' in r.text


@pytest.mark.parametrize("kind", ["anthropic", "openai", "google"])
async def test_each_cloud_kind_binds_its_key_to_its_own_host_and_header(
    owner_client, gk_mock, kind
):
    spec = PROVIDER_KINDS[kind]
    await owner_client.post("/setup/model", data={"kind": kind})
    sent = sent_request(gk_mock)
    assert len(sent.bindings) == 1
    binding = sent.bindings[0]
    assert binding.host == spec.default_base_url.split("//")[1].split("/")[0]
    assert binding.port is None
    assert binding.location == spec.key_location


async def test_the_address_of_a_cloud_provider_is_not_the_visitors_to_choose(
    owner_client, gk_mock, session
):
    await owner_client.post(
        "/setup/model", data=KEYED_FORM | {"base_url": "https://evil.example.net/v1"}
    )
    assert sent_request(gk_mock).bindings[0].host == "api.anthropic.com"
    (row,) = await provider_rows(session)
    assert row.base_url == PROVIDER_KINDS["anthropic"].default_base_url


async def test_the_provider_row_and_its_reference_are_recorded(owner_client, gk_mock, session):
    await owner_client.post("/setup/model", data=KEYED_FORM)
    (row,) = await provider_rows(session)
    assert re.fullmatch(r"p-[0-9a-f]{8}", row.id)
    assert row.kind == "anthropic"
    assert row.secret_ref == f"provider:{row.id}:api_key" == sent_request(gk_mock).ref
    assert await setup_stage(session) == "model"  # not finished: no key yet


async def test_the_secret_request_says_what_the_key_is_for_in_the_visitors_language(
    owner_client, gk_mock
):
    await owner_client.post("/setup/model", data=KEYED_FORM)
    assert sent_request(gk_mock).purpose == t("setup.model.secret_purpose", "en")
    await owner_client.post("/setup/model", data=KEYED_FORM, headers={"Accept-Language": "fr"})
    assert sent_request(gk_mock).purpose == t("setup.model.secret_purpose", "fr")
    # Before Domo can write, the form keeps the gatekeeper's own fixed texts: none are sent.
    assert (sent_request(gk_mock).language, sent_request(gk_mock).texts) == (None, None)


async def test_openai_compatible_with_a_key_binds_the_host_and_the_explicit_port(
    owner_client, gk_mock
):
    await owner_client.post(
        "/setup/model",
        data=LOCAL_FORM | {"base_url": "https://LLM.Example.com:8443/v1", "has_key": "on"},
    )
    binding = sent_request(gk_mock).bindings[0]
    assert (binding.host, binding.port) == ("llm.example.com", 8443)
    assert binding.location == PROVIDER_KINDS["openai_compatible"].key_location


async def test_the_wait_fragment_shows_the_vault_form_in_a_safe_frame(owner_client):
    r = await owner_client.post("/setup/model", data=KEYED_FORM)
    frame = re.search(r"<iframe\b[^>]*>", r.text)
    assert frame
    tag = frame.group(0)
    assert re.search(r'src="/vault/r/[^"]+"', tag)
    assert f'title="{t("setup.model.wait.frame_title", "en")}"' in tag
    assert 'referrerpolicy="no-referrer"' in tag
    assert "sandbox" not in tag  # it would block the form's post


async def test_the_wait_fragment_polls_the_status_every_two_seconds(owner_client):
    r = await owner_client.post("/setup/model", data=KEYED_FORM)
    assert re.search(r'hx-get="/setup/model/status/p-[0-9a-f]{8}/[^"/]+"', r.text)
    assert 'hx-trigger="every 2s"' in r.text and 'hx-swap="outerHTML"' in r.text


async def test_status_poll_pending_keeps_polling(owner_client, gk_mock, pending_request):
    r = await owner_client.get(pending_request.status_url)
    assert 'hx-trigger="every 2s"' in r.text


async def test_the_poll_does_not_send_the_vault_form_again(owner_client, pending_request):
    # Swapping the frame in again would reload the form while the owner is typing the key.
    r = await owner_client.get(pending_request.status_url)
    assert r.status_code == 200
    assert "<iframe" not in r.text
    assert f'hx-get="{pending_request.status_url}"' in r.text
    assert 'hx-swap="outerHTML"' in r.text
    assert "hx-target" not in r.text  # it replaces itself
    assert "HX-Retarget" not in r.headers


async def test_polling_uses_no_trigger_filter(owner_client, pending_request):
    # With htmx's allowEval off a filter such as [x] is silently always true.
    for html in (
        (await owner_client.post("/setup/model", data=KEYED_FORM)).text,
        (await owner_client.get(pending_request.status_url)).text,
    ):
        triggers = re.findall(r'hx-trigger="([^"]*)"', html)
        assert triggers and set(triggers) == {"every 2s"}


@pytest.mark.parametrize("status", ["expired", "superseded"])
async def test_an_expired_or_replaced_request_offers_a_new_entry(
    owner_client, gk_mock, pending_request, status
):
    gk_mock.set_status(pending_request.request_id, status)
    r = await owner_client.get(pending_request.status_url)
    assert r.status_code == 200
    assert t(f"setup.model.error.{status}", "en") in r.text
    assert f'data-model-error="{status}"' in r.text
    assert actions(r) == {"rekey", "discard"}
    assert "hx-trigger" not in r.text  # polling stops
    assert "<iframe" not in r.text
    # The poll replaces the whole step, not only itself.
    assert (r.headers["hx-retarget"], r.headers["hx-reswap"]) == ("#model-step", "innerHTML")


async def test_status_of_an_unknown_request_is_a_404(owner_client, pending_request):
    r = await owner_client.get(f"/setup/model/status/{pending_request.provider_id}/nope")
    assert r.status_code == 404


async def test_status_of_an_unknown_provider_is_a_404(owner_client, pending_request):
    r = await owner_client.get(f"/setup/model/status/p-ffffffff/{pending_request.request_id}")
    assert r.status_code == 404


async def test_status_of_a_request_of_another_provider_is_a_404(
    owner_client, gk_mock, pending_request
):
    other = await start_request(owner_client, gk_mock)  # another provider, another reference
    r = await owner_client.get(
        f"/setup/model/status/{pending_request.provider_id}/{other.request_id}"
    )
    assert r.status_code == 404


async def test_status_of_a_keyless_provider_is_a_404(owner_client, gk_mock, use_model, session):
    use_model(FakeChatModel([], fail_after=0, error_kind="unreachable"))
    await owner_client.post("/setup/model", data=LOCAL_FORM)
    (row,) = await provider_rows(session)
    r = await owner_client.get(f"/setup/model/status/{row.id}/req1")
    assert r.status_code == 404


async def test_a_key_removed_while_it_is_checked_is_a_404(
    app, owner_client, fulfilled_request, use_model, session
):
    # The key is removed (another tab) once its models are listed, before the question: the
    # account of the question finds it gone, and the step ends as for a provider dropped while
    # the model answers.
    @asynccontextmanager
    async def removing_the_key() -> AsyncGenerator[AsyncSession]:
        await session.execute(
            text("DELETE FROM providers WHERE id = :id"), {"id": fulfilled_request.provider_id}
        )
        yield session

    app.dependency_overrides[get_session_factory] = lambda: removing_the_key
    model = FakeChatModel(["pong"])
    use_model(model)

    r = await owner_client.get(fulfilled_request.status_url)

    assert r.status_code == 404
    assert model.calls == []
    assert await setup_stage(session) == "model"


async def test_fulfilled_then_successful_probe_completes_setup(
    app, owner_client, gk_mock, fulfilled_request, use_model, session
):
    use_model(FakeChatModel(["pong"]))
    r = await owner_client.get(fulfilled_request.status_url)
    assert r.headers["hx-redirect"] == "/chat"
    assert await setup_stage(session) == "done"
    assert "provider.added" in await audit_actions(session)


async def test_completion_registers_the_route_before_the_probe_and_records_it(
    app, owner_client, gk_mock, fulfilled_request, use_model, session
):
    model = FakeChatModel(["pong"])
    use_model(model)
    await owner_client.get(fulfilled_request.status_url)
    route = registered_route(gk_mock)
    assert route == ProviderRoute(
        id=fulfilled_request.provider_id,
        base_url=PROVIDER_KINDS["anthropic"].default_base_url,
        secret_ref=fulfilled_request.ref,
    )
    assert len(model.calls) == 1  # the probe
    assert (await setup_state_row(session)).model_configured_at is not None


async def test_the_audit_log_records_the_request_and_the_provider_without_any_secret(
    app, owner_client, gk_mock, fulfilled_request, use_model, session
):
    use_model(FakeChatModel(["pong"]))
    await owner_client.get(fulfilled_request.status_url)
    entries = await step_entries(session)
    assert [entry.action for entry in entries] == ["secret.requested", "provider.added"]
    requested, added = entries
    assert requested.details == {"ref": fulfilled_request.ref}
    assert added.details == {
        "provider_id": fulfilled_request.provider_id,
        "kind": "anthropic",
        "host": "api.anthropic.com",
        "models": 3,
    }
    assert requested.actor == added.actor and requested.actor.startswith("person:")
    chain = await verify_audit_chain(session)
    assert chain == ChainCheck(ok=True, count=len(await audit_entries(session)), first_bad_id=None)


async def test_the_other_attempts_of_the_setup_are_dropped_when_a_model_is_chosen(
    app, owner_client, gk_mock, fulfilled_request, use_model, session
):
    # Abandoned attempts must not outlive the setup: one provider is left, the chosen one.
    await owner_client.post("/setup/model", data=KEYED_FORM | {"kind": "openai"})
    assert len(await provider_rows(session)) == 2
    use_model(FakeChatModel(["pong"]))
    await owner_client.get(fulfilled_request.status_url)
    assert [row.id for row in await provider_rows(session)] == [fulfilled_request.provider_id]


async def test_the_house_speech_key_is_no_attempt_and_outlives_the_setup(
    app, owner_client, gk_mock, fulfilled_request, use_model, session
):
    # The upkeep records the house's speech service as soon as the owner exists, so during this
    # step; it then reads its list again only when asked: dropped here, it would never come back.
    session.add(
        Provider(
            id=HOME_SPEECH_ID,
            kind="openai_compatible",
            base_url="http://speech:8083/v1",
            secret_ref=None,
            given_by=await owner_id(session),
            is_general=False,
        )
    )
    await session.flush()
    session.add(ProviderModel(provider_id=HOME_SPEECH_ID, model_id="whisper-1"))
    await session.flush()
    use_model(FakeChatModel(["pong"]))
    await owner_client.get(fulfilled_request.status_url)
    rows = {row.id: row.is_general for row in await provider_rows(session)}
    assert rows == {fulfilled_request.provider_id: True, HOME_SPEECH_ID: False}
    assert await listed_models(session, HOME_SPEECH_ID) == ["whisper-1"]


class _ConcurrentFinish(FakeChatModel):
    """A model whose probe is overtaken: another request finishes the setup while it runs."""

    def __init__(self, session) -> None:
        super().__init__(["pong"])
        self._session = session

    async def stream(
        self,
        *,
        system: str,
        messages: Sequence[HistoryItem],
        max_output_tokens: int,
        tools: Sequence[ToolDefinition] = (),
        tool_choice: ToolChoice = "auto",
        think: bool = True,
    ) -> AsyncGenerator[ChatChunk | ToolCall | ChatUsage]:
        await self._session.execute(text("UPDATE setup_state SET model_configured_at = now()"))
        async for event in super().stream(
            system=system,
            messages=messages,
            max_output_tokens=max_output_tokens,
            tools=tools,
            tool_choice=tool_choice,
        ):
            yield event


async def test_finishing_twice_records_the_provider_once(
    app, owner_client, gk_mock, fulfilled_request, use_model, session
):
    model = _ConcurrentFinish(session)
    use_model(model)
    r = await owner_client.get(fulfilled_request.status_url)
    # Another request ended the step while the key was checked: nothing more to write or record.
    assert r.headers["hx-redirect"] == "/chat"
    assert "provider.added" not in await audit_actions(session)
    assert model.writer_calls() == []


async def test_invalid_key_shows_auth_error_and_allows_rekey(
    owner_client, gk_mock, fulfilled_request, use_model
):  # Review Focus 3
    use_model(FakeChatModel([], fail_after=0, error_kind="auth"))
    r = await owner_client.get(fulfilled_request.status_url)
    assert t("setup.model.error.auth", "en") in r.text
    r2 = await owner_client.post(f"/setup/model/{fulfilled_request.provider_id}/rekey")
    resent = SecretRequestCreate.model_validate_json(
        gk_mock.secret_requests.calls.last.request.content
    )
    assert resent.ref == fulfilled_request.ref and 'src="/vault/r/' in r2.text


@pytest.mark.parametrize("kind", ["auth", "unreachable", "rate_limited", "bad_request", "upstream"])
async def test_each_model_error_has_its_message_and_marks_its_root(
    owner_client, gk_mock, fulfilled_request, use_model, session, kind
):
    use_model(FakeChatModel([], fail_after=0, error_kind=kind))
    r = await owner_client.get(fulfilled_request.status_url)
    assert r.status_code == 200
    assert t(f"setup.model.error.{kind}", "en") in r.text
    assert len(re.findall(r"data-model-error=", r.text)) == 1
    assert f'data-model-error="{kind}"' in r.text
    # Two ways out for a provider with a key, and no more polling.
    assert actions(r) == {"rekey", "discard"}
    assert "hx-trigger" not in r.text
    # The setup is not over, and the provider is kept for the new key.
    assert await setup_stage(session) == "model"
    assert [row.id for row in await provider_rows(session)] == [fulfilled_request.provider_id]
    assert "provider.added" not in await audit_actions(session)


@pytest.mark.parametrize(
    "kind",
    [
        *get_args(ErrorKind),
        "gatekeeper",
        "insecure_transport",
        "binding_conflict",
        "expired",
        "superseded",
    ],
)
@pytest.mark.parametrize("lang", ["en", "fr"])
def test_every_failure_has_a_message_in_both_languages(kind, lang):
    # The fragment builds the key from the kind: a kind without a text would be a 500.
    assert t(f"setup.model.error.{kind}", lang)


async def test_the_model_error_is_in_the_language_of_the_visitor(
    owner_client, fulfilled_request, use_model
):
    use_model(FakeChatModel([], fail_after=0, error_kind="auth"))
    r = await owner_client.get(fulfilled_request.status_url, headers={"Accept-Language": "fr"})
    assert t("setup.model.error.auth", "fr") in visible(r)


async def test_rekey_asks_again_for_the_same_reference_and_binding(
    owner_client, gk_mock, fulfilled_request, use_model, session
):
    use_model(FakeChatModel([], fail_after=0, error_kind="auth"))
    await owner_client.get(fulfilled_request.status_url)
    first = sent_request(gk_mock)
    r = await owner_client.post(f"/setup/model/{fulfilled_request.provider_id}/rekey")
    second = sent_request(gk_mock)
    assert (second.ref, second.bindings) == (first.ref, first.bindings)
    assert 'src="/vault/r/' in r.text and 'hx-trigger="every 2s"' in r.text
    new_url = STATUS_URL.search(r.text)
    assert new_url and new_url.group(3) != fulfilled_request.request_id
    assert await audit_actions(session) == ["secret.requested", "secret.requested"]


async def test_rekey_replaces_the_form_that_is_still_open(owner_client, gk_mock, pending_request):
    r = await owner_client.post(f"/setup/model/{pending_request.provider_id}/rekey")
    assert 'src="/vault/r/' in r.text
    assert gk_mock.statuses[pending_request.request_id] == "superseded"
    old = await owner_client.get(pending_request.status_url)  # a tab that still polls it
    assert 'data-model-error="superseded"' in old.text


async def test_after_a_rekey_the_new_key_completes_the_setup(
    app, owner_client, gk_mock, fulfilled_request, use_model, session
):
    use_model(FakeChatModel([], fail_after=0, error_kind="auth"))
    await owner_client.get(fulfilled_request.status_url)
    r = await owner_client.post(f"/setup/model/{fulfilled_request.provider_id}/rekey")
    match = STATUS_URL.search(r.text)
    assert match
    gk_mock.set_status(match.group(3), "fulfilled")
    use_model(FakeChatModel(["pong"]))
    done = await owner_client.get(match.group(1))
    assert done.headers["hx-redirect"] == "/chat"
    assert await setup_stage(session) == "done"


async def test_rekey_of_an_unknown_provider_is_a_404(owner_client):
    assert (await owner_client.post("/setup/model/p-ffffffff/rekey")).status_code == 404


async def test_rekey_of_a_provider_without_a_key_is_a_404(
    owner_client, gk_mock, use_model, session
):
    use_model(FakeChatModel([], fail_after=0, error_kind="unreachable"))
    await owner_client.post("/setup/model", data=LOCAL_FORM)
    (row,) = await provider_rows(session)
    assert (await owner_client.post(f"/setup/model/{row.id}/rekey")).status_code == 404
    assert not gk_mock.secret_requests.called


async def test_discard_removes_the_provider_and_gives_the_form_back(
    owner_client, gk_mock, pending_request, session
):
    calls = gk_mock.secret_requests.call_count
    r = await owner_client.post(f"/setup/model/{pending_request.provider_id}/discard")
    assert r.status_code == 200
    assert await provider_rows(session) == []
    assert 'name="kind"' in r.text and 'name="model"' not in r.text
    assert gk_mock.secret_requests.call_count == calls  # the secret stays in the vault, unused
    assert not gk_mock.providers.called
    assert await setup_stage(session) == "model"


async def test_discard_of_an_unknown_provider_is_a_404(owner_client):
    assert (await owner_client.post("/setup/model/p-ffffffff/discard")).status_code == 404


async def test_discarded_provider_cannot_be_polled_any_more(owner_client, pending_request):
    await owner_client.post(f"/setup/model/{pending_request.provider_id}/discard")
    assert (await owner_client.get(pending_request.status_url)).status_code == 404


# --- a provider without a key -----------------------------------------------------------------


async def test_unreachable_local_model_shows_error(
    owner_client, gk_mock, use_model
):  # Review Focus 3
    use_model(FakeChatModel([], fail_after=0, error_kind="unreachable"))
    r = await owner_client.post(
        "/setup/model",
        data={
            "kind": "openai_compatible",
            "base_url": "http://192.168.1.10:11434/v1",
        },
    )
    assert t("setup.model.error.unreachable", "en") in r.text


async def test_openai_compatible_without_key_skips_secret_request(
    app, owner_client, gk_mock, use_model
):
    use_model(FakeChatModel(["pong"]))
    r = await owner_client.post(
        "/setup/model",
        data={
            "kind": "openai_compatible",
            "base_url": "http://192.168.1.10:11434/v1",
        },
    )
    assert not gk_mock.secret_requests.called
    assert r.headers["hx-redirect"] == "/chat"


async def test_a_keyless_provider_is_registered_without_a_secret_and_audited_once(
    app, owner_client, gk_mock, use_model, session
):
    use_model(FakeChatModel(["pong"]))
    await owner_client.post("/setup/model", data=LOCAL_FORM)
    (row,) = await provider_rows(session)
    assert registered_route(gk_mock) == ProviderRoute(
        id=row.id, base_url="http://192.168.1.10:11434/v1", secret_ref=None
    )
    assert row.secret_ref is None
    entries = await step_entries(session)
    assert [entry.action for entry in entries] == ["provider.added"]
    assert entries[0].details == {
        "provider_id": row.id,
        "kind": "openai_compatible",
        "host": "192.168.1.10",
        "models": 3,
    }
    assert await setup_stage(session) == "done"


async def test_a_keyless_provider_may_use_plain_http_to_any_host(app, owner_client, use_model):
    use_model(FakeChatModel(["pong"]))
    r = await owner_client.post(
        "/setup/model", data=LOCAL_FORM | {"base_url": "http://models.example.com/v1"}
    )
    assert r.headers["hx-redirect"] == "/chat"


async def test_a_keyless_error_offers_a_retry_and_another_provider_but_no_new_key(
    owner_client, gk_mock, use_model, session
):  # R11
    use_model(FakeChatModel([], fail_after=0, error_kind="unreachable"))
    r = await owner_client.post("/setup/model", data=LOCAL_FORM)
    assert actions(r) == {"retry", "discard"}
    assert 'data-model-error="unreachable"' in r.text
    assert await setup_stage(session) == "model"
    assert len(await provider_rows(session)) == 1  # kept, so that the retry has it


async def test_retry_runs_the_check_again_and_finishes_the_setup(
    app, owner_client, gk_mock, use_model, session
):
    use_model(FakeChatModel([], fail_after=0, error_kind="unreachable"))
    await owner_client.post("/setup/model", data=LOCAL_FORM)
    (row,) = await provider_rows(session)
    use_model(FakeChatModel(["pong"]))
    r = await owner_client.post(f"/setup/model/{row.id}/retry")
    assert r.headers["hx-redirect"] == "/chat"
    assert await setup_stage(session) == "done"
    assert await audit_actions(session) == ["provider.added"]


async def test_retry_that_fails_again_shows_the_error_again(owner_client, use_model, session):
    use_model(FakeChatModel([], fail_after=0, error_kind="unreachable"))
    await owner_client.post("/setup/model", data=LOCAL_FORM)
    (row,) = await provider_rows(session)
    r = await owner_client.post(f"/setup/model/{row.id}/retry")
    assert 'data-model-error="unreachable"' in r.text and actions(r) == {"retry", "discard"}


async def test_retry_of_a_provider_with_a_key_is_a_404(owner_client, fulfilled_request, use_model):
    use_model(FakeChatModel(["pong"]))  # a key that was never checked must not be skipped
    r = await owner_client.post(f"/setup/model/{fulfilled_request.provider_id}/retry")
    assert r.status_code == 404


async def test_retry_of_an_unknown_provider_is_a_404(owner_client):
    assert (await owner_client.post("/setup/model/p-ffffffff/retry")).status_code == 404


# --- what the visitor types -------------------------------------------------------------------


async def test_rejects_non_http_base_url(owner_client):
    r = await owner_client.post(
        "/setup/model",
        data={"kind": "openai_compatible", "base_url": "file:///etc/passwd"},
    )
    assert r.status_code == 400


@pytest.mark.parametrize(
    "base_url",
    [
        "",  # required for this kind
        "ftp://192.168.1.10/v1",
        "//192.168.1.10/v1",
        "192.168.1.10:11434/v1",
        "http://",
        "http:///v1",
        "http://user:pass@192.168.1.10/v1",
        "http://192.168.1.10:99999/v1",
        "http://192.168.1.10:abc/v1",
        "http://192.168.1.10/v1?x=1",
        "http://192.168.1.10/v1#frag",
        "http://192.168.1.10/ v1",
        "http://192.168.1.10/v1\n",
        "http://192.168.1.10/\tv1",
        "http://192.168.1.10/v1\x00",
        "http://192.168.1.10/v1\x7f",
        "https://exämple.com/v1",
    ],
)
async def test_rejects_an_invalid_server_address(owner_client, gk_mock, session, base_url):
    r = await owner_client.post(
        "/setup/model",
        data={"kind": "openai_compatible", "base_url": base_url, "has_key": "on"},
    )
    assert r.status_code == 400
    assert t("setup.model.error.base_url", "en") in r.text
    assert 'name="kind"' in r.text  # the form again, to correct it
    assert not gk_mock.secret_requests.called and not gk_mock.providers.called
    assert await provider_rows(session) == []


async def test_the_form_comes_back_with_what_was_typed(owner_client):
    r = await owner_client.post(
        "/setup/model",
        data={"kind": "openai_compatible", "base_url": "ftp://host.example.com"},
    )
    assert 'value="ftp://host.example.com"' in r.text
    assert re.search(r'<option value="openai_compatible"[^>]*\bselected\b', r.text)


@pytest.mark.parametrize("kind", ["", "mystery", "Anthropic", "openai_compatible2"])
async def test_rejects_an_unknown_kind(owner_client, session, kind):
    r = await owner_client.post("/setup/model", data={"kind": kind})
    assert r.status_code == 400 and t("setup.model.error.kind", "en") in r.text
    assert await provider_rows(session) == []


async def test_rejects_a_missing_kind(owner_client):
    assert (await owner_client.post("/setup/model", data={})).status_code == 400


async def keyless_without_a_list(owner_client, use_model, model_lister):
    """A server of the household's own that gives no list of its models: the step asks the name."""
    use_model(FakeChatModel(["pong"]))
    model_lister.models = ()
    r = await owner_client.post("/setup/model", data=LOCAL_FORM)
    return r


@pytest.mark.parametrize("model", ["", "   ", "a\nb", "a\x00b", "x" * 201])
async def test_rejects_an_unusable_model_name(
    owner_client, gk_mock, use_model, model_lister, session, model
):
    await keyless_without_a_list(owner_client, use_model, model_lister)
    (row,) = await provider_rows(session)
    r = await owner_client.post(f"/setup/model/{row.id}/name", data={"model": model})
    assert r.status_code == 400 and t("setup.model.error.model", "en", max="200") in r.text
    assert "data-model-name" in r.text
    assert await setup_stage(session) == "model"


async def test_the_model_name_is_trimmed_and_recorded_as_the_models_of_the_key(
    app, owner_client, use_model, model_lister, session
):
    factory = use_model(FakeChatModel(["pong"]))
    model_lister.models = ()
    await owner_client.post("/setup/model", data=LOCAL_FORM)
    (row,) = await provider_rows(session)
    r = await owner_client.post(f"/setup/model/{row.id}/name", data={"model": "  some-model \n"})
    assert r.headers["hx-redirect"] == "/chat"
    assert set(factory.requested) == {(row.id, "some-model")}
    assert await listed_models(session, row.id) == ["some-model"]


async def test_a_host_that_cannot_be_bound_is_refused_for_a_provider_with_a_key(
    owner_client, gk_mock, session
):
    # A binding names a bare host name or IPv4 address: an IPv6 literal cannot be bound.
    r = await owner_client.post(
        "/setup/model",
        data=LOCAL_FORM | {"base_url": "http://[::1]:11434/v1", "has_key": "on"},
    )
    assert r.status_code == 400 and t("setup.model.error.base_url", "en") in r.text
    assert not gk_mock.secret_requests.called and await provider_rows(session) == []


# --- R23: a key never crosses the network in cleartext ----------------------------------------


@pytest.mark.parametrize(
    "form",
    [
        LOCAL_FORM | {"base_url": "http://api.example.com/v1", "has_key": "on"},
        LOCAL_FORM | {"base_url": "http://8.8.8.8:8080/v1", "has_key": "on"},
        LOCAL_FORM | {"base_url": "http://localhost.example.com/v1", "has_key": "on"},
    ],
)
async def test_a_key_is_refused_over_plain_http_to_a_public_host(
    owner_client, gk_mock, session, form
):
    r = await owner_client.post("/setup/model", data=form)
    assert r.status_code == 400
    assert t("setup.model.error.insecure_transport", "en") in r.text
    assert 'name="kind"' in r.text
    # Nothing was asked of the gatekeeper and nothing was recorded.
    assert not gk_mock.secret_requests.called and not gk_mock.providers.called
    assert await provider_rows(session) == []
    assert await audit_actions(session) == []


@pytest.mark.parametrize(
    "base_url",
    [
        "http://192.168.1.10:11434/v1",
        "http://10.0.0.5/v1",
        "http://localhost:8080/v1",
        "http://fake-llm:9000/v1",
        "https://api.example.com/v1",
    ],
)
async def test_a_key_is_accepted_over_http_to_a_local_host_and_over_https_anywhere(
    owner_client, gk_mock, base_url
):
    r = await owner_client.post(
        "/setup/model", data=LOCAL_FORM | {"base_url": base_url, "has_key": "on"}
    )
    assert r.status_code == 200 and 'src="/vault/r/' in r.text
    assert gk_mock.secret_requests.called


async def test_the_gatekeeper_stays_the_authority_on_cleartext(
    owner_client, gk_mock, fulfilled_request, use_model, session
):
    gk_mock.providers.mock(return_value=httpx.Response(409, json={"error": "insecure_transport"}))
    use_model(FakeChatModel(["pong"]))
    r = await owner_client.get(fulfilled_request.status_url)
    assert t("setup.model.error.insecure_transport", "en") in r.text
    assert 'data-model-error="insecure_transport"' in r.text
    assert actions(r) == {"discard"}  # a new key would not help
    assert await setup_stage(session) == "model"


# --- the gatekeeper's errors are logged (task 14) ------------------------------------------------


def gatekeeper_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    """What the core logged about the gatekeeper's failures."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "domo_core.gatekeeper_client" and record.levelname == "WARNING"
    ]


async def test_gatekeeper_errors_are_logged_without_secrets(owner_client, gk_mock, caplog):
    # The brief's form would be refused before the gatekeeper is asked (a key over plain http to
    # a host that is not local): a server of one's own, without a key, reaches register_provider.
    gk_mock.fail_next("register_provider", status=409, code="insecure_transport")
    with caplog.at_level(logging.INFO):
        r = await owner_client.post("/setup/model", data=LOCAL_FORM)
    assert 'data-model-error="insecure_transport"' in r.text
    assert "insecure_transport" in caplog.text and CORE_TOKEN not in caplog.text
    assert gatekeeper_warnings(caplog) == [
        "The gatekeeper call PUT /internal/providers/{id} failed: status 409, "
        "code insecure_transport"
    ]


async def test_a_failure_to_open_the_key_form_is_logged(owner_client, gk_mock, caplog):
    gk_mock.fail_next("create_secret_request", status=500, code="internal_error")
    with caplog.at_level(logging.INFO):
        r = await owner_client.post("/setup/model", data=KEYED_FORM)
    assert 'data-model-error="gatekeeper"' in r.text
    assert gatekeeper_warnings(caplog) == [
        "The gatekeeper call POST /internal/secret-requests failed: status 500, code internal_error"
    ]


async def test_a_failure_of_the_poll_is_logged_without_the_request(
    owner_client, gk_mock, pending_request, caplog
):
    gk_mock.fail_next("get_secret_request", status=500, code="internal_error")
    with caplog.at_level(logging.INFO):
        await owner_client.get(pending_request.status_url)
    assert gatekeeper_warnings(caplog) == [
        "The gatekeeper call GET /internal/secret-requests/{id} failed: status 500, "
        "code internal_error"
    ]
    assert pending_request.request_id not in "".join(gatekeeper_warnings(caplog))
    assert CORE_TOKEN not in caplog.text and "/vault/r/" not in caplog.text


# --- the gatekeeper's errors are shown, never a 500 --------------------------------------------


async def test_a_secret_with_other_destinations_is_explained_and_leaves_nothing_behind(
    owner_client, gk_mock, session
):
    gk_mock.secret_requests.mock(
        return_value=httpx.Response(409, json={"error": "binding_conflict"})
    )
    r = await owner_client.post("/setup/model", data=KEYED_FORM)
    assert r.status_code == 200
    assert t("setup.model.error.binding_conflict", "en") in r.text
    assert 'data-model-error="binding_conflict"' in r.text
    assert actions(r) == {"discard"}
    assert await provider_rows(session) == []
    assert await audit_actions(session) == []


async def test_a_gatekeeper_failure_when_asking_for_the_key_is_explained(
    owner_client, gk_mock, session
):
    gk_mock.secret_requests.mock(return_value=httpx.Response(500, json={"error": "internal_error"}))
    r = await owner_client.post("/setup/model", data=KEYED_FORM)
    assert r.status_code == 200
    assert t("setup.model.error.gatekeeper", "en") in r.text
    assert 'data-model-error="gatekeeper"' in r.text and actions(r) == {"discard"}
    assert await provider_rows(session) == []
    assert await audit_actions(session) == []


async def test_an_unreachable_gatekeeper_is_explained(owner_client, gk_mock, session):
    gk_mock.secret_requests.mock(side_effect=httpx.ConnectError("refused"))
    r = await owner_client.post("/setup/model", data=KEYED_FORM)
    assert r.status_code == 200 and t("setup.model.error.gatekeeper", "en") in r.text
    assert await provider_rows(session) == []


async def test_a_gatekeeper_failure_when_polling_is_explained_and_stops_the_polling(
    owner_client, gk_mock, pending_request
):
    gk_mock.request_views.mock(return_value=httpx.Response(500, json={"error": "internal_error"}))
    r = await owner_client.get(pending_request.status_url)
    assert r.status_code == 200
    assert t("setup.model.error.gatekeeper", "en") in r.text
    assert actions(r) == {"rekey", "discard"} and "hx-trigger" not in r.text
    assert r.headers["hx-retarget"] == "#model-step"


async def test_a_refused_route_is_explained(
    owner_client, gk_mock, fulfilled_request, use_model, session
):
    gk_mock.providers.mock(return_value=httpx.Response(409, json={"error": "binding_conflict"}))
    use_model(FakeChatModel(["pong"]))
    r = await owner_client.get(fulfilled_request.status_url)
    assert r.status_code == 200 and t("setup.model.error.binding_conflict", "en") in r.text
    assert actions(r) == {"discard"} and await setup_stage(session) == "model"


async def test_a_missing_secret_when_registering_the_route_is_explained(
    owner_client, gk_mock, fulfilled_request, use_model
):
    gk_mock.providers.mock(return_value=httpx.Response(404, json={"error": "unknown_secret"}))
    use_model(FakeChatModel(["pong"]))
    r = await owner_client.get(fulfilled_request.status_url)
    assert r.status_code == 200 and t("setup.model.error.gatekeeper", "en") in r.text
    assert actions(r) == {"rekey", "discard"}


# --- the step is closed once the setup is done ------------------------------------------------


async def test_the_model_pages_are_closed_once_the_setup_is_done(
    app, owner_client, fulfilled_request, use_model
):
    use_model(FakeChatModel(["pong"]))
    await owner_client.get(fulfilled_request.status_url)
    r = await owner_client.get("/setup/model")
    assert (r.status_code, r.headers["location"]) == (303, "/chat")
    r = await owner_client.post("/setup/model", data=KEYED_FORM, headers={"HX-Request": "true"})
    assert (r.status_code, r.headers["hx-redirect"]) == (401, "/chat")


# --- the step ends as soon as the key works (spec 5.1) -------------------------------------------


async def written_rows(session) -> int:
    """How many texts Domo wrote, for anyone."""
    return (await session.execute(select(func.count()).select_from(WrittenText))).scalar_one()


async def test_the_install_ends_as_soon_as_the_key_works(
    owner_client, fulfilled_request, use_model, session
):  # scenario 1
    model = FakeChatModel(["pong"])
    use_model(model)
    r = await owner_client.get(fulfilled_request.status_url)

    # The key works: the chat opens at once, and nothing was written for it.
    assert r.headers["hx-redirect"] == "/chat"
    assert await setup_stage(session) == "done"
    assert model.writer_calls() == [] and model.checker_calls() == []
    assert await written_rows(session) == 0
    assert "texts.written" not in [entry.action for entry in await audit_entries(session)]


async def test_the_house_language_is_the_owners_browser_language(
    owner_client, use_model, session
):  # scenario 2
    model = FakeChatModel(["pong"])
    use_model(model)
    r = await owner_client.post("/setup/model", data=LOCAL_FORM, headers={"Accept-Language": "de"})

    # Whatever it is, without a word written for it: the upkeep writes a language without a
    # catalog of its own, once the installation is over.
    assert r.headers["hx-redirect"] == "/chat"
    assert (await load_policy_settings(session)).house_language == "de"
    assert model.writer_calls() == [] and await written_rows(session) == 0
    changed = [entry.details for entry in await step_entries(session)]
    assert {"key": "house.language", "old": None, "new": "de"} in changed


async def test_the_page_opened_again_shows_the_form(owner_client):
    page = await owner_client.get("/setup/model")
    assert page.status_code == 200 and 'name="kind"' in page.text
    assert "hx-trigger" not in page.text
