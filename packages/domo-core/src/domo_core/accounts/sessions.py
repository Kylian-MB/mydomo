"""Web sessions: a random token in a cookie, and in the database only the SHA-256 of that token.

A leak of the database therefore does not leak working cookies. The token is 256 bits of
randomness, so a plain (unsalted, fast) hash is enough: there is nothing to brute-force.

None of the database functions commits: the caller owns the transaction (the owner setup creates
the person, the account and the session in one).

A session only counts while its person is in the household (``is_active``): someone who was
removed (``disabled_at``), or a guest whose stay is over (``expires_at``), is stopped at their next
request, whatever their sessions say. Removing someone also deletes their sessions
(``revoke_sessions_of``); a guest's simply stop counting. Changing a password ends the other
sessions of the account and keeps the one it was changed from (``revoke_other_sessions``). The
time of the check is the caller's (the pages pass their clock, ``web.deps.get_clock``).

The cookie that carries the token is set and cleared here too, so that its attributes
(``Secure``, ``HttpOnly``, ``SameSite=Strict``, host-only, whole site) are written in one place.
"""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal, TypedDict
from uuid import UUID

from sqlalchemy import ColumnElement, and_, delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Account, Person, WebSession

if TYPE_CHECKING:
    # Only for the types of the cookie helpers: the rest of the module is read by code that knows
    # no web (the conversation).
    from starlette.responses import Response

SESSION_COOKIE = "domo_session"


class _CookieAttributes(TypedDict):
    path: str
    secure: bool
    httponly: bool
    samesite: Literal["strict"]


# No ``domain``: the cookie belongs to the host that set it and is not sent to its siblings.
# ``Secure`` because the page is only served over HTTPS (the edge redirects HTTP to HTTPS).
_COOKIE_ATTRIBUTES: _CookieAttributes = {
    "path": "/",
    "secure": True,
    "httponly": True,
    "samesite": "strict",
}


def set_session_cookie(response: "Response", token: str, ttl: timedelta) -> None:
    """Put the session cookie for ``token`` on ``response``; the browser keeps it for ``ttl``."""
    response.set_cookie(
        SESSION_COOKIE, token, max_age=int(ttl.total_seconds()), **_COOKIE_ATTRIBUTES
    )


def clear_session_cookie(response: "Response") -> None:
    """Tell the browser to forget the session cookie."""
    response.delete_cookie(SESSION_COOKIE, **_COOKIE_ATTRIBUTES)


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


async def create_session(session: AsyncSession, account_id: UUID, *, ttl: timedelta) -> str:
    """Open a session for ``account_id``, valid for ``ttl``; returns the raw token (for the
    cookie: it is not stored anywhere).

    The sessions that have expired are deleted on the way: nothing else would ever remove them.
    """
    raw_token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    await session.execute(delete(WebSession).where(WebSession.expires_at < now))
    session.add(
        WebSession(
            account_id=account_id,
            token_hash=_hash_token(raw_token),
            created_at=now,
            last_seen_at=now,
            expires_at=now + ttl,
        )
    )
    await session.flush()
    return raw_token


def is_active(person: Person, now: datetime) -> bool:
    """Whether the person is in the household at ``now``: they have access (``state``), are not
    removed, and are not a guest whose stay has ended. With or without an account."""
    return (
        person.state == "has_access"
        and person.disabled_at is None
        and (person.expires_at is None or person.expires_at > now)
    )


def active_at(now: datetime) -> ColumnElement[bool]:
    """``is_active`` as a condition on ``persons``, for a query."""
    return and_(
        Person.state == "has_access",
        Person.disabled_at.is_(None),
        or_(Person.expires_at.is_(None), Person.expires_at > now),
    )


async def resolve_session(session: AsyncSession, raw_token: str, now: datetime) -> Person | None:
    """The person a session token belongs to at ``now``, or ``None``: an unknown or expired
    session, or a person who is no longer in the household (``is_active``). A session that counts
    gets its ``last_seen_at`` set to ``now``; its expiry does not move. One that does not is left
    untouched."""
    token_hash = _hash_token(raw_token)
    result = await session.execute(
        update(WebSession)
        .where(
            WebSession.token_hash == token_hash,
            WebSession.expires_at > now,
            # UPDATE ... FROM accounts, persons: only the session of someone still here.
            WebSession.account_id == Account.id,
            Account.person_id == Person.id,
            active_at(now),
        )
        .values(last_seen_at=now)
        .returning(WebSession.account_id)
        .execution_options(synchronize_session=False)
    )
    account_id = result.scalar_one_or_none()
    if account_id is None:
        return None
    person = await session.execute(
        select(Person).join(Account, Account.person_id == Person.id).where(Account.id == account_id)
    )
    return person.scalar_one()


async def person_of_session(session: AsyncSession, raw_token: str, now: datetime) -> Person | None:
    """The person a session token belongs to at ``now``, as ``resolve_session`` finds them, but
    only read: the session's ``last_seen_at`` is left as it is (the texts of a page are read
    before it, ``web.texts_middleware``)."""
    found = await session.execute(
        select(Person)
        .join(Account, Account.person_id == Person.id)
        .join(WebSession, WebSession.account_id == Account.id)
        .where(
            WebSession.token_hash == _hash_token(raw_token),
            WebSession.expires_at > now,
            active_at(now),
        )
    )
    return found.scalars().first()


async def revoke_sessions_of(session: AsyncSession, person_id: UUID) -> None:
    """End every session of the person (they were removed from the household)."""
    await session.execute(
        delete(WebSession)
        .where(WebSession.account_id.in_(select(Account.id).where(Account.person_id == person_id)))
        .execution_options(synchronize_session=False)
    )


async def session_id_of(session: AsyncSession, raw_token: str) -> UUID | None:
    """The id of the session of ``raw_token``, or ``None`` when there is no such session (it ended
    meanwhile, or the token is empty or unknown). Nothing is changed."""
    result = await session.execute(
        select(WebSession.id).where(WebSession.token_hash == _hash_token(raw_token))
    )
    return result.scalar_one_or_none()


async def revoke_other_sessions(
    session: AsyncSession, account_id: UUID, keep_session_id: UUID
) -> int:
    """End every session of the account except ``keep_session_id`` (the browser that is changing
    the password stays signed in). Returns how many ended.

    The session to keep is named, never guessed: it must be a session of this account (found with
    ``session_id_of``), otherwise this is a programming error (``ValueError``) and nothing is
    ended, so that a missing or wrong "keep" can never end the current session by accident."""
    kept = await session.execute(
        select(WebSession.id).where(
            WebSession.id == keep_session_id, WebSession.account_id == account_id
        )
    )
    if kept.scalar_one_or_none() is None:
        raise ValueError("the session to keep is not a session of this account")
    result = await session.execute(
        delete(WebSession)
        .where(WebSession.account_id == account_id, WebSession.id != keep_session_id)
        .returning(WebSession.id)
        .execution_options(synchronize_session=False)
    )
    return len(result.all())


async def revoke_session(session: AsyncSession, raw_token: str) -> UUID | None:
    """End the session of ``raw_token``. Returns the id of the person it belonged to, or ``None``
    when there was no such session (nothing happens then)."""
    result = await session.execute(
        delete(WebSession)
        .where(WebSession.token_hash == _hash_token(raw_token))
        .returning(WebSession.account_id)
    )
    account_id = result.scalar_one_or_none()
    if account_id is None:
        return None
    person_id = await session.execute(select(Account.person_id).where(Account.id == account_id))
    return person_id.scalar_one_or_none()
