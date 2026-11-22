"""The open catalog (spec 5.3): the kinds of action Domo opens for the tools he makes.

Each tool Domo makes has its own action type, ``made.<name>``, which must exist without a release
of the code: it is a row of ``action_kinds`` (``madetools.models.ActionKind``). ``load_policy``
reads them once per turn (kinds are few: one query, no cache) and lays them over the catalog of the
code: each kind is known, allowed to the roles of its ``default_roles``, and to Domo. The
barrier, the owner's rights and the writer of rights then treat it as any other type.

One bound holds in the code (``add_kind``): an open kind never takes the name of a type of the
code (and starts with ``made.``). Its default roles are whatever it is opened with: a tool is
Domo's, and its kind is opened to every role, guests included (the owner may close it to
someone). Its four risks are those of the tool's operations together (``madetools.build``): data
for whoever weighs a launch, never a reason the code refuses one. A name a removed tool freed
keeps its kind until a new tool of that name is made: ``replace_kind`` opens it again, within the
same bounds.

A row that does not read back (a role or a kind of fact that is not one: a database edited by
hand) is left out, and so unknown: its actions are refused, never guessed.
"""

import logging
import re
from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType
from typing import Final, get_args

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.audit.log import append_audit
from domo_core.madetools.models import NAME_PATTERN, ActionKind, MadeTool
from domo_core.policy.catalog import (
    DEFAULT_POLICY,
    KNOWN_ACTIONS,
    MADE_PREFIX,
    FactType,
    OpenKind,
    PolicyTables,
    is_made,
)
from domo_core.policy.roles import ROLES, Role

logger = logging.getLogger(__name__)

# The audit entry of a kind opened (``add_kind``).
KIND_ADDED: Final = "action_kind.added"

_FACT_TYPES: Final[tuple[FactType, ...]] = get_args(FactType)


class KindRefused(Exception):
    """A kind of action that may not be opened (``add_kind``); the message says why, for
    Domo."""


def _fact_type(value: object) -> FactType | None:
    for fact_type in _FACT_TYPES:
        if fact_type == value:
            return fact_type
    return None


def _role(value: object) -> Role | None:
    for role in ROLES:
        if role == value:
            return role
    return None


def _kind_of(row: ActionKind) -> OpenKind | None:
    """The kind of ``row``, or ``None`` when it does not read back."""
    facts: dict[str, FactType] = {}
    for name, value in row.facts.items():
        fact_type = _fact_type(value)
        if fact_type is None:
            return None
        facts[name] = fact_type
    roles: set[Role] = set()
    for value in row.default_roles:
        role = _role(value)
        if role is None:
            return None
        roles.add(role)
    return OpenKind(
        action_type=row.action_type,
        costs=row.costs,
        irreversible=row.irreversible,
        physical=row.physical,
        outward=row.outward,
        facts=MappingProxyType(facts),
        default_roles=frozenset(roles),
        description=row.description,
    )


async def open_kinds(session: AsyncSession) -> Mapping[str, OpenKind]:
    """The kinds of action of the database, by type, that read back and may be open: a type of
    the code, or one that does not start with ``made.``, is left out (``add_kind`` never writes
    one)."""
    rows = (await session.execute(select(ActionKind).order_by(ActionKind.action_type))).scalars()
    kinds: dict[str, OpenKind] = {}
    for row in rows:
        kind = _kind_of(row)
        if kind is None or row.action_type in KNOWN_ACTIONS or not is_made(row.action_type):
            logger.error("An action kind that cannot be read was left out")
            continue
        kinds[kind.action_type] = kind
    return MappingProxyType(kinds)


async def load_policy(session: AsyncSession, base: PolicyTables = DEFAULT_POLICY) -> PolicyTables:
    """``base`` with the kinds of the database laid over it (``open_kinds``): each one known,
    allowed to the roles of its ``default_roles``, and Domo's (the owner, 2026-11-18: his tools
    are his own, and he may do everything but the owner's own). A kind whose type ``base`` knows
    already is the code's or was laid already: ``base`` stays as it says. Read once per turn,
    never mid-turn."""
    kinds = {
        action_type: kind
        for action_type, kind in (await open_kinds(session)).items()
        if action_type not in base.known
    }
    if not kinds:
        return base
    allowed: dict[Role, frozenset[str]] = {
        role: base.allowed.get(role, frozenset())
        | {action_type for action_type, kind in kinds.items() if role in kind.default_roles}
        for role in ROLES
    }
    return PolicyTables(
        known=base.known | frozenset(kinds),
        allowed=MappingProxyType(allowed),
        limits=base.limits,
        kinds=MappingProxyType({**base.kinds, **kinds}),
        domo=base.domo | frozenset(kinds),
    )


def _bounded(kind: OpenKind) -> None:
    """``KindRefused`` when ``kind`` may not be opened (see ``add_kind``)."""
    if kind.action_type in KNOWN_ACTIONS:
        raise KindRefused("this is the name of an action type of the code")
    if not is_made(kind.action_type):
        raise KindRefused("the action type of a tool starts with made.")
    if re.fullmatch(NAME_PATTERN, kind.action_type.removeprefix(MADE_PREFIX)) is None:
        raise KindRefused(
            "after made. comes the name of the tool: 1 to 40 of a-z, 0-9 and _, from a letter"
        )


async def add_kind(session: AsyncSession, kind: OpenKind, *, now: datetime) -> None:
    """Open ``kind`` to the roles of its ``default_roles`` (and to Domo, ``load_policy``), and
    write the audit entry ``action_kind.added`` (its type, its roles and the length of its
    description: never its words). Flushed, not committed.

    ``KindRefused`` when its type is a type of the code (``KNOWN_ACTIONS``), does not start with
    ``made.`` or is not followed by the name of a tool (``madetools.models.NAME_PATTERN``); its
    four risks are whatever it says. No role bounds it: a tool is
    Domo's, not the tool of the one whose turn asked for it. ``IntegrityError`` (from the
    database) when the type is open already, or the description is not 1 to 300 characters."""
    _bounded(kind)
    roles = [role for role in ROLES if role in kind.default_roles]
    session.add(
        ActionKind(
            action_type=kind.action_type,
            costs=kind.costs,
            irreversible=kind.irreversible,
            physical=kind.physical,
            outward=kind.outward,
            facts=dict(kind.facts),
            default_roles=list(roles),
            description=kind.description,
            created_at=now,
        )
    )
    await session.flush()
    await append_audit(
        session,
        actor="system",
        action=KIND_ADDED,
        details={
            "action_type": kind.action_type,
            "roles": list(roles),
            "description_length": len(kind.description),
        },
    )


async def replace_kind(session: AsyncSession, kind: OpenKind, *, now: datetime) -> None:
    """Open ``kind`` again for a name a removed tool freed (``tool_remove`` leaves its kind): the
    row of that type, if any, is replaced by ``kind``, within the same bounds as ``add_kind``
    (``KindRefused``), and the audit entry ``action_kind.added`` is written. ``KindRefused`` too
    while a tool that is not removed has the name: its kind is its own. Flushed, not
    committed."""
    _bounded(kind)
    name = kind.action_type.removeprefix(MADE_PREFIX)
    in_use = await session.scalar(
        select(MadeTool.id).where(MadeTool.name == name, MadeTool.state != "removed")
    )
    if in_use is not None:
        raise KindRefused("a tool in use has this name: its kind is its own")
    # The session forgets the row it may hold of that type, so that the new one takes its place.
    await session.execute(delete(ActionKind).where(ActionKind.action_type == kind.action_type))
    await add_kind(session, kind, now=now)
