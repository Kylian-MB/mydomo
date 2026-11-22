"""Notes: when the recipient's page first showed one, and when its author took it back.

``seen_at`` is set the first time the recipient's page shows the note, so that its author can be
told whether a note they want to withdraw was already shown. ``withdrawn_at`` is set when the
author takes the note back: it then disappears from the recipient's page. Both are empty until
that happens (a note that exists already keeps them empty).

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("notes", sa.Column("seen_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("notes", sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("notes", "withdrawn_at")
    op.drop_column("notes", "seen_at")
