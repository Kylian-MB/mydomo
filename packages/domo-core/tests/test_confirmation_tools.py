"""Domo manages the pending requests (task 7): it reminds the person (the card comes back),
drops a request that has waited too long, requires an explicit check when a request looks odd,
and applies a "yes, but change X" answer (``confirmation_amend``).

The yes always comes from the person's words or a button (decision D2): Domo may only change a
request right after the person answered it with a change, in that very turn (``change_grant``).
The changed action is judged again: it runs without a new question when the judge says ``do``,
unless Domo asks again (more than the judge: ``ask_again`` or ``ask_before_acting``) or the
request asks an explicit yes or a proof (words never give those): it then becomes a new request.

The tests use ``profile_rename``, which waits for a yes when the judge asks one (the fake judge
says yes by default): the observable effect is the name the person is called by. The chat pilots
talk with Domo on a live server (see ``chat_support.py``).
"""

import asyncio
import html
import json
import re
import uuid
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from chat_support import Reply, confirmation_cards, status_of
from domo_common.i18n import t
from domo_core.accounts.actors import actor_of
from domo_core.chat.models import Conversation, Message
from domo_core.chat.store import ConversationStore
from domo_core.confirmations import service as confirmations_service
from domo_core.confirmations.models import Confirmation
from domo_core.confirmations.service import amend_confirmation, decide_confirmation
from domo_core.confirmations.store import (
    ANSWER_BY_THE_OWNER,
    ANSWER_EXPLICITLY,
    ANSWER_IN_WORDS,
)
from domo_core.conversation import execution
from domo_core.llm.types import ChatChunk, ChatMessage, ChatUsage, ToolCall, ToolDefinition
from domo_core.memory.models import Memory
from domo_core.memory.told import told
from domo_core.policy.actions import DescribedAction
from domo_core.policy.catalog import (
    CHANGES_NOTHING,
    DEFAULT_POLICY,
    KNOWN_ACTIONS,
    PolicyTables,
    is_silent,
)
from domo_core.policy.models import RightsDraft
from domo_core.policy.roles import Actor
from domo_core.tools.context import ToolContext, ToolOutcome
from domo_core.tools.registry import TOOLS, InvalidArguments
from domo_core.web.deps import get_policy, get_tool_registry
from fakes import JUDGE_DEFAULT as JUDGE_ASKS
from fakes import FakeCall, FakeChatModel, is_reader_call
from sqlalchemy import select, text, update
from test_rights_tools import (
    SUMMARY,
    THREE_DAYS,
    DomoAndWriter,
    WriterAnswer,
    alex_invites,
    drafts,
    live,
    propose,
    rights_of,
)
from web_support import audit_entries


def amend(rid, args, ask_again=False, note=None):
    """Domo's change of the request ``rid``; with ``note``, what an approver said with it."""
    arguments = {"request_id": rid, "arguments": args, "ask_again": ask_again}
    if note is not None:
        arguments["note"] = note
    return ToolCall("c1", "confirmation_amend", json.dumps(arguments))


# --- the brief's tests ---------------------------------------------------------------------------


async def test_a_yes_with_a_change_runs_the_changed_action(
    adult_chat, display_name_of, confirmation_status
):  # Review Focus 6
    rid = str(
        (
            await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
        ).pending_confirmation_id
    )
    adult_chat.use_model(
        FakeChatModel(
            tool_script=["change", amend(rid, {"name": "Alex C"}), "C'est fait, avec Alex C."],
            judge="do",
        )
    )
    await adult_chat.say("c'est ok, mais mets plutôt Alex C")
    assert await display_name_of(adult_chat.person) == "Alex C"
    assert await confirmation_status(rid) == "executed"


async def test_no_change_without_the_persons_answer(
    adult_chat, display_name_of, confirmation_status
):  # Review Focus 6
    rid = str(
        (
            await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
        ).pending_confirmation_id
    )
    reply = await adult_chat.say_with_tool(
        "confirmation_amend",
        {"request_id": rid, "arguments": {"name": "Alex Z"}, "ask_again": False},
    )
    assert reply.actions[0]["status"] == "invalid"
    assert await display_name_of(adult_chat.person) == "Alex"
    assert await confirmation_status(rid) == "pending"


async def test_a_change_with_bad_arguments_changes_nothing(
    adult_chat, confirmation_status
):  # Review Focus 6
    rid = str(
        (
            await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
        ).pending_confirmation_id
    )
    adult_chat.use_model(FakeChatModel(tool_script=["change", amend(rid, {"name": ""}), "Hmm."]))
    reply = await adult_chat.say("vas-y, mais enlève le nom")
    assert reply.actions[0]["status"] == "invalid" and await confirmation_status(rid) == "pending"


async def test_domo_can_choose_to_ask_again(
    adult_chat, display_name_of, confirmation_status, confirmation_ask
):  # ask_again is Domo asking more than the judge: a plain yes, with his reason
    rid = str(
        (
            await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
        ).pending_confirmation_id
    )
    adult_chat.use_model(
        FakeChatModel(
            tool_script=[
                "change",
                amend(rid, {"name": "Alex C"}, ask_again=True),
                "Je mets Alex C, d'accord ?",
            ],
            judge="do",
        )
    )
    reply = await adult_chat.say("plutôt Alex C, et ajoute un deuxième prénom")
    assert await confirmation_status(rid) == "replaced"
    assert await display_name_of(adult_chat.person) == "Alex"
    result = json.loads(reply.model_result_text)
    assert (result["asked_by"], result["why"]) == ("domo", "The assistant is not sure")
    assert await confirmation_ask(result["request_id"]) == (
        "yes",
        "domo",
        "The assistant is not sure",
    )
    await adult_chat.say("oui")
    assert await display_name_of(adult_chat.person) == "Alex C"


async def test_an_explicit_yes_asked_turns_a_change_into_a_new_request(
    adult_chat, display_name_of, confirmation_status, confirmation_ask
):  # F2: words never give an explicit yes
    rid = str(
        (
            await adult_chat.say_with_tool(
                "profile_rename", {"name": "Alex B"}, judge="explicit_yes: odd"
            )
        ).pending_confirmation_id
    )
    adult_chat.use_model(
        FakeChatModel(tool_script=["change", amend(rid, {"name": "Alex C"}), "Confirme Alex C."])
    )
    reply = await adult_chat.say("Tu peux le faire après avoir mis Alex C")
    assert await confirmation_status(rid) == "replaced"
    assert await display_name_of(adult_chat.person) == "Alex"
    new = json.loads(reply.model_result_text)["request_id"]
    assert await confirmation_ask(new) == ("explicit_yes", "judge", "odd")


async def test_domo_may_ask_more_on_a_change(
    adult_chat, display_name_of, confirmation_status, confirmation_ask
):  # F15: what Domo asks more reaches what confirmation_amend carries
    rid = await _rename_request(adult_chat)
    adult_chat.use_model(
        FakeChatModel(
            tool_script=[
                "change",
                ToolCall(
                    "a1",
                    "ask_before_acting",
                    json.dumps({"ask": "explicit_yes", "reason": "a new name"}),
                ),
                amend(rid, {"name": "Alex C"}),
                "Confirme ?",
            ],
            judge="do",
        )
    )
    reply = await adult_chat.say("oui, mais plutôt Alex C")
    result = reply.model_results[-1]
    assert result["outcome"] == "asked_again" and result["answer_with"] == ANSWER_EXPLICITLY
    assert await confirmation_status(rid) == "replaced"
    assert await confirmation_ask(result["request_id"]) == ("explicit_yes", "domo", "a new name")
    assert await display_name_of(adult_chat.person) == "Alex"


async def test_domo_can_ask_for_an_explicit_check(adult_chat, display_name_of):
    rid = str(
        (
            await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
        ).pending_confirmation_id
    )
    await adult_chat.say_with_tool(
        "confirmation_require_explicit", {"request_id": rid, "reason": "Unusual for you."}
    )
    model = adult_chat.use_model(FakeChatModel(["Dis « je confirme »."]))
    await adult_chat.say("oui")
    # The reader is asked whether that yes is firm (it is not); then Domo answers.
    assert len(model.calls) == 2 and is_reader_call(model.calls[0].system)
    assert await display_name_of(adult_chat.person) == "Alex"
    await adult_chat.say("je confirme")
    assert await display_name_of(adult_chat.person) == "Alex B"


async def test_domo_can_expire_a_request(adult_chat, display_name_of):  # Review Focus 2
    rid = str(
        (
            await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
        ).pending_confirmation_id
    )
    await adult_chat.say_with_tool("confirmation_expire", {"request_id": rid})
    r = await adult_chat.post(f"/confirmations/{rid}/approve")
    assert status_of(r) == "expired" and await display_name_of(adult_chat.person) == "Alex"


async def test_domo_can_remind_and_the_card_comes_back(adult_chat):
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    again = await adult_chat.say_with_tool(
        "confirmation_remind", {"request_id": str(reply.pending_confirmation_id)}
    )
    assert again.pending_confirmation_id == reply.pending_confirmation_id
    assert again.actions[0]["status"] == "done" and '"reminders": 1' in again.model_result_text


async def test_domo_cannot_touch_someone_elses_request(adult_chat, child_chat, confirmation_status):
    reply = await child_chat.say_with_tool("profile_rename", {"name": "Sam B"})
    rid = str(reply.pending_confirmation_id)
    for tool, args in [
        ("confirmation_expire", {"request_id": rid}),
        ("confirmation_require_explicit", {"request_id": rid, "reason": "x"}),
    ]:
        assert (await adult_chat.say_with_tool(tool, args)).actions[0]["status"] == "invalid"
    assert await confirmation_status(rid) == "pending"


# --- applying a change ---------------------------------------------------------------------------


async def test_an_executed_change_is_recorded_and_says_what_changed(
    adult_chat, chat_server
):  # R75: an executed change runs and shows the amended arguments
    rid = (
        await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    ).pending_confirmation_id
    adult_chat.use_model(
        FakeChatModel(
            tool_script=["change", amend(str(rid), {"name": "Alex C"}), "C'est fait."], judge="do"
        )
    )
    reply = await adult_chat.say("c'est ok, mais mets plutôt Alex C")
    assert reply.actions == [{"tool": "confirmation_amend", "status": "done"}]
    assert json.loads(reply.model_result_text) == {
        "status": "done",
        "outcome": "executed",
        "result": {"status": "done", "name": "Alex C"},
    }
    async with chat_server.session_factory() as reader:
        c = (await reader.execute(select(Confirmation).where(Confirmation.id == rid))).scalar_one()
        person_message = await _person_message(reader, "c'est ok, mais mets plutôt Alex C")
        decided = [e for e in await audit_entries(reader) if e.action == "confirmation.decided"]
    assert (c.arguments, c.amended_arguments) == ({"name": "Alex B"}, {"name": "Alex C"})
    assert (c.decided_via, c.decided_by, c.decided_message_id) == (
        "change",
        adult_chat.person.id,
        person_message,
    )
    assert c.result == {"key": "action.card.done.profile.rename", "params": {"name": "Alex C"}}
    assert [e.details for e in decided] == [
        {
            "confirmation_id": str(rid),
            "action_type": "profile.rename",
            "outcome": "executed",
            "approver_kind": "self",
            "via": "change",
            "changed": True,
        }
    ]
    # The card under the reply is the request's: what ran, and what it was before.
    (card,) = confirmation_cards(reply.cards_html)
    assert f'data-confirmation-id="{rid}"' in card
    assert 'data-confirmation-status="executed"' in card and "data-action=" not in card
    assert t("action.describe.profile.rename", "en", name="Alex C") in _plain(card)
    before = t("action.describe.profile.rename", "en", name="Alex B")
    assert t("confirmation.changed", "en", before=before) in _plain(card)
    # The page shows the request as it ran, with the change.
    page = (await adult_chat.get("/chat")).text
    shown = confirmation_cards(page)[0]
    assert t("action.describe.profile.rename", "en", name="Alex C") in _plain(shown)
    assert t("confirmation.changed", "en", before=before) in _plain(shown)


async def test_the_change_is_allowed_for_this_turn_only(
    adult_chat, display_name_of, confirmation_status
):  # Review Focus 6
    rid = str(
        (
            await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
        ).pending_confirmation_id
    )
    adult_chat.use_model(FakeChatModel(tool_script=["change", "Tu veux quel nom ?"]))
    await adult_chat.say("oui mais change le nom")
    # The next turn is no answer to the request any more (it was not shown again).
    adult_chat.use_model(FakeChatModel(tool_script=[amend(rid, {"name": "Alex C"}), "Voilà."]))
    reply = await adult_chat.say("Alex C")
    assert reply.actions == [
        {"tool": "confirmation_amend", "status": "invalid", "reason": "invalid"}
    ]
    assert await display_name_of(adult_chat.person) == "Alex"
    assert await confirmation_status(rid) == "pending"


async def test_a_change_is_applied_once(
    adult_chat, display_name_of
):  # the grant is used up by the change it allowed
    rid = str(
        (
            await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
        ).pending_confirmation_id
    )
    adult_chat.use_model(
        FakeChatModel(
            tool_script=[
                "change",
                amend(rid, {"name": "Alex C"}),
                amend(rid, {"name": "Alex D"}),
                "C'est fait.",
            ],
            judge="do",
        )
    )
    reply = await adult_chat.say("ok mais plutôt Alex C")
    assert [a["status"] for a in reply.actions] == ["done", "invalid"]
    assert await display_name_of(adult_chat.person) == "Alex C"


async def test_a_change_cannot_name_another_tool(
    adult_chat, confirmation_status, display_name_of
):  # Review Focus 6: the tool is the request's
    rid = str(
        (
            await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
        ).pending_confirmation_id
    )
    other_tool = ToolCall(
        "c1",
        "confirmation_amend",
        json.dumps(
            {
                "request_id": rid,
                "arguments": {"name": "Alex C"},
                "ask_again": False,
                "tool": "household_list",
            }
        ),
    )
    other_arguments = amend(rid, {"names": "Alex C"})  # not what profile_rename takes
    adult_chat.use_model(FakeChatModel(tool_script=["change", other_tool, other_arguments, "Hmm."]))
    reply = await adult_chat.say("oui mais autrement")
    assert [a["status"] for a in reply.actions] == ["invalid", "invalid"]
    assert await confirmation_status(rid) == "pending"
    assert await display_name_of(adult_chat.person) == "Alex"


async def test_a_change_the_barrier_refuses_is_not_run(
    adult_chat, chat_server, display_name_of, confirmation_status
):  # the changed action goes through the barrier again
    rid = str(
        (
            await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
        ).pending_confirmation_id
    )
    no_rename = PolicyTables(
        known=KNOWN_ACTIONS,
        allowed={
            role: actions - {"profile.rename"} for role, actions in DEFAULT_POLICY.allowed.items()
        },
    )
    chat_server.app.dependency_overrides[get_policy] = lambda: no_rename
    adult_chat.use_model(
        FakeChatModel(tool_script=["change", amend(rid, {"name": "Alex C"}), "Ah, non."])
    )
    reply = await adult_chat.say("d'accord, mais Alex C")
    assert json.loads(reply.model_result_text) == {
        "status": "done",
        "outcome": "refused",
        "reason": "not_allowed",
    }
    assert await confirmation_status(rid) == "refused"
    assert await display_name_of(adult_chat.person) == "Alex"


async def test_a_change_a_limit_refuses_ends_refused_for_the_limit(
    adult_chat, grant, display_name_of, confirmation_status
):  # the changed action meets the owner's limits as they are now
    rid = await _rename_request(adult_chat)
    await grant("profile.rename", role="adult", limit='"refuse"', summary="Adults keep a name.")
    adult_chat.use_model(
        FakeChatModel(tool_script=["change", amend(rid, {"name": "Alex C"}), "Ah, non."])
    )
    reply = await adult_chat.say("d'accord, mais Alex C")
    assert json.loads(reply.model_result_text) == {
        "status": "done",
        "outcome": "refused",
        "reason": "limit",
    }
    assert await confirmation_status(rid) == "refused"
    assert await display_name_of(adult_chat.person) == "Alex"


async def test_a_change_after_a_rule_was_raised_needs_the_stricter_yes(
    adult_chat, grant, chat_server, display_name_of, confirmation_status
):  # Ruling R7, F21: what the owner's rule asks now
    rid = await _rename_request(adult_chat)
    await grant("profile.rename", role="adult", ask="explicit_yes")
    adult_chat.use_model(
        FakeChatModel(tool_script=["change", amend(rid, {"name": "Alex C"}), "Confirme ?"])
    )
    reply = await adult_chat.say("oui, mais Alex C")
    # Not run: the change is a new request, asking what the rule asks: the explicit yes.
    result = json.loads(reply.model_result_text)
    assert (result["outcome"], result["answer_with"]) == ("asked_again", ANSWER_EXPLICITLY)
    assert await confirmation_status(rid) == "replaced"
    assert await display_name_of(adult_chat.person) == "Alex"
    async with chat_server.session_factory() as reader:
        new = (
            await reader.execute(select(Confirmation).where(Confirmation.status == "pending"))
        ).scalar_one()
    assert (new.ask, new.asked_by, new.approver_kind) == ("explicit_yes", "rule", "self")


async def test_a_new_request_keeps_what_domo_could_not_tell(
    adult_chat, chat_server
):  # ``_ask_again`` carries the ``ask_reason`` over
    rid = await _rename_request(adult_chat)
    async with chat_server.session_factory() as writer:
        await writer.execute(
            text("UPDATE confirmations SET ask_reason = 'Is Alex C a real name?' WHERE id = :id"),
            {"id": rid},
        )
        await writer.commit()
    adult_chat.use_model(
        FakeChatModel(
            tool_script=["change", amend(rid, {"name": "Alex C"}, ask_again=True), "D'accord ?"]
        )
    )
    await adult_chat.say("plutôt Alex C")
    async with chat_server.session_factory() as reader:
        new = (
            await reader.execute(select(Confirmation).where(Confirmation.status == "pending"))
        ).scalar_one()
    assert (new.ask, new.ask_reason) == ("yes", "Is Alex C a real name?")


async def test_a_change_whose_yes_is_now_the_owners_waits_for_the_owner(
    adult_chat, grant, chat_server, display_name_of, confirmation_status
):  # Ruling R27, on the way of a change
    rid = await _rename_request(adult_chat)
    await grant("profile.rename", role="adult", approver="owner")
    adult_chat.use_model(
        FakeChatModel(tool_script=["change", amend(rid, {"name": "Alex C"}), "Le propriétaire ?"])
    )
    reply = await adult_chat.say("oui, mais Alex C")
    result = json.loads(reply.model_result_text)
    assert (result["outcome"], result["answer_with"]) == ("approver_changed", ANSWER_BY_THE_OWNER)
    assert await confirmation_status(rid) == "replaced"
    assert await display_name_of(adult_chat.person) == "Alex"
    async with chat_server.session_factory() as reader:
        new = (
            await reader.execute(select(Confirmation).where(Confirmation.status == "pending"))
        ).scalar_one()
    assert (new.approver_kind, new.amended_arguments, new.arguments) == (
        "owner",
        None,
        {"name": "Alex C"},
    )


async def test_the_reply_that_asks_again_makes_and_shows_the_new_request(
    adult_chat, chat_server
):  # R74: the reply that makes the new request shows it
    rid = (
        await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    ).pending_confirmation_id
    adult_chat.use_model(
        FakeChatModel(
            tool_script=[
                "change",
                amend(str(rid), {"name": "Alex C"}, ask_again=True),
                "D'accord ?",
            ]
        )
    )
    reply = await adult_chat.say("plutôt Alex C")
    assert json.loads(reply.model_result_text)["outcome"] == "asked_again"
    async with chat_server.session_factory() as reader:
        old = (
            await reader.execute(select(Confirmation).where(Confirmation.id == rid))
        ).scalar_one()
        new = (
            await reader.execute(select(Confirmation).where(Confirmation.id == old.replaced_by))
        ).scalar_one()
    assert (old.status, old.decided_via) == ("replaced", "change")
    assert (new.status, new.tool_name, new.arguments) == (
        "pending",
        "profile_rename",
        {"name": "Alex C"},
    )
    assert (new.ask, new.approver_kind, new.requester_id) == ("yes", "self", old.requester_id)
    assert new.message_id == new.last_shown_message_id == reply.message_id
    # Its card, with its buttons, is under the reply, and says what changed.
    assert reply.pending_confirmation_id == new.id
    (card,) = confirmation_cards(reply.cards_html)
    assert 'data-confirmation-status="pending"' in card and 'data-action="confirm"' in card
    before = t("action.describe.profile.rename", "en", name="Alex B")
    assert t("confirmation.changed", "en", before=before) in _plain(card)
    assert json.loads(reply.model_result_text)["request_id"] == str(new.id)
    # Domo is told how the new request is answered (final review Important 1).
    assert json.loads(reply.model_result_text)["answer_with"] == ANSWER_IN_WORDS


async def test_a_new_request_keeps_the_explicit_check(adult_chat, chat_server, display_name_of):
    rid = str(
        (
            await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
        ).pending_confirmation_id
    )
    await adult_chat.say_with_tool(
        "confirmation_require_explicit", {"request_id": rid, "reason": "Unusual for you."}
    )
    adult_chat.use_model(
        FakeChatModel(tool_script=["change", amend(rid, {"name": "Alex C"}), "Confirme ?"])
    )
    reply = await adult_chat.say("plutôt Alex C")
    # Domo asked for an explicit check: the change does not run, it is a new request, which
    # takes the button or "I confirm" (and Domo is told so).
    assert json.loads(reply.model_result_text)["outcome"] == "asked_again"
    assert json.loads(reply.model_result_text)["answer_with"] == ANSWER_EXPLICITLY
    assert await display_name_of(adult_chat.person) == "Alex"
    async with chat_server.session_factory() as reader:
        new = (
            await reader.execute(select(Confirmation).where(Confirmation.status == "pending"))
        ).scalar_one()
    assert (new.ask, new.asked_by, new.ask_why) == ("explicit_yes", "domo", "Unusual for you.")
    (card,) = confirmation_cards(reply.cards_html)
    assert t("confirmation.hint.explicit", "en") in _plain(card)
    assert t("confirmation.asked.domo.explicit_yes", "en", reason="Unusual for you.") in _plain(
        card
    )
    adult_chat.use_model(FakeChatModel(["Dis « je confirme »."]))
    await adult_chat.say("oui")
    assert await display_name_of(adult_chat.person) == "Alex"
    await adult_chat.say("je confirme")
    assert await display_name_of(adult_chat.person) == "Alex C"


async def test_the_service_never_runs_a_change_that_asked_an_explicit_yes(
    adult_chat, chat_server, confirmation_status, clock
):  # R75: the second lock covers a change too

    def never(requester, conversation_id):
        raise AssertionError("nothing may run")

    reply = await adult_chat.say_with_tool(
        "profile_rename", {"name": "Alex B"}, judge="explicit_yes: odd"
    )
    outcome = await decide_confirmation(
        session_factory=chat_server.session_factory,
        confirmation_id=reply.pending_confirmation_id,
        decider=adult_chat.person,
        approve=True,
        via="change",
        reply_message_id=None,
        ctx_factory=never,
        registry=TOOLS,
        policy=DEFAULT_POLICY,
        now=clock(),
    )
    assert outcome == ("needs_explicit", None)
    assert await confirmation_status(reply.pending_confirmation_id) == "pending"


async def test_a_button_runs_what_the_card_shows(
    adult_chat, chat_server, display_name_of
):  # R75: the arguments a card describes are the ones a yes runs
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    async with chat_server.session_factory() as writer:
        await writer.execute(
            text("""UPDATE confirmations SET amended_arguments = '{"name": "Alex Z"}'""")
        )
        await writer.commit()
    (card,) = confirmation_cards((await adult_chat.get("/chat")).text)
    assert t("action.describe.profile.rename", "en", name="Alex Z") in _plain(card)
    await adult_chat.confirm_last(reply)
    assert await display_name_of(adult_chat.person) == "Alex Z"


# --- reminding, dropping, checking more ----------------------------------------------------------


async def test_a_reminder_lets_the_next_yes_answer_the_request(
    adult_chat, display_name_of
):  # R74: the reply that reminds presents the request again
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    adult_chat.use_model(FakeChatModel(tool_script=["unclear", "Je sais lister la maison."]))
    await adult_chat.say("qu'est-ce que tu sais faire ?")
    again = await adult_chat.say_with_tool(
        "confirmation_remind", {"request_id": str(reply.pending_confirmation_id)}
    )
    # The card comes back with its buttons.
    (card,) = confirmation_cards(again.cards_html)
    assert 'data-confirmation-status="pending"' in card and 'data-action="confirm"' in card
    model = adult_chat.use_model(FakeChatModel(["Voilà."]))
    await adult_chat.say("oui")
    assert len(model.calls) == 1 and await display_name_of(adult_chat.person) == "Alex B"


async def test_reminders_are_counted_and_domo_sees_them(adult_chat, chat_server):
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    rid = str(reply.pending_confirmation_id)
    await adult_chat.say_with_tool("confirmation_remind", {"request_id": rid})
    again = await adult_chat.say_with_tool("confirmation_remind", {"request_id": rid})
    assert '"reminders": 2' in again.model_result_text
    async with chat_server.session_factory() as reader:
        c = (await reader.execute(select(Confirmation))).scalar_one()
    assert c.reminder_count == 2 and c.last_reminded_at == adult_chat.clock()
    model = adult_chat.use_model(FakeChatModel(tool_script=["unclear", "Alors ?"]))
    await adult_chat.say("hmm")
    assert '"reminders": 2' in model.calls[1].system


async def test_an_explicit_check_shows_its_reason_on_the_card(adult_chat, chat_server):
    rid = str(
        (
            await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
        ).pending_confirmation_id
    )
    reply = await adult_chat.say_with_tool(
        "confirmation_require_explicit", {"request_id": rid, "reason": "<b>Unusual</b> for you."}
    )
    assert json.loads(reply.model_result_text) == {"status": "done"}
    (card,) = confirmation_cards(reply.cards_html)
    assert t("confirmation.hint.explicit", "en") in _plain(card)
    assert "<b>" not in card and "&lt;b&gt;Unusual&lt;/b&gt; for you." in card
    async with chat_server.session_factory() as reader:
        c = (await reader.execute(select(Confirmation))).scalar_one()
        entries = await audit_entries(reader)
    assert (c.ask, c.asked_by, c.ask_why) == ("explicit_yes", "domo", "<b>Unusual</b> for you.")
    (entry,) = [e for e in entries if e.action == "confirmation.explicit_required"]
    # Metadata only: never the reason.
    assert entry.details == {"confirmation_id": rid, "action_type": "profile.rename"}
    assert entry.actor == f"person:{adult_chat.person.id}"


async def test_an_explicit_check_never_asks_less_than_the_request_does(
    adult_chat, confirmation_ask, audit_actions
):  # Domo asks more, never less
    rid = str(
        (
            await adult_chat.say_with_tool(
                "profile_rename", {"name": "Alex B"}, judge="proof: not sure"
            )
        ).pending_confirmation_id
    )
    reply = await adult_chat.say_with_tool(
        "confirmation_require_explicit", {"request_id": rid, "reason": "Unusual for you."}
    )
    assert json.loads(reply.model_result_text) == {"status": "done", "already": "proof"}
    assert await confirmation_ask(rid) == ("proof", "judge", "not sure")
    assert "confirmation.explicit_required" not in await audit_actions()


@pytest.mark.parametrize(
    ("reason", "code"),
    [("", "invalid"), ("   ", "invalid"), ("x" * 201, "too_long"), ("one\ntwo", "invalid")],
)
async def test_the_reason_of_an_explicit_check_is_one_short_line(
    adult_chat, chat_server, reason, code
):
    rid = str(
        (
            await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
        ).pending_confirmation_id
    )
    reply = await adult_chat.say_with_tool(
        "confirmation_require_explicit", {"request_id": rid, "reason": reason}
    )
    assert reply.actions[0] == {
        "tool": "confirmation_require_explicit",
        "status": "invalid",
        "reason": code,
    }
    async with chat_server.session_factory() as reader:
        c = (await reader.execute(select(Confirmation))).scalar_one()
    assert (c.ask, c.asked_by) == ("yes", "judge")


async def test_an_expiry_by_domo_is_audited_and_the_card_says_expired(adult_chat, chat_server):
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    rid = str(reply.pending_confirmation_id)
    dropped = await adult_chat.say_with_tool("confirmation_expire", {"request_id": rid})
    assert json.loads(dropped.model_result_text) == {"status": "done"}
    (card,) = confirmation_cards(dropped.cards_html)
    assert 'data-confirmation-status="expired"' in card and "data-action=" not in card
    async with chat_server.session_factory() as reader:
        c = (await reader.execute(select(Confirmation))).scalar_one()
        entries = await audit_entries(reader)
    assert (c.status, c.expired_by) == ("expired", "domo")
    (entry,) = [e for e in entries if e.action == "confirmation.expired"]
    assert entry.details == {
        "confirmation_id": rid,
        "action_type": "profile.rename",
        "expired_by": "domo",
    }
    # The original card, on the page, says it expired.
    page = (await adult_chat.get("/chat")).text
    assert all('data-confirmation-status="expired"' in c for c in confirmation_cards(page))


async def test_domo_cannot_remind_someone_elses_request_or_an_unknown_one(
    adult_chat, child_chat, confirmation_status
):
    """Someone else's request is shown only to whoever may say yes to it (spec 5.1: an adult may
    show a child's, ``test_approver_in_words``): a child may not show an adult's."""
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    rid = str(reply.pending_confirmation_id)
    reminded = await child_chat.say_with_tool("confirmation_remind", {"request_id": rid})
    assert reminded.actions[0] == {
        "tool": "confirmation_remind",
        "status": "invalid",
        "reason": "not_found",
    }
    assert reminded.pending_confirmation_id is None
    for unknown in (str(uuid.uuid4()), "not-an-id"):
        r = await adult_chat.say_with_tool("confirmation_expire", {"request_id": unknown})
        assert r.actions[0]["status"] == "invalid"
    assert await confirmation_status(rid) == "pending"


async def test_members_get_the_request_tools_and_guests_do_not(adult_chat, guest_chat):
    model = adult_chat.use_model(FakeChatModel(["ok"]))
    await adult_chat.say("salut")
    names = {tool.name for tool in model.calls[0].tools}
    assert {
        "confirmation_remind",
        "confirmation_expire",
        "confirmation_require_explicit",
        "confirmation_amend",
        "household_leave",
    } <= names
    model = guest_chat.use_model(FakeChatModel(["ok"]))
    await guest_chat.say("salut")
    names = {tool.name for tool in model.calls[0].tools}
    assert not any(name.startswith("confirmation_") for name in names)
    # A guest may leave early, as every member may leave (spec 5.9).
    assert "household_leave" in names


# --- the tools, as the barrier sees them ----------------------------------------------------------

RID = uuid.uuid4()
WELL_FORMED = {
    "confirmation_remind": {"request_id": str(RID)},
    "confirmation_expire": {"request_id": str(RID)},
    "confirmation_require_explicit": {"request_id": str(RID), "reason": "Unusual for you."},
    "confirmation_amend": {"request_id": str(RID), "arguments": {"name": "A"}, "ask_again": False},
}


@pytest.mark.parametrize("name", list(WELL_FORMED))
async def test_a_request_tool_acts_on_the_persons_own_request(name):
    tool = TOOLS[name]
    action = await tool.describe(WELL_FORMED[name], Actor(uuid.uuid4(), "adult"), None)
    assert action == DescribedAction(
        "core", tool.action_type, f"confirmation:{RID}", "own_requests", "household"
    )
    parameters = tool.definition.parameters
    assert set(parameters["required"]) == set(WELL_FORMED[name])
    assert parameters["additionalProperties"] is False


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("confirmation_remind", {}),
        ("confirmation_remind", {"request_id": 3}),
        ("confirmation_remind", {"request_id": "not-an-id"}),
        ("confirmation_remind", {"request_id": str(RID), "reason": "x"}),
        ("confirmation_expire", {"id": str(RID)}),
        ("confirmation_require_explicit", {"request_id": str(RID)}),
        ("confirmation_require_explicit", {"request_id": str(RID), "reason": 7}),
        ("confirmation_amend", {"request_id": str(RID), "arguments": {"name": "A"}}),
        ("confirmation_amend", {"request_id": str(RID), "arguments": "A", "ask_again": False}),
        ("confirmation_amend", {"request_id": str(RID), "arguments": {}, "ask_again": "no"}),
    ],
)
async def test_a_request_tool_refuses_malformed_arguments(name, arguments):
    with pytest.raises(InvalidArguments) as refused:
        await TOOLS[name].describe(arguments, Actor(uuid.uuid4(), "adult"), None)
    assert refused.value.code == "invalid"


# --- what Domo reads in the history (R73) -------------------------------------------------------


async def test_the_history_tells_what_became_of_a_request_decided_by_button(adult_chat):
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    model = adult_chat.use_model(FakeChatModel(["ok"]))
    await adult_chat.say("hmm")
    assert (
        ChatMessage("assistant", "[action profile.rename: waiting]\nok") in model.calls[-1].messages
    )
    await adult_chat.confirm_last(reply)
    model = adult_chat.use_model(FakeChatModel(["ok"]))
    await adult_chat.say("merci")
    assert ChatMessage("assistant", "[action profile.rename: done]\nok") in model.calls[0].messages


async def test_the_history_tells_what_became_of_a_changed_request(adult_chat):
    rid = str(
        (
            await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
        ).pending_confirmation_id
    )
    adult_chat.use_model(
        FakeChatModel(
            tool_script=["change", amend(rid, {"name": "Alex C"}, ask_again=True), "D'accord ?"]
        )
    )
    await adult_chat.say("plutôt Alex C")
    model = adult_chat.use_model(FakeChatModel(tool_script=["unclear", "ok"]))
    await adult_chat.say("hmm")
    history = model.calls[-1].messages
    # The first request was replaced; the reply that changed it made a new one, which waits.
    assert ChatMessage("assistant", "[action profile.rename: replaced]\nok") in history
    assert (
        ChatMessage(
            "assistant",
            "[action profile.rename: waiting]\n[action confirmation.amend: done]\nD'accord ?",
        )
        in history
    )


async def test_the_history_tells_that_domo_dropped_a_request(adult_chat, session_factory):
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    dropped = await adult_chat.say_with_tool(
        "confirmation_expire", {"request_id": str(reply.pending_confirmation_id)}
    )
    # The history is in the order of the messages' times, which the database's clock gave; under
    # load that clock may step back between two of them (final review M12). Each of these four
    # gets an instant of its own, in the order they were written.
    async with session_factory() as writer:
        written = [
            await _person_message(writer, "Please use profile_rename."),
            reply.message_id,
            await _person_message(writer, "Please use confirmation_expire."),
            dropped.message_id,
        ]
        await _one_instant_each(writer, written)
        await writer.commit()
    model = adult_chat.use_model(FakeChatModel(["ok"]))
    await adult_chat.say("merci")
    assert model.calls[0].messages[1] == ChatMessage(
        "assistant", "[action profile.rename: expired]\nok"
    )
    assert model.calls[0].messages[3] == ChatMessage(
        "assistant", "[action confirmation.expire: done]\nok"
    )


# --- helpers of this file ------------------------------------------------------------------------


async def _person_message(reader, content: str) -> uuid.UUID:
    return (
        await reader.execute(
            text(
                "SELECT id FROM messages WHERE role = 'user' AND content = :content "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"content": content},
        )
    ).scalar_one()


async def _one_instant_each(session, message_ids: list[uuid.UUID]) -> None:
    """Give the messages ``message_ids`` times a second apart, in this order, an hour before
    now: a history that reads them no longer depends on the database's clock."""
    start = (await session.execute(text("SELECT clock_timestamp() - interval '1 hour'"))).scalar()
    for offset, message_id in enumerate(message_ids):
        await session.execute(
            update(Message)
            .where(Message.id == message_id)
            .values(created_at=start + timedelta(seconds=offset))
        )


def _plain(fragment: str) -> str:
    """What a person reads in a fragment."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


# --- fix round 1 ---------------------------------------------------------------------------------


class ActsFirstModel:
    """The model of a turn in which something happens while Domo thinks: the classifier reads
    the person's words as a change; the first call that may use tools awaits ``act`` (another tab
    presses a button, time passes...), then makes ``call``; every later call says "ok"."""

    def __init__(self, act, call: ToolCall) -> None:
        self._act = act
        self._call = call
        self._made = False
        self.calls: list[FakeCall] = []

    async def stream(
        self, *, system, messages, max_output_tokens, tools=(), tool_choice="auto", think=True
    ):
        self.calls.append(
            FakeCall(system, list(messages), tuple(tools), max_output_tokens, tool_choice)
        )
        if not tools:
            # The reader of the person's words: a change to the one request shown.
            yield ChatChunk("1: change" if is_reader_call(system) else "change")
        elif tool_choice == "auto" and not self._made:
            self._made = True
            await self._act()
            yield self._call
        else:
            yield ChatChunk("ok")
        yield ChatUsage(3, 2)


class SlowTool:
    """A tool of the registry that takes ``describe_seconds`` to describe a call, and
    ``run_seconds`` to run it (it does what the tool it wraps does)."""

    def __init__(self, tool, *, describe_seconds: float, run_seconds: float) -> None:
        self._tool = tool
        self._describe_seconds = describe_seconds
        self._run_seconds = run_seconds
        self.definition = tool.definition
        self.action_type = tool.action_type

    async def describe(self, arguments, actor, ctx):
        await asyncio.sleep(self._describe_seconds)
        return await self._tool.describe(arguments, actor, ctx)

    async def execute(self, arguments, ctx):
        await asyncio.sleep(self._run_seconds)
        return await self._tool.execute(arguments, ctx)


class PeekTool:
    """A tool that claims its call was about a request it was given (a buggy request tool)."""

    definition = ToolDefinition(
        name="household_list",
        description="Peek.",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
    )
    action_type = "household.list"

    def __init__(self, request_id: uuid.UUID) -> None:
        self._request_id = request_id

    async def describe(self, arguments, actor, ctx):
        return DescribedAction("core", "household.list", None, "read", "household")

    async def execute(self, arguments, ctx):
        card = {"key": "action.card.done", "params": {}}
        return ToolOutcome("done", {}, card, confirmation_id=self._request_id)


async def _rename_request(chat) -> str:
    reply = await chat.say_with_tool("profile_rename", {"name": "Alex B"})
    return str(reply.pending_confirmation_id)


@pytest.mark.parametrize("changes", [{}, {"name": "Alex B"}])
async def test_a_change_that_changes_nothing_is_refused(
    adult_chat,
    chat_server,
    display_name_of,
    confirmation_status,
    changes,
):  # Review Focus 6: never the original, stamped as a change
    rid = await _rename_request(adult_chat)
    adult_chat.use_model(FakeChatModel(tool_script=["change", amend(rid, changes), "Hmm."]))
    reply = await adult_chat.say("oui mais autrement")
    assert reply.actions == [
        {"tool": "confirmation_amend", "status": "invalid", "reason": "invalid"}
    ]
    assert await confirmation_status(rid) == "pending"
    assert await display_name_of(adult_chat.person) == "Alex"
    async with chat_server.session_factory() as reader:
        c = (await reader.execute(select(Confirmation))).scalar_one()
    assert (c.decided_via, c.amended_arguments) == (None, None)


async def test_after_a_change_that_changes_nothing_domo_may_still_apply_the_real_one(
    adult_chat, display_name_of
):  # the grant is kept: nothing was applied
    rid = await _rename_request(adult_chat)
    adult_chat.use_model(
        FakeChatModel(
            tool_script=["change", amend(rid, {}), amend(rid, {"name": "Alex C"}), "C'est fait."],
            judge="do",
        )
    )
    reply = await adult_chat.say("oui mais Alex C")
    assert [a["status"] for a in reply.actions] == ["invalid", "done"]
    assert await display_name_of(adult_chat.person) == "Alex C"


async def test_a_change_is_not_cut_by_a_time_limit_around_it(
    adult_chat, chat_server, monkeypatch, display_name_of
):  # Minor 2: each step has its own limit; one around the whole would cut a known outcome
    rid = await _rename_request(adult_chat)
    # Describing the changed call takes longer than a call's limit (it has the request's own,
    # far longer), and running it is quick: a limit of _TOOL_SECONDS around the whole change
    # would cut it while it describes, before anything runs (a shield protects only the run).
    slow = SlowTool(TOOLS["profile_rename"], describe_seconds=1.5, run_seconds=0.05)
    chat_server.app.dependency_overrides[get_tool_registry] = lambda: {
        **TOOLS,
        "profile_rename": slow,
    }
    monkeypatch.setattr(execution, "_TOOL_SECONDS", 1.0)
    adult_chat.use_model(
        FakeChatModel(
            tool_script=["change", amend(rid, {"name": "Alex C"}), "C'est fait."], judge="do"
        )
    )
    reply = await adult_chat.say("ok mais Alex C")
    assert json.loads(reply.model_result_text)["outcome"] == "executed"
    assert await display_name_of(adult_chat.person) == "Alex C"


async def test_a_request_whose_outcome_is_not_known_is_never_told_as_waiting(
    adult_chat, chat_server
):  # Minor 1: a request left executing (what came of it could not be stored)
    await _rename_request(adult_chat)
    adult_chat.use_model(FakeChatModel(["?"]))
    await adult_chat.say("oui")  # decided in words: the reply that answered shows what came of it
    async with chat_server.session_factory() as writer:
        await writer.execute(text("UPDATE confirmations SET status = 'executing'"))
        await writer.commit()
    model = adult_chat.use_model(FakeChatModel(["ok"]))
    await adult_chat.say("merci")
    assert model.calls[0].messages[1] == ChatMessage(
        "assistant", "[action profile.rename: running]\nok"
    )
    page = (await adult_chat.get("/chat")).text
    running = t("action.card.running", "en")
    (card,) = confirmation_cards(page)
    assert 'data-action-status="running"' in card and running in _plain(card)
    # The reply that answered the yes, too.
    assert re.search(
        r'<div class="action-card" data-action-status="running"[^>]*><p class="action-text">'
        + re.escape(html.escape(running)),
        page,
    )


class SlowToRun:
    """A tool of the registry that runs a call only after ``seconds`` (describing is quick)."""

    def __init__(self, tool, seconds: float) -> None:
        self._tool = tool
        self._seconds = seconds
        self.definition = tool.definition
        self.action_type = tool.action_type

    async def describe(self, arguments, actor, ctx):
        return await self._tool.describe(arguments, actor, ctx)

    async def execute(self, arguments, ctx):
        await asyncio.sleep(self._seconds)
        return await self._tool.execute(arguments, ctx)


async def test_a_request_whose_run_took_too_long_keeps_the_doubt_in_the_history(
    adult_chat, chat_server, monkeypatch
):  # R71: the yes ran it, but nobody knows what came of it
    await _rename_request(adult_chat)
    chat_server.app.dependency_overrides[get_tool_registry] = lambda: {
        **TOOLS,
        "profile_rename": SlowToRun(TOOLS["profile_rename"], seconds=10),
    }
    monkeypatch.setattr(execution, "_TOOL_SECONDS", 0.3)
    adult_chat.use_model(FakeChatModel(fail_after=0))  # the fixed sentence answers: its card's
    answered = await adult_chat.say("oui")
    assert answered.text == t("action.card.failed.timeout", "en")
    model = adult_chat.use_model(FakeChatModel(["ok"]))
    await adult_chat.say("alors ?")
    history = model.calls[0].messages
    # The reply that made the request, and the one that answered the yes.
    unknown = "[action profile.rename: failed (outcome unknown)]"
    assert history[1].content.startswith(unknown + "\n")
    assert history[3] == ChatMessage("assistant", f"{unknown}\n{answered.text}")


async def test_a_new_request_audits_what_the_barrier_said(
    adult_chat, chat_server
):  # Minor 3: the policy decision on the changed action, not the stored level
    rid = await _rename_request(adult_chat)
    adult_chat.use_model(
        FakeChatModel(
            tool_script=["change", amend(rid, {"name": "Alex C"}, ask_again=True), "D'accord ?"]
        )
    )
    await adult_chat.say("plutôt Alex C")
    async with chat_server.session_factory() as reader:
        new = (
            await reader.execute(select(Confirmation).where(Confirmation.status == "pending"))
        ).scalar_one()
        entries = await audit_entries(reader)
    # The new request asks what the old one asked; the audit says what the barrier said.
    assert (new.ask, new.approver_kind) == ("yes", "self")
    decisions = [
        e.details
        for e in entries
        if e.action == "policy.decision" and e.details["action_type"] == "profile.rename"
    ]
    assert decisions[-1]["decision"] == "allow"


async def test_a_card_never_shows_a_request_the_viewer_may_not_see(
    adult_chat, child_chat, chat_server
):  # Minor 6: whatever tool named it
    # An adult's own request: only that adult may see it (an adult may see a child's request,
    # which they approve, but no child sees an adult's).
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex Secret"})
    peek = PeekTool(reply.pending_confirmation_id)
    seen_by = {**TOOLS, "household_list": peek}
    chat_server.app.dependency_overrides[get_tool_registry] = lambda: seen_by
    seen = await child_chat.say_with_tool("household_list", {})
    assert "Alex Secret" not in seen.cards_html
    assert 'data-outcome="not_yours"' in seen.cards_html
    assert "data-confirmation-status" not in seen.cards_html
    page = (await child_chat.get("/chat")).text
    assert "Alex Secret" not in page and 'data-outcome="not_yours"' in page


@pytest.mark.parametrize("broken", ["waiting_action", "audit"])
async def test_a_new_request_is_stored_with_its_action_or_not_at_all(
    adult_chat, chat_server, monkeypatch, confirmation_status, broken
):  # Minor 7, as R69 for the first request
    rid = await _rename_request(adult_chat)
    if broken == "waiting_action":
        # The waiting action that shows the new request under the reply.
        class Broken(ConversationStore):
            async def add_action(self, *args, **kwargs):
                raise RuntimeError("the waiting action of the new request could not be stored")

        monkeypatch.setattr(confirmations_service, "ConversationStore", Broken)
    else:
        # The last write before the commit: the new request, its waiting action and the old
        # request made "replaced" are all written by then.
        audited = confirmations_service._audit_decided

        async def broken_at_the_last_step(*args, **kwargs):
            if kwargs.get("outcome") == "asked_again":
                raise RuntimeError("the audit of the new request could not be written")
            await audited(*args, **kwargs)

        monkeypatch.setattr(confirmations_service, "_audit_decided", broken_at_the_last_step)
    adult_chat.use_model(
        FakeChatModel(
            tool_script=["change", amend(rid, {"name": "Alex C"}, ask_again=True), "Hmm."]
        )
    )
    reply = await adult_chat.say("plutôt Alex C")
    assert json.loads(reply.model_result_text) == {"status": "failed"}
    assert reply.actions == [{"tool": "confirmation_amend", "status": "failed"}]
    # Nothing of it was stored: no new request, no waiting action for one, and the request
    # still waits as it was.
    assert await confirmation_status(rid) == "pending"
    async with chat_server.session_factory() as reader:
        (only,) = (await reader.execute(select(Confirmation))).scalars().all()
        waiting = await reader.execute(
            text("SELECT confirmation_id FROM message_actions WHERE status = 'waiting'")
        )
    assert str(only.id) == rid and (only.replaced_by, only.decided_via) == (None, None)
    assert [str(row.confirmation_id) for row in waiting] == [rid]


async def test_the_page_shows_the_new_request_once_under_the_reply_that_made_it(
    adult_chat,
):  # Minor 7: the new request has its own waiting action, stored with it
    rid = await _rename_request(adult_chat)
    adult_chat.use_model(
        FakeChatModel(
            tool_script=["change", amend(rid, {"name": "Alex C"}, ask_again=True), "D'accord ?"]
        )
    )
    reply = await adult_chat.say("plutôt Alex C")
    assert reply.actions == [
        {"tool": "profile_rename", "status": "waiting"},
        {"tool": "confirmation_amend", "status": "done"},
    ]
    page = (await adult_chat.get("/chat")).text
    assert page.count(f'data-confirmation-id="{reply.pending_confirmation_id}"') == 1


async def test_a_change_to_a_request_that_expired_meanwhile_runs_nothing(
    adult_chat, chat_server, display_name_of
):  # Minor 10: the lifetime ran out while Domo thought
    rid = await _rename_request(adult_chat)

    async def time_passes():
        adult_chat.clock.advance(timedelta(hours=168))

    adult_chat.use_model(ActsFirstModel(time_passes, amend(rid, {"name": "Alex C"})))
    reply = await adult_chat.say("oui mais Alex C")
    assert json.loads(reply.model_result_text) == {"status": "done", "outcome": "expired"}
    assert await display_name_of(adult_chat.person) == "Alex"
    async with chat_server.session_factory() as reader:
        c = (await reader.execute(select(Confirmation))).scalar_one()
    assert (c.status, c.expired_by) == ("expired", "lifetime")
    (card,) = confirmation_cards(reply.cards_html)
    assert 'data-confirmation-status="expired"' in card


async def test_a_button_that_wins_the_race_runs_the_original_and_the_change_runs_nothing(
    adult_chat, display_name_of, confirmation_status
):  # Minor 10: another tab pressed the button while Domo thought
    rid = await _rename_request(adult_chat)

    async def another_tab_confirms():
        # A tab that does not wait for the wake its button starts: that wake waits for this turn.
        server = adult_chat.server
        async with server.browser(adult_chat.member, waits_for_wakes=False) as tab:
            answer = await tab.post(f"/confirmations/{rid}/approve", headers={"hx-request": "true"})
        assert status_of(answer) == "executed"

    adult_chat.use_model(ActsFirstModel(another_tab_confirms, amend(rid, {"name": "Alex C"})))
    reply = await adult_chat.say("oui mais Alex C")
    assert json.loads(reply.model_result_text) == {"status": "done", "outcome": "already_decided"}
    assert await display_name_of(adult_chat.person) == "Alex B"
    assert await confirmation_status(rid) == "executed"


async def test_a_request_of_another_conversation_is_not_found(
    adult_chat, chat_server, clock, confirmation_status
):  # Minor 10: the person's own request, but not here
    rid = await _rename_request(adult_chat)
    async with chat_server.session_factory() as writer:
        elsewhere = Conversation(person_id=adult_chat.person.id)
        writer.add(elsewhere)
        await writer.commit()
    for tool, args in [
        ("confirmation_remind", {"request_id": rid}),
        ("confirmation_expire", {"request_id": rid}),
        ("confirmation_require_explicit", {"request_id": rid, "reason": "Odd."}),
    ]:
        reply = await adult_chat.say_with_tool(tool, args)
        assert reply.actions[0] == {"tool": tool, "status": "invalid", "reason": "not_found"}
    ctx = ToolContext(
        session_factory=chat_server.session_factory,
        actor=Actor(adult_chat.person.id, "adult"),
        person=adult_chat.person,
        conversation_id=elsewhere.id,
        origin="http://127.0.0.1/",
        gatekeeper=None,
        clock=clock,
        registry=TOOLS,
        policy=DEFAULT_POLICY,
        reply_message_id=None,
        change_grant=uuid.UUID(rid),
    )
    with pytest.raises(InvalidArguments) as refused:
        await amend_confirmation(
            session_factory=chat_server.session_factory,
            confirmation_id=uuid.UUID(rid),
            new_arguments={"name": "Alex C"},
            ask_again=False,
            ctx=ctx,
            now=clock(),
        )
    assert refused.value.code == "not_found"
    assert await confirmation_status(rid) == "pending"


async def test_domo_cannot_manage_a_request_already_decided(
    adult_chat, confirmation_status
):  # Minor 10
    rid = await _rename_request(adult_chat)
    assert status_of(await adult_chat.post(f"/confirmations/{rid}/approve")) == "executed"
    for tool, args in [
        ("confirmation_remind", {"request_id": rid}),
        ("confirmation_expire", {"request_id": rid}),
        ("confirmation_require_explicit", {"request_id": rid, "reason": "Odd."}),
    ]:
        reply = await adult_chat.say_with_tool(tool, args)
        assert reply.actions[0] == {"tool": tool, "status": "invalid", "reason": "not_found"}
    assert await confirmation_status(rid) == "executed"


# --- a change on a draft of rights, drafted again by the writer (spec 5.3) -----------------------

TWO_DAYS_WORDS = "Alex peut inviter des invités, 2 jours au plus"
TWO_DAYS_SUMMARY = "Alex peut inviter des invités, 2 jours au plus."
CHANGE = "3 jours au lieu de 2"
# What Domo is told when the writer asks a question instead of drafting the rights again.
WRITER_ASKS = (
    "nothing has changed: the request still waits as it was. Ask the owner the question; with "
    "their answer, propose the rights again (and drop this request)"
)


async def _rights_request(
    owner_chat, alex: uuid.UUID, session_factory, judge: str = "yes"
) -> tuple[str, RightsDraft]:
    """The owner has the writer draft "Alex may invite guests, 2 days at most" (``rights_propose``),
    then asks to apply it (``rights_apply``, the judge answering ``judge``): the request, waiting
    for their yes, and its draft."""
    await propose(owner_chat, TWO_DAYS_WORDS, alex_invites(alex, summary=TWO_DAYS_SUMMARY, days=2))
    [draft] = await drafts(session_factory)
    reply = await owner_chat.say_with_tool("rights_apply", {"draft": str(draft.id)}, judge=judge)
    assert reply.actions == [{"tool": "rights_apply", "status": "waiting"}]
    return str(reply.pending_confirmation_id), draft


async def _change_rights(
    owner_chat, calls: list[ToolCall], *writer_answers: WriterAnswer, judge: str = JUDGE_ASKS
) -> tuple[Reply, DomoAndWriter]:
    """The owner answers the request with "yes, but 3 days instead of 2" (the classifier reads a
    change); Domo makes ``calls``, the writer answers ``writer_answers``, and the yes-judge
    answers ``judge`` about the changed draft (it asks a yes of it, unless told otherwise)."""
    model = owner_chat.use_model(
        DomoAndWriter(calls, *writer_answers, verdict="change", judge=judge)
    )
    try:
        return await owner_chat.say(f"oui, mais {CHANGE}"), model
    finally:
        owner_chat.use_model(FakeChatModel(["ok"]))


async def test_a_change_on_a_draft_of_rights_the_judge_lets_go_is_applied_at_once(
    owner_chat, adult_member, confirmation_status, session_factory
):
    """A change of rights changed in words was always a new request (the owner's yes, by
    default). It is judged like any other change now (2026-11-15): the judge lets it go, so the
    words of the change are the owner's yes to the new draft."""
    alex = adult_member.person.id
    rid, first = await _rights_request(owner_chat, alex, session_factory)
    reply, model = await _change_rights(
        owner_chat, [amend(rid, {"change": CHANGE})], alex_invites(alex), judge="do"
    )
    [result] = model.results
    assert result["outcome"] == "executed", result
    [kept, second] = await drafts(session_factory)
    assert kept.id == first.id and second.owner_words == f"{TWO_DAYS_WORDS}\n\n{CHANGE}"
    assert await confirmation_status(rid) == "executed"
    assert reply.actions == [{"tool": "confirmation_amend", "status": "done"}]
    # The new draft is the one in force, not the one the request first carried.
    [right] = live(await rights_of(session_factory))
    assert (right.draft_id, right.summary) == (second.id, SUMMARY)
    assert right.limit_expression == THREE_DAYS


async def test_a_change_on_a_draft_of_rights_never_spares_the_proof_the_owner_asks(
    owner_chat, adult_member, grant, confirmation_status, session_factory
):
    """Review M5: the owner asked for a proof before his rights change. A change in words on
    such a request, which the judge lets go, still asks that proof: words never give one, and the
    judge never lowers what a rule of the owner asks."""
    alex = adult_member.person.id
    await grant(
        "rights.apply",
        person=owner_chat.person,
        ask="proof",
        summary="Always a proof before my rights change.",
    )
    rid, first_draft = await _rights_request(owner_chat, alex, session_factory, judge="do")
    async with session_factory() as reader:
        first = await reader.get(Confirmation, uuid.UUID(rid))
        assert first is not None and (first.ask, first.asked_by) == ("proof", "rule")
    reply, model = await _change_rights(
        owner_chat, [amend(rid, {"change": CHANGE})], alex_invites(alex), judge="do"
    )
    [result] = model.results
    assert result["outcome"] == "asked_again", result
    async with session_factory() as reader:
        asked = await reader.get(Confirmation, uuid.UUID(result["request_id"]))
        assert asked is not None
        assert (asked.status, asked.ask, asked.asked_by) == ("pending", "proof", "rule")
    # Nothing was applied: neither the first draft nor the new one is in force.
    [_, second] = await drafts(session_factory)
    in_force = {right.draft_id for right in live(await rights_of(session_factory))}
    assert second.id not in in_force and first_draft.id not in in_force


async def test_a_change_on_a_draft_of_rights_has_the_writer_draft_it_again_and_asks_again(
    owner_chat, adult_member, confirmation_status, session_factory
):
    alex = adult_member.person.id
    rid, first = await _rights_request(owner_chat, alex, session_factory)
    reply, model = await _change_rights(
        owner_chat, [amend(rid, {"change": CHANGE})], alex_invites(alex)
    )
    [result] = model.results
    assert result["outcome"] == "asked_again"
    # The yes the judge asks of the changed draft: in words or the button.
    assert result["answer_with"] == ANSWER_IN_WORDS
    new = result["request_id"]
    async with session_factory() as reader:
        asked = await reader.get(Confirmation, uuid.UUID(new))
        assert asked is not None and (asked.ask, asked.asked_by) == ("yes", "judge")
    # The writer read the owner's words, then their change.
    [call] = model.writer_calls
    words = f"{TWO_DAYS_WORDS}\n\n{CHANGE}"
    assert words in call.messages[0].content  # type: ignore[union-attr]
    [kept, second] = await drafts(session_factory)
    assert kept.id == first.id and kept.owner_words == TWO_DAYS_WORDS
    assert second.owner_words == words
    # The new draft is a new request that replaces the old one: the owner reads its rights and
    # its examples before saying yes.
    assert await confirmation_status(rid) == "replaced"
    assert await confirmation_status(new) == "pending"
    async with session_factory() as reader:
        replacing = await reader.get(Confirmation, uuid.UUID(new))
        assert replacing is not None and replacing.arguments == {"draft": str(second.id)}
    assert reply.pending_confirmation_id == uuid.UUID(new)
    [card] = confirmation_cards(reply.cards_html)
    assert SUMMARY in card and TWO_DAYS_SUMMARY not in card
    assert "Dana" in card and "Finn" in card
    assert await rights_of(session_factory) == []
    # One yes: "I confirm" applies the new draft.
    owner_chat.use_model(FakeChatModel(["unclear"]))
    answer = await owner_chat.say("je confirme")
    assert answer.actions == [{"tool": "rights_apply", "status": "done"}]
    assert await confirmation_status(new) == "executed"
    [right] = live(await rights_of(session_factory))
    assert (right.draft_id, right.summary) == (second.id, SUMMARY)
    assert right.limit_expression == THREE_DAYS and right.owner_words == words


@pytest.mark.parametrize(
    "judge,asked",
    [
        ("explicit_yes: many rights at once", ("explicit_yes", "judge", "many rights at once")),
        ("proof: not sure it is you", ("proof", "judge", "not sure it is you")),
    ],
)
async def test_a_change_of_rights_never_asks_less_than_the_request_did(
    owner_chat, adult_member, confirmation_status, confirmation_ask, session_factory, judge, asked
):  # review I1, pre-flight F2: the old ask stays a minimum
    alex = adult_member.person.id
    rid, _ = await _rights_request(owner_chat, alex, session_factory, judge=judge)
    assert await confirmation_ask(rid) == asked
    _, model = await _change_rights(
        owner_chat, [amend(rid, {"change": CHANGE})], alex_invites(alex)
    )
    [result] = model.results
    assert result["outcome"] == "asked_again"
    new = result["request_id"]
    # The new draft asks what the judge asked, with his reason: more than the owner's plain yes.
    assert await confirmation_ask(new) == asked
    # A plain yes applies nothing.
    owner_chat.use_model(FakeChatModel(["Il faut plus qu'un oui."]))
    await owner_chat.say("oui")
    assert await confirmation_status(new) == "pending"
    assert live(await rights_of(session_factory)) == []


async def test_a_change_of_rights_keeps_the_explicit_yes_domo_asked_for(
    owner_chat, adult_member, confirmation_status, confirmation_ask, session_factory
):  # review I1: Domo's raise counts as the judge's does
    alex = adult_member.person.id
    rid, _ = await _rights_request(owner_chat, alex, session_factory)
    await owner_chat.say_with_tool(
        "confirmation_require_explicit", {"request_id": rid, "reason": "A big change."}
    )
    assert await confirmation_ask(rid) == ("explicit_yes", "domo", "A big change.")
    _, model = await _change_rights(
        owner_chat, [amend(rid, {"change": CHANGE})], alex_invites(alex)
    )
    [result] = model.results
    assert (result["outcome"], result["answer_with"]) == ("asked_again", ANSWER_EXPLICITLY)
    new = result["request_id"]
    assert await confirmation_ask(new) == ("explicit_yes", "domo", "A big change.")
    owner_chat.use_model(FakeChatModel(["Dis « je confirme »."]))
    await owner_chat.say("oui")
    assert await confirmation_status(new) == "pending"
    assert live(await rights_of(session_factory)) == []


async def test_a_question_of_the_writer_leaves_the_request_waiting(
    owner_chat, adult_member, confirmation_status, session_factory, audit_actions
):
    alex = adult_member.person.id
    rid, first = await _rights_request(owner_chat, alex, session_factory)
    # The writer asks; Domo changes the request again in the same turn, and the writer asks
    # again: the change stays allowed for the turn.
    reply, model = await _change_rights(
        owner_chat,
        [amend(rid, {"change": CHANGE}), amend(rid, {"change": "3 jours, pour tout le monde"})],
        {"question": "Pour les invités seulement ?"},
        {"question": "Et pour les adultes ?"},
    )
    assert reply.actions == [{"tool": "confirmation_amend", "status": "done"}] * 2
    assert model.results == [
        {
            "status": "done",
            "outcome": "writer_asks",
            "question": "Pour les invités seulement ?",
            "detail": WRITER_ASKS,
        },
        {
            "status": "done",
            "outcome": "writer_asks",
            "question": "Et pour les adultes ?",
            "detail": WRITER_ASKS,
        },
    ]
    # The request waits as it was, and no draft was written.
    assert await confirmation_status(rid) == "pending"
    assert [d.id for d in await drafts(session_factory)] == [first.id]
    assert "confirmation.decided" not in await audit_actions()
    # The reply says Domo needs to know more; it does not present the request again.
    assert t("action.card.done.rights.propose.question", "en") in _plain(reply.cards_html)
    assert reply.pending_confirmation_id is None


async def test_the_writer_runs_without_the_requests_lock(
    owner_chat, adult_member, confirmation_status, session_factory
):  # F18: a writer may take minutes; a button on the same request is not held up meanwhile
    alex = adult_member.person.id
    rid, first = await _rights_request(owner_chat, alex, session_factory)
    pressed: list[str] = []

    async def the_button_answers_meanwhile() -> None:
        # Another tab presses the button of the request while the writer writes (a tab that does
        # not wait for the wake its button starts: that wake waits for this turn).
        async with owner_chat.server.browser(owner_chat.member, waits_for_wakes=False) as tab:
            async with asyncio.timeout(10):
                answer = await tab.post(
                    f"/confirmations/{rid}/approve", headers={"hx-request": "true"}
                )
        pressed.append(status_of(answer))

    _, model = await _change_rights(
        owner_chat,
        [amend(rid, {"change": CHANGE})],
        the_button_answers_meanwhile,
        alex_invites(alex),
    )
    assert pressed == ["executed"]
    assert model.results == [{"status": "done", "outcome": "already_decided"}]
    assert await confirmation_status(rid) == "executed"
    # The draft the button applied is the one in force; the one written meanwhile is never
    # applied.
    [right] = live(await rights_of(session_factory))
    assert right.draft_id == first.id
    await owner_chat.server.wait_for_wakes()


async def test_the_time_of_a_change_of_rights_is_read_after_the_writer(
    owner_chat, adult_member, confirmation_status, session_factory
):  # final review M11: the writer may take minutes
    alex = adult_member.person.id
    rid, _ = await _rights_request(owner_chat, alex, session_factory)

    async def minutes_pass() -> None:
        owner_chat.clock.advance(timedelta(minutes=4))

    _, model = await _change_rights(
        owner_chat, [amend(rid, {"change": CHANGE})], minutes_pass, alex_invites(alex)
    )
    [result] = model.results
    assert result["outcome"] == "asked_again"
    async with session_factory() as reader:
        new = await reader.get(Confirmation, uuid.UUID(result["request_id"]))
        assert new is not None and new.created_at == owner_chat.clock()


async def test_a_request_of_rights_whose_lifetime_ends_while_the_writer_writes_expires(
    owner_chat, adult_member, confirmation_status, session_factory
):  # final review M11
    alex = adult_member.person.id
    rid, _ = await _rights_request(owner_chat, alex, session_factory)

    async def a_week_passes() -> None:
        owner_chat.clock.advance(timedelta(hours=169))

    _, model = await _change_rights(
        owner_chat, [amend(rid, {"change": CHANGE})], a_week_passes, alex_invites(alex)
    )
    assert model.results == [{"status": "done", "outcome": "expired"}]
    assert await confirmation_status(rid) == "expired"
    assert await rights_of(session_factory) == []


@pytest.mark.parametrize(
    "change",
    [
        {"draft": str(uuid.UUID(int=1))},
        {"change": 3},
        {"change": CHANGE, "draft": str(uuid.UUID(int=1))},
        {},
    ],
)
async def test_a_draft_is_changed_in_words_only(
    owner_chat, adult_member, confirmation_status, session_factory, change
):
    rid, first = await _rights_request(owner_chat, adult_member.person.id, session_factory)
    reply, model = await _change_rights(owner_chat, [amend(rid, change)])
    assert reply.actions == [
        {"tool": "confirmation_amend", "status": "invalid", "reason": "invalid"}
    ]
    assert model.results[0]["detail"] == (
        "a change of rights is changed in words: give change, what the owner wants different"
    )
    assert model.writer_calls == []
    assert await confirmation_status(rid) == "pending"
    assert [d.id for d in await drafts(session_factory)] == [first.id]


# --- Domo asks more than the judge, never less ----------------------------------------------


def ask_before(ask, reason="a lot at once"):
    """Domo's raise for the next judged action of the answer."""
    return ToolCall("a1", "ask_before_acting", json.dumps({"ask": ask, "reason": reason}))


def invite(name="Robin"):
    """A call that makes an invitation, which the judge is asked about."""
    return ToolCall(
        f"i-{name}", "household_invite", json.dumps({"display_name": name, "role": "guest"})
    )


async def test_domo_may_ask_more_on_the_next_action_never_less(owner_chat, confirmation_ask):
    owner_chat.use_model(
        FakeChatModel(tool_script=[ask_before("explicit_yes"), invite(), "ok"], judge="do")
    )
    reply = await owner_chat.say("invite Robin")
    assert reply.actions[0] == {"tool": "ask_before_acting", "status": "done"}
    assert await confirmation_ask(reply.pending_confirmation_id) == (
        "explicit_yes",
        "domo",
        "a lot at once",
    )
    waiting = reply.model_results[-1]
    assert (waiting["asked_by"], waiting["why"]) == ("domo", "a lot at once")


async def test_domo_never_lowers_what_the_judge_asks(owner_chat, confirmation_ask):
    owner_chat.use_model(
        FakeChatModel(tool_script=[ask_before("yes"), invite("Dana"), "ok"], judge="proof: never")
    )
    reply = await owner_chat.say("invite Dana")
    assert await confirmation_ask(reply.pending_confirmation_id) == ("proof", "judge", "never")


async def test_the_raise_goes_to_the_next_judged_action_only(
    owner_chat, confirmation_ask, count_invitations
):
    owner_chat.use_model(
        FakeChatModel(
            tool_script=[
                ask_before("yes"),
                ToolCall("l1", "household_list", "{}"),
                invite("Robin"),
                invite("Dana"),
                "ok",
            ],
            judge="do",
        )
    )
    reply = await owner_chat.say("invite both")
    assert [a["status"] for a in reply.actions] == ["done", "done", "waiting", "done"]
    assert await confirmation_ask(reply.pending_confirmation_id) == (
        "yes",
        "domo",
        "a lot at once",
    )
    assert await count_invitations() == 1


async def test_a_raise_without_an_action_is_forgotten_with_the_turn(
    owner_chat, confirmation_ask, count_invitations
):  # Review Focus 5
    owner_chat.use_model(FakeChatModel(tool_script=[ask_before("proof"), "ok"], judge="do"))
    await owner_chat.say("just checking")
    reply = await owner_chat.say_with_tool(
        "household_invite", {"display_name": "Robin", "role": "guest"}, judge="do"
    )
    assert reply.actions == [{"tool": "household_invite", "status": "done"}]
    assert reply.pending_confirmation_id is None
    assert await count_invitations() == 1


async def test_a_raise_never_overrides_a_do_not(owner_chat, stored_notes):
    owner_chat.use_model(
        FakeChatModel(
            tool_script=[
                ask_before("yes"),
                ToolCall("n1", "note_leave", json.dumps({"recipient": "Owner", "text": "rude"})),
                "ok",
            ],
            judge="do_not: rude",
        )
    )
    reply = await owner_chat.say("leave it")
    assert reply.actions[-1] == {"tool": "note_leave", "status": "refused", "reason": "judge"}
    assert await stored_notes() == []


async def test_a_raise_is_used_up_by_a_refused_action(
    owner_chat, adult_member, count_invitations, stored_notes
):  # F15
    owner_chat.use_model(
        FakeChatModel(
            tool_script=[
                ask_before("yes"),
                ToolCall("n1", "note_leave", json.dumps({"recipient": "Alex", "text": "rude"})),
                invite("Robin"),
                "ok",
            ],
            judge=["do_not: rude", "do"],
        )
    )
    reply = await owner_chat.say("both")
    assert [a["status"] for a in reply.actions] == ["done", "refused", "done"]
    assert reply.pending_confirmation_id is None
    assert await count_invitations() == 1


@pytest.mark.parametrize("name", ["confirmation_require_explicit", "ask_before_acting"])
def test_asking_more_than_the_judge_needs_a_reason_of_his_own(name: str):
    # F5 (the owner's trial of 0.9.0): she said she knew it could not be undone, and Domo
    # asked a firm yes all the same, because it could not be undone.
    described = " ".join(TOOLS[name].definition.description.split())
    assert "for a reason of your own that your judge cannot see" in described
    assert "it does not sound like them, something contradicts what you know" in described
    assert "That an action is serious or cannot be undone is no such reason" in described
    assert "someone who says they know is not asked again" in described


# --- what cannot be undone: asked a first time, then as agreed (Task 26) -------------------------

AGREE = "asking_agreed"
AGREED_AT = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)


SAID = "Oui, vas-y. Plus besoin de me le redemander pour ça."
NO_NEED = "Plus besoin de me le redemander"


def agreeing(
    person, session, *, answers_them: bool = True, said: tuple[str, ...] = (SAID,)
) -> ToolContext:
    """The context of ``person``'s turn, on ``session`` (its commits only release a savepoint);
    ``answers_them``: the turn answers a message of theirs, which ``said`` holds (their messages
    of the turn, one by one)."""
    return ToolContext(
        session_factory=lambda: nullcontext(session),
        actor=actor_of(person),
        person=person,
        conversation_id=uuid.uuid4(),
        origin="",
        gatekeeper=None,  # type: ignore[arg-type]
        clock=lambda: AGREED_AT,
        registry=TOOLS,
        policy=DEFAULT_POLICY,
        reply_message_id=uuid.uuid4() if answers_them else None,
        said=said[0] if said else None,
        said_later=tuple(said[1:]),
    )


async def told_of(session, person_id: uuid.UUID) -> dict[str, bool | None]:
    rows = await session.scalars(
        select(Memory).where(Memory.said_by_id == person_id, Memory.origin == "told_by_domo")
    )
    return {memory.action_type: memory.told_keep_asking for memory in rows}


async def warned(session, person, *action_types: str) -> None:
    for action_type in action_types:
        await told(
            session,
            person_id=person.id,
            action_type=action_type,
            reason="you cannot take it back",
            answer="yes",
            now=AGREED_AT,
        )


async def test_no_need_to_ask_again_is_kept_for_that_person_and_kind(session, add_person):
    alex = await add_person("Alex", "adult")
    await warned(session, alex, "household.remove", "profile.rename")
    tool = TOOLS[AGREE]
    arguments = {"action_type": "household.remove", "keep_asking": False, "their_words": NO_NEED}
    ctx = agreeing(alex, session)
    # It changes nothing in the household, and is never shown.
    assert await tool.describe(arguments, ctx.actor, ctx) == DescribedAction(
        "core", "confirmation.agree_asking", None, "own_requests", "household"
    )
    assert tool.action_type in KNOWN_ACTIONS
    assert tool.action_type in CHANGES_NOTHING and is_silent(tool.action_type)
    outcome = await tool.execute(arguments, ctx)
    assert outcome.status == "done"
    assert await told_of(session, alex.id) == {"household.remove": False, "profile.rename": None}


async def test_another_person_s_agreement_is_not_theirs(session, add_person):
    alex = await add_person("Alex", "adult")
    dana = await add_person("Dana", "adult")
    await warned(session, alex, "household.remove")
    await warned(session, dana, "household.remove")
    arguments = {"action_type": "household.remove", "keep_asking": False, "their_words": NO_NEED}
    await TOOLS[AGREE].execute(arguments, agreeing(dana, session))
    assert await told_of(session, dana.id) == {"household.remove": False}
    assert await told_of(session, alex.id) == {"household.remove": None}


async def test_agreed_asking_without_a_warning_before_is_not_found(session, add_person):
    alex = await add_person("Alex", "adult")
    dana = await add_person("Dana", "adult")
    await warned(session, dana, "household.remove")
    arguments = {"action_type": "household.remove", "keep_asking": False, "their_words": NO_NEED}
    with pytest.raises(InvalidArguments) as refused:
        await TOOLS[AGREE].execute(arguments, agreeing(alex, session))
    assert refused.value.code == "not_found"
    # Tied to a passage of a message of theirs of this turn (ruling R68): never on his own, nor
    # in a turn nobody wrote in, which hold none.
    await warned(session, alex, "household.remove")
    nobody = agreeing(alex, session, answers_them=False, said=())
    for ctx in (nobody, replace(nobody, on_his_own=True)):
        with pytest.raises(InvalidArguments) as refused:
            await TOOLS[AGREE].execute(arguments, ctx)
        assert refused.value.code == "not_possible"
    assert await told_of(session, alex.id) == {"household.remove": None}
    # Words that are not theirs told to the turn they wrote in close nothing: their passage is
    # theirs.
    await TOOLS[AGREE].execute(arguments, replace(agreeing(alex, session), told_outside=True))
    assert await told_of(session, alex.id) == {"household.remove": False}


@pytest.mark.parametrize(
    "said",
    [
        # Not what they said.
        ("Oui, vas-y.",),
        # Across two of their messages: never (ruling R62).
        ("Oui, vas-y. Plus besoin", "de me le redemander"),
    ],
)
async def test_agreed_asking_needs_a_passage_of_their_message_of_now(session, add_person, said):
    alex = await add_person("Alex", "adult")
    await warned(session, alex, "household.remove")
    arguments = {
        "action_type": "household.remove",
        "keep_asking": False,
        "their_words": "Plus besoin de me le redemander",
    }
    with pytest.raises(InvalidArguments) as refused:
        await TOOLS[AGREE].execute(arguments, agreeing(alex, session, said=said))
    assert refused.value.code == "not_possible"
    assert await told_of(session, alex.id) == {"household.remove": None}
    # In one message of theirs, word for word: kept.
    await TOOLS[AGREE].execute(
        arguments, agreeing(alex, session, said=("ok", "Plus besoin de me le redemander !"))
    )
    assert await told_of(session, alex.id) == {"household.remove": False}


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"action_type": "household.remove", "keep_asking": True},
        {"action_type": "", "keep_asking": True, "their_words": "x"},
        {"action_type": 3, "keep_asking": True, "their_words": "x"},
        {"action_type": "household.remove", "keep_asking": "no", "their_words": "x"},
        {"action_type": "household.remove", "keep_asking": True, "more": 1, "their_words": "x"},
        {"action_type": "household.remove", "keep_asking": True, "their_words": " "},
        {"action_type": "household.remove", "keep_asking": True, "their_words": 1},
    ],
)
async def test_agreed_asking_refuses_malformed_arguments(arguments):
    with pytest.raises(InvalidArguments) as refused:
        await TOOLS[AGREE].describe(arguments, Actor(uuid.uuid4(), "adult"), None)
    assert refused.value.code == "invalid"
