"""Index the sessions of each account.

Removing someone from the household ends every session of their account at once
(``accounts.sessions.revoke_sessions_of``), and so does the deletion of an account (the foreign
key cascades): both look the sessions up by account.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-30
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_web_sessions_account_id", "web_sessions", ["account_id"])


def downgrade() -> None:
    op.drop_index("ix_web_sessions_account_id", table_name="web_sessions")
