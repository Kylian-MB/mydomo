"""A device a person entrusts to Domo (spec 6.5, task 7).

Domo shows the person a short code, once; they give it to a program on their own computer, and
the device receives a secret of its own, which is a strong clue of who is speaking. Three tools
let a person do all of it by talking: ``device_entrust``, ``device_list`` and ``device_remove``.

The service is tried directly on the shared rolled-back ``session`` (the program's side,
``finish_pairing`` and ``device_of``, is called by no route in this task); the tools are tried
through the chat pilots on a live server. The code and the device's secret are secrets: they are
in the one answer that shows the code, and in no stored message, no audit entry, no result of the
model and no log line; only their hashes are kept.
"""

import hashlib
import json
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from chat_support import an_actor
from domo_common.i18n import t
from domo_core.accounts.actors import actor_of
from domo_core.accounts.models import Person
from domo_core.confirmations.describe import describe_for_people
from domo_core.devices.models import Device, DevicePairing
from domo_core.devices.service import (
    MAX_DEVICE_NAME,
    PAIRING_FOR,
    begin_pairing,
    device_of,
    devices_of,
    finish_pairing,
    forget_devices_of,
    remove_device,
)
from domo_core.household.service import accept_return, check_return, create_return_invitation
from domo_core.household.service import disable as remove_from_household
from domo_core.household.upkeep import departures_pass
from domo_core.policy.actions import DescribedAction
from domo_core.policy.catalog import (
    ACTION_FACTS,
    CHANGES_NOTHING,
    DEFAULT_POLICY,
    OWNER_ONLY_ACTIONS,
    needs_the_judge,
)
from domo_core.setup_flow.code import SETUP_CODE_ALPHABET
from domo_core.texts.view import CatalogTexts
from domo_core.tools.context import InvalidArguments, ToolContext
from domo_core.tools.registry import TOOLS, tools_for
from sqlalchemy import select, text

NOW = datetime(2026, 11, 14, 10, 0, tzinfo=UTC)
CODE = re.compile(r"\b[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}\b")


def hash_of(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


async def paired(session, person, name="Laptop", *, at=NOW) -> tuple[Device, str]:
    """A device of ``person``, entrusted through the service: the device and its raw secret."""
    code = await begin_pairing(session, person.id, at)
    found = await finish_pairing(session, code, name, at)
    assert found is not None
    return found


# --- the service ---------------------------------------------------------------------------------


async def test_a_right_code_gives_a_device_and_a_secret_that_find_the_person(session, add_person):
    alex = await add_person("Alex", "adult")
    code = await begin_pairing(session, alex.id, NOW)
    # Like the setup code: twelve characters of its alphabet, in three groups.
    assert CODE.fullmatch(code) and set(code.replace("-", "")) <= set(SETUP_CODE_ALPHABET)
    device, secret = await finish_pairing(
        session, code, "  Alex laptop ", NOW + timedelta(minutes=1)
    )
    assert device.person_id == alex.id and device.name == "Alex laptop"
    assert device.removed_at is None and device.last_seen_at is None
    assert len(secret) >= 43  # token_urlsafe(32)
    found = await device_of(session, secret, NOW + timedelta(minutes=2))
    assert found is not None
    known, person = found
    assert known.id == device.id and person.id == alex.id
    assert known.last_seen_at == NOW + timedelta(minutes=2)
    assert await device_of(session, secret + "x", NOW) is None
    assert await device_of(session, "", NOW) is None


async def test_a_device_seen_often_is_written_down_once_a_minute(session, add_person):
    alex = await add_person("Alex", "adult")
    device, secret = await paired(session, alex)
    assert await device_of(session, secret, NOW) is not None
    await session.refresh(device)
    assert device.last_seen_at == NOW
    # Seen again within the minute: nothing is written.
    found = await device_of(session, secret, NOW + timedelta(seconds=59))
    assert found is not None and found[0].last_seen_at == NOW
    await session.refresh(device)
    assert device.last_seen_at == NOW
    # Past the minute, it is.
    later = NOW + timedelta(minutes=1, seconds=1)
    found = await device_of(session, secret, later)
    assert found is not None and found[0].last_seen_at == later
    await session.refresh(device)
    assert device.last_seen_at == later


async def test_only_the_hashes_are_kept(session, add_person):
    alex = await add_person("Alex", "adult")
    code = await begin_pairing(session, alex.id, NOW)
    pairing = (await session.execute(select(DevicePairing))).scalar_one()
    assert pairing.code_hash == hash_of(code) and pairing.person_id == alex.id
    assert pairing.expires_at == NOW + PAIRING_FOR and pairing.used_at is None
    device, secret = await finish_pairing(session, code, "Laptop", NOW)
    assert device.secret_hash == hash_of(secret)
    await session.refresh(pairing)
    assert pairing.used_at == NOW
    dump = await session.execute(
        text(
            "SELECT row_to_json(d)::text FROM devices d UNION ALL "
            "SELECT row_to_json(p)::text FROM device_pairings p"
        )
    )
    stored = "\n".join(row[0] for row in dump.all())
    assert secret not in stored and code not in stored and code.replace("-", "") not in stored


async def test_the_code_is_read_as_typed(session, add_person):
    alex = await add_person("Alex", "adult")
    code = await begin_pairing(session, alex.id, NOW)
    typed = f"  {code.lower().replace('-', ' ')}\n"
    assert await finish_pairing(session, typed, "Laptop", NOW) is not None


async def test_a_wrong_code_is_refused(session, add_person):
    alex = await add_person("Alex", "adult")
    code = await begin_pairing(session, alex.id, NOW)
    other = "-".join(["AAAA"] * 3) if code != "AAAA-AAAA-AAAA" else "BBBB-BBBB-BBBB"
    for wrong in (other, "", "x", code[:-1], code + "A", "\ud800"):
        assert await finish_pairing(session, wrong, "Laptop", NOW) is None
    assert (await session.execute(select(Device))).first() is None
    # The right one still works: a wrong guess spends nothing.
    assert await finish_pairing(session, code, "Laptop", NOW) is not None


async def test_a_code_lasts_ten_minutes(session, add_person):
    alex = await add_person("Alex", "adult")
    assert timedelta(minutes=10) == PAIRING_FOR
    late = await begin_pairing(session, alex.id, NOW)
    after = NOW + PAIRING_FOR + timedelta(seconds=1)
    assert await finish_pairing(session, late, "Laptop", after) is None
    just = await begin_pairing(session, alex.id, NOW)
    assert await finish_pairing(session, just, "Laptop", NOW + PAIRING_FOR) is not None


async def test_a_code_serves_once(session, add_person):
    alex = await add_person("Alex", "adult")
    code = await begin_pairing(session, alex.id, NOW)
    assert await finish_pairing(session, code, "Laptop", NOW) is not None
    assert await finish_pairing(session, code, "Other", NOW) is None
    assert len(await devices_of(session, alex.id)) == 1


async def test_a_new_code_replaces_the_one_that_waited(session, add_person):
    alex = await add_person("Alex", "adult")
    first = await begin_pairing(session, alex.id, NOW)
    second = await begin_pairing(session, alex.id, NOW + timedelta(minutes=1))
    assert await finish_pairing(session, first, "Laptop", NOW + timedelta(minutes=1)) is None
    assert await finish_pairing(session, second, "Laptop", NOW + timedelta(minutes=1)) is not None


async def test_a_code_of_someone_who_left_is_refused(session, add_person):
    alex = await add_person("Alex", "adult")
    code = await begin_pairing(session, alex.id, NOW)
    await remove_from_household(session, alex, NOW, by=alex.id)
    assert await finish_pairing(session, code, "Laptop", NOW) is None


@pytest.mark.parametrize("name", ["", "   ", "x" * (MAX_DEVICE_NAME + 1), "two\nlines"])
async def test_a_name_that_is_not_a_plain_line_of_sixty_characters_is_refused(
    session, add_person, name
):
    alex = await add_person("Alex", "adult")
    code = await begin_pairing(session, alex.id, NOW)
    with pytest.raises(ValueError):
        await finish_pairing(session, code, name, NOW)
    # The code was not spent by a name that cannot be kept.
    assert await finish_pairing(session, code, "x" * MAX_DEVICE_NAME, NOW) is not None


async def test_a_removed_device_is_no_longer_known(session, add_person):
    alex = await add_person("Alex", "adult")
    device, secret = await paired(session, alex)
    assert await remove_device(session, device.id, NOW) is True
    assert await device_of(session, secret, NOW) is None
    assert await remove_device(session, device.id, NOW) is False
    assert await remove_device(session, uuid.uuid4(), NOW) is False
    assert await devices_of(session, alex.id) == []
    await session.refresh(device)
    assert device.removed_at == NOW


async def test_devices_are_listed_by_person_oldest_first_without_the_removed(session, add_person):
    alex = await add_person("Alex", "adult")
    sam = await add_person("Sam", "child")
    first, _ = await paired(session, alex, "Desk", at=NOW)
    second, _ = await paired(session, alex, "Laptop", at=NOW + timedelta(minutes=1))
    gone, _ = await paired(session, alex, "Old", at=NOW + timedelta(minutes=2))
    await paired(session, sam, "Tablet")
    await remove_device(session, gone.id, NOW)
    assert [d.id for d in await devices_of(session, alex.id)] == [first.id, second.id]


async def test_forgetting_the_devices_of_a_person_forgets_them_all(session, add_person):
    alex = await add_person("Alex", "adult")
    sam = await add_person("Sam", "child")
    _, one = await paired(session, alex, "Desk")
    _, two = await paired(session, alex, "Laptop")
    _, kept = await paired(session, sam, "Tablet")
    assert await forget_devices_of(session, alex.id, NOW) == 2
    assert await forget_devices_of(session, alex.id, NOW) == 0
    for secret in (one, two):
        assert await device_of(session, secret, NOW) is None
    assert await device_of(session, kept, NOW) is not None


async def test_a_device_of_someone_who_left_is_not_known_even_before_it_is_forgotten(
    session, add_person
):
    guest = await add_person("Robin", "guest")
    guest.expires_at = NOW + timedelta(days=2)
    await session.flush()
    _, secret = await paired(session, guest)
    assert await device_of(session, secret, NOW + timedelta(days=1)) is not None
    assert await device_of(session, secret, NOW + timedelta(days=2, seconds=1)) is None
    removed = await add_person("Dana", "adult")
    _, other = await paired(session, removed)
    removed.disabled_at = NOW
    removed.state = "gone"
    await session.flush()
    assert await device_of(session, other, NOW) is None


async def test_the_devices_are_forgotten_when_a_person_leaves_or_is_removed(session, add_person):
    alex = await add_person("Alex", "adult")
    _, secret = await paired(session, alex)
    await remove_from_household(session, alex, NOW, by=alex.id)
    assert await devices_of(session, alex.id) == []
    assert await device_of(session, secret, NOW) is None
    stored = (await session.execute(select(Device))).scalar_one()
    assert stored.removed_at == NOW


async def test_the_devices_of_a_guest_whose_stay_ended_are_forgotten_by_the_upkeep(
    session_factory, add_member, live_clock
):
    # Real commits, as the upkeep reads them in a session of its own.
    guest = await add_member("Robin", "guest")
    here = await add_member("Alex", "adult")
    now = live_clock()
    async with session_factory() as writer:
        await paired(writer, guest, at=now)
        _, kept = await paired(writer, here, at=now)
        await writer.commit()
    after = guest.expires_at + timedelta(minutes=1)
    await departures_pass(session_factory)(after)
    async with session_factory() as reader:
        assert await devices_of(reader, guest.id) == []
        assert len(await devices_of(reader, here.id)) == 1
        assert await device_of(reader, kept, after) is not None
        [forgotten] = (
            await reader.execute(select(Device).where(Device.person_id == guest.id))
        ).scalars()
        assert forgotten.removed_at == after
    # A second pass finds nothing to do.
    await departures_pass(session_factory)(after + timedelta(minutes=1))
    async with session_factory() as reader:
        [again] = (
            await reader.execute(select(Device).where(Device.person_id == guest.id))
        ).scalars()
        assert again.removed_at == after


async def test_coming_back_does_not_bring_the_old_devices_back(
    session, owner_account, add_person, add_account, clock
):
    gone = await add_person("Gone", "guest")
    await add_account(gone, "gone", "hash")
    gone.expires_at = clock() + timedelta(hours=1)
    await session.flush()
    _, secret = await paired(session, gone, at=clock())
    # The stay ended and the upkeep has not looked yet: the return forgets them too.
    clock.advance(timedelta(hours=2))
    _, token = await create_return_invitation(
        session,
        person=gone,
        role="adult",
        guest_days=None,
        display_name="Gone",
        created_by=owner_account.person_id,
        now=clock(),
    )
    returning = await check_return(
        session, token=token, password="a-brand-new-passphrase", now=clock()
    )
    await accept_return(session, returning, "hash", now=clock())
    assert await device_of(session, secret, clock()) is None
    assert await devices_of(session, gone.id) == []


async def test_changing_a_password_does_not_remove_a_device(session, add_person):
    # Nothing in ``devices`` knows passwords: a device stays known until it is removed (spec 6.5).
    alex = await add_person("Alex", "adult")
    _, secret = await paired(session, alex)
    assert await device_of(session, secret, NOW) is not None


async def test_the_devices_of_a_person_go_with_them(session, add_person):
    alex = await add_person("Alex", "adult")
    await paired(session, alex)
    await begin_pairing(session, alex.id, NOW)
    await session.execute(text("DELETE FROM persons WHERE id = :id"), {"id": alex.id})
    assert (await session.execute(select(Device))).first() is None
    assert (await session.execute(select(DevicePairing))).first() is None


# --- the tools: entrusting a device ---------------------------------------------------------------


async def stored_everywhere(session_factory) -> str:
    """Everything the database keeps of the conversations and the audit log, as one text."""
    async with session_factory() as reader:
        rows = await reader.execute(
            text(
                "SELECT row_to_json(a)::text FROM audit_log a UNION ALL "
                "SELECT row_to_json(m)::text FROM messages m UNION ALL "
                "SELECT row_to_json(x)::text FROM message_actions x UNION ALL "
                "SELECT row_to_json(c)::text FROM confirmations c"
            )
        )
        return "\n".join(row[0] for row in rows.all())


async def test_an_adult_is_shown_a_code_once_and_gives_it_to_their_device(
    adult_chat, session_factory, last_audit_entry, live_clock, caplog
):
    caplog.set_level(logging.DEBUG)
    reply = await adult_chat.say_with_tool("device_entrust", {}, judge="do")
    assert reply.actions == [{"tool": "device_entrust", "status": "done"}]
    assert reply.pending_confirmation_id is None
    found = CODE.findall(reply.cards_html)
    assert len(found) == 1
    code = found[0]
    # The model reads that a code was shown, never the code.
    assert reply.model_results[-1] == {"status": "done", "code": "shown_once"}
    assert (
        code not in reply.model_result_text and code.replace("-", "") not in reply.model_result_text
    )
    # The card is in the person's language, with the code in it, once.
    assert t("action.card.done.device.entrust", "en") in reply.cards_html
    # The code is nowhere else: not stored, not audited, not logged.
    assert code not in await stored_everywhere(session_factory)
    assert code not in caplog.text and code.replace("-", "") not in caplog.text
    entry = await last_audit_entry("device.entrusted")
    assert entry.actor == f"person:{adult_chat.person.id}"
    assert entry.details == {"person_id": str(adult_chat.person.id)}
    # The code works, once, for the program.
    async with session_factory() as writer:
        made = await finish_pairing(writer, code, "Alex laptop", live_clock())
        assert made is not None
        device, secret = made
        await writer.commit()
    assert device.person_id == adult_chat.person.id
    assert secret not in caplog.text
    # The reload of the page does not show it again.
    history = await adult_chat.get("/")
    assert code not in history.text


async def test_the_owner_may_entrust_a_device_too(owner_chat):
    reply = await owner_chat.say_with_tool("device_entrust", {}, judge="do")
    assert reply.actions == [{"tool": "device_entrust", "status": "done"}]
    assert len(CODE.findall(reply.cards_html)) == 1


async def test_a_child_or_a_guest_may_not_unless_the_owner_gives_the_right(
    child_chat, guest_chat, grant, revoke_right, session_factory
):
    for chat in (child_chat, guest_chat):
        assert "device_entrust" not in [
            d.name for d in tools_for(an_actor(chat.person.role), TOOLS, DEFAULT_POLICY)
        ]
        reply = await chat.say_with_tool("device_entrust", {}, judge="do")
        assert reply.actions == [
            {"tool": "device_entrust", "status": "refused", "reason": "not_allowed"}
        ]
        assert not CODE.findall(reply.cards_html)
    async with session_factory() as reader:
        assert (await reader.execute(select(DevicePairing))).first() is None
    # Reserved to adults (ruling 16): the owner's right for the child opens it, an adult agreeing
    # first unless that right says the child agrees for themselves.
    opened = await grant("device.entrust", person=child_chat.person, access="allow")
    waiting = await child_chat.say_with_tool("device_entrust", {}, judge="do")
    assert waiting.actions == [{"tool": "device_entrust", "status": "waiting"}]
    assert waiting.model_results[-1]["by"] == "an adult"
    await revoke_right(opened)
    await grant("device.entrust", person=child_chat.person, access="allow", approver="self")
    reply = await child_chat.say_with_tool("device_entrust", {}, judge="do")
    assert reply.actions == [{"tool": "device_entrust", "status": "done"}]
    assert len(CODE.findall(reply.cards_html)) == 1
    # The right is the child's own: the guest is still refused.
    again = await guest_chat.say_with_tool("device_entrust", {}, judge="do")
    assert again.actions == [
        {"tool": "device_entrust", "status": "refused", "reason": "not_allowed"}
    ]


def context_of(person: Person, session_factory, clock, **more) -> ToolContext:
    return ToolContext(
        session_factory=session_factory,
        actor=actor_of(person),  # type: ignore[arg-type]
        person=person,
        conversation_id=uuid.uuid4(),
        origin="https://domo.local/",
        gatekeeper=None,  # type: ignore[arg-type]
        clock=clock,
        registry=TOOLS,
        policy=DEFAULT_POLICY,
        reply_message_id=None,
        **more,
    )


async def test_a_code_nobody_would_see_is_not_made(
    add_member, session_factory, live_clock, audit_actions
):
    owner = await add_member("Owner", "owner")
    alex = await add_member("Alex", "adult")
    left = (
        context_of(alex, session_factory, live_clock, approved_by=owner.id),  # someone else's yes
        context_of(alex, session_factory, live_clock, approved_by=alex.id),  # nobody reads
        context_of(alex, session_factory, live_clock, approved_by=alex.id),  # a chain's step
    )
    left[1].no_reader = True
    left[2].in_a_chain = True
    for ctx in left:
        outcome = await TOOLS["device_entrust"].execute({}, ctx)
        assert outcome.status == "done" and outcome.card_once is None
        assert outcome.model_result == {"code": "ask_again_when_they_are_there"}
        assert outcome.card["key"] == "action.card.done.device.entrust.later"
    async with session_factory() as reader:
        assert (await reader.execute(select(DevicePairing))).first() is None
    assert "device.entrusted" not in await audit_actions()
    # Said yes to by oneself, with a page that reads: the code is made.
    shown = await TOOLS["device_entrust"].execute(
        {}, context_of(alex, session_factory, live_clock, approved_by=alex.id)
    )
    assert shown.card_once is not None and shown.model_result == {"code": "shown_once"}
    assert CODE.fullmatch(shown.card_once["params"]["code"])
    assert "code" not in json.dumps(shown.card)


async def test_entrusting_takes_no_argument(add_member, session_factory, live_clock):
    alex = await add_member("Alex", "adult")
    with pytest.raises(InvalidArguments):
        await TOOLS["device_entrust"].execute(
            {"name": "x"}, context_of(alex, session_factory, live_clock)
        )


# --- the tools: seeing the devices ----------------------------------------------------------------


async def test_a_person_sees_their_own_devices_without_any_secret(
    adult_chat, owner_chat, session_factory, live_clock
):
    async with session_factory() as writer:
        device, secret = await paired(writer, adult_chat.person, "Alex laptop", at=live_clock())
        await paired(writer, owner_chat.person, "Owner desk", at=live_clock())
        await writer.commit()
    await device_of_and_commit(session_factory, secret, live_clock())
    reply = await adult_chat.say_with_tool("device_list", {})
    assert reply.actions == [{"tool": "device_list", "status": "done"}]
    [listed] = reply.model_results[-1]["devices"]
    assert listed["name"] == "Alex laptop"
    assert listed["added"].startswith(live_clock().astimezone(UTC).strftime("%Y-%m-%d"))
    assert listed["last_seen"] is not None
    written = json.dumps(reply.model_results[-1]) + reply.cards_html
    assert secret not in written and device.secret_hash not in written
    assert "Owner desk" not in written
    assert t("action.card.done.device.list", "en") in reply.cards_html
    none = await owner_chat.say_with_tool("device_list", {})
    assert [d["name"] for d in none.model_results[-1]["devices"]] == ["Owner desk"]


async def device_of_and_commit(session_factory, secret, now):
    async with session_factory() as writer:
        assert await device_of(writer, secret, now) is not None
        await writer.commit()


async def test_a_guest_and_a_child_see_theirs_too(guest_chat, child_chat):
    for chat in (guest_chat, child_chat):
        reply = await chat.say_with_tool("device_list", {})
        assert reply.actions == [{"tool": "device_list", "status": "done"}]
        assert reply.model_results[-1]["devices"] == []


# --- the tools: taking a device back --------------------------------------------------------------


async def test_a_person_removes_their_own_device_by_its_name(
    adult_chat, session_factory, live_clock, last_audit_entry, audit_dump
):
    async with session_factory() as writer:
        device, secret = await paired(writer, adult_chat.person, "Alex laptop", at=live_clock())
        await writer.commit()
    reply = await adult_chat.say_with_tool("device_remove", {"device": " ALEX laptop "}, judge="do")
    assert reply.actions == [{"tool": "device_remove", "status": "done"}]
    assert reply.model_results[-1] == {"status": "done", "removed": "Alex laptop"}
    assert t("action.card.done.device.remove", "en", name="Alex laptop") in reply.cards_html
    async with session_factory() as reader:
        assert await device_of(reader, secret, live_clock()) is None
    entry = await last_audit_entry("device.removed")
    assert entry.actor == f"person:{adult_chat.person.id}"
    assert entry.details == {"device_id": str(device.id), "person_id": str(adult_chat.person.id)}
    dump = await audit_dump()
    assert "laptop" not in dump.lower() and secret not in dump


async def test_a_device_nobody_has_is_not_found(
    adult_chat, owner_chat, session_factory, live_clock
):
    async with session_factory() as writer:
        await paired(writer, owner_chat.person, "Owner desk", at=live_clock())
        await writer.commit()
    for name in ("Nothing", "Owner desk"):  # the second one is someone else's
        reply = await adult_chat.say_with_tool("device_remove", {"device": name}, judge="do")
        assert reply.actions == [
            {"tool": "device_remove", "status": "invalid", "reason": "not_found"}
        ]
    async with session_factory() as reader:
        assert len(await devices_of(reader, owner_chat.person.id)) == 1


async def test_two_devices_with_one_name_are_ambiguous(adult_chat, session_factory, live_clock):
    async with session_factory() as writer:
        await paired(writer, adult_chat.person, "Laptop", at=live_clock())
        await paired(writer, adult_chat.person, "laptop", at=live_clock())
        await writer.commit()
    reply = await adult_chat.say_with_tool("device_remove", {"device": "Laptop"}, judge="do")
    assert reply.actions == [{"tool": "device_remove", "status": "invalid", "reason": "ambiguous"}]
    async with session_factory() as reader:
        assert len(await devices_of(reader, adult_chat.person.id)) == 2


async def test_an_adult_does_not_remove_the_device_of_another_but_the_owner_does(
    adult_chat, owner_chat, session_factory, live_clock, last_audit_entry
):
    async with session_factory() as writer:
        device, secret = await paired(writer, adult_chat.person, "Alex laptop", at=live_clock())
        await writer.commit()
    alex = adult_chat.person
    refused = await owner_chat.say_with_tool(
        "device_remove", {"device": "Alex laptop", "person": "Nobody"}, judge="do"
    )
    assert refused.actions == [
        {"tool": "device_remove", "status": "invalid", "reason": "not_found"}
    ]
    # Alex names the owner's person: not theirs to touch, whatever the rights say.
    async with session_factory() as writer:
        await paired(writer, owner_chat.person, "Desk", at=live_clock())
        await writer.commit()
    other = await adult_chat.say_with_tool(
        "device_remove", {"device": "Desk", "person": owner_chat.person.display_name}, judge="do"
    )
    assert other.actions == [{"tool": "device_remove", "status": "invalid", "reason": "invalid"}]
    async with session_factory() as reader:
        assert len(await devices_of(reader, owner_chat.person.id)) == 1
    done = await owner_chat.say_with_tool(
        "device_remove", {"device": "Alex laptop", "person": alex.display_name}, judge="do"
    )
    assert done.actions == [{"tool": "device_remove", "status": "done"}]
    async with session_factory() as reader:
        assert await device_of(reader, secret, live_clock()) is None
    entry = await last_audit_entry("device.removed")
    assert entry.actor == f"person:{owner_chat.person.id}"
    assert entry.details == {"device_id": str(device.id), "person_id": str(alex.id)}


async def test_removing_is_described_by_whose_device_and_never_its_name(
    adult_chat, session_factory, live_clock, last_audit_entry
):
    async with session_factory() as writer:
        device, _ = await paired(writer, adult_chat.person, "Alex laptop", at=live_clock())
        await writer.commit()
    await adult_chat.say_with_tool("device_remove", {"device": "Alex laptop"})
    decision = await last_audit_entry("policy.decision")
    assert decision.details["action_type"] == "device.remove"
    assert decision.details["facts"] == {
        "person": f"person:{adult_chat.person.id}",
        "is_self": True,
    }
    assert "laptop" not in json.dumps(decision.details).lower()


async def test_a_request_that_waits_is_still_about_the_same_device(
    owner_chat, session_factory, live_clock
):
    async with session_factory() as writer:
        device, _ = await paired(writer, owner_chat.person, "Desk", at=live_clock())
        await writer.commit()
    ctx = context_of(owner_chat.person, session_factory, live_clock)
    described = await TOOLS["device_remove"].describe({"device": "Desk"}, ctx.actor, ctx)
    assert described.target == f"device:{device.id}"
    async with session_factory() as writer:
        await remove_device(writer, device.id, live_clock())
        await writer.commit()
        await paired(writer, owner_chat.person, "Desk", at=live_clock())
        await writer.commit()
    ctx.described_action = described
    with pytest.raises(InvalidArguments) as refused:
        await TOOLS["device_remove"].execute({"device": "Desk"}, ctx)
    assert refused.value.code == "not_found"


@pytest.mark.parametrize(
    "arguments", [{}, {"device": ""}, {"device": 3}, {"device": "x" * 61}, {"device": "a", "x": 1}]
)
async def test_removing_checks_its_arguments(adult_chat, arguments):
    reply = await adult_chat.say_with_tool("device_remove", arguments, judge="do")
    assert reply.actions[0]["status"] == "invalid"


# --- the catalog, the registry and the texts ------------------------------------------------------


def test_the_catalog_knows_the_three_actions_and_who_may_do_them():
    for tool, action in (
        ("device_entrust", "device.entrust"),
        ("device_list", "device.list"),
        ("device_remove", "device.remove"),
    ):
        assert TOOLS[tool].action_type == action
        assert tool in [d.name for d in tools_for(an_actor("owner"), TOOLS, DEFAULT_POLICY)]
    assert "device.entrust" in DEFAULT_POLICY.allowed["adult"]
    for role in ("child", "guest"):
        assert "device.entrust" not in DEFAULT_POLICY.allowed[role]
    for action in ("device.list", "device.remove"):
        for role in ("owner", "adult", "child", "guest"):
            assert action in DEFAULT_POLICY.allowed[role]
        assert action not in OWNER_ONLY_ACTIONS
    assert "device.list" in CHANGES_NOTHING
    assert needs_the_judge("device.entrust") and needs_the_judge("device.remove")
    assert dict(ACTION_FACTS["device.entrust"]) == {}
    assert dict(ACTION_FACTS["device.list"]) == {}
    assert dict(ACTION_FACTS["device.remove"]) == {"person": "person", "is_self": "boolean"}


def test_a_request_is_told_in_both_languages():
    entrust = DescribedAction("core", "device.entrust", None, "permissions", "household")
    remove = DescribedAction(
        "core",
        "device.remove",
        "device:x",
        "permissions",
        "household",
        {"person": "person:x", "is_self": True},
    )
    for lang in ("en", "fr"):
        shown = CatalogTexts(lang)
        assert describe_for_people(entrust, {}, shown) != t("action.describe", lang)
        sentence = describe_for_people(remove, {"device": "Desk"}, shown)
        assert "Desk" in sentence and sentence != t("action.describe", lang)


def test_the_tools_tell_domo_what_he_sees_and_never_the_code():
    description = TOOLS["device_entrust"].definition.description
    assert "once" in description and "never see" in description
    assert "10 minutes" in description
    assert TOOLS["device_entrust"].definition.parameters["properties"] == {}
    assert "name" in TOOLS["device_remove"].definition.description
