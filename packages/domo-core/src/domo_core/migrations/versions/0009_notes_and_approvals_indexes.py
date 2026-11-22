"""Index what the zones at the top of the chat page ask for.

The chat page asks every 15 seconds for the notes left for the person (``notes`` by recipient)
and, for an adult or the owner, for the requests that wait for an adult's yes (``confirmations``
by status and approver).

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-30
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_notes_recipient_id", "notes", ["recipient_id"])
    op.create_index(
        "ix_confirmations_status_approver_kind", "confirmations", ["status", "approver_kind"]
    )


def downgrade() -> None:
    op.drop_index("ix_confirmations_status_approver_kind", table_name="confirmations")
    op.drop_index("ix_notes_recipient_id", table_name="notes")
