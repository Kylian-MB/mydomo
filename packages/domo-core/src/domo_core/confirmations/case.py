"""The case the yes-judge reads (spec 4.2): what it is made of (``JudgeCase`` and its parts) and
how the code builds it from the database (``build_case``), never copied from what the model that
talks read. ``confirmations.judge`` re-exports all of it; the judge's call, its verdicts and what
is asked in the end stay there.

The elements of the case ("Domo judges first", ``gather``): everything the assistant knows that
bears on a judged action, gathered in one place, bounded, each line marked with its author. Three
kinds of knowledge are never mixed: the person's own words (``them``), someone else's words with
their name (``other``, ``owner``), and the assistant's own conclusions (``domo``). Which kind a
memory is comes from its columns alone (``said_by_id``, ``origin``, ``by_domo``, ``kind``), never
from its words; and nothing kept private by anyone but ``private_of`` enters
(``memory.service.visible_to``, in every query). Everything in it is data, never instructions.
"""

import logging
import math
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, date, datetime
from typing import Any, Final, Literal, cast
from uuid import UUID
from zoneinfo import ZoneInfo

import anyio
from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.accounts.sessions import is_active
from domo_core.chat.models import Conversation, Message, MessageAction
from domo_core.confirmations.describe import (
    describe_for_people,
    describe_type,
    description_for_a_model,
    request_description,
    step_sentence,
)
from domo_core.confirmations.models import ConfirmationStep
from domo_core.confirmations.steps import PlannedStep
from domo_core.confirmations.store import pending_for, pending_for_others
from domo_core.confirmations.who_agrees import may_agree_for_a_child
from domo_core.conversation.speaker import Speaker
from domo_core.learning.models import WentWrong
from domo_core.learning.read import went_wrong_for
from domo_core.llm.types import JsonObject
from domo_core.memory.embedder import Embedder
from domo_core.memory.models import Memory
from domo_core.memory.search import search_for_case
from domo_core.memory.service import A_PROGRAM_OR_A_TOOL, of_domo, unconfirmed, visible_to
from domo_core.policy.actions import DescribedAction
from domo_core.policy.asks import NeverAsk
from domo_core.policy.facts import (
    DOMO_NAME,
    PersonHistory,
    domo_judge_history,
    household_people,
    person_history,
)
from domo_core.policy.roles import Actor
from domo_core.policy.settings import load_policy_settings
from domo_core.policy.subjects import Subject, owner_of_now
from domo_core.prompts import sendable, today_of
from domo_core.texts.view import REFERENCE
from domo_core.trust.service import facts_of, trust_of

logger = logging.getLogger(__name__)

# How many of the person's last messages the judge reads, each cut at that many characters.
JUDGE_MESSAGES: Final = 5
JUDGE_MESSAGE_CHARS: Final = 1000

# The facts of an action the judge never reads: ``owner_first`` (``household.invite``) is
# Domo's own judgement that the owner says yes first, for the barrier; the judge would take it
# for a doubt about the request. The barrier, the limit and the audit keep it.
NOT_FOR_THE_JUDGE: Final = frozenset({"owner_first"})

# What a step checked in its turn says after its sentence (it has no described action yet).
_CHECKED_IN_TURN: Final = " (checked in its turn)"


@dataclass(frozen=True)
class RightsSay:
    """What the barrier said, for the judge: the action is ``allowed``; ``needs`` is whose yes the
    rights require first (``adult`` or ``owner``), ``None`` when nobody else's; ``question`` is
    what a limit could not tell, when one sent the request to the owner. ``judge_decides``: that
    agreement is left to the judge first (``barrier.NeedsApproval.judge_first``): its ``do``
    lets the action through without them. For an adult's, only when the case holds a line marked
    ``may_agree`` (``build_case`` takes it back otherwise)."""

    allowed: bool
    needs: Literal["adult", "owner"] | None
    question: str | None
    judge_decides: bool = False


@dataclass(frozen=True)
class StepTold:
    """One step of a chain, as the judge reads it: its ``type``, what it does in words (``says``),
    whether it is checked in its turn (an earlier step changes what it touches: it has no
    described action yet), whose arrival it waits for (``waits_for``, the invitation's name), and
    the owner's own instruction about it (``owner_rule``, as ``JudgeCase.owner_rule``).

    A step that is not checked in its turn is also told what is known of it, as the case tells it
    of an action alone (``known_of_a_step``; each element ``STEP_LINES`` lines at most): its
    ``facts`` (as ``JudgeCase.action``), the ``history`` of its own type (as the case's, for the
    one the chain is judged as), what went wrong before (``went_wrong``), the assistant's
    conclusions about it (``lessons``), what is remembered of it (``remembered``, each line marked
    by its author) and the state of the thing (``subject``). The chain's own history stays the
    case's. A step checked in its turn, or past the ``CASE_STEPS`` first, has none of it."""

    type: str
    says: str
    checked_in_turn: bool
    waits_for: str | None
    owner_rule: JsonObject | None = None
    facts: JsonObject = field(default_factory=dict[str, Any])
    history: JsonObject | None = None
    went_wrong: tuple["WentWrongLine", ...] = ()
    lessons: tuple["KnownLine", ...] = ()
    remembered: tuple["KnownLine", ...] = ()
    subject: "SubjectState | None" = None

    def as_json(self) -> JsonObject:
        """The step as the case holds it (``owner_rule``, and each of what is known of it, only
        when there is one)."""
        told: JsonObject = {
            "type": self.type,
            "says": self.says,
            "checked_in_turn": self.checked_in_turn,
            "waits_for": self.waits_for,
        }
        if self.owner_rule is not None:
            told["owner_rule"] = dict(self.owner_rule)
        if self.facts:
            told["facts"] = dict(self.facts)
        if self.history is not None:
            told["history"] = dict(self.history)
        for name, lines in (
            ("went_wrong", self.went_wrong),
            ("lessons", self.lessons),
            ("remembered", self.remembered),
        ):
            if lines:
                told[name] = [line_json(line) for line in lines]
        if self.subject is not None:
            told["subject"] = asdict(self.subject)
        return told


@dataclass(frozen=True)
class YesTold:
    """The yes a request had: ``when`` (in words), ``by`` whom (``the person``, ``an adult`` or
    ``the owner``) and ``how`` (``button``, ``words``, ``change``, ``proof``, or ``none``)."""

    when: str
    by: str
    how: str


@dataclass(frozen=True)
class RequestTold:
    """A request that is changed or taken up again (spec 4.2, its last block), for the judge: the
    ``yes`` it had (``None`` when none), the steps ``done``, the step that ``failed`` and why
    (``failed_reason``), whether that step ``may_have_acted`` (a fact the code tells), what it did
    ``before`` and does ``after`` the change (both empty when nothing changed), and what it
    ``asked`` before (``yes``, ``explicit_yes``, ``proof``, or ``None``)."""

    yes: YesTold | None
    done: tuple[str, ...]
    failed: str | None
    failed_reason: str | None
    may_have_acted: bool
    before: tuple[str, ...]
    after: tuple[str, ...]
    asked: str | None = None


Author = Literal["them", "other", "owner", "domo"]


@dataclass(frozen=True)
class KnownLine:
    """One memory as the case holds it: what it says (``content``, cut at ``CASE_LINE_CHARS``),
    the day it was last written (``when``, ``YYYY-MM-DD`` in the household's time zone) and its
    author (``by``): the person who asks (``them``), the owner of now (``owner``), anyone else
    (``other``), or the assistant himself (``domo``). ``who`` is the display name of the one who
    said it, for ``other`` and ``owner`` only (``None`` when that person is gone; ``"a program or a
    tool"`` for what was kept from a program's text when nobody wrote, origin ``external``);
    ``unconfirmed`` that it is someone's word about another person of the household that this
    person has not confirmed (``memory.service.unconfirmed``). ``answer`` and ``keep_asking``
    are, for what he told them before an action (``told_by_domo``), the answer they gave and
    whether they agreed to be asked again. ``may_agree`` is the code's mark, never a model's nor
    anyone's words: the line is the own words, about the one who asks, of someone who may say yes
    for them (``who_agrees.may_agree_for_a_child``), still in the household; the case keeps it
    only where an adult's agreement is left to the judge (``build_case``), and the JSON names it
    only when it is set. ``concerned`` marks, in a case of the assistant's own, what he told the
    person the thing belongs to (ruling R69), apart from the words of the one who asks; named only
    when set too."""

    content: str
    when: str
    by: Author
    who: str | None
    unconfirmed: bool = False
    answer: str | None = None
    keep_asking: bool | None = None
    may_agree: bool = False
    concerned: bool = False


def line_json(line: "KnownLine | WentWrongLine") -> JsonObject:
    """``line`` as the judge reads it: a known line's ``may_agree`` and ``concerned`` only when
    the code set them."""
    told: JsonObject = asdict(line)
    if isinstance(line, KnownLine):
        for mark in ("may_agree", "concerned"):
            if not told[mark]:
                del told[mark]
    return told


@dataclass(frozen=True)
class WentWrongLine:
    """Something that went wrong before (``learning``): its ``kind``, ``when`` (``YYYY-MM-DD
    HH:MM`` in the household's time zone), ``by`` whom (a display name, the assistant's name for
    him or his judge, ``None`` for the code or someone gone), whether it was his own idea, act or
    verdict (``of_domo``), the ``reason`` said (cut at ``CASE_LINE_CHARS``) and whether it was
    about the very thing this action is about (``same_subject``)."""

    kind: str
    when: str
    by: str | None
    of_domo: bool
    reason: str | None
    same_subject: bool


@dataclass(frozen=True)
class SubjectState:
    """The last action done on the thing (the same subject key): its type (``last``), ``by``
    whom (a display name, or the assistant's name for an act of his), ``when`` (``YYYY-MM-DD
    HH:MM`` in the household's time zone), and the ``reason`` someone gave when it went wrong,
    when there is one."""

    key: str
    last: str
    by: str
    when: str
    reason: str | None


@dataclass(frozen=True)
class IdeaTold:
    """Whose idea the action is (``of``: the person's, or the assistant's own), his ``reason``
    for it, whose words it was born of (``born_of_words_of``, a display name), whether the
    person concerned lacks the right to do it themselves (``they_lack_the_right``), and whether
    the turn carries text nobody of the house said (``after_outside_words``: a note, a tool's or a
    program's output; ruling R77)."""

    of: Literal["person", "domo"]
    reason: str | None = None
    born_of_words_of: str | None = None
    they_lack_the_right: bool = False
    after_outside_words: bool = False


@dataclass(frozen=True)
class Gathered:
    """The elements of a case (``gather``): the person's own words (``remembered``, what he told
    them before such an action first), what others said (``others_said``), what the owner said
    (``owner_said``), the assistant's own conclusions (``lessons``, those about this action
    first), the person's ``trust`` (``None`` when nobody asks), what went wrong before
    (``went_wrong``) and the state of the thing (``subject``). ``missing`` names, in this order,
    the elements that could not be read in time or at all (``remembered`` stands for the four
    sorts of memories): they are left empty."""

    remembered: tuple[KnownLine, ...]
    others_said: tuple[KnownLine, ...]
    owner_said: tuple[KnownLine, ...]
    lessons: tuple[KnownLine, ...]
    trust: JsonObject | None
    went_wrong: tuple[WentWrongLine, ...]
    subject: SubjectState | None
    missing: tuple[str, ...]


@dataclass(frozen=True)
class JudgeCase:
    """What the judge reads (spec 4.2), built by the code from the database (``build_case``).

    ``person`` is ``{"name", "role"}``; ``action`` ``{"type", "target", "facts", "says"}``, the
    action as the barrier sees it (without ``NOT_FOR_THE_JUDGE``) and in words; ``rights`` what
    the barrier said; ``words`` the person's last ``JUDGE_MESSAGES`` messages of this
    conversation, oldest first, each cut at ``JUDGE_MESSAGE_CHARS``; ``asked_them`` what Domo
    had just said to them, before their last message (cut the same way; ``None`` when nothing):
    what a "yes" of theirs answers, as data;
    ``relayed`` that this reply is not his own answer to them (``Message.own_answer`` false: it
    told them words of someone else, or Domo spoke first), never true without ``asked_them``;
    ``history`` what they did of this kind; ``now`` ``{"weekday",
    "date", "hour", "minute", "zone"}`` (the date ``YYYY-MM-DD``), in the household's time zone,
    else in UTC with ``"zone": None``;
    ``household`` who is in it (``{"name", "role"}`` each); ``pending`` the person's requests that
    wait, in English (a note that waits for someone else's yes without its words); ``steps`` a
    chain's steps; ``request`` a request changed or taken up again; ``owner_rule`` the owner's own
    instruction about this action (``owner_rule_of``), ``None`` when he gave none; ``speaker`` how
    the person reached Domo and the clues that it is them (``Speaker.as_json``: never an
    identifier), ``None`` when nobody is said to speak. ``language`` is the language of the one
    who asks (a tag: ``en``, ``pt-BR``...): the judge writes its reason in it, since the reason is
    shown to them as it is (spec 4.3).

    What the assistant knows that bears on the action (``gather``, each line marked with its
    author, never mixed): ``remembered`` the words of the one who asks (what he told them before
    such an action first), ``others_said`` and ``owner_said`` someone else's words with their
    name, ``lessons`` his own conclusions; ``trust`` the trust of the one who asks (``None`` when
    nobody asks); ``went_wrong`` what went wrong before; ``subject`` the state of the thing;
    ``idea`` whose idea the action is, and why when it is his; ``cost`` what it costs, when it is
    known; ``missing`` what could not be read (``everything`` when nothing could)."""

    person: JsonObject
    action: JsonObject
    rights: RightsSay
    words: tuple[str, ...]
    history: PersonHistory
    now: JsonObject
    household: tuple[JsonObject, ...]
    pending: tuple[str, ...]
    language: str
    steps: tuple[StepTold, ...] = ()
    request: RequestTold | None = None
    owner_rule: JsonObject | None = None
    speaker: JsonObject | None = None
    asked_them: str | None = None
    relayed: bool = False
    remembered: tuple[KnownLine, ...] = ()
    others_said: tuple[KnownLine, ...] = ()
    owner_said: tuple[KnownLine, ...] = ()
    lessons: tuple[KnownLine, ...] = ()
    trust: JsonObject | None = None
    went_wrong: tuple[WentWrongLine, ...] = ()
    subject: SubjectState | None = None
    idea: IdeaTold = IdeaTold("person")
    cost: JsonObject | None = None
    missing: tuple[str, ...] = ()

    def as_json(self) -> JsonObject:
        """The case as the judge reads it: plain JSON, every text made sendable
        (``prompts.sendable``). ``history`` says ``first_time``, and the day of the ``last`` one
        (``_last_time``: ``YYYY-MM-DD`` in the zone of ``now``, with ``days_ago``; ``None`` and no
        ``days_ago`` when there is none). ``idea`` is always there; ``steps``, ``request``,
        ``owner_rule``, ``speaker``, ``asked_them`` and each element of what the assistant knows
        (``remembered`` to ``subject``, ``cost``, ``missing``) only when they hold something, and
        ``relayed`` (``true``) only with ``asked_them`` when that reply relayed someone or spoke
        first."""
        case: JsonObject = {
            "person": dict(self.person),
            "action": dict(self.action),
            "rights": asdict(self.rights),
            "words": list(self.words),
            "history": history_told(self.history, self.now),
            "now": dict(self.now),
            "household": [dict(person) for person in self.household],
            "pending": list(self.pending),
            "language": self.language,
        }
        if self.steps:
            case["steps"] = [step.as_json() for step in self.steps]
        if self.request is not None:
            case["request"] = asdict(self.request)
        if self.owner_rule is not None:
            case["owner_rule"] = dict(self.owner_rule)
        if self.speaker is not None:
            case["speaker"] = dict(self.speaker)
        if self.asked_them is not None:
            case["asked_them"] = self.asked_them
            if self.relayed:
                case["relayed"] = True
        for name, lines in (
            ("remembered", self.remembered),
            ("others_said", self.others_said),
            ("owner_said", self.owner_said),
            ("lessons", self.lessons),
            ("went_wrong", self.went_wrong),
        ):
            if lines:
                case[name] = [line_json(line) for line in lines]
        if self.trust is not None:
            case["trust"] = dict(self.trust)
        if self.subject is not None:
            case["subject"] = asdict(self.subject)
        case["idea"] = asdict(self.idea)
        if self.cost is not None:
            case["cost"] = dict(self.cost)
        if self.missing:
            case["missing"] = list(self.missing)
        return cast(JsonObject, sendable(case))


def history_told(history: PersonHistory, now: JsonObject) -> JsonObject:
    """A history as the judge reads it, for the case and for each step of a chain: the counts, the
    day of the last one (``_last_time``, in the zone of ``now``), what was refused or lapsed, and
    ``first_time``."""
    return {
        "day": history.day,
        "week": history.week,
        "month": history.month,
        "ever": history.ever,
        **_last_time(history.last_at, now),
        "refused": history.refused,
        "expired": history.expired,
        "first_time": history.first_time,
    }


async def history_of(
    session: AsyncSession, actor: Actor, action_type: str, now: datetime
) -> PersonHistory:
    """What ``actor`` did of ``action_type`` until ``now``: the assistant's own
    (``policy.facts.domo_judge_history``) when he is judged, else the person's."""
    if actor.subject == "domo":
        return await domo_judge_history(session, action_type, now)
    return await person_history(session, actor.person_id, action_type, now)


def _last_time(last_at: datetime | None, now: JsonObject) -> JsonObject:
    """When the last one was, as the judge reads it, in the same zone as ``now`` (its ``"zone"``,
    UTC when it is ``None``): its day (``"last"``, ``YYYY-MM-DD``) and how many whole days before
    ``now``'s ``"date"`` that day is (``"days_ago"``: 0 for today). Only ``"last": None`` when
    there is no last one."""
    if last_at is None:
        return {"last": None}
    zone = now["zone"]
    day = last_at.astimezone(UTC if zone is None else ZoneInfo(zone)).date()
    return {"last": day.isoformat(), "days_ago": (date.fromisoformat(now["date"]) - day).days}


def owner_rule_of(never_ask: NeverAsk | None) -> JsonObject | None:
    """The owner's "never ask me for this" (``NeverAsk``), as the judge reads it:
    ``{"never_ask": True, "when": <his conditions, or None>, "at_all": <"not at all">}``; ``None``
    when he gave none."""
    if never_ask is None:
        return None
    return {"never_ask": True, "when": never_ask.when, "at_all": never_ask.at_all}


async def _persons_words(
    session: AsyncSession, person_id: UUID, conversation_id: UUID
) -> tuple[str, ...]:
    """The person's last ``JUDGE_MESSAGES`` messages of the conversation, oldest first, each cut
    at ``JUDGE_MESSAGE_CHARS``: what they typed (role ``user``, provenance ``person``), in their
    own conversation only."""
    result = await session.execute(
        select(Message.content)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Message.conversation_id == conversation_id,
            Conversation.person_id == person_id,
            Message.role == "user",
            Message.provenance == "person",
        )
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(JUDGE_MESSAGES)
    )
    return tuple(content[:JUDGE_MESSAGE_CHARS] for content in reversed(result.scalars().all()))


async def _what_domo_had_said(
    session: AsyncSession, person_id: UUID, conversation_id: UUID
) -> tuple[str, bool] | None:
    """Domo's last reply before the person's last message of their conversation, cut at
    ``JUDGE_MESSAGE_CHARS``: what their last words answer, and whether it was relayed. ``None``
    when they said nothing yet, when Domo had said nothing before it, or nothing at all (an
    empty reply). The reply being written in this turn comes after their words: it is never read.

    It is that reply whether or not it is his own answer to them (the owner's decision of
    2026-11-17: his "yes, create it" to what Domo just told him is meant to be understood). When
    it is not (``Message.own_answer`` false: a turn that answered no message of theirs, a wake or
    an arrival, or that was given words from outside, a note someone left them that the reply may
    tell or quote; or a reply the code cannot tell that of), it is said to be relayed: the judge
    then counts a yes only for an action the reply itself plainly names. It is only the reply as
    the person read it: never the note, a tool's result or anyone's private words themselves."""
    theirs = (
        select(Message.created_at, Message.id)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Message.conversation_id == conversation_id,
            Conversation.person_id == person_id,
            Message.role == "user",
            Message.provenance == "person",
        )
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(1)
    )
    last = (await session.execute(theirs)).one_or_none()
    if last is None:
        return None
    said = await session.execute(
        select(Message.content, Message.own_answer)
        .where(
            Message.conversation_id == conversation_id,
            Message.role == "assistant",
            Message.created_at < last.created_at,
        )
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(1)
    )
    reply = said.one_or_none()
    if reply is None or not reply.content.strip():
        return None
    return reply.content[:JUDGE_MESSAGE_CHARS], not reply.own_answer


def _moment(now: datetime, zone: ZoneInfo | None) -> JsonObject:
    """``now`` as the judge reads it: the weekday, the date (``YYYY-MM-DD``), the hour and the
    minute in the household's time zone (``zone`` its name), else in UTC (``"zone": None``)."""
    local = now.astimezone(UTC if zone is None else zone)
    today = today_of(now, zone)
    return {
        "weekday": today.weekday,
        "date": today.day.isoformat(),
        "hour": local.hour,
        "minute": local.minute,
        "zone": None if zone is None else zone.key,
    }


async def build_case(
    session: AsyncSession,
    *,
    actor: Actor,
    asks: Person | None,
    action: DescribedAction,
    says: str,
    rights: RightsSay,
    conversation_id: UUID,
    now: datetime,
    zone: ZoneInfo | None,
    idea: IdeaTold,
    gathered: Gathered,
    steps: Sequence[StepTold] = (),
    request: RequestTold | None = None,
    owner_rule: JsonObject | None = None,
    speaker: Speaker | None = None,
    words_override: Sequence[str] | None = None,
    cost: JsonObject | None = None,
) -> JudgeCase:
    """The case of ``action``, judged as ``actor`` in the conversation ``conversation_id`` at
    ``now`` (``zone`` the household's time zone), as the database holds it on ``session``: one
    function for every judged action, a person's request and the assistant's own act alike.

    ``asks`` is the person whose turn it is (``None`` in a turn he acts in on his own, where
    nobody asks). The case names them (their display name and role; the assistant's name and
    ``actor``'s role when nobody asks) and reads their words: their last messages of this
    conversation, what the assistant had just said to them (``asked_them``), and their requests
    that wait in it (their own in their words, those that wait for someone else's yes as another
    model is told of them: a note without its words, D4); none when nobody asks. Its language is
    theirs (the house's while they have none, and when nobody asks). The history is what
    ``actor`` did of this kind: the assistant's own (``policy.facts.domo_judge_history``) when he
    is judged, else the person's.

    ``says`` is the action in words (``describe_for_people``, in English), ``rights`` what the
    barrier said, ``idea`` whose idea it is, ``gathered`` what the assistant knows that bears on
    it (``gather``, read apart by the caller), ``steps`` a chain's steps, ``request`` a request
    changed or taken up again, ``owner_rule`` the owner's own instruction about this action,
    ``speaker`` who speaks (told to the judge as ``Speaker.as_json`` at ``now``; when nobody asks,
    only that nobody spoke), ``cost`` what it
    costs when it is known. It reads only.

    ``words_override``, in a turn a plan woke Domo for (spec 6.9), is what the person said
    instead: the exact words the plans due keep (none for a plan he made alone), each cut at
    ``JUDGE_MESSAGE_CHARS``; their last messages are not read then, nor what Domo had said
    before them (``asked_them``): a "yes" of theirs there answers nothing of this turn."""
    settings = await load_policy_settings(session)
    if asks is None:
        person: JsonObject = {"name": DOMO_NAME, "role": actor.role}
        language = settings.house_language
        words: tuple[str, ...] = ()
        had_said = None
        pending: tuple[str, ...] = ()
    else:
        name, role, their_language = (
            await session.execute(
                select(Person.display_name, Person.role, Person.language).where(
                    Person.id == asks.id
                )
            )
        ).one()
        # Their role as the barrier read it when the call is theirs; as it is now otherwise.
        person = {"name": name, "role": actor.role if actor.person_id == asks.id else role}
        language = their_language or settings.house_language
        if words_override is None:
            words = await _persons_words(session, asks.id, conversation_id)
            had_said = await _what_domo_had_said(session, asks.id, conversation_id)
        else:
            words = tuple(said[:JUDGE_MESSAGE_CHARS] for said in words_override)
            had_said = None
        own = await pending_for(session, asks.id, conversation_id, settings, now)
        for_others = await pending_for_others(session, asks.id, conversation_id, settings, now)
        pending = (
            *(request_description(c, REFERENCE) for c in own),
            *(description_for_a_model(c) for c in for_others),
        )
    history = await history_of(session, actor, action.action_type, now)
    others_said, owner_said, rights = _marked_where_it_counts(
        gathered.others_said, gathered.owner_said, rights
    )
    return JudgeCase(
        person=person,
        action=_action_told(action, says),
        rights=rights,
        words=words,
        asked_them=None if had_said is None else had_said[0],
        relayed=had_said is not None and had_said[1],
        history=history,
        now=_moment(now, zone),
        household=tuple(await household_people(session, now)),
        pending=pending,
        language=language,
        steps=tuple(steps),
        request=request,
        owner_rule=owner_rule,
        speaker=_speaker_told(speaker, asks, now),
        remembered=gathered.remembered,
        others_said=others_said,
        owner_said=owner_said,
        lessons=gathered.lessons,
        trust=gathered.trust,
        went_wrong=gathered.went_wrong,
        subject=gathered.subject,
        idea=idea,
        cost=cost,
        missing=gathered.missing,
    )


def _marked_where_it_counts(
    others_said: tuple[KnownLine, ...], owner_said: tuple[KnownLine, ...], rights: RightsSay
) -> tuple[tuple[KnownLine, ...], tuple[KnownLine, ...], RightsSay]:
    """The lines of others and of the owner, and what the barrier said, as the case holds them:
    the ``may_agree`` marks only where an adult's agreement is left to the judge
    (``rights.needs`` ``adult`` with ``judge_decides``), and nowhere else; and that agreement is
    left to the judge only when at least one line is marked: without an adult's words already
    heard, the adult is asked, whatever the judge would say."""
    if rights.needs == "adult" and rights.judge_decides:
        if any(line.may_agree for line in (*others_said, *owner_said)):
            return others_said, owner_said, rights
        rights = replace(rights, judge_decides=False)

    def unmarked(lines: tuple[KnownLine, ...]) -> tuple[KnownLine, ...]:
        return tuple(replace(line, may_agree=False) for line in lines)

    return unmarked(others_said), unmarked(owner_said), rights


def _speaker_told(speaker: Speaker | None, asks: Person | None, now: datetime) -> JsonObject | None:
    """Who speaks, as the judge is told (``Speaker.as_json`` at ``now``): when nobody asks, only
    that nobody spoke, never the clues of someone whose context he acts in."""
    if speaker is None or (asks is None and not speaker.nobody_spoke):
        return None
    return speaker.as_json(now)


def _facts_for_the_judge(action: DescribedAction) -> JsonObject:
    """The facts of ``action`` as the judge reads them: all but what it never reads."""
    return {fact: value for fact, value in action.facts.items() if fact not in NOT_FOR_THE_JUDGE}


def _action_told(action: DescribedAction, says: str) -> JsonObject:
    """The action as the judge reads it: its type, target, facts and what it does in words."""
    return {
        "type": action.action_type,
        "target": action.target,
        "facts": _facts_for_the_judge(action),
        "says": says,
    }


def step_told(row: ConfirmationStep) -> StepTold:
    """A stored step of a chain, as the judge reads it (``describe.step_sentence``, in English)."""
    return StepTold(
        type=row.action_type,
        says=step_sentence(row, REFERENCE),
        checked_in_turn=row.action is None,
        waits_for=row.after_arrival_of,
    )


def planned_step_told(step: PlannedStep) -> StepTold:
    """A step of a chain being planned, as the judge reads it: as its own request would be told,
    or, checked in its turn, the sentence of its type with the words of its call and a word that
    says so (the general sentence only when the words do not fill it); with the owner's "never ask
    me" for it, if he gave one (``PlannedStep.never_ask``)."""
    if step.action is None:
        says = describe_type(step.action_type, step.arguments, REFERENCE) + _CHECKED_IN_TURN
    else:
        says = describe_for_people(step.action, step.arguments, REFERENCE)
    return StepTold(
        type=step.action_type,
        says=says,
        checked_in_turn=step.action is None,
        waits_for=step.after_arrival_of,
        owner_rule=owner_rule_of(step.never_ask),
    )


# --- the elements of the case ("Domo judges first") ----------------------------------------------

# How many lines each element of the case holds at most, and how many characters each line keeps.
CASE_LINES: Final = 5
CASE_LINE_CHARS: Final = 300
# How many steps of a chain are told what is known of them (the first ones), and how many lines
# of each element a step holds.
CASE_STEPS: Final = 8
STEP_LINES: Final = 3
# How many of a person's last trust facts the case gives.
TRUST_FACTS: Final = 5
# How long gathering may take when the caller set no time of its own.
CASE_SECONDS: Final = 6.0
# How many memories the search brings before they are sorted by author.
_SEARCHED: Final = 50
# Each element's share of the time left, in this order: the memories, whose search may wait on
# the model of the meanings, get the larger one (more than that wait itself when the caller set
# no time: ``memory.search.QUERY_SECONDS``).
_SHARES: Final[Mapping[str, int]] = {"remembered": 2, "trust": 1, "went_wrong": 1, "subject": 1}


def _cut(text: str) -> str:
    return text[:CASE_LINE_CHARS]


def _day(at: datetime, zone: ZoneInfo | None) -> str:
    return at.astimezone(UTC if zone is None else zone).date().isoformat()


def _minute(at: datetime, zone: ZoneInfo | None) -> str:
    return at.astimezone(UTC if zone is None else zone).strftime("%Y-%m-%d %H:%M")


async def _names(session: AsyncSession, ids: Collection[UUID | None]) -> dict[UUID, str]:
    """The display name of each of ``ids`` still in the database."""
    wanted = {person_id for person_id in ids if person_id is not None}
    if not wanted:
        return {}
    rows = await session.execute(
        select(Person.id, Person.display_name).where(Person.id.in_(wanted))
    )
    return {row.id: row.display_name for row in rows}


def _author(
    memory: Memory, about: UUID | None, owner: UUID | None, told_of: UUID | None = None
) -> Author:
    """Who a memory is from, read from its columns only: the assistant's conclusion (he wrote it,
    or it is a deduction); the words of the person who asks (said in their turn, their own words
    or what he told them); someone else's words (another's, or relayed from someone or from
    outside); the owner's; anyone else's."""
    if memory.by_domo or memory.origin == "deduced":
        return "domo"
    if memory.origin == "told_by_domo" and told_of is not None and memory.said_by_id == told_of:
        # What he told the person the thing belongs to: what was agreed with them (ruling R69).
        return "them"
    if (
        about is not None
        and memory.said_by_id == about
        and memory.origin in ("own_words", "told_by_domo")
    ):
        return "them"
    if memory.origin in ("others_words", "external"):
        return "other"
    if owner is not None and memory.said_by_id == owner:
        return "owner"
    return "other"


def _told_someone_else(memory: Memory, told_of: UUID | None) -> bool:
    """Whether ``memory`` is what the assistant told someone other than the person who asks
    (``told_by_domo`` said in another's turn): his words to them and their answer, about their
    own request; never that person's words, and not part of this case."""
    return memory.origin == "told_by_domo" and (told_of is None or memory.said_by_id != told_of)


def _a_lesson_about(
    memory: Memory, action: DescribedAction, subject: Subject, about: UUID | None
) -> bool:
    """Whether ``memory`` is a lesson about this action (its type or the thing) for the person who
    asks (said in their turn or about them) or for everyone."""
    about_this = memory.lesson_action_type == action.action_type or (
        subject.key is not None and memory.lesson_subject_key == subject.key
    )
    for_them = memory.for_everyone or (
        about is not None and about in (memory.said_by_id, memory.about_id)
    )
    return memory.kind == "lesson" and about_this and for_them


def _lessons_about(
    action: DescribedAction, subject: Subject, about: UUID | None
) -> ColumnElement[bool]:
    """``_a_lesson_about`` as a condition, for the assistant's own lessons."""
    about_this = [Memory.lesson_action_type == action.action_type]
    if subject.key is not None:
        about_this.append(Memory.lesson_subject_key == subject.key)
    for_them = [Memory.for_everyone.is_(True)]
    if about is not None:
        for_them += [Memory.said_by_id == about, Memory.about_id == about]
    return (
        (Memory.kind == "lesson")
        & or_(of_domo(), Memory.origin == "deduced")
        & or_(*about_this)
        & or_(*for_them)
    )


def _known_line(
    memory: Memory,
    by: Author,
    names: Mapping[UUID, str],
    zone: ZoneInfo | None,
    may_agree: frozenset[UUID] = frozenset(),
    about: UUID | None = None,
) -> KnownLine:
    """``memory`` as a line of the case, by ``by`` (``who`` from ``names``), marked
    ``may_agree`` when it is the own words (``own_words``, never relayed nor his) of one of
    ``may_agree``, whose content is itself a passage of their message, checked word for word
    (``Memory.words_checked``, rulings R73, R79: never words the model wrote), about the one
    who asks (``about``, ruling R66: a line about
    someone else, or about nobody, is never marked)."""
    told = memory.origin == "told_by_domo"
    return KnownLine(
        content=_cut(memory.content),
        when=_day(memory.changed_at, zone),
        by=by,
        who=(
            # Kept from a program's text when nobody wrote: never the name of the person whose
            # conversation it was.
            A_PROGRAM_OR_A_TOOL
            if by == "other" and memory.origin == "external"
            else names.get(memory.said_by_id)
            if by in ("other", "owner") and memory.said_by_id is not None
            else None
        ),
        unconfirmed=unconfirmed(memory),
        answer=memory.told_answer if told else None,
        keep_asking=memory.told_keep_asking if told else None,
        may_agree=(
            by in ("other", "owner")
            and memory.origin == "own_words"
            and memory.words_checked
            and not memory.by_domo
            and memory.said_by_id in may_agree
            and about is not None
            and memory.about_id == about
        ),
    )


async def _who_may_agree(
    session: AsyncSession, ids: Collection[UUID | None], about: UUID | None, now: datetime
) -> frozenset[UUID]:
    """Those of ``ids`` who may say yes for ``about`` (``may_agree_for_a_child``, the rule the
    confirmations check too) and are in the household at ``now``; nobody when nobody asks."""
    wanted = {person_id for person_id in ids if person_id is not None}
    if about is None or not wanted:
        return frozenset()
    rows = await session.scalars(select(Person).where(Person.id.in_(wanted)))
    return frozenset(
        person.id
        for person in rows
        if is_active(person, now) and may_agree_for_a_child(person.role, person.id, about)
    )


Known = tuple[
    tuple[KnownLine, ...], tuple[KnownLine, ...], tuple[KnownLine, ...], tuple[KnownLine, ...]
]


async def _known(
    session: AsyncSession,
    *,
    about: UUID | None,
    private_of: UUID,
    action: DescribedAction,
    subject: Subject,
    words: str,
    embedder: Embedder,
    zone: ZoneInfo | None,
    now: datetime,
    told_of: UUID | None,
) -> Known:
    """The memories of the case, sorted by author (``_author``), ``CASE_LINES`` each: what he
    told ``told_of`` before such an action (the person whose case it is), and the lessons about
    it, read first; then what the search finds from ``words``. Every query under
    ``visible_to(private_of)``."""
    visible = visible_to(private_of)
    first: list[Memory] = []
    if told_of is not None:
        first += (
            await session.scalars(
                select(Memory)
                .where(
                    visible,
                    Memory.said_by_id == told_of,
                    Memory.origin == "told_by_domo",
                    Memory.action_type == action.action_type,
                    Memory.by_domo.is_(False),
                )
                .order_by(Memory.changed_at.desc(), Memory.id)
                .limit(CASE_LINES)
            )
        ).all()
    first += (
        await session.scalars(
            select(Memory)
            .where(visible, _lessons_about(action, subject, about))
            .order_by(Memory.changed_at.desc(), Memory.id)
            .limit(CASE_LINES)
        )
    ).all()
    found = await search_for_case(session, private_of, words, embedder=embedder, limit=_SEARCHED)
    try:
        owner: UUID | None = await owner_of_now(session, now)
    except LookupError:
        owner = None
    sorted_out: dict[Author, list[Memory]] = {"them": [], "other": [], "owner": [], "domo": []}
    seen: set[UUID] = set()
    for memory in (*first, *found):
        if _told_someone_else(memory, told_of):
            continue
        if memory.id not in seen:
            seen.add(memory.id)
            sorted_out[_author(memory, about, owner, told_of)].append(memory)
    domo = sorted_out["domo"]
    sorted_out["domo"] = [
        *(m for m in domo if _a_lesson_about(m, action, subject, about)),
        *(m for m in domo if not _a_lesson_about(m, action, subject, about)),
    ]
    speakers = [m.said_by_id for m in (*sorted_out["other"], *sorted_out["owner"])]
    names = await _names(session, speakers)
    may_agree = await _who_may_agree(session, speakers, about, now)

    def of_the_concerned(memory: Memory) -> bool:
        # What he told the person the thing belongs to, in a case that is not theirs to ask.
        return (
            memory.origin == "told_by_domo"
            and told_of is not None
            and told_of != about
            and memory.said_by_id == told_of
        )

    def lines(by: Author) -> tuple[KnownLine, ...]:
        return tuple(
            replace(line, concerned=True) if of_the_concerned(m) else line
            for m in sorted_out[by][:CASE_LINES]
            for line in (_known_line(m, by, names, zone, may_agree, about),)
        )

    return lines("them"), lines("other"), lines("owner"), lines("domo")


async def _trust(
    session: AsyncSession, person_id: UUID, start: int, zone: ZoneInfo | None
) -> JsonObject:
    """The person's trust: the figure, how many facts it rests on, and their ``TRUST_FACTS``
    newest facts, the newest first, each with how it was established (``how``: confirmed, seen or
    reported, ``TrustFact.established``)."""
    figure = await trust_of(session, person_id, start=start)
    facts = await facts_of(session, person_id, limit=TRUST_FACTS)
    return {
        **figure.as_json(),
        "lately": [
            {
                "outcome": fact.outcome,
                "how": fact.established,
                "about": _cut(fact.about),
                "when": _day(fact.created_at, zone),
            }
            for fact in facts
        ],
    }


async def _went_wrong(
    session: AsyncSession,
    *,
    about: UUID | None,
    private_of: UUID,
    action: DescribedAction,
    subject: Subject,
    zone: ZoneInfo | None,
) -> tuple[WentWrongLine, ...]:
    """What went wrong before (``learning.read.went_wrong_for``), ``CASE_LINES`` at most: on the
    same thing first, then of this type for the person who asks, or, for an act of the
    assistant's with nobody behind it, of this type and his. A reason said by someone else than
    the person whose case it is (``private_of``) is never given: only that it happened, by whom
    and on what (ruling R76)."""
    rows = await went_wrong_for(
        session,
        action_type=action.action_type,
        subject_key=subject.key,
        about=about,
        of_domo=True if about is None else None,
        limit=CASE_LINES,
    )
    names = await _names(session, [row.by_person_id for row in rows])
    return tuple(
        WentWrongLine(
            kind=row.kind,
            when=_minute(row.at, zone),
            by=(
                DOMO_NAME
                if row.by_domo
                else None
                if row.by_person_id is None
                else names.get(row.by_person_id)
            ),
            of_domo=row.of_domo,
            reason=(
                None
                if row.reason is None or _said_by_someone_else(row, private_of)
                else _cut(row.reason)
            ),
            same_subject=subject.key is not None and row.subject_key == subject.key,
        )
        for row in rows
    )


def _said_by_someone_else(row: WentWrong, private_of: UUID) -> bool:
    """Whether the reason of ``row`` was said by a person other than ``private_of``."""
    return row.by_person_id is not None and row.by_person_id != private_of


async def _subject_state(
    session: AsyncSession, subject: Subject, zone: ZoneInfo | None, private_of: UUID
) -> SubjectState | None:
    """The last action done on the thing (``message_actions`` of the same subject key, joined to
    its conversation's person), with the reason of what went wrong on it, if any, unless someone
    else than ``private_of`` said it (ruling R76); ``None`` when the thing is not known or
    nothing was done on it."""
    if subject.key is None:
        return None
    row = (
        await session.execute(
            select(MessageAction, Person.display_name)
            .join(Message, Message.id == MessageAction.message_id)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .join(Person, Person.id == Conversation.person_id)
            .where(MessageAction.subject_key == subject.key, MessageAction.status == "done")
            .order_by(MessageAction.created_at.desc(), MessageAction.id)
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    done, name = row
    reason = await session.scalar(
        select(WentWrong.reason)
        .where(
            WentWrong.action_id == done.id,
            WentWrong.reason.is_not(None),
            or_(WentWrong.by_person_id.is_(None), WentWrong.by_person_id == private_of),
        )
        .order_by(WentWrong.at.desc(), WentWrong.id)
        .limit(1)
    )
    return SubjectState(
        key=subject.key,
        last=done.action_type,
        by=DOMO_NAME if done.by_domo else name,
        when=_minute(done.created_at, zone),
        reason=None if reason is None else _cut(reason),
    )


class _Within:
    """Runs each element under its share of the caller's time (``_SHARES``), each in a savepoint
    of the caller's session (released when it read, rolled back when it failed or was late),
    and keeps the names of those that were late or failed (``missing``)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.weights_left = sum(_SHARES.values())
        self.missing: list[str] = []

    async def run[T](self, name: str, work: Callable[[], Awaitable[T]]) -> T | None:
        """``work``'s result, or ``None`` (and ``name`` missing) when it fails or is late."""
        left = anyio.current_effective_deadline() - anyio.current_time()
        seconds = CASE_SECONDS if math.isinf(left) else max(left, 0.0)
        share = seconds * _SHARES[name] / self.weights_left
        self.weights_left -= _SHARES[name]
        result: T | None = None
        done = False
        try:
            savepoint = await self.session.begin_nested()
        except Exception as error:
            logger.warning("The case could not read %s (%s)", name, type(error).__name__)
            self.missing.append(name)
            return None
        try:
            with anyio.move_on_after(share):
                try:
                    result = await work()
                    done = True
                except Exception as error:
                    logger.warning("The case could not read %s (%s)", name, type(error).__name__)
        finally:
            # Shielded: a share that ran out still closes its savepoint, so that what failed or
            # was cut in it leaves the caller's transaction as it was.
            with anyio.CancelScope(shield=True):
                try:
                    if done:
                        await savepoint.commit()
                    else:
                        await savepoint.rollback()
                except Exception as error:
                    done = False
                    logger.warning(
                        "The case could not close its reading of %s (%s)",
                        name,
                        type(error).__name__,
                    )
        if not done:
            self.missing.append(name)
        return result if done else None


async def gather(
    session: AsyncSession,
    *,
    about: UUID | None,
    private_of: UUID,
    action: DescribedAction,
    subject: Subject,
    says: str,
    extra_query: str | None,
    embedder: Embedder,
    zone: ZoneInfo | None,
    now: datetime,
    start_trust: int,
    with_trust: bool = True,
    his_own: bool = False,
) -> Gathered:
    """What the assistant knows that bears on ``action`` (``says``: the action in words; its
    ``subject``), as the case holds it. ``about`` is the person who asks (``None`` for an act of
    the assistant's with nobody behind it); ``private_of`` is whose private memories may enter
    (the person who asks; for an idea of his, the person concerned). What he told someone before
    such an action is read for the person who asks, or, for his own case (``his_own``), for the
    person concerned (``private_of``) alone, never the one who asks (ruling R69): a told line of
    someone never enters a case that is not theirs.

    The memories are those the search finds from ``says``, the subject's key and ``extra_query``,
    with what he told the person who asks before such an action and the lessons about it, all
    under ``visible_to(private_of)``, sorted by author (``_author``); then the person's trust
    (``start_trust`` for a person with no fact; left unread when ``with_trust`` is false), what went
    wrong before, and the state of the
    thing. Each element has its share of the caller's time (``CASE_SECONDS`` when it set none),
    its own ``try`` and its own savepoint of ``session``: one late or failing is left empty and
    named in ``missing``. Reads only (opening a savepoint flushes what the caller left pending);
    never raises.

    What the caller's session is in afterwards: an element that failed in the database (an error
    of a query) is rolled back to its savepoint, so the transaction is as it was and the other
    elements read as usual. An element whose share ran out while it waited outside the database
    (the model of the meanings) is rolled back the same way. But one cut while a query of its own
    was running leaves the connection unusable (the driver cannot take the query back): its
    savepoint cannot be closed, every later element is named in ``missing`` too, and the session
    must then be rolled back or closed by the caller, never committed. Hence the caller gives
    ``gather`` a short read-only transaction of its own, never one that holds work to keep."""
    within = _Within(session)
    words = " ".join(part for part in (says, subject.key, extra_query) if part)
    known = await within.run(
        "remembered",
        lambda: _known(
            session,
            about=about,
            private_of=private_of,
            action=action,
            subject=subject,
            words=words,
            embedder=embedder,
            zone=zone,
            now=now,
            told_of=private_of if his_own else (about if about == private_of else None),
        ),
    )
    remembered, others_said, owner_said, lessons = known or ((), (), (), ())
    trust = (
        None
        if about is None or not with_trust
        else await within.run("trust", lambda: _trust(session, about, start_trust, zone))
    )
    if about is None or not with_trust:
        within.weights_left -= _SHARES["trust"]
    went_wrong = await within.run(
        "went_wrong",
        lambda: _went_wrong(
            session,
            about=about,
            private_of=private_of,
            action=action,
            subject=subject,
            zone=zone,
        ),
    )
    state = await within.run("subject", lambda: _subject_state(session, subject, zone, private_of))
    return Gathered(
        remembered=remembered,
        others_said=others_said,
        owner_said=owner_said,
        lessons=lessons,
        trust=trust,
        went_wrong=went_wrong or (),
        subject=state,
        missing=tuple(within.missing),
    )


def known_of_a_step(
    told: StepTold,
    action: DescribedAction,
    *,
    gathered: Gathered,
    history: PersonHistory,
    now: datetime,
    zone: ZoneInfo | None,
) -> StepTold:
    """``told`` (a step of a chain with a described ``action``) with what is known of it: its
    facts, its ``history`` (``history_told``, in the zone of the case's ``now``) and what
    ``gather`` read for it, each element ``STEP_LINES`` lines at most. What is remembered
    (``remembered``) is the person's own words only, as in the case: what the owner or others said
    stays out of a step, so that nothing else can pass for what the person asked or knows."""
    return replace(
        told,
        facts=_facts_for_the_judge(action),
        history=history_told(history, _moment(now, zone)),
        went_wrong=gathered.went_wrong[:STEP_LINES],
        lessons=gathered.lessons[:STEP_LINES],
        remembered=gathered.remembered[:STEP_LINES],
        subject=gathered.subject,
    )
