"""The owner's list of successors: who takes over the owner's role, in order.

* new table ``successors``: one place on the list. ``rank`` (from 1) is used once; a place is
  held by a person of the household (``person_id``, who is on the list once) or by someone outside
  it (``name``, with ``relation``): exactly one of the two. ``access_way`` is how the door is
  opened the day it comes (a personal access, or a paper code kept with the will), and
  ``deceased_at`` when the owner said the successor died. There is one list, the household's.

Going back down drops the table, and the list with it.

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Same server-side defaults as 0001.
NEW_UUID = sa.text("gen_random_uuid()")
NOW = sa.text("clock_timestamp()")


def upgrade() -> None:
    op.create_table(
        "successors",
        sa.Column("id", sa.Uuid(), server_default=NEW_UUID, nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("person_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("relation", sa.Text(), nullable=True),
        sa.Column("access_way", sa.Text(), nullable=True),
        sa.Column("deceased_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint(
            "(person_id IS NULL) <> (name IS NULL)", name=op.f("ck_successors_one_who")
        ),
        sa.CheckConstraint(
            "char_length(name) BETWEEN 1 AND 60", name=op.f("ck_successors_name_length")
        ),
        sa.CheckConstraint(
            "char_length(relation) BETWEEN 1 AND 200", name=op.f("ck_successors_relation_length")
        ),
        sa.CheckConstraint(
            "access_way IN ('personal_access', 'paper_code')",
            name=op.f("ck_successors_access_way_values"),
        ),
        sa.CheckConstraint("rank >= 1", name=op.f("ck_successors_rank_from_one")),
        sa.ForeignKeyConstraint(
            ["person_id"], ["persons.id"], name=op.f("fk_successors_person_id_persons")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_successors")),
        sa.UniqueConstraint("rank", name=op.f("uq_successors_rank")),
        sa.UniqueConstraint("person_id", name=op.f("uq_successors_person_id")),
    )


def downgrade() -> None:
    op.drop_table("successors")
