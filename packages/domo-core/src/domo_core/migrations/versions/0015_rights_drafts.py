"""The drafts of rights: what the rights writer made of the owner's words, checked on examples.

* new table ``policy_rights_drafts``: who asked (``created_by``), the owner's words (1 to 1000
  characters), the draft as it was checked (``content``: the changes, the examples with the
  outcome the code worked out, the names of the people they speak of, and the rights in force
  then), when it was made and when the owner's yes applied it (``applied_at``, once);
* ``policy_rights.draft_id`` becomes a foreign key to it: a right says which draft it came from.

Going back down drops the foreign key and the drafts. The rights keep the draft's id, which then
points at nothing (as before this revision).

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Same server-side defaults as 0001.
NEW_UUID = sa.text("gen_random_uuid()")
NOW = sa.text("clock_timestamp()")


def upgrade() -> None:
    op.create_table(
        "policy_rights_drafts",
        sa.Column("id", sa.Uuid(), server_default=NEW_UUID, nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("owner_words", sa.Text(), nullable=False),
        sa.Column("content", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "char_length(owner_words) BETWEEN 1 AND 1000",
            name=op.f("ck_policy_rights_drafts_owner_words_length"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["persons.id"], name=op.f("fk_policy_rights_drafts_created_by_persons")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_policy_rights_drafts")),
    )
    op.create_foreign_key(
        op.f("fk_policy_rights_draft_id_policy_rights_drafts"),
        "policy_rights",
        "policy_rights_drafts",
        ["draft_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_policy_rights_draft_id_policy_rights_drafts"), "policy_rights", type_="foreignkey"
    )
    op.drop_table("policy_rights_drafts")
