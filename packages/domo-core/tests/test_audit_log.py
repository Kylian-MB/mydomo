"""The audit log: the hash chain, its verification, and what may be written into it."""

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta, timezone
from types import MappingProxyType

import pytest
from domo_core.audit import log as audit_log
from domo_core.audit.log import (
    FORBIDDEN_DETAIL_KEYS,
    GENESIS_HASH,
    ChainCheck,
    append_audit,
    canonical_json,
    entry_hash,
    verify_audit_chain,
)
from domo_core.audit.models import AuditEntry
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from web_support import OWNER_PASSWORD, audit_actions


async def append_n(session, count, **overrides):
    """``count`` entries, one after the other."""
    values = {"actor": "system", "action": "t"} | overrides
    return [await append_audit(session, details={"i": i}, **values) for i in range(count)]


async def count_entries(session) -> int:
    return (await session.execute(select(func.count()).select_from(AuditEntry))).scalar_one()


@pytest.fixture
async def allow_tampering(session):
    """The table refuses UPDATE and DELETE (see the migration). These tests play someone who
    reached the database and got around that: the guard is switched off for the test's own
    transaction, which is rolled back at its end."""
    await session.execute(text("ALTER TABLE audit_log DISABLE TRIGGER audit_log_append_only"))


# --- the brief's tests ------------------------------------------------------------------------


async def test_chain_verifies(session):
    for i in range(3):
        await append_audit(session, actor="system", action="test.event", details={"i": i})
    assert await verify_audit_chain(session) == ChainCheck(ok=True, count=3, first_bad_id=None)


async def test_tampering_is_detected(session, allow_tampering):
    entries = [
        await append_audit(session, actor="system", action="t", details={"i": i}) for i in range(3)
    ]
    await session.execute(
        text("UPDATE audit_log SET details='{\"i\": 99}' WHERE id=:id"), {"id": entries[1].id}
    )
    check = await verify_audit_chain(session)
    assert not check.ok and check.first_bad_id == entries[1].id


async def test_concurrent_appends_keep_chain(session_factory):
    async def one(i):
        async with session_factory() as s, s.begin():
            await append_audit(s, actor="system", action="t", details={"i": i})

    await asyncio.gather(*(one(i) for i in range(10)))
    async with session_factory() as s:
        assert (await verify_audit_chain(s)).ok


async def test_secret_like_keys_are_refused(session):
    with pytest.raises(ValueError):
        await append_audit(
            session, actor="system", action="t", details={"nested": {"password": "x"}}
        )


async def test_login_is_audited(client, owner_account, session):
    await client.post("/login", data={"username": "owner", "password": OWNER_PASSWORD})
    assert "auth.login" in await audit_actions(session)


async def test_audit_page_requires_login(client):
    assert (await client.get("/audit")).status_code == 303


# --- the canonical form and the hash ----------------------------------------------------------


def test_the_constants_are_the_documented_ones():
    assert GENESIS_HASH == "0" * 64
    assert frozenset({"value", "password", "secret", "token", "api_key"}) == FORBIDDEN_DETAIL_KEYS


def test_canonical_json_sorts_keys_and_has_no_whitespace():
    assert canonical_json({"b": 1, "a": {"d": [1, 2], "c": None}}) == (
        b'{"a":{"c":null,"d":[1,2]},"b":1}'
    )


def test_canonical_json_keeps_non_ascii_text_as_utf8():
    assert canonical_json({"é": "café ☃"}) == '{"é":"café ☃"}'.encode()


def test_canonical_json_refuses_what_json_cannot_say():
    with pytest.raises(ValueError):
        canonical_json({"x": float("nan")})


def test_the_hash_covers_exactly_these_bytes():
    """The canonical form is part of the format: changing it would invalidate every log."""
    prev = "ab" * 32
    body = (
        '{"action":"chat.turn","actor":"person:00000000-0000-4000-8000-000000000001",'
        '"details":{"a":[1,2.5,null,true],"b":"café ☃","c":{}},'
        '"ts":"2030-01-02T03:04:05.678000+00:00"}'
    )
    expected = hashlib.sha256(prev.encode() + body.encode("utf-8")).hexdigest()
    assert expected == "b7cd69769915d11ee2b459a867a322b0a3cba548ba2593a879df868c81db600f"
    computed = entry_hash(
        prev,
        ts=datetime(2030, 1, 2, 3, 4, 5, 678000, tzinfo=UTC),
        actor="person:00000000-0000-4000-8000-000000000001",
        action="chat.turn",
        details={"c": {}, "b": "café ☃", "a": [1, 2.5, None, True]},
    )
    assert computed == expected


def test_the_timestamp_always_has_its_microseconds():
    """``isoformat()`` drops them when they are zero: the format must not depend on the value."""
    ts = datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC)
    body = b'{"action":"a","actor":"system","details":{},"ts":"2030-01-01T12:00:00.000000+00:00"}'
    assert entry_hash(GENESIS_HASH, ts=ts, actor="system", action="a", details={}) == (
        hashlib.sha256(GENESIS_HASH.encode() + body).hexdigest()
    )


def test_the_timestamp_is_hashed_in_utc():
    paris_summer = timezone(timedelta(hours=2))
    same_instant = (
        datetime(2030, 7, 1, 14, 0, tzinfo=paris_summer),
        datetime(2030, 7, 1, 12, 0, tzinfo=UTC),
    )
    hashes = {
        entry_hash(GENESIS_HASH, ts=ts, actor="system", action="a", details={})
        for ts in same_instant
    }
    assert len(hashes) == 1


def test_a_timestamp_without_a_timezone_is_refused():
    with pytest.raises(ValueError):
        entry_hash(GENESIS_HASH, ts=datetime(2030, 1, 1), actor="system", action="a", details={})


async def test_the_stored_row_hashes_to_the_pinned_bytes(session, monkeypatch):
    """End to end: the timestamp that is hashed is the one that is stored, and the hash is the
    SHA-256 of the previous hash and these bytes."""
    monkeypatch.setattr(audit_log, "_utcnow", lambda: datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC))
    entry = await append_audit(
        session, actor="system", action="test.event", details={"b": "é", "a": 1}
    )
    row = (
        await session.execute(
            select(AuditEntry.ts, AuditEntry.prev_hash, AuditEntry.hash).where(
                AuditEntry.id == entry.id
            )
        )
    ).one()
    body = (
        '{"action":"test.event","actor":"system","details":{"a":1,"b":"é"},'
        '"ts":"2030-01-02T03:04:05.000000+00:00"}'
    )
    assert row.ts == datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert row.prev_hash == GENESIS_HASH
    assert row.hash == hashlib.sha256((GENESIS_HASH + body).encode("utf-8")).hexdigest()


async def test_the_timestamp_is_set_by_the_application_not_the_database(session):
    before = datetime.now(UTC)
    entry = await append_audit(session, actor="system", action="t", details={})
    after = datetime.now(UTC)
    stored = (
        await session.execute(select(AuditEntry.ts).where(AuditEntry.id == entry.id))
    ).scalar_one()
    assert stored.tzinfo is not None
    assert before <= stored <= after
    assert stored == entry.ts


# --- appending --------------------------------------------------------------------------------


async def test_entries_are_linked_to_each_other(session):
    first, second, third = await append_n(session, 3)
    assert first.prev_hash == GENESIS_HASH
    assert second.prev_hash == first.hash
    assert third.prev_hash == second.hash
    assert len({first.hash, second.hash, third.hash}) == 3
    assert first.id < second.id < third.id


async def test_append_returns_the_stored_entry(session):
    entry = await append_audit(session, actor="person:abc", action="auth.login", details={"x": 1})
    assert isinstance(entry, AuditEntry)
    assert (entry.actor, entry.action, entry.details) == ("person:abc", "auth.login", {"x": 1})
    assert len(entry.hash) == 64 and entry.id is not None


async def test_append_audit_does_not_commit(session_factory):
    """The caller owns the transaction: what it has not committed is not in the log."""
    async with session_factory() as writer, session_factory() as reader:
        await append_audit(writer, actor="system", action="t", details={})
        assert await count_entries(reader) == 0
        await writer.commit()
        assert await count_entries(reader) == 1


async def test_a_rolled_back_append_leaves_no_trace_and_no_break(session_factory):
    async with session_factory() as s, s.begin():
        await append_audit(s, actor="system", action="t", details={"i": 0})
    async with session_factory() as s:
        await append_audit(s, actor="system", action="t", details={"i": "lost"})
        await s.rollback()
    async with session_factory() as s, s.begin():
        await append_audit(s, actor="system", action="t", details={"i": 2})
    async with session_factory() as s:
        ids = (await s.execute(select(AuditEntry.id).order_by(AuditEntry.id))).scalars().all()
        assert ids[1] - ids[0] > 1  # the lost entry used up an id: the chain tolerates the gap
        assert await verify_audit_chain(s) == ChainCheck(ok=True, count=2, first_bad_id=None)


async def test_a_second_writer_waits_for_the_first_to_commit(session_factory):
    """Without the lock, both would chain onto the same entry and fork the log."""
    async with session_factory() as first, session_factory() as second:
        a = await append_audit(first, actor="system", action="t", details={"i": 1})
        waiting = asyncio.create_task(
            append_audit(second, actor="system", action="t", details={"i": 2})
        )
        done, _ = await asyncio.wait({waiting}, timeout=0.5)
        assert not done
        await first.commit()
        b = await asyncio.wait_for(waiting, timeout=10)
        await second.commit()
        assert b.prev_hash == a.hash


async def test_many_concurrent_appends_make_one_chain(session_factory):
    async def one(i):
        async with session_factory() as s, s.begin():
            await append_audit(s, actor="system", action="t", details={"i": i})

    await asyncio.gather(*(one(i) for i in range(20)))
    async with session_factory() as s:
        assert await verify_audit_chain(s) == ChainCheck(ok=True, count=20, first_bad_id=None)
        prev_hashes = (await s.execute(select(AuditEntry.prev_hash))).scalars().all()
        assert len(set(prev_hashes)) == 20  # no two entries chain onto the same one


async def test_a_transaction_that_could_not_see_the_last_entry_is_refused(engine):
    """The lock only works if the read that follows it sees what the previous writer committed."""
    strict = engine.execution_options(isolation_level="REPEATABLE READ")
    async with AsyncSession(strict) as s:
        with pytest.raises(RuntimeError, match="read committed"):
            await append_audit(s, actor="system", action="t", details={})
        await s.rollback()


# --- what may be written ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "details",
    [
        pytest.param({"password": "x"}, id="top level"),
        pytest.param({"nested": {"password": "x"}}, id="nested"),
        pytest.param({"items": [{"token": "x"}]}, id="in a list"),
        pytest.param({"a": [[{"api_key": "x"}]]}, id="in lists of lists"),
        pytest.param({"a": {"b": {"c": {"secret": "x"}}}}, id="deep"),
        pytest.param({"Password": "x"}, id="capitalised"),
        pytest.param({"SECRET": "x"}, id="upper case"),
        pytest.param({"Api_Key": "x"}, id="mixed case"),
        pytest.param({"a": [{"B": {"VALUE": 1}}]}, id="mixed case, deep, in a list"),
        pytest.param({"ok": 1, "token": "x"}, id="next to harmless keys"),
    ],
)
async def test_forbidden_keys_are_refused_at_any_depth(session, details):
    with pytest.raises(ValueError, match="forbidden"):
        await append_audit(session, actor="system", action="t", details=details)
    assert await count_entries(session) == 0


async def test_the_error_names_the_key_but_never_shows_the_value(session):
    with pytest.raises(ValueError) as refused:
        await append_audit(
            session, actor="system", action="t", details={"a": {"password": "hunter2-do-not-leak"}}
        )
    assert "password" in str(refused.value)
    assert "hunter2-do-not-leak" not in str(refused.value)


async def test_only_whole_keys_are_forbidden_not_words_inside_them_or_values(session):
    entry = await append_audit(
        session,
        actor="system",
        action="secret.requested",
        details={"secret_ref": "provider-key", "token_count": 3, "values": [1], "note": "password"},
    )
    assert entry.details["token_count"] == 3


@pytest.mark.parametrize(
    "details",
    [
        pytest.param({1: "x"}, id="a key that is not text"),
        pytest.param({"a": object()}, id="an arbitrary object"),
        pytest.param({"a": {1, 2}}, id="a set"),
        pytest.param({"a": b"bytes"}, id="bytes"),
        pytest.param({"a": float("nan")}, id="nan"),
        pytest.param({"a": [float("inf")]}, id="infinity"),
        pytest.param({"a": "x\x00y"}, id="a NUL character in a value"),
        pytest.param({"a\x00b": 1}, id="a NUL character in a key"),
        pytest.param({"a": ["\ud800"]}, id="a lone surrogate"),
    ],
)
async def test_what_the_log_cannot_store_is_refused_before_it_is_written(session, details):
    with pytest.raises(ValueError):
        await append_audit(session, actor="system", action="t", details=details)
    assert await count_entries(session) == 0


def _nested(depth: int) -> dict:
    root: dict = {}
    node = root
    for _ in range(depth):
        node["a"] = {}
        node = node["a"]
    return root


def _cycle() -> dict:
    cyclic: dict = {}
    cyclic["self"] = cyclic
    return cyclic


@pytest.mark.parametrize("details", [_nested(100), _cycle()], ids=["too deep", "a cycle"])
async def test_details_that_nest_too_deeply_are_refused(session, details):
    with pytest.raises(ValueError, match="deep"):
        await append_audit(session, actor="system", action="t", details=details)


@pytest.mark.parametrize(
    ("actor", "action"), [("", "t"), ("system", ""), ("sys\x00tem", "t"), ("system", "t\x00")]
)
async def test_the_actor_and_the_action_must_be_usable_text(session, actor, action):
    with pytest.raises(ValueError):
        await append_audit(session, actor=actor, action=action, details={})


async def test_mappings_and_tuples_are_stored_as_json_objects_and_arrays(session):
    entry = await append_audit(
        session, actor="system", action="t", details=MappingProxyType({"a": (1, 2), "b": {}})
    )
    assert entry.details == {"a": [1, 2], "b": {}}
    assert await verify_audit_chain(session) == ChainCheck(ok=True, count=1, first_bad_id=None)


async def test_the_callers_details_are_copied_not_kept(session):
    details = {"a": [1]}
    entry = await append_audit(session, actor="system", action="t", details=details)
    details["a"].append(2)
    assert entry.details == {"a": [1]}


ROUND_TRIP = {
    "text": "café ☃ \U0001f600   \\ \" ' <script>&",
    "controls": "\t\n\r\x01\x1f\x7f",
    "ints": [0, -1, 2**63, -(2**70), 10**40],
    "floats": [0.1, 1.5e-10, 1e-7, 123456.789, 5e-324, 2.5, -0.5],
    "integral_floats": [1.0, -0.0, 1e16, 1e23, 1.7976931348623157e308],
    "constants": [True, False, None],
    "empty": {"list": [], "dict": {}},
    "": "an empty key",
    "nested": {"z": {"y": [{"x": [[]]}]}},
}


async def test_details_survive_the_database_unchanged_so_the_chain_still_verifies(session):
    """JSONB does not keep key order or number spelling: what is hashed must be what comes back."""
    await append_audit(session, actor="system", action="t", details=ROUND_TRIP)
    await append_audit(session, actor="system", action="t", details=ROUND_TRIP)
    assert await verify_audit_chain(session) == ChainCheck(ok=True, count=2, first_bad_id=None)
    stored = (await session.execute(select(AuditEntry.details).limit(1))).scalar_one()
    assert stored["floats"] == ROUND_TRIP["floats"]
    assert stored["text"] == ROUND_TRIP["text"]
    # A float with an integral value is an integer in the log (the database would turn 1e16 into
    # one anyway, and the two sides must agree on the spelling).
    assert [type(x) for x in stored["integral_floats"]] == [int] * 5
    assert stored["integral_floats"][:3] == [1, 0, 10**16]


# --- verifying --------------------------------------------------------------------------------


async def test_an_empty_log_is_an_intact_chain(session):
    assert await verify_audit_chain(session) == ChainCheck(ok=True, count=0, first_bad_id=None)


TAMPERINGS = [
    # (statement, index of the entry it hits, index of the entry that must be reported)
    pytest.param("UPDATE audit_log SET details = '{\"i\": 99}' WHERE id = :id", 1, 1, id="details"),
    pytest.param("UPDATE audit_log SET actor = 'person:mallory' WHERE id = :id", 1, 1, id="actor"),
    pytest.param("UPDATE audit_log SET action = 'other' WHERE id = :id", 1, 1, id="action"),
    pytest.param(
        "UPDATE audit_log SET ts = ts + interval '1 second' WHERE id = :id", 1, 1, id="ts"
    ),
    pytest.param("UPDATE audit_log SET hash = repeat('a', 64) WHERE id = :id", 1, 1, id="hash"),
    pytest.param(
        "UPDATE audit_log SET prev_hash = repeat('b', 64) WHERE id = :id", 1, 1, id="prev_hash"
    ),
    pytest.param("UPDATE audit_log SET details = '{\"i\": 99}' WHERE id = :id", 0, 0, id="first"),
    pytest.param("UPDATE audit_log SET details = '{\"i\": 99}' WHERE id = :id", 3, 3, id="last"),
    pytest.param("DELETE FROM audit_log WHERE id = :id", 1, 2, id="a deleted entry"),
    pytest.param("DELETE FROM audit_log WHERE id = :id", 0, 1, id="a deleted first entry"),
]


@pytest.mark.parametrize(("statement", "target", "reported"), TAMPERINGS)
async def test_every_kind_of_tampering_is_located(
    session, allow_tampering, statement, target, reported
):
    entries = await append_n(session, 4)
    await session.execute(text(statement), {"id": entries[target].id})
    check = await verify_audit_chain(session)
    assert check.ok is False
    assert check.first_bad_id == entries[reported].id


async def test_rewriting_an_entry_consistently_still_breaks_the_next_one(session, allow_tampering):
    """Someone who recomputes the hash of the entry they changed is caught by its successor."""
    entries = await append_n(session, 3)
    forged = entry_hash(
        entries[1].prev_hash,
        ts=entries[1].ts,
        actor=entries[1].actor,
        action=entries[1].action,
        details={"i": 99},
    )
    await session.execute(
        text("UPDATE audit_log SET details = '{\"i\": 99}', hash = :hash WHERE id = :id"),
        {"hash": forged, "id": entries[1].id},
    )
    check = await verify_audit_chain(session)
    assert (check.ok, check.first_bad_id) == (False, entries[2].id)


async def test_the_first_bad_entry_is_reported_and_the_count_stops_there(session, allow_tampering):
    entries = await append_n(session, 5)
    for index in (2, 4):
        await session.execute(
            text("UPDATE audit_log SET details = '{\"i\": 99}' WHERE id = :id"),
            {"id": entries[index].id},
        )
    assert await verify_audit_chain(session) == ChainCheck(
        ok=False, count=3, first_bad_id=entries[2].id
    )


async def test_a_value_the_chain_cannot_even_hash_is_reported_not_raised(session, allow_tampering):
    """Hand-edited data can be anything: verifying it must answer, not crash."""
    entries = await append_n(session, 2)
    # A number too large for a float: Python reads it back as infinity, which has no JSON form.
    too_large = "1" + "0" * 400 + ".5"
    await session.execute(
        text(f"UPDATE audit_log SET details = '{{\"x\": {too_large}}}' WHERE id = :id"),
        {"id": entries[1].id},
    )
    check = await verify_audit_chain(session)
    assert (check.ok, check.first_bad_id) == (False, entries[1].id)


NOT_AN_OBJECT = [
    # (the details the entry was written with, what someone put there instead)
    pytest.param({"i": 1}, "null", id="null"),
    pytest.param({"i": 1}, "5", id="an integer"),
    pytest.param({"i": 1}, "true", id="a boolean"),
    pytest.param({"i": 1}, "[1]", id="a list"),
    pytest.param({"i": 1}, '"text"', id="a string"),
    pytest.param({}, "[]", id="an empty list for an empty object"),
    pytest.param({}, '""', id="an empty string for an empty object"),
    pytest.param({"a": "b"}, '[["a", "b"]]', id="pairs for an object"),
]


@pytest.mark.parametrize(("written", "put_instead"), NOT_AN_OBJECT)
async def test_details_that_are_not_an_object_are_reported_not_raised_or_passed(
    session, allow_tampering, written, put_instead
):
    """A change of representation must not verify (``[]`` and ``{}`` would hash alike if the
    value were converted), and a value that is not an object must not crash the check."""
    first = await append_audit(session, actor="system", action="t", details={"i": 0})
    second = await append_audit(session, actor="system", action="t", details=written)
    await append_audit(session, actor="system", action="t", details={"i": 2})
    await session.execute(
        text(f"UPDATE audit_log SET details = '{put_instead}'::jsonb WHERE id = :id"),
        {"id": second.id},
    )
    assert first.id < second.id
    assert await verify_audit_chain(session) == ChainCheck(
        ok=False, count=2, first_bad_id=second.id
    )


async def test_a_number_python_refuses_to_decode_is_reported_not_raised(session, allow_tampering):
    """``json.loads`` refuses integers of more than 4300 digits: that must not escape from
    the batch read and take the whole check down."""
    entries = await append_n(session, 3)
    huge = "1" * 5000
    await session.execute(
        text(f"UPDATE audit_log SET details = '{{\"i\": {huge}}}'::jsonb WHERE id = :id"),
        {"id": entries[1].id},
    )
    assert await verify_audit_chain(session) == ChainCheck(
        ok=False, count=2, first_bad_id=entries[1].id
    )


@pytest.mark.parametrize(
    "timestamp",
    ["infinity", "-infinity", "10000-01-01 00:00:00+00", "0001-01-01 00:00:00+00 BC"],
)
async def test_a_timestamp_python_cannot_represent_is_reported_not_raised(
    session, allow_tampering, timestamp
):
    entries = await append_n(session, 3)
    await session.execute(
        text(f"UPDATE audit_log SET ts = '{timestamp}' WHERE id = :id"), {"id": entries[1].id}
    )
    assert await verify_audit_chain(session) == ChainCheck(
        ok=False, count=2, first_bad_id=entries[1].id
    )


async def test_the_chain_is_read_in_batches_without_losing_the_link_between_them(
    session, allow_tampering, monkeypatch
):
    monkeypatch.setattr(audit_log, "VERIFY_BATCH_SIZE", 2)
    entries = await append_n(session, 5)
    assert await verify_audit_chain(session) == ChainCheck(ok=True, count=5, first_bad_id=None)
    # The break is in the first entry of the third batch, which links to the second batch's last.
    await session.execute(
        text("UPDATE audit_log SET details = '{\"i\": 99}' WHERE id = :id"),
        {"id": entries[4].id},
    )
    check = await verify_audit_chain(session)
    assert (check.ok, check.count, check.first_bad_id) == (False, 5, entries[4].id)
    # And a deletion at a batch boundary (the next batch starts after the missing entry).
    await session.execute(text("DELETE FROM audit_log WHERE id = :id"), {"id": entries[2].id})
    check = await verify_audit_chain(session)
    assert (check.ok, check.first_bad_id) == (False, entries[3].id)


async def test_ids_that_are_not_contiguous_are_fine(session):
    first = await append_audit(session, actor="system", action="t", details={})
    await session.execute(text("SELECT nextval('audit_log_id_seq')"))  # an id nobody uses
    second = await append_audit(session, actor="system", action="t", details={})
    assert second.id - first.id == 2
    assert (await verify_audit_chain(session)).ok


# --- the table refuses to be edited ------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    ["UPDATE audit_log SET actor = 'x' WHERE id = :id", "DELETE FROM audit_log WHERE id = :id"],
)
async def test_the_database_refuses_to_change_or_remove_an_entry(session, statement):
    entry = await append_audit(session, actor="system", action="t", details={})
    with pytest.raises(DBAPIError, match="append-only"):
        await session.execute(text(statement), {"id": entry.id})


async def test_the_refusal_does_not_stop_the_log_from_growing(session):
    entry = await append_audit(session, actor="system", action="t", details={})
    with pytest.raises(DBAPIError):
        async with session.begin_nested():
            await session.execute(text("DELETE FROM audit_log WHERE id = :id"), {"id": entry.id})
    await append_audit(session, actor="system", action="t", details={})
    assert await verify_audit_chain(session) == ChainCheck(ok=True, count=2, first_bad_id=None)
