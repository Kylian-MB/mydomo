"""The schema of iteration 1b: the household, confirmations, message actions, notes, passkeys and
the owner's policy settings.

The tests run on the database the migration made, through the ORM models: a constraint that
the migration or a model forgets fails here. ``test_migrations`` checks that the two agree.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from domo_core.accounts.models import Person
from domo_core.accounts.passkey_models import PasskeyChallenge, PasskeyCredential
from domo_core.chat.models import MessageAction
from domo_core.confirmations.models import Confirmation
from domo_core.household.models import Invitation
from domo_core.notes.models import Note
from domo_core.policy.models import PolicySetting
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

CONFIRMATION_STATUSES = (
    "pending",
    "executing",
    "executed",
    "refused",
    "expired",
    "failed",
    "replaced",
)


def utc_in(**delta: float) -> datetime:
    return datetime.now(UTC) + timedelta(**delta)


@pytest.fixture
async def owner(add_person):
    return await add_person("Owner", "owner")


@pytest.fixture
async def adult(add_person):
    return await add_person("Sam", "adult")


# --- persons -------------------------------------------------------------------------------


async def test_person_role_is_constrained(session):
    session.add(Person(display_name="X", role="admin"))
    with pytest.raises(IntegrityError):
        await session.flush()


@pytest.mark.parametrize("role", ["member", "Owner", ""])
async def test_only_the_four_household_roles_are_accepted(session, role):
    session.add(Person(display_name="X", role=role))
    with pytest.raises(IntegrityError):
        await session.flush()


@pytest.mark.parametrize("role", ["owner", "adult", "child", "guest"])
async def test_every_household_role_is_accepted(session, role):
    session.add(Person(display_name="X", role=role))
    await session.flush()


async def test_a_new_person_is_active_and_was_not_invited(add_person):
    person = await add_person("Alex", "adult")
    assert person.disabled_at is None and person.expires_at is None and person.invited_by is None


async def test_a_person_keeps_when_they_stop_and_who_invited_them(session, owner):
    until = utc_in(days=2)
    guest = Person(
        display_name="Alex",
        role="guest",
        invited_by=owner.id,
        expires_at=until,
        disabled_at=utc_in(days=1),
    )
    session.add(guest)
    await session.flush()
    await session.refresh(guest)
    assert guest.invited_by == owner.id
    assert guest.expires_at == until and guest.expires_at.utcoffset() == timedelta(0)
    assert guest.disabled_at is not None and guest.disabled_at.utcoffset() == timedelta(0)


async def test_a_person_can_only_be_invited_by_a_person_that_exists(session):
    session.add(Person(display_name="Alex", role="adult", invited_by=uuid.uuid4()))
    with pytest.raises(IntegrityError):
        await session.flush()


# --- invitations ---------------------------------------------------------------------------


def an_invitation(owner: Person, **overrides: object) -> Invitation:
    values: dict[str, object] = {
        "token_hash": "h" * 64,
        "display_name": "Alex",
        "role": "adult",
        "created_by": owner.id,
        "expires_at": utc_in(days=7),
    } | overrides
    return Invitation(**values)


async def test_guest_days_only_for_guests(session, owner):
    session.add(
        Invitation(
            token_hash="h" * 64,
            display_name="Alex",
            role="adult",
            guest_days=3,
            created_by=owner.id,
            expires_at=utc_in(days=7),
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()


@pytest.mark.parametrize("role", ["adult", "child"])
async def test_an_invitation_that_is_not_for_a_guest_has_no_guest_days(session, owner, role):
    invitation = an_invitation(owner, role=role)
    session.add(invitation)
    await session.flush()
    assert invitation.guest_days is None


@pytest.mark.parametrize("days", [1, 2, 30])
async def test_a_guest_invitation_has_one_to_thirty_days(session, owner, days):
    invitation = an_invitation(owner, role="guest", guest_days=days)
    session.add(invitation)
    await session.flush()
    await session.refresh(invitation)
    assert invitation.guest_days == days


@pytest.mark.parametrize("days", [-1, 0, 366])
async def test_guest_days_outside_one_to_the_ceiling_are_refused(session, owner, days):
    """The table keeps a technical ceiling only: the household's rule (30 days unless the owner
    chose otherwise) is a limit of the catalog (``test_policy_limits.py``)."""
    session.add(an_invitation(owner, role="guest", guest_days=days))
    with pytest.raises(IntegrityError):
        await session.flush()


@pytest.mark.parametrize("role", ["adult", "child", "guest"])
async def test_an_invitation_is_for_an_adult_a_child_or_a_guest(session, owner, role):
    session.add(an_invitation(owner, role=role))
    await session.flush()


@pytest.mark.parametrize("role", ["owner", "admin", "member"])
async def test_nobody_is_invited_as_owner_or_with_another_role(session, owner, role):
    session.add(an_invitation(owner, role=role))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_a_new_invitation_is_open(session, owner):
    invitation = an_invitation(owner)
    session.add(invitation)
    await session.flush()
    await session.refresh(invitation)
    assert invitation.created_at.tzinfo is not None
    assert invitation.accepted_at is None
    assert invitation.accepted_person_id is None
    assert invitation.cancelled_at is None


async def test_invitation_token_hashes_are_unique(session, owner):
    session.add_all([an_invitation(owner), an_invitation(owner, display_name="Sam")])
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_an_invitation_needs_the_person_who_made_it(session):
    session.add(
        Invitation(
            token_hash="h" * 64,
            display_name="Alex",
            role="adult",
            created_by=uuid.uuid4(),
            expires_at=utc_in(days=7),
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_an_accepted_invitation_points_at_the_person_it_created(session, owner, adult):
    invitation = an_invitation(owner, accepted_at=utc_in(days=1), accepted_person_id=adult.id)
    session.add(invitation)
    await session.flush()
    assert invitation.accepted_person_id == adult.id


async def test_an_invitation_cannot_create_a_person_that_does_not_exist(session, owner):
    session.add(an_invitation(owner, accepted_person_id=uuid.uuid4()))
    with pytest.raises(IntegrityError):
        await session.flush()


# --- confirmations -------------------------------------------------------------------------


@pytest.fixture
async def make_confirmation(session, add_conversation):
    """``await make_confirmation(person, **overrides)``: a pending confirmation of ``person``,
    flushed."""

    async def _make_confirmation(person: Person, **overrides: object) -> Confirmation:
        conversation = overrides.pop("conversation", None) or await add_conversation(person)
        values: dict[str, object] = {
            "requester_id": person.id,
            "approver_kind": "self",
            "conversation_id": conversation.id,
            "tool_name": "household_invite",
            "arguments": {"display_name": "Alex", "role": "adult"},
            "action": {"type": "household.invite", "summary": "Invite Alex"},
            "status": "pending",
        } | overrides
        confirmation = Confirmation(**values)
        session.add(confirmation)
        await session.flush()
        return confirmation

    return _make_confirmation


async def test_a_confirmation_round_trips_and_starts_pending_with_no_reminder(
    session, owner, make_confirmation
):
    confirmation = await make_confirmation(
        owner,
        arguments={"nested": {"list": [1, "a", None]}},
        action={"type": "household.invite", "flags": [True, False]},
    )
    await session.refresh(confirmation)
    assert isinstance(confirmation.id, uuid.UUID)
    assert confirmation.arguments == {"nested": {"list": [1, "a", None]}}
    assert confirmation.action == {"type": "household.invite", "flags": [True, False]}
    assert confirmation.created_at.utcoffset() == timedelta(0)
    assert confirmation.reminder_count == 0 and confirmation.last_reminded_at is None
    for name in (
        "ask",
        "asked_by",
        "ask_why",
        "message_id",
        "decided_by",
        "decided_at",
        "decided_via",
        "decided_message_id",
        "amended_arguments",
        "replaced_by",
        "expired_by",
        "result",
    ):
        assert getattr(confirmation, name) is None, name


def test_a_confirmation_stores_no_expiry_date():
    """The ceiling is worked out at every read from ``created_at`` and the owner's setting."""
    assert "expires_at" not in Confirmation.__table__.columns


@pytest.mark.parametrize("status", CONFIRMATION_STATUSES)
async def test_every_confirmation_status_is_accepted(owner, make_confirmation, status):
    await make_confirmation(owner, status=status)


@pytest.mark.parametrize("status", ["done", "approved", "PENDING", ""])
async def test_other_confirmation_statuses_are_refused(owner, make_confirmation, status):
    with pytest.raises(IntegrityError):
        await make_confirmation(owner, status=status)


@pytest.mark.parametrize(
    "overrides",
    [
        {"approver_kind": "child"},
        {"ask": "free"},
        {"ask": "confirm"},
        {"asked_by": "model"},
        {"decided_via": "voice"},
        {"expired_by": "owner"},
    ],
    ids=["approver_kind", "ask", "ask_level", "asked_by", "decided_via", "expired_by"],
)
async def test_confirmation_values_are_constrained(owner, make_confirmation, overrides):
    with pytest.raises(IntegrityError):
        await make_confirmation(owner, **overrides)


@pytest.mark.parametrize("kind", ["self", "adult", "owner"])
async def test_a_confirmation_is_approved_by_oneself_an_adult_or_the_owner(
    owner, make_confirmation, kind
):
    await make_confirmation(owner, approver_kind=kind)


@pytest.mark.parametrize("ask", ["yes", "explicit_yes", "proof"])
@pytest.mark.parametrize("by", ["rule", "judge", "domo", "approver"])
async def test_a_confirmation_keeps_what_is_asked_and_by_whom(
    session, owner, make_confirmation, ask, by
):
    confirmation = await make_confirmation(owner, ask=ask, asked_by=by, ask_why="why")
    await session.refresh(confirmation)
    assert (confirmation.ask, confirmation.asked_by, confirmation.ask_why) == (ask, by, "why")


@pytest.mark.parametrize(
    "via", ["button", "explicit_phrase", "phrase", "classifier", "change", "proof", "judge"]
)
async def test_every_way_of_deciding_is_accepted(owner, make_confirmation, via):
    confirmation = await make_confirmation(
        owner, status="executed", decided_by=owner.id, decided_at=utc_in(), decided_via=via
    )
    assert confirmation.decided_via == via


@pytest.mark.parametrize("by", ["domo", "lifetime"])
async def test_a_confirmation_expires_by_domo_or_by_its_lifetime(owner, make_confirmation, by):
    await make_confirmation(owner, status="expired", expired_by=by)


@pytest.mark.parametrize("length,accepted", [(1, True), (200, True), (201, False)])
async def test_the_reason_of_what_is_asked_is_at_most_200_characters(
    owner, make_confirmation, length, accepted
):
    if accepted:
        await make_confirmation(owner, ask="explicit_yes", asked_by="domo", ask_why="r" * length)
    else:
        with pytest.raises(IntegrityError):
            await make_confirmation(
                owner, ask="explicit_yes", asked_by="domo", ask_why="r" * length
            )


async def test_a_decided_confirmation_keeps_who_how_and_what_came_out(
    session, owner, adult, add_conversation, add_message, make_confirmation
):
    conversation = await add_conversation(adult)
    asked = await add_message(conversation)
    answer = await add_message(conversation)
    confirmation = await make_confirmation(
        adult,
        conversation=conversation,
        message_id=asked.id,
        approver_kind="adult",
        status="executed",
        decided_by=owner.id,
        decided_at=utc_in(),
        decided_via="button",
        decided_message_id=answer.id,
        amended_arguments={"display_name": "Alex B"},
        result={"status": "done", "card": {"kind": "invitation"}},
    )
    await session.refresh(confirmation)
    assert confirmation.requester_id == adult.id and confirmation.decided_by == owner.id
    assert confirmation.message_id == asked.id and confirmation.decided_message_id == answer.id
    assert confirmation.amended_arguments == {"display_name": "Alex B"}
    assert confirmation.result == {"status": "done", "card": {"kind": "invitation"}}


async def test_a_replaced_confirmation_points_at_its_replacement(owner, make_confirmation):
    new = await make_confirmation(owner)
    old = await make_confirmation(owner, status="replaced", replaced_by=new.id)
    assert old.replaced_by == new.id


async def test_a_confirmation_cannot_be_replaced_by_one_that_does_not_exist(
    owner, make_confirmation
):
    with pytest.raises(IntegrityError):
        await make_confirmation(owner, status="replaced", replaced_by=uuid.uuid4())


async def test_a_confirmation_needs_its_requester_and_its_conversation(
    session, owner, add_conversation
):
    conversation = await add_conversation(owner)
    session.add(
        Confirmation(
            requester_id=uuid.uuid4(),
            approver_kind="self",
            conversation_id=conversation.id,
            tool_name="t",
            arguments={},
            action={},
            status="pending",
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_a_confirmation_needs_a_conversation_that_exists(session, owner):
    session.add(
        Confirmation(
            requester_id=owner.id,
            approver_kind="self",
            conversation_id=uuid.uuid4(),
            tool_name="t",
            arguments={},
            action={},
            status="pending",
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_the_pending_requests_of_a_person_are_indexed(session):
    definition = (
        await session.execute(
            text(
                "SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' "
                "AND tablename = 'confirmations' "
                "AND indexname = 'ix_confirmations_requester_id_status'"
            )
        )
    ).scalar_one()
    assert "(requester_id, status)" in definition


# --- message actions -----------------------------------------------------------------------


async def test_a_message_action_round_trips_and_goes_with_its_message(
    session, owner, add_conversation, add_message
):
    message = await add_message(await add_conversation(owner), role="assistant", provenance="model")
    action = MessageAction(
        message_id=message.id,
        tool_name="note_leave",
        action_type="note.leave",
        status="done",
        summary={"title": "Note left", "items": ["Sam"]},
    )
    session.add(action)
    await session.flush()
    await session.refresh(action)
    assert isinstance(action.id, uuid.UUID) and action.created_at.tzinfo is not None
    assert action.confirmation_id is None
    assert action.summary == {"title": "Note left", "items": ["Sam"]}
    await session.delete(message)
    await session.flush()
    assert (await session.execute(select(MessageAction))).first() is None


@pytest.mark.parametrize("status", ["done", "waiting", "refused", "invalid", "failed"])
async def test_every_message_action_status_is_accepted(
    session, owner, add_conversation, add_message, status
):
    message = await add_message(await add_conversation(owner))
    session.add(
        MessageAction(
            message_id=message.id, tool_name="t", action_type="a", status=status, summary={}
        )
    )
    await session.flush()


@pytest.mark.parametrize("status", ["pending", "executed", "DONE", ""])
async def test_other_message_action_statuses_are_refused(
    session, owner, add_conversation, add_message, status
):
    message = await add_message(await add_conversation(owner))
    session.add(
        MessageAction(
            message_id=message.id, tool_name="t", action_type="a", status=status, summary={}
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_a_waiting_action_points_at_its_confirmation(
    session, owner, add_conversation, add_message, make_confirmation
):
    confirmation = await make_confirmation(owner)
    message = await add_message(await add_conversation(owner))
    action = MessageAction(
        message_id=message.id,
        tool_name="household_invite",
        action_type="household.invite",
        status="waiting",
        confirmation_id=confirmation.id,
        summary={},
    )
    session.add(action)
    await session.flush()
    assert action.confirmation_id == confirmation.id


async def test_a_message_action_cannot_point_at_a_confirmation_that_does_not_exist(
    session, owner, add_conversation, add_message
):
    message = await add_message(await add_conversation(owner))
    session.add(
        MessageAction(
            message_id=message.id,
            tool_name="t",
            action_type="a",
            status="waiting",
            confirmation_id=uuid.uuid4(),
            summary={},
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_a_message_action_needs_a_message_that_exists(session):
    session.add(
        MessageAction(
            message_id=uuid.uuid4(), tool_name="t", action_type="a", status="done", summary={}
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()


# --- notes ---------------------------------------------------------------------------------


async def test_note_text_length_is_constrained(session, owner, adult):
    session.add(Note(author_id=owner.id, recipient_id=adult.id, text="x" * 1001))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_an_empty_note_is_refused(session, owner, adult):
    session.add(Note(author_id=owner.id, recipient_id=adult.id, text=""))
    with pytest.raises(IntegrityError):
        await session.flush()


@pytest.mark.parametrize("length", [1, 1000])
async def test_a_note_has_one_to_a_thousand_characters(session, owner, adult, length):
    note = Note(author_id=owner.id, recipient_id=adult.id, text="x" * length)
    session.add(note)
    await session.flush()
    await session.refresh(note)
    assert len(note.text) == length


async def test_a_new_note_is_neither_told_nor_taken_back(session, owner, adult):
    note = Note(author_id=owner.id, recipient_id=adult.id, text="Milk is in the fridge")
    session.add(note)
    await session.flush()
    await session.refresh(note)
    assert isinstance(note.id, uuid.UUID) and note.created_at.utcoffset() == timedelta(0)
    assert (note.seen_at, note.told_at, note.withdrawn_at) == (None, None, None)


async def test_a_note_needs_an_author_and_a_recipient_that_exist(session, owner):
    session.add(Note(author_id=owner.id, recipient_id=uuid.uuid4(), text="hello"))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_a_note_needs_an_author_that_exists(session, owner):
    session.add(Note(author_id=uuid.uuid4(), recipient_id=owner.id, text="hello"))
    with pytest.raises(IntegrityError):
        await session.flush()


# --- passkeys ------------------------------------------------------------------------------


def a_credential(account_id: uuid.UUID, **overrides: object) -> PasskeyCredential:
    values: dict[str, object] = {
        "account_id": account_id,
        "credential_id": b"\x01\x02\x03",
        "public_key": b"\x04\x05",
        "sign_count": 0,
        "transports": ["internal", "hybrid"],
        "label": "Alex's phone",
    } | overrides
    return PasskeyCredential(**values)


async def test_a_passkey_round_trips(session, account):
    credential = a_credential(account.id, sign_count=2**33, transports=[])
    session.add(credential)
    await session.flush()
    await session.refresh(credential)
    assert credential.credential_id == b"\x01\x02\x03" and credential.public_key == b"\x04\x05"
    assert credential.sign_count == 2**33  # a bigint: counters can pass 32 bits
    assert credential.transports == [] and credential.label == "Alex's phone"
    assert credential.created_at.tzinfo is not None and credential.last_used_at is None


async def test_a_passkey_keeps_its_transports(session, account):
    credential = a_credential(account.id)
    session.add(credential)
    await session.flush()
    await session.refresh(credential)
    assert credential.transports == ["internal", "hybrid"]


async def test_credential_ids_are_unique(session, account):
    session.add_all([a_credential(account.id), a_credential(account.id, label="Another")])
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_a_passkey_needs_an_account_that_exists(session):
    session.add(a_credential(uuid.uuid4()))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_passkeys_go_with_their_account(session, account):
    session.add(a_credential(account.id))
    await session.flush()
    await session.delete(account)
    await session.flush()
    assert (await session.execute(select(PasskeyCredential))).first() is None


@pytest.mark.parametrize("purpose", ["register", "login"])
async def test_a_challenge_is_for_registering_or_logging_in(session, purpose):
    challenge = PasskeyChallenge(purpose=purpose, challenge=b"\xaa" * 32, expires_at=utc_in())
    session.add(challenge)
    await session.flush()
    await session.refresh(challenge)
    assert challenge.account_id is None and challenge.used_at is None
    assert challenge.challenge == b"\xaa" * 32


async def test_a_challenge_for_something_else_is_refused(session):
    session.add(PasskeyChallenge(purpose="reset", challenge=b"\x00", expires_at=utc_in()))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_a_challenge_may_name_the_account_it_is_for(session, account):
    challenge = PasskeyChallenge(
        purpose="register", account_id=account.id, challenge=b"\x01", expires_at=utc_in()
    )
    session.add(challenge)
    await session.flush()
    assert challenge.account_id == account.id


# --- the owner's policy settings -----------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [True, 3, "ask", {"level": "confirm", "days": [1, 2]}, [1, "a"], 1.5],
    ids=["bool", "int", "str", "object", "list", "float"],
)
async def test_a_setting_holds_any_json_value(session, owner, value):
    session.add(PolicySetting(key="household.guest_days", value=value, updated_by=owner.id))
    await session.flush()
    session.expire_all()
    row = (await session.execute(select(PolicySetting))).scalar_one()
    assert row.value == value
    assert row.updated_at.utcoffset() == timedelta(0)


async def test_a_setting_has_a_value(session, owner):
    with pytest.raises(IntegrityError):
        await session.execute(
            text("INSERT INTO policy_settings (key, value, updated_by) VALUES ('k', NULL, :by)"),
            {"by": owner.id},
        )


async def test_a_key_has_one_row(session, owner):
    session.add_all(
        [
            PolicySetting(key="k", value=1, updated_by=owner.id),
            PolicySetting(key="k", value=2, updated_by=owner.id),
        ]
    )
    with pytest.raises(IntegrityError):
        await session.flush()


@pytest.mark.parametrize("length,accepted", [(1, True), (200, True), (201, False)])
async def test_a_setting_key_is_at_most_200_characters(session, owner, length, accepted):
    session.add(PolicySetting(key="k" * length, value=True, updated_by=owner.id))
    if accepted:
        await session.flush()
    else:
        with pytest.raises(IntegrityError):
            await session.flush()


async def test_a_setting_is_set_by_a_person_that_exists(session):
    session.add(PolicySetting(key="k", value=True, updated_by=uuid.uuid4()))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_no_setting_exists_until_the_owner_chooses_one(session):
    """No row means the default value: the migration seeds nothing."""
    assert (await session.execute(select(PolicySetting))).first() is None
