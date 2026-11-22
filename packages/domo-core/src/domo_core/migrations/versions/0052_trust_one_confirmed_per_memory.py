"""One confirmed fact per memory.

``uq_trust_facts_confirmed_memory_id`` (the review of trust per person): a memory that someone
said about another person is confirmed or denied by that person once, and gives one ``confirmed``
fact at most; noting it again would count the same word twice. A partial unique index on
``trust_facts.memory_id`` where ``established = 'confirmed'``. Going back down drops it.

Revision ID: 0052
Revises: 0051
Create Date: 2026-11-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0052"
down_revision: str | None = "0051"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_trust_facts_confirmed_memory_id",
        "trust_facts",
        ["memory_id"],
        unique=True,
        postgresql_where=sa.text("established = 'confirmed'"),
    )


def downgrade() -> None:
    op.drop_index("uq_trust_facts_confirmed_memory_id", table_name="trust_facts")
