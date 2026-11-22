"""The head of a card written on the moment, for each of its readers (spec 4.4, scenarios 7, 8, 9).

The top of a card (what the action does, who asks and who must say yes, how to answer, what
changed, a remark and what Domo could not tell) is written by the writer for each reader, from
what the card shows today in English and the facts of the exact action, then checked by the code
and the checker before it is kept with the card. The one who asks reads theirs in their own turn
(``CARD_HEAD_SECONDS`` at most, else the texts' parts); anyone else who reads the card has theirs
written in the background, on their own keys. Someone else's words (the judge's reason, the
question of the rights, a note's words) are put in by the code: the writer never reads them. The
yes runs the action stored, whatever the head says.

The chat pilots talk with Domo on a live server (see ``chat_support.py``); the writer of the
texts is the server's ``text_model``.
"""

import asyncio
import dataclasses
import html
import json
import re
import uuid
from collections.abc import AsyncGenerator, Sequence
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from chat_support import confirmation_cards, dump
from domo_common.i18n import t
from domo_common.meanings import SIGN
from domo_core.accounts.models import Person
from domo_core.audit.models import AuditEntry
from domo_core.chat.models import MessageAction
from domo_core.confirmations.models import Confirmation
from domo_core.household.models import Invitation
from domo_core.llm.choices import HOUSEHOLD_SCOPE, Choices, save_choices
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
from domo_core.notes.models import Note
from domo_core.policy.models import PolicyRight
from domo_core.texts import cards
from domo_core.texts.card_heads import head_facts_of
from domo_core.texts.cards import CardOf, head_of
from domo_core.texts.formatting import local_date
from domo_core.texts.models import CardText
from fakes import FakeCall, FakeChatModel
from sqlalchemy import delete, func, select, update
from web_support import in_language, reads_in

ROBIN_FOR_TWO_DAYS = {"display_name": "Robin", "role": "guest", "guest_days": 2}
# How the request of Robin's invitation reads, in English.
INVITE_ROBIN = "Invite “Robin” to join the household as a guest, for 2 days."


def marked(key: str, meaning: str, for_whom: Any) -> str:
    """What the writer of these tests writes: a head is its meaning, signed with its reader's
    name; any other text its meaning as it is."""
    return f"{meaning} [{for_whom.name}]" if key == "head" else meaning


def asked_of(call: FakeCall) -> dict[str, Any]:
    """The JSON the writer or the checker was sent."""
    first = call.messages[0]
    assert isinstance(first, ChatMessage)
    return json.loads(first.content)


def head_calls(calls: Sequence[FakeCall]) -> list[FakeCall]:
    """The calls that wrote, or checked, the head of a card."""
    return [call for call in calls if "head" in asked_of(call)["texts"]]


def writes_heads(chat_server: Any, **options: Any) -> FakeChatModel:
    """The writer and the checker of the server's texts, from now on: ``marked`` heads."""
    model = FakeChatModel(writer=marked, usage=ChatUsage(0, 0), **options)
    chat_server.text_model = model
    return model


async def heads_of(session_factory, card_id: uuid.UUID) -> dict[uuid.UUID, str]:
    """The heads kept for the request ``card_id``, by reader."""
    async with session_factory() as reader:
        rows = await reader.execute(
            select(CardText.reader_id, CardText.head).where(CardText.confirmation_id == card_id)
        )
        return {reader_id: head for reader_id, head in rows.all()}


async def audited(session_factory, action: str) -> list[dict[str, Any]]:
    """The details of the lines ``action`` of the journal, oldest first."""
    async with session_factory() as reader:
        rows = await reader.execute(
            select(AuditEntry.details).where(AuditEntry.action == action).order_by(AuditEntry.id)
        )
        return [dict(details) for details in rows.scalars()]


def plain(page: str) -> str:
    """What a person reads of a page: its markup left out, its spaces as one."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", page)).split())


def one_card(page: str) -> str:
    [card] = confirmation_cards(page)
    return card


@pytest.fixture
async def alex_invites(grant, adult_member) -> None:
    """Alex may invite (an adult may not by default): a right of the owner."""
    await grant("household.invite", person=adult_member.person, access="allow")


def test_the_fixed_values_are_the_plans():
    assert cards.CARD_HEAD_SECONDS == 8


async def test_each_reader_has_their_own_head(
    adult_chat, owner_member, live_owner_client, grant, chat_server, session_factory
):  # scenario 7
    model = writes_heads(chat_server)
    await grant("household.invite", person=adult_chat.person, access="allow", approver="owner")
    reply = await adult_chat.say_with_tool("household_invite", ROBIN_FOR_TWO_DAYS, judge="do")
    card_id = reply.pending_confirmation_id
    assert card_id is not None
    await adult_chat.texts_idle()

    alex = one_card(reply.cards_html)
    assert 'class="confirmation-head"' in alex and "[Alex]" in alex
    assert INVITE_ROBIN in alex
    owners = one_card((await live_owner_client.get("/approvals")).text)
    assert 'class="confirmation-head"' in owners and "[Owner]" in owners
    assert INVITE_ROBIN in owners and "[Alex]" not in owners
    # The head took the place of the parts it says.
    for card in (alex, owners):
        for part in ("confirmation-what", "confirmation-asked", "confirmation-hint"):
            assert part not in card

    heads = await heads_of(session_factory, card_id)
    assert set(heads) == {adult_chat.person.id, owner_member.person.id}
    # Each was checked by the code and by the checker, with Robin and the stay in it.
    written = head_calls(model.writer_calls())
    assert len(written) == 2
    for call in written:
        head = asked_of(call)["texts"]["head"]
        assert "Robin" in head["names"] and "2" in head["amounts"]
        assert INVITE_ROBIN in head["meaning"]
    assert {asked_of(call)["for_whom"]["name"] for call in written} == {"Alex", "Owner"}
    assert len(head_calls(model.checker_calls())) == 2
    lines = await audited(session_factory, "texts.written")
    card_lines = [line for line in lines if line["group"] == "card"]
    assert sorted(line["person"] for line in card_lines) == sorted(
        [str(adult_chat.person.id), str(owner_member.person.id)]
    )
    assert all(line["count"] == 1 and line["language"] == "en" for line in card_lines)


class SlowHeads:
    """The writer and the checker of the texts: every text written at once, but a head answered
    ``seconds`` after it is asked."""

    def __init__(self, seconds: float) -> None:
        self.model = FakeChatModel(writer=marked, usage=ChatUsage(0, 0))
        self._seconds = seconds

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
        first = messages[0]
        if isinstance(first, ChatMessage) and '"head"' in first.content:
            await asyncio.sleep(self._seconds)
        async for item in self.model.stream(
            system=system,
            messages=messages,
            max_output_tokens=max_output_tokens,
            tools=tools,
            tool_choice=tool_choice,
        ):
            yield item


async def test_a_slow_writer_leaves_the_recueil_head(
    adult_chat, alex_invites, chat_server, session_factory, monkeypatch
):  # scenario 8
    monkeypatch.setattr(cards, "CARD_HEAD_SECONDS", 0.3)
    # The writer and the checker each give the head a second after they are asked: long after
    # the card went out.
    chat_server.text_model = SlowHeads(1.0)  # type: ignore[assignment]
    reply = await adult_chat.say_with_tool("household_invite", ROBIN_FOR_TWO_DAYS)
    card = one_card(reply.cards_html)
    # The card went out with the texts' parts: what it does, who asks, how to answer.
    assert "confirmation-head" not in card and "[Alex]" not in card
    assert f'<p class="confirmation-what">{INVITE_ROBIN}</p>' in card
    assert t("confirmation.hint.natural", "en") in card
    # The head the writer gave later is kept: the next display shows it.
    await adult_chat.texts_idle()
    assert reply.pending_confirmation_id is not None
    heads = await heads_of(session_factory, reply.pending_confirmation_id)
    assert list(heads) == [adult_chat.person.id]
    page = (await adult_chat.get("/chat")).text
    assert "[Alex]" in one_card(page)
    # Waiting longer than the turn does is no refusal: the journal says none (R25).
    assert await audited(session_factory, "texts.refused") == []


def misleading(key: str, meaning: str, for_whom: Any) -> str:
    """A writer whose head says something else, keeping the names, the stay and the holes that
    the code checks: Dana for nine days."""
    if key != "head":
        return meaning
    holes = " ".join(hole for hole in ("{reason}", "{question}", "{words}") if hole in meaning)
    return f"Say yes and Dana comes in for 9 days, not Robin for 2. {holes}".strip()


async def test_the_yes_runs_the_exact_action_whatever_the_head(
    adult_chat, live_owner_client, grant, chat_server, session_factory
):  # scenario 9
    chat_server.text_model = FakeChatModel(writer=misleading, usage=ChatUsage(0, 0))
    await grant("household.invite", person=adult_chat.person, access="allow", approver="owner")
    reply = await adult_chat.say_with_tool("household_invite", ROBIN_FOR_TWO_DAYS, judge="do")
    card_id = reply.pending_confirmation_id
    await adult_chat.texts_idle()
    owners = one_card((await live_owner_client.get("/approvals")).text)
    assert "Dana comes in for 9 days" in owners
    answer = await live_owner_client.post(f"/confirmations/{card_id}/approve")
    assert 'data-outcome="executed"' in answer.text
    async with session_factory() as reader:
        invitations = (await reader.execute(select(Invitation))).scalars().all()
    assert [(i.display_name, i.role, i.guest_days) for i in invitations] == [("Robin", "guest", 2)]
    # Once decided, the card says what came of it in the texts' words.
    assert "Dana" not in answer.text


async def test_a_head_is_kept_and_shown_again(
    adult_chat, owner_member, live_owner_client, grant, chat_server, session_factory
):
    model = writes_heads(chat_server)
    await grant("household.invite", person=adult_chat.person, access="allow", approver="owner")
    reply = await adult_chat.say_with_tool("household_invite", ROBIN_FOR_TWO_DAYS, judge="do")
    await adult_chat.texts_idle()
    streamed = one_card(reply.cards_html)
    owners = one_card((await live_owner_client.get("/approvals")).text)
    written = len(head_calls(model.writer_calls()))
    assert written == 2

    for _ in range(2):
        page = one_card((await adult_chat.get("/chat")).text)
        assert "[Alex]" in page and page.count("[Alex]") == streamed.count("[Alex]")
        again = one_card((await live_owner_client.get("/approvals")).text)
        assert "[Owner]" in again and again.count("[Owner]") == owners.count("[Owner]")
    await adult_chat.texts_idle()
    # Shown again, never written again.
    assert len(head_calls(model.writer_calls())) == written
    assert reply.pending_confirmation_id is not None
    assert set(await heads_of(session_factory, reply.pending_confirmation_id)) == {
        adult_chat.person.id,
        owner_member.person.id,
    }


async def test_one_head_when_the_reader_asks_and_says_yes(
    owner_chat, chat_server, session_factory
):  # review focus 5
    model = writes_heads(chat_server)
    reply = await owner_chat.say_with_tool("household_invite", ROBIN_FOR_TWO_DAYS, judge="yes")
    card_id = reply.pending_confirmation_id
    assert card_id is not None
    assert "[Owner]" in one_card(reply.cards_html)
    assert (await owner_chat.confirm_last(reply)).status == "executed"
    await owner_chat.texts_idle()
    assert list(await heads_of(session_factory, card_id)) == [owner_chat.person.id]
    assert len(head_calls(model.writer_calls())) == 1
    lines = await audited(session_factory, "texts.written")
    assert [line["person"] for line in lines if line["group"] == "card"] == [
        str(owner_chat.person.id)
    ]


async def test_the_judges_reason_is_placed_by_the_code(adult_chat, alex_invites, chat_server):
    model = writes_heads(chat_server)
    reason = "Robin would be the first guest this year"
    reply = await adult_chat.say_with_tool(
        "household_invite", ROBIN_FOR_TWO_DAYS, judge=f"yes: {reason}"
    )
    await adult_chat.texts_idle()
    card = one_card(reply.cards_html)
    assert "[Alex]" in card
    assert t("confirmation.asked.domo.yes", "en", reason=reason) in card
    [written] = head_calls(model.writer_calls())
    assert "{reason}" in asked_of(written)["texts"]["head"]["meaning"]
    assert asked_of(written)["texts"]["head"]["holes"] == ["reason"]
    for call in [*model.writer_calls(), *model.checker_calls()]:
        assert "first guest this year" not in json.dumps(dump(call.messages[0]))


async def test_a_notes_words_never_reach_the_writer(adult_chat, owner_member, chat_server):
    model = writes_heads(chat_server)
    words = "the blue key is under the mat"
    reply = await adult_chat.say_with_tool(
        "note_leave", {"recipient": "Owner", "text": words}, judge="yes"
    )
    await adult_chat.texts_idle()
    card = one_card(reply.cards_html)
    assert "[Alex]" in card and words in card
    [written] = head_calls(model.writer_calls())
    assert "{words}" in asked_of(written)["texts"]["head"]["meaning"]
    assert "Owner" in asked_of(written)["texts"]["head"]["names"]
    for call in [*model.writer_calls(), *model.checker_calls()]:
        assert "blue key" not in json.dumps([dump(item) for item in call.messages])


async def test_done_cards_are_written_when_domo_chooses_it(
    owner_chat, chat_server, session_factory
):
    model = writes_heads(chat_server)
    # By default, only a request that waits for a yes is written on the moment.
    done = await owner_chat.say_with_tool("household_invite", ROBIN_FOR_TWO_DAYS, judge="do")
    assert done.actions == [{"tool": "household_invite", "status": "done"}]
    assert "action-head" not in done.cards_html and "[Owner]" not in done.cards_html
    assert head_calls(model.writer_calls()) == []

    async with session_factory() as writer:
        await save_choices(
            writer,
            HOUSEHOLD_SCOPE,
            Choices(write_now=("requests", "done")),
            by=owner_chat.person.id,
            why="the cards of what I do too",
            now=datetime.now(UTC),
        )
        await writer.commit()
    dana = await owner_chat.say_with_tool(
        "household_invite", {"display_name": "Dana", "role": "adult"}, judge="do"
    )
    assert dana.actions == [{"tool": "household_invite", "status": "done"}]
    assert 'class="action-head"' in dana.cards_html and "[Owner]" in dana.cards_html
    assert "Dana" in dana.cards_html
    [written] = head_calls(model.writer_calls())
    assert "Dana" in asked_of(written)["texts"]["head"]["names"]
    async with session_factory() as reader:
        rows = (
            await reader.execute(
                select(CardText.message_action_id, MessageAction.tool_name).join(
                    MessageAction, MessageAction.id == CardText.message_action_id
                )
            )
        ).all()
    assert [tool for _, tool in rows] == ["household_invite"]
    # Kept: the page shows it again.
    page = (await owner_chat.get("/chat")).text
    assert page.count('class="action-head"') == 1 and "[Owner]" in page


async def test_a_reader_without_a_key_gets_no_head(
    child_chat,
    adult_member,
    owner_member,
    live_adult_client,
    live_owner_client,
    grant,
    chat_server,
    session_factory,
):
    model = writes_heads(chat_server)
    await grant("note.leave", role="child", approver="adult")
    # Alex may not use the general key, and has no key of his own.
    async with session_factory() as writer:
        writer.add(
            ProviderUser(
                provider_id=chat_server.provider_id,
                scope="person",
                person_id=adult_member.person.id,
                allowed=False,
                set_by=owner_member.person.id,
            )
        )
        await writer.commit()
    reply = await child_chat.say_with_tool("note_leave", {"recipient": "Owner", "text": "park?"})
    card_id = reply.pending_confirmation_id
    assert card_id is not None
    await child_chat.texts_idle()
    assert set(await heads_of(session_factory, card_id)) == {
        child_chat.person.id,
        owner_member.person.id,
    }
    assert {asked_of(call)["for_whom"]["name"] for call in head_calls(model.writer_calls())} == {
        "Sam",
        "Owner",
    }
    alex = one_card((await live_adult_client.get("/approvals")).text)
    assert "confirmation-head" not in alex and "confirmation-what" in alex
    owners = one_card((await live_owner_client.get("/approvals")).text)
    assert "[Owner]" in owners
    # Shown to Alex without a head, his is asked again: still none, and no key is no refusal.
    await child_chat.texts_idle()
    assert set(await heads_of(session_factory, card_id)) == {
        child_chat.person.id,
        owner_member.person.id,
    }
    assert await audited(session_factory, "texts.refused") == []


async def test_a_head_refused_twice_shows_the_parts_and_is_journaled(
    adult_chat, alex_invites, chat_server, session_factory
):  # ruling R25
    model = writes_heads(
        chat_server, checker=lambda key, text: "it says more" if key == "head" else None
    )
    reply = await adult_chat.say_with_tool(
        "household_invite", ROBIN_FOR_TWO_DAYS, judge="yes: a reason of the judge"
    )
    await adult_chat.texts_idle()
    card = one_card(reply.cards_html)
    assert "confirmation-head" not in card and "confirmation-what" in card
    assert reply.pending_confirmation_id is not None
    assert await heads_of(session_factory, reply.pending_confirmation_id) == {}
    assert len(head_calls(model.writer_calls())) == 2  # written again once, with why
    assert await audited(session_factory, "texts.refused") == [
        {"key": "card.head", "why": "it says more"}
    ]
    written = await audited(session_factory, "texts.written")
    assert [line for line in written if line["group"] == "card"] == []


@pytest.mark.parametrize("line_break", ["\n", "\n\n"])
async def test_a_head_keeps_its_lines_and_is_escaped(
    adult_chat, alex_invites, chat_server, line_break
):
    def on_lines(key: str, meaning: str, for_whom: Any) -> str:
        if key != "head":
            return meaning
        rest = "\n".join(meaning.splitlines()[1:])
        return f"Robin, 2 days?{line_break}{rest}"

    chat_server.text_model = FakeChatModel(writer=on_lines, usage=ChatUsage(0, 0))
    # The judge's reason, put in by the code, is escaped like the rest.
    reason = "<script>x</script> & co"
    reply = await adult_chat.say_with_tool(
        "household_invite", ROBIN_FOR_TWO_DAYS, judge=f"yes: {reason}"
    )
    card = one_card(reply.cards_html)
    assert f'<p class="confirmation-head">Robin, 2 days?{line_break}The assistant asks' in card
    assert "<script>" not in card and "&lt;script&gt;x&lt;/script&gt; &amp; co" in card


async def test_a_successors_card_says_its_lists_in_each_readers_texts(
    owner_chat, adult_member, session_factory
):  # carried from Task 2: the card keeps the people, each reader's texts say them
    reply = await owner_chat.say_with_tool("successors_set", {"successors": [{"person": "Alex"}]})
    assert (await owner_chat.confirm_last(reply)).status == "executed"
    async with session_factory() as reader:
        request = await reader.get(Confirmation, reply.pending_confirmation_id)
    assert request is not None and request.result is not None
    assert request.result["params"] == {}
    assert request.result["successors"] == {"before": [], "after": ["Alex"]}
    async with session_factory() as writer:
        await reads_in(writer, owner_chat.person, "fr")
        await writer.commit()
    page = plain((await owner_chat.get("/chat")).text)
    nobody = in_language("fr", "successors.none")
    assert in_language("fr", "action.card.done.successors.set", before=nobody, after="Alex") in page


async def test_a_head_gives_way_once_its_card_says_something_else(
    adult_chat, chat_server, session_factory
):  # the card says what the yes runs, whatever was kept (R75)
    writes_heads(chat_server)
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    assert "[Alex]" in one_card(reply.cards_html)
    async with session_factory() as writer:
        await writer.execute(update(Confirmation).values(amended_arguments={"name": "Alex Z"}))
        await writer.commit()
    card = one_card((await adult_chat.get("/chat")).text)
    assert "[Alex]" not in card and "confirmation-head" not in card
    assert t("action.describe.profile.rename", "en", name="Alex Z") in plain(card)
    # Shown again by Domo, the request has its head written again, for what it says now.
    again = await adult_chat.say_with_tool(
        "confirmation_require_explicit",
        {"request_id": str(reply.pending_confirmation_id), "reason": "A second look."},
    )
    shown = one_card(again.cards_html)
    assert "[Alex]" in shown and "Alex Z" in shown
    assert t("confirmation.hint.explicit", "en") in plain(shown)


async def test_a_head_gives_way_when_its_reader_reads_another_language(
    adult_chat, chat_server, session_factory
):
    writes_heads(chat_server)
    await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    assert "[Alex]" in one_card((await adult_chat.get("/chat")).text)
    async with session_factory() as writer:
        await reads_in(writer, adult_chat.person, "fr")
        await writer.commit()
    card = one_card((await adult_chat.get("/chat")).text)
    assert "[Alex]" not in card
    assert in_language("fr", "action.describe.profile.rename", name="Alex B") in plain(card)


class FailingHeads:
    """The writer and the checker of the texts: every text written at once, but a call that would
    write a head fails, as a provider that errs."""

    def __init__(self) -> None:
        self.model = FakeChatModel(writer=marked, usage=ChatUsage(0, 0))
        self.failed = 0

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
        first = messages[0]
        if isinstance(first, ChatMessage) and '"head"' in first.content:
            self.failed += 1
            raise ModelCallError("upstream")
        async for item in self.model.stream(
            system=system,
            messages=messages,
            max_output_tokens=max_output_tokens,
            tools=tools,
            tool_choice=tool_choice,
        ):
            yield item


async def test_a_writer_that_fails_journals_no_refusal(
    adult_chat, alex_invites, chat_server, session_factory
):  # R25: a writer that does not answer is no refusal
    failing = FailingHeads()
    chat_server.text_model = failing  # type: ignore[assignment]
    reply = await adult_chat.say_with_tool("household_invite", ROBIN_FOR_TWO_DAYS)
    await adult_chat.texts_idle()
    assert failing.failed == 1
    assert "confirmation-head" not in one_card(reply.cards_html)
    assert reply.pending_confirmation_id is not None
    assert await heads_of(session_factory, reply.pending_confirmation_id) == {}
    assert await audited(session_factory, "texts.refused") == []


async def test_a_request_that_waits_for_another_yes_gets_its_heads_without_a_tool_call(
    adult_chat, owner_member, live_owner_client, grant, chat_server, session_factory
):  # the new approver's head is written as soon as the request waits for them (spec 4.4)
    model = writes_heads(chat_server)
    reply = await adult_chat.say_with_tool("note_leave", {"recipient": "Owner", "text": "x"})
    card_id = reply.pending_confirmation_id
    assert card_id is not None
    assert list(await heads_of(session_factory, card_id)) == [adult_chat.person.id]
    # The owner takes the yes of notes meanwhile: Alex's "oui" is not his to give any more.
    await grant("note.leave", role="adult", approver="owner")
    adult_chat.use_model(FakeChatModel(["Il faut l'accord du propriétaire."]))
    answer = await adult_chat.say("oui")
    assert answer.actions == []  # no tool was called
    await adult_chat.texts_idle()
    assert set(await heads_of(session_factory, card_id)) == {
        adult_chat.person.id,
        owner_member.person.id,
    }
    assert "[Owner]" in one_card((await live_owner_client.get("/approvals")).text)
    # Alex's head was written again, for what the request asks now: the owner's yes.
    alex = one_card((await adult_chat.get("/chat")).text)
    assert "[Alex]" in alex
    the_owner = t("confirmation.asked.to.owner", "en")
    assert t("confirmation.asked.domo.yes.others.plain", "en", to_whom=the_owner) in plain(alex)
    written = {asked_of(call)["for_whom"]["name"] for call in head_calls(model.writer_calls())}
    assert written == {"Alex", "Owner"}


async def test_a_request_that_comes_back_after_a_button_gets_its_heads_again(
    adult_chat, owner_member, live_owner_client, grant, chat_server, session_factory
):  # approver_changed by a button: the one who asked has a head for what it asks now
    writes_heads(chat_server)
    right = await grant(
        "note.leave", person=adult_chat.person, limit='"ask_owner"', summary="Alex asks first."
    )
    reply = await adult_chat.say_with_tool("note_leave", {"recipient": "Owner", "text": "x"})
    card_id = reply.pending_confirmation_id
    assert card_id is not None
    await adult_chat.texts_idle()
    assert set(await heads_of(session_factory, card_id)) == {
        adult_chat.person.id,
        owner_member.person.id,
    }
    async with session_factory() as writer:
        await writer.execute(
            update(PolicyRight).where(PolicyRight.id == right.id).values(revoked_at=func.now())
        )
        await writer.commit()
    answer = await live_owner_client.post(f"/confirmations/{card_id}/approve")
    assert 'data-outcome="approver_changed"' in answer.text
    await adult_chat.texts_idle()
    alex = one_card((await adult_chat.get("/chat")).text)
    assert "[Alex]" in alex and 'data-action="confirm"' in alex


async def test_a_writing_from_newer_facts_is_kept_last(adult_chat, chat_server, session_factory):
    """One writing at a time for a card and a reader: one asked from newer facts while another
    runs begins once it has ended, so that the newest facts are kept last."""
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    card_id = reply.pending_confirmation_id
    assert card_id is not None
    async with session_factory() as writer:
        [(alex, older)] = await head_facts_of(writer, card_id, now=datetime.now(UTC))
        # No head kept yet: both are written.
        await writer.execute(delete(CardText))
        await writer.commit()
    newer = dataclasses.replace(older, reference=f"{older.reference}\nAnd more besides.")
    slow = SlowHeads(0.3)
    chat_server.text_model = slow  # type: ignore[assignment]
    writing = chat_server.app.state.text_writing
    card = CardOf(confirmation_id=card_id)
    first = writing.write_head(card, alex, older)
    second = writing.write_head(card, alex, newer)
    # The same facts asked again join the writing that runs; other facts make one of their own.
    assert first is not None and second is not None and first is not second
    assert writing.write_head(card, alex, newer) is second
    await adult_chat.texts_idle()
    async with session_factory() as reader:
        kept = await head_of(
            reader, confirmation_id=card_id, message_action_id=None, reader_id=alex.id
        )
    assert kept is not None and kept.facts_hash == newer.fingerprint("en")
    assert "And more besides." in kept.text
    written = [
        asked_of(call)["texts"]["head"]["meaning"] for call in head_calls(slow.model.writer_calls())
    ]
    assert written == [older.reference, newer.reference]


async def test_a_head_kept_from_the_same_facts_needs_no_key(
    adult_chat, owner_member, chat_server, session_factory
):  # the head kept is looked for before a key is chosen
    writes_heads(chat_server)
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    card_id = reply.pending_confirmation_id
    assert card_id is not None
    async with session_factory() as reader:
        [(alex, facts)] = await head_facts_of(reader, card_id, now=datetime.now(UTC))
    # Alex may not use any key now: the head kept from these facts is all that is needed.
    async with session_factory() as writer:
        writer.add(
            ProviderUser(
                provider_id=chat_server.provider_id,
                scope="person",
                person_id=alex.id,
                allowed=False,
                set_by=owner_member.person.id,
            )
        )
        await writer.commit()
    writing = chat_server.app.state.text_writing
    card = CardOf(confirmation_id=card_id)
    assert writing.write_head(card, alex, facts) is not None
    await adult_chat.texts_idle()
    # Not left unwritten: a page that shows the card may ask it whenever it needs to.
    assert not writing.asked_lately(card, alex.id)


class NoWriting:
    """A writer of the texts that never answers: every call fails, as a provider that errs."""

    def __init__(self) -> None:
        self.failed = 0

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
        self.failed += 1
        raise ModelCallError("upstream")
        yield ChatUsage(0, 0)  # pragma: no cover - makes this a generator


async def test_a_card_without_a_written_head_shows_its_default_phrase(
    adult_chat, chat_server, session_factory
):
    # Alex reads French, and nothing could be written for him: neither his texts nor the head of
    # his card. The card shows its default phrases in French, never a sign.
    writer = NoWriting()
    chat_server.text_model = writer  # type: ignore[assignment]
    async with session_factory() as session:
        await session.execute(
            update(Person).where(Person.id == adult_chat.person.id).values(language="fr")
        )
        await session.commit()
    await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    await adult_chat.texts_idle()
    assert writer.failed > 0
    card = one_card((await adult_chat.get("/chat")).text)
    assert "[Alex]" not in card
    assert t("action.describe.profile.rename", "fr", name="Alex B") in plain(card)
    assert SIGN not in plain(card)


async def test_the_exact_words_card_shows_its_default_phrase_with_the_message(
    owner_chat, adult_chat, chat_server, session_factory, live_clock
):  # scenario 10, review focus 4
    # Alex reads French, and nothing could be written for him: the card of the exact words says
    # in French whose they are, and when, the words put in by the code.
    writer = NoWriting()
    chat_server.text_model = writer  # type: ignore[assignment]
    async with session_factory() as session:
        await session.execute(
            update(Person).where(Person.id == adult_chat.person.id).values(language="fr")
        )
        await session.commit()
    said = "Dit à Alex de faire les courses"
    left_at = live_clock()
    await owner_chat.say_with_tool(
        "note_leave",
        {"recipient": "Alex", "text": "Fais les courses.", "exact": said},
        text=said,
        judge="do",
    )
    async with session_factory() as reader:
        [note_id] = (await reader.execute(select(Note.id))).scalars().all()
    await adult_chat.say_with_tool("note_show", {"note": str(note_id)})
    await adult_chat.texts_idle()
    card = plain((await adult_chat.get("/chat")).text)
    when = " ".join(local_date(left_at, language="fr", zone=ZoneInfo("UTC")).split())
    assert t("action.card.done.note.show", "fr", name="Owner", when=when, words=said) in card
    assert "Fais les courses." not in card and SIGN not in card


async def test_a_refused_head_shows_the_cards_default_phrase(
    adult_chat, chat_server, session_factory
):
    # Alex reads French; the checker refuses every text, the head of his card included: the card
    # shows its default phrases in French, never a sign.
    writes_heads(chat_server, checker=lambda key, text: "it says more")
    async with session_factory() as session:
        await session.execute(
            update(Person).where(Person.id == adult_chat.person.id).values(language="fr")
        )
        await session.commit()
    await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    await adult_chat.texts_idle()
    assert {"key": "card.head", "why": "it says more"} in await audited(
        session_factory, "texts.refused"
    )
    card = one_card((await adult_chat.get("/chat")).text)
    assert "confirmation-head" not in card
    assert t("action.describe.profile.rename", "fr", name="Alex B") in plain(card)
    assert SIGN not in plain(card)


async def test_a_reader_without_a_key_reads_the_default_phrases(
    adult_member, owner_member, live_adult_client, chat_server, session_factory
):
    # Alex reads French and may use no key, and the house's writer fails too (so no version of
    # French is written on the house's key): his pages show the French default phrases, never a
    # sign.
    writer = NoWriting()
    chat_server.text_model = writer  # type: ignore[assignment]
    async with session_factory() as session:
        session.add(
            ProviderUser(
                provider_id=chat_server.provider_id,
                scope="person",
                person_id=adult_member.person.id,
                allowed=False,
                set_by=owner_member.person.id,
            )
        )
        await session.execute(
            update(Person).where(Person.id == adult_member.person.id).values(language="fr")
        )
        await session.commit()
    for _ in range(2):
        for path in ("/account", "/chat"):
            page = plain((await live_adult_client.get(path)).text)
            assert SIGN not in page, path
    account = plain((await live_adult_client.get("/account")).text)
    assert t("account.title", "fr") in account
    assert t("account.password.heading", "fr") in account
