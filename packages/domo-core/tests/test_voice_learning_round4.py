"""Reading and learning a voice, fourth round: one "nearest known other" (base and recent prints,
no discount for a microphone), the reading itself says "someone else we know", learning decides on
the single print it stores, following is anchored to the base, and the table of verdicts a
consumer reads."""

import math
from datetime import timedelta

import pytest
from domo_core.channels.voice.models import VoicePrint, VoiceRef
from domo_core.channels.voice.prints import (
    Reading,
    degree_of,
    learn,
    read_voice,
    similarity,
)
from domo_core.channels.voice.thresholds import (
    AGAINST_AT,
    BASE_WEIGHT,
    FOR_AT,
    VISIT_GAP,
)
from sqlalchemy import select
from voice_support import (
    ALEX,
    ALEX_NEAR,
    DANA,
    MIC,
    MODEL,
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
HABITS = (120.0, 10.0, 4.0, 0.5)
FITTING = {"pitch_hz": 120.0, "rate": 4.0}
MISFIT = {"pitch_hz": 220.0, "rate": 4.0}


def at_angle(degrees: float) -> tuple[float, float, float, float]:
    """A unit voice in the plane of ALEX and SAM, ``degrees`` away from ALEX."""
    return (math.cos(math.radians(degrees)), math.sin(math.radians(degrees)), 0.0, 0.0)


def verdict(reading: Reading) -> str:
    """The table at the top of ``prints.py``, read in its order."""
    if reading.weighs == "for":
        return "confirms"
    if reading.weighs == "against":
        assert reading.best_other is not None
        return "someone else"
    if not reading.learnt:
        return "not learnt yet"
    if reading.resembles_nobody:
        return "unknown voice"
    return "unsure"


async def _read(session, who, vector, *, at=NOW, microphone=MIC, recent=(), **measures):
    return await read_voice(
        session,
        person_id=None if who is None else who.id,
        microphone=microphone,
        print_=print_of(vector, **measures),
        recent=[print_of(v) for v in recent],
        thresholds=THRESHOLDS,
        now=at,
    )


async def _hear(session, who, vector, *, at=NOW, microphone=MIC, recent=(), **measures):
    """One utterance at the signed-in page of ``who``: the reading (with the utterances just
    before), then what was learnt from this utterance."""
    print_ = print_of(vector, **measures)
    reading = await _read(
        session, who, vector, at=at, microphone=microphone, recent=recent, **measures
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


async def _prints(session, who, microphone=MIC):
    return {row.kind: row for row in await _rows(session, who) if row.microphone == microphone}


# --- A: one nearest known other ------------------------------------------------------------------


async def test_the_other_is_reported_without_the_discount_of_a_microphone(session):
    alex = await person(session)
    sam = await person(session, "Sam", "adult")
    await learnt_voice(session, alex, ALEX)
    await learnt_voice(session, sam, SAM)
    reading = await _read(session, alex, SAM, microphone=HEADSET)
    assert reading.best_other == sam.id
    assert reading.other_degree == pytest.approx(1.0)
    assert reading.other_raw == pytest.approx(1.0)


async def test_a_passing_print_never_names_its_owner(session):
    """(g) A stranger's voice held in Alex's passing print names Alex nowhere."""
    alex = await person(session)
    dana = await person(session, "Dana", "adult")
    tom = await person(session, "Tom", "adult")
    ref = await learnt_voice(session, alex, ALEX)
    await learnt_voice(session, dana, DANA)
    stranger = with_cosine(ALEX, 0.35, TOM)  # a degree of 0.20 against Alex's base
    session.add(
        VoicePrint(
            ref_id=ref.id,
            microphone=MIC,
            kind="passing",
            model=MODEL,
            vector=list(stranger),
            first_visit_at=NOW,
            last_at=NOW,
            expires_at=NOW + 3 * DAY,
        )
    )
    await session.flush()
    for who in (dana, None):
        reading = await _read(session, who, stranger, at=NOW + HOUR)
        assert reading.best_other is None
        assert reading.other_degree < AGAINST_AT
        assert reading.weighs != "against"
    # At Tom's page, the stranger's voice is learnt as Tom's: nobody known is in the way.
    results = [
        (await _hear(session, tom, stranger, at=NOW + HOUR + delay))[1]
        for delay in (timedelta(0), timedelta(minutes=1), 2 * HOUR)
    ]
    assert results == ["candidate", "candidate", "learnt"]


# --- B: the reading says "someone else we know" ---------------------------------------------------


@pytest.mark.parametrize(
    "alex_voice",
    [
        (0.8, 0.6, 0.0, 0.0),  # Sam's voice reads "for" Alex (0.70), and is Sam's (1.0)
        at_angle(26.7),  # Sam's voice is in Alex's band (0.40) and is Sam's
    ],
)
async def test_a_known_other_nearer_than_the_person_makes_the_reading_against(session, alex_voice):
    alex = await person(session)
    sam = await person(session, "Sam", "adult")
    await learnt_voice(session, alex, alex_voice)
    await learnt_voice(session, sam, SAM)
    reading = await _read(session, alex, SAM)
    assert reading.core_degree >= AGAINST_AT
    assert (reading.weighs, reading.best_other) == ("against", sam.id)
    assert verdict(reading) == "someone else"


async def test_a_tie_between_the_person_and_another_is_unsure_and_does_not_follow(session):
    alex = await person(session)
    sam = await person(session, "Sam", "adult")
    await learnt_voice(session, alex, at_angle(20.0))
    await learnt_voice(session, sam, at_angle(70.0))
    before = _kept(await _rows(session, alex))
    reading, result = await _hear(session, alex, at_angle(45.0))
    assert reading.core_degree == pytest.approx(reading.other_raw) == pytest.approx(1.0)
    # Round 5: a tie reads unsure and names the other (it read "for" in round 4).
    assert (reading.weighs, reading.best_other) == ("unknown", sam.id)
    assert result == "nothing"
    assert _kept(await _rows(session, alex)) == before


async def test_the_person_nearer_than_another_who_reads_for_is_followed(session):
    alex = await person(session)
    sam = await person(session, "Sam", "adult")
    await learnt_voice(session, alex, ALEX)
    await learnt_voice(session, sam, at_angle(50.0))  # Alex's voice is 0.79 for Sam
    reading, result = await _hear(session, alex, ALEX)
    assert FOR_AT <= reading.other_raw < reading.core_degree
    assert (reading.weighs, result) == ("for", "updated")


# --- C: learning decides on the single print ------------------------------------------------------


@pytest.mark.parametrize("guest", [SAM, at_degree(0.4)])
async def test_a_guest_right_after_alex_puts_nothing_of_her_voice_in_his_prints(session, guest):
    """(e) Alex speaks, a guest speaks two seconds later at his signed-in page."""
    alex = await person(session)
    await learnt_voice(session, alex, ALEX)
    _, result = await _hear(session, alex, ALEX)
    assert result == "updated"
    before = _kept(await _rows(session, alex))
    later = NOW + timedelta(seconds=2)
    reading, result = await _hear(session, alex, guest, at=later, recent=[ALEX])
    assert result in ("nothing", "passing")
    assert _kept(await _rows(session, alex)) == before
    assert reading.weighs != "for"  # round 5: the join never lifts a voice to "for"


# --- D: following is anchored to the base ---------------------------------------------------------


async def test_an_unknown_voice_said_thirty_times_never_takes_recent_out_of_the_base(session):
    """(f) A voice at raw 0.62 against Alex's base, thirty times at his signed-in page."""
    alex = await person(session)
    await learnt_voice(session, alex, ALEX)
    voice = at_degree(0.62)
    for n in range(30):
        _, result = await _hear(session, alex, voice, at=NOW + n * HOUR)
        assert result == "updated"
        rows = await _prints(session, alex)
        between = degree_of(similarity(rows["recent"].vector, rows["base"].vector), THRESHOLDS)
        assert between >= FOR_AT
    base = rows["base"].vector
    allowed = (1.0 - (1.0 - BASE_WEIGHT) ** 30) * math.dist(voice, ALEX)
    assert math.dist(base, ALEX) <= allowed + 1e-4


async def test_a_voice_that_reads_for_only_against_recent_teaches_nothing(session):
    alex = await person(session)
    await learnt_voice(session, alex, ALEX)
    rows = await _prints(session, alex)
    rows["recent"].vector = list(at_angle(53.13))  # 0.70 against the base
    await session.flush()
    before = _kept(await _rows(session, alex))
    voice = at_angle(78.97)  # 1.0 against recent, nothing against the base
    for n in range(5):
        reading, result = await _hear(session, alex, voice, at=NOW + n * HOUR)
        assert reading.weighs == "for"  # recent recognises it...
        assert result == "nothing"  # ...but nothing is learnt from it
    assert _kept(await _rows(session, alex)) == before
    assert {row.kind for row in await _rows(session, alex)} == {"base", "recent"}


async def test_voices_stepping_away_never_walk_recent_away_from_the_base(session):
    alex = await person(session)
    await learnt_voice(session, alex, ALEX)
    for step, angle in enumerate((20.0, 40.0, 60.0, 80.0, 100.0, 120.0)):
        for n in range(10):
            await _hear(session, alex, at_angle(angle), at=NOW + (step * 10 + n) * HOUR)
            rows = await _prints(session, alex)
            between = degree_of(similarity(rows["recent"].vector, rows["base"].vector), THRESHOLDS)
            assert between >= FOR_AT
    assert similarity(rows["recent"].vector, ALEX) > 0.5


# --- F: the table of verdicts, one row each ------------------------------------------------------


async def test_table_the_voice_confirms_the_person(session):
    alex = await person(session)
    await learnt_voice(session, alex, ALEX)
    reading = await _read(session, alex, ALEX_NEAR)
    assert (reading.weighs, reading.learnt) == ("for", True)
    assert verdict(reading) == "confirms"


@pytest.mark.parametrize("how", ["band", "short", "passing"])
async def test_table_the_voice_is_unsure(session, how):
    alex = await person(session)
    await learnt_voice(session, alex, ALEX)
    if how == "band":
        reading = await _read(session, alex, at_degree(0.45))
    elif how == "short":
        reading = await _read(session, alex, ALEX, seconds=0.8)
    else:
        cold = at_degree(0.4)
        await _hear(session, alex, cold)  # a passing print
        reading = await _read(session, alex, cold, at=NOW + HOUR)
        assert reading.via_passing
    assert reading.weighs == "unknown" and reading.learnt
    assert not reading.resembles_nobody
    assert verdict(reading) == "unsure"


async def test_table_it_is_someone_else_we_know(session):
    alex = await person(session)
    dana = await person(session, "Dana", "adult")
    await learnt_voice(session, alex, ALEX)
    await learnt_voice(session, dana, DANA)
    reading = await _read(session, alex, DANA)
    assert (reading.weighs, reading.best_other) == ("against", dana.id)
    assert verdict(reading) == "someone else"


async def test_table_an_unknown_voice(session):
    alex = await person(session)
    dana = await person(session, "Dana", "adult")
    await learnt_voice(session, alex, ALEX)
    await learnt_voice(session, dana, DANA)
    reading = await _read(session, alex, SAM)
    assert reading.resembles_nobody
    assert (reading.weighs, reading.learnt, reading.best_other) == ("unknown", True, None)
    assert verdict(reading) == "unknown voice"


async def test_table_not_learnt_yet(session):
    alex = await person(session)
    dana = await person(session, "Dana", "adult")
    await learnt_voice(session, dana, DANA)
    reading = await _read(session, alex, DANA)
    assert (reading.weighs, reading.learnt) == ("unknown", False)
    assert verdict(reading) == "not learnt yet"


# --- the scenarios, re-traced on the final code ---------------------------------------------------


@pytest.mark.parametrize("raw", [0.20, 0.23, 0.26, 0.29])
async def test_scenario_a_a_guest_with_alexs_pitch(session, raw):
    alex = await person(session)
    await learnt_voice(session, alex, ALEX, habits=HABITS)
    before = _kept(await _rows(session, alex))
    for n in range(10):
        reading, result = await _hear(session, alex, at_degree(raw), at=NOW + n * HOUR, **FITTING)
        assert (verdict(reading), result) == ("unknown voice", "nothing")
    rows = await _rows(session, alex)
    assert {row.kind for row in rows} == {"base", "recent"}
    assert _kept(rows) == before


async def test_scenario_b_alexs_cold_with_a_misfit_pitch_then_healthy(session):
    alex = await person(session)
    await learnt_voice(session, alex, ALEX, habits=HABITS)
    before = _kept(await _rows(session, alex))
    cold = (1.0, 2.0, 0.0, 0.0)  # 0.39 against the base
    for day in range(4):
        reading, result = await _hear(session, alex, cold, at=NOW + day * DAY, **MISFIT)
        assert (verdict(reading), result) == ("unsure", "passing")
        other, result = await _hear(
            session, alex, cold, at=NOW + day * DAY + HOUR, microphone=HEADSET, **MISFIT
        )
        assert verdict(other) == "unsure" and result == "nothing"
    assert _kept(await _rows(session, alex)) == before
    assert {row.microphone for row in await _rows(session, alex)} == {MIC}
    healthy, result = await _hear(session, alex, ALEX, at=NOW + 4 * DAY, **FITTING)
    assert (verdict(healthy), healthy.via_passing, result) == ("confirms", False, "updated")


@pytest.mark.parametrize("raw", [0.31, 0.40, 0.50, 0.58])
async def test_scenario_c_the_sibling_on_two_microphones_then_at_her_page(session, raw):
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
            assert verdict(reading) == "unsure"
    assert _kept(await _rows(session, alex)) == before
    assert {row.microphone for row in await _rows(session, alex)} == {MIC}
    results = [
        (await _hear(session, dana, voice, at=NOW + 5 * DAY + delay))[1]
        for delay in (timedelta(0), timedelta(minutes=1), VISIT_GAP + HOUR)
    ]
    assert results == ["candidate", "candidate", "learnt"]
    # Learnt, her voice at Alex's page is now someone else we know: her.
    reading, result = await _hear(session, alex, voice, at=NOW + 6 * DAY)
    assert (verdict(reading), reading.best_other, result) == ("someone else", dana.id, "nothing")


async def test_scenario_d_dana_beside_her_brother_sam(session):
    dana = await person(session, "Dana", "adult")
    sam = await person(session, "Sam", "adult")
    await learnt_voice(session, sam, SAM)
    voice = with_cosine(SAM, 0.425, DANA)  # 0.35 against Sam
    results = [
        (await _hear(session, dana, voice, at=NOW + delay))[1]
        for delay in (timedelta(0), timedelta(minutes=1), 2 * HOUR)
    ]
    assert results == ["candidate", "candidate", "learnt"]
    reading = await _read(session, dana, voice, at=NOW + 3 * HOUR)
    assert verdict(reading) == "confirms"
    # Sam's own voice at Dana's page is someone else we know, and never moves her prints.
    before = _kept(await _rows(session, dana))
    for n in range(5):
        reading, result = await _hear(session, dana, SAM, at=NOW + (4 + n) * HOUR)
        assert (verdict(reading), reading.best_other, result) == ("someone else", sam.id, "nothing")
    assert _kept(await _rows(session, dana)) == before
