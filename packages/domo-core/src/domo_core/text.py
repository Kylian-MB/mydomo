"""Rules for short texts that someone wrote (a person, or the model on their behalf) and that are
shown to people on a line of their own: a name, the reason Domo gives for a check.

They are here, and not with any one tool or service, so that whatever shows such a text checks it
the same way. What a plain line is (``is_plain_line``) is the rule the gatekeeper reads too
(``domo_common.text_rules``).
"""

import re
import unicodedata
from typing import Final, Literal

from domo_common.text_rules import is_plain_line as is_plain_line

# The longest display name: it is shown on a line of its own, to other people too.
MAX_DISPLAY_NAME_LENGTH: Final = 60

# Characters that show nothing, and that the comparison of names skips: the variation selectors,
# and the few "default ignorable" ones that are not format characters (Cf, which are found by
# their category): the combining grapheme joiner, the Hangul fillers, the Khmer inherent vowels.
_INVISIBLE: Final = frozenset(
    {
        0x034F,
        0x115F,
        0x1160,
        0x17B4,
        0x17B5,
        0x3164,
        0xFFA0,
        *range(0x180B, 0x180E),
        0x180F,
        *range(0xFE00, 0xFE10),
        *range(0xE0100, 0xE01F0),
    }
)


def _without_invisible(text: str) -> str:
    return "".join(
        character
        for character in text
        if unicodedata.category(character) != "Cf" and ord(character) not in _INVISIBLE
    )


def name_key(name: str) -> str:
    """What two names must share to be the same name: they read the same. The invisible
    characters are dropped (the zero-width space and joiners, the soft hyphen, the word joiner,
    the variation selectors, a byte order mark...), the compatibility forms are unified (full-width
    letters, ligatures, any kind of space, however the accents are encoded: NFKC), every run of
    white space becomes one space, the spaces around are dropped and the case is set aside.

    Letters of another script that look like ours (a Cyrillic "a" for a Latin one) are not
    unified: that is out of scope for now. A name whose key is empty shows nothing at all:
    ``display_name_value`` refuses it."""
    text = _without_invisible(unicodedata.normalize("NFKC", _without_invisible(name)))
    return unicodedata.normalize("NFKC", " ".join(text.split()).casefold())


def cleaned_name(name: str) -> str:
    """``name`` as ``name_key`` reads it, with its case kept: the invisible characters dropped,
    the compatibility forms unified (NFKC), every run of white space one space, none around. What
    is left may still hold a control character: the caller checks it (``is_plain_line``)."""
    text = _without_invisible(unicodedata.normalize("NFKC", _without_invisible(name)))
    return unicodedata.normalize("NFKC", " ".join(text.split()))


class InvalidDisplayName(ValueError):
    """A display name that cannot be used. ``code`` is ``invalid`` (not a text, empty, or not one
    plain line) or ``too_long``; ``detail`` says which, in plain words.

    It never repeats the name: what it says may reach the model, the logs, or a page."""

    def __init__(self, code: Literal["invalid", "too_long"], detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code: Literal["invalid", "too_long"] = code
        self.detail = detail


def display_name_value(value: object) -> str:
    """A name people are called by, without the spaces around it: ``InvalidDisplayName`` unless it
    is a text of 1 to ``MAX_DISPLAY_NAME_LENGTH`` characters that make one plain line
    (``is_plain_line``) and show something (its ``name_key`` is not empty: a lone zero-width
    space would read as no name at all, R86).

    The one rule for every name that is set: renaming oneself (on the account page, or through
    Domo), the owner's name at the first launch, inviting someone, and joining with an
    invitation (R70)."""
    if not isinstance(value, str):
        raise InvalidDisplayName("invalid", "the name is a text")
    name = value.strip()
    if not name:
        raise InvalidDisplayName("invalid", "the name is empty")
    if len(name) > MAX_DISPLAY_NAME_LENGTH:
        raise InvalidDisplayName(
            "too_long", f"the name has more than {MAX_DISPLAY_NAME_LENGTH} characters"
        )
    if not is_plain_line(name):
        raise InvalidDisplayName(
            "invalid", "the name has a control character, a line break or a direction mark"
        )
    if not name_key(name):
        raise InvalidDisplayName("invalid", "the name has no character that shows")
    return name


# The marks of a markdown reply that a page shows as they are: the two stars that open or close
# words in bold (next to a character that is no space: "2 ** 3" keeps its stars), and the hashes
# that begin a heading. Underscores are left (``__init__`` is a name), and so are the dashes of a
# short list, which read well as they are.
_BOLD: Final = re.compile(r"(?<=\S)\*\*|\*\*(?=\S)")
_HEADING: Final = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(?=\S)", re.MULTILINE)
# What a line may begin with while it is not known yet whether it is a heading.
_MAYBE_HEADING: Final = re.compile(r"[ \t]{0,3}#{0,6}[ \t]*")


def without_marks(reply: str) -> str:
    """``reply`` without the markdown marks a model writes in spite of being told not to: the
    stars of bold and the hashes of a heading go, their words stay. People read a reply as plain
    text, and may hear it said: the marks would be shown, or read aloud."""
    return _HEADING.sub("", _BOLD.sub("", reply))


class PlainStream:
    """A reply that comes piece by piece, given on without its markdown marks
    (``without_marks``): ``feed`` gives what can be shown of the pieces so far, and keeps back
    the end that the next piece may turn into a mark (stars, the beginning of a line that may be
    a heading); ``flush`` gives what was kept back, once the reply is over."""

    def __init__(self) -> None:
        self._held = ""
        self._before = "\n"

    def feed(self, piece: str) -> str:
        text = self._held + piece
        cut = len(text)
        while cut > 0 and text[cut - 1] == "*":
            cut -= 1
        line = text.rfind("\n", 0, cut) + 1
        if (line > 0 or self._before == "\n") and _MAYBE_HEADING.fullmatch(text, line, cut):
            cut = line
        self._held = text[cut:]
        return self._plain(text[:cut])

    def flush(self) -> str:
        text, self._held = self._held, ""
        return self._plain(text)

    def _plain(self, text: str) -> str:
        if not text:
            return ""
        plain = without_marks(self._before + text)
        self._before = text[-1]
        return plain[1:]
