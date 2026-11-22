"""The module ``domo`` a program imports in the sandbox: its requests to the core.

The launcher never imports this file: it reads it as text and gives it to the child, whose
bootstrap makes it the module ``domo`` (``-I -S``: the child reaches nothing else of the
launcher). Each function writes one request, one line of JSON ``{"ask": {"do": <name>, ...}}``, on
the run's own answer descriptor, then reads the core's answer, one line, on the run's own standard
input: ``{"ok": value}`` gives ``value``, ``{"refused": reason, "detail": ...}`` raises
``Refused``. One request at a time: a function returns only once its answer came.

``dry_run`` is true when the run is a trial of the tool (its entry says ``"dry_run": true``).
"""

import json
from typing import BinaryIO, cast

dry_run = False

_answers: BinaryIO | None = None
_asks: BinaryIO | None = None


class Refused(Exception):  # noqa: N818 - the name a program reads
    """The core did not do the request: ``reason`` says why, ``detail`` may say more."""

    def __init__(self, reason: str, detail: str | None = None) -> None:
        super().__init__(reason if detail is None else f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def _connect(  # pyright: ignore[reportUnusedFunction] - called by the bootstrap
    answers: BinaryIO, asks: BinaryIO, is_dry_run: bool
) -> None:
    """Called once by the bootstrap: where the answers come from and where the requests go."""
    global _answers, _asks, dry_run
    _answers, _asks, dry_run = answers, asks, is_dry_run


def _ask(do: str, fields: dict[str, object]) -> object:
    if _answers is None or _asks is None:
        raise Refused("no_channel")
    asked = {"ask": {"do": do, **fields}}
    try:
        # Weighed in UTF-8 by the launcher, as the core weighs what it reads.
        line = json.dumps(asked, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except UnicodeEncodeError:
        # A lone surrogate has no UTF-8: escaped, it is the core's to refuse.
        line = json.dumps(asked, allow_nan=False).encode("utf-8")
    _asks.write(line + b"\n")
    _asks.flush()
    raw = _answers.readline()
    if not raw:
        raise Refused("no_channel")
    answer: object = json.loads(raw)
    if isinstance(answer, dict):
        fields = cast("dict[str, object]", answer)
        if "ok" in fields:
            return fields["ok"]
        if "refused" in fields:
            detail = fields.get("detail")
            raise Refused(str(fields["refused"]), None if detail is None else str(detail))
    raise Refused("no_answer")


def begin(operation: str, amount_cents: int | None = None) -> object:
    """Says what the program is about to do (an operation of the tool, and what it costs)."""
    return _ask("begin", {"operation": operation, "amount_cents": amount_cents})


def read(sql: str, params: tuple[object, ...] | list[object] = (), space: str = "common") -> object:
    """Reads the tools' database: the rows of one statement."""
    return _ask("read", {"sql": sql, "params": list(params), "space": space})


def write(
    sql: str, params: tuple[object, ...] | list[object] = (), space: str = "common"
) -> object:
    """Writes the tools' database: one statement."""
    return _ask("write", {"sql": sql, "params": list(params), "space": space})


def act(action: str, arguments: dict[str, object]) -> object:
    """Does an action of the catalog."""
    return _ask("act", {"action": action, "arguments": arguments})


def tool(name: str, arguments: dict[str, object]) -> object:
    """Launches another tool and gives its result."""
    return _ask("tool", {"name": name, "arguments": arguments})


def later(at: str, arguments: dict[str, object]) -> object:
    """Has this tool launched again at ``at``, with ``arguments``."""
    return _ask("later", {"at": at, "arguments": arguments})


def wake(content: object) -> object:
    """Wakes Domo with ``content``: he reads it as data."""
    return _ask("wake", {"content": content})
