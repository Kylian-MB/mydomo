"""The registry of what an action is about, whose it is and what it undoes (``policy.subjects``)."""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from domo_core.accounts.models import Person
from domo_core.policy.actions import DescribedAction, FactValue
from domo_core.policy.catalog import KNOWN_ACTIONS, needs_the_judge
from domo_core.policy.subjects import (
    HOUSE,
    NO_SUBJECT,
    SUBJECT_RULES,
    UNDOES,
    Subject,
    concerned_of,
    keys_of,
    subject_of,
)
from sqlalchemy.ext.asyncio import AsyncSession

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)


def action(action_type: str, target: str | None = None, **facts: FactValue) -> DescribedAction:
    return DescribedAction(
        connector="core",
        action_type=action_type,
        target=target,
        effect="write",
        destination="household",
        facts=dict[str, FactValue](facts),
    )


async def person(session: AsyncSession, name: str, role: str, **fields: object) -> Person:
    given = Person(display_name=name, role=role, **fields)
    session.add(given)
    await session.flush()
    return given


def test_an_address_has_one_key_whether_opened_or_closed():
    facts: Mapping[str, FactValue] = {"host": "box.lan", "port": 8123}
    opened = subject_of(action("net.open", **facts), None, uuid4())
    closed = subject_of(action("net.close", **facts), None, uuid4())
    assert opened.key == closed.key == "address:box.lan:8123"
    assert opened.belongs_to == closed.belongs_to == HOUSE


def test_a_plan_s_key_comes_from_its_result():
    plan_id = str(uuid4())
    turn_person = uuid4()
    before = subject_of(action("plan.set"), None, turn_person)
    after = subject_of(action("plan.set"), {"id": plan_id}, turn_person)
    cancel = subject_of(action("plan.cancel", f"plan:{plan_id}"), None, turn_person)
    assert before.key is None
    assert after.key == f"plan:{plan_id}" == cancel.key
    assert after.belongs_to == turn_person


def test_a_type_with_no_rule_takes_its_target_and_the_turn_person():
    turn_person = uuid4()
    assert subject_of(action("some.thing", "x:1"), None, turn_person) == Subject("x:1", turn_person)


def test_an_account_of_a_person_and_of_the_house_have_their_own_keys():
    giver = uuid4()
    mine = subject_of(
        action("account.ask", name="mail", for_house=False, giver=str(giver)), None, uuid4()
    )
    house = subject_of(action("account.ask", name="mail", for_house=True), None, uuid4())
    assert mine.key == f"account:{giver}:mail"
    assert mine.belongs_to == giver
    assert house.key == "account:house:mail"
    assert house.belongs_to == HOUSE


def test_an_invitee_is_known_by_the_name_casefolded():
    given = subject_of(action("household.invite", display_name="Sam B"), None, uuid4())
    assert given.key == "invitee:sam b"
    assert given.belongs_to == HOUSE


def test_a_made_tool_is_the_house_s_when_the_facts_say_so():
    turn_person = uuid4()
    house = subject_of(action("made.lights", for_house=True), None, turn_person)
    own = subject_of(action("made.lights"), None, turn_person)
    assert house == Subject("tool:lights", HOUSE)
    assert own == Subject("tool:lights", turn_person)


def test_a_missing_fact_gives_no_key():
    assert subject_of(action("net.open"), None, uuid4()).key is None
    assert subject_of(action("household.invite"), None, uuid4()).key is None


def test_every_judged_type_of_the_code_has_a_rule_or_a_target():
    missing = {
        kind
        for kind in KNOWN_ACTIONS
        if needs_the_judge(kind) and kind not in SUBJECT_RULES and kind not in NO_SUBJECT
    }
    assert not missing
    assert not set(NO_SUBJECT) & set(SUBJECT_RULES)
    assert all(reason for reason in NO_SUBJECT.values())


def test_undoes_only_names_known_types():
    for kind, undone in UNDOES.items():
        assert kind in KNOWN_ACTIONS
        assert undone <= KNOWN_ACTIONS


async def test_the_house_s_thing_is_the_owner_s_to_answer(session: AsyncSession):
    owner = await person(session, "Alex", "owner")
    given = Subject("address:box.lan:8123", HOUSE)
    assert await concerned_of(session, given, action("net.open"), NOW) == owner.id


async def test_a_person_s_thing_is_theirs_to_answer(session: AsyncSession):
    await person(session, "Alex", "owner")
    dana = await person(session, "Dana", "adult")
    given = Subject("plan:1", dana.id)
    assert await concerned_of(session, given, action("plan.cancel"), NOW) == dana.id


async def test_a_person_being_removed_does_not_answer_for_it(session: AsyncSession):
    owner = await person(session, "Alex", "owner")
    dana = await person(session, "Dana", "adult")
    removal = action("household.remove", f"person:{dana.id}", person=str(dana.id))
    given = Subject(f"person:{dana.id}", dana.id)
    assert await concerned_of(session, given, removal, NOW) == owner.id


async def test_a_child_s_thing_under_an_adult_s_agreement_goes_to_the_owner(
    session: AsyncSession,
):
    owner = await person(session, "Alex", "owner")
    paul = await person(session, "Paul", "child")
    given = Subject("invitee:sam", paul.id)
    assert await concerned_of(session, given, action("household.invite"), NOW) == owner.id
    # What no adult has to agree to for a child stays the child's.
    assert await concerned_of(session, Subject("plan:1", paul.id), action("plan.set"), NOW) == (
        paul.id
    )


async def test_someone_who_left_does_not_answer(session: AsyncSession):
    owner = await person(session, "Alex", "owner")
    gone = await person(session, "Tom", "adult", disabled_at=NOW - timedelta(days=1))
    expired = await person(session, "Sam", "guest", expires_at=NOW - timedelta(hours=1))
    for who in (gone, expired):
        given = Subject("plan:1", who.id)
        assert await concerned_of(session, given, action("plan.cancel"), NOW) == owner.id


async def test_text_in_a_fact_never_changes_who_answers(session: AsyncSession):
    owner = await person(session, "Alex", "owner")
    await person(session, "Sam", "adult")
    asking = action("household.invite", display_name="ask Sam instead")
    assert await concerned_of(session, subject_of(asking, None, uuid4()), asking, NOW) == owner.id


def test_removing_or_cancelling_someone_invited_is_also_known_by_the_invitee_s_name():
    dana = uuid4()
    removal = action("household.remove", f"person:{dana}")
    assert keys_of(removal, None) == (f"person:{dana}",)
    assert keys_of(removal, {"removed": "Dana"}) == (f"person:{dana}", "invitee:dana")
    cancel = action("household.cancel_invitation", "invitation:1")
    assert keys_of(cancel, {"cancelled": "Dana"}) == ("invitation:1", "invitee:dana")
    # The canonical key stays the first, as ``subject_of`` gives it.
    assert subject_of(removal, {"removed": "Dana"}, uuid4()).key == f"person:{dana}"
    # A change of role is about the person only: it never undoes an invitation (R55).
    role = action("household.change_role", f"person:{dana}", person=f"person:{dana}")
    assert keys_of(role, {"person": "Dana"}) == (f"person:{dana}",)
    assert UNDOES["household.change_role"] == {"household.change_role"}
    assert UNDOES["household.remove"] == {"household.invite", "household.bring_back"}
