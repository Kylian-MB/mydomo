"""Domo acts: tool calls in the chat, through the policy barrier, and what they leave behind.

The pilots of ``chat_support`` talk with Domo on a live server, as the people of one household
(see ``conftest.py``): the model is a scripted one, the rest is the real application.
"""

import asyncio
import html
import json
import logging
import re
import uuid
from zoneinfo import ZoneInfo

import pytest
from chat_support import (
    ActsBeforeItsCall,
    Card,
    ToolCallingModel,
    assistant_id_in,
    consume_sse,
    post_message,
    turn_tools,
    wait_for_status,
)
from domo_common.i18n import t
from domo_core.accounts.models import Person
from domo_core.audit.log import verify_audit_chain
from domo_core.channels.page import render as page_render
from domo_core.channels.page.abilities import PAGE
from domo_core.chat.models import MessageAction
from domo_core.confirmations.store import (
    ANSWER_BY_AN_ADULT,
    ANSWER_EXPLICITLY,
    ANSWER_IN_WORDS,
)
from domo_core.conversation import execution
from domo_core.llm.types import (
    ChatChunk,
    ChatMessage,
    ChatUsage,
    ModelCallError,
    ToolCall,
    ToolDefinition,
)
from domo_core.policy.actions import DescribedAction
from domo_core.policy.catalog import DEFAULT_POLICY, PolicyTables
from domo_core.prompts import (
    HouseholdLine,
    KeyShown,
    KeysNote,
    MemoryBlock,
    ModelsNote,
    build_system_prompt,
    today_of,
)
from domo_core.tools.context import ToolOutcome
from domo_core.tools.registry import TOOLS, InvalidArguments
from domo_core.trust.service import Trust
from domo_core.web.deps import get_policy, get_tool_registry
from fakes import JUDGE_DEFAULT, FakeChatModel
from sqlalchemy import select, text
from web_support import audit_entries, in_language, reads_in

# The key and the model the pilots talk on, as Domo is told them: the medium model of the
# general key, whose price the catalog does not know.
PILOTS_MODEL = ModelsNote(
    key_label="openai_compatible key of Owner, general",
    model_id="example-sonnet-1",
    level="medium",
    price_known=False,
)


def pilots_keys(chat) -> KeysNote:
    """The keys a pilot is told of: the pilots' general key, which the owner gave."""
    return KeysNote(
        listed=(
            KeyShown(
                "openai_compatible key of Owner, general",
                chat.server.provider_id,
                yours=chat.person.role == "owner",
            ),
        )
    )


async def test_free_tool_runs_and_its_result_goes_back_to_the_model(owner_chat):
    model = owner_chat.use_model(
        FakeChatModel(tool_script=[ToolCall("c1", "household_list", "{}"), "Voilà."])
    )
    reply = await owner_chat.say("qui est là ?")
    assert reply.text == "Voilà." and reply.actions[0]["status"] == "done"
    assert '"status": "done"' in model.calls[1].messages[-1].content


async def test_the_model_only_sees_tools_allowed_for_the_role(guest_chat):
    model = guest_chat.use_model(FakeChatModel(["ok"]))
    await guest_chat.say("salut")
    # A guest may rename themselves, leave early (spec 5.9), hear the notes left for them (spec
    # 4.8), bring a key of their own (add it, give it again, remove it, limit it), ask what they
    # cost, have Domo take a stronger model for the rest of a request, and change their own
    # texts, writing note and language (spec 4.7). They see and take back their own devices
    # (spec 6.5); entrusting one needs the owner's right.
    assert [t.name for t in model.calls[0].tools] == [
        "profile_rename",
        "household_leave",
        "note_told",
        "note_show",
        "model_rekey",
        "key_add",
        "key_remove",
        "key_limit",
        "spending_show",
        "models_step_up",
        "texts_find",
        "texts_rewrite",
        "texts_restore",
        "texts_language",
        "texts_note",
        "device_list",
        "device_remove",
        # Domo's memory is in everyone's turn (0.9.0, spec 6.2).
        "memory_keep",
        "memory_change",
        "memory_forget",
        "memory_search",
        # And his plans (0.9.0, spec 6.9).
        "plan_set",
        "plan_change",
        "plan_cancel",
        # And the name they call him by (0.9.0, spec 6.7).
        "domo_name",
        # And the names they use for the others (0.9.0, spec 6.7).
        "nickname_keep",
        "nickname_forget",
        # And his look at what reached someone of his replies (0.9.0, spec 6.10).
        "delivery_look",
        # And what he notes of a person's word (0.9.1, trust per person).
        "trust_note",
        "trust_look",
        "trust_correct",
        # And the tools of his own that he makes, looks at and times (0.10.0, spec 5.5).
        "tool_build",
        "tool_change",
        "tool_remove",
        "tool_look",
        "tool_find",
        "tool_launch_when",
        # And the bounds of his tools, his settings (2026-11-21, spec 12.1).
        "tools_settings",
        "tool_set",
        # And keeping them up on his own (2026-11-21, spec 12.3).
        "tool_back",
        "tool_check",
        # And what he runs by hand at once (2026-11-18, spec 5.4).
        "program_run",
        # And the agreements that last, for themselves (2026-11-18).
        "agreement_keep",
        "agreement_drop",
        # And the addresses his programs reach, and the accounts they have (step 2, spec 13).
        "address_open",
        "address_close",
        "addresses_look",
        "accounts_look",
    ]


async def test_a_refused_tool_is_explained_not_executed(guest_chat):
    guest_chat.use_model(
        FakeChatModel(tool_script=[ToolCall("c1", "household_list", "{}"), "Je ne peux pas."])
    )
    reply = await guest_chat.say("qui habite ici ?")
    assert reply.actions[0] == {
        "tool": "household_list",
        "status": "refused",
        "reason": "not_allowed",
    }


async def test_unknown_tool_and_bad_json_are_refused_cleanly(owner_chat):  # Review Focus 3
    owner_chat.use_model(
        FakeChatModel(
            tool_script=[
                ToolCall("c1", "rm_rf", "{}"),
                ToolCall("c2", "profile_rename", "{oops"),
                "ok",
            ]
        )
    )
    reply = await owner_chat.say("x")
    assert [a["status"] for a in reply.actions] == ["invalid", "invalid"]
    assert reply.status == "complete"


async def test_tool_rounds_are_capped(owner_chat):
    model = owner_chat.use_model(
        FakeChatModel(
            tool_script=[ToolCall(f"c{i}", "household_list", "{}") for i in range(10)] + ["fin"]
        )
    )
    reply = await owner_chat.say("boucle")
    # R60: the last round still declares the tools, and may not call them.
    assert len(reply.actions) == 4 and model.calls[-1].tool_choice == "none"
    assert model.calls[-1].tools == model.calls[0].tools and reply.text == "fin"


async def test_a_model_without_tool_support_still_chats(owner_chat):
    owner_chat.use_model(FakeChatModel(["salut"], refuse_tools=True))
    assert (await owner_chat.say("hello")).text == "salut"


async def test_actions_are_summarised_in_the_next_turns_history(owner_chat):
    model = owner_chat.use_model(
        FakeChatModel(tool_script=[ToolCall("c1", "household_list", "{}"), "Voilà.", "Encore ?"])
    )
    await owner_chat.say("qui ?")
    await owner_chat.say("et alors")
    assistant = [m for m in model.calls[-1].messages if getattr(m, "role", "") == "assistant"][-1]
    assert "[action household.list: done]" in assistant.content


# --- helpers of this file ------------------------------------------------------------------------


def call(name: str, arguments: object = None, call_id: str = "c1") -> ToolCall:
    return ToolCall(call_id, name, json.dumps({} if arguments is None else arguments))


def one_call_then(
    tool_call: ToolCall, text: str = "ok", judge: str = JUDGE_DEFAULT
) -> FakeChatModel:
    """A model that makes ``tool_call``, then says ``text``; the yes-judge answers ``judge``."""
    return FakeChatModel(tool_script=[tool_call, text], judge=judge)


async def display_name_of(session_factory, person: Person) -> str:
    async with session_factory() as reader:
        result = await reader.execute(select(Person.display_name).where(Person.id == person.id))
        return result.scalar_one()


async def stored_actions(session_factory) -> list[tuple[str, str, str, dict]]:
    async with session_factory() as reader:
        result = await reader.execute(
            select(
                MessageAction.tool_name,
                MessageAction.action_type,
                MessageAction.status,
                MessageAction.summary,
            ).order_by(MessageAction.created_at)
        )
        return [tuple(row) for row in result.all()]


async def decisions(session_factory) -> list[dict]:
    async with session_factory() as reader:
        return [e.details for e in await audit_entries(reader) if e.action == "policy.decision"]


def cards_in(page: str) -> list[tuple[str, str]]:
    """The cards of a page or of a stream: their status and their text, as a person reads it."""
    return [
        (status, html.unescape(re.sub(r"<[^>]+>", "", body)))
        for status, body in re.findall(
            r'<div class="action-card" data-action-status="([a-z]+)"[^>]*>(.*?)</div>', page
        )
    ]


class SpyTool:
    """A tool of the tests (it reads the household, a free action of every member): it keeps
    the context of each call, and its ``execute`` does what the test says."""

    definition = ToolDefinition(
        "spy", "A tool of the tests.", {"type": "object", "additionalProperties": False}
    )
    action_type = "household.list"

    def __init__(self, execute=None) -> None:
        self.contexts = []
        self._execute = execute

    async def describe(self, arguments, actor, ctx):
        self.contexts.append(ctx)
        return DescribedAction("core", self.action_type, None, "read", "household")

    async def execute(self, arguments, ctx):
        if self._execute is not None:
            return await self._execute(arguments, ctx)
        return ToolOutcome("done", {"seen": True}, {"key": "action.card.done", "params": {}})


@pytest.fixture
def use_tool(chat_server):
    """``use_tool(tool)``: the server's registry has ``tool`` too, under its name."""

    def _use_tool(tool):
        registry = {**TOOLS, tool.definition.name: tool}
        chat_server.app.dependency_overrides[get_tool_registry] = lambda: registry
        return tool

    return _use_tool


# --- what the tools do ---------------------------------------------------------------------------


async def test_renaming_changes_the_name_of_the_person_who_speaks(adult_chat, owner_member):
    adult_chat.use_model(
        one_call_then(call("profile_rename", {"name": "Alex B"}), "C'est fait.", judge="do")
    )
    reply = await adult_chat.say("appelle-moi Alex B")
    assert reply.actions == [{"tool": "profile_rename", "status": "done"}]
    factory = adult_chat.server.session_factory
    assert await display_name_of(factory, adult_chat.person) == "Alex B"
    assert await display_name_of(factory, owner_member.person) == "Owner"
    assert json.loads(reply.model_result_text) == {"status": "done", "name": "Alex B"}
    # The next turn speaks to the new name.
    model = adult_chat.use_model(FakeChatModel(["ok"]))
    await adult_chat.say("merci")
    assert model.calls[0].system == build_system_prompt(
        abilities=PAGE,
        display_name="Alex B",
        role="adult",
        can_act=True,
        **turn_tools("adult"),
        today=today_of(adult_chat.clock(), None),
        household=(
            HouseholdLine("Owner", "owner"),
            HouseholdLine("Alex B", "adult", is_you=True),
        ),
        # No model of meanings in the pilots: the search works by words alone.
        memory=MemoryBlock(search="words_only"),
        models=PILOTS_MODEL,
        keys=pilots_keys(adult_chat),
        reads="en",
        # Nobody's word has been weighed yet: the household's start (trust per person).
        trust=Trust(percent=70, facts=0),
    )


async def test_the_household_as_the_model_reads_it(owner_chat, adult_member, guest_member):
    owner_chat.use_model(one_call_then(call("household_list")))
    reply = await owner_chat.say("qui est là ?")
    result = json.loads(reply.model_result_text)
    assert result["status"] == "done"
    assert [(p["name"], p["role"]) for p in result["people"]] == [
        ("Owner", "owner"),
        ("Alex", "adult"),
        ("Robin", "guest"),
    ]
    assert result["people"][2]["status"].startswith("guest_until:")


async def test_a_bad_name_changes_nothing(adult_chat):
    adult_chat.use_model(one_call_then(call("profile_rename", {"name": "x" * 61})))
    reply = await adult_chat.say("renomme-moi")
    assert reply.actions == [{"tool": "profile_rename", "status": "invalid", "reason": "too_long"}]
    assert json.loads(reply.model_result_text)["reason"] == "too_long"
    assert await display_name_of(adult_chat.server.session_factory, adult_chat.person) == "Alex"


# --- what the model is told ----------------------------------------------------------------------


async def test_unknown_tools_and_bad_arguments_are_explained_to_the_model(owner_chat):
    owner_chat.use_model(
        FakeChatModel(
            tool_script=[
                ToolCall("c1", "rm_rf", "{}"),
                ToolCall("c2", "profile_rename", "{oops"),
                ToolCall("c3", "profile_rename", "[1, 2]"),
                # Deep enough to stop the JSON parser (RecursionError), short enough to be read.
                ToolCall("c4", "household_list", "[" * 5000),
                "ok",
            ]
        )
    )
    reply = await owner_chat.say("x")
    results = [json.loads(line) for line in reply.model_result_text.splitlines()]
    assert [(r["status"], r["reason"]) for r in results] == [
        ("invalid", "unknown_tool"),
        ("invalid", "invalid"),
        ("invalid", "invalid"),
        ("invalid", "invalid"),
    ]
    assert reply.status == "complete" and reply.text == "ok"


async def test_a_tool_name_the_model_made_up_is_stored_short_and_shown_escaped(owner_chat):
    name = "<img src=x onerror=alert(1)>" + "x" * 200
    owner_chat.use_model(one_call_then(ToolCall("c1", name, "{}")))
    reply = await owner_chat.say("x")
    (stored,) = await stored_actions(owner_chat.server.session_factory)
    assert stored[0] == name[:64] and stored[1:3] == ("", "invalid")
    assert "<img" not in reply.cards_html and "&lt;img" in reply.cards_html
    assert "<img" not in (await owner_chat.get("/chat")).text


async def test_a_waiting_action_says_it_has_not_happened(adult_chat):
    adult_chat.use_model(one_call_then(call("profile_rename", {"name": "Alex B"})))
    reply = await adult_chat.say("appelle-moi Alex B")
    assert reply.actions == [{"tool": "profile_rename", "status": "waiting"}]
    # R64: the result says plainly that nothing was done, who must say yes, how (the judge asked
    # a yes, which the person's own words give) and who asked it.
    assert json.loads(reply.model_result_text) == {
        "status": "waiting_for_confirmation",
        "by": "you",
        "answer_with": ANSWER_IN_WORDS,
        "asked_by": "judge",
    }
    assert await display_name_of(adult_chat.server.session_factory, adult_chat.person) == "Alex"
    assert cards_in(reply.cards_html) == [("waiting", t("action.card.waiting.self", "en"))]


async def test_a_waiting_action_says_when_it_needs_the_button_or_i_confirm(adult_chat):
    # The judge asks an explicit yes, and says why.
    adult_chat.use_model(
        FakeChatModel(
            tool_script=[call("profile_rename", {"name": "Alex B"}), "ok"],
            judge="explicit_yes: a name unlike the others",
        )
    )
    reply = await adult_chat.say("appelle-moi Alex B")
    assert json.loads(reply.model_result_text) == {
        "status": "waiting_for_confirmation",
        "by": "you",
        "answer_with": ANSWER_EXPLICITLY,
        "asked_by": "judge",
        "why": "a name unlike the others",
    }


async def test_a_childs_action_waits_for_an_adult(child_chat, an_adult_agrees_to_a_childs_rename):
    child_chat.use_model(one_call_then(call("profile_rename", {"name": "Sam B"})))
    reply = await child_chat.say("appelle-moi Sam B")
    # The child cannot say yes: an adult does, with the button on their own page.
    assert json.loads(reply.model_result_text) == {
        "status": "waiting_for_confirmation",
        "by": "an adult",
        "answer_with": ANSWER_BY_AN_ADULT,
        "asked_by": "judge",
    }
    assert cards_in(reply.cards_html) == [("waiting", t("action.card.waiting.adult", "en"))]


# --- the person, read again before each call (final review Minor 1) -------------------------------


async def test_someone_removed_while_their_reply_streams_can_do_nothing_more(
    adult_chat, chat_server, caplog
):
    async def removed() -> None:
        async with chat_server.session_factory() as writer:
            await writer.execute(
                text("UPDATE persons SET disabled_at = now() WHERE id = :id"),
                {"id": adult_chat.person.id},
            )
            await writer.commit()

    adult_chat.use_model(ActsBeforeItsCall(removed, call("household_list")))
    with caplog.at_level(logging.WARNING, logger="domo_core.conversation.execution"):
        reply = await adult_chat.say("qui est là ?")
    # A free action, which the adult could do when the turn began: refused, and nothing read.
    assert reply.actions == [
        {"tool": "household_list", "status": "refused", "reason": "not_allowed"}
    ]
    # The reason, with words Domo can say in its stead.
    assert json.loads(reply.model_result_text) == {
        "status": "refused",
        "reason": "not_allowed",
        "detail": execution.NOT_ALLOWED_DETAIL,
    }
    assert "Alex" not in reply.model_result_text
    assert "no longer in the household" in caplog.text


@pytest.mark.parametrize(
    "role,actions,result",
    [
        # A guest may not rename themselves under the owner's right: refused.
        (
            "guest",
            [{"tool": "profile_rename", "status": "refused", "reason": "not_allowed"}],
            {
                "status": "refused",
                "reason": "not_allowed",
                "detail": execution.NOT_ALLOWED_DETAIL,
            },
        ),
        # A child's request waits for an adult's yes.
        (
            "child",
            [{"tool": "profile_rename", "status": "waiting"}],
            {
                "status": "waiting_for_confirmation",
                "by": "an adult",
                "answer_with": ANSWER_BY_AN_ADULT,
                "asked_by": "judge",
            },
        ),
    ],
)
async def test_someone_given_another_role_while_their_reply_streams_acts_in_it(
    adult_chat, an_adult_agrees_to_a_childs_rename, grant, set_role, role, actions, result
):
    await grant("profile.rename", role="guest", access="deny")

    async def demoted() -> None:
        await set_role(adult_chat.person, role)

    adult_chat.use_model(ActsBeforeItsCall(demoted, call("profile_rename", {"name": "Alex B"})))
    reply = await adult_chat.say("appelle-moi Alex B")
    assert reply.actions == actions
    assert json.loads(reply.model_result_text) == result
    assert await display_name_of(adult_chat.server.session_factory, adult_chat.person) == "Alex"


async def test_within_the_turn_the_call_and_its_result_are_sent_back_as_such(owner_chat):
    model = owner_chat.use_model(one_call_then(call("household_list")))
    await owner_chat.say("qui ?")
    # The call, then its result, in the provider's own terms within the turn.
    (assistant_calls, result) = model.calls[1].messages[1:]
    assert assistant_calls.calls[0].name == "household_list"
    assert (result.call_id, result.name) == ("c1", "household_list")


# --- the prompt and the tools offered ------------------------------------------------------------


async def test_the_prompt_says_domo_can_act_when_it_has_tools(
    owner_chat, adult_chat, child_chat, guest_chat
):
    for chat in (owner_chat, adult_chat, child_chat, guest_chat):
        model = chat.use_model(FakeChatModel(["ok"]))
        await chat.say("salut")
        person = chat.person
        # Only the owner is told the list of their successors.
        is_owner = person.role == "owner"
        assert model.calls[0].system == build_system_prompt(
            abilities=PAGE,
            display_name=person.display_name,
            role=person.role,
            can_act=True,
            **turn_tools(person.role),
            successors=[] if is_owner else None,
            today=today_of(chat.clock(), None),
            household=tuple(
                HouseholdLine(name, role, is_you=name == person.display_name)
                for name, role in (
                    ("Owner", "owner"),
                    ("Alex", "adult"),
                    ("Sam", "child"),
                    ("Robin", "guest"),
                )
            ),
            # No model of meanings in the pilots: the search works by words alone.
            memory=MemoryBlock(search="words_only"),
            models=PILOTS_MODEL,
            keys=pilots_keys(chat),
            reads="en",
            # Nobody's word has been weighed yet: the household's start (trust per person).
            trust=Trust(percent=70, facts=0),
        )
        assert model.calls[0].tools and model.calls[0].tool_choice == "auto"


async def test_without_any_tool_domo_is_told_it_cannot_act(guest_chat, chat_server):
    # Nothing for anyone, and nothing for Domo either: making his tools is his own act, offered
    # by his own rights (Task 9).
    nothing = PolicyTables(known=DEFAULT_POLICY.known, allowed={}, domo=frozenset())
    chat_server.app.dependency_overrides[get_policy] = lambda: nothing
    model = guest_chat.use_model(FakeChatModel(["ok"]))
    await guest_chat.say("salut")
    assert model.calls[0].tools == ()
    assert model.calls[0].system == build_system_prompt(
        abilities=PAGE,
        display_name="Robin",
        role="guest",
        can_act=False,
        today=today_of(guest_chat.clock(), None),
        household=(
            HouseholdLine("Owner", "owner"),
            HouseholdLine("Robin", "guest", is_you=True),
        ),
        # No model of meanings in the pilots: the search works by words alone.
        memory=MemoryBlock(search="words_only"),
        models=PILOTS_MODEL,
        keys=pilots_keys(guest_chat),
        reads="en",
        # Nobody's word has been weighed yet: the household's start (trust per person).
        trust=Trust(percent=70, facts=0),
    )


async def test_a_model_without_tool_support_is_told_it_cannot_act(owner_chat, caplog):  # D6
    caplog.set_level(logging.WARNING)
    model = owner_chat.use_model(FakeChatModel(["salut"], refuse_tools=True))
    reply = await owner_chat.say("hello")
    assert (reply.text, reply.status) == ("salut", "complete")
    assert [bool(c.tools) for c in model.calls] == [True, False]
    assert model.calls[1].system == build_system_prompt(
        abilities=PAGE,
        display_name="Owner",
        role="owner",
        can_act=False,
        successors=[],
        today=today_of(owner_chat.clock(), None),
        household=(HouseholdLine("Owner", "owner", is_you=True),),
        # No model of meanings in the pilots: the search works by words alone.
        memory=MemoryBlock(search="words_only"),
        models=PILOTS_MODEL,
        keys=pilots_keys(owner_chat),
        reads="en",
        # Nobody's word has been weighed yet: the household's start (trust per person).
        trust=Trust(percent=70, facts=0),
    )
    assert "cannot act" in caplog.text


async def test_domo_is_told_the_day_in_the_households_time_zone(adult_chat, set_setting):
    """A model has no today: "until Sunday" is a date only once the code says which day it is,
    in the household's time zone once the owner chose one (read again at every turn)."""
    model = adult_chat.use_model(FakeChatModel(["ok"]))
    await adult_chat.say("salut")
    in_utc = today_of(adult_chat.clock(), None).line()
    assert in_utc.endswith("in UTC (the household's time zone is not set).")
    assert in_utc in model.calls[0].system.split("\n\nContext\n")[1].splitlines()

    await set_setting("household.time_zone", "Pacific/Kiritimati")
    model = adult_chat.use_model(FakeChatModel(["ok"]))
    await adult_chat.say("et maintenant ?")
    told = today_of(adult_chat.clock(), ZoneInfo("Pacific/Kiritimati"))
    assert told.zone == "Pacific/Kiritimati"
    assert told.line() in model.calls[0].system.split("\n\nContext\n")[1].splitlines()


# --- the barrier and the audit -------------------------------------------------------------------


async def test_every_decision_of_the_barrier_is_audited(guest_chat):  # R19, R56
    guest_chat.use_model(
        FakeChatModel(
            tool_script=[
                call("profile_rename", {"name": "Robin B"}, "c1"),
                call("household_list", None, "c2"),
                ToolCall("c3", "rm_rf", "{oops"),
                call("profile_rename", {"name": ""}, "c4"),
                "ok",
            ],
            judge="do",
        )
    )
    reply = await guest_chat.say("x")
    assert [a["status"] for a in reply.actions] == ["done", "refused", "invalid", "invalid"]
    factory = guest_chat.server.session_factory
    person = f"person:{guest_chat.person.id}"
    # Only where the barrier decided: an unknown tool (or bad JSON) and arguments the tool
    # rejected never reach it.
    assert await decisions(factory) == [
        {
            "tool": "profile_rename",
            "action_type": "profile.rename",
            "target": person,
            "decision": "allow",
            "reason": None,
            "facts": {},
        },
        {
            "tool": "household_list",
            "action_type": "household.list",
            "target": None,
            "decision": "refuse",
            "reason": "not_allowed",
            "facts": {},
        },
    ]
    async with factory() as reader:
        assert (await verify_audit_chain(reader)).ok
        entries = await audit_entries(reader)
    assert all(e.actor == person for e in entries if e.action == "policy.decision")
    # The arguments are not in the audit log.
    assert "Robin B" not in json.dumps([e.details for e in entries])


async def test_a_waiting_decision_is_audited_with_the_judges_verdict(adult_chat):
    adult_chat.use_model(one_call_then(call("profile_rename", {"name": "Alex B"})))
    await adult_chat.say("x")
    # The barrier allows it; the judge asks a yes first.
    (entry,) = await decisions(adult_chat.server.session_factory)
    assert (entry["decision"], entry["reason"]) == ("allow", None)
    async with adult_chat.server.session_factory() as reader:
        (verdict,) = [e for e in await audit_entries(reader) if e.action == "judge.verdict"]
    assert (verdict.details["tool"], verdict.details["verdict"]) == ("profile_rename", "yes")


async def test_arguments_a_tool_rejects_when_it_runs_are_invalid(owner_chat, use_tool):  # R14
    async def not_found(arguments, ctx):
        raise InvalidArguments("not_found", "nobody has that name")

    use_tool(SpyTool(execute=not_found))
    owner_chat.use_model(one_call_then(call("spy")))
    reply = await owner_chat.say("x")
    assert reply.actions == [{"tool": "spy", "status": "invalid", "reason": "not_found"}]
    assert json.loads(reply.model_result_text) == {
        "status": "invalid",
        "reason": "not_found",
        "detail": "nobody has that name",
    }
    # The barrier allowed it: that decision is audited.
    assert [d["decision"] for d in await decisions(owner_chat.server.session_factory)] == ["allow"]


async def test_a_tool_that_breaks_fails_cleanly(owner_chat, use_tool, caplog):
    async def boom(arguments, ctx):
        raise RuntimeError("secret detail that must not be shown")

    use_tool(SpyTool(execute=boom))
    owner_chat.use_model(one_call_then(call("spy"), "Désolé."))
    reply = await owner_chat.say("x")
    assert reply.actions == [{"tool": "spy", "status": "failed"}]
    assert json.loads(reply.model_result_text) == {"status": "failed"}
    assert (reply.text, reply.status) == ("Désolé.", "complete")
    assert "secret detail" not in caplog.text and "RuntimeError" in caplog.text
    assert cards_in(reply.cards_html) == [("failed", t("action.card.failed", "en"))]


async def test_a_head_that_cannot_be_read_leaves_the_card_its_sentence(
    owner_chat, monkeypatch, caplog
):
    async def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("secret detail of the database")

    monkeypatch.setattr(page_render, "head_of", broken)
    owner_chat.use_model(
        FakeChatModel(tool_script=[ToolCall("c1", "household_list", "{}"), "Voilà."])
    )
    reply = await owner_chat.say("qui est là ?")
    # The turn goes on: the card is shown with its sentence, the reply is Domo's.
    assert (reply.text, reply.status) == ("Voilà.", "complete")
    assert cards_in(reply.cards_html) == [("done", t("action.card.done.household.list", "en"))]
    assert "secret detail" not in caplog.text and "RuntimeError" in caplog.text


# --- what a tool is given ------------------------------------------------------------------------


async def test_a_tool_is_given_the_moment_of_the_call(adult_chat, use_tool, chat_server):
    spy = use_tool(SpyTool())
    adult_chat.use_model(one_call_then(call("spy")))
    reply = await adult_chat.say("regarde")
    (ctx,) = spy.contexts
    assert (ctx.actor.person_id, ctx.actor.role, ctx.actor.trusted) == (
        adult_chat.person.id,
        "adult",
        True,
    )
    assert ctx.person.id == adult_chat.person.id
    assert ctx.origin == chat_server.base_url + "/"
    assert ctx.clock() == chat_server.clock()
    assert ctx.registry["spy"] is spy and ctx.policy == DEFAULT_POLICY
    async with chat_server.session_factory() as reader:
        rows = await reader.execute(
            text("SELECT id, conversation_id, role FROM messages ORDER BY created_at")
        )
        (person_message, answer) = rows.all()
    # The person's message of this turn, and the conversation it is in.
    assert ctx.reply_message_id == person_message.id and person_message.role == "user"
    assert ctx.conversation_id == person_message.conversation_id
    assert answer.id == reply.message_id


async def test_what_is_shown_once_is_never_stored_nor_given_to_the_model(
    owner_chat, use_tool, chat_server
):  # R16
    secret = "ONCE-" + uuid.uuid4().hex

    async def with_a_link(arguments, ctx):
        return ToolOutcome(
            "done",
            {"made": True},
            {"key": "action.card.done", "params": {}},
            card_once={"key": "action.card.done.profile.rename", "params": {"name": secret}},
        )

    use_tool(SpyTool(execute=with_a_link))
    model = owner_chat.use_model(FakeChatModel(tool_script=[call("spy"), "Voilà.", "Et ensuite ?"]))
    reply = await owner_chat.say("fais-le")
    assert secret in reply.cards_html  # shown once, to the person who asked
    await owner_chat.say("et alors ?")  # a later turn: what the model is sent then
    assert secret not in (await owner_chat.get("/chat")).text
    assert secret not in json.dumps(
        [[repr(m) for m in c.messages] for c in model.calls] + [c.system for c in model.calls]
    )
    async with chat_server.session_factory() as reader:
        for table in ("message_actions", "messages", "audit_log", "confirmations"):
            dump = (await reader.execute(text(f"SELECT CAST(t AS text) FROM {table} t"))).all()
            assert secret not in json.dumps([row[0] for row in dump]), table


# --- the cards, in the stream and on the page ----------------------------------------------------


async def test_the_cards_show_under_the_reply_in_the_stream_and_on_the_page(adult_chat):
    adult_chat.use_model(
        FakeChatModel(
            tool_script=[
                call("profile_rename", {"name": "<b>Al</b> & co"}, "c1"),
                ToolCall("c2", "rm_rf", "{}"),
                "Voilà.",
            ],
            judge="do",
        )
    )
    reply = await adult_chat.say("x")
    expected = [
        ("done", t("action.card.done.profile.rename", "en", name="<b>Al</b> & co")),
        ("invalid", t("action.card.invalid.unknown_tool", "en")),
    ]
    assert cards_in(reply.cards_html) == expected
    assert [e.event for e in reply.events] == ["action", "action", "delta", "done"]
    assert "<b>" not in reply.cards_html and "&lt;b&gt;Al&lt;/b&gt; &amp; co" in reply.cards_html
    page = (await adult_chat.get("/chat")).text
    assert cards_in(page) == expected
    assert "<b>Al" not in page
    # Under the words of the reply, inside its bubble.
    bubble = re.search(r'<div class="message assistant".*?</div>\s*</div>', page, re.DOTALL)
    assert bubble is not None and 'class="message-actions"' in bubble.group(0)


async def test_the_cards_are_in_the_language_of_the_reader(guest_chat, session_factory):
    async with session_factory() as writer:
        await reads_in(writer, guest_chat.person, "fr")
        await writer.commit()
    guest_chat.use_model(one_call_then(call("household_list")))
    msg_id = await post_message(guest_chat.client, "x")
    async with guest_chat.client.stream("GET", f"/chat/stream/{msg_id}") as response:
        body = (await response.aread()).decode()
    refused = in_language("fr", "action.card.refused.not_allowed")
    assert refused in html.unescape(body)
    page = (await guest_chat.get("/chat")).text
    assert cards_in(page) == [("refused", refused)]


async def test_the_live_reply_has_a_place_for_the_cards(owner_chat):
    r = await owner_chat.client.post("/chat/messages", data={"text": "hi"})
    cards = re.search(r'<[^>]*\bclass="message-actions"[^>]*>', r.text)
    assert cards is not None
    assert 'sse-swap="action"' in cards.group(0) and 'hx-swap="beforeend"' in cards.group(0)
    await consume_sse(owner_chat.client, f"/chat/stream/{assistant_id_in(r.text)}")


async def test_a_stored_card_the_catalog_no_longer_knows_says_its_status(owner_chat, chat_server):
    owner_chat.use_model(one_call_then(call("household_list")))
    await owner_chat.say("x")
    async with chat_server.session_factory() as writer:
        await writer.execute(
            text("""UPDATE message_actions SET summary = '{"key": "gone.key", "params": {}}'""")
        )
        await writer.commit()
    page = (await owner_chat.get("/chat")).text
    assert cards_in(page) == [("done", t("action.card.done", "en"))]


# --- the history ---------------------------------------------------------------------------------


async def test_actions_come_before_the_words_of_their_reply_in_the_history(owner_chat):
    model = owner_chat.use_model(
        FakeChatModel(
            tool_script=[
                ToolCall("c1", "household_list", "{}"),
                ToolCall("c2", "rm_rf", "{}"),
                "Voilà.",
                "Encore ?",
            ]
        )
    )
    await owner_chat.say("qui ?")
    await owner_chat.say("et alors")
    assert model.calls[-1].messages == [
        ChatMessage("user", "qui ?"),
        ChatMessage(
            "assistant", "[action household.list: done]\n[action unknown: invalid]\nVoilà."
        ),
        ChatMessage("user", "et alors"),
    ]


class FailsAfterOneRound:
    """Calls ``household_list``, then fails when asked again."""

    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, **_):
        self.calls += 1
        if self.calls > 1:
            raise ModelCallError("upstream")
        yield ToolCall("c1", "household_list", "{}")


async def test_what_was_done_stays_in_the_history_when_the_reply_failed(owner_chat):
    owner_chat.use_model(FailsAfterOneRound())
    first = await owner_chat.say("qui ?")
    assert (first.status, first.actions[0]["status"]) == ("error", "done")
    model = owner_chat.use_model(FakeChatModel(["ok"]))
    await owner_chat.say("alors ?")
    assert model.calls[0].messages == [
        ChatMessage("user", "qui ?"),
        ChatMessage("assistant", "[action household.list: done]"),
        ChatMessage("user", "alors ?"),
    ]


# --- a reader who leaves while a tool runs -------------------------------------------------------


async def test_a_tool_that_runs_when_the_reader_leaves_is_still_recorded(
    owner_chat, use_tool, chat_server
):
    started, release = asyncio.Event(), asyncio.Event()

    async def slow(arguments, ctx):
        started.set()
        await release.wait()
        return ToolOutcome("done", {}, {"key": "action.card.done", "params": {}})

    use_tool(SpyTool(execute=slow))
    owner_chat.use_model(one_call_then(call("spy")))
    msg_id = await post_message(owner_chat.client, "x")
    async with owner_chat.client.stream("GET", f"/chat/stream/{msg_id}") as response:
        assert response.status_code == 200
        await asyncio.wait_for(started.wait(), 5)
        # leaving the block closes the connection while the tool runs
    await asyncio.sleep(0.2)
    release.set()
    async with chat_server.session_factory() as reader:
        # Settled either way: cut short, or finished by a model that never had to wait.
        await wait_for_status(reader, msg_id, {"partial", "complete"})
    actions = await stored_actions(chat_server.session_factory)
    assert [(a[0], a[2]) for a in actions] == [("spy", "done")]
    assert [d["decision"] for d in await decisions(chat_server.session_factory)] == ["allow"]


# --- the pilot itself ----------------------------------------------------------------------------


async def test_say_with_tool_makes_the_model_call_once_then_puts_the_model_back(adult_chat):
    before = adult_chat.use_model(FakeChatModel(["before"]))
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"}, judge="do")
    assert reply.actions == [{"tool": "profile_rename", "status": "done"}]
    assert reply.text == "ok" and reply.pending_confirmation_id is None
    assert adult_chat.server.model is before
    assert (await adult_chat.say("et ?")).text == "before"


async def test_say_with_tool_answers_unclear_to_a_call_without_tools():
    model = ToolCallingModel(call("household_list"))
    no_tools = [c async for c in model.stream(system="s", messages=[], max_output_tokens=4)]
    assert no_tools[0].text == "unclear"
    tools = (TOOLS["household_list"].definition,)
    first = [
        c async for c in model.stream(system="s", messages=[], max_output_tokens=4, tools=tools)
    ]
    again = [
        c async for c in model.stream(system="s", messages=[], max_output_tokens=4, tools=tools)
    ]
    assert first[0] == call("household_list") and again[0].text == "ok"


async def test_the_pilots_are_one_household_on_one_server(
    owner_chat, adult_chat, live_child_client, chat_server
):
    assert owner_chat.server is adult_chat.server is chat_server
    assert (owner_chat.person.role, adult_chat.person.role) == ("owner", "adult")
    assert (await live_child_client.get("/chat")).status_code == 200
    await owner_chat.say("mine")
    await adult_chat.say("theirs")
    assert "mine" not in (await adult_chat.get("/chat")).text


def test_a_confirmation_card_is_read_from_its_html():
    """What ``confirm_last`` reads on the card the server answers (tasks 6 and 9 render it)."""
    shown = uuid.uuid4()
    page = (
        f'<div class="confirmation" data-confirmation-id="{shown}" '
        'data-confirmation-status="executed" data-outcome="already_decided">'
        '<a href="https://domo.example.com/invite/AbC_-12">link</a></div>'
    )
    card = Card.read(page, uuid.uuid4())
    assert (card.id, card.status, card.invitation_token) == (shown, "already_decided", "AbC_-12")
    plain = Card.read('<div data-confirmation-status="refused"></div>', shown)
    assert (plain.id, plain.status, plain.invitation_token) == (shown, "refused", None)


# --- fix round 1: time limits, too many calls, records that cannot be stored ---------------------


def test_the_arguments_are_read_only_when_short_flat_json_objects(monkeypatch):
    assert execution._arguments_of("[" * 5000) is None
    assert execution._arguments_of('{"a": "' + "x" * 16_000 + '"}') is None
    assert execution._arguments_of("[1]") is None
    assert execution._arguments_of('{"name": "Alex"}') == {"name": "Alex"}

    # How deep the parser goes before it gives up depends on the Python and the system.
    def too_deep(raw: str) -> object:
        raise RecursionError

    monkeypatch.setattr(execution.json, "loads", too_deep)
    assert execution._arguments_of('{"name": "Alex"}') is None


async def test_a_tool_that_takes_too_long_is_recorded_with_an_unknown_outcome(
    owner_chat, use_tool, chat_server, monkeypatch
):
    monkeypatch.setattr(execution, "_TOOL_SECONDS", 0.3)

    async def slow(arguments, ctx):
        await asyncio.sleep(10)
        return ToolOutcome("done", {}, {"key": "action.card.done", "params": {}})

    use_tool(SpyTool(execute=slow))
    owner_chat.use_model(one_call_then(call("spy"), "Je ne sais pas si c'est fait."))
    reply = await owner_chat.say("fais-le")
    assert reply.actions == [{"tool": "spy", "status": "failed", "reason": "timeout"}]
    assert reply.status == "complete"
    # The model is told plainly that the action may have happened, so as not to do it twice.
    result = json.loads(reply.model_result_text)
    assert (result["status"], result["reason"], result["outcome"]) == (
        "failed",
        "timeout",
        "unknown",
    )
    assert "may have happened" in result["detail"] and "again" in result["detail"]
    # Stored and audited like any other action, and still there after a reload.
    assert [(a[0], a[2]) for a in await stored_actions(chat_server.session_factory)] == [
        ("spy", "failed")
    ]
    assert [d["decision"] for d in await decisions(chat_server.session_factory)] == ["allow"]
    expected = [("failed", t("action.card.failed.timeout", "en"))]
    assert cards_in(reply.cards_html) == expected
    assert cards_in((await owner_chat.get("/chat")).text) == expected


async def test_the_history_keeps_the_doubt_about_an_action_that_took_too_long(
    owner_chat, use_tool, monkeypatch
):  # R71: a plain "failed" would tell the next turns that it did not happen
    monkeypatch.setattr(execution, "_TOOL_SECONDS", 0.3)

    async def slow(arguments, ctx):
        await asyncio.sleep(10)
        return ToolOutcome("done", {}, {"key": "action.card.done", "params": {}})

    use_tool(SpyTool(execute=slow))
    owner_chat.use_model(one_call_then(call("spy"), "Je ne sais pas."))
    await owner_chat.say("fais-le")
    model = owner_chat.use_model(FakeChatModel(["ok"]))
    await owner_chat.say("alors ?")
    assert model.calls[0].messages[1] == ChatMessage(
        "assistant", "[action household.list: failed (outcome unknown)]\nJe ne sais pas."
    )


async def test_an_action_that_failed_is_only_failed_in_the_history(owner_chat, use_tool):
    async def broken(arguments, ctx):
        raise RuntimeError("broken")

    use_tool(SpyTool(execute=broken))
    owner_chat.use_model(one_call_then(call("spy"), "Raté."))
    await owner_chat.say("fais-le")
    model = owner_chat.use_model(FakeChatModel(["ok"]))
    await owner_chat.say("alors ?")
    assert model.calls[0].messages[1] == ChatMessage(
        "assistant", "[action household.list: failed]\nRaté."
    )


async def test_an_action_that_ran_but_could_not_be_recorded_is_logged(
    adult_chat, chat_server, monkeypatch, caplog
):
    async def hang(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(execution, "record_decision", hang)
    monkeypatch.setattr(execution, "_STORE_SECONDS", 0.3)
    adult_chat.use_model(
        one_call_then(call("profile_rename", {"name": "Alex Z"}), "C'est fait.", judge="do")
    )
    reply = await adult_chat.say("appelle-moi Alex Z")
    # The rename ran (the tool committed it), the model is told so, and the turn ends well...
    assert await display_name_of(chat_server.session_factory, adult_chat.person) == "Alex Z"
    assert json.loads(reply.model_result_text)["status"] == "done"
    assert (reply.text, reply.status) == ("C'est fait.", "complete")
    # ...but its record could not be written: an operator is told which action to reconcile.
    assert reply.actions == []
    assert "profile_rename" in caplog.text and "profile.rename" in caplog.text
    assert "could not be recorded" in caplog.text
    assert "Alex Z" not in caplog.text  # never the arguments


class ManyCallsAtOnce:
    """Makes ``count`` calls in its first answer, then says "ok"."""

    def __init__(self, count: int) -> None:
        self._count = count
        self.calls = 0

    async def stream(self, **_):
        self.calls += 1
        if self.calls == 1:
            for index in range(self._count):
                yield ToolCall(f"c{index}", "household_list", "{}")
        else:
            yield ChatChunk("ok")
        yield ChatUsage(3, 2)


async def test_too_many_calls_in_one_answer_are_not_run(owner_chat, chat_server):
    owner_chat.use_model(ManyCallsAtOnce(10))
    reply = await owner_chat.say("liste tout, dix fois")
    too_many = {"tool": "household_list", "status": "invalid", "reason": "too_many"}
    assert reply.actions == [{"tool": "household_list", "status": "done"}] * 8 + [too_many] * 2
    results = [json.loads(line) for line in reply.model_result_text.splitlines()]
    assert [r["status"] for r in results] == ["done"] * 8 + ["invalid"] * 2
    assert results[-1]["reason"] == "too_many"
    # Only the calls that ran went through the barrier.
    assert len(await decisions(chat_server.session_factory)) == 8
    assert cards_in(reply.cards_html)[-1] == ("invalid", t("action.card.invalid.too_many", "en"))
