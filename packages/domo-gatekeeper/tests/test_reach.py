import logging
import os
from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network
from pathlib import Path

import pytest
from domo_gatekeeper.config import GatekeeperSettings
from domo_gatekeeper.reach import (
    Reach,
    Unresolvable,
    describe,
    lookup_addresses,
    reach_from,
    resolve,
    zone,
)
from pydantic import ValidationError

NOTHING = Reach(own_networks=(), host_addresses=())
# What is known with nothing detected nor configured: the gatekeeper's own two ports.
NOTHING_FOUND = Reach(own_networks=(), host_addresses=(), own_ports=(8081, 8082))


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.upper().startswith("DOMO_"):
            monkeypatch.delenv(name)


# --- The zones ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        # The private ranges are the house.
        ("10.1.2.3", "house"),
        ("172.16.0.1", "house"),
        ("172.31.255.254", "house"),
        ("192.168.1.10", "house"),
        ("100.64.0.1", "house"),
        ("100.127.255.254", "house"),
        ("fc00::1", "house"),
        ("fd12:3456::1", "house"),
        # Just outside them, the internet.
        ("172.32.0.1", "internet"),
        ("100.128.0.1", "internet"),
        ("8.8.8.8", "internet"),
        ("2606:4700::1111", "internet"),
        # Never: unspecified, "this network", multicast, link-local, loopback, metadata.
        ("0.0.0.0", "never"),
        ("0.1.2.3", "never"),
        ("::", "never"),
        ("224.0.0.1", "never"),
        ("239.255.255.250", "never"),
        ("ff02::1", "never"),
        ("169.254.169.254", "never"),
        ("169.254.1.1", "never"),
        ("fe80::1", "never"),
        ("127.0.0.1", "never"),
        ("127.8.8.8", "never"),
        ("::1", "never"),
        ("100.100.100.200", "never"),
        ("fd00:ec2::254", "never"),
        # Reserved, and the IPv6 forms that carry an IPv4 address inside (IPv4-compatible,
        # NAT64, 6to4): no destination of a program (Minor 7 of the final review).
        ("240.0.0.1", "never"),
        ("255.255.255.255", "never"),
        ("::7f00:1", "never"),
        ("64:ff9b::7f00:1", "never"),
        ("64:ff9b:1::a00:1", "never"),
        ("2002:7f00:1::1", "never"),
    ],
)
def test_each_range_lands_in_its_zone(address, expected):
    assert zone(ip_address(address), NOTHING) == expected


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("::ffff:127.0.0.1", "never"),
        ("::ffff:169.254.169.254", "never"),
        ("::ffff:192.168.1.10", "house"),
        ("::ffff:8.8.8.8", "internet"),
    ],
)
def test_an_ipv4_mapped_address_is_judged_as_its_ipv4(address, expected):
    assert zone(ip_address(address), NOTHING) == expected


def test_an_address_of_my_own_networks_is_never_reachable_even_in_a_private_range():
    reach = Reach(own_networks=(ip_network("10.247.81.0/24"),), host_addresses=())
    assert zone(ip_address("10.247.81.3"), reach) == "never"
    assert zone(ip_address("::ffff:10.247.81.3"), reach) == "never"
    assert zone(ip_address("10.247.82.3"), reach) == "house"


def test_a_host_address_is_host_not_house():
    reach = Reach(own_networks=(), host_addresses=(ip_address("172.18.0.1"),))
    assert zone(ip_address("172.18.0.1"), reach) == "host"
    assert zone(ip_address("::ffff:172.18.0.1"), reach) == "host"
    assert zone(ip_address("172.18.0.9"), reach) == "house"


class CountedAddresses(tuple[IPv4Address | IPv6Address, ...]):
    """Host addresses that count how often they are gone through."""

    passes = 0

    def __iter__(self):
        type(self).passes += 1
        return super().__iter__()


def test_the_host_addresses_are_gathered_once_not_at_every_judgement():
    hosts = CountedAddresses((ip_address("172.18.0.1"), ip_address("::ffff:172.18.0.9")))
    reach = Reach(own_networks=(), host_addresses=hosts)
    before = CountedAddresses.passes
    for _ in range(5):
        assert zone(ip_address("172.18.0.9"), reach) == "host"
        assert zone(ip_address("172.18.0.5"), reach) == "house"
    assert CountedAddresses.passes == before


def test_loopback_is_host_only_when_it_is_a_host_address():
    reach = Reach(own_networks=(), host_addresses=(ip_address("127.0.0.1"),))
    assert zone(ip_address("127.0.0.1"), reach) == "host"
    assert zone(ip_address("127.0.0.2"), reach) == "never"


def test_my_own_networks_win_over_a_host_address():
    reach = Reach(
        own_networks=(ip_network("10.247.81.0/24"),), host_addresses=(ip_address("10.247.81.1"),)
    )
    assert zone(ip_address("10.247.81.1"), reach) == "never"


def test_link_local_and_metadata_stay_never_even_as_host_addresses():
    reach = Reach(
        own_networks=(),
        host_addresses=(ip_address("169.254.169.254"), ip_address("fe80::1")),
    )
    assert zone(ip_address("169.254.169.254"), reach) == "never"
    assert zone(ip_address("fe80::1"), reach) == "never"


# --- Domo's own ports on the machine that hosts him ----------------------------------------

HOSTED = Reach(
    own_networks=(),
    host_addresses=(ip_address("172.18.0.1"), ip_address("127.0.0.1")),
    own_ports=(8443, 8080),
)


def test_a_host_address_at_one_of_my_own_ports_is_never_reachable():
    assert zone(ip_address("172.18.0.1"), HOSTED, 8443) == "never"
    assert zone(ip_address("::ffff:172.18.0.1"), HOSTED, 8080) == "never"
    assert zone(ip_address("127.0.0.1"), HOSTED, 8443) == "never"


def test_a_host_address_at_another_port_is_the_host():
    assert zone(ip_address("172.18.0.1"), HOSTED, 8123) == "host"


def test_a_house_address_at_the_same_port_number_stays_the_house():
    assert zone(ip_address("192.168.1.30"), HOSTED, 8443) == "house"
    assert zone(ip_address("8.8.8.8"), HOSTED, 8443) == "internet"


@pytest.mark.parametrize(
    "address", ["172.18.0.1", "127.0.0.1", "127.0.0.2", "192.168.1.30", "10.247.81.3", "8.8.8.8"]
)
def test_without_a_port_the_answer_is_the_address_alone(address):
    without_ports = Reach(own_networks=HOSTED.own_networks, host_addresses=HOSTED.host_addresses)
    assert zone(ip_address(address), HOSTED) == zone(ip_address(address), without_ports)
    assert zone(ip_address(address), HOSTED, None) == zone(ip_address(address), without_ports)


def test_own_ports_come_from_the_configuration(monkeypatch, tmp_path):
    monkeypatch.setenv("DOMO_OWN_PORTS", "8443, 8080")
    reach = reach_from(GatekeeperSettings(), proc_net=write_container(tmp_path), lookup=no_lookup)
    assert reach.own_ports == (8443, 8080, 8081, 8082)
    assert zone(ip_address("172.18.0.1"), reach, 8443) == "never"


def test_the_gatekeeper_own_ports_are_always_closed_and_nothing_else_is_guessed(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("DOMO_HOST_IS_LOCAL", "1")
    monkeypatch.setenv("DOMO_GATEKEEPER_INTERNAL_PORT", "9101")
    monkeypatch.setenv("DOMO_GATEKEEPER_PUBLIC_PORT", "9102")
    reach = reach_from(GatekeeperSettings(), proc_net=write_container(tmp_path), lookup=no_lookup)
    assert reach.own_ports == (9101, 9102)
    # Its loopback, at its own ports, is never reachable; another port of the machine is.
    assert zone(ip_address("127.0.0.1"), reach, 9101) == "never"
    assert zone(ip_address("127.0.0.1"), reach, 9102) == "never"
    assert zone(ip_address("::1"), reach, 9101) == "never"
    assert zone(ip_address("127.0.0.1"), reach, 8123) == "host"


@pytest.mark.parametrize("value", ["0", "65536", "https"])
def test_a_wrong_own_port_stops_the_start(monkeypatch, value):
    monkeypatch.setenv("DOMO_OWN_PORTS", value)
    with pytest.raises(ValidationError):
        GatekeeperSettings()


# --- The addresses the page is opened with ----------------------------------------------------


def test_an_ip_literal_of_the_site_addresses_is_the_host(monkeypatch, tmp_path):
    monkeypatch.setenv("DOMO_SITE_ADDRESSES", "localhost, 192.168.1.10, [fd00::10], domo.lan")
    asked: list[str] = []

    def lookup(name: str) -> list[IPv4Address | IPv6Address]:
        asked.append(name)
        return []

    reach = reach_from(GatekeeperSettings(), proc_net=write_container(tmp_path), lookup=lookup)
    assert zone(ip_address("192.168.1.10"), reach) == "host"
    assert zone(ip_address("fd00::10"), reach) == "host"
    # The names add nothing and are not looked up.
    assert asked == ["host.docker.internal"]
    assert set(reach.host_addresses) == {
        ip_address("172.18.0.1"),
        ip_address("192.168.1.10"),
        ip_address("fd00::10"),
    }


def test_the_site_addresses_count_even_when_the_host_addresses_are_configured(monkeypatch):
    monkeypatch.setenv("DOMO_HOST_ADDRESSES", "172.18.0.1")
    monkeypatch.setenv("DOMO_SITE_ADDRESSES", "192.168.1.10")
    reach = reach_from(GatekeeperSettings(), proc_net=Path("no-proc"), lookup=no_lookup)
    assert reach.host_addresses == (ip_address("172.18.0.1"), ip_address("192.168.1.10"))


def test_the_gateway_of_the_edge_network_at_the_edge_port_is_never_reachable(monkeypatch, tmp_path):
    # What compose.yaml gives the gatekeeper: the gateways of the networks it defines, and the
    # edge's ports. The edge publishes on every address of the host, these gateways included.
    monkeypatch.setenv("DOMO_HOST_ADDRESSES", "10.247.80.1,10.247.81.1")
    monkeypatch.setenv("DOMO_OWN_PORTS", "8443,8080")
    reach = reach_from(GatekeeperSettings(), proc_net=write_container(tmp_path), lookup=no_lookup)
    assert zone(ip_address("10.247.80.1"), reach, 8443) == "never"
    assert zone(ip_address("10.247.80.1"), reach, 8080) == "never"
    assert zone(ip_address("10.247.80.1"), reach, 8123) == "host"
    assert zone(ip_address("10.247.80.1"), reach) == "host"


# --- What this machine says: a container on Domo's core network and on the way out -----------
# eth0: the core's network (10.247.81.0/24), no route out. eth1: the way out (172.18.0.0/16),
# default route through 172.18.0.1, the machine that hosts the container.


def _le(address: str) -> str:
    """An IPv4 address as /proc/net/route writes it: little-endian hexadecimal."""
    return f"{int.from_bytes(IPv4Address(address).packed, 'little'):08X}"


def _route(iface: str, destination: str, gateway: str, mask: str, flags: str) -> str:
    return f"{iface}\t{_le(destination)}\t{_le(gateway)}\t{flags}\t0\t0\t0\t{_le(mask)}\t0\t0\t0\n"


def _v6(address: str) -> str:
    return IPv6Address(address).exploded.replace(":", "")


def write_container(proc: Path, *, with_ipv6: bool = False) -> Path:
    proc.mkdir(parents=True, exist_ok=True)
    (proc / "route").write_text(
        "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
        + _route("eth1", "0.0.0.0", "172.18.0.1", "0.0.0.0", "0003")
        + _route("eth0", "10.247.81.0", "0.0.0.0", "255.255.255.0", "0001")
        + _route("eth1", "172.18.0.0", "0.0.0.0", "255.255.0.0", "0001"),
        encoding="ascii",
    )
    (proc / "fib_trie").write_text(
        "Main:\n"
        "  +-- 0.0.0.0/0 3 0 5\n"
        "     |-- 0.0.0.0\n"
        "        /0 universe UNICAST\n"
        "     +-- 10.247.81.0/24 2 0 2\n"
        "        |-- 10.247.81.0\n"
        "           /24 link UNICAST\n"
        "        |-- 10.247.81.130\n"
        "           /32 host LOCAL\n"
        "     |-- 127.0.0.1\n"
        "        /32 host LOCAL\n"
        "     |-- 172.18.0.2\n"
        "        /32 host LOCAL\n"
        "Local:\n"
        "     |-- 172.18.0.2\n"
        "        /32 host LOCAL\n",
        encoding="ascii",
    )
    if with_ipv6:
        (proc / "if_inet6").write_text(
            f"{_v6('::1')} 01 80 10 80       lo\n"
            f"{_v6('fd00:81::5')} 02 40 00 80     eth0\n"
            f"{_v6('fe80::42:aff:fe00:5')} 02 40 20 80     eth0\n"
            f"{_v6('fd00:18::2')} 03 40 00 80     eth1\n",
            encoding="ascii",
        )
        zero = "0" * 32
        (proc / "ipv6_route").write_text(
            f"{zero} 00 {zero} 00 {_v6('fd00:18::1')} 00000400 00000001 00000000 00000003"
            "     eth1\n"
            f"{_v6('fd00:81::')} 40 {zero} 00 {zero} 00000100 00000001 00000000 00000001"
            "     eth0\n",
            encoding="ascii",
        )
    return proc


def no_lookup(name: str) -> list[IPv4Address | IPv6Address]:
    return []


def docker_desktop_lookup(name: str) -> list[IPv4Address | IPv6Address]:
    return [ip_address("192.168.65.254")] if name == "host.docker.internal" else []


def test_detected_own_networks_are_those_of_my_interfaces_but_the_way_out(tmp_path):
    reach = reach_from(GatekeeperSettings(), proc_net=write_container(tmp_path), lookup=no_lookup)
    assert ip_network("10.247.81.0/24") in reach.own_networks
    assert not any(ip_address("172.18.0.9") in network for network in reach.own_networks)
    # The gatekeeper's own interface network is never reachable; the way out is the house.
    assert zone(ip_address("10.247.81.3"), reach) == "never"
    assert zone(ip_address("172.18.0.9"), reach) == "house"


def test_my_own_address_on_the_way_out_is_never_reachable(tmp_path):
    reach = reach_from(GatekeeperSettings(), proc_net=write_container(tmp_path), lookup=no_lookup)
    assert zone(ip_address("172.18.0.2"), reach) == "never"


def test_the_default_gateway_is_the_machine_that_hosts_me(tmp_path):
    reach = reach_from(GatekeeperSettings(), proc_net=write_container(tmp_path), lookup=no_lookup)
    assert reach.host_addresses == (ip_address("172.18.0.1"),)
    assert zone(ip_address("172.18.0.1"), reach) == "host"


def test_what_host_docker_internal_resolves_to_is_the_machine_that_hosts_me(tmp_path):
    reach = reach_from(
        GatekeeperSettings(), proc_net=write_container(tmp_path), lookup=docker_desktop_lookup
    )
    assert set(reach.host_addresses) == {ip_address("172.18.0.1"), ip_address("192.168.65.254")}
    assert zone(ip_address("192.168.65.254"), reach) == "host"


def test_ipv6_interfaces_are_read_too(tmp_path):
    proc = write_container(tmp_path, with_ipv6=True)
    reach = reach_from(GatekeeperSettings(), proc_net=proc, lookup=no_lookup)
    assert zone(ip_address("fd00:81::9"), reach) == "never"  # the core's network
    assert zone(ip_address("fd00:18::2"), reach) == "never"  # my own address on the way out
    assert zone(ip_address("fd00:18::9"), reach) == "house"  # the way out
    assert zone(ip_address("fd00:18::1"), reach) == "host"  # its gateway


def test_without_routes_nothing_is_detected_and_it_says_so(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="domo_gatekeeper.reach")
    reach = reach_from(GatekeeperSettings(), proc_net=tmp_path / "none", lookup=no_lookup)
    assert reach == NOTHING_FOUND
    assert "DOMO_OWN_NETWORKS" in caplog.text and "DOMO_HOST_ADDRESSES" in caplog.text


def test_without_routes_host_docker_internal_is_still_used(tmp_path):
    reach = reach_from(
        GatekeeperSettings(), proc_net=tmp_path / "none", lookup=docker_desktop_lookup
    )
    assert reach.host_addresses == (ip_address("192.168.65.254"),)


def test_an_ipv6_line_with_a_wrong_prefix_is_skipped(tmp_path):
    proc = write_container(tmp_path, with_ipv6=True)
    lines = (proc / "if_inet6").read_text("ascii")
    (proc / "if_inet6").write_text(lines + f"{_v6('fd00:77::2')} 04 ff 00 80     eth2\n", "ascii")
    reach = reach_from(GatekeeperSettings(), proc_net=proc, lookup=no_lookup)
    assert zone(ip_address("fd00:81::9"), reach) == "never"  # the rest is still read
    assert zone(ip_address("fd00:77::2"), reach) == "house"


def test_routes_that_cannot_be_parsed_are_skipped(tmp_path):
    (tmp_path / "route").write_text("Iface\tDestination\ngarbage line\neth0\tZZ\n", "ascii")
    (tmp_path / "fib_trie").write_text("|-- not-an-address\n   /32 host LOCAL\n", "ascii")
    assert reach_from(GatekeeperSettings(), proc_net=tmp_path, lookup=no_lookup) == NOTHING_FOUND


# --- When the gatekeeper runs on the machine itself ---------------------------------------------


def test_outside_a_container_loopback_and_my_addresses_are_the_host(monkeypatch, tmp_path):
    monkeypatch.setenv("DOMO_HOST_IS_LOCAL", "1")
    reach = reach_from(GatekeeperSettings(), proc_net=write_container(tmp_path), lookup=no_lookup)
    assert zone(ip_address("127.0.0.1"), reach) == "host"
    assert zone(ip_address("::1"), reach) == "host"
    assert zone(ip_address("::ffff:127.0.0.1"), reach) == "host"
    assert zone(ip_address("172.18.0.2"), reach) == "host"


def test_outside_a_container_my_name_gives_my_addresses_where_routes_cannot_be_read(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("DOMO_HOST_IS_LOCAL", "1")

    def lookup(name: str) -> list[IPv4Address | IPv6Address]:
        return [] if name == "host.docker.internal" else [ip_address("192.168.1.20")]

    reach = reach_from(GatekeeperSettings(), proc_net=tmp_path / "none", lookup=lookup)
    assert zone(ip_address("192.168.1.20"), reach) == "host"
    assert zone(ip_address("127.0.0.1"), reach) == "host"


# --- Configuration wins -------------------------------------------------------------------------


def test_configured_networks_replace_and_host_addresses_add_to_what_is_detected(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("DOMO_OWN_NETWORKS", "10.99.7.0/24, fd00:99::/64")
    monkeypatch.setenv("DOMO_HOST_ADDRESSES", "192.168.1.20,fd00:1::20")
    reach = reach_from(
        GatekeeperSettings(), proc_net=write_container(tmp_path), lookup=docker_desktop_lookup
    )
    assert ip_network("10.99.7.0/24") in reach.own_networks
    assert ip_network("fd00:99::/64") in reach.own_networks
    assert ip_network("10.247.81.0/24") not in reach.own_networks
    # A setting never drops a detected host address: the gateway and host.docker.internal stay.
    assert reach.host_addresses == (
        ip_address("172.18.0.1"),
        ip_address("192.168.65.254"),
        ip_address("192.168.1.20"),
        ip_address("fd00:1::20"),
    )
    assert zone(ip_address("172.18.0.1"), reach) == "host"
    # The gatekeeper's own addresses are never reachable, whatever the configuration says.
    assert zone(ip_address("172.18.0.2"), reach) == "never"


def test_configuration_works_where_nothing_can_be_detected(monkeypatch, tmp_path):
    monkeypatch.setenv("DOMO_OWN_NETWORKS", "10.99.7.0/24")
    monkeypatch.setenv("DOMO_HOST_ADDRESSES", "192.168.1.20")
    reach = reach_from(GatekeeperSettings(), proc_net=tmp_path / "none", lookup=no_lookup)
    assert reach == Reach(
        own_networks=(ip_network("10.99.7.0/24"),),
        host_addresses=(ip_address("192.168.1.20"),),
        own_ports=(8081, 8082),
    )


@pytest.mark.parametrize(
    ("value", "addresses"),
    [
        # compose.yaml: the file's gateways, then the person's DOMO_EXTRA_HOST_ADDRESSES (I5).
        ("10.247.80.1,10.247.81.1,", ("10.247.80.1", "10.247.81.1")),
        ("10.247.80.1,10.247.81.1,192.168.1.11, 172.18.0.1", None),
    ],
)
def test_the_host_addresses_of_compose_with_an_empty_tail_or_more(monkeypatch, value, addresses):
    monkeypatch.setenv("DOMO_HOST_ADDRESSES", value)
    expected = addresses or tuple(item.strip() for item in value.split(","))
    assert GatekeeperSettings().host_addresses == tuple(ip_address(a) for a in expected)


def test_an_empty_variable_is_not_set(monkeypatch):
    monkeypatch.setenv("DOMO_OWN_NETWORKS", " ")
    monkeypatch.setenv("DOMO_HOST_ADDRESSES", "")
    settings = GatekeeperSettings()
    assert settings.own_networks is None and settings.host_addresses is None
    assert settings.host_is_local is False


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DOMO_OWN_NETWORKS", "10.247.81.0/33"),
        ("DOMO_OWN_NETWORKS", "core_net"),
        ("DOMO_HOST_ADDRESSES", "host.docker.internal"),
        ("DOMO_HOST_ADDRESSES", "192.168.1.300"),
    ],
)
def test_a_wrong_value_stops_the_start(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        GatekeeperSettings()


def test_describe_names_what_is_never_reachable_and_the_host():
    reach = Reach(
        own_networks=(ip_network("10.247.81.0/24"),), host_addresses=(ip_address("172.18.0.1"),)
    )
    text = describe(reach)
    assert "10.247.81.0/24" in text and "172.18.0.1" in text
    assert "nothing" in describe(NOTHING)


# --- Resolving a name once ----------------------------------------------------------------------


class CountingLookup:
    """A resolver that answers from a table and counts how often it is asked."""

    def __init__(self, table: dict[str, list[str]]) -> None:
        self.table = table
        self.calls: list[str] = []

    async def __call__(self, name: str) -> list[IPv4Address | IPv6Address]:
        self.calls.append(name)
        if name not in self.table:
            raise OSError("no such name")
        return [ip_address(address) for address in self.table[name]]


async def test_an_ip_literal_is_itself_and_nothing_is_asked():
    lookup = CountingLookup({})
    assert await resolve("192.168.1.10", lookup=lookup) == ip_address("192.168.1.10")
    assert await resolve("fd00::5", lookup=lookup) == ip_address("fd00::5")
    assert await resolve("[fd00::5]", lookup=lookup) == ip_address("fd00::5")
    assert lookup.calls == []


async def test_a_name_with_several_addresses_gives_the_ipv4_one():
    lookup = CountingLookup({"device.local": ["fd00::7", "192.168.1.7", "192.168.1.8"]})
    assert await resolve("device.local", lookup=lookup) == ip_address("192.168.1.7")


async def test_a_name_with_ipv6_addresses_only_gives_the_first():
    lookup = CountingLookup({"site.test": ["2001:db8::1", "2001:db8::2"]})
    assert await resolve("site.test", lookup=lookup) == ip_address("2001:db8::1")


async def test_the_resolver_is_asked_once_per_call():
    lookup = CountingLookup({"device.local": ["192.168.1.7"]})
    await resolve("device.local", lookup=lookup)
    assert lookup.calls == ["device.local"]
    await resolve("device.local", lookup=lookup)
    assert lookup.calls == ["device.local", "device.local"]


async def test_a_name_that_does_not_resolve_is_unresolvable():
    lookup = CountingLookup({"empty.local": []})
    with pytest.raises(Unresolvable):
        await resolve("nowhere.local", lookup=lookup)
    with pytest.raises(Unresolvable):
        await resolve("empty.local", lookup=lookup)
    with pytest.raises(Unresolvable):
        await resolve("", lookup=lookup)


async def test_the_real_lookup_resolves_localhost_to_loopback():
    addresses = await lookup_addresses("localhost")
    assert addresses and all(address.is_loopback for address in addresses)
    assert await resolve("localhost") == ip_address("127.0.0.1")
