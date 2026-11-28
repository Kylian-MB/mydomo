"""Stored actions and waiting requests carry their subject key, and a request that has a person
concerned is that person's alone to answer ("Domo judges first", task 4).

The turns run on the chat pilots' live server (``chat_support``); the yes-judge is the fake's."""

import uuid
from datetime import UTC, datetime

import pytest
from chat_support import ChatPilot
from domo_core.accounts.models import Person
from domo_core.chat.models import MessageAction
from domo_core.confirmations.models import Confirmation
from domo_core.confirmations.service import may_decide, may_see
from domo_core.policy.catalog import DEFAULT_POLICY
from domo_core.policy.rights import NO_RIGHTS
from sqlalchemy import select

NOW = datetime(2030, 1, 7, 9, 0, tzinfo=UTC)
CLOSED = {"host": "box.lan", "port": 8123, "tool": "lights"}
ASKED = {"host": "192.168.1.20", "port": 8123, "tool": "lights"}


async def actions_of_type(pilot: ChatPilot, action_type: str) -> list[MessageAction]:
    async with pilot.server.session_factory() as session:
        found = await session.scalars(
            select(MessageAction)
            .where(MessageAction.action_type == action_type)
            .order_by(MessageAction.created_at)
        )
        return list(found.all())


def person(name: str, role: str, **kwargs: object) -> Person:
    return Person(id=uuid.uuid4(), display_name=name, role=role, **kwargs)


def request(requester: Person, approver_kind: str, **kwargs: object) -> Confirmation:
    return Confirmation(
        requester_id=requester.id,
        approver_kind=approver_kind,
        tool_name="profile_rename",
        by_domo=False,
        **kwargs,
    )


# --- the subject key of what was done ------------------------------------------------------------


async def test_a_done_action_is_stored_with_its_subject_key(owner_chat: ChatPilot):
    reply = await owner_chat.say_with_tool("address_close", CLOSED, judge="do")
    assert [a["status"] for a in reply.actions] == ["done"]
    (stored,) = await actions_of_type(owner_chat, "net.close")
    assert stored.subject_key == "address:box.lan:8123"
    assert stored.idea_reason is None


async def test_a_plan_set_is_stored_with_the_key_of_its_result(owner_chat: ChatPilot):
    reply = await owner_chat.say_with_tool(
        "plan_set", {"why": "Ask Alex about the trip.", "on_return": True}
    )
    plan_id = reply.model_results[0]["id"]
    (stored,) = await actions_of_type(owner_chat, "plan.set")
    assert stored.subject_key == f"plan:{plan_id}"


async def test_a_waiting_request_keeps_the_subject_key_of_its_action(owner_chat: ChatPilot):
    reply = await owner_chat.say_with_tool("address_open", ASKED, judge="yes")
    assert [a["status"] for a in reply.actions] == ["waiting"]
    async with owner_chat.server.session_factory() as session:
        waiting = await session.get(Confirmation, reply.pending_confirmation_id)
        assert waiting is not None
    assert waiting.subject_key == "address:192.168.1.20:8123"
    assert (waiting.idea_reason, waiting.concerned_id) == (None, None)
    (stored,) = await actions_of_type(owner_chat, "net.open")
    assert stored.subject_key == "address:192.168.1.20:8123"


# --- who answers ---------------------------------------------------------------------------------


def test_a_request_with_a_person_concerned_is_theirs_alone_to_answer():
    alex, dana, sam = person("Alex", "owner"), person("Dana", "adult"), person("Sam", "adult")
    paul = person("Paul", "child")
    waiting = request(paul, "adult", concerned_id=dana.id)
    assert may_decide(waiting, dana, NOW, rights=NO_RIGHTS, policy=DEFAULT_POLICY)
    assert not may_decide(waiting, alex, NOW, rights=NO_RIGHTS, policy=DEFAULT_POLICY)
    assert not may_decide(waiting, sam, NOW, rights=NO_RIGHTS, policy=DEFAULT_POLICY)
    # They see it; the one who asked sees it too; another does not.
    assert may_see(waiting, dana, NOW, rights=NO_RIGHTS, policy=DEFAULT_POLICY) and may_see(
        waiting, paul, NOW, rights=NO_RIGHTS, policy=DEFAULT_POLICY
    )
    assert not may_see(waiting, alex, NOW, rights=NO_RIGHTS, policy=DEFAULT_POLICY) and not may_see(
        waiting, sam, NOW, rights=NO_RIGHTS, policy=DEFAULT_POLICY
    )


def test_the_person_concerned_answers_only_while_in_the_household():
    dana = person("Dana", "adult", disabled_at=NOW, state="gone")
    paul = person("Paul", "child")
    assert not may_decide(
        request(paul, "adult", concerned_id=dana.id),
        dana,
        NOW,
        rights=NO_RIGHTS,
        policy=DEFAULT_POLICY,
    )


@pytest.mark.parametrize(
    ("approver_kind", "answers", "does_not"),
    [
        ("self", "requester", "owner"),
        ("adult", "owner", "requester"),
        ("owner", "owner", "adult"),
    ],
)
def test_a_request_without_one_is_answered_as_before(approver_kind, answers, does_not):
    people = {
        "requester": person("Paul", "adult"),
        "owner": person("Alex", "owner"),
        "adult": person("Dana", "adult"),
    }
    waiting = request(people["requester"], approver_kind)
    assert waiting.concerned_id is None
    assert may_decide(waiting, people[answers], NOW, rights=NO_RIGHTS, policy=DEFAULT_POLICY)
    assert not may_decide(waiting, people[does_not], NOW, rights=NO_RIGHTS, policy=DEFAULT_POLICY)
