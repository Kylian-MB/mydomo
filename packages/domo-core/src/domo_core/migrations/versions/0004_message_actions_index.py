"""Index the actions of a message.

The chat reads the actions of the replies it shows, and of the ones it sends back to the model,
by message (``ConversationStore.actions_of``); deleting a conversation's messages also looks them
up that way (the foreign key cascades).

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-30
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_message_actions_message_id", "message_actions", ["message_id"])


def downgrade() -> None:
    op.drop_index("ix_message_actions_message_id", table_name="message_actions")
