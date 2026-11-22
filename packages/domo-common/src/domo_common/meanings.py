"""The catalog of meanings: what each text people read must say.

``locales/en.json`` maps a key to the English sentence that states its meaning, and
``locales/notes.json`` gives a one-line note to the keys whose sentence is ambiguous outside its
page (what a button is for, where a title is shown). Both are read through ``importlib.resources``
like the message catalogs of ``i18n``. This module gives the meaning, the holes (placeholders),
the hash, the allowed length and the group of each key.

Each meaning also has a default phrase in every language of ``DEFAULT_LANGUAGES``: the English
one is the meaning itself, the French one is in ``locales/fr.json``, with the same keys and the
same holes. A text Domo never wrote shows its default phrase (``default_text``); he rewrites
them when he wants.
"""

import hashlib
import json
from collections.abc import Mapping
from functools import cache
from importlib import resources
from string import Formatter
from typing import Final, cast

SIGN: Final = "…"
# The languages whose default phrases are shipped, each in ``locales/<language>.json``.
DEFAULT_LANGUAGES: Final[tuple[str, ...]] = ("en", "fr")
INSTALL_PREFIXES: Final[tuple[str, ...]] = (
    "setup.",
    "login.",
    "error.",
    "vault.",
    "nav.",
    "passwords.",
    "security.",
)
INSTALL_KEYS_EXTRA: Final[frozenset[str]] = frozenset({"account.passkeys.ip_unavailable"})
_TWO_SEGMENT_FIRSTS: Final[frozenset[str]] = frozenset({"action"})
_NEVER_WRITTEN_GROUP: Final = "setup"


def _read_strings(name: str) -> Mapping[str, str]:
    path = resources.files("domo_common").joinpath("locales", name)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in cast(dict[object, object], data).items()
    ):
        raise ValueError(f"{name} must be a JSON object of strings.")
    return cast(dict[str, str], data)


@cache
def _meanings() -> Mapping[str, str]:
    return _read_strings("en.json")


@cache
def _defaults(language: str) -> Mapping[str, str]:
    return _read_strings(f"{language}.json")


@cache
def _notes() -> Mapping[str, str]:
    return _read_strings("notes.json")


def meaning(key: str) -> str:
    """The English sentence saying what the text of ``key`` must say; KeyError when unknown."""
    return _meanings()[key]


def has_meaning(key: str) -> bool:
    """Whether ``key`` is in the catalog: a text that may be shown (a key that is not shows only
    a sign once Domo writes the texts)."""
    return key in _meanings()


def _defaults_language(language: str) -> str | None:
    """The language of ``DEFAULT_LANGUAGES`` that ``language`` reads: itself, or the one of its
    primary subtag (``fr-CA`` and ``fr_CA`` read ``fr``); ``None`` when there is none."""
    primary = language.replace("_", "-").split("-", 1)[0].lower()
    return primary if primary in DEFAULT_LANGUAGES else None


def has_defaults(language: str) -> bool:
    """Whether ``language`` reads default phrases of its own (or of a close language)."""
    return _defaults_language(language) is not None


def default_text(key: str, language: str) -> str:
    """The default phrase of ``key``, its holes left as they are: in ``language`` when it reads
    default phrases of its own (``has_defaults``), else in English. ``KeyError`` for a key that
    has no meaning."""
    english = meaning(key)
    shipped = _defaults_language(language)
    if shipped is None or shipped == "en":
        return english
    return _defaults(shipped).get(key, english)


def meaning_note(key: str) -> str | None:
    """The one-line note that completes the meaning of ``key``, when it has one."""
    return _notes().get(key)


def holes(key: str) -> frozenset[str]:
    """The placeholder names of the meaning of ``key``."""
    names: set[str] = set()
    for _, field, _, _ in Formatter().parse(meaning(key)):
        if field:
            names.add(field.split(".", 1)[0].split("[", 1)[0])
    return frozenset(names)


def meaning_hash(key: str) -> str:
    """The sha256 hex digest of the meaning and its note: it changes when either changes."""
    return hashlib.sha256(f"{meaning(key)}\n{meaning_note(key) or ''}".encode()).hexdigest()


def max_length(key: str) -> int:
    """The longest text allowed for ``key``, counted with its holes left unfilled."""
    return 2 * len(meaning(key)) + 40


def group_of(key: str) -> str:
    """The group of ``key``: its first segment, or its first two for ``action.`` keys."""
    segments = key.split(".")
    if segments[0] in _TWO_SEGMENT_FIRSTS and len(segments) > 1:
        return ".".join(segments[:2])
    return segments[0]


def all_keys() -> tuple[str, ...]:
    return tuple(_meanings())


def keys_of_group(group: str) -> tuple[str, ...]:
    return tuple(sorted(key for key in _meanings() if group_of(key) == group))


def written_groups() -> tuple[str, ...]:
    """Every group that gets written (all but ``setup``), sorted."""
    return tuple(sorted({group_of(key) for key in _meanings()} - {_NEVER_WRITTEN_GROUP}))


def is_install_key(key: str) -> bool:
    """Whether ``key`` is a text shown during installation, before Domo can write."""
    return key.startswith(INSTALL_PREFIXES) or key in INSTALL_KEYS_EXTRA
