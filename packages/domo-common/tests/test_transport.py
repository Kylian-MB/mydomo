"""Where a key may travel in cleartext: the rule the core and the gatekeeper share."""

import pytest
from domo_common.transport import cleartext_allowed


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "127.0.0.1",
        "127.255.255.254",
        "10.0.0.1",
        "172.16.0.1",
        "172.31.255.255",
        "192.168.1.10",
        "169.254.169.254",
        "::1",
        "fd00::1",
        "fc00::1",
        "fe80::1",
        "::ffff:10.0.0.1",
        "::ffff:127.0.0.1",
        "fake-llm",
        "ollama",
    ],
)
def test_cleartext_is_allowed_to_local_hosts(host):
    assert cleartext_allowed(host)


@pytest.mark.parametrize(
    "host",
    [
        "",
        "api.example.com",
        "localhost.example.com",
        "fake-llm.",
        "a.b",
        "8.8.8.8",
        "11.0.0.1",
        "172.15.255.255",
        "172.32.0.1",
        "192.169.0.1",
        "169.253.0.1",
        "100.64.0.1",
        "0.0.0.0",
        "::",
        "2001:4860:4860::8888",
        "2001:db8::1",
        "fe00::1",
        "::ffff:8.8.8.8",
        "1",
        "134744072",
        "0x7f000001",
        "0X7F000001",
        "127.1",
    ],
)
def test_cleartext_is_refused_to_every_other_host(host):
    assert not cleartext_allowed(host)
