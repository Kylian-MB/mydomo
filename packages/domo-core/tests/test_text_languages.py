"""The languages of the people Domo writes for (``texts.languages``, spec 4.6, scenarios 14 and
27).

The language a browser prefers, whatever it is, is read from its ``Accept-Language`` header, as a
whole tag of a strict shape (it goes into what the writer reads). Each person reads their own
language: their browser's at their first page, kept with them. A visitor reads the language of
their browser when a version of it exists, else the house's, and never makes a new language
written: only a valid link (an invitation, or a way back) does, on the keys of the one who
invited, the texts of its page within ten seconds and the rest in the background. The house
begins three new languages a day at most.
"""

import asyncio
import html
import json
import re
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from chat_support import PILOT_MODELS, PILOT_PROVIDER_URL, ChatServer, Member, live_http_client
from domo_common.meanings import default_text, meaning
from domo_core.accounts.models import Person
from domo_core.audit.models import AuditEntry
from domo_core.household import routes as household_routes
from domo_core.household.service import (
    INVITATION_TTL,
    create_invitation,
    create_return_invitation,
)
from domo_core.llm.models import Provider, ProviderModel, new_provider_id
from domo_core.llm.spending_models import ModelCall
from domo_core.llm.types import (
    ChatChunk,
    ChatMessage,
    ChatUsage,
    HistoryItem,
    ModelCallError,
    ToolCall,
    ToolChoice,
    ToolDefinition,
)
from domo_core.policy.settings import HOUSE_LANGUAGE, TIME_ZONE_KEY, update_policy_setting
from domo_core.texts.languages import (
    NEW_LANGUAGES_PER_DAY,
    VISITOR_LANGUAGE_SECONDS,
    are_close,
    browser_language,
    known_languages,
    may_begin_a_language,
    version_for,
)
from domo_core.texts.models import WrittenText
from domo_core.texts.store import Reader, save_written, written
from domo_core.texts.upkeep import GAP_RETRY
from domo_core.texts.writer import ForWhom
from fakes import FakeChatModel, TextWriter
from sqlalchemy import select, text, update
from web_support import house_reads, in_language, write_language_version

# --- the language a browser prefers --------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "language"),
    [
        ("de", "de"),
        ("de-CH", "de-CH"),
        ("pt-BR", "pt-BR"),
        ("ja,en;q=0.8", "ja"),
        ("fr-CA,fr;q=0.9,en;q=0.8", "fr-CA"),
        # The tag in its usual case: the language in lower case, the script capitalized, the
        # region in upper case.
        ("EN-us", "en-US"),
        ("DE-ch", "de-CH"),
        ("zh-hant-tw", "zh-Hant-TW"),
        ("sr-LATN", "sr-Latn"),
        ("es-419", "es-419"),
        # A three-letter language is a language too.
        ("gsw-CH", "gsw-CH"),
        # The highest weight wins, wherever it is listed; the first of equals.
        ("en;q=0.5, it", "it"),
        ("it, de", "it"),
        ("en;q=0.7, de;q=0.7", "en"),
        (" de-AT ; q=0.9 ", "de-AT"),
    ],
)
def test_the_browser_language_is_the_tag_it_prefers_whatever_it_is(header, language):
    assert browser_language(header) == language


@pytest.mark.parametrize(
    ("header", "language"),
    [
        ("*, de;q=0.5", "de"),  # the wildcard names no language
        ("de;q=0, it;q=0.1", "it"),  # a weight of zero refuses it
        ("de;q=abc, it", "it"),  # a weight that does not read
        ("de;q=2, it;q=0.5", "it"),  # a weight out of its bounds
        ("x-klingon, it;q=0.5", "it"),  # a private tag, not a language
        ("deutsch, it;q=0.5", "it"),  # a word, not a language code
        ("1234, it;q=0.5", "it"),
        # Only a language, a script and a region: nothing else gets to the writer.
        ("en-ignore-previous, it;q=0.5", "it"),
        ("en-US-x-twain, it;q=0.5", "it"),
        ("de-CH-1996, it;q=0.5", "it"),
        ("en-US-US, it;q=0.5", "it"),
        ("en_US, it;q=0.5", "it"),
        ("en-, it;q=0.5", "it"),
        ("en-" + "A" * 60 + ", it;q=0.5", "it"),  # far too long
        # Letters that only look like ASCII ones once their case changes (a Kelvin sign, a
        # dotted capital I).
        ("Ky, it;q=0.5", "it"),
        ("tr-İİ, it;q=0.5", "it"),
    ],
)
def test_what_names_no_language_is_passed_over(header, language):
    assert browser_language(header) == language


@pytest.mark.parametrize(
    "header", [None, "", " ", "*", ",,", "de;q=0", "deutsch", "q=1", "en-ignore-previous"]
)
def test_a_header_that_names_no_language_gives_none(header):
    assert browser_language(header) is None


# --- close languages -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("one", "other"),
    [
        ("fr", "fr-FR"),
        ("fr-FR", "fr-CA"),
        ("fr", "fr-CA"),
        ("en-US", "en-GB"),
        ("pt-BR", "pt-PT"),
        # The script the language is written in when the tag does not say it (Babel's likely
        # subtags): Chinese in simplified characters, Serbian in Cyrillic.
        ("zh", "zh-CN"),
        ("zh-TW", "zh-HK"),
        ("sr", "sr-Cyrl"),
        ("es", "es-419"),
    ],
)
def test_close_languages_share_their_language_and_its_script(one, other):
    assert are_close(one, other)
    assert are_close(other, one)


@pytest.mark.parametrize(
    ("one", "other"),
    [
        ("zh-TW", "zh-CN"),  # traditional and simplified characters
        ("zh", "zh-Hant"),
        ("sr-Latn", "sr-Cyrl"),
        ("sr", "sr-Latn"),
        ("de", "nl"),
        ("en", "de"),
        # A tag Babel cannot read has no close language.
        ("qaa", "qaa-FR"),
        ("de-XX", "de"),
    ],
)
def test_other_languages_are_not_close(one, other):
    assert not are_close(one, other)
    assert not are_close(other, one)


def test_the_version_read_is_the_own_then_the_house_then_the_first_close():
    versions = {"en", "fr", "fr-CA", "fr-FR", "zh-CN"}
    # Its own version first.
    assert version_for("fr-CA", versions, house="en") == "fr-CA"
    # Then the house's, when it is close.
    assert version_for("fr-BE", versions, house="fr-FR") == "fr-FR"
    # Then the first close one, in alphabetical order.
    assert version_for("fr-BE", versions, house="en") == "fr"
    assert version_for("en-GB", versions, house="fr") == "en"
    # None close: no version.
    assert version_for("zh-TW", versions, house="en") is None
    assert version_for("de", versions, house="en") is None
    # A tag Babel cannot read: its own version only.
    assert version_for("qaa", {"qaa", "en"}, house="en") == "qaa"
    assert version_for("qaa-FR", {"qaa", "en"}, house="en") is None


# --- helpers -------------------------------------------------------------------------------------


def plain(page: str) -> str:
    """What a person reads of a page: its markup left out, its spaces as they show."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", page)).split())


def in_their_language(key: str, meaning_of: str, for_whom: ForWhom) -> str:
    """What the writer of these tests writes: the meaning marked with the language, as
    ``web_support.in_language`` shows it, and with the name of the one it is written for (none for
    the version of a language)."""
    text = f"({for_whom.language}) {meaning_of}"
    return text if for_whom.name is None else f"{text} [{for_whom.name}]"


def texts_model() -> FakeChatModel:
    """The writer and the checker of these tests (``in_their_language``), whose calls cost
    nothing."""
    return FakeChatModel(writer=in_their_language, usage=ChatUsage(0, 0))


class HeldTexts:
    """The writer and the checker of the texts (a ``FakeChatModel``, whose calls cost nothing),
    each call held until ``release``."""

    def __init__(self, writes: TextWriter) -> None:
        self.model = FakeChatModel(writer=writes, usage=ChatUsage(0, 0))
        self._gate = asyncio.Event()

    def release(self) -> None:
        self._gate.set()

    @property
    def released(self) -> bool:
        return self._gate.is_set()

    async def stream(
        self,
        *,
        system: str,
        messages: Sequence[HistoryItem],
        max_output_tokens: int,
        tools: Sequence[ToolDefinition] = (),
        tool_choice: ToolChoice = "auto",
        think: bool = True,
    ) -> AsyncGenerator[ChatChunk | ToolCall | ChatUsage]:
        await self._gate.wait()
        async for item in self.model.stream(
            system=system,
            messages=messages,
            max_output_tokens=max_output_tokens,
            tools=tools,
            tool_choice=tool_choice,
        ):
            yield item


@asynccontextmanager
async def a_visitor(server: ChatServer, accept_language: str) -> AsyncIterator[httpx.AsyncClient]:
    """A browser that is not signed in, whose ``Accept-Language`` is ``accept_language``."""
    async with live_http_client(server.base_url) as client:
        client.headers["accept-language"] = accept_language
        yield client


async def add_key(session_factory: Any, giver: Person) -> Provider:
    """A key of ``giver`` of their own (not the general key), listing the models of the server's
    key (committed)."""
    provider_id = new_provider_id()
    key = Provider(
        id=provider_id,
        kind="openai_compatible",
        base_url=PILOT_PROVIDER_URL,
        secret_ref=f"provider:{provider_id}:api_key",
        given_by=giver.id,
        is_general=False,
    )
    async with session_factory() as writer_session:
        writer_session.add(key)
        await writer_session.flush()
        writer_session.add_all(
            ProviderModel(provider_id=provider_id, model_id=model_id) for model_id in PILOT_MODELS
        )
        await writer_session.commit()
    return key


async def invite(session_factory: Any, now: datetime, *, by: Person) -> str:
    """The token of an invitation of Robin, a guest, made by ``by`` at ``now`` (committed)."""
    async with session_factory() as writer_session:
        _, token = await create_invitation(
            writer_session,
            display_name="Robin",
            role="guest",
            guest_days=2,
            created_by=by.id,
            now=now,
        )
        await writer_session.commit()
    return token


async def language_of(session_factory: Any, person: Person) -> str | None:
    async with session_factory() as reader:
        return await reader.scalar(select(Person.language).where(Person.id == person.id))


async def version_of(session_factory: Any, language: str) -> dict[str, str]:
    """The texts of the version of ``language``, by key."""
    reader = Reader(None, language)
    async with session_factory() as session:
        return (await written(session, [reader]))[reader]


async def known(session_factory: Any) -> frozenset[str]:
    async with session_factory() as session:
        return await known_languages(session)


async def who_paid(session_factory: Any) -> set[tuple[str | None, uuid.UUID | None]]:
    """The key and the person each call of the writer and the checker was counted on."""
    async with session_factory() as reader:
        rows = await reader.execute(
            select(ModelCall.provider_id, ModelCall.person_id)
            .where(ModelCall.task.in_(("writing", "checking")))
            .distinct()
        )
        return {(provider_id, person_id) for provider_id, person_id in rows.all()}


async def written_lines(session_factory: Any) -> list[dict[str, Any]]:
    """The details of the lines ``texts.written`` of the journal."""
    async with session_factory() as reader:
        found = await reader.execute(
            select(AuditEntry.details).where(AuditEntry.action == "texts.written")
        )
        return list(found.scalars())


async def waiting_for_a_lock(session_factory: Any) -> None:
    """Wait until a statement of the database waits for a lock (for at most ten seconds)."""
    async with asyncio.timeout(10):
        while True:
            async with session_factory() as session:
                waiting = await session.scalar(
                    text("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'")
                )
            if waiting:
                return
            await asyncio.sleep(0.01)


def names_written_for(model: FakeChatModel) -> set[str | None]:
    """For whom each call of the writer wrote: a person's name, or ``None`` for a version."""
    return {for_whom["name"] for for_whom in written_for(model)}


def languages_written_for(model: FakeChatModel) -> list[str]:
    """The language each call of the writer wrote in, in order."""
    return [for_whom["language"] for for_whom in written_for(model)]


def written_for(model: FakeChatModel) -> list[dict[str, Any]]:
    """Who each call of the writer wrote for (its ``for_whom``), in order."""
    found: list[dict[str, Any]] = []
    for call in model.writer_calls():
        first = call.messages[0]
        assert isinstance(first, ChatMessage)
        found.append(json.loads(first.content)["for_whom"])
    return found


# --- each person's language ----------------------------------------------------------------------


async def test_a_person_takes_their_browser_language_at_their_first_page(
    chat_server: ChatServer, adult_member: Member, child_member: Member, session_factory
):
    chat_server.text_model = texts_model()
    alex, sam = adult_member.person, child_member.person
    # Texts each one had in the house's language before their first page.
    async with session_factory() as writer_session:
        for person in (alex, sam):
            await save_written(
                writer_session,
                Reader(person.id, "en"),
                {"chat.send": f"Send it, {person.display_name}"},
                model_id="m",
                now=chat_server.clock(),
            )
        await writer_session.commit()

    async with chat_server.browser(adult_member) as browser:
        browser.headers["accept-language"] = "de-CH, de;q=0.9, en;q=0.5"
        first = await browser.get("/chat")
        assert first.status_code == 200
        # Their browser's language, whole, kept with them; the texts of their old language are
        # gone, and the page shows at once the best text there is.
        assert await language_of(session_factory, alex) == "de-CH"
        assert '<html lang="de-CH">' in first.text
        assert "Send it, Alex" not in plain(first.text)
        assert meaning("chat.send") in plain(first.text)
        await chat_server.texts_idle()
        # From the next display on: their own texts, in their language.
        again = await browser.get("/chat")
        theirs = in_their_language("chat.send", meaning("chat.send"), ForWhom("de-CH", "Alex"))
        assert theirs in plain(again.text)
        # Another browser later changes nothing: their language is theirs.
        browser.headers["accept-language"] = "it"
        await browser.get("/chat")
    assert await language_of(session_factory, alex) == "de-CH"

    # Sam's browser names no language: the house's, and the texts he had stay his.
    async with chat_server.browser(child_member) as browser:
        page = await browser.get("/chat")
    assert await language_of(session_factory, sam) == "en"
    assert '<html lang="en">' in page.text
    assert "Send it, Sam" in plain(page.text)


async def test_two_first_pages_at_once_keep_one_language(
    chat_server: ChatServer, adult_member: Member, session_factory
):
    chat_server.text_model = texts_model()
    alex = adult_member.person
    async with chat_server.browser(adult_member, waits_for_wakes=False) as browser:
        browser.headers["accept-language"] = "it"
        # Another first page of Alex keeps German, and its transaction is not over yet: this
        # page waits for it.
        async with session_factory() as other_page:
            await other_page.execute(
                update(Person)
                .where(Person.id == alex.id, Person.language.is_(None))
                .values(language="de")
            )
            page = asyncio.create_task(browser.get("/chat"))
            await waiting_for_a_lock(session_factory)
            await other_page.commit()
        shown = await page
        await chat_server.texts_idle()
    # The page does not fail: it shows the language the other one kept, whose version alone is
    # begun.
    assert shown.status_code == 200
    assert await language_of(session_factory, alex) == "de"
    assert '<html lang="de">' in shown.text
    assert await known(session_factory) == {"en", "de"}


async def test_a_new_member_language_is_written_on_the_general_key(
    chat_server: ChatServer, adult_member: Member, session_factory
):
    chat_server.text_model = texts_model()
    alex = adult_member.person
    alex_key = await add_key(session_factory, alex)
    async with chat_server.browser(adult_member) as browser:
        browser.headers["accept-language"] = "it"
        await browser.get("/chat")
        await chat_server.texts_idle()

    # The whole version of Italian, for the house: the groups the page did not show too.
    version = await version_of(session_factory, "it")
    assert version["chat.send"] == in_language("it", "chat.send")
    assert version["audit.title"] == in_language("it", "audit.title")
    # On the general key, counted for the house; Alex's own texts on Alex's key, for Alex.
    assert await who_paid(session_factory) == {
        (chat_server.provider_id, None),
        (alex_key.id, alex.id),
    }
    lines = [line for line in await written_lines(session_factory) if line["language"] == "it"]
    assert {line["person"] for line in lines} == {None, str(alex.id)}


class FailingTexts:
    """A writer whose every call fails, as a provider that errs or limits its rate (each call is
    counted in ``calls``)."""

    def __init__(self) -> None:
        self.calls = 0

    async def stream(
        self,
        *,
        system: str,
        messages: Sequence[HistoryItem],
        max_output_tokens: int,
        tools: Sequence[ToolDefinition] = (),
        tool_choice: ToolChoice = "auto",
        think: bool = True,
    ) -> AsyncGenerator[ChatChunk | ToolCall | ChatUsage]:
        self.calls += 1
        raise ModelCallError("rate_limited")
        yield ChatChunk("")  # a generator, which fails at its first item


async def test_a_language_without_a_catalogue_is_written_in_the_background(
    chat_server: ChatServer, adult_member: Member, session_factory
):  # scenario 4
    chat_server.text_model = texts_model()
    async with chat_server.browser(adult_member) as browser:
        browser.headers["accept-language"] = "de"
        # German has no default phrases of its own: the English ones, at once.
        first = await browser.get("/account")
        assert '<html lang="de">' in first.text
        assert meaning("account.title") in plain(first.text)
        await chat_server.texts_idle()
        # Then German, once written: Alex's own, and the version of the language.
        again = await browser.get("/account")
    theirs = in_their_language("account.title", meaning("account.title"), ForWhom("de", "Alex"))
    assert theirs in plain(again.text)
    assert (await version_of(session_factory, "de"))["account.title"] == in_language(
        "de", "account.title"
    )
    # A visitor in German reads the version from then on.
    async with a_visitor(chat_server, "de") as visitor:
        page = await visitor.get("/login")
    assert '<html lang="de">' in page.text
    assert in_language("de", "login.submit") in plain(page.text)


async def test_a_failed_writing_for_a_language_without_a_catalogue_waits(
    chat_server: ChatServer, adult_member: Member, session_factory
):  # Review Focus 5
    failing = FailingTexts()
    chat_server.text_model = failing
    async with chat_server.browser(adult_member) as browser:
        browser.headers["accept-language"] = "de"
        await browser.get("/chat")
        await chat_server.texts_idle()
        tried = failing.calls
        assert tried > 0
        # The chat polls every few seconds: each text was tried once, and waits its hour.
        for _ in range(10):
            page = await browser.get("/chat")
            await chat_server.texts_idle()
        assert failing.calls == tried
        assert meaning("chat.send") in plain(page.text)
        # An hour later, it is tried again.
        chat_server.clock.advance(GAP_RETRY + timedelta(seconds=1))
        await browser.get("/chat")
        await chat_server.texts_idle()
        assert failing.calls > tried
    assert await known(session_factory) == {"en"}


# --- visitors ------------------------------------------------------------------------------------


async def test_a_stranger_never_makes_a_new_language_written(
    chat_server: ChatServer, session_factory
):
    # Scenario 27: a browser in Japanese, a language nobody in the house has.
    texts = texts_model()
    chat_server.text_model = texts
    async with a_visitor(chat_server, "ja") as stranger:
        page = await stranger.get("/login")
        refused = await stranger.post("/login", data={"username": "nobody", "password": "nothing"})
    await chat_server.texts_idle()
    assert page.status_code == 200
    assert '<html lang="en">' in page.text and '<html lang="en">' in refused.text
    assert meaning("login.submit") in plain(page.text)
    assert texts.writer_calls() == []
    assert await known(session_factory) == {"en"}


async def test_a_stranger_sees_a_known_language(client, session):
    await write_language_version(session, "de")
    german = await client.get("/login", headers={"accept-language": "de, en;q=0.5"})
    assert '<html lang="de">' in german.text
    assert in_language("de", "login.submit") in plain(german.text)
    # A language with no version: the house's.
    other = await client.get("/login", headers={"accept-language": "ja"})
    assert '<html lang="en">' in other.text
    assert meaning("login.submit") in plain(other.text)


async def test_a_visitor_reads_the_default_phrases_of_their_language(client, session):
    # French has default phrases of its own: a French browser reads them, in a house in English.
    french = await client.get("/login", headers={"accept-language": "fr-CA, en;q=0.5"})
    assert '<html lang="fr-CA">' in french.text
    assert default_text("login.submit", "fr") in plain(french.text)


async def test_a_visitor_whose_language_has_no_catalogue_reads_english(
    client, session, owner_account
):
    # A German browser in a house in French: German has no default phrases, nor a version yet.
    await house_reads(session, "fr")
    german = await client.get("/login", headers={"accept-language": "de"})
    assert '<html lang="en">' in german.text
    assert default_text("login.submit", "en") in plain(german.text)
    assert in_language("fr", "login.submit") not in plain(german.text)
    # Once a German version exists, they read it.
    await write_language_version(session, "de")
    again = await client.get("/login", headers={"accept-language": "de"})
    assert '<html lang="de">' in again.text
    assert in_language("de", "login.submit") in plain(again.text)


# --- a new language, on a valid link -------------------------------------------------------------


async def test_an_invitation_writes_the_invitees_language_on_the_inviters_keys(
    chat_server: ChatServer, adult_member: Member, add_member, session_factory
):
    # Scenario 14: Alex invited Robin, whose browser is in Italian.
    chat_server.text_model = texts_model()
    alex = adult_member.person
    alex_key = await add_key(session_factory, alex)
    token = await invite(session_factory, chat_server.clock(), by=alex)
    async with a_visitor(chat_server, "it") as robin:
        page = await robin.get(f"/invite/{token}")
    assert page.status_code == 200
    assert '<html lang="it">' in page.text
    assert in_language("it", "invite.submit") in plain(page.text)
    # The rest of the version is written in the background.
    await chat_server.texts_idle()
    assert (await version_of(session_factory, "it"))["chat.send"] == in_language("it", "chat.send")
    # On Alex's keys, counted for Alex.
    assert await who_paid(session_factory) == {(alex_key.id, alex.id)}

    # A way back is a valid link too: Dana, who left, comes back with a browser in Spanish.
    dana = await add_member("Dana", "adult", username="dana")
    async with session_factory() as writer_session:
        await writer_session.execute(
            update(Person)
            .where(Person.id == dana.id)
            .values(disabled_at=chat_server.clock() - timedelta(days=1))
        )
        left = await writer_session.get(Person, dana.id)
        assert left is not None
        _, back = await create_return_invitation(
            writer_session,
            person=left,
            role="adult",
            guest_days=None,
            display_name="Dana",
            created_by=alex.id,
            now=chat_server.clock(),
        )
        await writer_session.commit()
    async with a_visitor(chat_server, "es") as returning:
        page = await returning.get(f"/invite/{back}")
    assert '<html lang="es">' in page.text
    assert in_language("es", "invite.return.submit") in plain(page.text)
    await chat_server.texts_idle()
    assert await who_paid(session_factory) == {(alex_key.id, alex.id)}


async def test_an_invitation_page_waits_ten_seconds_at_most(
    chat_server: ChatServer, adult_member: Member, session_factory, monkeypatch
):
    assert VISITOR_LANGUAGE_SECONDS == 10
    # A writer that does not answer yet, and a wait made short for the test.
    monkeypatch.setattr(household_routes, "VISITOR_LANGUAGE_SECONDS", 0.5)
    held = HeldTexts(in_their_language)
    chat_server.text_model = held
    token = await invite(session_factory, chat_server.clock(), by=adult_member.person)
    async with a_visitor(chat_server, "it") as robin:
        began = time.monotonic()
        page = await robin.get(f"/invite/{token}")
        waited = time.monotonic() - began
        # Not ready in time: the house's version. The page came back while the writer was still
        # held, so it did not wait for it; how long the rest took is the machine's own speed (a
        # shared build machine pauses for ten seconds at times), and no proof of anything.
        assert page.status_code == 200
        assert waited >= 0.5
        assert not held.released
        assert '<html lang="en">' in page.text
        assert meaning("invite.submit") in plain(page.text)
        # Written meanwhile, in the background: the next display is in Italian.
        held.release()
        await chat_server.texts_idle()
        again = await robin.get(f"/invite/{token}")
    assert '<html lang="it">' in again.text
    assert in_language("it", "invite.submit") in plain(again.text)


async def test_no_more_than_three_new_languages_a_day(
    chat_server: ChatServer, adult_member: Member, session_factory, monkeypatch
):
    assert NEW_LANGUAGES_PER_DAY == 3
    # Two languages begun today: the house's, and German.
    assert await known(session_factory) == {"en"}
    async with session_factory() as writer_session:
        await write_language_version(writer_session, "de")
        await writer_session.commit()
    # Two links opened at once, in two new languages, while the writer has not answered yet:
    # the one begun first counts, and the other is not begun.
    monkeypatch.setattr(household_routes, "VISITOR_LANGUAGE_SECONDS", 0.3)
    held = HeldTexts(in_their_language)
    chat_server.text_model = held
    alex = adult_member.person
    tokens = [await invite(session_factory, chat_server.clock(), by=alex) for _ in range(2)]
    async with a_visitor(chat_server, "it") as robin, a_visitor(chat_server, "es") as dana:
        pages = await asyncio.gather(
            robin.get(f"/invite/{tokens[0]}"), dana.get(f"/invite/{tokens[1]}")
        )
    assert [page.status_code for page in pages] == [200, 200]
    held.release()
    await chat_server.texts_idle()
    begun = await known(session_factory) - {"en", "de"}
    assert begun in ({"it"}, {"es"})
    assert names_written_for(held.model) == {None}

    # Three begun: a member whose language is new reads the house's version, and only their own
    # texts are written.
    texts = texts_model()
    chat_server.text_model = texts
    async with chat_server.browser(adult_member) as browser:
        browser.headers["accept-language"] = "pt"
        page = await browser.get("/chat")
        assert '<html lang="pt">' in page.text
        assert meaning("chat.send") in plain(page.text)
    await chat_server.texts_idle()
    assert await language_of(session_factory, alex) == "pt"
    assert names_written_for(texts) == {"Alex"}
    assert await known(session_factory) == {"en", "de", *begun}


async def test_a_day_of_new_languages_begins_at_midnight_at_the_house_time(session, add_person):
    owner = await add_person("Owner", "owner")
    await update_policy_setting(
        session, key=TIME_ZONE_KEY, value="Asia/Tokyo", by=owner.id, now=datetime.now(UTC)
    )
    for language in ("de", "es", "pt"):
        await write_language_version(session, language)
    # 01:00 in Tokyo on the 11th is 16:00 in UTC on the 10th.
    now = datetime(2026, 11, 10, 16, 0, tzinfo=UTC)
    before_midnight = datetime(2026, 11, 10, 14, 59, tzinfo=UTC)
    after_midnight = datetime(2026, 11, 10, 15, 1, tzinfo=UTC)
    await session.execute(update(WrittenText).values(written_at=before_midnight))
    await session.execute(
        update(WrittenText)
        .where(WrittenText.language.in_(("de", "es")))
        .values(written_at=after_midnight)
    )
    assert await may_begin_a_language(session, now=now)
    await session.execute(
        update(WrittenText).where(WrittenText.language == "pt").values(written_at=after_midnight)
    )
    assert not await may_begin_a_language(session, now=now)
    # The next day in Tokyo, a new day of languages.
    assert await may_begin_a_language(session, now=now + timedelta(days=1))
    assert await known_languages(session) == {"en", "de", "es", "pt"}


async def test_an_expired_invitation_writes_nothing(
    chat_server: ChatServer, adult_member: Member, session_factory
):
    texts = texts_model()
    chat_server.text_model = texts
    long_ago = chat_server.clock() - INVITATION_TTL - timedelta(minutes=1)
    token = await invite(session_factory, long_ago, by=adult_member.person)
    async with a_visitor(chat_server, "it") as robin:
        expired = await robin.get(f"/invite/{token}")
        unknown = await robin.get("/invite/not-a-token")
    await chat_server.texts_idle()
    assert expired.status_code == unknown.status_code == 410
    assert '<html lang="en">' in expired.text
    assert texts.writer_calls() == []
    assert "it" not in await known(session_factory)


# --- close versions (ruling R21) -----------------------------------------------------------------


async def test_a_member_whose_language_is_close_to_a_version_begins_none(
    chat_server: ChatServer, adult_member: Member, owner_member: Member, session_factory
):
    # A house in German of Germany; Alex's browser says German, and nothing more.
    texts = texts_model()
    chat_server.text_model = texts
    alex = adult_member.person
    alex_key = await add_key(session_factory, alex)
    async with session_factory() as writer_session:
        await update_policy_setting(
            writer_session,
            key=HOUSE_LANGUAGE,
            value="de-DE",
            by=owner_member.person.id,
            now=chat_server.clock(),
        )
        await write_language_version(writer_session, "de-DE")
        await writer_session.commit()
    async with chat_server.browser(adult_member) as browser:
        browser.headers["accept-language"] = "de"
        first = await browser.get("/chat")
        await chat_server.texts_idle()
        again = await browser.get("/chat")
    await chat_server.texts_idle()
    # Their own tag, kept; the version of Germany read meanwhile, and none begun.
    assert await language_of(session_factory, alex) == "de"
    assert '<html lang="de">' in first.text
    assert in_language("de-DE", "chat.send") in plain(first.text)
    assert await known(session_factory) == {"en", "de-DE"}
    # Only their own texts are written, in their own tag, on their key, for them.
    assert names_written_for(texts) == {"Alex"}
    assert await who_paid(session_factory) == {(alex_key.id, alex.id)}
    theirs = in_their_language("chat.send", meaning("chat.send"), ForWhom("de", "Alex"))
    assert theirs in plain(again.text)


async def test_a_visitor_reads_a_close_version(client, session, owner_account):
    # A house in English of the United States: a British browser reads its version.
    await house_reads(session, "en-US")
    british = await client.get("/login", headers={"accept-language": "en-GB"})
    assert '<html lang="en-GB">' in british.text
    assert in_language("en-US", "login.submit") in plain(british.text)
    # A Canadian browser in a house with a version of France (the house's is not close).
    await write_language_version(session, "fr-FR")
    canadian = await client.get("/login", headers={"accept-language": "fr-CA"})
    assert '<html lang="fr-CA">' in canadian.text
    assert in_language("fr-FR", "login.submit") in plain(canadian.text)


async def test_an_invitation_begins_no_close_version_but_a_distant_one(
    chat_server: ChatServer, adult_member: Member, session_factory
):
    texts = texts_model()
    chat_server.text_model = texts
    alex = adult_member.person
    # Portuguese of Brazil begun before today, Chinese in simplified characters today.
    async with session_factory() as writer_session:
        for language in ("pt-BR", "zh-CN"):
            await write_language_version(writer_session, language)
        await writer_session.execute(
            update(WrittenText)
            .where(WrittenText.language == "pt-BR")
            .values(written_at=chat_server.clock() - timedelta(days=2))
        )
        await writer_session.commit()
    # Portuguese of Portugal is close to that of Brazil: no new language, the page reads it.
    token = await invite(session_factory, chat_server.clock(), by=alex)
    async with a_visitor(chat_server, "pt-PT") as robin:
        page = await robin.get(f"/invite/{token}")
    await chat_server.texts_idle()
    assert '<html lang="pt-PT">' in page.text
    assert in_language("pt-BR", "invite.submit") in plain(page.text)
    assert texts.writer_calls() == []
    # Chinese in traditional characters is not close to simplified: it is begun (the third today).
    other = await invite(session_factory, chat_server.clock(), by=alex)
    async with a_visitor(chat_server, "zh-TW") as dana:
        page = await dana.get(f"/invite/{other}")
    await chat_server.texts_idle()
    assert '<html lang="zh-TW">' in page.text
    assert in_language("zh-TW", "invite.submit") in plain(page.text)
    assert await known(session_factory) == {"en", "pt-BR", "zh-CN", "zh-TW"}


# --- the slots of a day, and who makes a language written (fix round 1) --------------------------


async def test_a_begun_language_that_keeps_nothing_still_uses_its_slot(
    chat_server: ChatServer, adult_member: Member, session_factory
):
    # Two languages begun today: the house's, and German. Every text written in Italian is refused.
    async with session_factory() as writer_session:
        await write_language_version(writer_session, "de")
        await writer_session.commit()
    texts = FakeChatModel(
        writer=in_their_language,
        checker=lambda key, text: "refused" if text.startswith("(it)") else None,
        usage=ChatUsage(0, 0),
    )
    chat_server.text_model = texts
    alex = adult_member.person
    first, second = [await invite(session_factory, chat_server.clock(), by=alex) for _ in "ab"]
    async with a_visitor(chat_server, "it") as robin:
        italian = await robin.get(f"/invite/{first}")
        await chat_server.texts_idle()
        assert '<html lang="en">' in italian.text
        assert "it" not in await known(session_factory)
        calls = len(texts.writer_calls())
        assert calls > 0
        # Italian was begun today, though it kept nothing: the third of the day.
        async with a_visitor(chat_server, "es") as dana:
            spanish = await dana.get(f"/invite/{second}")
        await chat_server.texts_idle()
        assert '<html lang="en">' in spanish.text
        assert "es" not in languages_written_for(texts)
        # Italian again: no new slot, and the texts left unwritten wait their hour.
        again = await robin.get(f"/invite/{first}")
        await chat_server.texts_idle()
    assert '<html lang="en">' in again.text
    assert len(texts.writer_calls()) == calls
    assert await known(session_factory) == {"en", "de"}


async def test_the_rest_of_a_version_is_in_flight_before_the_page_waits(
    chat_server: ChatServer, adult_member: Member, session_factory
):
    async with session_factory() as writer_session:
        await write_language_version(writer_session, "de")
        await writer_session.commit()
    held = HeldTexts(in_their_language)
    chat_server.text_model = held
    alex = adult_member.person
    writing = chat_server.app.state.text_writing
    italian = Reader(None, "it")
    # Begun for the texts of a page: the rest of the version is asked at once, behind them.
    assert await writing.begin_language("it", ["invite"], host=alex.id)
    assert writing.running == 2
    # The visitor leaves while the page waits: the version is written all the same.
    page = asyncio.create_task(writing.write_now(italian, ["invite"], within=10, host=alex.id))
    await asyncio.sleep(0.05)
    page.cancel()
    # Italian is the third language of the day, while none of its texts is kept yet.
    assert not await writing.begin_language("es", ["invite"], host=alex.id)
    held.release()
    await chat_server.texts_idle()
    version = await version_of(session_factory, "it")
    assert version["invite.submit"] == in_language("it", "invite.submit")
    assert version["chat.send"] == in_language("it", "chat.send")
    assert await known(session_factory) == {"en", "de", "it"}
    assert await who_paid(session_factory) == {(chat_server.provider_id, alex.id)}


async def test_a_language_with_default_phrases_takes_no_slot_of_the_day(
    chat_server: ChatServer, adult_member: Member, session_factory
):
    # Two languages begun today: the house's, and German; Italian, begun now, is the third.
    async with session_factory() as writer_session:
        await write_language_version(writer_session, "de")
        await writer_session.commit()
    held = HeldTexts(in_their_language)
    chat_server.text_model = held
    alex = adult_member.person
    writing = chat_server.app.state.text_writing
    assert await writing.begin_language("it", ["invite"], host=alex.id)
    running = writing.running
    # French has default phrases of its own: read at once, nothing written, no slot taken.
    assert await writing.begin_language("fr", ["invite"], host=alex.id)
    assert writing.running == running
    # A fourth language without a catalogue is still refused.
    assert not await writing.begin_language("es", ["invite"], host=alex.id)
    held.release()
    await chat_server.texts_idle()
    assert "fr" not in languages_written_for(held.model)
    assert await known(session_factory) == {"en", "de", "it"}


async def test_only_a_visitor_makes_a_new_language_on_a_link_page(
    chat_server: ChatServer, adult_member: Member, session_factory
):
    texts = texts_model()
    chat_server.text_model = texts
    token = await invite(session_factory, chat_server.clock(), by=adult_member.person)
    async with chat_server.browser(adult_member) as browser:
        # Alex's language is the house's; their browser says Italian later.
        await browser.get("/chat")
        browser.headers["accept-language"] = "it"
        page = await browser.get(f"/invite/{token}")
    await chat_server.texts_idle()
    # Signed in, Alex sees the page as they read every page: nothing is begun for it.
    assert page.status_code == 200
    assert '<html lang="en">' in page.text
    assert meaning("invite.submit") in plain(page.text)
    assert names_written_for(texts) <= {"Alex"}
    assert await known(session_factory) == {"en"}
