"""What the code shows someone is read through a ``Texts``: the text of a key with its holes
filled, a date and an amount, in what the reader reads.

``RecueilTexts`` is what a reader reads once Domo is installed (spec 4.2, 0.7.1 spec 5.1): the
texts Domo wrote, the best one of each key (``recueil_for``): the reader's own, else the version
of their language (or of a close one); a key with neither shows its default phrase, in the
reader's language or a close one, else in English (``meanings.default_text``). The version of the
house's language is never read in place of the reader's: a language without default phrases of
its own reads the English ones until its version is written. Only a key with no meaning shows a
sign without words (``SIGN``). It notes each key it was asked (``asked``), so that the texts a
page showed that are not yet the reader's own can be asked for them afterwards
(``web.texts_middleware``).
Dates and amounts are said by the code in the reader's language, at the time of the house
(``formatting``). What a person reads away from their pages (the vault form of a key they give,
asked for in a turn of theirs, say) is read the same way, from their language alone
(``person_texts``).

``CatalogTexts`` reads the fixed catalogs (``domo_common.i18n``), in English or in French: the
pages of the installation, before Domo can write. ``REFERENCE`` is the English catalog: what a
model reads (the yes judge, the description of a request for a model, the results of the tools)
is said in it, whoever it is about, its dates in UTC.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Final, Protocol
from zoneinfo import ZoneInfo

from domo_common.i18n import Language, t, template
from domo_common.meanings import SIGN, default_text, group_of, has_meaning
from domo_common.text_rules import HOLE
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.policy.settings import PolicySettings, load_policy_settings
from domo_core.texts.formatting import local_amount, local_date, plain_date
from domo_core.texts.languages import version_read
from domo_core.texts.store import Reader, written

# The time of a house that chose no time zone.
_UTC: Final = ZoneInfo("UTC")


class Texts(Protocol):
    """The texts one reader reads."""

    @property
    def language(self) -> str:
        """The BCP 47 tag of what is shown (``<html lang>``)."""
        ...

    def __call__(self, key: str, /, **params: str) -> str:
        """The text of ``key``, with its ``{name}`` holes filled from ``params``.

        ``key`` is positional only, so that a parameter may have any name (the parameters can
        come from the arguments of a tool call)."""
        ...

    def template(self, key: str, /) -> str:
        """The text of ``key`` with its ``{name}`` holes left as they are, for another to fill
        (the gatekeeper fills those of its form); the sign for a key with no meaning."""
        ...

    def date(self, moment: date | datetime, *, time: bool = True) -> str:
        """``moment`` as the reader reads a date: a moment with its time, or the day alone; a
        day alone (a ``date``) as that very day."""
        ...

    def amount(self, value: Decimal, currency: str) -> str:
        """``value`` in ``currency`` as the reader reads an amount, its currency with it."""
        ...


@dataclass(frozen=True)
class CatalogTexts:
    """The fixed catalogs in ``lang``, as ``t`` reads them: a key missing in ``lang`` falls back to
    the English text, and a key missing everywhere, or a hole left without a parameter, raises
    ``KeyError``. Dates are said in UTC, the same in every language (``plain_date``); amounts as
    a reader of ``lang`` reads them (``local_amount``)."""

    lang: Language

    @property
    def language(self) -> str:
        return self.lang

    def __call__(self, key: str, /, **params: str) -> str:
        return t(key, self.lang, **params)

    def template(self, key: str, /) -> str:
        return template(key, self.lang)

    def date(self, moment: date | datetime, *, time: bool = True) -> str:
        return plain_date(moment, time=time)

    def amount(self, value: Decimal, currency: str) -> str:
        return local_amount(value, currency, language=self.lang)


# What models read: the English catalog.
REFERENCE: Final[Texts] = CatalogTexts("en")


def fill_holes(text: str, params: Mapping[str, object]) -> str:
    """``text`` with each of its ``{name}`` holes replaced by ``params[name]``, as it is (a value
    that is not a text, a count from a template say, as ``str`` writes it): braces in a value are
    shown as they were typed, never read as a hole. ``KeyError`` for a hole without a value."""
    return HOLE.sub(lambda hole: str(params[hole.group(1)]), text)


@dataclass
class RecueilTexts:
    """The texts Domo wrote that one reader reads (see the module docstring): in ``language``,
    dates at the time of ``zone`` (the house's). ``found`` is the best text of each key: the
    reader's own, then their language's; ``personal`` the keys found in the reader's own version;
    ``asked`` each key looked up so far. A key with no text shows its
    default phrase in ``language`` (``default_text``), a key with no meaning ``SIGN``; a hole
    without a value raises ``KeyError``, as the catalogs do."""

    language: str
    zone: ZoneInfo
    found: Mapping[str, str]
    personal: frozenset[str]
    asked: set[str] = field(default_factory=lambda: set[str]())

    def __call__(self, key: str, /, **params: str) -> str:
        text = self.template(key)
        return fill_holes(text, params) if key in self.found or has_meaning(key) else text

    def template(self, key: str, /) -> str:
        self.asked.add(key)
        if key in self.found:
            return self.found[key]
        return default_text(key, self.language) if has_meaning(key) else SIGN

    def date(self, moment: date | datetime, *, time: bool = True) -> str:
        return local_date(moment, language=self.language, zone=self.zone, time=time)

    def amount(self, value: Decimal, currency: str) -> str:
        return local_amount(value, currency, language=self.language)


def shown_groups(texts: RecueilTexts) -> list[str]:
    """The groups of the keys ``texts`` showed that are not the reader's own yet."""
    keys = sorted(key for key in texts.asked - texts.personal if has_meaning(key))
    return list(dict.fromkeys(group_of(key) for key in keys))


async def recueil_for(
    session: AsyncSession,
    *,
    person: Person | None,
    language: str,
    settings: PolicySettings,
    version: str | None = None,
) -> RecueilTexts:
    """What ``person`` (``None``: a visitor) reads in ``language``: their own texts, then those
    of the version of ``version`` (``language`` itself unless said: a close language whose
    version they read, ``languages.version_for``); else the default phrases (``RecueilTexts``).
    At the time of the house (``settings.time_zone``, UTC when the house chose none). Read in one
    query."""
    of_the_language = Reader(None, language if version is None else version)
    own = None if person is None else Reader(person.id, language)
    readers = [of_the_language] if own is None else [own, of_the_language]
    texts = await written(session, readers)
    mine = {} if own is None else texts[own]
    found = {**texts[of_the_language], **mine}
    return RecueilTexts(
        language=language,
        zone=settings.time_zone or _UTC,
        found=found,
        personal=frozenset(mine),
    )


async def person_texts(session: AsyncSession, person: Person) -> RecueilTexts:
    """What ``person`` reads, away from a page of theirs: in their language as the database holds
    it now (the house's until a page of theirs kept one), where a text is not their own the
    version of it or of a close one (``languages.version_read``); else the default phrases
    (``recueil_for``). Reads only."""
    settings = await load_policy_settings(session)
    house = settings.house_language
    kept = await session.scalar(select(Person.language).where(Person.id == person.id))
    language = kept or house
    version = await version_read(session, language, house=house)
    return await recueil_for(
        session, person=person, language=language, settings=settings, version=version
    )
