"""Who speaks: a person is established only by a strong clue from the code that is for them."""

import json
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

import pytest
from domo_core.accounts.models import Person
from domo_core.conversation.speaker import Clue, Speaker, actor_of, established, standing

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)


def _clue(
    *,
    kind: str = "session",
    weighs: Literal["for", "against", "unknown"] = "for",
    strength: Literal["strong", "weak"] = "strong",
    source: Literal["code", "domo"] = "code",
    at: datetime = NOW,
    degree: float | None = None,
) -> Clue:
    return Clue(kind=kind, weighs=weighs, strength=strength, source=source, at=at, degree=degree)


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


def test_the_actor_carries_the_speakers_standing():
    person = Person(id=uuid4(), display_name="Dana", role="adult")
    for clues in ((), (_clue(),), (_clue(source="domo"),)):
        speaker = Speaker(person_id=person.id, channel="api", clues=clues)
        actor = actor_of(speaker, person)
        assert actor.standing == standing(speaker)
        assert actor.trusted == (standing(speaker) == "sure")
        assert actor.person_id == person.id
        assert actor.role == "adult"
    # A voice too short to tell travels with the actor: sure, but not for what is sensitive.
    short = actor_of(Speaker(person.id, "voice", clues=(_clue(),), voice_too_short=True), person)
    assert (short.standing, short.voice_too_short, short.trusted) == ("sure", True, False)


def test_nobody_has_no_actor():
    with pytest.raises(ValueError):
        actor_of(Speaker(person_id=None, channel="page"), Person(display_name="Sam", role="guest"))


def test_the_judge_reads_no_identifier():
    speaker = Speaker(
        person_id=uuid4(),
        channel="page",
        device_id=uuid4(),
        clues=(_clue(at=NOW - timedelta(minutes=7, seconds=30)),),
        last_heard_at=NOW - timedelta(hours=2),
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
    assert seen["last_heard_minutes_ago"] == 120
    # Never heard before in this conversation: said so.
    first = Speaker(person_id=uuid4(), channel="page", clues=(_clue(),)).as_json(NOW)
    assert first["last_heard_minutes_ago"] is None


def test_a_turn_nobody_spoke_in_tells_no_clue():
    speaker = Speaker(
        person_id=uuid4(),
        channel="unasked",
        clues=(_clue(at=NOW - timedelta(days=3)),),
        nobody_spoke=True,
        last_heard_at=NOW - timedelta(hours=1),
    )
    assert speaker.as_json(NOW) == {"channel": "unasked", "nobody_spoke": True}
    # Who it is stays as the barrier reads it.
    assert established(speaker)


def test_the_page_of_today_renders_exactly_as_before():
    # Copied from the merged master's test: one strong session clue, no microphone.
    speaker = Speaker(
        person_id=uuid4(),
        channel="page",
        device_id=uuid4(),
        clues=(_clue(at=NOW - timedelta(minutes=7, seconds=30)),),
        last_heard_at=NOW - timedelta(hours=2),
    )
    assert json.dumps(speaker.as_json(NOW)) == (
        '{"channel": "page", "clues": [{"kind": "session", "weighs": "for", "strength": "strong", '
        '"source": "code", "minutes_ago": 7}], "last_heard_minutes_ago": 120}'
    )


def test_a_voice_clue_carries_its_degree():
    voice = _clue(kind="voice", strength="weak", degree=0.82, at=NOW - timedelta(minutes=1))
    seen = Speaker(person_id=uuid4(), channel="voice", clues=(voice, _clue())).as_json(NOW)
    assert seen["clues"] == [
        {
            "kind": "voice",
            "weighs": "for",
            "strength": "weak",
            "source": "code",
            "minutes_ago": 1,
            "degree": 0.82,
        },
        {
            "kind": "session",
            "weighs": "for",
            "strength": "strong",
            "source": "code",
            "minutes_ago": 0,
        },
    ]


def test_unknown_is_a_weight_of_its_own():
    unknown = _clue(kind="voice", weighs="unknown", strength="weak", degree=0.1)
    speaker = Speaker(person_id=uuid4(), channel="voice", clues=(unknown,))
    assert speaker.as_json(NOW)["clues"][0]["weighs"] == "unknown"
    # Neither for nor against: it establishes nobody.
    assert not established(speaker)
    assert not established(
        Speaker(person_id=uuid4(), channel="voice", clues=(_clue(weighs="unknown"),))
    )


def test_a_microphone_is_said_to_exist_never_named():
    microphone = "mic-7f3a"
    speaker = Speaker(person_id=uuid4(), channel="voice", clues=(_clue(),), microphone=microphone)
    seen = speaker.as_json(NOW)
    assert seen["microphone"] is True
    assert microphone not in json.dumps(seen)
    # No microphone, no key.
    assert "microphone" not in Speaker(person_id=uuid4(), channel="page").as_json(NOW)


def test_nobody_spoke_stays_apart_from_unsure():
    # CNV-5: a turn nobody spoke in tells no clue, even with a microphone and an unsure voice...
    quiet = Speaker(
        person_id=uuid4(),
        channel="voice",
        clues=(_clue(kind="voice", weighs="unknown", strength="weak", degree=0.4),),
        nobody_spoke=True,
        microphone="mic-1",
    )
    assert quiet.as_json(NOW) == {"channel": "voice", "nobody_spoke": True}
    # ...and someone unsure is someone who spoke.
    unsure = Speaker(
        person_id=uuid4(),
        channel="voice",
        clues=(_clue(kind="voice", strength="weak", degree=0.55),),
    ).as_json(NOW)
    assert "nobody_spoke" not in unsure
    assert unsure["clues"][0]["degree"] == 0.55


def test_at_another_persons_page_a_kept_proof_needs_this_voice_clearly_theirs():
    # Final fix wave B, I-3: a proof given earlier and kept alive does not make a merely unsure
    # voice sure at someone else's page; this sentence's voice must read clearly for them.
    now = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    person, page = uuid4(), uuid4()

    def heard(degree: float, *, at: UUID | None = page) -> Speaker:
        return Speaker(
            person,
            "voice",
            clues=(
                Clue("voice", "for", "weak", "code", now, degree=degree),
                Clue("session", "against", "weak", "code", now),
                Clue("proof", "for", "strong", "code", now, means="spoken_phrase"),
            ),
            page_person_id=at,
        )

    assert standing(heard(0.62)) == "unsure"
    assert standing(heard(0.8)) == "sure"
    # At their own place (no other page person), the kept proof counts as before.
    assert standing(heard(0.62, at=None)) == "sure"
