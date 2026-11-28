"""The owner's lines on a role the assistant made (``role_accesses``): a line the owner said for
the role itself ("the nannies may not open the garage", "…but not after 22:00", "…ask me first")
is his rule (``by_owner``), which only his own request changes, and may carry a limit (with its
question to the judge), an approver, what to ask before, and its words (``summary``). It holds for
whoever has the role, now and later. (Numbered 0062 for now: to be renumbered at the merge.)

Going back down drops the owner's limits, approvers and asks of the roles; their allow or deny
stays as an ordinary line of the role.

Revision ID: 0062
Revises: 0061
Create Date: 2026-11-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0062"
down_revision: str | None = "0061"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "role_accesses",
        sa.Column("by_owner", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column("role_accesses", sa.Column("limit_expression", sa.Text(), nullable=True))
    op.add_column("role_accesses", sa.Column("judge_question", sa.Text(), nullable=True))
    op.add_column("role_accesses", sa.Column("approver", sa.Text(), nullable=True))
    op.add_column("role_accesses", sa.Column("ask", sa.Text(), nullable=True))
    op.add_column("role_accesses", sa.Column("summary", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_role_accesses_approver_values"),
        "role_accesses",
        "approver IN ('self', 'adult', 'owner')",
    )
    op.create_check_constraint(
        op.f("ck_role_accesses_ask_values"),
        "role_accesses",
        "ask IN ('yes', 'explicit_yes', 'proof')",
    )
    op.create_check_constraint(
        op.f("ck_role_accesses_limit_expression_length"),
        "role_accesses",
        "char_length(limit_expression) <= 2000",
    )
    op.create_check_constraint(
        op.f("ck_role_accesses_judge_question_length"),
        "role_accesses",
        "char_length(judge_question) <= 300",
    )
    op.create_check_constraint(
        op.f("ck_role_accesses_judge_needs_a_limit"),
        "role_accesses",
        "judge_question IS NULL OR limit_expression IS NOT NULL",
    )
    op.create_check_constraint(
        op.f("ck_role_accesses_summary_length"),
        "role_accesses",
        "char_length(summary) <= 300",
    )
    # Only the owner's lines say more than allow or deny.
    op.create_check_constraint(
        op.f("ck_role_accesses_more_is_the_owner_s"),
        "role_accesses",
        "by_owner OR (limit_expression IS NULL AND approver IS NULL AND ask IS NULL)",
    )


def downgrade() -> None:
    for name in (
        "more_is_the_owner_s",
        "summary_length",
        "judge_needs_a_limit",
        "judge_question_length",
        "limit_expression_length",
        "ask_values",
        "approver_values",
    ):
        op.drop_constraint(op.f(f"ck_role_accesses_{name}"), "role_accesses", type_="check")
    for column in ("summary", "ask", "approver", "judge_question", "limit_expression", "by_owner"):
        op.drop_column("role_accesses", column)
