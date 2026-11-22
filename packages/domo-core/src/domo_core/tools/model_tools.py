"""The tools of Domo's own choices of models (spec 4.6, 4.9).

``models_choose`` (``models.choose``) changes Domo's choices (``llm.choices``): for the whole
household (``scope`` ``household``), or on the key of the request alone (``my_key``), which only
the one who gave that key may ask for (``Provider.given_by``, the person of the turn; anyone else
is refused, ``not_possible``: a call the household refuses, not a mistake of form). For a kind
of task, a level (``default`` takes the choice away) or a pinned model (``""`` takes it away); the
levers (how much of the conversation each call is sent, how long an answer may be, ``0`` takes
them away; ``conversation.prepare`` holds them in their bounds); the threshold at which Domo tells
the one who set a spending limit where it stands (``0`` takes it away); how many characters of his
memory a turn gives him, of the core and of the recall (``0`` gives nothing: these two are never
taken away, only set again); how many minutes without a message make a conversation quiet enough to
go back over it (``0`` never; the household's alone: ``my_key`` cannot set it); where the trust of
someone Domo has no fact about starts (``0`` to ``100``; the household's alone too). With
``failed_task`` and ``failed_reason``, Domo notes that a call of this request did that kind of
task badly: a lesson about the model the request took for it (``llm.lessons.record_failure``),
whose reason is one of a closed list (``lessons.FAILURE_REASONS``): lessons are shown to everyone,
so no words of anyone's go with them. It changes nothing in the household: the judge is not asked
(``catalog.CHANGES_NOTHING``); each change is written with who asked, when and why
(``model_choices``), and audited (``models.choices_changed``).

``models_step_up`` (``models.step_up``, everyone's, the judge not asked either) has the rest of
the request take a model one level stronger (``llm.picker.ModelPicker.step_up``,
``MAX_STEP_UPS`` at most), audited (``models.stepped_up``). Outside a request (a stored request
run after a yes) there is nothing to step up.

``models_set_level`` (``models.set_level``, the owner's alone, judged) is the owner's word on the
level of a model (``model_levels``): it counts for every choice, the protected tasks' included,
since their levels come only from the project's table and from that word (``llm.choice``). The
model is named as a key lists it; the key is the one that lists it (none: ``not_found``; several:
``ambiguous``). Audited (``models.level_set``).

``models_refresh`` (``models.refresh``, every member's, the judge not asked: it changes nothing in
the household) reads again, through the gatekeeper, the euro's rate and the catalog of models and
prices (``llm.refresh``: what cannot be read leaves the ones in force), and the models of the key
of the request (``ToolContext.model_lister``, ``llm.refresh.relist_key``; outside a request there
is none to list).

What Domo learns never lets a weaker model do a protected task: the levels he chooses never go
under a task's minimum (``router.wanted_level``), a pinned model under it is never taken
(``choice.pick_model``), and lessons only inform his choices.
"""

from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Final, Literal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.audit.log import append_audit
from domo_core.gatekeeper_client import new_http_client
from domo_core.llm.catalog_store import current_catalog
from domo_core.llm.choices import (
    HOUSEHOLD_SCOPE,
    MAX_MEMORY_CHARS,
    MAX_REVIEW_AFTER_MINUTES,
    MAX_TRUST_PERCENT,
    MIN_HISTORY_CHARS,
    MIN_OUTPUT_TOKENS,
    NO_CHOICES,
    Choices,
    TaskChoice,
    choices_as_json,
    choices_from_json,
    provider_scope,
    save_choices,
)
from domo_core.llm.lessons import FAILURE_REASONS, record_failure
from domo_core.llm.levels import LEVELS, Level
from domo_core.llm.models import ModelChoices, ModelLevel, Provider, ProviderModel
from domo_core.llm.picker import MAX_STEP_UPS
from domo_core.llm.refresh import refresh_catalog, refresh_rate, relist_key
from domo_core.llm.router import Route
from domo_core.llm.spending import amount_text, usd_per
from domo_core.llm.tasks import PROTECTED, TASKS, TaskKind
from domo_core.llm.types import JsonObject, ToolDefinition
from domo_core.policy.actions import DescribedAction
from domo_core.policy.roles import Actor
from domo_core.text import is_plain_line
from domo_core.tools.context import InvalidArguments, ToolContext, ToolOutcome

CHOOSE_ACTION_TYPE: Final = "models.choose"
STEP_UP_ACTION_TYPE: Final = "models.step_up"
SET_LEVEL_ACTION_TYPE: Final = "models.set_level"
REFRESH_ACTION_TYPE: Final = "models.refresh"

# The longest reason, model name and failure reason a call may give.
MAX_WHY_LENGTH: Final = 200
MAX_MODEL_LENGTH: Final = 200
# What a lever may say at most, whatever the house's maximum (a number of that size is no lever;
# the house's maximum applies where the levers are used: ``choices.bounded``).
_MAX_HISTORY_CHARS: Final = 10_000_000
_MAX_OUTPUT_TOKENS: Final = 1_000_000

Scope = Literal["household", "my_key"]
_SCOPES: Final[tuple[Scope, ...]] = ("household", "my_key")
# "default" takes the level chosen for a kind of task away.
_DEFAULT_LEVEL: Final = "default"
_LEVEL_CHOICES: Final[tuple[Level | Literal["default"], ...]] = (*LEVELS, "default")
_TASK_NAMES: Final[tuple[TaskKind, ...]] = tuple(TASKS)

_CHANGES: Final = (
    "task",
    "level",
    "model",
    "history_chars",
    "output_tokens",
    "warn_at_percent",
    "memory_core_chars",
    "memory_recall_chars",
    "review_after_minutes",
    "trust_start_percent",
)
_CHOOSE_ARGUMENTS: Final = frozenset({"why", "scope", *_CHANGES, "failed_task", "failed_reason"})

_NOT_YOUR_KEY: Final = (
    "my_key is only for a request on the person's own key, and this request is on someone "
    "else's: choose for the household instead, or not at all"
)
_REVIEW_IS_THE_HOUSEHOLDS: Final = (
    "review_after_minutes is the household's: choose it with scope household"
)
_TRUST_START_IS_THE_HOUSEHOLDS: Final = (
    "trust_start_percent is the household's: choose it with scope household"
)
_NO_CALL: Final = (
    "this request made no call of that kind of task: there is nothing to note about its model"
)
_PROTECTED_NOTE: Final = (
    "the yes judge, the reading of a yes and the writing of rights never take a model under "
    "medium: for them a model's level comes only from the project's table and from "
    "models_set_level, so a weaker model is never used, whatever is chosen"
)


# --- the arguments ---------------------------------------------------------------------------


def _text(arguments: JsonObject, name: str, *, longest: int, empty_ok: bool = False) -> str:
    """The text ``name`` of the call: one plain line of ``longest`` characters at most."""
    value = arguments.get(name)
    if not isinstance(value, str):
        raise InvalidArguments("invalid", f"{name} must be a text")
    if len(value) > longest:
        raise InvalidArguments("too_long", f"{name} has more than {longest} characters")
    if (not value.strip() and not (empty_ok and value == "")) or not is_plain_line(value):
        raise InvalidArguments("invalid", f"{name} must be one plain line of text")
    return value


def _whole(arguments: JsonObject, name: str, *, least: int, most: int) -> int | None:
    """The whole number ``name`` of the call, if given: 0 (taken away) or ``least`` to
    ``most``."""
    if name not in arguments:
        return None
    value = arguments[name]
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidArguments("invalid", f"{name} must be a whole number")
    if value != 0 and not least <= value <= most:
        raise InvalidArguments("invalid", f"{name} is 0 (to take it away) or {least} to {most}")
    return value


def _memory_size(arguments: JsonObject, name: str) -> int | None:
    """The size of memory ``name`` of the call, if given: 0 (nothing given) to
    ``MAX_MEMORY_CHARS``. Not ``_whole``: 0 is a size here, not a lever taken away."""
    if name not in arguments:
        return None
    value = arguments[name]
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidArguments("invalid", f"{name} must be a whole number")
    if not 0 <= value <= MAX_MEMORY_CHARS:
        raise InvalidArguments("invalid", f"{name} is 0 (nothing) to {MAX_MEMORY_CHARS}")
    return value


def _review_minutes(arguments: JsonObject) -> int | None:
    """The minutes before a review of the call, if given: 0 (never) to
    ``MAX_REVIEW_AFTER_MINUTES``. Not ``_whole``: 0 is a choice here, not a lever taken away."""
    name = "review_after_minutes"
    if name not in arguments:
        return None
    value = arguments[name]
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidArguments("invalid", f"{name} must be a whole number")
    if not 0 <= value <= MAX_REVIEW_AFTER_MINUTES:
        raise InvalidArguments("invalid", f"{name} is 0 (never) to {MAX_REVIEW_AFTER_MINUTES}")
    return value


def _trust_percent(arguments: JsonObject) -> int | None:
    """Where the trust of someone with no fact starts, if given: 0 to ``MAX_TRUST_PERCENT``. Not
    ``_whole``: 0 is a choice here, not a lever taken away."""
    name = "trust_start_percent"
    if name not in arguments:
        return None
    value = arguments[name]
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidArguments("invalid", f"{name} must be a whole number")
    if not 0 <= value <= MAX_TRUST_PERCENT:
        raise InvalidArguments("invalid", f"{name} is 0 to {MAX_TRUST_PERCENT}")
    return value


def _checked(arguments: JsonObject, name: str, values: tuple[str, ...]) -> object:
    """The value of ``name`` if it is given (``None`` otherwise): one of ``values``."""
    value: object = arguments.get(name)
    if name in arguments and not any(known == value for known in values):
        raise InvalidArguments("invalid", f"{name} must be one of: {', '.join(values)}")
    return value


def _scope_named(arguments: JsonObject) -> Scope:
    value = _checked(arguments, "scope", _SCOPES)
    for scope in _SCOPES:
        if scope == value:
            return scope
    return "household"


def _task_named(arguments: JsonObject, name: str) -> TaskKind | None:
    value = _checked(arguments, name, _TASK_NAMES)
    for task in _TASK_NAMES:
        if task == value:
            return task
    return None


def _reason_named(arguments: JsonObject) -> str | None:
    """A failure's reason, one of ``lessons.FAILURE_REASONS``: never words of Domo's own."""
    value = _checked(arguments, "failed_reason", FAILURE_REASONS)
    return next((reason for reason in FAILURE_REASONS if reason == value), None)


def _level_named(arguments: JsonObject) -> Level | Literal["default"] | None:
    value = _checked(arguments, "level", _LEVEL_CHOICES)
    for level in _LEVEL_CHOICES:
        if level == value:
            return level
    return None


@dataclass(frozen=True)
class _Choice:
    """A ``models_choose`` call, read."""

    why: str
    scope: Scope
    task: TaskKind | None
    level: Level | Literal["default"] | None
    model: str | None
    history_chars: int | None
    output_tokens: int | None
    warn_at_percent: int | None
    memory_core_chars: int | None
    memory_recall_chars: int | None
    review_after_minutes: int | None
    trust_start_percent: int | None
    failed_task: TaskKind | None
    failed_reason: str | None

    def changes(self) -> JsonObject:
        """What the call changes, as it asked it (the audit's ``changes``)."""
        given: dict[str, Any] = {
            "task": self.task,
            "level": self.level,
            "model": self.model,
            "history_chars": self.history_chars,
            "output_tokens": self.output_tokens,
            "warn_at_percent": self.warn_at_percent,
            "memory_core_chars": self.memory_core_chars,
            "memory_recall_chars": self.memory_recall_chars,
            "review_after_minutes": self.review_after_minutes,
            "trust_start_percent": self.trust_start_percent,
        }
        return {name: value for name, value in given.items() if value is not None}

    def applied_to(self, choices: Choices) -> Choices:
        """``choices`` with this call's changes."""
        tasks = dict(choices.tasks)
        if self.task is not None:
            chosen = tasks.get(self.task, TaskChoice())
            if self.level is not None:
                level = None if self.level == _DEFAULT_LEVEL else self.level
                chosen = replace(chosen, level=level)
            if self.model is not None:
                chosen = replace(chosen, model=self.model or None)
            if chosen == TaskChoice():
                tasks.pop(self.task, None)
            else:
                tasks[self.task] = chosen

        def lever(value: int | None, before: int | None) -> int | None:
            return before if value is None else (value or None)

        def size(value: int | None, before: int | None) -> int | None:
            # 0 is a size of memory (nothing given), kept as it is.
            return before if value is None else value

        return replace(
            choices,
            tasks=MappingProxyType(tasks),
            history_chars=lever(self.history_chars, choices.history_chars),
            output_tokens=lever(self.output_tokens, choices.output_tokens),
            warn_at_percent=lever(self.warn_at_percent, choices.warn_at_percent),
            memory_core_chars=size(self.memory_core_chars, choices.memory_core_chars),
            memory_recall_chars=size(self.memory_recall_chars, choices.memory_recall_chars),
            review_after_minutes=size(self.review_after_minutes, choices.review_after_minutes),
            trust_start_percent=size(self.trust_start_percent, choices.trust_start_percent),
        )


def _choice_of(arguments: JsonObject) -> _Choice:
    """A ``models_choose`` call, checked: what it changes needs no database. Raises
    ``InvalidArguments``."""
    unknown = set(arguments) - _CHOOSE_ARGUMENTS
    if unknown:
        raise InvalidArguments("invalid", "models_choose was given an argument it does not take")
    why = _text(arguments, "why", longest=MAX_WHY_LENGTH).strip()
    scope = _scope_named(arguments)
    task = _task_named(arguments, "task")
    level = _level_named(arguments)
    model = (
        _text(arguments, "model", longest=MAX_MODEL_LENGTH, empty_ok=True)
        if "model" in arguments
        else None
    )
    if model is not None and model != model.strip():
        raise InvalidArguments("invalid", "model is a name as its key lists it, with no spaces")
    if (task is None) != (level is None and model is None):
        raise InvalidArguments("invalid", "task goes with a level or a model, and they with it")
    failed_task = _task_named(arguments, "failed_task")
    failed_reason = _reason_named(arguments)
    if (failed_task is None) != (failed_reason is None):
        raise InvalidArguments("invalid", "failed_task and failed_reason go together")
    choice = _Choice(
        why=why,
        scope=scope,
        task=task,
        level=level,
        model=model,
        history_chars=_whole(
            arguments, "history_chars", least=MIN_HISTORY_CHARS, most=_MAX_HISTORY_CHARS
        ),
        output_tokens=_whole(
            arguments, "output_tokens", least=MIN_OUTPUT_TOKENS, most=_MAX_OUTPUT_TOKENS
        ),
        warn_at_percent=_whole(arguments, "warn_at_percent", least=1, most=100),
        memory_core_chars=_memory_size(arguments, "memory_core_chars"),
        memory_recall_chars=_memory_size(arguments, "memory_recall_chars"),
        review_after_minutes=_review_minutes(arguments),
        trust_start_percent=_trust_percent(arguments),
        failed_task=failed_task,
        failed_reason=failed_reason,
    )
    if not choice.changes() and choice.failed_task is None:
        raise InvalidArguments("invalid", "models_choose was asked to change nothing")
    if choice.scope == "my_key" and choice.review_after_minutes is not None:
        raise InvalidArguments("invalid", _REVIEW_IS_THE_HOUSEHOLDS)
    if choice.scope == "my_key" and choice.trust_start_percent is not None:
        raise InvalidArguments("invalid", _TRUST_START_IS_THE_HOUSEHOLDS)
    return choice


def _scope_of(choice: _Choice, ctx: ToolContext) -> str:
    """Where the choice is written: the household, or the key of the request when it is the
    person's own (outside a turn, a stored request names it: its described target)."""
    if choice.scope == "household":
        return HOUSEHOLD_SCOPE
    if ctx.models is not None:
        if ctx.models.provider.given_by != ctx.person.id:
            # A right call that the household refuses: no mistake of the model's form.
            raise InvalidArguments("not_possible", _NOT_YOUR_KEY)
        return provider_scope(ctx.models.provider.id)
    target = None if ctx.described_action is None else ctx.described_action.target
    if target is not None and target.startswith(provider_scope("")):
        return target
    raise InvalidArguments("not_possible", "my_key is only for a request on the person's own key")


def _failed_route(choice: _Choice, ctx: ToolContext) -> Route | None:
    """The route the request took for the kind of task that failed, if a failure is noted."""
    if choice.failed_task is None:
        return None
    route = None if ctx.models is None else ctx.models.route_of(choice.failed_task)
    if route is None:
        raise InvalidArguments("not_found", _NO_CALL)
    return route


async def _still_theirs(session: AsyncSession, scope: str, ctx: ToolContext) -> bool:
    """Whether the key a choice of ``my_key`` is written for is still the person's."""
    if scope == HOUSEHOLD_SCOPE:
        return True
    key = await session.get(Provider, scope.removeprefix(provider_scope("")))
    return key is not None and key.given_by == ctx.person.id


def _why_of(arguments: JsonObject) -> str:
    if set(arguments) - {"why"}:
        raise InvalidArguments("invalid", "models_step_up takes only why")
    return _text(arguments, "why", longest=MAX_WHY_LENGTH).strip()


def _level_request(arguments: JsonObject) -> tuple[str, Level]:
    if set(arguments) - {"model", "level"}:
        raise InvalidArguments("invalid", "models_set_level takes a model and a level")
    model = _text(arguments, "model", longest=MAX_MODEL_LENGTH)
    value: object = arguments.get("level")
    for level in LEVELS:
        if level == value:
            return model, level
    raise InvalidArguments("invalid", "level must be one of: small, medium, strong")


async def _keys_listing(ctx: ToolContext, model: str) -> list[str]:
    """The keys that list ``model``, by id."""
    async with ctx.session_factory() as session:
        found = await session.execute(
            select(ProviderModel.provider_id)
            .where(ProviderModel.model_id == model)
            .order_by(ProviderModel.provider_id)
        )
        return list(found.scalars().all())


def _the_one(keys: list[str]) -> str:
    if not keys:
        raise InvalidArguments("not_found", "no key lists a model of that name")
    if len(keys) > 1:
        raise InvalidArguments("ambiguous", "several keys list a model of that name")
    return keys[0]


# --- the tools -------------------------------------------------------------------------------

_TASK_SCHEMA: Final[JsonObject] = {"type": "string", "enum": list(_TASK_NAMES)}


class ModelsChoose:
    """Change Domo's own choices of models, or note that a call did its task badly."""

    definition = ToolDefinition(
        name="models_choose",
        description=(
            "Change your own choices of models: for the whole household (scope household), or "
            "only on the key of this request when it is the person's own (scope my_key). For a "
            "kind of task, a level (small, medium, strong; default gives back the task's usual "
            "start) or one model to take whenever its key lists it (an empty model takes it away; "
            "when it is gone, the level is used). The levers: how much of the conversation each "
            "call is sent (history_chars) and how long your answers may be (output_tokens); how "
            "much of your memory each turn gives you, in characters: what you always keep in mind "
            "(memory_core_chars) and what you remember about the subject (memory_recall_chars), 0 "
            "for nothing. How many minutes without a message make a conversation quiet enough for "
            "you to go back over it and sort what you remember (review_after_minutes, 0 for "
            "never; the household's alone). Where your trust in someone you have no fact about "
            "starts, in percent (trust_start_percent; the household's alone). And when you tell "
            "the person who set a spending limit where it stands (warn_at_percent). It changes "
            "nothing in the household: no yes is needed, and each change is written down with "
            "why. Judge a request about models as any other: you may do it, say no, or propose "
            "something else, and talk it over. To "
            "spend less: cheaper or local models where the task allows, less history when it is "
            "enough, shorter answers, and no step up when it is not needed. What you know of "
            "spending is an estimate, not the real bill: say so. With failed_task and "
            "failed_reason you note that a call of this request did that kind of task badly (a "
            "mistake the person points out, or one you see), with the reason of the list that "
            "fits best: it becomes a lesson about that model. The yes judge, the reading of a yes "
            "and the writing of rights never take a model under medium, whatever you choose."
        ),
        parameters={
            "type": "object",
            "properties": {
                "why": {
                    "type": "string",
                    "maxLength": MAX_WHY_LENGTH,
                    "description": "Why, in a few words (written down with the change).",
                },
                "scope": {
                    "type": "string",
                    "enum": list(_SCOPES),
                    "description": (
                        "household (the default), or my_key: the key of this request, only "
                        "when it is the person's own."
                    ),
                },
                "task": {
                    **_TASK_SCHEMA,
                    "description": "The kind of task the level or the model is for.",
                },
                "level": {
                    "type": "string",
                    "enum": list(_LEVEL_CHOICES),
                    "description": "The level for that kind of task; default takes it away.",
                },
                "model": {
                    "type": "string",
                    "maxLength": MAX_MODEL_LENGTH,
                    "description": (
                        "A model to take for that kind of task, named as its key lists it; "
                        "an empty text takes it away."
                    ),
                },
                "history_chars": {
                    "type": "integer",
                    "minimum": 0,
                    "description": (
                        f"How many characters of the conversation each call is sent: "
                        f"{MIN_HISTORY_CHARS} at least, more than the house's maximum counts "
                        "as the maximum; 0 takes it away."
                    ),
                },
                "output_tokens": {
                    "type": "integer",
                    "minimum": 0,
                    "description": (
                        f"How many tokens an answer may have: {MIN_OUTPUT_TOKENS} at least, "
                        "more than the house's maximum counts as the maximum; 0 takes it away."
                    ),
                },
                "warn_at_percent": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 100,
                    "description": (
                        "The share of a spending limit at which you tell the person who set it, "
                        "once per period; 0 takes it away."
                    ),
                },
                "memory_core_chars": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": MAX_MEMORY_CHARS,
                    "description": (
                        "How many characters of what you always keep in mind each turn gives "
                        "you; 0 gives nothing."
                    ),
                },
                "memory_recall_chars": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": MAX_MEMORY_CHARS,
                    "description": (
                        "How many characters of what you remember about the subject each turn "
                        "gives you; 0 gives nothing."
                    ),
                },
                "review_after_minutes": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": MAX_REVIEW_AFTER_MINUTES,
                    "description": (
                        "How many minutes without a message before you go back over a "
                        "conversation; 0 never. Only with scope household."
                    ),
                },
                "trust_start_percent": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": MAX_TRUST_PERCENT,
                    "description": (
                        "Where your trust in someone you have no fact about starts, in percent: "
                        "the benefit of the doubt. Only with scope household."
                    ),
                },
                "failed_task": {
                    **_TASK_SCHEMA,
                    "description": "The kind of task a call of this request did badly.",
                },
                "failed_reason": {
                    "type": "string",
                    "enum": list(FAILURE_REASONS),
                    "description": "What went wrong with the model.",
                },
            },
            "required": ["why"],
            "additionalProperties": False,
        },
    )
    action_type = CHOOSE_ACTION_TYPE

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        choice = _choice_of(arguments)
        scope = _scope_of(choice, ctx)
        _failed_route(choice, ctx)
        return DescribedAction(
            connector="core",
            action_type=CHOOSE_ACTION_TYPE,
            target=scope,
            effect="self_profile",
            destination="household",
            facts={"own_key": choice.scope == "my_key"},
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        choice = _choice_of(arguments)
        scope = _scope_of(choice, ctx)
        route = _failed_route(choice, ctx)
        changes = choice.changes()
        result: JsonObject = {"scope": choice.scope}
        async with ctx.session_factory() as session:
            if not await _still_theirs(session, scope, ctx):
                return ToolOutcome(
                    status="failed",
                    model_result={"reason": "not_your_key", "detail": _NOT_YOUR_KEY},
                    card={"key": "action.card.failed.models.choose", "params": {}},
                )
            if changes:
                row = await session.get(ModelChoices, scope)
                before = NO_CHOICES if row is None else choices_from_json(row.choices)
                now = choice.applied_to(before)
                await save_choices(
                    session, scope, now, by=ctx.person.id, why=choice.why, now=ctx.clock()
                )
                result["choices"] = choices_as_json(now)
            if route is not None and choice.failed_reason is not None:
                await record_failure(
                    session,
                    provider_kind=route.provider.kind,
                    model_id=route.model_id,
                    task=route.task,
                    reason=choice.failed_reason,
                    now=ctx.clock(),
                )
                result["lesson"] = {"model": route.model_id, "task": route.task}
            if changes:
                # Last, just before the commit: the audit log holds a lock until it ends.
                await append_audit(
                    session,
                    actor=f"person:{ctx.person.id}",
                    action="models.choices_changed",
                    details={"scope": scope, "changes": changes, "why": choice.why},
                )
            await session.commit()
        if choice.task in PROTECTED and (choice.model or choice.level == "small"):
            result["note"] = _PROTECTED_NOTE
        key = (
            "action.card.done.models.choose" if changes else "action.card.done.models.choose.lesson"
        )
        return ToolOutcome(status="done", model_result=result, card={"key": key, "params": {}})


class ModelsStepUp:
    """Take a stronger model for the rest of the request."""

    definition = ToolDefinition(
        name="models_step_up",
        description=(
            "Take a model one level stronger for the rest of this request, when it deserves "
            f"it (a hard or important question): {MAX_STEP_UPS} steps at most in a request, "
            "and never past a spending limit. Only when it is needed: a stronger model costs "
            "more. The result says the level you have now, or why there is none above."
        ),
        parameters={
            "type": "object",
            "properties": {
                "why": {
                    "type": "string",
                    "maxLength": MAX_WHY_LENGTH,
                    "description": "Why this request deserves more, in a few words.",
                }
            },
            "required": ["why"],
            "additionalProperties": False,
        },
    )
    action_type = STEP_UP_ACTION_TYPE

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        _why_of(arguments)
        return DescribedAction(
            connector="core",
            action_type=STEP_UP_ACTION_TYPE,
            target=None,
            effect="self_profile",
            destination="household",
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        why = _why_of(arguments)
        models = ctx.models
        level = None if models is None else models.step_up()
        if models is None or level is None:
            if models is None:
                reason = "no_request"
                detail = "there is no request in progress to step up"
            elif models.step_ups >= MAX_STEP_UPS:
                reason = "no_more_steps"
                detail = f"this request took its {MAX_STEP_UPS} steps up already"
            else:
                reason = "at_the_top"
                detail = "the key of this request has no stronger model for it: nothing changed"
            return ToolOutcome(
                status="failed",
                model_result={"reason": reason, "detail": detail},
                card={"key": "action.card.failed.models.step_up", "params": {}},
            )
        async with ctx.session_factory() as session:
            await append_audit(
                session,
                actor=f"person:{ctx.person.id}",
                action="models.stepped_up",
                details={"level": level, "why": why},
            )
            await session.commit()
        return ToolOutcome(
            status="done",
            model_result={"level": level, "steps_left": MAX_STEP_UPS - models.step_ups},
            card={"key": "action.card.done.models.step_up", "params": {}},
        )


class ModelsSetLevel:
    """Say what level a model of a key is."""

    definition = ToolDefinition(
        name="models_set_level",
        description=(
            "Say what level a model is (small, medium or strong), when its level is wrong or "
            "unknown: it counts for every choice of a model, the yes judge's, the reading of a "
            "yes and the writing of rights included, which take a model of medium or more. The "
            "model is named as its key lists it; when several keys list that name, it is not "
            "done (ambiguous)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "model": {
                    "type": "string",
                    "maxLength": MAX_MODEL_LENGTH,
                    "description": "The model, named as its key lists it.",
                },
                "level": {
                    "type": "string",
                    "enum": list(LEVELS),
                    "description": "Its level.",
                },
            },
            "required": ["model", "level"],
            "additionalProperties": False,
        },
    )
    action_type = SET_LEVEL_ACTION_TYPE

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        model, _ = _level_request(arguments)
        provider_id = _the_one(await _keys_listing(ctx, model))
        return DescribedAction(
            connector="core",
            action_type=SET_LEVEL_ACTION_TYPE,
            target=provider_scope(provider_id),
            effect="permissions",
            destination="household",
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        model, level = _level_request(arguments)
        keys = await _keys_listing(ctx, model)
        described = ctx.described_action
        if described is None:
            provider_id = _the_one(keys)
        else:
            # A request said yes to: its key, if it still lists the model.
            provider_id = (described.target or "").removeprefix(provider_scope(""))
            if provider_id not in keys:
                return ToolOutcome(
                    status="failed",
                    model_result={
                        "reason": "not_listed",
                        "detail": "the key no longer lists that model: nothing was done",
                    },
                    card={"key": "action.card.failed.models.set_level", "params": {}},
                )
        now = ctx.clock()
        async with ctx.session_factory() as session:
            statement = insert(ModelLevel).values(
                provider_id=provider_id,
                model_id=model,
                level=level,
                set_by=ctx.person.id,
                set_at=now,
            )
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[ModelLevel.provider_id, ModelLevel.model_id],
                    set_={"level": level, "set_by": ctx.person.id, "set_at": now},
                )
            )
            # Last, just before the commit.
            await append_audit(
                session,
                actor=f"person:{ctx.person.id}",
                action="models.level_set",
                details={"provider_id": provider_id, "model": model, "level": level},
            )
            await session.commit()
        return ToolOutcome(
            status="done",
            model_result={"model": model, "level": level},
            card={"key": f"action.card.done.models.set_level.{level}", "params": {"model": model}},
        )


class ModelsRefresh:
    """Read the catalog of models, the euro's rate and the models of the request's key again."""

    definition = ToolDefinition(
        name="models_refresh",
        description=(
            "Read again the catalog of models and prices (models.dev), the euro's rate (the "
            "European Central Bank) and the models the key of this request opens, when they may "
            "be out of date: a model the person names is not listed, a provider changed its "
            "prices, the rate seems old. It changes nothing in the household. The result says "
            "what was read again (refreshed) and what stayed as it was (kept): what could not be "
            "read is left as it was."
        ),
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
    )
    action_type = REFRESH_ACTION_TYPE

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        if arguments:
            raise InvalidArguments("invalid", "models_refresh takes no argument")
        return DescribedAction(
            connector="core",
            action_type=REFRESH_ACTION_TYPE,
            target=None,
            effect="read",
            destination="household",
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        if arguments:
            raise InvalidArguments("invalid", "models_refresh takes no argument")
        now = ctx.clock()
        async with new_http_client() as http, ctx.session_factory() as session:
            # The rate first: the catalog read next carries it.
            rate = await refresh_rate(session, ctx.gatekeeper, now=now, http=http)
            catalog = await refresh_catalog(session, ctx.gatekeeper, now=now, http=http)
            in_force = catalog or await current_catalog(session)
            usd_per_eur = await usd_per(session, "EUR", in_force)
            await session.commit()
        result: JsonObject = {
            "catalog": "kept" if catalog is None else "refreshed",
            "models": sum(len(models) for models in in_force.entries.values()),
            "rate": "kept" if rate is None else "refreshed",
            "usd_per_eur": amount_text(usd_per_eur),
        }
        listed = None
        if ctx.models is not None and ctx.model_lister is not None:
            async with ctx.session_factory() as session:
                key = await session.get(Provider, ctx.models.provider.id)
                await session.commit()
                if key is not None:
                    listed = await relist_key(
                        session, ctx.gatekeeper, key, ctx.model_lister, now=now
                    )
            result["key_list"] = "kept" if listed is None else "refreshed"
            if listed is not None:
                result["key_models"] = len(listed)
        else:
            result["key_list"] = "no_request"
        if catalog is None and rate is None and listed is None:
            return ToolOutcome(
                status="failed",
                model_result={**result, "reason": "nothing_read"},
                card={"key": "action.card.failed.models.refresh", "params": {}},
            )
        return ToolOutcome(
            status="done",
            model_result=result,
            card={"key": "action.card.done.models.refresh", "params": {}},
        )
