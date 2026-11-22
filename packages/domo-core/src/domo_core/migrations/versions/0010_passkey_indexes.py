"""Index what the passkeys look up.

The account page lists the passkeys of an account, and so do the options of a new registration
(the passkeys already there are left out): ``webauthn_credentials`` by account (R55). An answer
of the browser is matched to the challenge it signed by the challenge itself:
``webauthn_challenges`` by challenge.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-30
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_webauthn_credentials_account_id", "webauthn_credentials", ["account_id"])
    op.create_index("ix_webauthn_challenges_challenge", "webauthn_challenges", ["challenge"])


def downgrade() -> None:
    op.drop_index("ix_webauthn_challenges_challenge", table_name="webauthn_challenges")
    op.drop_index("ix_webauthn_credentials_account_id", table_name="webauthn_credentials")
