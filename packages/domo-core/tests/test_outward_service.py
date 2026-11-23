"""What Domo does with the addresses and the accounts (``outward.service``, step 2, spec 13.3 to
13.5, 13.10): an address the owner closed stays closed to anyone but the owner, whatever opens it
(Review Focus 4); who a launch may use an account for, in the eight cases; an account is never
replaced, and its form's request is made from its row.

The rows are tried on the shared rolled-back ``session``; the gatekeeper is a fake that keeps what
it was asked."""

import ast
import itertools
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

import pytest
from domo_common.gatekeeper_api import (
    BasicLocation,
    Binding,
    HeaderLocation,
    SecretMetadata,
    SecretRequestCreate,
    SecretRequestView,
)
from domo_common.gatekeeper_api import OpenAddress as AddressToReach
from domo_core.accounts.models import Person
from domo_core.llm.provider_keys import KeyForm
from domo_core.outward.models import DOMO, OpenAddress, ServiceAccount
from domo_core.outward.service import (
    AccountExists,
    ClosedByOwner,
    RemovedByOwner,
    account_for,
    ask_account,
    close_address,
    closed_by_owner_now,
    closed_by_the_owner,
    open_address,
    open_for,
    remove_account,
)
from domo_core.texts.view import REFERENCE
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

NOW = datetime(2030, 1, 7, 9, 0, tzinfo=UTC)
_MINUTES = itertools.count(1)


def later() -> datetime:
    """A moment after every one given before: each act of a test comes after the one before."""
    return NOW + timedelta(minutes=next(_MINUTES))


TOKEN = HeaderLocation(kind="header", name="Authorization", template="Bearer {value}")
HOSTS = [("192.168.1.20", 8123)]


class FakeGatekeeper:
    """The gatekeeper's two calls an account needs: the forms it was asked for, and the secrets it
    holds (``given``)."""

    def __init__(self) -> None:
        self.asked: list[SecretRequestCreate] = []
        self.given: set[str] = set()

    async def create_secret_request(self, request: SecretRequestCreate) -> SecretRequestView:
        self.asked.append(request)
        return SecretRequestView(
            id=f"req{len(self.asked)}",
            ref=request.ref,
            status="pending",
            form_path=f"/vault/r/token-{len(self.asked)}",
            expires_at=NOW + timedelta(minutes=15),
        )

    async def list_secrets(self) -> list[SecretMetadata]:
        return [
            SecretMetadata(
                ref=ref,
                purpose="p",
                bindings=[],
                created_at=NOW,
                rotated_at=None,
                last_used_at=None,
            )
            for ref in sorted(self.given)
        ]


async def person(session: AsyncSession, name: str, role: str, **more) -> Person:
    found = Person(display_name=name, role=role, **more)
    session.add(found)
    await session.flush()
    return found


async def household(session: AsyncSession) -> tuple[Person, Person, Person, Person]:
    return (
        await person(session, "Alex", "owner"),
        await person(session, "Sam", "adult"),
        await person(session, "Dana", "child"),
        await person(session, "Paul", "guest", expires_at=datetime.now(UTC) + timedelta(days=5)),
    )


async def opens(session: AsyncSession, by: str, **more) -> OpenAddress:
    arguments = {"tool_name": "lights", "host": "192.168.1.20", "port": 8123, "on_host": False}
    return await open_address(session, **{**arguments, **more}, by=by, now=later())


async def closes(session: AsyncSession, by: str, **more) -> bool:
    arguments = {"tool_name": "lights", "host": "192.168.1.20", "port": 8123}
    return await close_address(session, **{**arguments, **more}, by=by, now=later())


# --- the addresses --------------------------------------------------------------------------------


async def test_what_the_owner_closed_only_the_owner_opens_again(session: AsyncSession):
    alex, sam, *_ = await household(session)
    await opens(session, DOMO)
    assert await closes(session, str(alex.id))
    # Neither Domo on his own nor anyone else opens it again.
    for by in (DOMO, str(sam.id)):
        with pytest.raises(ClosedByOwner):
            await opens(session, by)
    assert await open_for(session, "lights") == []
    # The owner does; once someone else closes it, it is anyone's again.
    await opens(session, str(alex.id))
    assert await open_for(session, "lights") == [AddressToReach(host="192.168.1.20", port=8123)]
    assert await closes(session, DOMO)
    await opens(session, str(sam.id))
    assert len(await open_for(session, "lights")) == 1


async def test_the_owner_s_closing_outlives_a_change_of_owner(session: AsyncSession):
    alex, sam, *_ = await household(session)
    await opens(session, DOMO)
    await closes(session, str(alex.id))
    (closed,) = (await session.scalars(select(OpenAddress))).all()
    assert (closed.closed_by, closed.closed_by_owner) == (str(alex.id), True)
    # Alex hands the owner's role over: his closing stays the owner's, and he is no longer it.
    alex.role = "adult"
    await session.flush()
    sam.role = "owner"
    await session.flush()
    with pytest.raises(ClosedByOwner):
        await opens(session, str(alex.id))
    await opens(session, str(sam.id))


async def test_an_address_is_one_tool_s_and_a_closing_by_someone_else_is_not_the_owner_s(
    session: AsyncSession,
):
    _, sam, *_ = await household(session)
    first = await opens(session, DOMO)
    # Opening it again gives the same row; another tool's, or the programs run by hand, are apart.
    assert (await opens(session, str(sam.id))).id == first.id
    await opens(session, DOMO, tool_name="heating")
    await opens(session, DOMO, tool_name=None, host="Device.LAN", port=80, on_host=True)
    assert await open_for(session, None) == [
        AddressToReach(host="device.lan", port=80, on_host=True)
    ]
    assert [a.host for a in await open_for(session, "heating")] == ["192.168.1.20"]
    assert await closes(session, str(sam.id))
    assert not await closes(session, str(sam.id))
    row = await session.get(OpenAddress, first.id)
    assert row is not None and (row.closed_by, row.closed_by_owner) == (str(sam.id), False)
    await opens(session, DOMO)
    # Asked as a port of the host, the open one is closed and opened again so, by nobody's will.
    again = await opens(session, DOMO, on_host=True)
    assert again.on_host and again.id != first.id
    assert not await session.scalar(
        select(OpenAddress.closed_by_owner)
        .where(OpenAddress.tool_name == "lights", OpenAddress.closed_at.is_not(None))
        .limit(1)
    )


async def test_what_the_owner_closes_is_closed_for_every_tool_and_for_hand_runs(
    session: AsyncSession,
):
    # Ruling R20: the owner's closing of a host and port closes it whatever tool it was opened
    # for, and nobody but the owner opens it again, for any tool or for the programs run by hand.
    alex, sam, *_ = await household(session)
    await opens(session, DOMO)
    await opens(session, DOMO, tool_name="heating")
    await opens(session, DOMO, tool_name=None)
    await opens(session, DOMO, tool_name="other", port=80)
    # He names one tool: every live row of that host and port is closed, another port stays.
    assert await closes(session, str(alex.id))
    for tool in ("lights", "heating", None):
        assert await open_for(session, tool) == []
    assert len(await open_for(session, "other")) == 1
    for tool in ("lights", "heating", "lights2", None):
        assert await closed_by_the_owner(session, "192.168.1.20", 8123)
        for by in (DOMO, str(sam.id)):
            with pytest.raises(ClosedByOwner):
                await opens(session, by, tool_name=tool)
    closed = await closed_by_owner_now(session)
    assert {(row.host, row.port) for row in closed} == {("192.168.1.20", 8123)}
    # One tool's look shows its own row of it; a tool that never had it, none.
    assert [(row.tool_name, row.host) for row in await closed_by_owner_now(session, "heating")] == [
        ("heating", "192.168.1.20")
    ]
    assert await closed_by_owner_now(session, "lights2") == []
    # The owner opens it again, for one tool: it is anyone's to open again for another.
    await opens(session, str(alex.id), tool_name="heating")
    assert not await closed_by_the_owner(session, "192.168.1.20", 8123)
    await opens(session, DOMO, tool_name="lights2")
    assert await closed_by_owner_now(session) == []
    # Someone else's closing stays one tool's: the other keeps going there.
    assert await closes(session, str(sam.id), tool_name="lights2")
    assert len(await open_for(session, "heating")) == 1
    assert not await closed_by_the_owner(session, "192.168.1.20", 8123)


# What a build may read of the addresses (to tell Domo which a tool lacks, Task 7): never a
# function that opens, closes or writes one.
_BUILD_READS: Final = frozenset({"open_for", "closed_by_the_owner", "closed_by_owner_now"})
_WRITES_AN_ADDRESS: Final = frozenset({"open_address", "close_address", "OpenAddress"})


def test_a_build_or_a_new_version_never_opens_an_address():
    # Review Focus 4: only an act of Domo's in a turn (``address_open``), or the owner's, opens
    # an address; what builds a tool or makes a new version of it may read the open ones, never
    # open, close or make a row of one. (The behaviour: ``test_madetools_build.py``,
    # ``test_a_build_that_names_an_address_the_tool_lacks_opens_nothing_and_says_which``.)
    madetools = Path(__file__).parents[1] / "src" / "domo_core" / "madetools"
    builders = sorted(madetools.glob("build*.py"))
    assert [path.name for path in builders] == ["build.py", "build_writes.py"]
    for path in builders:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "domo_core.outward"
            ):
                assert node.module == "domo_core.outward.service", (path.name, node.module)
                imported = {alias.name for alias in node.names}
                assert imported <= _BUILD_READS, (path.name, imported - _BUILD_READS)
            named = (
                node.id
                if isinstance(node, ast.Name)
                else node.attr
                if isinstance(node, ast.Attribute)
                else None
            )
            assert named not in _WRITES_AN_ADDRESS, (path.name, named)


# --- who an account serves ------------------------------------------------------------------------


async def account(
    session: AsyncSession, name: str, *, of: Person | None, by: Person, **more
) -> ServiceAccount:
    row = ServiceAccount(
        name=name,
        person_id=None if of is None else of.id,
        ref=f"account.{uuid.uuid4().hex}",
        hosts=[{"host": "192.168.1.20", "port": 8123}],
        place={"kind": "basic"},
        given_by=by.id,
        **more,
    )
    session.add(row)
    await session.flush()
    return row


@pytest.mark.parametrize(
    ("whose", "launch", "given"),
    [
        # A person's account: only for a launch for that very person.
        ("dana", "dana", "dana"),
        ("dana", "house", None),
        ("dana", "guest", None),
        ("dana", "on_his_own", None),
        # The house's: for anyone of the household, not a guest by default, Domo on his own yes.
        ("house", "dana", "house"),
        ("house", "house", "house"),
        ("house", "guest", None),
        ("house", "on_his_own", "house"),
    ],
)
async def test_whom_an_account_serves_in_the_eight_cases(
    session: AsyncSession, whose: str, launch: str, given: str | None
):
    alex, _, dana, paul = await household(session)
    made = await account(session, "lights", of=dana if whose == "dana" else None, by=alex)
    for_person = {"dana": dana.id, "guest": paul.id}.get(launch)
    found = await account_for(
        session,
        name="lights",
        for_person=for_person,
        on_his_own=launch == "on_his_own",
        is_guest=launch == "guest",
    )
    assert (None if found is None else found.id) == (None if given is None else made.id)


async def test_the_owner_opens_a_house_account_to_guests_and_closes_it_to_domo(
    session: AsyncSession,
):
    alex, _, dana, paul = await household(session)
    house = await account(session, "lights", of=None, by=alex, for_guests=True, for_domo=False)
    as_guest = await account_for(
        session, name="lights", for_person=paul.id, on_his_own=False, is_guest=True
    )
    alone = await account_for(
        session, name="lights", for_person=None, on_his_own=True, is_guest=False
    )
    assert as_guest is not None and as_guest.id == house.id and alone is None
    # A person's own of that name comes first, for them; never for the house.
    own = await account(session, "lights", of=dana, by=dana)
    for_dana = await account_for(
        session, name="lights", for_person=dana.id, on_his_own=False, is_guest=False
    )
    assert for_dana is not None and for_dana.id == own.id


async def test_the_accounts_of_someone_who_left_are_frozen_and_taken_back(session: AsyncSession):
    _, _, dana, _ = await household(session)
    own = await account(session, "music", of=dana, by=dana)

    async def for_dana() -> ServiceAccount | None:
        return await account_for(
            session, name="music", for_person=dana.id, on_his_own=False, is_guest=False
        )

    dana.disabled_at = NOW - timedelta(days=1)
    await session.flush()
    assert await for_dana() is None
    dana.disabled_at = None
    await session.flush()
    found = await for_dana()
    assert found is not None and found.id == own.id
    # Removed, none is given any more.
    await remove_account(session, account=own, by=str(dana.id), now=later())
    assert await for_dana() is None


# --- asking for an account ------------------------------------------------------------------------


def form() -> tuple[KeyForm, str]:
    return KeyForm(REFERENCE), "The account lights"


async def test_the_form_is_asked_from_the_row_and_its_bindings_never_change(session: AsyncSession):
    alex, sam, *_ = await household(session)
    gatekeeper = FakeGatekeeper()
    asked = await ask_account(
        session,
        gatekeeper,  # type: ignore[arg-type]
        name="lights",
        person_id=None,
        giver=alex,
        hosts=[("Device.LAN", 8123), ("192.168.1.20", 8123)],
        place=TOKEN,
        by=str(alex.id),
        form=form(),
    )
    row = asked.account
    assert not asked.again and asked.view is not None
    assert row.hosts == [
        {"host": "device.lan", "port": 8123},
        {"host": "192.168.1.20", "port": 8123},
    ]
    assert row.place == {"kind": "header", "name": "authorization", "template": "Bearer {value}"}
    (request,) = gatekeeper.asked
    assert request.ref == row.ref and request.fields == "value"
    place = HeaderLocation(kind="header", name="authorization", template="Bearer {value}")
    assert request.bindings == [
        Binding(host="device.lan", port=8123, location=place),
        Binding(host="192.168.1.20", port=8123, location=place),
    ]
    assert request.purpose == "The account lights" and request.texts is not None
    # Asked again before it is given, with the same description: the same row, a new form.
    again = await ask_account(
        session,
        gatekeeper,  # type: ignore[arg-type]
        name="lights",
        person_id=None,
        giver=alex,
        hosts=[("device.lan", 8123), ("192.168.1.20", 8123)],
        place=place,
        by=DOMO,
        form=form(),
    )
    assert again.again and again.account.id == row.id and len(gatekeeper.asked) == 2
    # Other hosts, another place or another giver: an account of that name is there.
    for hosts, where, giver in (
        ([("device.lan", 8123)], place, alex),
        ([("device.lan", 8123), ("192.168.1.20", 8123)], BasicLocation(kind="basic"), alex),
        ([("device.lan", 8123), ("192.168.1.20", 8123)], place, sam),
    ):
        with pytest.raises(AccountExists):
            await ask_account(
                session,
                gatekeeper,  # type: ignore[arg-type]
                name="lights",
                person_id=None,
                giver=giver,
                hosts=hosts,
                place=where,
                by=DOMO,
                form=form(),
            )
    assert len(gatekeeper.asked) == 2


async def test_an_account_given_is_never_asked_again(session: AsyncSession):
    alex, _, dana, _ = await household(session)
    gatekeeper = FakeGatekeeper()
    basic = BasicLocation(kind="basic")
    arguments = {"name": "music", "hosts": HOSTS, "place": basic, "form": None}
    first = await ask_account(
        session,
        gatekeeper,  # type: ignore[arg-type]
        person_id=dana.id,
        giver=dana,
        by=str(dana.id),
        **arguments,
    )
    assert first.view is None and gatekeeper.asked == []
    gatekeeper.given.add(first.account.ref)
    for by in (str(dana.id), str(alex.id), DOMO):
        with pytest.raises(AccountExists):
            await ask_account(
                session,
                gatekeeper,  # type: ignore[arg-type]
                person_id=dana.id,
                giver=dana,
                by=by,
                **arguments,
            )
    # The house may have its own of that name.
    house = await ask_account(
        session,
        gatekeeper,  # type: ignore[arg-type]
        person_id=None,
        giver=alex,
        by=str(alex.id),
        **arguments,
    )
    assert house.account.id != first.account.id and house.account.ref != first.account.ref


async def test_an_account_the_owner_removed_only_the_owner_asks_again(session: AsyncSession):
    alex, sam, dana, _ = await household(session)
    gatekeeper = FakeGatekeeper()
    arguments = {
        "name": "lights",
        "person_id": None,
        "hosts": HOSTS,
        "place": TOKEN,
        "form": None,
    }
    made = await ask_account(
        session,
        gatekeeper,  # type: ignore[arg-type]
        giver=sam,
        by=str(sam.id),
        **arguments,
    )
    await remove_account(session, account=made.account, by=str(alex.id), now=later())
    assert made.account.removed_by_owner and made.account.removed_by == str(alex.id)
    for by in (DOMO, str(sam.id)):
        with pytest.raises(RemovedByOwner):
            await ask_account(
                session,
                gatekeeper,  # type: ignore[arg-type]
                giver=sam,
                by=by,
                **arguments,
            )
    again = await ask_account(
        session,
        gatekeeper,  # type: ignore[arg-type]
        giver=sam,
        by=str(alex.id),
        **arguments,
    )
    assert again.account.id != made.account.id
    # A person's own account the owner removed: theirs to give again, nobody else's (ruling R10).
    own = {**arguments, "person_id": dana.id, "name": "music"}
    mine = await ask_account(
        session,
        gatekeeper,  # type: ignore[arg-type]
        giver=dana,
        by=str(dana.id),
        **own,
    )
    await remove_account(session, account=mine.account, by=str(alex.id), now=later())
    for by in (DOMO, str(sam.id)):
        with pytest.raises(RemovedByOwner):
            await ask_account(
                session,
                gatekeeper,  # type: ignore[arg-type]
                giver=dana,
                by=by,
                **own,
            )
    await ask_account(
        session,
        gatekeeper,  # type: ignore[arg-type]
        giver=dana,
        by=str(dana.id),
        **own,
    )
    # Removed by whoever gave it, it is anyone's to ask for again.
    await remove_account(session, account=again.account, by=str(sam.id), now=later())
    assert not again.account.removed_by_owner
    await ask_account(
        session,
        gatekeeper,  # type: ignore[arg-type]
        giver=sam,
        by=DOMO,
        **arguments,
    )
