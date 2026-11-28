"""The assistant makes roles for the household, changes them, deletes them and looks at them:
``role_create``, ``role_change``, ``role_delete`` and ``roles_look``.

A role starts from the rights of a built-in base (an adult of the house, a child, someone passing
by) and gives or denies action types beside it (``people.made_roles``). It never holds what is
above its ceiling (``policy.catalog.ROLE_CEILING``): such a call is refused, naming the types
(they are no secret), and nothing is written. A role for one single person is an ordinary role
carried by one record: ``role_create`` then ``household_change_role``. A role that nobody carries
any more is cleaned up (``people.records.leave_role``, ``household.upkeep``).

Creating and changing a role changes who may do what: they are shown and judged. Deleting one only
removes a role nobody uses, and is silent; looking at the roles changes nothing. Whose act it is
follows ``people.whose``: a call carries ``reason`` exactly when it is the assistant's own. What the
tools give back is data for the assistant: he writes every text himself.
"""

from typing import Final, cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.llm.types import JsonObject, ToolDefinition
from domo_core.people import made_roles, whose
from domo_core.people.acts import tell_owner_of
from domo_core.people.models import MadeRole
from domo_core.policy.actions import DescribedAction
from domo_core.policy.limits import InvalidLimit, check_limit
from domo_core.policy.open_catalog import load_policy
from domo_core.policy.roles import ROLES, Actor, Role
from domo_core.policy.settings import load_policy_settings
from domo_core.tools.context import InvalidArguments, ToolContext, ToolOutcome

# What a call leaves under the reply.
_CARD: Final[JsonObject] = {"key": "action.card.done", "params": {}}
_BASES: Final = ("adult", "child", "guest")
_MAX_TYPES: Final = 200
_REASON_PROPERTY: Final[JsonObject] = {
    "type": "string",
    "description": (
        "Only when the idea is yours (after act_on_my_own): why you do it, in one sentence. "
        "Leave it out when the person asked for it."
    ),
    "minLength": 1,
    "maxLength": whose.MAX_REASON_LENGTH,
}


def _types_property(what: str) -> JsonObject:
    return {
        "type": "array",
        "description": what,
        "items": {"type": "string", "minLength": 1},
        "maxItems": _MAX_TYPES,
    }


def _only(arguments: JsonObject, allowed: frozenset[str], needed: frozenset[str]) -> None:
    keys = frozenset(arguments)
    if not needed <= keys or not keys <= allowed:
        raise InvalidArguments(
            "invalid",
            f"the arguments are {', '.join(sorted(needed))} and, if needed, "
            f"{', '.join(sorted(allowed - needed))}",
        )


def _text(arguments: JsonObject, argument: str) -> str:
    value = arguments.get(argument)
    if not isinstance(value, str) or not value.strip():
        raise InvalidArguments("invalid", f"{argument} is a text")
    return value


def _types(arguments: JsonObject, argument: str) -> list[str]:
    """The action types the argument lists (none when it is left out)."""
    value = arguments.get(argument, [])
    items = cast("list[object]", value) if isinstance(value, list) else None
    if items is None or len(items) > _MAX_TYPES:
        raise InvalidArguments("invalid", f"{argument} is a list of action types")
    types: list[str] = []
    for item in items:
        if not isinstance(item, str) or not item.strip():
            raise InvalidArguments("invalid", f"{argument} is a list of action types")
        types.append(item.strip())
    return types


def _not_possible(error: Exception) -> InvalidArguments:
    """What a refusal of ``people.made_roles`` says to the assistant."""
    if isinstance(error, made_roles.AboveTheCeiling):
        return InvalidArguments(
            "not_possible",
            "no role holds these types, they are above the ceiling of a role: "
            + ", ".join(error.types),
        )
    if isinstance(error, made_roles.RoleNameTaken):
        return InvalidArguments(
            "not_possible", "a role, the owner's or a built-in one, already has this name"
        )
    if isinstance(error, made_roles.RoleInUse):
        return InvalidArguments(
            "not_possible", "a promise of access that waits names this role: drop it first"
        )
    if isinstance(error, made_roles.OwnersRule):
        return InvalidArguments(
            "not_possible",
            "a rule of the owner's applies to this role: it stays, only the household's owner "
            "changes it (" + ", ".join(error.types) + ")",
        )
    return InvalidArguments("invalid", str(error))


# The owner's rules a call of his writes for the role: one per action type, as he said them.
_OWNER_LINES_PROPERTY: Final[JsonObject] = {
    "type": "array",
    "description": (
        "Only when the household's owner says a rule for this role himself: for each action "
        "type, what more he says beside allow or deny (a limit written as a CEL expression "
        "answering allow, refuse, ask_owner or judge, with judge_question; whose agreement "
        "first: approver; what to ask before: ask), and summary, his rule in plain words."
    ),
    "items": {
        "type": "object",
        "properties": {
            "type": {"type": "string"},
            "limit": {"type": "string", "maxLength": 2000},
            "judge_question": {"type": "string", "maxLength": 300},
            "approver": {"type": "string", "enum": ["self", "adult", "owner"]},
            "ask": {"type": "string", "enum": ["yes", "explicit_yes", "proof"]},
            "summary": {"type": "string", "minLength": 1, "maxLength": 300},
        },
        "required": ["type", "summary"],
        "additionalProperties": False,
    },
}


async def _owner_lines(
    session: AsyncSession, arguments: JsonObject, *, by_owner: bool
) -> dict[str, made_roles.OwnerLine]:
    """The owner's rules of a ``role_change`` call (``owner_lines``), checked: only on his own
    request, each a type the call also allows or denies, a limit that compiles and reads the
    facts of its type."""
    given = arguments.get("owner_lines")
    if given is None:
        return {}
    if not by_owner:
        raise InvalidArguments(
            "invalid", "owner_lines are written only on the household owner's own request"
        )
    if not isinstance(given, list):
        raise InvalidArguments("invalid", "owner_lines is a list")
    named = {*_types(arguments, "allow"), *_types(arguments, "deny")}
    settings = await load_policy_settings(session)
    lines: dict[str, made_roles.OwnerLine] = {}
    for item in cast("list[object]", given):
        if not isinstance(item, dict):
            raise InvalidArguments("invalid", "each of owner_lines is an object")
        line = cast("dict[str, object]", item)
        action_type = line.get("type")
        if not isinstance(action_type, str) or action_type not in named:
            raise InvalidArguments(
                "invalid", "each of owner_lines names a type the call allows or denies"
            )
        limit = line.get("limit")
        if limit is not None:
            if not isinstance(limit, str):
                raise InvalidArguments("invalid", "limit is a text")
            try:
                check_limit(limit, action_type, time_zone_set=settings.time_zone is not None)
            except InvalidLimit as error:
                raise InvalidArguments("invalid", f"the limit cannot be: {error}") from None
        question, approver, ask, summary = (
            line.get("judge_question"),
            line.get("approver"),
            line.get("ask"),
            line.get("summary"),
        )
        if not isinstance(summary, str) or not summary.strip():
            raise InvalidArguments("invalid", "summary is the owner's rule in plain words")
        lines[action_type] = made_roles.OwnerLine(
            limit_expression=limit,
            judge_question=question if isinstance(question, str) and limit else None,
            approver=approver if approver in ("self", "adult", "owner") else None,  # type: ignore[arg-type]
            ask=ask if ask in ("yes", "explicit_yes", "proof") else None,  # type: ignore[arg-type]
            summary=summary.strip()[:300],
        )
    return lines


async def _role_named(session: AsyncSession, name: str) -> MadeRole:
    role = await made_roles.named(session, name)
    if role is None:
        raise InvalidArguments(
            "not_found", "no role made for the household has this name (see roles_look)"
        )
    return role


class RoleCreate:
    """Make a role for the household."""

    definition = ToolDefinition(
        name="role_create",
        description=(
            "Make a role for the household: a name, a base (adult: someone of the house, child, "
            "guest: someone passing by) and what it gives (allow) or takes away (deny) beside "
            "that base, as action types of the catalog. It gives nothing by itself: give it "
            "to someone with household_change_role. A role may be made for one single person "
            "(someone always at the house who should control it, say). No role holds what is "
            "above its ceiling: what only the household's owner holds, spending limits, the "
            "gestures on people and roles, what is closed to you; such a call is refused, the "
            "refusal names those types, and nothing is made. " + whose.HIS_OWN_IDEA
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Its name, in your own plain words: 'neighbour', 'nanny'.",
                    "minLength": 1,
                    "maxLength": 40,
                },
                "base": {"type": "string", "enum": list(_BASES)},
                "allow": _types_property("The action types it gives beside its base."),
                "deny": _types_property("The action types it takes away from its base."),
                "reason": _REASON_PROPERTY,
            },
            "required": ["name", "base"],
            "additionalProperties": False,
        },
    )
    action_type = "role.create"

    @staticmethod
    def _read(arguments: JsonObject) -> tuple[str, str, list[str], list[str]]:
        _only(
            arguments,
            frozenset({"name", "base", "allow", "deny", "reason"}),
            frozenset({"name", "base"}),
        )
        base = arguments["base"]
        if base not in _BASES:
            raise InvalidArguments("invalid", 'the base is "adult", "child" or "guest"')
        return (
            _text(arguments, "name"),
            str(base),
            _types(arguments, "allow"),
            _types(arguments, "deny"),
        )

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        whose.check(arguments, actor)
        _, _, allow, deny = self._read(arguments)
        async with ctx.session_factory() as session:
            try:
                await made_roles.check_types(session, allow, deny)
            except (made_roles.AboveTheCeiling, ValueError) as error:
                raise _not_possible(error) from None
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=None,
            effect="permissions",
            destination="household",
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        name, base, allow, deny = self._read(arguments)
        by = whose.by(arguments, ctx)
        async with ctx.session_factory() as session:
            try:
                role = await made_roles.create(
                    session,
                    name=name,
                    base=base,  # type: ignore[arg-type]
                    allow=allow,
                    deny=deny,
                    by=by,
                    reason=whose.reason_of(arguments),
                    turn_of=whose.turn_of(arguments, ctx),
                    now=ctx.clock(),
                )
            except (made_roles.AboveTheCeiling, made_roles.RoleNameTaken, ValueError) as error:
                raise _not_possible(error) from None
            made: JsonObject = {"id": str(role.id), "role": role.name, "base": role.base}
            await session.commit()
        return ToolOutcome(status="done", model_result=made, card=_CARD)


class RoleChange:
    """Change a role made for the household."""

    definition = ToolDefinition(
        name="role_change",
        description=(
            "Change a role made for the household: its name, what it gives (allow) or takes "
            "away (deny) from now on, and what it says nothing about any more (forget: the "
            "base decides again). Everyone who carries it is changed at once, and whoever gets it "
            "later has it too: a rule the household's owner gives for the role ('the nannies may "
            "not open the garage', '...not after 22:00', '...ask me first') is written here, on "
            "his own request, with owner_lines for what he says beside allow or deny: it is his "
            "rule, which only his own request changes. The same ceiling as when it was made. "
            + whose.TELL_OWNER_IS_YOURS
            + " "
            + whose.HIS_OWN_IDEA
        ),
        parameters={
            "type": "object",
            "properties": {
                "role": {"type": "string", "description": "The name of the role."},
                "name": {
                    "type": "string",
                    "description": "Its new name.",
                    "minLength": 1,
                    "maxLength": 40,
                },
                "allow": _types_property("The action types it gives from now on."),
                "deny": _types_property("The action types it takes away from now on."),
                "forget": _types_property("The action types it says nothing about any more."),
                "owner_lines": _OWNER_LINES_PROPERTY,
                "reason": _REASON_PROPERTY,
                "tell_owner": whose.TELL_OWNER_PROPERTY,
            },
            "required": ["role"],
            "additionalProperties": False,
        },
    )
    action_type = "role.change"

    @staticmethod
    def _read(arguments: JsonObject) -> tuple[str, str | None, list[str], list[str], list[str]]:
        _only(
            arguments,
            frozenset(
                {"role", "name", "allow", "deny", "forget", "owner_lines", "reason", "tell_owner"}
            ),
            frozenset({"role"}),
        )
        name = None if "name" not in arguments else _text(arguments, "name")
        allow, deny, forget = (
            _types(arguments, "allow"),
            _types(arguments, "deny"),
            _types(arguments, "forget"),
        )
        if name is None and not (allow or deny or forget):
            raise InvalidArguments("invalid", "say what changes: name, allow, deny or forget")
        return _text(arguments, "role"), name, allow, deny, forget

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        whose.check(arguments, actor)
        whose.tells_the_owner(arguments, actor)
        named, _, allow, deny, forget = self._read(arguments)
        async with ctx.session_factory() as session:
            role = await _role_named(session, named)
            try:
                await made_roles.check_types(session, allow, deny, forget)
            except (made_roles.AboveTheCeiling, ValueError) as error:
                raise _not_possible(error) from None
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=f"role:{role.id}",
            effect="permissions",
            destination="household",
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        named, name, allow, deny, forget = self._read(arguments)
        by = whose.by(arguments, ctx)
        by_owner = whose.by_the_owner(arguments, ctx)
        async with ctx.session_factory() as session:
            role = await _role_named(session, named)
            lines = await _owner_lines(session, arguments, by_owner=by_owner)
            try:
                await made_roles.change(
                    session,
                    role,
                    name=name,
                    allow=allow,
                    deny=deny,
                    forget=forget,
                    by=by,
                    reason=whose.reason_of(arguments),
                    turn_of=whose.turn_of(arguments, ctx),
                    now=ctx.clock(),
                    by_owner=by_owner,
                    lines=lines,
                )
            except (
                made_roles.AboveTheCeiling,
                made_roles.RoleNameTaken,
                made_roles.OwnersRule,
                ValueError,
            ) as error:
                raise _not_possible(error) from None
            changed: JsonObject = {"id": str(role.id), "role": role.name}
            # His own change, told to the owner as he judged the owner would want.
            owner = (
                await tell_owner_of(
                    session,
                    "role_changed",
                    knows=lambda one: whose.owner_knows(ctx, one),
                    now=ctx.clock(),
                    role_id=role.id,
                )
                if whose.tells_the_owner(arguments, ctx.acting)
                else None
            )
            await session.commit()
        if owner is not None and ctx.notify is not None:
            ctx.notify(owner)
        return ToolOutcome(status="done", model_result=changed, card=_CARD)


class RoleDelete:
    """Delete a role made for the household that nobody uses."""

    definition = ToolDefinition(
        name="role_delete",
        description=(
            "Delete a role made for the household, when the household's owner asks it or it is "
            "your own idea: at once, whoever carries it goes back to its base (an ordinary "
            "adult, say), and each is told; the result names them. Not while a promise of "
            "access that waits names it, nor, on your own idea, while it holds a rule the "
            "household's owner said for it. It is judged as a change of role is. "
            + whose.TELL_OWNER_IS_YOURS
            + " "
            + whose.HIS_OWN_IDEA
        ),
        parameters={
            "type": "object",
            "properties": {
                "role": {"type": "string", "description": "The name of the role."},
                "reason": _REASON_PROPERTY,
                "tell_owner": whose.TELL_OWNER_PROPERTY,
            },
            "required": ["role"],
            "additionalProperties": False,
        },
    )
    action_type = "role.delete"

    @staticmethod
    def _read(arguments: JsonObject) -> str:
        _only(arguments, frozenset({"role", "reason", "tell_owner"}), frozenset({"role"}))
        return _text(arguments, "role")

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        whose.check(arguments, actor)
        whose.tells_the_owner(arguments, actor)
        named = self._read(arguments)
        async with ctx.session_factory() as session:
            role = await _role_named(session, named)
            carriers = list(
                await session.scalars(
                    select(Person.display_name).where(made_roles.carries(role.id))
                )
            )
            accesses = dict(await made_roles.accesses_of(session, role.id))
        # What it changes for whoever carries it, for the judge: each goes back to the base, and
        # loses what the role gave and gets back what it took away.
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=f"role:{role.id}",
            effect="permissions",
            destination="household",
            facts={
                "base": role.base,
                "carried_by": len(carriers),
                "carriers": ", ".join(sorted(carriers)) or None,
                "they_lose": ", ".join(sorted(t for t, a in accesses.items() if a == "allow"))
                or None,
                "they_get_back": ", ".join(sorted(t for t, a in accesses.items() if a == "deny"))
                or None,
            },
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        named = self._read(arguments)
        by = whose.by(arguments, ctx)
        async with ctx.session_factory() as session:
            role = await _role_named(session, named)
            try:
                holders = await made_roles.delete(
                    session,
                    role,
                    by=by,
                    reason=whose.reason_of(arguments),
                    turn_of=whose.turn_of(arguments, ctx),
                    now=ctx.clock(),
                    by_owner=whose.by_the_owner(arguments, ctx),
                )
            except (made_roles.RoleInUse, made_roles.OwnersRule) as error:
                raise _not_possible(error) from None
            # His own deletion, told to the owner when he judged the owner would want it.
            told = (
                await tell_owner_of(
                    session,
                    "role_deleted",
                    knows=lambda one: whose.owner_knows(ctx, one),
                    now=ctx.clock(),
                    role_id=role.id,
                )
                if whose.tells_the_owner(arguments, ctx.acting)
                else None
            )
            names = (
                list(
                    await session.scalars(select(Person.display_name).where(Person.id.in_(holders)))
                )
                if holders
                else []
            )
            await session.commit()
        if ctx.notify is not None:
            for holder in holders:
                ctx.notify(holder)
            if told is not None:
                ctx.notify(told)
        done: JsonObject = {"deleted": True}
        if names:
            done["back_to_base"] = list(names)
        return ToolOutcome(status="done", model_result=done, card=_CARD)


class RolesLook:
    """Look at the roles: the four built-in ones and those made for the household."""

    definition = ToolDefinition(
        name="roles_look",
        description=(
            "Look at the roles: the four built-in ones and those made for the household, each "
            "with its base, what it gives or takes away, and how many records carry it. With "
            "role: that one only."
        ),
        parameters={
            "type": "object",
            "properties": {"role": {"type": "string", "description": "The name of a role."}},
            "additionalProperties": False,
        },
    )
    action_type = "role.look"

    @staticmethod
    def _read(arguments: JsonObject) -> str | None:
        _only(arguments, frozenset({"role"}), frozenset())
        return None if "role" not in arguments else _text(arguments, "role")

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        named = self._read(arguments)
        target = None
        if named is not None:
            async with ctx.session_factory() as session:
                target = f"role:{(await _role_named(session, named)).id}"
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=target,
            effect="read",
            destination="household",
        )

    @staticmethod
    async def _made(session: AsyncSession, role: MadeRole) -> JsonObject:
        accesses = await made_roles.accesses_of(session, role.id)
        carried = await session.scalar(
            select(func.count()).select_from(Person).where(made_roles.carries(role.id))
        )
        return {
            "role": role.name,
            "made": True,
            "base": role.base,
            "allow": sorted(kind for kind, access in accesses.items() if access == "allow"),
            "deny": sorted(kind for kind, access in accesses.items() if access == "deny"),
            "carried_by": carried or 0,
        }

    @staticmethod
    async def _built_in(session: AsyncSession, name: Role) -> JsonObject:
        policy = await load_policy(session)
        carried = await session.scalar(
            select(func.count())
            .select_from(Person)
            .where(
                Person.role == name,
                Person.made_role_id.is_(None),
                Person.state == "has_access",
                Person.merged_into_id.is_(None),
                Person.forgotten_at.is_(None),
            )
        )
        return {
            "role": name,
            "made": False,
            "allow": sorted(policy.allowed.get(name, frozenset())),
            "carried_by": carried or 0,
        }

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        named = self._read(arguments)
        async with ctx.session_factory() as session:
            shown: list[JsonObject] = []
            if named is None:
                shown = [await self._built_in(session, name) for name in ROLES]
                shown += [
                    await self._made(session, role) for role in await made_roles.all_roles(session)
                ]
            elif (built_in := _built_in_role(named)) is not None:
                shown = [await self._built_in(session, built_in)]
            else:
                shown = [await self._made(session, await _role_named(session, named))]
        return ToolOutcome(status="done", model_result={"roles": shown}, card=_CARD)


def _built_in_role(name: str) -> Role | None:
    """The built-in role ``name`` is the name of (compared as a role's name is), if any."""
    key = made_roles.name_key(name)
    return next((role for role in ROLES if role == key), None)
