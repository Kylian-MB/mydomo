"""``trust_note``, ``trust_look`` and ``trust_correct``: Domo notes what a person's word was
worth, looks at a score with its reasons and corrects a fact (trust per person, spec 6.3 and 6.5,
task 2).

The code keeps anyone from lowering someone else's score by talk, or raising their own: no fact in
a turn that does not answer the person who speaks, or that carries someone else's words; no fact
about the person who speaks; a confirmed fact only from what was said about the person who speaks;
a reported fact keeps who reported it.
"""

import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from chat_support import ChatPilot
from domo_core.accounts.models import Person
from domo_core.audit.models import AuditEntry
from domo_core.db import SessionFactory
from domo_core.llm.choices import HOUSEHOLD_SCOPE, Choices, save_choices
from domo_core.llm.models import ModelLesson
from domo_core.llm.types import ToolCall
from domo_core.memory.models import Memory
from domo_core.memory.service import keep
from domo_core.policy.catalog import DEFAULT_POLICY, is_silent, needs_the_judge
from domo_core.policy.roles import Actor
from domo_core.tools.context import InvalidArguments, NamesPeople, ToolContext
from domo_core.tools.registry import TOOLS
from domo_core.trust import service
from domo_core.trust.models import TrustFact
from fakes import FakeChatModel
from sqlalchemy import select

NOW = datetime(2030, 1, 7, 9, 0, tzinfo=UTC)
TRUST_TOOLS = ("trust_note", "trust_look", "trust_correct")


def context(person: Person, session_factory: SessionFactory) -> ToolContext:
    """A turn that answers a message of ``person``, with nothing from outside."""
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
        reply_message_id=uuid.uuid4(),
    )


async def run(name: str, ctx: ToolContext, **arguments: Any) -> dict[str, Any]:
    """Describe then execute ``name`` in ``ctx``; what the model reads."""
    tool = TOOLS[name]
    await tool.describe(arguments, ctx.actor, ctx)
    return dict((await tool.execute(arguments, ctx)).model_result)


async def refusal(name: str, ctx: ToolContext, **arguments: Any) -> InvalidArguments:
    """The refusal of ``name`` in ``ctx``, the same at describe and at execute."""
    tool = TOOLS[name]
    with pytest.raises(InvalidArguments) as described:
        await tool.describe(arguments, ctx.actor, ctx)
    with pytest.raises(InvalidArguments) as executed:
        await tool.execute(arguments, ctx)
    assert described.value.code == executed.value.code
    return executed.value


async def household(session_factory: SessionFactory) -> tuple[Person, Person, Person]:
    async with session_factory() as writer:
        alex = Person(display_name="Alex", role="owner")
        sam = Person(display_name="Sam Martin", role="adult")
        dana = Person(display_name="Dana", role="child")
        writer.add_all([alex, sam, dana])
        await writer.commit()
    return alex, sam, dana


async def said(
    session_factory: SessionFactory, by: Person, about: Person, content: str
) -> uuid.UUID:
    """A memory of what ``by`` said about ``about``: its id."""
    async with session_factory() as session:
        memory = await keep(
            session,
            said_by=by.id,
            subject=about.display_name,
            content=content,
            kind="preference",
            origin="own_words",
            kept_private=False,
            core=False,
            now=NOW,
            about_id=about.id,
        )
        await session.commit()
        return memory.id


async def facts(session_factory: SessionFactory) -> list[TrustFact]:
    async with session_factory() as session:
        return list((await session.scalars(select(TrustFact))).all())


async def audit_entries(session_factory: SessionFactory, action: str) -> list[AuditEntry]:
    async with session_factory() as session:
        found = await session.scalars(select(AuditEntry).where(AuditEntry.action == action))
        return list(found.all())


ABOUT = "Sam said Alex hates spinach; Alex says he loves it."


def call(name: str, arguments: dict[str, object], call_id: str) -> ToolCall:
    return ToolCall(call_id, name, json.dumps(arguments))


def denial(memory: uuid.UUID) -> dict[str, object]:
    # A first name alone finds Sam (``NamesPeople``).
    return {
        "person": "Sam",
        "outcome": "mistaken",
        "how": "confirmed",
        "about": ABOUT,
        "memory": str(memory),
    }


async def test_a_denial_by_the_person_concerned_notes_a_fact_about_who_said_it(
    owner_chat: ChatPilot, add_member
):
    sam = await add_member("Sam Martin", "adult")
    alex = owner_chat.person
    session_factory = owner_chat.server.session_factory
    memory = await said(session_factory, sam, alex, "Alex hates spinach.")
    # The fact first, then the memory confirmed (here: corrected, in Alex's words).
    owner_chat.use_model(
        FakeChatModel(
            tool_script=[
                call("trust_note", denial(memory), "c1"),
                call(
                    "memory_change",
                    {"id": str(memory), "content": "Alex loves spinach."},
                    "c2",
                ),
                "Noted.",
            ]
        )
    )
    reply = await owner_chat.say("That's wrong, I love spinach.")
    assert reply.actions == [
        {"tool": "trust_note", "status": "done"},
        {"tool": "memory_change", "status": "done"},
    ]
    (fact,) = await facts(session_factory)
    assert (fact.person_id, fact.outcome, fact.established) == (sam.id, "mistaken", "confirmed")
    assert (fact.memory_id, fact.noted_for_id, fact.reported_by_id) == (memory, alex.id, None)
    assert fact.about == ABOUT
    async with session_factory() as session:
        changed = await session.get(Memory, memory)
        assert changed is not None and changed.confirmed_at is not None


async def test_a_memory_confirmed_first_can_no_longer_be_noted(owner_chat: ChatPilot, add_member):
    sam = await add_member("Sam Martin", "adult")
    session_factory = owner_chat.server.session_factory
    memory = await said(session_factory, sam, owner_chat.person, "Alex hates spinach.")
    owner_chat.use_model(
        FakeChatModel(
            tool_script=[
                call("memory_change", {"id": str(memory), "confirmed": True}, "c1"),
                call("trust_note", denial(memory), "c2"),
                "Noted.",
            ]
        )
    )
    reply = await owner_chat.say("That's wrong, I love spinach.")
    assert reply.actions[0] == {"tool": "memory_change", "status": "done"}
    assert reply.actions[1]["tool"] == "trust_note"
    assert reply.actions[1]["status"] == "invalid"
    assert "already confirmed" in reply.model_result_text
    assert await facts(session_factory) == []


async def test_a_memory_gives_one_confirmed_fact(session_factory: SessionFactory):
    alex, sam, _ = await household(session_factory)
    memory = await said(session_factory, sam, alex, "Alex hates spinach.")
    ctx = context(alex, session_factory)
    note = {"person": "Sam", "outcome": "lied", "how": "confirmed", "about": "Sam lied."}
    await run("trust_note", ctx, **note, memory=str(memory))
    async with session_factory() as session:
        once = await service.trust_of(session, sam.id, start=70)
    for _ in range(3):
        refused = await refusal("trust_note", ctx, **note, memory=str(memory))
        assert refused.code == "not_possible"
        assert "trust_correct" in (refused.detail or "")
    async with session_factory() as session:
        assert await service.trust_of(session, sam.id, start=70) == once
    assert once.facts == 1


async def test_a_memory_goes_with_a_confirmed_fact_only(session_factory: SessionFactory):
    alex, sam, _ = await household(session_factory)
    memory = await said(session_factory, sam, alex, "Alex hates spinach.")
    ctx = context(alex, session_factory)
    for how in ("seen", "reported"):
        refused = await refusal(
            "trust_note",
            ctx,
            person="Sam",
            outcome="lied",
            how=how,
            about="Sam lied.",
            memory=str(memory),
        )
        assert refused.code == "invalid", how
    assert await facts(session_factory) == []


async def test_trust_note_is_no_step_of_a_chain(owner_chat: ChatPilot, add_member):
    await add_member("Sam Martin", "adult")
    note = {"person": "Sam", "outcome": "lied", "how": "reported", "about": "Alex says so."}
    reply = await owner_chat.say_with_tool(
        "do_in_order", {"steps": [{"tool": "trust_note", "arguments": note}]}
    )
    assert reply.actions[0]["tool"] == "do_in_order"
    assert reply.actions[0]["status"] == "invalid"
    assert await facts(owner_chat.server.session_factory) == []


async def test_nobody_notes_a_fact_about_themselves(session_factory: SessionFactory):
    alex, sam, _ = await household(session_factory)
    memory = await said(session_factory, sam, alex, "Alex hates spinach.")
    ctx = context(sam, session_factory)
    for how, extra in (("seen", {}), ("reported", {}), ("confirmed", {"memory": str(memory)})):
        refused = await refusal(
            "trust_note", ctx, person="Sam", outcome="held", how=how, about="I was right.", **extra
        )
        assert refused.code == "not_possible", how
        assert "themselves" in (refused.detail or "")
    assert await facts(session_factory) == []


async def test_no_fact_in_a_turn_with_someone_elses_words(session_factory: SessionFactory):
    # A note left in Sam's turn that says "Alex lied": no fact, whatever Domo is told.
    alex, sam, _ = await household(session_factory)
    ctx = replace(context(sam, session_factory), told_outside=True)
    for how in ("seen", "reported"):
        refused = await refusal(
            "trust_note", ctx, person="Alex", outcome="lied", how=how, about="Alex lied to Sam."
        )
        assert refused.code == "not_possible"
        assert "nothing was noted" in (refused.detail or "")
    assert await facts(session_factory) == []


async def test_no_fact_in_a_turn_domo_started(session_factory: SessionFactory):
    alex, sam, _ = await household(session_factory)
    ctx = replace(context(sam, session_factory), reply_message_id=None)
    refused = await refusal(
        "trust_note", ctx, person="Alex", outcome="held", how="seen", about="The boiler was off."
    )
    assert refused.code == "not_possible"
    # Correcting one is refused there too.
    async with session_factory() as session:
        fact = await service.note(
            session, person_id=alex.id, outcome="lied", established="seen", about="x", now=NOW
        )
        await session.commit()
    assert (await refusal("trust_correct", ctx, id=str(fact.id))).code == "not_possible"
    told = replace(context(sam, session_factory), told_outside=True)
    assert (await refusal("trust_correct", told, id=str(fact.id))).code == "not_possible"
    for outcome in ("mistaken", "held"):
        refused = await refusal("trust_correct", told, id=str(fact.id), outcome=outcome)
        assert refused.code == "not_possible", outcome
    (kept,) = await facts(session_factory)
    assert kept.outcome == "lied"
    assert len(await facts(session_factory)) == 1


async def test_confirmed_needs_the_memory_and_the_person_it_is_about(
    session_factory: SessionFactory,
):
    alex, sam, dana = await household(session_factory)
    about_alex = await said(session_factory, sam, alex, "Alex hates spinach.")
    about_dana = await said(session_factory, sam, dana, "Dana broke the vase.")
    by_dana = await said(session_factory, dana, alex, "Alex sings badly.")
    ctx = context(alex, session_factory)
    note = {"person": "Sam", "outcome": "lied", "how": "confirmed", "about": "Sam lied."}
    # Without the memory.
    assert (await refusal("trust_note", ctx, **note)).code == "invalid"
    # About someone else than the one who speaks: only Dana confirms or denies it.
    assert (await refusal("trust_note", ctx, **note, memory=str(about_dana))).code == "not_possible"
    # Said by someone else than the person the fact is about.
    assert (await refusal("trust_note", ctx, **note, memory=str(by_dana))).code == "not_possible"
    # No memory has this id.
    assert (await refusal("trust_note", ctx, **note, memory=str(uuid.uuid4()))).code == (
        "not_found"
    )
    assert await facts(session_factory) == []
    # A wrong value is refused cleanly, before the database.
    for wrong in ({"outcome": "liar"}, {"how": "heard"}):
        refused = await refusal("trust_note", ctx, **{**note, **wrong}, memory=str(about_alex))
        assert refused.code == "invalid"
    assert await run("trust_note", ctx, **note, memory=str(about_alex))
    (fact,) = await facts(session_factory)
    assert (fact.person_id, fact.established, fact.memory_id) == (sam.id, "confirmed", about_alex)


async def test_a_reported_fact_keeps_who_reported_it(session_factory: SessionFactory):
    # "Sam lied to Dana", said by Alex in his own turn: Alex's word, kept with it.
    alex, sam, _ = await household(session_factory)
    ctx = context(alex, session_factory)
    result = await run(
        "trust_note", ctx, person="Sam", outcome="lied", how="reported", about="Alex says so."
    )
    (fact,) = await facts(session_factory)
    assert result == {"id": str(fact.id)}
    assert (fact.person_id, fact.established) == (sam.id, "reported")
    assert (fact.reported_by_id, fact.noted_for_id) == (alex.id, alex.id)
    # And never a secret in what it says.
    refused = await refusal(
        "trust_note",
        ctx,
        person="Sam",
        outcome="lied",
        how="reported",
        about="Sam gave the code sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
    )
    assert refused.code == "invalid" and "secret" in (refused.detail or "")
    assert len(await facts(session_factory)) == 1


async def test_looking_at_ones_own_score_gives_the_reasons(session_factory: SessionFactory):
    alex, sam, _ = await household(session_factory)
    async with session_factory() as session:
        # The household chose where trust starts.
        await save_choices(
            session,
            HOUSEHOLD_SCOPE,
            Choices(trust_start_percent=60),
            by=alex.id,
            why="test",
            now=NOW,
        )
        await session.commit()
    ctx = context(sam, session_factory)
    assert await run("trust_look", ctx) == {"percent": 60, "facts": 0, "recent": []}
    async with session_factory() as session:
        for i in range(12):
            await service.note(
                session,
                person_id=sam.id,
                outcome="held",
                established="seen",
                about=f"Fact {i}.",
                now=NOW.replace(minute=i),
            )
        await session.commit()
    result = await run("trust_look", ctx)
    async with session_factory() as session:
        expected = await service.trust_of(session, sam.id, start=60)
    assert (result["percent"], result["facts"]) == (expected.percent, 12)
    assert len(result["recent"]) == 10
    newest = result["recent"][0]
    assert set(newest) == {"id", "outcome", "how", "about", "when"}
    assert (newest["outcome"], newest["how"], newest["about"]) == ("held", "seen", "Fact 11.")
    assert newest["when"] == NOW.replace(minute=11).isoformat()
    # Naming oneself is naming nobody, and leaves nothing in the audit log.
    assert await run("trust_look", ctx, person="Sam") == result
    assert await audit_entries(session_factory, "trust.looked") == []


async def test_looking_at_someone_elses_is_in_the_audit_without_content(
    session_factory: SessionFactory,
):
    alex, sam, _ = await household(session_factory)
    async with session_factory() as session:
        await service.note(
            session,
            person_id=sam.id,
            outcome="lied",
            established="seen",
            about="Sam hid the cake.",
            now=NOW,
        )
        await session.commit()
    result = await run("trust_look", context(alex, session_factory), person="Sam")
    assert result["facts"] == 1 and result["recent"][0]["about"] == "Sam hid the cake."
    (entry,) = await audit_entries(session_factory, "trust.looked")
    assert entry.actor == f"person:{alex.id}"
    assert entry.details == {"about": str(sam.id), "count": 1}
    assert "cake" not in json.dumps(entry.details)
    description = TOOLS["trust_look"].definition.description
    assert "judge whether the person asking should know it" in description


async def test_a_fact_is_corrected_or_taken_back(session_factory: SessionFactory):
    alex, sam, _ = await household(session_factory)
    async with session_factory() as session:
        fact = await service.note(
            session, person_id=sam.id, outcome="lied", established="seen", about="x", now=NOW
        )
        await session.commit()
    ctx = context(alex, session_factory)
    assert await run("trust_correct", ctx, id=str(fact.id), outcome="mistaken") == {
        "id": str(fact.id),
        "outcome": "mistaken",
    }
    (changed,) = await facts(session_factory)
    assert changed.outcome == "mistaken"
    # The same outcome changes nothing; a wrong one, or an unknown id, is refused cleanly.
    assert (await refusal("trust_correct", ctx, id=str(fact.id), outcome="mistaken")).code == (
        "not_possible"
    )
    assert (await refusal("trust_correct", ctx, id=str(fact.id), outcome="liar")).code == (
        "invalid"
    )
    assert (await refusal("trust_correct", ctx, id=str(uuid.uuid4()))).code == "not_found"
    assert (await refusal("trust_correct", ctx, id="nope")).code == "not_found"
    assert await run("trust_correct", ctx, id=str(fact.id)) == {"taken_back": True}
    assert await facts(session_factory) == []
    entries = await audit_entries(session_factory, "trust.corrected")
    assert [entry.details for entry in entries] == [
        {"fact": str(fact.id), "about": str(sam.id)}
    ] * 2
    assert all(entry.actor == f"person:{alex.id}" for entry in entries)


async def test_the_trust_tools_show_no_card(owner_chat: ChatPilot, add_member):
    await add_member("Sam Martin", "adult")
    for name, arguments in (
        ("trust_note", {"person": "Sam", "outcome": "held", "how": "seen", "about": "It held."}),
        ("trust_look", {"person": "Sam"}),
    ):
        reply = await owner_chat.say_with_tool(name, arguments)
        assert reply.actions == [{"tool": name, "status": "done"}], name
        assert reply.cards_html == "", name
        assert f'data-tool="{name}"' not in (await owner_chat.get("/chat")).text


async def test_the_trust_tools_are_not_judged(owner_chat: ChatPilot, add_member):
    for name in TRUST_TOOLS:
        tool = TOOLS[name]
        assert tool.action_type == name.replace("_", ".")
        assert is_silent(tool.action_type) and not needs_the_judge(tool.action_type)
        for role in ("owner", "adult", "child", "guest"):
            assert tool.action_type in DEFAULT_POLICY.allowed[role]  # type: ignore[index]
    for name in ("trust_note", "trust_look"):
        tool = TOOLS[name]
        assert isinstance(tool, NamesPeople) and tool.person_arguments == ("person",)
    # A judge that would ask for a yes is never asked: the fact is noted at once.
    await add_member("Sam Martin", "adult")
    reply = await owner_chat.say_with_tool(
        "trust_note",
        {"person": "Sam", "outcome": "lied", "how": "reported", "about": "Alex says so."},
        judge="yes",
    )
    assert reply.actions == [{"tool": "trust_note", "status": "done"}]
    assert len(await facts(owner_chat.server.session_factory)) == 1


async def test_in_their_own_turn_a_lie_only_becomes_a_mistake(session_factory: SessionFactory):
    alex, sam, _ = await household(session_factory)

    async def fact_of(outcome: str) -> uuid.UUID:
        async with session_factory() as session:
            fact = await service.note(
                session,
                person_id=sam.id,
                outcome=outcome,  # type: ignore[arg-type]
                established="seen",
                about="x",
                now=NOW,
            )
            await session.commit()
            return fact.id

    theirs = context(sam, session_factory)
    lied, mistaken = await fact_of("lied"), await fact_of("mistaken")
    for fact_id, outcome in ((lied, None), (lied, "held"), (mistaken, "held"), (mistaken, None)):
        given = {} if outcome is None else {"outcome": outcome}
        refused = await refusal("trust_correct", theirs, id=str(fact_id), **given)
        assert refused.code == "not_possible", (outcome, given)
        assert "someone else speaks" in (refused.detail or "")
    assert sorted(f.outcome for f in await facts(session_factory)) == ["lied", "mistaken"]
    assert await audit_entries(session_factory, "trust.corrected") == []
    # Lied to mistaken passes in their own turn.
    assert await run("trust_correct", theirs, id=str(lied), outcome="mistaken")
    # In Alex's turn, all of them pass.
    other = context(alex, session_factory)
    assert await run("trust_correct", other, id=str(lied), outcome="held")
    assert await run("trust_correct", other, id=str(mistaken), outcome="held")
    assert await run("trust_correct", other, id=str(lied))
    another = await fact_of("lied")
    assert await run("trust_correct", other, id=str(another), outcome="mistaken")
    assert len(await audit_entries(session_factory, "trust.corrected")) == 5


async def test_a_fact_is_made_worse_only_by_whoever_established_it(
    session_factory: SessionFactory,
):
    # Final review of 0.9.1, I1: Dana cannot turn what Alex established about Sam into a lie.
    alex, sam, dana = await household(session_factory)

    async def fact_of(outcome: str, how: str, by: Person) -> uuid.UUID:
        async with session_factory() as session:
            fact = await service.note(
                session,
                person_id=sam.id,
                outcome=outcome,  # type: ignore[arg-type]
                established=how,  # type: ignore[arg-type]
                about="x",
                now=NOW,
                noted_for_id=by.id,
                reported_by_id=by.id if how == "reported" else None,
            )
            await session.commit()
            return fact.id

    danas = context(dana, session_factory)
    held = await fact_of("held", "confirmed", alex)
    mistaken = await fact_of("mistaken", "seen", alex)
    reported = await fact_of("held", "reported", alex)
    for fact_id, outcome in (
        (held, "lied"),
        (held, "mistaken"),
        (mistaken, "lied"),
        (reported, "lied"),
    ):
        refused = await refusal("trust_correct", danas, id=str(fact_id), outcome=outcome)
        assert refused.code == "not_possible", (fact_id, outcome)
        assert "theirs to report" in (refused.detail or "")
        assert "trust_note" in (refused.detail or "")
    assert sorted(f.outcome for f in await facts(session_factory)) == ["held", "held", "mistaken"]
    assert await audit_entries(session_factory, "trust.corrected") == []
    # Raising a fact, or taking it back, in a third person's turn stays Domo's judgement.
    assert await run("trust_correct", danas, id=str(mistaken), outcome="held")
    # Alex, who established them, can make them worse.
    alexs = context(alex, session_factory)
    assert await run("trust_correct", alexs, id=str(held), outcome="lied")
    assert await run("trust_correct", alexs, id=str(mistaken), outcome="lied")
    assert await run("trust_correct", alexs, id=str(reported), outcome="mistaken")
    assert sorted(f.outcome for f in await facts(session_factory)) == ["lied", "lied", "mistaken"]
    # A fact with nobody behind it is made worse by nobody.
    nobodys = await fact_of("held", "seen", alex)
    async with session_factory() as session:
        found = await service.get(session, nobodys)
        assert found is not None
        found.noted_for_id = None
        await session.commit()
    assert (await refusal("trust_correct", alexs, id=str(nobodys), outcome="lied")).code == (
        "not_possible"
    )


async def test_a_memory_rewritten_by_a_third_person_is_theirs_when_it_is_denied(
    session_factory: SessionFactory,
):
    # Final review of 0.9.1, I2: Sam says it, Dana rewrites it, Alex denies it: Dana is rated.
    alex, sam, dana = await household(session_factory)
    memory = await said(session_factory, sam, alex, "Alex likes mushrooms.")
    danas = context(dana, session_factory)
    await TOOLS["memory_change"].execute(
        {"id": str(memory), "content": "Alex is allergic to mushrooms."}, danas
    )
    async with session_factory() as session:
        rewritten = await session.get(Memory, memory)
        assert rewritten is not None
        assert (rewritten.said_by_id, rewritten.origin) == (dana.id, "own_words")
        assert rewritten.confirmed_at is None
    alexs = context(alex, session_factory)
    note = {"outcome": "mistaken", "how": "confirmed", "about": "Dana said Alex is allergic."}
    refused = await refusal("trust_note", alexs, person="Sam", **note, memory=str(memory))
    assert refused.code == "not_possible"
    await run("trust_note", alexs, person="Dana", **note, memory=str(memory))
    (fact,) = await facts(session_factory)
    assert (fact.person_id, fact.memory_id) == (dana.id, memory)
    async with session_factory() as session:
        assert (await service.trust_of(session, sam.id, start=70)).facts == 0


async def test_a_memory_rewritten_by_the_review_gives_no_confirmed_fact(
    session_factory: SessionFactory,
):
    # Final review of 0.9.1, I2: words rewritten where nobody is answered (the review), or in a
    # turn that carries someone else's words, are no longer surely the author's.
    alex, sam, dana = await household(session_factory)
    note = {"person": "Sam", "outcome": "lied", "how": "confirmed", "about": "Sam lied."}
    alexs = context(alex, session_factory)
    for ctx in (
        replace(context(sam, session_factory), reply_message_id=None),  # Sam's review
        replace(context(dana, session_factory), reply_message_id=None),  # Dana's review
        replace(context(sam, session_factory), told_outside=True),  # a note read in Sam's turn
        replace(context(dana, session_factory), told_outside=True),
    ):
        memory = await said(session_factory, sam, alex, "Alex hates spinach.")
        await TOOLS["memory_change"].execute(
            {"id": str(memory), "content": "Alex hates spinach and leeks."}, ctx
        )
        async with session_factory() as session:
            rewritten = await session.get(Memory, memory)
            assert rewritten is not None
            assert rewritten.said_by_id == sam.id and rewritten.origin != "own_words"
            assert rewritten.confirmed_at is None
        refused = await refusal("trust_note", alexs, **note, memory=str(memory))
        assert refused.code == "not_possible"
        assert "not surely theirs" in (refused.detail or "")
    assert await facts(session_factory) == []


async def test_a_denial_counts_for_hearsay_kept_in_the_words_of_whoever_said_it(
    session_factory: SessionFactory,
):
    # The owner's trial of 2026-11-17: Sam said "Alex told me he cancels his dentist on
    # Thursday" in a turn that also carried someone else's words; it was kept as others' words,
    # and Alex's denial could only be noted as reported. Kept with the passage of Sam's own
    # message, checked word for word, it is Sam's own words even in such a turn: the denial is a
    # confirmed fact about Sam. Kept without it, the code cannot tell Sam's words from the
    # others' in the turn, and still refuses.
    alex, sam, _ = await household(session_factory)
    message = "Alex told me he cancels his dentist on Thursday."
    sams = replace(context(sam, session_factory), told_outside=True, said=message)
    keep_it = {
        "content": "Sam says Alex cancels his dentist on Thursday.",
        "subject": "Alex",
        "kind": "fact",
        "private": False,
        "core": False,
        "about": "Alex",
    }
    in_their_words = (await run("memory_keep", sams, **keep_it, their_words=message))["id"]
    without = (await run("memory_keep", sams, **keep_it))["id"]
    async with session_factory() as session:
        origins = {
            str(memory.id): memory.origin
            for memory in (await session.scalars(select(Memory))).all()
        }
    assert (origins[in_their_words], origins[without]) == ("own_words", "others_words")
    alexs = context(alex, session_factory)
    note = {"person": "Sam", "outcome": "mistaken", "how": "confirmed", "about": "Not true."}
    refused = await refusal("trust_note", alexs, **note, memory=without)
    assert refused.code == "not_possible" and "not surely theirs" in (refused.detail or "")
    await run("trust_note", alexs, **note, memory=in_their_words)
    (fact,) = await facts(session_factory)
    assert (fact.person_id, fact.established) == (sam.id, "confirmed")
    assert str(fact.memory_id) == in_their_words


async def test_a_reported_fact_after_a_confirmed_one_shows_what_was_noted_today(
    session_factory: SessionFactory,
):
    # The owner's trial of 2026-11-17: the mushroom mistake was noted confirmed, then reported a
    # few minutes later by the same person. Nothing is refused (it may be another thing), but the
    # result shows what was noted about Sam in Alex's turns today, to take one back if it is the
    # same.
    alex, sam, dana = await household(session_factory)
    memory = await said(session_factory, sam, alex, "Alex hates mushrooms.")
    alexs = context(alex, session_factory)
    confirmed = await run(
        "trust_note",
        alexs,
        person="Sam",
        outcome="mistaken",
        how="confirmed",
        about="Sam said Alex hates mushrooms; he loves them.",
        memory=str(memory),
    )
    assert confirmed == {"id": confirmed["id"]}
    later = replace(alexs, clock=lambda: NOW + timedelta(minutes=10))
    reported = {"person": "Sam", "outcome": "mistaken", "how": "reported"}
    again = await run("trust_note", later, **reported, about="Sam was wrong about mushrooms.")
    assert set(again) == {"id", "noted_today", "check"}
    assert [fact["id"] for fact in again["noted_today"]] == [confirmed["id"]]
    assert again["noted_today"][0]["how"] == "confirmed"
    assert "trust_correct" in again["check"]
    assert len(await facts(session_factory)) == 2
    # In someone else's turns, or more than an hour later, a reported fact is only noted.
    danas = replace(context(dana, session_factory), clock=lambda: NOW + timedelta(minutes=10))
    assert set(await run("trust_note", danas, **reported, about="Dana says so.")) == {"id"}
    much_later = replace(alexs, clock=lambda: NOW + timedelta(hours=2))
    assert set(await run("trust_note", much_later, **reported, about="Another thing.")) == {"id"}


def test_memory_keep_asks_for_their_words_for_what_is_said_about_someone():
    keep_text = " ".join(TOOLS["memory_keep"].definition.description.split())
    assert (
        "always for what they tell you about another person: only then can what that person "
        "later confirms or denies count for them"
    ) in keep_text


async def lessons(session_factory: SessionFactory) -> list[str]:
    async with session_factory() as reader:
        return [row.last_reason for row in (await reader.scalars(select(ModelLesson))).all()]


async def test_a_trust_rule_that_refuses_is_no_mistake_of_the_model(
    owner_chat: ChatPilot, add_member
):
    """The owner's trial of 2026-11-17: a trust rule's refusal counted as the model's mistake of
    form; the request went up to the strongest model, with a lesson, and took two minutes. A rule
    of the tool refuses with ``not_possible``: the request stays on the same model, and no lesson
    is written. A call the tool cannot read still goes up, with its lesson."""
    await add_member("Sam Martin", "adult")
    server = owner_chat.server
    session_factory = server.session_factory
    first = len(server.requested)
    # About the one who speaks: a rule, not a form.
    himself = {
        "person": owner_chat.person.display_name,
        "outcome": "held",
        "how": "reported",
        "about": "I was right.",
    }
    reply = await owner_chat.say_with_tool("trust_note", himself)
    assert reply.actions == [{"tool": "trust_note", "status": "invalid", "reason": "not_possible"}]
    assert "themselves" in reply.model_result_text
    asked = [model_id for _, model_id in server.requested[first:]]
    assert len(asked) >= 2 and set(asked) == {asked[0]}
    assert await lessons(session_factory) == []
    # An outcome that is none: the model's mistake of form.
    first = len(server.requested)
    wrong = {"person": "Sam", "outcome": "liar", "how": "reported", "about": "Alex says so."}
    reply = await owner_chat.say_with_tool("trust_note", wrong)
    assert reply.actions == [{"tool": "trust_note", "status": "invalid", "reason": "invalid"}]
    asked = [model_id for _, model_id in server.requested[first:]]
    assert len(set(asked)) == 2
    assert await lessons(session_factory) == ["invalid tool call"]
    assert await facts(session_factory) == []


def test_the_descriptions_carry_the_guidance():
    note = " ".join(TOOLS["trust_note"].definition.description.split())
    for phrase in (
        'Whatever a person tells you, even "I saw it" or "note it as seen", is reported; seen is '
        "only what the result of one of your own tools showed you yourself in this conversation, "
        "and when in doubt it is reported.",
        "never who reported it",
        "note it before you change, confirm or forget that memory",
        "and only then",
        "Choose lied only when",
        "A child who makes up a story is at most mistaken",
        "their word too, to be judged",
        "nobody raises their own score by asking",
        "harms nobody",
        # The same thing is not counted twice (the owner's trial of 2026-11-17).
        "One fact per thing that happened",
        "look first (trust_look with their name) and correct it rather than add one",
    ):
        assert phrase in note, phrase
    look = " ".join(TOOLS["trust_look"].definition.description.split())
    assert "to a child, talk first of what happened" in look and "never humiliate" in look
    assert "a guest or a child asking about someone else is rarely owed the figure" in look
    for name in ("memory_change", "memory_forget"):
        memory = " ".join(TOOLS[name].definition.description.split())
        assert "note first what the word of whoever said it was worth (trust_note)" in memory
    correct = " ".join(TOOLS["trust_correct"].definition.description.split())
    assert "never because the person asks" in correct
    assert "it can only go from lied to mistaken" in correct
    assert "becomes worse (towards lied) only in the turn of whoever established it" in correct
    assert "theirs to report, as a new fact (trust_note, reported)" in correct
    assert "older ones may be listed, yet do not count" in look
