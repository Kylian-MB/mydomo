"""A kept clue says which microphone heard it and, when measured, its degree.

``identity_clues`` gains ``microphone`` (empty when no microphone heard it: every clue before
this one) and ``degree`` (a number from 0 to 1, empty when not measured).
Going back down drops them (Domo is not installed anywhere yet: this is for a development
database).

Revision ID: 0064
Revises: 0063
Create Date: 2026-11-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0064"
down_revision: str | None = "0063"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("identity_clues", sa.Column("microphone", sa.Text(), nullable=True))
    op.add_column("identity_clues", sa.Column("degree", sa.REAL(), nullable=True))
    op.create_check_constraint(
        op.f("ck_identity_clues_degree_range"),
        "identity_clues",
        "degree IS NULL OR (degree >= 0 AND degree <= 1)",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_identity_clues_degree_range"), "identity_clues", type_="check")
    op.drop_column("identity_clues", "degree")
    op.drop_column("identity_clues", "microphone")
