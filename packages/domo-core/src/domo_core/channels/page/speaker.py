"""Who speaks on the page: the person of the session, which the code is sure of."""

import uuid
from datetime import datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from domo_core.accounts.models import WebSession
from domo_core.accounts.sessions import SESSION_COOKIE, session_id_of
from domo_core.conversation.speaker import Clue, Speaker

# The name of the page's channel, for the judge, the audit and what reached whom.
PAGE_CHANNEL: Final = "page"


def page_speaker(
    person_id: uuid.UUID, now: datetime, signed_in_at: datetime | None = None
) -> Speaker:
    """The speaker of a request that came with the session of ``person_id``: the session is a
    strong clue, made by the code, that it is them, dated when they signed in (``signed_in_at``,
    ``signed_in_at_of``; ``now`` when it is not known)."""
    at = now if signed_in_at is None else signed_in_at
    return Speaker(person_id, PAGE_CHANNEL, clues=(Clue("session", "for", "strong", "code", at),))


async def signed_in_at_of(session: AsyncSession, request: Request) -> datetime | None:
    """When the session the request's cookie carries was opened (the person signed in), or
    ``None`` when it carries none that is known. Reads only."""
    raw_token = request.cookies.get(SESSION_COOKIE)
    if not raw_token:
        return None
    return await signed_in_at_of_token(session, raw_token)


async def signed_in_at_of_token(session: AsyncSession, raw_token: str) -> datetime | None:
    """When the session of ``raw_token`` was opened, or ``None`` when it is not known. Reads
    only."""
    session_id = await session_id_of(session, raw_token)
    if session_id is None:
        return None
    return await session.scalar(select(WebSession.created_at).where(WebSession.id == session_id))
