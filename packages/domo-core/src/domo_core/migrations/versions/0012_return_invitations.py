"""Return invitations: an invitation may be for a person who left the household.

``invitations.person_id`` is empty for an ordinary invitation, which makes a new person, and is the
person for a return link, which brings that same person back (their username, their history).
The foreign key is to ``persons.id``, as ``created_by`` and ``accepted_person_id`` are.

Going back down cancels the return links that still wait: without the column nothing would tell
them from ordinary invitations, and a link made to bring someone back must not become one that
makes a new person.

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("invitations", sa.Column("person_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_invitations_person_id_persons"), "invitations", "persons", ["person_id"], ["id"]
    )


def downgrade() -> None:
    op.execute(
        "UPDATE invitations SET cancelled_at = clock_timestamp() "
        "WHERE person_id IS NOT NULL AND accepted_at IS NULL AND cancelled_at IS NULL"
    )
    op.drop_constraint(op.f("fk_invitations_person_id_persons"), "invitations", type_="foreignkey")
    op.drop_column("invitations", "person_id")
