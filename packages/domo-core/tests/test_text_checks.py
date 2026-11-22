"""What the code checks of a text the writer wrote, before the checker reads it (``texts.checks``):
the holes of its meaning, once each and no other brace; plain text (no markup, no web address, no
control character or direction mark); one line; a length counted on the text with its holes. And
for the head of a card: every name and every amount of its facts, its holes, plain text, a length.

The meanings are a small catalog of the tests' own (``catalog``), so that the lengths are known."""

import pytest
from domo_common import meanings
from domo_common.gatekeeper_api import (
    FORM_TEXT_KEYS,
    Binding,
    InjectionLocation,
    SecretRequestCreate,
)
from domo_common.i18n import template
from domo_common.meanings import SIGN
from domo_core.texts.checks import HEAD_MAX_LENGTH, check_head, check_text

BINDING = Binding(
    host="api.example.com", location=InjectionLocation(kind="header", name="x-api-key")
)

# A button whose meaning is short: its longest text is 2 * 11 + 40 = 62 characters.
BUTTON = "test.button"
GREETING = "test.greeting"
INVITED = "test.invited"
PLAIN = "test.plain"
TEST_MEANINGS = {
    BUTTON: "Yes, {name}",
    GREETING: "Welcome back, {name}.",
    INVITED: "{who} invited {name} to the household.",
    PLAIN: "Nothing has been recorded yet.",
}


@pytest.fixture(autouse=True)
def catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(meanings, "_meanings", lambda: TEST_MEANINGS)


def test_a_text_that_keeps_its_holes_passes():
    assert check_text(GREETING, "Bon retour, {name}.") is None
    assert check_text(INVITED, "{name} est invité par {who}.") is None
    assert check_text(PLAIN, "Rien n'a encore été noté.") is None


def test_a_missing_hole_is_refused():
    why = check_text(INVITED, "{who} a invité quelqu'un.")
    assert why is not None and "{name}" in why
    # A hole translated is a hole missing.
    assert check_text(GREETING, "Bon retour, {nom}.") is not None


def test_a_hole_twice_is_refused():
    why = check_text(GREETING, "Bon retour, {name}, oui, {name}.")
    assert why is not None and "{name}" in why


@pytest.mark.parametrize(
    "text",
    [
        "Bon retour, {name} {other}.",
        "Bon retour, {name} {.",
        "Bon retour, {name} }.",
        "Bon retour, {{name}}.",
        "Bon retour, {name}, {}.",
    ],
)
def test_an_unknown_brace_is_refused(text):
    assert check_text(GREETING, text) is not None


def test_a_meaning_without_holes_takes_no_brace():
    assert check_text(PLAIN, "Rien {encore}.") is not None
    assert check_text(PLAIN, "Rien {.") is not None


@pytest.mark.parametrize(
    "text",
    [
        "Rien <b>encore</b>.",
        "Rien > encore.",
        "Rien < encore.",
        "Voir https://example.com",
        "Voir ftp://example.com",
        "Voir www.example.com",
        "Voir WWW.EXAMPLE.COM",
        "Rien‮encore.",
        "Rien‏encore.",
        "Rien\x07encore.",
        "Rien\tencore.",
        "Rien\rencore.",
        "Rien encore.",
    ],
)
def test_markup_and_links_are_refused(text):
    assert check_text(PLAIN, text) is not None


def test_a_second_line_is_refused():
    why = check_text(PLAIN, "Rien n'a été noté.\nPas encore.")
    assert why is not None and "line" in why


def test_an_empty_text_is_refused():
    assert check_text(PLAIN, "") is not None
    assert check_text(PLAIN, "   ") is not None


def test_lengths_are_checked_on_the_text_with_its_holes():
    longest = meanings.max_length(BUTTON)
    assert longest == 2 * len(TEST_MEANINGS[BUTTON]) + 40 == 62
    button = "{name} — OK"
    # Filled with a name of 60 characters, the button would be longer than its meaning allows: it
    # is checked as written, with its hole, and passes.
    assert len(button.replace("{name}", "A" * 60)) > longest
    assert check_text(BUTTON, button) is None
    # A text that is itself too long is refused, and the reason says how long it may be.
    too_long = "{name} " + "x" * (longest - len("{name} ") + 1)
    assert len(too_long) == longest + 1
    why = check_text(BUTTON, too_long)
    assert why is not None and str(longest) in why
    assert check_text(BUTTON, too_long[:-1]) is None


# --- the head of a card -------------------------------------------------------------------------

NAMES = ("Alex", "Robin")
AMOUNTS = ("1250.50", "3")
HEAD = (
    "alex demande d'inviter ROBIN pour 3 jours, pour 1 250,50 €.\n"
    "Le juge dit : {reason}\nTon oui est demandé."
)


def head_problem(text: str, *, holes: tuple[str, ...] = ("reason",)) -> str | None:
    return check_head(text, names=NAMES, amounts=AMOUNTS, holes=holes)


def test_a_head_must_name_everyone_and_every_amount():
    # Names in any case, amounts in digits however they are grouped; a head may hold several lines.
    assert head_problem(HEAD) is None
    without_robin = HEAD.replace("ROBIN", "quelqu'un")
    why = head_problem(without_robin)
    assert why is not None and "Robin" in why
    without_the_amount = HEAD.replace("1 250,50", "mille")
    why = head_problem(without_the_amount)
    assert why is not None and "1250.50" in why
    # Another number is not the amount: 13 is not 3, 1 250,5 is not 1250.50.
    assert head_problem(HEAD.replace("3 jours", "13 jours")) is not None
    assert head_problem(HEAD.replace("1 250,50", "1 250,5")) is not None


def test_a_head_keeps_each_of_its_holes_once_and_is_plain():
    assert head_problem(HEAD.replace("{reason}", "rien")) is not None
    assert head_problem(HEAD + " {reason}") is not None
    assert head_problem(HEAD + " {words}") is not None
    assert head_problem(HEAD + " <script>") is not None
    assert head_problem(HEAD + " https://example.com") is not None
    assert head_problem(HEAD + "‮") is not None
    assert head_problem("   ") is not None


def test_a_head_has_a_longest_length():
    filler = "x" * (HEAD_MAX_LENGTH - len(HEAD) - 1)
    assert HEAD_MAX_LENGTH == 1200
    assert head_problem(f"{HEAD} {filler}") is None
    assert head_problem(f"{HEAD} {filler}x") is not None


@pytest.mark.parametrize(
    "text",
    [
        "Saisissez une clé de 1 à {max} caractères \U0001f469\u200d\U0001f4bb.",
        "Cl\u00e9\u00a0API\u202f: de 1 \u00e0 {max} caract\u00e8res, 20\u202f\u20ac",
        SIGN,
    ],
)
def test_a_form_text_the_checks_accept_the_gatekeeper_accepts(
    text: str, monkeypatch: pytest.MonkeyPatch
):
    # One rule for both (domo_common.text_rules): a text Domo may keep never fails a form.
    form = {key: template(key, "en") for key in FORM_TEXT_KEYS}
    monkeypatch.setattr(meanings, "_meanings", lambda: TEST_MEANINGS | form)
    key = "vault.form.error.length"
    if text != SIGN:
        assert check_text(key, text) is None
    for one in FORM_TEXT_KEYS:
        meant = meanings.meaning(one)
        assert check_text(one, meant) is None, one
        assert SecretRequestCreate(
            ref="r",
            purpose=meant[:200],
            bindings=[BINDING],
            language="fr",
            texts={one: meant, key: text},
        )
