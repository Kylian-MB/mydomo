"""A reply says a thing once (2026-11-16, the owner's trial of 0.9.0).

A model often writes a few words, calls a tool (a silent one most of the time: a memory kept, a
note marked told), then writes the same thing again in the next round of the same answer; both
pieces were kept in the one reply ("Alex left you a note: ..." twice). The prompt tells Domo to
answer once; ``said_once`` is the net under it, for what is stored: the live stream cannot be taken
back, but the stored reply, which ``/since``, a reload and the history read, keeps a thing once.

It is very conservative (the review's ruling): only two things go, compared once the spaces, the
case and the final punctuation are set aside:

* a whole later round whose text is the text of an earlier round of the same answer;
* a whole paragraph (text between blank lines) of a later round that is a paragraph of an earlier
  round, when it is prose of at least ``MIN_PROSE`` characters: none of its lines begins with a
  list marker or a number.

Nothing that holds quotation marks ever goes (exact words quoted twice stay), nothing inside the
first round, no single line of a list, and nothing is rewritten.
"""

import re
from collections.abc import Sequence
from typing import Final

# The shortest paragraph that may be taken for a repeat.
MIN_PROSE: Final = 25
# What separates two paragraphs: a blank line, with the spaces around it.
_PARAGRAPHS: Final = re.compile(r"(\n[ \t]*\n\s*)")
# A line of a list: a marker or a number first.
_LIST_LINE: Final = re.compile(r"^\s*(?:[-*•+–]|\d+[.)])", re.MULTILINE)
# Quotation marks: exact words are never taken out.
_QUOTES: Final = frozenset('"«»“”„')
# What a text ends with that does not make it another one.
_FINAL: Final = ".!?…:;, \t\n"


def _key(text: str) -> str:
    """A text as it is compared: its words, one space apart, in no case, without its final
    punctuation."""
    return " ".join(text.split()).casefold().rstrip(_FINAL)


def _quoted(text: str) -> bool:
    return any(mark in _QUOTES for mark in text)


def _prose(paragraph: str) -> bool:
    """Whether a paragraph may be taken for a repeat: prose, long enough, with no quote."""
    return (
        len(_key(paragraph)) >= MIN_PROSE
        and _LIST_LINE.search(paragraph) is None
        and not _quoted(paragraph)
    )


def _split(body: str) -> list[tuple[str, str]]:
    """The paragraphs of ``body``, each with the blank line that follows it (empty for the
    last)."""
    parts = _PARAGRAPHS.split(body)
    texts, separators = parts[0::2], [*parts[1::2], ""]
    return list(zip(texts, separators, strict=True))


def said_once(rounds: Sequence[str]) -> str:
    """The text of an answer whose rounds said ``rounds`` (each with what separates it from the
    round before), each thing said once (see the module docstring)."""
    rounds_said: set[str] = set()
    paragraphs_said: set[str] = set()
    kept: list[str] = []
    for index, text in enumerate(rounds):
        body = text.lstrip()
        lead = text[: len(text) - len(body)]
        whole = _key(body)
        paragraphs = _split(body)
        if index > 0 and whole and whole in rounds_said and not _quoted(body):
            continue
        if index == 0 or not whole:
            kept.append(text)
        else:
            parts = [
                (paragraph, separator)
                for paragraph, separator in paragraphs
                if not (_prose(paragraph) and _key(paragraph) in paragraphs_said)
            ]
            if not parts:
                continue
            if len(parts) == len(paragraphs):
                kept.append(text)
            else:
                rest = "".join(paragraph + separator for paragraph, separator in parts)
                kept.append(lead + (rest if body[-1:].isspace() else rest.rstrip()))
        if whole:
            rounds_said.add(whole)
        paragraphs_said.update(_key(paragraph) for paragraph, _ in paragraphs if _key(paragraph))
    return "".join(kept)
