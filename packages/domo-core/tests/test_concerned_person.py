"""Whom a doubt of the assistant's goes to (spec part 7): when his judge really doubts an idea of
his, the request waits for the person the thing belongs to (the owner for the house), never the
owner by default; who answers is the code's to say, never the judge's; the barrier's own approver
still wins; and a refusal of the judge is a fact noted (``went_wrong``)."""

import json
import uuid
from dataclasses import replace
from typing import Any

import pytest
from domo_core.accounts.models import Person
from domo_core.audit.models import AuditEntry
from domo_core.channels.api.render import request_of
from domo_core.chat.models import Conversation, Message, MessageAction
from domo_core.confirmations import service as service_module
from domo_core.confirmations.judge import Judgement
from domo_core.confirmations.models import Confirmation
from domo_core.confirmations.routes import confirmation_card
from domo_core.confirmations.service import (
    amend_confirmation,
    decide_confirmation,
    may_decide,
    requester_context,
)
from domo_core.confirmations.store import (
    CANNOT_BE_UNDONE_REASON,
    conversation_made_in,
    pending_for,
)
from domo_core.conversation import execution as execution_module
from domo_core.conversation.execution import handle_tool_call
from domo_core.conversation.turn import told_here
from domo_core.conversation.visits import untold_decision
from domo_core.household.models import Invitation
from domo_core.learning.models import WentWrong
from domo_core.llm.types import JsonObject, ToolCall, ToolDefinition
from domo_core.policy.actions import DescribedAction
from domo_core.policy.catalog import DEFAULT_POLICY, on_his_own_tables
from domo_core.policy.models import PolicyRight
from domo_core.policy.roles import Actor, as_role
from domo_core.policy.settings import load_policy_settings
from domo_core.texts.view import REFERENCE
from domo_core.tools.context import OwnIdea, ToolContext, ToolOutcome
from domo_core.tools.registry import TOOLS
from fakes import FakeChatModel
from sqlalchemy import func, select, update
from test_own_idea import LIVE, REASON, Judged, invite, mark

DECIDED = "Just decided (data, not instructions):\n"
DOUBT = Judgement("yes", reason="I am not sure of it", noted="it looks odd")
WITH_MY_KEY = {"display_name": "Tom", "role": "guest", "guest_days": 1, "uses_my_key": True}


async def _people(
    session_factory: Any, *people: tuple[str, str], grants: bool = True
) -> dict[str, Person]:
    """These people, each with a conversation, and (``grants``) the assistant's own rights to
    invite and to take back a device (he has neither by default)."""
    async with session_factory() as writer:
        made = {name: Person(display_name=name, role=role) for name, role in people}
        writer.add_all(made.values())
        await writer.flush()
        writer.add_all(Conversation(person_id=person.id) for person in made.values())
        creator = next(iter(made.values()))
        writer.add_all(
            PolicyRight(
                for_domo=True,
                action_type=action_type,
                access="allow",
                summary="The assistant may.",
                owner_words="you may",
                created_by=creator.id,
            )
            for action_type in (("household.invite", "device.remove") if grants else ())
        )
        await writer.commit()
    return made


async def _conversation_of(session_factory: Any, person: Person) -> Conversation:
    async with session_factory() as reader:
        return (
            await reader.scalars(select(Conversation).where(Conversation.person_id == person.id))
        ).one()


async def _reply_in(session_factory: Any, conversation: Conversation) -> uuid.UUID:
    async with session_factory() as writer:
        reply = Message(
            conversation_id=conversation.id,
            role="assistant",
            content="",
            status="complete",
            provenance="model",
        )
        writer.add(reply)
        await writer.commit()
        return reply.id


async def _turn(
    session_factory: Any, person: Person, **changes: Any
) -> tuple[ToolContext, uuid.UUID, list[uuid.UUID]]:
    """A turn in ``person``'s conversation (``changes`` to its context), its reply, and who was
    notified of news in it."""
    conversation = await _conversation_of(session_factory, person)
    reply_id = await _reply_in(session_factory, conversation)
    notified: list[uuid.UUID] = []
    ctx = ToolContext(
        session_factory=session_factory,
        actor=Actor(person.id, as_role(person.role)),
        person=person,
        conversation_id=conversation.id,
        origin="",
        gatekeeper=None,  # type: ignore[arg-type]
        clock=lambda: LIVE,
        registry={**TOOLS, "take_back": TakeBack()},
        policy=DEFAULT_POLICY,
        reply_message_id=uuid.uuid4(),
        notify=notified.append,
    )
    return replace(ctx, **changes), reply_id, notified


def _judge(monkeypatch: pytest.MonkeyPatch, *answers: Judgement) -> Judged:
    judged = Judged(*answers)
    monkeypatch.setattr(execution_module, "judge", judged)
    return judged


class TakeBack:
    """A test-only tool: take back a device of the person its argument names (a thing of theirs,
    ``device.remove`` with the fact ``person``)."""

    definition = ToolDefinition(
        name="take_back",
        description="Take back a device.",
        parameters={
            "type": "object",
            "properties": {"person": {"type": "string"}},
            "additionalProperties": False,
        },
    )
    action_type = "device.remove"

    async def describe(self, arguments: JsonObject, actor: Actor, ctx: Any) -> DescribedAction:
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target="device:phone",
            effect="self_profile",
            destination="household",
            # As the real tool writes a fact of kind ``person``: ``person:<id>``.
            facts={"person": f"person:{arguments['person']}", "is_self": False},
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        return ToolOutcome("done", {}, {"key": "action.card.done", "params": {}})


def take_back(person: Person) -> ToolCall:
    return ToolCall("t", "take_back", json.dumps({"person": str(person.id)}))


async def _requests(session_factory: Any) -> list[Confirmation]:
    async with session_factory() as reader:
        return list(await reader.scalars(select(Confirmation)))


async def _facts(session_factory: Any) -> list[WentWrong]:
    async with session_factory() as reader:
        return list(await reader.scalars(select(WentWrong)))


async def test_on_his_own_a_doubt_about_the_house_waits_for_the_owner(session_factory, monkeypatch):
    people = await _people(session_factory, ("Alex", "owner"), ("Sam", "guest"))
    alex = people["Alex"]
    _judge(monkeypatch, DOUBT)
    ctx, reply_id, _ = await _turn(session_factory, alex, on_his_own=True, reply_message_id=None)
    record = await handle_tool_call(invite(), ctx=ctx, reply_id=reply_id)
    assert record.status == "waiting"
    assert record.model_result["by"] == "you"
    [request] = await _requests(session_factory)
    assert (request.concerned_id, request.approver_kind, request.by_domo) == (alex.id, "self", True)
    assert (request.requester_id, request.conversation_id) == (alex.id, ctx.conversation_id)
    async with session_factory() as reader:
        settings = await load_policy_settings(reader)
        listed = await pending_for(reader, alex.id, ctx.conversation_id, settings, LIVE)
    assert [c.id for c in listed] == [request.id]
    assert may_decide(request, alex, LIVE) and not may_decide(request, people["Sam"], LIVE)


async def test_a_doubt_about_dana_s_thing_waits_for_dana_not_the_owner(
    session_factory, monkeypatch
):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"))
    alex, dana = people["Alex"], people["Dana"]
    _judge(monkeypatch, DOUBT)
    ctx, reply_id, notified = await _turn(
        session_factory, alex, on_his_own=True, reply_message_id=None
    )
    record = await handle_tool_call(take_back(dana), ctx=ctx, reply_id=reply_id)
    assert record.status == "waiting"
    assert record.model_result["by"] == "the person it concerns"
    assert record.model_result["who"] == "Dana"
    # Nothing of the request is shown here: it is Dana's to answer, in her conversation.
    assert record.confirmation_id is None
    [request] = await _requests(session_factory)
    danas = await _conversation_of(session_factory, dana)
    # It waits in her conversation; who asked stays who spoke (on his own: Alex's turn).
    assert (request.conversation_id, request.requester_id) == (danas.id, alex.id)
    assert (request.concerned_id, request.approver_kind, request.by_domo) == (dana.id, "self", True)
    assert request.last_shown_message_id is None
    assert may_decide(request, dana, LIVE) and not may_decide(request, alex, LIVE)
    async with session_factory() as reader:
        settings = await load_policy_settings(reader)
        listed = await pending_for(reader, dana.id, danas.id, settings, LIVE)
        news_at = await reader.scalar(select(Person.news_at).where(Person.id == dana.id))
    assert [c.id for c in listed] == [request.id]
    assert news_at == LIVE
    assert notified == [dana.id]
    # The line of the turn says it waits for her, by her name.
    async with session_factory() as reader:
        [line] = await reader.scalars(
            select(MessageAction).where(MessageAction.message_id == reply_id)
        )
    assert (line.status, line.confirmation_id) == ("waiting", None)
    assert line.summary == {"key": "action.card.waiting.concerned", "params": {"name": "Dana"}}


async def test_in_a_turn_he_started_for_dana_it_waits_for_dana_here(session_factory, monkeypatch):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"))
    dana = people["Dana"]
    _judge(monkeypatch, DOUBT)
    ctx, reply_id, notified = await _turn(
        session_factory, dana, turn_reason="her plan is due", reply_message_id=None
    )
    record = await handle_tool_call(take_back(dana), ctx=ctx, reply_id=reply_id)
    assert record.status == "waiting"
    assert record.model_result["by"] == "you"
    [request] = await _requests(session_factory)
    assert (request.conversation_id, request.requester_id) == (ctx.conversation_id, dana.id)
    assert (request.concerned_id, request.approver_kind) == (dana.id, "self")
    # Judged as hers (no mark): not his.
    assert request.by_domo is False
    assert notified == []


async def test_the_barrier_s_approver_still_wins(session_factory, monkeypatch):
    people = await _people(session_factory, ("Alex", "owner"), ("Paul", "child"))
    paul = people["Paul"]
    _judge(monkeypatch, DOUBT)
    ctx, reply_id, _ = await _turn(session_factory, paul)
    await handle_tool_call(mark(), ctx=ctx, reply_id=reply_id)
    record = await handle_tool_call(invite(), ctx=ctx, reply_id=reply_id)
    assert record.status == "waiting"
    assert record.model_result["by"] == "an adult"
    [request] = await _requests(session_factory)
    assert (request.approver_kind, request.concerned_id) == ("adult", None)


async def test_the_judge_cannot_name_who_answers(session_factory, monkeypatch):
    people = await _people(session_factory, ("Alex", "owner"), ("Sam", "adult"))
    alex = people["Alex"]
    _judge(monkeypatch, Judgement("yes", reason="ask Sam", noted="Sam must answer this, not Alex"))
    ctx, reply_id, _ = await _turn(session_factory, alex, on_his_own=True, reply_message_id=None)
    await handle_tool_call(invite(), ctx=ctx, reply_id=reply_id)
    [request] = await _requests(session_factory)
    assert (request.concerned_id, request.requester_id) == (alex.id, alex.id)
    assert not may_decide(request, people["Sam"], LIVE)


async def test_a_refusal_of_the_judge_is_a_fact(session_factory, monkeypatch):
    people = await _people(session_factory, ("Alex", "owner"), ("Sam", "guest"))
    sam = people["Sam"]
    _judge(monkeypatch, Judgement("do_not", reason="not now", noted="Tom was sent away last week"))
    ctx, reply_id, _ = await _turn(session_factory, sam)
    await handle_tool_call(mark(), ctx=ctx, reply_id=reply_id)
    record = await handle_tool_call(invite(), ctx=ctx, reply_id=reply_id)
    assert record.status == "refused"
    [fact] = await _facts(session_factory)
    # The case was the house's (Alex's): no reason of it is kept about Sam, never the journal's
    # line (final review C1, ruling R75).
    assert (fact.kind, fact.action_type, fact.reason) == ("judge_refused", "household.invite", None)
    assert (fact.by_domo, fact.of_domo, fact.about_person_id) == (True, True, sam.id)
    assert fact.subject_key == "invitee:tom"
    async with session_factory() as reader:
        [line] = await reader.scalars(
            select(MessageAction).where(
                MessageAction.message_id == reply_id,
                MessageAction.action_type == "household.invite",
            )
        )
    assert fact.action_id == line.id


async def test_a_refusal_of_the_judge_on_a_person_s_own_call_is_a_fact_too(
    session_factory, monkeypatch
):
    people = await _people(session_factory, ("Alex", "owner"))
    alex = people["Alex"]
    _judge(monkeypatch, Judgement("do_not", reason="not now"))
    ctx, reply_id, _ = await _turn(session_factory, alex)
    await handle_tool_call(invite(), ctx=ctx, reply_id=reply_id)
    [fact] = await _facts(session_factory)
    assert (fact.kind, fact.of_domo, fact.about_person_id, fact.reason) == (
        "judge_refused",
        False,
        alex.id,
        "not now",
    )


async def test_with_no_owner_now_nobody_is_asked_and_it_is_a_fact(session_factory, monkeypatch):
    people = await _people(session_factory, ("Dana", "adult"))
    dana = people["Dana"]
    _judge(monkeypatch, DOUBT)
    ctx, reply_id, _ = await _turn(session_factory, dana, on_his_own=True, reply_message_id=None)
    record = await handle_tool_call(invite(), ctx=ctx, reply_id=reply_id)
    assert record.status == "refused"
    assert record.model_result["reason"] == "nobody_to_ask"
    assert await _requests(session_factory) == []
    [fact] = await _facts(session_factory)
    assert (fact.kind, fact.action_type, fact.of_domo) == ("failed", "household.invite", True)


async def test_a_guest_s_idea_the_judge_doubts_waits_for_the_owner_and_his_yes_makes_it_as_his(
    session_factory, monkeypatch
):
    people = await _people(session_factory, ("Alex", "owner"), ("Sam", "guest"))
    alex, sam = people["Alex"], people["Sam"]
    _judge(monkeypatch, DOUBT)
    ctx, reply_id, notified = await _turn(session_factory, sam)
    await handle_tool_call(mark(), ctx=ctx, reply_id=reply_id)
    record = await handle_tool_call(invite(WITH_MY_KEY), ctx=ctx, reply_id=reply_id)
    assert record.status == "waiting"
    assert record.model_result["who"] == "Alex"
    [request] = await _requests(session_factory)
    alexs = await _conversation_of(session_factory, alex)
    # An invitation is the house's: the owner answers it, in his conversation; it is his idea,
    # born in Sam's turn (Sam is who asked there).
    assert (request.conversation_id, request.concerned_id) == (alexs.id, alex.id)
    assert (request.by_domo, request.idea_reason, request.requester_id) == (True, REASON, sam.id)
    assert notified == [alex.id]
    deciding: list[Actor] = []
    decide = service_module.decide_now

    async def replayed_for(session: Any, actor: Actor, *args: Any, **kwargs: Any) -> Any:
        deciding.append(actor)
        return await decide(session, actor, *args, **kwargs)

    monkeypatch.setattr(service_module, "decide_now", replayed_for)
    outcome, ran = await decide_confirmation(
        session_factory=session_factory,
        confirmation_id=request.id,
        decider=alex,
        approve=True,
        via="button",
        reply_message_id=None,
        ctx_factory=requester_context(ctx),
        registry=ctx.registry,
        policy=DEFAULT_POLICY,
        now=LIVE,
    )
    # Made, not refused at the replay: replayed as his, with Sam's socle as at first (a right
    # merely not given lifted), in that turn, not one of his own.
    assert outcome == "executed", ran
    assert [(actor.subject, actor.role) for actor in deciding] == [
        ("domo", "adult"),
        ("person", "guest"),
    ]
    assert deciding[1].person_id == sam.id
    async with session_factory() as reader:
        [made] = await reader.scalars(select(Invitation))
        done = await reader.get(Confirmation, request.id)
        decided = await reader.scalars(
            select(AuditEntry).where(
                AuditEntry.action == "policy.decision",
                AuditEntry.details["action_type"].astext == "household.invite",
            )
        )
        decided_by = {entry.actor for entry in decided}
        news_at = await reader.scalar(select(Person.news_at).where(Person.id == sam.id))
    # Run in Sam's turn, as at first: his invitation, and "my key" still means Sam's.
    assert (made.created_by, made.uses_inviter_key) == (sam.id, True)
    # The assistant's act: the barrier's lines on it are his, and it is counted once as his.
    assert decided_by == {"domo"}
    assert done is not None
    assert (done.status, done.by_domo, done.counted_for_domo) == ("executed", True, True)
    # Sam, in whose turn it was born, is told what the owner decided (news for him).
    assert news_at is not None
    async with session_factory() as reader:
        assert await conversation_made_in(reader, done) == ctx.conversation_id


async def test_his_idea_replayed_at_the_yes_keeps_the_speaker_s_socle(session_factory, monkeypatch):
    people = await _people(session_factory, ("Alex", "owner"), ("Sam", "guest"))
    alex, sam = people["Alex"], people["Sam"]
    _judge(monkeypatch, DOUBT)
    ctx, reply_id, _ = await _turn(session_factory, sam)
    await handle_tool_call(mark(), ctx=ctx, reply_id=reply_id)
    await handle_tool_call(invite(), ctx=ctx, reply_id=reply_id)
    [request] = await _requests(session_factory)
    # Meanwhile the owner forbids Sam to invite, on purpose.
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
    outcome, _ = await decide_confirmation(
        session_factory=session_factory,
        confirmation_id=request.id,
        decider=alex,
        approve=True,
        via="button",
        reply_message_id=None,
        ctx_factory=requester_context(ctx),
        registry=ctx.registry,
        policy=DEFAULT_POLICY,
        now=LIVE,
    )
    assert outcome == "refused"
    async with session_factory() as reader:
        assert await reader.scalar(select(func.count()).select_from(Invitation)) == 0


async def test_a_refusal_on_the_person_s_side_is_audited_under_them(session_factory, monkeypatch):
    people = await _people(session_factory, ("Alex", "owner"), ("Sam", "guest"))
    alex, sam = people["Alex"], people["Sam"]
    async with session_factory() as writer:
        writer.add(
            PolicyRight(
                person_id=sam.id,
                action_type="household.invite",
                limit_expression='"refuse"',
                summary="Never for Sam.",
                owner_words="as I say",
                created_by=alex.id,
            )
        )
        await writer.commit()
    judged = _judge(monkeypatch)
    ctx, reply_id, _ = await _turn(session_factory, sam)
    ctx.own_idea = OwnIdea(REASON, born_of=sam.id)
    record = await handle_tool_call(invite(), ctx=ctx, reply_id=reply_id)
    assert record.status == "refused" and record.model_result["whose_rights"] == "the person's"
    assert judged.cases == []
    async with session_factory() as reader:
        entry = (
            await reader.scalars(
                select(AuditEntry)
                .where(AuditEntry.action == "policy.decision")
                .order_by(AuditEntry.id.desc())
                .limit(1)
            )
        ).one()
    assert entry.actor == f"person:{sam.id}"


async def test_a_request_in_someone_else_s_conversation_holds_no_judge_reason(
    session_factory, monkeypatch
):
    # Ruling R29: the judge wrote its reason from Sam's case; Alex reads only the request.
    people = await _people(session_factory, ("Alex", "owner"), ("Sam", "guest"))
    _judge(monkeypatch, Judgement("warn", reason="Sam said Tom was sad lately"))
    ctx, reply_id, _ = await _turn(session_factory, people["Sam"])
    await handle_tool_call(mark(), ctx=ctx, reply_id=reply_id)
    await handle_tool_call(invite(), ctx=ctx, reply_id=reply_id)
    [request] = await _requests(session_factory)
    assert request.concerned_id == people["Alex"].id
    # Ruling R69: the fact that it was warned is kept (Alex's answer is remembered for Alex), never
    # the judge's words; R72: Alex is shown the code's own line that it cannot be undone.
    assert (request.ask, request.ask_why, request.warned) == ("yes", CANNOT_BE_UNDONE_REASON, True)
    assert "Tom" not in (request.ask_why or "")


async def test_an_unmarked_call_in_a_turn_he_began_stays_the_person_s_wherever_it_waits(
    session_factory, monkeypatch
):
    # Ruling R31: no grant to the assistant; Dana may invite, the house's thing is Alex's.
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"), grants=False)
    alex, dana = people["Alex"], people["Dana"]
    _judge(monkeypatch, DOUBT)
    ctx, reply_id, _ = await _turn(
        session_factory, dana, turn_reason="her plan is due", reply_message_id=None
    )
    record = await handle_tool_call(invite(), ctx=ctx, reply_id=reply_id)
    assert record.status == "waiting" and record.model_result["who"] == "Alex"
    [request] = await _requests(session_factory)
    alexs = await _conversation_of(session_factory, alex)
    assert (request.conversation_id, request.concerned_id) == (alexs.id, alex.id)
    # Hers, not his: moving it to Alex changes nobody's act.
    assert (request.by_domo, request.requester_id, request.idea_reason) == (False, dana.id, None)
    outcome, ran = await decide_confirmation(
        session_factory=session_factory,
        confirmation_id=request.id,
        decider=alex,
        approve=True,
        via="button",
        reply_message_id=None,
        ctx_factory=requester_context(ctx),
        registry=ctx.registry,
        policy=DEFAULT_POLICY,
        now=LIVE,
    )
    assert outcome == "executed", ran
    async with session_factory() as reader:
        [made] = await reader.scalars(select(Invitation))
    assert made.created_by == dana.id


async def test_his_change_of_an_idea_born_in_a_turn_keeps_that_turn_s_rules(
    session_factory, monkeypatch
):
    people = await _people(session_factory, ("Alex", "owner"), ("Sam", "guest"))
    alex = people["Alex"]
    judged = _judge(monkeypatch, DOUBT)
    ctx, reply_id, _ = await _turn(session_factory, people["Sam"])
    await handle_tool_call(mark(), ctx=ctx, reply_id=reply_id)
    await handle_tool_call(invite(), ctx=ctx, reply_id=reply_id)
    [request] = await _requests(session_factory)
    deciding: list[tuple[Actor, Any]] = []
    decide = service_module.decide_now

    async def changed_for(session: Any, actor: Actor, action: Any, policy: Any, now: Any) -> Any:
        deciding.append((actor, policy))
        return await decide(session, actor, action, policy, now)

    monkeypatch.setattr(service_module, "decide_now", changed_for)
    # On his own, in Alex's conversation, he changes it.
    his, his_reply, _ = await _turn(
        session_factory,
        alex,
        on_his_own=True,
        reply_message_id=None,
        policy=on_his_own_tables(DEFAULT_POLICY),
    )
    his.domo_message_id = his_reply
    change = {"request_id": str(request.id), "arguments": {"guest_days": 2}, "ask_again": False}
    record = await handle_tool_call(
        ToolCall("a", "confirmation_amend", json.dumps(change)), ctx=his, reply_id=his_reply
    )
    assert record.status == "done", record.model_result
    # Decided with the plain rules of the turn it was born in, for him and for Sam.
    assert deciding and all(policy.domo == DEFAULT_POLICY.domo for _, policy in deciding)
    assert {actor.subject for actor, _ in deciding} == {"domo", "person"}
    # The judge is told it is his idea, and that Sam lacks the right.
    idea = judged.cases[-1].as_json()["idea"]
    assert (idea["of"], idea["reason"], idea["they_lack_the_right"]) == ("domo", REASON, True)
    new = next(c for c in await _requests(session_factory) if c.id != request.id)
    # Still born in Sam's turn, still Alex's to answer, without the judge's reason (R29).
    assert (new.requester_id, new.concerned_id, new.idea_reason) == (
        people["Sam"].id,
        alex.id,
        REASON,
    )
    assert new.ask_why is None


# --- fix round 2: changes of a request that waits for the person it concerns --------------------


async def _yes(ctx: ToolContext, request: Confirmation, decider: Person) -> Any:
    return await decide_confirmation(
        session_factory=ctx.session_factory,
        confirmation_id=request.id,
        decider=decider,
        approve=True,
        via="button",
        reply_message_id=None,
        ctx_factory=requester_context(ctx),
        registry=ctx.registry,
        policy=DEFAULT_POLICY,
        now=LIVE,
    )


async def _danas_invite_waiting_for_alex(
    session_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, Person], ToolContext, Confirmation]:
    """A turn he began for Dana: her invitation (no mark) waits for Alex, the house's owner."""
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"), grants=False)
    _judge(monkeypatch, DOUBT)
    ctx, reply_id, _ = await _turn(
        session_factory, people["Dana"], turn_reason="her plan is due", reply_message_id=None
    )
    await handle_tool_call(invite(), ctx=ctx, reply_id=reply_id)
    [request] = await _requests(session_factory)
    return people, ctx, request


async def _alex_changes(
    session_factory: Any, alex: Person, request: Confirmation, **changes: Any
) -> tuple[Any, Any, Any]:
    ctx, reply_id, _ = await _turn(session_factory, alex, change_grant=request.id)
    ctx.domo_message_id = reply_id
    ctx.reply_message_id = None
    return await amend_confirmation(
        session_factory=session_factory,
        confirmation_id=request.id,
        new_arguments=changes,
        ask_again=False,
        ctx=ctx,
        now=LIVE,
    )


async def test_the_concerned_person_s_change_asked_again_still_waits_for_them(
    session_factory, monkeypatch
):
    people, danas, request = await _danas_invite_waiting_for_alex(session_factory, monkeypatch)
    alex, dana = people["Alex"], people["Dana"]
    outcome, _, new = await _alex_changes(session_factory, alex, request, guest_days=2)
    assert outcome in ("asked_again", "approver_changed"), outcome
    assert new is not None and new.id != request.id
    alexs = await _conversation_of(session_factory, alex)
    # Who answers is the code's again: Alex, in his conversation, listed for him.
    assert (new.approver_kind, new.concerned_id, new.conversation_id) == ("self", alex.id, alexs.id)
    assert (new.requester_id, new.by_domo) == (dana.id, False)
    assert may_decide(new, alex, LIVE) and not may_decide(new, dana, LIVE)
    async with session_factory() as reader:
        settings = await load_policy_settings(reader)
        listed = await pending_for(reader, alex.id, alexs.id, settings, LIVE)
        # Still made in Dana's turn: run and woken there.
        assert await conversation_made_in(reader, new) == danas.conversation_id
    assert [c.id for c in listed] == [new.id]


async def test_the_concerned_person_s_change_that_runs_runs_in_the_turn_it_was_made_in(
    session_factory, monkeypatch
):
    people, danas, request = await _danas_invite_waiting_for_alex(session_factory, monkeypatch)
    alex, dana = people["Alex"], people["Dana"]
    _judge(monkeypatch, Judgement("do"))
    ran_in: list[uuid.UUID] = []
    execute = service_module._run_and_settle

    async def running(*args: Any, **kwargs: Any) -> Any:
        ran_in.append(kwargs["ctx"].conversation_id)
        return await execute(*args, **kwargs)

    monkeypatch.setattr(service_module, "_run_and_settle", running)
    outcome, _, _ = await _alex_changes(session_factory, alex, request, guest_days=2)
    assert outcome == "executed", outcome
    assert ran_in == [danas.conversation_id]
    async with session_factory() as reader:
        [made] = await reader.scalars(select(Invitation))
    assert made.created_by == dana.id


async def test_his_change_that_now_needs_the_owner_waits_in_the_turn_it_was_made_in(
    session_factory, monkeypatch
):
    people = await _people(session_factory, ("Alex", "owner"), ("Sam", "guest"))
    alex, sam = people["Alex"], people["Sam"]
    _judge(monkeypatch, DOUBT)
    ctx, reply_id, _ = await _turn(session_factory, sam)
    await handle_tool_call(mark(), ctx=ctx, reply_id=reply_id)
    await handle_tool_call(invite(), ctx=ctx, reply_id=reply_id)
    [request] = await _requests(session_factory)
    # Now the owner wants to say yes to every invitation of the assistant's first.
    async with session_factory() as writer:
        await writer.execute(
            update(PolicyRight)
            .where(PolicyRight.for_domo.is_(True), PolicyRight.action_type == "household.invite")
            .values(approver="owner")
        )
        await writer.commit()
    his, his_reply, _ = await _turn(
        session_factory,
        alex,
        on_his_own=True,
        reply_message_id=None,
        policy=on_his_own_tables(DEFAULT_POLICY),
    )
    his.domo_message_id = his_reply
    change = {"request_id": str(request.id), "arguments": {"guest_days": 2}, "ask_again": False}
    await handle_tool_call(
        ToolCall("a", "confirmation_amend", json.dumps(change)), ctx=his, reply_id=his_reply
    )
    new = next(c for c in await _requests(session_factory) if c.id != request.id)
    assert (new.approver_kind, new.concerned_id, new.requester_id) == ("owner", None, sam.id)
    # In Sam's conversation, the one it was made in, never Alex's as Sam's.
    assert new.conversation_id == ctx.conversation_id
    async with session_factory() as reader:
        assert await conversation_made_in(reader, new) == ctx.conversation_id


async def test_a_request_whose_turn_cannot_be_known_is_never_run_in_the_approver_s(
    session_factory, monkeypatch
):
    people = await _people(session_factory, ("Alex", "owner"), ("Sam", "guest"))
    alex = people["Alex"]
    _judge(monkeypatch, DOUBT)
    ctx, reply_id, _ = await _turn(session_factory, people["Sam"])
    await handle_tool_call(mark(), ctx=ctx, reply_id=reply_id)
    await handle_tool_call(invite(), ctx=ctx, reply_id=reply_id)
    [request] = await _requests(session_factory)
    async with session_factory() as writer:
        await writer.execute(
            update(Confirmation).where(Confirmation.id == request.id).values(message_id=None)
        )
        await writer.commit()
        stored = await writer.get(Confirmation, request.id)
        assert stored is not None
        assert await conversation_made_in(writer, stored) is None
    outcome, _ = await _yes(ctx, request, alex)
    assert outcome == "refused"
    async with session_factory() as reader:
        assert await reader.scalar(select(func.count()).select_from(Invitation)) == 0


def test_words_that_answer_a_request_of_someone_else_s_turn_wake_its_turn():
    alex, dana = uuid.uuid4(), uuid.uuid4()
    here = uuid.uuid4()
    # His own, asked of the person whose conversation it waits in: told by this reply.
    his = Confirmation(by_domo=True, requester_id=alex, conversation_id=here)
    assert told_here(his, conversation_id=here, person_id=alex)
    # His, made on his own in Alex's turn, answered by Dana here: Alex's turn is woken.
    assert not told_here(his, conversation_id=here, person_id=dana)
    # A person's, answered by the person it concerns: its turn is woken.
    hers = Confirmation(by_domo=False, requester_id=dana, conversation_id=here)
    assert not told_here(hers, conversation_id=here, person_id=alex)


async def test_the_one_who_asked_sees_it_waits_for_the_person_it_concerns(
    session_factory, monkeypatch
):
    people, _, request = await _danas_invite_waiting_for_alex(session_factory, monkeypatch)
    dana = people["Dana"]
    async with session_factory() as reader:
        settings = await load_policy_settings(reader)
        shown = await request_of(reader, request.id, dana, LIVE)
    card = confirmation_card(request, viewer=dana, settings=settings, texts=REFERENCE, now=LIVE)
    assert card.can_decide is False
    assert card.action.text == REFERENCE("action.card.waiting.the_concerned")
    assert card.asked is not None and "your" not in card.asked.lower()
    assert shown is not None and shown["answer_with"] == "concerned"


async def test_on_his_own_the_concerned_person_s_answer_is_no_news_for_the_owner(
    session_factory, monkeypatch
):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"))
    alex, dana = people["Alex"], people["Dana"]
    _judge(monkeypatch, DOUBT)
    ctx, reply_id, _ = await _turn(session_factory, alex, on_his_own=True, reply_message_id=None)
    await handle_tool_call(take_back(dana), ctx=ctx, reply_id=reply_id)
    [request] = await _requests(session_factory)
    outcome, _ = await _yes(ctx, request, dana)
    assert outcome == "executed"
    async with session_factory() as reader:
        news_at = await reader.scalar(select(Person.news_at).where(Person.id == alex.id))
        untold = await untold_decision(reader, alex.id, since=None, told=())
    assert news_at is None and untold is False


# --- fix round 3 --------------------------------------------------------------------------------


def chain_of(*calls: ToolCall) -> ToolCall:
    steps = [{"tool": call.name, "arguments": json.loads(call.arguments)} for call in calls]
    return ToolCall("c", "do_in_order", json.dumps({"steps": steps}))


async def test_a_doubted_chain_about_someone_else_s_thing_waits_for_the_person_of_the_turn(
    session_factory, monkeypatch
):
    # Ruling R34: a chain is never moved to a person concerned.
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"), grants=False)
    dana = people["Dana"]
    _judge(monkeypatch, DOUBT)
    ctx, reply_id, notified = await _turn(
        session_factory, dana, turn_reason="her plan is due", reply_message_id=None
    )
    record = await handle_tool_call(chain_of(invite()), ctx=ctx, reply_id=reply_id)
    assert record.status == "waiting", record.model_result
    assert record.model_result["by"] == "you"
    [request] = await _requests(session_factory)
    assert (request.approver_kind, request.concerned_id) == ("self", None)
    assert (request.requester_id, request.conversation_id) == (dana.id, ctx.conversation_id)
    assert may_decide(request, dana, LIVE)
    assert notified == []


async def test_a_doubted_chain_on_his_own_waits_for_the_conversation_s_person(
    session_factory, monkeypatch
):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"))
    alex = people["Alex"]
    _judge(monkeypatch, DOUBT)
    ctx, reply_id, _ = await _turn(session_factory, alex, on_his_own=True, reply_message_id=None)
    record = await handle_tool_call(chain_of(take_back(people["Dana"])), ctx=ctx, reply_id=reply_id)
    assert record.status == "waiting", record.model_result
    [request] = await _requests(session_factory)
    assert request.concerned_id is None
    assert (request.requester_id, request.conversation_id) == (alex.id, ctx.conversation_id)
    assert may_decide(request, alex, LIVE) and not may_decide(request, people["Dana"], LIVE)


async def test_the_note_and_the_question_survive_a_change_of_the_person_it_concerns(
    session_factory, monkeypatch
):
    people, _, request = await _danas_invite_waiting_for_alex(session_factory, monkeypatch)
    alex = people["Alex"]
    async with session_factory() as writer:
        await writer.execute(
            update(Confirmation)
            .where(Confirmation.id == request.id)
            .values(ask_reason="Is Tom a friend of the house?")
        )
        await writer.commit()
    ctx, reply_id, _ = await _turn(session_factory, alex, change_grant=request.id)
    ctx.domo_message_id = reply_id
    ctx.reply_message_id = None
    _, _, new = await amend_confirmation(
        session_factory=session_factory,
        confirmation_id=request.id,
        new_arguments={"guest_days": 2},
        ask_again=True,
        ctx=ctx,
        now=LIVE,
        note="two days, not more: firm",
    )
    assert new is not None and new.id != request.id
    assert new.approver_note == "two days, not more: firm"
    assert new.ask_reason == "Is Tom a friend of the house?"


async def _sam_s_take_back_waiting_for_alex(
    session_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, Person], ToolContext, Confirmation]:
    """Sam's turn: the assistant marks taking back a device of Alex's; his judge doubts it; it
    waits for Alex, in Alex's conversation."""
    people = await _people(session_factory, ("Alex", "owner"), ("Sam", "guest"))
    _judge(monkeypatch, DOUBT)
    ctx, reply_id, _ = await _turn(session_factory, people["Sam"])
    await handle_tool_call(mark(), ctx=ctx, reply_id=reply_id)
    await handle_tool_call(take_back(people["Alex"]), ctx=ctx, reply_id=reply_id)
    [request] = await _requests(session_factory)
    assert request.concerned_id == people["Alex"].id
    return people, ctx, request


async def _his_change_in_alex_s(
    session_factory: Any, alex: Person, request: Confirmation, **changes: Any
) -> list[uuid.UUID]:
    his, his_reply, notified = await _turn(
        session_factory,
        alex,
        on_his_own=True,
        reply_message_id=None,
        policy=on_his_own_tables(DEFAULT_POLICY),
    )
    his.domo_message_id = his_reply
    change = {"request_id": str(request.id), "arguments": changes, "ask_again": False}
    record = await handle_tool_call(
        ToolCall("a", "confirmation_amend", json.dumps(change)), ctx=his, reply_id=his_reply
    )
    assert record.status == "done", record.model_result
    return notified


async def _news_at(session_factory: Any, person: Person) -> Any:
    async with session_factory() as reader:
        return await reader.scalar(select(Person.news_at).where(Person.id == person.id))


async def test_his_change_gives_no_news_to_the_person_of_his_turn(session_factory, monkeypatch):
    people = await _people(session_factory, ("Alex", "owner"), ("Sam", "guest"))
    alex = people["Alex"]
    _judge(monkeypatch, DOUBT)
    ctx, reply_id, _ = await _turn(session_factory, people["Sam"])
    await handle_tool_call(mark(), ctx=ctx, reply_id=reply_id)
    await handle_tool_call(invite(), ctx=ctx, reply_id=reply_id)
    [request] = await _requests(session_factory)
    async with session_factory() as writer:
        await writer.execute(update(Person).where(Person.id == alex.id).values(news_at=None))
        await writer.commit()
    notified = await _his_change_in_alex_s(session_factory, alex, request, guest_days=2)
    # It still waits for Alex, here, in his turn: no news, nobody notified.
    assert await _news_at(session_factory, alex) is None
    assert notified == []


async def test_his_change_that_makes_it_the_requester_s_again_tells_them(
    session_factory, monkeypatch
):
    people, sams, request = await _sam_s_take_back_waiting_for_alex(session_factory, monkeypatch)
    alex, sam = people["Alex"], people["Sam"]
    notified = await _his_change_in_alex_s(session_factory, alex, request, person=str(sam.id))
    new = next(c for c in await _requests(session_factory) if c.id != request.id)
    # Sam's own device now: Sam answers it, in his conversation, and is told.
    assert (new.concerned_id, new.conversation_id) == (sam.id, sams.conversation_id)
    assert await _news_at(session_factory, sam) is not None
    assert notified == [sam.id]


async def test_a_reroute_at_the_yes_lets_the_barrier_s_approver_answer(
    session_factory, monkeypatch
):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"))
    alex, dana = people["Alex"], people["Dana"]
    _judge(monkeypatch, DOUBT)
    ctx, reply_id, _ = await _turn(session_factory, alex, on_his_own=True, reply_message_id=None)
    await handle_tool_call(take_back(dana), ctx=ctx, reply_id=reply_id)
    [request] = await _requests(session_factory)
    async with session_factory() as writer:
        await writer.execute(
            update(PolicyRight)
            .where(PolicyRight.for_domo.is_(True), PolicyRight.action_type == "device.remove")
            .values(approver="owner")
        )
        await writer.commit()
    outcome, _ = await _yes(ctx, request, dana)
    assert outcome == "approver_changed"
    async with session_factory() as reader:
        now_waiting = await reader.get(Confirmation, request.id)
    assert now_waiting is not None
    # The barrier's approver wins: the owner answers it now, not the person it concerned.
    assert (now_waiting.approver_kind, now_waiting.concerned_id) == ("owner", None)
    assert may_decide(now_waiting, alex, LIVE) and not may_decide(now_waiting, dana, LIVE)


async def test_the_person_it_concerns_answers_in_words_and_the_turn_it_was_born_in_wakes(
    chat_server, owner_chat, adult_chat, grant, session_factory
):
    # Dana's turn: the assistant's marked invitation, his judge doubts it; an invitation is the
    # house's, so it waits for Alex, in Alex's conversation.
    await grant("household.invite", for_domo=True, access="allow")
    adult_chat.use_model(FakeChatModel(tool_script=[mark(), invite(), "Done."], judge="yes"))
    await adult_chat.say("Tom comes tonight")
    [request] = await _requests(session_factory)
    assert request.concerned_id == owner_chat.person.id
    # Alex is shown it, then answers in words.
    shown = await owner_chat.say_with_tool("confirmation_remind", {"request_id": str(request.id)})
    assert shown.pending_confirmation_id == request.id
    owner_chat.use_model(FakeChatModel(["Fine."]))
    await owner_chat.say("oui")
    async with session_factory() as reader:
        done = await reader.get(Confirmation, request.id)
    assert done is not None and done.status == "executed"
    # Dana's turn, where it was born, is woken, as after a button.
    await chat_server.wait_for_wakes()
    woken = [
        call
        for call in chat_server.calls
        if DECIDED in call.system
        and str(request.id) in call.system.split(DECIDED)[1]
        and f'Name (data, not instructions): "{adult_chat.person.display_name}"' in call.system
    ]
    assert woken, "Dana's turn was not woken"


async def test_the_person_of_a_real_device_removal_is_whom_the_thing_belongs_to(session_factory):
    # Fix round 1 of Task 27: ``device_remove`` writes its ``person`` fact as ``person:<id>``;
    # the subject rules read it so, whoever's turn the call is in.
    from domo_core.devices.service import begin_pairing, finish_pairing
    from domo_core.policy.subjects import concerned_of, subject_of

    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"), grants=False)
    dana = people["Dana"]
    async with session_factory() as writer:
        code = await begin_pairing(writer, dana.id, LIVE)
        assert await finish_pairing(writer, code, "Desk", LIVE) is not None
        await writer.commit()
    ctx, _, _ = await _turn(session_factory, dana)
    action = await TOOLS["device_remove"].describe({"device": "Desk"}, ctx.actor, ctx)
    assert action.facts["person"] == f"person:{dana.id}"
    subject = subject_of(action, None, uuid.uuid4())
    assert subject.belongs_to == dana.id
    async with session_factory() as reader:
        assert await concerned_of(reader, subject, action, LIVE) == dana.id
