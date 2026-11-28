"""How the voice trials (``test_voice.py``) compare what was said with what a hearing model wrote.

The set says its hours and numbers in words ("neuf heures"); a model may write them in figures
("9h00", "9h", "3 pommes"), which the plain count of ``domo_speech.measure.heard_back`` takes for
misses. ``in_words`` writes the figures of a French or an English text as the words said, and
``heard_back_said`` counts once both are in words. Nothing else is forgiven: a wrong word is
still missed.

Each hearing model has its own bound of sentences it may mishear (``misheard_at_most``): the
smallest one that holds on a processor from what it was measured to miss in the trials, the
larger ones stricter, and a model never measured none at all, so that a real regression still
fails.
"""

import math
import re
from typing import Final

from domo_speech.measure import heard_back

# The share of the trial's sentences each hearing model may mishear (a wrong language, or less
# than 0.8 of the words heard back). base: 6 of 77 measured on the home server's processor once
# hours and numbers are in words (9 of 77 counted before, 2026-11-28, build 823), so at most 10%.
# small: 1 of 76 on the PC's card (2026-11-26). turbo: none.
MISHEARD_SHARE: Final[dict[str, float]] = {
    "faster-whisper-base": 0.10,
    "faster-whisper-small": 0.03,
    "faster-whisper-large-v3-turbo": 0.0,
}

_FR_UNITS: Final = [
    "zéro",
    "un",
    "deux",
    "trois",
    "quatre",
    "cinq",
    "six",
    "sept",
    "huit",
    "neuf",
    "dix",
    "onze",
    "douze",
    "treize",
    "quatorze",
    "quinze",
    "seize",
    "dix-sept",
    "dix-huit",
    "dix-neuf",
]
_FR_TENS: Final = {2: "vingt", 3: "trente", 4: "quarante", 5: "cinquante", 6: "soixante"}
_EN_UNITS: Final = [
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
]
_EN_TENS: Final = {
    2: "twenty",
    3: "thirty",
    4: "forty",
    5: "fifty",
    6: "sixty",
    7: "seventy",
    8: "eighty",
    9: "ninety",
}
_FR_HOUR: Final = re.compile(r"\b(\d{1,2})\s?h\s?(\d{2})?\b", re.IGNORECASE)
_EN_HOUR: Final = re.compile(r"\b(\d{1,2}):(\d{2})\b")
_NUMBER: Final = re.compile(r"\b\d{1,2}\b")


def _french(number: int, *, feminine: bool = False) -> str:
    """``number`` (0..99) in French words; ``feminine`` for "une" (une heure, vingt et une)."""
    if number == 1 and feminine:
        return "une"
    if number < 20:
        return _FR_UNITS[number]
    tens, unit = divmod(number, 10)
    if tens in (7, 9):
        base = "soixante" if tens == 7 else "quatre-vingt"
        rest = unit + 10
        joiner = " et " if tens == 7 and unit == 1 else "-"
        return f"{base}{joiner}{_FR_UNITS[rest]}"
    if tens == 8:
        return "quatre-vingts" if unit == 0 else f"quatre-vingt-{_french(unit, feminine=feminine)}"
    word = _FR_TENS[tens]
    if unit == 0:
        return word
    if unit == 1:
        return f"{word} et {_french(1, feminine=feminine)}"
    return f"{word}-{_FR_UNITS[unit]}"


def _english(number: int) -> str:
    """``number`` (0..99) in English words."""
    if number < 20:
        return _EN_UNITS[number]
    tens, unit = divmod(number, 10)
    return _EN_TENS[tens] if unit == 0 else f"{_EN_TENS[tens]}-{_EN_UNITS[unit]}"


def _french_hour(found: re.Match[str]) -> str:
    hours, minutes = int(found.group(1)), found.group(2)
    if hours > 23:
        return found.group(0)
    said = f"{_french(hours, feminine=True)} {'heure' if hours <= 1 else 'heures'}"
    if minutes is not None and int(minutes) != 0:
        said += f" {_french(int(minutes))}"
    return said


def _english_hour(found: re.Match[str]) -> str:
    hours, minutes = int(found.group(1)), int(found.group(2))
    if hours > 23 or minutes > 59:
        return found.group(0)
    return _english(hours) if minutes == 0 else f"{_english(hours)} {_english(minutes)}"


def in_words(text: str, language: str) -> str:
    """``text`` with its hours and its numbers up to 99 in words, in ``language`` (``fr`` or
    ``en``; any other is left as it is)."""
    if language == "fr":
        text = _FR_HOUR.sub(_french_hour, text)
        return _NUMBER.sub(lambda found: _french(int(found.group(0))), text)
    if language == "en":
        text = _EN_HOUR.sub(_english_hour, text)
        return _NUMBER.sub(lambda found: _english(int(found.group(0))), text)
    return text


def heard_back_said(said: str, heard: str, language: str) -> float:
    """The share of the words said that were heard (``heard_back``), once the figures of both are
    in words."""
    return heard_back(in_words(said, language), in_words(heard, language))


def misheard_at_most(model: str, sentences: int) -> int:
    """How many of ``sentences`` the hearing ``model`` may mishear (none for a model the trials
    never measured)."""
    return math.floor(MISHEARD_SHARE.get(model, 0.0) * sentences)
