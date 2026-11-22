"""The texts Domo wrote for the people who read them, and the heads of the cards."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, UniqueConstraint, func
from sqlalchemy import text as sql
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from domo_core.db import Base


class WrittenText(Base):
    """The text Domo wrote for one key of the catalog of meanings, for one person
    (``person_id``) or for a language (``language``, the version of the house), never both.

    ``meaning_hash`` is the hash of the meaning the text was written for: when the meaning
    changes, the text is no longer read. ``previous`` keeps the texts it replaced, newest first,
    as ``{"text", "meaning_hash", "written_at"}`` (at most ``texts.store.KEPT_VERSIONS``).
    """

    __tablename__ = "written_texts"
    __table_args__ = (
        CheckConstraint("(person_id IS NULL) <> (language IS NULL)", name="one_reader"),
        Index(
            "uq_written_texts_person_id_key",
            "person_id",
            "key",
            unique=True,
            postgresql_where=sql("person_id IS NOT NULL"),
        ),
        Index(
            "uq_written_texts_language_key",
            "language",
            "key",
            unique=True,
            postgresql_where=sql("language IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    person_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("persons.id", ondelete="CASCADE")
    )
    language: Mapped[str | None]
    key: Mapped[str]
    text: Mapped[str]
    meaning_hash: Mapped[str]
    model_id: Mapped[str]
    written_at: Mapped[datetime]
    previous: Mapped[list[dict[str, str]]] = mapped_column(JSONB, server_default=sql("'[]'::jsonb"))


class CardText(Base):
    """The head of a card (what it says above the action) written for one reader: the card of a
    request (``confirmation_id``) or of an action (``message_action_id``), one head per card and
    reader.

    ``facts_hash`` is the hash of what it was written from (``texts.cards.head_fingerprint``):
    it is shown only while its card says the same thing to its reader. ``''`` matches nothing."""

    __tablename__ = "card_texts"
    __table_args__ = (
        CheckConstraint(
            "(confirmation_id IS NULL) <> (message_action_id IS NULL)", name="one_card"
        ),
        UniqueConstraint(
            "confirmation_id",
            "message_action_id",
            "reader_id",
            name="uq_card_texts_card_reader",
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    confirmation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("confirmations.id", ondelete="CASCADE")
    )
    message_action_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("message_actions.id", ondelete="CASCADE")
    )
    reader_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("persons.id"))
    head: Mapped[str]
    model_id: Mapped[str]
    written_at: Mapped[datetime]
    facts_hash: Mapped[str] = mapped_column(server_default=sql("''"))
