"""The database layer and the fixtures every other core test relies on."""

from domo_core.accounts.models import Person
from domo_core.config import CoreSettings
from domo_core.db import Base, create_engine, load_models, make_session_factory
from sqlalchemy import text


async def test_the_engine_is_built_from_the_settings(settings):
    engine = create_engine(settings)
    try:
        async with engine.connect() as connection:
            assert (await connection.execute(text("SELECT 1"))).scalar() == 1
        assert engine.dialect.driver == "asyncpg"
    finally:
        await engine.dispose()


async def test_the_engine_does_not_show_the_password(tmp_path):
    password_file = tmp_path / "db_password"
    password_file.write_text("do-not-leak-me", encoding="utf-8")
    engine = create_engine(CoreSettings(db_password_file=password_file))
    assert "do-not-leak-me" not in repr(engine) and "do-not-leak-me" not in str(engine.url)
    await engine.dispose()


async def test_sessions_keep_loaded_attributes_after_a_commit(engine):
    """Without expiry, an object can still be read after commit (lazy loading is impossible in
    async code)."""
    async with make_session_factory(engine)() as independent:
        person = Person(display_name="A", role="owner")
        independent.add(person)
        await independent.commit()
        assert person.display_name == "A"


def test_every_table_is_declared_on_the_shared_base():
    load_models()
    assert set(Base.metadata.tables) == {
        "persons",
        "accounts",
        "web_sessions",
        "setup_state",
        "providers",
        "conversations",
        "messages",
        "audit_log",
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
        "service_accounts",
        "voice_refs",
        "voice_prints",
        "voice_moments",
        # And what went wrong, kept for the judge to learn from (domo judges first).
        "went_wrong",
    }


# --- the fixtures --------------------------------------------------------------------------


async def test_a_commit_in_the_session_fixture_stays_inside_the_test(
    session, session_factory, count_persons
):
    session.add(Person(display_name="A", role="owner"))
    await session.commit()
    assert await count_persons(session) == 1
    async with session_factory() as other:
        assert await count_persons(other) == 0  # never committed for real


async def test_the_session_fixture_survives_a_rollback_in_the_test(session, count_persons):
    session.add(Person(display_name="A", role="owner"))
    await session.flush()
    await session.rollback()
    assert await count_persons(session) == 0
    session.add(Person(display_name="B", role="owner"))
    await session.flush()
    assert await count_persons(session) == 1


async def test_an_independent_session_commits_for_real(session_factory, count_persons):
    async with session_factory() as one:
        one.add(Person(display_name="A", role="owner"))
        await one.commit()
    async with session_factory() as two:
        assert await count_persons(two) == 1


async def test_what_an_independent_session_committed_is_gone_in_the_next_test(
    session_factory, count_persons
):
    # Depends on the test above, which commits one person: the reset must have removed it.
    async with session_factory() as other:
        assert await count_persons(other) == 0


async def test_a_test_may_delete_the_setup_state_row(session_factory):
    async with session_factory() as writer:
        await writer.execute(text("DELETE FROM setup_state"))
        await writer.commit()


async def test_the_reset_puts_back_a_setup_state_row_a_test_deleted(session):
    # Depends on the test above, which commits the deletion of the row: the reset must have
    # put it back, empty (an UPDATE would match nothing and every later test would miss the row).
    rows = (
        await session.execute(
            text(
                "SELECT id, setup_code_hash, code_created_at, owner_created_at, "
                "model_configured_at FROM setup_state"
            )
        )
    ).all()
    assert [tuple(row) for row in rows] == [(1, None, None, None, None)]
