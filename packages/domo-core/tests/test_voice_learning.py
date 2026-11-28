"""Learning a voice slowly: candidates, base and recent prints, passing prints, forgetting."""

from datetime import timedelta

import pytest
from domo_core.channels.voice.models import VoicePrint, VoiceRef
from domo_core.channels.voice.prints import (
    erase_reference,
    learn,
    read_voice,
    reference_for,
    sweep,
    unlearn,
)
from domo_core.channels.voice.thresholds import (
    BASE_WEIGHT,
    PASSING_LIVES,
    RECENT_WEIGHT,
    VISIT_GAP,
)
from sqlalchemy import func, select
from voice_support import (
    ALEX,
    DANA,
    MIC,
    MODEL,
    NOW,
    SAM,
    THRESHOLDS,
    learnt_voice,
    person,
    print_of,
)

HOUR = timedelta(hours=1)


async def _learn(
    session, who, vector, *, at=NOW, sure=True, microphone=MIC, seconds=3.0, pitch_hz=None
):
    """One utterance heard at ``at``, learnt from."""
    print_ = print_of(vector, seconds=seconds, pitch_hz=pitch_hz)
    return await learn(
        session,
        person_id=who.id,
        microphone=microphone,
        print_=print_,
        thresholds=THRESHOLDS,
        sure_otherwise=sure,
        visit_at=at,
        now=at,
    )


async def _rows(session, who=None):
    query = select(VoicePrint).join(VoiceRef, VoiceRef.id == VoicePrint.ref_id)
    if who is not None:
        query = query.where(VoiceRef.person_id == who.id)
    return list((await session.scalars(query.order_by(VoicePrint.kind))).all())


def _kinds(rows):
    return sorted(row.kind for row in rows)


async def test_one_visit_is_not_enough(session):
    alex = await person(session)
    results = [await _learn(session, alex, ALEX, at=NOW + timedelta(minutes=n)) for n in range(6)]
    assert results == ["candidate"] * 6
    assert _kinds(await _rows(session, alex)) == ["candidate"]


async def test_three_like_utterances_on_two_visits_learn_the_voice(session):
    alex = await person(session)
    first = await _learn(session, alex, ALEX, at=NOW)
    second = await _learn(session, alex, (0.97, 0.1, 0.0, 0.0), at=NOW + timedelta(minutes=1))
    third = await _learn(session, alex, ALEX, at=NOW + VISIT_GAP + timedelta(minutes=5))
    assert [first, second, third] == ["candidate", "candidate", "learnt"]
    assert _kinds(await _rows(session, alex)) == ["base", "recent"]
    reading = await read_voice(
        session,
        person_id=alex.id,
        microphone=MIC,
        print_=print_of(ALEX),
        thresholds=THRESHOLDS,
        now=NOW + 2 * VISIT_GAP,
    )
    assert reading.learnt and reading.weighs == "for"


async def test_unlike_utterances_restart_the_candidate(session):
    """Alex and Sam in turn at Alex's page, hours apart: the code is "sure" of the page each time,
    and the voice is never learnt."""
    alex = await person(session)
    results = []
    for n in range(12):
        vector = ALEX if n % 2 == 0 else SAM
        results.append(await _learn(session, alex, vector, at=NOW + n * 2 * HOUR))
    assert "learnt" not in results
    assert _kinds(await _rows(session, alex)) == ["candidate"]


async def test_nothing_is_learnt_when_not_sure_otherwise(session):
    alex = await person(session)
    assert await _learn(session, alex, ALEX, sure=False) == "nothing"
    assert await _rows(session) == []
    await learnt_voice(session, alex, ALEX)
    assert await _learn(session, alex, ALEX, sure=False) == "nothing"


async def test_a_short_utterance_teaches_nothing(session):
    alex = await person(session)
    assert await _learn(session, alex, ALEX, seconds=0.8) == "nothing"
    assert await _rows(session) == []


async def test_without_thresholds_nothing_is_learnt(session):
    alex = await person(session)
    result = await learn(
        session,
        person_id=alex.id,
        microphone=MIC,
        print_=print_of(ALEX),
        thresholds=None,
        sure_otherwise=True,
        visit_at=NOW,
        now=NOW,
    )
    assert result == "nothing"
    assert await _rows(session) == []


async def test_the_recent_print_follows_a_voice_that_drifts_and_the_base_moves_slowly(session):
    alex = await person(session)
    await learnt_voice(session, alex, ALEX)
    drifted = (1.0, 0.5, 0.0, 0.0)
    assert await _learn(session, alex, drifted) == "updated"
    rows = {row.kind: row for row in await _rows(session, alex)}
    assert rows["recent"].vector == pytest.approx(
        [(1 - RECENT_WEIGHT) * a + RECENT_WEIGHT * b for a, b in zip(ALEX, drifted, strict=True)],
        abs=1e-5,
    )
    assert rows["base"].vector == pytest.approx(
        [(1 - BASE_WEIGHT) * a + BASE_WEIGHT * b for a, b in zip(ALEX, drifted, strict=True)],
        abs=1e-5,
    )
    assert rows["recent"].utterances == 6


async def test_a_cold_goes_to_a_passing_print_and_leaves_the_base(session):
    alex = await person(session)
    await learnt_voice(session, alex, ALEX)
    cold = (1.0, 2.0, 0.0, 0.0)  # a middle degree: unsure
    assert await _learn(session, alex, cold, at=NOW) == "passing"
    rows = {row.kind: row for row in await _rows(session, alex)}
    assert rows["base"].vector == pytest.approx(list(ALEX))
    assert rows["recent"].vector == pytest.approx(list(ALEX))
    assert rows["passing"].vector == pytest.approx(list(cold))
    assert rows["passing"].expires_at == NOW + PASSING_LIVES
    # Still ill the next day: the passing print keeps it unsure, with a marker, never "for".
    reading = await read_voice(
        session,
        person_id=alex.id,
        microphone=MIC,
        print_=print_of(cold),
        thresholds=THRESHOLDS,
        now=NOW + timedelta(days=1),
    )
    assert reading.weighs == "unknown" and reading.via_passing


async def test_a_voice_nearer_someone_else_is_not_kept_as_passing(session):
    alex = await person(session)
    dana = await person(session, "Dana", "adult")
    await learnt_voice(session, alex, ALEX)
    await learnt_voice(session, dana, DANA)
    assert await _learn(session, alex, DANA) == "nothing"
    assert "passing" not in _kinds(await _rows(session, alex))


async def test_a_passing_print_goes_away_alone(session):
    alex = await person(session)
    await learnt_voice(session, alex, ALEX)
    await _learn(session, alex, (1.0, 2.0, 0.0, 0.0), at=NOW)
    assert await sweep(session, NOW + PASSING_LIVES - HOUR) == 0
    assert await sweep(session, NOW + PASSING_LIVES + HOUR) == 1
    assert _kinds(await _rows(session, alex)) == ["base", "recent"]


async def test_a_new_microphone_is_learnt_when_the_others_agree(session):
    alex = await person(session)
    await learnt_voice(session, alex, ALEX)
    first = await _learn(session, alex, ALEX, microphone="headset", at=NOW)
    second = await _learn(session, alex, ALEX, microphone="headset", at=NOW + 2 * HOUR)
    third = await _learn(session, alex, ALEX, microphone="headset", at=NOW + 2 * HOUR)
    assert [first, second, third] == ["candidate", "candidate", "learnt"]
    mics = {(row.microphone, row.kind) for row in await _rows(session, alex)}
    assert ("headset", "base") in mics and ("headset", "recent") in mics


async def test_a_new_microphone_learns_nothing_when_the_others_disagree(session):
    alex = await person(session)
    await learnt_voice(session, alex, ALEX)
    assert await _learn(session, alex, SAM, microphone="headset") == "nothing"


async def test_that_was_not_me_removes_what_was_learnt(session):
    alex = await person(session)
    await learnt_voice(session, alex, ALEX)
    await _learn(session, alex, (1.0, 2.0, 0.0, 0.0), at=NOW + 3 * HOUR)  # passing
    await _learn(session, alex, ALEX, microphone="headset", at=NOW + 3 * HOUR)  # candidate
    # Only what came after the page was lent: the base stays.
    removed = await unlearn(session, person_id=alex.id, since=NOW + 2 * HOUR, microphone=None)
    assert removed == 2
    assert _kinds(await _rows(session, alex)) == ["base", "recent"]
    # Everything, base included.
    removed = await unlearn(session, person_id=alex.id, since=None, microphone=None)
    assert removed == 2
    assert await _rows(session, alex) == []
    reading = await read_voice(
        session,
        person_id=alex.id,
        microphone=MIC,
        print_=print_of(ALEX),
        thresholds=THRESHOLDS,
        now=NOW,
    )
    assert not reading.learnt


async def test_unlearning_one_microphone_leaves_the_others(session):
    alex = await person(session)
    await learnt_voice(session, alex, ALEX)
    await learnt_voice(session, alex, ALEX, microphone="headset", model=MODEL)
    removed = await unlearn(session, person_id=alex.id, since=None, microphone="headset")
    assert removed == 2
    assert {row.microphone for row in await _rows(session, alex)} == {MIC}


async def test_a_person_has_one_reference_made_at_need(session):
    alex = await person(session)
    first = await reference_for(
        session, person_id=alex.id, print_=print_of(ALEX), microphone=MIC, now=NOW
    )
    again = await reference_for(
        session, person_id=alex.id, print_=print_of(SAM), microphone=MIC, now=NOW
    )
    assert first == again
    assert first.startswith("v-") and len(first) == 18


async def test_an_unknown_voice_keeps_its_reference_and_another_gets_its_own(session):
    one = await reference_for(
        session, person_id=None, print_=print_of(ALEX), microphone=MIC, now=NOW
    )
    again = await reference_for(
        session,
        person_id=None,
        print_=print_of((0.98, 0.1, 0.0, 0.0)),
        microphone="headset",
        now=NOW,
    )
    other = await reference_for(
        session, person_id=None, print_=print_of(SAM), microphone=MIC, now=NOW
    )
    assert one == again
    assert other != one


async def test_a_reference_is_erased_with_its_prints(session):
    ref = await reference_for(
        session, person_id=None, print_=print_of(ALEX), microphone=MIC, now=NOW
    )
    assert await _count(session) == 1
    await erase_reference(session, ref)
    assert await _count(session) == 0
    await erase_reference(session, ref)  # nothing left: no error


async def _count(session):
    return await session.scalar(select(func.count()).select_from(VoicePrint))
