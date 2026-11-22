"""The means of proof a person has agreed."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, func, text
from sqlalchemy.orm import Mapped, mapped_column

from domo_core.db import Base


class ProofMean(Base):
    """One means of proof a person has agreed: a ``kind`` the code knows how to verify
    (``proofs.kinds.PROOF_KINDS``), proposed by the ``code`` (the password every account has, the
    passkey a person adds), by ``domo`` or by the ``person``, and agreed at ``agreed_at``.

    A means that is taken away keeps its row, with ``removed_at``; a person has at most one live
    row of each kind, and agreeing again after a removal is a new row.
    """

    __tablename__ = "proof_means"
    __table_args__ = (
        CheckConstraint("proposed_by IN ('code', 'domo', 'person')", name="proposed_by_values"),
        Index(
            "uq_proof_means_person_id_kind_live",
            "person_id",
            "kind",
            unique=True,
            postgresql_where=text("removed_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    person_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("persons.id", ondelete="CASCADE"))
    kind: Mapped[str]
    proposed_by: Mapped[str]
    agreed_at: Mapped[datetime]
    removed_at: Mapped[datetime | None]
