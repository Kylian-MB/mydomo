"""What a person's action undoes of the assistant's own (spec 9.1, 9.2): the action it takes back,
found from the subject key alone (``policy.subjects``), the facts it leaves and the line that tells
him so.

Read in the transaction that records the undoing action, before it is stored: an action that ran
at once (``conversation.execution.store_action``), or a request that ran after a yes
(``confirmations.service.settle``). No tool knows of it.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from sqlalchemy import Exists, String, and_, cast, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.audit.models import AuditEntry
from domo_core.chat.models import Conversation, Message, MessageAction
from domo_core.confirmations.models import Confirmation
from domo_core.learning.models import Kind
from domo_core.learning.record import note_went_wrong
from domo_core.policy.settings import load_policy_settings
from domo_core.policy.subjects import UNDOES

logger = logging.getLogger(__name__)

# What the assistant reads in the result of an action that undoes his own: the day and the type
# only, never anything of the conversation it was done in (it may be someone else's).
UNDOES_LINE: Final = "this undoes what you had done (or decided) yourself on {day}: {action_type}"
# The same, for a person's plain request his judge let through, undone by someone else (R53).
JUDGED_LINE: Final = "this undoes what your judge had let through on {day}: {action_type}"


@dataclass(frozen=True)
class Undone:
    """An action that a person's action undoes and that says something of the assistant: the row
    (``action``), the person whose conversation it was done in (``about``), whether it was his
    idea (``his_idea``: his own act, or a reason of his) and whether his judge let it through with
    nothing asked (``let_through``: a verdict ``do`` of the judge's model, tied to it)."""

    action: MessageAction
    about: UUID
    his_idea: bool
    let_through: bool

    @property
    def kinds(self) -> tuple[Kind, ...]:
        """The facts it leaves: ``undone`` for an idea of his, with ``wrong_verdict`` when his
        judge let it through too; ``wrong_verdict`` alone for a person's plain request (R53)."""
        if not self.his_idea:
            return ("wrong_verdict",)
        return ("undone", "wrong_verdict") if self.let_through else ("undone",)


def _judge_let_it_through() -> Exists:
    """Whether the judge's model said ``do`` of the action of the row, as the audit ties it
    (``judge.verdict`` with its ``action_id``); a fallback is no verdict of his."""
    return exists().where(
        AuditEntry.action == "judge.verdict",
        AuditEntry.details["action_id"].astext == cast(MessageAction.id, String),
        AuditEntry.details["verdict"].astext == "do",
        AuditEntry.details["source"].astext == "model",
    )


async def undoes_his(
    session: AsyncSession,
    *,
    action_type: str,
    keys: tuple[str, ...],
    by: UUID,
    ran_from: UUID | None = None,
) -> Undone | None:
    """What an action of ``action_type`` done by ``by`` undoes that says something of the
    assistant, on the thing ``keys`` stand for (``policy.subjects.keys_of``: its canonical key and
    aliases); ``None`` when nothing.

    The action it undoes is the latest one done on those keys among the types it undoes and its
    own (R54: whoever did it; a request that ran after a yes counts by its row, ``ran_from`` being
    the request of the undoing action itself, never counted): when that latest is an undoing
    already, or a type it does not undo, nothing. It counts when it was his idea (``by_domo`` or
    ``idea_reason``), or when it was a person's plain request his judge let through and someone
    else than that person undoes it (R53: changing one's mind is no mistake of his). Reads
    only."""
    undone_types = UNDOES.get(action_type)
    if not undone_types or not keys:
        return None
    let_through = _judge_let_it_through()
    ran = or_(
        MessageAction.status == "done",
        and_(MessageAction.status == "waiting", Confirmation.status == "executed"),
    )
    conditions = [
        ran,
        MessageAction.subject_key.in_(keys),
        MessageAction.action_type.in_(undone_types | {action_type}),
    ]
    if ran_from is not None:
        conditions.append(
            or_(MessageAction.confirmation_id.is_(None), MessageAction.confirmation_id != ran_from)
        )
    found = (
        await session.execute(
            select(MessageAction, Conversation.person_id, let_through)
            .join(Message, Message.id == MessageAction.message_id)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .outerjoin(Confirmation, Confirmation.id == MessageAction.confirmation_id)
            .where(*conditions)
            .order_by(MessageAction.created_at.desc(), MessageAction.id.desc())
            .limit(1)
        )
    ).first()
    if found is None:
        return None
    row, about, judged = found
    if row.action_type not in undone_types:
        return None
    his_idea = row.by_domo or row.idea_reason is not None
    if his_idea or (judged and about != by):
        return Undone(row, about, his_idea, bool(judged))
    return None


async def undoes_line(session: AsyncSession, undone: Undone) -> str:
    """What the assistant is told: the day of the action undone, on the household's clock (UTC
    until the owner sets it), and its type."""
    zone = (await load_policy_settings(session)).time_zone or UTC
    day = undone.action.created_at.astimezone(zone).date().isoformat()
    line = UNDOES_LINE if undone.his_idea else JUDGED_LINE
    return line.format(day=day, action_type=undone.action.action_type)


async def note_undoing(
    session: AsyncSession,
    *,
    action_type: str,
    keys: tuple[str, ...],
    by: UUID,
    now: datetime,
    reason: str | None,
    ran_from: UUID | None = None,
) -> str | None:
    """Find what an action of ``action_type`` that ``by`` did undoes (``undoes_his``), note its
    facts (``Undone.kinds``: under the thing the action undone was about, by ``by``, about the
    person of its conversation, with ``reason``, the person's own words of the turn when there are
    some: data with its author, cut and a secret shape dropped by ``note_went_wrong``) and give
    the line the assistant is told (``None`` when it undoes nothing).

    In a savepoint of its own, guarded: a failure is logged by its kind and gives ``None``,
    and never stops the record of the action itself."""
    try:
        async with session.begin_nested():
            undone = await undoes_his(
                session, action_type=action_type, keys=keys, by=by, ran_from=ran_from
            )
            if undone is None:
                return None
            for kind in undone.kinds:
                await note_went_wrong(
                    session,
                    kind=kind,
                    action_type=action_type,
                    now=now,
                    subject_key=undone.action.subject_key,
                    action_id=undone.action.id,
                    by=by,
                    of_domo=undone.his_idea,
                    about=undone.about,
                    reason=reason,
                )
            return await undoes_line(session, undone)
    except Exception as error:
        logger.warning("what an action undoes was not read (%s)", type(error).__name__)
        return None


async def let_through_for(
    session: AsyncSession, *, person_id: UUID, action_type: str, subject_key: str
) -> MessageAction | None:
    """The latest action ``done`` of this type and subject key, in a conversation of the person,
    that his judge let through with nothing asked (the same verdict as ``undoes_his`` reads): what
    a complaint of the person is then a wrong verdict about. ``None`` when there is none. Reads
    only."""
    return (
        await session.scalars(
            select(MessageAction)
            .join(Message, Message.id == MessageAction.message_id)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                MessageAction.status == "done",
                MessageAction.action_type == action_type,
                MessageAction.subject_key == subject_key,
                Conversation.person_id == person_id,
                _judge_let_it_through(),
            )
            .order_by(MessageAction.created_at.desc(), MessageAction.id.desc())
            .limit(1)
        )
    ).first()
