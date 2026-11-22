"""When the person a memory is about confirmed what someone else said of them.

``memories.confirmed_at`` (2026-11-16, the owner's trial of 0.9.0): a memory about a person of the
household that someone else said is their word, not a fact, until that person confirms it; the
column says when they did (``NULL``: not yet, or a memory that needs no confirmation). Going back
down drops it (Domo is not installed anywhere yet: this is for a development database).

Revision ID: 0049
Revises: 0048
Create Date: 2026-11-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0049"
down_revision: str | None = "0048"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("memories", sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("memories", "confirmed_at")
