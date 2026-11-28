"""The owner's rule "guests come with their own key" (``keys.guests_bring_own``, design 6.5,
ruling 19): with it, an account made for someone visiting is off every general key, whoever
invited them; without it, an account the assistant gave on his own has no host and the house's
general key pays, within the limits that exist. Either way the assistant may send a visitor a
link on his own (the owner's answer of 2026-11-27: he is told); the rule only changes who pays.
Someone who lives here is never taken off by it, and only the owner sets it."""

import json
import uuid
from datetime import timedelta
from typing import Any

import pytest
from domo_core.accounts.models import Person
from domo_core.conversation.execution import handle_tool_call
from domo_core.household.service import (
    accept_return,
    check_return,
    create_invitation,
    create_return_invitation,
    disable,
)
from domo_core.llm.models import ProviderUser
from domo_core.llm.router import usable_keys
from domo_core.llm.spending import LimitReached
from domo_core.llm.spending_models import ModelCall
from domo_core.llm.types import ToolCall
from domo_core.people.models import PersonAct
from domo_core.policy.models import PolicyRight
from domo_core.policy.settings import (
    GUESTS_OWN_KEY,
    KEYS_SETTINGS,
    InvalidSetting,
    load_policy_settings,
    update_policy_setting,
    validate_setting,
)
from sqlalchemy import select
from test_concerned_person import _judge, _people, _turn
from test_invitations_by_domo import (
    PASSPHRASE,
    _guest_invites_tom,
    _invitation,
    _join,
    _sam_known,
    _token_for,
    _token_of,
)
from test_own_idea import LIVE, mark
from test_spending import add_key, add_limit, add_spent, try_reserve

REASON = "Sam would like to talk to the assistant during his stay"


def _call(tool: str, **arguments: Any) -> ToolCall:
    return ToolCall(uuid.uuid4().hex[:8], tool, json.dumps(arguments))


async def _rule(session_factory: Any, owner: Person, value: bool | None) -> None:
    async with session_factory() as writer:
        await update_policy_setting(writer, key=GUESTS_OWN_KEY, value=value, by=owner.id, now=LIVE)
        await writer.commit()


async def _lines(session_factory: Any, person: Person) -> dict[str, bool]:
    async with session_factory() as reader:
        found = await reader.scalars(
            select(ProviderUser).where(ProviderUser.person_id == person.id)
        )
        return {line.provider_id: line.allowed for line in found}


async def _usable(session_factory: Any, person: Person) -> list[str]:
    async with session_factory() as reader:
        now = await reader.get(Person, person.id)
        assert now is not None
        return [key.id for key in await usable_keys(reader, now)]


async def _the_assistant_invites(session_factory: Any, alex: Person, name: str, role: str) -> Any:
    """In Alex's turn, the assistant invites the record ``name`` on his own idea."""
    ctx, reply_id, _ = await _turn(session_factory, alex)
    await handle_tool_call(mark(REASON), ctx=ctx, reply_id=reply_id)
    record = await handle_tool_call(
        _call("household_invite", person=name, role=role, reason=REASON),
        ctx=ctx,
        reply_id=reply_id,
    )
    assert record.status == "done", record.model_result
    return record


async def _joins(session_factory: Any, record: Any, alex: Person, name: str) -> Person:
    """``name`` opens the link of the invitation ``record`` made (shown once, or waiting)."""
    if record.card_once is not None:
        token = _token_of(record)
    else:
        token = await _token_for(session_factory, await _invitation(session_factory, name), alex)
    return await _join(session_factory, token, username=name.lower())


def test_the_rule_is_true_or_false_and_off_by_default():
    validate_setting(GUESTS_OWN_KEY, True)
    validate_setting(GUESTS_OWN_KEY, None)
    for wrong in ("yes", 1, 0):
        with pytest.raises(InvalidSetting):
            validate_setting(GUESTS_OWN_KEY, wrong)  # type: ignore[arg-type]


async def test_with_the_rule_a_visitors_account_is_off_the_general_key(
    session_factory, monkeypatch
):
    # Someone the assistant invited in a guest's turn: nobody's invitee (``invited_by`` empty).
    people = await _guest_invites_tom(session_factory, monkeypatch)
    alex, sam = people["Alex"], people["Sam"]
    async with session_factory() as reader:
        assert (await load_policy_settings(reader)).guests_bring_own is False
    await _rule(session_factory, alex, True)
    await add_key(session_factory, alex.id, general=True)
    sams_key = await add_key(session_factory, sam.id)
    tom = await _join(
        session_factory, await _token_for(session_factory, await _invitation(session_factory), alex)
    )
    assert tom.invited_by is None
    # Read at the moment of a turn: nothing is written for it.
    assert await _lines(session_factory, tom) == {}
    assert await _usable(session_factory, tom) == []
    assert sams_key.id not in await _usable(session_factory, tom)
    # The owner's own invitation of a guest: off every general key too, his own included.
    async with session_factory() as writer:
        _, token = await create_invitation(
            writer,
            display_name="Paul",
            role="guest",
            guest_days=2,
            created_by=alex.id,
            now=LIVE,
        )
        await writer.commit()
    paul = await _join(session_factory, token, username="paul")
    assert paul.invited_by == alex.id
    assert await _usable(session_factory, paul) == []


async def test_with_the_rule_a_hosts_guest_uses_the_host_s_own_key_never_a_general_one(
    session_factory,
):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"))
    await _rule(session_factory, people["Alex"], True)
    dana = people["Dana"]
    # The general key is the one Dana gave.
    danas_general = await add_key(session_factory, dana.id, general=True)
    danas_key = await add_key(session_factory, dana.id)
    async with session_factory() as writer:
        _, token = await create_invitation(
            writer,
            display_name="Tom",
            role="guest",
            guest_days=2,
            created_by=dana.id,
            now=LIVE,
            uses_inviter_key=True,
        )
        await writer.commit()
    guest = await _join(session_factory, token, username="tom")
    # Dana's line on her general key is not the owner's word: the rule holds.
    assert await _lines(session_factory, guest) == {danas_general.id: True, danas_key.id: True}
    assert await _usable(session_factory, guest) == [danas_key.id]


@pytest.mark.parametrize("rule", [True, False])
async def test_with_the_rule_the_assistant_may_send_a_visitor_a_link(
    session_factory, monkeypatch, rule
):
    people = await _people(session_factory, ("Alex", "owner"))
    alex = people["Alex"]
    _judge(monkeypatch)
    await _rule(session_factory, alex, rule)
    general = await add_key(session_factory, alex.id, general=True)
    await _sam_known(session_factory, standing="visiting")
    record = await _the_assistant_invites(session_factory, alex, "Sam", "guest")
    # The owner is told once: in his own turn, as it happens.
    async with session_factory() as reader:
        [act] = await reader.scalars(select(PersonAct).where(PersonAct.act == "invited"))
    assert (act.by, act.owed_to_owner, act.told_owner_at) == ("domo", True, LIVE)
    sam = await _joins(session_factory, record, alex, "Sam")
    assert sam.invited_by is None
    # What the rule changes is who pays.
    assert await _lines(session_factory, sam) == {}
    assert await _usable(session_factory, sam) == ([] if rule else [general.id])


async def test_without_the_rule_the_house_key_pays_and_the_limits_hold(
    session_factory, monkeypatch
):
    people = await _guest_invites_tom(session_factory, monkeypatch)
    alex = people["Alex"]
    general = await add_key(session_factory, alex.id, general=True)
    tom = await _join(
        session_factory, await _token_for(session_factory, await _invitation(session_factory), alex)
    )
    assert tom.invited_by is None
    assert await _usable(session_factory, tom) == [general.id]
    # A turn of that account is counted on the general key.
    reserved = await try_reserve(session_factory, general, tom, lambda: LIVE, "0.1")
    assert isinstance(reserved, uuid.UUID)
    async with session_factory() as reader:
        call = await reader.get(ModelCall, reserved)
    assert call is not None and (call.provider_id, call.person_id) == (general.id, tom.id)
    # The owner's limit for each guest holds for them.
    await add_limit(
        session_factory, general.id, amount="1", set_by=alex.id, role="guest", split="each"
    )
    await add_spent(session_factory, general.id, "0.95", person_id=tom.id, at=LIVE)
    stopped = await try_reserve(session_factory, general, tom, lambda: LIVE, "0.1")
    assert isinstance(stopped, LimitReached)


async def test_only_the_owner_sets_it(session_factory, monkeypatch):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"))
    _judge(monkeypatch)
    setting = _call("policy_update", key=GUESTS_OWN_KEY, value=True)
    ctx, reply_id, _ = await _turn(session_factory, people["Dana"])
    refused = await handle_tool_call(setting, ctx=ctx, reply_id=reply_id)
    # The barrier: policy.update is the owner's.
    assert (refused.status, refused.model_result["reason"]) == ("refused", "not_allowed")
    # Nor the assistant on his own, in the owner's turn.
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    await handle_tool_call(mark("guests should bring their keys"), ctx=ctx, reply_id=reply_id)
    his = await handle_tool_call(setting, ctx=ctx, reply_id=reply_id)
    # Closed to him: his own rights refuse it, whatever the owner's turn.
    assert (his.status, his.model_result["reason"]) == ("refused", "not_allowed")
    assert his.model_result["whose_rights"] == "yours"
    # Nor through the rules of the keys, which the owner may give.
    # Nor through the rules of the keys, which the owner may give: keys_house writes only
    # KEYS_SETTINGS, and takes no argument for this one, even from a delegate.
    assert GUESTS_OWN_KEY not in KEYS_SETTINGS
    async with session_factory() as writer:
        writer.add(
            PolicyRight(
                person_id=people["Dana"].id,
                action_type="keys.house",
                access="allow",
                summary="Dana may set the rules of the keys.",
                owner_words="she may",
                created_by=people["Alex"].id,
            )
        )
        await writer.commit()
    ctx, reply_id, _ = await _turn(session_factory, people["Dana"])
    keys = await handle_tool_call(
        _call("keys_house", **{GUESTS_OWN_KEY: True}), ctx=ctx, reply_id=reply_id
    )
    assert (keys.status, keys.model_result["reason"]) == ("invalid", "invalid")
    assert GUESTS_OWN_KEY not in keys.model_result["detail"]
    async with session_factory() as reader:
        assert (await load_policy_settings(reader)).guests_bring_own is False
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    done = await handle_tool_call(setting, ctx=ctx, reply_id=reply_id)
    assert done.status == "done", done.model_result
    async with session_factory() as reader:
        settings = await load_policy_settings(reader)
    assert settings.guests_bring_own is True
    assert {"key": GUESTS_OWN_KEY, "value": True, "source": "owner"} in settings.rows()


async def test_someone_who_lives_here_is_never_taken_off_by_this_rule(session_factory, monkeypatch):
    people = await _people(session_factory, ("Alex", "owner"))
    alex = people["Alex"]
    _judge(monkeypatch)
    await _rule(session_factory, alex, True)
    general = await add_key(session_factory, alex.id, general=True)
    # A record that lives here, invited by the assistant on his own idea.
    await _sam_known(session_factory, role="adult")
    record = await _the_assistant_invites(session_factory, alex, "Sam", "adult")
    sam = await _joins(session_factory, record, alex, "Sam")
    assert await _lines(session_factory, sam) == {}
    assert await _usable(session_factory, sam) == [general.id]
    # A child the owner invites: the house's too.
    async with session_factory() as writer:
        _, token = await create_invitation(
            writer, display_name="Paul", role="child", guest_days=None, created_by=alex.id, now=LIVE
        )
        await writer.commit()
    paul = await _join(session_factory, token, username="paul")
    assert await _lines(session_factory, paul) == {}
    assert await _usable(session_factory, paul) == [general.id]


async def test_the_rule_holds_at_each_turn_whenever_it_was_set(session_factory, monkeypatch):
    people = await _guest_invites_tom(session_factory, monkeypatch)
    alex = people["Alex"]
    general = await add_key(session_factory, alex.id, general=True)
    dana = (await _people(session_factory, ("Dana", "adult"), grants=False))["Dana"]
    tom = await _join(
        session_factory, await _token_for(session_factory, await _invitation(session_factory), alex)
    )
    assert await _usable(session_factory, tom) == [general.id]
    # Set after Tom joined: it holds from his next turn.
    await _rule(session_factory, alex, True)
    assert await _usable(session_factory, tom) == []
    assert await _usable(session_factory, dana) == [general.id]
    # Turned off: the general key is his again.
    await _rule(session_factory, alex, False)
    assert await _usable(session_factory, tom) == [general.id]
    await _rule(session_factory, alex, True)
    await _rule(session_factory, alex, None)
    assert await _usable(session_factory, tom) == [general.id]


async def _line(session_factory: Any, key: str, person: Person, set_by: Person) -> None:
    async with session_factory() as writer:
        writer.add(
            ProviderUser(
                provider_id=key, scope="person", person_id=person.id, allowed=True, set_by=set_by.id
            )
        )
        await writer.commit()


async def test_the_owner_s_own_word_for_a_visitor_wins_over_his_rule(session_factory):
    people = await _people(
        session_factory, ("Alex", "owner"), ("Dana", "adult"), ("Tom", "guest"), ("Sam", "guest")
    )
    alex, tom, sam = people["Alex"], people["Tom"], people["Sam"]
    await _rule(session_factory, alex, True)
    general = await add_key(session_factory, alex.id, general=True)
    assert await _usable(session_factory, tom) == []
    await _line(session_factory, general.id, tom, alex)
    assert await _usable(session_factory, tom) == [general.id]
    # Anyone else's line on the general key is not his word.
    await _line(session_factory, general.id, sam, people["Dana"])
    assert await _usable(session_factory, sam) == []
    assert await _usable(session_factory, people["Dana"]) == [general.id]


async def test_someone_brought_back_as_a_guest_is_under_the_rule(session_factory):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"), ("Sam", "adult"))
    alex, dana = people["Alex"], people["Dana"]
    await _rule(session_factory, alex, True)
    general = await add_key(session_factory, alex.id, general=True)
    assert await _usable(session_factory, dana) == [general.id]
    async with session_factory() as session:
        gone = await session.get(Person, dana.id)
        assert gone is not None
        await disable(session, gone, LIVE - timedelta(days=1), by=alex.id)
        await session.refresh(gone)
        _, token = await create_return_invitation(
            session,
            person=gone,
            role="guest",
            guest_days=2,
            display_name="Dana",
            created_by=alex.id,
            now=LIVE,
        )
        returning = await check_return(session, token=token, password=PASSPHRASE, now=LIVE)
        back = await accept_return(session, returning, "hash", now=LIVE)
        await session.commit()
    assert back.role == "guest"
    assert await _usable(session_factory, back) == []
    assert await _usable(session_factory, people["Sam"]) == [general.id]


async def test_someone_made_a_guest_is_under_the_rule(session_factory, monkeypatch):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"), ("Sam", "adult"))
    alex, dana = people["Alex"], people["Dana"]
    _judge(monkeypatch)
    await _rule(session_factory, alex, True)
    general = await add_key(session_factory, alex.id, general=True)
    assert await _usable(session_factory, dana) == [general.id]
    ctx, reply_id, _ = await _turn(session_factory, alex)
    made = await handle_tool_call(
        _call("household_change_role", person="Dana", role="guest", guest_days=2),
        ctx=ctx,
        reply_id=reply_id,
    )
    assert made.status == "done", made.model_result
    assert await _usable(session_factory, dana) == []
    assert await _usable(session_factory, people["Sam"]) == [general.id]


async def test_a_visiting_record_invited_as_an_adult_is_under_the_rule(
    session_factory, monkeypatch
):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"))
    alex = people["Alex"]
    _judge(monkeypatch)
    await _rule(session_factory, alex, True)
    general = await add_key(session_factory, alex.id, general=True)
    await _sam_known(session_factory, role="adult", standing="visiting")
    record = await _the_assistant_invites(session_factory, alex, "Sam", "adult")
    sam = await _joins(session_factory, record, alex, "Sam")
    assert (sam.role, sam.standing) == ("adult", "visiting")
    assert await _usable(session_factory, sam) == []
    assert await _usable(session_factory, people["Dana"]) == [general.id]
