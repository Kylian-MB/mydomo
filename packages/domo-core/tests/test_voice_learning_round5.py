"""Reading and learning a voice, fifth round: a short sound is unsure, a tie is unsure, the base
moves at most once a visit, the joined reading never lifts a voice to "for", a new microphone
enters on the bases only, "against" needs a nearer other, a dead passing print is replaced."""

import math
from datetime import timedelta

import pytest
from domo_core.channels.voice.models import VoicePrint, VoiceRef
from domo_core.channels.voice.prints import learn, read_voice
from domo_core.channels.voice.thresholds import BASE_WEIGHT, PASSING_LIVES
from sqlalchemy import select
from voice_support import (
    ALEX,
    DANA,
    MIC,
    MODEL,
    NOW,
    SAM,
    THRESHOLDS,
    at_degree,
    learnt_voice,
    person,
    print_of,
)

HOUR = timedelta(hours=1)
MINUTE = timedelta(minutes=1)
DAY = timedelta(days=1)
HEADSET = "headset"
VOICE = at_degree(0.8)  # Alex's voice, a little changed: "for" against his base


def at_angle(degrees: float) -> tuple[float, float, float, float]:
    return (math.cos(math.radians(degrees)), math.sin(math.radians(degrees)), 0.0, 0.0)


def _mix(old, new, weight):
    return [(1 - weight) * a + weight * b for a, b in zip(old, new, strict=True)]


async def _read(session, who, vector, *, at=NOW, microphone=MIC, recent=(), seconds=3.0):
    return await read_voice(
        session,
        person_id=who.id,
        microphone=microphone,
        print_=print_of(vector, seconds=seconds),
        recent=recent,
        thresholds=THRESHOLDS,
        now=at,
    )


async def _learn(session, who, vector, *, at=NOW, microphone=MIC, visit_at=None):
    return await learn(
        session,
        person_id=who.id,
        microphone=microphone,
        print_=print_of(vector),
        thresholds=THRESHOLDS,
        sure_otherwise=True,
        visit_at=at if visit_at is None else visit_at,
        now=at,
    )


async def _prints(session, who, microphone=MIC):
    rows = await session.scalars(
        select(VoicePrint)
        .join(VoiceRef, VoiceRef.id == VoicePrint.ref_id)
        .where(VoiceRef.person_id == who.id, VoicePrint.microphone == microphone)
    )
    return {row.kind: row for row in rows}


# --- 1: a short sound is unsure, never an unknown voice ------------------------------------------


async def test_a_far_short_sound_is_unsure_not_an_unknown_voice(session):
    alex = await person(session)
    await learnt_voice(session, alex, ALEX)
    reading = await _read(session, alex, SAM, seconds=0.8)
    assert not reading.resembles_nobody
    assert reading.short
    assert (reading.weighs, reading.learnt) == ("unknown", True)
    long = await _read(session, alex, SAM)
    assert not long.short and long.resembles_nobody


# --- 2: a tie with a known other is unsure and names the other ---------------------------------


async def test_a_tie_with_a_known_other_is_unsure_and_names_the_other(session):
    alex = await person(session)
    sam = await person(session, "Sam", "adult")
    await learnt_voice(session, alex, at_angle(20.0))
    await learnt_voice(session, sam, at_angle(70.0))
    reading = await _read(session, alex, at_angle(45.0))
    assert reading.core_degree == pytest.approx(reading.other_raw) == pytest.approx(1.0)
    assert (reading.weighs, reading.best_other) == ("unknown", sam.id)
    assert not reading.resembles_nobody


# --- 3: the base moves at most once a visit -----------------------------------------------------


async def test_thirty_sure_utterances_in_one_evening_move_the_base_one_step(session):
    alex = await person(session)
    await learnt_voice(session, alex, ALEX)
    for n in range(30):
        assert await _learn(session, alex, VOICE, at=NOW + n * 2 * MINUTE) == "updated"
    rows = await _prints(session, alex)
    assert rows["base"].vector == pytest.approx(_mix(ALEX, VOICE, BASE_WEIGHT), abs=1e-5)
    assert rows["recent"].vector != pytest.approx(_mix(ALEX, VOICE, 0.2), abs=1e-3)


async def test_two_evenings_a_day_apart_move_the_base_twice_whatever_the_visit_time(session):
    alex = await person(session)
    await learnt_voice(session, alex, ALEX)
    for evening in range(2):
        for n in range(10):
            at = NOW + evening * DAY + n * 5 * MINUTE
            # The caller's visit time is not what counts: the server's time is.
            assert await _learn(session, alex, VOICE, at=at, visit_at=NOW) == "updated"
    once = _mix(ALEX, VOICE, BASE_WEIGHT)
    rows = await _prints(session, alex)
    assert rows["base"].vector == pytest.approx(_mix(once, VOICE, BASE_WEIGHT), abs=1e-5)


async def test_a_six_hour_stay_with_an_utterance_every_ten_minutes_is_one_visit(session):
    alex = await person(session)
    await learnt_voice(session, alex, ALEX)
    for n in range(37):
        await _learn(session, alex, VOICE, at=NOW + n * 10 * MINUTE)
    rows = await _prints(session, alex)
    assert rows["base"].vector == pytest.approx(_mix(ALEX, VOICE, BASE_WEIGHT), abs=1e-5)


async def test_a_visit_is_measured_from_the_last_utterance_heard_even_one_teaching_nothing(
    session,
):
    alex = await person(session)
    await learnt_voice(session, alex, ALEX)
    assert await _learn(session, alex, VOICE, at=NOW) == "updated"
    assert await _learn(session, alex, SAM, at=NOW + 50 * MINUTE) == "nothing"
    assert await _learn(session, alex, VOICE, at=NOW + 100 * MINUTE) == "updated"
    rows = await _prints(session, alex)
    assert rows["base"].vector == pytest.approx(_mix(ALEX, VOICE, BASE_WEIGHT), abs=1e-5)


# --- 4: the joined reading never lifts a voice to "for" -------------------------------------------


async def test_a_guest_two_seconds_after_alex_is_not_reported_for_alex(session):
    alex = await person(session)
    await learnt_voice(session, alex, ALEX)
    before = [print_of(ALEX)]
    for guest, seconds in ((SAM, 3.0), (SAM, 1.0), (at_degree(0.4), 3.0), (at_degree(0.4), 1.0)):
        reading = await _read(session, alex, guest, seconds=seconds, recent=before)
        assert reading.weighs != "for"


async def test_short_utterances_of_one_voice_add_up(session):
    alex = await person(session)
    await learnt_voice(session, alex, ALEX)
    first = print_of((0.98, 0.1, 0.05, 0.0), seconds=1.0)
    alone = await _read(session, alex, ALEX, seconds=1.0)
    together = await _read(session, alex, ALEX, seconds=1.0, recent=[first])
    assert alone.weighs == "unknown" and alone.short
    assert together.weighs == "for" and not together.short


# --- 5: a new microphone enters on the bases of the others only -----------------------------------


async def test_a_new_microphone_does_not_enter_on_a_recent_print(session):
    alex = await person(session)
    await learnt_voice(session, alex, ALEX)
    rows = await _prints(session, alex)
    rows["recent"].vector = list(at_angle(53.13))
    await session.flush()
    for n in range(4):
        assert (
            await _learn(session, alex, at_angle(78.97), at=NOW + n * 2 * HOUR, microphone=HEADSET)
            == "nothing"
        )
    assert await _prints(session, alex, HEADSET) == {}


async def test_promotion_on_a_new_microphone_rechecks_the_mean_against_the_bases(session):
    alex = await person(session)
    ref = await learnt_voice(session, alex, ALEX)
    session.add(
        VoicePrint(
            ref_id=ref.id,
            microphone=HEADSET,
            kind="candidate",
            model=MODEL,
            vector=list(at_angle(75.0)),
            utterances=2,
            visits=2,
            first_visit_at=NOW - DAY,
            last_at=NOW - 2 * HOUR,
        )
    )
    await session.flush()
    # Alone, the voice reads "for" against the desk's base; the candidate's mean does not.
    assert await _learn(session, alex, at_angle(30.0), microphone=HEADSET) == "nothing"
    assert "base" not in await _prints(session, alex, HEADSET)


# --- 6: "against" needs a nearer other; a dead passing print is replaced -------------------------


async def test_against_needs_the_named_other_to_be_nearer_than_the_person(session):
    alex = await person(session)
    sam = await person(session, "Sam", "adult")
    await learnt_voice(session, alex, ALEX)
    await learnt_voice(session, sam, SAM)
    rest = math.sqrt(1 - 0.425**2 - 0.41**2)
    heard = (0.425, 0.41, rest, 0.0)  # 0.35 for Alex (0.28 on a new microphone), 0.32 for Sam
    reading = await _read(session, alex, heard, microphone=HEADSET)
    assert reading.degree < 0.3 and reading.best_other == sam.id
    assert reading.weighs == "unknown"
    assert not reading.resembles_nobody


async def test_an_expired_passing_print_not_yet_swept_is_replaced_not_mixed(session):
    alex = await person(session)
    ref = await learnt_voice(session, alex, ALEX)
    session.add(
        VoicePrint(
            ref_id=ref.id,
            microphone=MIC,
            kind="passing",
            model=MODEL,
            vector=list(DANA),
            utterances=7,
            first_visit_at=NOW - 5 * DAY,
            last_at=NOW - 4 * DAY,
            expires_at=NOW - 4 * DAY + PASSING_LIVES,
        )
    )
    await session.flush()
    cold = (1.0, 2.0, 0.0, 0.0)
    assert await _learn(session, alex, cold) == "passing"
    passing = (await _prints(session, alex))["passing"]
    assert passing.vector == pytest.approx(list(cold), abs=1e-6)
    assert passing.utterances == 1
    assert passing.first_visit_at == NOW
    assert passing.expires_at == NOW + PASSING_LIVES
