"""Where a key may travel without encryption.

A key is only sent over plain http to a host that is not on the public Internet. The gatekeeper
enforces this rule when it registers a provider and again at every relayed call; the core applies
the same rule when the owner chooses a provider, so that a refusal is explained at the form and
not after a key was typed. The rule lives here so that the two cannot drift apart.
"""

import ipaddress
import re

# Networks a key may cross in cleartext: the loopback, the private ranges (RFC 1918 and unique
# local addresses) and the link-local ranges. Spelled out rather than taken from
# ``ipaddress``'s ``is_private``, which also covers documentation and reserved ranges.
_LOCAL_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in (
        "127.0.0.0/8",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    )
)
# A single label that inet_aton-style resolvers read as a (decimal, octal or hex) IPv4 number.
_NUMERIC_LABEL = re.compile(r"[0-9]+|0x[0-9a-f]*", re.IGNORECASE)


def cleartext_allowed(host: str) -> bool:
    """Whether a key may be sent to ``host`` over plain http.

    Only to a host that is not on the public Internet: ``localhost``, an IP literal in a
    loopback, private or link-local range (IPv4 or IPv6), or a single-label name such as a
    Docker service name. Every other host needs https.
    """
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return bool(host) and "." not in host and _NUMERIC_LABEL.fullmatch(host) is None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    # An address is never "in" a network of the other IP version.
    return any(address in network for network in _LOCAL_NETWORKS)
