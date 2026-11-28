"""Voice prints follow the record of the person they are of (Task 27).

"Domo crée les personnes" is not merged: ``channels.voice.people`` stands in for its hooks
(``claim_reference``, ``refs_of``, ``person_of_ref``) over ``voice_refs`` itself, and applies its
notices (``on_record``: merged, split, erased, forgotten) to the prints. A missed notice is mended
by ``reconcile``, here given the record's view as that branch would give it."""

import uuid
from datetime import timedelta

import pytest
from domo_core.channels.voice import people
from domo_core.channels.voice.models import VoicePrint, VoiceRef
from domo_core.channels.voice.people import (
    NoSuchRecord,
    RecordEvent,
    ReferenceTaken,
    claim_reference,
    on_record,
    person_of_ref,
    reconcile,
    refs_of,
)
from domo_core.channels.voice.prints import learn, reference_for
from domo_core.channels.voice.thresholds import UNKNOWN_LIVES
from sqlalchemy import func, select
from voice_support import ALEX, DANA, MIC, NOW, SAM, THRESHOLDS, learnt_voice, person, print_of


async def prints_under(session, reference: str) -> int:
    found = await session.scalar(
        select(func.count())
        .select_from(VoicePrint)
        .join(VoiceRef, VoiceRef.id == VoicePrint.ref_id)
        .where(VoiceRef.reference == reference)
    )
    return int(found or 0)


async def test_a_learnt_voice_claims_its_reference(session):
    alex = await person(session)
    learning = await learn(
        session,
        person_id=alex.id,
        microphone=MIC,
        print_=print_of(ALEX),
        thresholds=THRESHOLDS,
        sure_otherwise=True,
        visit_at=NOW,
        now=NOW,
    )
    assert learning == "candidate"
    [reference] = await session.scalars(
        select(VoiceRef.reference).where(VoiceRef.person_id == alex.id)
    )
    assert reference in await refs_of(session, "voice")
    assert await person_of_ref(session, channel="voice", reference=reference) == alex.id
    # Another channel holds nothing here; a reference nobody claimed is nobody's.
    assert await refs_of(session, "page") == set()
    assert await person_of_ref(session, channel="voice", reference="v-0000000000000000") is None


async def test_a_claim_is_kept_once_and_never_taken_from_another(session):
    alex = await person(session)
    dana = await person(session, "Dana", "adult")
    ref = await learnt_voice(session, alex, ALEX)
    await claim_reference(
        session, channel="voice", reference=ref.reference, person_id=alex.id, now=NOW
    )
    with pytest.raises(ReferenceTaken):
        await claim_reference(
            session, channel="voice", reference=ref.reference, person_id=dana.id, now=NOW
        )
    with pytest.raises(NoSuchRecord):
        await claim_reference(
            session,
            channel="voice",
            reference="v-1111111111111111",
            person_id=uuid.uuid4(),
            now=NOW,
        )
    assert await person_of_ref(session, channel="voice", reference=ref.reference) == alex.id
    assert await prints_under(session, ref.reference) == 2


async def test_a_merge_moves_the_prints_to_the_kept_record(session):
    alex = await person(session)
    kept = await person(session, "Alex Martin", "adult")
    ref = await learnt_voice(session, alex, ALEX)
    event = RecordEvent("merged", alex.id, kept.id, (("voice", ref.reference),), False, NOW)
    await on_record(session, event)
    assert await person_of_ref(session, channel="voice", reference=ref.reference) == kept.id
    assert await prints_under(session, ref.reference) == 2


async def test_a_split_gives_them_back(session):
    alex = await person(session)
    kept = await person(session, "Alex Martin", "adult")
    ref = await learnt_voice(session, alex, ALEX)
    refs = (("voice", ref.reference),)
    await on_record(session, RecordEvent("merged", alex.id, kept.id, refs, False, NOW))
    await on_record(session, RecordEvent("split", alex.id, kept.id, refs, False, NOW))
    assert await person_of_ref(session, channel="voice", reference=ref.reference) == alex.id
    assert await prints_under(session, ref.reference) == 2


async def test_another_channel_s_references_are_left_alone(session):
    alex = await person(session)
    kept = await person(session, "Alex Martin", "adult")
    ref = await learnt_voice(session, alex, ALEX)
    event = RecordEvent("merged", alex.id, kept.id, (("page", ref.reference),), False, NOW)
    await on_record(session, event)
    assert await person_of_ref(session, channel="voice", reference=ref.reference) == alex.id


async def test_forget_me_erases_the_prints_even_for_someone_refused(session):
    alex = await person(session)
    dana = await person(session, "Dana", "adult")
    gone = await learnt_voice(session, dana, DANA)
    stays = await learnt_voice(session, alex, ALEX)
    # The owner's decision 12 ("description only"): kept minimum or not, the voice goes.
    event = RecordEvent("forgotten", dana.id, None, (("voice", gone.reference),), True, NOW)
    await on_record(session, event)
    assert await refs_of(session, "voice") == {stays.reference}
    assert await prints_under(session, gone.reference) == 0
    assert await prints_under(session, stays.reference) == 2


async def test_forget_me_erases_an_unknown_voice_named_by_its_reference(session):
    stranger = await reference_for(
        session, person_id=None, print_=print_of(SAM), microphone=MIC, now=NOW
    )
    assert await prints_under(session, stranger) == 1
    someone = uuid.uuid4()
    event = RecordEvent("forgotten", someone, None, (("voice", stranger),), True, NOW)
    await on_record(session, event)
    assert await session.scalar(select(VoiceRef).where(VoiceRef.reference == stranger)) is None
    assert await prints_under(session, stranger) == 0


async def test_an_erased_record_takes_every_reference_of_the_person(session):
    dana = await person(session, "Dana", "adult")
    ref = await learnt_voice(session, dana, DANA)
    # Even a reference the notice did not list.
    await on_record(session, RecordEvent("erased", dana.id, None, (), False, NOW))
    assert await refs_of(session, "voice") == set()
    assert await prints_under(session, ref.reference) == 0


async def test_a_missed_notice_is_mended_by_the_reconciliation(session):
    alex = await person(session)
    dana = await person(session, "Dana", "adult")
    kept = await person(session, "Alex Martin", "adult")
    merged = await learnt_voice(session, alex, ALEX)
    forgotten = await learnt_voice(session, dana, DANA)
    stranger = await reference_for(
        session, person_id=None, print_=print_of(SAM), microphone=MIC, now=NOW
    )

    # The record's view after two notices that never came: Alex merged into "Alex Martin",
    # Dana forgotten.
    async def owner_of(_session, *, channel: str, reference: str) -> uuid.UUID | None:
        return kept.id if reference == merged.reference else None

    mended = await reconcile(session, NOW, person_of=owner_of)
    assert mended == 2
    assert await person_of_ref(session, channel="voice", reference=merged.reference) == kept.id
    assert await prints_under(session, merged.reference) == 2
    assert await prints_under(session, forgotten.reference) == 0
    # A voice nobody knows is not the record's to keep: the reconciliation leaves it.
    assert await prints_under(session, stranger) == 1
    # Nothing left to mend.
    assert await reconcile(session, NOW, person_of=owner_of) == 0


async def test_a_reference_made_while_the_pass_runs_is_never_erased(session_factory):
    async with session_factory() as writer:
        alex = await person(writer)
        dana = await person(writer, "Dana", "adult")
        await learnt_voice(writer, dana, DANA)
        await writer.commit()
    made: list[str] = []

    async def meanwhile(session, *, channel: str, reference: str) -> uuid.UUID | None:
        # Alex's first voice is learnt and committed while the pass weighs Dana's reference.
        if not made:
            async with session_factory() as other:
                ref = await learnt_voice(other, alex, ALEX)
                made.append(ref.reference)
                await other.commit()
        return await person_of_ref(session, channel=channel, reference=reference)

    async with session_factory() as session:
        assert await reconcile(session, NOW, person_of=meanwhile) == 0
        await session.commit()
    async with session_factory() as reader:
        assert await person_of_ref(reader, channel="voice", reference=made[0]) == alex.id
        assert await prints_under(reader, made[0]) == 2


async def test_with_the_stand_in_there_is_nothing_to_mend(session):
    alex = await person(session)
    await learnt_voice(session, alex, ALEX)
    assert await reconcile(session, NOW) == 0


async def test_an_unknown_reference_claimed_later_keeps_its_prints(session):
    alex = await person(session)
    stranger = await reference_for(
        session, person_id=None, print_=print_of(ALEX), microphone=MIC, now=NOW
    )
    await claim_reference(session, channel="voice", reference=stranger, person_id=alex.id, now=NOW)
    assert await person_of_ref(session, channel="voice", reference=stranger) == alex.id
    # Its prints stay, and no longer die as an unknown voice's do.
    later = NOW + UNKNOWN_LIVES + timedelta(days=1)
    from domo_core.channels.voice.prints import sweep

    await sweep(session, later)
    assert await prints_under(session, stranger) == 1
    assert stranger in await refs_of(session, "voice")


async def test_the_upkeep_pass_reconciles_at_start_and_then_hourly(session_factory, monkeypatch):
    calls: list[object] = []

    async def counted(session, now, **_: object) -> int:
        calls.append(now)
        return 0

    monkeypatch.setattr(people, "reconcile", counted)
    one_pass = people.voice_upkeep_pass(session_factory)
    await one_pass(NOW)
    await one_pass(NOW + timedelta(minutes=30))
    await one_pass(NOW + timedelta(minutes=61))
    assert calls == [NOW, NOW + timedelta(minutes=61)]


async def test_a_failing_sweep_never_holds_the_reconciliation_back(session_factory, monkeypatch):
    calls: list[object] = []

    async def counted(session, now, **_: object) -> int:
        calls.append(now)
        return 0

    async def broken(session, now) -> int:
        raise RuntimeError("the sweep failed")

    monkeypatch.setattr(people, "reconcile", counted)
    monkeypatch.setattr(people, "sweep", broken)
    one_pass = people.voice_upkeep_pass(session_factory)
    for minutes in (0, 61):
        with pytest.raises(RuntimeError):
            await one_pass(NOW + timedelta(minutes=minutes))
    assert calls == [NOW, NOW + timedelta(minutes=61)]


async def test_a_failing_reconciliation_is_tried_again_and_the_sweep_still_runs(
    session_factory, monkeypatch
):
    calls: list[object] = []
    swept: list[object] = []

    async def broken(session, now, **_: object) -> int:
        calls.append(now)
        raise RuntimeError("the record could not be read")

    async def counted(session, now) -> int:
        swept.append(now)
        return 0

    monkeypatch.setattr(people, "reconcile", broken)
    monkeypatch.setattr(people, "sweep", counted)
    one_pass = people.voice_upkeep_pass(session_factory)
    await one_pass(NOW)
    await one_pass(NOW + timedelta(minutes=1))
    assert calls == [NOW, NOW + timedelta(minutes=1)]
    assert swept == calls


async def test_two_claims_of_one_new_reference_at_once(session_factory):
    import asyncio

    async with session_factory() as writer:
        alex = await person(writer)
        dana = await person(writer, "Dana", "adult")
        await writer.commit()
    reference = "v-2222222222222222"
    async with session_factory() as first, session_factory() as second:
        await claim_reference(
            first, channel="voice", reference=reference, person_id=alex.id, now=NOW
        )
        racing = asyncio.create_task(
            claim_reference(
                second, channel="voice", reference=reference, person_id=dana.id, now=NOW
            )
        )
        await asyncio.sleep(0.5)
        await first.commit()
        with pytest.raises(ReferenceTaken) as taken:
            await racing
        assert taken.value.person_id == alex.id
