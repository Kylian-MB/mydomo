"""Who logs in to the tools' database, and where (spec 5.2, "Base"; 5.8, a person who goes).

A launch for the house logs in as the house's role (``jt_house``): it sees the common space alone.
A launch for a person logs in as that person's own role: it sees the common space and the person's
corner, a schema only that role may use. Each corner is made the first time it is needed
(``space_for``). Their names are random (``jt_<12 hex>`` for a role, ``c_<12 hex>`` for a
schema): nothing in them says whose they are, and the names of tables are all that one role may
see of another's corner (in the catalog; never their rows).

``tools_spaces`` (in the core's database) keeps each role, its corner and its password. The
passwords are secrets of the core: generated here, kept there, given to ``store.ToolsBase`` to log
in, and never to a program, a model, a log or the audit log (``SpaceLogin`` does not show its
password in its ``repr``).

A person's row taken away leaves their corner without anyone (``ON DELETE SET NULL``): the
upkeep's ``corners_pass`` drops it, its role with it (``drop_corners_of_the_gone``), audited
``toolsdb.corner_dropped`` with no name in it. A person who only left the household keeps theirs:
what a launch for them may do is the barrier's to say.

The role and the schema are made by the core (``ToolsAdmin``) outside the caller's transaction:
the row is written first (flushed), so that a role is never made for a row that could not be
written, and two first uses for one person make one corner (the second waits for the first). If
the caller never commits that row, the role and the schema are left with no row: each carries, in
its comment, the tools' database it belongs to and when it was made, and the upkeep drops those
that have had no row for ``ORPHAN_GRACE`` (``drop_orphans``). A corner whose drop fails is left
for the next pass; the others are still dropped.
"""

import logging
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final, Literal

from sqlalchemy import CheckConstraint, ForeignKey, Index, Text, delete, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from domo_core.audit.log import append_audit
from domo_core.db import Base, SessionFactory
from domo_core.toolsdb.setup import (
    HOUSE_ROLE,
    MISSING_DATABASE,
    ToolsAdmin,
    bounds_statements,
    new_password,
    password_statement,
    quoted,
)
from domo_core.upkeep import UpkeepPass

logger = logging.getLogger(__name__)

SpaceKind = Literal["house", "corner"]

CORNER_DROPPED: Final = "toolsdb.corner_dropped"


class ToolsSpace(Base):
    """A role of the tools' database: the house's (``kind = 'house'``, no corner, no person) or a
    person's (``corner``: their schema, ``schema_name``; no person once they were removed)."""

    __tablename__ = "tools_spaces"
    __table_args__ = (
        CheckConstraint("kind IN ('house', 'corner')", name="kind_values"),
        CheckConstraint("(kind = 'corner') = (schema_name IS NOT NULL)", name="corner_has_schema"),
        CheckConstraint("kind = 'corner' OR person_id IS NULL", name="house_has_no_person"),
        CheckConstraint("role_name ~ '^jt_[a-z0-9_]{1,40}$'", name="role_name_format"),
        CheckConstraint(
            "schema_name IS NULL OR schema_name ~ '^c_[0-9a-f]{12}$'", name="schema_name_format"
        ),
        Index(
            "uq_tools_spaces_person_id",
            "person_id",
            unique=True,
            postgresql_where=text("person_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    kind: Mapped[SpaceKind] = mapped_column(Text())
    person_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("persons.id", ondelete="SET NULL")
    )
    role_name: Mapped[str] = mapped_column(unique=True)
    schema_name: Mapped[str | None] = mapped_column(unique=True)
    password: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())


@dataclass(frozen=True)
class SpaceLogin:
    """How the core logs in to the tools' database for a launch: the role, its password (never
    shown in a ``repr``), and the person's corner (``None`` for the house)."""

    role_name: str
    password: str = field(repr=False)
    corner: str | None


def _login_of(space: ToolsSpace) -> SpaceLogin:
    return SpaceLogin(space.role_name, space.password, space.schema_name)


async def _house(session: AsyncSession, admin: ToolsAdmin) -> SpaceLogin:
    found = await session.scalar(select(ToolsSpace).where(ToolsSpace.kind == "house"))
    if found is not None:
        return _login_of(found)
    password = new_password()
    made = await session.scalar(
        pg_insert(ToolsSpace)
        .values(kind="house", role_name=HOUSE_ROLE, password=password)
        .on_conflict_do_nothing(index_elements=[ToolsSpace.role_name])
        .returning(ToolsSpace.id)
    )
    if made is None:
        kept = await session.scalar(select(ToolsSpace).where(ToolsSpace.kind == "house"))
        if kept is None:
            raise RuntimeError("the house's space could not be read")
        return _login_of(kept)
    await admin.execute(password_statement(HOUSE_ROLE, password))
    return SpaceLogin(HOUSE_ROLE, password, None)


async def _corner(session: AsyncSession, admin: ToolsAdmin, person_id: uuid.UUID) -> SpaceLogin:
    found = await session.scalar(select(ToolsSpace).where(ToolsSpace.person_id == person_id))
    if found is not None:
        return _login_of(found)
    role = f"jt_{secrets.token_hex(6)}"
    schema = f"c_{secrets.token_hex(6)}"
    password = new_password()
    made = await session.scalar(
        pg_insert(ToolsSpace)
        .values(
            kind="corner",
            person_id=person_id,
            role_name=role,
            schema_name=schema,
            password=password,
        )
        .on_conflict_do_nothing(
            index_elements=[ToolsSpace.person_id], index_where=text("person_id IS NOT NULL")
        )
        .returning(ToolsSpace.id)
    )
    if made is None:
        # Made in between by another first use, which committed it (the insert waited for it).
        kept = await session.scalar(select(ToolsSpace).where(ToolsSpace.person_id == person_id))
        if kept is None:
            raise RuntimeError("a corner could not be read")
        return _login_of(kept)
    name, corner = quoted(role), quoted(schema)
    mark = _made_mark(admin.database, datetime.now(UTC))
    await admin.execute(
        ";\n".join(
            [
                f"CREATE ROLE {name} LOGIN",
                f"COMMENT ON ROLE {name} IS '{mark}'",
                password_statement(role, password),
                *bounds_statements(role),
                f"CREATE SCHEMA {corner}",
                f"COMMENT ON SCHEMA {corner} IS '{mark}'",
                f"REVOKE ALL ON SCHEMA {corner} FROM PUBLIC",
                f"GRANT USAGE, CREATE ON SCHEMA {corner} TO {name}",
            ]
        )
    )
    return SpaceLogin(role, password, schema)


# How a corner's role and schema say where they belong and when they were made, in their comment.
_MARK_PREFIX: Final = "domo-tools:"
# How long a corner's role or schema may be without a row before the upkeep drops it: far longer
# than the short transaction that makes it and commits its row (``madetools.means``, before the
# launch's first request reaches the base), so that a row not committed yet is never taken for a
# missing one. How long the launch itself then runs (``run_seconds``, a setting of Domo's) does
# not count: its corner's row is committed by then.
ORPHAN_GRACE: Final = timedelta(hours=2)


def _made_mark(database: str, when: datetime) -> str:
    quoted(database)
    return f"{_MARK_PREFIX}{database}:{when.astimezone(UTC).isoformat()}"


def _made_at(comment: object, database: str) -> datetime | None:
    """When a role or a schema of this tools' database was made, read from its comment; ``None``
    for one that is not this database's (another instance's, or made by hand)."""
    prefix = f"{_MARK_PREFIX}{database}:"
    if not isinstance(comment, str) or not comment.startswith(prefix):
        return None
    try:
        return datetime.fromisoformat(comment.removeprefix(prefix))
    except ValueError:
        return None


async def space_for(
    session: AsyncSession, admin: ToolsAdmin, person_id: uuid.UUID | None
) -> SpaceLogin:
    """How a launch for ``person_id`` logs in (the house's role for ``None``), the person's
    corner made on first use. The row is flushed, not committed: the caller commits it. The role
    and the schema are made at once, by the core."""
    if person_id is None:
        return await _house(session, admin)
    return await _corner(session, admin, person_id)


def _drop_statement(role: str | None, schema: str | None) -> str:
    statements: list[str] = []
    if schema is not None:
        statements.append(f"DROP SCHEMA IF EXISTS {quoted(schema)} CASCADE")
    if role is not None:
        name = quoted(role)
        # ``DROP OWNED`` takes what the role still owns in the tools' database (an object of the
        # common space not given away yet, its privileges); it fails for a role already gone.
        statements = [
            f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = '{role}'",
            *statements,
            "DO $drop$ BEGIN IF EXISTS (SELECT FROM pg_roles WHERE rolname = "
            f"'{role}') THEN EXECUTE 'DROP OWNED BY {name} CASCADE'; END IF; END $drop$",
            f"DROP ROLE IF EXISTS {name}",
        ]
    return ";\n".join(statements)


async def drop_corners_of_the_gone(session: AsyncSession, admin: ToolsAdmin) -> int:
    """Drop the corners whose person was removed (their schema, what is in it, and their role),
    their rows deleted and each audited ``toolsdb.corner_dropped`` (no name: the details are
    empty). How many; flushed, not committed. A corner whose drop fails is left for the next pass
    (dropping again what is already gone is harmless), and the others are still dropped."""
    gone = await session.scalars(
        select(ToolsSpace)
        .where(ToolsSpace.kind == "corner", ToolsSpace.person_id.is_(None))
        .order_by(ToolsSpace.created_at, ToolsSpace.id)
        .with_for_update(skip_locked=True)
    )
    dropped = 0
    for space in gone.all():
        if space.schema_name is None:
            continue
        try:
            await admin.execute(_drop_statement(space.role_name, space.schema_name))
        except Exception as error:
            logger.error("A corner of the tools' database could not be dropped (%s)", kind(error))
            continue
        await session.execute(delete(ToolsSpace).where(ToolsSpace.id == space.id))
        await append_audit(session, actor="system", action=CORNER_DROPPED, details={})
        dropped += 1
    return dropped


def kind(error: BaseException) -> str:
    """An error by its kind alone: its message may name a role."""
    return type(error).__name__


async def drop_orphans(
    session: AsyncSession, admin: ToolsAdmin, now: datetime, *, grace: timedelta = ORPHAN_GRACE
) -> int:
    """Drop the corners' roles and schemas of this tools' database that have had no row in
    ``tools_spaces`` for ``grace`` (their row was never committed): what they hold goes with them.
    Only those whose comment says they are this database's are looked at. How many were dropped
    (roles and schemas counted apart). Nothing is written in the core's database."""
    kept = (await session.execute(select(ToolsSpace.role_name, ToolsSpace.schema_name))).all()
    known = {name for row in kept for name in row if name is not None}
    try:
        roles = await admin.fetch(
            "SELECT rolname, shobj_description(oid, 'pg_authid') FROM pg_roles "
            "WHERE rolname ~ '^jt_[0-9a-f]{12}$'"
        )
        schemas = await admin.fetch(
            "SELECT nspname, obj_description(oid, 'pg_namespace') FROM pg_namespace "
            "WHERE nspname ~ '^c_[0-9a-f]{12}$'"
        )
    except MISSING_DATABASE:
        return 0  # no tools' database yet: no corner either
    dropped = 0
    for found, is_role in [*((row, True) for row in roles), *((row, False) for row in schemas)]:
        name, comment = str(found[0]), found[1]
        made = _made_at(comment, admin.database)
        if name in known or made is None or now - made < grace:
            continue
        try:
            await admin.execute(
                _drop_statement(name, None) if is_role else _drop_statement(None, name)
            )
        except Exception as error:
            logger.error("A corner left without a row could not be dropped (%s)", kind(error))
            continue
        dropped += 1
    return dropped


def corners_pass(session_factory: SessionFactory, admin: ToolsAdmin) -> UpkeepPass:
    """The pass of the upkeep that drops the corners of the people who were removed
    (``drop_corners_of_the_gone``), in a session of its own, committed, then those left without a
    row (``drop_orphans``)."""

    async def drop_the_corners(now: datetime) -> None:
        async with session_factory() as session:
            dropped = await drop_corners_of_the_gone(session, admin)
            await session.commit()
            orphans = await drop_orphans(session, admin, now)
        if dropped:
            logger.info("%d corner(s) of the tools' database dropped", dropped)
        if orphans:
            logger.info("%d role(s) or schema(s) left without a row dropped", orphans)

    return drop_the_corners
