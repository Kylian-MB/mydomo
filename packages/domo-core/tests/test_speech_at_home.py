"""Where a key is: the gatekeeper's word, asked once for a while, failing closed."""

import httpx
import pytest
from domo_core.gatekeeper_client import GatekeeperError
from domo_core.llm.models import Provider
from domo_core.llm.speech.at_home import KEEP_FOR_S, AtHome
from domo_core.llm.speech.home import HOME_SPEECH_ID

URL = "http://gatekeeper.test/internal/providers/p-aaaaaaaa/zone"


def key(*, kind: str = "openai_compatible", url: str = "http://speech:8083/v1") -> Provider:
    return Provider(id="p-aaaaaaaa", kind=kind, base_url=url, secret_ref=None)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.parametrize(
    ("zone", "expected"),
    [("host", True), ("house", True), ("internet", False), ("never", False)],
)
async def test_only_the_house_and_the_host_are_home(respx_mock, gk_client, zone, expected):
    respx_mock.get(URL).respond(200, json={"zone": zone})
    assert await AtHome(gk_client)(key()) is expected


async def test_a_gatekeeper_that_fails_fails_closed_and_is_asked_again(respx_mock, gk_client):
    route = respx_mock.get(URL)
    route.side_effect = [
        httpx.Response(500, json={"error": "internal_error"}),
        httpx.ConnectError("down"),
        httpx.Response(404, json={"error": "unknown_provider"}),
        httpx.Response(200, json={"zone": "house"}),
    ]
    at_home = AtHome(gk_client)
    assert [await at_home(key()) for _ in range(3)] == [False, False, False]
    assert await at_home(key()) is True  # a failure is not kept
    assert route.call_count == 4


async def test_the_answer_is_kept_briefly_for_a_key_and_its_address(respx_mock, gk_client):
    route = respx_mock.get(URL)
    route.side_effect = [
        httpx.Response(200, json={"zone": "house"}),
        httpx.Response(200, json={"zone": "internet"}),
        httpx.Response(200, json={"zone": "host"}),
    ]
    clock = Clock()
    at_home = AtHome(gk_client, clock=clock)
    assert await at_home(key()) is True
    assert await at_home(key()) is True
    assert route.call_count == 1
    # Another address is another key.
    assert await at_home(key(url="https://speech.example.com/v1")) is False
    assert route.call_count == 2
    clock.now += KEEP_FOR_S + 1
    assert await at_home(key()) is True
    assert route.call_count == 3


async def test_only_a_server_that_imitates_openai_is_asked_about(respx_mock, gk_client):
    route = respx_mock.get(URL).respond(200, json={"zone": "house"})
    assert await AtHome(gk_client)(key(kind="anthropic")) is False
    assert not route.called


async def test_the_core_never_reads_the_address_itself(respx_mock, gk_client):
    # A single-label name, a private literal and a name that looks local mean nothing here:
    # what the gatekeeper says is the only word.
    respx_mock.get(URL).respond(200, json={"zone": "internet"})
    for url in ("http://speech:8083/v1", "http://192.168.1.10/v1", "http://nas.local/v1"):
        assert await AtHome(gk_client)(key(url=url)) is False


async def test_the_client_asks_the_gatekeeper_for_the_zone(respx_mock, gk_client):
    route = respx_mock.get(URL).respond(200, json={"zone": "host"})
    assert await gk_client.provider_zone("p-aaaaaaaa") == "host"
    assert route.calls.last.request.headers["authorization"] == "Bearer core-token"
    respx_mock.get(URL).respond(200, json={"zone": "elsewhere"})
    with pytest.raises(GatekeeperError):
        await gk_client.provider_zone("p-aaaaaaaa")


# The service the owner declared (``DOMO_SPEECH_URL``) is at home by that declaration: it is a
# compose service on a network the gatekeeper's zones call its own, so its zone would be "never".
DECLARED = "http://speech:8083/v1"
DECLARED_ZONE_URL = f"http://gatekeeper.test/internal/providers/{HOME_SPEECH_ID}/zone"


def declared_key(*, url: str = DECLARED, provider_id: str = HOME_SPEECH_ID) -> Provider:
    return Provider(id=provider_id, kind="openai_compatible", base_url=url, secret_ref=None)


async def test_the_declared_service_is_at_home_even_when_the_zone_says_never(respx_mock, gk_client):
    route = respx_mock.get(DECLARED_ZONE_URL).respond(200, json={"zone": "never"})
    assert await AtHome(gk_client, declared_url=DECLARED)(declared_key()) is True
    assert not route.called  # the declaration is not asked of the zone


async def test_another_provider_at_the_same_address_is_not_at_home(respx_mock, gk_client):
    respx_mock.get(URL).respond(200, json={"zone": "never"})
    other = declared_key(provider_id="p-aaaaaaaa")
    assert await AtHome(gk_client, declared_url=DECLARED)(other) is False


async def test_the_declaration_is_lost_when_the_address_moves_away_from_it(respx_mock, gk_client):
    respx_mock.get(DECLARED_ZONE_URL).respond(200, json={"zone": "internet"})
    moved = declared_key(url="https://speech.example.com/v1")
    assert await AtHome(gk_client, declared_url=DECLARED)(moved) is False


async def test_without_a_declared_address_the_provider_id_means_nothing(respx_mock, gk_client):
    respx_mock.get(DECLARED_ZONE_URL).respond(200, json={"zone": "never"})
    assert await AtHome(gk_client)(declared_key()) is False


@pytest.mark.parametrize(
    "written", ["http://speech:8083/v1/", "HTTP://Speech:8083/v1", "http://SPEECH:8083/v1//"]
)
async def test_the_declaration_is_read_on_the_normalised_address(respx_mock, gk_client, written):
    respx_mock.get(DECLARED_ZONE_URL).respond(200, json={"zone": "never"})
    assert await AtHome(gk_client, declared_url=DECLARED)(declared_key(url=written)) is True
    assert await AtHome(gk_client, declared_url=written)(declared_key()) is True


async def test_a_different_path_or_port_is_not_the_declared_address(respx_mock, gk_client):
    respx_mock.get(DECLARED_ZONE_URL).respond(200, json={"zone": "never"})
    for other in ("http://speech:8084/v1", "http://speech:8083/v2", "https://speech:8083/v1"):
        assert await AtHome(gk_client, declared_url=DECLARED)(declared_key(url=other)) is False
