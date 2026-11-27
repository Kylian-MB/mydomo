"""What the code notes when something went wrong (spec 9.1, ``learning.record.note_went_wrong``): a
refusal, with who refused and, said in words, their words cut short; a silence (an expiry), apart
from a refusal; a no to a warning, kept with the warning; a failure, the silent ones too; a build
abandoned. Each fact is written in the transaction of what it is about, and never makes it
fail."""

import json
import uuid
from dataclasses import replace
from datetime import timedelta
from typing import Any

import anyio
import pytest
from chat_support import status_of
from domo_core.accounts.models import Person
from domo_core.confirmations import service as service_module
from domo_core.confirmations.models import Confirmation
from domo_core.confirmations.service import decide_confirmation, expire_by_domo, requester_context
from domo_core.conversation import execution as execution_module
from domo_core.conversation.execution import failed_record, handle_tool_call
from domo_core.learning.models import WentWrong
from domo_core.llm.types import JsonObject, ToolCall, ToolDefinition
from domo_core.policy.actions import DescribedAction
from domo_core.policy.catalog import DEFAULT_POLICY
from domo_core.policy.models import PolicyRight
from domo_core.policy.roles import Actor
from domo_core.tools.context import ToolContext, ToolOutcome
from domo_core.tools.registry import TOOLS
from domo_core.web.deps import get_tool_registry
from fakes import FakeChatModel
from sqlalchemy import select, update
from test_chain_run import CONTINUE, TOOL, chain
from test_concerned_person import DOUBT, TakeBack, _judge, _people, _requests, _turn, take_back
from test_own_idea import LIVE, invite, mark
from test_told import RENAME, WARNED, WARNING, told_memories

NO_WORDS = "non"


async def facts(session_factory: Any) -> list[WentWrong]:
    """Every fact of what went wrong, oldest first (committed)."""
    async with session_factory() as reader:
        found = await reader.scalars(select(WentWrong).order_by(WentWrong.at, WentWrong.id))
        return list(found.all())


async def request_of(session_factory: Any, cid: uuid.UUID | str | None) -> Confirmation:
    assert cid is not None
    async with session_factory() as reader:
        found = await reader.get(Confirmation, uuid.UUID(str(cid)))
        assert found is not None
        return found


async def _refuse(session_factory: Any, cid: uuid.UUID, decider: Person, **given: Any) -> Any:
    """A no of ``decider`` to the request ``cid`` (nothing runs: no context is needed)."""
    return await decide_confirmation(
        session_factory=session_factory,
        confirmation_id=cid,
        decider=decider,
        approve=False,
        reply_message_id=None,
        ctx_factory=lambda *_: None,  # type: ignore[arg-type,return-value]
        registry={},
        policy=DEFAULT_POLICY,
        **given,
    )


# --- a refusal ---------------------------------------------------------------------------------


async def test_a_no_in_words_keeps_who_said_it_and_their_words_cut_short(adult_chat, chat_server):
    reply = await adult_chat.say_with_tool(*RENAME, judge="yes: a first time")
    cid = uuid.UUID(str(reply.pending_confirmation_id))
    said = "no, " + "I would rather keep my name as it is " * 20
    outcome, _ = await _refuse(
        chat_server.session_factory,
        cid,
        adult_chat.person,
        via="classifier",
        words=said,
        now=adult_chat.clock(),
    )
    assert outcome == "refused"
    request = await request_of(chat_server.session_factory, cid)
    assert request.refusal_words is not None and len(request.refusal_words) <= 300
    assert said.startswith(request.refusal_words)
    [fact] = await facts(chat_server.session_factory)
    assert (fact.kind, fact.action_type, fact.confirmation_id) == ("refused", "profile.rename", cid)
    assert (fact.by_person_id, fact.by_domo, fact.of_domo) == (adult_chat.person.id, False, False)
    assert (fact.about_person_id, fact.reason) == (adult_chat.person.id, request.refusal_words)
    assert fact.subject_key == request.subject_key
    # The words are data of the refusal, never what the request says it does.
    assert (request.ask_why, request.idea_reason) == ("a first time", None)


async def test_a_no_said_in_the_conversation_keeps_its_words(adult_chat, chat_server):
    reply = await adult_chat.say_with_tool(*RENAME, judge="yes: a first time")
    cid = reply.pending_confirmation_id
    await adult_chat.say(NO_WORDS)
    request = await request_of(chat_server.session_factory, cid)
    assert (request.status, request.refusal_words) == ("refused", NO_WORDS)
    [fact] = await facts(chat_server.session_factory)
    assert (fact.kind, fact.by_person_id, fact.reason) == (
        "refused",
        adult_chat.person.id,
        NO_WORDS,
    )


async def test_a_no_by_button_keeps_no_words(adult_chat, chat_server):
    reply = await adult_chat.say_with_tool(*RENAME, judge="yes: a first time")
    cid = reply.pending_confirmation_id
    assert status_of(await adult_chat.post(f"/confirmations/{cid}/refuse")) == "refused"
    request = await request_of(chat_server.session_factory, cid)
    assert request.refusal_words is None
    [fact] = await facts(chat_server.session_factory)
    assert (fact.kind, fact.by_person_id, fact.reason) == ("refused", adult_chat.person.id, None)


async def test_a_secret_said_with_a_no_is_never_kept(adult_chat, chat_server):
    reply = await adult_chat.say_with_tool(*RENAME, judge="yes: a first time")
    cid = uuid.UUID(str(reply.pending_confirmation_id))
    await _refuse(
        chat_server.session_factory,
        cid,
        adult_chat.person,
        via="phrase",
        words="no, my password is sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789",
        now=adult_chat.clock(),
    )
    request = await request_of(chat_server.session_factory, cid)
    [fact] = await facts(chat_server.session_factory)
    assert (request.refusal_words, fact.reason) == (None, None)


async def test_the_owner_s_no_to_his_own_request_is_of_his(session_factory, monkeypatch):
    people = await _people(session_factory, ("Alex", "owner"), ("Sam", "guest"))
    alex = people["Alex"]
    _judge(monkeypatch, DOUBT)
    ctx, reply_id, _ = await _turn(session_factory, alex, on_his_own=True, reply_message_id=None)
    await handle_tool_call(invite(), ctx=ctx, reply_id=reply_id)
    [request] = await _requests(session_factory)
    assert request.by_domo
    outcome, _ = await _refuse(session_factory, request.id, alex, via="button", now=LIVE)
    assert outcome == "refused"
    [fact] = await facts(session_factory)
    # The owner refused; what was refused was the assistant's own request.
    assert (fact.kind, fact.by_person_id, fact.by_domo, fact.of_domo) == (
        "refused",
        alex.id,
        False,
        True,
    )
    assert (fact.about_person_id, fact.action_type) == (alex.id, "household.invite")


async def test_a_yes_the_rules_refuse_is_the_code_s_refusal(session_factory, monkeypatch):
    people = await _people(session_factory, ("Alex", "owner"), ("Sam", "guest"))
    alex = people["Alex"]
    _judge(monkeypatch, DOUBT)
    ctx, reply_id, _ = await _turn(session_factory, alex, on_his_own=True, reply_message_id=None)
    await handle_tool_call(invite(), ctx=ctx, reply_id=reply_id)
    [request] = await _requests(session_factory)
    # Meanwhile the owner takes back the assistant's right to invite.
    async with session_factory() as writer:
        await writer.execute(
            update(PolicyRight)
            .where(PolicyRight.for_domo, PolicyRight.action_type == "household.invite")
            .values(access="deny")
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
    [fact] = await facts(session_factory)
    # Alex said yes: nobody refused it but the rules.
    assert (fact.kind, fact.by_person_id, fact.by_domo) == ("refused", None, False)
    assert (fact.reason, fact.of_domo, fact.about_person_id) == ("not_allowed", True, alex.id)


# --- a silence ---------------------------------------------------------------------------------


async def test_an_expiry_is_not_a_refusal(adult_chat, chat_server):
    reply = await adult_chat.say_with_tool(*RENAME, judge="yes: a first time")
    cid = reply.pending_confirmation_id
    adult_chat.clock.advance(timedelta(hours=168, seconds=1))
    assert status_of(await adult_chat.post(f"/confirmations/{cid}/approve")) == "expired"
    [fact] = await facts(chat_server.session_factory)
    assert (fact.kind, fact.by_person_id, fact.by_domo, fact.of_domo) == (
        "expired",
        None,
        False,
        False,
    )
    assert (fact.about_person_id, fact.reason) == (adult_chat.person.id, None)


async def test_a_request_he_drops_is_a_silence_of_his(adult_chat, chat_server):
    reply = await adult_chat.say_with_tool(*RENAME, judge="yes: a first time")
    request = await request_of(chat_server.session_factory, reply.pending_confirmation_id)
    async with chat_server.session_factory() as session:
        await expire_by_domo(
            session,
            confirmation_id=request.id,
            requester_id=adult_chat.person.id,
            conversation_id=request.conversation_id,
            now=adult_chat.clock(),
        )
        await session.commit()
    [fact] = await facts(chat_server.session_factory)
    assert (fact.kind, fact.by_person_id, fact.by_domo) == ("expired", None, True)


# --- a warning ---------------------------------------------------------------------------------


async def test_a_no_to_a_warning_is_remembered_as_a_no(adult_chat, chat_server):
    reply = await adult_chat.say_with_tool(*RENAME, judge=WARNING)
    cid = reply.pending_confirmation_id
    await adult_chat.say(NO_WORDS)
    [memory] = await told_memories(chat_server.session_factory, adult_chat.person.id)
    assert (memory.content, memory.told_answer) == (WARNED, "no")
    kinds = {(fact.kind, fact.by_person_id) for fact in await facts(chat_server.session_factory)}
    assert kinds == {("refused", adult_chat.person.id), ("warning_declined", adult_chat.person.id)}
    declined = next(
        f for f in await facts(chat_server.session_factory) if f.kind == "warning_declined"
    )
    assert (declined.action_type, declined.reason) == ("profile.rename", WARNED)
    assert declined.confirmation_id == uuid.UUID(str(cid))


async def test_a_yes_to_a_warning_is_remembered_as_a_yes_and_is_no_fact(adult_chat, chat_server):
    await adult_chat.say_with_tool(*RENAME, judge=WARNING)
    await adult_chat.say("oui")
    [memory] = await told_memories(chat_server.session_factory, adult_chat.person.id)
    assert memory.told_answer == "yes"
    assert await facts(chat_server.session_factory) == []


async def test_a_chain_s_warning_is_kept_under_its_step_s_type(adult_chat, chat_server):
    chain = {"steps": [{"tool": RENAME[0], "arguments": RENAME[1]}]}
    reply = await adult_chat.say_with_tool("do_in_order", chain, judge=WARNING)
    assert reply.pending_confirmation_id is not None
    await adult_chat.say(NO_WORDS)
    [memory] = await told_memories(chat_server.session_factory, adult_chat.person.id)
    # Under the type of its step, never the chain's own (``chain.run``).
    assert (memory.content, memory.action_type, memory.told_answer) == (
        WARNED,
        "profile.rename",
        "no",
    )
    declined = [f for f in await facts(chat_server.session_factory) if f.kind == "warning_declined"]
    assert [f.action_type for f in declined] == ["profile.rename"]


# --- a failure ---------------------------------------------------------------------------------


class _Keep:
    """A test-only silent tool (``memory.keep``) that fails as it is told: raising, or with a
    result that says why."""

    action_type = "memory.keep"

    def __init__(self, result: JsonObject | None = None) -> None:
        self.result = result
        self.definition = ToolDefinition(
            name="keep_it",
            description="Keep it.",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
        )

    async def describe(self, arguments: JsonObject, actor: Actor, ctx: Any) -> DescribedAction:
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target="memory:new",
            effect="self_profile",
            destination="household",
            facts={},
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        if self.result is None:
            raise RuntimeError("the library said: connection reset by peer at 10.0.0.1")
        return ToolOutcome("failed", self.result, {"key": "action.card.failed", "params": {}})


def _keep_it() -> ToolCall:
    return ToolCall("k", "keep_it", json.dumps({}))


@pytest.mark.parametrize(
    ("result", "reason"),
    [
        (None, None),
        ({"reason": "not_found"}, "not_found"),
        # A text, not a code: never kept (it may be a library's).
        ({"reason": "the library said: boom at 10.0.0.1"}, None),
    ],
)
async def test_a_silent_action_that_fails_leaves_a_fact(
    session_factory, result: JsonObject | None, reason: str | None
):
    people = await _people(session_factory, ("Alex", "owner"), grants=False)
    alex = people["Alex"]
    ctx, reply_id, _ = await _turn(session_factory, alex)
    ctx = replace(ctx, registry={**ctx.registry, "keep_it": _Keep(result)})
    record = await handle_tool_call(_keep_it(), ctx=ctx, reply_id=reply_id)
    assert record.status == "failed"
    [fact] = await facts(session_factory)
    assert (fact.kind, fact.action_type, fact.action_id) == (
        "failed",
        "memory.keep",
        record.action_id,
    )
    assert (fact.by_person_id, fact.by_domo, fact.of_domo) == (None, False, False)
    assert (fact.about_person_id, fact.reason) == (alex.id, reason)


async def test_a_decision_that_takes_too_long_says_nothing_was_done_and_leaves_a_fact(
    session_factory, monkeypatch
):
    people = await _people(session_factory, ("Alex", "owner"), grants=False)
    alex = people["Alex"]
    ctx, reply_id, _ = await _turn(session_factory, alex)
    ctx = replace(ctx, registry={**ctx.registry, "keep_it": _Keep({})})

    async def stalls(*_: Any, **__: Any) -> Any:
        await anyio.sleep(30)

    monkeypatch.setattr(execution_module, "_CHECK_SECONDS", 1.0)
    monkeypatch.setattr(execution_module, "decide_now", stalls)
    record = await handle_tool_call(_keep_it(), ctx=ctx, reply_id=reply_id)
    assert (record.status, record.model_result["reason"]) == ("failed", "took_too_long")
    detail = str(record.model_result["detail"])
    assert "nothing was done" in detail and "try again" in detail
    [fact] = await facts(session_factory)
    assert (fact.kind, fact.reason, fact.action_id) == ("failed", "took_too_long", record.action_id)


async def test_a_confirmed_request_that_fails_leaves_one_fact(adult_chat, chat_server, monkeypatch):
    async def fails(tool: Any, tool_name: str, action_type: str, *_: Any) -> Any:
        return failed_record(tool_name, action_type)

    monkeypatch.setattr(service_module, "execute_allowed", fails)
    reply = await adult_chat.say_with_tool(*RENAME, judge="yes: a first time")
    cid = reply.pending_confirmation_id
    await adult_chat.say("oui")
    assert (await request_of(chat_server.session_factory, cid)).status == "failed"
    [fact] = await facts(chat_server.session_factory)
    assert (fact.kind, fact.confirmation_id, fact.by_person_id) == (
        "failed",
        uuid.UUID(str(cid)),
        None,
    )


# --- fix round 1 -------------------------------------------------------------------------------


async def test_a_warned_request_whose_change_is_refused_remembers_no_warning(
    adult_chat, chat_server, confirmation_status
):
    # R48: the person answered with a change, not to the warning; the change was refused, and
    # the whole request with it: nothing is remembered of the warning, and nothing was declined.
    reply = await adult_chat.say_with_tool(*RENAME, judge=WARNING)
    rid = str(reply.pending_confirmation_id)
    change = ToolCall(
        "c1",
        "confirmation_amend",
        json.dumps({"request_id": rid, "arguments": {"name": "Alex C"}, "ask_again": False}),
    )
    adult_chat.use_model(
        FakeChatModel(tool_script=["change", change, "Done."], judge="do_not: not that name")
    )
    await adult_chat.say("ok, but call me Alex C instead")
    assert await confirmation_status(rid) == "refused"
    assert await told_memories(chat_server.session_factory, adult_chat.person.id) == []
    kinds = [fact.kind for fact in await facts(chat_server.session_factory)]
    assert kinds == ["judge_refused"]


async def test_a_chain_warned_over_several_types_remembers_nothing(adult_chat, chat_server):
    # R49: one verdict for the whole chain; with several types of step, which one the warning was
    # about is not known: it is kept under none of them (it would refresh another type's).
    steps = chain(RENAME, ("note_leave", {"recipient": "Owner", "text": "x"}))
    reply = await adult_chat.say_with_tool(TOOL, steps, judge=WARNING)
    assert reply.pending_confirmation_id is not None
    await adult_chat.say(NO_WORDS)
    assert await told_memories(chat_server.session_factory, adult_chat.person.id) == []
    kinds = [fact.kind for fact in await facts(chat_server.session_factory)]
    assert kinds == ["refused"]


async def test_rights_for_an_idea_that_take_too_long_say_nothing_was_done(
    session_factory, monkeypatch
):
    people = await _people(session_factory, ("Alex", "owner"), ("Sam", "guest"))
    _judge(monkeypatch, DOUBT)
    ctx, reply_id, _ = await _turn(session_factory, people["Sam"])

    async def stalls(*_: Any, **__: Any) -> Any:
        await anyio.sleep(30)

    await handle_tool_call(mark(), ctx=ctx, reply_id=reply_id)
    monkeypatch.setattr(execution_module, "_CHECK_SECONDS", 1.0)
    monkeypatch.setattr(execution_module, "load_rights", stalls)
    record = await handle_tool_call(invite(), ctx=ctx, reply_id=reply_id)
    assert (record.status, record.model_result["reason"]) == ("failed", "took_too_long")
    assert "try again" in str(record.model_result["detail"])
    [fact] = await facts(session_factory)
    assert (fact.kind, fact.reason, fact.action_id) == ("failed", "took_too_long", record.action_id)


class _FailingInvite:
    """``household_invite`` as it is, but whose run fails, saying why with a code."""

    def __init__(self) -> None:
        self.real = TOOLS["household_invite"]
        self.definition = self.real.definition
        self.action_type = self.real.action_type

    async def describe(self, arguments: JsonObject, actor: Actor, ctx: Any) -> DescribedAction:
        return await self.real.describe(arguments, actor, ctx)

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        return ToolOutcome(
            "failed", {"reason": "not_found"}, {"key": "action.card.failed", "params": {}}
        )


async def test_a_continued_chain_that_fails_leaves_one_fact_dated_when_it_failed(
    adult_chat, chat_server, grant, revoke_right, confirmation_status
):
    right = await grant("household.invite", person=adult_chat.person, access="allow")
    reply = await adult_chat.say_with_tool(
        TOOL,
        chain(
            ("household_invite", {"display_name": "Tom", "role": "guest"}),
            ("note_leave", {"recipient": "Owner", "text": "x"}),
        ),
    )
    cid = reply.pending_confirmation_id
    await revoke_right(right)
    no = await grant("household.invite", person=adult_chat.person, access="deny")
    assert (await adult_chat.confirm_last(reply)).status == "failed"
    # A day later, allowed again, the continued chain runs and its step fails.
    await revoke_right(no)
    registry = {**TOOLS, "household_invite": _FailingInvite()}
    adult_chat.app.dependency_overrides[get_tool_registry] = lambda: registry
    adult_chat.clock.advance(timedelta(days=1))
    again = await adult_chat.say_with_tool(CONTINUE, {"request_id": str(cid)}, judge="do")
    assert again.actions == [{"tool": CONTINUE, "status": "failed", "reason": "not_found"}]
    assert await confirmation_status(cid) == "failed"
    failed = [f for f in await facts(chat_server.session_factory) if f.kind == "failed"]
    # One fact for each run that failed, none for the record of the call that continued it.
    assert [(f.confirmation_id, f.action_id) for f in failed] == [
        (uuid.UUID(str(cid)), None),
        (uuid.UUID(str(cid)), None),
    ]
    assert (failed[-1].at, failed[-1].reason) == (adult_chat.clock(), "not_found")


# --- fix round 2 -------------------------------------------------------------------------------


class _WhoseYesUnread(TakeBack):
    """``take_back``, whose request may wait for someone else's yes (``AsksWhomItsYes``), but
    whose read of whose it is stalls (``stalls``) or fails."""

    def __init__(self, *, stalls: bool) -> None:
        self.stalls = stalls

    async def whose_yes(self, arguments: JsonObject, ctx: ToolContext) -> Any:
        if self.stalls:
            await anyio.sleep(30)
        raise RuntimeError("the library said: connection reset")


@pytest.mark.parametrize(("stalls", "why"), [(True, "took_too_long"), (False, "not_read")])
async def test_whose_yes_unread_asks_nobody_and_says_try_again(
    session_factory, monkeypatch, stalls: bool, why: str
):
    # Waiting for the person's own yes when the owner's was due would drop who must be asked.
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"))
    _judge(monkeypatch, DOUBT)
    if stalls:
        # Only the read that stalls is about the deadline. The read that fails is not: under a
        # deadline of one second, a machine that stalls may cut a read of the call, which then
        # ends as ``took_too_long`` and not as ``not_read`` (``bounds_for_a_stalling_machine``).
        monkeypatch.setattr(execution_module, "_CHECK_SECONDS", 1.0)
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    ctx = replace(ctx, registry={**ctx.registry, "take_back": _WhoseYesUnread(stalls=stalls)})
    record = await handle_tool_call(take_back(people["Dana"]), ctx=ctx, reply_id=reply_id)
    assert (record.status, record.model_result["reason"]) == ("failed", why)
    detail = str(record.model_result["detail"])
    assert "nothing was done" in detail and "try again" in detail
    assert await _requests(session_factory) == []
    [fact] = await facts(session_factory)
    assert (fact.kind, fact.reason, fact.action_id) == ("failed", why, record.action_id)


async def test_a_warned_request_whose_change_the_rules_refuse_remembers_no_warning(
    adult_chat, chat_server, grant, confirmation_status
):
    # R48 at the barrier: the change is refused by the rules, and the request with it.
    reply = await adult_chat.say_with_tool(*RENAME, judge=WARNING)
    rid = str(reply.pending_confirmation_id)
    await grant("profile.rename", person=adult_chat.person, access="deny")
    change = ToolCall(
        "c1",
        "confirmation_amend",
        json.dumps({"request_id": rid, "arguments": {"name": "Alex C"}, "ask_again": False}),
    )
    adult_chat.use_model(FakeChatModel(tool_script=["change", change, "Done."], judge="do"))
    await adult_chat.say("ok, but call me Alex C instead")
    assert await confirmation_status(rid) == "refused"
    assert await told_memories(chat_server.session_factory, adult_chat.person.id) == []
    assert [(f.kind, f.by_person_id) for f in await facts(chat_server.session_factory)] == [
        ("refused", None)
    ]
