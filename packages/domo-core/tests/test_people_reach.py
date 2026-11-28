"""Having access no longer means having an account: a record may have none, and only someone who
can answer (access now and an account) is asked, written to or kept a plan for."""

from datetime import timedelta

import pytest
from domo_core.accounts.models import Account, Person
from domo_core.accounts.sessions import active_at, is_active
from domo_core.confirmations.case import _who_may_agree
from domo_core.confirmations.store import awaiting_approval, create_confirmation
from domo_core.household.service import Removed, active_persons, disable, former_persons
from domo_core.household.succession import entries_from
from domo_core.learning.models import WentWrong
from domo_core.memory.review import review
from domo_core.people.reach import answerers, can_answer, has_account, known_records
from domo_core.policy.actions import DescribedAction
from domo_core.policy.asks import Asked
from domo_core.policy.roles import Actor
from domo_core.policy.settings import load_policy_settings
from domo_core.proofs.service import means_of
from domo_core.tools.context import InvalidArguments
from domo_core.tools.notes import NoteLeave
from sqlalchemy import delete, select
from test_concerned_person import _people, _turn
from test_memory_review import deps_of

KNOWN = {"state": "known", "origin": "domo", "origin_reason": "Alex said Tom comes on Saturday"}


def _rename(person: Person) -> DescribedAction:
    return DescribedAction(
        "core", "profile.rename", f"person:{person.id}", "household", "household"
    )


async def _ask(session, requester: Person, conversation_id, now, **more):
    return await create_confirmation(
        session,
        requester=Actor(requester.id, "child", child=True),
        tool_name="profile_rename",
        arguments={"name": "Sam"},
        action=_rename(requester),
        approver="adult",
        asked=Asked("yes", "judge"),
        conversation_id=conversation_id,
        message_id=None,
        now=now,
        **more,
    )


async def test_an_adult_without_an_account_never_approves_for_a_child(
    session, add_person, add_account, add_conversation, clock
):
    paul = await add_person("Paul", "child")
    dana = await add_person("Dana", "adult")
    await _ask(session, paul, (await add_conversation(paul)).id, clock())
    settings = await load_policy_settings(session)
    later = clock() + timedelta(minutes=1)

    # The only other adult has no account: nobody is eligible and nothing is shown to them.
    assert await awaiting_approval(session, dana, settings, later, limit=20) == ([], 0)
    assert await _who_may_agree(session, [dana.id], paul.id, later) == frozenset()

    await add_account(dana, "dana")
    listed, total = await awaiting_approval(session, dana, settings, later, limit=20)
    assert total == 1 and len(listed) == 1
    assert await _who_may_agree(session, [dana.id], paul.id, later) == {dana.id}


async def test_a_note_for_someone_who_cannot_answer_is_not_possible(session_factory):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"))
    ctx, _, _ = await _turn(session_factory, people["Alex"])
    async with session_factory() as writer:  # ``_people`` gives everyone an account
        await writer.execute(delete(Account).where(Account.person_id == people["Dana"].id))
        await writer.commit()
    arguments = {"recipient": "Dana", "text": "The bins go out tonight."}
    with pytest.raises(InvalidArguments) as refused:
        await NoteLeave().describe(arguments, ctx.actor, ctx)
    assert refused.value.code == "not_possible"
    async with session_factory() as writer:
        writer.add(Account(person_id=people["Dana"].id, username="dana", password_hash="h"))
        await writer.commit()
    described = await NoteLeave().describe(arguments, ctx.actor, ctx)
    assert described.target == f"person:{people['Dana'].id}"


async def test_someone_only_known_is_not_in_the_household(session, add_person, clock):
    await add_person("Alex", "owner")
    tom = await add_person("Tom", "guest", **KNOWN)
    assert [p.display_name for p in await active_persons(session, clock())] == ["Alex"]
    named = await session.scalar(select(Person.id).where(known_records(), Person.id == tom.id))
    assert named == tom.id


async def test_someone_without_an_account_has_no_means_of_proof(session, add_person, clock):
    tom = await add_person("Tom", "guest", **KNOWN)
    assert await means_of(session, tom, clock()) == ()


async def test_removing_someone_without_an_account_ends_nothing_and_fails_nothing(
    session, add_person, clock
):
    alex = await add_person("Alex", "owner")
    sam = await add_person("Sam", "guest")
    removed = await disable(session, sam, clock(), by=alex.id)
    assert removed == Removed(0, 0)
    await session.refresh(sam)
    assert sam.disabled_at is not None and sam.state == "gone"
    assert [p.id for p in await former_persons(session, clock())] == [sam.id]


async def test_the_departure_review_of_someone_without_a_conversation_settles_at_once(
    owner_chat,
):
    server = owner_chat.server
    async with server.session_factory() as writer:
        tom = Person(
            display_name="Tom",
            role="guest",
            state="gone",
            disabled_at=owner_chat.clock(),
            last_review_due=True,
        )
        writer.add(tom)
        await writer.commit()
    assert await review(deps_of(server), tom.id, owner_chat.clock()) == "nothing_new"
    async with server.session_factory() as reader:
        due = await reader.scalar(select(Person.last_review_due).where(Person.id == tom.id))
    assert due is False


async def test_a_successor_by_person_has_an_account(session, add_person, add_account, clock):
    await add_person("Alex", "owner")
    dana = await add_person("Dana", "adult")
    with pytest.raises(InvalidArguments) as refused:
        await entries_from(session, [{"person": "Dana"}], clock())
    assert refused.value.code == "not_possible"
    await add_account(dana, "dana")
    [entry] = await entries_from(session, [{"person": "Dana"}], clock())
    assert entry.person_id == dana.id


async def test_is_active_and_active_at_agree_on_the_state(session, add_person, clock):
    tom = await add_person("Tom", "guest", **KNOWN)
    assert tom.disabled_at is None and not is_active(tom, clock())
    seen = await session.scalar(select(Person.id).where(Person.id == tom.id, active_at(clock())))
    assert seen is None
    sam = await add_person("Sam", "guest")
    assert is_active(sam, clock())
    assert await session.scalar(select(Person.id).where(Person.id == sam.id, active_at(clock())))


async def test_can_answer_needs_access_and_an_account(session, add_person, add_account, clock):
    alex = await add_person("Alex", "owner")
    dana = await add_person("Dana", "adult")
    await add_person("Tom", "guest", **KNOWN)
    await add_account(alex, "alex")
    assert await can_answer(session, alex.id, clock())
    assert not await can_answer(session, dana.id, clock())
    assert [p.id for p in await answerers(session, clock())] == [alex.id]
    assert await session.scalar(select(Person.id).where(Person.id == dana.id, ~has_account()))


async def test_requests_refused_when_someone_is_removed_each_leave_a_fact(
    session, add_person, add_conversation, clock
):
    alex = await add_person("Alex", "owner")
    dana = await add_person("Dana", "adult")
    conversation = await add_conversation(dana)
    first = await _ask(session, dana, conversation.id, clock(), subject_key="person:x")
    second = await _ask(
        session,
        dana,
        conversation.id,
        clock() + timedelta(seconds=1),
        by_domo=True,
        idea_reason="Dana seems to want it",
    )
    removed = await disable(session, dana, clock(), by=alex.id)
    assert removed.requests_refused == 2
    facts = (await session.scalars(select(WentWrong).order_by(WentWrong.of_domo))).all()
    assert [(f.kind, f.reason, f.of_domo) for f in facts] == [
        ("refused", "person_removed", False),
        ("refused", "person_removed", True),
    ]
    assert [f.confirmation_id for f in facts] == [first.id, second.id]
    assert facts[0].subject_key == "person:x"
    assert all(f.action_type == "profile.rename" and f.by_person_id == alex.id for f in facts)
    assert all(f.about_person_id == dana.id for f in facts)


async def test_someone_who_cannot_answer_is_never_the_person_concerned(
    session, add_person, add_account, clock
):
    from domo_core.policy.subjects import Subject, concerned_of

    alex = await add_person("Alex", "owner")
    dana = await add_person("Dana", "adult")
    await add_account(alex, "alex")
    action = DescribedAction(
        "core", "profile.rename", f"person:{dana.id}", "household", "household"
    )
    subject = Subject(f"person:{dana.id}", dana.id)
    # Dana has access but no account: nobody could authenticate as her, so the owner answers.
    assert await concerned_of(session, subject, action, clock()) == alex.id
    await add_account(dana, "dana")
    assert await concerned_of(session, subject, action, clock()) == dana.id
