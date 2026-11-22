"""The name each person gives Domo.

``persons`` gains ``calls_domo``: the name a person calls Domo by ("Brenda"), 1 to 40
characters, or nothing when they call him Domo (every person already there). Going back down
drops it (Domo is not installed anywhere yet: this is for a development database).

Revision ID: 0046
Revises: 0045
Create Date: 2026-11-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0046"
down_revision: str | None = "0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("persons", sa.Column("calls_domo", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_persons_calls_domo_length"),
        "persons",
        "calls_domo IS NULL OR char_length(calls_domo) BETWEEN 1 AND 40",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_persons_calls_domo_length"), "persons", type_="check")
    op.drop_column("persons", "calls_domo")
