"""The parameters of a tool Domo made: the part of JSON Schema the core reads, and nothing else.

A tool's ``parameters`` is a JSON Schema whose top level is ``{"type": "object", ...}``, written
with the words of ``KEYWORDS`` alone (and the type names of ``TYPES``); ``additionalProperties`` is
absent or a boolean. ``unsupported`` lists what a schema holds beyond that: a tool whose parameters
it flags is never stored (the build refuses it), and one met at a launch is the tool's fault
(``madetools.launch``), never the model's. ``schema_problem`` says what is wrong with the arguments
of a call for a schema of that part: a mistake of the model's form, in words that never repeat a
value of the arguments.
"""

from collections.abc import Callable, Mapping, Sequence
from typing import Final, cast

# What constrains a value, and what only describes it.
_CONSTRAINTS: Final = frozenset(
    {
        "type",
        "enum",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "minItems",
        "maxItems",
    }
)
_ANNOTATIONS: Final = frozenset({"description", "title"})
KEYWORDS: Final = _CONSTRAINTS | _ANNOTATIONS
TYPES: Final = frozenset({"object", "array", "string", "integer", "number", "boolean", "null"})


def _listed(value: object) -> list[object] | None:
    return cast(list[object], value) if isinstance(value, list) else None


def _mapped(value: object) -> Mapping[str, object] | None:
    return cast(Mapping[str, object], value) if isinstance(value, dict) else None


def _at(path: str, name: str) -> str:
    return f"{path}.{name}" if path else name


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


# What the value of a keyword must be, for the keywords whose value is not a schema of its own.
_VALUE_RULES: Final[Mapping[str, tuple[Callable[[object], bool], str]]] = {
    "required": (
        lambda v: isinstance(v, list) and all(isinstance(n, str) for n in cast(list[object], v)),
        "a list of names",
    ),
    "enum": (lambda v: isinstance(v, list), "a list"),
    "minLength": (_is_count, "a whole number, 0 or more"),
    "maxLength": (_is_count, "a whole number, 0 or more"),
    "minItems": (_is_count, "a whole number, 0 or more"),
    "maxItems": (_is_count, "a whole number, 0 or more"),
    "minimum": (lambda v: isinstance(v, int | float) and not isinstance(v, bool), "a number"),
    "maximum": (lambda v: isinstance(v, int | float) and not isinstance(v, bool), "a number"),
    "description": (lambda v: isinstance(v, str), "a text"),
    "title": (lambda v: isinstance(v, str), "a text"),
}


def _unsupported_in(schema: object, path: str) -> list[str]:
    rules = _mapped(schema)
    if rules is None:
        return [f"{path or 'the schema'}: not an object"]
    found: list[str] = []
    for keyword, value in rules.items():
        where = _at(path, keyword)
        if keyword not in KEYWORDS:
            found.append(where)
        elif keyword in _VALUE_RULES:
            fits, what = _VALUE_RULES[keyword]
            if not fits(value):
                found.append(f"{where}: must be {what}")
        elif keyword == "type":
            names = [value] if isinstance(value, str) else _listed(value)
            if names is None or not names:
                found.append(where)
            else:
                found.extend(
                    f"{where}: {name}"
                    for name in names
                    if not isinstance(name, str) or name not in TYPES
                )
        elif keyword == "additionalProperties" and not isinstance(value, bool):
            found.append(where)
        elif keyword == "properties":
            properties = _mapped(value)
            if properties is None:
                found.append(where)
            else:
                for name, inner in properties.items():
                    found.extend(_unsupported_in(inner, _at(path, name)))
        elif keyword == "items":
            found.extend(_unsupported_in(value, _at(path, "[]")))
    return found


def unsupported(parameters: object) -> list[str]:
    """Each keyword or type name of ``parameters`` that ``schema_problem`` does not read (with
    where it is, nested ``properties`` and ``items`` included), a known keyword whose value is not
    of its kind (``required`` a list of names, ``enum`` a list, the lengths and counts whole
    numbers from 0, ``minimum`` and ``maximum`` numbers, ``properties`` and ``items`` objects,
    ``description`` and ``title`` texts), and an ``additionalProperties`` that is no boolean; the
    top level must be ``{"type": "object"}``. Empty: the schema is within the part the core
    reads."""
    found = _unsupported_in(parameters, "")
    rules = _mapped(parameters)
    if rules is not None and rules.get("type") != "object":
        found.insert(0, "the top level is not of type object")
    return found


def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


_CHECKS: Final[Mapping[str, Callable[[object], bool]]] = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": _is_integer,
    "number": _is_number,
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def _json_kind(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if _is_number(value):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def same_json(left: object, right: object) -> bool:
    """Whether two JSON values are equal as JSON: of the same type (``true`` is no ``1``) and
    value (``1`` and ``1.0`` are one number), arrays and objects item by item."""
    kind = _json_kind(left)
    if kind != _json_kind(right):
        return False
    if kind == "array":
        items, others = cast(list[object], left), cast(list[object], right)
        return len(items) == len(others) and all(map(same_json, items, others))
    if kind == "object":
        mapped, others_mapped = cast(Mapping[str, object], left), cast(Mapping[str, object], right)
        return mapped.keys() == others_mapped.keys() and all(
            same_json(item, others_mapped[name]) for name, item in mapped.items()
        )
    return left == right


def _problem(schema: object, value: object, path: str) -> str | None:
    rules = _mapped(schema)
    if rules is None:
        return None
    where = path or "the arguments"
    expected = rules.get("type")
    names: Sequence[object] | None = [expected] if isinstance(expected, str) else _listed(expected)
    if names is not None and not any(
        isinstance(name, str) and name in _CHECKS and _CHECKS[name](value) for name in names
    ):
        return f"{where}: must be of type {' or '.join(str(name) for name in names)}"
    enum = _listed(rules.get("enum"))
    if enum is not None and not any(same_json(value, allowed) for allowed in enum):
        return f"{where}: must be one of the values the tool lists"
    if isinstance(value, str):
        least, most = rules.get("minLength"), rules.get("maxLength")
        if _is_integer(least) and len(value) < cast(int, least):
            return f"{where}: too short"
        if _is_integer(most) and len(value) > cast(int, most):
            return f"{where}: too long"
    if _is_number(value):
        number = cast(float, value)
        least, most = rules.get("minimum"), rules.get("maximum")
        if _is_number(least) and number < cast(float, least):
            return f"{where}: too small"
        if _is_number(most) and number > cast(float, most):
            return f"{where}: too big"
    items = _listed(value)
    if items is not None:
        least, most = rules.get("minItems"), rules.get("maxItems")
        if _is_integer(least) and len(items) < cast(int, least):
            return f"{where}: too few items"
        if _is_integer(most) and len(items) > cast(int, most):
            return f"{where}: too many items"
        for index, item in enumerate(items):
            problem = _problem(
                rules.get("items"), item, f"{path}[{index}]" if path else f"[{index}]"
            )
            if problem is not None:
                return problem
    given = _mapped(value)
    if given is not None:
        known = _mapped(rules.get("properties")) or {}
        for name in _listed(rules.get("required")) or []:
            if isinstance(name, str) and name not in given:
                return f"{_at(path, name)} is missing"
        for name, item in given.items():
            if name in known:
                problem = _problem(known[name], item, _at(path, name))
                if problem is not None:
                    return problem
            elif rules.get("additionalProperties") is False:
                return f"{where}: the tool takes only {', '.join(sorted(known))}"
    return None


def schema_problem(parameters: object, arguments: object) -> str | None:
    """What is wrong with ``arguments`` for the schema ``parameters`` (which ``unsupported`` finds
    nothing in), with the path of the value at fault, never the value; ``None`` when they fit."""
    return _problem(parameters, arguments, "")
