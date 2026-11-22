"""The rules of a text people read that the core and the gatekeeper share, written once.

What the core keeps of the writer's texts (``domo_core.texts.checks``) is what the gatekeeper
shows on a vault form (``gatekeeper_api.SecretRequestCreate``): if the gatekeeper were stricter,
every form would fail. So both read these rules:

* ``is_plain_line``: a text that can be shown on a line of its own, as it reads: no control
  character, no line break, no lone surrogate and no bidirectional control (they can make a text
  look like another one to the people who read it);
* ``plain_problem``: what keeps a text, on one line or several, from being plain text: markup
  (``<`` or ``>``), a web address (``://`` or ``www.``), or what ``is_plain_line`` refuses;
* ``HOLE``: a hole of a text, a name in braces (``{name}``), which the code fills;
* ``LANGUAGE_TAG``: a language tag as it is taken: a language of two or three letters, maybe a
  script of four letters, maybe a region of two letters or three digits, and nothing else (no
  variant, extension or private part). ASCII only: in any case, ``[a-z]`` would otherwise take
  letters such as the Kelvin sign. ``language_tag`` gives a tag of that shape in its usual case
  (``pt-BR``, ``zh-Hant-TW``): the one a language is kept and told with, wherever it comes from
  (a browser, a setting of the house, a person who asks for another language).
"""

import re
import unicodedata
from typing import Final

# A hole of a text: a name in braces.
HOLE: Final = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
# A language tag: its language, its script and its region are the groups 1, 2 and 3.
LANGUAGE_TAG: Final = re.compile(
    r"([a-z]{2,3})(?:-([a-z]{4}))?(?:-([a-z]{2}|[0-9]{3}))?", re.IGNORECASE | re.ASCII
)
# The longest tag of that shape: a language of three letters, a script and a region of three
# digits.
LANGUAGE_TAG_MAX: Final = 12
# Control characters, lone surrogates (they cannot be stored), and the line and paragraph
# separators (they break a line like a newline does).
REFUSED_CATEGORIES: Final = frozenset({"Cc", "Cs", "Zl", "Zp"})
# The bidirectional controls: embeddings and overrides, isolates, and the left-to-right,
# right-to-left and Arabic letter marks. Only these: the other format characters (the zero-width
# joiner of emoji sequences, say) are fine.
BIDI_CONTROLS: Final = frozenset(
    map(chr, [*range(0x202A, 0x202F), *range(0x2066, 0x206A), 0x200E, 0x200F, 0x061C])
)


def is_plain_line(text: str) -> bool:
    """Whether ``text`` can be shown on a line of its own, as it reads (see the module
    docstring)."""
    return not any(
        unicodedata.category(character) in REFUSED_CATEGORIES or character in BIDI_CONTROLS
        for character in text
    )


def plain_problem(text: str) -> str | None:
    """What keeps ``text``, on one line or several, from being plain text, in a few words of
    English (the writer reads them); ``None`` when it is plain."""
    if "<" in text or ">" in text:
        return "plain text only: no < or >"
    folded = text.casefold()
    if "://" in folded or "www." in folded:
        return "no web address"
    if not all(is_plain_line(line) for line in text.split("\n")):
        return "no control character and no direction mark"
    return None


def language_tag(text: str) -> str | None:
    """``text`` as a language tag of the shape ``LANGUAGE_TAG`` takes, in its usual case (the
    language in lower case, the script capitalized, the region in upper case: ``zh-Hant-TW``);
    ``None`` when it has another shape (spaces around it included)."""
    found = LANGUAGE_TAG.fullmatch(text) if len(text) <= LANGUAGE_TAG_MAX else None
    if found is None:
        return None
    language, script, region = found.groups()
    parts = [language.lower()]
    if script is not None:
        parts.append(script.capitalize())
    if region is not None:
        parts.append(region.upper())
    return "-".join(parts)
