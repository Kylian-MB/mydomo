"""Keys say who gave them and who uses them; the tables of spending and of Domo's choices.

A key (``providers``) gains the person who gave it (``given_by``, null for now: migration 0028
makes it required and drops ``model``), the mark of the one general key, the reference of its admin
key and the factor between the estimated and the real cost. The new tables hold the models a key
lists, who may use it, the levels the owner gave, Domo's choices and lessons, the calls and what
they cost, the limits, the real spending, the exchange rates and the catalog snapshots. An
invitation says whether the invitee uses the inviter's key.

Every existing key goes to the owner and the oldest becomes the general key; a key with no owner
to go to is deleted (Domo is not installed anywhere yet: this is for a development database).
Going back down removes the columns and the tables.

Revision ID: 0027
Revises: 0026
Create Date: 2026-11-08
"""

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Same server-side defaults as 0001.
NEW_UUID = sa.text("gen_random_uuid()")
NOW = sa.text("clock_timestamp()")
ZERO = sa.text("0")
FALSE = sa.text("false")

_TASKS = "('conversation', 'judge', 'intent', 'rights_writer', 'key_check', 'upkeep')"


def _now(name: str) -> sa.Column[datetime]:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=NOW, nullable=False)


def _provider_fk(table: str, ondelete: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["provider_id"],
        ["providers.id"],
        name=op.f(f"fk_{table}_provider_id_providers"),
        ondelete=ondelete,
    )


def _person_fk(table: str, column: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        [column], ["persons.id"], name=op.f(f"fk_{table}_{column}_persons")
    )


def upgrade() -> None:
    op.add_column("providers", sa.Column("given_by", sa.Uuid(), nullable=True))
    op.add_column(
        "providers", sa.Column("is_general", sa.Boolean(), server_default=FALSE, nullable=False)
    )
    op.add_column("providers", sa.Column("admin_secret_ref", sa.Text(), nullable=True))
    op.add_column("providers", sa.Column("spend_factor", sa.Numeric(10, 4), nullable=True))
    op.create_foreign_key(
        op.f("fk_providers_given_by_persons"), "providers", "persons", ["given_by"], ["id"]
    )
    op.create_index(
        "uq_providers_one_general",
        "providers",
        ["is_general"],
        unique=True,
        postgresql_where=sa.text("is_general"),
    )
    # Every key goes to the owner; the oldest becomes the general one; a key with no owner is
    # deleted. Plain SQL, so that the migration still renders offline.
    op.execute("UPDATE providers SET given_by = (SELECT id FROM persons WHERE role = 'owner')")
    op.execute("DELETE FROM providers WHERE given_by IS NULL")
    op.execute(
        "UPDATE providers SET is_general = true WHERE id = "
        "(SELECT id FROM providers ORDER BY created_at, id LIMIT 1)"
    )

    op.add_column("invitations", sa.Column("uses_inviter_key", sa.Boolean(), nullable=True))

    op.create_table(
        "provider_models",
        sa.Column("provider_id", sa.Text(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False),
        _now("listed_at"),
        sa.CheckConstraint(
            "char_length(model_id) <= 200", name=op.f("ck_provider_models_model_id_length")
        ),
        _provider_fk("provider_models", "CASCADE"),
        sa.PrimaryKeyConstraint("provider_id", "model_id", name=op.f("pk_provider_models")),
    )

    op.create_table(
        "provider_users",
        sa.Column("id", sa.Uuid(), server_default=NEW_UUID, nullable=False),
        sa.Column("provider_id", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("person_id", sa.Uuid(), nullable=True),
        sa.Column("allowed", sa.Boolean(), nullable=False),
        sa.Column("set_by", sa.Uuid(), nullable=False),
        _now("set_at"),
        sa.CheckConstraint(
            "scope IN ('household', 'invitees', 'person')",
            name=op.f("ck_provider_users_scope_values"),
        ),
        sa.CheckConstraint(
            "(scope = 'person') = (person_id IS NOT NULL)",
            name=op.f("ck_provider_users_person_matches_scope"),
        ),
        _provider_fk("provider_users", "CASCADE"),
        _person_fk("provider_users", "person_id"),
        _person_fk("provider_users", "set_by"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_provider_users")),
        sa.UniqueConstraint(
            "provider_id",
            "scope",
            "person_id",
            name=op.f("uq_provider_users_provider_scope_person"),
            postgresql_nulls_not_distinct=True,
        ),
    )

    op.create_table(
        "model_levels",
        sa.Column("provider_id", sa.Text(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column("level", sa.Text(), nullable=False),
        sa.Column("set_by", sa.Uuid(), nullable=False),
        _now("set_at"),
        sa.CheckConstraint(
            "level IN ('small', 'medium', 'strong')", name=op.f("ck_model_levels_level_values")
        ),
        sa.CheckConstraint(
            "char_length(model_id) <= 200", name=op.f("ck_model_levels_model_id_length")
        ),
        _provider_fk("model_levels", "CASCADE"),
        _person_fk("model_levels", "set_by"),
        sa.PrimaryKeyConstraint("provider_id", "model_id", name=op.f("pk_model_levels")),
    )

    op.create_table(
        "model_choices",
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("choices", postgresql.JSONB(), nullable=False),
        sa.Column("changed_by", sa.Uuid(), nullable=True),
        _now("changed_at"),
        sa.Column("why", sa.Text(), nullable=True),
        sa.CheckConstraint("char_length(why) <= 200", name=op.f("ck_model_choices_why_length")),
        _person_fk("model_choices", "changed_by"),
        sa.PrimaryKeyConstraint("scope", name=op.f("pk_model_choices")),
    )

    op.create_table(
        "model_lessons",
        sa.Column("provider_kind", sa.Text(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column("task", sa.Text(), nullable=False),
        sa.Column("failures", sa.Integer(), server_default=ZERO, nullable=False),
        _now("last_failure_at"),
        sa.Column("last_reason", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "char_length(last_reason) <= 200", name=op.f("ck_model_lessons_last_reason_length")
        ),
        sa.PrimaryKeyConstraint("provider_kind", "model_id", "task", name=op.f("pk_model_lessons")),
    )

    op.create_table(
        "model_calls",
        sa.Column("id", sa.Uuid(), server_default=NEW_UUID, nullable=False),
        sa.Column("provider_id", sa.Text(), nullable=True),
        sa.Column("person_id", sa.Uuid(), nullable=True),
        sa.Column("task", sa.Text(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("reserved_usd", sa.Numeric(14, 6), nullable=False),
        sa.Column("cost_usd", sa.Numeric(14, 6), nullable=True),
        sa.Column("input_tokens", sa.Integer(), server_default=ZERO, nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default=ZERO, nullable=False),
        sa.Column("cache_read_tokens", sa.Integer(), server_default=ZERO, nullable=False),
        sa.Column("cache_write_tokens", sa.Integer(), server_default=ZERO, nullable=False),
        sa.Column("estimated", sa.Boolean(), server_default=FALSE, nullable=False),
        sa.Column("price_unknown", sa.Boolean(), server_default=FALSE, nullable=False),
        _now("started_at"),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(f"task IN {_TASKS}", name=op.f("ck_model_calls_task_values")),
        sa.CheckConstraint(
            "status IN ('reserved', 'settled')", name=op.f("ck_model_calls_status_values")
        ),
        _provider_fk("model_calls", "SET NULL"),
        _person_fk("model_calls", "person_id"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_model_calls")),
    )
    op.create_index(
        "ix_model_calls_provider_id_started_at", "model_calls", ["provider_id", "started_at"]
    )

    op.create_table(
        "spending_limits",
        sa.Column("id", sa.Uuid(), server_default=NEW_UUID, nullable=False),
        sa.Column("provider_id", sa.Text(), nullable=False),
        sa.Column("person_id", sa.Uuid(), nullable=True),
        sa.Column("amount", sa.Numeric(12, 4), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("period", sa.Text(), nullable=False),
        sa.Column("warn_at_percent", sa.Integer(), nullable=True),
        sa.Column("set_by", sa.Uuid(), nullable=False),
        _now("set_at"),
        sa.Column("warned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("domo_warned_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("amount > 0", name=op.f("ck_spending_limits_amount_positive")),
        sa.CheckConstraint(
            "currency IN ('USD', 'EUR')", name=op.f("ck_spending_limits_currency_values")
        ),
        sa.CheckConstraint(
            "period IN ('day', 'week', 'month')", name=op.f("ck_spending_limits_period_values")
        ),
        sa.CheckConstraint(
            "warn_at_percent BETWEEN 1 AND 100",
            name=op.f("ck_spending_limits_warn_at_percent_range"),
        ),
        _provider_fk("spending_limits", "CASCADE"),
        _person_fk("spending_limits", "person_id"),
        _person_fk("spending_limits", "set_by"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_spending_limits")),
        sa.UniqueConstraint(
            "provider_id",
            "person_id",
            name=op.f("uq_spending_limits_provider_person"),
            postgresql_nulls_not_distinct=True,
        ),
    )

    op.create_table(
        "real_spend",
        sa.Column("provider_id", sa.Text(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("usd", sa.Numeric(14, 6), nullable=False),
        _now("fetched_at"),
        _provider_fk("real_spend", "CASCADE"),
        sa.PrimaryKeyConstraint("provider_id", "day", name=op.f("pk_real_spend")),
    )

    op.create_table(
        "exchange_rates",
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("usd_per_unit", sa.Numeric(12, 6), nullable=False),
        _now("fetched_at"),
        sa.CheckConstraint("currency IN ('EUR')", name=op.f("ck_exchange_rates_currency_values")),
        sa.CheckConstraint(
            "usd_per_unit > 0", name=op.f("ck_exchange_rates_usd_per_unit_positive")
        ),
        sa.PrimaryKeyConstraint("currency", name=op.f("pk_exchange_rates")),
    )

    op.create_table(
        "catalog_snapshots",
        sa.Column("id", sa.Uuid(), server_default=NEW_UUID, nullable=False),
        _now("fetched_at"),
        sa.Column("data", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_catalog_snapshots")),
    )


def downgrade() -> None:
    for table in (
        "catalog_snapshots",
        "exchange_rates",
        "real_spend",
        "spending_limits",
        "model_calls",
        "model_lessons",
        "model_choices",
        "model_levels",
        "provider_users",
        "provider_models",
    ):
        op.drop_table(table)
    op.drop_column("invitations", "uses_inviter_key")
    op.drop_index("uq_providers_one_general", table_name="providers")
    op.drop_constraint(op.f("fk_providers_given_by_persons"), "providers", type_="foreignkey")
    for column in ("spend_factor", "admin_secret_ref", "is_general", "given_by"):
        op.drop_column("providers", column)
