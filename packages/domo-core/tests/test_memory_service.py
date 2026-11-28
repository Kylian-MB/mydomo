"""The memories Domo keeps and who sees which (spec 6.1, task 2).

The service is tried directly on the shared rolled-back ``session``."""

from datetime import UTC, datetime, timedelta

import pytest
from domo_core.accounts.models import Person
from domo_core.memory.models import Kind, Memory, Origin
from domo_core.memory.service import (
    SecretRefused,
    change,
    core_of,
    forget,
    get,
    keep,
    mark_served,
    refuses_secret_shapes,
    visible_to,
)
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError

NOW = datetime(2026, 11, 15, 10, 0, tzinfo=UTC)


async def write(
    session,
    said_by: Person,
    content: str = "likes tea",
    *,
    subject: str = "tea",
    kept_private: bool = False,
    core: bool = False,
    kind: Kind = "preference",
    origin: Origin = "own_words",
    now: datetime = NOW,
) -> Memory:
    return await keep(
        session,
        said_by=said_by.id,
        subject=subject,
        content=content,
        kind=kind,
        origin=origin,
        kept_private=kept_private,
        core=core,
        now=now,
    )


async def seen_by(session, person: Person, *, with_kept: bool = False) -> set[str]:
    rows = await session.scalars(
        select(Memory.content).where(visible_to(person.id, with_kept=with_kept))
    )
    return set(rows)


async def test_a_private_memory_is_only_visible_to_who_said_it(session, add_person):
    owner = await add_person("Owner", "owner")
    other = await add_person("Other", "adult")
    await write(session, owner, "the surprise for Other", kept_private=True)
    assert await seen_by(session, owner) == {"the surprise for Other"}
    assert await seen_by(session, other) == set()


async def test_a_shared_memory_is_visible_to_everyone_with_who_said_it(session, add_person):
    owner = await add_person("Owner", "owner")
    guest = await add_person("Guest", "guest")
    memory = await write(session, owner, "the boiler is noisy")
    assert await seen_by(session, owner) == {"the boiler is noisy"}
    assert await seen_by(session, guest) == {"the boiler is noisy"}
    assert (await get(session, memory.id)).said_by_id == owner.id  # type: ignore[union-attr]


async def test_visible_to_with_kept_gives_everything(session, add_person):
    owner = await add_person("Owner", "owner")
    other = await add_person("Other", "adult")
    await write(session, owner, "a secret", kept_private=True)
    await write(session, owner, "no secret")
    assert await seen_by(session, other, with_kept=True) == {"a secret", "no secret"}
    assert await seen_by(session, other) == {"no secret"}


async def test_forget_deletes_for_good(session, add_person):
    owner = await add_person("Owner", "owner")
    other = await add_person("Other", "adult")
    mine = await write(session, owner, "one", kept_private=True)
    theirs = await write(session, other, "two")
    kept = await write(session, other, "three")
    assert await forget(session, [mine.id, theirs.id]) == 2
    assert await forget(session, [mine.id]) == 0
    assert await forget(session, []) == 0
    assert await get(session, mine.id) is None
    assert await session.scalar(select(func.count()).select_from(Memory)) == 1
    assert (await get(session, kept.id)) is kept


async def test_change_keeps_who_said_it_and_where_it_comes_from(session, add_person):
    owner = await add_person("Owner", "owner")
    guest = await add_person("Guest", "guest")
    memory = await write(session, owner, "likes tea", origin="deduced")
    later = NOW + timedelta(hours=1)
    await change(
        session, memory, by=guest.id, now=later, content="likes green tea", kept_private=True
    )
    assert (memory.content, memory.subject, memory.kept_private) == (
        "likes green tea",
        "tea",
        True,
    )
    assert (memory.said_by_id, memory.origin, memory.created_at) == (owner.id, "deduced", NOW)
    assert memory.changed_at == later
    await change(session, memory, by=guest.id, now=later, core=True, kept_private=False)
    assert (memory.core, memory.kept_private, memory.content) == (True, False, "likes green tea")


async def test_the_core_stops_at_its_size_and_says_how_many_are_left_out(session, add_person):
    owner = await add_person("Owner", "owner")
    other = await add_person("Other", "adult")
    # Each weighs 10 characters (subject "s" + content of 9).
    old = await write(session, owner, "old.......", subject="s", core=True)
    served = await write(session, owner, "served....", subject="s", core=True)
    fresh = await write(
        session, owner, "fresh.....", subject="s", core=True, now=NOW + timedelta(minutes=1)
    )
    await write(session, owner, "not core..", subject="s")
    await write(session, other, "theirs....", subject="s", core=True, kept_private=True)
    await session.execute(
        text("UPDATE memories SET created_at = :at WHERE id = :id"),
        {"at": NOW - timedelta(days=3), "id": old.id},
    )
    await session.execute(
        text("UPDATE memories SET created_at = :at WHERE id = :id"),
        {"at": NOW - timedelta(days=2), "id": served.id},
    )
    await session.refresh(old)
    await mark_served(session, [served.id], NOW)
    await session.refresh(served)
    await session.refresh(fresh)
    core = await core_of(session, owner.id, max_chars=1000)
    assert [m.id for m in core.items] == [fresh.id, served.id, old.id]
    assert core.left_out == 0
    core = await core_of(session, owner.id, max_chars=22)
    # Never served counts as served when created: "old" is the least recent and goes.
    assert [m.id for m in core.items] == [fresh.id, served.id]
    assert core.left_out == 1
    # The other person's private core memory is not in anyone else's core.
    assert {m.content for m in (await core_of(session, other.id, max_chars=1000)).items} == {
        "served....",
        "fresh.....",
        "old.......",
        "theirs....",
    }
    assert (await core_of(session, owner.id, max_chars=0)).left_out == 3


@pytest.mark.parametrize(
    "secret",
    [
        "sk-ant-api03-AbC123xyz456DEF789",
        "my key is ghp_1234567890abcdefghijKLMN ok",
        "see https://example.com/join?token=abc",
        "go to http://example.com/x?a=1&code=zz",
        "the code is ABCD-EFGH-JKMN",
        "the code is abcd-efgh-jkmn",
        "the code is abcd efgh jkmn",
        "the code is ABCDEFGHJKMN",
        "the code is abcdefghjk23",
        "ABCD-efgh JKMN",
    ],
)
async def test_a_key_shaped_text_is_refused(session, add_person, secret):
    owner = await add_person("Owner", "owner")
    assert refuses_secret_shapes(secret)
    with pytest.raises(SecretRefused):
        await write(session, owner, secret)
    with pytest.raises(SecretRefused):
        await write(session, owner, "fine", subject=secret[:120])
    assert await session.scalar(select(func.count()).select_from(Memory)) == 0


async def test_a_refused_change_leaves_the_memory_as_it_was(session, add_person):
    owner = await add_person("Owner", "owner")
    memory = await write(session, owner, "likes tea")
    with pytest.raises(SecretRefused):
        await change(session, memory, by=owner.id, now=NOW, content="sk-ant-api03-AbC123xyz456DEF")
    assert memory.content == "likes tea"


@pytest.mark.parametrize(
    "text_",
    [
        "Alex likes tea and lives on 12 rue de la Paix",
        "a long-enough-word-without-digits-anywhere",
        "12345678901234567890123",
        "version 2026-11-15 of the plan",
        "https://example.com/about?page=2",
        "the code of the garage is 4821",
        "that blue door",
        "she is a neighbourhood fan",
        "the word is abcdefghjkmn",
        "Photographers",
    ],
)
def test_ordinary_text_is_not_refused(text_):
    assert not refuses_secret_shapes(text_)


async def test_bounds_are_checked_before_the_database(session, add_person):
    owner = await add_person("Owner", "owner")
    with pytest.raises(ValueError):
        await write(session, owner, "")
    with pytest.raises(ValueError):
        await write(session, owner, "a " * 301)
    with pytest.raises(ValueError):
        await write(session, owner, "ok", subject="")
    with pytest.raises(ValueError):
        await write(session, owner, "ok", subject="   ")
    with pytest.raises(ValueError):
        await write(session, owner, "  \t ")
    with pytest.raises(ValueError):
        await keep(
            session,
            said_by=owner.id,
            subject="s",
            content="c",
            kind="fact",
            origin="own_words",
            kept_private=False,
            core=False,
            now=NOW,
            action_type="note.leave",
        )
    await write(session, owner, "a " * 299 + "a")  # 599 characters: fits


async def test_the_database_refuses_values_the_service_does_not_make(session, add_person):
    owner = await add_person("Owner", "owner")
    with pytest.raises(IntegrityError):
        await session.execute(
            text(
                "INSERT INTO memories (said_by_id, subject, content, kind, origin) "
                "VALUES (:p, 's', 'c', 'rumour', 'own_words')"
            ),
            {"p": owner.id},
        )


async def test_a_memory_is_found_by_its_words(session, add_person):
    owner = await add_person("Owner", "owner")
    await write(session, owner, "The boiler makes a noise", subject="boiler")
    await write(session, owner, "likes tea")
    found = await session.scalar(
        select(Memory.content).where(
            Memory.search.op("@@")(func.plainto_tsquery("simple", "noise"))
        )
    )
    assert found == "The boiler makes a noise"


async def test_a_person_who_is_deleted_leaves_the_shared_memories_they_said(session, add_person):
    owner = await add_person("Owner", "owner")
    gone = await add_person("Gone", "adult")
    shared = await write(session, gone, "shared one")
    private = await write(session, gone, "private one", kept_private=True)
    about = await keep(
        session,
        said_by=owner.id,
        about_id=gone.id,
        subject="Gone",
        content="Gone likes jam",
        kind="fact",
        origin="others_words",
        kept_private=True,
        core=False,
        now=NOW,
    )
    private_id, shared_id, about_id = private.id, shared.id, about.id
    await session.execute(delete(Person).where(Person.id == gone.id))
    session.expunge_all()
    assert await get(session, private_id) is None
    left = await get(session, shared_id)
    assert left is not None
    assert (left.said_by_id, left.content) == (None, "shared one")
    # What someone else said about them stays, and no longer names them.
    about_left = await get(session, about_id)
    assert about_left is not None
    assert (about_left.said_by_id, about_left.about_id) == (owner.id, None)


async def test_a_person_who_only_left_keeps_everything(session, add_person):
    owner = await add_person("Owner", "owner")
    away = await add_person("Away", "guest")
    await write(session, away, "private one", kept_private=True)
    away.disabled_at = NOW
    away.state = "gone"
    await session.flush()
    assert await seen_by(session, owner, with_kept=True) == {"private one"}
