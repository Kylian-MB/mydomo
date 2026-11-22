"""An invitation may wait for its link: ``invitations.token_hash`` may be empty.

When someone other than the one who asked says yes to an invitation (the owner, to Alex's), the
link is for the one who asked (spec 5.10): the yes makes the invitation without a token, and the
one who asked makes the token, and sees the link, once, with a button on their card. Until then
the hash is empty. The column stays unique (PostgreSQL lets any number of invitations wait with an
empty hash); every hash that is there is still its own.

Going back down gives every invitation a hash again, as the column needs one. An invitation that
never had a token cannot be used by anyone (the code of before has no way to make one), so those
that still wait are cancelled, and every one that has no hash gets one that is no link's (the
SHA-256 of a random identifier that is kept nowhere).

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("invitations", "token_hash", existing_type=sa.Text(), nullable=True)


def downgrade() -> None:
    op.execute(
        "UPDATE invitations SET cancelled_at = clock_timestamp() "
        "WHERE token_hash IS NULL AND accepted_at IS NULL AND cancelled_at IS NULL"
    )
    op.execute(
        "UPDATE invitations "
        "SET token_hash = encode(sha256(convert_to(gen_random_uuid()::text, 'UTF8')), 'hex') "
        "WHERE token_hash IS NULL"
    )
    op.alter_column("invitations", "token_hash", existing_type=sa.Text(), nullable=False)
