"""What people ask Domo of the texts they read (spec 4.7, scenarios 11, 12, 13 and 29).

Each person asks for other words on their own pages (``texts_rewrite``, with an indication: a
button called "Go"), the old text back (``texts_restore``), another language (``texts_language``)
and a writing note (``texts_note``), children and guests too; Domo finds their texts first
(``texts_find``). What is written is checked like any text: a button that confirms is never called
"Cancel", and the old text stays. Someone else's texts, writing note or language need a right of
the owner (``texts_for_someone``); the version of a language is the owner's to have rewritten
(``texts_rewrite_language``). Domo's choices on writing are kept with his other choices
(``writing_choose``). The writing note and the indication of a rewrite reach the writer as data,
and never the audit log.
"""

import asyncio
import json
from collections.abc import AsyncGenerator, Sequence
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from chat_support import ChatServer
from domo_common.i18n import t
from domo_common.meanings import keys_of_group
from domo_core.accounts.models import Person
from domo_core.audit.models import AuditEntry
from domo_core.confirmations.describe import describe_for_people
from domo_core.llm.choices import load_choices
from domo_core.llm.models import ProviderUser
from domo_core.llm.spending_models import ModelCall
from domo_core.llm.types import (
    ChatChunk,
    ChatMessage,
    ChatUsage,
    HistoryItem,
    ToolCall,
    ToolChoice,
    ToolDefinition,
)
from domo_core.policy.catalog import DEFAULT_POLICY
from domo_core.policy.roles import Actor, as_role
from domo_core.texts.languages import known_languages
from domo_core.texts.models import WrittenText
from domo_core.texts.scheduler import reads_another_language
from domo_core.texts.store import Reader
from domo_core.texts.view import REFERENCE, person_texts
from domo_core.tools import text_tools
from domo_core.tools.context import ToolContext
from domo_core.tools.registry import TOOLS, tools_for
from fakes import FakeChatModel, TextChecker, TextWriter, is_text_writer_call
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError
from test_spending import add_key
from web_support import write_language_version

INDICATION = "Call this button exactly: Go"
CANCEL_WHY = "a button that confirms cannot say Cancel"
WHY = "they like to see their requests at once"


def texts_model(writer: TextWriter | None = None, checker: TextChecker | None = None) -> Any:
    """The writer and the checker of the texts (calls that cost nothing)."""
    return FakeChatModel(usage=ChatUsage(0, 0), writer=writer, checker=checker)


def follows_the_indication(key: str, meaning: str, for_whom: Any) -> str:
    """A writer that writes "Go" when it is given an indication, else the meaning."""
    return "Go" if for_whom.indication else meaning


def in_its_language(key: str, meaning: str, for_whom: Any) -> str:
    """A writer that marks each text with the language it is written in."""
    return f"({for_whom.language}) {meaning}"


async def own_texts(session_factory, person: Person) -> dict[str, str]:
    """The texts written for ``person`` alone, by key."""
    async with session_factory() as reader:
        rows = await reader.execute(
            select(WrittenText.key, WrittenText.text).where(WrittenText.person_id == person.id)
        )
        return {key: text for key, text in rows.all()}


async def version_texts(session_factory, language: str) -> dict[str, str]:
    """The texts of the version of ``language``, by key."""
    async with session_factory() as reader:
        rows = await reader.execute(
            select(WrittenText.key, WrittenText.text).where(WrittenText.language == language)
        )
        return {key: text for key, text in rows.all()}


async def reads(session_factory, person: Person, key: str) -> str:
    """The text of ``key`` that ``person`` reads now."""
    async with session_factory() as reader:
        return (await person_texts(reader, person))(key)


def asked_of_the_writer(model: Any) -> list[dict[str, Any]]:
    """Who each call of the writer was for (its ``for_whom``), in order."""
    told: list[dict[str, Any]] = []
    for call in model.writer_calls():
        first = call.messages[0]
        assert isinstance(first, ChatMessage)
        told.append(json.loads(first.content)["for_whom"])
    return told


def send_button(label: str) -> str:
    """The send button of the chat page, as it is shown with ``label``."""
    return f'data-action="send">{label}</button>'


class HeldWriter:
    """The writer and the checker of the texts (a ``FakeChatModel``), each call of the writer held
    until ``release``: a writing under way."""

    def __init__(self, writer: TextWriter | None = None) -> None:
        self.model = texts_model(writer=writer)
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
        if is_text_writer_call(system):
            await self._gate.wait()
        async for item in self.model.stream(
            system=system,
            messages=messages,
            max_output_tokens=max_output_tokens,
            tools=tools,
            tool_choice=tool_choice,
        ):
            yield item


def acting_as(tool_context: ToolContext, server: ChatServer, person: Person) -> ToolContext:
    """A call made by ``person`` on the pilots' server by hand, its texts written in the
    background as in a turn."""
    return replace(
        tool_context,
        actor=Actor(person.id, as_role(person.role)),
        person=person,
        text_writing=server.app.state.text_writing,
    )


async def with_a_writing_note(session_factory, person: Person) -> None:
    """``person`` has a writing note Domo keeps for them: a reason to write their texts as their
    pages show them (committed)."""
    async with session_factory() as writer:
        await writer.execute(
            update(Person).where(Person.id == person.id).values(writing_note="Plain words.")
        )
        await writer.commit()


async def writing_calls_of(session_factory, person: Person) -> int:
    """How many calls of the writer were counted for ``person``."""
    async with session_factory() as reader:
        return (
            await reader.scalar(
                select(func.count())
                .select_from(ModelCall)
                .where(ModelCall.person_id == person.id, ModelCall.task == "writing")
            )
            or 0
        )


async def test_a_person_renames_one_of_their_buttons(
    adult_chat, owner_chat, session_factory, last_audit_entry, audit_dump
):
    # Scenario 11: "call this button Go".
    server = adult_chat.server
    server.text_model = texts_model(writer=follows_the_indication)
    alex = adult_chat.person
    await with_a_writing_note(session_factory, alex)
    await adult_chat.get("/chat")
    await adult_chat.texts_idle()
    assert (await own_texts(session_factory, alex))["chat.send"] == "Send"

    found = await adult_chat.say_with_tool("texts_find", {"words": "send"})
    assert found.actions == [{"tool": "texts_find", "status": "done"}]
    [result] = found.model_results
    assert {"key": "chat.send", "group": "chat", "text": "Send"} in result["found"]
    assert len(result["found"]) <= 20

    reply = await adult_chat.say_with_tool(
        "texts_rewrite", {"keys": ["chat.send"], "indication": INDICATION}, judge="do"
    )
    assert reply.actions == [{"tool": "texts_rewrite", "status": "done"}]
    # One key is written at once, and the result says it.
    [result] = reply.model_results
    assert result["key"] == "chat.send" and result["text"] == "Go"
    assert (await own_texts(session_factory, alex))["chat.send"] == "Go"
    assert send_button("Go") in (await adult_chat.get("/chat")).text
    # For Alex only.
    assert send_button("Send") in (await owner_chat.get("/chat")).text
    entry = await last_audit_entry("texts.rewrite_asked")
    assert entry.actor == f"person:{alex.id}"
    assert entry.details == {"person": f"person:{alex.id}", "scope": "keys", "count": 1}
    assert INDICATION not in await audit_dump()

    # A key that is not a text people read is refused, and so is more than one way to say which.
    for wrong in ({"keys": ["chat.nothing"]}, {"keys": ["setup.title"]}, {"groups": ["setup"]}):
        refused = await adult_chat.say_with_tool("texts_rewrite", wrong, judge="do")
        assert refused.actions[0]["status"] == "invalid", wrong
    both = await adult_chat.say_with_tool(
        "texts_rewrite", {"keys": ["chat.send"], "all": True}, judge="do"
    )
    assert both.actions == [{"tool": "texts_rewrite", "status": "invalid", "reason": "invalid"}]


async def test_restore_brings_back_the_old_button(adult_chat, session_factory, last_audit_entry):
    server = adult_chat.server
    server.text_model = texts_model(writer=follows_the_indication)
    alex = adult_chat.person
    await with_a_writing_note(session_factory, alex)
    await adult_chat.get("/chat")
    await adult_chat.texts_idle()
    await adult_chat.say_with_tool(
        "texts_rewrite", {"keys": ["chat.send"], "indication": INDICATION}, judge="do"
    )
    assert send_button("Go") in (await adult_chat.get("/chat")).text

    # "Put it back as it was."
    reply = await adult_chat.say_with_tool("texts_restore", {"keys": ["chat.send"]}, judge="do")
    assert reply.actions == [{"tool": "texts_restore", "status": "done"}]
    [result] = reply.model_results
    assert result["restored"] == ["chat.send"]
    assert (await own_texts(session_factory, alex))["chat.send"] == "Send"
    assert send_button("Send") in (await adult_chat.get("/chat")).text
    entry = await last_audit_entry("texts.restored")
    assert entry.details == {"person": f"person:{alex.id}", "count": 1}
    # Nothing older: nothing comes back, and the result says so.
    again = await adult_chat.say_with_tool("texts_restore", {"keys": ["chat.send"]}, judge="do")
    assert again.actions == [{"tool": "texts_restore", "status": "done"}]
    assert again.model_results[0]["restored"] == []
    assert (await own_texts(session_factory, alex))["chat.send"] == "Send"


async def test_putting_texts_back_waits_for_a_rewrite_under_way(
    adult_chat, tool_context, session_factory, last_audit_entry, monkeypatch
):
    # "Write my texts shorter", then at once "no, put them back": the text they had comes back,
    # never an older one under the shorter one that lands afterwards.
    server = adult_chat.server
    alex = adult_chat.person
    await with_a_writing_note(session_factory, alex)
    await adult_chat.get("/chat")
    await adult_chat.texts_idle()
    as_alex = acting_as(tool_context, server, alex)

    def shorter(key: str, meaning: str, for_whom: Any) -> str:
        return f"(short) {meaning}" if for_whom.indication else meaning

    held = HeldWriter(writer=shorter)
    server.text_model = held
    rewrite = await TOOLS["texts_rewrite"].execute(
        {"groups": ["chat"], "indication": "Shorter"}, as_alex
    )
    assert rewrite.model_result["in_background"] is True
    restoring = asyncio.create_task(
        TOOLS["texts_restore"].execute({"keys": ["chat.send"]}, as_alex)
    )
    await asyncio.sleep(0.3)
    # It waits for the rewrite: nothing is brought back meanwhile.
    assert not restoring.done()
    assert (await own_texts(session_factory, alex))["chat.send"] == "Send"
    held.release()
    restored = await restoring
    assert (restored.status, restored.model_result["restored"]) == ("done", ["chat.send"])
    own = await own_texts(session_factory, alex)
    assert own["chat.send"] == "Send" and own["chat.title"] == "(short) Chat"
    entry = await last_audit_entry("texts.restored")
    assert entry.details == {"person": f"person:{alex.id}", "count": 1}

    # A rewrite longer than the wait: the restore goes on in the background, and is said so.
    monkeypatch.setattr(text_tools, "REWRITE_NOW_SECONDS", 0.1)
    held = HeldWriter(writer=shorter)
    server.text_model = held
    await TOOLS["texts_rewrite"].execute({"groups": ["chat"], "indication": "Shorter"}, as_alex)
    later = await TOOLS["texts_restore"].execute({"keys": ["chat.send"]}, as_alex)
    assert later.status == "done" and later.model_result["in_background"] is True
    assert later.card["key"] == "action.card.done.texts.restore.later"
    held.release()
    await server.texts_idle()
    own = await own_texts(session_factory, alex)
    assert own["chat.send"] == "Send" and own["chat.title"] == "(short) Chat"


async def test_a_confirm_button_cannot_be_called_cancel(adult_chat, session_factory):
    # Scenario 12: the checker refuses the new text; the old one stays, and Domo is told why.
    server = adult_chat.server
    server.text_model = texts_model(
        writer=lambda key, meaning, for_whom: "Cancel" if for_whom.indication else meaning,
        checker=lambda key, text: CANCEL_WHY if text == "Cancel" else None,
    )
    alex = adult_chat.person
    reply = await adult_chat.say_with_tool(
        "texts_rewrite",
        {"keys": ["confirmation.confirm"], "indication": "Call this button: Cancel"},
        judge="do",
    )
    assert reply.actions == [{"tool": "texts_rewrite", "status": "failed"}]
    [result] = reply.model_results
    assert result["reason"] == "refused" and result["key"] == "confirmation.confirm"
    assert result["why"] == CANCEL_WHY
    assert "Cancel" not in (await own_texts(session_factory, alex)).values()
    assert await reads(session_factory, alex, "confirmation.confirm") == "Confirm"
    # The refusal is journaled with why, by the writing itself.
    async with session_factory() as reader:
        refused = await reader.execute(
            select(AuditEntry.details).where(AuditEntry.action == "texts.refused")
        )
        assert {"key": "confirmation.confirm", "why": CANCEL_WHY} in refused.scalars().all()


async def test_a_language_change_rewrites_the_persons_version(
    adult_chat, session_factory, person_row, current_settings, last_audit_entry
):
    # Scenario 13: "speak Spanish to me".
    server = adult_chat.server
    texts = server.text_model = texts_model(writer=in_its_language)
    alex = adult_chat.person
    await with_a_writing_note(session_factory, alex)
    await adult_chat.get("/chat")
    await adult_chat.texts_idle()
    assert (await own_texts(session_factory, alex))["chat.send"] == "(en) Send"

    reply = await adult_chat.say_with_tool("texts_language", {"language": "es"}, judge="do")
    assert reply.actions == [{"tool": "texts_language", "status": "done"}]
    assert (await person_row(alex)).language == "es"
    # Their own version is written again, in Spanish, on their keys: nothing of it stays English.
    own = await own_texts(session_factory, alex)
    assert own["chat.send"] == "(es) Send"
    assert all(text.startswith("(es) ") for text in own.values()), own
    assert {"language": "es", "name": "Alex"} in [
        {"language": told["language"], "name": told["name"]} for told in asked_of_the_writer(texts)
    ]
    assert send_button("(es) Send") in (await adult_chat.get("/chat")).text
    entry = await last_audit_entry("texts.language_changed")
    assert entry.details == {"person": f"person:{alex.id}", "language": "es"}
    # Only the owner's own language moves the house's.
    assert (await current_settings()).house_language == "en"

    # A tag in another case is taken in its usual one; anything else is no language.
    reply = await adult_chat.say_with_tool("texts_language", {"language": "es-mx"}, judge="do")
    assert reply.actions == [{"tool": "texts_language", "status": "done"}]
    assert (await person_row(alex)).language == "es-MX"
    for wrong in ("Spanish", "es_ES", "", "es-ES-x-private"):
        refused = await adult_chat.say_with_tool("texts_language", {"language": wrong}, judge="do")
        assert refused.actions == [
            {"tool": "texts_language", "status": "invalid", "reason": "invalid"}
        ], wrong
    assert (await person_row(alex)).language == "es-MX"


async def test_the_owners_language_moves_the_house_language(
    owner_chat, session_factory, person_row, current_settings, last_audit_entry
):
    server = owner_chat.server
    texts = server.text_model = texts_model(writer=in_its_language)
    owner = owner_chat.person
    await with_a_writing_note(session_factory, owner)
    await owner_chat.get("/chat")
    await owner_chat.texts_idle()
    assert (await own_texts(session_factory, owner))["chat.send"] == "(en) Send"
    reply = await owner_chat.say_with_tool("texts_language", {"language": "es"}, judge="do")
    assert reply.actions == [{"tool": "texts_language", "status": "done"}]
    assert (await person_row(owner)).language == "es"
    assert (await current_settings()).house_language == "es"
    entry = await last_audit_entry("policy.updated")
    assert entry.details == {"key": "house.language", "old": None, "new": "es"}
    # The house's version in Spanish is written in the background, on the house's key.
    async with session_factory() as reader:
        assert "es" in await known_languages(reader)
    spanish = await version_texts(session_factory, "es")
    assert spanish["chat.send"] == "(es) Send"
    told = asked_of_the_writer(texts)
    assert any(entry["language"] == "es" and entry["name"] is None for entry in told)

    # Two more languages begun today: the house may begin no other one, so its language stays
    # (it never reads a language with no version), and the owner's own changes all the same.
    async with session_factory() as writer:
        await write_language_version(writer, "de")
        await write_language_version(writer, "it")
        await writer.commit()
    reply = await owner_chat.say_with_tool("texts_language", {"language": "pt"}, judge="do")
    assert reply.actions == [{"tool": "texts_language", "status": "done"}]
    assert reply.model_results[0]["house_language"] == "es"
    assert (await person_row(owner)).language == "pt"
    assert (await current_settings()).house_language == "es"
    assert await version_texts(session_factory, "pt") == {}
    # A language that has its version already: the house takes it, and nothing is begun for
    # it; only the owner's own texts are written again, in German.
    calls = len(texts.writer_calls())
    reply = await owner_chat.say_with_tool("texts_language", {"language": "de"}, judge="do")
    assert reply.model_results[0]["house_language"] == "de"
    assert (await current_settings()).house_language == "de"
    told = asked_of_the_writer(texts)[calls:]
    assert told and all((entry["name"], entry["language"]) == ("Owner", "de") for entry in told)
    assert (await own_texts(session_factory, owner))["chat.send"] == "(de) Send"


async def test_the_owners_change_of_language_says_the_house_follows(tool_context, adult_member):
    # Whoever judges it, or says yes to it, reads that the house's language follows.
    owner_said = await TOOLS["texts_language"].describe(
        {"language": "es"}, tool_context.actor, tool_context
    )
    assert owner_said.facts == {"house_follows": True}
    assert describe_for_people(owner_said, {"language": "es"}, REFERENCE) == t(
        "action.describe.texts.language.house", "en", language="es"
    )
    # The house's own language, or anyone else's change: their own pages only.
    alex = adult_member.person
    as_alex = replace(tool_context, actor=Actor(alex.id, "adult"), person=alex)
    for actor, context in ((tool_context.actor, tool_context), (as_alex.actor, as_alex)):
        language = "en" if context is tool_context else "es"
        said = await TOOLS["texts_language"].describe({"language": language}, actor, context)
        assert said.facts == {"house_follows": False}
        assert describe_for_people(said, {"language": language}, REFERENCE) == t(
            "action.describe.texts.language", "en", language=language
        )


async def test_someone_elses_texts_need_a_right(
    adult_chat, owner_chat, child_member, session_factory, person_row, grant, last_audit_entry
):
    # Scenario 29: in Alex's conversation, Domo cannot change Sam's texts, writing note or
    # language; nor the owner's, by default.
    server = adult_chat.server
    texts = server.text_model = texts_model(writer=follows_the_indication)
    sam = child_member.person
    for what, more in (
        ("rewrite", {"all": True}),
        ("restore", {"keys": ["chat.send"]}),
        ("language", {"language": "es"}),
        ("note", {"note": "Short sentences."}),
    ):
        arguments = {"person": "Sam", "what": what, **more}
        for chat in (adult_chat, owner_chat):
            reply = await chat.say_with_tool("texts_for_someone", arguments, judge="do")
            assert reply.actions == [
                {"tool": "texts_for_someone", "status": "refused", "reason": "not_allowed"}
            ], (what, chat.person.display_name)
    row = await person_row(sam)
    assert (row.language, row.writing_note) == (None, None)
    assert "texts_for_someone" not in [
        tool.name for tool in tools_for("adult", TOOLS, DEFAULT_POLICY)
    ]

    # With a right of the owner, Alex may.
    await grant("texts.for_someone", person=adult_chat.person, access="allow")
    # Sam has texts of his own; Alex has a key of his own, which Sam may not use.
    async with server.browser(child_member) as sams_browser:
        await sams_browser.get("/chat")
    await server.texts_idle()
    alexs_key = await add_key(session_factory, adult_chat.person.id)
    counted_for_sam = await writing_calls_of(session_factory, sam)
    reply = await adult_chat.say_with_tool(
        "texts_for_someone",
        {"person": "Sam", "what": "rewrite", "groups": ["chat"], "indication": INDICATION},
        judge="do",
    )
    assert reply.actions == [{"tool": "texts_for_someone", "status": "done"}]
    assert reply.model_results[0]["count"] == len(keys_of_group("chat"))
    assert (await own_texts(session_factory, sam))["chat.send"] == "Go"
    assert {"name": "Sam", "indication": INDICATION} in [
        {"name": told["name"], "indication": told["indication"]}
        for told in asked_of_the_writer(texts)
    ]
    # Written on Sam's keys, and counted for him (socle 7): never on Alex's key.
    assert await writing_calls_of(session_factory, sam) > counted_for_sam
    async with session_factory() as reader:
        on_alexs_key = await reader.scalar(
            select(func.count())
            .select_from(ModelCall)
            .where(ModelCall.provider_id == alexs_key.id, ModelCall.person_id == sam.id)
        )
    assert on_alexs_key == 0
    reply = await adult_chat.say_with_tool(
        "texts_for_someone",
        {"person": "Sam", "what": "note", "note": "Short sentences."},
        judge="do",
    )
    assert reply.actions == [{"tool": "texts_for_someone", "status": "done"}]
    assert "Short sentences." not in json.dumps(reply.model_results)
    reply = await adult_chat.say_with_tool(
        "texts_for_someone", {"person": "Sam", "what": "language", "language": "fr"}, judge="do"
    )
    assert reply.actions == [{"tool": "texts_for_someone", "status": "done"}]
    row = await person_row(sam)
    assert (row.language, row.writing_note) == ("fr", "Short sentences.")
    entry = await last_audit_entry("texts.note_changed")
    assert entry.actor == f"person:{adult_chat.person.id}"
    assert entry.details == {"person": f"person:{sam.id}"}
    # Their own texts are theirs to change with their own tools.
    reply = await adult_chat.say_with_tool(
        "texts_for_someone", {"person": "Alex", "what": "note", "note": "x"}, judge="do"
    )
    assert reply.actions == [
        {"tool": "texts_for_someone", "status": "invalid", "reason": "invalid"}
    ]
    # Only the parameters of what is done.
    reply = await adult_chat.say_with_tool(
        "texts_for_someone", {"person": "Sam", "what": "note", "language": "fr"}, judge="do"
    )
    assert reply.actions == [
        {"tool": "texts_for_someone", "status": "invalid", "reason": "invalid"}
    ]


async def test_a_child_changes_their_own_texts(
    child_chat, session_factory, person_row, count_confirmations
):
    server = child_chat.server
    server.text_model = texts_model(writer=follows_the_indication)
    sam = child_chat.person
    for tool, arguments in (
        ("texts_note", {"note": "Say tu, and keep it short."}),
        ("texts_rewrite", {"keys": ["chat.send"], "indication": INDICATION}),
        ("texts_restore", {"keys": ["chat.send"]}),
        ("texts_language", {"language": "fr"}),
    ):
        reply = await child_chat.say_with_tool(tool, arguments, judge="do")
        # Nobody's agreement is needed: it is their own.
        assert reply.actions == [{"tool": tool, "status": "done"}], tool
    assert await count_confirmations() == 0
    row = await person_row(sam)
    assert (row.language, row.writing_note) == ("fr", "Say tu, and keep it short.")
    # The note rewrites nothing by itself; Domo is given the one it replaced, to keep it whole.
    reply = await child_chat.say_with_tool("texts_note", {"note": ""}, judge="do")
    assert reply.actions == [{"tool": "texts_note", "status": "done"}]
    assert reply.model_results[0]["before"] == "Say tu, and keep it short."
    assert (await person_row(sam)).writing_note is None


async def test_only_the_owner_rewrites_a_language_version_by_default(
    owner_chat, adult_chat, session_factory, grant, last_audit_entry
):
    server = owner_chat.server
    server.text_model = texts_model(writer=follows_the_indication)
    arguments = {"language": "en", "keys": ["chat.send"], "indication": INDICATION}
    reply = await adult_chat.say_with_tool("texts_rewrite_language", arguments, judge="do")
    assert reply.actions == [
        {"tool": "texts_rewrite_language", "status": "refused", "reason": "not_allowed"}
    ]
    assert "texts_rewrite_language" in [
        tool.name for tool in tools_for("owner", TOOLS, DEFAULT_POLICY)
    ]
    assert "texts_rewrite_language" not in [
        tool.name for tool in tools_for("adult", TOOLS, DEFAULT_POLICY)
    ]

    reply = await owner_chat.say_with_tool("texts_rewrite_language", arguments, judge="do")
    assert reply.actions == [{"tool": "texts_rewrite_language", "status": "done"}]
    assert (await version_texts(session_factory, "en"))["chat.send"] == "Go"
    entry = await last_audit_entry("texts.version_rewrite_asked")
    assert entry.details == {"language": "en", "scope": "keys", "count": 1}
    # A language that has no version is never begun this way.
    reply = await owner_chat.say_with_tool(
        "texts_rewrite_language", {"language": "it", "all": True}, judge="do"
    )
    assert reply.actions == [
        {"tool": "texts_rewrite_language", "status": "invalid", "reason": "not_found"}
    ]
    assert await version_texts(session_factory, "it") == {}

    # Given like any right.
    await grant("texts.language_version", person=adult_chat.person, access="allow")
    reply = await adult_chat.say_with_tool(
        "texts_rewrite_language", {"language": "en", "keys": ["chat.title"]}, judge="do"
    )
    assert reply.actions == [{"tool": "texts_rewrite_language", "status": "done"}]


async def test_writing_choices_are_saved_and_audited(
    owner_chat, guest_chat, session_factory, last_audit_entry
):
    await owner_chat.say_with_tool(
        "models_choose", {"why": "spend less", "task": "conversation", "level": "small"}
    )
    arguments = {"write_now": ["requests", "done"], "arrival": "when_new", "why": WHY}
    reply = await owner_chat.say_with_tool("writing_choose", arguments, judge="do_not")
    # It changes nothing in the household: the judge is not asked.
    assert reply.actions == [{"tool": "writing_choose", "status": "done"}]
    async with session_factory() as reader:
        choices = await load_choices(reader, None)
    assert (choices.write_now, choices.arrival) == (("requests", "done"), "when_new")
    # The choices of models are kept.
    assert choices.tasks["conversation"].level == "small"
    entry = await last_audit_entry("writing.choices_changed")
    assert entry.details == {
        "changes": {"write_now": ["requests", "done"], "arrival": "when_new"},
        "why": WHY,
    }
    # And a change of the models keeps them.
    await owner_chat.say_with_tool(
        "models_choose", {"why": "spend less", "task": "conversation", "level": "medium"}
    )
    async with session_factory() as reader:
        choices = await load_choices(reader, None)
    assert (choices.write_now, choices.arrival) == (("requests", "done"), "when_new")
    # Nothing at once is a choice too.
    await owner_chat.say_with_tool("writing_choose", {"write_now": [], "why": WHY})
    async with session_factory() as reader:
        assert (await load_choices(reader, None)).write_now == ()

    for wrong in (
        {"why": WHY},
        {"write_now": ["always"], "why": WHY},
        {"arrival": "sometimes", "why": WHY},
        {"arrival": "never"},
    ):
        reply = await owner_chat.say_with_tool("writing_choose", wrong)
        assert reply.actions == [
            {"tool": "writing_choose", "status": "invalid", "reason": "invalid"}
        ], wrong
    # Not a guest's.
    reply = await guest_chat.say_with_tool("writing_choose", {"arrival": "never", "why": WHY})
    assert reply.actions == [
        {"tool": "writing_choose", "status": "refused", "reason": "not_allowed"}
    ]


async def test_the_writing_note_never_reaches_the_audit(
    adult_chat, session_factory, person_row, last_audit_entry, audit_dump, caplog
):
    note = "Say tu, and never a joke about cards (note-mark-7)."
    indication = "Shorter, please (indication-mark-9)."
    server = adult_chat.server
    texts = server.text_model = texts_model()
    alex = adult_chat.person
    await adult_chat.get("/chat")
    await adult_chat.texts_idle()

    reply = await adult_chat.say_with_tool("texts_note", {"note": note}, judge="do")
    assert reply.actions == [{"tool": "texts_note", "status": "done"}]
    assert (await person_row(alex)).writing_note == note
    # It rewrites nothing by itself.
    calls = len(texts.writer_calls())
    await adult_chat.texts_idle()
    assert len(texts.writer_calls()) == calls

    # A whole page, in the background: the result says how many.
    reply = await adult_chat.say_with_tool(
        "texts_rewrite", {"groups": ["chat"], "indication": indication}, judge="do"
    )
    assert reply.actions == [{"tool": "texts_rewrite", "status": "done"}]
    assert reply.model_results[0]["count"] == len(keys_of_group("chat"))
    await adult_chat.texts_idle()
    # The writer read both, as data.
    told = asked_of_the_writer(texts)[calls:]
    rewritten = [entry for entry in told if entry["indication"] is not None]
    assert {(entry["writing_note"], entry["indication"]) for entry in rewritten} == {
        (note, indication)
    }
    # With the note, the pages Alex sees are written for them too, after it.
    assert {entry["writing_note"] for entry in told} == {note}

    entry = await last_audit_entry("texts.note_changed")
    assert entry.details == {"person": f"person:{alex.id}"}
    dump = await audit_dump()
    assert "note-mark-7" not in dump and "indication-mark-9" not in dump
    assert "note-mark-7" not in caplog.text and "indication-mark-9" not in caplog.text
    # Too long, or not one plain line: refused.
    for wrong in ("x" * 501, "two\nlines"):
        refused = await adult_chat.say_with_tool("texts_note", {"note": wrong}, judge="do")
        assert refused.actions[0]["status"] == "invalid"
    refused = await adult_chat.say_with_tool(
        "texts_rewrite", {"all": True, "indication": "x" * 301}, judge="do"
    )
    assert refused.actions[0]["status"] == "invalid"


async def test_a_rewrite_for_someone_without_a_key_writes_nothing(
    chat_server, add_member, owner_member, tool_context, session_factory, audit_actions
):
    # Paul may not use the general key, and has no key of his own: nothing can be written for him
    # (socle 7), and the result says so.
    paul = await add_member("Paul", "adult", username="paul")
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
    texts = chat_server.text_model = texts_model(writer=follows_the_indication)
    as_paul = acting_as(tool_context, chat_server, paul)
    # He has no texts of his own yet: there is nothing to write again, and no line for nothing.
    nothing = await TOOLS["texts_rewrite"].execute({"all": True}, as_paul)
    assert nothing.status == "done" and nothing.model_result["count"] == 0
    assert nothing.card["key"] == "action.card.done.texts.rewrite.none"
    assert "texts.rewrite_asked" not in await audit_actions()

    one = await TOOLS["texts_rewrite"].execute(
        {"keys": ["chat.send"], "indication": INDICATION}, as_paul
    )
    assert one.status == "failed" and one.model_result["reason"] == "not_written"
    assert one.card["key"] == "action.card.failed.texts.rewrite.unwritten"
    await chat_server.texts_idle()
    assert texts.writer_calls() == []
    assert await own_texts(session_factory, paul) == {}


async def test_a_version_written_again_begins_no_language(
    chat_server, tool_context, session_factory, live_clock
):
    # French has had its version for days; English (the house's, written by the tests today) and
    # German were begun today. Writing the French version again takes none of the day's slots.
    async with session_factory() as writer:
        await write_language_version(writer, "fr")
        await writer.execute(
            update(WrittenText)
            .where(WrittenText.language == "fr")
            .values(written_at=live_clock() - timedelta(days=3))
        )
        await write_language_version(writer, "de")
        await writer.commit()
    held = HeldWriter()
    chat_server.text_model = held
    as_owner = replace(tool_context, text_writing=chat_server.app.state.text_writing)
    rewrite = await TOOLS["texts_rewrite_language"].execute(
        {"language": "fr", "all": True}, as_owner
    )
    assert rewrite.model_result["in_background"] is True
    writing = chat_server.app.state.text_writing
    assert await writing.begin_language("it", (), host=None)
    # The third of the day: no fourth.
    assert not await writing.begin_language("pt", (), host=None)
    held.release()
    await chat_server.texts_idle()


async def test_a_text_kept_holds_the_persons_language_until_it_is_kept(add_member, session_factory):
    # What is kept for someone holds their row: a change of their language, which locks it, waits
    # until it is kept (Ruling R17 holds even when both meet).
    alex = await add_member("Alex", "adult")
    async with session_factory() as keeping:
        assert not await reads_another_language(keeping, Reader(alex.id, "en"), hold=True)
        async with session_factory() as changing:
            with pytest.raises(DBAPIError):
                await changing.execute(
                    select(Person).where(Person.id == alex.id).with_for_update(nowait=True)
                )
    async with session_factory() as changing:
        await changing.execute(
            select(Person).where(Person.id == alex.id).with_for_update(nowait=True)
        )
