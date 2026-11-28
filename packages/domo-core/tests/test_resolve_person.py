"""A first name alone finds the right person (spec 6.7, task 10, Review Focus 6).

``household.service.resolve_person`` tries the whole name first, as before; then, when nobody has
it, the people whose first word of the name it is; then, when nobody has that either, the people
whose name begins with it. One person is them; several are ``ambiguous``, and the refusal names
them all, so that Domo chooses with what he knows or asks. Never someone who left, and never a
member for the whole name of an invitation that waits.

What a call that names someone is about is the person found, everywhere: the call is given their
whole name before it is described (``household.service.whole_names``), so that its sentence, its
card, the judge and the stored request read "Samuel", not "Sam"; and a share or a limit of a key
that waits for a yes runs for the very person the yes was given for.
"""

import html
import json
import re
import uuid
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from chat_support import ChatPilot, dump
from domo_core.accounts.models import Person
from domo_core.confirmations.models import Confirmation
from domo_core.household.service import create_invitation, resolve_person, whole_names
from domo_core.household.succession import entries_from
from domo_core.llm.models import ProviderUser
from domo_core.llm.types import ToolCall
from domo_core.policy.catalog import DEFAULT_POLICY
from domo_core.policy.roles import Actor
from domo_core.tools.context import InvalidArguments, ToolContext
from domo_core.tools.registry import TOOLS
from fakes import FakeChatModel
from sqlalchemy import select, update
from test_spending import add_key


async def refused(session, name: str, clock) -> InvalidArguments:
    with pytest.raises(InvalidArguments) as error:
        await resolve_person(session, name, now=clock())
    return error.value


async def test_a_first_name_alone_finds_the_person(session, add_person, clock):
    await add_person("Owner", "owner")
    alex = await add_person("Alex Martin", "adult")
    for asked in ("Alex", "alex", " ALEX ", "Alex Martin", "Alex M"):
        assert (await resolve_person(session, asked, now=clock())).id == alex.id, asked
    # A piece from inside a name, or a later word of it, means nobody.
    for asked in ("lex", "Martin", "Alex Martinez", "Alexa"):
        assert (await refused(session, asked, clock)).code == "not_found", asked


async def test_two_alexes_are_ambiguous_and_named(session, add_person, clock):
    await add_person("Owner", "owner")
    await add_person("Alex Martin", "adult")
    await add_person("Alex Dupont", "child")
    error = await refused(session, "Alex", clock)
    assert error.code == "ambiguous"
    assert error.detail == "several people match: Alex Martin, Alex Dupont"
    # The whole name tells them apart.
    dupont = await resolve_person(session, "alex dupont", now=clock())
    assert dupont.display_name == "Alex Dupont"


async def test_sam_does_not_find_samuel_when_sam_exists(session, add_person, clock):
    await add_person("Owner", "owner")
    sam = await add_person("Sam", "child")
    await add_person("Samuel", "adult")
    assert (await resolve_person(session, "Sam", now=clock())).id == sam.id
    # Nor Samuel's first word when Sam has another name and "Sam" is a first word of his.
    await add_person("Sam Bee", "guest")
    assert (await resolve_person(session, "sam", now=clock())).id == sam.id


async def test_sam_finds_samuel_when_no_sam_exists(session, add_person, clock):
    await add_person("Owner", "owner")
    samuel = await add_person("Samuel", "adult")
    assert (await resolve_person(session, "Sam", now=clock())).id == samuel.id
    # A first word comes before a beginning: "Sam Bee" is Sam, not Samuel.
    sam_bee = await add_person("Sam Bee", "child")
    assert (await resolve_person(session, "Sam", now=clock())).id == sam_bee.id
    # Two beginnings are ambiguous, named.
    await add_person("Samira", "adult")
    error = await refused(session, "Sa", clock)
    assert (error.code, error.detail) == (
        "ambiguous",
        "several people match: Samuel, Sam Bee, Samira",
    )


async def test_someone_whose_stay_ended_is_not_found(session, add_person, clock):
    await add_person("Owner", "owner")
    gone = await add_person("Dana Gone", "adult")
    gone.disabled_at = clock()
    gone.state = "gone"
    over = await add_person("Paul Over", "guest")
    over.expires_at = clock() - timedelta(seconds=1)
    await session.flush()
    for asked in ("Dana", "Dan", "Paul", "Pa", "Dana Gone", "Paul Over"):
        assert (await refused(session, asked, clock)).code == "not_found", asked
    # Nor do they make someone else ambiguous.
    paul = await add_person("Paul Here", "adult")
    assert (await resolve_person(session, "Paul", now=clock())).id == paul.id


async def test_an_empty_name_means_nobody(session, add_person, clock):
    await add_person("Owner", "owner")
    for asked in ("", "   ", "\N{ZERO WIDTH SPACE}"):
        assert (await refused(session, asked, clock)).code == "not_found", ascii(asked)


async def test_a_successor_named_in_full_is_never_taken_for_a_longer_name(
    session, add_person, add_account, clock
):
    """The list of successors finds someone who left by their whole name before a member whose
    name only begins the same way (``succession._person_named``)."""
    await add_person("Owner", "owner")
    sam = await add_person("Sam", "adult")
    samuel = await add_person("Samuel", "adult")
    # A successor by person has an account.
    await add_account(sam, "sam")
    await add_account(samuel, "samuel")
    [entry] = await entries_from(session, [{"person": "Sam"}], clock())
    assert entry.person_id == sam.id
    sam.disabled_at = clock()
    sam.state = "gone"
    await session.flush()
    # Sam left and is not on the list: then "Sam" is Samuel, the one person it begins.
    [entry] = await entries_from(session, [{"person": "Sam"}], clock())
    assert entry.person_id == samuel.id


# --- an invitation that waits ---------------------------------------------------------------------


async def test_the_name_of_an_invitation_that_waits_never_means_a_longer_name(
    session, add_person, clock
):
    owner = await add_person("Owner", "owner")
    samuel = await add_person("Samuel", "adult")
    await create_invitation(
        session, display_name="Sam", role="child", guest_days=None, created_by=owner.id, now=clock()
    )
    error = await refused(session, "sam", clock)
    assert error.code == "not_found"
    assert error.detail == (
        "no member has this name yet: an invitation that waits has it; did you mean: Samuel?"
    )
    # Nobody it could mean: the refusal says only that the invitation has it.
    await create_invitation(
        session, display_name="Zed", role="guest", guest_days=2, created_by=owner.id, now=clock()
    )
    zed = await refused(session, "zed", clock)
    assert (zed.code, zed.detail) == (
        "not_found",
        "no member has this name yet: an invitation that waits has it",
    )
    # The whole name of a member still means them, and a name of no invitation still begins one.
    assert (await resolve_person(session, "Samuel", now=clock())).id == samuel.id
    assert (await resolve_person(session, "Samu", now=clock())).id == samuel.id


async def test_whole_names_are_given_only_for_a_name_that_means_one_person(
    session, add_person, clock
):
    await add_person("Owner", "owner")
    await add_person("Samuel", "adult")
    await add_person("Alex Martin", "adult")
    await add_person("Alex Dupont", "child")
    given = {"person": "sam", "recipient": "alex", "other": "sam", "role": "adult"}
    named = await whole_names(
        session, given, ("person", "recipient", "missing"), now=clock(), speaker=None
    )
    # One person: their whole name; several, or nobody: as it was given; other arguments: kept.
    assert named == {"person": "Samuel", "recipient": "alex", "other": "sam", "role": "adult"}
    assert given["person"] == "sam"  # the arguments given are not changed
    left = await whole_names(
        session, given, ("person",), now=clock(), speaker=None, leave=frozenset({"sam"})
    )
    assert left["person"] == "sam"


# --- what a request made with a short name says and does ------------------------------------------


def call(name: str, arguments: dict[str, Any], call_id: str) -> ToolCall:
    return ToolCall(call_id, name, json.dumps(arguments))


async def stored_request(chat: ChatPilot, confirmation_id) -> Confirmation:
    async with chat.server.session_factory() as session:
        found = await session.get(Confirmation, confirmation_id)
        assert found is not None
        return found


async def test_the_card_of_a_request_made_with_sam_says_samuel(owner_chat: ChatPilot, add_member):
    samuel = await add_member("Samuel", "adult")
    model = owner_chat.use_model(
        FakeChatModel(
            tool_script=[call("household_remove", {"person": "Sam"}, "c1"), "Shall I?"],
            judge="yes",
        )
    )
    reply = await owner_chat.say("Remove Sam")
    assert reply.actions == [{"tool": "household_remove", "status": "waiting"}]
    request = await stored_request(owner_chat, reply.pending_confirmation_id)
    # What is stored and run names him whole...
    assert dict(request.arguments) == {"person": "Samuel"}
    assert request.action["target"] == f"person:{samuel.id}"
    # ...and so do the card, the page and the judge: never "Sam" alone.
    card = html.unescape(re.sub(r"<[^>]+>", " ", reply.cards_html))
    assert "“Samuel”" in card and "“Sam”" not in card
    page = html.unescape((await owner_chat.get("/chat")).text)
    assert "“Samuel”" in page and "“Sam”" not in page
    [judged] = model.judge_calls()
    told = json.dumps([dump(item) for item in judged.messages], ensure_ascii=False)
    assert "“Samuel”" in told and "“Sam”" not in told


async def test_remove_sam_while_sams_invitation_waits_removes_nobody(
    owner_chat: ChatPilot, add_member, invite_for_test, session_factory
):
    samuel = await add_member("Samuel", "adult")
    await invite_for_test("Sam", "child")
    reply = await owner_chat.say_with_tool("household_remove", {"person": "Sam"}, judge="do")
    assert reply.actions == [
        {"tool": "household_remove", "status": "invalid", "reason": "not_found"}
    ]
    assert "did you mean: Samuel?" in reply.model_results[0]["detail"]
    async with session_factory() as session:
        found = await session.get(Person, samuel.id)
        assert found is not None and found.disabled_at is None


async def test_a_chain_names_whom_its_steps_act_on_whole(owner_chat: ChatPilot, add_member):
    await add_member("Samuel", "adult", username="samuel")
    chain = {
        "steps": [
            {"tool": "household_change_role", "arguments": {"person": "Sam", "role": "child"}},
            {"tool": "note_leave", "arguments": {"recipient": "sam", "text": "Welcome back"}},
        ]
    }
    reply = await owner_chat.say_with_tool("do_in_order", chain, judge="yes")
    request = await stored_request(owner_chat, reply.pending_confirmation_id)
    steps = request.arguments["steps"]
    assert steps[0]["arguments"]["person"] == "Samuel"
    assert steps[1]["arguments"]["recipient"] == "Samuel"
    card = html.unescape(re.sub(r"<[^>]+>", " ", reply.cards_html))
    assert "“Samuel”" in card and "“Sam”" not in card


# --- a share or a limit runs for the person the yes was given for ---------------------------------


async def _a_key_and_samuel(session_factory, clock) -> tuple[Person, str, Person]:
    async with session_factory() as writer:
        owner = Person(display_name="Owner", role="owner")
        alex = Person(display_name="Alex", role="adult")
        samuel = Person(display_name="Samuel", role="adult")
        writer.add_all([owner, alex, samuel])
        await writer.commit()
    key = await add_key(session_factory, alex.id)
    return alex, key.id, samuel


def _context(person: Person, session_factory, clock) -> ToolContext:
    return ToolContext(
        session_factory=session_factory,
        actor=Actor(person.id, "adult"),
        person=person,
        conversation_id=uuid.uuid4(),
        origin="",
        gatekeeper=None,
        clock=clock,
        registry=TOOLS,
        policy=DEFAULT_POLICY,
        reply_message_id=uuid.uuid4(),
    )


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("key_share", {"with": "person", "person": "Samuel", "allowed": True}),
        ("key_limit", {"person": "Samuel", "amount": 5, "currency": "EUR"}),
    ],
)
async def test_a_stored_share_or_limit_runs_for_the_person_it_was_described_for(
    session_factory, clock, tool, arguments
):
    alex, key_id, samuel = await _a_key_and_samuel(session_factory, clock)
    ctx = _context(alex, session_factory, clock)
    asked = {"key": key_id, **arguments}
    described = await TOOLS[tool].describe(asked, ctx.actor, ctx)
    assert described.target == f"provider:{key_id} person:{samuel.id}"
    # Before the yes, "Samuel" comes to mean someone else: Samuel renames himself, and a new
    # Samuel joins.
    async with session_factory() as writer:
        await writer.execute(
            update(Person).where(Person.id == samuel.id).values(display_name="Sami")
        )
        writer.add(Person(display_name="Samuel", role="child"))
        await writer.commit()
    with pytest.raises(InvalidArguments) as refused_now:
        await TOOLS[tool].execute(asked, replace(ctx, described_action=described))
    assert refused_now.value.code == "not_found"
    async with session_factory() as session:
        shared = await session.scalars(
            select(ProviderUser).where(ProviderUser.provider_id == key_id)
        )
        assert [row for row in shared if row.person_id is not None] == []
    # The same call, described for whom it means now, runs.
    again = await TOOLS[tool].describe(asked, ctx.actor, ctx)
    outcome = await TOOLS[tool].execute(asked, replace(ctx, described_action=again))
    assert outcome.status == "done"


async def test_a_name_among_the_records_never_takes_the_household_member_it_begins(
    session, add_person, clock
):
    from domo_core.household.service import names_among

    await add_person("Owner", "owner")
    await add_person("Tomas", "adult")
    tom = await add_person("Tom", "guest", expires_at=clock() - timedelta(days=1))
    # Among the household "Tom" is the beginning of "Tomas"; among the records it is Tom.
    assert (await resolve_person(session, "Tom", now=clock())).display_name == "Tomas"
    found = await resolve_person(session, "Tom", now=clock(), among="records")
    assert found.id == tom.id
    named = await whole_names(
        session, {"person": "Tom"}, ("person",), now=clock(), speaker=None, among="records"
    )
    assert named == {"person": "Tom"}
    assert names_among(TOOLS["record_look"]) == "records"
    # A removal and a change of role act on a record too (with or without access); handing the
    # owner's role over is only ever to someone of the household.
    assert names_among(TOOLS["household_remove"]) == "records"
    assert names_among(TOOLS["household_change_role"]) == "records"
    assert names_among(TOOLS["household_transfer_ownership"]) == "household"
