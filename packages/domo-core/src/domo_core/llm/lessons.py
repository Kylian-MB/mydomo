"""What Domo learns from the calls that went wrong: a model did a kind of task badly, how often
lately, and why the last time (``model_lessons``, one row per kind of key, model and kind of task).

A failure is recorded (``record_failure``):

* by the code, when it sees it: a round of the conversation whose every tool call could not be
  acted on (``INVALID_TOOL_CALL``, ``llm.picker.ModelPicker.retry_up``), a yes judge whose answer
  is no verdict (``UNREADABLE_VERDICT``, ``confirmations.judge``);
* by Domo, when he judges it (a mistake the person points out, or one he sees), with the tool of
  his choices (``models_choose``, ``failed_task`` and ``failed_reason``), with a reason of a
  closed list (``FAILURE_REASONS``).

A reason is always one of ``ALL_REASONS``, never words of Domo's own: lessons are shown in the
context of every person of the household, and nothing said in one person's conversation reaches
another's that way (as for the words of a note, D4). ``record_failure`` refuses any other reason.

The failures are counted over the last ``LESSON_DAYS`` days: a failure that comes more than that
after the one before counts from one again (a row keeps no more than its count and its last
failure). The most telling lessons of the last days (``recent_lessons``: the most failures first,
``LESSONS_SHOWN`` at most) are shown in Domo's context, as data, so that he takes them into
account in his choices. They never change the level of a model: for the protected tasks, only the
project's table and the owner's word count (``llm.choice``).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import case, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.llm.models import ModelLesson
from domo_core.llm.tasks import TASKS, TaskKind

# How many lessons Domo's context shows, and over how many days the failures count.
LESSONS_SHOWN: Final = 3
LESSON_DAYS: Final = 7
# The reasons the code gives for the failures it sees itself.
INVALID_TOOL_CALL: Final = "invalid tool call"
UNREADABLE_VERDICT: Final = "unreadable verdict"
# The reasons Domo may give for a failure he judges (``models_choose``).
FAILURE_REASONS: Final[tuple[str, ...]] = (
    "wrong tool",
    "wrong answer",
    "misunderstood the request",
    "bad format",
    "too long",
    "too slow",
)
# Every reason a lesson may hold.
ALL_REASONS: Final[frozenset[str]] = frozenset(
    {*FAILURE_REASONS, INVALID_TOOL_CALL, UNREADABLE_VERDICT}
)


@dataclass(frozen=True)
class Lesson:
    """``model_id`` did ``task`` badly ``failures`` times lately, the last time for
    ``last_reason``."""

    model_id: str
    task: TaskKind
    failures: int
    last_reason: str


def _task_of(value: str) -> TaskKind | None:
    for task in TASKS:
        if task == value:
            return task
    return None


async def record_failure(
    session: AsyncSession,
    *,
    provider_kind: str,
    model_id: str,
    task: TaskKind,
    reason: str,
    now: datetime,
) -> None:
    """One more failure of ``model_id`` (on a key of ``provider_kind``) at ``task``, at ``now``,
    for ``reason``, one of ``ALL_REASONS`` (``ValueError`` otherwise), in the caller's
    transaction (the caller commits). It counts from one again when the last one is more than
    ``LESSON_DAYS`` old."""
    if reason not in ALL_REASONS:
        raise ValueError("a lesson's reason is one of the list")
    since = now - timedelta(days=LESSON_DAYS)
    statement = insert(ModelLesson).values(
        provider_kind=provider_kind,
        model_id=model_id,
        task=task,
        failures=1,
        last_failure_at=now,
        last_reason=reason,
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[ModelLesson.provider_kind, ModelLesson.model_id, ModelLesson.task],
            set_={
                "failures": case(
                    (ModelLesson.last_failure_at >= since, ModelLesson.failures + 1), else_=1
                ),
                "last_failure_at": now,
                "last_reason": reason,
            },
        )
    )


async def recent_lessons(session: AsyncSession, *, now: datetime) -> list[Lesson]:
    """The lessons of the last ``LESSON_DAYS`` days, the most failures first (then the latest),
    ``LESSONS_SHOWN`` at most. A row whose reason is of no list (none is stored so) is left."""
    rows = await session.execute(
        select(ModelLesson)
        .where(
            ModelLesson.last_failure_at >= now - timedelta(days=LESSON_DAYS),
            ModelLesson.last_reason.in_(sorted(ALL_REASONS)),
        )
        .order_by(
            ModelLesson.failures.desc(),
            ModelLesson.last_failure_at.desc(),
            ModelLesson.model_id,
            ModelLesson.task,
        )
        .limit(LESSONS_SHOWN)
    )
    lessons: list[Lesson] = []
    for row in rows.scalars():
        task = _task_of(row.task)
        if task is not None:
            lessons.append(Lesson(row.model_id, task, row.failures, row.last_reason))
    return lessons
