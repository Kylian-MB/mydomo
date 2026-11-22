"""What a memory's meaning is searched by.

``memories`` gains ``embedding``, the vector of its subject and content (384 numbers, the local
model's), and ``embedding_model``, the model that computed it: both empty until the core computes
them outside the turn. An HNSW index (cosine distance) finds the nearest ones. Going back down
drops them (Domo is not installed anywhere yet: this is for a development database).

Revision ID: 0042
Revises: 0041
Create Date: 2026-11-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "0042"
down_revision: str | None = "0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("memories", sa.Column("embedding", Vector(384), nullable=True))
    op.add_column("memories", sa.Column("embedding_model", sa.Text(), nullable=True))
    op.create_index(
        "ix_memories_embedding",
        "memories",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_memories_embedding", table_name="memories")
    op.drop_column("memories", "embedding_model")
    op.drop_column("memories", "embedding")
