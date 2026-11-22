"""What reached whom of Domo's replies."""

import uuid
from datetime import datetime
from typing import Literal

from sqlalchemy import CheckConstraint, ForeignKey, Index, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from domo_core.db import Base

# How a reply reached someone: streamed as it was said (``live``), or come to get later
# (``fetched``: a page or a program asked for the conversation).
How = Literal["live", "fetched"]
# How much of it: all of it (``whole``), or only what came before the reader left (``cut``).
Extent = Literal["whole", "cut"]


class Delivery(Base):
    """One reply of Domo (``message_id``) that reached ``person_id`` on a channel (``channel``:
    the name of the channel that read it, ``page``, ``api``...) and, for a channel of a device, on
    that device (``device_id``; none for the page). ``how`` it came there, how much of it
    (``extent``), and when (``at``).

    One row per reply, channel, device and way (``one_per_way``, a page has no device): a reply
    cut, then read whole, is whole from then on, and is never cut again. It goes with the reply and
    with the person; a device taken back leaves the row without it."""

    __tablename__ = "deliveries"
    __table_args__ = (
        CheckConstraint("how IN ('live', 'fetched')", name="how_values"),
        CheckConstraint("extent IN ('whole', 'cut')", name="extent_values"),
        CheckConstraint("char_length(channel) BETWEEN 1 AND 40", name="channel_length"),
        UniqueConstraint(
            "message_id",
            "channel",
            "device_id",
            "how",
            name="uq_deliveries_one_per_way",
            postgresql_nulls_not_distinct=True,
        ),
        # "What reached this person, lately" (``deliveries.service.received_of``, ``reached``).
        Index("ix_deliveries_person_id_at", "person_id", "at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    message_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"))
    person_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("persons.id", ondelete="CASCADE"))
    channel: Mapped[str]
    device_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("devices.id", ondelete="SET NULL")
    )
    how: Mapped[str]
    extent: Mapped[str]
    at: Mapped[datetime]
