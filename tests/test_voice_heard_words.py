"""How the voice trials compare what was said with what a hearing model wrote
(``tests/e2e/heard_words.py``): hours and numbers written in figures are the words said, and each
hearing model has its own bound of misheard sentences."""

import pytest

from tests.e2e.heard_words import heard_back_said, in_words, misheard_at_most


@pytest.mark.parametrize(
    ("written", "language", "said"),
    [
        ("vers 9h00 ?", "fr", "vers neuf heures ?"),
        ("vers 9h?", "fr", "vers neuf heures?"),
        ("vers 9 h", "fr", "vers neuf heures"),
        ("à 21h30", "fr", "à vingt et une heures trente"),
        ("à 1h05", "fr", "à une heure cinq"),
        ("et 3 pommes", "fr", "et trois pommes"),
        ("71 ans", "fr", "soixante et onze ans"),
        ("92", "fr", "quatre-vingt-douze"),
        ("at 9:00", "en", "at nine"),
        ("at 7:45", "en", "at seven forty-five"),
        ("3 apples", "en", "three apples"),
        ("no figure here", "en", "no figure here"),
    ],
)
def test_figures_are_written_as_the_words_said(written: str, language: str, said: str) -> None:
    assert in_words(written, language) == said


def test_an_hour_in_figures_is_heard_back_whole() -> None:
    said = "Peux-tu me rappeler d'appeler le garage demain matin vers neuf heures ?"
    heard = "Peut-tu me rappeler d'appeler le garage demain matin vers 9h00 ?"
    # Only "peux" is missed, as a person would count it.
    assert heard_back_said(said, heard, "fr") == pytest.approx(12 / 13)


def test_a_wrong_word_is_still_missed() -> None:
    said = "Bonjour, peux-tu allumer la lumière du salon, s'il te plaît ?"
    heard = "Bonjour, peu tue allumée la lumière du salon s'il te plaît."
    assert heard_back_said(said, heard, "fr") < 0.8


def test_the_smallest_model_has_the_widest_bound_and_the_others_are_stricter() -> None:
    base = misheard_at_most("faster-whisper-base", 77)
    small = misheard_at_most("faster-whisper-small", 77)
    turbo = misheard_at_most("faster-whisper-large-v3-turbo", 77)
    assert base == 7 and small == 2 and turbo == 0
    # A model the trials never measured is held to none.
    assert misheard_at_most("another-hearing-model", 77) == 0
