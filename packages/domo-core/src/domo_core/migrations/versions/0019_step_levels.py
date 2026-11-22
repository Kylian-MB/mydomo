"""The steps of a chain keep their level, and how they are judged; a chain keeps its own level.

* ``confirmation_steps.level``: the level the barrier gave the step when the chain was asked for
  (``free``, ``confirm`` or ``always_confirm``), so that the yes of a chain is said in the style
  of each step at its own level (spec 4.2). ``NULL`` for a step checked in its turn (it had no
  level then), and for the steps stored before this revision: a step that was described and has
  none takes the stricter style of ``confirm`` and ``always_confirm``
  (``confirmations.steps.step_levels``), which closes;
* ``confirmation_steps.with_basis``: whether the step is judged with what the chain remembers of
  the one who asked (``ChainBasis``) or with the role of the moment it was changed (spec 4.1).
  True unless said otherwise: every step stored so far was asked for as it is;
* ``confirmations.chain_level``: for a chain, the level the barrier gave the chain itself
  (``chain.run``: a right of the owner on chains may ask a yes of it), whose style its own yes is
  said in. ``NULL`` for a request that is no chain, and for a chain stored before this revision
  (read as the request's own ``level``, which closes).

Going back down drops the three columns: the code of before reads none of them.

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("confirmation_steps", sa.Column("level", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_confirmation_steps_level_values"),
        "confirmation_steps",
        "level IN ('free', 'confirm', 'always_confirm')",
    )
    op.add_column(
        "confirmation_steps",
        sa.Column("with_basis", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    )
    op.add_column("confirmations", sa.Column("chain_level", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_confirmations_chain_level_values"),
        "confirmations",
        "chain_level IN ('free', 'confirm', 'always_confirm')",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_confirmations_chain_level_values"), "confirmations", type_="check")
    op.drop_column("confirmations", "chain_level")
    op.drop_column("confirmation_steps", "with_basis")
    op.drop_constraint(
        op.f("ck_confirmation_steps_level_values"), "confirmation_steps", type_="check"
    )
    op.drop_column("confirmation_steps", "level")
