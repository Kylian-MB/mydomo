"""The catalog of models, their levels and prices, the kinds of tasks and the cost helpers."""

from datetime import date
from decimal import Decimal
from importlib import resources

import pytest
from domo_core.llm.catalog import (
    CATALOG_KINDS,
    FREE,
    UNKNOWN_PRICES,
    Prices,
    bare_model_id,
    catalog_from_json,
    shipped_catalog,
    trim_models_dev,
)
from domo_core.llm.costs import cost_usd, estimate_tokens, max_cost_usd
from domo_core.llm.levels import LEVELS, level_rank, table_level
from domo_core.llm.tasks import PROTECTED, TASKS, TaskSpec
from domo_core.llm.types import ChatUsage, JsonObject


def _entry(**changes: object) -> JsonObject:
    entry: JsonObject = {
        "input": "3",
        "output": "15",
        "cache_read": "0.3",
        "cache_write": "3.75",
        "context": 200000,
        "output_tokens": 64000,
        "tools": True,
        "vision": True,
    }
    entry.update(changes)
    return entry


def _file(anthropic: JsonObject, google: JsonObject | None = None) -> JsonObject:
    return {
        "source": "models.dev",
        "fetched": "2026-11-08",
        "usd_per_eur": "1.0800",
        "providers": {"anthropic": anthropic, "openai": {}, "google": google or {}},
    }


@pytest.mark.parametrize(
    ("model_id", "level"),
    [
        ("claude-haiku-4-5", "small"),
        ("claude-sonnet-4-5-20250929", "medium"),
        ("claude-opus-4-1", "strong"),
        ("gpt-5-mini", "small"),
        ("gpt-5-nano", "small"),
        ("gpt-5", "medium"),
        ("gpt-5-pro", "strong"),
        ("o3", "strong"),
        ("o4-mini", "small"),
        ("models/gemini-2.5-flash", "medium"),
        ("gemini-2.5-flash-lite", "small"),
        ("gemini-2.5-pro", "strong"),
        ("anthropic/claude-sonnet-4.5", "medium"),
        ("fake-haiku", "small"),
        ("fake-sonnet", "medium"),
        ("llama3.1:8b", None),
        ("gemini-2.5-flash", "medium"),
        ("gpt-4o-mini", "small"),
    ],
)
def test_the_levels_of_the_table(model_id: str, level: str | None) -> None:
    assert table_level(model_id) == level


def test_whole_words_only_for_nano_mini_lite_and_pro() -> None:
    assert table_level("gemini-1.0-ultra") is None  # "mini" inside "gemini" is not a word
    assert table_level("promax-1") is None  # "pro" inside a longer word is not a word


def test_levels_rank_from_small_and_none_counts_as_small() -> None:
    assert LEVELS == ("small", "medium", "strong")
    assert [level_rank(level) for level in (None, *LEVELS)] == [0, 0, 1, 2]


def test_a_listed_model_finds_its_entry() -> None:
    catalog = catalog_from_json(
        _file({"claude-sonnet-4-5": _entry()}, {"gemini-2.5-flash": _entry(input="0.3")})
    )
    found = catalog.lookup("anthropic", "claude-sonnet-4-5-20250929")
    assert found is not None and found.model_id == "claude-sonnet-4-5"
    google = catalog.lookup("google", "models/gemini-2.5-flash")
    assert google is not None and google.model_id == "gemini-2.5-flash"
    assert catalog.lookup("anthropic", "claude-unknown") is None
    assert catalog.lookup("openai", "gpt-5") is None


def test_the_longest_catalog_name_that_starts_the_model_wins() -> None:
    catalog = catalog_from_json(_file({"claude-opus": _entry(), "claude-opus-4": _entry()}))
    found = catalog.lookup("anthropic", "claude-opus-4-preview")
    assert found is not None and found.model_id == "claude-opus-4"
    assert catalog.lookup("anthropic", "claude-opus4") is None  # needs the "-" after the name


def test_bare_model_id_drops_the_prefix_and_the_final_date() -> None:
    assert bare_model_id("models/gemini-2.5-flash") == "gemini-2.5-flash"
    assert bare_model_id("claude-sonnet-4-5-20250929") == "claude-sonnet-4-5"
    assert bare_model_id("gpt-5") == "gpt-5"


def test_highest_prices_go_field_by_field() -> None:
    catalog = catalog_from_json(
        _file(
            {
                "claude-a": _entry(input="1", output="50", cache_read="0.1", cache_write="1"),
                "claude-b": _entry(input="5", output="10", cache_read="0.5", cache_write="2"),
            }
        )
    )
    assert catalog.highest_prices("anthropic") == Prices(
        Decimal(5), Decimal(50), Decimal("0.5"), Decimal(2)
    )
    assert catalog.highest_prices("openai") == UNKNOWN_PRICES
    assert Prices(Decimal(15), Decimal(75), Decimal(15), Decimal(75)) == UNKNOWN_PRICES
    assert Prices(Decimal(0), Decimal(0), Decimal(0), Decimal(0)) == FREE


@pytest.mark.parametrize(
    "broken",
    [
        {},
        {"fetched": "2026-11-08", "usd_per_eur": "1.08"},
        {**_file({}), "usd_per_eur": "free"},
        {**_file({}), "usd_per_eur": "0"},
        {**_file({}), "fetched": "yesterday"},
        _file({"m": {"input": "3"}}),
        _file({"m": _entry(input="cheap")}),
        _file({"m": _entry(input="-1")}),
        _file({"m": _entry(tools="yes")}),
        _file({"m": _entry(context="big")}),
    ],
)
def test_a_bad_shape_is_a_value_error(broken: JsonObject) -> None:
    with pytest.raises(ValueError):
        catalog_from_json(broken)


def test_trimming_keeps_three_providers_and_the_useful_fields() -> None:
    priced = {
        "id": "claude-sonnet-4-5",
        "name": "Claude Sonnet 4.5",
        "tool_call": True,
        "modalities": {"input": ["text", "image"], "output": ["text"]},
        "limit": {"context": 200000, "output": 64000},
        "cost": {"input": 3, "output": 15, "cache_read": 0.3, "cache_write": 3.75},
    }
    raw: JsonObject = {
        "anthropic": {
            "models": {
                "claude-sonnet-4-5": priced,
                "claude-no-cost": {"id": "claude-no-cost", "tool_call": True},
            }
        },
        "openai": {
            "models": {
                "gpt-5": {
                    "tool_call": False,
                    "modalities": {"input": ["text"]},
                    "limit": {"context": 400000, "output": 128000},
                    "cost": {"input": 1.25, "output": 10, "cache_read": 0.125},
                },
                "text-embedding": {"type": "embedding", "cost": {"input": 0.1, "output": 0}},
            }
        },
        "google": {"models": {"gemini-2.5-flash": {"cost": {"input": 0.3, "output": 2.5}}}},
        "openrouter": {"models": {"x/y": priced}},
    }
    trimmed = trim_models_dev(raw, fetched=date(2026, 11, 8), usd_per_eur=Decimal("1.0800"))
    assert set(trimmed["providers"]) == {"anthropic", "openai", "google"}
    assert trimmed["fetched"] == "2026-11-08" and trimmed["usd_per_eur"] == "1.0800"
    assert list(trimmed["providers"]["anthropic"]) == ["claude-sonnet-4-5"]  # no cost: gone
    assert list(trimmed["providers"]["openai"]) == ["gpt-5"]  # an embedding model: gone
    assert trimmed["providers"]["anthropic"]["claude-sonnet-4-5"] == _entry()
    gpt = trimmed["providers"]["openai"]["gpt-5"]
    assert gpt["cache_write"] == "1.25"  # a missing cache price is the input price
    assert (gpt["tools"], gpt["vision"]) == (False, False)
    flash = trimmed["providers"]["google"]["gemini-2.5-flash"]
    assert (flash["context"], flash["output_tokens"]) == (None, None)
    for entry in (*trimmed["providers"]["anthropic"].values(), gpt, flash):
        assert set(entry) == set(_entry())
    assert catalog_from_json(trimmed).lookup("openai", "gpt-5") is not None


def test_the_shipped_catalog_reads() -> None:
    catalog = shipped_catalog()
    assert all(catalog.entries[kind] for kind in CATALOG_KINDS)
    assert catalog.usd_per_eur > 0
    models = resources.files("domo_core.llm.data") / "models.json"
    assert len(models.read_bytes()) < 300_000
    assert shipped_catalog() is catalog


def test_the_costs_of_a_call() -> None:
    p = Prices(Decimal(3), Decimal(15), Decimal("0.3"), Decimal("3.75"))
    assert cost_usd(p, ChatUsage(1_000_000, 100_000, cache_read_tokens=200_000)) == Decimal("3.96")
    assert max_cost_usd(p, input_tokens=1000, max_output_tokens=2048) == Decimal("0.03447")
    assert estimate_tokens(10) == 4
    assert estimate_tokens(0) == 0 and estimate_tokens(9) == 3


def test_the_cost_never_has_a_negative_plain_input() -> None:
    p = Prices(Decimal(3), Decimal(15), Decimal("0.3"), Decimal("3.75"))
    odd = ChatUsage(100, 0, cache_read_tokens=100, cache_write_tokens=100)
    assert cost_usd(p, odd) == (Decimal("0.3") * 100 + Decimal("3.75") * 100) / 1_000_000
    assert cost_usd(FREE, ChatUsage(5, 5)) == 0


def test_the_usage_has_no_cache_by_default() -> None:
    usage = ChatUsage(10, 5)
    assert (usage.cache_read_tokens, usage.cache_write_tokens) == (0, 0)


def test_the_tasks_table() -> None:
    assert TASKS["judge"] == TaskSpec(False, "medium", "medium")
    assert TASKS["conversation"] == TaskSpec(True, "medium", None)
    assert TASKS["upkeep"] == TaskSpec(True, "small", None)
    assert TASKS["key_check"] == TaskSpec(True, "small", None)
    assert TASKS["intent"] == TaskSpec(False, "medium", "medium")
    assert TASKS["rights_writer"] == TaskSpec(True, "medium", "medium")
    assert {"judge", "intent", "rights_writer", "tool_review"} == PROTECTED
    assert TASKS["writing"] == TaskSpec(True, "small", None)
    assert TASKS["checking"] == TaskSpec(True, "medium", None)
    assert TASKS["arrival"] == TaskSpec(True, "small", None)
    assert TASKS["memory"] == TaskSpec(True, "small", None)
    assert TASKS["toolmaking"] == TaskSpec(True, "medium", None)
    assert TASKS["tool_review"] == TaskSpec(False, "medium", "medium")
