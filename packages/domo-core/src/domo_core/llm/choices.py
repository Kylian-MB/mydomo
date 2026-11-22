"""The choices of Domo about models, as the code applies them at each call.

Domo keeps his choices in ``model_choices``, by scope: ``household`` for the whole house, and
``provider:<id>`` for a key that has its own (« économise sur ma clé », asked by the one who gave
it). For each kind of task he may choose a level (``TaskChoice.level``) or one model
(``TaskChoice.model``, a pinned model); he may also choose how much of the history a call is sent
(``history_chars``), how long an answer may be (``output_tokens``), when he warns about a limit
(``warn_at_percent``), how much of his memory a turn gives him (``memory_core_chars``,
``memory_recall_chars``: 0 gives nothing) and how long a conversation is quiet before he goes back
over it (``review_after_minutes``: 0 never, the household's alone), and where the trust of
someone he has no fact about starts (``trust_start_percent``: 0 to 100, the household's alone).

What a row holds is read as data: an unknown key is ignored and a value of the wrong kind is
dropped, as if it were not there. The levels chosen never lower the minimum of a protected task:
``router.wanted_level`` keeps it.

The choices of a key sit on top of the household's, field by field (``merged``): what a key chose
replaces what the household chose for the same field, and a field the key left unset keeps the
household's (a key that pins a model only keeps the household's level for when that model is
gone). Domo writes them with ``save_choices`` (``tools.model_tools``), which keeps who asked,
when and why; the bounds of the levers are applied where they are used (``conversation.prepare``).
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Final, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.llm.levels import LEVELS, Level
from domo_core.llm.models import ModelChoices, Provider
from domo_core.llm.tasks import TASKS, TaskKind
from domo_core.llm.types import JsonObject

HOUSEHOLD_SCOPE: Final = "household"
# The least the levers may say (the most is the house's: ``CoreSettings.max_history_chars`` and
# ``max_output_tokens``): fewer characters of history, or fewer tokens of answer, leave nothing
# useful.
MIN_HISTORY_CHARS: Final = 4000
MIN_OUTPUT_TOKENS: Final = 256
# How many characters of his memory a turn gives Domo, until he chooses otherwise (spec 6.4):
# what he always keeps in mind (the core), and what he remembers about the subject (the recall).
# He may lower either to 0 (nothing given), or raise it up to ``MAX_MEMORY_CHARS``.
START_MEMORY_CORE_CHARS: Final = 2000
START_MEMORY_RECALL_CHARS: Final = 3000
MAX_MEMORY_CHARS: Final = 20_000
# How many minutes without a message make a conversation quiet enough for Domo to go back over
# it (spec 6.6), until he chooses otherwise: 0 never, ``MAX_REVIEW_AFTER_MINUTES`` (a week) at most.
START_REVIEW_AFTER_MINUTES: Final = 30
MAX_REVIEW_AFTER_MINUTES: Final = 7 * 24 * 60
# Where the trust of someone Domo has no fact about starts, in percent (trust per person, spec
# 6.2), until he chooses otherwise: the benefit of the doubt. 0 to 100.
START_TRUST_PERCENT: Final = 70
MAX_TRUST_PERCENT: Final = 100
WRITE_NOW_VALUES: Final = ("requests", "done")
ARRIVAL_VALUES: Final = ("every_visit", "when_new", "never")
DEFAULT_WRITE_NOW: Final[tuple[str, ...]] = ("requests",)
DEFAULT_ARRIVAL: Final = "every_visit"
# The longest model name a choice may pin (as a key's list keeps them: ``provider_models``).
_MAX_MODEL_ID_LENGTH: Final = 200


def provider_scope(provider_id: str) -> str:
    """The scope of the choices of one key."""
    return f"provider:{provider_id}"


@dataclass(frozen=True)
class TaskChoice:
    """What Domo chose for a kind of task: a level, or a pinned model (``None``: nothing)."""

    level: Level | None = None
    model: str | None = None


def _no_tasks() -> Mapping[TaskKind, TaskChoice]:
    return MappingProxyType({})


@dataclass(frozen=True)
class Choices:
    """The choices of Domo that apply to a call: by kind of task, and the levers."""

    tasks: Mapping[TaskKind, TaskChoice] = field(default_factory=_no_tasks)
    history_chars: int | None = None
    output_tokens: int | None = None
    warn_at_percent: int | None = None
    # When Domo writes the texts of someone at once rather than later (``requests``: what they
    # ask for; ``done``: what he did for them), and when he writes to someone who opened their
    # page (``every_visit``, ``when_new``, ``never``).
    write_now: tuple[str, ...] = DEFAULT_WRITE_NOW
    arrival: str = DEFAULT_ARRIVAL
    # How many characters of the core and of the recall a turn is given (``None``: not chosen,
    # the start applies; 0: nothing).
    memory_core_chars: int | None = None
    memory_recall_chars: int | None = None
    # How many minutes without a message before he goes back over a conversation (``None``: not
    # chosen, the start applies; 0: never). The household's: a key has no say in it.
    review_after_minutes: int | None = None
    # Where the trust of someone with no fact starts, in percent (``None``: not chosen, the start
    # applies). The household's: a key has no say in it.
    trust_start_percent: int | None = None

    @property
    def memory_core_size(self) -> int:
        """The characters of the core a turn is given: the choice, else the start."""
        return START_MEMORY_CORE_CHARS if self.memory_core_chars is None else self.memory_core_chars

    @property
    def memory_recall_size(self) -> int:
        """The characters of the recall a turn is given: the choice, else the start."""
        if self.memory_recall_chars is None:
            return START_MEMORY_RECALL_CHARS
        return self.memory_recall_chars

    @property
    def review_minutes(self) -> int:
        """How many minutes without a message make a conversation quiet: the choice, else the
        start (0: never)."""
        if self.review_after_minutes is None:
            return START_REVIEW_AFTER_MINUTES
        return self.review_after_minutes

    @property
    def trust_start(self) -> int:
        """Where the trust of someone with no fact starts, in percent: the choice, else the
        start."""
        if self.trust_start_percent is None:
            return START_TRUST_PERCENT
        return self.trust_start_percent


NO_CHOICES: Final[Choices] = Choices()


def _level(value: object) -> Level | None:
    for level in LEVELS:
        if level == value:
            return level
    return None


def _model(value: object) -> str | None:
    if not isinstance(value, str) or not value or len(value) > _MAX_MODEL_ID_LENGTH:
        return None
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        return None
    return value


def _count(value: object, *, top: int | None = None) -> int | None:
    """A whole number above 0 (and at most ``top``), or ``None``."""
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        return None
    if top is not None and value > top:
        return None
    return value


def _memory_size(value: object) -> int | None:
    """A size of memory: a whole number from 0 (nothing given) to ``MAX_MEMORY_CHARS``, or
    ``None``. Not ``_count``: 0 is a choice here."""
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return None
    return value if value <= MAX_MEMORY_CHARS else None


def _review_minutes(value: object) -> int | None:
    """A number of minutes before a review: a whole number from 0 (never) to
    ``MAX_REVIEW_AFTER_MINUTES``, or ``None``. Not ``_count``: 0 is a choice here."""
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return None
    return value if value <= MAX_REVIEW_AFTER_MINUTES else None


def _trust_percent(value: object) -> int | None:
    """A start of trust: a whole number from 0 to ``MAX_TRUST_PERCENT``, or ``None``. Not
    ``_count``: 0 is a choice here."""
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return None
    return value if value <= MAX_TRUST_PERCENT else None


def _write_now(value: object) -> tuple[str, ...]:
    """The moments chosen (those known, in the order of ``WRITE_NOW_VALUES``, each once); the
    default when the value is no list or holds an unknown moment. An empty list is a choice."""
    if not isinstance(value, list):
        return DEFAULT_WRITE_NOW
    items = cast(list[object], value)
    if not all(isinstance(item, str) and item in WRITE_NOW_VALUES for item in items):
        return DEFAULT_WRITE_NOW
    return tuple(known for known in WRITE_NOW_VALUES if known in items)


def _arrival(value: object) -> str:
    return value if isinstance(value, str) and value in ARRIVAL_VALUES else DEFAULT_ARRIVAL


def _task_choices(value: object) -> Mapping[TaskKind, TaskChoice]:
    tasks: dict[TaskKind, TaskChoice] = {}
    given = cast(JsonObject, value) if isinstance(value, dict) else {}
    for task in TASKS:
        raw: object = given.get(task)
        if not isinstance(raw, dict):
            continue
        item = cast(JsonObject, raw)
        choice = TaskChoice(level=_level(item.get("level")), model=_model(item.get("model")))
        if choice != TaskChoice():
            tasks[task] = choice
    return MappingProxyType(tasks)


def choices_from_json(data: JsonObject) -> Choices:
    """The choices a row holds: unknown keys are ignored, values of the wrong kind dropped (and a
    row that holds no object is no choice)."""
    held = cast(object, data)
    if not isinstance(held, dict):
        return NO_CHOICES
    return Choices(
        tasks=_task_choices(data.get("tasks")),
        history_chars=_count(data.get("history_chars")),
        output_tokens=_count(data.get("output_tokens")),
        warn_at_percent=_count(data.get("warn_at_percent"), top=100),
        write_now=_write_now(data.get("write_now")),
        arrival=_arrival(data.get("arrival")),
        memory_core_chars=_memory_size(data.get("memory_core_chars")),
        memory_recall_chars=_memory_size(data.get("memory_recall_chars")),
        review_after_minutes=_review_minutes(data.get("review_after_minutes")),
        trust_start_percent=_trust_percent(data.get("trust_start_percent")),
    )


def choices_as_json(choices: Choices) -> JsonObject:
    """The choices as a row holds them (what is not chosen is left out)."""
    tasks: JsonObject = {}
    for task, choice in choices.tasks.items():
        item: JsonObject = {}
        if choice.level is not None:
            item["level"] = choice.level
        if choice.model is not None:
            item["model"] = choice.model
        if item:
            tasks[task] = item
    data: JsonObject = {"tasks": tasks}
    for name, value in (
        ("history_chars", choices.history_chars),
        ("output_tokens", choices.output_tokens),
        ("warn_at_percent", choices.warn_at_percent),
        ("memory_core_chars", choices.memory_core_chars),
        ("memory_recall_chars", choices.memory_recall_chars),
        ("review_after_minutes", choices.review_after_minutes),
        ("trust_start_percent", choices.trust_start_percent),
    ):
        if value is not None:
            data[name] = value
    if choices.write_now != DEFAULT_WRITE_NOW:
        data["write_now"] = list(choices.write_now)
    if choices.arrival != DEFAULT_ARRIVAL:
        data["arrival"] = choices.arrival
    return data


def _either[T](key: T | None, household: T | None) -> T | None:
    return household if key is None else key


def _task_merged(household: TaskChoice | None, key: TaskChoice | None) -> TaskChoice:
    """A kind of task's choice of a key on top of the household's, field by field."""
    below = household or TaskChoice()
    above = key or TaskChoice()
    return TaskChoice(
        level=_either(above.level, below.level), model=_either(above.model, below.model)
    )


def merged(household: Choices, key: Choices | None) -> Choices:
    """The household's choices with those of a key on top, field by field: for each kind of task,
    the level and the pinned model the key chose replace the household's, and what the key left
    unset keeps the household's; a lever the key set wins."""
    if key is None:
        return household
    named: list[TaskKind] = [*household.tasks]
    named.extend(task for task in key.tasks if task not in household.tasks)
    tasks: dict[TaskKind, TaskChoice] = {
        task: _task_merged(household.tasks.get(task), key.tasks.get(task)) for task in named
    }
    return Choices(
        tasks=MappingProxyType(tasks),
        history_chars=_either(key.history_chars, household.history_chars),
        output_tokens=_either(key.output_tokens, household.output_tokens),
        warn_at_percent=_either(key.warn_at_percent, household.warn_at_percent),
        memory_core_chars=_either(key.memory_core_chars, household.memory_core_chars),
        memory_recall_chars=_either(key.memory_recall_chars, household.memory_recall_chars),
        # How Domo writes, when he goes back over a conversation and where the trust of someone
        # starts are the house's: a key has no say in them.
        write_now=household.write_now,
        arrival=household.arrival,
        review_after_minutes=household.review_after_minutes,
        trust_start_percent=household.trust_start_percent,
    )


async def load_choices(session: AsyncSession, provider_id: str | None) -> Choices:
    """The choices that apply on the key ``provider_id`` (the household's alone for ``None``)."""
    scopes = [HOUSEHOLD_SCOPE]
    if provider_id is not None:
        scopes.append(provider_scope(provider_id))
    rows = await session.execute(
        select(ModelChoices.scope, ModelChoices.choices).where(ModelChoices.scope.in_(scopes))
    )
    found = {scope: choices_from_json(data) for scope, data in rows.all()}
    household = found.get(HOUSEHOLD_SCOPE, NO_CHOICES)
    if provider_id is None:
        return household
    return merged(household, found.get(provider_scope(provider_id)))


def bounded(chosen: int | None, *, least: int, most: int) -> int:
    """A lever as it applies: ``most`` when nothing is chosen, else the choice held between
    ``least`` and ``most`` (``most`` when the house's maximum is under ``least``)."""
    if chosen is None:
        return most
    return min(most, max(least, chosen))


async def save_choices(
    session: AsyncSession, scope: str, choices: Choices, *, by: UUID, why: str, now: datetime
) -> None:
    """Write ``choices`` as the choices of ``scope`` (``household`` or ``provider_scope(id)``),
    in place of the ones it had, with who asked for them (``by``), when (``now``) and why, in the
    caller's transaction (the caller commits)."""
    data = choices_as_json(choices)
    statement = insert(ModelChoices).values(
        scope=scope, choices=data, changed_by=by, changed_at=now, why=why
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[ModelChoices.scope],
            set_={"choices": data, "changed_by": by, "changed_at": now, "why": why},
        )
    )


async def warn_thresholds(session: AsyncSession) -> dict[str, int]:
    """The threshold Domo gave himself for the spending limits of each key, by key id: the
    key's own (``warn_at_percent`` of its scope), else the household's. A key with neither is left
    out."""
    rows = await session.execute(select(ModelChoices.scope, ModelChoices.choices))
    found = {scope: choices_from_json(data) for scope, data in rows.all()}
    household = found.get(HOUSEHOLD_SCOPE, NO_CHOICES)
    thresholds: dict[str, int] = {}
    for provider_id in (await session.execute(select(Provider.id))).scalars():
        threshold = merged(household, found.get(provider_scope(provider_id))).warn_at_percent
        if threshold is not None:
            thresholds[provider_id] = threshold
    return thresholds
