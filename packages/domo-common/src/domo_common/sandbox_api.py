"""Models shared by the Domo core and the sandbox launcher.

The core opens a run with one piece of code, an entry and limits (``RunRequest`` on
``POST /runs``, answered ``RunOpened``). While it runs, the program asks the core for what it
needs, one request at a time: each ``POST /runs/{run}/next`` carries the answer to the last
request (``RunNext``) and gives back the next one (``RunAsk``) or the end (``RunEnded``, with
the ``RunResult``). ``POST /runs/{run}/alive`` says the core still works on an answer, and
``POST /runs/{run}/stop`` ends it. A run's ``max_seconds`` counts the program's own time, not the
time the core takes to answer it. A run the launcher cannot open is refused (409, ``RunRefused``).
The launcher also scans a code with Bandit (``ScanRequest`` / ``ScanResult``) without running it,
says what its machine has (``GET /machine``, ``Machine``) and takes how many runs it runs at once
from the core (``PUT /places``, ``Places``).

A tool's code is a module that defines ``run(entry)``; it returns a JSON value. It may
``import domo`` (``PROGRAM_MODULE``), whose functions (``PROGRAM_FUNCTIONS``) are its requests.
"""

from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

# A JSON object: the entry of a tool.
type JsonObject = dict[str, JsonValue]

# The bounds of a run are Domo's settings (spec 12.1): the core sends them on every run
# (``run_seconds`` as ``max_seconds``, ``memory_mb``, ``output_bytes``), with no ceiling above
# them; the defaults are only what a request that says nothing gets. The launcher refuses only
# what its machine cannot give (``RunRefused``, ``not_here``).
DEFAULT_SECONDS: Final = 60
DEFAULT_MEMORY_MB: Final = 256
DEFAULT_OUTPUT_BYTES: Final = 65_536
# A time past this (a hundred years) is one no clock reaches: a run given more is bounded by it,
# which is to say never by its clock (a figure that big must not break the arithmetic of a clock).
LONGEST_SECONDS: Final = 36_500 * 86_400

# How many bytes the launcher holds, at worst, for each byte of a run's output: the line in its
# buffer and the copy it reads (2), the value read from it (a JSON text of empty arrays or objects
# comes to about 20 times its size as Python objects), the answer written again (1), and room.
OUTPUT_HELD: Final = 24

# One request of a program, as a line of JSON: at most this size. It is the frame of the
# channel's protocol, not a bound of use.
MAX_ASK_BYTES: Final = 65_536
# What one answer of the core a program reads weighs at most, as JSON, when the request says
# nothing (``RunRequest.answer_bytes``): the core sends the launch's ``result_bytes``, a setting of
# Domo's (2026-11-22, ruling R33), so that a program reads what a tool it launched may give back.
DEFAULT_ANSWER_BYTES: Final = 65_536
# The most one answer weighs, whatever ``result_bytes`` says (ruling R37): what the core gathers
# for a read and what the core and the sandbox hold of one answer. A protection of the machine, not
# a bound of use: past it a program reads in pieces.
ANSWER_CEILING_BYTES: Final = 8 * 1024 * 1024
# What a refusal past it says, in the core's words: never a setting to raise.
ANSWER_CEILING_WORDS: Final = (
    f"over {ANSWER_CEILING_BYTES} bytes, the code's ceiling of one answer (not a setting): read it "
    "in pieces"
)
# A run the core leaves waiting this long (no next, no stop, no alive) is ended.
IDLE_SECONDS: Final = 60
# While the core works on an answer, it says the run is still awaited this often
# (``POST /runs/{run}/alive``): an answer may take longer than ``IDLE_SECONDS``.
ALIVE_EVERY_SECONDS: Final = 20
# The module a program imports, and its functions: each one is a request to the core.
PROGRAM_MODULE: Final = "domo"
PROGRAM_FUNCTIONS: Final = ("begin", "read", "write", "act", "tool", "later", "wake")

# How a run ended: ``killed`` only when the sandbox tried to end it and could not;
# ``ended_by_machine`` when the program died of a signal the sandbox did not send (the kernel's
# memory killer, a processor limit): a failure of its own kind, never a run that could not be ended.
RunStatus = Literal["done", "error", "timeout", "too_big", "killed", "stopped", "ended_by_machine"]


class RunRequest(BaseModel):
    """One launch: the code, its entry and its limits, which have a floor (one second, one
    megabyte, one byte) and no ceiling (see ``DEFAULT_SECONDS``).

    ``answer_bytes``: what one answer of the core the program reads may weigh, as JSON (the
    launch's ``result_bytes``); the core refuses a heavier one itself, naming the setting.
    ``wait_for_a_place``: when false and every place is taken, the run is refused at once (409,
    ``RunRefused`` ``busy``) instead of waiting for one.
    """

    model_config = ConfigDict(extra="forbid")

    code: str
    entry: JsonObject
    max_seconds: int = Field(default=DEFAULT_SECONDS, ge=1)
    memory_mb: int = Field(default=DEFAULT_MEMORY_MB, ge=1)
    output_bytes: int = Field(default=DEFAULT_OUTPUT_BYTES, ge=1)
    answer_bytes: int = Field(default=DEFAULT_ANSWER_BYTES, ge=1)
    wait_for_a_place: bool = True


class Machine(BaseModel):
    """What the launcher has (``GET /machine``): the memory its runs may have in all, in
    megabytes, and its processors (``None`` when it cannot tell), and how many runs it runs at
    once now (``places``, which the core sets: ``PUT /places``)."""

    model_config = ConfigDict(extra="forbid")

    memory_mb: int | None
    processors: int | None
    places: int


class Places(BaseModel):
    """How many runs the launcher runs at once (``PUT /places``): Domo's setting ``places``."""

    model_config = ConfigDict(extra="forbid")

    places: int = Field(ge=1)


# The limits of a run the launcher may refuse because its machine cannot give them.
type Beyond = Literal["memory_mb", "output_bytes"]


class RunRefused(BaseModel):
    """Why a run was not opened (409): ``busy``, no place free and it may not wait for one; or
    ``not_here``, its machine cannot give what it asks (``beyond``: which limits), said with what
    it has (``has``)."""

    model_config = ConfigDict(extra="forbid")

    refused: Literal["busy", "not_here"]
    beyond: list[Beyond] | None = None
    has: Machine | None = None


class RunResult(BaseModel):
    """What a launch gave back.

    ``error`` is the program's own words: the last line of its traceback (the exception type and
    its message), never a path. ``note`` is the sandbox's own words on how the run ended (out of
    time, an answer too big, a request not valid, ended by the machine...), never the program's.
    """

    model_config = ConfigDict(extra="forbid")

    status: RunStatus
    output: JsonValue | None = None
    error: str | None = None
    note: str | None = None
    seconds: float


class RunOpened(BaseModel):
    """A run the launcher holds: its id, which nobody can guess."""

    model_config = ConfigDict(extra="forbid")

    run: str


class RunNext(BaseModel):
    """The answer to the program's last request (none before the first one)."""

    model_config = ConfigDict(extra="forbid")

    answer: JsonObject | None = None


class RunAsk(BaseModel):
    """A request of the program, the ``n``-th of its run: it waits for the answer."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["ask"] = "ask"
    n: int
    ask: JsonObject


class RunEnded(BaseModel):
    """The run is over: what it gave back."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["end"] = "end"
    result: RunResult


RunEvent = Annotated[RunAsk | RunEnded, Field(discriminator="kind")]


class ScanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str


class ScanFinding(BaseModel):
    """One alert of the analyser."""

    model_config = ConfigDict(extra="forbid")

    test_id: str
    severity: str
    line: int
    text: str


class ScanResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    findings: list[ScanFinding]
