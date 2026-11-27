"""Who probably speaks at a page, and how sure the code is (spec 6.5): three states computed from
the code's own clues."""

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from domo_core.accounts.models import Person
from domo_core.channels.page.speaker import page_speaker
from domo_core.channels.voice.prints import NOT_LEARNED, Reading
from domo_core.channels.voice.speaker import voice_speaker
from domo_core.channels.voice.thresholds import AGAINST_AT, FOR_AT, SHORT_CAP
from domo_core.conversation.speaker import Clue, Speaker, heeded_proofs, standing
from domo_core.llm.speech.client import Print

NOW = datetime(2026, 11, 26, 20, 0, tzinfo=UTC)
SIGNED_IN = NOW - timedelta(hours=3)
MIC = "desk-1"
ALEX = Person(id=uuid.uuid4(), display_name="Alex", role="owner")
PAUL = uuid.uuid4()
PRINT = Print(
    vector=(0.1, 0.2, 0.3), model="print-1", seconds=2.0, pitch_hz=None, pitch_range_hz=None,
    rate=None,
)  # fmt: skip
PROOF = Clue("proof", "for", "strong", "code", NOW - timedelta(minutes=5), means="spoken_phrase")
MANNER = Clue("manner", "for", "weak", "domo", NOW - timedelta(minutes=5))


def _reading(**fields: object) -> Reading:
    learnt = {"weighs": "unknown", "degree": 0.0, "learnt": True, "best_other": None}
    learnt |= {"other_degree": 0.0, "intonation_fits": None}
    return Reading(**(learnt | fields))  # type: ignore[arg-type]


def _speaker(
    reading: Reading, *, print_: Print | None = PRINT, proofs: tuple[Clue, ...] = ()
) -> Speaker:
    return voice_speaker(
        page_person=ALEX,
        reading=reading,
        print_=print_,
        microphone=MIC,
        signed_in_at=SIGNED_IN,
        proofs=proofs,
        now=NOW,
    )


def _voice(speaker: Speaker) -> Clue:
    [voice] = [clue for clue in speaker.clues if clue.kind == "voice"]
    return voice


def _kinds(speaker: Speaker) -> list[str]:
    return [clue.kind for clue in speaker.clues]


def test_alex_whose_voice_fits_is_sure() -> None:
    speaker = _speaker(_reading(weighs="for", degree=0.82, intonation_fits=True))
    assert speaker.person_id == ALEX.id
    assert speaker.channel == "voice"
    assert speaker.microphone == MIC
    session, voice, intonation = speaker.clues
    assert session == Clue("session", "for", "strong", "code", SIGNED_IN)
    assert voice == Clue("voice", "for", "weak", "code", NOW, degree=0.82)
    assert intonation == Clue("intonation", "for", "weak", "code", NOW)
    assert standing(speaker) == "sure"


def test_a_voice_not_learnt_yet_is_sure_by_the_page() -> None:
    speaker = _speaker(NOT_LEARNED)
    assert speaker.person_id == ALEX.id
    assert _voice(speaker).weighs == "unknown"
    assert _voice(speaker).degree == 0.0
    assert standing(speaker) == "sure"


def test_alex_with_a_cold_is_unsure_and_still_alex() -> None:
    # Fits badly, nearer nobody else: a cold held up by a passing print, or a degree between
    # the thresholds.
    for reading in (
        _reading(degree=(AGAINST_AT + FOR_AT) / 2, intonation_fits=False),
        _reading(degree=FOR_AT - 0.01, via_passing=True),
        _reading(degree=0.1, via_passing=True),
    ):
        speaker = _speaker(reading)
        assert speaker.person_id == ALEX.id
        assert _voice(speaker) == Clue(
            "voice", "against", "weak", "code", NOW, degree=reading.degree
        )
        assert "session" in _kinds(speaker)
        assert standing(speaker) == "unsure"
    assert _kinds(_speaker(_reading(degree=0.45, intonation_fits=False)))[-1] == "intonation"


def test_paul_at_alexs_page_is_paul_unsure() -> None:
    reading = _reading(
        weighs="against", degree=0.2, best_other=PAUL, other_degree=0.75, intonation_fits=False
    )
    speaker = _speaker(reading, proofs=(PROOF,))
    assert speaker.person_id == PAUL
    # Not their proof, not their habits: the voice, which is weak, and the page signed in as
    # someone else (review of Task 24), which the code says.
    assert speaker.clues == (
        Clue("voice", "for", "weak", "code", NOW, degree=0.75),
        Clue("session", "against", "weak", "code", SIGNED_IN),
    )
    assert speaker.page_person_id == ALEX.id
    assert speaker.microphone == MIC
    assert standing(speaker) == "unsure"


def test_a_voice_only_somewhat_like_another_is_not_them() -> None:
    # Paul at FOR_AT but no nearer than Alex: a tie, still Alex's page, unsure.
    tie = _speaker(_reading(degree=0.65, best_other=PAUL, other_degree=0.65))
    assert tie.person_id == ALEX.id
    # Paul under FOR_AT: never Paul.
    vague = _speaker(_reading(weighs="against", degree=0.4, best_other=PAUL, other_degree=0.5))
    assert vague.person_id == ALEX.id
    assert standing(vague) == "unsure"


def test_a_stranger_at_alexs_page_is_nobody() -> None:
    speaker = _speaker(_reading(degree=0.1, intonation_fits=False), proofs=(PROOF, MANNER))
    assert speaker.person_id is None
    assert _voice(speaker) == Clue("voice", "against", "weak", "code", NOW, degree=0.1)
    assert "proof" not in _kinds(speaker)
    # The page's session is the page person's, not a stranger's: no session clue for nobody
    # (ruling T23 from the Task 21 review).
    assert "session" not in _kinds(speaker)
    # Nor the assistant's reading of Alex's manner: what is kept is Alex's (review of Task 23).
    assert "manner" not in _kinds(speaker)
    assert standing(speaker) == "unknown"
    # Somewhat like Paul, but not clearly: still nobody known (far from Alex).
    vague = _speaker(_reading(weighs="against", degree=0.1, best_other=PAUL, other_degree=0.5))
    assert vague.person_id is None


def test_a_typed_message_has_no_voice_and_is_sure() -> None:
    typed = page_speaker(ALEX.id, NOW, SIGNED_IN)
    assert standing(typed) == "sure"
    assert not typed.voice_too_short
    # A sentence whose print could not be made: a voice clue that says nothing, sure by the page
    # for what is ordinary, but its voice too short to tell for what is sensitive (ruling T22).
    speaker = _speaker(_reading(degree=0.1), print_=None)
    assert speaker.person_id == ALEX.id
    assert _kinds(speaker) == ["session", "voice"]
    assert _voice(speaker) == Clue("voice", "unknown", "weak", "code", NOW)
    assert standing(speaker) == "sure"
    assert speaker.voice_too_short


def test_a_domo_clue_never_changes_the_standing() -> None:
    manner_against = Clue("manner", "against", "strong", "domo", NOW)
    manner_for = Clue("manner", "for", "strong", "domo", NOW)
    voice_against = Clue("voice", "against", "weak", "domo", NOW, degree=0.1)
    sure = _speaker(_reading(weighs="for", degree=0.9))
    for clue in (manner_against, voice_against):
        assert standing(replace(sure, clues=(*sure.clues, clue))) == "sure"
    unsure = _speaker(_reading(degree=0.45))
    assert standing(replace(unsure, clues=(*unsure.clues, manner_for))) == "unsure"
    # A strong clue for the person that Domo made never establishes them.
    alone = Speaker(ALEX.id, "voice", clues=(manner_for,))
    assert standing(alone) == "unsure"
    assert standing(Speaker(None, "voice", clues=(manner_for,))) == "unknown"


def test_a_proof_does_not_cover_a_voice_that_contradicts_it() -> None:
    fits = _speaker(_reading(weighs="for", degree=0.9), proofs=(PROOF,))
    assert fits.clues[-1] == PROOF
    contradicts = _speaker(_reading(degree=0.45), proofs=(PROOF,))
    assert "proof" not in _kinds(contradicts)
    assert standing(contradicts) == "unsure"


def test_a_short_utterance_never_makes_a_stranger() -> None:
    # A short sound is never against anybody, however far: it cannot be judged.
    for degree in (0.0, 0.1, SHORT_CAP):
        speaker = _speaker(_reading(degree=degree, short=True), proofs=(PROOF,))
        assert speaker.person_id == ALEX.id
        assert _voice(speaker).weighs == "unknown"
        assert PROOF in speaker.clues
        assert standing(speaker) == "sure"
        # Too short to tell, even with a kept proof: unsure for what is sensitive (ruling T22).
        assert speaker.voice_too_short
    # A sentence long enough, or joined to the same voice just before, is not.
    assert not _speaker(_reading(weighs="for", degree=0.9), proofs=(PROOF,)).voice_too_short


def test_the_turn_adds_a_kept_proof_once_and_never_against_the_voice() -> None:
    # What the turn adds of the proofs kept (``prepare._speaking``): not those the channel
    # brought already, none when the voice weighs against the person.
    fits = _speaker(_reading(weighs="for", degree=0.9), proofs=(PROOF,))
    assert heeded_proofs(fits.clues, (PROOF,)) == ()
    unsure = _speaker(_reading(degree=0.45))
    assert heeded_proofs(unsure.clues, (PROOF,)) == ()
    typed = page_speaker(ALEX.id, NOW, SIGNED_IN)
    assert heeded_proofs(typed.clues, (PROOF,)) == (PROOF,)


def test_another_known_voice_at_a_page_not_learnt_yet_is_that_person() -> None:
    # Final fix wave, voice I3: the page person's voice is not learnt, Paul's clearly is.
    speaker = _speaker(_reading(learnt=False, best_other=PAUL, other_degree=0.9))
    assert speaker.person_id == PAUL
    assert speaker.page_person_id == ALEX.id
    assert _voice(speaker) == Clue("voice", "for", "weak", "code", NOW, degree=0.9)
    assert standing(speaker) == "unsure"
