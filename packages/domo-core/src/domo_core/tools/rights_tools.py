"""The tools with which the owner changes the rights in words (spec 5.6).

* ``rights_propose`` (free, the owner only): the owner's words for one request go to the rights
  writer (``policy.rights_writer``), a call of its own on the key of the request, to a model of
  medium level or stronger (``ToolContext.models``, ``model_for("rights_writer")``; without one
  nothing is written, and Domo is told why) that sees only those words and the household's
  context, never the conversation. The code checks the draft on its examples; a checked draft is
  stored as it is (``RightsDraft``), each example with the moment it was worked out at (Ruling
  R25), and Domo is told the summaries and the examples, each as a sentence the code makes
  from its facts (``confirmations.describe.describe_example``) with that moment in words when a
  limit reads it or the writer stated it (``example_moment``) and the outcome the code worked
  out. Nothing changes yet. The writer may ask a question instead (Domo asks the
  owner); a writer that cannot make a draft the code accepts stores nothing. It puts a time limit
  on each of its own steps (``BoundsItsOwnSteps``, Ruling R16): each call to the writer has its
  own, the owner's setting ``rights_writer.seconds`` (``PolicySettings.writer_seconds``, read
  with the context of the writer: one that takes longer refuses the draft), and so has each read
  and the storing (``_STEP_SECONDS``), so that the writer's second call is never cut short by a
  limit around the whole. A writer that ran out of time is noted: the audit entry
  ``rights.writer_timeout`` (the owner as the actor, the seconds the writer had as the only
  detail), written in a session of its own and committed, and Domo is told the seconds with the
  failure, even if the entry could not be written (that is logged, by the kind of error alone).
  The owner's context counts those entries over the last ``WRITER_TIMEOUT_WINDOW``
  (``writer_timeouts``), so that Domo may suggest giving the writer more time; only the owner
  changes the setting, in words (``policy_update``).
* ``rights_apply`` (the owner only; judged like any action: nothing is asked by default, so a
  clear request is drafted and applied in the same answer): applies a draft of the
  owner's, once. A request that waits shows the draft's rights and examples
  (``confirmations.describe.draft_lines_of``). The draft must be the owner's, not applied yet,
  and checked against the rights in force now (its examples were worked out on them): otherwise
  it is proposed again (a draft found out of date once the yes runs it fails with a card that
  says so). In one transaction, with the table of the rights locked (two drafts
  applied at once are applied one after the other): for each change, the right of the same
  subject and action type that is not revoked, paused or not, is revoked, and the new one is
  written (unless the change takes it away: giving a paused right back is writing it again),
  with what it says to ask (``ask``, ``ask_when``); the draft is marked
  applied; and the audit entry ``rights.changed`` says, for each change, the subject (a person, a
  role, ``domo`` for Domo's own right, or ``everyone`` for a rule for everyone: never the one
  for the other), the action type and the summary before and after.

Who may use them is the foundation's business (``catalog.OWNER_ONLY_ACTIONS``): the barrier
refuses them to anyone but the owner, and the model of anyone else is not shown them.

A change of rights that waits for the owner's yes is changed in words ("yes, but 3 days instead
of 2", spec 5.3): ``redraft_rights`` has the writer draft it again from the owner's words and
their change, for ``confirmations.service.amend_confirmation``, which has the new draft judged
like any other change: applied at once, or a new request whose examples the owner reads.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

import anyio
from pydantic import JsonValue
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.accounts.sessions import active_at
from domo_core.audit.log import append_audit
from domo_core.audit.models import AuditEntry
from domo_core.confirmations.describe import example_moment, example_situation
from domo_core.household.service import former_persons, left_on
from domo_core.llm.types import JsonObject, ToolDefinition
from domo_core.naming import name_shown
from domo_core.policy.actions import DescribedAction
from domo_core.policy.facts import decision_facts, made_up_facts
from domo_core.policy.models import PolicyRight, RightsDraft
from domo_core.policy.rights import load_rights, paused_rights
from domo_core.policy.rights_drafts import (
    RightChange,
    StoredDraft,
    draft_as_json,
    rights_basis,
    stored_draft,
)
from domo_core.policy.rights_writer import (
    MADE_UP_PERSON,
    MAX_OWNER_WORDS,
    DraftRefused,
    WriterAsks,
    WriterContext,
    WriterTooSlow,
    write_draft,
)
from domo_core.policy.roles import DOMO_LABEL, Actor, Role, as_role
from domo_core.policy.settings import PolicySettings, load_policy_settings
from domo_core.texts.view import REFERENCE
from domo_core.tools.context import (
    InvalidArguments,
    RefusesWhateverTheRights,
    ToolContext,
    ToolOutcome,
)

logger = logging.getLogger(__name__)

# How many of the code's reasons Domo is told when no draft could be written.
_REASONS_TOLD: Final = 5
# How long each read of the database, and the storing of a draft, may take in ``rights_propose``
# (the calls to the writer have their own limit: the owner's setting).
_STEP_SECONDS: Final = 15.0

# The audit entry of a writer that ran out of time, and how far back the owner's context counts
# them.
WRITER_TIMEOUT_ACTION: Final = "rights.writer_timeout"
WRITER_TIMEOUT_WINDOW: Final = timedelta(days=30)

_NO_WRITER: Final = (
    "the rights writer cannot be reached from here: ask the owner to say it again in the chat"
)
_NO_WRITER_MODEL: Final = (
    "the key of this request has no model strong enough to write rights (medium or stronger): "
    "nothing was written; tell the owner plainly"
)
_NOT_WRITTEN: Final = (
    "the rights could not be written: tell the owner plainly, and ask them to say it another way"
)
_OWNER_ONLY: Final = "only the owner has rights written: this person is not the owner"
_NO_DRAFT: Final = "no draft of the owner waits with this id"
_APPLIED: Final = "this draft was applied already: nothing more to do"
_UNREADABLE: Final = "this draft can no longer be read: propose the rights again"
_OUT_OF_DATE: Final = (
    "the rights changed since this draft was written, and its examples may no longer hold: "
    "propose the rights again"
)


class _OutOfDate(InvalidArguments):
    """The rights in force are no longer those the draft was checked against."""

    def __init__(self) -> None:
        super().__init__("invalid", _OUT_OF_DATE)


def _owner_words(value: object, what: str) -> str:
    """``value``, words of the owner's for the writer, without the spaces around them: 1 to
    ``MAX_OWNER_WORDS`` characters that the database can hold. ``what`` names them in the detail
    ("the request"), which never repeats them."""
    if not isinstance(value, str):
        raise InvalidArguments("invalid", f"{what} is a text")
    words = value.strip()
    if not words:
        raise InvalidArguments("invalid", f"{what} is empty")
    if len(words) > MAX_OWNER_WORDS:
        raise InvalidArguments("too_long", f"{what} has more than {MAX_OWNER_WORDS} characters")
    if "\x00" in words or any(0xD800 <= ord(character) <= 0xDFFF for character in words):
        raise InvalidArguments("invalid", f"{what} holds characters that cannot be kept")
    return words


def _request_of(arguments: JsonObject) -> str:
    """The owner's words of a ``rights_propose`` call (``_owner_words``)."""
    if set(arguments) != {"request"}:
        raise InvalidArguments("invalid", "the argument is request")
    return _owner_words(arguments["request"], "the request")


async def _writer_context(ctx: ToolContext, now: datetime) -> tuple[WriterContext, PolicySettings]:
    """What the writer is told, as the database holds it at ``now``: the people of the household
    and those who left it (with the day they left), the rights in force and the paused ones, and
    whether the time zone is set; and the owner's settings, read in the same step (the writer's
    time is one of them). The facts of an example
    are read later, each in a session of its own (those of a made-up person of a role for
    ``MADE_UP_PERSON``). Each read has ``_STEP_SECONDS`` (``TimeoutError``)."""
    with anyio.fail_after(_STEP_SECONDS):
        async with ctx.session_factory() as session:
            people = await session.execute(
                select(Person.id, Person.display_name, Person.role)
                .where(active_at(now))
                .order_by(Person.created_at, Person.id)
            )
            persons: tuple[tuple[uuid.UUID, str, Role], ...] = tuple(
                (pid, name, as_role(role)) for pid, name, role in people.all()
            )
            former: tuple[tuple[uuid.UUID, str, Role, str], ...] = tuple(
                (left.id, left.display_name, as_role(left.role), left_on(left).isoformat())
                for left in await former_persons(session, now)
            )
            rights = await load_rights(session)
            paused = await paused_rights(session)
            settings = await load_policy_settings(session)
    time_zone = settings.time_zone

    async def facts_for(actor: Actor, action: DescribedAction) -> dict[str, object]:
        with anyio.fail_after(_STEP_SECONDS):
            async with ctx.session_factory() as session:
                # Domo's id is nobody's, as a made-up person's: the subject tells them apart.
                if actor.subject != "domo" and actor.person_id == MADE_UP_PERSON:
                    return await made_up_facts(session, actor, action, now, time_zone)
                return await decision_facts(session, actor, action, now, time_zone)

    context = WriterContext(
        persons=persons,
        rights=rights,
        paused=paused,
        policy=ctx.policy,
        time_zone_set=time_zone is not None,
        facts_for=facts_for,
        former=former,
        # What the tools always refuse, whatever the rights (Ruling R19): their own words.
        fixed_refusals={
            tool.action_type: tool.fixed_refusal
            for tool in ctx.registry.values()
            if isinstance(tool, RefusesWhateverTheRights)
        },
    )
    return context, settings


async def _note_the_timeout(ctx: ToolContext, seconds: int) -> None:
    """Write the audit entry of a writer that ran out of time: the owner is the actor, and the
    only detail is the time the writer had. It is committed in a session of its own, in a step
    bounded by ``_STEP_SECONDS`` (``TimeoutError``); the caller decides what a failure costs."""
    with anyio.fail_after(_STEP_SECONDS):
        async with ctx.session_factory() as session:
            await append_audit(
                session,
                actor=f"person:{ctx.person.id}",
                action=WRITER_TIMEOUT_ACTION,
                details={"seconds": seconds},
            )
            await session.commit()


async def _note_the_timeout_if_possible(ctx: ToolContext, seconds: int) -> None:
    """``_note_the_timeout``; a note that cannot be written is lost, and Domo is still told the
    time the writer had. Only the kind of error is logged, as everywhere in the core."""
    try:
        await _note_the_timeout(ctx, seconds)
    except Exception as error:
        logger.error(
            "The audit entry of a rights writer out of time was not written (%s)",
            type(error).__name__,
        )


async def writer_timeouts(session: AsyncSession, now: datetime) -> int:
    """How many times the writer ran out of time over the last ``WRITER_TIMEOUT_WINDOW`` before
    ``now``: the audit entries ``rights.writer_timeout`` that are newer than that."""
    result = await session.execute(
        select(func.count())
        .select_from(AuditEntry)
        .where(AuditEntry.action == WRITER_TIMEOUT_ACTION)
        .where(AuditEntry.ts > now - WRITER_TIMEOUT_WINDOW)
    )
    return result.scalar_one()


async def _store_draft(ctx: ToolContext, words: str, content: JsonObject) -> uuid.UUID:
    """Store a checked draft of the owner (``ctx.person``), as it is, with the ``words`` it was
    written from: committed, in a step bounded by ``_STEP_SECONDS`` (``TimeoutError``). Its id."""
    with anyio.fail_after(_STEP_SECONDS):
        async with ctx.session_factory() as session:
            row = RightsDraft(created_by=ctx.person.id, owner_words=words, content=content)
            session.add(row)
            await session.flush()
            draft_id = row.id
            await session.commit()
    return draft_id


class RightsPropose:
    """Have the rights writer draft the rights the owner asks for, checked on examples.

    It bounds each of its own steps (``BoundsItsOwnSteps``): no limit is put around the whole."""

    bounds_its_own_steps: Final = True

    definition = ToolDefinition(
        name="rights_propose",
        description=(
            "Draft the rights they ask for: who may ask you for what (one person, a role, "
            "or everyone), or what you may do on your own for the house, who must agree, with "
            "which limits, and what you must always ask them first, or never ask (for one of "
            "their own actions too: under conditions they state, or not at all); or take a right "
            "away, or give back a paused one. Give their words for this request only, with the "
            "names they used, never anything secret. A separate writer drafts the rights and the "
            "code checks them on examples; the draft changes nothing by itself. When their "
            "request was clear and the draft says what they asked, call rights_apply with the "
            "draft in this same answer, then tell them what changed, with one example. Only when "
            "you are not sure you understood, or the draft does more than they said, tell them "
            "its rights and examples first and ask. If the result is a question, ask it to them "
            "and call again with their answer added."
        ),
        parameters={
            "type": "object",
            "properties": {
                "request": {
                    "type": "string",
                    "description": "The person's words for this request, as they said them.",
                    "minLength": 1,
                    "maxLength": MAX_OWNER_WORDS,
                }
            },
            "required": ["request"],
            "additionalProperties": False,
        },
    )
    action_type = "rights.propose"

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        _request_of(arguments)
        # The words are not in the action: they are the owner's, and stored with the draft.
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=None,
            effect="permissions",
            destination="household",
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        words = _request_of(arguments)
        failed: JsonObject = {"key": "action.card.failed.rights.propose", "params": {}}
        if ctx.models is None:
            return ToolOutcome("failed", {"detail": _NO_WRITER}, failed)
        with anyio.fail_after(_STEP_SECONDS):
            writer = await ctx.models.model_for("rights_writer")
        if writer is None:
            return ToolOutcome("failed", {"detail": _NO_WRITER_MODEL}, failed)
        now = ctx.clock()
        context, settings = await _writer_context(ctx, now)
        seconds = settings.writer_seconds
        try:
            draft = await write_draft(writer, words, context, seconds=seconds)
        except WriterAsks as asks:
            card: JsonObject = {"key": "action.card.done.rights.propose.question", "params": {}}
            return ToolOutcome("done", {"question": asks.question}, card)
        except WriterTooSlow as too_slow:
            await _note_the_timeout_if_possible(ctx, seconds)
            why = too_slow.errors[:_REASONS_TOLD]
            details: JsonObject = {"why": why, "detail": _NOT_WRITTEN, "seconds": seconds}
            return ToolOutcome("failed", details, failed)
        except DraftRefused as refused:
            why = refused.errors[:_REASONS_TOLD]
            return ToolOutcome("failed", {"why": why, "detail": _NOT_WRITTEN}, failed)
        draft_id = await _store_draft(ctx, words, draft_as_json(draft, context))
        names = {str(person_id): name for person_id, name, _ in context.persons}
        examples: list[JsonObject] = []
        for example in draft.examples:
            told: JsonObject = {
                "situation": example_situation(
                    example,
                    names,
                    REFERENCE,
                    ctx.policy.kinds,
                    name_shown(ctx.person.calls_domo, settings.house_domo_name),
                )
            }
            moment = example_moment(example, draft, REFERENCE)
            if moment is not None:
                told["moment"] = moment
            told["outcome"] = example.expected
            examples.append(told)
        return ToolOutcome(
            "done",
            {
                "draft": str(draft_id),
                "rights": [change.summary for change in draft.changes],
                "examples": examples,
            },
            {"key": "action.card.done.rights.propose", "params": {}},
        )


def _draft_id(arguments: JsonObject) -> uuid.UUID:
    if set(arguments) != {"draft"}:
        raise InvalidArguments("invalid", "the argument is draft")
    value: object = arguments["draft"]
    if not isinstance(value, str):
        raise InvalidArguments("invalid", "draft is the id rights_propose gave, as a text")
    try:
        return uuid.UUID(value)
    except ValueError:
        raise InvalidArguments("invalid", "draft is the id rights_propose gave") from None


async def _owned_draft(
    session: AsyncSession, draft_id: uuid.UUID, person_id: uuid.UUID, *, lock: bool = False
) -> RightsDraft:
    """The draft (locked with ``lock``), if it is ``person_id``'s, not applied yet and readable;
    whatever the rights in force now. ``InvalidArguments`` otherwise (``not_found`` for a draft
    that is not theirs: nobody learns it exists)."""
    query = (
        select(RightsDraft)
        .where(RightsDraft.id == draft_id)
        .execution_options(populate_existing=True)
    )
    row = (await session.execute(query.with_for_update() if lock else query)).scalar_one_or_none()
    if row is None or row.created_by != person_id:
        raise InvalidArguments("not_found", _NO_DRAFT)
    if row.applied_at is not None:
        raise InvalidArguments("invalid", _APPLIED)
    try:
        stored_draft(row.content)
    except ValueError:
        raise InvalidArguments("invalid", _UNREADABLE) from None
    return row


async def _ready(
    session: AsyncSession, draft_id: uuid.UUID, person_id: uuid.UUID, *, lock: bool = False
) -> tuple[RightsDraft, StoredDraft]:
    """The draft, if it may be applied for ``person_id`` now (locked with ``lock``): theirs, not
    applied yet, readable (``_owned_draft``), and checked against the rights in force now
    (``_OutOfDate`` otherwise)."""
    row = await _owned_draft(session, draft_id, person_id, lock=lock)
    stored = stored_draft(row.content)
    if list(stored.based_on) != rights_basis(
        await load_rights(session), await paused_rights(session)
    ):
        raise _OutOfDate()
    return row, stored


@dataclass(frozen=True)
class Redraft:
    """What came of drafting rights again with a change (``redraft_rights``): the new draft,
    stored, or the writer's question for the owner (nothing was stored)."""

    draft_id: uuid.UUID | None
    question: str | None


async def redraft_rights(ctx: ToolContext, draft_id: uuid.UUID, change: str) -> Redraft:
    """Have the writer draft the rights of the owner's draft ``draft_id`` again, with ``change``:
    what the owner wants different, in their words (spec 5.3: "yes, but 3 days instead of 2", on
    the card of a change of rights). ``confirmation_amend`` calls it on a request that applies
    that draft, before it locks the request: the writer talks to the network, up to the owner's
    setting for each of its calls, and no lock or session is held meanwhile.

    Only the owner has rights written (``catalog.OWNER_ONLY_ACTIONS``): for anyone else (the owner
    who handed the role over since, say), nothing is read and the writer is never called. The
    draft must be the person's, not applied yet, and readable (``_owned_draft``; it may be out of
    date: the new one is written on the rights in force now). The writer is given the owner's
    words of that draft, then, after a blank line, the change (the writer's prompt says a later
    change wins), at most ``MAX_OWNER_WORDS`` characters in all (``too_long``), with the context
    of now, as ``rights_propose`` does. A checked draft is stored, with those words: its id. The
    writer's question comes back as it is (``Redraft.question``), never in the detail of an error
    (it is the writer's text), and nothing is stored. A writer that cannot write a draft the code
    accepts, or runs out of time (noted, as for ``rights_propose``), or cannot be reached from
    here (``ctx.models`` is ``None``), or has no model strong enough on the key of the request, is
    ``InvalidArguments("invalid")``, with what Domo is to do; nothing is stored either."""
    if ctx.actor.role != "owner":
        raise InvalidArguments("invalid", _OWNER_ONLY)
    if ctx.models is None:
        raise InvalidArguments("invalid", _NO_WRITER)
    later = _owner_words(change, "the change")
    with anyio.fail_after(_STEP_SECONDS):
        async with ctx.session_factory() as session:
            row = await _owned_draft(session, draft_id, ctx.person.id)
            before = row.owner_words
    words = f"{before}\n\n{later}"
    if len(words) > MAX_OWNER_WORDS:
        raise InvalidArguments(
            "too_long",
            f"the owner's words and the change have more than {MAX_OWNER_WORDS} characters",
        )
    with anyio.fail_after(_STEP_SECONDS):
        writer = await ctx.models.model_for("rights_writer")
    if writer is None:
        raise InvalidArguments("invalid", _NO_WRITER_MODEL)
    context, settings = await _writer_context(ctx, ctx.clock())
    seconds = settings.writer_seconds
    try:
        draft = await write_draft(writer, words, context, seconds=seconds)
    except WriterAsks as asks:
        return Redraft(None, asks.question)
    except WriterTooSlow:
        await _note_the_timeout_if_possible(ctx, seconds)
        raise InvalidArguments("invalid", _NOT_WRITTEN) from None
    except DraftRefused:
        raise InvalidArguments("invalid", _NOT_WRITTEN) from None
    return Redraft(await _store_draft(ctx, words, draft_as_json(draft, context)), None)


async def _revoke(session: AsyncSession, change: RightChange, now: datetime) -> str | None:
    """Revoke the right of the change's subject and action type that is not revoked, paused or
    not (one at most: the table's unique index); its summary, if there was one. Domo's own right
    (``for_domo``) is his alone; no person, no role and not Domo's: the rule for everyone."""
    if change.for_domo:
        subject = PolicyRight.for_domo.is_(True)
    elif change.person_id is not None:
        subject = PolicyRight.person_id == change.person_id
    elif change.role is not None:
        subject = PolicyRight.person_id.is_(None) & (PolicyRight.role == change.role)
    else:
        subject = (
            PolicyRight.person_id.is_(None)
            & PolicyRight.role.is_(None)
            & PolicyRight.for_domo.is_(False)
        )
    revoked = await session.execute(
        update(PolicyRight)
        .where(subject, PolicyRight.action_type == change.action_type)
        .where(PolicyRight.revoked_at.is_(None))
        .values(revoked_at=now)
        .returning(PolicyRight.summary)
        .execution_options(synchronize_session=False)
    )
    return next(iter(revoked.scalars().all()), None)


class RightsApply:
    """Apply a draft of rights."""

    definition = ToolDefinition(
        name="rights_apply",
        description=(
            "Apply a draft that rights_propose wrote (its draft id): right after the draft, in "
            "the same answer, when it says what they asked. Then say what changed. A request "
            "that waits shows the rights and the examples. A draft is applied once, and only "
            "while the rights in force are those it was written with; otherwise propose the "
            "rights again."
        ),
        parameters={
            "type": "object",
            "properties": {
                "draft": {"type": "string", "description": "The draft id rights_propose gave."}
            },
            "required": ["draft"],
            "additionalProperties": False,
        },
    )
    action_type = "rights.apply"

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        draft_id = _draft_id(arguments)
        async with ctx.session_factory() as session:
            await _ready(session, draft_id, actor.person_id)
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=f"draft:{draft_id}",
            effect="permissions",
            destination="household",
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        draft_id = _draft_id(arguments)
        now = ctx.clock()
        async with ctx.session_factory() as session:
            # One change of rights at a time: another waits here until this one is committed,
            # then finds the rights it was checked against changed.
            await session.execute(text("LOCK TABLE policy_rights IN SHARE ROW EXCLUSIVE MODE"))
            try:
                row, stored = await _ready(session, draft_id, ctx.person.id, lock=True)
            except _OutOfDate:
                # Another change of rights went first (two drafts applied at once): the owner
                # reads that this one was not applied, and why.
                failed: JsonObject = {
                    "key": "action.card.failed.rights.apply.out_of_date",
                    "params": {},
                }
                return ToolOutcome("failed", {"detail": _OUT_OF_DATE}, failed)
            changes: list[JsonValue] = []
            for change in stored.draft.changes:
                before = await _revoke(session, change, now)
                if not change.remove:
                    session.add(
                        PolicyRight(
                            person_id=change.person_id,
                            role=change.role,
                            action_type=change.action_type,
                            access=change.access,
                            approver=change.approver,
                            limit_expression=change.limit_expression,
                            judge_question=change.judge_question,
                            ask=change.ask,
                            ask_when=change.ask_when,
                            summary=change.summary,
                            for_domo=change.for_domo,
                            owner_words=row.owner_words,
                            draft_id=row.id,
                            created_by=ctx.person.id,
                        )
                    )
                    await session.flush()
                changes.append(_audited(change, stored, before))
            row.applied_at = now
            await session.flush()
            # Last, just before the commit.
            await append_audit(
                session,
                actor=f"person:{ctx.person.id}",
                action="rights.changed",
                details={"draft_id": str(draft_id), "changes": changes},
            )
            await session.commit()
        return ToolOutcome(
            "done",
            {"changed": len(changes)},
            {"key": "action.card.done.rights.apply", "params": {}},
        )


def _audited(change: RightChange, stored: StoredDraft, before: str | None) -> JsonValue:
    """A change as the audit log keeps it: its subject (with the person's name; ``domo`` for
    Domo's own right, ``everyone`` for a rule for everyone), its action type, and the summary of
    the right before and after (``None``: there was none, or there is none)."""
    entry: dict[str, JsonValue] = {}
    if change.for_domo:
        entry["subject"] = DOMO_LABEL
    elif change.person_id is not None:
        entry["subject"] = f"person:{change.person_id}"
        entry["name"] = stored.names.get(str(change.person_id))
    elif change.role is not None:
        entry["subject"] = f"role:{change.role}"
    else:
        entry["subject"] = "everyone"
    entry["action_type"] = change.action_type
    entry["before"] = before
    entry["after"] = None if change.remove else change.summary
    return entry
