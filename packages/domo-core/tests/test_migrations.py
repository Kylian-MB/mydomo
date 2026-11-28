import asyncio
import io
from pathlib import Path

import domo_core
import pytest
from alembic import command
from alembic.script import ScriptDirectory
from domo_core.accounts.models import Person
from domo_core.db import Base, load_models
from domo_core.migrate import MIGRATIONS_DIR, alembic_config, upgrade_to_head
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

ALL_TABLES = {
    "persons",
    "accounts",
    "web_sessions",
    "setup_state",
    "providers",
    "conversations",
    "messages",
    "audit_log",
    "alembic_version",
    "invitations",
    "confirmations",
    "message_actions",
    "notes",
    "webauthn_credentials",
    "webauthn_challenges",
    "policy_settings",
    "policy_rights",
    "policy_rights_drafts",
    "successors",
    "confirmation_steps",
    "provider_models",
    "provider_users",
    "model_levels",
    "model_choices",
    "model_lessons",
    "model_calls",
    "spending_limits",
    "key_newcomers",
    "real_spend",
    "exchange_rates",
    "catalog_snapshots",
    "written_texts",
    "card_texts",
    "identity_clues",
    "proof_means",
    "devices",
    "device_pairings",
    "memories",
    "plans",
    "deliveries",
    "person_nicknames",
    "trust_facts",
    "made_tools",
    "made_tool_versions",
    "hand_runs",
    "made_tool_triggers",
    "made_tool_runs",
    "action_kinds",
    "tool_builds",
    "tools_spaces",
    "launch_stops",
    "standing_agreements",
    "declared_spending",
    "tool_settings",
    "made_tool_launches",
    "tool_trouble_days",
    "open_addresses",
    "went_wrong",
    "service_accounts",
    "roles",
    "role_accesses",
    "person_links",
    "access_promises",
    "owner_directives",
    "record_merges",
    "person_acts",
    "person_channel_refs",
    "voice_refs",
    "voice_prints",
    "voice_moments",
}
# The tables of iteration 1a: what the database holds one revision before the household.
TABLES_OF_1A = {
    "persons",
    "accounts",
    "web_sessions",
    "setup_state",
    "providers",
    "conversations",
    "messages",
    "audit_log",
    "alembic_version",
}


async def test_migrations_create_all_tables(database_url, list_tables):
    tables = await list_tables(database_url)
    assert tables == ALL_TABLES


async def test_single_owner_constraint(session):
    session.add_all(
        [Person(display_name="A", role="owner"), Person(display_name="B", role="owner")]
    )
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_other_roles_are_not_limited(session):
    session.add_all([Person(display_name=name, role="guest") for name in "ABC"])
    session.add(Person(display_name="D", role="owner"))
    await session.flush()


async def test_vector_extension_installed(session):
    assert (
        await session.execute(text("SELECT 1 FROM pg_extension WHERE extname='vector'"))
    ).scalar() == 1


async def schema_of(url: str) -> dict[str, list[tuple[object, ...]]]:
    """Everything that defines the tables of a database, as the catalog states it."""
    queries = {
        "columns": (
            "SELECT table_name, column_name, data_type, is_nullable, column_default "
            "FROM information_schema.columns WHERE table_schema = 'public' "
            "AND table_name <> 'alembic_version' ORDER BY table_name, column_name"
        ),
        "constraints": (
            "SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid) "
            "FROM pg_constraint WHERE connamespace = 'public'::regnamespace "
            "AND conrelid::regclass::text <> 'alembic_version' ORDER BY 1, 2"
        ),
        "indexes": (
            "SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' "
            "AND tablename <> 'alembic_version' ORDER BY 1, 2"
        ),
    }
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            return {
                name: [tuple(row) for row in await connection.execute(text(query))]
                for name, query in queries.items()
            }
    finally:
        await engine.dispose()


async def test_the_migration_creates_what_the_models_declare(database_url, scratch_database_url):
    """No drift: the schema the migration made is the one SQLAlchemy would create from the
    models (columns, defaults, constraints with their names and expressions, indexes)."""
    load_models()
    engine = create_async_engine(scratch_database_url)
    try:
        async with engine.begin() as connection:
            # The first migration creates the extension; the models only use its type.
            await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await connection.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()
    from_models = await schema_of(scratch_database_url)
    from_migration = await schema_of(database_url)
    assert from_migration["columns"]  # the comparison below is not between two empty schemas
    for part in ("columns", "constraints", "indexes"):
        assert from_migration[part] == from_models[part], part


async def test_downgrade_removes_everything_and_upgrade_can_run_again(
    scratch_database_url, list_tables
):
    config = alembic_config(scratch_database_url)
    # Alembic starts its own event loop: from async code it has to run in a thread.
    await asyncio.to_thread(command.upgrade, config, "head")
    assert await list_tables(scratch_database_url) == ALL_TABLES
    await asyncio.to_thread(command.downgrade, config, "base")
    assert await list_tables(scratch_database_url) == {"alembic_version"}
    await asyncio.to_thread(command.upgrade, config, "head")
    assert await list_tables(scratch_database_url) == ALL_TABLES


async def test_downgrading_one_revision_gives_back_the_schema_of_1a(
    scratch_database_url, list_tables
):
    """The household migration undoes itself cleanly: the new tables go, and the people table
    loses its new columns and its role constraint (a "member" is a role 1a could store)."""
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0002")
    before = await schema_of(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "head")
    assert await list_tables(scratch_database_url) == ALL_TABLES
    await asyncio.to_thread(command.downgrade, config, "0002")
    assert await list_tables(scratch_database_url) == TABLES_OF_1A
    assert await schema_of(scratch_database_url) == before


async def test_the_notes_gain_when_they_were_seen_and_when_they_were_taken_back(
    scratch_database_url,
):
    """Both are empty until something happens to a note: an old note keeps the two empty."""
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0010")
    before = await schema_of(scratch_database_url)
    note_columns = {row[1] for row in before["columns"] if row[0] == "notes"}
    assert not {"seen_at", "withdrawn_at"} & note_columns
    await asyncio.to_thread(command.upgrade, config, "head")
    after = await schema_of(scratch_database_url)
    added = [
        row
        for row in after["columns"]
        if row[0] == "notes" and row[1] in {"seen_at", "withdrawn_at"}
    ]
    assert sorted(added) == [
        ("notes", "seen_at", "timestamp with time zone", "YES", None),
        ("notes", "withdrawn_at", "timestamp with time zone", "YES", None),
    ]
    await asyncio.to_thread(command.downgrade, config, "0010")
    assert await schema_of(scratch_database_url) == before


async def test_the_invitations_gain_the_person_a_return_link_brings_back(scratch_database_url):
    """An ordinary invitation keeps the column empty: only a return link names a person."""
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0011")
    before = await schema_of(scratch_database_url)
    assert "person_id" not in {row[1] for row in before["columns"] if row[0] == "invitations"}
    await asyncio.to_thread(command.upgrade, config, "head")
    after = await schema_of(scratch_database_url)
    added = [row for row in after["columns"] if row[:2] == ("invitations", "person_id")]
    assert added == [("invitations", "person_id", "uuid", "YES", None)]
    assert any(
        row[1] == "fk_invitations_person_id_persons" and "REFERENCES persons(id)" in row[2]
        for row in after["constraints"]
    )
    await asyncio.to_thread(command.downgrade, config, "0011")
    assert await schema_of(scratch_database_url) == before


async def test_going_back_down_cancels_the_return_links_that_wait(scratch_database_url):
    """Without the column nothing would tell a return link from an invitation that makes a new
    person: the links that wait are cancelled, not turned into the other kind."""
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "head")
    engine = create_async_engine(scratch_database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO persons (id, display_name, role, disabled_at, state) VALUES "
                    "('00000000-0000-0000-0000-000000000001', 'Owner', 'owner', NULL, "
                    "'has_access'), "
                    "('00000000-0000-0000-0000-000000000002', 'Away', 'adult', now(), 'gone')"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO invitations (token_hash, display_name, role, created_by, "
                    "expires_at, person_id, kind) VALUES "
                    "('h1', 'Away', 'adult', '00000000-0000-0000-0000-000000000001', "
                    "now() + interval '7 days', '00000000-0000-0000-0000-000000000002', "
                    "'return'), "
                    "('h2', 'New', 'adult', '00000000-0000-0000-0000-000000000001', "
                    "now() + interval '7 days', NULL, 'new')"
                )
            )
        await asyncio.to_thread(command.downgrade, config, "0011")
        async with engine.connect() as connection:
            rows = await connection.execute(
                text(
                    "SELECT display_name, cancelled_at IS NOT NULL FROM invitations "
                    "ORDER BY display_name"
                )
            )
            assert [tuple(row) for row in rows] == [("Away", True), ("New", False)]
    finally:
        await engine.dispose()


async def test_the_rights_come_with_the_owner_as_one_who_says_yes(scratch_database_url):
    """The table of the rights, with one right in force per subject and action type, and the owner
    as a new kind of approver of a request."""
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0012")
    before = await schema_of(scratch_database_url)
    assert "policy_rights" not in {row[0] for row in before["columns"]}
    await asyncio.to_thread(command.upgrade, config, "head")
    after = await schema_of(scratch_database_url)
    [approver_kinds] = [
        row[2] for row in after["constraints"] if row[1] == "ck_confirmations_approver_kind_values"
    ]
    assert "'owner'" in approver_kinds
    [in_force] = [
        row[2] for row in after["indexes"] if row[1] == "uq_policy_rights_subject_action_type"
    ]
    assert "UNIQUE" in in_force and "NULLS NOT DISTINCT" in in_force
    assert "WHERE (revoked_at IS NULL)" in in_force
    await asyncio.to_thread(command.downgrade, config, "0012")
    assert await schema_of(scratch_database_url) == before


async def test_going_back_down_drops_the_requests_that_wait_for_the_owner(scratch_database_url):
    """Before the rights, nothing could tell such a request from another: one that still waits
    is dropped (expired), never handed to someone else to approve; the others keep their state and
    read as an adult's (someone other than the requester said yes)."""
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "head")
    engine = create_async_engine(scratch_database_url)
    owner, adult = "00000000-0000-0000-0000-000000000001", "00000000-0000-0000-0000-000000000002"
    conversation = "00000000-0000-0000-0000-000000000003"
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    f"INSERT INTO persons (id, display_name, role) VALUES "
                    f"('{owner}', 'Owner', 'owner'), ('{adult}', 'Alex', 'adult')"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO conversations (id, person_id) "
                    f"VALUES ('{conversation}', '{adult}')"
                )
            )
            for tool, kind, status in (
                ("waits", "owner", "pending"),
                ("done", "owner", "executed"),
                ("mine", "self", "pending"),
            ):
                await connection.execute(
                    text(
                        "INSERT INTO confirmations (requester_id, approver_kind, conversation_id, "
                        "tool_name, arguments, action, ask, asked_by, status) VALUES "
                        f"('{adult}', '{kind}', '{conversation}', '{tool}', '{{}}', '{{}}', "
                        f"'yes', 'judge', '{status}')"
                    )
                )
            await connection.execute(
                text(
                    "INSERT INTO policy_rights (person_id, action_type, access, summary, "
                    f"owner_words, created_by) VALUES ('{adult}', 'household.invite', 'allow', "
                    f"'Alex may invite', '', '{owner}')"
                )
            )
        await asyncio.to_thread(command.downgrade, config, "0012")
        async with engine.connect() as connection:
            rows = await connection.execute(
                text(
                    "SELECT tool_name, approver_kind, status, expired_by FROM confirmations "
                    "ORDER BY tool_name"
                )
            )
            assert [tuple(row) for row in rows] == [
                ("done", "adult", "executed", None),
                ("mine", "self", "pending", None),
                ("waits", "adult", "expired", "domo"),
            ]
    finally:
        await engine.dispose()


async def test_the_limits_keep_what_domo_could_not_tell_and_stays_up_to_a_year(
    scratch_database_url,
):
    """A request keeps the question Domo could not answer, and a guest's stay goes up to a year
    (until ``0074`` takes the ceiling away: ``test_people_stays.py``). Going back down brings a
    longer stay back to 30 days: shorter than allowed, never longer."""
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0013")
    before = await schema_of(scratch_database_url)
    assert "ask_reason" not in {row[1] for row in before["columns"] if row[0] == "confirmations"}
    await asyncio.to_thread(command.upgrade, config, "0073")
    after = await schema_of(scratch_database_url)
    added = [row for row in after["columns"] if row[:2] == ("confirmations", "ask_reason")]
    assert added == [("confirmations", "ask_reason", "text", "YES", None)]
    [stay] = [row[2] for row in after["constraints"] if row[1] == "ck_invitations_guest_days_range"]
    assert "365" in stay and "30)" not in stay
    engine = create_async_engine(scratch_database_url)
    owner = "00000000-0000-0000-0000-000000000001"
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    f"INSERT INTO persons (id, display_name, role) VALUES ('{owner}', 'O', 'owner')"
                )
            )
            for name, days in (("Long", 365), ("Short", 3)):
                await connection.execute(
                    text(
                        "INSERT INTO invitations (token_hash, display_name, role, guest_days, "
                        f"created_by, expires_at) VALUES ('{name}', '{name}', 'guest', {days}, "
                        f"'{owner}', now())"
                    )
                )
        await asyncio.to_thread(command.downgrade, config, "0013")
        async with engine.connect() as connection:
            rows = await connection.execute(
                text("SELECT display_name, guest_days FROM invitations ORDER BY display_name")
            )
            assert [tuple(row) for row in rows] == [("Long", 30), ("Short", 3)]
    finally:
        await engine.dispose()
    assert await schema_of(scratch_database_url) == before


async def test_the_drafts_of_rights_come_with_their_table(scratch_database_url, list_tables):
    """A draft of rights is kept in a table of its own, and a right says which draft it came
    from. Going back down drops both, and gives back the schema of 0014."""
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0014")
    before = await schema_of(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0015")
    assert "policy_rights_drafts" in await list_tables(scratch_database_url)
    after = await schema_of(scratch_database_url)
    [link] = [
        row[2]
        for row in after["constraints"]
        if row[1] == "fk_policy_rights_draft_id_policy_rights_drafts"
    ]
    assert link == "FOREIGN KEY (draft_id) REFERENCES policy_rights_drafts(id)"
    await asyncio.to_thread(command.downgrade, config, "0014")
    assert "policy_rights_drafts" not in await list_tables(scratch_database_url)
    assert await schema_of(scratch_database_url) == before


async def test_an_invitation_may_wait_for_its_link_and_going_back_cancels_those(
    scratch_database_url,
):
    """An invitation says yes to its token later: the hash is empty until the one who asked shows
    the link (and every hash that is there is still its own). Going back down gives every
    invitation a hash again: those that never had a token cannot be used by anyone, so they are
    cancelled, and keep a hash that is no link's."""
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0015")
    before = await schema_of(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "head")
    after = await schema_of(scratch_database_url)
    assert [row for row in after["columns"] if row[:2] == ("invitations", "token_hash")] == [
        ("invitations", "token_hash", "text", "YES", None)
    ]
    assert any(
        row[1] == "uq_invitations_token_hash" and "UNIQUE (token_hash)" in row[2]
        for row in after["constraints"]
    )
    owner = "00000000-0000-0000-0000-000000000001"
    engine = create_async_engine(scratch_database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    f"INSERT INTO persons (id, display_name, role) VALUES ('{owner}', 'O', 'owner')"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO invitations (token_hash, display_name, role, created_by, "
                    "expires_at, accepted_at) VALUES "
                    f"('h1', 'Has', 'adult', '{owner}', now() + interval '7 days', NULL), "
                    f"(NULL, 'Waits', 'adult', '{owner}', now() + interval '7 days', NULL), "
                    f"(NULL, 'Also', 'adult', '{owner}', now() + interval '7 days', NULL), "
                    f"(NULL, 'Used', 'adult', '{owner}', now() + interval '7 days', now())"
                )
            )
        await asyncio.to_thread(command.downgrade, config, "0015")
        async with engine.connect() as connection:
            rows = await connection.execute(
                text(
                    "SELECT display_name, token_hash, cancelled_at IS NOT NULL FROM invitations "
                    "ORDER BY display_name"
                )
            )
            kept = [tuple(row) for row in rows]
    finally:
        await engine.dispose()
    assert [(name, cancelled) for name, _, cancelled in kept] == [
        ("Also", True),
        ("Has", False),
        ("Used", False),  # used already: nothing to cancel
        ("Waits", True),
    ]
    hashes = [token_hash for _, token_hash, _ in kept]
    assert "h1" in hashes and None not in hashes and len(set(hashes)) == len(hashes)
    assert await schema_of(scratch_database_url) == before


async def test_the_successors_come_with_their_table_and_going_back_drops_it(
    scratch_database_url, list_tables
):
    """The owner's list of successors is a table of its own: one rank per row, a person at most
    once, and a row is a person of the household or a name. Going back down drops it (the list goes
    with its table) and gives back the schema of 0016."""
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0016")
    before = await schema_of(scratch_database_url)
    assert "successors" not in await list_tables(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0017")
    assert "successors" in await list_tables(scratch_database_url)
    after = await schema_of(scratch_database_url)
    constraints = {row[1]: row[2] for row in after["constraints"] if row[0] == "successors"}
    assert constraints["uq_successors_rank"] == "UNIQUE (rank)"
    assert constraints["uq_successors_person_id"] == "UNIQUE (person_id)"
    assert (
        constraints["fk_successors_person_id_persons"]
        == "FOREIGN KEY (person_id) REFERENCES persons(id)"
    )
    assert "(person_id IS NULL) <> (name IS NULL)" in constraints["ck_successors_one_who"]
    assert "'personal_access'" in constraints["ck_successors_access_way_values"]
    assert "'paper_code'" in constraints["ck_successors_access_way_values"]
    await asyncio.to_thread(command.downgrade, config, "0016")
    assert "successors" not in await list_tables(scratch_database_url)
    assert await schema_of(scratch_database_url) == before


async def test_a_row_of_the_list_is_a_person_or_a_name_with_its_own_rank(scratch_database_url):
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "head")
    engine = create_async_engine(scratch_database_url)
    owner = "00000000-0000-0000-0000-000000000001"
    adult = "00000000-0000-0000-0000-000000000002"
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO persons (id, display_name, role) VALUES "
                    f"('{owner}', 'Owner', 'owner'), ('{adult}', 'Alex', 'adult')"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO successors (rank, person_id, name) VALUES "
                    f"(1, '{adult}', NULL), (2, NULL, 'Paul')"
                )
            )
        for statement in (
            # a rank is used once
            "INSERT INTO successors (rank, name) VALUES (2, 'Lou')",
            # a person is listed once
            f"INSERT INTO successors (rank, person_id) VALUES (3, '{adult}')",
            # a person or a name, never both and never none
            f"INSERT INTO successors (rank, person_id, name) VALUES (3, '{owner}', 'Lou')",
            "INSERT INTO successors (rank) VALUES (3)",
            # the ways to open the door
            "INSERT INTO successors (rank, name, access_way) VALUES (3, 'Lou', 'key')",
            # the lengths
            f"INSERT INTO successors (rank, name) VALUES (3, '{'x' * 61}')",
            "INSERT INTO successors (rank, name) VALUES (3, '')",
            f"INSERT INTO successors (rank, name, relation) VALUES (3, 'Lou', '{'x' * 201}')",
            # a rank counts from 1
            "INSERT INTO successors (rank, name) VALUES (0, 'Lou')",
        ):
            with pytest.raises(IntegrityError):
                async with engine.begin() as connection:
                    await connection.execute(text(statement))
        async with engine.connect() as connection:
            rows = await connection.execute(text("SELECT rank, name FROM successors ORDER BY rank"))
            assert [tuple(row) for row in rows] == [(1, None), (2, "Paul")]
    finally:
        await engine.dispose()


async def test_the_steps_of_a_chain_come_with_their_table_and_going_back_drops_them(
    scratch_database_url, list_tables
):
    """A chain is a request with its steps in a table of their own (one row per position, in one
    of four states) and, on the request, what it remembers of the one who asked
    (``chain_basis``). Going back down drops both, and gives back the schema of 0017."""
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0017")
    before = await schema_of(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0018")
    assert "confirmation_steps" in await list_tables(scratch_database_url)
    after = await schema_of(scratch_database_url)
    constraints = {row[1]: row[2] for row in after["constraints"] if row[0] == "confirmation_steps"}
    assert constraints["pk_confirmation_steps"] == 'PRIMARY KEY (confirmation_id, "position")'
    assert (
        constraints["fk_confirmation_steps_confirmation_id_confirmations"]
        == "FOREIGN KEY (confirmation_id) REFERENCES confirmations(id)"
    )
    for status in ("planned", "done", "failed", "stopped"):
        assert f"'{status}'" in constraints["ck_confirmation_steps_status_values"]
    assert ("confirmations", "chain_basis", "jsonb", "YES", None) in after["columns"]
    await asyncio.to_thread(command.downgrade, config, "0017")
    assert "confirmation_steps" not in await list_tables(scratch_database_url)
    assert await schema_of(scratch_database_url) == before


# A person and their conversation, for the requests below.
_OWNER = "00000000-0000-0000-0000-000000000001"
_CONVERSATION = "00000000-0000-0000-0000-000000000003"
_BASIS = """'{"role": "owner", "right_ids": []}'"""


async def _owner_and_conversation(connection) -> None:
    await connection.execute(
        text(f"INSERT INTO persons (id, display_name, role) VALUES ('{_OWNER}', 'O', 'owner')")
    )
    await connection.execute(
        text(f"INSERT INTO conversations (id, person_id) VALUES ('{_CONVERSATION}', '{_OWNER}')")
    )


async def test_going_back_down_drops_the_chains_that_wait(scratch_database_url):
    """Before the chains, nothing could carry out a request of several steps: one that still waits
    is dropped (expired by Domo), never run as something else; the other requests keep their
    state."""
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "head")
    engine = create_async_engine(scratch_database_url)
    try:
        async with engine.begin() as connection:
            await _owner_and_conversation(connection)
            for tool, basis, status in (
                ("chain", _BASIS, "pending"),
                ("chain_done", _BASIS, "failed"),
                ("single", "NULL", "pending"),
            ):
                await connection.execute(
                    text(
                        "INSERT INTO confirmations (requester_id, approver_kind, conversation_id, "
                        "tool_name, arguments, action, ask, asked_by, status, chain_basis) "
                        f"VALUES ('{_OWNER}', 'self', '{_CONVERSATION}', '{tool}', '{{}}', "
                        f"'{{}}', 'yes', 'judge', '{status}', {basis})"
                    )
                )
            await connection.execute(
                text(
                    "INSERT INTO confirmation_steps (confirmation_id, position, tool_name, "
                    "action_type, arguments, status) SELECT id, 0, 'household_list', "
                    "'household.list', '{}', 'planned' FROM confirmations WHERE tool_name = 'chain'"
                )
            )
        await asyncio.to_thread(command.downgrade, config, "0017")
        async with engine.connect() as connection:
            rows = await connection.execute(
                text("SELECT tool_name, status, expired_by FROM confirmations ORDER BY tool_name")
            )
            assert [tuple(row) for row in rows] == [
                ("chain", "expired", "domo"),
                ("chain_done", "failed", None),
                ("single", "pending", None),
            ]
    finally:
        await engine.dispose()


async def test_a_step_has_one_position_in_its_chain_and_a_known_state(scratch_database_url):
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "head")
    engine = create_async_engine(scratch_database_url)
    request = "00000000-0000-0000-0000-000000000004"
    columns = "(confirmation_id, position, tool_name, action_type, arguments, status)"
    try:
        async with engine.begin() as connection:
            await _owner_and_conversation(connection)
            await connection.execute(
                text(
                    "INSERT INTO confirmations (id, requester_id, approver_kind, conversation_id, "
                    "tool_name, arguments, action, ask, asked_by, status, chain_basis) VALUES "
                    f"('{request}', '{_OWNER}', 'self', '{_CONVERSATION}', 'do_in_order', '{{}}', "
                    f"'{{}}', 'yes', 'judge', 'pending', {_BASIS})"
                )
            )
            await connection.execute(
                text(
                    f"INSERT INTO confirmation_steps {columns} VALUES "
                    f"('{request}', 0, 'household_list', 'household.list', '{{}}', 'planned')"
                )
            )
        for statement in (
            # one step per position
            f"INSERT INTO confirmation_steps {columns} VALUES "
            f"('{request}', 0, 'household_list', 'household.list', '{{}}', 'planned')",
            # a position counts from 0
            f"INSERT INTO confirmation_steps {columns} VALUES "
            f"('{request}', -1, 'household_list', 'household.list', '{{}}', 'planned')",
            # the states
            f"INSERT INTO confirmation_steps {columns} VALUES "
            f"('{request}', 1, 'household_list', 'household.list', '{{}}', 'running')",
            # a step belongs to a request that exists
            f"INSERT INTO confirmation_steps {columns} VALUES "
            f"('{_OWNER}', 1, 'household_list', 'household.list', '{{}}', 'planned')",
        ):
            with pytest.raises(IntegrityError):
                async with engine.begin() as connection:
                    await connection.execute(text(statement))
    finally:
        await engine.dispose()


async def test_the_steps_of_a_chain_keep_their_level(scratch_database_url):
    """Each step of a chain keeps the level the barrier gave it when the chain was asked for
    (``NULL`` for a step checked in its turn, and for the steps of before), one of the three
    levels; and whether it is judged with what the chain remembers of the one who asked
    (``with_basis``, true unless said otherwise). The chain keeps its own level as well
    (``chain_level``). Going back down drops the three, and gives back the schema of 0018."""
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0018")
    before = await schema_of(scratch_database_url)
    engine = create_async_engine(scratch_database_url)
    request = "00000000-0000-0000-0000-000000000005"
    columns = "(confirmation_id, position, tool_name, action_type, arguments, status)"
    try:
        async with engine.begin() as connection:
            await _owner_and_conversation(connection)
            await connection.execute(
                text(
                    "INSERT INTO confirmations (id, requester_id, approver_kind, conversation_id, "
                    "tool_name, arguments, action, level, status, chain_basis) VALUES "
                    f"('{request}', '{_OWNER}', 'self', '{_CONVERSATION}', 'do_in_order', '{{}}', "
                    f"'{{}}', 'confirm', 'pending', {_BASIS})"
                )
            )
            # A step of before the levels.
            await connection.execute(
                text(
                    f"INSERT INTO confirmation_steps {columns} VALUES "
                    f"('{request}', 0, 'household_list', 'household.list', '{{}}', 'planned')"
                )
            )
        await asyncio.to_thread(command.upgrade, config, "0019")
        after = await schema_of(scratch_database_url)
        assert ("confirmation_steps", "level", "text", "YES", None) in after["columns"]
        assert (
            "confirmation_steps",
            "with_basis",
            "boolean",
            "NO",
            "true",
        ) in after["columns"]
        constraints = {
            row[1]: row[2] for row in after["constraints"] if row[0] == "confirmation_steps"
        }
        for level in ("free", "confirm", "always_confirm"):
            assert f"'{level}'" in constraints["ck_confirmation_steps_level_values"]
        async with engine.begin() as connection:
            rows = await connection.execute(
                text("SELECT level, with_basis FROM confirmation_steps")
            )
            assert [tuple(row) for row in rows] == [(None, True)]
            for position, level in ((1, "free"), (2, "confirm"), (3, "always_confirm")):
                await connection.execute(
                    text(
                        f"INSERT INTO confirmation_steps {columns[:-1]}, level) VALUES "
                        f"('{request}', {position}, 'household_list', 'household.list', '{{}}', "
                        f"'planned', '{level}')"
                    )
                )
        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        f"INSERT INTO confirmation_steps {columns[:-1]}, level) VALUES "
                        f"('{request}', 4, 'household_list', 'household.list', '{{}}', "
                        "'planned', 'never')"
                    )
                )
        # The chain itself keeps its own level too: none for a chain of before.
        assert ("confirmations", "chain_level", "text", "YES", None) in after["columns"]
        checks = {row[1]: row[2] for row in after["constraints"] if row[0] == "confirmations"}
        for level in ("free", "confirm", "always_confirm"):
            assert f"'{level}'" in checks["ck_confirmations_chain_level_values"]
        async with engine.begin() as connection:
            stored = await connection.execute(text("SELECT chain_level FROM confirmations"))
            assert [row[0] for row in stored] == [None]
            await connection.execute(text("UPDATE confirmations SET chain_level = 'free'"))
        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(text("UPDATE confirmations SET chain_level = 'never'"))
        await asyncio.to_thread(command.downgrade, config, "0018")
        assert await schema_of(scratch_database_url) == before
        async with engine.connect() as connection:
            rows = await connection.execute(text("SELECT position FROM confirmation_steps"))
            assert sorted(row[0] for row in rows) == [0, 1, 2, 3]
    finally:
        await engine.dispose()


async def test_a_chain_may_wait_for_an_arrival(scratch_database_url):
    """A step of a chain may wait for someone's arrival (``after_arrival_of``, the name of an
    invitation, 60 characters at most), and a chain may pause until then (``waiting_arrival``,
    with the invitation it waits for, ``waiting_for_invitation_id``). Going back down expires (by
    Domo) what the code of before would get wrong: a paused chain, which it never resumes, and a
    chain whose wait is still to come that a yes or a continuation would run at once (``pending``,
    or ``failed`` at the wait); then it drops the columns, and gives back the schema of 0019."""
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0019")
    before = await schema_of(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0020")
    after = await schema_of(scratch_database_url)
    assert ("confirmation_steps", "after_arrival_of", "text", "YES", None) in after["columns"]
    assert ("confirmations", "waiting_for_invitation_id", "uuid", "YES", None) in after["columns"]
    engine = create_async_engine(scratch_database_url)
    invitation = "00000000-0000-0000-0000-000000000006"
    paused = "00000000-0000-0000-0000-000000000007"
    columns = (
        "(confirmation_id, position, tool_name, action_type, arguments, status, after_arrival_of)"
    )
    try:
        async with engine.begin() as connection:
            await _owner_and_conversation(connection)
            await connection.execute(
                text(
                    "INSERT INTO invitations (id, display_name, role, created_by, expires_at) "
                    f"VALUES ('{invitation}', 'Paul', 'adult', '{_OWNER}', now())"
                )
            )
            # Each request, its state, and the state of its step that waits (``None``: none).
            for request, status, wait in _REQUESTS_AROUND_A_WAIT:
                await connection.execute(
                    text(
                        "INSERT INTO confirmations (id, requester_id, approver_kind, "
                        "conversation_id, tool_name, arguments, action, level, status, "
                        "chain_basis, waiting_for_invitation_id) VALUES "
                        f"('{request}', '{_OWNER}', 'self', '{_CONVERSATION}', 'do_in_order', "
                        f"'{{}}', '{{}}', 'confirm', '{status}', {_BASIS}, '{invitation}')"
                    )
                )
                if wait is not None:
                    await connection.execute(
                        text(
                            f"INSERT INTO confirmation_steps {columns} VALUES ('{request}', 0, "
                            f"'note_leave', 'note.leave', '{{}}', '{wait}', 'Paul')"
                        )
                    )
        for statement in (
            # a name of 60 characters at most
            f"INSERT INTO confirmation_steps {columns} VALUES ('{paused}', 1, 'note_leave', "
            f"'note.leave', '{{}}', 'planned', '{'x' * 61}')",
            # an invitation that exists
            f"UPDATE confirmations SET waiting_for_invitation_id = '{_OWNER}'",
            # the states
            "UPDATE confirmations SET status = 'paused'",
        ):
            with pytest.raises(IntegrityError):
                async with engine.begin() as connection:
                    await connection.execute(text(statement))
        await asyncio.to_thread(command.downgrade, config, "0019")
        assert await schema_of(scratch_database_url) == before
        async with engine.connect() as connection:
            rows = await connection.execute(
                text("SELECT id::text, status, expired_by FROM confirmations ORDER BY id")
            )
            assert [tuple(row) for row in rows] == [
                (_OTHER_REQUEST, "pending", None),
                (paused, "expired", "domo"),
                (_WAIT_TO_COME, "expired", "domo"),
                (_STOPPED_AT_THE_WAIT, "expired", "domo"),
                (_WAIT_PASSED, "pending", None),
                (_DONE_WITH_A_WAIT, "executed", None),
            ]
    finally:
        await engine.dispose()


async def test_a_request_remembers_what_its_approver_was_shown_and_said(scratch_database_url):
    """A request remembers the reply of an approver that last showed it
    (``shown_to_approver_message_id``, a message), and what the approver said with a change
    (``approver_note``, 300 characters at most). Going back down drops both, and gives back the
    schema of 0020."""
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0020")
    before = await schema_of(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0021")
    after = await schema_of(scratch_database_url)
    assert ("confirmations", "shown_to_approver_message_id", "uuid", "YES", None) in after[
        "columns"
    ]
    assert ("confirmations", "approver_note", "text", "YES", None) in after["columns"]
    engine = create_async_engine(scratch_database_url)
    request = "00000000-0000-0000-0000-000000000005"
    try:
        async with engine.begin() as connection:
            await _owner_and_conversation(connection)
            await connection.execute(
                text(
                    "INSERT INTO confirmations (id, requester_id, approver_kind, conversation_id, "
                    "tool_name, arguments, action, level, status, approver_note) VALUES "
                    f"('{request}', '{_OWNER}', 'adult', '{_CONVERSATION}', 'note_leave', '{{}}', "
                    f"'{{}}', 'confirm', 'pending', '{'x' * 300}')"
                )
            )
        for statement in (
            # a note of 300 characters at most
            f"UPDATE confirmations SET approver_note = '{'x' * 301}'",
            # a message that exists
            f"UPDATE confirmations SET shown_to_approver_message_id = '{_OWNER}'",
        ):
            with pytest.raises(IntegrityError):
                async with engine.begin() as connection:
                    await connection.execute(text(statement))
        await asyncio.to_thread(command.downgrade, config, "0020")
        assert await schema_of(scratch_database_url) == before
    finally:
        await engine.dispose()


async def test_a_request_remembers_when_the_one_who_asked_nudged_its_approver(
    scratch_database_url,
):
    """A request that waits for someone else's yes remembers when the one who asked nudged its
    approver (``nudged_at``, spec 5.2). Going back down drops it, and gives back the schema of
    0021."""
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0021")
    before = await schema_of(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0022")
    after = await schema_of(scratch_database_url)
    assert (
        "confirmations",
        "nudged_at",
        "timestamp with time zone",
        "YES",
        None,
    ) in after["columns"]
    await asyncio.to_thread(command.downgrade, config, "0021")
    assert await schema_of(scratch_database_url) == before


async def test_a_note_may_be_kept_until_a_date_after_its_guest_left(scratch_database_url):
    """A note remembers the day an author keeps it until (``keep_until``, spec 5.10): empty for
    every note that was there before. Going back down drops it, and gives back the schema of
    0022."""
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0022")
    before = await schema_of(scratch_database_url)
    assert not any(row[:2] == ("notes", "keep_until") for row in before["columns"])
    await asyncio.to_thread(command.upgrade, config, "0023")
    after = await schema_of(scratch_database_url)
    assert ("notes", "keep_until", "timestamp with time zone", "YES", None) in after["columns"]
    await asyncio.to_thread(command.downgrade, config, "0022")
    assert await schema_of(scratch_database_url) == before


async def test_the_rights_carry_a_rule_and_may_be_for_everyone(scratch_database_url):
    """A right says what Domo must ask before an action, or that the owner wants nothing asked
    (``ask``, with his conditions in ``ask_when``); a right for nobody in particular is a rule for
    everyone, and says nothing else. The level stays at this revision (0025 drops it). Going back
    down drops both columns and the rules for everyone, and gives back the schema of 0023."""
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0023")
    before = await schema_of(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0024")
    after = await schema_of(scratch_database_url)
    columns = {row[1]: row[2:] for row in after["columns"] if row[0] == "policy_rights"}
    assert columns["ask"] == ("text", "YES", None)
    assert columns["ask_when"] == ("text", "YES", None)
    assert "level" in columns
    engine = create_async_engine(scratch_database_url)
    owner = "00000000-0000-0000-0000-000000000001"
    adult = "00000000-0000-0000-0000-000000000002"

    def for_everyone(action_type: str, **fields: str) -> str:
        """The statement that writes a right of the owner's with these ``fields``."""
        row = {"action_type": action_type, "summary": "a rule", "owner_words": "", **fields}
        row["created_by"] = owner
        values = ", ".join(f"'{value}'" for value in row.values())
        return f"INSERT INTO policy_rights ({', '.join(row)}) VALUES ({values})"

    def for_alex(action_type: str, **fields: str) -> str:
        return for_everyone(action_type, person_id=adult, **fields)

    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO persons (id, display_name, role) VALUES "
                    f"('{owner}', 'Owner', 'owner'), ('{adult}', 'Alex', 'adult')"
                )
            )
            await connection.execute(text(for_everyone("household.invite", ask="explicit_yes")))
            await connection.execute(
                text(for_alex("household.invite", access="allow", ask="judge", ask_when="guests"))
            )
            await connection.execute(text(for_alex("note.leave", level="confirm")))
        for statement in (
            # a rule for everyone says what to ask, and nothing else
            for_everyone("household.remove", ask="yes", access="allow"),
            for_everyone("household.leave", ask="yes", level="confirm"),
            for_everyone("household.change_role", approver="owner"),
            for_everyone("note.leave", limit_expression='"allow"'),
            # what to ask, and conditions only with "judge"
            for_alex("household.remove", ask="maybe"),
            for_alex("household.leave", ask="yes", ask_when="guests"),
            for_alex("household.bring_back", ask_when="guests"),
            for_alex("household.change_role", ask="judge", ask_when="x" * 301),
            # never a person and a role at once
            for_alex("household.list", role="adult", ask="yes"),
        ):
            with pytest.raises(IntegrityError):
                async with engine.begin() as connection:
                    await connection.execute(text(statement))
        await asyncio.to_thread(command.downgrade, config, "0023")
        async with engine.connect() as connection:
            rows = await connection.execute(
                text("SELECT person_id::text, action_type FROM policy_rights ORDER BY action_type")
            )
            assert [tuple(row) for row in rows] == [
                (adult, "household.invite"),
                (adult, "note.leave"),
            ]
    finally:
        await engine.dispose()
    assert await schema_of(scratch_database_url) == before


async def test_a_request_keeps_what_is_asked_and_the_levels_are_gone(scratch_database_url):
    """A request keeps what it asks (``ask``), who asked it (``asked_by``) and why (``ask_why``,
    200 characters at most), and may be decided by a proof or let through by the judge; the
    levels go (of a request, of a chain, of a step, of a right), so do Domo's explicit check of
    before and the styles the owner chose; an index serves the history the judge reads. Nothing
    is installed anywhere yet: a request that waits is expired, nothing is converted. Going back
    down gives back the schema of 0024, every request at ``confirm``."""
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0024")
    before = await schema_of(scratch_database_url)
    waiting, done = (
        "00000000-0000-0000-0000-000000000005",
        "00000000-0000-0000-0000-000000000006",
    )
    engine = create_async_engine(scratch_database_url)
    try:
        async with engine.begin() as connection:
            await _owner_and_conversation(connection)
            for request, status, decided_via in (
                (waiting, "pending", "NULL"),
                (done, "executed", "'button'"),
            ):
                await connection.execute(
                    text(
                        "INSERT INTO confirmations (id, requester_id, approver_kind, "
                        "conversation_id, tool_name, arguments, action, level, status, "
                        "explicit_required, explicit_reason, decided_via) VALUES "
                        f"('{request}', '{_OWNER}', 'self', '{_CONVERSATION}', 'note_leave', "
                        f"'{{}}', '{{}}', 'always_confirm', '{status}', true, 'odd', "
                        f"{decided_via})"
                    )
                )
            for key in ("confirmation_style.confirm", "confirmation_max_lifetime_hours"):
                await connection.execute(
                    text(
                        "INSERT INTO policy_settings (key, value, updated_by) VALUES "
                        f"('{key}', '1', '{_OWNER}')"
                    )
                )
            await connection.execute(
                text(
                    "INSERT INTO policy_rights (role, action_type, level, summary, owner_words, "
                    f"created_by) VALUES ('adult', 'note.leave', 'confirm', 'Notes wait', '', "
                    f"'{_OWNER}')"
                )
            )
        await asyncio.to_thread(command.upgrade, config, "0025")
        after = await schema_of(scratch_database_url)
        columns = {(row[0], row[1]): row[2:] for row in after["columns"]}
        for column in ("ask", "asked_by", "ask_why"):
            assert columns[("confirmations", column)] == ("text", "YES", None)
        for table, column in (
            ("confirmations", "level"),
            ("confirmations", "chain_level"),
            ("confirmations", "explicit_required"),
            ("confirmations", "explicit_reason"),
            ("confirmation_steps", "level"),
            ("policy_rights", "level"),
        ):
            assert (table, column) not in columns, (table, column)
        assert (
            "confirmations",
            "ix_confirmations_requester_id_action_type",
            "CREATE INDEX ix_confirmations_requester_id_action_type ON public.confirmations "
            "USING btree (requester_id, ((action ->> 'action_type'::text)))",
        ) in after["indexes"]
        async with engine.begin() as connection:
            rows = await connection.execute(
                text("SELECT id::text, status, expired_by, ask FROM confirmations ORDER BY id")
            )
            # The request that waited is expired; nothing is converted.
            assert [tuple(row) for row in rows] == [
                (waiting, "expired", "lifetime", None),
                (done, "executed", None, None),
            ]
            keys = await connection.execute(text("SELECT key FROM policy_settings"))
            assert [row[0] for row in keys] == ["confirmation_max_lifetime_hours"]
            await connection.execute(
                text(
                    "UPDATE confirmations SET ask = 'proof', asked_by = 'judge', "
                    f"ask_why = '{'x' * 200}', decided_via = 'proof' WHERE id = '{done}'"
                )
            )
            await connection.execute(
                text(f"UPDATE confirmations SET decided_via = 'judge' WHERE id = '{waiting}'")
            )
        for statement in (
            "UPDATE confirmations SET ask = 'confirm'",
            "UPDATE confirmations SET asked_by = 'model'",
            f"UPDATE confirmations SET ask_why = '{'x' * 201}'",
            "UPDATE confirmations SET decided_via = 'voice'",
        ):
            with pytest.raises(IntegrityError):
                async with engine.begin() as connection:
                    await connection.execute(text(statement))
        await asyncio.to_thread(command.downgrade, config, "0024")
        async with engine.connect() as connection:
            rows = await connection.execute(
                text(
                    "SELECT id::text, level, explicit_required, decided_via FROM confirmations "
                    "ORDER BY id"
                )
            )
            assert [tuple(row) for row in rows] == [
                (waiting, "confirm", False, "button"),
                (done, "confirm", False, "button"),
            ]
    finally:
        await engine.dispose()
    assert await schema_of(scratch_database_url) == before


async def test_a_challenge_may_be_a_proof(scratch_database_url):
    """A passkey challenge may be one of a proof that it is the person (spec 4.5), next to a
    registration and a login, and nothing else. Going back down forgets the challenges of a proof
    (they live five minutes), and gives back the schema of 0025."""
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0025")
    before = await schema_of(scratch_database_url)

    def challenge(purpose: str, byte: int) -> str:
        return (
            "INSERT INTO webauthn_challenges (purpose, account_id, challenge, expires_at) VALUES "
            f"('{purpose}', NULL, '\\x{byte:02x}', now() + interval '5 minutes')"
        )

    engine = create_async_engine(scratch_database_url)
    try:
        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(text(challenge("proof", 1)))
        await asyncio.to_thread(command.upgrade, config, "0026")
        async with engine.begin() as connection:
            for purpose, byte in (("register", 2), ("login", 3), ("proof", 4)):
                await connection.execute(text(challenge(purpose, byte)))
        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(text(challenge("sms", 5)))
        await asyncio.to_thread(command.downgrade, config, "0025")
        async with engine.connect() as connection:
            rows = await connection.execute(
                text("SELECT purpose FROM webauthn_challenges ORDER BY purpose")
            )
            assert [row[0] for row in rows] == ["login", "register"]
    finally:
        await engine.dispose()
    assert await schema_of(scratch_database_url) == before


_OTHER_REQUEST = "00000000-0000-0000-0000-000000000005"
_WAIT_TO_COME = "00000000-0000-0000-0000-000000000008"
_STOPPED_AT_THE_WAIT = "00000000-0000-0000-0000-000000000009"
_WAIT_PASSED = "00000000-0000-0000-0000-00000000000a"
_DONE_WITH_A_WAIT = "00000000-0000-0000-0000-00000000000b"
# The requests of ``test_a_chain_may_wait_for_an_arrival``: each id, its state, and the state of
# its step that waits (``None``: it has none).
_REQUESTS_AROUND_A_WAIT = (
    ("00000000-0000-0000-0000-000000000007", "waiting_arrival", "planned"),
    (_OTHER_REQUEST, "pending", None),
    (_WAIT_TO_COME, "pending", "planned"),
    (_STOPPED_AT_THE_WAIT, "failed", "failed"),
    (_WAIT_PASSED, "pending", "done"),
    (_DONE_WITH_A_WAIT, "executed", "done"),
)


def test_upgrading_an_up_to_date_database_changes_nothing(database_url):
    """The core migrates at every start: the second run must be a no-op."""
    upgrade_to_head(database_url)


def test_the_migrations_ship_inside_the_package():
    """The container installs the package without the source tree: only what is inside
    ``domo_core`` gets there."""
    package_dir = Path(domo_core.__file__).parent
    assert MIGRATIONS_DIR.parent == package_dir
    for name in (
        "env.py",
        "script.py.mako",
        "versions/0001_initial.py",
        "versions/0003_household.py",
    ):
        assert (MIGRATIONS_DIR / name).is_file()


def test_the_migration_renders_as_sql_without_a_database():
    """The offline mode (``alembic upgrade head --sql``) needs no connection."""
    config = alembic_config("postgresql+asyncpg://user:do-not-leak-me@db.invalid/domo")
    config.output_buffer = io.StringIO()
    command.upgrade(config, "head", sql=True)
    sql = config.output_buffer.getvalue()
    assert "CREATE EXTENSION IF NOT EXISTS vector" in sql
    assert "CREATE TABLE persons" in sql
    assert "uq_persons_single_owner" in sql and "WHERE role = 'owner'" in sql
    assert "CREATE TABLE confirmations" in sql and "ck_persons_role_values" in sql
    assert "do-not-leak-me" not in sql


async def test_0027_gives_the_existing_key_to_the_owner_and_makes_it_general(
    scratch_database_url,
):
    """Every key goes to the owner, the oldest becomes the general one; going back down gives the
    schema of 0026 again."""
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0026")
    before = await schema_of(scratch_database_url)
    owner = "00000000-0000-0000-0000-000000000001"
    engine = create_async_engine(scratch_database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    f"INSERT INTO persons (id, display_name, role) VALUES ('{owner}', 'O', 'owner')"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO providers (id, kind, base_url, model, created_at) VALUES "
                    "('p-00000001', 'anthropic', 'https://a.example', 'm', '2026-01-01'), "
                    "('p-00000002', 'anthropic', 'https://b.example', 'm', '2026-02-01')"
                )
            )
        await asyncio.to_thread(command.upgrade, config, "0027")
        async with engine.connect() as connection:
            rows = await connection.execute(
                text("SELECT id, given_by::text, is_general FROM providers ORDER BY id")
            )
            assert [tuple(row) for row in rows] == [
                ("p-00000001", owner, True),
                ("p-00000002", owner, False),
            ]
        await asyncio.to_thread(command.downgrade, config, "0026")
    finally:
        await engine.dispose()
    assert await schema_of(scratch_database_url) == before


async def test_0028_takes_the_model_away_and_back(scratch_database_url):
    """A key loses its model and must say who gave it (one that says nobody goes to the owner);
    going back down gives the model back, empty, and the schema of 0027 again."""
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0027")
    before = await schema_of(scratch_database_url)
    owner = "00000000-0000-0000-0000-000000000001"
    engine = create_async_engine(scratch_database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    f"INSERT INTO persons (id, display_name, role) VALUES ('{owner}', 'O', 'owner')"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO providers (id, kind, base_url, model, given_by) VALUES "
                    f"('p-00000001', 'anthropic', 'https://a.example', 'm', '{owner}'), "
                    "('p-00000002', 'anthropic', 'https://b.example', 'm', NULL)"
                )
            )
        await asyncio.to_thread(command.upgrade, config, "0028")
        async with engine.connect() as connection:
            columns = await connection.execute(
                text(
                    "SELECT column_name, is_nullable FROM information_schema.columns "
                    "WHERE table_name = 'providers' AND column_name IN ('model', 'given_by')"
                )
            )
            assert [tuple(row) for row in columns] == [("given_by", "NO")]
            rows = await connection.execute(
                text("SELECT id, given_by::text FROM providers ORDER BY id")
            )
            assert [tuple(row) for row in rows] == [("p-00000001", owner), ("p-00000002", owner)]
        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO providers (id, kind, base_url) "
                        "VALUES ('p-00000003', 'anthropic', 'https://c.example')"
                    )
                )
        await asyncio.to_thread(command.downgrade, config, "0027")
        async with engine.connect() as connection:
            rows = await connection.execute(text("SELECT id, model FROM providers ORDER BY id"))
            assert [tuple(row) for row in rows] == [("p-00000001", ""), ("p-00000002", "")]
    finally:
        await engine.dispose()
    assert await schema_of(scratch_database_url) == before


async def test_0029_gives_a_limit_a_role_and_a_split_and_takes_them_back(scratch_database_url):
    """A limit may be for a role (``role`` and ``split`` go together, never with a person); each
    and together for one role are two limits; going back down drops the role limits and the
    schema of 0028 comes back."""
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0028")
    before = await schema_of(scratch_database_url)
    owner = "00000000-0000-0000-0000-000000000001"
    engine = create_async_engine(scratch_database_url)
    insert = (
        "INSERT INTO spending_limits (provider_id, person_id, {columns}amount, currency, period, "
        "set_by) VALUES ('p-00000001', {person}{values}1, 'USD', 'month', '" + owner + "')"
    )
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    f"INSERT INTO persons (id, display_name, role) VALUES ('{owner}', 'O', 'owner')"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO providers (id, kind, base_url, given_by) VALUES "
                    f"('p-00000001', 'anthropic', 'https://a.example', '{owner}')"
                )
            )
            await connection.execute(text(insert.format(columns="", person="NULL, ", values="")))
        await asyncio.to_thread(command.upgrade, config, "0029")
        async with engine.begin() as connection:
            columns = "role, split, "
            for split in ("each", "together"):
                await connection.execute(
                    text(
                        insert.format(
                            columns=columns, person="NULL, ", values=f"'guest', '{split}', "
                        )
                    )
                )
        for bad in (
            ("NULL, ", "'guest', 'each', "),  # the same people twice
            ("NULL, ", "'owner', 'each', "),
            ("NULL, ", "'guest', 'some', "),
            ("NULL, ", "'guest', NULL, "),
            ("NULL, ", "NULL, 'each', "),
            (f"'{owner}', ", "'guest', 'each', "),
        ):
            with pytest.raises(IntegrityError):
                async with engine.begin() as connection:
                    await connection.execute(
                        text(insert.format(columns=columns, person=bad[0], values=bad[1]))
                    )
        await asyncio.to_thread(command.downgrade, config, "0028")
        async with engine.connect() as connection:
            rows = await connection.execute(text("SELECT count(*) FROM spending_limits"))
            assert rows.scalar_one() == 1
    finally:
        await engine.dispose()
    assert await schema_of(scratch_database_url) == before


async def test_0030_keeps_who_began_using_a_key_and_loses_it_with_the_key(scratch_database_url):
    """``key_newcomers`` has one row per key and person, goes with its key (``ON DELETE
    CASCADE``), and going back down drops it so that the schema of 0029 comes back."""
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0029")
    before = await schema_of(scratch_database_url)
    owner = "00000000-0000-0000-0000-000000000001"
    engine = create_async_engine(scratch_database_url)
    insert = (
        "INSERT INTO key_newcomers (provider_id, person_id, told_at) VALUES "
        f"('p-00000001', '{owner}', now())"
    )
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    f"INSERT INTO persons (id, display_name, role) VALUES ('{owner}', 'O', 'owner')"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO providers (id, kind, base_url, given_by) VALUES "
                    f"('p-00000001', 'anthropic', 'https://a.example', '{owner}')"
                )
            )
        await asyncio.to_thread(command.upgrade, config, "0030")
        async with engine.begin() as connection:
            await connection.execute(text(insert))
        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(text(insert))
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM providers WHERE id = 'p-00000001'"))
            rows = await connection.execute(text("SELECT count(*) FROM key_newcomers"))
            assert rows.scalar_one() == 0
        await asyncio.to_thread(command.downgrade, config, "0029")
    finally:
        await engine.dispose()
    assert await schema_of(scratch_database_url) == before


async def test_0031_keeps_written_texts_and_takes_the_new_kinds_of_calls(scratch_database_url):
    """The tables of written texts and card heads, the columns of a person, a message and a note,
    and the three new kinds of calls; going back down brings the schema of 0030 back."""
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0030")
    before = await schema_of(scratch_database_url)
    owner = "00000000-0000-0000-0000-000000000001"
    engine = create_async_engine(scratch_database_url)
    call = (
        "INSERT INTO model_calls (task, model_id, status, reserved_usd) "
        "VALUES ('{task}', 'm', 'settled', 0)"
    )
    text_row = (
        "INSERT INTO written_texts (person_id, language, key, text, meaning_hash, model_id, "
        "written_at) VALUES ({person}, {language}, 'k', 't', 'h', 'm', now())"
    )
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    f"INSERT INTO persons (id, display_name, role) VALUES ('{owner}', 'O', 'owner')"
                )
            )
            await connection.execute(text(call.format(task="upkeep")))
        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(text(call.format(task="writing")))
        await asyncio.to_thread(command.upgrade, config, "0031")
        async with engine.begin() as connection:
            for task in ("writing", "checking", "arrival"):
                await connection.execute(text(call.format(task=task)))
            await connection.execute(text(text_row.format(person=f"'{owner}'", language="NULL")))
            await connection.execute(text(text_row.format(person="NULL", language="'fr'")))
        for person, language in ((f"'{owner}'", "NULL"), ("NULL", "'fr'"), ("NULL", "NULL")):
            with pytest.raises(IntegrityError):
                async with engine.begin() as connection:
                    await connection.execute(
                        text(text_row.format(person=person, language=language))
                    )
        bad = (f"'{owner}'", "'fr'")
        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(text(text_row.format(person=bad[0], language=bad[1])))
        async with engine.begin() as connection:
            await connection.execute(text(f"DELETE FROM persons WHERE id = '{owner}'"))
            rows = await connection.execute(text("SELECT count(*) FROM written_texts"))
            assert rows.scalar_one() == 1  # the person's went with them, the language's stays
        await asyncio.to_thread(command.downgrade, config, "0030")
        async with engine.connect() as connection:
            rows = await connection.execute(text("SELECT task FROM model_calls"))
            assert rows.scalars().all() == ["upkeep"]
    finally:
        await engine.dispose()
    assert await schema_of(scratch_database_url) == before


async def test_0032_gives_a_card_head_the_fingerprint_it_was_written_from(scratch_database_url):
    """``card_texts.facts_hash``: a head kept before has none (``''``, which matches nothing, so
    it is written again); going back down brings the schema of 0031 back."""
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0031")
    before = await schema_of(scratch_database_url)
    owner = "00000000-0000-0000-0000-000000000001"
    engine = create_async_engine(scratch_database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    f"INSERT INTO persons (id, display_name, role) VALUES ('{owner}', 'O', 'owner')"
                )
            )
            await connection.execute(
                text(f"INSERT INTO conversations (id, person_id) VALUES ('{owner}', '{owner}')")
            )
            await connection.execute(
                text(
                    "INSERT INTO messages (id, conversation_id, role, content, status, provenance) "
                    f"VALUES ('{owner}', '{owner}', 'assistant', '', 'complete', 'model')"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO message_actions (id, message_id, tool_name, action_type, status, "
                    f"summary) VALUES ('{owner}', '{owner}', 't', 't', 'done', '{{}}')"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO card_texts (message_action_id, reader_id, head, model_id, "
                    f"written_at) VALUES ('{owner}', '{owner}', 'Head', 'm', now())"
                )
            )
        await asyncio.to_thread(command.upgrade, config, "0032")
        async with engine.connect() as connection:
            rows = await connection.execute(text("SELECT head, facts_hash FROM card_texts"))
            assert [tuple(row) for row in rows.all()] == [("Head", "")]
        await asyncio.to_thread(command.downgrade, config, "0031")
    finally:
        await engine.dispose()
    assert await schema_of(scratch_database_url) == before


async def test_0033_takes_away_the_closing_of_a_note(scratch_database_url):
    """``notes.dismissed_at`` goes (nothing closes a note since the zone of notes is gone), a note
    stays as it was; going back down brings the schema of 0032 back, the column empty."""
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0032")
    before = await schema_of(scratch_database_url)
    assert ("notes", "dismissed_at", "timestamp with time zone", "YES", None) in before["columns"]
    owner = "00000000-0000-0000-0000-000000000001"
    engine = create_async_engine(scratch_database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    f"INSERT INTO persons (id, display_name, role) VALUES ('{owner}', 'O', 'owner')"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO notes (author_id, recipient_id, text, dismissed_at) "
                    f"VALUES ('{owner}', '{owner}', 'Milk', now())"
                )
            )
        await asyncio.to_thread(command.upgrade, config, "0033")
        after = await schema_of(scratch_database_url)
        assert not any(row[:2] == ("notes", "dismissed_at") for row in after["columns"])
        async with engine.connect() as connection:
            rows = await connection.execute(text("SELECT text FROM notes"))
            assert rows.scalars().all() == ["Milk"]
        await asyncio.to_thread(command.downgrade, config, "0032")
        async with engine.connect() as connection:
            rows = await connection.execute(text("SELECT text, dismissed_at FROM notes"))
            assert [tuple(row) for row in rows.all()] == [("Milk", None)]
    finally:
        await engine.dispose()
    assert await schema_of(scratch_database_url) == before


async def test_0034_gives_a_person_when_news_came_for_them(scratch_database_url):
    """``persons.news_at`` comes, empty for everyone; going back down brings the schema of 0033
    back, the people as they were."""
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0033")
    before = await schema_of(scratch_database_url)
    owner = "00000000-0000-0000-0000-000000000001"
    engine = create_async_engine(scratch_database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    f"INSERT INTO persons (id, display_name, role) VALUES ('{owner}', 'O', 'owner')"
                )
            )
        await asyncio.to_thread(command.upgrade, config, "0034")
        after = await schema_of(scratch_database_url)
        assert ("persons", "news_at", "timestamp with time zone", "YES", None) in after["columns"]
        async with engine.connect() as connection:
            rows = await connection.execute(text("SELECT display_name, news_at FROM persons"))
            assert [tuple(row) for row in rows.all()] == [("O", None)]
        await asyncio.to_thread(command.downgrade, config, "0033")
        async with engine.connect() as connection:
            rows = await connection.execute(text("SELECT display_name FROM persons"))
            assert rows.scalars().all() == ["O"]
    finally:
        await engine.dispose()
    assert await schema_of(scratch_database_url) == before


async def test_0035_lets_a_note_keep_the_exact_message_of_its_author(scratch_database_url):
    """``notes.exact_text`` comes, empty for the notes there are, and holds 1 to 1000 characters
    when it holds any; going back down brings the schema of 0034 back, the notes as they were."""
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0034")
    before = await schema_of(scratch_database_url)
    owner = "00000000-0000-0000-0000-000000000001"
    engine = create_async_engine(scratch_database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    f"INSERT INTO persons (id, display_name, role) VALUES ('{owner}', 'O', 'owner')"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO notes (author_id, recipient_id, text) "
                    f"VALUES ('{owner}', '{owner}', 'Milk')"
                )
            )
        await asyncio.to_thread(command.upgrade, config, "0035")
        after = await schema_of(scratch_database_url)
        assert ("notes", "exact_text", "text", "YES", None) in after["columns"]
        async with engine.begin() as connection:
            rows = await connection.execute(text("SELECT text, exact_text FROM notes"))
            assert [tuple(row) for row in rows.all()] == [("Milk", None)]
            await connection.execute(
                text(
                    "INSERT INTO notes (author_id, recipient_id, text, exact_text) "
                    f"VALUES ('{owner}', '{owner}', 'Bread', 'Tell me: bread')"
                )
            )
        for wrong in ("", "x" * 1001):
            with pytest.raises(IntegrityError):
                async with engine.begin() as connection:
                    await connection.execute(
                        text(
                            "INSERT INTO notes (author_id, recipient_id, text, exact_text) "
                            f"VALUES ('{owner}', '{owner}', 'Bread', :wrong)"
                        ),
                        {"wrong": wrong},
                    )
        await asyncio.to_thread(command.downgrade, config, "0034")
        async with engine.connect() as connection:
            rows = await connection.execute(text("SELECT text FROM notes ORDER BY text"))
            assert rows.scalars().all() == ["Bread", "Milk"]
    finally:
        await engine.dispose()
    assert await schema_of(scratch_database_url) == before


async def test_0042_gives_the_memories_a_vector_and_takes_it_back(scratch_database_url):
    """The vector of a memory, the model that made it and their index come and go together."""
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0041")
    before = await schema_of(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0042")
    after = await schema_of(scratch_database_url)
    columns = {(table, column) for table, column, *_ in after["columns"]}
    assert {("memories", "embedding"), ("memories", "embedding_model")} <= columns
    indexes = {name: definition for _, name, definition in after["indexes"]}
    assert "hnsw" in indexes["ix_memories_embedding"]
    assert "vector_cosine_ops" in indexes["ix_memories_embedding"]
    await asyncio.to_thread(command.downgrade, config, "0041")
    assert await schema_of(scratch_database_url) == before


async def test_0043_keeps_where_a_review_stopped_and_who_is_owed_a_last_one(scratch_database_url):
    """``conversations.reviewed_upto_at`` comes empty, ``persons.last_review_due`` false for
    everyone, and the calls of a model may be of the task ``memory``; going back down drops those
    calls and brings the schema of 0042 back."""
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0042")
    before = await schema_of(scratch_database_url)
    owner = "00000000-0000-0000-0000-000000000001"
    call = (
        "INSERT INTO model_calls (task, model_id, status, reserved_usd) "
        "VALUES ('{task}', 'm', 'settled', 0)"
    )
    engine = create_async_engine(scratch_database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    f"INSERT INTO persons (id, display_name, role) VALUES ('{owner}', 'O', 'owner')"
                )
            )
            await connection.execute(
                text(f"INSERT INTO conversations (person_id) VALUES ('{owner}')")
            )
            await connection.execute(text(call.format(task="arrival")))
        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(text(call.format(task="memory")))
        await asyncio.to_thread(command.upgrade, config, "0043")
        after = await schema_of(scratch_database_url)
        assert (
            "conversations",
            "reviewed_upto_at",
            "timestamp with time zone",
            "YES",
            None,
        ) in after["columns"]
        async with engine.begin() as connection:
            rows = await connection.execute(text("SELECT reviewed_upto_at FROM conversations"))
            assert rows.scalars().all() == [None]
            rows = await connection.execute(text("SELECT last_review_due FROM persons"))
            assert rows.scalars().all() == [False]
            await connection.execute(text(call.format(task="memory")))
        await asyncio.to_thread(command.downgrade, config, "0042")
        async with engine.connect() as connection:
            rows = await connection.execute(text("SELECT task FROM model_calls"))
            assert rows.scalars().all() == ["arrival"]
    finally:
        await engine.dispose()
    assert await schema_of(scratch_database_url) == before


async def test_0044_tells_whether_a_request_warned_the_person(scratch_database_url):
    """``confirmations.warned`` comes false for the requests already there; going back down
    drops it and brings the schema of 0043 back."""
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0043")
    before = await schema_of(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0044")
    after = await schema_of(scratch_database_url)
    assert ("confirmations", "warned", "boolean", "NO", "false") in after["columns"]
    await asyncio.to_thread(command.downgrade, config, "0043")
    assert await schema_of(scratch_database_url) == before


async def test_0045_keeps_the_plans_domo_sets_and_takes_them_back(scratch_database_url):
    """``plans`` comes with its index of what is due; a waiting plan needs a time or a return,
    and goes with its person; going back down brings the schema of 0044 back."""
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0044")
    before = await schema_of(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0045")
    after = await schema_of(scratch_database_url)
    indexes = {name: definition for _, name, definition in after["indexes"]}
    assert "WHERE (state = 'waiting'::text)" in indexes["ix_plans_due_at"]
    person = "00000000-0000-0000-0000-000000000001"
    engine = create_async_engine(scratch_database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO persons (id, display_name, role) "
                    f"VALUES ('{person}', 'O', 'owner')"
                )
            )
            await connection.execute(
                text(
                    f"INSERT INTO plans (person_id, why, on_return) VALUES ('{person}', 'w', true)"
                )
            )
        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    text(f"INSERT INTO plans (person_id, why) VALUES ('{person}', 'nothing')")
                )
        async with engine.begin() as connection:
            await connection.execute(text(f"DELETE FROM persons WHERE id = '{person}'"))
            rows = await connection.execute(text("SELECT count(*) FROM plans"))
            assert rows.scalar() == 0
    finally:
        await engine.dispose()
    await asyncio.to_thread(command.downgrade, config, "0044")
    assert await schema_of(scratch_database_url) == before


async def test_0046_keeps_the_name_each_person_gives_domo(scratch_database_url):
    """``persons.calls_domo`` comes empty for the people already there and holds 1 to 40
    characters; going back down brings the schema of 0045 back."""
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0045")
    before = await schema_of(scratch_database_url)
    engine = create_async_engine(scratch_database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO persons (display_name, role) VALUES ('O', 'owner')")
            )
        await asyncio.to_thread(command.upgrade, config, "0046")
        async with engine.begin() as connection:
            rows = await connection.execute(text("SELECT calls_domo FROM persons"))
            assert rows.scalars().all() == [None]
            await connection.execute(text("UPDATE persons SET calls_domo = 'Brenda'"))
        for refused in ("", "x" * 41):
            with pytest.raises(IntegrityError):
                async with engine.begin() as connection:
                    await connection.execute(
                        text("UPDATE persons SET calls_domo = :name"), {"name": refused}
                    )
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM persons"))
    finally:
        await engine.dispose()
    await asyncio.to_thread(command.downgrade, config, "0045")
    assert await schema_of(scratch_database_url) == before


async def test_0047_keeps_what_reached_whom_and_takes_it_back(scratch_database_url):
    """``deliveries`` holds one row per reply, channel, device and way (no device counting as one),
    goes with its reply, and keeps a row whose device is gone; going back down brings the schema of
    0046 back."""
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0046")
    before = await schema_of(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0047")
    person = "00000000-0000-0000-0000-000000000001"
    thread = "00000000-0000-0000-0000-000000000002"
    reply = "00000000-0000-0000-0000-000000000003"
    device = "00000000-0000-0000-0000-000000000004"
    row = (
        "INSERT INTO deliveries (message_id, person_id, channel, device_id, how, extent, at) "
        f"VALUES ('{reply}', '{person}', :channel, :device, :how, 'whole', now())"
    )
    engine = create_async_engine(scratch_database_url)
    try:
        async with engine.begin() as connection:
            for statement in (
                f"INSERT INTO persons (id, display_name, role) VALUES ('{person}', 'O', 'owner')",
                f"INSERT INTO conversations (id, person_id) VALUES ('{thread}', '{person}')",
                "INSERT INTO messages (id, conversation_id, role, status, provenance) "
                f"VALUES ('{reply}', '{thread}', 'assistant', 'complete', 'model')",
                "INSERT INTO devices (id, person_id, name, secret_hash) "
                f"VALUES ('{device}', '{person}', 'Laptop', 'h')",
            ):
                await connection.execute(text(statement))
            for channel, on, how in (
                ("page", None, "live"),
                ("page", None, "fetched"),
                ("api", device, "live"),
            ):
                await connection.execute(text(row), {"channel": channel, "device": on, "how": how})
        with pytest.raises(IntegrityError):  # the page has no device: still one row per way
            async with engine.begin() as connection:
                await connection.execute(
                    text(row), {"channel": "page", "device": None, "how": "live"}
                )
        async with engine.begin() as connection:
            await connection.execute(text(f"DELETE FROM devices WHERE id = '{device}'"))
            rows = await connection.execute(
                text("SELECT count(*) FROM deliveries WHERE device_id IS NULL")
            )
            assert rows.scalar() == 3
            await connection.execute(text(f"DELETE FROM messages WHERE id = '{reply}'"))
            rows = await connection.execute(text("SELECT count(*) FROM deliveries"))
            assert rows.scalar() == 0
            await connection.execute(text("DELETE FROM conversations"))
            await connection.execute(text("DELETE FROM persons"))
    finally:
        await engine.dispose()
    await asyncio.to_thread(command.downgrade, config, "0046")
    assert await schema_of(scratch_database_url) == before


async def test_0048_keeps_the_names_each_person_uses_for_the_others(scratch_database_url):
    """``person_nicknames`` holds one nickname per person and key, of 1 to 40 characters, and goes
    with either person; going back down brings the schema of 0047 back."""
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0047")
    before = await schema_of(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0048")
    alex = "00000000-0000-0000-0000-000000000001"
    samuel = "00000000-0000-0000-0000-000000000002"
    row = (
        "INSERT INTO person_nicknames (person_id, target_id, nickname, nickname_key, created_at) "
        f"VALUES ('{alex}', '{samuel}', :nickname, :key, now())"
    )
    engine = create_async_engine(scratch_database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO persons (id, display_name, role) "
                    f"VALUES ('{alex}', 'Alex', 'owner'), ('{samuel}', 'Samuel', 'adult')"
                )
            )
            await connection.execute(text(row), {"nickname": "Sam", "key": "sam"})
        for nickname, key in (("SAM", "sam"), ("", "x"), ("x" * 41, "x")):
            with pytest.raises(IntegrityError):
                async with engine.begin() as connection:
                    await connection.execute(text(row), {"nickname": nickname, "key": key})
        async with engine.begin() as connection:
            await connection.execute(text(f"DELETE FROM persons WHERE id = '{samuel}'"))
            rows = await connection.execute(text("SELECT count(*) FROM person_nicknames"))
            assert rows.scalar() == 0
            await connection.execute(text("DELETE FROM persons"))
    finally:
        await engine.dispose()
    await asyncio.to_thread(command.downgrade, config, "0047")
    assert await schema_of(scratch_database_url) == before


async def test_0049_keeps_when_a_memory_was_confirmed(scratch_database_url):
    """``memories.confirmed_at`` comes empty (F4, the owner's trial of 0.9.0); going back down
    brings the schema of 0048 back."""
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0048")
    before = await schema_of(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0049")
    after = await schema_of(scratch_database_url)
    columns = {(table, column) for table, column, *_ in after["columns"]}
    assert ("memories", "confirmed_at") in columns
    await asyncio.to_thread(command.downgrade, config, "0048")
    assert await schema_of(scratch_database_url) == before


async def test_0050_keeps_for_whom_an_invitations_link_waits(scratch_database_url):
    """``invitations.link_for_id`` comes empty (the review of the owner's trial of 0.9.0); going
    back down brings the schema of 0049 back."""
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0049")
    before = await schema_of(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0050")
    after = await schema_of(scratch_database_url)
    columns = {(table, column) for table, column, *_ in after["columns"]}
    assert ("invitations", "link_for_id") in columns
    await asyncio.to_thread(command.downgrade, config, "0049")
    assert await schema_of(scratch_database_url) == before


async def test_0051_keeps_what_a_persons_word_was_worth_and_takes_it_back(scratch_database_url):
    """``trust_facts`` holds known outcomes and ways of establishing, a text of 1 to 200
    characters, a reporter only on a reported fact, and goes with the person whose word it is;
    going back down brings the schema of 0050 back."""
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0050")
    before = await schema_of(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0051")
    sam = "00000000-0000-0000-0000-000000000001"
    dana = "00000000-0000-0000-0000-000000000002"
    row = (
        "INSERT INTO trust_facts "
        "(person_id, outcome, established, about, reported_by_id, created_at) "
        f"VALUES ('{sam}', :outcome, :established, :about, :reporter, now())"
    )
    engine = create_async_engine(scratch_database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO persons (id, display_name, role) "
                    f"VALUES ('{sam}', 'Sam', 'owner'), ('{dana}', 'Dana', 'adult')"
                )
            )
            await connection.execute(
                text(row),
                {"outcome": "lied", "established": "reported", "about": "x", "reporter": dana},
            )
        for outcome, established, about, reporter in (
            ("lying", "seen", "x", None),
            ("lied", "heard", "x", None),
            ("lied", "seen", "", None),
            ("lied", "seen", "x" * 201, None),
            ("lied", "seen", "x", dana),
        ):
            with pytest.raises(IntegrityError):
                async with engine.begin() as connection:
                    await connection.execute(
                        text(row),
                        {
                            "outcome": outcome,
                            "established": established,
                            "about": about,
                            "reporter": reporter,
                        },
                    )
        async with engine.begin() as connection:
            await connection.execute(text(f"DELETE FROM persons WHERE id = '{dana}'"))
            rows = await connection.execute(text("SELECT reported_by_id FROM trust_facts"))
            assert rows.all() == [(None,)]
            await connection.execute(text(f"DELETE FROM persons WHERE id = '{sam}'"))
            rows = await connection.execute(text("SELECT count(*) FROM trust_facts"))
            assert rows.scalar() == 0
    finally:
        await engine.dispose()
    await asyncio.to_thread(command.downgrade, config, "0050")
    assert await schema_of(scratch_database_url) == before


async def test_0052_keeps_one_confirmed_fact_per_memory(scratch_database_url):
    """A memory gives one ``confirmed`` fact at most, and any number of others; going back down
    brings the schema of 0051 back."""
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0051")
    before = await schema_of(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0052")
    sam = "00000000-0000-0000-0000-000000000001"
    memory = "00000000-0000-0000-0000-0000000000aa"
    row = (
        "INSERT INTO trust_facts (person_id, outcome, established, about, memory_id, created_at) "
        f"VALUES ('{sam}', 'lied', :established, 'x', '{memory}', now())"
    )
    engine = create_async_engine(scratch_database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    f"INSERT INTO persons (id, display_name, role) VALUES ('{sam}', 'Sam', 'owner')"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO memories (id, said_by_id, subject, content, kind, origin, "
                    "kept_private, core, created_at, changed_at) "
                    f"VALUES ('{memory}', '{sam}', 's', 'c', 'fact', 'own_words', false, false, "
                    "now(), now())"
                )
            )
            for established in ("confirmed", "seen", "seen"):
                await connection.execute(text(row), {"established": established})
        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(text(row), {"established": "confirmed"})
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM persons"))
    finally:
        await engine.dispose()
    await asyncio.to_thread(command.downgrade, config, "0051")
    assert await schema_of(scratch_database_url) == before


async def test_0053_lets_a_right_be_domo_s_and_takes_it_back(scratch_database_url):
    """A right may be Domo's own (``for_domo``, false unless said): never with a person or a
    role, never a rule for everyone, one in force per type apart from the rule for everyone;
    going back down drops his rights and brings the schema of 0052 back."""
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0052")
    before = await schema_of(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0053")
    after = await schema_of(scratch_database_url)
    assert ("policy_rights", "for_domo", "boolean", "NO", "false") in after["columns"]
    # Whether his history counted a request of his already (Task 7 of 2026-11-21).
    assert ("confirmations", "counted_for_domo", "boolean", "NO", "false") in after["columns"]
    sam = "00000000-0000-0000-0000-000000000001"

    def right(action_type: str, **fields: str) -> str:
        row = {"action_type": action_type, "summary": "a right", "owner_words": "", **fields}
        row["created_by"] = sam
        values = ", ".join(f"'{value}'" for value in row.values())
        return f"INSERT INTO policy_rights ({', '.join(row)}) VALUES ({values})"

    engine = create_async_engine(scratch_database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    f"INSERT INTO persons (id, display_name, role) VALUES ('{sam}', 'Sam', 'owner')"
                )
            )
            await connection.execute(text(right("note.leave", for_domo="true", access="deny")))
            await connection.execute(text(right("note.leave", ask="yes")))
            await connection.execute(text(right("note.leave", role="adult", access="deny")))
        for statement in (
            right("household.list", for_domo="true", person_id=sam),
            right("household.list", for_domo="true", role="adult"),
            right("note.leave", for_domo="true", ask="yes"),
            right("household.list", access="deny"),
        ):
            with pytest.raises(IntegrityError):
                async with engine.begin() as connection:
                    await connection.execute(text(statement))
        await asyncio.to_thread(command.downgrade, config, "0052")
        async with engine.connect() as connection:
            rows = await connection.execute(
                text("SELECT role, ask FROM policy_rights ORDER BY role NULLS FIRST")
            )
            assert [tuple(row) for row in rows] == [(None, "yes"), ("adult", None)]
    finally:
        await engine.dispose()
    assert await schema_of(scratch_database_url) == before


async def test_every_account_is_given_its_means_of_proof(scratch_database_url):
    """The password for each account, the passkey for the ones that have a passkey."""
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0036")
    engine = create_async_engine(scratch_database_url)
    try:
        async with engine.begin() as connection:
            for name, username in (("Alex", "alex"), ("Sam", "sam")):
                await connection.execute(
                    text(
                        "WITH p AS (INSERT INTO persons (display_name, role) "
                        f"VALUES ('{name}', 'adult') RETURNING id) "
                        "INSERT INTO accounts (person_id, username, password_hash) "
                        f"SELECT id, '{username}', 'hash' FROM p"
                    )
                )
            await connection.execute(
                text(
                    "INSERT INTO webauthn_credentials (account_id, credential_id, public_key, "
                    "sign_count, transports, label) SELECT id, decode('63', 'hex'), "
                    "decode('6b', 'hex'), 0, '{}'::text[], 'phone' "
                    "FROM accounts WHERE username = 'alex'"
                )
            )
        await asyncio.to_thread(command.upgrade, config, "head")
        async with engine.connect() as connection:
            rows = await connection.execute(
                text(
                    "SELECT a.username, m.kind, m.proposed_by, m.removed_at FROM proof_means m "
                    "JOIN accounts a ON a.person_id = m.person_id ORDER BY a.username, m.kind"
                )
            )
            assert [tuple(row) for row in rows.all()] == [
                ("alex", "passkey", "code", None),
                ("alex", "password", "code", None),
                ("sam", "password", "code", None),
            ]
        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO proof_means (person_id, kind, proposed_by, agreed_at) "
                        "SELECT person_id, 'password', 'code', now() FROM accounts "
                        "WHERE username = 'sam'"
                    )
                )
    finally:
        await engine.dispose()


async def test_0062_counts_the_sound_of_a_call_and_takes_it_back(scratch_database_url):
    """The calls of sound gain their seconds and characters and the five kinds of tasks; going
    back down drops them, and the calls of those kinds, and gives the schema of 0057 back."""
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0057")
    before = await schema_of(scratch_database_url)
    engine = create_async_engine(scratch_database_url)
    call = (
        "INSERT INTO model_calls (task, model_id, status, reserved_usd) "
        "VALUES ('{task}', 'm', 'settled', 0)"
    )
    try:
        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(text(call.format(task="hear")))
        await asyncio.to_thread(command.upgrade, config, "0062")
        async with engine.begin() as connection:
            await connection.execute(text(call.format(task="conversation")))
            await connection.execute(text(call.format(task="say")))
            rows = await connection.execute(
                text("SELECT task, heard_seconds, said_chars FROM model_calls ORDER BY task")
            )
            assert [tuple(row) for row in rows] == [("conversation", 0, 0), ("say", 0, 0)]
        await asyncio.to_thread(command.downgrade, config, "0057")
        async with engine.connect() as connection:
            rows = await connection.execute(text("SELECT task FROM model_calls"))
            assert rows.scalars().all() == ["conversation"]
    finally:
        await engine.dispose()
    assert await schema_of(scratch_database_url) == before


async def test_the_task_constraint_takes_the_speech_kinds(session):
    for task in ("hear", "say", "voice_print", "attention", "heard_sorting"):
        await session.execute(
            text(
                "INSERT INTO model_calls (task, model_id, status, reserved_usd) "
                f"VALUES ('{task}', 'm', 'settled', 0)"
            )
        )
    with pytest.raises(IntegrityError):
        await session.execute(
            text(
                "INSERT INTO model_calls (task, model_id, status, reserved_usd) "
                "VALUES ('humming', 'm', 'settled', 0)"
            )
        )


async def test_0063_makes_the_voice_tables_and_takes_them_back(scratch_database_url):
    """References, prints and moments of the voice: schema only, symmetric downgrade."""
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0062")
    before = await schema_of(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0063")
    after = await schema_of(scratch_database_url)
    tables = {row[0] for row in after["columns"]}
    assert {"voice_refs", "voice_prints", "voice_moments"} <= tables
    assert not [
        row for row in after["columns"] if row[0].startswith("voice_") and row[2] == "bytea"
    ]
    assert any(row[1] == "uq_voice_prints_ref_id" for row in after["constraints"])
    assert any(row[1] == "ck_voice_prints_vector_size" for row in after["constraints"])
    assert any(row[:2] == ("voice_prints", "learnt_at") for row in after["columns"])
    assert any(row[:2] == ("voice_prints", "heard_at") for row in after["columns"])
    assert any(row[:2] == ("voice_prints", "moved_in_visit") for row in after["columns"])
    # One live moment per person is the database's guarantee.
    assert any(
        row[:2] == ("voice_moments", "ix_voice_moments_person_id") and "UNIQUE" in str(row[2])
        for row in after["indexes"]
    )
    await asyncio.to_thread(command.downgrade, config, "0062")
    assert await schema_of(scratch_database_url) == before


async def test_upgrade_then_downgrade_leaves_no_table(scratch_database_url, list_tables):
    """0055 makes the table of what went wrong; going back down leaves the schema of 0054."""
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0054")
    before = await schema_of(scratch_database_url)
    tables_before = await list_tables(scratch_database_url)
    assert "went_wrong" not in tables_before
    await asyncio.to_thread(command.upgrade, config, "0055")
    assert await list_tables(scratch_database_url) == tables_before | {"went_wrong"}
    after = await schema_of(scratch_database_url)
    assert ("went_wrong", "by_domo", "boolean", "NO", "false") in after["columns"]
    assert ("went_wrong", "of_domo", "boolean", "NO", "false") in after["columns"]
    indexes = {row[1]: row[2] for row in after["indexes"]}
    assert "(action_type, at DESC)" in indexes["ix_went_wrong_action_type_at"]
    assert "(subject_key, at DESC)" in indexes["ix_went_wrong_subject_key_at"]
    await asyncio.to_thread(command.downgrade, config, "0054")
    assert await list_tables(scratch_database_url) == tables_before
    assert await schema_of(scratch_database_url) == before


async def test_0056_gives_a_memory_its_author_and_what_a_lesson_is_about(scratch_database_url):
    """0056 adds the assistant's authorship, a lesson's target and a warning's answer to
    ``memories``; going back down leaves the schema of 0055."""
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0055")
    before = await schema_of(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0056")
    after = await schema_of(scratch_database_url)
    columns = {(row[0], row[1]): row[2:] for row in after["columns"]}
    assert columns[("memories", "by_domo")] == ("boolean", "NO", "false")
    assert columns[("memories", "for_everyone")] == ("boolean", "NO", "false")
    for name in ("lesson_action_type", "lesson_subject_key", "told_answer", "told_keep_asking"):
        assert columns[("memories", name)][1] == "YES"
    indexes = {row[1]: row[2] for row in after["indexes"]}
    assert "WHERE (lesson_action_type IS NOT NULL)" in indexes["ix_memories_lesson_action_type"]
    await asyncio.to_thread(command.downgrade, config, "0055")
    assert await schema_of(scratch_database_url) == before


async def test_0057_gives_actions_and_requests_their_subject_idea_and_who_answers(
    scratch_database_url,
):
    """0057 adds the subject key and the assistant's reason to actions and requests, and to a
    request who answers, the words of a refusal and when an expiry woke him; going back down
    leaves the schema of 0056."""
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0056")
    before = await schema_of(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0057")
    after = await schema_of(scratch_database_url)
    columns = {(row[0], row[1]): row[2:] for row in after["columns"]}
    for table, names in (
        ("message_actions", ("subject_key", "idea_reason")),
        (
            "confirmations",
            ("subject_key", "idea_reason", "concerned_id", "refusal_words", "expiry_woken_at"),
        ),
    ):
        for name in names:
            assert columns[(table, name)][1] == "YES", (table, name)
    indexes = {row[1]: row[2] for row in after["indexes"]}
    assert "(subject_key, created_at DESC)" in indexes["ix_message_actions_subject_key_created_at"]
    assert "WHERE (subject_key IS NOT NULL)" in indexes["ix_message_actions_subject_key_created_at"]
    await asyncio.to_thread(command.downgrade, config, "0056")
    assert await schema_of(scratch_database_url) == before


async def test_0065_marks_a_voice_to_be_learnt_again_and_takes_it_back(scratch_database_url):
    """``voice_refs.relearn_at`` (empty: nothing to learn again); going back down drops it."""
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0064")
    before = await schema_of(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0065")
    after = await schema_of(scratch_database_url)
    columns = {(row[0], row[1]): row[2:] for row in after["columns"]}
    assert columns[("voice_refs", "relearn_at")][:2] == ("timestamp with time zone", "YES")
    await asyncio.to_thread(command.downgrade, config, "0064")
    assert await schema_of(scratch_database_url) == before


async def test_0069_lets_a_memory_come_from_an_unsure_voice_and_takes_it_back(
    scratch_database_url,
):
    """``memories.origin`` may be ``unsure_voice`` (the owner, 2026-11-27: what is kept from a
    voice the house was not sure of keeps that source); going back down makes such a memory
    relayed words (``others_words``, the most cautious a person's could be) and brings the schema
    of 0068 back, every other origin untouched."""
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0068")
    before = await schema_of(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0069")
    sam = "00000000-0000-0000-0000-000000000001"
    origins = ("own_words", "others_words", "deduced", "external", "unsure_voice")
    engine = create_async_engine(scratch_database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    f"INSERT INTO persons (id, display_name, role) VALUES ('{sam}', 'Sam', 'owner')"
                )
            )
            for origin in origins:
                await connection.execute(
                    text(
                        "INSERT INTO memories (said_by_id, subject, content, kind, origin, "
                        "kept_private, core, created_at, changed_at) "
                        f"VALUES ('{sam}', :origin, 'c', 'fact', :origin, false, false, "
                        "now(), now())"
                    ),
                    {"origin": origin},
                )
        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO memories (said_by_id, subject, content, kind, origin, "
                        "kept_private, core, created_at, changed_at) "
                        f"VALUES ('{sam}', 's', 'c', 'fact', 'heard', false, false, now(), now())"
                    )
                )
        await asyncio.to_thread(command.downgrade, config, "0068")
        async with engine.begin() as connection:
            found = await connection.execute(text("SELECT subject, origin FROM memories"))
            assert dict(found.all()) == {
                "own_words": "own_words",
                "others_words": "others_words",
                "deduced": "deduced",
                "external": "external",
                "unsure_voice": "others_words",
            }
            await connection.execute(text("DELETE FROM persons"))
    finally:
        await engine.dispose()
    assert await schema_of(scratch_database_url) == before


def test_the_line_logged_for_each_migration_quotes_no_word_the_voice_trial_says():
    """Alembic logs every step at start ("Running upgrade 0074 -> 0075, <first paragraph of the
    docstring>"). The voice trial checks that none of the words it says out loud reaches the
    core's log: a migration's example sentence quoting one of them would make that check fail for
    a reason that is not something heard."""
    heard = ("lumière", "lumiere", "salon", "fromage", "garage")
    scripts = ScriptDirectory.from_config(alembic_config("postgresql+asyncpg://unused/unused"))
    logged = {script.revision: (script.doc or "").lower() for script in scripts.walk_revisions()}
    assert logged
    assert {
        revision: [word for word in heard if word in line]
        for revision, line in logged.items()
        if any(word in line for word in heard)
    } == {}
