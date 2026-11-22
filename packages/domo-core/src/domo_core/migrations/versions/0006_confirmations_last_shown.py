"""Remember the reply that last showed a request.

A person's words answer a request only when it was shown in the reply just before them (the
reply that made it, or that presented it again): an unrelated "yes" later in the conversation
must not decide a request shown long ago (controller ruling R74).

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("confirmations", sa.Column("last_shown_message_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_confirmations_last_shown_message_id_messages"),
        "confirmations",
        "messages",
        ["last_shown_message_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_confirmations_last_shown_message_id_messages"), "confirmations", type_="foreignkey"
    )
    op.drop_column("confirmations", "last_shown_message_id")
