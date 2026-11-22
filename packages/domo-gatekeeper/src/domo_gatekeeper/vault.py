"""The encrypted secret vault.

Values are encrypted with AES-256-GCM under the master key. The associated data is a versioned,
canonical form of the secret's ref and bindings (see ``_AAD_PREFIX``), so a ciphertext cannot be
moved to another ref and editing the stored bindings (to re-point a secret at another host)
makes ``reveal`` fail.
Refs, purposes and bindings are stored in clear: they are metadata, and the core is allowed to
see them. A value never appears in an exception message, a log line or a ``repr``.

Every operation opens its own short-lived SQLite connection, so a ``Vault`` can be shared by
the threads of a web server without any locking of its own.
"""

import json
import os
import re
import sqlite3
from collections.abc import Generator, Iterable, Sequence
from contextlib import closing, contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from domo_common.gatekeeper_api import REF_PATTERN, Binding, InjectionLocation, SecretMetadata
from pydantic import TypeAdapter

from domo_gatekeeper.errors import (
    BindingMismatchError,
    InvalidSecretValueError,
    VaultDecryptError,
)
from domo_gatekeeper.master_key import KEY_SIZE

NONCE_SIZE = 12
_BUSY_TIMEOUT_SECONDS = 30.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS secrets (
    ref TEXT PRIMARY KEY,
    purpose TEXT NOT NULL,
    bindings_json TEXT NOT NULL,
    nonce BLOB NOT NULL,
    ciphertext BLOB NOT NULL,
    created_at TEXT NOT NULL,
    rotated_at TEXT,
    last_used_at TEXT
)
"""
_METADATA_COLUMNS = "ref, purpose, bindings_json, created_at, rotated_at, last_used_at"

_BINDINGS = TypeAdapter(list[Binding])

# ref, purpose, bindings_json, created_at, rotated_at, last_used_at
_MetadataRow = tuple[str, str, str, str, str | None, str | None]
# ref, bindings_json, nonce, ciphertext
_SecretRow = tuple[str, str, bytes, bytes]
_SECRET_COLUMNS = "ref, bindings_json, nonce, ciphertext"


# Format of the associated data, version 1. It is a storage format: changing it makes every
# stored secret undecryptable. It is built from an explicit list of fields (never from a model
# dump), so adding fields or validators to the API models cannot alter it; anything that
# changes what it covers needs a new version prefix (and a migration).
_AAD_PREFIX = b"domo-vault-aad/v1\n"


def _canonical_binding(host: str, port: int | None, kind: str, name: str, template: str) -> str:
    return json.dumps(
        {
            "host": host,
            "port": port,
            "location": {"kind": kind, "name": name, "template": template},
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _build_associated_data(ref: str, canonical_bindings: Iterable[str]) -> bytes:
    """Prefix, ref and the sorted, de-duplicated bindings: order of the bindings is irrelevant."""
    body = ",".join(sorted(set(canonical_bindings)))
    return _AAD_PREFIX + f"{ref}\n[{body}]".encode()


def _associated_data(ref: str, bindings: Iterable[Binding]) -> bytes:
    """The associated data for a secret about to be stored."""
    return _build_associated_data(
        ref,
        (
            _canonical_binding(
                b.host, b.port, b.location.kind, b.location.name, b.location.template
            )
            for b in bindings
        ),
    )


def _stored_associated_data(ref: str, bindings_json: str) -> bytes:
    """The associated data rebuilt from a stored row, without the API models' validators.

    Raises ``ValueError``, ``LookupError``, ``TypeError`` or ``RecursionError`` when the stored
    JSON is malformed.
    """
    entries: Any = json.loads(bindings_json)
    return _build_associated_data(
        ref,
        (
            _canonical_binding(
                entry["host"],
                entry["port"],
                entry["location"]["kind"],
                entry["location"]["name"],
                entry["location"]["template"],
            )
            for entry in entries
        ),
    )


def _stored_bindings(bindings_json: str) -> list[Binding]:
    """The bindings of a stored row, built from the fields the associated data covers only.

    Fields the v1 format does not cover are ignored: they are not authenticated, so they must
    not reach a caller that trusts the result.
    """
    entries: Any = json.loads(bindings_json)
    return list(
        dict.fromkeys(
            Binding(
                host=entry["host"],
                port=entry["port"],
                location=InjectionLocation(
                    kind=entry["location"]["kind"],
                    name=entry["location"]["name"],
                    template=entry["location"]["template"],
                ),
            )
            for entry in entries
        )
    )


# What reading a stored row can raise when it was tampered with or corrupted: JSON and
# validation errors are ValueErrors, and deeply nested JSON is a RecursionError.
_STORED_DATA_ERRORS = (InvalidTag, ValueError, LookupError, TypeError, RecursionError)


def _undecryptable(ref: str) -> VaultDecryptError:
    return VaultDecryptError(
        f"Cannot decrypt the secret {ref!r}: wrong master key or corrupted vault."
    )


def _encode_value(value: str) -> bytes:
    """The UTF-8 bytes of a secret value.

    Text that cannot be encoded (a lone surrogate) is refused without chaining the encoder's
    error: its repr carries the whole value. The raise sits outside the ``except`` block so that
    the error has no ``__context__`` either.
    """
    try:
        return value.encode()
    except UnicodeEncodeError:
        pass
    raise InvalidSecretValueError("A secret value must be valid Unicode text.")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _parse(timestamp: str | None) -> datetime | None:
    return None if timestamp is None else datetime.fromisoformat(timestamp)


def _to_metadata(row: _MetadataRow) -> SecretMetadata:
    ref, purpose, bindings_json, created_at, rotated_at, last_used_at = row
    return SecretMetadata(
        ref=ref,
        purpose=purpose,
        bindings=_BINDINGS.validate_json(bindings_json),
        created_at=datetime.fromisoformat(created_at),
        rotated_at=_parse(rotated_at),
        last_used_at=_parse(last_used_at),
    )


def _create_private_file(path: Path) -> None:
    """Create an empty file readable by its owner only, unless it already exists."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    with suppress(FileExistsError):
        os.close(os.open(path, flags, 0o600))


class Vault:
    """Encrypted secrets keyed by ref, each bound to immutable destinations."""

    def __init__(self, db_path: Path, key: bytes) -> None:
        if len(key) != KEY_SIZE:
            raise ValueError(f"The vault key must be exactly {KEY_SIZE} bytes.")
        self._db_path = db_path
        self._aead = AESGCM(key)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _create_private_file(db_path)
        with self._connection() as conn:
            conn.execute(_SCHEMA)

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

    def _decrypt(self, row: _SecretRow) -> str:
        ref, bindings_json, nonce, ciphertext = row
        try:
            associated_data = _stored_associated_data(ref, bindings_json)
            return self._aead.decrypt(nonce, ciphertext, associated_data).decode()
        except _STORED_DATA_ERRORS:
            raise _undecryptable(ref) from None

    def _decrypt_bound(self, row: _SecretRow) -> tuple[str, list[Binding]]:
        """The plaintext of a stored row with exactly the bindings its ciphertext authenticates."""
        value = self._decrypt(row)
        ref, bindings_json = row[0], row[1]
        try:
            bindings = _stored_bindings(bindings_json)
            # What is returned must be what was authenticated, whatever the models do with it.
            if _associated_data(ref, bindings) != _stored_associated_data(ref, bindings_json):
                raise ValueError("bindings differ from the authenticated ones")
        except _STORED_DATA_ERRORS:
            raise _undecryptable(ref) from None
        return value, bindings

    def put(
        self, ref: str, value: str, *, purpose: str, bindings: Sequence[Binding]
    ) -> SecretMetadata:
        """Store a secret, or rotate its value when it exists with the same bindings.

        A ref that already exists with other bindings (any difference in destinations, in any
        order) is refused: bindings are immutable. A rotation also refreshes the purpose.
        The bindings are part of the encryption's associated data, so they cannot be edited in
        the database file either.
        """
        if re.fullmatch(REF_PATTERN, ref) is None:
            raise ValueError(f"The secret ref must match {REF_PATTERN}.")
        if not value:
            raise InvalidSecretValueError("A secret value must not be empty.")
        plaintext = _encode_value(value)
        unique = list(dict.fromkeys(bindings))
        if not unique:
            raise ValueError("A secret needs at least one binding.")

        nonce = os.urandom(NONCE_SIZE)
        ciphertext = self._aead.encrypt(nonce, plaintext, _associated_data(ref, unique))
        bindings_json = _BINDINGS.dump_json(unique).decode()
        now = _now()
        with self._transaction() as conn:
            row = conn.execute("SELECT bindings_json FROM secrets WHERE ref = ?", (ref,)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO secrets"
                    " (ref, purpose, bindings_json, nonce, ciphertext, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (ref, purpose, bindings_json, nonce, ciphertext, now),
                )
            else:
                stored = cast(str, row[0])
                if set(_BINDINGS.validate_json(stored)) != set(unique):
                    raise BindingMismatchError(
                        f"The secret {ref!r} already exists with other bindings; "
                        "bindings are immutable."
                    )
                # Rewrite the bindings too: the stored JSON must be the one this ciphertext
                # was sealed with, not merely an equivalent spelling of it.
                conn.execute(
                    "UPDATE secrets SET purpose = ?, bindings_json = ?, nonce = ?,"
                    " ciphertext = ?, rotated_at = ? WHERE ref = ?",
                    (purpose, bindings_json, nonce, ciphertext, now, ref),
                )
            stored_row = conn.execute(
                f"SELECT {_METADATA_COLUMNS} FROM secrets WHERE ref = ?", (ref,)
            ).fetchone()
        return _to_metadata(cast(_MetadataRow, stored_row))

    def _secret_row(self, ref: str) -> _SecretRow:
        with self._connection() as conn:
            row = conn.execute(
                f"SELECT {_SECRET_COLUMNS} FROM secrets WHERE ref = ?", (ref,)
            ).fetchone()
        if row is None:
            raise KeyError(ref)
        return cast(_SecretRow, row)

    def reveal(self, ref: str) -> str:
        """Return the plaintext of a secret, without its bindings: for tests and administration
        only. Production code uses ``reveal_bound``, which returns the value together with the
        destinations it may go to, from a single authenticated read."""
        return self._decrypt(self._secret_row(ref))

    def reveal_bound(self, ref: str) -> tuple[str, list[Binding]]:
        """Return the plaintext of a secret with the bindings its ciphertext authenticates.

        Both come from one read of one row, so the destinations that gate the use of the value
        are exactly the ones sealed with it: a concurrent writer cannot swap them in between.
        Only the relay should ever call this.
        """
        return self._decrypt_bound(self._secret_row(ref))

    def metadata(self, ref: str) -> SecretMetadata | None:
        with self._connection() as conn:
            row = conn.execute(
                f"SELECT {_METADATA_COLUMNS} FROM secrets WHERE ref = ?", (ref,)
            ).fetchone()
        return None if row is None else _to_metadata(cast(_MetadataRow, row))

    def list_metadata(self) -> list[SecretMetadata]:
        with self._connection() as conn:
            rows = conn.execute(f"SELECT {_METADATA_COLUMNS} FROM secrets ORDER BY ref").fetchall()
        return [_to_metadata(cast(_MetadataRow, row)) for row in rows]

    def values(self) -> list[str]:
        """Every plaintext in the vault, so responses can be scrubbed of them."""
        with self._connection() as conn:
            rows = conn.execute(f"SELECT {_SECRET_COLUMNS} FROM secrets ORDER BY ref").fetchall()
        return [self._decrypt(cast(_SecretRow, row)) for row in rows]

    def mark_used(self, ref: str) -> None:
        with self._connection() as conn:
            cursor = conn.execute(
                "UPDATE secrets SET last_used_at = ? WHERE ref = ?", (_now(), ref)
            )
            if cursor.rowcount == 0:
                raise KeyError(ref)

    @staticmethod
    def count_secrets(db_path: Path) -> int:
        """Number of stored secrets; 0 when there is no vault file. Needs no key."""
        if not db_path.exists():
            return 0
        uri = f"{db_path.resolve().as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=_BUSY_TIMEOUT_SECONDS)) as conn:
            has_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'secrets'"
            ).fetchone()
            if has_table is None:
                return 0
            return cast(int, conn.execute("SELECT COUNT(*) FROM secrets").fetchone()[0])
