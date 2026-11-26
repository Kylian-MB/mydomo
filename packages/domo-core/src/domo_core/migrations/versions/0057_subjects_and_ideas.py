"""Actions and requests keep their subject, the assistant's reason and who answers.

``message_actions`` gains ``subject_key`` (what the action is about, ``policy.subjects``) and
``idea_reason`` (set when the action was an idea of the assistant's). ``confirmations`` gains the
same two, ``concerned_id`` (the person who alone answers the request), ``refusal_words`` (what
the person said when they refused it), ``expiry_woken_at`` (when its expiry woke the
assistant) and ``in_their_turn`` (a request of his born of the owner's rule in a person's turn,
replayed in that turn: false for every request before it). Going back down drops them (the
assistant is not installed anywhere yet: this is for
a development database).

Revision ID: 0057
Revises: 0056
Create Date: 2026-11-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0057"
down_revision: str | None = "0056"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("message_actions", sa.Column("subject_key", sa.Text(), nullable=True))
    op.add_column("message_actions", sa.Column("idea_reason", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_message_actions_idea_reason_length"),
        "message_actions",
        "char_length(idea_reason) <= 200",
    )
    op.create_index(
        "ix_message_actions_subject_key_created_at",
        "message_actions",
        ["subject_key", sa.text("created_at DESC")],
        postgresql_where=sa.text("subject_key IS NOT NULL"),
    )
    op.add_column("confirmations", sa.Column("subject_key", sa.Text(), nullable=True))
    op.add_column("confirmations", sa.Column("idea_reason", sa.Text(), nullable=True))
    op.add_column("confirmations", sa.Column("concerned_id", sa.Uuid(), nullable=True))
    op.add_column("confirmations", sa.Column("refusal_words", sa.Text(), nullable=True))
    op.add_column(
        "confirmations", sa.Column("expiry_woken_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "confirmations",
        sa.Column("in_their_turn", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.create_foreign_key(
        op.f("fk_confirmations_concerned_id_persons"),
        "confirmations",
        "persons",
        ["concerned_id"],
        ["id"],
    )
    op.create_check_constraint(
        op.f("ck_confirmations_idea_reason_length"),
        "confirmations",
        "char_length(idea_reason) <= 200",
    )
    op.create_check_constraint(
        op.f("ck_confirmations_refusal_words_length"),
        "confirmations",
        "char_length(refusal_words) <= 300",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_confirmations_refusal_words_length"), "confirmations", type_="check"
    )
    op.drop_constraint(op.f("ck_confirmations_idea_reason_length"), "confirmations", type_="check")
    op.drop_constraint(
        op.f("fk_confirmations_concerned_id_persons"), "confirmations", type_="foreignkey"
    )
    for column in (
        "in_their_turn",
        "expiry_woken_at",
        "refusal_words",
        "concerned_id",
        "idea_reason",
        "subject_key",
    ):
        op.drop_column("confirmations", column)
    op.drop_index("ix_message_actions_subject_key_created_at", table_name="message_actions")
    op.drop_constraint(
        op.f("ck_message_actions_idea_reason_length"), "message_actions", type_="check"
    )
    op.drop_column("message_actions", "idea_reason")
    op.drop_column("message_actions", "subject_key")
