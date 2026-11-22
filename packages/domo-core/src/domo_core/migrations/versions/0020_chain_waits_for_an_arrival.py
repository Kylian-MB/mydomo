"""A step of a chain may wait for someone's arrival, and the chain pauses until then (spec 4.3b).

* ``confirmation_steps.after_arrival_of``: the name of the invitation whose arrival the step waits
  for (one an earlier step of the chain makes, or one that already waited when the chain was asked
  for), 60 characters at most; ``NULL`` for a step that waits for nobody;
* ``confirmations.waiting_for_invitation_id``: the invitation a paused chain waits for (the one of
  its wait of now: a chain with two waits waits for each in turn);
* ``confirmations.status`` may be ``waiting_arrival``: the chain did its steps up to a wait, and
  the upkeep resumes it once the invitation is accepted, or stops it when it ends.

Going back down expires (by Domo) the paused chains, which no code of before resumes, and the
chains with a wait still to come that a yes or a continuation could run (``pending``, or ``failed``
at the wait: the code of before would run the step that waits at once, without the wait); then it
drops the columns and gives the states back as they were.

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATES = "'pending', 'executing', 'executed', 'refused', 'expired', 'failed', 'replaced'"


def upgrade() -> None:
    op.add_column("confirmation_steps", sa.Column("after_arrival_of", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_confirmation_steps_after_arrival_of_length"),
        "confirmation_steps",
        "char_length(after_arrival_of) <= 60",
    )
    op.add_column("confirmations", sa.Column("waiting_for_invitation_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_confirmations_waiting_for_invitation_id_invitations"),
        "confirmations",
        "invitations",
        ["waiting_for_invitation_id"],
        ["id"],
    )
    op.drop_constraint(op.f("ck_confirmations_status_values"), "confirmations", type_="check")
    op.create_check_constraint(
        op.f("ck_confirmations_status_values"),
        "confirmations",
        f"status IN ({_STATES}, 'waiting_arrival')",
    )


def downgrade() -> None:
    op.execute(
        "UPDATE confirmations SET status = 'expired', expired_by = 'domo' "
        "WHERE status = 'waiting_arrival' OR (status IN ('pending', 'failed') AND id IN ("
        "SELECT confirmation_id FROM confirmation_steps "
        "WHERE after_arrival_of IS NOT NULL AND status <> 'done'))"
    )
    op.drop_constraint(op.f("ck_confirmations_status_values"), "confirmations", type_="check")
    op.create_check_constraint(
        op.f("ck_confirmations_status_values"), "confirmations", f"status IN ({_STATES})"
    )
    op.drop_constraint(
        op.f("fk_confirmations_waiting_for_invitation_id_invitations"),
        "confirmations",
        type_="foreignkey",
    )
    op.drop_column("confirmations", "waiting_for_invitation_id")
    op.drop_constraint(
        op.f("ck_confirmation_steps_after_arrival_of_length"),
        "confirmation_steps",
        type_="check",
    )
    op.drop_column("confirmation_steps", "after_arrival_of")
