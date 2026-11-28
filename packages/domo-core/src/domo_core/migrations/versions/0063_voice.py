"""The voice: references, prints and reserved moments (see ``channels.voice.models``).

``voice_refs`` (a reference of ours, opaque, and the person it stands for when there is one),
``voice_prints`` (vectors, never sound, by reference, microphone, kind and print model, with the
habits of pitch and rate when measured, and on a base when the person was last heard and whether it
moved in that visit) and ``voice_moments`` (a short reserved time of a person).
Going back down drops the three (Domo is not installed anywhere yet: this is for a development
database).

Revision ID: 0063
Revises: 0062
Create Date: 2026-11-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0063"
down_revision: str | None = "0062"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_UUID = sa.text("gen_random_uuid()")
NOW = sa.text("clock_timestamp()")


def upgrade() -> None:
    op.create_table(
        "voice_refs",
        sa.Column("id", sa.Uuid(), server_default=NEW_UUID, nullable=False),
        sa.Column("reference", sa.Text(), nullable=False),
        sa.Column("person_id", sa.Uuid(), nullable=True),
        sa.Column("warned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint(
            "char_length(reference) BETWEEN 1 AND 200",
            name=op.f("ck_voice_refs_reference_length"),
        ),
        sa.ForeignKeyConstraint(
            ["person_id"],
            ["persons.id"],
            name=op.f("fk_voice_refs_person_id_persons"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_voice_refs")),
        sa.UniqueConstraint("reference", name=op.f("uq_voice_refs_reference")),
    )
    op.create_index("ix_voice_refs_person_id", "voice_refs", ["person_id"])

    op.create_table(
        "voice_prints",
        sa.Column("id", sa.Uuid(), server_default=NEW_UUID, nullable=False),
        sa.Column("ref_id", sa.Uuid(), nullable=False),
        sa.Column("microphone", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("vector", postgresql.ARRAY(sa.Float(24)), nullable=False),
        sa.Column("utterances", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("visits", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("first_visit_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("last_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("learnt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pitch_mean", sa.Float(), nullable=True),
        sa.Column("pitch_spread", sa.Float(), nullable=True),
        sa.Column("rate_mean", sa.Float(), nullable=True),
        sa.Column("rate_spread", sa.Float(), nullable=True),
        sa.Column("heard_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("moved_in_visit", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.CheckConstraint(
            "char_length(microphone) BETWEEN 1 AND 80",
            name=op.f("ck_voice_prints_microphone_length"),
        ),
        sa.CheckConstraint(
            "char_length(model) BETWEEN 1 AND 200", name=op.f("ck_voice_prints_model_length")
        ),
        sa.CheckConstraint(
            "kind IN ('base', 'recent', 'passing', 'candidate')",
            name=op.f("ck_voice_prints_kind_values"),
        ),
        sa.CheckConstraint(
            "array_length(vector, 1) BETWEEN 1 AND 2048", name=op.f("ck_voice_prints_vector_size")
        ),
        sa.CheckConstraint(
            "utterances >= 0 AND visits >= 0", name=op.f("ck_voice_prints_counts_not_negative")
        ),
        sa.ForeignKeyConstraint(
            ["ref_id"],
            ["voice_refs.id"],
            name=op.f("fk_voice_prints_ref_id_voice_refs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_voice_prints")),
        sa.UniqueConstraint(
            "ref_id", "microphone", "kind", "model", name=op.f("uq_voice_prints_ref_id")
        ),
    )

    op.create_table(
        "voice_moments",
        sa.Column("id", sa.Uuid(), server_default=NEW_UUID, nullable=False),
        sa.Column("person_id", sa.Uuid(), nullable=False),
        sa.Column("microphone", sa.Text(), nullable=True),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("step", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "purpose IN ('phrase_agree', 'phrase_change', 'stop_listening')",
            name=op.f("ck_voice_moments_purpose_values"),
        ),
        sa.CheckConstraint(
            "microphone IS NULL OR char_length(microphone) BETWEEN 1 AND 80",
            name=op.f("ck_voice_moments_microphone_length"),
        ),
        sa.ForeignKeyConstraint(
            ["person_id"],
            ["persons.id"],
            name=op.f("fk_voice_moments_person_id_persons"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_voice_moments")),
    )
    op.create_index("ix_voice_moments_person_id", "voice_moments", ["person_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_voice_moments_person_id", table_name="voice_moments")
    op.drop_table("voice_moments")
    op.drop_table("voice_prints")
    op.drop_index("ix_voice_refs_person_id", table_name="voice_refs")
    op.drop_table("voice_refs")
