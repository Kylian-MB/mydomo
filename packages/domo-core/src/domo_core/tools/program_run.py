"""``program_run``: Domo runs a program of his own by hand, at once (spec 5.4, 5.8).

Everything a tool does, Domo can do himself without the tool: when nothing he has does what is
asked and the person waits for it, he writes a program and runs it now; when the request will come
again, he builds the tool from it (``tool_build`` with ``from_run``). The call is Domo's own act
(``ActsAsDomo``: ``program.run`` judged with his rights, in anyone's turn) and is silent: no
card, no line in the history, the judge never asked (``policy.catalog.is_silent``).

Before it runs, the program passes the two quick checks of a tool, and only those (a tool kept
passes four): what it imports and uses (``imports.forbidden``, read by ``ast``, never executed
here) with no text that has the shape of a secret in it (``imports.secret_literals``), then the
analyser, in the sandbox (``Sandbox.scan``: a finding of severity medium or high). A finding runs
nothing: the call is ``invalid``, with the findings (their lines, never the code). Arguments that
hold the shape of a secret run nothing either (``not_possible``): a secret is never given to a
program.

Its code has the house's ``code_chars`` at most, and it may take the time its call says
(``max_seconds``, 1 at least, no ceiling of the code; the house's ``run_seconds`` when it does not
say): both are settings of Domo's (``madetools.settings``). A time past ``run_seconds`` is a
change of that setting for this run: it runs only when Domo's right on ``tools.settings`` allows
it (ruling R31: the owner's deny holds here too), else nothing runs (``not_possible``, with the
setting).

It is then kept (``HandRun``: its code, operations, arguments and time, who opened it and for whom,
the house's ``hand_runs_kept_days``, ``madetools.upkeep``) and launched for the person of the turn
(for the house in a turn Domo acts in on his own) through the very path of a tool's launch
(``handover.launch_in_a_turn`` with ``AskingTool.of_run``, then ``launch.launch_by_hand``): the turn
waits ``HAND_OVER_SECONDS`` and hands it over, and each request of its program is judged by the
barrier with the rights of whom it runs for, under the same bounds (``madetools.means``). A stop for
a yes is told as a tool's is, and run again by the same call with ``yes_for`` alone, under the same
rules as a tool's (``madetools.offered``: who must answer, what the yes carries, used once); such a
call waits for its yes and is shown like any request that waits. What the model reads is
``{"launch", "result", "run"}`` (``run``: the id of the kept program, for ``from_run``).

The audit keeps the barrier's decision on ``program.run`` (its facts: those of a yes it carries,
never the code nor an argument) and, from the launch, ``program.ran`` (the status, the seconds and
how many requests); never the code, the arguments nor the result.
"""

import json
import uuid
from collections.abc import Iterator
from dataclasses import replace
from typing import Final, Literal, cast

from domo_common.sandbox_api import JsonObject

from domo_core.llm.types import ToolDefinition
from domo_core.madetools.checks import BLOCKING
from domo_core.madetools.handover import AskingTool, launch_in_a_turn
from domo_core.madetools.imports import forbidden, secret_literals
from domo_core.madetools.launch import SANDBOX_DOWN
from domo_core.madetools.models import HandRun, LaunchStop
from domo_core.madetools.offered import (
    carried_stop,
    outcome_of,
    waits_for_its_yes,
    yes_facts,
    yes_taken,
)
from domo_core.madetools.risks import OperationsNotRight, kept_operations, operations_of
from domo_core.madetools.risks import read_operations as read_declared
from domo_core.madetools.service import fingerprint
from domo_core.madetools.settings import held, told, value
from domo_core.memory.service import refuses_secret_shapes
from domo_core.policy.actions import DescribedAction, FactValue
from domo_core.policy.catalog import MADE_FACTS_WITHOUT_A_YES, PROGRAM_RUN
from domo_core.policy.rights import load_rights
from domo_core.policy.roles import Actor, domo_actor
from domo_core.sandbox_client import SandboxUnavailable
from domo_core.tools.context import InvalidArguments, ToolContext, ToolOutcome, WaitsForAYes

# What the arguments of a program run by hand weigh at most: a trigger's at most five times.
MAX_ARGUMENTS_CHARS: Final = 20_000
# How many findings the model is told at most.
FINDINGS_TOLD: Final = 10

_NOT_RUN: Final = "the program was not run: "
_SECRET: Final = (
    "this looks like a secret: no secret goes in a program or its arguments. Nothing was run. If "
    "it is not a secret, say it differently (in words, without a long code, a long number or a "
    "run of letters and digits)"
)
_NO_SUCH_YES: Final = (
    "no program you ran by hand waits for this yes (it was given already, it is someone else's, "
    "or the program is no longer kept): write it and run it again"
)
_FAILED_CARD: Final[JsonObject] = {"key": "action.card.failed", "params": {}}


def _strings(value: object) -> Iterator[str]:
    """Every text in ``value``: the values and the keys, at any depth."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in cast(dict[object, object], value).items():
            if isinstance(key, str):
                yield key
            yield from _strings(item)
    elif isinstance(value, list):
        for item in cast(list[object], value):
            yield from _strings(item)


class _Given:
    """The arguments of a call that runs a program (not one that carries a yes), checked: its
    form, never what the code does (``_findings``); its code ``code_chars`` at most (the house's
    setting). ``max_seconds`` is ``None`` when the call does not say."""

    _REQUIRED: Final = frozenset({"code", "arguments", "operations"})
    _KEYS: Final = _REQUIRED | {"max_seconds"}

    def __init__(self, arguments: JsonObject, code_chars: int) -> None:
        if not self._REQUIRED <= frozenset(arguments) <= self._KEYS:
            raise InvalidArguments(
                "invalid",
                "give code, arguments and operations (and max_seconds), or yes_for alone",
            )
        code = arguments["code"]
        if not isinstance(code, str) or not code.strip() or "\x00" in code:
            raise InvalidArguments("invalid", "code is the text of a Python program")
        if len(code) > code_chars:
            raise InvalidArguments(
                "too_long",
                f"the code has more than {code_chars} characters; {told('code_chars', code_chars)}",
                setting=("code_chars", code_chars),
            )
        given = arguments["arguments"]
        if not isinstance(given, dict):
            raise InvalidArguments("invalid", "arguments is an object")
        if len(json.dumps(given, ensure_ascii=False)) > MAX_ARGUMENTS_CHARS:
            raise InvalidArguments(
                "too_long", f"the arguments have more than {MAX_ARGUMENTS_CHARS} characters"
            )
        if any(refuses_secret_shapes(text) for text in _strings(given)):
            raise InvalidArguments("not_possible", _SECRET)
        operations = arguments["operations"]
        if not isinstance(operations, dict):
            raise InvalidArguments("invalid", "operations is an object, by name")
        try:
            declared = read_declared(operations) if operations else {}
        except OperationsNotRight as wrong:
            raise InvalidArguments("invalid", str(wrong)) from None
        seconds = arguments.get("max_seconds")
        if seconds is not None and (
            not isinstance(seconds, int) or isinstance(seconds, bool) or seconds < 1
        ):
            raise InvalidArguments("invalid", "max_seconds is a whole number, 1 at least")
        self.code = code
        self.arguments = cast(JsonObject, given)
        self.operations = kept_operations(declared)
        self.max_seconds: int | None = seconds


async def _code_chars(ctx: ToolContext) -> int:
    """How long the code of a program may be: the house's ``code_chars``."""
    async with ctx.session_factory() as session:
        return await value(session, "code_chars")


# Domo's act of setting the house's settings, whose right a time past ``run_seconds`` needs.
_SET_THE_HOUSE: Final = "tools.settings"


async def _time_allowed(ctx: ToolContext, given: _Given) -> None:
    """``InvalidArguments`` (``not_possible``, with the setting) when ``given`` asks more time than
    the house's ``run_seconds`` and the owner does not let Domo change his settings."""
    if given.max_seconds is None:
        return
    async with ctx.session_factory() as session:
        house = await value(session, "run_seconds")
        if given.max_seconds <= house:
            return
        rights = await load_rights(session)
    if rights.resolve(domo_actor(), _SET_THE_HOUSE, ctx.policy).allowed:
        return
    raise InvalidArguments(
        "not_possible",
        f"max_seconds is over your run_seconds, and the owner does not let you change your "
        f"settings; {told('run_seconds', house)}. Give {house} at most, or leave it out",
        setting=("run_seconds", house),
    )


def _findings(code: str) -> list[str]:
    """What the imports check finds in ``code`` (one line each, in order): what it uses beyond
    what a program may, and the lines of its texts that have the shape of a secret."""
    found = forbidden(code)
    found.extend(f"line {line}: a text has the shape of a secret" for line in secret_literals(code))
    return found


def _described(facts: dict[str, FactValue]) -> DescribedAction:
    """What a call does, for the barrier: Domo computes in the household, and what its program
    asks is judged request by request; its facts say the yes it carries, when it carries one."""
    return DescribedAction(
        connector="core",
        action_type=PROGRAM_RUN,
        target=None,
        effect="read",
        destination="household",
        facts=facts,
    )


async def _says(ctx: ToolContext, stop: LaunchStop) -> str | None:
    """What the operation ``stop`` stopped in says, as the program declared it."""
    if stop.hand_run_id is None or stop.operation is None:
        return None
    async with ctx.session_factory() as session:
        run = await session.get(HandRun, stop.hand_run_id)
    if run is None:
        return None
    found = operations_of(run.operations).get(stop.operation)
    return None if found is None else found.says


class ProgramRun:
    """Run a program by hand, at once (see the module docstring)."""

    # Domo's own act, in anyone's turn: judged with his own rights.
    acts_as_domo: Literal[True] = True
    # It waits for its run a while, then hands it over (``handover.launch_in_a_turn``).
    bounds_its_own_steps: Literal[True] = True

    definition = ToolDefinition(
        name="program_run",
        description=(
            "Run a small Python program of your own at once, to do now what none of your tools "
            "does yet, when the person waits for it. Whatever a tool of yours could do, you can "
            "do this way: write the program and run it. It is yours and serves anyone. It runs "
            "apart, with no network, for the person you are talking with (with their rights of "
            "the moment), or for the house when you act on your own. It defines "
            "`def run(entry):` and returns its result, any JSON "
            "value; entry has arguments (what you give), now and for (the name of whom it runs "
            "for). While it runs it may ask the module domo (begin an operation, read and write "
            "your tools' base, launch one of your tools): each request is judged with the rights "
            "of whom it runs for. Declare its operations by name (operations: {name: {says, "
            "risks}}, risks among costs, irreversible, physical, outward; {} when it only reads); "
            "every request but a read follows a begin of one, and one that costs says its amount. "
            "Before it runs, only what it imports and an analyser are checked: a finding runs "
            "nothing. Never a secret in the code or the arguments. You get launch, result and run. "
            "When it stopped for a yes, call this again at once with yes_for alone (the stop's "
            "id): that call is the ask, and waits for the yes. In the same reply tell the person "
            "in your words what is to be done and at what cost; their yes, however they give it, "
            "runs the same program again from the start. On your own for the house, the cost is "
            "yours to judge: your yes_for runs it at once. When "
            "the request will come again, also make the tool from it: tool_build with from_run "
            "(the run). Nobody is told that you do this unless they ask: your words say nothing "
            "of a program or of a tool."
        ),
        parameters={
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": (
                        "The program: Python, defining run(entry); code_chars characters at most "
                        "(tools_settings)."
                    ),
                    "minLength": 1,
                },
                "arguments": {
                    "type": "object",
                    "description": "What the program is given (entry.arguments).",
                },
                "operations": {
                    "type": "object",
                    "description": "Its operations, by name: {says, risks}; {} for none.",
                },
                "max_seconds": {
                    "type": "integer",
                    "description": (
                        "How long it may take, 1 at least; your run_seconds (tools_settings) "
                        "unless it needs more."
                    ),
                    "minimum": 1,
                },
                "yes_for": {
                    "type": "string",
                    "description": "The stop a yes was asked for: alone, to run it again.",
                },
            },
            "additionalProperties": False,
        },
    )
    action_type = PROGRAM_RUN

    async def _yes_of(self, arguments: JsonObject, ctx: ToolContext) -> LaunchStop | None:
        """The stop of a program run by hand whose yes the call carries
        (``offered.carried_stop``)."""
        return await carried_stop(arguments, ctx, tool_id=None, missing=_NO_SUCH_YES)

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        found = await self._yes_of(arguments, ctx)
        if found is None:
            _Given(arguments, await _code_chars(ctx))
            return _described(dict(MADE_FACTS_WITHOUT_A_YES))
        return _described(yes_facts(found))

    async def waits_for(self, arguments: JsonObject, ctx: ToolContext) -> WaitsForAYes | None:
        """What the call waits for when it carries a yes (``offered.waits_for_its_yes``)."""
        found = await self._yes_of(arguments, ctx)
        if found is None:
            return None
        return waits_for_its_yes(found, ctx, await _says(ctx, found), missing=_NO_SUCH_YES)

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        deps = ctx.launch_deps
        if deps is None:
            # Nowhere to run: nothing ran, and Domo does the request another way.
            told: JsonObject = {"launch": "failed", "result": {"reason": SANDBOX_DOWN}}
            return ToolOutcome(status="failed", model_result=told, card=_FAILED_CARD)
        found = await self._yes_of(arguments, ctx)
        yes: LaunchStop | None = None
        if found is not None:
            if found.hand_run_id is None:
                raise InvalidArguments("not_found", _NO_SUCH_YES)
            # The stop's own program and arguments, for whom it stopped, with the yes taken once.
            run_id = found.hand_run_id
            for_person = None if found.for_person_id is None else ctx.person
            yes = await yes_taken(found, ctx, tool_id=None, missing=_NO_SUCH_YES)
        else:
            given = _Given(arguments, await _code_chars(ctx))
            await _time_allowed(ctx, given)
            findings = _findings(given.code)
            if findings:
                raise InvalidArguments("invalid", _NOT_RUN + "; ".join(findings[:FINDINGS_TOLD]))
            try:
                scanned = await deps.sandbox.scan(given.code)
            except SandboxUnavailable:
                told = {"launch": "failed", "result": {"reason": SANDBOX_DOWN}}
                return ToolOutcome(status="failed", model_result=told, card=_FAILED_CARD)
            blocking = [f for f in scanned.findings if f.severity.upper() in BLOCKING]
            if blocking:
                # Their test, severity and line: never the analyser's words, which quote code.
                said = [f"line {f.line}: {f.test_id} ({f.severity.upper()})" for f in blocking]
                raise InvalidArguments("invalid", _NOT_RUN + "; ".join(said[:FINDINGS_TOLD]))
            # For the person of the turn; for the house in a turn Domo acts in on his own.
            for_person = None if ctx.on_his_own else ctx.person
            run_id = await _kept(ctx, given, None if for_person is None else for_person.id)
        launched = await launch_in_a_turn(
            deps, AskingTool.of_run(run_id), arguments={}, for_person=for_person, yes=yes
        )
        outcome = outcome_of(launched)
        return replace(outcome, model_result={**outcome.model_result, "run": str(run_id)})


async def _kept(ctx: ToolContext, given: _Given, for_person_id: uuid.UUID | None) -> uuid.UUID:
    """The program kept (``HandRun``, committed), for ``for_person_id`` (``None``: the house),
    opened in this turn (by nobody in a turn Domo acts in on his own); its id."""
    async with ctx.session_factory() as session:
        # The time its call gave, else the house's ``run_seconds`` of now.
        seconds = given.max_seconds
        if seconds is None:
            seconds = await value(session, "run_seconds")
        run = HandRun(
            code=given.code,
            fingerprint=fingerprint(given.code),
            operations=given.operations,
            arguments=dict(given.arguments),
            opened_by=None if ctx.on_his_own else ctx.person.id,
            for_person_id=for_person_id,
            # Past what the database keeps is as long as it keeps: never reached.
            max_seconds=held(seconds),
            created_at=ctx.clock(),
        )
        session.add(run)
        await session.commit()
        return run.id
