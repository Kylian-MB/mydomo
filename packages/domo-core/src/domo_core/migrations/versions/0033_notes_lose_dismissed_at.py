"""A note is no longer closed by its recipient.

``notes.dismissed_at`` was when the recipient closed a note, on the zone of notes of the chat page.
That zone is gone (0.7.0, spec 4.8): Domo tells the notes left for someone, and shows their exact
words when asked, so nothing closes a note any more, and the column goes. Going back down adds it
again, empty (Domo is not installed anywhere yet: this is for a development database).

Revision ID: 0033
Revises: 0032
Create Date: 2026-11-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("notes", "dismissed_at")


def downgrade() -> None:
    op.add_column("notes", sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True))
