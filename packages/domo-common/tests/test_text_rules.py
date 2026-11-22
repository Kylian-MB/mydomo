"""The rules of a text people read that the core and the gatekeeper share
(``domo_common.text_rules``): what the core's checks accept, the gatekeeper accepts."""

import pytest
from domo_common.text_rules import (
    HOLE,
    LANGUAGE_TAG,
    LANGUAGE_TAG_MAX,
    is_plain_line,
    language_tag,
    plain_problem,
)


@pytest.mark.parametrize(
    "text",
    [
        "Plain words, with numbers 1 250,50 and a name: Robin.",
        "Emoji with a joiner: \U0001f469\u200d\U0001f4bb",
        "A no-break\u00a0space and a narrow one\u202f",
        "Saisissez une valeur de 1 à {max} caractères.",
    ],
)
def test_a_plain_text_has_no_problem(text: str):
    assert plain_problem(text) is None and is_plain_line(text)


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("<b>bold</b>", "plain text only: no < or >"),
        ("1 > 0", "plain text only: no < or >"),
        ("see https://example.org", "no web address"),
        ("see WWW.example.org", "no web address"),
        ("bell\x07", "no control character and no direction mark"),
        ("\u202eesrever", "no control character and no direction mark"),
        ("a\u2028b", "no control character and no direction mark"),
    ],
)
def test_what_is_not_plain(text: str, why: str):
    assert plain_problem(text) == why


def test_several_lines_are_plain_but_not_a_plain_line():
    assert plain_problem("one\ntwo") is None
    assert not is_plain_line("one\ntwo")


def test_holes_and_language_tags():
    assert HOLE.findall("{when}: {who} kept {name}, {{not}} {9no}") == [
        "when",
        "who",
        "name",
        "not",
    ]
    for tag in ("en", "fr-CA", "zh-Hant-TW", "es-419", "und"):
        assert LANGUAGE_TAG.fullmatch(tag), tag
    for tag in ("French", "en_US", "e", "en-<b>", "en-U\u212a", "fr-CA-x-private"):
        assert not LANGUAGE_TAG.fullmatch(tag), tag


def test_a_language_tag_is_taken_in_its_usual_case():
    # The language in lower case, the script capitalized, the region in upper case.
    assert language_tag("fr") == "fr"
    assert language_tag("PT-br") == "pt-BR"
    assert language_tag("zh-hant-tw") == "zh-Hant-TW"
    assert language_tag("es-419") == "es-419"
    for text in ("French", "en_US", "", " fr", "fr ", "en-U\u212a", "fr-CA-x-private", "x" * 40):
        assert language_tag(text) is None, text
    # The longest: a language of three letters, a script and a region of three digits.
    assert len("abc-Abcd-123") == LANGUAGE_TAG_MAX
    assert language_tag("ABC-abcd-123") == "abc-Abcd-123"
