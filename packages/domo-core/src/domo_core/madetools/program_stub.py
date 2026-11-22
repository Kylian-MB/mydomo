"""The module ``domo`` as the check of a tool's imports reads it (``madetools.imports``): the
names a program may use, and nothing else.

The real module is the sandbox's (``domo_sandbox.program_module``), given to the child as text;
the core never imports it and never runs a program. This one has the same public names and no
behaviour: the check resolves ``domo.read`` or ``from domo import Refused`` against it, and
refuses a name it does not have (``domo._send``, ``domo.json``). It imports nothing, so that no
module is reachable through it."""

# Whether the run is a trial of the tool (its entry says ``"dry_run": true``).
dry_run = False

_ONLY_IN_THE_SANDBOX = "the module domo works only in the sandbox"


class Refused(Exception):  # noqa: N818 - the name a program reads
    """The core did not do the request: ``reason`` says why, ``detail`` may say more."""

    def __init__(self, reason: str, detail: str | None = None) -> None:
        super().__init__(reason if detail is None else f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def begin(operation: str, amount_cents: int | None = None) -> object:
    """Says what the program is about to do (an operation of the tool, and what it costs)."""
    raise NotImplementedError(_ONLY_IN_THE_SANDBOX)


def read(sql: str, params: object = (), space: str = "common") -> object:
    """Reads the tools' database: the rows of one statement."""
    raise NotImplementedError(_ONLY_IN_THE_SANDBOX)


def write(sql: str, params: object = (), space: str = "common") -> object:
    """Writes the tools' database: one statement."""
    raise NotImplementedError(_ONLY_IN_THE_SANDBOX)


def act(action: str, arguments: dict[str, object]) -> object:
    """Does an action of the catalog."""
    raise NotImplementedError(_ONLY_IN_THE_SANDBOX)


def tool(name: str, arguments: dict[str, object]) -> object:
    """Launches another tool and gives its result."""
    raise NotImplementedError(_ONLY_IN_THE_SANDBOX)


def later(at: str, arguments: dict[str, object]) -> object:
    """Has this tool launched again at ``at``, with ``arguments``."""
    raise NotImplementedError(_ONLY_IN_THE_SANDBOX)


def wake(content: object) -> object:
    """Wakes Domo with ``content``: he reads it as data."""
    raise NotImplementedError(_ONLY_IN_THE_SANDBOX)
