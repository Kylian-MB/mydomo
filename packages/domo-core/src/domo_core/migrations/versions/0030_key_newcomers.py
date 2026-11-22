"""The giver of a key is told once who began using it.

``key_newcomers`` keeps, for a key and a person, that the giver was told the person began using
the key. It goes with the key (``ON DELETE CASCADE``). Going back down drops the table (Domo is
not installed anywhere yet: this is for a development database).

Revision ID: 0030
Revises: 0029
Create Date: 2026-11-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "key_newcomers",
        sa.Column("provider_id", sa.Text(), nullable=False),
        sa.Column("person_id", sa.Uuid(), nullable=False),
        sa.Column("told_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["providers.id"],
            name=op.f("fk_key_newcomers_provider_id_providers"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["person_id"], ["persons.id"], name=op.f("fk_key_newcomers_person_id_persons")
        ),
        sa.PrimaryKeyConstraint("provider_id", "person_id", name=op.f("pk_key_newcomers")),
    )


def downgrade() -> None:
    op.drop_table("key_newcomers")
