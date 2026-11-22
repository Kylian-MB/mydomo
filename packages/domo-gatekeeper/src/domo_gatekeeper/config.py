"""The gatekeeper's configuration: where its data lives, which ports it serves, the core's token.

Everything is read from ``DOMO_*`` environment variables, with the defaults of the container
layout. The core's token is a credential and is read from a file (a Docker volume written by the
``init`` service), never from the environment, where it would show up in ``docker inspect``.
"""

from pathlib import Path
from typing import Self

from domo_common.core_token import CoreTokenError
from domo_common.core_token import load_core_token as common_load_core_token
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

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
