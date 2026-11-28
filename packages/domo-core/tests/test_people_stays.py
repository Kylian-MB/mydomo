"""Stays (Task 21, audit POL-5): no length of stay is refused by the code, and a stay without an
account ends by the assistant's judgement, its access waking at the next visit.

The catalog's 30-day limit and the 365-day ceiling are gone: any number of days from 1, or no end
at all, is the owner's order or the assistant's own idea with his reason, judged like any act of
his. What still applies before any judgement is the owner's own word: a rule on the type, a limit
he wrote on ``days``, his directives. When nothing is said, an invitation (it makes an account)
gives ``GUEST_DAYS_DEFAULT`` days, a record without an account no end. The assistant ends a stay
with ``record_complete(stay_until="now")``, in a turn or in the quiet review: the access sleeps,
the record stays whole, and a record whose access wakes at its next visit (``access_returns``) is
given a new stay by his gesture, still under the owner's directives."""

import asyncio
import json
import uuid
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from alembic import command
from chat_support import ChatPilot
from domo_core.accounts.models import Person
from domo_core.audit.models import AuditEntry
from domo_core.confirmations.describe import describe_household_change
from domo_core.conversation import execution as execution_module
from domo_core.conversation.execution import handle_tool_call
from domo_core.devices.service import forget_devices_of_those_who_left
from domo_core.household import service as household_service
from domo_core.household.models import Invitation
from domo_core.household.service import (
    GUEST_DAYS_DEFAULT,
    Joining,
    accept_invitation,
    active_persons,
    resolve_person,
)
from domo_core.household.upkeep import pause_rights_of_those_who_left
from domo_core.llm.types import ToolCall
from domo_core.memory import review as review_module
from domo_core.memory.models import Memory
from domo_core.migrate import alembic_config
from domo_core.notes.models import Note
from domo_core.notes.upkeep import erase_notes_of_departed_guests
from domo_core.people import directives, made_roles, records
from domo_core.people.directives import end_expired
from domo_core.people.forget import finish_the_forgotten
from domo_core.people.merge import purge
from domo_core.people.models import PersonAct
from domo_core.policy import catalog
from domo_core.policy.actions import DescribedAction
from domo_core.policy.barrier import Allow, Refuse, decide
from domo_core.policy.catalog import DEFAULT_LIMITS, DEFAULT_POLICY
from domo_core.policy.facts import facts_of
from domo_core.policy.rights import Limit, Right, RightsTable
from domo_core.policy.roles import Actor
from domo_core.texts.view import CatalogTexts
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine
from test_concerned_person import _people, _requests, _turn
from test_memory_review import review_calls
from test_migrations import schema_of
from test_own_idea import LIVE, Judged, mark
from test_review_people import his, reviewed

REASON = "Tom stays with us while his flat is repaired"


@pytest.fixture(autouse=True)
def judged(monkeypatch: pytest.MonkeyPatch) -> Judged:
    judge = Judged()
    monkeypatch.setattr(execution_module, "judge", judge)
    return judge


def _decisions(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """What the barrier decides, from now on."""
    decided: list[Any] = []
    decide = execution_module.decide_now

    async def deciding(*args: Any, **kwargs: Any) -> Any:
        decision = await decide(*args, **kwargs)
        decided.append(decision)
        return decision

    monkeypatch.setattr(execution_module, "decide_now", deciding)
    return decided


def _call(tool: str, **arguments: Any) -> ToolCall:
    return ToolCall(uuid.uuid4().hex[:8], tool, json.dumps(arguments))


async def _run(ctx: Any, reply_id: uuid.UUID, *calls: ToolCall) -> Any:
    record = None
    for call in calls:
        record = await handle_tool_call(call, ctx=ctx, reply_id=reply_id)
    return record


async def _records(session_factory: Any, *names: str, **values: Any) -> dict[str, Person]:
    """Records the assistant knows by name, with no account: ``known`` unless ``values`` say."""
    columns: dict[str, Any] = {
        "role": "guest",
        "state": "known",
        "standing": "visiting",
        "origin": "domo",
        "origin_reason": "they were spoken of",
    } | values
    async with session_factory() as writer:
        made = {name: Person(display_name=name, **columns) for name in names}
        writer.add_all(made.values())
        await writer.commit()
    return made


async def _person(session_factory: Any, person_id: uuid.UUID) -> Person:
    async with session_factory() as reader:
        found = await reader.get(Person, person_id)
    assert found is not None
    return found


async def _invitation(session_factory: Any, name: str) -> Invitation:
    async with session_factory() as reader:
        return (
            await reader.scalars(select(Invitation).where(Invitation.display_name == name))
        ).one()


# --- no length is refused ------------------------------------------------------------------------


@pytest.mark.parametrize("days", [90, 400])
async def test_the_owner_asks_ninety_days_and_it_is_done(session_factory, monkeypatch, days):
    people = await _people(session_factory, ("Alex", "owner"), grants=False)
    tom = (await _records(session_factory, "Tom"))["Tom"]
    paul = (
        await _records(
            session_factory,
            "Paul",
            state="gone",
            first_access_at=LIVE - timedelta(days=9),
            disabled_at=LIVE - timedelta(days=1),
        )
    )["Paul"]
    decided = _decisions(monkeypatch)
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    for call in (
        _call("household_invite", display_name="Sam", role="guest", guest_days=days),
        _call("household_change_role", person="Tom", role="guest", guest_days=days),
        _call("household_bring_back", person="Paul", role="guest", guest_days=days),
    ):
        done = await _run(ctx, reply_id, call)
        assert done.status == "done", done.model_result
    assert decided and all(isinstance(decision, Allow) for decision in decided)
    assert not any(isinstance(decision, Refuse) for decision in decided)
    assert (await _invitation(session_factory, "Sam")).guest_days == days
    for record in (tom, paul):
        now = await _person(session_factory, record.id)
        assert (now.role, now.expires_at) == ("guest", LIVE + timedelta(days=days))


async def test_a_stay_with_no_end_is_accepted(session_factory):
    people = await _people(session_factory, ("Alex", "owner"), grants=False)
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    done = await _run(
        ctx, reply_id, _call("household_invite", display_name="Sam", role="guest", no_end=True)
    )
    assert done.status == "done", done.model_result
    invitation = await _invitation(session_factory, "Sam")
    assert (invitation.role, invitation.guest_days) == ("guest", None)
    async with session_factory() as writer:
        sam = await accept_invitation(
            writer, Joining(invitation.id, "sam", "Sam"), "hash", now=LIVE + timedelta(hours=1)
        )
        await writer.commit()
    assert (sam.role, sam.expires_at) == ("guest", None)


async def test_thirty_days_is_the_default_when_nothing_was_said(session_factory):
    people = await _people(session_factory, ("Alex", "owner"), grants=False)
    tom = (await _records(session_factory, "Tom"))["Tom"]
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    invited = await _run(ctx, reply_id, _call("household_invite", display_name="Sam", role="guest"))
    assert invited.status == "done", invited.model_result
    assert GUEST_DAYS_DEFAULT == 30
    assert (await _invitation(session_factory, "Sam")).guest_days == GUEST_DAYS_DEFAULT
    # A record without an account is given no end: the assistant judges when they left.
    given = await _run(ctx, reply_id, _call("household_change_role", person="Tom", role="guest"))
    assert given.status == "done", given.model_result
    now = await _person(session_factory, tom.id)
    assert (now.state, now.role, now.expires_at) == ("has_access", "guest", None)
    assert given.model_result["no_end"] is True


async def test_days_and_no_end_together_are_invalid_and_zero_days_too(session_factory):
    people = await _people(session_factory, ("Alex", "owner"), grants=False)
    await _records(session_factory, "Tom")
    await _records(
        session_factory,
        "Paul",
        state="gone",
        first_access_at=LIVE - timedelta(days=2),
        disabled_at=LIVE - timedelta(days=1),
    )
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    for tool, named in (
        ("household_invite", {"display_name": "Sam"}),
        ("household_change_role", {"person": "Tom"}),
        ("household_bring_back", {"person": "Paul"}),
    ):
        for stay in ({"guest_days": 5, "no_end": True}, {"guest_days": 0}):
            refused = await _run(ctx, reply_id, _call(tool, **named, role="guest", **stay))
            assert refused.status == "invalid", (tool, stay, refused.model_result)
    async with session_factory() as reader:
        assert list(await reader.scalars(select(Invitation))) == []


def test_no_default_limit_reads_a_length():
    assert not any("days" in limit.expression for limit in DEFAULT_LIMITS.values())
    assert set(DEFAULT_LIMITS) == {"household.invite", "household.invite_again"}
    assert not hasattr(catalog, "GUEST_DAYS_RULE")
    assert not hasattr(household_service, "GUEST_DAYS_CEILING")
    # The fact stays declared: a limit the owner writes himself still reads it.
    for action_type in ("household.invite", "household.change_role", "household.bring_back"):
        assert catalog.ACTION_FACTS[action_type]["days"] == "number"
        assert catalog.ACTION_FACTS[action_type]["no_end"] == "boolean"


# --- the owner's word applies first --------------------------------------------------------------


async def test_an_owner_s_rule_on_the_type_is_applied_first(session_factory, grant, judged):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"), grants=False)
    tom = (await _records(session_factory, "Tom"))["Tom"]
    no = await grant("household.change_role", for_domo=True, access="deny")
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    refused = await _run(
        ctx,
        reply_id,
        mark(REASON),
        _call("household_change_role", person="Tom", role="guest", guest_days=90, reason=REASON),
    )
    assert refused.status == "refused", refused.model_result
    assert judged.cases == []
    assert (await _person(session_factory, tom.id)).state == "known"
    async with session_factory() as writer:
        right = await writer.merge(no)
        await writer.delete(right)
        await writer.commit()
    # His own limit on the length: Dana's request with a right, and the assistant's, wait for him.
    longer = 'action.facts.days > 60 ? "ask_owner" : "allow"'
    await grant("household.change_role", person=people["Dana"], access="allow", limit=longer)
    await grant("household.change_role", for_domo=True, access="allow", limit=longer)
    danas, danas_reply, _ = await _turn(session_factory, people["Dana"])
    asked = _call("household_change_role", person="Tom", role="guest", guest_days=90)
    waiting = await _run(danas, danas_reply, asked)
    assert waiting.status == "waiting", waiting.model_result
    his_own = await _run(
        ctx,
        reply_id,
        mark(REASON),
        _call("household_change_role", person="Tom", role="guest", guest_days=90, reason=REASON),
    )
    assert his_own.status == "waiting", his_own.model_result
    owners = {request.approver_kind for request in await _requests(session_factory)}
    assert owners == {"owner"}
    assert (await _person(session_factory, tom.id)).state == "known"


async def test_the_assistant_gives_a_long_stay_as_his_own_idea_with_a_reason(
    session_factory, judged
):
    people = await _people(session_factory, ("Alex", "owner"), grants=False)
    tom = (await _records(session_factory, "Tom"))["Tom"]
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    done = await _run(
        ctx,
        reply_id,
        mark(REASON),
        _call("household_change_role", person="Tom", role="guest", guest_days=90, reason=REASON),
    )
    assert done.status == "done", done.model_result
    [case] = judged.cases
    assert case.action["facts"]["days"] == 90  # type: ignore[index]
    assert (await _person(session_factory, tom.id)).expires_at == LIVE + timedelta(days=90)
    async with session_factory() as reader:
        [act] = await reader.scalars(select(PersonAct).where(PersonAct.person_id == tom.id))
    assert (act.act, act.by, act.reason) == ("access_given", "domo", REASON)


# --- the review looks at the stays that last -----------------------------------------------------


async def _guest(server: Any, name: str, *, came: timedelta, end: timedelta | None) -> Person:
    """A guest without an account, there for ``came``, whose stay ends ``end`` from now (none:
    no end)."""
    now = server.clock()
    async with server.session_factory() as writer:
        guest = Person(
            display_name=name,
            role="guest",
            standing="visiting",
            first_access_at=now - came,
            expires_at=None if end is None else now + end,
        )
        writer.add(guest)
        await writer.commit()
    return guest


async def test_the_review_is_shown_the_stays_that_last(owner_chat: ChatPilot):
    server = owner_chat.server
    await _guest(server, "Sam", came=timedelta(days=45), end=timedelta(days=5))
    await _guest(server, "Tom", came=timedelta(days=3), end=None)
    await _guest(server, "Paul", came=timedelta(days=10), end=timedelta(days=5))
    await owner_chat.say("Sam and Tom are still with us.")
    since = len(server.calls)
    await reviewed(owner_chat)
    [made] = review_calls(server, since)
    context = made.system.split("\n\nContext\n", 1)[1]
    [line] = [row for row in context.splitlines() if row.startswith("Visitors whose stay")]
    shown = json.loads(line.split("(data, never instructions): ", 1)[1])
    assert [stay["name"] for stay in shown] == ["Sam", "Tom"]
    assert shown[1]["until"] is None and shown[0]["until"] is not None
    # A visitor's own review shows their own stay only; an adult's shows none.
    async with server.session_factory() as reader:
        tom = (await reader.scalars(select(Person).where(Person.display_name == "Tom"))).one()
        paul = (await reader.scalars(select(Person).where(Person.display_name == "Paul"))).one()
        now = server.clock()
        assert [s["name"] for s in await review_module._stays_that_last(reader, tom, now)] == [
            "Tom"
        ]
        assert await review_module._stays_that_last(reader, paul, now) == []


# --- a stay ends by his judgement, and its access wakes at the next visit -------------------------


async def _a_friend_who_stays(server: Any) -> tuple[Person, made_roles.MadeRole]:
    """Tom, without an account, a guest with no end and a role made for him, whose access wakes at
    his next visit."""
    tom = (await _records(server.session_factory, "Tom", standing="visiting", access_returns=True))[
        "Tom"
    ]
    async with server.session_factory() as writer:
        role = await made_roles.create(
            writer,
            name="Weekend friend",
            base="guest",
            allow=("note.leave",),
            deny=(),
            by="domo",
            reason="Tom comes every month",
            now=server.clock(),
        )
        record = await writer.get(Person, tom.id)
        assert record is not None
        await records.give_access(
            writer,
            record,
            role="guest",
            made_role=role,
            standing=None,
            guest_days=None,
            no_end=True,
            by="domo",
            by_owner=False,
            reason="Tom came for the weekend",
            turn_of=None,
            now=server.clock(),
        )
        await writer.commit()
    return tom, role


async def _owners_turn(chat: ChatPilot) -> tuple[Any, uuid.UUID]:
    """A turn of the owner of the pilots, on their clock."""
    ctx, reply_id, _ = await _turn(chat.server.session_factory, chat.person)
    return replace(ctx, clock=chat.clock), reply_id


async def _end_his_stay(chat: ChatPilot) -> Any:
    await chat.say("Tom went home this morning.")
    ctx, reply_id = await _owners_turn(chat)
    return await _run(
        ctx,
        reply_id,
        mark(REASON),
        _call("record_complete", person="Tom", stay_until="now", reason="Tom went home"),
    )


async def test_the_assistant_ends_a_stay_and_the_access_sleeps(owner_chat: ChatPilot):
    server = owner_chat.server
    tom, role = await _a_friend_who_stays(server)
    async with server.session_factory() as reader:
        accesses = dict(await made_roles.accesses_of(reader, role.id))
    ended = await _end_his_stay(owner_chat)
    assert ended.status == "done", ended.model_result
    now = owner_chat.clock()
    async with server.session_factory() as reader:
        assert tom.id not in {p.id for p in await active_persons(reader, now)}
        found = await resolve_person(reader, "Tom", now=now, among="records")
        assert found.id == tom.id
        assert dict(await made_roles.accesses_of(reader, role.id)) == accesses
    record = await _person(server.session_factory, tom.id)
    assert (record.state, record.made_role_id, record.last_review_due) == (
        "has_access",
        role.id,
        True,
    )
    assert record.expires_at is not None and record.expires_at <= now


async def _returns_audited(server: Any, person_id: uuid.UUID) -> list[dict[str, Any]]:
    async with server.session_factory() as reader:
        lines = await reader.scalars(
            select(AuditEntry)
            .where(AuditEntry.action == "people.access_given")
            .order_by(AuditEntry.id)
        )
        return [
            line.details
            for line in lines
            if line.details.get("person_id") == str(person_id)  # type: ignore[union-attr]
        ]


@pytest.mark.parametrize("where", ["turn", "review"])
async def test_access_that_returns_wakes_at_the_next_visit_by_the_assistants_gesture(
    owner_chat: ChatPilot, judged: Judged, where: str
):
    server = owner_chat.server
    tom, role = await _a_friend_who_stays(server)
    assert (await _end_his_stay(owner_chat)).status == "done"
    owner_chat.clock.advance(timedelta(days=20))
    await owner_chat.say("Tom is back for the weekend.")
    back = his("household_bring_back", person="Tom")
    if where == "turn":
        ctx, reply_id = await _owners_turn(owner_chat)
        done = await _run(ctx, reply_id, mark(REASON), back)
        assert done.status == "done", done.model_result
        assert done.model_result["returns"] is True
    else:
        [woke] = await reviewed(owner_chat, back)
        assert woke["status"] == "done", woke
    record = await _person(server.session_factory, tom.id)
    assert (record.state, record.role, record.made_role_id) == ("has_access", "guest", role.id)
    assert record.expires_at is None
    [case] = [c for c in judged.cases if c.action["type"] == "household.bring_back"]  # type: ignore[index]
    assert case.action["facts"]["returns"] is True  # type: ignore[index]
    given = await _returns_audited(server, tom.id)
    assert [line.get("returns") for line in given] == [None, "true"]


async def test_the_assistant_ends_a_stay_from_a_review_too(owner_chat: ChatPilot):
    server = owner_chat.server
    tom, _ = await _a_friend_who_stays(server)
    await owner_chat.say("Tom went home this morning.")
    [ended] = await reviewed(owner_chat, his("record_complete", person="Tom", stay_until="now"))
    assert ended["status"] == "done", ended
    record = await _person(server.session_factory, tom.id)
    assert record.state == "has_access" and record.last_review_due is True
    async with server.session_factory() as reader:
        assert tom.id not in {p.id for p in await active_persons(reader, owner_chat.clock())}


async def test_a_sleeping_access_under_a_directive_does_not_wake(owner_chat: ChatPilot):
    server = owner_chat.server
    tom, _ = await _a_friend_who_stays(server)
    assert (await _end_his_stay(owner_chat)).status == "done"
    async with server.session_factory() as writer:
        owner = await writer.get(Person, owner_chat.person.id)
        record = await writer.get(Person, tom.id)
        assert owner is not None and record is not None
        await directives.set_directive(
            writer,
            kind="withdraw",
            person=record,
            description=None,
            except_words=None,
            until=None,
            owner=owner,
            now=owner_chat.clock(),
        )
        await writer.commit()
    await owner_chat.say("Tom is back.")
    [refused] = await reviewed(owner_chat, his("household_bring_back", person="Tom"))
    assert (refused["status"], refused["reason"]) == ("refused", "owner_directive")
    async with server.session_factory() as reader:
        assert tom.id not in {p.id for p in await active_persons(reader, owner_chat.clock())}


async def test_the_record_of_a_guest_is_kept_without_limit(session_factory):
    people = await _people(session_factory, ("Alex", "owner"), grants=False)
    tom = (await _records(session_factory, "Tom"))["Tom"]
    async with session_factory() as writer:
        record = await writer.get(Person, tom.id)
        assert record is not None
        await records.give_access(
            writer,
            record,
            role="guest",
            made_role=None,
            standing=None,
            guest_days=None,
            by=f"person:{people['Alex'].id}",
            by_owner=True,
            reason=None,
            turn_of=people["Alex"].id,
            now=LIVE,
        )
        await records.complete(
            writer, record, stay_until=LIVE, by="domo", reason="Tom left", turn_of=None, now=LIVE
        )
        writer.add(Note(author_id=people["Alex"].id, recipient_id=tom.id, text="See you soon"))
        writer.add(
            Memory(
                subject="Tom",
                content="Tom loves the lemon pie",
                about_id=tom.id,
                kind="fact",
                origin="deduced",
            )
        )
        await writer.commit()
    later = LIVE + timedelta(days=800)
    async with session_factory() as writer:
        assert await erase_notes_of_departed_guests(writer, later) == 1
        await pause_rights_of_those_who_left(writer, later)
        await forget_devices_of_those_who_left(writer, later)
        await end_expired(writer, later)
        await finish_the_forgotten(writer, later)
        await purge(writer, later)
        await writer.commit()
    kept = await _person(session_factory, tom.id)
    assert (kept.display_name, kept.state, kept.forgotten_at, kept.merged_into_id) == (
        "Tom",
        "has_access",
        None,
        None,
    )
    async with session_factory() as reader:
        assert list(await reader.scalars(select(Note))) == []
        [memory] = await reader.scalars(select(Memory).where(Memory.about_id == tom.id))
    assert memory.content == "Tom loves the lemon pie"


# --- the migration -------------------------------------------------------------------------------


async def test_the_ceiling_goes_and_comes_back_with_the_migration(scratch_database_url):
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0073")
    before = await schema_of(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0074")
    after = await schema_of(scratch_database_url)
    [stay] = [row[2] for row in after["constraints"] if row[1] == "ck_invitations_guest_days_range"]
    assert "365" not in stay and ">= 1" in stay
    engine = create_async_engine(scratch_database_url)
    owner = "00000000-0000-0000-0000-000000000001"
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    f"INSERT INTO persons (id, display_name, role) VALUES ('{owner}', 'O', 'owner')"
                )
            )
            made = (("Long", 400, False), ("Used", 500, True), ("Short", 3, False))
            for name, days, used in made:
                accepted = "now()" if used else "NULL"
                await connection.execute(
                    text(
                        "INSERT INTO invitations (token_hash, display_name, role, guest_days, "
                        f"created_by, expires_at, accepted_at) VALUES ('{name}', '{name}', "
                        f"'guest', {days}, '{owner}', now() + interval '1 day', {accepted})"
                    )
                )
        await asyncio.to_thread(command.downgrade, config, "0073")
        async with engine.connect() as connection:
            rows = await connection.execute(
                text(
                    "SELECT display_name, guest_days, cancelled_at IS NOT NULL FROM invitations "
                    "ORDER BY display_name"
                )
            )
            assert [tuple(row) for row in rows] == [
                ("Long", 365, True),
                ("Short", 3, False),
                ("Used", 365, False),
            ]
    finally:
        await engine.dispose()
    assert await schema_of(scratch_database_url) == before


# --- fix round 1 ---------------------------------------------------------------------------------


async def test_a_record_ends_the_stay_of_a_guest_or_a_visitor_never_the_owner_s_or_a_member_s(
    session_factory,
):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"), grants=False)
    tom = (await _records(session_factory, "Tom", state="has_access"))["Tom"]
    sam = (await _records(session_factory, "Sam", role="adult", state="has_access"))[
        "Sam"
    ]  # visiting, no account
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    for name, until in (("Alex", "now"), ("Alex", "2020-01-01"), ("Dana", "now")):
        kept = await _run(
            ctx,
            reply_id,
            mark(REASON),
            _call("record_complete", person=name, stay_until=until, reason="they left"),
        )
        assert kept.status == "done", kept.model_result
        assert "household_remove" in kept.model_result["stay_not_ended"]
        record = await _person(session_factory, people[name].id)
        assert record.expires_at is None, name
    async with session_factory() as reader:
        here = {p.id for p in await active_persons(reader, LIVE)}
    assert {people["Alex"].id, people["Dana"].id} <= here
    for record in (tom, sam):
        ended = await _run(
            ctx,
            reply_id,
            mark(REASON),
            _call("record_complete", person=record.display_name, stay_until="now", reason="left"),
        )
        assert ended.status == "done", ended.model_result
        assert "stay_not_ended" not in ended.model_result
        assert (await _person(session_factory, record.id)).expires_at is not None


async def test_the_silent_record_only_ends_or_shortens_a_stay_that_runs(session_factory):
    """Lengthening a stay, or waking one that is over, gives an access: never by the silent
    ``record_complete``, whose result names the judged gesture that does it."""
    people = await _people(session_factory, ("Alex", "owner"), grants=False)
    month = LIVE + timedelta(days=30)
    over = LIVE - timedelta(days=7)
    sam = (await _records(session_factory, "Sam", state="has_access", expires_at=month))["Sam"]
    tom = (await _records(session_factory, "Tom", state="has_access", expires_at=over))["Tom"]
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    for record, until, gesture, kept in (
        (sam, "2027-11-27", "household_change_role", month),
        (tom, "2027-11-27", "household_bring_back", over),
    ):
        done = await _run(
            ctx,
            reply_id,
            mark(REASON),
            _call("record_complete", person=record.display_name, stay_until=until, reason="stays"),
        )
        assert done.status == "done", done.model_result
        assert done.model_result["stay_not_changed"] is True
        assert done.model_result["gives_access_by"] == gesture
        assert (await _person(session_factory, record.id)).expires_at == kept
    async with session_factory() as reader:
        assert tom.id not in {p.id for p in await active_persons(reader, LIVE)}
    shorter = await _run(
        ctx,
        reply_id,
        mark(REASON),
        _call("record_complete", person="Sam", stay_until="now", reason="Sam went home"),
    )
    assert "stay_not_changed" not in shorter.model_result
    assert (await _person(session_factory, sam.id)).expires_at == LIVE


async def test_the_owner_ends_an_adult_s_access_on_a_day_without_making_her_a_guest(
    session_factory,
):
    """Dana, an adult with an account, comes for three days. Alex: "her access stops in three
    days": through the judged gesture, her role stays adult, her access ends then."""
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"), grants=False)
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    done = await _run(
        ctx, reply_id, _call("household_change_role", person="Dana", role="adult", guest_days=3)
    )
    assert done.status == "done", done.model_result
    assert done.card["key"] == "action.card.done.household.change_role.access_ends"
    assert "until" in done.card["dates"]
    dana = await _person(session_factory, people["Dana"].id)
    assert (dana.role, dana.expires_at) == ("adult", LIVE + timedelta(days=3))


def test_an_end_of_access_and_living_here_each_have_their_words():
    """What a yes is asked for says what runs: an adult's access that ends, or someone passing
    by who now lives here, never "change the role to adult" (final re-review R3)."""
    adult = {"role": "adult", "current_role": "adult"}
    for language in ("en", "fr"):
        texts = CatalogTexts(language)
        ends = describe_household_change(
            "household.change_role",
            {"person": "Dana", "role": "adult", "guest_days": 3},
            texts,
            {**adult, "days": 3},
        )
        lives = describe_household_change(
            "household.change_role",
            {"person": "Dana", "role": "adult", "lives_here": True},
            texts,
            adult,
        )
        for said, key in ((ends, "access_ends"), (lives, "lives_here")):
            start = texts.template(f"action.describe.household.change_role.{key}").split("{")[0]
            assert said.startswith(start), (language, said)
            assert "Dana" in said


SIXTY_DAYS = 'action.facts.days != null && action.facts.days > 60 ? "refuse" : "allow"'
# Written before a stay could have no end: unguarded, it would fail on a null.
THREE_DAYS_UNGUARDED = 'action.facts.role == "guest" && action.facts.days <= 3 ? "allow" : "refuse"'


def _invite_facts(days: int | None, no_end: bool) -> DescribedAction:
    return DescribedAction(
        "core",
        "household.invite",
        None,
        "permissions",
        "household",
        {"role": "guest", "days": days, "no_end": no_end, "display_name": "Tom",
         "owner_first": False},
    )  # fmt: skip


@pytest.mark.parametrize("expression", [SIXTY_DAYS, THREE_DAYS_UNGUARDED])
def test_a_stay_with_no_end_counts_as_longer_than_any_limit_of_the_owner(expression):
    owner = Actor(uuid.uuid4(), "owner")
    rights = RightsTable(
        (Right(uuid.uuid4(), owner.person_id, None, "household.invite", "allow", None,
               Limit(expression, None, "short stays"), None, None, "short stays"),)
    )  # fmt: skip
    for days, no_end, expected in ((2, False, Allow()), (None, True, None)):
        action = _invite_facts(days, no_end)
        moment = facts_of(owner, action, name="Alex", local_now=None)
        decision = decide(owner, action, DEFAULT_POLICY, rights, moment)
        if expected is not None:
            assert decision == expected
        else:
            assert decision == Refuse("limit", limit="short stays")


async def test_the_owner_s_sixty_days_stop_an_invitation_with_no_end(session_factory, grant):
    people = await _people(session_factory, ("Alex", "owner"), grants=False)
    await grant("household.invite", role="owner", limit=SIXTY_DAYS, summary="60 days at most.")
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    stopped = await _run(
        ctx, reply_id, _call("household_invite", display_name="Sam", role="guest", no_end=True)
    )
    assert (stopped.status, stopped.model_result["reason"]) == ("refused", "limit")
    async with session_factory() as reader:
        assert list(await reader.scalars(select(Invitation))) == []


def test_only_a_stay_that_is_over_returns_never_someone_removed():
    came = LIVE - timedelta(days=9)
    over = Person(display_name="Tom", role="guest", state="has_access", access_returns=True,
                  first_access_at=came, expires_at=LIVE - timedelta(days=1))  # fmt: skip
    removed = Person(display_name="Sam", role="guest", state="gone", access_returns=True,
                     first_access_at=came, disabled_at=LIVE - timedelta(days=1))  # fmt: skip
    assert records.returns_now(over, LIVE) is True
    assert records.returns_now(removed, LIVE) is False


def test_a_stay_in_words_says_no_end_for_a_record_without_an_account():
    from domo_core.confirmations.describe import describe_household_change
    from domo_core.texts.view import CatalogTexts

    texts = CatalogTexts("en")
    # The tool's facts say what the stay really is: no days for a record without an account.
    facts = {"person": "person:x", "current_role": "adult", "role": "guest", "days": None,
             "no_end": True}  # fmt: skip
    sentence = describe_household_change(
        "household.change_role", {"person": "Tom", "role": "guest"}, texts, facts
    )
    assert sentence == texts("action.describe.household.change_role.guest.no_end", name="Tom")
    # Without facts, a change of role says no number it cannot know.
    unknown = describe_household_change(
        "household.change_role", {"person": "Tom", "role": "guest"}, texts
    )
    assert "30" not in unknown
