"""A memory may come from a voice the house was not sure of.

``memories.origin`` gains ``unsure_voice``: kept from a sentence of a voice the house was not sure
was the person's (unsure, too short, or heard at another person's page), probably theirs and never
their own words (the owner, 2026-11-27: "Domo juge quoi garder"). Going back down makes such a
memory relayed words (``others_words``, the most cautious a person's memory could be before) and
restores the old constraint (Domo is not installed anywhere yet: this is for a development
database).

Revision ID: 0069
Revises: 0068
Create Date: 2026-11-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0069"
down_revision: str | None = "0068"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHECK = "origin_values"
_BEFORE = "origin IN ('own_words', 'others_words', 'deduced', 'told_by_domo', 'external')"
_AFTER = (
    "origin IN ('own_words', 'others_words', 'deduced', 'told_by_domo', 'external', 'unsure_voice')"
)


def upgrade() -> None:
    op.drop_constraint(op.f(f"ck_memories_{_CHECK}"), "memories", type_="check")
    op.create_check_constraint(op.f(f"ck_memories_{_CHECK}"), "memories", _AFTER)


def downgrade() -> None:
    op.execute("UPDATE memories SET origin = 'others_words' WHERE origin = 'unsure_voice'")
    op.drop_constraint(op.f(f"ck_memories_{_CHECK}"), "memories", type_="check")
    op.create_check_constraint(op.f(f"ck_memories_{_CHECK}"), "memories", _BEFORE)
