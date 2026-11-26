"""What Domo does with the addresses and the accounts (step 2, spec 13.3 to 13.5, 13.10).

**Addresses.** ``open_address`` opens an address (a host and a port) for one tool, or for a program
run by hand (``tool_name`` ``None``), and ``close_address`` closes it; ``open_for`` gives the live
ones of a tool as the gatekeeper reads them (``domo_common.gatekeeper_api.OpenAddress``), sent
with each of its requests: a closing holds from the next one. Who opens or closes is ``"domo"``
(a turn he acts in on his own) or a person's id (``by``). The owner's closing of a host and port
closes it for every tool and for the programs run by hand at once (ruling R20). While its last
closing, whatever the tool, was the owner's (``OpenAddress.closed_by_owner``, kept on the row as it
was at the closing, so that it outlives a change of owner), it is opened again only when Domo
judged that things changed, that closing weighed (``reopening``: the tool's call, judged, with the
owner's yes when one is asked), or by the owner of now; anyone else's opening is refused
(``ClosedByOwner``). Anyone else's closing is one tool's. Only an act of Domo's in a
turn, or the owner's, opens an address: a build or a new version of a tool never does.

**Accounts.** ``ask_account`` makes the row of an account Domo asks someone for (a person's own,
or the house's: ``person_id`` ``None``), bound to its hosts and its place for good, and asks the
gatekeeper for its one-time form when someone may open it now (``form``). The row is made before
the form is filled: until the vault holds its secret, asking again for that very account (its
giver, hosts and place) opens a new form for it; anything else of that name is refused
(``AccountExists``), as is an account whose secret the vault holds already: an account is never
replaced. ``remove_account`` marks one removed (the vault cannot erase: the core refuses it from
then on); a removal by the owner is kept as his (``ServiceAccount.removed_by_owner``, and when:
``owner_s_removal``), and the same account (its name and whose it is) asked again is refused
(``RemovedByOwner``) unless the caller weighed that removal first (``asking_again``: Domo's
judgement, whoever asks, Task 27). ``account_for`` is
the one rule of who a launch may use an account for:
the person's own account of that name, else the house's; a house account is never given to a guest
unless the owner opened it to guests (``for_guests``), nor to Domo on his own unless he may
(``for_domo``, spec 13.5); a person's account is never given to a launch for the house, for
someone else, or for Domo on his own; and a person who left the household has their accounts
frozen (none of theirs is given), taken back when they return, and deleted with their row.

Nothing here holds a secret: what an account is made of goes from the person's browser into the
vault, and the core keeps only its reference, its hosts and its place.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, cast

from domo_common.gatekeeper_api import (
    Binding,
    InjectionLocation,
    SecretRequestView,
    location_fields,
)
from domo_common.gatekeeper_api import OpenAddress as AddressToReach
from pydantic import TypeAdapter
from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.gatekeeper_client import GatekeeperClient, GatekeeperError
from domo_core.llm.provider_keys import KeyForm
from domo_core.outward.models import DOMO, OpenAddress, ServiceAccount, account_ref

# The place of an account as it is kept (``ServiceAccount.place``) and sent to the vault.
PLACE: Final = TypeAdapter[InjectionLocation](InjectionLocation)


class ClosedByOwner(Exception):
    """The last closing of this address was the owner's, and it was opened again by someone other
    than the owner of now without that closing weighed (``reopening``)."""


class AccountExists(Exception):
    """An account of this name is there already for whom it would be: given (the vault holds its
    secret), or asked of someone else or with other hosts or another place. Nothing replaces it."""


class RemovedByOwner(Exception):
    """The owner removed this account, and it was asked again without that removal weighed."""


# --- who ------------------------------------------------------------------------------------------


def who_of(person: Person | None) -> str:
    """How a row says who opened, closed or removed it: ``"domo"`` for Domo on his own
    (``None``), else the person's id."""
    return DOMO if person is None else str(person.id)


async def is_the_owner(session: AsyncSession, by: str) -> bool:
    """Whether ``by`` (``"domo"`` or a person's id) is the owner now."""
    if by == DOMO:
        return False
    try:
        person_id = uuid.UUID(by)
    except ValueError:
        return False
    role = await session.scalar(select(Person.role).where(Person.id == person_id))
    return role == "owner"


# --- addresses ------------------------------------------------------------------------------------


def _same_address(tool_name: str | None, host: str, port: int) -> ColumnElement[bool]:
    tool = (
        OpenAddress.tool_name.is_(None) if tool_name is None else OpenAddress.tool_name == tool_name
    )
    return and_(tool, OpenAddress.host == host.lower(), OpenAddress.port == port)


async def _live(
    session: AsyncSession, tool_name: str | None, host: str, port: int
) -> OpenAddress | None:
    found = await session.execute(
        select(OpenAddress)
        .where(_same_address(tool_name, host, port), OpenAddress.closed_at.is_(None))
        .with_for_update()
    )
    return found.scalar_one_or_none()


def _same_host_and_port(host: str, port: int) -> ColumnElement[bool]:
    """The rows of ``host``:``port``, whatever tool they were opened for (ruling R20)."""
    return and_(OpenAddress.host == host.lower(), OpenAddress.port == port)


async def owner_s_closing(session: AsyncSession, host: str, port: int) -> datetime | None:
    """When the owner closed ``host``:``port`` (ruling R20), for every tool and for the programs
    run by hand: no row of it is live, and its last closing, whatever the tool, was the owner's
    (his closing closes them all at once; an opening since, his own first, lifts it). ``None``
    when it is not closed by him."""
    live = await session.scalar(
        select(OpenAddress.id)
        .where(_same_host_and_port(host, port), OpenAddress.closed_at.is_(None))
        .limit(1)
    )
    if live is not None:
        return None
    last = (
        await session.execute(
            select(OpenAddress.closed_by_owner, OpenAddress.closed_at)
            .where(_same_host_and_port(host, port), OpenAddress.closed_at.is_not(None))
            # At the same instant, the owner's closing counts.
            .order_by(OpenAddress.closed_at.desc(), OpenAddress.closed_by_owner.desc())
            .limit(1)
        )
    ).first()
    return last[1] if last is not None and last[0] else None


async def closed_by_the_owner(session: AsyncSession, host: str, port: int) -> bool:
    """Whether ``host``:``port`` is closed by the owner (``owner_s_closing``)."""
    return await owner_s_closing(session, host, port) is not None


async def open_address(
    session: AsyncSession,
    *,
    tool_name: str | None,
    host: str,
    port: int,
    on_host: bool,
    by: str,
    now: datetime,
    reopening: bool = False,
) -> OpenAddress:
    """The live address ``host``:``port`` of ``tool_name`` (``None``: programs run by hand), opened
    by ``by`` at ``now`` unless it is open already. One open with the other ``on_host`` is closed
    and opened again as asked (a row never changes back). Raises ``ClosedByOwner`` when the host
    and port are closed by the owner (``closed_by_the_owner``, for whatever tool) and ``by`` is
    not the owner now, unless the caller weighed that closing and opens it again knowingly
    (``reopening``, ruling R24: Domo's judgement, under the owner's rules). Flushed, not
    committed."""
    host = host.lower()
    live = await _live(session, tool_name, host, port)
    if live is not None and live.on_host == on_host:
        return live
    if (
        live is None
        and not reopening
        and not await is_the_owner(session, by)
        and await closed_by_the_owner(session, host, port)
    ):
        raise ClosedByOwner
    if live is not None:
        # Not a closing of anyone's will: the same address, opened again otherwise.
        live.closed_at = now
        live.closed_by = by
        live.closed_by_owner = False
        await session.flush()
    row = OpenAddress(
        tool_name=tool_name, host=host, port=port, on_host=on_host, opened_by=by, opened_at=now
    )
    session.add(row)
    await session.flush()
    return row


async def close_address(
    session: AsyncSession,
    *,
    tool_name: str | None,
    host: str,
    port: int,
    by: str,
    now: datetime,
) -> bool:
    """Close the live address ``host``:``port`` of ``tool_name`` at ``now``, as ``by``'s act;
    whether one was open. The owner's closing (``by`` the owner now: kept so on the rows) closes
    every live row of that host and port, whatever tool it was opened for (ruling R20); anyone
    else's, the one of ``tool_name`` only. Flushed, not committed."""
    by_the_owner = await is_the_owner(session, by)
    if by_the_owner:
        found = await session.scalars(
            select(OpenAddress)
            .where(_same_host_and_port(host, port), OpenAddress.closed_at.is_(None))
            .with_for_update()
        )
        rows = list(found.all())
    else:
        live = await _live(session, tool_name, host, port)
        rows = [] if live is None else [live]
    for row in rows:
        row.closed_at = now
        row.closed_by = by
        row.closed_by_owner = by_the_owner
    await session.flush()
    return bool(rows)


async def live_addresses(session: AsyncSession, tool_name: str | None = None) -> list[OpenAddress]:
    """The live addresses, of ``tool_name`` only when one is given (all of them otherwise), in
    the order they were opened."""
    query = select(OpenAddress).where(OpenAddress.closed_at.is_(None))
    if tool_name is not None:
        query = query.where(OpenAddress.tool_name == tool_name)
    found = await session.scalars(query.order_by(OpenAddress.opened_at, OpenAddress.host))
    return list(found.all())


async def open_for(session: AsyncSession, tool_name: str | None) -> list[AddressToReach]:
    """The live addresses of ``tool_name`` (``None``: a program run by hand), as the gatekeeper
    reads them with a request (``OpenAddress`` of the gatekeeper's API)."""
    tool = (
        OpenAddress.tool_name.is_(None) if tool_name is None else OpenAddress.tool_name == tool_name
    )
    found = await session.scalars(
        select(OpenAddress)
        .where(tool, OpenAddress.closed_at.is_(None))
        .order_by(OpenAddress.host, OpenAddress.port)
    )
    return [AddressToReach(host=row.host, port=row.port, on_host=row.on_host) for row in found]


async def closed_by_owner_now(
    session: AsyncSession, tool_name: str | None = None
) -> list[OpenAddress]:
    """The hosts and ports closed by the owner now (``closed_by_the_owner``: for every tool): the
    last closed row of each; with ``tool_name``, only those that tool had, its own last row."""
    rows = (
        await session.scalars(
            select(OpenAddress)
            .where(OpenAddress.closed_at.is_not(None))
            .order_by(OpenAddress.closed_at.desc(), OpenAddress.closed_by_owner.desc())
        )
    ).all()
    live = {(row.host, row.port) for row in await live_addresses(session)}
    # The last closing of each host and port not open again, whatever the tool.
    last: dict[tuple[str, int], OpenAddress] = {}
    for row in rows:
        key = (row.host, row.port)
        if key not in live and key not in last:
            last[key] = row
    found: list[OpenAddress] = []
    for key, row in last.items():
        if not row.closed_by_owner:
            continue
        if tool_name is not None:
            own = next(
                (r for r in rows if (r.host, r.port) == key and r.tool_name == tool_name), None
            )
            if own is None:
                continue
            row = own
        found.append(row)
    return found


# --- accounts -------------------------------------------------------------------------------------


def _whose(person_id: uuid.UUID | None) -> ColumnElement[bool]:
    return (
        ServiceAccount.person_id.is_(None)
        if person_id is None
        else ServiceAccount.person_id == person_id
    )


async def live_account(
    session: AsyncSession, *, name: str, person_id: uuid.UUID | None
) -> ServiceAccount | None:
    """The live account ``name`` of ``person_id`` (``None``: the house's), if there is one."""
    found = await session.execute(
        select(ServiceAccount).where(
            ServiceAccount.name == name, _whose(person_id), ServiceAccount.removed_at.is_(None)
        )
    )
    return found.scalar_one_or_none()


async def owner_s_removal(
    session: AsyncSession, *, name: str, person_id: uuid.UUID | None
) -> datetime | None:
    """When the owner removed the account ``name`` of ``person_id`` (``None``: the house's): its
    last removal was his, and none of that name is live since. ``None`` otherwise."""
    if await live_account(session, name=name, person_id=person_id) is not None:
        return None
    last = (
        await session.execute(
            select(ServiceAccount.removed_by_owner, ServiceAccount.removed_at)
            .where(
                ServiceAccount.name == name,
                _whose(person_id),
                ServiceAccount.removed_at.is_not(None),
            )
            .order_by(ServiceAccount.removed_at.desc())
            .limit(1)
        )
    ).first()
    return last[1] if last is not None and last[0] else None


async def removed_by_the_owner(
    session: AsyncSession, *, name: str, person_id: uuid.UUID | None
) -> bool:
    """Whether the account ``name`` of ``person_id`` is removed by the owner
    (``owner_s_removal``)."""
    return await owner_s_removal(session, name=name, person_id=person_id) is not None


def place_of(account: ServiceAccount) -> InjectionLocation:
    """Where ``account`` is placed in a request, as it was kept."""
    return PLACE.validate_python(account.place)


def bindings_of(account: ServiceAccount) -> list[Binding]:
    """The destinations of ``account`` for the vault: each of its hosts, at its place."""
    place = place_of(account)
    hosts = cast(list[dict[str, object]], account.hosts)
    return [Binding(host=str(h["host"]), port=cast(int, h["port"]), location=place) for h in hosts]


def kept_hosts(hosts: Sequence[tuple[str, int]]) -> list[dict[str, object]]:
    """``hosts`` as an account keeps them: ``{"host", "port"}``, the host in lower case."""
    return [{"host": host.lower(), "port": port} for host, port in hosts]


@dataclass(frozen=True)
class AskedAccount:
    """What came of asking for an account: its row, the form the gatekeeper opened for it
    (``None`` when none was asked for), and whether it was asked before (``again``: its form not
    filled yet)."""

    account: ServiceAccount
    view: SecretRequestView | None
    again: bool


async def ask_account(
    session: AsyncSession,
    gatekeeper: GatekeeperClient,
    *,
    name: str,
    person_id: uuid.UUID | None,
    giver: Person,
    hosts: Sequence[tuple[str, int]],
    place: InjectionLocation,
    by: str,
    form: tuple[KeyForm, str] | None,
    asking_again: bool = False,
) -> AskedAccount:
    """The account ``name`` of ``person_id`` (``None``: the house's) that ``giver`` gives, bound to
    ``hosts`` at ``place``, asked by ``by`` (``"domo"`` or a person's id); with ``form`` (the
    words of the giver's form and its purpose), the one-time form the gatekeeper opens for it.

    Raises ``RemovedByOwner`` when the owner removed it last, unless the caller weighed that
    removal and asks for it again knowingly (``asking_again``: the call was judged with it, or
    agreed); and ``AccountExists`` when an account of that name is there for ``person_id``
    already, unless it is this very one (its giver, hosts and place) and the vault does not hold
    its secret yet: its form is then asked for again. ``GatekeeperError`` when the gatekeeper does
    not answer as it should. Flushed, not committed."""
    kept = kept_hosts(hosts)
    kept_place = cast(dict[str, object], PLACE.dump_python(place, mode="json"))
    live = await live_account(session, name=name, person_id=person_id)
    again = live is not None
    if live is None:
        if not asking_again and await removed_by_the_owner(session, name=name, person_id=person_id):
            raise RemovedByOwner
        account = ServiceAccount(
            name=name,
            person_id=person_id,
            ref=account_ref(uuid.uuid4()),
            hosts=kept,
            place=kept_place,
            given_by=giver.id,
        )
        session.add(account)
        await session.flush()
    else:
        if live.given_by != giver.id or live.hosts != kept or live.place != kept_place:
            raise AccountExists
        if live.ref in {secret.ref for secret in await gatekeeper.list_secrets()}:
            raise AccountExists
        account = live
    view = None
    if form is not None:
        words, purpose = form
        view = await gatekeeper.create_secret_request(
            words.request_for(
                account.ref, bindings_of(account), purpose, fields=location_fields(place)
            )
        )
    return AskedAccount(account, view, again)


async def given_refs(gatekeeper: GatekeeperClient) -> frozenset[str] | None:
    """The references of the accounts whose secret the vault holds; ``None`` when the gatekeeper
    does not say (it is then unknown which are given)."""
    try:
        secrets = await gatekeeper.list_secrets()
    except GatekeeperError:
        return None
    return frozenset(secret.ref for secret in secrets)


async def remove_account(
    session: AsyncSession, *, account: ServiceAccount, by: str, now: datetime
) -> None:
    """Mark ``account`` removed at ``now``, as ``by``'s act (the owner's, when ``by`` is the owner
    now: kept so on the row). Flushed, not committed."""
    account.removed_at = now
    account.removed_by = by
    account.removed_by_owner = await is_the_owner(session, by)
    await session.flush()


def _in_the_household() -> ColumnElement[bool]:
    """The person of an account is in the household now (``accounts.sessions.is_active``)."""
    return ServiceAccount.person_id.in_(
        select(Person.id).where(
            Person.disabled_at.is_(None),
            or_(Person.expires_at.is_(None), Person.expires_at > func.now()),
        )
    )


async def account_for(
    session: AsyncSession,
    *,
    name: str,
    for_person: uuid.UUID | None,
    on_his_own: bool,
    is_guest: bool,
) -> ServiceAccount | None:
    """The account ``name`` a launch may use (see the module docstring): for ``for_person``
    (``None``: a launch for the house), Domo acting on his own or not (``on_his_own``), the
    person being a guest or not (``is_guest``). ``None`` when there is none it may use."""
    if for_person is not None and not on_his_own:
        found = await session.execute(
            select(ServiceAccount).where(
                ServiceAccount.name == name,
                ServiceAccount.person_id == for_person,
                ServiceAccount.removed_at.is_(None),
                _in_the_household(),
            )
        )
        own = found.scalar_one_or_none()
        if own is not None:
            return own
    house = await live_account(session, name=name, person_id=None)
    if house is None:
        return None
    if is_guest and not house.for_guests:
        return None
    if on_his_own and not house.for_domo:
        return None
    return house


async def was_removed(
    session: AsyncSession, *, name: str, for_person: uuid.UUID | None, on_his_own: bool
) -> bool:
    """Whether an account ``name`` a launch could have used was removed (the one of
    ``for_person``, when the launch is theirs and not Domo's on his own, or the house's): what a
    program is told when ``account_for`` gives it none, instead of "not yours". Never when one of
    that name is live there: it is then closed to this launch, which is "not yours"."""
    whose = ServiceAccount.person_id.is_(None)
    if for_person is not None and not on_his_own:
        whose = or_(whose, ServiceAccount.person_id == for_person)
    live = await session.scalar(
        select(ServiceAccount.id)
        .where(ServiceAccount.name == name, whose, ServiceAccount.removed_at.is_(None))
        .limit(1)
    )
    if live is not None:
        return False
    found = await session.scalar(
        select(ServiceAccount.id)
        .where(ServiceAccount.name == name, whose, ServiceAccount.removed_at.is_not(None))
        .limit(1)
    )
    return found is not None
