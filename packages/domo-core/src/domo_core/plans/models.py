"""The plans Domo sets for himself."""

import uuid
from datetime import datetime
from typing import Literal

from sqlalchemy import CheckConstraint, ForeignKey, Index, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from domo_core.db import Base

State = Literal["waiting", "done", "cancelled"]


class Plan(Base):
    """Something Domo planned to do, think of or say, for one person (spec 6.9).

    A plan is always a person's (``person_id``): the one who asked for it, or the one Domo
    planned it for; their keys pay for the turn it starts, their rights apply then, and it goes
    with their row. ``why`` is what Domo wrote for himself; ``their_words`` the exact passage of
    the person's message it comes from, checked by the code, when it comes from a request.
    ``made_by_domo`` says that nobody asked for it.

    It is due at ``due_at`` (UTC), when the person comes back (``on_return``), when the build of a
    tool it waits for ends (``after_build_id``; nothing deletes a build a plan waits on, and
    ``waits_since`` is when it began waiting for it: both or neither), or at whichever comes
    first. ``repeat`` is an iCalendar rule (``plans.schedule``) computed in the
    household's time zone: a repeating plan that waits always has its next ``due_at``. A waiting
    plan always has a time, a return or a build to wait for (``needs_a_moment``). ``last_fired_at``
    and ``last_outcome`` say when it was last due and what came of it, in a few words of Domo."""

    __tablename__ = "plans"
    __table_args__ = (
        CheckConstraint("char_length(why) BETWEEN 1 AND 500", name="why_length"),
        CheckConstraint(
            "their_words IS NULL OR char_length(their_words) BETWEEN 1 AND 1000",
            name="their_words_length",
        ),
        CheckConstraint(
            "repeat IS NULL OR char_length(repeat) BETWEEN 1 AND 200", name="repeat_length"
        ),
        CheckConstraint(
            "last_outcome IS NULL OR char_length(last_outcome) BETWEEN 1 AND 300",
            name="last_outcome_length",
        ),
        CheckConstraint("state IN ('waiting', 'done', 'cancelled')", name="state_values"),
        CheckConstraint(
            "state <> 'waiting' OR due_at IS NOT NULL OR on_return OR after_build_id IS NOT NULL",
            name="needs_a_moment",
        ),
        CheckConstraint(
            "(after_build_id IS NULL) = (waits_since IS NULL)", name="waits_since_with_build"
        ),
        # "This person's plans" (their context, their tools).
        Index("ix_plans_person_id", "person_id"),
        # "What is due now" (the plans that wait, by their time).
        Index("ix_plans_due_at", "due_at", postgresql_where=text("state = 'waiting'")),
        # "The plans waiting for this build" (the wake at the end of a build).
        Index(
            "ix_plans_after_build_id", "after_build_id", postgresql_where=text("state = 'waiting'")
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    person_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("persons.id", ondelete="CASCADE"))
    why: Mapped[str]
    their_words: Mapped[str | None]
    due_at: Mapped[datetime | None]
    repeat: Mapped[str | None]
    on_return: Mapped[bool] = mapped_column(server_default=text("false"))
    after_build_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tool_builds.id", ondelete="RESTRICT")
    )
    waits_since: Mapped[datetime | None]
    state: Mapped[State] = mapped_column(Text(), server_default=text("'waiting'"))
    made_by_domo: Mapped[bool] = mapped_column(server_default=text("false"))
    last_fired_at: Mapped[datetime | None]
    last_outcome: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())
    changed_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())
