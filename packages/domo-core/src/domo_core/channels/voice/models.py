"""The tables of the voice (spec 6.5, 6.10, 6.11): voice references, their prints, moments.

A voice reference (``voice_refs``) is ours and opaque (``v-`` and 16 hexadecimal digits): it exists
from the first sentence of a voice nobody knows, and ``person_id`` is the link to a person (set
through ``channels.voice.people.claim_reference``, which stands in for the people's records until
they are merged). A print (``voice_prints``) is a vector and
never sound: by reference, by microphone and by print model (a print of one model is never
compared to a print of another), of a kind: ``base`` (moves slowly), ``recent`` (follows the voice),
``passing`` (a cold: dies at ``expires_at``) or ``candidate`` (not learnt yet: ``utterances`` and
``visits`` count what supports it). A row also keeps the habits of the voice's pitch and rate (a
mean and a spread) when they were measured. A moment (``voice_moments``) is a short reserved time
in which a person's next words mean something particular (a phrase being agreed or changed; the
``stop_listening`` purpose is no longer used: a stop acts at once).
"""

import uuid
from datetime import datetime
from typing import Final, Literal

from sqlalchemy import CheckConstraint, Float, ForeignKey, Index, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from domo_core.db import Base

PrintKind = Literal["base", "recent", "passing", "candidate"]
MomentPurpose = Literal["phrase_agree", "phrase_change", "stop_listening"]

REFERENCE_PREFIX: Final = "v-"
# The most dimensions a print may have (speaker embeddings have a few hundred).
MAX_DIMENSIONS: Final = 2048


class VoiceRef(Base):
    """A voice reference (see the module docstring); ``warned_at`` is when the voice was told it
    is listened to; ``relearn_at`` when the assistant erased what was learnt of this person's
    voice on his own idea: until it is learnt again (``prints.learn``), a voice taken for them is
    not sure for what is sensitive (``prints.Reading.to_relearn``, Task 27 ruling R)."""

    __tablename__ = "voice_refs"
    __table_args__ = (
        CheckConstraint("char_length(reference) BETWEEN 1 AND 200", name="reference_length"),
        Index("ix_voice_refs_person_id", "person_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    reference: Mapped[str] = mapped_column(unique=True)
    person_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("persons.id", ondelete="CASCADE")
    )
    warned_at: Mapped[datetime | None]
    relearn_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())


class VoicePrint(Base):
    """A print of a voice (see the module docstring). ``first_visit_at`` and ``last_at`` are when
    it was begun and last moved; ``visits`` counts the visits it was heard on; ``learnt_at`` is
    when a base or recent print was made from its candidate (so that "that was not me" can take
    it back). On a base print, ``heard_at`` is when the person was last heard on its microphone
    (whatever was learnt) and ``moved_in_visit`` whether the base already moved in the visit that
    time belongs to: a base moves at most once a visit. On a candidate, ``misses`` counts the
    utterances in a row that did not resemble it (it starts again at ``RESTART_AFTER_UNLIKE``)."""

    __tablename__ = "voice_prints"
    __table_args__ = (
        CheckConstraint("char_length(microphone) BETWEEN 1 AND 80", name="microphone_length"),
        CheckConstraint("char_length(model) BETWEEN 1 AND 200", name="model_length"),
        CheckConstraint("kind IN ('base', 'recent', 'passing', 'candidate')", name="kind_values"),
        CheckConstraint(
            f"array_length(vector, 1) BETWEEN 1 AND {MAX_DIMENSIONS}", name="vector_size"
        ),
        CheckConstraint("utterances >= 0 AND visits >= 0", name="counts_not_negative"),
        CheckConstraint("misses >= 0", name="misses_not_negative"),
        UniqueConstraint("ref_id", "microphone", "kind", "model"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    ref_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("voice_refs.id", ondelete="CASCADE"))
    microphone: Mapped[str]
    kind: Mapped[str]
    model: Mapped[str]
    vector: Mapped[list[float]] = mapped_column(ARRAY(Float(24)))
    utterances: Mapped[int] = mapped_column(server_default=text("1"), default=1)
    visits: Mapped[int] = mapped_column(server_default=text("1"), default=1)
    first_visit_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())
    last_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())
    expires_at: Mapped[datetime | None]
    learnt_at: Mapped[datetime | None]
    pitch_mean: Mapped[float | None]
    pitch_spread: Mapped[float | None]
    rate_mean: Mapped[float | None]
    rate_spread: Mapped[float | None]
    heard_at: Mapped[datetime | None]
    moved_in_visit: Mapped[bool] = mapped_column(server_default=text("false"), default=False)
    misses: Mapped[int] = mapped_column(server_default=text("0"), default=0)


class VoiceMoment(Base):
    """A reserved moment of ``person_id`` (see the module docstring), over at ``expires_at``."""

    __tablename__ = "voice_moments"
    __table_args__ = (
        CheckConstraint(
            "purpose IN ('phrase_agree', 'phrase_change', 'stop_listening')",
            name="purpose_values",
        ),
        CheckConstraint(
            "microphone IS NULL OR char_length(microphone) BETWEEN 1 AND 80",
            name="microphone_length",
        ),
        Index("ix_voice_moments_person_id", "person_id", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    person_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("persons.id", ondelete="CASCADE"))
    microphone: Mapped[str | None]
    purpose: Mapped[str]
    step: Mapped[int] = mapped_column(server_default=text("0"), default=0)
    expires_at: Mapped[datetime]
