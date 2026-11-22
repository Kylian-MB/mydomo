"""A right carries what the owner wants asked, or never asked, for a person, a role or everyone.

* ``policy_rights.ask``: what Domo must ask before the action, whatever he would judge (``yes``,
  ``explicit_yes``, ``proof``), or the owner's "never ask me" (``judge``: leave it to Domo's
  judge; ``never``: not at all, only on a yes the owner gets by default). Empty: the right says
  nothing of it;
* ``policy_rights.ask_when``: the owner's conditions to a ``judge``, in his words (300 characters
  at most), and only with it;
* a right for nobody in particular (no person, no role) is a rule for everyone: it says what to
  ask and nothing else (``everyone_only_asks``). The rule of one subject exactly becomes "never a
  person and a role at once" (``at_most_one_subject``). The unique index already counts nulls as
  equal: one rule for everyone per action type.

The level stays for now: the yes-judge work drops it with its own migration.

Going back down erases the rules for everyone (nothing before could hold them), drops both columns
and puts the rule of one subject exactly back.

Revision ID: 0024
Revises: 0023
Create Date: 2026-11-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "policy_rights"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("ask", sa.Text(), nullable=True))
    op.add_column(_TABLE, sa.Column("ask_when", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_policy_rights_ask_values"),
        _TABLE,
        "ask IN ('judge', 'never', 'yes', 'explicit_yes', 'proof')",
    )
    op.create_check_constraint(
        op.f("ck_policy_rights_ask_when_length"), _TABLE, "char_length(ask_when) <= 300"
    )
    # (Not "ask = 'judge'": a check passes on null, and a null ask would let conditions in.)
    op.create_check_constraint(
        op.f("ck_policy_rights_ask_when_needs_judge"),
        _TABLE,
        "ask_when IS NULL OR ask IS NOT DISTINCT FROM 'judge'",
    )
    op.drop_constraint(op.f("ck_policy_rights_one_subject"), _TABLE, type_="check")
    op.create_check_constraint(
        op.f("ck_policy_rights_at_most_one_subject"),
        _TABLE,
        "NOT (person_id IS NOT NULL AND role IS NOT NULL)",
    )
    op.create_check_constraint(
        op.f("ck_policy_rights_everyone_only_asks"),
        _TABLE,
        "person_id IS NOT NULL OR role IS NOT NULL OR (access IS NULL AND approver IS NULL "
        "AND limit_expression IS NULL AND level IS NULL)",
    )


def downgrade() -> None:
    op.execute("DELETE FROM policy_rights WHERE person_id IS NULL AND role IS NULL")
    op.drop_constraint(op.f("ck_policy_rights_everyone_only_asks"), _TABLE, type_="check")
    op.drop_constraint(op.f("ck_policy_rights_at_most_one_subject"), _TABLE, type_="check")
    op.create_check_constraint(
        op.f("ck_policy_rights_one_subject"), _TABLE, "(person_id IS NULL) <> (role IS NULL)"
    )
    op.drop_constraint(op.f("ck_policy_rights_ask_when_needs_judge"), _TABLE, type_="check")
    op.drop_constraint(op.f("ck_policy_rights_ask_when_length"), _TABLE, type_="check")
    op.drop_constraint(op.f("ck_policy_rights_ask_values"), _TABLE, type_="check")
    op.drop_column(_TABLE, "ask_when")
    op.drop_column(_TABLE, "ask")
