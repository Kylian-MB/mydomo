"""Where a declared provider's address leads: one rule for the core and for a program's request
(``reach.zone``), asked through a read-only route and answered on the address the name resolves
to."""

from collections.abc import AsyncIterator
from ipaddress import IPv4Address, ip_address, ip_network

import httpx
import pytest
from domo_common.gatekeeper_api import ProviderRoute, ProviderZone
from domo_gatekeeper.internal_api import create_internal_app
from domo_gatekeeper.providers import ProviderRegistry
from domo_gatekeeper.reach import IPAddress, Reach
from domo_gatekeeper.vault import Vault
from fastapi import FastAPI

CORE_TOKEN = "core-token-for-tests"
BEARER = {"authorization": f"Bearer {CORE_TOKEN}"}
HOST = IPv4Address("172.17.0.1")
REACH = Reach(
    own_networks=(ip_network("10.247.81.0/24"),),
    host_addresses=(HOST,),
    own_ports=(8081, 8082),
)
NAMES: dict[str, list[IPAddress]] = {
    "host.docker.internal": [HOST],
    "nas.lan": [ip_address("192.168.1.20")],
    "speech.example.com": [ip_address("93.184.216.34")],
    "rebinding.example.com": [ip_address("93.184.216.35")],
}


async def lookup(name: str) -> list[IPAddress]:
    if name not in NAMES:
        raise OSError("no such name")
    return NAMES[name]


@pytest.fixture
def app(vault: Vault, store, registry: ProviderRegistry, http_client: httpx.AsyncClient) -> FastAPI:
    return create_internal_app(
        vault=vault,
        requests=store,
        providers=registry,
        http_client=http_client,
        core_token=CORE_TOKEN,
        reach=REACH,
        lookup=lookup,
    )


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://gatekeeper.test", headers=BEARER
    ) as client:
        yield client


async def zone_of(client: httpx.AsyncClient, registry: ProviderRegistry, vault, url: str) -> str:
    registry.register(ProviderRoute(id="p-speech", base_url=url, secret_ref=None), vault)
    answer = await client.get("/internal/providers/p-speech/zone")
    assert answer.status_code == 200
    return ProviderZone.model_validate(answer.json()).zone


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://host.docker.internal:8083/v1", "host"),
        ("http://172.17.0.1:8083/v1", "host"),
        ("http://192.168.1.10:8000/v1", "house"),
        ("http://nas.lan:8000/v1", "house"),
        # A single-label name is not trusted by its form: it is judged where it leads.
        ("http://speech:8083/v1", "never"),
        ("https://speech.example.com/v1", "internet"),
        ("https://93.184.216.34/v1", "internet"),
        # Nowhere: the gatekeeper itself, an address of the stack, a loopback, an unknown name.
        ("http://172.17.0.1:8081/v1", "never"),
        ("http://10.247.81.5:8083/v1", "never"),
        ("http://127.0.0.1:8083/v1", "never"),
        ("http://unknown.example.com/v1", "never"),
    ],
)
async def test_the_zone_is_the_one_a_program_s_request_would_be_judged_by(
    client, registry, vault, url, expected
):
    assert await zone_of(client, registry, vault, url) == expected


async def test_a_name_is_judged_on_the_address_it_resolves_to_now(client, registry, vault):
    url = "http://rebinding.example.com/v1"
    assert await zone_of(client, registry, vault, url) == "internet"
    NAMES["rebinding.example.com"] = [ip_address("192.168.1.99")]
    try:
        assert (await client.get("/internal/providers/p-speech/zone")).json() == {"zone": "house"}
    finally:
        NAMES["rebinding.example.com"] = [ip_address("93.184.216.35")]


async def test_an_unknown_provider_is_a_404_and_the_route_needs_the_token(app, client):
    assert (await client.get("/internal/providers/p-none/zone")).status_code == 404
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gatekeeper.test") as bare:
        assert (await bare.get("/internal/providers/p-none/zone")).status_code == 401


async def test_the_route_changes_nothing(client, registry, vault):
    await zone_of(client, registry, vault, "http://192.168.1.10:8000/v1")
    before = registry.get("p-speech")
    await client.get("/internal/providers/p-speech/zone")
    assert registry.get("p-speech") == before
    assert (await client.put("/internal/providers/p-speech/zone")).status_code == 405


async def test_without_a_reach_the_app_has_no_such_route(vault, store, registry, http_client):
    bare = create_internal_app(
        vault=vault,
        requests=store,
        providers=registry,
        http_client=http_client,
        core_token=CORE_TOKEN,
    )
    transport = httpx.ASGITransport(app=bare)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://gatekeeper.test", headers=BEARER
    ) as client:
        assert (await client.get("/internal/providers/p-speech/zone")).status_code == 404
