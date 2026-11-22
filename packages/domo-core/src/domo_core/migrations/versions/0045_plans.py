"""The plans Domo sets for himself.

``plans`` keeps, for each plan: whose it is (``ON DELETE CASCADE``), why Domo set it and the
exact words of the person it comes from, when it is due (a time, a repeating rule, the person's
return), its state, whether Domo set it without being asked, when it was last due and what came
of it, and its dates. A waiting plan always has a time or a return to wait for. Going back down
drops it all (Domo is not installed anywhere yet: this is for a development database).

Revision ID: 0045
Revises: 0044
Create Date: 2026-11-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0045"
down_revision: str | None = "0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_UUID = sa.text("gen_random_uuid()")
NOW = sa.text("clock_timestamp()")


def upgrade() -> None:
    op.create_table(
        "plans",
        sa.Column("id", sa.Uuid(), server_default=NEW_UUID, nullable=False),
        sa.Column("person_id", sa.Uuid(), nullable=False),
        sa.Column("why", sa.Text(), nullable=False),
        sa.Column("their_words", sa.Text(), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("repeat", sa.Text(), nullable=True),
        sa.Column("on_return", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("state", sa.Text(), server_default=sa.text("'waiting'"), nullable=False),
        sa.Column("made_by_domo", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("last_fired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_outcome", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint("char_length(why) BETWEEN 1 AND 500", name=op.f("ck_plans_why_length")),
        sa.CheckConstraint(
            "their_words IS NULL OR char_length(their_words) BETWEEN 1 AND 1000",
            name=op.f("ck_plans_their_words_length"),
        ),
        sa.CheckConstraint(
            "repeat IS NULL OR char_length(repeat) BETWEEN 1 AND 200",
            name=op.f("ck_plans_repeat_length"),
        ),
        sa.CheckConstraint(
            "last_outcome IS NULL OR char_length(last_outcome) BETWEEN 1 AND 300",
            name=op.f("ck_plans_last_outcome_length"),
        ),
        sa.CheckConstraint(
            "state IN ('waiting', 'done', 'cancelled')", name=op.f("ck_plans_state_values")
        ),
        sa.CheckConstraint(
            "state <> 'waiting' OR due_at IS NOT NULL OR on_return",
            name=op.f("ck_plans_needs_a_moment"),
        ),
        sa.ForeignKeyConstraint(
            ["person_id"],
            ["persons.id"],
            name=op.f("fk_plans_person_id_persons"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plans")),
    )
    op.create_index("ix_plans_person_id", "plans", ["person_id"])
    op.create_index(
        "ix_plans_due_at", "plans", ["due_at"], postgresql_where=sa.text("state = 'waiting'")
    )


def downgrade() -> None:
    op.drop_index("ix_plans_due_at", table_name="plans")
    op.drop_index("ix_plans_person_id", table_name="plans")
    op.drop_table("plans")
