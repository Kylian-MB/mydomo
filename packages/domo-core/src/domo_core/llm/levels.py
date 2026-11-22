"""The levels of models: how strong a model is, read from its name.

The table is the project's own and nothing Domo learns can change it: the tasks that must not
use a weak model (the yes judge, the reading of a yes in words, the writing of rights) rely on it.
"""

import re
from typing import Final, Literal

Level = Literal["small", "medium", "strong"]

LEVELS: Final[tuple[Level, ...]] = ("small", "medium", "strong")

_SMALL_WORDS: Final = frozenset({"nano", "mini", "lite"})
_WORD_SEPARATORS: Final = re.compile(r"[-.:_]")
_REASONING_NAME: Final = re.compile(r"o\d")
_GPT_NAME: Final = re.compile(r"gpt-\d")


def level_rank(level: Level | None) -> int:
    """The position of a level, from 0 (``small``). No level counts as ``small``."""
    return 0 if level is None else LEVELS.index(level)


def table_level(model_id: str) -> Level | None:
    """The level the project's table gives a model, or ``None`` when the name says nothing.

    The name is read in lower case, without ``models/`` and whatever precedes its last ``/``. In
    this order: ``small`` for ``haiku`` anywhere or ``nano``, ``mini``, ``lite`` as a whole word;
    ``strong`` for ``opus`` anywhere, ``pro`` as a whole word, or a name made of ``o`` and digits
    at its start; ``medium`` for ``sonnet`` or ``flash`` anywhere, or ``gpt-`` and a digit at its
    start. The words of a name are what is left when it is cut on ``-``, ``.``, ``:`` and ``_``.
    """
    name = model_id.lower().rsplit("/", 1)[-1]
    words = set(_WORD_SEPARATORS.split(name))
    if "haiku" in name or words & _SMALL_WORDS:
        return "small"
    if "opus" in name or "pro" in words or _REASONING_NAME.match(name):
        return "strong"
    if "sonnet" in name or "flash" in name or _GPT_NAME.match(name):
        return "medium"
    return None
