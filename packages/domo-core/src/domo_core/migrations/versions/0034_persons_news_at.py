"""A person keeps when something new last came for them.

``persons.news_at`` is when something new last came for a person that no turn of Domo said yet
(a note left for them, a request of theirs someone else decided, a newcomer on a key they gave):
their open page gives Domo a turn at once, and their next page one even within a quarter of an
hour (0.7.1, ``conversation.visits.news_for``). Going back down drops it (Domo is not
installed anywhere yet: this is for a development database).

Revision ID: 0034
Revises: 0033
Create Date: 2026-11-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("persons", sa.Column("news_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("persons", "news_at")
