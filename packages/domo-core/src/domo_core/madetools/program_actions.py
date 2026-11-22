"""The actions of the catalog a running program may do itself (``act``, spec 5.2 "Action"), by type
(``PROGRAM_HANDLERS``: the code's ``policy.catalog.PROGRAM_ACTIONS``).

Each one describes what it would do for the barrier (``describe``, from its arguments alone) and
does it once it is let through (``run``), for whom the launch is made (``LaunchScope.for_whom``,
judged with ``LaunchScope.actor``). Step 1 has one: ``tool.launch_when``, giving one of Domo's
tools a time to launch at (``{"tool", "when"?, "repeat"?, "arguments"?}``: a ``time`` trigger for
the launch's own for-whom, a person or the house). As in a turn (``tools.made_tools``), a tool
closed to whom the launch is for (they may not launch it: ``made.<name>`` read in the barrier's
tables) is never timed for them (``not_possible``); its arguments must fit the tool and hold no
shape of a secret. ``time_trigger`` is how a program's ``later`` makes its trigger too
(``madetools.means``). Nothing here speaks to a person.
"""

import json
import re
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Final, Protocol, cast

from anyio import to_thread
from domo_common.sandbox_api import JsonObject
from pydantic import JsonValue
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.madetools.models import NAME_PATTERN, MadeTool, MadeToolTrigger
from domo_core.madetools.schema import schema_problem
from domo_core.madetools.service import tool_named
from domo_core.madetools.settings import told, value
from domo_core.memory.service import refuses_secret_shapes
from domo_core.plans.schedule import as_local, next_after
from domo_core.policy.actions import DescribedAction
from domo_core.policy.catalog import MADE_PREFIX
from domo_core.policy.open_catalog import load_policy
from domo_core.policy.rights import load_rights
from domo_core.tools.context import InvalidArguments
from domo_core.tools.plan_tools import household_zone, in_zone, parse_repeat, parse_when

if TYPE_CHECKING:
    from domo_core.madetools.launch import LaunchDeps
    from domo_core.madetools.means import LaunchScope

_NAME: Final = re.compile(NAME_PATTERN)
_NO_TOOL: Final = "no tool of yours has this name"
_CLOSED: Final = (
    "this tool is not open to whom this launch is for: it is not timed for them. Nothing was done"
)
_TOO_MANY: Final = "this tool waits with enough triggers already"
_SECRET: Final = "this looks like a secret: no secret goes in a tool's arguments. Nothing was done"


class ProgramAction(Protocol):
    """An action of the catalog a program may do: what it does, described for the barrier from
    its arguments (``InvalidArguments`` when they are not right), and doing it."""

    def describe(self, arguments: JsonObject, scope: "LaunchScope") -> DescribedAction: ...

    async def run(
        self, arguments: JsonObject, scope: "LaunchScope", deps: "LaunchDeps"
    ) -> JsonValue: ...

    async def tried(self, arguments: JsonObject, deps: "LaunchDeps") -> None:
        """What a dry run checks of it beyond ``describe``, without doing it: what ``run`` would
        refuse whoever the launch is for (``InvalidArguments``)."""
        ...


def _strings(value: object) -> list[str]:
    """Every text in ``value``: the values and the keys, at any depth."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        found: list[str] = []
        for key, item in cast(dict[object, object], value).items():
            if isinstance(key, str):
                found.append(key)
            found.extend(_strings(item))
        return found
    if isinstance(value, list):
        return [text for item in cast(list[object], value) for text in _strings(item)]
    return []


def arguments_within(arguments: JsonObject, most: int) -> None:
    """``InvalidArguments`` (``too_long``, with the setting) when the arguments of a trigger weigh
    more than ``most`` characters as JSON: its tool's ``trigger_argument_chars``."""
    if len(json.dumps(arguments, ensure_ascii=False)) > most:
        raise InvalidArguments(
            "too_long",
            f"the arguments have more than {most} characters; "
            f"{told('trigger_argument_chars', most)}",
            setting=("trigger_argument_chars", most),
        )


def trigger_arguments(parameters: object, arguments: JsonObject, most: int) -> None:
    """``InvalidArguments`` when the arguments of a trigger do not fit the parameters of its tool
    (``invalid``), weigh more than ``most`` characters, its tool's ``trigger_argument_chars``
    (``too_long``, ``arguments_within``) or hold the shape of a secret (``not_possible``). A dry
    run checks them the same way."""
    arguments_within(arguments, most)
    if any(refuses_secret_shapes(text) for text in _strings(arguments)):
        raise InvalidArguments("not_possible", _SECRET)
    problem = schema_problem(parameters, arguments)
    if problem is not None:
        raise InvalidArguments("invalid", f"the arguments do not fit the tool: {problem}")


async def time_trigger(
    session: AsyncSession,
    tool: MadeTool,
    *,
    due_at: datetime,
    repeat: str | None,
    for_person_id: uuid.UUID | None,
    arguments: JsonObject,
) -> MadeToolTrigger:
    """A waiting ``time`` trigger of ``tool`` (its row locked by the caller) at ``due_at``, by the
    rule ``repeat``, for ``for_person_id`` (``None``: the house), with ``arguments``; flushed.
    ``InvalidArguments`` when the arguments are not right for the tool (``trigger_arguments``), or
    when the tool waits with its ``triggers_per_tool`` already (``not_possible``, with the
    setting)."""
    chars = await value(session, "trigger_argument_chars", tool)
    trigger_arguments(tool.parameters, arguments, chars)
    waiting = await session.scalar(
        select(func.count())
        .select_from(MadeToolTrigger)
        .where(MadeToolTrigger.tool_id == tool.id, MadeToolTrigger.state == "waiting")
    )
    most = await value(session, "triggers_per_tool", tool)
    if (waiting or 0) >= most:
        raise InvalidArguments(
            "not_possible",
            f"{_TOO_MANY}; {told('triggers_per_tool', most)}",
            setting=("triggers_per_tool", most),
        )
    trigger = MadeToolTrigger(
        tool_id=tool.id,
        kind="time",
        due_at=due_at,
        repeat=repeat,
        for_person_id=for_person_id,
        arguments=arguments,
    )
    session.add(trigger)
    await session.flush()
    return trigger


class _Timed:
    """The arguments of a ``tool.launch_when`` a program asks, checked."""

    _KEYS: Final = frozenset({"tool", "when", "repeat", "arguments"})

    def __init__(self, arguments: JsonObject) -> None:
        if "tool" not in arguments or not set(arguments) <= self._KEYS:
            raise InvalidArguments("invalid", "the arguments are tool, and when, repeat, arguments")
        name = arguments["tool"]
        if not isinstance(name, str) or not _is_name(name):
            raise InvalidArguments("invalid", "tool is the name of a tool")
        self.tool = name
        when, repeat = arguments.get("when"), arguments.get("repeat")
        self.when: datetime | None = None if when is None else parse_when(when)
        self.repeat: str | None = None if repeat is None else parse_repeat(repeat)
        if self.when is None and self.repeat is None:
            raise InvalidArguments("invalid", "say when to launch it: when, repeat, or both")
        given = arguments.get("arguments", {})
        if not isinstance(given, dict):
            raise InvalidArguments("invalid", "arguments is an object")
        self.arguments: JsonObject = given


def _is_name(name: str) -> bool:
    return _NAME.fullmatch(name) is not None


class LaunchWhen:
    """``tool.launch_when``: give one of Domo's tools a time to launch at, for whom the launch
    that asks is made."""

    action_type: Final = "tool.launch_when"

    def describe(self, arguments: JsonObject, scope: "LaunchScope") -> DescribedAction:
        # Checked here, never described: the name is the program's own text, and what is
        # described goes to the audit log (metadata only). Who may not launch that tool is
        # refused when it runs.
        _Timed(arguments)
        return DescribedAction(
            connector="program",
            action_type=self.action_type,
            target=None,
            effect="program",
            destination="household",
        )

    async def tried(self, arguments: JsonObject, deps: "LaunchDeps") -> None:
        timed = _Timed(arguments)
        async with deps.session_factory() as session:
            tool = await tool_named(session, timed.tool)
            if tool is None:
                raise InvalidArguments("not_found", _NO_TOOL)
            chars = await value(session, "trigger_argument_chars", tool)
        trigger_arguments(tool.parameters, timed.arguments, chars)

    async def run(
        self, arguments: JsonObject, scope: "LaunchScope", deps: "LaunchDeps"
    ) -> JsonValue:
        timed = _Timed(arguments)
        now = deps.clock()
        person = scope.for_whom.person
        async with deps.session_factory() as session:
            tool = await tool_named(session, timed.tool)
            if tool is None:
                raise InvalidArguments("not_found", _NO_TOOL)
            # Who may not launch a tool may not have it timed either (the owner closed it).
            policy = await load_policy(session, deps.policy_base)
            rights = await load_rights(session)
            if not rights.resolve(scope.actor, f"{MADE_PREFIX}{tool.name}", policy).allowed:
                raise InvalidArguments("not_possible", _CLOSED)
            await session.refresh(tool, with_for_update=True)
            zone = await household_zone(session)
            if timed.when is not None:
                due = in_zone(timed.when, zone, now)
            else:
                found = await to_thread.run_sync(next_after, cast(str, timed.repeat), now, zone)
                if found is None:
                    raise InvalidArguments("invalid", "this rule gives no time to come")
                due = found
            trigger = await time_trigger(
                session,
                tool,
                due_at=due,
                repeat=timed.repeat,
                for_person_id=None if person is None else person.id,
                arguments=timed.arguments,
            )
            told: JsonObject = {"id": str(trigger.id), "when": as_local(due, zone)}
            await session.commit()
        return told


# The actions a program may do, by type: those of ``policy.catalog.PROGRAM_ACTIONS``.
PROGRAM_HANDLERS: Final[Mapping[str, ProgramAction]] = {"tool.launch_when": LaunchWhen()}
