"""The operations of a tool and the code's minimum risks (spec 5.3, 5.8).

A tool names its operations ("order", "follow") and says the risks of each: Domo writes them when
he builds it, the reviewer checks them, and they are kept on the tool (``MadeTool.operations``).
Each request of a running program belongs to the operation it began last (``begin``); its risks
are its operation's, never less than what the code itself sees in the request
(``minimum_risks``). Following an order then never asks what ordering asks.

An operation is kept as ``{"<name>": {"says": <what it does, one plain line>, "risks": [<among
costs, irreversible, physical, outward>]}}``: 1 to ``MAX_OPERATIONS`` of them, each name
``OPERATION_PATTERN`` (the same as a ``begin`` names), each ``says`` 1 to ``MAX_SAYS`` characters
on one plain line, with no shape of a secret. ``read_operations`` reads them (``OperationsNotRight``
otherwise: its message says what, never a value). ``says`` is what Domo is told when an operation
stops for a yes: his own words of when he built it, data he reads, never an instruction."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, cast

from domo_core.madetools.asks import OPERATION_PATTERN, ActAsk, ProgramAsk
from domo_core.memory.service import refuses_secret_shapes
from domo_core.policy.catalog import NO_RISK, PolicyTables, Risks, risks_of
from domo_core.text import is_plain_line

# How many operations a tool has at most, and the longest ``says``.
MAX_OPERATIONS: Final = 10
MAX_SAYS: Final = 200
# The four risks, by the word an operation names them with.
RISK_WORDS: Final = ("costs", "irreversible", "physical", "outward")

_OPERATION: Final = re.compile(OPERATION_PATTERN)


class OperationsNotRight(ValueError):
    """The operations of a tool are not what they must be: the message says what, never a value
    of them."""


@dataclass(frozen=True)
class Operation:
    """One operation of a tool: its name, its four risks, and what it does (``says``)."""

    name: str
    risks: Risks
    says: str


def _risks(raw: object) -> Risks:
    if not isinstance(raw, list):
        raise OperationsNotRight(
            f"the risks of an operation are a list among {', '.join(RISK_WORDS)}"
        )
    words = cast(list[object], raw)
    if any(word not in RISK_WORDS for word in words) or len(set(map(str, words))) != len(words):
        raise OperationsNotRight(
            f"the risks of an operation are a list among {', '.join(RISK_WORDS)}, each once"
        )
    return Risks(**{word: word in words for word in RISK_WORDS})


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
        if not isinstance(value, Mapping) or set(cast(Mapping[str, object], value)) != {
            "says",
            "risks",
        }:
            raise OperationsNotRight("an operation is {says, risks}")
        fields = cast(Mapping[str, object], value)
        operations[name] = Operation(name, _risks(fields["risks"]), _says(fields["says"]))
    return operations


def kept_operations(operations: Mapping[str, Operation]) -> dict[str, object]:
    """``operations`` as they are kept on a tool (the inverse of ``read_operations``)."""
    return {
        name: {
            "says": operation.says,
            "risks": [word for word in RISK_WORDS if getattr(operation.risks, word)],
        }
        for name, operation in operations.items()
    }


def operations_of(raw: object) -> dict[str, Operation]:
    """The operations kept on a tool: none when it declares none (``{}``, a tool built before
    tools declared them) or when what is kept does not read back (never guessed)."""
    if isinstance(raw, Mapping) and not cast(Mapping[object, object], raw):
        return {}
    try:
        return read_operations(cast(object, raw))
    except OperationsNotRight:
        return {}


def minimum_risks(ask: ProgramAsk, policy: PolicyTables) -> Risks:
    """What the code itself sees in a request, whatever its operation says (spec 5.3): an action
    of the catalog (``act``) has its type's risks (``risks_of``); any other request of step 1 has
    none (going out and accounts, which have their own, come with step 2)."""
    if isinstance(ask, ActAsk):
        return risks_of(ask.action, policy)
    return NO_RISK
