"""A reply says whether it is Domo's own answer to the person.

``messages.own_answer`` is true for a reply that answers a message of the person and was written
in a turn that was given nothing from outside (no note someone left them): the only reply the
yes-judge may read, as what a "yes" of the person answers. Every message kept before is false: the
code cannot tell it of them, so the judge reads none of them. Going back down drops the column
(Domo is not installed anywhere yet: this is for a development database).

Revision ID: 0039
Revises: 0038
Create Date: 2026-11-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column("own_answer", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("messages", "own_answer")
