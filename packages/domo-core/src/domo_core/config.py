"""The core's configuration: where the database is, how to reach the gatekeeper, the limits.

Everything is read from ``DOMO_*`` environment variables, with the defaults of the container
layout. The database password is a credential and is read from a file (a Docker volume written
by the ``init`` service), never from the environment, where it would show up in
``docker inspect``. The settings object never holds that password: it is read when the URL is
built, and the URL is a plain string that callers hand to the engine and nothing else. A complete
URL given in ``DOMO_DATABASE_URL`` (for tests) is kept as a secret string, masked in reprs.

``DOMO_TRUSTED_PROXIES`` says which addresses may tell the core, through ``X-Forwarded-*``
headers, who the real client is and whether it came over HTTPS: only the edge proxy should. The
web server compares the client's address with each entry and never resolves a name, so entries
must be IP addresses or networks; ``*`` (everyone) is refused.

``DOMO_SITE_ADDRESSES`` and ``DOMO_HTTPS_PORT`` are the edge's own settings, passed to the core
as well: the names and addresses the page is opened with, and its HTTPS port. Passkeys only work
on the host names among them, and the origin they sign carries that port, never one a request
names (``accounts.passkeys``).

``DOMO_UPKEEP_SECONDS`` is the time between two rounds of the core's periodic upkeep
(``upkeep``): a minute, unless a test stack wants it shorter.

``DOMO_TOOLS_DATABASE`` is the name of the tools' own database (``toolsdb``), on the same server
as the core's, reached with the same host, user and password: ``domo_tools`` by default.

``DOMO_EMBEDDING_MODEL_DIR`` is the folder of the local model that gives the meaning of memories
(``memory.embedder``), downloaded into the image when it is built.
"""

import ipaddress
import re
from datetime import timedelta
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL


class ConfigurationError(Exception):
    """The core cannot start with the configuration it was given.

    Messages carry paths and variable names only, never a password.
    """


DEFAULT_TRUSTED_PROXIES = "127.0.0.1"
# The edge's defaults too (compose.yaml): the page answers on https://localhost:8443 only.
DEFAULT_SITE_ADDRESSES = "localhost"
DEFAULT_HTTPS_PORT = 8443


_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)


def _reachable_host(entry: str) -> str | None:
    """The host of one site address (``DOMO_SITE_ADDRESSES``: a scheme, a port or a path around
    it is ignored), as it goes in an origin (an IPv6 address in brackets); ``None`` when it is
    empty or names this machine alone (``localhost``, a loopback address), which nobody else
    reaches."""
    authority = _SCHEME.sub("", entry.strip(), count=1).split("/", 1)[0]
    if authority.startswith("["):
        host = authority[1 : authority.find("]")] if "]" in authority else ""
    elif authority.count(":") == 1:
        host = authority.split(":", 1)[0]
    else:
        host = authority
    name = host.lower().rstrip(".")
    if not name or name == "localhost" or name.endswith(".localhost"):
        return None
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return name
    if address.is_loopback:
        return None
    return f"[{name}]" if address.version == 6 else name


def site_origin(site_addresses: str, https_port: int) -> str | None:
    """The address of the house's page for a link made outside any request (an invitation's link
    the assistant sends from a review, say): ``https://`` the first site address someone else can
    reach, with ``https_port`` unless it is 443. ``None`` when there is none: a link without an
    address is never sent."""
    for entry in re.split(r"[,\s]+", site_addresses):
        host = _reachable_host(entry)
        if host is not None:
            return f"https://{host}" if https_port == 443 else f"https://{host}:{https_port}"
    return None


def check_trusted_proxies(value: str) -> str:
    """``value`` (comma-separated IP addresses and CIDR networks) with its entries trimmed.

    Raises ``ValueError`` for ``*`` or a network of prefix length 0 (``0.0.0.0/0``, ``::/0``: every
    address all the same), an empty entry, a host name (uvicorn would keep it as a literal that
    matches no client address) or a network with host bits set (same fate).
    """
    entries = [entry.strip() for entry in value.split(",")]
    for entry in entries:
        _check_proxy(entry)
    return ",".join(entries)


def _every_address(entry: str) -> ValueError:
    return ValueError(
        f"{entry!r} would let every address set the X-Forwarded-* headers: list the edge "
        "proxy's address (or network) instead"
    )


def _not_an_address(entry: str) -> ValueError:
    return ValueError(f"{entry!r} is not an IP address or a network (host names are not resolved)")


def _check_proxy(entry: str) -> None:
    if entry == "*":
        raise _every_address(entry)
    if not entry:
        raise ValueError("an empty entry (check the commas)")
    if "/" not in entry:
        try:
            ipaddress.ip_address(entry)
        except ValueError:
            raise _not_an_address(entry) from None
        return
    try:
        meant = ipaddress.ip_network(entry, strict=False)
    except ValueError:
        raise _not_an_address(entry) from None
    if meant.prefixlen == 0:
        raise _every_address(entry)
    try:
        ipaddress.ip_network(entry)
    except ValueError:
        raise ValueError(f"{entry!r} has host bits set: write the network, {meant}") from None


class CoreSettings(BaseSettings):
    """Settings of the core service (variables prefixed ``DOMO_``)."""

    # The gatekeeper's own variables share the prefix and may share the environment: ignore them.
    model_config = SettingsConfigDict(
        env_prefix="DOMO_",
        extra="ignore",
        frozen=True,
        validate_by_name=True,
        validate_by_alias=True,
    )

    db_host: str = "db"
    db_name: str = "domo"
    db_user: str = "domo"
    db_password_file: Path = Path("/run/domo/db-auth/db_password")
    # A complete URL that replaces the four settings above (and the password file): for tests,
    # and for running the core against a database that is not the stack's. A secret, since the
    # URL carries the password: it stays out of reprs.
    database_url_override: SecretStr | None = Field(
        default=None, validation_alias="DOMO_DATABASE_URL"
    )
    # The tools' own database (``toolsdb``), on the same server as the core's and reached the same
    # way: only its name differs. Lower-case letters, digits and ``_``.
    tools_database: str = Field(default="domo_tools", pattern=r"^[a-z_][a-z0-9_]{0,62}$")

    gatekeeper_url: str = "http://gatekeeper:8081"
    core_token_file: Path = Path("/run/domo/core-auth/core_token")
    # The Unix socket of the sandbox launcher (``sandbox_client``). It may be absent: the core
    # starts without it, and only a call to the sandbox fails.
    sandbox_socket: Path = Path("/run/domo/sandbox/launcher.sock")

    max_history_chars: int = Field(default=48_000, gt=0)
    max_output_tokens: int = Field(default=2048, gt=0)
    session_ttl_days: int = Field(default=30, gt=0)
    # Seconds between two rounds of the periodic upkeep (``upkeep.run_upkeep``): a chain paused
    # until someone's arrival resumes at most that long after it (spec 4.3b). A minute; the
    # end-to-end tests make it shorter.
    upkeep_seconds: float = Field(default=60.0, gt=0)
    # Where the local model that gives the meaning of memories is (``memory.embedder``): put
    # there when the image is built, never downloaded by the running core.
    embedding_model_dir: Path = Path("/opt/domo/models")

    # Comma-separated addresses or networks (``172.20.0.0/16``) whose ``X-Forwarded-*`` headers the
    # web server believes. The default is the local machine, never "everyone": the compose file
    # sets the edge proxy's.
    trusted_proxies: str = DEFAULT_TRUSTED_PROXIES

    # The names and addresses the page is opened with, comma-separated, as the edge has them
    # (``localhost, 192.168.1.10``). Passkeys work on the host names among them only.
    site_addresses: str = DEFAULT_SITE_ADDRESSES
    # The port of the page (the edge publishes it, the same inside and outside): browsers put it
    # in the origin they sign for a passkey.
    https_port: int = Field(default=DEFAULT_HTTPS_PORT, ge=1, le=65535)

    @field_validator("site_addresses", mode="before")
    @classmethod
    def _blank_site_addresses_are_the_default(cls, value: object) -> object:
        """An empty ``DOMO_SITE_ADDRESSES`` means the edge's default, ``localhost``."""
        if isinstance(value, str):
            return value.strip() or DEFAULT_SITE_ADDRESSES
        return value

    @field_validator("https_port", mode="before")
    @classmethod
    def _blank_https_port_is_the_default(cls, value: object) -> object:
        """An empty ``DOMO_HTTPS_PORT`` means the edge's default, 8443."""
        if isinstance(value, str) and not value.strip():
            return DEFAULT_HTTPS_PORT
        return value

    @field_validator("trusted_proxies", mode="before")
    @classmethod
    def _blank_proxies_are_the_default(cls, value: object) -> object:
        """An empty ``DOMO_TRUSTED_PROXIES`` (as a Compose file passes it when the variable is
        not set) means the default: the local machine, not an empty list and not everyone."""
        if isinstance(value, str):
            return value.strip() or DEFAULT_TRUSTED_PROXIES
        return value

    @field_validator("trusted_proxies")
    @classmethod
    def _proxies_are_addresses_or_networks(cls, value: str) -> str:
        return check_trusted_proxies(value)

    @field_validator("database_url_override", mode="before")
    @classmethod
    def _blank_url_is_unset(cls, value: object) -> object:
        """``DOMO_DATABASE_URL=`` (empty, as a Compose file passes it when the variable is not
        set) means "not set": the stack's database is used, not an empty URL."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def site_origin(self) -> str | None:
        """The address of the house's page outside any request (``site_origin``)."""
        return site_origin(self.site_addresses, self.https_port)

    @property
    def session_ttl(self) -> timedelta:
        """How long a web session lasts (and so the lifetime of the session cookie)."""
        return timedelta(days=self.session_ttl_days)

    @property
    def database_url(self) -> str:
        """The SQLAlchemy URL of the database (asyncpg driver), password included.

        ``DOMO_DATABASE_URL`` wins when set. Otherwise the URL is built from the host, name
        and user, and the password is read from ``db_password_file`` at each call (the password
        is encoded for the URL). Raises ``ConfigurationError`` when the file is missing,
        unreadable or empty: there is no fallback to an empty password.
        """
        if self.database_url_override is not None:
            return self.database_url_override.get_secret_value()
        url = URL.create(
            "postgresql+asyncpg",
            username=self.db_user,
            password=self._read_password(),
            host=self.db_host,
            database=self.db_name,
        )
        return url.render_as_string(hide_password=False)

    def _read_password(self) -> str:
        path = self.db_password_file
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise ConfigurationError(
                f"The database password file is missing at {path}. The init service creates "
                "it: check that it ran, or set DOMO_DB_PASSWORD_FILE (or DOMO_DATABASE_URL)."
            ) from None
        except (OSError, UnicodeDecodeError) as exc:
            reason = exc.strerror if isinstance(exc, OSError) else "not valid UTF-8"
            raise ConfigurationError(
                f"The database password file at {path} cannot be read: {reason or 'I/O error'}."
            ) from None
        # Only the line ending an editor or `echo` adds: spaces may be part of the password.
        password = raw.rstrip("\r\n")
        if not password:
            raise ConfigurationError(f"The database password file at {path} is empty.")
        return password
