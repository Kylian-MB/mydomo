"""The yes-judge (spec 4): whether an action the rights allow is done at once, or what is asked
first.

Like the classifier of the replies (``confirmations.intent``), it is an isolated call to the
household's model, with a prompt of its own (``prompts/yes_judge.md``): no tool, no history of the
conversation, a short answer read strictly. What it reads is a case (``JudgeCase``) that the code
builds from the database (``build_case``), never copied from what the model that talks read: who
asks, the action as the barrier sees it and in words, what the rights say, the person's last
messages of this conversation (only theirs: provenance ``person``), what Domo had just said to
them before the last one (``asked_them``: what a "yes" of theirs answers), what they did of this
kind before (``policy.facts.person_history``), the moment, who is in the household, and their
requests that wait (a waiting note without its words, D4). ``asked_them`` is that one reply and no
other, as the person read it. When it is not Domo's own answer to a message of theirs written in
a turn given nothing from outside (``Message.own_answer`` false: it told them a note someone left
or a request of someone else, or Domo wrote it on his own, at a wake or an arrival), the case
says so (``relayed``): the owner's decision of 2026-11-17 is that a "yes" to what Domo just told
them is understood all the same, but only for an action that reply itself plainly names. So the
judge never sees Domo's other replies, the results of the tools, an outside content, a secret,
the private words of someone else or the words of a note themselves (those reach their Domo
alone, as data, spec 4.8): only what Domo last said to the person, as they read it. Everything
in it is data, never instructions.

It answers one of six verdicts (``VERDICTS``) with a short reason, on one line
(``read_judgement``): ``do``, ``yes``, ``warn``, ``explicit_yes``, ``proof``, ``do_not``. ``warn``
is a ``yes`` everywhere a verdict decides (``Judgement.as_ask``); it says that the reason is
something the person must know first (today: that it cannot be undone), not a doubt about what
they want, and the request keeps that (``Asked.warned``): their answer to it, yes or no, is
remembered as what Domo told them (``memory.told``), and the judge reads it the next time
(``remembered``). The audit keeps ``warn``. When it does not
answer (no model, an error, too long, an answer it cannot read), the verdict is ``do``
(``fallback``): what a person asked for and Domo understood is done, and nothing in the code
asks them again (the owner's decision of 2026-11-15); the rights, someone else's agreement and the
owner's own rules do not come from the judge and hold all the same. That is for a request the
person made: when the turn answers no message of theirs (a wake, an arrival: Domo acts on his
own) or was given words from outside (a note someone left them), nobody is known to have asked,
and a judge that does not answer means a ``yes`` (``they_asked`` false:
``conversation.execution.consult_the_judge``). An answer that is no verdict
is the model's mistake: it is a lesson about the
judge's model (``LearnsFromFailures``, ``llm.lessons.UNREADABLE_VERDICT``), which Domo sees in
his context; it never changes which models may judge.

What is asked in the end is the most demanding of the judge's verdict, the owner's rule ("always
ask me before...") and what Domo asks more (``what_to_ask``): no type asks anything by default.
A request changed in words, or a chain taken up again, is
judged again with its history (``RequestTold``, which ``confirmations.chain.request_told``
builds), and what it asks then is ``ask_after_a_change``: words never give an explicit yes nor a
proof, and a yes already given covers what it was given to. ``do_not`` is not on that scale: the
action is refused. The verdict is audited with its reason (``audit_judgement``).
"""

import json
from collections.abc import Sequence
from contextlib import aclosing
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from functools import cache
from importlib import resources
from typing import Final, Literal, Protocol, cast
from uuid import UUID
from zoneinfo import ZoneInfo

import anyio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.audit.log import append_audit
from domo_core.chat.models import Conversation, Message
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
from domo_core.conversation.speaker import Speaker
from domo_core.llm.lessons import UNREADABLE_VERDICT
from domo_core.llm.tasks import TaskKind
from domo_core.llm.types import (
    ChatChunk,
    ChatMessage,
    ChatModel,
    JsonObject,
    ModelCallError,
    ToolCall,
)
from domo_core.memory.models import Memory
from domo_core.policy.actions import DescribedAction
from domo_core.policy.asks import Ask, Asked, NeverAsk, most_demanding, most_demanding_asked
from domo_core.policy.facts import (
    DOMO_NAME,
    PersonHistory,
    domo_judge_history,
    household_people,
    person_history,
)
from domo_core.policy.roles import Actor
from domo_core.policy.settings import load_policy_settings
from domo_core.prompts import sendable, today_of
from domo_core.text import is_plain_line
from domo_core.texts.view import REFERENCE

Verdict = Literal["do", "yes", "warn", "explicit_yes", "proof", "do_not"]
# From "do it" to "do not": ``do``, ``yes``, ``explicit_yes`` and ``proof`` are a scale, ``warn``
# is a ``yes`` to the code (it says that the reason is something the person must know first), and
# ``do_not`` is not on it.
VERDICTS: Final[tuple[Verdict, ...]] = ("do", "yes", "warn", "explicit_yes", "proof", "do_not")
# Why the judge did not answer: no model to ask, the call failed, it took too long, or what it
# said is no verdict.
Fallback = Literal["no_model", "error", "timeout", "unreadable"]

# How long the judge may take before its verdict is the fallback's.
JUDGE_SECONDS: Final = 20.0
# How many of the person's last messages the judge reads, each cut at that many characters.
JUDGE_MESSAGES: Final = 5
JUDGE_MESSAGE_CHARS: Final = 1000
# The judge answers on one short line, asked without thinking first (``ChatModel.stream``): the
# line itself took 20 to 45 tokens with a real model, in French (2026-11-15). The room is for a
# model that thinks anyway (one that cannot be told not to, or of a provider whose adapter does
# not ask): those runs took 150 to 330 tokens, thinking included, and 80 cut most of them before
# the verdict. A ceiling, not a cost: only what is written is paid.
JUDGE_OUTPUT_TOKENS: Final = 500
# The longest reason kept (the column ``ask_why`` of a request).
MAX_JUDGE_REASON_LENGTH: Final = 200

# The facts of an action the judge never reads: ``owner_first`` (``household.invite``) is
# Domo's own judgement that the owner says yes first, for the barrier; the judge would take it
# for a doubt about the request. The barrier, the limit and the audit keep it.
NOT_FOR_THE_JUDGE: Final = frozenset({"owner_first"})

# What a step checked in its turn says after its sentence (it has no described action yet).
_CHECKED_IN_TURN: Final = " (checked in its turn)"


@dataclass(frozen=True)
class Judgement:
    """What the judge said: its ``verdict`` and its ``reason`` (one plain line, 200 characters at
    most; ``""`` when none was given). ``fallback`` is set when the judge did not answer: the
    verdict is then ``do``, or ``yes`` when the person is not known to have asked
    (``fallback``)."""

    verdict: Verdict
    reason: str = ""
    fallback: Fallback | None = None

    @property
    def source(self) -> Literal["model", "fallback"]:
        """Where the verdict comes from: the judge's model, or the fallback."""
        return "model" if self.fallback is None else "fallback"

    def as_ask(self) -> Ask | None:
        """What the verdict asks of the person: nothing for ``do``, the ask itself for ``yes``,
        ``explicit_yes`` and ``proof``, a ``yes`` for ``warn``, and nothing for ``do_not`` (it is
        refused, not asked)."""
        match self.verdict:
            case "yes" | "explicit_yes" | "proof":
                return self.verdict
            case "warn":
                return "yes"
            case "do" | "do_not":
                return None

    @property
    def warns(self) -> bool:
        """Whether the judge asks the yes only to tell the person something they must know first
        (``warn``), and said it (a reason): the request is ``warned``, and their answer to it is
        remembered (``memory.told``)."""
        return self.verdict == "warn" and bool(self.reason)


def fallback(why: Fallback, *, they_asked: bool = True) -> Judgement:
    """The verdict when the judge did not answer, with no reason (never a ``do_not``): ``do``
    when the person asked (``they_asked``: the turn answers a message of theirs and was given
    nothing from outside): the request was asked for and understood, and a judge that is silent
    asks nothing. ``yes`` otherwise: nobody is known to have asked for what Domo is about to do
    (he acts on his own, or was told words that are not theirs), so they say yes first."""
    return Judgement("do" if they_asked else "yes", "", why)


@dataclass(frozen=True)
class RightsSay:
    """What the barrier said, for the judge: the action is ``allowed``; ``needs`` is whose yes the
    rights require first (``adult`` or ``owner``), ``None`` when nobody else's; ``question`` is
    what a limit could not tell, when one sent the request to the owner."""

    allowed: bool
    needs: Literal["adult", "owner"] | None
    question: str | None


@dataclass(frozen=True)
class StepTold:
    """One step of a chain, as the judge reads it: its ``type``, what it does in words (``says``),
    whether it is checked in its turn (an earlier step changes what it touches: it has no
    described action yet), whose arrival it waits for (``waits_for``, the invitation's name), and
    the owner's own instruction about it (``owner_rule``, as ``JudgeCase.owner_rule``)."""

    type: str
    says: str
    checked_in_turn: bool
    waits_for: str | None
    owner_rule: JsonObject | None = None

    def as_json(self) -> JsonObject:
        """The step as the case holds it (``owner_rule`` only when there is one)."""
        told: JsonObject = {
            "type": self.type,
            "says": self.says,
            "checked_in_turn": self.checked_in_turn,
            "waits_for": self.waits_for,
        }
        if self.owner_rule is not None:
            told["owner_rule"] = dict(self.owner_rule)
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


@dataclass(frozen=True)
class RememberedLine:
    """A memory the judge reads (``memory.told.for_judge``): what it says (``content``), the day
    it was last written (``when``, ``YYYY-MM-DD`` in the household's time zone), and where it
    comes from (``origin``: the person's own words, or what Domo told them)."""

    content: str
    when: str
    origin: str


def remembered_line(memory: Memory, zone: ZoneInfo | None) -> RememberedLine:
    """``memory`` as the judge reads it, its day in ``zone`` (UTC when ``None``)."""
    day = memory.changed_at.astimezone(UTC if zone is None else zone).date()
    return RememberedLine(content=memory.content, when=day.isoformat(), origin=memory.origin)


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
    identifier), ``None`` when nobody is said to speak. ``remembered`` is what the person said
    before and what Domo already told them that touches on the action (``RememberedLine``,
    from their own memories only: ``memory.told.for_judge``). ``language`` is
    the language of the one who asks (a tag: ``en``, ``pt-BR``...): the judge writes its reason
    in it, since the reason is shown to them as it is (spec 4.3)."""

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
    remembered: tuple[RememberedLine, ...] = ()

    def as_json(self) -> JsonObject:
        """The case as the judge reads it: plain JSON, every text made sendable
        (``prompts.sendable``). ``history`` says ``first_time``, and the day of the ``last`` one
        (``_last_time``: ``YYYY-MM-DD`` in the zone of ``now``, with ``days_ago``; ``None`` and no
        ``days_ago`` when there is none). ``steps``, ``request``, ``owner_rule``, ``speaker``,
        ``asked_them`` and ``remembered`` are there only when they are given, and ``relayed``
        (``true``) only with ``asked_them`` when that reply relayed someone or spoke first."""
        history = self.history
        case: JsonObject = {
            "person": dict(self.person),
            "action": dict(self.action),
            "rights": asdict(self.rights),
            "words": list(self.words),
            "history": {
                "day": history.day,
                "week": history.week,
                "month": history.month,
                "ever": history.ever,
                **_last_time(history.last_at, self.now),
                "refused_or_dropped": history.refused_or_dropped,
                "first_time": history.first_time,
            },
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
        if self.remembered:
            case["remembered"] = [asdict(line) for line in self.remembered]
        return cast(JsonObject, sendable(case))


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
    session: AsyncSession, actor: Actor, conversation_id: UUID
) -> tuple[str, ...]:
    """The person's last ``JUDGE_MESSAGES`` messages of the conversation, oldest first, each cut
    at ``JUDGE_MESSAGE_CHARS``: what they typed (role ``user``, provenance ``person``), in their
    own conversation only."""
    result = await session.execute(
        select(Message.content)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Message.conversation_id == conversation_id,
            Conversation.person_id == actor.person_id,
            Message.role == "user",
            Message.provenance == "person",
        )
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(JUDGE_MESSAGES)
    )
    return tuple(content[:JUDGE_MESSAGE_CHARS] for content in reversed(result.scalars().all()))


async def _what_domo_had_said(
    session: AsyncSession, actor: Actor, conversation_id: UUID
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
            Conversation.person_id == actor.person_id,
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
    action: DescribedAction,
    says: str,
    rights: RightsSay,
    conversation_id: UUID,
    now: datetime,
    zone: ZoneInfo | None,
    steps: Sequence[StepTold] = (),
    request: RequestTold | None = None,
    owner_rule: JsonObject | None = None,
    speaker: Speaker | None = None,
    remembered: Sequence[RememberedLine] = (),
    words_override: Sequence[str] | None = None,
) -> JudgeCase:
    """The case of ``action``, asked by ``actor`` in the conversation ``conversation_id`` at
    ``now`` (``zone`` the household's time zone), as the database holds it on ``session``.

    ``says`` is the action in words (``describe_for_people``, in English), ``rights`` what the
    barrier said, ``steps`` a chain's steps, ``request`` a request changed or taken up again, and
    ``owner_rule`` the owner's own instruction about this action, ``speaker`` who speaks (told to
    the judge as ``Speaker.as_json`` at ``now``), ``remembered`` the memories the judge may read
    (read apart by the caller: ``memory.told.for_judge``). It reads only: the person's
    display name and the language they read (the house's while they have none), their last
    messages of this conversation, what they did of this kind before, who is in the household, and
    their requests that wait in this conversation (their own in their words, those that wait for
    someone else's yes as another model is told of them: a note without its words, D4).

    ``words_override``, in a turn a plan woke Domo for (spec 6.9), is what the person said
    instead: the exact words the plans due keep (none for a plan he made alone), each cut at
    ``JUDGE_MESSAGE_CHARS``; their last messages are not read then, nor what Domo had said
    before them (``asked_them``): a "yes" of theirs there answers nothing of this turn.

    For Domo acting on his own (``roles.domo_actor``, a turn a launch for the house woke him
    for), the case is his: his name, the house's language, nobody's words nor requests (the
    conversation is the owner's, and nobody asked), and what he did of this kind on his own
    (``policy.facts.domo_judge_history``), never the owner's."""
    settings = await load_policy_settings(session)
    if actor.subject == "domo":
        return await _domo_case(
            session,
            actor=actor,
            action=action,
            says=says,
            rights=rights,
            now=now,
            zone=zone,
            language=settings.house_language,
            steps=steps,
            request=request,
            owner_rule=owner_rule,
            remembered=remembered,
        )
    name, language = (
        await session.execute(
            select(Person.display_name, Person.language).where(Person.id == actor.person_id)
        )
    ).one()
    own = await pending_for(session, actor.person_id, conversation_id, settings, now)
    for_others = await pending_for_others(session, actor.person_id, conversation_id, settings, now)
    had_said = (
        await _what_domo_had_said(session, actor, conversation_id)
        if words_override is None
        else None
    )
    return JudgeCase(
        person={"name": name, "role": actor.role},
        action={
            "type": action.action_type,
            "target": action.target,
            "facts": {
                fact: value for fact, value in action.facts.items() if fact not in NOT_FOR_THE_JUDGE
            },
            "says": says,
        },
        rights=rights,
        words=(
            await _persons_words(session, actor, conversation_id)
            if words_override is None
            else tuple(said[:JUDGE_MESSAGE_CHARS] for said in words_override)
        ),
        asked_them=None if had_said is None else had_said[0],
        relayed=had_said is not None and had_said[1],
        history=await person_history(session, actor.person_id, action.action_type, now),
        now=_moment(now, zone),
        household=tuple(await household_people(session, now)),
        pending=(
            *(request_description(c, REFERENCE) for c in own),
            *(description_for_a_model(c) for c in for_others),
        ),
        language=language or settings.house_language,
        steps=tuple(steps),
        request=request,
        owner_rule=owner_rule,
        speaker=None if speaker is None else speaker.as_json(now),
        remembered=tuple(remembered),
    )


def _facts_for_the_judge(action: DescribedAction, says: str) -> JsonObject:
    """The action as the judge reads it: its type, target, facts (but what it never reads) and
    what it does in words."""
    return {
        "type": action.action_type,
        "target": action.target,
        "facts": {
            fact: value for fact, value in action.facts.items() if fact not in NOT_FOR_THE_JUDGE
        },
        "says": says,
    }


async def _domo_case(
    session: AsyncSession,
    *,
    actor: Actor,
    action: DescribedAction,
    says: str,
    rights: RightsSay,
    now: datetime,
    zone: ZoneInfo | None,
    language: str,
    steps: Sequence[StepTold],
    request: RequestTold | None,
    owner_rule: JsonObject | None,
    remembered: Sequence[RememberedLine],
) -> JudgeCase:
    """The case of ``action`` that Domo does on his own (see ``build_case``): nobody speaks
    in such a turn, so no speaker is told."""
    return JudgeCase(
        person={"name": DOMO_NAME, "role": actor.role},
        action=_facts_for_the_judge(action, says),
        rights=rights,
        words=(),
        asked_them=None,
        relayed=False,
        history=await domo_judge_history(session, action.action_type, now),
        now=_moment(now, zone),
        household=tuple(await household_people(session, now)),
        pending=(),
        language=language,
        steps=tuple(steps),
        request=request,
        owner_rule=owner_rule,
        speaker=None,
        remembered=tuple(remembered),
    )


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


@cache
def judge_prompt() -> str:
    """The system prompt of the judge (``prompts/yes_judge.md``), read once."""
    path = resources.files("domo_core.prompts").joinpath("yes_judge.md")
    return path.read_text(encoding="utf-8").strip()


def _reason_of(text: str) -> str:
    """The reason of a verdict: the first line of ``text``, without the spaces around it, cut at
    ``MAX_JUDGE_REASON_LENGTH``; ``""`` unless it is one plain line (no control character, no
    direction mark: it is shown on a card)."""
    lines = text.strip().splitlines()
    reason = lines[0].strip()[:MAX_JUDGE_REASON_LENGTH].rstrip() if lines else ""
    return reason if is_plain_line(reason) else ""


def read_judgement(text: str, *, they_asked: bool = True) -> Judgement:
    """The judge's answer, read strictly: ``<verdict>`` or ``<verdict>: <reason>``, the verdict
    one of ``VERDICTS`` (case folded, without the spaces around it), the reason its first line
    (``_reason_of``). When the answer does not begin with a verdict, its last line is read the
    same way: a model that is asked not to think first sometimes reasons aloud before its line
    (seen on a real one, 2026-11-15). Anything else is ``fallback("unreadable")``: a ``do`` when
    the person asked (``they_asked``), a ``yes`` otherwise."""
    lines = [line for line in text.strip().splitlines() if line.strip()]
    for line in [text.strip(), lines[-1]] if len(lines) > 1 else [text.strip()]:
        said, _, reason = line.partition(":")
        word = said.strip().casefold()
        for verdict in VERDICTS:
            if word == verdict:
                return Judgement(verdict, _reason_of(reason))
    return fallback("unreadable", they_asked=they_asked)


class LearnsFromFailures(Protocol):
    """What keeps the lessons of a turn's calls (``llm.picker.ModelPicker.note_failure``)."""

    async def note_failure(self, task: TaskKind, reason: str) -> None:
        """The last call of ``task`` did it badly, for ``reason``. It never raises."""
        ...


async def judge(
    model: ChatModel | None,
    case: JudgeCase,
    *,
    learns: LearnsFromFailures | None = None,
    they_asked: bool = True,
) -> Judgement:
    """The verdict of the judge on ``case``.

    The call holds one user message, the case as JSON, under the judge's prompt, with no tool,
    ``max_output_tokens=JUDGE_OUTPUT_TOKENS`` and ``think=False`` (one line, asked without
    thinking first), and it has ``JUDGE_SECONDS`` to answer. Without a
    model, the verdict is the fallback's (``no_model``); so it is when the call fails
    (``error``), takes too long (``timeout``), calls a tool or says no verdict (``unreadable``):
    ``do`` when the person asked (``they_asked``: the turn answers a message of theirs and was
    given nothing from outside), ``yes`` otherwise (``fallback``). An unreadable answer
    is told to ``learns``, a lesson about the judge's model (``UNREADABLE_VERDICT``)."""
    judgement = await _verdict_of(model, case, they_asked)
    if judgement.fallback == "unreadable" and learns is not None:
        await learns.note_failure("judge", UNREADABLE_VERDICT)
    return judgement


async def _verdict_of(model: ChatModel | None, case: JudgeCase, they_asked: bool) -> Judgement:
    """The judge's call and its verdict (see ``judge``)."""
    if model is None:
        return fallback("no_model", they_asked=they_asked)
    pieces: list[str] = []
    with anyio.move_on_after(JUDGE_SECONDS):
        try:
            stream = model.stream(
                system=judge_prompt(),
                messages=[ChatMessage("user", json.dumps(case.as_json(), ensure_ascii=False))],
                max_output_tokens=JUDGE_OUTPUT_TOKENS,
                # One line: nothing to think over that the line does not say.
                think=False,
            )
            async with aclosing(stream) as items:
                async for item in items:
                    if isinstance(item, ToolCall):
                        return fallback("unreadable", they_asked=they_asked)
                    if isinstance(item, ChatChunk):
                        pieces.append(item.text)
        except ModelCallError:
            return fallback("error", they_asked=they_asked)
        return read_judgement("".join(pieces), they_asked=they_asked)
    return fallback("timeout", they_asked=they_asked)


def what_to_ask(
    judgement: Judgement,
    *,
    rule: Ask | None,
    raised: tuple[Ask, str] | None,
    never_ask: NeverAsk | None = None,
) -> Asked | None:
    """What the person is asked before the action (spec 4.4): the most demanding of the judge's
    verdict, the owner's ``rule`` and what Domo ``raised`` (the ask and his reason); ``None``
    when nothing is asked. Who asked it is the first of ``rule``, ``judge`` and ``domo`` that
    asks that much, with the judge's reason or Domo's (``None`` for a rule, and for an empty
    reason).

    With the owner's "not at all" (``never_ask.at_all``, spec 6.3), the judge's verdict and
    Domo's raise count only when they ask for a proof: a yes or an explicit yes of theirs is
    dropped. A rule counts as it is. A ``do_not`` is not asked: ``ValueError``."""
    if judgement.verdict == "do_not":
        raise ValueError("a do_not is refused, not asked")
    judged = judgement.as_ask()
    domo = None if raised is None else raised[0]
    if never_ask is not None and never_ask.at_all:
        judged = judged if judged == "proof" else None
        domo = domo if domo == "proof" else None
    ask = most_demanding(rule, judged, domo)
    if ask is None:
        return None
    if ask == rule:
        return Asked(ask, "rule", None)
    if ask == judged:
        return Asked(ask, "judge", judgement.reason or None, judgement.warns)
    return Asked(ask, "domo", (raised[1] if raised is not None else "") or None)


# What words never give: a change in words is a plain yes (F2).
_WORDS_NEVER_GIVE: Final[frozenset[Ask]] = frozenset({"explicit_yes", "proof"})


def ask_after_a_change(
    judgement: Judgement,
    *,
    old_ask: Asked | None,
    steps: Sequence[PlannedStep],
    new: Sequence[bool],
    raised: tuple[Ask, str] | None,
    never_ask: NeverAsk | None,
    yes_already_given: bool,
    yes_gave: Ask | None = None,
    chain_rule: Ask | None = None,
) -> Asked | None:
    """What a request changed in words, or a chain taken up again, asks now (spec 4.7, F2), once
    the judge judged it (``judgement``, never a ``do_not``: ``ValueError``): the most demanding of
    the judge's verdict, what Domo ``raised``, what the request asked before (``old_ask``) and
    the owner's rules on its steps (``PlannedStep.rule``); ``None`` when nothing is asked.

    ``yes_already_given`` says whether a yes was given to it:

    * not for a change (``confirmation_amend``): the request still waits, nobody said yes yet. The
      rules count over every step still to run, and so does the owner's rule on chains
      (``chain_rule``); what the request asked stays the least it asks when it was an explicit
      yes or a proof (words never give those);
    * yes for a chain taken up again (``chain_continue``): that yes covers what it was given to, so
      the rules count in full for the steps changed or added (``new``, one for each of
      ``steps``), and for a step kept only when they ask more than that yes gave (``yes_gave``,
      ``service.what_the_yes_gave``: ``None``, nothing, when the judge let the chain go): a rule
      the owner added since asks what the yes never gave. What was asked before was given.

    With the owner's "not at all" (``never_ask.at_all``), the judge's verdict and Domo's raise
    count only when they ask for a proof, as in ``what_to_ask``. Who asked it is, on a tie, the
    first of the judge, Domo, the request before and the owner's rule: what the judge or Domo
    asks of a change is what makes it asked again (``given_by_the_change``)."""
    if judgement.verdict == "do_not":
        raise ValueError("a do_not is refused, not asked")
    if len(new) != len(steps):
        raise ValueError("a change says, for each of its steps, whether it is new")

    def counted(ask: Ask | None, is_new: bool) -> Ask | None:
        """``ask`` as it counts for a step: in full when nobody said yes yet or the step is
        new; for a step kept under a yes given, only when it asks more than that yes gave."""
        if is_new or not yes_already_given or ask is None:
            return ask
        return ask if yes_gave is None or most_demanding(ask, yes_gave) != yes_gave else None

    pairs = list(zip(steps, new, strict=True))
    rules: list[Ask | None] = [counted(step.rule, is_new) for step, is_new in pairs]
    if not yes_already_given:
        rules.append(chain_rule)
    rule = most_demanding(*rules)
    judged = judgement.as_ask()
    domo = None if raised is None else raised[0]
    if never_ask is not None and never_ask.at_all:
        judged = judged if judged == "proof" else None
        domo = domo if domo == "proof" else None
    before = (
        old_ask
        if old_ask is not None and old_ask.ask in _WORDS_NEVER_GIVE and not yes_already_given
        else None
    )
    return most_demanding_asked(
        None
        if judged is None
        else Asked(judged, "judge", judgement.reason or None, judgement.warns),
        None if domo is None or raised is None else Asked(domo, "domo", raised[1] or None),
        before,
        None if rule is None else Asked(rule, "rule", None),
    )


def given_by_the_change(asked: Asked | None) -> bool:
    """Whether the words of a change (a yes in words) give what ``asked`` asks
    (``ask_after_a_change``): nothing, or the plain yes of a rule of the owner. A yes the judge
    or Domo asks again of the change, an explicit yes and a proof are not given by it: the
    change is then a new request."""
    return asked is None or (asked.ask == "yes" and asked.by == "rule")


async def audit_judgement(
    session: AsyncSession,
    *,
    actor: Actor,
    tool_name: str,
    action: DescribedAction,
    judgement: Judgement,
) -> None:
    """Write the audit entry ``judge.verdict`` (the tool, the action type and target, the verdict,
    its reason and whether it came from the model or the fallback) in the caller's transaction:
    like ``append_audit``, nothing is committed, and the caller writes it last."""
    await append_audit(
        session,
        actor=f"person:{actor.person_id}",
        action="judge.verdict",
        details={
            "tool": tool_name,
            "action_type": action.action_type,
            "target": action.target,
            "verdict": judgement.verdict,
            "reason": judgement.reason,
            "source": judgement.source,
        },
    )
