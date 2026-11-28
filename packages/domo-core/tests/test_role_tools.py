"""The assistant makes, changes and deletes roles for the household, under the ceiling held in
code; a role nobody carries any more is cleaned up, at once when its last bearer leaves it, after
seven days when it was never given. A role for one single person is an ordinary role carried by
one record."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from domo_core.accounts.actors import actor_of
from domo_core.accounts.models import Person
from domo_core.audit.models import AuditEntry
from domo_core.conversation import execution as execution_module
from domo_core.conversation.execution import handle_tool_call
from domo_core.household.upkeep import unused_roles_pass
from domo_core.llm.types import ToolCall
from domo_core.people import made_roles, records
from domo_core.people.models import AccessPromise, MadeRole, PersonAct, RoleAccess
from domo_core.policy import open_catalog
from domo_core.policy.catalog import OpenKind
from domo_core.policy.models import PolicyRight
from domo_core.policy.open_catalog import load_policy
from domo_core.policy.rights import load_rights
from sqlalchemy import func, select
from test_concerned_person import _people, _turn
from test_own_idea import LIVE, Judged, mark

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
WHY = "Dana says her friend Sam is always at the house"
LIGHTS = OpenKind(
    "made.lights", False, False, False, False, {}, frozenset({"owner", "adult"}), "The lights."
)


def call(tool: str, **arguments: Any) -> ToolCall:
    return ToolCall(uuid.uuid4().hex[:8], tool, json.dumps(arguments))


@pytest.fixture
def judged(monkeypatch: pytest.MonkeyPatch) -> Judged:
    judge = Judged()
    monkeypatch.setattr(execution_module, "judge", judge)
    return judge


async def run(ctx: Any, reply_id: uuid.UUID, *calls: ToolCall) -> Any:
    record = None
    for one in calls:
        record = await handle_tool_call(one, ctx=ctx, reply_id=reply_id)
    return record


async def records_named(session_factory: Any, *names: str, **values: Any) -> dict[str, Person]:
    """Records the assistant knows by name, with no account: ``known`` unless ``values`` say."""
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


async def made(session_factory: Any, name: str, **more: Any) -> MadeRole:
    async with session_factory() as writer:
        role = await made_roles.create(
            writer,
            name=name,
            base=more.pop("base", "guest"),
            allow=more.pop("allow", ()),
            deny=(),
            by="domo",
            reason=WHY,
            now=more.pop("now", LIVE),
        )
        await writer.commit()
        return role


async def all_roles(session_factory: Any) -> list[MadeRole]:
    async with session_factory() as reader:
        return list(await reader.scalars(select(MadeRole).order_by(MadeRole.created_at)))


async def acts(session_factory: Any, *kinds: str) -> list[PersonAct]:
    async with session_factory() as reader:
        found = await reader.scalars(
            select(PersonAct).where(PersonAct.act.in_(kinds)).order_by(PersonAct.at, PersonAct.id)
        )
        return list(found)


async def person_now(session_factory: Any, person: Person) -> Person:
    async with session_factory() as reader:
        found = await reader.get(Person, person.id)
        assert found is not None
        return found


async def carrying(session, name: str, role: MadeRole) -> Person:
    """A record that has access with ``role``."""
    person = await records.open_record(
        session,
        name=name,
        standing="visiting",
        is_child=False,
        by="domo",
        reason=WHY,
        turn_of=None,
        now=NOW,
    )
    return await records.give_access(
        session,
        person,
        role=role.base,
        made_role=role,
        standing=None,
        guest_days=3 if role.base == "guest" else None,
        by="domo",
        by_owner=False,
        reason=WHY,
        turn_of=None,
        now=NOW,
    )


async def role_of(session, name: str, **more: Any) -> MadeRole:
    return await made_roles.create(
        session,
        name=name,
        base=more.pop("base", "guest"),
        allow=more.pop("allow", ()),
        deny=(),
        by="domo",
        reason=WHY,
        now=more.pop("now", NOW),
    )


async def deletions(session) -> list[PersonAct]:
    found = await session.scalars(select(PersonAct).where(PersonAct.act == "role_deleted"))
    return list(found)


# --- a role for one person -----------------------------------------------------------------------


async def test_a_role_made_for_one_person_gives_her_the_control_of_the_house(
    session_factory, judged
):
    async with session_factory() as writer:
        await open_catalog.add_kind(writer, LIGHTS, now=LIVE)
        await writer.commit()
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"), grants=False)
    records_ = await records_named(session_factory, "Sam")
    other = (await records_named(session_factory, "Tom", role="guest", state="has_access"))["Tom"]
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    created = await run(
        ctx,
        reply_id,
        mark(WHY),
        call("role_create", name="Sam at home", base="guest", allow=["made.lights"], reason=WHY),
    )
    assert created.status == "done", created.model_result
    given = await run(
        ctx,
        reply_id,
        mark(WHY),
        call("household_change_role", person="Sam", role="Sam at home", reason=WHY),
    )
    assert given.status == "done", given.model_result
    sam = await person_now(session_factory, records_["Sam"])
    async with session_factory() as reader:
        policy = await load_policy(reader)
        rights = await load_rights(reader)
        assert rights.resolve(actor_of(sam), "made.lights", policy).allowed
        assert not rights.resolve(actor_of(other), "made.lights", policy).allowed
        # No right of her own: the role is the one thing that gives it.
        own = await reader.scalar(
            select(func.count()).select_from(PolicyRight).where(PolicyRight.person_id == sam.id)
        )
        assert own == 0


async def test_the_assistant_makes_a_neighbour_role_and_gives_it(session_factory, judged):
    people = await _people(session_factory, ("Alex", "owner"), grants=False)
    tom = (await records_named(session_factory, "Tom"))["Tom"]
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    created = await run(
        ctx,
        reply_id,
        mark(WHY),
        call("role_create", name="neighbour", base="guest", allow=["note.leave"], reason=WHY),
    )
    assert created.status == "done", created.model_result
    [role] = await all_roles(session_factory)
    assert (role.name, role.base, role.created_by, role.reason) == (
        "neighbour",
        "guest",
        "domo",
        WHY,
    )
    assert created.model_result["id"] == str(role.id)
    given = await run(
        ctx,
        reply_id,
        mark(WHY),
        call("household_change_role", person="Tom", role="neighbour", reason=WHY),
    )
    assert given.status == "done", given.model_result
    now = await person_now(session_factory, tom)
    assert (now.state, now.role, now.made_role_id) == ("has_access", "guest", role.id)


# --- the ceiling and the owner's own ones --------------------------------------------------------


async def test_a_role_asked_above_the_ceiling_is_refused_and_nothing_is_written(
    session_factory, judged
):
    people = await _people(session_factory, ("Alex", "owner"), grants=False)
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    record = await run(
        ctx,
        reply_id,
        mark(WHY),
        call(
            "role_create",
            name="steward",
            base="adult",
            allow=["note.leave", "spending.limit", "household.invite"],
            reason=WHY,
        ),
    )
    assert record.status == "invalid"
    assert record.model_result["reason"] == "not_possible"
    detail = json.dumps(record.model_result)
    assert "household.invite" in detail and "spending.limit" in detail
    assert "note.leave" not in detail
    assert await all_roles(session_factory) == []
    assert await acts(session_factory, "role_created") == []


async def test_a_nanny_cannot_make_people(session_factory, judged):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"), grants=False)
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    refused = await run(
        ctx,
        reply_id,
        mark(WHY),
        call("role_create", name="nanny", base="adult", allow=["people.open"], reason=WHY),
    )
    assert refused.status == "invalid"
    assert "people.open" in json.dumps(refused.model_result)
    assert await all_roles(session_factory) == []
    # A nanny the owner opens other doors to, still cannot open a record: the gesture is in no role.
    nanny = await made(session_factory, "nanny", base="adult", allow=["note.leave"])
    async with session_factory() as writer:
        dana = await writer.get(Person, people["Dana"].id)
        assert dana is not None
        dana.made_role_id = nanny.id
        await writer.commit()
    dana_ctx, dana_reply, _ = await _turn(session_factory, await person_now(session_factory, dana))
    record = await run(
        dana_ctx,
        dana_reply,
        call("record_open", name="Tom", standing="visiting", ground="comes"),
    )
    assert record.status == "refused" and record.model_result["reason"] == "not_allowed"


# --- changing and deleting -----------------------------------------------------------------------


async def test_changing_a_role_changes_what_its_people_may_do_at_once(session_factory, judged):
    people = await _people(session_factory, ("Alex", "owner"), grants=False)
    role = await made(session_factory, "neighbour")
    tom = (await records_named(session_factory, "Tom", state="has_access", made_role_id=role.id))[
        "Tom"
    ]

    async def may_leave_a_note() -> bool:
        async with session_factory() as reader:
            policy = await load_policy(reader)
            rights = await load_rights(reader)
            return rights.resolve(actor_of(tom), "note.leave", policy).allowed

    assert not await may_leave_a_note()
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    changed = await run(ctx, reply_id, call("role_change", role="neighbour", allow=["note.leave"]))
    assert changed.status == "done", changed.model_result
    assert await may_leave_a_note()
    again = await run(ctx, reply_id, call("role_change", role="Neighbour", forget=["note.leave"]))
    assert again.status == "done", again.model_result
    assert not await may_leave_a_note()
    # The ceiling holds when a role is changed too, and nothing is written.
    refused = await run(
        ctx, reply_id, call("role_change", role="neighbour", allow=["rights.apply"])
    )
    assert refused.status == "invalid" and "rights.apply" in json.dumps(refused.model_result)
    assert not await may_leave_a_note()


async def test_a_role_someone_carries_is_deleted_at_once_and_they_go_back_to_its_base(
    session_factory, judged
):
    """Alex: "delete the nanny role" while Tom carries it: done at once; Tom is an ordinary
    guest again (the role's base) and is told."""
    people = await _people(session_factory, ("Alex", "owner"), grants=False)
    role = await made(session_factory, "nanny")
    tom = (await records_named(session_factory, "Tom", state="has_access", made_role_id=role.id))[
        "Tom"
    ]
    ctx, reply_id, notified = await _turn(session_factory, people["Alex"])
    record = await run(ctx, reply_id, call("role_delete", role="nanny"))
    assert record.status == "done", record.model_result
    assert record.model_result["back_to_base"] == ["Tom"]
    assert notified == [tom.id]
    async with session_factory() as reader:
        back = await reader.get(Person, tom.id)
    assert back is not None and (back.made_role_id, back.role) == (None, "guest")
    assert [r.deleted_at is not None for r in await all_roles(session_factory)] == [True]


async def test_a_role_a_waiting_promise_names_is_not_deleted(session_factory, judged):
    people = await _people(session_factory, ("Alex", "owner"), grants=False)
    promised = await made(session_factory, "relative")
    async with session_factory() as writer:
        writer.add(
            AccessPromise(
                description="Alex's cousin",
                role="guest",
                made_role_id=promised.id,
                standing="visiting",
                asked_by="domo",
                is_order=False,
                reason=WHY,
            )
        )
        await writer.commit()
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    record = await run(ctx, reply_id, mark(WHY), call("role_delete", role="relative", reason=WHY))
    assert record.status != "done", record.model_result
    assert [r.deleted_at for r in await all_roles(session_factory)] == [None]
    assert await acts(session_factory, "role_deleted") == []


async def test_a_role_nobody_carries_is_deleted_by_the_assistant_and_looked_at(
    session_factory, judged
):
    people = await _people(session_factory, ("Alex", "owner"), grants=False)
    await made(session_factory, "neighbour", allow=["note.leave"])
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    seen = await run(ctx, reply_id, call("roles_look"))
    assert seen.status == "done", seen.model_result
    shown = {role["role"]: role for role in seen.model_result["roles"]}
    assert {"owner", "adult", "child", "guest", "neighbour"} <= set(shown)
    assert shown["neighbour"]["made"] and shown["neighbour"]["base"] == "guest"
    assert shown["neighbour"]["allow"] == ["note.leave"] and shown["neighbour"]["carried_by"] == 0
    assert shown["adult"]["carried_by"] == 0 and not shown["adult"]["made"]
    one = await run(ctx, reply_id, call("roles_look", role="Neighbour"))
    assert [role["role"] for role in one.model_result["roles"]] == ["neighbour"]
    deleted = await run(ctx, reply_id, mark(WHY), call("role_delete", role="neighbour", reason=WHY))
    assert deleted.status == "done", deleted.model_result
    [role] = await all_roles(session_factory)
    assert role.deleted_at is not None
    gone = await run(ctx, reply_id, call("roles_look", role="neighbour"))
    assert gone.status == "invalid"


async def test_each_role_act_is_traced_with_its_reason(session_factory, judged):
    people = await _people(session_factory, ("Alex", "owner"), grants=False)
    alex = people["Alex"]
    ctx, reply_id, _ = await _turn(session_factory, alex)
    steps = (
        (mark(WHY), call("role_create", name="neighbour", base="guest", reason=WHY)),
        (mark(WHY), call("role_change", role="neighbour", allow=["note.leave"], reason=WHY)),
        (mark(WHY), call("role_delete", role="neighbour", reason=WHY)),
    )
    for step in steps:
        record = await run(ctx, reply_id, *step)
        assert record.status == "done", record.model_result
    traced = await acts(session_factory, "role_created", "role_changed", "role_deleted")
    assert sorted((a.act, a.by, a.reason, a.turn_of) for a in traced) == [
        ("role_changed", "domo", WHY, alex.id),
        ("role_created", "domo", WHY, alex.id),
        ("role_deleted", "domo", WHY, alex.id),
    ]
    [role] = await all_roles(session_factory)
    assert {a.role_id for a in traced} == {role.id}
    async with session_factory() as reader:
        lines = (
            await reader.scalars(select(AuditEntry).where(AuditEntry.action.like("people.role_%")))
        ).all()
    assert sorted(line.action for line in lines) == [
        "people.role_changed",
        "people.role_created",
        "people.role_deleted",
    ]
    for line in lines:
        assert line.actor == "domo" and WHY not in json.dumps(line.details)
        assert set(line.details) == {"act_id", "role_id"}
    # The owner's own order has no reason to give: its trace is the owner's.
    ordered = await run(ctx, reply_id, call("role_create", name="nanny", base="adult"))
    assert ordered.status == "done", ordered.model_result
    [last] = [a for a in await acts(session_factory, "role_created") if a.by != "domo"]
    assert (last.by, last.reason) == (f"person:{alex.id}", None)


# --- a role nobody carries any more is cleaned up ------------------------------------------------


async def test_a_role_nobody_carries_any_more_is_cleaned_up(session, add_person):
    role = await role_of(session, "Sam at home")
    sam = await carrying(session, "Sam", role)
    kept = await role_of(session, "relative")
    tom = await carrying(session, "Tom", kept)
    session.add(
        AccessPromise(
            description="Tom's cousin",
            role="guest",
            made_role_id=kept.id,
            standing="visiting",
            asked_by="domo",
            is_order=False,
            reason=WHY,
        )
    )
    await session.flush()
    # Another role given: the role Sam carried is nobody's, and goes.
    await records.give_access(
        session,
        sam,
        role="guest",
        made_role=None,
        standing=None,
        guest_days=3,
        by="domo",
        by_owner=False,
        reason=WHY,
        turn_of=None,
        now=NOW,
    )
    assert sam.made_role_id is None and role.deleted_at == NOW
    [act] = await deletions(session)
    assert (act.act, act.by, act.reason, act.role_id) == ("role_deleted", "domo", "unused", role.id)
    # A role a waiting promise names stays, though nobody carries it.
    await records.give_access(
        session,
        tom,
        role="guest",
        made_role=None,
        standing=None,
        guest_days=3,
        by="domo",
        by_owner=False,
        reason=WHY,
        turn_of=None,
        now=NOW,
    )
    assert tom.made_role_id is None and kept.deleted_at is None
    assert len(await deletions(session)) == 1


async def test_the_role_a_record_keeps_is_not_deleted_when_it_is_given_again(session):
    role = await role_of(session, "neighbour")
    sam = await carrying(session, "Sam", role)
    await records.give_access(
        session,
        sam,
        role="guest",
        made_role=role,
        standing=None,
        guest_days=5,
        by="domo",
        by_owner=False,
        reason=WHY,
        turn_of=None,
        now=NOW,
    )
    assert role.deleted_at is None and sam.made_role_id == role.id
    # A role another record carries too is kept when one leaves.
    tom = await carrying(session, "Tom", role)
    await records.leave_role(session, tom, now=NOW)
    assert role.deleted_at is None and tom.made_role_id is None


async def test_the_owner_taking_an_access_away_and_an_erased_record_leave_their_role(
    session, add_person
):
    alex = await add_person("Alex", "owner")
    role = await role_of(session, "neighbour")
    sam = await carrying(session, "Sam", role)
    await records.remove(
        session,
        sam,
        by=f"person:{alex.id}",
        by_owner=True,
        reason=None,
        turn_of=alex.id,
        now=NOW,
    )
    assert sam.state == "withdrawn" and sam.made_role_id is None and role.deleted_at == NOW
    other = await role_of(session, "nanny", base="adult")
    tom = await records.open_record(
        session,
        name="Tom",
        standing="visiting",
        is_child=False,
        by="domo",
        reason=WHY,
        turn_of=None,
        now=NOW,
    )
    tom.made_role_id = other.id
    await session.flush()
    await records.erase(session, tom, by="domo", reason=WHY, turn_of=None, now=NOW)
    assert tom.made_role_id is None and other.deleted_at == NOW


async def test_the_assistant_removing_someone_keeps_their_role_for_their_return(session):
    role = await role_of(session, "neighbour")
    sam = await carrying(session, "Sam", role)
    await records.remove(session, sam, by="domo", by_owner=False, reason=WHY, turn_of=None, now=NOW)
    assert sam.state == "gone" and sam.made_role_id == role.id and role.deleted_at is None


async def test_a_merged_or_forgotten_record_does_not_keep_a_role_alive(session):
    role = await role_of(session, "neighbour")
    sam = await carrying(session, "Sam", role)
    tom = await carrying(session, "Tom", role)
    sam.merged_into_id = tom.id
    await session.flush()
    assert await made_roles.unused(session, before=NOW + timedelta(days=1)) == []
    # The one that is left carries it: it stays whoever else is gone.
    tom.forgotten_at = NOW
    await session.flush()
    assert await made_roles.unused(session, before=NOW + timedelta(days=1)) == [role]


async def test_giving_the_plain_base_role_to_someone_who_carries_a_made_role(
    session_factory, judged
):
    people = await _people(session_factory, ("Alex", "owner"), grants=False)
    role = await made(session_factory, "nanny", base="adult")
    sam = (
        await records_named(
            session_factory, "Sam", role="adult", state="has_access", made_role_id=role.id
        )
    )["Sam"]
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    record = await run(ctx, reply_id, call("household_change_role", person="Sam", role="adult"))
    assert record.status == "done", record.model_result
    assert (await person_now(session_factory, sam)).made_role_id is None
    [gone] = await all_roles(session_factory)
    assert gone.deleted_at is not None
    [act] = await acts(session_factory, "role_deleted")
    assert (act.by, act.reason) == ("domo", "unused")
    # Someone with no made role is still told they have the role they have.
    again = await run(ctx, reply_id, call("household_change_role", person="Sam", role="adult"))
    assert again.status != "done"


# --- a role never given --------------------------------------------------------------------------


async def test_a_role_never_given_is_removed_after_seven_days_not_before(session_factory):
    role = await made(session_factory, "neighbour", now=NOW)
    promised = await made(session_factory, "relative", now=NOW)
    async with session_factory() as writer:
        writer.add(
            AccessPromise(
                description="Alex's cousin",
                role="guest",
                made_role_id=promised.id,
                standing="visiting",
                asked_by="domo",
                is_order=False,
                reason=WHY,
            )
        )
        await writer.commit()
    clean_up = unused_roles_pass(session_factory)
    await clean_up(NOW + timedelta(days=6, hours=23))
    assert [r.deleted_at for r in await all_roles(session_factory)] == [None, None]
    await clean_up(NOW + timedelta(days=7, minutes=1))
    deleted = {r.name: r.deleted_at is not None for r in await all_roles(session_factory)}
    assert deleted == {"neighbour": True, "relative": False}
    [act] = await acts(session_factory, "role_deleted")
    assert (act.role_id, act.by, act.reason) == (role.id, "domo", "unused")
    # A second pass finds nothing to do.
    await clean_up(NOW + timedelta(days=8))
    assert len(await acts(session_factory, "role_deleted")) == 1


async def test_a_rule_for_a_made_role_holds_for_whoever_gets_it_later(session_factory, judged):
    """Alex: "the nannies may not leave notes". Written on the role itself, it holds for Tom,
    who has it now, and for Sam, who gets it later."""
    people = await _people(session_factory, ("Alex", "owner"), grants=False)
    role = await made(session_factory, "nanny", allow=["note.leave"])
    tom = (await records_named(session_factory, "Tom", state="has_access", made_role_id=role.id))[
        "Tom"
    ]
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    changed = await run(ctx, reply_id, call("role_change", role="nanny", deny=["note.leave"]))
    assert changed.status == "done", changed.model_result
    sam = (await records_named(session_factory, "Sam", state="has_access", made_role_id=role.id))[
        "Sam"
    ]
    async with session_factory() as reader:
        rights = await load_rights(reader)
        policy = await load_policy(reader)
        for person in (tom, sam):
            found = await reader.get(Person, person.id)
            assert found is not None
            assert not rights.resolve(actor_of(found), "note.leave", policy).allowed


async def _owner_says_no_notes_for_nannies(session_factory: Any, people: dict[str, Person]):
    role = await made(session_factory, "nanny", allow=["note.leave"])
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    said = await run(
        ctx,
        reply_id,
        call(
            "role_change",
            role="nanny",
            deny=["note.leave"],
            owner_lines=[{"type": "note.leave", "summary": "The nannies leave no notes."}],
        ),
    )
    assert said.status == "done", said.model_result
    return role


async def test_the_owner_s_rule_on_a_made_role_is_his_and_the_assistant_cannot_lift_it(
    session_factory, judged
):
    """Alex: "the nannies leave no notes". The assistant, on his own idea, can neither forget
    that line, nor allow it again, nor delete the role; the role stays when its last nanny
    leaves, and a later nanny has the rule; Alex himself lifts it (final re-review R2)."""
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"), grants=False)
    role = await _owner_says_no_notes_for_nannies(session_factory, people)
    async with session_factory() as reader:
        [line] = await reader.scalars(select(RoleAccess).where(RoleAccess.role_id == role.id))
    assert (line.access, line.by_owner) == ("deny", True)
    ctx, reply_id, _ = await _turn(session_factory, people["Dana"])
    for gesture in (
        call("role_change", role="nanny", forget=["note.leave"], reason=WHY),
        call("role_change", role="nanny", allow=["note.leave"], reason=WHY),
        call("role_delete", role="nanny", reason=WHY),
    ):
        refused = await run(ctx, reply_id, mark(WHY), gesture)
        assert refused.status != "done", refused.model_result
        assert "rule of the owner" in json.dumps(refused.model_result)
    tom = (await records_named(session_factory, "Tom", state="has_access", made_role_id=role.id))[
        "Tom"
    ]
    async with session_factory() as writer:
        found = await writer.get(Person, tom.id)
        assert found is not None
        await records.leave_role(writer, found, now=LIVE)
        await writer.commit()
    [kept] = await all_roles(session_factory)
    assert kept.deleted_at is None
    sam = (await records_named(session_factory, "Sam", state="has_access", made_role_id=role.id))[
        "Sam"
    ]
    async with session_factory() as reader:
        rights = await load_rights(reader)
        policy = await load_policy(reader)
        found = await reader.get(Person, sam.id)
        assert found is not None
        assert not rights.resolve(actor_of(found), "note.leave", policy).allowed
    owners, owners_reply, _ = await _turn(session_factory, people["Alex"])
    lifted = await run(
        owners, owners_reply, call("role_change", role="nanny", forget=["note.leave"])
    )
    assert lifted.status == "done", lifted.model_result
    deleted = await run(owners, owners_reply, call("role_delete", role="nanny"))
    assert deleted.status == "done", deleted.model_result


async def test_a_limit_or_an_ask_the_owner_says_for_a_made_role_holds_for_later_holders(
    session_factory, judged
):
    """Alex: "the nannies may leave notes, but ask me first" and "…never on Sundays" (a limit):
    written on the role, they hold for a nanny who gets the role later (ruling 19)."""
    people = await _people(session_factory, ("Alex", "owner"), grants=False)
    await made(session_factory, "nanny")
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    said = await run(
        ctx,
        reply_id,
        call(
            "role_change",
            role="nanny",
            allow=["note.leave", "device.list"],
            owner_lines=[
                {"type": "note.leave", "ask": "yes", "summary": "Ask me first."},
                {"type": "device.list", "limit": '"refuse"', "summary": "Never."},
            ],
        ),
    )
    assert said.status == "done", said.model_result
    async with session_factory() as reader:
        role = await made_roles.named(reader, "nanny")
    assert role is not None
    sam = (await records_named(session_factory, "Sam", state="has_access", made_role_id=role.id))[
        "Sam"
    ]
    async with session_factory() as reader:
        rights = await load_rights(reader)
        policy = await load_policy(reader)
        found = await reader.get(Person, sam.id)
        assert found is not None
        notes = rights.resolve(actor_of(found), "note.leave", policy)
        listing = rights.resolve(actor_of(found), "device.list", policy)
    assert notes.allowed and notes.ask == "yes"
    assert listing.limit is not None and listing.limit.expression == '"refuse"'


async def test_deleting_a_carried_role_on_his_own_idea_is_judged_and_told(session_factory, judged):
    """The assistant deletes, on his own idea, a role Tom carries (no rule of the owner's on
    it): it is judged with who carries it and what they lose, Tom is told, and the owner too
    when he judged it (ruling 20)."""
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"), grants=False)
    role = await made(session_factory, "nanny", allow=["note.leave"])
    tom = (await records_named(session_factory, "Tom", state="has_access", made_role_id=role.id))[
        "Tom"
    ]
    ctx, reply_id, notified = await _turn(session_factory, people["Dana"])
    deleted = await run(
        ctx,
        reply_id,
        mark(WHY),
        call("role_delete", role="nanny", reason=WHY, tell_owner=True),
    )
    assert deleted.status == "done", deleted.model_result
    [case] = [c for c in judged.cases if c.action["type"] == "role.delete"]  # type: ignore[index]
    facts = case.action["facts"]  # type: ignore[index]
    assert (facts["carried_by"], facts["carriers"], facts["they_lose"]) == (1, "Tom", "note.leave")
    assert set(notified) == {tom.id, people["Alex"].id}
