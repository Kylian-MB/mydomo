import asyncio
import hashlib
import re
from datetime import UTC, datetime, timedelta

import pytest
from domo_common.i18n import t
from domo_core.accounts import routes as account_routes
from domo_core.accounts.models import WebSession
from domo_core.accounts.sessions import SESSION_COOKIE
from domo_core.accounts.throttle import MAX_FAILED_ATTEMPTS, WINDOW, LoginThrottle
from domo_core.web.app import create_app
from domo_core.web.deps import get_settings
from sqlalchemy import func, select
from web_support import (
    OWNER_PASSWORD,
    OWNER_USERNAME,
    add_protected_routes,
    audit_entries,
    cookie_attributes,
    house_reads,
    in_language,
)

BAD_PASSWORD = "bad-password-x"


async def login(client, username=OWNER_USERNAME, password=OWNER_PASSWORD, **kwargs):
    return await client.post("/login", data={"username": username, "password": password}, **kwargs)


async def count_sessions(session) -> int:
    return (await session.execute(select(func.count()).select_from(WebSession))).scalar_one()


# --- the page --------------------------------------------------------------------------------


async def test_security_headers_present(client):
    r = await client.get("/login")
    assert "default-src 'self'" in r.headers["content-security-policy"]


async def test_the_login_page_is_a_form_with_the_expected_fields(client):
    r = await client.get("/login")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    html = r.text
    assert re.search(r'<form[^>]*method="post"[^>]*action="/login"', html)
    assert re.search(r'<input[^>]*name="username"[^>]*autocomplete="username"', html)
    assert re.search(r'<input[^>]*name="password"[^>]*type="password"', html)
    assert 'autocomplete="current-password"' in html
    assert '<button type="submit"' in html
    assert 'role="alert"' not in html  # no error before anything was tried


async def test_the_login_page_speaks_the_house_language(client, owner_account, session):
    # A browser that names no language reads the house's (one whose language has neither a
    # version nor default phrases reads English: ``test_text_languages``).
    await house_reads(session, "fr")
    r = await client.get("/login")
    assert '<html lang="fr">' in r.text
    assert in_language("fr", "login.submit") in r.text
    assert in_language("fr", "login.username") in r.text


# --- logging in ------------------------------------------------------------------------------


async def test_login_sets_secure_cookie(client, owner_account):
    r = await client.post("/login", data={"username": "owner", "password": OWNER_PASSWORD})
    assert r.status_code == 303
    cookie = r.headers["set-cookie"].lower()
    assert "secure" in cookie and "httponly" in cookie and "samesite=strict" in cookie


async def test_login_redirects_to_the_chat_with_the_exact_cookie_attributes(
    client, owner_account, session, settings
):
    r = await login(client)
    assert r.status_code == 303
    assert r.headers["location"] == "/chat"
    attributes = cookie_attributes(r)
    assert {"secure", "httponly", "samesite", "path", "max-age"} <= attributes.keys()
    assert attributes["samesite"].lower() == "strict"
    assert attributes["path"] == "/"
    assert "domain" not in attributes  # host-only: not shared with sibling hosts
    assert int(attributes["max-age"]) == settings.session_ttl_days * 24 * 3600


async def test_the_cookie_lifetime_is_the_session_ttl(
    client, app, owner_account, settings, session
):
    app.dependency_overrides[get_settings] = lambda: settings.model_copy(
        update={"session_ttl_days": 7}
    )
    before = datetime.now(UTC)
    r = await login(client)
    after = datetime.now(UTC)
    assert int(cookie_attributes(r)["max-age"]) == 7 * 24 * 3600
    expires_at = (await session.execute(select(WebSession.expires_at))).scalar_one()
    assert before + timedelta(days=7) <= expires_at <= after + timedelta(days=7)


async def test_the_session_is_stored_hashed_and_matches_the_cookie(client, owner_account, session):
    r = await login(client)
    token = cookie_attributes(r)[SESSION_COOKIE]
    row = (await session.execute(select(WebSession))).scalar_one()
    assert row.account_id == owner_account.id
    assert token not in row.token_hash
    assert row.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert len(token) >= 43


async def test_a_new_login_makes_a_new_session(client, owner_account, session):
    first = cookie_attributes(await login(client))[SESSION_COOKIE]
    client.cookies.clear()  # another browser: it presents no session, so none is replaced
    second = cookie_attributes(await login(client))[SESSION_COOKIE]
    assert first != second
    assert await count_sessions(session) == 2


async def test_the_username_is_normalised(client, owner_account):
    for username in ("OWNER", "  owner  ", "Owner\t"):
        r = await login(client, username=username)
        assert r.status_code == 303, repr(username)


async def test_the_password_is_not_normalised(client, owner_account):
    for password in (OWNER_PASSWORD.upper(), OWNER_PASSWORD + " ", " " + OWNER_PASSWORD):
        assert (await login(client, password=password)).status_code == 400


async def test_a_logged_in_person_can_use_the_session(client, owner_account):
    await login(client)  # the client keeps the cookie, as a browser does
    me = await client.get("/whoami")
    assert me.status_code == 200
    assert me.json()["role"] == "owner"


# --- refusing --------------------------------------------------------------------------------


async def test_a_wrong_password_is_refused_without_a_cookie(client, owner_account, session):
    r = await login(client, password=BAD_PASSWORD)
    assert r.status_code == 400
    assert "set-cookie" not in r.headers
    assert t("login.error.invalid", "en") in r.text
    assert await count_sessions(session) == 0


@pytest.mark.parametrize("left", ["removed", "stay_over"])
async def test_someone_who_left_the_household_cannot_log_in(
    client, session, add_person, add_account, owner_password_hash, left
):
    """A removed person, or a guest whose stay is over, gets the answer of a wrong password,
    with the right one (task 9): no session, and nothing that tells why."""
    before = datetime.now(UTC) - timedelta(minutes=1)
    person = await add_person("Alex", "adult" if left == "removed" else "guest")
    if left == "removed":
        person.disabled_at = before
    else:
        person.expires_at = before
    await session.flush()
    await add_account(person, "alex", owner_password_hash)
    r = await login(client, "alex", OWNER_PASSWORD)
    wrong = await login(client, "alex", BAD_PASSWORD)
    assert r.status_code == wrong.status_code == 400
    assert "set-cookie" not in r.headers
    assert t("login.error.invalid", "en") in r.text
    assert r.text == wrong.text
    assert await count_sessions(session) == 0
    actions = [(e.action, e.details) for e in await audit_entries(session)]
    assert actions == [("auth.login_failed", {"username": "alex"})] * 2


async def test_the_error_is_the_same_whether_or_not_the_username_exists(client, owner_account):
    known = await login(client, password=BAD_PASSWORD)
    unknown = await login(client, username="nobody-here", password=BAD_PASSWORD)
    assert known.status_code == unknown.status_code == 400
    message = t("login.error.invalid", "en")
    assert message in known.text and message in unknown.text

    def strip_username(html: str, username: str) -> str:
        return html.replace(f'value="{username}"', 'value=""')

    # Apart from the username that is typed back into the form, the pages are identical.
    assert strip_username(known.text, "owner") == strip_username(unknown.text, "nobody-here")


async def test_an_unknown_username_costs_a_verification_too(
    client, owner_account, monkeypatch: pytest.MonkeyPatch
):
    verified: list[str] = []
    dummies: list[str] = []
    real_verify = account_routes.verify_password_limited

    async def verify(password_hash: str, plain: str) -> bool:
        verified.append(plain)
        return await real_verify(password_hash, plain)

    async def dummy(plain: str) -> None:
        dummies.append(plain)

    monkeypatch.setattr(account_routes, "verify_password_limited", verify)
    monkeypatch.setattr(account_routes, "dummy_verify_limited", dummy)
    await login(client, username="nobody-here", password=BAD_PASSWORD)
    assert (verified, dummies) == ([], [BAD_PASSWORD])
    verified.clear()
    dummies.clear()
    await login(client, password=BAD_PASSWORD)
    assert (verified, dummies) == ([BAD_PASSWORD], [])


async def test_a_username_the_database_cannot_hold_is_just_unknown(client, owner_account):
    r = await login(client, username="ow\x00ner", password=BAD_PASSWORD)  # PostgreSQL has no NUL
    assert r.status_code == 400
    assert (await login(client)).status_code == 303  # and nothing broke


async def test_the_typed_username_is_echoed_escaped(client):
    payload = '"><script>alert(1)</script>'
    r = await login(client, username=payload, password=BAD_PASSWORD)
    assert payload not in r.text
    assert "&lt;script&gt;" in r.text


async def test_the_password_is_never_echoed(client, owner_account):
    r = await login(client, password=BAD_PASSWORD)
    assert BAD_PASSWORD not in r.text


@pytest.mark.parametrize("data", [{}, {"username": "owner"}, {"password": BAD_PASSWORD}])
async def test_missing_fields_are_a_refused_login_not_a_validation_error(client, data):
    r = await client.post("/login", data=data)
    assert r.status_code == 400
    assert "text/html" in r.headers["content-type"]
    assert t("login.error.invalid", "en") in r.text


async def test_the_refusal_is_translated(client, owner_account, session):
    await house_reads(session, "fr")
    r = await login(client, password=BAD_PASSWORD)
    assert in_language("fr", "login.error.invalid") in r.text


# --- throttling ------------------------------------------------------------------------------


async def test_login_throttled_after_5_failures(client, owner_account):
    for _ in range(5):
        await client.post("/login", data={"username": "owner", "password": "bad-password-x"})
    r = await client.post("/login", data={"username": "owner", "password": OWNER_PASSWORD})
    assert r.status_code == 429


async def test_the_fifth_failure_is_still_a_normal_refusal(client, owner_account):
    statuses = [(await login(client, password=BAD_PASSWORD)).status_code for _ in range(5)]
    assert statuses == [400] * MAX_FAILED_ATTEMPTS


async def test_a_throttled_attempt_says_when_to_come_back(client, owner_account, session):
    for _ in range(5):
        await login(client, password=BAD_PASSWORD)
    r = await login(client)
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) == WINDOW.total_seconds()
    assert "set-cookie" not in r.headers
    assert await count_sessions(session) == 0
    assert t("login.error.throttled", "en", minutes="15") in r.text
    await house_reads(session, "fr")
    french = await login(client)
    assert in_language("fr", "login.error.throttled", minutes="15") in french.text


async def test_a_throttled_attempt_does_not_check_the_password(
    client, owner_account, monkeypatch: pytest.MonkeyPatch
):
    for _ in range(5):
        await login(client, password=BAD_PASSWORD)

    async def fail(*args: object) -> bool:
        raise AssertionError("no verification while throttled")

    monkeypatch.setattr(account_routes, "verify_password_limited", fail)
    monkeypatch.setattr(account_routes, "dummy_verify_limited", fail)
    assert (await login(client)).status_code == 429


async def test_the_right_password_works_again_once_the_window_has_passed(
    client, owner_account, clock
):
    for _ in range(5):
        await login(client, password=BAD_PASSWORD)
    assert (await login(client)).status_code == 429
    clock.advance(WINDOW - timedelta(seconds=1))
    assert (await login(client)).status_code == 429
    clock.advance(timedelta(seconds=2))
    r = await login(client)
    assert r.status_code == 303


async def test_throttled_attempts_do_not_extend_the_lockout(client, owner_account, clock):
    for _ in range(5):
        await login(client, password=BAD_PASSWORD)
    for _ in range(14):  # hammering during the lockout...
        clock.advance(timedelta(minutes=1))
        assert (await login(client, password=BAD_PASSWORD)).status_code == 429
    clock.advance(timedelta(minutes=1, seconds=1))
    assert (await login(client)).status_code == 303  # ...does not delay its end


async def test_an_unknown_username_is_throttled_like_a_known_one(client, owner_account):
    for _ in range(5):
        assert (await login(client, username="ghost", password=BAD_PASSWORD)).status_code == 400
    assert (await login(client, username="ghost", password=BAD_PASSWORD)).status_code == 429


async def test_the_throttle_is_per_username(client, owner_account):
    for _ in range(6):
        await login(client, username="ghost", password=BAD_PASSWORD)
    assert (await login(client)).status_code == 303


async def test_the_throttle_key_is_the_normalised_username(client, owner_account):
    for username in ("owner", "OWNER", " Owner ", "owner", "OWNER"):
        await login(client, username=username, password=BAD_PASSWORD)
    assert (await login(client, username="  oWnEr")).status_code == 429


async def test_a_good_login_resets_the_count(client, owner_account):
    for _ in range(2):
        for _ in range(4):
            await login(client, password=BAD_PASSWORD)
        assert (await login(client)).status_code == 303


async def test_a_burst_of_parallel_attempts_cannot_beat_the_limit(settings, clock, client_factory):
    """The attempt is counted before the (slow) verification, not after: twelve attempts sent at
    once must not all get through."""
    app = create_app(settings)
    app.state.login_throttle = LoginThrottle(clock=clock)
    async with app.router.lifespan_context(app), client_factory(app) as http:
        responses = await asyncio.gather(
            *(login(http, username="ghost", password=f"guess-number-{i}") for i in range(12))
        )
    assert sorted(r.status_code for r in responses) == [400] * 5 + [429] * 7


# --- logging out -----------------------------------------------------------------------------


async def test_cross_origin_post_is_refused(client):
    r = await client.post("/logout", headers={"origin": "https://evil.example.net"})
    assert r.status_code == 403


async def test_logout_revokes_the_session_and_clears_the_cookie(client, owner_account, session):
    cookie = cookie_attributes(await login(client))[SESSION_COOKIE]
    assert (await client.get("/whoami")).status_code == 200

    r = await client.post("/logout")
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
    attributes = cookie_attributes(r)
    assert attributes[SESSION_COOKIE] in ("", '""')
    assert attributes["max-age"] == "0"
    assert attributes["path"] == "/"
    assert {"secure", "httponly"} <= attributes.keys()
    assert await count_sessions(session) == 0  # revoked on the server, not just forgotten

    assert (await client.get("/whoami")).status_code == 303  # the browser forgot it...
    stolen = await client.get("/whoami", headers={"cookie": f"{SESSION_COOKIE}={cookie}"})
    assert stolen.status_code == 303  # ...and a stolen copy is worthless


async def test_logout_only_revokes_the_session_of_the_request(client, owner_account, session):
    mine = cookie_attributes(await login(client))[SESSION_COOKIE]
    await login(client)  # another browser
    await client.post("/logout", headers={"cookie": f"{SESSION_COOKIE}={mine}"})
    assert await count_sessions(session) == 1


# --- logging in again ------------------------------------------------------------------------


def with_cookie(token: str) -> dict[str, str]:
    return {"cookie": f"{SESSION_COOKIE}={token}"}


async def test_logging_in_again_ends_the_session_that_was_presented(client, owner_account, session):
    first = cookie_attributes(await login(client))[SESSION_COOKIE]
    second = cookie_attributes(await login(client))[SESSION_COOKIE]  # sends the first cookie
    assert first != second
    assert await count_sessions(session) == 1  # not one more per login
    assert (await client.get("/whoami", headers=with_cookie(first))).status_code == 303
    assert (await client.get("/whoami", headers=with_cookie(second))).status_code == 200


async def test_logging_in_ends_only_the_session_that_was_presented(client, owner_account, session):
    other_browser = cookie_attributes(await login(client))[SESSION_COOKIE]
    client.cookies.clear()
    await login(client)  # this browser had no session
    assert await count_sessions(session) == 2
    assert (await client.get("/whoami", headers=with_cookie(other_browser))).status_code == 200


async def test_a_failed_login_leaves_the_session_that_was_presented(client, owner_account, session):
    first = cookie_attributes(await login(client))[SESSION_COOKIE]
    r = await login(client, password=BAD_PASSWORD)
    assert r.status_code == 400
    assert await count_sessions(session) == 1
    assert (await client.get("/whoami", headers=with_cookie(first))).status_code == 200


@pytest.mark.parametrize("cookie", ["garbage", "x" * 5000])
async def test_logging_in_with_a_cookie_that_means_nothing_still_works(
    client, owner_account, session, cookie
):
    r = await login(client, headers=with_cookie(cookie))
    assert r.status_code == 303
    assert await count_sessions(session) == 1


async def test_logging_in_clears_the_expired_sessions(client, owner_account, session):
    session.add(
        WebSession(
            account_id=owner_account.id,
            token_hash="b" * 64,
            expires_at=datetime.now(UTC) - timedelta(days=1),
        )
    )
    await session.flush()
    await login(client)
    assert await count_sessions(session) == 1  # the new one only


@pytest.mark.parametrize("cookie", [None, "garbage", "x" * 5000])
async def test_logout_without_a_valid_session_still_redirects_to_the_login(client, cookie):
    headers = {} if cookie is None else {"cookie": f"{SESSION_COOKIE}={cookie}"}
    r = await client.post("/logout", headers=headers)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


# --- the whole stack, with real commits ------------------------------------------------------


async def test_login_and_logout_persist_through_the_real_session_factory(
    settings, session_factory, clock, client_factory, add_owner_in_database
):
    """The other tests share one rolled-back session: this one checks that the application
    commits what it must, with a session per request as in production."""
    await add_owner_in_database()
    app = create_app(settings)
    app.state.login_throttle = LoginThrottle(clock=clock)
    add_protected_routes(app)
    async with app.router.lifespan_context(app), client_factory(app) as http:
        r = await login(http)
        assert r.status_code == 303
        async with session_factory() as other:  # another connection: only committed rows
            assert await count_sessions(other) == 1
            first_seen = (await other.execute(select(WebSession.last_seen_at))).scalar_one()
        assert (await http.get("/whoami")).status_code == 200
        async with session_factory() as other:  # using the session is recorded for real too
            last_seen = (await other.execute(select(WebSession.last_seen_at))).scalar_one()
        assert last_seen > first_seen
        assert (await http.post("/logout")).status_code == 303
        async with session_factory() as other:
            assert await count_sessions(other) == 0
        assert (await http.get("/whoami")).status_code == 303
