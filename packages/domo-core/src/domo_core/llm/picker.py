"""The models of one turn: each call to a model takes its route on the key of the request, and is
counted on Domo's account.

A turn (``conversation.turn``) makes a ``ModelPicker`` on the key it chose (``router.choose_key``)
and asks it for a model before each call: the conversation's own (``conversation``, at each round of
tools), the yes judge's, the reading of a yes in words and the writing of rights (``model_for``).
Every call of a request stays on that key; each kind of task takes its route there
(``router.choose_route``), so the protected ones never take a model under ``medium``: without one,
``model_for`` gives ``None`` and the caller does as when the model does not answer. A call may be
asked at least a level (``model_for``'s ``at_least``): a text a smaller model could not write is
asked of a stronger one (``texts.writer``, whose ``TextModels`` a picker is).

Every model it gives is a ``metered.MeteredModel``: each call is reserved before it is made and
settled after it, for the person of the turn (``person_id``), at the time of ``clock``; a call that
would pass a firm limit of the key is not made (``ModelCallError("limit_reached")``, which the
callers take as any failure of the model). ``cost_usd`` and ``estimated`` add up what the calls of
the turn cost.

For the choice, the picker keeps only what it is made from (the catalog in force, read once unless
it is given, and the level the main calls stepped up to), never the key itself (the key it was
given only names the request's key: ``provider``). Before each call it reads the key again, in a
short transaction of its own, so that a key removed during the turn leaves the next call without a
route (``None``; ``conversation`` raises ``ModelCallError("no_model")``).

``main_task`` is the kind of task of the turn's main calls (``conversation``, and the route the
reply says it was written with, ``conversation_route``): ``conversation`` unless the turn is
something else. ``min_context`` is the size of the turn's request (its system text and its
history, in estimated tokens; the turn sets it once it has built the text it sends): a model
whose known context is smaller is left out of the main calls (the other calls are not sent the
history).

Going up a level, for the rest of the turn
------------------------------------------

The main calls go up one level at a time, for one of two reasons, each counted apart:

* Domo judges that the request deserves a stronger model (``step_up``, the tool
  ``models_step_up``): ``MAX_STEP_UPS`` steps at most in a turn;
* the code saw a round whose every tool call could not be acted on (``retry_up``, from
  ``chat.agent.run_turn``): the next round is the new try, ``MAX_RETRIES_UP`` at most. Each such
  round is a lesson about the model that made it (``llm.lessons.INVALID_TOOL_CALL``), written at
  the next call (``write_lessons``), whether the turn could still go up or not.

A step starts from the level of the model the last main call took (before any, from the level it
would ask for), and goes to the next level the key has a model of for the main task
(``Route.fit_levels``): from then on, the main calls take at least that level (``choose_route``'s
``at_least``), and a pinned model under it is left. When the key has no model above, there is no
step (``step_up`` gives ``None``, ``retry_up`` ``False``): a step never claims a stronger model it
would not take. Nor does it pass a firm limit: each call is still reserved (``metered``). The
choices themselves are read again at each call, so a change Domo makes during the turn
(``models_choose``) holds from its next call on, never under the level stepped up to.

``note_failure`` records at once that the last call of a kind of task the turn made did it badly
(a yes judge whose answer is no verdict: ``confirmations.judge``); ``route_of`` tells which model
that was (``models_choose``, when Domo notes a failure himself).
"""

import logging
from collections.abc import Callable, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Final, Protocol
from uuid import UUID

import anyio

from domo_core.db import SessionFactory
from domo_core.llm.catalog import Catalog
from domo_core.llm.catalog_store import current_catalog
from domo_core.llm.choices import NO_CHOICES
from domo_core.llm.factory import ModelFactory
from domo_core.llm.lessons import INVALID_TOOL_CALL, record_failure
from domo_core.llm.levels import LEVELS, Level, level_rank
from domo_core.llm.metered import MeteredModel
from domo_core.llm.models import Provider
from domo_core.llm.router import Route, choose_route, wanted_level
from domo_core.llm.tasks import TaskKind
from domo_core.llm.types import ChatModel, ModelCallError

logger = logging.getLogger(__name__)

# How many times a turn may go up a level: on Domo's judgement, and for a new try.
MAX_STEP_UPS: Final = 2
MAX_RETRIES_UP: Final = 2
# How long writing lessons may take: they are never worth holding a call for.
_LESSON_SECONDS: Final = 5.0


def _higher(first: Level | None, second: Level | None) -> Level | None:
    """The higher of two levels, either of which may be none."""
    if first is None or second is None:
        return second if first is None else first
    return first if level_rank(first) >= level_rank(second) else second


class TurnModels(Protocol):
    """What a turn asks for its model, before each round (``chat.agent.run_turn``)."""

    async def conversation(self) -> ChatModel:
        """The model of the next call of the conversation; ``ModelCallError`` when there is
        none."""
        ...

    def retry_up(self) -> bool:
        """The last round's calls could not be acted on: the next round is a new try, one level
        up when the turn may still go up (``True`` then)."""
        ...


class ModelPicker:
    """The models of one turn, on the key ``provider`` (see the module docstring). It is a
    ``TurnModels``."""

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        model_factory: ModelFactory,
        provider: Provider,
        person_id: UUID | None,
        clock: Callable[[], datetime],
        main_task: TaskKind = "conversation",
        min_context: int = 0,
        catalog: Catalog | None = None,
        notify: Callable[[UUID], None] | None = None,
        tool_id: UUID | None = None,
        build_id: UUID | None = None,
    ) -> None:
        self._session_factory = session_factory
        # The tool and the build its calls are made for, when it builds one (``MeteredModel``).
        self._tool_id = tool_id
        self._build_id = build_id
        # Told whom a call kept news for (``metered.MeteredModel``).
        self._notify = notify
        self._model_factory = model_factory
        self._provider = provider
        self._provider_id = provider.id
        self._person_id = person_id
        self._clock = clock
        self._main_task: TaskKind = main_task
        self._min_context = min_context
        # What the choice is made from, kept for the turn.
        self._catalog: Catalog | None = catalog
        # The steps up of the main calls: on Domo's judgement, and for a new try.
        self._step_ups = 0
        self._retries_up = 0
        # The level the main calls take at least, once a step was taken, and the level they are
        # at now (of the model the last one took, or of the last step).
        self._floor: Level | None = None
        self._at: Level | None = None
        # The route each kind of task took last.
        self._routes: dict[TaskKind, Route] = {}
        self._conversation_route: Route | None = None
        # The lessons to write at the next call (``retry_up``).
        self._lessons_due: list[tuple[Route, str]] = []
        # The models given, each counting its calls.
        self._given: list[MeteredModel] = []

    @property
    def provider(self) -> Provider:
        """The key of the request, as the turn was given it."""
        return self._provider

    @property
    def conversation_route(self) -> Route | None:
        """The route of the last main call that found one (``None`` before, or when none did)."""
        return self._conversation_route

    @property
    def step_ups(self) -> int:
        """How many steps up Domo took in this turn (``step_up``)."""
        return self._step_ups

    def route_of(self, task: TaskKind) -> Route | None:
        """The route of the last call of ``task`` the turn made (``None`` when it made none)."""
        return self._routes.get(task)

    @property
    def min_context(self) -> int:
        """The size of the request of the main calls, in estimated tokens."""
        return self._min_context

    @min_context.setter
    def min_context(self, tokens: int) -> None:
        self._min_context = tokens

    @property
    def cost_usd(self) -> Decimal:
        """What the calls of the turn cost so far, in dollars."""
        return sum((model.cost_usd for model in self._given), Decimal(0))

    @property
    def estimated(self) -> bool:
        """Whether the tokens of one of the turn's calls were estimated (the provider reported
        none)."""
        return any(model.estimated for model in self._given)

    async def _route(self, task: TaskKind, at_least: Level | None) -> tuple[Route, Catalog] | None:
        async with self._session_factory() as session:
            provider = await session.get(Provider, self._provider_id)
            if provider is None:
                return None
            if self._catalog is None:
                self._catalog = await current_catalog(session)
            catalog = self._catalog
            main = task == self._main_task
            route = await choose_route(
                session,
                provider=provider,
                task=task,
                catalog=catalog,
                min_context=self._min_context if main else 0,
                at_least=_higher(self._floor if main else None, at_least),
            )
            return None if route is None else (route, catalog)

    async def model_for(self, task: TaskKind, *, at_least: Level | None = None) -> ChatModel | None:
        """The model of a call of ``task`` on the key, as it is now, counted
        (``metered.MeteredModel``); ``None`` when there is no route (no model fit for the task,
        or the key is gone). ``at_least`` is a level the caller wants at least for this call
        (the writing of a text refused at a lower level, ``texts.writer``): for the main task,
        the higher of it and the level the turn stepped up to. The lessons due are written first
        (``write_lessons``)."""
        await self.write_lessons()
        found = await self._route(task, at_least)
        if found is None:
            return None
        route, catalog = found
        self._routes[task] = route
        if task == self._main_task:
            self._conversation_route = route
            # Where a step up starts from: the model taken (no level counts as small).
            self._at = LEVELS[level_rank(route.level)]
        model = MeteredModel(
            self._model_factory(route.provider, route.model_id),
            route=route,
            person_id=self._person_id,
            session_factory=self._session_factory,
            clock=self._clock,
            catalog=catalog,
            notify=self._notify,
            tool_id=self._tool_id,
            build_id=self._build_id,
        )
        self._given.append(model)
        return model

    async def conversation(self) -> ChatModel:
        """The model of the next main call; ``ModelCallError("no_model")`` when there is none."""
        model = await self.model_for(self._main_task)
        if model is None:
            raise ModelCallError("no_model")
        return model

    def _level_now(self) -> Level:
        """The level the main calls are at: of the model the last of them took, or of the last
        step since (before any call, the level the main task starts at)."""
        if self._at is not None:
            return self._at
        return wanted_level(self._main_task, NO_CHOICES, raised=0)

    def _go_up(self) -> Level | None:
        """One step up for the main calls: the next level above the one they are at that the key
        has a model of for the main task (any, before the first call); ``None`` (and nothing
        changes) when there is none."""
        route = self._conversation_route
        levels = frozenset(LEVELS) if route is None else route.fit_levels
        rank = level_rank(self._level_now())
        for higher in LEVELS[rank + 1 :]:
            if higher in levels:
                self._floor = self._at = higher
                return higher
        return None

    def step_up(self) -> Level | None:
        """Domo judges the request deserves more: the main calls go up one level for the rest
        of the turn. The new level, or ``None`` (nothing changes) when the turn took
        ``MAX_STEP_UPS`` steps already or the key has no stronger model for it."""
        if self._step_ups >= MAX_STEP_UPS:
            return None
        higher = self._go_up()
        if higher is not None:
            self._step_ups += 1
        return higher

    def retry_up(self) -> bool:
        """The last round's calls could not be acted on (``chat.agent.run_turn``): the model of
        that round gets a lesson (written at the next call), and the next round goes up one
        level, unless the turn made ``MAX_RETRIES_UP`` such tries already or the key has no
        stronger model for it (``False`` then: the new try is at the same level)."""
        route = self._conversation_route
        if route is not None:
            self._lessons_due.append((route, INVALID_TOOL_CALL))
        if self._retries_up >= MAX_RETRIES_UP or self._go_up() is None:
            return False
        self._retries_up += 1
        return True

    async def note_failure(self, task: TaskKind, reason: str) -> None:
        """The last call of ``task`` the turn made did it badly, for ``reason``: a lesson about
        its model, written now. Nothing when the turn made no such call. It never raises."""
        route = self._routes.get(task)
        if route is not None:
            await self._write_lessons([(route, reason)])

    async def write_lessons(self) -> None:
        """Write the lessons due (``retry_up``). It never raises."""
        due, self._lessons_due = self._lessons_due, []
        if due:
            await self._write_lessons(due)

    async def _write_lessons(self, lessons: Sequence[tuple[Route, str]]) -> None:
        """Record each failure (``lessons.record_failure``) in a short transaction of its own,
        within ``_LESSON_SECONDS``: a lesson that cannot be written is logged, never raised."""
        with anyio.move_on_after(_LESSON_SECONDS) as attempt:
            try:
                async with self._session_factory() as session:
                    for route, reason in lessons:
                        await record_failure(
                            session,
                            provider_kind=route.provider.kind,
                            model_id=route.model_id,
                            task=route.task,
                            reason=reason,
                            now=self._clock(),
                        )
                    await session.commit()
            except Exception as error:
                logger.error(
                    "A lesson about a model could not be written (%s)", type(error).__name__
                )
        if attempt.cancelled_caught:
            logger.error("Writing a lesson about a model took too long")
