"""Choosing a model among the ones a key opens.

``candidates`` describes each listed model (its level, price, tools and context window) and
``pick_model`` takes the best one for a kind of task, by the rule of the plan:

* the pinned model, if it is listed and fit for the task;
* else the cheapest of the level wanted (input plus output price; at an equal price a model whose
  level is known comes before one that has none, then by name);
* else the levels above, the nearest first;
* else, for a task that is not protected, the strongest below.

A model with no level counts as ``small`` for the order. A task that is protected (the yes judge,
the reading of a yes, the writing of rights) only takes a model whose level is known and at least
the task's minimum. A model with no catalog entry is supposed to call tools, and a model whose
context window is known and smaller than the request is dropped before the choice.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final
from urllib.parse import urlsplit

from domo_common.transport import cleartext_allowed

from domo_core.llm.catalog import FREE, Catalog, Prices
from domo_core.llm.levels import LEVELS, Level, level_rank, table_level
from domo_core.llm.models import Provider
from domo_core.llm.provider_kinds import PROVIDER_KINDS, ProviderKind
from domo_core.llm.tasks import PROTECTED, TASKS, TaskKind

_LOCAL_KIND: Final = "openai_compatible"


@dataclass(frozen=True)
class Candidate:
    """A model a key lists, as the choice sees it. ``price_known`` is false when ``prices`` is
    the highest price of the provider, taken for want of a catalog entry."""

    model_id: str
    level: Level | None
    prices: Prices
    tools: bool
    context_tokens: int | None
    price_known: bool


def is_local(provider: Provider) -> bool:
    """Whether the key is a server of the household's own: a server that imitates OpenAI and has
    no key, or whose address the gatekeeper would send a key to in cleartext (a local one)."""
    if provider.kind != _LOCAL_KIND:
        return False
    return provider.secret_ref is None or cleartext_allowed(
        urlsplit(provider.base_url).hostname or ""
    )


def _kind_of(provider: Provider) -> ProviderKind:
    for known in PROVIDER_KINDS:
        if known == provider.kind:
            return known
    raise RuntimeError(f"the provider {provider.id} has an unknown kind: {provider.kind!r}")


def candidates(
    catalog: Catalog,
    provider: Provider,
    model_ids: Iterable[str],
    owner_levels: Mapping[str, Level],
) -> list[Candidate]:
    """The models ``model_ids`` of the key ``provider``, described. The level is the owner's word
    when there is one, else the project's table; the price is nothing for a local key, else the
    catalog's, else the highest of the provider's models."""
    kind = _kind_of(provider)
    local = is_local(provider)
    fallback = catalog.highest_prices(kind)
    described: list[Candidate] = []
    for model_id in model_ids:
        info = catalog.lookup(kind, model_id)
        if local:
            prices, known = FREE, True
        elif info is not None:
            prices, known = info.prices, True
        else:
            prices, known = fallback, False
        described.append(
            Candidate(
                model_id=model_id,
                level=owner_levels.get(model_id) or table_level(model_id),
                prices=prices,
                tools=True if info is None else info.tools,
                context_tokens=None if info is None else info.context_tokens,
                price_known=known,
            )
        )
    return described


def _order(candidate: Candidate) -> tuple[Decimal, int, str]:
    price = candidate.prices.input + candidate.prices.output
    return (price, 0 if candidate.level is not None else 1, candidate.model_id)


def _cheapest(options: Sequence[Candidate]) -> Candidate | None:
    return min(options, key=_order, default=None)


def fit_for(cands: Sequence[Candidate], task: TaskKind, *, min_context: int = 0) -> list[Candidate]:
    """The models of ``cands`` that may do ``task``: those that call tools when it needs them,
    whose known context holds the request (``min_context``, in estimated tokens), and, for a
    protected task, whose known level is at least its minimum."""
    spec = TASKS[task]
    fit = [
        c
        for c in cands
        if (c.tools or not spec.needs_tools)
        and (c.context_tokens is None or c.context_tokens >= min_context)
    ]
    if task in PROTECTED and spec.minimum is not None:
        floor = level_rank(spec.minimum)
        fit = [c for c in fit if c.level is not None and level_rank(c.level) >= floor]
    return fit


def pick_model(
    cands: Sequence[Candidate],
    task: TaskKind,
    *,
    wanted: Level,
    pinned: str | None = None,
    min_context: int = 0,
) -> Candidate | None:
    """The best model of ``cands`` for ``task`` at the level ``wanted`` (see the module docstring),
    or ``None`` when none is fit. ``min_context`` is the size of the request, in estimated tokens:
    a model whose known context is smaller is dropped."""
    fit = fit_for(cands, task, min_context=min_context)
    if pinned is not None:
        for c in fit:
            if c.model_id == pinned:
                return c
    wanted_rank = level_rank(wanted)
    for rank in range(wanted_rank, len(LEVELS)):
        chosen = _cheapest([c for c in fit if level_rank(c.level) == rank])
        if chosen is not None:
            return chosen
    if task in PROTECTED:
        return None
    for rank in range(wanted_rank - 1, -1, -1):
        chosen = _cheapest([c for c in fit if level_rank(c.level) == rank])
        if chosen is not None:
            return chosen
    return None
