"""Who speaks: a person is established only by a strong clue from the code that is for them."""

from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import uuid4

import pytest
from domo_core.conversation.speaker import Clue, Speaker, actor_of, established

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)


def _clue(
    *,
    kind: str = "session",
    weighs: Literal["for", "against"] = "for",
    strength: Literal["strong", "weak"] = "strong",
    source: Literal["code", "domo"] = "code",
    at: datetime = NOW,
) -> Clue:
    return Clue(kind=kind, weighs=weighs, strength=strength, source=source, at=at)


def test_nobody_is_established_without_a_person():
    assert not established(Speaker(person_id=None, channel="page", clues=(_clue(),)))


def test_a_clue_from_domo_alone_does_not_establish():
    speaker = Speaker(person_id=uuid4(), channel="page", clues=(_clue(source="domo"),))
    assert not established(speaker)


def test_a_weak_clue_does_not_establish():
    assert not established(
        Speaker(person_id=uuid4(), channel="page", clues=(_clue(strength="weak"),))
    )


def test_a_strong_clue_against_does_not_establish():
    speaker = Speaker(person_id=uuid4(), channel="page", clues=(_clue(weighs="against"),))
    assert not established(speaker)


def test_a_strong_session_clue_from_the_code_establishes():
    assert established(Speaker(person_id=uuid4(), channel="page", clues=(_clue(),)))


def test_the_actor_is_trusted_as_the_speaker_is_established():
    person = uuid4()
    for clues in ((), (_clue(),), (_clue(source="domo"),)):
        speaker = Speaker(person_id=person, channel="api", clues=clues)
        actor = actor_of(speaker, "adult")
        assert actor.trusted == established(speaker)
        assert actor.person_id == person
        assert actor.role == "adult"


def test_nobody_has_no_actor():
    with pytest.raises(ValueError):
        actor_of(Speaker(person_id=None, channel="page"), "guest")


def test_the_judge_reads_no_identifier():
    speaker = Speaker(
        person_id=uuid4(),
        channel="page",
        device_id=uuid4(),
        clues=(_clue(at=NOW - timedelta(minutes=7, seconds=30)),),
    )
    seen = speaker.as_json(NOW)
    assert "person_id" not in seen
    assert "device_id" not in seen
    assert seen["channel"] == "page"
    assert seen["clues"] == [
        {
            "kind": "session",
            "weighs": "for",
            "strength": "strong",
            "source": "code",
            "minutes_ago": 7,
        }
    ]
