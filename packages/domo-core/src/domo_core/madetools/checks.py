"""The checks of a version of a tool before its first use (spec 6), all automatic, in this order,
the first that fails ending them (the most costly come last; the report keeps what ran):

1. its parameters are within the part of JSON Schema the core reads (``schema.unsupported``);
2. its code uses only what a tool may (``imports.forbidden``, by ``ast``, never executed), its
   texts and its examples have no shape of a secret, its description is one plain line of
   1 to ``MAX_DESCRIPTION`` characters (``secret_shape``, a finding of this check), and there is
   at least one example to check it against;
3. the analyser, in the sandbox (``Sandbox.scan``): a finding of severity medium or high fails
   (the launcher answers a high ``B000`` when it could not read the code);
4. the dry runs, in the sandbox, on the build's examples: each one is given its example's
   ``entry`` as its arguments, the time of now, for whom it runs (the name of whoever opened the
   build, or none for the house) and that it is a dry run (``"dry_run": true``, which the program
   reads as ``domo.dry_run``), the time limit of the tool but ``dry_run_seconds`` at most (an
   example is quick), and its ``memory_mb`` and ``output_bytes`` (what the sandbox's machine
   cannot give is ``not_here``, said with the setting); it passes when it ends ``done`` with
   what a tool may give back, its result itself (``launch.result_of``: any JSON value of
   ``result_bytes`` at most), equal to the example's ``expected`` as JSON. A dry run keeps
   nothing and is no launch: its program's requests are answered by what answers a real launch
   (``means.Means``), in a dry run, with the operations the tool declares (see
   ``madetools.means``: what is refused for real by its form is refused alike; a ``begin`` is
   let through, an action, a tool, a later launch and a wake do nothing; the reads and writes
   run in one transaction of the tools' base, rolled back when the dry run ends), within the
   bounds of the build's tool (``madetools.settings``: its own, else the house's, else the
   starts; the build reads them and gives them, ``settings``);
5. the review, by another call of a model (the task ``tool_review``, never under ``medium``,
   ``prompts/tool_review.md``): it is given the purpose, the description, the parameters, the
   operations and their risks, and the code as data, and answers ``fine: <reason>`` or
   ``alert: <reason>``; anything else is an alert. No model, a call that fails or takes too long:
   ``unavailable``.

A sandbox that does not answer during 3 or 4 (``SandboxUnavailable``), or a review
``unavailable``, is no answer at all: the report ``waits`` and nothing of it is kept (the build
keeps the submission and checks it again later). ``CheckReport.for_model`` is what the model
that wrote the code reads of it at its next round, and is kept with the version
(``MadeToolVersion.checks``);
``CheckReport.audited`` is what the audit log keeps (results and counts, never the code, a
finding's words or the reviewer's).
"""

import json
from collections.abc import Mapping, Sequence
from contextlib import aclosing
from dataclasses import dataclass, field
from functools import cache
from importlib import resources
from typing import Final, Literal, Protocol, cast

import anyio
from domo_common.sandbox_api import ANSWER_CEILING_BYTES, JsonObject, RunRequest, ScanFinding
from pydantic import JsonValue

from domo_core.llm.levels import Level
from domo_core.llm.tasks import TaskKind
from domo_core.llm.types import ChatChunk, ChatMessage, ChatModel, ModelCallError
from domo_core.madetools.imports import forbidden, secret_literals
from domo_core.madetools.launch import HOUSE, LaunchDeps, NotAResult, not_here, result_of
from domo_core.madetools.means import Chain, LaunchScope, Means, too_many_requests
from domo_core.madetools.risks import Operation, kept_operations
from domo_core.madetools.schema import same_json, unsupported
from domo_core.madetools.settings import starts, wait_for
from domo_core.memory.service import refuses_secret_shapes
from domo_core.policy.roles import domo_actor
from domo_core.sandbox_client import SandboxNotHere, SandboxUnavailable
from domo_core.text import is_plain_line

MAX_DESCRIPTION: Final = 200
# What a reason of the reviewer keeps at most.
MAX_REVIEW_REASON: Final = 300
# The severities of the analyser that fail the check.
BLOCKING: Final = frozenset({"MEDIUM", "HIGH"})
# How much the review may write (it may think first); how long it may take is Domo's
# ``review_seconds``.
REVIEW_OUTPUT_TOKENS: Final = 4000

type Review = Literal["fine", "alert", "unavailable", "not_run"]
type Result = Literal["pass", "fail", "not_run"]

_SECRET: Final = "a text has the shape of a secret (a key, a token, a code): none goes in a tool"
_BAD_OUTPUT: Final = "bad_output"
_NO_EXAMPLE: Final = "there is no example to check it against"


class ReviewModels(Protocol):
    """Where the review's model comes from (``llm.picker.ModelPicker``)."""

    async def model_for(
        self, task: TaskKind, *, at_least: Level | None = None
    ) -> ChatModel | None: ...


@dataclass(frozen=True)
class CheckDeps:
    """What the checks need: what a launch needs (``launch``: the sandbox, the database, the
    clock, the catalog and the tools' base, which answer the dry runs; nobody is woken by them),
    where the review's model comes from (``None``: there is no model), the time a dry run is given
    (ISO, in the household's zone) and the name it is given of whom it runs for (``None``: the
    house)."""

    launch: LaunchDeps
    models: ReviewModels | None
    now: str
    for_name: str | None


@dataclass(frozen=True)
class DryRun:
    """One dry run: the example (from 1), how it ended (a status of the sandbox, or
    ``bad_output`` when what it gave back is not what a tool may), whether its result was the
    expected one, the last line of its error when it stopped on one, and the result it gave
    (``got``: ``None`` unless it ended ``done``; Domo reads it with ``tool_check``, the model that
    writes a tool never)."""

    example: int
    status: str
    matched: bool
    error: str | None = None
    # What it gave, for Domo: not part of what the run was (it compares by its form alone).
    got: JsonValue = field(default=None, compare=False)

    @property
    def passed(self) -> bool:
        return self.status == "done" and self.matched


def _blocking(finding: ScanFinding) -> bool:
    return finding.severity.upper() in BLOCKING


@dataclass
class CheckReport:
    """What the checks said of one version (see the module docstring). A check that did not run
    (an earlier one failed) is ``None`` (``scan``, ``dry_runs``) or ``not_run`` (``review``).
    ``parameters`` and ``imports`` are the findings of the first two checks, one line each."""

    parameters: list[str] = field(default_factory=lambda: [])
    imports: list[str] = field(default_factory=lambda: [])
    scan: list[ScanFinding] | None = None
    dry_runs: list[DryRun] | None = None
    review: Review = "not_run"
    review_reason: str = ""
    sandbox_down: bool = False

    @property
    def waits(self) -> bool:
        """Whether the checks could not be made (the sandbox or the reviewer did not answer):
        nothing of it counts."""
        return self.sandbox_down or self.review == "unavailable"

    @property
    def passed(self) -> bool:
        """No finding in the first two checks, none of severity medium or high from the analyser,
        every dry run ``done`` with the expected result, and the review ``fine``."""
        return (
            not self.parameters
            and not self.imports
            and self.scan is not None
            and not any(_blocking(finding) for finding in self.scan)
            and self.dry_runs is not None
            and all(run.passed for run in self.dry_runs)
            and self.review == "fine"
        )

    def results(self) -> dict[str, Result]:
        """How each of the five checks went."""
        scan_ok = self.scan is not None and not any(_blocking(f) for f in self.scan)
        runs_ok = self.dry_runs is not None and all(run.passed for run in self.dry_runs)
        review: Result = "not_run"
        if self.review in ("fine", "alert"):
            review = "pass" if self.review == "fine" else "fail"
        return {
            "parameters": "fail" if self.parameters else "pass",
            "imports": "not_run" if self.parameters else ("fail" if self.imports else "pass"),
            "scan": "not_run" if self.scan is None else ("pass" if scan_ok else "fail"),
            "dry_runs": "not_run" if self.dry_runs is None else ("pass" if runs_ok else "fail"),
            "review": review,
        }

    def for_model(self) -> JsonObject:
        """What the model that wrote the code reads of the checks: which failed and why, the
        findings of the analyser that fail (their test and line), each dry run's status and
        whether its result matched, the reviewer's reason; never more."""
        told: JsonObject = {"passed": self.passed, "checks": dict(self.results())}
        if self.parameters:
            told["parameters"] = list(self.parameters)
        if self.imports:
            told["imports"] = list(self.imports)
        if self.scan is not None:
            told["scan"] = [
                {"test": f.test_id, "severity": f.severity.upper(), "line": f.line}
                for f in self.scan
                if _blocking(f)
            ]
        if self.dry_runs is not None:
            runs: list[JsonValue] = []
            for run in self.dry_runs:
                shown: JsonObject = {
                    "example": run.example,
                    "status": run.status,
                    "matched": run.matched,
                }
                if run.error is not None:
                    shown["error"] = run.error
                runs.append(shown)
            told["dry_runs"] = runs
        if self.review in ("fine", "alert"):
            told["review"] = self.review
            told["review_reason"] = self.review_reason
        return told

    def audited(self) -> JsonObject:
        """What the audit log keeps of the checks: each one's result and counts, never a finding's
        words, the code or the reviewer's."""
        counts: JsonObject = {
            "parameter_findings": len(self.parameters),
            "import_findings": len(self.imports),
            "scan_findings": 0 if self.scan is None else sum(map(_blocking, self.scan)),
            "dry_runs_made": 0 if self.dry_runs is None else len(self.dry_runs),
            "dry_runs_passed": 0 if self.dry_runs is None else sum(r.passed for r in self.dry_runs),
        }
        return {**self.results(), **counts}


def _description_findings(description: str) -> list[str]:
    if not description.strip() or len(description) > MAX_DESCRIPTION:
        return [f"the description is 1 to {MAX_DESCRIPTION} characters"]
    if not is_plain_line(description):
        return ["the description is one plain line"]
    return []


def _texts_of(value: object) -> list[str]:
    """Every text in ``value``, keys and values, at any depth."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        found: list[str] = []
        for key, item in cast(dict[object, object], value).items():
            if isinstance(key, str):
                found.append(key)
            found.extend(_texts_of(item))
        return found
    if isinstance(value, list):
        return [text for item in cast(list[object], value) for text in _texts_of(item)]
    return []


def _code_findings(code: str, description: str, examples: Sequence[object]) -> list[str]:
    found = forbidden(code)
    found.extend(f"line {line}: {_SECRET}" for line in secret_literals(code))
    found.extend(_description_findings(description))
    if refuses_secret_shapes(description) or any(
        refuses_secret_shapes(text) for text in _texts_of(list(examples))
    ):
        found.append(_SECRET)
    return found


def _schema_of(parameters: object) -> JsonObject:
    """The parameters of a tool as an object (``{}`` when they are not one)."""
    return cast(JsonObject, parameters) if isinstance(parameters, dict) else {}


def _example(item: object) -> tuple[JsonObject, JsonValue]:
    """The arguments and the expected result of an example (``tools.made_tools`` checked its
    shape: ``{entry, expected}``)."""
    example = cast(dict[str, JsonValue], item) if isinstance(item, dict) else {}
    entry = example.get("entry")
    return (cast(JsonObject, entry) if isinstance(entry, dict) else {}), example.get("expected")


async def _dry_run(
    deps: CheckDeps,
    code: str,
    place: int,
    item: object,
    max_seconds: int,
    operations: Mapping[str, Operation],
    name: str | None,
    parameters: JsonObject,
    settings: Mapping[str, int],
) -> DryRun:
    arguments, expected = _example(item)
    request = RunRequest(
        code=code,
        entry={"arguments": arguments, "now": deps.now, "for": deps.for_name, "dry_run": True},
        max_seconds=min(max_seconds, settings["dry_run_seconds"]),
        memory_mb=settings["memory_mb"],
        output_bytes=settings["output_bytes"],
        answer_bytes=min(settings["result_bytes"], ANSWER_CEILING_BYTES),
    )
    # A trial for nobody: as the house, judged by no one's rights, its requests done without
    # effect (``madetools.means``). The tool's own name is its path: it may not launch itself.
    means = Means(
        deps.launch,
        LaunchScope(
            tool_name=None,
            tool_id=None,
            for_whom=HOUSE,
            actor=domo_actor(),
            depth=0,
            chain=Chain(),
            waited=False,
            path=() if name is None else (name,),
            dry_run=True,
            operations=operations,
            parameters=parameters,
            settings=settings,
        ),
    )
    try:
        ran = await deps.launch.sandbox.run(request, answer=means.answer)
    except SandboxNotHere as refused:
        # Its machine cannot give what the tool's settings ask: said with the setting.
        return DryRun(place, "not_here", False, str(not_here(refused, settings)["reason"]))
    finally:
        await means.end()
    if means.too_many:
        # The core stopped it, as it would a launch.
        return DryRun(place, ran.status, False, too_many_requests(settings["requests_per_launch"]))
    if ran.status != "done":
        # The program's words, or else the sandbox's own note on how it ended.
        return DryRun(place, ran.status, False, ran.error if ran.error is not None else ran.note)
    try:
        result = result_of(ran.output, settings["result_bytes"])
    except NotAResult as wrong:
        return DryRun(place, _BAD_OUTPUT, False, str(wrong))
    return DryRun(place, "done", same_json(result, expected), got=result)


async def dry_runs(
    deps: CheckDeps,
    *,
    code: str,
    examples: Sequence[object],
    max_seconds: int,
    operations: Mapping[str, Operation],
    name: str | None,
    parameters: JsonObject,
    settings: Mapping[str, int],
) -> list[DryRun]:
    """The dry runs of ``code`` on ``examples`` (``{entry, expected}``), one after the other (see
    the module docstring, check 4): nothing of them is kept. Raises ``SandboxUnavailable`` when the
    sandbox does not answer. ``tools.made_tools``'s ``tool_check`` runs them on a tool's version in
    force."""
    return [
        await _dry_run(deps, code, place, item, max_seconds, operations, name, parameters, settings)
        for place, item in enumerate(examples, start=1)
    ]


@cache
def review_prompt() -> str:
    """The system prompt of the review (``prompts/tool_review.md``), read once."""
    path = resources.files("domo_core.prompts").joinpath("tool_review.md")
    return path.read_text(encoding="utf-8").strip()


def read_review(text: str) -> tuple[Literal["fine", "alert"], str]:
    """The reviewer's answer: ``fine`` only when it begins with ``fine:`` (the word in any case),
    with its reason; anything else is an ``alert``, with the reason it gave after ``alert:``, or
    else that it answered nothing readable. The reason is one plain line, ``MAX_REVIEW_REASON``
    characters at most."""
    lines = text.strip().splitlines()
    first = lines[0].strip() if lines else ""
    word, colon, reason = first.partition(":")
    reason = reason.strip()[:MAX_REVIEW_REASON].rstrip()
    if not is_plain_line(reason):
        reason = ""
    if colon and word.strip().casefold() == "fine":
        return "fine", reason
    if colon and word.strip().casefold() == "alert":
        return "alert", reason or "the reviewer gave no reason"
    return "alert", "the reviewer's answer was neither fine nor alert"


async def _review(
    models: ReviewModels | None,
    *,
    purpose: str,
    code: str,
    description: str,
    parameters: object,
    operations: Mapping[str, Operation],
    seconds: int,
) -> tuple[Review, str]:
    """The review of a code by another call of a model (see the module docstring), ``seconds`` at
    most (Domo's ``review_seconds``)."""
    model = None if models is None else await models.model_for("tool_review")
    if model is None:
        return "unavailable", ""
    case = {
        "purpose": purpose,
        "description": description,
        "parameters": parameters,
        "operations": kept_operations(operations),
        "code": code,
    }
    pieces: list[str] = []
    with anyio.move_on_after(wait_for(seconds)):
        try:
            stream = model.stream(
                system=review_prompt(),
                messages=[ChatMessage("user", json.dumps(case, ensure_ascii=False))],
                max_output_tokens=REVIEW_OUTPUT_TOKENS,
            )
            async with aclosing(stream) as items:
                async for item in items:
                    if isinstance(item, ChatChunk):
                        pieces.append(item.text)
        except ModelCallError:
            return "unavailable", ""
        return read_review("".join(pieces))
    return "unavailable", ""


async def check(
    deps: CheckDeps,
    *,
    code: str,
    description: str,
    parameters: object,
    examples: Sequence[object],
    purpose: str = "",
    max_seconds: int | None = None,
    operations: Mapping[str, Operation] | None = None,
    name: str | None = None,
    settings: Mapping[str, int] | None = None,
) -> CheckReport:
    """The five checks of a version (see the module docstring), in order, the first that fails
    ending them. ``purpose`` is what the tool must do (for the reviewer), ``max_seconds`` the time
    of a launch of the tool (a dry run takes ``dry_run_seconds`` at most; ``None``: the
    ``run_seconds`` of ``settings``), ``operations`` those the tool declares (a ``begin`` of any
    other is refused, as it would be when it runs), ``name`` the tool's (a dry run that launches it
    is refused, as it would be), ``settings`` the bounds of the dry runs (``None``: the starts).
    It never raises for what the sandbox or the model do: a sandbox that does not answer is
    ``sandbox_down``, a reviewer that does not, ``unavailable``."""
    bounds = starts() if settings is None else settings
    seconds = bounds["run_seconds"] if max_seconds is None else max_seconds
    declared = operations or {}
    schema = _schema_of(parameters)
    report = CheckReport(parameters=unsupported(parameters))
    if report.parameters:
        return report
    report.imports = _code_findings(code, description, examples)
    if not examples:
        report.imports.append(_NO_EXAMPLE)
    if report.imports:
        return report
    try:
        report.scan = list((await deps.launch.sandbox.scan(code)).findings)
        if any(_blocking(finding) for finding in report.scan):
            return report
        report.dry_runs = await dry_runs(
            deps,
            code=code,
            examples=examples,
            max_seconds=seconds,
            operations=declared,
            name=name,
            parameters=schema,
            settings=bounds,
        )
    except SandboxUnavailable:
        report.sandbox_down = True
        return report
    if not all(run.passed for run in report.dry_runs):
        return report
    report.review, report.review_reason = await _review(
        deps.models,
        purpose=purpose,
        code=code,
        description=description,
        parameters=parameters,
        operations=declared,
        seconds=bounds["review_seconds"],
    )
    return report
