"""A flood of logins must not exhaust the server (task 14).

A burst of logins, each with a username of its own so that the per-username throttle never stops
it, used to run as many argon2 verifications at once (64 MiB each) and to hold a connection of the
database through each one, until the pool ran dry. Now every argon2 verification and hash a request
asks for goes through one limited path (``accounts.passwords``): at most ``VERIFY_CONCURRENCY`` run
at once, in threads of their own, and the others wait their turn. Neither those that wait nor
those that run hold a connection: the request reads what it needs in a transaction that it ends
first, and opens another one for what it writes. The login (and the fake verification of an
unknown username: R48), the account page (R50, R82), adding a passkey (R87), joining with an
invitation (R79) and the owner's creation all take that path.

The line is bounded, and so is the wait (fix round 1): beyond ``MAX_WAITING_HASHES`` waiting
requests, or after ``HASH_WAIT_TIMEOUT_S`` in the line, a request is refused at once (503 with
``Retry-After``, the same answer whatever its username) and nothing of it is hashed.

The tests of connections use ``app_factory()``: a session per request, from the test engine's
pool, as in production. The shared ``session`` of the ``app`` fixture would hide them.
"""

import asyncio
import html
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

import httpx
import pytest
from domo_common.i18n import t
from domo_core.accounts import passwords
from domo_core.accounts import routes as login_routes
from domo_core.accounts.models import Account, WebSession
from domo_core.accounts.passwords import (
    BUSY_RETRY_AFTER_S,
    VERIFY_CONCURRENCY,
    hash_password,
    waiting_hashes,
)
from domo_core.household.service import create_invitation
from domo_core.setup_flow.owner_routes import SETUP_CODE_MAX_ATTEMPTS
from fastapi import FastAPI
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from web_support import (
    ADULT_PASSWORD,
    ADULT_USERNAME,
    OWNER_PASSWORD,
    OWNER_USERNAME,
    HashProbe,
    audit_actions,
    log_in,
    wait_until,
)

FLOOD_PASSWORD = "x" * 12
NEW_PASSWORD = "another-long-one"
BY_NAME_URL = "https://localhost:8443"

SlowHash = Callable[..., AbstractAsyncContextManager[HashProbe]]
ClientFactory = Callable[..., httpx.AsyncClient]
Connections = Callable[[FastAPI], int]


def flood_login(client: httpx.AsyncClient, index: int) -> Awaitable[httpx.Response]:
    """A login with a username nobody has (each one its own: the throttle never stops them)."""
    return client.post("/login", data={"username": f"u{index}", "password": FLOOD_PASSWORD})


# --- the brief's tests ---------------------------------------------------------------------------


async def test_login_does_not_hold_a_db_connection_while_hashing(
    app_factory: Callable[[], FastAPI],
    client_factory: ClientFactory,
    add_owner_in_database: Callable[[], Awaitable[Account]],
    slow_hash: SlowHash,
    max_checked_out_connections: Connections,
):
    await add_owner_in_database()
    app = app_factory()

    async def attempt(client: httpx.AsyncClient, index: int) -> httpx.Response:
        # One every 25 ms: twenty reads at the very same instant would take up to the pool's
        # fifteen connections for a few milliseconds, whatever the login does next. What matters
        # here is what is held during the half second that each hash takes.
        await asyncio.sleep(index * 0.025)
        return await flood_login(client, index)

    async with slow_hash(seconds=0.5), client_factory(app) as client:
        responses = await asyncio.gather(*(attempt(client, index) for index in range(20)))
    assert [r.status_code for r in responses] == [400] * 20
    assert max_checked_out_connections(app) <= 5


async def test_at_most_four_hashes_run_at_once(
    app_factory: Callable[[], FastAPI], client_factory: ClientFactory, slow_hash: SlowHash
):
    app = app_factory()
    async with slow_hash(seconds=0.3) as probe, client_factory(app) as client:
        responses = await asyncio.gather(*(flood_login(client, index) for index in range(12)))
    assert [r.status_code for r in responses] == [400] * 12
    assert VERIFY_CONCURRENCY == 4
    assert probe.max_concurrent == VERIFY_CONCURRENCY
    assert probe.started == 12  # an unknown username costs a verification too (R48)


# --- every request that hashes waits in the same line, without a connection ----------------------


@asynccontextmanager
async def argon2_taken(
    app: FastAPI, client_factory: ClientFactory, slow_hash: SlowHash
) -> AsyncIterator[HashProbe]:
    """Inside the block, every place of argon2 is taken by a login with an unknown username, held
    until the block ends: whatever the test sends meanwhile has to wait for its turn."""
    async with slow_hash(gated=True) as probe, client_factory(app) as fillers:
        tasks = [
            asyncio.create_task(flood_login(fillers, index)) for index in range(VERIFY_CONCURRENCY)
        ]
        try:
            await wait_until(
                lambda: probe.running == VERIFY_CONCURRENCY, "every place of argon2 is taken"
            )
            yield probe
        finally:
            probe.release()
            responses = await asyncio.gather(*tasks)
        assert [r.status_code for r in responses] == [400] * VERIFY_CONCURRENCY


async def waits_its_turn_without_a_connection(
    app: FastAPI,
    probe: HashProbe,
    request: Awaitable[httpx.Response],
    checked_out_connections: Connections,
) -> httpx.Response:
    """Send ``request`` while argon2 is taken (``argon2_taken``): it must wait in the line of the
    limited path, and nothing (neither it, nor the requests whose hashes run) may hold a
    connection of the database meanwhile. Then let everything through, and return its answer."""
    sent = asyncio.ensure_future(request)
    try:
        await wait_until(
            lambda: probe.queued > VERIFY_CONCURRENCY or sent.done(),
            "the request waits for its turn of argon2",
        )
        assert not sent.done(), sent.result().text
        assert probe.started == VERIFY_CONCURRENCY  # it waits: it does not run
        assert checked_out_connections(app) == 0
    finally:
        probe.release()
    return await sent


async def test_a_known_username_waits_in_the_same_line(
    app_factory: Callable[[], FastAPI],
    client_factory: ClientFactory,
    add_owner_in_database: Callable[[], Awaitable[Account]],
    slow_hash: SlowHash,
    checked_out_connections: Connections,
):
    await add_owner_in_database()
    app = app_factory()
    async with (
        client_factory(app) as owner,
        argon2_taken(app, client_factory, slow_hash) as probe,
    ):
        login = owner.post("/login", data={"username": OWNER_USERNAME, "password": OWNER_PASSWORD})
        r = await waits_its_turn_without_a_connection(app, probe, login, checked_out_connections)
    assert r.status_code == 303


async def test_a_password_change_waits_in_the_same_line(
    app_factory: Callable[[], FastAPI],
    client_factory: ClientFactory,
    add_owner_in_database: Callable[[], Awaitable[Account]],
    slow_hash: SlowHash,
    checked_out_connections: Connections,
):
    """R50, R82: both argon2 steps of the account page (verifying the current password, hashing
    the new one) take the limited path."""
    await add_owner_in_database()
    app = app_factory()
    async with client_factory(app) as owner:
        await log_in(owner)
        async with argon2_taken(app, client_factory, slow_hash) as probe:
            change = owner.post(
                "/account/password",
                data={"current": OWNER_PASSWORD, "new": NEW_PASSWORD, "confirm": NEW_PASSWORD},
            )
            r = await waits_its_turn_without_a_connection(
                app, probe, change, checked_out_connections
            )
        assert (r.status_code, r.headers["location"]) == (303, "/account?saved=password")
        assert probe.queued == VERIFY_CONCURRENCY + 2  # the verification, then the new hash


async def test_adding_a_passkey_waits_in_the_same_line(
    app_factory: Callable[[], FastAPI],
    client_factory: ClientFactory,
    add_owner_in_database: Callable[[], Awaitable[Account]],
    slow_hash: SlowHash,
    checked_out_connections: Connections,
):
    """R87: the password asked before adding a passkey."""
    await add_owner_in_database()
    app = app_factory()
    async with client_factory(app, origin=BY_NAME_URL, base_url=BY_NAME_URL) as owner:
        await log_in(owner)
        async with argon2_taken(app, client_factory, slow_hash) as probe:
            options = owner.post("/account/passkeys/options", data={"current": OWNER_PASSWORD})
            r = await waits_its_turn_without_a_connection(
                app, probe, options, checked_out_connections
            )
    assert r.status_code == 200 and "challenge" in r.json()


async def test_joining_waits_in_the_same_line(
    app_factory: Callable[[], FastAPI],
    client_factory: ClientFactory,
    add_owner_in_database: Callable[[], Awaitable[Account]],
    session_factory: async_sessionmaker[AsyncSession],
    clock,
    slow_hash: SlowHash,
    checked_out_connections: Connections,
):
    """R79: the password of someone who joins with an invitation is hashed on the limited path,
    after the checks' transaction has ended."""
    owner = await add_owner_in_database()
    async with session_factory() as writer:
        _, token = await create_invitation(
            writer,
            display_name="Alex",
            role="adult",
            guest_days=None,
            created_by=owner.person_id,
            now=clock(),
        )
        await writer.commit()
    app = app_factory()
    async with (
        client_factory(app) as newcomer,
        argon2_taken(app, client_factory, slow_hash) as probe,
    ):
        join = newcomer.post(
            f"/invite/{token}", data={"username": "alex", "password": NEW_PASSWORD}
        )
        r = await waits_its_turn_without_a_connection(app, probe, join, checked_out_connections)
    assert (r.status_code, r.headers["location"]) == (303, "/chat")


async def test_creating_the_owner_waits_in_the_same_line(
    app_factory: Callable[[], FastAPI],
    client_factory: ClientFactory,
    setup_code: str,
    slow_hash: SlowHash,
    checked_out_connections: Connections,
):
    """The first launch hashes the owner's password too, after the setup code was checked."""
    app = app_factory()
    form = {
        "code": setup_code,
        "display_name": "Owner",
        "username": OWNER_USERNAME,
        "password": OWNER_PASSWORD,
    }
    async with client_factory(app) as owner, argon2_taken(app, client_factory, slow_hash) as probe:
        create = owner.post("/setup/owner", data=form)
        r = await waits_its_turn_without_a_connection(app, probe, create, checked_out_connections)
    assert (r.status_code, r.headers["location"]) == (303, "/setup/model")


# --- a password that changes while a login verifies the old one (R83) ----------------------------


async def test_a_login_whose_password_changed_meanwhile_opens_no_session(
    app_factory: Callable[[], FastAPI],
    client_factory: ClientFactory,
    add_owner_in_database: Callable[[], Awaitable[Account]],
    session_factory: async_sessionmaker[AsyncSession],
    slow_hash: SlowHash,
):
    """The login verifies the old password while the password changes (and the change ends
    every other session): the session is opened only if the stored hash is still the one that
    was verified, so a login with the old password cannot outlive the change."""
    owner = await add_owner_in_database()
    newer_hash = hash_password(NEW_PASSWORD)  # before the slow argon2 of the test
    app = app_factory()
    async with client_factory(app) as browser, slow_hash(gated=True) as probe:
        login = asyncio.ensure_future(
            browser.post("/login", data={"username": OWNER_USERNAME, "password": OWNER_PASSWORD})
        )
        await wait_until(lambda: probe.running == 1, "the old password is being verified")
        async with session_factory() as writer:
            await writer.execute(
                update(Account).where(Account.id == owner.id).values(password_hash=newer_hash)
            )
            await writer.commit()
        probe.release()
        r = await login
    assert r.status_code == 400 and "set-cookie" not in r.headers
    async with session_factory() as reader:
        sessions = await reader.execute(
            select(func.count()).select_from(WebSession).where(WebSession.account_id == owner.id)
        )
        assert sessions.scalar_one() == 0
        assert (await audit_actions(reader))[-1] == "auth.login_failed"


@pytest.mark.parametrize("password", [OWNER_PASSWORD, FLOOD_PASSWORD])
async def test_a_login_reads_and_writes_in_two_transactions(
    app_factory: Callable[[], FastAPI],
    client_factory: ClientFactory,
    add_owner_in_database: Callable[[], Awaitable[Account]],
    slow_hash: SlowHash,
    checked_out_connections: Connections,
    password: str,
):
    """Whatever the outcome (signed in, or refused), nothing is held while the password is
    verified, even when argon2 has room."""
    await add_owner_in_database()
    app = app_factory()
    async with client_factory(app) as browser, slow_hash(gated=True) as probe:
        login = asyncio.ensure_future(
            browser.post("/login", data={"username": OWNER_USERNAME, "password": password})
        )
        try:
            await wait_until(lambda: probe.running == 1, "the password is being verified")
            assert checked_out_connections(app) == 0
        finally:
            probe.release()
        r = await login
    assert r.status_code == (303 if password == OWNER_PASSWORD else 400)


# --- the line is bounded, and so is the wait (fix round 1) ---------------------------------------


def page_text(response: httpx.Response) -> str:
    """What a person reads in a page."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", response.text)).split())


def busy(response: httpx.Response) -> bool:
    """Whether ``response`` is the answer of a full line: refused at once, come back later."""
    return (
        response.status_code == 503
        and response.headers.get("retry-after") == str(BUSY_RETRY_AFTER_S)
        and t("passwords.busy", "en") in page_text(response)
    )


def test_the_line_and_the_wait_are_bounded():
    assert passwords.MAX_WAITING_HASHES == 64
    assert passwords.HASH_WAIT_TIMEOUT_S == 30
    assert BUSY_RETRY_AFTER_S > 0


async def test_a_full_line_refuses_the_next_request_at_once(
    app_factory: Callable[[], FastAPI],
    client_factory: ClientFactory,
    slow_hash: SlowHash,
    monkeypatch: pytest.MonkeyPatch,
):
    """Every place of argon2 is taken and the line is full: the next request is refused at once,
    nothing of it is queued, and the requests in the line still get their turn."""
    app = app_factory()
    async with (
        client_factory(app) as client,
        argon2_taken(app, client_factory, slow_hash) as probe,
    ):
        # Set once the four have started: until then they wait in the line too.
        monkeypatch.setattr(passwords, "MAX_WAITING_HASHES", 2)
        in_line = [asyncio.ensure_future(flood_login(client, index)) for index in (10, 11)]
        await wait_until(lambda: waiting_hashes() == 2, "two requests wait in the line")
        started = time.monotonic()
        refused = await flood_login(client, 12)
        assert time.monotonic() - started < 2
        assert busy(refused)
        assert probe.queued == VERIFY_CONCURRENCY + 2  # the refused one was never handed over
        probe.release()
        assert [r.status_code for r in await asyncio.gather(*in_line)] == [400, 400]
    assert waiting_hashes() == 0


async def test_a_request_that_waits_too_long_is_refused(
    app_factory: Callable[[], FastAPI],
    client_factory: ClientFactory,
    add_owner_in_database: Callable[[], Awaitable[Account]],
    slow_hash: SlowHash,
    monkeypatch: pytest.MonkeyPatch,
):
    """Its place in the line is given up when the wait is over: its hash never runs."""
    monkeypatch.setattr(passwords, "HASH_WAIT_TIMEOUT_S", 0.3)
    await add_owner_in_database()
    app = app_factory()
    async with (
        client_factory(app) as owner,
        argon2_taken(app, client_factory, slow_hash) as probe,
    ):
        r = await owner.post(
            "/login", data={"username": OWNER_USERNAME, "password": OWNER_PASSWORD}
        )
        assert busy(r) and "set-cookie" not in r.headers
        assert probe.queued == VERIFY_CONCURRENCY + 1 and waiting_hashes() == 0
    # The held hashes went through (they had started); the one that gave up never ran.
    assert probe.started == VERIFY_CONCURRENCY


async def test_known_and_unknown_usernames_get_the_same_answer_when_the_line_is_full(
    client, owner_account, session, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(passwords, "MAX_WAITING_HASHES", 0)
    before = await audit_actions(session)
    known = await client.post("/login", data={"username": "owner", "password": OWNER_PASSWORD})
    unknown = await client.post("/login", data={"username": "nobody", "password": OWNER_PASSWORD})
    assert busy(known) and busy(unknown)
    assert "set-cookie" not in known.headers
    assert known.text.replace('value="owner"', "") == unknown.text.replace('value="nobody"', "")
    assert await audit_actions(session) == before  # nothing was decided: nothing is written


async def test_a_full_line_is_explained_on_the_account_page(
    adult_client, adult_account, session, monkeypatch: pytest.MonkeyPatch
):
    account_id = adult_account.id
    query = select(Account.password_hash).where(Account.id == account_id)
    before = (await session.execute(query)).scalar_one()
    monkeypatch.setattr(passwords, "MAX_WAITING_HASHES", 0)
    r = await adult_client.post(
        "/account/password",
        data={"current": ADULT_PASSWORD, "new": NEW_PASSWORD, "confirm": NEW_PASSWORD},
    )
    assert busy(r)
    assert (await session.execute(query)).scalar_one() == before


async def test_a_full_line_is_explained_when_adding_a_passkey(
    app, client_factory: ClientFactory, adult_account, monkeypatch: pytest.MonkeyPatch
):
    async with client_factory(app, origin=BY_NAME_URL, base_url=BY_NAME_URL) as adult:
        await log_in(adult, ADULT_USERNAME, ADULT_PASSWORD)
        monkeypatch.setattr(passwords, "MAX_WAITING_HASHES", 0)
        r = await adult.post("/account/passkeys/options", data={"current": ADULT_PASSWORD})
    assert r.status_code == 503 and r.headers["retry-after"] == str(BUSY_RETRY_AFTER_S)
    assert r.json() == {"error": t("passwords.busy", "en")}


async def test_a_full_line_leaves_an_invitation_usable(
    client, owner_account, create_invitation_for_test, monkeypatch: pytest.MonkeyPatch
):
    _, token = await create_invitation_for_test(display_name="Sam")
    form = {"username": "sam", "password": NEW_PASSWORD}
    monkeypatch.setattr(passwords, "MAX_WAITING_HASHES", 0)
    assert busy(await client.post(f"/invite/{token}", data=form))
    monkeypatch.setattr(passwords, "MAX_WAITING_HASHES", 64)
    assert (await client.post(f"/invite/{token}", data=form)).status_code == 303


async def test_a_full_line_leaves_the_setup_code_usable(
    app_factory: Callable[[], FastAPI],
    client_factory: ClientFactory,
    setup_code: str,
    monkeypatch: pytest.MonkeyPatch,
):
    """The code was right: being refused for the load does not count as a wrong guess against the
    instance's few."""
    form = {
        "code": setup_code,
        "display_name": "Owner",
        "username": OWNER_USERNAME,
        "password": OWNER_PASSWORD,
    }
    async with client_factory(app_factory()) as owner:
        monkeypatch.setattr(passwords, "MAX_WAITING_HASHES", 0)
        for _ in range(SETUP_CODE_MAX_ATTEMPTS + 1):
            assert busy(await owner.post("/setup/owner", data=form))
        monkeypatch.setattr(passwords, "MAX_WAITING_HASHES", 64)
        r = await owner.post("/setup/owner", data=form)
    assert (r.status_code, r.headers["location"]) == (303, "/setup/model")


# --- a password change that comes while a login opens its session (R83, second half) ------------


async def until_a_lock_is_awaited_or(
    done: Callable[[], bool], session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Wait until a connection of the test database waits for a lock, or ``done()`` holds."""
    async with asyncio.timeout(10):
        while not done():
            async with session_factory() as watcher:
                waiting = await watcher.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                    )
                )
                if waiting.scalar_one():
                    return
            await asyncio.sleep(0.02)


async def test_a_password_change_during_a_login_ends_the_session_it_opens(
    app_factory: Callable[[], FastAPI],
    client_factory: ClientFactory,
    add_owner_in_database: Callable[[], Awaitable[Account]],
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
):
    """The login has checked the hash and is opening its session when the password changes. The
    account's row is locked until the login commits: the change waits for it, then ends that
    session with the other ones. Without the lock, the change would end the sessions it can see,
    and the new one, committed after it, would outlive the old password."""
    owner = await add_owner_in_database()
    app = app_factory()
    opening, go_on = asyncio.Event(), asyncio.Event()
    real_create_session = login_routes.create_session

    async def held_create_session(*args: Any, **kwargs: Any) -> str:
        opening.set()
        await go_on.wait()
        return await real_create_session(*args, **kwargs)

    async with client_factory(app) as changer, client_factory(app) as browser:
        await log_in(changer)
        monkeypatch.setattr(login_routes, "create_session", held_create_session)
        login = asyncio.ensure_future(
            browser.post("/login", data={"username": OWNER_USERNAME, "password": OWNER_PASSWORD})
        )
        async with asyncio.timeout(10):
            await opening.wait()
        change = asyncio.ensure_future(
            changer.post(
                "/account/password",
                data={"current": OWNER_PASSWORD, "new": NEW_PASSWORD, "confirm": NEW_PASSWORD},
            )
        )
        await until_a_lock_is_awaited_or(change.done, session_factory)
        go_on.set()
        signed_in, changed = await asyncio.gather(login, change)
        assert signed_in.status_code == 303  # the login did happen...
        assert changed.headers["location"] == "/account?saved=password"
        # ...but its session ended with the old password: that browser is signed out.
        assert (await browser.get("/chat")).headers["location"] == "/login"
        assert (await changer.get("/chat")).status_code == 200
    async with session_factory() as reader:
        sessions = await reader.execute(
            select(func.count()).select_from(WebSession).where(WebSession.account_id == owner.id)
        )
        assert sessions.scalar_one() == 1
