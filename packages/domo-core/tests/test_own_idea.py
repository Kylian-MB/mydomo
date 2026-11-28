"""An idea of the assistant's (spec part 7): a call he marks as his own idea (``act_on_my_own``) is
judged with his own rights and what he knows, done when his judge finds nothing wrong, and recorded
as his. What the owner denied the person on purpose comes first, in code, with no judge; and a
judge that stays silent on an idea of his makes him do nothing and ask nothing."""

import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from domo_core.accounts.models import Person
from domo_core.audit.models import AuditEntry
from domo_core.chat.models import Conversation, Message, MessageAction
from domo_core.confirmations.judge import JudgeCase, Judgement, fallback
from domo_core.confirmations.models import Confirmation
from domo_core.conversation import execution as execution_module
from domo_core.conversation.execution import handle_tool_call
from domo_core.llm.types import JsonObject, ToolCall, ToolDefinition
from domo_core.policy import catalog as catalog_module
from domo_core.policy.actions import DescribedAction
from domo_core.policy.catalog import DEFAULT_POLICY, PolicyTables
from domo_core.policy.models import PolicyRight
from domo_core.policy.roles import Actor, as_role
from domo_core.tools.context import OwnIdea, ToolContext, ToolOutcome, idea_of
from domo_core.tools.registry import TOOLS
from sqlalchemy import func, select

LIVE = datetime.now(UTC)
REASON = "Sam says his friend Tom comes tonight"
INVITE_TOM = {"display_name": "Tom", "role": "guest", "guest_days": 1}


async def _household(
    session_factory: Any, role: str = "guest"
) -> tuple[Person, Person, Conversation, uuid.UUID]:
    """Alex, the owner; Sam, a guest (or ``role``), with a conversation and a reply of the
    assistant in it; and the assistant's own right to invite (he has none by default)."""
    async with session_factory() as writer:
        alex = Person(display_name="Alex", role="owner")
        sam = Person(display_name="Sam", role=role)
        writer.add_all([alex, sam])
        await writer.flush()
        conversation = Conversation(person_id=sam.id)
        writer.add(conversation)
        await writer.flush()
        writer.add(
            Message(
                conversation_id=conversation.id,
                role="user",
                content="my friend Tom comes tonight",
                status="complete",
                provenance="person",
            )
        )
        reply = Message(
            conversation_id=conversation.id,
            role="assistant",
            content="",
            status="complete",
            provenance="model",
        )
        writer.add(reply)
        writer.add(
            PolicyRight(
                for_domo=True,
                action_type="household.invite",
                access="allow",
                summary="The assistant may invite.",
                owner_words="you may invite",
                created_by=alex.id,
            )
        )
        await writer.flush()
        reply_id = reply.id
        await writer.commit()
    return alex, sam, conversation, reply_id


def _ctx(session_factory: Any, person: Person, conversation: Conversation) -> ToolContext:
    return ToolContext(
        session_factory=session_factory,
        actor=Actor(person.id, as_role(person.role)),
        person=person,
        conversation_id=conversation.id,
        origin="",
        gatekeeper=None,  # type: ignore[arg-type]
        clock=lambda: LIVE,
        registry=TOOLS,
        policy=DEFAULT_POLICY,
        reply_message_id=uuid.uuid4(),
    )


class Judged:
    """The judge of the test: what it was given, and what it answers (``do`` unless told)."""

    def __init__(self, *answers: Judgement) -> None:
        self.cases: list[JudgeCase] = []
        self._answers = answers or (Judgement("do"),)

    async def __call__(self, model: object, case: JudgeCase, **_: object) -> Judgement:
        self.cases.append(case)
        return self._answers[min(len(self.cases), len(self._answers)) - 1]


def _judge(monkeypatch: pytest.MonkeyPatch, *answers: Judgement) -> Judged:
    judged = Judged(*answers)
    monkeypatch.setattr(execution_module, "judge", judged)
    return judged


def _barrier_actors(monkeypatch: pytest.MonkeyPatch) -> list[Actor]:
    """Who the barrier decides for, from now on."""
    actors: list[Actor] = []
    decide = execution_module.decide_now

    async def deciding(session: Any, actor: Actor, *args: Any, **kwargs: Any) -> Any:
        actors.append(actor)
        return await decide(session, actor, *args, **kwargs)

    monkeypatch.setattr(execution_module, "decide_now", deciding)
    return actors


def mark(reason: str = REASON) -> ToolCall:
    return ToolCall("m", "act_on_my_own", json.dumps({"reason": reason}))


def invite(arguments: JsonObject = INVITE_TOM) -> ToolCall:
    return ToolCall("i", "household_invite", json.dumps(arguments))


async def _actions(session_factory: Any, reply_id: uuid.UUID) -> list[MessageAction]:
    async with session_factory() as reader:
        found = await reader.execute(
            select(MessageAction)
            .where(MessageAction.message_id == reply_id)
            .order_by(MessageAction.created_at)
        )
        return list(found.scalars())


async def test_a_marked_call_is_judged_as_the_assistant_s_with_his_reason(
    session_factory, monkeypatch
):
    _, sam, conversation, reply_id = await _household(session_factory)
    judged = _judge(monkeypatch)
    actors = _barrier_actors(monkeypatch)
    ctx = _ctx(session_factory, sam, conversation)
    marked = await handle_tool_call(mark(), ctx=ctx, reply_id=reply_id)
    assert marked.status == "done"
    assert ctx.own_idea == OwnIdea(REASON, born_of=sam.id)
    await handle_tool_call(invite(), ctx=ctx, reply_id=reply_id)
    invite_actors = actors[1:]
    # Decided as the assistant, and for Sam too: the mark lifts only a right not given (R26).
    assert invite_actors[0].subject == "domo"
    assert {actor.subject for actor in invite_actors} == {"domo", "person"}
    [case] = judged.cases
    assert case.as_json()["idea"] == {
        "of": "domo",
        "reason": REASON,
        "born_of_words_of": "Sam",
        "they_lack_the_right": True,
        "after_outside_words": False,
    }


async def test_the_mark_reaches_one_action_only(session_factory, monkeypatch):
    _, sam, conversation, reply_id = await _household(session_factory)
    _judge(monkeypatch)
    actors = _barrier_actors(monkeypatch)
    ctx = _ctx(session_factory, sam, conversation)
    await handle_tool_call(mark(), ctx=ctx, reply_id=reply_id)
    await handle_tool_call(invite(), ctx=ctx, reply_id=reply_id)
    assert ctx.own_idea is None
    actors.clear()
    second = await handle_tool_call(
        invite({"display_name": "Paul", "role": "guest"}), ctx=ctx, reply_id=reply_id
    )
    # Sam's own call, judged with Sam's rights: a guest may not invite.
    assert all(actor.subject == "person" for actor in actors)
    assert second.status == "refused"


async def test_a_silent_action_does_not_use_it_up(session_factory, monkeypatch):
    _, sam, conversation, reply_id = await _household(session_factory)
    _judge(monkeypatch)
    ctx = _ctx(session_factory, sam, conversation)
    await handle_tool_call(mark(), ctx=ctx, reply_id=reply_id)
    keep = ToolCall(
        "k",
        "memory_keep",
        json.dumps(
            {
                "subject": "Tom",
                "content": "Tom is Sam's friend",
                "kind": "fact",
                "private": False,
                "core": False,
            }
        ),
    )
    await handle_tool_call(keep, ctx=ctx, reply_id=reply_id)
    assert ctx.own_idea == OwnIdea(REASON, born_of=sam.id)


class SilentButHis:
    """A test-only tool of a silent type that is listed among the silent types an idea of his
    reaches (``SILENT_YET_HIS_IDEA``)."""

    definition = ToolDefinition(
        name="quiet_one",
        description="A quiet action.",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
    )
    action_type = "plan.set"

    async def describe(self, arguments: JsonObject, actor: Actor, ctx: Any) -> DescribedAction:
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=None,
            effect="self_profile",
            destination="household",
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        return ToolOutcome("done", {}, {"key": "action.card.done", "params": {}})


async def test_a_silent_type_listed_for_his_ideas_uses_the_mark_and_is_his(
    session_factory, monkeypatch
):
    _, sam, conversation, reply_id = await _household(session_factory)
    _judge(monkeypatch)
    actors = _barrier_actors(monkeypatch)
    monkeypatch.setattr(execution_module, "SILENT_YET_HIS_IDEA", frozenset({"plan.set"}))
    ctx = replace(
        _ctx(session_factory, sam, conversation),
        registry={**TOOLS, "quiet_one": SilentButHis()},
    )
    await handle_tool_call(mark(), ctx=ctx, reply_id=reply_id)
    actors.clear()
    record = await handle_tool_call(ToolCall("q", "quiet_one", "{}"), ctx=ctx, reply_id=reply_id)
    assert record.status == "done"
    assert ctx.own_idea is None
    assert actors[0].subject == "domo"
    assert {actor.subject for actor in actors} == {"domo", "person"}
    stored = (await _actions(session_factory, reply_id))[-1]
    assert (stored.by_domo, stored.idea_reason) == (True, REASON)


async def test_a_silent_type_not_listed_neither_uses_the_mark_nor_changes_the_actor(
    session_factory, monkeypatch
):
    _, sam, conversation, reply_id = await _household(session_factory)
    _judge(monkeypatch)
    actors = _barrier_actors(monkeypatch)
    assert "plan.set" not in catalog_module.SILENT_YET_HIS_IDEA
    ctx = replace(
        _ctx(session_factory, sam, conversation),
        registry={**TOOLS, "quiet_one": SilentButHis()},
    )
    await handle_tool_call(mark(), ctx=ctx, reply_id=reply_id)
    actors.clear()
    await handle_tool_call(ToolCall("q", "quiet_one", "{}"), ctx=ctx, reply_id=reply_id)
    assert ctx.own_idea == OwnIdea(REASON, born_of=sam.id)
    assert actors and all(actor.subject == "person" for actor in actors)


@pytest.mark.parametrize("whose", ["person", "role"])
async def test_what_the_owner_denied_them_on_purpose_is_refused_without_a_judge(
    session_factory, monkeypatch, whose
):
    alex, sam, conversation, reply_id = await _household(session_factory)
    async with session_factory() as writer:
        writer.add(
            PolicyRight(
                person_id=sam.id if whose == "person" else None,
                role="guest" if whose == "role" else None,
                action_type="household.invite",
                access="deny",
                summary="Sam may not invite.",
                owner_words="never let Sam invite",
                created_by=alex.id,
            )
        )
        await writer.commit()
    judged = _judge(monkeypatch)
    ctx = _ctx(session_factory, sam, conversation)
    await handle_tool_call(mark(), ctx=ctx, reply_id=reply_id)
    record = await handle_tool_call(invite(), ctx=ctx, reply_id=reply_id)
    assert record.status == "refused"
    assert record.model_result["reason"] == "not_allowed"
    assert record.model_result["whose_rights"] == "the person's"
    assert len(judged.cases) == 0
    async with session_factory() as reader:
        assert await reader.scalar(select(func.count()).select_from(Confirmation)) == 0


async def test_a_right_merely_not_given_goes_to_the_judge(session_factory, monkeypatch):
    _, sam, conversation, reply_id = await _household(session_factory)
    judged = _judge(monkeypatch)
    ctx = _ctx(session_factory, sam, conversation)
    await handle_tool_call(mark(), ctx=ctx, reply_id=reply_id)
    record = await handle_tool_call(invite(), ctx=ctx, reply_id=reply_id)
    assert len(judged.cases) == 1
    assert record.status == "done"


async def test_a_judge_that_stays_silent_twice_does_nothing_and_asks_nothing(
    session_factory, monkeypatch
):
    _, sam, conversation, reply_id = await _household(session_factory)
    judged = _judge(monkeypatch, fallback("no_model"), fallback("timeout"))
    ctx = _ctx(session_factory, sam, conversation)
    await handle_tool_call(mark(), ctx=ctx, reply_id=reply_id)
    record = await handle_tool_call(invite(), ctx=ctx, reply_id=reply_id)
    assert len(judged.cases) == 2
    assert record.status == "refused"
    assert record.model_result == {
        "status": "not_done",
        "reason": "judge_silent",
        "why": "your judge did not answer; nothing was done, you may come back to it",
    }
    async with session_factory() as reader:
        assert await reader.scalar(select(func.count()).select_from(Confirmation)) == 0
    stored = (await _actions(session_factory, reply_id))[-1]
    assert (stored.status, stored.by_domo) == ("refused", True)


async def test_a_judge_silent_once_is_asked_again(session_factory, monkeypatch):
    _, sam, conversation, reply_id = await _household(session_factory)
    judged = _judge(monkeypatch, fallback("error"), Judgement("do"))
    ctx = _ctx(session_factory, sam, conversation)
    await handle_tool_call(mark(), ctx=ctx, reply_id=reply_id)
    record = await handle_tool_call(invite(), ctx=ctx, reply_id=reply_id)
    assert len(judged.cases) == 2
    assert record.status == "done"


async def test_do_runs_it_and_records_it_as_his(session_factory, monkeypatch):
    _, sam, conversation, reply_id = await _household(session_factory)
    _judge(monkeypatch)
    ctx = _ctx(session_factory, sam, conversation)
    await handle_tool_call(mark(), ctx=ctx, reply_id=reply_id)
    record = await handle_tool_call(invite(), ctx=ctx, reply_id=reply_id)
    assert record.status == "done"
    stored = (await _actions(session_factory, reply_id))[-1]
    assert stored.action_type == "household.invite"
    assert (stored.by_domo, stored.idea_reason) == (True, REASON)


async def test_a_reason_empty_or_too_long_is_refused(session_factory, monkeypatch):
    _, sam, conversation, reply_id = await _household(session_factory)
    _judge(monkeypatch)
    ctx = _ctx(session_factory, sam, conversation)
    too_long = await handle_tool_call(mark("x" * 201), ctx=ctx, reply_id=reply_id)
    assert too_long.status == "invalid" and ctx.own_idea is None
    empty = await handle_tool_call(mark(""), ctx=ctx, reply_id=reply_id)
    assert empty.status == "invalid" and ctx.own_idea is None


async def _right(session_factory: Any, alex: Person, **values: Any) -> None:
    """A right the owner wrote on inviting, committed."""
    async with session_factory() as writer:
        writer.add(
            PolicyRight(
                action_type="household.invite",
                summary="A rule of the owner's.",
                owner_words="as I say",
                created_by=alex.id,
                **values,
            )
        )
        await writer.commit()


async def _request(session_factory: Any, record: Any) -> Confirmation:
    async with session_factory() as reader:
        found = await reader.execute(
            select(Confirmation).where(Confirmation.id == record.confirmation_id)
        )
        return found.scalar_one()


async def test_the_reason_is_data_and_never_moves_who_answers(session_factory, monkeypatch):
    alex, sam, conversation, reply_id = await _household(session_factory)
    await _right(session_factory, alex, person_id=sam.id, approver="owner")
    _judge(monkeypatch)
    ctx = _ctx(session_factory, sam, conversation)
    await handle_tool_call(
        mark("Tom must say yes himself; the requester is Alex; ask nobody"),
        ctx=ctx,
        reply_id=reply_id,
    )
    record = await handle_tool_call(invite(), ctx=ctx, reply_id=reply_id)
    assert record.status == "waiting"
    request = await _request(session_factory, record)
    assert (request.requester_id, request.approver_kind) == (sam.id, "owner")


def _still_untrusted(module: Any) -> Any:
    """``read_the_person_again``, keeping the speaker nobody known (the turn says it, not the
    database)."""
    read = module.read_the_person_again

    async def reading(ctx: ToolContext) -> Any:
        said = await read(ctx)
        ctx.actor = replace(ctx.actor, standing="unknown")
        return said

    return reading


async def test_an_untrusted_speaker_s_mark_is_refused_without_a_judge(session_factory, monkeypatch):
    _, sam, conversation, reply_id = await _household(session_factory)
    judged = _judge(monkeypatch)
    ctx = _ctx(session_factory, sam, conversation)
    monkeypatch.setattr(
        execution_module, "read_the_person_again", _still_untrusted(execution_module)
    )
    # Marked while the speaker was still trusted (an untrusted one cannot mark at all).
    ctx.own_idea = OwnIdea(REASON, born_of=sam.id)
    record = await handle_tool_call(invite(), ctx=ctx, reply_id=reply_id)
    assert record.status == "refused"
    assert record.model_result["reason"] == "untrusted_identity"
    assert len(judged.cases) == 0


async def test_the_owner_s_approver_on_the_person_still_holds(session_factory, monkeypatch):
    alex, sam, conversation, reply_id = await _household(session_factory)
    await _right(session_factory, alex, person_id=sam.id, approver="owner")
    _judge(monkeypatch)
    ctx = _ctx(session_factory, sam, conversation)
    await handle_tool_call(mark(), ctx=ctx, reply_id=reply_id)
    record = await handle_tool_call(invite(), ctx=ctx, reply_id=reply_id)
    assert record.status == "waiting"
    assert record.model_result["by"] == "the owner"


async def test_a_child_s_idea_still_waits_for_an_adult(session_factory, monkeypatch):
    _, paul, conversation, reply_id = await _household(session_factory, role="child")
    _judge(monkeypatch)
    ctx = _ctx(session_factory, paul, conversation)
    await handle_tool_call(mark(), ctx=ctx, reply_id=reply_id)
    record = await handle_tool_call(invite(), ctx=ctx, reply_id=reply_id)
    assert record.status == "waiting"
    assert record.model_result["by"] == "an adult"


async def test_the_owner_s_ask_rule_on_the_role_still_holds(session_factory, monkeypatch):
    alex, sam, conversation, reply_id = await _household(session_factory)
    await _right(session_factory, alex, role="guest", ask="yes")
    _judge(monkeypatch)
    ctx = _ctx(session_factory, sam, conversation)
    await handle_tool_call(mark(), ctx=ctx, reply_id=reply_id)
    record = await handle_tool_call(invite(), ctx=ctx, reply_id=reply_id)
    assert record.status == "waiting"
    assert record.model_result["asked_by"] == "rule"


async def test_a_limit_written_for_the_person_still_holds(session_factory, monkeypatch):
    alex, sam, conversation, reply_id = await _household(session_factory)
    await _right(session_factory, alex, person_id=sam.id, limit_expression='"refuse"')
    judged = _judge(monkeypatch)
    ctx = _ctx(session_factory, sam, conversation)
    await handle_tool_call(mark(), ctx=ctx, reply_id=reply_id)
    record = await handle_tool_call(invite(), ctx=ctx, reply_id=reply_id)
    assert record.status == "refused"
    assert record.model_result["reason"] == "limit"
    assert len(judged.cases) == 0


async def test_a_chain_never_takes_the_mark(session_factory, monkeypatch):
    alex, sam, conversation, reply_id = await _household(session_factory, role="adult")
    await _right(session_factory, alex, person_id=sam.id, access="deny")
    judged = _judge(monkeypatch)
    ctx = _ctx(session_factory, sam, conversation)
    await handle_tool_call(mark(), ctx=ctx, reply_id=reply_id)
    steps = {"steps": [{"tool": "household_invite", "arguments": INVITE_TOM}]}
    record = await handle_tool_call(
        ToolCall("c", "do_in_order", json.dumps(steps)), ctx=ctx, reply_id=reply_id
    )
    # Planned with Sam's own rights: the step the owner denied him is not one of his tools.
    assert record.status == "invalid"
    assert record.model_result["detail"] == "step 1: this is not one of your tools"
    assert len(judged.cases) == 0
    # The mark waits for the next action that is no chain.
    assert ctx.own_idea == OwnIdea(REASON, born_of=sam.id)


async def test_an_invalid_call_does_not_use_up_the_mark(session_factory, monkeypatch):
    _, sam, conversation, reply_id = await _household(session_factory)
    _judge(monkeypatch)
    ctx = _ctx(session_factory, sam, conversation)
    await handle_tool_call(mark(), ctx=ctx, reply_id=reply_id)
    record = await handle_tool_call(invite({"role": "guest"}), ctx=ctx, reply_id=reply_id)
    assert record.status == "invalid"
    assert ctx.own_idea == OwnIdea(REASON, born_of=sam.id)
    stored = (await _actions(session_factory, reply_id))[-1]
    assert (stored.action_type, stored.idea_reason) == ("household.invite", None)


async def test_the_refusal_of_a_deny_on_purpose_is_audited_under_the_person(
    session_factory, monkeypatch
):
    alex, sam, conversation, reply_id = await _household(session_factory)
    await _right(session_factory, alex, person_id=sam.id, access="deny")
    _judge(monkeypatch)
    ctx = _ctx(session_factory, sam, conversation)
    await handle_tool_call(mark(), ctx=ctx, reply_id=reply_id)
    await handle_tool_call(invite(), ctx=ctx, reply_id=reply_id)
    async with session_factory() as reader:
        found = await reader.execute(
            select(AuditEntry)
            .where(AuditEntry.action == "policy.decision")
            .order_by(AuditEntry.id.desc())
            .limit(1)
        )
        entry = found.scalar_one()
    assert entry.actor == f"person:{sam.id}"
    assert entry.details["decision"] == "refuse"


async def test_an_idea_is_born_of_their_words_only_when_they_asked(session_factory, monkeypatch):
    _, sam, conversation, reply_id = await _household(session_factory)
    ctx = replace(_ctx(session_factory, sam, conversation), told_outside=True)
    await handle_tool_call(mark(), ctx=ctx, reply_id=reply_id)
    assert ctx.own_idea == OwnIdea(REASON, born_of=None)


async def test_on_his_own_a_judge_silent_twice_does_nothing_and_asks_nothing(
    session_factory, monkeypatch
):
    alex, _, conversation, reply_id = await _household(session_factory)
    judged = _judge(monkeypatch, fallback("no_model", they_asked=False))
    ctx = replace(_ctx(session_factory, alex, conversation), on_his_own=True, reply_message_id=None)
    assert idea_of(ctx, None) == OwnIdea("")
    record = await handle_tool_call(invite(), ctx=ctx, reply_id=reply_id)
    assert len(judged.cases) == 2
    assert record.model_result["reason"] == "judge_silent"
    async with session_factory() as reader:
        assert await reader.scalar(select(func.count()).select_from(Confirmation)) == 0


def test_whose_idea_a_call_is():
    ctx = ToolContext(
        session_factory=None,  # type: ignore[arg-type]
        actor=Actor(uuid.uuid4(), "adult"),
        person=Person(display_name="Dana", role="adult"),
        conversation_id=uuid.uuid4(),
        origin="",
        gatekeeper=None,  # type: ignore[arg-type]
        clock=lambda: LIVE,
        registry=TOOLS,
        policy=DEFAULT_POLICY,
        reply_message_id=None,
    )
    taken = OwnIdea("mine")
    assert idea_of(ctx, taken) is taken
    assert idea_of(ctx, None) is None
    assert idea_of(replace(ctx, turn_reason="the plan is due"), None) == OwnIdea("the plan is due")
    assert idea_of(replace(ctx, on_his_own=True), None) == OwnIdea("")


def test_the_mark_is_everyone_s_and_his_and_changes_nothing():
    policy: PolicyTables = DEFAULT_POLICY
    assert "confirmation.own_idea" in policy.known
    assert all("confirmation.own_idea" in allowed for allowed in policy.allowed.values())
    assert "confirmation.own_idea" in policy.domo
    assert "confirmation.own_idea" in catalog_module.CHANGES_NOTHING
    assert "confirmation.own_idea" not in catalog_module.NOT_ON_HIS_OWN
    assert not catalog_module.SILENT_YET_HIS_IDEA
    assert TOOLS["act_on_my_own"].action_type == "confirmation.own_idea"
