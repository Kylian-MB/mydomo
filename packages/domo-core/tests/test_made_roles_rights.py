"""The roles the assistant makes, under a ceiling held in code; the gestures on people that no role
holds; and who may agree for a child, a fact and never a role's name (S-J)."""

import json
import re
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import domo_core
import pytest
from domo_core.accounts.actors import actor_of
from domo_core.accounts.models import Account, Person
from domo_core.confirmations.case import IdeaTold, _who_may_agree, build_case
from domo_core.confirmations.judge import Judgement
from domo_core.confirmations.service import may_decide
from domo_core.confirmations.store import create_confirmation
from domo_core.confirmations.who_agrees import may_agree_for_a_child
from domo_core.conversation.execution import handle_tool_call
from domo_core.llm.types import JsonObject, ToolCall, ToolDefinition
from domo_core.people.made_roles import AboveTheCeiling, RoleNameTaken, change, create
from domo_core.people.models import MadeRole, RoleAccess
from domo_core.policy.actions import DescribedAction
from domo_core.policy.asks import Asked
from domo_core.policy.barrier import Refuse, approval_needed, decide, given_for_an_idea
from domo_core.policy.catalog import (
    ALLOWED_ACTIONS,
    CHANGES_NOTHING,
    DEFAULT_POLICY,
    DOMO_ACTIONS,
    KNOWN_ACTIONS,
    NOT_ON_HIS_OWN,
    OWNER_DIRECTIVE_ACTIONS,
    OWNER_ONLY_ACTIONS,
    PEOPLE_GESTURES,
    ROLE_CEILING,
    SILENT_YET_HIS_IDEA,
    PolicyTables,
    actions_allowed,
    is_silent,
    needs_the_judge,
)
from domo_core.policy.models import PolicyRight
from domo_core.policy.rights import NO_RIGHTS, load_rights
from domo_core.policy.rights_drafts import Draft, Example, RightChange
from domo_core.policy.rights_writer import check_draft
from domo_core.policy.roles import Actor, domo_actor
from domo_core.policy.subjects import HOUSE, SUBJECT_RULES, UNDOES, concerned_of, subject_of
from domo_core.tools.context import ToolContext, ToolOutcome, tools_for
from domo_core.tools.registry import TOOLS
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from test_concerned_person import _people, _turn
from test_judge import ALLOWED, NOTHING_GATHERED
from test_judge_case import NOW, _keep, case_of
from test_own_idea import REASON, _actions, _ctx, _household, _judge, invite, mark
from test_rights_writer import context as writer_context
from test_undone import action_of, call, facts

SOURCE = Path(domo_core.__file__).parent
WHEN = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
WHY = "Tom's neighbour waters the plants"

# The types of this plan, and how each is shown and judged (the brief's table).
NEW_TYPES = (
    "people.open",
    "people.complete",
    "people.look",
    "people.erase",
    "people.forget",
    "people.forget_me",
    "people.link",
    "people.unlink",
    "people.merge",
    "people.split",
    "people.mistake",
    "role.create",
    "role.change",
    "role.delete",
    "role.look",
    "access.promise",
    "access.promise_keep",
    "access.promise_drop",
    "access.promises_look",
    "household.send_link",
    "people.directive_set",
    "people.directive_lift",
    "people.directives_look",
)
SHOWN_AND_JUDGED = (
    "household.invite",
    "household.invite_again",
    "household.cancel_invitation",
    "household.change_role",
    "household.remove",
    "household.bring_back",
    "household.send_link",
    "access.promise_keep",
    "people.merge",
    "role.create",
    "role.change",
    # Deleting a role people carry changes what they may do (ruling 20).
    "role.delete",
    "people.forget",
    "people.forget_me",
    "people.directive_set",
    "people.directive_lift",
)
SILENT_YET_HIS = (
    "people.open",
    "people.complete",
    "people.erase",
    "people.link",
    "people.unlink",
    "people.split",
    "people.mistake",
    "access.promise",
    "access.promise_drop",
)
READS = ("people.look", "role.look", "access.promises_look", "people.directives_look")


def described(action_type: str, target: str | None = None, **facts: Any) -> DescribedAction:
    return DescribedAction("core", action_type, target, "household", "household", facts)


async def made_role(
    session: AsyncSession,
    name: str,
    base: str = "guest",
    *,
    allow: tuple[str, ...] = (),
    deny: tuple[str, ...] = (),
) -> MadeRole:
    return await create(
        session,
        name=name,
        base=base,  # type: ignore[arg-type]
        allow=allow,
        deny=deny,
        by="domo",
        reason=WHY,
        now=WHEN,
    )


async def owner_right(session: AsyncSession, owner: Person, action_type: str, **parts: Any) -> None:
    session.add(
        PolicyRight(
            action_type=action_type,
            summary="The owner's rule.",
            owner_words="as I say",
            created_by=owner.id,
            **parts,
        )
    )
    await session.flush()


async def with_account(session: AsyncSession, person: Person) -> Person:
    session.add(
        Account(person_id=person.id, username=person.display_name.lower(), password_hash="hash")
    )
    await session.flush()
    return person


# --- what a made role gives, and its ceiling -----------------------------------------------------


async def test_a_made_role_adds_and_removes_from_its_base(session, add_person):
    role = await made_role(session, "neighbour", allow=("note.leave",), deny=("models.step_up",))
    tom = await add_person("Tom", "guest", standing="visiting", made_role_id=role.id)
    rights = await load_rights(session)
    actor = actor_of(tom)
    assert actor.made_role == role.id
    assert rights.resolve(actor, "note.leave", DEFAULT_POLICY).allowed
    assert not rights.resolve(actor, "models.step_up", DEFAULT_POLICY).allowed
    # Without the role, the guest's base decides both.
    plain = replace(actor, made_role=None)
    assert not rights.resolve(plain, "note.leave", DEFAULT_POLICY).allowed
    assert rights.resolve(plain, "models.step_up", DEFAULT_POLICY).allowed


@pytest.mark.parametrize("action_type", sorted(ROLE_CEILING))
async def test_the_ceiling_holds_at_write(session, action_type):
    with pytest.raises(AboveTheCeiling) as refused:
        await made_role(session, "neighbour", base="adult", allow=(action_type,))
    assert refused.value.types == (action_type,)
    assert await session.scalar(select(MadeRole.id)) is None


async def test_a_type_the_catalog_does_not_know_is_no_access(session):
    with pytest.raises(ValueError):
        await made_role(session, "neighbour", allow=("garden.water",))


async def test_the_ceiling_holds_at_read(session, add_person):
    role = MadeRole(
        name="neighbour", name_key="neighbour", base="guest", created_by="domo", reason=WHY
    )
    session.add(role)
    await session.flush()
    above = ("rights.apply", "household.invite", "spending.limit", "key.remove")
    session.add_all(
        RoleAccess(role_id=role.id, action_type=action_type, access="allow")
        for action_type in (*above, "note.leave")
    )
    tom = await add_person("Tom", "guest", made_role_id=role.id)
    rights = await load_rights(session)
    # A base that gives none of them: only the role's rows could.
    bare = PolicyTables(KNOWN_ACTIONS, {**ALLOWED_ACTIONS, "guest": frozenset()})
    for action_type in above:
        assert not rights.resolve(actor_of(tom), action_type, bare).allowed, action_type
    assert rights.resolve(actor_of(tom), "note.leave", bare).allowed


async def test_the_owner_role_is_never_created_nor_copied(session):
    for name in ("Owner", " owner ", "ADULT", "guest"):
        with pytest.raises(RoleNameTaken):
            await made_role(session, name)
    with pytest.raises(AboveTheCeiling):
        await made_role(session, "steward", base="adult", allow=("household.transfer_ownership",))


async def test_a_made_role_s_name_is_taken_once(session):
    await made_role(session, "Neighbour")
    with pytest.raises(RoleNameTaken):
        await made_role(session, "neighbour")


async def test_a_role_never_opens_an_adult_thing_to_a_child(session, add_person):
    alex = await add_person("Alex", "owner")
    role = await made_role(session, "big brother", base="adult", allow=("device.entrust",))
    paul = await add_person("Paul", "adult", is_child=True, made_role_id=role.id)
    # The owner's rule for every adult does not reach him either.
    await owner_right(session, alex, "device.entrust", role="adult", access="allow")
    actor = actor_of(paul)
    assert actor.child and actor.made_role == role.id
    assert not (await load_rights(session)).resolve(actor, "device.entrust", DEFAULT_POLICY).allowed
    # The owner opens it to Paul himself: allowed, and an adult still agrees first.
    await owner_right(session, alex, "device.entrust", person_id=paul.id, access="allow")
    resolved = (await load_rights(session)).resolve(actor, "device.entrust", DEFAULT_POLICY)
    assert resolved.allowed
    needed = approval_needed(actor, resolved, "device.entrust")
    assert needed is not None and needed.approver == "adult"
    # Unless his right says he agrees for himself.
    old = await session.scalar(select(PolicyRight).where(PolicyRight.person_id == paul.id))
    assert old is not None
    old.revoked_at = WHEN
    await session.flush()
    session.add(
        PolicyRight(
            action_type="device.entrust",
            person_id=paul.id,
            access="allow",
            approver="self",
            summary="Paul entrusts on his own.",
            owner_words="Paul may",
            created_by=alex.id,
        )
    )
    await session.flush()
    resolved = (await load_rights(session)).resolve(actor, "device.entrust", DEFAULT_POLICY)
    assert resolved.allowed and approval_needed(actor, resolved, "device.entrust") is None


async def test_the_owners_rule_on_the_base_role_beats_the_made_role(session, add_person):
    alex = await add_person("Alex", "owner")
    role = await made_role(session, "neighbour", allow=("note.leave",))
    tom = await add_person("Tom", "guest", made_role_id=role.id)
    await owner_right(session, alex, "note.leave", role="guest", access="deny")
    rights = await load_rights(session)
    assert not rights.resolve(actor_of(tom), "note.leave", DEFAULT_POLICY).allowed


async def test_a_deleted_role_gives_nothing(session, add_person):
    role = await made_role(session, "neighbour", allow=("note.leave",))
    tom = await add_person("Tom", "guest", made_role_id=role.id)
    role.deleted_at = WHEN
    await session.flush()
    rights = await load_rights(session)
    assert not rights.resolve(actor_of(tom), "note.leave", DEFAULT_POLICY).allowed


async def test_a_made_role_s_access_offers_its_tool(session, add_person):
    role = await made_role(session, "neighbour", allow=("note.leave",))
    tom = await add_person("Tom", "guest", made_role_id=role.id)
    rights = await load_rights(session)
    offered = {tool.name for tool in tools_for(actor_of(tom), TOOLS, DEFAULT_POLICY, rights=rights)}
    assert "note_leave" in offered
    elder = await made_role(session, "elder", base="adult", allow=("device.entrust",))
    paul = await add_person("Paul", "adult", is_child=True, made_role_id=elder.id)
    rights = await load_rights(session)
    offered = {
        tool.name for tool in tools_for(actor_of(paul), TOOLS, DEFAULT_POLICY, rights=rights)
    }
    assert "device_entrust" not in offered


# --- who may agree for a child -------------------------------------------------------------------


async def test_who_may_agree_for_a_child_is_a_fact_not_a_name(session, add_person):
    alex = await add_person("Alex", "owner")
    paul = await add_person("Paul", "child")
    godmother = await made_role(session, "godmother", base="adult")
    nanny = await made_role(session, "nanny", base="adult")
    dana = await add_person("Dana", "adult", made_role_id=godmother.id)
    marked = await add_person("Tom", "adult", is_child=True, made_role_id=godmother.id)
    sam = await add_person("Sam", "adult", standing="visiting", made_role_id=nanny.id)

    async def agrees(person: Person) -> bool:
        rights = await load_rights(session)
        return may_agree_for_a_child(
            actor_of(person), paul.id, rights=rights, policy=DEFAULT_POLICY
        )

    assert await agrees(dana)
    assert not await agrees(marked)
    # Someone with an adult base only passing by too: whether they look after the child then is
    # the judge's (the owner's answer of 2026-11-27).
    assert await agrees(sam)
    assert await agrees(await add_person("Visitor", "adult", standing="visiting"))
    assert await agrees(alex)
    await change(
        session, nanny, name=None, allow=(), deny=("child.agree",), forget=(), by="domo",
        reason=WHY, now=WHEN,
    )  # fmt: skip
    assert not await agrees(sam)
    assert not may_agree_for_a_child(domo_actor(), paul.id, rights=NO_RIGHTS, policy=DEFAULT_POLICY)
    assert not may_agree_for_a_child(
        actor_of(paul), paul.id, rights=NO_RIGHTS, policy=DEFAULT_POLICY
    )
    # The nanny once her role allows it; a child-marked record never, the owner's yes or not.
    await change(
        session,
        nanny,
        name=None,
        allow=("child.agree",),
        deny=(),
        forget=(),
        by="domo",
        reason=WHY,
        now=WHEN,
    )
    assert await agrees(sam)
    await owner_right(session, alex, "child.agree", person_id=marked.id, access="allow")
    assert not await agrees(marked)
    # The owner's own word for one person decides, either way.
    await change(
        session, nanny, name=None, allow=(), deny=("child.agree",), forget=(), by="domo",
        reason=WHY, now=WHEN,
    )  # fmt: skip
    assert not await agrees(sam)
    await owner_right(session, alex, "child.agree", person_id=sam.id, access="allow")
    assert await agrees(sam)
    await owner_right(session, alex, "child.agree", person_id=dana.id, access="deny")
    assert not await agrees(dana)


async def test_the_approvers_and_the_judge_s_marking_read_that_fact(
    session, add_person, add_conversation
):
    # A role that takes the agreement for a child away (passing by alone no longer does: the
    # owner's answer of 2026-11-27).
    nanny = await made_role(session, "nanny", base="adult", deny=("child.agree",))
    godmother = await made_role(session, "godmother", base="adult")
    await with_account(session, await add_person("Alex", "owner"))
    paul = await with_account(session, await add_person("Paul", "child"))
    sam = await with_account(
        session, await add_person("Sam", "adult", standing="visiting", made_role_id=nanny.id)
    )
    dana = await with_account(session, await add_person("Dana", "adult", made_role_id=godmother.id))
    asked = await create_confirmation(
        session,
        requester=actor_of(paul),
        tool_name="household_leave",
        arguments={},
        action=described("household.leave", f"person:{paul.id}"),
        approver="adult",
        asked=Asked("yes", "judge"),
        conversation_id=(await add_conversation(paul)).id,
        message_id=None,
        now=NOW - timedelta(minutes=1),
    )
    rights = await load_rights(session)
    assert may_decide(asked, dana, NOW, rights=rights, policy=DEFAULT_POLICY)
    assert not may_decide(asked, sam, NOW, rights=rights, policy=DEFAULT_POLICY)
    assert await _who_may_agree(session, [sam.id, dana.id], paul.id, NOW) == {dana.id}
    await _keep(session, sam.id, "Paul may put the TV on, Sam says", about_id=paul.id)
    await _keep(session, dana.id, "Paul may put the TV on, Dana says", about_id=paul.id)
    case = await case_of(session, paul.id, paul.id)
    marked = {line.content for line in (*case.others_said, *case.owner_said) if line.may_agree}
    assert marked == {"Paul may put the TV on, Dana says"}


_ROLE_COMPARED = re.compile(
    r"""\.role\s*(==|!=)\s*["']adult["']|["']adult["']\s*(==|!=)\s*\w+\.role"""
)
_ROLE_IN = re.compile(r"""\.role\s+(not\s+)?in\s+[^\n]*["']adult["']""")
_ROLE_GIVEN = re.compile(r"(may_agree_for_a_child|agrees_for_children)\(\s*[\w.]*\.role\b")


def test_no_check_of_who_agrees_reads_a_role_s_name():
    files = [
        path for path in (SOURCE / "confirmations").glob("*.py") if path.name != "who_agrees.py"
    ]
    files.append(SOURCE / "madetools" / "offered.py")
    for path in files:
        text = path.read_text(encoding="utf-8")
        for pattern in (_ROLE_COMPARED, _ROLE_IN, _ROLE_GIVEN):
            assert not pattern.search(text), (path.name, pattern.pattern)


# --- the gestures on people ----------------------------------------------------------------------


def test_people_gestures_are_in_no_role():
    for role in ("adult", "child", "guest"):
        assert PEOPLE_GESTURES.isdisjoint(actions_allowed(role)), role
    assert actions_allowed("owner") >= PEOPLE_GESTURES
    assert PEOPLE_GESTURES <= DOMO_ACTIONS
    assert PEOPLE_GESTURES.isdisjoint(NOT_ON_HIS_OWN)
    assert OWNER_DIRECTIVE_ACTIONS <= OWNER_ONLY_ACTIONS
    assert {"people.look", "people.forget_me"} <= actions_allowed("guest")
    assert "child.agree" in actions_allowed("adult") and "child.agree" not in ROLE_CEILING


async def test_the_owner_may_open_a_gesture_to_one_person(session, add_person):
    alex = await add_person("Alex", "owner")
    dana = await add_person("Dana", "adult")
    tom = await add_person("Tom", "adult")
    await owner_right(session, alex, "household.invite", person_id=dana.id, access="allow")
    rights = await load_rights(session)
    assert rights.resolve(actor_of(dana), "household.invite", DEFAULT_POLICY).allowed
    assert not rights.resolve(actor_of(tom), "household.invite", DEFAULT_POLICY).allowed
    # An adult's own call is refused; the assistant's own idea is his to make.
    refused = decide(actor_of(tom), described("household.invite"), DEFAULT_POLICY, rights, {})
    assert refused == Refuse("not_allowed")


async def test_a_guest_announcing_a_friend_lets_the_assistant_act_in_his_own_name(
    session_factory, monkeypatch
):
    alex, sam, conversation, reply_id = await _household(session_factory)
    judged = _judge(monkeypatch)
    ctx = _ctx(session_factory, sam, conversation)
    await handle_tool_call(mark(), ctx=ctx, reply_id=reply_id)
    record = await handle_tool_call(invite(his=True), ctx=ctx, reply_id=reply_id)
    assert record.status == "done"
    [case] = judged.cases
    idea = case.as_json()["idea"]
    assert (idea["of"], idea["born_of_words_of"], idea["they_lack_the_right"]) == (
        "domo",
        "Sam",
        True,
    )
    # The owner's deny for Sam comes first, in code: no judge.
    async with session_factory() as writer:
        writer.add(
            PolicyRight(
                person_id=sam.id,
                action_type="household.invite",
                access="deny",
                summary="Sam may not invite.",
                owner_words="never let Sam invite",
                created_by=alex.id,
            )
        )
        await writer.commit()
    await handle_tool_call(mark(), ctx=ctx, reply_id=reply_id)
    again = await handle_tool_call(
        invite({"display_name": "Paul", "role": "guest", "guest_days": 1}, his=True),
        ctx=ctx,
        reply_id=reply_id,
    )
    assert again.status == "refused" and len(judged.cases) == 1


# --- subjects, undoing, shown or silent ----------------------------------------------------------


def test_every_new_type_has_its_subject_rule():
    assert set(NEW_TYPES) <= KNOWN_ACTIONS
    assert set(NEW_TYPES) <= set(SUBJECT_RULES)


def test_a_record_s_key_is_the_same_when_opened_and_when_erased():
    record = str(uuid.uuid4())
    turn = uuid.uuid4()
    opened = subject_of(described("people.open"), {"id": record}, turn)
    erased = subject_of(described("people.erase", f"person:{record}"), None, turn)
    assert opened.key == erased.key == f"person:{record}"
    assert opened.belongs_to == erased.belongs_to == HOUSE
    assert subject_of(described("people.open"), None, turn).key is None


def test_the_keys_of_links_merges_promises_and_directives():
    dana, alex = uuid.uuid4(), uuid.uuid4()
    turn = uuid.uuid4()
    link = described("people.link", **{"from": f"person:{dana}", "to": f"person:{alex}"})
    assert subject_of(link, None, turn).key == f"link:{dana}:{alex}"
    merged = described("people.split", kept=f"person:{alex}", absorbed=f"person:{dana}")
    assert subject_of(merged, None, turn).key == f"person:{alex}"
    kept = described("access.promise_keep", f"person:{dana}", promise="p1")
    assert subject_of(kept, None, turn).key == f"person:{dana}"
    assert subject_of(described("people.directive_set", kind="no_new_people"), None, turn).key == (
        "directive:no_new_people"
    )
    assert subject_of(described("people.forget_me", f"person:{turn}"), None, turn).belongs_to == (
        turn
    )


async def test_what_is_about_someone_of_the_house_is_the_owner_s_to_answer(session, add_person):
    alex = await with_account(session, await add_person("Alex", "owner"))
    dana = await with_account(session, await add_person("Dana", "adult"))
    for action_type in ("people.complete", "people.mistake", "household.send_link"):
        action = described(action_type, f"person:{dana.id}")
        subject = subject_of(action, None, dana.id)
        assert await concerned_of(session, subject, action, NOW) == alex.id


def test_the_new_undoing_types_name_known_types():
    for undoing in ("people.erase", "people.split", "people.unlink", "role.delete"):
        assert undoing in UNDOES
    for undoing, undone in UNDOES.items():
        assert undoing in KNOWN_ACTIONS and undone <= KNOWN_ACTIONS, undoing
    assert (
        UNDOES["people.mistake"]
        == UNDOES["people.directive_set"]
        == {
            "household.invite",
            "household.change_role",
            "household.bring_back",
            "access.promise_keep",
        }
    )
    assert {"household.invite", "access.promise_keep"} <= UNDOES["household.change_role"]


def test_each_people_type_is_shown_and_judged_as_the_table_says():
    for action_type in SHOWN_AND_JUDGED:
        assert not is_silent(action_type) and needs_the_judge(action_type), action_type
        assert action_type not in SILENT_YET_HIS_IDEA
    for action_type in SILENT_YET_HIS:
        assert is_silent(action_type) and not needs_the_judge(action_type), action_type
    # The look at a record is a read his own idea reaches (to check for a duplicate), and so is the
    # look at the promises that wait (the day he recognises someone, in anyone's turn), and the
    # look at the roles a review offers him on his own.
    his_reads = ("people.look", "access.promises_look", "role.look")
    assert {*SILENT_YET_HIS, *his_reads} == SILENT_YET_HIS_IDEA
    for action_type in (*READS, "child.agree"):
        assert is_silent(action_type) and action_type in CHANGES_NOTHING, action_type
        assert (action_type in SILENT_YET_HIS_IDEA) == (action_type in his_reads)


class OpenARecord:
    """A test-only tool of the type ``people.open``: a record opened (its id in the result)."""

    definition = ToolDefinition(
        name="open_a_record",
        description="Open a record.",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
    )
    action_type = "people.open"
    opened = str(uuid.uuid4())

    async def describe(self, arguments: JsonObject, actor: Actor, ctx: Any) -> DescribedAction:
        return described(self.action_type)

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        return ToolOutcome("done", {"id": self.opened}, {"key": "action.card.done", "params": {}})


async def test_a_silent_people_gesture_takes_the_mark_and_another_silent_tool_does_not(
    session_factory, monkeypatch
):
    _, sam, conversation, reply_id = await _household(session_factory)
    judged = _judge(monkeypatch)
    ctx = replace(
        _ctx(session_factory, sam, conversation),
        registry={**TOOLS, "open_a_record": OpenARecord()},
    )
    await handle_tool_call(mark(), ctx=ctx, reply_id=reply_id)
    search = ToolCall("s", "memory_search", json.dumps({"words": "Tom"}))
    await handle_tool_call(search, ctx=ctx, reply_id=reply_id)
    assert ctx.own_idea is not None
    record = await handle_tool_call(
        ToolCall("o", "open_a_record", "{}"), ctx=ctx, reply_id=reply_id
    )
    assert record.status == "done"
    assert ctx.own_idea is None
    assert judged.cases == []
    stored = (await _actions(session_factory, reply_id))[-1]
    assert (stored.action_type, stored.by_domo, stored.idea_reason) == ("people.open", True, REASON)
    assert stored.subject_key == f"person:{OpenARecord.opened}"


async def test_the_case_shows_the_made_role_the_mark_and_where_they_stand(
    session, add_person, add_conversation
):
    role = await made_role(session, "school friend", base="child")
    tom = await add_person("Tom", "child", is_child=True, standing="visiting", made_role_id=role.id)
    here = await add_conversation(tom)
    case = await build_case(
        session,
        actor=actor_of(tom),
        asks=tom,
        action=described("note.leave"),
        says="Leave a note.",
        rights=ALLOWED,
        conversation_id=here.id,
        now=NOW,
        zone=None,
        idea=IdeaTold("person"),
        gathered=NOTHING_GATHERED,
    )
    assert case.person == {
        "name": "Tom",
        "role": "child",
        "made_role": "school friend",
        "child": True,
        "standing": "visiting",
    }


async def test_the_owner_denies_the_assistant_a_new_gesture_by_a_rule():
    deny = RightChange(
        person_id=None,
        role=None,
        action_type="people.open",
        remove=False,
        access="deny",
        approver=None,
        limit_expression=None,
        judge_question=None,
        ask=None,
        ask_when=None,
        summary="The assistant opens no record.",
        for_domo=True,
    )
    refused = Example(None, "people.open", {}, "refused", for_domo=True)
    assert await check_draft(Draft(changes=(deny,), examples=(refused,)), writer_context()) == []
    rights = (await _laid_over_for(deny)).resolve(domo_actor(), "people.open", DEFAULT_POLICY)
    assert not rights.allowed
    decided = decide(
        domo_actor(), described("people.open"), DEFAULT_POLICY, await _laid_over_for(deny), {}
    )
    assert decided == Refuse("not_allowed")


async def _laid_over_for(change: RightChange) -> Any:
    from domo_core.policy.rights_writer import _laid_over

    return _laid_over(NO_RIGHTS, Draft(changes=(change,), examples=()))


def test_a_role_name_the_writer_does_not_know_is_refused_as_a_made_role():
    from domo_core.policy.rights_drafts import ROLE_RULE

    assert "not a role a rule is written on" in ROLE_RULE


# --- R55's cost: a role given at the invitation, then changed ------------------------------------


async def test_a_role_given_at_the_invitation_then_changed_is_noted_as_undone(
    session_factory, monkeypatch
):
    alex = (await _people(session_factory, ("Alex", "owner")))["Alex"]
    _judge(monkeypatch, Judgement("do", "a friend of the house"))
    ctx, reply_id, _ = await _turn(session_factory, alex, on_his_own=True, reply_message_id=None)
    tom_as_adult = {"display_name": "Tom", "role": "adult"}
    assert (
        await handle_tool_call(invite(tom_as_adult, his=True), ctx=ctx, reply_id=reply_id)
    ).status == ("done")
    invited = await action_of(session_factory, "household.invite")
    async with session_factory() as writer:
        writer.add(Person(display_name="Tom", role="adult"))
        await writer.commit()
    ctx, reply_id, _ = await _turn(session_factory, alex, said="Tom is only passing by.")
    record = await handle_tool_call(
        call("household_change_role", {"person": "Tom", "role": "guest", "guest_days": 2}),
        ctx=ctx,
        reply_id=reply_id,
    )
    assert record.status == "done", record.model_result
    assert "undoes" in record.model_result
    undone = [fact for fact in await facts(session_factory) if fact.kind == "undone"]
    assert [fact.action_id for fact in undone] == [invited.id]


async def test_a_role_given_at_a_return_then_changed_is_noted_as_undone(
    session_factory, monkeypatch
):
    people = await _people(session_factory, ("Alex", "owner"), ("Tom", "adult"))
    alex, tom = people["Alex"], people["Tom"]
    async with session_factory() as writer:
        gone = await writer.get(Person, tom.id)
        assert gone is not None
        gone.disabled_at, gone.state = WHEN - timedelta(days=3), "gone"
        await writer.commit()
    _judge(monkeypatch, Judgement("do", "Tom is back for good"))
    ctx, reply_id, _ = await _turn(session_factory, alex, on_his_own=True, reply_message_id=None)
    back = await handle_tool_call(
        # His own act, in a turn of his own: it carries his reason (``people.whose``).
        call(
            "household_bring_back",
            {"person": "Tom", "role": "adult", "reason": "Tom is back for good"},
        ),
        ctx=ctx,
        reply_id=reply_id,
    )
    assert back.status == "done", back.model_result
    brought = await action_of(session_factory, "household.bring_back")
    async with session_factory() as writer:
        returned = await writer.get(Person, tom.id)
        assert returned is not None
        returned.disabled_at, returned.state = None, "has_access"
        await writer.commit()
    ctx, reply_id, _ = await _turn(session_factory, alex, said="Tom is only passing by.")
    record = await handle_tool_call(
        call("household_change_role", {"person": "Tom", "role": "guest", "guest_days": 2}),
        ctx=ctx,
        reply_id=reply_id,
    )
    assert record.status == "done", record.model_result
    undone = [fact for fact in await facts(session_factory) if fact.kind == "undone"]
    assert [fact.action_id for fact in undone] == [brought.id]


# --- fix round 1 ---------------------------------------------------------------------------------


async def test_the_owner_s_rule_on_a_role_for_agreeing_for_a_child_is_applied(session, add_person):
    alex = await add_person("Alex", "owner")
    paul = await add_person("Paul", "child")
    dana = await add_person("Dana", "adult")
    sam = await add_person("Sam", "guest", standing="visiting")

    async def agrees(person: Person) -> bool:
        rights = await load_rights(session)
        return may_agree_for_a_child(
            actor_of(person), paul.id, rights=rights, policy=DEFAULT_POLICY
        )

    assert await agrees(dana) and not await agrees(sam)
    await owner_right(session, alex, "child.agree", role="adult", access="deny")
    await owner_right(session, alex, "child.agree", role="guest", access="allow")
    assert not await agrees(dana)
    assert await agrees(sam)
    # The owner's right for the person comes before the one for the role.
    await owner_right(session, alex, "child.agree", person_id=dana.id, access="allow")
    assert await agrees(dana)


async def test_a_role_right_for_the_adults_never_reaches_a_child_marked_person(session, add_person):
    alex = await add_person("Alex", "owner")
    paul = await add_person("Paul", "adult", is_child=True)
    dana = await add_person("Dana", "adult")
    await owner_right(session, alex, "household.invite", role="adult", access="allow")
    rights = await load_rights(session)
    assert rights.resolve(actor_of(dana), "household.invite", DEFAULT_POLICY).allowed
    assert not rights.resolve(actor_of(paul), "household.invite", DEFAULT_POLICY).allowed
    # A right on the role "child" opens it to every child-marked person, an adult agreeing first.
    await owner_right(session, alex, "household.invite", role="child", access="allow")
    resolved = (await load_rights(session)).resolve(
        actor_of(paul), "household.invite", DEFAULT_POLICY
    )
    assert resolved.allowed
    needed = approval_needed(actor_of(paul), resolved, "household.invite")
    assert needed is not None and needed.approver == "adult"


async def test_a_made_role_gives_a_child_marked_person_nothing_beyond_a_child_s(
    session, add_person
):
    role = await made_role(session, "keeper", base="adult", allow=("house.set", "role.look"))
    paul = await add_person("Paul", "adult", is_child=True, made_role_id=role.id)
    dana = await add_person("Dana", "adult", made_role_id=role.id)
    rights = await load_rights(session)
    for action_type in ("house.set", "role.look"):
        assert rights.resolve(actor_of(dana), action_type, DEFAULT_POLICY).allowed
        assert not rights.resolve(actor_of(paul), action_type, DEFAULT_POLICY).allowed


@pytest.mark.parametrize("action_type", ["spending.limit", "household.invite", "rights.apply"])
async def test_a_role_denies_nothing_above_its_ceiling_either(session, action_type):
    with pytest.raises(AboveTheCeiling):
        await made_role(session, "neighbour", deny=(action_type,))


async def test_a_change_refused_renames_nothing(session):
    role = await made_role(session, "neighbour")
    with pytest.raises(AboveTheCeiling):
        await change(
            session,
            role,
            name="gardener",
            allow=("rights.apply",),
            deny=(),
            forget=(),
            by="domo",
            reason=WHY,
            now=WHEN,
        )
    assert (role.name, role.name_key) == ("neighbour", "neighbour")


async def test_a_role_s_name_is_kept_as_compared(session):
    role = await made_role(session, "  Big   Neighbour ")
    assert (role.name, role.name_key) == ("Big Neighbour", "big neighbour")
    with pytest.raises(RoleNameTaken):
        await made_role(session, "OWNER")


def test_forgetting_oneself_is_about_the_person_of_the_turn():
    turn = uuid.uuid4()
    subject = subject_of(described("people.forget_me"), None, turn)
    assert subject.key == f"person:{turn}" and subject.belongs_to == turn


def test_his_own_idea_in_a_visiting_child_s_turn_is_not_refused_by_the_child_s_set():
    """Tom, Paul's friend, a visiting child (role guest, child mark): "my mum Sam picks me up at
    six". The assistant's own record_open in Tom's turn is read with the child's set too, as it
    would be in Paul's."""
    tom = Actor(uuid.uuid4(), "guest", child=True, visiting=True)
    rights = NO_RIGHTS
    assert not rights.resolve(tom, "people.open", DEFAULT_POLICY).allowed
    theirs = given_for_an_idea(DEFAULT_POLICY, tom.role, "people.open", child=tom.child)
    assert rights.resolve(tom, "people.open", theirs).allowed


def test_a_made_role_never_gives_what_the_assistant_does_not_have():
    """A role the assistant makes never lets its bearer change someone else's texts, nor open a
    port of the machine that hosts him: neither is his to give."""
    assert {"texts.for_someone", "net.open_host"} <= ROLE_CEILING


async def test_a_made_role_may_take_away_forget_me_and_leaving(session, add_person):
    """The assistant makes a role for Sam that takes away "forget me" and leaving the household:
    written, and it holds when read (the owner's answer of 2026-11-27, "Oui, Domo juge")."""
    role = await made_role(
        session, "lodger", base="adult", deny=("people.forget_me", "household.leave")
    )
    sam = await add_person("Sam", "adult", made_role_id=role.id)
    rights = await load_rights(session)
    for action_type in ("people.forget_me", "household.leave"):
        assert not rights.resolve(actor_of(sam), action_type, DEFAULT_POLICY).allowed
    with pytest.raises(AboveTheCeiling):
        await made_role(session, "keeper", base="adult", allow=("household.leave",))
