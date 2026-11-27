"""The words of a reply cut into sentences to be said, and cleaned of what is only for the eye
(spec 6.7).

The words come piece by piece (``conversation.events.Words``). ``SentenceCutter`` gives each
sentence as soon as it ends, so that the first one can be turned into sound while the model still
writes the next. It never cuts inside a number (``3.5``), a time (``12:30``) or after an initial
(``J. Martin``: a capital letter on its own, before a word with a capital), and a piece shorter
than ``MIN_CHARS`` waits for the next one (a sound of two words is jerky). A sentence longer than
``MAX_CHARS`` is cut at its last comma, else its last space.

The voice makes a whole piece before any of its sound, so a long first sentence delays his first
word. With ``early_first`` (the voice's listener), the FIRST piece given out also ends at the
first comma, semicolon or colon followed by a space once ``FIRST_PIECE_CHARS`` have come, or at
the end of its sentence if that comes first; every later piece is a whole sentence, as without
it. Only what is said is cut so: what is shown is the reply's text, untouched.
Nothing is written anywhere: the text lives in the cutter until it is given out.
"""

import re
from typing import Final

MIN_CHARS: Final = 12
MAX_CHARS: Final = 300
# How long the first piece of a reply is, at least, before it may end at a comma (``early_first``).
FIRST_PIECE_CHARS: Final = 40
_PAUSES: Final = ",;:"

_MARKS: Final = ".!?…:;"
_CLOSERS: Final = "\"')»”’)]"

_FENCE: Final = "```"
_CODE_BLOCK: Final = re.compile(r"`{3,}.*?(?:`{3,}|\Z)", re.DOTALL)
_INLINE_CODE: Final = re.compile(r"`[^`\n]*`")
_LINK: Final = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_URL: Final = re.compile(r"(?:https?://|www\.)\S+")
_TABLE_RULE: Final = re.compile(r"^[ \t]*\|?[ \t]*:?-{3,}[-:| \t]*$", re.MULTILINE)
_LIST_MARK: Final = re.compile(r"^[ \t]*(?:[-*+•]|\d+[.)])[ \t]+", re.MULTILINE)
_HEADING_OR_QUOTE: Final = re.compile(r"^[ \t]*(?:#{1,6}|>)[ \t]*", re.MULTILINE)
_STARS: Final = re.compile(r"\*+|`+|~~")
_EDGE_UNDERSCORES: Final = re.compile(r"(?<!\w)_+|_+(?!\w)")
_EMOJI: Final = re.compile("[\U0001f000-\U0001faff\u2600-\u27bf\u2b50\ufe0f\u200d]")
_SPACES: Final = re.compile(r"\s+")


def speakable(text: str) -> str:
    """``text`` without what is only for the eye: code (a fenced block, which is dropped to its end
    when it is not closed, and inline code: neither is read aloud), the rules and pipes of a table,
    list marks, headings, emphasis marks, emoji, the address of a link (its words stay), and bare
    addresses (the page shows them). Empty when nothing is left to say."""
    text = _CODE_BLOCK.sub(" ", text)
    text = _INLINE_CODE.sub(" ", text)
    text = _LINK.sub(r"\1", text)
    text = _URL.sub("", text)
    text = _TABLE_RULE.sub("", text)
    text = _LIST_MARK.sub("", text)
    text = _HEADING_OR_QUOTE.sub("", text)
    text = _STARS.sub("", text)
    text = _EDGE_UNDERSCORES.sub("", text)
    text = _EMOJI.sub("", text.replace("|", " "))
    return _SPACES.sub(" ", text).strip()


class Fence:
    """Follows code across the sentences it is cut into: ``outside`` gives each piece of the words
    without what lies inside a fenced block (a run of three backticks or more opens and closes it)
    or inside inline code (a shorter run, until the next one or the end of the line)."""

    def __init__(self) -> None:
        self._inside = False
        self._inline = False
        self._carried = False  # inline code opened with no closing mark in its piece

    @property
    def open(self) -> bool:
        """Whether a fenced block is still open: the rest of the words is code."""
        return self._inside

    def reset(self) -> None:
        """Start again (a card came between two segments of the words)."""
        self._inside = self._inline = self._carried = False

    @staticmethod
    def _closes(text: str, start: int) -> bool:
        """Whether a backtick comes in ``text`` from ``start`` before the end of the line."""
        end = text.find("\n", start)
        return "`" in text[start : len(text) if end < 0 else end]

    def outside(self, text: str) -> str:
        if self._carried:
            # Opened in the piece before: it holds if this one closes it, else it was no code.
            self._carried = False
            if not self._closes(text, 0):
                self._inline = False
        kept: list[str] = []
        i = 0
        while i < len(text):
            char = text[i]
            if char == "`":
                end = i
                while end < len(text) and text[end] == "`":
                    end += 1
                if end - i >= 3:
                    self._inside = not self._inside
                    self._inline = False
                elif not self._inside:
                    if self._inline:
                        self._inline = False
                    elif (
                        i > 0 and text[i - 1].isalnum() and end < len(text) and text[end].isalnum()
                    ):
                        kept.append("'")  # an apostrophe, not code
                    else:
                        self._inline = True
                        self._carried = not self._closes(text, end)
                i = end
                continue
            if char == "\n":
                self._inline = False
            if not self._inside and not self._inline:
                kept.append(char)
            i += 1
        return "".join(kept)


def _is_initial(before: str) -> bool:
    """Whether the text before a full stop ends with a capital letter on its own (an initial)."""
    return (
        len(before) >= 1
        and before[-1].isalpha()
        and before[-1].isupper()
        and (len(before) == 1 or not before[-2].isalnum())
    )


class SentenceCutter:
    """Cuts words that come in pieces into sentences, in order; with ``early_first``, the first
    piece may end at a pause (see the module docstring)."""

    def __init__(self, *, early_first: bool = False) -> None:
        self._buffer = ""
        self._early = early_first

    @property
    def pending(self) -> bool:
        """Whether words wait for the end of their sentence."""
        return bool(self._buffer.strip())

    def feed(self, text: str) -> list[str]:
        """Add ``text``; the sentences it completed, in order."""
        self._buffer += text
        return self._take()

    def flush(self) -> list[str]:
        """The end: what is left, as a last sentence, if there is any."""
        out = self._take()
        rest = self._buffer.strip()
        self._buffer = ""
        if rest:
            out.append(rest)
        return out

    def _take(self) -> list[str]:
        out: list[str] = []
        while (end := self._boundary()) is not None:
            sentence = self._buffer[:end].strip()
            self._buffer = self._buffer[end:]
            if sentence:
                out.append(sentence)
                self._early = False  # the first piece is out: whole sentences from now on
        return out

    def _boundary(self) -> int | None:
        """Where the first sentence of the buffer ends (the index after it), or ``None``."""
        buffer = self._buffer
        i = 0
        while i < len(buffer):
            char = buffer[i]
            if char == "\n":
                if len(buffer[:i].strip()) >= MIN_CHARS:
                    return i + 1
            elif char in _MARKS:
                end = i + 1
                while end < len(buffer) and buffer[end] in _CLOSERS:
                    end += 1
                follows = buffer[end] if end < len(buffer) else None
                if (
                    (follows is None or follows.isspace())
                    and len(buffer[:end].strip()) >= MIN_CHARS
                    and not self._is_inside(buffer[:i], char, buffer[end:])
                ):
                    return end
            if (
                self._early
                and char in _PAUSES
                and i + 1 < len(buffer)
                and buffer[i + 1].isspace()
                and len(buffer[: i + 1].strip()) >= FIRST_PIECE_CHARS
            ):
                # The first piece of a reply, said sooner (a number's "21,5" has no space).
                return i + 1
            if i + 1 >= MAX_CHARS:
                break
            i += 1
        if len(buffer) > MAX_CHARS:
            return self._cut_long(buffer)
        return None

    @staticmethod
    def _is_inside(before: str, mark: str, after: str) -> bool:
        """Whether a mark is part of a number or an initial and not the end of a sentence
        (``after`` is what follows the mark)."""
        if mark != ".":
            return False
        if before and before[-1].isdigit() and not after:
            return True  # "3." at the end of a piece: the digits may come next
        if _is_initial(before):
            following = after.lstrip()
            # An initial goes with a name: "J. Martin". With no word yet, wait for it.
            return not following or following[0].isupper()
        return False

    @staticmethod
    def _cut_long(buffer: str) -> int:
        window = buffer[:MAX_CHARS]
        comma = window.rfind(",")
        if comma > 0:
            return comma + 1
        space = window.rfind(" ")
        return space if space > 0 else MAX_CHARS
