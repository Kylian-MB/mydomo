"""Forgetting someone ("forget me"): a guest or a relative, for good, without the owner's yes.

The record's row stays, emptied, so that the journal and every column that names it keep
standing; everything about them goes (their links, nicknames, trust, the memories about them,
their conversations and account, the traces of merges); what others said that is not about them
stays. Someone who lives here and has access is forgotten too, their access with them, and the
owner is told afterwards; the owner is never forgotten, and a withdrawal or a refusal of the
owner's survives with the strict minimum to know them again."""

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from chat_support import consume_sse, post_message
from domo_core.accounts.actors import actor_of
from domo_core.accounts.models import Account, Person
from domo_core.audit.log import verify_audit_chain
from domo_core.audit.models import AuditEntry
from domo_core.chat.models import Conversation, Message
from domo_core.confirmations.models import Confirmation
from domo_core.confirmations.store import create_confirmation
from domo_core.conversation.execution import handle_tool_call
from domo_core.db import Base, load_models
from domo_core.household.models import Successor
from domo_core.household.nicknames import PersonNickname
from domo_core.learning.models import WentWrong
from domo_core.llm.models import Provider, new_provider_id
from domo_core.llm.types import ToolCall
from domo_core.madetools.models import DeclaredSpending
from domo_core.memory.models import Memory
from domo_core.people import directives, forget, made_roles, merge, records
from domo_core.people.acts import owed_rows
from domo_core.people.directives import set_directive
from domo_core.people.models import (
    AccessPromise,
    ChannelRef,
    MadeRole,
    PersonAct,
    PersonLink,
    RecordMerge,
)
from domo_core.policy.actions import DescribedAction
from domo_core.policy.asks import Asked
from domo_core.policy.models import PolicyRight
from domo_core.policy.subjects import keys_of, subject_of
from domo_core.tools.registry import TOOLS
from domo_core.trust.models import TrustFact
from fakes import FakeChatModel
from sqlalchemy import func, or_, select
from test_concerned_person import _judge, _people, _turn
from test_own_idea import LIVE, mark

NOW = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)
WHY = "Dana says Tom asked her to have him forgotten"


def call(tool: str, **arguments: Any) -> ToolCall:
    return ToolCall(uuid.uuid4().hex[:8], tool, json.dumps(arguments))


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


async def _count(session: Any, table: Any, *where: Any) -> int:
    return await session.scalar(select(func.count()).select_from(table).where(*where)) or 0


async def _visiting(session_factory: Any, person: Person) -> Person:
    """``person`` as a visitor: a guest who does not live here."""
    async with session_factory() as writer:
        row = await writer.get(Person, person.id)
        assert row is not None
        row.standing = "visiting"
        await writer.commit()
        await writer.refresh(row)
        return row


# --- what goes, what stays ------------------------------------------------------------------------


async def test_a_guest_is_forgotten_without_the_owners_yes(session_factory, monkeypatch):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"), ("Sam", "guest"))
    dana = people["Dana"]
    sam = await _visiting(session_factory, people["Sam"])
    async with session_factory() as writer:
        cousin = await made_roles.create(
            writer, name="cousin", base="guest", allow=(), deny=(), by="domo", reason=WHY, now=NOW
        )
        row = await writer.get(Person, sam.id)
        assert row is not None
        row.made_role_id = cousin.id
        writer.add_all(
            [
                Memory(
                    subject="Sam",
                    content="Sam is allergic to cats",
                    kind="fact",
                    origin="others_words",
                    about_id=sam.id,
                    said_by_id=dana.id,
                ),
                Memory(
                    subject="secret",
                    content="Sam keeps a gift for Dana",
                    kind="fact",
                    origin="own_words",
                    said_by_id=sam.id,
                    kept_private=True,
                ),
                PersonLink(from_id=sam.id, to_id=dana.id, relation="cousin of", said_by_id=dana.id),
                PersonNickname(
                    person_id=dana.id,
                    target_id=sam.id,
                    nickname="Sammy",
                    nickname_key="sammy",
                    created_at=NOW,
                ),
                TrustFact(
                    person_id=sam.id,
                    outcome="held",
                    established="seen",
                    about="said so",
                    created_at=NOW,
                ),
                ChannelRef(person_id=sam.id, channel="phone", reference="ref-sam"),
            ]
        )
        await writer.commit()
    judged = _judge(monkeypatch)
    ctx, reply_id, _ = await _turn(session_factory, sam)
    record = await handle_tool_call(call("forget_me"), ctx=ctx, reply_id=reply_id)
    assert record.status == "done", record.model_result
    # Nobody's yes was asked: the assistant's own judge weighed it, as for anything that cannot be
    # undone, and nothing waits for the owner.
    assert len(judged.cases) == 1
    # What the call ran in (their conversation, their account) goes once the turn is over.
    assert ctx.forgotten_in_turn == {sam.id}
    async with session_factory() as reader:
        assert await _count(reader, Account, Account.person_id == sam.id) == 1
    await forget.finish_after_the_turn(session_factory, [sam.id], NOW)
    async with session_factory() as reader:
        assert await _count(reader, Confirmation) == 0
        assert await _count(reader, Memory, Memory.about_id == sam.id) == 0
        assert await _count(reader, Memory, Memory.said_by_id == sam.id) == 0
        assert await _count(reader, Memory) == 0
        assert (
            await _count(
                reader, PersonLink, or_(PersonLink.from_id == sam.id, PersonLink.to_id == sam.id)
            )
            == 0
        )
        assert await _count(reader, PersonNickname) == 0
        assert await _count(reader, TrustFact) == 0
        assert await _count(reader, Account, Account.person_id == sam.id) == 0
        assert await _count(reader, Conversation, Conversation.person_id == sam.id) == 0
        assert await _count(reader, Message, Message.conversation_id == ctx.conversation_id) == 0
        assert await _count(reader, ChannelRef) == 0
        role = await reader.get(MadeRole, cousin.id)
        assert role is not None and role.deleted_at is not None
        forgotten = await reader.get(Person, sam.id)
        assert forgotten is not None
        assert (forgotten.display_name, forgotten.made_role_id) == ("", None)
        assert forgotten.forgotten_at is not None and forgotten.state != "has_access"
        # The acts about them keep no reason, the act of forgetting none either.
        acts = list(await reader.scalars(select(PersonAct).where(PersonAct.person_id == sam.id)))
        assert "forgotten" in {act.act for act in acts}
        assert all(act.reason is None for act in acts)
        assert (await verify_audit_chain(reader)).ok
        # The others are still here.
        assert await _count(reader, Account, Account.person_id == dana.id) == 1


async def test_what_others_said_that_is_not_about_them_stays(session, add_person):
    await add_person("Alex", "owner")
    dana = await add_person("Dana", "adult")
    sam = await add_person("Sam", "guest", standing="visiting")
    tom = await add_person("Tom", "guest", standing="visiting")
    about_tom = Memory(
        subject="Tom",
        content="Tom likes tea",
        kind="fact",
        origin="others_words",
        about_id=tom.id,
        said_by_id=dana.id,
    )
    danas_own = Memory(
        subject="plans",
        content="Dana goes away in May",
        kind="fact",
        origin="own_words",
        said_by_id=dana.id,
        kept_private=True,
    )
    said_by_sam = Memory(
        subject="the house",
        content="the wifi is in the hall",
        kind="fact",
        origin="own_words",
        said_by_id=sam.id,
    )
    about_sam = Memory(
        subject="Sam",
        content="Sam comes on Fridays",
        kind="fact",
        origin="others_words",
        about_id=sam.id,
        said_by_id=dana.id,
    )
    session.add_all([about_tom, danas_own, said_by_sam, about_sam])
    await session.flush()
    await forget.forget(session, sam, by="domo", turn_of=None, now=NOW)
    found = await session.scalars(select(Memory).execution_options(populate_existing=True))
    left = {m.content: m for m in found}
    assert set(left) == {"Tom likes tea", "Dana goes away in May", "the wifi is in the hall"}
    # What they said for everyone stays, no longer tied to them.
    assert left["the wifi is in the hall"].said_by_id is None
    assert left["Tom likes tea"].said_by_id == dana.id


async def test_a_refusal_survives_being_forgotten(session, add_person):
    alex = await add_person("Alex", "owner")
    sam = a_record("Sam")
    session.add(sam)
    await session.flush()
    refusal = await set_directive(
        session,
        kind="refuse",
        person=sam,
        description=None,
        except_words=None,
        until=None,
        owner=alex,
        now=NOW,
    )
    await forget.forget(session, sam, by=f"person:{alex.id}", turn_of=alex.id, now=NOW)
    await session.refresh(refusal)
    await session.refresh(sam)
    assert refusal.lifted_at is None and refusal.person_id == sam.id
    # The strict minimum to know them again: the name they had, nothing more.
    assert refusal.description == "Sam"
    assert (sam.display_name, sam.state) == ("", "refused")
    # Someone who comes back under another name and is found to be them stays refused.
    again = a_record("Sam")
    session.add(again)
    await session.flush()
    trace = await merge.merge(
        session, kept=sam, absorbed=again, by="domo", reason=WHY, turn_of=None, now=NOW
    )
    # The living record is the one kept, and carries the refusal.
    assert trace.kept_id == again.id
    await session.refresh(again)
    assert again.state == "refused"
    found = await directives.under_directive(session, again.id, NOW)
    assert found is not None and found.id == refusal.id


async def test_forgetting_one_person_keeps_a_promise_that_still_waits_for_others(
    session, add_person
):
    """Alex: "give access to Dana's children". The promise was kept for Tom, it stays open for
    Paul. Tom says "forget me": the promise still waits, kept for nobody; one that is over goes."""
    alex = await add_person("Alex", "owner")
    tom = await add_person("Tom", "guest", standing="visiting", is_child=True)
    sam = await add_person("Sam", "guest", standing="visiting")
    promise = {
        "role": "child",
        "standing": "visiting",
        "asked_by": f"person:{alex.id}",
        "is_order": True,
        "reason": "Alex asked it",
    }
    open_one = AccessPromise(description="Dana's children", kept_person_id=tom.id, **promise)
    over = AccessPromise(description="Sam, once", kept_person_id=sam.id, kept_at=NOW, **promise)
    session.add_all([open_one, over])
    await session.flush()
    await forget.forget(session, tom, by="domo", turn_of=None, now=NOW)
    await forget.forget(session, sam, by="domo", turn_of=None, now=NOW)
    kept = await session.get(AccessPromise, open_one.id, populate_existing=True)
    assert kept is not None and kept.kept_person_id is None
    assert await session.get(AccessPromise, over.id, populate_existing=True) is None


async def test_someone_who_lives_here_is_forgotten_and_the_owner_is_told(
    session_factory, monkeypatch
):
    """The owner's answer of 2026-11-27 ("Domo juge"): someone who lives here and has access is
    forgotten at their request, without the owner's yes; their access goes with them, the judge is
    shown that it does, and the owner is told afterwards (an act owed to him, his news)."""
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"))
    alex, dana = people["Alex"], people["Dana"]
    assert forget.loses_access(dana) is True
    assert forget.loses_access(a_record("Tom")) is False
    judged = _judge(monkeypatch)
    ctx, reply_id, notified = await _turn(session_factory, dana)
    record = await handle_tool_call(call("forget_me"), ctx=ctx, reply_id=reply_id)
    assert record.status == "done", record.model_result
    [case] = judged.cases
    assert case.action["facts"]["loses_access"] is True
    assert notified == [alex.id]
    async with session_factory() as reader:
        kept = await reader.get(Person, dana.id)
        owner = await reader.get(Person, alex.id)
        [act] = await reader.scalars(select(PersonAct).where(PersonAct.act == "forgotten"))
        [row] = await owed_rows(reader)
    # What the owner is told: the act and the role they had, never their name.
    assert (row["act"], row["person"], row["role"]) == ("forgotten", None, "adult")
    assert kept is not None and (kept.display_name, kept.state) == ("", "gone")
    assert kept.forgotten_at is not None and kept.disabled_at is not None
    assert act.owed_to_owner is True and act.told_owner_at is None and act.reason is None
    assert owner is not None and owner.news_at == LIVE


async def test_the_owner_who_forgets_someone_who_lives_here_is_not_told_it_again(
    session_factory, monkeypatch
):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"))
    alex = people["Alex"]
    _judge(monkeypatch)
    ctx, reply_id, notified = await _turn(session_factory, alex)
    record = await handle_tool_call(
        call("record_forget", person="Dana"), ctx=ctx, reply_id=reply_id
    )
    assert record.status == "done", record.model_result
    assert notified == []
    async with session_factory() as reader:
        [act] = await reader.scalars(select(PersonAct).where(PersonAct.act == "forgotten"))
    assert act.owed_to_owner is False


async def test_his_own_forgetting_in_the_owner_s_own_turn_is_not_told_again(
    session_factory, monkeypatch
):
    """Forgetting on the assistant's own idea, in the owner's own turn that he sees: owed to him,
    and told as it happens, once, as an invitation is."""
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"))
    _judge(monkeypatch)
    ctx, reply_id, notified = await _turn(session_factory, people["Alex"])
    await handle_tool_call(mark(WHY), ctx=ctx, reply_id=reply_id)
    record = await handle_tool_call(
        call("record_forget", person="Dana", reason=WHY), ctx=ctx, reply_id=reply_id
    )
    assert record.status == "done", record.model_result
    assert notified == []
    async with session_factory() as reader:
        [act] = await reader.scalars(select(PersonAct).where(PersonAct.act == "forgotten"))
    assert (act.owed_to_owner, act.told_owner_at) == (True, LIVE)


async def test_a_guest_forgotten_is_owed_to_nobody(session_factory, monkeypatch):
    people = await _people(session_factory, ("Alex", "owner"), ("Sam", "guest"))
    sam = await _visiting(session_factory, people["Sam"])
    judged = _judge(monkeypatch)
    ctx, reply_id, notified = await _turn(session_factory, sam)
    record = await handle_tool_call(call("forget_me"), ctx=ctx, reply_id=reply_id)
    assert record.status == "done", record.model_result
    assert judged.cases[0].action["facts"]["loses_access"] is False
    assert notified == []
    async with session_factory() as reader:
        [act] = await reader.scalars(select(PersonAct).where(PersonAct.act == "forgotten"))
    assert act.owed_to_owner is False


async def test_the_owner_is_never_forgotten(session_factory, monkeypatch):
    people = await _people(session_factory, ("Alex", "owner"))
    alex = people["Alex"]
    async with session_factory() as session:
        row = await session.get(Person, alex.id)
        assert row is not None
        with pytest.raises(records.NotPossible) as refused:
            await forget.forget(session, row, by=f"person:{alex.id}", turn_of=alex.id, now=NOW)
        assert refused.value.code == "the_owner"
    _judge(monkeypatch)
    ctx, reply_id, _ = await _turn(session_factory, alex)
    record = await handle_tool_call(call("forget_me"), ctx=ctx, reply_id=reply_id)
    assert record.status == "invalid"
    async with session_factory() as reader:
        kept = await reader.get(Person, alex.id)
    assert kept is not None and kept.forgotten_at is None


async def test_a_forgotten_successor_leaves_the_list_and_the_ranks_close_up(session, add_person):
    await add_person("Alex", "owner")
    sam = await add_person("Sam", "guest", standing="visiting")
    tom = await add_person("Tom", "guest", standing="visiting")
    session.add_all(
        [
            Successor(rank=1, person_id=sam.id, access_way="personal_access"),
            Successor(rank=2, person_id=tom.id, access_way="personal_access"),
            Successor(rank=3, name="Paul", relation="my brother", access_way="paper_code"),
        ]
    )
    await session.flush()
    await forget.forget(session, sam, by="domo", turn_of=None, now=NOW)
    places = list(await session.scalars(select(Successor).order_by(Successor.rank)))
    assert [(place.rank, place.person_id, place.name) for place in places] == [
        (1, tom.id, None),
        (2, None, "Paul"),
    ]


async def test_a_key_they_gave_and_people_they_invited_still_hold(session, add_person):
    await add_person("Alex", "owner")
    sam = await add_person("Sam", "guest", standing="visiting")
    tom = await add_person("Tom", "guest", standing="visiting", invited_by=sam.id)
    provider = Provider(
        id=new_provider_id(),
        kind="openai_compatible",
        base_url="http://127.0.0.1:8080",
        given_by=sam.id,
        is_general=False,
    )
    session.add(provider)
    await session.flush()
    await forget.forget(session, sam, by="domo", turn_of=None, now=NOW)
    await session.refresh(provider)
    await session.refresh(tom)
    assert provider.given_by == sam.id
    assert tom.invited_by == sam.id and tom.state == "has_access"
    assert await _count(session, Person, Person.id == sam.id) == 1


async def test_a_relayed_forget_is_the_assistants_own_gesture(session_factory, monkeypatch):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"))
    async with session_factory() as writer:
        tom = a_record("Tom", standing="elsewhere")
        writer.add(tom)
        await writer.flush()
        await records.complete(
            writer, tom, standing="elsewhere", by="domo", reason=WHY, turn_of=None, now=NOW
        )
        await writer.commit()
    _judge(monkeypatch)
    ctx, reply_id, _ = await _turn(session_factory, people["Dana"])
    asked = await handle_tool_call(
        call("record_forget", person=f"person:{tom.id}"), ctx=ctx, reply_id=reply_id
    )
    assert asked.status == "refused" and asked.model_result["reason"] == "not_allowed"
    await handle_tool_call(mark(WHY), ctx=ctx, reply_id=reply_id)
    done = await handle_tool_call(
        call("record_forget", person=f"person:{tom.id}", reason=WHY), ctx=ctx, reply_id=reply_id
    )
    assert done.status == "done", done.model_result
    async with session_factory() as reader:
        row = await reader.get(Person, tom.id)
        assert row is not None and (row.display_name, row.forgotten_at is not None) == ("", True)
        acts = list(await reader.scalars(select(PersonAct).where(PersonAct.person_id == tom.id)))
        assert {act.act for act in acts} == {"completed", "forgotten"}
        # The assistant's reason named them: no act keeps it.
        assert all(act.reason is None for act in acts)
        [forgotten] = [act for act in acts if act.act == "forgotten"]
        assert (forgotten.by, forgotten.turn_of) == ("domo", people["Dana"].id)


async def test_what_was_merged_into_them_goes_with_them(session, add_person):
    await add_person("Alex", "owner")
    sam = await add_person("Sam", "guest", standing="visiting")
    cousin = a_record("Sam the cousin")
    session.add(cousin)
    await session.flush()
    trace = await merge.merge(
        session, kept=sam, absorbed=cousin, by="domo", reason=WHY, turn_of=None, now=NOW
    )
    await forget.forget(session, sam, by="domo", turn_of=None, now=NOW)
    await session.refresh(cousin)
    await session.refresh(trace)
    assert (cousin.display_name, cousin.forgotten_at) == ("", NOW)
    assert trace.moved == {}
    assert await _count(session, RecordMerge) == 1
    with pytest.raises(merge.TraceGone):
        await merge.split(session, trace, by="domo", reason=WHY, turn_of=None, now=NOW)


# --- in a real turn -------------------------------------------------------------------------------


async def test_forgetting_oneself_in_a_turn_lets_it_end_then_takes_what_it_ran_in(
    guest_chat, chat_server
):
    person = guest_chat.person
    model = guest_chat.use_model(
        FakeChatModel(tool_script=[ToolCall("f", "forget_me", "{}"), "Goodbye."], judge="do")
    )
    reply_id = await post_message(guest_chat.client, "forget me, please")
    await consume_sse(guest_chat.client, f"/chat/stream/{reply_id}")
    await chat_server.wait_for_wakes()
    # The model was given the tool's result and answered: the turn went to its end.
    assert '"forgotten": true' in model.calls[-1].messages[-1].content
    async with chat_server.session_factory() as reader:
        row = await reader.get(Person, person.id)
        assert row is not None and row.display_name == "" and row.forgotten_at is not None
        assert await _count(reader, Account, Account.person_id == person.id) == 0
        assert await _count(reader, Conversation, Conversation.person_id == person.id) == 0
        assert await _count(reader, Message, Message.id == uuid.UUID(reply_id)) == 0
        # The journal lines of the call landed: the barrier's decision and the act.
        actions = {
            (entry.action, entry.details.get("action_type"))
            for entry in await reader.scalars(select(AuditEntry))
        }
        assert ("policy.decision", "people.forget_me") in actions
        assert ("people.forgotten", None) in actions
        assert (await verify_audit_chain(reader)).ok


# --- what may still be found, and what is still owed ---------------------------------------------


async def test_the_merge_tool_finds_a_refused_person_who_was_forgotten(
    session_factory, monkeypatch
):
    people = await _people(session_factory, ("Alex", "owner"))
    alex = people["Alex"]
    async with session_factory() as writer:
        sam = a_record("Sam")
        again = a_record("Sam")
        writer.add_all([sam, again])
        await writer.flush()
        owner = await writer.get(Person, alex.id)
        assert owner is not None
        await set_directive(
            writer,
            kind="refuse",
            person=sam,
            description=None,
            except_words=None,
            until=None,
            owner=owner,
            now=NOW,
        )
        await forget.forget(writer, sam, by=f"person:{alex.id}", turn_of=alex.id, now=NOW)
        await writer.commit()
    _judge(monkeypatch)
    ctx, reply_id, _ = await _turn(session_factory, alex)
    done = await handle_tool_call(
        call("records_merge", keep=f"person:{sam.id}", absorb=f"person:{again.id}"),
        ctx=ctx,
        reply_id=reply_id,
    )
    assert done.status == "done", done.model_result
    assert done.model_result["kept"] == f"person:{again.id}"
    assert done.model_result["state"] == "refused"
    # Without a directive in force, a forgotten record is no record.
    async with session_factory() as writer:
        tom = a_record("Tom")
        writer.add(tom)
        await writer.flush()
        await forget.forget(writer, tom, by="domo", turn_of=None, now=NOW)
        await writer.commit()
    refused = await handle_tool_call(
        call("records_merge", keep=f"person:{tom.id}", absorb=f"person:{again.id}"),
        ctx=ctx,
        reply_id=reply_id,
    )
    assert refused.status == "invalid"


async def test_what_the_owner_is_still_owed_of_a_merge_stays(session, add_person):
    await add_person("Alex", "owner")
    sam = await add_person("Sam", "guest", standing="visiting")
    cousin = a_record("Sam the cousin")
    session.add(cousin)
    await session.flush()
    trace = await merge.merge(
        session,
        kept=sam,
        absorbed=cousin,
        by="domo",
        reason=WHY,
        turn_of=None,
        now=NOW,
        owed_to_owner=True,
    )
    owed = trace.moved["owed"]
    await forget.forget(session, sam, by="domo", turn_of=None, now=NOW)
    await session.refresh(trace)
    assert trace.moved == {"owed": owed}


async def test_no_words_about_them_stay_and_the_house_counts_stay_right(session, add_person):
    alex = await add_person("Alex", "owner")
    sam = await add_person("Sam", "guest", standing="visiting")
    tom = a_record("Tom")
    session.add(tom)
    await session.flush()
    cousin = await made_roles.create(
        session, name="cousin", base="guest", allow=(), deny=(), by="domo", reason=WHY, now=NOW
    )
    sam.made_role_id = cousin.id
    session.add_all(
        [
            PolicyRight(
                person_id=sam.id,
                action_type="household.invite",
                access="allow",
                summary="Sam may invite.",
                owner_words="Sam may invite his friends",
                created_by=alex.id,
            ),
            PersonAct(act="completed", person_id=tom.id, reason=WHY, by="domo", turn_of=sam.id),
            DeclaredSpending(for_person_id=sam.id, operation="order", amount_cents=500, at=NOW),
        ]
    )
    await session.flush()
    await forget.forget(session, sam, by="domo", turn_of=None, now=NOW)
    assert await _count(session, PolicyRight, PolicyRight.person_id == sam.id) == 0
    acts = list(
        await session.scalars(
            select(PersonAct)
            .where(or_(PersonAct.turn_of == sam.id, PersonAct.role_id == cousin.id))
            .execution_options(populate_existing=True)
        )
    )
    assert acts and all(act.reason is None for act in acts)
    role = await session.get(MadeRole, cousin.id, populate_existing=True)
    assert role is not None and role.deleted_at is not None and role.reason == made_roles.UNUSED
    # What a program declared it spent for them still counts.
    assert await _count(session, DeclaredSpending, DeclaredSpending.for_person_id == sam.id) == 1


async def test_whoever_waited_for_them_is_told_their_request_is_refused(
    session, add_person, add_conversation
):
    await add_person("Alex", "owner")
    dana = await add_person("Dana", "adult")
    session.add(Account(person_id=dana.id, username="dana", password_hash="hash"))
    sam = await add_person("Sam", "guest", standing="visiting")
    waiting = await create_confirmation(
        session,
        requester=actor_of(dana),
        tool_name="profile_rename",
        arguments={"name": "Dana"},
        action=DescribedAction("core", "profile.rename", None, "self_profile", "household"),
        approver="adult",
        asked=Asked("yes", "judge"),
        conversation_id=(await add_conversation(dana)).id,
        message_id=None,
        now=NOW,
    )
    waiting.concerned_id = sam.id
    await session.flush()
    await forget.forget(session, sam, by="domo", turn_of=None, now=NOW)
    await session.refresh(waiting)
    await session.refresh(dana)
    assert (waiting.status, waiting.concerned_id) == ("refused", None)
    assert dana.news_at == NOW
    [fact] = list(
        await session.scalars(select(WentWrong).where(WentWrong.confirmation_id == waiting.id))
    )
    assert (fact.kind, fact.reason, fact.about_person_id) == (
        "refused",
        forget.FORGOTTEN_REASON,
        dana.id,
    )


async def test_the_upkeep_finishes_what_a_call_left(session, add_person, add_conversation):
    await add_person("Alex", "owner")
    sam = await add_person("Sam", "guest", standing="visiting")
    session.add(Account(person_id=sam.id, username="sam", password_hash="hash"))
    await add_conversation(sam)
    await forget.forget(
        session, sam, by=f"person:{sam.id}", turn_of=sam.id, now=NOW, finish_later=True
    )
    assert await _count(session, Account, Account.person_id == sam.id) == 1
    # Not before the longest turn is over.
    assert await forget.finish_the_forgotten(session, NOW + forget.FINISH_AFTER / 2) == 0
    assert await forget.finish_the_forgotten(session, NOW + forget.FINISH_AFTER) == 1
    assert await _count(session, Account, Account.person_id == sam.id) == 0
    assert await _count(session, Conversation, Conversation.person_id == sam.id) == 0


async def test_forgetting_oneself_is_never_an_idea_of_the_assistant(session_factory, monkeypatch):
    people = await _people(session_factory, ("Alex", "owner"), ("Sam", "guest"))
    sam = await _visiting(session_factory, people["Sam"])
    _judge(monkeypatch)
    ctx, reply_id, _ = await _turn(session_factory, sam)
    await handle_tool_call(mark(WHY), ctx=ctx, reply_id=reply_id)
    record = await handle_tool_call(call("forget_me"), ctx=ctx, reply_id=reply_id)
    assert record.status == "invalid"
    assert "record_forget" in json.dumps(record.model_result)
    assert "give reason" not in json.dumps(record.model_result)


# --- the rules of the code -----------------------------------------------------------------------


def test_every_column_that_names_a_record_says_what_forgetting_does_to_it():
    load_models()
    pointing = {
        f"{table.name}.{column.name}": column
        for table in Base.metadata.tables.values()
        for column in table.columns
        if any(key.column.table.name == "persons" for key in column.foreign_keys)
    }
    # A table added later that names a record is classified here, or this fails.
    assert set(forget.WHAT_FORGETTING_DOES) == set(pointing)
    for where, fate in forget.WHAT_FORGETTING_DOES.items():
        assert fate in ("deleted", "emptied", "kept"), where
        if fate == "emptied":
            assert pointing[where].nullable, where
    for where in (
        "persons.invited_by",
        "providers.given_by",
        "person_acts.person_id",
        "record_merges.kept_id",
        "record_merges.absorbed_id",
    ):
        assert forget.WHAT_FORGETTING_DOES[where] == "kept"


def test_forgetting_oneself_is_about_the_person_of_the_turn():
    turn = uuid.uuid4()
    action = DescribedAction(
        connector="core",
        action_type="people.forget_me",
        target=None,
        effect="self_profile",
        destination="household",
    )
    assert keys_of(action, None, turn) == (f"person:{turn}",)
    assert subject_of(action, None, turn).key == f"person:{turn}"


def test_the_forget_tools_are_in_the_registry():
    assert TOOLS["forget_me"].action_type == "people.forget_me"
    assert TOOLS["record_forget"].action_type == "people.forget"
    assert TOOLS["forget_me"].definition.parameters["properties"] == {}
    assert "reason" in TOOLS["record_forget"].definition.parameters["properties"]  # type: ignore[operator]


async def test_the_owner_s_deny_for_someone_survives_their_forgetting(session, add_person):
    """Alex wrote "Tom never opens the garage". Tom says "forget me": the rule stays, and holds
    again when Tom is known again under another record (the owner's answer of 2026-11-27)."""
    alex = await add_person("Alex", "owner")
    tom = a_record("Tom")
    session.add(tom)
    await session.flush()
    deny = PolicyRight(
        person_id=tom.id,
        action_type="note.leave",
        access="deny",
        summary="Tom never leaves notes.",
        owner_words="Tom never leaves notes",
        created_by=alex.id,
    )
    allow = PolicyRight(
        person_id=tom.id,
        action_type="device.list",
        access="allow",
        summary="Tom may list devices.",
        owner_words="he may",
        created_by=alex.id,
    )
    session.add_all([deny, allow])
    await session.flush()
    allow_id = allow.id
    await forget.forget(session, tom, by="domo", turn_of=None, now=NOW)
    kept = await session.get(PolicyRight, deny.id, populate_existing=True)
    assert kept is not None and kept.person_id == tom.id and kept.revoked_at is None
    assert await session.get(PolicyRight, allow_id, populate_existing=True) is None
    again = a_record("Tom")
    session.add(again)
    await session.flush()
    trace = await merge.merge(
        session, kept=tom, absorbed=again, by="domo", reason=WHY, turn_of=None, now=NOW
    )
    assert trace.kept_id == again.id
    await session.refresh(kept)
    assert kept.person_id == again.id
