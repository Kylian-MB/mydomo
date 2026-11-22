"""Logging in, failing to and logging out leave entries in the audit log, in the same
transaction as the change they describe."""

import httpx
import pytest
from domo_core.accounts import routes as account_routes
from domo_core.accounts.models import WebSession
from domo_core.accounts.sessions import SESSION_COOKIE
from domo_core.accounts.throttle import LoginThrottle
from domo_core.audit.log import ChainCheck, verify_audit_chain
from domo_core.web.app import create_app
from sqlalchemy import func, select
from web_support import (
    OWNER_PASSWORD,
    OWNER_USERNAME,
    add_protected_routes,
    audit_entries,
    log_in,
)

BAD_PASSWORD = "bad-password-x"


async def fail_to_log_in(client, username=OWNER_USERNAME, password=BAD_PASSWORD, **kwargs):
    return await client.post("/login", data={"username": username, "password": password}, **kwargs)


async def log_in_raw(client):
    return await client.post(
        "/login", data={"username": OWNER_USERNAME, "password": OWNER_PASSWORD}
    )


def recorded(entries):
    return [(entry.actor, entry.action, entry.details) for entry in entries]


async def count_sessions(session) -> int:
    return (await session.execute(select(func.count()).select_from(WebSession))).scalar_one()


# --- logging in -------------------------------------------------------------------------------


async def test_a_login_is_recorded_for_the_person(client, owner_account, session):
    await log_in(client)
    assert recorded(await audit_entries(session)) == [
        (f"person:{owner_account.person_id}", "auth.login", {})
    ]


async def test_each_login_is_recorded(client, owner_account, session):
    await log_in(client)
    await log_in(client)
    actions = [entry.action for entry in await audit_entries(session)]
    assert actions == ["auth.login", "auth.login"]


# --- failing ----------------------------------------------------------------------------------


@pytest.mark.parametrize("typed", [OWNER_USERNAME, "  OWNER\t"])  # as typed, and with noise
async def test_a_failed_login_for_an_existing_account_records_its_username(
    client, owner_account, session, typed
):
    r = await fail_to_log_in(client, username=typed)
    assert r.status_code == 400
    assert "set-cookie" not in r.headers
    assert recorded(await audit_entries(session)) == [
        ("system", "auth.login_failed", {"username": "owner"})
    ]
    assert await count_sessions(session) == 0


@pytest.mark.parametrize(
    "typed",
    [
        "nobody-here",
        "  Nobody-Here\t",
        OWNER_PASSWORD,  # a password typed into the username box
        "ow\x00ner",  # a character PostgreSQL text cannot hold
        "a" * 5000,
        "<script>alert(1)</script>",
    ],
    ids=["unknown", "unknown with noise", "a password", "a NUL character", "very long", "markup"],
)
async def test_a_failed_login_for_an_unknown_username_records_nothing_that_was_typed(
    client, owner_account, session, typed
):
    r = await fail_to_log_in(client, username=typed)
    assert r.status_code == 400
    assert "set-cookie" not in r.headers
    assert recorded(await audit_entries(session)) == [
        ("system", "auth.login_failed", {"username_known": False})
    ]
    assert await count_sessions(session) == 0


async def test_the_unknown_username_entries_do_not_depend_on_what_was_typed(
    client, owner_account, session
):
    for typed in ("ghost", "someone-else", OWNER_PASSWORD):
        await fail_to_log_in(client, username=typed)
    details = {entry.details["username_known"] for entry in await audit_entries(session)}
    assert details == {False}
    everything = repr(recorded(await audit_entries(session)))
    for typed in ("ghost", "someone", "correct", "battery"):
        assert typed not in everything


async def test_passwords_never_reach_the_log(client, owner_account, session):
    await fail_to_log_in(client, password=BAD_PASSWORD)
    await fail_to_log_in(client, username="nobody-here", password="another-bad-one")
    await log_in(client)
    everything = repr(recorded(await audit_entries(session)))
    for password in (BAD_PASSWORD, "another-bad-one", OWNER_PASSWORD):
        assert password not in everything


async def test_a_failed_login_answers_the_same_whether_or_not_the_account_exists(
    client, owner_account
):
    """The log tells them apart (it is for the owner); the visitor must not be able to."""
    known = await fail_to_log_in(client)
    unknown = await fail_to_log_in(client, username="nobody-here")
    assert known.status_code == unknown.status_code == 400
    assert known.text.replace('value="owner"', 'value=""') == unknown.text.replace(
        'value="nobody-here"', 'value=""'
    )


async def test_attempts_refused_by_the_throttle_are_not_recorded_again(
    client, owner_account, session
):
    for _ in range(5):
        await fail_to_log_in(client)
    assert (await fail_to_log_in(client)).status_code == 429
    assert (await log_in_raw(client)).status_code == 429
    assert [entry.action for entry in await audit_entries(session)] == ["auth.login_failed"] * 5


# --- logging out ------------------------------------------------------------------------------


async def test_a_logout_is_recorded_for_the_person(client, owner_account, session):
    await log_in(client)
    r = await client.post("/logout")
    assert r.status_code == 303
    assert recorded(await audit_entries(session)) == [
        (f"person:{owner_account.person_id}", "auth.login", {}),
        (f"person:{owner_account.person_id}", "auth.logout", {}),
    ]


@pytest.mark.parametrize("cookie", [None, "garbage"])
async def test_a_logout_that_ends_no_session_is_not_recorded(
    client, owner_account, session, cookie
):
    headers = {} if cookie is None else {"cookie": f"{SESSION_COOKIE}={cookie}"}
    r = await client.post("/logout", headers=headers)
    assert r.status_code == 303
    assert await audit_entries(session) == []


async def test_logging_out_twice_with_the_same_session_is_recorded_once(
    client, owner_account, session
):
    await log_in(client)
    token = client.cookies[SESSION_COOKIE]
    await client.post("/logout")
    await client.post("/logout", headers={"cookie": f"{SESSION_COOKIE}={token}"})
    actions = [entry.action for entry in await audit_entries(session)]
    assert actions == ["auth.login", "auth.logout"]


# --- the real stack: one transaction per request, committed for real ----------------------------


@pytest.fixture
async def production_like_app(settings, clock, add_owner_in_database):
    """The application, started, with a session per request (as in production) and an owner
    committed in the database."""
    await add_owner_in_database()
    app = create_app(settings)
    app.state.login_throttle = LoginThrottle(clock=clock)
    add_protected_routes(app)
    async with app.router.lifespan_context(app):
        yield app


def lenient_client(app) -> httpx.AsyncClient:
    """A client that gets a 500 response instead of the exception the application re-raises."""
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(
        transport=transport, base_url="https://testserver", headers={"origin": "https://testserver"}
    )


async def test_audit_entries_are_committed_with_the_request(
    production_like_app, session_factory, client_factory
):
    async with client_factory(production_like_app) as http:
        await fail_to_log_in(http)
        await log_in(http)
        await http.post("/logout")
    async with session_factory() as other:  # another connection: only what was committed
        entries = await audit_entries(other)
        assert [entry.action for entry in entries] == [
            "auth.login_failed",
            "auth.login",
            "auth.logout",
        ]
        assert await count_sessions(other) == 0
        assert await verify_audit_chain(other) == ChainCheck(ok=True, count=3, first_bad_id=None)


async def test_a_login_whose_audit_entry_fails_creates_no_session(
    production_like_app, session_factory, monkeypatch
):
    async def unavailable(*args, **kwargs):
        raise RuntimeError("the audit log is unavailable")

    monkeypatch.setattr(account_routes, "append_audit", unavailable)
    async with lenient_client(production_like_app) as http:
        r = await log_in_raw(http)
    assert r.status_code == 500
    assert "set-cookie" not in r.headers
    async with session_factory() as other:
        assert await count_sessions(other) == 0


async def test_a_logout_whose_audit_entry_fails_does_not_end_the_session(
    production_like_app, session_factory, monkeypatch
):
    async with lenient_client(production_like_app) as http:
        await log_in(http)

        async def unavailable(*args, **kwargs):
            raise RuntimeError("the audit log is unavailable")

        monkeypatch.setattr(account_routes, "append_audit", unavailable)
        r = await http.post("/logout")
        assert r.status_code == 500
        monkeypatch.undo()
        assert (await http.get("/whoami")).status_code == 200  # still logged in
    async with session_factory() as other:
        assert await count_sessions(other) == 1
