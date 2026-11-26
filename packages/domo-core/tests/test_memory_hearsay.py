"""What someone says about someone else is their word until that person confirms it (F4, the
owner's trial of 0.9.0).

Sam told Domo that Alex does not like pine nuts; Domo kept it as a fact and planned a salad for
Alex around it, and it was false. Such a memory (about a person of the household, said by someone
else) is now marked ``unconfirmed`` in every context and search; only the person it is about
confirms it, in Domo's answer to them, or corrects it (then it is theirs).
"""

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from chat_support import ChatPilot
from domo_core.accounts.models import Person
from domo_core.audit.models import AuditEntry
from domo_core.confirmations.case import gather
from domo_core.memory.embedder import NO_EMBEDDER
from domo_core.memory.models import Memory
from domo_core.policy.actions import DescribedAction
from domo_core.policy.catalog import DEFAULT_POLICY
from domo_core.policy.roles import Actor
from domo_core.policy.subjects import Subject
from domo_core.prompts import build_system_prompt
from domo_core.tools.context import ToolContext
from domo_core.tools.registry import TOOLS
from fakes import FakeChatModel
from sqlalchemy import select

CORE_HEADING = "What you know (always): "
CONTEXT_MARK = "\n\nContext\n"
PINE_NUTS = "Alex does not like pine nuts."


def core_of(system: str) -> list[dict[str, Any]]:
    for line in system.split(CONTEXT_MARK, 1)[1].splitlines():
        if line.startswith(CORE_HEADING):
            return json.loads(line.removeprefix(CORE_HEADING))
    return []


async def context_of(chat: ChatPilot) -> str:
    model = chat.use_model(FakeChatModel(["ok"]))
    await chat.say("Hello")
    return model.calls[0].system


async def kept(chat: ChatPilot, content: str, subject: str, **more: Any) -> str:
    reply = await chat.say_with_tool(
        "memory_keep",
        {
            "content": content,
            "subject": subject,
            "kind": "preference",
            "private": False,
            "core": True,
            **more,
        },
    )
    assert reply.actions == [{"tool": "memory_keep", "status": "done"}]
    return reply.model_results[-1]["id"]


async def the_memory(chat: ChatPilot, memory_id: str) -> Memory:
    async with chat.server.session_factory() as reader:
        return (
            await reader.execute(
                select(Memory)
                .where(Memory.id == uuid.UUID(memory_id))
                .execution_options(populate_existing=True)
            )
        ).scalar_one()


def line_of(system: str, memory_id: str) -> dict[str, Any]:
    [line] = [m for m in core_of(system) if m["id"] == memory_id]
    return line


async def test_what_sam_says_about_alex_is_unconfirmed_in_alexs_context(
    adult_chat: ChatPilot, child_chat: ChatPilot
):
    memory_id = await kept(child_chat, PINE_NUTS, "Alex")
    memory = await the_memory(adult_chat, memory_id)
    # Its subject named a member: it is about them, though Domo gave no about.
    assert memory.about_id == adult_chat.person.id and memory.confirmed_at is None
    line = line_of(await context_of(adult_chat), memory_id)
    assert line["unconfirmed"] is True and line["said_by"] == "Sam"
    # In Sam's own turn too: it is her word about Alex.
    assert line_of(await context_of(child_chat), memory_id)["unconfirmed"] is True
    # The guard says what to do with it.
    flat = " ".join((await context_of(adult_chat)).split(CONTEXT_MARK, 1)[1].split())
    assert "What someone told you about another person (unconfirmed) is their word" in flat
    # How far he believes it is his judgement (the owner, 2026-11-16): who says it, what they
    # are to that person, what it is about; he checks when he doubts or when it would matter.
    assert "you judge how far to believe it, from who says it" in flat
    assert "When you doubt it, or when being wrong would matter" in flat
    assert "keep what that taught you about whom to believe" in flat


async def test_alex_confirms_it_and_the_mark_goes(adult_chat: ChatPilot, child_chat: ChatPilot):
    memory_id = await kept(child_chat, PINE_NUTS, "Alex")
    before = await the_memory(adult_chat, memory_id)
    reply = await adult_chat.say_with_tool(
        "memory_change", {"id": memory_id, "confirmed": True}, text="Yes, that's true."
    )
    assert reply.actions == [{"tool": "memory_change", "status": "done"}]
    after = await the_memory(adult_chat, memory_id)
    assert after.confirmed_at is not None
    # Who first said it, and where it comes from, stay.
    assert (after.said_by_id, after.origin) == (before.said_by_id, before.origin)
    line = line_of(await context_of(adult_chat), memory_id)
    assert "unconfirmed" not in line and line["said_by"] == "Sam"


async def test_sam_cannot_confirm_what_she_said_about_alex(
    adult_chat: ChatPilot, child_chat: ChatPilot, owner_chat: ChatPilot
):
    memory_id = await kept(child_chat, PINE_NUTS, "Alex")
    # The one who said it never confirms it in their own turn, whatever reason is given.
    reply = await child_chat.say_with_tool(
        "memory_change", {"id": memory_id, "confirmed": True, "confirmed_reason": "I say so."}
    )
    assert reply.actions == [
        {"tool": "memory_change", "status": "invalid", "reason": "not_possible"}
    ]
    assert "the one who said it does not confirm it" in reply.model_result_text
    # A third person's turn: not without his reason (he judges, he says why).
    reply = await owner_chat.say_with_tool("memory_change", {"id": memory_id, "confirmed": True})
    assert reply.actions == [{"tool": "memory_change", "status": "invalid", "reason": "invalid"}]
    assert "confirmed_reason" in reply.model_result_text
    assert (await the_memory(adult_chat, memory_id)).confirmed_at is None


async def test_he_confirms_what_he_heard_elsewhere_in_a_third_person_s_turn(
    adult_chat: ChatPilot, child_chat: ChatPilot, owner_chat: ChatPilot
):
    # The owner's decision of 2026-11-24: what he hears elsewhere, he judges. In the owner's turn
    # (not Alex's, not Sam's) he marks it confirmed with his reason; the audit keeps the reason.
    memory_id = await kept(child_chat, PINE_NUTS, "Alex")
    reply = await owner_chat.say_with_tool(
        "memory_change",
        {"id": memory_id, "confirmed": True, "confirmed_reason": "Alex told me in the kitchen."},
    )
    assert reply.actions == [{"tool": "memory_change", "status": "done"}]
    after = await the_memory(adult_chat, memory_id)
    assert after.confirmed_at is not None and after.said_by_id == child_chat.person.id
    async with adult_chat.server.session_factory() as reader:
        entry = await reader.scalar(select(AuditEntry).where(AuditEntry.action == "memory.changed"))
    assert entry is not None
    assert entry.details["confirmed_reason"] == "Alex told me in the kitchen."


async def test_what_alex_says_of_himself_and_of_the_boiler_is_never_marked(
    adult_chat: ChatPilot,
):
    himself = await kept(adult_chat, "I love pine nuts.", "Alex")
    boiler = await kept(adult_chat, "The boiler is noisy.", "the boiler")
    assert (await the_memory(adult_chat, himself)).about_id == adult_chat.person.id
    assert (await the_memory(adult_chat, boiler)).about_id is None
    lines = core_of(await context_of(adult_chat))
    assert {m["id"] for m in lines} >= {himself, boiler}
    assert all("unconfirmed" not in m for m in lines)
    # Nothing to confirm on either.
    reply = await adult_chat.say_with_tool("memory_change", {"id": boiler, "confirmed": True})
    assert reply.actions == [
        {"tool": "memory_change", "status": "invalid", "reason": "not_possible"}
    ]
    assert "needs no confirmation" in reply.model_result_text


async def judge_reads(chat: ChatPilot) -> list[str]:
    """What the yes-judge reads as the own words of the person of ``chat`` about pine nuts (the
    ``remembered`` of their case)."""
    async with chat.server.session_factory() as session:
        case = await gather(
            session,
            about=chat.person.id,
            private_of=chat.person.id,
            action=DescribedAction("core", "household.invite", None, "permissions", "household"),
            subject=Subject(None, chat.person.id),
            says="pine nuts",
            extra_query=None,
            embedder=NO_EMBEDDER,
            zone=None,
            now=datetime.now(UTC),
            start_trust=50,
        )
    return [line.content for line in case.remembered]


async def test_alexs_correction_with_his_own_words_makes_it_his(
    adult_chat: ChatPilot, child_chat: ChatPilot
):
    memory_id = await kept(child_chat, PINE_NUTS, "Alex")
    reply = await adult_chat.say_with_tool(
        "memory_change",
        {"id": memory_id, "content": "Alex loves pine nuts.", "their_words": "I love them"},
        text="No, I love them!",
    )
    assert reply.actions == [{"tool": "memory_change", "status": "done"}]
    memory = await the_memory(adult_chat, memory_id)
    assert memory.said_by_id == adult_chat.person.id and memory.origin == "own_words"
    assert memory.confirmed_at is not None
    line = line_of(await context_of(adult_chat), memory_id)
    assert line["said_by"] == "you" and "unconfirmed" not in line
    assert "Alex loves pine nuts." in await judge_reads(adult_chat)


async def test_alexs_correction_without_his_words_is_confirmed_but_not_his(
    adult_chat: ChatPilot, child_chat: ChatPilot
):
    """IMPORTANT 3 of the review: without the checked passage of his message, a rewrite never
    makes Sam's word Alex's own words, which his yes-judge would read."""
    memory_id = await kept(child_chat, PINE_NUTS, "Alex")
    before = await the_memory(adult_chat, memory_id)
    reply = await adult_chat.say_with_tool(
        "memory_change", {"id": memory_id, "content": "Alex loves pine nuts."}, text="No!"
    )
    assert reply.actions == [{"tool": "memory_change", "status": "done"}]
    memory = await the_memory(adult_chat, memory_id)
    assert memory.confirmed_at is not None
    assert memory.said_by_id == child_chat.person.id
    assert memory.origin == "others_words" and before.origin == "own_words"
    line = line_of(await context_of(adult_chat), memory_id)
    assert line["said_by"] == "Sam" and "unconfirmed" not in line
    assert "Alex loves pine nuts." not in await judge_reads(adult_chat)


async def test_the_search_marks_it_too(adult_chat: ChatPilot, child_chat: ChatPilot):
    memory_id = await kept(child_chat, PINE_NUTS, "Alex")
    reply = await adult_chat.say_with_tool("memory_search", {"words": "pine nuts"})
    [found] = [m for m in reply.model_results[-1]["memories"] if m["id"] == memory_id]
    assert found["unconfirmed"] is True and found["said_by"] == "Sam"


def _review_context(person: Person, session_factory) -> ToolContext:
    """A call of the review's (``memory.review``): no message answered."""
    return ToolContext(
        session_factory=session_factory,
        actor=Actor(person.id, person.role),  # type: ignore[arg-type]
        person=person,
        conversation_id=uuid.uuid4(),
        origin="",
        gatekeeper=None,  # type: ignore[arg-type]
        clock=lambda: datetime.now(UTC),
        registry=TOOLS,
        policy=DEFAULT_POLICY,
        reply_message_id=None,
        no_reader=True,
    )


async def test_the_review_keeps_the_mark_when_it_rewrites(
    adult_chat: ChatPilot, child_chat: ChatPilot
):
    memory_id = await kept(child_chat, PINE_NUTS, "Alex")
    factory = adult_chat.server.session_factory
    for person in (child_chat.person, adult_chat.person):
        outcome = await TOOLS["memory_change"].execute(
            {"id": memory_id, "content": "Alex may not like pine nuts."},
            _review_context(person, factory),
        )
        assert outcome.status == "done"
        memory = await the_memory(adult_chat, memory_id)
        assert memory.confirmed_at is None and memory.said_by_id == child_chat.person.id
    # Confirmed by Alex, then rewritten in Sam's review: her new words wait for him again.
    await adult_chat.say_with_tool("memory_change", {"id": memory_id, "confirmed": True})
    assert (await the_memory(adult_chat, memory_id)).confirmed_at is not None
    await TOOLS["memory_change"].execute(
        {"id": memory_id, "content": "Alex hates pine nuts."},
        _review_context(child_chat.person, factory),
    )
    assert (await the_memory(adult_chat, memory_id)).confirmed_at is None


def test_a_line_says_unconfirmed_only_when_it_is_so():
    from domo_core.prompts import MemoryBlock, MemoryLine

    def line(**more: Any) -> MemoryLine:
        return MemoryLine(
            id="m1",
            subject="Alex",
            content=PINE_NUTS,
            kind="preference",
            said_by="Sam",
            when="2026-11-16T10:00:00+00:00",
            private=False,
            **more,
        )

    prompt = build_system_prompt(
        display_name="Alex",
        role="adult",
        can_act=True,
        memory=MemoryBlock(core=(line(unconfirmed=True), line())),
    )
    first, second = core_of(prompt)
    assert first["unconfirmed"] is True and "unconfirmed" not in second
