"""The registry of model providers the gatekeeper relays to.

A provider route says where a provider lives (``base_url``) and which secret authenticates it
(``secret_ref``). Registering a route that names a secret is only allowed when one of the
secret's bindings permits the route's host: the key can never be pointed at another destination
by registering a provider. A keyed route must also use https, unless its host is local (the
rule is ``domo_common.transport.cleartext_allowed``, shared with the core): bindings name a
host, not a scheme, and a key sent over plain http to a public host would cross the network in
cleartext. The relay checks both again at every call.

The registry keeps its own SQLite file, in the data directory next to the vault. Every
operation opens a short-lived connection, so it can be shared by the threads of a web server.
"""

import os
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

from domo_common.gatekeeper_api import Binding, ProviderRoute
from domo_common.transport import cleartext_allowed

from domo_gatekeeper.errors import BindingMismatchError, InsecureTransportError
from domo_gatekeeper.vault import Vault

_BUSY_TIMEOUT_SECONDS = 30.0
_DEFAULT_PORTS = {"http": 80, "https": 443}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS providers (
    id TEXT PRIMARY KEY,
    base_url TEXT NOT NULL,
    secret_ref TEXT
)
"""
# id, base_url, secret_ref
_Row = tuple[str, str, str | None]


def default_port(scheme: str) -> int:
    """The port a URL of this scheme uses when it names none."""
    return _DEFAULT_PORTS[scheme]


def binding_allows(binding: Binding, host: str, port: int) -> bool:
    """Whether a binding permits sending its secret to ``host``:``port``.

    A binding names a host, and optionally a port: without one, every port of the host is fine.
    """
    return binding.host == host and (binding.port is None or binding.port == port)


def require_secure_transport(ref: str, scheme: str, host: str) -> None:
    """Refuse to send the secret ``ref`` over plain http to a host that is not local."""
    if scheme != "https" and not cleartext_allowed(host):
        raise InsecureTransportError(
            f"The secret {ref!r} may only be sent to {host} over https, not over plain {scheme}."
        )


def _route_destination(route: ProviderRoute) -> tuple[str, int]:
    parts = urlsplit(route.base_url)
    # The model guarantees an http(s) URL with a host and a valid port.
    return cast(str, parts.hostname), parts.port or default_port(parts.scheme)


def _create_private_file(path: Path) -> None:
    """Create an empty file readable by its owner only, unless it already exists."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    with suppress(FileExistsError):
        os.close(os.open(path, flags, 0o600))


class ProviderRegistry:
    """Provider routes by id."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _create_private_file(db_path)
        with self._connection() as conn:
            conn.execute(_SCHEMA)

    @contextmanager
    def _connection(self) -> Generator[sqlite3.Connection]:
        conn = sqlite3.connect(self._db_path, timeout=_BUSY_TIMEOUT_SECONDS, isolation_level=None)
        try:
            yield conn
        finally:
            conn.close()

    def register(self, route: ProviderRoute, vault: Vault) -> None:
        """Store a route, replacing the one with the same id.

        When the route names a secret, one of the secret's bindings must permit the route's host
        (and port, if the binding fixes one): otherwise ``BindingMismatchError``. The route
        must use https unless its host is local: otherwise ``InsecureTransportError`` (a
        ``BindingMismatchError``). An unknown secret raises ``KeyError``.
        """
        if route.secret_ref is not None:
            metadata = vault.metadata(route.secret_ref)
            if metadata is None:
                raise KeyError(route.secret_ref)
            host, port = _route_destination(route)
            if not any(binding_allows(binding, host, port) for binding in metadata.bindings):
                raise BindingMismatchError(
                    f"No binding of the secret {route.secret_ref!r} permits {host}:{port}."
                )
            require_secure_transport(route.secret_ref, urlsplit(route.base_url).scheme, host)
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO providers (id, base_url, secret_ref) VALUES (?, ?, ?)"
                " ON CONFLICT (id) DO UPDATE SET"
                " base_url = excluded.base_url, secret_ref = excluded.secret_ref",
                (route.id, route.base_url, route.secret_ref),
            )

    def get(self, provider_id: str) -> ProviderRoute | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT id, base_url, secret_ref FROM providers WHERE id = ?", (provider_id,)
            ).fetchone()
        if row is None:
            return None
        route_id, base_url, secret_ref = cast(_Row, row)
        return ProviderRoute(id=route_id, base_url=base_url, secret_ref=secret_ref)
