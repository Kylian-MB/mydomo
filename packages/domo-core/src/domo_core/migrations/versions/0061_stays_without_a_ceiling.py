"""A stay has no ceiling: ``invitations.guest_days`` is a number of days from 1, with no upper bound
(``ck_invitations_guest_days_range``: ``guest_days >= 1``). No length of stay is refused by the
code (audit POL-5); an invitation's empty ``guest_days``, for a guest, is a stay with no end.

Going back down cancels the invitations that still wait with a stay above 365 days, brings every
stay above 365 days back to 365 (the rows that were used or ended keep their history otherwise),
and puts the old ceiling back.

Revision ID: 0061
Revises: 0060
Create Date: 2026-11-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0061"
down_revision: str | None = "0060"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_invitations_guest_days_range"), "invitations", type_="check")
    op.create_check_constraint(
        op.f("ck_invitations_guest_days_range"), "invitations", "guest_days >= 1"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE invitations SET cancelled_at = now() "
        "WHERE guest_days > 365 AND accepted_at IS NULL AND cancelled_at IS NULL"
    )
    op.execute("UPDATE invitations SET guest_days = 365 WHERE guest_days > 365")
    op.drop_constraint(op.f("ck_invitations_guest_days_range"), "invitations", type_="check")
    op.create_check_constraint(
        op.f("ck_invitations_guest_days_range"), "invitations", "guest_days BETWEEN 1 AND 365"
    )
