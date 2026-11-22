"""No request says ``always`` as who asked its yes.

``confirmations.asked_by`` could say ``always``: the yes some types asked by default. Nothing asks
one by default any more (2026-11-15: what a person asks and Domo understood is done), and no code
writes that value: it leaves the values the column accepts. A request that still holds it is said
to be asked by a rule of the owner (``rule``): that default was what the owner had decided. Going
back down accepts the value again, and gives it back to no request (Domo is not installed
anywhere yet: this is for a development database).

Revision ID: 0040
Revises: 0039
Create Date: 2026-11-15
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0040"
down_revision: str | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_REQUESTS = "confirmations"
_ASKED_BY = "ck_confirmations_asked_by_values"


def upgrade() -> None:
    op.execute("UPDATE confirmations SET asked_by = 'rule' WHERE asked_by = 'always'")
    op.drop_constraint(op.f(_ASKED_BY), _REQUESTS, type_="check")
    op.create_check_constraint(
        op.f(_ASKED_BY), _REQUESTS, "asked_by IN ('rule', 'judge', 'domo', 'approver')"
    )


def downgrade() -> None:
    op.drop_constraint(op.f(_ASKED_BY), _REQUESTS, type_="check")
    op.create_check_constraint(
        op.f(_ASKED_BY),
        _REQUESTS,
        "asked_by IN ('always', 'rule', 'judge', 'domo', 'approver')",
    )
