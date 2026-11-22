"""Dates and amounts put in words by the code, never by the writer (spec 4.6).

``local_date`` and ``local_amount`` say them as a reader of a language reads them, with the data
of the Babel library: a date at the time of the house (a day alone as that day), an amount with
its currency (``$20.00``, ``20,00 €``), every digit of it kept (``$0.0012``: never rounded to a
cent). A language Babel has no data for is said as its primary language (``de-XX`` as ``de``),
else in English (``babel_locale``).

``plain_date`` is a date as models read it, whatever the language: in UTC
(``2026-11-06 14:03 UTC``), or a day alone (``2026-11-06``).

Nothing here reads the database: every module that shows a text may import it.
"""

from datetime import UTC, date, datetime, tzinfo
from decimal import Decimal
from functools import cache
from typing import Final

from babel import Locale, UnknownLocaleError
from babel.dates import format_date, format_time, get_datetime_format
from babel.numbers import format_currency

# The language said when Babel knows neither a tag nor its primary language.
FALLBACK_LANGUAGE: Final = "en"


@cache
def babel_locale(language: str) -> Locale:
    """The Babel locale of the tag ``language`` (``pt-BR``, ``de``): the tag's own, else its
    primary language's, else English's, when Babel has no data for it."""
    for tag in (language, language.split("-", 1)[0], FALLBACK_LANGUAGE):
        try:
            return Locale.parse(tag, sep="-")
        except (UnknownLocaleError, ValueError):
            continue
    return Locale.parse(FALLBACK_LANGUAGE)


def local_date(moment: date | datetime, *, language: str, zone: tzinfo, time: bool = True) -> str:
    """``moment`` as a reader of ``language`` reads it: a moment at the time of ``zone`` (the
    house's), with its time or the day alone; a day alone (a ``date``) as that very day, never
    moved by a zone."""
    locale = babel_locale(language)
    if not isinstance(moment, datetime):
        return format_date(moment, format="medium", locale=locale)
    local = moment.astimezone(zone)
    day = format_date(local.date(), format="medium", locale=locale)
    if not time:
        return day
    hour = format_time(local, format="short", locale=locale)
    # How the language puts a day and a time together (``{1}, {0}``): a quoted word of it is
    # said as it is, without its quotes.
    glue = str(get_datetime_format("medium", locale=locale))
    glue = glue.replace("''", "\0").replace("'", "").replace("\0", "'")
    return glue.replace("{1}", day).replace("{0}", hour)


def local_amount(value: Decimal, currency: str, *, language: str) -> str:
    """``value`` in ``currency`` as a reader of ``language`` reads an amount, its currency with
    it, every digit of ``value`` kept."""
    return format_currency(
        value, currency, locale=babel_locale(language), decimal_quantization=False
    )


def plain_date(moment: date | datetime, *, time: bool = True) -> str:
    """``moment`` in UTC: ``2026-11-06 14:03 UTC``, or the day alone (``2026-11-06``) without
    ``time``; a day alone (a ``date``) as that very day."""
    if not isinstance(moment, datetime):
        return f"{moment:%Y-%m-%d}"
    in_utc = moment.astimezone(UTC)
    if not time:
        return f"{in_utc:%Y-%m-%d}"
    return f"{in_utc:%Y-%m-%d %H:%M} UTC"
