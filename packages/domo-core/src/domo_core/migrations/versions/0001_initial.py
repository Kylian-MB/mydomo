"""Initial schema of iteration 1a.

People, accounts, sessions, setup state, providers, conversations, messages and the audit log,
plus the pgvector extension.

Written by hand, constraint names included (``op.f`` marks a name as final, so the naming
convention of the models does not rewrite it). ``tests/test_migrations.py`` checks that the
models and this migration describe the same schema.

Revision ID: 0001
Revises:
Create Date: 2026-10-28
"""

import uuid
from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Server-side defaults: identifiers from gen_random_uuid(), creation times from clock_timestamp()
# (not now(): rows created one after the other in a transaction must keep their order).
NEW_UUID = sa.text("gen_random_uuid()")
NOW = sa.text("clock_timestamp()")


def _uuid_pk() -> sa.Column[uuid.UUID]:
    return sa.Column("id", sa.Uuid(), server_default=NEW_UUID, nullable=False)


def _created_at() -> sa.Column[datetime]:
    return sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False)


def upgrade() -> None:
    # Used by the memory of iteration 1c; created now so that the database image decides once.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "persons",
        _uuid_pk(),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        _created_at(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_persons")),
    )
    op.create_index(
        "uq_persons_single_owner",
        "persons",
        ["role"],
        unique=True,
        postgresql_where=sa.text("role = 'owner'"),
    )

    op.create_table(
        "accounts",
        _uuid_pk(),
        sa.Column("person_id", sa.Uuid(), nullable=False),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        _created_at(),
        sa.CheckConstraint(
            "username = lower(username)", name=op.f("ck_accounts_username_lowercase")
        ),
        sa.ForeignKeyConstraint(
            ["person_id"], ["persons.id"], name=op.f("fk_accounts_person_id_persons")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_accounts")),
        sa.UniqueConstraint("person_id", name=op.f("uq_accounts_person_id")),
        sa.UniqueConstraint("username", name=op.f("uq_accounts_username")),
    )

    op.create_table(
        "web_sessions",
        _uuid_pk(),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        _created_at(),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f("fk_web_sessions_account_id_accounts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_web_sessions")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_web_sessions_token_hash")),
    )

    op.create_table(
        "setup_state",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("setup_code_hash", sa.Text(), nullable=True),
        sa.Column("code_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("owner_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("model_configured_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("id = 1", name=op.f("ck_setup_state_singleton")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_setup_state")),
    )
    # The single row: code that reads or updates the state can rely on it being there.
    op.execute("INSERT INTO setup_state (id) VALUES (1)")

    op.create_table(
        "providers",
        # No default: the application generates the id ("p-" and 8 hexadecimal characters).
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("secret_ref", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=False),
        _created_at(),
        sa.CheckConstraint("id ~ '^p-[0-9a-f]{8}$'", name=op.f("ck_providers_id_format")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_providers")),
    )

    op.create_table(
        "conversations",
        _uuid_pk(),
        sa.Column("person_id", sa.Uuid(), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["person_id"], ["persons.id"], name=op.f("fk_conversations_person_id_persons")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversations")),
    )
    op.create_index(
        "ix_conversations_person_id_created_at", "conversations", ["person_id", "created_at"]
    )

    op.create_table(
        "messages",
        _uuid_pk(),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("provenance", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        _created_at(),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint("role IN ('user', 'assistant')", name=op.f("ck_messages_role_values")),
        sa.CheckConstraint(
            "status IN ('streaming', 'complete', 'partial', 'error')",
            name=op.f("ck_messages_status_values"),
        ),
        sa.CheckConstraint(
            "provenance IN ('person', 'model')", name=op.f("ck_messages_provenance_values")
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_messages_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
    )
    op.create_index(
        "ix_messages_conversation_id_created_at", "messages", ["conversation_id", "created_at"]
    )

    op.create_table(
        "audit_log",
        # A bigserial: the order of the hash chain.
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("details", postgresql.JSONB(), nullable=False),
        sa.Column("prev_hash", sa.CHAR(64), nullable=False),
        sa.Column("hash", sa.CHAR(64), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
        sa.UniqueConstraint("hash", name=op.f("uq_audit_log_hash")),
    )


def downgrade() -> None:
    # Dropping a table drops its indexes; children go before their parents.
    for table in (
        "audit_log",
        "messages",
        "conversations",
        "providers",
        "setup_state",
        "web_sessions",
        "accounts",
        "persons",
    ):
        op.drop_table(table)
    op.execute("DROP EXTENSION IF EXISTS vector")
