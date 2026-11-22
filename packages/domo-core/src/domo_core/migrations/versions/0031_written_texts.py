"""The texts Domo writes himself, and what goes with them.

``written_texts`` keeps a text for a key of the catalog of meanings, for a person or for a
language; ``card_texts`` the head of a card written for a reader. A person gains the language they
read, a note on how they like to be written to and the time of their last visit; a message the
reason a reply failed; a note when Domo told it. The kinds of tasks of a model call gain
``writing``, ``checking`` and ``arrival``. Going back down undoes all of it (Domo is not installed
anywhere yet: this is for a development database); the calls of the new kinds are dropped first,
as the old constraint would refuse them.

Revision ID: 0031
Revises: 0030
Create Date: 2026-11-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_UUID = sa.text("gen_random_uuid()")
_OLD_TASKS = "('conversation', 'judge', 'intent', 'rights_writer', 'key_check', 'upkeep')"
_NEW_TASKS = (
    "('conversation', 'judge', 'intent', 'rights_writer', 'key_check', 'upkeep', "
    "'writing', 'checking', 'arrival')"
)


def _set_tasks(tasks: str) -> None:
    op.drop_constraint(op.f("ck_model_calls_task_values"), "model_calls", type_="check")
    op.create_check_constraint(
        op.f("ck_model_calls_task_values"), "model_calls", f"task IN {tasks}"
    )


def upgrade() -> None:
    op.create_table(
        "written_texts",
        sa.Column("id", sa.Uuid(), server_default=NEW_UUID, nullable=False),
        sa.Column("person_id", sa.Uuid(), nullable=True),
        sa.Column("language", sa.Text(), nullable=True),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("meaning_hash", sa.Text(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column("written_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "previous",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(person_id IS NULL) <> (language IS NULL)", name=op.f("ck_written_texts_one_reader")
        ),
        sa.ForeignKeyConstraint(
            ["person_id"],
            ["persons.id"],
            name=op.f("fk_written_texts_person_id_persons"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_written_texts")),
    )
    op.create_index(
        "uq_written_texts_person_id_key",
        "written_texts",
        ["person_id", "key"],
        unique=True,
        postgresql_where=sa.text("person_id IS NOT NULL"),
    )
    op.create_index(
        "uq_written_texts_language_key",
        "written_texts",
        ["language", "key"],
        unique=True,
        postgresql_where=sa.text("language IS NOT NULL"),
    )
    op.create_table(
        "card_texts",
        sa.Column("id", sa.Uuid(), server_default=NEW_UUID, nullable=False),
        sa.Column("confirmation_id", sa.Uuid(), nullable=True),
        sa.Column("message_action_id", sa.Uuid(), nullable=True),
        sa.Column("reader_id", sa.Uuid(), nullable=False),
        sa.Column("head", sa.Text(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column("written_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(confirmation_id IS NULL) <> (message_action_id IS NULL)",
            name=op.f("ck_card_texts_one_card"),
        ),
        sa.ForeignKeyConstraint(
            ["confirmation_id"],
            ["confirmations.id"],
            name=op.f("fk_card_texts_confirmation_id_confirmations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["message_action_id"],
            ["message_actions.id"],
            name=op.f("fk_card_texts_message_action_id_message_actions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["reader_id"], ["persons.id"], name=op.f("fk_card_texts_reader_id_persons")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_card_texts")),
        sa.UniqueConstraint(
            "confirmation_id",
            "message_action_id",
            "reader_id",
            name=op.f("uq_card_texts_card_reader"),
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.add_column("persons", sa.Column("language", sa.Text(), nullable=True))
    op.add_column("persons", sa.Column("writing_note", sa.Text(), nullable=True))
    op.add_column("persons", sa.Column("last_visit_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("messages", sa.Column("failure", sa.Text(), nullable=True))
    op.add_column("notes", sa.Column("told_at", sa.DateTime(timezone=True), nullable=True))
    _set_tasks(_NEW_TASKS)


def downgrade() -> None:
    op.execute("DELETE FROM model_calls WHERE task IN ('writing', 'checking', 'arrival')")
    _set_tasks(_OLD_TASKS)
    op.drop_column("notes", "told_at")
    op.drop_column("messages", "failure")
    op.drop_column("persons", "last_visit_at")
    op.drop_column("persons", "writing_note")
    op.drop_column("persons", "language")
    op.drop_table("card_texts")
    op.drop_index("uq_written_texts_language_key", table_name="written_texts")
    op.drop_index("uq_written_texts_person_id_key", table_name="written_texts")
    op.drop_table("written_texts")
