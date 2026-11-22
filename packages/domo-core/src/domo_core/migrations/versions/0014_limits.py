"""The limits: what Domo could not tell the owner, and a guest's stay up to a technical ceiling.

* ``confirmations.ask_reason`` (text of 300 characters at most, nullable): the question of the
  right whose limit sent the request to the owner (spec 5.7), shown on the owner's card;
* ``invitations.guest_days`` goes from 1 to 365 days (``ck_invitations_guest_days_range``): the
  household's rule, 30 days unless the owner chose otherwise, is now a limit of the catalog that
  the barrier applies (spec 5.11); the table only keeps a technical ceiling.

Going back down drops the questions, and brings the stays longer than 30 days back to 30 (they
could not be stored before): a shorter stay than the owner allowed, never a longer one.

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("confirmations", sa.Column("ask_reason", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_confirmations_ask_reason_length"),
        "confirmations",
        "char_length(ask_reason) <= 300",
    )
    op.drop_constraint(op.f("ck_invitations_guest_days_range"), "invitations", type_="check")
    op.create_check_constraint(
        op.f("ck_invitations_guest_days_range"), "invitations", "guest_days BETWEEN 1 AND 365"
    )


def downgrade() -> None:
    op.execute("UPDATE invitations SET guest_days = 30 WHERE guest_days > 30")
    op.drop_constraint(op.f("ck_invitations_guest_days_range"), "invitations", type_="check")
    op.create_check_constraint(
        op.f("ck_invitations_guest_days_range"), "invitations", "guest_days BETWEEN 1 AND 30"
    )
    op.drop_constraint(op.f("ck_confirmations_ask_reason_length"), "confirmations", type_="check")
    op.drop_column("confirmations", "ask_reason")
