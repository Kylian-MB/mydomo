"""A passkey challenge may be one of a proof that it is the person.

A request the yes-judge (or a rule of the owner) asks a proof for is answered on its card, by the
person's passkey or their password (``confirmations.proof``). The passkey's ceremony keeps its
challenge in ``webauthn_challenges`` like a registration and a login do: ``purpose`` may now be
``proof`` too (bound to the account of the person who proves it).

Going back down deletes the challenges of a proof (they live five minutes, and the schema of before
has no place for them) and gives the constraint back.

Revision ID: 0026
Revises: 0025
Create Date: 2026-11-07
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHALLENGES = "webauthn_challenges"
_CONSTRAINT = "ck_webauthn_challenges_purpose_values"


def upgrade() -> None:
    op.drop_constraint(op.f(_CONSTRAINT), _CHALLENGES, type_="check")
    op.create_check_constraint(
        op.f(_CONSTRAINT), _CHALLENGES, "purpose IN ('register', 'login', 'proof')"
    )


def downgrade() -> None:
    op.execute("DELETE FROM webauthn_challenges WHERE purpose = 'proof'")
    op.drop_constraint(op.f(_CONSTRAINT), _CHALLENGES, type_="check")
    op.create_check_constraint(op.f(_CONSTRAINT), _CHALLENGES, "purpose IN ('register', 'login')")
