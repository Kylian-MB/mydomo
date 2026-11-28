"""Which listed models hear, say or recognise a voice, and what the paid ones cost.

``llm/data/speech_models.json`` lists them by name (an exact id, or a prefix ending in ``*``);
a model that is not listed is a chat model. Prices are in US dollars: per minute of sound heard,
per million characters said.
"""

from dataclasses import dataclass
from decimal import Decimal
from functools import cache
from importlib import resources
from json import loads
from typing import Final, cast

from domo_core.llm.provider_kinds import ProviderKind
from domo_core.llm.tasks import Does
from domo_core.llm.types import JsonObject


@dataclass(frozen=True)
class SpeechPrices:
    """Prices in US dollars: ``per_minute_heard`` and ``per_million_chars_said``."""

    per_minute_heard: Decimal
    per_million_chars_said: Decimal


FREE_SPEECH: Final = SpeechPrices(Decimal(0), Decimal(0))

_DOES: Final[tuple[Does, ...]] = ("hear", "say", "voice_print")


@dataclass(frozen=True)
class _SpeechFile:
    names: dict[Does, tuple[str, ...]]
    prices: dict[str, SpeechPrices]


@cache
def _shipped() -> _SpeechFile:
    text = (resources.files("domo_core.llm.data") / "speech_models.json").read_text(
        encoding="utf-8"
    )
    data = cast(JsonObject, loads(text))
    names: dict[Does, tuple[str, ...]] = {
        does: tuple(str(name) for name in cast(list[object], data[does])) for does in _DOES
    }
    prices = {
        key: SpeechPrices(
            Decimal(item["per_minute_heard"]), Decimal(item["per_million_chars_said"])
        )
        for key, item in cast(dict[str, JsonObject], data["prices"]).items()
    }
    return _SpeechFile(names, prices)


def _matches(model_id: str, name: str) -> bool:
    if name.endswith("*"):
        return model_id.startswith(name[:-1])
    return model_id == name


def does_of(kind: ProviderKind, model_id: str) -> Does:
    """What the model does: ``hear``, ``say``, ``voice_print``, else ``chat`` (a model that is not
    listed). ``kind`` is the provider's, for the day two providers name a model alike."""
    del kind
    bare = model_id.removeprefix("models/")
    for does, names in _shipped().names.items():
        if any(_matches(bare, name) for name in names):
            return does
    return "chat"


def __getattr__(name: str) -> SpeechPrices:
    """``UNKNOWN_SPEECH_PRICES``, the highest of the file, read when first asked for."""
    if name == "UNKNOWN_SPEECH_PRICES":
        prices = _shipped().prices.values()
        return SpeechPrices(
            max(p.per_minute_heard for p in prices), max(p.per_million_chars_said for p in prices)
        )
    raise AttributeError(name)


def speech_prices(kind: ProviderKind, model_id: str) -> SpeechPrices | None:
    """The prices of a paid speech model, ``None`` when the file does not know it."""
    bare = model_id.removeprefix("models/")
    return _shipped().prices.get(f"{kind}/{bare}")
