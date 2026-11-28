"""The clues Domo keeps about who speaks."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, Float, ForeignKey, Index, func
from sqlalchemy.orm import Mapped, mapped_column

from domo_core.db import Base


class IdentityClue(Base):
    """One clue kept for a person: a proof they gave (``kind`` ``proof``, ``means`` its kind) or
    Domo's reading of how they spoke (``kind`` ``manner``, from ``domo``), given on a ``channel``
    and, when the channel has devices, one ``device_id``, and, when a microphone heard it, that
    ``microphone``.

    It weighs ``for`` or ``against`` the person being the one who speaks, ``strength`` ``strong``
    or ``weak``, and comes from the ``code`` or from ``domo``; a measured one has a ``degree``
    (0 to 1). It was made ``at`` a time and is no clue any more from ``expires_at``.
    """

    __tablename__ = "identity_clues"
    __table_args__ = (
        CheckConstraint("weighs IN ('for', 'against')", name="weighs_values"),
        CheckConstraint("strength IN ('strong', 'weak')", name="strength_values"),
        CheckConstraint("source IN ('code', 'domo')", name="source_values"),
        CheckConstraint("degree IS NULL OR (degree >= 0 AND degree <= 1)", name="degree_range"),
        Index("ix_identity_clues_person_id_channel", "person_id", "channel"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    person_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("persons.id", ondelete="CASCADE"))
    kind: Mapped[str]
    weighs: Mapped[str]
    strength: Mapped[str]
    source: Mapped[str]
    channel: Mapped[str]
    device_id: Mapped[uuid.UUID | None]
    means: Mapped[str | None]
    microphone: Mapped[str | None]
    degree: Mapped[float | None] = mapped_column(Float(24))
    at: Mapped[datetime]
    expires_at: Mapped[datetime]
