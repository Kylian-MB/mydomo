"""The devices a person entrusts to Domo, and the codes that entrust them.

``devices`` keeps, for a person, each device they entrusted: its name, the SHA-256 of its secret,
when it was last seen and, once taken back or forgotten, when. ``device_pairings`` keeps the codes
made to entrust one (the SHA-256 of the code, its end and when it was used). Going back down drops
both tables (Domo is not installed anywhere yet: this is for a development database).

Revision ID: 0038
Revises: 0037
Create Date: 2026-11-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_UUID = sa.text("gen_random_uuid()")


def upgrade() -> None:
    op.create_table(
        "devices",
        sa.Column("id", sa.Uuid(), server_default=NEW_UUID, nullable=False),
        sa.Column("person_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("secret_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["person_id"],
            ["persons.id"],
            name=op.f("fk_devices_person_id_persons"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_devices")),
        sa.UniqueConstraint("secret_hash", name=op.f("uq_devices_secret_hash")),
    )
    op.create_index("ix_devices_person_id", "devices", ["person_id"])
    op.create_table(
        "device_pairings",
        sa.Column("id", sa.Uuid(), server_default=NEW_UUID, nullable=False),
        sa.Column("person_id", sa.Uuid(), nullable=False),
        sa.Column("code_hash", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["person_id"],
            ["persons.id"],
            name=op.f("fk_device_pairings_person_id_persons"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_device_pairings")),
        sa.UniqueConstraint("code_hash", name=op.f("uq_device_pairings_code_hash")),
    )


def downgrade() -> None:
    op.drop_table("device_pairings")
    op.drop_index("ix_devices_person_id", table_name="devices")
    op.drop_table("devices")
