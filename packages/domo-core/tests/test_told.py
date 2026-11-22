"""What Domo told a person before an action (spec 6.8): when the judge warns them (``warn``)
and they answer, yes or no, the code keeps it as a memory of their own, ``told_by_domo``; and
what the judge may read of the memories (``for_judge``): only what the person said themselves or
what Domo told them, never what someone else said, nor the words of someone else, a deduction or
an outside content."""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from chat_support import status_of
from domo_core.confirmations.models import Confirmation
from domo_core.llm.types import ToolCall
from domo_core.memory.embedder import NO_EMBEDDER
from domo_core.memory.models import Memory
from domo_core.memory.service import keep
from domo_core.memory.told import for_judge, told
from fakes import FakeChatModel
from sqlalchemy import select

RENAME = ("profile_rename", {"name": "Alex B"})
WARNING = "warn: you cannot take it back"
WARNED = "you cannot take it back"


async def told_memories(session_factory, person_id: uuid.UUID) -> list[Memory]:
    """The memories of what Domo told this person (committed)."""
    async with session_factory() as reader:
        found = await reader.scalars(
            select(Memory).where(Memory.said_by_id == person_id, Memory.origin == "told_by_domo")
        )
        return list(found.all())


async def warned_of(session_factory, cid: uuid.UUID | str | None) -> bool:
    assert cid is not None
    async with session_factory() as reader:
        found = await reader.execute(
            select(Confirmation.warned).where(Confirmation.id == uuid.UUID(str(cid)))
        )
        return found.scalar_one()


# --- what the code keeps when the person answers a warning ---------------------------------------


@pytest.mark.parametrize("answer", ["oui", "non merci", "button_refuse"])
async def test_answering_a_warned_request_is_remembered(
    adult_chat, chat_server, confirmation_ask, confirmation_status, answer
):
    reply = await adult_chat.say_with_tool(*RENAME, judge=WARNING)
    cid = reply.pending_confirmation_id
    # To the code a warning is a yes asked, with the judge's reason.
    assert await confirmation_ask(cid) == ("yes", "judge", WARNED)
    assert await warned_of(chat_server.session_factory, cid)
    assert await told_memories(chat_server.session_factory, adult_chat.person.id) == []

    if answer == "button_refuse":
        assert status_of(await adult_chat.post(f"/confirmations/{cid}/refuse")) == "refused"
    else:
        await adult_chat.say(answer)
    assert await confirmation_status(cid) == ("executed" if answer == "oui" else "refused")

    [memory] = await told_memories(chat_server.session_factory, adult_chat.person.id)
    assert (memory.content, memory.action_type, memory.subject) == (
        WARNED,
        "profile.rename",
        "profile.rename",
    )
    assert (memory.kind, memory.kept_private, memory.core) == ("fact", True, False)


async def test_an_expired_warned_request_is_not_remembered(adult_chat, chat_server, clock):
    reply = await adult_chat.say_with_tool(*RENAME, judge=WARNING)
    clock.advance(timedelta(hours=168, seconds=1))
    response = await adult_chat.post(f"/confirmations/{reply.pending_confirmation_id}/approve")
    assert status_of(response) == "expired"
    assert await told_memories(chat_server.session_factory, adult_chat.person.id) == []


async def test_a_plain_yes_of_the_judge_is_not_remembered(adult_chat, chat_server):
    reply = await adult_chat.say_with_tool(*RENAME, judge="yes: a first time")
    assert not await warned_of(chat_server.session_factory, reply.pending_confirmation_id)
    await adult_chat.say("oui")
    assert await told_memories(chat_server.session_factory, adult_chat.person.id) == []


async def test_an_approver_answering_a_childs_warned_request_writes_nothing(
    child_chat,
    chat_server,
    grant,
    live_adult_client,
    owner_member,
    adult_member,
    confirmation_status,
):
    # Task 7's missing test: only the one who asked was told; an approver's answer keeps nothing.
    await grant("note.leave", role="child", approver="adult")
    reply = await child_chat.say_with_tool(
        "note_leave", {"recipient": "Owner", "text": "x"}, judge=WARNING
    )
    cid = reply.pending_confirmation_id
    assert await warned_of(chat_server.session_factory, cid)
    await live_adult_client.post(f"/confirmations/{cid}/approve")
    assert await confirmation_status(cid) == "executed"
    for person in (child_chat.person, adult_member.person):
        assert await told_memories(chat_server.session_factory, person.id) == []


async def test_a_warned_request_answered_by_a_change_in_words_is_remembered(
    adult_chat, chat_server, confirmation_status
):
    # Task 7's missing test: the one who asked answers with a change; they were told.
    reply = await adult_chat.say_with_tool(*RENAME, judge=WARNING)
    rid = str(reply.pending_confirmation_id)
    change = ToolCall(
        "c1",
        "confirmation_amend",
        json.dumps({"request_id": rid, "arguments": {"name": "Alex C"}, "ask_again": False}),
    )
    adult_chat.use_model(FakeChatModel(tool_script=["change", change, "Done."], judge="do"))
    await adult_chat.say("ok, but call me Alex C instead")
    assert await confirmation_status(rid) == "executed"
    [memory] = await told_memories(chat_server.session_factory, adult_chat.person.id)
    assert (memory.content, memory.action_type) == (WARNED, "profile.rename")


async def test_a_warning_given_for_a_chain_is_not_remembered(adult_chat, chat_server):
    # Final review M9 (interim): kept under the chain's type, it would say nothing of its step.
    chain = {"steps": [{"tool": RENAME[0], "arguments": RENAME[1]}]}
    reply = await adult_chat.say_with_tool("do_in_order", chain, judge=WARNING)
    cid = reply.pending_confirmation_id
    assert await warned_of(chat_server.session_factory, cid)
    await adult_chat.say("oui")
    assert await told_memories(chat_server.session_factory, adult_chat.person.id) == []


async def test_a_second_warning_refreshes_the_memory(session, add_person):
    alex = await add_person("Alex", "adult")
    first = datetime(2030, 1, 1, tzinfo=UTC)
    await told(
        session, person_id=alex.id, action_type="household.remove", reason="no way back", now=first
    )
    later = first + timedelta(days=30)
    await told(
        session,
        person_id=alex.id,
        action_type="household.remove",
        reason="gone for good",
        now=later,
    )
    # Another type of action is another memory.
    await told(session, person_id=alex.id, action_type="profile.rename", reason="x", now=later)
    found = (
        await session.scalars(select(Memory).where(Memory.action_type == "household.remove"))
    ).all()
    [memory] = found
    assert (memory.content, memory.changed_at, memory.created_at) == ("gone for good", later, first)


async def test_a_reason_the_memories_refuse_is_not_kept_and_raises_nothing(session, add_person):
    alex = await add_person("Alex", "adult")
    await told(
        session,
        person_id=alex.id,
        action_type="household.remove",
        reason="sk-ant-0123456789abcdefghijkl",
        now=datetime(2030, 1, 1, tzinfo=UTC),
    )
    assert (await session.scalars(select(Memory))).all() == []


# --- what the judge may read ---------------------------------------------------------------------


async def _keep(
    session, said_by, content, origin, *, private=False, action_type=None, subject="the guest room"
):
    return await keep(
        session,
        said_by=said_by,
        subject=subject,
        content=content,
        kind="fact",
        origin=origin,
        kept_private=private,
        core=False,
        now=datetime(2030, 1, 1, tzinfo=UTC),
        action_type=action_type,
    )


async def test_the_judge_reads_what_it_told_them_first_then_their_own_words(session, add_person):
    alex = await add_person("Alex", "adult")
    words = await _keep(session, alex.id, "Robin may sleep in the guest room", "own_words")
    warned = await _keep(
        session, alex.id, "Robin keeps the key", "told_by_domo", action_type="household.invite"
    )
    other_type = await _keep(
        session,
        alex.id,
        "a rename is final",
        "told_by_domo",
        action_type="profile.rename",
        subject="names",
    )
    found = await for_judge(
        session, alex.id, "household.invite", "Invite Robin to the guest room", embedder=NO_EMBEDDER
    )
    assert found[0].id == warned.id
    assert words.id in {memory.id for memory in found}
    # What Domo told them before another action is not about this one; the search may still
    # find it by its words, but here it shares none.
    assert other_type.id not in {memory.id for memory in found}


async def test_the_judge_never_reads_what_someone_else_said(session, add_person):
    alex = await add_person("Alex", "adult")
    sam = await add_person("Sam", "child")
    # Shared, about the very same thing, in Sam's own words: still not Alex's.
    await _keep(session, sam.id, "Robin may sleep in the guest room", "own_words")
    await _keep(
        session, sam.id, "Robin keeps the key", "told_by_domo", action_type="household.invite"
    )
    found = await for_judge(
        session, alex.id, "household.invite", "Invite Robin to the guest room", embedder=NO_EMBEDDER
    )
    assert found == []


async def test_the_judge_never_reads_others_words_or_deductions(session, add_person):
    alex = await add_person("Alex", "adult")
    for origin in ("others_words", "deduced", "external"):
        await _keep(session, alex.id, f"Robin may sleep in the guest room ({origin})", origin)
    found = await for_judge(
        session, alex.id, "household.invite", "Invite Robin to the guest room", embedder=NO_EMBEDDER
    )
    assert found == []
    mine = await _keep(session, alex.id, "Robin may sleep in the guest room", "own_words")
    found = await for_judge(
        session, alex.id, "household.invite", "Invite Robin to the guest room", embedder=NO_EMBEDDER
    )
    assert [memory.id for memory in found] == [mine.id]


async def test_the_judge_reads_at_most_its_limit(session, add_person):
    alex = await add_person("Alex", "adult")
    for n in range(4):
        await _keep(
            session, alex.id, f"warning {n}", "told_by_domo", action_type="household.invite"
        )
    await _keep(session, alex.id, "Robin may sleep in the guest room", "own_words")
    found = await for_judge(
        session,
        alex.id,
        "household.invite",
        "Invite Robin to the guest room",
        embedder=NO_EMBEDDER,
        limit=3,
    )
    assert len(found) == 3 and {memory.origin for memory in found} == {"told_by_domo"}
