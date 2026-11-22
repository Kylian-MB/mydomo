"""For whom an invitation's link waits, when it is not the one who made the invitation.

``invitations.link_for_id`` (2026-11-16, the review of the owner's trial of 0.9.0): a link given
again after someone else's yes (or when nobody could be shown it) waits, behind the "show the
link" button, for the person who asked for it, who may not be the one who made the invitation.
``NULL``: the link is for the one who made it, as before. Going back down drops it (Domo is not
installed anywhere yet: this is for a development database).

Revision ID: 0050
Revises: 0049
Create Date: 2026-11-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0050"
down_revision: str | None = "0049"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "invitations",
        sa.Column(
            "link_for_id",
            sa.Uuid(),
            sa.ForeignKey(
                "persons.id", name="fk_invitations_link_for_id_persons", ondelete="SET NULL"
            ),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_constraint("fk_invitations_link_for_id_persons", "invitations", type_="foreignkey")
    op.drop_column("invitations", "link_for_id")
