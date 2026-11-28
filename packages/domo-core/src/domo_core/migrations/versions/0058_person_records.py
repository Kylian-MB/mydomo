"""A record of ``persons`` is a person: where they stand, the child mark, a state, an origin.

``persons`` gains ``standing`` (lives here, visiting, elsewhere), ``is_child`` (the mark of a
child, always set for the built-in child role), ``born_on``, ``state`` (known, has access,
withdrawn, refused, gone), ``origin`` and ``origin_reason`` (who opened the record, and why when
it was the assistant), ``first_access_at``, ``access_returns``, ``merged_into_id``,
``forgotten_at`` and ``made_role_id``. Rows that exist get the values that say what they were:
a child carries the mark, a removed person is ``gone``, the owner is of origin ``setup``, a
person with access had it since their record was made. Going back down drops them all (the
assistant is not installed anywhere yet: this is for a development database).

Revision ID: 0058
Revises: 0057
Create Date: 2026-11-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0058"
down_revision: str | None = "0057"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHECKS = {
    "standing_values": "standing IN ('lives_here', 'visiting', 'elsewhere')",
    "child_role_is_marked": "role <> 'child' OR is_child",
    "state_values": "state IN ('known', 'has_access', 'withdrawn', 'refused', 'gone')",
    "left_has_a_date": "state NOT IN ('gone', 'withdrawn') OR disabled_at IS NOT NULL",
    "access_is_not_disabled": "state <> 'has_access' OR disabled_at IS NULL",
    "origin_values": "origin IN ('setup', 'invitation', 'domo')",
    "origin_reason_length": "char_length(origin_reason) BETWEEN 1 AND 300",
    "domo_keeps_his_reason": "origin <> 'domo' OR origin_reason IS NOT NULL",
}


def upgrade() -> None:
    op.add_column(
        "persons",
        sa.Column("standing", sa.String(), server_default=sa.text("'lives_here'"), nullable=False),
    )
    op.add_column(
        "persons",
        sa.Column("is_child", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column("persons", sa.Column("born_on", sa.Date(), nullable=True))
    op.add_column(
        "persons",
        sa.Column("state", sa.String(), server_default=sa.text("'has_access'"), nullable=False),
    )
    op.add_column(
        "persons",
        sa.Column("origin", sa.String(), server_default=sa.text("'invitation'"), nullable=False),
    )
    op.add_column("persons", sa.Column("origin_reason", sa.Text(), nullable=True))
    op.add_column(
        "persons", sa.Column("first_access_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "persons",
        sa.Column("access_returns", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column("persons", sa.Column("merged_into_id", sa.Uuid(), nullable=True))
    op.add_column("persons", sa.Column("forgotten_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("persons", sa.Column("made_role_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_persons_merged_into_id_persons"), "persons", "persons", ["merged_into_id"], ["id"]
    )
    # What the rows that exist were, before the checks look at them.
    op.execute("UPDATE persons SET is_child = true WHERE role = 'child'")
    op.execute("UPDATE persons SET state = 'gone' WHERE disabled_at IS NOT NULL")
    op.execute("UPDATE persons SET origin = 'setup' WHERE role = 'owner'")
    op.execute("UPDATE persons SET first_access_at = created_at WHERE disabled_at IS NULL")
    for name, expression in CHECKS.items():
        op.create_check_constraint(op.f(f"ck_persons_{name}"), "persons", expression)


def downgrade() -> None:
    for name in reversed(CHECKS):
        op.drop_constraint(op.f(f"ck_persons_{name}"), "persons", type_="check")
    op.drop_constraint(op.f("fk_persons_merged_into_id_persons"), "persons", type_="foreignkey")
    for column in (
        "made_role_id",
        "forgotten_at",
        "merged_into_id",
        "access_returns",
        "first_access_at",
        "origin_reason",
        "origin",
        "state",
        "born_on",
        "is_child",
        "standing",
    ):
        op.drop_column("persons", column)
