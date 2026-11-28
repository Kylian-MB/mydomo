"""A candidate voice counts the unlike utterances heard in a row.

``voice_prints.misses``: on a candidate, how many utterances in a row did not resemble it (the
owner, 2026-11-28, "deux phrases de suite": a candidate starts again only after two). Every other
kind of print keeps 0. Going back down drops the column (Domo is not installed anywhere yet: this
is for a development database).

Revision ID: 0070
Revises: 0069
Create Date: 2026-11-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0070"
down_revision: str | None = "0069"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "voice_prints",
        sa.Column("misses", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_voice_prints_misses_not_negative"), "voice_prints", "misses >= 0"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_voice_prints_misses_not_negative"), "voice_prints", type_="check")
    op.drop_column("voice_prints", "misses")
