"""The names each person uses for the others.

``person_nicknames`` keeps, for each person (``person_id``, ``ON DELETE CASCADE``), the nicknames
they use for someone of the household (``target_id``, ``ON DELETE CASCADE``): the nickname as they
say it (1 to 40 characters), what it reads as (``nickname_key``) and when it was kept. One per
person and key. Going back down drops it (Domo is not installed anywhere yet: this is for a
development database).

Revision ID: 0048
Revises: 0047
Create Date: 2026-11-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0048"
down_revision: str | None = "0047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_UUID = sa.text("gen_random_uuid()")


def upgrade() -> None:
    op.create_table(
        "person_nicknames",
        sa.Column("id", sa.Uuid(), server_default=NEW_UUID, nullable=False),
        sa.Column("person_id", sa.Uuid(), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("nickname", sa.Text(), nullable=False),
        sa.Column("nickname_key", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "char_length(nickname) BETWEEN 1 AND 40",
            name=op.f("ck_person_nicknames_nickname_length"),
        ),
        sa.ForeignKeyConstraint(
            ["person_id"],
            ["persons.id"],
            name=op.f("fk_person_nicknames_person_id_persons"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["target_id"],
            ["persons.id"],
            name=op.f("fk_person_nicknames_target_id_persons"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_person_nicknames")),
        sa.UniqueConstraint(
            "person_id", "nickname_key", name=op.f("uq_person_nicknames_one_per_person")
        ),
    )


def downgrade() -> None:
    op.drop_table("person_nicknames")
