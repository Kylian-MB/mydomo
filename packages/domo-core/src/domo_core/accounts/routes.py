"""The login page, the login itself and the logout.

The page also offers to sign in with a passkey where the request can use one (a host name, see
``accounts.passkeys``); ``accounts.passkey_routes`` does that login, with the same throttle, the
same failure answer and the same session as this one.

Logging in does not reveal whether a username exists: an unknown username gets the same answer as
a wrong password, after the same amount of work (one argon2 verification). Failed attempts are
throttled per username, existing or not. Someone who is no longer in the household (removed, or a
guest whose stay is over, at the time of ``get_clock``) gets that answer too, with the right
password: their sessions would not count anyway (``accounts.sessions``).

A login works in two transactions, with the verification between them. The first reads the
account and ends before the password is verified: argon2 is slow and waits for its turn
(``accounts.passwords``: at most ``VERIFY_CONCURRENCY`` at once), and a burst of logins that each
held a connection of the database all that time would empty the pool. The second writes. It opens
the session only if the account still has the password hash that was verified, and its person is
still in the household (a compare-and-set, R83). The account's row is locked (``FOR SHARE``) until
it commits, so a password change made meanwhile either ended first (and the login fails, like a
wrong password) or waits for it (and then ends its session with the other ones). When too many
verifications wait already, the login is refused at once (503, ``Retry-After``), the same way
whether the username exists or not, and nothing is written.

Logging in, failing to and logging out are audited. The entry is written in the transaction of the
change it describes and committed with it: a login whose entry cannot be written does not happen.
A login ends the session the browser presented, if any (the new one replaces it); a failed login
leaves it alone.
A failed login is audited too (no session, but the entry is committed); it keeps the username
only when it names an existing account, otherwise just that it did not. The audit entry is
appended after the slow password verification, never before: writers of the log wait for each
other.
"""

import math
from collections.abc import Callable
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import RedirectResponse, Response

from domo_core.accounts.models import Account, Person
from domo_core.accounts.passkeys import passkey_availability_of
from domo_core.accounts.passwords import (
    Argon2Busy,
    busy_headers,
    dummy_verify_limited,
    verify_password_limited,
)
from domo_core.accounts.sessions import (
    SESSION_COOKIE,
    active_at,
    clear_session_cookie,
    create_session,
    revoke_session,
    set_session_cookie,
)
from domo_core.accounts.throttle import LoginThrottle
from domo_core.accounts.usernames import normalize_username
from domo_core.audit.log import append_audit
from domo_core.config import CoreSettings
from domo_core.web.deps import get_clock, get_session, get_settings
from domo_core.web.templating import render

router = APIRouter()

# After a login: where the person lands.
HOME_PATH = "/chat"
LOGIN_PATH = "/login"


def get_login_throttle(request: Request) -> LoginThrottle:
    """The throttle of the application (one per process). Tests replace it to control time."""
    throttle: LoginThrottle = request.app.state.login_throttle
    return throttle


def render_login_page(
    request: Request,
    settings: CoreSettings,
    *,
    status_code: int = 200,
    error_key: str | None = None,
    error_params: dict[str, str] | None = None,
    username: str = "",
    headers: dict[str, str] | None = None,
) -> Response:
    """The login page: the password form and, where the request can use them, the passkey
    button (``accounts.passkeys``); on an IP address, why there is none."""
    return render(
        request,
        "login.html",
        status_code=status_code,
        headers=headers,
        error_key=error_key,
        error_params=error_params or {},
        username=username,
        passkeys=passkey_availability_of(request, settings),
    )


async def _may_still_sign_in(
    session: AsyncSession, account: Account, verified_hash: str, now: datetime
) -> bool:
    """Whether ``account`` still has the password hash that was verified, and its person is still
    in the household at ``now``. The account's row stays locked (``FOR SHARE``) until the
    transaction ends: a password change waits for the login to commit (see the module
    docstring)."""
    found = await session.execute(
        select(Account.id)
        .join(Person, Person.id == Account.person_id)
        .where(Account.id == account.id, Account.password_hash == verified_hash, active_at(now))
        .with_for_update(read=True, of=Account)
    )
    return found.scalar_one_or_none() is not None


@router.get(LOGIN_PATH)
async def login_page(
    request: Request, settings: Annotated[CoreSettings, Depends(get_settings)]
) -> Response:
    return render_login_page(request, settings)


@router.post(LOGIN_PATH)
async def login(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[CoreSettings, Depends(get_settings)],
    throttle: Annotated[LoginThrottle, Depends(get_login_throttle)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
    username: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
) -> Response:
    name = normalize_username(username)
    # Counted now, before the slow verification (see ``LoginThrottle``); forgotten on success.
    wait = throttle.register_attempt(name)
    if wait is not None:
        return render_login_page(
            request,
            settings,
            status_code=429,
            error_key="login.error.throttled",
            username=username,
            error_params={"minutes": str(math.ceil(wait / 60))},
            headers={"Retry-After": str(math.ceil(wait))},
        )

    # PostgreSQL cannot store a NUL character: no username has one, and the query would fail.
    account = None
    if "\x00" not in name:
        found = await session.execute(select(Account).where(Account.username == name))
        account = found.scalar_one_or_none()
    # The first transaction ends here: nothing is held while argon2 runs or waits for its turn.
    await session.commit()
    verified_hash = "" if account is None else account.password_hash
    try:
        if account is None:
            await dummy_verify_limited(password)
            verified = False
        else:
            verified = await verify_password_limited(verified_hash, password)
    except Argon2Busy:
        # Too many logins wait already: refused at once, the same way whatever the username, and
        # nothing was decided, so nothing is written.
        return render_login_page(
            request,
            settings,
            status_code=503,
            error_key="passwords.busy",
            username=username,
            headers=busy_headers(),
        )
    # The second one starts with the compare-and-set (R83), then opens the session.
    if (
        verified
        and account is not None
        and not await _may_still_sign_in(session, account, verified_hash, clock())
    ):
        verified = False
    if account is None or not verified:
        # What a visitor typed into the username box can be anything, a password included, and
        # the log is append-only: the username is kept only when it names an account.
        details: dict[str, str | bool] = (
            {"username_known": False} if account is None else {"username": account.username}
        )
        await append_audit(session, actor="system", action="auth.login_failed", details=details)
        await session.commit()
        return render_login_page(
            request, settings, status_code=400, error_key="login.error.invalid", username=username
        )

    # Logging in again from a browser that has a session replaces it, in the same transaction:
    # the old token must not stay valid next to the new one.
    presented = request.cookies.get(SESSION_COOKIE)
    if presented:
        await revoke_session(session, presented)
    token = await create_session(session, account.id, ttl=settings.session_ttl)
    await append_audit(
        session, actor=f"person:{account.person_id}", action="auth.login", details={}
    )
    await session.commit()
    throttle.clear(name)
    response = RedirectResponse(HOME_PATH, status_code=303)
    set_session_cookie(response, token, settings.session_ttl)
    return response


@router.post("/logout")
async def logout(
    request: Request, session: Annotated[AsyncSession, Depends(get_session)]
) -> Response:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        person_id = await revoke_session(session, token)
        if person_id is not None:
            await append_audit(
                session, actor=f"person:{person_id}", action="auth.logout", details={}
            )
        await session.commit()
    response = RedirectResponse(LOGIN_PATH, status_code=303)
    clear_session_cookie(response)
    return response
