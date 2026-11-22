"""Where Domo stopped going back over a conversation, and who is owed a last look.

``conversations`` gains ``reviewed_upto_at``, the time of the last message a review read (empty:
none was read yet); ``persons`` gains ``last_review_due``, set where someone is removed and where a
guest's stay is given, until the review that follows their departure is over. The kinds of tasks of
a model call gain ``memory``. Going back down undoes all of it (Domo is not installed anywhere
yet: this is for a development database); the calls of the new kind are dropped first, as the old
constraint would refuse them.

Revision ID: 0043
Revises: 0042
Create Date: 2026-11-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0043"
down_revision: str | None = "0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_TASKS = (
    "('conversation', 'judge', 'intent', 'rights_writer', 'key_check', 'upkeep', "
    "'writing', 'checking', 'arrival')"
)
_NEW_TASKS = (
    "('conversation', 'judge', 'intent', 'rights_writer', 'key_check', 'upkeep', "
    "'writing', 'checking', 'arrival', 'memory')"
)


def _set_tasks(tasks: str) -> None:
    op.drop_constraint(op.f("ck_model_calls_task_values"), "model_calls", type_="check")
    op.create_check_constraint(
        op.f("ck_model_calls_task_values"), "model_calls", f"task IN {tasks}"
    )


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("reviewed_upto_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "persons",
        sa.Column("last_review_due", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    _set_tasks(_NEW_TASKS)


def downgrade() -> None:
    op.execute("DELETE FROM model_calls WHERE task = 'memory'")
    _set_tasks(_OLD_TASKS)
    op.drop_column("persons", "last_review_due")
    op.drop_column("conversations", "reviewed_upto_at")
