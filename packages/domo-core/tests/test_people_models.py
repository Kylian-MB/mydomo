"""A record of ``persons`` is a person: where they stand, the child mark, a state, an origin."""

import asyncio
from datetime import UTC, datetime

import pytest
from alembic import command
from domo_core.migrate import alembic_config
from domo_core.people.models import (
    ChannelRef,
    MadeRole,
    OwnerDirective,
    PersonAct,
    PersonLink,
    RoleAccess,
)
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from test_migrations import schema_of

NEW_COLUMNS = {
    "standing",
    "is_child",
    "born_on",
    "state",
    "origin",
    "origin_reason",
    "first_access_at",
    "access_returns",
    "merged_into_id",
    "forgotten_at",
    "made_role_id",
}


async def test_a_person_may_exist_without_an_account(session, add_person):
    person = await add_person(
        "Tom",
        "guest",
        state="known",
        origin="domo",
        origin_reason="Alex said Tom comes on Saturday",
    )
    count = await session.execute(
        text("SELECT count(*) FROM accounts WHERE person_id = :id"), {"id": person.id}
    )
    assert count.scalar() == 0
    assert person.standing == "lives_here"
    assert person.is_child is False


async def test_the_builtin_child_role_always_carries_the_mark(add_person):
    with pytest.raises(IntegrityError):
        await add_person("Paul", "child", is_child=False)


async def test_a_record_the_assistant_opened_keeps_his_reason(add_person):
    with pytest.raises(IntegrityError):
        await add_person("Sam", "guest", state="known", origin="domo")


async def test_someone_gone_or_withdrawn_has_a_leaving_date(add_person):
    with pytest.raises(IntegrityError):
        await add_person("Sam", "guest", state="gone")


async def test_someone_withdrawn_has_a_leaving_date(add_person):
    with pytest.raises(IntegrityError):
        await add_person("Sam", "guest", state="withdrawn")


async def test_someone_with_access_has_none(add_person):
    with pytest.raises(IntegrityError):
        await add_person("Dana", "adult", state="has_access", disabled_at=text("now()"))


async def test_upgrade_then_downgrade_leaves_persons_as_before(scratch_database_url):
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0069")
    before = await schema_of(scratch_database_url)
    assert not NEW_COLUMNS & {row[1] for row in before["columns"] if row[0] == "persons"}
    engine = create_async_engine(scratch_database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO persons (display_name, role, created_at) VALUES "
                    "('Alex', 'owner', now()), ('Paul', 'child', now())"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO persons (display_name, role, disabled_at) VALUES "
                    "('Sam', 'guest', now())"
                )
            )
        await asyncio.to_thread(command.upgrade, config, "0070")
        async with engine.connect() as connection:
            rows = {
                row[0]: row[1:]
                for row in await connection.execute(
                    text(
                        "SELECT display_name, is_child, state, origin, "
                        "first_access_at IS NOT NULL FROM persons"
                    )
                )
            }
    finally:
        await engine.dispose()
    assert rows["Alex"] == (False, "has_access", "setup", True)
    assert rows["Paul"] == (True, "has_access", "invitation", True)
    assert rows["Sam"] == (False, "gone", "invitation", False)
    await asyncio.to_thread(command.downgrade, config, "0069")
    assert await schema_of(scratch_database_url) == before


PEOPLE_TABLES = {
    "roles",
    "role_accesses",
    "person_links",
    "access_promises",
    "owner_directives",
    "record_merges",
    "person_acts",
    "person_channel_refs",
}


async def test_the_people_tables_come_and_go_with_their_migration(
    scratch_database_url, list_tables
):
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0070")
    before = await schema_of(scratch_database_url)
    assert not PEOPLE_TABLES & await list_tables(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0071")
    assert await list_tables(scratch_database_url) >= PEOPLE_TABLES
    await asyncio.to_thread(command.downgrade, config, "0070")
    assert not PEOPLE_TABLES & await list_tables(scratch_database_url)
    assert await schema_of(scratch_database_url) == before


async def test_everything_about_a_person_points_at_persons(session):
    """One world: a column of these tables that names a person points at ``persons.id``, never
    at a second table of people."""
    rows = await session.execute(
        text(
            "SELECT conrelid::regclass::text, a.attname, confrelid::regclass::text, "
            "fa.attname FROM pg_constraint c "
            "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1] "
            "JOIN pg_attribute fa ON fa.attrelid = c.confrelid AND fa.attnum = c.confkey[1] "
            "WHERE c.contype = 'f' AND c.conrelid::regclass::text = ANY(:tables)"
        ),
        {"tables": sorted(PEOPLE_TABLES)},
    )
    # An act's invitation is the link it made, not a person (someone new has no record yet).
    not_about_a_person = {"role_id", "made_role_id", "promise_id", "directive_id", "invitation_id"}
    rows = rows.all()
    # The act's invitation is checked too: it points at an invitation, nothing else.
    invitation = [row[2:] for row in rows if row[:2] == ("person_acts", "invitation_id")]
    assert invitation == [("invitations", "id")]
    about_a_person = [row for row in rows if row[1] not in not_about_a_person]
    assert len(about_a_person) >= 10
    for table, column, target, target_column in about_a_person:
        assert (target, target_column) == ("persons", "id"), f"two worlds: {table}.{column}"
    tables = await session.execute(text("SELECT to_regclass('records')"))
    assert tables.scalar() is None


@pytest.mark.parametrize("key", ["owner", "adult", "child", "guest"])
async def test_a_made_role_cannot_take_a_builtin_name(session, key):
    session.add(MadeRole(name=key, name_key=key, base="adult", created_by="domo", reason="test"))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_a_made_role_name_is_unique_while_it_lives(session):
    def made(**columns: object) -> MadeRole:
        return MadeRole(
            name="Cook", name_key="cook", base="guest", created_by="domo", reason="test", **columns
        )

    session.add(made(deleted_at=datetime.now(UTC)))
    session.add(made())
    await session.flush()
    session.add(made())
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_one_directive_in_force_per_kind_and_person(session, add_person):
    alex = await add_person("Alex", "owner")
    sam = await add_person("Sam", "guest", state="known", origin="invitation")

    def directive(kind: str, person_id: object, **columns: object) -> OwnerDirective:
        return OwnerDirective(kind=kind, person_id=person_id, set_by=alex.id, **columns)

    session.add_all([directive("withdraw", sam.id), directive("no_new_people", None)])
    await session.flush()
    # A lifted one does not count; another kind or another person is not the same directive.
    session.add_all(
        [
            directive("withdraw", sam.id, lifted_at=datetime.now(UTC), lifted_by=alex.id),
            directive("refuse", sam.id),
        ]
    )
    await session.flush()
    session.add(directive("no_new_people", None))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_a_directive_about_a_person_names_one(session, add_person):
    alex = await add_person("Alex", "owner")
    session.add(OwnerDirective(kind="withdraw", person_id=None, set_by=alex.id))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_a_link_joins_two_different_records(session, add_person):
    alex = await add_person("Alex", "owner")
    dana = await add_person("Dana", "adult")
    session.add(PersonLink(from_id=dana.id, to_id=alex.id, relation="mother of"))
    await session.flush()
    session.add(PersonLink(from_id=dana.id, to_id=dana.id, relation="same as"))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_the_same_link_is_told_once(session, add_person):
    alex = await add_person("Alex", "owner")
    dana = await add_person("Dana", "adult")
    session.add(PersonLink(from_id=dana.id, to_id=alex.id, relation="mother of"))
    await session.flush()
    session.add(PersonLink(from_id=dana.id, to_id=alex.id, relation="mother of"))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_an_author_is_the_assistant_or_a_person(session, add_person):
    alex = await add_person("Alex", "owner")
    session.add(PersonAct(act="opened", person_id=alex.id, by="domo"))
    session.add(PersonAct(act="opened", person_id=alex.id, by=f"person:{alex.id}"))
    await session.flush()
    session.add(PersonAct(act="opened", person_id=alex.id, by="somebody"))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_a_channel_reference_belongs_to_one_person_and_one_channel(session, add_person):
    dana = await add_person("Dana", "adult")
    paul = await add_person("Paul", "child")
    session.add_all(
        [
            ChannelRef(person_id=dana.id, channel="messages", reference="r1"),
            ChannelRef(person_id=dana.id, channel="messages", reference="r2"),
        ]
    )
    await session.flush()
    session.add(ChannelRef(person_id=paul.id, channel="messages", reference="r1"))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_a_role_goes_with_its_accesses(session):
    role = MadeRole(name="Cook", name_key="cook", base="guest", created_by="domo", reason="test")
    session.add(role)
    await session.flush()
    session.add(RoleAccess(role_id=role.id, action_type="devices.use", access="allow"))
    await session.flush()
    session.add(RoleAccess(role_id=role.id, action_type="devices.use", access="deny"))
    with pytest.raises(IntegrityError):
        await session.flush()
