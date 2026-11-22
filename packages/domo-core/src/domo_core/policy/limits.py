"""Limits written in CEL (spec 5.5): compiled, checked and evaluated here, and nowhere else.

A limit is an expression of the Common Expression Language that reads the facts of the moment
(``policy.facts``: ``actor``, ``action``, ``now``, ``household``, ``history``, ``spent``) and
answers one of
four words: ``"allow"``, ``"refuse"``, ``"ask_owner"`` or ``"judge"``. CEL only computes a value
from what it is given: no file, no network, no side effect. An expression is data that the
interpreter evaluates, not code that the core runs (spec 3).

The library
-----------

``common-expression-language`` (imported as ``cel``), a Python binding of the CEL interpreter
written in Rust (``cel-rust``), under the Apache-2.0 license. It was chosen over ``cel-python``
(pure Python) because:

* its license is declared plainly (``License-Expression: Apache-2.0``, and the OSI classifier), so
  ``scripts/check_licenses.py`` accepts it without an exception; ``cel-python``'s package declares
  no license at all, which the check reads as unknown;
* it ships wheels for Windows and Linux (CPython 3.12), published before the dependency cooldown;
* it has what a limit needs: the ternary operator, strings, integers, booleans, nested maps,
  ``null``, ``in``, ``has()``; it refuses to mix types (``1 + 2u``) rather than guess; it reports
  an overflow and a missing key as errors; its parser bounds how deep an expression nests, and its
  regular expressions are linear in time and capped in size;
* it compiles an expression once (``cel.compile``) and says which variables and functions it
  uses (``Program.variables``, ``Program.functions``), which the checks below read.

It has no budget of cost or time, and it holds the interpreter's lock while it evaluates, so a
limit that ran for long would stop the whole core. What could make an evaluation long is a loop:
CEL has no unbounded one, but its macros that go through a list (``all``, ``exists``,
``exists_one``, ``map``, ``filter``) nest, and a few of them over short lists already take
minutes. The facts hold no list, so a limit never needs them: an expression in which one of these
words appears as a name (even in a string or a comment: refusing too much is safe) does not
compile here. What is left runs in a time that grows with the length of the expression, which is
``MAX_EXPRESSION_LENGTH`` characters at most. For the same reason only the functions a limit needs
are accepted (comparisons, arithmetics, ``size``, ``contains``, ``startsWith``, ``endsWith``,
``matches``, the conversions), and only the six roots of the facts may be read.

Failing closed
--------------

``compile_limit`` raises ``InvalidLimit`` for whatever it does not accept; ``evaluate_limit``
raises ``LimitError`` for whatever goes wrong while evaluating (a missing fact, a type CEL does not
compare, an overflow, an answer that is not one of the four). The barrier turns both into a
refusal (``limit_error``): a limit that cannot say is never read as a yes. ``check_limit`` is the
test a limit passes before it may be stored (spec 5.6): it compiles, and it answers on made-up
facts that have every fact of its action type, and again with each number among them missing
(``null``: it does not apply, as a guest's stay for anyone but a guest), so that a limit that
reads a number without a guard against a missing one is sent back rather than stored, to refuse
later every request where it does not apply.

This module is the only one that imports the library.
"""

import functools
import itertools
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Final, Literal, get_args

import cel

from domo_core.policy.catalog import ACTION_FACTS, NO_KINDS, FactType, OpenKind, kind_facts
from domo_core.policy.roles import ROLES

LimitOutcome = Literal["allow", "refuse", "ask_owner", "judge"]
LIMIT_OUTCOMES: Final[tuple[LimitOutcome, ...]] = get_args(LimitOutcome)

# The longest expression, in characters (the ``policy_rights`` table says the same).
MAX_EXPRESSION_LENGTH: Final = 2000

# What an expression may read: the facts of the moment (``policy.facts``).
FACT_ROOTS: Final = frozenset({"actor", "action", "now", "household", "history", "spent"})
# The fact that only exists when the household's time zone is set.
_NOW: Final = "now"

# The functions and operators an expression may use, as CEL names them.
_FUNCTIONS: Final = frozenset(
    {
        "_==_",
        "_!=_",
        "_<_",
        "_<=_",
        "_>_",
        "_>=_",
        "_&&_",
        "_||_",
        "!_",
        "_?_:_",
        "-_",
        "_+_",
        "_-_",
        "_*_",
        "_/_",
        "_%_",
        "_[_]",
        "@in",
        "size",
        "contains",
        "startsWith",
        "endsWith",
        "matches",
        "int",
        "uint",
        "double",
        "string",
    }
)

# The macros that go through a list. A name is a run of these characters in CEL, whatever is
# around it (a space, a comment), so the word alone is enough to find one.
_LOOPS: Final = re.compile(
    r"(?<![A-Za-z0-9_])(?:all|exists|exists_one|existsOne|map|filter)(?![A-Za-z0-9_])"
)

# How many compiled expressions are kept: the rights in force are a few dozen at most.
_CACHED_EXPRESSIONS: Final = 512


class InvalidLimit(Exception):
    """An expression that is not a limit: too long, not CEL, or reading or using what a limit may
    not (``compile_limit``); or one that fails on facts of its action type (``check_limit``). The
    message says why in plain words, for the one who writes limits."""


class LimitError(Exception):
    """A limit could not answer: its evaluation failed, or it answered something else than the
    four words."""


@dataclass(frozen=True)
class CompiledLimit:
    """An expression that ``compile_limit`` accepted, ready to be evaluated. ``roots`` are the
    roots of the facts it reads (among ``actor``, ``action``, ``now``, ``household``,
    ``history``, ``spent``)."""

    expression: str
    program: cel.Program = field(repr=False, compare=False)
    roots: frozenset[str] = frozenset()


def _is_panic(error: BaseException) -> bool:
    """Whether ``error`` is a panic of the interpreter: pyo3's ``PanicException``, which is not an
    ``Exception`` (and cannot be imported by name). It fails the limit, as any error does."""
    return type(error).__name__ == "PanicException"


def compile_limit(expression: str) -> CompiledLimit:
    """``expression`` compiled, once (the result is kept, by expression). ``InvalidLimit`` when it
    is longer than ``MAX_EXPRESSION_LENGTH``, does not compile, reads something else than the
    facts of the moment, uses a function a limit may not use or goes through a list (see the
    module docstring)."""
    if len(expression) > MAX_EXPRESSION_LENGTH:
        raise InvalidLimit(f"a limit has {MAX_EXPRESSION_LENGTH} characters at most")
    return _compiled(expression)


@functools.lru_cache(maxsize=_CACHED_EXPRESSIONS)
def _compiled(expression: str) -> CompiledLimit:
    if _LOOPS.search(expression) is not None:
        raise InvalidLimit(
            "a limit does not go through a list (all, exists, exists_one, map, filter): the "
            "facts hold none, and none of these words may appear in it"
        )
    try:
        program = cel.compile(expression)
        variables = set(program.variables())
        functions = set(program.functions())
    except Exception as error:
        raise InvalidLimit(f"the limit is not CEL: {error}") from None
    except BaseException as error:
        if not _is_panic(error):
            raise
        raise InvalidLimit("the limit is not CEL (the interpreter stopped)") from None
    unknown = variables - FACT_ROOTS
    if unknown:
        raise InvalidLimit(
            f"a limit reads only {', '.join(sorted(FACT_ROOTS))}, not {', '.join(sorted(unknown))}"
        )
    refused = functions - _FUNCTIONS
    if refused:
        raise InvalidLimit(f"a limit may not use {', '.join(sorted(refused))}")
    return CompiledLimit(expression, program, frozenset(variables))


def evaluate_limit(limit: CompiledLimit, facts: Mapping[str, object]) -> LimitOutcome:
    """What ``limit`` answers on ``facts`` (the facts of the moment, ``policy.facts``).
    ``LimitError`` when it fails, or answers something else than the four words."""
    try:
        result: object = limit.program.execute(dict(facts))
    except Exception as error:
        raise LimitError(f"the limit failed ({type(error).__name__}): {error}") from None
    except BaseException as error:
        if not _is_panic(error):
            raise
        raise LimitError("the limit failed (the interpreter stopped)") from None
    for outcome in LIMIT_OUTCOMES:
        if isinstance(result, str) and result == outcome:
            return outcome
    raise LimitError('a limit answers "allow", "refuse", "ask_owner" or "judge"')


# A made-up value of each kind of fact, to try a limit on (``check_limit``).
_SAMPLE_PERSON: Final = "person:00000000-0000-0000-0000-000000000000"
_SAMPLES: Final[Mapping[FactType, object]] = {
    "text": "sample",
    "number": 1,
    "boolean": False,
    "person": _SAMPLE_PERSON,
    "role": "guest",
    "date": "2030-01-07",
}


# A number is ``null`` when it does not apply (``catalog.ACTION_FACTS``). For a number that applies
# only with one value of another fact, that fact and that value: the days of a stay are a guest's
# (with the action's ``role`` ``guest``, they are always given).
_APPLIES_ONLY_WITH: Final[Mapping[str, tuple[str, str]]] = {"days": ("role", "guest")}


def _declared(action_type: str, kinds: Mapping[str, OpenKind]) -> Mapping[str, FactType]:
    """The facts of ``action_type``: the code's (``catalog.ACTION_FACTS``), else those of its
    open kind among ``kinds``. ``KeyError`` for a type neither knows."""
    declared = ACTION_FACTS.get(action_type)
    if declared is not None:
        return declared
    return kind_facts(kinds[action_type])


def sample_facts(
    action_type: str,
    *,
    time_zone_set: bool,
    action_facts: Mapping[str, object] | None = None,
    kinds: Mapping[str, OpenKind] = NO_KINDS,
) -> dict[str, object]:
    """Made-up facts of the moment for ``action_type``: every fact of its kind
    (``catalog.ACTION_FACTS``, or its open kind among ``kinds``), and ``now`` only when the
    household's time zone is set. ``action_facts`` replace some of the made-up facts of the
    action. ``KeyError`` for an action type neither knows."""
    declared = _declared(action_type, kinds)
    of_the_action: dict[str, object] = {name: _SAMPLES[kind] for name, kind in declared.items()}
    of_the_action.update(action_facts or {})
    facts: dict[str, object] = {
        "actor": {"id": _SAMPLE_PERSON, "role": "adult", "name": "Sample"},
        "action": {
            "type": action_type,
            "target": None,
            "facts": of_the_action,
        },
        "household": {
            "people": {role: 1 for role in ROLES},
            "invitations": {role: 0 for role in ROLES},
        },
        "history": {"day": 0, "week": 0, "month": 0},
        "spent": {"day": 0, "week": 0, "month": 0},
    }
    if time_zone_set:
        facts[_NOW] = {"hour": 12, "minute": 0, "weekday": 1, "date": "2030-01-07"}
    return facts


def _missing_numbers(
    action_type: str, kinds: Mapping[str, OpenKind]
) -> Iterator[tuple[str, dict[str, object]]]:
    """For each number fact of ``action_type``, facts of the action with that one missing
    (``None``) and its facts of kind role taking every value they may have then: every role, but
    the one with which the number always applies (``_APPLIES_ONLY_WITH``)."""
    declared = _declared(action_type, kinds)
    roles = [name for name, kind in declared.items() if kind == "role"]
    for number in (name for name, kind in declared.items() if kind == "number"):
        applies_with = _APPLIES_ONLY_WITH.get(number)
        for chosen in itertools.product(ROLES, repeat=len(roles)):
            by_name: dict[str, object] = dict(zip(roles, chosen, strict=True))
            if applies_with is not None and by_name.get(applies_with[0]) == applies_with[1]:
                continue
            yield number, {**by_name, number: None}


def check_limit(
    expression: str,
    action_type: str,
    *,
    time_zone_set: bool,
    kinds: Mapping[str, OpenKind] = NO_KINDS,
) -> None:
    """``InvalidLimit`` unless ``expression`` may be a limit on ``action_type``: a type of the
    catalog, of the code or one of the open ``kinds`` (``PolicyTables.kinds``) with facts to read
    (an open kind without any takes no limit); it compiles (``compile_limit``), it reads the hour
    or the day only when the household's time zone is set (``time_zone_set``: Domo asks the
    owner for it first, it is never guessed), and it answers one of the four words on made-up
    facts that have every fact of the action type (``sample_facts``): a fact the action does not
    have fails there. It answers too with each number of the action missing (``null``: it does
    not apply), whatever the roles of the action then (``_missing_numbers``): a limit that reads a
    number without checking that it is there would refuse every such request (``limit_error``)."""
    compiled = compile_limit(expression)
    if action_type not in ACTION_FACTS:
        kind = kinds.get(action_type)
        if kind is None:
            raise InvalidLimit("a limit is about an action type of the catalog")
        if not kind_facts(kind):
            raise InvalidLimit(f"{action_type} carries no fact: it takes no limit")
    if not time_zone_set and _NOW in compiled.roots:
        raise InvalidLimit(
            "the limit reads the hour or the day: the household's time zone must be set first"
        )
    try:
        evaluate_limit(
            compiled, sample_facts(action_type, time_zone_set=time_zone_set, kinds=kinds)
        )
    except LimitError as error:
        raise InvalidLimit(
            f"the limit does not work on the facts of {action_type}: {error}"
        ) from None
    for number, missing in _missing_numbers(action_type, kinds):
        facts = sample_facts(
            action_type, time_zone_set=time_zone_set, action_facts=missing, kinds=kinds
        )
        try:
            evaluate_limit(compiled, facts)
        except LimitError as error:
            raise InvalidLimit(
                f"the limit fails when action.facts.{number} is null (it does not apply, as a "
                "guest's stay for anyone else): read it only when it is there, as in "
                f"action.facts.{number} != null && ...: {error}"
            ) from None
