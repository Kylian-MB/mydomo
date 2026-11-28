"""The pages read the texts Domo wrote (spec 4.2, 4.6, scenarios 4 and 22).

After the installation, each text is the reader's own version, else their language's, else its
default phrase in their language or in English. A page shows at once the best text there is. When
Domo has a reason to (a writing note he keeps for the reader, or a language without default
phrases of its own), the texts it showed that are not the reader's own yet are written for them in
the background, on their keys: from the next display on, they are theirs. Without one, nothing is
asked. Dates and amounts are put in words by the code, in the reader's
language, at the time of the house.
"""

import asyncio
import html
import json
import re
from collections import Counter
from collections.abc import AsyncGenerator, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from chat_support import ChatServer, Member
from domo_common.i18n import t
from domo_common.meanings import SIGN, default_text, group_of, meaning
from domo_core.accounts.models import Person
from domo_core.audit.models import AuditEntry
from domo_core.llm.models import ProviderUser
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
from domo_core.policy.settings import (
    HOUSE_LANGUAGE,
    TIME_ZONE_KEY,
    load_policy_settings,
    update_policy_setting,
)
from domo_core.texts import scheduler
from domo_core.texts.models import WrittenText
from domo_core.texts.store import Reader, save_written
from domo_core.texts.upkeep import GAP_RETRY
from domo_core.texts.view import CatalogTexts, RecueilTexts, person_texts, recueil_for
from domo_core.web import texts_middleware
from fakes import FakeChatModel, TextWriter
from sqlalchemy import delete, select, update
from web_support import build_test_app

NOW = datetime(2026, 11, 6, 14, 3, tzinfo=UTC)


def plain(page: str) -> str:
    """The text of a page, its markup left out."""
    return html.unescape(re.sub(r"<[^>]+>", " ", page))


def marked(key: str, meaning_of: str, for_whom: Any) -> str:
    """What the writer of these tests writes: the meaning, with the name it was written for."""
    return f"{meaning_of} [{for_whom.name}]"


class HeldTexts:
    """The writer and the checker of the texts (a ``FakeChatModel``, whose calls cost nothing),
    each call held until ``release``."""

    def __init__(self, writer: TextWriter | None = None) -> None:
        self.model = FakeChatModel(writer=writer, usage=ChatUsage(0, 0))
        self._gate = asyncio.Event()

    def release(self) -> None:
        self._gate.set()

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


class FailingWriter:
    """A writer whose every call fails, as a provider that errs or limits its rate (each call is
    counted in ``writer_calls``); the checker is never reached."""

    def __init__(self) -> None:
        self.writer_calls = 0

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
        self.writer_calls += 1
        raise ModelCallError("rate_limited")
        yield ChatChunk("")  # a generator, which fails at its first item


class AskThatBreaks:
    """Stands in for ``app.state.text_writing``: asking for a writing raises."""

    def __init__(self) -> None:
        self.asked = 0

    def ask(self, reader: Reader, groups: Any, *, writing_note: str | None = None) -> None:
        self.asked += 1
        raise RuntimeError("the writing could not be asked")

    async def idle(self) -> None:
        return None


def groups_written(model: FakeChatModel) -> list[str]:
    """The group of each call of the writer, in order."""
    found: list[str] = []
    for call in model.writer_calls():
        first = call.messages[0]
        assert isinstance(first, ChatMessage)
        keys = list(json.loads(first.content)["texts"])
        assert len({group_of(key) for key in keys}) == 1, keys
        found.append(group_of(keys[0]))
    return found


def keys_written(model: FakeChatModel) -> list[str]:
    """The keys each call of the writer was first asked, in order."""
    found: list[str] = []
    for call in model.writer_calls():
        first = call.messages[0]
        assert isinstance(first, ChatMessage)
        found.extend(json.loads(first.content)["texts"])
    return found


NOTE = "Short sentences, and no word of jargon."


async def with_a_writing_note(session_factory: Any, person: Person) -> None:
    """``person`` has a writing note Domo keeps for them (``NOTE``): a reason to write their
    texts (committed)."""
    async with session_factory() as writer:
        await writer.execute(update(Person).where(Person.id == person.id).values(writing_note=NOTE))
        await writer.commit()


async def rows_of(session_factory: Any, person: Person) -> list[tuple[str, str]]:
    async with session_factory() as reader:
        found = await reader.execute(
            select(WrittenText.key, WrittenText.text).where(WrittenText.person_id == person.id)
        )
        return [(key, text) for key, text in found.all()]


async def audited(session_factory: Any, action: str) -> list[dict[str, Any]]:
    async with session_factory() as reader:
        found = await reader.execute(
            select(AuditEntry.details).where(AuditEntry.action == action).order_by(AuditEntry.id)
        )
        return list(found.scalars())


# --- which text shows ---------------------------------------------------------------------------


async def test_the_persons_own_text_comes_first_then_their_language_then_the_defaults(
    session, add_person
):
    alex = await add_person("Alex", "adult")
    alex.language = "fr"
    await session.flush()
    await save_written(
        session,
        Reader(None, "en"),
        {"chat.input.label": "The house's words"},
        model_id="m",
        now=NOW,
    )
    await save_written(
        session,
        Reader(None, "fr"),
        {"chat.send": "Envoyer", "chat.title": "Discussion"},
        model_id="m",
        now=NOW,
    )
    await save_written(
        session, Reader(alex.id, "fr"), {"chat.send": "Envoie"}, model_id="m", now=NOW
    )

    texts = await recueil_for(
        session, person=alex, language="fr", settings=await load_policy_settings(session)
    )

    assert texts("chat.send") == "Envoie"  # their own
    assert texts("chat.title") == "Discussion"  # their language's
    # French has default phrases: they come before the house's version (English).
    assert texts("chat.input.label") == t("chat.input.label", "fr")
    assert texts.personal == frozenset({"chat.send"})
    assert texts.language == "fr"
    # Someone else reads their language's version, never Alex's own texts.
    sam = await add_person("Sam", "child")
    theirs = await recueil_for(
        session, person=sam, language="fr", settings=await load_policy_settings(session)
    )
    assert theirs("chat.send") == "Envoyer" and theirs.personal == frozenset()
    # A language with no default phrases of its own reads the English ones, never the house's
    # version.
    german = await recueil_for(
        session, person=None, language="de", settings=await load_policy_settings(session)
    )
    assert german("chat.input.label") == meaning("chat.input.label")
    assert german("chat.send") == meaning("chat.send")


async def test_a_key_with_no_text_shows_its_default_phrase(session, adult_client):
    await session.execute(delete(WrittenText).where(WrittenText.key == "account.title"))
    await session.flush()
    texts = await recueil_for(
        session, person=None, language="en", settings=await load_policy_settings(session)
    )
    assert texts("account.title") == meaning("account.title")
    # Only a key with no meaning shows a sign.
    assert texts("no.such.key") == SIGN
    assert {"account.title", "no.such.key"} <= texts.asked
    # On the page: the default phrase, never a sign in the place of a text.
    page = await adult_client.get("/account")
    assert page.status_code == 200
    title = html.escape(meaning("account.title"), quote=False)
    assert f"<h1>{title}</h1>" in page.text
    assert f"<title>{title} - Domo</title>" in page.text
    assert SIGN not in page.text


async def test_a_template_keeps_its_holes_for_another_to_fill(session):
    # What the vault form is sent: the text with its holes, the gatekeeper fills them itself.
    texts = await recueil_for(
        session, person=None, language="en", settings=await load_policy_settings(session)
    )
    assert texts.template("vault.form.error.length") == meaning("vault.form.error.length")
    assert "{max}" in texts.template("vault.form.error.length")
    assert texts.template("no.such.key") == SIGN
    assert {"vault.form.error.length", "no.such.key"} <= texts.asked
    assert CatalogTexts("fr").template("setup.model.secret_purpose") == t(
        "setup.model.secret_purpose", "fr"
    )
    assert "{max}" in CatalogTexts("fr").template("vault.form.error.length")


async def test_what_a_person_reads_away_from_their_pages(session, add_person):
    alex = await add_person("Alex", "adult")
    await save_written(session, Reader(None, "fr"), {"chat.send": "Envoyer"}, model_id="m", now=NOW)
    # No page of his kept a language yet: the house's.
    assert (await person_texts(session, alex)).language == "en"
    alex.language = "fr-CA"
    await session.flush()
    # His language, the version of a close one where it has none of its own.
    texts = await person_texts(session, alex)
    assert texts.language == "fr-CA" and texts("chat.send") == "Envoyer"


# --- writing in the background -----------------------------------------------------------------


async def test_a_page_never_waits_for_the_writer(
    chat_server: ChatServer, adult_member: Member, session_factory
):
    await with_a_writing_note(session_factory, adult_member.person)
    texts = HeldTexts(writer=marked)
    chat_server.text_model = texts
    async with chat_server.browser(adult_member, waits_for_wakes=False) as browser:
        # The writer holds every call: the page is shown all the same, with the house's texts.
        first = await asyncio.wait_for(browser.get("/chat"), timeout=5)
        assert first.status_code == 200
        assert t("chat.send", "en") in plain(first.text)
        assert "[Alex]" not in plain(first.text)
        assert await rows_of(session_factory, adult_member.person) == []

        texts.release()
        await chat_server.texts_idle()

        # Written in the background, for Alex: the next display is theirs.
        again = await browser.get("/chat")
        assert f"{meaning('chat.send')} [Alex]" in plain(again.text)
    written = dict(await rows_of(session_factory, adult_member.person))
    assert written["chat.send"] == f"{meaning('chat.send')} [Alex]"
    # In Alex's language: until it is known, the house's.
    [call, *_] = texts.model.writer_calls()
    first_message = call.messages[0]
    assert isinstance(first_message, ChatMessage)
    asked = json.loads(first_message.content)
    assert asked["for_whom"]["language"] == "en" and asked["for_whom"]["name"] == "Alex"


async def test_two_pages_at_once_write_a_group_once(
    chat_server: ChatServer, adult_member: Member, session_factory
):
    await with_a_writing_note(session_factory, adult_member.person)
    texts = HeldTexts(writer=marked)
    chat_server.text_model = texts
    async with (
        chat_server.browser(adult_member, waits_for_wakes=False) as one,
        chat_server.browser(adult_member, waits_for_wakes=False) as other,
    ):
        pages = await asyncio.gather(one.get("/chat"), other.get("/chat"), one.get("/account"))
        assert all(page.status_code == 200 for page in pages)
        texts.release()
        await chat_server.texts_idle()
        # Shown again once written: nothing is left to write.
        await one.get("/chat")
        await chat_server.texts_idle()

    groups = groups_written(texts.model)
    assert groups and "chat" in groups and "account" in groups
    # Each key asked once (a group of more than ``WRITER_KEYS_AT_ONCE`` texts in a few calls).
    asked = keys_written(texts.model)
    assert Counter(asked).most_common(1)[0][1] == 1, asked
    # Each group kept once, with its line of the journal (no text in it).
    lines = await audited(session_factory, "texts.written")
    assert sorted(line["group"] for line in lines) == sorted(set(groups))
    assert all(
        line["person"] == str(adult_member.person.id) and line["language"] == "en" for line in lines
    )
    written = await rows_of(session_factory, adult_member.person)
    assert len(written) == len({key for key, _ in written})
    assert sum(line["count"] for line in lines) == len(written)


async def test_a_text_refused_is_not_asked_again_at_each_page(
    chat_server: ChatServer, adult_member: Member, session_factory
):
    await with_a_writing_note(session_factory, adult_member.person)
    # The checker refuses one text every time: it is left unwritten, and journaled with why.
    texts = FakeChatModel(
        usage=ChatUsage(0, 0), checker=lambda key, text: "wrong" if key == "chat.send" else None
    )
    chat_server.text_model = texts
    async with chat_server.browser(adult_member) as browser:
        await browser.get("/chat")
        await chat_server.texts_idle()
        written = dict(await rows_of(session_factory, adult_member.person))
        assert "chat.send" not in written and "chat.title" in written
        assert {"key": "chat.send", "why": "wrong"} in await audited(
            session_factory, "texts.refused"
        )
        calls = len(texts.writer_calls())
        # Shown again: the best text there is, and no call for it before an hour.
        page = await browser.get("/chat")
        await chat_server.texts_idle()
        assert t("chat.send", "en") in plain(page.text)
        assert len(texts.writer_calls()) == calls
        # An hour later, it is asked again, alone.
        chat_server.clock.advance(GAP_RETRY + timedelta(seconds=1))
        await browser.get("/chat")
        await chat_server.texts_idle()
    [again] = texts.writer_calls()[calls : calls + 1]
    first = again.messages[0]
    assert isinstance(first, ChatMessage)
    assert list(json.loads(first.content)["texts"]) == ["chat.send"]


async def test_a_writer_that_fails_is_not_asked_again_at_each_page(
    chat_server: ChatServer, adult_member: Member, session_factory
):
    await with_a_writing_note(session_factory, adult_member.person)
    # The chat page polls every few seconds: a writer that fails must not be called at each poll.
    failing = FailingWriter()
    chat_server.text_model = failing
    async with chat_server.browser(adult_member) as browser:
        await browser.get("/chat")
        await chat_server.texts_idle()
        calls = failing.writer_calls
        assert calls > 0
        page = await browser.get("/chat")
        await chat_server.texts_idle()
        # The house's texts are shown, and no new call is made before an hour.
        assert t("chat.send", "en") in plain(page.text)
        assert failing.writer_calls == calls
        # An hour later, it is asked again.
        chat_server.clock.advance(GAP_RETRY + timedelta(seconds=1))
        await browser.get("/chat")
        await chat_server.texts_idle()
        assert failing.writer_calls > calls
    assert await rows_of(session_factory, adult_member.person) == []


async def test_a_french_reader_without_a_writing_note_asks_for_nothing(
    chat_server: ChatServer, adult_member: Member, session_factory, monkeypatch
):
    # French has default phrases of its own: writing them again for Alex, with nothing to write
    # them from, would cost without a reason.
    texts = FakeChatModel(writer=marked, usage=ChatUsage(0, 0))
    chat_server.text_model = texts
    begun: list[Reader] = []
    start = scheduler.TextWriting._start

    def counted(self: Any, reader: Reader, *args: Any, **kwargs: Any) -> Any:
        begun.append(reader)
        return start(self, reader, *args, **kwargs)

    monkeypatch.setattr(scheduler.TextWriting, "_start", counted)
    async with chat_server.browser(adult_member) as browser:
        browser.headers["accept-language"] = "fr"
        for path in ("/chat", "/account") * 5:
            page = await browser.get(path)
            assert page.status_code == 200 and '<html lang="fr">' in page.text
            await chat_server.texts_idle()
    assert default_text("account.title", "fr") in plain(page.text)
    assert begun == [] and texts.writer_calls() == []
    assert await rows_of(session_factory, adult_member.person) == []
    assert await audited(session_factory, "texts.written") == []


async def test_a_writing_note_makes_the_persons_texts_written(
    chat_server: ChatServer, adult_member: Member, session_factory
):
    await with_a_writing_note(session_factory, adult_member.person)
    texts = FakeChatModel(writer=marked, usage=ChatUsage(0, 0))
    chat_server.text_model = texts
    async with chat_server.browser(adult_member) as browser:
        browser.headers["accept-language"] = "fr"
        first = await browser.get("/account")
        assert default_text("account.title", "fr") in plain(first.text)
        await chat_server.texts_idle()
        again = await browser.get("/account")
    # Written for Alex, in French, with the note Domo keeps for them.
    assert f"{meaning('account.title')} [Alex]" in plain(again.text)
    asked = [json.loads(call.messages[0].content)["for_whom"] for call in texts.writer_calls()]
    assert asked and all(
        (whom["language"], whom["name"], whom["writing_note"]) == ("fr", "Alex", NOTE)
        for whom in asked
    )
    assert dict(await rows_of(session_factory, adult_member.person))["account.title"] == (
        f"{meaning('account.title')} [Alex]"
    )


async def test_a_page_whose_writing_cannot_be_asked_still_arrives_whole(app, adult_client, caplog):
    breaking = AskThatBreaks()
    app.state.text_writing = breaking
    page = await adult_client.get("/account")
    assert page.status_code == 200
    assert page.text.rstrip().endswith("</html>")
    assert breaking.asked == 1
    assert "RuntimeError" in caplog.text


async def test_a_bug_in_reading_the_texts_is_not_hidden(app, adult_client, monkeypatch):
    # Only a database that does not answer makes signs of the texts; a bug is an error.
    async def broken(*args: Any, **kwargs: Any) -> RecueilTexts:
        raise ValueError("a bug")

    monkeypatch.setattr(texts_middleware, "recueil_for", broken)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://testserver", cookies=adult_client.cookies
    ) as browser:
        page = await browser.get("/account")
    assert page.status_code == 500
    assert SIGN not in page.text


async def keyless_paul(
    chat_server: ChatServer, add_member: Any, owner_member: Member, session_factory: Any
) -> Person:
    """Paul, an adult who may not use the general key and has no key of his own, and for whom
    Domo keeps a writing note."""
    paul = await add_member("Paul", "adult", username="paul")
    await with_a_writing_note(session_factory, paul)
    async with session_factory() as writer:
        writer.add(
            ProviderUser(
                provider_id=chat_server.provider_id,
                scope="person",
                person_id=paul.id,
                allowed=False,
                set_by=owner_member.person.id,
            )
        )
        await writer.commit()
    return paul


async def test_no_writing_for_a_person_without_a_key(
    chat_server: ChatServer, add_member, owner_member: Member, session_factory
):
    paul = await keyless_paul(chat_server, add_member, owner_member, session_factory)
    texts = HeldTexts(writer=marked)
    texts.release()
    chat_server.text_model = texts
    async with chat_server.browser(Member(paul, "paul")) as browser:
        page = await browser.get("/account")
        assert page.status_code == 200
        # The version of his language (the house's), and nothing written for him.
        assert t("account.title", "en") in plain(page.text)
        await chat_server.texts_idle()
    assert texts.model.writer_calls() == []
    assert await rows_of(session_factory, paul) == []
    assert await audited(session_factory, "texts.written") == []


async def test_a_reader_nothing_can_write_for_is_looked_at_again_only_an_hour_later(
    chat_server: ChatServer, add_member, owner_member: Member, session_factory, monkeypatch
):
    # The chat page polls every few seconds: for someone no key may write for, a poll must not
    # read their texts and look for their keys each time.
    paul = await keyless_paul(chat_server, add_member, owner_member, session_factory)
    looked: list[str] = []
    real_written, real_keys = scheduler.written, scheduler.text_keys

    async def written(session: Any, readers: Any) -> Any:
        looked.append("texts")
        return await real_written(session, readers)

    async def text_keys(session: Any, person: Person) -> Any:
        looked.append("keys")
        return await real_keys(session, person)

    monkeypatch.setattr(scheduler, "written", written)
    monkeypatch.setattr(scheduler, "text_keys", text_keys)
    async with chat_server.browser(Member(paul, "paul")) as browser:
        await browser.get("/account")
        await chat_server.texts_idle()
        assert looked == ["texts", "keys"]
        # Shown again, and again, within the hour: nothing is looked at for him.
        for _ in range(2):
            await browser.get("/account")
            await chat_server.texts_idle()
        assert looked == ["texts", "keys"]
        # An hour later, he may have a key: it is looked for again.
        chat_server.clock.advance(GAP_RETRY + timedelta(seconds=1))
        await browser.get("/account")
        await chat_server.texts_idle()
    assert looked == ["texts", "keys"] * 2
    assert await rows_of(session_factory, paul) == []


# --- the installation ---------------------------------------------------------------------------


@pytest.fixture
async def install_client(settings, session, clock) -> AsyncGenerator[httpx.AsyncClient]:
    """A browser of an instance whose first launch is not over."""
    app = build_test_app(settings, session, clock)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 123)),
        base_url="https://testserver",
        headers={"origin": "https://testserver"},
    ) as client:
        yield client


async def test_the_install_pages_keep_their_fixed_texts(install_client):
    owner_page = await install_client.get("/setup/owner", headers={"accept-language": "fr"})
    assert owner_page.status_code == 200
    assert '<html lang="fr">' in owner_page.text
    assert t("setup.owner.submit", "fr") in plain(owner_page.text)
    assert t("setup.owner.submit", "fr") != t("setup.owner.submit", "en")
    login = await install_client.get("/login", headers={"accept-language": "fr-FR,fr;q=0.9"})
    assert t("login.submit", "fr") in plain(login.text)
    english = await install_client.get("/setup/owner", headers={"accept-language": "de"})
    assert '<html lang="en">' in english.text
    assert t("setup.owner.submit", "en") in plain(english.text)


# --- dates, amounts and the language of a page ---------------------------------------------------


def _texts(language: str, zone: str = "Europe/Paris") -> RecueilTexts:
    return RecueilTexts(language=language, zone=ZoneInfo(zone), found={}, personal=frozenset())


async def test_dates_and_amounts_follow_the_language_at_the_house_time(session, add_person):
    # 14:03 in UTC is 15:03 in Paris in November.
    assert _texts("fr").date(NOW) == "6 nov. 2026, 15:03"
    assert _texts("fr").date(NOW, time=False) == "6 nov. 2026"
    assert _texts("en").date(NOW) == "Nov 6, 2026, 3:03 PM"
    assert _texts("de").date(NOW) == "06.11.2026, 15:03"
    # A day alone is that very day, at the time of any house.
    assert _texts("fr", "Pacific/Kiritimati").date(date(2026, 11, 20)) == "20 nov. 2026"
    assert _texts("en", "Pacific/Pago_Pago").date(date(2026, 11, 20)) == "Nov 20, 2026"
    # Late at night in UTC is the next day in Kiritimati.
    late = datetime(2026, 11, 6, 23, 30, tzinfo=UTC)
    assert _texts("en", "Pacific/Kiritimati").date(late, time=False) == "Nov 7, 2026"
    # Amounts: their currency, every digit, the language's way.
    assert _texts("fr").amount(Decimal("0.5"), "EUR") == "0,50\xa0€"
    assert _texts("en").amount(Decimal("20"), "USD") == "$20.00"
    assert _texts("en").amount(Decimal("0.0012"), "USD") == "$0.0012"
    assert CatalogTexts("fr").amount(Decimal("0.5"), "EUR") == "0,50\xa0€"
    # A language Babel has no data for: its primary language, else English.
    assert _texts("de-XX").date(NOW) == "06.11.2026, 15:03"
    assert _texts("xx").date(NOW) == "Nov 6, 2026, 3:03 PM"
    # The time of the house is the household's time zone.
    owner = await add_person("Owner", "owner")
    await update_policy_setting(
        session, key=TIME_ZONE_KEY, value="Asia/Tokyo", by=owner.id, now=NOW
    )
    house = await recueil_for(
        session, person=None, language="en", settings=await load_policy_settings(session)
    )
    assert house.zone == ZoneInfo("Asia/Tokyo")
    assert house.date(NOW) == "Nov 6, 2026, 11:03 PM"


async def test_html_lang_is_the_language_shown(session, adult_client, client, adult_account):
    await session.execute(
        update(Person).where(Person.id == adult_account.person_id).values(language="de")
    )
    await save_written(
        session, Reader(None, "de"), {"account.title": "Konto"}, model_id="m", now=NOW
    )
    page = await adult_client.get("/account")
    assert '<html lang="de">' in page.text
    assert "Konto" in plain(page.text)
    # A visitor whose language has no version, nor default phrases, reads English, even in a
    # house in French; a French browser reads the house's version.
    owner = await session.scalar(select(Person.id).limit(1))
    await update_policy_setting(session, key=HOUSE_LANGUAGE, value="fr", by=owner, now=NOW)
    await save_written(
        session, Reader(None, "fr"), {"login.submit": "Entrer"}, model_id="m", now=NOW
    )
    login = await client.get("/login", headers={"accept-language": "ja"})
    assert '<html lang="en">' in login.text
    assert default_text("login.submit", "en") in plain(login.text)
    french = await client.get("/login", headers={"accept-language": "fr"})
    assert '<html lang="fr">' in french.text
    assert "Entrer" in plain(french.text)


async def test_the_manifest_is_written_by_domo(session, client):
    await save_written(
        session,
        Reader(None, "en"),
        {"manifest.description": "Your own assistant, at home."},
        model_id="m",
        now=NOW,
    )
    response = await client.get("/manifest.webmanifest")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/manifest+json")
    manifest = response.json()
    assert manifest["description"] == "Your own assistant, at home."
    assert manifest["lang"] == "en"
    assert manifest["name"] == "Domo" and manifest["start_url"] == "/chat"


async def test_a_name_with_braces_is_shown_as_typed(session, add_person):
    await save_written(
        session,
        Reader(None, "en"),
        {"approvals.from": "Asked by {name}."},
        model_id="m",
        now=NOW,
    )
    texts = await recueil_for(
        session, person=None, language="en", settings=await load_policy_settings(session)
    )
    # The braces of a name are the name's: never read as a hole.
    assert texts("approvals.from", name="{name} {0} {{x}}") == "Asked by {name} {0} {{x}}."
    # A hole without its value is an error, as in the catalogs.
    with pytest.raises(KeyError):
        texts("approvals.from")
