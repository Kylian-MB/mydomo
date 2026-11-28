"""Learning a voice, second round of review: learning reads raw degrees only, a passing print never
says "for", a new microphone is learnt from raw proof only, a known person's voice is never
learnt as another's."""

from datetime import timedelta

import pytest
from domo_core.channels.voice.models import VoicePrint, VoiceRef
from domo_core.channels.voice.prints import learn, read_voice, reference_for
from domo_core.channels.voice.thresholds import AGAINST_AT
from domo_core.llm.speech.client import PrintThresholds
from sqlalchemy import func, select, update
from voice_support import (
    ALEX,
    DANA,
    MIC,
    NOW,
    SAM,
    THRESHOLDS,
    TOM,
    at_degree,
    learnt_voice,
    person,
    print_of,
    with_cosine,
)

HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
HEADSET = "headset"
COLD = (1.0, 2.0, 0.0, 0.0)  # a raw degree of 0.39 against ALEX: the middle band
HABITS = (120.0, 10.0, 4.0, 0.5)


async def _hear(session, who, vector, *, at=NOW, microphone=MIC, pitch_hz=None, rate=None):
    """One utterance: the reading, then what was learnt from it."""
    print_ = print_of(vector, pitch_hz=pitch_hz, rate=rate)
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


def _kept(rows, kinds=("base", "recent")):
    return {(row.microphone, row.kind): list(row.vector) for row in rows if row.kind in kinds}


# --- R1: learning reads raw degrees ---------------------------------------------------------------


async def test_a_raw_middle_degree_with_a_fitting_pitch_does_not_move_base_or_recent(session):
    alex = await person(session)
    await learnt_voice(session, alex, ALEX, habits=HABITS)
    before = _kept(await _rows(session, alex))
    reading, result = await _hear(session, alex, at_degree(0.55), pitch_hz=120.0, rate=4.0)
    assert reading.weighs == "unknown"  # intonation never lifts a reading to "for"
    assert reading.degree < 0.6
    assert reading.core_degree == pytest.approx(0.55, abs=1e-3)  # learnt from raw
    assert result == "passing"
    assert _kept(await _rows(session, alex)) == before


async def test_a_raw_far_degree_with_a_fitting_pitch_makes_no_passing_print(session):
    alex = await person(session)
    await learnt_voice(session, alex, ALEX, habits=HABITS)
    for n in range(10):
        reading, result = await _hear(
            session, alex, at_degree(0.25), at=NOW + n * HOUR, pitch_hz=120.0, rate=4.0
        )
        assert result == "nothing"
        assert reading.weighs != "for"
    assert sorted(row.kind for row in await _rows(session, alex)) == ["base", "recent"]


# --- a passing print never says "for" ---------------------------------------------------------


async def test_a_voice_in_the_band_never_reads_for_in_a_week_and_nothing_moves(session):
    alex = await person(session)
    await learnt_voice(session, alex, ALEX)
    before = _kept(await _rows(session, alex))
    for n in range(20):
        reading, _ = await _hear(session, alex, COLD, at=NOW + n * 8 * HOUR)
        assert reading.weighs != "for"
        assert reading.degree < 0.6
        assert _kept(await _rows(session, alex)) == before
    assert reading.via_passing and reading.weighs == "unknown"
    assert reading.degree >= AGAINST_AT


async def test_alex_with_a_cold_is_unsure_for_the_days_of_the_cold_and_for_again_when_healthy(
    session,
):
    alex = await person(session)
    await learnt_voice(session, alex, ALEX)
    before = _kept(await _rows(session, alex))
    first, _ = await _hear(session, alex, COLD, at=NOW)
    assert (first.weighs, first.via_passing) == ("unknown", False)
    for day in range(1, 4):
        reading, _ = await _hear(session, alex, COLD, at=NOW + day * DAY)
        assert (reading.weighs, reading.via_passing) == ("unknown", True)
    assert _kept(await _rows(session, alex)) == before
    healthy, result = await _hear(session, alex, ALEX, at=NOW + 3 * DAY + HOUR)
    assert (healthy.weighs, healthy.via_passing, result) == ("for", False, "updated")


# --- R3: a new microphone is learnt from raw proof only -----------------------------------------


async def test_a_passing_print_on_one_microphone_never_gives_a_base_on_another(session):
    alex = await person(session)
    await learnt_voice(session, alex, ALEX)
    await _hear(session, alex, COLD, at=NOW)
    for n in range(20):
        reading, result = await _hear(
            session, alex, COLD, at=NOW + n * 2 * HOUR, microphone=HEADSET
        )
        assert reading.weighs != "for"
        assert result == "nothing"
    assert {row.microphone for row in await _rows(session, alex)} == {MIC}


async def test_intonation_alone_never_opens_a_new_microphone(session):
    alex = await person(session)
    await learnt_voice(session, alex, ALEX, habits=HABITS)
    # A raw 0.70 on another microphone is 0.56 at its lesser weight: under "for" even with the step.
    heard = at_degree(0.7)
    for n in range(6):
        _, result = await _hear(
            session, alex, heard, at=NOW + n * 2 * HOUR, microphone=HEADSET, pitch_hz=120.0
        )
        assert result == "nothing"
    assert {row.microphone for row in await _rows(session, alex)} == {MIC}


# --- R4: a known person's voice is not learnt as another's ----------------------------------------


async def test_a_known_voice_on_a_microphone_it_has_no_print_on_is_never_a_candidate(session):
    alex = await person(session)
    sam = await person(session, "Sam", "adult")
    await learnt_voice(session, sam, SAM)
    # Raw 0.72 for Sam; on a microphone he has no print on it would be 0.58 at the lesser weight,
    # but the nearest known other is read with no discount for a microphone (round 4).
    heard = with_cosine(SAM, 0.61, TOM)
    for n in range(8):
        reading, result = await _hear(
            session, alex, heard, at=NOW + n * 2 * HOUR, microphone=HEADSET
        )
        assert result == "nothing"
    assert reading.best_other == sam.id
    assert reading.other_degree == pytest.approx(reading.other_raw) == pytest.approx(0.72)
    assert await _rows(session, alex) == []


async def test_a_faint_resemblance_to_a_known_person_does_not_block_the_page_person(session):
    alex = await person(session)
    sam = await person(session, "Sam", "adult")
    await learnt_voice(session, alex, ALEX)
    await learnt_voice(session, sam, SAM)
    heard = (0.9, 0.425, 0.1, 0.0)  # close to Alex, only a little like Sam (degree 0.35)
    reading, result = await _hear(session, alex, heard, microphone=HEADSET)
    assert AGAINST_AT <= reading.other_raw < 0.6
    assert result == "candidate"


async def test_a_candidate_whose_mean_has_become_a_known_voice_is_not_promoted(session):
    alex = await person(session)
    dana = await person(session, "Dana", "adult")
    ref = await learnt_voice(session, alex, ALEX)
    await learnt_voice(session, dana, DANA)
    session.add(
        VoicePrint(
            ref_id=ref.id,
            microphone=HEADSET,
            kind="candidate",
            model="print-a",
            vector=[0.6, 0.0, 0.8, 0.0],
            utterances=2,
            visits=2,
            first_visit_at=NOW - DAY,
            last_at=NOW - 2 * HOUR,
        )
    )
    await session.flush()
    heard = (0.954, 0.0, 0.301, 0.0)  # Alex's voice, hardly Dana's, joined to Dana-like candidate
    _, result = await _hear(session, alex, heard, microphone=HEADSET)
    assert result == "nothing"
    kinds = {row.kind for row in await _rows(session, alex) if row.microphone == HEADSET}
    assert "base" not in kinds


# --- the minors --------------------------------------------------------------------------------


async def test_habits_come_from_the_same_microphone_and_the_same_model(session):
    alex = await person(session)
    await learnt_voice(session, alex, ALEX, habits=HABITS)
    await learnt_voice(session, alex, ALEX, microphone=HEADSET, habits=(200.0, 10.0, 4.0, 0.5))
    await learnt_voice(session, alex, ALEX, model="print-b", habits=(300.0, 10.0, 4.0, 0.5))
    await session.execute(
        update(VoicePrint).where(VoicePrint.model == "print-b").values(last_at=NOW + HOUR)
    )
    await session.flush()

    async def fits(microphone, pitch):
        reading = await read_voice(
            session,
            person_id=alex.id,
            microphone=microphone,
            print_=print_of(ALEX, pitch_hz=pitch),
            thresholds=THRESHOLDS,
            now=NOW,
        )
        return reading.intonation_fits

    assert await fits(MIC, 120.0) is True
    assert await fits(HEADSET, 200.0) is True
    assert await fits(MIC, 200.0) is False


async def test_an_unknown_voice_on_a_new_microphone_keeps_one_first_print(session):
    one = await reference_for(
        session, person_id=None, print_=print_of(ALEX), microphone=MIC, now=NOW
    )
    again = await reference_for(
        session, person_id=None, print_=print_of(ALEX), microphone=HEADSET, now=NOW + HOUR
    )
    assert one == again
    assert await session.scalar(select(func.count()).select_from(VoicePrint)) == 1


@pytest.mark.parametrize("bounds", [(0.25, 0.9, 0.75), (0.25, 0.1, 0.75), (0.25, 0.8, 0.75)])
async def test_bounds_out_of_order_are_like_no_bounds(session, bounds):
    alex = await person(session)
    await learnt_voice(session, alex, ALEX)
    reading = await read_voice(
        session,
        person_id=alex.id,
        microphone=MIC,
        print_=print_of(ALEX),
        thresholds=PrintThresholds(*bounds),
        now=NOW,
    )
    assert (reading.weighs, reading.learnt) == ("unknown", False)


async def test_when_an_insert_falls_back_to_an_existing_row_the_utterance_counts_on_it(
    session, monkeypatch
):
    from domo_core.channels.voice import prints as voice_prints

    alex = await person(session)
    await _hear(session, alex, ALEX, at=NOW)
    real, calls = voice_prints._person_rows, []

    async def blind_once(*args, **kwargs):
        calls.append(1)
        return [] if len(calls) == 1 else await real(*args, **kwargs)

    monkeypatch.setattr(voice_prints, "_person_rows", blind_once)
    _, result = await _hear(session, alex, ALEX, at=NOW + timedelta(minutes=1))
    assert result == "candidate"
    rows = await _rows(session, alex)
    assert [(row.kind, row.utterances) for row in rows] == [("candidate", 2)]
