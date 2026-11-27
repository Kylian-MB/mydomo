"""Setting, changing and cancelling the plans Domo sets for himself, and which ones are due (spec
6.9).

A plan is a person's: the functions that look plans up for a turn take whose they are, and a plan
of someone else is no plan for them. A plan is data: nothing here reads a right or a rule in it,
and the barrier decides what Domo may do once it is due. Its ``why`` never has the shape of a
secret (``memory.service.refuses_secret_shapes``), nor do the exact words it keeps.

A time is in UTC once stored; a repeating plan is computed in the household's time zone
(``plans.schedule``). A plan that is due again once it fired takes the first time of its series
after the moment it fired: the times it missed while Domo was away are not due one by one (each
counts down its ``COUNT``), and a rule that gives nothing more ends it.

A plan may wait for the build of a tool (``after_build_id``, spec 5.5, 5.8): it is due when that
build ends (``ready`` or ``abandoned``) or when its time comes, whichever is first, and it is served
once. Without a rule, it is done whatever woke it, and the end of the build never wakes it again.
With a rule, the end of its build, once given to Domo, clears what it waited for (its next times
use the tool); a time that came while the build still ran moves it to its next time and it still
waits for the build, but the end of the build no longer wakes it on its own: it was served since it
began waiting, and its next time tells how the build ended.

"Served since it began waiting" is read from ``waits_since``, the moment the plan was pointed at
its build (``make``, ``change``): a plan is served for its build only by a turn that STARTED after
that moment and was settled (``last_fired_at`` is the moment that turn started). The turn in which
Domo points a plan at a build serves nothing of it: the end of that build still wakes it once.

None of the functions commits: the caller owns the transaction (an audit entry commits with the
change it describes). Nothing here knows the web.

The series of a rule is read in a worker thread (``anyio.to_thread``): a plan of every minute that
catches up a year reads half a million times, which would hold every other turn up for a couple of
seconds in the loop.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime
from types import EllipsisType
from typing import Final, cast
from zoneinfo import ZoneInfo

from anyio import to_thread
from sqlalchemy import ColumnElement, and_, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.accounts.sessions import active_at
from domo_core.madetools.models import BuildState, MadeTool, ToolBuild
from domo_core.memory.service import SecretRefused, refuses_secret_shapes
from domo_core.plans.models import Plan
from domo_core.plans.schedule import catch_up, next_after, remaining, valid_repeat, with_count

MAX_WHY: Final = 500
MAX_THEIR_WORDS: Final = 1000
MAX_OUTCOME: Final = 300
# The states of a build that has ended: the plans that wait for it are due.
ENDED: Final = ("ready", "abandoned")


def _check_why(why: str) -> None:
    if not why.strip() or len(why) > MAX_WHY:
        raise ValueError(f"Why a plan is set is 1 to {MAX_WHY} characters.")
    if refuses_secret_shapes(why):
        raise SecretRefused


def _check_their_words(their_words: str | None) -> None:
    if their_words is None:
        return
    if not their_words.strip() or len(their_words) > MAX_THEIR_WORDS:
        raise ValueError(f"The exact words of a plan are 1 to {MAX_THEIR_WORDS} characters.")
    if refuses_secret_shapes(their_words):
        raise SecretRefused


def _check_due(due_at: datetime | None, now: datetime) -> None:
    if due_at is not None and due_at <= now:
        raise ValueError("A plan is due later than now.")


def _first_due(repeat: str, now: datetime, zone: ZoneInfo) -> datetime:
    if not valid_repeat(repeat):
        raise ValueError("Not a rule a plan repeats by.")
    due = next_after(repeat, now, zone)
    if due is None:
        raise ValueError("The rule gives no time to come.")
    return due


async def make(
    session: AsyncSession,
    *,
    person_id: uuid.UUID,
    why: str,
    now: datetime,
    zone: ZoneInfo,
    due_at: datetime | None = None,
    repeat: str | None = None,
    on_return: bool = False,
    their_words: str | None = None,
    made_by_domo: bool = False,
    after_build_id: uuid.UUID | None = None,
) -> Plan:
    """A new plan of ``person_id``, waiting. It needs ``due_at`` (later than ``now``), ``repeat``
    (a rule ``plans.schedule.valid_repeat`` accepts), ``on_return`` or ``after_build_id`` (the
    build of a tool it waits for: the caller checked that the person may wait for it); with
    ``repeat`` and no ``due_at`` it is first due at the rule's first time after ``now``. Raises
    ``SecretRefused`` for a ``why`` or exact words with the shape of a secret and ``ValueError``
    for anything else out of bounds; nothing is written then. Flushed."""
    _check_why(why)
    _check_their_words(their_words)
    if due_at is None and repeat is None and not on_return and after_build_id is None:
        raise ValueError("A plan needs a time, a rule, a return or a build.")
    _check_due(due_at, now)
    if repeat is not None:
        first = await to_thread.run_sync(_first_due, repeat, now, zone)
        if due_at is None:
            due_at = first
    plan = Plan(
        person_id=person_id,
        why=why,
        their_words=their_words,
        due_at=due_at,
        repeat=repeat,
        on_return=on_return,
        after_build_id=after_build_id,
        waits_since=None if after_build_id is None else now,
        state="waiting",
        made_by_domo=made_by_domo,
        created_at=now,
        changed_at=now,
    )
    session.add(plan)
    await session.flush()
    return plan


async def change(
    session: AsyncSession,
    plan: Plan,
    *,
    now: datetime,
    zone: ZoneInfo,
    why: str | None = None,
    due_at: datetime | None = None,
    repeat: str | None | EllipsisType = ...,
    on_return: bool | None = None,
    after_build_id: uuid.UUID | None | EllipsisType = ...,
    forget_their_words: bool = False,
    no_longer_his: bool = False,
) -> Plan:
    """Change what is given of a waiting ``plan``: ``None`` leaves ``why``, ``due_at`` and
    ``on_return`` as they are, and ``...`` leaves ``repeat`` (``None`` stops it repeating: it is
    then due at its next time only) and ``after_build_id`` (``None``: it no longer waits for a
    build; a build given, even the same, waits from ``now``: ``waits_since``). A new rule with no
    ``due_at`` is first due at its first time after ``now``.
    ``forget_their_words`` empties the exact words it kept (a ``why`` that no longer comes from
    them alone). Same refusals as ``make``, and ``ValueError`` for a plan that no longer waits or
    would be left with no time, no rule, no return and no build. Flushed."""
    if plan.state != "waiting":
        raise ValueError("Only a plan that waits is changed.")
    if why is not None:
        _check_why(why)
    _check_due(due_at, now)
    new_repeat = plan.repeat if isinstance(repeat, EllipsisType) else repeat
    new_due = plan.due_at if due_at is None else due_at
    if not isinstance(repeat, EllipsisType) and repeat is not None:
        first = await to_thread.run_sync(_first_due, repeat, now, zone)
        if due_at is None:
            new_due = first
    new_return = plan.on_return if on_return is None else on_return
    new_build = plan.after_build_id if isinstance(after_build_id, EllipsisType) else after_build_id
    if new_due is None and new_repeat is None and not new_return and new_build is None:
        raise ValueError("A plan needs a time, a rule, a return or a build.")
    if why is not None:
        plan.why = why
    if forget_their_words:
        plan.their_words = None
    if no_longer_his:
        plan.made_by_domo = False
    plan.due_at = new_due
    plan.repeat = new_repeat
    plan.on_return = new_return
    if not isinstance(after_build_id, EllipsisType):
        plan.after_build_id = new_build
        plan.waits_since = None if new_build is None else now
    plan.changed_at = now
    await session.flush()
    return plan


async def cancel(session: AsyncSession, plan: Plan, now: datetime) -> None:
    """``plan`` will not be due any more. Flushed."""
    plan.state = "cancelled"
    plan.changed_at = now
    await session.flush()


async def waiting_of(session: AsyncSession, person_id: uuid.UUID, *, limit: int = 10) -> list[Plan]:
    """The plans of ``person_id`` that wait, ``limit`` at most, the soonest first (those due only
    at their return last, the oldest first)."""
    found = await session.scalars(
        select(Plan)
        .where(Plan.person_id == person_id, Plan.state == "waiting")
        .order_by(Plan.due_at.asc().nulls_last(), Plan.created_at, Plan.id)
        .limit(limit)
    )
    return list(found.all())


async def waiting_plan_of(
    session: AsyncSession, person_id: uuid.UUID, plan_id: uuid.UUID
) -> Plan | None:
    """The plan ``plan_id`` when it is ``person_id``'s and waits; ``None`` otherwise (someone
    else's plan is none of theirs)."""
    plan = await session.get(Plan, plan_id)
    if plan is None or plan.person_id != person_id or plan.state != "waiting":
        return None
    return plan


def _of_people_here(now: datetime) -> ColumnElement[bool]:
    """The plans of people who are in the household at ``now``: the plans of someone removed, or of
    a guest whose stay ended, are never due (they stay as they are, waiting, should they come
    back)."""
    return Plan.person_id.in_(select(Person.id).where(active_at(now)))


def _woken_by_their_build() -> ColumnElement[bool]:
    """The plans whose build ended (``ENDED``) and that it still wakes: all but a repeating plan
    served since it began waiting, by a turn that started after ``waits_since`` (see the module
    docstring)."""
    served = and_(
        Plan.repeat.is_not(None),
        Plan.last_fired_at.is_not(None),
        Plan.last_fired_at > Plan.waits_since,
    )
    ended = select(ToolBuild.id).where(ToolBuild.state.in_(ENDED))
    return and_(Plan.after_build_id.in_(ended), not_(served))


async def due(session: AsyncSession, now: datetime) -> list[Plan]:
    """The plans that wait and whose time has come (``due_at`` at or before ``now``) or whose
    build ended (``_woken_by_their_build``), the soonest first (those woken by their build alone
    after them), of people who are in the household at ``now``. It only reads: a plan stays due
    until it is settled (``fired``), and a second look, or a restart, loses nothing."""
    found = await session.scalars(
        select(Plan)
        .where(
            Plan.state == "waiting",
            or_(Plan.due_at <= now, _woken_by_their_build()),
            _of_people_here(now),
        )
        .order_by(Plan.due_at, Plan.id)
    )
    return list(found.all())


async def builds_waited_on(
    session: AsyncSession, plans: Sequence[Plan]
) -> dict[uuid.UUID, tuple[BuildState, str]]:
    """The builds ``plans`` wait for, by id: each one's state and the name of its tool (the name
    of the tool it made, once ``ready``; the name it builds, otherwise). A build that ended ready
    but whose tool is no longer ready (removed, set aside) is told ``abandoned``: no tool of that
    name can be used."""
    ids = {plan.after_build_id for plan in plans if plan.after_build_id is not None}
    if not ids:
        return {}
    found = await session.execute(
        select(ToolBuild.id, ToolBuild.state, ToolBuild.name, MadeTool.name, MadeTool.state)
        .outerjoin(MadeTool, MadeTool.id == ToolBuild.tool_id)
        .where(ToolBuild.id.in_(ids))
    )
    named: dict[uuid.UUID, tuple[BuildState, str]] = {}
    for build_id, state, name, made, made_state in found.all():
        # The tool it made, once ready (the outer join gives none for a build without one).
        tool = cast("str | None", made)
        if state == "ready" and (tool is None or made_state != "ready"):
            named[build_id] = ("abandoned", name)
        else:
            named[build_id] = (state, tool if state == "ready" and tool is not None else name)
    return named


async def time_served(
    session: AsyncSession, plan: Plan, *, started: datetime, now: datetime, zone: ZoneInfo
) -> None:
    """``plan``, which Domo changed in the turn it was due in (started at ``started``), still
    waits with a time he left at or before that start: that time was served, as ``fired`` would
    have it (a repeating plan goes to its next time, at ``now``; one that does not repeat no longer
    has a time, and is done when nothing else is left to wait for). Without it, a plan he only
    pointed at a build in its turn would be due again at once. Flushed."""
    if plan.state != "waiting" or plan.due_at is None or plan.due_at > started:
        return
    upcoming, repeat = await to_thread.run_sync(after_firing, plan.repeat, plan.due_at, now, zone)
    plan.due_at = upcoming
    plan.repeat = None if upcoming is None else repeat
    if upcoming is None and not plan.on_return and plan.after_build_id is None:
        plan.state = "done"
    await session.flush()


async def served_by_its_build(session: AsyncSession, plan: Plan, started: datetime) -> None:
    """``plan`` was due in a turn that started at ``started`` and was settled: when it already
    waited for its build then (``waits_since`` before ``started``) and that build has ended, that
    end was given to Domo and it no longer waits for it (a plan left with nothing else to wait
    for is done). A plan pointed at its build during that turn keeps waiting for it. Flushed."""
    if plan.after_build_id is None or plan.waits_since is None or plan.waits_since >= started:
        return
    state = await session.scalar(select(ToolBuild.state).where(ToolBuild.id == plan.after_build_id))
    if state not in ENDED:
        return
    plan.after_build_id = None
    plan.waits_since = None
    if plan.due_at is None and plan.repeat is None and not plan.on_return:
        plan.state = "done"
    await session.flush()


async def due_on_return(session: AsyncSession, person_id: uuid.UUID, now: datetime) -> list[Plan]:
    """The plans of ``person_id`` that wait for their return, the oldest first; none when they are
    no longer in the household at ``now``."""
    found = await session.scalars(
        select(Plan)
        .where(
            Plan.person_id == person_id,
            Plan.state == "waiting",
            Plan.on_return.is_(True),
            _of_people_here(now),
        )
        .order_by(Plan.created_at, Plan.id)
    )
    return list(found.all())


async def fired(
    session: AsyncSession,
    plan: Plan,
    *,
    now: datetime,
    zone: ZoneInfo,
    outcome: str,
    started: datetime | None = None,
) -> None:
    """``plan`` was due at ``now``, and ``outcome`` (1 to ``MAX_OUTCOME`` characters, Domo's
    words: never a secret) is what came of it. A plan that does not repeat is done; one that
    repeats is due at the first time of its series after ``now`` (the series goes on from the
    time it was due), the times it missed counting down its ``COUNT``, and is done once the rule
    gives nothing more. A repeating plan that fires before its time (the person came back first,
    ``on_return``, or the end of its build) keeps its time and its rule: the return was served,
    the time is still to come. A plan given the end of its build no longer waits for it
    (``served_by_its_build``). ``started`` is when the turn it was due in started (``now`` when
    not given): it is the moment it was last due (``last_fired_at``). Flushed."""
    if not outcome.strip() or len(outcome) > MAX_OUTCOME:
        raise ValueError(f"What came of a plan is 1 to {MAX_OUTCOME} characters.")
    if refuses_secret_shapes(outcome):
        raise SecretRefused
    due_since = now if started is None else started
    plan.last_fired_at = due_since
    plan.last_outcome = outcome
    plan.changed_at = now
    await served_by_its_build(session, plan, due_since)
    if plan.repeat is not None and plan.due_at is not None and now < plan.due_at:
        await session.flush()
        return
    upcoming, repeat = await to_thread.run_sync(
        after_firing, plan.repeat, plan.due_at or now, now, zone
    )
    if upcoming is None:
        plan.state = "done"
    else:
        plan.repeat = repeat
    plan.due_at = upcoming
    await session.flush()


def after_firing(
    repeat: str | None, since: datetime, now: datetime, zone: ZoneInfo
) -> tuple[datetime | None, str | None]:
    """When a plan that repeats by ``repeat``, due at ``since``, is due next once it fired at
    ``now``, and its rule then (its ``COUNT`` counted down); ``None`` when it is over. A trigger of
    a tool goes on the same way (``madetools.triggers``). It reads the series: call it in a worker
    thread."""
    if repeat is None:
        return None, None
    # The times missed while Domo was away are not due again one by one.
    upcoming, missed = catch_up(repeat, since=since, now=now, zone=zone)
    count = remaining(repeat)
    if count is not None:
        count -= 1 + missed
        if count <= 0:
            return None, None
    if upcoming is None:
        return None, None
    return upcoming, repeat if count is None else with_count(repeat, count)
