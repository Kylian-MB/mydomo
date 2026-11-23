"""The zone of an address, read on its literal alone (spec 13.6): the one place that says it.

Opening an address towards the internet "leaves the house", and that risk is known when Domo
opens it, before anything is resolved: so it is read on the host as written. An IP literal in a
private range (``10/8``, ``172.16/12``, ``192.168/16``, ``100.64/10``, ``fc00::/7``, loopback,
link-local), a name of a single label (``printer``), or a name ending in ``.local``, ``.lan`` or
``.home.arpa`` is the house; anything else is the internet. Numbers a resolver reads as an IPv4
address (``134744072``, ``0x8080808``, ``10.1``) are that address, never a name of the house.

This is only the risk Domo weighs. What a request may reach is the gatekeeper's to judge, on the
address the name resolves to (``domo_gatekeeper.reach``), never on this.
"""

import ipaddress
import re
from typing import Final, Literal, cast

from domo_core.policy.catalog import Risks

LiteralZone = Literal["house", "internet"]

_HOUSE_NETWORKS: Final = tuple(
    ipaddress.ip_network(network)
    for network in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "fc00::/7",
        "::1/128",
        "fe80::/10",
    )
)
_HOUSE_SUFFIXES: Final = (".local", ".lan", ".home.arpa")


def _ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """``host`` as an IP literal (an IPv6 one with or without its brackets), else ``None``."""
    bare = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        address = ipaddress.ip_address(bare)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


# One part of an IPv4 address as a resolver reads it (``inet_aton``): hexadecimal after ``0x``,
# octal after a ``0``, decimal otherwise.
_NUMBER = re.compile(r"0x[0-9a-f]+|0[0-7]*|[1-9][0-9]*")


def _number(part: str) -> int | None:
    """``part`` as a resolver reads a number of an address, else ``None``."""
    if _NUMBER.fullmatch(part) is None:
        return None
    if part.startswith("0x"):
        return int(part, 16)
    return int(part, 8) if part.startswith("0") else int(part)


def _numbers(name: str) -> ipaddress.IPv4Address | Literal["no address"] | None:
    """``name`` as the IPv4 address a resolver reads in one to four numbers (``134744072``,
    ``0x8080808``, ``10.1``, ``0300.0250.1.20``: the last one fills the bytes left); ``"no
    address"`` when they are numbers that make none; ``None`` when ``name`` is not made of
    numbers (a name)."""
    parts = name.split(".")
    if not 1 <= len(parts) <= 4:
        return None
    numbers = [_number(part) for part in parts]
    if any(number is None for number in numbers):
        return None
    *first, last = cast(list[int], numbers)
    room = 256 ** (4 - len(first))
    if any(number > 255 for number in first) or last >= room:
        return "no address"
    value = last
    for place, number in enumerate(reversed(first)):
        value += number * room * 256**place
    return ipaddress.IPv4Address(value)


def _of_the_house(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    # Compared within its own version: an IPv4 address is never read against an IPv6 range.
    return any(
        network.version == address.version and address in network for network in _HOUSE_NETWORKS
    )


def literal_zone(host: str) -> LiteralZone:
    """Whether ``host``, as written, is an address of the house or of the internet (see the
    module docstring). Letters are compared in lower case, and a final dot is ignored."""
    written = host.strip().lower()
    address = _ip(written)
    if address is not None:
        return "house" if _of_the_house(address) else "internet"
    name = written.removesuffix(".")
    numbers = _numbers(name)
    if numbers == "no address":
        return "internet"
    if numbers is not None:
        return "house" if _of_the_house(numbers) else "internet"
    if name and "." not in name:
        return "house"
    return "house" if name.endswith(_HOUSE_SUFFIXES) else "internet"


def is_an_address(host: str) -> bool:
    """Whether ``host`` is written as an address (an IP literal, or numbers a resolver reads as
    one), so that its literal settles its zone; a name's zone is the gatekeeper's to find, on
    what it resolves to."""
    written = host.strip().lower()
    return _ip(written) is not None or _numbers(written.removesuffix(".")) is not None


# A label of a host name: ASCII letters, digits, ``-`` and ``_``, neither first nor last a ``-``.
_LABEL: Final = re.compile(r"[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?")


def is_a_host(host: str) -> bool:
    """Whether ``host`` is a valid host: an IP literal (an IPv6 one with or without its brackets)
    or a name of ASCII labels in lower case, 253 characters at most (a final dot allowed).
    Anything else is text a program wrote, never kept where a host is (an audit line, the
    figures of a tool)."""
    if _ip(host) is not None:
        return True
    name = host.removesuffix(".")
    return 0 < len(name) <= 253 and all(_LABEL.fullmatch(label) for label in name.split("."))


def open_risks(host: str) -> Risks:
    """The risks of opening an address at ``host`` (``net.open``): it leaves the house
    (``outward``) when the address is the internet's, and carries no other."""
    return Risks(outward=literal_zone(host) == "internet")
