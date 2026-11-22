"""The languages of the people Domo writes for.

``browser_language`` reads the language a browser prefers from its ``Accept-Language`` header,
whatever it is: Domo writes his texts in any language (spec 4.1, 4.6). Only the pages of the
installation, before the first key, keep fixed texts, in English or in French
(``domo_common.i18n.negotiate_language``).

The tag goes into what the writer reads (``writer.ForWhom.language``), and the header may come
from anyone: only a tag of a strict shape is taken, a language, then maybe a script and a region
(``pt-BR``, ``zh-Hant-TW``, ``es-419``), in its usual case (``text_rules.language_tag``), never
anything else a header can hold (no variant, extension or private part).

A language *has a version* once one text at least was written for it (``known_languages``). A
language with no version of its own reads a close one (``version_for``): the same language, in the
same script (``are_close``: ``fr`` and ``fr-CA``, ``en-US`` and ``en-GB``, but not ``zh-TW`` and
``zh-CN``). Only that version, read where a text is not the reader's own, is the close one: the
reader's own tag is kept with them, and their own texts, dates, amounts and ``<html lang>`` follow
it. A visitor reads the version of the language their browser prefers, or a close one, if there
is one. Anyone can open the login page, so a visitor never makes a new language written; only a
valid link does (an invitation, or a way back: ``household.routes``), and the first page of a
member, when their language has no version, nor a close one. The house begins
``NEW_LANGUAGES_PER_DAY`` new languages a day at most (``may_begin_a_language``), and the page of a
link waits ``VISITOR_LANGUAGE_SECONDS`` at most for its texts in the new language (spec 4.6).
"""

from collections.abc import Collection, Mapping
from datetime import datetime, time
from functools import lru_cache
from typing import Final
from zoneinfo import ZoneInfo

from babel import Locale, UnknownLocaleError
from babel.core import get_global, parse_locale
from domo_common.text_rules import language_tag
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.policy.settings import load_policy_settings
from domo_core.texts.models import WrittenText
from domo_core.texts.store import languages_written_since

# How long the page of a valid link waits for its texts in a new language, in seconds.
VISITOR_LANGUAGE_SECONDS: Final = 10
# How many new languages the house begins in a day, at most.
NEW_LANGUAGES_PER_DAY: Final = 3
# The time of a house that chose no time zone.
_UTC: Final = ZoneInfo("UTC")


def _weight(parameters: str) -> float | None:
    """The ``q`` weight of one entry of the header, from its parameters (1 when it has none);
    ``None`` when it does not read or is not between 0 and 1."""
    for parameter in parameters.split(";"):
        name, _, value = parameter.partition("=")
        if name.strip().lower() == "q":
            try:
                weight = float(value.strip())
            except ValueError:
                return None
            return weight if 0.0 <= weight <= 1.0 else None
    return 1.0


def browser_language(accept_language: str | None) -> str | None:
    """The language the browser prefers, whatever it is: the whole tag of the entry of
    ``accept_language`` with the highest weight, in its usual case (``"de-CH"`` for
    ``"de-ch, fr;q=0.8"``). Among entries of the same weight, the first listed wins, as the
    header's rules say. An entry that is not a tag of the shape taken (``*``, a private tag, a
    variant, a word, anything longer), or whose weight is zero or does not read, is passed over.
    ``None`` when the header is missing or names no language."""
    if not accept_language:
        return None
    best: str | None = None
    best_weight = 0.0
    for entry in accept_language.split(","):
        text, _, parameters = entry.partition(";")
        tag = language_tag(text.strip())
        weight = _weight(parameters)
        if tag is not None and weight is not None and weight > best_weight:
            best = tag
            best_weight = weight
    return best


async def known_languages(session: AsyncSession) -> frozenset[str]:
    """The languages that have a version: one text at least written for the language (not for a
    person)."""
    found = await session.execute(
        select(WrittenText.language).where(WrittenText.language.is_not(None)).distinct()
    )
    return frozenset(language for language in found.scalars() if language is not None)


def _midnight(now: datetime, zone: ZoneInfo) -> datetime:
    """The midnight that began the day of ``now``, at the time of ``zone``."""
    return datetime.combine(now.astimezone(zone).date(), time(), tzinfo=zone)


async def languages_begun_today(
    session: AsyncSession, *, now: datetime, begun: Mapping[str, datetime] | None = None
) -> frozenset[str]:
    """The languages begun since midnight at the house's time (its time zone, UTC when it chose
    none), on the day of ``now``: those whose version kept a first text since, and those of
    ``begun`` (each with when it was begun) begun since, whatever their writing kept."""
    zone = (await load_policy_settings(session)).time_zone or _UTC
    midnight = _midnight(now, zone)
    today = set(await languages_written_since(session, midnight))
    today.update(language for language, when in (begun or {}).items() if when >= midnight)
    return frozenset(today)


async def may_begin_a_language(
    session: AsyncSession, *, now: datetime, begun: Mapping[str, datetime] | None = None
) -> bool:
    """Whether the house may begin the version of a new language at ``now``: fewer than
    ``NEW_LANGUAGES_PER_DAY`` were begun since midnight at the house's time
    (``languages_begun_today``, with ``begun``)."""
    return len(await languages_begun_today(session, now=now, begun=begun)) < NEW_LANGUAGES_PER_DAY


@lru_cache(maxsize=256)
def _language_and_script(tag: str) -> tuple[str, str | None] | None:
    """The language of ``tag`` and the script it is written in, as Babel reads them: the script
    the tag says, else the likely one for its language and region (``zh`` in ``Hans``, ``zh-TW``
    in ``Hant``, ``sr`` in ``Cyrl``); ``None`` when Babel cannot read the tag."""
    try:
        locale = Locale.parse(tag, sep="-")
    except (UnknownLocaleError, ValueError):
        return None
    script = locale.script
    if script is None:
        likely = get_global("likely_subtags")
        found = (locale.territory and likely.get(f"{locale.language}_{locale.territory}")) or (
            likely.get(locale.language)
        )
        if isinstance(found, str):
            script = parse_locale(found)[2]
    return locale.language, script


def are_close(one: str, other: str) -> bool:
    """Whether the languages of the tags ``one`` and ``other`` are close: the same language, in
    the same script, once Babel resolves what the tags leave out (``_language_and_script``). A
    tag Babel cannot read is close to none."""
    mine = _language_and_script(one)
    return mine is not None and mine == _language_and_script(other)


def version_for(language: str, versions: Collection[str], *, house: str) -> str | None:
    """The version of ``versions`` (the languages that have one) that a reader of ``language``
    reads: the language's own; else, among the close ones (``are_close``), the house's language
    (``house``), else the first one in alphabetical order; ``None`` when there is none."""
    if language in versions:
        return language
    close = sorted(version for version in versions if are_close(language, version))
    if house in close:
        return house
    return close[0] if close else None


async def version_read(session: AsyncSession, language: str, *, house: str) -> str | None:
    """The version read in ``language`` where a text is not the reader's own: its own, or a
    close one (``version_for``); ``None`` when there is none (the reader then gets the default
    phrases of their language, else the English ones: never the house's version)."""
    if language == house:
        return house
    return version_for(language, await known_languages(session), house=house)
