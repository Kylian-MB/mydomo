"""What the code checks of a text the writer wrote, before the checker reads it (spec 4.3).

``check_text`` checks the text of a key of the catalog of meanings, in this order:

1. it shows something (it is not empty, nor only spaces);
2. each hole of the key's meaning (``{name}``) is in it once, and it has no other brace: a hole
   left out, written twice, translated or made up is refused, and so is a lone brace;
3. it is plain text: no ``<`` nor ``>`` (no markup), no web address (``://`` or ``www.``), and the
   rules of ``text.is_plain_line``: no control character, no lone surrogate, no line or paragraph
   separator and no direction mark (``domo_common.text_rules.plain_problem``, the rule the
   gatekeeper holds the texts of a vault form to);
4. it is one line;
5. it is no longer than ``meanings.max_length(key)``, counted on the text with its holes as they
   are (``{name}``), never once they are filled: a long name never makes a text too long.

``check_head`` checks the head of a card, written on the moment for one reader (spec 4.4): each
name of its facts is in it (whatever the case), and each amount, in digits (``numbers_in``: however
its digits are grouped, never inside another number); each of its holes once and no other brace;
plain text, as above, but on as many lines as it needs (a head holds several parts of a card);
``HEAD_MAX_LENGTH`` characters at most.

Each answers ``None`` when the text is fine, else a short reason in English, for the writer: it
asks the text again with it (``texts.writer``).
"""

import json
import re
from collections.abc import Collection, Sequence
from typing import Final

from domo_common import meanings
from domo_common.text_rules import HOLE, plain_problem

# The longest head of a card.
HEAD_MAX_LENGTH: Final = 1200

# A number as it may be written in a text, its digits grouped by one space (a thin or a
# non-breaking one too), a dot, a comma or an apostrophe: 1 250,50 and 1,250.50 are both 125050.
_NUMBER: Final = re.compile(r"[0-9](?:[ .,'  ]?[0-9])*")
_NOT_A_DIGIT: Final = re.compile(r"[^0-9]")


def _empty(text: str) -> str | None:
    return "the text is empty" if not text.strip() else None


def _holes_problem(text: str, wanted: Collection[str]) -> str | None:
    """What is wrong with the holes of ``text``: each of ``wanted`` once, and no other brace."""
    found = HOLE.findall(text)
    for hole in sorted(wanted):
        count = found.count(hole)
        if count == 0:
            return f"the hole {{{hole}}} is missing: keep it as it is, in braces"
        if count > 1:
            return f"the hole {{{hole}}} is there {count} times: keep it once"
    rest = HOLE.sub(lambda match: "" if match.group(1) in wanted else match.group(0), text)
    if "{" in rest or "}" in rest:
        if not wanted:
            return "no braces: the meaning has no holes"
        allowed = ", ".join(f"{{{hole}}}" for hole in sorted(wanted))
        return f"a brace that is not a hole of the meaning: only {allowed} may be in braces"
    return None


def check_text(key: str, text: str) -> str | None:
    """``None`` when ``text`` may stand for the key ``key`` (see the module docstring), else why
    not. ``KeyError`` for a key that is not in the catalog of meanings."""
    problem = _empty(text) or _holes_problem(text, meanings.holes(key)) or plain_problem(text)
    if problem is not None:
        return problem
    if "\n" in text:
        return "one line only"
    longest = meanings.max_length(key)
    if len(text) > longest:
        return f"too long: {longest} characters at most, holes counted as they are"
    return None


def numbers_in(text: str) -> set[str]:
    """The digits of each number written in ``text``."""
    return {_NOT_A_DIGIT.sub("", number) for number in _NUMBER.findall(text)}


def check_head(
    text: str, *, names: Sequence[str], amounts: Sequence[str], holes: Collection[str]
) -> str | None:
    """``None`` when ``text`` may be the head of a card whose facts hold ``names`` and
    ``amounts`` (in digits), with ``holes`` (see the module docstring), else why not."""
    problem = _empty(text)
    if problem is not None:
        return problem
    folded = text.casefold()
    for name in names:
        if name.strip() and name.casefold() not in folded:
            shown = json.dumps(name, ensure_ascii=False)
            return f"the name {shown} is missing: write it as it is"
    numbers = numbers_in(text)
    for amount in amounts:
        digits = _NOT_A_DIGIT.sub("", amount)
        if digits and digits not in numbers:
            return f"the amount {amount} is missing: write it in digits"
    problem = _holes_problem(text, holes) or plain_problem(text)
    if problem is not None:
        return problem
    if len(text) > HEAD_MAX_LENGTH:
        return f"too long: {HEAD_MAX_LENGTH} characters at most"
    return None
