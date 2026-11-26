"""The facts of the moment, which a limit reads (spec 5.5): fixed by the code, read-only, worked
out when the barrier decides.

* ``actor``: ``{"id": "person:<uuid>", "role", "name"}``, who asks (their display name now); for
  Domo on his own (``roles.domo_actor``), ``{"id": "domo", "role": "adult", "name":
  "Domo"}``;
* ``action``: ``{"type", "target", "facts"}``, the described action and its facts (written by the
  tool, ``DescribedAction.facts``);
* ``now``: ``{"hour", "minute", "weekday" (1 is Monday ... 7 Sunday), "date": "YYYY-MM-DD"}``, in
  the household's time zone, and only when the owner has set it (``household.time_zone``): an hour
  is never guessed, and a limit that reads it without one fails (closed);
* ``household``: ``{"people": {role: how many are in the household}, "invitations": {role: how
  many open invitations}}``, with every role (``0`` when there is none);
* ``spent``: ``{"day", "week", "month"}``, what the programs of Domo's tools declared spending
  for the subject (a person, or the house for Domo on his own), in whole cents of euros, over the
  current calendar day, ISO week and month in the household's zone (UTC when it is not set:
  ``madetools.yes.spent_by``). It is apart from what models cost;
* ``history``: ``{"day", "week", "month"}``, how many actions of this type the person did over
  the last 24 hours, 7 days and 30 days. Done means it ran and ended ``done``: an action the
  barrier allowed at once (its ``message_actions`` row), or a request that a yes ran and that
  ended ``executed`` (counted at the yes, once: the row a yes in words leaves under the reply that
  took it is that request's, and is not counted again). A call of a tool of Domo's whose launch
  only stopped for a yes did not do the tool's work and is not counted, whichever of the two it
  is (its card says so: ``STOPPED_FOR_A_YES``): one request counts once, when a launch of it
  ends, however many stops it went through. A request that is being carried out
  under a yes already given can be left out of the count (``not_counting``, with its steps for a
  chain): a limit is then read as it stood at the yes, when that request was not counted yet
  either. A chain of actions
  (``tools.chain``, spec 4.3) counts each of its steps that was done, as an action of the step's
  type: the steps of a chain that waited for a yes by their rows (``confirmation_steps``, when each
  ended; a new request that replaces a chain holds its steps done, copied, and only they count),
  and those of a chain that ran at once by the types its card lists (``"did"``, under the
  reply, when it ended): the rest of a chain continued that ran at once is stored as such a chain
  (``ToolOutcome.recorded_as``). The steps a chain that runs at once has done while it still runs
  are not recorded yet: its runner gives their types (``unrecorded``), and each counts one more.
  What Domo did as his own (``by_domo``: his own acts, and the calls and requests of a turn he
  acted in on his own, in someone's conversation) is never in that person's history. Domo on
  his own has no conversation nor request: his history is what the barrier allowed him
  of this type, from the audit log, but the calls that carried a yes (``"yes": true`` among the
  action's facts, ``madetools.offered``): one request counts once, at its first call, however
  many stops it went through (a request that stopped and was never answered counts too, which a
  person's does not: the audit does not say how a launch ended).

``decision_facts`` reads them on the caller's session (``policy.decision.decide_now`` calls it);
``facts_of`` puts them together, and is pure. ``made_up_facts`` gives the facts of a made-up person
of a role (an example of the rights writer for a role nobody has yet): the role's word as the
name, no history, and the household as it is; for an example about Domo on his own, his name
(``DOMO_NAME``) and what was declared spent for the house.

The yes-judge (``confirmations.judge``, spec 4.2) reads more of the same: ``person_history`` counts
the same four kinds of actions done, with no request left out, since always too, with the moment of
the last one and how many of the person's requests of this type were refused or dropped (a refusal
of the judge itself creates no request: its card counts); ``household_people`` says who is in the
household, by name and role (display names are no secret). For Domo on his own,
``domo_judge_history`` reads the same from the audit log, as ``_domo_history`` does: he has no
request of his own, so none of his was refused or dropped.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Final, NamedTuple
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import ColumnElement, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import QueryableAttribute

from domo_core.accounts.models import Person
from domo_core.accounts.sessions import active_at
from domo_core.audit.models import AuditEntry
from domo_core.chat.models import Conversation, Message, MessageAction
from domo_core.confirmations.models import Confirmation, ConfirmationStep
from domo_core.household.models import Invitation
from domo_core.household.service import open_at
from domo_core.llm.types import JsonObject
from domo_core.madetools.yes import spent_by
from domo_core.policy.actions import DescribedAction
from domo_core.policy.roles import DOMO_LABEL, ROLES, Actor, actor_label, as_role

# What the card of a call says under ``"launch"`` when the tool's launch only stopped for a yes
# (``madetools.offered.outcome_of``): the tool's work is not done, and the history leaves that
# call out, however it was allowed (at once, or after a yes).
STOPPED_FOR_A_YES: Final = "stopped"
# The name the facts of a limit give Domo.
DOMO_NAME: Final = "Domo"
# The audit entry of a decision of the barrier (``barrier.record_decision``).
_DECISION: Final = "policy.decision"

_DAY: Final = timedelta(days=1)
_WEEK: Final = timedelta(days=7)
_MONTH: Final = timedelta(days=30)

# The action of a chain of actions, whose card lists the types of the steps it did (``"did"``,
# ``confirmations.chain``).
_CHAIN: Final = "chain.run"
# The reason on the card of an action the yes-judge refused (``"reason"``).
_JUDGE_REFUSAL: Final = "judge"
_NO_ANSWER: Final = ("refused", "expired")


class History(NamedTuple):
    """How many actions of a type a person did over the last day, week and month."""

    day: int = 0
    week: int = 0
    month: int = 0


NO_HISTORY: Final = History()


class PersonHistory(NamedTuple):
    """How many actions of a type a person did (the four kinds ``_history`` counts, with no request
    left out): over the last day, week and month, since always (``ever``), when the last one was
    (``last_at``), how many of their requests of this type were refused (``refused``: requests
    refused, and the actions of this type the judge refused, which create no request) and how many
    lapsed (``expired``: past their lifetime, or dropped by the assistant). A no and a silence are
    apart: only the first says what the person wants."""

    day: int
    week: int
    month: int
    ever: int
    last_at: datetime | None
    refused: int
    expired: int

    @property
    def first_time(self) -> bool:
        """Whether the person never did an action of this type."""
        return self.ever == 0


class _Done(NamedTuple):
    """What one kind of action done counts: over the last day, week and month, in all (since
    always, or since the moment asked), and the moment of the last one."""

    day: int
    week: int
    month: int
    ever: int
    last_at: datetime | None


def facts_of(
    actor: Actor,
    action: DescribedAction,
    *,
    name: str,
    local_now: datetime | None,
    people: Mapping[str, int] | None = None,
    invitations: Mapping[str, int] | None = None,
    history: History = NO_HISTORY,
    spent: Mapping[str, int] | None = None,
) -> dict[str, object]:
    """The facts of the moment, as a limit reads them (see the module docstring): ``name`` is the
    actor's display name, ``local_now`` the moment in the household's time zone (``None`` when it
    is not set: no ``now``), ``people`` and ``invitations`` the counts by role (a role that is not
    there counts 0)."""
    counted_people = people or {}
    counted_invitations = invitations or {}
    facts: dict[str, object] = {
        "actor": {"id": actor_label(actor), "role": actor.role, "name": name},
        "action": {
            "type": action.action_type,
            "target": action.target,
            "facts": dict(action.facts),
        },
        "household": {
            "people": {role: counted_people.get(role, 0) for role in ROLES},
            "invitations": {role: counted_invitations.get(role, 0) for role in ROLES},
        },
        "history": {"day": history.day, "week": history.week, "month": history.month},
        "spent": {period: (spent or {}).get(period, 0) for period in ("day", "week", "month")},
    }
    if local_now is not None:
        facts["now"] = {
            "hour": local_now.hour,
            "minute": local_now.minute,
            "weekday": local_now.isoweekday(),
            "date": local_now.date().isoformat(),
        }
    return facts


async def decision_facts(
    session: AsyncSession,
    actor: Actor,
    action: DescribedAction,
    now: datetime,
    time_zone: ZoneInfo | None,
    *,
    not_counting: UUID | None = None,
    unrecorded: Sequence[str] = (),
    spent_leaving_out: UUID | None = None,
) -> dict[str, object]:
    """The facts of the moment ``now`` for ``actor`` and ``action``, as the database holds them on
    ``session`` (see the module docstring). ``spent_leaving_out`` is a declaration ``spent`` leaves
    out (a relaunch's begin whose money it is: counted once, as the begin's amount). ``time_zone``
    is the household's, ``None`` when the owner has not set it. ``not_counting`` is a request (a
    confirmation) that the history leaves out; ``unrecorded`` the types of actions the person did a
    moment ago that no table holds yet (the steps of a chain that runs at once): each of this type
    counts one more. Reads only.

    For Domo (``roles.domo_actor``), no person is read: his name is ``DOMO_NAME``, and his
    history what the barrier allowed him of this type (``_domo_history``)."""
    if actor.subject == "domo":
        name = DOMO_NAME
        history = await _domo_history(session, action.action_type, now)
    else:
        name = (
            await session.execute(select(Person.display_name).where(Person.id == actor.person_id))
        ).scalar_one()
        history = await _history(session, actor, action.action_type, now, not_counting)
    people, invitations = await _household(session, now)
    more = sum(1 for done in unrecorded if done == action.action_type)
    # What was declared spent for the subject: the house's for Domo on his own.
    spent = await spent_by(
        session,
        None if actor.subject == "domo" else actor.person_id,
        now,
        time_zone,
        leaving_out=spent_leaving_out,
    )
    return facts_of(
        actor,
        action,
        name=name,
        local_now=None if time_zone is None else now.astimezone(time_zone),
        people=people,
        invitations=invitations,
        history=History(history.day + more, history.week + more, history.month + more),
        spent=spent,
    )


async def made_up_facts(
    session: AsyncSession,
    actor: Actor,
    action: DescribedAction,
    now: datetime,
    time_zone: ZoneInfo | None,
) -> dict[str, object]:
    """The facts of the moment ``now`` for a made-up person of ``actor.role`` (``actor``'s id
    belongs to nobody): the role's word as the name, no history (``NO_HISTORY``), and the
    household as the database holds it on ``session``. For Domo on his own
    (``roles.domo_actor``), his name and what was declared spent for the house, as his own
    facts are (``decision_facts``). Reads only."""
    people, invitations = await _household(session, now)
    domo = actor.subject == "domo"
    return facts_of(
        actor,
        action,
        name=DOMO_NAME if domo else actor.role,
        local_now=None if time_zone is None else now.astimezone(time_zone),
        people=people,
        invitations=invitations,
        history=NO_HISTORY,
        spent=await spent_by(session, None, now, time_zone) if domo else None,
    )


async def _household(session: AsyncSession, now: datetime) -> tuple[dict[str, int], dict[str, int]]:
    """How many people are in the household at ``now``, and how many invitations are open, by
    role."""
    people = await session.execute(
        select(Person.role, func.count()).where(active_at(now)).group_by(Person.role)
    )
    invitations = await session.execute(
        select(Invitation.role, func.count()).where(open_at(now)).group_by(Invitation.role)
    )
    return (
        {role: count for role, count in people.all()},
        {role: count for role, count in invitations.all()},
    )


async def household_people(session: AsyncSession, now: datetime) -> list[JsonObject]:
    """Who is in the household at ``now``: ``{"name", "role"}`` each, by role (the owner first,
    in the order of ``ROLES``), then by name. Reads only."""
    result = await session.execute(select(Person.display_name, Person.role).where(active_at(now)))
    people = [(ROLES.index(as_role(role)), name, role) for name, role in result.all()]
    return [{"name": name, "role": role} for _, name, role in sorted(people)]


def _counts(
    moment: QueryableAttribute[datetime | None], now: datetime
) -> tuple[
    ColumnElement[int],
    ColumnElement[int],
    ColumnElement[int],
    ColumnElement[int],
    ColumnElement[datetime | None],
]:
    """How many rows fall in the last day, week and month before ``now``, by ``moment``, how many
    rows there are in all, and the latest ``moment``."""
    return (
        func.count().filter(moment > now - _DAY),
        func.count().filter(moment > now - _WEEK),
        func.count().filter(moment > now - _MONTH),
        func.count(),
        func.max(moment),
    )


def _did_it(card: QueryableAttribute[dict[str, object] | None]) -> ColumnElement[bool]:
    """That the call whose card is ``card`` did its action: its launch did not only stop for a
    yes (``STOPPED_FOR_A_YES``)."""
    return func.coalesce(card["launch"].astext, "") != STOPPED_FOR_A_YES


def _after(
    moment: QueryableAttribute[datetime | None], since: datetime | None
) -> list[ColumnElement[bool]]:
    """That ``moment`` is after ``since``; no condition when ``since`` is ``None``."""
    return [] if since is None else [moment > since]


async def _done(
    session: AsyncSession,
    person_id: UUID,
    action_type: str,
    now: datetime,
    *,
    since: datetime | None,
    not_counting: UUID | None = None,
) -> list[_Done]:
    """What each of the four kinds of actions done counts (see the module docstring), for the
    actions of ``action_type`` of the person after ``since`` (since always when it is ``None``),
    without the request ``not_counting`` (its confirmation, the rows that record it under a reply,
    and, for a chain, its steps: it is counted zero times, not subtracted)."""
    of_this_type = Confirmation.action["action_type"].astext == action_type
    # The row a yes in words leaves under its reply records a request of this type: the request
    # counts it.
    records_a_yes = (
        select(Confirmation.id)
        .where(Confirmation.id == MessageAction.confirmation_id, of_this_type)
        .exists()
    )
    allowed_at_once = (
        await session.execute(
            select(*_counts(MessageAction.created_at, now))
            .select_from(MessageAction)
            .join(Message, Message.id == MessageAction.message_id)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Conversation.person_id == person_id,
                MessageAction.by_domo.is_(False),
                MessageAction.action_type == action_type,
                MessageAction.status == "done",
                _did_it(MessageAction.summary),
                *_after(MessageAction.created_at, since),
                ~records_a_yes,
                *(
                    [
                        or_(
                            MessageAction.confirmation_id.is_(None),
                            MessageAction.confirmation_id != not_counting,
                        )
                    ]
                    if not_counting is not None
                    else []
                ),
            )
        )
    ).one()
    decided_at = Confirmation.decided_at
    after_a_yes = (
        await session.execute(
            select(*_counts(decided_at, now)).where(
                Confirmation.requester_id == person_id,
                Confirmation.by_domo.is_(False),
                Confirmation.status == "executed",
                _did_it(Confirmation.result),
                of_this_type,
                *_after(decided_at, since),
                *([Confirmation.id != not_counting] if not_counting is not None else []),
            )
        )
    ).one()
    # The steps of the person's chains that waited for a yes, each when it ended; those of a
    # chain that a new request replaced are counted on the new one, which holds them copied.
    step_ended = ConfirmationStep.done_at
    steps_after_a_yes = (
        await session.execute(
            select(*_counts(step_ended, now))
            .select_from(ConfirmationStep)
            .join(Confirmation, Confirmation.id == ConfirmationStep.confirmation_id)
            .where(
                Confirmation.requester_id == person_id,
                Confirmation.by_domo.is_(False),
                Confirmation.replaced_by.is_(None),
                ConfirmationStep.action_type == action_type,
                ConfirmationStep.status == "done",
                *_after(step_ended, since),
                *([Confirmation.id != not_counting] if not_counting is not None else []),
            )
        )
    ).one()
    # The steps of the person's chains that ran at once (no request), by the types their card
    # lists, each when its chain ended.
    did = (
        func.jsonb_array_elements_text(MessageAction.summary["did"]).table_valued("value").lateral()
    )
    steps_at_once = (
        await session.execute(
            select(*_counts(MessageAction.created_at, now))
            .select_from(MessageAction)
            .join(Message, Message.id == MessageAction.message_id)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .join(did, true())
            .where(
                Conversation.person_id == person_id,
                MessageAction.by_domo.is_(False),
                MessageAction.action_type == _CHAIN,
                MessageAction.confirmation_id.is_(None),
                *_after(MessageAction.created_at, since),
                did.c.value == action_type,
            )
        )
    ).one()
    return [
        _Done(*counted)
        for counted in (allowed_at_once, after_a_yes, steps_after_a_yes, steps_at_once)
    ]


async def _history(
    session: AsyncSession,
    actor: Actor,
    action_type: str,
    now: datetime,
    not_counting: UUID | None = None,
) -> History:
    """How many actions of ``action_type`` the person did, over the last day, week and month,
    without the request ``not_counting`` (see ``_done``)."""
    done = await _done(
        session, actor.person_id, action_type, now, since=now - _MONTH, not_counting=not_counting
    )
    return History(
        sum(counted.day for counted in done),
        sum(counted.week for counted in done),
        sum(counted.month for counted in done),
    )


def _allowed_to_domo(action_type: str) -> list[ColumnElement[bool]]:
    """The audit's ``policy.decision`` entries that count in Domo's history of ``action_type``:
    his own (actor ``domo``), allowed, of this type, and no call that carried a yes (``"yes":
    true`` among the facts): that request counted already, at its first call."""
    return [
        AuditEntry.actor == DOMO_LABEL,
        AuditEntry.action == _DECISION,
        AuditEntry.details["decision"].astext == "allow",
        AuditEntry.details["action_type"].astext == action_type,
        AuditEntry.details["facts"]["yes"].astext.is_distinct_from("true"),
    ]


async def _domo_history(session: AsyncSession, action_type: str, now: datetime) -> History:
    """How many actions of ``action_type`` the barrier allowed Domo on his own over the last day,
    week and month before ``now`` (``_allowed_to_domo``: he has no conversation nor request of
    his own to count)."""
    ts = AuditEntry.ts
    counted = (
        await session.execute(
            select(
                func.count().filter(ts > now - _DAY),
                func.count().filter(ts > now - _WEEK),
                func.count(),
            ).where(*_allowed_to_domo(action_type), ts > now - _MONTH)
        )
    ).one()
    day, week, month = counted
    return History(day, week, month)


async def domo_judge_history(
    session: AsyncSession, action_type: str, now: datetime
) -> PersonHistory:
    """What Domo did on his own of ``action_type`` until ``now``, as the yes-judge reads it
    (``PersonHistory``, as ``person_history`` for a person): what the barrier allowed him
    (``_allowed_to_domo``) over the last day, week and month, since always, and the moment of the
    last one, less the requests of his that were counted when made (``counted_for_domo``) and ended
    ``refused`` or ``expired``, which never ran (they are counted by the moment they were made, the
    moment of their ``allow``); and how many of his requests of this type were refused or expired.
    Reads only."""
    ts = AuditEntry.ts
    day, week, month, ever, last_at = (
        await session.execute(
            select(
                func.count().filter(ts > now - _DAY),
                func.count().filter(ts > now - _WEEK),
                func.count().filter(ts > now - _MONTH),
                func.count(),
                func.max(ts),
            ).where(*_allowed_to_domo(action_type), ts <= now)
        )
    ).one()
    made = Confirmation.created_at
    never_ran = (
        await session.execute(
            select(
                func.count().filter(made > now - _DAY),
                func.count().filter(made > now - _WEEK),
                func.count().filter(made > now - _MONTH),
                func.count(),
            ).where(
                *_his_requests(action_type),
                Confirmation.counted_for_domo.is_(True),
                Confirmation.status.in_(_NO_ANSWER),
            )
        )
    ).one()
    day, week, month, ever = (
        max(0, done - lost) for done, lost in zip((day, week, month, ever), never_ran, strict=True)
    )
    return PersonHistory(
        day=day,
        week=week,
        month=month,
        ever=ever,
        last_at=last_at if ever else None,
        refused=await _his_requests_ended(session, action_type, "refused"),
        expired=await _his_requests_ended(session, action_type, "expired"),
    )


def _his_requests(action_type: str) -> list[ColumnElement[bool]]:
    """What picks the requests of ``action_type`` that Domo made in his own name."""
    return [
        Confirmation.by_domo.is_(True),
        Confirmation.action["action_type"].astext == action_type,
    ]


async def _his_requests_ended(session: AsyncSession, action_type: str, status: str) -> int:
    """How many of Domo's requests of ``action_type`` ended ``status``."""
    return (
        await session.execute(
            select(func.count())
            .select_from(Confirmation)
            .where(*_his_requests(action_type), Confirmation.status == status)
        )
    ).scalar_one()


async def person_history(
    session: AsyncSession, person_id: UUID, action_type: str, now: datetime
) -> PersonHistory:
    """What the person did of ``action_type`` until ``now``, as the yes-judge reads it
    (``PersonHistory``): the four kinds of actions done, since always and with no request left
    out, and how many of their requests of this type were refused and how many expired.
    Reads only."""
    done = await _done(session, person_id, action_type, now, since=None)
    moments = [counted.last_at for counted in done if counted.last_at is not None]
    requests_ended = {
        status: (
            await session.execute(
                select(func.count())
                .select_from(Confirmation)
                .where(
                    Confirmation.requester_id == person_id,
                    Confirmation.by_domo.is_(False),
                    Confirmation.action["action_type"].astext == action_type,
                    Confirmation.status == status,
                )
            )
        ).scalar_one()
        for status in _NO_ANSWER
    }
    # The judge's own refusals create no request: the card of the action says so.
    refused_by_the_judge = (
        await session.execute(
            select(func.count())
            .select_from(MessageAction)
            .join(Message, Message.id == MessageAction.message_id)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Conversation.person_id == person_id,
                MessageAction.by_domo.is_(False),
                MessageAction.action_type == action_type,
                MessageAction.status == "refused",
                MessageAction.confirmation_id.is_(None),
                MessageAction.summary["reason"].astext == _JUDGE_REFUSAL,
            )
        )
    ).scalar_one()
    return PersonHistory(
        day=sum(counted.day for counted in done),
        week=sum(counted.week for counted in done),
        month=sum(counted.month for counted in done),
        ever=sum(counted.ever for counted in done),
        last_at=max(moments, default=None),
        refused=requests_ended["refused"] + refused_by_the_judge,
        expired=requests_ended["expired"],
    )
