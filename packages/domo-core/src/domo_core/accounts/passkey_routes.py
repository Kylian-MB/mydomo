"""The passkey routes: adding and removing a passkey on the account page, and signing in with one.

What ``static/passkeys.js`` does with them: it asks for the options (``POST .../options``, a JSON
answer), hands them to ``navigator.credentials``, and posts the browser's answer, as JSON in the
``credential`` field, with the page's form. So the last step is a plain form post, answered like
the rest of the pages: a redirect when it worked, the page again with the reason when it did not.

Adding a passkey (signed-in people only)
----------------------------------------
* ``POST /account/passkeys/options`` (field ``current``): the person's password is asked for
  first, and checked like on the account page (argon2 on its limited path, with no connection of
  the database held meanwhile, R87; and the same count per account,
  ``account_routes.get_password_check_throttle``: not the login's username, which anyone may
  hammer, R89). A session alone is not enough to add a way in that outlives a password change: on
  a shared device left signed in, anyone could. Then the registration options
  (``accounts.passkeys.registration_options``), as JSON. A refusal is JSON too,
  ``{"error": <the text, in the person's language>}``: 400 (wrong password, or already
  ``MAX_PASSKEYS_PER_ACCOUNT``), 429 with ``Retry-After``, or 503 with ``Retry-After`` when too
  many requests wait for argon2 (nothing is written then). A wrong password is audited
  (``auth.passkey_add_failed``, no details).
* ``POST /account/passkeys`` (fields ``label`` and ``credential``): the new passkey is checked and
  stored; 303 to ``/account?saved=passkey``. A refusal shows the account page again (400) with one
  of three reasons: the label, too many passkeys, or "it could not be added" for everything else.
  Audit ``auth.passkey_added`` with the passkey's id (never its label, which a person wrote, nor
  anything of the key).
* ``POST /account/passkeys/{id}/delete``: removes a passkey of the person, 303 to
  ``/account?saved=passkey_removed``; 404 for one that is not theirs, or not an id. Audit
  ``auth.passkey_removed`` with its id. It works wherever the page is opened (an IP address too):
  removing needs no passkey.

Signing in
----------
* ``POST /login/passkey/options``: the login options, as JSON. Anyone may ask, so each client
  address (the real one: the web server takes it from the trusted edge's ``X-Forwarded-For``)
  gets at most ``MAX_LOGIN_OPTIONS_PER_ADDRESS`` in any window of five minutes (the life of a
  challenge, ``CHALLENGE_LIFETIME``: R91), forgotten when a passkey login from it works: one
  address cannot fill the household's pool of waiting challenges
  (``MAX_PENDING_LOGIN_CHALLENGES``), and where the household shares one source address (behind
  Docker Desktop, say) a hold lasts no longer than the challenges it protects the pool from.
  Either limit answers 429 (JSON, with ``Retry-After`` for the address's).
* ``POST /login/passkey`` (field ``credential``): like the password login (``accounts.routes``).
  The attempt is counted before it is checked, with the same limits and the same 429 page, by a
  throttle of its own (``passkey_login_throttle``) keyed by the credential id the answer names.
  Not the login's: a passkey attempt costs no password verification, so a burst of them with
  made-up ids would push blocked usernames out of the login's bounded throttle; and with keys of
  their own, hammering a passkey does not lock its owner's password out, nor the other way round.
  Any refusal, whatever its reason (R77 included: someone no longer in the household), gets the
  answer of a wrong password: the login page, 400, ``login.error.invalid``, no session, and
  ``auth.login_failed`` with ``{"method": "passkey"}``. When it works, the session the browser
  had is ended, a new one is opened, ``auth.login`` is written with ``{"method": "passkey"}``, and
  303 to the chat, with the session cookie.

The options routes answer 404 where passkeys cannot be used (an IP address, plain HTTP, a name
that is not a site address, a port that is not the page's: ``accounts.passkeys``); the pages there
offer none. Every route that changes something goes through the security middleware's ``Origin``
check. Audit entries are written last, just before the commit. The reason of a refusal goes to the
log (INFO), never to the person.
"""

import json
import logging
import math
import re
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any, Final, cast

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse, RedirectResponse, Response

from domo_core.accounts.account_routes import (
    ACCOUNT_PATH,
    FormError,
    account_of,
    get_password_check_throttle,
    render_account_page,
)
from domo_core.accounts.models import Person
from domo_core.accounts.passkeys import (
    CHALLENGE_LIFETIME,
    MAX_PASSKEY_LABEL_LENGTH,
    MAX_PASSKEYS_PER_ACCOUNT,
    PasskeyRejected,
    PasskeySite,
    finish_login,
    finish_registration,
    login_options,
    passkey_availability_of,
    passkeys_of,
    registration_options,
    remove_passkey,
)
from domo_core.accounts.passwords import Argon2Busy, busy_headers, verify_password_limited
from domo_core.accounts.routes import HOME_PATH, LOGIN_PATH, render_login_page
from domo_core.accounts.sessions import (
    SESSION_COOKIE,
    create_session,
    revoke_session,
    set_session_cookie,
)
from domo_core.accounts.throttle import LoginThrottle, password_check_key
from domo_core.audit.log import append_audit
from domo_core.config import CoreSettings
from domo_core.llm.types import JsonObject
from domo_core.naming import name_shown
from domo_core.policy.settings import load_policy_settings
from domo_core.proofs.service import agree, remove
from domo_core.web.deps import current_person, get_clock, get_session, get_settings
from domo_core.web.language import request_texts

router = APIRouter()

logger = logging.getLogger(__name__)

PASSKEYS_PATH: Final = ACCOUNT_PATH + "/passkeys"
LOGIN_PASSKEY_PATH: Final = LOGIN_PATH + "/passkey"
# The longest answer of a browser that is read: a credential with a "none" attestation is a few
# hundred bytes, an assertion less.
MAX_CREDENTIAL_LENGTH: Final = 64 * 1024
# The longest credential id kept as a throttle key (ids are 16 to 1023 bytes; base64url).
_MAX_KEY_LENGTH: Final = 1400
_PASSKEY_ID: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# How many login options one client address may ask for in any window of ``CHALLENGE_LIFETIME``
# (five minutes) without signing in: plenty for someone who cancels a few times, a fifth of the
# household's pool.
MAX_LOGIN_OPTIONS_PER_ADDRESS: Final = 20


def new_passkey_options_throttle(*, clock: Callable[[], datetime] | None = None) -> LoginThrottle:
    """The throttle of the login options, per client address, over the life of a challenge (one
    per application; tests pass their own clock)."""
    if clock is None:
        return LoginThrottle(max_attempts=MAX_LOGIN_OPTIONS_PER_ADDRESS, window=CHALLENGE_LIFETIME)
    return LoginThrottle(
        clock=clock, max_attempts=MAX_LOGIN_OPTIONS_PER_ADDRESS, window=CHALLENGE_LIFETIME
    )


def get_passkey_login_throttle(request: Request) -> LoginThrottle:
    """The throttle of passkey logins, per credential id (``create_app`` makes it): not the
    password login's (see the module docstring)."""
    throttle: LoginThrottle = request.app.state.passkey_login_throttle
    return throttle


def get_passkey_options_throttle(request: Request) -> LoginThrottle:
    """The throttle of login options, per client address (``create_app`` makes it)."""
    throttle: LoginThrottle = request.app.state.passkey_options_throttle
    return throttle


def _client_address(request: Request) -> str:
    """The client's address, as the web server has it: the real one behind the trusted edge."""
    return request.client.host if request.client is not None else ""


def _site(request: Request, settings: CoreSettings) -> PasskeySite:
    """Where the request uses passkeys; 404 where it cannot (the page offered none)."""
    site = passkey_availability_of(request, settings).site
    if site is None:
        raise HTTPException(status_code=404)
    return site


def _json_error(
    request: Request,
    status_code: int,
    key: str,
    headers: dict[str, str] | None = None,
    **params: str,
) -> JSONResponse:
    """A refusal for ``passkeys.js``: the text to show, in the visitor's texts."""
    text = request_texts(request)(key, **params)
    return JSONResponse({"error": text}, status_code=status_code, headers=headers)


def _answer_of(credential: str) -> JsonObject | None:
    """The browser's answer, posted as JSON in a form field: an object, or ``None``."""
    if not credential or len(credential) > MAX_CREDENTIAL_LENGTH:
        return None
    try:
        value: Any = json.loads(credential)
    except (ValueError, RecursionError):
        return None
    return cast(JsonObject, value) if isinstance(value, dict) else None


def _throttle_key(answer: JsonObject | None) -> str:
    """The key a passkey login is counted under: the credential id it names (empty for an answer
    that names none)."""
    raw_id = answer.get("rawId") if answer is not None else None
    return raw_id[:_MAX_KEY_LENGTH] if isinstance(raw_id, str) else ""


def _wait_minutes(wait: float) -> str:
    return str(math.ceil(wait / 60))


# --- adding and removing ------------------------------------------------------------------------


@router.post(PASSKEYS_PATH + "/options")
async def passkey_registration_options(
    request: Request,
    person: Annotated[Person, Depends(current_person)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[CoreSettings, Depends(get_settings)],
    throttle: Annotated[LoginThrottle, Depends(get_password_check_throttle)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
    current: Annotated[str, Form()] = "",
) -> Response:
    site = _site(request, settings)
    account = await account_of(session, person)
    throttle_key = password_check_key(account)
    # Counted now, before the slow verification (see ``LoginThrottle``); forgotten on success.
    wait = throttle.register_attempt(throttle_key)
    if wait is not None:
        return _json_error(
            request,
            429,
            "account.password.error.throttled",
            headers={"Retry-After": str(math.ceil(wait))},
            minutes=_wait_minutes(wait),
        )
    # The read is over: its transaction ends, so that nothing is held while argon2 runs or waits
    # for its turn (R87).
    await session.commit()
    try:
        right = await verify_password_limited(account.password_hash, current)
    except Argon2Busy:
        # Too many requests wait for argon2: refused at once, nothing written.
        return _json_error(request, 503, "passwords.busy", headers=busy_headers())
    if not right:
        await append_audit(
            session, actor=f"person:{person.id}", action="auth.passkey_add_failed", details={}
        )
        await session.commit()
        return _json_error(request, 400, "account.password.error.current")
    throttle.clear(throttle_key)
    try:
        options = await registration_options(
            session,
            account,
            site.rp_id,
            clock(),
            rp_name=name_shown(None, (await load_policy_settings(session)).house_domo_name),
        )
    except PasskeyRejected:
        await session.rollback()
        return _json_error(
            request, 400, "account.passkeys.error.too_many", max=str(MAX_PASSKEYS_PER_ACCOUNT)
        )
    await session.commit()
    return JSONResponse(options)


def _registration_error(rejected: PasskeyRejected) -> FormError:
    if rejected.reason == "label":
        return ("account.passkeys.error.label", {"max": str(MAX_PASSKEY_LABEL_LENGTH)})
    if rejected.reason == "too_many":
        return ("account.passkeys.error.too_many", {"max": str(MAX_PASSKEYS_PER_ACCOUNT)})
    return ("account.passkeys.error.failed", {})


@router.post(PASSKEYS_PATH)
async def add_passkey(
    request: Request,
    person: Annotated[Person, Depends(current_person)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[CoreSettings, Depends(get_settings)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
    label: Annotated[str, Form()] = "",
    credential: Annotated[str, Form()] = "",
) -> Response:
    site = _site(request, settings)
    account = await account_of(session, person)
    answer = _answer_of(credential)
    try:
        if answer is None:
            raise PasskeyRejected("malformed")
        passkey = await finish_registration(
            session, account, site.rp_id, site.origin, answer, label, clock()
        )
    except PasskeyRejected as rejected:
        logger.info("A new passkey was refused (%s)", rejected.reason)
        # The challenge is spent whatever went wrong: that is kept.
        await session.commit()
        return await render_account_page(
            request,
            person,
            account,
            session,
            settings,
            status_code=400,
            passkey_error=_registration_error(rejected),
        )
    # The first passkey makes the passkey a means of proof; a later one changes nothing.
    await agree(session, person.id, "passkey", proposed_by="person", now=clock())
    await append_audit(
        session,
        actor=f"person:{person.id}",
        action="auth.passkey_added",
        details={"passkey_id": str(passkey.id)},
    )
    await session.commit()
    return RedirectResponse(f"{ACCOUNT_PATH}?saved=passkey", status_code=303)


@router.post(PASSKEYS_PATH + "/{passkey_id}/delete")
async def delete_passkey(
    passkey_id: str,
    person: Annotated[Person, Depends(current_person)],
    session: Annotated[AsyncSession, Depends(get_session)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
) -> Response:
    if _PASSKEY_ID.fullmatch(passkey_id.lower()) is None:
        raise HTTPException(status_code=404)
    removed_id = uuid.UUID(passkey_id)
    account = await account_of(session, person)
    if not await remove_passkey(session, account.id, removed_id):
        raise HTTPException(status_code=404)
    if not await passkeys_of(session, account.id):
        # The last one is gone: so is the means of proof that went with them.
        await remove(session, person.id, "passkey", clock())
    await append_audit(
        session,
        actor=f"person:{person.id}",
        action="auth.passkey_removed",
        details={"passkey_id": str(removed_id)},
    )
    await session.commit()
    return RedirectResponse(f"{ACCOUNT_PATH}?saved=passkey_removed", status_code=303)


# --- signing in ---------------------------------------------------------------------------------


@router.post(LOGIN_PASSKEY_PATH + "/options")
async def passkey_login_options(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[CoreSettings, Depends(get_settings)],
    throttle: Annotated[LoginThrottle, Depends(get_passkey_options_throttle)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
) -> Response:
    site = _site(request, settings)
    wait = throttle.register_attempt(_client_address(request))
    if wait is not None:
        return _json_error(
            request, 429, "login.passkey.busy", headers={"Retry-After": str(math.ceil(wait))}
        )
    try:
        options = await login_options(session, site.rp_id, clock())
    except PasskeyRejected:
        await session.rollback()
        return _json_error(request, 429, "login.passkey.busy")
    await session.commit()
    return JSONResponse(options)


@router.post(LOGIN_PASSKEY_PATH)
async def passkey_login(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[CoreSettings, Depends(get_settings)],
    throttle: Annotated[LoginThrottle, Depends(get_passkey_login_throttle)],
    options_throttle: Annotated[LoginThrottle, Depends(get_passkey_options_throttle)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
    credential: Annotated[str, Form()] = "",
) -> Response:
    site = passkey_availability_of(request, settings).site
    answer = _answer_of(credential)
    key = _throttle_key(answer)
    # Counted now, before the answer is checked; forgotten on success (as for a password).
    wait = throttle.register_attempt(key)
    if wait is not None:
        return render_login_page(
            request,
            settings,
            status_code=429,
            error_key="login.error.throttled",
            error_params={"minutes": _wait_minutes(wait)},
            headers={"Retry-After": str(math.ceil(wait))},
        )
    try:
        if site is None or answer is None:
            raise PasskeyRejected("malformed")
        account = await finish_login(session, site.rp_id, site.origin, answer, clock())
    except PasskeyRejected as rejected:
        logger.info("A passkey login was refused (%s)", rejected.reason)
        # The challenge is spent whatever went wrong: committed with the entry.
        await append_audit(
            session, actor="system", action="auth.login_failed", details={"method": "passkey"}
        )
        await session.commit()
        return render_login_page(
            request, settings, status_code=400, error_key="login.error.invalid"
        )

    # As the password login does: the session the browser had is replaced, in this transaction.
    presented = request.cookies.get(SESSION_COOKIE)
    if presented:
        await revoke_session(session, presented)
    token = await create_session(session, account.id, ttl=settings.session_ttl)
    await append_audit(
        session,
        actor=f"person:{account.person_id}",
        action="auth.login",
        details={"method": "passkey"},
    )
    await session.commit()
    throttle.clear(key)
    options_throttle.clear(_client_address(request))
    response = RedirectResponse(HOME_PATH, status_code=303)
    set_session_cookie(response, token, settings.session_ttl)
    return response
