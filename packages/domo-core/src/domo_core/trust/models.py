"""The facts Domo notes about what a person's word has been worth."""

import uuid
from datetime import datetime
from typing import Literal

from sqlalchemy import CheckConstraint, ForeignKey, Index, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from domo_core.db import Base

# What the person's word was worth: what they said turned out true (``held``), false in good
# faith (``mistaken``), or false and they knew it (``lied``). Domo chooses, as he understands it.
Outcome = Literal["held", "mistaken", "lied"]
# How it is known: the person it was about confirmed or denied it in a turn of theirs
# (``confirmed``), Domo saw it himself through his tools (``seen``), or someone told him
# (``reported``: their word too, to be judged).
Established = Literal["confirmed", "seen", "reported"]


class TrustFact(Base):
    """One thing ``person_id``'s word turned out to be worth (spec 6.1), in Domo's own words
    (``about``, never a secret: ``trust.service.note``).

    ``memory_id`` is the memory it comes from, when there is one; ``noted_for_id`` the person in
    whose turn it was noted (one ``confirmed`` fact per memory at most); ``reported_by_id`` who
    reported it, for a ``reported`` fact only. The
    three become ``None`` when what they name is deleted; the fact goes with ``person_id``'s row. A
    fact is data, never a right, a rule or a limit: nothing of the policy, the confirmations or
    the spending reads it."""

    __tablename__ = "trust_facts"
    __table_args__ = (
        CheckConstraint("outcome IN ('held', 'mistaken', 'lied')", name="outcome_values"),
        CheckConstraint(
            "established IN ('confirmed', 'seen', 'reported')", name="established_values"
        ),
        CheckConstraint("char_length(about) BETWEEN 1 AND 200", name="about_length"),
        CheckConstraint(
            "reported_by_id IS NULL OR established = 'reported'", name="reporter_needs_reported"
        ),
        # "The facts of this person, the newest first" (the score and the tool).
        Index("ix_trust_facts_person_id_created_at", "person_id", "created_at"),
        # One confirmed fact per memory: the same word is never counted twice (migration 0052).
        Index(
            "uq_trust_facts_confirmed_memory_id",
            "memory_id",
            unique=True,
            postgresql_where=text("established = 'confirmed'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    person_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("persons.id", ondelete="CASCADE"))
    outcome: Mapped[Outcome] = mapped_column(Text())
    established: Mapped[Established] = mapped_column(Text())
    about: Mapped[str]
    memory_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("memories.id", ondelete="SET NULL")
    )
    noted_for_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("persons.id", ondelete="SET NULL")
    )
    reported_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("persons.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime]
