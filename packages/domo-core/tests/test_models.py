import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from domo_core.accounts.models import Account, Person, WebSession
from domo_core.chat.models import Conversation, Message
from domo_core.llm.models import Provider
from domo_core.setup_flow.models import SetupState
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

# --- identifiers and timestamps ------------------------------------------------------------


async def test_the_database_generates_the_uuid_primary_keys(
    session, add_person, add_account, add_conversation
):
    person = await add_person()
    account = await add_account(person)
    conversation = await add_conversation(person)
    ids = {person.id, account.id, conversation.id}
    assert len(ids) == 3 and all(isinstance(value, uuid.UUID) for value in ids)


async def test_a_row_inserted_with_plain_sql_gets_its_defaults(session):
    await session.execute(text("INSERT INTO persons (display_name, role) VALUES ('A', 'owner')"))
    person = (await session.execute(select(Person))).scalar_one()
    assert isinstance(person.id, uuid.UUID) and person.created_at.tzinfo is not None


async def test_timestamps_are_timezone_aware_utc(add_person):
    person = await add_person()
    assert person.created_at.utcoffset() == timedelta(0)
    assert abs(datetime.now(UTC) - person.created_at) < timedelta(minutes=1)


async def test_timestamps_keep_the_instant_they_were_given(session, add_person, add_account):
    account = await add_account(await add_person())
    expires = datetime(2031, 5, 17, 9, 30, tzinfo=UTC)
    web_session = WebSession(account_id=account.id, token_hash="a" * 64, expires_at=expires)
    session.add(web_session)
    await session.flush()
    await session.refresh(web_session)
    assert web_session.expires_at == expires and web_session.expires_at.utcoffset() == timedelta(0)
    assert web_session.last_seen_at.tzinfo is not None


async def test_rows_created_in_one_transaction_get_increasing_creation_times(
    add_conversation, add_message
):
    """now() would give every row of a transaction the same time and lose the message order."""
    conversation = await add_conversation()
    first = await add_message(conversation)
    second = await add_message(conversation, role="assistant", provenance="model")
    assert first.created_at < second.created_at


# --- persons and accounts ------------------------------------------------------------------


async def test_one_account_per_person(session, add_person, add_account):
    person = await add_person()
    await add_account(person, username="first")
    session.add(Account(person_id=person.id, username="second", password_hash="hash"))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_usernames_are_unique(session, add_person, add_account):
    await add_account(await add_person("A", "owner"), username="same")
    other = await add_person("B", "guest")
    session.add(Account(person_id=other.id, username="same", password_hash="hash"))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_usernames_are_stored_in_lower_case(session, add_person):
    person = await add_person()
    session.add(Account(person_id=person.id, username="Owner", password_hash="hash"))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_an_account_needs_an_existing_person(session):
    session.add(Account(person_id=uuid.uuid4(), username="nobody", password_hash="hash"))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_token_hashes_are_unique(session, add_person, add_account):
    account = await add_account(await add_person())
    expires = datetime.now(UTC) + timedelta(days=1)
    session.add_all(
        [
            WebSession(account_id=account.id, token_hash="t" * 64, expires_at=expires),
            WebSession(account_id=account.id, token_hash="t" * 64, expires_at=expires),
        ]
    )
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_sessions_go_with_their_account(session, add_person, add_account):
    account = await add_account(await add_person())
    session.add(
        WebSession(
            account_id=account.id,
            token_hash="t" * 64,
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
    )
    await session.flush()
    await session.delete(account)
    await session.flush()
    assert (await session.execute(select(WebSession))).first() is None


# --- setup state ---------------------------------------------------------------------------


async def test_setup_state_starts_as_one_empty_row(session):
    state = (await session.execute(select(SetupState))).scalar_one()
    assert state.id == 1
    assert state.setup_code_hash is None
    assert state.code_created_at is None
    assert state.owner_created_at is None
    assert state.model_configured_at is None


async def test_setup_state_is_a_singleton(session):
    session.add(SetupState(id=2))
    with pytest.raises(IntegrityError):
        await session.flush()


# --- providers -----------------------------------------------------------------------------


async def test_provider_ids_look_like_p_and_eight_hex_digits(session, add_person):
    owner = await add_person()
    providers = [
        Provider(kind="anthropic", base_url="https://api.example.com", given_by=owner.id)
        for _ in range(3)
    ]
    session.add_all(providers)
    await session.flush()
    ids = {provider.id for provider in providers}
    assert len(ids) == 3
    assert all(re.fullmatch(r"p-[0-9a-f]{8}", value) for value in ids)


async def test_provider_ids_must_keep_their_format(session, add_person):
    owner = await add_person()
    session.add(
        Provider(id="provider-1", kind="anthropic", base_url="https://x", given_by=owner.id)
    )
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_a_provider_may_have_no_secret(session, add_person):
    owner = await add_person()
    provider = Provider(
        kind="openai_compatible", base_url="http://192.168.1.10:11434", given_by=owner.id
    )
    session.add(provider)
    await session.flush()
    assert provider.secret_ref is None


# --- conversations and messages ------------------------------------------------------------


async def test_a_message_round_trips(session, add_conversation, add_message):
    message = await add_message(
        await add_conversation(), role="assistant", provenance="model", model="some-model"
    )
    await session.refresh(message)
    assert (message.role, message.status, message.provenance, message.model) == (
        "assistant",
        "complete",
        "model",
        "some-model",
    )
    assert message.created_at.tzinfo is not None and message.updated_at.tzinfo is not None


async def test_a_message_defaults_to_empty_content_and_no_model(session, add_conversation):
    """The streaming placeholder of an assistant reply starts empty."""
    conversation = await add_conversation()
    message = Message(
        conversation_id=conversation.id, role="assistant", status="streaming", provenance="model"
    )
    session.add(message)
    await session.flush()
    await session.refresh(message)
    assert message.content == "" and message.model is None


async def test_updating_a_message_moves_updated_at_only(session, add_conversation, add_message):
    message = await add_message(await add_conversation(), status="streaming")
    created, first_update = message.created_at, message.updated_at
    message.status = "complete"
    await session.flush()
    assert message.created_at == created
    assert message.updated_at > first_update


@pytest.mark.parametrize(
    "overrides",
    [{"role": "system"}, {"status": "done"}, {"provenance": "tool"}],
    ids=["role", "status", "provenance"],
)
async def test_message_values_are_constrained(add_conversation, add_message, overrides):
    conversation = await add_conversation()
    with pytest.raises(IntegrityError):
        await add_message(conversation, **overrides)


@pytest.mark.parametrize("status", ["streaming", "complete", "partial", "error"])
async def test_every_message_status_is_accepted(add_conversation, add_message, status):
    await add_message(await add_conversation(), status=status)


async def test_messages_go_with_their_conversation(session, add_conversation, add_message):
    conversation = await add_conversation()
    await add_message(conversation)
    await session.delete(conversation)
    await session.flush()
    assert (await session.execute(select(Message))).first() is None


async def test_a_conversation_belongs_to_a_person(session):
    session.add(Conversation(person_id=uuid.uuid4()))
    with pytest.raises(IntegrityError):
        await session.flush()


# --- audit log -----------------------------------------------------------------------------


async def test_audit_ids_increase_and_details_keep_their_json(session, make_audit_entry):
    entries = [
        make_audit_entry(index, details={"nested": {"i": index}, "list": [1, "a"]})
        for index in range(3)
    ]
    for entry in entries:
        session.add(entry)
        await session.flush()
    assert entries[0].id < entries[1].id < entries[2].id
    await session.refresh(entries[2])
    assert entries[2].details == {"nested": {"i": 2}, "list": [1, "a"]}
    assert entries[2].ts.utcoffset() == timedelta(0)
    assert len(entries[2].hash) == 64 and len(entries[2].prev_hash) == 64


async def test_audit_hashes_are_unique(session, make_audit_entry):
    session.add_all([make_audit_entry(1), make_audit_entry(2, hash=f"{1:064x}")])
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_an_audit_hash_cannot_be_longer_than_64_characters(session, make_audit_entry):
    session.add(make_audit_entry(1, hash="f" * 65))
    with pytest.raises(DBAPIError, match="too long"):
        await session.flush()
