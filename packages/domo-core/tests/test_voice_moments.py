"""The reserved moments of a voice (spec 6.9): one live moment per person, gone after three
minutes, advanced one saying at a time and closed when over."""

from datetime import timedelta

import pytest
from domo_core.channels.voice.models import VoiceMoment
from domo_core.channels.voice.moments import (
    MOMENT_LIVES,
    advance,
    close_moment,
    moment_of,
    open_moment,
)
from sqlalchemy import func, select
from voice_support import NOW, person


async def count(session) -> int:
    return await session.scalar(select(func.count()).select_from(VoiceMoment)) or 0


async def test_one_live_moment_per_person(session):
    alex = await person(session)
    dana = await person(session, "Dana", "adult")
    await open_moment(
        session, person_id=alex.id, microphone="desk", purpose="phrase_agree", now=NOW
    )
    await open_moment(
        session, person_id=dana.id, microphone=None, purpose="stop_listening", now=NOW
    )
    # A new one replaces the person's own, and only theirs.
    later = NOW + timedelta(seconds=30)
    await open_moment(
        session, person_id=alex.id, microphone=None, purpose="phrase_change", now=later
    )
    assert await count(session) == 2
    mine = await moment_of(session, person_id=alex.id, now=later)
    assert mine is not None
    assert (mine.purpose, mine.microphone, mine.step) == ("phrase_change", None, 0)
    assert mine.expires_at == later + MOMENT_LIVES
    theirs = await moment_of(session, person_id=dana.id, now=later)
    assert theirs is not None and theirs.purpose == "stop_listening"


async def test_a_moment_goes_away_after_three_minutes(session):
    alex = await person(session)
    assert timedelta(minutes=3) == MOMENT_LIVES
    await open_moment(session, person_id=alex.id, microphone=None, purpose="phrase_agree", now=NOW)
    just_in = NOW + timedelta(minutes=3) - timedelta(seconds=1)
    assert await moment_of(session, person_id=alex.id, now=just_in) is not None
    assert await moment_of(session, person_id=alex.id, now=NOW + timedelta(minutes=3)) is None
    assert await moment_of(session, person_id=alex.id, now=NOW + timedelta(minutes=4)) is None


async def test_a_moment_advances_by_one_saying_and_closes(session):
    alex = await person(session)
    await open_moment(session, person_id=alex.id, microphone=None, purpose="phrase_agree", now=NOW)
    moment = await moment_of(session, person_id=alex.id, now=NOW)
    assert moment is not None
    await advance(session, moment)
    again = await moment_of(session, person_id=alex.id, now=NOW)
    assert again is not None and again.step == 1
    await close_moment(session, again)
    assert await moment_of(session, person_id=alex.id, now=NOW) is None
    assert await count(session) == 0


async def test_the_database_keeps_one_moment_per_person(session):
    from domo_core.channels.voice.models import VoiceMoment
    from sqlalchemy.exc import IntegrityError

    alex = await person(session)
    await open_moment(session, person_id=alex.id, microphone=None, purpose="phrase_agree", now=NOW)
    session.add(
        VoiceMoment(
            person_id=alex.id, purpose="stop_listening", expires_at=NOW + timedelta(minutes=1)
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()
