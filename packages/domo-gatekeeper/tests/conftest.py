from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from domo_common.gatekeeper_api import (
    Binding,
    InjectionLocation,
    ProviderRoute,
    SecretRequestCreate,
)
from domo_gatekeeper.providers import ProviderRegistry
from domo_gatekeeper.public_app import create_public_app
from domo_gatekeeper.secret_requests import SecretRequestStore
from domo_gatekeeper.vault import Vault


class FakeClock:
    """A settable clock: the store reads it on every operation."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


@pytest.fixture
def vault_db_path(tmp_path: Path) -> Path:
    return tmp_path / "vault.db"


@pytest.fixture
def vault(vault_db_path: Path) -> Vault:
    return Vault(vault_db_path, b"\x01" * 32)


@pytest.fixture
def requests_db_path(tmp_path: Path) -> Path:
    return tmp_path / "requests.db"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2030, 1, 1, 12, 0, tzinfo=UTC))


@pytest.fixture
def store(requests_db_path: Path) -> SecretRequestStore:
    return SecretRequestStore(requests_db_path)


@pytest.fixture
def store_with_clock(requests_db_path: Path, clock: FakeClock) -> SecretRequestStore:
    return SecretRequestStore(requests_db_path, clock=clock)


@pytest.fixture
def req() -> SecretRequestCreate:
    """The sample secret request used across the gatekeeper tests."""
    return SecretRequestCreate(
        ref="provider:example:api_key",
        purpose="API key for the example model provider",
        bindings=[
            Binding(
                host="api.example.com",
                location=InjectionLocation(kind="header", name="x-api-key"),
            )
        ],
    )


@pytest.fixture
def form_path(store: SecretRequestStore, vault: Vault, req: SecretRequestCreate) -> str:
    """The one-time form path of a pending request created in ``store``."""
    path = store.create(req, vault).form_path
    assert path is not None
    return path


@pytest.fixture
async def public_client(
    store: SecretRequestStore, vault: Vault
) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_public_app(store, vault))
    async with httpx.AsyncClient(transport=transport, base_url="http://gatekeeper.test") as client:
        yield client


@pytest.fixture
def registry_db_path(tmp_path: Path) -> Path:
    return tmp_path / "providers.db"


@pytest.fixture
def registry(registry_db_path: Path) -> ProviderRegistry:
    return ProviderRegistry(registry_db_path)


@pytest.fixture
async def http_client() -> AsyncIterator[httpx.AsyncClient]:
    """The client the relay sends provider calls with."""
    async with httpx.AsyncClient() as client:
        yield client


@pytest.fixture
def route_p1(vault: Vault) -> ProviderRoute:
    """A provider route whose key ("real-key") is stored in the vault, bound to its host."""
    vault.put(
        "provider:p1:api_key",
        "real-key",
        purpose="API key for provider p1",
        bindings=[
            Binding(
                host="api.example.com",
                location=InjectionLocation(kind="header", name="x-api-key"),
            )
        ],
    )
    return ProviderRoute(
        id="p1", base_url="https://api.example.com", secret_ref="provider:p1:api_key"
    )
