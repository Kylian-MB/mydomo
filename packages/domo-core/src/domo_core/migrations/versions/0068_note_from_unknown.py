"""A note of the assistant's own says whether it was born of words of an unknown source.

``notes`` gains ``from_unknown`` (false for every note before it): true for a note of his own
left from words nobody of the house is known for (a voice nobody knows, a program's, a site's or
a tool's text); only such a note can never, alone, cause what the voice tools do (final fix wave,
B2). A person's note never is. Going back down drops it (Domo is not installed anywhere yet: this
is for a development database).

Revision ID: 0068
Revises: 0067
Create Date: 2026-11-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0068"
down_revision: str | None = "0067"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHECK = "unknown_is_his"


def upgrade() -> None:
    op.add_column(
        "notes",
        sa.Column("from_unknown", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.create_check_constraint(op.f(f"ck_notes_{_CHECK}"), "notes", "NOT from_unknown OR by_domo")


def downgrade() -> None:
    op.drop_constraint(op.f(f"ck_notes_{_CHECK}"), "notes", type_="check")
    op.drop_column("notes", "from_unknown")
