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
  heavier one, naming it);
* ``{"do": "out", "method", "url", "headers", "body" | "body_b64", "account"}`` (``OutAsk``, step
  2): an HTTP request the gatekeeper sends for the program. ``method`` is one of ``OUT_METHODS``
  (any case), ``url`` an ``http`` or ``https`` address that names a host, without a user or a
  password in it nor any blank or control character, ``MAX_URL`` characters at most; ``headers``
  an object of header names (an RFC 9110 token) to values of printable ASCII and tabs; the body
  is text (``body``, sent in UTF-8) or base64 (``body_b64``), never both; ``account`` the name of
  an account (``ACCOUNT_NAME_PATTERN``), whose secret the program never sees. What a body may
  weigh is what the line of a request holds (the sandbox's frame).

``arguments`` may be left out (``{}``), and so may ``params`` (none), ``space`` (``common``) and
``amount_cents`` (``null``), and an out's ``headers`` (none), body and ``account`` (or be
``null``). Anything else is not right (``AskNotRight``: its message says what,
never a value of it). This is the union ``ProgramAsk``, not ``policy.asks.Ask`` (what kind of yes
a request asks)."""

import base64
import binascii
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Final, cast, get_args

import httpx
from domo_common.gatekeeper_api import OutMethod
from domo_common.sandbox_api import JsonObject
from pydantic import JsonValue

from domo_core.madetools.models import NAME_PATTERN
from domo_core.outward.models import ACCOUNT_NAME_PATTERN
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

# What an out may ask: its methods, and the longest address (the gatekeeper's own bound).
OUT_METHODS: Final[tuple[str, ...]] = get_args(OutMethod)
MAX_URL: Final = 8192
# The methods that only read: an account used with any other cannot be undone (spec 13.6).
READING_METHODS: Final = frozenset({"GET", "HEAD"})

_NAME: Final = re.compile(NAME_PATTERN)
_OPERATION: Final = re.compile(OPERATION_PATTERN)
_ACCOUNT: Final = re.compile(ACCOUNT_NAME_PATTERN)
# RFC 9110 "token", and what the HTTP stack carries as a header value, as the gatekeeper reads them.
_HEADER_NAME: Final = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_HEADER_VALUE: Final = re.compile(r"[\x20-\x7e\t]*")
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
    "out": (
        frozenset({"do", "method", "url"}),
        frozenset({"do", "method", "url", "headers", "body", "body_b64", "account"}),
    ),
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


@dataclass(frozen=True)
class OutAsk:
    """Send an HTTP request through the gatekeeper: ``method`` (upper case), ``url``, the
    program's ``headers``, its ``body`` (bytes, ``None``: none) and the name of the ``account``
    the gatekeeper puts in it (``None``: none)."""

    method: str
    url: str
    headers: dict[str, str]
    body: bytes | None
    account: str | None

    @property
    def address(self) -> tuple[str, int]:
        """The host and port the request goes to, as the gatekeeper compares them with the open
        addresses: the host in lower case (an IPv6 one without its brackets), the port the
        scheme's own when none is written."""
        return out_address(self.url)

    @property
    def writes(self) -> bool:
        """Whether the request does more than read (``READING_METHODS``)."""
        return self.method not in READING_METHODS


def out_address(url: str) -> tuple[str, int]:
    """The host and port of ``url`` (see ``OutAsk.address``); ``ValueError`` when it has none."""
    parsed = httpx.URL(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if not parsed.host:
        raise ValueError("no host")
    return parsed.host, port


type ProgramAsk = BeginAsk | ReadAsk | WriteAsk | ActAsk | ToolAsk | LaterAsk | WakeAsk | OutAsk


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
        case "out":
            return _out(raw)
        case _:
            return WakeAsk(raw["content"])


def _out(raw: JsonObject) -> OutAsk:
    method = raw["method"]
    if not isinstance(method, str) or method.upper() not in OUT_METHODS:
        raise AskNotRight(f"method is one of {', '.join(OUT_METHODS)}")
    url = _url(raw["url"])
    headers = raw.get("headers")
    if headers is None:
        headers = {}
    if not isinstance(headers, dict):
        raise AskNotRight("headers is an object of names to values")
    for name, value in headers.items():
        if _HEADER_NAME.fullmatch(name) is None:
            raise AskNotRight("a header's name is a token of letters, digits and !#$%&'*+.^_`|~-")
        if not isinstance(value, str) or _HEADER_VALUE.fullmatch(value) is None:
            raise AskNotRight("a header's value is text of printable ASCII, on one line")
    account = raw.get("account")
    if account is not None and (not isinstance(account, str) or not _ACCOUNT.fullmatch(account)):
        raise AskNotRight("account is the name of an account: 2 to 40 of a-z, 0-9 and _")
    return OutAsk(method.upper(), url, cast(dict[str, str], headers), _out_body(raw), account)


def _url(url: JsonValue) -> str:
    if (
        not isinstance(url, str)
        or not 1 <= len(url) <= MAX_URL
        or any(char.isspace() or not char.isprintable() for char in url)
    ):
        raise AskNotRight(
            f"url is an address of 1 to {MAX_URL} characters, with no blank nor control character"
        )
    try:
        parsed = httpx.URL(url)
        _ = out_address(url)
    except (httpx.InvalidURL, TypeError, ValueError):
        parsed = None
    if (
        parsed is None
        or parsed.scheme not in ("http", "https")
        or not parsed.host
        or not 1 <= out_address(url)[1] <= 65535
    ):
        raise AskNotRight("url is an http or https address that names a host, at a port")
    if parsed.userinfo:
        raise AskNotRight("url holds no user nor password: an account is named, never written")
    return url


def _out_body(raw: JsonObject) -> bytes | None:
    text, coded = raw.get("body"), raw.get("body_b64")
    if text is not None and coded is not None:
        raise AskNotRight("a body is text (body) or base64 (body_b64), never both")
    if text is not None:
        if not isinstance(text, str):
            raise AskNotRight("body is text")
        try:
            return text.encode("utf-8")
        except UnicodeEncodeError:
            raise AskNotRight("body is text that UTF-8 can write") from None
    if coded is None:
        return None
    decoded: bytes | None = None
    if isinstance(coded, str):
        try:
            decoded = base64.b64decode(coded, validate=True)
        except (binascii.Error, ValueError):
            decoded = None
    if decoded is None:
        raise AskNotRight("body_b64 is base64")
    return decoded


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
