"""A person's message says whether the code was sure it was theirs.

``messages`` gains ``heard_unsure`` (empty: sure, as every message before it): set when a person's
message is stored, true when the voice that said it was not sure (final fix wave, I1). Going back
down drops it (Domo is not installed anywhere yet: this is for a development database).

Revision ID: 0066
Revises: 0065
Create Date: 2026-11-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0066"
down_revision: str | None = "0065"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("heard_unsure", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "heard_unsure")
