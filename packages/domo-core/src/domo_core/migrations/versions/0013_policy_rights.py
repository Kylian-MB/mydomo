"""The owner's rights, and the owner as one who says yes to a request.

* new table ``policy_rights``: one right of the owner, about an action type and a subject (a
  person or a role, exactly one of the two), with what it says (access, level, who says yes, a
  limit), its summary and the owner's words. A right is never deleted (``revoked_at``), and a
  person's right may be paused (``paused_at``). One right that is not revoked per subject and
  action type: a unique index on the two subject columns and the type, where nulls count as equal
  (the subject column that is not used is null);
* ``confirmations.approver_kind`` accepts ``owner``: a request that only the owner may answer.

Going back down drops the requests that wait for the owner (they become ``expired``, dropped by
Domo): without the new value nothing would tell them apart, and they must not be handed to
someone else to approve. The others that had the owner as approver read as an adult's (someone
other than the requester said yes). The rights go with their table.

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Same server-side defaults as 0001.
NEW_UUID = sa.text("gen_random_uuid()")
NOW = sa.text("clock_timestamp()")


def upgrade() -> None:
    op.create_table(
        "policy_rights",
        sa.Column("id", sa.Uuid(), server_default=NEW_UUID, nullable=False),
        sa.Column("person_id", sa.Uuid(), nullable=True),
        sa.Column("role", sa.Text(), nullable=True),
        sa.Column("action_type", sa.Text(), nullable=False),
        sa.Column("access", sa.Text(), nullable=True),
        sa.Column("level", sa.Text(), nullable=True),
        sa.Column("approver", sa.Text(), nullable=True),
        sa.Column("limit_expression", sa.Text(), nullable=True),
        sa.Column("judge_question", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("owner_words", sa.Text(), nullable=False),
        sa.Column("draft_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(person_id IS NULL) <> (role IS NULL)", name=op.f("ck_policy_rights_one_subject")
        ),
        sa.CheckConstraint(
            "role IN ('owner', 'adult', 'child', 'guest')",
            name=op.f("ck_policy_rights_role_values"),
        ),
        sa.CheckConstraint(
            "char_length(action_type) <= 100", name=op.f("ck_policy_rights_action_type_length")
        ),
        sa.CheckConstraint(
            "access IN ('allow', 'deny')", name=op.f("ck_policy_rights_access_values")
        ),
        sa.CheckConstraint(
            "level IN ('free', 'confirm', 'always_confirm')",
            name=op.f("ck_policy_rights_level_values"),
        ),
        sa.CheckConstraint(
            "approver IN ('self', 'adult', 'owner')", name=op.f("ck_policy_rights_approver_values")
        ),
        sa.CheckConstraint(
            "char_length(limit_expression) <= 2000",
            name=op.f("ck_policy_rights_limit_expression_length"),
        ),
        sa.CheckConstraint(
            "char_length(judge_question) <= 300",
            name=op.f("ck_policy_rights_judge_question_length"),
        ),
        sa.CheckConstraint(
            "judge_question IS NULL OR limit_expression IS NOT NULL",
            name=op.f("ck_policy_rights_judge_needs_a_limit"),
        ),
        sa.CheckConstraint(
            "char_length(summary) BETWEEN 1 AND 300", name=op.f("ck_policy_rights_summary_length")
        ),
        sa.CheckConstraint(
            "char_length(owner_words) <= 1000", name=op.f("ck_policy_rights_owner_words_length")
        ),
        sa.ForeignKeyConstraint(
            ["person_id"], ["persons.id"], name=op.f("fk_policy_rights_person_id_persons")
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["persons.id"], name=op.f("fk_policy_rights_created_by_persons")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_policy_rights")),
    )
    op.create_index(
        "uq_policy_rights_subject_action_type",
        "policy_rights",
        ["person_id", "role", "action_type"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
        postgresql_nulls_not_distinct=True,
    )

    op.drop_constraint(
        op.f("ck_confirmations_approver_kind_values"), "confirmations", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_confirmations_approver_kind_values"),
        "confirmations",
        "approver_kind IN ('self', 'adult', 'owner')",
    )


def downgrade() -> None:
    op.execute(
        "UPDATE confirmations SET status = 'expired', expired_by = 'domo' "
        "WHERE approver_kind = 'owner' AND status = 'pending'"
    )
    op.execute("UPDATE confirmations SET approver_kind = 'adult' WHERE approver_kind = 'owner'")
    op.drop_constraint(
        op.f("ck_confirmations_approver_kind_values"), "confirmations", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_confirmations_approver_kind_values"),
        "confirmations",
        "approver_kind IN ('self', 'adult')",
    )

    op.drop_index("uq_policy_rights_subject_action_type", table_name="policy_rights")
    op.drop_table("policy_rights")
