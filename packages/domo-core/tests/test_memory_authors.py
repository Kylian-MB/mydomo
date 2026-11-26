"""A memory may be the assistant's own, a lesson says what it is about (plan 'Domo judges first',
task 3). Tried directly on the shared rolled-back ``session``."""

from datetime import UTC, datetime

import pytest
from domo_core.memory.models import Memory
from domo_core.memory.service import keep, of_domo
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

NOW = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)


async def write(session, said_by=None, *, kind="fact", **more):
    return await keep(
        session,
        said_by=said_by,
        subject="the boiler",
        content="was too hot last winter",
        kind=kind,
        origin="deduced",
        kept_private=False,
        core=False,
        now=NOW,
        **more,
    )


async def test_a_memory_of_the_assistant_s_needs_no_person(session):
    memory = await write(session, None, by_domo=True)
    assert (memory.said_by_id, memory.by_domo) == (None, True)
    found = (await session.scalars(select(Memory).where(of_domo()))).all()
    assert [m.id for m in found] == [memory.id]
    with pytest.raises(ValueError):
        await write(session, None)


async def test_lesson_fields_only_on_a_lesson(session, add_person):
    alex = await add_person("Alex", "adult")
    lesson = await write(
        session,
        alex.id,
        kind="lesson",
        for_everyone=True,
        lesson_action_type="light.set",
        lesson_subject_key="light:hall",
    )
    assert (lesson.for_everyone, lesson.lesson_action_type, lesson.lesson_subject_key) == (
        True,
        "light.set",
        "light:hall",
    )
    for field in (
        {"for_everyone": True},
        {"lesson_action_type": "light.set"},
        {"lesson_subject_key": "light:hall"},
    ):
        with pytest.raises(ValueError):
            await write(session, alex.id, kind="fact", **field)


async def test_the_database_refuses_what_the_service_refuses(session, add_person):
    alex = await add_person("Alex", "adult")
    memory = await write(session, alex.id)
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(
                text("UPDATE memories SET lesson_action_type = 'x' WHERE id = :id"),
                {"id": memory.id},
            )
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(
                text("UPDATE memories SET told_answer = 'yes' WHERE id = :id"), {"id": memory.id}
            )
