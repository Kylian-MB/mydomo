"""The house, devices, triggers, money (final review of the judge-first branch): an idea of his
to take back someone's device goes through the real tool, judged as his (D1); a right on setting
something for the house is read whole, for him too (D2); the owner reads what a trigger or an
agreement for the house really does before his yes (D3); cancelling a trigger for the house needs
what setting it needs (D5, ruling R74); his removal of an account is recorded as his (D7)."""

import json
from typing import Any

import pytest
from domo_core.audit.models import AuditEntry
from domo_core.confirmations.describe import describe_type
from domo_core.confirmations.judge import Judgement
from domo_core.conversation.execution import handle_tool_call
from domo_core.devices.models import Device
from domo_core.llm.types import ToolCall
from domo_core.madetools.models import MadeToolTrigger
from domo_core.policy.models import PolicyRight
from domo_core.texts.view import CatalogTexts
from domo_core.tools.context import InvalidArguments
from sqlalchemy import select
from test_concerned_person import DOUBT, _judge, _people, _requests, _turn
from test_made_tools_tools import HOUSE_AT, _the_house, _timed, _yes, trigger_rows
from test_own_idea import mark


async def _tablet_of(session_factory: Any, person: Any) -> Device:
    async with session_factory() as writer:
        device = Device(person_id=person.id, name="Tablet", secret_hash=f"h-{person.id}")
        writer.add(device)
        await writer.commit()
        return device


def take_back_the_tablet_of(name: str) -> ToolCall:
    return ToolCall("d", "device_remove", json.dumps({"device": "Tablet", "person": name}))


async def _device(session_factory: Any, device: Device) -> Device:
    async with session_factory() as reader:
        found = await reader.get(Device, device.id)
        assert found is not None
        return found


async def test_his_idea_to_take_back_dana_s_tablet_waits_for_her_then_is_his(
    session_factory, monkeypatch
):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"))
    alex, dana = people["Alex"], people["Dana"]
    tablet = await _tablet_of(session_factory, dana)
    _judge(monkeypatch, DOUBT)
    ctx, reply_id, _ = await _turn(session_factory, alex)
    await handle_tool_call(mark("Dana lost her tablet"), ctx=ctx, reply_id=reply_id)
    record = await handle_tool_call(take_back_the_tablet_of("Dana"), ctx=ctx, reply_id=reply_id)
    # Judged as his, doubted: it waits for Dana, whose tablet it is.
    assert record.status == "waiting", record.model_result
    [request] = await _requests(session_factory)
    assert (request.concerned_id, request.by_domo) == (dana.id, True)
    assert (await _device(session_factory, tablet)).removed_at is None
    outcome, ran = await _yes(ctx, request.id, dana)
    assert outcome == "executed", ran
    assert (await _device(session_factory, tablet)).removed_at is not None


async def test_his_idea_the_judge_lets_go_takes_the_tablet_back_as_his(
    session_factory, monkeypatch
):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"))
    alex, dana = people["Alex"], people["Dana"]
    tablet = await _tablet_of(session_factory, dana)
    _judge(monkeypatch, Judgement("do"))
    ctx, reply_id, _ = await _turn(session_factory, alex)
    await handle_tool_call(mark("Dana lost her tablet"), ctx=ctx, reply_id=reply_id)
    record = await handle_tool_call(take_back_the_tablet_of("Dana"), ctx=ctx, reply_id=reply_id)
    assert record.status == "done", record.model_result
    assert (await _device(session_factory, tablet)).removed_at is not None
    async with session_factory() as reader:
        actors = set(
            await reader.scalars(select(AuditEntry.actor).where(AuditEntry.action.like("device.%")))
        )
    # Audited as his act, never as Alex's.
    assert actors and f"person:{alex.id}" not in actors


async def _house_set_right(session_factory: Any, by: Any, **right: Any) -> None:
    async with session_factory() as writer:
        writer.add(
            PolicyRight(
                action_type="house.set",
                summary="Setting things for the house.",
                owner_words="as I say",
                created_by=by.id,
                **right,
            )
        )
        await writer.commit()


async def test_a_deny_of_house_set_written_for_him_holds_on_his_own(session_factory):
    people = await _people(session_factory, ("Alex", "owner"), grants=False)
    alex = people["Alex"]
    await _house_set_right(session_factory, alex, for_domo=True, access="deny")
    ctx, _, _ = await _turn(session_factory, alex, on_his_own=True, reply_message_id=None)
    async with session_factory() as session:
        with pytest.raises(InvalidArguments):
            await ctx.may_set_for_the_house(session)


async def test_a_right_on_house_set_that_asks_first_is_no_at_once(session_factory):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"), grants=False)
    alex, dana = people["Alex"], people["Dana"]
    await _house_set_right(session_factory, alex, person_id=dana.id, access="allow", ask="yes")
    ctx, _, _ = await _turn(session_factory, dana)
    async with session_factory() as session:
        assert await ctx.may_set_for_the_house(session) is False


async def test_a_plain_right_on_house_set_sets_at_once(session_factory):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"), grants=False)
    alex, dana = people["Alex"], people["Dana"]
    await _house_set_right(session_factory, alex, person_id=dana.id, access="allow")
    ctx, _, _ = await _turn(session_factory, dana)
    async with session_factory() as session:
        assert await ctx.may_set_for_the_house(session) is True


@pytest.mark.parametrize("lang", ["en", "fr"])
def test_the_owner_reads_the_rule_and_what_a_trigger_gives(lang):
    said = describe_type(
        "tool.launch_when",
        {
            "name": "chores",
            "repeat": "FREQ=WEEKLY;BYDAY=MO",
            "for": "house",
            "arguments": {"room": "kitchen", "loud": True},
        },
        CatalogTexts(lang),
    )
    assert "FREQ=WEEKLY;BYDAY=MO" in said
    # What it is given, as data: a text in quotes, never part of the sentence.
    assert '"kitchen"' in said and "loud" in said


@pytest.mark.parametrize(("lang", "day"), [("en", "Friday"), ("fr", "vendredi")])
def test_the_owner_reads_the_days_an_agreement_for_the_house_holds_on(lang, day):
    said = describe_type(
        "agreement.keep",
        {
            "tool": "pizza",
            "operation": "order",
            "amount_cents_at_most": 4000,
            "times": 1,
            "per": "week",
            "weekdays": [5],
            "for": "house",
        },
        CatalogTexts(lang),
        {"for_house": True},
    )
    assert day in said


async def _house_trigger(session_factory: Any, monkeypatch: Any) -> tuple[dict, MadeToolTrigger]:
    people = await _the_house(session_factory)
    _judge(monkeypatch, Judgement("do"))
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    record = await handle_tool_call(_timed(HOUSE_AT), ctx=ctx, reply_id=reply_id)
    assert record.status == "done", record.model_result
    [trigger] = await trigger_rows(session_factory)
    assert trigger.for_person_id is None
    return people, trigger


async def test_cancelling_a_trigger_for_the_house_waits_for_the_owner(session_factory, monkeypatch):
    people, trigger = await _house_trigger(session_factory, monkeypatch)
    ctx, reply_id, _ = await _turn(session_factory, people["Dana"])
    cancel = _timed({"name": "chores", "cancel": str(trigger.id)})
    record = await handle_tool_call(cancel, ctx=ctx, reply_id=reply_id)
    assert record.status == "waiting", record.model_result
    [still] = await trigger_rows(session_factory)
    assert still.state == "waiting"
    [request] = await _requests(session_factory)
    assert request.approver_kind == "owner"
    # On the owner's yes it is cancelled.
    outcome, ran = await _yes(ctx, request.id, people["Alex"])
    assert outcome == "executed", ran
    [after] = await trigger_rows(session_factory)
    assert after.state == "cancelled"


async def test_the_owner_cancels_a_trigger_for_the_house_at_once(session_factory, monkeypatch):
    people, trigger = await _house_trigger(session_factory, monkeypatch)
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    cancel = _timed({"name": "chores", "cancel": str(trigger.id)})
    record = await handle_tool_call(cancel, ctx=ctx, reply_id=reply_id)
    assert record.status == "done", record.model_result
