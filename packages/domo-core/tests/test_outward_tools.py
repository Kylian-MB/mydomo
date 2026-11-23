"""``address_open``, ``address_close``, ``addresses_look``, ``account_ask``, ``account_remove``,
``account_share`` and ``accounts_look`` (``tools.outward_tools``, step 2, spec 13.3 to 13.5, 13.10):
what the owner closed or removed stays so whatever Domo does, a build or a new version of a tool
included (Review Focus 4); a port of the machine that hosts Domo takes the owner's yes, or a right
of his he gave Domo; an account is asked for good, its form shown once to whoever gives it; and no
result nor audit line holds a value, a form's token or its path.

The tools are tried directly, with the sessions of the test database, and through the barrier's
decision for what only the rights can say. The gatekeeper is a fake (``test_outward_service``)."""

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from domo_core.accounts.models import Person
from domo_core.audit.models import AuditEntry
from domo_core.db import SessionFactory
from domo_core.household.service import disable
from domo_core.madetools.models import MadeToolTrigger
from domo_core.madetools.triggers import _claim_due  # pyright: ignore[reportPrivateUsage]
from domo_core.notes.models import Note
from domo_core.outward.models import DOMO, OpenAddress, ServiceAccount
from domo_core.outward.service import account_for, open_for
from domo_core.policy.actions import DescribedAction
from domo_core.policy.barrier import Allow, Refuse, decide
from domo_core.policy.catalog import DEFAULT_POLICY, OWNER_ONLY_ACTIONS, is_silent
from domo_core.policy.models import PolicyRight
from domo_core.policy.rights import Right, RightsTable, load_rights
from domo_core.policy.roles import Actor, domo_actor
from domo_core.tools.context import (
    ActsAsDomo,
    AsksItsYes,
    InvalidArguments,
    NamesPeople,
    ToolContext,
    ToolOutcome,
)
from domo_core.tools.registry import TOOLS, tools_for
from sqlalchemy import delete, select, update
from test_made_tools_tools import add_tool, household
from test_outward_service import FakeGatekeeper

NOW = datetime(2030, 1, 7, 9, 0, tzinfo=UTC)
ORIGIN = "https://domo.example.com/"
OUTWARD_TOOLS = (
    "address_open",
    "address_close",
    "addresses_look",
    "account_ask",
    "account_remove",
    "account_share",
    "accounts_look",
)
ADDRESS = {"host": "192.168.1.20", "port": 8123, "tool": "lights"}
TOKEN = {"kind": "header", "name": "Authorization", "template": "Bearer {value}"}
# What Domo writes to whoever gives an account he asks for on his own, in their language.
WORDS = "  Sam, peux-tu me donner le compte des lumières ? Dis-le-moi et je t'ouvre le formulaire. "
ACCOUNT = {
    "name": "lights",
    "for": "house",
    "hosts": [{"host": "192.168.1.20", "port": 8123}],
    "place": TOKEN,
}


def context(
    person: Person,
    session_factory: SessionFactory,
    gatekeeper: FakeGatekeeper | None = None,
    *,
    on_his_own: bool = False,
    origin: str = ORIGIN,
) -> ToolContext:
    return ToolContext(
        session_factory=session_factory,
        actor=Actor(person.id, person.role),  # type: ignore[arg-type]
        person=person,
        conversation_id=uuid.uuid4(),
        origin=origin,
        gatekeeper=gatekeeper or FakeGatekeeper(),  # type: ignore[arg-type]
        clock=lambda: NOW,
        registry=TOOLS,
        policy=DEFAULT_POLICY,
        reply_message_id=None if on_his_own else uuid.uuid4(),
        no_reader=on_his_own,
        on_his_own=on_his_own,
    )


def later(ctx: ToolContext, minutes: int) -> ToolContext:
    """``ctx`` a few minutes on: each act after the one before."""
    return replace(ctx, clock=lambda: NOW + timedelta(minutes=minutes))


async def run(tool_name: str, ctx: ToolContext, /, **arguments: Any) -> ToolOutcome:
    """Describe then execute ``tool_name`` in ``ctx``."""
    tool = TOOLS[tool_name]
    await tool.describe(arguments, ctx.actor, ctx)
    return await tool.execute(arguments, ctx)


async def refused(tool_name: str, ctx: ToolContext, /, **arguments: Any) -> InvalidArguments:
    """The refusal of ``tool_name`` in ``ctx``, the same when described and when executed."""
    tool = TOOLS[tool_name]
    with pytest.raises(InvalidArguments) as described:
        await tool.describe(arguments, ctx.actor, ctx)
    with pytest.raises(InvalidArguments) as executed:
        await tool.execute(arguments, ctx)
    assert described.value.detail == executed.value.detail
    return executed.value


async def asks_the_owner(ctx: ToolContext, arguments: dict[str, Any] = ADDRESS) -> None:
    """Out of the owner's own turn, a port of the host without Domo's right: the call waits
    for the owner's yes as Domo's request (ruling R24), and run without that yes it opens
    nothing."""
    tool = TOOLS["address_open"]
    waits = await tool.waits_for(arguments, ctx)  # type: ignore[attr-defined]
    assert waits is not None and (waits.approver, waits.domo_asks) == ("owner", True)
    with pytest.raises(InvalidArguments) as ran:
        await tool.execute(arguments, ctx)
    assert ran.value.code == "not_possible" and "the owner's yes" in str(ran.value.detail)


async def audit(session_factory: SessionFactory, prefix: str = "") -> list[AuditEntry]:
    async with session_factory() as session:
        found = await session.scalars(
            select(AuditEntry).where(AuditEntry.action.startswith(prefix)).order_by(AuditEntry.id)
        )
        return list(found.all())


async def live(session_factory: SessionFactory, tool: str | None = "lights") -> list[str]:
    async with session_factory() as session:
        return [f"{a.host}:{a.port}" for a in await open_for(session, tool)]


def test_the_seven_tools_and_how_they_are_judged():
    for name in OUTWARD_TOOLS:
        assert name in TOOLS
    # Addresses are Domo's own acts; accounts are the person's to give, remove and share.
    acts = {name for name in OUTWARD_TOOLS if isinstance(TOOLS[name], ActsAsDomo)}
    assert acts == {"address_open", "address_close", "addresses_look"}
    assert isinstance(TOOLS["address_open"], AsksItsYes)
    assert isinstance(TOOLS["account_ask"], NamesPeople)
    assert isinstance(TOOLS["account_remove"], NamesPeople)
    # Opening and asking are shown (ruling R8), closing and looking are not.
    silent = {name for name in OUTWARD_TOOLS if is_silent(TOOLS[name].action_type)}
    assert silent == {"address_close", "addresses_look", "accounts_look"}


# --- Review Focus 4: what the owner closed stays closed ------------------------------------------


async def test_what_the_owner_closes_stays_closed_to_domo_a_build_and_a_new_version(
    session_factory: SessionFactory,
):
    alex, sam, _, _ = await household(session_factory)
    await add_tool(session_factory, "lights")
    owner = context(alex, session_factory)
    opened = await run("address_open", owner, **ADDRESS)
    assert opened.model_result == {**ADDRESS, "status": "done", "on_host": False}
    assert opened.card == {
        "key": "action.card.done.net.open",
        "params": {"address": "192.168.1.20:8123"},
    }
    closed = await run("address_close", later(owner, 1), **ADDRESS)
    assert closed.model_result == {"status": "done", "closed": True}
    async with session_factory() as session:
        (row,) = (await session.scalars(select(OpenAddress))).all()
        assert (row.closed_by, row.closed_by_owner) == (str(alex.id), True)
    # Opening it again is Domo's judgement (ruling R24), for this tool, another one or by hand:
    # the call says the owner closed it, and when, and a yes asked out of his own turn is his.
    tool = TOOLS["address_open"]
    by_hand = {"host": ADDRESS["host"], "port": ADDRESS["port"]}
    for arguments in (ADDRESS, {**ADDRESS, "tool": "heating"}, by_hand):
        theirs = later(context(sam, session_factory), 2)
        action = await tool.describe(arguments, theirs.actor, theirs)
        assert action.action_type == "net.open"
        assert (action.facts["closed_by_owner"], action.facts["closed_on"]) == (
            True,
            (NOW + timedelta(minutes=1)).date().isoformat(),
        )
        assert await tool.whose_yes(arguments, theirs) == "owner"  # type: ignore[attr-defined]
        assert await tool.waits_for(arguments, theirs) is None  # type: ignore[attr-defined]
    assert await tool.whose_yes(ADDRESS, later(owner, 2)) is None  # type: ignore[attr-defined]
    # A new version of the tool opens a build, and no address.
    await run(
        "tool_change",
        context(sam, session_factory),
        name="lights",
        purpose="Turn the lights on or off.",
        examples=[{"entry": {"amount": 1}, "expected": {"total": 1}}],
    )
    assert await live(session_factory) == []
    # A request run without the owner's own yes on it opens nothing.
    with pytest.raises(InvalidArguments):
        await tool.execute(ADDRESS, replace(later(owner, 3), approved_by=alex.id))
    assert await live(session_factory) == []
    # On his own, once his judge let it through, he opens it again; the audit says so.
    await tool.execute(ADDRESS, later(context(alex, session_factory, on_his_own=True), 4))
    assert await live(session_factory) == ["192.168.1.20:8123"]
    (opening, closing, again) = await audit(session_factory, "outward.address_")
    assert (opening.action, closing.action, again.action) == (
        "outward.address_opened",
        "outward.address_closed",
        "outward.address_opened",
    )
    assert opening.actor == f"person:{alex.id}"
    assert opening.details == {
        "tool": "lights",
        "host": "192.168.1.20",
        "port": 8123,
        "on_host": False,
        "zone": "house",
        "under": None,
        "reopened": False,
        "turn_of": f"person:{alex.id}",
    }
    assert again.actor == "domo"
    assert (again.details["under"], again.details["reopened"], again.details["turn_of"]) == (
        "on_his_own",
        True,
        "domo",
    )
    assert closing.details == {**ADDRESS, "by_owner": True}


async def test_a_closing_by_someone_else_leaves_it_to_domo(session_factory: SessionFactory):
    alex, sam, _, _ = await household(session_factory)
    by_hand = {"host": "device.lan", "port": 80}
    await run("address_open", context(sam, session_factory), **by_hand)
    await run("address_close", later(context(sam, session_factory), 1), **by_hand)
    alone = later(context(alex, session_factory, on_his_own=True), 2)
    await run("address_open", alone, **by_hand)
    assert await live(session_factory, None) == ["device.lan:80"]
    (row,) = [a for a in await rows(session_factory) if a.closed_at is None]
    assert row.opened_by == DOMO
    look = await run("addresses_look", alone)
    assert look.model_result == {
        "open": [{"tool": None, "host": "device.lan", "port": 80, "on_host": False}],
        "closed_by_the_owner": [],
    }
    assert look.card == {"key": "action.card.done", "params": {}}


async def rows(session_factory: SessionFactory) -> list[OpenAddress]:
    async with session_factory() as session:
        return list((await session.scalars(select(OpenAddress))).all())


async def test_an_address_of_a_tool_closed_to_someone_is_not_touched_in_their_conversation(
    session_factory: SessionFactory,
):
    alex, _, dana, _ = await household(session_factory)
    await add_tool(session_factory, "lights", roles=("owner", "adult"))
    refusal = await refused("address_open", context(dana, session_factory), **ADDRESS)
    assert refusal.code == "not_possible"
    await run("address_open", context(alex, session_factory), **ADDRESS)
    assert (await refused("address_close", context(dana, session_factory), **ADDRESS)).code == (
        "not_possible"
    )


# --- a port of the machine that hosts Domo -----------------------------------------------------

ON_HOST = {"host": "host.docker.internal", "port": 8123, "tool": "lights", "on_host": True}


async def test_a_port_of_the_host_takes_the_owner_s_yes_or_his_words(
    session_factory: SessionFactory,
):
    alex, sam, _, _ = await household(session_factory)
    tool = TOOLS["address_open"]
    owner = context(alex, session_factory)
    action = await tool.describe(ON_HOST, owner.actor, owner)
    # Judged as an opening of Domo's, which always waits for the owner's yes.
    assert action.action_type == "net.open" and action.facts["on_host"] is True
    # A port of the host is an address of the house (spec 13.10), whatever its name.
    assert (action.facts["zone"], action.facts["outward"]) == ("house", False)
    assert action.destination == "household"
    async with session_factory() as session:
        rights = await load_rights(session)
    assert isinstance(decide(domo_actor(), action, DEFAULT_POLICY, rights, {}), Allow)
    # In the owner's own turn it is judged on his words (R23): no yes of the code's.
    assert await tool.waits_for(ON_HOST, owner) is None  # type: ignore[attr-defined]
    # A request run without his own yes on it opens nothing.
    with pytest.raises(InvalidArguments):
        await tool.execute(ON_HOST, replace(owner, approved_by=alex.id))
    assert await live(session_factory) == [] and await rows(session_factory) == []
    # In anyone else's conversation, or on his own, it waits for the owner's yes (R24).
    other = {**ON_HOST, "port": 9000}
    await asks_the_owner(context(sam, session_factory), other)
    await asks_the_owner(context(alex, session_factory, on_his_own=True), other)
    # Let through by the judge in his own turn, it opens on his words.
    await tool.execute(ON_HOST, owner)
    assert await live(session_factory) == ["host.docker.internal:8123"]
    (entry,) = await audit(session_factory, "outward.")
    assert (entry.details["under"], entry.details["reopened"]) == ("owner_asked", False)


async def test_a_port_of_the_host_the_owner_closed_is_still_the_right_s_case(
    session_factory: SessionFactory,
):
    # Without the right, the right comes first: closed by the owner or not, out of his own turn
    # it waits for his yes and is never judged; the facts still say he closed it.
    alex, sam, _, _ = await household(session_factory)
    owner = context(alex, session_factory)
    await run("address_open", owner, **ON_HOST)
    closing = {key: ON_HOST[key] for key in ("host", "port", "tool")}
    await run("address_close", later(owner, 1), **closing)
    assert await live(session_factory) == []
    tool = TOOLS["address_open"]
    for ctx in (
        later(context(sam, session_factory), 2),
        later(context(alex, session_factory, on_his_own=True), 2),
    ):
        action = await tool.describe(ON_HOST, ctx.actor, ctx)
        assert action.action_type == "net.open" and action.facts["closed_by_owner"] is True
        await asks_the_owner(ctx, ON_HOST)
    assert await live(session_factory) == []
    # In his own turn, on his words: opened, and audited as a port of the host opened again.
    await tool.execute(ON_HOST, later(owner, 3))
    assert await live(session_factory) == ["host.docker.internal:8123"]
    again = (await audit(session_factory, "outward.address_opened"))[-1]
    assert (again.details["under"], again.details["on_host"], again.details["reopened"]) == (
        "owner_asked",
        True,
        True,
    )


async def test_with_the_right_of_the_owner_domo_opens_a_port_of_the_host_on_his_own(
    session_factory: SessionFactory,
):
    alex, _, _, _ = await household(session_factory)
    async with session_factory() as writer:
        writer.add(
            PolicyRight(
                for_domo=True,
                action_type="net.open_host",
                access="allow",
                summary="The assistant opens the ports of his own machine.",
                owner_words="you may open the ports of your machine",
                created_by=alex.id,
            )
        )
        await writer.commit()
    tool = TOOLS["address_open"]
    alone = context(alex, session_factory, on_his_own=True)
    action = await tool.describe(ON_HOST, alone.actor, alone)
    assert action.action_type == "net.open_host"
    async with session_factory() as session:
        rights = await load_rights(session)
    assert isinstance(decide(domo_actor(), action, DEFAULT_POLICY, rights, {}), Allow)
    assert await tool.waits_for(ON_HOST, alone) is None  # type: ignore[attr-defined]
    await tool.execute(ON_HOST, alone)
    (row,) = await rows(session_factory)
    assert (row.on_host, row.opened_by) == (True, DOMO)
    (entry,) = await audit(session_factory, "outward.")
    assert entry.actor == "domo"
    assert entry.details == {
        "tool": "lights",
        "host": "host.docker.internal",
        "port": 8123,
        "on_host": True,
        "zone": "house",
        "under": "domo_right",
        "reopened": False,
        "turn_of": "domo",
    }


async def test_an_address_on_the_internet_leaves_the_house(session_factory: SessionFactory):
    _, sam, _, _ = await household(session_factory)
    ctx = context(sam, session_factory)
    tool = TOOLS["address_open"]
    action = await tool.describe({"host": "api.example.com", "port": 443}, ctx.actor, ctx)
    assert action.destination == "external"
    assert (action.facts["zone"], action.facts["outward"], action.facts["tool"]) == (
        "internet",
        True,
        None,
    )
    inside = await tool.describe(ADDRESS, ctx.actor, ctx)
    assert inside.destination == "household" and inside.facts["outward"] is False


# --- accounts ------------------------------------------------------------------------------------


async def test_an_account_is_asked_for_good_and_its_form_shown_once_to_whoever_gives_it(
    session_factory: SessionFactory,
):
    alex, _, _, _ = await household(session_factory)
    gatekeeper = FakeGatekeeper()
    ctx = context(alex, session_factory, gatekeeper)
    tool = TOOLS["account_ask"]
    action = await tool.describe(ACCOUNT, ctx.actor, ctx)
    assert action.action_type == "account.ask" and action.effect == "secrets"
    assert dict(action.facts) == {
        "name": "lights",
        "place": "header",
        "for_house": True,
        "giver": f"person:{alex.id}",
        "hosts": 1,
        "outward": False,
    }
    done = await tool.execute(ACCOUNT, ctx)
    assert done.model_result == {"status": "done", "name": "lights", "form": "shown"}
    assert done.card == {"key": "action.card.done.account.ask", "params": {"name": "lights"}}
    assert done.card_once == {
        "key": "action.card.once.account.ask",
        "params": {},
        "link": "https://domo.example.com/vault/r/token-1",
    }
    async with session_factory() as session:
        (account,) = (await session.scalars(select(ServiceAccount))).all()
    (request,) = gatekeeper.asked
    # The request is made from the row: its reference, its hosts and place, and a purpose written
    # from its name and its place (the giver's words).
    assert request.ref == account.ref and request.fields == "value"
    assert [(b.host, b.port, b.location.kind) for b in request.bindings] == [
        ("192.168.1.20", 8123, "header")
    ]
    assert request.purpose == "The account “lights”, sent in the authorization header"
    (entry,) = await audit(session_factory, "outward.")
    assert entry.details == {
        "name": "lights",
        "for_house": True,
        "place": "header",
        "hosts": 1,
        "giver": str(alex.id),
        "again": False,
        "form": True,
    }
    # Given, it is never asked again: nothing replaces it.
    gatekeeper.given.add(account.ref)
    with pytest.raises(InvalidArguments) as again:
        await tool.execute(ACCOUNT, ctx)
    assert again.value.code == "not_possible" and str(again.value.detail).startswith("exists")
    assert len(gatekeeper.asked) == 1


async def test_a_login_account_says_where_it_signs_in_and_takes_two_fields(
    session_factory: SessionFactory,
):
    _, sam, _, _ = await household(session_factory)
    gatekeeper = FakeGatekeeper()
    login = {
        "kind": "login",
        "path": "/api/login",
        "encoding": "json",
        "user_field": "user",
        "password_field": "password",
    }
    arguments = {**ACCOUNT, "name": "shop", "for": "me", "place": login}
    await run("account_ask", context(sam, session_factory, gatekeeper), **arguments)
    (request,) = gatekeeper.asked
    assert request.fields == "user_password"
    assert request.purpose == (
        "The account “shop”: a user name and a password, to sign in at /api/login"
    )


@pytest.mark.parametrize(
    "place",
    [
        {"kind": "cookie"},
        {"kind": "header", "name": "content-length"},
        {"kind": "header", "name": "authorization", "template": "Bearer abcdef0123456789abcdef"},
        {"kind": "header", "name": "authorization", "template": "Bearer"},
        {"kind": "basic", "name": "x"},
        {
            "kind": "login",
            "path": "/a?b",
            "encoding": "form",
            "user_field": "u",
            "password_field": "p",
        },
        {
            "kind": "login",
            "path": "/a",
            "encoding": "xml",
            "user_field": "u",
            "password_field": "p",
        },
        {
            "kind": "login",
            "path": "/a",
            "encoding": "form",
            "user_field": "u",
            "password_field": "u",
        },
    ],
)
async def test_a_place_that_cannot_be_is_refused(session_factory: SessionFactory, place):
    alex, _, _, _ = await household(session_factory)
    refusal = await refused(
        "account_ask", context(alex, session_factory), **{**ACCOUNT, "place": place}
    )
    assert refusal.code in ("invalid", "not_possible")


async def test_who_gives_an_account(session_factory: SessionFactory):
    alex, sam, dana, paul = await household(session_factory)
    # A child gives an account of their own, not the house's; a guest gives none.
    refusal = await refused("account_ask", context(dana, session_factory), **ACCOUNT)
    assert refusal.code == "not_possible"
    await run("account_ask", context(dana, session_factory), **{**ACCOUNT, "for": "me"})
    guest = await refused("account_ask", context(paul, session_factory), **{**ACCOUNT, "for": "me"})
    assert guest.code == "not_possible"
    # In someone's conversation, it is theirs to give.
    named = await refused("account_ask", context(sam, session_factory), **ACCOUNT, giver="Dana")
    assert named.code == "invalid"
    # Words for a note are for a giver Domo names on his own, never in someone's conversation.
    noted = await refused("account_ask", context(sam, session_factory), **ACCOUNT, note=WORDS)
    assert noted.code == "invalid"
    del alex


async def test_on_his_own_domo_names_who_gives_it_and_a_note_of_his_tells_them(
    session_factory: SessionFactory,
):
    alex, sam, _, _ = await household(session_factory)
    gatekeeper = FakeGatekeeper()
    alone = context(alex, session_factory, gatekeeper, on_his_own=True)
    assert (await refused("account_ask", alone, **ACCOUNT, note=WORDS)).code == "invalid"
    # The words to the giver are Domo's own, in their language: required on his own.
    assert (await refused("account_ask", alone, **ACCOUNT, giver="Sam")).code == "invalid"
    done = await run("account_ask", alone, **ACCOUNT, giver="Sam", note=WORDS)
    assert done.model_result == {
        "status": "done",
        "name": "lights",
        "form": "waits",
        "note_left_for": "Sam Martin",
    }
    assert done.card_once is None and gatekeeper.asked == []
    async with session_factory() as session:
        (note,) = (await session.scalars(select(Note))).all()
        (account,) = (await session.scalars(select(ServiceAccount))).all()
    assert (note.by_domo, note.author_id, note.recipient_id) == (True, None, sam.id)
    assert note.text == WORDS.strip() and note.exact_text is None
    assert account.given_by == sam.id and account.person_id is None
    # In Sam's conversation, the same call shows him the form of that very account.
    shown = await run("account_ask", context(sam, session_factory, gatekeeper), **ACCOUNT)
    assert shown.model_result["form"] == "shown"
    (request,) = gatekeeper.asked
    assert request.ref == account.ref
    asked = await audit(session_factory, "outward.account_asked")
    assert [entry.actor for entry in asked] == ["domo", f"person:{sam.id}"]
    assert [entry.details["again"] for entry in asked] == [False, True]


async def test_a_form_nobody_can_see_now_waits(session_factory: SessionFactory):
    alex, _, _, _ = await household(session_factory)
    gatekeeper = FakeGatekeeper()
    for ctx in (
        context(alex, session_factory, gatekeeper, origin=""),
        replace(context(alex, session_factory, gatekeeper), in_a_chain=True),
    ):
        done = await run("account_ask", ctx, **ACCOUNT)
        assert done.model_result["form"] == "waits" and "detail" in done.model_result
        assert done.card_once is None
    assert gatekeeper.asked == []


async def test_what_the_owner_removed_stays_removed(session_factory: SessionFactory):
    alex, sam, _, _ = await household(session_factory)
    await run("account_ask", context(alex, session_factory), **ACCOUNT)
    removing = {"name": "lights", "for": "house"}
    # Only whoever gave it, or the owner.
    assert (await refused("account_remove", context(sam, session_factory), **removing)).code == (
        "not_possible"
    )
    alone = context(alex, session_factory, on_his_own=True)
    assert (await refused("account_remove", alone, **removing)).code == "not_possible"
    done = await run("account_remove", later(context(alex, session_factory), 1), **removing)
    assert done.card == {"key": "action.card.done.account.remove", "params": {"name": "lights"}}
    (entry,) = await audit(session_factory, "outward.account_removed")
    assert entry.details == {"name": "lights", "for_house": True, "of": None, "by_owner": True}
    # Neither Domo on his own nor anyone else asks for it again; the owner does.
    for ctx, more in (
        (context(alex, session_factory, on_his_own=True), {"giver": "Sam", "note": WORDS}),
        (context(sam, session_factory), {}),
    ):
        refusal = await refused("account_ask", later(ctx, 2), **ACCOUNT, **more)
        assert str(refusal.detail).startswith("removed_by_owner")
    await run("account_ask", later(context(alex, session_factory), 3), **ACCOUNT)


async def test_the_owner_removes_someone_s_own_account(session_factory: SessionFactory):
    alex, sam, dana, _ = await household(session_factory)
    mine = {**ACCOUNT, "for": "me"}
    await run("account_ask", context(dana, session_factory), **mine)
    of_dana = {"name": "lights", "for": "person", "of": "Dana"}
    assert (await refused("account_remove", context(sam, session_factory), **of_dana)).code == (
        "not_possible"
    )
    await run("account_remove", later(context(alex, session_factory), 1), **of_dana)
    (entry,) = await audit(session_factory, "outward.account_removed")
    assert entry.details["of"] == str(dana.id) and entry.details["for_house"] is False


async def test_an_own_account_the_owner_removed_is_given_again_by_its_person_alone(
    session_factory: SessionFactory,
):
    # Ruling R10: a personal account the owner removed is asked again in its person's turn or
    # the owner's, never by Domo on his own nor in anyone else's turn.
    alex, sam, dana, _ = await household(session_factory)
    mine = {**ACCOUNT, "for": "me"}
    await run("account_ask", context(dana, session_factory), **mine)
    of_dana = {"name": "lights", "for": "person", "of": "Dana"}
    await run("account_remove", later(context(alex, session_factory), 1), **of_dana)
    alone = later(context(alex, session_factory, on_his_own=True), 2)
    refusal = await refused("account_ask", alone, **mine, giver="Dana", note=WORDS)
    assert str(refusal.detail).startswith("removed_by_owner")
    await run("account_ask", later(context(dana, session_factory), 3), **mine)
    del sam


async def test_a_child_removes_an_account_of_their_own_and_no_other(
    session_factory: SessionFactory,
):
    # Spec 13.4: whoever gave it removes it; a child, their own only.
    alex, _, dana, _ = await household(session_factory)
    assert "account_remove" in {d.name for d in tools_for("child", TOOLS, DEFAULT_POLICY)}
    await run("account_ask", context(alex, session_factory), **ACCOUNT)
    await run("account_ask", context(dana, session_factory), **{**ACCOUNT, "for": "me"})
    house = {"name": "lights", "for": "house"}
    assert (await refused("account_remove", context(dana, session_factory), **house)).code == (
        "not_possible"
    )
    await run("account_ask", context(alex, session_factory), **{**ACCOUNT, "for": "me"})
    of_alex = {"name": "lights", "for": "person", "of": "Alex"}
    assert (await refused("account_remove", context(dana, session_factory), **of_alex)).code == (
        "not_possible"
    )
    await run("account_remove", context(dana, session_factory), **{"name": "lights", "for": "me"})


async def test_sharing_a_house_account_is_the_owner_s_alone():
    # Not on the default rights only: no right gives it to anyone else, nor to Domo.
    assert "account.share" in OWNER_ONLY_ACTIONS
    rights = (
        Right(uuid.uuid4(), None, "adult", "account.share", "allow", None, None, None, None, "s"),
        Right(
            uuid.uuid4(), None, None, "account.share", "allow", None, None, None, None, "s", True
        ),
    )
    given = RightsTable(rights)
    action = DescribedAction("core", "account.share", "account:lights", "secrets", "household")
    for who in (Actor(uuid.uuid4(), "adult"), domo_actor()):
        assert isinstance(decide(who, action, DEFAULT_POLICY, given, {}), Refuse)
    owner = Actor(uuid.uuid4(), "owner")
    assert isinstance(decide(owner, action, DEFAULT_POLICY, given, {}), Allow)


async def test_who_leaves_has_their_timed_launches_and_accounts_wait_and_get_them_back(
    session_factory: SessionFactory,
):
    # Spec 5.8: a person who leaves has their timed launches and their accounts frozen, taken back
    # when they return; deleted with them. Nothing in ``household`` needed changing for it.
    _, _, dana, _ = await household(session_factory)
    tool_id = await add_tool(session_factory, "lights")
    await run("account_ask", context(dana, session_factory), **{**ACCOUNT, "for": "me"})
    async with session_factory() as writer:
        writer.add(
            MadeToolTrigger(
                tool_id=tool_id,
                kind="time",
                due_at=NOW - timedelta(minutes=1),
                for_person_id=dana.id,
            )
        )
        await disable(writer, await writer.get_one(Person, dana.id), NOW, by=dana.id)
        await writer.commit()

    async def hers() -> tuple[int, bool]:
        async with session_factory() as session:
            found = await account_for(
                session, name="lights", for_person=dana.id, on_his_own=False, is_guest=False
            )
        async with session_factory() as session:
            fired = await _claim_due(session, NOW)
        return len(fired), found is not None and found.person_id == dana.id

    assert await hers() == (0, False)
    async with session_factory() as writer:
        # She comes back (``household.service.accept_return`` empties ``disabled_at``).
        await writer.execute(update(Person).where(Person.id == dana.id).values(disabled_at=None))
        await writer.commit()
    assert await hers() == (1, True)
    # Deleted, her accounts and what was timed for her go with her row.
    async with session_factory() as writer:
        await writer.execute(delete(Person).where(Person.id == dana.id))
        await writer.commit()
    async with session_factory() as session:
        left = await session.scalars(
            select(ServiceAccount.id).where(ServiceAccount.person_id == dana.id)
        )
        timed = await session.scalars(
            select(MadeToolTrigger.id).where(MadeToolTrigger.for_person_id == dana.id)
        )
        assert (list(left), list(timed)) == ([], [])


async def test_the_owner_shares_a_house_account_and_each_one_sees_what_they_have(
    session_factory: SessionFactory,
):
    alex, sam, dana, paul = await household(session_factory)
    gatekeeper = FakeGatekeeper()
    await run("account_ask", context(alex, session_factory, gatekeeper), **ACCOUNT)
    music = {**ACCOUNT, "name": "music", "for": "me"}
    await run("account_ask", context(dana, session_factory, gatekeeper), **music)
    async with session_factory() as session:
        house = await session.scalar(select(ServiceAccount).where(ServiceAccount.name == "lights"))
    assert house is not None
    gatekeeper.given.add(house.ref)

    async def names(ctx: ToolContext) -> list[tuple[str, str]]:
        found = await run("accounts_look", ctx)
        assert found.card == {"key": "action.card.done", "params": {}}
        return [(a["name"], a["whose"]) for a in found.model_result["accounts"]]

    assert await names(context(dana, session_factory, gatekeeper)) == [
        ("lights", "house"),
        ("music", "yours"),
    ]
    assert await names(context(alex, session_factory, gatekeeper)) == [
        ("lights", "house"),
        ("music", "Dana"),
    ]
    assert await names(context(alex, session_factory, gatekeeper, on_his_own=True)) == [
        ("lights", "house")
    ]
    assert await names(context(paul, session_factory, gatekeeper)) == []
    shared = await run("account_share", context(alex, session_factory), name="lights", guests=True)
    assert shared.model_result == {
        "status": "done",
        "name": "lights",
        "for_guests": True,
        "for_domo": True,
    }
    look = await run("accounts_look", context(paul, session_factory, gatekeeper))
    (seen,) = look.model_result["accounts"]
    assert seen == {
        "name": "lights",
        "whose": "house",
        "hosts": [{"host": "192.168.1.20", "port": 8123}],
        "place": {"kind": "header", "name": "authorization", "template": "Bearer {value}"},
        "given": True,
        "given_by": "Alex",
        "for_guests": True,
        "for_domo": True,
    }
    (entry,) = await audit(session_factory, "outward.account_shared")
    assert entry.details == {"name": "lights", "guests": True, "domo": None}
    del sam


async def test_no_result_nor_audit_line_holds_a_form_s_token_or_its_path(
    session_factory: SessionFactory,
):
    alex, sam, _, _ = await household(session_factory)
    gatekeeper = FakeGatekeeper()
    said: list[object] = []
    owner = context(alex, session_factory, gatekeeper)
    for name, arguments in (
        ("address_open", ADDRESS),
        ("account_ask", ACCOUNT),
        ("account_ask", {**ACCOUNT, "name": "shop", "for": "me"}),
        ("accounts_look", {}),
        ("addresses_look", {}),
        ("account_share", {"name": "lights", "domo": False}),
        ("address_close", ADDRESS),
        ("account_remove", {"name": "shop", "for": "me"}),
    ):
        outcome = await run(name, owner, **arguments)
        said.extend([outcome.model_result, outcome.card])
    said.extend(entry.details for entry in await audit(session_factory))
    for words in map(str, said):
        assert "token-" not in words and "/vault/r/" not in words
    del sam
