"""The owner's list of successors (spec 7 and 8): an ordered list, kept by the owner alone and seen
by the owner alone, of who takes over if the owner can no longer.

A successor is a person of the household or someone outside it (a name, and what the owner says of
them: "my brother"), with the way the door is opened the day it comes (a personal access that only
claims the role, or a paper code kept with the will). The owner sees the list, changes it with an
explicit yes every time (the card shows the list before and after), and says that a successor died
(no yes: the next one comes first). Someone who left the household, or a child, stays on the list
and the owner's context says so. The list is the household's: it passes to the new owner by itself.

The chat pilots talk with Domo on a live server (see ``chat_support.py``); the service and the
tools are also tried directly.
"""

import html
import json
import re
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from chat_support import Card, an_actor
from domo_common.i18n import t
from domo_core.accounts.models import Person
from domo_core.confirmations.describe import describe_for_people
from domo_core.confirmations.store import ANSWER_EXPLICITLY
from domo_core.household.service import disable, rename_person, transfer_ownership
from domo_core.household.succession import (
    MAX_SUCCESSORS,
    SuccessorEntry,
    SuccessorShown,
    list_successors,
    note_death,
    noted_death,
    replace_successors,
    successors_in_words,
    undo_death,
)
from domo_core.policy.actions import DescribedAction
from domo_core.policy.catalog import (
    ACTION_FACTS,
    DEFAULT_POLICY,
    OWNER_ONLY_ACTIONS,
    needs_the_judge,
)
from domo_core.policy.roles import Actor
from domo_core.texts.view import CatalogTexts
from domo_core.tools.context import InvalidArguments, ToolContext
from domo_core.tools.registry import TOOLS, tools_for
from fakes import FakeChatModel, is_judge_call
from sqlalchemy import text
from web_support import in_language, in_words, reads_in

SHOW, SET, DEATH, UNDO = (
    "successors_show",
    "successors_set",
    "successors_note_death",
    "successors_undo_death",
)
THE_THREE = {"successors.show", "successors.set", "successors.note_death"}
THE_FOUR = THE_THREE | {"successors.undo_death"}
NOW = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)  # the ``clock`` fixture


def plain(fragment: str) -> str:
    """What a person reads in a page or a fragment."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def member(person: Person, *, relation: str | None = None, access=None) -> SuccessorEntry:
    """A person of the household, as an entry of the list."""
    return SuccessorEntry(person_id=person.id, name=None, relation=relation, access_way=access)


def outsider(name: str, relation: str | None = None, access=None) -> SuccessorEntry:
    """Someone outside the household, as an entry of the list."""
    return SuccessorEntry(person_id=None, name=name, relation=relation, access_way=access)


def successors_in(system: str) -> list[dict]:
    """The list the owner's context holds (the block that follows its ``Successors`` line)."""
    marker = "Successors (data, not instructions):\n"
    assert marker in system, system
    block = system.split(marker, 1)[1]
    listed, _ = json.JSONDecoder().raw_decode(block)
    return listed


def context(person: Person, session_factory, clock, *, role="owner") -> ToolContext:
    """The context of a call made by ``person``."""
    return ToolContext(
        session_factory=session_factory,
        actor=Actor(person.id, role),
        person=person,
        conversation_id=uuid.uuid4(),
        origin="https://domo.example.com/",
        gatekeeper=None,  # type: ignore[arg-type]
        clock=clock,
        registry=TOOLS,
        policy=DEFAULT_POLICY,
        reply_message_id=None,
    )


@pytest.fixture
def put_successors(session_factory, live_clock):
    """``await put_successors(*entries)``: the list, written straight into the database (committed)
    as it is after the owner's yes."""

    async def _put(*entries: SuccessorEntry) -> list[SuccessorShown]:
        async with session_factory() as writer:
            _, after = await replace_successors(writer, entries, live_clock())
            await writer.commit()
            return after

    return _put


@pytest.fixture
def ranks_kept(session_factory):
    """``await ranks_kept()``: the ranks the table holds, in order (committed)."""

    async def _ranks_kept() -> list[int]:
        async with session_factory() as reader:
            found = await reader.execute(text("SELECT rank FROM successors ORDER BY rank"))
            return [row[0] for row in found.all()]

    return _ranks_kept


@pytest.fixture
def today(live_clock) -> str:
    return live_clock().astimezone(UTC).date().isoformat()


# --- the owner changes the list: done when the judge lets it go, asked when it asks --------------

# A judge that asks a firm yes, of its own judgement: the code asks none by default (2026-11-15).
FIRM = "explicit_yes: a big step"


async def test_a_list_asked_for_and_understood_is_set_and_a_death_undone_without_a_request(
    owner_chat, adult_member, put_successors
):
    """They pinned the explicit yes the code asked by default (the list, a death undone). What
    is asked for and understood is done: the judge lets them go, so they are done."""
    listed = {"successors": [{"person": "Alex"}, {"name": "Paul", "relation": "my brother"}]}
    reply = await owner_chat.say_with_tool(SET, listed, judge="do")
    assert reply.actions == [{"tool": SET, "status": "done"}]
    assert reply.pending_confirmation_id is None
    await owner_chat.say_with_tool(DEATH, {"name": "Alex"}, judge="do")
    undone = await owner_chat.say_with_tool(UNDO, {"name": "Alex"}, judge="do")
    assert undone.actions == [{"tool": UNDO, "status": "done"}]
    shown = await owner_chat.say_with_tool(SHOW, {})
    assert [(s["name"], s["deceased_on"]) for s in shown.model_results[-1]["successors"]] == [
        ("Alex", None),
        ("Paul", None),
    ]


async def test_the_owner_sets_the_list_with_an_explicit_yes_and_reads_before_and_after(
    owner_chat, adult_member, confirmation_status, last_audit_entry
):
    listed = {
        "successors": [
            {"person": "Alex"},
            {"name": "Paul", "relation": "my brother", "access": "paper_code"},
        ]
    }
    reply = await owner_chat.say_with_tool(SET, listed, judge=FIRM)
    assert reply.actions == [{"tool": SET, "status": "waiting"}]
    # Domo is told how the yes can be given: the firm yes the judge asked.
    assert reply.model_results[-1]["answer_with"] == ANSWER_EXPLICITLY
    assert "confirmation-successors-after" in reply.cards_html and "Paul" in reply.cards_html
    words = plain(reply.cards_html)
    assert t("confirmation.successors.before", "en", list=t("successors.none", "en")) in words
    assert (
        t(
            "confirmation.successors.after",
            "en",
            list="1. Alex (adult) ; 2. Paul (my brother, paper code)",
        )
        in words
    )
    assert t("action.describe.successors.set", "en") in words

    await owner_chat.say("oui")
    assert await confirmation_status(reply.pending_confirmation_id) == "pending"
    await owner_chat.say("je confirme")
    assert await confirmation_status(reply.pending_confirmation_id) == "executed"

    shown = await owner_chat.say_with_tool(SHOW, {})
    listed_now = shown.model_results[-1]["successors"]
    assert [(s["rank"], s["name"], s["in_household"]) for s in listed_now] == [
        (1, "Alex", True),
        (2, "Paul", False),
    ]
    assert listed_now[1]["access_way"] == "paper_code"
    assert listed_now[1]["relation"] == "my brother"
    # The audit holds the names, in order, and nothing else; the owner is the one who changed it.
    entry = await last_audit_entry("successors.changed")
    assert entry.actor == f"person:{owner_chat.person.id}"
    assert entry.details == {"before": [], "after": ["Alex", "Paul"]}


async def test_the_button_is_enough_and_the_card_tells_the_list_before_and_after(
    owner_chat, adult_member, put_successors
):
    await put_successors(outsider("Paul"))
    reply = await owner_chat.say_with_tool(SET, {"successors": [{"person": "Alex"}]})
    card = await owner_chat.confirm_last(reply)
    assert card.status == "executed"
    words = plain(card.html)
    assert t("action.card.done.successors.set", "en", before="Paul", after="Alex") in words
    # What has been done has no "before and after" left to read.
    assert "confirmation-successors" not in card.html


async def test_the_card_of_a_request_that_waits_shows_the_list_as_it_is_now(
    owner_chat, adult_member, put_successors
):
    await put_successors(outsider("Paul", "my brother"))
    reply = await owner_chat.say_with_tool(SET, {"successors": [{"person": "Alex"}]})
    page = (await owner_chat.get("/chat")).text
    assert 'class="confirmation-successors-before"' in page
    words = plain(page)
    assert t("confirmation.successors.before", "en", list="1. Paul (my brother)") in words
    assert t("confirmation.successors.after", "en", list="1. Alex (adult)") in words
    # The list changes meanwhile: the card says what it is then.
    await put_successors(outsider("Lou"))
    words = plain((await owner_chat.get("/chat")).text)
    assert t("confirmation.successors.before", "en", list="1. Lou") in words
    assert reply.pending_confirmation_id is not None


async def test_the_list_can_be_emptied(owner_chat, put_successors, last_audit_entry):
    await put_successors(outsider("Paul"))
    reply = await owner_chat.say_with_tool(SET, {"successors": []})
    words = plain(reply.cards_html)
    assert t("confirmation.successors.after", "en", list=t("successors.none", "en")) in words
    card = await owner_chat.confirm_last(reply)
    assert t(
        "action.card.done.successors.set", "en", before="Paul", after=t("successors.none", "en")
    ) in plain(card.html)
    assert (await owner_chat.say_with_tool(SHOW, {})).model_results[-1] == {
        "status": "done",
        "successors": [],
    }
    assert (await last_audit_entry("successors.changed")).details == {
        "before": ["Paul"],
        "after": [],
    }


async def test_the_card_speaks_french_to_someone_who_reads_french(
    owner_chat, adult_member, put_successors, session_factory
):
    await put_successors(outsider("Paul", "mon frère", "paper_code"))
    await owner_chat.say_with_tool(SET, {"successors": [{"person": "Alex"}]})
    async with session_factory() as writer:
        await reads_in(writer, owner_chat.person, "fr")
        await writer.commit()
    page = await owner_chat.get("/chat")
    words = plain(page.text)
    # Each part in the reader's version; the relation is the owner's own words.
    paper_code = in_language("fr", "successors.access.paper_code")
    before = in_language(
        "fr", "confirmation.successors.before", list=f"1. Paul (mon frère, {paper_code})"
    )
    assert before in words
    adult = in_language("fr", "household.role.adult")
    assert in_language("fr", "confirmation.successors.after", list=f"1. Alex ({adult})") in words
    assert in_language("fr", "action.describe.successors.set") in words


# --- the way the door is opened, and what is told ------------------------------------------------


async def test_the_tools_tell_domo_what_each_one_is_for():
    setter = TOOLS[SET].definition.description
    for words in ("outsider", "personal access", "paper code", "recommend", "only notes"):
        assert words in setter, words
    note = TOOLS[DEATH].definition.description
    for words in ("tact", "no confirmation", "next one"):
        assert words in note, words
    undo = TOOLS[UNDO].definition.description
    for words in ("by mistake", "back", "successors_show"):
        assert words in undo, words
    # They announced a firm yes by default: nothing is asked by default any more (2026-11-15).
    for description in (setter, undo, TOOLS["rights_apply"].definition.description):
        for gone in ("firm yes", "I confirm", "lifted", "waits for their yes"):
            assert gone not in description, gone
    assert TOOLS[UNDO].definition.parameters["required"] == ["name"]
    parameters = TOOLS[SET].definition.parameters
    assert parameters["required"] == ["successors"] and parameters["additionalProperties"] is False
    assert TOOLS[SHOW].definition.parameters["properties"] == {}


# --- only the owner ------------------------------------------------------------------------------


async def test_only_the_owner_sees_or_changes_the_list(
    adult_chat, owner_member, adult_member, grant, put_successors
):
    await put_successors(outsider("Paul"))
    calls = (
        (SHOW, {}),
        (SET, {"successors": []}),
        (DEATH, {"name": "Paul"}),
        (UNDO, {"name": "Paul"}),
    )
    for tool, arguments in calls:
        reply = await adult_chat.say_with_tool(tool, arguments)
        assert reply.actions == [{"tool": tool, "status": "refused", "reason": "not_allowed"}]
    # Domo does not even show them to an adult.
    model = adult_chat.use_model(FakeChatModel(["ok"]))
    await adult_chat.say("hello")
    assert not {tool.name for tool in model.calls[0].tools} & {SHOW, SET, DEATH, UNDO}
    # No right gives them: they are the owner's whatever the owner wrote.
    for action_type in THE_FOUR:
        await grant(action_type, person=adult_member.person, access="allow")
        await grant(action_type, role="adult", access="allow")
    for tool, arguments in calls:
        reply = await adult_chat.say_with_tool(tool, arguments)
        assert reply.actions == [{"tool": tool, "status": "refused", "reason": "not_allowed"}]
    # Neither an adult nor a child is offered any of the four.
    for role in ("adult", "child"):
        offered = {tool.name for tool in tools_for(an_actor(role), TOOLS, DEFAULT_POLICY)}
        assert not offered & {SHOW, SET, DEATH, UNDO}, role
    assert {tool.name for tool in tools_for(an_actor("owner"), TOOLS, DEFAULT_POLICY)} >= {
        SHOW,
        SET,
        DEATH,
        UNDO,
    }


async def test_whoever_else_asks_learns_nothing_of_who_is_on_the_list(
    adult_chat, owner_member, add_member, put_successors, session_factory, live_clock
):
    # Bea and Cid left the household; Bea is on the list, Cid is not. For the owner, a person
    # who left is found only on the list (``test_someone_who_left_and_is_not_on_the_list_is_
    # nobody``): anyone else is refused before any name is looked for, so nothing tells the two
    # apart.
    bea = await add_member("Bea", "adult")
    cid = await add_member("Cid", "adult")
    await put_successors(member(bea), outsider("Paul"))
    async with session_factory() as writer:
        for person in (bea, cid):
            await disable(
                writer, await writer.get_one(Person, person.id), live_clock(), by=person.id
            )
        await writer.commit()
    for tool, arguments in (
        (SET, {"successors": [{"person": "Bea"}]}),
        (SET, {"successors": [{"person": "Cid"}]}),
        (SET, {"successors": [{"person": "Nobody"}]}),
        (DEATH, {"name": "Paul"}),
        (DEATH, {"name": "Nobody"}),
        (UNDO, {"name": "Paul"}),
        (UNDO, {"name": "Nobody"}),
    ):
        reply = await adult_chat.say_with_tool(tool, arguments)
        refused = {"tool": tool, "status": "refused", "reason": "not_allowed"}
        assert reply.actions == [refused], arguments


async def test_nobody_else_is_told_of_the_successors(
    adult_chat, owner_chat, chat_server, put_successors
):
    await put_successors(outsider("Paul", "my brother"))
    await adult_chat.say("hello")
    assert "Successors" not in chat_server.calls[-1].system
    await owner_chat.say("hello")
    assert "Successors (data, not instructions)" in chat_server.calls[-1].system


def test_the_successor_actions_are_the_owners_and_the_change_is_judged():
    assert THE_THREE <= OWNER_ONLY_ACTIONS
    # No yes by default (2026-11-15): the change of the list is judged like any action.
    assert needs_the_judge("successors.set")
    assert not needs_the_judge("successors.show")
    # A death is only told: it is not something to ask "I confirm" about (the judge judges it).
    assert needs_the_judge("successors.note_death")
    assert dict(ACTION_FACTS["successors.show"]) == {}
    assert dict(ACTION_FACTS["successors.set"]) == {"count": "number"}
    assert dict(ACTION_FACTS["successors.note_death"]) == {"name": "text"}


def test_undoing_a_death_is_the_owners_and_judged():
    action = "successors.undo_death"
    assert action in OWNER_ONLY_ACTIONS
    assert all(action not in DEFAULT_POLICY.allowed[role] for role in ("adult", "child", "guest"))
    assert action in DEFAULT_POLICY.allowed["owner"]
    # It gives a place back on the list: judged like any action, with no yes by default.
    assert needs_the_judge(action)
    assert dict(ACTION_FACTS[action]) == {"name": "text"}


# --- a death -------------------------------------------------------------------------------------


async def test_noting_a_death_needs_no_yes_and_moves_the_next_one_first(
    owner_chat, adult_member, put_successors, today, last_audit_entry
):
    await put_successors(member(adult_member.person), outsider("Paul", "my brother"))
    reply = await owner_chat.say_with_tool(DEATH, {"name": "Alex"}, judge="do")
    assert reply.actions == [{"tool": DEATH, "status": "done"}]
    assert reply.pending_confirmation_id is None
    assert t("action.card.done.successors.note_death", "en", name="Alex") in plain(reply.cards_html)
    shown = await owner_chat.say_with_tool(SHOW, {})
    alex, paul = shown.model_results[-1]["successors"]
    assert (alex["name"], alex["rank"], alex["deceased_on"]) == ("Alex", None, today)
    assert (paul["name"], paul["rank"], paul["deceased_on"]) == ("Paul", 1, None)
    # The audit holds the name, nothing else; Alex is still in the household.
    assert (await last_audit_entry("successors.death_noted")).details == {"name": "Alex"}


async def test_a_death_is_told_once_and_a_name_that_is_not_on_the_list_is_asked_about(
    owner_chat, put_successors
):
    await put_successors(outsider("Paul"))
    assert (await owner_chat.say_with_tool(DEATH, {"name": "paul"}, judge="do")).actions == [
        {"tool": DEATH, "status": "done"}
    ]
    again = await owner_chat.say_with_tool(DEATH, {"name": "Paul"}, judge="do")
    assert again.actions == [{"tool": DEATH, "status": "invalid", "reason": "invalid"}]
    assert "already" in again.model_results[-1]["detail"]
    nobody = await owner_chat.say_with_tool(DEATH, {"name": "Zed"}, judge="do")
    assert nobody.actions == [{"tool": DEATH, "status": "invalid", "reason": "not_found"}]


@pytest.mark.parametrize("arguments", [{}, {"name": ""}, {"name": 3}, {"name": "A", "x": 1}])
async def test_the_death_takes_a_name_and_nothing_else(owner_chat, arguments):
    reply = await owner_chat.say_with_tool(DEATH, arguments)
    assert reply.actions == [{"tool": DEATH, "status": "invalid", "reason": "invalid"}]


async def test_the_model_is_told_what_a_death_did(
    owner_member, adult_member, put_successors, session_factory, live_clock
):
    await put_successors(member(adult_member.person), outsider("Paul"))
    ctx = context(owner_member.person, session_factory, live_clock)
    outcome = await TOOLS[DEATH].execute({"name": "Alex"}, ctx)
    assert outcome.status == "done"
    assert [s["rank"] for s in outcome.model_result["successors"]] == [None, 1]
    assert outcome.card == {
        "key": "action.card.done.successors.note_death",
        "params": {"name": "Alex"},
    }
    assert outcome.card_once is None


# --- a death noted by mistake --------------------------------------------------------------------


async def test_the_owner_undoes_a_death_noted_by_mistake_with_an_explicit_yes(
    owner_chat, adult_member, put_successors, confirmation_status, last_audit_entry
):  # spec 5.4, 8.9
    await put_successors(member(adult_member.person), outsider("Paul", "my brother"))
    # A slip of the tongue: Alex is very much alive.
    assert (await owner_chat.say_with_tool(DEATH, {"name": "Alex"}, judge="do")).actions == [
        {"tool": DEATH, "status": "done"}
    ]
    reply = await owner_chat.say_with_tool(UNDO, {"name": "Alex"}, judge=FIRM)
    assert reply.actions == [{"tool": UNDO, "status": "waiting"}]
    # Domo is told how the yes can be given: the firm yes the judge asked.
    assert reply.model_results[-1]["answer_with"] == ANSWER_EXPLICITLY
    assert t("action.describe.successors.undo_death", "en", name="Alex") in plain(reply.cards_html)

    await owner_chat.say("oui")
    assert await confirmation_status(reply.pending_confirmation_id) == "pending"
    await owner_chat.say("je confirme")
    assert await confirmation_status(reply.pending_confirmation_id) == "executed"

    shown = await owner_chat.say_with_tool(SHOW, {})
    alex, paul = shown.model_results[-1]["successors"]
    assert (alex["name"], alex["rank"], alex["deceased_on"]) == ("Alex", 1, None)
    assert (paul["name"], paul["rank"]) == ("Paul", 2)
    # The audit holds the name and nothing else; the owner is the one who undid it.
    entry = await last_audit_entry("successors.death_undone")
    assert entry.actor == f"person:{owner_chat.person.id}"
    assert entry.details == {"name": "Alex"}


async def test_the_button_is_enough_to_undo_a_death_and_the_card_says_so(
    owner_chat, put_successors
):
    await put_successors(outsider("Paul"))
    await owner_chat.say_with_tool(DEATH, {"name": "Paul"}, judge="do")
    card = await owner_chat.confirm_last(await owner_chat.say_with_tool(UNDO, {"name": "paul"}))
    assert card.status == "executed"
    assert t("action.card.done.successors.undo_death", "en", name="Paul") in plain(card.html)
    async with owner_chat.server.session_factory() as writer:
        await reads_in(writer, owner_chat.person, "fr")
        await writer.commit()
    page = await owner_chat.get("/chat")
    done = in_language("fr", "action.card.done.successors.undo_death", name="Paul")
    assert done in plain(page.text)


async def test_undoing_a_death_that_is_not_noted_is_refused(
    owner_member, put_successors, session_factory, live_clock, audit_actions
):
    await put_successors(outsider("Paul"))
    ctx = context(owner_member.person, session_factory, live_clock)
    details = {}
    for name, code in (("Paul", "invalid"), ("Zed", "not_found")):
        with pytest.raises(InvalidArguments) as refused:
            await TOOLS[UNDO].execute({"name": name}, ctx)
        assert refused.value.code == code
        details[name] = refused.value.detail
    assert "not noted" in (details["Paul"] or "")
    # Nothing was audited.
    assert "successors.death_undone" not in await audit_actions()


async def test_undoing_a_death_that_was_not_noted_is_refused_before_anything_is_asked(
    owner_chat, put_successors, chat_server, count_confirmations, audit_actions
):  # spec 5.5.3
    await put_successors(outsider("Paul"))
    reply = await owner_chat.say_with_tool(UNDO, {"name": "Paul"})
    # Refused as undoing it says (F29), before any yes: no request, and no judge asked.
    assert reply.actions == [{"tool": UNDO, "status": "invalid", "reason": "invalid"}]
    assert "not noted" in reply.model_results[-1]["detail"]
    unknown = await owner_chat.say_with_tool(UNDO, {"name": "Zed"})
    assert unknown.actions == [{"tool": UNDO, "status": "invalid", "reason": "not_found"}]
    assert await count_confirmations() == 0
    assert not [call for call in chat_server.calls if is_judge_call(call.system)]
    assert "successors.death_undone" not in await audit_actions()


async def test_a_noted_death_is_read_without_changing_anything(session, add_person, clock):
    await add_person("Owner", "owner")
    alex = await add_person("Alex", "adult")
    await replace_successors(session, [member(alex), outsider("Paul")], clock())
    await note_death(session, "paul", clock())
    noted = await noted_death(session, " PAUL ", clock())
    assert (noted.name, noted.rank) == ("Paul", None) and noted.deceased_on is not None
    # Nothing changed: the death is still noted.
    assert [(s.name, s.rank) for s in await list_successors(session, clock())] == [
        ("Alex", 1),
        ("Paul", None),
    ]
    for name, code in (("Alex", "invalid"), ("Zed", "not_found")):
        with pytest.raises(InvalidArguments) as refused:
            await noted_death(session, name, clock())
        assert refused.value.code == code
    await rename_person(session, alex, "Paul", now=clock())
    with pytest.raises(InvalidArguments) as asked:
        await noted_death(session, "paul", clock())
    assert asked.value.code == "ambiguous"


async def test_a_yes_to_undoing_a_death_that_is_not_noted_anymore_changes_nothing(
    owner_chat, put_successors, audit_actions
):
    await put_successors(outsider("Paul"))
    await owner_chat.say_with_tool(DEATH, {"name": "Paul"}, judge="do")
    first = await owner_chat.say_with_tool(UNDO, {"name": "Paul"})
    second = await owner_chat.say_with_tool(UNDO, {"name": "Paul"})
    assert (await owner_chat.confirm_last(first)).status == "executed"
    # The second yes finds the death already undone: nothing more is done.
    assert (await owner_chat.confirm_last(second)).status != "executed"
    assert (await audit_actions()).count("successors.death_undone") == 1


@pytest.mark.parametrize("arguments", [{}, {"name": ""}, {"name": 3}, {"name": "A", "x": 1}])
async def test_undoing_a_death_takes_a_name_and_nothing_else(owner_chat, arguments):
    reply = await owner_chat.say_with_tool(UNDO, arguments)
    assert reply.actions == [{"tool": UNDO, "status": "invalid", "reason": "invalid"}]


async def test_the_model_is_told_what_undoing_a_death_did(
    owner_member, adult_member, put_successors, session_factory, live_clock
):
    await put_successors(member(adult_member.person), outsider("Paul"))
    ctx = context(owner_member.person, session_factory, live_clock)
    await TOOLS[DEATH].execute({"name": "Alex"}, ctx)
    outcome = await TOOLS[UNDO].execute({"name": "Alex"}, ctx)
    assert outcome.status == "done"
    assert [s["rank"] for s in outcome.model_result["successors"]] == [1, 2]
    assert outcome.card == {
        "key": "action.card.done.successors.undo_death",
        "params": {"name": "Alex"},
    }
    assert outcome.card_once is None


# --- who stays on the list -----------------------------------------------------------------------


async def test_a_child_or_someone_who_left_stays_on_the_list_and_the_context_says_so(
    owner_chat, child_member, adult_member, chat_server, put_successors, today
):
    await put_successors(member(child_member.person), member(adult_member.person))
    removed = await owner_chat.say_with_tool("household_remove", {"person": "Alex"})
    assert (await owner_chat.confirm_last(removed)).status == "executed"
    await owner_chat.say("hello")
    sam, alex = successors_in(chat_server.calls[-1].system)
    assert (sam["name"], sam["rank"], sam["role"], sam["in_household"]) == ("Sam", 1, "child", True)
    assert (alex["name"], alex["rank"], alex["in_household"]) == ("Alex", 2, False)
    assert alex["left_on"] == today and alex["role"] is None and sam["left_on"] is None
    # Leaving the household does not take them off: the owner decides.
    shown = await owner_chat.say_with_tool(SHOW, {})
    assert [s["name"] for s in shown.model_results[-1]["successors"]] == ["Sam", "Alex"]


async def test_someone_who_left_can_stay_on_the_list_when_the_owner_changes_it(
    owner_chat, adult_member, child_member, put_successors, today
):
    await put_successors(member(adult_member.person), member(child_member.person))
    removed = await owner_chat.say_with_tool("household_remove", {"person": "Alex"})
    assert (await owner_chat.confirm_last(removed)).status == "executed"
    reply = await owner_chat.say_with_tool(
        SET, {"successors": [{"person": "Sam"}, {"person": "alex"}]}
    )
    assert reply.actions == [{"tool": SET, "status": "waiting"}]
    left = in_words(date.fromisoformat(today))
    after = f"1. Sam (child) ; 2. Alex (left the household on {left})"
    assert t("confirmation.successors.after", "en", list=after) in plain(reply.cards_html)
    assert (await owner_chat.confirm_last(reply)).status == "executed"
    shown = await owner_chat.say_with_tool(SHOW, {})
    sam, alex = shown.model_results[-1]["successors"]
    assert (sam["rank"], sam["in_household"]) == (1, True)
    assert (alex["name"], alex["rank"], alex["in_household"]) == ("Alex", 2, False)


async def test_someone_who_left_and_is_not_on_the_list_is_nobody(
    owner_chat, adult_member, session_factory, live_clock
):
    async with session_factory() as writer:
        await disable(
            writer,
            await writer.get_one(Person, adult_member.person.id),
            live_clock(),
            by=adult_member.person.id,
        )
        await writer.commit()
    reply = await owner_chat.say_with_tool(SET, {"successors": [{"person": "Alex"}]})
    assert reply.actions == [{"tool": SET, "status": "invalid", "reason": "not_found"}]


async def test_a_child_is_accepted_as_a_successor(owner_chat, child_member):
    reply = await owner_chat.say_with_tool(SET, {"successors": [{"person": "Sam"}]})
    assert reply.actions == [{"tool": SET, "status": "waiting"}]
    assert "Sam (child)" in plain(reply.cards_html)


async def test_an_ambiguous_successor_name_is_asked_about(owner_chat, add_member):
    await add_member("Paul", "adult")
    await add_member("Paul", "child")
    reply = await owner_chat.say_with_tool(SET, {"successors": [{"person": "Paul"}]})
    assert reply.actions == [{"tool": SET, "status": "invalid", "reason": "ambiguous"}]
    assert reply.pending_confirmation_id is None


async def test_the_list_is_rewritten_and_keeps_who_died(
    owner_chat, adult_member, put_successors, today
):
    await put_successors(member(adult_member.person), outsider("Paul"))
    await owner_chat.say_with_tool(DEATH, {"name": "Paul"}, judge="do")
    reply = await owner_chat.say_with_tool(
        SET, {"successors": [{"name": "  paul ", "relation": "my brother"}, {"person": "Alex"}]}
    )
    assert (await owner_chat.confirm_last(reply)).status == "executed"
    shown = await owner_chat.say_with_tool(SHOW, {})
    paul, alex = shown.model_results[-1]["successors"]
    # Paul is still dead, whatever the order, and Alex is first among the living.
    assert (paul["name"], paul["rank"], paul["deceased_on"]) == ("paul", None, today)
    assert (alex["name"], alex["rank"]) == ("Alex", 1)
    assert paul["relation"] == "my brother"


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        ({}, "invalid"),
        ({"successors": "Alex"}, "invalid"),
        ({"successors": ["Alex"]}, "invalid"),
        ({"successors": [{}]}, "invalid"),
        ({"successors": [{"person": "Alex", "name": "Paul"}]}, "invalid"),
        ({"successors": [{"person": "Alex", "why": "x"}]}, "invalid"),
        ({"successors": [{"person": 3}]}, "invalid"),
        ({"successors": [{"person": "Alex", "access": "key"}]}, "invalid"),
        ({"successors": [{"name": "Paul", "relation": 3}]}, "invalid"),
        ({"successors": [{"name": "Paul", "relation": "x" * 201}]}, "too_long"),
        ({"successors": [{"name": "Paul", "relation": "a\nb"}]}, "invalid"),
        ({"successors": [{"name": "P" * 61}]}, "too_long"),
        ({"successors": [{"name": "Paul"}, {"name": " PAUL "}]}, "invalid"),
        ({"successors": [{"person": "Alex"}, {"person": "alex"}]}, "invalid"),
        ({"successors": [{"person": "Alex"}, {"name": "Alex"}]}, "invalid"),
        ({"successors": [{"person": "Owner"}]}, "invalid"),
        ({"successors": [{"person": "Zed"}]}, "not_found"),
        ({"successors": [{"name": f"P{n}"} for n in range(MAX_SUCCESSORS + 1)]}, "too_long"),
        ({"successors": [], "x": 1}, "invalid"),
    ],
)
async def test_a_list_that_makes_no_sense_is_not_asked_a_yes_for(
    owner_chat, adult_member, arguments, code
):
    reply = await owner_chat.say_with_tool(SET, arguments)
    assert reply.actions == [{"tool": SET, "status": "invalid", "reason": code}]
    assert reply.pending_confirmation_id is None


async def test_the_most_successors_are_ten(owner_chat):
    ten = {"successors": [{"name": f"P{n}"} for n in range(10)]}
    reply = await owner_chat.say_with_tool(SET, ten)
    assert reply.actions == [{"tool": SET, "status": "waiting"}]


async def test_a_list_that_waited_is_refused_if_a_person_on_it_left_meanwhile(
    owner_chat, adult_member, session_factory, live_clock, confirmation_status
):
    """The yes was given to the list as it read: when Alex left meanwhile, nothing is changed."""
    reply = await owner_chat.say_with_tool(SET, {"successors": [{"person": "Alex"}]})
    async with session_factory() as writer:
        await disable(
            writer,
            await writer.get_one(Person, adult_member.person.id),
            live_clock(),
            by=adult_member.person.id,
        )
        await writer.commit()
    card = await owner_chat.confirm_last(reply)
    assert card.status == "refused"
    assert await confirmation_status(reply.pending_confirmation_id) == "refused"
    shown = await owner_chat.say_with_tool(SHOW, {})
    assert shown.model_results[-1]["successors"] == []


async def test_the_list_passes_to_the_new_owner(
    owner_chat, adult_chat, adult_member, chat_server, put_successors
):
    await put_successors(outsider("Paul", "my brother", "paper_code"))
    reply = await owner_chat.say_with_tool("household_transfer_ownership", {"person": "Alex"})
    assert (await owner_chat.confirm_last(reply)).status == "executed"
    await adult_chat.say("hello")
    [paul] = successors_in(chat_server.calls[-1].system)
    assert (paul["name"], paul["relation"], paul["access_way"]) == (
        "Paul",
        "my brother",
        "paper_code",
    )
    # The old owner is an adult now: the list is no longer theirs to know.
    await owner_chat.say("hello")
    assert "Successors" not in chat_server.calls[-1].system
    refused = await owner_chat.say_with_tool(SHOW, {})
    assert refused.actions == [{"tool": SHOW, "status": "refused", "reason": "not_allowed"}]
    shown = await adult_chat.say_with_tool(SHOW, {})
    assert [s["name"] for s in shown.model_results[-1]["successors"]] == ["Paul"]


async def test_a_transfer_to_the_person_ranked_first_takes_them_off_the_list(
    owner_chat, adult_chat, adult_member, add_member, put_successors, last_audit_entry, ranks_kept
):
    kim = await add_member("Kim", "adult")
    await put_successors(member(adult_member.person), outsider("Paul"), member(kim))
    reply = await owner_chat.say_with_tool("household_transfer_ownership", {"person": "Alex"})
    assert (await owner_chat.confirm_last(reply)).status == "executed"
    # The new owner is no longer a successor (an owner never is) and the next one comes first.
    shown = await adult_chat.say_with_tool(SHOW, {})
    assert [(s["name"], s["rank"]) for s in shown.model_results[-1]["successors"]] == [
        ("Paul", 1),
        ("Kim", 2),
    ]
    assert await ranks_kept() == [1, 2]
    entry = await last_audit_entry("household.ownership_transferred")
    assert entry.details["left_the_successors"] == 1


async def test_a_transfer_to_someone_in_the_middle_closes_the_gap_and_leaves_the_dead_as_they_are(
    owner_chat, adult_chat, adult_member, add_member, put_successors, ranks_kept, today
):
    kim = await add_member("Kim", "adult")
    await put_successors(outsider("Paul"), member(adult_member.person), member(kim))
    await owner_chat.say_with_tool(DEATH, {"name": "Paul"}, judge="do")
    reply = await owner_chat.say_with_tool("household_transfer_ownership", {"person": "Alex"})
    assert (await owner_chat.confirm_last(reply)).status == "executed"
    assert await ranks_kept() == [1, 2]
    shown = await adult_chat.say_with_tool(SHOW, {})
    paul, kim_now = shown.model_results[-1]["successors"]
    assert (paul["name"], paul["rank"], paul["deceased_on"]) == ("Paul", None, today)
    assert (kim_now["name"], kim_now["rank"]) == ("Kim", 1)


async def test_a_transfer_to_someone_who_is_not_on_the_list_changes_nothing_of_it(
    owner_chat, adult_chat, adult_member, put_successors, last_audit_entry, ranks_kept
):
    await put_successors(outsider("Paul"), outsider("Lou", "my sister"))
    reply = await owner_chat.say_with_tool("household_transfer_ownership", {"person": "Alex"})
    assert (await owner_chat.confirm_last(reply)).status == "executed"
    shown = await adult_chat.say_with_tool(SHOW, {})
    assert [(s["name"], s["rank"]) for s in shown.model_results[-1]["successors"]] == [
        ("Paul", 1),
        ("Lou", 2),
    ]
    assert await ranks_kept() == [1, 2]
    entry = await last_audit_entry("household.ownership_transferred")
    assert entry.details["left_the_successors"] == 0


async def test_the_service_says_whether_the_new_owner_left_the_list(
    session, add_person, add_account, clock
):
    owner = await add_person("Owner", "owner")
    await add_account(owner, "owner")
    alex = await add_person("Alex", "adult")
    await add_account(alex, "alex")
    dana = await add_person("Dana", "adult")
    await replace_successors(session, [member(alex), member(dana)], clock())
    done = await transfer_ownership(session, old_owner=owner, new_owner=alex, now=clock())
    assert done.left_the_successors == 1
    assert [(s.name, s.rank) for s in await list_successors(session, clock())] == [("Dana", 1)]
    # The old owner is not put on the list: nothing is automatic.
    back = await transfer_ownership(session, old_owner=alex, new_owner=owner, now=clock())
    assert back.left_the_successors == 0
    assert [s.name for s in await list_successors(session, clock())] == ["Dana"]


async def test_the_old_owner_does_not_read_the_list_on_a_request_they_left_waiting(
    owner_chat, adult_member, put_successors
):
    await put_successors(outsider("Paul"))
    await owner_chat.say_with_tool(SET, {"successors": []})
    assert "Paul" in (await owner_chat.get("/chat")).text
    handed = await owner_chat.say_with_tool("household_transfer_ownership", {"person": "Alex"})
    assert (await owner_chat.confirm_last(handed)).status == "executed"
    # They are an adult now: the request still waits on their page, without the list in it.
    page = (await owner_chat.get("/chat")).text
    assert "confirmation-successors" not in page and "Paul" not in page


async def test_the_context_says_when_nobody_is_chosen_yet(owner_chat, chat_server):
    await owner_chat.say("hello")
    assert "Successors: none chosen yet." in chat_server.calls[-1].system
    assert "Successors (data" not in chat_server.calls[-1].system


# --- the service ---------------------------------------------------------------------------------


async def test_the_service_keeps_the_list_in_order_and_gives_it_back_as_it_replaces_it(
    session, add_person, clock
):
    await add_person("Owner", "owner")
    alex = await add_person("Alex", "adult")
    sam = await add_person("Sam", "child")
    before, after = await replace_successors(
        session,
        [
            member(sam),
            outsider("Paul", "my brother", "paper_code"),
            member(alex, access="personal_access"),
        ],
        clock(),
    )
    assert before == []
    assert [(s.rank, s.name, s.in_household, s.role) for s in after] == [
        (1, "Sam", True, "child"),
        (2, "Paul", False, None),
        (3, "Alex", True, "adult"),
    ]
    assert (after[1].relation, after[1].access_way) == ("my brother", "paper_code")
    assert after[2].access_way == "personal_access"
    assert all(s.left_on is None and s.deceased_on is None for s in after)
    assert await list_successors(session, clock()) == after
    again_before, again_after = await replace_successors(session, [outsider("Lou")], clock())
    assert again_before == after
    assert [s.name for s in again_after] == ["Lou"]


async def test_as_json_gives_what_the_owner_and_domo_read(session, add_person, clock):
    await add_person("Owner", "owner")
    alex = await add_person("Alex", "adult")
    _, after = await replace_successors(
        session, [member(alex), outsider("Paul", "my brother", "paper_code")], clock()
    )
    assert after[0].as_json() == {
        "rank": 1,
        "name": "Alex",
        "in_household": True,
        "role": "adult",
        "left_on": None,
        "deceased_on": None,
        "relation": None,
        "access_way": None,
    }
    assert after[1].as_json() == {
        "rank": 2,
        "name": "Paul",
        "in_household": False,
        "role": None,
        "left_on": None,
        "deceased_on": None,
        "relation": "my brother",
        "access_way": "paper_code",
    }
    json.dumps([s.as_json() for s in after])


async def test_the_rank_counts_the_living_only_and_a_death_keeps_its_place_in_the_list(
    session, add_person, clock
):
    await add_person("Owner", "owner")
    await replace_successors(session, [outsider("Ann"), outsider("Bob"), outsider("Cy")], clock())
    died = await note_death(session, "ann", clock())
    assert (died.name, died.rank, died.deceased_on) == ("Ann", None, "2030-01-01")
    assert [(s.name, s.rank) for s in await list_successors(session, clock())] == [
        ("Ann", None),
        ("Bob", 1),
        ("Cy", 2),
    ]


async def test_rewriting_the_list_keeps_who_died_by_person_or_by_name(session, add_person, clock):
    await add_person("Owner", "owner")
    alex = await add_person("Alex", "adult")
    await replace_successors(session, [member(alex), outsider("Paul"), outsider("Lou")], clock())
    await note_death(session, "Alex", clock())
    await note_death(session, "PAUL", clock())
    _, after = await replace_successors(
        session, [outsider("Lou"), outsider(" paul "), member(alex), outsider("New")], clock()
    )
    assert [(s.name, s.rank, s.deceased_on) for s in after] == [
        ("Lou", 1, None),
        ("paul", None, "2030-01-01"),
        ("Alex", None, "2030-01-01"),
        ("New", 2, None),
    ]


async def test_someone_who_left_is_told_with_the_day_they_left_and_a_guest_whose_stay_ended_too(
    session, add_person, clock
):
    await add_person("Owner", "owner")
    away = await add_person("Kim", "adult")
    guest = await add_person("Robin", "guest")
    guest.expires_at = clock() + timedelta(days=2)
    await replace_successors(session, [member(away), member(guest)], clock())
    await disable(session, away, clock(), by=away.id)
    clock.advance(timedelta(days=3))
    kim, robin = await list_successors(session, clock())
    assert (kim.in_household, kim.role, kim.left_on) == (False, None, "2030-01-01")
    assert (robin.in_household, robin.role, robin.left_on) == (False, None, "2030-01-03")
    assert (kim.rank, robin.rank) == (1, 2)


async def test_a_renamed_member_is_listed_by_the_name_they_have_now(session, add_person, clock):
    await add_person("Owner", "owner")
    alex = await add_person("Alex", "adult")
    await replace_successors(session, [member(alex)], clock())
    await rename_person(session, alex, "Alexandre", now=clock())
    [shown] = await list_successors(session, clock())
    assert shown.name == "Alexandre"
    assert (await note_death(session, "alexandre", clock())).name == "Alexandre"


async def test_the_service_refuses_what_the_table_would(session, add_person, clock):
    await add_person("Owner", "owner")
    alex = await add_person("Alex", "adult")
    too_many = [outsider(f"P{n}") for n in range(MAX_SUCCESSORS + 1)]
    for entries in (
        too_many,
        [member(alex), member(alex)],
        [outsider("Paul"), outsider(" PAUL")],
        [member(alex), outsider("alex")],
        [SuccessorEntry(None, None, None, None)],
        [SuccessorEntry(alex.id, "Alex", None, None)],
        [outsider("Paul", access="key")],  # type: ignore[arg-type]
        [outsider("P" * 61)],
        [outsider("Paul", "x" * 201)],
    ):
        with pytest.raises(ValueError):
            await replace_successors(session, entries, clock())
    assert await list_successors(session, clock()) == []
    # Ten are fine.
    await replace_successors(session, too_many[:MAX_SUCCESSORS], clock())
    assert len(await list_successors(session, clock())) == MAX_SUCCESSORS


async def test_noting_a_death_finds_a_member_or_an_outsider_by_the_way_names_read(
    session, add_person, clock
):
    await add_person("Owner", "owner")
    alex = await add_person("Alex", "adult")
    await replace_successors(session, [member(alex), outsider("Paul")], clock())
    for who, expected in (("  ALEX ", "Alex"), ("paul", "Paul")):
        assert (await note_death(session, who, clock())).name == expected
    with pytest.raises(InvalidArguments) as nobody:
        await note_death(session, "Zed", clock())
    assert nobody.value.code == "not_found"
    with pytest.raises(InvalidArguments) as twice:
        await note_death(session, "Alex", clock())
    assert twice.value.code == "invalid" and "already" in (twice.value.detail or "")


async def test_undoing_a_death_puts_the_place_back_in_the_running(session, add_person, clock):
    await add_person("Owner", "owner")
    await replace_successors(session, [outsider("Ann"), outsider("Bob"), outsider("Cy")], clock())
    await note_death(session, "ann", clock())
    assert [s.rank for s in await list_successors(session, clock())] == [None, 1, 2]
    back = await undo_death(session, " ANN ", clock())
    # The place is where it was, first, and counts again.
    assert (back.name, back.rank, back.deceased_on) == ("Ann", 1, None)
    assert [(s.name, s.rank) for s in await list_successors(session, clock())] == [
        ("Ann", 1),
        ("Bob", 2),
        ("Cy", 3),
    ]


async def test_undoing_a_death_is_refused_when_it_is_not_noted_or_the_name_is_not_clear(
    session, add_person, clock
):
    await add_person("Owner", "owner")
    alex = await add_person("Alex", "adult")
    await replace_successors(session, [member(alex), outsider("Paul")], clock())
    with pytest.raises(InvalidArguments) as not_noted:
        await undo_death(session, "Alex", clock())
    assert not_noted.value.code == "invalid" and "not noted" in (not_noted.value.detail or "")
    with pytest.raises(InvalidArguments) as nobody:
        await undo_death(session, "Zed", clock())
    assert nobody.value.code == "not_found"
    # Alex is renamed as Paul meanwhile: the name now fits two places.
    await rename_person(session, alex, "Paul", now=clock())
    with pytest.raises(InvalidArguments) as asked:
        await undo_death(session, "paul", clock())
    assert asked.value.code == "ambiguous"


async def test_a_name_that_reads_like_two_on_the_list_is_ambiguous(session, add_person, clock):
    await add_person("Owner", "owner")
    alex = await add_person("Alex", "adult")
    await replace_successors(session, [member(alex), outsider("Paul")], clock())
    # Alex is renamed as Paul meanwhile: the name now fits two entries.
    await rename_person(session, alex, "Paul", now=clock())
    with pytest.raises(InvalidArguments) as asked:
        await note_death(session, "paul", clock())
    assert asked.value.code == "ambiguous"


@pytest.mark.parametrize(
    ("lang", "relation", "expected"),
    [
        (
            "en",
            "my brother",
            "1. Alex (adult) ; 2. Paul (my brother, paper code) ; 3. Lou (personal access) ; "
            "4. Sam (child) ; 5. Kim (left the household on 2030-01-05) ; "
            "Ann (deceased 2030-01-01)",
        ),
    ],
)
def test_the_list_in_words(lang, relation, expected):
    shown = [
        SuccessorShown(1, "Alex", True, "adult", None, None, None, None),
        SuccessorShown(2, "Paul", False, None, None, None, relation, "paper_code"),
        SuccessorShown(3, "Lou", False, None, None, None, None, "personal_access"),
        SuccessorShown(4, "Sam", True, "child", None, None, None, None),
        SuccessorShown(5, "Kim", False, None, "2030-01-05", None, None, None),
        SuccessorShown(None, "Ann", False, None, None, "2030-01-01", None, None),
    ]
    assert successors_in_words(shown, CatalogTexts(lang)) == expected


def test_an_empty_list_is_told():
    assert successors_in_words([], CatalogTexts("en")) == t("successors.none", "en")


# --- the tools, for the barrier ------------------------------------------------------------------


async def test_the_tools_describe_what_they_do_for_the_barrier(
    owner_member, adult_member, session_factory, live_clock
):
    ctx = context(owner_member.person, session_factory, live_clock)
    shown = await TOOLS[SHOW].describe({}, ctx.actor, ctx)
    assert shown == DescribedAction("core", "successors.show", None, "read", "household")
    changed = await TOOLS[SET].describe(
        {"successors": [{"person": "Alex"}, {"name": "Paul"}]}, ctx.actor, ctx
    )
    assert changed == DescribedAction(
        "core", "successors.set", "successors", "permissions", "household", {"count": 2}
    )
    died = await TOOLS[DEATH].describe({"name": " Paul "}, ctx.actor, ctx)
    assert died == DescribedAction(
        "core",
        "successors.note_death",
        "successors",
        "permissions",
        "household",
        {"name": "Paul"},
    )
    with pytest.raises(InvalidArguments):
        await TOOLS[SET].describe({"successors": [{"person": "Nobody"}]}, ctx.actor, ctx)
    with pytest.raises(InvalidArguments):
        await TOOLS[SHOW].describe({"x": 1}, ctx.actor, ctx)
    # For anyone but the owner, no name is looked for: the barrier refuses them the list.
    adult = context(adult_member.person, session_factory, live_clock, role="adult")
    for places in ([{"person": "Alex"}, {"name": "Paul"}], [{"person": "Nobody"}]):
        described = await TOOLS[SET].describe({"successors": places}, adult.actor, adult)
        assert described == DescribedAction(
            "core", "successors.set", "successors", "permissions", "household", {"count": 0}
        )


async def test_undoing_a_death_is_described_by_the_name_alone(
    owner_member, adult_member, session_factory, live_clock
):
    expected = DescribedAction(
        "core",
        "successors.undo_death",
        "successors",
        "permissions",
        "household",
        {"name": "Paul"},
    )
    owners = context(owner_member.person, session_factory, live_clock)
    adults = context(adult_member.person, session_factory, live_clock, role="adult")
    # For anyone but the owner the list is not looked at: the barrier refuses them first, and
    # nothing they are told says whether a name is on it.
    assert await TOOLS[UNDO].describe({"name": " Paul "}, adults.actor, adults) == expected
    # For the owner, only a death that is noted is undone (spec 5.5.3).
    with pytest.raises(InvalidArguments) as nobody:
        await TOOLS[UNDO].describe({"name": " Paul "}, owners.actor, owners)
    assert nobody.value.code == "not_found"
    async with session_factory() as writer:
        await replace_successors(writer, [outsider("Paul")], live_clock())
        await note_death(writer, "Paul", live_clock())
        await writer.commit()
    assert await TOOLS[UNDO].describe({"name": " Paul "}, owners.actor, owners) == expected
    with pytest.raises(InvalidArguments):
        await TOOLS[UNDO].describe({"name": "Paul", "x": 1}, owners.actor, owners)


def test_the_request_is_told_in_one_sentence():
    action = DescribedAction(
        "core", "successors.set", "successors", "permissions", "household", {"count": 1}
    )
    arguments = {"successors": [{"person": "Alex"}]}
    assert (
        describe_for_people(action, arguments, CatalogTexts("en"))
        == "Change the list of successors."
    )
    undo = DescribedAction(
        "core",
        "successors.undo_death",
        "successors",
        "permissions",
        "household",
        {"name": "Alex"},
    )
    assert (
        describe_for_people(undo, {"name": "Alex"}, CatalogTexts("en"))
        == "Put “Alex” back on the list of successors."
    )


async def test_another_member_who_has_the_address_of_a_card_learns_nothing_of_the_list(
    owner_chat, adult_member, put_successors, live_adult_client, confirmation_status
):
    await put_successors(outsider("Paul"))
    reply = await owner_chat.say_with_tool(SET, {"successors": [{"person": "Alex"}]})
    # Another member who has the card's address learns nothing of the list.
    answered = await live_adult_client.post(
        f"/confirmations/{reply.pending_confirmation_id}/approve", headers={"hx-request": "true"}
    )
    card = Card.read(answered.text, reply.pending_confirmation_id)
    assert card.status == "not_yours" and "Paul" not in answered.text
    assert "confirmation-successors" not in answered.text
    assert await confirmation_status(reply.pending_confirmation_id) == "pending"
