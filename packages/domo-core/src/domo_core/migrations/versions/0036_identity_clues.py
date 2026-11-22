"""The clues Domo keeps about who speaks.

``identity_clues`` keeps, for a person, a clue that it is them (a proof that succeeded, for now):
its strength and its source, the channel and the device it was given on, the kind of proof, when it
was made and when it stops counting. Going back down drops the table (Domo is not installed
anywhere yet: this is for a development database).

Revision ID: 0036
Revises: 0035
Create Date: 2026-11-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_UUID = sa.text("gen_random_uuid()")


def upgrade() -> None:
    op.create_table(
        "identity_clues",
        sa.Column("id", sa.Uuid(), server_default=NEW_UUID, nullable=False),
        sa.Column("person_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("weighs", sa.Text(), nullable=False),
        sa.Column("strength", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("device_id", sa.Uuid(), nullable=True),
        sa.Column("means", sa.Text(), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "weighs IN ('for', 'against')", name=op.f("ck_identity_clues_weighs_values")
        ),
        sa.CheckConstraint(
            "strength IN ('strong', 'weak')", name=op.f("ck_identity_clues_strength_values")
        ),
        sa.CheckConstraint(
            "source IN ('code', 'domo')", name=op.f("ck_identity_clues_source_values")
        ),
        sa.ForeignKeyConstraint(
            ["person_id"],
            ["persons.id"],
            name=op.f("fk_identity_clues_person_id_persons"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_identity_clues")),
    )
    op.create_index(
        "ix_identity_clues_person_id_channel", "identity_clues", ["person_id", "channel"]
    )


def downgrade() -> None:
    op.drop_index("ix_identity_clues_person_id_channel", table_name="identity_clues")
    op.drop_table("identity_clues")
