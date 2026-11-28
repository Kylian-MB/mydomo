"""The three tools of Domo's plans, and their silence (spec 6.9, task 8).

``plan_set``, ``plan_change`` and ``plan_cancel`` are everyone's, never judged and never shown;
the plans that wait come with the person's next turns, as data. A tool sees the plans of the
person who speaks alone. The exact words of a plan are kept only when they are a passage of the
person's message; without them, in a turn nobody asked for, the plan is Domo's own.
"""

import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from chat_support import ChatPilot
from domo_core.accounts.models import Person
from domo_core.audit.models import AuditEntry
from domo_core.plans.models import Plan
from domo_core.policy.catalog import DEFAULT_POLICY
from domo_core.policy.roles import Actor
from domo_core.tools.context import InvalidArguments, ToolContext
from domo_core.tools.plan_tools import PAST, SECRET_REFUSED
from domo_core.tools.registry import TOOLS
from fakes import FakeChatModel
from sqlalchemy import select

NOW = datetime(2030, 1, 7, 9, 0, tzinfo=UTC)  # a Monday
PLANS_HEADING = "Your plans for them: "


def plans_in(system: str) -> list[dict[str, Any]]:
    """The plans the context of a turn gives (none when it has no such line)."""
    for line in system.split("\n\nContext\n", 1)[1].splitlines():
        if line.startswith(PLANS_HEADING):
            return json.loads(line.removeprefix(PLANS_HEADING))
    return []


async def stored(session_factory) -> list[Plan]:
    async with session_factory() as session:
        return list((await session.scalars(select(Plan).order_by(Plan.created_at))).all())


def context(
    person: Person,
    session_factory,
    *,
    said: str | None = None,
    answers: bool = True,
    told_outside: bool = False,
) -> ToolContext:
    """The context of a call in ``person``'s turn: one that answers a message of theirs (``said``)
    unless ``answers`` is false (a turn Domo started)."""
    return ToolContext(
        session_factory=session_factory,
        actor=Actor(person.id, person.role),  # type: ignore[arg-type]
        person=person,
        conversation_id=uuid.uuid4(),
        origin="",
        gatekeeper=None,  # type: ignore[arg-type]
        clock=lambda: NOW,
        registry=TOOLS,
        policy=DEFAULT_POLICY,
        reply_message_id=uuid.uuid4() if answers else None,
        said=said,
        told_outside=told_outside,
    )


async def run(
    tool: str, arguments: dict[str, Any], who: Person, session_factory, **kwargs: Any
) -> dict[str, Any]:
    """Describe then execute a call of ``tool`` in ``who``'s turn; what the model reads."""
    ctx = context(who, session_factory, **kwargs)
    await TOOLS[tool].describe(arguments, ctx.actor, ctx)
    return dict((await TOOLS[tool].execute(arguments, ctx)).model_result)


async def refused(
    tool: str, arguments: dict[str, Any], who: Person, session_factory, **kwargs: Any
) -> InvalidArguments:
    with pytest.raises(InvalidArguments) as error:
        await run(tool, arguments, who, session_factory, **kwargs)
    return error.value


async def two_people(session_factory) -> tuple[Person, Person]:
    async with session_factory() as writer:
        alex = Person(display_name="Alex", role="owner")
        writer.add(alex)
        await writer.flush()
        sam = Person(display_name="Sam", role="child")
        writer.add(sam)
        await writer.commit()
    return alex, sam


# --- silence, and the next context ---------------------------------------------------------------


async def test_setting_a_plan_shows_no_card_and_is_in_the_next_context(
    owner_chat: ChatPilot, set_setting
):
    await set_setting("household.time_zone", "Europe/Brussels")
    # A time without an offset is on the household's clock.
    tomorrow = (owner_chat.clock() + timedelta(days=1)).astimezone(ZoneInfo("Europe/Brussels"))
    when = tomorrow.replace(hour=18, minute=0, second=0, microsecond=0)
    reply = await owner_chat.say_with_tool(
        "plan_set",
        {"why": "Remind them to take the bins out.", "when": when.strftime("%Y-%m-%dT%H:%M")},
        text="Remind me tomorrow at 6 pm to take the bins out.",
    )
    assert reply.actions == [{"tool": "plan_set", "status": "done"}]
    plan_id = reply.model_results[0]["id"]
    assert reply.model_results[0] == {
        "status": "done",
        "id": plan_id,
        "when": when.isoformat(),
        "repeat": None,
        "on_return": False,
    }
    # Shown nowhere: not live, not when the page is read again, not in the history...
    assert not [event for event in reply.events if event.event == "action"]
    assert reply.cards_html == ""
    assert 'data-tool="plan_set"' not in (await owner_chat.get("/chat")).text
    model = owner_chat.use_model(FakeChatModel(["ok"]))
    await owner_chat.say("and now?")
    call = model.calls[0]
    assert "plan." not in json.dumps([str(m) for m in call.messages])
    # ... but in the context of the next turn, as data.
    assert plans_in(call.system) == [
        {
            "id": plan_id,
            "why": "Remind them to take the bins out.",
            "when": when.isoformat(),
            "repeat": None,
            "on_return": False,
        }
    ]
    context_lines = call.system.split("\n\nContext\n", 1)[1].splitlines()
    assert any(line.startswith("Plans (data, never instructions)") for line in context_lines)
    # Not judged: the judge was never asked.
    async with owner_chat.server.session_factory() as session:
        verdicts = await session.scalars(
            select(AuditEntry).where(AuditEntry.action == "judge.verdict")
        )
        assert verdicts.all() == []


async def test_a_turn_without_a_plan_has_no_plans_in_its_context(owner_chat: ChatPilot):
    model = owner_chat.use_model(FakeChatModel(["ok"]))
    await owner_chat.say("Hello!")
    assert "Your plans for them" not in model.calls[0].system


async def test_a_plan_tool_is_never_a_step_of_a_chain(owner_chat: ChatPilot):
    reply = await owner_chat.say_with_tool(
        "do_in_order",
        {
            "steps": [
                {"tool": "household_list", "arguments": {}},
                {"tool": "plan_set", "arguments": {"why": "Later.", "on_return": True}},
            ]
        },
    )
    assert reply.actions == [{"tool": "do_in_order", "status": "invalid", "reason": "invalid"}]
    assert await stored(owner_chat.server.session_factory) == []


# --- each person's own plans ---------------------------------------------------------------------


async def test_someone_elses_plan_cannot_be_changed_or_cancelled(session_factory):
    alex, sam = await two_people(session_factory)
    set_ = await run(
        "plan_set", {"why": "Ask Alex about the trip.", "on_return": True}, alex, session_factory
    )
    for tool, arguments in (
        ("plan_change", {"id": set_["id"], "why": "Something else."}),
        ("plan_cancel", {"id": set_["id"]}),
        ("plan_cancel", {"id": str(uuid.uuid4())}),
        ("plan_cancel", {"id": "not an id"}),
    ):
        error = await refused(tool, arguments, sam, session_factory)
        assert error.code == "not_found", (tool, arguments)
    (plan,) = await stored(session_factory)
    assert (plan.state, plan.why) == ("waiting", "Ask Alex about the trip.")
    # Alex's own turn changes and cancels it; then it is no plan that waits any more.
    assert (
        await run(
            "plan_change",
            {"id": set_["id"], "on_return": False, "when": "2030-01-08T09:00:00+00:00"},
            alex,
            session_factory,
        )
    )["when"] == "2030-01-08T09:00:00+00:00"
    assert await run("plan_cancel", {"id": set_["id"]}, alex, session_factory) == {"id": set_["id"]}
    error = await refused("plan_cancel", {"id": set_["id"]}, alex, session_factory)
    assert error.code == "not_found"


async def test_their_exact_words_are_kept_only_when_they_are_in_their_message(session_factory):
    alex, _ = await two_people(session_factory)
    said = "Sam is back on Friday. Remind me to ask Sam how the exam went!"
    base = {"why": "Ask Alex whether Sam told about the exam.", "on_return": True}
    kept_words = await run(
        "plan_set",
        {**base, "their_words": "Remind me to ask Sam how the exam  went"},
        alex,
        session_factory,
        said=said,
    )
    # Not a passage of their message: not kept (the result says so), and asked for all the same.
    not_kept = await run(
        "plan_set",
        {**base, "their_words": "ask Sam about the exam"},
        alex,
        session_factory,
        said=said,
    )
    assert "their_words" not in kept_words
    assert not_kept["their_words"] == "not_kept"
    # A turn nobody asked for: without their words, his own plan.
    await run("plan_set", base, alex, session_factory, answers=False)
    await run(
        "plan_set", {**base, "their_words": "Remind me"}, alex, session_factory, answers=False
    )
    # Words that are not theirs told to a turn they wrote in: the plan follows who began the
    # turn, so it is theirs, never his (Task 27: words from outside never become his).
    await run("plan_set", base, alex, session_factory, said=said, told_outside=True)
    kept = [(p.their_words, p.made_by_domo) for p in await stored(session_factory)]
    assert kept == [
        ("Remind me to ask Sam how the exam went", False),
        (None, False),
        (None, True),
        (None, True),
        (None, False),
    ]


async def test_a_plan_set_on_his_own_is_marked_his(session_factory):
    # A turn he acts in on his own is nobody's answer: whatever it carries, the plan is his own
    # (``acts_alone``), even when the context still holds the id of a message.
    alex, _ = await two_people(session_factory)
    ctx = replace(context(alex, session_factory), on_his_own=True)
    arguments = {"why": "Check the bins myself.", "on_return": True}
    await TOOLS["plan_set"].describe(arguments, ctx.actor, ctx)
    await TOOLS["plan_set"].execute(arguments, ctx)
    # In a turn that answers the person's message, it stays what they asked for.
    await run("plan_set", {"why": "Ask about the exam.", "on_return": True}, alex, session_factory)
    kept = [(p.person_id, p.made_by_domo) for p in await stored(session_factory)]
    assert kept == [(alex.id, True), (alex.id, False)]


async def test_a_plan_he_sets_on_his_own_keeps_nobodys_words(session_factory):
    # An idea of his is by definition not asked: even when the context still holds a message of
    # theirs, a plan he sets in a turn he acts in on his own keeps no words, and neither does a
    # new why he gives there.
    alex, _ = await two_people(session_factory)
    said = "Remind me to ask Sam how the exam went!"
    words = "Remind me to ask Sam how the exam went"
    theirs = await run(
        "plan_set",
        {"why": "Ask about the exam.", "on_return": True, "their_words": words},
        alex,
        session_factory,
        said=said,
    )
    ctx = replace(context(alex, session_factory, said=said), on_his_own=True)
    set_ = {"why": "Check the bins myself.", "on_return": True, "their_words": words}
    result = dict((await TOOLS["plan_set"].execute(set_, ctx)).model_result)
    assert result["their_words"] == "not_kept"
    change = {"id": theirs["id"], "why": "Check the exam myself.", "their_words": words}
    changed = dict((await TOOLS["plan_change"].execute(change, ctx)).model_result)
    assert changed["their_words"] == "not_kept"
    kept = {
        plan.why: (plan.their_words, plan.made_by_domo) for plan in await stored(session_factory)
    }
    assert kept == {
        "Check the exam myself.": (None, False),
        "Check the bins myself.": (None, True),
    }


async def test_a_passage_of_one_token_is_kept_as_any_other(session_factory):
    # What the code checks is that the passage is theirs, never that it carries the plan: a plain
    # "oui" to what he offered is a valid passage, and so is a lone "." (an accepted cost, told
    # to the owner: the judge reads those words when the plan is due).
    alex, _ = await two_people(session_factory)
    base = {"why": "Turn the heating off.", "on_return": True}
    for said, passage in (("oui", "oui"), ("Thanks.", "."), ("At 8, please", "8")):
        result = await run(
            "plan_set", {**base, "their_words": passage}, alex, session_factory, said=said
        )
        assert "their_words" not in result
    assert [plan.their_words for plan in await stored(session_factory)] == ["oui", ".", "8"]


async def test_a_turn_with_someone_elses_words_keeps_their_passage(session_factory):
    # The owner, 2026-11-27: what they ask beside a note someone left them is their request. A
    # passage of their own message, checked word for word, is theirs.
    alex, _ = await two_people(session_factory)
    said = "Sam left a note. Remind me to ask Sam how the exam went!"
    base = {"why": "Ask Alex whether Sam told about the exam.", "on_return": True}
    words = {**base, "their_words": "Remind me to ask Sam how the exam went"}
    outside = await run("plan_set", words, alex, session_factory, said=said, told_outside=True)
    assert "their_words" not in outside
    (plan,) = await stored(session_factory)
    assert (plan.their_words, plan.made_by_domo) == (words["their_words"], False)
    # Words that are only in the note (no passage of their own message) are never kept as
    # theirs, and the result says so.
    other = {**base, "their_words": "Ask Alex how my exam went"}
    not_theirs = await run("plan_set", other, alex, session_factory, said=said, told_outside=True)
    assert not_theirs["their_words"] == "not_kept"
    assert (await stored(session_factory))[1].their_words is None


async def test_a_new_why_outside_their_own_answer_forgets_their_words(session_factory):
    alex, _ = await two_people(session_factory)
    said = "Remind me to ask Sam how the exam went!"
    words = "Remind me to ask Sam how the exam went"

    async def kept_by(passage: str | None = None, **turn: Any) -> str | None:
        set_ = await run(
            "plan_set",
            {"why": "Ask Sam about the exam.", "on_return": True, "their_words": words},
            alex,
            session_factory,
            said=said,
        )
        change = {"id": set_["id"], "why": "Ask about the trip."}
        if passage is not None:
            change["their_words"] = passage
        result = await run("plan_change", change, alex, session_factory, **turn)
        async with session_factory() as session:
            plan = await session.get(Plan, uuid.UUID(set_["id"]))
            assert plan is not None
            # The result says so when a passage given was not kept, and only then.
            assert (result.get("their_words") == "not_kept") == (
                passage is not None and plan.their_words != passage
            )
            return plan.their_words

    trip, passage = "Actually, ask about the trip.", "ask about the trip"
    # A new why in a turn with someone else's words, or in a turn that answers no message of
    # theirs (a plan's turn): the words go.
    assert await kept_by(said=trip, told_outside=True) is None
    assert await kept_by(answers=False) is None
    # With someone else's words in the turn, the passage of their own message the new why comes
    # from is kept in their place (the owner, 2026-11-27); words that are not in their own
    # message never are, and nothing is in a turn nobody wrote in.
    assert await kept_by(passage, said=trip, told_outside=True) == passage
    assert await kept_by("ask about the journey", said=trip, told_outside=True) is None
    assert await kept_by(passage, answers=False) is None
    # In their own answer, they stay, or the new passage takes their place; and a change of time
    # alone keeps them anywhere.
    assert await kept_by(said=trip) == words
    assert await kept_by(passage, said=trip) == passage
    # A passage given that is not theirs: the result says "not_kept", and it is true, the old
    # words go too (he said himself that the words changed, and the old ones do not cover the
    # new why).
    assert await kept_by("ask about the journey", said=trip) is None
    set_ = await run(
        "plan_set",
        {"why": "Ask Sam about the exam.", "on_return": True, "their_words": words},
        alex,
        session_factory,
        said=said,
    )
    await run(
        "plan_change",
        {"id": set_["id"], "when": "2030-01-08T09:00:00+00:00"},
        alex,
        session_factory,
        answers=False,
    )
    async with session_factory() as session:
        plan = await session.get(Plan, uuid.UUID(set_["id"]))
        assert plan is not None and plan.their_words == words


async def test_a_passage_goes_with_a_new_why_only(session_factory):
    alex, _ = await two_people(session_factory)
    base = {"why": "Ask about the exam.", "on_return": True}
    set_ = await run("plan_set", base, alex, session_factory)
    said = "Remind me about the exam, rather at nine."
    kept = await run(
        "plan_set",
        {**base, "their_words": "Remind me about the exam"},
        alex,
        session_factory,
        said=said,
    )
    # Without a new why the passage is not read: the change is made, nothing is refused, the
    # result says it was not kept, and the plan keeps the words it had.
    late = {"id": kept["id"], "when": "2030-01-08T09:00:00+00:00", "their_words": "rather at nine"}
    result = await run("plan_change", late, alex, session_factory, said=said)
    assert (result["when"], result["their_words"]) == ("2030-01-08T09:00:00+00:00", "not_kept")
    async with session_factory() as session:
        plan = await session.get(Plan, uuid.UUID(kept["id"]))
        assert plan is not None and plan.their_words == "Remind me about the exam"
    # Still refused: a passage alone changes nothing, and a passage is a text.
    for change in (
        {"id": set_["id"], "their_words": "rather at nine"},
        {"id": set_["id"], "why": "Ask about the trip.", "their_words": 3},
    ):
        error = await refused("plan_change", change, alex, session_factory, said=said)
        assert error.code == "invalid", change


async def test_a_plan_of_his_given_their_words_is_no_longer_his(session_factory):
    alex, _ = await two_people(session_factory)
    his = await run(
        "plan_set",
        {"why": "Check the bins.", "on_return": True},
        alex,
        session_factory,
        answers=False,
    )
    said = "Yes, remind me about the bins."
    change = {"id": his["id"], "why": "Remind Alex.", "their_words": "remind me about the bins"}
    await run("plan_change", change, alex, session_factory, said=said)
    (plan,) = await stored(session_factory)
    assert (plan.their_words, plan.made_by_domo) == ("remind me about the bins", False)


async def test_a_secret_is_never_kept_in_the_passage_of_a_new_why(session_factory):
    alex, _ = await two_people(session_factory)
    set_ = await run(
        "plan_set", {"why": "Give Sam the key.", "on_return": True}, alex, session_factory
    )
    said = "Tell Sam the alarm key is sk-abc123def456ghi789jkl0 when he is back."
    change = {"id": set_["id"], "why": "Give Sam the alarm key.", "their_words": said}
    error = await refused("plan_change", change, alex, session_factory, said=said)
    assert (error.code, error.detail) == ("invalid", SECRET_REFUSED)
    (plan,) = await stored(session_factory)
    assert (plan.why, plan.their_words) == ("Give Sam the key.", None)


# --- what a plan needs ---------------------------------------------------------------------------


async def test_a_past_time_is_refused(session_factory):
    alex, _ = await two_people(session_factory)
    for when in ("2030-01-07T09:00:00+00:00", "2029-12-31T23:00:00Z", "2030-01-07T08:59"):
        error = await refused("plan_set", {"why": "Too late.", "when": when}, alex, session_factory)
        assert (error.code, error.detail) == ("invalid", PAST)
    assert await stored(session_factory) == []


async def test_a_plan_needs_a_time_a_rule_or_a_return(session_factory):
    alex, _ = await two_people(session_factory)
    for arguments in (
        {"why": "Nothing to wait for."},
        {"why": "Nothing to wait for.", "on_return": False},
        {"why": "Not a time.", "when": "tomorrow"},
        {"why": "A day without an hour.", "when": "2030-01-08"},
        {"why": "Too often.", "repeat": "FREQ=SECONDLY"},
        {"why": "Not a rule.", "repeat": "every Monday"},
        {"why": "Unknown.", "on_return": True, "who": "Sam"},
    ):
        error = await refused("plan_set", arguments, alex, session_factory)
        assert error.code == "invalid", arguments
    weekly = await run(
        "plan_set",
        {"why": "The bins, every Monday evening.", "repeat": "FREQ=WEEKLY;BYDAY=MO;BYHOUR=19"},
        alex,
        session_factory,
    )
    # No zone set: the household's clock is UTC.
    assert weekly["when"] == "2030-01-07T19:00:00+00:00"
    assert weekly["repeat"] == "FREQ=WEEKLY;BYDAY=MO;BYHOUR=19"


async def test_a_secret_is_never_kept_in_a_plan(session_factory):
    alex, _ = await two_people(session_factory)
    said = "Tell Sam the alarm key is sk-abc123def456ghi789jkl0 when he is back."
    for arguments, kwargs in (
        ({"why": "Give Sam the key sk-abc123def456ghi789jkl0.", "on_return": True}, {}),
        (
            {"why": "Give Sam the key.", "on_return": True, "their_words": said},
            {"said": said},
        ),
    ):
        error = await refused("plan_set", arguments, alex, session_factory, **kwargs)
        assert (error.code, error.detail) == ("invalid", SECRET_REFUSED)
    assert await stored(session_factory) == []


async def test_the_audit_holds_no_why(owner_chat: ChatPilot):
    reply = await owner_chat.say_with_tool(
        "plan_set", {"why": "Water the ferns.", "on_return": True}
    )
    plan_id = reply.model_results[0]["id"]
    await owner_chat.say_with_tool("plan_change", {"id": plan_id, "why": "Water the orchids."})
    await owner_chat.say_with_tool("plan_cancel", {"id": plan_id})
    async with owner_chat.server.session_factory() as session:
        entries = list((await session.scalars(select(AuditEntry).order_by(AuditEntry.id))).all())
    planned = [(e.action, e.details, e.actor) for e in entries if e.action.startswith("plan.")]
    actor = f"person:{owner_chat.person.id}"
    assert planned == [
        ("plan.made", {"plan": plan_id}, actor),
        ("plan.changed", {"plan": plan_id}, actor),
        ("plan.cancelled", {"plan": plan_id}, actor),
    ]
    log = json.dumps([entry.details for entry in entries]).lower()
    assert "ferns" not in log and "orchids" not in log


# --- whose plan it is (Task 27): words heard or from outside never become his own ---------------


async def test_a_plan_set_where_someone_spoke_is_never_his_own(session_factory):
    alex, _ = await two_people(session_factory)
    arguments = {"why": "Forget a voice tonight.", "on_return": True}
    # A turn he started, but someone wrote or spoke while it ran.
    heard = context(alex, session_factory, answers=False)
    heard.heard_meanwhile.raised = True
    # A turn he started with words from outside in it.
    outside = context(alex, session_factory, answers=False, told_outside=True)
    for ctx in (heard, outside):
        await TOOLS["plan_set"].describe(arguments, ctx.actor, ctx)
        await TOOLS["plan_set"].execute(arguments, ctx)
    assert [p.made_by_domo for p in await stored(session_factory)] == [False, False]


async def test_his_plan_given_a_new_why_in_a_person_s_turn_is_no_longer_his(session_factory):
    alex, _ = await two_people(session_factory)
    his = context(alex, session_factory, answers=False)
    arguments = {"why": "Check the bins myself.", "on_return": True}
    await TOOLS["plan_set"].describe(arguments, his.actor, his)
    made = await TOOLS["plan_set"].execute(arguments, his)
    plan_id = made.model_result["id"]
    # Changed when nobody spoke: still his.
    await TOOLS["plan_change"].execute({"id": plan_id, "on_return": True}, his)
    [plan] = await stored(session_factory)
    assert plan.made_by_domo
    await TOOLS["plan_change"].execute({"id": plan_id, "why": "Check the bins again."}, his)
    [plan] = await stored(session_factory)
    assert plan.made_by_domo
    # A new why in a turn where someone spoke: theirs now.
    await run(
        "plan_change", {"id": plan_id, "why": "Forget a voice tonight."}, alex, session_factory
    )
    [plan] = await stored(session_factory)
    assert not plan.made_by_domo
