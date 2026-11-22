"""What each person really received of Domo's replies.

``deliveries`` keeps, for each reply of Domo that reached someone: the reply (``ON DELETE
CASCADE``), the person (``ON DELETE CASCADE``), the channel that read it, the device it reached
(``ON DELETE SET NULL``; none for the page), how it came there (``live`` or ``fetched``), how much
of it (``whole`` or ``cut``) and when. One row per reply, channel, device and way, a missing device
counting as one. Going back down drops it all (Domo is not installed anywhere yet: this is for a
development database).

Revision ID: 0047
Revises: 0046
Create Date: 2026-11-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0047"
down_revision: str | None = "0046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_UUID = sa.text("gen_random_uuid()")


def upgrade() -> None:
    op.create_table(
        "deliveries",
        sa.Column("id", sa.Uuid(), server_default=NEW_UUID, nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("person_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("device_id", sa.Uuid(), nullable=True),
        sa.Column("how", sa.Text(), nullable=False),
        sa.Column("extent", sa.Text(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("how IN ('live', 'fetched')", name=op.f("ck_deliveries_how_values")),
        sa.CheckConstraint("extent IN ('whole', 'cut')", name=op.f("ck_deliveries_extent_values")),
        sa.CheckConstraint(
            "char_length(channel) BETWEEN 1 AND 40", name=op.f("ck_deliveries_channel_length")
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name=op.f("fk_deliveries_message_id_messages"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["person_id"],
            ["persons.id"],
            name=op.f("fk_deliveries_person_id_persons"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["devices.id"],
            name=op.f("fk_deliveries_device_id_devices"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_deliveries")),
        sa.UniqueConstraint(
            "message_id",
            "channel",
            "device_id",
            "how",
            name=op.f("uq_deliveries_one_per_way"),
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index("ix_deliveries_person_id_at", "deliveries", ["person_id", "at"])


def downgrade() -> None:
    op.drop_index("ix_deliveries_person_id_at", table_name="deliveries")
    op.drop_table("deliveries")
