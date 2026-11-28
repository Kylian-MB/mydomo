"""The household's pages: joining with an invitation, or coming back with a return link
(``/invite/<token>``), and the household as the owner sees it (``/household``).

Joining
-------

``GET /invite/<token>`` shows who is invited and with which role, and asks for a username and a
password; ``POST /invite/<token>`` checks them (``household.service.check_joining``) in a
transaction that ends before the password is hashed: the hash takes the limited path of argon2
(``accounts.passwords``) and may wait for its turn, and nothing is held meanwhile (R79). A new
transaction then creates the person and the account (``household.service.accept_invitation``),
opens their session, writes the audit entry ``household.joined`` (their name and role, never the
token), commits, and sends them to the chat. A browser that already had a session ends it in that
transaction, as a login does.

A *return link* (an invitation tied to a person who left, ``household_bring_back``) is the same
address with another form: "welcome back", the username the person keeps (shown, not asked) and a
new password. ``POST`` then runs ``check_return``, the hash outside the transaction, and
``accept_return``, which brings the same person back (their history, not a new person), opens their
session and writes ``household.brought_back`` (their identifier and role, never the token); then
the vault forgets the phrase they once said aloud (ruling T11). The two kinds of link are never
taken for each other, and the same throttle holds them back.

A link that cannot be used (unknown, already used, cancelled or expired, or a return link for
someone who is in the household again) gets one page, the same for each case, with the status 410.
A username that is taken or not valid, or a password that is too short, gets the form again (400)
and the link still works; so does a request refused because too many wait for argon2 (503 with
``Retry-After``).

The page of a link that can be used, opened by a visitor (nobody signed in), is in the language the
browser prefers (``texts.languages.browser_language``) even when it has no version yet, nor a
close one: the only way a new language is written for someone who is not signed in (spec 4.6).
Someone signed in reads the page as they read every page. A language with a close version
(``texts.languages.version_for``), or with default phrases of its own (``meanings.has_defaults``),
reads it, as any visitor does, and begins none.
Else the texts of the page are written in it then, on the keys of the one who invited
(``created_by``), counting for them (``TextWriting.begin_language``, ``write_now``), if the house
may begin a new language today; the page waits ``VISITOR_LANGUAGE_SECONDS`` at most for them, then
shows the house's version. The rest of that version is asked with them, before the page waits,
and written in the background on the same keys, even if the visitor leaves. A link that cannot be
used writes nothing, and neither does the form shown again after a refusal: by then, the language
has a version, or the house's is read.

Joining is held back like a login (``accounts.throttle``): five attempts per link in any window
of fifteen minutes, counted before the slow hash of the password and forgotten once it worked
(429 beyond). The links are reachable without a session once the first launch is over (the setup
gate leads them to the setup before that), and the security middleware checks the ``Origin`` of
the form, as for every request that changes something.

The link carries the invitation's token in its path: it is not written to any log (the core's
access log hides it, see ``domo_core.__main__``), and the page sends it nowhere else.

The link, for the one who asked
-------------------------------

When someone other than the one who asked said yes to an invitation (or to a return link), the
invitation was made without a token (``household.service.create_invitation``, ``with_token``),
and the card of the one who asked has a button, "Show the link". ``POST /invitations/<id>/link``
(``current_person``) makes the token (``household.service.reveal_link``) and answers with the link,
in a fragment that takes the place of the button, this once: the token is stored nowhere (only its
SHA-256), and the audit entry ``household.link_shown`` holds the invitation's identifier, never
the link. A link given again after someone else's yes waits the same way for the one who asked
for it (``Invitation.link_for_id``). It is a 404 when the invitation's link is not for the person
(as when it does not exist: nobody
learns which exist), and a 410 when its link was shown already or it is closed (accepted,
cancelled or expired).

The household page
------------------

``GET /household`` is the owner's (403 for anyone else) and reads only: each person, their role
(``data-person-role``) and where they stand (``data-person-state``: ``active``, ``guest`` until a
date, ``stay_over``, or ``removed``), then the invitations that wait (never their links, which
are not kept). Changes go through Domo, whose actions are confirmed.
"""

import contextlib
import math
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import RedirectResponse, Response

from domo_core.accounts.models import Account, Person
from domo_core.accounts.passwords import (
    MIN_PASSWORD_LENGTH,
    Argon2Busy,
    busy_headers,
    hash_password_limited,
)
from domo_core.accounts.routes import HOME_PATH
from domo_core.accounts.sessions import (
    SESSION_COOKIE,
    create_session,
    person_of_session,
    revoke_session,
    set_session_cookie,
)
from domo_core.accounts.throttle import LoginThrottle
from domo_core.audit.log import append_audit
from domo_core.config import CoreSettings
from domo_core.gatekeeper_client import GatekeeperError
from domo_core.household.models import Invitation
from domo_core.household.service import (
    MAX_USERNAME_LENGTH,
    InvalidUsername,
    InvitationUnavailable,
    Joining,
    Returning,
    UsernameTaken,
    WeakPassword,
    accept_invitation,
    accept_return,
    check_joining,
    check_return,
    hash_token,
    invitation_of,
    open_invitations,
    returning_account,
    reveal_link,
)
from domo_core.policy.settings import load_policy_settings
from domo_core.proofs.kinds import phrase_ref
from domo_core.texts.languages import (
    VISITOR_LANGUAGE_SECONDS,
    browser_language,
    known_languages,
    version_for,
)
from domo_core.texts.scheduler import TextWriting
from domo_core.texts.store import Reader
from domo_core.texts.view import RecueilTexts, Texts, recueil_for, shown_groups
from domo_core.web.deps import (
    current_person,
    gatekeeper_of,
    get_clock,
    get_session,
    get_settings,
    require_owner,
)
from domo_core.web.language import request_texts, use_texts
from domo_core.web.templating import render

router = APIRouter()

INVITE_PATH: Final = "/invite"
INVITATIONS_PATH: Final = "/invitations"
HOUSEHOLD_PATH: Final = "/household"

_INVITATION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def get_invitation_throttle(request: Request) -> LoginThrottle:
    """The throttle of the attempts to join (one per application; ``create_app`` makes it, tests
    replace it to control time)."""
    throttle: LoginThrottle = request.app.state.invitation_throttle
    return throttle


def _days(count: int, texts: Texts) -> str:
    return texts("policy.duration.day" if count == 1 else "policy.duration.days", count=str(count))


# --- joining and coming back ---------------------------------------------------------------------


def _unavailable(request: Request) -> Response:
    """The one page of a link that cannot be used, whatever the reason."""
    return render(request, "invite.html", status_code=410, invitation=None)


@dataclass(frozen=True)
class _Link:
    """A link that can be used: its open ``invitation`` and, for a return link, the username of
    the one it brings back (``returning_username``)."""

    invitation: Invitation
    returning_username: str | None


async def _usable_link(session: AsyncSession, token: str, now: datetime) -> _Link | None:
    """The link of ``token`` at ``now``, ``None`` when it cannot be used (nothing is locked)."""
    invitation = await invitation_of(session, token, now)
    if invitation is None:
        return None
    if invitation.person_id is None:
        return _Link(invitation, None)
    account = await returning_account(session, invitation, now)
    return None if account is None else _Link(invitation, account.username)


async def _form(
    request: Request,
    session: AsyncSession,
    token: str,
    now: datetime,
    *,
    status_code: int = 200,
    error_key: str | None = None,
    error_params: dict[str, str] | None = None,
    username: str = "",
    headers: dict[str, str] | None = None,
) -> Response:
    """The form of the open invitation of ``token`` (``_form_of``), or the page of a link that
    cannot be used."""
    link = await _usable_link(session, token, now)
    if link is None:
        return _unavailable(request)
    return _form_of(
        request,
        link,
        token,
        status_code=status_code,
        error_key=error_key,
        error_params=error_params,
        username=username,
        headers=headers,
    )


def _form_of(
    request: Request,
    link: _Link,
    token: str,
    *,
    status_code: int = 200,
    error_key: str | None = None,
    error_params: dict[str, str] | None = None,
    username: str = "",
    headers: dict[str, str] | None = None,
) -> Response:
    """The form of ``link``, of the token ``token``, in the texts of ``request``.

    A return link gets the form of someone coming back: it shows their username (it does not
    change, and is not asked) and asks for a new password only."""
    invitation = link.invitation
    texts = request_texts(request)
    stay = (
        _days(invitation.guest_days, texts)
        if invitation.role == "guest" and invitation.guest_days is not None
        else None
    )
    # The password is never put back in the form.
    return render(
        request,
        "invite.html",
        status_code=status_code,
        headers=headers,
        invitation=invitation,
        token=token,
        stay=stay,
        error_key=error_key,
        error_params=error_params or {},
        username=username,
        returning_username=link.returning_username,
        min_password_length=MIN_PASSWORD_LENGTH,
    )


async def _in_a_new_language(
    request: Request, session: AsyncSession, invitation: Invitation, shown: Texts, now: datetime
) -> Texts | None:
    """The texts of the page of a link that can be used, in the language the browser prefers when
    it has no version yet (see the module docstring): the groups of the texts the page showed
    (``shown``) are written in it, on the keys of the one who invited, and waited for
    ``VISITOR_LANGUAGE_SECONDS`` at most; the rest of the version is written in the background.
    ``None`` when the page stays in ``shown``: someone is signed in (at ``now``); the browser
    prefers no language, the house's, or one that has a version or a close one (``shown`` is in it
    already); the house may not begin a new language today; or the texts were not written in
    time."""
    # Never the header itself: a tag of a strict shape, or none.
    language = browser_language(request.headers.get("accept-language"))
    if language is None or not isinstance(shown, RecueilTexts) or language == shown.language:
        return None
    token = request.cookies.get(SESSION_COOKIE)
    if token and await person_of_session(session, token, now) is not None:
        # Only a visitor makes a new language written on the page of a link.
        return None
    settings = await load_policy_settings(session)
    house = settings.house_language
    if language == house:
        return None
    if version_for(language, await known_languages(session), house=house) is not None:
        return None
    groups = shown_groups(shown)
    # The page only read: nothing is held while the writer writes.
    await session.commit()
    writing: TextWriting = request.app.state.text_writing
    host = invitation.created_by
    # The texts of the page, then the rest of the version: all asked before the page waits.
    if not await writing.begin_language(language, groups, host=host):
        return None
    version = Reader(None, language)
    ready = await writing.write_now(version, groups, within=VISITOR_LANGUAGE_SECONDS, host=host)
    # Written in time, and something of it kept (the one who invited may have no key left).
    if not ready or language not in await known_languages(session):
        return None
    return await recueil_for(session, person=None, language=language, settings=settings)


# What the form can be refused for, while the link still works.
Refusal = InvalidUsername | UsernameTaken | WeakPassword


def _refusal(refused: Refusal) -> tuple[str, dict[str, str]]:
    """What the form says about a username or a password it cannot take."""
    match refused:
        case InvalidUsername():
            return "invite.error.username", {"max": str(MAX_USERNAME_LENGTH)}
        case UsernameTaken():
            return "invite.error.username_taken", {}
        case WeakPassword():
            return "invite.error.password", {"min": str(MIN_PASSWORD_LENGTH)}


@router.get(INVITE_PATH + "/{token}")
async def invitation_page(
    token: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
) -> Response:
    now = clock()
    link = await _usable_link(session, token, now)
    if link is None:
        return _unavailable(request)
    # Shown once in the texts the visitor reads, to learn which texts the page shows.
    page = _form_of(request, link, token)
    shown = request_texts(request)
    texts = await _in_a_new_language(request, session, link.invitation, shown, now)
    if texts is None:
        return page
    use_texts(request, texts)
    return _form_of(request, link, token)


@router.post(INVITE_PATH + "/{token}")
async def join_household(
    token: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[CoreSettings, Depends(get_settings)],
    throttle: Annotated[LoginThrottle, Depends(get_invitation_throttle)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
    username: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
) -> Response:
    now = clock()
    key = hash_token(token)
    # Counted now, before the slow hash (see ``LoginThrottle``); forgotten once it worked.
    wait = throttle.register_attempt(key)
    if wait is not None:
        return await _form(
            request,
            session,
            token,
            now,
            status_code=429,
            error_key="invite.error.throttled",
            error_params={"minutes": str(math.ceil(wait / 60))},
            username=username,
            headers={"Retry-After": str(math.ceil(wait))},
        )

    async def refused(reason: InvitationUnavailable | Refusal) -> Response:
        await session.rollback()
        if isinstance(reason, InvitationUnavailable):
            return _unavailable(request)
        error_key, error_params = _refusal(reason)
        return await _form(
            request,
            session,
            token,
            now,
            status_code=400,
            error_key=error_key,
            error_params=error_params,
            username=username,
        )

    # A return link brings someone back (their username stays, only a password is asked); any
    # other link makes a new person. The service checks each against the other.
    invitation = await invitation_of(session, token, now)
    coming_back = invitation is not None and invitation.person_id is not None
    checked: Joining | Returning
    try:
        if coming_back:
            checked = await check_return(session, token=token, password=password, now=now)
        else:
            checked = await check_joining(
                session, token=token, username=username, password=password, now=now
            )
    except (InvitationUnavailable, InvalidUsername, UsernameTaken, WeakPassword) as reason:
        return await refused(reason)
    # The checks only read: their transaction ends, so that nothing is held while the password
    # is hashed or waits for its turn (R79).
    await session.commit()
    try:
        password_hash = await hash_password_limited(password)
    except Argon2Busy:
        # Too many requests wait for argon2: the form again (503), and the link still works.
        return await _form(
            request,
            session,
            token,
            now,
            status_code=503,
            error_key="passwords.busy",
            username=username,
            headers=busy_headers(),
        )
    try:
        if isinstance(checked, Returning):
            person = await accept_return(session, checked, password_hash, now=now)
        else:
            person = await accept_invitation(session, checked, password_hash, now=now)
    except (InvitationUnavailable, UsernameTaken) as reason:
        return await refused(reason)

    account_id = (
        await session.execute(select(Account.id).where(Account.person_id == person.id))
    ).scalar_one()
    # A browser that had a session (someone else's, say) ends it: the new one replaces it.
    presented = request.cookies.get(SESSION_COOKIE)
    if presented:
        await revoke_session(session, presented)
    session_token = await create_session(session, account_id, ttl=settings.session_ttl)
    # Last, just before the commit: the audit log holds a lock until the transaction ends.
    if isinstance(checked, Returning):
        action = "household.brought_back"
        details: dict[str, str] = {"person_id": str(person.id), "role": person.role}
    else:
        action = "household.joined"
        details = {
            "name": person.display_name,
            "role": person.role,
            "invitation_id": str(checked.invitation_id),
        }
    await append_audit(session, actor=f"person:{person.id}", action=action, details=details)
    await session.commit()
    throttle.clear(key)
    if isinstance(checked, Returning):
        # The phrase said aloud went with the old access (ruling T11): its sealed copy at the
        # vault goes too. A vault that does not answer (logged by the client) keeps nobody out:
        # without the means it proves nothing, and agreeing a phrase again removes it first.
        # The vault is asked for a return alone: a new member's joining never depends on it.
        with contextlib.suppress(GatekeeperError):
            await gatekeeper_of(request.app).phrase_remove(phrase_ref(person.id))
    response = RedirectResponse(HOME_PATH, status_code=303)
    set_session_cookie(response, session_token, settings.session_ttl)
    return response


# --- the link of an invitation, for the one who asked ---------------------------------------------


@router.post(INVITATIONS_PATH + "/{invitation_id}/link")
async def show_link(
    invitation_id: str,
    request: Request,
    person: Annotated[Person, Depends(current_person)],
    session: Annotated[AsyncSession, Depends(get_session)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
) -> Response:
    """Make the link of an invitation that waits for it, and show it, once, to the one who made the
    invitation (see the module docstring)."""
    if _INVITATION_ID.fullmatch(invitation_id.lower()) is None:
        raise HTTPException(status_code=404)
    wanted = uuid.UUID(invitation_id)
    # The one the link waits for: the one who made the invitation, or who asked for it again.
    found = await session.execute(
        select(Invitation.display_name, Invitation.person_id).where(
            Invitation.id == wanted,
            func.coalesce(Invitation.link_for_id, Invitation.created_by) == person.id,
        )
    )
    row = found.first()
    if row is None:
        raise HTTPException(status_code=404)
    name, person_id = row
    try:
        token = await reveal_link(session, wanted, person.id, clock())
    except InvitationUnavailable:
        await session.rollback()
        return render(
            request, "fragments/invitation_link.html", status_code=410, link=None, once=None
        )
    # Last, just before the commit. The invitation, never the link.
    await append_audit(
        session,
        actor=f"person:{person.id}",
        action="household.link_shown",
        details={"invitation_id": str(wanted)},
    )
    await session.commit()
    kind = "bring_back" if person_id is not None else "invite"
    return render(
        request,
        "fragments/invitation_link.html",
        link=f"{str(request.base_url).rstrip('/')}{INVITE_PATH}/{token}",
        once=request_texts(request)(f"action.card.once.household.{kind}", name=name),
    )


# --- the household page --------------------------------------------------------------------------


@dataclass(frozen=True)
class PersonRow:
    """A person as the household page shows them."""

    id: str
    name: str
    role: str
    state: str  # active, guest, stay_over, removed
    state_text: str


@dataclass(frozen=True)
class InvitationRow:
    """An invitation that waits, as the household page shows it (never its link)."""

    name: str
    role: str
    stay: str | None
    expires: str


def _person_row(person: Person, now: datetime, texts: Texts) -> PersonRow:
    if person.disabled_at is not None:
        state = "removed"
        text = texts("household.state.removed", date=texts.date(person.disabled_at))
    elif person.expires_at is not None and person.expires_at <= now:
        state = "stay_over"
        text = texts("household.state.stay_over", date=texts.date(person.expires_at))
    elif person.expires_at is not None:
        state = "guest"
        text = texts("household.state.guest", date=texts.date(person.expires_at))
    else:
        state = "active"
        text = texts("household.state.active")
    return PersonRow(str(person.id), person.display_name, person.role, state, text)


@router.get(HOUSEHOLD_PATH)
async def household_page(
    request: Request,
    person: Annotated[Person, Depends(require_owner)],
    session: Annotated[AsyncSession, Depends(get_session)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
) -> Response:
    now = clock()
    texts = request_texts(request)
    everyone = await session.execute(select(Person).order_by(Person.created_at, Person.id))
    people = [_person_row(someone, now, texts) for someone in everyone.scalars()]
    invitations = [
        InvitationRow(
            name=invitation.display_name,
            role=invitation.role,
            stay=None if invitation.guest_days is None else _days(invitation.guest_days, texts),
            expires=texts.date(invitation.expires_at),
        )
        for invitation in await open_invitations(session, now)
    ]
    return render(request, "household.html", person=person, people=people, invitations=invitations)
