"""A model whose every call is counted on Domo's account, and is not made when it would pass a
firm limit (``llm.spending``; spec 4.7, 4.8).

``MeteredModel`` wraps the model of a route. For each call:

1. it counts what it sends: the system text, the texts of the history, the arguments and results
   of the tools, and the JSON of the tools it declares;
2. it reserves the most the call can cost (``costs.max_cost_usd``: that input, estimated in
   tokens, and the call's ceiling of output: what the caller asks, but never more than the
   catalog says the model gives, ``catalog.ModelInfo.output_tokens``, so that a model with a
   smaller limit is not asked for more than it gives), in a short transaction of its own
   (``spending.reserve``). A limit that would be passed raises ``ModelCallError("limit_reached")``
   before any request, and a key that is gone ``ModelCallError("no_model")``;
3. the model is asked with that ceiling, and its stream goes through as it is;
4. however the stream ends (to its end, failed, abandoned by its reader, cancelled), the row is
   settled with what the call cost (``spending.settle``), shielded from the cancellation and within
   a time limit. A settlement that fails is logged (by the kind of error only); the reservation is
   then settled at its maximum by the upkeep (``spending.settle_stale``).

``cost_usd`` and ``estimated`` say what its calls cost so far, even when a settlement could not be
written.
"""

import json
import logging
from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import aclosing
from datetime import datetime
from decimal import Decimal
from typing import Final
from uuid import UUID

import anyio

from domo_core.db import SessionFactory
from domo_core.llm.catalog import Catalog
from domo_core.llm.costs import estimate_tokens, max_cost_usd
from domo_core.llm.provider_kinds import PROVIDER_KINDS
from domo_core.llm.router import Route
from domo_core.llm.spending import LimitReached, call_cost, reserve, settle
from domo_core.llm.types import (
    AssistantToolCalls,
    ChatChunk,
    ChatMessage,
    ChatModel,
    ChatUsage,
    HistoryItem,
    ModelCallError,
    ToolCall,
    ToolChoice,
    ToolDefinition,
)
from domo_core.policy.settings import load_policy_settings

logger = logging.getLogger(__name__)

# How long writing what a call cost may take, once the call is over.
_SETTLE_SECONDS: Final = 10.0


def sent_chars(
    system: str, messages: Sequence[HistoryItem], tools: Sequence[ToolDefinition]
) -> int:
    """How many characters a call sends: the system text, the texts of the history, the names,
    arguments and results of the tool calls in it, and the tools declared, as JSON."""
    count = len(system)
    for item in messages:
        if isinstance(item, ChatMessage):
            count += len(item.content)
        elif isinstance(item, AssistantToolCalls):
            count += len(item.text) + sum(len(c.name) + len(c.arguments) for c in item.calls)
        else:
            count += len(item.name) + len(item.content)
    if tools:
        declared = [
            {"name": tool.name, "description": tool.description, "parameters": tool.parameters}
            for tool in tools
        ]
        count += len(json.dumps(declared, ensure_ascii=False))
    return count


def _output_limit_of(catalog: Catalog, route: Route) -> int | None:
    """The most output tokens the model of ``route`` gives, as ``catalog`` knows it; ``None``
    when it does not (a model it does not list, a kind of key it has no models of)."""
    for kind in PROVIDER_KINDS:
        if kind == route.provider.kind:
            info = catalog.lookup(kind, route.model_id)
            if info is not None and info.output_tokens is not None and info.output_tokens > 0:
                return info.output_tokens
    return None


def _added(total: ChatUsage | None, usage: ChatUsage) -> ChatUsage:
    if total is None:
        return usage
    return ChatUsage(
        total.input_tokens + usage.input_tokens,
        total.output_tokens + usage.output_tokens,
        total.cache_read_tokens + usage.cache_read_tokens,
        total.cache_write_tokens + usage.cache_write_tokens,
    )


class MeteredModel:
    """The model ``inner`` of ``route``, each call counted for ``person_id`` (see the module
    docstring). It is a ``ChatModel``. ``catalog`` is the catalog in force (its rate of the euro
    stands in when Domo has read none). ``notify`` is told whom a reservation kept news for, once
    it is committed (``tools.context.Notify``). ``tool_id`` and ``build_id`` are the tool and the
    build its calls are made for, when they build one of Domo's tools (``spending.reserve``)."""

    def __init__(
        self,
        inner: ChatModel,
        *,
        route: Route,
        person_id: UUID | None,
        session_factory: SessionFactory,
        clock: Callable[[], datetime],
        catalog: Catalog,
        notify: Callable[[UUID], None] | None = None,
        tool_id: UUID | None = None,
        build_id: UUID | None = None,
    ) -> None:
        self._inner = inner
        self._tool_id = tool_id
        self._build_id = build_id
        self._notify = notify
        self._route = route
        self._person_id = person_id
        self._session_factory = session_factory
        self._clock = clock
        self._catalog = catalog
        self._output_limit = _output_limit_of(catalog, route)
        self._cost = Decimal(0)
        self._estimated = False

    @property
    def route(self) -> Route:
        return self._route

    @property
    def cost_usd(self) -> Decimal:
        """What its calls cost so far, in dollars."""
        return self._cost

    @property
    def estimated(self) -> bool:
        """Whether the tokens of one of its calls were estimated from the length of the texts
        (the provider reported none)."""
        return self._estimated

    async def stream(
        self,
        *,
        system: str,
        messages: Sequence[HistoryItem],
        max_output_tokens: int,
        tools: Sequence[ToolDefinition] = (),
        tool_choice: ToolChoice = "auto",
        think: bool = True,
    ) -> AsyncGenerator[ChatChunk | ToolCall | ChatUsage]:
        if self._output_limit is not None:
            max_output_tokens = min(max_output_tokens, self._output_limit)
        input_chars = sent_chars(system, messages, tools)
        call_id = await self._reserve(
            max_cost_usd(
                self._route.prices,
                input_tokens=estimate_tokens(input_chars),
                max_output_tokens=max_output_tokens,
            )
        )
        usage: ChatUsage | None = None
        output_chars = 0
        answered = failed = False
        try:
            inner = self._inner.stream(
                system=system,
                messages=messages,
                max_output_tokens=max_output_tokens,
                tools=tools,
                tool_choice=tool_choice,
                think=think,
            )
            async with aclosing(inner) as items:
                async for item in items:
                    if isinstance(item, ChatChunk):
                        output_chars += len(item.text)
                        answered = answered or bool(item.text)
                    elif isinstance(item, ToolCall):
                        output_chars += len(item.name) + len(item.arguments)
                        answered = True
                    elif item.input_tokens or item.output_tokens:
                        # Counts of 0 are a provider that counted nothing (an answer cut short
                        # still says so): the call is then settled from the length of the texts.
                        usage = _added(usage, item)
                    yield item
        except Exception:
            failed = True
            raise
        finally:
            await self._settle(
                call_id,
                usage=usage,
                input_chars=input_chars,
                output_chars=output_chars,
                failed_before_any_answer=failed and not answered,
            )

    async def _reserve(self, max_usd: Decimal) -> UUID:
        """The reservation of a call, in a transaction of its own; ``ModelCallError`` when the
        call may not be made (``limit_reached``, ``no_model``). The lock is let go first by a
        commit: a refused reservation wrote nothing, and a commit, unlike a rollback, leaves what
        the session holds as it is."""
        news: list[UUID] = []
        async with self._session_factory() as session:
            try:
                zone = (await load_policy_settings(session)).time_zone
                reserved = await reserve(
                    session,
                    route=self._route,
                    person_id=self._person_id,
                    max_usd=max_usd,
                    now=self._clock(),
                    catalog=self._catalog,
                    zone=zone,
                    news=news,
                    tool_id=self._tool_id,
                    build_id=self._build_id,
                )
            except ModelCallError:
                await session.commit()
                raise
            if isinstance(reserved, LimitReached):
                await session.commit()
                logger.info("A call to a model was not made: it would pass a spending limit")
                raise ModelCallError("limit_reached")
            await session.commit()
        if self._notify is not None:
            for person_id in news:
                self._notify(person_id)
        return reserved

    async def _settle(
        self,
        call_id: UUID,
        *,
        usage: ChatUsage | None,
        input_chars: int,
        output_chars: int,
        failed_before_any_answer: bool,
    ) -> None:
        cost = call_cost(
            usage=usage,
            input_chars=input_chars,
            output_chars=output_chars,
            prices=self._route.prices,
            failed_before_any_answer=failed_before_any_answer,
        )
        self._cost += cost.usd
        self._estimated = self._estimated or cost.estimated
        with anyio.move_on_after(_SETTLE_SECONDS, shield=True):
            try:
                async with self._session_factory() as session:
                    await settle(
                        session,
                        call_id,
                        usage=usage,
                        input_chars=input_chars,
                        output_chars=output_chars,
                        prices=self._route.prices,
                        failed_before_any_answer=failed_before_any_answer,
                        now=self._clock(),
                    )
                    await session.commit()
                return
            except Exception as error:
                logger.error("A call to a model could not be settled (%s)", type(error).__name__)
                return
        logger.error("Settling a call to a model took too long")
