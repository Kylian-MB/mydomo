"""Learning a voice, third round of review: intonation never crosses a threshold, a cold is never a
stranger, passing prints name no other person, an unlearnt person is learnt beside a relative."""

from datetime import timedelta

import pytest
from domo_core.channels.voice.models import VoicePrint, VoiceRef
from domo_core.channels.voice.prints import learn, read_voice
from domo_core.channels.voice.thresholds import AGAINST_AT, FOR_AT
from sqlalchemy import select
from voice_support import (
    ALEX,
    MIC,
    NOW,
    SAM,
    THRESHOLDS,
    at_degree,
    learnt_voice,
    person,
    print_of,
    with_cosine,
)

HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
HEADSET = "headset"
COLD = (1.0, 2.0, 0.0, 0.0)  # a raw degree of 0.39 against ALEX
HABITS = (120.0, 10.0, 4.0, 0.5)
FITTING = {"pitch_hz": 120.0, "rate": 4.0}
MISFIT = {"pitch_hz": 220.0, "rate": 4.0}


async def _hear(session, who, vector, *, at=NOW, microphone=MIC, **measures):
    print_ = print_of(vector, **measures)
    reading = await read_voice(
        session,
        person_id=who.id,
        microphone=microphone,
        print_=print_,
        thresholds=THRESHOLDS,
        now=at,
    )
    result = await learn(
        session,
        person_id=who.id,
        microphone=microphone,
        print_=print_,
        thresholds=THRESHOLDS,
        sure_otherwise=True,
        visit_at=at,
        now=at,
    )
    return reading, result


async def _rows(session, who):
    query = (
        select(VoicePrint)
        .join(VoiceRef, VoiceRef.id == VoicePrint.ref_id)
        .where(VoiceRef.person_id == who.id)
        .order_by(VoicePrint.microphone, VoicePrint.kind)
    )
    return list((await session.scalars(query)).all())


def _kept(rows):
    return {
        (row.microphone, row.kind): list(row.vector)
        for row in rows
        if row.kind in ("base", "recent")
    }


async def _learn_at_own_page(session, who, vector):
    """Three like utterances on two visits at the page of ``who``."""
    results = [
        (await _hear(session, who, vector, at=NOW + delay))[1]
        for delay in (timedelta(0), timedelta(minutes=1), 2 * HOUR)
    ]
    return results


# --- 1: a cold is never a stranger ----------------------------


async def test_a_cold_read_through_a_passing_print_with_a_misfit_pitch_is_not_a_stranger(session):
    alex = await person(session)
    ref = await learnt_voice(session, alex, ALEX, habits=HABITS)
    heard = with_cosine(ALEX, 0.35, SAM)  # a raw degree of 0.20 against Alex's base
    passing = with_cosine(heard, 0.425, (0.0, 0.0, 0.0, 1.0))  # the passing print: degree 0.35
    session.add(
        VoicePrint(
            ref_id=ref.id,
            microphone=MIC,
            kind="passing",
            model="print-a",
            vector=list(passing),
            first_visit_at=NOW,
            last_at=NOW,
            expires_at=NOW + 3 * DAY,
        )
    )
    await session.flush()
    for measures in (MISFIT, FITTING, {}):
        reading, _ = await _hear(session, alex, heard, at=NOW + HOUR, **measures)
        assert reading.via_passing
        assert reading.degree >= AGAINST_AT
        assert not reading.resembles_nobody
        assert reading.weighs == "unknown"


# --- 2: a passing print names nobody ----------------------------


async def test_a_passing_print_holding_a_strangers_voice_does_not_block_that_voice_at_her_page(
    session,
):
    alex = await person(session)
    dana = await person(session, "Dana", "adult")
    await learnt_voice(session, alex, ALEX)
    heard = at_degree(0.4)
    await _hear(session, alex, heard)  # a band voice: a passing print
    assert "passing" in {row.kind for row in await _rows(session, alex)}
    reading, result = await _hear(session, dana, heard)
    assert reading.other_raw < 0.6
    assert result == "candidate"


# --- 3: intonation never moves a reading across a threshold ----------------------------


@pytest.mark.parametrize(
    ("raw", "measures", "low", "high"),
    [
        (0.55, FITTING, AGAINST_AT, FOR_AT),  # cannot reach "for"
        (0.59, FITTING, AGAINST_AT, FOR_AT),
        (0.50, FITTING, AGAINST_AT, FOR_AT),
        (0.45, FITTING, 0.45, FOR_AT),  # inside the band it nuances up...
        (0.45, MISFIT, AGAINST_AT, 0.45),  # ...and down
        (0.35, MISFIT, AGAINST_AT, 0.35),  # cannot lower under "against"
        (0.25, FITTING, 0.0, AGAINST_AT),  # cannot lift a far voice into the band
        (0.65, MISFIT, FOR_AT, 1.0),  # cannot lower "for"
        (0.95, FITTING, FOR_AT, 1.0),
    ],
)
async def test_intonation_nuances_inside_a_band_and_never_crosses_it(
    session, raw, measures, low, high
):
    alex = await person(session)
    await learnt_voice(session, alex, ALEX, habits=HABITS)
    reading, _ = await _hear(session, alex, at_degree(raw), **measures)
    assert low - 1e-6 <= reading.degree <= high + 1e-6
    assert (reading.weighs == "for") == (raw >= FOR_AT)


# --- 4: beside a relative, an unlearnt person is learnt ----------------------------


async def test_an_unlearnt_person_beside_a_relative_is_learnt_at_her_own_page(session):
    dana = await person(session, "Dana", "adult")
    sam = await person(session, "Sam", "adult")
    await learnt_voice(session, sam, SAM)
    voice = with_cosine(SAM, 0.425, (0.0, 0.0, 1.0, 0.0))  # a degree of 0.35 against Sam
    reading = await read_voice(
        session,
        person_id=dana.id,
        microphone=MIC,
        print_=print_of(voice),
        thresholds=THRESHOLDS,
        now=NOW,
    )
    assert AGAINST_AT <= reading.other_raw < FOR_AT
    assert await _learn_at_own_page(session, dana, voice) == ["candidate", "candidate", "learnt"]


async def test_a_known_voice_at_an_unlearnt_page_is_never_learnt_as_her(session):
    dana = await person(session, "Dana", "adult")
    sam = await person(session, "Sam", "adult")
    await learnt_voice(session, sam, SAM)
    results = [(await _hear(session, dana, SAM, at=NOW + n * 2 * HOUR))[1] for n in range(8)]
    assert results == ["nothing"] * 8
    assert await _rows(session, dana) == []


# --- 5: a known other nearer than the person is not followed ----------------------------


async def test_a_known_voice_nearer_to_himself_does_not_move_the_page_persons_prints(session):
    alex = await person(session)
    sam = await person(session, "Sam", "adult")
    await learnt_voice(session, alex, (0.8, 0.6, 0.0, 0.0))
    await learnt_voice(session, sam, SAM)
    before = _kept(await _rows(session, alex))
    for n in range(5):
        reading, result = await _hear(session, alex, SAM, at=NOW + n * HOUR)
        assert FOR_AT <= reading.core_degree < reading.other_raw
        assert result == "nothing"
    assert _kept(await _rows(session, alex)) == before


# --- 6: the promotion re-check reads the mean's own degree ----------------------------------------


async def test_a_candidate_mean_nearer_a_known_person_than_the_person_is_not_promoted(session):
    alex = await person(session)
    sam = await person(session, "Sam", "adult")
    ref = await learnt_voice(session, alex, ALEX)
    await learnt_voice(session, sam, SAM)
    session.add(
        VoicePrint(
            ref_id=ref.id,
            microphone=HEADSET,
            kind="candidate",
            model="print-a",
            vector=[0.23, 0.555, 0.17, 0.781],
            utterances=2,
            visits=2,
            first_visit_at=NOW - DAY,
            last_at=NOW - 2 * HOUR,
        )
    )
    await session.flush()
    # Read alone it is Alex's (0.62 at the lesser weight) and not Sam's; with the candidate it is
    # nearer to Sam (0.43) than to Alex (0.20).
    reading, result = await _hear(session, alex, (0.639, 0.243, 0.164, 0.711), microphone=HEADSET)
    assert reading.core_degree >= FOR_AT and reading.other_raw < AGAINST_AT
    assert result == "nothing"
    assert "base" not in {
        row.kind for row in await _rows(session, alex) if row.microphone == HEADSET
    }


# --- the scenarios, with the final code ----------------------------------------------------------


@pytest.mark.parametrize("raw", [0.20, 0.23, 0.26, 0.29])
async def test_a_guest_far_from_alex_with_his_pitch_teaches_nothing(session, raw):
    alex = await person(session)
    await learnt_voice(session, alex, ALEX, habits=HABITS)
    before = _kept(await _rows(session, alex))
    for n in range(10):
        reading, result = await _hear(session, alex, at_degree(raw), at=NOW + n * HOUR, **FITTING)
        assert (reading.weighs, result) == ("unknown", "nothing")
    rows = await _rows(session, alex)
    assert {row.kind for row in rows} == {"base", "recent"}
    assert _kept(rows) == before


async def test_alexs_cold_with_a_misfit_pitch_for_three_days_then_healthy(session):
    alex = await person(session)
    await learnt_voice(session, alex, ALEX, habits=HABITS)
    before = _kept(await _rows(session, alex))
    for day in range(4):
        reading, _ = await _hear(session, alex, COLD, at=NOW + day * DAY, **MISFIT)
        assert reading.weighs == "unknown" and not reading.resembles_nobody
        assert reading.degree >= AGAINST_AT
        other, result = await _hear(
            session, alex, COLD, at=NOW + day * DAY + HOUR, microphone=HEADSET, **MISFIT
        )
        assert other.weighs != "for" and result == "nothing"
    assert _kept(await _rows(session, alex)) == before
    assert {row.microphone for row in await _rows(session, alex)} == {MIC}
    healthy, result = await _hear(session, alex, ALEX, at=NOW + 4 * DAY, **FITTING)
    assert (healthy.weighs, healthy.via_passing, result) == ("for", False, "updated")


@pytest.mark.parametrize("raw", [0.31, 0.40, 0.50, 0.58])
async def test_the_sibling_in_the_band_is_never_for_and_is_learnable_at_her_own_page(session, raw):
    alex = await person(session)
    dana = await person(session, "Dana", "adult")
    await learnt_voice(session, alex, ALEX, habits=HABITS)
    before = _kept(await _rows(session, alex))
    voice = at_degree(raw)
    for n in range(10):
        for microphone in (MIC, HEADSET):
            reading, _ = await _hear(
                session, alex, voice, at=NOW + n * HOUR, microphone=microphone, **FITTING
            )
            assert reading.weighs != "for"
    assert _kept(await _rows(session, alex)) == before
    assert {row.microphone for row in await _rows(session, alex)} == {MIC}
    # At her own page, a few days on, she is learnt: Alex's passing print of her voice names nobody.
    results = [
        (await _hear(session, dana, voice, at=NOW + 5 * DAY + delay))[1]
        for delay in (timedelta(0), timedelta(minutes=1), 2 * HOUR)
    ]
    assert results == ["candidate", "candidate", "learnt"]
