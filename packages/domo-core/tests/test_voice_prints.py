"""Comparing a heard voice to the voice prints kept by reference and by microphone (spec 6.5)."""

from datetime import timedelta

import pytest
from domo_core.channels.voice.models import VoicePrint
from domo_core.channels.voice.prints import degree_of, read_voice, similarity
from domo_core.channels.voice.thresholds import (
    AGAINST_AT,
    FOR_AT,
    OTHER_MICROPHONE,
    SHORT_CAP,
)
from sqlalchemy import text
from voice_support import (
    ALEX,
    ALEX_NEAR,
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


async def test_the_same_voice_weighs_for_with_a_high_degree(session):
    alex = await person(session)
    await learnt_voice(session, alex, ALEX)
    reading = await read_voice(
        session,
        person_id=alex.id,
        microphone=MIC,
        print_=print_of(ALEX_NEAR),
        thresholds=THRESHOLDS,
        now=NOW,
    )
    assert reading.weighs == "for"
    assert reading.learnt
    assert reading.degree >= 0.9
    assert reading.best_other is None


async def test_another_known_voice_names_that_person(session):
    alex = await person(session)
    dana = await person(session, "Dana", "adult")
    await learnt_voice(session, alex, ALEX)
    await learnt_voice(session, dana, DANA)
    reading = await read_voice(
        session,
        person_id=alex.id,
        microphone=MIC,
        print_=print_of(DANA),
        thresholds=THRESHOLDS,
        now=NOW,
    )
    assert reading.weighs == "against"
    assert reading.degree < AGAINST_AT
    assert reading.best_other == dana.id
    assert reading.other_degree >= 0.9


async def test_a_voice_far_from_everyone_is_unknown_and_names_nobody(session):
    alex = await person(session)
    dana = await person(session, "Dana", "adult")
    await learnt_voice(session, alex, ALEX)
    await learnt_voice(session, dana, DANA)
    reading = await read_voice(
        session,
        person_id=alex.id,
        microphone=MIC,
        print_=print_of(SAM),
        thresholds=THRESHOLDS,
        now=NOW,
    )
    assert reading.weighs == "unknown"
    assert reading.degree < AGAINST_AT
    assert reading.learnt
    assert reading.best_other is None
    assert reading.other_degree < AGAINST_AT


async def test_the_middle_degrees_are_unsure_not_against(session):
    alex = await person(session)
    await learnt_voice(session, alex, ALEX)
    # cosine 0.447: a degree between 0.30 and 0.60.
    reading = await read_voice(
        session,
        person_id=alex.id,
        microphone=MIC,
        print_=print_of((1.0, 2.0, 0.0, 0.0)),
        thresholds=THRESHOLDS,
        now=NOW,
    )
    assert AGAINST_AT <= reading.degree < FOR_AT
    assert reading.weighs == "unknown"


async def test_nothing_learnt_is_unknown_not_against(session):
    alex = await person(session)
    reading = await read_voice(
        session,
        person_id=alex.id,
        microphone=MIC,
        print_=print_of(SAM),
        thresholds=THRESHOLDS,
        now=NOW,
    )
    assert (reading.weighs, reading.learnt, reading.degree) == ("unknown", False, 0.0)


async def test_without_thresholds_the_voice_is_not_learned_yet(session):
    alex = await person(session)
    await learnt_voice(session, alex, ALEX)
    reading = await read_voice(
        session,
        person_id=alex.id,
        microphone=MIC,
        print_=print_of(SAM),
        thresholds=None,
        now=NOW,
    )
    assert (reading.weighs, reading.learnt) == ("unknown", False)


async def test_a_short_utterance_is_never_against(session):
    alex = await person(session)
    await learnt_voice(session, alex, ALEX)
    for vector in (SAM, ALEX):
        reading = await read_voice(
            session,
            person_id=alex.id,
            microphone=MIC,
            print_=print_of(vector, seconds=0.8),
            thresholds=THRESHOLDS,
            now=NOW,
        )
        assert reading.weighs == "unknown"
        assert reading.degree <= SHORT_CAP


async def test_utterances_that_follow_add_up(session):
    alex = await person(session)
    await learnt_voice(session, alex, ALEX)
    # Round 5: only short prints of the same voice add up (two long prints that differ, which
    # added up to "for" before, are each read alone).
    a, b = print_of(ALEX_NEAR, seconds=1.0), print_of((0.95, 0.3, 0.0, 0.0), seconds=1.0)
    alone = await read_voice(
        session, person_id=alex.id, microphone=MIC, print_=b, thresholds=THRESHOLDS, now=NOW
    )
    together = await read_voice(
        session,
        person_id=alex.id,
        microphone=MIC,
        print_=b,
        recent=[a],
        thresholds=THRESHOLDS,
        now=NOW,
    )
    assert alone.weighs == "unknown"
    assert together.weighs == "for"
    assert together.degree > alone.degree


async def test_a_new_microphone_reads_the_others_with_less_weight(session):
    alex = await person(session)
    await learnt_voice(session, alex, ALEX)
    same = await read_voice(
        session,
        person_id=alex.id,
        microphone=MIC,
        print_=print_of(ALEX),
        thresholds=THRESHOLDS,
        now=NOW,
    )
    other = await read_voice(
        session,
        person_id=alex.id,
        microphone="headset",
        print_=print_of(ALEX),
        thresholds=THRESHOLDS,
        now=NOW,
    )
    assert same.degree == pytest.approx(1.0)
    assert other.degree == pytest.approx(OTHER_MICROPHONE)
    assert other.learnt


async def test_a_print_of_another_model_is_not_compared(session):
    alex = await person(session)
    await learnt_voice(session, alex, ALEX)
    reading = await read_voice(
        session,
        person_id=alex.id,
        microphone=MIC,
        print_=print_of(ALEX, model="print-b"),
        thresholds=THRESHOLDS,
        now=NOW,
    )
    assert (reading.weighs, reading.learnt, reading.degree) == ("unknown", False, 0.0)


async def test_an_expired_passing_print_is_not_compared(session):
    alex = await person(session)
    ref = await learnt_voice(session, alex, ALEX)
    session.add(
        VoicePrint(
            ref_id=ref.id,
            microphone=MIC,
            kind="passing",
            model="print-a",
            vector=list(SAM),
            first_visit_at=NOW,
            last_at=NOW,
            expires_at=NOW + timedelta(days=1),
        )
    )
    await session.flush()
    live = await read_voice(
        session,
        person_id=alex.id,
        microphone=MIC,
        print_=print_of(SAM),
        thresholds=THRESHOLDS,
        now=NOW,
    )
    gone = await read_voice(
        session,
        person_id=alex.id,
        microphone=MIC,
        print_=print_of(SAM),
        thresholds=THRESHOLDS,
        now=NOW + timedelta(days=2),
    )
    assert (live.weighs, live.via_passing) == ("unknown", True)
    assert live.degree >= AGAINST_AT
    assert (gone.weighs, gone.via_passing, gone.degree) == ("unknown", False, 0.0)


async def test_intonation_is_read_against_the_habits(session):
    alex = await person(session)
    await learnt_voice(session, alex, ALEX, habits=(120.0, 10.0, 4.0, 0.5))
    dana = await person(session, "Dana", "adult")
    await learnt_voice(session, dana, DANA)

    async def fits(who, **measures):
        reading = await read_voice(
            session,
            person_id=who.id,
            microphone=MIC,
            print_=print_of(ALEX, **measures),
            thresholds=THRESHOLDS,
            now=NOW,
        )
        return reading.intonation_fits

    assert await fits(alex, pitch_hz=125.0, rate=4.2) is True
    assert await fits(alex, pitch_hz=200.0, rate=4.2) is False
    assert await fits(alex, pitch_hz=None, rate=None) is None
    assert await fits(dana, pitch_hz=125.0, rate=4.2) is None


def test_vectors_of_unequal_length_or_all_zero_are_zero():
    assert similarity((1.0, 0.0), (1.0, 0.0, 0.0)) == 0.0
    assert similarity((0.0, 0.0), (1.0, 0.0)) == 0.0
    assert similarity((), ()) == 0.0
    assert similarity((1.0, 1.0), (2.0, 2.0)) == pytest.approx(1.0)
    assert similarity((1.0, 0.0), (-1.0, 0.0)) == pytest.approx(-1.0)


def test_the_degree_is_linear_between_the_bounds_and_clamped():
    assert degree_of(0.25, THRESHOLDS) == 0.0
    assert degree_of(0.0, THRESHOLDS) == 0.0
    assert degree_of(0.5, THRESHOLDS) == pytest.approx(0.5)
    assert degree_of(0.75, THRESHOLDS) == 1.0
    assert degree_of(0.99, THRESHOLDS) == 1.0


async def test_no_sound_column_exists(session):
    rows = await session.execute(
        text(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name LIKE 'voice\\_%' "
            "AND data_type = 'bytea'"
        )
    )
    assert rows.all() == []


async def _degree(session, who, vector, **measures):
    return await read_voice(
        session,
        person_id=who.id,
        microphone=MIC,
        print_=print_of(vector, **measures),
        thresholds=THRESHOLDS,
        now=NOW,
    )


async def test_intonation_nuances_the_degree_by_at_most_a_named_step_inside_its_band(session):
    from domo_core.channels.voice import thresholds as bounds

    alex = await person(session)
    await learnt_voice(session, alex, ALEX, habits=(120.0, 10.0, 4.0, 0.5))
    plain = await _degree(session, alex, at_degree(0.45))
    fitting = await _degree(session, alex, at_degree(0.45), pitch_hz=121.0, rate=4.0)
    off = await _degree(session, alex, at_degree(0.45), pitch_hz=200.0, rate=4.0)
    assert plain.degree == pytest.approx(0.45, abs=1e-3)
    assert fitting.degree == pytest.approx(0.45 + bounds.INTONATION_ADJUST, abs=1e-3)
    assert (fitting.weighs, fitting.intonation_fits) == ("unknown", True)
    assert off.degree == pytest.approx(0.45 - bounds.INTONATION_ADJUST, abs=1e-3)
    assert (off.weighs, off.intonation_fits) == ("unknown", False)
    top = await _degree(session, alex, at_degree(0.97), pitch_hz=120.0, rate=4.0)
    assert top.degree == 1.0


async def test_intonation_alone_never_lifts_a_far_voice_to_for(session, monkeypatch):
    from domo_core.channels.voice import prints as voice_prints

    alex = await person(session)
    await learnt_voice(session, alex, ALEX, habits=(120.0, 10.0, 4.0, 0.5))
    monkeypatch.setattr(voice_prints, "INTONATION_ADJUST", 0.9)
    reading = await _degree(session, alex, at_degree(0.2), pitch_hz=120.0, rate=4.0)
    assert reading.degree < FOR_AT


async def test_a_short_sound_gets_no_intonation_step(session):
    alex = await person(session)
    await learnt_voice(session, alex, ALEX, habits=(120.0, 10.0, 4.0, 0.5))
    reading = await _degree(session, alex, at_degree(0.3), seconds=1.0, pitch_hz=120.0, rate=4.0)
    assert reading.degree == pytest.approx(0.3, abs=1e-3)


@pytest.mark.parametrize(
    "vector",
    [
        (float("nan"), 0.0, 0.0, 0.0),
        (float("inf"), 0.0, 0.0, 0.0),
        (0.0, 0.0, 0.0, 0.0),
        (),
    ],
)
async def test_a_vector_that_is_no_print_is_not_learned_yet(session, vector):
    alex = await person(session)
    await learnt_voice(session, alex, ALEX)
    reading = await _degree(session, alex, vector)
    assert (reading.weighs, reading.learnt, reading.degree) == ("unknown", False, 0.0)
    assert similarity(vector, ALEX) == 0.0


def test_similarity_is_never_not_a_number():
    assert similarity((float("nan"), 1.0), (1.0, 1.0)) == 0.0
    assert similarity((float("inf"), 1.0), (1.0, 1.0)) == 0.0
    assert similarity((1e200, 1e200), (1e200, 1e200)) == pytest.approx(1.0)


@pytest.mark.parametrize("bounds", [(0.5, 0.5, 0.5), (0.8, 0.6, 0.2), (float("nan"), 0.5, 0.75)])
async def test_malformed_bounds_are_like_no_bounds(session, bounds):
    from domo_core.llm.speech.client import PrintThresholds

    alex = await person(session)
    await learnt_voice(session, alex, ALEX)
    reading = await read_voice(
        session,
        person_id=alex.id,
        microphone=MIC,
        print_=print_of(SAM),
        thresholds=PrintThresholds(*bounds),
        now=NOW,
    )
    assert (reading.weighs, reading.learnt) == ("unknown", False)


async def test_a_short_utterance_names_the_nearest_other_not_the_last_seen(session):
    alex = await person(session)
    dana = await person(session, "Dana", "adult")
    tom = await person(session, "Tom", "adult")
    await learnt_voice(session, alex, ALEX)
    await learnt_voice(session, dana, with_cosine(SAM, 0.5, DANA))
    await learnt_voice(session, tom, with_cosine(SAM, 0.475, TOM))
    reading = await _degree(session, alex, (0.0, 1.0, 0.0, 0.0), seconds=1.0)
    # Dana is nearer (0.5 against 0.45): the cap is applied once, after the comparison.
    assert reading.best_other == dana.id
    assert reading.other_degree == pytest.approx(SHORT_CAP)


async def test_a_short_voice_short_of_for_is_not_lifted_by_the_person_just_before(session):
    # Ruling T14 (parked from Task 9): a 1 s voice at degree 0.55 against the person's 3 s
    # utterance just before is not joined with it: on its own it is short, so unsure.
    alex = await person(session)
    await learnt_voice(session, alex, ALEX)
    guest = at_degree(0.55)
    assert degree_of(similarity(guest, ALEX), THRESHOLDS) == pytest.approx(0.55)
    reading = await read_voice(
        session,
        person_id=alex.id,
        microphone=MIC,
        print_=print_of(guest, seconds=1.0),
        thresholds=THRESHOLDS,
        recent=[print_of(ALEX, seconds=3.0)],
        now=NOW,
    )
    assert reading.weighs != "for"
    assert reading.short
