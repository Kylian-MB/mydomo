"""The tables of the people of the house: roles the assistant makes, links, promises, directives.

``roles`` and ``role_accesses`` (the roles the assistant makes, never under a built-in name, and
what each gives or denies), ``person_links`` ("from is relation to"), ``access_promises``,
``owner_directives`` (one in force per kind and person), ``record_merges``, ``person_acts`` (the
reason of each act, and who did it) and ``person_channel_refs`` (an opaque reference a channel
owns). Every column that names a person points at ``persons.id``; ``persons.made_role_id`` gets
its foreign key to ``roles`` here. See ``people.models``. Going back down drops all of it (the
assistant is not installed anywhere yet: this is for a development database).

Revision ID: 0071
Revises: 0070
Create Date: 2026-11-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0071"
down_revision: str | None = "0070"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_UUID = sa.text("gen_random_uuid()")
NOW = sa.text("clock_timestamp()")
_UUID = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


def _author(column: str) -> str:
    """``column`` says ``domo`` or ``person:<uuid>`` (``people.models.author_rule``)."""
    return f"{column} = 'domo' OR {column} ~ '^person:{_UUID}$'"


def _ref(
    table: str, column: str, target: str, ondelete: str | None = None
) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        [column],
        [f"{target}.id"],
        name=op.f(f"fk_{table}_{column}_{target}"),
        ondelete=ondelete,
    )


def upgrade() -> None:
    op.create_table(
        "roles",
        sa.Column("id", sa.Uuid(), server_default=NEW_UUID, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("name_key", sa.Text(), nullable=False),
        sa.Column("base", sa.String(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("char_length(name) BETWEEN 1 AND 40", name=op.f("ck_roles_name_length")),
        sa.CheckConstraint(
            "base IN ('adult', 'child', 'guest')", name=op.f("ck_roles_base_values")
        ),
        sa.CheckConstraint(
            "name_key NOT IN ('owner', 'adult', 'child', 'guest')",
            name=op.f("ck_roles_name_free"),
        ),
        sa.CheckConstraint(_author("created_by"), name=op.f("ck_roles_created_by_format")),
        sa.CheckConstraint(
            "char_length(reason) BETWEEN 1 AND 300", name=op.f("ck_roles_reason_length")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_roles")),
    )
    op.create_index(
        "uq_roles_name_key",
        "roles",
        ["name_key"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_foreign_key(
        op.f("fk_persons_made_role_id_roles"), "persons", "roles", ["made_role_id"], ["id"]
    )

    op.create_table(
        "role_accesses",
        sa.Column("role_id", sa.Uuid(), nullable=False),
        sa.Column("action_type", sa.Text(), nullable=False),
        sa.Column("access", sa.String(), nullable=False),
        sa.CheckConstraint(
            "char_length(action_type) BETWEEN 1 AND 100",
            name=op.f("ck_role_accesses_action_type_length"),
        ),
        sa.CheckConstraint(
            "access IN ('allow', 'deny')", name=op.f("ck_role_accesses_access_values")
        ),
        _ref("role_accesses", "role_id", "roles", "CASCADE"),
        sa.PrimaryKeyConstraint("role_id", "action_type", name=op.f("pk_role_accesses")),
    )

    op.create_table(
        "person_links",
        sa.Column("id", sa.Uuid(), server_default=NEW_UUID, nullable=False),
        sa.Column("from_id", sa.Uuid(), nullable=False),
        sa.Column("to_id", sa.Uuid(), nullable=False),
        sa.Column("relation", sa.Text(), nullable=False),
        sa.Column("said_by_id", sa.Uuid(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint("from_id <> to_id", name=op.f("ck_person_links_two_records")),
        sa.CheckConstraint(
            "char_length(relation) BETWEEN 1 AND 60",
            name=op.f("ck_person_links_relation_length"),
        ),
        _ref("person_links", "from_id", "persons", "CASCADE"),
        _ref("person_links", "to_id", "persons", "CASCADE"),
        _ref("person_links", "said_by_id", "persons", "SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_person_links")),
    )
    op.create_index(
        "uq_person_links_told_once", "person_links", ["from_id", "to_id", "relation"], unique=True
    )

    op.create_table(
        "access_promises",
        sa.Column("id", sa.Uuid(), server_default=NEW_UUID, nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("made_role_id", sa.Uuid(), nullable=True),
        sa.Column("standing", sa.Text(), nullable=False),
        sa.Column("guest_days", sa.Integer(), nullable=True),
        sa.Column("asked_by", sa.Text(), nullable=False),
        sa.Column("is_order", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("kept_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("kept_person_id", sa.Uuid(), nullable=True),
        sa.Column("dropped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dropped_by", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "char_length(description) BETWEEN 1 AND 300",
            name=op.f("ck_access_promises_description_length"),
        ),
        sa.CheckConstraint(
            "role IN ('adult', 'child', 'guest')", name=op.f("ck_access_promises_role_values")
        ),
        sa.CheckConstraint(
            "standing IN ('lives_here', 'visiting', 'elsewhere')",
            name=op.f("ck_access_promises_standing_values"),
        ),
        sa.CheckConstraint(
            "guest_days IS NULL OR guest_days > 0",
            name=op.f("ck_access_promises_guest_days_positive"),
        ),
        sa.CheckConstraint(_author("asked_by"), name=op.f("ck_access_promises_asked_by_format")),
        sa.CheckConstraint(
            "char_length(reason) BETWEEN 1 AND 300",
            name=op.f("ck_access_promises_reason_length"),
        ),
        sa.CheckConstraint(
            f"dropped_by IS NULL OR {_author('dropped_by')}",
            name=op.f("ck_access_promises_dropped_by_format"),
        ),
        sa.CheckConstraint(
            "(dropped_at IS NULL) = (dropped_by IS NULL)",
            name=op.f("ck_access_promises_dropped_by_whom"),
        ),
        _ref("access_promises", "made_role_id", "roles"),
        _ref("access_promises", "kept_person_id", "persons", "SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_access_promises")),
    )

    op.create_table(
        "owner_directives",
        sa.Column("id", sa.Uuid(), server_default=NEW_UUID, nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("person_id", sa.Uuid(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("except_words", sa.Text(), nullable=True),
        sa.Column("until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("set_by", sa.Uuid(), nullable=False),
        sa.Column("set_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("lifted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lifted_by", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "kind IN ('no_new_people', 'no_keeping', 'withdraw', 'refuse')",
            name=op.f("ck_owner_directives_kind_values"),
        ),
        sa.CheckConstraint(
            "(kind IN ('withdraw', 'refuse')) = (person_id IS NOT NULL)",
            name=op.f("ck_owner_directives_person_for_kind"),
        ),
        sa.CheckConstraint(
            "char_length(description) <= 300",
            name=op.f("ck_owner_directives_description_length"),
        ),
        sa.CheckConstraint(
            "char_length(except_words) <= 300",
            name=op.f("ck_owner_directives_except_words_length"),
        ),
        _ref("owner_directives", "person_id", "persons"),
        _ref("owner_directives", "set_by", "persons"),
        _ref("owner_directives", "lifted_by", "persons"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_owner_directives")),
    )
    op.create_index(
        "uq_owner_directives_in_force",
        "owner_directives",
        ["kind", "person_id"],
        unique=True,
        postgresql_where=sa.text("lifted_at IS NULL"),
        postgresql_nulls_not_distinct=True,
    )

    op.create_table(
        "record_merges",
        sa.Column("id", sa.Uuid(), server_default=NEW_UUID, nullable=False),
        sa.Column("kept_id", sa.Uuid(), nullable=False),
        sa.Column("absorbed_id", sa.Uuid(), nullable=False),
        sa.Column("moved", postgresql.JSONB(), nullable=False),
        sa.Column("by", sa.Text(), nullable=False),
        sa.Column("merged_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("split_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kept_id <> absorbed_id", name=op.f("ck_record_merges_two_records")),
        sa.CheckConstraint(_author('"by"'), name=op.f("ck_record_merges_by_format")),
        _ref("record_merges", "kept_id", "persons"),
        _ref("record_merges", "absorbed_id", "persons"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_record_merges")),
    )

    op.create_table(
        "person_acts",
        sa.Column("id", sa.Uuid(), server_default=NEW_UUID, nullable=False),
        sa.Column("act", sa.String(), nullable=False),
        sa.Column("person_id", sa.Uuid(), nullable=True),
        sa.Column("other_id", sa.Uuid(), nullable=True),
        sa.Column("role_id", sa.Uuid(), nullable=True),
        sa.Column("promise_id", sa.Uuid(), nullable=True),
        sa.Column("directive_id", sa.Uuid(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("by", sa.Text(), nullable=False),
        sa.Column("turn_of", sa.Uuid(), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("owed_to_owner", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("told_owner_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "char_length(act) BETWEEN 1 AND 40", name=op.f("ck_person_acts_act_length")
        ),
        sa.CheckConstraint("char_length(reason) <= 300", name=op.f("ck_person_acts_reason_length")),
        sa.CheckConstraint(_author('"by"'), name=op.f("ck_person_acts_by_format")),
        _ref("person_acts", "person_id", "persons", "SET NULL"),
        _ref("person_acts", "other_id", "persons", "SET NULL"),
        _ref("person_acts", "role_id", "roles", "SET NULL"),
        _ref("person_acts", "promise_id", "access_promises", "SET NULL"),
        _ref("person_acts", "directive_id", "owner_directives", "SET NULL"),
        _ref("person_acts", "turn_of", "persons", "SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_person_acts")),
    )

    op.create_table(
        "person_channel_refs",
        sa.Column("id", sa.Uuid(), server_default=NEW_UUID, nullable=False),
        sa.Column("person_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("reference", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint(
            "char_length(channel) BETWEEN 1 AND 40",
            name=op.f("ck_person_channel_refs_channel_length"),
        ),
        sa.CheckConstraint(
            "char_length(reference) BETWEEN 1 AND 200",
            name=op.f("ck_person_channel_refs_reference_length"),
        ),
        _ref("person_channel_refs", "person_id", "persons", "CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_person_channel_refs")),
    )
    op.create_index(
        "uq_person_channel_refs_reference",
        "person_channel_refs",
        ["channel", "reference"],
        unique=True,
    )
    op.create_index("ix_person_channel_refs_person_id", "person_channel_refs", ["person_id"])


def downgrade() -> None:
    op.drop_table("person_channel_refs")
    op.drop_table("person_acts")
    op.drop_table("record_merges")
    op.drop_table("owner_directives")
    op.drop_table("access_promises")
    op.drop_table("person_links")
    op.drop_table("role_accesses")
    op.drop_constraint(op.f("fk_persons_made_role_id_roles"), "persons", type_="foreignkey")
    op.drop_table("roles")
