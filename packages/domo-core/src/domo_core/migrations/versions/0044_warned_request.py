"""Whether a request was asked because the judge warned the person of something.

``confirmations`` gains ``warned``: the yes-judge answered ``warn`` (a yes asked only because the
person must know something first, its reason) and that reason is what the request asks with. False
for every request already there. Going back down drops it (Domo is not installed anywhere yet:
this is for a development database).

Revision ID: 0044
Revises: 0043
Create Date: 2026-11-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0044"
down_revision: str | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "confirmations",
        sa.Column("warned", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("confirmations", "warned")
