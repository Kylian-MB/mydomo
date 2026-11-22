"""The devices entrusted to Domo, and the codes that entrust them."""

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, func
from sqlalchemy.orm import Mapped, mapped_column

from domo_core.db import Base


class Device(Base):
    """A device a person entrusted to Domo. Only the SHA-256 of its secret is kept
    (``secret_hash``): a leak of the database leaks no working secret. ``name`` is what the person
    called it when they paired it. A device that is taken back (or forgotten, when its person
    leaves the household) keeps its row, with ``removed_at``."""

    __tablename__ = "devices"
    __table_args__ = (
        # "The devices of this person" (listing them, forgetting them all).
        Index("ix_devices_person_id", "person_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    person_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("persons.id", ondelete="CASCADE"))
    name: Mapped[str]
    secret_hash: Mapped[str] = mapped_column(unique=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())
    last_seen_at: Mapped[datetime | None]
    removed_at: Mapped[datetime | None]


class DevicePairing(Base):
    """A code made to entrust a device: valid until ``expires_at``, served once (``used_at``).
    Only the SHA-256 of the code is kept (``code_hash``)."""

    __tablename__ = "device_pairings"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    person_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("persons.id", ondelete="CASCADE"))
    code_hash: Mapped[str] = mapped_column(unique=True)
    expires_at: Mapped[datetime]
    used_at: Mapped[datetime | None]
