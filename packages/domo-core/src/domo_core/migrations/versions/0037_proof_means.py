"""The means of proof each person has agreed.

``proof_means`` keeps, for a person, a means of proof they have agreed: the kind (which the code
knows how to verify), who proposed it, when it was agreed and, once taken away, when. A person has
at most one live row of each kind. Every existing account is given its ``password`` means, and
every account that has a passkey its ``passkey`` means (proposed by the code). Going back down
drops the table (Domo is not installed anywhere yet: this is for a development database).

Revision ID: 0037
Revises: 0036
Create Date: 2026-11-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_UUID = sa.text("gen_random_uuid()")


def upgrade() -> None:
    op.create_table(
        "proof_means",
        sa.Column("id", sa.Uuid(), server_default=NEW_UUID, nullable=False),
        sa.Column("person_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("proposed_by", sa.Text(), nullable=False),
        sa.Column("agreed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "proposed_by IN ('code', 'domo', 'person')",
            name=op.f("ck_proof_means_proposed_by_values"),
        ),
        sa.ForeignKeyConstraint(
            ["person_id"],
            ["persons.id"],
            name=op.f("fk_proof_means_person_id_persons"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_proof_means")),
    )
    op.create_index(
        "uq_proof_means_person_id_kind_live",
        "proof_means",
        ["person_id", "kind"],
        unique=True,
        postgresql_where=sa.text("removed_at IS NULL"),
    )
    # What each person already has, without having to do anything.
    op.execute(
        "INSERT INTO proof_means (person_id, kind, proposed_by, agreed_at) "
        "SELECT person_id, 'password', 'code', clock_timestamp() FROM accounts"
    )
    op.execute(
        "INSERT INTO proof_means (person_id, kind, proposed_by, agreed_at) "
        "SELECT DISTINCT a.person_id, 'passkey', 'code', clock_timestamp() FROM accounts a "
        "JOIN webauthn_credentials c ON c.account_id = a.id"
    )


def downgrade() -> None:
    op.drop_index("uq_proof_means_person_id_kind_live", table_name="proof_means")
    op.drop_table("proof_means")
