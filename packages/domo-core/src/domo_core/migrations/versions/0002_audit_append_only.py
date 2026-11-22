"""Make the audit log append-only at the database level.

A row-level trigger refuses every UPDATE and DELETE on ``audit_log``. This is a guard against
mistakes (a bug, a hasty ``DELETE`` in a console), not a security boundary: the role that owns
the table can still drop or disable the trigger. Someone who gets that far is caught by the hash
chain (``audit.log.verify_audit_chain``), which this trigger complements and does not replace.

TRUNCATE is not covered (a row trigger does not see it): the tests empty the tables with it.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-28
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# No colon followed by a name in the SQL below: ``op.execute`` reads ``:name`` as a bind parameter.
CREATE_FUNCTION = """
CREATE FUNCTION audit_log_reject_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only (% refused)', TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$
"""

CREATE_TRIGGER = """
CREATE TRIGGER audit_log_append_only
BEFORE UPDATE OR DELETE ON audit_log
FOR EACH ROW EXECUTE FUNCTION audit_log_reject_change()
"""


def upgrade() -> None:
    op.execute(CREATE_FUNCTION)
    op.execute(CREATE_TRIGGER)


def downgrade() -> None:
    op.execute("DROP TRIGGER audit_log_append_only ON audit_log")
    op.execute("DROP FUNCTION audit_log_reject_change()")
