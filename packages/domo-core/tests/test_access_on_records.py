"""The household's own tools work on a record without an account (spec 6.4, 6.9, ruling 13): giving
a role gives an access to someone only known, removing takes it away, bringing back gives it again,
and each may be the assistant's own act, traced with his reason. What the owner removes is a
directive; what the assistant removed he may bring back; the owner's role is never given."""

import json
import uuid
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from domo_core.accounts.models import Account, Person
from domo_core.accounts.passwords import hash_password
from domo_core.audit.models import AuditEntry
from domo_core.confirmations.models import Confirmation
from domo_core.conversation import execution as execution_module
from domo_core.conversation.execution import handle_tool_call
from domo_core.household.models import Invitation
from domo_core.household.service import accept_return, check_return, create_return_invitation
from domo_core.llm.types import JsonObject, ToolCall
from domo_core.people import made_roles
from domo_core.people.directives import under_directive
from domo_core.people.models import OwnerDirective, PersonAct
from domo_core.policy.models import PolicyRight
from domo_core.tools.household_admin import OWNER_ROLE_NEVER_GIVEN
from sqlalchemy import func, select
from test_concerned_person import _people, _requests, _turn, _yes
from test_own_idea import LIVE, Judged, mark

REASON = "Alex said his parents are here for the weekend"


async def _records(session_factory: Any, *names: str, **values: Any) -> dict[str, Person]:
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


def _call(tool: str, **arguments: Any) -> ToolCall:
    return ToolCall(uuid.uuid4().hex[:8], tool, json.dumps(arguments))


async def _run(ctx: Any, reply_id: uuid.UUID, *calls: ToolCall) -> Any:
    record = None
    for call in calls:
        record = await handle_tool_call(call, ctx=ctx, reply_id=reply_id)
    return record


async def _person(session_factory: Any, person: Person) -> Person:
    async with session_factory() as reader:
        found = await reader.get(Person, person.id)
        assert found is not None
        return found


async def _acts(session_factory: Any, person: Person) -> list[PersonAct]:
    async with session_factory() as reader:
        found = await reader.scalars(
            select(PersonAct).where(PersonAct.person_id == person.id).order_by(PersonAct.at)
        )
        return list(found)


async def _count(session_factory: Any, model: Any) -> int:
    async with session_factory() as reader:
        return await reader.scalar(select(func.count()).select_from(model)) or 0


@pytest.fixture
def judged(monkeypatch: pytest.MonkeyPatch) -> Judged:
    judge = Judged()
    monkeypatch.setattr(execution_module, "judge", judge)
    return judge


async def test_give_the_house_to_my_parents_gives_access_to_two_records_without_accounts(
    session_factory, judged
):
    people = await _people(session_factory, ("Alex", "owner"), grants=False)
    parents = await _records(session_factory, "Sam", "Tom")
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    for name in ("Sam", "Tom"):
        record = await _run(
            ctx, reply_id, _call("household_change_role", person=name, role="adult")
        )
        assert record.status == "done", record.model_result
    for parent in parents.values():
        now = await _person(session_factory, parent)
        assert (now.state, now.role, now.first_access_at is not None) == (
            "has_access",
            "adult",
            True,
        )
        [act] = await _acts(session_factory, parent)
        assert (act.act, act.by, act.reason) == (
            "access_given",
            f"person:{people['Alex'].id}",
            None,
        )
    async with session_factory() as reader:
        owners = await reader.scalars(select(Account.person_id))
        assert set(owners) == {people["Alex"].id}
    assert await _count(session_factory, Confirmation) == 0


async def test_the_assistant_gives_access_on_his_own_and_it_is_his_line(session_factory, judged):
    people = await _people(session_factory, ("Alex", "owner"), grants=False)
    sam = (await _records(session_factory, "Sam"))["Sam"]
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    record = await _run(
        ctx,
        reply_id,
        mark(REASON),
        _call("household_change_role", person="Sam", role="guest", reason=REASON),
    )
    assert record.status == "done", record.model_result
    assert (await _person(session_factory, sam)).state == "has_access"
    [act] = await _acts(session_factory, sam)
    assert (act.act, act.by, act.reason, act.turn_of) == (
        "access_given",
        "domo",
        REASON,
        people["Alex"].id,
    )
    async with session_factory() as reader:
        line = await reader.scalar(
            select(AuditEntry).where(AuditEntry.action == "people.access_given")
        )
    assert line is not None and line.actor == "domo"
    assert line.details["act_id"] == str(act.id)
    assert REASON not in json.dumps(line.details)


async def test_the_assistant_s_own_call_has_its_reason_and_a_person_s_has_none(
    session_factory, judged
):
    people = await _people(session_factory, ("Alex", "owner"), grants=False)
    await _records(session_factory, "Sam")
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    without = await _run(
        ctx, reply_id, mark(REASON), _call("household_change_role", person="Sam", role="adult")
    )
    assert without.status == "invalid"
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    theirs = await _run(
        ctx, reply_id, _call("household_change_role", person="Sam", role="adult", reason=REASON)
    )
    assert theirs.status == "invalid"


async def test_an_adult_removes_through_the_assistant_not_by_right(session_factory, judged):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"), grants=False)
    sam = (await _records(session_factory, "Sam", state="has_access"))["Sam"]
    ctx, reply_id, _ = await _turn(session_factory, people["Dana"])
    hers = await _run(ctx, reply_id, _call("household_remove", person="Sam"))
    assert hers.status == "refused"
    assert hers.model_result["reason"] == "not_allowed"
    assert (await _person(session_factory, sam)).state == "has_access"
    his = await _run(
        ctx,
        reply_id,
        mark("Dana says Sam left this morning"),
        _call("household_remove", person="Sam", reason="Dana says Sam left this morning"),
    )
    assert his.status == "done", his.model_result
    assert (await _person(session_factory, sam)).state == "gone"
    [act] = await _acts(session_factory, sam)
    assert (act.act, act.by, act.turn_of) == ("access_removed", "domo", people["Dana"].id)
    assert await _count(session_factory, OwnerDirective) == 0


async def test_what_the_owner_removes_is_a_directive(session_factory, judged):
    people = await _people(session_factory, ("Alex", "owner"), grants=False)
    sam = (await _records(session_factory, "Sam", state="has_access"))["Sam"]
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    removed = await _run(ctx, reply_id, _call("household_remove", person="Sam"))
    assert removed.status == "done", removed.model_result
    assert (await _person(session_factory, sam)).state == "withdrawn"
    async with session_factory() as reader:
        directive = await under_directive(reader, sam.id, LIVE)
    assert directive is not None and directive.kind == "withdraw"
    back = await _run(
        ctx,
        reply_id,
        mark("Sam is back in town"),
        _call("household_bring_back", person="Sam", role="guest", reason="Sam is back in town"),
    )
    assert back.status == "refused", back.model_result
    assert back.model_result["reason"] == "owner_directive"
    assert (await _person(session_factory, sam)).state == "withdrawn"


async def test_someone_the_assistant_removed_may_come_back_through_him(session_factory, judged):
    people = await _people(session_factory, ("Alex", "owner"), grants=False)
    sam = (await _records(session_factory, "Sam", state="has_access"))["Sam"]
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    gone = await _run(
        ctx,
        reply_id,
        mark("Sam went home"),
        _call("household_remove", person="Sam", reason="Sam went home"),
    )
    assert gone.status == "done", gone.model_result
    assert (await _person(session_factory, sam)).state == "gone"
    back = await _run(
        ctx,
        reply_id,
        mark("Sam is here again"),
        _call("household_bring_back", person="Sam", role="guest", reason="Sam is here again"),
    )
    assert back.status == "done", back.model_result
    now = await _person(session_factory, sam)
    assert (now.state, now.disabled_at) == ("has_access", None)
    # No account: the access is given at once, with no link.
    assert await _count(session_factory, Invitation) == 0
    acts = [(act.act, act.by, act.reason) for act in await _acts(session_factory, sam)]
    assert acts == [
        ("access_removed", "domo", "Sam went home"),
        ("access_given", "domo", "Sam is here again"),
    ]


async def test_a_made_role_is_given_by_its_name(session_factory, judged):
    people = await _people(session_factory, ("Alex", "owner"), grants=False)
    tom = (await _records(session_factory, "Tom"))["Tom"]
    async with session_factory() as writer:
        role = await made_roles.create(
            writer,
            name="Family friend",
            base="adult",
            allow=(),
            deny=(),
            by="domo",
            reason="Tom comes often",
            now=LIVE,
        )
        await writer.commit()
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    record = await _run(
        ctx, reply_id, _call("household_change_role", person="Tom", role="family friend")
    )
    assert record.status == "done", record.model_result
    now = await _person(session_factory, tom)
    assert (now.state, now.role, now.made_role_id) == ("has_access", "adult", role.id)


async def test_the_owners_role_is_never_given(session_factory, judged):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"), grants=False)
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    for arguments in (
        {"person": "Dana", "role": "owner", "reason": "Alex is away for a year"},
        {"person": "Dana", "role": "Owner", "reason": "Alex is away for a year"},
    ):
        record = await _run(
            ctx,
            reply_id,
            mark("Alex is away for a year"),
            _call("household_change_role", **arguments),
        )
        assert record.status == "invalid", record.model_result
        assert record.model_result["detail"] == OWNER_ROLE_NEVER_GIVEN
    assert (await _person(session_factory, people["Dana"])).role == "adult"


async def test_changing_the_role_of_someone_who_has_access_leaves_its_trace(
    session_factory, judged
):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"), grants=False)
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    record = await _run(
        ctx, reply_id, _call("household_change_role", person="Dana", role="guest", guest_days=3)
    )
    assert record.status == "done", record.model_result
    [act] = await _acts(session_factory, people["Dana"])
    assert (act.act, act.by) == ("access_given", f"person:{people['Alex'].id}")


async def test_leaving_goes_through_the_records_and_leaves_its_trace(session_factory, judged):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"), grants=False)
    ctx, reply_id, _ = await _turn(session_factory, people["Dana"])
    record = await _run(ctx, reply_id, _call("household_leave"))
    assert record.status == "done", record.model_result
    assert (await _person(session_factory, people["Dana"])).state == "gone"
    [act] = await _acts(session_factory, people["Dana"])
    assert (act.act, act.by) == ("access_removed", f"person:{people['Dana'].id}")


# --- what acts only on the people of the house, and what no tool acts on -----------------------


async def test_a_tool_for_the_people_of_the_house_refuses_a_mere_record(session_factory, judged):
    people = await _people(session_factory, ("Alex", "owner"), grants=False)
    await _records(session_factory, "Tom")
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    calls: list[tuple[str, JsonObject]] = [
        ("note_leave", {"recipient": "Tom", "text": "Dinner is at eight"}),
        ("household_transfer_ownership", {"person": "Tom"}),
    ]
    for tool, arguments in calls:
        record = await _run(ctx, reply_id, _call(tool, **arguments))
        assert record.status in ("invalid", "refused"), (tool, record.model_result)
    assert (await _person(session_factory, people["Alex"])).role == "owner"


async def test_no_tool_acts_on_a_refused_merged_or_forgotten_record(session_factory, judged):
    people = await _people(session_factory, ("Alex", "owner"), grants=False)
    sam = (await _records(session_factory, "Sam", state="has_access"))["Sam"]
    tom = (await _records(session_factory, "Tom", state="refused"))["Tom"]
    async with session_factory() as writer:
        writer.add(
            OwnerDirective(kind="refuse", person_id=tom.id, set_by=people["Alex"].id, set_at=LIVE)
        )
        writer.add(
            Person(
                display_name="Paul",
                role="guest",
                state="gone",
                disabled_at=LIVE,
                merged_into_id=sam.id,
            )
        )
        writer.add(
            Person(
                display_name="Dana", role="guest", state="gone", disabled_at=LIVE, forgotten_at=LIVE
            )
        )
        await writer.commit()
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    refused = await _run(
        ctx,
        reply_id,
        mark("Tom asked to come"),
        _call("household_change_role", person="Tom", role="guest", reason="Tom asked to come"),
    )
    assert refused.status == "refused" and refused.model_result["reason"] == "owner_directive"
    for name in ("Paul", "Dana"):
        for tool, arguments in (
            ("household_change_role", {"role": "adult"}),
            ("household_remove", {}),
            ("household_bring_back", {"role": "adult"}),
        ):
            record = await _run(ctx, reply_id, _call(tool, person=name, **arguments))
            assert record.status == "invalid", (name, tool, record.model_result)
    assert (await _person(session_factory, tom)).state == "refused"


# --- fix round 1 -------------------------------------------------------------------------------


async def test_the_owner_s_removal_after_his_own_yes_is_still_his_withdrawal(
    session_factory, judged
):
    people = await _people(session_factory, ("Alex", "owner"), grants=False)
    alex = people["Alex"]
    sam = (await _records(session_factory, "Sam", state="has_access"))["Sam"]
    async with session_factory() as writer:
        # The owner's rule: ask him first before removing anyone.
        writer.add(
            PolicyRight(
                person_id=alex.id,
                action_type="household.remove",
                ask="yes",
                summary="Ask me first.",
                owner_words="ask me first",
                created_by=alex.id,
            )
        )
        await writer.commit()
    ctx, reply_id, _ = await _turn(session_factory, alex)
    waiting = await _run(ctx, reply_id, _call("household_remove", person="Sam"))
    assert waiting.status == "waiting", waiting.model_result
    [request] = await _requests(session_factory)
    # His yes comes later, by the button: no message of his is answered then.
    outcome, _ = await _yes(replace(ctx, reply_message_id=None), request, alex)
    assert outcome == "executed"
    assert (await _person(session_factory, sam)).state == "withdrawn"
    ctx, reply_id, _ = await _turn(session_factory, alex)
    back = await _run(
        ctx,
        reply_id,
        mark("Sam is back in town"),
        _call("household_bring_back", person="Sam", role="guest", reason="Sam is back in town"),
    )
    assert back.status == "refused" and back.model_result["reason"] == "owner_directive"


async def test_the_assistant_s_own_removal_in_the_owner_s_turn_is_no_directive(
    session_factory, judged
):
    people = await _people(session_factory, ("Alex", "owner"), grants=False)
    sam = (await _records(session_factory, "Sam", state="has_access"))["Sam"]
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    done = await _run(
        ctx,
        reply_id,
        mark("Sam went home"),
        _call("household_remove", person="Sam", reason="Sam went home"),
    )
    assert done.status == "done", done.model_result
    assert (await _person(session_factory, sam)).state == "gone"
    assert await _count(session_factory, OwnerDirective) == 0


async def _child(session_factory: Any) -> Person:
    """Paul, a child of the household, with access."""
    return (
        await _records(session_factory, "Paul", role="child", is_child=True, state="has_access")
    )["Paul"]


async def test_the_owner_lifts_the_mark_of_a_child_with_the_child_role(session_factory, judged):
    people = await _people(session_factory, ("Alex", "owner"), grants=False)
    paul = await _child(session_factory)
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    record = await _run(ctx, reply_id, _call("household_change_role", person="Paul", role="adult"))
    assert record.status == "done", record.model_result
    now = await _person(session_factory, paul)
    assert (now.role, now.is_child) == ("adult", False)


async def test_the_assistant_or_an_adult_with_the_right_leaves_the_mark_of_a_child(
    session_factory, judged
):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"), grants=False)
    paul = await _child(session_factory)
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    his = await _run(
        ctx,
        reply_id,
        mark("Paul turned eighteen, Alex says"),
        _call(
            "household_change_role",
            person="Paul",
            role="adult",
            reason="Paul turned eighteen, Alex says",
        ),
    )
    assert his.status == "done", his.model_result
    now = await _person(session_factory, paul)
    assert (now.role, now.is_child) == ("adult", True)
    async with session_factory() as writer:
        found = await writer.get(Person, paul.id)
        assert found is not None
        found.role = "child"
        writer.add(
            PolicyRight(
                person_id=people["Dana"].id,
                action_type="household.change_role",
                access="allow",
                summary="Dana may change roles.",
                owner_words="she may",
                created_by=people["Alex"].id,
            )
        )
        await writer.commit()
    ctx, reply_id, _ = await _turn(session_factory, people["Dana"])
    hers = await _run(ctx, reply_id, _call("household_change_role", person="Paul", role="adult"))
    assert hers.status == "done", hers.model_result
    now = await _person(session_factory, paul)
    assert (now.role, now.is_child) == ("adult", True)


async def test_a_return_link_never_lifts_the_mark_of_a_child(session, add_person, add_account):
    alex = await add_person("Alex", "owner")
    paul = await add_person(
        "Paul", "adult", is_child=True, state="gone", disabled_at=LIVE - timedelta(days=3)
    )
    await add_account(paul, "paul", hash_password("old password"))
    _, token = await create_return_invitation(
        session,
        person=paul,
        role="adult",
        guest_days=None,
        display_name="Paul",
        created_by=alex.id,
        now=LIVE,
    )
    assert token is not None
    returning = await check_return(session, token=token, password="a new password", now=LIVE)
    await accept_return(session, returning, hash_password("a new password"), now=LIVE)
    back = await session.get(Person, paul.id, populate_existing=True)
    assert back is not None and (back.state, back.is_child) == ("has_access", True)


# --- the made role at a return (the coordinator's ruling) --------------------------------------


async def _away_with_a_made_role(session, add_person, add_account, base: str):
    """Paul, an adult who carries a role made for the household on ``base``, gone with his
    account (the assistant removed him: the made role is kept for a return), and that role."""
    role = await made_roles.create(
        session,
        name="Family friend",
        base=base,  # type: ignore[arg-type]
        allow=(),
        deny=(),
        by="domo",
        reason="Paul comes often",
        now=LIVE,
    )
    paul = await add_person(
        "Paul",
        base,
        state="gone",
        disabled_at=LIVE - timedelta(days=3),
        made_role_id=role.id,
    )
    await add_account(paul, "paul", hash_password("old password"))
    return paul, role


async def _comes_back(session, paul: Person, alex: Person, role: str = "adult") -> Person:
    _, token = await create_return_invitation(
        session,
        person=paul,
        role=role,  # type: ignore[arg-type]
        guest_days=None,
        display_name="Paul",
        created_by=alex.id,
        now=LIVE,
    )
    assert token is not None
    returning = await check_return(session, token=token, password="a new password", now=LIVE)
    await accept_return(session, returning, hash_password("a new password"), now=LIVE)
    back = await session.get(Person, paul.id, populate_existing=True)
    assert back is not None
    return back


async def test_a_return_link_brings_the_made_role_back_when_its_base_is_the_link_s(
    session, add_person, add_account
):
    alex = await add_person("Alex", "owner")
    paul, role = await _away_with_a_made_role(session, add_person, add_account, "adult")
    back = await _comes_back(session, paul, alex)
    assert (back.state, back.role, back.made_role_id) == ("has_access", "adult", role.id)


async def test_a_return_link_leaves_a_made_role_of_another_base_or_one_deleted(
    session, add_person, add_account
):
    alex = await add_person("Alex", "owner")
    paul, role = await _away_with_a_made_role(session, add_person, add_account, "adult")
    back = await _comes_back(session, paul, alex, role="guest")
    assert (back.role, back.made_role_id) == ("guest", None)
    # Nobody carries it any more: it is deleted, as any role left unused.
    await session.refresh(role)
    assert role.deleted_at is not None


async def test_bringing_back_says_the_made_role_that_comes_back(session_factory, judged):
    people = await _people(session_factory, ("Alex", "owner"), grants=False)
    async with session_factory() as writer:
        role = await made_roles.create(
            writer,
            name="Family friend",
            base="adult",
            allow=(),
            deny=(),
            by="domo",
            reason="Paul comes often",
            now=LIVE,
        )
        paul = Person(
            display_name="Paul",
            role="adult",
            state="gone",
            disabled_at=LIVE - timedelta(days=3),
            made_role_id=role.id,
        )
        writer.add(paul)
        await writer.flush()
        writer.add(Account(person_id=paul.id, username="paul", password_hash="hash"))
        await writer.commit()
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    record = await _run(ctx, reply_id, _call("household_bring_back", person="Paul", role="adult"))
    assert record.status == "done", record.model_result
    assert record.model_result["made_role"] == "Family friend"
    assert "made_role_not_given_back" not in record.model_result
    guest = await _run(ctx, reply_id, _call("household_bring_back", person="Paul", role="guest"))
    assert guest.status == "done", guest.model_result
    assert guest.model_result["made_role_not_given_back"] == "Family friend"
    assert "made_role" not in guest.model_result


async def test_someone_with_an_account_is_brought_back_with_a_made_role_named(
    session_factory, judged
):
    """Alex: "bring Paul back as a nanny" (Paul has an account, "nanny" a made role): done; the
    link carries the role's base, and the made role is his once he is back (final review B M8)."""
    people = await _people(session_factory, ("Alex", "owner"), grants=False)
    async with session_factory() as writer:
        nanny = await made_roles.create(
            writer, name="Nanny", base="adult", allow=(), deny=(), by="domo", reason="x", now=LIVE
        )
        paul = Person(
            display_name="Paul", role="adult", state="gone", disabled_at=LIVE - timedelta(days=3)
        )
        writer.add(paul)
        await writer.flush()
        writer.add(Account(person_id=paul.id, username="paul", password_hash="hash"))
        await writer.commit()
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    record = await _run(ctx, reply_id, _call("household_bring_back", person="Paul", role="Nanny"))
    assert record.status == "done", record.model_result
    assert record.model_result["role"] == "adult"
    assert record.model_result["made_role"] == "Nanny"
    async with session_factory() as reader:
        found = await reader.get(Person, paul.id)
    assert found is not None and found.made_role_id == nanny.id
