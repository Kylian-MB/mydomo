"""The memories Domo keeps.

``memories`` keeps, for each thing Domo remembers: who said it and who it is about (both
``ON DELETE SET NULL``), a subject and a content, its kind and origin, whether he keeps it for
himself and whether it is in the always-present core, the action it is about when he told it, and
its dates. ``search`` is generated from the subject and the content (``simple`` configuration)
and indexed. A trigger deletes the memories Domo keeps for himself that a person said when
their row is deleted, before the foreign key empties ``said_by_id`` of the others. Going back down
drops it all (Domo is not installed anywhere yet: this is for a development database).

Revision ID: 0041
Revises: 0040
Create Date: 2026-11-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import TSVECTOR

revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_UUID = sa.text("gen_random_uuid()")


def upgrade() -> None:
    op.create_table(
        "memories",
        sa.Column("id", sa.Uuid(), server_default=NEW_UUID, nullable=False),
        sa.Column("said_by_id", sa.Uuid(), nullable=True),
        sa.Column("about_id", sa.Uuid(), nullable=True),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("kept_private", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("core", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("action_type", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column(
            "changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column("served_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "search",
            TSVECTOR(),
            sa.Computed("to_tsvector('simple', subject || ' ' || content)", persisted=True),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(subject) BETWEEN 1 AND 120", name=op.f("ck_memories_subject_length")
        ),
        sa.CheckConstraint(
            "char_length(content) BETWEEN 1 AND 600", name=op.f("ck_memories_content_length")
        ),
        sa.CheckConstraint(
            "kind IN ('fact', 'preference', 'moment', 'lesson', 'self')",
            name=op.f("ck_memories_kind_values"),
        ),
        sa.CheckConstraint(
            "origin IN ('own_words', 'others_words', 'deduced', 'told_by_domo', 'external')",
            name=op.f("ck_memories_origin_values"),
        ),
        sa.CheckConstraint(
            "action_type IS NULL OR origin = 'told_by_domo'",
            name=op.f("ck_memories_action_type_needs_origin"),
        ),
        sa.ForeignKeyConstraint(
            ["said_by_id"],
            ["persons.id"],
            name=op.f("fk_memories_said_by_id_persons"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["about_id"],
            ["persons.id"],
            name=op.f("fk_memories_about_id_persons"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memories")),
    )
    op.create_index("ix_memories_said_by_id", "memories", ["said_by_id"])
    op.create_index("ix_memories_core", "memories", ["core"])
    op.create_index("ix_memories_search", "memories", ["search"], postgresql_using="gin")
    op.execute(
        """
        CREATE FUNCTION forget_private_memories_of_deleted_person() RETURNS trigger AS $$
        BEGIN
            DELETE FROM memories WHERE said_by_id = OLD.id AND kept_private;
            RETURN OLD;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER persons_forget_private_memories BEFORE DELETE ON persons "
        "FOR EACH ROW EXECUTE FUNCTION forget_private_memories_of_deleted_person()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER persons_forget_private_memories ON persons")
    op.execute("DROP FUNCTION forget_private_memories_of_deleted_person()")
    op.drop_index("ix_memories_search", table_name="memories", postgresql_using="gin")
    op.drop_index("ix_memories_core", table_name="memories")
    op.drop_index("ix_memories_said_by_id", table_name="memories")
    op.drop_table("memories")
