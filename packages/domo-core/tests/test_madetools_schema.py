"""The parameters of a tool Domo made (Task 6, fix round 1): the part of JSON Schema the core
reads (``unsupported`` flags the rest) and the arguments of a call checked against it
(``schema_problem``). Pure: no database."""

from typing import Any

import pytest
from domo_core.madetools.schema import same_json, schema_problem, unsupported

SPLIT: dict[str, Any] = {
    "type": "object",
    "description": "How to split a bill.",
    "properties": {
        "amount": {"type": "number", "minimum": 0, "description": "The total."},
        "people": {"type": "integer", "minimum": 1, "maximum": 20},
        "mode": {"type": "string", "enum": ["equal", "by_share"]},
        "shares": {
            "type": "array",
            "maxItems": 20,
            "items": {
                "type": "object",
                "properties": {"who": {"type": "string", "maxLength": 40}},
                "required": ["who"],
                "additionalProperties": False,
            },
        },
        "flags": {"type": ["boolean", "null"]},
    },
    "required": ["amount", "people"],
    "additionalProperties": False,
}


def test_a_schema_within_the_part_the_core_reads_has_nothing_unsupported():
    assert unsupported(SPLIT) == []
    assert unsupported({"type": "object"}) == []


@pytest.mark.parametrize(
    ("schema", "flagged"),
    [
        (
            {"type": "object", "properties": {"code": {"type": "string", "pattern": "^a"}}},
            "code.pattern",
        ),
        ({"type": "object", "properties": {"x": {"const": 1}}}, "x.const"),
        ({"type": "object", "anyOf": [{"required": ["a"]}]}, "anyOf"),
        ({"type": "object", "oneOf": []}, "oneOf"),
        ({"type": "object", "allOf": []}, "allOf"),
        (
            {"type": "object", "properties": {"n": {"type": "number", "exclusiveMinimum": 0}}},
            "n.exclusiveMinimum",
        ),
        (
            {"type": "object", "properties": {"n": {"type": "number", "multipleOf": 2}}},
            "n.multipleOf",
        ),
        (
            {"type": "object", "properties": {"l": {"type": "array", "uniqueItems": True}}},
            "l.uniqueItems",
        ),
        ({"type": "object", "minProperties": 1}, "minProperties"),
        ({"type": "object", "properties": {"d": {"type": "string", "format": "date"}}}, "d.format"),
        ({"type": "object", "additionalProperties": {"type": "string"}}, "additionalProperties"),
        ({"type": "object", "properties": {"t": {"type": "decimal"}}}, "t.type: decimal"),
        (
            {"type": "object", "properties": {"l": {"type": "array", "items": {"pattern": "x"}}}},
            "l.[].pattern",
        ),
        ({"type": "array"}, "the top level is not of type object"),
        ({"properties": {}}, "the top level is not of type object"),
    ],
)
def test_what_the_core_does_not_read_is_flagged_where_it_is(schema, flagged):
    assert flagged in unsupported(schema)


def _with(keyword: str, value: Any) -> dict[str, Any]:
    return {"type": "object", "properties": {"x": {"type": "string", keyword: value}}}


@pytest.mark.parametrize(
    ("schema", "flagged"),
    [
        ({"type": "object", "required": "amount"}, "required: must be a list of names"),
        ({"type": "object", "required": ["a", 1]}, "required: must be a list of names"),
        (_with("enum", "a"), "x.enum: must be a list"),
        (_with("minLength", "3"), "x.minLength: must be a whole number, 0 or more"),
        (_with("maxLength", -1), "x.maxLength: must be a whole number, 0 or more"),
        (_with("minItems", 1.5), "x.minItems: must be a whole number, 0 or more"),
        (_with("maxItems", True), "x.maxItems: must be a whole number, 0 or more"),
        (_with("minimum", True), "x.minimum: must be a number"),
        (_with("maximum", "9"), "x.maximum: must be a number"),
        (_with("description", 3), "x.description: must be a text"),
        (_with("title", None), "x.title: must be a text"),
        ({"type": "object", "properties": []}, "properties"),
        ({"type": "object", "properties": {"l": {"type": "array", "items": []}}}, "l.[]"),
        (_with("default", "a"), "x.default"),
        (_with("examples", ["a"]), "x.examples"),
    ],
)
def test_a_known_keyword_with_a_value_of_the_wrong_kind_is_flagged(schema, flagged):
    found = unsupported(schema)
    assert any(item.startswith(flagged) for item in found), found


def test_the_values_of_the_known_keywords_that_fit_are_not_flagged():
    schema = {
        "type": "object",
        "title": "Split",
        "properties": {
            "n": {"type": "number", "minimum": 0, "maximum": 2.5},
            "s": {"type": "string", "minLength": 0, "maxLength": 3, "enum": ["a", 1]},
            "l": {"type": "array", "minItems": 0, "maxItems": 4, "items": {"type": "string"}},
        },
        "required": [],
    }
    assert unsupported(schema) == []


@pytest.mark.parametrize(
    ("arguments", "problem"),
    [
        ({"amount": 3, "people": 2}, None),
        ({"amount": 3.5, "people": 2, "mode": "equal", "flags": None}, None),
        ({"amount": 3}, "people is missing"),
        ({"amount": 3, "people": 2, "tip": 1}, "the arguments: the tool takes only"),
        ({"amount": -1, "people": 2}, "amount: too small"),
        ({"amount": 3, "people": 21}, "people: too big"),
        # An integer is no float, even a whole one, and no boolean.
        ({"amount": 3, "people": 2.0}, "people: must be of type integer"),
        ({"amount": 3, "people": True}, "people: must be of type integer"),
        # A number is no boolean.
        ({"amount": True, "people": 2}, "amount: must be of type number"),
        ({"amount": 3, "people": 2, "mode": "random"}, "mode: must be one of the values"),
        ({"amount": 3, "people": 2, "flags": 0}, "flags: must be of type boolean or null"),
        # A nested value is named by its path.
        ({"amount": 3, "people": 2, "shares": [{"who": "x" * 41}]}, "shares[0].who: too long"),
        ({"amount": 3, "people": 2, "shares": [{}]}, "shares[0].who is missing"),
        ({"amount": 3, "people": 2, "shares": [{"who": "a", "n": 1}]}, "shares[0]: the tool takes"),
    ],
)
def test_the_arguments_are_checked_against_the_schema(arguments, problem):
    found = schema_problem(SPLIT, arguments)
    if problem is None:
        assert found is None
    else:
        assert found is not None and problem in found, found


def test_an_enum_compares_by_json_type_and_value():
    schema = {"type": "object", "properties": {"v": {"enum": [1, "a", [1, 2], {"k": False}]}}}
    for fits in (1, 1.0, "a", [1, 2], {"k": False}):
        assert schema_problem(schema, {"v": fits}) is None, fits
    for wrong in (True, "1", [True, 2], {"k": 0}, None):
        assert schema_problem(schema, {"v": wrong}) is not None, wrong
    assert same_json(1, 1.0) and not same_json(True, 1) and not same_json(False, 0)


def test_a_message_never_repeats_a_value():
    found = schema_problem(SPLIT, {"amount": "a secret amount", "people": 2})
    assert found is not None and "secret" not in found
