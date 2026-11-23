"""The bounds of Domo's tools are settings of his own (spec 12.1, the owner, 2026-11-21).

Every bound on what his tools and their programs do is a whole number with a start (``SETTINGS``):
Domo reads it and changes it himself, for the whole house (``tool_settings``, ``set_house``) or
for one tool alone (``made_tools.settings``, ``set_for_tool``, only the keys of ``PER_TOOL``). What
counts for a launch is the tool's own value, else the house's, else the start (``value``). There is
no ceiling above a setting; its floor (``least``) is only where a smaller number has no meaning.

Only Domo changes a setting, by a tool call in the core (``tools.made_tools``: ``tools_settings``
and ``tool_set``): no request a program makes (``madetools.asks.ProgramAsk``) reaches this module.
A refusal a setting causes carries its key and its figure (``hit``), so that Domo judges whether
to change it."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from types import MappingProxyType
from typing import Final, cast

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.madetools.models import MadeTool, ToolSetting


@dataclass(frozen=True)
class Setting:
    """One bound: its ``key``, the number it starts at (``start``), the smallest it may be
    (``least``) and what it bounds, in one plain sentence Domo reads (``says``)."""

    key: str
    start: int
    least: int
    says: str


_ALL: Final = (
    Setting(
        "chain_depth",
        5,
        1,
        "How many tools deep a chain goes: a tool launched by a turn or a trigger, the tool it "
        "launches, and so on.",
    ),
    Setting(
        "chain_launches", 10, 1, "How many launches one chain makes in all, the first one too."
    ),
    Setting(
        "requests_per_launch",
        1000,
        1,
        "How many requests one launch makes of the core; the next one stops its run.",
    ),
    Setting(
        "answer_seconds", 30, 1, "How many seconds the core takes at most to answer one request."
    ),
    Setting("run_seconds", 60, 1, "How many seconds one launch may run."),
    Setting("memory_mb", 256, 1, "How many megabytes of memory one launch may use."),
    Setting("output_bytes", 65536, 1, "How many bytes a program may write as it runs."),
    Setting("result_bytes", 65536, 1, "How many bytes a launch's result may weigh, as JSON."),
    Setting("rows", 1000, 1, "How many rows one read of your tools' base gives back at most."),
    Setting("statement_seconds", 10, 1, "How many seconds one statement on your tools' base runs."),
    Setting(
        "request_seconds",
        15,
        1,
        "How many seconds one request to your tools' base may take in all.",
    ),
    Setting("dry_run_seconds", 60, 1, "How many seconds a dry run of an example may take."),
    Setting(
        "launches_per_day",
        200,
        1,
        "How many times a day a tool may be launched; past that it is set aside.",
    ),
    Setting(
        "wakes_per_day",
        20,
        1,
        "How many times a day a tool may wake you; past that it is set aside.",
    ),
    Setting("triggers_per_tool", 10, 1, "How many triggers one tool may wait with."),
    Setting(
        "trigger_argument_chars",
        4000,
        1,
        "How many characters the arguments of one trigger of a tool have at most, as JSON "
        "(tool_launch_when, a later, or a program's tool.launch_when).",
    ),
    Setting(
        "later_days", 365, 1, "How many days ahead a launch may ask to be launched again (later)."
    ),
    Setting(
        "wake_bytes",
        4096,
        1,
        "How many bytes of JSON the content of one wake weighs at most (each wake is a turn of "
        "yours: what it holds is read in it).",
    ),
    Setting("failures_in_a_row", 3, 1, "How many failures in a row set a tool aside."),
    Setting("places", 4, 1, "How many programs the sandbox runs at once."),
    Setting(
        "free_rounds",
        5,
        0,
        "How many rounds in a row of a build that make no tool run at once, before the next ones "
        "wait.",
    ),
    Setting(
        "first_pause_seconds",
        10,
        1,
        "How many seconds the first wait of a slowed build lasts; each next one doubles.",
    ),
    Setting(
        "longest_pause_seconds",
        600,
        1,
        "How many seconds the longest wait of a slowed build lasts.",
    ),
    Setting(
        "give_up_hours",
        24,
        1,
        "How many hours a build may wait in all, its checks or its model out of reach, before it "
        "is abandoned.",
    ),
    Setting("builds_at_once", 2, 1, "How many builds run at once."),
    Setting(
        "making_seconds",
        600,
        1,
        "How many seconds one call of the model that writes a tool may take; past that the round "
        "waits and is tried again.",
    ),
    Setting(
        "review_seconds",
        180,
        1,
        "How many seconds one call of the reviewer of a tool may take; past that the review is "
        "unavailable and the round waits.",
    ),
    Setting(
        "purpose_chars",
        1800,
        1,
        "How many characters the purpose of a build has at most (tool_build, tool_change).",
    ),
    Setting(
        "examples",
        5,
        1,
        "How many examples a build is checked against at most; each is a dry run, one after the "
        "other (tool_check runs them again).",
    ),
    Setting(
        "examples_chars",
        20000,
        1,
        "How many characters the examples of a build have at most, as JSON: they are given to the "
        "model that writes the tool.",
    ),
    Setting(
        "code_chars", 20000, 1, "How many characters the code of a tool or a program may have."
    ),
    Setting("tools_offered", 30, 1, "How many of your tools a turn offers at most."),
    Setting("hand_runs_kept_days", 7, 1, "How many days a program you ran by hand is kept."),
    Setting("launches_kept_days", 90, 1, "How many days the trace of a launch is kept."),
    Setting(
        "trouble_wakes_per_day",
        5,
        0,
        "How many times a day you are woken because a tool was set aside or a build abandoned.",
    ),
    # Going out (step 2, spec 13.3): what one request a program sends through the gatekeeper may
    # take, and how many one launch sends; how long a site's session is kept unused.
    Setting(
        "out_seconds",
        30,
        1,
        "How many seconds one request a program sends out may take in all, redirects included.",
    ),
    Setting(
        "out_bytes",
        1_048_576,
        1,
        "How many bytes the answer to one request a program sends out may weigh; a heavier one "
        "is refused, and so is one past the channel's own ceiling.",
    ),
    Setting(
        "out_per_launch", 100, 0, "How many requests one launch may send out; the next is refused."
    ),
    Setting(
        "session_idle_minutes",
        60,
        1,
        "How many minutes a site's session is kept without being used before it is forgotten.",
    ),
)

SETTINGS: Final[Mapping[str, Setting]] = MappingProxyType({s.key: s for s in _ALL})
# The keys a single tool may carry: the bounds of its own launches.
PER_TOOL: Final[frozenset[str]] = frozenset(
    {
        "chain_depth",
        "chain_launches",
        "requests_per_launch",
        "answer_seconds",
        "run_seconds",
        "memory_mb",
        "output_bytes",
        "result_bytes",
        "rows",
        "statement_seconds",
        "request_seconds",
        "launches_per_day",
        "wakes_per_day",
        "triggers_per_tool",
        "trigger_argument_chars",
        "later_days",
        "wake_bytes",
        "failures_in_a_row",
        "out_seconds",
        "out_bytes",
        "out_per_launch",
    }
)


class InvalidSetting(ValueError):
    """A change no setting takes: ``key`` and ``why``, in words Domo reads."""

    def __init__(self, key: str, why: str) -> None:
        super().__init__(f"{key}: {why}")
        self.key = key
        self.why = why


def hit(key: str, value: int) -> dict[str, object]:
    """What every refusal a setting causes carries: the setting's key and its figure. It goes in
    what Domo reads (a refusal, a failure, a reason a tool was set aside), never in an audit
    line (whose details hold no ``value``)."""
    return {"setting": key, "value": value}


# The longest span of time a date of the database holds with room to spare (a hundred years):
# what the machine cannot give past it. A setting of more is "never" (a time never reached) or
# "forever" (kept), never an error.
HELD: Final = timedelta(days=36_500)


# The largest whole number the database keeps in a count or a time (a ``bigint``): what the machine
# cannot hold past it. A setting of more counts as that much, which no count or time ever reaches.
HELD_NUMBER: Final = 2**63 - 1


def held(number: int) -> int:
    """``number`` as the database keeps it: ``HELD_NUMBER`` at most."""
    return min(number, HELD_NUMBER)


def span(seconds: int) -> timedelta:
    """``seconds`` as a span of time, ``HELD`` at most."""
    return HELD if seconds >= HELD.total_seconds() else timedelta(seconds=seconds)


def wait_for(seconds: int) -> float:
    """``seconds`` as a clock waits it (``anyio.move_on_after``, ``asyncio.timeout``): ``HELD`` at
    most, a time no clock reaches, so that no figure is too big for a clock's arithmetic."""
    return float(min(seconds, int(HELD.total_seconds())))


def told(key: str, value: int) -> str:
    """The words Domo reads with a refusal the setting ``key`` caused: its figure, and that it
    is his to change."""
    where = "tools_settings, or tool_set for one tool" if key in PER_TOOL else "tools_settings"
    return f"{key} is {value}, a setting of yours: you may change it ({where})"


def starts() -> dict[str, int]:
    """Every setting at its start: what counts before anything is read."""
    return {key: setting.start for key, setting in SETTINGS.items()}


def _whole(found: object) -> int | None:
    """``found`` if it is a whole number (never a boolean), else ``None``."""
    if isinstance(found, bool) or not isinstance(found, int):
        return None
    return found


def check(changes: Mapping[str, object], *, for_a_tool: bool) -> dict[str, int | None]:
    """``changes`` as a setting takes them (a key to a whole number, or ``None``: back to the level
    above); ``InvalidSetting`` for an unknown key, a key a tool may not carry (``for_a_tool``), a
    value that is not a whole number (a float, a text, a boolean) or one under the key's
    ``least``. Pure: nothing is read nor written."""
    checked: dict[str, int | None] = {}
    for key, given in changes.items():
        setting = SETTINGS.get(key)
        if setting is None:
            raise InvalidSetting(key, "no setting has this key (tools_settings lists them)")
        if for_a_tool and key not in PER_TOOL:
            raise InvalidSetting(
                key, "this setting is the house's, not a tool's: use tools_settings"
            )
        if given is None:
            checked[key] = None
            continue
        number = _whole(given)
        if number is None:
            raise InvalidSetting(key, "a setting is a whole number, or null to go back")
        if number < setting.least:
            raise InvalidSetting(key, f"this setting is {setting.least} at least")
        checked[key] = number
    return checked


def _known_numbers(stored: Mapping[str, object], keys: frozenset[str]) -> dict[str, int]:
    """What ``stored`` holds of ``keys`` as whole numbers (anything else is ignored)."""
    kept: dict[str, int] = {}
    for key, found in stored.items():
        number = _whole(found)
        if key in keys and number is not None:
            kept[key] = number
    return kept


async def _house(session: AsyncSession) -> dict[str, int]:
    rows = await session.execute(select(ToolSetting.key, ToolSetting.value))
    return _known_numbers({key: found for key, found in rows.all()}, frozenset(SETTINGS))


def own_values(tool: MadeTool) -> dict[str, int]:
    """The settings ``tool`` carries of its own."""
    return _known_numbers(cast(Mapping[str, object], tool.settings), PER_TOOL)


async def all_values(session: AsyncSession, tool: MadeTool | None = None) -> dict[str, int]:
    """Every setting's value: the tool's own, else the house's, else its start."""
    values = {key: setting.start for key, setting in SETTINGS.items()}
    values.update(await _house(session))
    if tool is not None:
        values.update(own_values(tool))
    return values


async def value(session: AsyncSession, key: str, tool: MadeTool | None = None) -> int:
    """The value of the setting ``key`` (``KeyError`` for a key no setting has): ``tool``'s own,
    else the house's, else its start."""
    setting = SETTINGS[key]
    if tool is not None and key in PER_TOOL:
        own = own_values(tool).get(key)
        if own is not None:
            return own
    stored = await session.scalar(select(ToolSetting.value).where(ToolSetting.key == key))
    number = _whole(stored)
    return setting.start if number is None else number


async def set_house(session: AsyncSession, changes: Mapping[str, int | None]) -> dict[str, int]:
    """Set the house's settings (``None``: back to the start), all or none
    (``InvalidSetting``, before anything is written); every value after it. Flushes, never
    commits."""
    checked = check(changes, for_a_tool=False)
    for key, number in checked.items():
        if number is None:
            await session.execute(delete(ToolSetting).where(ToolSetting.key == key))
            continue
        upsert = insert(ToolSetting).values(key=key, value=number, set_at=func.clock_timestamp())
        await session.execute(
            upsert.on_conflict_do_update(
                index_elements=[ToolSetting.key],
                set_={"value": upsert.excluded.value, "set_at": upsert.excluded.set_at},
            )
        )
    await session.flush()
    return await all_values(session)


async def set_for_tool(
    session: AsyncSession, tool: MadeTool, changes: Mapping[str, int | None]
) -> dict[str, int]:
    """Set ``tool``'s own settings (``None``: back to the house's), all or none
    (``InvalidSetting``, before anything is written; only the keys of ``PER_TOOL``); every value
    for it after it. Takes the tool's row for the rest of the transaction; flushes, never
    commits."""
    checked = check(changes, for_a_tool=True)
    if not checked:
        return await all_values(session, tool)
    await session.refresh(tool, attribute_names=["settings"], with_for_update=True)
    kept: dict[str, object] = dict(cast(Mapping[str, object], tool.settings))
    for key, number in checked.items():
        if number is None:
            kept.pop(key, None)
        else:
            kept[key] = number
    tool.settings = kept
    await session.flush()
    return await all_values(session, tool)


def write_for_tool_from_build(tool: MadeTool, changes: Mapping[str, int | None]) -> None:
    """What a build's submission says of its tool's own bounds (``run_seconds``,
    ``launches_per_day``), written on ``tool`` as ``set_for_tool`` would (checked alike,
    ``InvalidSetting`` before anything is written); a key given ``None`` (the submission does not
    say) is left as it is. The model that writes a tool is Domo's own maker, never a program.
    The caller holds the tool's row (a new tool, or one locked by the build); nothing is
    flushed."""
    checked = check(changes, for_a_tool=True)
    kept: dict[str, object] = dict(cast(Mapping[str, object], tool.settings or {}))
    for key, number in checked.items():
        if number is not None:
            kept[key] = number
    tool.settings = kept
