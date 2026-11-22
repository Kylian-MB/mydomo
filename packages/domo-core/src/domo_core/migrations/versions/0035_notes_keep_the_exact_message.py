"""A note keeps the exact words of its author.

``notes.exact_text`` is the passage of the message the author wrote to Domo in the turn the note
was left that is the note, as they typed it, checked by the code, 1 to 1000 characters like the
text (``None`` when there is none): the card of the exact words shows it, where it showed what
Domo noted (0.7.1). The notes there are
keep none. Going back down drops it (Domo is not installed anywhere yet: this is for a
development database).

Revision ID: 0035
Revises: 0034
Create Date: 2026-11-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("notes", sa.Column("exact_text", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_notes_exact_text_length"),
        "notes",
        "char_length(exact_text) BETWEEN 1 AND 1000",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_notes_exact_text_length"), "notes", type_="check")
    op.drop_column("notes", "exact_text")
