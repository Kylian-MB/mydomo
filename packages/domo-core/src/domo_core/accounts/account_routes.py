"""The account page: ``GET /account``, ``POST /account/name`` and ``POST /account/password``.

Every person of the household has one (the owner included). It shows their display name,
username and role, lets them change their display name and their password, and lists their
passkeys (``data-passkeys="slot"``): adding and removing one is ``accounts.passkey_routes``. A
visitor who has no session is sent to the login. The forms are plain ones: the security
middleware checks the ``Origin`` of every request that changes something, and the page carries no
inline script and no style (adding a passkey needs ``static/passkeys.js``, loaded only where
passkeys can be used; elsewhere the page says why, see ``accounts.passkeys``).

A change redirects (303) to ``/account?saved=<what>`` (``name``, ``password``, ``passkey`` or
``passkey_removed``), which shows a notice; any other value of ``saved`` shows nothing. A refusal
shows the page again with the reason and the status 400 (429 when the person is held back):
nothing is changed. The password fields are never put back in the page.

The display name
----------------

It follows the rule of every display name (``text.display_name_value``: 1 to 60 characters, one
plain line, no direction mark) and must not be the name of someone else in the household, nor of
an invitation that waits (``household.service.rename_person``, which also takes the lock of the
display names): a copy would let a person speak as another. A person may change the case of their
own name. The audit entry ``profile.renamed`` says who, never the name (the log is append-only).

The password
------------

``current`` is verified as at the login (argon2, on its limited path). The attempt is
counted before the slow verification and forgotten if it worked, against a count of the account
(``get_password_check_throttle``, shared with the password asked before adding a passkey): five
wrong ones in any window of fifteen minutes, then 429 with ``Retry-After``. The count is not the
login's (R84): anyone may hammer the login with a username, and that must not keep its owner from
changing their password. So someone who holds a session but not the password gets five guesses per
window on these two forms, and five more at the login itself: twice the login's rate, not more.
What is wrong with the *new* password (too short, or not
typed twice the same) says nothing about the current one: it is refused first and costs no
attempt. The new password is kept as typed (not trimmed) and counted in characters, like at the
login.

The slow steps (verify, hash) take the limited path of argon2 (``accounts.passwords``: they wait
for their turn) and run without a lock, and without a connection of the database: the transaction
that read the account ends before them, and the change is written in a new one (R50, R82). The new
hash is written only if the stored hash is still the one that was verified (a compare-and-set): a
password changed in the meantime is not overwritten, and the answer is the same as for a wrong
current password. When too many requests wait for argon2 already, the change is refused at once
(503 with ``Retry-After``) and nothing is written; a current password that was found right before
that is not counted as a guess.

Changing the password ends every **other** session of the account (``revoke_other_sessions``):
whoever else was signed in with the old password is signed out, and the browser it was changed
from stays signed in. That session is named by its id (``session_id_of``, looked up first: if it
is gone the person is sent to the login and nothing is done; if it ends while the slow steps run,
the change is rolled back and the person is sent to the login too, R85), never by a cookie that
may be empty.
Both outcomes are audited, in the transaction of the change and last:
``auth.password_changed`` (with how many sessions ended) and ``auth.password_change_failed`` (no
details: never what was typed).
"""

import math
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Final, NamedTuple

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import RedirectResponse, Response

from domo_core.accounts.models import Account, Person
from domo_core.accounts.passkeys import (
    MAX_PASSKEY_LABEL_LENGTH,
    passkey_availability_of,
    passkeys_of,
)
from domo_core.accounts.passwords import (
    MIN_PASSWORD_LENGTH,
    Argon2Busy,
    busy_headers,
    hash_password_limited,
    verify_password_limited,
)
from domo_core.accounts.sessions import SESSION_COOKIE, revoke_other_sessions, session_id_of
from domo_core.accounts.throttle import LoginThrottle, password_check_key
from domo_core.audit.log import append_audit
from domo_core.config import CoreSettings
from domo_core.household.service import NameInUse, rename_person
from domo_core.text import MAX_DISPLAY_NAME_LENGTH, InvalidDisplayName, display_name_value
from domo_core.web.deps import (
    LoginRequired,
    current_person,
    get_clock,
    get_session,
    get_settings,
)
from domo_core.web.language import request_texts
from domo_core.web.templating import render

router = APIRouter()

ACCOUNT_PATH: Final = "/account"
# The notices a change can ask the page for (``?saved=``): any other value shows none.
_SAVED: Final = frozenset({"name", "password", "passkey", "passkey_removed"})

# An error of a form: the catalog key of its text and the parameters.
FormError = tuple[str, dict[str, str]]


class _PasskeyRow(NamedTuple):
    """A passkey as the page lists it."""

    id: str
    label: str
    added: str
    last_used: str | None


def get_password_check_throttle(request: Request) -> LoginThrottle:
    """The throttle of the password a signed-in person types again: the account page's current
    password, and the one asked before adding a passkey (``accounts.passkey_routes``). One count
    per account (``password_check_key``) for both: going from one form to the other gives no more
    guesses. Not the login's, whose key is a username anyone may hammer (R84, R89). ``create_app``
    makes it; tests replace it to control time."""
    throttle: LoginThrottle = request.app.state.password_check_throttle
    return throttle


async def render_account_page(
    request: Request,
    person: Person,
    account: Account,
    session: AsyncSession,
    settings: CoreSettings,
    *,
    status_code: int = 200,
    saved: str | None = None,
    name_value: str | None = None,
    name_error: FormError | None = None,
    password_error: FormError | None = None,
    passkey_error: FormError | None = None,
    headers: dict[str, str] | None = None,
) -> Response:
    """The account page of ``person`` (whose login is ``account``). ``name_value`` is what the
    name field holds (what was typed, after a refusal; otherwise the current name). The password
    fields are always empty. The passkeys are listed wherever the page is opened (removing one
    needs no passkey); adding one is offered where the request can use passkeys."""
    texts = request_texts(request)
    passkeys = [
        _PasskeyRow(
            id=str(passkey.id),
            label=passkey.label,
            added=texts.date(passkey.created_at),
            last_used=None if passkey.last_used_at is None else texts.date(passkey.last_used_at),
        )
        for passkey in await passkeys_of(session, account.id)
    ]
    return render(
        request,
        "account.html",
        status_code=status_code,
        headers=headers,
        person=person,
        username=account.username,
        saved=saved if saved in _SAVED else None,
        name_value=person.display_name if name_value is None else name_value,
        name_error=name_error,
        password_error=password_error,
        max_name_length=MAX_DISPLAY_NAME_LENGTH,
        min_password_length=MIN_PASSWORD_LENGTH,
        passkeys=passkey_availability_of(request, settings),
        passkey_list=passkeys,
        passkey_error=passkey_error,
        max_passkey_label_length=MAX_PASSKEY_LABEL_LENGTH,
    )


async def account_of(session: AsyncSession, person: Person) -> Account:
    """The login account of ``person`` (every person who can sign in has exactly one)."""
    found = await session.execute(select(Account).where(Account.person_id == person.id))
    return found.scalar_one()


@router.get(ACCOUNT_PATH)
async def account_page(
    request: Request,
    person: Annotated[Person, Depends(current_person)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[CoreSettings, Depends(get_settings)],
) -> Response:
    account = await account_of(session, person)
    return await render_account_page(
        request, person, account, session, settings, saved=request.query_params.get("saved")
    )


@router.post(ACCOUNT_PATH + "/name")
async def change_name(
    request: Request,
    person: Annotated[Person, Depends(current_person)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[CoreSettings, Depends(get_settings)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
    name: Annotated[str, Form()] = "",
) -> Response:
    account = await account_of(session, person)

    async def refused(error: FormError) -> Response:
        return await render_account_page(
            request,
            person,
            account,
            session,
            settings,
            status_code=400,
            name_value=name,
            name_error=error,
        )

    try:
        shown = display_name_value(name)
    except InvalidDisplayName:
        return await refused(("account.name.error.invalid", {"max": str(MAX_DISPLAY_NAME_LENGTH)}))
    try:
        found = await rename_person(session, person, shown, now=clock())
    except NameInUse:
        # Nothing was written; commit to let go of the lock of the display names.
        await session.commit()
        return await refused(("account.name.error.in_use", {}))
    if not found:
        await session.rollback()
        raise LoginRequired
    # Last, just before the commit: the audit log holds a lock until the transaction ends. The
    # new name is not in it.
    await append_audit(session, actor=f"person:{person.id}", action="profile.renamed", details={})
    await session.commit()
    return RedirectResponse(f"{ACCOUNT_PATH}?saved=name", status_code=303)


@router.post(ACCOUNT_PATH + "/password")
async def change_password(
    request: Request,
    person: Annotated[Person, Depends(current_person)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[CoreSettings, Depends(get_settings)],
    throttle: Annotated[LoginThrottle, Depends(get_password_check_throttle)],
    current: Annotated[str, Form()] = "",
    new: Annotated[str, Form()] = "",
    confirm: Annotated[str, Form()] = "",
) -> Response:
    account = await account_of(session, person)
    throttle_key = password_check_key(account)
    account_id = account.id
    verified_hash = account.password_hash
    # The session this comes from is the one that stays; if it is gone (a logout in another tab,
    # say) the person is not signed in any more, and nothing is done.
    token = request.cookies.get(SESSION_COOKIE)
    current_session_id = await session_id_of(session, token) if token else None
    if current_session_id is None:
        raise LoginRequired

    async def refused(
        error: FormError, status_code: int = 400, headers: dict[str, str] | None = None
    ) -> Response:
        return await render_account_page(
            request,
            person,
            account,
            session,
            settings,
            status_code=status_code,
            password_error=error,
            headers=headers,
        )

    # Characters, not bytes; and not trimmed: spaces may be part of a password.
    if len(new) < MIN_PASSWORD_LENGTH:
        return await refused(("account.password.error.short", {"min": str(MIN_PASSWORD_LENGTH)}))
    if new != confirm:
        return await refused(("account.password.error.mismatch", {}))

    # Counted now, before the slow verification (see ``LoginThrottle``); forgotten on success.
    wait = throttle.register_attempt(throttle_key)
    if wait is not None:
        return await refused(
            ("account.password.error.throttled", {"minutes": str(math.ceil(wait / 60))}),
            429,
            {"Retry-After": str(math.ceil(wait))},
        )

    # The reads are over: their transaction ends, so that nothing is held while argon2 runs or
    # waits for its turn. The change is written in a new one.
    await session.commit()
    right = False
    try:
        right = await verify_password_limited(verified_hash, current)
        new_hash = await hash_password_limited(new) if right else None
    except Argon2Busy:
        # Too many requests wait for argon2: refused at once, nothing written. A current password
        # found right is not a guess.
        if right:
            throttle.clear(throttle_key)
        return await refused(("passwords.busy", {}), 503, busy_headers())
    changed = False
    if new_hash is not None:
        # Written only over the hash that was verified: see the module docstring.
        written = await session.execute(
            update(Account)
            .where(Account.id == account_id, Account.password_hash == verified_hash)
            .values(password_hash=new_hash)
            .returning(Account.id)
        )
        changed = written.scalar_one_or_none() is not None
    if not changed:
        await append_audit(
            session, actor=f"person:{person.id}", action="auth.password_change_failed", details={}
        )
        await session.commit()
        return await refused(("account.password.error.current", {}))

    try:
        revoked = await revoke_other_sessions(session, account_id, current_session_id)
    except ValueError:
        # The session this comes from ended while the slow steps ran (a logout in another tab,
        # say): the person is not signed in any more, and nothing is written (R85).
        await session.rollback()
        raise LoginRequired from None
    await append_audit(
        session,
        actor=f"person:{person.id}",
        action="auth.password_changed",
        details={"revoked_sessions": revoked},
    )
    await session.commit()
    throttle.clear(throttle_key)
    return RedirectResponse(f"{ACCOUNT_PATH}?saved=password", status_code=303)
