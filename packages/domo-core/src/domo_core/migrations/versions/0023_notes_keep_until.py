"""The author of notes to a guest keeps them until a date (spec 5.10).

* ``notes.keep_until``: the end of the day (UTC) until which the author keeps a note that was left
  for a guest, though the guest leaves: the upkeep erases it then, if the guest is still away. Empty
  for every note that was there before, and for any note the author did not ask to keep: such a note
  is erased when its guest leaves, as before.

Going back down drops it (the notes kept so far are then erased with their guest, as they were).

Revision ID: 0023
Revises: 0022
Create Date: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("notes", sa.Column("keep_until", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("notes", "keep_until")
