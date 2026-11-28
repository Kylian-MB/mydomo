"""The assistant opens, completes, looks at and erases the record of a person, each act with its
reason, and never a program: a record gives nothing by itself, saying who one is gives nothing
either, and every act of his leaves its trace (``person_acts``) and an audit line without any
free text."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from domo_core.accounts.actors import actor_of, is_child
from domo_core.accounts.models import Account, Person
from domo_core.audit.models import AuditEntry
from domo_core.chat.models import MessageAction
from domo_core.confirmations.judge import Judgement, fallback
from domo_core.confirmations.models import Confirmation
from domo_core.conversation.execution import handle_tool_call
from domo_core.household.nicknames import PersonNickname
from domo_core.llm.types import ToolCall
from domo_core.madetools.asks import ActAsk, read_ask
from domo_core.madetools.program_actions import PROGRAM_HANDLERS
from domo_core.memory.models import Memory
from domo_core.people import records, whose
from domo_core.people.acts import why
from domo_core.people.directives import set_directive, under_directive
from domo_core.people.models import PersonAct, PersonLink
from domo_core.policy import open_catalog
from domo_core.policy.catalog import (
    PEOPLE_GESTURES,
    PROGRAM_ACTIONS,
    PROGRAM_REQUESTS,
    PROGRAM_TYPES,
    OpenKind,
)
from domo_core.policy.models import PolicyRight
from domo_core.policy.roles import Actor, domo_actor
from domo_core.policy.settings import (
    InvalidSetting,
    load_policy_settings,
    update_policy_setting,
)
from domo_core.tools.context import InvalidArguments
from domo_core.tools.registry import TOOLS
from domo_core.trust.models import TrustFact
from fakes import FakeAsk, FakeRefused
from sqlalchemy import func, select
from test_concerned_person import _facts, _judge, _people, _turn
from test_madetools_handover import BEGIN, add_tool, by_program, deps_of
from test_madetools_means import launched
from test_own_idea import LIVE, mark

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
WHY = "Paul says his friend Tom comes on Saturday"


def call(name: str, arguments: dict[str, Any]) -> ToolCall:
    return ToolCall("c", name, json.dumps(arguments))


def open_tom(**more: Any) -> ToolCall:
    return call(
        "record_open",
        {"name": "Tom", "standing": "visiting", "ground": "comes", **more},
    )


async def _persons(session_factory: Any, name: str) -> list[Person]:
    async with session_factory() as reader:
        return list(await reader.scalars(select(Person).where(Person.display_name == name)))


async def _acts(session_factory: Any) -> list[PersonAct]:
    async with session_factory() as reader:
        return list(await reader.scalars(select(PersonAct).order_by(PersonAct.at, PersonAct.id)))


async def _count(session_factory: Any, table: Any) -> int:
    async with session_factory() as reader:
        return await reader.scalar(select(func.count()).select_from(table)) or 0


async def _audit(session_factory: Any, action: str) -> list[AuditEntry]:
    async with session_factory() as reader:
        found = await reader.scalars(
            select(AuditEntry).where(AuditEntry.action == action).order_by(AuditEntry.id)
        )
        return list(found)


# --- opening a record ----------------------------------------------------------------------------


async def test_a_child_talking_of_a_friend_lets_the_assistant_open_a_record_and_no_access(
    session_factory, monkeypatch
):
    people = await _people(session_factory, ("Alex", "owner"), ("Paul", "child"))
    paul = people["Paul"]
    judged = _judge(monkeypatch)
    ctx, reply_id, _ = await _turn(session_factory, paul)
    await handle_tool_call(mark(WHY), ctx=ctx, reply_id=reply_id)
    record = await handle_tool_call(open_tom(is_child=True, reason=WHY), ctx=ctx, reply_id=reply_id)
    assert record.status == "done"
    [tom] = await _persons(session_factory, "Tom")
    assert (tom.state, tom.origin, tom.origin_reason) == ("known", "domo", WHY)
    assert (tom.standing, tom.is_child, tom.first_access_at) == ("visiting", True, None)
    # Silent: the judge is not asked, nothing waits for anyone's yes.
    assert judged.cases == []
    assert await _count(session_factory, Confirmation) == 0
    [decision] = [
        entry
        for entry in await _audit(session_factory, "policy.decision")
        if entry.details["action_type"] == "people.open"
    ]
    assert decision.actor == "domo"
    async with session_factory() as reader:
        stored = await reader.scalar(
            select(MessageAction).where(MessageAction.action_type == "people.open")
        )
    assert stored is not None and (stored.by_domo, stored.idea_reason) == (True, WHY)
    [act] = [a for a in await _acts(session_factory) if a.act == "opened"]
    assert (act.by, act.turn_of, act.person_id, act.reason) == ("domo", paul.id, tom.id, WHY)


def test_his_own_act_without_a_reason_is_invalid_and_a_reason_without_the_mark_too():
    domo, person = domo_actor(), Actor(uuid.uuid4(), "adult")
    whose.check({"reason": "Paul says so"}, domo)
    whose.check({}, person)
    for arguments in ({}, {"reason": "  "}, {"reason": 3}):
        with pytest.raises(InvalidArguments) as refused:
            whose.check(arguments, domo)
        assert refused.value.code == "invalid"
    with pytest.raises(InvalidArguments) as refused:
        whose.check({"reason": "Paul says so"}, person)
    assert refused.value.code == "invalid"
    assert refused.value.detail is not None and "act_on_my_own" in refused.value.detail
    with pytest.raises(InvalidArguments) as refused:
        whose.check({"reason": "x" * (whose.MAX_REASON_LENGTH + 1)}, domo)
    assert refused.value.code == "too_long"


async def test_paul_asking_for_it_himself_is_refused(session_factory, monkeypatch):
    people = await _people(session_factory, ("Alex", "owner"), ("Paul", "child"))
    _judge(monkeypatch)
    ctx, reply_id, _ = await _turn(session_factory, people["Paul"])
    record = await handle_tool_call(open_tom(is_child=True), ctx=ctx, reply_id=reply_id)
    assert record.status == "refused"
    assert record.model_result["reason"] == "not_allowed"
    assert await _persons(session_factory, "Tom") == []


async def test_a_record_needs_a_ground(session_factory, monkeypatch):
    people = await _people(session_factory, ("Alex", "owner"))
    _judge(monkeypatch)
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    tool = TOOLS["record_open"]
    without = {"name": "Tom", "standing": "visiting"}
    for arguments in (without, {**without, "ground": "mentioned"}):
        with pytest.raises(InvalidArguments) as refused:
            await tool.describe(arguments, ctx.actor, ctx)
        assert refused.value.code == "invalid"
        record = await handle_tool_call(call("record_open", arguments), ctx=ctx, reply_id=reply_id)
        assert record.status == "invalid"
    assert await _persons(session_factory, "Tom") == []
    description = tool.definition.description
    assert "only came up in passing" in description and "stays a memory" in description


async def test_two_records_may_have_the_same_name(session_factory, monkeypatch):
    people = await _people(session_factory, ("Alex", "owner"))
    _judge(monkeypatch)
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    for _ in range(2):
        record = await handle_tool_call(open_tom(), ctx=ctx, reply_id=reply_id)
        assert record.status == "done"
    first, second = await _persons(session_factory, "Tom")
    # Nobody is picked: the name means both, and the refusal tells them apart.
    ask = call("record_complete", {"person": "Tom", "standing": "lives_here"})
    refused = await handle_tool_call(ask, ctx=ctx, reply_id=reply_id)
    assert refused.status == "invalid" and refused.model_result["reason"] == "ambiguous"
    detail = str(refused.model_result["detail"])
    assert f"person:{first.id}" in detail and f"person:{second.id}" in detail
    by_id = call("record_complete", {"person": f"person:{second.id}", "standing": "lives_here"})
    assert (await handle_tool_call(by_id, ctx=ctx, reply_id=reply_id)).status == "done"
    standings = {p.id: p.standing for p in await _persons(session_factory, "Tom")}
    assert standings == {first.id: "visiting", second.id: "lives_here"}


# --- saying who one is gives nothing -------------------------------------------------------------


async def test_saying_who_one_is_gives_nothing_by_itself(session_factory, monkeypatch):
    people = await _people(session_factory, ("Alex", "owner"), ("Sam", "guest"))
    sam = people["Sam"]
    judged = _judge(
        monkeypatch,
        Judgement("do_not", reason="not now", noted="Sam only said who he is"),
    )
    ctx, reply_id, _ = await _turn(session_factory, sam)
    become_adult = call("household_change_role", {"person": "Sam", "role": "adult"})
    # The assistant's own call carries his reason (``people.whose``).
    his_idea = call(
        "household_change_role", {"person": "Sam", "role": "adult", "reason": "Sam says he is 30"}
    )

    async def role() -> str:
        async with session_factory() as reader:
            return (await reader.get(Person, sam.id)).role  # type: ignore[union-attr]

    # Pin 1: Sam's own call, not marked: the rights refuse, no judge is asked.
    own = await handle_tool_call(become_adult, ctx=ctx, reply_id=reply_id)
    assert own.status in ("refused", "invalid") and judged.cases == []
    assert await role() == "guest"
    # Pin 2: the marked call reaches the judge with what it must weigh, and a no changes nothing.
    await handle_tool_call(mark(), ctx=ctx, reply_id=reply_id)
    marked = await handle_tool_call(his_idea, ctx=ctx, reply_id=reply_id)
    assert marked.status == "refused"
    [case] = judged.cases
    assert case.action["facts"]["serves_the_speaker"] is True  # type: ignore[index]
    idea = case.as_json()["idea"]
    assert idea["born_of_words_of"] == "Sam" and idea["they_lack_the_right"] is True
    assert await role() == "guest"
    [fact] = await _facts(session_factory)
    assert (fact.kind, fact.action_type, fact.of_domo) == (
        "judge_refused",
        "household.change_role",
        True,
    )
    assert fact.subject_key == f"person:{sam.id}"
    # Pin 3: a judge that stays silent twice: the idea is refused, nothing waits.
    silent = _judge(monkeypatch, fallback("no_model", they_asked=False))
    await handle_tool_call(mark(), ctx=ctx, reply_id=reply_id)
    quiet = await handle_tool_call(his_idea, ctx=ctx, reply_id=reply_id)
    assert len(silent.cases) == 2
    assert quiet.status == "refused" and quiet.model_result["reason"] == "judge_silent"
    assert await _count(session_factory, Confirmation) == 0
    assert await role() == "guest"


async def test_someone_else_s_record_is_not_the_speaker_s(session_factory, monkeypatch):
    people = await _people(session_factory, ("Alex", "owner"), ("Sam", "guest"), ("Dana", "adult"))
    judged = _judge(monkeypatch, Judgement("do_not", reason="not now"))
    ctx, reply_id, _ = await _turn(session_factory, people["Sam"])
    await handle_tool_call(mark(), ctx=ctx, reply_id=reply_id)
    await handle_tool_call(
        call(
            "household_change_role",
            {"person": "Dana", "role": "guest", "guest_days": 3, "reason": "Dana leaves soon"},
        ),
        ctx=ctx,
        reply_id=reply_id,
    )
    assert judged.cases[0].action["facts"]["serves_the_speaker"] is False  # type: ignore[index]


# --- the trace -----------------------------------------------------------------------------------


async def test_every_act_leaves_its_reason_and_an_audit_line_without_it(session, add_person):
    alex = await add_person("Alex", "owner")
    tom = await records.open_record(
        session,
        name="Tomás",
        standing="visiting",
        is_child=False,
        by="domo",
        reason=WHY,
        turn_of=alex.id,
        now=NOW,
    )
    [act] = await why(session, tom.id)
    assert (act.act, act.reason, act.by, act.turn_of) == ("opened", WHY, "domo", alex.id)
    [line] = (
        await session.scalars(select(AuditEntry).where(AuditEntry.action == "people.opened"))
    ).all()
    assert line.actor == "domo"
    assert line.details == {"act_id": str(act.id), "person_id": str(tom.id)}
    assert WHY not in json.dumps(line.details) and "Tomás" not in json.dumps(line.details)
    await records.complete(
        session,
        tom,
        standing="lives_here",
        by="domo",
        reason="he moved in",
        turn_of=None,
        now=NOW + timedelta(minutes=1),
    )
    assert [a.act for a in await why(session, tom.id)] == ["opened", "completed"]


async def test_the_owner_can_ask_why(session_factory, monkeypatch):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"))
    _judge(monkeypatch)
    async with session_factory() as writer:
        tom = await records.open_record(
            writer,
            name="Tom",
            standing="visiting",
            is_child=False,
            by="domo",
            reason=WHY,
            turn_of=people["Alex"].id,
            now=NOW,
        )
        await writer.commit()
    look = call("record_look", {"person": "Tom"})
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    record = await handle_tool_call(look, ctx=ctx, reply_id=reply_id)
    assert record.status == "done"
    shown = record.model_result
    assert (shown["id"], shown["state"], shown["origin"]) == (str(tom.id), "known", "domo")
    assert shown["origin_reason"] == WHY
    [told] = shown["acts"]  # type: ignore[misc]
    assert (told["act"], told["by"], told["reason"]) == ("opened", "domo", WHY)  # type: ignore[index]
    dana_ctx, dana_reply, _ = await _turn(session_factory, people["Dana"])
    refused = await handle_tool_call(look, ctx=dana_ctx, reply_id=dana_reply)
    assert refused.status != "done" and "acts" not in refused.model_result


async def test_in_an_adult_s_turn_the_duplicate_check_is_record_look_after_the_mark(
    session_factory, monkeypatch
):
    """What ``household_list`` and ``record_look`` say of a marked look: in someone else's turn the
    list is not his (it changes nothing, so it leaves the mark) and shows only who has access; the
    record is seen with ``record_look``, which takes the mark."""
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"))
    _judge(monkeypatch)
    async with session_factory() as writer:
        await records.open_record(
            writer,
            name="Tom",
            standing="visiting",
            is_child=False,
            by="domo",
            reason=WHY,
            turn_of=people["Alex"].id,
            now=NOW,
        )
        await writer.commit()
    ctx, reply_id, _ = await _turn(session_factory, people["Dana"])
    await handle_tool_call(mark(WHY), ctx=ctx, reply_id=reply_id)
    listed = await handle_tool_call(call("household_list", {}), ctx=ctx, reply_id=reply_id)
    assert listed.status == "done"
    assert "Tom" not in json.dumps(listed.model_result)
    assert ctx.own_idea is not None
    looked = await handle_tool_call(
        call("record_look", {"person": "Tom"}), ctx=ctx, reply_id=reply_id
    )
    assert looked.status == "done" and looked.model_result["state"] == "known"
    assert ctx.own_idea is None


async def test_looking_at_oneself_shows_nothing_someone_else_confided(session_factory, monkeypatch):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"), ("Sam", "guest"))
    sam, dana = people["Sam"], people["Dana"]
    _judge(monkeypatch)
    async with session_factory() as writer:
        writer.add_all(
            [
                Memory(
                    subject="Sam",
                    content="Sam is scared of dogs",
                    kind="fact",
                    origin="others_words",
                    about_id=sam.id,
                    said_by_id=dana.id,
                    kept_private=True,
                ),
                Memory(
                    subject="Sam",
                    content="Sam likes tea",
                    kind="preference",
                    origin="own_words",
                    about_id=sam.id,
                    said_by_id=sam.id,
                ),
            ]
        )
        writer.add(
            PersonLink(from_id=sam.id, to_id=dana.id, relation="nephew of", said_by_id=dana.id)
        )
        writer.add(
            PersonLink(
                from_id=dana.id,
                to_id=sam.id,
                relation="aunt of",
                said_by_id=dana.id,
                confirmed_at=NOW,
            )
        )
        await writer.commit()
    ctx, reply_id, _ = await _turn(session_factory, sam)
    record = await handle_tool_call(call("record_look", {}), ctx=ctx, reply_id=reply_id)
    assert record.status == "done"
    shown = json.dumps(record.model_result)
    assert "scared of dogs" not in shown and "likes tea" in shown
    # The link Dana said and Sam did not confirm stays hers; the one he confirmed is his too.
    assert "nephew of" not in shown and "aunt of" in shown


# --- completing ----------------------------------------------------------------------------------


async def _tom(session, alex, *, is_child=True, **more):
    return await records.open_record(
        session,
        name="Tom",
        standing="visiting",
        is_child=is_child,
        by="domo",
        reason=WHY,
        turn_of=alex.id,
        now=NOW - timedelta(hours=1),
        **more,
    )


async def test_the_mark_is_lifted_by_the_owner_or_by_a_known_age(session, add_person):
    alex = await add_person("Alex", "owner")
    tom = await _tom(session, alex)
    lift = {
        "is_child": False,
        "by": "domo",
        "reason": "he turned 19",
        "turn_of": alex.id,
        "now": NOW,
    }
    with pytest.raises(records.NotPossible) as refused:
        await records.complete(session, tom, **lift)
    assert refused.value.code == "no_birth_date"
    young = NOW.date().replace(year=NOW.year - 15)
    with pytest.raises(records.NotPossible) as refused:
        await records.complete(session, tom, born_on=young, **lift)
    assert refused.value.code == "too_young"
    assert tom.is_child
    # Never from the child's own word: given in the turn of the child it is about, the date is
    # kept as what they said, never the date the mark is lifted on.
    own_turn = {**lift, "turn_of": tom.id}
    adult = NOW.date().replace(year=NOW.year - 19)
    with pytest.raises(records.NotPossible) as refused:
        await records.complete(session, tom, born_on=adult, **own_turn)
    assert refused.value.code == "no_birth_date"
    assert tom.born_on is None
    said = {key: value for key, value in own_turn.items() if key != "is_child"}
    await records.complete(session, tom, born_on=adult, **said)
    assert (tom.is_child, tom.born_on) == (True, None)
    [word] = await session.scalars(select(Memory).where(Memory.about_id == tom.id))
    assert (word.said_by_id, word.origin) == (tom.id, "own_words")
    assert adult.isoformat() in word.content
    await records.complete(session, tom, born_on=adult, **lift)
    assert (tom.is_child, tom.born_on) == (False, adult)
    # The owner lifts it whatever the age; the setting moves the age of the assistant's own.
    paul = await _tom(session, alex)
    with pytest.raises(records.NotPossible):
        await records.complete(
            session,
            paul,
            is_child=False,
            by=f"person:{uuid.uuid4()}",
            reason=None,
            turn_of=None,
            now=NOW,
        )
    await records.complete(
        session, paul, is_child=False, by=f"person:{alex.id}", reason=None, turn_of=alex.id, now=NOW
    )
    assert not paul.is_child


async def test_the_age_of_a_grown_up_is_the_owner_s_to_set(session, add_person):
    alex = await add_person("Alex", "owner")
    assert (await load_policy_settings(session)).adult_age == 18
    await update_policy_setting(session, key="people.adult_age", value=21, by=alex.id, now=NOW)
    assert (await load_policy_settings(session)).adult_age == 21
    tom = await _tom(session, alex)
    nearly = NOW.date().replace(year=NOW.year - 20)
    with pytest.raises(records.NotPossible) as refused:
        await records.complete(
            session,
            tom,
            is_child=False,
            born_on=nearly,
            by="domo",
            reason="x",
            turn_of=None,
            now=NOW,
        )
    assert refused.value.code == "too_young"


async def test_the_owner_never_carries_the_mark_of_a_child(session, add_person):
    """The assistant hears "Alex is Sam's son": the mark is never set on Alex, and a mark left on
    the owner's row by any other path gives him no child's protections."""
    alex = await add_person("Alex", "owner")
    with pytest.raises(records.NotPossible) as refused:
        await records.complete(
            session,
            alex,
            is_child=True,
            by="domo",
            reason="Alex is Sam's son",
            turn_of=None,
            now=NOW,
        )
    assert refused.value.code == "owner_no_mark"
    assert not alex.is_child
    alex.is_child = True
    assert not is_child(alex)
    assert not actor_of(alex).child


async def test_the_age_of_a_grown_up_is_whatever_the_owner_gives(session, add_person):
    """Alex: "here one is a grown-up at 15" (or at 25): accepted, any positive whole number."""
    alex = await add_person("Alex", "owner")
    for age in (15, 25):
        await update_policy_setting(session, key="people.adult_age", value=age, by=alex.id, now=NOW)
        assert (await load_policy_settings(session)).adult_age == age
    for wrong in (0, -3, "fifteen", True):
        with pytest.raises(InvalidSetting):
            await update_policy_setting(
                session, key="people.adult_age", value=wrong, by=alex.id, now=NOW
            )


async def test_the_built_in_child_role_keeps_its_mark(session, add_person):
    alex = await add_person("Alex", "owner")
    paul = await add_person("Paul", "child")
    with pytest.raises(records.NotPossible) as refused:
        await records.complete(
            session,
            paul,
            is_child=False,
            by=f"person:{alex.id}",
            reason=None,
            turn_of=alex.id,
            now=NOW,
        )
    assert refused.value.code == "child_role"
    assert paul.is_child


# --- giving and taking an access -----------------------------------------------------------------


async def test_giving_access_is_the_one_place_a_record_gets_it(session, add_person):
    alex = await add_person("Alex", "owner")
    tom = await _tom(session, alex)
    assert tom.state == "known"
    given = {"role": "guest", "made_role": None, "standing": None, "guest_days": 3, "turn_of": None}
    await records.give_access(
        session, tom, by="domo", by_owner=False, reason="he arrives", now=NOW, **given
    )
    assert (tom.state, tom.first_access_at, tom.expires_at) == (
        "has_access",
        NOW,
        NOW + timedelta(days=3),
    )
    # The mark of a child never goes with a role.
    assert tom.is_child
    await records.give_access(
        session,
        tom,
        **{**given, "role": "adult", "guest_days": None},
        by="domo",
        by_owner=False,
        reason="more",
        now=NOW + timedelta(days=1),
    )
    assert (tom.role, tom.expires_at, tom.first_access_at, tom.is_child) == (
        "adult",
        None,
        NOW,
        True,
    )
    assert [a.act for a in await why(session, tom.id)] == ["opened", "access_given", "access_given"]


async def test_the_owner_s_directive_stops_the_assistant_and_only_the_owner_lifts_it(
    session, add_person
):
    alex = await add_person("Alex", "owner")
    tom = await _tom(session, alex)
    await set_directive(
        session,
        kind="refuse",
        person=tom,
        description="Tom from the club",
        except_words=None,
        until=None,
        owner=alex,
        now=NOW,
    )
    given = {"role": "guest", "made_role": None, "standing": None, "guest_days": 1, "turn_of": None}
    with pytest.raises(records.UnderDirective):
        await records.give_access(
            session, tom, by="domo", by_owner=False, reason="x", now=NOW, **given
        )
    assert tom.state == "refused"
    await records.give_access(
        session, tom, by=f"person:{alex.id}", by_owner=True, reason=None, now=NOW, **given
    )
    assert tom.state == "has_access"


async def test_the_owner_giving_it_back_lifts_both_his_withdrawal_and_his_refusal(
    session, add_person
):
    alex = await add_person("Alex", "owner")
    tom = await add_person("Tom", "guest")
    for kind in ("withdraw", "refuse"):
        await set_directive(
            session,
            kind=kind,
            person=tom,
            description=None,
            except_words=None,
            until=None,
            owner=alex,
            now=NOW,
        )
    given = {"role": "guest", "made_role": None, "standing": None, "guest_days": 1, "turn_of": None}
    await records.give_access(
        session, tom, by=f"person:{alex.id}", by_owner=True, reason=None, now=NOW, **given
    )
    assert tom.state == "has_access"
    assert await under_directive(session, tom.id, NOW) is None


async def test_removing_leaves_gone_and_the_owner_s_removal_is_a_withdrawal(session, add_person):
    alex = await add_person("Alex", "owner")
    dana = await add_person("Dana", "adult")
    sam = await add_person("Sam", "guest")
    away = {"by_owner": False, "reason": "he left", "turn_of": None, "now": NOW}
    removed = await records.remove(session, sam, by="domo", **away)
    assert (sam.state, removed.requests_refused) == ("gone", 0)
    await records.remove(
        session, dana, by=f"person:{alex.id}", **{**away, "by_owner": True, "reason": None}
    )
    assert dana.state == "withdrawn"
    with pytest.raises(records.NotPossible):
        await records.remove(session, alex, by="domo", **away)


# --- erasing -------------------------------------------------------------------------------------


async def test_erasing_gives_the_memories_back_to_free_text(session, add_person):
    alex = await add_person("Alex", "owner")
    dana = await add_person("Dana", "adult")
    tom = await _tom(session, alex)
    memory = Memory(
        subject="Tom",
        content="Tom likes tea",
        kind="fact",
        origin="others_words",
        about_id=tom.id,
        said_by_id=dana.id,
    )
    session.add_all(
        [
            memory,
            PersonLink(from_id=tom.id, to_id=dana.id, relation="friend of", said_by_id=dana.id),
            PersonNickname(
                person_id=dana.id,
                target_id=tom.id,
                nickname="Tommy",
                nickname_key="tommy",
                created_at=NOW,
            ),
            TrustFact(
                person_id=tom.id,
                outcome="held",
                established="seen",
                about="said so",
                created_at=NOW,
            ),
        ]
    )
    await session.flush()
    freed = await records.erase(session, tom, by="domo", reason="a mistake", turn_of=None, now=NOW)
    assert freed == [memory.id]
    await session.refresh(memory)
    assert memory.about_id is None and memory.content == "Tom likes tea"
    assert await session.scalar(select(func.count()).select_from(PersonLink)) == 0
    assert await session.scalar(select(func.count()).select_from(PersonNickname)) == 0
    assert await session.scalar(select(func.count()).select_from(TrustFact)) == 0
    assert (tom.display_name, tom.forgotten_at, tom.origin_reason) == ("", NOW, None)
    # The reason of what was done to them is gone; the act of erasing keeps none either (it would
    # name them).
    acts = await why(session, tom.id)
    assert [(a.act, a.reason) for a in acts] == [("opened", None), ("erased", None)]
    # The row stays, emptied: the journal and every column that names it keep standing.
    assert (await session.scalars(select(Person.id).where(Person.id == tom.id))).all() == [tom.id]


async def test_someone_who_had_access_is_not_erased(session, add_person):
    alex = await add_person("Alex", "owner")
    gone = await add_person("Sam", "guest", first_access_at=NOW)
    left = await records.remove(
        session, gone, by="domo", by_owner=False, reason="x", turn_of=None, now=NOW
    )
    assert left.requests_refused == 0
    with pytest.raises(records.NotPossible) as refused:
        await records.erase(session, gone, by="domo", reason="x", turn_of=None, now=NOW)
    assert refused.value.code == "has_been_here"
    with_account = await _tom(session, alex)
    session.add(Account(person_id=with_account.id, username="tom", password_hash="hash"))
    await session.flush()
    with pytest.raises(records.NotPossible) as refused:
        await records.erase(session, with_account, by="domo", reason="x", turn_of=None, now=NOW)
    assert refused.value.code == "has_been_here"
    assert with_account.display_name == "Tom"


async def test_the_owner_s_directive_keeps_a_record_from_being_erased(session, add_person):
    alex = await add_person("Alex", "owner")
    tom = await _tom(session, alex)
    await set_directive(
        session,
        kind="refuse",
        person=tom,
        description="Tom",
        except_words=None,
        until=None,
        owner=alex,
        now=NOW,
    )
    with pytest.raises(records.NotPossible) as refused:
        await records.erase(session, tom, by="domo", reason="x", turn_of=None, now=NOW)
    assert refused.value.code == "under_directive"


async def test_the_erase_tool_is_the_assistant_s_own_gesture(session_factory, monkeypatch):
    people = await _people(session_factory, ("Alex", "owner"), ("Paul", "child"))
    _judge(monkeypatch)
    ctx, reply_id, _ = await _turn(session_factory, people["Paul"])
    await handle_tool_call(mark(WHY), ctx=ctx, reply_id=reply_id)
    await handle_tool_call(open_tom(is_child=True, reason=WHY), ctx=ctx, reply_id=reply_id)
    [tom] = await _persons(session_factory, "Tom")
    erase = call("record_erase", {"person": f"person:{tom.id}", "reason": "Tom is not coming"})
    # Not marked this time: a reason without the mark is invalid, and nothing is erased.
    refused = await handle_tool_call(erase, ctx=ctx, reply_id=reply_id)
    assert refused.status == "invalid"
    await handle_tool_call(mark("Tom is not coming"), ctx=ctx, reply_id=reply_id)
    done = await handle_tool_call(erase, ctx=ctx, reply_id=reply_id)
    assert done.status == "done" and done.model_result["erased"] is True
    async with session_factory() as reader:
        row = await reader.get(Person, tom.id)
    assert row is not None and (row.display_name, row.forgotten_at is not None) == ("", True)
    assert sorted(a.act for a in await _acts(session_factory)) == ["erased", "opened"]


# --- never a program -----------------------------------------------------------------------------


async def test_a_program_never_makes_a_person_nor_gives_access(
    session_factory, live_clock, add_member, monkeypatch
):
    assert PEOPLE_GESTURES.isdisjoint(PROGRAM_REQUESTS | PROGRAM_TYPES)
    # What a program may do by itself (``act``) is a type of the handlers, and none is a gesture
    # on people; every other kind of request maps to a type of ``PROGRAM_REQUESTS``.
    assert PEOPLE_GESTURES.isdisjoint(PROGRAM_ACTIONS | set(PROGRAM_HANDLERS))
    for gesture in sorted(PEOPLE_GESTURES):
        asked = read_ask({"do": "act", "action": gesture, "arguments": {}})
        assert isinstance(asked, ActAsk) and asked.action not in PROGRAM_HANDLERS
    # No open kind takes the name of a gesture of the people.
    async with session_factory() as session:
        for gesture in ("people.open", "role.create", "access.promise", "household.invite"):
            kind = OpenKind(gesture, False, False, False, False, {}, frozenset(), "A kind.")
            with pytest.raises(open_catalog.KindRefused):
                await open_catalog.add_kind(session, kind, now=NOW)
    # A launch that asks for it is refused, and no person is made.
    await add_member("Alex", "owner")
    dana = await add_member("Dana", "adult")
    outcomes: list[Any] = []

    async def program(request: Any, ask: FakeAsk) -> Any:
        await ask(BEGIN)
        for gesture, arguments in (
            ("people.open", {"name": "Tom", "standing": "visiting", "ground": "comes"}),
            ("household.change_role", {"person": "Dana", "role": "guest"}),
        ):
            try:
                outcomes.append(await ask({"do": "act", "action": gesture, "arguments": arguments}))
            except FakeRefused as refused:
                outcomes.append(refused.reason)
        return None

    tool = await add_tool(session_factory, "maker")
    before = await _count(session_factory, Person)
    deps = deps_of(session_factory, live_clock, by_program(maker=program), [])
    await launched(deps, tool, dana)
    assert outcomes == ["not_allowed", "not_allowed"]
    assert await _count(session_factory, Person) == before
    assert await _persons(session_factory, "Tom") == []


def test_the_people_tools_are_in_the_registry_and_describe_what_they_do():
    for name, action_type in (
        ("record_open", "people.open"),
        ("record_complete", "people.complete"),
        ("record_look", "people.look"),
        ("record_erase", "people.erase"),
    ):
        assert TOOLS[name].action_type == action_type
    for name in ("record_open", "record_complete", "record_erase"):
        properties = TOOLS[name].definition.parameters["properties"]
        assert "reason" in properties  # type: ignore[operator]
    assert "reason" not in TOOLS["record_look"].definition.parameters["properties"]  # type: ignore[operator]


async def test_a_marked_look_at_another_record_works_in_anyone_s_turn(session_factory, monkeypatch):
    people = await _people(session_factory, ("Alex", "owner"), ("Paul", "child"))
    _judge(monkeypatch)
    async with session_factory() as writer:
        tom = await records.open_record(
            writer,
            name="Tom",
            standing="visiting",
            is_child=False,
            by="domo",
            reason=WHY,
            turn_of=None,
            now=NOW,
        )
        await writer.commit()
    look = call("record_look", {"person": "Tom"})
    ctx, reply_id, _ = await _turn(session_factory, people["Paul"])
    unmarked = await handle_tool_call(look, ctx=ctx, reply_id=reply_id)
    assert unmarked.status != "done" and "acts" not in unmarked.model_result
    await handle_tool_call(
        mark("I check for a duplicate before opening a record"), ctx=ctx, reply_id=reply_id
    )
    marked = await handle_tool_call(look, ctx=ctx, reply_id=reply_id)
    assert marked.status == "done" and marked.model_result["id"] == str(tom.id)
    # The mark is used up: the next look, unmarked, is refused again.
    again = await handle_tool_call(look, ctx=ctx, reply_id=reply_id)
    assert again.status != "done"


async def test_the_owner_looks_at_a_record_even_with_a_note_told_in_his_turn(
    session_factory, monkeypatch
):
    """Dana's request waits for Alex's yes, and a note is told in his turn: "show me Tom's
    record" is still his own request."""
    people = await _people(session_factory, ("Alex", "owner"), grants=False)
    async with session_factory() as writer:
        writer.add(
            Person(
                display_name="Tom",
                role="guest",
                state="known",
                origin="domo",
                origin_reason="Alex spoke of him",
                standing="visiting",
            )
        )
        await writer.commit()
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"], told_outside=True)
    assert not ctx.they_asked
    looked = await handle_tool_call(
        call("record_look", {"person": "Tom"}), ctx=ctx, reply_id=reply_id
    )
    assert looked.status == "done", looked.model_result


async def test_a_right_to_look_at_records_the_owner_gave_works(session_factory, monkeypatch):
    """Alex gives Dana the right to look at the records: "what do you know of Tom?" is no longer
    refused by the code; without it, it is."""
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"), grants=False)
    async with session_factory() as writer:
        writer.add(
            Person(
                display_name="Tom",
                role="guest",
                state="known",
                origin="domo",
                origin_reason="Dana spoke of him",
                standing="visiting",
            )
        )
        await writer.commit()
    ctx, reply_id, _ = await _turn(session_factory, people["Dana"])
    refused = await handle_tool_call(
        call("record_look", {"person": "Tom"}), ctx=ctx, reply_id=reply_id
    )
    assert refused.status == "invalid", refused.model_result
    async with session_factory() as writer:
        writer.add(
            PolicyRight(
                person_id=people["Dana"].id,
                action_type="people.look",
                access="allow",
                summary="Dana may look at the records.",
                owner_words="she may see the records",
                created_by=people["Alex"].id,
            )
        )
        await writer.commit()
    looked = await handle_tool_call(
        call("record_look", {"person": "Tom"}), ctx=ctx, reply_id=reply_id
    )
    assert looked.status == "done", looked.model_result


async def test_whether_the_owner_is_told_of_his_own_removal_is_his_judgement(
    session_factory, monkeypatch
):
    """The assistant takes away, on his own idea, an access he gave: the owner is told when he
    judged the owner would want it (tell_owner), not otherwise; the act is traced either way."""
    people = await _people(
        session_factory, ("Alex", "owner"), ("Dana", "adult"), ("Sam", "guest"), ("Tom", "guest")
    )
    async with session_factory() as writer:
        writer.add(
            PolicyRight(
                for_domo=True,
                action_type="household.remove",
                access="allow",
                summary="The assistant may.",
                owner_words="you may",
                created_by=people["Alex"].id,
            )
        )
        await writer.commit()
    _judge(monkeypatch)
    ctx, reply_id, notified = await _turn(session_factory, people["Dana"])
    for name, tell in (("Sam", False), ("Tom", True)):
        await handle_tool_call(mark(WHY), ctx=ctx, reply_id=reply_id)
        arguments: dict[str, Any] = {"person": name, "reason": "their stay is over"}
        if tell:
            arguments["tell_owner"] = True
        done = await handle_tool_call(
            call("household_remove", arguments), ctx=ctx, reply_id=reply_id
        )
        assert done.status == "done", done.model_result
    assert notified == [people["Alex"].id]
    removed = [a for a in await _acts(session_factory) if a.act == "access_removed"]
    owed = {a.person_id: a.owed_to_owner for a in removed}
    assert owed == {people["Sam"].id: False, people["Tom"].id: True}
    # A person's own call never says it: it is the assistant's judgement.
    refused = await handle_tool_call(
        call("household_remove", {"person": "Dana", "tell_owner": True}), ctx=ctx, reply_id=reply_id
    )
    assert refused.status == "invalid", refused.model_result


def test_forget_me_says_he_may_ask_an_adult_first_for_a_child():
    assert "ask an adult" in TOOLS["forget_me"].definition.description


async def test_someone_passing_who_now_lives_here_is_judged_never_silent(
    session_factory, monkeypatch
):
    """Sam, an adult only passing by, now lives here: record_complete leaves it as it is and
    names the judged gesture; household_change_role with lives_here does it."""
    people = await _people(session_factory, ("Alex", "owner"), ("Sam", "adult"))
    async with session_factory() as writer:
        sam = await writer.get(Person, people["Sam"].id)
        assert sam is not None
        sam.standing = "visiting"
        writer.add(
            PolicyRight(
                for_domo=True,
                action_type="household.change_role",
                access="allow",
                summary="The assistant may.",
                owner_words="you may",
                created_by=people["Alex"].id,
            )
        )
        await writer.commit()
    judged = _judge(monkeypatch)
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    await handle_tool_call(mark(WHY), ctx=ctx, reply_id=reply_id)
    silent = await handle_tool_call(
        call("record_complete", {"person": "Sam", "standing": "lives_here", "reason": WHY}),
        ctx=ctx,
        reply_id=reply_id,
    )
    assert silent.model_result["standing_not_changed"] is True
    await handle_tool_call(mark(WHY), ctx=ctx, reply_id=reply_id)
    moved = await handle_tool_call(
        call(
            "household_change_role",
            {"person": "Sam", "role": "adult", "lives_here": True, "reason": WHY},
        ),
        ctx=ctx,
        reply_id=reply_id,
    )
    assert moved.status == "done", moved.model_result
    assert moved.card["key"] == "action.card.done.household.change_role.lives_here"
    [case] = [c for c in judged.cases if c.action["type"] == "household.change_role"]  # type: ignore[index]
    assert case.action["facts"]["comes_to_live"] is True  # type: ignore[index]
    async with session_factory() as reader:
        found = await reader.get(Person, people["Sam"].id)
    assert found is not None and found.standing == "lives_here"


def test_an_owner_s_rule_may_read_that_someone_comes_to_live_here():
    from domo_core.policy.catalog import ACTION_FACTS

    declared = ACTION_FACTS["household.change_role"]
    for fact in ("comes_to_live", "off_the_guests_key_rule", "may_then_agree_for_a_child"):
        assert declared[fact] == "boolean", fact


async def test_the_owner_s_removal_of_someone_whose_stay_is_over_is_his_withdrawal(
    session_factory, monkeypatch
):
    """B M4: Alex, "remove Sam" the day after Sam's stay ended: done, as his withdrawal."""
    people = await _people(session_factory, ("Alex", "owner"), ("Sam", "guest"))
    async with session_factory() as writer:
        sam = await writer.get(Person, people["Sam"].id)
        assert sam is not None
        sam.expires_at = NOW - timedelta(days=1)
        await writer.commit()
    _judge(monkeypatch)
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    done = await handle_tool_call(
        call("household_remove", {"person": "Sam"}), ctx=ctx, reply_id=reply_id
    )
    assert done.status == "done", done.model_result
    async with session_factory() as reader:
        found = await reader.get(Person, people["Sam"].id)
    assert found is not None and found.state == "withdrawn"


async def test_the_owner_keeping_a_promise_for_someone_he_withdrew_gives_it_back(
    session_factory, monkeypatch
):
    """B M2: Alex withdrew Tom, then keeps a promise for him himself: it is done, his directive
    lifted, as his invitation would."""
    from domo_core.people.models import AccessPromise

    people = await _people(session_factory, ("Alex", "owner"))
    alex = people["Alex"]
    async with session_factory() as writer:
        tom = Person(
            display_name="Tom",
            role="guest",
            state="known",
            standing="visiting",
            origin="domo",
            origin_reason="Alex spoke of him",
        )
        writer.add(tom)
        await writer.flush()
        await set_directive(
            writer,
            kind="withdraw",
            person=tom,
            description=None,
            except_words=None,
            until=None,
            owner=await writer.get(Person, alex.id),  # type: ignore[arg-type]
            now=NOW,
        )
        promise = AccessPromise(
            description="Tom, my cousin",
            role="guest",
            standing="visiting",
            asked_by=f"person:{alex.id}",
            is_order=True,
            reason="Alex asked it",
        )
        writer.add(promise)
        await writer.commit()
    _judge(monkeypatch)
    ctx, reply_id, _ = await _turn(session_factory, alex)
    kept = await handle_tool_call(
        call("promise_keep", {"promise": str(promise.id), "person": f"person:{tom.id}"}),
        ctx=ctx,
        reply_id=reply_id,
    )
    assert kept.status == "done", kept.model_result
    async with session_factory() as reader:
        assert await under_directive(reader, tom.id, LIVE) is None
