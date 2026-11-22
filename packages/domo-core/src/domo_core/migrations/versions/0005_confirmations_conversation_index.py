"""Index the confirmations of a conversation.

Every turn of the chat reads the requests that wait in the conversation for the person who
speaks (``confirmations.store.pending_for``), to show them to Domo and to read the person's
answer.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-30
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_confirmations_conversation_id", "confirmations", ["conversation_id"])


def downgrade() -> None:
    op.drop_index("ix_confirmations_conversation_id", table_name="confirmations")
