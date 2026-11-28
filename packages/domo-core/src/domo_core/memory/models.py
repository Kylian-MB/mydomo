"""The memories Domo keeps."""

import uuid
from datetime import datetime
from typing import Any, Final, Literal

from pgvector.sqlalchemy import Vector
from sqlalchemy import CheckConstraint, Computed, Dialect, ForeignKey, Index, Text, func, text
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from domo_core.db import Base

Kind = Literal["fact", "preference", "moment", "lesson", "self"]
Origin = Literal["own_words", "others_words", "deduced", "told_by_domo", "external", "unsure_voice"]

# The size of a memory's vector: the local model's (``memory.embedder``).
EMBEDDING_DIMENSIONS: Final = 384


class Embedding(TypeDecorator[list[float]]):
    """pgvector's ``vector``, read as a list of numbers (pgvector itself gives a numpy array)."""

    impl = Vector
    cache_ok = True

    def process_result_value(self, value: Any, dialect: Dialect) -> list[float] | None:
        return None if value is None else [float(number) for number in value]


class Memory(Base):
    """Something Domo keeps, in one or two sentences he wrote himself.

    ``said_by_id`` is the person in whose turn it was born and ``about_id`` the person of the
    household it is about, when it is about one; both become ``None`` when that person's row is
    deleted (their ``kept_private`` memories are deleted with them, by a trigger of the database:
    a person who only left keeps everything, Domo judges what he keeps). ``subject`` is a free
    word or two (a name, "me", "the boiler"). ``kept_private`` is Domo's own mark, "I keep it
    to myself": such a memory is never put under his eyes in another person's turn of itself.
    ``core`` memories are always present in the turn. ``origin`` is fixed by the code from the turn
    that made the memory, never by the model (``unsure_voice``: kept from a sentence of a voice the
    house was not sure was this person's, probably theirs: never their own words);
    ``action_type`` names the action Domo told the person about, for ``told_by_domo`` only.
    ``by_domo`` says the assistant himself wrote it (``said_by_id`` may then be empty).
    A lesson (``kind``) may say whom it is for (``for_everyone``) and what it is about
    (``lesson_action_type``, ``lesson_subject_key``); what he told a person keeps the answer they
    gave (``told_answer``) and whether they agreed to be asked again
    (``told_keep_asking``). ``words_checked`` says the code checked, word for word,
    that its content is itself a passage of the person's own message (rulings R73, R79):
    only such a memory may count as an adult's agreement for a child (``may_agree``, ruling R73).
    ``served_at`` is the last time it was put under his
    eyes. ``confirmed_at`` is when the person it is about confirmed it, for a memory about someone
    of the household that someone else said (``memory.service.unconfirmed``: their word, not a
    fact, until then). ``search`` is what the words are searched in (``simple`` configuration: no
    language is assumed). ``embedding`` is what its meaning is searched by, computed outside the
    turn by the local model named in ``embedding_model`` (``memory.search.embed_missing``): both
    are empty until then, and again once its subject or its content changes; a memory the model
    failed on keeps an empty ``embedding`` and a mark in ``embedding_model``
    (``memory.search.failed_mark``). Neither is loaded with the row unless asked for."""

    __tablename__ = "memories"
    __table_args__ = (
        CheckConstraint("char_length(subject) BETWEEN 1 AND 120", name="subject_length"),
        CheckConstraint("char_length(content) BETWEEN 1 AND 600", name="content_length"),
        CheckConstraint(
            "kind IN ('fact', 'preference', 'moment', 'lesson', 'self')", name="kind_values"
        ),
        CheckConstraint(
            "origin IN ('own_words', 'others_words', 'deduced', 'told_by_domo', 'external', "
            "'unsure_voice')",
            name="origin_values",
        ),
        CheckConstraint(
            "action_type IS NULL OR origin = 'told_by_domo'", name="action_type_needs_origin"
        ),
        CheckConstraint(
            "kind = 'lesson' OR (lesson_action_type IS NULL AND lesson_subject_key IS NULL "
            "AND NOT for_everyone)",
            name="lesson_fields_need_a_lesson",
        ),
        CheckConstraint("told_answer IN ('yes', 'no')", name="told_answer_values"),
        CheckConstraint(
            "origin = 'told_by_domo' OR (told_answer IS NULL AND told_keep_asking IS NULL)",
            name="told_fields_need_origin",
        ),
        # "The lessons about this type of action".
        Index(
            "ix_memories_lesson_action_type",
            "lesson_action_type",
            postgresql_where=text("lesson_action_type IS NOT NULL"),
        ),
        # "The memories this person said" (what they see, what goes with them).
        Index("ix_memories_said_by_id", "said_by_id"),
        # "The core" (present in every turn).
        Index("ix_memories_core", "core"),
        Index("ix_memories_search", "search", postgresql_using="gin"),
        Index(
            "ix_memories_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    said_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("persons.id", ondelete="SET NULL")
    )
    about_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("persons.id", ondelete="SET NULL")
    )
    subject: Mapped[str]
    content: Mapped[str]
    kind: Mapped[Kind] = mapped_column(Text())
    origin: Mapped[Origin] = mapped_column(Text())
    kept_private: Mapped[bool] = mapped_column(server_default=text("false"))
    core: Mapped[bool] = mapped_column(server_default=text("false"))
    action_type: Mapped[str | None]
    by_domo: Mapped[bool] = mapped_column(server_default=text("false"))
    for_everyone: Mapped[bool] = mapped_column(server_default=text("false"))
    lesson_action_type: Mapped[str | None]
    lesson_subject_key: Mapped[str | None]
    told_answer: Mapped[Literal["yes", "no"] | None] = mapped_column(Text())
    told_keep_asking: Mapped[bool | None]
    words_checked: Mapped[bool] = mapped_column(server_default=text("false"), default=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())
    changed_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())
    served_at: Mapped[datetime | None]
    confirmed_at: Mapped[datetime | None]
    search: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('simple', subject || ' ' || content)", persisted=True),
        deferred=True,
    )
    embedding: Mapped[list[float] | None] = mapped_column(
        Embedding(EMBEDDING_DIMENSIONS), deferred=True
    )
    embedding_model: Mapped[str | None] = mapped_column(deferred=True)
