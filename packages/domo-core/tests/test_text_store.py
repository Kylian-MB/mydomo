"""The texts Domo wrote: kept for a person or a language, with the versions they replaced."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from domo_common.meanings import all_keys, meaning_hash
from domo_core.chat.models import Conversation, Message, MessageAction
from domo_core.texts.models import CardText, WrittenText
from domo_core.texts.store import (
    KEPT_VERSIONS,
    Reader,
    languages_written_since,
    missing_keys,
    restore_previous,
    save_written,
    written,
)
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
KEY, OTHER_KEY = all_keys()[0], all_keys()[1]
MODEL = "example-small-1"


def at(minutes: int) -> datetime:
    return NOW + timedelta(minutes=minutes)


async def count(session, **where) -> int:
    statement = select(func.count()).select_from(WrittenText).filter_by(**where)
    return (await session.execute(statement)).scalar_one()


@pytest.fixture
def house_texts() -> None:
    """No text written yet: these tests keep the texts themselves."""


async def test_a_text_is_kept_for_a_person_or_a_language_never_both(session, add_person):
    alex = await add_person("Alex", "owner")
    row = dict(key=KEY, text="Hello", meaning_hash="h", model_id=MODEL, written_at=NOW)
    for bad in (dict(person_id=alex.id, language="fr"), dict(person_id=None, language=None)):
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                session.add(WrittenText(**row, **bad))
                await session.flush()
    session.add(WrittenText(**row, person_id=alex.id))
    session.add(WrittenText(**row, language="fr"))
    await session.flush()
    # One text per reader and key, for a person and for a language.
    for same in (dict(person_id=alex.id), dict(language="fr")):
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                session.add(WrittenText(**row, **same))
                await session.flush()


async def test_saving_again_keeps_five_previous_versions(session):
    reader = Reader(None, "fr")
    for number in range(KEPT_VERSIONS + 3):
        await save_written(session, reader, {KEY: f"text {number}"}, model_id=MODEL, now=at(number))
    session.expire_all()
    row = (await session.execute(select(WrittenText))).scalar_one()
    assert row.text == f"text {KEPT_VERSIONS + 2}"
    assert [version["text"] for version in row.previous] == [
        f"text {number}" for number in range(KEPT_VERSIONS + 1, 1, -1)
    ]
    assert len(row.previous) == KEPT_VERSIONS
    assert row.previous[0] == {
        "text": f"text {KEPT_VERSIONS + 1}",
        "meaning_hash": meaning_hash(KEY),
        "written_at": row.previous[0]["written_at"],
    }
    assert datetime.fromisoformat(row.previous[0]["written_at"]) == at(KEPT_VERSIONS + 1)
    assert row.written_at == at(KEPT_VERSIONS + 2)


async def test_saving_the_same_text_again_changes_nothing(session):
    reader = Reader(None, "fr")
    await save_written(session, reader, {KEY: "Hello"}, model_id=MODEL, now=at(0))
    await save_written(session, reader, {KEY: "Hello"}, model_id="other", now=at(5))
    session.expire_all()
    row = (await session.execute(select(WrittenText))).scalar_one()
    assert (row.previous, row.model_id, row.written_at) == ([], MODEL, at(0))


async def test_a_person_and_a_language_are_kept_apart(session, add_person):
    alex = await add_person("Alex", "owner")
    person = Reader(alex.id, "fr")
    await save_written(session, person, {KEY: "For Alex"}, model_id=MODEL, now=NOW)
    await save_written(session, Reader(None, "fr"), {KEY: "For French"}, model_id=MODEL, now=NOW)
    # A person's texts are kept by the person, whatever language the reader says.
    again = Reader(alex.id, "de")
    assert (await written(session, [person, again, Reader(None, "fr"), Reader(None, "de")])) == {
        person: {KEY: "For Alex"},
        again: {KEY: "For Alex"},
        Reader(None, "fr"): {KEY: "For French"},
        Reader(None, "de"): {},
    }
    assert await count(session, person_id=alex.id, language=None) == 1


async def test_restore_brings_back_the_previous_text(session):
    reader = Reader(None, "fr")
    for number, text in enumerate(("first", "second", "third")):
        await save_written(
            session, reader, {KEY: text, OTHER_KEY: text}, model_id=MODEL, now=at(number)
        )
    assert await restore_previous(session, reader, [KEY, "unknown.key"]) == [KEY]
    assert (await written(session, [reader]))[reader] == {KEY: "second", OTHER_KEY: "third"}
    session.expire_all()
    row = (await session.execute(select(WrittenText).filter_by(key=KEY))).scalar_one()
    assert [version["text"] for version in row.previous] == ["first"]
    assert row.written_at == at(1)
    # Again, then once more: nothing is left to bring back.
    assert await restore_previous(session, reader, [KEY]) == [KEY]
    assert (await written(session, [reader]))[reader][KEY] == "first"
    assert await restore_previous(session, reader, [KEY]) == []
    assert (await written(session, [reader]))[reader][KEY] == "first"


async def test_restore_skips_versions_written_for_another_meaning(session):
    reader = Reader(None, "fr")
    await save_written(session, reader, {KEY: "kept"}, model_id=MODEL, now=at(0))
    await save_written(session, reader, {KEY: "new"}, model_id=MODEL, now=at(1))
    row = (await session.execute(select(WrittenText))).scalar_one()
    old = {"text": "old meaning", "meaning_hash": "stale", "written_at": at(2).isoformat()}
    row.previous = [old, *row.previous, {**old, "text": "older meaning"}]
    await session.flush()
    assert await restore_previous(session, reader, [KEY]) == [KEY]
    session.expire_all()
    row = (await session.execute(select(WrittenText))).scalar_one()
    # The newer entry above it is dropped with the text it replaced; the older one stays.
    assert row.text == "kept"
    assert [version["text"] for version in row.previous] == ["older meaning"]
    # With no version of the current meaning, the key is not restored.
    row.previous = [old]
    await session.flush()
    assert await restore_previous(session, reader, [KEY]) == []
    assert (await written(session, [reader]))[reader] == {KEY: "kept"}


async def test_a_text_written_for_an_old_meaning_is_not_read(session):
    reader = Reader(None, "fr")
    await save_written(session, reader, {KEY: "Hello", OTHER_KEY: "Hi"}, model_id=MODEL, now=NOW)
    row = (await session.execute(select(WrittenText).filter_by(key=KEY))).scalar_one()
    row.meaning_hash = "the meaning before"
    session.add(
        WrittenText(
            language="fr",
            key="gone.from.the.catalog",
            text="Bye",
            meaning_hash="whatever",
            model_id=MODEL,
            written_at=NOW,
        )
    )
    await session.flush()
    assert (await written(session, [reader]))[reader] == {OTHER_KEY: "Hi"}
    assert await missing_keys(session, reader, [KEY, OTHER_KEY, "gone.from.the.catalog"]) == [KEY]
    # Written again for the meaning of now, it is read.
    await save_written(session, reader, {KEY: "Hello again"}, model_id=MODEL, now=at(1))
    assert (await written(session, [reader]))[reader][KEY] == "Hello again"
    assert await missing_keys(session, reader, [KEY, OTHER_KEY]) == []


async def test_missing_keys_are_those_without_a_text_in_the_order_given(session):
    reader = Reader(None, "fr")
    await save_written(session, reader, {OTHER_KEY: "Hi"}, model_id=MODEL, now=NOW)
    third = all_keys()[2]
    assert await missing_keys(session, reader, [third, KEY, OTHER_KEY, KEY]) == [third, KEY]


async def test_two_saves_at_once_keep_one_row(session_factory):
    reader = Reader(None, "fr")

    started = asyncio.Event()

    async def first() -> None:
        async with session_factory() as one:
            await save_written(one, reader, {KEY: "one"}, model_id=MODEL, now=NOW)
            started.set()
            # Not committed yet: the other save meets this row and has to wait for it.
            await asyncio.sleep(0.5)
            await one.commit()

    async def second() -> None:
        await started.wait()
        async with session_factory() as two:
            await save_written(two, reader, {KEY: "two"}, model_id=MODEL, now=NOW)
            await two.commit()

    await asyncio.gather(first(), second())
    async with session_factory() as check:
        row = (await check.execute(select(WrittenText))).scalar_one()
        assert row.text == "two"
        assert [version["text"] for version in row.previous] == ["one"]
    # And when both begin with a row there: the second replaces what the first left.
    barrier = asyncio.Barrier(2)

    async def replace(text: str) -> None:
        async with session_factory() as other:
            await barrier.wait()
            await save_written(other, reader, {KEY: text}, model_id=MODEL, now=at(1))
            await other.commit()

    await asyncio.gather(replace("three"), replace("four"))
    async with session_factory() as check:
        row = (await check.execute(select(WrittenText))).scalar_one()
        assert {row.text, row.previous[0]["text"]} == {"three", "four"}
        assert len(row.previous) == 3


async def test_the_languages_begun_since_a_moment(session):
    await save_written(session, Reader(None, "fr"), {KEY: "a"}, model_id=MODEL, now=at(0))
    await save_written(session, Reader(None, "de"), {KEY: "b"}, model_id=MODEL, now=at(10))
    await save_written(session, Reader(None, "es"), {KEY: "c"}, model_id=MODEL, now=at(20))
    # French is rewritten later, but it was begun before.
    await save_written(session, Reader(None, "fr"), {OTHER_KEY: "d"}, model_id=MODEL, now=at(30))
    assert await languages_written_since(session, at(5)) == ["de", "es"]
    assert await languages_written_since(session, at(0)) == ["fr", "de", "es"]
    assert await languages_written_since(session, at(40)) == []


async def test_a_card_head_is_one_per_card_and_reader(session, add_person):
    alex = await add_person("Alex", "owner")
    sam = await add_person("Sam", "child")
    conversation = Conversation(person_id=alex.id)
    session.add(conversation)
    await session.flush()
    message = Message(
        conversation_id=conversation.id, role="assistant", status="complete", provenance="model"
    )
    session.add(message)
    await session.flush()
    action = MessageAction(
        message_id=message.id, tool_name="t", action_type="a", status="done", summary={}
    )
    session.add(action)
    await session.flush()

    def head(reader, **card):
        return CardText(reader_id=reader.id, head="Head", model_id=MODEL, written_at=NOW, **card)

    session.add_all(
        [
            head(alex, message_action_id=action.id),
            head(sam, message_action_id=action.id),
        ]
    )
    await session.flush()
    for bad in (
        head(alex, message_action_id=action.id),
        head(alex),
        head(alex, confirmation_id=None),
    ):
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                session.add(bad)
                await session.flush()
