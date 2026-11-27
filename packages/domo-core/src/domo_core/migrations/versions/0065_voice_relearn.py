"""A person's voice may be marked "to be learnt again".

``voice_refs`` gains ``relearn_at`` (empty: nothing to learn again): set when the assistant erased
what was learnt of the person's voice on his own idea, cleared when it is learnt again (Task 27,
ruling R). Going back down drops it (Domo is not installed anywhere yet: this is for a development
database).

Revision ID: 0065
Revises: 0064
Create Date: 2026-11-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0065"
down_revision: str | None = "0064"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("voice_refs", sa.Column("relearn_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("voice_refs", "relearn_at")
