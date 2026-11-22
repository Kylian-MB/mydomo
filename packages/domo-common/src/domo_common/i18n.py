"""The fixed texts of the installation: negotiation from ``Accept-Language`` and the catalogs.

Before Domo has a key, he cannot write: the pages of the installation keep fixed texts, in
English or in French (spec 4.1). Once installed, a text people read is one Domo wrote
(``domo_core.texts``), else its default phrase from these same catalogs
(``meanings.default_text``).

The catalogs are flat JSON objects (``locales/en.json``, ``locales/fr.json``) shipped as package
data and read through ``importlib.resources``, so they are found whether the package runs from
a checkout or from an installed wheel. ``en.json`` is also the catalog of meanings
(``meanings``): it holds every key, and ``fr.json`` holds the same keys with the same holes; a key
missing in a catalog falls back to the English text.
"""

import json
from collections.abc import Mapping
from functools import cache
from importlib import resources
from typing import Literal, cast

Language = Literal["en", "fr"]

DEFAULT_LANGUAGE: Language = "en"
_SUPPORTED: tuple[Language, ...] = ("en", "fr")


def _parse_weight(parameters: str) -> float | None:
    """The ``q`` weight of one ``Accept-Language`` entry (1 when absent); None when malformed."""
    for parameter in parameters.split(";"):
        name, _, value = parameter.partition("=")
        if name.strip().lower() == "q":
            try:
                weight = float(value.strip())
            except ValueError:
                return None
            return weight if 0.0 <= weight <= 1.0 else None
    return 1.0


def negotiate_language(accept_language: str | None) -> Language:
    """The supported language the client prefers; English when none is acceptable.

    Languages are matched on their primary subtag (``fr-CA`` is French). The highest weight wins
    and, at equal weight, the entry listed first. Entries with a zero or malformed weight, the
    ``*`` wildcard and unsupported languages are ignored.
    """
    if not accept_language:
        return DEFAULT_LANGUAGE
    best: Language | None = None
    best_weight = 0.0
    for entry in accept_language.split(","):
        tag, _, parameters = entry.partition(";")
        primary = tag.strip().split("-", 1)[0].lower()
        weight = _parse_weight(parameters)
        if primary in _SUPPORTED and weight is not None and weight > best_weight:
            best = primary
            best_weight = weight
    return best or DEFAULT_LANGUAGE


@cache
def _catalog(lang: Language) -> Mapping[str, str]:
    path = resources.files("domo_common").joinpath("locales", f"{lang}.json")
    catalog = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(catalog, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in cast(dict[object, object], catalog).items()
    ):
        raise ValueError(f"The {lang} catalog must be a JSON object of strings.")
    return cast(dict[str, str], catalog)


def t(key: str, lang: Language, /, **params: str) -> str:
    """The text for ``key`` in ``lang``, with ``{name}`` placeholders filled from ``params``.

    ``key`` and ``lang`` are positional only, so that a parameter may have any name (``key`` and
    ``lang`` included: the parameters can come from the arguments of a tool call).

    A key missing in ``lang`` falls back to the English text; a key missing in every catalog
    raises ``KeyError``, and so does a placeholder left without a parameter. Parameter values are
    inserted verbatim: braces inside a value are not interpreted. Literal braces in a catalog text
    are written ``{{`` and ``}}``.
    """
    return template(key, lang).format(**params)


def template(key: str, lang: Language, /) -> str:
    """The text for ``key`` in ``lang`` as the catalog holds it, its ``{name}`` placeholders left
    as they are; the English text when ``lang`` has none, ``KeyError`` when no catalog has it."""
    catalog = _catalog(lang)
    return catalog[key] if key in catalog else _catalog(DEFAULT_LANGUAGE)[key]
