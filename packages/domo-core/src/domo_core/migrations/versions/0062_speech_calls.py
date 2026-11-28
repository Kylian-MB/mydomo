"""The calls of sound: what a model heard and said is counted like any call.

``model_calls`` gains ``heard_seconds`` (the seconds of sound a call heard) and ``said_chars``
(the characters it said), both zero for a chat call, and its ``task`` takes the five new kinds
(``hear``, ``say``, ``voice_print``, ``attention``, ``heard_sorting``). Going back down drops the
columns and the calls of the new kinds (Domo is not installed anywhere yet: this is for a
development database).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0062"
down_revision: str | None = "0057"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_TASKS = (
    "('conversation', 'judge', 'intent', 'rights_writer', 'key_check', 'upkeep', "
    "'writing', 'checking', 'arrival', 'memory', 'toolmaking', 'tool_review')"
)
# Merged with the task kinds other branches added (none up to 0057): never drop one.
_NEW_TASKS = (
    "('conversation', 'judge', 'intent', 'rights_writer', 'key_check', 'upkeep', "
    "'writing', 'checking', 'arrival', 'memory', 'toolmaking', 'tool_review', "
    "'hear', 'say', 'voice_print', 'attention', 'heard_sorting')"
)


def _set_tasks(tasks: str) -> None:
    op.drop_constraint(op.f("ck_model_calls_task_values"), "model_calls", type_="check")
    op.create_check_constraint(
        op.f("ck_model_calls_task_values"), "model_calls", f"task IN {tasks}"
    )


def upgrade() -> None:
    _set_tasks(_NEW_TASKS)
    op.add_column(
        "model_calls",
        sa.Column("heard_seconds", sa.Numeric(10, 2), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "model_calls",
        sa.Column("said_chars", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("model_calls", "said_chars")
    op.drop_column("model_calls", "heard_seconds")
    # Only the five kinds this revision added, by name: a task kind another branch added is kept.
    op.execute(
        "DELETE FROM model_calls WHERE task IN "
        "('hear', 'say', 'voice_print', 'attention', 'heard_sorting')"
    )
    _set_tasks(_OLD_TASKS)
