"""Chains of actions: a request may carry several steps, done in order after one yes.

* new table ``confirmation_steps``: one step of a chain, at its ``position`` (from 0) in the
  request it belongs to (the two are its key): the call (``tool_name``, ``arguments``), its
  ``action_type``, the ``action`` its tool described when the chain was asked for (empty for a
  step checked in its turn), its ``status`` (``planned``, ``done``, ``failed`` or ``stopped``),
  the card of what came of it (``result``) and when it ended (``done_at``);
* ``confirmations.chain_basis``: what a chain remembers of the one who asked (their role and the
  ids of their own rights then). It is set for a chain only.

Going back down drops the chains that still wait (they become ``expired``, dropped by Domo): the
code of before has no way to carry out a request of several steps, and it must never run one as
something else. Then the steps go with their table, and the column with them.

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("confirmations", sa.Column("chain_basis", postgresql.JSONB(), nullable=True))
    op.create_table(
        "confirmation_steps",
        sa.Column("confirmation_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("tool_name", sa.Text(), nullable=False),
        sa.Column("action_type", sa.Text(), nullable=False),
        sa.Column("arguments", postgresql.JSONB(), nullable=False),
        sa.Column("action", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('planned', 'done', 'failed', 'stopped')",
            name=op.f("ck_confirmation_steps_status_values"),
        ),
        sa.CheckConstraint("position >= 0", name=op.f("ck_confirmation_steps_position_from_zero")),
        sa.ForeignKeyConstraint(
            ["confirmation_id"],
            ["confirmations.id"],
            name=op.f("fk_confirmation_steps_confirmation_id_confirmations"),
        ),
        sa.PrimaryKeyConstraint("confirmation_id", "position", name=op.f("pk_confirmation_steps")),
    )


def downgrade() -> None:
    op.execute(
        "UPDATE confirmations SET status = 'expired', expired_by = 'domo' "
        "WHERE chain_basis IS NOT NULL AND status = 'pending'"
    )
    op.drop_table("confirmation_steps")
    op.drop_column("confirmations", "chain_basis")
