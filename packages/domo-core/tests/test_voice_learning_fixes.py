"""Learning a voice: what the review of the first round found (passing prints that teach, a known
voice learnt as another, unlearning a wrong base, bad vectors, unknown voices that last)."""

import math
from datetime import timedelta

import pytest
from domo_core.channels.voice.models import MAX_DIMENSIONS, VoicePrint, VoiceRef
from domo_core.channels.voice.prints import (
    _mix,
    learn,
    reference_for,
    sweep,
    unlearn,
)
from domo_core.channels.voice.thresholds import UNKNOWN_LIVES
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from voice_support import (
    ALEX,
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
    session, who, vector, *, at=NOW, microphone=MIC, seconds=3.0, pitch_hz=None, sure=True
):
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


async def _rows(session, who):
    query = (
        select(VoicePrint)
        .join(VoiceRef, VoiceRef.id == VoicePrint.ref_id)
        .where(VoiceRef.person_id == who.id)
        .order_by(VoicePrint.kind)
    )
    return list((await session.scalars(query)).all())


async def _count(session, table=VoicePrint):
    return await session.scalar(select(func.count()).select_from(table))


async def test_the_same_cold_voice_said_often_never_moves_the_base_or_the_recent(session):
    alex = await person(session)
    await learnt_voice(session, alex, ALEX)
    cold = (1.0, 2.0, 0.0, 0.0)
    results = [await _learn(session, alex, cold, at=NOW + n * HOUR) for n in range(10)]
    assert results == ["passing"] * 10
    rows = {row.kind: row for row in await _rows(session, alex)}
    assert rows["base"].vector == pytest.approx(list(ALEX))
    assert rows["recent"].vector == pytest.approx(list(ALEX))
    assert rows["base"].utterances == rows["recent"].utterances == 5


async def test_a_stranger_said_often_never_makes_a_passing_print_or_moves_anything(session):
    alex = await person(session)
    await learnt_voice(session, alex, ALEX)
    results = [await _learn(session, alex, SAM, at=NOW + n * HOUR) for n in range(10)]
    assert results == ["nothing"] * 10
    rows = await _rows(session, alex)
    assert sorted(row.kind for row in rows) == ["base", "recent"]
    for row in rows:
        assert row.vector == pytest.approx(list(ALEX))


async def test_a_known_voice_is_not_learnt_as_another_person(session):
    alex = await person(session)
    sam = await person(session, "Sam", "adult")
    await learnt_voice(session, sam, SAM)
    results = [await _learn(session, alex, SAM, at=NOW + n * 2 * HOUR) for n in range(6)]
    assert results == ["nothing"] * 6
    assert await _rows(session, alex) == []


async def test_that_was_not_me_also_removes_a_base_learnt_in_the_wrong_period(session):
    alex = await person(session)
    lent_at = NOW
    await _learn(session, alex, SAM, at=lent_at + HOUR)
    await _learn(session, alex, SAM, at=lent_at + HOUR + timedelta(minutes=1))
    assert await _learn(session, alex, SAM, at=lent_at + 3 * HOUR) == "learnt"
    assert sorted(row.kind for row in await _rows(session, alex)) == ["base", "recent"]
    assert await unlearn(session, person_id=alex.id, since=lent_at, microphone=None) == 2
    assert await _rows(session, alex) == []


async def test_an_older_base_survives_a_since(session):
    alex = await person(session)
    await learnt_voice(session, alex, ALEX)
    assert await unlearn(session, person_id=alex.id, since=NOW + HOUR, microphone=None) == 0


async def test_the_spread_of_a_habit_never_shrinks_to_nothing(session):
    alex = await person(session)
    await learnt_voice(session, alex, ALEX, habits=(120.0, 6.5, 4.0, 0.5))
    for n in range(150):
        await _learn(session, alex, ALEX, at=NOW + n * HOUR, pitch_hz=120.0)
    base = next(row for row in await _rows(session, alex) if row.kind == "base")
    assert base.pitch_spread >= 0.05 * 120.0 - 1e-6


async def test_a_vector_that_is_no_print_is_learnt_and_referenced_as_nothing(session):
    alex = await person(session)
    bad = (float("nan"), 0.0, 0.0, 0.0)
    result = await learn(
        session,
        person_id=alex.id,
        microphone=MIC,
        print_=print_of(bad),
        thresholds=THRESHOLDS,
        sure_otherwise=True,
        visit_at=NOW,
        now=NOW,
    )
    assert result == "nothing"
    reference = await reference_for(
        session, person_id=None, print_=print_of(bad), microphone=MIC, now=NOW
    )
    assert reference.startswith("v-")
    assert await _count(session) == 0
    assert await _count(session, VoiceRef) == 0


async def test_a_new_unknown_voice_shorter_than_the_minimum_stores_nothing(session):
    reference = await reference_for(
        session, person_id=None, print_=print_of(ALEX, seconds=0.8), microphone=MIC, now=NOW
    )
    assert reference.startswith("v-")
    assert await _count(session) == 0
    assert await _count(session, VoiceRef) == 0


async def test_strangers_never_chain_into_one_reference(session):
    """Voices each near the one before and far from the first never share one reference."""
    steps = [
        (math.cos(math.radians(35 * n)), math.sin(math.radians(35 * n)), 0.0, 0.0) for n in range(7)
    ]
    refs = [
        await reference_for(session, person_id=None, print_=print_of(v), microphone=MIC, now=NOW)
        for v in steps
    ]
    assert refs[0] == refs[1]
    assert refs[2] != refs[0]
    assert len(set(refs)) >= 3


async def test_an_unknown_voice_is_forgotten_thirty_days_after_it_was_last_heard(session):
    ref = await reference_for(
        session, person_id=None, print_=print_of(ALEX), microphone=MIC, now=NOW
    )
    again = await reference_for(
        session,
        person_id=None,
        print_=print_of(ALEX),
        microphone=MIC,
        now=NOW + timedelta(days=20),
    )
    assert again == ref
    assert await sweep(session, NOW + UNKNOWN_LIVES + timedelta(days=1)) == 0
    assert await sweep(session, NOW + timedelta(days=20) + UNKNOWN_LIVES + HOUR) == 2
    assert await _count(session) == 0
    assert await _count(session, VoiceRef) == 0


async def test_a_known_person_candidate_is_not_swept(session):
    alex = await person(session)
    await _learn(session, alex, ALEX)
    assert await sweep(session, NOW + timedelta(days=400)) == 0
    assert [row.kind for row in await _rows(session, alex)] == ["candidate"]


async def test_the_vector_has_a_bounded_size(session):
    ref = VoiceRef(reference="v-0000000000000001")
    session.add(ref)
    await session.flush()
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(
                VoicePrint(
                    ref_id=ref.id,
                    microphone=MIC,
                    kind="candidate",
                    model=MODEL,
                    vector=[0.1] * (MAX_DIMENSIONS + 1),
                )
            )
            await session.flush()


def test_mixing_vectors_of_unequal_length_keeps_the_old_one():
    assert _mix([1.0, 0.0], [0.0, 1.0, 0.0], 0.5) == [1.0, 0.0]


async def test_two_utterances_inserting_the_same_row_do_not_crash(session, monkeypatch):
    from domo_core.channels.voice import prints as voice_prints

    ref = await reference_for(
        session, person_id=None, print_=print_of(ALEX), microphone=MIC, now=NOW
    )
    real, calls = voice_prints._first_candidate, []

    async def blind_once(*args, **kwargs):
        calls.append(1)
        return None if len(calls) == 1 else await real(*args, **kwargs)

    monkeypatch.setattr(voice_prints, "_first_candidate", blind_once)
    again = await reference_for(
        session, person_id=None, print_=print_of(ALEX), microphone=MIC, now=NOW
    )
    assert again == ref
    assert await _count(session) == 1
