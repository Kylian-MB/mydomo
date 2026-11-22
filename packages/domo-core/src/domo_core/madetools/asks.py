"""What a running program may ask the core, read (spec 5.2): the shapes alone, with no database and
no launch, so that what answers it (``madetools.means``) reads every request the same way.

A program asks through the module ``domo`` (``domo_sandbox.program_module``): each function
writes one line ``{"do": <name>, ...}``, which the launcher checks for its size and its ``do``
before the core sees it. ``read_ask`` reads one such line into what it asks (``ProgramAsk``):

* ``{"do": "begin", "operation", "amount_cents"}`` (``BeginAsk``): the operation it begins, a name
  of ``a-z``, ``0-9`` and ``_`` (``OPERATION_PATTERN``), and what it says it costs, whole cents of
  euros, ``MAX_AMOUNT_CENTS`` at most (``null``: nothing);
* ``{"do": "read" | "write", "sql", "params", "space"}`` (``ReadAsk``, ``WriteAsk``): one statement
  of 1 to ``MAX_SQL`` characters, ``MAX_PARAMS`` parameters at most, in the common space
  (``common``, by default) or the corner of whom the launch is for (``mine``);
* ``{"do": "act", "action", "arguments"}`` (``ActAsk``): an action of the catalog, by its type;
* ``{"do": "tool", "name", "arguments"}`` (``ToolAsk``): another tool of Domo, by its name;
* ``{"do": "later", "at", "arguments"}`` (``LaterAsk``): the same tool again at ``at``, an ISO date
  and time with its offset;
* ``{"do": "wake", "content"}`` (``WakeAsk``): wake Domo with ``content``, any JSON value (what
  it weighs is the launch's ``wake_bytes``, a setting of Domo's: ``madetools.means`` refuses a
  heavier one, naming it).

``arguments`` may be left out (``{}``), and so may ``params`` (none), ``space`` (``common``) and
``amount_cents`` (``null``). Anything else is not right (``AskNotRight``: its message says what,
never a value of it). This is the union ``ProgramAsk``, not ``policy.asks.Ask`` (what kind of yes
a request asks)."""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Final, cast

from domo_common.sandbox_api import JsonObject
from pydantic import JsonValue

from domo_core.madetools.models import NAME_PATTERN
from domo_core.toolsdb.store import Space

# The most characters of one statement, and the most parameters it is given.
MAX_SQL: Final = 20_000
MAX_PARAMS: Final = 100
# The most a begin may say it costs, in cents: what the ledger of spending, the stops and the
# agreements hold (``tools.agreements.MOST_CENTS``, well inside their 32-bit columns).
MAX_AMOUNT_CENTS: Final = 10_000_000
# The longest name of an action type a program may name.
MAX_ACTION: Final = 100
# The name of an operation of a tool.
OPERATION_PATTERN: Final = "^[a-z][a-z0-9_]{0,29}$"

_NAME: Final = re.compile(NAME_PATTERN)
_OPERATION: Final = re.compile(OPERATION_PATTERN)
_SPACES: Final[frozenset[str]] = frozenset({"common", "mine"})
# What each request needs, and what it may hold, by its ``do``.
_FIELDS: Final[dict[str, tuple[frozenset[str], frozenset[str]]]] = {
    "begin": (frozenset({"do", "operation"}), frozenset({"do", "operation", "amount_cents"})),
    "read": (frozenset({"do", "sql"}), frozenset({"do", "sql", "params", "space"})),
    "write": (frozenset({"do", "sql"}), frozenset({"do", "sql", "params", "space"})),
    "act": (frozenset({"do", "action"}), frozenset({"do", "action", "arguments"})),
    "tool": (frozenset({"do", "name"}), frozenset({"do", "name", "arguments"})),
    "later": (frozenset({"do", "at"}), frozenset({"do", "at", "arguments"})),
    "wake": (frozenset({"do", "content"}), frozenset({"do", "content"})),
}


class AskNotRight(ValueError):
    """What a program asked is not what a program may ask: the message says what, never a value
    of it."""


@dataclass(frozen=True)
class BeginAsk:
    """The program begins ``operation``, which it says costs ``amount_cents`` (``None``:
    nothing)."""

    operation: str
    amount_cents: int | None


@dataclass(frozen=True)
class ReadAsk:
    """Read the tools' base: one statement, its parameters, in ``space``."""

    sql: str
    params: tuple[JsonValue, ...]
    space: Space


@dataclass(frozen=True)
class WriteAsk:
    """Write the tools' base: one statement, its parameters, in ``space``."""

    sql: str
    params: tuple[JsonValue, ...]
    space: Space


@dataclass(frozen=True)
class ActAsk:
    """Do the action of the catalog ``action`` with ``arguments``."""

    action: str
    arguments: JsonObject


@dataclass(frozen=True)
class ToolAsk:
    """Launch the tool ``tool`` with ``arguments`` and give its result."""

    tool: str
    arguments: JsonObject


@dataclass(frozen=True)
class LaterAsk:
    """Launch the tool that asks again at ``at`` (aware), with ``arguments``."""

    at: datetime
    arguments: JsonObject


@dataclass(frozen=True)
class WakeAsk:
    """Wake Domo with ``content``, data he reads, never an instruction."""

    content: JsonValue


type ProgramAsk = BeginAsk | ReadAsk | WriteAsk | ActAsk | ToolAsk | LaterAsk | WakeAsk


def read_ask(raw: JsonObject) -> ProgramAsk:
    """What a program asks, read from its line (see the module docstring). Raises
    ``AskNotRight`` when it is not what a program may ask."""
    do = raw.get("do")
    if not isinstance(do, str) or do not in _FIELDS:
        raise AskNotRight(f"do is one of {', '.join(_FIELDS)}")
    needs, may = _FIELDS[do]
    if not needs <= set(raw) or not set(raw) <= may:
        raise AskNotRight(
            f"a {do} needs {', '.join(sorted(needs - {'do'}))} and takes only "
            f"{', '.join(sorted(may - {'do'}))}"
        )
    match do:
        case "begin":
            return _begin(raw)
        case "read" | "write":
            sql, params, space = _statement(raw)
            return ReadAsk(sql, params, space) if do == "read" else WriteAsk(sql, params, space)
        case "act":
            action = raw["action"]
            if not isinstance(action, str) or not 1 <= len(action) <= MAX_ACTION:
                raise AskNotRight(
                    f"action is the type of an action, {MAX_ACTION} characters at most"
                )
            return ActAsk(action, _arguments(raw))
        case "tool":
            name = raw["name"]
            if not isinstance(name, str) or _NAME.fullmatch(name) is None:
                raise AskNotRight("name is the name of a tool")
            return ToolAsk(name, _arguments(raw))
        case "later":
            return LaterAsk(_moment(raw["at"]), _arguments(raw))
        case _:
            return WakeAsk(raw["content"])


def _begin(raw: JsonObject) -> BeginAsk:
    operation = raw["operation"]
    if not isinstance(operation, str) or _OPERATION.fullmatch(operation) is None:
        raise AskNotRight("operation is 1 to 30 characters: a to z, 0 to 9 and _, a letter first")
    amount = raw.get("amount_cents")
    if amount is not None and (
        isinstance(amount, bool)
        or not isinstance(amount, int)
        or not 0 <= amount <= MAX_AMOUNT_CENTS
    ):
        raise AskNotRight(
            f"amount_cents is a whole number of cents, 0 to {MAX_AMOUNT_CENTS}, or null"
        )
    return BeginAsk(operation, amount)


def _statement(raw: JsonObject) -> tuple[str, tuple[JsonValue, ...], Space]:
    sql = raw["sql"]
    if not isinstance(sql, str) or not 1 <= len(sql) <= MAX_SQL or not sql.strip():
        raise AskNotRight(f"sql is one statement of 1 to {MAX_SQL} characters")
    params = raw.get("params", [])
    if not isinstance(params, list):
        raise AskNotRight("params is a list")
    if len(params) > MAX_PARAMS:
        raise AskNotRight(f"at most {MAX_PARAMS} params")
    space = raw.get("space", "common")
    if not isinstance(space, str) or space not in _SPACES:
        raise AskNotRight("space is common or mine")
    return sql, tuple(params), cast(Space, space)


def _arguments(raw: JsonObject) -> JsonObject:
    arguments = raw.get("arguments", {})
    if not isinstance(arguments, dict):
        raise AskNotRight("arguments is an object")
    return arguments


def _moment(at: JsonValue) -> datetime:
    when: datetime | None = None
    if isinstance(at, str):
        try:
            when = datetime.fromisoformat(at)
        except ValueError:
            when = None
    if when is None or when.tzinfo is None:
        raise AskNotRight("at is an ISO date and time with its offset")
    return when
