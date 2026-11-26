"""What went wrong: the table of facts of mistakes noted by code.

``went_wrong`` (see ``learning.models``): a kind, the action type and subject key it was about, who
refused, undid or complained, whose idea it was (Domo's or not), whose request or turn it was, a
short reason and whether someone was asked why. Going back down drops it (Domo is not installed
anywhere yet: this is for a development database).

Revision ID: 0055
Revises: 0054
Create Date: 2026-11-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0055"
down_revision: str | None = "0054"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_UUID = sa.text("gen_random_uuid()")
KINDS = (
    "refused",
    "expired",
    "judge_refused",
    "warning_declined",
    "undone",
    "complaint",
    "wrong_verdict",
    "asked_for_nothing",
    "failed",
    "build_abandoned",
    "launch_refused",
)


def upgrade() -> None:
    op.create_table(
        "went_wrong",
        sa.Column("id", sa.Uuid(), server_default=NEW_UUID, nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("action_type", sa.Text(), nullable=False),
        sa.Column("subject_key", sa.Text(), nullable=True),
        sa.Column("action_id", sa.Uuid(), nullable=True),
        sa.Column("confirmation_id", sa.Uuid(), nullable=True),
        sa.Column("by_person_id", sa.Uuid(), nullable=True),
        sa.Column("by_domo", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("of_domo", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("about_person_id", sa.Uuid(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("asked_why_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind IN (" + ", ".join(f"'{kind}'" for kind in KINDS) + ")",
            name=op.f("ck_went_wrong_kind_values"),
        ),
        sa.CheckConstraint("char_length(reason) <= 300", name=op.f("ck_went_wrong_reason_length")),
        sa.ForeignKeyConstraint(
            ["action_id"],
            ["message_actions.id"],
            name=op.f("fk_went_wrong_action_id_message_actions"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["confirmation_id"],
            ["confirmations.id"],
            name=op.f("fk_went_wrong_confirmation_id_confirmations"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["by_person_id"],
            ["persons.id"],
            name=op.f("fk_went_wrong_by_person_id_persons"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["about_person_id"],
            ["persons.id"],
            name=op.f("fk_went_wrong_about_person_id_persons"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_went_wrong")),
    )
    op.create_index(
        "ix_went_wrong_action_type_at", "went_wrong", ["action_type", sa.text("at DESC")]
    )
    op.create_index(
        "ix_went_wrong_subject_key_at", "went_wrong", ["subject_key", sa.text("at DESC")]
    )


def downgrade() -> None:
    op.drop_index("ix_went_wrong_subject_key_at", table_name="went_wrong")
    op.drop_index("ix_went_wrong_action_type_at", table_name="went_wrong")
    op.drop_table("went_wrong")
