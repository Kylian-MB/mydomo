"""The audit log: an append-only, hash-chained record of what happened."""

from datetime import datetime

from sqlalchemy import CHAR, BigInteger, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from domo_core.db import Base


class AuditEntry(Base):
    """One audit entry. ``hash`` covers ``prev_hash`` and the entry's content (the rules are in
    ``audit.log``), so that altering or removing an entry breaks the chain from there on.

    ``details`` holds metadata only: never a secret, a password or a message's content.
    """

    __tablename__ = "audit_log"

    # A bigserial: the order of the chain.
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())
    actor: Mapped[str]
    action: Mapped[str]
    details: Mapped[dict[str, object]] = mapped_column(JSONB)
    prev_hash: Mapped[str] = mapped_column(CHAR(64))
    hash: Mapped[str] = mapped_column(CHAR(64), unique=True)
