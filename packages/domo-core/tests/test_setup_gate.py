"""The setup gate: until the first launch is over, every page leads to the current setup step."""

import httpx
import pytest
from domo_common.i18n import t
from domo_core.accounts import routes as account_routes
from domo_core.setup_flow import middleware
from domo_core.setup_flow.middleware import (
    ALWAYS_REACHABLE,
    SetupGateMiddleware,
    mark_setup_done,
)
from domo_core.web.app import create_app
from sqlalchemy import text
from web_support import build_test_app

EVIL = "https://evil.example.net"


@pytest.fixture
def app(settings, session, clock):
    """The application on the shared rolled-back session, with the first launch not over."""
    return build_test_app(settings, session, clock)


async def set_stage(session, stage: str) -> None:
    owner = "now()" if stage in ("model", "done") else "NULL"
    model = "now()" if stage == "done" else "NULL"
    await session.execute(
        text(f"UPDATE setup_state SET owner_created_at = {owner}, model_configured_at = {model}")
    )


# --- stage "owner": nothing exists yet -------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/chat", "/audit", "/whoami", "/owner-only", "/anything"])
async def test_every_page_leads_to_the_owner_step_until_there_is_an_owner(client, path):
    r = await client.get(path)
    assert r.status_code == 303
    assert r.headers["location"] == "/setup/owner"


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
async def test_unsafe_requests_are_redirected_too(client, method):
    r = await client.request(method, "/chat")
    assert r.status_code == 303
    assert r.headers["location"] == "/setup/owner"


async def test_the_pages_that_must_stay_reachable_are_reachable(client):
    assert (await client.get("/healthz")).status_code == 200
    assert (await client.get("/static/app.css")).status_code == 200
    assert (await client.get("/login")).status_code == 200
    assert (await client.get("/setup/owner")).status_code == 200
    r = await client.post("/logout")  # nobody is logged in: it still ends where it should
    assert r.status_code == 303 and r.headers["location"] == "/login"


@pytest.mark.parametrize(("stage", "step"), [("owner", "/setup/owner"), ("model", "/setup/model")])
async def test_an_invitation_link_leads_to_the_setup_until_it_is_over(
    client, session, stage, step
):  # R37
    await set_stage(session, stage)
    for method in ("GET", "POST"):
        r = await client.request(method, "/invite/x")
        assert r.status_code == 303
        assert r.headers["location"] == step


async def test_an_invitation_link_is_reachable_once_the_setup_is_over(client, session):
    await set_stage(session, "done")
    r = await client.get("/invite/x")
    assert r.status_code == 410  # no such invitation: the page says so, the gate let it through


async def test_the_model_step_needs_the_owner_step_first(client):
    r = await client.get("/setup/model")
    assert r.status_code == 303
    assert r.headers["location"] == "/setup/owner"


@pytest.mark.parametrize("path", ["/setupx", "/setup-owner", "/staticx", "/healthzz", "/loginx"])
async def test_lookalike_paths_are_not_exempt(client, path):
    r = await client.get(path)
    assert r.status_code == 303
    assert r.headers["location"] == "/setup/owner"


async def test_paths_that_only_start_like_a_step_are_not_that_step(client):
    # Not the owner step, and not a page: the router answers (a 404), the gate does not redirect.
    assert (await client.get("/setup/ownerx")).status_code == 404
    assert (await client.get("/setup/nothing")).status_code == 404


async def test_an_htmx_request_gets_a_401_and_a_redirect_header(client):
    r = await client.get("/chat", headers={"HX-Request": "true"})
    assert r.status_code == 401
    assert r.headers["hx-redirect"] == "/setup/owner"
    assert "location" not in r.headers


async def test_a_request_that_only_looks_like_htmx_gets_the_normal_redirect(client):
    r = await client.get("/chat", headers={"HX-Request": "false"})
    assert r.status_code == 303
    assert "hx-redirect" not in r.headers


async def test_redirects_carry_the_security_headers_and_are_not_cached(client):
    r = await client.get("/chat")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"


async def test_a_cross_site_post_is_refused_before_the_gate_answers(client):
    r = await client.post("/chat", headers={"origin": EVIL})
    assert r.status_code == 403


# --- stage "model": the owner exists ---------------------------------------------------------


async def test_every_page_leads_to_the_model_step_once_the_owner_exists(client, session):
    await set_stage(session, "model")
    for path in ("/", "/chat", "/audit", "/whoami"):
        r = await client.get(path)
        assert (r.status_code, r.headers["location"]) == (303, "/setup/model"), path


async def test_the_owner_step_is_closed_once_the_owner_exists(client, session):
    await set_stage(session, "model")
    for method in ("GET", "POST"):
        r = await client.request(method, "/setup/owner")
        assert (r.status_code, r.headers["location"]) == (303, "/setup/model")
    r = await client.get("/setup/owner", headers={"HX-Request": "true"})
    assert (r.status_code, r.headers["hx-redirect"]) == (401, "/setup/model")


async def test_the_model_step_is_open_to_the_owner_during_the_model_stage(client, session):
    await set_stage(session, "model")
    r = await client.get("/setup/model")
    # The gate lets it through: the page itself wants a logged-in owner.
    assert r.status_code == 303 and r.headers["location"] == "/login"


async def test_the_login_stays_reachable_during_the_model_stage(client, session):
    # An owner whose session ended during the model step must be able to sign in again.
    await set_stage(session, "model")
    assert (await client.get("/login")).status_code == 200
    r = await client.post("/login", data={"username": "ghost", "password": "wrong-password"})
    assert r.status_code == 400  # an answer of the login, not a redirect of the gate
    r = await client.post("/logout")
    assert r.status_code == 303 and r.headers["location"] == "/login"


# --- stage "done" ----------------------------------------------------------------------------


SETUP_PATHS = ["/setup/owner", "/setup/model", "/setup/model/anything", "/setup"]


@pytest.mark.parametrize("path", SETUP_PATHS)
async def test_the_setup_pages_lead_to_the_chat_once_it_is_all_done(client, session, path):
    await set_stage(session, "done")
    for method in ("GET", "POST"):
        r = await client.request(method, path)
        assert (r.status_code, r.headers["location"]) == (303, "/chat"), (method, path)


async def test_an_htmx_request_to_the_setup_is_sent_to_the_chat_with_a_header(client, session):
    await set_stage(session, "done")
    r = await client.get("/setup/model", headers={"HX-Request": "true"})
    assert (r.status_code, r.headers["hx-redirect"]) == (401, "/chat")


async def test_the_other_pages_are_left_alone_once_it_is_all_done(client, session):
    await set_stage(session, "done")
    r = await client.get("/")
    assert (r.status_code, r.headers["location"]) == (303, "/chat")  # the page's own redirect
    r = await client.get("/whoami")
    assert (r.status_code, r.headers["location"]) == (303, "/login")  # its own: no session
    assert (await client.get("/no-such-page")).status_code == 404
    assert (await client.get("/healthz")).status_code == 200


async def test_the_gate_follows_the_stages_as_they_advance(client, session):
    for stage, expected in (("owner", "/setup/owner"), ("model", "/setup/model")):
        await set_stage(session, stage)
        assert (await client.get("/whoami")).headers["location"] == expected
    await set_stage(session, "done")
    assert (await client.get("/whoami")).headers["location"] == "/login"


# --- remembering that it is over -------------------------------------------------------------


async def test_once_it_is_done_the_database_is_not_asked_again(client, session, monkeypatch):
    asked: list[object] = []
    real = middleware.setup_stage

    async def counting(db_session):
        asked.append(db_session)
        return await real(db_session)

    monkeypatch.setattr(middleware, "setup_stage", counting)
    await set_stage(session, "done")
    for _ in range(3):
        await client.get("/whoami")
    assert len(asked) == 1  # the stage only moves forward: "done" is final


async def test_a_stage_that_is_not_done_is_read_every_time(client, session, monkeypatch):
    asked: list[object] = []
    real = middleware.setup_stage

    async def counting(db_session):
        asked.append(db_session)
        return await real(db_session)

    monkeypatch.setattr(middleware, "setup_stage", counting)
    await set_stage(session, "model")
    for _ in range(3):
        await client.get("/whoami")
    assert len(asked) == 3


async def test_the_exempt_pages_never_ask_the_database(client, monkeypatch):
    asked: list[object] = []

    async def counting(db_session):
        asked.append(db_session)
        return "owner"

    monkeypatch.setattr(middleware, "setup_stage", counting)
    for path in ("/healthz", "/static/app.css"):
        assert (await client.get(path)).status_code == 200
    assert asked == []
    # The gate leaves the login alone; its texts read the stage once, to know whether they are
    # the fixed ones of the installation.
    login = await client.get("/login", headers={"accept-language": "fr"})
    assert login.status_code == 200 and len(asked) == 1
    assert t("login.submit", "fr") in login.text
    assert (await client.post("/logout")).status_code == 303


async def test_the_exempt_pages_work_without_any_database(offline_settings, client_factory):
    app = create_app(offline_settings)  # no session factory: any database access would fail
    assert not hasattr(app.state, "session_factory")
    async with client_factory(app) as http:
        assert (await http.get("/healthz")).status_code == 200
        assert (await http.get("/static/app.css")).status_code == 200
        assert (await http.get("/login")).status_code == 200


async def test_an_unreachable_database_is_a_server_error_not_an_open_gate(offline_settings):
    app = create_app(offline_settings)

    def broken():
        raise OSError("the database is down")

    app.state.session_factory = broken
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as http:
        r = await http.get("/chat")
    assert r.status_code == 500
    assert "location" not in r.headers


async def test_mark_setup_done_makes_the_gate_step_aside(offline_settings, client_factory):
    app = create_app(offline_settings)
    mark_setup_done(app)
    async with client_factory(app) as http:
        assert (await http.get("/no-such-page")).status_code == 404  # not a redirect
        r = await http.get("/setup/owner")
        assert (r.status_code, r.headers["location"]) == (303, "/chat")


# --- the middleware itself -------------------------------------------------------------------


def test_the_reachable_paths_match_the_application():
    assert set(ALWAYS_REACHABLE) == {
        "/static",
        "/manifest.webmanifest",
        "/healthz",
        account_routes.LOGIN_PATH,
        "/logout",
    }


def test_the_application_has_the_gate_inside_the_security_middleware(offline_settings):
    app = create_app(offline_settings)
    classes = [entry.cls for entry in app.user_middleware]
    names = [cls.__name__ for cls in classes]
    # user_middleware lists the outermost first: the security checks come before the gate.
    assert names.index("SecurityMiddleware") < names.index(SetupGateMiddleware.__name__)


async def test_the_gate_ignores_what_is_not_http():
    calls: list[str] = []

    async def inner(scope, receive, send):
        calls.append(scope["type"])

    gate = SetupGateMiddleware(inner)
    for kind in ("lifespan", "websocket"):
        await gate({"type": kind}, None, None)
    assert calls == ["lifespan", "websocket"]
