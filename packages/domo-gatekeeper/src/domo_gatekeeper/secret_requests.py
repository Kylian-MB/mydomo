"""Single-use secret requests.

When the core needs a secret it asks the gatekeeper for a request. The gatekeeper answers with a
one-time form path; the owner opens it in a browser and types the value into the form served by
the gatekeeper itself, so the value goes straight into the vault and never transits through the
core.

The form path embeds an unguessable token. Only its SHA-256 hash is stored, so the database
cannot be used to open a form, and the token is returned once, by ``create``. A request is
pending until it is fulfilled, superseded by a newer request for the same ref, or expired.

A secret is one value or an account (a user and a password): which one follows from the places it
is bound to (``PendingRequest.fields``), so the store keeps nothing more for it. For an account,
the form joins the two values into the one string the vault keeps (``vault.join_account``).

A request keeps the texts of its form (``language``, ``texts``): those the core sent, in the
words of the one who gives the secret, or none during the installation, when the form shows the
gatekeeper's fixed texts (``public_app``).

The store keeps its own SQLite file, apart from the vault. Every operation opens a short-lived
connection, so a store can be shared by the threads of a web server without locking of its own. A
file made before the texts of the form gains their columns when it is opened.
"""

import hashlib
import os
import secrets
import sqlite3
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

from domo_common.gatekeeper_api import (
    Binding,
    SecretFields,
    SecretRequestCreate,
    SecretRequestView,
    location_fields,
)
from pydantic import TypeAdapter

from domo_gatekeeper.errors import BindingMismatchError, RequestNotPendingError
from domo_gatekeeper.vault import Vault

DEFAULT_TTL = timedelta(minutes=15)
FORM_PATH_PREFIX = "/vault/r/"
_BUSY_TIMEOUT_SECONDS = 30.0
_TOKEN_BYTES = 32

_SCHEMA = """
CREATE TABLE IF NOT EXISTS secret_requests (
    id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    ref TEXT NOT NULL,
    purpose TEXT NOT NULL,
    bindings_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'fulfilled', 'superseded')),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    fulfilled_at TEXT,
    language TEXT,
    texts_json TEXT
)
"""
# The columns a file made before them gains (0.7.0): the texts of the form.
_ADDED_COLUMNS = ("language", "texts_json")
_COLUMNS = "id, ref, purpose, bindings_json, status, expires_at, language, texts_json"
# id, ref, purpose, bindings_json, status, expires_at, language, texts_json
_Row = tuple[str, str, str, str, str, str, str | None, str | None]

_BINDINGS = TypeAdapter(list[Binding])
_TEXTS = TypeAdapter(dict[str, str])

_Status = Literal["pending", "fulfilled", "expired", "superseded"]


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class PendingRequest:
    """What the form needs to know about a request that can still be fulfilled: with the texts
    of its form in ``language``, or none (the installation: the gatekeeper's fixed texts)."""

    id: str
    ref: str
    purpose: str
    bindings: list[Binding]
    language: str | None = None
    texts: Mapping[str, str] | None = None

    @property
    def fields(self) -> SecretFields:
        """What the secret is made of, and so which fields its form has: the one its places
        take (all of them take the same, as ``SecretRequestCreate`` checks)."""
        return location_fields(self.bindings[0].location)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _timestamp(moment: datetime) -> str:
    """A fixed-width UTC timestamp: stored timestamps then compare correctly as text."""
    return moment.isoformat(timespec="microseconds")


def _create_private_file(path: Path) -> None:
    """Create an empty file readable by its owner only, unless it already exists."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    with suppress(FileExistsError):
        os.close(os.open(path, flags, 0o600))


class SecretRequestStore:
    """Secret requests and their one-time tokens."""

    def __init__(self, db_path: Path, *, clock: Callable[[], datetime] = utcnow) -> None:
        self._db_path = db_path
        self._clock = clock
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _create_private_file(db_path)
        with self._transaction() as conn:
            conn.execute(_SCHEMA)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(secret_requests)")}
            for column in _ADDED_COLUMNS:
                if column not in columns:
                    conn.execute(f"ALTER TABLE secret_requests ADD COLUMN {column} TEXT")

    @contextmanager
    def _connection(self) -> Generator[sqlite3.Connection]:
        # Autocommit mode: transactions are explicit (see _transaction).
        conn = sqlite3.connect(self._db_path, timeout=_BUSY_TIMEOUT_SECONDS, isolation_level=None)
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _transaction(self) -> Generator[sqlite3.Connection]:
        """A write transaction that holds the database lock from its very first statement."""
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("The clock must return timezone-aware datetimes.")
        return now.astimezone(UTC)

    @staticmethod
    def _status(row: _Row, now: datetime) -> _Status:
        """The status of a stored row: a pending request past its expiry is ``expired``."""
        stored_status, expires_at = row[4], row[5]
        if stored_status == "pending" and datetime.fromisoformat(expires_at) <= now:
            return "expired"
        return cast(_Status, stored_status)

    def create(
        self, req: SecretRequestCreate, vault: Vault, ttl: timedelta = DEFAULT_TTL
    ) -> SecretRequestView:
        """Open a request for ``req.ref``; its view carries the one-time ``form_path``.

        Refused at once (``BindingMismatchError``) when the secret already exists with other
        bindings: the owner would type a value that could never be stored. Pending requests for
        the same ref are superseded, so only the newest link works. Expired ones are left alone.
        """
        if ttl <= timedelta(0):
            raise ValueError("The ttl of a secret request must be positive.")
        existing = vault.metadata(req.ref)
        if existing is not None and set(existing.bindings) != set(req.bindings):
            raise BindingMismatchError(
                f"The secret {req.ref!r} already exists with other bindings; "
                "bindings are immutable."
            )

        token = secrets.token_urlsafe(_TOKEN_BYTES)
        request_id = secrets.token_urlsafe(16)
        bindings = list(dict.fromkeys(req.bindings))
        with self._transaction() as conn:
            now = self._now()
            expires_at = now + ttl
            conn.execute(
                "UPDATE secret_requests SET status = 'superseded'"
                " WHERE ref = ? AND status = 'pending' AND expires_at > ?",
                (req.ref, _timestamp(now)),
            )
            conn.execute(
                "INSERT INTO secret_requests (id, token_hash, ref, purpose, bindings_json,"
                " status, created_at, expires_at, language, texts_json)"
                " VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
                (
                    request_id,
                    _hash_token(token),
                    req.ref,
                    req.purpose,
                    _BINDINGS.dump_json(bindings).decode(),
                    _timestamp(now),
                    _timestamp(expires_at),
                    req.language,
                    None if req.texts is None else _TEXTS.dump_json(req.texts).decode(),
                ),
            )
        return SecretRequestView(
            id=request_id,
            ref=req.ref,
            status="pending",
            form_path=f"{FORM_PATH_PREFIX}{token}",
            expires_at=expires_at,
        )

    def get(self, request_id: str) -> SecretRequestView | None:
        """The state of a request. ``form_path`` is always None: the token is not recoverable."""
        now = self._now()
        with self._connection() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM secret_requests WHERE id = ?", (request_id,)
            ).fetchone()
        if row is None:
            return None
        found_id, ref, _purpose, _bindings_json, _status, expires_at, _, _ = cast(_Row, row)
        return SecretRequestView(
            id=found_id,
            ref=ref,
            status=self._status(cast(_Row, row), now),
            form_path=None,
            expires_at=datetime.fromisoformat(expires_at),
        )

    def pending_by_token(self, token: str) -> PendingRequest | None:
        """The request behind ``token`` when it can still be fulfilled, else None."""
        now = self._now()
        with self._connection() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM secret_requests WHERE token_hash = ?",
                (_hash_token(token),),
            ).fetchone()
        if row is None or self._status(cast(_Row, row), now) != "pending":
            return None
        request_id, ref, purpose, bindings_json, _status, _expires_at, language, texts_json = cast(
            _Row, row
        )
        return PendingRequest(
            id=request_id,
            ref=ref,
            purpose=purpose,
            bindings=_BINDINGS.validate_json(bindings_json),
            language=language,
            texts=None if texts_json is None else _TEXTS.validate_json(texts_json),
        )

    def fulfil(self, token: str, value: str, vault: Vault) -> SecretRequestView:
        """Store ``value`` in the vault for the request behind ``token``, once.

        Raises ``RequestNotPendingError`` when the token is unknown, expired, superseded or
        already used. The request is looked up and marked fulfilled in one write transaction,
        around the vault write: of two concurrent submissions of a token exactly one succeeds,
        and a refused value (``InvalidSecretValueError``, ``BindingMismatchError``) leaves the
        request pending.
        """
        with self._transaction() as conn:
            now = self._now()
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM secret_requests WHERE token_hash = ?",
                (_hash_token(token),),
            ).fetchone()
            if row is None or self._status(cast(_Row, row), now) != "pending":
                raise RequestNotPendingError(
                    "The secret request is unknown, expired, superseded or already fulfilled."
                )
            request_id, ref, purpose, bindings_json, _status, expires_at, _, _ = cast(_Row, row)
            vault.put(ref, value, purpose=purpose, bindings=_BINDINGS.validate_json(bindings_json))
            conn.execute(
                "UPDATE secret_requests SET status = 'fulfilled', fulfilled_at = ? WHERE id = ?",
                (_timestamp(now), request_id),
            )
        return SecretRequestView(
            id=request_id,
            ref=ref,
            status="fulfilled",
            form_path=None,
            expires_at=datetime.fromisoformat(expires_at),
        )
