"""An action of a person that undoes what the assistant did or decided himself (spec 9.1, 9.2): the
result he reads says so, and the code notes it as a fact, from one place
(``conversation.execution.store_action``, ``learning.undone.undoes_his``); no tool is edited."""

import json
from dataclasses import replace
from typing import Any

from domo_core.accounts.models import Person
from domo_core.chat.models import MessageAction
from domo_core.confirmations.judge import Judgement
from domo_core.confirmations.service import decide_confirmation, requester_context
from domo_core.conversation import execution as execution_module
from domo_core.conversation.execution import handle_tool_call
from domo_core.learning import undone as undone_module
from domo_core.learning.models import WentWrong
from domo_core.llm.types import JsonObject, ToolCall, ToolDefinition
from domo_core.policy.actions import DescribedAction
from domo_core.policy.catalog import DEFAULT_POLICY
from domo_core.policy.models import PolicyRight
from domo_core.policy.roles import Actor
from domo_core.tools.context import ToolContext, ToolOutcome
from sqlalchemy import select
from test_concerned_person import _conversation_of, _judge, _people, _reply_in, _turn
from test_memory_tools import BOILER
from test_own_idea import invite

UNDOES = "undoes"
CANCEL_WORDS = "Cancel that reminder, I never asked for it."
HIS_REASON = "Alex leaves on a trip on Friday"


async def facts(session_factory: Any) -> list[WentWrong]:
    async with session_factory() as reader:
        found = await reader.scalars(select(WentWrong).order_by(WentWrong.at, WentWrong.kind))
        return list(found.all())


async def action_of(session_factory: Any, action_type: str) -> MessageAction:
    async with session_factory() as reader:
        return (
            await reader.scalars(
                select(MessageAction).where(MessageAction.action_type == action_type)
            )
        ).one()


def call(name: str, arguments: JsonObject) -> ToolCall:
    return ToolCall(f"c-{name}", name, json.dumps(arguments))


async def his_plan(session_factory: Any, alex: Person) -> str:
    """A plan the assistant set alone, in a turn he began himself for Alex."""
    ctx, reply_id, _ = await _turn(
        session_factory, alex, reply_message_id=None, turn_reason=HIS_REASON
    )
    record = await handle_tool_call(
        call("plan_set", {"why": "Ask about the trip.", "on_return": True}),
        ctx=ctx,
        reply_id=reply_id,
    )
    assert record.status == "done"
    return str(record.model_result["id"])


# --- a plan --------------------------------------------------------------------------------------


async def test_cancelling_a_plan_he_set_alone_tells_him_and_notes_it(session_factory):
    alex = (await _people(session_factory, ("Alex", "owner"), grants=False))["Alex"]
    plan_id = await his_plan(session_factory, alex)
    plan_set = await action_of(session_factory, "plan.set")
    assert (plan_set.by_domo, plan_set.idea_reason) == (False, HIS_REASON)

    ctx, reply_id, _ = await _turn(session_factory, alex, said=CANCEL_WORDS)
    record = await handle_tool_call(
        call("plan_cancel", {"id": plan_id}), ctx=ctx, reply_id=reply_id
    )
    assert record.status == "done"
    told = str(record.model_result[UNDOES])
    assert told.startswith("this undoes what you had done (or decided) yourself on ")
    assert told.endswith(f"{plan_set.created_at.date().isoformat()}: plan.set")
    # Nothing of it on the card a person sees.
    assert UNDOES not in json.dumps(record.card)

    [fact] = await facts(session_factory)
    assert (fact.kind, fact.action_type, fact.action_id) == ("undone", "plan.cancel", plan_set.id)
    assert (fact.by_person_id, fact.by_domo, fact.of_domo) == (alex.id, False, True)
    assert (fact.about_person_id, fact.subject_key) == (alex.id, f"plan:{plan_id}")
    # Why it was undone, in the person's own words of that turn.
    assert fact.reason == CANCEL_WORDS


async def test_his_own_undoing_is_not_a_mistake(session_factory):
    alex = (await _people(session_factory, ("Alex", "owner"), grants=False))["Alex"]
    plan_id = await his_plan(session_factory, alex)
    ctx, reply_id, _ = await _turn(
        session_factory, alex, reply_message_id=None, turn_reason="the trip was called off"
    )
    record = await handle_tool_call(
        call("plan_cancel", {"id": plan_id}), ctx=ctx, reply_id=reply_id
    )
    assert record.status == "done"
    assert UNDOES not in record.model_result
    assert await facts(session_factory) == []


# --- a memory ------------------------------------------------------------------------------------


async def test_forgetting_a_memory_a_person_asked_him_to_keep_is_nothing(session_factory):
    alex = (await _people(session_factory, ("Alex", "owner"), grants=False))["Alex"]
    ctx, reply_id, _ = await _turn(session_factory, alex, said="Remember the boiler whistles.")
    kept = await handle_tool_call(
        call("memory_keep", BOILER),
        ctx=ctx,
        reply_id=reply_id,
    )
    assert kept.status == "done"
    ctx, reply_id, _ = await _turn(session_factory, alex, said="Forget about the boiler.")
    forgotten = await handle_tool_call(
        call("memory_forget", {"ids": [kept.model_result["id"]]}), ctx=ctx, reply_id=reply_id
    )
    assert forgotten.model_result == {"status": "done", "forgotten": 1}
    assert await facts(session_factory) == []


# --- an address ----------------------------------------------------------------------------------


class _Close:
    """A test-only ``address_close`` (``net.close``), that needs no gatekeeper: done, or failed."""

    action_type = "net.close"

    def __init__(self, *, fails: bool = False) -> None:
        self.fails = fails
        self.definition = ToolDefinition(
            name="close_it",
            description="Close an address.",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
        )

    async def describe(self, arguments: JsonObject, actor: Actor, ctx: Any) -> DescribedAction:
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target="box.lan",
            effect="write",
            destination="household",
            facts={"host": "box.lan", "port": 8123},
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        if self.fails:
            return ToolOutcome("failed", {}, {"key": "action.card.failed", "params": {}})
        return ToolOutcome("done", {"closed": True}, {"key": "action.card.done", "params": {}})


async def opened_by_him_for(session_factory: Any, person: Person) -> MessageAction:
    """An address the assistant opened as his own act, in ``person``'s conversation."""
    reply_id = await _reply_in(session_factory, await _conversation_of(session_factory, person))
    async with session_factory() as writer:
        opened = MessageAction(
            message_id=reply_id,
            tool_name="address_open",
            action_type="net.open",
            status="done",
            summary={"key": "action.card.done", "params": {}},
            by_domo=True,
            subject_key="address:box.lan:8123",
        )
        writer.add(opened)
        await writer.commit()
        return opened


async def test_closing_an_address_he_opened_is_noted_under_the_address(session_factory):
    people = await _people(session_factory, ("Alex", "owner"), ("Sam", "guest"), grants=False)
    alex, sam = people["Alex"], people["Sam"]
    opened = await opened_by_him_for(session_factory, sam)
    ctx, reply_id, _ = await _turn(session_factory, alex, said="Close the box, please.")
    ctx = replace(ctx, registry={**ctx.registry, "close_it": _Close()})
    record = await handle_tool_call(call("close_it", {}), ctx=ctx, reply_id=reply_id)
    assert record.status == "done"
    assert str(record.model_result[UNDOES]).endswith(": net.open")
    # Nothing of Sam's conversation is told: the type and the day only.
    assert "Sam" not in str(record.model_result[UNDOES])
    [fact] = await facts(session_factory)
    assert (fact.kind, fact.subject_key, fact.action_id) == (
        "undone",
        "address:box.lan:8123",
        opened.id,
    )
    # Sam's conversation is where it was done; Alex undid it.
    assert (fact.about_person_id, fact.by_person_id) == (sam.id, alex.id)


async def test_an_undoing_that_fails_undoes_nothing(session_factory):
    people = await _people(session_factory, ("Alex", "owner"), grants=False)
    alex = people["Alex"]
    await opened_by_him_for(session_factory, alex)
    ctx, reply_id, _ = await _turn(session_factory, alex, said="Close the box, please.")
    ctx = replace(ctx, registry={**ctx.registry, "close_it": _Close(fails=True)})
    record = await handle_tool_call(call("close_it", {}), ctx=ctx, reply_id=reply_id)
    assert record.status == "failed"
    assert UNDOES not in record.model_result
    assert [fact.kind for fact in await facts(session_factory)] == ["failed"]


class _Open(_Close):
    """A test-only ``address_open`` (``net.open``) of the same address, that needs no gatekeeper."""

    action_type = "net.open"

    def __init__(self) -> None:
        super().__init__()
        self.definition = ToolDefinition(
            name="open_it",
            description="Open an address.",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
        )


async def test_an_undoing_counts_once_against_the_latest_thing_done(session_factory, monkeypatch):
    # R54: he opens it, Alex closes it (his undone), Alex opens it again himself, then closes it
    # again: the second close undoes Alex's own opening, not his.
    alex = (await _people(session_factory, ("Alex", "owner"), grants=False))["Alex"]
    _judge(monkeypatch, Judgement("do", "their own box"))
    opened = await opened_by_him_for(session_factory, alex)
    tools = {"close_it": _Close(), "open_it": _Open()}
    record = None
    for name in ("close_it", "open_it", "close_it"):
        ctx, reply_id, _ = await _turn(session_factory, alex, said="As I say.")
        ctx = replace(ctx, registry={**ctx.registry, **tools})
        record = await handle_tool_call(call(name, {}), ctx=ctx, reply_id=reply_id)
        assert record.status == "done"
    assert record is not None and UNDOES not in record.model_result
    found = await facts(session_factory)
    assert [(fact.kind, fact.action_id) for fact in found] == [("undone", opened.id)]


async def test_a_plan_he_set_that_the_person_changed_is_theirs_to_cancel(session_factory):
    alex = (await _people(session_factory, ("Alex", "owner"), grants=False))["Alex"]
    plan_id = await his_plan(session_factory, alex)
    ctx, reply_id, _ = await _turn(session_factory, alex, said="Make it about the tickets.")
    changed = await handle_tool_call(
        call("plan_change", {"id": plan_id, "why": "Ask about the tickets."}),
        ctx=ctx,
        reply_id=reply_id,
    )
    assert changed.status == "done"
    ctx, reply_id, _ = await _turn(session_factory, alex, said=CANCEL_WORDS)
    record = await handle_tool_call(
        call("plan_cancel", {"id": plan_id}), ctx=ctx, reply_id=reply_id
    )
    assert record.status == "done" and UNDOES not in record.model_result
    assert await facts(session_factory) == []


# --- what the judge let through, an invitation -------------------------------------------------


async def _invited_by(session_factory: Any, person: Person) -> MessageAction:
    ctx, reply_id, _ = await _turn(session_factory, person, said="Invite Tom for the night.")
    assert (await handle_tool_call(invite(), ctx=ctx, reply_id=reply_id)).status == "done"
    invited = await action_of(session_factory, "household.invite")
    assert (invited.by_domo, invited.idea_reason) == (False, None)
    return invited


async def _may_invite(session_factory: Any, owner: Person, person: Person) -> None:
    """The owner opens inviting to ``person`` (in no role: ruling 14)."""
    async with session_factory() as writer:
        writer.add(
            PolicyRight(
                person_id=person.id,
                action_type="household.invite",
                access="allow",
                summary="Dana may invite.",
                owner_words="Dana may invite",
                created_by=owner.id,
            )
        )
        await writer.commit()


async def _cancelled_by(session_factory: Any, person: Person) -> Any:
    ctx, reply_id, _ = await _turn(session_factory, person, said="Tom is not coming after all.")
    record = await handle_tool_call(
        call("household_cancel_invitation", {"display_name": "Tom"}), ctx=ctx, reply_id=reply_id
    )
    assert record.status == "done"
    return record


async def test_changing_one_s_own_mind_is_no_mistake_of_his(session_factory, monkeypatch):
    # R53: the person's own request, let through by the judge, undone by the same person.
    alex = (await _people(session_factory, ("Alex", "owner"), grants=False))["Alex"]
    _judge(monkeypatch, Judgement("do", "they invite friends often"))
    await _invited_by(session_factory, alex)
    record = await _cancelled_by(session_factory, alex)
    assert UNDOES not in record.model_result
    assert await facts(session_factory) == []


async def test_someone_else_undoing_what_the_judge_let_through_is_a_wrong_verdict(
    session_factory, monkeypatch
):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"), grants=False)
    alex, dana = people["Alex"], people["Dana"]
    await _may_invite(session_factory, alex, dana)
    _judge(monkeypatch, Judgement("do", "Dana invites friends often"))
    invited = await _invited_by(session_factory, dana)
    record = await _cancelled_by(session_factory, alex)
    told = str(record.model_result[UNDOES])
    assert told.startswith("this undoes what your judge had let through on ")
    assert told.endswith(": household.invite") and "Dana" not in told
    [fact] = await facts(session_factory)
    assert (fact.kind, fact.action_id, fact.subject_key) == (
        "wrong_verdict",
        invited.id,
        "invitee:tom",
    )
    assert (fact.by_person_id, fact.about_person_id, fact.of_domo) == (alex.id, dana.id, False)


async def test_a_verdict_the_judge_did_not_give_is_no_wrong_verdict(session_factory, monkeypatch):
    # A silent judge's fallback let it through: no verdict of his was wrong.
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"), grants=False)
    await _may_invite(session_factory, people["Alex"], people["Dana"])
    _judge(monkeypatch, Judgement("do", fallback="timeout"))
    await _invited_by(session_factory, people["Dana"])
    record = await _cancelled_by(session_factory, people["Alex"])
    assert UNDOES not in record.model_result
    assert await facts(session_factory) == []


# --- his invitation: a promotion confirms it, a removal or a cancel undoes it -------------------


async def _his_invitation(session_factory: Any, alex: Person) -> MessageAction:
    """Tom invited by the assistant on his own, in Alex's conversation."""
    ctx, reply_id, _ = await _turn(session_factory, alex, on_his_own=True, reply_message_id=None)
    assert (await handle_tool_call(invite(his=True), ctx=ctx, reply_id=reply_id)).status == "done"
    invited = await action_of(session_factory, "household.invite")
    assert (invited.by_domo, invited.subject_key) == (True, "invitee:tom")
    return invited


async def _tom_joined(session_factory: Any) -> None:
    async with session_factory() as writer:
        writer.add(Person(display_name="Tom", role="guest"))
        await writer.commit()


async def test_promoting_someone_he_invited_is_noted_as_undone(session_factory, monkeypatch):
    # R55 left the invitation out, at a cost: a role given at the invitation and changed later
    # was not noted. A change of role now undoes the role his invitation gave.
    alex = (await _people(session_factory, ("Alex", "owner")))["Alex"]
    _judge(monkeypatch, Judgement("do", "a friend of the house"))
    invited = await _his_invitation(session_factory, alex)
    await _tom_joined(session_factory)
    ctx, reply_id, _ = await _turn(session_factory, alex, said="Tom stays: make him an adult.")
    record = await handle_tool_call(
        call("household_change_role", {"person": "Tom", "role": "adult"}),
        ctx=ctx,
        reply_id=reply_id,
    )
    assert record.status == "done"
    assert UNDOES in record.model_result
    undone = [fact for fact in await facts(session_factory) if fact.kind == "undone"]
    assert [(fact.action_id, fact.subject_key) for fact in undone] == [(invited.id, "invitee:tom")]


async def test_removing_someone_he_invited_matches_by_the_invitee_s_name(
    session_factory, monkeypatch
):
    alex = (await _people(session_factory, ("Alex", "owner")))["Alex"]
    _judge(monkeypatch, Judgement("do", "a friend of the house"))
    invited = await _his_invitation(session_factory, alex)
    await _tom_joined(session_factory)
    ctx, reply_id, _ = await _turn(session_factory, alex, said="Tom has to go.")
    record = await handle_tool_call(
        call("household_remove", {"person": "Tom"}), ctx=ctx, reply_id=reply_id
    )
    assert record.status == "done"
    assert str(record.model_result[UNDOES]).startswith(
        "this undoes what you had done (or decided) yourself on "
    )
    undone = [fact for fact in await facts(session_factory) if fact.kind == "undone"]
    assert [(f.action_id, f.by_person_id, f.of_domo) for f in undone] == [
        (invited.id, alex.id, True)
    ]
    assert undone[0].subject_key == "invitee:tom"


async def test_cancelling_his_invitation_after_a_yes_counts_when_it_runs(
    session_factory, monkeypatch
):
    # R56: the cancel waits for Alex's yes, then runs: it undid his invitation all the same.
    alex = (await _people(session_factory, ("Alex", "owner")))["Alex"]
    _judge(monkeypatch, Judgement("do", "a friend of the house"), Judgement("yes", "sure?"))
    invited = await _his_invitation(session_factory, alex)
    ctx, reply_id, _ = await _turn(session_factory, alex, said="Cancel Tom's invitation.")
    waiting = await handle_tool_call(
        call("household_cancel_invitation", {"display_name": "Tom"}), ctx=ctx, reply_id=reply_id
    )
    assert waiting.status == "waiting" and waiting.confirmation_id is not None
    assert await facts(session_factory) == []
    outcome, _ = await decide_confirmation(
        session_factory=session_factory,
        confirmation_id=waiting.confirmation_id,
        decider=alex,
        approve=True,
        via="button",
        reply_message_id=None,
        ctx_factory=requester_context(ctx),
        registry=ctx.registry,
        policy=DEFAULT_POLICY,
        now=ctx.clock(),
    )
    assert outcome == "executed"
    undone = [fact for fact in await facts(session_factory) if fact.kind == "undone"]
    assert [(f.action_id, f.by_person_id, f.about_person_id) for f in undone] == [
        (invited.id, alex.id, alex.id)
    ]
    assert undone[0].action_type == "household.cancel_invitation"


async def test_a_failing_lookup_never_loses_the_record(session_factory, monkeypatch):
    alex = (await _people(session_factory, ("Alex", "owner"), grants=False))["Alex"]
    plan_id = await his_plan(session_factory, alex)

    async def breaks(*_: Any, **__: Any) -> Any:
        raise RuntimeError("the lookup broke")

    monkeypatch.setattr(undone_module, "undoes_his", breaks)
    monkeypatch.setattr(execution_module, "undoes_his", breaks, raising=False)
    ctx, reply_id, _ = await _turn(session_factory, alex, said=CANCEL_WORDS)
    record = await handle_tool_call(
        call("plan_cancel", {"id": plan_id}), ctx=ctx, reply_id=reply_id
    )
    assert record.status == "done" and record.action_id is not None
    assert UNDOES not in record.model_result
    assert (await action_of(session_factory, "plan.cancel")).id == record.action_id
