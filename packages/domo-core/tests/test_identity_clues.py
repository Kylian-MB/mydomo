"""The clues a proof leaves behind: kept for 15 minutes, for the same channel and device, and kept
alive while the person goes on speaking."""

import uuid
from datetime import UTC, datetime, timedelta

from domo_core.conversation.speaker import Clue
from domo_core.identity.clues import (
    PROOF_CLUE_FOR,
    keep_proof_clue,
    kept_clues,
    still_speaking,
)

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)


def at(minutes: float = 0, seconds: float = 0) -> datetime:
    return NOW + timedelta(minutes=minutes, seconds=seconds)


async def test_a_proof_is_kept_for_fifteen_minutes(session, add_person):
    alex = await add_person("Alex", "adult")
    await keep_proof_clue(
        session, person_id=alex.id, channel="page", device_id=None, means="password", now=NOW
    )
    assert timedelta(minutes=15) == PROOF_CLUE_FOR

    async def kept(now: datetime) -> tuple[Clue, ...]:
        return await kept_clues(session, person_id=alex.id, channel="page", device_id=None, now=now)

    assert await kept(at(15)) == (Clue("proof", "for", "strong", "code", NOW, means="password"),)
    assert await kept(at(15, 1)) == ()


async def test_speaking_keeps_a_proof_alive(session, add_person):
    alex = await add_person("Alex", "adult")
    await keep_proof_clue(
        session, person_id=alex.id, channel="page", device_id=None, means="passkey", now=NOW
    )
    await still_speaking(session, person_id=alex.id, channel="page", now=at(10))
    kept = await kept_clues(session, person_id=alex.id, channel="page", device_id=None, now=at(25))
    assert [clue.means for clue in kept] == ["passkey"]
    assert (
        await kept_clues(session, person_id=alex.id, channel="page", device_id=None, now=at(25, 1))
        == ()
    )


async def test_an_expired_proof_is_not_brought_back_by_speaking(session, add_person):
    alex = await add_person("Alex", "adult")
    await keep_proof_clue(
        session, person_id=alex.id, channel="page", device_id=None, means="password", now=NOW
    )
    await still_speaking(session, person_id=alex.id, channel="page", now=at(16))
    assert (
        await kept_clues(session, person_id=alex.id, channel="page", device_id=None, now=at(16))
        == ()
    )


async def test_a_proof_is_for_the_channel_the_device_and_the_person_it_was_given_on(
    session, add_person
):
    alex = await add_person("Alex", "adult")
    dana = await add_person("Dana", "adult")
    phone = uuid.uuid4()
    await keep_proof_clue(
        session, person_id=alex.id, channel="page", device_id=None, means="password", now=NOW
    )
    await keep_proof_clue(
        session, person_id=alex.id, channel="voice", device_id=phone, means="password", now=NOW
    )

    async def kept(person_id, channel, device_id):
        return await kept_clues(
            session, person_id=person_id, channel=channel, device_id=device_id, now=at(1)
        )

    assert len(await kept(alex.id, "page", None)) == 1
    assert len(await kept(alex.id, "voice", phone)) == 1
    assert await kept(alex.id, "api", None) == ()
    assert await kept(alex.id, "voice", None) == ()
    assert await kept(alex.id, "voice", uuid.uuid4()) == ()
    assert await kept(dana.id, "page", None) == ()
    # Speaking on another channel keeps nothing alive here.
    await still_speaking(session, person_id=alex.id, channel="api", now=at(14))
    assert (
        await kept_clues(session, person_id=alex.id, channel="page", device_id=None, now=at(16))
        == ()
    )
