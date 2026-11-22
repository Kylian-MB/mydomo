"""An approver answers a request in their own words (spec 5.1).

* ``confirmations.shown_to_approver_message_id``: the reply of an approver (an adult for a child's
  request, the owner when a right says so) that last showed the request, in the approver's own
  conversation; only the approver's answer to that reply can decide it in words (R74 for them),
  apart from the reply that last showed it to the one who asked (``last_shown_message_id``);
* ``confirmations.approver_note``: what the approver said with a change, and how firm they were,
  in a few words written by Domo (300 characters at most), for the one who asked.

Going back down drops both.

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "confirmations", sa.Column("shown_to_approver_message_id", sa.Uuid(), nullable=True)
    )
    op.create_foreign_key(
        op.f("fk_confirmations_shown_to_approver_message_id_messages"),
        "confirmations",
        "messages",
        ["shown_to_approver_message_id"],
        ["id"],
    )
    op.add_column("confirmations", sa.Column("approver_note", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_confirmations_approver_note_length"),
        "confirmations",
        "char_length(approver_note) <= 300",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_confirmations_approver_note_length"), "confirmations", type_="check"
    )
    op.drop_column("confirmations", "approver_note")
    op.drop_constraint(
        op.f("fk_confirmations_shown_to_approver_message_id_messages"),
        "confirmations",
        type_="foreignkey",
    )
    op.drop_column("confirmations", "shown_to_approver_message_id")
