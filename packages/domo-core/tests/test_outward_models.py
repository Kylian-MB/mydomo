"""The tables of going out (step 2, spec 13.3, 13.4): the open addresses and the accounts, their
migration (``0054``) and the zone of an address read on its literal (``outward.literal``).

The rows are tried on the shared rolled-back ``session``; the migration on a scratch database."""

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from alembic import command
from domo_core.accounts.models import Person
from domo_core.migrate import alembic_config
from domo_core.outward.literal import is_a_host, literal_zone, open_risks
from domo_core.outward.models import (
    DOMO,
    OpenAddress,
    ServiceAccount,
    account_ref,
)
from domo_core.policy.catalog import NO_RISK, Risks
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from test_migrations import schema_of

WHEN = datetime(2026, 11, 22, 10, 0, tzinfo=UTC)
HOSTS = [{"host": "192.168.1.20", "port": 8123}]
PLACE = {"kind": "header", "name": "authorization", "template": "Bearer {value}"}


async def _person(session: AsyncSession, role: str = "owner") -> Person:
    person = Person(display_name="Alex" if role == "owner" else "Dana", role=role)
    session.add(person)
    await session.flush()
    return person


def _account(name: str, *, by: Person, person: Person | None = None) -> ServiceAccount:
    return ServiceAccount(
        name=name,
        person_id=None if person is None else person.id,
        ref=account_ref(uuid.uuid4()),
        hosts=HOSTS,
        place=PLACE,
        given_by=by.id,
    )


async def _refused(session: AsyncSession, row: object) -> None:
    """``row`` is refused by the database, and the session stays usable."""
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(row)
            await session.flush()


# --- the migration -------------------------------------------------------------------------------


async def test_0054_makes_the_two_tables_and_going_back_down_leaves_none(
    scratch_database_url, list_tables
):
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0053")
    before = await schema_of(scratch_database_url)
    tables_before = await list_tables(scratch_database_url)
    assert not {"open_addresses", "service_accounts"} & tables_before
    await asyncio.to_thread(command.upgrade, config, "0054")
    assert await list_tables(scratch_database_url) == tables_before | {
        "open_addresses",
        "service_accounts",
    }
    after = await schema_of(scratch_database_url)
    indexes = {row[1]: row[2] for row in after["indexes"]}
    for index, columns, where in (
        ("uq_open_addresses_live", "(tool_name, host, port)", "(closed_at IS NULL)"),
        ("uq_service_accounts_live", "(person_id, name)", "(removed_at IS NULL)"),
    ):
        assert f"UNIQUE INDEX {index}" in indexes[index]
        assert columns in indexes[index] and "NULLS NOT DISTINCT" in indexes[index]
        assert indexes[index].endswith(f"WHERE {where}")
    await asyncio.to_thread(command.downgrade, config, "0053")
    assert await list_tables(scratch_database_url) == tables_before
    assert await schema_of(scratch_database_url) == before


# --- the open addresses --------------------------------------------------------------------------


async def test_one_live_address_per_tool_host_and_port(session: AsyncSession):
    first = OpenAddress(tool_name="lights", host="192.168.1.20", port=8123, opened_by=DOMO)
    session.add(first)
    await session.flush()
    assert (first.on_host, first.closed_at, first.closed_by) == (False, None, None)
    await _refused(
        session, OpenAddress(tool_name="lights", host="192.168.1.20", port=8123, opened_by=DOMO)
    )
    # Another tool, another port, or a program run by hand (no tool) is another address.
    session.add_all(
        [
            OpenAddress(tool_name="heating", host="192.168.1.20", port=8123, opened_by=DOMO),
            OpenAddress(tool_name="lights", host="192.168.1.20", port=8124, opened_by=DOMO),
            OpenAddress(tool_name=None, host="192.168.1.20", port=8123, opened_by=DOMO),
        ]
    )
    await session.flush()
    # No tool counts as a value of its own: two live rows without a tool are refused too.
    await _refused(
        session, OpenAddress(tool_name=None, host="192.168.1.20", port=8123, opened_by=DOMO)
    )
    # Once closed, the address may be opened again: a new row, the closing kept.
    owner = await _person(session)
    first.closed_at = first.opened_at
    first.closed_by = str(owner.id)
    await session.flush()
    session.add(
        OpenAddress(tool_name="lights", host="192.168.1.20", port=8123, opened_by=str(owner.id))
    )
    await session.flush()
    rows = await session.scalars(
        select(OpenAddress).where(OpenAddress.tool_name == "lights", OpenAddress.port == 8123)
    )
    assert sorted(row.closed_by is None for row in rows) == [False, True]


@pytest.mark.parametrize(
    "fields",
    [
        {"host": "Device.Lan"},
        {"host": ""},
        {"port": 0},
        {"port": 65536},
        {"tool_name": "Lights"},
        {"opened_by": "someone"},
        {"closed_by": DOMO},
        {"closed_by": "nobody", "closed_at": WHEN},
        {"closed_by_owner": True},
    ],
)
async def test_an_address_the_database_refuses(session: AsyncSession, fields):
    row = {"tool_name": "lights", "host": "device.lan", "port": 80, "opened_by": DOMO, **fields}
    await _refused(session, OpenAddress(**row))


# --- the accounts --------------------------------------------------------------------------------


async def test_one_live_account_per_person_or_house_and_name(session: AsyncSession):
    owner = await _person(session)
    dana = await _person(session, "adult")
    house = _account("lights", by=owner)
    session.add(house)
    await session.flush()
    assert (house.for_guests, house.for_domo, house.removed_at) == (False, True, None)
    # A second house account of that name is refused; a person's own of that name is not.
    await _refused(session, _account("lights", by=owner))
    session.add_all([_account("lights", by=dana, person=dana), _account("heating", by=owner)])
    await session.flush()
    await _refused(session, _account("lights", by=dana, person=dana))
    # Once removed, the name is free again; the removed row stays.
    house.removed_at = house.given_at
    house.removed_by = str(owner.id)
    await session.flush()
    session.add(_account("lights", by=owner))
    await session.flush()
    names = await session.scalars(
        select(ServiceAccount.removed_by).where(
            ServiceAccount.person_id.is_(None), ServiceAccount.name == "lights"
        )
    )
    assert sorted(str(found) for found in names) == sorted([str(owner.id), "None"])


async def test_a_reference_is_unique_and_of_its_own_namespace(session: AsyncSession):
    owner = await _person(session)
    first = _account("lights", by=owner)
    session.add(first)
    await session.flush()
    again = _account("heating", by=owner)
    again.ref = first.ref
    await _refused(session, again)
    assert account_ref(uuid.UUID(int=255)) == "account." + "0" * 30 + "ff"


@pytest.mark.parametrize(
    "fields",
    [
        {"name": "a"},
        {"name": "Lights"},
        {"name": "1lights"},
        {"name": "l" * 41},
        {"ref": "openai"},
        {"ref": "account.XYZ"},
        {"hosts": []},
        {"hosts": {"host": "device.lan"}},
        {"place": {"kind": "cookie"}},
        {"place": "header"},
        {"place": {"name": "authorization"}},
        {"removed_by": DOMO},
        {"removed_by_owner": True},
        {"removed_by": "someone", "removed_at": WHEN},
    ],
)
async def test_an_account_the_database_refuses(session: AsyncSession, fields):
    owner = await _person(session)
    row = _account("lights", by=owner)
    for name, value in fields.items():
        setattr(row, name, value)
    await _refused(session, row)


async def test_a_person_s_accounts_go_with_their_row_and_the_house_s_stay(session: AsyncSession):
    # Spec 5.8: deleting a person deletes their own accounts; a house account they gave stays,
    # forgetting who gave it.
    owner = await _person(session)
    dana = await _person(session, "adult")
    session.add_all(
        [
            _account("mail", by=dana, person=dana),
            _account("heating", by=dana),
            _account("lights", by=owner),
        ]
    )
    await session.flush()
    owner_id = owner.id
    await session.delete(dana)
    await session.flush()
    session.expire_all()
    left = await session.execute(
        select(ServiceAccount.name, ServiceAccount.person_id, ServiceAccount.given_by).order_by(
            ServiceAccount.name
        )
    )
    assert [tuple(row) for row in left] == [("heating", None, None), ("lights", None, owner_id)]


# --- the zone of an address, on its literal -----------------------------------------------------


@pytest.mark.parametrize(
    "host",
    [
        "10.0.0.5",
        "172.16.0.1",
        "172.31.255.254",
        "192.168.1.20",
        "100.64.0.1",
        "127.0.0.1",
        "169.254.10.1",
        "fd00::1",
        "[fd12:3456::1]",
        "::1",
        "fe80::1",
        "::ffff:192.168.1.20",
        "printer",
        "printer.",
        "device.local",
        "Device.LAN",
        "box.home.arpa",
        # Numbers a resolver reads as an IPv4 address, when that address is the house's.
        "3232235796",
        "0xc0a80114",
        "0300.0250.1.20",
        "192.168.276",
        "10.1",
        "127.1",
        "0x7f.1",
    ],
)
def test_an_address_of_the_house(host):
    assert literal_zone(host) == "house"
    assert open_risks(host) == NO_RISK


@pytest.mark.parametrize(
    "host",
    [
        "8.8.8.8",
        "172.32.0.1",
        "100.128.0.1",
        "0.0.0.1",
        "2001:db8::1",
        "::ffff:8.8.8.8",
        "example.com",
        "site.example",
        "local.example.com",
        "lan.example",
        "home.arpa.example",
        "",
        # A single label of digits, or a hexadecimal or octal number, is read by a resolver as an
        # IPv4 address: the internet unless that address is the house's.
        "134744072",
        "0x8080808",
        "8123",
        "010.0.0.1",
        "8.8",
        "0x8.0x8.0x8.0x8",
    ],
)
def test_an_address_of_the_internet(host):
    assert literal_zone(host) == "internet"
    assert open_risks(host) == Risks(outward=True)


@pytest.mark.parametrize(
    ("host", "valid"),
    [
        ("192.168.1.20", True),
        ("::1", True),
        ("[fd00::1]", True),
        ("lamps.home.arpa", True),
        ("lamps.home.arpa.", True),
        ("host.docker.internal", True),
        ("my_device-2", True),
        ("printer", True),
        ("", False),
        ("a..b", False),
        ("-lamps", False),
        ("tell!domo*to,obey", False),
        ("ignore~all", False),
        ("Lamps", False),
        ("bücher.example", False),
        ("x" * 64 + ".example", False),
    ],
)
def test_a_host_kept_is_a_host_name_or_an_address(host, valid):
    # I3: what an audit line or a tool's figures keep where a host goes.
    assert is_a_host(host) is valid
