"""Where an address leads, seen from the gatekeeper: the house, the internet, the machine that
hosts Domo, or nowhere a program may go.

``zone`` is a pure function of an address and of a ``Reach``: what this installation knows of
its own networks and of the machine that hosts it. ``reach_from`` builds the ``Reach`` once, at
start: from the configuration when it says, else from what the machine shows without any
privilege nor external program (on Linux, the files of ``/proc/net``). Nothing here assumes
Docker: where nothing can be read, nothing is detected and the log says which variables to set.

``resolve`` turns a name into the one address that is then both judged and connected to: a name
is looked up once per request, so a name that changes its target between the two opens nothing.
"""

import asyncio
import logging
import re
import socket
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from ipaddress import (
    IPv4Address,
    IPv4Network,
    IPv6Address,
    IPv6Network,
    ip_address,
    ip_network,
)
from pathlib import Path

from domo_common.gatekeeper_api import Zone

from domo_gatekeeper.config import GatekeeperSettings
from domo_gatekeeper.errors import GatekeeperError

logger = logging.getLogger(__name__)

IPAddress = IPv4Address | IPv6Address
IPNetwork = IPv4Network | IPv6Network
Lookup = Callable[[str], Awaitable[list[IPAddress]]]
LookupNow = Callable[[str], list[IPAddress]]

PROC_NET = Path("/proc/net")
# The name Docker gives the machine that hosts the containers, where it gives one.
HOST_NAME_IN_CONTAINERS = "host.docker.internal"
LOOPBACK = (IPv4Address("127.0.0.1"), IPv6Address("::1"))

# The private ranges: a device of the house (CGNAT included, where some boxes and VPNs live).
_HOUSE: tuple[IPNetwork, ...] = (
    IPv4Network("10.0.0.0/8"),
    IPv4Network("172.16.0.0/12"),
    IPv4Network("192.168.0.0/16"),
    IPv4Network("100.64.0.0/10"),
    IPv6Network("fc00::/7"),
)
# "This network": 0.0.0.0 reaches the machine itself on some systems, and the rest of the range
# is no destination.
_THIS_NETWORK = IPv4Network("0.0.0.0/8")
# A hoster's metadata services outside link-local (169.254.169.254 already is).
_METADATA: frozenset[IPAddress] = frozenset(
    {IPv4Address("100.100.100.200"), IPv6Address("fd00:ec2::254")}
)
# The IPv6 forms that carry an IPv4 address inside, which a network may route to it: IPv4-compatible
# (``::a.b.c.d``), NAT64 (well-known and local-use) and 6to4. No destination of a program.
_CARRIES_IPV4: tuple[IPv6Network, ...] = (
    IPv6Network("::/96"),
    IPv6Network("64:ff9b::/96"),
    IPv6Network("64:ff9b:1::/48"),
    IPv6Network("2002::/16"),
)


class Unresolvable(GatekeeperError):
    """A name gives no address."""


@dataclass(frozen=True)
class Reach:
    """What the gatekeeper knows of where it runs.

    ``own_networks`` are Domo's own (its services, the gatekeeper itself): never reachable.
    ``host_addresses`` are the machine that hosts Domo: closed unless a port of it is opened.
    ``own_ports`` are the ports of Domo's own services on that machine: never reachable there.
    """

    own_networks: tuple[IPNetwork, ...]
    host_addresses: tuple[IPAddress, ...]
    own_ports: tuple[int, ...] = ()
    # The host addresses as ``zone`` compares them, gathered once.
    _hosts: frozenset[IPAddress] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_hosts", frozenset(_plain(host) for host in self.host_addresses))

    def is_host(self, address: IPAddress) -> bool:
        """Whether ``address`` is one of the host addresses (an IPv4-mapped one included)."""
        return _plain(address) in self._hosts


def _plain(address: IPAddress) -> IPAddress:
    """An IPv4-mapped IPv6 address as its IPv4; an IPv6 address without its scope."""
    if isinstance(address, IPv6Address):
        mapped = address.ipv4_mapped
        return mapped if mapped is not None else IPv6Address(int(address))
    return address


def _within(address: IPAddress, networks: Iterable[IPNetwork]) -> bool:
    return any(address in network for network in networks)


def zone(address: IPAddress, reach: Reach, port: int | None = None) -> Zone:
    """Where ``address`` leads; with ``port``, a host address at one of Domo's own ports
    leads nowhere a program may go."""
    address = _plain(address)
    if (
        address.is_unspecified
        or address.is_multicast
        or address.is_link_local
        or address in _METADATA
        or address in _THIS_NETWORK
        or _within(address, reach.own_networks)
    ):
        return "never"
    if reach.is_host(address):
        return "never" if port is not None and port in reach.own_ports else "host"
    # Loopback (the host's above, when the gatekeeper runs on it), reserved ranges, and the IPv6
    # forms that carry an IPv4 address: never.
    if address.is_loopback or address.is_reserved or _within(address, _CARRIES_IPV4):
        return "never"
    if _within(address, _HOUSE):
        return "house"
    return "internet"


# --- Resolving ----------------------------------------------------------------------------------


def _literal(host: str) -> IPAddress | None:
    try:
        return ip_address(host.removeprefix("[").removesuffix("]"))
    except ValueError:
        return None


def _unique[T](items: Iterable[T]) -> list[T]:
    """``items`` each once, in their order."""
    return list(dict.fromkeys(items))


_ADDRESS_FAMILIES = (socket.AF_INET, socket.AF_INET6)


def _addresses(answers: Iterable[tuple[int, int, int, str, tuple[object, ...]]]) -> list[IPAddress]:
    """The addresses of ``getaddrinfo``'s answers, each once, in their order."""
    return _unique(
        ip_address(sockaddr[0])
        for family, _, _, _, sockaddr in answers
        if family in _ADDRESS_FAMILIES and isinstance(sockaddr[0], str)
    )


async def lookup_addresses(name: str) -> list[IPAddress]:
    """Every address the system's resolver gives ``name``, in its order."""
    answers = await asyncio.get_running_loop().getaddrinfo(name, None, type=socket.SOCK_STREAM)
    return _addresses(answers)


async def resolve(host: str, *, lookup: Lookup = lookup_addresses) -> IPAddress:
    """The one address a request to ``host`` goes to: an IP literal is itself; a name is looked
    up once, and its first IPv4 address is preferred to its first IPv6 one.

    Raises ``Unresolvable`` when the name gives no address.
    """
    literal = _literal(host)
    if literal is not None:
        return literal
    if not host:
        raise Unresolvable("no name to resolve")
    try:
        addresses = await lookup(host)
    except (OSError, UnicodeError, ValueError):
        raise Unresolvable("the name gives no address") from None
    for address in addresses:
        if isinstance(address, IPv4Address):
            return address
    if not addresses:
        raise Unresolvable("the name gives no address")
    return addresses[0]


def lookup_now(name: str) -> list[IPAddress]:
    """``lookup_addresses`` for the start, before any event loop: no address on any error."""
    try:
        answers = socket.getaddrinfo(name, None, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return []
    return _addresses(answers)


# --- What this machine shows --------------------------------------------------------------------


@dataclass(frozen=True)
class _Interfaces:
    """What ``/proc/net`` says: the networks of each interface, the addresses of this machine,
    the interfaces the default routes leave by and their gateways."""

    networks: tuple[tuple[str, IPNetwork], ...]
    addresses: tuple[IPAddress, ...]
    way_out: frozenset[str]
    gateways: tuple[IPAddress, ...]


def _read(path: Path) -> list[str] | None:
    try:
        return path.read_text(encoding="ascii", errors="replace").splitlines()
    except OSError:
        return None


def _ipv4_le(field: str) -> IPv4Address:
    return IPv4Address(int(field, 16).to_bytes(4, "big")[::-1])


def _ipv6_hex(field: str) -> IPv6Address:
    return IPv6Address(int(field, 16))


_FIB_ENTRY = re.compile(r"\|--\s+(\S+)")


def _read_interfaces(proc_net: Path) -> _Interfaces | None:
    """The interfaces as Linux shows them in ``proc_net``; ``None`` when the routes cannot be
    read (another system, or no ``/proc``)."""
    routes = _read(proc_net / "route")
    if routes is None:
        return None
    networks: list[tuple[str, IPNetwork]] = []
    addresses: list[IPAddress] = []
    way_out: set[str] = set()
    gateways: list[IPAddress] = []

    # Iface Destination Gateway Flags RefCnt Use Metric Mask ... (little-endian hexadecimal).
    for line in routes[1:]:
        fields = line.split()
        if len(fields) < 8 or fields[0] == "lo":
            continue
        try:
            destination, gateway, mask = (_ipv4_le(fields[i]) for i in (1, 2, 7))
            if int(mask) == 0:
                way_out.add(fields[0])
                if int(gateway):
                    gateways.append(gateway)
            elif not int(gateway):
                networks.append((fields[0], ip_network(f"{destination}/{mask}", strict=False)))
        except (ValueError, OverflowError):
            continue

    # The local addresses are the "/32 host LOCAL" entries, each under its "|-- address" line.
    current: str | None = None
    for line in _read(proc_net / "fib_trie") or []:
        if entry := _FIB_ENTRY.search(line):
            current = entry.group(1)
        elif (
            current is not None
            and "/32 host LOCAL" in line
            and (address := _literal(current)) is not None
        ):
            addresses.append(address)

    # ipv6_route: destination, its prefix, source, its prefix, next hop, metric, ..., interface.
    for line in _read(proc_net / "ipv6_route") or []:
        fields = line.split()
        if len(fields) < 10 or fields[9] == "lo":
            continue
        try:
            if int(fields[1], 16) == 0:
                way_out.add(fields[9])
                if (next_hop := _ipv6_hex(fields[4])) != IPv6Address(0):
                    gateways.append(next_hop)
        except ValueError:
            continue

    # if_inet6: address, interface index, prefix length, scope, flags, interface.
    for line in _read(proc_net / "if_inet6") or []:
        fields = line.split()
        if len(fields) < 6:
            continue
        try:
            address = _ipv6_hex(fields[0])
            network = IPv6Network(f"{address}/{int(fields[2], 16)}", strict=False)
        except ValueError:
            continue
        if address.is_loopback or address.is_link_local:
            continue
        addresses.append(address)
        networks.append((fields[5], network))

    return _Interfaces(
        networks=tuple(networks),
        addresses=tuple(address for address in _unique(addresses) if not address.is_loopback),
        way_out=frozenset(way_out),
        gateways=tuple(_unique(gateways)),
    )


def reach_from(
    settings: GatekeeperSettings,
    *,
    proc_net: Path = PROC_NET,
    lookup: LookupNow = lookup_now,
) -> Reach:
    """What the gatekeeper may reach where it runs.

    Own networks: ``settings.own_networks`` when set, else the networks of this machine's
    interfaces but the ones the default routes leave by. Host addresses: the default gateways,
    what ``host.docker.internal`` resolves to, and, when the gatekeeper runs on the machine
    itself (``host_is_local``), the machine's own addresses and loopback; then
    ``settings.host_addresses``, which adds to them and never drops one. In a container, the
    gatekeeper's own addresses are never reachable, whatever the configuration says: they are
    the gatekeeper itself. The IP literals of ``settings.site_addresses`` are always host
    addresses. Domo's own ports there are ``settings.own_ports`` and the gatekeeper's own two
    (nothing else is guessed).
    """
    interfaces = _read_interfaces(proc_net)
    if interfaces is None and (settings.own_networks is None or settings.host_addresses is None):
        logger.warning(
            "The routes of this machine cannot be read: nothing is detected of my own networks "
            "nor of the machine that hosts me. Set DOMO_OWN_NETWORKS and "
            "DOMO_HOST_ADDRESSES to say them."
        )
    networks = list(interfaces.networks) if interfaces else []
    own_addresses = list(interfaces.addresses) if interfaces else []
    way_out = interfaces.way_out if interfaces else frozenset[str]()

    own: list[IPNetwork]
    if settings.own_networks is not None:
        own = list(settings.own_networks)
    else:
        own = [network for iface, network in networks if iface not in way_out]
    if not settings.host_is_local:
        own += [ip_network(address) for address in own_addresses if not _within(address, own)]

    # A setting adds host addresses, never drops one that is detected.
    host: list[IPAddress] = list(interfaces.gateways) if interfaces else []
    host += lookup(HOST_NAME_IN_CONTAINERS)
    if settings.host_is_local:
        host += own_addresses + lookup(socket.gethostname()) + list(LOOPBACK)
    host += settings.host_addresses or ()
    # The addresses the page is opened with are this machine's, whatever else is said.
    host += _site_literals(settings.site_addresses)
    # The gatekeeper's own ports are known, not guessed: closed wherever it listens.
    ports = (
        *settings.own_ports,
        settings.gatekeeper_internal_port,
        settings.gatekeeper_public_port,
    )
    return Reach(
        own_networks=tuple(_unique(own)),
        host_addresses=tuple(_unique(host)),
        own_ports=tuple(_unique(ports)),
    )


def _site_literals(site_addresses: str) -> list[IPAddress]:
    """The IP literals among the page's addresses; its names are left alone (no lookup)."""
    literals = (_literal(item.strip()) for item in site_addresses.split(","))
    return [literal for literal in literals if literal is not None]


def describe(reach: Reach) -> str:
    """One line for the log: what is never reachable, and where the machine that hosts me is."""
    own = ", ".join(str(network) for network in reach.own_networks) or "nothing detected"
    host = ", ".join(str(address) for address in reach.host_addresses) or "nothing detected"
    ports = ", ".join(str(port) for port in reach.own_ports) or "none named"
    return (
        f"My own networks (never reachable): {own}. The machine that hosts me: {host}, "
        f"where my own ports (never reachable) are: {ports}."
    )
