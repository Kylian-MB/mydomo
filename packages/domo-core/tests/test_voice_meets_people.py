"""Where the voice meets the people of the house (the merge of "voice on a computer"): what the
voice learnt of someone follows their record, and who speaks is judged with the marks of the
records involved."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from domo_core.accounts.models import Person
from domo_core.channels.voice.models import VoiceMoment, VoicePrint, VoiceRef
from domo_core.people import forget, merge, records
from sqlalchemy import func, select

NOW = datetime(2026, 11, 28, 12, 0, tzinfo=UTC)
MERGE_WHY = "Alex said Sam and Tom are one person"


def a_record(name: str, **columns: Any) -> Person:
    """Someone who lives here and has an access, with no account."""
    values: dict[str, Any] = {
        "role": "adult",
        "state": "has_access",
        "standing": "lives_here",
        "first_access_at": NOW - timedelta(days=3),
    } | columns
    return Person(display_name=name, **values)


async def _voice_of(session: Any, person: Person, reference: str) -> VoiceRef:
    """A voice learnt for ``person``: its reference, a print, and a moment reserved for them."""
    ref = VoiceRef(reference=reference, person_id=person.id)
    session.add(ref)
    await session.flush()
    session.add(
        VoicePrint(ref_id=ref.id, microphone="mic", kind="base", model="m", vector=[0.1, 0.2])
    )
    session.add(
        VoiceMoment(
            person_id=person.id,
            microphone="mic",
            purpose="phrase_agree",
            expires_at=NOW + timedelta(minutes=5),
        )
    )
    await session.flush()
    return ref


async def _count(session: Any, model: Any) -> int:
    return await session.scalar(select(func.count()).select_from(model)) or 0


async def test_a_forgotten_person_leaves_no_voice_behind(session, add_person):
    await add_person("Alex", "owner")
    dana = a_record("Dana")
    session.add(dana)
    await session.flush()
    await _voice_of(session, dana, "v-dana")
    await forget.forget(session, dana, by="domo", turn_of=None, now=NOW)
    assert await _count(session, VoiceRef) == 0
    assert await _count(session, VoicePrint) == 0
    assert await _count(session, VoiceMoment) == 0


async def test_a_record_merged_into_a_forgotten_one_leaves_no_voice_either(session, add_person):
    await add_person("Alex", "owner")
    tom, sam = a_record("Tom"), a_record("Sam", state="known", standing="visiting", role="guest")
    session.add_all([tom, sam])
    await session.flush()
    await _voice_of(session, sam, "v-sam")
    await merge.merge(
        session, kept=tom, absorbed=sam, by="domo", reason=MERGE_WHY, turn_of=None, now=NOW
    )
    # The voice follows the record kept...
    moved = await session.scalar(select(VoiceRef.person_id).where(VoiceRef.reference == "v-sam"))
    assert moved == tom.id
    # ...and goes with it.
    await forget.forget(session, tom, by="domo", turn_of=None, now=NOW)
    assert await _count(session, VoiceRef) == 0
    assert await _count(session, VoicePrint) == 0


async def test_the_owner_keeps_his_voice_since_he_is_never_forgotten(session, add_person):
    alex = await add_person("Alex", "owner")
    await _voice_of(session, alex, "v-alex")
    with pytest.raises(records.NotPossible):
        await forget.forget(session, alex, by="domo", turn_of=None, now=NOW)
    assert await _count(session, VoicePrint) == 1
