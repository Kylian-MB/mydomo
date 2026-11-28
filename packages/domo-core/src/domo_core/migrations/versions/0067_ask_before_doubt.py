"""A request keeps what it asked before a doubted yes raised it.

``confirmations`` gains ``ask_before_doubt`` (empty: never raised so): what the request asked
before the judge raised it because he doubted who said yes in words; a later yes the code is sure
of is held to it (final fix wave, case 10). Going back down drops it (Domo is not installed
anywhere yet: this is for a development database).

Revision ID: 0067
Revises: 0066
Create Date: 2026-11-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0067"
down_revision: str | None = "0066"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHECK = "ask_before_doubt_values"


def upgrade() -> None:
    op.add_column("confirmations", sa.Column("ask_before_doubt", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f(f"ck_confirmations_{_CHECK}"),
        "confirmations",
        "ask_before_doubt IN ('yes', 'explicit_yes', 'proof')",
    )


def downgrade() -> None:
    op.drop_constraint(op.f(f"ck_confirmations_{_CHECK}"), "confirmations", type_="check")
    op.drop_column("confirmations", "ask_before_doubt")
