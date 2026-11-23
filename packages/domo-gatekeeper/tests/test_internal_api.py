import hmac
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from domo_common.gatekeeper_api import (
    FORM_TEXT_MAX,
    Binding,
    HeaderLocation,
    ProviderRoute,
    SecretMetadata,
    SecretRequestCreate,
    SecretRequestView,
)
from domo_gatekeeper import internal_api
from domo_gatekeeper.internal_api import create_internal_app
from domo_gatekeeper.providers import ProviderRegistry
from domo_gatekeeper.relay import RelayResponse
from domo_gatekeeper.secret_requests import SecretRequestStore
from domo_gatekeeper.vault import Vault
from fastapi import FastAPI
from pydantic import TypeAdapter

CORE_TOKEN = "core-token-for-tests"
BEARER = {"authorization": f"Bearer {CORE_TOKEN}"}
KEY = "real-key"  # what the route_p1 fixture stores in the vault

B = Binding(host="api.example.com", location=HeaderLocation(kind="header", name="x-api-key"))
EVIL = Binding(host="evil.example.net", location=HeaderLocation(kind="header", name="x-api-key"))
REQ = SecretRequestCreate(
    ref="provider:p1:api_key", purpose="API key for provider p1", bindings=[B]
)
MESSAGES = "https://api.example.com/v1/messages"


# --- Fixtures -----------------------------------------------------------------------------------


def client_for(
    app: FastAPI, headers: dict[str, str] | None = None, *, raise_app_exceptions: bool = True
) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions)
    return httpx.AsyncClient(
        transport=transport, base_url="http://gatekeeper.test", headers=headers
    )


@pytest.fixture
def internal_app(
    vault: Vault,
    store: SecretRequestStore,
    registry: ProviderRegistry,
    http_client: httpx.AsyncClient,
) -> FastAPI:
    return create_internal_app(
        vault=vault,
        requests=store,
        providers=registry,
        http_client=http_client,
        core_token=CORE_TOKEN,
    )


@pytest.fixture
async def internal_client(internal_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """A client with no credentials."""
    async with client_for(internal_app) as client:
        yield client


@pytest.fixture
async def authed_client(internal_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """A client that sends the core's token as a bearer token."""
    async with client_for(internal_app, BEARER) as client:
        yield client


@pytest.fixture
def registered_p1(route_p1: ProviderRoute, registry: ProviderRegistry, vault: Vault):
    registry.register(route_p1, vault)
    return route_p1


def insert_route(db: Path, route_id: str, base_url: str, secret_ref: str | None) -> None:
    """Store a route without the registry's checks, as a tampered or older database would."""
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO providers (id, base_url, secret_ref) VALUES (?, ?, ?)",
            (route_id, base_url, secret_ref),
        )


def token_of(form_path: str) -> str:
    return form_path.rsplit("/", 1)[1]


# --- Health and authentication ------------------------------------------------------------------


async def test_health_is_open(internal_client):
    assert (await internal_client.get("/internal/health")).json() == {"status": "ok"}


async def test_health_answers_whatever_credentials_are_sent(internal_client):
    r = await internal_client.get("/internal/health", headers={"authorization": "Bearer wrong"})
    assert r.status_code == 200


async def test_requires_core_token(internal_client):
    assert (await internal_client.get("/internal/secrets")).status_code == 401
    r = await internal_client.get("/internal/secrets", headers={"authorization": "Bearer wrong"})
    assert r.status_code == 401


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/internal/secrets"),
        ("POST", "/internal/secret-requests"),
        ("GET", "/internal/secret-requests/some-id"),
        ("PUT", "/internal/providers/p1"),
        ("GET", "/internal/health/"),  # only the exact health path is open
        ("GET", "/internal/does-not-exist"),  # denied before it is found missing
        ("GET", "/"),
        ("POST", "/llm/p1/v1/messages"),
        ("GET", "/llm/nope/x"),  # an unknown provider is not revealed without the token
    ],
)
async def test_everything_but_health_is_closed_to_callers_without_the_token(
    internal_client, method, path
):
    r = await internal_client.request(method, path)
    assert r.status_code == 401
    assert r.json() == {"error": "unauthorized"}
    assert r.headers["www-authenticate"] == "Bearer"


async def test_authentication_comes_before_the_body_is_read(internal_client):
    r = await internal_client.post(
        "/internal/secret-requests", content=b"{not json", headers={"content-type": "text/plain"}
    )
    assert r.status_code == 401


@pytest.mark.parametrize("header", ["x-api-key", "x-goog-api-key"])
async def test_internal_routes_only_take_the_token_as_a_bearer(internal_client, header):
    r = await internal_client.get("/internal/secrets", headers={header: CORE_TOKEN})
    assert r.status_code == 401


@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER"])
async def test_the_bearer_scheme_is_case_insensitive(internal_client, scheme):
    r = await internal_client.get(
        "/internal/secrets", headers={"authorization": f"{scheme} {CORE_TOKEN}"}
    )
    assert r.status_code == 200


@pytest.mark.parametrize(
    "value",
    [
        CORE_TOKEN,  # no scheme
        f"Basic {CORE_TOKEN}",
        f"Token {CORE_TOKEN}",
        "Bearer",
        "Bearer ",
        f"Bearer {CORE_TOKEN}x",
        f"Bearer {CORE_TOKEN[:-1]}",
        f"Bearer {CORE_TOKEN.upper()}",
        f"Bearer {CORE_TOKEN} extra",
    ],
)
async def test_any_other_authorization_value_is_refused(internal_client, value):
    r = await internal_client.get("/internal/secrets", headers={"authorization": value})
    assert r.status_code == 401


async def test_the_token_is_compared_with_compare_digest(internal_client, monkeypatch):
    calls: list[tuple[bytes, bytes]] = []
    real = hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(hmac, "compare_digest", spy)
    await internal_client.get("/internal/secrets", headers=BEARER)
    assert calls == [(CORE_TOKEN.encode(), CORE_TOKEN.encode())]


def test_an_empty_core_token_is_refused(vault, store, registry, http_client):
    with pytest.raises(ValueError, match="token"):
        create_internal_app(
            vault=vault, requests=store, providers=registry, http_client=http_client, core_token=""
        )


# --- Secret requests ----------------------------------------------------------------------------


async def test_create_request_returns_form_path(authed_client):
    r = await authed_client.post("/internal/secret-requests", json=REQ.model_dump(mode="json"))
    assert r.status_code == 201 and r.json()["form_path"].startswith("/vault/r/")


async def test_the_created_request_is_a_pending_view(authed_client):
    r = await authed_client.post("/internal/secret-requests", json=REQ.model_dump(mode="json"))
    view = SecretRequestView.model_validate(r.json())
    assert view.ref == REQ.ref and view.status == "pending" and view.form_path is not None


async def test_conflicting_binding_returns_409(authed_client, vault):
    vault.put(REQ.ref, "v", purpose="p", bindings=[EVIL])
    r = await authed_client.post("/internal/secret-requests", json=REQ.model_dump(mode="json"))
    assert r.status_code == 409
    assert r.json() == {"error": "binding_conflict"}


async def test_the_same_bindings_in_another_order_are_a_rotation_not_a_conflict(
    authed_client, vault
):
    vault.put(REQ.ref, "v", purpose="p", bindings=[B, EVIL])
    body = REQ.model_copy(update={"bindings": [EVIL, B]}).model_dump(mode="json")
    r = await authed_client.post("/internal/secret-requests", json=body)
    assert r.status_code == 201


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"ref": "Not A Ref", "purpose": "p", "bindings": [{"host": "api.example.com"}]},
        {"ref": "provider:p1:api_key", "purpose": "p", "bindings": []},
        {**REQ.model_dump(mode="json"), "value": "smuggled-secret-value"},  # extra field
    ],
)
async def test_an_invalid_request_is_refused_without_echoing_it(authed_client, body):
    r = await authed_client.post("/internal/secret-requests", json=body)
    assert r.status_code == 422
    assert r.json() == {"error": "invalid_request"}
    assert "smuggled" not in r.text


def with_texts(**texts: str) -> dict[str, object]:
    """The body of ``REQ`` with the texts of its form, as the core sends them once installed."""
    return {**REQ.model_dump(mode="json"), "language": "en", "texts": texts}


def no_request_was_made(store: SecretRequestStore) -> bool:
    with sqlite3.connect(store._db_path) as conn:  # pyright: ignore[reportPrivateUsage]
        return conn.execute("SELECT count(*) FROM secret_requests").fetchone()[0] == 0


@pytest.mark.parametrize(
    "text",
    [
        '<script>alert("x")</script>',
        '<a href="https://evil.example">Type your key here</a>',
        "Your key &gt; here >",
        "Two" + chr(10) + "lines",
        "A bell" + chr(7),
    ],
)
async def test_the_gatekeeper_refuses_markup_in_form_texts(authed_client, store, text):
    body = with_texts(**{"vault.form.title": "Your key", "vault.form.privacy": text})
    r = await authed_client.post("/internal/secret-requests", json=body)
    assert r.status_code == 422
    # Refused cleanly: nothing echoed, no form opened.
    assert r.json() == {"error": "invalid_request"}
    assert no_request_was_made(store)


@pytest.mark.parametrize("key", ["vault.form.destination", "chat.send", "vault.form.secret"])
async def test_an_unknown_text_key_is_refused(authed_client, store, key):
    r = await authed_client.post("/internal/secret-requests", json=with_texts(**{key: "words"}))
    assert r.status_code == 422 and r.json() == {"error": "invalid_request"}
    assert no_request_was_made(store)


@pytest.mark.parametrize(
    "purpose", ["<b>API key</b>", "API key: https://evil.example", "API key" + chr(10) + "more"]
)
async def test_with_texts_the_purpose_is_held_to_the_same_rule(authed_client, store, purpose):
    body = {**with_texts(**{"vault.form.title": "Your key"}), "purpose": purpose}
    r = await authed_client.post("/internal/secret-requests", json=body)
    assert r.status_code == 422 and r.json() == {"error": "invalid_request"}
    assert no_request_was_made(store)


async def test_a_text_too_long_is_refused(authed_client, store):
    long = "x" * (FORM_TEXT_MAX + 1)
    r = await authed_client.post(
        "/internal/secret-requests", json=with_texts(**{"vault.form.title": long})
    )
    assert r.status_code == 422 and long not in r.text
    assert no_request_was_made(store)


async def test_a_body_that_is_not_json_is_refused(authed_client):
    r = await authed_client.post(
        "/internal/secret-requests",
        content=b"{not json",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 422
    assert r.json() == {"error": "invalid_request"}


async def test_get_request_reports_its_state_without_the_form_path(authed_client):
    created = (
        await authed_client.post("/internal/secret-requests", json=REQ.model_dump(mode="json"))
    ).json()
    r = await authed_client.get(f"/internal/secret-requests/{created['id']}")
    assert r.status_code == 200
    assert r.json()["status"] == "pending" and r.json()["form_path"] is None


async def test_get_request_follows_the_owner_filling_the_form(authed_client, store, vault):
    created = (
        await authed_client.post("/internal/secret-requests", json=REQ.model_dump(mode="json"))
    ).json()
    store.fulfil(token_of(created["form_path"]), "typed-by-the-owner", vault)
    r = await authed_client.get(f"/internal/secret-requests/{created['id']}")
    assert r.json()["status"] == "fulfilled"
    assert "typed-by-the-owner" not in r.text


async def test_get_unknown_request_returns_404(authed_client):
    r = await authed_client.get("/internal/secret-requests/nope")
    assert r.status_code == 404
    assert r.json() == {"error": "not_found"}


# --- Secret metadata ----------------------------------------------------------------------------


async def test_no_secrets_is_an_empty_list(authed_client):
    assert (await authed_client.get("/internal/secrets")).json() == []


async def test_secrets_are_listed_as_metadata(authed_client, vault):
    vault.put("provider:p1:api_key", "real-key-value", purpose="p", bindings=[B])
    r = await authed_client.get("/internal/secrets")
    listed = TypeAdapter(list[SecretMetadata]).validate_python(r.json())
    assert [(s.ref, s.purpose, s.bindings) for s in listed] == [("provider:p1:api_key", "p", [B])]


async def test_secret_values_never_in_internal_responses(authed_client, vault):
    vault.put("provider:p1:api_key", "real-key-value", purpose="p", bindings=[B])
    assert "real-key-value" not in (await authed_client.get("/internal/secrets")).text


async def test_secret_values_never_in_request_responses(authed_client, store, vault):
    created = await authed_client.post(
        "/internal/secret-requests", json=REQ.model_dump(mode="json")
    )
    store.fulfil(token_of(created.json()["form_path"]), "typed-by-the-owner", vault)
    state = await authed_client.get(f"/internal/secret-requests/{created.json()['id']}")
    listing = await authed_client.get("/internal/secrets")
    assert "typed-by-the-owner" not in created.text + state.text + listing.text


# --- Provider registration ----------------------------------------------------------------------


async def test_register_provider(authed_client, route_p1, registry):
    r = await authed_client.put("/internal/providers/p1", json=route_p1.model_dump(mode="json"))
    assert r.status_code == 204 and r.content == b""
    assert registry.get("p1") == route_p1


async def test_register_a_keyless_provider(authed_client, registry):
    route = ProviderRoute(id="local", base_url="http://fake-llm:9000/v1", secret_ref=None)
    r = await authed_client.put("/internal/providers/local", json=route.model_dump(mode="json"))
    assert r.status_code == 204 and registry.get("local") == route


async def test_registering_again_replaces_the_route(authed_client, route_p1, registry):
    await authed_client.put("/internal/providers/p1", json=route_p1.model_dump(mode="json"))
    moved = route_p1.model_copy(update={"base_url": "https://api.example.com/v2"})
    r = await authed_client.put("/internal/providers/p1", json=moved.model_dump(mode="json"))
    assert r.status_code == 204 and registry.get("p1") == moved


async def test_the_path_id_must_equal_the_body_id(authed_client, route_p1, registry):
    r = await authed_client.put("/internal/providers/other", json=route_p1.model_dump(mode="json"))
    assert r.status_code == 400
    assert r.json() == {"error": "id_mismatch"}
    assert registry.get("p1") is None and registry.get("other") is None


async def test_a_route_naming_an_unknown_secret_returns_404(authed_client, registry):
    route = ProviderRoute(id="p2", base_url="https://api.example.com", secret_ref="provider:p2:key")
    r = await authed_client.put("/internal/providers/p2", json=route.model_dump(mode="json"))
    assert r.status_code == 404
    assert r.json() == {"error": "unknown_secret"}
    assert registry.get("p2") is None


async def test_a_route_to_a_host_the_secret_is_not_bound_to_returns_409(
    authed_client, route_p1, registry
):
    elsewhere = route_p1.model_copy(update={"base_url": "https://evil.example.net"})
    r = await authed_client.put("/internal/providers/p1", json=elsewhere.model_dump(mode="json"))
    assert r.status_code == 409
    assert r.json() == {"error": "binding_conflict"}
    assert registry.get("p1") is None


async def test_a_keyed_route_over_plain_http_to_a_public_host_returns_409(
    authed_client, route_p1, registry
):
    cleartext = route_p1.model_copy(update={"base_url": "http://api.example.com"})
    r = await authed_client.put("/internal/providers/p1", json=cleartext.model_dump(mode="json"))
    assert r.status_code == 409
    assert r.json() == {"error": "insecure_transport"}
    assert registry.get("p1") is None


async def test_an_invalid_route_is_refused(authed_client):
    r = await authed_client.put(
        "/internal/providers/P1", json={"id": "P1", "base_url": "ftp://x", "secret_ref": None}
    )
    assert r.status_code == 422
    assert r.json() == {"error": "invalid_request"}


# --- Relay: authentication, injection, pass-through ---------------------------------------------


async def test_llm_route_accepts_token_in_x_api_key(internal_client, respx_mock, registered_p1):
    respx_mock.post("https://api.example.com/v1/messages").respond(200, json={})
    r = await internal_client.post(
        "/llm/p1/v1/messages", headers={"x-api-key": CORE_TOKEN}, content=b"{}"
    )
    assert r.status_code == 200


@pytest.mark.parametrize(
    "headers",
    [
        {"authorization": f"Bearer {CORE_TOKEN}"},
        {"x-api-key": CORE_TOKEN},
        {"x-goog-api-key": CORE_TOKEN},
        {"authorization": "Bearer wrong", "x-api-key": CORE_TOKEN},  # one valid token is enough
    ],
)
async def test_llm_route_accepts_the_token_from_any_of_the_three_headers(
    internal_client, respx_mock, registered_p1, headers
):
    respx_mock.post(MESSAGES).respond(200, json={})
    r = await internal_client.post("/llm/p1/v1/messages", headers=headers, content=b"{}")
    assert r.status_code == 200


@pytest.mark.parametrize("header", ["authorization", "x-api-key", "x-goog-api-key"])
async def test_llm_route_rejects_a_wrong_token_in_each_header(
    internal_client, registered_p1, header
):
    value = "Bearer wrong" if header == "authorization" else "wrong"
    r = await internal_client.post("/llm/p1/v1/messages", headers={header: value}, content=b"{}")
    assert r.status_code == 401


async def test_llm_route_rejects_unknown_provider(internal_client):
    r = await internal_client.post("/llm/nope/x", headers={"authorization": f"Bearer {CORE_TOKEN}"})
    assert r.status_code == 404
    assert r.json() == {"error": "unknown_provider"}


async def test_the_real_key_replaces_the_core_token_on_the_way_out(
    internal_client, respx_mock, registered_p1
):
    upstream = respx_mock.post(MESSAGES).respond(200, json={})
    await internal_client.post(
        "/llm/p1/v1/messages",
        headers={
            "x-api-key": CORE_TOKEN,
            "authorization": f"Bearer {CORE_TOKEN}",
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        content=b'{"model": "m"}',
    )
    sent = upstream.calls.last.request
    assert sent.headers.get_list("x-api-key") == [KEY]
    assert "authorization" not in sent.headers
    assert sent.headers["anthropic-version"] == "2023-06-01"
    assert sent.headers["content-type"] == "application/json"
    assert sent.content == b'{"model": "m"}'
    assert CORE_TOKEN not in str(sent.headers)


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE"])
async def test_the_method_is_relayed(internal_client, respx_mock, registered_p1, method):
    upstream = respx_mock.route(host="api.example.com").respond(200)
    await internal_client.request(method, "/llm/p1/v1/models", headers=BEARER)
    assert upstream.calls.last.request.method == method


async def test_the_query_is_relayed(internal_client, respx_mock, registered_p1):
    upstream = respx_mock.route(host="api.example.com").respond(200)
    await internal_client.get("/llm/p1/v1/models?alt=sse&page=2", headers=BEARER)
    assert upstream.calls.last.request.url.query == b"alt=sse&page=2"


async def test_the_path_is_relayed_as_it_was_sent(internal_client, respx_mock, registered_p1):
    upstream = respx_mock.route(host="api.example.com").respond(200)
    await internal_client.get("/llm/p1/v1beta/models/a%20b:generate", headers=BEARER)
    assert upstream.calls.last.request.url.raw_path == b"/v1beta/models/a%20b:generate"


async def test_an_empty_path_reaches_the_base_url(internal_client, respx_mock, registered_p1):
    upstream = respx_mock.route(host="api.example.com").respond(200)
    await internal_client.get("/llm/p1/", headers=BEARER)
    assert upstream.calls.last.request.url.raw_path == b"/"


async def test_the_status_and_headers_of_the_provider_are_passed_through(
    internal_client, respx_mock, registered_p1
):
    respx_mock.post(MESSAGES).respond(
        429, headers={"retry-after": "7", "x-request-id": "req_1"}, json={"error": "slow down"}
    )
    r = await internal_client.post("/llm/p1/v1/messages", headers=BEARER, content=b"{}")
    assert r.status_code == 429
    assert r.headers["retry-after"] == "7" and r.headers["x-request-id"] == "req_1"
    assert r.json() == {"error": "slow down"}


async def test_the_key_is_masked_in_what_the_provider_echoes(
    internal_client, respx_mock, registered_p1
):
    def echo(request: httpx.Request) -> httpx.Response:
        key = request.headers["x-api-key"]
        return httpx.Response(401, headers={"x-echo": key}, json={"message": f"bad key {key}"})

    respx_mock.post(MESSAGES).mock(side_effect=echo)
    r = await internal_client.post("/llm/p1/v1/messages", headers=BEARER, content=b"{}")
    assert r.status_code == 401
    assert KEY not in r.text and KEY not in str(r.headers)
    assert "[REDACTED]" in r.text and r.headers["x-echo"] == "[REDACTED]"


async def test_a_streamed_answer_is_relayed_whole(internal_client, respx_mock, registered_p1):
    events = [f"data: {n}\n\n".encode() for n in range(50)]

    async def stream() -> AsyncIterator[bytes]:
        for event in events:
            yield event

    respx_mock.post(MESSAGES).respond(
        200, headers={"content-type": "text/event-stream"}, content=stream()
    )
    r = await internal_client.post("/llm/p1/v1/messages", headers=BEARER, content=b"{}")
    assert r.content == b"".join(events)
    assert r.headers["content-type"] == "text/event-stream"


async def test_a_redirect_is_handed_to_the_caller_not_followed(
    internal_client, respx_mock, registered_p1
):
    respx_mock.post(MESSAGES).respond(302, headers={"location": "https://evil.example.net/steal"})
    elsewhere = respx_mock.route(host="evil.example.net").respond(200)
    r = await internal_client.post("/llm/p1/v1/messages", headers=BEARER, content=b"{}")
    assert r.status_code == 302 and r.headers["location"] == "https://evil.example.net/steal"
    assert not elsewhere.called


async def test_the_date_of_the_provider_is_not_duplicated(
    internal_client, respx_mock, registered_p1
):
    respx_mock.get("https://api.example.com/v1/models").respond(
        200, headers={"date": "Mon, 01 Jan 2029 00:00:00 GMT", "x-kept": "yes"}
    )
    r = await internal_client.get("/llm/p1/v1/models", headers=BEARER)
    assert "date" not in r.headers  # the server adds its own
    assert r.headers["x-kept"] == "yes"


async def test_repeated_response_headers_are_kept(internal_client, respx_mock, registered_p1):
    respx_mock.get("https://api.example.com/v1/models").respond(
        200, headers=[("x-multi", "a"), ("x-multi", "b")]
    )
    r = await internal_client.get("/llm/p1/v1/models", headers=BEARER)
    assert r.headers.get_list("x-multi") == ["a", "b"]


async def test_response_headers_that_cannot_be_forwarded_are_dropped(
    internal_client, respx_mock, registered_p1
):
    respx_mock.get("https://api.example.com/v1/models").respond(
        200,
        headers=[(b"x-ok", b"1"), (b"x-accent", "café".encode()), (b"x bad", b"1")],
    )
    r = await internal_client.get("/llm/p1/v1/models", headers=BEARER)
    assert r.status_code == 200
    assert r.headers["x-ok"] == "1" and "x-accent" not in r.headers and "x bad" not in r.headers


# --- Relay: the caller's request ----------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/llm/p1/%2e%2e/secret",
        "/llm/p1/%2E%2E",
        "/llm/p1/a%2Fb",
        "/llm/p1/a%5Cb",
        "/llm/p1/http:evil",
        "/llm/p1//double",
    ],
)
async def test_an_unacceptable_path_returns_400(internal_client, registered_p1, path):
    r = await internal_client.get(path, headers=BEARER)
    assert r.status_code == 400
    assert r.json() == {"error": "invalid_path"}


@pytest.mark.parametrize(
    "header",
    [
        (b"x bad", b"1"),  # a space is not a token character
        (b"x(bad)", b"1"),
        (b"", b"1"),
        (b"x-ok", "café".encode("latin-1")),
        (b"x-ok", b"\xff"),
        (b"x-ok", b"nul\x00"),
        (b"x-ok", b"bell\x07"),
        (b"x-ok", b"del\x7f"),
    ],
)
async def test_a_header_that_cannot_be_relayed_returns_400(internal_client, registered_p1, header):
    r = await internal_client.get(
        "/llm/p1/v1/models", headers=[(b"authorization", f"Bearer {CORE_TOKEN}".encode()), header]
    )
    assert r.status_code == 400
    assert r.json() == {"error": "invalid_header"}


async def test_repeated_request_headers_are_combined(internal_client, respx_mock, registered_p1):
    upstream = respx_mock.get("https://api.example.com/v1/models").respond(200)
    await internal_client.get(
        "/llm/p1/v1/models", headers=[*BEARER.items(), ("x-multi", "a"), ("x-multi", "b")]
    )
    assert upstream.calls.last.request.headers["x-multi"] == "a, b"


async def test_an_empty_header_value_is_relayed(internal_client, respx_mock, registered_p1):
    upstream = respx_mock.get("https://api.example.com/v1/models").respond(200)
    await internal_client.get("/llm/p1/v1/models", headers={**BEARER, "x-empty": ""})
    assert upstream.calls.last.request.headers["x-empty"] == ""


async def test_a_body_over_the_limit_returns_413(
    internal_client, registered_p1, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(internal_api, "MAX_RELAY_BODY_BYTES", 10)
    r = await internal_client.post("/llm/p1/v1/messages", headers=BEARER, content=b"x" * 11)
    assert r.status_code == 413
    assert r.json() == {"error": "body_too_large"}


async def test_a_body_at_the_limit_is_relayed(
    internal_client, respx_mock, registered_p1, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(internal_api, "MAX_RELAY_BODY_BYTES", 10)
    upstream = respx_mock.post(MESSAGES).respond(200)
    r = await internal_client.post("/llm/p1/v1/messages", headers=BEARER, content=b"x" * 10)
    assert r.status_code == 200 and upstream.calls.last.request.content == b"x" * 10


async def test_a_streamed_body_over_the_limit_returns_413(
    internal_client, registered_p1, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(internal_api, "MAX_RELAY_BODY_BYTES", 10)

    async def chunks() -> AsyncIterator[bytes]:  # no content-length: sent chunked
        yield b"x" * 6
        yield b"x" * 6

    r = await internal_client.post("/llm/p1/v1/messages", headers=BEARER, content=chunks())
    assert r.status_code == 413


# --- Relay: errors ------------------------------------------------------------------------------


async def test_a_provider_that_cannot_be_reached_returns_502(
    internal_client, respx_mock, registered_p1
):
    respx_mock.post(MESSAGES).mock(side_effect=httpx.ConnectError(f"refused {KEY}"))
    r = await internal_client.post("/llm/p1/v1/messages", headers=BEARER, content=b"{}")
    assert r.status_code == 502
    assert r.json() == {"error": "upstream_error"}


async def test_a_provider_that_does_not_answer_in_time_returns_504(
    internal_client, respx_mock, registered_p1
):
    respx_mock.post(MESSAGES).mock(side_effect=httpx.ReadTimeout("slow"))
    r = await internal_client.post("/llm/p1/v1/messages", headers=BEARER, content=b"{}")
    assert r.status_code == 504
    assert r.json() == {"error": "upstream_timeout"}


@pytest.mark.parametrize("coding", ["x-gzip", "compress"])
async def test_an_answer_that_cannot_be_scrubbed_returns_502(
    internal_client, respx_mock, registered_p1, coding
):
    respx_mock.post(MESSAGES).respond(
        200, headers={"content-encoding": coding}, content=b"opaque " + KEY.encode()
    )
    r = await internal_client.post("/llm/p1/v1/messages", headers=BEARER, content=b"{}")
    assert r.status_code == 502
    assert r.json() == {"error": "upstream_error"}
    assert KEY not in r.text


async def test_a_route_to_a_host_the_key_is_not_bound_to_returns_403(
    internal_client, registered_p1, registry_db_path
):
    insert_route(registry_db_path, "evil", "https://evil.example.net", registered_p1.secret_ref)
    r = await internal_client.post("/llm/evil/v1/messages", headers=BEARER, content=b"{}")
    assert r.status_code == 403
    assert r.json() == {"error": "binding_mismatch"}


async def test_a_keyed_route_over_plain_http_to_a_public_host_returns_403(
    internal_client, registered_p1, registry_db_path
):
    insert_route(registry_db_path, "plain", "http://api.example.com", registered_p1.secret_ref)
    r = await internal_client.post("/llm/plain/v1/messages", headers=BEARER, content=b"{}")
    assert r.status_code == 403
    assert r.json() == {"error": "insecure_transport"}


async def test_a_route_whose_secret_left_the_vault_returns_409(internal_client, registry_db_path):
    insert_route(registry_db_path, "gone", "https://api.example.com", "provider:gone:api_key")
    r = await internal_client.post("/llm/gone/v1/messages", headers=BEARER, content=b"{}")
    assert r.status_code == 409
    assert r.json() == {"error": "secret_missing"}


async def test_a_secret_that_cannot_be_decrypted_returns_500(
    internal_client, registered_p1, vault_db_path
):
    with sqlite3.connect(vault_db_path) as conn:
        conn.execute("UPDATE secrets SET ciphertext = zeroblob(length(ciphertext))")
    r = await internal_client.post("/llm/p1/v1/messages", headers=BEARER, content=b"{}")
    assert r.status_code == 500
    assert r.json() == {"error": "vault_error"}


async def test_a_secret_that_cannot_go_in_a_header_returns_500(internal_client, vault, registry):
    value_with_newline = "line\nbreak"
    unusable = Binding(
        host="api.example.com", location=HeaderLocation(kind="header", name="x-api-key")
    )
    vault.put("provider:p3:api_key", value_with_newline, purpose="p", bindings=[unusable])
    route = ProviderRoute(
        id="p3", base_url="https://api.example.com", secret_ref="provider:p3:api_key"
    )
    registry.register(route, vault)
    r = await internal_client.post("/llm/p3/v1/messages", headers=BEARER, content=b"{}")
    assert r.status_code == 500
    assert r.json() == {"error": "secret_unusable"}
    assert "line" not in r.text


async def test_an_unexpected_failure_returns_a_generic_500(
    internal_app, registered_p1, monkeypatch: pytest.MonkeyPatch
):
    async def explode(*args: object, **kwargs: object) -> RelayResponse:
        raise RuntimeError(f"boom {KEY}")

    monkeypatch.setattr(internal_api, "relay", explode)
    async with client_for(internal_app, BEARER, raise_app_exceptions=False) as client:
        r = await client.post("/llm/p1/v1/messages", content=b"{}")
    assert r.status_code == 500
    assert r.json() == {"error": "internal_error"}
    assert "boom" not in r.text and KEY not in r.text


async def test_a_keyerror_that_is_not_the_missing_secret_is_not_taken_for_it(
    internal_app, registered_p1, monkeypatch: pytest.MonkeyPatch
):
    async def explode(*args: object, **kwargs: object) -> RelayResponse:
        raise KeyError("something-else")

    monkeypatch.setattr(internal_api, "relay", explode)
    async with client_for(internal_app, BEARER, raise_app_exceptions=False) as client:
        r = await client.post("/llm/p1/v1/messages", content=b"{}")
    assert r.status_code == 500


@pytest.mark.parametrize(
    ("method", "path", "status", "error"),
    [
        ("GET", "/internal/nothing-here", 404, "not_found"),
        ("DELETE", "/internal/secrets", 405, "method_not_allowed"),
        ("GET", "/llm/p1", 404, "not_found"),
    ],
)
async def test_framework_errors_have_the_same_small_body(
    authed_client, method, path, status, error
):
    r = await authed_client.request(method, path)
    assert r.status_code == status
    assert r.json() == {"error": error}


async def test_a_method_that_is_not_allowed_says_which_are(authed_client):
    r = await authed_client.delete("/internal/secrets")
    assert r.status_code == 405 and "GET" in r.headers["allow"]


async def test_there_is_no_api_documentation(authed_client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert (await authed_client.get(path)).status_code == 404


# --- Releasing the provider's connection --------------------------------------------------------


class _Closable(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"data"

    async def aclose(self) -> None:
        self.closed = True


async def test_the_provider_connection_is_released_once_the_answer_is_sent(
    internal_client, registered_p1, monkeypatch: pytest.MonkeyPatch
):
    # A body that never closes the upstream by itself: the handler must do it.
    stream = _Closable()

    async def body() -> AsyncIterator[bytes]:
        yield b"data"

    async def fake_relay(*args: object, **kwargs: object) -> RelayResponse:
        upstream = httpx.Response(200, stream=stream)
        return RelayResponse(status=200, headers=[], body=body(), upstream=upstream)

    monkeypatch.setattr(internal_api, "relay", fake_relay)
    r = await internal_client.get("/llm/p1/x", headers=BEARER)
    assert r.content == b"data" and stream.closed
