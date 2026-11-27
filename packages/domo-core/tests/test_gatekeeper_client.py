"""The core's client of the gatekeeper's internal API, and how the application wires it."""

import json
from pathlib import Path
from typing import Annotated

import httpx
import pytest
from domo_common import core_token
from domo_common.gatekeeper_api import (
    Binding,
    HeaderLocation,
    OpenAddress,
    OutRequest,
    OutResponse,
    ProviderRoute,
    SecretRequestCreate,
)
from domo_core import gatekeeper_client
from domo_core.config import ConfigurationError, CoreSettings
from domo_core.gatekeeper_client import (
    BindingConflictError,
    GatekeeperClient,
    GatekeeperError,
    OutRefused,
    PhraseHeldBack,
    load_core_token,
)
from domo_core.llm.factory import ModelFactory
from domo_core.setup_flow.middleware import mark_setup_done
from domo_core.web.app import create_app
from domo_core.web.deps import get_gatekeeper, get_model_factory
from fakes import FakeChatModel, fake_factory
from fastapi import Depends
from llm_support import CORE_TOKEN, google_sse, provider, run, text_of

REQ = SecretRequestCreate(
    ref="provider:p1:api_key",
    purpose="API key of the test provider",
    bindings=[
        Binding(
            host="api.example.com",
            location=HeaderLocation(kind="header", name="authorization", template="Bearer {value}"),
        )
    ],
)
ROUTE = ProviderRoute(
    id="p1", base_url="https://api.example.com/v1", secret_ref="provider:p1:api_key"
)
VIEW = {
    "id": "r1",
    "ref": "provider:p1:api_key",
    "status": "pending",
    "form_path": "/vault/one-time",
    "expires_at": "2030-01-01T12:00:00Z",
}
METADATA = {
    "ref": "provider:p1:api_key",
    "purpose": "API key of the test provider",
    "bindings": [
        {
            "host": "api.example.com",
            "port": None,
            "location": {"kind": "header", "name": "authorization", "template": "Bearer {value}"},
        }
    ],
    "created_at": "2030-01-01T12:00:00Z",
    "rotated_at": None,
    "last_used_at": None,
}


# --- secret requests -----------------------------------------------------------------------


async def test_client_create_secret_request_conflict_raises(respx_mock, gk_client):
    respx_mock.post("http://gatekeeper.test/internal/secret-requests").respond(409)
    with pytest.raises(BindingConflictError):
        await gk_client.create_secret_request(REQ)


async def test_create_secret_request_posts_the_request_with_the_token(respx_mock, gk_client):
    route = respx_mock.post("http://gatekeeper.test/internal/secret-requests").respond(
        201, json=VIEW
    )
    view = await gk_client.create_secret_request(REQ)
    assert view.id == "r1" and view.status == "pending" and view.form_path == "/vault/one-time"
    request = route.calls.last.request
    assert request.headers["authorization"] == f"Bearer {CORE_TOKEN}"
    assert json.loads(request.content) == REQ.model_dump(mode="json")


async def test_a_conflict_answer_with_its_code_is_a_binding_conflict(respx_mock, gk_client):
    respx_mock.post("http://gatekeeper.test/internal/secret-requests").respond(
        409, json={"error": "binding_conflict"}
    )
    with pytest.raises(BindingConflictError) as excinfo:
        await gk_client.create_secret_request(REQ)
    assert excinfo.value.status == 409 and excinfo.value.code == "binding_conflict"


async def test_get_secret_request_reads_one_request(respx_mock, gk_client):
    route = respx_mock.get("http://gatekeeper.test/internal/secret-requests/r1").respond(
        200, json=VIEW
    )
    view = await gk_client.get_secret_request("r1")
    assert view.ref == "provider:p1:api_key"
    assert route.calls.last.request.headers["authorization"] == f"Bearer {CORE_TOKEN}"


async def test_the_request_id_cannot_change_the_path(respx_mock, gk_client):
    route = respx_mock.get(url__regex=r"http://gatekeeper\.test/internal/.*").respond(404)
    with pytest.raises(GatekeeperError):
        await gk_client.get_secret_request("../secrets")
    assert route.calls.last.request.url.raw_path == b"/internal/secret-requests/..%2Fsecrets"


async def test_an_unknown_request_is_a_gatekeeper_error_with_its_status(respx_mock, gk_client):
    respx_mock.get("http://gatekeeper.test/internal/secret-requests/r9").respond(
        404, json={"error": "not_found"}
    )
    with pytest.raises(GatekeeperError) as excinfo:
        await gk_client.get_secret_request("r9")
    assert (excinfo.value.status, excinfo.value.code) == (404, "not_found")


# --- secrets and providers -----------------------------------------------------------------


async def test_list_secrets_reads_the_metadata(respx_mock, gk_client):
    respx_mock.get("http://gatekeeper.test/internal/secrets").respond(200, json=[METADATA])
    secrets = await gk_client.list_secrets()
    assert [secret.ref for secret in secrets] == ["provider:p1:api_key"]
    assert secrets[0].bindings[0].host == "api.example.com"


async def test_register_provider_puts_the_route(respx_mock, gk_client):
    route = respx_mock.put("http://gatekeeper.test/internal/providers/p1").respond(204)
    assert await gk_client.register_provider(ROUTE) is None
    request = route.calls.last.request
    assert request.headers["authorization"] == f"Bearer {CORE_TOKEN}"
    assert json.loads(request.content) == ROUTE.model_dump(mode="json")


async def test_register_provider_conflict_raises(respx_mock, gk_client):
    respx_mock.put("http://gatekeeper.test/internal/providers/p1").respond(409)
    with pytest.raises(BindingConflictError):
        await gk_client.register_provider(ROUTE)


async def test_an_insecure_route_is_not_reported_as_a_binding_conflict(respx_mock, gk_client):
    respx_mock.put("http://gatekeeper.test/internal/providers/p1").respond(
        409, json={"error": "insecure_transport"}
    )
    with pytest.raises(GatekeeperError) as excinfo:
        await gk_client.register_provider(ROUTE)
    assert not isinstance(excinfo.value, BindingConflictError)
    assert (excinfo.value.status, excinfo.value.code) == (409, "insecure_transport")


async def test_a_route_for_an_unknown_secret_is_a_gatekeeper_error(respx_mock, gk_client):
    respx_mock.put("http://gatekeeper.test/internal/providers/p1").respond(
        404, json={"error": "unknown_secret"}
    )
    with pytest.raises(GatekeeperError) as excinfo:
        await gk_client.register_provider(ROUTE)
    assert (excinfo.value.status, excinfo.value.code) == (404, "unknown_secret")


# --- a program's request (step 2) ----------------------------------------------------------

OUT = OutRequest(
    method="GET",
    url="http://192.168.1.20:8123/api/states",
    headers={"accept": "application/json"},
    account="account.0123456789abcdef0123456789abcdef",
    open=[OpenAddress(host="192.168.1.20", port=8123)],
    seconds=30,
    most_bytes=1000,
    session_idle_seconds=3600,
)
ANSWERED = {"status": 200, "headers": {"x": "1"}, "body_b64": "e30=", "zone": "house", "hops": 0}


async def test_out_posts_the_request_with_the_token_and_reads_the_answer(respx_mock, gk_client):
    route = respx_mock.post("http://gatekeeper.test/internal/out").respond(200, json=ANSWERED)
    answer = await gk_client.out(OUT)
    assert answer == OutResponse.model_validate(ANSWERED)
    request = route.calls.last.request
    assert request.headers["authorization"] == f"Bearer {CORE_TOKEN}"
    assert json.loads(request.content) == OUT.model_dump(mode="json")


async def test_out_waits_for_the_gatekeeper_beyond_the_request_s_own_seconds(respx_mock, gk_client):
    # Its read timeout follows the request's seconds, past the client's usual 320 s.
    route = respx_mock.post("http://gatekeeper.test/internal/out").respond(200, json=ANSWERED)
    await gk_client.out(OUT.model_copy(update={"seconds": 900}))
    timeout = route.calls.last.request.extensions["timeout"]
    assert timeout["read"] > 900


@pytest.mark.parametrize(
    ("status", "code"),
    [(403, "closed_address"), (404, "account_not_found"), (502, "too_big"), (504, "too_slow")],
)
async def test_a_refusal_of_the_relay_is_out_refused_with_its_code(
    respx_mock, gk_client, status, code
):
    respx_mock.post("http://gatekeeper.test/internal/out").respond(status, json={"code": code})
    with pytest.raises(OutRefused) as excinfo:
        await gk_client.out(OUT)
    assert excinfo.value.code == code


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (401, {"error": "unauthorized"}),
        (500, {"code": "the site said: secret-1234"}),
        (403, {"code": "not_a_code"}),
        (200, {"status": "x"}),
        (404, {"error": "not_found"}),
    ],
)
async def test_any_other_answer_is_out_refused_failed_with_nothing_of_its_text(
    respx_mock, gk_client, status, body
):
    respx_mock.post("http://gatekeeper.test/internal/out").respond(status, json=body)
    with pytest.raises(OutRefused) as excinfo:
        await gk_client.out(OUT)
    assert excinfo.value.code == "failed"
    assert "secret" not in str(excinfo.value) and "not_a_code" not in str(excinfo.value)


async def test_a_gatekeeper_out_of_reach_is_out_refused_unreachable(respx_mock, gk_client):
    respx_mock.post("http://gatekeeper.test/internal/out").mock(
        side_effect=httpx.ConnectError("refused")
    )
    with pytest.raises(OutRefused) as excinfo:
        await gk_client.out(OUT)
    error = excinfo.value
    assert error.code == "unreachable"
    assert error.__cause__ is None and error.__context__ is None
    assert CORE_TOKEN not in str(error)


# --- the relay address and the token -----------------------------------------------------


def test_the_relay_base_url_is_below_llm_with_the_provider_id(gk_client):
    assert gk_client.relay_base_url("p1") == "http://gatekeeper.test/llm/p1"


async def test_a_trailing_slash_of_the_gatekeeper_url_makes_no_difference(respx_mock):
    async with httpx.AsyncClient() as http:
        client = GatekeeperClient("http://gatekeeper.test:8081/", CORE_TOKEN, http)
        assert client.relay_base_url("p1") == "http://gatekeeper.test:8081/llm/p1"
        route = respx_mock.get("http://gatekeeper.test:8081/internal/secrets").respond(200, json=[])
        assert await client.list_secrets() == []
        assert route.called


def test_the_token_property_gives_the_token_for_building_sdk_clients(gk_client):
    assert gk_client.token == CORE_TOKEN


def test_the_token_is_not_in_the_repr_or_the_str(gk_client):
    for text in (repr(gk_client), str(gk_client), f"{gk_client}", f"{gk_client!r}"):
        assert CORE_TOKEN not in text
        assert "gatekeeper.test" in text


# --- failures ------------------------------------------------------------------------------


async def test_a_wrong_token_is_a_gatekeeper_error_that_does_not_echo_it(respx_mock, gk_client):
    respx_mock.get("http://gatekeeper.test/internal/secrets").respond(
        401, json={"error": "unauthorized"}
    )
    with pytest.raises(GatekeeperError) as excinfo:
        await gk_client.list_secrets()
    assert (excinfo.value.status, excinfo.value.code) == (401, "unauthorized")
    assert CORE_TOKEN not in str(excinfo.value)


@pytest.mark.parametrize(
    "failure", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), httpx.ReadError("cut")]
)
async def test_a_connection_failure_is_an_unreachable_gatekeeper_error(
    respx_mock, gk_client, failure
):
    respx_mock.get("http://gatekeeper.test/internal/secrets").mock(side_effect=failure)
    with pytest.raises(GatekeeperError) as excinfo:
        await gk_client.list_secrets()
    error = excinfo.value
    assert (error.status, error.code) == (None, "unreachable")
    # The httpx exception carries the request, with the token in its headers: it is not kept.
    assert error.__cause__ is None and error.__context__ is None
    assert CORE_TOKEN not in str(error) and CORE_TOKEN not in repr(error)


@pytest.mark.parametrize("body", [b"not json", b'{"id": 3}', b"[]"])
async def test_an_answer_that_is_not_what_the_api_says_is_a_gatekeeper_error(
    respx_mock, gk_client, body
):
    respx_mock.get("http://gatekeeper.test/internal/secret-requests/r1").respond(200, content=body)
    with pytest.raises(GatekeeperError) as excinfo:
        await gk_client.get_secret_request("r1")
    assert excinfo.value.code == "bad_response"


async def test_an_error_code_that_is_not_a_plain_word_is_dropped(respx_mock, gk_client):
    respx_mock.get("http://gatekeeper.test/internal/secrets").respond(
        500, json={"error": "<script>" + "x" * 500}
    )
    with pytest.raises(GatekeeperError) as excinfo:
        await gk_client.list_secrets()
    assert excinfo.value.code is None and "script" not in str(excinfo.value)


async def test_the_client_does_not_follow_redirects(respx_mock, gk_client):
    respx_mock.get("http://gatekeeper.test/internal/secrets").respond(
        302, headers={"location": "http://evil.example.net/"}
    )
    other = respx_mock.get("http://evil.example.net/").respond(200, json=[])
    with pytest.raises(GatekeeperError):
        await gk_client.list_secrets()
    assert not other.called


# --- the core token file -------------------------------------------------------------------
# The rule itself is domo_common's (packages/domo-common/tests/test_core_token.py), shared with
# the gatekeeper: here, only that the core reads the token with it and stops with its own error.


def test_the_token_is_read_from_its_file(tmp_path: Path):
    path = tmp_path / "core_token"
    path.write_bytes(b"  abc123-token" + bytes([13, 10]))
    assert load_core_token(path) == "abc123-token"


def test_a_missing_token_file_is_a_clear_error(tmp_path: Path):
    with pytest.raises(ConfigurationError, match="core token file is missing") as excinfo:
        load_core_token(tmp_path / "nothing")
    assert str(tmp_path) in str(excinfo.value)
    assert excinfo.value.__cause__ is None and excinfo.value.__suppress_context__


@pytest.mark.parametrize("content", [b"", b"two words", bytes([0, 97, 98, 99])])
def test_a_token_the_shared_rule_refuses_is_a_clear_error_that_does_not_echo_it(
    tmp_path: Path, content: bytes
):
    path = tmp_path / "core_token"
    path.write_bytes(content)
    with pytest.raises(ConfigurationError, match="empty|printable ASCII") as excinfo:
        load_core_token(path)
    assert "words" not in str(excinfo.value)


def test_the_core_reads_the_token_with_the_shared_rule():
    """One rule for both sides: a token the gatekeeper accepts is never refused here (every
    call would then fail with 401)."""
    assert gatekeeper_client.common_load_core_token is core_token.load_core_token


# --- the application's lifespan and dependencies ---------------------------------------------


async def test_the_application_builds_the_client_when_it_starts(offline_settings: CoreSettings):
    app = create_app(offline_settings)
    assert not hasattr(app.state, "gatekeeper")
    async with app.router.lifespan_context(app):
        client = app.state.gatekeeper
        assert isinstance(client, GatekeeperClient)
        assert client.token == CORE_TOKEN
        assert client.relay_base_url("p1") == "http://gatekeeper:8081/llm/p1"
        http = client._http
        assert not http.is_closed
    assert http.is_closed


async def test_the_gatekeeper_client_uses_the_url_of_the_settings(core_token_file: Path):
    settings = CoreSettings(
        database_url_override="postgresql+asyncpg://domo:unused@localhost:1/db",
        core_token_file=core_token_file,
        gatekeeper_url="http://192.168.1.10:9000",
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        assert app.state.gatekeeper.relay_base_url("p1") == "http://192.168.1.10:9000/llm/p1"


async def test_the_http_client_has_the_documented_timeouts_and_follows_no_redirect(
    offline_settings: CoreSettings,
):
    app = create_app(offline_settings)
    async with app.router.lifespan_context(app):
        http: httpx.AsyncClient = app.state.gatekeeper._http
        assert http.timeout == httpx.Timeout(connect=10.0, read=320.0, write=60.0, pool=10.0)
        assert http.follow_redirects is False


@pytest.mark.parametrize("content", [None, ""])
async def test_starting_without_a_usable_core_token_is_a_clear_error(
    tmp_path: Path, content: str | None
):
    path = tmp_path / "core_token"
    if content is not None:
        path.write_text(content, encoding="ascii")
    settings = CoreSettings(
        database_url_override="postgresql+asyncpg://domo:unused@localhost:1/db",
        core_token_file=path,
    )
    app = create_app(settings)
    with pytest.raises(ConfigurationError, match="core token"):
        async with app.router.lifespan_context(app):
            pytest.fail("the application must not start")
    assert not hasattr(app.state, "engine")


async def test_get_gatekeeper_gives_the_client_of_the_application(offline_settings: CoreSettings):
    app = create_app(offline_settings)
    mark_setup_done(app)

    @app.get("/which-gatekeeper")
    async def which(client: Annotated[GatekeeperClient, Depends(get_gatekeeper)]) -> dict[str, str]:
        return {"relay": client.relay_base_url("p9")}

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as web:
            response = await web.get("/which-gatekeeper")
    assert response.json() == {"relay": "http://gatekeeper:8081/llm/p9"}


async def test_the_model_factory_builds_models_that_talk_through_the_applications_gatekeeper(
    respx_mock, offline_settings: CoreSettings
):
    route = respx_mock.post(url__regex=r"http://gatekeeper:8081/llm/p3/.*streamGenerateContent.*")
    route.respond(200, headers={"content-type": "text/event-stream"}, content=google_sse(["ok"]))
    app = create_app(offline_settings)
    mark_setup_done(app)

    @app.get("/ask")
    async def ask(factory: Annotated[ModelFactory, Depends(get_model_factory)]) -> dict[str, str]:
        events = await run(factory(provider("google", "p3"), "test-model"))
        return {"text": text_of(events)}

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as web:
            response = await web.get("/ask")
    assert response.json() == {"text": "ok"}
    assert route.calls.last.request.headers["x-goog-api-key"] == CORE_TOKEN


async def test_the_model_factory_can_be_replaced_in_tests(offline_settings: CoreSettings):
    app = create_app(offline_settings)
    mark_setup_done(app)

    @app.get("/ask")
    async def ask(factory: Annotated[ModelFactory, Depends(get_model_factory)]) -> dict[str, str]:
        return {"text": text_of(await run(factory(provider("openai", "p1"), "test-model")))}

    app.dependency_overrides[get_model_factory] = lambda: fake_factory(FakeChatModel(["fa", "ke"]))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as web:
        response = await web.get("/ask")
    assert response.json() == {"text": "fake"}


# --- every failure is logged, with nothing secret (task 14, R51) --------------------------------

REQUEST_ID = "req-id-that-must-not-be-logged"


@pytest.mark.parametrize(
    ("answer", "status", "code"),
    [
        (httpx.Response(500, json={"error": "internal_error"}), 500, "internal_error"),
        (httpx.Response(404, json={"error": "not_found"}), 404, "not_found"),
        (httpx.Response(200, content=b"not json"), 200, "bad_response"),
        (httpx.ConnectError("refused"), None, "unreachable"),
    ],
    ids=["5xx", "4xx", "bad answer", "unreachable"],
)
async def test_a_failed_call_is_logged_with_its_operation_status_and_code_only(
    respx_mock, gk_client, caplog, answer, status, code
):
    route = respx_mock.get(f"http://gatekeeper.test/internal/secret-requests/{REQUEST_ID}")
    if isinstance(answer, httpx.Response):
        route.mock(return_value=answer)
    else:
        route.mock(side_effect=answer)
    with caplog.at_level("INFO"), pytest.raises(GatekeeperError):
        await gk_client.get_secret_request(REQUEST_ID)
    [record] = [r for r in caplog.records if r.name == "domo_core.gatekeeper_client"]
    assert record.levelname == "WARNING"
    assert record.getMessage() == (
        f"The gatekeeper call GET /internal/secret-requests/{{id}} failed: status {status}, "
        f"code {code}"
    )
    # Never the token, nor the id in the path, nor what the exception of httpx said. (The line of
    # httpx itself, at INFO, names the URL: the core keeps httpx at WARNING, see __main__.)
    assert CORE_TOKEN not in caplog.text
    assert REQUEST_ID not in record.getMessage() and "refused" not in record.getMessage()


async def test_a_binding_conflict_is_logged_too(respx_mock, gk_client, caplog):
    respx_mock.post("http://gatekeeper.test/internal/secret-requests").respond(
        409, json={"error": "binding_conflict"}
    )
    with caplog.at_level("WARNING"), pytest.raises(BindingConflictError):
        await gk_client.create_secret_request(REQ)
    assert caplog.messages == [
        "The gatekeeper call POST /internal/secret-requests failed: status 409, "
        "code binding_conflict"
    ]
    assert "api.example.com" not in caplog.text and CORE_TOKEN not in caplog.text


async def test_an_unreachable_gatekeeper_names_the_operation_not_the_path(respx_mock, gk_client):
    respx_mock.put("http://gatekeeper.test/internal/providers/p1").mock(
        side_effect=httpx.ConnectError("refused")
    )
    with pytest.raises(GatekeeperError) as excinfo:
        await gk_client.register_provider(ROUTE)
    assert excinfo.value.operation == "PUT /internal/providers/{id}"


# --- the spoken phrase --------------------------------------------------------------------------

PHRASE_REF = "phrase.00000000-0000-0000-0000-000000000001"
PHRASE_URL = f"http://gatekeeper.test/internal/phrases/{PHRASE_REF}"
PHRASE = "le chat bleu dort sur le toit"


async def test_phrase_enrol_sends_what_was_heard_and_reads_the_step(respx_mock, gk_client):
    route = respx_mock.post(f"{PHRASE_URL}/enrol").respond(200, json={"step": "first_kept"})
    enrolled = await gk_client.phrase_enrol(PHRASE_REF, PHRASE)
    assert enrolled.step == "first_kept"
    sent = route.calls.last.request
    assert json.loads(sent.content) == {"heard": PHRASE}
    assert sent.headers["authorization"] == f"Bearer {CORE_TOKEN}"


@pytest.mark.parametrize("result", ["it_is", "almost", "nothing_like"])
async def test_phrase_compare_gives_the_result(respx_mock, gk_client, result):
    route = respx_mock.post(f"{PHRASE_URL}/compare").respond(200, json={"result": result})
    assert await gk_client.phrase_compare(PHRASE_REF, PHRASE) == result
    assert json.loads(route.calls.last.request.content) == {"heard": PHRASE}


async def test_a_held_back_phrase_says_how_long_to_wait(respx_mock, gk_client):
    respx_mock.post(f"{PHRASE_URL}/compare").respond(
        429, json={"error": "held_back"}, headers={"retry-after": "120"}
    )
    with pytest.raises(PhraseHeldBack) as held:
        await gk_client.phrase_compare(PHRASE_REF, PHRASE)
    assert held.value.wait == 120.0
    assert isinstance(held.value, GatekeeperError) and held.value.code == "held_back"


async def test_an_unknown_phrase_is_nothing_like_it(respx_mock, gk_client):
    """The gatekeeper answers ``nothing_like`` for a ref with no phrase; should a 404 of it come
    back all the same, it means the same: no error tells whether a phrase exists (T5 / T10)."""
    respx_mock.post(f"{PHRASE_URL}/compare").respond(404, json={"error": "unknown_phrase"})
    assert await gk_client.phrase_compare(PHRASE_REF, PHRASE) == "nothing_like"


async def test_another_404_of_a_comparison_is_still_an_error(respx_mock, gk_client):
    respx_mock.post(f"{PHRASE_URL}/compare").respond(404, json={"error": "not_found"})
    with pytest.raises(GatekeeperError) as failed:
        await gk_client.phrase_compare(PHRASE_REF, PHRASE)
    assert not isinstance(failed.value, PhraseHeldBack)


async def test_phrase_remove_says_whether_there_was_one(respx_mock, gk_client):
    route = respx_mock.delete(PHRASE_URL)
    route.side_effect = [httpx.Response(204), httpx.Response(404, json={"error": "unknown_phrase"})]
    assert await gk_client.phrase_remove(PHRASE_REF) is True
    assert await gk_client.phrase_remove(PHRASE_REF) is False


async def test_phrase_exists_asks_without_saying_anything(respx_mock, gk_client):
    route = respx_mock.get(PHRASE_URL)
    route.side_effect = [httpx.Response(204), httpx.Response(404, json={"error": "unknown_phrase"})]
    assert await gk_client.phrase_exists(PHRASE_REF) is True
    assert await gk_client.phrase_exists(PHRASE_REF) is False
    assert route.calls.last.request.content == b""


async def test_another_404_is_not_taken_for_no_phrase(respx_mock, gk_client):
    respx_mock.get(PHRASE_URL).respond(404, json={"error": "not_found"})
    with pytest.raises(GatekeeperError):
        await gk_client.phrase_exists(PHRASE_REF)


async def test_the_phrase_ref_cannot_change_the_path(respx_mock, gk_client):
    route = respx_mock.get("http://gatekeeper.test/internal/phrases/a%2F..%2Fsecrets").respond(204)
    assert await gk_client.phrase_exists("a/../secrets") is True
    assert route.called


@pytest.mark.parametrize(
    ("call", "method", "url", "answer"),
    [
        ("enrol", "POST", f"{PHRASE_URL}/enrol", httpx.Response(500, text=PHRASE)),
        ("compare", "POST", f"{PHRASE_URL}/compare", httpx.Response(422, json={"heard": PHRASE})),
        ("compare", "POST", f"{PHRASE_URL}/compare", httpx.Response(200, text=PHRASE)),
        ("compare", "POST", f"{PHRASE_URL}/compare", httpx.ConnectError(PHRASE)),
    ],
    ids=["enrol 500", "compare 422", "compare bad answer", "compare unreachable"],
)
async def test_a_refused_phrase_call_logs_no_body(
    respx_mock, gk_client, caplog, call, method, url, answer
):
    route = respx_mock.request(method, url)
    if isinstance(answer, httpx.Response):
        route.mock(return_value=answer)
    else:
        route.mock(side_effect=answer)
    with caplog.at_level("DEBUG"), pytest.raises(GatekeeperError) as failed:
        await getattr(gk_client, f"phrase_{call}")(PHRASE_REF, PHRASE)
    assert caplog.messages, "the failure is logged"
    for word in ("chat", "bleu", "toit"):
        assert word not in caplog.text and word not in str(failed.value)
    # The operation is named, not the path. (httpx's own line, at INFO, names the URL: the core
    # keeps httpx at WARNING, see __main__.)
    mine = [r.getMessage() for r in caplog.records if r.name == "domo_core.gatekeeper_client"]
    assert mine and not any(PHRASE_REF in message for message in mine)


@pytest.mark.parametrize("heard", ["", "x" * 401, "le chat bleu " * 40])
async def test_a_heard_text_the_api_would_refuse_is_refused_without_echoing_it(
    respx_mock, gk_client, caplog, heard
):
    with caplog.at_level("DEBUG"), pytest.raises(GatekeeperError) as failed:
        await gk_client.phrase_compare(PHRASE_REF, heard)
    assert failed.value.code == "invalid_request"
    assert failed.value.__cause__ is None and failed.value.__context__ is None
    assert "chat" not in caplog.text and "xxxx" not in caplog.text
    assert not respx_mock.calls
