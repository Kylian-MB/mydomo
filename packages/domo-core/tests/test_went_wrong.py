"""The table of what went wrong and the one way to write into it and read from it
(``learning``): tried on the shared rolled-back ``session``."""

import uuid
from datetime import UTC, datetime, timedelta

from domo_core.accounts.models import Person
from domo_core.learning.models import WentWrong
from domo_core.learning.read import went_wrong_for
from domo_core.learning.record import mark_asked_why, note_went_wrong
from domo_core.memory.service import refuses_secret_shapes
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

NOW = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)


async def _person(session: AsyncSession, name: str, role: str = "adult") -> Person:
    person = Person(display_name=name, role=role)
    session.add(person)
    await session.flush()
    return person


async def test_a_fact_is_kept_with_who_what_and_why(session: AsyncSession):
    alex = await _person(session, "Alex", "owner")
    dana = await _person(session, "Dana")
    row = await note_went_wrong(
        session,
        kind="refused",
        action_type="note.leave",
        now=NOW,
        subject_key="note:dana",
        by=dana.id,
        of_domo=True,
        about=alex.id,
        reason="  not now  ",
    )
    assert row is not None
    kept = (await session.execute(select(WentWrong))).scalar_one()
    assert (kept.kind, kept.action_type, kept.subject_key) == ("refused", "note.leave", "note:dana")
    assert (kept.by_person_id, kept.by_domo, kept.of_domo) == (dana.id, False, True)
    assert (kept.about_person_id, kept.reason, kept.at) == (alex.id, "not now", NOW)
    assert kept.asked_why_at is None
    by_domo = await note_went_wrong(
        session, kind="judge_refused", action_type="note.leave", now=NOW, by="domo"
    )
    by_code = await note_went_wrong(session, kind="expired", action_type="note.leave", now=NOW)
    assert by_domo is not None and (by_domo.by_domo, by_domo.by_person_id) == (True, None)
    assert by_code is not None and (by_code.by_domo, by_code.by_person_id) == (False, None)


async def test_a_reason_is_cut_at_300_and_a_secret_shape_is_dropped(session: AsyncSession):
    long = await note_went_wrong(
        session, kind="failed", action_type="a.b", now=NOW, reason="word " * 100
    )
    assert long is not None and long.reason is not None and len(long.reason) <= 300
    secret = await note_went_wrong(
        session,
        kind="failed",
        action_type="a.b",
        now=NOW,
        reason="the key was sk0123456789abcdef0123456789abcdef",
    )
    assert secret is not None and secret.reason is None
    assert (await session.get(WentWrong, secret.id)) is not None
    nul = await note_went_wrong(session, kind="failed", action_type="a.b", now=NOW, reason="a\x00b")
    assert nul is not None and nul.reason == "ab"


async def test_a_secret_straddling_the_cut_is_still_dropped(session: AsyncSession):
    reason = "word " * 58 + "sk0123456789abcdef0123456789abcdef"
    assert len(reason) > 300 and not refuses_secret_shapes(reason[:300].rstrip())
    row = await note_went_wrong(session, kind="failed", action_type="a.b", now=NOW, reason=reason)
    assert row is not None and row.reason is None
    assert (await session.get(WentWrong, row.id)) is not None


async def test_a_failing_write_never_raises(session: AsyncSession):
    bad = await note_went_wrong(
        session,
        kind="nonsense",  # type: ignore[arg-type]
        action_type="a.b",
        now=NOW,
    )
    assert bad is None
    # The outer transaction is still alive, and its other facts are kept.
    good = await note_went_wrong(session, kind="failed", action_type="a.b", now=NOW)
    assert good is not None
    assert (await session.execute(text("SELECT count(*) FROM went_wrong"))).scalar_one() == 1


async def test_the_same_subject_comes_first(session: AsyncSession):
    alex = await _person(session, "Alex", "owner")
    dana = await _person(session, "Dana")
    other = await note_went_wrong(
        session, kind="refused", action_type="x.y", now=NOW + timedelta(minutes=5), of_domo=True
    )
    about = await note_went_wrong(
        session, kind="refused", action_type="x.y", now=NOW + timedelta(minutes=4), about=alex.id
    )
    older_subject = await note_went_wrong(
        session, kind="refused", action_type="z.z", now=NOW, subject_key="k"
    )
    newer_subject = await note_went_wrong(
        session,
        kind="refused",
        action_type="x.y",
        now=NOW + timedelta(minutes=1),
        subject_key="k",
        about=dana.id,
    )
    unrelated = await note_went_wrong(session, kind="refused", action_type="x.y", now=NOW)
    rows = (other, about, older_subject, newer_subject, unrelated)
    assert all(row is not None for row in rows)
    found = await went_wrong_for(
        session, action_type="x.y", subject_key="k", about=alex.id, of_domo=True
    )
    assert [row.id for row in found] == [
        row.id for row in (newer_subject, older_subject, about, other) if row is not None
    ]
    one = await went_wrong_for(session, action_type="x.y", subject_key="k", about=None, limit=1)
    assert len(one) == 1
    assert await went_wrong_for(session, action_type="x.y", subject_key=None, about=None) == []


async def test_asked_why_is_marked_once(session: AsyncSession):
    row = await note_went_wrong(session, kind="undone", action_type="a.b", now=NOW)
    assert row is not None
    assert await mark_asked_why(session, row.id, NOW) is True
    assert await mark_asked_why(session, row.id, NOW + timedelta(hours=1)) is False
    assert await mark_asked_why(session, uuid.uuid4(), NOW) is False
    await session.refresh(row)
    assert row.asked_why_at == NOW


async def test_a_person_s_deletion_takes_their_facts_and_keeps_the_others(session: AsyncSession):
    sam = await _person(session, "Sam")
    tom = await _person(session, "Tom")
    about_sam = await note_went_wrong(
        session, kind="refused", action_type="a.b", now=NOW, by=tom.id, about=sam.id
    )
    by_sam = await note_went_wrong(
        session, kind="undone", action_type="a.b", now=NOW, by=sam.id, about=tom.id
    )
    assert about_sam is not None and by_sam is not None
    about_sam_id, by_sam_id, sam_id, tom_id = about_sam.id, by_sam.id, sam.id, tom.id
    await session.execute(text("DELETE FROM persons WHERE id = :id"), {"id": sam_id})
    session.expire_all()
    assert await session.get(WentWrong, about_sam_id) is None
    kept = await session.get(WentWrong, by_sam_id)
    assert kept is not None and kept.by_person_id is None and kept.about_person_id == tom_id
