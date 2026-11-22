"""Index the request each confirmation replaced.

Every card of a request, on the chat page and in the stream, looks for the request it replaced
(``confirmations.store.predecessors_of``: a change Domo applied made it a new request), to say
what it was before the change.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-30
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_confirmations_replaced_by", "confirmations", ["replaced_by"])


def downgrade() -> None:
    op.drop_index("ix_confirmations_replaced_by", table_name="confirmations")
