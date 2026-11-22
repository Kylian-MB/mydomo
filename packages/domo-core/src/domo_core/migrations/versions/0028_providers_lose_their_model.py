"""A key no longer has one model, and always says who gave it.

Each call now chooses its model among the ones a key lists (``provider_models``): ``providers``
loses ``model``, whose old value is forgotten. ``given_by`` becomes required: a key that says
nobody goes to the owner, and one with no owner to go to is deleted (Domo is not installed
anywhere yet: this is for a development database). Going back down gives ``model`` back, empty,
and makes ``given_by`` optional again.

Revision ID: 0028
Revises: 0027
Create Date: 2026-11-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Plain SQL, so that the migration still renders offline.
    op.execute(
        "UPDATE providers SET given_by = (SELECT id FROM persons WHERE role = 'owner') "
        "WHERE given_by IS NULL"
    )
    op.execute("DELETE FROM providers WHERE given_by IS NULL")
    op.alter_column("providers", "given_by", existing_type=sa.Uuid(), nullable=False)
    op.drop_column("providers", "model")


def downgrade() -> None:
    op.add_column(
        "providers",
        sa.Column("model", sa.Text(), server_default=sa.text("''"), nullable=False),
    )
    op.alter_column("providers", "model", existing_type=sa.Text(), server_default=None)
    op.alter_column("providers", "given_by", existing_type=sa.Uuid(), nullable=True)
