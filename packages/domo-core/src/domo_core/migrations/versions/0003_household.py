"""Schema of iteration 1b: the household, confirmations, notes, passkeys and policy settings.

* ``persons`` gets the end of a person's access (``disabled_at``, ``expires_at``), who invited
  them (``invited_by``) and a constraint on ``role`` (``owner``, ``adult``, ``child``, ``guest``);
* new tables: ``invitations``, ``confirmations``, ``message_actions``, ``notes``,
  ``webauthn_credentials``, ``webauthn_challenges`` and ``policy_settings``.

Written by hand, constraint names included (see 0001). ``tests/test_migrations.py`` checks that
the models and the migrations describe the same schema.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-30
"""

import uuid
from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Same server-side defaults as 0001.
NEW_UUID = sa.text("gen_random_uuid()")
NOW = sa.text("clock_timestamp()")


def _uuid_pk() -> sa.Column[uuid.UUID]:
    return sa.Column("id", sa.Uuid(), server_default=NEW_UUID, nullable=False)


def _created_at() -> sa.Column[datetime]:
    return sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False)


def upgrade() -> None:
    op.add_column("persons", sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("persons", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("persons", sa.Column("invited_by", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_persons_invited_by_persons"), "persons", "persons", ["invited_by"], ["id"]
    )
    op.create_check_constraint(
        op.f("ck_persons_role_values"), "persons", "role IN ('owner', 'adult', 'child', 'guest')"
    )

    op.create_table(
        "invitations",
        _uuid_pk(),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("guest_days", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        _created_at(),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_person_id", sa.Uuid(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "role IN ('adult', 'child', 'guest')", name=op.f("ck_invitations_role_values")
        ),
        sa.CheckConstraint(
            "guest_days BETWEEN 1 AND 30", name=op.f("ck_invitations_guest_days_range")
        ),
        sa.CheckConstraint(
            "guest_days IS NULL OR role = 'guest'",
            name=op.f("ck_invitations_guest_days_only_for_guests"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["persons.id"], name=op.f("fk_invitations_created_by_persons")
        ),
        sa.ForeignKeyConstraint(
            ["accepted_person_id"],
            ["persons.id"],
            name=op.f("fk_invitations_accepted_person_id_persons"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invitations")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_invitations_token_hash")),
    )

    op.create_table(
        "confirmations",
        _uuid_pk(),
        sa.Column("requester_id", sa.Uuid(), nullable=False),
        sa.Column("approver_kind", sa.Text(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=True),
        sa.Column("tool_name", sa.Text(), nullable=False),
        sa.Column("arguments", postgresql.JSONB(), nullable=False),
        sa.Column("action", postgresql.JSONB(), nullable=False),
        sa.Column("level", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        _created_at(),
        sa.Column("reminder_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_reminded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "explicit_required", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("explicit_reason", sa.Text(), nullable=True),
        sa.Column("decided_by", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_via", sa.Text(), nullable=True),
        sa.Column("decided_message_id", sa.Uuid(), nullable=True),
        sa.Column("amended_arguments", postgresql.JSONB(), nullable=True),
        sa.Column("replaced_by", sa.Uuid(), nullable=True),
        sa.Column("expired_by", sa.Text(), nullable=True),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.CheckConstraint(
            "approver_kind IN ('self', 'adult')", name=op.f("ck_confirmations_approver_kind_values")
        ),
        sa.CheckConstraint(
            "level IN ('confirm', 'always_confirm')", name=op.f("ck_confirmations_level_values")
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'executing', 'executed', 'refused', 'expired', 'failed', "
            "'replaced')",
            name=op.f("ck_confirmations_status_values"),
        ),
        sa.CheckConstraint(
            "decided_via IN ('button', 'explicit_phrase', 'phrase', 'classifier', 'change')",
            name=op.f("ck_confirmations_decided_via_values"),
        ),
        sa.CheckConstraint(
            "expired_by IN ('domo', 'lifetime')", name=op.f("ck_confirmations_expired_by_values")
        ),
        sa.CheckConstraint(
            "char_length(explicit_reason) <= 200",
            name=op.f("ck_confirmations_explicit_reason_length"),
        ),
        sa.ForeignKeyConstraint(
            ["requester_id"], ["persons.id"], name=op.f("fk_confirmations_requester_id_persons")
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_confirmations_conversation_id_conversations"),
        ),
        sa.ForeignKeyConstraint(
            ["message_id"], ["messages.id"], name=op.f("fk_confirmations_message_id_messages")
        ),
        sa.ForeignKeyConstraint(
            ["decided_by"], ["persons.id"], name=op.f("fk_confirmations_decided_by_persons")
        ),
        sa.ForeignKeyConstraint(
            ["decided_message_id"],
            ["messages.id"],
            name=op.f("fk_confirmations_decided_message_id_messages"),
        ),
        sa.ForeignKeyConstraint(
            ["replaced_by"],
            ["confirmations.id"],
            name=op.f("fk_confirmations_replaced_by_confirmations"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_confirmations")),
    )
    op.create_index(
        "ix_confirmations_requester_id_status", "confirmations", ["requester_id", "status"]
    )

    op.create_table(
        "message_actions",
        _uuid_pk(),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("tool_name", sa.Text(), nullable=False),
        sa.Column("action_type", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("confirmation_id", sa.Uuid(), nullable=True),
        sa.Column("summary", postgresql.JSONB(), nullable=False),
        _created_at(),
        sa.CheckConstraint(
            "status IN ('done', 'waiting', 'refused', 'invalid', 'failed')",
            name=op.f("ck_message_actions_status_values"),
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name=op.f("fk_message_actions_message_id_messages"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["confirmation_id"],
            ["confirmations.id"],
            name=op.f("fk_message_actions_confirmation_id_confirmations"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_message_actions")),
    )

    op.create_table(
        "notes",
        _uuid_pk(),
        sa.Column("author_id", sa.Uuid(), nullable=False),
        sa.Column("recipient_id", sa.Uuid(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        _created_at(),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "char_length(text) BETWEEN 1 AND 1000", name=op.f("ck_notes_text_length")
        ),
        sa.ForeignKeyConstraint(
            ["author_id"], ["persons.id"], name=op.f("fk_notes_author_id_persons")
        ),
        sa.ForeignKeyConstraint(
            ["recipient_id"], ["persons.id"], name=op.f("fk_notes_recipient_id_persons")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notes")),
    )

    op.create_table(
        "webauthn_credentials",
        _uuid_pk(),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("credential_id", sa.LargeBinary(), nullable=False),
        sa.Column("public_key", sa.LargeBinary(), nullable=False),
        sa.Column("sign_count", sa.BigInteger(), nullable=False),
        sa.Column("transports", sa.ARRAY(sa.Text()), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        _created_at(),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f("fk_webauthn_credentials_account_id_accounts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webauthn_credentials")),
        sa.UniqueConstraint("credential_id", name=op.f("uq_webauthn_credentials_credential_id")),
    )

    op.create_table(
        "webauthn_challenges",
        _uuid_pk(),
        sa.Column("purpose", sa.Text(), nullable=False),
        # Not a foreign key: a challenge is short-lived (see the model).
        sa.Column("account_id", sa.Uuid(), nullable=True),
        sa.Column("challenge", sa.LargeBinary(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "purpose IN ('register', 'login')", name=op.f("ck_webauthn_challenges_purpose_values")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webauthn_challenges")),
    )

    op.create_table(
        "policy_settings",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", postgresql.JSONB(), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint("char_length(key) <= 200", name=op.f("ck_policy_settings_key_length")),
        sa.ForeignKeyConstraint(
            ["updated_by"], ["persons.id"], name=op.f("fk_policy_settings_updated_by_persons")
        ),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_policy_settings")),
    )


def downgrade() -> None:
    # Dropping a table drops its indexes; children go before their parents.
    for table in (
        "policy_settings",
        "webauthn_challenges",
        "webauthn_credentials",
        "notes",
        "message_actions",
        "confirmations",
        "invitations",
    ):
        op.drop_table(table)
    op.drop_constraint(op.f("ck_persons_role_values"), "persons", type_="check")
    op.drop_constraint(op.f("fk_persons_invited_by_persons"), "persons", type_="foreignkey")
    op.drop_column("persons", "invited_by")
    op.drop_column("persons", "expires_at")
    op.drop_column("persons", "disabled_at")
