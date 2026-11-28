"""The operations of a tool and the code's minimum risks (spec 5.3, 5.8).

A tool names its operations ("order", "follow") and says the risks of each: Domo writes them when
he builds it, the reviewer checks them, and they are kept on the tool (``MadeTool.operations``).
Each request of a running program belongs to the operation it began last (``begin``); its risks
are its operation's, never less than what the code itself sees in the request
(``minimum_risks``). Following an order then never asks what ordering asks.

An operation is kept as ``{"<name>": {"says": <what it does, one plain line>, "risks": [<among
costs, irreversible, physical, outward>]}}``: 1 to ``MAX_OPERATIONS`` of them, each name
``OPERATION_PATTERN`` (the same as a ``begin`` names), each ``says`` 1 to ``MAX_SAYS`` characters
on one plain line, with no shape of a secret. An operation that goes out may also name, as
declared, the accounts its requests use (``accounts``: names of ``ACCOUNT_NAME_PATTERN``) and the
addresses they go to (``addresses``: ``{"host", "port"}``, the host as an open address keeps it),
``MAX_NAMED`` of each at most, each once: what Domo reads with the tool (``tool_look``) and the
reviewer checks; nothing here opens an address or binds an account (the request is judged as it
runs). They are kept only when there are some. An operation whose risks are ``physical`` alone may
say ``"harmless": true`` (``Risks.harmless``): done by mistake it hurts nobody, locks nobody out,
exposes nothing and costs nothing (a light, a speaker's volume; never a lock, an alarm, a garage
door, heating, an oven, a valve); declared by Domo when he builds it, checked by the reviewer like
every risk; kept only when true. Not declared (every tool made before it existed), acting in the
house is risky: under a voice the code is not sure of, its launch is judged (the owner,
2026-11-27). ``read_operations`` reads them
(``OperationsNotRight`` otherwise: its message says what, never a value). ``says`` is what Domo
is told when an operation stops for a yes: his own words of when he built it, data he reads, never
an instruction."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, cast

from domo_common.gatekeeper_api import OpenAddress, Zone
from pydantic import ValidationError

from domo_core.madetools.asks import OPERATION_PATTERN, ActAsk, OutAsk, ProgramAsk
from domo_core.memory.service import refuses_secret_shapes
from domo_core.outward.literal import literal_zone
from domo_core.outward.models import ACCOUNT_NAME_PATTERN
from domo_core.policy.catalog import NO_RISK, PolicyTables, Risks, risks_of
from domo_core.text import is_plain_line

# How many operations a tool has at most, and the longest ``says``.
MAX_OPERATIONS: Final = 10
MAX_SAYS: Final = 200
# How many accounts, and how many addresses, an operation names at most.
MAX_NAMED: Final = 10
# The four risks, by the word an operation names them with.
RISK_WORDS: Final = ("costs", "irreversible", "physical", "outward")
# What an operation holds: what it must, and what it may.
_NEEDED: Final = frozenset({"says", "risks"})
_MAY: Final = _NEEDED | {"accounts", "addresses", "harmless"}

_OPERATION: Final = re.compile(OPERATION_PATTERN)
_ACCOUNT: Final = re.compile(ACCOUNT_NAME_PATTERN)


class OperationsNotRight(ValueError):
    """The operations of a tool are not what they must be: the message says what, never a value
    of them."""


@dataclass(frozen=True)
class Operation:
    """One operation of a tool: its name, its four risks, what it does (``says``), and, as
    declared, the accounts its requests out name and the addresses (host, port) they go to."""

    name: str
    risks: Risks
    says: str
    accounts: tuple[str, ...] = ()
    addresses: tuple[tuple[str, int], ...] = ()


def _risks(raw: object, harmless: object = False) -> Risks:
    if not isinstance(raw, list):
        raise OperationsNotRight(
            f"the risks of an operation are a list among {', '.join(RISK_WORDS)}"
        )
    words = cast(list[object], raw)
    if any(word not in RISK_WORDS for word in words) or len(set(map(str, words))) != len(words):
        raise OperationsNotRight(
            f"the risks of an operation are a list among {', '.join(RISK_WORDS)}, each once"
        )
    if harmless is not True and harmless is not False:
        raise OperationsNotRight("harmless is true or false")
    if harmless and words != ["physical"]:
        raise OperationsNotRight(
            "harmless is only for an operation that acts in the house and does nothing else"
        )
    return Risks(**{word: word in words for word in RISK_WORDS}, harmless=harmless)


def _says(raw: object) -> str:
    if (
        not isinstance(raw, str)
        or not raw.strip()
        or len(raw) > MAX_SAYS
        or not is_plain_line(raw)
        or "\n" in raw
    ):
        raise OperationsNotRight(f"says is one plain line of 1 to {MAX_SAYS} characters")
    if refuses_secret_shapes(raw):
        raise OperationsNotRight("says has the shape of a secret: none goes in a tool")
    return raw.strip()


def _named(raw: object, what: str) -> list[object]:
    """``raw`` as the list an operation names ``what`` in (``None``: none)."""
    if raw is None:
        return []
    if not isinstance(raw, list) or len(cast(list[object], raw)) > MAX_NAMED:
        raise OperationsNotRight(f"{what} is a list of {MAX_NAMED} at most")
    return cast(list[object], raw)


def _accounts(raw: object) -> tuple[str, ...]:
    names = _named(raw, "accounts")
    if any(not isinstance(name, str) or _ACCOUNT.fullmatch(name) is None for name in names):
        raise OperationsNotRight("an account is named by 2 to 40 of a-z, 0-9 and _, a letter first")
    if len(set(map(str, names))) != len(names):
        raise OperationsNotRight("each account is named once")
    return tuple(cast(list[str], names))


def _address(raw: object) -> tuple[str, int]:
    if not isinstance(raw, Mapping) or set(cast(Mapping[str, object], raw)) != {"host", "port"}:
        raise OperationsNotRight("an address is {host, port}")
    given = cast(Mapping[str, object], raw)
    host, port = given["host"], given["port"]
    if not isinstance(port, int) or isinstance(port, bool):
        raise OperationsNotRight("the port of an address is a whole number from 1 to 65535")
    try:
        address = OpenAddress(host=cast(str, host), port=port)
    except ValidationError:
        raise OperationsNotRight(
            "an address is a host (a name or an IP address, without a scheme, a port or a path) "
            "and a port from 1 to 65535"
        ) from None
    return address.host, address.port


def _addresses(raw: object) -> tuple[tuple[str, int], ...]:
    addresses = tuple(_address(item) for item in _named(raw, "addresses"))
    if len(set(addresses)) != len(addresses):
        raise OperationsNotRight("each address is named once")
    return addresses


def read_operations(raw: object) -> dict[str, Operation]:
    """The operations of a tool, read from what is kept (see the module docstring), by name.
    ``OperationsNotRight`` when they are not 1 to ``MAX_OPERATIONS`` well-formed operations."""
    if not isinstance(raw, Mapping):
        raise OperationsNotRight("the operations are an object, by name")
    given = cast(Mapping[object, object], raw)
    if not 1 <= len(given) <= MAX_OPERATIONS:
        raise OperationsNotRight(f"a tool has 1 to {MAX_OPERATIONS} operations")
    operations: dict[str, Operation] = {}
    for name, value in given.items():
        if not isinstance(name, str) or _OPERATION.fullmatch(name) is None:
            raise OperationsNotRight(
                "the name of an operation is 1 to 30 of a-z, 0-9 and _, a letter first"
            )
        if not isinstance(value, Mapping) or not (
            _NEEDED <= set(cast(Mapping[str, object], value)) <= _MAY
        ):
            raise OperationsNotRight(
                "an operation is {says, risks}, harmless when it acts in the house harmlessly, "
                "and when it goes out, accounts and addresses"
            )
        fields = cast(Mapping[str, object], value)
        operations[name] = Operation(
            name,
            _risks(fields["risks"], fields.get("harmless", False)),
            _says(fields["says"]),
            _accounts(fields.get("accounts")),
            _addresses(fields.get("addresses")),
        )
    return operations


def kept_operations(operations: Mapping[str, Operation]) -> dict[str, object]:
    """``operations`` as they are kept on a tool (the inverse of ``read_operations``): its
    accounts and its addresses only when it names some."""
    kept: dict[str, object] = {}
    for name, operation in operations.items():
        one: dict[str, object] = {
            "says": operation.says,
            "risks": [word for word in RISK_WORDS if getattr(operation.risks, word)],
        }
        if operation.risks.harmless:
            one["harmless"] = True
        if operation.accounts:
            one["accounts"] = list(operation.accounts)
        if operation.addresses:
            one["addresses"] = [{"host": host, "port": port} for host, port in operation.addresses]
        kept[name] = one
    return kept


def accounts_named(operations: Mapping[str, Operation]) -> dict[str, list[str]]:
    """The accounts ``operations`` name, by name in order, each with the operations that name
    it, by name in order."""
    named: dict[str, list[str]] = {}
    for operation in sorted(operations.values(), key=lambda operation: operation.name):
        for account in operation.accounts:
            named.setdefault(account, []).append(operation.name)
    return dict(sorted(named.items()))


def addresses_named(operations: Mapping[str, Operation]) -> dict[tuple[str, int], list[str]]:
    """The addresses ``operations`` name, by host and port in order, each with the operations that
    name it, by name in order."""
    named: dict[tuple[str, int], list[str]] = {}
    for operation in sorted(operations.values(), key=lambda operation: operation.name):
        for address in operation.addresses:
            named.setdefault(address, []).append(operation.name)
    return dict(sorted(named.items()))


def reaches_told(operations: Mapping[str, Operation]) -> list[str]:
    """What a program is built to reach, as Domo is told with what it says: each address its
    ``operations`` declare, once, by host and port in order, as ``"<host>:<port> (in the
    house)"``, or ``"... (outside the house)"`` when the address is the internet's by its literal
    (``outward.literal.literal_zone``, the one place that says it). ``[]`` when they name none.
    It is what he declared when he built it and the reviewer checked, never what the program
    says of itself; what a request really reaches is judged as it runs."""
    return [
        f"{host}:{port} "
        f"({'outside the house' if literal_zone(host) == 'internet' else 'in the house'})"
        for host, port in addresses_named(operations)
    ]


def operations_of(raw: object) -> dict[str, Operation]:
    """The operations kept on a tool: none when it declares none (``{}``, a tool built before
    tools declared them) or when what is kept does not read back (never guessed)."""
    if isinstance(raw, Mapping) and not cast(Mapping[object, object], raw):
        return {}
    try:
        return read_operations(cast(object, raw))
    except OperationsNotRight:
        return {}


def minimum_risks(ask: ProgramAsk, policy: PolicyTables, *, zone: Zone | None = None) -> Risks:
    """What the code itself sees in a request, whatever its operation says (spec 5.3, 13.6): an
    action of the catalog (``act``) has its type's risks (``risks_of``); a request sent out
    (``out``) leaves the house when its address is the internet's by its literal
    (``outward.literal.literal_zone``) or by the ``zone`` the gatekeeper found for it, and cannot
    be undone when it uses an account for anything but a read (``OutAsk.writes``); any other
    request has none."""
    if isinstance(ask, ActAsk):
        return risks_of(ask.action, policy)
    if isinstance(ask, OutAsk):
        host, _ = ask.address
        outward = literal_zone(host) == "internet" or zone == "internet"
        return Risks(outward=outward, irreversible=ask.account is not None and ask.writes)
    return NO_RISK
