"""The owner step of the first launch: the page, and the creation of the owner account.

Whoever opens the page of a fresh instance must prove they can read the server's logs by typing
the one-time setup code (``setup_flow.code``). With the code they choose the owner's name,
username and password; the owner is then logged in and sent on to the model step.

The code is checked in a transaction that only reads, and ends before the password is hashed: the
hash takes the limited path of argon2 (``accounts.passwords``) and may wait for its turn, and
nothing is held meanwhile; when too many requests wait already, the page answers 503 with
``Retry-After`` and the right code is not counted as a wrong guess. The creation is then one
transaction: the person, the account, the end of the code's life, the web session and last of all
the audit entry are written, and committed together. The audit entry goes last because the audit
log serialises its writers with a lock held until the end of the transaction. If anything fails
nothing is left behind and the code is still good.

Two requests may arrive at the same time with the right code. The unique index on the owner role
(``uq_persons_single_owner``) lets one through: the other one fails on it and is told that the
setup is done.

Wrong guesses are throttled for the whole instance (``SETUP_CODE_MAX_ATTEMPTS`` per fifteen
minutes). The attempt is counted before the code is checked and forgotten if it was right; while
the limit is reached even the right code is refused, until the window has passed.

The setup code is never echoed back, logged or recorded: not in a page, not in an audit entry.
"""

import math
import unicodedata
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Final, NamedTuple

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import RedirectResponse, Response

from domo_core.accounts.models import Account, Person
from domo_core.accounts.passwords import (
    MIN_PASSWORD_LENGTH,
    Argon2Busy,
    busy_headers,
    hash_password_limited,
)
from domo_core.accounts.sessions import create_session, set_session_cookie
from domo_core.accounts.throttle import LoginThrottle
from domo_core.accounts.usernames import normalize_username
from domo_core.audit.log import append_audit
from domo_core.config import CoreSettings
from domo_core.proofs.service import agree
from domo_core.setup_flow.code import verify_setup_code
from domo_core.setup_flow.models import SetupState
from domo_core.setup_flow.state import STEP_PATHS, owner_exists
from domo_core.text import MAX_DISPLAY_NAME_LENGTH, InvalidDisplayName, display_name_value
from domo_core.web.deps import get_session, get_settings
from domo_core.web.templating import render

router = APIRouter()

OWNER_PATH: Final = STEP_PATHS["owner"]
MODEL_PATH: Final = STEP_PATHS["model"]

# Wrong guesses allowed in any window of fifteen minutes, for the whole instance. With about 59
# bits in the code this is only there so that nobody on the network can hammer it.
SETUP_CODE_MAX_ATTEMPTS: Final = 10
_THROTTLE_KEY: Final = "setup-code"

MAX_USERNAME_LENGTH: Final = 64


def new_setup_code_throttle(*, clock: Callable[[], datetime] | None = None) -> LoginThrottle:
    """The throttle of wrong setup codes (one per application; tests pass their own clock)."""
    if clock is None:
        return LoginThrottle(max_attempts=SETUP_CODE_MAX_ATTEMPTS)
    return LoginThrottle(clock=clock, max_attempts=SETUP_CODE_MAX_ATTEMPTS)


def get_setup_code_throttle(request: Request) -> LoginThrottle:
    """The throttle of the application (``create_app`` makes it)."""
    throttle: LoginThrottle = request.app.state.setup_code_throttle
    return throttle


class _FormError(NamedTuple):
    key: str
    params: dict[str, str]


def _has_control_characters(text: str) -> bool:
    return any(unicodedata.category(character) == "Cc" for character in text)


def _check_fields(display_name: str, username: str, password: str) -> _FormError | None:
    """What is wrong with the owner's details, if anything (``username`` is normalised). The name
    follows the rule of every display name (``text.display_name_value``)."""
    try:
        display_name_value(display_name)
    except InvalidDisplayName:
        return _FormError("setup.owner.error.display_name", {"max": str(MAX_DISPLAY_NAME_LENGTH)})
    if not username or len(username) > MAX_USERNAME_LENGTH or _has_control_characters(username):
        return _FormError("setup.owner.error.username", {"max": str(MAX_USERNAME_LENGTH)})
    # Characters, not bytes; and not trimmed: spaces may be part of a password.
    if len(password) < MIN_PASSWORD_LENGTH:
        return _FormError("setup.owner.error.password", {"min": str(MIN_PASSWORD_LENGTH)})
    return None


def _owner_page(
    request: Request,
    *,
    status_code: int = 200,
    error: _FormError | None = None,
    display_name: str = "",
    username: str = "",
    headers: dict[str, str] | None = None,
) -> Response:
    # The code and the password are never put back in the form.
    return render(
        request,
        "setup_owner.html",
        status_code=status_code,
        headers=headers,
        error_key=error.key if error else None,
        error_params=error.params if error else {},
        display_name=display_name,
        username=username,
        min_password_length=MIN_PASSWORD_LENGTH,
    )


def _already_done_page(request: Request) -> Response:
    return render(request, "error.html", status_code=409, kind="setup_done")


@router.get(OWNER_PATH)
async def owner_page(request: Request) -> Response:
    return _owner_page(request)


@router.post(OWNER_PATH)
async def create_owner(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[CoreSettings, Depends(get_settings)],
    throttle: Annotated[LoginThrottle, Depends(get_setup_code_throttle)],
    code: Annotated[str, Form()] = "",
    display_name: Annotated[str, Form()] = "",
    username: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
) -> Response:
    shown_name = display_name.strip()
    name = normalize_username(username)

    def retry(
        status_code: int, error: _FormError, headers: dict[str, str] | None = None
    ) -> Response:
        return _owner_page(
            request,
            status_code=status_code,
            error=error,
            display_name=display_name,
            username=username,
            headers=headers,
        )

    # What is wrong with the form says nothing about the code: it is not a guess at it.
    problem = _check_fields(shown_name, name, password)
    if problem is not None:
        return retry(400, problem)

    wait = throttle.register_attempt(_THROTTLE_KEY)
    if wait is not None:
        error = _FormError("setup.owner.error.throttled", {"minutes": str(math.ceil(wait / 60))})
        return retry(429, error, {"Retry-After": str(math.ceil(wait))})
    if not await verify_setup_code(session, code):
        return retry(400, _FormError("setup.owner.error.code", {}))

    # The check only read: its transaction ends, so that nothing is held while the password is
    # hashed or waits for its turn. The slow part comes before anything is written.
    await session.commit()
    try:
        password_hash = await hash_password_limited(password)
    except Argon2Busy:
        # Too many requests wait for argon2: refused at once (503), nothing written. The code was
        # right: the attempt is not a wrong guess.
        throttle.clear(_THROTTLE_KEY)
        return retry(503, _FormError("passwords.busy", {}), busy_headers())

    person = Person(
        display_name=shown_name, role="owner", origin="setup", first_access_at=datetime.now(UTC)
    )
    try:
        session.add(person)
        await session.flush()
        account = Account(person_id=person.id, username=name, password_hash=password_hash)
        session.add(account)
        await session.flush()
        await agree(session, person.id, "password", proposed_by="code", now=datetime.now(UTC))
    except IntegrityError:
        await session.rollback()
        if await owner_exists(session):
            # Another request made the owner first (or the setup was done in the meantime).
            return _already_done_page(request)
        # Not the owner index: the username cannot be used (taken, or not storable).
        return retry(
            400, _FormError("setup.owner.error.username", {"max": str(MAX_USERNAME_LENGTH)})
        )

    state = await session.execute(
        update(SetupState)
        .where(SetupState.id == 1)
        .values(setup_code_hash=None, owner_created_at=datetime.now(UTC))
        .returning(SetupState.id)
    )
    if state.scalar_one_or_none() is None:
        raise RuntimeError("the setup_state row is missing: the database migrations have not run")
    token = await create_session(session, account.id, ttl=settings.session_ttl)
    # Last, just before the commit: the audit log holds a lock until the transaction ends.
    await append_audit(
        session,
        actor=f"person:{person.id}",
        action="setup.owner_created",
        details={"username": account.username},
    )
    await session.commit()

    throttle.clear(_THROTTLE_KEY)
    response = RedirectResponse(MODEL_PATH, status_code=303)
    set_session_cookie(response, token, settings.session_ttl)
    return response
