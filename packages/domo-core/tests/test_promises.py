"""Access promised to someone the assistant does not know yet, kept the day he recognises them.

The owner says "give access to my partner and her children": the assistant notes the promise,
with the role and where they stand. Later, in anyone's turn, someone he recognises is opened as a
record and the promise is kept for them: the access is given through the one place that gives it
(``people.records.give_access``), so the owner's directives, the child mark and the yes-judge hold
as for any access. Nothing is asked again of the owner."""

import json
import uuid
from typing import Any

import pytest
from domo_core.accounts.models import Person
from domo_core.confirmations.judge import Judgement
from domo_core.confirmations.models import Confirmation
from domo_core.conversation.execution import handle_tool_call
from domo_core.llm.types import ToolCall
from domo_core.people import directives, made_roles, promises, records
from domo_core.people.models import AccessPromise, MadeRole, PersonAct
from sqlalchemy import func, select
from test_concerned_person import _judge, _people, _turn
from test_own_idea import LIVE, mark
from test_undone import action_of, facts

WHY = "Alex says his partner and her children are coming to live here"
LATER = "Dana opened Sam and Alex had promised her partner access"


def call(tool: str, **arguments: Any) -> ToolCall:
    return ToolCall(uuid.uuid4().hex[:8], tool, json.dumps(arguments))


async def run(ctx: Any, reply_id: uuid.UUID, *calls: ToolCall) -> Any:
    record = None
    for one in calls:
        record = await handle_tool_call(one, ctx=ctx, reply_id=reply_id)
    return record


async def known(session_factory: Any, *names: str, **values: Any) -> dict[str, Person]:
    """Records the assistant knows by name: no access, no account."""
    columns: dict[str, Any] = {
        "role": "guest",
        "state": "known",
        "standing": "visiting",
        "origin": "domo",
        "origin_reason": "Alex said they come",
    } | values
    async with session_factory() as writer:
        made = {name: Person(display_name=name, **columns) for name in names}
        writer.add_all(made.values())
        await writer.commit()
    return made


async def promise_row(session_factory: Any, **values: Any) -> AccessPromise:
    """A promise that waits, written as the assistant's own."""
    columns: dict[str, Any] = {
        "description": "Alex's partner and her children",
        "role": "adult",
        "standing": "lives_here",
        "asked_by": "domo",
        "is_order": False,
        "reason": WHY,
    } | values
    async with session_factory() as writer:
        row = AccessPromise(**columns)
        writer.add(row)
        await writer.commit()
        return row


async def promise_now(session_factory: Any, row: AccessPromise) -> AccessPromise:
    async with session_factory() as reader:
        found = await reader.get(AccessPromise, row.id)
        assert found is not None
        return found


async def person_now(session_factory: Any, person: Person) -> Person:
    async with session_factory() as reader:
        found = await reader.get(Person, person.id)
        assert found is not None
        return found


async def acts_of(session_factory: Any, *kinds: str) -> list[PersonAct]:
    async with session_factory() as reader:
        found = await reader.scalars(
            select(PersonAct).where(PersonAct.act.in_(kinds)).order_by(PersonAct.at, PersonAct.id)
        )
        return list(found)


async def count(session_factory: Any, model: Any) -> int:
    async with session_factory() as reader:
        return await reader.scalar(select(func.count()).select_from(model)) or 0


# --- the promise is kept the day the person is recognised ----------------------------------------


async def test_a_promise_is_kept_the_day_the_person_is_recognised_without_asking_again(
    session_factory, monkeypatch
):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"), grants=False)
    judged = _judge(monkeypatch)
    # The owner's own words: the promise is noted, nobody is asked to say yes.
    ctx, reply_id, _ = await _turn(
        session_factory, people["Alex"], said="Give access to my partner."
    )
    made = await run(
        ctx,
        reply_id,
        call(
            "access_promise",
            description="Alex's partner and her children",
            role="adult",
            standing="lives_here",
        ),
    )
    assert made.status == "done", made.model_result
    promise_id = made.model_result["id"]
    async with session_factory() as reader:
        row = await reader.get(AccessPromise, uuid.UUID(promise_id))
        assert row is not None
        assert (row.asked_by, row.is_order, row.role, row.standing) == (
            f"person:{people['Alex'].id}",
            True,
            "adult",
            "lives_here",
        )
    # Later, in Dana's turn, the assistant recognises Sam: he looks at what waits, opens the
    # record and keeps the promise as his own idea.
    dana_ctx, dana_reply, _ = await _turn(session_factory, people["Dana"], said="Sam is here.")
    seen = await run(dana_ctx, dana_reply, mark(LATER), call("promises_look"))
    assert seen.status == "done", seen.model_result
    assert [p["id"] for p in seen.model_result["promises"]] == [promise_id]
    opened = await run(
        dana_ctx,
        dana_reply,
        mark(LATER),
        call(
            "record_open",
            name="Sam",
            standing="lives_here",
            ground="gets_access",
            reason=LATER,
        ),
    )
    assert opened.status == "done", opened.model_result
    kept = await run(
        dana_ctx,
        dana_reply,
        mark(LATER),
        call("promise_keep", promise=promise_id, person="Sam", last=True, reason=LATER),
    )
    assert kept.status == "done", kept.model_result
    async with session_factory() as reader:
        sam = (await reader.scalars(select(Person).where(Person.display_name == "Sam"))).one()
        assert (sam.state, sam.role, sam.standing) == ("has_access", "adult", "lives_here")
        row = await reader.get(AccessPromise, uuid.UUID(promise_id))
        assert row is not None and row.kept_at is not None and row.kept_person_id == sam.id
    # The judge weighed it once (the assistant's own idea); the owner was asked nothing.
    assert len(judged.cases) == 1
    assert await count(session_factory, Confirmation) == 0
    [kept_act] = await acts_of(session_factory, "promise_kept")
    assert (kept_act.by, kept_act.reason, kept_act.person_id) == ("domo", LATER, sam.id)


async def test_a_promise_kept_in_the_turn_of_the_one_it_serves_says_so_to_the_judge(
    session_factory, monkeypatch
):
    people = await _people(session_factory, ("Alex", "owner"), ("Sam", "guest"), grants=False)
    sam = people["Sam"]
    promised = await promise_row(session_factory, role="adult")
    judged = _judge(monkeypatch, Judgement("do_not", reason="he only said who he is"))
    ctx, reply_id, _ = await _turn(session_factory, await person_now(session_factory, sam))
    refused = await run(
        ctx,
        reply_id,
        mark("Sam says he is the partner"),
        call(
            "promise_keep",
            promise=str(promised.id),
            person="Sam",
            last=True,
            reason="Sam says he is the partner",
        ),
    )
    assert refused.status == "refused"
    [case] = judged.cases
    assert case.action["facts"]["serves_the_speaker"] is True  # type: ignore[index]
    # A judge that says no keeps nothing: Sam only said who he is, and the promise still waits.
    assert (await person_now(session_factory, sam)).role == "guest"
    waiting = await promise_now(session_factory, promised)
    assert waiting.kept_at is None and waiting.kept_person_id is None
    assert await acts_of(session_factory, "promise_kept") == []


# --- the owner's withdrawal or refusal, before or after ------------------------------------------


async def test_a_promise_gives_nothing_to_someone_withdrawn_or_refused_before_or_after(
    session_factory, session, monkeypatch
):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"), grants=False)
    judged = _judge(monkeypatch)
    sam, tom = (await known(session_factory, "Sam", "Tom")).values()
    first = await promise_row(session_factory)
    second = await promise_row(session_factory, description="Alex's cousin")
    # Before: the owner withdraws Sam, then the promise is kept for him.
    async with session_factory() as writer:
        alex = await writer.get(Person, people["Alex"].id)
        sam_row = await writer.get(Person, sam.id)
        tom_row = await writer.get(Person, tom.id)
        assert alex and sam_row and tom_row
        await directives.set_directive(
            writer,
            kind="withdraw",
            person=sam_row,
            description=None,
            except_words=None,
            until=None,
            owner=alex,
            now=LIVE,
        )
        await writer.commit()
    ctx, reply_id, _ = await _turn(session_factory, people["Dana"])
    refused = await run(
        ctx,
        reply_id,
        mark(LATER),
        call("promise_keep", promise=str(first.id), person="Sam", last=True, reason=LATER),
    )
    assert refused.status == "refused" and refused.model_result["reason"] == "owner_directive"
    assert judged.cases == []
    assert (await person_now(session_factory, sam)).state == "withdrawn"
    async with session_factory() as writer:
        waiting = await writer.get(AccessPromise, first.id)
        sam_row = await writer.get(Person, sam.id)
        assert waiting and sam_row
        assert waiting.kept_at is None
        with pytest.raises(records.UnderDirective):
            await promises.keep(
                writer,
                waiting,
                sam_row,
                by="domo",
                reason=LATER,
                turn_of=None,
                now=LIVE,
            )
    # After: the promise is kept for Tom, then the owner refuses him.
    async with session_factory() as writer:
        waiting = await writer.get(AccessPromise, second.id)
        tom_row = await writer.get(Person, tom.id)
        assert waiting and tom_row
        await promises.keep(
            writer, waiting, tom_row, by="domo", reason=LATER, turn_of=None, now=LIVE
        )
        await promises.close(writer, waiting, now=LIVE)
        await writer.commit()
    assert (await person_now(session_factory, tom)).state == "has_access"
    async with session_factory() as writer:
        alex = await writer.get(Person, people["Alex"].id)
        tom_row = await writer.get(Person, tom.id)
        assert alex and tom_row
        await directives.set_directive(
            writer,
            kind="refuse",
            person=tom_row,
            description=None,
            except_words=None,
            until=None,
            owner=alex,
            now=LIVE,
        )
        await writer.commit()
    gone = await person_now(session_factory, tom)
    assert gone.state == "refused"
    async with session_factory() as writer:
        closed = await writer.get(AccessPromise, second.id)
        tom_row = await writer.get(Person, tom.id)
        assert closed and tom_row
        # A promise ends once: the closed one stays as it is, only Tom's access was taken back.
        assert closed.kept_at is not None and closed.dropped_at is None
        # Nothing gives him an access again, a promise included: the one place that gives it
        # refuses.
        with pytest.raises(records.UnderDirective):
            await records.give_access(
                writer,
                tom_row,
                role="adult",
                made_role=None,
                standing=None,
                guest_days=None,
                by="domo",
                by_owner=False,
                reason=LATER,
                turn_of=None,
                now=LIVE,
            )
        with pytest.raises(promises.PromiseOver):
            await promises.keep(
                writer, closed, tom_row, by="domo", reason=LATER, turn_of=None, now=LIVE
            )


# --- who asked, and what the owner's rules stop --------------------------------------------------


async def test_only_the_owners_promise_is_an_order(session_factory, monkeypatch):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"), grants=False)
    _judge(monkeypatch)
    alex_ctx, alex_reply, _ = await _turn(
        session_factory, people["Alex"], said="Give access to my cousin."
    )
    owners = await run(
        alex_ctx,
        alex_reply,
        call("access_promise", description="Alex's cousin", role="guest", standing="visiting"),
    )
    assert owners.status == "done", owners.model_result
    dana_ctx, dana_reply, _ = await _turn(
        session_factory, people["Dana"], said="Alex's brother will come."
    )
    # Dana's own request is the rights' to refuse: the gesture is in no role.
    own = await run(
        dana_ctx,
        dana_reply,
        call("access_promise", description="Alex's brother", role="guest", standing="visiting"),
    )
    assert own.status == "refused" and own.model_result["reason"] == "not_allowed"
    # Her words as the assistant's idea: a promise of his, never an order.
    hers = await run(
        dana_ctx,
        dana_reply,
        mark(LATER),
        call(
            "access_promise",
            description="Alex's brother",
            role="guest",
            standing="visiting",
            reason=LATER,
        ),
    )
    assert hers.status == "done", hers.model_result
    async with session_factory() as reader:
        rows = {row.description: row for row in await reader.scalars(select(AccessPromise))}
    assert rows["Alex's cousin"].is_order is True
    assert rows["Alex's brother"].is_order is False
    assert rows["Alex's brother"].asked_by == "domo"
    # Both are kept by the assistant's own gesture; while the owner wants nobody new, neither
    # passes.
    cousin, brother = (await known(session_factory, "Sam", "Tom")).values()
    async with session_factory() as writer:
        alex = await writer.get(Person, people["Alex"].id)
        assert alex is not None
        await directives.set_directive(
            writer,
            kind="no_new_people",
            person=None,
            description="no one new for now",
            except_words=None,
            until=None,
            owner=alex,
            now=LIVE,
        )
        await writer.commit()
    for description, person in (("Alex's cousin", cousin), ("Alex's brother", brother)):
        refused = await run(
            dana_ctx,
            dana_reply,
            mark(LATER),
            call(
                "promise_keep",
                promise=str(rows[description].id),
                person=person.display_name,
                last=True,
                reason=LATER,
            ),
        )
        assert refused.status == "refused", description
        assert refused.model_result["reason"] == "owner_directive"
        assert (await person_now(session_factory, person)).state == "known"


# --- one promise, several people -----------------------------------------------------------------


async def test_a_promise_kept_for_two_children_closes_once(session_factory, monkeypatch):
    people = await _people(session_factory, ("Alex", "owner"), grants=False)
    _judge(monkeypatch)
    paul, tom = (await known(session_factory, "Paul", "Tom", role="child")).values()
    promised = await promise_row(
        session_factory, description="her children", role="child", standing="lives_here"
    )
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"], on_his_own=True)
    first = await run(
        ctx,
        reply_id,
        mark(LATER),
        call("promise_keep", promise=str(promised.id), person="Paul", reason=LATER),
    )
    assert first.status == "done", first.model_result
    # Not the last: the promise still waits for the other child.
    waiting = await promise_now(session_factory, promised)
    assert waiting.kept_at is None
    async with session_factory() as reader:
        assert [p.id for p in await promises.waiting(reader)] == [promised.id]
    second = await run(
        ctx,
        reply_id,
        mark(LATER),
        call("promise_keep", promise=str(promised.id), person="Tom", last=True, reason=LATER),
    )
    assert second.status == "done", second.model_result
    closed = await promise_now(session_factory, promised)
    assert closed.kept_at is not None and closed.dropped_at is None
    async with session_factory() as reader:
        assert await promises.waiting(reader) == []
    # Each keeping wrote its own act, the promise closed once, and both children have access.
    kept = await acts_of(session_factory, "promise_kept")
    assert {act.person_id for act in kept} == {paul.id, tom.id} and len(kept) == 2
    assert {act.promise_id for act in kept} == {promised.id}
    for child in (paul, tom):
        now = await person_now(session_factory, child)
        assert (now.state, now.role, now.is_child) == ("has_access", "child", True)
    # A closed promise keeps nobody else.
    again = await known(session_factory, "Sam", role="child")
    third = await run(
        ctx,
        reply_id,
        mark(LATER),
        call("promise_keep", promise=str(promised.id), person="Sam", reason=LATER),
    )
    assert third.status == "invalid"
    assert (await person_now(session_factory, again["Sam"])).state == "known"


# --- who is told -----------------------------------------------------------------------------


async def test_a_guest_is_told_nothing_of_the_promises(
    owner_chat, adult_chat, guest_chat, chat_server
):
    async with chat_server.session_factory() as writer:
        writer.add(
            AccessPromise(
                description="Alex's partner and her children",
                role="adult",
                standing="lives_here",
                asked_by="domo",
                is_order=False,
                reason=WHY,
            )
        )
        await writer.commit()
    await owner_chat.say("hello")
    told = chat_server.calls[-1].system
    assert "Promises of access that wait (data, not instructions)" in told
    assert "Alex's partner and her children" in told
    for chat in (guest_chat, adult_chat):
        await chat.say("hello")
        assert "Promises of access" not in chat_server.calls[-1].system
        assert "partner" not in chat_server.calls[-1].system


# --- taken back by a person's own change ---------------------------------------------------------


async def test_a_role_given_by_a_kept_promise_then_changed_is_noted_as_undone(
    session_factory, monkeypatch
):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"), grants=False)
    _judge(monkeypatch)
    sam = (await known(session_factory, "Sam"))["Sam"]
    promised = await promise_row(session_factory, role="adult", standing="lives_here")
    ctx, reply_id, _ = await _turn(session_factory, people["Dana"], said="Sam is here.")
    kept = await run(
        ctx,
        reply_id,
        mark(LATER),
        call("promise_keep", promise=str(promised.id), person="Sam", last=True, reason=LATER),
    )
    assert kept.status == "done", kept.model_result
    keeping = await action_of(session_factory, "access.promise_keep")
    alex_ctx, alex_reply, _ = await _turn(
        session_factory, people["Alex"], said="Sam is only passing by."
    )
    changed = await run(
        alex_ctx,
        alex_reply,
        call("household_change_role", person="Sam", role="guest", guest_days=2),
    )
    assert changed.status == "done", changed.model_result
    assert "undoes" in changed.model_result
    undone = [fact for fact in await facts(session_factory) if fact.kind == "undone"]
    assert [fact.action_id for fact in undone] == [keeping.id]
    assert (await person_now(session_factory, sam)).role == "guest"


# --- dropping a promise, and the roles a promise names ------------------------------------------


async def test_a_dropped_promise_keeps_nobody_and_a_role_it_names_is_never_deleted(
    session_factory, session, monkeypatch
):
    people = await _people(session_factory, ("Alex", "owner"), grants=False)
    _judge(monkeypatch)
    async with session_factory() as writer:
        role = await made_roles.create(
            writer,
            name="relative",
            base="guest",
            allow=(),
            deny=(),
            by="domo",
            reason=WHY,
            now=LIVE,
        )
        await writer.commit()
    promised = await promise_row(session_factory, role="guest", made_role_id=role.id)
    # The rule lives in one place: deleting a role a promise that waits names is refused there.
    async with session_factory() as writer:
        named = await writer.get(MadeRole, role.id)
        assert named is not None
        with pytest.raises(made_roles.RoleInUse):
            await made_roles.delete(writer, named, by="domo", reason=WHY, now=LIVE)
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"], said="Never mind her.")
    dropped = await run(ctx, reply_id, call("promise_drop", promise=str(promised.id)))
    assert dropped.status == "done", dropped.model_result
    row = await promise_now(session_factory, promised)
    assert row.dropped_at is not None and row.dropped_by == f"person:{people['Alex'].id}"
    async with session_factory() as reader:
        assert await promises.waiting(reader) == []
    [act] = await acts_of(session_factory, "promise_dropped")
    assert act.promise_id == promised.id
    # Nothing waits for the role any more: it may go.
    async with session_factory() as writer:
        named = await writer.get(MadeRole, role.id)
        assert named is not None
        await made_roles.delete(writer, named, by="domo", reason=WHY, now=LIVE)
        assert named.deleted_at is not None


# --- the household's rules on a promised stay, and on someone who already has access ------------


async def test_a_promised_stay_of_any_length_is_kept_unless_the_owner_limited_it(
    session_factory, monkeypatch, grant
):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"), grants=False)
    judged = _judge(monkeypatch)
    sam, tom = (await known(session_factory, "Sam", "Tom")).values()
    long_stay = await promise_row(session_factory, role="guest", guest_days=90)
    short_stay = await promise_row(session_factory, role="guest", guest_days=5)
    ctx, reply_id, _ = await _turn(session_factory, people["Dana"])
    # No length is refused by the code (audit POL-5): a long stay is judged as his own idea.
    kept = await run(
        ctx,
        reply_id,
        mark(LATER),
        call("promise_keep", promise=str(long_stay.id), person="Sam", last=True, reason=LATER),
    )
    assert kept.status == "done", kept.model_result
    assert len(judged.cases) == 1
    assert (await person_now(session_factory, sam)).state == "has_access"
    # A limit the owner wrote himself on the days refuses before any judgement.
    await grant(
        "access.promise_keep",
        for_domo=True,
        access="allow",
        limit='action.facts.days != null && action.facts.days > 3 ? "refuse" : "allow"',
    )
    refused = await run(
        ctx,
        reply_id,
        mark(LATER),
        call("promise_keep", promise=str(short_stay.id), person="Tom", last=True, reason=LATER),
    )
    assert refused.status == "refused", refused.model_result
    assert len(judged.cases) == 1
    assert (await person_now(session_factory, tom)).state == "known"
    assert (await promise_now(session_factory, short_stay)).kept_at is None


async def test_no_new_people_does_not_stop_a_role_change_by_a_promise_but_a_withdrawal_does(
    session_factory, monkeypatch
):
    people = await _people(
        session_factory, ("Alex", "owner"), ("Dana", "adult"), ("Sam", "guest"), grants=False
    )
    _judge(monkeypatch)
    sam = people["Sam"]
    tom = (await known(session_factory, "Tom"))["Tom"]
    promised = await promise_row(session_factory, role="adult")
    async with session_factory() as writer:
        alex = await writer.get(Person, people["Alex"].id)
        row = await writer.get(Person, sam.id)
        assert alex is not None and row is not None
        row.first_access_at = LIVE  # let in before: not someone new
        await directives.set_directive(
            writer,
            kind="no_new_people",
            person=None,
            description="no one new",
            except_words=None,
            until=None,
            owner=alex,
            now=LIVE,
        )
        await writer.commit()
    ctx, reply_id, _ = await _turn(session_factory, people["Dana"])
    changed = await run(
        ctx,
        reply_id,
        mark(LATER),
        call("promise_keep", promise=str(promised.id), person="Sam", last=True, reason=LATER),
    )
    # Someone who has access already is not someone new: it is a change of role.
    assert changed.status == "done", changed.model_result
    assert (await person_now(session_factory, sam)).role == "adult"
    # Withdrawn or refused, the same call is stopped.
    other = await promise_row(session_factory, role="adult")
    async with session_factory() as writer:
        alex = await writer.get(Person, people["Alex"].id)
        row = await writer.get(Person, tom.id)
        assert alex and row
        await directives.set_directive(
            writer,
            kind="withdraw",
            person=row,
            description=None,
            except_words=None,
            until=None,
            owner=alex,
            now=LIVE,
        )
        await writer.commit()
    stopped = await run(
        ctx,
        reply_id,
        mark(LATER),
        call("promise_keep", promise=str(other.id), person="Tom", last=True, reason=LATER),
    )
    assert stopped.status == "refused" and stopped.model_result["reason"] == "owner_directive"


async def test_a_withdrawal_leaves_a_closed_promise_closed_and_an_open_one_open(session_factory):
    people = await _people(session_factory, ("Alex", "owner"), grants=False)
    paul, tom = (await known(session_factory, "Paul", "Tom", role="child")).values()
    open_one = await promise_row(session_factory, role="child")
    closed_one = await promise_row(session_factory, role="child", description="her cousin")
    async with session_factory() as writer:
        first = await writer.get(AccessPromise, open_one.id)
        second = await writer.get(AccessPromise, closed_one.id)
        paul_row = await writer.get(Person, paul.id)
        tom_row = await writer.get(Person, tom.id)
        assert first and second and paul_row and tom_row
        await promises.keep(
            writer, first, paul_row, by="domo", reason=LATER, turn_of=None, now=LIVE
        )
        await promises.keep(
            writer, second, tom_row, by="domo", reason=LATER, turn_of=None, now=LIVE
        )
        await promises.close(writer, second, now=LIVE)
        alex = await writer.get(Person, people["Alex"].id)
        assert alex is not None
        for withdrawn in (paul_row, tom_row):
            await directives.set_directive(
                writer,
                kind="withdraw",
                person=withdrawn,
                description=None,
                except_words=None,
                until=None,
                owner=alex,
                now=LIVE,
            )
        await writer.commit()
    # Still waiting for the other children; the access of each was taken back.
    still = await promise_now(session_factory, open_one)
    assert still.kept_at is None and still.dropped_at is None
    ended = await promise_now(session_factory, closed_one)
    assert ended.kept_at is not None and ended.dropped_at is None
    for child in (paul, tom):
        assert (await person_now(session_factory, child)).state == "withdrawn"
