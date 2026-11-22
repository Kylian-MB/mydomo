"""The audit log: an append-only record of what Domo did, chained by SHA-256 hashes.

Every entry carries the hash of the entry before it, and its own hash covers that and its own
content. Changing or removing an entry therefore breaks the chain from that point on, and
``verify_audit_chain`` finds where.

The format (it is data: changing any of it would invalidate every log that exists)
----------------------------------------------------------------------------------

An entry's ``hash`` is the SHA-256, written as 64 lower-case hexadecimal characters, of

* the ``prev_hash`` text (``GENESIS_HASH`` for the first entry), as ASCII, immediately followed by
* the *canonical JSON* of ``{"action", "actor", "details", "ts"}``, as UTF-8.

Canonical JSON: keys sorted at every level, no whitespace at all (separators ``,`` and ``:``),
non-ASCII characters written as they are (not as ``\\uXXXX``), no NaN or infinity.

``ts`` is ISO 8601 in UTC with exactly six fractional digits and a ``+00:00`` suffix, for
instance ``2030-01-02T03:04:05.678000+00:00`` (the digits are there even when they are zero).
The application sets ``ts`` itself, at microsecond precision, which is what the database
stores: the value that is hashed is exactly the value that is read back.

``details`` is metadata, never content: JSON objects, arrays, strings, integers, finite numbers,
booleans and ``null``. A float whose value is a whole number is stored as an integer (the
database keeps numbers as decimals and would hand ``1e16`` back as ``10000000000000000``: the two
sides must agree on one spelling). Keys that suggest a secret (``FORBIDDEN_DETAIL_KEYS``) are
refused at any depth, whatever their case, and so are text that PostgreSQL cannot store (the NUL
character) and structures that are too deep.

Concurrency and transactions
----------------------------

``append_audit`` never commits: the caller owns the transaction, so an audit entry commits (or
rolls back) together with the change it describes. Writers are serialised by a transaction-level
advisory lock that is held until the caller's transaction ends. Append *as late as possible* in
a transaction, never before slow work: every other writer waits for it. The lock only does its
job in ``READ COMMITTED`` (PostgreSQL's default), where the read of the last hash that follows it
sees what the previous writer committed; any other level is refused.

What the chain does not do
--------------------------

It detects, it does not prevent: someone who can rewrite the whole table and recompute every
hash produces a chain that verifies, and the removal of the *last* entries cannot be seen (no
later entry vouches for them). Anchoring the latest hash somewhere outside the database would
close both; ``audit_log`` also refuses UPDATE and DELETE through a trigger (migration 0002),
which only guards against mistakes.
"""

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from pydantic import JsonValue
from sqlalchemy import Text, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import Label

from domo_core.audit.models import AuditEntry

# The ``prev_hash`` of the first entry.
GENESIS_HASH = "0" * 64

# Details never hold a secret: these keys (compared without regard to case) are refused.
FORBIDDEN_DETAIL_KEYS = frozenset({"value", "password", "secret", "token", "api_key"})

# The key of the advisory lock that serialises the writers.
APPEND_LOCK_KEY = 4242

# How deep ``details`` may nest. Far more than metadata needs; it also stops a cycle.
MAX_DETAILS_DEPTH = 32

# How many entries ``verify_audit_chain`` reads at a time: the chain is never held in memory.
VERIFY_BATCH_SIZE = 1000


def _utcnow() -> datetime:
    return datetime.now(UTC)


def canonical_json(value: JsonValue) -> bytes:
    """The canonical JSON of ``value`` (see the module docstring), as UTF-8 bytes."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def format_timestamp(ts: datetime) -> str:
    """``ts`` as the chain writes it: UTC, six fractional digits, ``+00:00``."""
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError("an audit timestamp must carry a timezone")
    return ts.astimezone(UTC).isoformat(timespec="microseconds")


def entry_hash(
    prev_hash: str, *, ts: datetime, actor: str, action: str, details: dict[str, JsonValue]
) -> str:
    """The hash of an entry that follows the one whose hash is ``prev_hash``.

    ``details`` goes into the canonical JSON as it is, without any conversion: converting it
    (``dict(...)``) would make a list of pairs hash like the object it came from.
    """
    body = canonical_json(
        {"ts": format_timestamp(ts), "actor": actor, "action": action, "details": details}
    )
    return hashlib.sha256(prev_hash.encode("utf-8") + body).hexdigest()


def _check_text(value: str, what: str) -> None:
    if "\x00" in value:
        raise ValueError(f"{what} contains a NUL character, which the database cannot store")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(f"{what} is not valid text (it contains a lone surrogate)") from None


def _clean(value: object, path: str, depth: int) -> JsonValue:
    """A copy of ``value`` that holds only what the log can store and hash identically after a
    round trip through the database; ``ValueError`` for anything else."""
    if depth > MAX_DETAILS_DEPTH:
        raise ValueError(f"{path} is nested too deeply (more than {MAX_DETAILS_DEPTH} levels)")
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} is not a finite number")
        return int(value) if value.is_integer() else value
    if isinstance(value, str):
        _check_text(value, path)
        return value
    if isinstance(value, Mapping):
        cleaned: dict[str, JsonValue] = {}
        for key, item in cast(Mapping[object, object], value).items():
            if not isinstance(key, str):
                raise ValueError(f"{path} has a key that is not text")
            _check_text(key, f"a key of {path}")
            if key.casefold() in FORBIDDEN_DETAIL_KEYS:
                raise ValueError(
                    f"{path} has the forbidden key {key!r}: audit details never hold secrets"
                )
            cleaned[key] = _clean(item, f"{path}[{key!r}]", depth + 1)
        return cleaned
    if isinstance(value, list | tuple):
        items = cast(Sequence[object], value)
        return [_clean(item, f"{path}[{index}]", depth + 1) for index, item in enumerate(items)]
    raise ValueError(f"{path} holds a {type(value).__name__}, which JSON cannot represent")


def _clean_details(details: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    cleaned = _clean(details, "details", 0)
    if not isinstance(cleaned, dict):
        raise ValueError("details must be a mapping")
    return cleaned


def _check_label(value: str, what: str) -> None:
    if not value:
        raise ValueError(f"{what} must not be empty")
    _check_text(value, what)


async def append_audit(
    session: AsyncSession, *, actor: str, action: str, details: Mapping[str, JsonValue]
) -> AuditEntry:
    """Add an entry to the end of the chain, in the caller's transaction (nothing is committed).

    ``actor`` is ``"person:<uuid>"``, ``"system"`` or ``"household"`` (``madetools.launch``).
    ``details`` is metadata (see the module docstring); a forbidden key, at any depth, or any value
    the log cannot store is a ``ValueError`` and nothing is written.
    """
    _check_label(actor, "the actor")
    _check_label(action, "the action")
    cleaned = _clean_details(details)

    lock = await session.execute(
        select(
            func.pg_advisory_xact_lock(APPEND_LOCK_KEY),
            func.current_setting("transaction_isolation"),
        )
    )
    isolation = lock.one()[1]
    if isolation != "read committed":
        raise RuntimeError(
            f"the audit log needs a read committed transaction (this one is {isolation})"
        )
    last = await session.execute(select(AuditEntry.hash).order_by(AuditEntry.id.desc()).limit(1))
    prev_hash = last.scalar_one_or_none() or GENESIS_HASH
    # Taken once the lock is held, so that the order of the timestamps follows the chain's.
    ts = _utcnow()
    entry = AuditEntry(
        ts=ts,
        actor=actor,
        action=action,
        details=cast(dict[str, object], cleaned),
        prev_hash=prev_hash,
        hash=entry_hash(prev_hash, ts=ts, actor=actor, action=action, details=cleaned),
    )
    session.add(entry)
    await session.flush()
    return entry


@dataclass(frozen=True)
class ChainCheck:
    """The result of ``verify_audit_chain``.

    ``count`` is the number of entries examined: all of them when ``ok``, otherwise up to and
    including the first one that does not verify (``first_bad_id``, whose chain is broken at or
    just before it: a removed entry is reported as the one that followed it).
    """

    ok: bool
    count: int
    first_bad_id: int | None


# The years Python's ``datetime`` can hold, with a margin: PostgreSQL stores much more (and
# ``infinity``), and asyncpg raises while *fetching* such a value, which would take the whole
# check down for one hand-edited row.
_EARLIEST_READABLE_TS = datetime(1, 1, 2, tzinfo=UTC)
_LATEST_READABLE_TS = datetime(9999, 12, 30, tzinfo=UTC)


def readable_ts() -> Label[datetime]:
    """The ``ts`` column as a query column: the stored time, or NULL when it is outside what
    Python can represent (only hand-edited data is)."""
    in_range = AuditEntry.ts.between(_EARLIEST_READABLE_TS, _LATEST_READABLE_TS)
    return case((in_range, AuditEntry.ts), else_=None).label("ts")


def details_as_text() -> Label[str]:
    """The ``details`` column as its JSON text, to be decoded by the caller: decoding it while
    fetching (what the column type does) would make one hand-edited row fail the whole read."""
    return AuditEntry.details.cast(Text).label("details")


def _entry_is_sound(
    expected_prev: str,
    *,
    ts: datetime | None,
    actor: str,
    action: str,
    details_text: str,
    prev_hash: str,
    stored_hash: str,
) -> bool:
    """Whether a stored row links to ``expected_prev`` and hashes to what it claims.

    Anything unexpected about the row makes it unsound, never an exception: an unreadable
    time, details that do not decode (a number of more than 4300 digits) or are not a JSON
    object (``null``, ``5``, ``[1]``, ``[]``...), and anything that cannot be hashed.
    """
    if ts is None or prev_hash != expected_prev:
        return False
    try:
        details: object = json.loads(details_text)
        if not isinstance(details, dict):
            return False
        return stored_hash == entry_hash(
            prev_hash,
            ts=ts,
            actor=actor,
            action=action,
            details=cast(dict[str, JsonValue], details),
        )
    except (ValueError, TypeError, RecursionError):
        return False


async def verify_audit_chain(session: AsyncSession) -> ChainCheck:
    """Walk the whole chain in order and report where it first breaks.

    Entries are read in batches of ``VERIFY_BATCH_SIZE`` (by id, from where the last batch
    stopped), so the chain is never held in memory. Ids need not be contiguous: a rolled-back
    append leaves a gap. Whatever someone did to a row, the answer is a ``ChainCheck``.
    """
    expected_prev = GENESIS_HASH
    count = 0
    last_id: int | None = None
    while True:
        query = (
            select(
                AuditEntry.id,
                readable_ts(),
                AuditEntry.actor,
                AuditEntry.action,
                details_as_text(),
                AuditEntry.prev_hash,
                AuditEntry.hash,
            )
            .order_by(AuditEntry.id)
            .limit(VERIFY_BATCH_SIZE)
        )
        if last_id is not None:
            query = query.where(AuditEntry.id > last_id)
        rows = (await session.execute(query)).all()
        if not rows:
            return ChainCheck(ok=True, count=count, first_bad_id=None)
        for entry_id, ts, actor, action, details_text, prev_hash, stored_hash in rows:
            count += 1
            if not _entry_is_sound(
                expected_prev,
                ts=ts,
                actor=actor,
                action=action,
                details_text=details_text,
                prev_hash=prev_hash,
                stored_hash=stored_hash,
            ):
                return ChainCheck(ok=False, count=count, first_bad_id=entry_id)
            expected_prev = stored_hash
        last_id = rows[-1].id
