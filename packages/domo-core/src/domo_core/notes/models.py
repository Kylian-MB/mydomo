"""Notes people leave for one another through Domo."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, func, text
from sqlalchemy.orm import Mapped, mapped_column

from domo_core.db import Base

# A note names exactly one author: a person, or Domo himself.
ONE_AUTHOR = "(author_id IS NULL) = by_domo"


class Note(Base):
    """A short note from one person to another, that the recipient's Domo tells them in his own
    words (``tools.notes``, spec 4.8) unless its author takes it back first.

    ``seen_at`` is the first time the recipient's Domo told it or showed its exact words
    (``None`` until then), ``told_at`` the first time he told it, ``withdrawn_at`` when its author
    took it back: from then on it is neither told nor shown.

    ``text`` is what Domo noted (the ``text`` of ``note_leave``), the words his context gives
    the recipient's Domo to tell; ``exact_text`` is the passage of the message the author wrote
    to Domo in the turn the note was left that is the note, as they typed it, taken by the code
    from that message (``ToolContext.said``) once Domo said which passage (``exact``), never
    Domo's copy: ``None`` when he gave none, it is no passage of the message, that turn had no
    message of theirs (a wake, a yes given later) or it was beyond a note's bounds. The card of
    ``note_show`` shows it, and the recipient's Domo reads it as data in his context
    (``prompts.NoteToTell.exact``); no other model, no audit entry and no log has it, and it lives
    and goes with the note.

    ``keep_until`` is the end of a day (UTC) until which the author keeps a note left for a guest
    though the guest leaves (``tools.notes.NoteKeep``, spec 5.10): ``None`` for a note that is not
    kept, which is erased when its guest has left (``notes.upkeep``).

    A note is a person's (``author_id``) or Domo's own (``by_domo``, the owner, 2026-11-21:
    left in a turn he acts in on his own, or in someone's turn as his own word,
    ``tools.notes.NoteLeave``), exactly one of them: a note of his has no person for author, and
    whoever reads it reads that it is his, never someone else's."""

    __tablename__ = "notes"
    __table_args__ = (
        CheckConstraint("char_length(text) BETWEEN 1 AND 1000", name="text_length"),
        CheckConstraint("char_length(exact_text) BETWEEN 1 AND 1000", name="exact_text_length"),
        # A person's, or Domo's own: exactly one author.
        CheckConstraint(ONE_AUTHOR, name="one_author"),
        # "The notes of this person" (the context of each of their turns).
        Index("ix_notes_recipient_id", "recipient_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    author_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("persons.id"))
    by_domo: Mapped[bool] = mapped_column(server_default=text("false"), default=False)
    recipient_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("persons.id"))
    text: Mapped[str]
    # The exact words: the passage of the author's message that is the note, as they wrote it
    # (``None``: none kept).
    exact_text: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())
    seen_at: Mapped[datetime | None]
    withdrawn_at: Mapped[datetime | None]
    keep_until: Mapped[datetime | None]
    # When Domo told the recipient about the note (``None``: not told).
    told_at: Mapped[datetime | None]
