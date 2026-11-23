"""Going out (step 2, spec 13): the addresses Domo opens, and the accounts the vault keeps.

``open_addresses`` (a host and a port a program may reach, for one tool or for a program run by
hand, opened and closed by Domo or a person, and whether the owner closed it; one live row per
tool, host and port, by a partial unique index where the absence of a tool counts as a value of its
own) and ``service_accounts`` (an account by its vault reference, a person's or the house's, its
hosts and its place, as the vault keeps it and never a value, who gave it, whether it is removed
and whether by the owner, open to guests or to Domo on his own; one live row per person, or the
house, and name). See ``outward.models``. Going back down drops both (Domo is not installed
anywhere yet: this is for a development database).

Revision ID: 0054
Revises: 0053
Create Date: 2026-11-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0054"
down_revision: str | None = "0053"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_UUID = sa.text("gen_random_uuid()")
NOW = sa.text("clock_timestamp()")
_UUID = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


def _who(column: str) -> str:
    """``column`` says ``"domo"`` or a person's id (``outward.models.who_rule``)."""
    return f"{column} = 'domo' OR {column} ~ '^{_UUID}$'"


def upgrade() -> None:
    op.create_table(
        "open_addresses",
        sa.Column("id", sa.Uuid(), server_default=NEW_UUID, nullable=False),
        sa.Column("tool_name", sa.Text(), nullable=True),
        sa.Column("host", sa.Text(), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("on_host", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("opened_by", sa.Text(), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_by", sa.Text(), nullable=True),
        sa.Column("closed_by_owner", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.CheckConstraint(
            "char_length(host) BETWEEN 1 AND 253 AND host = lower(host)",
            name=op.f("ck_open_addresses_host_format"),
        ),
        sa.CheckConstraint("port BETWEEN 1 AND 65535", name=op.f("ck_open_addresses_port_range")),
        sa.CheckConstraint(
            "tool_name IS NULL OR tool_name ~ '^[a-z][a-z0-9_]{0,39}$'",
            name=op.f("ck_open_addresses_tool_name_format"),
        ),
        sa.CheckConstraint(_who("opened_by"), name=op.f("ck_open_addresses_opened_by_format")),
        sa.CheckConstraint(
            f"closed_by IS NULL OR {_who('closed_by')}",
            name=op.f("ck_open_addresses_closed_by_format"),
        ),
        sa.CheckConstraint(
            "(closed_at IS NULL) = (closed_by IS NULL)",
            name=op.f("ck_open_addresses_closed_with_whom"),
        ),
        sa.CheckConstraint(
            "NOT closed_by_owner OR closed_at IS NOT NULL",
            name=op.f("ck_open_addresses_owner_closed_it"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_open_addresses")),
    )
    op.create_index(
        "uq_open_addresses_live",
        "open_addresses",
        ["tool_name", "host", "port"],
        unique=True,
        postgresql_where=sa.text("closed_at IS NULL"),
        postgresql_nulls_not_distinct=True,
    )

    op.create_table(
        "service_accounts",
        sa.Column("id", sa.Uuid(), server_default=NEW_UUID, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("person_id", sa.Uuid(), nullable=True),
        sa.Column("ref", sa.Text(), nullable=False),
        sa.Column("hosts", postgresql.JSONB(), nullable=False),
        sa.Column("place", postgresql.JSONB(), nullable=False),
        sa.Column("given_by", sa.Uuid(), nullable=True),
        sa.Column("given_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("removed_by", sa.Text(), nullable=True),
        sa.Column(
            "removed_by_owner", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("for_guests", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("for_domo", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.CheckConstraint(
            "name ~ '^[a-z][a-z0-9_]{1,39}$'", name=op.f("ck_service_accounts_name_format")
        ),
        sa.CheckConstraint(
            "ref ~ '^account\\.[0-9a-f]{32}$'", name=op.f("ck_service_accounts_ref_format")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(hosts) = 'array' AND jsonb_array_length(hosts) >= 1",
            name=op.f("ck_service_accounts_hosts_is_a_list"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(place) = 'object' "
            "AND coalesce(place ->> 'kind', '') IN ('header', 'basic', 'login')",
            name=op.f("ck_service_accounts_place_values"),
        ),
        sa.CheckConstraint(
            f"removed_by IS NULL OR {_who('removed_by')}",
            name=op.f("ck_service_accounts_removed_by_format"),
        ),
        sa.CheckConstraint(
            "(removed_at IS NULL) = (removed_by IS NULL)",
            name=op.f("ck_service_accounts_removed_with_whom"),
        ),
        sa.CheckConstraint(
            "NOT removed_by_owner OR removed_at IS NOT NULL",
            name=op.f("ck_service_accounts_owner_removed_it"),
        ),
        sa.ForeignKeyConstraint(
            ["person_id"],
            ["persons.id"],
            name=op.f("fk_service_accounts_person_id_persons"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["given_by"],
            ["persons.id"],
            name=op.f("fk_service_accounts_given_by_persons"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_service_accounts")),
        sa.UniqueConstraint("ref", name=op.f("uq_service_accounts_ref")),
    )
    op.create_index(
        "uq_service_accounts_live",
        "service_accounts",
        ["person_id", "name"],
        unique=True,
        postgresql_where=sa.text("removed_at IS NULL"),
        postgresql_nulls_not_distinct=True,
    )
    op.create_index("ix_service_accounts_given_by", "service_accounts", ["given_by"])


def downgrade() -> None:
    op.drop_index("ix_service_accounts_given_by", table_name="service_accounts")
    op.drop_index("uq_service_accounts_live", table_name="service_accounts")
    op.drop_table("service_accounts")
    op.drop_index("uq_open_addresses_live", table_name="open_addresses")
    op.drop_table("open_addresses")
