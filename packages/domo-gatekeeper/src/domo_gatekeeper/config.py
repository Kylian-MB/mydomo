"""The gatekeeper's configuration: where its data lives, which ports it serves, the core's token.

Everything is read from ``DOMO_*`` environment variables, with the defaults of the container
layout. The core's token is a credential and is read from a file (a Docker volume written by the
``init`` service), never from the environment, where it would show up in ``docker inspect``.
"""

from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network
from pathlib import Path
from typing import Annotated, Self

from domo_common.core_token import CoreTokenError
from domo_common.core_token import load_core_token as common_load_core_token
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from domo_gatekeeper.errors import ConfigurationError


class GatekeeperSettings(BaseSettings):
    """Settings of the gatekeeper service (variables prefixed ``DOMO_``)."""

    # The core's own variables share the prefix and may share the environment: ignore them.
    model_config = SettingsConfigDict(env_prefix="DOMO_", extra="ignore", frozen=True)

    gatekeeper_data_dir: Path = Path("/data")
    core_token_file: Path = Path("/run/domo/core-auth/core_token")
    gatekeeper_internal_port: int = Field(default=8081, ge=1, le=65535)
    gatekeeper_public_port: int = Field(default=8082, ge=1, le=65535)
    # Inside the container the servers listen on every interface: the networks decide who can
    # reach them (the internal API only from the core's network).
    gatekeeper_host: str = "0.0.0.0"

    # What a program may never reach and where the machine that hosts Domo is (``reach``),
    # comma-separated. ``own_networks`` replaces the networks that would be detected (the
    # gatekeeper's own addresses stay never reachable all the same). ``host_addresses`` adds to
    # the host addresses that are detected, and never drops one: in compose.yaml, the gateways
    # of the networks the file defines, then what the person names in ``.env``
    # (``DOMO_EXTRA_HOST_ADDRESSES``, empty by default: an empty item is skipped). Known limit:
    # from inside a container, the host's address on a network this file does not define
    # (another bridge, its address in the house that is not among ``site_addresses``) cannot be
    # known unless it is named there, and is judged as any other address.
    own_networks: Annotated[tuple[IPv4Network | IPv6Network, ...] | None, NoDecode] = None
    host_addresses: Annotated[tuple[IPv4Address | IPv6Address, ...] | None, NoDecode] = None
    # True when the gatekeeper runs on the machine itself rather than in a container: the
    # machine's own addresses and loopback are then the machine that hosts Domo.
    host_is_local: bool = False
    # The ports of Domo's own services as the machine that hosts it shows them: never
    # reachable at a host address, whoever opens what. In containers, the edge's published
    # ports. The gatekeeper's own two ports (above) are always added. When the gatekeeper runs on
    # the machine itself (``host_is_local``), this is how the ports of its sibling services
    # (core, database, edge) are named: nothing else is guessed without it.
    own_ports: Annotated[tuple[Annotated[int, Field(ge=1, le=65535)], ...], NoDecode] = ()
    # The names and addresses the page is opened with, as the edge and the core have them: the
    # IP literals among them are the machine that hosts Domo (the names are not looked up).
    site_addresses: str = ""

    @field_validator("own_ports", mode="before")
    @classmethod
    def _comma_separated_ports(cls, value: object) -> object:
        """``8443, 8080`` is the list ``[8443, 8080]``; an empty value is no port."""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("own_networks", "host_addresses", mode="before")
    @classmethod
    def _comma_separated(cls, value: object) -> object:
        """``a, b`` is the list ``[a, b]``; an empty value (as a Compose file passes a variable
        that is not set) is "not set", never "nothing"."""
        if isinstance(value, str):
            items = [item.strip() for item in value.split(",") if item.strip()]
            return items or None
        return value

    @model_validator(mode="after")
    def _distinct_ports(self) -> Self:
        if self.gatekeeper_internal_port == self.gatekeeper_public_port:
            raise ValueError("the internal and the public servers need different ports")
        return self

    @property
    def vault_db_path(self) -> Path:
        return self.gatekeeper_data_dir / "vault.db"

    @property
    def master_key_path(self) -> Path:
        return self.gatekeeper_data_dir / "master.key"

    @property
    def requests_db_path(self) -> Path:
        return self.gatekeeper_data_dir / "requests.db"

    @property
    def providers_db_path(self) -> Path:
        return self.gatekeeper_data_dir / "providers.db"


def load_core_token(path: Path) -> str:
    """The token the core authenticates with, read from ``path`` with the rule the core reads it
    with (``domo_common.core_token``).

    There is no fallback: a gatekeeper without a token would either accept anyone or nobody.
    Raises ``ConfigurationError`` (whose message never includes the token) when the file is
    missing, unreadable, empty, or holds something that cannot be a header value.
    """
    try:
        return common_load_core_token(path)
    except CoreTokenError as exc:
        raise ConfigurationError(str(exc)) from None
