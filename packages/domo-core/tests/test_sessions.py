import hashlib
from datetime import UTC, datetime, timedelta

from domo_core.accounts.models import Account, Person, WebSession
from domo_core.accounts.sessions import (
    SESSION_COOKIE,
    clear_session_cookie,
    create_session,
    resolve_session,
    revoke_session,
    set_session_cookie,
)
from sqlalchemy import func, select, update
from starlette.responses import Response
from web_support import cookie_attributes


def test_the_cookie_name():
    assert SESSION_COOKIE == "domo_session"


async def test_session_token_stored_hashed(session, account):
    raw = await create_session(session, account.id, ttl=timedelta(days=1))
    row = (await session.execute(select(WebSession))).scalar_one()
    assert raw not in row.token_hash
    assert row.token_hash == hashlib.sha256(raw.encode()).hexdigest()


async def test_tokens_are_long_random_and_distinct(session, account):
    tokens = {await create_session(session, account.id, ttl=timedelta(days=1)) for _ in range(5)}
    assert len(tokens) == 5
    assert all(len(token) >= 43 for token in tokens)  # 256 bits, url-safe base64


async def test_expired_session_resolves_to_none(session, account):
    raw = await create_session(session, account.id, ttl=timedelta(seconds=-1))
    assert await resolve_session(session, raw, datetime.now(UTC)) is None


async def test_valid_session_resolves_to_the_person(session, owner_account):
    raw = await create_session(session, owner_account.id, ttl=timedelta(days=1))
    person = await resolve_session(session, raw, datetime.now(UTC))
    assert person is not None
    assert person.id == owner_account.person_id
    assert person.role == "owner"


async def test_expiry_follows_the_ttl(session, account):
    before = datetime.now(UTC)
    await create_session(session, account.id, ttl=timedelta(days=30))
    after = datetime.now(UTC)
    expires_at = (await session.execute(select(WebSession.expires_at))).scalar_one()
    assert before + timedelta(days=30) <= expires_at <= after + timedelta(days=30)


async def test_unknown_token_resolves_to_none(session, account):
    await create_session(session, account.id, ttl=timedelta(days=1))
    assert await resolve_session(session, "not-a-real-token", datetime.now(UTC)) is None
    assert await resolve_session(session, "", datetime.now(UTC)) is None


async def test_resolving_updates_last_seen_at(session, account):
    raw = await create_session(session, account.id, ttl=timedelta(days=1))
    created = (await session.execute(select(WebSession.last_seen_at))).scalar_one()
    # A moment surely after the creation: on Windows, two readings of the clock within its tick
    # (15.6 ms) give the same time, and a "now" read right away could equal the creation's.
    later = created + timedelta(seconds=1)
    assert await resolve_session(session, raw, later) is not None
    seen = (await session.execute(select(WebSession.last_seen_at))).scalar_one()
    assert seen == later


async def test_resolving_an_expired_session_leaves_it_untouched(session, account):
    raw = await create_session(session, account.id, ttl=timedelta(seconds=-1))
    created = (await session.execute(select(WebSession.last_seen_at))).scalar_one()
    assert await resolve_session(session, raw, datetime.now(UTC)) is None
    assert (await session.execute(select(WebSession.last_seen_at))).scalar_one() == created


async def test_resolving_does_not_extend_the_expiry(session, account):
    raw = await create_session(session, account.id, ttl=timedelta(days=1))
    expires = (await session.execute(select(WebSession.expires_at))).scalar_one()
    await resolve_session(session, raw, datetime.now(UTC))
    assert (await session.execute(select(WebSession.expires_at))).scalar_one() == expires


async def test_revoked_session_no_longer_resolves(session, account):
    raw = await create_session(session, account.id, ttl=timedelta(days=1))
    other = await create_session(session, account.id, ttl=timedelta(days=1))
    await revoke_session(session, raw)
    assert await resolve_session(session, raw, datetime.now(UTC)) is None
    assert (
        await resolve_session(session, other, datetime.now(UTC)) is not None
    )  # only that session is revoked
    count = (await session.execute(select(func.count()).select_from(WebSession))).scalar_one()
    assert count == 1


async def test_revoking_an_unknown_token_is_harmless(session, account):
    await create_session(session, account.id, ttl=timedelta(days=1))
    await revoke_session(session, "not-a-real-token")
    count = (await session.execute(select(func.count()).select_from(WebSession))).scalar_one()
    assert count == 1


async def test_revoking_says_whose_session_it_was(session, account):
    raw = await create_session(session, account.id, ttl=timedelta(days=1))
    assert await revoke_session(session, raw) == account.person_id
    assert await revoke_session(session, raw) is None  # already gone
    assert await revoke_session(session, "not-a-real-token") is None


async def test_create_session_leaves_the_transaction_to_the_caller(session_factory):
    """The caller owns the transaction (the owner setup creates the person, the account and the
    session in one): nothing is committed until the caller says so."""

    async def count_sessions() -> int:
        async with session_factory() as reader:
            return (await reader.execute(select(func.count()).select_from(WebSession))).scalar_one()

    async with session_factory() as writer:
        person = Person(display_name="A", role="owner")
        writer.add(person)
        await writer.flush()
        account = Account(person_id=person.id, username="owner", password_hash="hash")
        writer.add(account)
        await writer.commit()
        await create_session(writer, account.id, ttl=timedelta(days=1))
        assert await count_sessions() == 0
        await writer.commit()
    assert await count_sessions() == 1


# --- people who are no longer in the household ---------------------------------------------------


async def test_the_session_of_a_removed_person_resolves_to_none(session, account):
    raw = await create_session(session, account.id, ttl=timedelta(days=1))
    now = datetime.now(UTC)
    await session.execute(
        update(Person).where(Person.id == account.person_id).values(disabled_at=now)
    )
    seen = (await session.execute(select(WebSession.last_seen_at))).scalar_one()
    assert await resolve_session(session, raw, now) is None
    # Nothing is touched for someone who is gone.
    assert (await session.execute(select(WebSession.last_seen_at))).scalar_one() == seen


async def test_a_guest_whose_stay_is_over_resolves_to_none(session, account):
    raw = await create_session(session, account.id, ttl=timedelta(days=30))
    end = datetime.now(UTC) + timedelta(days=2)
    await session.execute(
        update(Person).where(Person.id == account.person_id).values(role="guest", expires_at=end)
    )
    assert await resolve_session(session, raw, end - timedelta(seconds=1)) is not None
    assert await resolve_session(session, raw, end) is None
    assert await resolve_session(session, raw, end + timedelta(days=1)) is None


async def test_the_time_of_the_check_is_the_one_given(session, account):
    """The caller's clock decides (the pages pass ``get_clock``): a session is over once that
    time passes its end, whatever the machine's clock says."""
    raw = await create_session(session, account.id, ttl=timedelta(days=1))
    assert await resolve_session(session, raw, datetime.now(UTC) + timedelta(hours=23)) is not None
    assert await resolve_session(session, raw, datetime.now(UTC) + timedelta(days=2)) is None
    seen = datetime.now(UTC) + timedelta(hours=1)
    assert await resolve_session(session, raw, seen) is not None
    assert (await session.execute(select(WebSession.last_seen_at))).scalar_one() == seen


# --- expired sessions ------------------------------------------------------------------------


async def count_sessions(session) -> int:
    return (await session.execute(select(func.count()).select_from(WebSession))).scalar_one()


async def test_creating_a_session_purges_the_expired_ones(session, account):
    expired = await create_session(session, account.id, ttl=timedelta(seconds=-1))
    long_expired = await create_session(session, account.id, ttl=timedelta(days=-40))
    assert await count_sessions(session) == 1  # the first one went with the second's creation
    live = await create_session(session, account.id, ttl=timedelta(days=1))
    assert await count_sessions(session) == 1  # the second one went with the third's creation
    assert await resolve_session(session, live, datetime.now(UTC)) is not None
    assert await resolve_session(session, expired, datetime.now(UTC)) is None
    assert await resolve_session(session, long_expired, datetime.now(UTC)) is None


async def test_the_purge_keeps_every_live_session(session, account):
    first = await create_session(session, account.id, ttl=timedelta(days=1))
    await create_session(session, account.id, ttl=timedelta(seconds=-1))
    second = await create_session(session, account.id, ttl=timedelta(minutes=1))
    assert await count_sessions(session) == 2
    assert await resolve_session(session, first, datetime.now(UTC)) is not None
    assert await resolve_session(session, second, datetime.now(UTC)) is not None


async def test_the_purge_leaves_the_transaction_to_the_caller(session_factory):
    async with session_factory() as setup:
        person = Person(display_name="A", role="owner")
        setup.add(person)
        await setup.flush()
        account = Account(person_id=person.id, username="owner", password_hash="hash")
        setup.add(account)
        await setup.flush()
        # An expired row, committed for real.
        setup.add(
            WebSession(
                account_id=account.id,
                token_hash="a" * 64,
                expires_at=datetime.now(UTC) - timedelta(days=1),
            )
        )
        await setup.commit()
    async with session_factory() as writer, session_factory() as reader:
        await create_session(writer, account.id, ttl=timedelta(days=1))
        assert await count_sessions(reader) == 1  # the old row is still there for everyone else
        await writer.commit()
        assert await count_sessions(reader) == 1  # and now it is the new one only
        remaining = (await reader.execute(select(WebSession.token_hash))).scalar_one()
        assert remaining != "a" * 64


# --- the cookie ------------------------------------------------------------------------------


def test_the_session_cookie_is_hardened_in_one_place():
    response = Response()
    set_session_cookie(response, "the-token", timedelta(days=2))
    attributes = cookie_attributes(response)
    assert attributes[SESSION_COOKIE] == "the-token"
    assert attributes["max-age"] == str(2 * 24 * 3600)
    assert attributes["path"] == "/"
    assert attributes["samesite"].lower() == "strict"
    assert {"secure", "httponly"} <= attributes.keys()
    assert "domain" not in attributes  # host-only: not shared with sibling hosts


def test_clearing_the_session_cookie_uses_the_same_attributes():
    response = Response()
    clear_session_cookie(response)
    attributes = cookie_attributes(response)
    assert attributes[SESSION_COOKIE] in ("", '""')
    assert attributes["max-age"] == "0"
    assert attributes["path"] == "/"
    assert attributes["samesite"].lower() == "strict"
    assert {"secure", "httponly"} <= attributes.keys()
    assert "domain" not in attributes
