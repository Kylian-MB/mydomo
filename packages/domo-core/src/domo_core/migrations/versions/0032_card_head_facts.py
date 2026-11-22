"""The head of a card keeps what it was written from.

``card_texts.facts_hash`` is the hash of what the head of a card was written from for its reader
(``texts.cards.head_fingerprint``: what the card said, the words of its holes, the reader's
language): a head is shown only while its card still says the same thing to its reader. A head kept
before has none (``''``, which matches nothing): it is not shown, and is written again. Going back
down drops the column (Domo is not installed anywhere yet: this is for a development database).

Revision ID: 0032
Revises: 0031
Create Date: 2026-11-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "card_texts",
        sa.Column("facts_hash", sa.Text(), server_default=sa.text("''"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("card_texts", "facts_hash")
