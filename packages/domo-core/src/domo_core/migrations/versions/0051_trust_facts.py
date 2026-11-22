"""What a person's word has been worth.

``trust_facts`` keeps, for each person (``person_id``, ``ON DELETE CASCADE``), the facts Domo
noted about what their word turned out to be worth: the outcome (``held``, ``mistaken``,
``lied``), how it is known (``confirmed``, ``seen``, ``reported``), a sentence of his (1 to 200
characters), the memory it comes from, in whose turn it was noted and who reported it (all three
``ON DELETE SET NULL``; a reporter only on a reported fact) and when. Indexed by person and date.
Going back down drops it (Domo is not installed anywhere yet: this is for a development
database).

Revision ID: 0051
Revises: 0050
Create Date: 2026-11-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0051"
down_revision: str | None = "0050"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_UUID = sa.text("gen_random_uuid()")


def upgrade() -> None:
    op.create_table(
        "trust_facts",
        sa.Column("id", sa.Uuid(), server_default=NEW_UUID, nullable=False),
        sa.Column("person_id", sa.Uuid(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("established", sa.Text(), nullable=False),
        sa.Column("about", sa.Text(), nullable=False),
        sa.Column("memory_id", sa.Uuid(), nullable=True),
        sa.Column("noted_for_id", sa.Uuid(), nullable=True),
        sa.Column("reported_by_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "outcome IN ('held', 'mistaken', 'lied')", name=op.f("ck_trust_facts_outcome_values")
        ),
        sa.CheckConstraint(
            "established IN ('confirmed', 'seen', 'reported')",
            name=op.f("ck_trust_facts_established_values"),
        ),
        sa.CheckConstraint(
            "char_length(about) BETWEEN 1 AND 200", name=op.f("ck_trust_facts_about_length")
        ),
        sa.CheckConstraint(
            "reported_by_id IS NULL OR established = 'reported'",
            name=op.f("ck_trust_facts_reporter_needs_reported"),
        ),
        sa.ForeignKeyConstraint(
            ["person_id"],
            ["persons.id"],
            name=op.f("fk_trust_facts_person_id_persons"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["memory_id"],
            ["memories.id"],
            name=op.f("fk_trust_facts_memory_id_memories"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["noted_for_id"],
            ["persons.id"],
            name=op.f("fk_trust_facts_noted_for_id_persons"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["reported_by_id"],
            ["persons.id"],
            name=op.f("fk_trust_facts_reported_by_id_persons"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_trust_facts")),
    )
    op.create_index(
        "ix_trust_facts_person_id_created_at", "trust_facts", ["person_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_trust_facts_person_id_created_at", table_name="trust_facts")
    op.drop_table("trust_facts")
