"""A spending limit may be for a role, for each of them or for them together.

``spending_limits`` gains ``role`` (``adult``, ``child`` or ``guest``) and ``split`` (``each``: the
amount is for every one who has the role; ``together``: it is shared by all of them), both empty
for a limit of the whole key or of one person. They go together, and never with a person. The
limits of one key are now one per people: ``(provider_id, person_id, role, split)``, so that
"each" and "together" for one role are two limits. Going back down removes the limits of a role
(Domo is not installed anywhere yet: this is for a development database).

Revision ID: 0029
Revises: 0028
Create Date: 2026-11-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("spending_limits", sa.Column("role", sa.Text(), nullable=True))
    op.add_column("spending_limits", sa.Column("split", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_spending_limits_role_values"),
        "spending_limits",
        "role IN ('adult', 'child', 'guest')",
    )
    op.create_check_constraint(
        op.f("ck_spending_limits_split_values"), "spending_limits", "split IN ('each', 'together')"
    )
    op.create_check_constraint(
        op.f("ck_spending_limits_role_matches_split"),
        "spending_limits",
        "(role IS NULL) = (split IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_spending_limits_role_or_person"),
        "spending_limits",
        "role IS NULL OR person_id IS NULL",
    )
    op.drop_constraint(
        op.f("uq_spending_limits_provider_person"), "spending_limits", type_="unique"
    )
    op.create_unique_constraint(
        op.f("uq_spending_limits_provider_whom"),
        "spending_limits",
        ["provider_id", "person_id", "role", "split"],
        postgresql_nulls_not_distinct=True,
    )


def downgrade() -> None:
    op.execute("DELETE FROM spending_limits WHERE role IS NOT NULL")
    op.drop_constraint(op.f("uq_spending_limits_provider_whom"), "spending_limits", type_="unique")
    op.create_unique_constraint(
        op.f("uq_spending_limits_provider_person"),
        "spending_limits",
        ["provider_id", "person_id"],
        postgresql_nulls_not_distinct=True,
    )
    for name in ("role_or_person", "role_matches_split", "split_values", "role_values"):
        op.drop_constraint(op.f(f"ck_spending_limits_{name}"), "spending_limits", type_="check")
    op.drop_column("spending_limits", "split")
    op.drop_column("spending_limits", "role")
