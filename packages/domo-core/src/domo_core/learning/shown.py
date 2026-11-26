"""What a turn is shown of what went wrong lately (spec 9.2): the latest facts for the person of
the conversation and for the assistant's own acts, as ``prompts.WentWrongShown`` lines.

What is another person's stays theirs: a fact about someone else's request, made by someone else,
is never in this turn; one of his own acts that is about nobody is shown without the words of a
person who is not the conversation's (their name and what they said), and a reason said by another
person (an undo's words, a refusal's) is never shown in this turn (ruling R76): only that it
happened, by whom and on what. "Lately" is the last ``LATELY`` days.
"""

from datetime import datetime, timedelta
from typing import Final
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.learning.models import WentWrong
from domo_core.plans.schedule import as_local
from domo_core.prompts import MAX_WENT_WRONG_SHOWN, WentWrongShown

# What a fact says when no person did it: the assistant himself (his judge, his own call), or the
# code (an expiry, a failure).
_THE_ASSISTANT = "the assistant"
_THE_RULES = "the rules"
_SOMEONE = "someone"
# How far back "what went wrong lately" goes.
LATELY: Final = timedelta(days=30)


async def went_wrong_shown(
    session: AsyncSession,
    person: Person,
    zone: ZoneInfo | None,
    *,
    now: datetime,
    on_his_own: bool = False,
) -> tuple[WentWrongShown, ...]:
    """The ``MAX_WENT_WRONG_SHOWN`` latest facts about ``person`` or made by them, of the last
    ``LATELY`` before ``now``, newest first; in a turn on his own (``on_his_own``) also his own
    acts that are about nobody."""
    own = [WentWrong.about_person_id == person.id, WentWrong.by_person_id == person.id]
    if on_his_own:
        # His own acts that are about nobody (a build abandoned, a launch refused) are for a turn
        # on his own, not for a person's.
        own.append(WentWrong.of_domo.is_(True) & WentWrong.about_person_id.is_(None))
    found = await session.scalars(
        select(WentWrong)
        .where(or_(*own), WentWrong.at >= now - LATELY)
        .order_by(WentWrong.at.desc(), WentWrong.id)
        .limit(MAX_WENT_WRONG_SHOWN)
    )
    facts = list(found)
    names = {
        row.id: row.display_name
        for row in await session.scalars(
            select(Person).where(
                Person.id.in_({fact.by_person_id for fact in facts if fact.by_person_id})
            )
        )
    }
    return tuple(_shown(fact, person, names, zone) for fact in facts)


def _shown(
    fact: WentWrong, person: Person, names: dict[UUID, str], zone: ZoneInfo | None
) -> WentWrongShown:
    theirs = person.id in (fact.about_person_id, fact.by_person_id)
    by: str | None = _THE_ASSISTANT if fact.by_domo else _THE_RULES
    if fact.by_person_id is not None:
        by = names.get(fact.by_person_id, _SOMEONE) if theirs else _SOMEONE
    # A reason said by someone else than the person of the conversation stays theirs (R76).
    said_by_someone_else = fact.by_person_id is not None and fact.by_person_id != person.id
    return WentWrongShown(
        id=str(fact.id),
        kind=fact.kind,
        action_type=fact.action_type,
        subject=fact.subject_key,
        when=as_local(fact.at, zone) or "",
        by=by,
        reason=None if said_by_someone_else else fact.reason,
        asked_why=fact.asked_why_at is not None,
    )
