"""A memory says when its author is the assistant, whom a lesson is for and what it is about.

``memories`` gains ``by_domo`` (the assistant himself wrote it; ``said_by_id`` was already allowed
to be empty), ``for_everyone``, ``lesson_action_type`` and ``lesson_subject_key`` (a lesson only),
and ``told_answer`` and ``told_keep_asking`` (what he told a person only: the answer they gave and
whether they agreed to be asked again), and ``words_checked`` (the code checked, word for word,
that the passage it came from is in the person's own message: false for every memory before it).
Going back down drops them (Domo is not installed anywhere yet: this is for a development
database).

Revision ID: 0056
Revises: 0055
Create Date: 2026-11-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0056"
down_revision: str | None = "0055"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "memories",
        sa.Column("by_domo", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "memories",
        sa.Column("for_everyone", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column("memories", sa.Column("lesson_action_type", sa.Text(), nullable=True))
    op.add_column("memories", sa.Column("lesson_subject_key", sa.Text(), nullable=True))
    op.add_column("memories", sa.Column("told_answer", sa.Text(), nullable=True))
    op.add_column("memories", sa.Column("told_keep_asking", sa.Boolean(), nullable=True))
    op.add_column(
        "memories",
        sa.Column("words_checked", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_memories_lesson_fields_need_a_lesson"),
        "memories",
        "kind = 'lesson' OR (lesson_action_type IS NULL AND lesson_subject_key IS NULL "
        "AND NOT for_everyone)",
    )
    op.create_check_constraint(
        op.f("ck_memories_told_answer_values"), "memories", "told_answer IN ('yes', 'no')"
    )
    op.create_check_constraint(
        op.f("ck_memories_told_fields_need_origin"),
        "memories",
        "origin = 'told_by_domo' OR (told_answer IS NULL AND told_keep_asking IS NULL)",
    )
    op.create_index(
        "ix_memories_lesson_action_type",
        "memories",
        ["lesson_action_type"],
        postgresql_where=sa.text("lesson_action_type IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_memories_lesson_action_type", table_name="memories")
    op.drop_constraint(op.f("ck_memories_told_fields_need_origin"), "memories", type_="check")
    op.drop_constraint(op.f("ck_memories_told_answer_values"), "memories", type_="check")
    op.drop_constraint(op.f("ck_memories_lesson_fields_need_a_lesson"), "memories", type_="check")
    for column in (
        "words_checked",
        "told_keep_asking",
        "told_answer",
        "lesson_subject_key",
        "lesson_action_type",
        "for_everyone",
        "by_domo",
    ):
        op.drop_column("memories", column)
