"""Reading back what went wrong, the closest facts first."""

import uuid

from sqlalchemy import ColumnElement, case, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.learning.models import WentWrong


async def went_wrong_for(
    session: AsyncSession,
    *,
    action_type: str,
    subject_key: str | None,
    about: uuid.UUID | None,
    of_domo: bool | None = None,
    limit: int = 5,
) -> list[WentWrong]:
    """The latest facts first: those of the same subject key, then of the same type and the same
    ``about``, then of the same type with ``of_domo`` (when given)."""
    same_type = WentWrong.action_type == action_type
    matches: list[ColumnElement[bool]] = []
    if subject_key is not None:
        matches.append(WentWrong.subject_key == subject_key)
    if about is not None:
        matches.append(same_type & (WentWrong.about_person_id == about))
    if of_domo is not None:
        matches.append(same_type & (WentWrong.of_domo == of_domo))
    if not matches:
        return []
    closeness = case(*((match, position) for position, match in enumerate(matches)), else_=99)
    found = await session.execute(
        select(WentWrong)
        .where(or_(*matches))
        .order_by(closeness, WentWrong.at.desc(), WentWrong.id)
        .limit(limit)
    )
    return list(found.scalars())
