"""The conversation store: the active conversation, its messages, and how a reply is finished."""

import asyncio
import uuid
from datetime import timedelta

import pytest
from domo_core.chat.store import ConversationStore
from domo_core.text import PlainStream, without_marks
from sqlalchemy import text


@pytest.fixture
def store(session):
    return ConversationStore(session)


@pytest.fixture
async def person(add_person):
    return await add_person("Someone", "owner")


async def add_pair(store, conversation, *, reply="", status="streaming"):
    user = await store.add_message(
        conversation.id, role="user", content="question", status="complete", provenance="person"
    )
    assistant = await store.add_message(
        conversation.id,
        role="assistant",
        content=reply,
        status=status,
        provenance="model",
        model="example-model-1",
    )
    return user, assistant


async def add_text_messages(store, conversation, count):
    for index in range(count):
        await store.add_message(
            conversation.id,
            role="user",
            content=str(index),
            status="complete",
            provenance="person",
        )


async def stored(session, message_id):
    result = await session.execute(
        text("SELECT content, status FROM messages WHERE id = :id"), {"id": message_id}
    )
    return tuple(result.one())


# --- conversations ---------------------------------------------------------------------------


async def test_a_person_without_conversation_gets_one(store, person):
    conversation = await store.active_conversation(person.id)
    assert conversation.person_id == person.id
    assert (await store.active_conversation(person.id)).id == conversation.id


async def test_the_active_conversation_is_the_most_recent_one(store, person, add_conversation):
    older = await add_conversation(person)
    newer = await add_conversation(person)
    assert older.id != newer.id
    assert (await store.active_conversation(person.id)).id == newer.id


async def test_conversations_belong_to_their_person(store, person, add_person):
    other = await add_person("Other", "adult")
    mine = await store.active_conversation(person.id)
    theirs = await store.active_conversation(other.id)
    assert mine.id != theirs.id


async def test_looking_for_a_conversation_creates_nothing(store, person, session):
    assert await store.latest_conversation(person.id) is None
    count = (await session.execute(text("SELECT count(*) FROM conversations"))).scalar_one()
    assert count == 0


# --- messages --------------------------------------------------------------------------------


async def test_add_message_stores_what_it_is_given(store, person):
    conversation = await store.active_conversation(person.id)
    user, assistant = await add_pair(store, conversation)
    assert (user.role, user.provenance, user.model, user.status) == (
        "user",
        "person",
        None,
        "complete",
    )
    assert (assistant.role, assistant.provenance, assistant.model, assistant.status) == (
        "assistant",
        "model",
        "example-model-1",
        "streaming",
    )
    assert assistant.content == "" and assistant.created_at is not None


async def test_recent_messages_are_in_chronological_order(store, person):
    conversation = await store.active_conversation(person.id)
    await add_text_messages(store, conversation, 4)
    messages = await store.recent_messages(conversation.id)
    assert [m.content for m in messages] == ["0", "1", "2", "3"]


async def test_recent_messages_keeps_the_latest_when_there_are_more_than_the_limit(store, person):
    conversation = await store.active_conversation(person.id)
    await add_text_messages(store, conversation, 5)
    messages = await store.recent_messages(conversation.id, limit=2)
    assert [m.content for m in messages] == ["3", "4"]


async def test_recent_messages_are_those_of_the_conversation(store, person, add_conversation):
    first = await store.active_conversation(person.id)
    other = await add_conversation(person)
    await add_pair(store, first, status="complete")
    assert await store.recent_messages(other.id) == []


# --- the reply that streams ------------------------------------------------------------------


async def test_streaming_message_finds_the_reply_in_progress(store, person):
    conversation = await store.active_conversation(person.id)
    assert await store.streaming_message(conversation.id) is None
    _, assistant = await add_pair(store, conversation)
    found = await store.streaming_message(conversation.id)
    assert found is not None and found.id == assistant.id


async def test_finish_message_settles_the_reply(store, person, session):
    conversation = await store.active_conversation(person.id)
    _, assistant = await add_pair(store, conversation)
    assert await store.finish_message(assistant.id, content="Hello", status="complete") is True
    assert await stored(session, assistant.id) == ("Hello", "complete")
    assert await store.streaming_message(conversation.id) is None


async def test_finish_message_keeps_a_reply_without_markdown_marks(store, person, session):
    """In the owner's trial of 0.7.1 the model wrote ``**`` around its headings, told not to: the
    page showed the stars. The reply is kept without them, words and lists as they are."""
    conversation = await store.active_conversation(person.id)
    _, assistant = await add_pair(store, conversation)
    written = (
        "## Ce que je sais faire\n\n**La maison**\n- inviter **quelqu'un**\n"
        "2 ** 3, __init__ et a * b"
    )
    kept = "Ce que je sais faire\n\nLa maison\n- inviter quelqu'un\n2 ** 3, __init__ et a * b"
    assert await store.finish_message(assistant.id, content=written, status="complete") is True
    assert await stored(session, assistant.id) == (kept, "complete")


@pytest.mark.parametrize("size", [1, 2, 3, 7, 1000])
def test_a_reply_that_comes_in_pieces_loses_its_marks_however_it_is_cut(size):
    written = (
        "## Ce que je sais faire\n\n**La maison**\n- inviter **quelqu'un**\n"
        "  # pas un titre ?\n2 ** 3, __init__ et a * b\n#\n**"
    )
    stream = PlainStream()
    pieces = [stream.feed(written[at : at + size]) for at in range(0, len(written), size)]
    shown = "".join(pieces) + stream.flush()
    assert shown == without_marks(written)
    assert shown == (
        "Ce que je sais faire\n\nLa maison\n- inviter quelqu'un\n"
        "pas un titre ?\n2 ** 3, __init__ et a * b\n#\n**"
    )


async def test_finish_message_refreshes_the_update_time(store, person, session):
    conversation = await store.active_conversation(person.id)
    _, assistant = await add_pair(store, conversation)
    await asyncio.sleep(0.01)
    await store.finish_message(assistant.id, content="x", status="complete")
    result = await session.execute(
        text("SELECT created_at, updated_at FROM messages WHERE id = :id"), {"id": assistant.id}
    )
    created, updated = result.one()
    assert updated - created > timedelta(0)


async def test_finish_message_does_not_rewrite_a_settled_reply(store, person, session):
    conversation = await store.active_conversation(person.id)
    _, assistant = await add_pair(store, conversation)
    assert await store.finish_message(assistant.id, content="first", status="error") is True
    # It says that it changed nothing, so that the caller does not report a turn twice.
    assert await store.finish_message(assistant.id, content="second", status="complete") is False
    assert await stored(session, assistant.id) == ("first", "error")


async def test_finish_message_of_an_unknown_message_is_a_no_op(store):
    assert await store.finish_message(uuid.uuid4(), content="x", status="complete") is False


# --- ownership -------------------------------------------------------------------------------


async def test_a_message_is_found_for_the_person_who_owns_it(store, person, add_person):
    conversation = await store.active_conversation(person.id)
    _, assistant = await add_pair(store, conversation)
    other = await add_person("Other", "adult")
    found = await store.message_of(person.id, assistant.id)
    assert found is not None and found.id == assistant.id
    assert await store.message_of(other.id, assistant.id) is None
    assert await store.message_of(person.id, uuid.uuid4()) is None


# --- the actions of a reply ------------------------------------------------------------------


async def test_the_actions_of_replies_come_back_by_reply_in_the_order_they_were_taken(
    store, person
):
    conversation = await store.active_conversation(person.id)
    _, first = await add_pair(store, conversation, status="complete")
    _, second = await add_pair(store, conversation)
    for message, tool, status in (
        (first, "household_list", "done"),
        (second, "profile_rename", "refused"),
        (first, "profile_rename", "invalid"),
    ):
        await store.add_action(
            message.id,
            tool_name=tool,
            action_type="",
            status=status,
            summary={"key": "action.card.done", "params": {}},
        )
    actions = await store.actions_of([first.id, second.id, uuid.uuid4()])
    assert {key: [(a.tool_name, a.status) for a in rows] for key, rows in actions.items()} == {
        first.id: [("household_list", "done"), ("profile_rename", "invalid")],
        second.id: [("profile_rename", "refused")],
    }
    assert actions[first.id][0].summary == {"key": "action.card.done", "params": {}}
    assert actions[first.id][0].confirmation_id is None


async def test_no_message_has_no_actions(store):
    assert await store.actions_of([]) == {}
