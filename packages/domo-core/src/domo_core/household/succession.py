"""The owner's list of successors: who takes over the owner's role if the owner can no longer, in
the order the owner chose.

The list
--------

There is one list, the household's: whoever is the owner keeps it, and it passes to the next owner
by itself (``Successor`` has no owner column). A place on it is a person of the household (a child
is accepted: the owner's context says so, since they will have to be an adult the day it comes) or
someone outside the household, with a name and what the owner says of them ("my brother"), and no
coordinates for now. For each the owner may note the way the door is opened the day it comes
(``AccessWay``): a personal access that only claims the role, or a paper code kept with the will.
It is only noted; building it is for the succession itself.

Nothing about the list ever leaves the owner and Domo talking with the owner: the tools
(``tools.successors``) and the owner's context are the owner's alone
(``catalog.OWNER_ONLY_ACTIONS``).

What the owner and Domo read (``SuccessorShown``)
-----------------------------------------------------

Each place comes with its ``rank`` (1 is first *among the living*: someone who died has none, and
the next one comes first), the name, whether the person is in the household now and their role if
so, the day they left the household if they did (``household.service.left_on``), the day they
died if they did, the relation and the way noted. Leaving the household does not take anyone off
the list: it is told, and the owner decides. The name of a person of the household is the one they
have now.

Changing the list
-----------------

``replace_successors`` deletes the rows and writes them again in the order given, in the caller's
transaction, and gives the list as it was and as it is. It holds an advisory lock (two changes at
once would meet on the ranks) and locks the rows it reads, so that a death noted at the same
moment is not lost. A place that matches one of the old list keeps its ``deceased_at``: the same
person, or an outsider whose name reads the same (``text.name_key``). Whoever asks for the change
sees a list that the table could keep (``ValueError`` otherwise): at most ``MAX_SUCCESSORS``
places, each a person or a name, no person twice, no two names that read the same (a death is told
by the name, so names must tell the places apart), no relation or name that is not one plain line,
and never the owner (they are not their own successor). For the same reason, handing the owner's
role over takes the new owner off the list, in the same transaction, and closes the gap in the
ranks (``household.service.transfer_ownership``); the old owner is not added.

``entries_from`` reads what the owner gave through the tool (a list of ``{"person": ...}`` or
``{"name": ..., "relation": ...}``, each with an optional ``"access"``), finds the people by their
display name and refuses what makes no sense (``InvalidArguments``). A person who left the
household and is still on the list may be named again, so that a change to the list does not drop
them. ``preview_successors`` gives the list such a change would make without making it, for the
card that shows it before and after.

``note_death`` marks a place as deceased. The name is a member's display name or an outsider's
name; it is matched the way names read the same, and a name that fits no place is ``not_found``,
one that fits several is ``ambiguous``, and a death noted twice is ``invalid``. ``undo_death``
undoes it, for a death noted by mistake: the place is where it was and counts again, with the same
ways to name it, and a death that is not noted is ``invalid``.

None of these functions commits: the caller owns the transaction (and writes the audit entry
last).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Final, Literal, cast, get_args
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Account, Person
from domo_core.accounts.sessions import is_active
from domo_core.household.models import Successor
from domo_core.household.service import SUCCESSORS_LOCK_KEY, left_on, resolve_person
from domo_core.llm.types import JsonObject
from domo_core.policy.roles import Role, as_role
from domo_core.text import InvalidDisplayName, display_name_value, is_plain_line, name_key
from domo_core.texts.view import Texts
from domo_core.tools.context import InvalidArguments

# How the door is opened the day it comes: a personal access that only claims the role, or a
# paper code kept with the will. The table says the same.
AccessWay = Literal["personal_access", "paper_code"]
ACCESS_WAYS: Final[tuple[AccessWay, ...]] = get_args(AccessWay)

# The most places on the list.
MAX_SUCCESSORS: Final = 10
# The longest relation ("my brother"), in characters (the table says the same).
MAX_RELATION_LENGTH: Final = 200

_PLACE_KEYS: Final = frozenset({"relation", "access"})


@dataclass(frozen=True)
class SuccessorEntry:
    """What the owner gives for one place, once checked: a person of the household
    (``person_id``) or an outsider's ``name``, never both, with what the owner says of them and
    the way the door is opened."""

    person_id: UUID | None
    name: str | None
    relation: str | None
    access_way: AccessWay | None


@dataclass(frozen=True)
class SuccessorShown:
    """A place on the list as the owner and Domo read it (see the module docstring).

    ``rank`` is ``None`` once the successor died. ``role`` is the role of a person who is in the
    household, ``left_on`` the day (``YYYY-MM-DD``, UTC) a person of the household left it, and
    ``deceased_on`` the day the death was noted."""

    rank: int | None
    name: str
    in_household: bool
    role: Role | None
    left_on: str | None
    deceased_on: str | None
    relation: str | None
    access_way: AccessWay | None

    def as_json(self) -> JsonObject:
        """The place as the model reads it (and the owner's context holds it)."""
        return {
            "rank": self.rank,
            "name": self.name,
            "in_household": self.in_household,
            "role": self.role,
            "left_on": self.left_on,
            "deceased_on": self.deceased_on,
            "relation": self.relation,
            "access_way": self.access_way,
        }


# --- reading the list ----------------------------------------------------------------------------


async def _rows(session: AsyncSession, *, lock: bool = False) -> list[Successor]:
    """The places of the list in their order, as the database holds them now (locked, with
    ``lock``). A place of a record merged into another is not one (``people.merge``: the kept
    record holds the better of the two places); it is back if they are split."""
    merged = select(Person.id).where(Person.merged_into_id.is_not(None))
    query = (
        select(Successor)
        .where(Successor.person_id.is_(None) | Successor.person_id.not_in(merged))
        .order_by(Successor.rank)
        .execution_options(populate_existing=True)
    )
    result = await session.execute(query.with_for_update() if lock else query)
    return list(result.scalars())


async def _persons_of(session: AsyncSession, person_ids: Sequence[UUID]) -> dict[UUID, Person]:
    """The people with these identifiers (whether or not they are in the household)."""
    if not person_ids:
        return {}
    found = await session.execute(
        select(Person)
        .where(Person.id.in_(set(person_ids)))
        .execution_options(populate_existing=True)
    )
    return {person.id: person for person in found.scalars()}


def _person_ids(rows: Sequence[Successor]) -> list[UUID]:
    return [row.person_id for row in rows if row.person_id is not None]


def _label(row: Successor, persons: Mapping[UUID, Person]) -> str:
    """The name of a place: the display name a person has now, or the outsider's name."""
    if row.person_id is not None:
        return persons[row.person_id].display_name
    return row.name or ""


def _access_way_of(value: str | None) -> AccessWay | None:
    """The stored way, which the table keeps to ``ACCESS_WAYS`` (``ValueError`` otherwise)."""
    if value is None:
        return None
    for way in ACCESS_WAYS:
        if way == value:
            return way
    raise ValueError(f"not a way to open the door: {value!r}")


def _day(moment: datetime) -> str:
    return moment.astimezone(UTC).date().isoformat()


def _shown_of(
    rows: Sequence[Successor], persons: Mapping[UUID, Person], now: datetime
) -> list[SuccessorShown]:
    """The places as they are read at ``now``: ranks count the living only, in the order of
    ``rows``."""
    shown: list[SuccessorShown] = []
    alive = 0
    for row in rows:
        rank = None
        if row.deceased_at is None:
            alive += 1
            rank = alive
        person = persons[row.person_id] if row.person_id is not None else None
        in_household = person is not None and is_active(person, now)
        shown.append(
            SuccessorShown(
                rank=rank,
                name=_label(row, persons),
                in_household=in_household,
                role=None if person is None or not in_household else as_role(person.role),
                left_on=None if person is None or in_household else left_on(person).isoformat(),
                deceased_on=None if row.deceased_at is None else _day(row.deceased_at),
                relation=row.relation,
                access_way=_access_way_of(row.access_way),
            )
        )
    return shown


async def list_successors(session: AsyncSession, now: datetime) -> list[SuccessorShown]:
    """The list as the owner reads it at ``now``, in its order. Reads only."""
    rows = await _rows(session)
    return _shown_of(rows, await _persons_of(session, _person_ids(rows)), now)


# --- checking and writing the list ---------------------------------------------------------------


async def _checked(session: AsyncSession, entries: Sequence[SuccessorEntry]) -> dict[UUID, Person]:
    """The people of ``entries`` by identifier, if the list is one the table could keep:
    ``ValueError`` otherwise (see the module docstring). The messages never repeat a name."""
    if len(entries) > MAX_SUCCESSORS:
        raise ValueError(f"the list has {MAX_SUCCESSORS} successors at most")
    persons = await _persons_of(session, [e.person_id for e in entries if e.person_id is not None])
    people: set[UUID] = set()
    names: set[str] = set()
    for entry in entries:
        if (entry.person_id is None) == (entry.name is None):
            raise ValueError("a successor is a person of the household or a name, not both")
        if entry.access_way is not None and entry.access_way not in ACCESS_WAYS:
            raise ValueError("the way to open the door is a personal access or a paper code")
        relation = entry.relation
        if relation is not None and not (
            0 < len(relation) <= MAX_RELATION_LENGTH and is_plain_line(relation)
        ):
            raise ValueError(
                f"a relation is one plain line of 1 to {MAX_RELATION_LENGTH} characters"
            )
        if entry.person_id is not None:
            person = persons.get(entry.person_id)
            if person is None:
                raise ValueError("nobody has this identifier")
            if person.role == "owner":
                raise ValueError("the owner is not their own successor")
            if entry.person_id in people:
                raise ValueError("a person is named twice")
            people.add(entry.person_id)
            label = person.display_name
        else:
            label = display_name_value(entry.name)  # (a ValueError: InvalidDisplayName)
        if name_key(label) in names:
            raise ValueError("two successors read the same name: names must tell them apart")
        names.add(name_key(label))
    return persons


def _rows_for(entries: Sequence[SuccessorEntry], old: Sequence[Successor]) -> list[Successor]:
    """The places that ``entries`` make, in order, not yet in any session. A place that matches
    one of ``old`` keeps its ``deceased_at``: the same person, or an outsider with a name that
    reads the same."""
    died_person = {r.person_id: r.deceased_at for r in old if r.person_id is not None}
    died_name = {name_key(r.name): r.deceased_at for r in old if r.name is not None}
    rows: list[Successor] = []
    for rank, entry in enumerate(entries, start=1):
        name = None if entry.name is None else display_name_value(entry.name)
        deceased = None
        if entry.person_id is not None:
            deceased = died_person.get(entry.person_id)
        elif name is not None:
            deceased = died_name.get(name_key(name))
        rows.append(
            Successor(
                rank=rank,
                person_id=entry.person_id,
                name=name,
                relation=entry.relation,
                access_way=entry.access_way,
                deceased_at=deceased,
            )
        )
    return rows


async def replace_successors(
    session: AsyncSession, entries: Sequence[SuccessorEntry], now: datetime
) -> tuple[list[SuccessorShown], list[SuccessorShown]]:
    """Make ``entries`` the list, in their order: the list as it was and as it is now (read at
    ``now``). ``ValueError`` when the list is not one the table could keep (see the module
    docstring); nothing is changed then. Flushed, not committed."""
    await session.execute(select(func.pg_advisory_xact_lock(SUCCESSORS_LOCK_KEY)))
    old = await _rows(session, lock=True)
    persons = await _checked(session, entries)
    before = _shown_of(old, await _persons_of(session, _person_ids(old)), now)
    new = _rows_for(entries, old)
    await session.execute(delete(Successor).execution_options(synchronize_session="fetch"))
    session.add_all(new)
    await session.flush()
    return before, _shown_of(new, persons, now)


async def preview_successors(
    session: AsyncSession, entries: Sequence[SuccessorEntry], now: datetime
) -> list[SuccessorShown]:
    """The list that ``replace_successors`` would make from ``entries``, read at ``now``, without
    making it. ``ValueError`` as ``replace_successors``. Reads only."""
    persons = await _checked(session, entries)
    return _shown_of(_rows_for(entries, await _rows(session)), persons, now)


async def _named_place(
    session: AsyncSession, who: str, *, lock: bool = True
) -> tuple[Successor, list[Successor], dict[UUID, Person]]:
    """The place of the list that ``who`` names (a member's display name, or an outsider's name,
    matched the way names read the same), with the places and the people of the list, read as the
    database holds them now: all locked, under the advisory lock of the list, unless ``lock`` is
    false (to read only). ``InvalidArguments``: ``not_found`` when no place has this name,
    ``ambiguous`` when several do."""
    if lock:
        await session.execute(select(func.pg_advisory_xact_lock(SUCCESSORS_LOCK_KEY)))
    rows = await _rows(session, lock=lock)
    persons = await _persons_of(session, _person_ids(rows))
    key = name_key(who)
    named = [row for row in rows if name_key(_label(row, persons)) == key]
    if not named:
        raise InvalidArguments("not_found", "nobody on the list has this name")
    if len(named) > 1:
        raise InvalidArguments(
            "ambiguous", "several successors have this name: ask which one is meant"
        )
    return named[0], rows, persons


async def note_death(session: AsyncSession, who: str, now: datetime) -> SuccessorShown:
    """Note that the successor named ``who`` died, at ``now``: the place as it reads afterwards (no
    rank: the next one comes first). ``InvalidArguments``: ``not_found`` when no place has this
    name, ``ambiguous`` when several do, ``invalid`` when the death is already noted. Flushed, not
    committed."""
    row, rows, persons = await _named_place(session, who)
    if row.deceased_at is not None:
        raise InvalidArguments("invalid", "this death is already noted")
    row.deceased_at = now
    await session.flush()
    return _shown_of(rows, persons, now)[rows.index(row)]


async def _noted_place(
    session: AsyncSession, who: str, *, lock: bool
) -> tuple[Successor, list[Successor], dict[UUID, Person]]:
    """The place that ``who`` names, whose death is noted (``_named_place``, locked when
    ``lock``). ``InvalidArguments``: ``not_found`` and ``ambiguous`` for the name, ``invalid``
    when this death is not noted."""
    row, rows, persons = await _named_place(session, who, lock=lock)
    if row.deceased_at is None:
        raise InvalidArguments("invalid", "this death is not noted")
    return row, rows, persons


async def noted_death(session: AsyncSession, who: str, now: datetime) -> SuccessorShown:
    """The place of the successor named ``who`` whose death is noted, as it reads at ``now``
    (no rank). It reads only: what ``undo_death`` checks first, so that undoing a death that was
    never noted is refused before anything is asked (spec 5.5.3). ``InvalidArguments``:
    ``not_found`` when no place has this name, ``ambiguous`` when several do, ``invalid`` when
    this death is not noted."""
    row, rows, persons = await _noted_place(session, who, lock=False)
    return _shown_of(rows, persons, now)[rows.index(row)]


async def undo_death(session: AsyncSession, who: str, now: datetime) -> SuccessorShown:
    """Undo the death noted for the successor named ``who`` (a slip of the tongue): the place is
    where it was and counts again, as it reads afterwards at ``now``. ``InvalidArguments`` as
    ``noted_death``: ``not_found`` when no place has this name, ``ambiguous`` when several do,
    ``invalid`` when this death is not noted. Flushed, not committed."""
    row, rows, persons = await _noted_place(session, who, lock=True)
    row.deceased_at = None
    await session.flush()
    return _shown_of(rows, persons, now)[rows.index(row)]


# --- what the owner gives ------------------------------------------------------------------------


def _relation_from(value: object) -> str | None:
    """The relation of a place (``None`` when not given): one plain line, 1 to
    ``MAX_RELATION_LENGTH`` characters once the spaces around it are removed."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise InvalidArguments("invalid", "the relation is a short text, such as 'my brother'")
    relation = value.strip()
    if len(relation) > MAX_RELATION_LENGTH:
        raise InvalidArguments(
            "too_long", f"the relation has more than {MAX_RELATION_LENGTH} characters"
        )
    if not is_plain_line(relation):
        raise InvalidArguments("invalid", "the relation is one plain line")
    return relation


def _access_from(value: object) -> AccessWay | None:
    """The way to open the door of a place (``None`` when not given)."""
    if value is None:
        return None
    for way in ACCESS_WAYS:
        if way == value:
            return way
    raise InvalidArguments("invalid", 'the access is "personal_access" or "paper_code"')


async def _person_named(session: AsyncSession, name: str, now: datetime) -> Person:
    """The person ``name`` means: someone in the household whose whole name it is, else someone
    who left it and is still on the list with that whole name, else someone in the household
    whose first name or the beginning of whose name it is (``resolve_person``: a former member
    named in full is never taken for someone whose name only begins the same way).
    ``InvalidArguments`` ``not_found`` or ``ambiguous`` otherwise."""
    try:
        return await resolve_person(session, name, now=now, whole_name_only=True)
    except InvalidArguments as refused:
        if refused.code != "not_found":
            raise
    key = name_key(name)
    listed = await _persons_of(session, _person_ids(await _rows(session)))
    kept = [p for p in listed.values() if name_key(p.display_name) == key]
    if len(kept) > 1:
        raise InvalidArguments(
            "ambiguous", "several people on the list have this name: ask which one is meant"
        )
    if not kept:
        return await resolve_person(session, name, now=now)
    return kept[0]


async def _entry_from(session: AsyncSession, item: object, now: datetime) -> SuccessorEntry:
    """One place as the owner gave it: ``{"person": name}`` or ``{"name": name}``, and optionally
    ``"relation"`` and ``"access"``."""
    if not isinstance(item, dict):
        raise InvalidArguments("invalid", "a successor is an object, with a person or a name")
    given = cast(dict[str, object], item)
    keys = frozenset(given)
    if ("person" in keys) == ("name" in keys):
        raise InvalidArguments("invalid", "a successor has a person or a name, not both")
    who = "person" if "person" in keys else "name"
    if not keys <= _PLACE_KEYS | {who}:
        raise InvalidArguments("invalid", f"a successor has only {who}, relation and access")
    relation = _relation_from(given.get("relation"))
    access = _access_from(given.get("access"))
    if who == "person":
        named = given["person"]
        if not isinstance(named, str) or not named.strip():
            raise InvalidArguments("invalid", "person is a name, as a text")
        person = await _person_named(session, named, now)
        if not await session.scalar(select(Account.id).where(Account.person_id == person.id)):
            # Someone with no account is named as someone outside the household: by name and
            # relation.
            raise InvalidArguments(
                "not_possible",
                "this person has no account: name them as someone outside the household",
            )
        return SuccessorEntry(person.id, None, relation, access)
    try:
        name = display_name_value(given["name"])
    except InvalidDisplayName as refused:
        raise InvalidArguments(refused.code, refused.detail) from None
    return SuccessorEntry(None, name, relation, access)


async def entries_from(session: AsyncSession, items: object, now: datetime) -> list[SuccessorEntry]:
    """The places the owner gave (``items``: a list of 0 to ``MAX_SUCCESSORS`` places, see
    ``_entry_from``), the people found by their display name at ``now``, checked as
    ``replace_successors`` checks them. ``InvalidArguments`` for anything that makes no sense."""
    if not isinstance(items, list):
        raise InvalidArguments("invalid", "successors is a list")
    given = cast(list[object], items)
    if len(given) > MAX_SUCCESSORS:
        raise InvalidArguments("too_long", f"the list has {MAX_SUCCESSORS} successors at most")
    entries = [await _entry_from(session, item, now) for item in given]
    try:
        await _checked(session, entries)
    except ValueError as refused:
        raise InvalidArguments("invalid", str(refused)) from None
    return entries


# --- in words ------------------------------------------------------------------------------------


def _details(place: SuccessorShown, texts: Texts) -> list[str]:
    """What is told of a place in brackets: the role (or the day the person left the household),
    the relation, the way to open the door, and the death."""
    details: list[str] = []
    if place.role is not None:
        details.append(texts(f"household.role.{place.role}"))
    elif place.left_on is not None:
        left = texts.date(date.fromisoformat(place.left_on), time=False)
        details.append(texts("successors.left", date=left))
    if place.relation is not None:
        details.append(place.relation)
    if place.access_way is not None:
        details.append(texts(f"successors.access.{place.access_way}"))
    if place.deceased_on is not None:
        deceased = texts.date(date.fromisoformat(place.deceased_on), time=False)
        details.append(texts("successors.deceased", date=deceased))
    return details


def successors_in_words(shown: Sequence[SuccessorShown], texts: Texts) -> str:
    """The list in one line of ``texts`` for a card: ``1. Alex (adult) ; 2. Paul (my brother, paper
    code) ; Tom (deceased 2030-01-01)`` (the living are numbered, in order), or ``successors.none``
    when it is empty."""
    if not shown:
        return texts("successors.none")
    places: list[str] = []
    for place in shown:
        details = _details(place, texts)
        label = f"{place.name} ({', '.join(details)})" if details else place.name
        places.append(label if place.rank is None else f"{place.rank}. {label}")
    return " ; ".join(places)
