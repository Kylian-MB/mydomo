"""Getting it wrong: two records of one person made one, split again within 90 days, and an access
given by mistake taken back at once.

A merge moves everything that points at the absorbed record to the kept one and writes the ids of
what moved, so that a split puts it back; the trace is kept 90 days, then the absorbed row is
emptied. Each mistake the assistant corrects himself is a fact of ``went_wrong``; the same done by a
person in their turn is found by ``learning.undone.undoes_his``, with no code in the tools."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from domo_core.accounts.actors import actor_of
from domo_core.accounts.models import Account, Person, WebSession
from domo_core.accounts.sessions import is_active
from domo_core.audit.log import append_audit
from domo_core.chat.models import MessageAction
from domo_core.confirmations.models import Confirmation
from domo_core.confirmations.store import create_confirmation
from domo_core.conversation.execution import handle_tool_call
from domo_core.db import Base, load_models
from domo_core.devices.models import Device
from domo_core.household.models import Invitation, Successor
from domo_core.household.nicknames import PersonNickname
from domo_core.household.service import active_persons
from domo_core.household.succession import list_successors
from domo_core.household.upkeep import merge_traces_pass
from domo_core.learning.models import WentWrong
from domo_core.learning.read import went_wrong_for
from domo_core.llm.models import Provider, new_provider_id
from domo_core.llm.spending_models import SpendingLimit
from domo_core.llm.types import ToolCall
from domo_core.memory.models import Memory
from domo_core.people import acts, directives, made_roles, merge, records
from domo_core.people.directives import set_directive
from domo_core.people.models import OwnerDirective, PersonAct, PersonLink, RecordMerge
from domo_core.people.reach import known_records
from domo_core.plans.models import Plan
from domo_core.policy.actions import DescribedAction
from domo_core.policy.asks import Asked
from domo_core.policy.models import PolicyRight
from domo_core.policy.subjects import owner_of_now
from domo_core.trust.models import TrustFact
from sqlalchemy import func, select
from test_concerned_person import _judge, _people, _turn
from test_own_idea import LIVE, mark

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
WHY = "Paul calls her mamie and Alex calls her his mother: she is one person"
SPLIT = "Alex says mamie is his wife's mother, not his own"
OOPS = "Sam is not the Sam Alex meant: the access was given to the wrong person"


def call(tool: str, **arguments: Any) -> ToolCall:
    return ToolCall(uuid.uuid4().hex[:8], tool, json.dumps(arguments))


async def run(ctx: Any, reply_id: uuid.UUID, *calls: ToolCall) -> Any:
    record = None
    for one in calls:
        record = await handle_tool_call(one, ctx=ctx, reply_id=reply_id)
    return record


def a_record(name: str, **columns: Any) -> Person:
    """A record the assistant knows by name: no access, no account."""
    values: dict[str, Any] = {
        "role": "guest",
        "state": "known",
        "standing": "visiting",
        "origin": "domo",
        "origin_reason": "Alex spoke of them",
    } | columns
    return Person(display_name=name, **values)


async def merged(session: Any, kept: Person, absorbed: Person, now: datetime = NOW) -> RecordMerge:
    return await merge.merge(
        session, kept=kept, absorbed=absorbed, by="domo", reason=WHY, turn_of=None, now=now
    )


async def fresh(session: Any, person: Person) -> Person:
    found = await session.get(Person, person.id, populate_existing=True)
    assert found is not None
    return found


def person_columns() -> list[Any]:
    """Every column of every table that points at a record, read from the metadata: a table added
    later is checked too."""
    load_models()
    return [
        column
        for table in Base.metadata.tables.values()
        for column in table.columns
        if any(key.column.table.name == "persons" for key in column.foreign_keys)
    ]


# --- one record of two ---------------------------------------------------------------------------


async def test_mamie_and_my_mother_become_one_record(session, add_person):
    alex = await add_person("Alex", "owner")
    paul = await add_person("Paul", "child")
    mother = await add_person("Dana", "adult")
    mamie = a_record("Dana", state="gone", disabled_at=NOW - timedelta(days=30))
    session.add(mamie)
    await session.flush()
    grandparent = await made_roles.create(
        session, name="grandparent", base="guest", allow=(), deny=(), by="domo", reason=WHY, now=NOW
    )
    mamie.made_role_id = grandparent.id
    session.add_all(
        [
            Memory(
                subject="mamie",
                content="Mamie likes tea",
                kind="preference",
                origin="others_words",
                about_id=mamie.id,
                said_by_id=paul.id,
            ),
            PersonNickname(
                person_id=paul.id,
                target_id=mamie.id,
                nickname="mamie",
                nickname_key="mamie",
                created_at=NOW,
            ),
            PersonLink(from_id=mamie.id, to_id=paul.id, relation="grandmother of"),
            PersonLink(from_id=mother.id, to_id=alex.id, relation="mother of"),
            # A link between the two would join the record to itself.
            PersonLink(from_id=mamie.id, to_id=mother.id, relation="mother of"),
            TrustFact(
                person_id=mamie.id,
                outcome="held",
                established="seen",
                about="said she comes on Sundays",
                created_at=NOW,
            ),
        ]
    )
    await session.flush()

    trace = await merged(session, mother, mamie)

    assert (trace.kept_id, trace.absorbed_id) == (mother.id, mamie.id)
    assert trace.expires_at == NOW + merge.MERGE_KEPT == NOW + timedelta(days=90)
    assert trace.moved["state"] == "gone"
    memory = (await session.scalars(select(Memory))).one()
    assert memory.about_id == mother.id
    nickname = (await session.scalars(select(PersonNickname))).one()
    assert nickname.target_id == mother.id
    links = (await session.scalars(select(PersonLink).where(PersonLink.from_id == mother.id))).all()
    assert {(link.relation, link.to_id) for link in links} == {
        ("grandmother of", paul.id),
        ("mother of", alex.id),
    }
    trust = (await session.scalars(select(TrustFact))).one()
    assert trust.person_id == mother.id
    # The absorbed record is in no listing any more, and its made role is cleaned up.
    absorbed = await fresh(session, mamie)
    assert absorbed.merged_into_id == mother.id and absorbed.made_role_id is None
    assert mamie.id not in set(await session.scalars(select(Person.id).where(known_records())))
    assert mamie.id not in {person.id for person in await active_persons(session, NOW)}
    await session.refresh(grandparent)
    assert grandparent.deleted_at is not None
    kept = await fresh(session, mother)
    assert (kept.state, kept.role) == ("has_access", "adult")
    [act] = (await session.scalars(select(PersonAct).where(PersonAct.act == "merged"))).all()
    assert (act.person_id, act.other_id, act.by, act.reason) == (mother.id, mamie.id, "domo", WHY)


async def test_merging_with_someone_refused_gives_a_refused_record(
    session, add_person, add_account
):
    alex = await add_person("Alex", "owner")
    cases = [(kind, refused_kept) for kind in ("refuse", "withdraw") for refused_kept in (0, 1)]
    for number, (kind, refused_kept) in enumerate(cases):
        state = "refused" if kind == "refuse" else "withdrawn"
        dana = await add_person("Dana", "adult")
        device = Device(person_id=dana.id, name="Phone", secret_hash=f"h-dana-{number}")
        session.add(device)
        account = None
        if not refused_kept:
            # The record with access has an account: it is the one kept.
            account = await add_account(dana, f"dana-{number}")
            session.add(
                WebSession(
                    account_id=account.id,
                    token_hash=str(number) * 64,
                    expires_at=NOW + timedelta(days=1),
                )
            )
        tom = a_record("Tom")
        session.add(tom)
        await session.flush()
        directive = await set_directive(
            session,
            kind=kind,  # type: ignore[arg-type]
            person=tom,
            description=None,
            except_words=None,
            until=None,
            owner=alex,
            now=NOW,
        )

        trace = await (merged(session, tom, dana) if refused_kept else merged(session, dana, tom))

        which = (kind, refused_kept)
        kept = await fresh(session, tom if refused_kept else dana)
        assert trace.kept_id == kept.id, which
        assert kept.state == state, which
        assert kept.id not in {person.id for person in await active_persons(session, NOW)}
        await session.refresh(device)
        assert device.person_id == kept.id and device.removed_at is not None, which
        if account is not None:
            assert (
                await session.scalars(select(WebSession).where(WebSession.account_id == account.id))
            ).all() == []
        await session.refresh(directive)
        assert directive.person_id == kept.id and directive.lifted_at is None, which


async def test_the_account_is_kept_whichever_side_it_was_on(session, add_person, add_account):
    await add_person("Alex", "owner")
    dana = await add_person("Dana", "adult")
    await add_account(dana, "dana")
    tom = a_record("Tom")
    session.add(tom)
    await session.flush()
    trace = await merged(session, tom, dana)
    assert (trace.kept_id, trace.absorbed_id) == (dana.id, tom.id)
    assert (await fresh(session, tom)).merged_into_id == dana.id
    assert (await fresh(session, dana)).merged_into_id is None
    assert await session.scalar(select(Account.person_id)) == dana.id


async def test_what_was_live_follows_the_kept_record(session, add_person, add_conversation):
    alex = await add_person("Alex", "owner")
    dana = await add_person("Dana", "adult")
    tom = a_record("Tom", state="has_access", first_access_at=NOW)
    session.add(tom)
    await session.flush()
    waiting = await create_confirmation(
        session,
        requester=actor_of(tom),
        tool_name="profile_rename",
        arguments={"name": "Tom"},
        action=DescribedAction("core", "profile.rename", None, "self_profile", "household"),
        approver="adult",
        asked=Asked("yes", "judge"),
        conversation_id=(await add_conversation(alex)).id,
        message_id=None,
        now=NOW,
    )
    plan = Plan(person_id=tom.id, why="Remind Tom of Sunday.", due_at=NOW + timedelta(days=2))
    device = Device(person_id=tom.id, name="Phone", secret_hash="h-tom")
    place = Successor(rank=1, person_id=tom.id)
    invitation = Invitation(
        token_hash="t" * 64,
        display_name="Sam",
        role="guest",
        guest_days=3,
        created_by=tom.id,
        expires_at=NOW + timedelta(days=7),
    )
    session.add_all([plan, device, place, invitation])
    await session.flush()

    await merged(session, dana, tom)

    for row, column in (
        (waiting, "requester_id"),
        (plan, "person_id"),
        (device, "person_id"),
        (place, "person_id"),
        (invitation, "created_by"),
    ):
        await session.refresh(row)
        assert getattr(row, column) == dana.id, type(row).__name__
    assert waiting.status == "pending"
    assert (await fresh(session, dana)).state == "has_access"
    still: list[str] = []
    for column in person_columns():
        if column.table.name in ("record_merges", "person_acts"):
            continue
        found = await session.scalar(
            select(func.count()).select_from(column.table).where(column == tom.id)
        )
        if found:
            still.append(f"{column.table.name}.{column.name}")
    assert still == []


# --- split again within 90 days ------------------------------------------------------------------


def _memory(about: Person, content: str) -> Memory:
    return Memory(
        subject=about.display_name,
        content=content,
        kind="fact",
        origin="others_words",
        about_id=about.id,
    )


async def test_a_merge_is_undone_within_ninety_days_and_not_after(session, add_person):
    await add_person("Alex", "owner")
    dana = await add_person("Dana", "adult")
    tom = a_record("Tom")
    sam = a_record("Sam")
    session.add_all([tom, sam])
    await session.flush()
    session.add_all([_memory(dana, "Dana works nights"), _memory(tom, "Tom plays the drums")])
    await session.flush()
    trace = await merged(session, dana, tom)
    # What is added to the kept record since stays on it.
    session.add(_memory(dana, "Dana likes rain"))
    await session.flush()

    back = await merge.split(
        session, trace, by="domo", reason=SPLIT, turn_of=None, now=NOW + timedelta(days=89)
    )

    assert back.person.id == tom.id and back.stayed == {}
    again = await fresh(session, tom)
    assert (again.merged_into_id, again.state) == (None, "known")
    contents = {memory.content: memory.about_id for memory in await session.scalars(select(Memory))}
    assert contents == {
        "Dana works nights": dana.id,
        "Tom plays the drums": tom.id,
        "Dana likes rain": dana.id,
    }
    assert trace.split_at == NOW + timedelta(days=89)
    [act] = (await session.scalars(select(PersonAct).where(PersonAct.act == "split"))).all()
    assert (act.person_id, act.other_id, act.reason) == (dana.id, tom.id, SPLIT)

    # Past the 90 days the trace is gone: the absorbed row is emptied.
    late = await merged(session, dana, sam)
    after = NOW + timedelta(days=91)
    with pytest.raises(merge.TraceGone):
        await merge.split(session, late, by="domo", reason=SPLIT, turn_of=None, now=after)
    assert await merge.purge(session, after) == 1
    await session.refresh(late)
    assert late.moved == {}
    emptied = await fresh(session, sam)
    assert emptied.forgotten_at == after and emptied.display_name == ""
    # The trace split in time is cleared without emptying anyone; a second purge finds nothing.
    assert await merge.purge(session, after) == 0
    assert (await fresh(session, tom)).forgotten_at is None


async def test_the_upkeep_purges_the_traces_past_their_ninety_days(session_factory):
    async with session_factory() as writer:
        writer.add(Person(display_name="Alex", role="owner"))
        dana, tom = Person(display_name="Dana", role="adult"), a_record("Tom")
        writer.add_all([dana, tom])
        await writer.flush()
        await merged(writer, dana, tom)
        await writer.commit()
    await merge_traces_pass(session_factory)(NOW + timedelta(days=89))
    async with session_factory() as reader:
        assert (await reader.get(Person, tom.id)).forgotten_at is None  # type: ignore[union-attr]
    await merge_traces_pass(session_factory)(NOW + timedelta(days=90))
    async with session_factory() as reader:
        assert (await reader.get(Person, tom.id)).forgotten_at is not None  # type: ignore[union-attr]
        assert (await reader.scalars(select(RecordMerge))).one().moved == {}


# --- the mistakes are facts of what went wrong ---------------------------------------------------


async def _his_three_acts(session_factory: Any, people: dict[str, Person]) -> dict[str, Any]:
    """In Dana's turn, the assistant merges Tom and Sam, opens a record for Paul and gives Dana's
    friend Tom an access, each as his own idea."""
    async with session_factory() as writer:
        tom, sam, friend = a_record("Tom"), a_record("Sam"), a_record("Tom", standing="lives_here")
        writer.add_all([tom, sam, friend])
        await writer.commit()
    ctx, reply_id, _ = await _turn(session_factory, people["Dana"], said="Tom is here.")
    made = await run(
        ctx,
        reply_id,
        mark(WHY),
        call("records_merge", keep=f"person:{tom.id}", absorb=f"person:{sam.id}", reason=WHY),
    )
    assert made.status == "done", made.model_result
    opened = await run(
        ctx,
        reply_id,
        mark(WHY),
        call("record_open", name="Paul", standing="visiting", ground="comes", reason=WHY),
    )
    assert opened.status == "done", opened.model_result
    given = await run(
        ctx,
        reply_id,
        mark(OOPS),
        call(
            "household_change_role",
            person=f"person:{friend.id}",
            role="guest",
            guest_days=3,
            reason=OOPS,
        ),
    )
    assert given.status == "done", given.model_result
    return {
        "kept": tom.id,
        "opened": uuid.UUID(opened.model_result["id"]),
        "given": friend.id,
    }


async def _action(session_factory: Any, action_type: str) -> MessageAction:
    async with session_factory() as reader:
        return (
            await reader.scalars(
                select(MessageAction).where(
                    MessageAction.action_type == action_type, MessageAction.status == "done"
                )
            )
        ).one()


async def _went_wrong(session_factory: Any) -> list[WentWrong]:
    async with session_factory() as reader:
        return list(await reader.scalars(select(WentWrong).order_by(WentWrong.at, WentWrong.id)))


async def test_each_mistake_leaves_a_fact_in_the_table_of_what_went_wrong(
    session_factory, monkeypatch
):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"), grants=False)
    _judge(monkeypatch)
    ids = await _his_three_acts(session_factory, people)
    earlier = {
        kind: await _action(session_factory, kind)
        for kind in ("people.merge", "people.open", "household.change_role")
    }
    ctx, reply_id, _ = await _turn(session_factory, people["Dana"], said="Not that Tom.")
    for one in (
        call("records_split", person=f"person:{ids['kept']}", reason=SPLIT),
        call("record_erase", person=f"person:{ids['opened']}", reason=SPLIT),
        call("access_mistake", person=f"person:{ids['given']}", reason=OOPS),
    ):
        done = await run(ctx, reply_id, mark(SPLIT), one)
        assert done.status == "done", done.model_result
        assert "undoes" not in done.model_result
    facts = await _went_wrong(session_factory)
    assert {
        (fact.kind, fact.action_type, fact.subject_key, fact.action_id, fact.of_domo, fact.by_domo)
        for fact in facts
    } == {
        (
            "wrong_verdict",
            kind,
            f"person:{ids[key]}",
            earlier[kind].id,
            True,
            True,
        )
        for kind, key in (
            ("people.merge", "kept"),
            ("people.open", "opened"),
            ("household.change_role", "given"),
        )
    }
    assert len(facts) == 3
    assert {fact.about_person_id for fact in facts} == {people["Dana"].id}


async def test_the_same_mistakes_undone_by_the_owner_are_found_by_what_undoes_his(
    session_factory, monkeypatch
):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"), grants=False)
    _judge(monkeypatch)
    ids = await _his_three_acts(session_factory, people)
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"], said="You got these wrong.")
    for one in (
        call("records_split", person=f"person:{ids['kept']}"),
        call("record_erase", person=f"person:{ids['opened']}"),
        call("access_mistake", person=f"person:{ids['given']}"),
    ):
        done = await run(ctx, reply_id, one)
        assert done.status == "done", done.model_result
        assert "undoes" in done.model_result
    # His judge let the merge and the access through too: those also leave a wrong verdict.
    facts = await _went_wrong(session_factory)
    assert sorted((fact.kind, fact.action_type) for fact in facts) == [
        ("undone", "people.erase"),
        ("undone", "people.mistake"),
        ("undone", "people.split"),
        ("wrong_verdict", "people.mistake"),
        ("wrong_verdict", "people.split"),
    ]
    undone = [fact for fact in facts if fact.kind == "undone"]
    assert sorted((fact.action_type, fact.by_person_id, fact.of_domo) for fact in undone) == [
        ("people.erase", people["Alex"].id, True),
        ("people.mistake", people["Alex"].id, True),
        ("people.split", people["Alex"].id, True),
    ]
    async with session_factory() as reader:
        found = await went_wrong_for(
            reader, action_type="people.merge", subject_key=f"person:{ids['kept']}", about=None
        )
    merging = await _action(session_factory, "people.merge")
    assert [fact.action_id for fact in found if fact.kind == "undone"] == [merging.id]


# --- an access given by mistake ------------------------------------------------------------------


async def test_a_mistaken_access_is_removed_at_once_and_the_owner_is_owed_the_story(
    session_factory, monkeypatch
):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"), grants=False)
    _judge(monkeypatch)
    ids = await _his_three_acts(session_factory, people)
    async with session_factory() as writer:
        await append_audit(
            writer,
            actor=f"person:{ids['given']}",
            action="policy.decision",
            details={"action_type": "note.leave", "decision": "allow", "tool": "note_leave"},
        )
        await writer.commit()
    later = LIVE + timedelta(minutes=45)
    ctx, reply_id, notified = await _turn(
        session_factory, people["Dana"], said="Not that Tom.", clock=lambda: later
    )
    done = await run(
        ctx,
        reply_id,
        mark(OOPS),
        call("access_mistake", person=f"person:{ids['given']}", reason=OOPS),
    )
    assert done.status == "done", done.model_result
    told = done.model_result
    assert told["role"] == "guest" and told["open_for_minutes"] == 45
    assert told["did"] == {"note.leave": 1}
    assert notified == [people["Alex"].id]
    async with session_factory() as reader:
        friend = await reader.get(Person, ids["given"])
        assert friend is not None and friend.state == "gone"
        [act] = (
            await reader.scalars(select(PersonAct).where(PersonAct.act == "access_mistake"))
        ).all()
        assert (act.person_id, act.owed_to_owner, act.by, act.reason) == (
            ids["given"],
            True,
            "domo",
            OOPS,
        )
        assert await reader.scalar(select(func.count()).select_from(OwnerDirective)) == 0
        assert await reader.scalar(select(func.count()).select_from(Confirmation)) == 0
        # Told the single way (``acts.tell_owner``): his news are kept, so a turn starts for him.
        owner = await reader.get(Person, people["Alex"].id)
        assert owner is not None and owner.news_at is not None


# --- two records that each have an account (the owner's decision of 2026-11-26) ------------------


async def _two_accounts(session_factory: Any) -> dict[str, Person]:
    people = await _people(
        session_factory, ("Alex", "owner"), ("Dana", "adult"), ("Sam", "adult"), grants=False
    )
    async with session_factory() as writer:
        account = (
            await writer.scalars(select(Account).where(Account.person_id == people["Sam"].id))
        ).one()
        writer.add(
            WebSession(
                account_id=account.id, token_hash="s" * 64, expires_at=LIVE + timedelta(days=1)
            )
        )
        writer.add(Device(person_id=people["Sam"].id, name="Phone", secret_hash="h-sam"))
        await writer.commit()
    return people


async def _closed(session_factory: Any, sam: Person) -> None:
    """Sam's account is closed: it is still his, but nothing of it is open and he cannot sign
    in (his record is merged)."""
    async with session_factory() as reader:
        account = (await reader.scalars(select(Account).where(Account.person_id == sam.id))).one()
        assert (
            await reader.scalars(select(WebSession).where(WebSession.account_id == account.id))
        ).all() == []
        device = (await reader.scalars(select(Device))).one()
        assert device.removed_at is not None
        record = await reader.get(Person, sam.id)
        assert record is not None and record.merged_into_id is not None
        assert not is_active(record, LIVE)


def _merge_call(keep: Person, absorb: Person, **more: Any) -> ToolCall:
    return call("records_merge", keep=f"person:{keep.id}", absorb=f"person:{absorb.id}", **more)


async def test_two_records_with_an_account_each_are_merged_on_the_owner_s_request(
    session_factory, monkeypatch
):
    people = await _two_accounts(session_factory)
    _judge(monkeypatch)
    ctx, reply_id, notified = await _turn(
        session_factory, people["Alex"], said="Dana and Sam are the same person."
    )
    done = await run(ctx, reply_id, _merge_call(people["Dana"], people["Sam"]))
    assert done.status == "done", done.model_result
    assert done.model_result["account_kept"] == f"person:{people['Dana'].id}"
    assert done.model_result["account_closed"] == f"person:{people['Sam'].id}"
    await _closed(session_factory, people["Sam"])
    # His own request: the journal keeps it, he is not told of it again.
    assert notified == []
    async with session_factory() as reader:
        [act] = (await reader.scalars(select(PersonAct).where(PersonAct.act == "merged"))).all()
        assert act.owed_to_owner is False and act.by == f"person:{people['Alex'].id}"


async def test_two_records_with_an_account_each_are_merged_on_his_own_and_the_owner_is_told(
    session_factory, monkeypatch
):
    people = await _two_accounts(session_factory)
    judged = _judge(monkeypatch)
    ctx, reply_id, notified = await _turn(
        session_factory, people["Dana"], said="Sam is my other account, I made it by mistake."
    )
    done = await run(
        ctx, reply_id, mark(WHY), _merge_call(people["Dana"], people["Sam"], reason=WHY)
    )
    assert done.status == "done", done.model_result
    assert len(judged.cases) == 1
    await _closed(session_factory, people["Sam"])
    assert notified == [people["Alex"].id]
    async with session_factory() as reader:
        [act] = (await reader.scalars(select(PersonAct).where(PersonAct.act == "merged"))).all()
        assert (act.owed_to_owner, act.by, act.reason) == (True, "domo", WHY)
        # Told the single way (``acts.tell_owner``): his news are kept, so a turn starts for him.
        owner = await reader.get(Person, people["Alex"].id)
        assert owner is not None and owner.news_at is not None


async def test_a_split_brings_the_closed_account_back(session_factory, monkeypatch):
    people = await _two_accounts(session_factory)
    _judge(monkeypatch)
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"], said="Merge them.")
    dana, sam = people["Dana"], people["Sam"]
    merged_now = await run(ctx, reply_id, _merge_call(dana, sam))
    assert merged_now.status == "done", merged_now.model_result
    back = await run(ctx, reply_id, call("records_split", person=f"person:{dana.id}"))
    assert back.status == "done", back.model_result
    assert back.model_result["account_open_again"] is True
    assert back.model_result["sign_in_again"] is True
    assert back.model_result["devices_stay_removed"] == 1
    async with session_factory() as reader:
        record = await reader.get(Person, sam.id)
        assert record is not None and record.merged_into_id is None
        assert is_active(record, LIVE)
        owner_of_login = await reader.scalar(
            select(Account.person_id).where(Account.username == "sam")
        )
        assert owner_of_login == sam.id


# --- the owner's directives and a merge ----------------------------------------------------------


async def test_the_assistant_merges_someone_refused_and_the_record_is_refused(
    session_factory, monkeypatch
):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"), grants=False)
    _judge(monkeypatch)
    async with session_factory() as writer:
        tom = a_record("Tom")
        newcomer = a_record("Tom", state="has_access", first_access_at=LIVE)
        writer.add_all([tom, newcomer])
        await writer.flush()
        owner = await writer.get(Person, people["Alex"].id)
        assert owner is not None
        await set_directive(
            writer,
            kind="refuse",
            person=tom,
            description=None,
            except_words=None,
            until=None,
            owner=owner,
            now=LIVE,
        )
        await writer.commit()
    ctx, reply_id, _ = await _turn(session_factory, people["Dana"], said="The new Tom is that Tom.")
    done = await run(ctx, reply_id, mark(WHY), _merge_call(newcomer, tom, reason=WHY))
    assert done.status == "done", done.model_result
    async with session_factory() as reader:
        kept = await reader.get(Person, newcomer.id)
        assert kept is not None and kept.state == "refused"


async def test_a_split_lets_each_directive_bind_where_it_belongs(session, add_person):
    alex = await add_person("Alex", "owner")
    dana = await add_person("Dana", "adult")
    tom = a_record("Tom")
    sam = a_record("Sam", state="has_access", first_access_at=NOW)
    session.add_all([tom, sam])
    await session.flush()
    # A refusal that came from the absorbed record goes back to it: the kept one is free again.
    await set_directive(
        session,
        kind="refuse",
        person=tom,
        description=None,
        except_words=None,
        until=None,
        owner=alex,
        now=NOW,
    )
    trace = await merged(session, dana, tom)
    assert (await fresh(session, dana)).state == "refused"
    await merge.split(session, trace, by="domo", reason=SPLIT, turn_of=None, now=NOW)
    assert (await fresh(session, dana)).state == "has_access"
    assert (await fresh(session, tom)).state == "refused"
    # A withdrawal of the merged person, set since the merge, binds both once split.
    later = await merged(session, dana, sam)
    await set_directive(
        session,
        kind="withdraw",
        person=await fresh(session, dana),
        description=None,
        except_words=None,
        until=None,
        owner=alex,
        now=NOW,
    )
    await merge.split(session, later, by="domo", reason=SPLIT, turn_of=None, now=NOW)
    assert (await fresh(session, sam)).state == "withdrawn"
    assert (await fresh(session, dana)).state == "withdrawn"
    withdrawn = set(
        await session.scalars(
            select(OwnerDirective.person_id).where(
                OwnerDirective.kind == "withdraw", OwnerDirective.lifted_at.is_(None)
            )
        )
    )
    assert withdrawn == {dana.id, sam.id}


async def test_a_directive_the_kept_record_had_before_the_merge_stays_its_own(session, add_person):
    alex = await add_person("Alex", "owner")
    tom = a_record("Tom")
    sam = a_record("Sam", state="has_access", first_access_at=NOW)
    session.add_all([tom, sam])
    await session.flush()
    await set_directive(
        session,
        kind="refuse",
        person=tom,
        description=None,
        except_words=None,
        until=None,
        owner=alex,
        now=NOW,
    )
    trace = await merged(session, tom, sam)
    assert (await fresh(session, tom)).state == "refused"
    await merge.split(session, trace, by="domo", reason=SPLIT, turn_of=None, now=NOW)
    # The refusal was Tom's before the merge: Sam gets back what he had, and nothing binds him.
    assert (await fresh(session, tom)).state == "refused"
    assert (await fresh(session, sam)).state == "has_access"
    refused = set(
        await session.scalars(
            select(OwnerDirective.person_id).where(OwnerDirective.lifted_at.is_(None))
        )
    )
    assert refused == {tom.id}


# --- what collides: the stricter wins ------------------------------------------------------------


def _right(person: Person, access: str, owner: Person) -> PolicyRight:
    return PolicyRight(
        person_id=person.id,
        action_type="note.leave",
        access=access,
        summary=f"{person.display_name} {access}.",
        owner_words="so",
        created_by=owner.id,
    )


def _limit(provider: Provider, person: Person, amount: int, owner: Person) -> SpendingLimit:
    return SpendingLimit(
        provider_id=provider.id,
        person_id=person.id,
        amount=Decimal(amount),
        currency="EUR",
        period="month",
        set_by=owner.id,
    )


async def test_the_stricter_right_limit_and_place_stay_in_force_on_the_kept_record(
    session, add_person
):
    alex = await add_person("Alex", "owner")
    dana = await add_person("Dana", "adult")
    tom = await add_person("Tom", "guest")
    provider = Provider(
        id=new_provider_id(),
        kind="anthropic",
        base_url="https://api.anthropic.com",
        given_by=alex.id,
        is_general=False,
    )
    session.add(provider)
    await session.flush()
    allow, deny = _right(dana, "allow", alex), _right(tom, "deny", alex)
    ours, theirs = _limit(provider, dana, 10, alex), _limit(provider, tom, 5, alex)
    session.add_all(
        [
            allow,
            deny,
            ours,
            theirs,
            Successor(rank=1, person_id=tom.id),
            Successor(rank=2, person_id=dana.id),
        ]
    )
    await session.flush()

    trace = await merged(session, dana, tom)

    in_force = (
        await session.scalars(
            select(PolicyRight).where(
                PolicyRight.person_id == dana.id, PolicyRight.revoked_at.is_(None)
            )
        )
    ).all()
    assert [right.access for right in in_force] == ["deny"]
    limits = (
        await session.scalars(select(SpendingLimit).where(SpendingLimit.person_id == dana.id))
    ).all()
    assert [limit.amount for limit in limits] == [Decimal(5)]
    places = await list_successors(session, NOW)
    assert [(place.rank, place.name) for place in places] == [(1, "Dana")]

    await merge.split(session, trace, by="domo", reason=SPLIT, turn_of=None, now=NOW)
    for row in (allow, deny, ours):
        await session.refresh(row)
    assert (allow.person_id, allow.revoked_at) == (dana.id, None)
    assert (deny.person_id, deny.revoked_at) == (tom.id, None)
    assert ours.amount == Decimal(10)
    places = await list_successors(session, NOW)
    assert [(place.rank, place.name) for place in places] == [(1, "Tom"), (2, "Dana")]


async def test_a_merge_into_the_owner_s_record_binds_him_with_nothing_of_the_other(
    session, add_person
):
    """Paul, child-marked, with a deny and a spending limit, taken for a second record of Alex:
    Alex keeps no child mark, no deny and no limit of Paul's."""
    alex = await add_person("Alex", "owner")
    paul = await add_person("Paul", "child")
    provider = Provider(
        id=new_provider_id(),
        kind="anthropic",
        base_url="https://api.anthropic.com",
        given_by=alex.id,
        is_general=False,
    )
    session.add(provider)
    await session.flush()
    deny, limit = _right(paul, "deny", alex), _limit(provider, paul, 5, alex)
    session.add_all([deny, limit])
    await session.flush()

    trace = await merged(session, alex, paul)

    alex = await fresh(session, alex)
    assert not alex.is_child
    for row in (deny, limit):
        await session.refresh(row)
    assert (deny.person_id, limit.person_id) == (paul.id, paul.id)
    await merge.split(session, trace, by="domo", reason=SPLIT, turn_of=None, now=NOW)
    await session.refresh(deny)
    assert (deny.person_id, deny.revoked_at) == (paul.id, None)


async def test_a_split_gives_an_access_back_only_through_the_single_door(session, add_person):
    """Two records of Tom merged, the absorbed one with an access: split, it comes back through
    ``give_access`` (its act); while Alex's withdrawal of the kept record binds both, it does
    not, and the records are two all the same."""
    alex = await add_person("Alex", "owner")
    kept = a_record("Tom")
    absorbed = a_record("Tom", state="has_access", first_access_at=NOW - timedelta(days=3))
    session.add_all([kept, absorbed])
    await session.flush()
    trace = await merged(session, kept, absorbed)
    await merge.split(session, trace, by="domo", reason=SPLIT, turn_of=None, now=NOW)
    back = await fresh(session, absorbed)
    assert (back.merged_into_id, back.state) == (None, "has_access")
    given = await session.scalars(
        select(PersonAct).where(PersonAct.act == "access_given", PersonAct.person_id == back.id)
    )
    assert len(given.all()) == 1

    again = await merged(session, kept, back, now=NOW + timedelta(hours=1))
    await set_directive(
        session,
        kind="withdraw",
        person=await fresh(session, kept),
        description=None,
        except_words=None,
        until=None,
        owner=alex,
        now=NOW + timedelta(hours=2),
    )
    done = await merge.split(
        session, again, by="domo", reason=SPLIT, turn_of=None, now=NOW + timedelta(hours=3)
    )
    back = await fresh(session, absorbed)
    assert back.merged_into_id is None
    assert back.state != "has_access"
    assert done.without_access == "not_given"


async def test_what_stayed_on_the_absorbed_record_goes_when_the_trace_is_emptied(
    session, add_person, add_account
):
    await add_person("Alex", "owner")
    dana = await add_person("Dana", "adult")
    tom = await add_person("Tom", "adult")
    await add_account(dana, "dana")
    closed = await add_account(tom, "tom")
    session.add_all(
        [
            PersonLink(from_id=tom.id, to_id=dana.id, relation="brother of"),
            Successor(rank=1, person_id=dana.id),
            Successor(rank=2, person_id=tom.id),
            Successor(rank=3, name="Sam", relation="a cousin"),
        ]
    )
    await session.flush()
    trace = await merged(session, dana, tom)
    assert {"person_links.from_id", "accounts.person_id", "successors.person_id"} <= set(
        trace.moved["stayed"]
    )
    assert await merge.purge(session, NOW + timedelta(days=90)) == 1
    assert (await session.scalars(select(PersonLink))).all() == []
    # The account closed by the merge goes with the record, as when someone is forgotten.
    assert await session.get(Account, closed.id, populate_existing=True) is None
    # The place of the absorbed record goes, and the ranks keep no gap.
    places = (await session.scalars(select(Successor).order_by(Successor.rank))).all()
    assert [(place.rank, place.person_id, place.name) for place in places] == [
        (1, dana.id, None),
        (2, None, "Sam"),
    ]


# --- the owner's record is always the one kept ---------------------------------------------------


async def test_the_owner_s_record_is_always_the_one_kept(session_factory, monkeypatch):
    people = await _people(session_factory, ("Alex", "owner"), ("Sam", "adult"), grants=False)
    _judge(monkeypatch)
    alex, sam = people["Alex"], people["Sam"]
    # The owner's own request, naming himself as the one absorbed; then the assistant's idea in
    # Sam's turn, naming the owner as the one kept. Each time the owner's record is kept.
    turns = (
        (alex, _merge_call(sam, alex), []),
        (sam, _merge_call(alex, sam, reason=WHY), [alex.id]),
    )
    for speaker, merging, told in turns:
        # The owner's turn is made before the merge: Sam's conversation is his while they are one.
        owner_ctx, owner_reply, _ = await _turn(session_factory, alex, said="Not the same.")
        ctx, reply_id, notified = await _turn(session_factory, speaker, said="Same person.")
        calls = (merging,) if speaker is alex else (mark(WHY), merging)
        done = await run(ctx, reply_id, *calls)
        assert done.status == "done", done.model_result
        assert done.model_result["kept"] == f"person:{alex.id}"
        assert done.model_result["account_closed"] == f"person:{sam.id}"
        assert notified == told
        async with session_factory() as reader:
            assert await owner_of_now(reader, LIVE) == alex.id
            owner = await reader.get(Person, alex.id)
            assert owner is not None and owner.role == "owner" and owner.merged_into_id is None
        # Split again: Sam is back, the owner is still the owner.
        back = await run(owner_ctx, owner_reply, call("records_split", person=f"person:{alex.id}"))
        assert back.status == "done", back.model_result
        assert back.model_result["back"] == f"person:{sam.id}"
        async with session_factory() as reader:
            assert await owner_of_now(reader, LIVE) == alex.id
            again = await reader.get(Person, sam.id)
            assert again is not None and again.merged_into_id is None and is_active(again, LIVE)


# --- directives at a split: whoever set them, on one record only ---------------------------------


async def test_a_directive_set_since_the_merge_binds_both_whoever_is_the_owner_now(
    session, add_person
):
    alex = await add_person("Alex", "owner")
    dana = await add_person("Dana", "adult")
    sam = a_record("Sam", state="has_access", first_access_at=NOW)
    session.add(sam)
    await session.flush()
    trace = await merged(session, dana, sam)
    await set_directive(
        session,
        kind="withdraw",
        person=await fresh(session, dana),
        description=None,
        except_words=None,
        until=None,
        owner=alex,
        now=NOW,
    )
    # Whoever set it is no longer the owner: the directive still binds.
    alex.role = "adult"
    await session.flush()
    await merge.split(session, trace, by="domo", reason=SPLIT, turn_of=None, now=NOW)
    assert (await fresh(session, sam)).state == "withdrawn"
    copy = (
        await session.scalars(select(OwnerDirective).where(OwnerDirective.person_id == sam.id))
    ).one()
    assert (copy.kind, copy.set_by, copy.lifted_at) == ("withdraw", alex.id, None)


async def test_a_directive_of_the_absorbed_record_ends_on_one_record_only(session, add_person):
    alex = await add_person("Alex", "owner")
    dana = await add_person("Dana", "adult")
    tom = a_record("Tom")
    session.add(tom)
    await session.flush()
    first = await set_directive(
        session,
        kind="refuse",
        person=tom,
        description=None,
        except_words=None,
        until=None,
        owner=alex,
        now=NOW,
    )
    trace = await merged(session, dana, tom)
    # Something put a refusal on the absorbed record since: the first one cannot go back.
    session.add(OwnerDirective(kind="refuse", person_id=tom.id, set_by=alex.id, set_at=NOW))
    await session.flush()
    await merge.split(session, trace, by="domo", reason=SPLIT, turn_of=None, now=NOW)
    await session.refresh(first)
    assert (first.person_id, first.lifted_at) == (dana.id, NOW)
    in_force = set(
        await session.scalars(
            select(OwnerDirective.person_id).where(OwnerDirective.lifted_at.is_(None))
        )
    )
    assert in_force == {tom.id}
    assert (await fresh(session, dana)).state == "has_access"
    assert (await fresh(session, tom)).state == "refused"


# --- spending limits in two currencies -----------------------------------------------------------


async def test_a_limit_in_another_currency_is_dropped_and_told_to_the_owner(session, add_person):
    alex = await add_person("Alex", "owner")
    dana = await add_person("Dana", "adult")
    tom = await add_person("Tom", "guest")
    provider = Provider(
        id=new_provider_id(),
        kind="anthropic",
        base_url="https://api.anthropic.com",
        given_by=alex.id,
        is_general=False,
    )
    session.add(provider)
    await session.flush()
    ours, theirs = _limit(provider, dana, 10, alex), _limit(provider, tom, 1, alex)
    theirs.currency = "USD"
    session.add_all([ours, theirs])
    await session.flush()
    await merged(session, dana, tom)
    await session.refresh(ours)
    # No rate is read: the kept record's limit holds as it was.
    assert (ours.amount, ours.currency, ours.person_id) == (Decimal(10), "EUR", dana.id)
    [act] = (await session.scalars(select(PersonAct).where(PersonAct.act == "merged"))).all()
    assert act.owed_to_owner is True
    [row] = await acts.owed_rows(session)
    assert row["limits_dropped"] == [
        {
            "holds": {"amount": "10.0000", "currency": "EUR", "period": "month"},
            "dropped": {"amount": "1.0000", "currency": "USD", "period": "month"},
        }
    ]
    assert "account_closed" not in row


async def test_limits_in_one_currency_keep_the_lower_per_day(session, add_person):
    alex = await add_person("Alex", "owner")
    dana = await add_person("Dana", "adult")
    tom = await add_person("Tom", "guest")
    provider = Provider(
        id=new_provider_id(),
        kind="anthropic",
        base_url="https://api.anthropic.com",
        given_by=alex.id,
        is_general=False,
    )
    session.add(provider)
    await session.flush()
    # 20 a month is 0.67 a day; 2 a week is 0.29 a day: the week's limit is the lower.
    ours, theirs = _limit(provider, dana, 20, alex), _limit(provider, tom, 2, alex)
    theirs.period = "week"
    session.add_all([ours, theirs])
    await session.flush()
    await merged(session, dana, tom)
    await session.refresh(ours)
    assert (ours.amount, ours.period) == (Decimal(2), "week")
    [act] = (await session.scalars(select(PersonAct).where(PersonAct.act == "merged"))).all()
    assert act.owed_to_owner is False


# --- a record that was ever in the house, a made role gone, what collides on a split -------------


async def test_a_record_that_had_access_stays_one_that_had_it_once_merged(session, add_person):
    await add_person("Alex", "owner")
    tom = a_record("Tom")
    gone = a_record("Tom", state="gone", disabled_at=NOW, first_access_at=NOW - timedelta(days=30))
    session.add_all([tom, gone])
    await session.flush()
    await merged(session, tom, gone)
    kept = await fresh(session, tom)
    assert kept.first_access_at == NOW - timedelta(days=30)
    with pytest.raises(records.NotPossible, match="has_been_here"):
        await records.erase(session, kept, by="domo", reason=SPLIT, turn_of=None, now=NOW)


async def test_a_split_does_not_give_back_an_access_whose_made_role_is_gone(session, add_person):
    await add_person("Alex", "owner")
    dana = await add_person("Dana", "adult")
    role = await made_roles.create(
        session, name="neighbour", base="guest", allow=(), deny=(), by="domo", reason=WHY, now=NOW
    )
    tom = a_record("Tom", state="has_access", first_access_at=NOW, made_role_id=role.id)
    session.add(tom)
    await session.flush()
    trace = await merged(session, dana, tom)
    await session.refresh(role)
    assert role.deleted_at is not None
    done = await merge.split(session, trace, by="domo", reason=SPLIT, turn_of=None, now=NOW)
    assert done.without_access == "made_role_gone"
    back = await fresh(session, tom)
    assert (back.state, back.made_role_id) == ("gone", None)


async def test_what_cannot_go_back_on_a_split_is_said(session, add_person):
    alex = await add_person("Alex", "owner")
    dana = await add_person("Dana", "adult")
    tom = a_record("Tom")
    session.add(tom)
    await session.flush()
    nickname: dict[str, Any] = {
        "target_id": alex.id,
        "nickname": "boss",
        "nickname_key": "boss",
        "created_at": NOW,
    }
    session.add(PersonNickname(person_id=tom.id, **nickname))
    await session.flush()
    trace = await merged(session, dana, tom)
    # Something gave the absorbed record the same nickname since (it cannot be kept twice).
    session.add(PersonNickname(person_id=tom.id, **nickname))
    await session.flush()
    done = await merge.split(session, trace, by="domo", reason=SPLIT, turn_of=None, now=NOW)
    assert done.stayed == {"person_nicknames.person_id": 1}


async def test_an_earlier_merge_is_split_after_the_later_one(session, add_person):
    await add_person("Alex", "owner")
    dana = await add_person("Dana", "adult")
    tom, sam = a_record("Tom"), a_record("Sam")
    session.add_all([tom, sam])
    await session.flush()
    first = await merged(session, tom, sam)
    second = await merged(session, dana, tom)
    with pytest.raises(records.NotPossible, match="merged_since"):
        await merge.split(session, first, by="domo", reason=SPLIT, turn_of=None, now=NOW)
    await merge.split(session, second, by="domo", reason=SPLIT, turn_of=None, now=NOW)
    assert (await fresh(session, sam)).merged_into_id == tom.id
    await merge.split(session, first, by="domo", reason=SPLIT, turn_of=None, now=NOW)
    assert (await fresh(session, sam)).merged_into_id is None


# --- an access already ended, and the owner's own correction -------------------------------------


async def test_a_mistaken_access_that_already_ended_still_gives_the_story(
    session_factory, monkeypatch
):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"), grants=False)
    _judge(monkeypatch)
    async with session_factory() as writer:
        tom = a_record(
            "Tom", state="gone", first_access_at=LIVE - timedelta(hours=2), disabled_at=LIVE
        )
        writer.add(tom)
        await writer.commit()
    ctx, reply_id, notified = await _turn(session_factory, people["Dana"], said="Not that Tom.")
    done = await run(
        ctx, reply_id, mark(OOPS), call("access_mistake", person=f"person:{tom.id}", reason=OOPS)
    )
    assert done.status == "done", done.model_result
    assert done.model_result["removed_now"] is False
    assert done.model_result["open_for_minutes"] == 120
    assert notified == [people["Alex"].id]
    [fact] = await _went_wrong(session_factory)
    assert (fact.kind, fact.subject_key) == ("wrong_verdict", f"person:{tom.id}")


async def test_the_owner_taking_back_an_access_given_by_mistake_withdraws_it(
    session_factory, monkeypatch
):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"), grants=False)
    _judge(monkeypatch)
    ids = await _his_three_acts(session_factory, people)
    ctx, reply_id, notified = await _turn(session_factory, people["Alex"], said="Not him.")
    done = await run(ctx, reply_id, call("access_mistake", person=f"person:{ids['given']}"))
    assert done.status == "done", done.model_result
    assert notified == []
    async with session_factory() as reader:
        friend = await reader.get(Person, ids["given"])
        assert friend is not None and friend.state == "withdrawn"
        [act] = (
            await reader.scalars(select(PersonAct).where(PersonAct.act == "access_mistake"))
        ).all()
        assert act.owed_to_owner is False
        directive = (await reader.scalars(select(OwnerDirective))).one()
        assert (directive.kind, directive.person_id) == ("withdraw", ids["given"])


async def test_the_child_mark_the_merge_adds_is_said(session_factory, monkeypatch):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"), grants=False)
    _judge(monkeypatch)
    async with session_factory() as writer:
        young = a_record("Paul", is_child=True)
        writer.add(young)
        await writer.commit()
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"], said="Paul is Dana.")
    done = await run(ctx, reply_id, _merge_call(people["Dana"], young))
    assert done.status == "done", done.model_result
    assert done.model_result["child_mark_added"] is True


# --- the owner is never under a directive, a merge included --------------------------------------


async def test_the_owner_kept_is_never_put_under_the_other_record_s_directive(
    session, add_person, add_account
):
    alex = await add_person("Alex", "owner")
    account = await add_account(alex, "alex")
    session.add(
        WebSession(account_id=account.id, token_hash="o" * 64, expires_at=NOW + timedelta(days=1))
    )
    await session.flush()
    for number, (kind, state) in enumerate((("refuse", "refused"), ("withdraw", "withdrawn"))):
        tom = a_record("Tom")
        session.add(tom)
        await session.flush()
        directive = await set_directive(
            session,
            kind=kind,  # type: ignore[arg-type]
            person=tom,
            description=None,
            except_words=None,
            until=None,
            owner=alex,
            now=NOW,
        )
        # Named either way: the owner's record is kept.
        trace = await (merged(session, alex, tom) if number else merged(session, tom, alex))
        assert trace.kept_id == alex.id
        assert trace.moved["ended_directives"] == [str(directive.id)]
        owner = await fresh(session, alex)
        assert (owner.state, owner.role, owner.disabled_at) == ("has_access", "owner", None)
        assert await owner_of_now(session, NOW) == alex.id
        assert (await session.scalars(select(WebSession))).all() != []
        assert await directives.under_directive(session, alex.id, NOW) is None
        # Split again: the other record is back under its directive, in force.
        await merge.split(session, trace, by="domo", reason=SPLIT, turn_of=None, now=NOW)
        await session.refresh(directive)
        assert (directive.person_id, directive.lifted_at) == (tom.id, None)
        assert (await fresh(session, tom)).state == state
        assert (await fresh(session, alex)).state == "has_access"


async def test_the_merge_says_the_directive_it_ended_for_the_owner(session_factory, monkeypatch):
    people = await _people(session_factory, ("Alex", "owner"), grants=False)
    _judge(monkeypatch)
    alex = people["Alex"]
    async with session_factory() as writer:
        tom = a_record("Tom")
        writer.add(tom)
        await writer.flush()
        owner = await writer.get(Person, alex.id)
        assert owner is not None
        await set_directive(
            writer,
            kind="refuse",
            person=tom,
            description=None,
            except_words=None,
            until=None,
            owner=owner,
            now=LIVE,
        )
        await writer.commit()
    ctx, reply_id, _ = await _turn(session_factory, alex, said="Tom is me.")
    done = await run(ctx, reply_id, _merge_call(tom, alex))
    assert done.status == "done", done.model_result
    assert done.model_result["kept"] == f"person:{alex.id}"
    assert done.model_result["directives_ended"] == 1
    async with session_factory() as reader:
        assert await owner_of_now(reader, LIVE) == alex.id


async def test_what_the_owner_is_owed_of_a_merge_outlives_the_emptied_trace(session, add_person):
    alex = await add_person("Alex", "owner")
    dana = await add_person("Dana", "adult")
    tom = await add_person("Tom", "guest")
    provider = Provider(
        id=new_provider_id(),
        kind="anthropic",
        base_url="https://api.anthropic.com",
        given_by=alex.id,
        is_general=False,
    )
    session.add(provider)
    await session.flush()
    theirs = _limit(provider, tom, 1, alex)
    theirs.currency = "USD"
    session.add_all([_limit(provider, dana, 10, alex), theirs])
    await session.flush()
    trace = await merged(session, dana, tom)
    assert await merge.purge(session, NOW + timedelta(days=90)) == 1
    await session.refresh(trace)
    # He was not told yet: the facts of his telling stay; the ids of what moved are gone.
    assert set(trace.moved) == {"owed"}
    [row] = await acts.owed_rows(session)
    assert row["limits_dropped"] == [
        {
            "holds": {"amount": "10.0000", "currency": "EUR", "period": "month"},
            "dropped": {"amount": "1.0000", "currency": "USD", "period": "month"},
        }
    ]
    # A second pass finds nothing more to empty.
    assert await merge.purge(session, NOW + timedelta(days=91)) == 0
