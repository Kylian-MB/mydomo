"""A request keeps what is asked, who asked it and why; the levels and the styles are gone.

The yes-judge decides whether a yes is asked before an action, and how (``confirmations.judge``):

* ``confirmations.ask``: what the request asks for (``yes``, ``explicit_yes`` or ``proof``),
  ``asked_by``: who asked it (``always``, the yes the owner decided by default; ``rule``, a rule
  of his; ``judge``; ``domo``; ``approver``, someone else's agreement the rights ask for), and
  ``ask_why``: why, in one line of 200 characters at most. All three empty for a chain the judge
  let through that keeps a request for the upkeep. ``decided_via`` may now say ``proof`` (a proof
  that it is the person) and ``judge`` (nobody said yes: the judge let it go);
* the levels go: ``confirmations.level``, ``chain_level``, ``confirmation_steps.level`` and the
  level of a right (``policy_rights.level``, which a rule for everyone no longer has to leave
  empty); so do Domo's explicit check of before (``explicit_required``, ``explicit_reason``:
  it is an ``ask`` of ``domo`` now) and the styles the owner chose
  (``policy_settings`` keys ``confirmation_style.*``);
* the history the judge reads counts a person's requests of a type: an index on
  ``(requester_id, action->>'action_type')``.

Domo is installed nowhere yet: the requests that wait are expired (by their lifetime), nothing
is converted.

Going back down gives the columns back (the level of a request ``confirm``, no explicit check),
drops the new ones and the index, and turns a decision by ``proof`` or ``judge`` into a
``button``.

Revision ID: 0025
Revises: 0024
Create Date: 2026-11-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_REQUESTS = "confirmations"
_STEPS = "confirmation_steps"
_RIGHTS = "policy_rights"
_INDEX = "ix_confirmations_requester_id_action_type"
_VIAS_BEFORE = "'button', 'explicit_phrase', 'phrase', 'classifier', 'change'"
_EVERYONE_BEFORE = (
    "person_id IS NOT NULL OR role IS NOT NULL OR (access IS NULL AND approver IS NULL "
    "AND limit_expression IS NULL AND level IS NULL)"
)
_EVERYONE_NOW = (
    "person_id IS NOT NULL OR role IS NOT NULL OR (access IS NULL AND approver IS NULL "
    "AND limit_expression IS NULL)"
)
_LEVELS = "'free', 'confirm', 'always_confirm'"


def upgrade() -> None:
    op.execute(
        "UPDATE confirmations SET status = 'expired', expired_by = 'lifetime' "
        "WHERE status = 'pending'"
    )
    op.execute("DELETE FROM policy_settings WHERE key LIKE 'confirmation_style.%'")

    op.drop_constraint(op.f("ck_confirmations_level_values"), _REQUESTS, type_="check")
    op.drop_constraint(op.f("ck_confirmations_chain_level_values"), _REQUESTS, type_="check")
    op.drop_constraint(op.f("ck_confirmations_explicit_reason_length"), _REQUESTS, type_="check")
    op.drop_column(_REQUESTS, "level")
    op.drop_column(_REQUESTS, "chain_level")
    op.drop_column(_REQUESTS, "explicit_required")
    op.drop_column(_REQUESTS, "explicit_reason")
    op.add_column(_REQUESTS, sa.Column("ask", sa.Text(), nullable=True))
    op.add_column(_REQUESTS, sa.Column("asked_by", sa.Text(), nullable=True))
    op.add_column(_REQUESTS, sa.Column("ask_why", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_confirmations_ask_values"), _REQUESTS, "ask IN ('yes', 'explicit_yes', 'proof')"
    )
    op.create_check_constraint(
        op.f("ck_confirmations_asked_by_values"),
        _REQUESTS,
        "asked_by IN ('always', 'rule', 'judge', 'domo', 'approver')",
    )
    op.create_check_constraint(
        op.f("ck_confirmations_ask_why_length"), _REQUESTS, "char_length(ask_why) <= 200"
    )
    op.drop_constraint(op.f("ck_confirmations_decided_via_values"), _REQUESTS, type_="check")
    op.create_check_constraint(
        op.f("ck_confirmations_decided_via_values"),
        _REQUESTS,
        f"decided_via IN ({_VIAS_BEFORE}, 'proof', 'judge')",
    )
    op.create_index(_INDEX, _REQUESTS, ["requester_id", sa.text("(action->>'action_type')")])

    op.drop_constraint(op.f("ck_confirmation_steps_level_values"), _STEPS, type_="check")
    op.drop_column(_STEPS, "level")

    op.drop_constraint(op.f("ck_policy_rights_everyone_only_asks"), _RIGHTS, type_="check")
    op.drop_constraint(op.f("ck_policy_rights_level_values"), _RIGHTS, type_="check")
    op.drop_column(_RIGHTS, "level")
    op.create_check_constraint(op.f("ck_policy_rights_everyone_only_asks"), _RIGHTS, _EVERYONE_NOW)


def downgrade() -> None:
    op.drop_constraint(op.f("ck_policy_rights_everyone_only_asks"), _RIGHTS, type_="check")
    op.add_column(_RIGHTS, sa.Column("level", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_policy_rights_level_values"), _RIGHTS, f"level IN ({_LEVELS})"
    )
    op.create_check_constraint(
        op.f("ck_policy_rights_everyone_only_asks"), _RIGHTS, _EVERYONE_BEFORE
    )

    op.add_column(_STEPS, sa.Column("level", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_confirmation_steps_level_values"), _STEPS, f"level IN ({_LEVELS})"
    )

    op.drop_index(_INDEX, table_name=_REQUESTS)
    op.execute(
        "UPDATE confirmations SET decided_via = 'button' WHERE decided_via IN ('proof', 'judge')"
    )
    op.drop_constraint(op.f("ck_confirmations_decided_via_values"), _REQUESTS, type_="check")
    op.create_check_constraint(
        op.f("ck_confirmations_decided_via_values"), _REQUESTS, f"decided_via IN ({_VIAS_BEFORE})"
    )
    op.drop_constraint(op.f("ck_confirmations_ask_why_length"), _REQUESTS, type_="check")
    op.drop_constraint(op.f("ck_confirmations_asked_by_values"), _REQUESTS, type_="check")
    op.drop_constraint(op.f("ck_confirmations_ask_values"), _REQUESTS, type_="check")
    op.drop_column(_REQUESTS, "ask_why")
    op.drop_column(_REQUESTS, "asked_by")
    op.drop_column(_REQUESTS, "ask")
    # Every request comes back at ``confirm``, with no explicit check; the default only fills the
    # rows, and goes (the schema of before had none).
    op.add_column(
        _REQUESTS,
        sa.Column("level", sa.Text(), server_default=sa.text("'confirm'"), nullable=False),
    )
    op.alter_column(_REQUESTS, "level", server_default=None)
    op.add_column(_REQUESTS, sa.Column("chain_level", sa.Text(), nullable=True))
    op.add_column(
        _REQUESTS,
        sa.Column(
            "explicit_required", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
    )
    op.add_column(_REQUESTS, sa.Column("explicit_reason", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_confirmations_level_values"), _REQUESTS, "level IN ('confirm', 'always_confirm')"
    )
    op.create_check_constraint(
        op.f("ck_confirmations_chain_level_values"), _REQUESTS, f"chain_level IN ({_LEVELS})"
    )
    op.create_check_constraint(
        op.f("ck_confirmations_explicit_reason_length"),
        _REQUESTS,
        "char_length(explicit_reason) <= 200",
    )
