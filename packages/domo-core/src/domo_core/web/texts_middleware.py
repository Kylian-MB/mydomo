"""The texts of each request: what its reader reads (spec 4.2, 4.6, 5.4).

``TextsMiddleware`` sets ``request.state.texts`` (a ``Texts``, which ``web.language.request_texts``
reads) before any route runs:

* during the installation, the fixed catalog of the language the browser prefers, English or
  French (``CatalogTexts``), as the pages always had;
* once Domo is installed, what Domo wrote for the reader (``view.recueil_for``), in their
  language. A person signed in reads their own language: at their first page, the one their
  browser prefers (``languages.browser_language``), else the house's, is kept with them, and the
  texts written for them in the language they read until then are dropped, so that they read the
  version of their new language until their own texts are written again. Where a text is not
  their own, they read the version of their language, or of a close one when theirs has none
  (``languages.version_for``: ``fr-FR`` for ``fr``), then the default phrases of their language,
  else the English ones (``view.recueil_for``). A visitor reads the language their browser
  prefers when it has a version, or a close one (``languages.known_languages``), or default
  phrases of its own (``meanings.has_defaults``), else English; the house's when their browser
  prefers none. Their own tag is kept for the page (``<html lang>``, dates and amounts) when it is
  only a close version they read. The texts are read once per request, in one query, and the
  connection is given back before the route runs.

Once the response is whole, the groups of the texts the page showed that are not yet the person's
own are asked for them (``scheduler.TextWriting.ask``, ``app.state.text_writing``, with the
writing note Domo keeps for them): they are written in the background only when Domo has a
reason to (``scheduler.has_a_reason``), and show from the next display on. A page never waits for
the writer. When the person's language has no version yet, nor a close one, the house begins it
then, in the background, on its own key (``TextWriting.begin_language``, which begins none for a
language with default phrases of its own), if it may begin a new language today; else a later
page asks again. Both are asked for just before the last piece of the response is sent, so that
whoever has the whole response finds the writing begun (``app.state.wakes``). Nothing is ever
written for a visitor: only the page of a valid link makes a new language written
(``household.routes``).

Keeping a person's language is the one write of this middleware, in a short transaction of its
own, committed before the texts are read; two first pages at once keep one language. The refusal
of a request from another site, and the error pages read outside this middleware
(``texts_for``), only read: someone whose language is not kept yet reads the house's there.

It is a plain ASGI middleware, so that the chat's streams pass through untouched: the texts a
stream shows count too, and are asked for as it ends. Static files and the health check have no
texts; should one of them end on an error page, its handler reads the texts then
(``ensure_texts``), and so does the refusal of a request from another site (``texts_for``), which
comes before this middleware.

Texts that cannot be read because the database does not answer (or the application has none)
show the English default phrase of each text (``meanings.default_text``): a page never fails for
its database alone. Any other error is not hidden: it is an error of the
page. Should asking for the writing fail, the error is logged and the page is sent whole.
"""

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any, Final, NamedTuple
from zoneinfo import ZoneInfo

from domo_common.i18n import DEFAULT_LANGUAGE, negotiate_language
from domo_common.meanings import has_defaults
from sqlalchemy import delete, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import Headers
from starlette.requests import HTTPConnection, Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from domo_core.accounts.models import Person
from domo_core.accounts.sessions import SESSION_COOKIE, person_of_session
from domo_core.channels.api import API_PATH
from domo_core.db import SessionFactory
from domo_core.naming import DEFAULT_NAME, name_shown
from domo_core.policy.settings import load_policy_settings
from domo_core.setup_flow.middleware import setup_is_done
from domo_core.texts.languages import browser_language, version_read
from domo_core.texts.models import WrittenText
from domo_core.texts.store import Reader
from domo_core.texts.view import CatalogTexts, RecueilTexts, Texts, recueil_for, shown_groups
from domo_core.web.deps import get_clock
from domo_core.web.language import DOMO_STATE_KEY, STATE_KEY

logger = logging.getLogger(__name__)

# The paths whose requests have no texts (and each one's sub-paths): the channel of the API reads
# the texts of the person its device belongs to (``texts.view.person_texts``), never a request's.
NO_TEXTS: Final = ("/static", "/healthz", API_PATH)
# The language of texts that could not be read: undetermined (BCP 47).
UNREAD_LANGUAGE: Final = "und"


class _Read(NamedTuple):
    """What was read of a request: its ``texts``; who reads them once Domo is installed
    (``reader``: ``None`` for a visitor, and during the installation), with the writing note
    Domo keeps for them (``writing_note``); and the reader's language when it has no version
    yet, nor a close one (``new_language``), which the house may begin; and the name the reader
    sees for him (``domo``: their own name for him, else the house's, else "Domo"; a visitor
    reads the house's, and every page of the installation says "Domo"), which the pages say where
    the code names him (``web.templating``, ``DOMO_STATE_KEY``)."""

    texts: Texts
    reader: Reader | None = None
    new_language: str | None = None
    writing_note: str | None = None
    domo: str = DEFAULT_NAME


def _is_under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(f"{prefix}/")


def _now(app: Any) -> datetime:
    """The time as the pages read it (the ``get_clock`` dependency: a test may replace it)."""
    clock: Callable[[], Callable[[], datetime]] = app.dependency_overrides.get(get_clock, get_clock)
    return clock()()


def _unread() -> RecueilTexts:
    """The texts of a request whose texts could not be read: the English default phrases."""
    return RecueilTexts(
        language=UNREAD_LANGUAGE, zone=ZoneInfo("UTC"), found={}, personal=frozenset()
    )


async def _read(scope: Scope, *, keeps_language: bool) -> _Read:
    """What the reader of the request ``scope`` reads, and who they are (see ``_Read``); the
    default phrases when the database does not answer, or when the application has none. With
    ``keeps_language`` (the middleware's read), the language of a person who has none yet is kept
    (their first page), and a language that has no version is told; without it, nothing is
    written or told."""
    state = getattr(scope.get("app"), "state", None)
    if getattr(state, "session_factory", None) is None:
        # An application without a database (one not started, or a middleware on its own).
        logger.warning("The texts of a request could not be read (no database)")
        return _Read(_unread())
    try:
        return await _read_or_fail(scope, keeps_language=keeps_language)
    except (DBAPIError, OSError) as error:
        # The kind of error only: its message may hold what the database said.
        logger.warning("The texts of a request could not be read (%s)", type(error).__name__)
        return _Read(_unread())


async def _read_or_fail(scope: Scope, *, keeps_language: bool) -> _Read:
    app = scope["app"]
    accept_language = Headers(scope=scope).get("accept-language")
    if not await setup_is_done(app, scope):
        return _Read(CatalogTexts(negotiate_language(accept_language)))
    token = HTTPConnection(scope).cookies.get(SESSION_COOKIE)
    # Never the header itself: a tag of a strict shape, or none.
    browser = browser_language(accept_language)
    session_factory: SessionFactory = app.state.session_factory
    async with session_factory() as session:
        person = await person_of_session(session, token, _now(app)) if token else None
        settings = await load_policy_settings(session)
        house = settings.house_language
        if person is None:
            language, version = await _visitors_language(session, browser, house=house)
        else:
            if person.language is not None:
                language = person.language
            elif keeps_language:
                language = await _keep_first_language(
                    session, person, browser or house, house=house
                )
            else:
                language = house
            version = await version_read(session, language, house=house)
        new_language = (
            language if keeps_language and person is not None and version is None else None
        )
        texts = await recueil_for(
            session, person=person, language=language, settings=settings, version=version
        )
    domo = name_shown(person.calls_domo if person is not None else None, settings.house_domo_name)
    if person is None:
        return _Read(texts, None, new_language, None, domo)
    return _Read(
        texts,
        Reader(person.id, language),
        new_language,
        person.writing_note,
        domo,
    )


async def _visitors_language(
    session: AsyncSession, browser: str | None, *, house: str
) -> tuple[str, str]:
    """The language a visitor reads, and the version they read in it: the one their browser
    prefers (``browser``) when it has a version, or a close one, or default phrases of its own (it
    reads them where its version has no text); else English, as a member does (its version, or a
    close one, where there is one; else the English default phrases); the house's (``house``) when
    the browser prefers none."""
    if browser is None:
        return house, house
    version = await version_read(session, browser, house=house)
    if version is not None:
        return browser, version
    if has_defaults(browser):
        return browser, browser
    english = await version_read(session, DEFAULT_LANGUAGE, house=house)
    return DEFAULT_LANGUAGE, english or DEFAULT_LANGUAGE


async def _keep_first_language(
    session: AsyncSession, person: Person, language: str, *, house: str
) -> str:
    """The language ``person`` reads from their first page on: ``language``, kept with them, or
    the one another page of theirs kept at the same time. Until then they read the house's
    language (``house``): when ``language`` is another one, the texts written for them are dropped
    with it. Committed at once."""
    kept = await session.execute(
        update(Person)
        .where(Person.id == person.id, Person.language.is_(None))
        .values(language=language)
        .returning(Person.id)
    )
    if kept.scalar_one_or_none() is None:
        # Another page of theirs kept their language first.
        stored = await session.scalar(select(Person.language).where(Person.id == person.id))
        return language if stored is None else stored
    if language != house:
        await session.execute(delete(WrittenText).where(WrittenText.person_id == person.id))
    await session.commit()
    return language


async def texts_for(scope: Scope) -> Texts:
    """The texts of the reader of the request ``scope``, read now, and only read (see the module
    docstring)."""
    return (await _read(scope, keeps_language=False)).texts


async def ensure_texts(request: Request) -> Texts:
    """The texts of ``request``: those the middleware read, else read now (a static file not
    found, say), and kept on it."""
    state = request.scope.setdefault("state", {})
    texts = state.get(STATE_KEY)
    if texts is None:
        texts = state[STATE_KEY] = await texts_for(request.scope)
    return texts


class TextsMiddleware:
    """Sets the texts of each request before its route runs, and has the texts it showed written
    for its reader afterwards (see the module docstring)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or any(_is_under(scope["path"], p) for p in NO_TEXTS):
            await self.app(scope, receive, send)
            return
        read = await _read(scope, keeps_language=True)
        texts, reader = read.texts, read.reader
        scope.setdefault("state", {})[STATE_KEY] = texts
        scope["state"][DOMO_STATE_KEY] = read.domo
        if reader is None or not isinstance(texts, RecueilTexts):
            await self.app(scope, receive, send)
            return
        asked = False

        async def ask() -> None:
            nonlocal asked
            asked = True
            try:
                writing = scope["app"].state.text_writing
                groups = shown_groups(texts)
                if groups:
                    writing.ask(reader, groups, writing_note=read.writing_note)
                if read.new_language is not None:
                    # The version of their language, for the house, on its key: the texts of the
                    # page first, then the rest.
                    await writing.begin_language(read.new_language, groups, host=None)
            except Exception as error:
                # The page is sent whole all the same. The kind of error only: beginning a
                # language reads the database, whose message may hold what it said.
                logger.error("Asking for the texts of a page failed (%s)", type(error).__name__)

        async def send_then_ask(message: Message) -> None:
            last = message["type"] == "http.response.body" and not message.get("more_body", False)
            if last and not asked:
                await ask()
            await send(message)

        await self.app(scope, receive, send_then_ask)
        if not asked:
            await ask()
