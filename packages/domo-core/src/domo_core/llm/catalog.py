"""The catalog of models with their prices, shipped with Domo.

It is a trimmed copy of the public models.dev list (three kinds of provider: Anthropic, OpenAI and
Google) with the day's dollar rate for one euro. A model a provider lists is found here by its
name; what is not found has unknown capabilities and prices.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import cache
from importlib import resources
from json import loads
from types import MappingProxyType
from typing import Final, cast

from domo_core.llm.provider_kinds import ProviderKind
from domo_core.llm.types import JsonObject


@dataclass(frozen=True)
class Prices:
    """Prices in US dollars per million tokens."""

    input: Decimal
    output: Decimal
    cache_read: Decimal
    cache_write: Decimal


FREE: Final = Prices(Decimal(0), Decimal(0), Decimal(0), Decimal(0))
UNKNOWN_PRICES: Final = Prices(Decimal(15), Decimal(75), Decimal(15), Decimal(75))

CATALOG_KINDS: Final[tuple[ProviderKind, ...]] = ("anthropic", "openai", "google")

_SOURCE: Final = "models.dev"
_DATED_SUFFIX: Final = re.compile(r"-\d{8}$")


@dataclass(frozen=True)
class ModelInfo:
    model_id: str
    prices: Prices
    context_tokens: int | None
    output_tokens: int | None
    tools: bool
    vision: bool


def bare_model_id(model_id: str) -> str:
    """A model name without its ``models/`` prefix and without a final ``-YYYYMMDD`` date."""
    return _DATED_SUFFIX.sub("", model_id.removeprefix("models/"))


@dataclass(frozen=True)
class Catalog:
    fetched: date
    usd_per_eur: Decimal
    entries: Mapping[ProviderKind, Mapping[str, ModelInfo]]

    def lookup(self, kind: ProviderKind, model_id: str) -> ModelInfo | None:
        """The entry of a model: by its exact name, then without ``models/``, then without a final
        date, then by the longest catalog name that starts it and is followed by ``-``."""
        entries = self.entries.get(kind, {})
        without_prefix = model_id.removeprefix("models/")
        for candidate in (model_id, without_prefix, bare_model_id(model_id)):
            if candidate in entries:
                return entries[candidate]
        starting = [name for name in entries if without_prefix.startswith(name + "-")]
        if starting:
            return entries[max(starting, key=len)]
        return None

    def highest_prices(self, kind: ProviderKind) -> Prices:
        """The highest price of each field among a kind's models, or ``UNKNOWN_PRICES`` when the
        kind has no entry."""
        models = self.entries.get(kind, {})
        if not models:
            return UNKNOWN_PRICES
        prices = [info.prices for info in models.values()]
        return Prices(
            max(p.input for p in prices),
            max(p.output for p in prices),
            max(p.cache_read for p in prices),
            max(p.cache_write for p in prices),
        )


def _decimal_text(value: object) -> str:
    """A price from the raw list, as text for ``Decimal`` (a float's repr is its shortest form)."""
    return format(Decimal(repr(value)).normalize(), "f")


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _object(value: object) -> JsonObject:
    """The value as a JSON object, or an empty one when it is not."""
    return cast(JsonObject, value) if isinstance(value, dict) else {}


def trim_models_dev(raw: JsonObject, *, fetched: date, usd_per_eur: Decimal) -> JsonObject:
    """The shipped file's content, from the raw models.dev list: three providers, the chat models
    that have a ``cost``, and the useful fields only. A missing cache price is the input price."""
    providers: JsonObject = {}
    for kind in CATALOG_KINDS:
        raw_models = _object(_object(raw.get(kind)).get("models"))
        kept: JsonObject = {}
        for model_id in sorted(raw_models):
            model = _object(raw_models[model_id])
            cost = _object(model.get("cost"))
            if "input" not in cost or "output" not in cost or model.get("type") == "embedding":
                continue
            limit = _object(model.get("limit"))
            inputs = _object(model.get("modalities")).get("input", [])
            kept[model_id] = {
                "input": _decimal_text(cost["input"]),
                "output": _decimal_text(cost["output"]),
                "cache_read": _decimal_text(cost.get("cache_read", cost["input"])),
                "cache_write": _decimal_text(cost.get("cache_write", cost["input"])),
                "context": _int_or_none(limit.get("context")),
                "output_tokens": _int_or_none(limit.get("output")),
                "tools": model.get("tool_call") is True,
                "vision": isinstance(inputs, list) and "image" in inputs,
            }
        providers[kind] = kept
    return {
        "source": _SOURCE,
        "fetched": fetched.isoformat(),
        "usd_per_eur": str(usd_per_eur),
        "providers": providers,
    }


def _model_info(model_id: object, item: JsonObject) -> ModelInfo:
    prices = Prices(
        Decimal(item["input"]),
        Decimal(item["output"]),
        Decimal(item["cache_read"]),
        Decimal(item["cache_write"]),
    )
    context, output = item["context"], item["output_tokens"]
    tools, vision = item["tools"], item["vision"]
    amounts = (prices.input, prices.output, prices.cache_read, prices.cache_write)
    if (
        not isinstance(model_id, str)
        or not isinstance(tools, bool)
        or not isinstance(vision, bool)
        or any(not (v is None or isinstance(v, int)) for v in (context, output))
        or any(not amount.is_finite() or amount < 0 for amount in amounts)
    ):
        raise ValueError("bad model entry")
    return ModelInfo(model_id, prices, context, output, tools, vision)


def catalog_from_json(data: JsonObject) -> Catalog:
    """The catalog a shipped file describes. ``ValueError`` on a bad shape."""
    try:
        entries: dict[ProviderKind, Mapping[str, ModelInfo]] = {}
        for kind in CATALOG_KINDS:
            models = {
                model_id: _model_info(model_id, item)
                for model_id, item in data["providers"][kind].items()
            }
            entries[kind] = MappingProxyType(models)
        usd_per_eur = Decimal(data["usd_per_eur"])
        if not usd_per_eur.is_finite() or usd_per_eur <= 0:
            raise ValueError("bad dollar rate")
        return Catalog(date.fromisoformat(data["fetched"]), usd_per_eur, MappingProxyType(entries))
    except (KeyError, TypeError, AttributeError, InvalidOperation) as error:
        raise ValueError("the catalog file has a bad shape") from error


@cache
def shipped_catalog() -> Catalog:
    """The catalog shipped in ``llm/data/models.json``."""
    text = (resources.files("domo_core.llm.data") / "models.json").read_text(encoding="utf-8")
    return catalog_from_json(cast(JsonObject, loads(text)))
