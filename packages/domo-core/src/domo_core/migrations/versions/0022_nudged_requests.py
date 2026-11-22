"""The one who asked nudges the approver of their request (spec 5.2).

* ``confirmations.nudged_at``: when the one who asked last asked Domo to remind the approver of
  their request that waits for someone else's yes (an adult's for a child's request, the owner's
  when a right says so): the request then comes first in the approver's zone "To approve", and
  their Domo is told.

Going back down drops it.

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "confirmations", sa.Column("nudged_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("confirmations", "nudged_at")
