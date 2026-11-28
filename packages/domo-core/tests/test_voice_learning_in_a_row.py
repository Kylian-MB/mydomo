"""Learning a voice restarts only after two unlike utterances in a row (the owner, 2026-11-28,
"deux phrases de suite"): one unlike sentence between like ones is not counted and throws nothing
away."""

from datetime import timedelta

from domo_core.channels.voice.models import VoicePrint, VoiceRef
from domo_core.channels.voice.prints import learn, read_voice
from domo_core.channels.voice.thresholds import VISIT_GAP
from sqlalchemy import select
from voice_support import (
    ALEX,
    DANA,
    MIC,
    NOW,
    SAM,
    THRESHOLDS,
    learnt_voice,
    person,
    print_of,
    with_cosine,
)

MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)
# One person in two languages on a poor microphone: the two read under ``same_voice`` (0.50)
# against each other, yet far above another person (cosine 0).
FRENCH = ALEX
ENGLISH = with_cosine(ALEX, 0.45, DANA)


async def _learn(session, who, vector, at):
    return await learn(
        session,
        person_id=who.id,
        microphone=MIC,
        print_=print_of(vector),
        thresholds=THRESHOLDS,
        sure_otherwise=True,
        visit_at=at,
        now=at,
    )


async def _rows(session, who):
    found = await session.scalars(
        select(VoicePrint)
        .join(VoiceRef, VoiceRef.id == VoicePrint.ref_id)
        .where(VoiceRef.person_id == who.id)
    )
    return {row.kind: row for row in found}


async def test_one_unlike_utterance_between_like_ones_is_not_counted_and_keeps_the_candidate(
    session,
):
    alex = await person(session)
    assert await _learn(session, alex, ALEX, NOW) == "candidate"
    assert await _learn(session, alex, ALEX, NOW + MINUTE) == "candidate"
    assert await _learn(session, alex, SAM, NOW + 2 * MINUTE) == "candidate"
    candidate = (await _rows(session, alex))["candidate"]
    assert tuple(candidate.vector) == ALEX and candidate.utterances == 2
    assert await _learn(session, alex, ALEX, NOW + 2 * HOUR) == "learnt"
    base = (await _rows(session, alex))["base"]
    # The unlike sentence never entered the mean nor the count.
    assert tuple(base.vector) == ALEX and base.utterances == 3


async def test_two_unlike_utterances_in_a_row_restart_the_candidate(session):
    alex = await person(session)
    await _learn(session, alex, ALEX, NOW)
    await _learn(session, alex, ALEX, NOW + MINUTE)
    await _learn(session, alex, SAM, NOW + 2 * MINUTE)
    await _learn(session, alex, SAM, NOW + 3 * MINUTE)
    candidate = (await _rows(session, alex))["candidate"]
    assert tuple(candidate.vector) == SAM
    assert candidate.utterances == 1 and candidate.visits == 1
    assert candidate.first_visit_at == NOW + 3 * MINUTE


async def test_a_like_utterance_forgets_the_unlike_one_before_it(session):
    """Unlike, like, unlike: never two in a row, so the candidate is never thrown away."""
    alex = await person(session)
    await _learn(session, alex, ALEX, NOW)
    await _learn(session, alex, SAM, NOW + MINUTE)
    await _learn(session, alex, ALEX, NOW + 2 * MINUTE)
    await _learn(session, alex, SAM, NOW + 3 * MINUTE)
    candidate = (await _rows(session, alex))["candidate"]
    assert tuple(candidate.vector) == ALEX and candidate.utterances == 2


async def test_a_person_who_alternates_two_languages_is_learnt(session):
    """Dana's case: French and English in turn on a poor microphone, each step under
    ``same_voice``: learnt from the sentences of the language that began the candidate."""
    dana = await person(session, "Dana", "adult")
    steps = [
        (FRENCH, NOW),
        (ENGLISH, NOW + MINUTE),
        (FRENCH, NOW + 2 * HOUR),
        (ENGLISH, NOW + 2 * HOUR + MINUTE),
        (FRENCH, NOW + 2 * HOUR + 2 * MINUTE),
    ]
    results = [await _learn(session, dana, vector, at) for vector, at in steps]
    assert results == ["candidate", "candidate", "candidate", "candidate", "learnt"]
    reading = await read_voice(
        session,
        person_id=dana.id,
        microphone=MIC,
        print_=print_of(ENGLISH),
        thresholds=THRESHOLDS,
        now=NOW + 3 * HOUR,
    )
    # Her other language, never taught, reads unsure, not a stranger.
    assert reading.learnt and reading.weighs == "unknown" and not reading.resembles_nobody


async def test_two_alternating_voices_are_never_learnt_as_one(session):
    """Alex and Sam in turn at Alex's page, hours apart: the code is "sure" of the page each time.
    The unlike sentences are never counted nor mixed in: what is learnt is the voice that began
    the candidate, alone, and the other reads far from it."""
    alex = await person(session)
    results = []
    for n in range(6):
        vector = ALEX if n % 2 == 0 else SAM
        results.append(await _learn(session, alex, vector, NOW + n * 2 * HOUR))
    assert results == ["candidate", "candidate", "candidate", "candidate", "learnt", "nothing"]
    rows = await _rows(session, alex)
    assert tuple(rows["base"].vector) == ALEX and rows["base"].utterances == 3
    sam = await read_voice(
        session,
        person_id=alex.id,
        microphone=MIC,
        print_=print_of(SAM),
        thresholds=THRESHOLDS,
        now=NOW + 13 * HOUR,
    )
    assert sam.resembles_nobody


async def test_a_known_voice_in_the_way_is_no_unlike_utterance(session):
    """Dana is known: her sentence at Alex's page teaches nothing and is no miss of the candidate
    (the nearest known other is weighed before it)."""
    alex = await person(session)
    dana = await person(session, "Dana", "adult")
    await learnt_voice(session, dana, DANA)
    await _learn(session, alex, ALEX, NOW)
    assert await _learn(session, alex, DANA, NOW + MINUTE) == "nothing"
    assert await _learn(session, alex, SAM, NOW + 2 * MINUTE) == "candidate"
    candidate = (await _rows(session, alex))["candidate"]
    # Sam's alone is the first unlike one: the candidate is still Alex's.
    assert tuple(candidate.vector) == ALEX and candidate.misses == 1


async def test_the_learning_still_needs_two_visits(session):
    alex = await person(session)
    for n in range(3):
        await _learn(session, alex, ALEX, NOW + n * MINUTE)
    await _learn(session, alex, SAM, NOW + 4 * MINUTE)
    assert await _learn(session, alex, ALEX, NOW + 5 * MINUTE) == "candidate"
    assert await _learn(session, alex, ALEX, NOW + 5 * MINUTE + VISIT_GAP) == "learnt"
