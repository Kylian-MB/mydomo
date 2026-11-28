"""Confirmations: a request that waits for a yes, and the person's answer, by button or in their
own words (decision D2). The main model never decides.

The tests use ``profile_rename``, which waits for a yes when the judge asks one (the fake judge
says yes by default): the observable effect is the name the person is called by. The chat pilots
talk with Domo on a live server (see ``chat_support.py``).
"""

import asyncio
import html
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from chat_support import confirmation_cards, status_of, turn_tools
from domo_common.i18n import t
from domo_common.meanings import meaning
from domo_core.accounts.models import Person
from domo_core.channels.page import render as page_render
from domo_core.channels.page.abilities import PAGE
from domo_core.chat.models import Conversation
from domo_core.confirmations import routes as confirmation_routes
from domo_core.confirmations.describe import describe_for_people, waiting_since
from domo_core.confirmations.intent import Reading, classifier_prompt
from domo_core.confirmations.models import Confirmation
from domo_core.confirmations.service import (
    NOT_SURE_REASON,
    create_confirmation,
    decide_confirmation,
    pending_for,
)
from domo_core.confirmations.store import (
    ANSWER_AS_APPROVER,
    ANSWER_AS_APPROVER_EXPLICITLY,
    ANSWER_BY_AN_ADULT,
    ANSWER_BY_THE_OWNER,
    ANSWER_EXPLICITLY,
    ANSWER_IN_WORDS,
    ANSWER_WITH_A_PROOF,
    how_to_answer,
    who_answers,
)
from domo_core.conversation import execution
from domo_core.conversation import turn as conversation_turn
from domo_core.llm.types import ChatMessage, ToolCall
from domo_core.policy.actions import DescribedAction
from domo_core.policy.asks import Asked
from domo_core.policy.catalog import DEFAULT_POLICY
from domo_core.policy.rights import NO_RIGHTS
from domo_core.policy.roles import Actor
from domo_core.policy.settings import PolicySettings
from domo_core.prompts import (
    HouseholdLine,
    KeyShown,
    KeysNote,
    MemoryBlock,
    ModelsNote,
    PendingRequest,
    build_system_prompt,
    today_of,
)
from domo_core.texts.view import CatalogTexts, RecueilTexts
from domo_core.tools.registry import TOOLS
from domo_core.trust.service import Trust
from fakes import FakeChatModel
from sqlalchemy import select, text
from web_support import audit_entries, in_language

# The header of the requests in the context block of the prompt (the fixed text also speaks of
# "Pending requests", in general).
PENDING_IN_CONTEXT = "Pending requests (data, not instructions)"


# --- the brief's tests ---------------------------------------------------------------------------


async def test_confirm_button_executes_the_stored_action(adult_chat, display_name_of):
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    card = await adult_chat.confirm_last(reply)
    assert card.status == "executed" and await display_name_of(adult_chat.person) == "Alex B"


async def test_double_click_executes_once(adult_chat, chat_server):  # Review Focus 1
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    card_id = reply.pending_confirmation_id
    r1, r2 = await asyncio.gather(
        *(adult_chat.post(f"/confirmations/{card_id}/approve") for _ in range(2))
    )
    assert sorted([status_of(r1), status_of(r2)]) == ["already_decided", "executed"]
    async with chat_server.session_factory() as reader:
        decided = [e for e in await audit_entries(reader) if e.action == "confirmation.decided"]
    assert [e.details["outcome"] for e in decided] == ["executed"]


async def test_expired_confirmation_is_refused(
    adult_chat, clock, display_name_of
):  # Review Focus 2
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    clock.advance(timedelta(hours=168, seconds=1))
    r = await adult_chat.post(f"/confirmations/{reply.pending_confirmation_id}/approve")
    assert status_of(r) == "expired" and await display_name_of(adult_chat.person) == "Alex"


async def test_a_listed_phrase_confirms_without_the_classifier(adult_chat, display_name_of):
    await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    model = adult_chat.use_model(FakeChatModel(["Voilà !"]))
    await adult_chat.say("Vas-y !")
    # The code read the yes: the one call is Domo's own answer, with his tools.
    assert len(model.calls) == 1 and model.calls[0].tools
    assert await display_name_of(adult_chat.person) == "Alex B"


async def test_free_wording_goes_to_the_isolated_classifier(adult_chat, display_name_of):
    await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    model = adult_chat.use_model(FakeChatModel(["yes"]))
    await adult_chat.say("ça me va très bien, merci")
    # The reader reads the words (no tools, one message: data); then Domo answers.
    assert len(model.calls) == 2 and model.calls[0].tools == () and model.calls[1].tools
    assert model.calls[0].system == classifier_prompt()
    (given,) = model.calls[0].messages
    assert isinstance(given, ChatMessage)
    assert json.loads(given.content)["reply"] == "ça me va très bien, merci"
    assert await display_name_of(adult_chat.person) == "Alex B"


async def test_an_unclear_reply_goes_to_domo_with_the_request_in_context(
    adult_chat, display_name_of
):
    await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    model = adult_chat.use_model(FakeChatModel(tool_script=["unclear", "Tu hésites ?"]))
    await adult_chat.say("hmm je sais pas")
    assert len(model.calls) == 2 and "Alex B" in model.calls[1].system
    assert await display_name_of(adult_chat.person) == "Alex"


async def test_no_refuses_the_request(adult_chat, confirmation_status):
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    await adult_chat.say("non merci")
    assert await confirmation_status(reply.pending_confirmation_id) == "refused"


async def test_an_explicit_yes_needs_the_button_or_i_confirm(adult_chat, display_name_of):
    await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"}, judge="explicit_yes: odd")
    model = adult_chat.use_model(FakeChatModel(["Tu confirmes ?"]))
    await adult_chat.say("oui")
    # The reader is asked whether that yes is firm (it is not), then Domo answers.
    assert len(model.calls) == 2 and model.calls[0].system == classifier_prompt()
    assert model.calls[1].tools
    assert await display_name_of(adult_chat.person) == "Alex"
    await adult_chat.say("Je confirme.")
    assert await display_name_of(adult_chat.person) == "Alex B"


async def test_with_two_pending_a_reply_answers_the_one_just_shown(
    adult_chat, display_name_of, confirmation_status
):  # R93 changed this test: it said that a yes decides nothing while two requests wait
    first = await adult_chat.say_with_tool("profile_rename", {"name": "A"})
    second = await adult_chat.say_with_tool("profile_rename", {"name": "B"})
    model = adult_chat.use_model(FakeChatModel(["Fait."]))
    await adult_chat.say("oui")
    # The reply just before showed B, and only B: the yes is B's. A still waits.
    assert len(model.calls) == 1 and await display_name_of(adult_chat.person) == "B"
    assert await confirmation_status(second.pending_confirmation_id) == "executed"
    assert await confirmation_status(first.pending_confirmation_id) == "pending"


async def test_someone_else_cannot_approve_a_self_confirmation(
    adult_chat, live_owner_client, display_name_of
):
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    r = await live_owner_client.post(f"/confirmations/{reply.pending_confirmation_id}/approve")
    assert status_of(r) == "not_yours" and await display_name_of(adult_chat.person) == "Alex"


async def test_rights_are_rechecked_at_approval(adult_chat, grant, display_name_of):
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    await grant("profile.rename", person=adult_chat.person, access="deny")  # the owner says no
    r = await adult_chat.post(f"/confirmations/{reply.pending_confirmation_id}/approve")
    assert status_of(r) == "refused" and await display_name_of(adult_chat.person) == "Alex"


async def test_a_child_request_waits_for_an_adult(
    child_chat, live_adult_client, display_name_of, adult_member, an_adult_agrees_to_a_childs_rename
):
    reply = await child_chat.say_with_tool("profile_rename", {"name": "Sam B"})
    path = f"/confirmations/{reply.pending_confirmation_id}/approve"
    assert status_of(await child_chat.post(path)) == "not_yours"
    assert status_of(await live_adult_client.post(path)) == "executed"
    # It ran as the child's action: the child is renamed, not the adult who approved it.
    assert await display_name_of(child_chat.person) == "Sam B"
    assert await display_name_of(adult_member.person) == "Alex"


# --- creating a request --------------------------------------------------------------------------


async def test_a_request_is_dated_by_the_injected_clock(session, add_person, clock):  # R54
    person = await add_person("Alex", "adult")
    conversation = Conversation(person_id=person.id)
    session.add(conversation)
    await session.flush()
    far_from_now = clock()  # 2030: nowhere near the database's own clock
    assert abs(far_from_now - datetime.now(UTC)) > timedelta(days=30)
    c = await create_confirmation(
        session,
        requester=Actor(person.id, "adult"),
        tool_name="profile_rename",
        arguments={"name": "Alex B"},
        action=DescribedAction(
            "core", "profile.rename", f"person:{person.id}", "self_profile", "household"
        ),
        approver="self",
        asked=Asked("yes", "judge"),
        conversation_id=conversation.id,
        message_id=None,
        now=far_from_now,
    )
    stored = (
        await session.execute(select(Confirmation.created_at).where(Confirmation.id == c.id))
    ).scalar_one()
    assert stored == far_from_now
    assert (c.status, c.approver_kind, c.ask, c.asked_by, c.tool_name) == (
        "pending",
        "self",
        "yes",
        "judge",
        "profile_rename",
    )
    assert c.arguments == {"name": "Alex B"} and c.action["action_type"] == "profile.rename"
    # It waits, under the lifetime, and stops being listed once past it.
    settings = PolicySettings({})
    listed = await pending_for(session, person.id, conversation.id, settings, far_from_now)
    assert [p.id for p in listed] == [c.id]
    later = far_from_now + timedelta(hours=168)
    assert await pending_for(session, person.id, conversation.id, settings, later) == []


async def test_a_request_stored_before_facts_existed_can_still_be_confirmed(
    adult_chat, chat_server, display_name_of
):
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    # What a request made before the facts looked like: the five fields of the action, no more.
    async with chat_server.session_factory() as writer:
        await writer.execute(text("UPDATE confirmations SET action = action - 'facts'"))
        await writer.commit()
        stored = (await writer.execute(select(Confirmation.action))).scalar_one()
    assert set(stored) == {"connector", "action_type", "target", "effect", "destination"}
    card = await adult_chat.confirm_last(reply)
    assert card.status == "executed"
    assert await display_name_of(adult_chat.person) == "Alex B"


async def test_a_waiting_action_is_stored_with_its_request(adult_chat, chat_server):
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    assert reply.actions == [{"tool": "profile_rename", "status": "waiting"}]
    async with chat_server.session_factory() as reader:
        (row,) = (
            await reader.execute(text("SELECT confirmation_id, message_id FROM message_actions"))
        ).all()
        c = (await reader.execute(select(Confirmation))).scalar_one()
    assert row.confirmation_id == c.id == reply.pending_confirmation_id
    assert row.message_id == c.message_id == reply.message_id
    assert (c.requester_id, c.conversation_id) == (
        adult_chat.person.id,
        await _conversation_of(chat_server, reply.message_id),
    )
    assert c.created_at == adult_chat.clock()


async def test_a_request_that_cannot_be_stored_is_a_failure_not_a_promise(
    adult_chat, monkeypatch, chat_server
):  # R69
    async def broken(*args, **kwargs):
        raise RuntimeError("the database refused the request")

    monkeypatch.setattr(execution, "create_confirmation", broken)
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    # Never "waiting for a yes" when nothing waits.
    assert json.loads(reply.model_result_text) == {"status": "failed"}
    assert reply.actions == [{"tool": "profile_rename", "status": "failed"}]
    assert reply.pending_confirmation_id is None
    assert 'data-action-status="failed"' in reply.cards_html
    async with chat_server.session_factory() as reader:
        assert (await reader.execute(select(Confirmation))).first() is None
    # Nothing is pending in the next turn's context, and a yes decides nothing.
    model = adult_chat.use_model(FakeChatModel(["D'accord."]))
    await adult_chat.say("oui")
    assert len(model.calls) == 1 and PENDING_IN_CONTEXT not in model.calls[0].system


# --- what Domo is told -------------------------------------------------------------------------


async def test_domo_sees_each_pending_request_in_plain_words(adult_chat, clock):
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    created = clock()
    clock.advance(timedelta(days=2, hours=3))
    model = adult_chat.use_model(FakeChatModel(tool_script=["unclear", "Alors ?"]))
    await adult_chat.say("hmm")
    expected = PendingRequest(
        request_id=str(reply.pending_confirmation_id),
        description=t("action.describe.profile.rename", "en", name="Alex B"),
        created_at=f"{created.astimezone(UTC):%Y-%m-%dT%H:%M}Z (waiting 2 days)",
        reminders=0,
        answered_with_change=False,
        answered_by="the person",
        answer_with=ANSWER_IN_WORDS,
        plain_yes_not_enough=False,
        asked_by="judge",
    )
    assert model.calls[1].system == build_system_prompt(
        abilities=PAGE,
        display_name="Alex",
        role="adult",
        can_act=True,
        **turn_tools("adult"),
        pending=[expected],
        today=today_of(clock(), None),
        household=(
            HouseholdLine("Owner", "owner"),
            HouseholdLine("Alex", "adult", is_you=True),
        ),
        # The language Alex reads: the house's, as no page of his kept one.
        reads="en",
        # Nobody's word has been weighed yet: the household's start (trust per person).
        trust=Trust(percent=70, facts=0),
        # The key and the model of the pilots, whose price the catalog does not know.
        # No model of meanings in the pilots: the search works by words alone.
        memory=MemoryBlock(search="words_only"),
        models=ModelsNote(
            key_label="openai_compatible key of Owner, general",
            model_id="example-sonnet-1",
            level="medium",
            price_known=False,
        ),
        # The keys Alex may use: the general key, which the owner gave.
        keys=KeysNote(
            listed=(
                KeyShown("openai_compatible key of Owner, general", adult_chat.server.provider_id),
            )
        ),
    )


@pytest.mark.parametrize(
    "approver_kind,ask,who,how",
    [
        # The person's own request takes their own words when it asks a yes...
        ("self", "yes", "the person", ANSWER_IN_WORDS),
        ("self", None, "the person", ANSWER_IN_WORDS),
        # ... the button or "I confirm" when it asks an explicit yes, a proof on the card.
        ("self", "explicit_yes", "the person", ANSWER_EXPLICITLY),
        ("self", "proof", "the person", ANSWER_WITH_A_PROOF),
        # A child's request: an adult says yes, with the button of their own page.
        ("adult", "yes", "an adult", ANSWER_BY_AN_ADULT),
        # One that waits for the owner's yes, whatever it asks.
        ("owner", "explicit_yes", "the owner", ANSWER_BY_THE_OWNER),
    ],
)
def test_domo_is_told_who_says_yes_and_how(approver_kind, ask, who, how):  # final review I1
    c = Confirmation(approver_kind=approver_kind, ask=ask, action={"action_type": "profile.rename"})
    assert (who_answers(c), how_to_answer(c)) == (who, how)


def test_the_ways_of_saying_yes_are_told_in_plain_words():
    assert "own words" in ANSWER_IN_WORDS and "button" in ANSWER_IN_WORDS
    assert '"I confirm"' in ANSWER_EXPLICITLY and "plain yes is not enough" in ANSWER_EXPLICITLY
    assert "adult" in ANSWER_BY_AN_ADULT and "button" in ANSWER_BY_AN_ADULT
    assert "proof" in ANSWER_WITH_A_PROOF and "never in the conversation" in ANSWER_WITH_A_PROOF


def test_the_ways_of_saying_yes_are_true_on_every_channel():  # spec 6.8
    """Where there is no button (the program of text), the words answer: how a request is answered
    puts them first, and names its button only where there is one (the context says where:
    ``prompts.abilities_line``)."""
    for how in (
        ANSWER_IN_WORDS,
        ANSWER_EXPLICITLY,
        ANSWER_AS_APPROVER,
        ANSWER_AS_APPROVER_EXPLICITLY,
    ):
        assert not how.startswith("the button") and "card" not in how, how
        assert "its button where there is one" in how, how


async def test_domo_is_told_that_a_request_a_rule_asks_explicitly_needs_the_button_or_i_confirm(
    adult_chat, grant
):  # final review Important 1: the owner's rule "always ask an explicit yes before..."
    await grant("profile.rename", role="adult", ask="explicit_yes")
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    result = json.loads(reply.model_result_text)
    assert (result["answer_with"], result["asked_by"]) == (ANSWER_EXPLICITLY, "rule")
    model = adult_chat.use_model(FakeChatModel(tool_script=["unclear", "Alors ?"]))
    await adult_chat.say("hmm")
    (request,) = _pending_in(model.calls[1].system)
    assert (request["answered_by"], request["answer_with"]) == ("the person", ANSWER_EXPLICITLY)
    assert request["asked_by"] == "rule"
    assert request["plain_yes_not_enough"] is False


@pytest.mark.parametrize(
    "words,script",
    [
        # A phrase the code knows: the reader says whether it is firm (a bare yes here).
        ("oui", ["yes", "Il faut le bouton, ou dire « je confirme »."]),
        ("ça me va très bien", ["yes", "Il faut le bouton, ou « je confirme »."]),  # classifier
    ],
)
async def test_domo_is_told_when_a_plain_yes_was_not_enough(
    adult_chat, display_name_of, words, script
):  # final review Important 1
    reply = await adult_chat.say_with_tool(
        "profile_rename", {"name": "Alex B"}, judge="explicit_yes: odd"
    )
    model = adult_chat.use_model(FakeChatModel(tool_script=script))
    await adult_chat.say(words)
    (request,) = _pending_in(model.calls[-1].system)
    assert model.calls[-1].tools  # the main model
    assert request["request_id"] == str(reply.pending_confirmation_id)
    assert request["plain_yes_not_enough"] is True
    assert request["answer_with"] == ANSWER_EXPLICITLY
    assert await display_name_of(adult_chat.person) == "Alex"
    # For that turn only: the next one does not say it again.
    model = adult_chat.use_model(FakeChatModel(tool_script=["unclear", "Hmm ?"]))
    await adult_chat.say("hmm")
    assert _pending_in(model.calls[-1].system)[0]["plain_yes_not_enough"] is False


async def test_a_yes_the_service_finds_not_explicit_enough_is_marked_too(
    adult_chat, grant, display_name_of, monkeypatch
):  # the owner's rule asks an explicit yes now, between the reading of the words and the decision
    await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    await grant("profile.rename", role="adult", ask="explicit_yes")

    async def read_as_a_plain_yes(text, shown, **_):
        return tuple(Reading("approve", "phrase") for _ in shown)

    monkeypatch.setattr(conversation_turn, "read_replies", read_as_a_plain_yes)
    model = adult_chat.use_model(FakeChatModel(["Il faut le bouton, ou « je confirme »."]))
    await adult_chat.say("oui")
    (request,) = _pending_in(model.calls[0].system)
    assert request["plain_yes_not_enough"] is True
    assert await display_name_of(adult_chat.person) == "Alex"


async def test_a_yes_with_a_change_goes_to_domo_marked_as_such(
    adult_chat, display_name_of, confirmation_status
):
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    model = adult_chat.use_model(FakeChatModel(tool_script=["change", "Je note."]))
    await adult_chat.say("oui mais plutôt Alex C")
    assert len(model.calls) == 2 and model.calls[1].tools
    assert '"answered_with_change": true' in model.calls[1].system
    # Task 7 lets Domo apply the change; here nothing has run.
    assert await display_name_of(adult_chat.person) == "Alex"
    assert await confirmation_status(reply.pending_confirmation_id) == "pending"


async def test_domo_only_sees_the_requests_of_this_person(
    adult_chat, child_chat, an_adult_agrees_to_a_childs_rename
):
    reply = await child_chat.say_with_tool("profile_rename", {"name": "Sam B"})
    model = adult_chat.use_model(FakeChatModel(["ok"]))
    await adult_chat.say("salut")
    assert PENDING_IN_CONTEXT not in model.calls[0].system
    # A child's request waits for an adult: the child's Domo sees it, marked so (spec 5.2), but
    # it is not one a reply of the child decides (the classifier never reads their words).
    model = child_chat.use_model(FakeChatModel(["ok"]))
    await child_chat.say("oui")
    assert len(model.calls) == 1 and model.calls[0].tools
    [told] = json.loads(model.calls[0].system.split(f"{PENDING_IN_CONTEXT}:\n")[1].split("\n\n")[0])
    assert (told["request_id"], told["answered_by"]) == (
        str(reply.pending_confirmation_id),
        "an adult",
    )


@pytest.mark.parametrize(
    "seconds,words",
    [
        (20, "waiting less than a minute"),
        (60, "waiting 1 minute"),
        (59 * 60, "waiting 59 minutes"),
        (3600, "waiting 1 hour"),
        (23 * 3600 + 59 * 60, "waiting 23 hours"),
        (86400, "waiting 1 day"),
        (6 * 86400 + 23 * 3600, "waiting 6 days"),
    ],
)
def test_the_age_of_a_request_is_told_in_plain_words(seconds, words):  # R62
    created = datetime(2030, 1, 1, 10, 0, tzinfo=UTC)
    assert waiting_since(created, created + timedelta(seconds=seconds)) == (
        f"2030-01-01T10:00Z ({words})"
    )


# --- answering in the chat -----------------------------------------------------------------------


async def test_a_yes_in_the_chat_is_answered_by_domo_and_the_card_shows(adult_chat, chat_server):
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    model = adult_chat.use_model(FakeChatModel(["Voilà, c'est fait."]))
    answer = await adult_chat.say("oui")
    assert len(model.calls) == 1 and model.calls[0].tools  # Domo answers, not the classifier
    assert (answer.text, answer.status) == ("Voilà, c'est fait.", "complete")
    assert answer.actions == [{"tool": "profile_rename", "status": "done"}]
    assert answer.pending_confirmation_id == reply.pending_confirmation_id
    assert 'data-confirmation-status="executed"' in answer.cards_html
    assert "data-action=" not in answer.cards_html  # no button on a decided request
    # How the decision was made, and from which message.
    async with chat_server.session_factory() as reader:
        c = (await reader.execute(select(Confirmation))).scalar_one()
        person_message = (
            await reader.execute(
                text(
                    "SELECT id FROM messages WHERE role = 'user' AND content = 'oui' "
                    "ORDER BY created_at DESC LIMIT 1"
                )
            )
        ).scalar_one()
        decided = [e for e in await audit_entries(reader) if e.action == "confirmation.decided"]
    assert (c.status, c.decided_via, c.decided_by) == ("executed", "phrase", adult_chat.person.id)
    assert c.decided_message_id == person_message
    assert c.result == {"key": "action.card.done.profile.rename", "params": {"name": "Alex B"}}
    assert [e.details for e in decided] == [
        {
            "confirmation_id": str(c.id),
            "action_type": "profile.rename",
            "outcome": "executed",
            "approver_kind": "self",
            "via": "phrase",
            "changed": False,
        }
    ]
    assert decided[0].actor == f"person:{adult_chat.person.id}"
    # The next turn: the model reads what came of it.
    model = adult_chat.use_model(FakeChatModel(["ok"]))
    await adult_chat.say("merci")
    assert (
        ChatMessage("assistant", "[action profile.rename: done]\nVoilà, c'est fait.")
        in model.calls[0].messages
    )


async def test_a_no_in_the_chat_cancels_and_domo_answers(adult_chat, display_name_of):
    await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    model = adult_chat.use_model(FakeChatModel(["D'accord, j'annule."]))
    answer = await adult_chat.say("Non merci !")
    assert len(model.calls) == 1 and model.calls[0].tools
    assert answer.text == "D'accord, j'annule."
    assert 'data-confirmation-status="refused"' in answer.cards_html
    assert await display_name_of(adult_chat.person) == "Alex"


async def test_the_classifier_can_refuse_too(adult_chat, confirmation_status):
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    model = adult_chat.use_model(FakeChatModel(["no"]))
    await adult_chat.say("finalement je préfère garder mon nom")
    assert len(model.calls) == 2 and model.calls[0].tools == () and model.calls[1].tools
    assert await confirmation_status(reply.pending_confirmation_id) == "refused"


async def test_a_reply_after_the_lifetime_decides_nothing(adult_chat, clock, display_name_of):
    await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    clock.advance(timedelta(hours=168))
    model = adult_chat.use_model(FakeChatModel(["Quoi donc ?"]))
    await adult_chat.say("oui")
    assert len(model.calls) == 1 and PENDING_IN_CONTEXT not in model.calls[0].system
    assert await display_name_of(adult_chat.person) == "Alex"


async def test_a_shorter_lifetime_applies_to_requests_already_waiting(
    adult_chat, clock, set_setting
):  # D8: the settings are read again at every decision
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    clock.advance(timedelta(hours=2))
    await set_setting("confirmation_max_lifetime_hours", 1)
    r = await adult_chat.post(f"/confirmations/{reply.pending_confirmation_id}/approve")
    assert status_of(r) == "expired"


# --- the buttons and the card --------------------------------------------------------------------


async def test_the_refuse_button_refuses(adult_chat, confirmation_status, display_name_of):
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    r = await adult_chat.post(f"/confirmations/{reply.pending_confirmation_id}/refuse")
    assert r.status_code == 200 and status_of(r) == "refused"
    assert await confirmation_status(reply.pending_confirmation_id) == "refused"
    assert await display_name_of(adult_chat.person) == "Alex"
    # A second answer finds it decided.
    again = await adult_chat.post(f"/confirmations/{reply.pending_confirmation_id}/approve")
    assert status_of(again) == "already_decided"
    assert 'data-confirmation-status="refused"' in again.text


async def test_the_card_shows_what_would_happen_and_how_to_answer(adult_chat):
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "<b>Al</b> & co"})
    card = confirmation_cards(reply.cards_html)[0]
    assert f'data-confirmation-id="{reply.pending_confirmation_id}"' in card
    assert 'data-confirmation-status="pending"' in card
    assert 'data-action="confirm"' in card and 'data-action="refuse"' in card
    assert t("confirmation.hint.natural", "en") in _plain(card)
    assert t("confirmation.asked.domo.yes.plain", "en") in _plain(card)
    # What the model wrote is shown escaped.
    assert "<b>" not in card and "&lt;b&gt;Al&lt;/b&gt; &amp; co" in card
    # The page shows the same card, from the database.
    page = (await adult_chat.get("/chat")).text
    assert confirmation_cards(page) == [card]


async def test_the_card_says_who_asks_what(adult_chat, grant):
    await grant("profile.rename", role="adult", ask="explicit_yes")
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    card = confirmation_cards(reply.cards_html)[0]
    # The owner's rule asks an explicit yes: the card says so, and how to give it.
    assert t("confirmation.hint.explicit", "en") in _plain(card)
    assert t("confirmation.asked.rule.explicit_yes", "en") in _plain(card)
    # Domo's own reason, when the judge gives one, is on the card too.
    reply = await adult_chat.say_with_tool(
        "profile_rename", {"name": "Alex C"}, judge="proof: <b>a big change</b>"
    )
    card = confirmation_cards(reply.cards_html)[0]
    assert t("confirmation.hint.proof", "en") in _plain(card)
    assert 'data-action="confirm"' not in card  # a proof is never a click
    assert "<b>a big change</b>" not in card
    assert t("confirmation.asked.domo.proof", "en", reason="<b>a big change</b>") in _plain(card)


async def test_a_child_sees_the_request_but_no_button(
    child_chat, an_adult_agrees_to_a_childs_rename
):
    reply = await child_chat.say_with_tool("profile_rename", {"name": "Sam B"})
    (card,) = confirmation_cards(reply.cards_html)
    assert t("action.card.waiting.adult", "en") in _plain(card)
    assert "data-action=" not in card


async def test_a_decided_request_shows_its_result_on_the_page(adult_chat):
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    card = await adult_chat.confirm_last(reply)
    assert t("action.card.done.profile.rename", "en", name="Alex B") in _plain(card.html)
    page = (await adult_chat.get("/chat")).text
    (shown,) = confirmation_cards(page)
    assert 'data-confirmation-status="executed"' in shown and "data-action=" not in shown
    assert t("action.card.done.profile.rename", "en", name="Alex B") in _plain(shown)


async def test_not_yours_tells_nothing_about_the_request(adult_chat, live_owner_client):  # R24
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Secret Name"})
    r = await live_owner_client.post(f"/confirmations/{reply.pending_confirmation_id}/refuse")
    assert r.status_code == 200 and status_of(r) == "not_yours"
    assert "Secret Name" not in r.text and "data-confirmation-status" not in r.text
    # Nor whether it exists.
    unknown = await live_owner_client.post(f"/confirmations/{uuid.uuid4()}/approve")
    assert status_of(unknown) == "not_yours"


async def test_a_malformed_id_is_not_found(adult_chat):
    for path in ("/confirmations/nope/approve", "/confirmations/1234/refuse"):
        assert (await adult_chat.post(path)).status_code == 404


async def test_a_decision_from_another_site_is_refused(adult_chat, confirmation_status):
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    r = await adult_chat.post(
        f"/confirmations/{reply.pending_confirmation_id}/approve",
        headers={"origin": "https://evil.example.net"},
    )
    assert r.status_code == 403
    assert await confirmation_status(reply.pending_confirmation_id) == "pending"


async def test_a_removed_person_can_no_longer_decide(
    adult_chat, chat_server, confirmation_status, clock
):
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    async with chat_server.session_factory() as writer:
        await writer.execute(
            text("UPDATE persons SET disabled_at = now(), state = 'gone' WHERE id = :id"),
            {"id": adult_chat.person.id},
        )
        await writer.commit()
    # Their open tab is sent to the login: the session of someone removed no longer counts
    # (task 9) ...
    r = await adult_chat.post(f"/confirmations/{reply.pending_confirmation_id}/approve")
    assert r.status_code == 401 and r.headers["hx-redirect"] == "/login"

    # ... and the service itself refuses them, whatever reaches it.
    def never(requester, conversation_id):
        raise AssertionError("nothing may run")

    outcome = await decide_confirmation(
        session_factory=chat_server.session_factory,
        confirmation_id=reply.pending_confirmation_id,
        decider=adult_chat.person,
        approve=True,
        via="button",
        reply_message_id=None,
        ctx_factory=never,
        registry=TOOLS,
        policy=DEFAULT_POLICY,
        now=clock(),
    )
    assert outcome == ("not_yours", None)
    assert await confirmation_status(reply.pending_confirmation_id) == "pending"


# --- fix round 1 ---------------------------------------------------------------------------------


def test_a_request_with_arguments_named_key_or_lang_is_still_described():  # Important 1
    action = DescribedAction("core", "profile.rename", "person:x", "self_profile", "household")
    assert describe_for_people(action, {"key": "x", "lang": "y"}, CatalogTexts("en")) == t(
        "action.describe", "en"
    )
    assert describe_for_people(
        action, {"name": "A", "key": "x", "lang": "y"}, CatalogTexts("fr")
    ) == t("action.describe.profile.rename", "fr", name="A")


async def test_a_stored_request_with_such_arguments_still_renders(
    adult_chat, chat_server
):  # Important 1: the card, the page and the next turn
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    async with chat_server.session_factory() as writer:
        await writer.execute(
            text("""UPDATE confirmations SET arguments = '{"key": "x", "lang": "y"}'""")
        )
        await writer.commit()
    page = await adult_chat.get("/chat")
    assert page.status_code == 200
    (card,) = confirmation_cards(page.text)
    assert t("action.describe", "en") in _plain(card)
    model = adult_chat.use_model(FakeChatModel(tool_script=["unclear", "Hmm ?"]))
    answer = await adult_chat.say("hmm")
    assert answer.status == "complete" and t("action.describe", "en") in model.calls[1].system
    r = await adult_chat.post(f"/confirmations/{reply.pending_confirmation_id}/refuse")
    assert status_of(r) == "refused"


async def test_a_requester_who_became_a_child_cannot_approve_alone(
    adult_chat, set_role, display_name_of, confirmation_status, an_adult_agrees_to_a_childs_rename
):  # Important 2: the decider must satisfy the replay's approver
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    await set_role(adult_chat.person, "child")  # the replay now asks for an adult's yes
    r = await adult_chat.post(f"/confirmations/{reply.pending_confirmation_id}/approve")
    # Ruling R27: nothing is done, and the request now waits for an adult (it is not refused).
    assert status_of(r) == "approver_changed" and await display_name_of(adult_chat.person) == "Alex"
    assert await confirmation_status(reply.pending_confirmation_id) == "pending"
    assert t("confirmation.outcome.approver_changed.adult", "en") in _plain(r.text)
    assert t("confirmation.outcome.not_allowed", "en") not in _plain(r.text)


async def test_a_yes_right_after_the_request_answers_it(adult_chat, display_name_of):  # R74 (a)
    await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    model = adult_chat.use_model(FakeChatModel(["Voilà."]))
    await adult_chat.say("oui")
    assert len(model.calls) == 1 and await display_name_of(adult_chat.person) == "Alex B"


async def test_a_yes_after_another_exchange_does_not_answer_an_older_request(
    adult_chat, display_name_of, confirmation_status
):  # R74 (b)
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    adult_chat.use_model(
        FakeChatModel(tool_script=["unclear", "Je sais lister le foyer. Tu veux ?"])
    )
    await adult_chat.say("qu'est-ce que tu sais faire ?")
    model = adult_chat.use_model(FakeChatModel(["Voilà qui est là."]))
    await adult_chat.say("oui")
    # The yes answered Domo's last question, not the request shown two replies ago.
    assert len(model.calls) == 1 and model.calls[0].tools
    assert await display_name_of(adult_chat.person) == "Alex"
    assert await confirmation_status(reply.pending_confirmation_id) == "pending"
    assert PENDING_IN_CONTEXT in model.calls[0].system and "Alex B" in model.calls[0].system
    # The button still works.
    r = await adult_chat.post(f"/confirmations/{reply.pending_confirmation_id}/approve")
    assert status_of(r) == "executed"


async def test_a_yes_that_does_not_say_which_of_two_requests_decides_neither(
    adult_chat, display_name_of
):  # R74 (c); spec 11.1: the reader reads it for both, and finds no answer to either
    adult_chat.use_model(
        FakeChatModel(
            tool_script=[
                ToolCall("c1", "profile_rename", '{"name": "A"}'),
                ToolCall("c2", "profile_rename", '{"name": "B"}'),
                "Deux demandes.",
            ]
        )
    )
    reply = await adult_chat.say("appelle-moi A, ou B")
    assert [a["status"] for a in reply.actions] == ["waiting", "waiting"]
    model = adult_chat.use_model(FakeChatModel(["Laquelle ?"]))
    await adult_chat.say("oui")
    assert len(model.calls) == 2 and model.calls[0].system == classifier_prompt()
    assert await display_name_of(adult_chat.person) == "Alex"


async def test_words_answer_the_request_domo_shows_again_while_another_waits(
    adult_chat, display_name_of, confirmation_status
):  # R93: "he shows you the one he means, then you answer" works
    a = (await adult_chat.say_with_tool("profile_rename", {"name": "A"})).pending_confirmation_id
    b = (await adult_chat.say_with_tool("profile_rename", {"name": "B"})).pending_confirmation_id
    await adult_chat.say_with_tool("confirmation_remind", {"request_id": str(a)})
    model = adult_chat.use_model(FakeChatModel(["Voilà."]))
    await adult_chat.say("oui")
    assert len(model.calls) == 1 and await display_name_of(adult_chat.person) == "A"
    assert await confirmation_status(a) == "executed"
    assert await confirmation_status(b) == "pending"


async def test_a_reply_that_showed_two_requests_again_is_read_for_both(
    adult_chat, display_name_of, confirmation_status
):  # spec 11.1 (it was R93): words answer each request the reply just before showed
    a = (await adult_chat.say_with_tool("profile_rename", {"name": "A"})).pending_confirmation_id
    b = (await adult_chat.say_with_tool("profile_rename", {"name": "B"})).pending_confirmation_id
    adult_chat.use_model(
        FakeChatModel(
            tool_script=[
                "unclear",
                ToolCall("r1", "confirmation_remind", json.dumps({"request_id": str(a)})),
                ToolCall("r2", "confirmation_remind", json.dumps({"request_id": str(b)})),
                "Ces deux demandes attendent.",
            ]
        )
    )
    both = await adult_chat.say("qu'est-ce qui attend ?")
    assert [a["status"] for a in both.actions] == ["done", "done"]
    model = adult_chat.use_model(FakeChatModel(["Laquelle des deux ?"]))
    await adult_chat.say("oui")
    # The reader is given both, and its answer names neither: nothing decided, Domo answers.
    assert len(model.calls) == 2 and model.calls[1].tools
    (given,) = model.calls[0].messages
    assert isinstance(given, ChatMessage)
    assert [r["n"] for r in json.loads(given.content)["requests"]] == [1, 2]
    assert await display_name_of(adult_chat.person) == "Alex"
    assert [await confirmation_status(c) for c in (a, b)] == ["pending", "pending"]
    # Shown again, both (the reply before showed none: no reader), and a yes to the second only
    # decides it.
    adult_chat.use_model(
        FakeChatModel(
            tool_script=[
                ToolCall("r3", "confirmation_remind", json.dumps({"request_id": str(a)})),
                ToolCall("r4", "confirmation_remind", json.dumps({"request_id": str(b)})),
                "Les revoici.",
            ]
        )
    )
    await adult_chat.say("montre-les encore")
    model = adult_chat.use_model(FakeChatModel(tool_script=["1: unclear\n2: yes", "Fait."]))
    await adult_chat.say("oui pour le deuxième")
    (given,) = model.calls[0].messages
    assert isinstance(given, ChatMessage)
    second = json.loads(given.content)["requests"][1]["says"]
    yes, no = (b, a) if second == t("action.describe.profile.rename", "en", name="B") else (a, b)
    assert [await confirmation_status(c) for c in (yes, no)] == ["executed", "pending"]


async def test_for_an_explicit_yes_i_confirm_answers_after_domo_asked_for_it_only(
    adult_chat, display_name_of
):  # R74, explicit yes: a plain yes is an answer, which Domo's next reply asks to confirm
    await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"}, judge="explicit_yes: odd")
    adult_chat.use_model(
        FakeChatModel(tool_script=["yes", "Dis « je confirme ».", "unclear", "Ah ?"])
    )
    await adult_chat.say("oui")  # a plain yes: Domo asks for "je confirme"
    await adult_chat.say("hmm")  # another exchange: the request is two replies back now
    model = adult_chat.use_model(FakeChatModel(["Confirmer quoi ?"]))
    await adult_chat.say("je confirme")
    assert len(model.calls) == 1 and await display_name_of(adult_chat.person) == "Alex"


async def test_a_word_decision_is_recorded_before_its_card_is_built(
    adult_chat, display_name_of, monkeypatch
):  # Minor 1
    async def broken(*args, **kwargs):
        raise RuntimeError("the card could not be built")

    await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    monkeypatch.setattr(page_render, "card_after_decision", broken)
    adult_chat.use_model(FakeChatModel(["?"]))
    answer = await adult_chat.say("oui")
    assert await display_name_of(adult_chat.person) == "Alex B"
    assert answer.actions == [{"tool": "profile_rename", "status": "done"}]


async def test_the_page_shows_a_request_card_once_after_a_word_decision(adult_chat):  # Minor 4
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    adult_chat.use_model(FakeChatModel(["Voilà, fait."]))
    await adult_chat.say("oui")
    page = (await adult_chat.get("/chat")).text
    assert page.count(f'data-confirmation-id="{reply.pending_confirmation_id}"') == 1
    done = t("action.card.done.profile.rename", "en", name="Alex B")
    # The deciding reply shows what came of it as a plain action card.
    assert re.search(
        r'<div class="message assistant"[^>]*>\s*<div class="message-text">'
        + re.escape("Voilà, fait.")
        + r'</div>\s*<div class="message-actions"[^>]*>\s*<div class="action-card" '
        r'data-action-status="done"',
        page,
    )
    assert _plain(page).count(done) == 2  # once in the request's card, once under the answer


async def test_a_request_whose_requester_left_can_no_longer_be_carried_out(
    child_chat, live_adult_client, chat_server, display_name_of, an_adult_agrees_to_a_childs_rename
):  # Minor 5 and 8: R21's inactive requester, and the note that says so
    reply = await child_chat.say_with_tool("profile_rename", {"name": "Sam B"})
    async with chat_server.session_factory() as writer:
        await writer.execute(
            text("UPDATE persons SET disabled_at = now(), state = 'gone' WHERE id = :id"),
            {"id": child_chat.person.id},
        )
        await writer.commit()
    r = await live_adult_client.post(f"/confirmations/{reply.pending_confirmation_id}/approve")
    assert status_of(r) == "refused" and await display_name_of(child_chat.person) == "Sam"
    assert t("confirmation.outcome.no_longer_possible", "en") in _plain(r.text)
    assert t("confirmation.outcome.not_allowed", "en") not in _plain(r.text)


async def test_an_expired_request_says_so_clearly(adult_chat, clock):  # Minor 8, Review Focus 2
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    clock.advance(timedelta(hours=168, seconds=1))
    r = await adult_chat.post(f"/confirmations/{reply.pending_confirmation_id}/approve")
    assert t("confirmation.outcome.expired", "en") in _plain(r.text)
    assert "data-action=" not in r.text


async def test_the_service_itself_refuses_a_plain_yes_to_an_explicit_request(
    adult_chat, chat_server, confirmation_status, clock
):  # Minor 8: the second lock, reached without read_reply
    reply = await adult_chat.say_with_tool(
        "profile_rename", {"name": "Alex B"}, judge="explicit_yes: odd"
    )

    def never(requester, conversation_id):
        raise AssertionError("nothing may run")

    for via in ("phrase", "classifier"):
        outcome = await decide_confirmation(
            session_factory=chat_server.session_factory,
            confirmation_id=reply.pending_confirmation_id,
            decider=adult_chat.person,
            approve=True,
            via=via,
            reply_message_id=None,
            ctx_factory=never,
            registry=TOOLS,
            policy=DEFAULT_POLICY,
            now=clock(),
        )
        assert outcome == ("needs_explicit", None)
    assert await confirmation_status(reply.pending_confirmation_id) == "pending"


# --- a chain of actions (spec 4) -----------------------------------------------------------------

# A chain the owner asks for: a look at the household, then Alex made a child.
_A_CHAIN = {
    "steps": [
        {"tool": "household_list", "arguments": {}},
        {"tool": "household_change_role", "arguments": {"person": "Alex", "role": "child"}},
    ]
}


@pytest.mark.parametrize("ask_again", [False, True])
async def test_a_chain_is_changed_as_a_whole_and_judged_again(
    owner_chat, adult_member, person_row, confirmation_status, steps_rows, ask_again
):  # spec 4.1, 4.7 (test_chain_amend.py tells the rest)
    # The judge asks a yes of the changed chain (the fake judge's default), or Domo asks again:
    # it is a new request, with the step it never showed.
    reply = await owner_chat.say_with_tool("do_in_order", _A_CHAIN)
    rid = str(reply.pending_confirmation_id)
    other = {
        "steps": [
            {"tool": "household_list", "arguments": {}},
            {"tool": "household_remove", "arguments": {"person": "Alex"}},
        ]
    }
    call = ToolCall(
        "c1",
        "confirmation_amend",
        json.dumps({"request_id": rid, "arguments": other, "ask_again": ask_again}),
    )
    owner_chat.use_model(FakeChatModel(tool_script=["change", call, "Ok."]))
    answer = await owner_chat.say("oui, mais retire plutôt Alex")
    assert answer.model_results[0]["outcome"] == "asked_again"
    assert await confirmation_status(rid) == "replaced"
    new = answer.model_results[0]["request_id"]
    assert [(row.tool_name, row.status) for row in await steps_rows(new)] == [
        ("household_list", "planned"),
        ("household_remove", "planned"),
    ]
    assert (await person_row(adult_member.person)).disabled_at is None


async def test_a_rule_raised_after_a_chain_was_asked_for_reaches_it(
    owner_chat, adult_member, grant, confirmation_status, confirmation_ask, person_row
):
    reply = await owner_chat.say_with_tool("do_in_order", _A_CHAIN)
    assert reply.model_results[-1]["answer_with"] == ANSWER_IN_WORDS
    rid = reply.pending_confirmation_id
    # The owner's rule asks an explicit yes for one of its steps now: so does the chain that waits.
    await grant("household.change_role", person=owner_chat.person, ask="explicit_yes")
    owner_chat.use_model(FakeChatModel(["Il faut le bouton, ou « je confirme »."]))
    await owner_chat.say("oui")
    assert await confirmation_status(rid) == "pending"
    assert (await person_row(adult_member.person)).role == "adult"
    assert await confirmation_ask(rid) == ("explicit_yes", "rule", None)
    await owner_chat.say("je confirme")
    assert await confirmation_status(rid) == "executed"
    assert (await person_row(adult_member.person)).role == "child"


# --- helpers of this file ------------------------------------------------------------------------


async def _conversation_of(chat_server, message_id: uuid.UUID) -> uuid.UUID:
    async with chat_server.session_factory() as reader:
        return (
            await reader.execute(
                text("SELECT conversation_id FROM messages WHERE id = :id"), {"id": message_id}
            )
        ).scalar_one()


def _plain(fragment: str) -> str:
    """What a person reads in a fragment."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def _pending_in(system: str) -> list[dict]:
    """The pending requests of a system prompt's context, as Domo reads them (its JSON list)."""
    block = system.split(PENDING_IN_CONTEXT + ":\n")[1]
    return json.loads(block.split("\n\n")[0])


def test_the_reason_domo_is_not_sure_is_told_in_the_language_of_the_page():
    c = Confirmation(ask="yes", asked_by="domo", ask_why=NOT_SURE_REASON)
    asked = confirmation_routes._asked(
        c, Person(role="adult"), CatalogTexts("en"), rights=NO_RIGHTS, policy=DEFAULT_POLICY
    )
    assert asked == t(
        "confirmation.asked.domo.yes", "en", reason=t("confirmation.asked.domo.not_sure", "en")
    )
    # In another language, the reason is a text of the reader's version, never the English one
    # kept with the request.
    keys = ("confirmation.asked.domo.yes", "confirmation.asked.domo.not_sure")
    french = RecueilTexts(
        language="fr",
        zone=ZoneInfo("UTC"),
        found={key: f"(fr) {meaning(key)}" for key in keys},
        personal=frozenset(),
    )
    asked = confirmation_routes._asked(
        c, Person(role="adult"), french, rights=NO_RIGHTS, policy=DEFAULT_POLICY
    )
    assert asked == in_language(
        "fr",
        "confirmation.asked.domo.yes",
        reason=in_language("fr", "confirmation.asked.domo.not_sure"),
    )
