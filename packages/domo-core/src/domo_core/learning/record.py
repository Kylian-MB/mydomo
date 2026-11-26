"""The one way to note what went wrong: a fact written beside a decision, never in its way."""

import logging
import re
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Final, Literal

from sqlalchemy import update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.conversation.cards import storable
from domo_core.learning.models import MAX_REASON_LENGTH, Kind, WentWrong
from domo_core.memory.service import refuses_secret_shapes

logger = logging.getLogger(__name__)

# The reason of a failure that is a code (``"timeout"``), never a text: one a library wrote may
# hold what the arguments held.
_REASON_CODE: Final = re.compile(r"[a-z][a-z0-9_]{0,39}")


def reason_code(result: Mapping[str, object]) -> str | None:
    """The ``reason`` of what a tool gave back, when it is a code; ``None`` otherwise."""
    reason = result.get("reason")
    return reason if isinstance(reason, str) and _REASON_CODE.fullmatch(reason) else None


def kept_reason(reason: str | None) -> str | None:
    """``reason`` made storable and cut at ``MAX_REASON_LENGTH``; ``None`` when it is empty or has
    the shape of a secret. What a person said with a no is kept this way too
    (``Confirmation.refusal_words``)."""
    if reason is None:
        return None
    whole = storable(reason).strip()
    # Checked whole, before the cut: a secret straddling the cut must not survive as a prefix.
    if not whole or refuses_secret_shapes(whole):
        return None
    return whole[:MAX_REASON_LENGTH].rstrip()


async def note_went_wrong(
    session: AsyncSession,
    *,
    kind: Kind,
    action_type: str,
    now: datetime,
    subject_key: str | None = None,
    action_id: uuid.UUID | None = None,
    confirmation_id: uuid.UUID | None = None,
    by: uuid.UUID | Literal["domo", "code"] = "code",
    of_domo: bool = False,
    about: uuid.UUID | None = None,
    reason: str | None = None,
) -> WentWrong | None:
    """Note a fact in a savepoint of ``session`` (flushed, not committed).

    ``by`` is a person's id, ``"domo"`` (the judge or the assistant himself) or ``"code"`` (an
    expiry, a failure). A database error is logged by its kind and gives ``None``: a fact never
    stops a decision."""
    row = WentWrong(
        at=now,
        kind=kind,
        action_type=action_type,
        subject_key=subject_key,
        action_id=action_id,
        confirmation_id=confirmation_id,
        by_person_id=by if isinstance(by, uuid.UUID) else None,
        by_domo=by == "domo",
        of_domo=of_domo,
        about_person_id=about,
        reason=kept_reason(reason),
    )
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except SQLAlchemyError as error:
        logger.warning("a fact of what went wrong was not kept (%s)", type(error).__name__)
        return None
    return row


async def mark_asked_why(session: AsyncSession, went_wrong_id: uuid.UUID, now: datetime) -> bool:
    """Mark that someone was asked why; ``False`` when it was asked already (never twice)."""
    result = await session.execute(
        update(WentWrong)
        .where(WentWrong.id == went_wrong_id, WentWrong.asked_why_at.is_(None))
        .values(asked_why_at=now)
        .returning(WentWrong.id)
    )
    return result.first() is not None
